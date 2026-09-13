"""SQLite index over the pipeline's JSON output.

The JSON files stay the source of truth — they are what the API produced, and
nothing here modifies them. This is a queryable view over them, rebuilt from
scratch whenever you want:

    python db.py build
    python db.py stats
    python db.py queue --limit 40
    python db.py sql "SELECT video_id, statement FROM extractions WHERE topic LIKE '%integral%'"

The point is the `review_queue` view. You are not going to read 3,000 problem
statements, and you should not have to: the failure modes are concentrated.
Items are ranked by a risk score built from the signals the pipeline already
collects, so reviewing the top few hundred covers most of what would actually
reach a student wrong.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import config
import questions as Q

DB_PATH = config.OUTPUT_DIR / "review.db"


SCHEMA = """
-- One row per QUESTION, not per frame. Real practice boards carry several.
CREATE TABLE IF NOT EXISTS extractions (
    question_id          TEXT PRIMARY KEY,
    video_id             TEXT,
    part_label           TEXT,
    part_index           INTEGER,
    part_count           INTEGER,
    frame                TEXT,
    statement            TEXT,
    shared_context       TEXT,
    board_transcription  TEXT,
    task_is_explicit     INTEGER,
    task_inference_basis TEXT,
    answer_kind          TEXT,
    has_diagram          INTEGER,
    diagram_description  TEXT,
    board_shows_solution INTEGER,
    legibility           TEXT,
    unreadable_items     TEXT,
    topic                TEXT,
    difficulty           TEXT,
    confidence           REAL,
    notes                TEXT,
    frame_confidence     REAL,
    frame_notes          TEXT,
    model                TEXT
);

CREATE TABLE IF NOT EXISTS solutions (
    question_id          TEXT PRIMARY KEY,
    video_id             TEXT,
    final_answer_latex   TEXT,
    final_answer_plain   TEXT,
    answer_is_expression INTEGER,
    verification_status  TEXT,
    verification_kind    TEXT,
    verification_detail  TEXT,
    second_opinion       TEXT,
    solution_steps       TEXT,
    confidence           REAL,
    notes                TEXT,
    model                TEXT
);

CREATE TABLE IF NOT EXISTS items (
    question_id    TEXT PRIMARY KEY,
    video_id       TEXT,
    question_stem  TEXT,
    correct_latex  TEXT,
    correct_plain  TEXT,
    distractors    TEXT,
    misconceptions TEXT,
    confidence     REAL,
    review_flags   TEXT,
    model          TEXT
);

CREATE TABLE IF NOT EXISTS qa (
    question_id TEXT PRIMARY KEY,
    status   TEXT,
    errors   TEXT,
    warnings TEXT
);

-- Human decisions. The only table this pipeline does not generate.
CREATE TABLE IF NOT EXISTS reviews (
    question_id         TEXT PRIMARY KEY,
    decision            TEXT,   -- approved | corrected | rejected
    corrected_statement TEXT,
    comment             TEXT,
    reviewer            TEXT,
    reviewed_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_ex_video   ON extractions(video_id);
CREATE INDEX IF NOT EXISTS idx_ex_kind    ON extractions(answer_kind);
CREATE INDEX IF NOT EXISTS idx_ex_topic   ON extractions(topic);
CREATE INDEX IF NOT EXISTS idx_ex_task    ON extractions(task_is_explicit);
CREATE INDEX IF NOT EXISTS idx_sol_verify ON solutions(verification_status);
CREATE INDEX IF NOT EXISTS idx_qa_status  ON qa(status);
"""

# Risk score. Weights are ordered by how badly the failure lands on a student,
# not by how likely it is. A refuted answer and an invented question are the
# two that put a wrong item in front of a class, so they dominate.
RISK_VIEW = """
DROP VIEW IF EXISTS review_queue;
CREATE VIEW review_queue AS
SELECT
    e.question_id,
    e.video_id,
    e.part_label,
    e.part_count,
    e.frame,
    e.statement,
    e.answer_kind,
    e.topic,
    e.difficulty,
    e.confidence            AS extract_confidence,
    e.notes                 AS question_notes,
    e.task_is_explicit,
    e.task_inference_basis,
    e.legibility,
    e.unreadable_items,
    e.has_diagram,
    e.board_shows_solution,
    e.board_transcription,
    s.final_answer_latex,
    s.final_answer_plain,
    s.verification_status,
    s.verification_detail,
    s.second_opinion,
    q.status                AS qa_status,
    q.errors                AS qa_errors,
    q.warnings              AS qa_warnings,
    r.decision,
    (
        CASE WHEN e.confidence < 0.5                          THEN 110 ELSE 0 END +
        CASE WHEN e.task_is_explicit = 0                      THEN 100 ELSE 0 END +
        CASE WHEN e.answer_kind IN ('prose','drawing')        THEN  35 ELSE 0 END +
        CASE WHEN s.verification_status = 'refuted'           THEN  90 ELSE 0 END +
        CASE WHEN e.legibility = 'partly_illegible'           THEN  50 ELSE 0 END +
        CASE WHEN e.unreadable_items NOT IN ('[]', '')        THEN  40 ELSE 0 END +
        CASE WHEN e.confidence < 0.85                         THEN  30 ELSE 0 END +
        CASE WHEN s.verification_status IN ('skipped','error') THEN  25 ELSE 0 END +
        CASE WHEN e.has_diagram = 1                           THEN  20 ELSE 0 END +
        CASE WHEN q.status = 'HOLD'                           THEN  15 ELSE 0 END
    ) AS risk
FROM extractions e
LEFT JOIN solutions s ON s.question_id = e.question_id
LEFT JOIN items    i ON i.question_id = e.question_id
LEFT JOIN qa       q ON q.question_id = e.question_id
LEFT JOIN reviews  r ON r.question_id = e.question_id;
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _j(value) -> str:
    return json.dumps(value, ensure_ascii=False) if value is not None else ""


def build(conn: sqlite3.Connection) -> dict:
    """(Re)index every JSON file. Safe to run repeatedly; reviews are preserved."""
    conn.executescript(SCHEMA)
    counts = {"extractions": 0, "solutions": 0, "items": 0, "qa": 0}

    for path in sorted(config.EXTRACT_DIR.glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        model = (d.get("_meta") or {}).get("model", "")
        for q in Q.iter_questions(d):
            conn.execute(
                "INSERT OR REPLACE INTO extractions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (q["question_id"], q["video_id"], q["part_label"], q["part_index"],
                 q["part_count"], q["frame"], q["statement"], q["shared_context"],
                 q["board_transcription"], int(q["task_is_explicit"]),
                 q["task_inference_basis"], q["answer_kind"], int(bool(q["has_diagram"])),
                 q["diagram_description"], int(bool(q["board_shows_solution"])),
                 q["legibility"], _j(q["unreadable_items"]),
                 q["topic"], q["difficulty"], q["confidence"], q["notes"],
                 q["frame_confidence"], q["frame_notes"], model))
            counts["extractions"] += 1

    for path in sorted(config.SOLVE_DIR.glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        check = d.get("verification_result") or {}
        conn.execute(
            "INSERT OR REPLACE INTO solutions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (d.get("question_id", path.stem), d.get("video_id", ""), d.get("final_answer_latex", ""),
             d.get("final_answer_plain", ""), int(bool(d.get("answer_is_expression"))),
             check.get("status", ""), check.get("kind", ""), check.get("detail", ""),
             _j(d.get("second_opinion")), _j(d.get("solution_steps")),
             d.get("confidence"), d.get("notes", ""), (d.get("_meta") or {}).get("model", "")))
        counts["solutions"] += 1

    for path in sorted(config.MCQ_DIR.glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        distractors = d.get("distractors") or []
        conn.execute(
            "INSERT OR REPLACE INTO items VALUES (?,?,?,?,?,?,?,?,?,?)",
            (d.get("question_id", path.stem), d.get("video_id", ""), d.get("question_stem", ""),
             (d.get("correct_answer") or {}).get("latex", ""),
             (d.get("correct_answer") or {}).get("plain", ""),
             _j(distractors),
             ",".join(x.get("misconception", "") for x in distractors),
             d.get("confidence"), _j(d.get("review_flags")),
             (d.get("_meta") or {}).get("model", "")))
        counts["items"] += 1

    report = config.QA_DIR / "qa_report.json"
    if report.exists():
        for r in json.loads(report.read_text(encoding="utf-8"))["results"]:
            conn.execute("INSERT OR REPLACE INTO qa VALUES (?,?,?,?)",
                         (r["video_id"], "pass" if r["ok"] else "HOLD",
                          " | ".join(r["errors"]), " | ".join(r["warnings"])))
            counts["qa"] += 1

    conn.executescript(RISK_VIEW)
    conn.commit()
    return counts


def stats(conn: sqlite3.Connection) -> None:
    def one(sql: str):
        row = conn.execute(sql).fetchone()
        return row[0] if row else 0

    total = one("SELECT COUNT(*) FROM extractions")
    if not total:
        print("no extractions indexed — run the pipeline first, then `python db.py build`")
        return

    frames = one("SELECT COUNT(DISTINCT video_id) FROM extractions")
    print(f"{total} questions across {frames} frames  ({total/max(frames,1):.1f} per frame)\n")

    multi = one("SELECT COUNT(DISTINCT video_id) FROM extractions WHERE part_count > 1")
    print("  extraction")
    print(f"    frames carrying >1 question  {multi}")
    print(f"    task written on the board    {one('SELECT COUNT(*) FROM extractions WHERE task_is_explicit=1')}")
    print(f"    task inferred                {one('SELECT COUNT(*) FROM extractions WHERE task_is_explicit=0')}")
    print(f"    written but unanswerable     {one('SELECT COUNT(*) FROM extractions WHERE task_is_explicit=1 AND confidence < 0.5')}")
    print(f"    depends on a figure          {one('SELECT COUNT(*) FROM extractions WHERE has_diagram=1')}")
    print("    answer kinds                 " + ", ".join(
        f"{r['answer_kind']} {r['n']}" for r in conn.execute(
            "SELECT answer_kind, COUNT(*) n FROM extractions GROUP BY answer_kind ORDER BY n DESC")))

    if one("SELECT COUNT(*) FROM solutions"):
        print("\n  answer verification")
        for row in conn.execute(
                "SELECT verification_status s, COUNT(*) n FROM solutions GROUP BY s ORDER BY n DESC"):
            print(f"    {row['s'] or '(none)':24s} {row['n']}")

    if one("SELECT COUNT(*) FROM qa"):
        print("\n  QA")
        for row in conn.execute("SELECT status, COUNT(*) n FROM qa GROUP BY status"):
            print(f"    {row['status']:24s} {row['n']}")

    reviewed = one("SELECT COUNT(*) FROM reviews")
    print(f"\n  human review: {reviewed} of {total} decided")
    if reviewed:
        for row in conn.execute("SELECT decision, COUNT(*) n FROM reviews GROUP BY decision"):
            print(f"    {row['decision']:24s} {row['n']}")

    pending_risky = one("SELECT COUNT(*) FROM review_queue WHERE risk >= 50 AND decision IS NULL")
    print(f"\n  {pending_risky} unreviewed items score risk >= 50 — start there")


def queue(conn: sqlite3.Connection, limit: int, include_reviewed: bool) -> None:
    where = "" if include_reviewed else "WHERE decision IS NULL"
    rows = conn.execute(
        f"SELECT question_id, risk, task_is_explicit, verification_status, "
        f"substr(statement,1,62) stmt FROM review_queue {where} "
        f"ORDER BY risk DESC, question_id LIMIT ?", (limit,)).fetchall()
    if not rows:
        print("queue is empty")
        return
    print(f"{'question_id':18s} {'risk':>4s} {'task':>5s} {'verify':10s} {'statement'}")
    for r in rows:
        print(f"{r['question_id']:18s} {r['risk']:>4} "
              f"{'yes' if r['task_is_explicit'] else 'NO':>5s} "
              f"{(r['verification_status'] or '-'):10s} {r['stmt'] or ''}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build", help="(re)index every JSON file into SQLite")
    sub.add_parser("stats", help="summary of what is in the index")
    p = sub.add_parser("queue", help="highest-risk unreviewed items")
    p.add_argument("--limit", type=int, default=25)
    p.add_argument("--include-reviewed", action="store_true")
    p = sub.add_parser("sql", help="run an arbitrary query")
    p.add_argument("query")

    args = ap.parse_args()
    conn = connect()

    if args.cmd == "build":
        counts = build(conn)
        print("indexed: " + ", ".join(f"{v} {k}" for k, v in counts.items()))
        print(f"-> {DB_PATH}")
    elif args.cmd == "stats":
        stats(conn)
    elif args.cmd == "queue":
        queue(conn, args.limit, args.include_reviewed)
    else:
        for row in conn.execute(args.query):
            print(" | ".join(str(v)[:60] for v in row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
