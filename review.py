"""Human review of extracted problem statements.

Two commands:

    python review.py make --limit 200      # build output/review/review.html
    python review.py apply decisions.json  # feed decisions back into the pipeline

The generated page puts the whiteboard frame beside the extracted statement,
because that is the only way to review a transcription — no amount of reading
the text tells you whether the board really said x^3. It is a plain file you
open in a browser; no server, no install.

Review happens *between stage 1 and stage 2*, on purpose. Reviewing after the
whole pipeline has run means paying to solve and build distractors for problems
you were going to reject anyway, and it means the correction arrives too late
to change anything.

Decisions flow back:

    approved   nothing changes
    corrected  the statement is rewritten in that question's entry, the original
               kept alongside it, and any stale solve/mcq output for that
               question is deleted so it gets regenerated
    rejected   the question id is added to the frame's rejected_questions list;
               stage 2 skips it and it never reaches a worksheet

Decisions are per QUESTION, not per frame. A seven-part board where only part
(3) is unreadable keeps its other six questions.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import config
import db

REVIEW_DIR = config.OUTPUT_DIR / "review"
REVIEW_HTML = REVIEW_DIR / "review.html"


# ==========================================================================
# Build the page
# ==========================================================================
def gather(limit: int, include_reviewed: bool, min_risk: int) -> list[dict]:
    conn = db.connect()
    try:
        conn.execute("SELECT 1 FROM review_queue LIMIT 1")
    except Exception:
        raise SystemExit("no index yet — run `python db.py build` first")

    where = ["risk >= ?"]
    params: list = [min_risk]
    if not include_reviewed:
        where.append("decision IS NULL")

    rows = conn.execute(
        f"SELECT * FROM review_queue WHERE {' AND '.join(where)} "
        f"ORDER BY risk DESC, video_id LIMIT ?", (*params, limit)).fetchall()
    return [dict(r) for r in rows]


def _flags(row: dict) -> list[tuple[str, str]]:
    """(severity, text) badges, worst first."""
    out = []
    # The defect the pilot found: instruction written, information missing.
    # It looks fine to every automated check except this one.
    if row.get("task_is_explicit") and (row.get("extract_confidence") or 1) < 0.5:
        out.append(("bad", "Instruction IS written but the board lacks the data to answer it: "
                           + (row.get("question_notes") or "see notes")))
    if not row.get("task_is_explicit"):
        out.append(("bad", "Task was INFERRED, not written on the board"))
    if row.get("answer_kind") in ("prose", "drawing"):
        out.append(("bad", f"Answer is a {row.get('answer_kind')}; multiple choice does not fit"))
    if (row.get("part_count") or 1) > 1:
        out.append(("info", f"Part {row.get('part_label') or '?'} of "
                            f"{row.get('part_count')} questions on this board"))
    if row.get("verification_status") == "refuted":
        out.append(("bad", "Answer refuted by computer algebra"))
    if row.get("legibility") == "partly_illegible":
        out.append(("warn", "Board partly illegible"))
    unread = row.get("unreadable_items") or "[]"
    if unread not in ("[]", "", None):
        try:
            items = json.loads(unread)
            if items:
                out.append(("warn", "Unreadable: " + "; ".join(items)))
        except Exception:
            pass
    if row.get("verification_status") in ("skipped", "error"):
        out.append(("warn", f"Answer unverified ({row.get('verification_detail','')[:80]})"))
    if row.get("has_diagram"):
        out.append(("warn", "Depends on a figure the worksheet will not carry"))
    if row.get("board_shows_solution"):
        out.append(("info", "Board also showed worked steps — check the statement is clean"))
    if (row.get("extract_confidence") or 1) < 0.85:
        out.append(("info", f"Extraction confidence {row.get('extract_confidence')}"))
    if row.get("qa_errors"):
        out.append(("bad", "QA: " + row["qa_errors"][:160]))
    return out


def build_html(rows: list[dict], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = []

    for row in rows:
        frame = row.get("frame") or ""
        rel = ""
        if frame:
            try:
                rel = os.path.relpath(Path(frame).resolve(), out_path.parent.resolve())
            except Exception:
                rel = frame
        payload.append({
            "video_id": row["question_id"],
            "frame_id": row["video_id"],
            "part_label": row.get("part_label") or "",
            "part_count": row.get("part_count") or 1,
            "frame": rel.replace(os.sep, "/"),
            "statement": row.get("statement") or "",
            "board": row.get("board_transcription") or "",
            "topic": row.get("topic") or "",
            "difficulty": row.get("difficulty") or "",
            "answer": row.get("final_answer_latex") or "",
            "verification": row.get("verification_status") or "",
            "risk": row.get("risk", 0),
            "inference": row.get("task_inference_basis") or "",
            "flags": _flags(row),
        })

    data = json.dumps(payload, ensure_ascii=False)
    out_path.write_text(_TEMPLATE.replace("__DATA__", data), encoding="utf-8")
    return out_path


_TEMPLATE = r"""<!DOCTYPE html>
<meta charset="utf-8">
<title>Problem statement review</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script defer src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"></script>
<style>
  :root { --bad:#b42318; --warn:#b54708; --info:#475467; --ok:#067647; --line:#e4e7ec; }
  * { box-sizing: border-box; }
  body { margin:0; font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; color:#101828;
         background:#f9fafb; }
  header { position:sticky; top:0; z-index:5; background:#fff; border-bottom:1px solid var(--line);
           padding:10px 18px; display:flex; gap:18px; align-items:center; }
  header b { font-size:15px; }
  .bar { flex:1; height:7px; background:var(--line); border-radius:4px; overflow:hidden; }
  .bar i { display:block; height:100%; background:var(--ok); width:0; transition:width .2s; }
  button { font:inherit; padding:7px 14px; border:1px solid var(--line); background:#fff;
           border-radius:7px; cursor:pointer; }
  button:hover { background:#f2f4f7; }
  button.primary { background:#067647; color:#fff; border-color:#067647; }
  button.danger  { background:#b42318; color:#fff; border-color:#b42318; }
  main { display:grid; grid-template-columns: 1fr 1fr; gap:18px; padding:18px; align-items:start; }
  .pane { background:#fff; border:1px solid var(--line); border-radius:10px; padding:16px; }
  img.frame { width:100%; border:1px solid var(--line); border-radius:8px; cursor:zoom-in;
              background:#fff; }
  img.frame.zoom { position:fixed; inset:12px; width:auto; height:auto; max-width:97vw;
                   max-height:96vh; margin:auto; z-index:50; cursor:zoom-out;
                   box-shadow:0 8px 40px rgba(0,0,0,.4); }
  h3 { margin:0 0 8px; font-size:12px; letter-spacing:.06em; text-transform:uppercase;
       color:#667085; font-weight:600; }
  textarea { width:100%; min-height:118px; font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
             padding:10px; border:1px solid var(--line); border-radius:8px; resize:vertical; }
  .preview { margin-top:10px; padding:12px; background:#f9fafb; border-radius:8px;
             border:1px solid var(--line); min-height:44px; }
  pre.board { white-space:pre-wrap; font:13px/1.5 ui-monospace,Menlo,monospace; background:#f9fafb;
              padding:10px; border-radius:8px; border:1px solid var(--line); max-height:190px;
              overflow:auto; margin:0; }
  .flag { padding:7px 10px; border-radius:7px; margin-bottom:6px; font-size:13.5px;
          border-left:3px solid; background:#fff; }
  .flag.bad  { border-color:var(--bad);  background:#fef3f2; color:var(--bad); font-weight:600; }
  .flag.warn { border-color:var(--warn); background:#fffaeb; color:var(--warn); }
  .flag.info { border-color:var(--info); background:#f9fafb; color:var(--info); }
  .meta { font-size:13px; color:#667085; margin-bottom:10px; }
  .decided { outline:3px solid var(--ok); }
  kbd { background:#f2f4f7; border:1px solid var(--line); border-bottom-width:2px;
        border-radius:4px; padding:1px 5px; font:12px ui-monospace,monospace; }
  .done { padding:60px; text-align:center; }
</style>

<header>
  <b id="pos">–</b>
  <div class="bar"><i id="prog"></i></div>
  <span id="tally" class="meta" style="margin:0"></span>
  <button onclick="nav(-1)">← Prev</button>
  <button onclick="nav(1)">Next →</button>
  <button class="primary" onclick="decide('approved')">Approve <kbd>A</kbd></button>
  <button onclick="decide('corrected')">Save edit <kbd>S</kbd></button>
  <button class="danger" onclick="decide('rejected')">Reject <kbd>R</kbd></button>
  <button onclick="save()">Download decisions</button>
</header>

<main id="app"></main>

<script>
const ITEMS = __DATA__;
const KEY = "mw_review_decisions";
let decisions = {};
let idx = 0;

try { decisions = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { decisions = {}; }

function persist() {
  // Draft autosave only. The download button is what produces the real file.
  try { localStorage.setItem(KEY, JSON.stringify(decisions)); } catch (e) {}
}

function render() {
  const it = ITEMS[idx];
  if (!it) { document.getElementById("app").innerHTML =
      '<div class="pane done"><h2>Nothing to review</h2></div>'; return; }
  const d = decisions[it.video_id];

  document.getElementById("pos").textContent = `${idx + 1} / ${ITEMS.length}`;
  const n = Object.keys(decisions).length;
  document.getElementById("prog").style.width = (100 * n / ITEMS.length) + "%";
  const counts = { approved: 0, corrected: 0, rejected: 0 };
  Object.values(decisions).forEach(x => counts[x.decision] = (counts[x.decision] || 0) + 1);
  document.getElementById("tally").textContent =
    `${n} decided · ${counts.approved} approved · ${counts.corrected} edited · ${counts.rejected} rejected`;

  const flags = it.flags.map(([sev, text]) =>
      `<div class="flag ${sev}">${esc(text)}</div>`).join("") || "";

  document.getElementById("app").innerHTML = `
    <div class="pane ${d ? "decided" : ""}">
      <h3>Whiteboard frame</h3>
      ${it.frame ? `<img class="frame" src="${esc(it.frame)}"
           onclick="this.classList.toggle('zoom')"
           onerror="this.outerHTML='<p class=meta>Frame not found at ${esc(it.frame)}</p>'">`
                 : `<p class="meta">No frame path recorded.</p>`}
      <h3 style="margin-top:14px">Literal board transcription</h3>
      <pre class="board">${esc(it.board) || "(none)"}</pre>
    </div>

    <div class="pane ${d ? "decided" : ""}">
      <div class="meta">
        <b>${esc(it.video_id)}</b> · ${esc(it.topic)} · ${esc(it.difficulty)} ·
        risk ${it.risk}${it.part_count > 1 ? ` · part ${esc(it.part_label)} of ${it.part_count}` : ""}
        ${d ? `· <b style="color:var(--ok)">${esc(d.decision)}</b>` : ""}
      </div>
      ${flags}
      ${it.inference ? `<div class="flag info">Inference basis: ${esc(it.inference)}</div>` : ""}

      <h3 style="margin-top:14px">Problem statement — edit if wrong</h3>
      <textarea id="stmt" oninput="preview()">${esc(d && d.corrected_statement || it.statement)}</textarea>
      <div class="preview" id="prev"></div>

      <h3 style="margin-top:14px">Answer${it.verification ? ` (${esc(it.verification)})` : ""}</h3>
      <div class="preview">${it.answer ? "$" + esc(it.answer) + "$" : "(not solved yet)"}</div>

      <h3 style="margin-top:14px">Comment</h3>
      <textarea id="comment" style="min-height:52px"
        placeholder="Optional note for yourself">${esc(d && d.comment || "")}</textarea>
    </div>`;
  preview();
  typeset(document.getElementById("app"));
}

function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g,
    c => ({ "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;" }[c]));
}

function typeset(el) {
  if (window.renderMathInElement) {
    try {
      renderMathInElement(el, { delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "$",  right: "$",  display: false }], throwOnError: false });
    } catch (e) {}
  }
}

function preview() {
  const p = document.getElementById("prev");
  if (!p) return;
  p.innerHTML = esc(document.getElementById("stmt").value);
  typeset(p);
}

function decide(kind) {
  const it = ITEMS[idx];
  if (!it) return;
  const stmt = document.getElementById("stmt").value.trim();
  const comment = document.getElementById("comment").value.trim();
  const changed = stmt !== it.statement;
  decisions[it.video_id] = {
    decision: kind === "approved" && changed ? "corrected" : kind,
    corrected_statement: changed ? stmt : "",
    comment: comment,
    reviewed_at: new Date().toISOString()
  };
  persist();
  nav(1);
}

function nav(step) {
  idx = Math.max(0, Math.min(ITEMS.length - 1, idx + step));
  render();
  window.scrollTo(0, 0);
}

function save() {
  const blob = new Blob([JSON.stringify(decisions, null, 2)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "decisions.json";
  a.click();
}

document.addEventListener("keydown", e => {
  if (e.target.tagName === "TEXTAREA") return;
  const k = e.key.toLowerCase();
  if (k === "a") decide("approved");
  else if (k === "r") decide("rejected");
  else if (k === "s") decide("corrected");
  else if (e.key === "ArrowRight") nav(1);
  else if (e.key === "ArrowLeft") nav(-1);
});

window.addEventListener("load", render);
render();
</script>
"""


# ==========================================================================
# Apply decisions back into the pipeline
# ==========================================================================
def apply_decisions(path: Path) -> None:
    decisions = json.loads(path.read_text(encoding="utf-8"))
    if not decisions:
        print("no decisions in that file")
        return

    conn = db.connect()
    conn.executescript(db.SCHEMA)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    counts = {"approved": 0, "corrected": 0, "rejected": 0}
    invalidated = 0

    import questions as Q

    for question_id, d in decisions.items():
        decision = d.get("decision", "approved")
        counts[decision] = counts.get(decision, 0) + 1

        conn.execute(
            "INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?,?)",
            (question_id, decision, d.get("corrected_statement", ""), d.get("comment", ""),
             d.get("reviewer", "human"), d.get("reviewed_at", now)))

        video_id, part_index = Q.split_question_id(question_id)
        ext_path = config.EXTRACT_DIR / f"{video_id}.json"
        if not ext_path.exists():
            print(f"  ! {question_id}: no extraction file, decision recorded only")
            continue

        data = json.loads(ext_path.read_text(encoding="utf-8"))
        qs = data.get("questions") or []
        if not (1 <= part_index <= len(qs)):
            print(f"  ! {question_id}: part {part_index} not in that frame, recorded only")
            continue
        target = qs[part_index - 1]
        target.setdefault("_review", {}).update(
            {"decision": decision, "comment": d.get("comment", ""), "at": d.get("reviewed_at", now)})

        drop = set(data.get("rejected_questions") or [])
        if decision == "rejected":
            drop.add(question_id)
        else:
            drop.discard(question_id)
            if decision == "corrected" and d.get("corrected_statement"):
                if "statement_original" not in target:
                    target["statement_original"] = target.get("statement", "")
                target["statement"] = d["corrected_statement"]
                # A corrected statement invalidates everything derived from the
                # old one. Delete rather than leave stale output that survives.
                for stale_dir in (config.SOLVE_DIR, config.MCQ_DIR):
                    stale = stale_dir / f"{question_id}.json"
                    if stale.exists():
                        stale.unlink()
                        invalidated += 1
        data["rejected_questions"] = sorted(drop)

        ext_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    conn.commit()
    print("applied: " + ", ".join(f"{v} {k}" for k, v in counts.items() if v))
    if invalidated:
        print(f"deleted {invalidated} stale downstream file(s); re-run solve and mcq to regenerate")
    print("re-run `python db.py build` to refresh the index")


# ==========================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("make", help="generate the HTML review queue")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--min-risk", type=int, default=1,
                   help="0 includes clean items too; default skips zero-risk ones")
    p.add_argument("--include-reviewed", action="store_true")
    p.add_argument("--out", type=Path, default=REVIEW_HTML)

    p = sub.add_parser("apply", help="write decisions.json back into the pipeline")
    p.add_argument("decisions", type=Path)

    args = ap.parse_args()

    if args.cmd == "make":
        rows = gather(args.limit, args.include_reviewed, args.min_risk)
        if not rows:
            print("nothing matches — try --min-risk 0 or --include-reviewed")
            return 0
        out = build_html(rows, args.out)
        risky = sum(1 for r in rows if r.get("risk", 0) >= 50)
        print(f"{len(rows)} items ({risky} at risk >= 50)")
        print(f"-> {out}")
        print("open it in a browser; A approve, S save edit, R reject, arrows navigate")
    else:
        apply_decisions(args.decisions)
    return 0


if __name__ == "__main__":
    sys.exit(main())
