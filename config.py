"""Central configuration for the math worksheet pipeline."""

import os
from pathlib import Path

# Bumped on every release. Modules that must move together carry the same
# value; smoke_test compares them, so a partial update fails with a clear
# message instead of a TypeError deep inside a build.
BUILD_ID = "2026-08-16.2"

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
ROOT = Path(__file__).parent
INPUT_DIR = Path(os.getenv("MW_INPUT", ROOT / "input"))
OUTPUT_DIR = Path(os.getenv("MW_OUTPUT", ROOT / "output"))

EXTRACT_DIR = OUTPUT_DIR / "01_extracted"    # stage 1: problem statements
SOLVE_DIR = OUTPUT_DIR / "02_solved"         # stage 2: answers + verification
MCQ_DIR = OUTPUT_DIR / "03_mcq"              # stage 3: multiple-choice items
QA_DIR = OUTPUT_DIR / "04_qa"                # QA reports
DOCX_DIR = OUTPUT_DIR / "05_docx"            # finished worksheets

FRAME_NAMES = ("frame.png", "frame.jpg", "frame.jpeg", "problem.png")

# "auto" detects from the directory contents: loose image files mean a flat
# folder (what a synced Google Drive / Box / OneDrive mirror looks like),
# subdirectories mean one folder per video. Force with "flat" or "nested".
INPUT_LAYOUT = os.getenv("MW_LAYOUT", "auto")
TRANSCRIPT_NAMES = ("transcript.txt", "transcript.vtt", "transcript.srt")

# --------------------------------------------------------------------------
# Gateway support (Portkey, LiteLLM, Bedrock proxy, a university endpoint)
#
# Portkey proxies Anthropic's native /messages endpoint, so the anthropic SDK
# works unchanged apart from base_url, a header, and a model prefix. Set
# PORTKEY_API_KEY and everything below configures itself.
#
#     export PORTKEY_API_KEY=WGF...
#     export MW_MODEL_PREFIX=@your-account/
#
# For any other gateway, set MW_BASE_URL / MW_EXTRA_HEADERS by hand.
# --------------------------------------------------------------------------
PORTKEY_API_KEY = os.getenv("PORTKEY_API_KEY") or None
PORTKEY_BASE_URL = os.getenv("MW_PORTKEY_URL", "https://api.portkey.ai")

API_BASE_URL = os.getenv("MW_BASE_URL") or (PORTKEY_BASE_URL if PORTKEY_API_KEY else None)

# Portkey carries provider credentials itself, so the SDK still needs a
# non-empty api_key but its value is irrelevant.
API_KEY = os.getenv("ANTHROPIC_API_KEY") or ("gateway" if PORTKEY_API_KEY else None)


def _extra_headers() -> dict:
    import json as _json
    headers = _json.loads(os.getenv("MW_EXTRA_HEADERS", "{}"))
    if PORTKEY_API_KEY:
        headers.setdefault("x-portkey-api-key", PORTKEY_API_KEY)
    return headers


EXTRA_HEADERS = _extra_headers()

# Portkey addresses models as "@provider-slug/model-name". Everything in this
# codebase names models bare, so the prefix is applied at request time and
# stripped again for the pricing lookup.
MODEL_PREFIX = os.getenv("MW_MODEL_PREFIX", "")

# The /messages endpoint resolves the provider from the "@slug/model" prefix,
# but /messages/batches does not -- it returns 400 asking for an explicit
# x-portkey-provider header. Derived from MODEL_PREFIX unless overridden, and
# only sent on batch requests so it cannot disturb the path that already works.
PORTKEY_PROVIDER = os.getenv("MW_PORTKEY_PROVIDER") or (
    MODEL_PREFIX.lstrip("@").rstrip("/") if MODEL_PREFIX else None)

# The two endpoints want the provider named in *different places*, and setting
# up for one breaks the other:
#
#   /messages         "@slug/model" prefix, no provider header
#   /messages/batches x-portkey-provider: @slug, and a BARE model name
#
# So the prefix stays configured for the sync path and the batch path strips it
# while adding the header. One set of environment variables drives both.
BATCH_BARE_MODEL = os.getenv("MW_BATCH_BARE_MODEL", "1" if PORTKEY_PROVIDER else "0") == "1"


def batch_headers() -> dict:
    extra = dict(EXTRA_HEADERS)
    if PORTKEY_PROVIDER:
        extra.setdefault("x-portkey-provider", PORTKEY_PROVIDER)
    return extra

# "schema"  send output_config, so the decoder is grammar-constrained and the
#           model CANNOT emit non-conforming JSON. Always prefer this.
# "prompt"  ask for JSON in the system prompt instead. For gateways that strip
#           output_config. Weaker: the model can still return prose or drift
#           from the schema, so expect occasional retries. Run
#           check_gateway.py to find out which one you need.
JSON_MODE = os.getenv("MW_JSON_MODE", "schema")


def base_model(model: str) -> str:
    """Strip any gateway prefix, so PRICING still resolves."""
    return model.rsplit("/", 1)[-1] if model.startswith("@") else model


def resolve_model(model: str, *, for_batches: bool = False) -> str:
    """Bare model name -> whatever the target endpoint expects."""
    if for_batches and BATCH_BARE_MODEL:
        return base_model(model)
    if not MODEL_PREFIX or model.startswith("@"):
        return model
    return f"{MODEL_PREFIX.rstrip('/')}/{model}"


# --------------------------------------------------------------------------
# Models
#
# Stage 1 (vision): read the whiteboard. Nothing else.
# Stage 2 (reasoning): solve, and specify a check SymPy can run.
# Stage 3 (reasoning): design distractors around the verified answer.
#
# Stage 2 defaults to Opus. With no transcript there is no external answer
# key, so a wrong answer here poisons every option in the item; this is the
# one stage where the stronger model is worth paying for by default. Set
# MW_SOLVE_MODEL=claude-sonnet-5 and compare on a pilot before deciding.
# --------------------------------------------------------------------------
EXTRACT_MODEL = os.getenv("MW_EXTRACT_MODEL", "claude-sonnet-5")
SOLVE_MODEL = os.getenv("MW_SOLVE_MODEL", "claude-opus-5")
MCQ_MODEL = os.getenv("MW_MCQ_MODEL", "claude-sonnet-5")
ESCALATION_MODEL = os.getenv("MW_ESCALATION_MODEL", "claude-opus-5")

# Escalate to ESCALATION_MODEL when a stage reports confidence below this.
# Escalation is the single biggest swing in the bill -- it reruns a call on a
# more expensive model, so every escalated item costs roughly triple. Lower
# this to spend less and send more items to human review instead.
ESCALATE_BELOW = float(os.getenv("MW_ESCALATE_BELOW", "0.85"))

# Stage 1 rereads the WHOLE frame, so escalating for one shaky part out of
# eight is poor value -- that part should go to a human instead. Reread only
# when at least this share of the board's questions look weak.
ESCALATE_QUESTION_SHARE = float(os.getenv("MW_ESCALATE_SHARE", "0.5"))

# Route to a human when even the escalated run stays below this.
HUMAN_REVIEW_BELOW = float(os.getenv("MW_HUMAN_REVIEW_BELOW", "0.80"))

EXTRACT_MAX_TOKENS = 3000
SOLVE_MAX_TOKENS = 4000
MCQ_MAX_TOKENS = 4000

# --------------------------------------------------------------------------
# Answer verification (verifier.py)
#
# SymPy's integrate() and simplify() genuinely hang on some inputs. At 3,000
# items you will hit it, so every check runs in a subprocess under this cap.
# A timeout is recorded as "unverified", never as a wrong answer.
# --------------------------------------------------------------------------
# 20s was too tight: a legitimate 90-term trigonometric sum needed ~30s.
# A timeout costs a verification, so err generous -- the checks run in
# parallel with the API calls anyway.
VERIFY_TIMEOUT = int(os.getenv("MW_VERIFY_TIMEOUT", "60"))

# When no symbolic check applies (proofs, sketches, some word problems), solve
# a second time on the escalation model and require the answers to agree.
# Costs one extra solve on those items only. Turn off to save money at the
# price of unchecked answers.
SECOND_OPINION_WHEN_UNVERIFIED = os.getenv("MW_SECOND_OPINION", "1") != "0"

# Hold items whose answer was never confirmed out of the worksheet. Default is
# off: unverified is not the same as wrong, and holding every proof problem
# would cut the usable set hard. They are flagged in the QA report either way.
REQUIRE_VERIFIED_ANSWER = os.getenv("MW_REQUIRE_VERIFIED", "0") == "1"

# Hold items whose task was inferred rather than written on the board. Only
# bites when the reading was also unsure or the answer unverified.
REQUIRE_EXPLICIT_TASK = os.getenv("MW_REQUIRE_EXPLICIT_TASK", "1") == "1"

# --------------------------------------------------------------------------
# What a failed check does
#
#   "flag"    (default) only genuinely broken items are held: two options that
#             are the same number, LaTeX that will not render, an answer
#             computer algebra refuted. Everything else ships and is listed in
#             a concerns report for you to spot-check.
#   "strict"  low confidence also holds the item.
#
# Confidence-based holds have poor precision. Measured on a real pilot, about
# nine in ten items held for low confidence were in fact correct, so holding
# them buys very little and costs a lot of reading.
# --------------------------------------------------------------------------
HOLD_MODE = os.getenv("MW_HOLD_MODE", "flag")

# Optional. Opus 5 / Sonnet 5 accept "low" | "medium" | "high" and default to
# "high" on the Claude API. Extraction rarely needs high effort; distractor
# design usually does. Set to None to omit the parameter entirely.
EXTRACT_EFFORT = os.getenv("MW_EXTRACT_EFFORT") or None
SOLVE_EFFORT = os.getenv("MW_SOLVE_EFFORT") or None
MCQ_EFFORT = os.getenv("MW_MCQ_EFFORT") or None

# --------------------------------------------------------------------------
# Pricing, USD per million tokens. Synchronous rates; the Message Batches API
# bills at 50% of these. Verify against
# https://platform.claude.com/docs/en/about-claude/pricing before budgeting.
# --------------------------------------------------------------------------
PRICING = {
    "claude-opus-5":            {"input": 5.00, "output": 25.00},
    "claude-sonnet-5":          {"input": 3.00, "output": 15.00},
    "claude-haiku-4-5":         {"input": 1.00, "output": 5.00},
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00},
    "claude-fable-5":           {"input": 10.00, "output": 50.00},
}
BATCH_DISCOUNT = 0.5

# --------------------------------------------------------------------------
# Image handling
#
# Claude bills images at roughly (width x height) / 750 tokens and downscales
# anything whose long edge exceeds 1568px. Resizing locally to that ceiling
# costs nothing in quality and keeps the token count predictable.
# --------------------------------------------------------------------------
MAX_IMAGE_EDGE = 1568
JPEG_QUALITY = 90

# --------------------------------------------------------------------------
# Concurrency (synchronous path only). Tune to your org's rate limits.
# --------------------------------------------------------------------------
MAX_WORKERS = int(os.getenv("MW_WORKERS", "8"))
MAX_RETRIES = 5
RETRY_BASE_DELAY = 2.0

# --------------------------------------------------------------------------
# Worksheet formatting
# --------------------------------------------------------------------------
PROBLEMS_PER_WORKSHEET = 20

# Body font and size, matched to the reference worksheet. Word substitutes if
# Roboto is not installed; set MW_BODY_FONT=Calibri to avoid that.
# Track markers that appear in filenames and split a homework into a separate
# document, e.g. "5HW24 BASIC ..." is distinct from "5HW24 ...". Comma
# separated; add ADVANCED, HONORS, REVIEW or whatever your exports use.
TRACKS = [t.strip().upper() for t in os.getenv("MW_TRACKS", "BASIC").split(",") if t.strip()]

# Default document layout.
#   "combined"  one file per course series and track, every homework in it
#               starting on a fresh page after the previous answer key
#   "hw"        one file per homework number
#   "sets"      fixed-size worksheets with separate _KEY files
# Note this is the OUTPUT layout; MW_LAYOUT above controls the INPUT folder shape.
DOCX_LAYOUT = os.getenv("MW_DOCX_LAYOUT", "combined")

# Number problems with a real Word list rather than literal "1." text, so
# inserting a question renumbers the rest automatically. Set to 0 for fixed
# text numbers.
AUTO_NUMBER = os.getenv("MW_AUTO_NUMBER", "1") != "0"

# Put all four answer choices on one line when they fit across it together,
# and fall back to one per line when they do not. What matters is the TOTAL
# width, not the widest option: "does not exist" beside 1, 0 and -1/2 fits
# comfortably even though one option is long.
#
# INLINE_CHOICE_FIT is the fraction of the usable line width the choices may
# occupy; below 1.0 leaves a safety margin against the width estimate being
# optimistic. Set to 0 to always use one choice per line.
INLINE_CHOICE_FIT = float(os.getenv("MW_INLINE_CHOICE_FIT", "0.98"))

# How much of the line the row of choices actually spans when positioning them.
# Separate from INLINE_CHOICE_FIT, which only decides whether they go on one
# line at all: lowering the fit threshold would push questions to one-per-line
# as a side effect, whereas this just pulls the last choice left. 1.0 runs (D)
# to the right margin.
INLINE_CHOICE_SPREAD = float(os.getenv("MW_INLINE_CHOICE_SPREAD", "0.88"))

# A single option wider than this always forces one-per-line, however short its
# neighbours are -- a very long option next to three tiny ones looks broken
# even when the arithmetic says it fits.
INLINE_CHOICE_MAX = float(os.getenv("MW_INLINE_CHOICE_MAX", "26"))

BODY_FONT = os.getenv("MW_BODY_FONT", "Roboto")
BODY_SIZE_PT = float(os.getenv("MW_BODY_SIZE_PT", "12"))
CHOICE_LABELS = ("A", "B", "C", "D")
COURSE_TITLE = os.getenv("MW_COURSE_TITLE", "Mathematics Practice Set")

# Deterministic answer-position shuffling. Keep this fixed so regenerating a
# worksheet produces the same lettering; change it to reshuffle every form.
SHUFFLE_SALT = os.getenv("MW_SHUFFLE_SALT", "form-A")


def ensure_dirs() -> None:
    for d in (EXTRACT_DIR, SOLVE_DIR, MCQ_DIR, QA_DIR, DOCX_DIR):
        d.mkdir(parents=True, exist_ok=True)
