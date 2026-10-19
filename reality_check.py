#!/usr/bin/env python3
"""
reality_check.py - CreditMCP v0.2 scope-freeze reality check against PUDL's FERC EQR.

Answers four questions before v0.2 scope is frozen:
  Q1 Resolution coverage  Do roster entities resolve to EQR counterparties, and how much
                          in-scope PJM/MISO contract activity does that cover?
  Q2 Date quality         Are contract dates intact enough for early-termination math?
  Q3 Where tenor lives    Do contract dates carry exposure tenor, or must tenor come from
                          trade dates in the transactions table?
  Q4 Signal sizing        Early terminations net of re-papers, contracts that vanish without
                          a reported termination, and whether the contract<->transaction
                          join that supplier dormancy needs actually works.

Data: PUDL's FERC EQR Parquet, read in place from s3://pudl.catalyst.coop/ferceqr/ with
DuckDB (anonymous, path-style S3 because the bucket name contains dots).

Setup
  pip install "duckdb>=1.1" pandas openpyxl
  1) Bars live in BARS below (set Oct 10, 2026, before the first run); flags can override them.
  2) python reality_check.py --roster pjm_members.csv --out rc
     Add more rosters with more --roster flags (an XLSX takes --sheets); a roster's ISO comes
     from its tab or file name.
  3) Put y or n in decision_y_n in rc/review_queue.csv (add rows for matches it missed), then rerun:
     python reality_check.py ...same flags... --decisions rc/review_queue.csv
     Reruns into the same --out reuse that run's data snapshot; --refresh pulls fresh data.
     rc/alias_seed.csv is the seed alias table for resolve_entity.

Changes after the first real run (Oct 10, 2026):
  - Roster resolution counts a company only if it is the buyer in an EQR power sale.
    Interconnection, transmission and membership agreements name companies that may never buy.
  - Fuzzy matches must carry the same numbers ("Solar 1" never matches "Solar 2"), and the
    review queue drops pairs whose EQR name already has an exact match or never buys power.
  - Terminations count only inside the window; a one-seller cluster in any year is flagged.
  - Delivery scope is read from the balancing authority first, the location string second.

The transactions step reads ~13 columns of one quarter from S3 and is the slow part.
Skip it with --skip-transactions; the contract-side report is written first regardless.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import platform
import re
import sys
import time
from pathlib import Path

try:
    import duckdb
    import pandas as pd
except ImportError as exc:  # pragma: no cover
    sys.exit(f'Missing dependency ({exc.name}). Run: pip install "duckdb>=1.1" pandas openpyxl')

# =============================================================================
# 1. SET YOUR BARS BEFORE YOU LOOK
#    The script refuses to run while any bar is None. --no-bars overrides that,
#    and the report then records that the bars were not set in advance.
# =============================================================================
BARS: dict[str, float | None] = {
    # Set Oct 10, 2026, before the first run.
    # Share of roster entities found at HIGH confidence as the buyer in at least one EQR
    # power sale (no affiliate sales, no sales to an RTO/ISO). Interconnection, transmission
    # and membership agreements don't count: they name companies that may never buy power.
    "roster_resolution": 0.30,
    # Share of in-scope contracts (PJM/MISO delivery, power sales, no affiliates, no RTO/ISO
    # counterparty) whose customer resolves to a roster entity; with transactions, also the
    # share of traded energy (MWh). Gated only for ISOs your roster covers.
    "volume_coverage": 0.60,
    # Share of roster-linked contracts whose dates are intact: no impossible or
    # contradictory commencement / termination / actual-termination dates.
    "usable_dates": 0.80,
}

# =============================================================================
# 2. TUNABLES (edit freely; every value is recorded in manifest.json)
# =============================================================================
EQR_ROOT_S3 = "s3://pudl.catalyst.coop/ferceqr"
PUDL_ROOT_S3 = "s3://pudl.catalyst.coop"

# Geography comes from delivery BA + specific location. Order matters: PJM is tested first
# so "NORTHERN ILLINOIS HUB" lands in PJM before MISO's "ILLINOIS HUB" pattern catches it.
# The report lists the most common BA and location strings so you can tighten these.
SCOPE_PATTERNS = {
    "PJM": r"\bPJM\b|AEP[ -]?DAYTON|\bAD HUB\b|\bNI HUB\b|N(ORTHERN)? ILLINOIS HUB|\bWEST(ERN)? HUB\b"
           r"|\bEAST(ERN)? HUB\b|DOMINION HUB|NEW JERSEY HUB|\bOHIO HUB\b|CHICAGO HUB",
    "MISO": r"\bMISO\b|MIDCONTINENT|MIDWEST ISO|INDIANA HUB|\bCINERGY\b|ILLINOIS HUB|MICHIGAN HUB"
            r"|MINNESOTA HUB|ARKANSAS HUB|LOUISIANA HUB|TEXAS HUB|MISSISSIPPI HUB",
    "NYISO": r"\bNYIS\b|\bNYISO\b|\bNY ?ISO\b",
}
V02_SCOPES = ("PJM", "MISO")

# Sales to the market operator are not bilateral credit exposure; excluded from coverage.
MARKET_OPERATOR_RE = (
    r"^(PJM INTERCONNECTION|PJM SETTLEMENT|MIDCONTINENT INDEPENDENT|MIDWEST INDEPENDENT"
    r"|NEW YORK INDEPENDENT SYSTEM|ISO NEW ENGLAND|CALIFORNIA INDEPENDENT SYSTEM"
    r"|SOUTHWEST POWER POOL|ELECTRIC RELIABILITY COUNCIL)"
    r"|^(PJM|MISO|NYISO|ISO NE|ISONE|CAISO|SPP|ERCOT)( |$)"
)
# Extension language that keeps a contract alive past its stated termination date.
EVERGREEN_RE = (
    r"EVERGREEN|UNTIL (TERMINAT|CANCEL)|MONTH[ -]TO[ -]MONTH|YEAR[ -]TO[ -]YEAR|AUTOMATIC"
    r"|AUTO[ -]?RENEW|\bRENEW(S|AL|ED|ING)?\b|CONTINU|INDEFINITE|ROLL(S|ING)? OVER"
)
NO_EXTENSION_RE = r"^\s*(NONE|N/?A|NO EXTENSION|NOT APPLICABLE)?\s*$"

POWER_PRODUCT_TYPES = ("MB", "CB", "NPU")
NON_POWER_PRODUCTS = (
    "NETWORK", "POINT-TO-POINT AGREEMENT", "INTERCONNECTION AGREEMENT", "MEMBERSHIP AGREEMENT",
    "TRANSMISSION OWNERS AGREEMENT", "SYSTEM OPERATING AGREEMENTS", "NETWORK OPERATING AGREEMENT",
    "REASSIGNMENT AGREEMENT", "NEGOTIATED-RATE TRANSMISSION", "DIRECT ASSIGNMENT FACILITIES CHARGE",
    "REAL POWER TRANSMISSION LOSS", "CUSTOMER CHARGE", "UPLIFT",
)
TX_PRODUCTS = ("ENERGY", "CAPACITY", "BOOKED OUT POWER", "REQUIREMENTS SERVICE")
# Some sellers enter kWh in EQR's MWh field, and some bill an adjustment as a row that repeats a month's MWh. An energy
# row whose total charge works out under KWH_MAX_PRICE dollars per MWh, on at least KWH_MIN_QTY MWh as reported, is
# too cheap for that much energy (usually one or the other): it counts at a thousandth.
KWH_MAX_PRICE = 0.5
KWH_MIN_QTY = 1000.0

FUZZY_HIGH = 0.97        # Jaro-Winkler at/above this counts as a high-confidence match
FUZZY_REVIEW = 0.90      # between REVIEW and HIGH lands in review_queue.csv for hand checks
FUZZY_MIN_LEN = 6        # never fuzzy-match very short names
EARLY_TOLERANCE_DAYS = 30
REPAPER_DAYS_BEFORE, REPAPER_DAYS_AFTER = 30, 120
OPEN_ENDED_YEAR = 2090   # termination dates at/after this year are "no end" sentinels (e.g. 9999)
MIN_PLAUSIBLE_YEAR = 1980
MIN_DATED_TRADES = 20    # dated trades an entity needs in a quarter for a per-entity tenor read
SPARSE_EVENTS = 25       # fewer net termination events than this -> treat as an event flag

# =============================================================================
# 3. NAME NORMALIZATION (built to be lifted into resolve_entity later)
# =============================================================================
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


# =============================================================================
# 4. SMALL HELPERS
# =============================================================================
QRE = re.compile(r"(\d{4})q([1-4])", re.I)
QK_SQL = "(CAST(substr({c}, 1, 4) AS INTEGER) * 4 + CAST(substr({c}, 6, 1) AS INTEGER) - 1)"

CONTRACT_COLS = {
    "year_quarter": "VARCHAR",
    "seller_company_id_ferc": "VARCHAR",
    "contract_unique_id": "VARCHAR",
    "seller_company_name": "VARCHAR",
    "customer_company_name": "VARCHAR",
    "contract_affiliate": "BOOLEAN",
    "ferc_tariff_reference": "VARCHAR",
    "contract_service_agreement_id": "VARCHAR",
    "contract_execution_date": "DATE",
    "commencement_date_of_contract_term": "DATE",
    "contract_termination_date": "DATE",
    "actual_termination_date": "DATE",
    "extension_provision_description": "VARCHAR",
    "class_name": "VARCHAR",
    "term_name": "VARCHAR",
    "increment_name": "VARCHAR",
    "product_type_name": "VARCHAR",
    "product_name": "VARCHAR",
    "point_of_delivery_balancing_authority": "VARCHAR",
    "point_of_delivery_specific_location": "VARCHAR",
    "begin_date": "DATE",
    "end_date": "DATE",
}
IDENTITY_COLS = {
    "year_quarter": "VARCHAR",
    "company_id_ferc": "VARCHAR",
    "filer_unique_id": "VARCHAR",
    "company_name": "VARCHAR",
}
TX_COLS = {
    "year_quarter": "VARCHAR",
    "seller_company_id_ferc": "VARCHAR",
    "transaction_unique_id": "VARCHAR",
    "customer_company_name": "VARCHAR",
    "contract_service_agreement_id": "VARCHAR",
    "trade_date": "DATE",
    "transaction_begin_date": "DATE",
    "type_of_rate": "VARCHAR",
    "term_name": "VARCHAR",
    "product_name": "VARCHAR",
    "point_of_delivery_balancing_authority": "VARCHAR",
    "point_of_delivery_specific_location": "VARCHAR",
    "standardized_quantity": "DOUBLE",
    "total_transaction_charge": "DOUBLE",
}

MISSING_COLUMNS: dict[str, set[str]] = {}

EQR_SNAPSHOTS = {"core_ferceqr__contracts": CONTRACT_COLS, "core_ferceqr__quarterly_identity": IDENTITY_COLS}
EIA_SNAPSHOT = "core_pudl__assn_eia_pudl_utilities"
EIA_COLS = ("utility_id_eia", "utility_name_eia", "utility_id_pudl")
# EIA Form 861, schedule 2B: each utility's name as filed each year, and where its power came from. The names
# give renamed utilities their former names; the purchases show how much of a company's buying EQR can see.
EIA861_OPS = "core_eia861__yearly_operational_data_misc"
EIA861_OPS_COLS = {
    "report_date": "DATE",
    "utility_id_eia": "BIGINT",
    "utility_name_eia": "VARCHAR",
    "nerc_region": "VARCHAR",
    "wholesale_power_purchases_mwh": "DOUBLE",
    "net_generation_mwh": "DOUBLE",
    "retail_sales_mwh": "DOUBLE",
    "sales_for_resale_mwh": "DOUBLE",
}
# EIA abbreviates utility names ("South Mississippi El Pwr Assn"); sellers spell them out. A spelled-out
# copy of each EIA name is matched too. "EL" means Electric only before these words ("El Paso" stays).
EIA_SPELL_OUT = {
    "MUN": "MUNICIPAL", "MUNI": "MUNICIPAL", "AGNY": "AGENCY", "AGCY": "AGENCY", "AUTH": "AUTHORITY",
    "DIST": "DISTRICT", "DEPT": "DEPARTMENT", "UTIL": "UTILITY", "UTILS": "UTILITIES", "COMM": "COMMISSION",
    "SYS": "SYSTEM", "MEMB": "MEMBERSHIP", "PUB": "PUBLIC", "TWP": "TOWNSHIP", "CNTY": "COUNTY", "LT": "LIGHT",
    "SERV": "SERVICE", "BD": "BOARD", "COMMN": "COMMISSION", "ADMIN": "ADMINISTRATION",
}
EIA_REDACTED_ID = 88888   # utilities that keep their data private file under this ID: many companies in one
EIA_EL_BEFORE = {
    "PWR", "POWER", "COOP", "CO-OP", "COOPERATIVE", "MEMB", "MEMBERSHIP", "ASSN", "ASSOC", "ASSOCIATION", "CO",
    "CORP", "SYS", "SYSTEM", "LT", "LIGHT", "DEPT", "DEPARTMENT", "UTIL", "UTILITY", "UTILITIES", "AUTH",
    "AUTHORITY", "DIST", "DISTRICT", "COMM", "COMMISSION", "SVC", "SERVICE", "SERVICES", "&", "AND", "PLANT",
}


def window_snapshot(snap_dir: Path, name: str, window: list[str]) -> Path:
    return snap_dir / f"{name}_{window[0]}_{window[-1]}.parquet"


def snapshot_usable(path: Path, cols) -> bool:
    """True if a snapshot file exists and carries every column this version of the script reads."""
    if not path.exists():
        return False
    try:
        names = {r[0] for r in duckdb.sql(f"SELECT name FROM parquet_schema({sql_str(path.as_posix())})").fetchall()}
    except duckdb.Error:
        return False
    return set(cols) <= names


def tx_filter_sql() -> str:
    """Rows kept from the transactions table: bilateral power delivered in PJM, MISO or NYISO, plus every trade
    whose buyer is a roster company (by the raw spellings in tx_buyers), wherever it was delivered. A co-op in
    MISO can buy from a utility next door, delivered in that utility's area or with no area filled in."""
    geo = scope_case("point_of_delivery_balancing_authority", "point_of_delivery_specific_location")
    return (f"upper(coalesce(product_name, '')) IN {sql_tuple(TX_PRODUCTS)} "
            f"AND upper(coalesce(type_of_rate, '')) <> 'RTO/ISO' "
            f"AND ({geo} IS NOT NULL OR upper(trim(customer_company_name)) IN (SELECT raw_key FROM tx_buyers))")


def kwh_sql(t: str = "t") -> str:
    """True for an energy row priced so low it only makes sense as kWh entered in the MWh field."""
    q, charge = f"{t}.standardized_quantity", f"{t}.total_transaction_charge"
    return (f"coalesce(upper({t}.product_name) = 'ENERGY' AND {q} >= {KWH_MIN_QTY} AND {charge} > 0 "
            f"AND {charge} / {q} < {KWH_MAX_PRICE}, false)")


def spell_out_eia(raw) -> str:
    """EIA's abbreviations spelled out: "South Mississippi El Pwr Assn" -> "SOUTH MISSISSIPPI ELECTRIC PWR ASSN".
    normalize_name then turns PWR, ASSN and the rest into words as it does for every name."""
    toks = str(raw or "").upper().replace("&", " & ").replace(",", " ").split()
    keys = [t.strip(".") for t in toks]
    out = []
    for i, k in enumerate(keys):
        nxt = keys[i + 1] if i + 1 < len(keys) else ""
        prev = keys[i - 1] if i else ""
        if k == "EL" and nxt in EIA_EL_BEFORE:
            out.append("ELECTRIC")
        elif k == "P" and prev in ("MUN", "MUNI", "MUNICIPAL") and nxt in ("AGNY", "AGCY", "AGENCY"):
            out.append("POWER")
        else:
            out.append(EIA_SPELL_OUT.get(k, toks[i]))
    return " ".join(out)


def link_eia_history(con) -> dict:
    """Tie roster companies to EIA-861 utilities, then give each tied company the names its utility has filed
    under (as filed and spelled out) and its yearly sources of power. A company is tied only by an exact name
    (any year, either form), and only when the tie is one company to one utility: a name shared by several
    utilities ("City of Columbia") ties nothing, and neither does the ID that utilities keeping their data private
    share. A name that is another roster company's never becomes an alias."""
    hist = con.execute(f"""
        SELECT utility_id_eia, utility_name_eia, min(year(report_date)) AS first_year,
               max(year(report_date)) AS last_year
        FROM eia861_raw WHERE utility_id_eia IS NOT NULL AND utility_id_eia <> {EIA_REDACTED_ID}
          AND coalesce(trim(utility_name_eia), '') <> '' AND report_date IS NOT NULL
        GROUP BY 1, 2""").fetchall()
    recs = []
    for uid, raw, first, last in hist:
        as_filed, spelled = normalize_name(raw), normalize_name(spell_out_eia(raw))
        if as_filed:
            recs.append((int(uid), raw, "eia_as_filed", as_filed, int(first), int(last)))
        if spelled and spelled != as_filed:
            recs.append((int(uid), raw, "eia_spelled_out", spelled, int(first), int(last)))
    con.register("_eh", pd.DataFrame(recs, columns=["utility_id_eia", "utility_name_eia", "variant", "name_norm",
                                                    "first_year", "last_year"]))
    con.execute("CREATE OR REPLACE TABLE eia_hist_names AS SELECT * FROM _eh")
    con.unregister("_eh")
    build_candidates(con, "roster_entities", "eia_hist_names", "cand_eia_hist", fuzzy=False)
    con.execute("""
    CREATE OR REPLACE TABLE eia_links AS
    WITH m AS (SELECT DISTINCT c.entity_id, h.utility_id_eia
               FROM cand_eia_hist c JOIN eia_hist_names h ON h.name_norm = c.target_norm WHERE c.tier <= 2),
         e AS (SELECT entity_id FROM m GROUP BY 1 HAVING count(*) = 1),
         u AS (SELECT utility_id_eia FROM m GROUP BY 1 HAVING count(*) = 1)
    SELECT m.entity_id, m.utility_id_eia FROM m JOIN e USING (entity_id) JOIN u USING (utility_id_eia)""")
    con.execute("""
    CREATE OR REPLACE TABLE entity_eia_names AS
    WITH n AS (
      SELECT l.entity_id, l.utility_id_eia, h.name_norm AS alias_norm, h.variant,
             h.utility_name_eia || ' (' || h.first_year ||
               CASE WHEN h.last_year > h.first_year THEN '-' || h.last_year ELSE '' END || ')' AS example
      FROM eia_links l JOIN eia_hist_names h USING (utility_id_eia)
      WHERE h.name_norm NOT IN (SELECT name_norm FROM roster_entities)),
         shared AS (SELECT alias_norm FROM n GROUP BY 1 HAVING count(DISTINCT entity_id) > 1)
    SELECT entity_id, utility_id_eia, alias_norm, min(variant) AS variant,
           string_agg(DISTINCT example, ' ; ') AS raw_examples
    FROM n WHERE alias_norm NOT IN (SELECT alias_norm FROM shared)
    GROUP BY 1, 2, 3""")
    con.execute("""
    CREATE OR REPLACE TABLE entity_eia861 AS
    SELECT l.entity_id, l.utility_id_eia, year(r.report_date) AS year,
           sum(r.wholesale_power_purchases_mwh) AS purchases_mwh, sum(r.net_generation_mwh) AS net_generation_mwh,
           sum(r.retail_sales_mwh) AS retail_sales_mwh, sum(r.sales_for_resale_mwh) AS sales_for_resale_mwh
    FROM eia_links l JOIN eia861_raw r USING (utility_id_eia)
    WHERE r.report_date IS NOT NULL
    GROUP BY 1, 2, 3""")
    links = one(con, "SELECT count(*) FROM eia_links")[0]
    aliases, named = one(con, "SELECT count(*), count(DISTINCT entity_id) FROM entity_eia_names")
    latest = one(con, "SELECT max(year) FROM entity_eia861 WHERE purchases_mwh IS NOT NULL")[0]
    return {"status": "ok", "links": links, "aliases": aliases, "companies_with_aliases": named,
            "latest_purchases_year": latest}


def mtime_iso(path: Path) -> str:
    return dt.datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sql_str(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def sql_tuple(items) -> str:
    return "(" + ", ".join(sql_str(x) for x in items) + ")"


def q_key(q: str) -> int:
    m = QRE.fullmatch(q.strip().lower())
    if not m:
        raise SystemExit(f"Bad quarter '{q}'. Use YYYYqN, e.g. 2025q3.")
    return int(m.group(1)) * 4 + int(m.group(2)) - 1


def q_name(k: int) -> str:
    return f"{k // 4}q{k % 4 + 1}"


def pct(n, d) -> float:
    return (n / d) if d else float("nan")


def fp(x) -> str:
    return "n/a" if x is None or x != x else f"{x * 100:.1f}%"


def fi(n) -> str:
    return "n/a" if n is None else f"{int(n):,}"


def md_table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join("" if v is None else " ".join(str(v).replace("|", "/").split()) for v in r)
                   + " |")
    return "\n".join(out)


def gate(value, bar) -> str:
    if bar is None:
        return "NO BAR"
    if value is None or value != value:
        return "n/a"
    return "PASS" if value >= bar else "FAIL"


def scope_case(ba_col: str, loc_col: str) -> str:
    """The balancing authority decides first; the location string is consulted only when the BA
    names no in-scope market (blank, 'HUB', 'N/A', or another BA such as SOCO at a seam)."""
    ba, loc = f"upper(coalesce({ba_col}, ''))", f"upper(coalesce({loc_col}, ''))"
    whens = [f"WHEN regexp_matches({ba}, {sql_str(p)}) THEN {sql_str(s)}" for s, p in SCOPE_PATTERNS.items()]
    whens += [f"WHEN regexp_matches({loc}, {sql_str(p)}) THEN {sql_str(s)}" for s, p in SCOPE_PATTERNS.items()]
    return f"(CASE {' '.join(whens)} ELSE NULL END)"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def one(con, sql: str):
    return con.execute(sql).fetchone()


def rows(con, sql: str):
    return con.execute(sql).fetchall()


# =============================================================================
# 5. INPUTS
# =============================================================================
NAME_HEADERS = ["entity_name", "entity name", "entity", "name", "company", "company name", "company_name",
                "member", "member name", "member_name", "participant", "participant name",
                "market participant", "market participant name", "organization", "organization name",
                "legal name", "legal entity name", "counterparty", "customer"]
ISO_HEADERS = ["iso", "iso/rto", "rto", "market", "region"]


def canon_iso(label: str) -> str:
    u = (label or "").upper()
    if "PJM" in u:
        return "PJM"
    if "MISO" in u or "MIDCONTINENT" in u or "MIDWEST ISO" in u:
        return "MISO"
    if "NYISO" in u or "NEW YORK" in u or u.strip() == "NY":
        return "NYISO"
    if "ERCOT" in u:
        return "ERCOT"
    if "CAISO" in u or "CALIFORNIA" in u:
        return "CAISO"
    if "SPP" in u or "SOUTHWEST POWER" in u:
        return "SPP"
    if "ISO-NE" in u or "ISONE" in u or "ISO NE" in u or "NEW ENGLAND" in u:
        return "ISONE"
    return u.strip() or "UNKNOWN"


def _frame_from_raw(raw: pd.DataFrame, name_col, iso_col, iso_label: str, source: str) -> pd.DataFrame:
    raw = raw.astype(object).where(raw.notna(), "")
    raw = raw.map(lambda v: str(v).strip())
    wanted = [name_col.strip().lower()] if name_col else NAME_HEADERS
    header_row = name_idx = None
    vals: list[str] = []
    for i in range(min(25, len(raw))):
        vals = [v.lower() for v in raw.iloc[i].tolist()]
        hit = next((w for w in wanted if w in vals), None)
        if hit:
            header_row, name_idx = i, vals.index(hit)
            break
    if header_row is None:
        if raw.shape[1] == 1 and not name_col:
            names, isos = raw.iloc[:, 0].tolist(), [iso_label] * len(raw)
        else:
            preview = [raw.iloc[i].tolist()[:8] for i in range(min(3, len(raw)))]
            raise SystemExit(f"Couldn't find a name column in {source}. First rows: {preview}. "
                             f"Pass --name-col 'Your Header'.")
    else:
        body = raw.iloc[header_row + 1:]
        names = body.iloc[:, name_idx].tolist()
        iso_idx = None
        for w in ([iso_col.strip().lower()] if iso_col else ISO_HEADERS):
            if w in vals:
                iso_idx = vals.index(w)
                break
        isos = body.iloc[:, iso_idx].tolist() if iso_idx is not None else [iso_label] * len(body)
    df = pd.DataFrame({"entity_name": names, "iso_label": [i or iso_label for i in isos], "source": source})
    return df[(df.entity_name != "") & (df.entity_name.str.lower() != "nan")]


def load_roster(paths, sheets, name_col, iso_col, iso_default) -> pd.DataFrame:
    frames = []
    for p in map(Path, paths):
        if not p.exists():
            raise SystemExit(f"Roster not found: {p}")
        if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
            xl = pd.ExcelFile(p)
            use = [s.strip() for s in sheets.split(",")] if sheets else xl.sheet_names
            missing = [s for s in use if s not in xl.sheet_names]
            if missing:
                raise SystemExit(f"Sheets {missing} not in {p.name}. Available: {xl.sheet_names}")
            for sh in use:
                raw = xl.parse(sh, header=None, dtype=str)
                frames.append(_frame_from_raw(raw, name_col, iso_col, iso_default or sh, f"{p.name}:{sh}"))
        else:
            raw = pd.read_csv(p, header=None, dtype=str, keep_default_na=False)
            frames.append(_frame_from_raw(raw, name_col, iso_col, iso_default or p.stem, p.name))
    roster = pd.concat(frames, ignore_index=True)
    roster["iso"] = roster["iso_label"].map(canon_iso)
    return roster


def build_entities(roster: pd.DataFrame) -> pd.DataFrame:
    r = roster.copy()
    r["name_norm"] = r["entity_name"].map(normalize_name)
    r = r[r.name_norm != ""]
    g = (r.groupby("name_norm", sort=True)
          .agg(entity_name=("entity_name", "first"),
               isos=("iso", lambda s: "+".join(sorted(set(s)))),
               n_roster_rows=("entity_name", "size"),
               sources=("source", lambda s: "; ".join(sorted(set(s)))))
          .reset_index())
    g.insert(0, "entity_id", [f"R{i + 1:05d}" for i in range(len(g))])
    return g


def connect(out: Path, needs_s3: bool, args) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(out / "reality_check.duckdb"))
    tmp = (out / ".duckdb_tmp").as_posix()
    con.execute(f"SET temp_directory={sql_str(tmp)}")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET enable_progress_bar=true")
    if args.memory_limit:
        con.execute(f"SET memory_limit={sql_str(args.memory_limit)}")
    if args.threads:
        con.execute(f"SET threads={int(args.threads)}")
    if needs_s3:
        try:
            con.execute("INSTALL httpfs; LOAD httpfs;")
            # The bucket name contains dots, so DuckDB must use path-style addressing (per PUDL docs).
            con.execute("CREATE OR REPLACE SECRET pudl_s3 "
                        "(TYPE s3, PROVIDER config, REGION 'us-west-2', URL_STYLE 'path')")
        except duckdb.Error as exc:
            raise SystemExit(
                f"Couldn't set up S3 access ({str(exc).splitlines()[0]}).\n"
                "DuckDB downloads its httpfs extension from extensions.duckdb.org on first use; a proxy or "
                "firewall can block that. Workaround: mirror the small tables locally and point at them:\n"
                "  aws s3 sync --no-sign-request s3://pudl.catalyst.coop/ferceqr/core_ferceqr__contracts "
                "./ferceqr/core_ferceqr__contracts\n"
                "  (same for core_ferceqr__quarterly_identity, plus the transactions quarter you want)\n"
                "  then rerun with --eqr-root ./ferceqr --skip-eia")
    return con


def select_from_files(con, table: str, files: list[tuple[str, str]], cols: dict[str, str],
                      where: str | None = None) -> str:
    """UNION ALL of per-file SELECTs with explicit TRY_CASTs, so schema drift between
    quarters can't break the run. Missing columns become typed NULLs and are reported."""
    parts = []
    for q, path in files:
        try:
            avail = {r[0] for r in rows(con, f"SELECT name FROM parquet_schema({sql_str(path)})")}
        except duckdb.Error as exc:
            raise SystemExit(f"Couldn't read {path}: {exc}")
        exprs = []
        for c, t in cols.items():
            if c == "year_quarter":
                exprs.append(f"{sql_str(q)} AS year_quarter")  # one file == one quarter
            elif c in avail:
                exprs.append(f'TRY_CAST("{c}" AS {t}) AS "{c}"')
            else:
                exprs.append(f'CAST(NULL AS {t}) AS "{c}"')
                MISSING_COLUMNS.setdefault(table, set()).add(c)
        sel = f"SELECT {', '.join(exprs)} FROM read_parquet({sql_str(path)})"
        parts.append(f"SELECT * FROM ({sel}) WHERE {where}" if where else sel)
    return "\nUNION ALL\n".join(parts)


def extend_name_map(con, source_sql: str) -> int:
    """Normalize any raw names from source_sql (one column: raw_name) not yet in name_map."""
    new = rows(con, f"""
        SELECT DISTINCT raw_name FROM ({source_sql}) s
        WHERE raw_name IS NOT NULL AND raw_name NOT IN (SELECT raw_name FROM name_map)""")
    if new:
        df = pd.DataFrame({"raw_name": [r[0] for r in new]})
        df["name_norm"] = df["raw_name"].map(normalize_name)
        con.register("_new_names", df)
        con.execute("INSERT INTO name_map SELECT raw_name, name_norm FROM _new_names")
        con.unregister("_new_names")
    return len(new)


NUM_TOKEN_RE = r"\b([0-9]+|[IVX]+)\b"  # digits or roman numerals: "Solar 1" vs "Solar 2", "Ridge II" vs "IV"


def build_candidates(con, left: str, right: str, out: str, fuzzy: bool = True) -> None:
    """left(entity_id, name_norm) x right(name_norm) -> out(entity_id, target_norm, method, score, tier, confidence).
    Fuzzy matches must carry the same numbers: numbered project companies look alike but are different entities.
    fuzzy=False keeps exact matches only (EIA names: a former name is either the same name or not used)."""
    nums = f"list_sort(regexp_extract_all(name_norm, {sql_str(NUM_TOKEN_RE)}))"
    fuzzy_sql = f"""
           UNION ALL
           SELECT l.entity_id, r.name_norm, 'fuzzy', jaro_winkler_similarity(l.name_norm, r.name_norm), 3
           FROM l JOIN r ON left(l.name_norm, 1) = left(r.name_norm, 1) AND l.ns <> r.ns AND l.nums = r.nums
           WHERE length(l.name_norm) >= {FUZZY_MIN_LEN} AND length(r.name_norm) >= {FUZZY_MIN_LEN}
             AND jaro_winkler_similarity(l.name_norm, r.name_norm) >= {FUZZY_REVIEW}""" if fuzzy else ""
    con.execute(f"""
    CREATE OR REPLACE TABLE {out} AS
    WITH l AS (SELECT entity_id, name_norm, replace(name_norm, ' ', '') AS ns, {nums} AS nums
               FROM {left} WHERE coalesce(name_norm, '') <> ''),
         r AS (SELECT DISTINCT name_norm, replace(name_norm, ' ', '') AS ns, {nums} AS nums
               FROM {right} WHERE coalesce(name_norm, '') <> ''),
         c AS (
           SELECT l.entity_id, r.name_norm AS target_norm, 'exact' AS method, CAST(1.0 AS DOUBLE) AS score, 1 AS tier
           FROM l JOIN r ON l.name_norm = r.name_norm
           UNION ALL
           SELECT l.entity_id, r.name_norm, 'exact_nospace', 0.995, 2
           FROM l JOIN r ON l.ns = r.ns AND l.name_norm <> r.name_norm{fuzzy_sql})
    SELECT *, CASE WHEN tier < 3 OR score >= {FUZZY_HIGH} THEN 'high' ELSE 'review' END AS confidence
    FROM c""")


def copy_csv(con, sql: str, path: Path) -> None:
    con.execute(f"COPY ({sql}) TO {sql_str(path.as_posix())} (HEADER, DELIMITER ',')")


# =============================================================================
# 6. MAIN
# =============================================================================
def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--roster", action="append", required=True,
                   help="Roster CSV/XLSX. Repeat for more files (e.g. add the PJM member list).")
    p.add_argument("--sheets", help="Comma-separated XLSX tabs to read (default: all tabs).")
    p.add_argument("--name-col", help="Header of the entity-name column (auto-detected if omitted).")
    p.add_argument("--iso-col", help="Header of an ISO column (default: tab name or file name).")
    p.add_argument("--iso", help="Force one ISO label for every roster row.")
    p.add_argument("--quarters", help="Contract window, e.g. 2023q3:2026q2 (default: last --last-n available).")
    p.add_argument("--last-n", type=int, default=12, help="Quarters in the default window (default 12).")
    p.add_argument("--tx-quarters", help="Transaction quarters, comma-separated (default: last quarter of window).")
    p.add_argument("--skip-transactions", action="store_true", help="Contract-side checks only (fast).")
    p.add_argument("--eqr-root", help="Local mirror of s3://pudl.catalyst.coop/ferceqr (same layout).")
    p.add_argument("--pudl-release", default="stable", help="PUDL release for the EIA crosswalk (default: stable).")
    p.add_argument("--eia-utilities", help="Path to core_pudl__assn_eia_pudl_utilities.parquet (overrides release).")
    p.add_argument("--eia861", help=f"Path to {EIA861_OPS}.parquet (overrides release).")
    p.add_argument("--skip-eia", action="store_true", help="Skip the EIA utility name leg.")
    p.add_argument("--bar-roster", type=float, help="Override BARS['roster_resolution'].")
    p.add_argument("--bar-volume", type=float, help="Override BARS['volume_coverage'].")
    p.add_argument("--bar-dates", type=float, help="Override BARS['usable_dates'].")
    p.add_argument("--no-bars", action="store_true", help="Run without bars (the report will say so).")
    p.add_argument("--out", default="reality_check_out", help="Output directory.")
    p.add_argument("--memory-limit", help="DuckDB memory limit, e.g. 6GB.")
    p.add_argument("--threads", type=int, help="DuckDB threads.")
    p.add_argument("--decisions", help="Your filled review_queue.csv (or .xlsx): y accepts a match, n rejects it. "
                                       "Rows you add for matches the script missed are applied too.")
    p.add_argument("--refresh", action="store_true",
                   help="Pull fresh data from S3 instead of reusing the snapshot in --out.")
    return p.parse_args(argv)


YES = {"y", "yes", "1", "true", "t", "x", "accept", "ok"}
NO = {"n", "no", "0", "false", "f", "reject"}


def load_decisions(path: str) -> tuple[pd.DataFrame, str]:
    """Read match decisions into memory before anything in --out is rewritten."""
    p = Path(path).expanduser()
    if not p.exists():
        raise SystemExit(f"Decisions file not found: {p}")
    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(p, dtype=str)
    else:
        df = None
        for enc in ("utf-8-sig", "cp1252", "latin-1"):  # Excel on a Mac may save in a legacy encoding
            try:
                df = pd.read_csv(p, dtype=str, keep_default_na=False, encoding=enc)
                break
            except UnicodeDecodeError:
                continue
    df.columns = [str(c).strip().lower() for c in df.columns]
    if "decision_y_n" not in df.columns and "verdict_y_n" in df.columns:
        df = df.rename(columns={"verdict_y_n": "decision_y_n"})
    missing = {"entity_name", "eqr_customer_norm", "decision_y_n"} - set(df.columns)
    if missing:
        raise SystemExit(f"{p.name} is missing columns {sorted(missing)}. Use the review_queue.csv this script wrote.")
    df = df.fillna("").astype(str)
    flag = df["decision_y_n"].str.strip().str.lower()
    df["decision"] = flag.map(lambda s: "y" if s in YES else ("n" if s in NO else ""))
    df = df[df["decision"] != ""].copy()
    df["entity_norm"] = df["entity_name"].map(normalize_name)
    df["target_norm"] = df["eqr_customer_norm"].map(normalize_name)  # a raw EQR spelling works too
    return df[["entity_name", "entity_norm", "target_norm", "decision"]].drop_duplicates(), sha256(p)


def apply_decisions(con, dec: pd.DataFrame) -> dict:
    """y -> a manual high-confidence match; n -> the candidate is removed. A pair marked both ways counts as n."""
    con.register("_dec", dec)
    con.execute("""
    CREATE OR REPLACE TABLE decisions AS
    SELECT d.entity_name, d.entity_norm, d.target_norm,
           CASE WHEN bool_or(d.decision = 'n') THEN 'n' ELSE 'y' END AS decision,
           any_value(re.entity_id) AS entity_id,
           (any_value(re.entity_id) IS NOT NULL AND any_value(e.name_norm) IS NOT NULL) AS applies
    FROM _dec d LEFT JOIN roster_entities re ON re.name_norm = d.entity_norm
    LEFT JOIN eqr_customers e ON e.name_norm = d.target_norm
    GROUP BY d.entity_name, d.entity_norm, d.target_norm""")
    con.unregister("_dec")
    con.execute("""DELETE FROM cand_customers c USING decisions d
                   WHERE d.applies AND c.entity_id = d.entity_id AND c.target_norm = d.target_norm""")
    con.execute("""INSERT INTO cand_customers (entity_id, target_norm, method, score, tier, confidence)
                   SELECT DISTINCT entity_id, target_norm, 'manual', 1.0, 0, 'high'
                   FROM decisions WHERE applies AND decision = 'y'""")
    acc, rej, skipped = one(con, """SELECT count(*) FILTER (WHERE applies AND decision = 'y'),
                                           count(*) FILTER (WHERE applies AND decision = 'n'),
                                           count(*) FILTER (WHERE NOT applies) FROM decisions""")
    return {"accepted": acc, "rejected": rej, "not_found": skipped}


def main(argv=None) -> None:
    args = parse_args(argv)
    t_start = time.time()
    bars = dict(BARS)
    for key, flag in (("roster_resolution", args.bar_roster), ("volume_coverage", args.bar_volume),
                      ("usable_dates", args.bar_dates)):
        if flag is not None:
            bars[key] = flag
    bars_set = all(v is not None for v in bars.values())
    if not bars_set and not args.no_bars:
        raise SystemExit("Set your bars before you look: edit BARS at the top of this file or pass "
                         "--bar-roster / --bar-volume / --bar-dates (e.g. 0.6 0.5 0.8). "
                         "Use --no-bars to run anyway; the report will record that.")
    ver = tuple(int(x) for x in re.findall(r"\d+", duckdb.__version__)[:2])
    if ver < (1, 1):
        log(f"WARNING: DuckDB {duckdb.__version__} detected; 1.1+ is recommended (CREATE SECRET, path-style S3).")

    # Read decisions first: the file usually lives in --out, which this run rewrites.
    decisions, decisions_sha = load_decisions(args.decisions) if args.decisions else (None, None)

    out = Path(args.out).expanduser().resolve()
    snap_dir = out / "snapshot"
    snap_dir.mkdir(parents=True, exist_ok=True)
    eqr_root = args.eqr_root.rstrip("/") if args.eqr_root else EQR_ROOT_S3
    eia_path = args.eia_utilities or f"{PUDL_ROOT_S3}/{args.pudl_release}/core_pudl__assn_eia_pudl_utilities.parquet"
    eia861_path = args.eia861 or f"{PUDL_ROOT_S3}/{args.pudl_release}/{EIA861_OPS}.parquet"

    # ---------------------------------------------------------------- window + snapshot reuse
    # A rerun into the same --out reuses that run's snapshot, so only your decisions move the numbers.
    prev: dict = {}
    if not args.refresh and (out / "manifest.json").exists():
        try:
            prev = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prev = {}
    prev_snaps = prev.get("snapshots") or {}
    window = None
    if args.quarters:
        a, _, b = args.quarters.partition(":")
        ka, kb = q_key(a), q_key(b or a)
        if kb < ka:
            raise SystemExit("--quarters end is before its start.")
        window = [q_name(k) for k in range(ka, kb + 1)]
    elif prev.get("quarters"):
        window = [str(q) for q in prev["quarters"]]
    reuse_eqr = (window is not None and not args.refresh and
                 all(snapshot_usable(window_snapshot(snap_dir, n, window), cols) for n, cols in EQR_SNAPSHOTS.items()))
    if window is not None and not reuse_eqr and not args.quarters:
        window = None  # the old window's files are gone; list what's available now
    eia_snap = snap_dir / f"{EIA_SNAPSHOT}.parquet"
    reuse_eia = not args.refresh and not args.skip_eia and snapshot_usable(eia_snap, EIA_COLS)
    eia861_snap = snap_dir / f"{EIA861_OPS}.parquet"
    reuse_eia861 = not args.refresh and not args.skip_eia and snapshot_usable(eia861_snap, EIA861_OPS_COLS)

    def tx_quarters_for(win):
        return [q.strip().lower() for q in args.tx_quarters.split(",")] if args.tx_quarters else [win[-1]]

    # The trades slice depends on which buyer names resolve to roster companies, so whether its snapshot can be
    # reused is settled after resolution; S3 is set up whenever a pull may be needed.
    needs_s3 = ((eqr_root.startswith("s3://") and (not reuse_eqr or not args.skip_transactions))
                or (not args.skip_eia and not reuse_eia and eia_path.startswith("s3://"))
                or (not args.skip_eia and not reuse_eia861 and eia861_path.startswith("s3://")))
    con = connect(out, needs_s3, args)
    con.execute("CREATE OR REPLACE TABLE name_map (raw_name VARCHAR, name_norm VARCHAR)")

    if window is None:
        pattern = f"{eqr_root}/core_ferceqr__contracts/*.parquet"
        try:
            listed = [r[0] for r in rows(con, f"SELECT file FROM glob({sql_str(pattern)})")]
        except duckdb.Error as exc:
            raise SystemExit(f"Couldn't list {pattern} ({exc}). Pass --quarters 2023q3:2026q2.")
        ks = sorted({q_key(m.group(0)) for f in listed if (m := QRE.search(Path(f).name))})
        if not ks:
            raise SystemExit(f"No quarterly Parquet files found at {pattern}")
        window = [q_name(k) for k in ks if k > ks[-1] - args.last_n]
    min_qk, max_qk = q_key(window[0]), q_key(window[-1])
    tx_quarters = tx_quarters_for(window)
    if reuse_eqr:
        taken = (prev_snaps.get("core_ferceqr__contracts", {}).get("taken_at")
                 or mtime_iso(window_snapshot(snap_dir, "core_ferceqr__contracts", window)))
        log(f"Reusing the data snapshot in {out} (taken {taken}). Pass --refresh to pull fresh data.")
    log(f"Contract window {window[0]}..{window[-1]} ({len(window)} quarters); source "
        f"{'snapshot' if reuse_eqr else eqr_root}")

    qrows = []
    for k in range(min_qk - 1, max_qk + 9):
        start = dt.date(k // 4, (k % 4) * 3 + 1, 1)
        end = (dt.date(start.year + (start.month + 3 > 12), (start.month + 3 - 1) % 12 + 1, 1) - dt.timedelta(days=1))
        qrows.append((k, q_name(k), start, end))
    con.register("_q", pd.DataFrame(qrows, columns=["qk", "q", "q_start", "q_end"]))
    con.execute("CREATE OR REPLACE TABLE quarters AS SELECT * FROM _q")
    con.unregister("_q")

    # ---------------------------------------------------------------- roster
    roster = load_roster(args.roster, args.sheets, args.name_col, args.iso_col, args.iso)
    entities = build_entities(roster)
    if entities.empty:
        raise SystemExit("Roster produced zero usable names.")
    con.register("_ent", entities)
    con.execute("CREATE OR REPLACE TABLE roster_entities AS SELECT * FROM _ent")
    con.unregister("_ent")
    roster_isos = sorted(set(roster["iso"]))
    log(f"Roster: {len(roster):,} rows -> {len(entities):,} distinct entities; ISOs: {', '.join(roster_isos)}")

    # ---------------------------------------------------------------- EQR contracts + identity
    files_c = [(q, f"{eqr_root}/core_ferceqr__contracts/{q}.parquet") for q in window]
    files_i = [(q, f"{eqr_root}/core_ferceqr__quarterly_identity/{q}.parquet") for q in window]
    snap = {}
    taken_now = dt.datetime.now().isoformat(timespec="seconds")
    for tbl, name, label, files, cols in (
            ("contracts_raw", "core_ferceqr__contracts", "contracts", files_c, CONTRACT_COLS),
            ("identity_raw", "core_ferceqr__quarterly_identity", "identity", files_i, IDENTITY_COLS)):
        path = window_snapshot(snap_dir, name, window)
        kind = "TEMP TABLE" if tbl == "contracts_raw" else "TABLE"
        if reuse_eqr:
            con.execute(f"CREATE OR REPLACE {kind} {tbl} AS SELECT * FROM read_parquet({sql_str(path.as_posix())})")
            taken = prev_snaps.get(name, {}).get("taken_at") or mtime_iso(path)
        else:
            log(f"Reading {name} ({len(files)} quarters)...")
            con.execute(f"CREATE OR REPLACE {kind} {tbl} AS {select_from_files(con, label, files, cols)}")
            con.execute(f"COPY {tbl} TO {sql_str(path.as_posix())} (FORMAT parquet, COMPRESSION zstd)")
            taken = taken_now
        snap[name] = {"file": path.name, "sha256": sha256(path), "rows": one(con, f"SELECT count(*) FROM {tbl}")[0],
                      "taken_at": taken, "reused": reuse_eqr, "columns": "subset used by this check"}
    n_contract_rows = snap["core_ferceqr__contracts"]["rows"]
    log(f"Contract rows in window: {n_contract_rows:,}")

    extend_name_map(con, "SELECT customer_company_name AS raw_name FROM contracts_raw")
    extend_name_map(con, "SELECT company_name AS raw_name FROM identity_raw")
    geo = scope_case("c.point_of_delivery_balancing_authority", "c.point_of_delivery_specific_location")
    con.execute(f"""
    CREATE OR REPLACE TABLE contracts AS
    SELECT c.*, m.name_norm AS customer_norm,
           regexp_matches(coalesce(m.name_norm, ''), {sql_str(MARKET_OPERATOR_RE)}) AS customer_is_mo,
           {geo} AS geo_scope,
           (upper(coalesce(c.product_type_name, '')) IN {sql_tuple(POWER_PRODUCT_TYPES)}
            AND upper(coalesce(c.product_name, '')) NOT IN {sql_tuple(NON_POWER_PRODUCTS)}) AS is_power_sale,
           coalesce(c.seller_company_id_ferc, '?') || '|' ||
             coalesce(c.contract_service_agreement_id, 'NOCSA:' || coalesce(c.ferc_tariff_reference, '')) || '|' ||
             coalesce(m.name_norm, '') AS contract_key,
           {QK_SQL.format(c='c.year_quarter')} AS qk
    FROM contracts_raw c LEFT JOIN name_map m ON m.raw_name = c.customer_company_name""")

    con.execute("""
    CREATE OR REPLACE TABLE key_summary AS
    WITH k AS (
      SELECT contract_key, min(qk) AS first_qk, max(qk) AS last_qk,
             any_value(seller_company_id_ferc) AS seller_cid, any_value(customer_norm) AS customer_norm,
             any_value(contract_service_agreement_id) AS csa_id,
             bool_or(is_power_sale) AS power, bool_or(coalesce(contract_affiliate, false)) AS affiliate,
             bool_or(coalesce(customer_is_mo, false)) AS mo,
             bool_or(coalesce(geo_scope, '') = 'PJM') AS pjm, bool_or(coalesce(geo_scope, '') = 'MISO') AS miso,
             bool_or(coalesce(geo_scope, '') = 'NYISO') AS nyiso, count(*) AS n_rows
      FROM contracts GROUP BY contract_key),
    d AS (
      SELECT c.contract_key,
             max(c.contract_execution_date) AS execution_date,
             max(c.commencement_date_of_contract_term) AS commencement_date,
             max(c.contract_termination_date) AS termination_date,
             max(c.actual_termination_date) AS actual_termination_date,
             max(upper(c.extension_provision_description)) AS extension_text,
             max(upper(c.term_name)) AS term_name
      FROM contracts c JOIN k ON c.contract_key = k.contract_key AND c.qk = k.last_qk
      GROUP BY c.contract_key)
    SELECT k.*, d.* EXCLUDE (contract_key) FROM k JOIN d USING (contract_key)""")

    # buys_power: the name is the buyer in at least one power sale (not an affiliate sale, not a sale to an
    # RTO/ISO). Interconnection, transmission and membership agreements name companies that may never buy power.
    con.execute("""
    CREATE OR REPLACE TABLE eqr_customers AS
    SELECT customer_norm AS name_norm, count(*) AS n_rows, count(DISTINCT contract_key) AS n_keys,
           count(DISTINCT contract_key) FILTER (WHERE is_power_sale AND NOT coalesce(contract_affiliate, false)
                                                AND NOT coalesce(customer_is_mo, false)) AS n_power_keys,
           count(DISTINCT customer_company_name) AS n_spellings, count(DISTINCT seller_company_id_ferc) AS n_sellers,
           bool_or(coalesce(customer_is_mo, false)) AS is_mo,
           coalesce(bool_or(is_power_sale AND NOT coalesce(contract_affiliate, false)
                            AND NOT coalesce(customer_is_mo, false)), false) AS buys_power
    FROM contracts WHERE coalesce(customer_norm, '') <> '' GROUP BY 1""")
    con.execute("""
    CREATE OR REPLACE TABLE eqr_sellers AS
    SELECT i.company_id_ferc AS seller_cid, m.name_norm, any_value(i.company_name) AS company_name
    FROM identity_raw i JOIN name_map m ON m.raw_name = i.company_name
    WHERE upper(coalesce(i.filer_unique_id, '')) LIKE 'FS%' AND i.company_id_ferc IS NOT NULL
    GROUP BY 1, 2""")

    # ---------------------------------------------------------------- EIA leg
    eia_status = "skipped (--skip-eia)"
    con.execute("CREATE OR REPLACE TABLE eia_names (utility_id_eia BIGINT, utility_name_eia VARCHAR, "
                "utility_id_pudl BIGINT, name_norm VARCHAR)")
    if not args.skip_eia:
        try:
            src = eia_snap if reuse_eia else Path(eia_path) if not eia_path.startswith("s3://") else eia_path
            con.execute(f"""CREATE OR REPLACE TABLE eia_raw AS
                SELECT TRY_CAST(utility_id_eia AS BIGINT) AS utility_id_eia, CAST(utility_name_eia AS VARCHAR) AS utility_name_eia,
                       TRY_CAST(utility_id_pudl AS BIGINT) AS utility_id_pudl
                FROM read_parquet({sql_str(src.as_posix() if isinstance(src, Path) else src)})""")
            if not reuse_eia:
                con.execute(f"COPY eia_raw TO {sql_str(eia_snap.as_posix())} (FORMAT parquet, COMPRESSION zstd)")
            snap[EIA_SNAPSHOT] = {"file": eia_snap.name, "sha256": sha256(eia_snap),
                                  "rows": one(con, "SELECT count(*) FROM eia_raw")[0],
                                  "taken_at": (prev_snaps.get(EIA_SNAPSHOT, {}).get("taken_at") or mtime_iso(eia_snap))
                                  if reuse_eia else taken_now, "reused": reuse_eia, "source": eia_path}
            extend_name_map(con, "SELECT utility_name_eia AS raw_name FROM eia_raw")
            con.execute("""INSERT INTO eia_names SELECT e.utility_id_eia, e.utility_name_eia, e.utility_id_pudl, m.name_norm
                           FROM eia_raw e JOIN name_map m ON m.raw_name = e.utility_name_eia""")
            eia_status = (f"{one(con, 'SELECT count(*) FROM eia_names')[0]:,} EIA utilities from "
                          f"{'the snapshot' if reuse_eia else eia_path}")
        except duckdb.Error as exc:
            eia_status = f"unavailable ({str(exc).splitlines()[0][:160]})"
            log(f"EIA leg skipped: {eia_status}")

    # ---------------------------------------------------------------- EIA-861: former names and purchases
    eia_hist: dict = {"status": "skipped (--skip-eia)"}
    con.execute("CREATE OR REPLACE TABLE entity_eia_names (entity_id VARCHAR, utility_id_eia BIGINT, "
                "alias_norm VARCHAR, variant VARCHAR, raw_examples VARCHAR)")
    con.execute("CREATE OR REPLACE TABLE entity_eia861 (entity_id VARCHAR, utility_id_eia BIGINT, year INTEGER, "
                "purchases_mwh DOUBLE, net_generation_mwh DOUBLE, retail_sales_mwh DOUBLE, "
                "sales_for_resale_mwh DOUBLE)")
    if not args.skip_eia:
        try:
            src = eia861_snap.as_posix() if reuse_eia861 else eia861_path
            con.execute(f"CREATE OR REPLACE TABLE eia861_raw AS "
                        f"{select_from_files(con, 'eia861', [('', src)], EIA861_OPS_COLS)}")
            if not reuse_eia861:
                con.execute(f"COPY eia861_raw TO {sql_str(eia861_snap.as_posix())} (FORMAT parquet, COMPRESSION zstd)")
            snap[EIA861_OPS] = {"file": eia861_snap.name, "sha256": sha256(eia861_snap),
                                "rows": one(con, "SELECT count(*) FROM eia861_raw")[0],
                                "taken_at": (prev_snaps.get(EIA861_OPS, {}).get("taken_at") or mtime_iso(eia861_snap))
                                if reuse_eia861 else taken_now, "reused": reuse_eia861, "source": eia861_path}
            eia_hist = link_eia_history(con)
            log(f"EIA-861: {eia_hist['links']:,} roster companies tied to an EIA utility; {eia_hist['aliases']:,} "
                f"former or spelled-out names added for {eia_hist['companies_with_aliases']:,}.")
        except (duckdb.Error, SystemExit) as exc:
            eia_hist = {"status": f"unavailable ({str(exc).splitlines()[0][:160]})"}
            log(f"EIA-861 leg skipped: {eia_hist['status']}")
    con.execute("CREATE OR REPLACE VIEW eia_alias_names AS "
                "SELECT entity_id, alias_norm AS name_norm FROM entity_eia_names")

    # ---------------------------------------------------------------- Q1 resolution
    log("Resolving roster names against EQR customers, EQR sellers and EIA utilities...")
    build_candidates(con, "roster_entities", "eqr_customers", "cand_customers")
    # Former and spelled-out EIA names add exact matches only; fuzzy matching stays on roster names.
    build_candidates(con, "eia_alias_names", "eqr_customers", "cand_customers_eia", fuzzy=False)
    eia_hist["new_customer_names"] = one(con, """
        SELECT count(DISTINCT target_norm) FROM cand_customers_eia
        WHERE tier <= 2 AND target_norm NOT IN (SELECT target_norm FROM cand_customers WHERE confidence = 'high')""")[0]
    con.execute("INSERT INTO cand_customers SELECT entity_id, target_norm, 'eia_' || method, score, tier, confidence "
                "FROM cand_customers_eia WHERE tier <= 2")
    build_candidates(con, "roster_entities", "eqr_sellers", "cand_sellers")
    build_candidates(con, "roster_entities", "eia_names", "cand_eia")
    con.execute("CREATE OR REPLACE TABLE cand_customers_auto AS SELECT * FROM cand_customers")
    con.execute("CREATE OR REPLACE TABLE decisions (entity_name VARCHAR, entity_norm VARCHAR, target_norm VARCHAR, "
                "decision VARCHAR, entity_id VARCHAR, applies BOOLEAN)")
    decision_stats = None
    if decisions is not None:
        decision_stats = apply_decisions(con, decisions)
        log(f"Your match decisions: {decision_stats['accepted']} accepted, {decision_stats['rejected']} rejected, "
            f"{decision_stats['not_found']} not found in this run.")
    con.execute("""
    CREATE OR REPLACE TABLE eqr_resolved AS
    SELECT target_norm, entity_id, method, score FROM cand_customers WHERE confidence = 'high'
    QUALIFY row_number() OVER (PARTITION BY target_norm
                               ORDER BY tier, method LIKE 'eia_%', score DESC, entity_id) = 1""")
    con.execute("""
    CREATE OR REPLACE TABLE roster_matches AS
    WITH bc AS (SELECT * FROM cand_customers
                QUALIFY row_number() OVER (PARTITION BY entity_id ORDER BY tier, score DESC, target_norm) = 1),
         hc AS (SELECT entity_id, count(*) AS n_high FROM cand_customers WHERE confidence = 'high' GROUP BY 1),
         bs AS (SELECT c.entity_id, c.method, s.seller_cid, s.company_name FROM cand_sellers c
                JOIN eqr_sellers s ON s.name_norm = c.target_norm WHERE c.confidence = 'high'
                QUALIFY row_number() OVER (PARTITION BY c.entity_id ORDER BY c.tier, c.score DESC, s.seller_cid) = 1),
         be AS (SELECT c.entity_id, c.method, e.utility_id_eia, e.utility_name_eia, e.utility_id_pudl FROM cand_eia c
                JOIN eia_names e ON e.name_norm = c.target_norm WHERE c.confidence = 'high'
                QUALIFY row_number() OVER (PARTITION BY c.entity_id ORDER BY c.tier, c.score DESC, e.utility_id_eia) = 1),
         vol AS (SELECT r.entity_id, sum(e.n_keys) AS n_keys, sum(e.n_power_keys) AS n_power_keys,
                        sum(e.n_spellings) AS n_spellings
                 FROM eqr_resolved r JOIN eqr_customers e ON e.name_norm = r.target_norm GROUP BY 1),
         pb AS (SELECT DISTINCT c.entity_id, true AS pb_flag FROM cand_customers c
                JOIN eqr_customers e ON e.name_norm = c.target_norm WHERE c.confidence = 'high' AND e.buys_power)
    SELECT re.entity_id, re.entity_name, re.isos, re.name_norm,
           coalesce(pb.pb_flag, false) AS found_as_power_buyer,
           bc.target_norm AS best_eqr_customer, bc.method AS customer_method, round(bc.score, 3) AS customer_score,
           bc.confidence AS customer_confidence, coalesce(hc.n_high, 0) AS eqr_names_matched,
           coalesce(vol.n_power_keys, 0) AS eqr_power_contracts, coalesce(vol.n_keys, 0) AS eqr_contract_keys,
           coalesce(vol.n_spellings, 0) AS eqr_raw_spellings,
           bs.seller_cid, bs.company_name AS seller_name, bs.method AS seller_method,
           be.utility_id_eia, be.utility_name_eia, be.utility_id_pudl, be.method AS eia_method
    FROM roster_entities re
    LEFT JOIN bc USING (entity_id) LEFT JOIN hc USING (entity_id) LEFT JOIN bs USING (entity_id)
    LEFT JOIN be USING (entity_id) LEFT JOIN vol USING (entity_id) LEFT JOIN pb USING (entity_id)""")

    n_ent, n_power, n_high, n_review, n_exact, n_seller, n_eia, n_found_any = one(con, """
        SELECT count(*), count(*) FILTER (WHERE found_as_power_buyer),
               count(*) FILTER (WHERE customer_confidence = 'high'),
               count(*) FILTER (WHERE customer_confidence = 'review'),
               count(*) FILTER (WHERE customer_method IN ('exact', 'exact_nospace', 'eia_exact', 'eia_exact_nospace')),
               count(*) FILTER (WHERE seller_cid IS NOT NULL), count(*) FILTER (WHERE utility_id_eia IS NOT NULL),
               count(*) FILTER (WHERE customer_confidence = 'high' OR seller_cid IS NOT NULL)
        FROM roster_matches""")
    roster_rate = pct(n_power, n_ent)
    by_iso = rows(con, """
        SELECT iso, count(*), count(*) FILTER (WHERE found_as_power_buyer),
               count(*) FILTER (WHERE customer_confidence = 'high')
        FROM (SELECT unnest(string_split(isos, '+')) AS iso, found_as_power_buyer, customer_confidence
              FROM roster_matches)
        GROUP BY 1 ORDER BY 1""")
    # Why companies found in EQR aren't power buyers: the contract types that name them instead.
    not_buyer_why = rows(con, """
        WITH np AS (SELECT entity_id FROM roster_matches WHERE customer_confidence = 'high' AND NOT found_as_power_buyer),
             t AS (SELECT DISTINCT c.entity_id, c.target_norm FROM cand_customers c JOIN np USING (entity_id)
                   WHERE c.confidence = 'high')
        SELECT CASE WHEN k.is_power_sale AND coalesce(k.contract_affiliate, false) THEN 'Power sale from an affiliate'
                    WHEN k.is_power_sale AND coalesce(k.customer_is_mo, false) THEN 'Power sale to an RTO/ISO'
                    ELSE upper(coalesce(nullif(trim(k.product_name), ''), '(blank product)')) END AS kind,
               count(DISTINCT t.entity_id) AS entities
        FROM t JOIN contracts k ON k.customer_norm = t.target_norm
        GROUP BY 1 ORDER BY 2 DESC LIMIT 8""")

    con.execute("""
    CREATE OR REPLACE TABLE key_flags AS
    SELECT k.*,
      (power AND NOT affiliate AND NOT mo) AS eligible,
      coalesce(customer_norm IN (SELECT target_norm FROM eqr_resolved), false) AS roster_linked
    FROM key_summary k""")
    coverage = {}
    for scope in ("PJM", "MISO", "NYISO"):
        col = scope.lower()
        keys, keys_res, rws, rws_res = one(con, f"""
            SELECT count(*), count(*) FILTER (WHERE roster_linked), sum(n_rows), sum(n_rows) FILTER (WHERE roster_linked)
            FROM key_flags WHERE eligible AND {col}""")
        coverage[scope] = {"keys": keys, "keys_resolved": keys_res, "rate": pct(keys_res, keys),
                           "rows": rws or 0, "rows_resolved": rws_res or 0, "row_rate": pct(rws_res or 0, rws or 0)}
    scoped_share = one(con, "SELECT avg(CASE WHEN pjm OR miso OR nyiso THEN 1 ELSE 0 END) FROM key_flags WHERE eligible")[0]

    con.execute("""
    CREATE OR REPLACE TABLE unmatched_inscope AS
    WITH k AS (SELECT * FROM key_flags WHERE eligible AND (pjm OR miso) AND NOT roster_linked
                                         AND coalesce(customer_norm, '') <> ''),
         agg AS (SELECT customer_norm, count(*) AS n_keys, sum(n_rows) AS n_rows,
                        count(DISTINCT seller_cid) AS n_sellers, bool_or(pjm) AS pjm, bool_or(miso) AS miso
                 FROM k GROUP BY 1),
         rev AS (SELECT c.target_norm, re.entity_name AS review_candidate, round(c.score, 3) AS review_score
                 FROM cand_customers c JOIN roster_entities re USING (entity_id) WHERE c.confidence = 'review'
                 QUALIFY row_number() OVER (PARTITION BY c.target_norm ORDER BY c.score DESC) = 1),
         sp AS (SELECT customer_norm, string_agg(DISTINCT customer_company_name, ' ; ') AS spellings
                FROM contracts GROUP BY 1)
    SELECT a.customer_norm, a.n_keys, a.n_rows, a.n_sellers,
           CASE WHEN a.pjm AND a.miso THEN 'PJM+MISO' WHEN a.pjm THEN 'PJM' ELSE 'MISO' END AS scope,
           rev.review_candidate, rev.review_score, left(sp.spellings, 240) AS raw_spellings
    FROM agg a LEFT JOIN rev ON rev.target_norm = a.customer_norm LEFT JOIN sp USING (customer_norm)
    ORDER BY a.n_keys DESC, a.n_rows DESC""")
    unmatched_by_scope = dict(rows(con, "SELECT scope, count(*) FROM unmatched_inscope GROUP BY 1"))

    copy_csv(con, "SELECT * FROM roster_matches ORDER BY customer_confidence NULLS LAST, eqr_contract_keys DESC",
             out / "roster_matches.csv")
    con.execute("""
    CREATE OR REPLACE TABLE cust_info AS
    SELECT customer_norm, left(string_agg(DISTINCT customer_company_name, ' ; '), 200) AS spellings,
           array_to_string(list_sort(list_distinct(list(geo_scope) FILTER (WHERE geo_scope IS NOT NULL))), '+') AS scopes,
           max(year_quarter) AS last_quarter
    FROM contracts WHERE coalesce(customer_norm, '') <> '' GROUP BY 1""")
    # The review queue holds only fuzzy matches that can change a result: the EQR name must buy power, and
    # must not already have an exact match (or one you accepted), since an exact match always wins.
    # Pairs you decided on stay listed with your decision, so the file can be edited and passed back
    # with --decisions as many times as you like.
    con.execute("""
    CREATE OR REPLACE TABLE review_pairs AS
    WITH settled AS (SELECT DISTINCT target_norm FROM cand_customers WHERE tier <= 2),
         fz AS (SELECT a.entity_id, a.target_norm, a.score, a.confidence
                FROM cand_customers_auto a JOIN eqr_customers e ON e.name_norm = a.target_norm
                WHERE a.method = 'fuzzy' AND e.buys_power AND a.target_norm NOT IN (SELECT target_norm FROM settled)),
         d AS (SELECT * FROM decisions WHERE applies),
         pairs AS (SELECT entity_id, target_norm FROM fz UNION SELECT entity_id, target_norm FROM d)
    SELECT p.entity_id, p.target_norm,
           coalesce(fz.score, (SELECT max(a2.score) FROM cand_customers_auto a2 WHERE a2.method = 'fuzzy'
                               AND a2.entity_id = p.entity_id AND a2.target_norm = p.target_norm)) AS score,
           d.decision,
           CASE WHEN d.decision = 'y' THEN 'you accepted' WHEN d.decision = 'n' THEN 'you rejected'
                WHEN fz.confidence = 'high' THEN 'auto-accepted' ELSE 'needs decision' END AS status
    FROM pairs p LEFT JOIN fz USING (entity_id, target_norm) LEFT JOIN d USING (entity_id, target_norm)""")
    copy_csv(con, """
        WITH q AS (
          SELECT re.entity_name, re.isos, rp.target_norm AS eqr_customer_norm, round(rp.score, 3) AS score, rp.status,
                 e.n_power_keys AS eqr_power_contracts, e.n_sellers AS eqr_sellers, ci.scopes AS eqr_delivery_scopes,
                 ci.spellings AS eqr_spellings, coalesce(rp.decision, '') AS decision_y_n
          FROM review_pairs rp JOIN roster_entities re USING (entity_id)
          LEFT JOIN eqr_customers e ON e.name_norm = rp.target_norm
          LEFT JOIN cust_info ci ON ci.customer_norm = rp.target_norm
          UNION ALL
          SELECT entity_name, NULL, target_norm, NULL, 'not found in this run', NULL, NULL, NULL, NULL, decision
          FROM decisions WHERE NOT applies)
        SELECT * FROM q
        ORDER BY CASE status WHEN 'needs decision' THEN 0 WHEN 'auto-accepted' THEN 1 WHEN 'you accepted' THEN 2
                             WHEN 'you rejected' THEN 3 ELSE 4 END,
                 CASE WHEN status = 'auto-accepted' THEN score END ASC NULLS LAST,
                 eqr_power_contracts DESC NULLS LAST, entity_name""", out / "review_queue.csv")
    con.execute("""
    CREATE OR REPLACE TABLE alias_seed AS
    WITH a AS (
      SELECT entity_id, name_norm AS alias_norm, 'roster' AS source, 'roster' AS method, CAST(1.0 AS DOUBLE) AS score,
             CAST(NULL AS VARCHAR) AS seller_cid, CAST(NULL AS BIGINT) AS utility_id_eia,
             CAST(NULL AS BIGINT) AS utility_id_pudl, entity_name AS raw_examples
      FROM roster_entities
      UNION ALL
      SELECT r.entity_id, r.target_norm, 'eqr_customer', r.method, r.score, NULL, NULL, NULL, ci.spellings
      FROM eqr_resolved r LEFT JOIN cust_info ci ON ci.customer_norm = r.target_norm
      UNION ALL
      SELECT c.entity_id, c.target_norm, 'eqr_seller', c.method, c.score, s.seller_cid, NULL, NULL, s.company_name
      FROM cand_sellers c JOIN eqr_sellers s ON s.name_norm = c.target_norm WHERE c.confidence = 'high'
      UNION ALL
      SELECT c.entity_id, c.target_norm, 'eia_utility', c.method, c.score, NULL, e.utility_id_eia, e.utility_id_pudl,
             e.utility_name_eia
      FROM cand_eia c JOIN eia_names e ON e.name_norm = c.target_norm WHERE c.confidence = 'high'
      UNION ALL
      SELECT entity_id, alias_norm, 'eia_name_history', variant, 1.0, NULL, utility_id_eia, NULL, raw_examples
      FROM entity_eia_names)
    SELECT re.entity_id, re.entity_name, re.isos, a.source, a.alias_norm, a.method, round(a.score, 3) AS score,
           a.seller_cid, a.utility_id_eia, a.utility_id_pudl,
           CASE WHEN a.source = 'eqr_customer' THEN e.buys_power END AS buys_power,
           CASE WHEN a.source = 'eqr_customer' THEN e.n_power_keys END AS eqr_power_contracts,
           CASE WHEN a.source = 'eqr_customer' THEN e.n_keys END AS eqr_contract_keys,
           CASE WHEN a.source = 'eqr_customer' THEN e.n_sellers END AS eqr_sellers,
           CASE WHEN a.source = 'eqr_customer' THEN ci.last_quarter END AS eqr_last_quarter,
           a.raw_examples
    FROM a JOIN roster_entities re USING (entity_id)
    LEFT JOIN eqr_customers e ON e.name_norm = a.alias_norm AND a.source = 'eqr_customer'
    LEFT JOIN cust_info ci ON ci.customer_norm = a.alias_norm AND a.source = 'eqr_customer'""")
    copy_csv(con, """SELECT * FROM alias_seed
                     ORDER BY entity_id, CASE source WHEN 'roster' THEN 0 WHEN 'eqr_customer' THEN 1
                                                     WHEN 'eqr_seller' THEN 2 ELSE 3 END, alias_norm""",
             out / "alias_seed.csv")
    review_counts = dict(rows(con, """
        SELECT CASE status WHEN 'needs decision' THEN 'open' WHEN 'auto-accepted' THEN 'auto' ELSE 'decided' END,
               count(*)
        FROM review_pairs GROUP BY 1"""))
    review_counts["filtered"] = one(con, """
        SELECT count(*) FROM cand_customers_auto a
        WHERE a.method = 'fuzzy' AND NOT EXISTS (
          SELECT 1 FROM review_pairs rp WHERE rp.entity_id = a.entity_id AND rp.target_norm = a.target_norm)""")[0]
    copy_csv(con, "SELECT * FROM unmatched_inscope", out / "unmatched_inscope_customers.csv")
    copy_csv(con, """
        SELECT re.entity_name, re.isos, count(DISTINCT c.customer_company_name) AS raw_spellings,
               count(DISTINCT c.seller_company_id_ferc) AS sellers,
               left(string_agg(DISTINCT c.customer_company_name, ' ; '), 400) AS examples
        FROM eqr_resolved r JOIN roster_entities re USING (entity_id)
        JOIN contracts c ON c.customer_norm = r.target_norm
        GROUP BY 1, 2 ORDER BY raw_spellings DESC""", out / "spelling_variants.csv")
    spell_stats = one(con, """
        SELECT median(n), max(n), count(*) FILTER (WHERE n >= 3) FROM (
          SELECT r.entity_id, count(DISTINCT c.customer_company_name) AS n
          FROM eqr_resolved r JOIN contracts c ON c.customer_norm = r.target_norm GROUP BY 1)""")
    top_spell = rows(con, """
        SELECT re.entity_name, count(DISTINCT c.customer_company_name) AS n,
               left(string_agg(DISTINCT c.customer_company_name, ' ; '), 150)
        FROM eqr_resolved r JOIN roster_entities re USING (entity_id)
        JOIN contracts c ON c.customer_norm = r.target_norm
        GROUP BY 1 ORDER BY n DESC LIMIT 8""")

    # ---------------------------------------------------------------- Q2 date quality
    log("Checking contract dates...")
    con.execute(f"""
    CREATE OR REPLACE TABLE key_dates AS
    SELECT f.*,
      csa_id IS NULL AS csa_null,
      commencement_date IS NULL AS comm_null,
      (commencement_date IS NOT NULL AND (year(commencement_date) < {MIN_PLAUSIBLE_YEAR}
                                          OR year(commencement_date) >= {OPEN_ENDED_YEAR})) AS comm_bad,
      termination_date IS NULL AS term_null,
      (termination_date IS NOT NULL AND year(termination_date) >= {OPEN_ENDED_YEAR}) AS term_sentinel,
      (termination_date IS NOT NULL AND year(termination_date) < {MIN_PLAUSIBLE_YEAR}) AS term_bad,
      (termination_date IS NOT NULL AND commencement_date IS NOT NULL AND year(termination_date) < {OPEN_ENDED_YEAR}
       AND termination_date < commencement_date) AS term_before_comm,
      actual_termination_date IS NOT NULL AS has_actual,
      (actual_termination_date IS NOT NULL AND (year(actual_termination_date) < {MIN_PLAUSIBLE_YEAR}
                                                OR year(actual_termination_date) >= {OPEN_ENDED_YEAR})) AS actual_bad,
      (actual_termination_date IS NOT NULL AND commencement_date IS NOT NULL
       AND actual_termination_date < commencement_date) AS actual_before_comm,
      (regexp_matches(coalesce(extension_text, ''), {sql_str(EVERGREEN_RE)})
       AND NOT regexp_matches(coalesce(extension_text, ''), {sql_str(NO_EXTENSION_RE)})) AS evergreen_ext,
      date_diff('day', commencement_date, termination_date) AS stated_tenor_days
    FROM key_flags f""")
    con.execute("""
    CREATE OR REPLACE TABLE key_dates2 AS
    SELECT *,
      (NOT comm_null AND NOT comm_bad AND NOT term_bad AND NOT term_before_comm
       AND NOT actual_bad AND NOT actual_before_comm) AS usable_dates,
      (term_null OR term_sentinel) AS open_ended
    FROM key_dates""")

    populations = {"Roster-linked": "eligible AND roster_linked", "All PJM/MISO in scope": "eligible AND (pjm OR miso)"}
    date_rows, date_stats = [], {}
    metrics = [
        ("Commencement date missing", "comm_null"), ("Commencement date implausible", "comm_bad"),
        ("Termination date missing (open-ended)", "term_null"),
        (f"Termination date >= {OPEN_ENDED_YEAR} (sentinel)", "term_sentinel"),
        (f"Termination date < {MIN_PLAUSIBLE_YEAR}", "term_bad"),
        ("Termination before commencement", "term_before_comm"),
        ("Actual termination date present", "has_actual"), ("Actual termination implausible", "actual_bad"),
        ("Actual termination before commencement", "actual_before_comm"),
        ("**Usable (all dates intact)**", "usable_dates"),
        ("Service agreement ID missing (join key)", "csa_null"),
    ]
    for label, cond in populations.items():
        vals = one(con, f"SELECT count(*), {', '.join(f'avg({m}::INT)' for _, m in metrics)} FROM key_dates2 WHERE {cond}")
        date_stats[label] = {"n": vals[0], **{m: vals[i + 1] for i, (_, m) in enumerate(metrics)}}
    for label, m in metrics:
        date_rows.append([label] + [fp(date_stats[p][m]) for p in populations])
    usable_rate = date_stats["Roster-linked"]["usable_dates"]
    copy_csv(con, f"""SELECT '{list(populations)[0]}' AS population, * FROM (
                      SELECT count(*) AS contracts, {', '.join(f'round(avg({m}::INT), 4) AS {m}' for _, m in metrics)}
                      FROM key_dates2 WHERE {populations['Roster-linked']})
                      UNION ALL
                      SELECT '{list(populations)[1]}', * FROM (
                      SELECT count(*), {', '.join(f'round(avg({m}::INT), 4)' for _, m in metrics)}
                      FROM key_dates2 WHERE {populations['All PJM/MISO in scope']})""", out / "date_quality.csv")

    # ---------------------------------------------------------------- Q3 where tenor lives
    tenor = {}
    for label, cond in populations.items():
        tenor[label] = one(con, f"""
            SELECT count(*),
                   avg(open_ended::INT),
                   avg(CASE WHEN NOT open_ended THEN evergreen_ext::INT END),
                   avg((NOT open_ended AND NOT evergreen_ext AND stated_tenor_days < 3653)::INT),
                   avg(CASE WHEN term_name = 'LT' THEN 1 WHEN term_name = 'ST' THEN 0 END)
            FROM key_dates2 WHERE {cond} AND usable_dates""")
    buckets = rows(con, f"""
        SELECT CASE WHEN stated_tenor_days < 31 THEN '< 1 month' WHEN stated_tenor_days < 366 THEN '1-12 months'
                    WHEN stated_tenor_days < 1096 THEN '1-3 years' WHEN stated_tenor_days < 3653 THEN '3-10 years'
                    ELSE '> 10 years' END AS bucket, count(*) AS n, min(stated_tenor_days) AS o
        FROM key_dates2 WHERE {populations['Roster-linked']} AND usable_dates AND NOT open_ended
        GROUP BY 1 ORDER BY o""")
    carries_tenor = tenor["Roster-linked"][3]

    # ---------------------------------------------------------------- Q4 terminations + vanished
    log("Sizing early terminations, re-papers and vanished contracts...")
    # Only terminations dated inside the window count. Some filers keep listing contracts that ended years ago.
    window_start = dt.date(min_qk // 4, (min_qk % 4) * 3 + 1, 1)
    con.execute(f"""
    CREATE OR REPLACE TABLE term_events AS
    SELECT k.*,
      CASE WHEN open_ended THEN 'open_ended_terminated'
           WHEN date_diff('day', actual_termination_date, termination_date) > {EARLY_TOLERANCE_DAYS} THEN 'early'
           WHEN date_diff('day', termination_date, actual_termination_date) > {EARLY_TOLERANCE_DAYS} THEN 'after_stated_term'
           ELSE 'at_stated_term' END AS event_class,
      year(actual_termination_date) * 4 + quarter(actual_termination_date) - 1 AS event_qk
    FROM key_dates2 k
    WHERE eligible AND roster_linked AND has_actual AND usable_dates
      AND actual_termination_date >= DATE '{window_start.isoformat()}'""")
    stale_terminations = one(con, f"""
        SELECT count(*) FROM key_dates2
        WHERE eligible AND roster_linked AND has_actual AND usable_dates
          AND actual_termination_date < DATE '{window_start.isoformat()}'""")[0]
    # repaper_like: the same seller and customer sign a new contract around the termination (netted out).
    # recontracted_elsewhere: the customer signs with a different seller around the termination. Not netted,
    # but a cluster of these under one seller points to a novation, merger or portfolio exit, not credit.
    con.execute(f"""
    CREATE OR REPLACE TABLE term_events2 AS
    SELECT e.*, EXISTS (
      SELECT 1 FROM key_dates2 k2
      WHERE k2.seller_cid = e.seller_cid AND k2.customer_norm = e.customer_norm AND k2.contract_key <> e.contract_key
        AND (date_diff('day', e.actual_termination_date, coalesce(k2.execution_date, k2.commencement_date))
               BETWEEN -{REPAPER_DAYS_BEFORE} AND {REPAPER_DAYS_AFTER}
             OR (k2.first_qk > {min_qk} AND k2.first_qk BETWEEN e.event_qk - 1 AND e.event_qk + 2))) AS repaper_like,
      EXISTS (
      SELECT 1 FROM key_dates2 k3
      WHERE k3.customer_norm = e.customer_norm AND k3.seller_cid <> e.seller_cid AND k3.power
        AND (date_diff('day', e.actual_termination_date, coalesce(k3.execution_date, k3.commencement_date))
               BETWEEN -{REPAPER_DAYS_BEFORE} AND {REPAPER_DAYS_AFTER}
             OR (k3.first_qk > {min_qk} AND k3.first_qk BETWEEN e.event_qk - 1 AND e.event_qk + 2))) AS recontracted_elsewhere
    FROM term_events e""")
    ev = dict((r[0], r[1:]) for r in rows(con, """
        SELECT event_class, count(*), count(*) FILTER (WHERE NOT repaper_like) FROM term_events2 GROUP BY 1"""))
    net_signal_events, net_signal_entities, net_recontracted = one(con, """
        SELECT count(*), count(DISTINCT r.entity_id), count(*) FILTER (WHERE t.recontracted_elsewhere)
        FROM term_events2 t JOIN eqr_resolved r ON r.target_norm = t.customer_norm
        WHERE t.event_class IN ('early', 'open_ended_terminated') AND NOT t.repaper_like""")
    ev_by_year = rows(con, """
        SELECT year(actual_termination_date) AS y,
               count(*) FILTER (WHERE event_class = 'early'),
               count(*) FILTER (WHERE event_class = 'open_ended_terminated'),
               count(*) FILTER (WHERE event_class IN ('early', 'open_ended_terminated') AND NOT repaper_like)
        FROM term_events2 GROUP BY 1 ORDER BY 1""")
    con.execute("""CREATE OR REPLACE TABLE seller_names AS
                   SELECT seller_cid, any_value(company_name) AS seller FROM eqr_sellers GROUP BY 1""")
    term_sellers = rows(con, """
        SELECT t.seller_cid, any_value(s.seller) AS seller, count(*) AS net_events,
               count(DISTINCT t.customer_norm) AS customers, count(*) FILTER (WHERE t.recontracted_elsewhere) AS elsewhere,
               min(year(t.actual_termination_date)) AS y0, max(year(t.actual_termination_date)) AS y1
        FROM term_events2 t LEFT JOIN seller_names s USING (seller_cid)
        WHERE t.event_class IN ('early', 'open_ended_terminated') AND NOT t.repaper_like
        GROUP BY 1 ORDER BY 3 DESC, 1 LIMIT 8""")
    # Years where one seller drives most of the net events (only flagged when the year has 10+ events).
    term_concentration = rows(con, """
        WITH e AS (SELECT year(actual_termination_date) AS y, seller_cid, count(*) AS n FROM term_events2
                   WHERE event_class IN ('early', 'open_ended_terminated') AND NOT repaper_like GROUP BY 1, 2),
             tot AS (SELECT y, sum(n) AS total FROM e GROUP BY 1)
        SELECT e.y, e.seller_cid, s.seller, e.n, tot.total
        FROM e JOIN tot USING (y) LEFT JOIN seller_names s USING (seller_cid)
        WHERE tot.total >= 10
        QUALIFY row_number() OVER (PARTITION BY e.y ORDER BY e.n DESC, e.seller_cid) = 1
        ORDER BY e.y""")
    term_concentration = [r for r in term_concentration if r[3] / r[4] >= 0.4]
    copy_csv(con, """
        SELECT re.entity_name AS customer_entity, re.isos, t.customer_norm, t.seller_cid, s.seller, t.csa_id,
               t.commencement_date, t.termination_date, t.actual_termination_date, t.event_class, t.repaper_like,
               t.recontracted_elsewhere
        FROM term_events2 t JOIN eqr_resolved r ON r.target_norm = t.customer_norm
        JOIN roster_entities re USING (entity_id) LEFT JOIN seller_names s USING (seller_cid)
        ORDER BY t.actual_termination_date DESC""", out / "early_terminations.csv")

    con.execute(f"""
    CREATE OR REPLACE TABLE vanished AS
    WITH last_sellers AS (SELECT DISTINCT seller_company_id_ferc AS seller_cid FROM contracts WHERE qk = {max_qk})
    SELECT f.*, q.q_end AS last_q_end,
      EXISTS (SELECT 1 FROM key_summary k2 WHERE k2.seller_cid = f.seller_cid AND k2.csa_id = f.csa_id
              AND k2.customer_norm <> f.customer_norm AND k2.first_qk > f.last_qk) AS rekeyed_customer_name,
      EXISTS (SELECT 1 FROM key_summary k2 WHERE k2.seller_cid = f.seller_cid AND k2.customer_norm = f.customer_norm
              AND coalesce(k2.csa_id, '') <> coalesce(f.csa_id, '')
              AND k2.first_qk BETWEEN f.last_qk AND f.last_qk + 1) AS replaced_or_rekeyed_id
    FROM key_dates2 f JOIN quarters q ON q.qk = f.last_qk
    WHERE f.eligible AND f.roster_linked AND f.last_qk < {max_qk}
      AND f.seller_cid IN (SELECT seller_cid FROM last_sellers)
      AND NOT f.has_actual AND (f.open_ended OR f.termination_date > q.q_end)""")
    disappeared = one(con, f"""
        SELECT count(*) FROM key_dates2 WHERE eligible AND roster_linked AND last_qk < {max_qk}""")[0]
    van_n, van_rekey = one(con, """
        SELECT count(*), count(*) FILTER (WHERE rekeyed_customer_name OR replaced_or_rekeyed_id) FROM vanished""")
    copy_csv(con, """
        SELECT re.entity_name AS customer_entity, v.customer_norm, v.seller_cid, v.csa_id, q.q AS last_seen,
               v.termination_date, v.rekeyed_customer_name, v.replaced_or_rekeyed_id
        FROM vanished v JOIN eqr_resolved r ON r.target_norm = v.customer_norm
        JOIN roster_entities re USING (entity_id) JOIN quarters q ON q.qk = v.last_qk
        ORDER BY v.last_qk DESC""", out / "vanished_contracts.csv")

    top_ba = rows(con, """
        SELECT upper(coalesce(nullif(trim(point_of_delivery_balancing_authority), ''), '(blank)')) AS ba,
               array_to_string(list_sort(list_distinct(list(coalesce(geo_scope, 'none')))), '+') AS scope,
               count(*) AS n
        FROM contracts c JOIN key_flags f USING (contract_key) WHERE f.eligible
        GROUP BY 1 ORDER BY n DESC LIMIT 12""")
    top_loc = rows(con, """
        SELECT upper(coalesce(nullif(trim(point_of_delivery_specific_location), ''), '(blank)')) AS loc,
               array_to_string(list_sort(list_distinct(list(coalesce(geo_scope, 'none')))), '+') AS scope,
               count(*) AS n
        FROM contracts c JOIN key_flags f USING (contract_key) WHERE f.eligible
        GROUP BY 1 ORDER BY n DESC LIMIT 12""")

    # ---------------------------------------------------------------- manifest + contract report
    manifest = {
        "run_at": dt.datetime.now().isoformat(timespec="seconds"),
        "duckdb": duckdb.__version__, "python": platform.python_version(),
        "source": {"eqr_root": eqr_root, "eia_utilities": None if args.skip_eia else eia_path,
                   "eia861": None if args.skip_eia else eia861_path},
        "contract_window": [window[0], window[-1]], "quarters": window, "tx_quarters": None,
        "files": {"contracts": [f for _, f in files_c], "identity": [f for _, f in files_i]},
        "snapshots": snap, "missing_columns": {k: sorted(v) for k, v in MISSING_COLUMNS.items()},
        "bars": bars, "bars_set_before_run": bars_set,
        "tunables": {"SCOPE_PATTERNS": SCOPE_PATTERNS, "MARKET_OPERATOR_RE": MARKET_OPERATOR_RE,
                     "EVERGREEN_RE": EVERGREEN_RE, "FUZZY_HIGH": FUZZY_HIGH, "FUZZY_REVIEW": FUZZY_REVIEW,
                     "EARLY_TOLERANCE_DAYS": EARLY_TOLERANCE_DAYS, "OPEN_ENDED_YEAR": OPEN_ENDED_YEAR,
                     "REPAPER_WINDOW_DAYS": [-REPAPER_DAYS_BEFORE, REPAPER_DAYS_AFTER],
                     "KWH_MAX_PRICE": KWH_MAX_PRICE, "KWH_MIN_QTY": KWH_MIN_QTY},
        "roster": {"files": args.roster, "rows": len(roster), "entities": len(entities), "isos": roster_isos},
        "decisions": None if decisions is None else {"file": str(Path(args.decisions).expanduser()),
                                                     "sha256": decisions_sha, **decision_stats},
    }
    ctx = dict(args=args, bars=bars, bars_set=bars_set, window=window, roster_isos=roster_isos,
               n_ent=n_ent, n_high=n_high, n_review=n_review, n_exact=n_exact, n_seller=n_seller, n_eia=n_eia,
               roster_rate=roster_rate, by_iso=by_iso, coverage=coverage, scoped_share=scoped_share,
               unmatched_by_scope=unmatched_by_scope, spell_stats=spell_stats, top_spell=top_spell,
               date_rows=date_rows, date_stats=date_stats, populations=populations, usable_rate=usable_rate,
               tenor=tenor, buckets=buckets, carries_tenor=carries_tenor, ev=ev, ev_by_year=ev_by_year,
               net_signal_events=net_signal_events, net_signal_entities=net_signal_entities,
               disappeared=disappeared, van_n=van_n, van_rekey=van_rekey, top_ba=top_ba, top_loc=top_loc,
               eia_status=eia_status, eia_hist=eia_hist, snap=snap, n_contract_rows=n_contract_rows, tx=None,
               con=con,
               decision_stats=decision_stats, review_counts=review_counts, reuse_eqr=reuse_eqr,
               n_power=n_power, n_found_any=n_found_any, not_buyer_why=not_buyer_why,
               stale_terminations=stale_terminations, window_start=window_start, net_recontracted=net_recontracted,
               term_sellers=term_sellers, term_concentration=term_concentration)
    write_report(out, ctx, manifest)
    log(f"Contract-side report written ({time.time() - t_start:.0f}s).")

    # ---------------------------------------------------------------- transactions (slow step)
    if args.skip_transactions:
        log("Transactions skipped (--skip-transactions).")
    else:
        # Every raw spelling that resolves to a roster company: their trades are kept wherever delivered.
        con.execute("""
        CREATE OR REPLACE TABLE tx_buyers AS
        SELECT DISTINCT upper(trim(c.customer_company_name)) AS raw_key
        FROM contracts c JOIN eqr_resolved r ON r.target_norm = c.customer_norm
        WHERE coalesce(trim(c.customer_company_name), '') <> ''""")
        buyers = [r[0] for r in rows(con, "SELECT raw_key FROM tx_buyers ORDER BY 1")]
        tx_sig = hashlib.sha256((tx_filter_sql() + json.dumps(TX_COLS, sort_keys=True)).encode()).hexdigest()[:8]
        tx_paths = {q: snap_dir / f"core_ferceqr__transactions_{q}_{tx_sig}.parquet" for q in tx_quarters}
        # A snapshot pulled for a list of buyer spellings serves any run whose list it covers (the rows are
        # filtered again on reuse), so accepting or rejecting a match only forces a new pull when it adds a buyer.
        buyers_file = snap_dir / f"tx_buyers_{tx_sig}_{tx_quarters[0]}_{tx_quarters[-1]}.txt"
        pulled_for = set(buyers_file.read_text(encoding="utf-8").splitlines()) if buyers_file.exists() else set()
        new_buyers = set(buyers) - pulled_for
        reuse_tx = (not args.refresh and buyers_file.exists() and not new_buyers
                    and all(snapshot_usable(p, TX_COLS) for p in tx_paths.values()))
        if buyers_file.exists() and new_buyers and not args.refresh:
            log(f"Buyer spellings that resolve to roster companies since the trades were pulled: {len(new_buyers):,} "
                f"new. Pulling the trades again.")
        manifest["tx_buyer_spellings"] = len(buyers)
        ctx["tx"], tx_snap = run_transactions(con, eqr_root, tx_quarters, out, tx_paths, reuse_tx, prev_snaps)
        if not reuse_tx:
            buyers_file.write_text("\n".join(buyers) + "\n", encoding="utf-8")
        snap.update(tx_snap)
        manifest["tx_quarters"] = tx_quarters
        manifest["files"]["transactions"] = [f"{eqr_root}/core_ferceqr__transactions/{q}.parquet" for q in tx_quarters]
        manifest["missing_columns"] = {k: sorted(v) for k, v in MISSING_COLUMNS.items()}
        manifest["quantity_check"] = {k: ctx["tx"][k] for k in ("kwh_rows", "kwh_sellers", "kwh_reported", "kwh_counted",
                                                                "charge_rows", "energy_rows")}
        write_report(out, ctx, manifest)
        log(f"Report updated with transactions ({time.time() - t_start:.0f}s).")
    con.close()
    print(f"\nDone. Open {out / 'reality_check_report.md'}")


def run_transactions(con, eqr_root: str, tx_quarters: list[str], out: Path, tx_paths: dict[str, Path],
                     reuse: bool, prev_snaps: dict) -> tuple[dict, dict]:
    snap: dict = {}
    if reuse:
        log(f"Reusing the transactions slice for {', '.join(tx_quarters)} from the snapshot.")
        union = " UNION ALL ".join(f"SELECT * FROM read_parquet({sql_str(p.as_posix())})" for p in tx_paths.values())
        con.execute(f"CREATE OR REPLACE TEMP TABLE tx_raw AS SELECT * FROM ({union}) WHERE {tx_filter_sql()}")
    else:
        files_t = [(q, f"{eqr_root}/core_ferceqr__transactions/{q}.parquet") for q in tx_quarters]
        log(f"Reading EQR transactions for {', '.join(tx_quarters)} (the big table; bilateral power delivered in "
            f"PJM, MISO or NYISO, plus every roster company's purchases wherever delivered; ~14 columns). "
            f"This is the slow step...")
        con.execute(f"CREATE OR REPLACE TEMP TABLE tx_raw AS "
                    f"{select_from_files(con, 'transactions', files_t, TX_COLS, tx_filter_sql())}")
        for q, p in tx_paths.items():
            con.execute(f"COPY (SELECT * FROM tx_raw WHERE year_quarter = {sql_str(q)}) TO {sql_str(p.as_posix())} "
                        f"(FORMAT parquet, COMPRESSION zstd)")
    taken_now = dt.datetime.now().isoformat(timespec="seconds")
    for q, p in tx_paths.items():
        key = p.stem
        snap[key] = {"file": p.name, "sha256": sha256(p),
                     "rows": one(con, f"SELECT count(*) FROM read_parquet({sql_str(p.as_posix())})")[0],
                     "taken_at": (prev_snaps.get(key, {}).get("taken_at") or mtime_iso(p)) if reuse else taken_now,
                     "reused": reuse, "columns": "subset used by this check; bilateral power delivered in PJM, MISO "
                                                "or NYISO, plus roster companies' purchases wherever delivered"}
    log(f"Transaction rows kept: {one(con, 'SELECT count(*) FROM tx_raw')[0]:,}")
    extend_name_map(con, "SELECT customer_company_name AS raw_name FROM tx_raw")
    con.execute(f"""
    CREATE OR REPLACE TEMP TABLE tx AS
    SELECT t.*, m.name_norm AS customer_norm, {QK_SQL.format(c='t.year_quarter')} AS qk,
      regexp_matches(coalesce(m.name_norm, ''), {sql_str(MARKET_OPERATOR_RE)}) AS customer_is_mo,
      {scope_case('t.point_of_delivery_balancing_authority', 't.point_of_delivery_specific_location')} AS geo_scope,
      row_number() OVER (PARTITION BY t.year_quarter, t.seller_company_id_ferc,
                         coalesce(t.transaction_unique_id, 'ROW' || CAST(t.rowid AS VARCHAR))
                         ORDER BY t.trade_date NULLS LAST) AS dup_rank,
      (t.trade_date IS NOT NULL AND year(t.trade_date) >= 2000 AND t.transaction_begin_date IS NOT NULL
       AND date_diff('day', t.trade_date, t.transaction_begin_date) >= -1) AS trade_date_ok,
      date_diff('day', t.trade_date, t.transaction_begin_date) AS horizon_days
    FROM tx_raw t LEFT JOIN name_map m ON m.raw_name = t.customer_company_name""")

    con.execute("""CREATE OR REPLACE TABLE tx_only_names AS
                   SELECT DISTINCT customer_norm AS name_norm FROM tx
                   WHERE coalesce(customer_norm, '') <> '' AND customer_norm NOT IN (SELECT name_norm FROM eqr_customers)""")
    build_candidates(con, "roster_entities", "tx_only_names", "cand_tx")
    build_candidates(con, "eia_alias_names", "tx_only_names", "cand_tx_eia", fuzzy=False)
    con.execute("INSERT INTO cand_tx SELECT entity_id, target_norm, 'eia_' || method, score, tier, confidence "
                "FROM cand_tx_eia WHERE tier <= 2")
    con.execute("""
    CREATE OR REPLACE TABLE resolved_all AS
    SELECT target_norm, entity_id FROM eqr_resolved
    UNION ALL
    SELECT target_norm, entity_id FROM (
      SELECT * FROM cand_tx WHERE confidence = 'high'
      QUALIFY row_number() OVER (PARTITION BY target_norm
                                 ORDER BY tier, method LIKE 'eia_%', score DESC, entity_id) = 1)""")
    con.execute(f"""
    CREATE OR REPLACE TABLE tx_base AS
    SELECT t.*, r.entity_id, (upper(t.product_name) = 'ENERGY') AS is_energy, {kwh_sql()} AS kwh_fixed,
           CASE WHEN {kwh_sql()} THEN t.standardized_quantity / 1000 ELSE t.standardized_quantity END AS mwh
    FROM tx t LEFT JOIN resolved_all r ON r.target_norm = t.customer_norm
    WHERE t.dup_rank = 1 AND NOT coalesce(t.customer_is_mo, false)""")
    kwh = one(con, """
        SELECT count(*) FILTER (WHERE kwh_fixed), count(DISTINCT seller_company_id_ferc) FILTER (WHERE kwh_fixed),
               coalesce(sum(standardized_quantity) FILTER (WHERE kwh_fixed), 0), coalesce(sum(mwh) FILTER (WHERE kwh_fixed), 0),
               count(*) FILTER (WHERE is_energy AND total_transaction_charge IS NOT NULL), count(*) FILTER (WHERE is_energy)
        FROM tx_base""")

    rows_all, rows_dup = one(con, "SELECT count(*), count(*) FILTER (WHERE dup_rank > 1) FROM tx")
    b = one(con, """
        SELECT count(*), sum(CASE WHEN is_energy THEN mwh END),
               avg(trade_date_ok::INT),
               sum(CASE WHEN is_energy AND trade_date_ok THEN mwh END),
               sum(CASE WHEN is_energy AND upper(term_name) = 'LT' THEN mwh END),
               sum(CASE WHEN is_energy AND upper(term_name) IN ('LT', 'ST') THEN mwh END)
        FROM tx_base""")
    n_base, mwh, trade_ok_rows, mwh_trade_ok, mwh_lt, mwh_term = b
    cov = rows(con, """
        SELECT geo_scope, sum(CASE WHEN is_energy THEN mwh END),
               sum(CASE WHEN is_energy AND entity_id IS NOT NULL THEN mwh END),
               count(*), count(*) FILTER (WHERE entity_id IS NOT NULL)
        FROM tx_base GROUP BY 1 ORDER BY 1""")
    horizon = rows(con, """
        SELECT CASE WHEN horizon_days < 7 THEN '0-6 days (prompt)' WHEN horizon_days <= 31 THEN '7-31 days'
                    WHEN horizon_days <= 365 THEN '1-12 months' WHEN horizon_days <= 1095 THEN '1-3 years'
                    ELSE '> 3 years' END AS bucket,
               count(*) AS n_rows, sum(mwh) AS mwh, min(horizon_days) AS o
        FROM tx_base WHERE is_energy AND trade_date_ok GROUP BY 1 ORDER BY o""")
    copy_csv(con, """
        SELECT CASE WHEN horizon_days < 7 THEN '0-6 days (prompt)' WHEN horizon_days <= 31 THEN '7-31 days'
                    WHEN horizon_days <= 365 THEN '1-12 months' WHEN horizon_days <= 1095 THEN '1-3 years'
                    ELSE '> 3 years' END AS bucket, geo_scope, (entity_id IS NOT NULL) AS roster_linked,
               count(*) AS n_rows, round(sum(mwh)) AS mwh
        FROM tx_base WHERE is_energy AND trade_date_ok GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""",
             out / "tx_forward_horizon.csv")
    ent_any, ent_enough = one(con, f"""
        SELECT count(*), count(*) FILTER (WHERE n >= {MIN_DATED_TRADES}) FROM (
          SELECT entity_id, count(*) FILTER (WHERE is_energy AND trade_date_ok) AS n
          FROM tx_base WHERE entity_id IS NOT NULL GROUP BY 1)""")

    qks = ", ".join(str(q_key(q)) for q in tx_quarters)
    con.execute("""
    CREATE OR REPLACE TABLE tx_keys AS
    SELECT qk, seller_company_id_ferc AS seller_cid, contract_service_agreement_id AS csa_id, customer_norm,
           count(*) AS n_rows
    FROM tx_base GROUP BY 1, 2, 3, 4""")
    j = one(con, f"""
    WITH ck AS (SELECT DISTINCT qk, seller_company_id_ferc AS seller_cid, contract_service_agreement_id AS csa_id
                FROM contracts WHERE qk IN ({qks})),
         cc AS (SELECT DISTINCT qk, seller_company_id_ferc AS seller_cid, customer_norm
                FROM contracts WHERE qk IN ({qks}))
    SELECT sum(t.n_rows), sum(t.n_rows) FILTER (WHERE ck.seller_cid IS NOT NULL),
           sum(t.n_rows) FILTER (WHERE cc.seller_cid IS NOT NULL)
    FROM tx_keys t
    LEFT JOIN ck ON ck.qk = t.qk AND ck.seller_cid = t.seller_cid AND ck.csa_id = t.csa_id
    LEFT JOIN cc ON cc.qk = t.qk AND cc.seller_cid = t.seller_cid AND cc.customer_norm = t.customer_norm""")
    d = one(con, f"""
    WITH active AS (
      SELECT DISTINCT c.qk, c.contract_key, c.seller_company_id_ferc AS seller_cid,
             c.contract_service_agreement_id AS csa_id, c.customer_norm
      FROM contracts c JOIN key_flags f USING (contract_key)
      WHERE c.qk IN ({qks}) AND f.eligible AND f.roster_linked AND (f.pjm OR f.miso)),
    tk AS (SELECT DISTINCT qk, seller_cid, csa_id FROM tx_keys),
    tc AS (SELECT DISTINCT qk, seller_cid, customer_norm FROM tx_keys)
    SELECT count(*), count(*) FILTER (WHERE tk.seller_cid IS NOT NULL), count(*) FILTER (WHERE tc.seller_cid IS NOT NULL)
    FROM active a
    LEFT JOIN tk ON tk.qk = a.qk AND tk.seller_cid = a.seller_cid AND tk.csa_id = a.csa_id
    LEFT JOIN tc ON tc.qk = a.qk AND tc.seller_cid = a.seller_cid AND tc.customer_norm = a.customer_norm""")
    return dict(quarters=tx_quarters, rows_all=rows_all, rows_dup=rows_dup, n_base=n_base, mwh=mwh,
                trade_ok_rows=trade_ok_rows, mwh_trade_ok=mwh_trade_ok, mwh_lt=mwh_lt, mwh_term=mwh_term,
                cov=cov, horizon=horizon, ent_any=ent_any, ent_enough=ent_enough,
                join_rows=j[0] or 0, join_csa=j[1] or 0, join_cust=j[2] or 0,
                active=d[0] or 0, active_tx_csa=d[1] or 0, active_tx_cust=d[2] or 0,
                kwh_rows=kwh[0], kwh_sellers=kwh[1], kwh_reported=kwh[2], kwh_counted=kwh[3],
                charge_rows=kwh[4], energy_rows=kwh[5]), snap


# =============================================================================
# 7. REPORT
# =============================================================================
def write_report(out: Path, c: dict, manifest: dict) -> None:
    bars, cov, tx = c["bars"], c["coverage"], c["tx"]
    L: list[str] = []
    w = L.append
    w("# CreditMCP v0.2 — EQR reality check\n")
    w(f"Run {manifest['run_at']} · contract window {c['window'][0]}–{c['window'][-1]} "
      f"({len(c['window'])} quarters, {fi(c['n_contract_rows'])} contract rows) · transactions: "
      f"{', '.join(tx['quarters']) if tx else 'not run'} · roster ISOs: {', '.join(c['roster_isos'])}\n")
    w(f"Bars set before looking: **{'yes' if c['bars_set'] else 'NO — run with --no-bars'}**\n")

    # ---- verdicts
    w("## Verdicts\n")
    vrows = [["Roster resolution (found as a power buyer)", gate(c["roster_rate"], bars["roster_resolution"]),
              fp(c["roster_rate"]), fp(bars["roster_resolution"])]]
    tx_cov = {s: (m, mr) for s, m, mr, _n, _nr in tx["cov"]} if tx else {}
    for scope in V02_SCOPES:
        v, has = cov[scope], scope in c["roster_isos"]
        vrows.append([f"Volume coverage: {scope} contracts",
                      gate(v["rate"], bars["volume_coverage"]) if has else "NO ROSTER",
                      fp(v["rate"]), fp(bars["volume_coverage"]) if has else "not gated"])
        if scope in tx_cov:
            m, mr = tx_cov[scope]
            rate = pct(mr or 0, m or 0)
            vrows.append([f"Volume coverage: {scope} traded energy ({', '.join(tx['quarters'])})",
                          gate(rate, bars["volume_coverage"]) if has else "NO ROSTER",
                          fp(rate), fp(bars["volume_coverage"]) if has else "not gated"])
    vrows.append(["Usable dates (roster-linked contracts)", gate(c["usable_rate"], bars["usable_dates"]),
                  fp(c["usable_rate"]), fp(bars["usable_dates"])])
    w(md_table(["Gate", "Result", "Value", "Bar"], vrows) + "\n")

    w("**What the numbers say**\n")
    missing_scopes = [s for s in V02_SCOPES if s not in c["roster_isos"]]
    for s in missing_scopes:
        w(f"- **{s} roster missing.** {fi(c['unmatched_by_scope'].get(s, 0) + c['unmatched_by_scope'].get('PJM+MISO', 0))} "
          f"unmatched customers have {s}-scope contracts ({fi(cov[s]['keys'] - cov[s]['keys_resolved'])} contract keys). "
          f"Load the {s} member list as another --roster and rerun; `unmatched_inscope_customers.csv` is the checklist.")
    n_ent, n_power, n_high = c["n_ent"], c["n_power"], c["n_high"]
    w(f"- **Name resolution:** {fi(n_power)} of {fi(n_ent)} roster companies ({fp(c['roster_rate'])}) are the buyer in "
      f"at least one EQR power sale, which is the gated number. {fi(n_high - n_power)} more appear as a buyer only in "
      f"other contract types (see Q1), and {fi(c['n_found_any'])} ({fp(pct(c['n_found_any'], n_ent))}) are found "
      f"somewhere in EQR, as a buyer in any contract or as a filing seller.")
    if c["scoped_share"] is not None and c["scoped_share"] < 0.25:
        w(f"- **Location:** only {fp(c['scoped_share'])} of eligible contracts name a delivery point in PJM, MISO or "
          f"NYISO; most name none. Trades carry location, so "
          + ("the traded-energy coverage rows are the stronger test." if tx
             else "run with transactions to measure coverage on traded energy."))
    ct = c["carries_tenor"]
    if ct == ct and ct is not None:
        if ct < 0.5:
            w(f"- **Tenor:** contract dates carry exposure tenor for only {fp(ct)} of roster-linked contracts "
              f"(the rest are open-ended, evergreen or >10y). Build tenor compression on trade dates in transactions.")
        else:
            w(f"- **Tenor:** contract dates carry exposure tenor for {fp(ct)} of roster-linked contracts; "
              f"contract-level tenor compression is viable, with transactions as a refinement.")
    if tx:
        w(f"- **Trade dates:** usable on {fp(tx['trade_ok_rows'])} of the trade rows kept "
          f"({fp(pct(tx['mwh_trade_ok'] or 0, tx['mwh'] or 0))} of energy MWh). "
          f"{fi(tx['ent_enough'])} of {fi(tx['ent_any'])} trading roster entities have >= {MIN_DATED_TRADES} dated "
          f"trades this quarter, which is the per-entity tenor universe.")
    ne = c["net_signal_events"]
    w(f"- **Early termination:** {fi(ne)} early or open-ended terminations dated inside the window after netting "
      f"re-papers, across {fi(c['net_signal_entities'])} roster entities in {len(c['window'])} quarters "
      f"({fi(c['stale_terminations'])} older terminations still being listed were left out). "
      f"In {fi(c['net_recontracted'])} of them the customer signed with a different seller around the termination."
      + (" Sparse: ship it as an evidence-backed event flag, not a score input." if ne < SPARSE_EVENTS else ""))
    for y, cid, seller, n, total in c["term_concentration"]:
        w(f"- **Check {y} before using it:** {seller or cid} ({cid}) accounts for {fi(n)} of {fi(total)} net "
          f"termination events that year. A cluster under one seller usually means a novation, merger or portfolio "
          f"exit rather than customer credit; see Q4 and `early_terminations.csv`.")
    if c["disappeared"]:
        w(f"- **Silent exits:** {fi(c['van_n'])} of {fi(c['disappeared'])} roster-linked contracts that stopped "
          f"being reported did so with no termination date while the seller kept filing "
          f"(re-keyed by a name or ID change rather than ended: {fi(c['van_rekey'])}).")
    if tx:
        jr, jc = pct(tx["join_csa"], tx["join_rows"]), pct(tx["join_cust"], tx["join_rows"])
        ar, arc = pct(tx["active_tx_csa"], tx["active"]), pct(tx["active_tx_cust"], tx["active"])
        verdict = ("The join works on (seller CID, service agreement ID); dormancy is buildable."
                   if jr >= 0.8 else
                   "Seller CID + customer name joins better; use it as the fallback key."
                   if jc >= 0.8 and jc > jr else
                   "The join is weak; dormancy needs key work before it ships.")
        w(f"- **Dormancy join:** {fp(jr)} of the trade rows kept match an active contract on "
          f"(seller CID, service agreement ID), {fp(jc)} on (seller CID, customer name). {verdict} "
          f"Of active roster-linked PJM/MISO contracts, {fp(ar)} traded this quarter under their own agreement and "
          f"{fp(arc)} counting any agreement with the same seller. Measure dormancy per seller-customer "
          f"relationship: customers often trade under another agreement with the same seller.")
    else:
        w("- **Dormancy join / trade dates:** not run (--skip-transactions).")
    w("")

    # ---- Q1
    w("## Q1. Resolution coverage\n")
    w(md_table(["Roster entities", "Power buyer (gated)", "Buyer in any contract", "Exact (normalized)",
                "Review only", "Also an EQR seller (CID)", "Also an EIA utility"],
               [[fi(c["n_ent"]), f"{fi(c['n_power'])} ({fp(c['roster_rate'])})",
                 f"{fi(c['n_high'])} ({fp(pct(c['n_high'], c['n_ent']))})", fi(c["n_exact"]),
                 fi(c["n_review"]), fi(c["n_seller"]), fi(c["n_eia"])]]) + "\n")
    w(md_table(["Roster ISO", "Entities", "Power buyers", "Rate", "Buyer in any contract"],
               [[iso, fi(n), fi(p), fp(pct(p, n)), fi(h)] for iso, n, p, h in c["by_iso"]]) + "\n")
    if c["not_buyer_why"]:
        w("Roster companies found as a buyer only outside power sales, by the contract type that names them "
          "(a company can appear under several):\n")
        w(md_table(["Contract type", "Roster companies"], [[k, fi(n)] for k, n in c["not_buyer_why"]]) + "\n")
    w(md_table(["Delivery scope", "Eligible contract keys", "Customer resolves", "Key coverage", "Row coverage"],
               [[s, fi(v["keys"]), fi(v["keys_resolved"]), fp(v["rate"]), fp(v["row_rate"])]
                for s, v in cov.items()]) + "\n")
    w(f"Eligible = power sales (MB/CB/NPU, no transmission products), not affiliate, not sold to an RTO/ISO. "
      f"{fp(c['scoped_share'])} of eligible contracts carry delivery info that maps to any scope; the rest can "
      f"only be scoped through the counterparty. EIA leg: {c['eia_status']}.\n")
    eh = c.get("eia_hist") or {}
    if eh.get("status") == "ok":
        w(f"EIA-861 name history: {fi(eh['links'])} roster companies tied to one EIA utility each by an exact name; "
          f"{fi(eh['aliases'])} former or spelled-out EIA names added as aliases for "
          f"{fi(eh['companies_with_aliases'])} of them; EQR customer names newly matched through them: "
          f"{fi(eh.get('new_customer_names'))}. "
          f"Purchases from EIA-861 through {eh.get('latest_purchases_year') or 'n/a'}.\n")
    else:
        w(f"EIA-861 name history: {eh.get('status', 'not run')}.\n")
    if tx:
        w(md_table(["Trade scope (" + ", ".join(tx["quarters"]) + ")", "Energy MWh", "MWh resolved", "MWh coverage",
                    "Rows", "Row coverage"],
                   [[s or "Elsewhere (roster buyers only)", fi(m or 0), fi(mr or 0), fp(pct(mr or 0, m or 0)), fi(n),
                     fp(pct(nr, n))]
                    for s, m, mr, n, nr in tx["cov"]]) + "\n")
    med, mx, n3 = c["spell_stats"] or (None, None, None)
    w(f"**The spelling problem, measured:** resolved entities appear under a median of {f'{med:g}' if med is not None else 'n/a'} "
      f"raw spellings (max {mx if mx is not None else 'n/a'}); {fi(n3)} appear under 3 or more. "
      f"Top offenders (full list in `spelling_variants.csv`):\n")
    w(md_table(["Entity", "Raw spellings", "Examples"], [[a, n, e] for a, n, e in c["top_spell"]]) + "\n")
    rc = c.get("review_counts") or {}
    ds = c.get("decision_stats")
    w(f"**Match decisions.** `review_queue.csv` lists the fuzzy matches that can change a result "
      f"(needing a decision: {fi(rc.get('open', 0))}; auto-accepted at {FUZZY_HIGH} or above: {fi(rc.get('auto', 0))}"
      + (f"; already decided by you: {fi(rc.get('decided', 0))}" if rc.get('decided') else "") + "). "
      f"{fi(rc.get('filtered', 0))} other look-alike pairs were left out because the EQR name already has an exact "
      "match or never buys power, and names with different numbers (Solar 1 vs Solar 2) are never fuzzy-matched. "
      "Put y or n in `decision_y_n`, working down from the top (sorted by power contracts), skim the auto-accepted "
      "rows (lowest scores first), and add rows for matches the script missed, such as an acronym from the top of "
      "`unmatched_inscope_customers.csv`. Then rerun with `--decisions <that file>` into the same `--out`: it reuses "
      "this run's data snapshot, so only your decisions move the numbers.\n")
    if ds:
        w(f"Decisions applied this run: {fi(ds['accepted'])} accepted, {fi(ds['rejected'])} rejected"
          + (f", {fi(ds['not_found'])} not found (roster name or EQR name absent from this run)"
             if ds["not_found"] else "") + ".\n")

    # ---- Q2
    w("## Q2. Date quality (contract level, latest report per contract)\n")
    heads = ["Check"] + [f"{p} (n={fi(c['date_stats'][p]['n'])})" for p in c["populations"]]
    w(md_table(heads, c["date_rows"]) + "\n")
    w(f"Open-ended termination dates are a valid state, not an error; they are handled in Q3. "
      f"Years >= {OPEN_ENDED_YEAR} are treated as 'no end' sentinels, years < {MIN_PLAUSIBLE_YEAR} as broken.\n")

    # ---- Q3
    w("## Q3. Where tenor lives\n")
    trows = []
    for p, (n, oe, ever, carries, lt) in c["tenor"].items():
        trows.append([p, fi(n), fp(oe), fp(ever), fp(carries), fp(lt)])
    w(md_table(["Population (usable dates)", "Contracts", "Open-ended", "Evergreen extension (of fixed-term)",
                "Dates carry tenor", "LT share"], trows) + "\n")
    w("'Dates carry tenor' = fixed term, no evergreen extension, under 10 years. Stated tenor of those "
      "fixed-term roster-linked contracts:\n")
    w(md_table(["Stated tenor", "Contracts"], [[b_, fi(n)] for b_, n, _ in c["buckets"]]) + "\n")
    if tx:
        w(f"Forward horizon at delivery (trade date to delivery start), energy, {', '.join(tx['quarters'])}. "
          f"This is how far ahead prices were locked for the energy delivered this quarter. Long horizons are mostly "
          f"long-term deals delivering now, so one quarter shows the age of the book, not current appetite for "
          f"tenor: tenor compression needs each company's mix trended across quarters.\n")
        tot = sum((m or 0) for _, _, m, _ in tx["horizon"])
        w(md_table(["Horizon", "Trade rows", "MWh", "MWh share"],
                   [[b_, fi(n), fi(m or 0), fp(pct(m or 0, tot))] for b_, n, m, _ in tx["horizon"]]) + "\n")
        w(f"LT share of energy MWh: {fp(pct(tx['mwh_lt'] or 0, tx['mwh_term'] or 0))}. "
          f"Duplicate transaction rows (same quarter, seller CID, transaction ID): "
          f"{fi(tx['rows_dup'])} of {fi(tx['rows_all'])} ({fp(pct(tx['rows_dup'], tx['rows_all']))}); deduplicated above.\n")
        w(f"Quantity check: {fi(tx['kwh_rows'])} energy rows from {fi(tx['kwh_sellers'])} sellers were priced under "
          f"${KWH_MAX_PRICE:.2f} a MWh on at least {KWH_MIN_QTY:,.0f} MWh as reported, too little for that much energy "
          f"(usually kWh entered as MWh, or an adjustment to energy billed in another row), so they count at a "
          f"thousandth: "
          f"{fi(tx['kwh_reported'])} MWh as reported, "
          f"{fi(tx['kwh_counted'])} counted. All MWh figures above use the corrected quantities. Total charge is filled "
          f"in on {fp(pct(tx['charge_rows'], tx['energy_rows']))} of energy rows; rows without it can't be checked.\n")

    # ---- Q4
    w("## Q4. Signal sizing\n")
    evrows = [[k, fi(v[0]), fi(v[1])] for k, v in sorted(c["ev"].items())]
    w(md_table(["Termination class (roster-linked)", "Events", "Net of re-papers"], evrows) + "\n")
    w(md_table(["Year", "Early", "Open-ended terminated", "Net signal events"],
               [[y, fi(a), fi(b_), fi(n)] for y, a, b_, n in c["ev_by_year"]]) + "\n")
    w(f"Only terminations dated on or after {c['window_start'].isoformat()} count; "
      f"{fi(c['stale_terminations'])} older ones still listed in filings were left out. "
      f"Re-paper = a new contract between the same seller and customer executed or commencing within "
      f"-{REPAPER_DAYS_BEFORE}/+{REPAPER_DAYS_AFTER} days, or first reported within a quarter of the termination. "
      f"Every event is in `early_terminations.csv`: that file is also your backtest candidate list.\n")
    if c["term_sellers"]:
        w("Sellers behind the most net termination events (a cluster under one seller is worth checking "
          "for a novation, merger or portfolio exit before it counts as a credit signal):\n")
        w(md_table(["Seller", "CID", "Net events", "Customers", "Customer signed elsewhere", "Years"],
                   [[s or "(name not in identity table)", cid, fi(n), fi(cu), fi(el), f"{y0}" if y0 == y1 else f"{y0}–{y1}"]
                    for cid, s, n, cu, el, y0, y1 in c["term_sellers"]]) + "\n")
    if tx:
        w(f"Dormancy base: {fi(tx['active'])} active roster-linked PJM/MISO contracts in "
          f"{', '.join(tx['quarters'])}; {fi(tx['active_tx_csa'])} traded on (seller CID, service agreement ID), "
          f"{fi(tx['active_tx_cust'])} on (seller CID, customer name).\n")

    # ---- tuning + provenance
    w("## Scope tuning aids\n")
    w("Most common delivery strings on eligible contracts, with every scope the patterns assigned them to. "
      "Edit SCOPE_PATTERNS if something obvious lands in the wrong bucket or in none.\n")
    w(md_table(["Delivery BA", "Scope", "Rows"], [[a, s, fi(n)] for a, s, n in c["top_ba"]]) + "\n")
    w(md_table(["Delivery location", "Scope", "Rows"], [[a, s, fi(n)] for a, s, n in c["top_loc"]]) + "\n")
    w("## Provenance\n")
    w("PUDL keeps one EQR version in S3 and overwrites it on each build, and FERC filings get revised, so "
      "this run's inputs were snapshotted. Stamp these hashes into the envelope of anything built on them. "
      "Reruns into the same `--out` reuse these files; `--refresh` pulls fresh data.\n")
    w(md_table(["Snapshot", "Rows", "Taken", "SHA-256"],
               [[v["file"], fi(v["rows"]), str(v.get("taken_at", ""))[:16].replace("T", " ")
                 + (" (reused)" if v.get("reused") else ""), f"`{v['sha256'][:16]}…`"]
                for v in c["snap"].values()]) + "\n")
    if manifest.get("missing_columns"):
        w(f"Columns missing from some source files (filled with NULL): {manifest['missing_columns']}\n")
    w("Files: `roster_matches.csv`, `review_queue.csv`, `alias_seed.csv` (the seed for resolve_entity), "
      "`unmatched_inscope_customers.csv`, `spelling_variants.csv`, "
      "`date_quality.csv`, `early_terminations.csv`, `vanished_contracts.csv`"
      + (", `tx_forward_horizon.csv`" if tx else "") + ", `manifest.json`, `reality_check.duckdb` (query it directly).\n")

    (out / "reality_check_report.md").write_text("\n".join(L), encoding="utf-8")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
