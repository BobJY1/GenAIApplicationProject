#!/usr/bin/env bash
# Pilot run, end to end. Stops at the first failure.
set -euo pipefail

: "${ANTHROPIC_API_KEY:?Set ANTHROPIC_API_KEY first}"
LIMIT="${1:-50}"

echo "== 0. offline checks (no API calls, no cost) =="
python3 omml.py
python3 verifier.py | tail -1
python3 smoke_test.py | tail -1

echo; echo "== 1. read the boards =="
python3 pipeline.py extract --limit "$LIMIT"

echo; echo "== 2. review the statements BEFORE paying to solve them =="
python3 pipeline.py index
python3 pipeline.py review
echo "   Open output/review/review.html, work the queue, click Download decisions,"
echo "   then:  python3 review.py apply ~/Downloads/decisions.json"
echo "   Press Enter to continue without reviewing, or Ctrl-C to stop here."
read -r _

echo; echo "== 3. solve and verify =="
python3 pipeline.py solve

echo; echo "== 4. build the questions =="
python3 pipeline.py mcq

echo; echo "== 5. check and build =="
python3 pipeline.py qa
python3 pipeline.py build
python3 pipeline.py index
python3 db.py stats
