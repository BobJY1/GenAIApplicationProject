#!/usr/bin/env python3
"""Run every CLI subcommand that needs no API access, and fail loudly.

    python cli_test.py

`smoke_test.py` checks the libraries — conversion, verification, QA logic,
document generation. It never invokes the command line, so a command could
raise NameError on its first line and every library test would still pass.
That happened four times: an edit matched in two functions, one of which had no
such variable, and only running the command surfaced it.

This builds a throwaway workspace in a temp directory, populates it with
fixtures, and runs each offline command with a spread of flag combinations.
Nothing here touches the network or your real output.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent

# Every offline command, with the flag combinations that have broken before.
CASES = [
    ("inputs",   ["inputs"]),
    ("qa",       ["qa"]),
    ("preview",  ["preview"]),
    ("preview --why", ["preview", "--why"]),
    ("preview --held", ["preview", "--held"]),
    ("preview --held --why --hw", ["preview", "--held", "--why", "--hw", "1HW"]),
    ("preview --difficulty", ["preview", "--difficulty", "easy"]),
    ("preview --include-failures", ["preview", "--include-failures"]),
    ("build",    ["build"]),
    ("build --layout hw", ["build", "--layout", "hw"]),
    ("build --layout sets", ["build", "--layout", "sets"]),
    ("build --layout combined --one-file", ["build", "--layout", "combined", "--one-file"]),
    ("build --hw", ["build", "--hw", "1HW"]),
    ("build --only-held", ["build", "--only-held"]),
    ("build --only-malformed", ["build", "--only-malformed"]),
    ("build --only-flagged", ["build", "--only-flagged"]),
    ("build --only-held --hw", ["build", "--only-held", "--hw", "1HW"]),
    ("build --no-answer-key", ["build", "--no-answer-key"]),
    ("build --sort difficulty", ["build", "--sort", "difficulty"]),
    ("build --include-failures", ["build", "--include-failures"]),
    ("release --list", ["release", "--list"]),
    ("release --all", ["release", "--all"]),
    ("release --hw", ["release", "--hw", "1HW"]),
    ("release --clear", ["release", "--clear"]),
    ("reverify",  ["reverify"]),
    ("index",     ["index"]),
    ("review",    ["review"]),
    ("estimate",  ["estimate"]),
]

DB_CASES = [("db build", ["build"]), ("db stats", ["stats"]), ("db queue", ["queue"])]


def fixtures(root: Path) -> None:
    """Two levels, one clean item, one held, one with the wrong option count."""
    sys.path.insert(0, str(root))
    for mod in ("config", "questions", "smoke_test", "docx_builder", "qa", "pipeline"):
        sys.modules.pop(mod, None)
    import config, questions as Q, smoke_test as st
    config.ensure_dirs()

    frame = next(config.INPUT_DIR.glob("*.png"), None)
    plan = [("Copy of 1HW25 ZOOM 2025 PRACTICE 1", "clean"),
            ("Copy of 1HW25 ZOOM 2025 PRACTICE 2", "duplicate"),
            ("Copy of 5HW24 BASIC ZOOM 2025 PRACTICE 1", "extra_option")]

    for vid, kind in plan:
        (config.EXTRACT_DIR / f"{vid}.json").write_text(json.dumps({
            "video_id": vid, "board_transcription": "", "shared_context": "",
            "questions": [{"part_label": "", "statement": "s", "task_is_explicit": True,
                           "task_inference_basis": "", "answer_kind": "expression",
                           "topic": "algebra", "difficulty": "easy",
                           "confidence": 0.95, "notes": ""}],
            "board_shows_solution": False, "has_diagram": False, "diagram_description": "",
            "legibility": "clear", "unreadable_items": [], "confidence": 0.95, "notes": "",
            "_meta": {"stage": "extract", "frame": str(frame) if frame else ""}}))

        qid = Q.question_id(vid, 1)
        m = json.loads(json.dumps(st.MCQ_GOOD))
        m["question_id"], m["video_id"] = qid, vid
        m["_meta"] = {"verification_status": "verified"}
        if kind == "duplicate":
            m["distractors"] = [dict(m["distractors"][0], latex="-0.75", plain="-0.75")] \
                               + m["distractors"][1:]
        elif kind == "extra_option":
            m["distractors"] = m["distractors"] + [dict(
                latex="99", plain="99", misconception="sign_error",
                student_reasoning="r", plausibility=0.5)]
        (config.MCQ_DIR / f"{qid}.json").write_text(json.dumps(m))
        (config.SOLVE_DIR / f"{qid}.json").write_text(json.dumps(dict(
            st.SOLUTION_GOOD, question_id=qid, video_id=vid,
            verification_result={"status": "verified", "detail": "", "kind": "k"})))


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="mw_cli_"))
    root = work / "math-worksheets"
    shutil.copytree(HERE, root, ignore=shutil.ignore_patterns(
        "__pycache__", "output", "examples", "*.zip"))
    shutil.rmtree(root / "output", ignore_errors=True)

    fixtures(root)
    failures = []

    for label, argv in CASES:
        r = subprocess.run([sys.executable, "pipeline.py", *argv],
                           cwd=root, capture_output=True, text=True, timeout=300)
        ok = r.returncode == 0 and "Traceback" not in r.stderr
        print(f"  {'ok  ' if ok else 'FAIL'} pipeline.py {label}")
        if not ok:
            failures.append(label)
            tail = [l for l in r.stderr.strip().splitlines() if l.strip()][-2:]
            for line in tail:
                print(f"        {line}")

    for label, argv in DB_CASES:
        r = subprocess.run([sys.executable, "db.py", *argv],
                           cwd=root, capture_output=True, text=True, timeout=120)
        ok = r.returncode == 0 and "Traceback" not in r.stderr
        print(f"  {'ok  ' if ok else 'FAIL'} {label}")
        if not ok:
            failures.append(label)
            for line in r.stderr.strip().splitlines()[-2:]:
                print(f"        {line}")

    shutil.rmtree(work, ignore_errors=True)
    print()
    if failures:
        print(f"{len(failures)} COMMAND(S) FAILED: {', '.join(failures)}")
        return 1
    print(f"ALL {len(CASES) + len(DB_CASES)} COMMANDS RAN CLEANLY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
