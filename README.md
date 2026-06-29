# Hansard Extractor V4

A Streamlit app that turns Dewan Rakyat Hansard PDFs into a structured,
speaker-attributed transcript, matched against an MP roster and Cabinet
roster(s), with manual review before export.

## Setup

```bash
pip install -r requirements.txt
streamlit run app.py
```

A "📌 Nota: Garis Masa Parlimen ke-15" expander at the top of the app is a
static reference list of Parlimen ke-15's sitting terms/meeting dates and
Cabinet formation/reshuffle dates — handy when picking a PDF's sitting date
(Step 1) or a Cabinet snapshot's effective-from date (Step 3). It's
informational only and isn't used by the matching logic.

## Usage

1. **Hansard PDF(s)** — upload up to 5 Hansard PDFs. Each one's sitting date
   is auto-detected from the filename (`DR-DDMMYYYY.pdf`) or the cover page,
   and can be corrected.
2. **MP Roster** — upload an `.xlsx` with MP full names and constituencies
   (e.g. `Ahli_Dewan_Rakyat_Parlimen_15_updated.xlsx`). Used to auto-match
   each speaker to a constituency.
3. **Cabinet Roster(s)** — upload one or more `.xlsx` Cabinet rosters (e.g.
   `Kabinet_Asal_19122022.xlsx`, `Kabinet_Reshuffle_Pertama.xlsx`,
   `Kabinet_Reshuffle_Akhir.xlsx`). If you upload more than one, give each a
   label and an "effective from" date — the app picks whichever snapshot was
   in force on each PDF's sitting date when matching ministers to their
   portfolio.
4. **Process** — click to parse all PDFs and run the auto-matching.
4b. **AI-Assisted Speaker Split & Cleanup (optional)** — see below.
5. **Review & Edit Matches** — two editable tables, one row per unique
   speaker: constituency matches and Cabinet role/ministry matches. Fix any
   wrong auto-matches here (e.g. after a by-election, or a name the fuzzy
   matcher got wrong) — the fix applies everywhere that speaker appears. Rows
   with a low-confidence or missing match get a "Suggest matches with AI"
   button (same DeepSeek key as 4b) that proposes a match using broader
   knowledge of who these people are, not just spelling similarity — still
   shown in the same editable cell for you to accept or correct.
6. **Transcript Preview** — the assembled, per-turn transcript for each PDF,
   also editable for one-off corrections, and where you can delete
   mis-parsed rows.
7. **Export** — download either one combined workbook (a sheet per PDF, an
   "All Combined" sheet, and a Metadata sheet), or a `.zip` containing one
   workbook per PDF (each with its own Metadata sheet).

## How matching works

- Most transcript lines are tagged `Name [Constituency]:`. Ministers are
  tagged the other way round, `Position [Name]:`. The parser tells these
  apart by checking the bracketed text for name-like tokens (honorifics,
  "bin"/"binti").
- Names are normalized (honorifics, "bin"/"binti" and punctuation stripped)
  before fuzzy matching (`rapidfuzz`) against the roster/cabinet files, so
  minor spelling/formatting differences don't prevent a match.
- When a constituency is printed directly in the transcript, it's trusted
  as ground truth for that historical sitting, and only the speaker's name
  is cross-checked against the roster (constrained to that constituency) —
  the roster is not allowed to silently swap in a different person's name
  (useful since a roster may have since been updated after a by-election).

## Optional: AI-assisted speaker split & cleanup (DeepSeek)

The regex parser can only safely split a bracket-less `Name: text` reply
into its own turn when the name part has a tell-tale honorific/`bin`/`binti`
token (see `bracket_is_name()` in `hansard_lib/parser.py`) — anonymous
interjection tags like `Seorang Ahli:` ("an unnamed Member") or
`Beberapa Ahli:` ("several Members") have no such token and are
deliberately left merged into the surrounding speaker's Speech Text, since
a plain regex can't reliably tell those apart from an ordinary sentence
that happens to contain a colon. Leftover page header/footer print
artifacts (e.g. `DR. 12.6.2023 3`) can leak into Speech Text the same way,
when they land on the same line as real text rather than as a whole line
by themselves — `PAGE_STAMP_RE` only strips a whole-line stamp.

Step 4b optionally hands just those leftover, still-ambiguous turns to an
LLM (DeepSeek, via its Chat Completions API), which is asked to do two
things: split a genuine embedded speaker change, and strip out any print
artifact it finds — reporting exactly what it removed so you can check it.
You provide your own DeepSeek API key (get one at platform.deepseek.com) in
a password-masked field — it's kept only for the browser session and never
written to disk. Only the Speech Text of turns flagged by a quick local
pre-scan is sent to DeepSeek's API; every proposed change is shown for you
to accept or reject before anything is changed. A length-based safety check
also rejects any response where the reported removals look implausibly
large relative to the original text, since a real print artifact is a
handful of short boilerplate characters, never a meaningful share of an
actual speech turn.

## Optional: AI-assisted name matching (DeepSeek)

`hansard_lib/matching.py`'s roster/Cabinet matching is pure character
similarity (`rapidfuzz`) — it can't know that "Anwar bin Ibrahim" and
"Anwar Ibrahim" are the same person beyond what the strings themselves
suggest, and it can't recognise nicknames or nontrivial spelling/OCR
differences a human familiar with Malaysian politics would. In Step 6, any
row in the Speakers & Constituencies or Cabinet Roles table with a
low-confidence or missing match gets a "Suggest matches with AI" button
(same DeepSeek key as 4b). It sends just the unmatched printed names plus
the full candidate list (the roster, or the relevant Cabinet snapshot) to
DeepSeek and asks for its best match or "no match" — any answer that isn't
an exact, verbatim entry from the candidate list is discarded rather than
trusted. The suggestion lands directly in the same editable cell you
already review; it's never applied silently.

## Known limitations

- Parsing relies on text patterns specific to Parliament of Malaysia
  Hansard formatting; very old or visually-scanned PDFs without selectable
  text won't parse (no OCR step).
- The auto-detected "Position/Title" prefix for ministers can occasionally
  be truncated if it wraps across a page-internal line break in the source
  PDF — this doesn't affect the Jawatan/Kementerian columns, which come
  from the Cabinet roster match instead.
