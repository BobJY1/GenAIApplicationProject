"""Stage 3: verified problem and answer -> multiple-choice item."""

from __future__ import annotations

import config
import prompts
import schemas
import questions as Q
from claude_client import ClaudeClient, encode_frame


def _caveats(question: dict, solution: dict) -> str:
    out = []
    if not question.get("task_is_explicit", True):
        out.append(
            "The task was inferred from the board, not written on it, so this item may be "
            "asking the wrong question. Basis: "
            + (question.get("task_inference_basis") or "not given")
        )
    if question.get("notes"):
        out.append(question["notes"])
    if question.get("unreadable_items"):
        out.append("Possibly misread on the board: " + "; ".join(question["unreadable_items"]))
    if question.get("has_diagram"):
        out.append(prompts.DIAGRAM_NOTE + " Figure: "
                   + (question.get("diagram_description") or "present but undescribed"))
    if solution.get("notes"):
        out.append("Solver assumption: " + solution["notes"])
    so = solution.get("second_opinion")
    if so and so.get("agrees") is False:
        out.append(f"Two models disagreed on the answer; the other proposed {so.get('replaced_answer')}.")
    if not solution.get("answer_is_expression", True):
        out.append("The answer is not an expression, so multiple choice may not fit this problem.")
    return prompts.MCQ_CAVEAT_TEMPLATE.format(caveats="\n".join(f"- {c}" for c in out)) if out else ""


def _part_note(question: dict) -> str:
    n = question.get("part_count", 1)
    if n <= 1:
        return ""
    label = question.get("part_label") or f"({question.get('part_index')})"
    return f"[part {label}, one of {n} questions on this board]"


def build_content(question: dict, solution: dict, resend_frame: bool = False):
    steps = solution.get("solution_steps") or []
    check = solution.get("verification_result") or {}

    text = prompts.MCQ_USER_TEMPLATE.format(
        question_id=question.get("question_id", ""),
        part_note=_part_note(question),
        topic=question.get("topic", ""),
        difficulty=question.get("difficulty", ""),
        answer_kind=question.get("answer_kind", "expression"),
        statement=Q.full_statement(question),
        verification_status=check.get("status", "unknown"),
        final_answer_latex=solution.get("final_answer_latex", ""),
        verification_detail=check.get("detail", "no check was run"),
        solution_steps="\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)) or "(none)",
        caveat_block=_caveats(question, solution),
    )

    if not resend_frame:
        return text
    frame = question.get("frame")
    if not frame:
        return text

    from pathlib import Path
    path = Path(frame)
    if not path.exists():
        # The figure would help, but losing it is no reason to lose the whole
        # question. Carry on text-only and let the caller flag it.
        return text
    try:
        return [{"type": "text", "text": text}, encode_frame(path)]
    except Exception:
        return text


def generate(client: ClaudeClient, question: dict, solution: dict,
             model: str | None = None) -> dict:
    model = model or config.MCQ_MODEL
    resend = bool(question.get("has_diagram"))
    frame = question.get("frame") or ""
    frame_missing = bool(resend and frame and not __import__("pathlib").Path(frame).exists())

    result = client.json_call(
        model=model,
        system=prompts.MCQ_SYSTEM,
        content=build_content(question, solution, resend_frame=resend),
        schema=schemas.MCQ_SCHEMA,
        max_tokens=config.MCQ_MAX_TOKENS,
        effort=config.MCQ_EFFORT,
    )

    check = solution.get("verification_result") or {}
    result["question_id"] = question.get("question_id")
    result["video_id"] = question.get("video_id")
    result["part_label"] = question.get("part_label")
    result["part_count"] = question.get("part_count")
    result["topic"] = question.get("topic")
    result["difficulty"] = question.get("difficulty")
    result["answer_kind"] = question.get("answer_kind")
    if frame_missing:
        result.setdefault("review_flags", []).append(
            "the figure for this question was not available when the item was written")
    result["_meta"] = {
        "stage": "mcq",
        "model": model,
        "prompt_version": prompts.PROMPT_VERSION,
        "frame_resent": resend and not frame_missing,
        "frame_missing": frame_missing,
        "task_is_explicit": question.get("task_is_explicit"),
        "extraction_confidence": question.get("confidence"),
        "solve_confidence": solution.get("confidence"),
        "verification_status": check.get("status"),
        "verification_kind": check.get("kind"),
        "solver_answer_plain": solution.get("final_answer_plain"),
    }
    return result


def generate_with_escalation(client: ClaudeClient, question: dict, solution: dict) -> dict:
    result = generate(client, question, solution)
    if result.get("confidence", 0) < config.ESCALATE_BELOW:
        first = result
        result = generate(client, question, solution, model=config.ESCALATION_MODEL)
        result["_meta"]["escalated_from"] = {
            "model": first["_meta"]["model"],
            "confidence": first.get("confidence"),
        }
    return result
