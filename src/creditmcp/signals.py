"""CreditMCP — market signals: what a company's wholesale power trading shows, in either direction.

Every FERC-jurisdictional seller reports each bilateral wholesale power sale in its Electric
Quarterly Report (EQR). Read from the buyer's side, those reports are a trading record: how much
energy a company buys, from which sellers, and how that changes. This module turns the record into
signals for each roster company in PJM and MISO, comparing the latest quarter with the same quarter
a year earlier and with the four quarters before it.

A relationship is one seller (FERC company ID) and one roster company.
  - Under contract in a quarter: an eligible power-sale contract between them is reported that
    quarter and has no actual termination date before the quarter starts.
  - Regular: the seller traded energy with the company in at least 3 of the 4 prior quarters.
    Only energy trades count: capacity, booked-out power and requirements service come in lumps
    by design (capacity follows the June-May delivery year).
  - In the latest quarter each relationship is one of:
      kept_trading         regular, and traded energy with it
      new                  traded energy with it after no trades of any kind in the prior four quarters
      occasional           traded energy with it; traded before, but not regularly
      carried_over         would be new, but the seller already traded with an affiliate in its PJM group
      stopped              regular, contract still in force, and no trades of any kind
      stopped_not_counted  regular and stopped selling it energy, but still trades other products with
                           it, kept trading under the same agreement with a new buyer name, or kept
                           trading with an affiliate in its PJM group
      contract_ended       regular, no energy trades, and no contract in force
      quiet                not regular, and no energy trades

Buyer names linked by agreement. EQR buyer names are free text, so a renamed buyer can look like a
lost seller under its old name and a new seller under its new one. When a seller used one agreement
ID with exactly one roster company and exactly one other buyer name, one name before the other
(sharing at most one quarter), that name's trades on that agreement count as the company's.
Agreement IDs used with two known companies, placeholder IDs and placeholder buyer names never link.

Signals for each company:
  - Volume: energy MWh bought from all EQR sellers, by quarter. The latest quarter is compared with
    the same quarter a year earlier, because seasonal buyers make a four-quarter average misleading;
    when that quarter is empty, with the prior-quarter average. Up or down means 25% or more.
  - Sellers: how many traded energy with it in the latest quarter and a year earlier (broader or
    narrower means at least 2 and 25% more or fewer), and what each regular seller did.
  - Concentration: the largest seller's share of its energy, now and a year earlier.
  - Early warning: 2+ regular sellers under contract stopped, they are at least half of them, and
    energy volume fell by at least half. Sellers going quiet while their contracts stay in force can
    mean they are cutting credit exposure without terminating. The thresholds are provisional until
    they are backtested against public failures.

Quantities. Some sellers enter kWh in EQR's MWh field, which makes a quarter look a thousand times too
big, and some bill an adjustment as a separate row that repeats a month's MWh. reality_check.py counts
an energy row priced under $0.50 a MWh (on at least 1,000 MWh as reported) at a thousandth when it
can't be a real sale: 100,000 MWh or more, a seller that reported that buyer in kWh that quarter, or,
in PJM, MISO or NYISO, a price hundreds of times below the seller's usual price to that buyer. Every
answer names the reports it corrected. Other cheap rows count as filed: a price that low is not proof
of an error, since energy does trade near zero at times. A seller that scales both the MWh and the
dollars passes the price test, so the build report lists thousandfold jumps within a seller
relationship for a check by hand. Some sellers report a month's sales as a bill with a placeholder
quantity of a few MWh; that energy can't be counted. Every answer names the sellers that did and sizes
their bills against what EQR sellers billed the company for energy, so a small bill reads as small.

Coverage. EQR shows only bilateral sales by sellers that file it, so for some companies it is most
of their buying and for others a sliver. reality_check.py ties each roster company it can to one
EIA-861 utility by an exact name, and EIA-861 gives that utility's wholesale purchases from every
source. Each answer compares the energy EQR shows over the last four quarters with those purchases
(the latest year EIA has, so the periods differ: a guide, not a measurement). Under 25% is low
coverage, and it leads the one-line answer.

Every change is evidence to check, not a verdict: seasonal buyers, hedges that matured, contract
structure and purchases moved to the ISO markets all change bilateral volumes.

Inputs: a reality-check run with five consecutive quarters of transactions (its DuckDB file), and
resolve_entity's exact name map, so customers resolve the same way everywhere in CreditMCP.

    python -m creditmcp.signals build                  # writes rc_trades/signals/
    python -m creditmcp.signals "Company name"
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import statistics
from collections import Counter
from pathlib import Path

import duckdb

from creditmcp.resolve import DataError, EntityResolver
from creditmcp.schema import (
    Confidence,
    Coverage,
    CoverageLevel,
    EarlyWarning,
    MarketSignalsResult,
    PlaceholderBill,
    Provenance,
    QuantityFix,
    ResultStatus,
    SellerRelationship,
    SellerSignal,
    SellerStatus,
    SellerTrend,
    VolumeSignal,
    VolumeTrend,
)

METHODOLOGY_VERSION = "signals-0.6.0"
PRIOR_N = 4                  # prior quarters the "regular" test looks back over
REGULAR_MIN = 3              # quarters with energy trades, out of PRIOR_N, for a relationship to count as regular
VOLUME_BAND = 0.25           # a volume change of at least this share is up or down; anything less is steady
SLIGHT_BAND = 0.10           # inside the steady band, a change of at least this share reads as a little more or less
SELLER_MIN_CHANGE = 2        # broader or narrower needs at least this many more or fewer sellers...
SELLER_BAND = 0.25           # ...and at least this share more or fewer
TURNOVER_MIN = 2             # new sellers, and regular sellers lost, each at least this many: "a change of sellers"
WARN_MIN_STOPPED = 2         # provisional early-warning bar, to be set by backtests on public failures
WARN_MIN_SHARE = 0.5
WARN_MIN_VOLUME_DROP = 0.5
KWH_RULE = {"KWH_MAX_PRICE": 0.5, "KWH_MIN_QTY": 1000.0, "KWH_SURE_QTY": 100_000.0,  # reality_check.py's defaults;
            "KWH_PRICE_RATIO": 300}                                                 # a run records its own
PLACEHOLDER_MAX_MWH = 10.0   # an energy row of this many MWh or fewer, billed at more than PLACEHOLDER_MIN_PRICE a MWh,
PLACEHOLDER_MIN_PRICE = 10_000.0  # is a bill with a placeholder quantity: no market prices energy that high
PLACEHOLDER_SMALL = 0.02     # placeholder bills under this share of a company's energy billing hardly change its volumes
JUMP_RATIO = 300             # the build report lists relationships whose largest quarter is this many times
JUMP_MIN_MWH = 100_000.0     # their smallest, when the largest is at least this big
COVER_LOW = 0.25             # EQR shows under this share of the purchases a company reported to EIA: low coverage
COVER_HIGH = 0.6             # at or above this share: high; in between: partial
COVER_MAX = 1.5              # EQR showing more than this multiple of reported purchases: the two don't measure the
                             # same thing (trading companies), so no coverage is given
EIA_MAX_AGE = 3              # years: EIA-861 purchases older than this aren't compared with the latest quarter
COVER_LIST_MIN_MWH = 100_000.0  # the build report's coverage lists skip companies buying less than this in EQR
DEFAULT_RUN = "rc_trades"
OUT_DIR = "signals"
REASON_OTHER = "still trades other products with it"
REASON_AGREEMENT = "same agreement, new buyer name"
REASON_AFFILIATE = "kept trading with an affiliate"
REASON_GROUP = "already traded with an affiliate"
STOP_REASONS = (REASON_OTHER, REASON_AGREEMENT, REASON_AFFILIATE)
BASIS_YOY = "the same quarter last year"
BASIS_AVG = "the prior-quarter average"
# Agreement IDs and buyer names that name nothing: never used to link a buyer name to a company.
CSA_PLACEHOLDER = r"^(N/?A|NONE|NULL|TBD|UNKNOWN|NOT APPLICABLE|-+|0+|\.+)$"
BUYER_PLACEHOLDER = r"(VARIOUS|MULTIPLE|MISC|MISCELLANEOUS|N ?/? ?A|NONE|NULL|TBD|UNKNOWN|OTHERS?)"
TRADING = {SellerStatus.KEPT_TRADING, SellerStatus.NEW, SellerStatus.OCCASIONAL, SellerStatus.CARRIED_OVER}
LOST = {SellerStatus.STOPPED, SellerStatus.STOPPED_NOT_COUNTED, SellerStatus.CONTRACT_ENDED}
CAVEATS = [
    "Changes in trading activity are evidence to check, not a verdict: seasonal buyers, hedges that matured, contract "
    "structure and purchases moved to the ISO markets all change bilateral volumes. Only energy trades make a seller "
    "regular; capacity, booked-out power and requirements service come in lumps by design.",
    "Trades come from FERC EQR: bilateral power delivered in PJM, MISO or NYISO, plus every trade with a roster "
    "company under a buyer name sellers use for it in their contracts, wherever it was delivered. Purchases from the "
    "ISO markets, exchange trades and sales by sellers that don't file EQR are not seen; each answer's coverage says "
    "how much of the company's buying that leaves out.",
    "EQR customer names are free text typed by each seller. Trades are tied to a company through exact known "
    "spellings, and through a seller's agreement ID when the seller used it with this company and one other buyer "
    "name, one after the other; other trades are missed rather than misassigned.",
    "PUDL marks its FERC EQR tables as experimental and rebuilds them in place, so every result is tied to the "
    "snapshot hashes in provenance.",
]
QUANTITY_CAVEAT = ("Some sellers enter kWh in EQR's MWh field, and some bill adjustments as rows that repeat a "
                   "month's MWh. An energy row priced under ${price:.2f} a MWh counts at a thousandth when it can't be "
                   "a real sale: when it is 100,000 MWh or more, when the same seller reported that buyer in kWh that "
                   "quarter, or, in PJM, MISO or NYISO, when it is priced hundreds of times below that seller's usual "
                   "price. Each answer names any report it corrected. Other cheap rows count as filed, since energy "
                   "does trade near zero at times. Some sellers report a month's sales as a bill with a placeholder "
                   "quantity of a few MWh; that energy can't be counted, and each answer names the sellers that did "
                   "and sizes their bills against what EQR sellers billed the company for energy. Other reporting "
                   "errors pass through as filed.")
COVERAGE_CAVEAT = ("Coverage compares the energy EQR shows over the last four quarters with the wholesale purchases "
                   "the company reported on EIA Form 861 for its latest year, from every source. The periods differ "
                   "and EIA counts what EQR can't see, so the share is a guide to how much of its buying these "
                   "signals cover, not a measurement.")
NO_COVERAGE_CAVEAT = ("This build has no EIA-861 purchases, so answers can't say how much of a company's buying EQR "
                      "shows. Rerun reality_check.py with access to PUDL, then rebuild.")
NO_QUANTITY_CAVEAT = ("This build has no quantity check: its trades were pulled without total charges, so energy a "
                      "seller entered in kWh counts as MWh. Rerun reality_check.py, then rebuild.")
WARN_RULE = (f"{WARN_MIN_STOPPED} or more regular sellers under contract stopped trading with it, they are at least "
             f"{WARN_MIN_SHARE:.0%} of them, and its energy volume fell by at least {WARN_MIN_VOLUME_DROP:.0%} against "
             f"{BASIS_YOY} (or {BASIS_AVG} when that quarter is empty). Provisional thresholds.")
# How each regular seller's latest quarter reads in a summary: (one seller, several sellers).
CLAUSES = {
    "kept": ("kept trading", "kept trading"),
    "stopped": ("stopped while still under contract", "stopped while still under contract"),
    REASON_OTHER: ("stopped selling it energy but still trades other products with it",
                   "stopped selling it energy but still trade other products with it"),
    REASON_AGREEMENT: ("moved to a new buyer name on the same agreement",
                       "moved to new buyer names on the same agreement"),
    REASON_AFFILIATE: ("moved to an affiliate in its PJM group", "moved to affiliates in its PJM group"),
    "ended": ("reached the end of its contract", "reached the end of their contracts"),
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- the rules -----------------------------------------------------------------
def _volume(mwh_latest: float, mwh_last_year: float, mwh_prior_avg: float) -> tuple[float | None, str | None]:
    """Volume change against the same quarter last year, else the prior-quarter average."""
    if mwh_last_year > 0:
        return round(mwh_latest / mwh_last_year - 1, 3), BASIS_YOY
    if mwh_prior_avg > 0:
        return round(mwh_latest / mwh_prior_avg - 1, 3), BASIS_AVG
    return None, None


def _volume_trend(mwh_latest: float, change: float | None) -> VolumeTrend:
    if change is None:
        return VolumeTrend.STARTED if mwh_latest > 0 else VolumeTrend.NONE
    if change >= VOLUME_BAND:
        return VolumeTrend.UP
    if change <= -VOLUME_BAND:
        return VolumeTrend.DOWN
    return VolumeTrend.STEADY


def _seller_trend(now: int, before: int) -> SellerTrend:
    if now == 0 and before == 0:
        return SellerTrend.NONE
    diff, base = now - before, max(before, 1)
    if diff >= SELLER_MIN_CHANGE and diff / base >= SELLER_BAND:
        return SellerTrend.BROADER
    if -diff >= SELLER_MIN_CHANGE and -diff / base >= SELLER_BAND:
        return SellerTrend.NARROWER
    return SellerTrend.STEADY


def _seller_test(regular_under_contract: int, stopped: int) -> bool:
    return (regular_under_contract > 0 and stopped >= WARN_MIN_STOPPED
            and stopped / regular_under_contract >= WARN_MIN_SHARE)


def _warning(regular_under_contract: int, stopped: int, change: float | None) -> bool:
    return (_seller_test(regular_under_contract, stopped) and change is not None
            and change <= -WARN_MIN_VOLUME_DROP)


def _cover_level(share: float | None) -> CoverageLevel:
    if share is None:
        return CoverageLevel.UNKNOWN
    if share < COVER_LOW:
        return CoverageLevel.LOW
    return CoverageLevel.PARTIAL if share < COVER_HIGH else CoverageLevel.HIGH


def _linkable(buyer: str | None, known_q: list[int] | None, other_q: list[int] | None) -> bool:
    """A buyer name links to the agreement's one known company when the two names follow one another."""
    if not buyer or len(buyer) < 4 or re.fullmatch(BUYER_PLACEHOLDER, buyer):
        return False
    if not known_q or not other_q:
        return False
    return max(other_q) <= min(known_q) or max(known_q) <= min(other_q)


# --- build -------------------------------------------------------------------
def build(db_path: str | Path, out_dir: str | Path, resolver: EntityResolver,
          run_manifest: dict | None = None) -> dict:
    """Compute relationships and per-company signals from a reality-check DuckDB file."""
    db_path, out_dir = Path(db_path), Path(out_dir)
    if not db_path.exists():
        raise DataError(f"Missing {db_path}. Run reality_check.py with five quarters of transactions first.")
    con = duckdb.connect()
    con.execute(f"ATTACH '{db_path.as_posix()}' AS rc (READ_ONLY)")
    tables = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_catalog = 'rc'").fetchall()}
    missing = {"contracts", "key_flags", "tx_base", "quarters"} - tables
    if missing:
        raise DataError(f"{db_path.name} has no {sorted(missing)}; it was run with --skip-transactions or is too old.")
    tx_cols = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_catalog = 'rc' AND table_name = 'tx_base'"
    ).fetchall()}
    has_csa = "contract_service_agreement_id" in tx_cols
    csa = (f"CASE WHEN regexp_matches(upper(trim(t.contract_service_agreement_id)), '{CSA_PLACEHOLDER}') THEN NULL "
           f"ELSE nullif(upper(trim(t.contract_service_agreement_id)), '') END" if has_csa else "NULL::VARCHAR")
    # reality_check.py marks energy rows entered in kWh (kwh_fixed) and counts them at a thousandth (mwh). The check
    # needs total charges: a run whose source files had none can't have caught anything.
    has_qty = {"mwh", "kwh_fixed", "total_transaction_charge"} <= tx_cols and con.execute(
        "SELECT count(*) > 0 FROM rc.tx_base WHERE total_transaction_charge IS NOT NULL").fetchone()[0]
    qty, kwh = ("t.mwh", "coalesce(t.kwh_fixed, false)") if has_qty else ("t.standardized_quantity", "false")
    usd = "t.total_transaction_charge" if has_qty else "NULL::DOUBLE"

    qrows = con.execute("SELECT DISTINCT year_quarter, qk FROM rc.tx_base ORDER BY qk").fetchall()
    if len(qrows) < PRIOR_N + 1:
        raise DataError(f"Market signals need {PRIOR_N + 1} quarters of transactions; this run has {len(qrows)} "
                        f"({', '.join(q for q, _ in qrows)}). Rerun reality_check.py with --tx-quarters.")
    window = qrows[-(PRIOR_N + 1):]
    if window[-1][1] - window[0][1] != PRIOR_N:
        raise DataError(f"Transaction quarters must be consecutive; got {', '.join(q for q, _ in window)}.")
    labels = {k: q for q, k in window}
    latest_k, prior_ks, last_year_k = window[-1][1], [k for _, k in window[:-1]], window[0][1]
    latest_q, last_year_q = labels[latest_k], labels[last_year_k]
    ks = ", ".join(str(k) for k in labels)
    prior_list = ", ".join(str(k) for k in prior_ks)

    names = resolver.name_map()
    con.execute("CREATE TABLE names (alias_norm VARCHAR, entity_id VARCHAR)")
    items = list(names.items())
    for i in range(0, len(items), 1000):
        chunk = items[i:i + 1000]
        con.execute("INSERT INTO names VALUES " + ", ".join(["(?, ?)"] * len(chunk)), [v for kv in chunk for v in kv])

    # Every trade row in the window, with the company its buyer name maps to by exact known spelling.
    con.execute(f"""
        CREATE VIEW x0 AS
        SELECT t.qk, t.seller_company_id_ferc AS s, {csa} AS csa, t.customer_norm AS buyer, n.entity_id AS e,
               coalesce(t.is_energy, false) AS energy, {qty} AS q, {kwh} AS kwh, t.standardized_quantity AS q_reported,
               {usd} AS usd
        FROM rc.tx_base t LEFT JOIN names n ON n.alias_norm = t.customer_norm
        WHERE t.qk IN ({ks})""")
    # Buyer names linked by agreement: a seller's agreement ID used with exactly one known company and
    # exactly one other buyer name, one after the other.
    con.execute("CREATE TABLE links (s VARCHAR, csa VARCHAR, buyer VARCHAR, e VARCHAR)")
    candidates = con.execute("""
        SELECT s, csa, any_value(e), any_value(buyer) FILTER (WHERE e IS NULL),
               list(DISTINCT qk) FILTER (WHERE e IS NOT NULL), list(DISTINCT qk) FILTER (WHERE e IS NULL)
        FROM x0 WHERE s IS NOT NULL AND csa IS NOT NULL AND nullif(buyer, '') IS NOT NULL
        GROUP BY 1, 2
        HAVING count(DISTINCT e) = 1 AND count(DISTINCT buyer) FILTER (WHERE e IS NULL) = 1""").fetchall()
    links = [(s, c, b, e) for s, c, e, b, known_q, other_q in candidates if _linkable(b, known_q, other_q)]
    for i in range(0, len(links), 1000):
        chunk = links[i:i + 1000]
        con.execute("INSERT INTO links VALUES " + ", ".join(["(?, ?, ?, ?)"] * len(chunk)),
                    [v for r in chunk for v in r])
    con.execute("""
        CREATE VIEW x AS
        SELECT x0.qk, x0.s, x0.csa, x0.buyer, coalesce(x0.e, l.e) AS e, (x0.e IS NULL AND l.e IS NOT NULL) AS linked,
               x0.energy, x0.q, x0.kwh, x0.q_reported, x0.usd
        FROM x0 LEFT JOIN links l ON l.s = x0.s AND l.csa = x0.csa AND l.buyer = x0.buyer""")

    active = con.execute(f"""
        SELECT DISTINCT c.qk, c.seller_company_id_ferc, n.entity_id
        FROM rc.contracts c
        JOIN rc.key_flags f USING (contract_key)
        JOIN names n ON n.alias_norm = c.customer_norm
        JOIN rc.quarters q ON q.qk = c.qk
        WHERE f.eligible AND c.qk IN ({ks}) AND c.seller_company_id_ferc IS NOT NULL
          AND (c.actual_termination_date IS NULL OR c.actual_termination_date >= q.q_start)""").fetchall()
    traded = con.execute("""
        SELECT qk, s, e, count(*) FILTER (WHERE energy), count(*) FILTER (WHERE NOT energy),
               coalesce(sum(q) FILTER (WHERE energy), 0)
        FROM x WHERE s IS NOT NULL AND e IS NOT NULL GROUP BY 1, 2, 3""").fetchall()
    linked_names = con.execute("SELECT DISTINCT s, e, buyer FROM x WHERE linked").fetchall()
    kwh_by = {(k, s, e): [int(n), round(rep, 1), round(cnt, 1)] for k, s, e, n, rep, cnt in con.execute("""
        SELECT qk, s, e, count(*), sum(q_reported), sum(q) FROM x
        WHERE kwh AND energy AND s IS NOT NULL AND e IS NOT NULL GROUP BY 1, 2, 3""").fetchall()}
    kwh_all = con.execute("""
        SELECT count(*), count(DISTINCT s), coalesce(sum(q_reported), 0), coalesce(sum(q), 0)
        FROM x WHERE kwh""").fetchone()
    # Bills with a placeholder quantity: a month's sales reported as a few MWh at a price no market reaches.
    bill = (f"energy AND q_reported > 0 AND q_reported <= {PLACEHOLDER_MAX_MWH} "
            f"AND usd / q_reported > {PLACEHOLDER_MIN_PRICE}")
    bill_by = {(k, s, e): [int(n), round(b, 2)] for k, s, e, n, b in con.execute(f"""
        SELECT qk, s, e, count(*), sum(usd) FROM x
        WHERE {bill} AND s IS NOT NULL AND e IS NOT NULL GROUP BY 1, 2, 3""").fetchall()}
    bill_all = con.execute(f"""
        SELECT count(*), count(DISTINCT s), coalesce(sum(usd), 0), count(e),
               coalesce(sum(usd) FILTER (WHERE e IS NOT NULL), 0)
        FROM x WHERE {bill}""").fetchone()
    # The yardstick for those bills: what EQR sellers billed each company for energy at a price no higher than that
    # (so placeholder bills, and dollars a seller scaled by mistake, stay out of it).
    billed_by = {(k, e): float(b) for k, e, b in con.execute(f"""
        SELECT qk, e, sum(usd) FROM x
        WHERE energy AND s IS NOT NULL AND e IS NOT NULL AND q_reported > 0
          AND usd / q_reported <= {PLACEHOLDER_MIN_PRICE}
        GROUP BY 1, 2""").fetchall() if b is not None}
    # Same agreement, new buyer name, where the name didn't link (for instance, more than one new name): an
    # agreement ID a seller used in the prior quarters with one roster company and no other buyer name, carrying
    # energy trades in the latest quarter with a buyer name the name map doesn't know, or an affiliate's.
    renamed = con.execute(f"""
        WITH y AS (SELECT qk, s, csa, e FROM x WHERE energy AND s IS NOT NULL),
        own AS (
            SELECT s, csa, any_value(e) AS e FROM y
            WHERE qk IN ({prior_list}) AND csa IS NOT NULL
            GROUP BY 1, 2 HAVING count(DISTINCT e) = 1 AND count(e) = count(*))
        SELECT DISTINCT o.s, o.e, y.e FROM own o JOIN y ON y.s = o.s AND y.csa = o.csa
        WHERE y.qk = {latest_k} AND y.e IS DISTINCT FROM o.e""").fetchall()
    mapped = con.execute("""
        SELECT count(*), count(e), count(*) FILTER (WHERE linked),
               coalesce(sum(q) FILTER (WHERE energy), 0), coalesce(sum(q) FILTER (WHERE energy AND e IS NOT NULL), 0)
        FROM x""").fetchone()
    seller_names = dict(con.execute("""
        SELECT seller_company_id_ferc, seller_company_name FROM rc.contracts WHERE seller_company_id_ferc IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY seller_company_id_ferc ORDER BY qk DESC, seller_company_name) = 1
        """).fetchall())

    under: set[tuple] = {(k, s, e) for k, s, e in active}
    trades: dict[tuple, tuple[int, int, float]] = {(k, s, e): (int(ne), int(no), float(m))
                                                    for k, s, e, ne, no, m in traded}
    energy = {key for key, v in trades.items() if v[0] > 0}
    other_names: dict[tuple, set[str]] = {}
    for s, e, b in linked_names:
        other_names.setdefault((s, e), set()).add(b)
    pairs = {(s, e) for _, s, e in under} | {(s, e) for _, s, e in trades}
    group = {e: resolver.affiliate_key(e) for e in {e for _, e in pairs} | {e2 for _, _, e2 in renamed if e2}}
    same_group = lambda a, b: bool(group.get(a)) and group.get(a) == group.get(b)  # noqa: E731
    same_agreement = {(s, e) for s, e, e2 in renamed if e2 is None or same_group(e, e2)}
    latest_partners: dict[str, set[str]] = {}   # seller -> companies it traded anything with in the latest quarter
    prior_partners: dict[str, set[str]] = {}    # seller -> companies it traded anything with in the prior quarters
    for k, s, e in trades:
        (latest_partners if k == latest_k else prior_partners).setdefault(s, set()).add(e)
    affiliate_in = lambda partners, s, e: any(  # noqa: E731
        e2 != e and same_group(e, e2) for e2 in partners.get(s, ()))
    order = sorted(labels)

    rel_rows = []
    for s, e in sorted(pairs):
        mwh_q = {labels[k]: round(trades.get((k, s, e), (0, 0, 0.0))[2], 1) for k in order}
        n_prior = sum((k, s, e) in energy for k in prior_ks)
        any_prior = any((k, s, e) in trades for k in prior_ks)
        regular = n_prior >= REGULAR_MIN
        under_latest = (latest_k, s, e) in under
        reason = None
        if (latest_k, s, e) in energy:
            if regular:
                status = SellerStatus.KEPT_TRADING
            elif any_prior:
                status = SellerStatus.OCCASIONAL
            elif affiliate_in(prior_partners, s, e):
                status, reason = SellerStatus.CARRIED_OVER, REASON_GROUP
            else:
                status = SellerStatus.NEW
        elif regular and under_latest:
            if (latest_k, s, e) in trades:
                status, reason = SellerStatus.STOPPED_NOT_COUNTED, REASON_OTHER
            elif (s, e) in same_agreement:
                status, reason = SellerStatus.STOPPED_NOT_COUNTED, REASON_AGREEMENT
            elif affiliate_in(latest_partners, s, e):
                status, reason = SellerStatus.STOPPED_NOT_COUNTED, REASON_AFFILIATE
            else:
                status = SellerStatus.STOPPED
        elif regular:
            status = SellerStatus.CONTRACT_ENDED
        else:
            status = SellerStatus.QUIET
        latest_trades = trades.get((latest_k, s, e), (0, 0, 0.0))
        rel_rows.append({
            "entity_id": e, "seller_cid": s, "seller_name": seller_names.get(s, s),
            "status": status.value, "reason": reason or "", "regular": regular, "under_contract_latest": under_latest,
            "quarters_traded": ";".join(labels[k] for k in order if (k, s, e) in energy),
            "quarters_other_products": ";".join(labels[k] for k in order if (k, s, e) in trades
                                                and (k, s, e) not in energy),
            "other_buyer_names": sorted(other_names.get((s, e), ())),
            "trades_latest": latest_trades[0], "other_trades_latest": latest_trades[1],
            "mwh_by_quarter": mwh_q, "mwh_latest": mwh_q[latest_q], "mwh_same_quarter_last_year": mwh_q[last_year_q],
            "mwh_prior_avg": round(sum(mwh_q[labels[k]] for k in prior_ks) / PRIOR_N, 1),
            "traded_last_year": (last_year_k, s, e) in energy, "share_latest": None,
            "kwh_fixes": {labels[k]: kwh_by[(k, s, e)] for k in order if (k, s, e) in kwh_by},
            "placeholder_bills": {labels[k]: bill_by[(k, s, e)] for k in order if (k, s, e) in bill_by},
        })

    ent_rows = []
    by_ent: dict[str, list[dict]] = {}
    for r in rel_rows:
        by_ent.setdefault(r["entity_id"], []).append(r)
    for e, rels in sorted(by_ent.items()):
        mwh_q = {labels[k]: round(sum(r["mwh_by_quarter"][labels[k]] for r in rels), 1) for k in order}
        latest, last_year = mwh_q[latest_q], mwh_q[last_year_q]
        prior_avg = round(sum(mwh_q[labels[k]] for k in prior_ks) / PRIOR_N, 1)
        change, basis = _volume(latest, last_year, prior_avg)
        for r in rels:
            r["share_latest"] = round(r["mwh_latest"] / latest, 3) if latest > 0 else None
        n = Counter(r["status"] for r in rels)
        reasons = Counter(r["reason"] for r in rels if r["status"] == SellerStatus.STOPPED_NOT_COUNTED.value)
        now = sum(n[st.value] for st in TRADING)
        before = sum(r["traded_last_year"] for r in rels)
        regular_uc = sum(r["regular"] and r["under_contract_latest"] for r in rels)
        top_now, top_before = _top(rels, latest_q, latest), _top(rels, last_year_q, last_year)
        ent_rows.append({
            "entity_id": e, "name": resolver.company(e)["name"], "isos": "+".join(resolver.company(e)["isos"]),
            "relationships": len(rels), "mwh_by_quarter": mwh_q, "mwh_latest": latest,
            "mwh_same_quarter_last_year": last_year, "mwh_prior_avg": prior_avg, "volume_change": change,
            "volume_basis": basis or "", "volume_trend": _volume_trend(latest, change).value,
            "sellers_latest": now, "sellers_last_year": before, "seller_trend": _seller_trend(now, before).value,
            "new": n[SellerStatus.NEW.value], "occasional": n[SellerStatus.OCCASIONAL.value],
            "carried_over": n[SellerStatus.CARRIED_OVER.value], "regular": sum(r["regular"] for r in rels),
            "kept_trading": n[SellerStatus.KEPT_TRADING.value], "stopped": n[SellerStatus.STOPPED.value],
            "stopped_not_counted": n[SellerStatus.STOPPED_NOT_COUNTED.value],
            "not_counted_reasons": {reason: reasons[reason] for reason in STOP_REASONS if reasons[reason]},
            "contract_ended": n[SellerStatus.CONTRACT_ENDED.value],
            "other_products_latest": sum(r["other_trades_latest"] > 0 for r in rels),
            "top_seller": top_now[0], "top_seller_share": top_now[1], "top_seller_share_last_year": top_before[1],
            "regular_under_contract": regular_uc,
            "seller_test": _seller_test(regular_uc, n[SellerStatus.STOPPED.value]),
            "early_warning": _warning(regular_uc, n[SellerStatus.STOPPED.value], change),
            "linked_names": sum(bool(r["other_buyer_names"]) for r in rels),
            "kwh_fixes": sorted(({"seller_name": r["seller_name"], "quarter": q, "rows": v[0], "reported_mwh": v[1],
                                  "counted_mwh": v[2]} for r in rels for q, v in r["kwh_fixes"].items()),
                                key=lambda f: (f["quarter"], -f["reported_mwh"], f["seller_name"])),
            "placeholder_bills": sorted(({"seller_name": r["seller_name"], "quarter": q, "rows": v[0],
                                          "billed_usd": v[1]} for r in rels for q, v in r["placeholder_bills"].items()),
                                        key=lambda f: (f["quarter"], -f["billed_usd"], f["seller_name"])),
            "energy_usd_by_quarter": ({labels[k]: round(billed_by.get((k, e), 0.0), 2) for k in order}
                                      if has_qty else {}),
        })

    # Coverage: how much of each company's buying EQR shows. reality_check.py ties a roster company to one EIA-861
    # utility by an exact name and keeps that utility's yearly sources of power (entity_eia861). Its roster name maps
    # it to the resolver's company.
    cover_qs = [labels[k] for k in order[-PRIOR_N:]]
    eia: dict[str, tuple] = {}
    if {"entity_eia861", "roster_entities"} <= tables:
        for row in con.execute("""
            SELECT n.entity_id, x.utility_id_eia, x.year, x.purchases_mwh, x.net_generation_mwh, x.retail_sales_mwh,
                   x.sales_for_resale_mwh
            FROM rc.entity_eia861 x JOIN rc.roster_entities r USING (entity_id)
            JOIN names n ON n.alias_norm = r.name_norm
            QUALIFY row_number() OVER (PARTITION BY n.entity_id ORDER BY x.purchases_mwh IS NULL, x.year DESC) = 1
            """).fetchall():
            eia[row[0]] = row[1:]
    eqr4 = {r["entity_id"]: round(sum(r["mwh_by_quarter"][q] for q in cover_qs), 1) for r in ent_rows}
    latest_year = int(latest_q[:4])
    cov_rows = []
    for e in sorted(set(eia) | set(eqr4)):
        uid, year, purchases, gen, retail, resale = eia.get(e, (None,) * 6)
        why = ("not_linked" if year is None else "too_old" if latest_year - int(year) > EIA_MAX_AGE
               else "no_purchases" if not purchases or purchases <= 0 else "")
        ratio = None if why else round(eqr4.get(e, 0.0) / purchases, 4)
        if ratio is not None and ratio > COVER_MAX:
            why = "not_comparable"
        share = None if why else ratio
        cov_rows.append({"entity_id": e, "level": _cover_level(share).value, "share": share, "ratio": ratio,
                         "why_unknown": why,
                         "eqr_mwh": eqr4.get(e, 0.0), "eia_year": year, "eia_utility_id": uid,
                         "eia_purchases_mwh": purchases, "eia_net_generation_mwh": gen, "eia_retail_sales_mwh": retail,
                         "eia_sales_for_resale_mwh": resale})
    with_signals = [r for r in cov_rows if r["entity_id"] in eqr4]
    levels = Counter(r["level"] for r in with_signals)
    shares = sorted(r["share"] for r in with_signals if r["share"] is not None)
    eia_coverage = {
        "available": bool(eia), "quarters": cover_qs, "companies_linked": len(eia),
        "eia_years": dict(sorted(Counter(str(v[1]) for v in eia.values()).items())),
        "companies_with_signals": len(ent_rows),
        "levels": {lv.value: levels[lv.value] for lv in CoverageLevel},
        "median_share": round(statistics.median(shares), 3) if shares else None,
        "linked_without_activity": sum(r["entity_id"] not in eqr4 for r in cov_rows),
        "not_comparable": sum(r["why_unknown"] == "not_comparable" for r in with_signals),
        "rule": {"COVER_LOW": COVER_LOW, "COVER_HIGH": COVER_HIGH, "COVER_MAX": COVER_MAX, "EIA_MAX_AGE": EIA_MAX_AGE},
    }

    # How often relationships under contract trade energy in each quarter: the seasonal backdrop.
    per_quarter = []
    for k in order:
        uk = [(s, e) for kk, s, e in under if kk == k]
        per_quarter.append({"quarter": labels[k], "under_contract": len(uk),
                            "traded": sum((k, s, e) in energy for s, e in uk)})

    yoy = sorted(r["volume_change"] for r in ent_rows if r["volume_basis"] == BASIS_YOY)
    rel_status = Counter(r["status"] for r in rel_rows)
    rel_reasons = Counter(r["reason"] for r in rel_rows if r["status"] == SellerStatus.STOPPED_NOT_COUNTED.value)
    baseline = {
        "companies": len(ent_rows),
        "volume_trend": {t.value: sum(r["volume_trend"] == t.value for r in ent_rows) for t in VolumeTrend},
        "seller_trend": {t.value: sum(r["seller_trend"] == t.value for r in ent_rows) for t in SellerTrend},
        "companies_with_volume_last_year": len(yoy),
        "median_volume_change": round(statistics.median(yoy), 3) if yoy else None,
        "yoy_up": sum(c >= VOLUME_BAND for c in yoy), "yoy_down": sum(c <= -VOLUME_BAND for c in yoy),
        "yoy_steady": sum(-VOLUME_BAND < c < VOLUME_BAND for c in yoy),
        "relationships": {st.value: rel_status[st.value] for st in SellerStatus},
        "not_counted": {reason: rel_reasons[reason] for reason in STOP_REASONS},
        "regular_relationships": sum(r["regular"] for r in rel_rows),
        "regular_under_contract": sum(r["regular"] and r["under_contract_latest"] for r in rel_rows),
        "companies_any_new": sum(r["new"] > 0 for r in ent_rows),
        "companies_any_stopped": sum(r["stopped"] > 0 for r in ent_rows),
        "companies_seller_test": sum(r["seller_test"] for r in ent_rows),
        "companies_early_warning": sum(r["early_warning"] for r in ent_rows),
        "relationships_linked_by_agreement": sum(bool(r["other_buyer_names"]) for r in rel_rows),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    flat = lambda row: {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in row.items()}  # noqa: E731
    paths = (out_dir / "signals_relationships.csv", out_dir / "signals_companies.csv", out_dir / "signals_coverage.csv")
    for path, rows in zip(paths, (rel_rows, ent_rows, cov_rows)):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["entity_id"])
            w.writeheader()
            w.writerows(flat(r) for r in rows)

    run_manifest = run_manifest or {}
    snaps = run_manifest.get("snapshots", {})
    tun = run_manifest.get("tunables") or {}
    kwh_rule = {"applied": has_qty, "max_price": tun.get("KWH_MAX_PRICE", KWH_RULE["KWH_MAX_PRICE"]),
                "min_mwh": tun.get("KWH_MIN_QTY", KWH_RULE["KWH_MIN_QTY"]),
                "sure_mwh": tun.get("KWH_SURE_QTY", KWH_RULE["KWH_SURE_QTY"]),
                "price_ratio": tun.get("KWH_PRICE_RATIO", KWH_RULE["KWH_PRICE_RATIO"])}
    manifest = {
        "built_at": dt.datetime.now().isoformat(timespec="seconds"),
        "methodology_version": METHODOLOGY_VERSION,
        "latest_quarter": latest_q, "prior_quarters": [labels[k] for k in prior_ks],
        "same_quarter_last_year": last_year_q,
        "rules": {"PRIOR_N": PRIOR_N, "REGULAR_MIN": REGULAR_MIN, "VOLUME_BAND": VOLUME_BAND,
                  "SELLER_MIN_CHANGE": SELLER_MIN_CHANGE, "SELLER_BAND": SELLER_BAND, "TURNOVER_MIN": TURNOVER_MIN,
                  "WARN_MIN_STOPPED": WARN_MIN_STOPPED, "WARN_MIN_SHARE": WARN_MIN_SHARE,
                  "WARN_MIN_VOLUME_DROP": WARN_MIN_VOLUME_DROP, "regular_counts_products": ["ENERGY"],
                  "stopped_means": "no trades of any kept product", "volume_basis": [BASIS_YOY, BASIS_AVG],
                  "early_warning_rule": WARN_RULE, "agreement_check": has_csa, "quantity_check": kwh_rule,
                  "placeholder_check": {"applied": has_qty, "max_mwh": PLACEHOLDER_MAX_MWH,
                                        "min_price": PLACEHOLDER_MIN_PRICE, "small_share": PLACEHOLDER_SMALL},
                  "slight_band": SLIGHT_BAND},
        "inputs": {
            "run_db": db_path.name,
            "contract_window": run_manifest.get("contract_window"),
            "snapshots": {v.get("file", k): v.get("sha256", "") for k, v in snaps.items()},
            "snapshot_taken": sorted(str(v.get("taken_at", ""))[:10] for v in snaps.values() if v.get("taken_at")),
            "pudl_release": resolver.provenance.pudl_release,
            "resolver_inputs": resolver.provenance.inputs,
            "decisions_sha256": resolver.provenance.decisions_sha256,
        },
        "coverage": {"trade_rows": mapped[0], "trade_rows_mapped": mapped[1], "trade_rows_linked": mapped[2],
                     "energy_mwh": round(mapped[3], 1), "energy_mwh_mapped": round(mapped[4], 1),
                     "names_in_map": len(names), "agreement_links": len(links),
                     "kwh_rows": kwh_all[0], "kwh_sellers": kwh_all[1], "kwh_mwh_reported": round(kwh_all[2], 1),
                     "kwh_mwh_counted": round(kwh_all[3], 1),
                     "placeholder_rows": bill_all[0], "placeholder_sellers": bill_all[1],
                     "placeholder_usd": round(bill_all[2], 2), "placeholder_rows_roster": bill_all[3],
                     "placeholder_usd_roster": round(bill_all[4], 2)},
        "eia_coverage": eia_coverage,
        "per_quarter": per_quarter,
        "baseline": baseline,
        "counts": {"relationships": len(rel_rows), "companies": len(ent_rows)},
        "outputs": {p.name: _sha256(p) for p in paths},
    }
    (out_dir / "signals_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out_dir / "signals_report.md").write_text(_report(manifest, ent_rows, rel_rows, cov_rows), encoding="utf-8")
    return manifest


def _top(rels: list[dict], quarter: str, total: float) -> tuple[str | None, float | None]:
    """The seller that supplied the most energy in a quarter, and its share."""
    if total <= 0:
        return None, None
    r = sorted(rels, key=lambda r: (-r["mwh_by_quarter"][quarter], r["seller_name"]))[0]
    return r["seller_name"], round(r["mwh_by_quarter"][quarter] / total, 3)


# --- the internal build report -------------------------------------------------
def _count(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _top15(n: int) -> str:
    """How much of a list a 15-row table shows: the count, or "top 15 of" it."""
    return f"top 15 of {n:,}" if n > 15 else f"{n:,}"


def _pct(n, d) -> str:
    return f"{100 * n / d:.1f}%" if d else "n/a"


def _usd(x: float) -> str:
    return f"${x / 1e6:,.1f} million" if abs(x) >= 1e6 else f"${x:,.0f}"


def _change(x) -> str:
    return "n/a" if x is None else f"{x:+.0%}"


def _company_table(rows: list[dict], limit: int = 40) -> list[str]:
    out = ["| Company | ISOs | Energy MWh latest | Same quarter last year | Change | Sellers now (a year ago) | "
           "New | Regular | Stopped |", "|---|---|---|---|---|---|---|---|---|"]
    for e in rows[:limit]:
        note = "" if e["volume_basis"] in (BASIS_YOY, "") else " (vs prior avg)"
        change = "started" if e["volume_trend"] == VolumeTrend.STARTED.value else _change(e["volume_change"]) + note
        out.append(f"| {e['name']} | {e['isos']} | {e['mwh_latest']:,.0f} | {e['mwh_same_quarter_last_year']:,.0f} | "
                   f"{change} | {e['sellers_latest']} ({e['sellers_last_year']}) | "
                   f"{e['new']} | {e['regular']} | {e['stopped']} |")
    return out


def _coverage_table(rows: list[dict], ents: dict[str, dict], limit: int = 15) -> list[str]:
    out = ["| Company | ISOs | EQR energy MWh, last 4 quarters | EIA-861 purchases MWh (year) | Share | Volume | "
           "Sellers now (a year ago) |", "|---|---|---|---|---|---|---|"]
    for r in rows[:limit]:
        e = ents.get(r["entity_id"]) or {}
        volume = ("started" if e.get("volume_trend") == VolumeTrend.STARTED.value else
                  _change(e.get("volume_change")) if e else "no activity")
        sellers = f"{e['sellers_latest']} ({e['sellers_last_year']})" if e else "-"
        out.append(f"| {e.get('name') or r['entity_id']} | {e.get('isos', '')} | {r['eqr_mwh']:,.0f} | "
                   f"{r['eia_purchases_mwh']:,.0f} ({r['eia_year']}) | {r['ratio']:.0%} | {volume} | {sellers} |")
    return out


def _coverage_lines(m: dict, ents: list[dict], cov: list[dict]) -> list[str]:
    ec = m["eia_coverage"]
    lines = ["## Coverage of each company's buying (EIA-861)", ""]
    if not ec["available"]:
        return lines + ["- Not available: this run has no EIA-861 purchases, so every answer's coverage is unknown. "
                        "Rerun reality_check.py with access to PUDL, then rebuild.", ""]
    lv, years = ec["levels"], ", ".join(f"{y}: {n:,}" for y, n in ec["eia_years"].items())
    lines += [
        f"- Roster companies tied to one EIA-861 utility: {ec['companies_linked']:,} (latest year with purchases, by "
        f"company: {years}). Compared with EQR energy over {ec['quarters'][0]} to {ec['quarters'][-1]}.",
        f"- Of the {ec['companies_with_signals']:,} companies with signals, EQR shows {COVER_HIGH:.0%} or more of "
        f"reported purchases for {lv['high']:,}, {COVER_LOW:.0%} to {COVER_HIGH:.0%} for {lv['partial']:,} and under "
        f"{COVER_LOW:.0%} for {lv['low']:,}; unknown for {lv['unknown']:,}, including "
        f"{ec.get('not_comparable', 0):,} where EQR shows more than {COVER_MAX:.0%} of reported purchases (the two "
        f"don't measure the same thing). Median share: "
        f"{'n/a' if ec['median_share'] is None else format(ec['median_share'], '.0%')}.",
        f"- Tied to an EIA utility but no EQR contracts or trades in the window: {ec['linked_without_activity']:,} "
        "(their answers say EQR shows none of their buying).", ""]
    by_id = {e["entity_id"]: e for e in ents}
    known = [r for r in cov if r["share"] is not None and r["entity_id"] in by_id]
    top = sorted((r for r in known if r["eqr_mwh"] >= COVER_LIST_MIN_MWH and r["level"] != CoverageLevel.LOW.value),
                 key=lambda r: (r["level"] != CoverageLevel.HIGH.value, -r["eqr_mwh"]))
    low = sorted((r for r in known if r["level"] == CoverageLevel.LOW.value),
                 key=lambda r: -(r["eia_purchases_mwh"] or 0))
    over = sorted((r for r in cov if r["why_unknown"] == "not_comparable" and r["entity_id"] in by_id),
                  key=lambda r: -r["eqr_mwh"])
    for title, rows in ((f"EQR shows {COVER_LOW:.0%} or more, buying at least {COVER_LIST_MIN_MWH:,.0f} MWh in EQR "
                         f"(sample picks: high coverage first, then by volume; {_top15(len(top))})", top),
                        (f"Largest buyers where EQR shows under {COVER_LOW:.0%} ({_top15(len(low))})", low),
                        (f"EQR shows more than {COVER_MAX:.0%} of reported purchases, so no coverage is given "
                         f"({_top15(len(over))})", over)):
        lines += [f"### {title}", ""] + (_coverage_table(rows, by_id) if rows else ["None."]) + [""]
    return lines


def _report(m: dict, ents: list[dict], rels: list[dict], cov: list[dict] | None = None) -> str:
    b, c, r = m["baseline"], m["coverage"], m["rules"]
    kr = r["quantity_check"]
    qty_line = (f"- Energy rows counted at a thousandth (kWh entered as MWh, or adjustments: priced under "
                f"${kr['max_price']:.2f} a MWh on at least {kr['min_mwh']:,.0f} MWh as reported, and one of: "
                f"{kr['sure_mwh']:,.0f} MWh or more; a seller that reported that buyer in kWh that quarter; in PJM, "
                f"MISO or NYISO, at least {kr['price_ratio']} times below the seller's usual price): "
                f"{_count(c['kwh_rows'], 'row')} from {_count(c['kwh_sellers'], 'seller')}; "
                f"{c['kwh_mwh_reported']:,.0f} MWh as reported, {c['kwh_mwh_counted']:,.0f} counted."
                if kr["applied"] else
                "- Quantity check not applied: this run's trades have no total charges, so energy entered in kWh "
                "counts as MWh. Rerun reality_check.py, then rebuild.")
    pc = r.get("placeholder_check") or {}
    bill_line = (f"- Energy rows billed with a placeholder quantity ({pc['max_mwh']:,.0f} MWh or less at over "
                 f"${pc['min_price']:,.0f} a MWh): {_count(c['placeholder_rows'], 'row')} from "
                 f"{_count(c['placeholder_sellers'], 'seller')}, {_usd(c['placeholder_usd'])} billed "
                 f"({_usd(c['placeholder_usd_roster'])} to roster companies). That energy isn't counted."
                 if pc.get("applied") else "")
    rel, nc, vt, st = b["relationships"], b["not_counted"], b["volume_trend"], b["seller_trend"]
    q, ly = m["latest_quarter"], m["same_quarter_last_year"]
    exact = c["trade_rows_mapped"] - c["trade_rows_linked"]
    lines = [
        "# Market signals build (internal)",
        "",
        f"Latest quarter {q}, compared with {ly} (the same quarter last year) and with "
        f"{', '.join(m['prior_quarters'])}. Methodology {m['methodology_version']}; built {m['built_at']}.",
        "",
        "## Coverage",
        "",
        f"- Trade rows tied to a roster company: {c['trade_rows_mapped']:,} of {c['trade_rows']:,} "
        f"({_pct(c['trade_rows_mapped'], c['trade_rows'])}); {exact:,} by exact known spelling and "
        f"{c['trade_rows_linked']:,} through {c['agreement_links']:,} buyer names linked by agreement. Energy MWh "
        f"tied: "
        f"{_pct(c['energy_mwh_mapped'], c['energy_mwh'])}.",
        qty_line,
        *([bill_line] if bill_line else []),
        f"- Relationships (seller and roster company): {m['counts']['relationships']:,}, across "
        f"{m['counts']['companies']:,} companies. {b['relationships_linked_by_agreement']:,} include trades under a "
        "linked buyer name.",
        "",
        *_coverage_lines(m, ents, cov or []),
        f"## The market in {q}",
        "",
        f"- Energy volume against {ly}, across the {b['companies_with_volume_last_year']:,} companies that bought "
        f"energy then: median {_change(b['median_volume_change'])}; {b['yoy_up']:,} up 25% or more, "
        f"{b['yoy_steady']:,} about the same, {b['yoy_down']:,} down 25% or more. Started buying (none in the four "
        f"prior quarters): {vt.get('started', 0):,}.",
        f"- Sellers trading energy with a company, against {ly}: {st.get('broader', 0):,} companies broader, "
        f"{st.get('steady', 0):,} about the same, {st.get('narrower', 0):,} narrower.",
        f"- Regular relationships (energy trades in {r['REGULAR_MIN']}+ of the {r['PRIOR_N']} prior quarters): "
        f"{b['regular_relationships']:,}. Kept trading: {rel['kept_trading']:,}. Stopped while still under contract: "
        f"{rel['stopped']:,}. Stopped selling energy but not counted: {nc.get(REASON_OTHER, 0):,} still trade other "
        f"products, {nc.get(REASON_AGREEMENT, 0):,} moved to a new buyer name on the same agreement, "
        f"{nc.get(REASON_AFFILIATE, 0):,} moved to an affiliate. Contract ended: {rel['contract_ended']:,}.",
        f"- New relationships: {rel['new']:,} (at {b['companies_any_new']:,} companies), plus "
        f"{rel['carried_over']:,} carried over from an affiliate. Occasional sellers trading: {rel['occasional']:,}.",
        f"- Early-warning bar: {r['early_warning_rule']} Companies meeting the seller part: "
        f"{b['companies_seller_test']:,}; meeting the bar: {b['companies_early_warning']:,}.",
        "",
        "## How often relationships under contract trade energy, by quarter",
        "",
        "| Quarter | Under contract | Traded energy | Share |",
        "|---|---|---|---|",
    ]
    for pq in m["per_quarter"]:
        lines.append(f"| {pq['quarter']} | {pq['under_contract']:,} | {pq['traded']:,} | "
                     f"{_pct(pq['traded'], pq['under_contract'])} |")
    warn = sorted((e for e in ents if e["early_warning"]), key=lambda e: (-e["stopped"], -e["mwh_prior_avg"]))
    held = sorted((e for e in ents if e["seller_test"] and not e["early_warning"]),
                  key=lambda e: (-e["stopped"], -e["mwh_prior_avg"]))
    more = sorted((e for e in ents if e["volume_trend"] == VolumeTrend.UP.value),
                  key=lambda e: -(e["mwh_latest"] - e["mwh_same_quarter_last_year"]))
    new = sorted((e for e in ents if e["new"]), key=lambda e: (-e["new"], -e["mwh_latest"]))
    lines += ["", "## Internal review lists", "",
              "Not for publication: the repo publishes the method, not lists of named companies, and write-ups are "
              "backtests on public failures.", ""]
    for title, rows in ((f"Meets the early-warning bar ({len(warn)})", warn),
                        (f"Enough sellers stopped for the bar, but volume held ({len(held)})", held),
                        (f"Buying more: largest increases in MWh ({_top15(len(more))})", more[:15]),
                        (f"Most new sellers ({_top15(len(new))})", new[:15])):
        lines += [f"### {title}", ""] + (_company_table(rows) if rows else ["None."]) + [""]
    names = {e["entity_id"]: e["name"] for e in ents}
    fixes = sorted(((f, e["name"]) for e in ents for f in e["kwh_fixes"]), key=lambda x: -x[0]["reported_mwh"])
    lines += [f"### Energy reports counted at a thousandth ({_top15(len(fixes))}, by MWh as reported)", ""]
    lines += (["| Seller | Company | Quarter | Rows | MWh as reported | Counted |", "|---|---|---|---|---|---|"]
              + [f"| {f['seller_name']} | {n} | {f['quarter']} | {f['rows']:,} | {f['reported_mwh']:,.0f} | "
                 f"{f['counted_mwh']:,.0f} |" for f, n in fixes[:15]] if fixes else ["None."]) + [""]
    bills: dict[tuple[str, str], list] = {}
    for e in ents:
        for f in e["placeholder_bills"]:
            agg = bills.setdefault((f["seller_name"], e["entity_id"]), [set(), 0, 0.0])
            agg[0].add(f["quarter"])
            agg[1] += f["rows"]
            agg[2] += f["billed_usd"]
    by_id = {e["entity_id"]: e for e in ents}
    billing = {e["entity_id"]: sum(f["billed_usd"] for f in e["placeholder_bills"])
               + sum((e.get("energy_usd_by_quarter") or {}).values()) for e in ents}
    bill_rows = sorted(bills.items(), key=lambda kv: -kv[1][2])
    lines += [f"### Bills with a placeholder quantity ({_top15(len(bill_rows))}, by dollars)", "",
              ("Energy reported as a few MWh at a price no market reaches: the volume of these relationships is "
               "understated. Share: of everything EQR sellers billed the company for energy in these quarters."), ""]
    lines += (["| Seller | Company | Quarters | Rows | Billed | Share |", "|---|---|---|---|---|---|"]
              + [f"| {sn} | {by_id[e]['name']} | {', '.join(sorted(v[0]))} | {v[1]:,} | {_usd(v[2])} | "
                 f"{format(v[2] / billing[e], '.0%') if billing[e] > 0 else 'n/a'} |"
                 for (sn, e), v in bill_rows[:15]] if bill_rows else ["None."]) + [""]
    jumps = []
    for x in rels:
        vals = [v for v in x["mwh_by_quarter"].values() if v > 0]
        if len(vals) >= 2 and max(vals) >= JUMP_MIN_MWH and max(vals) / min(vals) >= JUMP_RATIO:
            jumps.append((max(vals) / min(vals), min(vals), max(vals), x))
    jumps.sort(key=lambda j: -j[0])
    lines += [f"### Thousandfold jumps within one seller relationship ({len(jumps)})", "",
              "A seller that scales both the MWh and the dollars passes the price test. Check these by hand.", ""]
    lines += (["| Seller | Company | Smallest quarter | Largest quarter | Ratio |", "|---|---|---|---|---|"]
              + [f"| {x['seller_name']} | {names.get(x['entity_id'], x['entity_id'])} | {lo:,.0f} | {hi:,.0f} | "
                 f"{ratio:,.0f}x |" for ratio, lo, hi, x in jumps[:15]] if jumps else ["None."]) + [""]
    big = sorted(((q, mwh, r) for r in rels for q, mwh in r["mwh_by_quarter"].items()), key=lambda x: -x[1])[:10]
    lines += ["### Largest seller-company volumes in one quarter (top 10)", "",
              "Check these each refresh: a units error the price test can't see shows up here first.", "",
              "| Seller | Company | Quarter | Energy MWh |", "|---|---|---|---|"]
    lines += [f"| {r['seller_name']} | {names.get(r['entity_id'], r['entity_id'])} | {q} | {mwh:,.0f} |"
              for q, mwh, r in big] + [""]
    return "\n".join(lines)


# --- lookups -----------------------------------------------------------------
def _num(v, default=0.0) -> float:
    return float(v) if v not in (None, "") else default


def _opt(v) -> float | None:
    return float(v) if v not in (None, "") else None


def _json(v, default):
    return json.loads(v) if v not in (None, "") else default


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _mwh(x: float) -> str:
    return f"{x:,.0f} MWh"


def _all(n: int) -> str:
    return "both" if n == 2 else f"all {n}"


class SignalsIndex:
    """Per-company market signals from a build, looked up by name through resolve_entity."""

    def __init__(self, resolver: EntityResolver, companies: list[dict], relationships: list[dict], manifest: dict,
                 coverage: list[dict] | None = None):
        built_with = manifest.get("methodology_version")
        if built_with != METHODOLOGY_VERSION:
            raise DataError(f"The market signals build was made with {built_with or 'an unknown version'}; this code "
                            f"is {METHODOLOGY_VERSION}. Rebuild: python -m creditmcp.signals build")
        self.resolver, self.manifest = resolver, manifest
        self._companies = {r["entity_id"]: r for r in companies}
        self._rels: dict[str, list[dict]] = {}
        for r in relationships:
            self._rels.setdefault(r["entity_id"], []).append(r)
        inp = manifest.get("inputs", {})
        taken = inp.get("snapshot_taken") or []
        self.provenance = Provenance(
            contract_window="–".join(inp.get("contract_window") or ["?", "?"]),
            snapshots=inp.get("snapshots", {}), snapshot_taken=taken[0] if taken else "unknown",
            pudl_release=inp.get("pudl_release") or "unknown",
            inputs={**(inp.get("resolver_inputs") or {}), **manifest.get("outputs", {})},
            decisions_sha256=inp.get("decisions_sha256"))
        self.as_of = taken[-1] if taken else "unknown"
        kr = (manifest.get("rules") or {}).get("quantity_check") or {}
        self.kwh_price = kr.get("max_price", KWH_RULE["KWH_MAX_PRICE"])
        ec = manifest.get("eia_coverage") or {}
        self._cover_on = bool(ec.get("available"))
        self._cover_qs = ec.get("quarters") or []
        self._coverage = {r["entity_id"]: r for r in coverage or []}
        self.caveats = CAVEATS + [QUANTITY_CAVEAT.format(price=self.kwh_price) if kr.get("applied")
                                  else NO_QUANTITY_CAVEAT, COVERAGE_CAVEAT if self._cover_on else NO_COVERAGE_CAVEAT]
        self.latest_q = manifest.get("latest_quarter", "")
        self.last_year_q = manifest.get("same_quarter_last_year", "")
        b = manifest.get("baseline", {})
        self.market_median = b.get("median_volume_change")
        rel = b.get("relationships", {})
        self.market = (
            (f"Across the {b.get('companies_with_volume_last_year', 0):,} roster companies that bought energy from EQR "
             f"sellers in {self.last_year_q}, the median change in {self.latest_q} was {self.market_median:+.0%}: "
             f"{b.get('yoy_up', 0):,} bought at least 25% more, {b.get('yoy_down', 0):,} at least 25% less and "
             f"{b.get('yoy_steady', 0):,} about the same. " if self.market_median is not None else "")
            + f"{b.get('volume_trend', {}).get('started', 0):,} started buying after none in the four prior quarters. "
            f"Of {b.get('regular_relationships', 0):,} regular seller relationships, {rel.get('kept_trading', 0):,} "
            f"kept trading and {rel.get('stopped', 0):,} stopped while still under contract; {rel.get('new', 0):,} new "
            f"relationships started.")

    @classmethod
    def from_dir(cls, home: str | Path, *, run: str = DEFAULT_RUN, resolver: EntityResolver | None = None):
        home = Path(home).expanduser()
        d = home / run / OUT_DIR
        paths = [d / "signals_companies.csv", d / "signals_relationships.csv", d / "signals_manifest.json",
                 d / "signals_coverage.csv"]
        for p in paths:
            if not p.exists():
                raise DataError(f"Missing {p}. Run: python -m creditmcp.signals build")
        read = lambda p: list(csv.DictReader(open(p, newline="", encoding="utf-8")))  # noqa: E731
        return cls(resolver or EntityResolver.from_dir(home), read(paths[0]), read(paths[1]),
                   json.loads(paths[2].read_text(encoding="utf-8")), coverage=read(paths[3]))

    def coverage(self, entity_id: str) -> Coverage:
        """How much of the company's buying EQR shows, against the purchases it reported to EIA."""
        r = self._coverage.get(entity_id) or {}
        share = _opt(r.get("share"))
        year = int(float(r["eia_year"])) if r.get("eia_year") not in (None, "") else None
        uid = int(float(r["eia_utility_id"])) if r.get("eia_utility_id") not in (None, "") else None
        cov = Coverage(level=_cover_level(share), share=share, eqr_mwh=_num(r.get("eqr_mwh")),
                       eqr_quarters=list(self._cover_qs), eia_year=year,
                       eia_purchases_mwh=_opt(r.get("eia_purchases_mwh")),
                       eia_net_generation_mwh=_opt(r.get("eia_net_generation_mwh")),
                       eia_retail_sales_mwh=_opt(r.get("eia_retail_sales_mwh")),
                       eia_sales_for_resale_mwh=_opt(r.get("eia_sales_for_resale_mwh")), eia_utility_id=uid, note="")
        why = (r.get("why_unknown") or "not_linked") if self._cover_on else "no_data"
        cov.note = _coverage_sentence(cov, why)
        return cov

    def lookup(self, name: str) -> MarketSignalsResult:
        res = self.resolver.resolve(name, limit=3)
        top = res.candidates[0] if res.candidates else None
        base = dict(name=name or "", latest_quarter=self.latest_q, same_quarter_last_year=self.last_year_q,
                    prior_quarters=self.manifest.get("prior_quarters", []), methodology_version=METHODOLOGY_VERSION,
                    as_of=self.as_of, provenance=self.provenance, caveats=self.caveats, market=self.market)
        if top is None or top.confidence != Confidence.MATCH or res.ambiguous:
            why = ("it matches two roster companies equally well" if res.ambiguous else
                   "no roster company matches it confidently" if top else "no PJM or MISO roster company matches it")
            return MarketSignalsResult(**base, status=ResultStatus.UNRESOLVED,
                                       company=top if top and not res.ambiguous else None,
                                       summary=f"No market signals: {why}. Resolve the name first with "
                                               "counterparty.resolve_entity.")
        c = self._companies.get(top.entity_id)
        cov = self.coverage(top.entity_id)
        if not c:
            window = f"{(self.manifest.get('prior_quarters') or ['?'])[0]} to {self.latest_q}"
            known = cov.level == CoverageLevel.LOW and cov.eia_purchases_mwh
            return MarketSignalsResult(
                **base, status=ResultStatus.NO_ACTIVITY, company=top, coverage=cov,
                in_short=f"EQR shows none of its power purchases ({_mwh(cov.eia_purchases_mwh)} in {cov.eia_year})."
                if known else None,
                summary=f"No EQR seller reported power contracts or trades with {top.name} from {window}. "
                        + (cov.note if known else "It may buy from the ISO markets or from sellers that don't file "
                           "EQR, or appear under a spelling CreditMCP doesn't know."))
        rels = [self._relationship(r) for r in self._rels.get(top.entity_id, [])]
        rels.sort(key=lambda r: (0 if r.status in TRADING else 1 if r.status in LOST else 2,
                                 -r.mwh_latest, -r.mwh_same_quarter_last_year, r.seller_name))
        latest, last_year = _num(c["mwh_latest"]), _num(c["mwh_same_quarter_last_year"])
        prior_avg = _num(c["mwh_prior_avg"])
        change, basis = _volume(latest, last_year, prior_avg)
        volume = VolumeSignal(mwh_by_quarter=_json(c.get("mwh_by_quarter"), {}), mwh_latest=latest,
                              mwh_same_quarter_last_year=last_year, mwh_prior_avg=prior_avg, change=change,
                              basis=basis, trend=_volume_trend(latest, change),
                              market_median_change=self.market_median)
        now, before = int(c["sellers_latest"]), int(c["sellers_last_year"])
        sellers = SellerSignal(
            trading_latest=now, trading_same_quarter_last_year=before, trend=_seller_trend(now, before),
            new=int(c["new"]), occasional=int(c["occasional"]), carried_over=int(c["carried_over"]),
            regular=int(c["regular"]), kept_trading=int(c["kept_trading"]), stopped=int(c["stopped"]),
            stopped_not_counted=int(c["stopped_not_counted"]), contract_ended=int(c["contract_ended"]),
            other_products_latest=int(c["other_products_latest"]), top_seller=c.get("top_seller") or None,
            top_seller_share=_opt(c.get("top_seller_share")),
            top_seller_share_last_year=_opt(c.get("top_seller_share_last_year")))
        ruc = int(c["regular_under_contract"])
        warning = EarlyWarning(met=_warning(ruc, sellers.stopped, change), regular_under_contract=ruc,
                               stopped=sellers.stopped, stopped_share=round(sellers.stopped / ruc, 3) if ruc else None,
                               volume_change=change, rule=WARN_RULE)
        reasons = Counter(_json(c.get("not_counted_reasons"), {}))
        linked = sorted({n for r in rels for n in r.other_buyer_names})
        fixes = [QuantityFix(**f) for f in _json(c.get("kwh_fixes"), [])]
        bills = self._placeholder_bills(_json(c.get("placeholder_bills"), []))
        bill_share = self._placeholder_share(bills, _json(c.get("energy_usd_by_quarter"), {}))
        return MarketSignalsResult(
            **base, status=ResultStatus.OK, company=top, volume=volume, sellers=sellers, early_warning=warning,
            relationships=rels, quantity_fixes=fixes, coverage=cov, placeholder_bills=bills,
            placeholder_billing_share=bill_share,
            in_short=_in_short(volume, sellers, warning, self.latest_q, cov),
            summary=_summary(top.name, self.latest_q, self.last_year_q, volume, sellers, warning, reasons, linked,
                             fixes, self.kwh_price, cov if self._cover_on else None, bills, bill_share))

    def _placeholder_bills(self, rows: list[dict]) -> list[PlaceholderBill]:
        """Bills with a placeholder quantity over the last four quarters, one entry per seller, largest first."""
        qs = set(self._cover_qs) or {r["quarter"] for r in rows}
        by: dict[str, list] = {}
        for r in rows:
            if r["quarter"] in qs:
                agg = by.setdefault(r["seller_name"], [[], 0, 0.0])
                agg[0].append(r["quarter"])
                agg[1] += int(r["rows"])
                agg[2] += float(r["billed_usd"])
        return [PlaceholderBill(seller_name=sn, quarters=sorted(set(v[0])), rows=v[1], billed_usd=round(v[2], 2))
                for sn, v in sorted(by.items(), key=lambda kv: -kv[1][2])]

    def _placeholder_share(self, bills: list[PlaceholderBill], billed: dict) -> float | None:
        """The placeholder bills' share of everything EQR sellers billed the company for energy over the same four
        quarters. None without bills, or when the build has no billing to compare with."""
        if not bills or not billed:
            return None
        qs = self._cover_qs or list(billed)
        total = sum(b.billed_usd for b in bills)
        usual = sum(float(billed.get(q) or 0) for q in qs)
        return round(total / (total + usual), 4) if usual >= 0 and total + usual > 0 else None

    @staticmethod
    def _relationship(r: dict) -> SellerRelationship:
        split = lambda v: [q for q in (v or "").split(";") if q]  # noqa: E731
        return SellerRelationship(
            seller_cid=r["seller_cid"], seller_name=r["seller_name"], status=SellerStatus(r["status"]),
            reason=r.get("reason") or None, regular=r["regular"] == "True",
            under_contract_latest=r["under_contract_latest"] == "True", quarters_traded=split(r["quarters_traded"]),
            quarters_other_products=split(r.get("quarters_other_products")),
            other_buyer_names=_json(r.get("other_buyer_names"), []), trades_latest=int(r["trades_latest"]),
            mwh_by_quarter=_json(r.get("mwh_by_quarter"), {}), mwh_latest=_num(r["mwh_latest"]),
            mwh_same_quarter_last_year=_num(r.get("mwh_same_quarter_last_year")),
            share_latest=_opt(r.get("share_latest")))


# --- plain-language reads -----------------------------------------------------
def _share_words(share: float) -> str:
    return "under 1%" if share < 0.005 else f"about {share:.0%}"


def _in_short(v: VolumeSignal, s: SellerSignal, w: EarlyWarning, q: str, cov: Coverage | None = None) -> str:
    """One line. Low coverage leads: a company whose buying EQR mostly can't see gets its signals read as a slice."""
    text = _in_short_signals(v, s, q) + (" Meets the early-warning bar." if w.met else "")
    if cov is None or cov.share is None:
        return text
    if cov.level == CoverageLevel.LOW:
        if cov.eqr_mwh <= 0:
            return "EQR shows none of its power purchases in the last four quarters." + (
                " Meets the early-warning bar." if w.met else "")
        lead = f"EQR shows only {_share_words(cov.share)} of its power purchases."
        return f"{lead} In that slice: {text[0].lower()}{text[1:]}"
    if cov.level == CoverageLevel.PARTIAL:
        return f"{text} EQR shows about {cov.share:.0%} of its power purchases."
    return text


def _in_short_signals(v: VolumeSignal, s: SellerSignal, q: str) -> str:
    if v.trend == VolumeTrend.NONE:
        text = "No energy bought from EQR sellers in the last five quarters."
    elif v.trend == VolumeTrend.STARTED:
        text = f"Started buying in {q}, from {s.trading_latest} seller{'' if s.trading_latest == 1 else 's'}."
    elif v.mwh_latest <= 0:
        text = f"Bought no energy from EQR sellers in {q}."
    else:
        verb = {VolumeTrend.UP: "Buying more", VolumeTrend.DOWN: "Buying less"}.get(v.trend, "Buying about the same")
        if v.trend == VolumeTrend.STEADY and v.change is not None and abs(v.change) >= SLIGHT_BAND:
            verb = "Buying a little more" if v.change > 0 else "Buying a little less"
        lost = s.stopped + s.contract_ended
        tail = ("from more sellers" if s.trend == SellerTrend.BROADER else
                "from fewer sellers" if s.trend == SellerTrend.NARROWER else
                "with a change of sellers" if s.new >= TURNOVER_MIN and lost >= TURNOVER_MIN else
                "from the same seller" if s.trading_latest == s.trading_same_quarter_last_year == s.kept_trading == 1
                else "from much the same sellers")
        text = f"{verb}, {tail}."
    return text


def _volume_sentence(name: str, q: str, ly: str, v: VolumeSignal, other_products: int) -> str:
    if v.trend == VolumeTrend.NONE:
        text = f"{name} bought no energy from EQR sellers in {q} or the four quarters before it."
        if other_products:
            text += (f" {other_products} seller{' traded' if other_products == 1 else 's traded'} capacity or other "
                     f"products with it in {q}.")
        return text
    if v.trend == VolumeTrend.STARTED:
        return (f"{name} bought {_mwh(v.mwh_latest)} of energy from EQR sellers in {q}, after none in the four "
                "quarters before it.")
    yoy = v.basis == BASIS_YOY
    median = v.market_median_change if yoy else None
    if v.mwh_latest <= 0:
        against = f"{_mwh(v.mwh_same_quarter_last_year)} in {ly}" if yoy else \
            f"an average of {_mwh(v.mwh_prior_avg)} a quarter before"
        return f"{name} bought no energy from EQR sellers in {q}, against {against}."
    head = f"{name} bought {_mwh(v.mwh_latest)} of energy from EQR sellers in {q}"
    market = "" if median is None else f" (market median: {median:+.0%})"
    if v.trend == VolumeTrend.STEADY:
        then = "a year earlier" if yoy else "its prior-quarter average"
        if abs(v.change) < 0.005:
            return f"{head}, unchanged from {then}{market}."
        if abs(v.change) >= SLIGHT_BAND:
            word = "up" if v.change > 0 else "down"
            return (f"{head}, {word} {abs(v.change):.0%} on {BASIS_YOY if yoy else 'its prior-quarter average'}"
                    f"{market}, inside the {VOLUME_BAND:.0%} band CreditMCP counts as steady.")
        detail = f"{v.change:+.0%}" + ("" if median is None else f"; market median {median:+.0%}")
        return f"{head}, about the same as {then} ({detail})."
    word = "up" if v.trend == VolumeTrend.UP else "down"
    return f"{head}, {word} {abs(v.change):.0%} on {BASIS_YOY if yoy else 'its prior-quarter average'}{market}."


def _sellers_sentence(q: str, ly: str, s: SellerSignal) -> str:
    if s.trend == SellerTrend.NONE:
        return ""
    n, m = s.trading_latest, s.trading_same_quarter_last_year
    if n == 0:
        return f"No seller traded energy with it in {q} ({m} in {ly})."
    text = f"It traded energy with {n} seller{'' if n == 1 else 's'} in {q} ({m or 'none'} in {ly})"
    if s.new:
        text += (", a new one" if n == 1 else ", all of them new" if s.new == n else f", {s.new} of them new")
    text += "."
    if s.carried_over:
        c = s.carried_over
        who = "That seller" if n == 1 else f"{c} of them"
        text += (f" {who} had already traded with an affiliate in its PJM group, so "
                 f"{'isn' if c == 1 else 'aren'}'t counted as new.")
    return text


def _regular_sentence(s: SellerSignal, reasons: Counter) -> str:
    if not s.regular:
        return ""
    parts = [(k, n) for k, n in [("kept", s.kept_trading), ("stopped", s.stopped)]
             + [(reason, reasons.get(reason, 0)) for reason in STOP_REASONS] + [("ended", s.contract_ended)] if n]
    if s.regular == 1 and parts:
        return f"Its one regular seller {CLAUSES[parts[0][0]][0]}."
    if len(parts) == 1:
        return f"{_all(s.regular).capitalize()} of its regular sellers {CLAUSES[parts[0][0]][1]}."
    items = [f"{n} {CLAUSES[k][0] if n == 1 else CLAUSES[k][1]}" for k, n in parts]
    return f"Of its {s.regular} regular sellers, {_join(items)}." if items else ""


def _concentration_sentence(s: SellerSignal) -> str:
    if not s.top_seller or s.top_seller_share is None:
        return ""
    if s.top_seller_share >= 0.995:
        return f"All of its energy came from {s.top_seller.rstrip('.')}."
    text = f"Its largest seller, {s.top_seller}, supplied {s.top_seller_share:.0%} of its energy"
    if s.top_seller_share_last_year is not None:
        text += f" ({s.top_seller_share_last_year:.0%} for the largest a year earlier)"
    return text + "."


def _warning_sentence(s: SellerSignal, w: EarlyWarning) -> str:
    n, total = w.stopped, w.regular_under_contract
    who = (f"{_all(n)} of its regular sellers under contract" if n == total else
           f"{n} of its {total} regular sellers under contract")
    if w.met:
        return (f"It meets the early-warning bar: {who} stopped trading with it, and its energy volume fell by half or "
                "more. Check for seasonality or matured hedges before reading it as credit stress.")
    if _seller_test(w.regular_under_contract, w.stopped):
        return (f"{who[0].upper() + who[1:]} stopped trading with it, enough for the early-warning bar, but its energy "
                "volume didn't fall by half, so the bar isn't met.")
    return ""


def _quantity_sentence(fixes: list[QuantityFix], price: float) -> str:
    """Name the reports counted at a thousandth because they only make sense as kWh."""
    if not fixes:
        return ""
    by: dict[str, list[QuantityFix]] = {}
    for f in fixes:
        by.setdefault(f.seller_name, []).append(f)
    why = (f"priced under ${price:.2f} a MWh, too little for that much energy (usually kWh entered as MWh, or an "
           "adjustment to energy billed in another row)")
    if len(by) == 1:
        seller, fs = next(iter(by.items()))
        quarters = _join([f.quarter for f in fs])
        reported, counted = sum(f.reported_mwh for f in fs), sum(f.counted_mwh for f in fs)
        one = len(fs) == 1
        return (f"{seller.rstrip('.')}'s report{'' if one else 's'} for {quarters} {'was' if one else 'were'} {why}, "
                f"so {'it counts' if one else 'they count'} here at a thousandth: {_mwh(reported)} as reported, "
                f"{counted:,.0f} counted.")
    items = [f"{seller} ({_join([f.quarter for f in fs])})" for seller, fs in list(by.items())[:3]]
    more = f" and {len(by) - 3} more" if len(by) > 3 else ""
    return (f"Reports from {len(by)} sellers were {why}, so they count here at a thousandth of the reported MWh: "
            f"{', '.join(items)}{more}.")


def _placeholder_sentence(bills: list[PlaceholderBill], span: str, coverage_known: bool,
                          share: float | None = None) -> str:
    """Name the sellers that billed the company with a placeholder quantity, and size their bills against what EQR
    sellers billed it for energy: a large share means its volumes read low, a small one hardly matters."""
    if not bills:
        return ""
    total = sum(b.billed_usd for b in bills)
    sized = "" if share is None else f", {_share_words(share)} of what EQR sellers billed it for energy over that time"
    if len(bills) == 1:
        head = (f"{bills[0].seller_name.rstrip('.')} reported its sales to it over {span} as bills with a placeholder "
                f"quantity of a few MWh ({_usd(total)} billed{sized})")
    else:
        names = [b.seller_name for b in bills[:3]] + ([f"{len(bills) - 3} more"] if len(bills) > 3 else [])
        head = (f"{len(bills)} sellers reported sales to it over {span} as bills with a placeholder quantity of a few "
                f"MWh ({_join(names)}: {_usd(total)} billed in all{sized})")
    if share is not None and share < PLACEHOLDER_SMALL:
        return f"{head}. That energy isn't counted, but at that size it hardly changes its volumes."
    what = "its volumes and coverage" if coverage_known else "its volumes"
    return f"{head}, so that energy isn't counted and {what} here understate its buying."


def _coverage_sentence(cov: Coverage, why: str = "") -> str:
    """How much of the company's buying EQR shows, in words."""
    span = f"the four quarters to {cov.eqr_quarters[-1]}" if cov.eqr_quarters else "the last four quarters"
    if cov.share is None:
        if why == "not_comparable":
            return (f"How much of its buying EQR shows isn't given: EQR shows {_mwh(cov.eqr_mwh)} of energy bought "
                    f"over {span}, far more than the {_mwh(cov.eia_purchases_mwh)} it reported buying for "
                    f"{cov.eia_year} (EIA-861), so the two don't measure the same thing for this company.")
        if why == "no_data":
            return "How much of its buying EQR shows is unknown: this build has no EIA-861 purchases to compare with."
        if why == "too_old":
            return (f"How much of its buying EQR shows is unknown: its latest EIA-861 purchases are for "
                    f"{cov.eia_year}, too old to compare with {span}.")
        if why == "no_purchases":
            return (f"It reported no wholesale purchases to EIA for {cov.eia_year}, so there is nothing to measure "
                    "EQR's view of its buying against.")
        return ("How much of its buying EQR shows is unknown: CreditMCP hasn't tied it to an EIA-861 utility, so "
                "there are no reported purchases to compare with.")
    bought = f"{_mwh(cov.eia_purchases_mwh)} it reported buying from all sources for {cov.eia_year} (EIA-861)"
    gen = (f", and it generated {_mwh(cov.eia_net_generation_mwh)} of its own"
           if cov.eia_net_generation_mwh and cov.eia_net_generation_mwh > 0 else "")
    where = ("from the ISO market, from sellers that don't file EQR (many co-ops, public power and federal power "
             "agencies) or under spellings CreditMCP can't tie to it")
    if cov.eqr_mwh <= 0:
        return f"EQR shows none of the {bought}{gen}: it buys {where}."
    if cov.level == CoverageLevel.LOW:
        return (f"Over {span}, EQR shows only {_mwh(cov.eqr_mwh)} of energy bought, {_share_words(cov.share)} of the "
                f"{bought}{gen}. The rest is out of view: bought {where}. Read these signals as a slice of its buying.")
    head = f"Over {span}, EQR shows {_mwh(cov.eqr_mwh)} of energy bought"
    if cov.share > 1.05:
        return (f"{head}, more than the {bought}: the periods differ, but these signals see essentially all of its "
                "buying.")
    most = "most of its buying" if cov.level == CoverageLevel.HIGH else "part of its buying"
    return f"{head}, about {cov.share:.0%} of the {bought}, so these signals see {most}."


def _summary(name: str, q: str, ly: str, v: VolumeSignal, s: SellerSignal, w: EarlyWarning, reasons: Counter,
             linked: list[str], fixes: list[QuantityFix] | None = None,
             price: float = KWH_RULE["KWH_MAX_PRICE"], cov: Coverage | None = None,
             bills: list[PlaceholderBill] | None = None, bill_share: float | None = None) -> str:
    known = cov is not None and cov.share is not None
    span = (f"the four quarters to {cov.eqr_quarters[-1]}" if cov is not None and cov.eqr_quarters
            else "the last four quarters")
    parts = [_volume_sentence(name, q, ly, v, s.other_products_latest), cov.note if known else "",
             _quantity_sentence(fixes or [], price), _placeholder_sentence(bills or [], span, known, bill_share),
             _sellers_sentence(q, ly, s), _regular_sentence(s, reasons), _concentration_sentence(s),
             _warning_sentence(s, w)]
    if linked:
        shown = ", ".join(f'"{n}"' for n in linked[:3]) + (f" and {len(linked) - 3} more" if len(linked) > 3 else "")
        parts.append(f"Trades reported under another buyer name on the same agreement count toward {name} ({shown}).")
    if cov is not None and not known:
        parts.append(cov.note)
    return " ".join(p for p in parts if p)


# --- command line ------------------------------------------------------------
def _home(arg: str | None) -> Path:
    return Path(arg or os.environ.get("CREDITMCP_HOME") or Path(__file__).resolve().parents[2]).expanduser()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Build or look up market signals.")
    p.add_argument("args", nargs="+", help='"build", or one or more company names')
    p.add_argument("--home", help="Repo folder (default: CREDITMCP_HOME or this repo)")
    p.add_argument("--run", default=DEFAULT_RUN, help="Reality-check run folder with five quarters of trades")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    home = _home(a.home)
    if a.args == ["build"]:
        run = home / a.run
        mf = run / "manifest.json"
        man = json.loads(mf.read_text(encoding="utf-8")) if mf.exists() else {}
        m = build(run / "reality_check.duckdb", run / OUT_DIR, EntityResolver.from_dir(home), man)
        print((run / OUT_DIR / "signals_report.md").read_text(encoding="utf-8"))
        print(f"Wrote {run / OUT_DIR} ({m['counts']['relationships']:,} relationships, "
              f"{m['counts']['companies']:,} companies).")
        return 0
    idx = SignalsIndex.from_dir(home, run=a.run)
    for name in a.args:
        r = idx.lookup(name)
        if a.json:
            print(r.model_dump_json(indent=2))
            continue
        print(f"\n{name}  ->  {r.company.name if r.company else '-'}  [{r.in_short or r.status.value}]")
        if r.coverage:
            share = "" if r.coverage.share is None else f" ({r.coverage.share:.1%} of reported purchases)"
            print(f"  coverage: {r.coverage.level.value}{share}")
        for rel in r.relationships[:15]:
            note = f"  ({rel.reason})" if rel.reason else ""
            print(f"  {rel.status.value:<20} {rel.seller_name[:40]:<40} {rel.mwh_latest:>12,.0f} MWh  "
                  f"energy: {', '.join(rel.quarters_traded) or 'none'}{note}")
        print(f"  {r.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
