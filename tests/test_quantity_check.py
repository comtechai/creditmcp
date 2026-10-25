"""The quantity check: energy rows priced under $0.50 a MWh, when they can't be real sales, count at a thousandth.

Some sellers report kWh where EQR wants MWh, which makes a quarter look a thousand times too big, and
some bill an adjustment as a row that repeats a month's MWh. The total charge gives both away. But
energy does trade near zero at times (some western hours, some solar contracts), so a cheap row alone
isn't enough: it must also be too big for a real sale, come from a seller that reported that buyer in
kWh that quarter, or, in PJM, MISO or NYISO, be priced hundreds of times below that seller's usual
price to that buyer.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]


def _reality_check():
    path = ROOT / "reality_check.py"
    if not path.exists():
        pytest.skip("reality_check.py is not in the repo root")
    pytest.importorskip("pandas")
    spec = importlib.util.spec_from_file_location("reality_check", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ROWS = [  # product, MWh as reported, total charge, read as kWh?
    ("ENERGY", 38_193_307.0, 1_527_732.0, True),   # $0.04 a MWh: kWh entered as MWh
    ("Energy", 4_307_790.0, 430_779.0, True),      # $0.10 a MWh, any case of product name
    ("ENERGY", 5_000.0, 3_000.0, False),           # $0.60 a MWh: low, but over the line
    ("ENERGY", 900.0, 10.0, False),                # under 1,000 MWh as reported: too small to matter
    ("ENERGY", 50_000.0, 0.0, False),              # no charge: can't tell
    ("ENERGY", 50_000.0, -20_000.0, False),        # negative prices: real, not a units error
    ("ENERGY", 50_000.0, None, False),             # charge missing
    ("CAPACITY", 50_000.0, 10.0, False),           # only energy is checked
    ("ENERGY", 38_193.0, 1_527_732.0, False),      # the same sale reported in MWh: $40 a MWh
    ("ENERGY", 4_052.0, 426.0, True),              # a month's MWh repeated on an adjustment row: $0.11 a MWh
    ("ENERGY", 4_307_790.0, 219_697_290.0, False),  # MWh and dollars both scaled: $51 a MWh passes (see the jumps list)
]


def test_energy_priced_under_fifty_cents_a_mwh_counts_at_a_thousandth():
    rc = _reality_check()
    con = duckdb.connect()
    con.execute("CREATE TABLE t (product_name VARCHAR, standardized_quantity DOUBLE, total_transaction_charge DOUBLE)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?)", [r[:3] for r in ROWS])
    got = con.execute(f"SELECT {rc.kwh_sql('t')} FROM t").fetchall()
    assert [g[0] for g in got] == [r[3] for r in ROWS]


def test_the_rule_is_recorded_with_every_run():
    rc = _reality_check()
    assert (rc.KWH_MAX_PRICE, rc.KWH_MIN_QTY, rc.KWH_SURE_QTY, rc.KWH_PRICE_RATIO) == (0.5, 1000.0, 100_000.0, 300)
    assert rc.TX_COLS["total_transaction_charge"] == "DOUBLE"


# (seller, buyer, quarter, delivered in, MWh as reported, total charge, counted at a thousandth?) The cheap rows
# mirror the 60 the Oct 18 run flagged.
TRADES = [
    # a utility that switched to kWh for one buyer in 2026q2: every cheap row that quarter is kWh
    ("S1", "COOP ONE", "2025q2", None, 32_000.0, 912_000.0, False),
    ("S1", "COOP ONE", "2025q3", None, 33_000.0, 940_500.0, False),
    ("S1", "COOP ONE", "2026q2", None, 260_000_000.0, 2_600_000.0, True),   # too big for a real sale at $0.01
    ("S1", "COOP ONE", "2026q2", None, 29_985.0, 1_199.0, True),            # that seller used kWh for this buyer
    ("S1", "COOP ONE", "2026q1", None, 29_985.0, 1_199.0, False),           # ...but not in this one
    # a western hub: real trades priced near zero in some hours
    ("S2", "TRADER TWO", "2026q2", None, 1_600.0, 57_280.0, False),
    ("S2", "TRADER TWO", "2026q2", None, 1_600.0, 208.0, False),
    # PJM: a row that repeats a month's MWh as a small adjustment, 600 times below the usual price
    ("S3", "TOWN THREE", "2025q2", "PJM", 4_020.0, 254_667.0, False),
    ("S3", "TOWN THREE", "2025q2", "PJM", 4_052.0, 426.0, True),
    # MISO: cheap, but only 190 times below the usual price: left as filed
    ("S4", "BUYER FOUR", "2025q3", "MISO", 22_495.0, 675_300.0, False),
    ("S4", "BUYER FOUR", "2025q3", "MISO", 27_039.0, 4_326.0, False),
    # a solar contract whose energy is always billed at a few dollars a MWh, outside the ISOs
    ("S5", "UTILITY FIVE", "2025q3", None, 36_160.0, 124_390.0, False),
    ("S5", "UTILITY FIVE", "2025q2", None, 42_403.0, 12_231.0, False),
    # a solar project that reports kWh every quarter: no normally priced rows at all
    ("S6", "UTILITY FIVE", "2025q2", None, 4_667_460.0, 159_889.0, True),
    # PJM, cheap and small, with nothing to compare it with: left as filed
    ("S7", "TOWN THREE", "2025q4", "PJM", 5_000.0, 250.0, False),
]


def test_a_cheap_row_counts_at_a_thousandth_only_when_it_cant_be_a_real_sale():
    rc = _reality_check()
    con = duckdb.connect()
    con.execute("CREATE TABLE t (seller_company_id_ferc VARCHAR, customer_norm VARCHAR, year_quarter VARCHAR, "
                "geo_scope VARCHAR, standardized_quantity DOUBLE, total_transaction_charge DOUBLE, "
                "product_name VARCHAR, row_id INTEGER)")
    con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?, ?, 'ENERGY', ?)",
                    [list(r[:6]) + [i] for i, r in enumerate(TRADES)])
    got = dict(con.execute(f"SELECT row_id, kwh_fixed FROM ({rc.quantity_check_sql('t')})").fetchall())
    assert [got[i] for i in range(len(TRADES))] == [r[6] for r in TRADES]
    mwh = dict(con.execute(f"SELECT row_id, mwh FROM ({rc.quantity_check_sql('t')})").fetchall())
    assert mwh[2] == 260_000.0 and mwh[6] == 1_600.0
