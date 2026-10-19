"""Offline tests for resolve_entity's matching logic.

These run against an in-memory fixture, with no data files. Names are real public companies
used only to exercise the matching; IDs are illustrative placeholders.

Run: pytest tests/test_resolve.py   (or: python -m tests.test_resolve)
"""

from __future__ import annotations

from creditmcp.names import normalize_name
from creditmcp.resolve import EntityResolver
from creditmcp.schema import AffiliateGroup, Confidence


def _row(eid, name, isos, source, alias, **kw):
    return {"entity_id": eid, "entity_name": name, "isos": isos, "source": source,
            "alias_norm": alias if source != "roster" else normalize_name(name), **kw}


FIXTURE = [
    _row("R1", "Tenaska Power Services Co.", "PJM", "roster", None),
    _row("R1", "Tenaska Power Services Co.", "PJM", "eqr_customer", "TENASKA POWER SERVICE",
         eqr_power_contracts=9, eqr_last_quarter="2026q2", raw_examples="Tenaska Power Service Company ; Tenaska Power Service Co."),
    _row("R1", "Tenaska Power Services Co.", "PJM", "eqr_seller", "TENASKA POWER SERVICES", seller_cid="C000238"),
    _row("R2", "Hoosier Energy REC, Inc.", "MISO+PJM", "roster", None),
    _row("R2", "Hoosier Energy REC, Inc.", "MISO+PJM", "eia_utility", "HOOSIER ENERGY RURAL ELECTRIC COOPERATIVE",
         utility_id_eia=9267, utility_id_pudl=4321),
    _row("R3", "Harvey Solar I, LLC", "PJM", "roster", None),
    _row("R4", "Harvey Solar II, LLC", "PJM", "roster", None),
    _row("R5", "American Municipal Power, Inc.", "PJM", "roster", None),
    _row("R5", "American Municipal Power, Inc.", "PJM", "eqr_customer", "AMERICAN MUNICIPAL POWER OHIO",
         eqr_power_contracts=8),
    _row("R6", "American Mun Power-Ohio, Inc", "MISO", "roster", None),
    _row("R6", "American Mun Power-Ohio, Inc", "MISO", "eqr_customer", "AMERICAN MUNICIPAL POWER OHIO",
         eqr_power_contracts=8),
    _row("R7", "DTE Energy Trading, Inc.", "MISO+PJM", "roster", None),
    _row("R8", "Calpine Energy Services, L.P.", "PJM", "roster", None),
    _row("R10", "PALMco Power PA, LLC dba Indra Energy", "PJM", "roster", None),
    _row("R11", "PALMco Power VA, LLC", "PJM", "roster", None),
    # The reality check's own fuzzy links, as they appear in alias_seed.csv:
    _row("R10", "PALMco Power PA, LLC dba Indra Energy", "PJM", "eqr_seller", "PALMCO POWER MA", method="fuzzy",
         seller_cid="C002879"),
    _row("R12", "AC Energy, LLC", "PJM", "roster", None),
    _row("R12", "AC Energy, LLC", "PJM", "eia_utility", "ACM ENERGY", method="fuzzy", utility_id_eia=63222),
    _row("R9", "Constellation Energy Generation, LLC", "PJM", "roster", None),
]
GROUPS = {"R8": AffiliateGroup(name="Constellation Energy Generation, LLC", entity_id="R9", is_voting_member=False,
                               source="fixture")}


def _r() -> EntityResolver:
    return EntityResolver(FIXTURE, groups=GROUPS)


def test_roster_name_is_an_exact_match():
    res = _r().resolve("Tenaska Power Services Co.")
    top = res.candidates[0]
    assert top.entity_id == "R1" and top.confidence == Confidence.MATCH and top.method.value == "exact"
    assert top.identifiers.ferc_cids == ["C000238"]
    assert top.evidence.eqr_power_contracts == 9


def test_eqr_spelling_resolves_through_the_alias():
    top = _r().resolve("Tenaska Power Service Company").candidates[0]
    assert top.entity_id == "R1" and top.method.value == "exact" and top.alias_source.value == "eqr_customer"


def test_spacing_differences_are_exact_nospace():
    top = _r().resolve("TenaskaPower Services").candidates[0]
    assert top.entity_id == "R1" and top.method.value == "exact_nospace" and top.confidence == Confidence.MATCH


def test_typo_is_a_fuzzy_match():
    top = _r().resolve("Tenaska Power Servces Co").candidates[0]
    assert top.entity_id == "R1" and top.method.value == "fuzzy" and top.confidence == Confidence.MATCH


def test_coop_shorthand_normalizes():
    res = _r().resolve("Hoosier Energy Rural Electric Cooperative, Inc.")
    assert res.candidates[0].entity_id == "R2" and res.candidates[0].identifiers.eia_utility_ids == [9267]


def test_numbered_projects_never_cross_match():
    ids = [c.entity_id for c in _r().resolve("Harvey Solar III LLC").candidates]
    assert "R3" not in ids and "R4" not in ids
    assert _r().resolve("Harvey Solar II").candidates[0].entity_id == "R4"


def test_shared_alias_is_flagged_ambiguous():
    res = _r().resolve("American Municipal Power-Ohio, Inc.")
    assert res.ambiguous and {c.entity_id for c in res.candidates[:2]} == {"R5", "R6"}


def test_lookalike_is_never_a_match():
    res = _r().resolve("DB Energy Trading LLC")
    assert all(c.confidence != Confidence.MATCH for c in res.candidates)


def test_state_codes_keep_sister_companies_apart():
    res = _r().resolve("PALMco Power PA LLC")
    assert res.candidates[0].entity_id == "R10" and not res.ambiguous
    assert all(c.confidence != Confidence.MATCH for c in res.candidates if c.entity_id == "R11")


def test_wrong_seed_links_are_dropped_or_capped():
    r = _r()
    pa = r.resolve("PALMco Power PA").candidates[0]
    assert pa.entity_id == "R10" and "C002879" not in pa.identifiers.ferc_cids      # MA's CID is not PA's
    assert all(c.entity_id != "R10" or c.confidence != Confidence.MATCH for c in r.resolve("PALMco Power MA").candidates)
    acm = r.resolve("ACM Energy").candidates
    assert acm and acm[0].entity_id == "R12" and acm[0].confidence == Confidence.POSSIBLE
    assert 63222 not in acm[0].identifiers.eia_utility_ids


def test_rejected_review_pairs_stay_rejected():
    r = EntityResolver(FIXTURE, rejected={("R7", normalize_name("DB Energy Trading LLC"))})
    assert all(c.entity_id != "R7" for c in r.resolve("DB Energy Trading LLC").candidates)


def test_affiliate_group_is_attached():
    top = _r().resolve("Calpine Energy Services LP").candidates[0]
    assert top.affiliate_group is not None and top.affiliate_group.entity_id == "R9"
    assert "affiliate group" in _r().resolve("Calpine Energy Services LP").summary


def test_unknown_and_empty_names_return_no_candidates():
    for q in ("Zzyzx Power Holdings", "", "   "):
        res = _r().resolve(q)
        assert res.candidates == [] and not res.ambiguous and "No PJM or MISO roster company" in res.summary


def test_every_result_carries_provenance_and_caveats():
    res = _r().resolve("Tenaska Power Services Co.")
    assert res.methodology_version.startswith("resolve-") and len(res.caveats) == 2
    assert res.provenance is not None and all(c.source for c in res.candidates)


def test_limit_is_respected():
    assert len(_r().resolve("American Municipal Power", limit=1).candidates) <= 1


def _main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} resolver tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


def test_a_former_eia_name_resolves_to_the_renamed_company():
    rows = FIXTURE + [
        _row("R13", "Northwind Energy Cooperative", "MISO", "roster", None),
        _row("R13", "Northwind Energy Cooperative", "MISO", "eia_name_history", "NORTHWIND ELECTRIC POWER ASSOCIATION",
             method="eia_spelled_out", utility_id_eia=90001, raw_examples="Northwind El Pwr Assn (2001-2016)"),
    ]
    res = EntityResolver(rows, groups=GROUPS).resolve("Northwind Electric Power Association")
    top = res.candidates[0]
    assert top.entity_id == "R13" and top.confidence == Confidence.MATCH and top.method.value == "exact"
    assert top.alias_source.value == "eia_name_history" and top.identifiers.eia_utility_ids == [90001]
    assert res.summary == ("Matches Northwind Energy Cooperative (MISO): exact match on a name in its EIA filing "
                           "history.")
    assert "matched on EIA filing history name 'NORTHWIND ELECTRIC POWER ASSOCIATION'" in top.source
