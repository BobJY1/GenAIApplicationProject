"""Stage 2: problem -> answer, checked by computer algebra.

This stage exists because the transcript does not. Previously the instructor's
worked solution was an external anchor on the answer; with frames only, the
model is both solver and sole witness to its own correctness.

So the model must also hand over a verification spec, which verifier.py runs
independently. `integrate` is computed here, not asserted. When the check
refutes the answer, the item is re-solved on a stronger model before any
distractors are built around it.

Solving is kept separate from distractor design so that a refuted answer costs
one solve rather than a whole discarded item.
"""

from __future__ import annotations

import config
import prompts
import schemas
import questions as Q
import verifier
from claude_client import ClaudeClient


def _caveats(question: dict) -> str:
    """Things the solver should know about how reliable the reading is."""
    out = []
    if not question.get("task_is_explicit", True):
        out.append(
            "The instruction was NOT written on the board; it was inferred. Basis: "
            + (question.get("task_inference_basis") or "not given")
        )
    if question.get("notes"):
        out.append(question["notes"])
    if question.get("unreadable_items"):
        out.append("Possibly misread on the board: " + "; ".join(question["unreadable_items"]))
    if question.get("legibility") == "partly_illegible":
        out.append("The board was partly illegible.")
    if question.get("has_diagram"):
        out.append("Figure: " + (question.get("diagram_description") or "present but undescribed"))
    if question.get("frame_notes"):
        out.append(question["frame_notes"])
    return prompts.EXTRACT_CAVEAT_TEMPLATE.format(caveats="\n".join(f"- {c}" for c in out)) if out else ""


def _part_note(question: dict) -> str:
    n = question.get("part_count", 1)
    if n <= 1:
        return ""
    label = question.get("part_label") or f"({question.get('part_index')})"
    return f"[part {label}, one of {n} questions on this board]"


def build_content(question: dict) -> str:
    return prompts.SOLVE_USER_TEMPLATE.format(
        question_id=question.get("question_id", ""),
        part_note=_part_note(question),
        topic=question.get("topic", ""),
        difficulty=question.get("difficulty", ""),
        answer_kind=question.get("answer_kind", "expression"),
        statement=Q.full_statement(question),
        board_transcription=question.get("board_transcription", "(not captured)"),
        caveat_block=_caveats(question),
    )


def solve(client: ClaudeClient, question: dict, model: str | None = None) -> dict:
    model = model or config.SOLVE_MODEL

    result = client.json_call(
        model=model,
        system=prompts.SOLVE_SYSTEM,
        content=build_content(question),
        schema=schemas.SOLVE_SCHEMA,
        max_tokens=config.SOLVE_MAX_TOKENS,
        effort=config.SOLVE_EFFORT,
    )

    result["question_id"] = question.get("question_id")
    result["video_id"] = question.get("video_id")
    result["_meta"] = {
        "stage": "solve",
        "model": model,
        "prompt_version": prompts.PROMPT_VERSION,
    }
    return result


def solve_and_verify(client: ClaudeClient, question: dict) -> dict:
    """Solve, run the symbolic check, and escalate when it fails.

    Three outcomes, handled differently:

      verified  computer algebra confirmed it. Done, whatever the model's own
                confidence said.
      refuted   the check contradicts the answer. Re-solve on the stronger
                model and re-check. Still refuted means a human looks at it.
      skipped   no template applied (proofs, sketches). Fall back to a second
      /error    independent solve and see whether the two agree.
    """
    result = solve(client, question)
    check = verifier.verify(result.get("verification"), config.VERIFY_TIMEOUT)
    result["verification_result"] = check.as_dict()

    if check.contradicted:
        first = result
        result = solve(client, question, model=config.ESCALATION_MODEL)
        check = verifier.verify(result.get("verification"), config.VERIFY_TIMEOUT)
        result["verification_result"] = check.as_dict()
        result["_meta"]["escalated_from"] = {
            "model": first["_meta"]["model"],
            "answer": first.get("final_answer_plain"),
            "refutation": first["verification_result"]["detail"],
        }
        return result

    if check.ok:
        return result

    # No mechanical check available. Second opinion instead: solve again on the
    # stronger model and require the two answers to agree symbolically. Weaker
    # evidence than computation, but far better than one unchecked assertion.
    if config.SECOND_OPINION_WHEN_UNVERIFIED and result.get("answer_is_expression"):
        second = solve(client, question, model=config.ESCALATION_MODEL)
        agreed = verifier.agree(
            result.get("final_answer_plain", ""), second.get("final_answer_plain", ""),
            config.VERIFY_TIMEOUT)
        result["second_opinion"] = {
            "model": second["_meta"]["model"],
            "answer_plain": second.get("final_answer_plain"),
            "answer_latex": second.get("final_answer_latex"),
            "agrees": agreed,
        }
        if agreed is False:
            # Prefer the stronger model's answer, and make sure a human sees it.
            second["verification_result"] = {
                "status": verifier.SKIPPED,
                "detail": "no symbolic check; two models disagreed and the stronger one was kept",
                "kind": "none",
            }
            second["second_opinion"] = dict(result["second_opinion"],
                                            replaced_answer=result.get("final_answer_plain"))
            second["video_id"] = result["video_id"]
            second["question_id"] = result["question_id"]
            second["_meta"]["escalated_from"] = {"model": result["_meta"]["model"],
                                                 "answer": result.get("final_answer_plain")}
            return second
    return result
