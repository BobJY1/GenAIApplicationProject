"""LaTeX -> OMML, so equations land in Word as native editable equations.

The alternative is pasting equations as plain text or as screenshots. Both make
the worksheet unusable for the thing instructors actually do with worksheets:
open them and change a coefficient. OMML is Word's own equation format, so an
integral generated here behaves exactly like one typed with the equation editor.

Path: LaTeX -> MathML (latex2mathml) -> OMML (mathml2omml) -> injected into the
paragraph's XML tree.

One patch is applied on the way out. mathml2omml emits `<m:rad>` without the
`<m:radPr>`/`<m:deg>` children the OOXML schema requires, which makes every
document containing a radical fail validation. `_patch_radicals` inserts them.
"""

from __future__ import annotations

import re

BUILD_ID = "2026-08-16.2"   # must match config.BUILD_ID

_MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_NS_DECL = f'xmlns:m="{_MATH_NS}" xmlns:w="{_W_NS}"'

# `<m:rad>` must be radPr?, deg, e. Only patch when neither is already present,
# so \sqrt[3]{x} (which does emit its own deg) is left alone.
_BARE_RAD = re.compile(r"<m:rad>(?!\s*<m:radPr|\s*<m:deg)")
_RAD_FIX = '<m:rad><m:radPr><m:degHide m:val="on"/></m:radPr><m:deg/>'

# A binomial coefficient is a bar-less fraction inside parentheses. latex2mathml
# gets this right -- it emits <mfrac linethickness="0"> *already wrapped in its
# own ( and ) operators* -- but mathml2omml drops the linethickness hint, so
# \binom{n}{k} came out as n over k with a fraction bar, which means something
# entirely different.
#
# Only the bar needs removing. Adding a delimiter as well produced a second set
# of brackets, because the ones from the MathML are still there as ordinary
# characters.
#
# The MathML is matched rather than the LaTeX so every spelling is covered at
# once: \binom, \dbinom, \tbinom and the plain TeX {n \choose k}.
_ANY_MFRAC = re.compile(r"<mfrac\b[^>]*>")
_NOLINE_MFRAC = re.compile(r'<mfrac\b[^>]*linethickness="0"[^>]*>')

# `\vec`, `\hat`, `\overline` and friends emit <m:groupChrPr> that is closed
# with </m:groupChr> instead of </m:groupChrPr>, producing XML that will not
# even parse. Found by running real lesson frames through the pipeline: the
# statement "Vectors $\vec{u}=(1,2,1)$..." silently dropped both vectors from
# the worksheet.
_GROUPCHR_BUG = re.compile(r"(<m:groupChrPr>(?:(?!</m:groupChr).)*?)</m:groupChr>(?!Pr)")

# $$...$$ first, then $...$, then \(...\) and \[...\]
_MATH_SPLIT = re.compile(r"(\$\$.+?\$\$|\$[^$]+?\$|\\\(.+?\\\)|\\\[.+?\\\])", re.S)


class MathConversionError(RuntimeError):
    pass


# XML 1.0 permits only tab, newline and carriage return below 0x20, plus a few
# other exclusions. Model output occasionally contains a vertical tab or form
# feed, and lxml then refuses the whole run -- which took down an entire build
# of hundreds of questions over one stray byte.
_ILLEGAL_XML = re.compile(
    "[^\u0009\u000A\u000D\u0020-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]")


def xml_safe(text: str) -> str:
    """Drop characters Word cannot store, leaving everything else untouched."""
    return _ILLEGAL_XML.sub("", str(text or ""))


def _binomial_indices(mathml: str) -> list[int]:
    """Positions of the bar-less fractions among ALL fractions, in document order.

    Counting them is not enough. In \\frac{\\binom{n}{k}}{2} the ordinary
    fraction is emitted first and the binomial second, so converting "the first
    N fractions" bracketed the wrong one. mathml2omml preserves element order,
    so the Nth <mfrac> becomes the Nth <m:f> and the index transfers directly.
    """
    out = []
    for i, m in enumerate(_ANY_MFRAC.finditer(mathml)):
        if _NOLINE_MFRAC.fullmatch(m.group(0)):
            out.append(i)
    return out


def _fix_binomials(omml: str, indices: list[int]) -> str:
    """Mark the fractions at `indices` as bar-less. Brackets are already there."""
    if not indices:
        return omml

    wanted = set(indices)
    out, pos, seen = [], 0, 0
    while True:
        i = omml.find("<m:f>", pos)
        if i == -1:
            break
        this = seen
        seen += 1
        if this not in wanted:
            out.append(omml[pos:i + 5])
            pos = i + 5
            continue
        # this fraction's matching close, allowing for nesting
        depth, j = 0, i
        while j < len(omml):
            if omml.startswith("<m:f>", j):
                depth += 1
                j += 5
            elif omml.startswith("</m:f>", j):
                depth -= 1
                j += 6
                if depth == 0:
                    break
            else:
                j += 1
        body = omml[i + 5:j - 6]
        # nested fractions inside this one were already counted by the scan
        seen += body.count("<m:f>")
        out.append(omml[pos:i])
        out.append(
            '<m:f><m:fPr><m:type m:val="noBar"/></m:fPr>'
            + _fix_binomials(body, [k - this - 1 for k in indices if k > this])
            + "</m:f>")
        pos = j
    out.append(omml[pos:])
    return "".join(out)


def _patch_library_bugs(omml: str) -> str:
    omml = _BARE_RAD.sub(_RAD_FIX, omml)
    omml = _GROUPCHR_BUG.sub(r"\1</m:groupChrPr>", omml)
    return omml


def latex_to_omml(latex: str) -> str:
    """Return an `<m:oMath>` XML string with namespaces declared on the root."""
    import latex2mathml.converter as l2m
    from mathml2omml import convert as mml2omml

    latex = xml_safe(latex).strip()
    if not latex:
        raise MathConversionError("empty LaTeX")

    try:
        mathml = l2m.convert(latex)
        binomials = _binomial_indices(mathml)
        omml = mml2omml(mathml)
    except Exception as exc:  # noqa: BLE001 - surface the offending source
        raise MathConversionError(f"{latex!r}: {exc}") from exc

    omml = _patch_library_bugs(omml)
    omml = _fix_binomials(omml, binomials)
    if "<m:oMath>" not in omml:
        raise MathConversionError(f"{latex!r}: no oMath root produced")
    return omml.replace("<m:oMath>", f"<m:oMath {_NS_DECL}>", 1)


def append_math(paragraph, latex: str) -> bool:
    """Append an equation to a python-docx paragraph.

    Returns True on success. On failure the LaTeX is written as literal text so
    the item is still readable and obviously in need of a fix, rather than
    silently missing from the worksheet.
    """
    from docx.oxml import parse_xml

    try:
        paragraph._p.append(parse_xml(latex_to_omml(latex)))
        return True
    except Exception:
        run = paragraph.add_run(f" [{xml_safe(latex)}] ")
        run.font.name = "Consolas"
        return False


def append_rich_text(paragraph, text: str) -> int:
    """Append mixed prose and math, splitting on $...$, $$...$$, \\(...\\), \\[...\\].

    Returns the number of equations that failed to convert.
    """
    failures = 0
    for chunk in _MATH_SPLIT.split(xml_safe(text)):
        if not chunk:
            continue
        if chunk.startswith("$$") and chunk.endswith("$$"):
            body = chunk[2:-2]
        elif chunk.startswith("$") and chunk.endswith("$"):
            body = chunk[1:-1]
        elif chunk.startswith("\\(") and chunk.endswith("\\)"):
            body = chunk[2:-2]
        elif chunk.startswith("\\[") and chunk.endswith("\\]"):
            body = chunk[2:-2]
        else:
            paragraph.add_run(xml_safe(chunk))
            continue

        if not append_math(paragraph, body.strip()):
            failures += 1
    return failures


def selftest(samples: list[str] | None = None) -> list[tuple[str, str]]:
    """Convert a list of LaTeX strings and return (latex, error) for failures."""
    samples = samples or [
        r"\int_0^\pi x\sin(x)\,dx",
        r"\frac{dy}{dx} = -\frac{x}{y}",
        r"\sqrt{\frac{3}{4}}",
        r"\sqrt[3]{x}",
        r"x = \frac{-b \pm \sqrt{b^2-4ac}}{2a}",
        r"\lim_{h \to 0}\frac{f(x+h)-f(x)}{h}",
        r"\begin{pmatrix}1 & 2\\3 & 4\end{pmatrix}",
        r"\sum_{i=1}^{n} i^2",
        r"\vec{u}=(1,2,1)",
        r"\vec{u}\times\vec{v}",
        r"\overline{AB}",
        r"\hat{n}",
        r"S_{\triangle ADE}=2",
        r"100\tfrac{3}{4}+200\tfrac{1}{2}",
        r"\log_{1/2} x",
        r"45^\circ",
        r"\binom{n}{k}",
        r"{n \choose k}",
        r"\binom{10}{3} = 120",
        r"\frac{\binom{n}{k}}{2}",
        r"\binom{n}{k}\binom{m}{j}",
    ]
    from docx.oxml import parse_xml

    failures = []
    for s in samples:
        try:
            # Converting is not enough: mathml2omml can emit well-formed-looking
            # text that lxml refuses. Parse it, exactly as append_math will.
            parse_xml(latex_to_omml(s))
        except Exception as exc:
            failures.append((s, f"{type(exc).__name__}: {exc}"))
    return failures


if __name__ == "__main__":
    bad = selftest()
    print("all samples converted" if not bad else f"{len(bad)} failed: {bad}")
