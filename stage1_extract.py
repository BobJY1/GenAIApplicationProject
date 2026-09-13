"""Stage 1: whiteboard frame -> structured problem statement.

Vision only. No answer is produced here; see stage2_solve.py for why.
"""

from __future__ import annotations

import config
import prompts
import schemas
from claude_client import ClaudeClient, encode_frame
from sources import VideoSource


def build_content(src: VideoSource) -> list[dict]:
    """Image first, then the instruction.

    With a transcript the text went first, to prime what to look for on the
    board. There is no transcript now, so the image leads and there is nothing
    to bias the read.
    """
    blocks: list[dict] = [encode_frame(src.frame)]
    text = prompts.EXTRACT_USER_TEMPLATE.format(video_id=src.video_id)

    # If a transcript does turn up later, use it — it is strictly more evidence.
    if src.has_transcript:
        text += (
            "\n\nA transcript of this lesson is also available. Use it to resolve "
            "handwriting and to establish what was actually asked; if it states the "
            "task, `task_is_explicit` may be true.\n\n"
            f"<transcript>\n{src.transcript}\n</transcript>\n"
        )

    blocks.append({"type": "text", "text": text})
    return blocks


def extract(client: ClaudeClient, src: VideoSource, model: str | None = None) -> dict:
    model = model or config.EXTRACT_MODEL

    result = client.json_call(
        model=model,
        system=prompts.EXTRACT_SYSTEM,
        content=build_content(src),
        schema=schemas.EXTRACTION_SCHEMA,
        max_tokens=config.EXTRACT_MAX_TOKENS,
        effort=config.EXTRACT_EFFORT,
    )

    result["video_id"] = src.video_id
    result["_meta"] = {
        "stage": "extract",
        "model": model,
        "prompt_version": prompts.PROMPT_VERSION,
        "frame": str(src.frame),
        "had_transcript": src.has_transcript,
    }
    return result


def _needs_stronger_model(result: dict) -> tuple[bool, str]:
    """Is this frame worth rereading on the expensive model? Returns (yes, why).

    Escalation rereads the WHOLE board, so the test has to be about the board,
    not about one shaky question on it. A seven-part frame with six clean
    questions and one dubious one should ship the six and send the one to a
    human -- rereading all seven triples the cost of six answers that were
    already fine.

    Note `task_is_explicit` is deliberately absent. It moved onto each question
    in the multi-part change, and the frame-level lookup that used to live here
    silently defaulted to True, so the trigger never fired again. An inferred
    task is now a review signal, not a reason to spend more money: a stronger
    model cannot read an instruction that was never written down.
    """
    questions = result.get("questions") or []

    if not questions:
        return True, "no questions were extracted"
    if result.get("confidence", 0) < config.ESCALATE_BELOW:
        return True, f"frame confidence {result.get('confidence')}"
    if result.get("legibility") == "partly_illegible":
        return True, "board partly illegible"
    if result.get("unreadable_items"):
        return True, f"unreadable: {'; '.join(result['unreadable_items'])[:60]}"

    weak = [q for q in questions if q.get("confidence", 1) < config.ESCALATE_BELOW]
    share = len(weak) / len(questions)
    if share >= config.ESCALATE_QUESTION_SHARE:
        return True, f"{len(weak)} of {len(questions)} questions below threshold"

    return False, ""


def extract_with_escalation(client: ClaudeClient, src: VideoSource) -> dict:
    """Cheap model first; rerun on the stronger one only where it earns it."""
    result = extract(client, src)

    needed, why = _needs_stronger_model(result)
    if needed:
        first = result
        result = extract(client, src, model=config.ESCALATION_MODEL)
        result["_meta"]["escalated_from"] = {
            "model": first["_meta"]["model"],
            "reason": why,
            "confidence": first.get("confidence"),
            "question_count": len(first.get("questions") or []),
        }
        # Two independent reads of the same board. If they disagree about how
        # many questions are on it, one of them missed something.
        before, after = len(first.get("questions") or []), len(result.get("questions") or [])
        if before != after:
            result.setdefault("notes", "")
            result["notes"] += (f" [two reads found different question counts: "
                                f"{before} then {after}]")
    return result
