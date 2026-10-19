"""EIA-861 name history and the buyer-based trade pull.

A company that renamed itself sits in EQR under its old name, which the roster doesn't know; EIA-861
records every name a utility filed under, abbreviated ("South Mississippi El Pwr Assn"). And a co-op
in MISO can buy from the utility next door, delivered outside the ISO, so trades are kept by buyer
as well as by delivery area.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _reality_check():
    path = ROOT / "reality_check.py"
    if not path.exists():
        pytest.skip("reality_check.py is not in the repo root")
    pd = pytest.importorskip("pandas")
    spec = importlib.util.spec_from_file_location("reality_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, pd


def test_eia_abbreviations_are_spelled_out():
    rc, _ = _reality_check()
    spelled = lambda raw: rc.normalize_name(rc.spell_out_eia(raw))  # noqa: E731
    assert spelled("Northwind El Pwr Assn") == rc.normalize_name("Northwind Electric Power Association")
    assert spelled("Upland Plains Mun P Agny") == "UPLAND PLAINS MUNICIPAL POWER AGENCY"
    assert spelled("Harbor El Memb Corp") == "HARBOR ELECTRIC MEMBERSHIP"
    assert spelled("Pub Util Dist No 1 of Fernhill Cnty") == "PUBLIC UTILITY DISTRICT NO 1 OF FERNHILL COUNTY"
    assert spelled("Riverbend Pub Serv Co") == "RIVERBEND PUBLIC SERVICE"
    assert spelled("El Pwr Bd of Glenmoor") == "ELECTRIC POWER BOARD OF GLENMOOR"
    assert spelled("El Paso Electric Co") == "EL PASO ELECTRIC"            # a place name, not an abbreviation
    assert spelled("Mesa P Holdings") == "MESA P HOLDINGS"                  # P is Power only inside Mun P Agny


def _history_db(rc, pd):
    con = duckdb.connect()
    roster = ["Northwind Energy Cooperative", "City of Glenmoor", "Larkspur Vale Power Cooperative",
              "Larkspur Vale Energy LLC", "Thistledown Electric Cooperative"]
    con.register("_r", pd.DataFrame({"entity_id": [f"R{i + 1}" for i in range(len(roster))], "entity_name": roster,
                                     "name_norm": [rc.normalize_name(n) for n in roster]}))
    con.execute("CREATE TABLE roster_entities AS SELECT * FROM _r")
    con.execute("CREATE TABLE eia861_raw (report_date DATE, utility_id_eia BIGINT, utility_name_eia VARCHAR, "
                "nerc_region VARCHAR, wholesale_power_purchases_mwh DOUBLE, net_generation_mwh DOUBLE, "
                "retail_sales_mwh DOUBLE, sales_for_resale_mwh DOUBLE)")
    rows = []
    for y in range(2012, 2025):  # Northwind: renamed in 2017; reports in two NERC regions from 2020
        name = "Northwind El Pwr Assn" if y < 2017 else "Northwind Energy Cooperative"
        rows.append((dt.date(y, 1, 1), 9001, name, "SERC", 1000.0, 5000.0, 4000.0, 2000.0))
        if y >= 2020:
            rows.append((dt.date(y, 1, 1), 9001, name, "MRO", 500.0, 0.0, 300.0, 0.0))
    for uid in (9002, 9003):  # two utilities named City of Glenmoor: ties nothing
        rows.append((dt.date(2024, 1, 1), uid, "City of Glenmoor", "RF", 100.0, 0.0, 100.0, 0.0))
    for y in (2008, 2016):  # one utility whose history holds two roster companies' names: ties neither
        name = "Larkspur Vale Energy LLC" if y < 2010 else "Larkspur Vale Power Coop"
        rows.append((dt.date(y, 1, 1), 9004, name, "MRO", 10.0, 0.0, 10.0, 0.0))
    rows.append((dt.date(2023, 1, 1), 9005, "Thistledown El Coop Inc", "MRO", None, 50.0, 40.0, 0.0))
    # Utilities that keep their data private all file under one ID: a name there ties nothing.
    rows.append((dt.date(2024, 1, 1), 88888, "City of Glenmoor", "RF", 100.0, 0.0, 100.0, 0.0))
    rows.append((dt.date(2024, 1, 1), 88888, "Larkspur Vale Energy LLC", "RF", 100.0, 0.0, 100.0, 0.0))
    con.executemany("INSERT INTO eia861_raw VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return con


def test_a_renamed_utility_brings_its_former_names_as_aliases():
    rc, pd = _reality_check()
    con = _history_db(rc, pd)
    stats = rc.link_eia_history(con)
    assert dict(con.execute("SELECT entity_id, utility_id_eia FROM eia_links").fetchall()) == {"R1": 9001, "R5": 9005}
    aliases = con.execute("SELECT alias_norm, variant, raw_examples FROM entity_eia_names WHERE entity_id = 'R1' "
                          "ORDER BY 1").fetchall()
    assert aliases == [("NORTHWIND EL POWER ASSOCIATION", "eia_as_filed", "Northwind El Pwr Assn (2012-2016)"),
                       ("NORTHWIND ELECTRIC POWER ASSOCIATION", "eia_spelled_out", "Northwind El Pwr Assn (2012-2016)")]
    # Thistledown is tied through its spelled-out name, which is its roster name, so only the abbreviated
    # form is new. The roster name itself is never repeated as an alias.
    assert con.execute("SELECT alias_norm, variant FROM entity_eia_names WHERE entity_id = 'R5'").fetchall() == [
        ("THISTLEDOWN EL COOPERATIVE", "eia_as_filed")]
    assert (stats["links"], stats["aliases"], stats["companies_with_aliases"]) == (2, 3, 2)
    assert stats["latest_purchases_year"] == 2024


def test_a_name_shared_by_two_utilities_or_two_companies_ties_nothing():
    rc, pd = _reality_check()
    con = _history_db(rc, pd)
    rc.link_eia_history(con)
    tied = {r[0] for r in con.execute("SELECT entity_id FROM eia_links").fetchall()}
    assert "R2" not in tied                     # City of Glenmoor: two utilities
    assert not tied & {"R3", "R4"}              # Larkspur Vale: one utility, two roster companies
    assert not con.execute("SELECT count(*) FROM eia_hist_names WHERE utility_id_eia = 88888").fetchone()[0]


def test_purchases_add_up_across_nerc_regions():
    rc, pd = _reality_check()
    con = _history_db(rc, pd)
    rc.link_eia_history(con)
    got = con.execute("SELECT year, purchases_mwh, net_generation_mwh FROM entity_eia861 WHERE entity_id = 'R1' "
                      "AND year IN (2019, 2024) ORDER BY 1").fetchall()
    assert got == [(2019, 1000.0, 5000.0), (2024, 1500.0, 5000.0)]


def test_eia_names_add_exact_matches_only():
    rc, pd = _reality_check()
    con = duckdb.connect()
    con.execute("CREATE TABLE a AS SELECT 'R1' AS entity_id, 'NORTHWIND ELECTRIC POWER ASSOCIATION' AS name_norm")
    con.execute("CREATE TABLE c AS SELECT * FROM (VALUES ('NORTHWIND ELECTRIC POWER ASSOCIATION'), "
                "('NORTHWINDELECTRIC POWER ASSOCIATION'), ('NORTHWIND ELECTRIC POWER ASSOCIATES')) t(name_norm)")
    rc.build_candidates(con, "a", "c", "exact_only", fuzzy=False)
    rc.build_candidates(con, "a", "c", "with_fuzzy")
    assert sorted(r[0] for r in con.execute("SELECT method FROM exact_only").fetchall()) == ["exact", "exact_nospace"]
    assert "fuzzy" in {r[0] for r in con.execute("SELECT method FROM with_fuzzy").fetchall()}


def test_trades_are_kept_by_buyer_as_well_as_by_delivery_area(tmp_path):
    rc, pd = _reality_check()
    con = duckdb.connect()
    con.execute("CREATE TABLE tx_buyers AS SELECT * FROM (VALUES ('NORTHWIND ENERGY COOPERATIVE')) t(raw_key)")
    base = dict(year_quarter="2026q2", seller_company_id_ferc="C1", transaction_unique_id="T", type_of_rate="FIXED",
                product_name="ENERGY", point_of_delivery_specific_location="", standardized_quantity=10.0,
                total_transaction_charge=400.0)
    rows = [  # (buyer, delivery area, product, rate type, kept?)
        ("Someone Else LLC", "PJM", "ENERGY", "FIXED", True),            # in PJM: kept whoever buys
        ("Northwind Energy Cooperative", "SOCO", "ENERGY", "FIXED", True),  # a roster buyer outside the ISOs
        ("  northwind energy cooperative ", "", "ENERGY", "FIXED", True),  # no area filled in; case and spaces aside
        ("Someone Else LLC", "SOCO", "ENERGY", "FIXED", False),          # neither
        ("Northwind Energy Cooperative", "SOCO", "FUEL CHARGE", "FIXED", False),  # not a kept product
        ("Northwind Energy Cooperative", "MISO", "ENERGY", "RTO/ISO", False),     # a sale to the market operator
    ]
    df = pd.DataFrame([{**base, "transaction_unique_id": f"T{i}", "customer_company_name": b,
                        "point_of_delivery_balancing_authority": ba, "product_name": prod, "type_of_rate": rate}
                       for i, (b, ba, prod, rate, _) in enumerate(rows)])
    path = tmp_path / "2026q2.parquet"
    con.register("_t", df)
    con.execute(f"COPY (SELECT * FROM _t) TO '{path.as_posix()}' (FORMAT parquet)")
    sql = rc.select_from_files(con, "transactions", [("2026q2", path.as_posix())], rc.TX_COLS, rc.tx_filter_sql())
    kept = sorted(r[0] for r in con.execute(f"SELECT transaction_unique_id FROM ({sql})").fetchall())
    assert kept == sorted(f"T{i}" for i, r in enumerate(rows) if r[-1])
