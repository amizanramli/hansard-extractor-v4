"""
Hansard Extractor V4 — Streamlit UI

Upload Dewan Rakyat Hansard PDFs, automatically attribute every speech to a
speaker + constituency (via an MP roster) and, where applicable, a Cabinet
role + ministry (via one or more Cabinet snapshots) — review/correct the
matches, then export a structured transcript to Excel.
"""
from __future__ import annotations

import os
from datetime import date, datetime

import pandas as pd
import streamlit as st

from hansard_lib import exporter, llm_assist, matching, parser
from hansard_lib.matching import CabinetSnapshot

MAX_PDFS = 10

# Reference notes only — not used anywhere in the matching logic. Helps when
# picking a PDF's sitting date (Step 1) or a Cabinet snapshot's "effective
# from" date (Step 3): which Parlimen ke-15 term/meeting a sitting falls
# under, and which Cabinet configuration was in force at the time.
PARLIAMENT_TIMELINE_MD = """
- **19 Nov 2022** — PRU15
- **24 Nov 2022** — Anwar menjadi PMX
- **2 Dis 2022** — Pembentukan Kabinet Pertama
- **19 Dis 2022** — Pembukaan Parlimen Ke-15 Bersidang
    - Mesyuarat Khas Pertama (19–20 Dis 2022)
- **13 Feb 2023** — Penggal Kedua bermula
    - Mesyuarat Pertama Penggal Kedua (13 Feb – 4 Apr 2023)
    - Mesyuarat Kedua Penggal Kedua (22 Mei – 15 Jun 2023)
    - Mesyuarat Khas (11–19 Sep 2023)
    - Mesyuarat Ketiga Penggal Kedua (9 Okt – 30 Nov 2023)
- **12 Dis 2023** — Rombakan Kabinet 1
- **26 Feb 2024** — Penggal Ketiga bermula
    - Mesyuarat Pertama (26 Februari – 27 Mac 2024)
    - Mesyuarat Kedua (24 Jun – 18 Julai 2024)
    - Mesyuarat Ketiga (14 Oktober – 12 Disember 2024)
- **3 Feb 2025** — Penggal Keempat bermula
    - Mesyuarat Pertama (3 Februari – 6 Mac 2025)
    - Mesyuarat Kedua (21 Julai – 28 Ogos 2025)
    - Mesyuarat Ketiga (6 Oktober – 4 Disember 2025)
- **16 Dis 2025** — Rombakan Kabinet 2
- **19 Jan 2026** — Penggal Kelima bermula
    - Mesyuarat Pertama (19 Januari 2026 – 3 Mac 2026)
    - Mesyuarat Kedua (22 Jun 2026 – 16 Julai 2026)
"""

st.set_page_config(page_title="Hansard Extractor V4", layout="wide")
st.title("Hansard Extractor V4")
st.caption(
    "Parse Dewan Rakyat Hansard PDFs into a structured, speaker-attributed "
    "transcript — matched against an MP roster and Cabinet roster(s)."
)
with st.expander("📌 Nota: Garis Masa Parlimen ke-15 (rujukan tarikh & kabinet)"):
    st.caption(
        "Reference only — useful for picking a sitting date (Step 1) or a "
        "Cabinet snapshot's effective-from date (Step 3)."
    )
    st.markdown(PARLIAMENT_TIMELINE_MD)


# --------------------------------------------------------------------------- #
# Cached parsing
# --------------------------------------------------------------------------- #
@st.cache_data(show_spinner=False)
def _parse_pdf(file_bytes: bytes, filename: str):
    pages_text = parser.extract_pages_text(file_bytes)
    turns = parser.extract_turns(pages_text, filename)
    detected_date = parser.detect_sitting_date(filename, pages_text)
    return pages_text, turns, detected_date


def _read_excel_with_sheet_picker(uploaded_file, key_prefix: str) -> pd.DataFrame:
    xl = pd.ExcelFile(uploaded_file)
    sheet = xl.sheet_names[0]
    if len(xl.sheet_names) > 1:
        sheet = st.selectbox(
            f"Sheet to use ({uploaded_file.name})", xl.sheet_names, key=f"{key_prefix}_sheet"
        )
    return xl.parse(sheet)


# --------------------------------------------------------------------------- #
# Step 1 — PDFs
# --------------------------------------------------------------------------- #
st.header("1. Hansard PDF(s)")
uploaded_pdfs = st.file_uploader(
    f"Upload up to {MAX_PDFS} Hansard PDF files", type=["pdf"], accept_multiple_files=True
)

if uploaded_pdfs and len(uploaded_pdfs) > MAX_PDFS:
    st.error(f"Maximum {MAX_PDFS} PDFs allowed — only the first {MAX_PDFS} will be used.")
    uploaded_pdfs = uploaded_pdfs[:MAX_PDFS]

pdf_entries = []
if uploaded_pdfs:
    cols = st.columns(min(len(uploaded_pdfs), 3) or 1)
    for i, f in enumerate(uploaded_pdfs):
        file_bytes = f.getvalue()
        pages_text, turns, detected_date = _parse_pdf(file_bytes, f.name)
        with cols[i % len(cols)]:
            st.markdown(f"**{f.name}**")
            st.caption(f"{len(pages_text)} pages · {len(turns)} speaker turns detected")
            sitting_date = st.date_input(
                "Sitting date",
                value=detected_date or date.today(),
                key=f"date_{f.name}",
            )
        pdf_entries.append(
            {"label": f.name, "pages_text": pages_text, "turns": turns, "sitting_date": sitting_date}
        )

# --------------------------------------------------------------------------- #
# Step 2 — Roster
# --------------------------------------------------------------------------- #
st.header("2. MP Roster (for constituency matching)")
roster_file = st.file_uploader("Upload MP roster (.xlsx)", type=["xlsx"], key="roster_upload")

roster_df, roster_name_col, roster_const_col = None, None, None
if roster_file:
    roster_df = _read_excel_with_sheet_picker(roster_file, "roster")
    cols = list(roster_df.columns)
    guess_name = matching.guess_column(cols, ["NAMA"]) or cols[0]
    guess_const = matching.guess_column(cols, ["KAWASAN", "PARLIMEN", "CONSTITUENCY"]) or cols[-1]
    c1, c2 = st.columns(2)
    with c1:
        roster_name_col = st.selectbox("Name column", cols, index=cols.index(guess_name))
    with c2:
        roster_const_col = st.selectbox("Constituency column", cols, index=cols.index(guess_const))
    with st.expander("Preview roster"):
        st.dataframe(roster_df.head(10), width="stretch")
else:
    st.info("No roster uploaded — constituency matching will be skipped (can still be filled in manually).")

# --------------------------------------------------------------------------- #
# Step 3 — Cabinet roster(s)
# --------------------------------------------------------------------------- #
st.header("3. Cabinet Roster(s) (for role/ministry matching)")
cabinet_files = st.file_uploader(
    "Upload one or more Cabinet roster files (.xlsx) — e.g. before/after a reshuffle",
    type=["xlsx"],
    accept_multiple_files=True,
    key="cabinet_upload",
)

snapshots: list[CabinetSnapshot] = []
if cabinet_files:
    for f in cabinet_files:
        df = _read_excel_with_sheet_picker(f, f"cab_{f.name}")
        cols = list(df.columns)
        default_label = f.name.rsplit(".", 1)[0]
        with st.expander(f"Configure: {f.name}", expanded=True):
            label = st.text_input("Snapshot label", value=default_label, key=f"label_{f.name}")
            cc1, cc2, cc3 = st.columns(3)
            with cc1:
                name_col = st.selectbox(
                    "Name column", cols,
                    index=cols.index(matching.guess_column(cols, ["NAMA"]) or cols[0]),
                    key=f"namecol_{f.name}",
                )
            with cc2:
                jawatan_col = st.selectbox(
                    "Position column", cols,
                    index=cols.index(matching.guess_column(cols, ["JAWATAN", "POSITION"]) or cols[0]),
                    key=f"jawcol_{f.name}",
                )
            with cc3:
                kementerian_col = st.selectbox(
                    "Ministry column", cols,
                    index=cols.index(matching.guess_column(cols, ["KEMENTERIAN", "MINISTRY"]) or cols[0]),
                    key=f"kemcol_{f.name}",
                )
            has_date = st.checkbox(
                "Set an effective-from date (needed only if uploading multiple snapshots)",
                key=f"hasdate_{f.name}",
                value=len(cabinet_files) > 1,
            )
            eff_date = None
            if has_date:
                eff_date = st.date_input("Effective from", value=date.today(), key=f"effdate_{f.name}")
            st.dataframe(df.head(5), width="stretch")
        snapshots.append(
            CabinetSnapshot(
                label=label, df=df, name_col=name_col, jawatan_col=jawatan_col,
                kementerian_col=kementerian_col, effective_date=eff_date,
            )
        )
else:
    st.info("No cabinet file uploaded — role/ministry matching will be skipped.")

# --------------------------------------------------------------------------- #
# Step 4 — Process
# --------------------------------------------------------------------------- #
st.header("4. Process")

if "processed" not in st.session_state:
    st.session_state.processed = False

if st.button("Process Hansard(s)", type="primary", disabled=not pdf_entries):
    all_turns = []
    for e in pdf_entries:
        all_turns.extend(e["turns"])
    pdf_sitting_dates = {e["label"]: e["sitting_date"] for e in pdf_entries}

    st.session_state.processed = True
    st.session_state.all_turns = all_turns
    st.session_state.pdf_sitting_dates = pdf_sitting_dates
    st.session_state.snapshots = snapshots
    st.session_state.unique_speakers_df = matching.build_unique_speakers_table(all_turns)
    st.session_state.pdf_entries_meta = [
        {"label": e["label"], "sitting_date": e["sitting_date"], "pages": len(e["pages_text"]), "turns": len(e["turns"])}
        for e in pdf_entries
    ]

_required_keys = ["all_turns", "pdf_sitting_dates", "snapshots", "unique_speakers_df", "pdf_entries_meta"]
if not st.session_state.processed or any(k not in st.session_state for k in _required_keys):
    if st.session_state.processed:
        # processed=True survived from a session started under older app code
        # that didn't set every key the current code expects — rather than
        # crashing on a missing key below, ask for a clean re-process.
        st.session_state.processed = False
        st.warning("The app was updated — please click “Process Hansard(s)” again.")
    st.stop()

all_turns = st.session_state.all_turns
pdf_sitting_dates = st.session_state.pdf_sitting_dates
snapshots = st.session_state.snapshots

# --------------------------------------------------------------------------- #
# Step 4b — AI-assisted speaker split (optional, DeepSeek)
# --------------------------------------------------------------------------- #
st.header("4b. AI-Assisted Speaker Split & Cleanup (optional)")
st.caption(
    "A handful of quick interjections are embedded in another speaker's "
    "block with no honorific the regex parser can key off — e.g. anonymous "
    "“Seorang Ahli:” / “Beberapa Ahli:” remarks — and stay merged into the "
    "wrong speaker's Speech Text. Leftover page-stamp/page-number print "
    "artifacts (e.g. “DR. 12.6.2023 3”) can leak in the same way. "
    "Optionally ask an LLM (DeepSeek) to find and propose fixes for both. "
    "Nothing changes until you review and accept a proposal below."
)

st.session_state.setdefault("deepseek_api_key", os.environ.get("DEEPSEEK_API_KEY", ""))
deepseek_api_key = st.text_input(
    "DeepSeek API key",
    type="password",
    key="deepseek_api_key",
    help=(
        "Get a key at platform.deepseek.com. Kept only for this session and "
        "never written to disk. Speech text for flagged turns only is sent "
        "to DeepSeek's API for analysis."
    ),
)

llm_candidates = llm_assist.find_candidate_turns(all_turns)
if not llm_candidates:
    st.caption("Pre-scan found no turns with a leftover speaker-tag or page-stamp-like line.")
else:
    st.caption(
        f"Pre-scan flagged {len(llm_candidates)} turn(s) with a leftover "
        f"bracket-less speaker-tag line or page-stamp/page-number artifact "
        f"for DeepSeek to check."
    )
    if st.button(
        "Scan flagged turns with DeepSeek", disabled=not deepseek_api_key, key="run_llm_scan"
    ):
        progress = st.progress(0.0)
        scan_proposals: dict[tuple[str, int], list[dict]] = {}
        scan_artifacts: dict[tuple[str, int], list[str]] = {}
        scan_errors: dict[tuple[str, int], str] = {}
        for i, t in enumerate(llm_candidates, start=1):
            try:
                segments, removed_artifacts = llm_assist.call_deepseek(t, deepseek_api_key)
                is_real_split = len(segments) > 1 or (
                    len(segments) == 1 and segments[0].get("speaker")
                )
                if is_real_split or removed_artifacts:
                    scan_proposals[llm_assist.turn_key(t)] = segments
                    if removed_artifacts:
                        scan_artifacts[llm_assist.turn_key(t)] = removed_artifacts
            except llm_assist.LLMError as e:
                scan_errors[llm_assist.turn_key(t)] = str(e)
            progress.progress(i / len(llm_candidates))
        progress.empty()
        st.session_state.llm_proposals = scan_proposals
        st.session_state.llm_artifacts = scan_artifacts
        st.session_state.llm_errors = scan_errors

llm_proposals = st.session_state.get("llm_proposals", {})
llm_artifacts = st.session_state.get("llm_artifacts", {})
llm_errors = st.session_state.get("llm_errors", {})

if llm_errors:
    st.warning(f"{len(llm_errors)} turn(s) could not be checked (left unchanged) — see details below.")
    with st.expander("DeepSeek errors"):
        for key, msg in llm_errors.items():
            st.caption(f"{key}: {msg}")

if llm_proposals:
    turns_by_key = {llm_assist.turn_key(t): t for t in all_turns}
    st.write(f"DeepSeek proposed a change for {len(llm_proposals)} turn(s) — review and accept below:")
    accepted_keys = set()
    for key, segments in llm_proposals.items():
        t = turns_by_key.get(key)
        if t is None:
            continue
        with st.expander(f"{t.pdf_label} · turn {t.order} · {t.speaker_raw} (p.{t.page_start})"):
            st.text_area("Original", t.speech_text, height=120, disabled=True, key=f"llm_orig_{key}")
            artifacts = llm_artifacts.get(key)
            if artifacts:
                st.caption("Print artifact(s) DeepSeek proposes removing:")
                for a in artifacts:
                    st.code(a, language=None)
            for i, seg in enumerate(segments, start=1):
                label = seg.get("speaker") or t.speaker_raw
                st.markdown(f"**{i}. {label}**")
                st.text(seg.get("text", ""))
            if st.checkbox("Apply this change", value=True, key=f"llm_apply_{key}"):
                accepted_keys.add(key)
    if st.button("Apply accepted changes", type="primary", key="apply_llm_splits"):
        all_turns = llm_assist.apply_splits(all_turns, llm_proposals, accepted_keys)
        st.session_state.all_turns = all_turns
        st.session_state.unique_speakers_df = matching.build_unique_speakers_table(all_turns)
        stale_prefixes = (
            "llm_orig_", "llm_apply_", "speaker_merge_editor", "const_editor",
            "role_editor", "transcript_editor_",
        )
        for ek in list(st.session_state.keys()):
            if ek in (
                "llm_proposals", "llm_errors", "llm_artifacts",
                "const_name_suggestions", "role_name_suggestions",
            ) or ek.startswith(stale_prefixes):
                st.session_state.pop(ek, None)
        st.success("Changes applied — tables below now reflect the update.")
        st.rerun()

# --------------------------------------------------------------------------- #
# Step 5 — Speaker consolidation (merge duplicate variants)
# --------------------------------------------------------------------------- #
st.header("5. Speaker Consolidation")
st.caption(
    "The same person printed two different ways (honorific inconsistencies, "
    "OCR quirks, a line-wrap truncation, etc.) shows up below as separate "
    "rows. Set “Merge Into” to collapse a variant into its canonical "
    "speaker — likely duplicates are pre-suggested, but always review them."
)

unique_speakers_df = st.session_state.unique_speakers_df
if unique_speakers_df.empty:
    st.caption("No speakers detected yet.")
    merge_map = {}
else:
    merge_options = ["(keep separate)"] + sorted(unique_speakers_df["Speaker (As Printed)"].unique().tolist())
    edited_speakers = st.data_editor(
        unique_speakers_df,
        key="speaker_merge_editor",
        width="stretch",
        num_rows="fixed",
        column_order=["Speaker (As Printed)", "Occurrences", "Merge Into", "Auto-Suggested"],
        disabled=["Speaker (As Printed)", "Occurrences", "Auto-Suggested"],
        column_config={
            "Merge Into": st.column_config.SelectboxColumn(
                "Merge Into", options=merge_options, required=True,
            ),
        },
        height=min(36 * (len(unique_speakers_df) + 1) + 4, 480),
    )
    merge_map = matching.resolve_merge_map(edited_speakers)
    n_before = len(unique_speakers_df)
    n_after = len(set(merge_map.values())) if merge_map else n_before
    if n_after < n_before:
        st.success(f"{n_before} speaker variants → {n_after} unique speakers after consolidation.")
    else:
        st.caption(f"{n_before} unique speakers — no duplicates merged yet.")

# --------------------------------------------------------------------------- #
# Step 6 — Review & edit matches
# --------------------------------------------------------------------------- #
st.header("6. Review & Edit Matches")

constituency_dir = matching.build_constituency_directory(
    all_turns, roster_df, roster_name_col, roster_const_col, merge_map=merge_map
)
role_dir = matching.build_role_directory(all_turns, snapshots, pdf_sitting_dates, merge_map=merge_map)

st.subheader("Speakers & Constituencies")
const_df = constituency_dir
if const_df.empty:
    st.caption("No speakers detected, or no roster uploaded.")
    edited_const = const_df
else:
    low_conf_mask = const_df["Match Confidence"].isin(["Low", "None"])
    n_low = int(low_conf_mask.sum())
    if n_low and roster_df is not None and roster_name_col:
        cc1, cc2 = st.columns([4, 1])
        with cc1:
            st.caption(
                f"{n_low} speaker(s) have a low-confidence or no constituency match. "
                f"An LLM (DeepSeek) can draw on broader knowledge of the roster than "
                f"spelling similarity alone — review any suggestion before relying on it."
            )
        with cc2:
            if st.button(
                "Suggest matches with AI", key="suggest_const_matches", disabled=not deepseek_api_key
            ):
                names_needing = const_df.loc[low_conf_mask, "Speaker (As Printed)"].tolist()
                roster_names_all = [str(n) for n in roster_df[roster_name_col].dropna().tolist()]
                try:
                    st.session_state.const_name_suggestions = llm_assist.suggest_name_matches(
                        names_needing, roster_names_all, deepseek_api_key
                    )
                    st.session_state.pop("const_editor", None)
                    st.rerun()
                except llm_assist.LLMError as e:
                    st.warning(f"Could not get AI suggestions: {e}")
        const_suggestions = st.session_state.get("const_name_suggestions", {})
        if const_suggestions:
            for idx in const_df.index[low_conf_mask]:
                printed = const_df.at[idx, "Speaker (As Printed)"]
                sugg = const_suggestions.get(printed)
                if not sugg:
                    continue
                const_df.at[idx, "Matched Name"] = sugg
                match_rows = roster_df[roster_df[roster_name_col] == sugg]
                if not match_rows.empty and roster_const_col:
                    const_df.at[idx, "Constituency"] = match_rows.iloc[0][roster_const_col]
                const_df.at[idx, "Match Confidence"] = "AI-Suggested"
    edited_const = st.data_editor(
        const_df,
        key="const_editor",
        width="stretch",
        num_rows="fixed",
        column_order=["Speaker (As Printed)", "Matched Name", "Constituency", "Occurrences", "Match Confidence"],
        disabled=["Speaker (As Printed)", "Occurrences", "Match Confidence"],
        height=min(36 * (len(const_df) + 1) + 4, 480),
    )

st.subheader("Cabinet Roles")
role_df = role_dir
if role_df.empty:
    st.caption("No ministers detected, or no cabinet file uploaded.")
    edited_role = role_df
else:
    low_conf_mask = role_df["Match Confidence"].isin(["Low", "None"])
    n_low = int(low_conf_mask.sum())
    if n_low and snapshots:
        cc1, cc2 = st.columns([4, 1])
        with cc1:
            st.caption(
                f"{n_low} speaker(s) have a low-confidence or no Cabinet role match. "
                f"An LLM (DeepSeek) can draw on broader knowledge of the roster than "
                f"spelling similarity alone — review any suggestion before relying on it."
            )
        with cc2:
            if st.button(
                "Suggest matches with AI", key="suggest_role_matches", disabled=not deepseek_api_key
            ):
                snap_by_label = {s.label: s for s in snapshots}
                low_rows = role_df.loc[low_conf_mask]
                try:
                    # Keyed by (printed name, snapshot label) - not just the
                    # printed name - because the same person can appear as a
                    # low-confidence row under two different Cabinet
                    # Snapshots (one row per (speaker, snapshot actually
                    # used), per build_role_directory()); a name-only key
                    # would let one snapshot's suggestion silently clobber
                    # another's for someone who spoke across both.
                    role_suggestions: dict[tuple[str, str], str | None] = {}
                    for snap_label, group in low_rows.groupby("Cabinet Snapshot"):
                        snap = snap_by_label.get(snap_label)
                        if snap is None:
                            continue
                        candidate_names = [str(n) for n in snap.df[snap.name_col].dropna().tolist()]
                        names_needing = group["Speaker (As Printed)"].tolist()
                        for name, sugg in llm_assist.suggest_name_matches(
                            names_needing, candidate_names, deepseek_api_key
                        ).items():
                            role_suggestions[(name, snap_label)] = sugg
                    st.session_state.role_name_suggestions = role_suggestions
                    st.session_state.pop("role_editor", None)
                    st.rerun()
                except llm_assist.LLMError as e:
                    st.warning(f"Could not get AI suggestions: {e}")
        role_suggestions = st.session_state.get("role_name_suggestions", {})
        if role_suggestions:
            snap_by_label = {s.label: s for s in snapshots}
            for idx in role_df.index[low_conf_mask]:
                printed = role_df.at[idx, "Speaker (As Printed)"]
                snap_label = role_df.at[idx, "Cabinet Snapshot"]
                sugg = role_suggestions.get((printed, snap_label))
                if not sugg:
                    continue
                snap = snap_by_label.get(snap_label)
                role_df.at[idx, "Matched Name"] = sugg
                if snap is not None:
                    match_rows = snap.df[snap.df[snap.name_col] == sugg]
                    if not match_rows.empty:
                        role_df.at[idx, "Jawatan"] = match_rows.iloc[0][snap.jawatan_col]
                        role_df.at[idx, "Kementerian"] = match_rows.iloc[0][snap.kementerian_col]
                role_df.at[idx, "Match Confidence"] = "AI-Suggested"
    edited_role = st.data_editor(
        role_df,
        key="role_editor",
        width="stretch",
        num_rows="fixed",
        column_order=["Speaker (As Printed)", "Matched Name", "Jawatan", "Kementerian", "Cabinet Snapshot", "Occurrences", "Match Confidence"],
        disabled=["Speaker (As Printed)", "Cabinet Snapshot", "Occurrences", "Match Confidence"],
        height=min(36 * (len(role_df) + 1) + 4, 480),
    )

transcript_df = matching.assemble_transcript(
    all_turns, edited_const, edited_role, pdf_sitting_dates, snapshots, merge_map=merge_map
)

# --------------------------------------------------------------------------- #
# Step 7 — Final transcript preview (per PDF, editable)
# --------------------------------------------------------------------------- #
st.header("7. Transcript Preview")

pdf_labels = list(transcript_df["PDF Source"].unique())
final_frames = {}
tabs = st.tabs(pdf_labels) if len(pdf_labels) > 1 else [st.container()]
for tab, label in zip(tabs, pdf_labels):
    with tab:
        sub = transcript_df[transcript_df["PDF Source"] == label].reset_index(drop=True)
        edited_sub = st.data_editor(
            sub,
            key=f"transcript_editor_{label}",
            width="stretch",
            num_rows="dynamic",
            column_order=[
                "Order", "Page", "Speaker (As Printed)", "Matched Name", "Constituency",
                "Constituency Confidence", "Jawatan", "Kementerian", "Role Confidence", "Speech Text",
            ],
            height=420,
        )
        final_frames[label] = edited_sub

final_transcript_df = pd.concat(final_frames.values(), ignore_index=True) if final_frames else transcript_df

# --------------------------------------------------------------------------- #
# Step 8 — Export
# --------------------------------------------------------------------------- #
st.header("8. Export")

metadata_rows = []
roster_name = roster_file.name if roster_file else "(none)"
snapshot_labels = ", ".join(s.label for s in snapshots) if snapshots else "(none)"
for m in st.session_state.pdf_entries_meta:
    metadata_rows.append(
        {
            "PDF Source": m["label"],
            "Sitting Date": m["sitting_date"],
            "Pages": m["pages"],
            "Total Speaker Turns": m["turns"],
            "Roster File Used": roster_name,
            "Cabinet Snapshot(s) Available": snapshot_labels,
            "Processed At": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
    )

pdf_labels_all = [m["label"] for m in st.session_state.pdf_entries_meta]
if len(pdf_labels_all) == 1:
    combined_file_name = exporter.matching_xlsx_filename(pdf_labels_all[0])
else:
    combined_file_name = "hansard_combined.xlsx"

c1, c2 = st.columns(2)
with c1:
    combined_bytes = exporter.build_combined_workbook(final_transcript_df, metadata_rows)
    st.download_button(
        "Download combined workbook (.xlsx)",
        data=combined_bytes,
        file_name=combined_file_name,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        width="stretch",
    )
with c2:
    zip_bytes = exporter.build_zip_of_individual_workbooks(final_transcript_df, metadata_rows)
    st.download_button(
        "Download individual workbooks (.zip)",
        data=zip_bytes,
        file_name="hansard_individual.zip",
        mime="application/zip",
        width="stretch",
    )
