"""Name normalization, lifted unchanged from reality_check.py.

The resolver and the reality check must agree on every name, so this is a verbatim copy of
section 3 of reality_check.py plus the matching thresholds it uses. tests/test_names_parity.py
fails if the two ever drift. Change both together, deliberately, or neither.
"""

from __future__ import annotations

import re

FUZZY_HIGH = 0.97        # Jaro-Winkler at/above this counts as a high-confidence match
FUZZY_REVIEW = 0.90      # between REVIEW and HIGH lands in review_queue.csv for hand checks
FUZZY_MIN_LEN = 6        # never fuzzy-match very short names
NUM_TOKEN_RE = r"\b([0-9]+|[IVX]+)\b"  # digits or roman numerals: "Solar 1" vs "Solar 2", "Ridge II" vs "IV"

_PARENS = re.compile(r"\([^)]*\)")
_DROP = re.compile(r"[.'`\u2019\"]")
_COOP = re.compile(r"\bCO\s*-?\s*OP\b")
_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
_ALIAS_TAIL = re.compile(r"\b(DBA|D B A|AKA|A K A|FKA|F K A)\b.*$")
_TOKEN_CANON = {
    "ASSN": "ASSOCIATION", "ASSOC": "ASSOCIATION", "SVCS": "SERVICES", "SVC": "SERVICE",
    "MKTG": "MARKETING", "ELEC": "ELECTRIC", "PWR": "POWER", "ENGY": "ENERGY",
    "INTL": "INTERNATIONAL", "NATL": "NATIONAL", "MGMT": "MANAGEMENT", "UTIL": "UTILITY",
    "UTILS": "UTILITIES", "AUTH": "AUTHORITY", "DEPT": "DEPARTMENT", "MUNI": "MUNICIPAL",
    "COOP": "COOPERATIVE",
}
# Co-op shorthand is everywhere in MISO/PJM rosters ("Hoosier Energy R.E.C.", "Jackson EMC", "G&T").
_PHRASES = (
    (("R", "E", "C"), ("RURAL", "ELECTRIC", "COOPERATIVE")), (("REC",), ("RURAL", "ELECTRIC", "COOPERATIVE")),
    (("E", "M", "C"), ("ELECTRIC", "MEMBERSHIP", "CORPORATION")), (("EMC",), ("ELECTRIC", "MEMBERSHIP", "CORPORATION")),
    (("G", "AND", "T"), ("GENERATION", "AND", "TRANSMISSION")),
)
_LEGAL_TAIL = {"INC", "INCORPORATED", "LLC", "LP", "LLP", "LLLP", "CORP", "CORPORATION", "CO",
               "COMPANY", "LTD", "LIMITED", "PLC", "PLLC", "PC", "NA"}
_LEGAL_TAIL_SEQ = (("L", "L", "C"), ("L", "L", "P"), ("L", "P"), ("N", "A"))


def normalize_name(raw) -> str:
    """Upper-case, strip punctuation, alias tails and legal-form suffixes, canonicalize abbreviations."""
    if raw is None:
        return ""
    s = str(raw).upper()
    s = _PARENS.sub(" ", s).replace("&", " AND ")
    s = _DROP.sub("", s)
    s = _COOP.sub(" COOPERATIVE ", s)
    s = _NON_ALNUM.sub(" ", s)
    s = _ALIAS_TAIL.sub(" ", s)
    toks = s.split()
    if len(toks) > 1 and toks[0] == "THE":
        toks = toks[1:]
    toks = [_TOKEN_CANON.get(t, t) for t in toks]
    expanded, i = [], 0
    while i < len(toks):
        for pat, rep in _PHRASES:
            if tuple(toks[i:i + len(pat)]) == pat:
                expanded.extend(rep)
                i += len(pat)
                break
        else:
            expanded.append(toks[i])
            i += 1
    toks = expanded
    stripped = True
    while stripped and len(toks) > 1:
        stripped = False
        for seq in _LEGAL_TAIL_SEQ:
            if len(toks) > len(seq) and tuple(toks[-len(seq):]) == seq:
                toks, stripped = toks[: -len(seq)], True
                break
        if not stripped and toks[-1] in _LEGAL_TAIL:
            toks, stripped = toks[:-1], True
    return " ".join(toks)
