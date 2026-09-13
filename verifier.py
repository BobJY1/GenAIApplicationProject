"""Independent verification of the model's answer using SymPy.

With no transcript, nothing external says the answer is right. The model is
both the solver and the only witness to its own correctness, which is exactly
the situation you do not want with 3,000 worksheets downstream.

So the solve stage returns, alongside its answer, a structured description of a
check that would confirm it, and this module runs that check. The integral is
computed here, not asserted by the model. A refuted answer is caught before any
distractors get built around it.

Deliberately not free-form code. The model fills one small template and this
module assembles the SymPy call, so there is no arbitrary execution path.
Expressions still go through `parse_expr`, which is not a security sandbox, so
every check also runs in a subprocess with a wall-clock timeout and a memory
cap. The timeout is not paranoia: `integrate` and `simplify` genuinely hang on
some inputs, and at 3,000 items you will hit it.

One field set, disambiguated by `kind`:

    kind                  expr               var   other fields used
    -------------------   ----------------   ---   --------------------------
    definite_integral     integrand          d/dx  lower, upper, claimed
    indefinite_integral   integrand          d/dx  claimed (differentiated back)
    derivative            function           d/dx  order, point, claimed
    implicit_derivative   F, where F(x,y)=0  x     var2, point, point_y, claimed
    limit                 function           x     point, direction, claimed
    equation_roots        F, where F(x)=0    x     claimed_roots
    evaluate              expression         --    subs_json, claimed
    expressions_equal     left side          --    expr2
    none                  --                 --    --
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass

BUILD_ID = "2026-08-16.2"   # must match config.BUILD_ID

VERIFIED = "verified"
REFUTED = "refuted"
ERROR = "error"
SKIPPED = "skipped"

KINDS = [
    "definite_integral",
    "indefinite_integral",
    "derivative",
    "implicit_derivative",
    "limit",
    "equation_roots",
    "evaluate",
    "expressions_equal",
    "none",
]


@dataclass
class VerificationResult:
    status: str
    detail: str = ""
    kind: str = ""

    @property
    def ok(self) -> bool:
        return self.status == VERIFIED

    @property
    def contradicted(self) -> bool:
        return self.status == REFUTED

    def as_dict(self) -> dict:
        return {"status": self.status, "detail": self.detail, "kind": self.kind}


# ==========================================================================
# Worker source — executed in a separate interpreter
# ==========================================================================
_WORKER = r'''
import json, sys
try:
    import resource                      # Unix only; absent on Windows
    resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
except Exception:
    # No memory cap available. The wall-clock timeout on the host side is the
    # real protection anyway; losing the cap degrades safety, not correctness.
    pass

import sympy as _sp
from sympy import Symbol, simplify, integrate, diff, limit, solve, sympify, N
from sympy.parsing.sympy_parser import parse_expr, standard_transformations

VERIFIED, REFUTED, ERROR, SKIPPED = "verified", "refuted", "error", "skipped"

# sympy names only, no builtins, so parse_expr cannot reach __import__ or open()
SAFE = {"__builtins__": {}}
for _n in dir(_sp):
    if not _n.startswith("_"):
        SAFE[_n] = getattr(_sp, _n)

DEFAULT_SYMS = ["x", "y", "z", "t", "n", "u", "v", "a", "b", "c", "k", "r", "theta"]


def make_syms(names):
    return {n: Symbol(n) for n in (list(names or []) + DEFAULT_SYMS)}


def P(text, syms):
    if text is None or str(text).strip() == "":
        raise ValueError("missing expression")
    return parse_expr(str(text), local_dict=dict(syms), global_dict=SAFE,
                      transformations=standard_transformations)


def equal(a, b):
    """Symbolic equality, with a numeric fallback for expressions simplify stalls on.

    Returns (ok, how). `how` distinguishes a proof from a 30-digit numeric
    agreement -- weaker evidence, and the caller should say which it got.
    """
    if simplify(a - b) == 0:
        return True, "symbolically"
    try:
        if abs(complex(N(a - b, 30))) < 1e-20:
            return True, "numerically to 30 digits (simplify could not prove it)"
    except Exception:
        pass
    return False, ""


def verdict(got, want, label):
    ok, how = equal(got, want)
    if ok:
        return VERIFIED, "%s = %s, confirmed %s" % (label, got, how)
    return REFUTED, "%s = %s, but the answer claims %s" % (label, got, want)


def run(spec):
    kind = spec.get("kind", "none")

    if kind == "_compare_batch":
        # Many equality tests in one interpreter. Starting python and importing
        # sympy costs ~0.4s; doing that per comparison dominated everything.
        syms = make_syms(spec.get("symbols"))
        out = []
        for a, b in spec.get("pairs", []):
            try:
                out.append(equal(P(a, syms), P(b, syms))[0])
            except Exception:
                out.append(None)
        return VERIFIED, json.dumps(out)

    if kind == "none":
        return SKIPPED, "no mechanical check applies"

    syms = make_syms(spec.get("symbols"))
    var = syms[spec.get("var") or "x"]
    claimed = spec.get("claimed")

    if kind == "definite_integral":
        got = integrate(P(spec["expr"], syms),
                        (var, P(spec["lower"], syms), P(spec["upper"], syms)))
        return verdict(got, P(claimed, syms), "integral")

    if kind == "indefinite_integral":
        got = diff(P(claimed, syms), var)
        return verdict(got, P(spec["expr"], syms), "d/d%s of the claimed antiderivative" % var)

    if kind == "derivative":
        got = diff(P(spec["expr"], syms), var, int(spec.get("order") or 1))
        if str(spec.get("point") or "").strip():
            got = got.subs(var, P(spec["point"], syms))
        return verdict(got, P(claimed, syms), "derivative")

    if kind == "implicit_derivative":
        v2 = syms[spec.get("var2") or "y"]
        F = P(spec["expr"], syms)
        got = -diff(F, var) / diff(F, v2)
        if str(spec.get("point") or "").strip() and str(spec.get("point_y") or "").strip():
            got = got.subs({var: P(spec["point"], syms), v2: P(spec["point_y"], syms)})
        return verdict(got, P(claimed, syms), "dy/dx")

    if kind == "limit":
        pt = spec.get("point")
        to = sympify(pt) if str(pt) in ("oo", "-oo") else P(pt, syms)
        got = limit(P(spec["expr"], syms), var, to, spec.get("direction") or "+")
        return verdict(got, P(claimed, syms), "limit")

    if kind == "equation_roots":
        expr = P(spec["expr"], syms)
        roots = [P(r, syms) for r in (spec.get("claimed_roots") or [])]
        if not roots:
            return ERROR, "claimed_roots is empty"
        bad = [r for r in roots if not equal(expr.subs(var, r), sympify(0))[0]]
        if bad:
            return REFUTED, "these do not satisfy the equation: %s" % bad
        try:
            actual = solve(expr, var)
            if actual and len(actual) != len(roots):
                return REFUTED, "solve() finds %d roots %s, the answer lists %d" % (
                    len(actual), actual, len(roots))
        except Exception:
            pass
        return VERIFIED, "all %d roots substitute to zero" % len(roots)

    if kind == "evaluate":
        expr = P(spec["expr"], syms)
        subs = {syms[k]: P(v, syms) for k, v in json.loads(spec.get("subs_json") or "{}").items()}
        got = expr.subs(subs)
        return verdict(got, P(claimed, syms), "expression")

    if kind == "expressions_equal":
        a, b = P(spec["expr"], syms), P(spec["expr2"], syms)
        ok, how = equal(a, b)
        if ok:
            return VERIFIED, "%s equals %s, confirmed %s" % (a, b, how)
        return REFUTED, "%s and %s differ by %s" % (a, b, simplify(a - b))

    return ERROR, "unknown kind %r" % kind


try:
    status, detail = run(json.loads(sys.stdin.read()))
except Exception as exc:
    status, detail = ERROR, "%s: %s" % (type(exc).__name__, exc)

print(json.dumps({"status": status, "detail": str(detail)[:400]}))
'''


# ==========================================================================
# Host side
# ==========================================================================
def verify(spec: dict | None, timeout: int = 20) -> VerificationResult:
    """Run one verification spec. Never raises."""
    if not spec or spec.get("kind") in (None, "", "none"):
        return VerificationResult(SKIPPED, "no mechanical check applies", "none")

    kind = spec.get("kind")
    if kind not in KINDS and kind != "_compare_batch":
        return VerificationResult(ERROR, f"unknown kind {kind!r}", str(kind))

    try:
        proc = subprocess.run(
            [sys.executable, "-c", _WORKER],
            input=json.dumps(spec), capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        # Common and benign. SymPy stalls on hard integrals, which is not
        # evidence the answer is wrong, so this must never read as a refutation.
        return VerificationResult(ERROR, f"timed out after {timeout}s", kind)

    if proc.returncode != 0:
        return VerificationResult(ERROR, (proc.stderr or "worker crashed")[-300:], kind)

    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception:
        return VerificationResult(ERROR, f"unparseable worker output: {proc.stdout[:200]}", kind)

    return VerificationResult(payload["status"], payload.get("detail", ""), kind)


def compare_batch(pairs: list[tuple[str, str]], timeout: int = 60) -> list:
    """Equality for many pairs in a single subprocess.

    True / False / None per pair, None meaning undecidable. One interpreter
    start instead of one per pair, which is the difference between a QA pass
    taking minutes and taking hours.
    """
    NON = "NON_NUMERIC"
    usable, index = [], []
    for i, (a, b) in enumerate(pairs):
        if a and b and NON not in (str(a).upper(), str(b).upper()):
            index.append(i)
            usable.append([str(a), str(b)])

    results: list = [None] * len(pairs)
    if not usable:
        return results

    r = verify({"kind": "_compare_batch", "pairs": usable}, timeout)
    if r.status != VERIFIED:
        return results
    try:
        decoded = json.loads(r.detail)
    except Exception:
        return results
    for slot, value in zip(index, decoded):
        results[slot] = value
    return results


def agree(plain_a: str, plain_b: str, timeout: int = 25) -> bool | None:
    """Do two independently produced answers match? None if undecidable.

    The fallback anchor for problems no template covers. Two separate solves
    agreeing is weaker evidence than computation, but far better than one
    assertion.
    """
    if not plain_a or not plain_b or "NON_NUMERIC" in (plain_a.upper(), plain_b.upper()):
        return None
    r = verify({"kind": "expressions_equal", "expr": plain_a, "expr2": plain_b}, timeout)
    return True if r.status == VERIFIED else (False if r.status == REFUTED else None)


if __name__ == "__main__":
    cases = [
        ({"kind": "definite_integral", "expr": "x*sin(x)", "var": "x",
          "lower": "0", "upper": "pi", "claimed": "pi"}, VERIFIED),
        ({"kind": "definite_integral", "expr": "x*sin(x)", "var": "x",
          "lower": "0", "upper": "pi", "claimed": "2*pi"}, REFUTED),
        ({"kind": "implicit_derivative", "expr": "x**2 + y**2 - 25", "var": "x",
          "var2": "y", "point": "3", "point_y": "4", "claimed": "-3/4"}, VERIFIED),
        ({"kind": "implicit_derivative", "expr": "x**2 + y**2 - 25", "var": "x",
          "var2": "y", "point": "3", "point_y": "4", "claimed": "3/4"}, REFUTED),
        ({"kind": "equation_roots", "expr": "x**2 - 5*x + 6", "var": "x",
          "claimed_roots": ["2", "3"]}, VERIFIED),
        ({"kind": "equation_roots", "expr": "x**2 - 5*x + 6", "var": "x",
          "claimed_roots": ["2"]}, REFUTED),
        ({"kind": "indefinite_integral", "expr": "2*x", "var": "x", "claimed": "x**2"}, VERIFIED),
        ({"kind": "limit", "expr": "sin(x)/x", "var": "x", "point": "0", "claimed": "1"}, VERIFIED),
        ({"kind": "derivative", "expr": "x**2*sin(x)", "var": "x", "order": "1",
          "claimed": "2*x*sin(x) + x**2*cos(x)"}, VERIFIED),
        ({"kind": "derivative", "expr": "x**2*sin(x)", "var": "x", "order": "1",
          "point": "0", "claimed": "0"}, VERIFIED),
        ({"kind": "evaluate", "expr": "a*x + b", "subs_json": '{"a":"2","x":"3","b":"1"}',
          "claimed": "7"}, VERIFIED),
        ({"kind": "expressions_equal", "expr": "1/2", "expr2": "0.5"}, VERIFIED),
        ({"kind": "none"}, SKIPPED),
        ({"kind": "definite_integral", "expr": "((", "var": "x",
          "lower": "0", "upper": "1", "claimed": "1"}, ERROR),
    ]
    bad = 0
    for spec, expected in cases:
        got = verify(spec)
        bad += got.status != expected
        print(f"  {'ok ' if got.status == expected else 'FAIL'} {spec['kind']:22s}"
              f" -> {got.status:9s} {got.detail[:58]}")
    print("\nverifier selftest passed" if not bad else f"\n{bad} case(s) failed")
