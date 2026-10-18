"""The quantity check: energy rows priced under $0.50 a MWh count at a thousandth.

Some sellers report kWh where EQR wants MWh, which makes a quarter look a thousand times too big, and
some bill an adjustment as a row that repeats a month's MWh. The total charge gives both away.
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
    assert (rc.KWH_MAX_PRICE, rc.KWH_MIN_QTY) == (0.5, 1000.0)
    assert rc.TX_COLS["total_transaction_charge"] == "DOUBLE"
