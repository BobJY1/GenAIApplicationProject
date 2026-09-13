#!/usr/bin/env python3
"""The Message Batches API path. Same two stages, half the price.

At 3,000 videos the synchronous path spends hours babysitting rate limits for
results nobody is waiting on. The Batches API bills every token at 50% and most
batches finish inside an hour.

    python batch_pipeline.py submit  extract
    python batch_pipeline.py status  extract      # progress, without blocking
    python batch_pipeline.py collect extract --wait
    python batch_pipeline.py submit  solve
    python batch_pipeline.py collect solve --wait    # runs the SymPy checks too
    python batch_pipeline.py submit  mcq
    python batch_pipeline.py collect mcq --wait

then `python pipeline.py qa` and `python pipeline.py build` as usual.

Escalation does not happen inside a batch: a batch is submitted all at once, so
nothing can be rerun on a stronger model mid-flight. Collect, then re-solve the
refuted items on the synchronous path, which is a small slice:

    python batch_pipeline.py resolve-refuted

Two limits shape the chunking below. A batch caps at 100,000 requests or 256 MB,
whichever comes first, and base64 whiteboard frames blow through 256 MB long
before 100,000 requests. Stage 1 is therefore chunked by payload size.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import anthropic

import config
import prompts
import questions as Q
import schemas
import sources
import stage1_extract
import stage2_solve
import stage3_distractors
import verifier
from claude_client import Usage, build_params, make_client, parse_json_response

STATE_DIR = config.OUTPUT_DIR / "batches"

# Stay well under the hard 256 MB ceiling; the JSON envelope adds overhead on
# top of the base64 payload we can measure.
MAX_BATCH_BYTES = 180 * 1024 * 1024
MAX_BATCH_REQUESTS = 5000

_CUSTOM_ID_OK = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def to_custom_id(video_id: str) -> str:
    """custom_id must match ^[a-zA-Z0-9_-]{1,64}$, and video ids often do not."""
    cid = re.sub(r"[^a-zA-Z0-9_-]", "-", video_id)[:64]
    return cid if _CUSTOM_ID_OK.match(cid) else "v-" + str(abs(hash(video_id)))[:20]


def _chunk(requests: list[dict]) -> list[list[dict]]:
    chunks, current, size = [], [], 0
    for req in requests:
        req_size = len(json.dumps(req["params"], ensure_ascii=False).encode())
        if current and (size + req_size > MAX_BATCH_BYTES or len(current) >= MAX_BATCH_REQUESTS):
            chunks.append(current)
            current, size = [], 0
        current.append(req)
        size += req_size
    if current:
        chunks.append(current)
    return chunks


def _all_questions(limit: int | None = None) -> list[dict]:
    """Every question across every extracted frame, minus review rejections.

    `limit` counts FRAMES, matching `pipeline.py`. Counting questions here would
    truncate mid-board and mean two different things depending on which path you
    ran.
    """
    out, frames, orphaned = [], 0, 0
    for path in sorted(config.EXTRACT_DIR.glob("*.json")):
        if limit and frames >= limit:
            break
        d = json.loads(path.read_text(encoding="utf-8"))
        if d.get("review_rejected"):
            continue
        # Skip extractions whose frame has left input/, as pipeline.py does.
        frame = (d.get("_meta") or {}).get("frame", "")
        if frame and not Path(frame).exists():
            orphaned += 1
            continue
        frames += 1
        drop = set(d.get("rejected_questions") or [])
        out += [q for q in Q.iter_questions(d) if q["question_id"] not in drop]
    if orphaned:
        print(f"  ! {orphaned} stale extraction(s) skipped — frame no longer in input/")
    return out


def _save_state(stage: str, batch_ids: list[str], id_map: dict) -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / f"{stage}.json"
    path.write_text(json.dumps(
        {"stage": stage, "batch_ids": batch_ids, "id_map": id_map,
         "submitted_at": time.time()}, indent=2), encoding="utf-8")
    return path


def _load_state(stage: str) -> dict:
    path = STATE_DIR / f"{stage}.json"
    if not path.exists():
        raise SystemExit(f"no submitted batch for stage {stage!r} (looked in {path})")
    return json.loads(path.read_text(encoding="utf-8"))


# ==========================================================================
# Submit
# ==========================================================================
def submit(stage: str, limit: int | None, force: bool) -> None:
    client = make_client(for_batches=True)
    requests, id_map = [], {}

    if stage == "extract":
        videos = sources.discover()
        if limit:
            videos = videos[:limit]
        for src in videos:
            if not force and (config.EXTRACT_DIR / f"{src.video_id}.json").exists():
                continue
            cid = to_custom_id(src.video_id)
            id_map[cid] = src.video_id
            requests.append({
                "custom_id": cid,
                "params": build_params(
                    for_batches=True,
                    model=config.EXTRACT_MODEL,
                    system=prompts.EXTRACT_SYSTEM,
                    content=stage1_extract.build_content(src),
                    schema=schemas.EXTRACTION_SCHEMA,
                    max_tokens=config.EXTRACT_MAX_TOKENS,
                    effort=config.EXTRACT_EFFORT,
                ),
            })

    elif stage == "solve":
        for question in _all_questions(limit):
            qid = question["question_id"]
            if not force and (config.SOLVE_DIR / f"{qid}.json").exists():
                continue
            cid = to_custom_id(qid)
            id_map[cid] = qid
            requests.append({
                "custom_id": cid,
                "params": build_params(
                    for_batches=True,
                    model=config.SOLVE_MODEL,
                    system=prompts.SOLVE_SYSTEM,
                    content=stage2_solve.build_content(question),
                    schema=schemas.SOLVE_SCHEMA,
                    max_tokens=config.SOLVE_MAX_TOKENS,
                    effort=config.SOLVE_EFFORT,
                ),
            })

    elif stage == "mcq":
        for question in _all_questions(limit):
            qid = question["question_id"]
            sol_path = config.SOLVE_DIR / f"{qid}.json"
            if not sol_path.exists():
                continue
            if not force and (config.MCQ_DIR / f"{qid}.json").exists():
                continue
            solution = json.loads(sol_path.read_text(encoding="utf-8"))
            cid = to_custom_id(qid)
            id_map[cid] = qid
            requests.append({
                "custom_id": cid,
                "params": build_params(
                    for_batches=True,
                    model=config.MCQ_MODEL,
                    system=prompts.MCQ_SYSTEM,
                    content=stage3_distractors.build_content(
                        question, solution, resend_frame=bool(question.get("has_diagram"))),
                    schema=schemas.MCQ_SCHEMA,
                    max_tokens=config.MCQ_MAX_TOKENS,
                    effort=config.MCQ_EFFORT,
                ),
            })
    else:
        raise SystemExit(f"unknown stage {stage!r}")

    if not requests:
        print("nothing to submit")
        return

    chunks = _chunk(requests)
    print(f"{len(requests)} requests across {len(chunks)} batch(es)")

    batch_ids = []
    for i, chunk in enumerate(chunks, 1):
        batch = client.messages.batches.create(requests=chunk)
        batch_ids.append(batch.id)
        print(f"  batch {i}/{len(chunks)}: {batch.id} ({len(chunk)} requests)")

    path = _save_state(stage, batch_ids, id_map)
    print(f"state written to {path}")
    print(f"run: python batch_pipeline.py collect {stage}")


# ==========================================================================
# Collect
# ==========================================================================
def collect(stage: str, poll_seconds: int, wait: bool) -> None:
    client = make_client(for_batches=True)
    state = _load_state(stage)
    id_map = state["id_map"]
    out_dir = {"extract": config.EXTRACT_DIR, "solve": config.SOLVE_DIR,
               "mcq": config.MCQ_DIR}[stage]
    out_dir.mkdir(parents=True, exist_ok=True)

    usage = Usage()
    ok = errored = expired = 0

    for batch_id in state["batch_ids"]:
        while True:
            batch = client.messages.batches.retrieve(batch_id)
            if batch.processing_status == "ended":
                break
            if not wait:
                print(f"{batch_id}: still {batch.processing_status}, "
                      f"{batch.request_counts}. Re-run later or pass --wait.")
                return
            print(f"{batch_id}: {batch.processing_status} {batch.request_counts}")
            time.sleep(poll_seconds)

        for result in client.messages.batches.results(batch_id):
            vid = id_map.get(result.custom_id, result.custom_id)
            rtype = result.result.type

            if rtype != "succeeded":
                if rtype == "expired":
                    expired += 1
                else:
                    errored += 1
                print(f"  {vid}: {rtype}")
                continue

            message = result.result.message
            try:
                data = parse_json_response(message)
            except Exception as exc:  # noqa: BLE001
                errored += 1
                print(f"  {vid}: unparseable - {exc}")
                continue

            usage.add(message.model, message.usage, batch=True)
            data["video_id"] = vid
            data["_meta"] = {
                "stage": stage,
                "model": message.model,
                "prompt_version": prompts.PROMPT_VERSION,
                "batch_id": batch_id,
                "via": "message_batches_api",
            }
            if stage == "extract":
                folder = config.INPUT_DIR / vid
                data["_meta"]["frame"] = str(next(
                    (folder / n for n in config.FRAME_NAMES if (folder / n).exists()), ""))
            elif stage == "solve":
                data["question_id"] = vid
                data["video_id"] = Q.split_question_id(vid)[0]
                # The batch only produced the claim; run the check here.
                data["verification_result"] = verifier.verify(
                    data.get("verification"), config.VERIFY_TIMEOUT).as_dict()

            (out_dir / f"{vid}.json").write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            ok += 1

    print(f"\n{stage}: {ok} written, {errored} errored, {expired} expired")
    print(usage.summary() + "  (batch pricing)")

    if stage == "solve":
        counts: dict[str, int] = {}
        for path in config.SOLVE_DIR.glob("*.json"):
            st = (json.loads(path.read_text(encoding="utf-8")).get("verification_result")
                  or {}).get("status", "missing")
            counts[st] = counts.get(st, 0) + 1
        total = sum(counts.values()) or 1
        print("answer verification: " + ", ".join(
            f"{k} {v} ({v*100//total}%)" for k, v in sorted(counts.items())))
        if counts.get(verifier.REFUTED):
            print(f"-> {counts[verifier.REFUTED]} answers were refuted; "
                  f"run `python batch_pipeline.py resolve-refuted`")
    if errored or expired:
        print("re-run `submit` to retry the missing ones; completed items are skipped")


# ==========================================================================
# Status
# ==========================================================================
def status(stage: str) -> None:
    """Progress on a submitted batch, without blocking.

    Results cannot be retrieved until a batch ends, so `succeeded` staying at
    zero for a while is normal -- the counts move as individual requests land,
    but nothing is downloadable until the whole batch is done.
    """
    client = make_client(for_batches=True)
    state = _load_state(stage)
    submitted = state.get("submitted_at")

    for batch_id in state["batch_ids"]:
        try:
            batch = client.messages.batches.retrieve(batch_id)
        except Exception as exc:
            print(f"{batch_id}: could not retrieve — {type(exc).__name__}: {str(exc)[:120]}")
            continue

        counts = batch.request_counts
        done = sum(getattr(counts, k, 0) or 0
                   for k in ("succeeded", "errored", "canceled", "expired"))
        total = done + (getattr(counts, "processing", 0) or 0)
        pct = 100 * done / total if total else 0

        line = f"{batch_id}\n  {batch.processing_status}  {done}/{total} done ({pct:.0f}%)"
        if submitted:
            mins = (time.time() - submitted) / 60
            line += f"  ·  {mins:.0f} min since submission"
        print(line)
        print(f"  succeeded {getattr(counts,'succeeded',0)}  errored {getattr(counts,'errored',0)}"
              f"  processing {getattr(counts,'processing',0)}"
              f"  expired {getattr(counts,'expired',0)}")

        mins = (time.time() - submitted) / 60 if submitted else 0

        if batch.processing_status == "ended":
            print(f"  ready — run: python batch_pipeline.py collect {stage}")
        elif done == 0 and mins > 90:
            # Nothing at all after 90 minutes is outside the normal range and
            # usually means the enqueued requests are not being routed, even
            # though the batch itself was created successfully.
            print("  nothing has completed in 90+ min, which is outside the normal range.")
            print("  Rather than wait out the 24h limit:")
            print(f"     python batch_pipeline.py cancel {stage}")
            print("     python pipeline.py run          # synchronous, 2x cost, known to work")
            print("  Cancelled and expired requests are not billed.")
        elif mins > 20 * 60:
            print("  approaching the 24h limit; anything that expires is not billed")
        else:
            print("  most batches finish within an hour; results are only retrievable")
            print("  once the whole batch ends, so a low succeeded count is not a problem")


def cancel(stage: str) -> None:
    """Stop a stalled batch so the synchronous path can take over.

    Requests that never ran are not billed, so cancelling a stuck batch costs
    nothing beyond whatever had already completed.
    """
    client = make_client(for_batches=True)
    state = _load_state(stage)
    for batch_id in state["batch_ids"]:
        try:
            client.messages.batches.cancel(batch_id)
            print(f"{batch_id}: cancellation requested")
        except Exception as exc:
            print(f"{batch_id}: {type(exc).__name__}: {str(exc)[:140]}")
    print("\nAnything already finished can still be collected:")
    print(f"    python batch_pipeline.py collect {stage}")
    print("Then continue synchronously — finished work is skipped, not re-billed:")
    print("    python pipeline.py run")


# ==========================================================================
# Repair pass for answers the checker refuted
# ==========================================================================
def resolve_refuted() -> None:
    """Re-solve refuted answers on the escalation model, synchronously.

    Batches cannot escalate mid-flight, and this is usually a small enough
    slice that waiting on another batch round-trip is not worth it.
    """
    from claude_client import ClaudeClient

    targets = []
    for path in sorted(config.SOLVE_DIR.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if (data.get("verification_result") or {}).get("status") == verifier.REFUTED:
            targets.append(path)

    if not targets:
        print("no refuted answers")
        return

    print(f"re-solving {len(targets)} refuted answers on {config.ESCALATION_MODEL}")
    client = ClaudeClient()
    fixed = 0

    for path in targets:
        old = json.loads(path.read_text(encoding="utf-8"))
        qid = old.get("question_id", path.stem)
        question = next((q for q in _all_questions() if q["question_id"] == qid), None)
        if question is None:
            print(f"  ! {qid}: no matching question, skipped")
            continue
        new = stage2_solve.solve(client, question, model=config.ESCALATION_MODEL)
        check = verifier.verify(new.get("verification"), config.VERIFY_TIMEOUT)
        new["verification_result"] = check.as_dict()
        new["_meta"]["escalated_from"] = {
            "model": (old.get("_meta") or {}).get("model"),
            "answer": old.get("final_answer_plain"),
            "refutation": (old.get("verification_result") or {}).get("detail"),
        }
        path.write_text(json.dumps(new, indent=2, ensure_ascii=False), encoding="utf-8")
        fixed += check.ok
        print(f"  {new.get('video_id')}: {old.get('final_answer_plain')} -> "
              f"{new.get('final_answer_plain')} [{check.status}]")

    print(f"\n{fixed}/{len(targets)} now verify. The rest need a human.")
    print(client.usage.summary())
    print("stage 3 will need re-running for these: "
          "delete their files from 03_mcq/ and re-submit mcq")


# ==========================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="action", required=True)

    p = sub.add_parser("submit")
    p.add_argument("stage", choices=["extract", "solve", "mcq"])
    p.add_argument("--limit", type=int)
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("collect")
    p.add_argument("stage", choices=["extract", "solve", "mcq"])
    p.add_argument("--poll-seconds", type=int, default=60)
    p.add_argument("--wait", action="store_true", help="block until the batch ends")

    p = sub.add_parser("status")
    p.add_argument("stage", choices=["extract", "solve", "mcq"])

    p = sub.add_parser("cancel")
    p.add_argument("stage", choices=["extract", "solve", "mcq"])

    sub.add_parser("resolve-refuted")

    args = ap.parse_args()
    if args.action == "submit":
        submit(args.stage, args.limit, args.force)
    elif args.action == "collect":
        collect(args.stage, args.poll_seconds, args.wait)
    elif args.action == "status":
        status(args.stage)
    elif args.action == "cancel":
        cancel(args.stage)
    else:
        resolve_refuted()
    return 0


if __name__ == "__main__":
    sys.exit(main())
