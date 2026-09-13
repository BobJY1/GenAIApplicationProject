# Whiteboard → worksheet

Thousands of recorded maths lessons, each ending on a whiteboard with a problem
on it — and no practice worksheets to go with them. This reads those frames and
writes multiple-choice questions, then **proves the answers with computer
algebra before any of them reaches a student**.

```
frame.png ──► read the board ──► solve ──► SymPy checks the answer ──► worksheet.docx
```

## Why the checking matters

A teacher has to be able to trust every answer on a worksheet, so the model does
not get the last word on whether its answer is right.

Every solve returns its answer *plus a description of a computation that would
confirm it*, and [`verifier.py`](verifier.py) runs that computation in SymPy —
evaluating the integral, differentiating the antiderivative back, substituting
the roots. The answer is checked by mathematics, not accepted on the model's
say-so.

On a pilot over 48 real lesson frames:

| | |
|---|---|
| answers confirmed by computer algebra | **88%** |
| questions produced from 20 frames | **43** — one board carried eight |

The remaining 12% are questions no symbolic check covers — proofs, graph
sketches — and they are marked as unconfirmed rather than quietly treated the
same as the rest.

## Why the questions are worth using

Every wrong answer is the **completed result of a specific mistake**, tagged
from a fixed list, rather than a random number near the right one. For `dy/dx`
at `(3,4)` on `x² + y² = 25`:

| option | | |
|---|---|---|
| −3/4 | | correct |
| 3/4 | `sign_error` | moved a term without flipping the sign |
| 4/3 | `reversed_operation` | found the normal slope instead |
| 3/8 | `chain_rule_omitted` | differentiated y² as 2y, losing dy/dx |

A student who makes one of those mistakes finds their answer waiting among the
choices. And because the categories are fixed, the answer key ends with a
frequency table: grading a worksheet tells a teacher *which* mistake a class is
making, not just how many got it wrong.

## What comes out

Word documents with real, editable equations — one file per homework, answer key
at the back. Questions the checks could not clear are not discarded quietly;
they go to a separate document with the reason printed under each one.

Real output from the pilot is in [`examples/`](examples): a worksheet, its
answer key, the QA report, and one extraction showing a board that held seven
separate questions.

## Running it

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python pipeline.py run --limit 5     # frames in input/ -> worksheets in output/
```

Full instructions, including batch processing and every configuration option,
are in **[USAGE.md](USAGE.md)**.

## Layout

| | |
|---|---|
| `pipeline.py` | the command line: extract, solve, mcq, qa, build |
| `verifier.py` | the SymPy checks, sandboxed in a subprocess |
| `prompts.py` `schemas.py` | what the model is asked, and the shape it must answer in |
| `docx_builder.py` `omml.py` | Word output, LaTeX converted to native equations |

