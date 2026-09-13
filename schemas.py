"""JSON schemas passed to the API as `output_config.format`.

Structured outputs compile these into a grammar that constrains decoding, so
the model cannot emit JSON that violates them. Two rules matter here:

  * every object needs `additionalProperties: false`
  * numeric/length constraints (minimum, maxLength, pattern) are not supported,
    so ranges are expressed in the field descriptions instead

Everything is marked required. That keeps the compiled grammar small, avoids
the 24-optional-parameter ceiling, and makes output key order match schema
order, which makes diffing runs much easier. Fields that do not apply to a
given case take the empty string rather than being omitted.
"""

import verifier

# --------------------------------------------------------------------------
# A fixed taxonomy is what turns 3,000 worksheets into a dataset. Once every
# distractor carries a category you can ask "which errors does this cohort
# actually make?" instead of just "how many did they miss?"
#
# Enum casing is not guaranteed by structured outputs, so compare these
# case-insensitively downstream (qa.py does).
# --------------------------------------------------------------------------
MISCONCEPTIONS = [
    "sign_error",
    "arithmetic_slip",
    "wrong_formula",
    "misapplied_rule",
    "chain_rule_omitted",
    "incomplete_solution",
    "reversed_operation",
    "dropped_constant",
    "distribution_error",
    "fraction_error",
    "exponent_rule_error",
    "units_or_domain_error",
    "misread_the_problem",
    "plausible_lookalike",
]

DIFFICULTIES = ["easy", "medium", "hard"]
LEGIBILITY = ["clear", "mostly_clear", "partly_illegible"]


# ==========================================================================
# Stage 1: whiteboard frame -> problem statement
#
# No answer here. Without a transcript there is nothing on the board that
# reliably states one, and a model that reads and solves in the same breath
# tends to let a hoped-for answer bend its reading of a smudged coefficient.
# ==========================================================================
ANSWER_KINDS = ["expression", "interval", "set", "prose", "drawing"]

QUESTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "part_label": {
            "type": "string",
            "description": "The label written on the board for this part, e.g. '(1)', '[3]', 'b)'. Empty string if the board shows only one question.",
        },
        "statement": {
            "type": "string",
            "description": (
                "This one question, complete and self-contained, in the imperative. Inline math in "
                "single dollar signs, display math in double. It MUST repeat any setup it depends on: "
                "'Find the intervals where $f$ increases' is useless on a worksheet, whereas 'Let "
                "$f(x)=2x^3-3x^2-12x+12$. Find the intervals where $f$ increases' is a question. "
                "Exclude every solution step."
            ),
        },
        "task_is_explicit": {
            "type": "boolean",
            "description": (
                "True only if the instruction for THIS part is written on the board. False if you "
                "inferred it. Judge each part separately: a board can number its parts explicitly "
                "and still leave one of them unstated."
            ),
        },
        "task_inference_basis": {
            "type": "string",
            "description": "If task_is_explicit is false, what the inference rests on, naming any competing reading you rejected. Empty string otherwise.",
        },
        "answer_kind": {
            "type": "string",
            "enum": ANSWER_KINDS,
            "description": (
                "What shape the answer takes. 'expression' is a number or formula; 'interval' is "
                "one or more intervals such as $(-\\infty,-1)\\cup(2,\\infty)$; 'set' is a list of "
                "values such as critical points; 'prose' is an explanation or proof; 'drawing' is a "
                "sketch. Only 'expression' and 'set' can be checked symbolically."
            ),
        },
        "topic": {"type": "string", "description": "Specific topic for this part, e.g. 'concavity'."},
        "difficulty": {"type": "string", "enum": DIFFICULTIES},
        "confidence": {
            "type": "number",
            "description": "0.0 to 1.0 that this part is what was actually asked. Score each part on its own; one illegible part does not condemn the rest.",
        },
        "notes": {
            "type": "string",
            "description": (
                "Anything specific to this part. Critically: if the instruction IS written but the "
                "information needed to answer it is missing from the board -- an undefined constant, "
                "an unlabelled measurement, a blank left for speech -- say so here and drop "
                "confidence below 0.5. This is the most common defect in practice frames."
            ),
        },
    },
    "required": ["part_label", "statement", "task_is_explicit", "task_inference_basis",
                 "answer_kind", "topic", "difficulty", "confidence", "notes"],
}

EXTRACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "problem_title": {
            "type": "string",
            "description": "Title written on the board, e.g. 'Example 5'. Empty string if none.",
        },
        "board_transcription": {
            "type": "string",
            "description": (
                "Everything written on the board, transcribed literally, top to bottom, including "
                "anything you believe is a worked solution. LaTeX for math. A faithful record of the "
                "image, not a cleaned-up problem."
            ),
        },
        "shared_context": {
            "type": "string",
            "description": (
                "Setup common to every question on the board, e.g. 'Let $f(x)=2x^3-3x^2-12x+12$.' "
                "Empty string if there is none. Each question's statement must still stand alone; "
                "this field is for the record."
            ),
        },
        "questions": {
            "type": "array",
            "description": (
                "Every distinct question on the board, in the order written. A numbered or lettered "
                "list is that many questions, not one. A board reading '(1) intervals f increasing "
                "(2) intervals f decreasing ... (7) local max/min' is SEVEN questions. Do not merge "
                "them and do not answer only the first."
            ),
            "items": QUESTION_SCHEMA,
        },
        "board_shows_solution": {
            "type": "boolean",
            "description": "True if the frame includes worked steps or a final answer as well as the problems.",
        },
        "has_diagram": {
            "type": "boolean",
            "description": "True if a diagram, graph, or figure is essential to any question here.",
        },
        "diagram_description": {
            "type": "string",
            "description": "If has_diagram, describe the figure precisely enough to redraw it, including every label and measure. Otherwise the empty string.",
        },
        "legibility": {"type": "string", "enum": LEGIBILITY},
        "unreadable_items": {
            "type": "array",
            "description": "Specific symbols or numbers you could not read with confidence. Empty array if the board is clean.",
            "items": {"type": "string"},
        },
        "confidence": {
            "type": "number",
            "description": "0.0 to 1.0 for the reading of the board as a whole. Per-question confidence lives on each question.",
        },
        "notes": {
            "type": "string",
            "description": "Anything about the frame a human should know. Empty string if clean.",
        },
    },
    "required": ["problem_title", "board_transcription", "shared_context", "questions",
                 "board_shows_solution", "has_diagram", "diagram_description",
                 "legibility", "unreadable_items", "confidence", "notes"],
}


# ==========================================================================
# Stage 2: problem -> answer, plus a machine-checkable proof of that answer
#
# The `verification` block is the point of this stage. It is not the model
# restating its answer; it is a description of a computation that verifier.py
# runs independently. See verifier.py for the per-kind field table.
# ==========================================================================
VERIFICATION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {
            "type": "string",
            "enum": verifier.KINDS,
            "description": (
                "Which check confirms your answer. Prefer the most direct one. Use "
                "'expressions_equal' as a general fallback, and 'none' ONLY when no symbolic "
                "check is possible (proofs, graph sketches, verbal explanations)."
            ),
        },
        "symbols": {
            "type": "array",
            "description": "Every variable name appearing in the fields below, e.g. ['x','y']. Common names are predefined; list unusual ones.",
            "items": {"type": "string"},
        },
        "expr": {
            "type": "string",
            "description": (
                "The primary expression in SymPy notation. Meaning depends on kind: the integrand "
                "for integrals; the function for derivative/limit; F for implicit_derivative where "
                "the relation is F(x,y)=0; F for equation_roots where the equation is F(x)=0; the "
                "expression for evaluate; the left side for expressions_equal."
            ),
        },
        "expr2": {"type": "string", "description": "Right side, for expressions_equal only. Empty string otherwise."},
        "var": {"type": "string", "description": "Variable of integration/differentiation/limit, usually 'x'. Empty string if unused."},
        "var2": {"type": "string", "description": "Dependent variable for implicit_derivative, usually 'y'. Empty string otherwise."},
        "lower": {"type": "string", "description": "Lower limit for definite_integral. Empty string otherwise."},
        "upper": {"type": "string", "description": "Upper limit for definite_integral. Empty string otherwise."},
        "point": {
            "type": "string",
            "description": "Evaluation point: where a derivative is evaluated, what a limit approaches (use 'oo' for infinity), or the x-coordinate for implicit_derivative. Empty string otherwise.",
        },
        "point_y": {"type": "string", "description": "y-coordinate for implicit_derivative. Empty string otherwise."},
        "direction": {"type": "string", "description": "'+' or '-' for one-sided limits. Empty string otherwise."},
        "order": {"type": "string", "description": "Derivative order as a digit, e.g. '1' or '2'. Empty string otherwise."},
        "subs_json": {
            "type": "string",
            "description": "For evaluate: a JSON object string mapping variables to values, e.g. '{\"x\":\"3\",\"a\":\"2\"}'. Empty string otherwise.",
        },
        "claimed": {
            "type": "string",
            "description": "Your final answer in SymPy notation, the value the check must confirm. Empty string for equation_roots and expressions_equal.",
        },
        "claimed_roots": {
            "type": "array",
            "description": "For equation_roots: every root, in SymPy notation. Empty array otherwise. List them ALL; a missing root is a wrong answer.",
            "items": {"type": "string"},
        },
    },
    "required": [
        "kind", "symbols", "expr", "expr2", "var", "var2", "lower", "upper",
        "point", "point_y", "direction", "order", "subs_json", "claimed", "claimed_roots",
    ],
}

SOLVE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "restated_problem": {
            "type": "string",
            "description": "The problem in your own words, to confirm you read it as intended. One sentence.",
        },
        "solution_steps": {
            "type": "array",
            "description": "Your worked solution, one short step per element. Used to design distractors; never printed on the worksheet.",
            "items": {"type": "string"},
        },
        "final_answer_latex": {
            "type": "string",
            "description": "The final answer as bare LaTeX with no dollar signs, e.g. '-\\\\frac{3}{4}'.",
        },
        "final_answer_plain": {
            "type": "string",
            "description": (
                "The same answer in SymPy notation, e.g. '-3/4', '2*pi', 'sqrt(2)/2', 'x**2 + 1'. "
                "Use 'NON_NUMERIC' if the answer is prose rather than an expression."
            ),
        },
        "answer_is_expression": {
            "type": "boolean",
            "description": "True if the answer is a number or symbolic expression that can serve as a multiple-choice option.",
        },
        "verification": VERIFICATION_SCHEMA,
        "confidence": {
            "type": "number",
            "description": "0.0 to 1.0. Confidence that this answer is correct.",
        },
        "notes": {
            "type": "string",
            "description": "Assumptions you had to make, e.g. 'the problem did not specify a branch; assumed the first quadrant'. Empty string if none.",
        },
    },
    "required": [
        "restated_problem", "solution_steps", "final_answer_latex", "final_answer_plain",
        "answer_is_expression", "verification", "confidence", "notes",
    ],
}


# ==========================================================================
# Stage 3: verified problem + answer -> multiple-choice item
#
# No "A/B/C/D" here. Models have a strong positional bias when asked to place
# the correct answer among choices, so lettering is assigned at render time by
# a seeded shuffle instead (docx_builder.assign_labels).
# ==========================================================================
_OPTION_PROPS = {
    "latex": {"type": "string", "description": "The option as bare LaTeX with no dollar signs."},
    "plain": {
        "type": "string",
        "description": "The same option in SymPy notation, e.g. '-3/4', 'sqrt(2)/2'. Use 'NON_NUMERIC' for prose options.",
    },
}

MCQ_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "question_stem": {
            "type": "string",
            "description": (
                "The question as students will read it, self-contained, with math in $...$ or "
                "$$...$$. It must be answerable without the video."
            ),
        },
        "correct_answer": {
            "type": "object",
            "additionalProperties": False,
            "properties": dict(_OPTION_PROPS),
            "required": ["latex", "plain"],
        },
        "distractors": {
            "type": "array",
            "description": "Exactly three wrong answers. Each must be the actual result of a specific mistake carried through to completion, never a random perturbation.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    **_OPTION_PROPS,
                    "misconception": {"type": "string", "enum": MISCONCEPTIONS},
                    "student_reasoning": {
                        "type": "string",
                        "description": "The mistaken reasoning that produces this answer, in the student's voice, e.g. 'Differentiated y^2 as 2y and forgot dy/dx'.",
                    },
                    "plausibility": {
                        "type": "number",
                        "description": "0.0 to 1.0. How likely a student who studied but erred lands here. Below 0.5 means too obviously wrong to be useful.",
                    },
                },
                "required": ["latex", "plain", "misconception", "student_reasoning", "plausibility"],
            },
        },
        "confidence": {
            "type": "number",
            "description": "0.0 to 1.0. Confidence that all three distractors are genuinely wrong and genuinely tempting.",
        },
        "review_flags": {
            "type": "array",
            "description": "Reasons a human should look at this item. Empty array if none.",
            "items": {"type": "string"},
        },
    },
    "required": ["question_stem", "correct_answer", "distractors", "confidence", "review_flags"],
}
