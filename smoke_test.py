#!/usr/bin/env python3
"""Exercise everything downstream of the API without spending a cent.

    python smoke_test.py

Covers the LaTeX->OMML converter, the SymPy answer verifier, the QA checks, and
document generation, using fixtures that include deliberately broken items. Run
this first: it separates "my environment is wrong" from "the model output is
wrong", which are very different problems.
"""

from __future__ import annotations

import sys
from pathlib import Path

import docx_builder
import omml
import qa
import verifier

OUT = Path(__file__).parent / "output" / "smoke"


# --------------------------------------------------------------------------
# Fixtures: one clean item, and four with a specific planted defect each.
# --------------------------------------------------------------------------
EXTRACTION_GOOD = {
    "video_id": "video001",
    "statement": "The circle $x^2 + y^2 = 25$ passes through $(3, 4)$. Find the slope of the tangent line there.",
    "task_is_explicit": True, "task_inference_basis": "",
    "legibility": "clear", "unreadable_items": [], "has_diagram": False,
    "confidence": 0.95, "notes": "",
}
SOLUTION_GOOD = {
    "video_id": "video001",
    "final_answer_latex": r"-\\frac{3}{4}", "final_answer_plain": "-3/4",
    "answer_is_expression": True, "confidence": 0.96, "notes": "",
    "verification": {"kind": "implicit_derivative", "expr": "x**2 + y**2 - 25",
                     "var": "x", "var2": "y", "point": "3", "point_y": "4",
                     "claimed": "-3/4"},
}

MCQ_GOOD = {
    "video_id": "video001", "topic": "implicit differentiation", "difficulty": "medium",
    "question_stem": (
        "The circle $x^2 + y^2 = 25$ passes through the point $(3, 4)$. "
        "Find the slope of the tangent line to the circle at that point."
    ),
    "correct_answer": {"latex": r"-\\frac{3}{4}", "plain": "-3/4"},
    "distractors": [
        {"latex": r"\\frac{3}{4}", "plain": "3/4", "misconception": "sign_error",
         "student_reasoning": "Moved a term across without flipping the sign.", "plausibility": 0.9},
        {"latex": r"\\frac{4}{3}", "plain": "4/3", "misconception": "reversed_operation",
         "student_reasoning": "Found the slope of the normal line instead.", "plausibility": 0.8},
        {"latex": r"\\frac{3}{8}", "plain": "3/8", "misconception": "chain_rule_omitted",
         "student_reasoning": "Differentiated y^2 as 2y and lost the dy/dx factor.", "plausibility": 0.75},
    ],
    "confidence": 0.95, "review_flags": [],
}

# Two options are the same number written differently, so the item has no
# single right answer. Invisible to a human skimming; SymPy catches it.
MCQ_DUPLICATE = {
    **MCQ_GOOD, "video_id": "video002",
    "question_stem": r"Evaluate $\\int_0^1 x\\,dx$.",
    "correct_answer": {"latex": r"\\frac{1}{2}", "plain": "1/2"},
    "distractors": [
        {"latex": "0.5", "plain": "0.5", "misconception": "arithmetic_slip",
         "student_reasoning": "Decimal form.", "plausibility": 0.6},
        {"latex": "1", "plain": "1", "misconception": "dropped_constant",
         "student_reasoning": "Forgot to divide by 2.", "plausibility": 0.8},
        {"latex": "2", "plain": "2", "misconception": "reversed_operation",
         "student_reasoning": "Multiplied by 2 instead of dividing.", "plausibility": 0.5},
    ],
}

# The answer is printed in the stem.
MCQ_LEAKED = {
    **MCQ_GOOD, "video_id": "video003",
    "question_stem": r"Given $\\frac{dy}{dx} = -\\frac{3}{4}$, what is the slope of the tangent?",
}

# The board never said what to do; the task was inferred. The mathematics can
# be flawless and the question still wrong, and with no transcript nothing
# downstream can tell.
EXTRACTION_INFERRED = {
    **EXTRACTION_GOOD, "video_id": "video004",
    "task_is_explicit": False,
    "task_inference_basis": "board showed only the circle equation and a marked point; "
                            "tangent slope is the usual task, but it could have been arc length",
    "confidence": 0.72,
}
MCQ_INFERRED = {**MCQ_GOOD, "video_id": "video004"}

# The model's answer is wrong and SymPy says so. This must never reach a
# worksheet, whatever the model's confidence claimed.
SOLUTION_WRONG = {
    "video_id": "video005",
    "final_answer_latex": r"\\frac{3}{4}", "final_answer_plain": "3/4",
    "answer_is_expression": True, "confidence": 0.93, "notes": "",
    "verification": {"kind": "implicit_derivative", "expr": "x**2 + y**2 - 25",
                     "var": "x", "var2": "y", "point": "3", "point_y": "4",
                     "claimed": "3/4"},
}
MCQ_WRONG = {**MCQ_GOOD, "video_id": "video005",
             "correct_answer": {"latex": r"\\frac{3}{4}", "plain": "3/4"},
             "distractors": [
                 {"latex": r"-\\frac{3}{4}", "plain": "-3/4", "misconception": "sign_error",
                  "student_reasoning": "Sign slip.", "plausibility": 0.9},
                 {"latex": r"\\frac{4}{3}", "plain": "4/3", "misconception": "reversed_operation",
                  "student_reasoning": "Normal line.", "plausibility": 0.8},
                 {"latex": r"\\frac{3}{8}", "plain": "3/8", "misconception": "chain_rule_omitted",
                  "student_reasoning": "Lost dy/dx.", "plausibility": 0.7},
             ]}


def run_verifications(solutions):
    for s in solutions:
        s["verification_result"] = verifier.verify(s.get("verification")).as_dict()
    return solutions


def main() -> int:
    failures = 0

    # A partial update -- one module replaced, another skipped -- surfaces as a
    # confusing TypeError deep inside a build. Catch it here instead.
    print("0. Module versions")
    import config, docx_builder, pipeline, qa as _qa, omml as _omml, verifier as _v
    stamps = {m.__name__: getattr(m, "BUILD_ID", "MISSING")
              for m in (config, docx_builder, pipeline, _qa, _omml, _v)}
    if len(set(stamps.values())) == 1:
        print(f"   all modules at {config.BUILD_ID}")
    else:
        failures += 1
        print("   FAIL modules are from different releases — re-extract the update")
        print("        and choose 'Replace the files in the destination':")
        for mod, ver in sorted(stamps.items()):
            print(f"          {mod:14s} {ver}")


    print("1. LaTeX -> OMML conversion")
    bad = omml.selftest()
    failures += bool(bad)
    for tex, err in bad:
        print(f"   FAIL {tex}: {err}")
    if not bad:
        print("   all notation samples converted")

    print("\n2. SymPy answer verification")
    run_verifications([SOLUTION_GOOD, SOLUTION_WRONG])
    for sol, expected in ((SOLUTION_GOOD, verifier.VERIFIED), (SOLUTION_WRONG, verifier.REFUTED)):
        got = sol["verification_result"]
        match = got["status"] == expected
        failures += not match
        print(f"   {sol['video_id']}: answer {sol['final_answer_plain']:>6} -> "
              f"{got['status']:9s} ({'as expected' if match else 'UNEXPECTED'})")
        print(f"      {got['detail']}")

    print("\n3. QA checks")
    # Expected severity, not just pass/fail. A defect that makes the item
    # unusable must block; mere uncertainty must not.
    cases = [
        ("clean item",              MCQ_GOOD,      EXTRACTION_GOOD,     SOLUTION_GOOD,  "clean"),
        ("duplicate options",       MCQ_DUPLICATE, None,                None,           "blocking"),
        ("answer leaked into stem", MCQ_LEAKED,    None,                None,           "blocking"),
        ("task was inferred",       MCQ_INFERRED,  EXTRACTION_INFERRED, SOLUTION_GOOD,  "advisory"),
        ("answer refuted by SymPy", MCQ_WRONG,     EXTRACTION_GOOD,     SOLUTION_WRONG, "blocking"),
    ]
    passing = []
    for label, mcq, ext, sol, expected in cases:
        r = qa.check_item(mcq, ext, sol)
        got = "blocking" if r.blocking else ("advisory" if r.advisory else "clean")
        match = got == expected
        failures += not match
        verdict = {"blocking": "BLOCKED", "advisory": "ships, flagged", "clean": "ships"}[got]
        print(f"   {mcq['video_id']} {label:24s} {verdict:14s} "
              f"({'as expected' if match else 'UNEXPECTED, wanted ' + expected})")
        for e in r.blocking:
            print(f"      BLOCK: {e}")
        for e in r.advisory:
            print(f"      check: {e}")
        if r.ok:
            passing.append(mcq)

    print("\n4. Answer position shuffle")
    for mcq in (MCQ_GOOD, MCQ_DUPLICATE, MCQ_LEAKED, MCQ_INFERRED):
        key = next(o["label"] for o in docx_builder.assign_labels(mcq) if o["is_correct"])
        print(f"   {mcq['video_id']}: correct answer is {key}")

    # Questions from one board must not all land on the same letter, and the
    # spread across many questions must stay near 25% each.
    from collections import Counter
    same_board = [next(o["label"] for o in docx_builder.assign_labels(
        dict(MCQ_GOOD, question_id=f"board-q{i}", video_id="board")) if o["is_correct"])
        for i in range(1, 9)]
    if len(set(same_board)) < 2:
        failures += 1
        print(f"   FAIL one board, 8 questions, all answered {same_board[0]}")
    else:
        print(f"   one board, 8 questions: {' '.join(same_board)}")

    spread = Counter(next(o["label"] for o in docx_builder.assign_labels(
        dict(MCQ_GOOD, question_id=f"v{v}-q{q}", video_id=f"v{v}")) if o["is_correct"])
        for v in range(300) for q in (1, 2))
    total = sum(spread.values())
    worst = max(abs(spread[k] / total - 0.25) for k in "ABCD")
    ok = worst < 0.06
    failures += 0 if ok else 1
    print(f"   {total} questions: " + "  ".join(f"{k} {100*spread[k]/total:4.1f}%" for k in "ABCD")
          + ("" if ok else "   FAIL — skewed"))

    print("\n5. Document generation")
    OUT.mkdir(parents=True, exist_ok=True)
    every = [MCQ_GOOD, MCQ_DUPLICATE, MCQ_LEAKED, MCQ_INFERRED]
    ws = docx_builder.build_worksheet(every, OUT / "smoke_worksheet.docx",
                                      title="Smoke Test", subtitle="fixtures only")
    docx_builder.build_answer_key(every, OUT / "smoke_worksheet_KEY.docx",
                                  title="Smoke Test", subtitle="fixtures only")
    print(f"   {ws['path']} ({ws['problems']} problems, {ws['render_failures']} equation fallbacks)")
    print(f"   {len(passing)} of {len(cases)} fixtures would reach a real worksheet")
    failures += bool(ws["render_failures"])

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{failures} CHECK(S) FAILED"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
