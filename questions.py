"""Fan-out from frames to questions.

A frame is not a question. The pilot on real lesson boards found 20 frames
carrying 43 questions -- one board had seven numbered parts, another eight.
Treating a frame as a single problem threw away more than half the material,
and worse, it made a frame all-or-nothing: one unreadable sub-part held back
the six good questions sitting next to it.

So stage 1 returns a list of questions per frame, and everything downstream --
solving, verification, QA, worksheets, review -- works on a question record.
This module is the seam between the two. Frame-level context (the board
transcription, legibility, the figure) is copied onto every question so each
record stands alone.
"""

from __future__ import annotations

import re

# Filename-safe and valid as a Batches API custom_id (^[a-zA-Z0-9_-]{1,64}$),
# which rules out the dot that would otherwise read more naturally.
_SAFE = re.compile(r"[^A-Za-z0-9_-]")


def question_id(video_id: str, index: int) -> str:
    return f"{_SAFE.sub('-', str(video_id))}-q{index}"


def split_question_id(qid: str) -> tuple[str, int]:
    """'10HW25_P3-q5' -> ('10HW25_P3', 5). Returns index 1 if unsuffixed."""
    m = re.match(r"^(.*)-q(\d+)$", qid or "")
    return (m.group(1), int(m.group(2))) if m else (qid, 1)


# Frame-level fields every question inherits.
_INHERIT = (
    "board_transcription", "legibility", "unreadable_items",
    "has_diagram", "diagram_description", "board_shows_solution",
)
_INHERIT_DEFAULTS = {"unreadable_items": [], "board_shows_solution": False}


def iter_questions(extraction: dict) -> list[dict]:
    """Expand one frame extraction into self-contained question records.

    Tolerates the old single-problem shape so extractions produced before this
    change still run: a frame with no `questions` array becomes one question.
    """
    video_id = extraction.get("video_id", "")
    raw = extraction.get("questions")

    if not raw:  # legacy single-problem extraction
        raw = [{
            "part_label": "",
            "statement": extraction.get("statement", ""),
            "task_is_explicit": extraction.get("task_is_explicit", True),
            "task_inference_basis": extraction.get("task_inference_basis", ""),
            "topic": extraction.get("topic", ""),
            "difficulty": extraction.get("difficulty", "medium"),
            "answer_kind": "expression",
            "confidence": extraction.get("confidence", 0.0),
            "notes": extraction.get("notes", ""),
        }]

    meta = extraction.get("_meta") or {}
    out = []
    for i, q in enumerate(raw, 1):
        rec = {
            "question_id": question_id(video_id, i),
            "video_id": video_id,
            "part_index": i,
            "part_count": len(raw),
            "part_label": q.get("part_label", ""),
            "statement": q.get("statement", ""),
            "task_is_explicit": bool(q.get("task_is_explicit", True)),
            "task_inference_basis": q.get("task_inference_basis", ""),
            "topic": q.get("topic", ""),
            "difficulty": q.get("difficulty", "medium"),
            "answer_kind": q.get("answer_kind", "expression"),
            "confidence": q.get("confidence", 0.0),
            "notes": q.get("notes", ""),
            "shared_context": extraction.get("shared_context", ""),
            "frame": meta.get("frame", ""),
            "frame_confidence": extraction.get("confidence", 0.0),
            "frame_notes": extraction.get("notes", ""),
        }
        for field in _INHERIT:
            rec[field] = extraction.get(field, _INHERIT_DEFAULTS.get(field, ""))
        out.append(rec)
    return out


def count(extraction: dict) -> int:
    return len(extraction.get("questions") or [1])


def full_statement(question: dict) -> str:
    """Statement as a student must read it, with shared setup restored if needed.

    Stage 1 is told to make every statement self-contained, because "find the
    intervals where f increases" is unanswerable without f. This is the safety
    net for when it does not.
    """
    stmt = (question.get("statement") or "").strip()
    ctx = (question.get("shared_context") or "").strip()
    if not ctx:
        return stmt
    # Crude but effective: if the statement never names the shared setup, prepend it.
    key = re.sub(r"[^A-Za-z0-9]", "", ctx)[:18]
    if key and key.lower() in re.sub(r"[^A-Za-z0-9]", "", stmt).lower():
        return stmt
    return f"{ctx} {stmt}" if ctx else stmt
