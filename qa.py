"""Deterministic checks that run on every generated item.

The model reports its own confidence, but self-reported confidence misses the
failure modes that actually ruin a multiple-choice item. These checks are
mechanical and catch things a reader would otherwise catch at 2am the night
before class.

Two of them exist specifically because there is no transcript:

  * `task_is_explicit` is false — the instruction was never written on the
    board and the model supplied it. The mathematics may be flawless and the
    question still wrong. Nothing downstream can detect this, so it is held.
  * the answer was never confirmed by computer algebra, and no second solve
    agreed with it. Not evidence of error, but it is unbacked.

Items with any `error` are held back from the worksheet. Items with only
`warn` still ship, and land in the QA report.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import config

import schemas
import verifier

BUILD_ID = "2026-08-16.2"   # must match config.BUILD_ID

NON_NUMERIC = "NON_NUMERIC"


@dataclass
class QAResult:
    """Three severities, because they need different treatment.

    blocking  the item cannot be used as written: two options that are the
              same number, LaTeX that will not render, an answer computer
              algebra refuted. Never ships, in any mode.
    advisory  the item is probably fine but something was uncertain. Ships
              under HOLD_MODE="flag" and appears in the concerns report.
    warnings  worth knowing, never affects whether it ships.
    """
    video_id: str          # question_id in the multi-part pipeline
    blocking: list[str] = field(default_factory=list)
    advisory: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def errors(self) -> list[str]:
        """Whatever counts as disqualifying in the current mode."""
        if config.HOLD_MODE == "strict":
            return self.blocking + self.advisory
        return self.blocking

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def concerns(self) -> list[str]:
        """Everything a human should look at, whether or not it was held."""
        return self.blocking + self.advisory

    def as_dict(self) -> dict:
        return {"video_id": self.video_id, "ok": self.ok,
                "blocking": self.blocking, "advisory": self.advisory,
                "errors": self.errors, "warnings": self.warnings}


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").strip().lower())


def _sympy_equal(a: str, b: str) -> bool | None:
    """True/False if both parse, None if either does not."""
    return verifier.agree(a or "", b or "", timeout=10)


def _latex_renders(tex: str) -> bool:
    try:
        import latex2mathml.converter as l2m
        l2m.convert(tex or "")
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------
def check_item(mcq: dict, extraction: dict | None = None, solution: dict | None = None) -> QAResult:
    vid = mcq.get("question_id") or mcq.get("video_id", "?")
    r = QAResult(vid)

    correct = mcq.get("correct_answer") or {}
    distractors = mcq.get("distractors") or []

    # --- structure ---------------------------------------------------------
    if len(distractors) != 3:
        r.blocking.append(f"expected 3 distractors, got {len(distractors)}")
    if not (correct.get("latex") or "").strip():
        r.blocking.append("correct answer has no LaTeX")
    if not (mcq.get("question_stem") or "").strip():
        r.blocking.append("empty question stem")

    options = [correct] + list(distractors)

    # --- surface distinctness ---------------------------------------------
    seen: dict[str, int] = {}
    for i, opt in enumerate(options):
        for name in ("latex", "plain"):
            norm = _normalize(opt.get(name, ""))
            # NON_NUMERIC is a placeholder, not a value. Interval and set answers
            # all carry it, and treating them as equal flags every such item as
            # having four identical options.
            if not norm or norm == "non_numeric":
                continue
            key = f"{name}:{norm}"
            if key in seen:
                r.blocking.append(f"options {seen[key]} and {i} have identical {name}: {opt.get(name)!r}")
            seen[key] = i

    # --- mathematical distinctness ----------------------------------------
    # Catches the sneaky duplicates: 1/2 vs 0.5, sqrt(4) vs 2, 2*pi vs 6.283...
    # All comparisons for this item go in one batch: a subprocess per pair made
    # a QA pass over a few thousand items take well over an hour.
    pairs, meta = [], []
    for i, d in enumerate(distractors, 1):
        pairs.append((correct.get("plain", ""), d.get("plain", "")))
        meta.append(("correct", i))
    for i in range(len(distractors)):
        for j in range(i + 1, len(distractors)):
            pairs.append((distractors[i].get("plain", ""), distractors[j].get("plain", "")))
            meta.append(("pair", (i + 1, j + 1)))
    if solution:
        pairs.append((solution.get("final_answer_plain", ""), correct.get("plain", "")))
        meta.append(("drift", None))

    outcomes = verifier.compare_batch(pairs) if pairs else []
    drift = None
    unverified = 0
    for (kind, which), eq in zip(meta, outcomes):
        if kind == "correct":
            if eq is True:
                d = distractors[which - 1]
                r.blocking.append(
                    f"distractor {which} ({d.get('plain')!r}) is mathematically equal to the "
                    f"correct answer ({correct.get('plain')!r})")
            elif eq is None:
                unverified += 1
        elif kind == "pair" and eq is True:
            r.blocking.append(f"distractors {which[0]} and {which[1]} are mathematically equal")
        elif kind == "drift":
            drift = eq

    if unverified:
        kind = mcq.get("answer_kind", "expression")
        note = (f"{unverified} option(s) could not be compared symbolically")
        if kind in ("interval", "set"):
            note += f" — expected for {kind} answers; check the options by eye"
        r.warnings.append(note)

    # --- rendering ---------------------------------------------------------
    for i, opt in enumerate(options):
        if not _latex_renders(opt.get("latex", "")):
            r.blocking.append(f"option {i} LaTeX will not render: {opt.get('latex')!r}")
    for tex in re.findall(r"\$\$?(.+?)\$\$?", mcq.get("question_stem", ""), re.S):
        if not _latex_renders(tex):
            r.blocking.append(f"stem LaTeX will not render: {tex[:60]!r}")

    # --- the answer must not be visible in the stem ------------------------
    stem_norm = _normalize(mcq.get("question_stem", ""))
    ans_norm = _normalize(correct.get("latex", ""))
    if len(ans_norm) >= 4 and ans_norm in stem_norm:
        r.blocking.append("the correct answer appears verbatim in the question stem")

    # --- format tells ------------------------------------------------------
    # Students who do not know the material still score above chance when the
    # correct option is visibly the odd one out.
    lengths = [len(o.get("latex", "")) for o in options]
    if len(lengths) == 4:
        others = lengths[1:]
        avg = sum(others) / len(others) if others else 0
        if avg and lengths[0] > 2.2 * avg:
            r.warnings.append("correct option is much longer than the distractors")
        if avg and lengths[0] * 2.2 < avg:
            r.warnings.append("correct option is much shorter than the distractors")

    # --- pedagogical quality ----------------------------------------------
    valid = {m.lower() for m in schemas.MISCONCEPTIONS}
    used = []
    for i, d in enumerate(distractors, 1):
        # Structured outputs do not guarantee enum casing, so compare lowered.
        m = (d.get("misconception") or "").lower()
        if m not in valid:
            r.warnings.append(f"distractor {i} has an unrecognized misconception: {m!r}")
        used.append(m)
        if not (d.get("student_reasoning") or "").strip():
            r.warnings.append(f"distractor {i} has no student_reasoning")
        if (d.get("plausibility") or 0) < 0.5:
            r.warnings.append(f"distractor {i} self-rates implausible ({d.get('plausibility')})")
    if len(used) == 3 and len(set(used)) == 1:
        r.warnings.append(f"all three distractors use the same misconception ({used[0]})")

    # --- confidence --------------------------------------------------------
    conf = mcq.get("confidence", 0)
    if conf < config.HUMAN_REVIEW_BELOW:
        r.advisory.append(f"distractor confidence {conf} is low")
    elif conf < config.ESCALATE_BELOW:
        r.warnings.append(f"distractor confidence {conf} is low")

    for flag in mcq.get("review_flags", []):
        r.warnings.append(f"model flag: {flag}")

    # --- stage 1: was the question even the right question? ----------------
    if extraction:
        if not extraction.get("task_is_explicit", True):
            basis = extraction.get("task_inference_basis") or "no basis given"
            msg = f"the task was inferred, not written on the board: {basis}"

            # An inferred task is a risk, not a verdict. A bare equation carries
            # no written instruction and still has exactly one sensible reading.
            # Hold it only when the rest of the evidence is also weak: when the
            # reader was unsure, or when nothing confirmed the answer. A
            # confident reading whose answer computer algebra proved is far more
            # likely to be a correct question than a wrongly guessed one.
            verified = ((solution or {}).get("verification_result") or {}).get(
                "status") == verifier.VERIFIED
            confident = extraction.get("confidence", 0) >= config.ESCALATE_BELOW

            if config.REQUIRE_EXPLICIT_TASK and not (verified and confident):
                r.advisory.append(msg)
            else:
                r.warnings.append(
                    msg + " — shipped anyway: the reading was confident and the "
                          "answer verified, but confirm the question is the one asked")

        if extraction.get("confidence", 1) < config.HUMAN_REVIEW_BELOW:
            r.advisory.append(f"extraction confidence {extraction['confidence']} is low")
        if extraction.get("notes"):
            # Per-question note. The one that matters: instruction written but
            # the board does not carry enough information to answer it.
            r.warnings.append(f"question note: {extraction['notes']}")
        if extraction.get("confidence", 1) < 0.5:
            r.advisory.append(
                f"question confidence {extraction['confidence']} — usually means the "
                f"instruction is written but the board lacks the data to answer it")
        if extraction.get("answer_kind") in ("prose", "drawing"):
            r.blocking.append(
                f"answer kind '{extraction['answer_kind']}' does not fit multiple choice")
        if extraction.get("legibility") == "partly_illegible":
            r.warnings.append("board was partly illegible")
        if extraction.get("unreadable_items"):
            r.warnings.append("unreadable on the board: " + "; ".join(extraction["unreadable_items"]))
        if extraction.get("has_diagram"):
            r.warnings.append("depends on a figure the worksheet does not carry")
        if extraction.get("frame_notes"):
            r.warnings.append(f"frame note: {extraction['frame_notes']}")

    # --- stage 2: is the answer backed by anything? ------------------------
    if solution:
        check = solution.get("verification_result") or {}
        status = check.get("status")

        if status == verifier.REFUTED:
            # Seen in the pilot: the answer was right and the verification spec
            # was wrong (it assumed f(x)=x^2 for a parabola with vertex (3,1)).
            # Either way a human looks, but do not prejudge which end is broken.
            r.blocking.append(
                f"computer algebra contradicts this item: {check.get('detail','')} "
                f"— check both the answer and the verification spec")
        elif status == verifier.VERIFIED:
            pass  # the strongest signal available; nothing to say
        else:
            second = solution.get("second_opinion") or {}
            if second.get("agrees") is True:
                r.warnings.append("no symbolic check applied; a second independent solve agreed")
            elif second.get("agrees") is False:
                # Keep these short: an answer can be a long expression and the
                # raw text made the QA summary unreadable.
                def _short(v, n=40):
                    t = " ".join(str(v or "").split())
                    return t if len(t) <= n else t[:n] + "..."
                r.blocking.append(
                    f"no symbolic check, and two models disagreed: "
                    f"{_short(second.get('replaced_answer'))} vs "
                    f"{_short(solution.get('final_answer_plain'))}")
            else:
                msg = (f"answer is unverified ({check.get('detail') or 'no check ran'})")
                (r.advisory if config.REQUIRE_VERIFIED_ANSWER else r.warnings).append(msg)

        # The item must be built on the answer that was actually checked.
        if drift is False:
            r.blocking.append(
                f"the worksheet answer {correct.get('plain')!r} is not the answer that was "
                f"verified ({solution.get('final_answer_plain')!r})")

        if not solution.get("answer_is_expression", True):
            r.warnings.append("answer is not an expression; multiple choice may not fit")
        if solution.get("confidence", 1) < config.HUMAN_REVIEW_BELOW:
            r.advisory.append(f"solver confidence {solution['confidence']} is low")
        if solution.get("notes"):
            r.warnings.append(f"solver assumption: {solution['notes']}")

    return r
