"""
test_coverage_checklist.py — unit tests for the Coverage Checklist box math.

Pure-function tests (no DB). Run: python test_coverage_checklist.py
Asserts the locked box rule against the user's worked examples + edge cases.
"""

from coverage_checklist import coverage_boxes


def _render(b):
    # ASCII render (v = green/answered, x = red/no-answer, . = empty) so the test
    # output is console-encoding-safe on Windows.
    return "v" * b["green"] + "x" * b["red"] + "." * b["empty"]


# (target, answered, no_answer, exp_green, exp_red, exp_empty, exp_covered, note)
CASES = [
    # ── the user's worked examples ──
    (1, 0, 1, 0, 1, 0, False, "needs 1, one no-answer -> [X]"),
    (1, 0, 2, 0, 1, 0, False, "needs 1, two no-answers -> still one [X]"),
    (1, 1, 2, 1, 0, 0, True,  "needs 1, then answered -> [check]"),
    (2, 0, 2, 0, 2, 0, False, "needs 2, two no-answers -> [X][X]"),
    (2, 1, 2, 1, 1, 0, False, "needs 2, one answer -> [check][X] (green replaced a red)"),
    (3, 1, 1, 1, 1, 1, False, "needs 3, 1 answer 1 no-answer -> [check][X][blank]"),
    (2, 2, 3, 2, 0, 0, True,  "needs 2, fully covered -> reds gone"),
    # ── edges ──
    (1, 0, 0, 0, 0, 1, False, "needs 1, untouched -> [blank]"),
    (3, 5, 0, 3, 0, 0, True,  "over-answered caps at target, all green"),
    (2, 0, 5, 0, 2, 0, False, "many no-answers cap at target"),
    (0, 5, 5, 0, 0, 0, False, "target 0 = excluded, no boxes"),
    (2, 1, 0, 1, 0, 1, False, "1 of 2 answered -> [check][blank]"),
]


def main():
    failures = 0
    for (target, ans, no_ans, eg, er, ee, ecov, note) in CASES:
        b = coverage_boxes(target, ans, no_ans)
        ok = (b["green"] == eg and b["red"] == er and b["empty"] == ee
              and b["covered"] == ecov
              and b["green"] + b["red"] + b["empty"] == max(target, 0))
        flag = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{flag}] target={target} ans={ans} no_ans={no_ans} "
              f"-> {_render(b):<8} green/red/empty={b['green']}/{b['red']}/{b['empty']} "
              f"covered={b['covered']}  ({note})")
        if not ok:
            print(f"        EXPECTED green/red/empty={eg}/{er}/{ee} covered={ecov}")

    print()
    if failures:
        print(f"{failures} FAILURE(S)")
        raise SystemExit(1)
    print(f"All {len(CASES)} cases passed.")


if __name__ == "__main__":
    main()
