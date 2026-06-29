"""
Parses Dewan Rakyat Hansard PDFs into individual speaker "turns".

A "turn" is one continuous block of speech attributed to a single speaker
tag as printed in the transcript, e.g.:

    Dato' Seri Dr. Shahidan bin Kassim [Arau]: Kita kawan.

or, for ministers, the tag is printed the other way round with the name
inside the brackets and the portfolio as the prefix:

    Menteri Komunikasi dan Digital [Tuan Ahmad Fahmi bin Mohamed Fadzil]: ...

The Speaker of the House is tagged simply as "Tuan Yang di-Pertua:" with no
brackets at all.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import pdfplumber

HEADER_RE = re.compile(r"^(.*?)\[([^\]\n]{1,90})\]:\s*(.*)$")
CHAIR_RE = re.compile(r"^(Tuan Yang di-Pertua):\s*(.*)$")

# Tokens that, if present in a "[bracket]" tag, mean the bracket holds a
# *person's name* rather than a constituency (used for ministers, deputy
# ministers, parliamentary officers, etc.)
NAME_HINT_RE = re.compile(
    r"\b(TUAN|PUAN|DATO|DATUK|ENCIK|CIK|DR|PROF|SENATOR|HAJI|HAJJAH|TENGKU|"
    r"RAJA|TAN|TUN|YB|IR|USTAZ|USTAZAH)\b",
    re.I,
)
BIN_RE = re.compile(r"\b(BIN|BINTI|A/L|A/P)\b", re.I)

MALAY_MONTHS = {
    "januari": 1, "februari": 2, "mac": 3, "april": 4, "mei": 5, "jun": 6,
    "julai": 7, "ogos": 8, "september": 9, "oktober": 10, "november": 11,
    "disember": 12,
}

FILENAME_DATE_RE = re.compile(r"(\d{2})(\d{2})(\d{4})")
TEXT_DATE_RE = re.compile(
    r"(\d{1,2})\s+(Januari|Februari|Mac|April|Mei|Jun|Julai|Ogos|September|"
    r"Oktober|November|Disember)\s+(\d{4})",
    re.I,
)


def bracket_is_name(bracket: str) -> bool:
    """Heuristic: does this bracket tag contain a person's name (rather than
    a constituency)?"""
    return bool(NAME_HINT_RE.search(bracket) or BIN_RE.search(bracket))


@dataclass
class Turn:
    pdf_label: str
    order: int
    page_start: int
    page_end: int
    kind: str  # "NAMED" | "CHAIR"
    prefix_raw: str
    bracket_raw: str
    speech_text: str
    is_minister_style: bool = False  # bracket holds a name, not a constituency

    @property
    def speaker_raw(self) -> str:
        if self.kind == "CHAIR":
            return "Tuan Yang di-Pertua"
        return self.bracket_raw if self.is_minister_style else self.prefix_raw

    @property
    def constituency_raw(self) -> str:
        if self.kind == "CHAIR" or self.is_minister_style:
            return ""
        return self.bracket_raw

    @property
    def position_raw(self) -> str:
        if self.kind == "CHAIR":
            return "Speaker / Pengerusi Mesyuarat"
        if self.is_minister_style:
            return self.prefix_raw
        return ""


def extract_pages_text(file_bytes: bytes) -> list[str]:
    pages = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for p in pdf.pages:
            pages.append(p.extract_text() or "")
    return pages


def extract_turns(pages_text: list[str], pdf_label: str) -> list[Turn]:
    """Split a Hansard's page texts into a flat list of speaker turns."""
    lines: list[tuple[int, str]] = []
    for page_no, txt in enumerate(pages_text, start=1):
        for line in txt.split("\n"):
            lines.append((page_no, line))

    turns: list[Turn] = []
    current: Optional[dict] = None
    order = 0

    def finalize(cur: dict) -> Turn:
        speech = "\n".join(cur["speech_lines"]).strip()
        return Turn(
            pdf_label=pdf_label,
            order=cur["order"],
            page_start=cur["page_start"],
            page_end=cur["page_end"],
            kind=cur["kind"],
            prefix_raw=cur["prefix_raw"],
            bracket_raw=cur["bracket_raw"],
            speech_text=speech,
            is_minister_style=cur["is_minister_style"],
        )

    for page_no, raw_line in lines:
        stripped = raw_line.strip()
        hm = HEADER_RE.match(stripped)
        cm = None if hm else CHAIR_RE.match(stripped)

        if hm or cm:
            if current is not None:
                turns.append(finalize(current))
            if hm:
                prefix, bracket, rest = hm.groups()
                prefix = prefix.strip()
                bracket = bracket.strip()
                order += 1
                current = {
                    "order": order,
                    "page_start": page_no,
                    "page_end": page_no,
                    "kind": "NAMED",
                    "prefix_raw": prefix,
                    "bracket_raw": bracket,
                    "is_minister_style": bracket_is_name(bracket),
                    "speech_lines": [rest] if rest else [],
                }
            else:
                _, rest = cm.groups()
                order += 1
                current = {
                    "order": order,
                    "page_start": page_no,
                    "page_end": page_no,
                    "kind": "CHAIR",
                    "prefix_raw": "",
                    "bracket_raw": "Tuan Yang di-Pertua",
                    "is_minister_style": False,
                    "speech_lines": [rest] if rest else [],
                }
        else:
            if current is not None:
                current["speech_lines"].append(raw_line)
                current["page_end"] = page_no
            # else: line belongs to front matter (cover page, table of
            # contents, attendance list) before the debate proper starts;
            # we deliberately drop it.

    if current is not None:
        turns.append(finalize(current))

    return turns


def detect_sitting_date(filename: str, pages_text: list[str]) -> Optional[date]:
    """Best-effort extraction of the sitting date, first from the filename
    (Parliament's own convention is DR-DDMMYYYY.pdf), falling back to the
    Malay-language date printed on the cover page."""
    m = FILENAME_DATE_RE.search(filename)
    if m:
        dd, mm, yyyy = m.groups()
        try:
            return date(int(yyyy), int(mm), int(dd))
        except ValueError:
            pass

    for txt in pages_text[:3]:
        m2 = TEXT_DATE_RE.search(txt or "")
        if m2:
            dd, month_name, yyyy = m2.groups()
            month = MALAY_MONTHS.get(month_name.lower())
            if month:
                try:
                    return date(int(yyyy), month, int(dd))
                except ValueError:
                    pass
    return None
