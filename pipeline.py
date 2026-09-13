#!/usr/bin/env python3
"""Batch runner for the video -> worksheet pipeline.

    python pipeline.py run                 # everything, end to end
    python pipeline.py extract --limit 25  # stage 1: read the boards
    python pipeline.py solve               # stage 2: answer + verify with SymPy
    python pipeline.py mcq                 # stage 3: distractors
    python pipeline.py qa                  # re-run checks without calling the API
    python pipeline.py build               # rebuild the .docx files only
    python pipeline.py reverify            # re-run SymPy checks, no API calls
    python pipeline.py index               # rebuild the SQLite index
    python pipeline.py review              # build the HTML review queue
    python pipeline.py preview             # read the questions without making a .docx
    python pipeline.py release --all       # force held questions into the build

Review the extracted statements between `extract` and `solve`, not at the end:
that is the point where a correction still changes what gets solved, and where
rejecting an item saves you paying to solve it.

Every stage writes JSON to disk and skips work that is already done, so an
interrupted run resumes where it stopped and `build` can be re-run all day
without spending anything. Use --force to redo completed items.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import config

import docx_builder
import qa
import questions as Q
import sources
import stage1_extract
import stage2_solve
import stage3_distractors
import verifier
from claude_client import ClaudeClient, RefusalError

BUILD_ID = "2026-08-16.2"   # must match config.BUILD_ID

_print_lock = threading.Lock()


def log(msg: str) -> None:
    with _print_lock:
        print(msg, flush=True)


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _run_parallel(jobs, fn, workers: int, label: str) -> tuple[int, int]:
    done = failed = 0
    total = len(jobs)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, job): job for job in jobs}
        for fut in as_completed(futures):
            job = futures[fut]
            try:
                fut.result()
                done += 1
                log(f"  [{done + failed}/{total}] {label} ok: {_job_id(job)}")
            except RefusalError as exc:
                failed += 1
                log(f"  [{done + failed}/{total}] {label} REFUSED: {_job_id(job)} - {exc}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log(f"  [{done + failed}/{total}] {label} FAILED: {_job_id(job)} - {exc}")
    return done, failed


def _job_id(job) -> str:
    """Label for the progress line. Question id where there is one, so parts of
    the same board are distinguishable rather than all printing the frame."""
    if isinstance(job, dict):
        return job.get("question_id") or job.get("video_id") or "?"
    return getattr(job, "video_id", None) or str(job)


# ==========================================================================
# Stage 1
# ==========================================================================
def _has_review(path: Path) -> bool:
    """Does this extraction carry human decisions that a re-read would destroy?"""
    if not path.exists():
        return False
    try:
        d = read_json(path)
    except Exception:
        return False
    if d.get("rejected_questions") or d.get("_review"):
        return True
    return any(q.get("_review") or q.get("statement_original")
               for q in (d.get("questions") or []))


def cmd_extract(args) -> None:
    config.ensure_dirs()
    videos = sources.discover()
    if args.limit:
        videos = videos[: args.limit]

    # --force overwrites the extraction file wholesale, taking corrected
    # statements, preserved originals and rejections with it. Model output is
    # cheap to regenerate; review time is not, so reviewed frames are protected
    # unless the discard is asked for explicitly.
    protected = []
    if args.force and not getattr(args, "discard_reviews", False):
        protected = [v for v in videos
                     if _has_review(config.EXTRACT_DIR / f"{v.video_id}.json")]
        if protected:
            videos = [v for v in videos if v not in protected]
            log(f"  {len(protected)} frame(s) carry review decisions and were skipped:")
            for v in protected[:8]:
                log(f"      {v.video_id}")
            if len(protected) > 8:
                log(f"      ... and {len(protected) - 8} more")
            log("  re-read them anyway with --discard-reviews (their corrections are lost)")

    pending = [v for v in videos
               if args.force or not (config.EXTRACT_DIR / f"{v.video_id}.json").exists()]
    log(f"stage 1 extract: {len(pending)} of {len(videos)} videos to process")
    if not pending:
        return

    client = ClaudeClient()

    def work(src):
        result = (stage1_extract.extract(client, src) if args.no_escalate
                  else stage1_extract.extract_with_escalation(client, src))
        write_json(config.EXTRACT_DIR / f"{src.video_id}.json", result)

    done, failed = _run_parallel(pending, work, args.workers, "extract")
    log(f"stage 1 done: {done} ok, {failed} failed | {client.usage.summary()}")


# ==========================================================================
# Stage 2 — solve and verify
# ==========================================================================
def load_questions(limit: int | None = None, *, quiet: bool = False) -> list[dict]:
    """Every question across every extracted frame, honouring review rejections.

    `limit` counts FRAMES, not questions -- the same thing it means for
    `extract`. Counting questions here silently truncated mid-board: a run with
    --limit 5 solved one whole frame plus four parts of the next, which on a
    sorted listing meant the four hardest questions in the set and nothing else.

    Rejection is per question: holding back part (3) of a seven-part board no
    longer discards the other six.
    """
    out, frames, rejected, orphaned = [], 0, 0, []
    for path in sorted(config.EXTRACT_DIR.glob("*.json")):
        if limit and frames >= limit:
            break
        extraction = read_json(path)
        if extraction.get("review_rejected"):
            rejected += Q.count(extraction)
            continue

        # An extraction whose frame has since left input/ is stale: usually a
        # previous run's leftovers. Processing it spends money on questions
        # you no longer have the source for, and stage 3 fails outright when
        # it tries to resend the image.
        frame = (extraction.get("_meta") or {}).get("frame", "")
        if frame and not Path(frame).exists():
            orphaned.append(path.stem)
            continue
        frames += 1
        drop = set(extraction.get("rejected_questions") or [])
        for q in Q.iter_questions(extraction):
            if q["question_id"] in drop:
                rejected += 1
                continue
            out.append(q)
    if not quiet:
        log(f"  {frames} frames -> {len(out)} questions"
            + (f" ({rejected} rejected in review)" if rejected else ""))
        if orphaned:
            log(f"  ! {len(orphaned)} stale extraction(s) skipped — their frame is no "
                f"longer in input/: {', '.join(orphaned[:4])}"
                + (" ..." if len(orphaned) > 4 else ""))
            log(f"    delete them from {config.EXTRACT_DIR.name}/ to stop seeing this")
    return out


def cmd_solve(args) -> None:
    config.ensure_dirs()
    every = load_questions(args.limit)
    pending = [q for q in every
               if args.force or not (config.SOLVE_DIR / f"{q['question_id']}.json").exists()]
    log(f"stage 2 solve: {len(pending)} of {len(every)} questions to solve")
    if not pending:
        return

    client = ClaudeClient()

    def work(question: dict):
        result = (stage2_solve.solve(client, question) if args.no_escalate
                  else stage2_solve.solve_and_verify(client, question))
        if args.no_escalate and "verification_result" not in result:
            result["verification_result"] = verifier.verify(
                result.get("verification"), config.VERIFY_TIMEOUT).as_dict()
        write_json(config.SOLVE_DIR / f"{question['question_id']}.json", result)

    done, failed = _run_parallel(pending, work, args.workers, "solve")
    _verification_summary()
    log(f"stage 2 done: {done} ok, {failed} failed | {client.usage.summary()}")


def _verification_summary() -> None:
    counts: dict[str, int] = {}
    for path in config.SOLVE_DIR.glob("*.json"):
        status = (read_json(path).get("verification_result") or {}).get("status", "missing")
        counts[status] = counts.get(status, 0) + 1
    if counts:
        total = sum(counts.values())
        parts = ", ".join(f"{k} {v} ({v*100//total}%)" for k, v in sorted(counts.items()))
        log(f"  answer verification: {parts}")


def cmd_reverify(args) -> None:
    """Re-run the symbolic checks against saved solves. No API calls."""
    config.ensure_dirs()
    changed = 0
    for path in sorted(config.SOLVE_DIR.glob("*.json")):
        data = read_json(path)
        before = (data.get("verification_result") or {}).get("status")
        result = verifier.verify(data.get("verification"), config.VERIFY_TIMEOUT)
        data["verification_result"] = result.as_dict()
        if result.status != before:
            changed += 1
            log(f"  {data.get('video_id')}: {before} -> {result.status}  {result.detail[:70]}")
        write_json(path, data)
    _verification_summary()
    log(f"reverified {len(list(config.SOLVE_DIR.glob('*.json')))} solves, {changed} changed")


# ==========================================================================
# Stage 3 — distractors
# ==========================================================================
def cmd_mcq(args) -> None:
    config.ensure_dirs()
    by_id = {q["question_id"]: q for q in load_questions(args.limit)}

    pending = []
    for qid, question in by_id.items():
        if not (config.SOLVE_DIR / f"{qid}.json").exists():
            continue
        if args.force or not (config.MCQ_DIR / f"{qid}.json").exists():
            pending.append(question)
    log(f"stage 3 mcq: {len(pending)} of {len(by_id)} questions to process")
    if not pending:
        return

    client = ClaudeClient()

    def work(question: dict):
        qid = question["question_id"]
        solution = read_json(config.SOLVE_DIR / f"{qid}.json")
        result = (stage3_distractors.generate(client, question, solution) if args.no_escalate
                  else stage3_distractors.generate_with_escalation(client, question, solution))
        write_json(config.MCQ_DIR / f"{qid}.json", result)

    done, failed = _run_parallel(pending, work, args.workers, "mcq")
    log(f"stage 3 done: {done} ok, {failed} failed | {client.usage.summary()}")


# ==========================================================================
# QA
# ==========================================================================
def cmd_qa(args) -> None:
    config.ensure_dirs()
    results, rows = [], []

    by_id = {q["question_id"]: q for q in load_questions(quiet=True)}
    paths = sorted(config.MCQ_DIR.glob("*.json"))

    def _check(path: Path):
        mcq = read_json(path)
        qid = mcq.get("question_id", path.stem)
        sol_path = config.SOLVE_DIR / f"{qid}.json"
        solution = read_json(sol_path) if sol_path.exists() else None
        return mcq, solution, qa.check_item(mcq, by_id.get(qid), solution)

    # Each check spawns a SymPy subprocess, so this is IO-bound from Python's
    # point of view and threads help even under the GIL.
    with ThreadPoolExecutor(max_workers=config.MAX_WORKERS) as pool:
        checked = list(pool.map(_check, paths))

    for mcq, solution, r in checked:
        qid = mcq.get("question_id", "?")
        results.append(r)
        check = (solution or {}).get("verification_result") or {}
        hw_label, video_no = docx_builder.parse_source(mcq.get("video_id", ""))
        rows.append({
            "question_id": qid,
            "hw": hw_label,
            "level": docx_builder.hw_series(hw_label),
            "video": video_no if video_no is not None else "",
            "verdict": "BLOCKED" if r.blocking else ("check" if r.advisory else "clean"),
            "video_id": mcq.get("video_id", ""),
            "part": mcq.get("part_label", ""),
            "status": "pass" if r.ok else "HOLD",
            "verification": check.get("status", ""),
            "check_kind": check.get("kind", ""),
            "task_explicit": (by_id.get(qid) or {}).get("task_is_explicit", ""),
            "answer_kind": mcq.get("answer_kind", ""),
            "answer": (solution or {}).get("final_answer_plain", ""),
            "confidence": mcq.get("confidence"),
            "topic": mcq.get("topic"),
            "difficulty": mcq.get("difficulty"),
            "blocking": " | ".join(r.blocking),
            "advisory": " | ".join(r.advisory),
            "warnings": " | ".join(r.warnings),
        })

    write_json(config.QA_DIR / "qa_report.json", {"results": [r.as_dict() for r in results]})

    csv_path = config.QA_DIR / "qa_report.csv"
    if rows:
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    blocked = [r for r in results if r.blocking]
    flagged = [r for r in results if not r.blocking and r.advisory]
    clean = len(results) - len(blocked) - len(flagged)

    log(f"QA ({config.HOLD_MODE} mode): {len(results)} items — "
        f"{clean} clean, {len(flagged)} to spot-check, {len(blocked)} unusable")

    from collections import Counter

    def _summarise(label, group, key):
        causes: Counter = Counter()
        for r in group:
            for e in getattr(r, key):
                causes[re.sub(r"[0-9].*", "", e).strip(" :,-")[:60] or e[:60]] += 1
        if causes:
            log(f"  {label}")
            for cause, n in causes.most_common(6):
                log(f"    {n:4}x  {cause}")

    _summarise("unusable as written (never shipped):", blocked, "blocking")
    _summarise("worth a glance (shipped, listed in the concerns report):",
               flagged, "advisory")


    # Which levels the problems are concentrated in. A rate that is uniform
    # across levels points at a threshold; one level far worse than the rest
    # points at that material.
    per_level: dict = {}
    for row in rows:
        slot = per_level.setdefault(row["level"], {"clean": 0, "check": 0, "BLOCKED": 0})
        slot[row["verdict"]] += 1
    if len(per_level) > 1:
        log("  by level:")
        log(f"    {'level':10s} {'total':>6s} {'clean':>6s} {'check':>6s} {'unusable':>9s}")
        for level in sorted(per_level, key=docx_builder.natural_key):
            c = per_level[level]
            total = sum(c.values())
            log(f"    {level:10s} {total:>6} {c['clean']:>6} {c['check']:>6} {c['BLOCKED']:>9}")

    # A short list of what to spot-check beats a long list of what was blocked.
    by_qid = {row["question_id"]: row for row in rows}
    concerns_path = config.QA_DIR / "concerns.csv"
    with concerns_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["level", "hw", "video", "question_id", "severity", "concern"])
        for r in results:
            row = by_qid.get(r.video_id, {})
            head = [row.get("level", ""), row.get("hw", ""), row.get("video", ""), r.video_id]
            for e in r.blocking:
                w.writerow(head + ["unusable", e])
            for e in r.advisory:
                w.writerow(head + ["check", e])
    log(f"  -> {concerns_path}")
    _verification_summary()
    log(f"  -> {csv_path}")


# ==========================================================================
# Build
# ==========================================================================
# Review artifacts live alongside the worksheets and must survive a normal
# build. Kept in one place because every new review document has so far been
# forgotten in the cleanup filter and silently deleted.
REVIEW_PREFIXES = ("held_", "wrong_choices_", "flagged_")


def cmd_build(args) -> None:
    config.ensure_dirs()

    holds, reasons = set(), {}
    report = config.QA_DIR / "qa_report.json"
    if report.exists():
        for r in read_json(report)["results"]:
            # Concerns are annotated in the answer key whether or not the item
            # was held, so the reason travels with the question.
            concerns = (r.get("blocking") or []) + (r.get("advisory") or [])
            if concerns:
                reasons[r["video_id"]] = concerns
            if not r["ok"]:
                holds.add(r["video_id"])

    released = _released()
    if released:
        log(f"  {len(released)} previously released question(s) included")
    holds -= released

    live = {q["question_id"] for q in load_questions(quiet=True)}
    rejected = 0
    malformed: list[str] = []
    only_malformed = getattr(args, "only_malformed", False)
    only_flagged = getattr(args, "only_flagged", False)
    only_held = getattr(args, "only_held", False)
    # "any review document" is a different question from "the held view", and
    # conflating them made the --only-flagged branch unreachable.
    review_mode = only_held or only_malformed or only_flagged

    # A QA report older than the newest generated item means holds are unknown
    # for anything produced since — usually an interrupted `run` that never
    # reached the qa step.
    mcq_files = list(config.MCQ_DIR.glob("*.json"))
    if mcq_files and report.exists():
        newest = max(f.stat().st_mtime for f in mcq_files)
        if newest > report.stat().st_mtime + 5:
            log("  ! the QA report is older than some questions — run "
                "`python pipeline.py qa` first, or holds will be missed")
    elif mcq_files and not report.exists():
        log("  ! no QA report yet — run `python pipeline.py qa` first")

    items = []
    for path in sorted(config.MCQ_DIR.glob("*.json")):
        mcq = read_json(path)
        qid = mcq.get("question_id", path.stem)
        if qid not in live:
            rejected += 1
            continue
        wrong_count = len(mcq.get("distractors") or []) != 3

        # Exactly what keeps a question off the worksheet: a blocking QA error
        # that has not been released, or the wrong number of answer choices.
        # Advisory concerns do NOT exclude anything in flag mode -- those items
        # ship, carrying a CHECK note, and belong in --only-flagged instead.
        excluded = (qid in holds) or wrong_count

        if only_malformed:
            if not wrong_count:
                continue
        elif only_flagged:
            if excluded or qid not in reasons:
                continue
        elif only_held:
            if not excluded:
                continue
        elif excluded and not args.include_failures:
            if wrong_count:
                malformed.append(qid)
            continue

        if args.topic and args.topic.lower() not in (mcq.get("topic") or "").lower():
            continue
        if args.difficulty and mcq.get("difficulty") != args.difficulty:
            continue
        if getattr(args, "hw", None):
            hw, _ = docx_builder.parse_source(mcq.get("video_id", ""))
            # Substring, so --hw BASIC selects a track and --hw 5HW a series.
            if args.hw.upper() not in hw.upper():
                continue
        items.append(mcq)

    if not items:
        log("nothing to build"
            + (" — every item has 4 answer choices" if only_malformed
               else " — nothing shipped with a concern" if only_flagged
               else " — nothing was excluded from the worksheets" if review_mode
               else " (check the QA report, or pass --include-failures)"))
        return

    items = docx_builder.sort_items(items, args.sort)

    if review_mode:
        if only_malformed:
            prefix, heading = "wrong_choices", "Wrong Number of Answer Choices"
        elif only_flagged:
            prefix, heading = "flagged", "Shipped, Worth Checking"
        else:
            prefix, heading = "held", "Excluded from the Worksheets"
        # Scope the cleanup to what this run will regenerate, so
        # --hw 2HW does not delete the other levels' documents.
        stale = [q for q in config.DOCX_DIR.glob(f"{prefix}_*.docx")
                 if not args.hw
                 or args.hw.upper().replace(" ", "_") in q.name.upper()]
        for old in stale:
            old.unlink()
        written = docx_builder.build_held(items, config.DOCX_DIR, reasons=reasons,
                                          prefix=prefix, heading=heading)

    elif args.layout in ("hw", "combined"):
        # With --hw, leave other series' documents alone: rebuilding just 2HW
        # must not delete the 1HW files it was never going to regenerate.
        stale = [q for q in config.DOCX_DIR.glob("*.docx")
                 if not q.name.startswith(REVIEW_PREFIXES + ("worksheet_",))
                 and (not args.hw
                      or args.hw.upper().replace(" ", "_") in q.name.upper())]
        for old in stale:
            old.unlink()
        if args.layout == "combined":
            written = docx_builder.build_combined(
                items, config.DOCX_DIR, include_key=not args.no_answer_key,
                split_series=not args.one_file)
        else:
            written = docx_builder.build_by_hw(
                items, config.DOCX_DIR, include_key=not args.no_answer_key)

    else:
        stale = sorted(config.DOCX_DIR.glob("worksheet_set*.docx"))
        for old in stale:
            old.unlink()
        written = docx_builder.build_all(
            items, config.DOCX_DIR, per_worksheet=args.per_worksheet,
            title=args.title, prefix="worksheet", reasons=reasons)

    if stale:
        log(f"  replaced {len(stale)} previously generated file(s)")

    failures = sum(w.get("render_failures", 0) for w in written)
    if malformed:
        log(f"  ! {len(malformed)} item(s) skipped: not exactly 4 answer choices "
            f"({', '.join(malformed[:3])}{' ...' if len(malformed) > 3 else ''})")

    if only_flagged:
        log(f"built {len(written)} file(s), one per level, from {len(items)} "
            f"question(s) that shipped but carry a concern")
    elif only_malformed:
        log(f"built {len(written)} file(s), one per level, from {len(items)} "
            f"question(s) whose answer-choice count is not 4")
        log("  regenerate these rather than releasing them: delete their files from")
        log(f"  {config.MCQ_DIR.name}/ and re-run `python pipeline.py mcq`")
    elif only_held:
        log(f"built {len(written)} file(s), one per level, from {len(items)} "
            f"question(s) excluded from the worksheets")
    elif args.layout == "combined" and written:
        total_hw = sum(w.get("homeworks", 0) for w in written)
        log(f"built {len(written)} file(s) covering {total_hw} homework(s), "
            f"{len(items)} questions")
    else:
        log(f"built {len(written)} files from {len(items)} questions "
            f"({len(holds)} held by QA, {rejected} rejected in review)")
    if failures:
        log(f"  ! {failures} equations fell back to literal LaTeX; search the docs for '['")
    for w in written:
        log(f"  -> {w['path']}")


# ==========================================================================
# Releasing held questions
# ==========================================================================
RELEASED_FILE = "released.json"


def _released() -> set:
    path = config.QA_DIR / RELEASED_FILE
    if not path.exists():
        return set()
    try:
        return set(read_json(path).get("question_ids") or [])
    except Exception:
        return set()


def _write_released(ids: set) -> None:
    write_json(config.QA_DIR / RELEASED_FILE, {"question_ids": sorted(ids)})


def cmd_release(args) -> None:
    """Force held questions into the build, individually or all at once.

    A hold means a check failed, not that you agree with the check. After
    reading the held items you may decide most are fine; releasing records that
    judgement so it survives every later rebuild. The concern is still printed
    in the answer key, so a released item is never silently laundered.
    """
    config.ensure_dirs()
    current = _released()

    if args.list:
        if not current:
            log("nothing released")
            return
        log(f"{len(current)} released question(s):")
        for qid in sorted(current, key=docx_builder.natural_key):
            log(f"  {qid}")
        return

    if args.clear:
        _write_released(set())
        log(f"cleared {len(current)} release(s) — held items are held again")
        return

    report = config.QA_DIR / "qa_report.json"
    if not report.exists():
        log("no QA report yet — run `pipeline.py qa` first")
        return
    held = {r["video_id"] for r in read_json(report)["results"] if not r["ok"]}

    if args.hw:
        selected = set()
        for qid in held:
            path = config.MCQ_DIR / f"{qid}.json"
            vid = read_json(path).get("video_id", "") if path.exists() else qid
            label, _ = docx_builder.parse_source(vid)
            if args.hw.upper() in label.upper():
                selected.add(qid)
        current |= selected
        _write_released(current)
        log(f"released {len(selected)} held question(s) matching {args.hw!r}")
    elif args.all:
        current |= held
        _write_released(current)
        log(f"released all {len(held)} held question(s)")
    elif args.question_ids:
        unknown = [q for q in args.question_ids if q not in held]
        current |= {q for q in args.question_ids if q in held}
        _write_released(current)
        log(f"released {len(args.question_ids) - len(unknown)} question(s)")
        for q in unknown:
            log(f"  ! {q} is not currently held — ignored")
    else:
        log("give question ids, or --all, or --hw LEVEL, or --list, or --clear")
        return

    log("run `python pipeline.py build` to regenerate with these included")


# ==========================================================================
# Preview finished questions without generating a document
# ==========================================================================
def cmd_preview(args) -> None:
    """Print the finished multiple-choice items to the terminal.

    Judging whether the questions are any good is a separate job from deciding
    how they should look on paper, and needing Word for the first one slows the
    part that matters. Lettering matches what `build` would produce.
    """
    import docx_builder

    holds, reasons = set(), {}
    report = config.QA_DIR / "qa_report.json"
    if report.exists():
        for r in read_json(report)["results"]:
            # Concerns are annotated in the answer key whether or not the item
            # was held, so the reason travels with the question.
            concerns = (r.get("blocking") or []) + (r.get("advisory") or [])
            if concerns:
                reasons[r["video_id"]] = concerns
            if not r["ok"]:
                holds.add(r["video_id"])

    items = []
    for path in sorted(config.MCQ_DIR.glob("*.json")):
        mcq = read_json(path)
        qid = mcq.get("question_id", path.stem)
        if args.held and qid not in holds:
            continue
        if not args.held and qid in holds and not args.include_failures:
            continue
        if args.difficulty and mcq.get("difficulty") != args.difficulty:
            continue
        if args.topic and args.topic.lower() not in (mcq.get("topic") or "").lower():
            continue
        if getattr(args, "hw", None):
            hw, _ = docx_builder.parse_source(mcq.get("video_id", ""))
            # Substring, so --hw BASIC selects a track and --hw 5HW a series.
            if args.hw.upper() not in hw.upper():
                continue
        items.append(mcq)

    if not items:
        log("nothing to preview (try --include-failures, or relax --difficulty/--topic)")
        return

    items = docx_builder.sort_items(items, "natural")
    for n, mcq in enumerate(items[: args.limit], 1):
        check = (mcq.get("_meta") or {}).get("verification_status", "?")
        hw_label, video_no = docx_builder.parse_source(mcq.get("video_id", ""))
        log("")
        log(f"{n}. {hw_label} video {video_no}  [{mcq.get('question_id')}]  "
            f"{mcq.get('topic')} · {mcq.get('difficulty')} · answer {check}")
        log(f"   {mcq.get('question_stem')}")
        for opt in docx_builder.assign_labels(mcq):
            mark = " <- correct" if opt["is_correct"] else ""
            log(f"     {opt['label']}. {opt['latex']}{mark}")
        if args.why:
            for opt in docx_builder.assign_labels(mcq):
                if not opt["is_correct"]:
                    log(f"        {opt['label']}: [{opt.get('misconception')}] "
                        f"{opt.get('student_reasoning','')}")
        n_opts = 1 + len(mcq.get("distractors") or [])
        if n_opts != 4:
            log(f"     ! {n_opts} answer choices, not 4 — this item is excluded from build")
        for reason in reasons.get(mcq.get("question_id"), []):
            log(f"     HELD: {reason}")
        for flag in mcq.get("review_flags", []):
            log(f"     ! {flag}")

    shown = min(len(items), args.limit)
    total = len(items) + len(holds)
    log("")
    if args.held:
        log(f"showing {shown} of {len(items)} held item(s)")
    else:
        log(f"showing {shown} of {len(items)} that passed QA"
            + (f"; {len(holds)} of {total} were held" if holds else ""))

    # A hold rate this high is never 564 separate problems -- it is one rule
    # firing on everything, and the grouped causes will name it in one line.
    if holds and total and len(holds) / total > 0.5:
        log("")
        log(f"  ! {100*len(holds)//total}% of items are held, which points at one")
        log("    systematic cause rather than many separate ones. To see it:")
        log("        python pipeline.py qa            # groups the causes")
        log("        python pipeline.py preview --held --why")
    log("")
    log("Nothing here costs anything to regenerate — `build` reads these same files.")


# ==========================================================================
# Input inspection
# ==========================================================================
def cmd_inputs(args) -> None:
    """List what would be processed, and flag anything that would corrupt a run.

    Worth running once before pointing this at thousands of files.
    """
    found = sources.discover()
    if not found:
        log("nothing found — check MW_INPUT and the file extensions")
        return

    problems = sources.audit(found)
    log("")
    for src in found[: args.show]:
        size = src.frame.stat().st_size / 1024 if src.frame.exists() else 0
        log(f"  {src.video_id:44s} {size:7.0f} KB  {src.frame.suffix}")
    if len(found) > args.show:
        log(f"  ... and {len(found) - args.show} more")

    log("")
    if problems:
        log(f"{len(problems)} problem(s) found — fix these before a full run:")
        for pr in problems:
            log(f"  ! {pr}")
    else:
        log(f"{len(found)} frames, no problems found. Ready to extract.")


# ==========================================================================
# Cost projection
# ==========================================================================
def cmd_estimate(args) -> None:
    """Project the full-run cost from what the pilot actually spent.

    Token counts are the least predictable part of this pipeline -- multi-part
    boards emit far more output than single-question ones, and escalation rates
    depend entirely on how clean your handwriting is. Measure, do not guess.
    """
    import claude_client

    frames = len(list(config.EXTRACT_DIR.glob("*.json")))
    questions = len(load_questions(quiet=True))
    solved = len(list(config.SOLVE_DIR.glob("*.json")))
    items = len(list(config.MCQ_DIR.glob("*.json")))

    if not frames:
        log("nothing to measure yet — run `pipeline.py extract` first")
        return

    per_frame = questions / frames
    log(f"pilot: {frames} frames -> {questions} questions ({per_frame:.1f} per frame), "
        f"{solved} solved, {items} items built")

    # Escalation rate, read straight off the saved output. Every escalated
    # result carries _meta.escalated_from, so this needs no extra calls and is
    # the number that decides whether escalation is worth what it costs.
    log("")
    log("escalation (each escalated item costs roughly 3x a clean one):")
    for label, folder, reasons in (
            ("stage 1 extract", config.EXTRACT_DIR, True),
            ("stage 2 solve",   config.SOLVE_DIR,   False),
            ("stage 3 mcq",     config.MCQ_DIR,     False)):
        paths = sorted(folder.glob("*.json"))
        if not paths:
            continue
        esc, why = 0, {}
        second_opinions = 0
        for path in paths:
            d = read_json(path)
            meta = d.get("_meta") or {}
            if "escalated_from" in meta:
                esc += 1
                r = (meta["escalated_from"] or {}).get("reason", "low confidence")
                why[r.split(":")[0][:40]] = why.get(r.split(":")[0][:40], 0) + 1
            if d.get("second_opinion"):
                second_opinions += 1
        pct = 100 * esc / len(paths)
        log(f"  {label:16s} {esc:4}/{len(paths):<4} ({pct:3.0f}%)")
        if second_opinions:
            log(f"  {'':16s} {second_opinions:4} more got a second solve (no symbolic check applied)")
        if reasons:
            for r, n in sorted(why.items(), key=lambda kv: -kv[1])[:4]:
                log(f"  {'':18s}{n:3}x  {r}")

    log("")
    log("To see the floor, rerun the same frames into a separate folder with")
    log("escalation off, and compare the '~$' lines:")
    log("    $env:MW_OUTPUT = \"output_noesc\"")
    log(f"    python pipeline.py extract --limit {frames} --no-escalate")
    log("")
    log("To project a full run, take the '~$X' totals printed by each stage and:")
    log(f"    stage 1  cost/{frames} frames    x  {args.frames:,} frames")
    log(f"    stage 2  cost/{max(solved,1)} questions x  {int(args.frames*per_frame):,} questions")
    log(f"    stage 3  cost/{max(items,1)} questions x  {int(args.frames*per_frame):,} questions")
    log("")
    log("Halve the total if you use batch_pipeline.py. Escalation is the biggest")
    log("swing: set --no-escalate on a second pilot to see the floor.")


# ==========================================================================
# Index and review
# ==========================================================================
def cmd_index(args) -> None:
    import db
    counts = db.build(db.connect())
    log("indexed: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
    log(f"  -> {db.DB_PATH}")


def cmd_review(args) -> None:
    import db
    import review
    db.build(db.connect())
    rows = review.gather(args.limit, False, args.min_risk)
    if not rows:
        log("nothing flagged for review")
        return
    out = review.build_html(rows, review.REVIEW_HTML)
    risky = sum(1 for r in rows if r.get("risk", 0) >= 50)
    log(f"{len(rows)} items queued for review ({risky} at risk >= 50)")
    log(f"  -> {out}")


# ==========================================================================
# Everything
# ==========================================================================
def cmd_run(args) -> None:
    cmd_extract(args)
    cmd_solve(args)
    cmd_mcq(args)
    cmd_qa(args)
    cmd_build(args)
    cmd_index(args)


# ==========================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--limit", type=int,
                       help="process at most N FRAMES (all their questions) — for pilot runs")
        p.add_argument("--force", action="store_true", help="redo items that already have output")
        p.add_argument("--workers", type=int, default=config.MAX_WORKERS)
        p.add_argument("--no-escalate", action="store_true",
                       help="never retry low-confidence items on the stronger model")
        p.add_argument("--discard-reviews", action="store_true",
                       help="with --force, re-read frames even if that destroys review decisions")

    def build_opts(p):
        p.add_argument("--per-worksheet", type=int, default=config.PROBLEMS_PER_WORKSHEET,
                       help="problems per file; only applies to --layout sets")
        p.add_argument("--title", default=config.COURSE_TITLE)
        p.add_argument("--topic", help="only include problems whose topic contains this string")
        p.add_argument("--difficulty", choices=["easy", "medium", "hard"])
        p.add_argument("--include-failures", action="store_true",
                       help="build items that failed QA too, mixed in with the rest")
        p.add_argument("--only-held", action="store_true",
                       help="build ONLY questions kept OFF the worksheets, annotated with why")
        p.add_argument("--only-flagged", action="store_true",
                       help="build ONLY questions that DID ship but carry a concern "
                            "(flagged_*.docx)")
        p.add_argument("--only-malformed", action="store_true",
                       help="build ONLY items whose answer-choice count is not 4 "
                            "(wrong_choices_*.docx), for regenerating them")
        p.add_argument("--layout", default=config.DOCX_LAYOUT,
                       choices=["combined", "hw", "sets"],
                       help="combined (default): one file per course series and "
                            "track, each homework starting on a new page. "
                            "hw: a separate file per homework number. "
                            "sets: fixed-size worksheets with separate KEY files.")
        p.add_argument("--hw", help="only homeworks whose label contains this, e.g. "
                                    "2HW, 2HW15, BASIC, or '5HW24 BASIC'")
        p.add_argument("--one-file", action="store_true",
                       help="with --layout combined, put every series in one document "
                            "instead of one per series")
        p.add_argument("--no-answer-key", action="store_true",
                       help="omit the answer key page, for the copy students get")
        p.add_argument("--sort", default="natural",
                       choices=["natural", "difficulty", "topic"],
                       help="natural reads numbers in the id, so HW9 < HW25 and practice 2 < 10")

    p = sub.add_parser("extract", help="stage 1: frame -> problem JSON")
    common(p); p.set_defaults(func=cmd_extract)

    p = sub.add_parser("solve", help="stage 2: problem -> answer, verified with SymPy")
    common(p); p.set_defaults(func=cmd_solve)

    p = sub.add_parser("mcq", help="stage 3: verified answer -> multiple-choice JSON")
    common(p); p.set_defaults(func=cmd_mcq)

    p = sub.add_parser("reverify", help="re-run SymPy checks on saved solves (no API calls)")
    p.set_defaults(func=cmd_reverify)

    p = sub.add_parser("qa", help="run deterministic checks over stage 2 output")
    p.set_defaults(func=cmd_qa)

    p = sub.add_parser("build", help="generate worksheets and answer keys")
    build_opts(p); p.set_defaults(func=cmd_build)

    p = sub.add_parser("release", help="force held questions into the build")
    p.add_argument("question_ids", nargs="*", help="question ids to release")
    p.add_argument("--all", action="store_true", help="release every currently held question")
    p.add_argument("--hw", help="release held questions in this homework, series or track")
    p.add_argument("--list", action="store_true", help="show what is already released")
    p.add_argument("--clear", action="store_true", help="undo all releases")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("preview", help="print finished questions to the terminal, no Word file")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--why", action="store_true", help="also show the misconception behind each distractor")
    p.add_argument("--held", action="store_true", help="show only items QA held, with the reason")
    p.add_argument("--hw", help="only homeworks whose label contains this, e.g. 5HW or BASIC")
    p.add_argument("--difficulty", choices=["easy", "medium", "hard"])
    p.add_argument("--topic")
    p.add_argument("--include-failures", action="store_true")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("inputs", help="list input frames and flag naming problems")
    p.add_argument("--show", type=int, default=15)
    p.set_defaults(func=cmd_inputs)

    p = sub.add_parser("estimate", help="project full-run cost from the pilot")
    p.add_argument("--frames", type=int, default=3000)
    p.set_defaults(func=cmd_estimate)

    p = sub.add_parser("index", help="(re)build the SQLite index over all JSON output")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("review", help="build the HTML review queue for extracted statements")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--min-risk", type=int, default=1)
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("run", help="extract, solve, mcq, qa, build, index")
    common(p); build_opts(p); p.set_defaults(func=cmd_run)

    args = ap.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
