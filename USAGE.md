# Running it

Installation, the pipeline stages, reviewing what was held, batch processing,
document layout, and every configuration variable. The short version of what
this project is and why is in [README.md](README.md).

 it

## 1. Install (once)

You need **Python 3.9 or newer**. Check with `python3 --version`.

**macOS / Linux**

```bash
cd math-worksheets
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell)** — use `python`, not `python3`:

```powershell
cd math-worksheets
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

If PowerShell blocks the activation script, run
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` first; it applies
to that window only.

Then set your key. Direct from the Claude Console (not your claude.ai login —
separate products, a chat subscription will not work here):

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```
```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."        # PowerShell
```

**Through a gateway instead?** Portkey proxies Anthropic's native `/messages`
endpoint, so the pipeline runs unchanged with two variables:

```bash
export PORTKEY_API_KEY=WGF...
export MW_MODEL_PREFIX=@your-account       # the "@account/" part of @account/claude-opus-5
```
```powershell
$env:PORTKEY_API_KEY  = "WGF..."           # PowerShell — quotes matter
$env:MW_MODEL_PREFIX  = "@your-account"
```

These last only for the current window. To keep them, set them once at user
scope and reopen PowerShell:

```powershell
[Environment]::SetEnvironmentVariable("PORTKEY_API_KEY", "WGF...", "User")
[Environment]::SetEnvironmentVariable("MW_MODEL_PREFIX", "@your-account", "User")
```

Then confirm what your gateway actually passes through — a few cents, and it
turns guesswork into facts:

```bash
python3 check_gateway.py     # Windows: python check_gateway.py
```

It tests four things in the order they would bite you: basic calls, image
input, structured outputs, and message batches. Two possible surprises:

- **Structured outputs stripped.** Set `MW_JSON_MODE=prompt` and the schema
  moves into the system prompt instead of constraining the decoder. Weaker —
  the model can drift — but the retry loop and lenient parsing already handle
  it.
- **Batches rejected.** The two endpoints want the provider named in different
  places: `/messages` reads it from the `@slug/model` prefix, while
  `/messages/batches` wants an `x-portkey-provider` header *and* a bare model
  name. Run `python check_batches.py` to find the accepted spelling, then set:

  ```powershell
  $env:MW_PORTKEY_PROVIDER = "@your-account"    # note the @
  ```

  Leave `MW_MODEL_PREFIX` alone. `batch_pipeline.py` strips it and adds the
  header itself, so one configuration drives both paths. If nothing works, use
  `pipeline.py` — identical output, roughly $190 more across a 3,000-frame run.

For any other gateway set `MW_BASE_URL` and `MW_EXTRA_HEADERS` (a JSON object)
directly.

To avoid retyping these every session, put them in `~/.zshrc` or `~/.bashrc`.

### Updating an existing install

Two archives exist and they are not interchangeable:

| archive | contains | use for |
|---|---|---|
| `math-worksheets.zip` | scripts **plus 20 sample frames** in `input/` | first install only |
| `math-worksheets-code-only.zip` | scripts and README only | every update afterwards |

Unzip the code-only archive over your existing folder and choose "replace files
in the destination". **Replace every file** — skipping even one leaves modules
from different releases, which surfaces as a confusing `TypeError` deep inside
a build rather than an obvious version error. `smoke_test.py` checks for this
first and names any module that is out of step. It contains no `input/` or `output/`, so your frames, your
extractions and any review decisions cannot be touched. Then run
`python smoke_test.py` to confirm.

Do **not** unzip the full archive over a working folder — it would add the 20
sample frames back into your own frame set, and they would be processed as if
they were yours.

You do not need to update after every change. Update when a fix affects what
you are about to do; otherwise keep running what you have.

## 2. Check the install before spending anything

```bash
python3 omml.py          # LaTeX -> Word equations
python3 verifier.py      # 14 SymPy cases, including answers it should reject
python3 smoke_test.py    # library logic, end to end on fixtures
python3 cli_test.py      # runs every offline command in a throwaway workspace
```

`cli_test.py` exists because the other three never invoke the command line: a
command can raise on its first line while every library test passes. It copies
the project to a temp directory, populates fixtures, and runs all 28 offline
commands with the flag combinations that have broken before. Run it after any
edit to `pipeline.py`.

All four make **zero API calls**. If they pass, the environment is good and
anything that goes wrong later is model output, not plumbing. If `omml.py`
fails, `pip install -r requirements.txt` did not finish.

## 3. Point it at your frames

Either layout works and is detected automatically:

```
input/video001.png            input/video001/frame.png
input/video002.png     OR     input/video002/frame.png
```

The flat form is what a synced Google Drive, Box, or OneDrive folder looks like.
Leave your frames where they are and point at them instead of copying:

```bash
export MW_INPUT=~/Library/CloudStorage/GoogleDrive-you/My\ Drive/math-frames
export MW_OUTPUT=~/math-worksheets-out
```

The 20 frames from the pilot ship in `input/` so you can run immediately.

## 4. The pilot

```bash
bash run.sh 50
```

(`bash run.sh`, not `./run.sh` — the executable bit does not survive a download.
On Windows use `run.bat 50`, or just run the commands below by hand.)

That walks the whole sequence and pauses for the review step. To drive it by
hand instead, the sequence is:

```bash
python3 pipeline.py extract --limit 50   # read the boards
python3 pipeline.py index                # build the searchable index
python3 pipeline.py review               # -> output/review/review.html
#   open it, work the queue, click "Download decisions"
python3 review.py apply ~/Downloads/decisions.json
python3 pipeline.py solve                # solve + verify with SymPy
python3 pipeline.py mcq                  # build the distractors
python3 pipeline.py qa                   # deterministic checks
python3 pipeline.py build                # -> output/05_docx/*.docx
python3 db.py stats                      # what you ended up with
```

**Review between `extract` and `solve`, not at the end.** That is the only point
where a correction still changes what gets solved, and where rejecting a
question saves you paying to solve it.

### Judging the questions before worrying about the document

Whether the questions are good and how they should look on paper are separate
jobs. Stop after `qa` and read them in the terminal:

```powershell
python pipeline.py extract --limit 20
python pipeline.py solve
python pipeline.py mcq
python pipeline.py qa
python pipeline.py preview --why
```

`preview` prints each stem with its four options lettered exactly as `build`
would letter them, marks the correct one, and with `--why` shows the
misconception behind each distractor. Filter with `--difficulty easy` or
`--topic integral`.

**`build` costs nothing and calls no API.** It only reads the JSON that already
exists, so once a pilot has run you can rebuild as often as you like. Edit
`docx_builder.py` — fonts, margins, spacing, the answer-key layout — and run
`python pipeline.py build` again to see the change. There is no reason to
avoid it while iterating on formatting.

### If a run is interrupted

Stages resume, so re-running costs nothing for finished work. Two things to know
after a `Ctrl-C` or a token-limit failure:

**Run `qa` before `build`.** `run` does `extract -> solve -> mcq -> qa -> build`,
so stopping partway can leave a QA report older than the questions. `build` warns
when it spots this, but the holds for anything newer are simply unknown until you
re-run `qa` — which is free.

**`qa` is the slow step, not a hang.** It runs SymPy comparisons on every item.
Expect roughly half a second per item, so a few thousand questions takes minutes,
not seconds. It is parallel across items and batches each item's comparisons into
one subprocess; without that it ran seven times slower.

### What re-running overwrites

Every stage writes JSON and **skips work that is already done**, so `Ctrl-C` is
safe, re-running resumes where it stopped, and nothing is re-paid for.

| you run | effect |
|---|---|
| `extract` / `solve` / `mcq` again | finished items skipped; only missing ones are processed |
| any stage with `--force` | those items are redone and their files overwritten |
| `extract --force` on a **reviewed** frame | refused — see below |
| `qa` | report regenerated from scratch, every time |
| `build` | worksheets regenerated; previously generated `worksheet_set*.docx` are deleted first so a shorter run cannot leave stale sets behind |
| `index` | rebuilt from the JSON, but the `reviews` table is preserved |
| `review.py apply` | edits statements in place, keeps the original alongside, deletes downstream files for corrected questions so they regenerate |

**Your review work is protected.** `--force` rewrites an extraction file
wholesale, which would take corrected statements, preserved originals and
rejections with it. Reviewed frames are therefore skipped and listed:

```
  1 frame(s) carry review decisions and were skipped:
      9HW30_Practice_5
  re-read them anyway with --discard-reviews (their corrections are lost)
```

Model output is cheap to regenerate; your review time is not.

**One thing to copy out.** `build` deletes the worksheets it generated before
rebuilding. If you hand-edit a worksheet in Word, save it under a different
name or outside `output/05_docx/`.

## 5. The full run, at half price

Everything below is also in `run_batch.ps1` — `powershell -File run_batch.ps1`.

Batches need the provider named in a header, so set this first (with the `@`):

```powershell
$env:MW_PORTKEY_PROVIDER = "@your-account"
```

Then the same three stages, submitted and collected:

```bash
python3 batch_pipeline.py submit  extract
python3 batch_pipeline.py collect extract --wait

python3 pipeline.py index && python3 pipeline.py review    # review pass
python3 review.py apply ~/Downloads/decisions.json

python3 batch_pipeline.py submit  solve
python3 batch_pipeline.py collect solve --wait             # runs the SymPy checks
python3 batch_pipeline.py resolve-refuted                  # small repair pass

python3 batch_pipeline.py submit  mcq
python3 batch_pipeline.py collect mcq --wait

python3 pipeline.py qa && python3 pipeline.py build && python3 pipeline.py index
```

A batch is submitted all at once and cannot escalate mid-flight, which is what
`resolve-refuted` is for.

> Before a long batch run, confirm batches **complete**, not just that they can
> be created: `python check_batches.py --full`. A gateway can accept the create
> call, return a real batch object, and never route the enqueued requests —
> which looks exactly like a batch stuck at zero for hours. Two minutes of
> checking beats ninety minutes of waiting.

### Waiting on a batch

```powershell
python batch_pipeline.py status extract
```

Most batches finish within an hour; the hard limit is 24, and anything that
expires past it is not billed. Ten or twenty minutes with nothing succeeded yet
is normal.

Two things that surprise people:

**Results arrive all at once.** Requests are processed independently and the
counts move as they land, but nothing is retrievable until the whole batch
reaches `ended`. There is no partial download.

**There is no callback.** Polling is the only mechanism, which is why `collect
--wait` loops rather than waiting on an event. `--poll-seconds` controls the
interval.

Worth starting the next stage's thinking while you wait, but not the next
submission — solve needs the extractions, mcq needs the solves.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `ANTHROPIC_API_KEY` errors | Key not exported, or you used a claude.ai login rather than a Console API key |
| `0 videos found` | `MW_INPUT` points at the wrong folder, or the frames are not `.png/.jpg/.jpeg/.webp` |
| `ModuleNotFoundError` | Virtualenv not activated — re-run `source .venv/bin/activate` |
| Every answer says `error` | Ran out of memory or SymPy is missing; try `MW_VERIFY_TIMEOUT=120` |
| `TypeError` on `output_config` | `anthropic` SDK too old — `pip install -U anthropic` |
| Gateway returns 404 or "model not found" | `MW_MODEL_PREFIX` wrong — it must match your provider slug, e.g. `@myaccount` |
| Gateway returns prose instead of JSON | `output_config` was stripped; set `MW_JSON_MODE=prompt` |
| `Could not find a version that satisfies mathml2omml` | You have an old `requirements.txt`; the correct pin is `mathml2omml>=0.0.2` (0.0.2 is the newest that exists) |
| Equations show as `[\frac{1}{2}]` in Word | A LaTeX fragment failed to convert; `build` reports the count |

`examples/` holds real output from the 20-frame pilot — a worksheet, its answer
key, the QA report, and one extraction showing a seven-part board — so you can
see the shape of things before running anything.

---

## What losing the transcript actually cost

The transcript was doing three jobs. They do not degrade equally.

**Intent — partly recoverable, and dangerous.** An instructor says "find the
slope of the tangent at x equals three" while writing only `x² + y² = 25`. With
frames alone the instruction may simply not exist in the evidence. The model
will usually guess right, and a confident correct guess is indistinguishable
from a reading unless it is marked. So stage 1 returns `task_is_explicit`: true
only when the instruction is physically on the board. Inferred items are held
for a human by default (`REQUIRE_EXPLICIT_TASK`). This is the single most
important flag in the pipeline — the mathematics can be flawless and the
question still wrong, and nothing downstream can detect it.

**The answer — recoverable by computation, and now stronger than before.** The
transcript carried the worked solution and acted as an external answer key.
Gone, the model becomes both solver and sole witness to its own correctness.
The replacement is `verifier.py`: the solve stage returns its answer *plus a
structured description of a check*, and SymPy runs that check independently.
The integral is computed, not asserted. In practice this is a better anchor
than the transcript ever was, because instructors misspeak and SymPy does not.

**Handwriting disambiguation — genuinely lost.** "x squared plus y squared
equals twenty-five" used to confirm a smudged board. There is no second source
now. Stage 1 reports `legibility` and `unreadable_items`, escalates to a
stronger model when either is bad, and refuses to invent a coefficient. An item
held for review is the correct outcome here; a plausible invented coefficient is
worse, because nobody will catch it.

> If you still have the videos, `faster-whisper` will transcribe 3,000 short
> clips locally in a day or two for the cost of electricity, and every stage
> below uses a transcript automatically if one appears next to the frame. It
> would mainly buy back the third item. The first two are already handled.

---

## Pipeline

```
frame.png
   │
   ▼
Stage 1  read the board                    → 01_extracted/   statement, task_is_explicit,
         (vision only, no solving)                           legibility, board transcription
   │
   ▼
Stage 2  solve + specify a check           → 02_solved/      answer + verification spec
   │
   ├─► verifier.py runs the check in SymPy
   │       verified  → done
   │       refuted   → re-solve on a stronger model, re-check
   │       skipped   → second independent solve must agree
   ▼
Stage 3  design distractors                → 03_mcq/         stem, answer, 3 distractors
         around the verified answer
   │
   ▼
QA + build                                 → 04_qa/, 05_docx/
```

Stage 1 does not solve, on purpose. A model that reads and solves in one breath
lets a hoped-for answer bend its reading of a smudged coefficient. Stage 2 is
separate from stage 3 so that a refuted answer costs one solve rather than a
whole discarded item.

---

## The offline checks in detail

`smoke_test.py` runs five fixtures — one clean, four with a specific planted
defect — and should hold four of them:

```
video001 clean item               pass
video002 duplicate options        HOLD   0.5 equals the correct answer 1/2
video003 answer leaked into stem  HOLD
video004 task was inferred        HOLD   right mathematics, possibly wrong question
video005 answer refuted by SymPy  HOLD   claimed 3/4, computer algebra says -3/4
```

Open `output/smoke/smoke_worksheet.docx` in Word. Every equation should be a
live, editable equation object, not text and not a picture.

## Input layout

or, equivalently, a flat folder:

```
input/
  video001.png
  video002.png
  ...
```

The layout is auto-detected — loose image files mean flat, subdirectories mean
one folder per video. Force it with `MW_LAYOUT=flat` or `MW_LAYOUT=nested`.
`.png`, `.jpg`, `.jpeg` and `.webp` are picked up; anything else is ignored, so
stray files in the folder are harmless.

**The filename becomes the id.** `9HW30_Practice_5.png` produces questions
`9HW30_Practice_5-q1`, `-q2`, and so on, and that id is how you trace a
worksheet question back to its video. Names are sanitised to
`A-Za-z0-9_-` for the Batches API, which means two files can collapse onto one
id — `9HW30 1.png` and `9HW30-1.png` both become `9HW30-1`, and the second
would silently overwrite the first. Check before a big run:

```powershell
python pipeline.py inputs
```

It lists what would be processed and flags collisions, over-long names, and
empty files. Takes a second and costs nothing.
A transcript is used if present (`transcript.txt` alongside a frame, or
`video001.txt` next to `video001.png`) but nothing requires one.

### Working from Google Drive, Box, or OneDrive

If your frames live in cloud storage, mirror the folder to disk and point the
pipeline at the mirror. Cloud storage is the pipeline's storage layer, not its
processing path:

```bash
# Google Drive for Desktop / Box Drive / OneDrive all mount a local folder.
# Or with rclone, for any of them:
rclone sync gdrive:math-frames ~/math-frames

export MW_INPUT=~/math-frames          # flat layout auto-detected
export MW_OUTPUT=~/math-worksheets-out
python pipeline.py run
```

Point `MW_OUTPUT` at a synced folder too and the finished worksheets, answer
keys, `review.db`, and `review.html` land back in the cloud, shareable with a
TA without another copy step. Nothing else in the pipeline changes.

A storage connector inside Claude cannot substitute for this. It changes how
files reach the model, not how many fit: a whiteboard frame is ~1,845 tokens,
so 3,000 of them are 5.5M tokens — about 28x the chat context window. Even with
perfect access to your Drive, a chat session tops out around 70 frames.

## Running

Pilot of 25, synchronously — you want to read the output while it is still in
your head:

```bash
python pipeline.py run --limit 25
```

Full run on the Batches API, every token at 50%:

```bash
python batch_pipeline.py submit  extract
python batch_pipeline.py collect extract --wait
python batch_pipeline.py submit  solve
python batch_pipeline.py collect solve --wait     # runs the SymPy checks too
python batch_pipeline.py resolve-refuted          # repair pass, small slice
python batch_pipeline.py submit  mcq
python batch_pipeline.py collect mcq --wait

python pipeline.py qa
python pipeline.py build
python pipeline.py index
```

Insert a review pass after `collect extract` on a real run — see
[Human review](#human-review-of-extracted-statements). Rejecting a problem
before the solve batch is submitted is the cheapest rejection available.

A batch is submitted all at once, so nothing can escalate mid-flight;
`resolve-refuted` re-solves the contradicted answers on the stronger model
afterwards. `python pipeline.py reverify` re-runs every symbolic check against
saved solves and costs nothing, which is useful after touching `verifier.py`.

### Human review of extracted statements

Review belongs **between stage 1 and stage 2**, not at the end. That is the only
point where a correction still changes what gets solved, and where rejecting an
item saves you paying to solve and build distractors for it.

```bash
python pipeline.py extract          # stage 1 only
python pipeline.py review           # builds output/review/review.html
# open it, work through the queue, click Download decisions
python review.py apply decisions.json
python pipeline.py solve            # rejected items are skipped
```

The page puts the whiteboard frame beside the extracted statement, because
that is the only way to review a transcription — no amount of reading the text
tells you whether the board really said `x³`. The statement is editable with a
live LaTeX preview, flags are shown worst-first, and `A` / `S` / `R` plus arrow
keys drive the whole thing so you are not reaching for the mouse 200 times.
Frames are referenced by relative path rather than embedded, so the file stays
a couple of megabytes even for thousands of items.

**You are not reviewing 3,000 problems.** The queue is ranked by a risk score
built from signals the pipeline already collects, weighted by how badly the
failure lands on a student rather than how likely it is:

| Signal | Weight | Why |
|---|---|---|
| task was inferred, not written | 100 | a confidently wrong question, undetectable downstream |
| answer refuted by computer algebra | 90 | wrong mathematics |
| board partly illegible | 50 | no second source to check against |
| specific unreadable symbols | 40 | a misread coefficient nobody catches |
| extraction confidence < 0.85 | 30 | model is unsure |
| answer unverified | 25 | no mechanical check applied |
| depends on a figure | 20 | worksheet will not carry it |
| board also showed worked steps | 10 | statement may be contaminated |
| held by QA | 15 | something else fired |

`python db.py queue` prints the same ranking as text. Review down to risk 0 and
stop; the tail is items where every signal came back clean.

Decisions flow back into the pipeline:

- **approved** — nothing changes
- **corrected** — the statement is rewritten in the extraction JSON, the
  original kept as `statement_original`, and any stale solve/mcq output for
  that video is *deleted* so it regenerates rather than silently surviving
- **rejected** — marked `review_rejected`; stage 2 skips it and it never
  reaches a worksheet

### Blocked, flagged, clean

Failed checks are not equal, so they are not treated equally.

**Blocking** means the item cannot be used as written: two options that are the
same number, LaTeX that will not render in Word, the answer printed in the stem,
an answer computer algebra refuted. These never ship, in any mode. They are rare
and every one is a genuine defect.

**Advisory** means something was uncertain — a confidence score below a
threshold, a task inferred rather than written. These ship, and are listed in
`04_qa/concerns.csv` and annotated in the answer key. Measured on a real pilot,
about nine in ten items held for low confidence were correct anyway, so holding
them cost a lot of reading and caught very little.

```
QA (flag mode): 62 items — 31 clean, 29 to spot-check, 2 unusable
  unusable as written (never shipped):
       2x  distractor is mathematically equal to the correct answer
  worth a glance (shipped, listed in the concerns report):
      15x  extraction confidence
      11x  the task was inferred, not written on the board
  -> output/04_qa/concerns.csv
```

`MW_HOLD_MODE=strict` restores the old behaviour, where low confidence also
holds the item.

To read the flagged questions in Word rather than a spreadsheet:

```powershell
python pipeline.py build --only-held
```

Three views, and they do not overlap:

| command | contains | on the worksheet? |
|---|---|---|
| `build --only-held` | questions kept **off** the worksheets | no |
| `build --only-flagged` | questions that **shipped** carrying a concern | yes |
| `build --only-malformed` | questions whose answer-choice count is not 4 | no |

A question is kept off the worksheet only by a *blocking* error that has not
been released, or by having the wrong number of choices. Advisory concerns —
low confidence, an inferred task — do not exclude anything in flag mode, so
those questions appear in `--only-flagged`, not `--only-held`.

**One document per level**, matching how the worksheets are grouped:

```
held_1HW.docx              excluded from the 1HW worksheets
flagged_1HW.docx           shipped, but worth a look
wrong_choices_1HW.docx     not 4 answer choices
```

Each entry is laid out for copying. The stem and its four choices sit together
with nothing between them — no correct-answer marker, no explanations — so the
block can be selected and pasted straight into a worksheet, in the same font
and indents so it needs no reformatting. Which option is right, the source, the
misconceptions and any `CHECK:` concern all appear underneath in small grey
text.
Items are identified by homework and video (`1HW25 · video 2`) rather than the
raw filename. `--hw` narrows it to one level and leaves the other files alone.

**Questions with the wrong number of choices** get their own pass. The schema
cannot enforce "exactly three distractors" — structured outputs has no
`maxItems` — so a model occasionally returns four or two:

```powershell
python pipeline.py build --only-malformed     # wrong_choices_1HW.docx, ...
```

These are excluded from student worksheets whatever you do, because a
five-option question is wrong on the page regardless of how good it is.
Releasing will not help. Regenerate instead — delete their files from
`03_mcq/` and re-run `python pipeline.py mcq`, which re-does only those
questions for a few cents.

Review documents (`held_*`, `wrong_choices_*`) are never deleted by a normal
`build`, so you can keep them open while regenerating worksheets.

### Releasing held questions

A hold means a check failed, not that you agree with the check. Read the held
items, then release the ones you judge fine:

```powershell
python pipeline.py release --list                 # what is already released
python pipeline.py release 1HW25-...-PRACTICE-2-q1 ...   # specific ones
python pipeline.py release --all                  # everything currently held
python pipeline.py release --clear                # undo
python pipeline.py build                          # regenerate
```

Releases are recorded in `output/04_qa/released.json` and survive every later
rebuild and QA run, so the judgement is made once.

**Released questions return to their proper place**, not the end. Ordering is
computed at build time from the question id, so a released item slots back into
its HW and video position and the problem numbers and answer key renumber
around it. The concern still prints in the answer key as `CHECK: ...`, so a
released item is never silently laundered.

`--all` on a large set is worth pausing over. Blocking holds are the checks with
real precision — a duplicated option makes an item unanswerable no matter how
good the question looks.

`qa` prints the causes grouped. A high rate is usually one systematic rule, not
many separate problems, so read the breakdown before changing thresholds:

```powershell
python pipeline.py qa
python pipeline.py preview --held --why      # the actual items, with reasons
```

The four thresholds behind most holds, and what loosening each one costs you:

| variable | default | effect |
|---|---|---|
| `MW_HUMAN_REVIEW_BELOW` | 0.80 | any stage reporting confidence under this holds the item |
| `MW_REQUIRE_EXPLICIT_TASK` | 1 | hold when the instruction was inferred rather than written |
| `MW_REQUIRE_VERIFIED` | 0 | hold when no symbolic check confirmed the answer |
| `MW_ESCALATE_BELOW` | 0.85 | also the bar for "confident" in the inferred-task rule |

`MW_REQUIRE_EXPLICIT_TASK` no longer holds on the flag alone. An inferred task
ships if the reading was confident *and* computer algebra verified the answer,
carrying a warning instead — a bare equation has no written instruction and
still has one sensible reading. It holds when the evidence is weak in some
other way too.

Set `MW_REQUIRE_EXPLICIT_TASK=0` to demote it to a warning in every case. Do
that only after reading a dozen held items: an invented question is the one
defect nothing downstream can catch.

### Escalation, and what it costs you

Each stage runs on a cheap model first. When the result looks unreliable the
pipeline reruns it on a stronger one — that rerun is *escalation*. Three places
trigger it:

| stage | escalates when | reruns on |
|---|---|---|
| 1 extract | the board looks badly read: illegible, unreadable symbols, no questions found, or at least half its questions score low | `MW_ESCALATION_MODEL` |
| 2 solve | **SymPy refutes the answer** — the strongest signal in the pipeline | `MW_ESCALATION_MODEL` |
| 2 solve | no symbolic check applied: solves again and requires the two answers to agree | `MW_ESCALATION_MODEL` |
| 3 mcq | distractor confidence below `MW_ESCALATE_BELOW` | `MW_ESCALATION_MODEL` |

An escalated item costs roughly **three times** a clean one — the first call
plus a second on a pricier model. On a pilot of difficult frames nearly
everything escalates, which is why measured cost can land far above a estimate
built on clean-board assumptions.

Stage 1 escalation is deliberately about the *board*, not one question on it.
A seven-part frame with six clean questions and one shaky one ships the six and
sends the one to review; rereading all seven would triple the cost of six
answers that were already fine. Tune with `MW_ESCALATE_SHARE` (default 0.5).

After any run, `python pipeline.py estimate` reports the escalation rate per
stage and the reasons, read straight off the saved JSON — no extra calls. To
measure what it costs, run the same frames twice into separate folders and
compare the `~$` lines:

```powershell
$env:MW_OUTPUT = "output_esc"
python pipeline.py extract --limit 50

$env:MW_OUTPUT = "output_noesc"
python pipeline.py extract --limit 50 --no-escalate
```

Separate folders matter: same folder means the second run skips the finished
work, and `--force` would overwrite the first result and lose the comparison.

Stage 2 escalation is the one worth keeping whatever the price: it only fires
when computer algebra has *proved* the answer wrong.

### The searchable index

`python db.py build` indexes every JSON file into `output/review.db`. The JSON
files stay the source of truth — nothing here modifies them — so the index can
be thrown away and rebuilt at any time.

```bash
python db.py stats
python db.py queue --limit 40
python db.py sql "SELECT video_id, statement FROM extractions WHERE topic LIKE '%integral%'"
python db.py sql "SELECT misconceptions, COUNT(*) FROM items GROUP BY misconceptions"
```

Tables: `extractions`, `solutions`, `items`, `qa`, `reviews`, plus the
`review_queue` view that carries the risk score.

### The QA report

`04_qa/qa_report.csv`, one row per item:

```
video_id   status  verification  task_explicit  answer   errors
video001   pass    verified      True           -3/4
video004   HOLD    verified      False          -3/4     task was inferred, not written on the board
video005   HOLD    refuted       True           3/4      computer algebra refutes the answer: dy/dx is -3/4
```

Those two holds are the two distinct failure modes of a frames-only pipeline,
and they are worth reading as a pair. `video004` has correct mathematics and
possibly the wrong question. `video005` has the right question and wrong
mathematics. Different problems, different fixes, both invisible to a confidence
score.

---

## Design notes

### The model never chooses A, B, C, or D

It returns one correct answer and three distractors, unlabeled. Lettering
happens at render time via a shuffle seeded on the video ID. Language models
have a pronounced positional bias when placing a correct answer; left alone you
get a set where the answer is disproportionately B, and students learn to guess
B. Change `SHUFFLE_SALT` to generate a second form with different lettering.

### Distractors are completed mistakes, not perturbations

Each one must be the endpoint of a specific error carried all the way through
the arithmetic, tagged from a fixed taxonomy in `schemas.py`. For `dy/dx` at
`(3,4)` on `x² + y² = 25`:

| Option | Category | What the student did |
|---|---|---|
| −3/4 | — | correct |
| 3/4 | `sign_error` | moved a term without flipping the sign |
| 4/3 | `reversed_operation` | found the normal slope, or inverted x/y |
| 3/8 | `chain_rule_omitted` | differentiated y² as 2y, losing dy/dx |

Because the taxonomy is fixed, the answer key ends with a frequency table, and
across a term you can ask which errors your cohort actually makes.

### Verification is templates, not generated code

The model fills one small template — `definite_integral`, `implicit_derivative`,
`equation_roots`, and six others — and `verifier.py` assembles the SymPy call.
No arbitrary execution path. Expressions still go through `parse_expr`, which is
not a security sandbox, so every check also runs in a subprocess with a memory
cap and a wall-clock timeout.

The timeout is not paranoia. `integrate` and `simplify` genuinely hang on some
inputs, and at 3,000 items you will hit it. A timeout records as *unverified*,
never as a wrong answer — the distinction matters, since treating "SymPy gave
up" as "the model is wrong" would throw away good problems.

Coverage is the honest limitation: roughly the algebra and calculus core.
Proofs, graph sketches, and open-ended explanations get `kind: none` and fall
through to the second-opinion path, where a second independent solve on a
stronger model has to agree symbolically.

### Everything else that can go wrong mechanically

`qa.py` also catches duplicate options after simplification (`1/2` and `0.5`
shipping together makes an item unanswerable and looks fine to a human
skimming), LaTeX that will not render in Word, the answer leaking into the stem,
format tells where the correct option is conspicuously longer or shorter, and
answer drift between the option that was verified and the option that reached
the worksheet.

### Equations are native Word equations

`omml.py` converts LaTeX → MathML → OMML and injects it into the paragraph XML,
so an integral behaves exactly like one typed with Word's equation editor.
Failures fall back to bracketed literal LaTeX rather than vanishing, and `build`
reports the count.

> `mathml2omml` emits `<m:rad>` without the children the OOXML schema requires,
> so any document containing a radical fails validation. `omml._patch_radicals`
> inserts them. Both generated documents pass full schema validation.

---

## Cost

Rates as of August 2026; verify at
[platform.claude.com/docs/en/about-claude/pricing](https://platform.claude.com/docs/en/about-claude/pricing).

| Configuration | 3,000 videos |
|---|---|
| All Sonnet 5, batch API | ~$51 |
| Opus 5 for solving only (the default), batch API | ~$82 |
| Add second opinions and refuted re-solves | + ~$15 |
| Everything synchronous instead of batched | ×2 |

The default spends Opus money on stage 2 only. With no transcript there is no
external answer key, and a wrong answer there poisons every option in the item —
this is the one stage where the stronger model is worth paying for by default.
Set `MW_SOLVE_MODEL=claude-sonnet-5` and compare on your pilot before accepting
that.

Two caveats. Sonnet 5's $1/$5 batch rate is introductory and rises to $1.50/$7.50
on September 1, 2026. And the widest error bar is what fraction of your problems
get a symbolic check rather than falling through to a second solve — your pilot
will report this directly as the `verification` column distribution.

Frames are downscaled to 1568px on the long edge before sending, which is where
Claude resizes anyway. A 1920×1080 frame is ~2,765 tokens; the same frame at
1568×882 is ~1,845.

---

## What to do first

Run 50, not 3,000. Pick them deliberately: clean typed boards, bad handwriting,
boards where the instruction is written and boards where it clearly is not,
algebra, calculus, geometry with figures.

```bash
python pipeline.py run --limit 50
```

Then read `04_qa/qa_report.csv` top to bottom. Three questions, in order:

0. **Open the review queue and work through it.** `python pipeline.py review`.
   Fifty items takes about fifteen minutes with the keyboard shortcuts, and it
   is the only way to find out whether the statements are actually right — the
   automated checks cannot tell you that.
1. **How many are held for `task_is_explicit: false`?** This is the number that
   decides whether frames-only is viable for your library. A handful is fine.
   A third of them means the instruction usually lived in the audio, and
   transcribing with Whisper will pay for itself immediately.
2. **What does the `verification` column look like?** High `verified` means the
   answers are anchored by computation and you can trust the set. Mostly
   `skipped` means your problems are outside SymPy's reach and you are leaning
   on two-model agreement instead — usable, but read more keys.
3. **Are the distractors any good?** The CSV cannot answer this; only you can.
   Read twenty answer keys. If distractors feel arbitrary, add worked examples
   from your own subject areas to `MCQ_SYSTEM` in `prompts.py`. Two or three
   concrete examples move quality more than a model upgrade does.

Where I expect trouble: problems whose answer is a proof or a graph do not
become multiple-choice questions cleanly — they are flagged, not solved. And
figure-dependent geometry produces a stem describing the figure in words, which
is a real degradation; redraw those or set them aside.

---

## Files

| File | Purpose |
|---|---|
| `config.py` | Models, pricing, thresholds, paths, formatting |
| `schemas.py` | JSON schemas for structured outputs; misconception taxonomy |
| `prompts.py` | All three system prompts, versioned |
| `claude_client.py` | Image prep, schema-constrained calls, retries, cost tracking |
| `sources.py` | Input discovery, nested or flat; optional transcripts |
| `stage1_extract.py` | Frame → problem statement |
| `stage2_solve.py` | Problem → answer + verification spec |
| `verifier.py` | Runs the check in sandboxed SymPy |
| `stage3_distractors.py` | Verified answer → multiple-choice item |
| `qa.py` | Deterministic checks |
| `omml.py` | LaTeX → native Word equations |
| `docx_builder.py` | Worksheet and answer key |
| `pipeline.py` | CLI: `extract` / `solve` / `mcq` / `reverify` / `qa` / `build` / `index` / `review` / `run` |
| `batch_pipeline.py` | Batches API path, plus `resolve-refuted` |
| `db.py` | SQLite index; risk-ranked review queue; SQL access |
| `review.py` | HTML review page; writes decisions back into the pipeline |
| `smoke_test.py` | Offline verification |

Environment overrides: `MW_EXTRACT_MODEL`, `MW_SOLVE_MODEL`, `MW_MCQ_MODEL`,
`MW_ESCALATION_MODEL`, `MW_VERIFY_TIMEOUT`, `MW_SECOND_OPINION`,
`MW_REQUIRE_VERIFIED`, `MW_REQUIRE_EXPLICIT_TASK`, `MW_WORKERS`, `MW_INPUT`,
`MW_OUTPUT`, `MW_COURSE_TITLE`, `MW_SHUFFLE_SALT`.
