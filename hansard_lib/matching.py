"""
Name normalization and fuzzy matching of speakers (as printed in a Hansard
transcript) against an MP roster and one or more Cabinet snapshots.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Optional

import pandas as pd

try:
    from rapidfuzz import fuzz, process

    _HAVE_RAPIDFUZZ = True
except ImportError:  # pragma: no cover - fallback path
    import difflib

    _HAVE_RAPIDFUZZ = False

from .parser import Turn

HONORIFICS = {
    "YB", "TUAN", "PUAN", "DATO", "DATUK", "SERI", "SRI", "HAJI", "HAJJAH",
    "HJ", "HJH", "DR", "PROF", "PROFESSOR", "TUN",
    "ENCIK", "CIK", "IR", "TS", "SENATOR", "PANGLIMA", "WIRA", "AMAR",
    "USTAZ", "USTAZAH", "MEJEN", "JEN", "KOL", "LT", "BRIG", "ARM", "ADM",
    "ABANG", "PEHIN", "BENTARA", "ORANG", "KAYA", "PM",
}
# Words that are *usually* honorifics but, in this roster, also turn up as
# real given-name/surname components (e.g. "Wan Ahmad Fayhsal", "Syed Saddiq",
# "Nik Nazmi", "Awang bin Hashim", "Tan Kok Wai" the Chinese surname).
# Stripping them unconditionally loses identifying signal and causes false
# collisions between different people (e.g. "Awang bin Hashim" collapsing to
# just "HASHIM"), so they're deliberately left out of HONORIFICS. "TAN" only
# functions as a pure title in the two-word phrase "Tan Sri", which is
# stripped explicitly below before tokenization.
_AMBIGUOUS_TITLE_PHRASES = [r"\bTAN\s+SRI\b"]
CONNECTORS = {"BIN", "BINTI", "A/L", "A/P"}


def normalize_name(name: Optional[str]) -> str:
    if not name:
        return ""
    s = str(name).upper()
    s = s.replace("’", "'").replace("`", "'")
    s = s.replace("@", " ")
    s = re.sub(r"[.,]", " ", s)
    s = s.replace("-", " ")
    s = s.replace("'", "")
    s = re.sub(r"\bA/L\b|\bA/P\b", " ", s)
    for phrase in _AMBIGUOUS_TITLE_PHRASES:
        s = re.sub(phrase, " ", s)
    tokens = [t.strip("'") for t in s.split() if t.strip("'")]
    out = [t for t in tokens if t not in HONORIFICS and t not in CONNECTORS]
    return " ".join(out)


def normalize_place(place: Optional[str]) -> str:
    if not place:
        return ""
    s = str(place).upper().strip()
    s = s.replace("’", "'")
    s = re.sub(r"\bW\.?P\.?\b", "WP", s)
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _best_match(query: str, choices: list[str], score_cutoff: float = 0.0):
    """Returns (index_into_choices, score_0_100) or (None, 0)."""
    if not query or not choices:
        return None, 0.0
    if _HAVE_RAPIDFUZZ:
        res = process.extractOne(
            query, choices, scorer=fuzz.token_sort_ratio, score_cutoff=score_cutoff
        )
        if res is None:
            return None, 0.0
        _, score, idx = res
        return idx, float(score)
    # difflib fallback
    best_idx, best_score = None, 0.0
    for i, c in enumerate(choices):
        score = difflib.SequenceMatcher(None, query, c).ratio() * 100
        if score > best_score:
            best_idx, best_score = i, score
    if best_score < score_cutoff:
        return None, 0.0
    return best_idx, best_score


def confidence_label(score: float) -> str:
    if score >= 88:
        return "High"
    if score >= 65:
        return "Medium"
    if score > 0:
        return "Low"
    return "None"


@dataclass
class CabinetSnapshot:
    label: str
    df: pd.DataFrame
    name_col: str
    jawatan_col: str
    kementerian_col: str
    effective_date: Optional[date] = None
    timbalan_col: Optional[str] = None
    senator_col: Optional[str] = None

    def norm_names(self) -> list[str]:
        return [normalize_name(n) for n in self.df[self.name_col].tolist()]


def choose_snapshot_for_date(
    snapshots: list[CabinetSnapshot], sitting_date: Optional[date]
) -> Optional[CabinetSnapshot]:
    if not snapshots:
        return None
    if len(snapshots) == 1 or sitting_date is None:
        return snapshots[0]
    dated = [s for s in snapshots if s.effective_date is not None]
    if not dated:
        return snapshots[0]
    on_or_before = [s for s in dated if s.effective_date <= sitting_date]
    if on_or_before:
        return max(on_or_before, key=lambda s: s.effective_date)
    return min(dated, key=lambda s: s.effective_date)


def guess_column(columns: list[str], keywords: list[str]) -> Optional[str]:
    cols_upper = {c: str(c).upper() for c in columns}
    for kw in keywords:
        for c, up in cols_upper.items():
            if kw in up:
                return c
    return None


def collect_speaker_occurrences(turns: list[Turn]) -> dict[str, dict]:
    """Groups non-CHAIR turns by normalize_name(speaker_raw). Shared by the
    speaker-consolidation table below and build_constituency_directory."""
    by_key: dict[str, dict] = {}
    for t in turns:
        if t.kind == "CHAIR":
            continue
        raw = t.speaker_raw
        key = normalize_name(raw)
        if not key:
            continue
        entry = by_key.setdefault(
            key,
            {"speaker_key": key, "speaker_raw": raw, "constituency_raw": "", "occurrences": 0},
        )
        entry["occurrences"] += 1
        if not entry["constituency_raw"] and t.constituency_raw:
            entry["constituency_raw"] = t.constituency_raw
        # Prefer the longest raw form seen (tends to be the most complete /
        # least truncated by line-wrapping).
        if len(raw) > len(entry["speaker_raw"]):
            entry["speaker_raw"] = raw
    return by_key


def apply_merge_map(
    by_key: dict[str, dict], merge_map: Optional[dict[str, str]]
) -> dict[str, dict]:
    """Re-keys a collect_speaker_occurrences() result using a user-confirmed
    merge_map (raw normalized key -> canonical normalized key) from the
    Speaker Consolidation UI, combining occurrences/constituency/
    representative text across merged variants."""
    if not merge_map:
        return by_key
    merged: dict[str, dict] = {}
    for key, entry in by_key.items():
        canon = merge_map.get(key, key)
        m = merged.setdefault(
            canon,
            {"speaker_key": canon, "speaker_raw": entry["speaker_raw"], "constituency_raw": "", "occurrences": 0},
        )
        m["occurrences"] += entry["occurrences"]
        if not m["constituency_raw"] and entry["constituency_raw"]:
            m["constituency_raw"] = entry["constituency_raw"]
        if len(entry["speaker_raw"]) > len(m["speaker_raw"]):
            m["speaker_raw"] = entry["speaker_raw"]
    return merged


def _self_similarity_score(a: str, b: str) -> float:
    """Similarity between two already-normalized speaker keys. Word order
    rarely differs between OCR/typo variants of the same person's name (that's
    what token_sort_ratio is for), but merged/dropped letters and a dropped or
    added nickname token do - e.g. "AHMAD ZAHID HAMIDI" vs "AHMAD ZAHIDHAMIDI"
    scores only ~69 on token_sort_ratio (token boundaries shifted) but ~97 on
    a plain character ratio; "MOKTAR RADIN" vs "BUNG MOKTAR RADIN" scores ~83
    on ratio/token_sort but 100 on token_set_ratio (handles inserted tokens).
    Taking the max of these three catches all of the above. Deliberately NOT
    using partial_ratio here - it scores a shared surname as near-perfect even
    when the rest of the name is completely different (e.g. two unrelated
    people who are both "... bin Mohd Nor" scored 88, exactly at cutoff),
    which produces false-positive merge suggestions between different real
    people."""
    if _HAVE_RAPIDFUZZ:
        return max(
            fuzz.token_sort_ratio(a, b),
            fuzz.ratio(a, b),
            fuzz.token_set_ratio(a, b),
        )
    return difflib.SequenceMatcher(None, a, b).ratio() * 100


def suggest_duplicate_targets(
    keys: list[str], occurrences: dict[str, int], score_cutoff: float = 88
) -> dict[str, str]:
    """Flags likely-duplicate speaker variants by comparing each normalized
    key against every other one - catches truncated/typo'd/honorific-
    inconsistent variants normalize_name() alone didn't already merge.
    Always suggests merging the less-frequent variant into the more-
    frequent one, never the reverse. This is only a starting suggestion -
    the user confirms/corrects it in the UI before it's applied to anything."""
    suggestions: dict[str, str] = {}
    for k in keys:
        best_candidate, best_score = None, 0.0
        for c in keys:
            if c == k:
                continue
            score = _self_similarity_score(k, c)
            if score > best_score:
                best_candidate, best_score = c, score
        if best_candidate is None or best_score < score_cutoff:
            continue
        if occurrences.get(best_candidate, 0) > occurrences.get(k, 0):
            suggestions[k] = best_candidate
    return suggestions


def build_unique_speakers_table(turns: list[Turn], score_cutoff: float = 88) -> pd.DataFrame:
    """One row per unique speaker (pre-consolidation), with a suggested
    duplicate target where the fuzzy matcher finds a likely match among the
    *other* speakers - feeds the "Speaker Consolidation" review UI."""
    by_key = collect_speaker_occurrences(turns)
    keys = list(by_key.keys())
    occurrences = {k: by_key[k]["occurrences"] for k in keys}
    printed = {k: by_key[k]["speaker_raw"] for k in keys}
    suggestions = suggest_duplicate_targets(keys, occurrences, score_cutoff=score_cutoff)

    rows = []
    for k in sorted(keys, key=lambda kk: -occurrences[kk]):
        sugg = suggestions.get(k)
        rows.append(
            {
                "Speaker (As Printed)": printed[k],
                "Occurrences": occurrences[k],
                "Merge Into": printed[sugg] if sugg else "(keep separate)",
                "Auto-Suggested": "Yes" if sugg else "",
                "_speaker_key": k,
            }
        )
    return pd.DataFrame(rows)


def resolve_merge_map(edited_speakers: pd.DataFrame) -> dict[str, str]:
    """Turns the user-edited "Merge Into" column (printed-name selections)
    into a normalized-key -> canonical-normalized-key map, following chains
    (A merges into B, B merges into C => A and B both resolve to C)."""
    if edited_speakers is None or edited_speakers.empty:
        return {}
    printed_to_key = {
        row["Speaker (As Printed)"]: row["_speaker_key"] for _, row in edited_speakers.iterrows()
    }
    target_key: dict[str, str] = {}
    for _, row in edited_speakers.iterrows():
        k = row["_speaker_key"]
        target_label = row.get("Merge Into") or "(keep separate)"
        if target_label != "(keep separate)" and target_label in printed_to_key:
            t = printed_to_key[target_label]
            if t != k:
                target_key[k] = t

    resolved: dict[str, str] = {}
    for k in printed_to_key.values():
        seen = set()
        cur = k
        while cur in target_key and cur not in seen:
            seen.add(cur)
            cur = target_key[cur]
        resolved[k] = cur
    return resolved


def build_constituency_directory(
    turns: list[Turn],
    roster_df: Optional[pd.DataFrame],
    name_col: Optional[str],
    const_col: Optional[str],
    merge_map: Optional[dict[str, str]] = None,
) -> pd.DataFrame:
    """One row per unique speaker (by normalized name, after applying any
    user-confirmed speaker-consolidation merges) with an auto-matched
    constituency, ready for manual review/editing."""
    by_key = apply_merge_map(collect_speaker_occurrences(turns), merge_map)

    rows = []
    roster_names = roster_df[name_col].tolist() if (roster_df is not None and name_col) else []
    roster_consts = roster_df[const_col].tolist() if (roster_df is not None and const_col) else []
    roster_names_norm = [normalize_name(n) for n in roster_names]
    roster_const_norm = [normalize_place(c) for c in roster_consts]

    for entry in by_key.values():
        matched_name, matched_const, score = "", "", 0.0

        if entry["constituency_raw"] and roster_consts:
            # Constrain the name lookup to roster rows whose constituency
            # matches the one printed in the transcript - far more reliable
            # than a global name search.
            cidx, cscore = _best_match(normalize_place(entry["constituency_raw"]), roster_const_norm, score_cutoff=80)
            if cidx is not None:
                matched_const = roster_consts[cidx]
                name_score = _best_match(entry["speaker_key"], [roster_names_norm[cidx]])[1]
                matched_name = roster_names[cidx]
                score = name_score
            else:
                matched_const = entry["constituency_raw"]
        elif roster_names_norm:
            # Unconstrained search across the whole roster (used for
            # minister-style tags, which carry no constituency in the
            # transcript) - needs a stricter cutoff than the
            # constituency-constrained branch above, since a global search
            # across ~200+ names can otherwise turn up coincidental
            # high-ish-scoring but wrong matches.
            idx, score = _best_match(entry["speaker_key"], roster_names_norm, score_cutoff=78)
            if idx is not None:
                matched_name = roster_names[idx]
                matched_const = roster_consts[idx]

        if not matched_const:
            matched_const = entry["constituency_raw"]
        if not matched_name:
            matched_name = entry["speaker_raw"]

        rows.append(
            {
                "Speaker (As Printed)": entry["speaker_raw"],
                "Matched Name": matched_name,
                "Constituency": matched_const,
                "Occurrences": entry["occurrences"],
                "Match Confidence": confidence_label(score) if score else ("Direct" if entry["constituency_raw"] else "None"),
                "_speaker_key": entry["speaker_key"],
            }
        )

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Occurrences", ascending=False).reset_index(drop=True)
    return df


def build_role_directory(
    turns: list[Turn],
    snapshots: list[CabinetSnapshot],
    pdf_sitting_dates: dict[str, Optional[date]],
    merge_map: Optional[dict[str, str]] = None,
) -> pd.DataFrame:
    """One row per (speaker, cabinet snapshot actually used) combination,
    after applying any user-confirmed speaker-consolidation merges."""
    if not snapshots:
        return pd.DataFrame(
            columns=[
                "Speaker (As Printed)", "Matched Name", "Jawatan", "Kementerian",
                "Cabinet Snapshot", "Match Confidence", "_speaker_key", "_snapshot_label",
            ]
        )

    by_key: dict[tuple[str, str], dict] = {}
    for t in turns:
        if t.kind == "CHAIR":
            continue
        snap = choose_snapshot_for_date(snapshots, pdf_sitting_dates.get(t.pdf_label))
        if snap is None:
            continue
        raw = t.speaker_raw
        key = normalize_name(raw)
        if not key:
            continue
        if merge_map:
            key = merge_map.get(key, key)
        dk = (key, snap.label)
        entry = by_key.setdefault(
            dk, {"speaker_key": key, "speaker_raw": raw, "snapshot": snap, "occurrences": 0}
        )
        entry["occurrences"] += 1
        if len(raw) > len(entry["speaker_raw"]):
            entry["speaker_raw"] = raw

    rows = []
    for (key, snap_label), entry in by_key.items():
        snap: CabinetSnapshot = entry["snapshot"]
        norm_choices = snap.norm_names()
        # Cabinet rosters only list ~50-60 people, so an unconstrained
        # search against them needs a strict cutoff - genuine matches score
        # ~80-100 in practice, coincidental ones top out around 77.
        idx, score = _best_match(key, norm_choices, score_cutoff=80)
        matched_name = jawatan = kementerian = ""
        if idx is not None:
            row = snap.df.iloc[idx]
            matched_name = row[snap.name_col]
            jawatan = row[snap.jawatan_col] if snap.jawatan_col else ""
            kementerian = row[snap.kementerian_col] if snap.kementerian_col else ""
        rows.append(
            {
                "Speaker (As Printed)": entry["speaker_raw"],
                "Matched Name": matched_name or entry["speaker_raw"],
                "Jawatan": jawatan,
                "Kementerian": kementerian,
                "Cabinet Snapshot": snap_label,
                "Match Confidence": confidence_label(score),
                "Occurrences": entry["occurrences"],
                "_speaker_key": key,
                "_snapshot_label": snap_label,
            }
        )
    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values("Occurrences", ascending=False).reset_index(drop=True)
    return df


def assemble_transcript(
    turns: list[Turn],
    constituency_dir: pd.DataFrame,
    role_dir: pd.DataFrame,
    pdf_sitting_dates: dict[str, Optional[date]],
    snapshots: list[CabinetSnapshot],
    merge_map: Optional[dict[str, str]] = None,
) -> pd.DataFrame:
    """Joins the (possibly user-edited) speaker directories back onto every
    individual turn to produce the final exportable transcript. merge_map
    must be the same one used to build constituency_dir/role_dir, so each
    turn resolves to the same canonical speaker key those directories used."""
    const_lookup = {}
    if constituency_dir is not None and not constituency_dir.empty:
        for _, r in constituency_dir.iterrows():
            const_lookup[r.get("_speaker_key", normalize_name(r.get("Speaker (As Printed)", "")))] = r

    role_lookup = {}
    if role_dir is not None and not role_dir.empty:
        for _, r in role_dir.iterrows():
            role_lookup[(r.get("_speaker_key", normalize_name(r.get("Speaker (As Printed)", ""))), r.get("_snapshot_label", r.get("Cabinet Snapshot", "")))] = r

    rows = []
    for t in turns:
        key = normalize_name(t.speaker_raw)
        if merge_map:
            key = merge_map.get(key, key)
        crow = const_lookup.get(key)
        snap = choose_snapshot_for_date(snapshots, pdf_sitting_dates.get(t.pdf_label)) if t.kind != "CHAIR" else None
        rrow = role_lookup.get((key, snap.label)) if snap is not None else None

        if t.kind == "CHAIR":
            matched_name = "Tuan Yang di-Pertua"
            constituency = ""
            jawatan = "Speaker / Pengerusi Mesyuarat"
            kementerian = ""
            cab_snapshot = ""
            const_conf = "N/A"
            role_conf = "N/A"
        else:
            matched_name = (crow["Matched Name"] if crow is not None else t.speaker_raw)
            constituency = (crow["Constituency"] if crow is not None else t.constituency_raw)
            const_conf = crow["Match Confidence"] if crow is not None else "None"
            if rrow is not None and str(rrow.get("Jawatan", "")).strip():
                jawatan = rrow["Jawatan"]
                kementerian = rrow.get("Kementerian", "")
                cab_snapshot = rrow.get("Cabinet Snapshot", "")
                role_conf = rrow.get("Match Confidence", "None")
            else:
                jawatan = t.position_raw
                kementerian = ""
                cab_snapshot = ""
                role_conf = "N/A" if not t.is_minister_style else "None"

        rows.append(
            {
                "PDF Source": t.pdf_label,
                "Sitting Date": pdf_sitting_dates.get(t.pdf_label),
                "Order": t.order,
                "Page": t.page_start if t.page_start == t.page_end else f"{t.page_start}-{t.page_end}",
                "Speaker (As Printed)": t.speaker_raw,
                "Matched Name": matched_name,
                "Constituency": constituency,
                "Constituency Confidence": const_conf,
                "Jawatan": jawatan,
                "Kementerian": kementerian,
                "Cabinet Snapshot": cab_snapshot,
                "Role Confidence": role_conf,
                "Speech Text": t.speech_text,
            }
        )
    return pd.DataFrame(rows)
