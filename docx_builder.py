"""Build the student worksheet and the instructor answer key."""

from __future__ import annotations

import hashlib
import random
import re
import string
from collections import Counter
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Emu, Inches, Pt, RGBColor

import config

import omml

BUILD_ID = "2026-08-16.2"   # must match config.BUILD_ID


# ==========================================================================
# Reading HW and video numbers out of the source id
# ==========================================================================
# Handles every naming variant in the wild, including the "PRATICE"
# misspelling that appears in some exported filenames.
_SOURCE = re.compile(
    r"(?P<hw>\d+HW\d+).*?(?:PRACTICE|PRATICE|PRAC|P)[\s_-]*(?P<video>\d+)", re.I)


def _track(video_id: str) -> str:
    """'BASIC' if the filename carries a track marker, else ''.

    Matched on word boundaries so a homework called 5HW24 is never mistaken for
    a track, and only whole words count.
    """
    upper = re.sub(r"[^A-Z0-9]+", " ", str(video_id or "").upper())
    words = set(upper.split())
    for name in config.TRACKS:
        if name in words:
            return name
    return ""


def parse_source(video_id: str) -> tuple[str, int | None]:
    """'Copy-of-1HW25-ZOOM-2025-PRACTICE-3' -> ('1HW25', 3).

    A track marker becomes part of the label, so '5HW24 BASIC' and '5HW24' are
    different homeworks. Without this they merge into one document section with
    two different 'Video 1' entries in the same answer key.
    """
    track = _track(video_id)
    suffix = f" {track}" if track else ""

    m = _SOURCE.search(str(video_id or ""))
    if m:
        return m.group("hw").upper() + suffix, int(m.group("video"))
    m = re.search(r"(\d+HW\d+)", str(video_id or ""), re.I)
    base = m.group(1).upper() if m else (video_id or "Worksheet")
    return base + suffix, None


def hw_series(hw: str) -> str:
    """'1HW25' -> '1';  '5HW24 BASIC' -> '5 BASIC'.

    Groups homeworks into families that share a combined document. The track
    is part of the family, so basic and regular never land in the same file.
    """
    label = str(hw or "")
    track = ""
    for name in config.TRACKS:
        if re.search(rf"\b{re.escape(name)}\b", label, re.I):
            track = f" {name}"
            break
    m = re.match(r"\s*(\d+)HW", label, re.I)
    return (m.group(1) + track) if m else (label or "other")


def compress_ranges(nums: list[int]) -> str:
    """[1,2] -> '1-2';  [3] -> '3';  [1,2,5] -> '1-2, 5'."""
    if not nums:
        return ""
    nums = sorted(nums)
    spans, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        spans.append((start, prev))
        start = prev = n
    spans.append((start, prev))
    return ", ".join(str(a) if a == b else f"{a}-{b}" for a, b in spans)


# ==========================================================================
# Ordering
# ==========================================================================
def natural_key(text: str):
    """Sort key that reads embedded numbers as numbers.

    Plain string sorting puts "PRACTICE 10" before "PRACTICE 2" and "1HW9"
    after "1HW28", which scrambles a worksheet built from lesson filenames.
    Splitting into digit and non-digit runs and comparing the digits
    numerically gives HW25 < HW27 < HW28, practice 2 < 3 < 10, and part q2
    before q10 -- all from the id, with no naming convention required.
    """
    parts = re.split(r"(\d+)", str(text or "").lower())
    # Uniform tuple shape so int and str never get compared to each other.
    return [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in parts if p != ""]


def sort_items(items: list[dict], order: str = "natural") -> list[dict]:
    """Order questions for the printed worksheet."""
    if order == "difficulty":
        rank = {"easy": 0, "medium": 1, "hard": 2}
        return sorted(items, key=lambda m: (rank.get(m.get("difficulty"), 1),
                                            m.get("topic") or "",
                                            natural_key(m.get("question_id") or "")))
    if order == "topic":
        return sorted(items, key=lambda m: ((m.get("topic") or "").lower(),
                                            natural_key(m.get("question_id") or "")))
    # natural: source order, which for lesson filenames means HW then video
    return sorted(items, key=lambda m: natural_key(
        m.get("question_id") or m.get("video_id") or ""))


# ==========================================================================
# Answer position
# ==========================================================================
def assign_labels(mcq: dict, salt: str | None = None) -> list[dict]:
    """Shuffle the four options and letter them A-D.

    Deliberately not the model's job. Language models have a marked positional
    bias when asked to place a correct answer among choices, and a worksheet
    where the answer is disproportionately B teaches students to guess B.

    Seeded, not truly random, so rebuilding a worksheet reproduces the same
    lettering. Change SHUFFLE_SALT to generate a second form of the same test.

    Seeded on the QUESTION id, not the frame. When one board carried one
    question those were the same thing; since the multi-part change they are
    not, and seeding on the frame gave every question from a seven-part board
    the identical answer letter — seven problems in a row all answered D.
    """
    salt = salt if salt is not None else config.SHUFFLE_SALT
    ident = mcq.get("question_id") or mcq.get("video_id") or ""
    seed = int(hashlib.sha256(f"{salt}:{ident}".encode()).hexdigest()[:16], 16)

    options = [dict(mcq["correct_answer"], is_correct=True)]
    options += [dict(d, is_correct=False) for d in mcq.get("distractors", [])]

    random.Random(seed).shuffle(options)

    # Label every option, however many there are. The schema cannot enforce
    # "exactly three distractors" (structured outputs has no maxItems), so a
    # model occasionally returns four. zip() against a 4-letter tuple silently
    # left the extras unlabelled and the document builder then crashed on a
    # missing key. QA blocks these items anyway; this stops a bad item from
    # taking the whole build down with it.
    letters = list(config.CHOICE_LABELS)
    letters += [c for c in string.ascii_uppercase if c not in letters]
    for label, opt in zip(letters, options):
        opt["label"] = label
    return options


# ==========================================================================
# Formatting helpers
# ==========================================================================
def _fix_default_settings(doc: Document) -> None:
    """python-docx ships a `w:zoom w:val="bestFit"` with no `w:percent`.

    Word tolerates it, but it fails OOXML schema validation, which matters if
    these files ever go through an LMS importer or a validating converter.
    """
    from docx.oxml.ns import qn
    zoom = doc.settings.element.find(qn("w:zoom"))
    if zoom is not None and zoom.get(qn("w:percent")) is None:
        zoom.set(qn("w:percent"), "100")


# Matched to the reference document (POT_1.docx): US Letter, 1in margins,
# Roboto 12pt throughout, questions hanging-indented, choices in parentheses.
BODY_FONT = config.BODY_FONT
BODY_SIZE = Pt(config.BODY_SIZE_PT)
Q_INDENT = Inches(0.5)          # where question text sits
Q_HANGING = Inches(0.24)        # the number hangs back this far
CHOICE_INDENT = Inches(0.5)


def _setup_page(doc: Document) -> None:
    """Page size, margins and base font. No title."""
    _fix_default_settings(doc)
    for section in doc.sections:
        section.page_width = Inches(8.5)       # US Letter; python-docx defaults to A4
        section.page_height = Inches(11)
        section.left_margin = section.right_margin = Inches(1.0)
        section.top_margin = section.bottom_margin = Inches(1.0)

    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = BODY_SIZE
    # eastAsia must be set explicitly or Word substitutes for CJK ranges.
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)


def _setup(doc: Document, title: str, subtitle: str = "") -> None:
    """Page setup plus a title block, for the classic 'sets' layout."""
    _setup_page(doc)
    h = doc.add_heading(omml.xml_safe(title), level=0)
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if subtitle:
        p = doc.add_paragraph(omml.xml_safe(subtitle))
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.runs[0].font.size = Pt(10)
        p.runs[0].font.color.rgb = RGBColor(0x60, 0x60, 0x60)


# ==========================================================================
# Real Word list numbering
# ==========================================================================
# Literal "1." text means inserting a question by hand renumbers nothing --
# every problem after it has to be retyped. A genuine numbered list renumbers
# itself, which is what anyone editing a worksheet actually wants.
_ABSTRACT_XML = """<w:abstractNum {ns} w:abstractNumId="{aid}">
  <w:multiLevelType w:val="singleLevel"/>
  <w:lvl w:ilvl="0">
    <w:start w:val="1"/>
    <w:numFmt w:val="decimal"/>
    <w:lvlText w:val="%1."/>
    <w:lvlJc w:val="left"/>
    <w:pPr><w:ind w:left="{left}" w:hanging="{hang}"/></w:pPr>
  </w:lvl>
</w:abstractNum>"""

_NUM_XML = """<w:num {ns} w:numId="{nid}">
  <w:abstractNumId w:val="{aid}"/>
  <w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride>
</w:num>"""

_W_NS_DECL = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _problem_numbering(doc: Document) -> int:
    """Return a fresh numId whose numbering restarts at 1.

    Each homework needs its own sequence, otherwise the second homework in a
    combined document continues from where the first left off. Sharing one
    abstract definition keeps the formatting identical across them.
    """
    from docx.oxml import parse_xml

    numbering = doc.part.numbering_part.element
    existing_abstract = numbering.findall(qn("w:abstractNum"))
    existing_num = numbering.findall(qn("w:num"))

    tag = "_mw_problem_abstract_id"
    aid = getattr(doc, tag, None)
    if aid is None:
        aid = max((int(a.get(qn("w:abstractNumId"))) for a in existing_abstract),
                  default=-1) + 1
        numbering.insert(0, parse_xml(_ABSTRACT_XML.format(
            ns=_W_NS_DECL, aid=aid,
            left=int(Q_INDENT.twips), hang=int(Q_HANGING.twips))))
        setattr(doc, tag, aid)

    nid = max((int(n.get(qn("w:numId"))) for n in existing_num), default=0) + 1
    numbering.append(parse_xml(_NUM_XML.format(ns=_W_NS_DECL, nid=nid, aid=aid)))
    return nid


def _numbered(paragraph, num_id: int):
    """Attach a paragraph to a numbering sequence, so Word supplies the number."""
    from docx.oxml import OxmlElement

    pPr = paragraph._p.get_or_add_pPr()
    numPr = OxmlElement("w:numPr")
    ilvl = OxmlElement("w:ilvl"); ilvl.set(qn("w:val"), "0")
    num = OxmlElement("w:numId"); num.set(qn("w:val"), str(num_id))
    numPr.append(ilvl); numPr.append(num)

    # CT_PPr is a sequence: numPr must precede ind, spacing, jc and the rest.
    # Appending it blindly produced a schema-invalid document that Word would
    # still open but a validating importer would reject.
    for after in ("w:pStyle", "w:keepNext", "w:keepLines", "w:pageBreakBefore",
                  "w:framePr", "w:widowControl"):
        node = pPr.find(qn(after))
        if node is not None:
            node.addnext(numPr)
            break
    else:
        pPr.insert(0, numPr)
    return paragraph


# ==========================================================================
# Choice layout: one row when they fit, otherwise one per row
# ==========================================================================
_CHOICE_COLUMNS = 4


def estimate_width(latex: str) -> float:
    """Approximate rendered width of an option, in character units.

    Word gives no way to measure text before laying it out, so this is a
    deliberate approximation. It exists to answer one question -- will four of
    these fit across a line -- and it errs toward one-per-line, because a
    choice that overflows its column is far worse than one that had room to
    spare.

    A stacked fraction is as wide as its wider half, not the sum; a sub- or
    superscript renders small; a symbol command like \\pi is one glyph.
    """
    t = str(latex or "")

    def _frac(m):
        top = re.sub(r"[\\{}]", "", m.group(1))
        bottom = re.sub(r"[\\{}]", "", m.group(2))
        return "#" * max(len(top), len(bottom))

    t = re.sub(r"\\[dt]?frac\{([^{}]*)\}\{([^{}]*)\}", _frac, t)
    t = re.sub(r"\\sqrt\{([^{}]*)\}", r"v\1", t)
    t = re.sub(r"[_^]\{([^{}]*)\}", lambda m: "." * len(m.group(1)), t)
    t = re.sub(r"[_^](.)", ".", t)
    t = re.sub(r"\\(left|right|,|;|!|quad|qquad|displaystyle)\b", "", t)
    t = re.sub(r"\\[a-zA-Z]+", "X", t)
    t = re.sub(r"[{}\\]", "", t)
    return len(t) + 0.5 * t.count(".")


_PREFIX_UNITS = 4.0     # "(A) "
_GAP_UNITS = 3.0        # breathing room between columns


def line_capacity_units(indent) -> float:
    """How many character units fit across one line at the current body size."""
    usable_in = 8.5 - 2.0 - (int(indent) / 914400)      # page less margins less indent
    char_in = config.BODY_SIZE_PT * 0.5 / 72            # approximate advance width
    return usable_in / char_in


def choice_columns(options: list[dict]) -> list[float]:
    """Minimum width in character units each option needs, prefix and gap included."""
    return [_PREFIX_UNITS + estimate_width(o.get("latex", "")) + _GAP_UNITS
            for o in options]


def choice_starts(options: list[dict], capacity: float) -> list[float]:
    """Where each choice begins, in character units from the left indent.

    Positions rather than widths, because what matters is where the tab stops
    land. Allocating equal column widths put the last choice's *start* at three
    quarters of the line and left the right quarter empty; spreading the starts
    across the full width instead uses the whole line and keeps the gaps even.

    The last option's own width is held back so it ends at the margin rather
    than running past it. Every gap is at least as wide as the option before
    it, so nothing overlaps whatever the content.
    """
    needed = choice_columns(options)
    last_own = _PREFIX_UNITS + estimate_width(options[-1].get("latex", ""))
    span = max(0.0, capacity * config.INLINE_CHOICE_SPREAD - last_own)

    starts = [0.0]
    for i in range(1, len(options)):
        starts.append(starts[-1] + needed[i - 1])

    # Push the remainder outward evenly, so the row fills the line.
    gaps = len(options) - 1
    leftover = span - starts[-1]
    if leftover > 0 and gaps:
        share = leftover / gaps
        starts = [s + share * i for i, s in enumerate(starts)]
    return starts


def choices_fit_one_row(options: list[dict], indent=None) -> bool:
    """True when all the options together fit across a single line.

    Judged on the total, not the widest one. A long option beside three short
    ones fits fine, and requiring every option to be short rejected obviously
    workable rows like "does not exist / -1/2 / 1 / 0".
    """
    if config.INLINE_CHOICE_FIT <= 0 or len(options) != _CHOICE_COLUMNS:
        return False
    if any(estimate_width(o.get("latex", "")) > config.INLINE_CHOICE_MAX
           for o in options):
        return False
    indent = CHOICE_INDENT if indent is None else indent
    return sum(choice_columns(options)) <= (
        line_capacity_units(indent) * config.INLINE_CHOICE_FIT)


def _write_choices(doc: Document, options: list[dict], indent) -> int:
    """Lay out the choices, one row if they fit, otherwise one per row."""
    failures = 0

    if choices_fit_one_row(options, indent):
        # Columns sized to their contents rather than split evenly, so a wide
        # option is not squeezed while three narrow ones waste a quarter line
        # each. Length arithmetic decays to float, so work in EMU.
        char_emu = int(Pt(config.BODY_SIZE_PT * 0.5))
        capacity = line_capacity_units(indent) * config.INLINE_CHOICE_FIT
        starts = choice_starts(options, capacity)

        row = doc.add_paragraph()
        row.paragraph_format.left_indent = indent
        row.paragraph_format.space_after = Pt(2)

        for start in starts[1:]:
            row.paragraph_format.tab_stops.add_tab_stop(
                Emu(int(indent) + int(start * char_emu)))
        for i, opt in enumerate(options):
            if i:
                row.add_run("\t")
            row.add_run(omml.xml_safe(f"({opt['label']}) "))
            if not omml.append_math(row, opt["latex"]):
                failures += 1
        _body(row)
        return failures

    for opt in options:
        c = doc.add_paragraph()
        c.paragraph_format.left_indent = indent
        c.paragraph_format.space_after = Pt(2)
        c.add_run(omml.xml_safe(f"({opt['label']})\t"))
        if not omml.append_math(c, opt["latex"]):
            failures += 1
        _body(c)
    return failures


def _body(paragraph, size: Pt = None, bold: bool = False, italic: bool = False):
    """Apply the reference document's body font to every run in a paragraph."""
    for run in paragraph.runs:
        run.font.name = BODY_FONT
        run.font.size = size or BODY_SIZE
        if bold:
            run.bold = True
        if italic:
            run.italic = True
    return paragraph


def _problem_paragraph(doc: Document, number: int, stem: str) -> int:
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(14)
    p.paragraph_format.space_after = Pt(6)
    run = p.add_run(omml.xml_safe(f"{number}.  "))
    run.bold = True
    return omml.append_rich_text(p, stem)


def _choice_paragraph(doc: Document, label: str, latex: str, mark: bool = False) -> int:
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Inches(0.4)
    p.paragraph_format.space_after = Pt(2)
    run = p.add_run(omml.xml_safe(f"{label}.  "))
    run.bold = True
    if mark:
        run.font.color.rgb = RGBColor(0x1B, 0x7F, 0x3B)
    failed = 0 if omml.append_math(p, latex) else 1
    if mark:
        p.add_run(omml.xml_safe("   \u2190 correct")).font.color.rgb = RGBColor(0x1B, 0x7F, 0x3B)
    return failed


# ==========================================================================
# Student worksheet
# ==========================================================================
def build_worksheet(items: list[dict], out_path: Path, title: str | None = None,
                    subtitle: str = "") -> dict:
    doc = Document()
    _setup(doc, title or config.COURSE_TITLE, subtitle)

    intro = doc.add_paragraph("Choose the best answer for each question. Show your work.")
    intro.runs[0].italic = True

    render_failures = 0
    for n, mcq in enumerate(items, 1):
        mark = len(doc.paragraphs)
        try:
            render_failures += _problem_paragraph(doc, n, mcq["question_stem"])
            for opt in assign_labels(mcq):
                render_failures += _choice_paragraph(doc, opt["label"], opt["latex"])
        except Exception as exc:
            for para in list(doc.paragraphs[mark:]):
                para._element.getparent().remove(para._element)
            print(f"  ! skipped {mcq.get('question_id','?')}: {type(exc).__name__}")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    return {"path": str(out_path), "problems": len(items), "render_failures": render_failures}


# ==========================================================================
# Instructor answer key
# ==========================================================================
def build_answer_key(items: list[dict], out_path: Path, title: str | None = None,
                     subtitle: str = "", reasons: dict | None = None,
                     copy_friendly: bool = False) -> dict:
    """Answer key, optionally laid out for copying questions elsewhere.

    `copy_friendly` keeps the stem and its four choices contiguous and free of
    annotation -- no "correct" marker inline, no explanation between choices --
    so the block can be selected and pasted straight into a worksheet. Which
    option is right, the source, the misconceptions and any QA concern all move
    to a note underneath. Formatting matches the worksheet exactly, so a paste
    needs no reformatting.
    """
    doc = Document()
    _setup(doc, (title or config.COURSE_TITLE)
           + ("" if copy_friendly else " \u2014 Answer Key"), subtitle)

    # Quick-reference line, the thing you actually grade from.
    key_line = ", ".join(
        f"{n}-{next(o['label'] for o in assign_labels(m) if o['is_correct'])}"
        for n, m in enumerate(items, 1)
    )
    p = doc.add_paragraph()
    p.add_run(omml.xml_safe("Key:  ")).bold = True
    p.add_run(omml.xml_safe(key_line))

    misconception_counts: Counter = Counter()

    # Same real list numbering as the worksheets, so a question pasted into a
    # worksheet joins that document's list instead of arriving with a hardcoded
    # number, and inserting one here renumbers the rest.
    num_id = _problem_numbering(doc) if (copy_friendly and config.AUTO_NUMBER) else None

    for n, mcq in enumerate(items, 1):
      mark = len(doc.paragraphs)
      try:
        options = assign_labels(mcq)

        _hw, _video = parse_source(mcq.get("video_id", ""))
        correct = next(o["label"] for o in options if o["is_correct"])

        if copy_friendly:
            # --- the copyable block: stem, then choices, nothing else --------
            head = doc.add_paragraph()
            head.paragraph_format.space_before = Pt(16)
            head.paragraph_format.space_after = Pt(6)
            if num_id is not None:
                _numbered(head, num_id)
            else:
                head.paragraph_format.left_indent = Q_INDENT
                head.paragraph_format.first_line_indent = -Q_HANGING
                head.add_run(omml.xml_safe(f"{n}.\t"))
            omml.append_rich_text(head, mcq["question_stem"])
            _body(head)

            _write_choices(doc, options, CHOICE_INDENT)
            for opt in options:
                if not opt["is_correct"]:
                    misconception_counts[opt.get("misconception", "")] += 1

            # --- everything else, below ------------------------------------
            note = doc.add_paragraph()
            note.paragraph_format.left_indent = CHOICE_INDENT
            note.paragraph_format.space_before = Pt(6)
            note.paragraph_format.space_after = Pt(1)
            a = note.add_run(omml.xml_safe(f"Answer: {correct}"))
            a.bold = True
            a.font.size = Pt(9)
            a.font.color.rgb = RGBColor(0x1B, 0x7F, 0x3B)
            src = note.add_run(omml.xml_safe(
                "   " + _hw + (f" \u00b7 video {_video}" if _video is not None else "")
                + f" \u00b7 {mcq.get('topic','')} \u00b7 {mcq.get('difficulty','')}"
                + f" \u00b7 confidence {mcq.get('confidence','?')}"))
            src.font.size = Pt(9)
            src.font.color.rgb = RGBColor(0x70, 0x70, 0x70)

            for opt in options:
                if opt["is_correct"]:
                    detail = (opt.get("verification") or "").strip()
                    if not detail:
                        continue          # decide before creating the paragraph
                    text = f"({opt['label']}) {detail}"
                else:
                    text = (f"({opt['label']}) [{opt.get('misconception','')}] "
                            f"{opt.get('student_reasoning','')}")
                line = doc.add_paragraph()
                line.paragraph_format.left_indent = CHOICE_INDENT + Inches(0.25)
                line.paragraph_format.space_after = Pt(1)
                run = line.add_run(omml.xml_safe(text))
                run.font.size = Pt(9)
                run.italic = True
                run.font.color.rgb = RGBColor(0x70, 0x70, 0x70)
        else:
            head = doc.add_paragraph()
            head.paragraph_format.space_before = Pt(14)
            head.add_run(omml.xml_safe(f"{n}.  ")).bold = True
            omml.append_rich_text(head, mcq["question_stem"])

            meta = doc.add_paragraph()
            meta.paragraph_format.left_indent = Inches(0.4)
            m = meta.add_run(omml.xml_safe(
                _hw + (f" \u00b7 video {_video}" if _video is not None else "")
                + f" \u00b7 {mcq.get('topic','')} \u00b7 {mcq.get('difficulty','')}"
                + f" \u00b7 confidence {mcq.get('confidence','?')}"
            ))
            m.font.size = Pt(9)
            m.font.color.rgb = RGBColor(0x70, 0x70, 0x70)

            for opt in options:
                _choice_paragraph(doc, opt["label"], opt["latex"], mark=opt["is_correct"])

                note = doc.add_paragraph()
                note.paragraph_format.left_indent = Inches(0.8)
                note.paragraph_format.space_after = Pt(4)
                if opt["is_correct"]:
                    text = opt.get("verification", "")
                else:
                    mis = opt.get("misconception", "")
                    misconception_counts[mis] += 1
                    text = f"[{mis}] {opt.get('student_reasoning','')}"
                run = note.add_run(omml.xml_safe(text))
                run.font.size = Pt(9)
                run.italic = True

        n_opts = 1 + len(mcq.get("distractors") or [])
        if n_opts != 4:
            warn = doc.add_paragraph()
            warn.paragraph_format.left_indent = (CHOICE_INDENT if copy_friendly
                                                 else Inches(0.4))
            wr = warn.add_run(omml.xml_safe(
                f"{n_opts} answer choices, not 4 — regenerate this question "
                f"rather than releasing it"))
            wr.font.size = Pt(9)
            wr.bold = True
            wr.font.color.rgb = RGBColor(0xB4, 0x23, 0x18)

        for reason in (reasons or {}).get(mcq.get("question_id"), []):
            held = doc.add_paragraph()
            held.paragraph_format.left_indent = (CHOICE_INDENT if copy_friendly
                                                 else Inches(0.4))
            hr = held.add_run(omml.xml_safe("CHECK: " + reason))
            hr.font.size = Pt(9)
            hr.bold = True
            hr.font.color.rgb = RGBColor(0xB4, 0x23, 0x18)

        if mcq.get("review_flags"):
            flag = doc.add_paragraph()
            flag.paragraph_format.left_indent = Inches(0.4)
            fr = flag.add_run(omml.xml_safe("Review: " + "; ".join(mcq["review_flags"])))
            fr.font.size = Pt(9)
            fr.font.color.rgb = RGBColor(0xB0, 0x40, 0x00)
      except Exception as exc:
        for para in list(doc.paragraphs[mark:]):
            para._element.getparent().remove(para._element)
        print(f"  ! skipped {mcq.get('question_id','?')} in the key: {type(exc).__name__}")

    # Misconception frequency, so the set can be tuned across a term.
    if misconception_counts:
        doc.add_page_break()
        doc.add_heading("Misconceptions covered in this set", level=1)
        table = doc.add_table(rows=1, cols=2)
        table.style = "Light Grid Accent 1"
        hdr = table.rows[0].cells
        hdr[0].text = "Misconception"
        hdr[1].text = "Distractors"
        for name, count in misconception_counts.most_common():
            row = table.add_row().cells
            row[0].text = name
            row[1].text = str(count)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    return {"path": str(out_path), "problems": len(items)}


# ==========================================================================
# One document per homework number
# ==========================================================================
def _write_one_problem(doc: Document, n: int, mcq: dict, rows: list,
                       num_id: int | None = None) -> None:
    """Render a single numbered problem and its choices."""
    q = doc.add_paragraph()
    q.paragraph_format.space_before = Pt(0)
    q.paragraph_format.space_after = Pt(6)
    if num_id is not None:
        # Word owns the number: insert a question by hand and the rest renumber.
        _numbered(q, num_id)
    else:
        q.paragraph_format.left_indent = Q_INDENT
        q.paragraph_format.first_line_indent = -Q_HANGING
        q.add_run(omml.xml_safe(f"{n}.\t"))
    omml.append_rich_text(q, mcq["question_stem"])
    _body(q)

    options = assign_labels(mcq)
    _write_choices(doc, options, CHOICE_INDENT)

    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(0)
    _body(spacer)

    _, video = parse_source(mcq.get("video_id", ""))
    rows.append((video, n, next(o["label"] for o in options if o["is_correct"])))


def _write_hw_section(doc: Document, hw: str, items: list[dict],
                      include_key: bool, first: bool) -> int:
    """One homework in the reference layout, then its answer key.

    Matches POT_1.docx: centred plain title, questions as "N." with a hanging
    indent so wrapped lines align under the text, a blank line, then the
    choices as "(A)" ... "(D)". Choices go one per line, which the reference
    packs onto a single line -- with equations as options that runs off the
    page, and one per line stays readable.
    """
    if not first:
        doc.add_page_break()

    title = doc.add_paragraph(omml.xml_safe(hw))
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_after = Pt(10)
    _body(title)

    render_failures = 0
    rows: list[tuple[int | None, int, str]] = []   # (video, problem no, letter)

    num_id = _problem_numbering(doc) if config.AUTO_NUMBER else None

    skipped = []
    for n, mcq in enumerate(items, 1):
        mark = len(doc.paragraphs)   # rewind point if this item explodes
        try:
            rows_before = len(rows)
            _write_one_problem(doc, n, mcq, rows, num_id)
        except Exception as exc:
            # One malformed item must not cost the whole document. Remove
            # whatever it managed to emit and carry on.
            del rows[rows_before:]
            for para in list(doc.paragraphs[mark:]):
                para._element.getparent().remove(para._element)
            skipped.append(f"{mcq.get('question_id', '?')}: {type(exc).__name__}")
    if skipped:
        print(f"  ! {len(skipped)} item(s) could not be rendered and were left out:")
        for line in skipped[:5]:
            print(f"      {line}")



    if include_key:
        doc.add_page_break()
        head = doc.add_paragraph(omml.xml_safe(f"{hw} — Answer Key"))
        head.alignment = WD_ALIGN_PARAGRAPH.CENTER
        head.paragraph_format.space_after = Pt(10)
        _body(head, bold=True)

        # One line per source video: several problems often share a lesson.
        grouped: list[tuple[int | None, list[int], list[str]]] = []
        for video, number, letter in rows:
            if grouped and grouped[-1][0] == video:
                grouped[-1][1].append(number)
                grouped[-1][2].append(letter)
            else:
                grouped.append((video, [number], [letter]))

        for video, numbers, letters in grouped:
            line = doc.add_paragraph()
            line.paragraph_format.left_indent = Q_INDENT
            line.paragraph_format.space_after = Pt(3)
            video_part = f"Video {video}, " if video is not None else ""
            line.add_run(omml.xml_safe(f"Problem {compress_ranges(numbers)}, {video_part}"
                         f"ans. {', '.join(letters)}"))
            _body(line)

    return render_failures


def _group_by_hw(items: list[dict]) -> list[tuple[str, list[dict]]]:
    groups: dict[str, list[dict]] = {}
    for mcq in items:
        hw, _ = parse_source(mcq.get("video_id", ""))
        groups.setdefault(hw, []).append(mcq)
    return [(hw, sort_items(groups[hw], "natural"))
            for hw in sorted(groups, key=natural_key)]


def build_hw_document(hw: str, items: list[dict], out_path: Path,
                      include_key: bool = True) -> dict:
    """A single homework: title, numbered problems, page break, answer key."""
    doc = Document()
    _setup_page(doc)
    failures = _write_hw_section(doc, hw, items, include_key, first=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    return {"path": str(out_path), "problems": len(items), "render_failures": failures}


def build_by_hw(items: list[dict], out_dir: Path, include_key: bool = True) -> list[dict]:
    """One document per homework number, named for it: 1HW25.docx."""
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for hw, chunk in _group_by_hw(items):
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", hw)
        written.append(build_hw_document(
            hw, chunk, out_dir / f"{safe}.docx", include_key=include_key))
    return written


def series_label(series: str) -> str:
    """'1' -> '1HW';  '5 BASIC' -> '5HW BASIC'. A readable name for a family."""
    parts = str(series or "").split(" ", 1)
    head = f"{parts[0]}HW" if parts[0].isdigit() else parts[0]
    return head + (f" {parts[1]}" if len(parts) > 1 else "")


def build_held(items: list[dict], out_dir: Path, reasons: dict | None = None,
               prefix: str = "held", heading: str = "Held for Review") -> list[dict]:
    """Held questions, one document per level, annotated with why.

    Grouped the same way the worksheets are, so reviewing 1HW holds means
    opening one file rather than hunting through fixed-size chunks that mix
    levels together. Answer-key layout: the question, its options, the correct
    one marked, and the reason it was held.
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    families: dict[str, list[dict]] = {}
    for mcq in items:
        hw, _ = parse_source(mcq.get("video_id", ""))
        families.setdefault(hw_series(hw), []).append(mcq)

    written = []
    for series in sorted(families, key=natural_key):
        chunk = sort_items(families[series], "natural")
        label = series_label(series)
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", label)
        written.append(build_answer_key(
            chunk, out_dir / f"{prefix}_{safe}.docx",
            title=f"{label} — {heading}",
            subtitle=f"{len(chunk)} question(s) \u00b7 copy a question and its "
                     f"choices straight into your worksheet",
            reasons=reasons, copy_friendly=True))
    return written


def build_combined(items: list[dict], out_dir: Path, include_key: bool = True,
                   split_series: bool = True) -> list[dict]:
    """Homeworks combined into one document each starting on a fresh page.

    Layout per homework: title, problems, page break, answer key. The next
    homework's title begins on a new page after that key.

    By default one document per course series, so 1HW25-1HW29 and 2HW13-2HW20
    are separate files. A single document spanning unrelated course levels is
    rarely what anyone wants to hand out. `split_series=False` forces one file.
    """
    groups = _group_by_hw(items)
    if not groups:
        return []

    if split_series:
        families: dict[str, list[tuple[str, list[dict]]]] = {}
        for hw, chunk in groups:
            families.setdefault(hw_series(hw), []).append((hw, chunk))
        batches = [families[k] for k in sorted(families, key=natural_key)]
    else:
        batches = [groups]

    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for batch in batches:
        doc = Document()
        _setup_page(doc)
        failures = 0
        for i, (hw, chunk) in enumerate(batch):
            failures += _write_hw_section(doc, hw, chunk, include_key, first=(i == 0))

        first_hw, last_hw = batch[0][0], batch[-1][0]
        name = first_hw if len(batch) == 1 else f"{first_hw}-{last_hw}"
        out_path = out_dir / f"{re.sub(r'[^A-Za-z0-9_-]', '_', name)}.docx"
        doc.save(out_path)
        written.append({"path": str(out_path),
                        "problems": sum(len(c) for _, c in batch),
                        "render_failures": failures, "homeworks": len(batch)})
    return written


# ==========================================================================
# Batching into multiple worksheets
# ==========================================================================
def build_all(items: list[dict], out_dir: Path, per_worksheet: int | None = None,
              title: str | None = None, prefix: str = "worksheet",
              reasons: dict | None = None, student_copy: bool = True) -> list[dict]:
    """Split items into worksheets of N problems and write a key for each.

    `student_copy=False` writes only the annotated key. Held items are for you
    to review, not for a class, so a student worksheet of them is just noise.
    """
    per = per_worksheet or config.PROBLEMS_PER_WORKSHEET
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    chunks = [items[i:i + per] for i in range(0, len(items), per)] or [[]]
    for idx, chunk in enumerate(chunks, 1):
        if not chunk:
            continue
        tag = f"set{idx:02d}"
        sub = f"Set {idx} of {len(chunks)} \u00b7 {len(chunk)} problems"
        if student_copy:
            written.append(build_worksheet(
                chunk, out_dir / f"{prefix}_{tag}.docx", title=title, subtitle=sub))
            written.append(build_answer_key(
                chunk, out_dir / f"{prefix}_{tag}_KEY.docx", title=title,
                subtitle=sub, reasons=reasons))
        else:
            written.append(build_answer_key(
                chunk, out_dir / f"{prefix}_{tag}.docx", title=title,
                subtitle=sub, reasons=reasons))
    return written
