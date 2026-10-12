"""Offline tests for the dormancy signal: a tiny reality-check database, built in a temp folder.

Run: pytest tests/test_dormancy.py
"""

from __future__ import annotations

import csv
import datetime as dt
import json

import duckdb
import pytest

from creditmcp.dormancy import EXPLAINED_AFFILIATE, EXPLAINED_AGREEMENT, DormancyIndex, build
from creditmcp.names import normalize_name
from creditmcp.resolve import DataError, EntityResolver
from creditmcp.schema import AffiliateGroup, DormancyFlag

Q = ["2025q2", "2025q3", "2025q4", "2026q1", "2026q2"]
QK = {q: int(q[:4]) * 4 + int(q[5]) - 1 for q in Q}
QSTART = {q: dt.date(int(q[:4]), (int(q[5]) - 1) * 3 + 1, 1) for q in Q}

COMPANIES = {"E1": "Alpha Municipal Power Agency", "E2": "Bravo Electric Cooperative", "E3": "Charlie Energy LLC",
             "E4": "Delta Power Authority", "E5": "Echo Retail Power LLC", "E6": "Foxtrot Power Marketing",
             "E7": "Golf Energy Trading", "E8": "Hotel Energy Services", "E9": "India Power Coop",
             "E10": "Juliet Utility", "E11": "Kilo Retail LLC", "E12": "Kilo Generation LLC",
             "E13": "Lima Power LLC", "E14": "Mike Energy Co"}
ROWS = [{"entity_id": e, "entity_name": n, "isos": "PJM", "source": "roster", "alias_norm": normalize_name(n)}
        for e, n in COMPANIES.items()]
ROWS.append({"entity_id": "E2", "entity_name": COMPANIES["E2"], "isos": "PJM", "source": "eqr_customer",
             "alias_norm": normalize_name("Bravo Electric Coop Assn")})  # a second spelling sellers use
GROUPS = {e: AffiliateGroup(name="Kilo Generation LLC", entity_id="E12", is_voting_member=e == "E12", source="fixture")
          for e in ("E11", "E12")}


def case(seller, cust, traded, *, under=Q, actual=None, energy=True, mwh_latest=100.0, csa=None, latest_as=None):
    return dict(seller=seller, cust=cust, traded=traded, under=under, actual=actual, energy=energy,
                mwh_latest=mwh_latest, csa=csa, latest_as=latest_as)


CASES = [
    case("S1", "Alpha Municipal Power Agency", Q[:4]),           # E1: 2 of 3 quiet, volume down -> watch
    case("S2", "Alpha Municipal Power Agency", Q[:4]),
    case("S3", "Alpha Municipal Power Agency", Q),
    case("S1", "Bravo Electric Cooperative", Q[:4]),             # E2: 1 of 3 quiet -> some
    case("S2", "Bravo Electric Coop Assn", Q),                   # E2: trading under the second spelling
    case("S3", "Bravo Electric Cooperative", Q),
    case("S1", "Charlie Energy LLC", Q),                         # E3: none
    case("S1", "Delta Power Authority", ["2025q3"]),             # E4: irregular -> no baseline
    case("S2", "Echo Retail Power LLC", Q[:4], under=Q[:4], actual="2026-02-15"),  # E5: contract ended
    case("S1", "Foxtrot Power Marketing", Q[:4], energy=False),  # E6: capacity only -> no baseline
    case("S2", "Foxtrot Power Marketing", Q[:4], energy=False),
    case("S1", "Golf Energy Trading", Q[:4]),                    # E7: 2 of 3 quiet, but volume rose -> some
    case("S2", "Golf Energy Trading", Q[:4]),
    case("S3", "Golf Energy Trading", Q, mwh_latest=400.0),
    case("S1", "Hotel Energy Services", Q[:4], csa="H-100", latest_as="Hotel Energy Svcs New Name"),  # E8: renamed
    case("S2", "Hotel Energy Services", Q),
    case("S2", "India Power Coop", Q[:4], csa="WSPP"),           # E9: quiet under a shared master agreement
    case("S2", "Juliet Utility", Q, csa="WSPP"),                 # E10: shares the master agreement
    case("S3", "India Power Coop", Q),
    case("S3", "Kilo Retail LLC", Q[:4]),                        # E11: S3's book moved to its affiliate E12
    case("S3", "Kilo Generation LLC", Q[4:]),
    case("S1", "Kilo Retail LLC", Q),
    case("S1", "Lima Power LLC", Q[:4], csa="L-7", latest_as="Charlie Energy LLC"),     # E13: contract handed to
                                                                                        # an unrelated company
    case("S3", "Mike Energy Co", Q[:4], csa="N/A", latest_as="Mike Energy Co New Name"),  # E14: placeholder ID
]


def _db(tmp_path, quarters=Q):
    path = tmp_path / "rc.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE quarters (qk INTEGER, q VARCHAR, q_start DATE, q_end DATE)")
    con.execute("CREATE TABLE contracts (qk INTEGER, seller_company_id_ferc VARCHAR, seller_company_name VARCHAR, "
                "customer_norm VARCHAR, contract_key VARCHAR, actual_termination_date DATE)")
    con.execute("CREATE TABLE key_flags (contract_key VARCHAR, eligible BOOLEAN)")
    con.execute("CREATE TABLE tx_base (qk INTEGER, year_quarter VARCHAR, seller_company_id_ferc VARCHAR, "
                "customer_norm VARCHAR, contract_service_agreement_id VARCHAR, is_energy BOOLEAN, "
                "standardized_quantity DOUBLE)")
    for q in Q:
        con.execute("INSERT INTO quarters VALUES (?, ?, ?, ?)", [QK[q], q, QSTART[q], QSTART[q]])
    tx = "INSERT INTO tx_base VALUES (?, ?, ?, ?, ?, ?, ?)"
    for i, c in enumerate(CASES):
        key, csa = f"K{i}", c["csa"] or f"K{i}"
        con.execute("INSERT INTO key_flags VALUES (?, true)", [key])
        for q in c["under"]:
            con.execute("INSERT INTO contracts VALUES (?, ?, ?, ?, ?, ?)",
                        [QK[q], c["seller"], f"Seller {c['seller']} LLC", normalize_name(c["cust"]), key, c["actual"]])
        for q in c["traded"]:
            if q in quarters:
                mwh = c["mwh_latest"] if q == Q[-1] else 100.0
                con.execute(tx, [QK[q], q, c["seller"], normalize_name(c["cust"]), csa, c["energy"], mwh])
        if c["latest_as"] and Q[-1] in quarters:
            con.execute(tx, [QK[Q[-1]], Q[-1], c["seller"], normalize_name(c["latest_as"]), csa, True, 100.0])
    con.close()
    return path


@pytest.fixture
def built(tmp_path):
    resolver = EntityResolver(ROWS, groups=GROUPS)
    m = build(_db(tmp_path), tmp_path / "out", resolver, {"contract_window": ["2023q3", "2026q2"]})
    return m, resolver, tmp_path / "out"


def _read(out):
    read = lambda p: list(csv.DictReader(open(p, newline="", encoding="utf-8")))  # noqa: E731
    return (read(out / "dormancy_companies.csv"), read(out / "dormancy_relationships.csv"),
            json.loads((out / "dormancy_manifest.json").read_text()))


def _index(out, resolver):
    return DormancyIndex(resolver, *_read(out))


def _rel(result, seller):
    return next(x for x in result.relationships if x.seller_cid == seller)


def test_flags_follow_the_rules(built):
    m, resolver, out = built
    idx = _index(out, resolver)
    assert idx.lookup("Alpha Municipal Power Agency").flag == DormancyFlag.WATCH   # 2 of 3 quiet, volume -67%
    assert idx.lookup("Bravo Electric Cooperative").flag == DormancyFlag.SOME     # 1 of 3 quiet
    assert idx.lookup("Charlie Energy").flag == DormancyFlag.NONE
    assert idx.lookup("Delta Power Authority").flag == DormancyFlag.NO_BASELINE   # never regular
    assert idx.lookup("Echo Retail Power").flag == DormancyFlag.NO_BASELINE       # its contract ended
    assert m["latest_quarter"] == "2026q2" and m["prior_quarters"] == Q[:4]
    assert m["methodology_version"] == "dormancy-0.2.0" and m["rules"]["agreement_check"]


def test_only_energy_trades_count(built):
    _, resolver, out = built
    r = _index(out, resolver).lookup("Foxtrot Power Marketing")
    assert r.flag == DormancyFlag.NO_BASELINE and r.regular_sellers == 0
    s1 = _rel(r, "S1")
    assert s1.quarters_traded == [] and s1.quarters_other_products == Q[:4] and not s1.went_quiet


def test_watch_needs_the_volume_to_fall(built):
    _, resolver, out = built
    idx = _index(out, resolver)
    golf = idx.lookup("Golf Energy Trading")
    assert golf.flag == DormancyFlag.SOME and golf.went_quiet == 2 and golf.volume_change == pytest.approx(0.333)
    assert "rose 33%" in golf.summary and "isn't flagged watch" in golf.summary
    alpha = idx.lookup("Alpha Municipal Power Agency")
    assert alpha.volume_change == pytest.approx(-0.667) and "fell 67%" in alpha.summary


def test_same_agreement_under_a_new_name_is_not_quiet(built):
    _, resolver, out = built
    r = _index(out, resolver).lookup("Hotel Energy Services")
    s1 = _rel(r, "S1")
    assert not s1.went_quiet and s1.quiet_explained == EXPLAINED_AGREEMENT
    assert r.flag == DormancyFlag.NONE and r.explained == 1 and "same agreement" in r.summary


def test_a_shared_master_agreement_explains_nothing(built):
    _, resolver, out = built
    r = _index(out, resolver).lookup("India Power Coop")
    s2 = _rel(r, "S2")
    assert s2.went_quiet and s2.quiet_explained is None and r.flag == DormancyFlag.SOME


def test_an_agreement_handed_to_an_unrelated_company_still_counts_as_quiet(built):
    _, resolver, out = built
    r = _index(out, resolver).lookup("Lima Power LLC")
    assert _rel(r, "S1").went_quiet and r.flag == DormancyFlag.SOME


def test_a_placeholder_agreement_id_explains_nothing(built):
    _, resolver, out = built
    r = _index(out, resolver).lookup("Mike Energy Co")
    assert _rel(r, "S3").went_quiet and r.flag == DormancyFlag.SOME


def test_a_book_moved_to_an_affiliate_is_not_quiet(built):
    _, resolver, out = built
    idx = _index(out, resolver)
    r = idx.lookup("Kilo Retail LLC")
    s3 = _rel(r, "S3")
    assert not s3.went_quiet and s3.quiet_explained == EXPLAINED_AFFILIATE
    assert r.flag == DormancyFlag.NONE and r.explained == 1
    assert idx.lookup("Kilo Generation LLC").flag == DormancyFlag.NO_BASELINE      # new relationship, no history


def test_relationships_count_trades_under_any_spelling(built):
    _, resolver, out = built
    r = _index(out, resolver).lookup("Bravo Electric Cooperative")
    s2 = _rel(r, "S2")
    assert s2.traded_latest and not s2.went_quiet and r.went_quiet == 1 and r.regular_sellers == 3


def test_quiet_relationships_come_first_with_a_summary(built):
    _, resolver, out = built
    r = _index(out, resolver).lookup("Alpha Municipal Power Agency")
    assert [x.went_quiet for x in r.relationships] == [True, True, False]
    assert r.summary.startswith("Watch: 2 of") and r.provenance and len(r.caveats) == 4


def test_report_lists_watch_and_volume_held_separately(built):
    m, _, out = built
    report = (out / "dormancy_report.md").read_text()
    watch, held = report.split("## Flagged watch")[1].split("## Met the seller test")
    assert "Alpha Municipal Power Agency" in watch and "Golf Energy Trading" not in watch
    assert "Golf Energy Trading" in held
    assert m["baseline"]["companies_seller_test"] == 2 and m["baseline"]["explained_relationships"] == 2


def test_unknown_names_are_unresolved(built):
    _, resolver, out = built
    r = _index(out, resolver).lookup("Zzyzx Holdings")
    assert r.flag == DormancyFlag.UNRESOLVED and r.company is None and r.relationships == []


def test_a_build_from_another_methodology_version_is_refused(built):
    _, resolver, out = built
    companies, rels, manifest = _read(out)
    with pytest.raises(DataError):
        DormancyIndex(resolver, companies, rels, {**manifest, "methodology_version": "dormancy-0.1.0"})


def test_build_needs_five_consecutive_quarters(tmp_path):
    with pytest.raises(DataError):
        build(_db(tmp_path, quarters=Q[2:]), tmp_path / "out", EntityResolver(ROWS))
