"""
Optional LLM-assisted second pass over already-parsed Hansard turns.

hansard_lib.parser's regex-based extract_turns() can only safely split a
bracket-less "Name: text" reply (see PLAIN_NAME_RE there) off into its own
turn when the name part looks like a person's name - i.e. it contains an
honorific/bin/binti token (see bracket_is_name() in parser.py). That's
deliberate: without *some* signal, a regex can't tell a genuine embedded
speaker change ("Seorang Ahli: Setuju.") from an ordinary sentence that
happens to contain a colon. Anonymous interjection tags like
"Seorang Ahli:" / "Beberapa Ahli:" (an unnamed Member / several Members)
carry no such token and so are deliberately left merged into the
surrounding speaker's Speech Text by the regex pass - these are "the
embedded block[s]" that are still there after the regex fix.

This module sends just those still-ambiguous turns to an LLM (DeepSeek, via
its OpenAI-compatible Chat Completions API) and asks it to make the call a
regex can't: is there a genuine embedded speaker change here, and if so,
where does it start and who is speaking? Nothing here is applied
automatically - app.py shows every proposed split for the user to accept or
reject before it touches the actual turns.

Two more LLM-assisted jobs live here for the same reason (a deterministic
pass already does what it safely can; the LLM is an optional, human-reviewed
backstop for what's left):

- Leftover print artifacts (page header/footer stamps such as "DR.
  12.6.2023 3" or a lone page number) that survive parser.py's PAGE_STAMP_RE
  - e.g. because they ended up on the same line as real text rather than as
  a whole line by themselves. call_deepseek() is asked to strip these out of
  a turn's Speech Text alongside its speaker-split job, returning what it
  removed separately so the UI can show it for review.
- suggest_name_matches(): when rapidfuzz's character-similarity scoring in
  matching.py can't confidently match a printed speaker name to the
  roster/cabinet list (truncation, a nickname, an alternate spelling), an
  LLM can draw on actual knowledge of who these people are rather than just
  string distance. Like everything else here, this only ever proposes a
  value into the same editable match table the user already reviews - it's
  never applied silently.
"""
from __future__ import annotations

import json
import re

import requests

from .parser import Turn

DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-v4-flash"
REQUEST_TIMEOUT = 45

# A candidate line: 2-8 "name-shaped" words (each starts with a capital
# letter, or is one of the small set of lowercase name-connector tokens —
# bin/binti/a/l/a/p/@ — mirroring parser.py's own BIN_RE) followed by ": "
# and more text. Deliberately NOT gated on bracket_is_name()'s honorific
# requirement - that's exactly the signal these residual cases lack. It IS
# still gated on every word looking name-shaped (not just the first), which
# is what keeps it from matching an ordinary capitalized sentence start
# followed by a colon (e.g. "Kerajaan akan memastikan: ...") - real
# transcript prose essentially never strings together 2-8 consecutive
# capitalized/connector words right before a colon.
_CONNECTOR = r"(?:bin|binti|bte|ibni|a/l|a/p|@)"
_CAP_WORD = r"[A-ZÀ-Ý][A-Za-zÀ-ÿ.'’/-]*"
_TAG_WORD = rf"(?:{_CAP_WORD}|{_CONNECTOR})"
EMBEDDED_TAG_RE = re.compile(rf"^(?:{_TAG_WORD}(?:\s+{_TAG_WORD}){{1,7}}):\s+(\S.*)$")

# A leftover page header/footer stamp that parser.py's own PAGE_STAMP_RE
# didn't catch - typically because it landed on the same line as real text
# rather than as a whole line by itself (PAGE_STAMP_RE only matches a full
# stripped line), or uses some other layout variant. Deliberately loose:
# this only decides whether a turn is worth an LLM call to check, not
# whether anything actually gets removed - call_deepseek() and the human
# review step in the UI make that call.
LOOSE_PAGE_STAMP_RE = re.compile(r"\bDR\.?\s*\d{1,2}[.\s]\d{1,2}[.\s]\d{2,4}\b", re.IGNORECASE)

# A standalone line that's just a page number (digits or a short roman
# numeral) - another common page-break leftover. Only worth flagging when
# it's one line among several in the turn, not the turn's entire content.
_LONE_PAGE_NUMBER_RE = re.compile(r"^(?:[ivxlcdm]{1,6}|\d{1,4})$", re.IGNORECASE)

_SYSTEM_PROMPT = """You are cleaning up one "turn" of speech from a Malaysian Parliament (Dewan Rakyat) Hansard transcript. An earlier parsing step has already attributed this whole block to one named speaker, given to you below as the original speaker. You have two separate jobs on this text.

JOB 1 - split embedded speaker changes. Malaysian Hansard transcripts sometimes print a SECOND, different speaker's quick interjection or reply inside that same block, on its own line, in the form "Name: text" - or a generic placeholder such as "Seorang Ahli: text" (an unnamed Member), "Beberapa Ahli: text" (several Members), or a Deputy Speaker's tag - with no distinguishing "[bracket]" tag, so it stayed merged into the surrounding speaker's text by the earlier parser. Find every point in the text where the speaker genuinely changes and split the text into ordered segments at that point.

JOB 2 - remove print artifacts. The PDF-to-text conversion sometimes leaves a leftover page header/footer stamp embedded in the text, e.g. "DR. 12.6.2023 3", "56 DR.19.12.2022", or an isolated page number/roman numeral on its own line. These are never real speech - strip them out of the segment text they'd otherwise sit in, and list each one you removed, verbatim, in "removed_artifacts". Be conservative: only remove text that is unambiguously a print artifact (a "DR" + date stamp, or a standalone page number/roman numeral line) - never remove, alter, or correct any real spoken words, even if they look unusual, repetitive, or are numbers spoken aloud as part of the speech.

Rules:
- Preserve all real speech text verbatim, in order. Do not summarise, translate, paraphrase, or correct anything, and do not drop any actual spoken words.
- For a segment that is still the original speaker continuing, set "speaker" to null.
- For a segment spoken by someone else, set "speaker" to that name or placeholder exactly as printed (e.g. "Seorang Ahli", "Dato' Seri Haji Ahmad bin Haji Maslan"), and do not include it in "text".
- Most turns have NO embedded speaker change and NO print artifacts. When that's the case, return exactly one segment with "speaker" set to null holding the entire original text, unchanged, and an empty "removed_artifacts" list.
- Every word of the input must end up in exactly one segment's "text" or in "removed_artifacts", in the same order. Do not output anything outside the JSON object.

Respond with a JSON object of exactly this form, and nothing else:
{"segments": [{"speaker": null_or_string, "text": "verbatim text"}, ...], "removed_artifacts": ["verbatim removed snippet", ...]}
"""


class LLMError(RuntimeError):
    """Raised when DeepSeek can't be reached, or its response can't be
    trusted. Callers should treat this the same as "no split proposed" and
    leave the turn untouched."""


def turn_key(t: Turn) -> tuple[str, int]:
    """A stable identifier for a turn within the current all_turns list.
    t.order alone isn't unique once multiple PDFs are loaded together
    (extract_turns() numbers each PDF's turns from 1) - pair it with
    pdf_label."""
    return (t.pdf_label, t.order)


def find_candidate_turns(turns: list[Turn]) -> list[Turn]:
    """Turns worth spending an LLM call on: either Speech Text still
    contains a line shaped like a bracket-less speaker tag that
    PLAIN_NAME_RE/bracket_is_name() in parser.py deliberately declined to
    split out at parse time, or it contains a line that looks like a
    leftover page-stamp/page-number print artifact parser.py's
    PAGE_STAMP_RE didn't catch. Both are residual cases a plain regex can't
    safely resolve on its own."""
    out = []
    for t in turns:
        lines = [ln.strip() for ln in t.speech_text.split("\n")]
        non_empty = [ln for ln in lines if ln]
        for ln in non_empty:
            if EMBEDDED_TAG_RE.match(ln) or LOOSE_PAGE_STAMP_RE.search(ln):
                out.append(t)
                break
            if _LONE_PAGE_NUMBER_RE.match(ln) and len(non_empty) > 1:
                out.append(t)
                break
    return out


def _extract_json(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    return json.loads(raw)


def call_deepseek(
    turn: Turn,
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: int = REQUEST_TIMEOUT,
) -> tuple[list[dict], list[str]]:
    """Calls DeepSeek about a single turn, returns (segments,
    removed_artifacts): segments is its proposed list of
    {"speaker": str|None, "text": str} pieces; removed_artifacts is the list
    of verbatim print-artifact snippets (page stamps, lone page numbers) it
    stripped out of the text, for the UI to show. Raises LLMError on any
    network failure, unusable response, or a reconstructed-text length that
    looks unsafe (likely truncation/hallucination/over-deletion) - never on
    a clean "nothing to change" answer, which comes back as a single
    null-speaker segment with an empty removed_artifacts list."""
    if not api_key:
        raise LLMError("No DeepSeek API key provided.")

    user_msg = f"Original speaker: {turn.speaker_raw}\n\n---\n{turn.speech_text}\n---"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "thinking": {"type": "disabled"},
    }
    try:
        resp = requests.post(
            DEEPSEEK_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise LLMError(f"Network error calling DeepSeek: {e}") from e

    if resp.status_code != 200:
        raise LLMError(f"DeepSeek returned HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        content = resp.json()["choices"][0]["message"]["content"]
        data = _extract_json(content)
        segments = data["segments"]
        removed_artifacts = data.get("removed_artifacts") or []
        if not isinstance(segments, list) or not segments:
            raise ValueError("empty/invalid segments")
        for seg in segments:
            if "text" not in seg:
                raise ValueError("segment missing 'text'")
        if not isinstance(removed_artifacts, list):
            raise ValueError("removed_artifacts must be a list")
    except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError) as e:
        raise LLMError(f"Could not parse DeepSeek's response: {e}") from e

    _check_reconstruction(turn.speech_text, segments, removed_artifacts)
    return segments, removed_artifacts


def _check_reconstruction(
    original: str, segments: list[dict], removed_artifacts: list[str] | None = None
) -> None:
    """Safety net against truncated/hallucinated/over-deleted output: the
    segments' combined text PLUS whatever was reported removed (ignoring
    whitespace differences) should be roughly the same length as the
    original - and removed_artifacts on its own should only ever account
    for a small fraction of that, since a real print artifact is a handful
    of short boilerplate snippets, never a meaningful share of an actual
    speech turn. Doesn't try to verify exact content - that's what the
    human review step in the UI is for."""
    orig_len = len(re.sub(r"\s+", "", original))
    if orig_len == 0:
        return
    got_len = len(re.sub(r"\s+", "", "".join(seg.get("text") or "" for seg in segments)))
    removed_len = len(re.sub(r"\s+", "", "".join(removed_artifacts or [])))

    max_artifact_len = max(40, int(orig_len * 0.15))
    if removed_len > max_artifact_len:
        raise LLMError(
            f"removed_artifacts looks too large ({removed_len} chars of {orig_len}) — rejected"
        )

    ratio = (got_len + removed_len) / orig_len
    if ratio < 0.7 or ratio > 1.3:
        raise LLMError(
            f"reconstructed text length looks unsafe (ratio {ratio:.2f} vs. original) — rejected"
        )


def apply_splits(
    turns: list[Turn], proposals: dict[tuple[str, int], list[dict]], accepted_keys: set[tuple[str, int]]
) -> list[Turn]:
    """Splices accepted proposals into the turn list in place of their
    original turn, then renumbers `order` sequentially within each PDF.
    Sub-turns inherit the parent turn's pdf_label/page_start/page_end -
    matches how a single turn already spanning multiple printed pages is
    handled elsewhere; the PDF itself doesn't record a finer-grained page
    boundary than that."""
    spliced: list[Turn] = []
    for t in turns:
        segments = proposals.get(turn_key(t)) if turn_key(t) in accepted_keys else None
        if not segments:
            spliced.append(t)
            continue
        for seg in segments:
            speaker = (seg.get("speaker") or "").strip()
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            if not speaker:
                spliced.append(
                    Turn(
                        pdf_label=t.pdf_label,
                        order=0,
                        page_start=t.page_start,
                        page_end=t.page_end,
                        kind=t.kind,
                        prefix_raw=t.prefix_raw,
                        bracket_raw=t.bracket_raw,
                        speech_text=text,
                        is_minister_style=t.is_minister_style,
                    )
                )
            else:
                spliced.append(
                    Turn(
                        pdf_label=t.pdf_label,
                        order=0,
                        page_start=t.page_start,
                        page_end=t.page_end,
                        kind="NAMED",
                        prefix_raw="",
                        bracket_raw=speaker,
                        speech_text=text,
                        is_minister_style=True,
                    )
                )

    counters: dict[str, int] = {}
    for t in spliced:
        counters[t.pdf_label] = counters.get(t.pdf_label, 0) + 1
        t.order = counters[t.pdf_label]
    return spliced


def suggest_name_matches(
    names_needing_match: list[str],
    candidate_names: list[str],
    api_key: str,
    *,
    extra_context: str = "",
    model: str = DEFAULT_MODEL,
    timeout: int = REQUEST_TIMEOUT,
) -> dict[str, str | None]:
    """Asks DeepSeek to match each of `names_needing_match` (speaker names as
    printed in the transcript - possibly OCR-noisy, truncated, a nickname, or
    using a different spelling/honorific) to the single best entry in
    `candidate_names` (the official MP roster or a Cabinet snapshot's name
    list), drawing on real-world knowledge of who these people are rather
    than pure character similarity - exactly the judgement call rapidfuzz's
    string-distance scoring in matching.py can't make on its own.

    Returns a dict from each input name to the matched candidate name
    (copied verbatim from candidate_names) or None if DeepSeek found no
    confident match. A hallucinated name that doesn't exactly match any
    entry in candidate_names is treated as None, never passed through - the
    match must be a real roster entry. Like call_deepseek(), this never
    changes anything by itself; callers should drop the suggestion into the
    same editable match table the user already reviews."""
    if not api_key:
        raise LLMError("No DeepSeek API key provided.")
    if not names_needing_match or not candidate_names:
        return {name: None for name in names_needing_match}

    system_prompt = (
        "You are matching speaker names extracted from a Malaysian Parliament "
        "(Dewan Rakyat) Hansard transcript to canonical names from an official "
        "roster. The extracted names may have OCR noise, truncation, "
        "nicknames, alternate spellings, or honorific differences from the "
        "roster's spelling. For each name in \"names\", pick the single entry "
        "from \"candidates\" that is the same real person, using your "
        "knowledge of Malaysian politicians as well as spelling similarity - "
        "not just whichever string looks closest. If you are not reasonably "
        "confident any candidate is the same person, use null - it is much "
        "better to say null than to guess wrong. Every match must be copied "
        "verbatim, character-for-character, from \"candidates\" - never "
        "invent or alter a name. Respond with a JSON object of exactly this "
        "form, and nothing else: "
        '{"matches": {"<name from names>": "<verbatim candidate or null>", ...}}'
    )
    user_payload: dict = {"candidates": candidate_names, "names": names_needing_match}
    if extra_context:
        user_payload["context"] = extra_context

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
        "thinking": {"type": "disabled"},
    }
    try:
        resp = requests.post(
            DEEPSEEK_API_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
        )
    except requests.RequestException as e:
        raise LLMError(f"Network error calling DeepSeek: {e}") from e

    if resp.status_code != 200:
        raise LLMError(f"DeepSeek returned HTTP {resp.status_code}: {resp.text[:300]}")

    try:
        content = resp.json()["choices"][0]["message"]["content"]
        data = _extract_json(content)
        matches = data["matches"]
        if not isinstance(matches, dict):
            raise ValueError("'matches' must be an object")
    except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError) as e:
        raise LLMError(f"Could not parse DeepSeek's response: {e}") from e

    candidate_set = set(candidate_names)
    out: dict[str, str | None] = {}
    for name in names_needing_match:
        val = matches.get(name)
        out[name] = val if (val and val in candidate_set) else None
    return out
