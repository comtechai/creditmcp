"""Drift guard: the resolver must normalize names exactly as reality_check.py does.

If this fails, either names.py or reality_check.py changed. Reconcile them deliberately (the
alias seed was built with the script's version), never by editing this test.
"""

from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

import pytest

from creditmcp import names

ROOT = Path(__file__).resolve().parents[1]
TRICKY = [
    "Hoosier Energy R.E.C., Inc.", "Jackson EMC", "The Energy Authority, Inc.", "J. Aron & Company LLC",
    "Dayton Power & Light, The d/b/a AES Ohio", "TransAlta Energy Marketing (U.S.) Inc.", "Basin Electric Power Co-op",
    "Washington - St. Tammany Electric Corp. Inc.", "Harvey Solar II, L.L.C.", "N.A. Power Co", "", None,
    "RWE Clean Energy Wholesale Services, Inc. f/k/a Con Edison Energy", "Assoc. Elec. Coop. Svcs. Mktg Pwr",
]


def _reality_check():
    path = ROOT / "reality_check.py"
    if not path.exists():
        pytest.skip("reality_check.py is not in the repo root")
    pytest.importorskip("pandas")
    spec = importlib.util.spec_from_file_location("reality_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _samples() -> list:
    out = list(TRICKY)
    seed = ROOT / "rc" / "alias_seed.csv"
    if seed.exists():
        with open(seed, newline="", encoding="utf-8") as f:
            for r in csv.DictReader(f):
                out.append(r["entity_name"])
                out.extend(s.strip() for s in (r.get("raw_examples") or "").split(" ; ") if s.strip())
    golden = Path(__file__).with_name("golden.csv")
    if golden.exists():
        with open(golden, newline="", encoding="utf-8") as f:
            out.extend(r["query"] for r in csv.DictReader(f))
    return out


def test_normalize_name_matches_the_reality_check():
    rc = _reality_check()
    diffs = [s for s in _samples() if names.normalize_name(s) != rc.normalize_name(s)]
    assert not diffs, f"{len(diffs)} names normalize differently, e.g. {diffs[:5]}"


def test_thresholds_match_the_reality_check():
    rc = _reality_check()
    assert (names.FUZZY_HIGH, names.FUZZY_REVIEW, names.FUZZY_MIN_LEN, names.NUM_TOKEN_RE) == \
        (rc.FUZZY_HIGH, rc.FUZZY_REVIEW, rc.FUZZY_MIN_LEN, rc.NUM_TOKEN_RE)
