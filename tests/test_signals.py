"""Offline tests for market signals: a tiny reality-check database, built in a temp folder.

Run: pytest tests/test_signals.py
"""

from __future__ import annotations

import csv
import datetime as dt
import json

import duckdb
import pytest

from creditmcp import signals
from creditmcp.names import normalize_name
from creditmcp.resolve import DataError, EntityResolver
from creditmcp.schema import (
    AffiliateGroup,
    CoverageLevel,
    PlaceholderBill,
    QuantityFix,
    ResultStatus,
    SellerSignal,
    SellerStatus,
    SellerTrend,
    VolumeTrend,
)
from creditmcp.signals import (
    BASIS_YOY,
    NO_COVERAGE_CAVEAT,
    NO_QUANTITY_CAVEAT,
    REASON_AFFILIATE,
    REASON_AGREEMENT,
    REASON_GROUP,
    REASON_OTHER,
    SignalsIndex,
    _concentration_sentence,
    _quantity_sentence,
    build,
)

Q = ["2025q2", "2025q3", "2025q4", "2026q1", "2026q2"]
QK = {q: int(q[:4]) * 4 + int(q[5]) - 1 for q in Q}
QSTART = {q: dt.date(int(q[:4]), (int(q[5]) - 1) * 3 + 1, 1) for q in Q}

COMPANIES = {"E1": "Alpha Municipal Power Agency", "E2": "Bravo Electric Cooperative", "E3": "Charlie Energy LLC",
             "E4": "Delta Power Authority", "E5": "Echo Retail Power LLC", "E6": "Foxtrot Power Marketing",
             "E7": "Golf Energy Trading", "E8": "Hotel Energy Services", "E9": "India Power Coop",
             "E10": "Juliet Utility", "E11": "Kilo Retail LLC", "E12": "Kilo Generation LLC",
             "E13": "Lima Power LLC", "E14": "Mike Energy Co", "E15": "November Power Cooperative",
             "E16": "Oscar Municipal Utility", "E17": "Papa Power Cooperative", "E18": "Quebec Energy LLC",
             "E19": "Romeo Utility", "E20": "Sierra Electric Cooperative", "E21": "Tango Power LLC",
             "E22": "Uniform Power Agency", "E23": "Victor Electric", "E24": "Whiskey Power",
             "E25": "Xray Power Cooperative", "E26": "Yankee Power Coop", "E27": "Zulu Energy Cooperative"}
ROWS = [{"entity_id": e, "entity_name": n, "isos": "PJM", "source": "roster", "alias_norm": normalize_name(n)}
        for e, n in COMPANIES.items()]
ROWS.append({"entity_id": "E2", "entity_name": COMPANIES["E2"], "isos": "PJM", "source": "eqr_customer",
             "alias_norm": normalize_name("Bravo Electric Coop Assn")})  # a second spelling sellers use
GROUPS = {e: AffiliateGroup(name="Kilo Generation LLC", entity_id="E12", is_voting_member=e == "E12", source="fixture")
          for e in ("E11", "E12")}
SEASONAL = {"2025q2": 100.0, "2025q3": 1000.0, "2025q4": 1000.0, "2026q1": 1000.0, "2026q2": 100.0}
NOW = Q[4:]


def case(seller, cust, traded, *, under=Q, actual=None, energy=True, mwh=None, csa=None, latest_as=None,
         latest_other=False, early_as=None, kwh=None, bill=None):
    return dict(seller=seller, cust=cust, traded=traded, under=under, actual=actual, energy=energy,
                mwh=mwh or {}, csa=csa, latest_as=latest_as, latest_other=latest_other, early_as=early_as,
                kwh=kwh or {}, bill=bill)


CASES = [
    case("S1", "Alpha Municipal Power Agency", Q[:4]),           # E1: 2 of 3 stop, volume -67% -> early warning
    case("S2", "Alpha Municipal Power Agency", Q[:4]),
    case("S3", "Alpha Municipal Power Agency", Q),
    case("S1", "Bravo Electric Cooperative", Q[:4]),             # E2: 1 of 3 stops
    case("S2", "Bravo Electric Coop Assn", Q),                   # E2: trading under the second spelling
    case("S3", "Bravo Electric Cooperative", Q),
    case("S1", "Charlie Energy LLC", Q),                         # E3: kept trading
    case("S1", "Delta Power Authority", ["2025q3"]),             # E4: one quarter of trades, never regular
    case("S2", "Echo Retail Power LLC", Q[:4], under=Q[:4], actual="2026-02-15"),  # E5: contract ended
    case("S1", "Foxtrot Power Marketing", Q[:4], energy=False),  # E6: capacity only, no energy at all
    case("S2", "Foxtrot Power Marketing", Q[:4], energy=False),
    case("S1", "Golf Energy Trading", Q[:4]),                    # E7: 2 of 3 stop, but volume rose
    case("S2", "Golf Energy Trading", Q[:4]),
    case("S3", "Golf Energy Trading", Q, mwh={"2026q2": 400.0}),
    case("S1", "Hotel Energy Services", Q[:4], csa="H-100", latest_as="Hotel Energy Svcs New Name"),  # E8: renamed
    case("S2", "Hotel Energy Services", Q),
    case("S2", "India Power Coop", Q[:4], csa="WSPP"),           # E9: stopped under a shared master agreement
    case("S2", "Juliet Utility", Q, csa="WSPP"),                 # E10: shares the master agreement
    case("S3", "India Power Coop", Q),
    case("S3", "Kilo Retail LLC", Q[:4]),                        # E11: S3's book moved to its affiliate E12
    case("S3", "Kilo Generation LLC", NOW),
    case("S1", "Kilo Retail LLC", Q),
    case("S1", "Lima Power LLC", Q[:4], csa="L-7", latest_as="Charlie Energy LLC"),     # E13: agreement handed to
                                                                                        # an unrelated company
    case("S3", "Mike Energy Co", Q[:4], csa="N/A", latest_as="Mike Energy Co New Name"),  # E14: placeholder ID
    case("S1", "November Power Cooperative", Q[:4], latest_other=True),  # E15: energy stopped, capacity continues
    case("S2", "November Power Cooperative", Q),
    case("S1", "Oscar Municipal Utility", Q[:4], mwh={q: 10.0 for q in Q}),  # E16: 2 of 3 stop, seasonal buyer:
    case("S2", "Oscar Municipal Utility", Q[:4], mwh={q: 10.0 for q in Q}),  # -87% against the average,
    case("S3", "Oscar Municipal Utility", Q, mwh=SEASONAL),                   # -17% against last year
    case("S1", "Papa Power Cooperative", Q),                     # E17: growing, two new sellers
    case("S4", "Papa Power Cooperative", NOW),
    case("S5", "Papa Power Cooperative", NOW),
    case("S1", "Quebec Energy LLC", Q[:4]),                      # E18: two regulars stop, two new sellers,
    case("S2", "Quebec Energy LLC", Q[:4]),                      # volume flat: a change of sellers
    case("S4", "Quebec Energy LLC", NOW),
    case("S5", "Quebec Energy LLC", NOW),
    case("S2", "Romeo Utility", Q, csa="R-1", early_as="Romeo Utility Old Name"),  # E19: renamed before this year
    case("S1", "Sierra Electric Cooperative", Q, mwh={"2026q2": 300.0}),           # E20: more concentrated
    case("S2", "Sierra Electric Cooperative", Q),
    case("S6", "Tango Power LLC", NOW),                          # E21: started buying
    case("S1", "Victor Electric", Q[:4], csa="V-1", latest_as="Various"),          # E23: placeholder buyer name
    case("S3", "Whiskey Power", Q, csa="W-1"),                   # E24: two buyers on one agreement at once
    case("S3", "Some Other Buyer Inc", Q, csa="W-1"),
    case("S7", "Xray Power Cooperative", Q, kwh={"2026q2": 100_000.0}),          # E25: one quarter entered in kWh
    case("S8", "Yankee Power Coop", Q, mwh={"2025q2": 100_000.0}),                 # E26: MWh and dollars both scaled
    case("S3", "Zulu Energy Cooperative", Q),                                        # E27: one seller trades as usual,
    case("S9", "Zulu Energy Cooperative", Q, bill=250_000.0),                        # one bills a month as 1 MWh
]                                                                # E22: no trades or contracts at all


def _db(tmp_path, quarters=Q, quantity=True):
    """quantity=False builds tx_base the way runs did before the quantity check: no mwh or kwh_fixed columns."""
    path = tmp_path / "rc.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE quarters (qk INTEGER, q VARCHAR, q_start DATE, q_end DATE)")
    con.execute("CREATE TABLE contracts (qk INTEGER, seller_company_id_ferc VARCHAR, seller_company_name VARCHAR, "
                "customer_norm VARCHAR, contract_key VARCHAR, actual_termination_date DATE)")
    con.execute("CREATE TABLE key_flags (contract_key VARCHAR, eligible BOOLEAN)")
    con.execute("CREATE TABLE tx_base (qk INTEGER, year_quarter VARCHAR, seller_company_id_ferc VARCHAR, "
                "customer_norm VARCHAR, contract_service_agreement_id VARCHAR, is_energy BOOLEAN, "
                "standardized_quantity DOUBLE"
                + (", total_transaction_charge DOUBLE, mwh DOUBLE, kwh_fixed BOOLEAN)" if quantity else ")"))
    for q in Q:
        con.execute("INSERT INTO quarters VALUES (?, ?, ?, ?)", [QK[q], q, QSTART[q], QSTART[q]])
    def tx(row, kwh=False, usd=None):  # row ends with the quantity as reported; reality_check counts kWh rows at 1/1000
        price = 0.04 if kwh else 40.0  # $40 a MWh, or the same sale's dollars spread over kWh
        charge = usd if usd is not None else row[-1] * price
        extra = [charge, row[-1] / 1000 if kwh else row[-1], kwh] if quantity else []
        con.execute(f"INSERT INTO tx_base VALUES ({', '.join(['?'] * (len(row) + len(extra)))})", row + extra)

    last = Q[-1]
    for i, c in enumerate(CASES):
        key, csa = f"K{i}", c["csa"] or f"K{i}"
        cust = normalize_name(c["cust"])
        con.execute("INSERT INTO key_flags VALUES (?, true)", [key])
        for q in c["under"]:
            con.execute("INSERT INTO contracts VALUES (?, ?, ?, ?, ?, ?)",
                        [QK[q], c["seller"], f"Seller {c['seller']} LLC", cust, key, c["actual"]])
        for q in c["traded"]:
            if q in quarters:
                name = normalize_name(c["early_as"]) if c["early_as"] and q != last else cust
                qty = 1.0 if c["bill"] else c["kwh"].get(q, c["mwh"].get(q, 100.0))
                tx([QK[q], q, c["seller"], name, csa, c["energy"], qty], kwh=q in c["kwh"], usd=c["bill"])
        if last in quarters and c["latest_as"]:
            tx([QK[last], last, c["seller"], normalize_name(c["latest_as"]), csa, True, 100.0])
        if last in quarters and c["latest_other"]:
            tx([QK[last], last, c["seller"], cust, csa, False, 50.0])
    con.close()
    return path


@pytest.fixture
def built(tmp_path):
    resolver = EntityResolver(ROWS, groups=GROUPS)
    m = build(_db(tmp_path), tmp_path / "out", resolver, {"contract_window": ["2023q3", "2026q2"]})
    return m, resolver, tmp_path / "out"


def _read(out):
    read = lambda p: list(csv.DictReader(open(p, newline="", encoding="utf-8")))  # noqa: E731
    return (read(out / "signals_companies.csv"), read(out / "signals_relationships.csv"),
            json.loads((out / "signals_manifest.json").read_text()))


def _index(resolver, out):
    read = lambda p: list(csv.DictReader(open(p, newline="", encoding="utf-8")))  # noqa: E731
    return SignalsIndex(resolver, *_read(out), coverage=read(out / "signals_coverage.csv"))


@pytest.fixture
def idx(built):
    _, resolver, out = built
    return _index(resolver, out)


def _rel(result, seller):
    return next(x for x in result.relationships if x.seller_cid == seller)


def test_signals_read_both_directions(idx, built):
    m = built[0]
    alpha, papa = idx.lookup("Alpha Municipal Power Agency"), idx.lookup("Papa Power Cooperative")
    assert (alpha.volume.trend, alpha.sellers.trend) == (VolumeTrend.DOWN, SellerTrend.NARROWER)
    assert alpha.early_warning.met and alpha.sellers.stopped == 2 and alpha.sellers.kept_trading == 1
    assert (papa.volume.trend, papa.sellers.trend) == (VolumeTrend.UP, SellerTrend.BROADER)
    assert papa.sellers.new == 2 and papa.volume.change == pytest.approx(2.0) and not papa.early_warning.met
    assert idx.lookup("Tango Power LLC").volume.trend == VolumeTrend.STARTED
    assert idx.lookup("Foxtrot Power Marketing").volume.trend == VolumeTrend.NONE
    assert idx.lookup("Uniform Power Agency").status == ResultStatus.NO_ACTIVITY
    assert m["latest_quarter"] == "2026q2" and m["prior_quarters"] == Q[:4] and m["same_quarter_last_year"] == "2025q2"
    assert m["methodology_version"] == "signals-0.6.0" and m["rules"]["agreement_check"]
    assert m["rules"]["quantity_check"] == {"applied": True, "max_price": 0.5, "min_mwh": 1000.0,
                                            "sure_mwh": 100_000.0, "price_ratio": 300}


def test_in_short_says_more_or_less_and_from_whom(idx):
    assert idx.lookup("Alpha Municipal Power Agency").in_short == (
        "Buying less, from fewer sellers. Meets the early-warning bar.")
    assert idx.lookup("Papa Power Cooperative").in_short == "Buying more, from more sellers."
    assert idx.lookup("Quebec Energy LLC").in_short == "Buying about the same, with a change of sellers."
    assert idx.lookup("Golf Energy Trading").in_short == "Buying more, from fewer sellers."
    assert idx.lookup("Tango Power LLC").in_short == "Started buying in 2026q2, from 1 seller."
    assert idx.lookup("Echo Retail Power").in_short == "Bought no energy from EQR sellers in 2026q2."
    assert idx.lookup("Juliet Utility").in_short == "Buying about the same, from the same seller."


def test_the_summary_reads_the_record(idx):
    papa = idx.lookup("Papa Power Cooperative").summary
    assert "up 200% on the same quarter last year (market median:" in papa
    assert "It traded energy with 3 sellers in 2026q2 (1 in 2025q2), 2 of them new." in papa
    assert "Its one regular seller kept trading." in papa
    alpha = idx.lookup("Alpha Municipal Power Agency").summary
    assert "Of its 3 regular sellers, 1 kept trading and 2 stopped while still under contract." in alpha
    assert "It meets the early-warning bar" in alpha and "All of its energy came from Seller S3 LLC." in alpha
    golf = idx.lookup("Golf Energy Trading").summary
    assert "enough for the early-warning bar, but its energy volume didn't fall by half" in golf
    assert "Its one regular seller reached the end of its contract." in idx.lookup("Echo Retail Power").summary


def test_a_change_of_sellers_with_volume_held_is_not_a_warning(idx):
    quebec = idx.lookup("Quebec Energy LLC")
    assert quebec.volume.trend == VolumeTrend.STEADY and quebec.sellers.trend == SellerTrend.STEADY
    assert quebec.sellers.new == 2 and quebec.sellers.stopped == 2 and not quebec.early_warning.met


def test_only_energy_trades_make_a_seller_regular(idx):
    r = idx.lookup("Foxtrot Power Marketing")
    assert r.sellers.regular == 0 and r.in_short == "No energy bought from EQR sellers in the last five quarters."
    s1 = _rel(r, "S1")
    assert s1.quarters_traded == [] and s1.quarters_other_products == Q[:4] and s1.status == SellerStatus.QUIET


def test_a_seller_still_trading_other_products_is_not_counted_as_stopped(idx):
    r = idx.lookup("November Power Cooperative")
    s1 = _rel(r, "S1")
    assert s1.status == SellerStatus.STOPPED_NOT_COUNTED and s1.reason == REASON_OTHER
    assert r.sellers.stopped == 0 and r.sellers.other_products_latest == 1
    assert "1 stopped selling it energy but still trades other products with it" in r.summary


def test_volume_is_compared_with_the_same_quarter_last_year(idx):
    oscar = idx.lookup("Oscar Municipal Utility")
    assert oscar.sellers.stopped == 2 and oscar.volume.basis == BASIS_YOY
    assert oscar.volume.change == pytest.approx(100 / 120 - 1, abs=1e-3)       # -17%, not -87% against the average
    assert oscar.volume.trend == VolumeTrend.STEADY and not oscar.early_warning.met
    assert oscar.volume.mwh_by_quarter == {"2025q2": 120.0, "2025q3": 1020.0, "2025q4": 1020.0, "2026q1": 1020.0,
                                           "2026q2": 100.0}
    assert _rel(idx.lookup("Golf Energy Trading"), "S3").mwh_by_quarter["2026q2"] == 400.0


def test_every_result_carries_the_market_comparison(idx, built):
    m = built[0]
    assert m["baseline"]["median_volume_change"] is not None
    assert "the median change in 2026q2 was" in idx.lookup("Papa Power Cooperative").market
    assert idx.lookup("Papa Power Cooperative").volume.market_median_change == m["baseline"]["median_volume_change"]


def test_a_renamed_buyer_is_neither_a_lost_seller_nor_a_new_one(idx):
    hotel = idx.lookup("Hotel Energy Services")                 # new name in the latest quarter
    s1 = _rel(hotel, "S1")
    assert s1.status == SellerStatus.KEPT_TRADING and s1.other_buyer_names == ["HOTEL ENERGY SERVICES NEW NAME"]
    assert hotel.volume.trend == VolumeTrend.STEADY and hotel.sellers.stopped == 0
    romeo = idx.lookup("Romeo Utility")                         # old name before the latest quarter
    s2 = _rel(romeo, "S2")
    assert s2.status == SellerStatus.KEPT_TRADING and s2.regular and romeo.sellers.new == 0
    assert romeo.volume.trend == VolumeTrend.STEADY and romeo.volume.mwh_same_quarter_last_year == 100.0
    assert 'count toward Romeo Utility ("ROMEO UTILITY OLD NAME")' in romeo.summary


def test_a_shared_master_agreement_explains_nothing(idx):
    assert _rel(idx.lookup("India Power Coop"), "S2").status == SellerStatus.STOPPED


def test_an_agreement_handed_to_an_unrelated_company_still_counts(idx):
    assert _rel(idx.lookup("Lima Power LLC"), "S1").status == SellerStatus.STOPPED


def test_a_placeholder_agreement_id_explains_nothing(idx):
    assert _rel(idx.lookup("Mike Energy Co"), "S3").status == SellerStatus.STOPPED


def test_a_placeholder_buyer_name_never_links(idx):
    r = idx.lookup("Victor Electric")
    s1 = _rel(r, "S1")
    assert s1.other_buyer_names == [] and r.volume.mwh_latest == 0.0
    assert s1.status == SellerStatus.STOPPED_NOT_COUNTED and s1.reason == REASON_AGREEMENT


def test_two_buyers_on_one_agreement_at_once_never_link(idx):
    r = idx.lookup("Whiskey Power")
    assert _rel(r, "S3").other_buyer_names == [] and r.volume.mwh_by_quarter == {q: 100.0 for q in Q}


def test_moves_within_an_affiliate_group_are_neither_stopped_nor_new(idx):
    retail = idx.lookup("Kilo Retail LLC")
    s3 = _rel(retail, "S3")
    assert s3.status == SellerStatus.STOPPED_NOT_COUNTED and s3.reason == REASON_AFFILIATE
    assert retail.sellers.stopped == 0 and "1 moved to an affiliate in its PJM group" in retail.summary
    gen = idx.lookup("Kilo Generation LLC")
    assert _rel(gen, "S3").status == SellerStatus.CARRIED_OVER and _rel(gen, "S3").reason == REASON_GROUP
    assert gen.sellers.new == 0 and gen.sellers.carried_over == 1 and gen.volume.trend == VolumeTrend.STARTED
    assert "That seller had already traded with an affiliate in its PJM group, so isn't counted as new." in gen.summary


def test_relationships_count_trades_under_any_spelling(idx):
    r = idx.lookup("Bravo Electric Cooperative")
    assert _rel(r, "S2").status == SellerStatus.KEPT_TRADING and r.sellers.regular == 3 and r.sellers.stopped == 1


def test_concentration_compares_the_largest_seller_now_and_a_year_earlier(idx):
    r = idx.lookup("Sierra Electric Cooperative")
    assert r.sellers.top_seller == "Seller S1 LLC" and r.sellers.top_seller_share == 0.75
    assert r.sellers.top_seller_share_last_year == 0.5 and _rel(r, "S1").share_latest == 0.75
    assert ("Its largest seller, Seller S1 LLC, supplied 75% of its energy (50% for the largest a year earlier)."
            in r.summary)


def test_a_seller_name_ending_in_a_period_ends_the_sentence_once():
    s = SellerSignal(trading_latest=1, trading_same_quarter_last_year=1, trend=SellerTrend.STEADY, new=0, occasional=0,
                     regular=1, kept_trading=1, stopped=0, top_seller="Ohio Valley Supply Co.", top_seller_share=1.0)
    assert _concentration_sentence(s) == "All of its energy came from Ohio Valley Supply Co."


def test_sellers_trading_now_come_first_then_those_that_stopped(idx):
    alpha = idx.lookup("Alpha Municipal Power Agency")
    assert [x.status for x in alpha.relationships] == [SellerStatus.KEPT_TRADING, SellerStatus.STOPPED,
                                                       SellerStatus.STOPPED]
    assert alpha.provenance and len(alpha.caveats) == 6 and "not a verdict" in alpha.disclaimer
    assert "counts at a thousandth" in alpha.caveats[-2] and alpha.quantity_fixes == []
    assert alpha.caveats[-1] == NO_COVERAGE_CAVEAT     # this fixture run has no EIA-861 purchases


def test_the_market_baseline_counts_both_directions(built):
    b = built[0]["baseline"]
    assert b["relationships"]["new"] == 5 and b["relationships"]["carried_over"] == 1
    assert b["companies_seller_test"] == 4 and b["companies_early_warning"] == 1
    assert b["not_counted"] == {REASON_OTHER: 1, REASON_AGREEMENT: 1, REASON_AFFILIATE: 1}
    assert b["relationships_linked_by_agreement"] == 2


def test_the_internal_report_lists_both_directions(built):
    _, _, out = built
    report = (out / "signals_report.md").read_text()
    warn, rest = report.split("### Meets the early-warning bar")[1].split("### Enough sellers stopped")
    held, rest = rest.split("### Buying more")
    more, new = rest.split("### Most new sellers")
    assert "Alpha Municipal Power Agency" in warn and "Golf Energy Trading" not in warn
    assert all(n in held for n in ("Golf Energy Trading", "Oscar Municipal Utility", "Quebec Energy LLC"))
    assert "Papa Power Cooperative" in more and "Quebec Energy LLC" in new and "Not for publication" in report


def test_unknown_names_are_unresolved(idx):
    r = idx.lookup("Zzyzx Holdings")
    assert r.status == ResultStatus.UNRESOLVED and r.company is None and r.relationships == []


def test_a_build_from_another_methodology_version_is_refused(built):
    _, resolver, out = built
    companies, rels, manifest = _read(out)
    with pytest.raises(DataError):
        SignalsIndex(resolver, companies, rels, {**manifest, "methodology_version": "dormancy-0.2.0"})


def test_build_needs_five_consecutive_quarters(tmp_path):
    with pytest.raises(DataError):
        build(_db(tmp_path, quarters=Q[2:]), tmp_path / "out", EntityResolver(ROWS))


def test_energy_entered_in_kwh_counts_at_a_thousandth(idx, built):
    m, _, out = built
    r = idx.lookup("Xray Power Cooperative")
    assert _rel(r, "S7").mwh_by_quarter == {q: 100.0 for q in Q}
    assert r.volume.trend == VolumeTrend.STEADY and r.volume.mwh_latest == 100.0
    assert r.quantity_fixes == [QuantityFix(seller_name="Seller S7 LLC", quarter="2026q2", rows=1,
                                            reported_mwh=100_000.0, counted_mwh=100.0)]
    assert ("Seller S7 LLC's report for 2026q2 was priced under $0.50 a MWh, too little for that much energy "
            "(usually kWh entered as MWh, or an adjustment to energy billed in another row), so it counts here at a "
            "thousandth: 100,000 MWh as reported, 100 counted.") in r.summary
    c = m["coverage"]
    assert (c["kwh_rows"], c["kwh_sellers"], c["kwh_mwh_reported"], c["kwh_mwh_counted"]) == (1, 1, 100_000.0, 100.0)
    report = (out / "signals_report.md").read_text()
    assert "1 row from 1 seller; 100,000 MWh as reported, 100 counted." in report
    assert "### Energy reports counted at a thousandth (1, by MWh as reported)" in report   # not "top 15 of 1"
    assert "| Seller S7 LLC | Xray Power Cooperative | 2026q2 | 1 | 100,000 | 100 |" in report
    assert "### Thousandfold jumps within one seller relationship (1)" in report
    assert "| Seller S8 LLC | Yankee Power Coop | 100 | 100,000 | 1,000x |" in report   # passes the price test


def test_a_run_without_charges_says_the_check_was_not_applied(tmp_path):
    resolver = EntityResolver(ROWS, groups=GROUPS)
    m = build(_db(tmp_path, quantity=False), tmp_path / "out", resolver, {"contract_window": ["2023q3", "2026q2"]})
    assert m["rules"]["quantity_check"]["applied"] is False and m["coverage"]["kwh_rows"] == 0
    r = _index(resolver, tmp_path / "out").lookup("Xray Power Cooperative")
    assert r.caveats[-2] == NO_QUANTITY_CAVEAT and r.quantity_fixes == []
    assert r.volume.mwh_latest == 100_000.0      # counted as filed: this is what the check exists to catch
    assert "Quantity check not applied" in (tmp_path / "out" / "signals_report.md").read_text()


def test_corrections_from_several_sellers_are_named_together():
    fixes = [QuantityFix(seller_name=n, quarter=q, rows=1, reported_mwh=4e6, counted_mwh=4e3)
             for n, q in (("Seller A LLC", "2025q2"), ("Seller A LLC", "2025q3"), ("Seller B Co.", "2026q2"))]
    assert _quantity_sentence(fixes, 0.5) == (
        "Reports from 2 sellers were priced under $0.50 a MWh, too little for that much energy (usually kWh entered "
        "as MWh, or an adjustment to energy billed in another row), so they count here at a thousandth of the "
        "reported MWh: Seller A LLC (2025q2 and 2025q3), Seller B Co. (2026q2).")
    assert _quantity_sentence(fixes[:2], 0.5).startswith("Seller A LLC's reports for 2025q2 and 2025q3 were priced")
    assert _quantity_sentence([], 0.5) == ""


# --- coverage: how much of a company's buying EQR shows ------------------------------------------------------------
EIA = [  # company, EIA utility, year, purchases from every source, own generation (MWh)
    ("E1", 101, 2025, 100_000.0, 0.0),     # Alpha: 1,000 MWh in EQR over the last four quarters: about 1%
    ("E3", 103, 2024, 8_000.0, 0.0),       # Charlie: an older year is passed over...
    ("E3", 103, 2025, 10_000.0, 2_500.0),  # ...for the latest: 500 / 10,000 = 5% (Lima's agreement adds 100)
    ("E2", 102, 2025, 2_000.0, 0.0),       # Bravo: 1,100 / 2,000 = 55%: partial
    ("E17", 117, 2025, 700.0, 0.0),        # Papa: 600 / 700 = 86%: high
    ("E21", 121, 2025, 80.0, 0.0),         # Tango: EQR shows a little more than it reported buying (125%)
    ("E19", 119, 2025, 100.0, 0.0),        # Romeo: EQR shows four times as much: the two don't compare
    ("E22", 122, 2024, 5_000.0, 1_000.0),  # Uniform: no EQR contracts or trades at all
    ("E15", 115, 2021, 900.0, 0.0),        # November: too old to compare with 2026
    ("E16", 116, 2025, 0.0, 300.0),        # Oscar: reported no purchases
]


def _with_eia(path):
    """What a run with EIA-861 adds: the roster, and each tied company's yearly sources of power. The run numbers its
    companies its own way (R...); the roster name ties them to the resolver's."""
    con = duckdb.connect(str(path))
    con.execute("CREATE TABLE roster_entities (entity_id VARCHAR, entity_name VARCHAR, name_norm VARCHAR)")
    con.execute("CREATE TABLE entity_eia861 (entity_id VARCHAR, utility_id_eia BIGINT, year INTEGER, "
                "purchases_mwh DOUBLE, net_generation_mwh DOUBLE, retail_sales_mwh DOUBLE, "
                "sales_for_resale_mwh DOUBLE)")
    rid = {e: f"R{i:03d}" for i, e in enumerate(COMPANIES)}
    for e, n in COMPANIES.items():
        con.execute("INSERT INTO roster_entities VALUES (?, ?, ?)", [rid[e], n, normalize_name(n)])
    for e, uid, y, purchases, gen in EIA:
        con.execute("INSERT INTO entity_eia861 VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [rid[e], uid, y, purchases, gen, 1.0, 0.0])
    con.close()
    return path


@pytest.fixture
def eia_idx(tmp_path, monkeypatch):
    monkeypatch.setattr(signals, "COVER_LIST_MIN_MWH", 0.0)    # list every company in the report's coverage tables
    resolver = EntityResolver(ROWS, groups=GROUPS)
    m = build(_with_eia(_db(tmp_path)), tmp_path / "out", resolver, {"contract_window": ["2023q3", "2026q2"]})
    return _index(resolver, tmp_path / "out"), m, tmp_path / "out"


def test_low_coverage_leads_the_one_line_answer(eia_idx):
    idx, _, _ = eia_idx
    charlie = idx.lookup("Charlie Energy LLC")
    assert charlie.coverage.level == CoverageLevel.LOW and charlie.coverage.share == 0.05
    cov = charlie.coverage
    assert (cov.eia_year, cov.eia_purchases_mwh, cov.eqr_mwh) == (2025, 10_000, 500)
    assert charlie.coverage.eqr_quarters == Q[1:] and charlie.coverage.eia_utility_id == 103
    assert charlie.in_short == ("EQR shows only about 5% of its power purchases. In that slice: buying more, from the "
                                "same seller.")
    assert ("Over the four quarters to 2026q2, EQR shows only 500 MWh of energy bought, about 5% of the 10,000 MWh it "
            "reported buying from all sources for 2025 (EIA-861), and it generated 2,500 MWh of its own. The rest is "
            "out of view: bought from the ISO market") in charlie.summary
    alpha = idx.lookup("Alpha Municipal Power Agency")
    assert alpha.in_short == ("EQR shows only about 1% of its power purchases. In that slice: buying less, from fewer "
                              "sellers. Meets the early-warning bar.")


def test_partial_and_high_coverage(eia_idx):
    idx, _, _ = eia_idx
    bravo = idx.lookup("Bravo Electric Cooperative")
    assert bravo.coverage.level == CoverageLevel.PARTIAL and bravo.coverage.share == 0.55
    assert bravo.in_short.endswith(". EQR shows about 55% of its power purchases.")
    papa = idx.lookup("Papa Power Cooperative")
    assert papa.coverage.level == CoverageLevel.HIGH and papa.in_short == "Buying more, from more sellers."
    assert ("EQR shows 600 MWh of energy bought, about 86% of the 700 MWh it reported buying from all sources for 2025 "
            "(EIA-861), so these signals see most of its buying.") in papa.summary
    tango = idx.lookup("Tango Power LLC")
    assert tango.coverage.share == 1.25 and "more than the 80 MWh it reported buying" in tango.summary
    romeo = idx.lookup("Romeo Utility")
    assert romeo.coverage.level == CoverageLevel.UNKNOWN and romeo.coverage.share is None
    assert ("EQR shows 400 MWh of energy bought over the four quarters to 2026q2, far more than the 100 MWh it "
            "reported buying for 2025 (EIA-861), so the two don't measure the same thing") in romeo.summary
    assert not romeo.in_short.startswith("EQR shows")


def test_a_company_with_no_eqr_activity_shows_none_of_its_buying(eia_idx):
    r = eia_idx[0].lookup("Uniform Power Agency")
    assert r.status == ResultStatus.NO_ACTIVITY and r.coverage.level == CoverageLevel.LOW and r.coverage.share == 0
    assert r.in_short == "EQR shows none of its power purchases (5,000 MWh in 2024)."
    assert ("EQR shows none of the 5,000 MWh it reported buying from all sources for 2024 (EIA-861), and it generated "
            "1,000 MWh of its own: it buys from the ISO market") in r.summary


def test_coverage_is_unknown_without_a_recent_eia_figure(eia_idx):
    idx, _, _ = eia_idx
    november, oscar, delta = (idx.lookup(n) for n in ("November Power Cooperative", "Oscar Municipal Utility",
                                                       "Delta Power Authority"))
    assert all(r.coverage.level == CoverageLevel.UNKNOWN and r.coverage.share is None for r in (november, oscar, delta))
    assert "its latest EIA-861 purchases are for 2021, too old to compare" in november.summary
    assert "It reported no wholesale purchases to EIA for 2025" in oscar.summary
    assert "hasn't tied it to an EIA-861 utility" in delta.coverage.note and delta.coverage.note in delta.summary
    assert delta.in_short == idx.lookup("Delta Power Authority").in_short and "EQR shows" not in delta.in_short
    assert november.caveats[-1].startswith("Coverage compares the energy EQR shows")


def test_the_build_counts_coverage_and_lists_sample_picks(eia_idx):
    _, m, out = eia_idx
    ec = m["eia_coverage"]
    assert ec["available"] and ec["companies_linked"] == 9 and ec["eia_years"] == {"2021": 1, "2024": 1, "2025": 7}
    assert ec["levels"] == {"high": 2, "partial": 1, "low": 2, "unknown": 21} and ec["median_share"] == 0.55
    assert ec["linked_without_activity"] == 1 and ec["quarters"] == Q[1:] and ec["not_comparable"] == 1
    report = (out / "signals_report.md").read_text()
    picks, low = report.split("### EQR shows 25% or more")[1].split("### Largest buyers where EQR shows under 25%")
    assert picks.index("Papa Power Cooperative") < picks.index("Tango Power LLC") < picks.index("Bravo Electric")
    assert "Charlie Energy LLC" not in picks
    assert "| Papa Power Cooperative | PJM | 600 | 700 (2025) | 86% | +200% | 3 (1) |" in picks
    low, over = low.split("### EQR shows more than 150%")
    assert low.index("Alpha Municipal Power Agency") < low.index("Charlie Energy LLC")
    assert "| Romeo Utility | PJM | 400 | 100 (2025) | 400% |" in over and "Romeo Utility" not in picks


def test_a_build_without_eia_says_coverage_is_unknown(idx, built):
    r = idx.lookup("Charlie Energy LLC")
    assert r.coverage.level == CoverageLevel.UNKNOWN and "this build has no EIA-861 purchases" in r.coverage.note
    assert "EIA-861" not in r.summary           # said once, in the caveats, not in every summary
    assert built[0]["eia_coverage"]["available"] is False
    assert "Not available: this run has no EIA-861 purchases" in (built[2] / "signals_report.md").read_text()


# --- reading the record: placeholder bills and moves inside the steady band ---------------------------------------
def test_bills_with_a_placeholder_quantity_are_named(idx, built):
    m, _, out = built
    zulu = idx.lookup("Zulu Energy Cooperative")
    assert zulu.placeholder_bills == [PlaceholderBill(seller_name="Seller S9 LLC", quarters=Q[1:], rows=4,
                                                      billed_usd=1_000_000.0)]
    assert _rel(zulu, "S9").status == SellerStatus.KEPT_TRADING        # a bill is still a sale
    # $1.0 million of bills against $16,000 of energy billed at real prices by its other seller
    assert zulu.placeholder_billing_share == round(1_000_000 / 1_016_000, 4)
    assert ("Seller S9 LLC reported its sales to it over the last four quarters as bills with a placeholder quantity "
            "of a few MWh ($1.0 million billed, about 98% of what EQR sellers billed it for energy over that time), so "
            "that energy isn't counted and its volumes here understate its buying.") in zulu.summary
    charlie = idx.lookup("Charlie Energy LLC")
    assert charlie.placeholder_bills == [] and charlie.placeholder_billing_share is None
    c = m["coverage"]
    assert (c["placeholder_rows"], c["placeholder_sellers"], c["placeholder_usd"]) == (5, 1, 1_250_000.0)
    assert m["rules"]["placeholder_check"] == {"applied": True, "max_mwh": 10.0, "min_price": 10_000.0,
                                               "small_share": 0.02}
    report = (out / "signals_report.md").read_text()
    assert "5 rows from 1 seller, $1.2 million billed ($1.2 million to roster companies)" in report
    assert "### Bills with a placeholder quantity (1, by dollars)" in report
    assert ("| Seller S9 LLC | Zulu Energy Cooperative | 2025q2, 2025q3, 2025q4, 2026q1, 2026q2 | 5 | $1.2 million "
            "| 98% |" in report)


def test_a_small_placeholder_bill_reads_as_small():
    bills = [PlaceholderBill(seller_name="Solar A LLC", quarters=["2026q1"], rows=2, billed_usd=184_508.0),
             PlaceholderBill(seller_name="Solar B LLC", quarters=["2026q2"], rows=1, billed_usd=20_000.0)]
    span = "the four quarters to 2026q2"
    small = signals._placeholder_sentence(bills[:1], span, False, 0.0002)
    assert small == ("Solar A LLC reported its sales to it over the four quarters to 2026q2 as bills with a placeholder "
                     "quantity of a few MWh ($184,508 billed, under 1% of what EQR sellers billed it for energy over "
                     "that time). That energy isn't counted, but at that size it hardly changes its volumes.")
    large = signals._placeholder_sentence(bills, span, True, 0.22)
    assert large == ("2 sellers reported sales to it over the four quarters to 2026q2 as bills with a placeholder "
                     "quantity of a few MWh (Solar A LLC and Solar B LLC: $204,508 billed in all, about 22% of what EQR "
                     "sellers billed it for energy over that time), so that energy isn't counted and its volumes and "
                     "coverage here understate its buying.")
    unsized = signals._placeholder_sentence(bills[:1], span, False)        # a build with no billing to compare with
    assert unsized.endswith("($184,508 billed), so that energy isn't counted and its volumes here understate its "
                            "buying.")
    assert signals._placeholder_sentence([], span, True, 0.5) == ""


def test_a_run_without_charges_has_no_placeholder_check(tmp_path):
    resolver = EntityResolver(ROWS, groups=GROUPS)
    m = build(_db(tmp_path, quantity=False), tmp_path / "out", resolver, {"contract_window": ["2023q3", "2026q2"]})
    assert m["rules"]["placeholder_check"]["applied"] is False and m["coverage"]["placeholder_rows"] == 0
    assert _index(resolver, tmp_path / "out").lookup("Zulu Energy Cooperative").placeholder_bills == []


def test_moves_inside_the_steady_band_read_as_a_little_more_or_less(idx):
    oscar = idx.lookup("Oscar Municipal Utility")                     # -17% against the same quarter last year
    assert oscar.volume.trend == VolumeTrend.STEADY
    assert oscar.in_short == "Buying a little less, from fewer sellers."
    assert ("bought 100 MWh of energy from EQR sellers in 2026q2, down 17% on the same quarter last year (market "
            "median:") in oscar.summary
    assert "inside the 25% band CreditMCP counts as steady." in oscar.summary
    assert idx.lookup("Juliet Utility").in_short == "Buying about the same, from the same seller."
