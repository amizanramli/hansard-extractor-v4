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

_SYSTEM_PROMPT = """You are analysing one "turn" of speech from a Malaysian Parliament (Dewan Rakyat) Hansard transcript. An earlier parsing step has already attributed this whole block to one named speaker, given to you below as the original speaker.

Malaysian Hansard transcripts sometimes print a SECOND, different speaker's quick interjection or reply inside that same block, on its own line, in the form "Name: text" - or a generic placeholder such as "Seorang Ahli: text" (an unnamed Member), "Beberapa Ahli: text" (several Members), or a Deputy Speaker's tag - with no distinguishing "[bracket]" tag, so it stayed merged into the surrounding speaker's text by the earlier parser.

Find every point in the text where the speaker genuinely changes and split the text into ordered segments. Rules:
- Preserve all text verbatim, in order. Do not summarise, translate, paraphrase, or correct anything, and do not drop any words.
- For a segment that is still the original speaker continuing, set "speaker" to null.
- For a segment spoken by someone else, set "speaker" to that name or placeholder exactly as printed (e.g. "Seorang Ahli", "Dato' Seri Haji Ahmad bin Haji Maslan"), and do not include it in "text".
- Most turns have NO embedded change at all. When that's the case, return exactly one segment with "speaker" set to null holding the entire original text, unchanged.
- Every word of the input must appear in exactly one segment's "text", in the same order. Do not output anything outside the JSON object.

Respond with a JSON object of exactly this form, and nothing else:
{"segments": [{"speaker": null_or_string, "text": "verbatim text"}, ...]}
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
    """Turns whose Speech Text still contains a line shaped like a
    bracket-less speaker tag that PLAIN_NAME_RE/bracket_is_name() in
    parser.py deliberately declined to split out at parse time. These are
    exactly the residual "embedded block" cases a plain regex can't safely
    resolve on its own, and so are worth spending an LLM call on."""
    out = []
    for t in turns:
        for line in t.speech_text.split("\n"):
            if EMBEDDED_TAG_RE.match(line.strip()):
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
) -> list[dict]:
    """Calls DeepSeek about a single turn, returns its proposed list of
    {"speaker": str|None, "text": str} segments. Raises LLMError on any
    network failure, unusable response, or a reconstructed-text length that
    looks unsafe (likely truncation/hallucination) - never on a clean "no
    split needed" answer, which is returned as a single null-speaker
    segment like any other."""
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
        if not isinstance(segments, list) or not segments:
            raise ValueError("empty/invalid segments")
        for seg in segments:
            if "text" not in seg:
                raise ValueError("segment missing 'text'")
    except (KeyError, IndexError, ValueError, TypeError, json.JSONDecodeError) as e:
        raise LLMError(f"Could not parse DeepSeek's response: {e}") from e

    _check_reconstruction(turn.speech_text, segments)
    return segments


def _check_reconstruction(original: str, segments: list[dict]) -> None:
    """Safety net against truncated/hallucinated output: the segments'
    combined text (ignoring whitespace differences) should be roughly the
    same length as the original. Doesn't try to verify exact content -
    that's what the human review step in the UI is for."""
    orig_len = len(re.sub(r"\s+", "", original))
    if orig_len == 0:
        return
    got_len = len(re.sub(r"\s+", "", "".join(seg.get("text") or "" for seg in segments)))
    ratio = got_len / orig_len
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
