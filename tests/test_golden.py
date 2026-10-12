"""Golden-set check for resolve_entity against your real reality-check run (rc/).

Forty names checked by hand, in tests/golden.csv:
  plain (15)               ordinary names and EQR spellings
  variant (10)             abbreviations, co-op shorthand, typos
  affiliate (5)            PJM affiliates; the affiliate group is checked too
  renamed (5)              old names and renames from the hand-reviewed match decisions
  should_not_resolve (5)   look-alikes and non-members that must not come back as a match

Pass bar, set before building: the right company first for at least 32 of
the 35 positives, and no match-level answer for any of the 5 negatives.

Run: pytest tests/test_golden.py      (or: python -m tests.test_golden for the full table)
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from creditmcp.names import normalize_name
from creditmcp.resolve import EntityResolver
from creditmcp.schema import Confidence

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).with_name("golden.csv")
KIND_COUNTS = {"plain": 15, "variant": 10, "affiliate": 5, "renamed": 5, "should_not_resolve": 5}
PASS_POSITIVES = 32


def load_golden() -> list[dict]:
    with open(GOLDEN, newline="", encoding="utf-8") as f:
        return [r for r in csv.DictReader(f) if (r.get("query") or "").strip()]


def evaluate(resolver: EntityResolver) -> list[dict]:
    out = []
    for row in load_golden():
        res = resolver.resolve(row["query"])
        top = res.candidates[0] if res.candidates else None
        if row["kind"] == "should_not_resolve":
            ok = not any(c.confidence == Confidence.MATCH for c in res.candidates)
        else:
            expected = {normalize_name(x) for x in row["expected"].split("|") if x.strip()}
            ok = top is not None and normalize_name(top.name) in expected
            if ok and (row.get("expected_group") or "").strip():
                ok = (top.affiliate_group is not None
                      and normalize_name(top.affiliate_group.name) == normalize_name(row["expected_group"]))
        out.append({"kind": row["kind"], "query": row["query"], "expected": row["expected"], "ok": ok,
                    "got": top.name if top else "", "confidence": top.confidence.value if top else "",
                    "score": top.score if top else None, "ambiguous": res.ambiguous,
                    "group": (top.affiliate_group.name if top and top.affiliate_group else "")})
    return out


@pytest.fixture(scope="module")
def resolver() -> EntityResolver:
    if not (ROOT / "rc" / "alias_seed.csv").exists():
        pytest.skip("No reality-check run in rc/ (run reality_check.py --out rc first).")
    return EntityResolver.from_dir(ROOT)


def test_golden_set_has_the_planned_shape():
    rows = load_golden()
    counts = {k: sum(r["kind"] == k for r in rows) for k in KIND_COUNTS}
    assert counts == KIND_COUNTS, counts
    assert all(r["expected"].strip() for r in rows if r["kind"] != "should_not_resolve")
    assert all(r["expected_group"].strip() for r in rows if r["kind"] == "affiliate")


def test_golden_pass_bar(resolver):
    results = evaluate(resolver)
    pos = [r for r in results if r["kind"] != "should_not_resolve"]
    neg = [r for r in results if r["kind"] == "should_not_resolve"]
    misses = [f"{r['kind']}: {r['query']!r} -> {r['got']!r}" for r in results if not r["ok"]]
    assert sum(r["ok"] for r in pos) >= PASS_POSITIVES and all(r["ok"] for r in neg), "\n".join(misses)


def _main() -> int:
    results = evaluate(EntityResolver.from_dir(ROOT))
    for r in results:
        mark = "ok  " if r["ok"] else "MISS"
        extra = f" [group: {r['group']}]" if r["kind"] == "affiliate" else ""
        amb = " (ambiguous)" if r["ambiguous"] else ""
        print(f"{mark} {r['kind']:<19} {r['query'][:44]:<44} -> {r['got'][:44]:<44} "
              f"{r['confidence']:<8}{amb}{extra}")
    pos = [r for r in results if r["kind"] != "should_not_resolve"]
    neg = [r for r in results if r["kind"] == "should_not_resolve"]
    p, n = sum(r["ok"] for r in pos), sum(r["ok"] for r in neg)
    verdict = "PASS" if p >= PASS_POSITIVES and n == len(neg) else "FAIL"
    print(f"\nPositives right first: {p} of {len(pos)} (bar {PASS_POSITIVES}). "
          f"Negatives kept below match: {n} of {len(neg)} (bar all). {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(_main())
