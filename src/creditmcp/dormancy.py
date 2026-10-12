"""CreditMCP v0.2 — dormancy: trading relationships that go quiet.

A relationship is one seller and one roster company.
  - Under contract in a quarter: an eligible power-sale contract between them is reported that
    quarter and has no actual termination date before the quarter starts.
  - Traded in a quarter: EQR transactions show at least one energy trade between them that quarter,
    under any agreement. (SCOPE.md decision 4: measure per relationship, not per contract.)
    Capacity, booked-out power and requirements-service trades are shown but don't count: they come
    in lumps by design (capacity follows the June-May delivery year), so a quarter without them
    says little.

The signal: a relationship that traded energy in at least 3 of the 4 prior quarters, is still under
contract in the latest quarter, and shows no energy trades in it has gone quiet, unless the seller
kept trading energy in the latest quarter
  - under the same agreement with a new buyer name (a rename, or a spelling the name map doesn't
    know). Only agreement IDs the seller used with this one company count, so a master agreement
    shared across buyers or a placeholder ID explains nothing, and a known, unrelated company on
    the same agreement is a different counterparty (a contract handed to someone else counts as
    quiet); or
  - with an affiliate in the same PJM group (the book moved inside the corporate family).
Sellers going quiet on a counterparty while their contracts stay in force can mean they are cutting
exposure without terminating. It is evidence to check, not a verdict: seasonal buyers, hedges that
matured and contract structure also produce quiet quarters.

Flags: watch when 2+ regular sellers went quiet, they are at least half of the company's regular
sellers, and its energy volume from all EQR sellers fell by at least half; some when at least one
went quiet. The thresholds are provisional until they are backtested against public failures.

Inputs: a reality-check run with five consecutive quarters of transactions (its DuckDB file), and
resolve_entity's exact name map, so customers resolve the same way everywhere in CreditMCP.

    python -m creditmcp.dormancy build                       # writes rc_dormancy/dormancy/
    python -m creditmcp.dormancy "Southern Minnesota Municipal Power Agency"
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import statistics
from pathlib import Path

import duckdb

from creditmcp.resolve import DataError, EntityResolver
from creditmcp.schema import (
    Confidence,
    DormancyFlag,
    DormancyResult,
    Provenance,
    Relationship,
)

METHODOLOGY_VERSION = "dormancy-0.2.0"
PRIOR_N = 4                  # prior quarters the "regular" test looks back over
REGULAR_MIN = 3              # quarters with energy trades, out of PRIOR_N, for a relationship to count as regular
WATCH_MIN_QUIET = 2          # provisional flag thresholds, to be set by backtests on public failures
WATCH_MIN_SHARE = 0.5
WATCH_MIN_VOLUME_DROP = 0.5  # watch also needs the company's energy volume down by at least this share
DEFAULT_RUN = "rc_dormancy"
EXPLAINED_AGREEMENT = "same agreement, new buyer name"
EXPLAINED_AFFILIATE = "kept trading with an affiliate"
# Agreement IDs that name no agreement: never used as evidence that a buyer was renamed.
CSA_PLACEHOLDER = r"^(N/?A|NONE|NULL|TBD|UNKNOWN|NOT APPLICABLE|-+|0+|\.+)$"
CAVEATS = [
    "A quiet quarter is evidence to check, not a verdict: seasonal buyers, hedges that matured and contract "
    "structure also produce quarters with no trades. Only energy trades count; capacity, booked-out power and "
    "requirements service come in lumps by design.",
    "Trades come from FERC EQR transactions delivered in PJM, MISO and NYISO. Trades elsewhere, exchange trades "
    "and sales by sellers that don't file EQR are not seen.",
    "EQR customer names are free text typed by each seller. Trades are tied to a company only through exact "
    "known spellings, so some trades are missed rather than misassigned.",
    "PUDL marks its FERC EQR tables as experimental and rebuilds them in place, so every result is tied to the "
    "snapshot hashes in provenance.",
]
FLAG_RULE = (f"watch: {WATCH_MIN_QUIET}+ regular sellers went quiet, they are at least {WATCH_MIN_SHARE:.0%} of its "
             f"regular sellers, and its energy volume fell by at least {WATCH_MIN_VOLUME_DROP:.0%}; some: at least "
             "one went quiet (provisional thresholds)")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _volume_change(mwh_latest: float, mwh_prior_avg: float) -> float | None:
    return round(mwh_latest / mwh_prior_avg - 1, 3) if mwh_prior_avg > 0 else None


def _seller_test(regular: int, quiet: int) -> bool:
    return regular > 0 and quiet >= WATCH_MIN_QUIET and quiet / regular >= WATCH_MIN_SHARE


def _flag(regular: int, quiet: int, mwh_latest: float, mwh_prior_avg: float) -> DormancyFlag:
    if regular == 0:
        return DormancyFlag.NO_BASELINE
    change = _volume_change(mwh_latest, mwh_prior_avg)
    if _seller_test(regular, quiet) and change is not None and change <= -WATCH_MIN_VOLUME_DROP:
        return DormancyFlag.WATCH
    return DormancyFlag.SOME if quiet else DormancyFlag.NONE


# --- build -------------------------------------------------------------------
def build(db_path: str | Path, out_dir: str | Path, resolver: EntityResolver,
          run_manifest: dict | None = None) -> dict:
    """Compute relationships and per-company results from a reality-check DuckDB file."""
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
    csa = (f"CASE WHEN regexp_matches(upper(trim(t.contract_service_agreement_id)), '{CSA_PLACEHOLDER}') THEN NULL "
           f"ELSE nullif(upper(trim(t.contract_service_agreement_id)), '') END"
           if "contract_service_agreement_id" in tx_cols else "NULL::VARCHAR")

    qrows = con.execute("SELECT DISTINCT year_quarter, qk FROM rc.tx_base ORDER BY qk").fetchall()
    if len(qrows) < PRIOR_N + 1:
        raise DataError(f"Dormancy needs {PRIOR_N + 1} quarters of transactions; this run has {len(qrows)} "
                        f"({', '.join(q for q, _ in qrows)}). Rerun reality_check.py with --tx-quarters.")
    window = qrows[-(PRIOR_N + 1):]
    if window[-1][1] - window[0][1] != PRIOR_N:
        raise DataError(f"Transaction quarters must be consecutive; got {', '.join(q for q, _ in window)}.")
    labels = {k: q for q, k in window}
    latest_k, prior_ks = window[-1][1], [k for _, k in window[:-1]]
    ks = ", ".join(str(k) for k in labels)
    prior_list = ", ".join(str(k) for k in prior_ks)

    names = resolver.name_map()
    con.execute("CREATE TABLE names (alias_norm VARCHAR, entity_id VARCHAR)")
    items = list(names.items())
    for i in range(0, len(items), 1000):
        chunk = items[i:i + 1000]
        con.execute("INSERT INTO names VALUES " + ", ".join(["(?, ?)"] * len(chunk)), [v for kv in chunk for v in kv])

    active = con.execute(f"""
        SELECT DISTINCT c.qk, c.seller_company_id_ferc, n.entity_id
        FROM rc.contracts c
        JOIN rc.key_flags f USING (contract_key)
        JOIN names n ON n.alias_norm = c.customer_norm
        JOIN rc.quarters q ON q.qk = c.qk
        WHERE f.eligible AND c.qk IN ({ks}) AND c.seller_company_id_ferc IS NOT NULL
          AND (c.actual_termination_date IS NULL OR c.actual_termination_date >= q.q_start)""").fetchall()
    traded = con.execute(f"""
        SELECT t.qk, t.seller_company_id_ferc, n.entity_id,
               count(*) FILTER (WHERE coalesce(t.is_energy, false)) AS n_energy,
               count(*) FILTER (WHERE NOT coalesce(t.is_energy, false)) AS n_other,
               coalesce(sum(CASE WHEN t.is_energy THEN t.standardized_quantity END), 0) AS mwh
        FROM rc.tx_base t JOIN names n ON n.alias_norm = t.customer_norm
        WHERE t.qk IN ({ks}) AND t.seller_company_id_ferc IS NOT NULL
        GROUP BY 1, 2, 3""").fetchall()
    # Same agreement, new buyer name: agreement IDs a seller used in the prior quarters with one roster
    # company and no other buyer name, that carry energy trades in the latest quarter with a buyer name
    # the name map doesn't know (a rename) or an affiliate's name. IDs shared across buyers (a master
    # agreement) and placeholder IDs never qualify; a known, unrelated company on the same agreement is
    # a different counterparty, so it explains nothing.
    renamed = con.execute(f"""
        WITH x AS (
            SELECT t.qk, t.seller_company_id_ferc AS s, {csa} AS csa, n.entity_id AS e
            FROM rc.tx_base t LEFT JOIN names n ON n.alias_norm = t.customer_norm
            WHERE coalesce(t.is_energy, false) AND t.qk IN ({ks}) AND t.seller_company_id_ferc IS NOT NULL),
        own AS (
            SELECT s, csa, any_value(e) AS e FROM x
            WHERE qk IN ({prior_list}) AND csa IS NOT NULL
            GROUP BY 1, 2 HAVING count(DISTINCT e) = 1 AND count(e) = count(*))
        SELECT DISTINCT o.s, o.e, x.e FROM own o JOIN x ON x.s = o.s AND x.csa = o.csa
        WHERE x.qk = {latest_k} AND x.e IS DISTINCT FROM o.e""").fetchall()
    mapped = con.execute(f"""
        SELECT count(*), count(n.entity_id),
               coalesce(sum(CASE WHEN t.is_energy THEN t.standardized_quantity END), 0),
               coalesce(sum(CASE WHEN t.is_energy AND n.entity_id IS NOT NULL THEN t.standardized_quantity END), 0)
        FROM rc.tx_base t LEFT JOIN names n ON n.alias_norm = t.customer_norm WHERE t.qk IN ({ks})""").fetchone()
    seller_names = dict(con.execute("""
        SELECT seller_company_id_ferc, seller_company_name FROM rc.contracts WHERE seller_company_id_ferc IS NOT NULL
        QUALIFY row_number() OVER (PARTITION BY seller_company_id_ferc ORDER BY qk DESC, seller_company_name) = 1
        """).fetchall())

    under: set[tuple] = {(k, s, e) for k, s, e in active}
    trades: dict[tuple, tuple[int, int, float]] = {(k, s, e): (int(ne), int(no), float(m))
                                                    for k, s, e, ne, no, m in traded}
    energy = {key for key, v in trades.items() if v[0] > 0}
    pairs = {(s, e) for _, s, e in under} | {(s, e) for _, s, e in trades}
    group = {e: resolver.affiliate_key(e) for e in {e for _, e in pairs} | {e2 for _, _, e2 in renamed if e2}}
    same_group = lambda a, b: bool(group.get(a)) and group.get(a) == group.get(b)  # noqa: E731
    same_agreement = {(s, e) for s, e, e2 in renamed if e2 is None or same_group(e, e2)}
    latest_partners: dict[str, set[str]] = {}   # seller -> companies it traded energy with in the latest quarter
    for k, s, e in energy:
        if k == latest_k:
            latest_partners.setdefault(s, set()).add(e)

    rel_rows = []
    for s, e in sorted(pairs):
        n_prior = sum((k, s, e) in energy for k in prior_ks)
        regular = n_prior >= REGULAR_MIN
        under_latest = (latest_k, s, e) in under
        traded_latest = (latest_k, s, e) in energy
        n_latest, _, mwh_latest = trades.get((latest_k, s, e), (0, 0, 0.0))
        explained = None
        if regular and under_latest and not traded_latest:
            if (s, e) in same_agreement:
                explained = EXPLAINED_AGREEMENT
            elif any(e2 != e and same_group(e, e2) for e2 in latest_partners.get(s, ())):
                explained = EXPLAINED_AFFILIATE
        rel_rows.append({
            "entity_id": e, "seller_cid": s, "seller_name": seller_names.get(s, s),
            "quarters_traded": ";".join(labels[k] for k in sorted(labels) if (k, s, e) in energy),
            "quarters_other_products": ";".join(labels[k] for k in sorted(labels)
                                                if (k, s, e) in trades and (k, s, e) not in energy),
            "n_prior_traded": n_prior, "regular": regular,
            "under_contract_latest": under_latest, "traded_latest": traded_latest,
            "went_quiet": regular and under_latest and not traded_latest and explained is None,
            "quiet_explained": explained or "",
            "trades_latest": n_latest, "mwh_latest": round(mwh_latest, 1),
            "mwh_prior_avg": round(sum(trades.get((k, s, e), (0, 0, 0.0))[2] for k in prior_ks) / PRIOR_N, 1),
        })

    ent_rows = []
    by_ent: dict[str, list[dict]] = {}
    for r in rel_rows:
        by_ent.setdefault(r["entity_id"], []).append(r)
    for e, rels in sorted(by_ent.items()):
        regular = sum(r["regular"] and r["under_contract_latest"] for r in rels)
        quiet = sum(r["went_quiet"] for r in rels)
        mwh_l = round(sum(r["mwh_latest"] for r in rels), 1)
        mwh_p = round(sum(r["mwh_prior_avg"] for r in rels), 1)
        ent_rows.append({
            "entity_id": e, "name": resolver.company(e)["name"], "isos": "+".join(resolver.company(e)["isos"]),
            "relationships": len(rels), "regular_sellers": regular, "went_quiet": quiet,
            "explained": sum(bool(r["quiet_explained"]) for r in rels),
            "quiet_share": round(quiet / regular, 3) if regular else None,
            "flag": _flag(regular, quiet, mwh_l, mwh_p).value, "seller_test": _seller_test(regular, quiet),
            "mwh_latest": mwh_l, "mwh_prior_avg": mwh_p, "volume_change": _volume_change(mwh_l, mwh_p),
        })

    # How often relationships under contract trade energy in each quarter: the seasonal backdrop.
    per_quarter = []
    for k in sorted(labels):
        uk = [(s, e) for kk, s, e in under if kk == k]
        per_quarter.append({"quarter": labels[k], "under_contract": len(uk),
                            "traded": sum((k, s, e) in energy for s, e in uk)})

    judged = [r for r in ent_rows if r["regular_sellers"]]
    shares = sorted(r["quiet_share"] for r in judged)
    baseline = {
        "companies_with_regular_sellers": len(judged),
        "companies_any_quiet": sum(r["went_quiet"] > 0 for r in judged),
        "companies_seller_test": sum(r["seller_test"] for r in judged),
        "companies_watch": sum(r["flag"] == DormancyFlag.WATCH.value for r in judged),
        "median_quiet_share": statistics.median(shares) if shares else None,
        "p90_quiet_share": shares[int(0.9 * (len(shares) - 1))] if shares else None,
        "regular_relationships": sum(r["regular"] and r["under_contract_latest"] for r in rel_rows),
        "went_quiet_relationships": sum(r["went_quiet"] for r in rel_rows),
        "explained_relationships": sum(bool(r["quiet_explained"]) for r in rel_rows),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    rel_cols = list(rel_rows[0].keys()) if rel_rows else ["entity_id"]
    for path, rows, cols in ((out_dir / "dormancy_relationships.csv", rel_rows, rel_cols),
                             (out_dir / "dormancy_companies.csv", ent_rows,
                              list(ent_rows[0].keys()) if ent_rows else ["entity_id"])):
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(rows)

    run_manifest = run_manifest or {}
    snaps = run_manifest.get("snapshots", {})
    manifest = {
        "built_at": dt.datetime.now().isoformat(timespec="seconds"),
        "methodology_version": METHODOLOGY_VERSION,
        "latest_quarter": labels[latest_k], "prior_quarters": [labels[k] for k in prior_ks],
        "rules": {"PRIOR_N": PRIOR_N, "REGULAR_MIN": REGULAR_MIN, "WATCH_MIN_QUIET": WATCH_MIN_QUIET,
                  "WATCH_MIN_SHARE": WATCH_MIN_SHARE, "WATCH_MIN_VOLUME_DROP": WATCH_MIN_VOLUME_DROP,
                  "counted_products": ["ENERGY"], "flag_rule": FLAG_RULE,
                  "agreement_check": "contract_service_agreement_id" in tx_cols},
        "inputs": {
            "run_db": db_path.name,
            "contract_window": run_manifest.get("contract_window"),
            "snapshots": {v.get("file", k): v.get("sha256", "") for k, v in snaps.items()},
            "snapshot_taken": sorted(str(v.get("taken_at", ""))[:10] for v in snaps.values() if v.get("taken_at")),
            "pudl_release": resolver.provenance.pudl_release,
            "resolver_inputs": resolver.provenance.inputs,
            "decisions_sha256": resolver.provenance.decisions_sha256,
        },
        "coverage": {"trade_rows": mapped[0], "trade_rows_mapped": mapped[1],
                     "energy_mwh": round(mapped[2], 1), "energy_mwh_mapped": round(mapped[3], 1),
                     "names_in_map": len(names)},
        "per_quarter": per_quarter,
        "baseline": baseline,
        "counts": {"relationships": len(rel_rows), "companies": len(ent_rows)},
        "outputs": {p.name: _sha256(p) for p in (out_dir / "dormancy_relationships.csv",
                                                 out_dir / "dormancy_companies.csv")},
    }
    (out_dir / "dormancy_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out_dir / "dormancy_report.md").write_text(_report(manifest, ent_rows), encoding="utf-8")
    return manifest


def _pct(n, d) -> str:
    return f"{100 * n / d:.1f}%" if d else "n/a"


def _share(x) -> str:
    return "n/a" if x is None else f"{x:.0%}"


def _change(x) -> str:
    return "n/a" if x is None else f"{x:+.0%}"


def _company_table(rows: list[dict]) -> list[str]:
    out = ["| Company | ISOs | Regular sellers | Went quiet | Energy MWh latest | MWh prior avg | Change |",
           "|---|---|---|---|---|---|---|"]
    for e in rows[:40]:
        out.append(f"| {e['name']} | {e['isos']} | {e['regular_sellers']} | {e['went_quiet']} | "
                   f"{e['mwh_latest']:,.0f} | {e['mwh_prior_avg']:,.0f} | {_change(e['volume_change'])} |")
    return out


def _report(m: dict, ents: list[dict]) -> str:
    b, c, r = m["baseline"], m["coverage"], m["rules"]
    lines = [
        "# Dormancy build (internal)",
        "",
        f"Latest quarter {m['latest_quarter']}, compared with {', '.join(m['prior_quarters'])}. "
        f"Methodology {m['methodology_version']}; built {m['built_at']}.",
        "",
        f"- Trade rows tied to a roster company by exact known spelling: {c['trade_rows_mapped']:,} of "
        f"{c['trade_rows']:,} ({_pct(c['trade_rows_mapped'], c['trade_rows'])}); energy MWh "
        f"{_pct(c['energy_mwh_mapped'], c['energy_mwh'])}.",
        f"- Relationships (seller and roster company): {m['counts']['relationships']:,}. Regular (energy trades in "
        f"{r['REGULAR_MIN']}+ of the {r['PRIOR_N']} prior quarters) and still under contract: "
        f"{b['regular_relationships']:,}. Went quiet in {m['latest_quarter']}: {b['went_quiet_relationships']:,} "
        f"({_pct(b['went_quiet_relationships'], b['regular_relationships'])}). Stopped under one name but kept "
        f"trading under the same agreement or with an affiliate, so not counted: {b['explained_relationships']:,}.",
        f"- Companies with at least one regular seller: {b['companies_with_regular_sellers']:,}; with any seller gone "
        f"quiet: {b['companies_any_quiet']:,}; meeting the seller test: {b['companies_seller_test']:,}; flagged "
        f"watch (energy volume also down by half or more): {b['companies_watch']:,}.",
        f"- Median share of a company's regular sellers gone quiet: {_share(b['median_quiet_share'])}; "
        f"90th percentile: {_share(b['p90_quiet_share'])}.",
        f"- Flag rule: {r['flag_rule']}.",
        "",
        "## How often relationships under contract trade energy, by quarter",
        "",
        "| Quarter | Under contract | Traded energy | Share |",
        "|---|---|---|---|",
    ]
    for q in m["per_quarter"]:
        lines.append(f"| {q['quarter']} | {q['under_contract']:,} | {q['traded']:,} | "
                     f"{_pct(q['traded'], q['under_contract'])} |")
    order = lambda e: (-e["went_quiet"], -e["mwh_prior_avg"])  # noqa: E731
    watch = sorted((e for e in ents if e["flag"] == DormancyFlag.WATCH.value), key=order)
    held = sorted((e for e in ents if e["seller_test"] and e["flag"] != DormancyFlag.WATCH.value), key=order)
    lines += ["", f"## Flagged watch ({len(watch)})", "",
              "Internal review list. Not for publication: write-ups are backtests on public failures only.", ""]
    lines += _company_table(watch) if watch else ["None."]
    lines += ["", f"## Met the seller test, but energy volume held up: not flagged ({len(held)})", "",
              "Also internal only.", ""]
    lines += _company_table(held) if held else ["None."]
    return "\n".join(lines) + "\n"


# --- lookups -----------------------------------------------------------------
class DormancyIndex:
    """Per-company dormancy results from a build, looked up by name through resolve_entity."""

    def __init__(self, resolver: EntityResolver, companies: list[dict], relationships: list[dict], manifest: dict):
        built_with = manifest.get("methodology_version")
        if built_with != METHODOLOGY_VERSION:
            raise DataError(f"The dormancy build was made with {built_with or 'an unknown version'}; this code is "
                            f"{METHODOLOGY_VERSION}. Rebuild: python -m creditmcp.dormancy build")
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
        b = manifest.get("baseline", {})
        self.baseline = (
            f"Across {b.get('companies_with_regular_sellers', 0):,} roster companies with regular sellers, "
            f"{b.get('companies_any_quiet', 0):,} had at least one go quiet in {manifest.get('latest_quarter')} and "
            f"{b.get('companies_watch', 0):,} {'was' if b.get('companies_watch', 0) == 1 else 'were'} flagged watch; "
            f"{b.get('went_quiet_relationships', 0):,} of {b.get('regular_relationships', 0):,} regular "
            f"relationships went quiet.")

    @classmethod
    def from_dir(cls, home: str | Path, *, run: str = DEFAULT_RUN, resolver: EntityResolver | None = None):
        home = Path(home).expanduser()
        d = home / run / "dormancy"
        paths = [d / "dormancy_companies.csv", d / "dormancy_relationships.csv", d / "dormancy_manifest.json"]
        for p in paths:
            if not p.exists():
                raise DataError(f"Missing {p}. Run: python -m creditmcp.dormancy build")
        read = lambda p: list(csv.DictReader(open(p, newline="", encoding="utf-8")))  # noqa: E731
        return cls(resolver or EntityResolver.from_dir(home), read(paths[0]), read(paths[1]),
                   json.loads(paths[2].read_text(encoding="utf-8")))

    def lookup(self, name: str) -> DormancyResult:
        res = self.resolver.resolve(name, limit=3)
        top = res.candidates[0] if res.candidates else None
        base = dict(name=name or "", latest_quarter=self.manifest.get("latest_quarter", ""),
                    prior_quarters=self.manifest.get("prior_quarters", []), methodology_version=METHODOLOGY_VERSION,
                    as_of=self.as_of, provenance=self.provenance, caveats=CAVEATS, baseline=self.baseline)
        if top is None or top.confidence != Confidence.MATCH or res.ambiguous:
            why = ("it matches two roster companies equally well" if res.ambiguous else
                   "no roster company matches it confidently" if top else "no PJM or MISO roster company matches it")
            return DormancyResult(**base, company=top if top and not res.ambiguous else None,
                                  flag=DormancyFlag.UNRESOLVED, regular_sellers=0, went_quiet=0, quiet_share=None,
                                  mwh_latest=0.0, mwh_prior_avg=0.0, relationships=[],
                                  summary=f"No dormancy reading: {why}. Resolve the name first with "
                                          "counterparty.resolve_entity.")
        c = self._companies.get(top.entity_id)
        rank = lambda r: (r["went_quiet"] != "True", not r.get("quiet_explained"),  # noqa: E731
                          -float(r["mwh_prior_avg"] or 0), r["seller_name"])
        rels = sorted(self._rels.get(top.entity_id, []), key=rank)
        split = lambda v: [q for q in (v or "").split(";") if q]  # noqa: E731
        out_rels = [Relationship(
            seller_cid=r["seller_cid"], seller_name=r["seller_name"], quarters_traded=split(r["quarters_traded"]),
            quarters_other_products=split(r.get("quarters_other_products")), regular=r["regular"] == "True",
            under_contract_latest=r["under_contract_latest"] == "True", traded_latest=r["traded_latest"] == "True",
            went_quiet=r["went_quiet"] == "True", quiet_explained=r.get("quiet_explained") or None,
            trades_latest=int(r["trades_latest"]), mwh_latest=float(r["mwh_latest"]),
            mwh_prior_avg=float(r["mwh_prior_avg"])) for r in rels]
        regular = int(c["regular_sellers"]) if c else 0
        quiet = int(c["went_quiet"]) if c else 0
        explained = int(c.get("explained") or 0) if c else 0
        mwh_l = float(c["mwh_latest"]) if c else 0.0
        mwh_p = float(c["mwh_prior_avg"]) if c else 0.0
        flag = _flag(regular, quiet, mwh_l, mwh_p)
        change = _volume_change(mwh_l, mwh_p)
        return DormancyResult(
            **base, company=top, flag=flag, regular_sellers=regular, went_quiet=quiet, explained=explained,
            quiet_share=round(quiet / regular, 3) if regular else None, mwh_latest=mwh_l, mwh_prior_avg=mwh_p,
            volume_change=change, relationships=out_rels,
            summary=_summary(top.name, flag, regular, quiet, explained, change, out_rels,
                             self.manifest.get("latest_quarter", "")))


def _summary(name: str, flag: DormancyFlag, regular: int, quiet: int, explained: int, change: float | None,
             rels: list[Relationship], q: str) -> str:
    if flag == DormancyFlag.NO_BASELINE:
        return (f"No seller both traded energy with {name} regularly in the prior quarters and is still under "
                "contract, so there is no baseline to judge a quiet quarter against.")
    moved = ("" if not explained else
             f" {'One regular seller' if explained == 1 else f'{explained} regular sellers'} stopped trading under "
             f"this name but kept trading under the same agreement or with an affiliate, so "
             f"{'it doesn' if explained == 1 else 'they don'}'t count.")
    if flag == DormancyFlag.NONE:
        if explained:
            return f"{name}: no regular seller went quiet in {q}.{moved}"
        who = "Its one regular seller" if regular == 1 else f"All {regular} of its regular sellers"
        return f"{name}: {who} still traded energy with it in {q}."
    quiet_names = [r.seller_name for r in rels if r.went_quiet]
    shown = ", ".join(quiet_names[:3]) + (f" and {len(quiet_names) - 3} more" if len(quiet_names) > 3 else "")
    head = "Watch: " if flag == DormancyFlag.WATCH else ""
    text = (f"{head}{quiet} of {name}'s {regular} regular sellers showed no energy trades with it in {q}, although "
            f"their contracts stayed in force ({shown}).")
    if change is not None:
        moved_by = "was flat" if abs(change) < 0.005 else f"{'fell' if change < 0 else 'rose'} {abs(change):.0%}"
        text += f" Its energy volume from all EQR sellers {moved_by} against the prior-quarter average"
        text += ", so it isn't flagged watch." if flag == DormancyFlag.SOME and _seller_test(regular, quiet) else "."
    return text + moved + " Check for seasonality or matured hedges before reading it as credit stress."


# --- command line ------------------------------------------------------------
def _home(arg: str | None) -> Path:
    return Path(arg or os.environ.get("CREDITMCP_HOME") or Path(__file__).resolve().parents[2]).expanduser()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Build or look up the dormancy signal.")
    p.add_argument("args", nargs="+", help='"build", or one or more company names')
    p.add_argument("--home", help="Repo folder (default: CREDITMCP_HOME or this repo)")
    p.add_argument("--run", default=DEFAULT_RUN, help="Reality-check run folder with five quarters of trades")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    home = _home(a.home)
    if a.args == ["build"]:
        run = home / a.run
        man = json.loads((run / "manifest.json").read_text(encoding="utf-8")) if (run / "manifest.json").exists() else {}
        m = build(run / "reality_check.duckdb", run / "dormancy", EntityResolver.from_dir(home), man)
        print((run / "dormancy" / "dormancy_report.md").read_text(encoding="utf-8"))
        print(f"Wrote {run / 'dormancy'} ({m['counts']['relationships']:,} relationships).")
        return 0
    idx = DormancyIndex.from_dir(home, run=a.run)
    for name in a.args:
        r = idx.lookup(name)
        if a.json:
            print(r.model_dump_json(indent=2))
            continue
        print(f"\n{name}  ->  {r.company.name if r.company else '-'}  [{r.flag.value}]")
        for rel in r.relationships[:12]:
            mark = ("QUIET" if rel.went_quiet else "moved" if rel.quiet_explained else
                    "trading" if rel.traded_latest else "-")
            note = f"  ({rel.quiet_explained})" if rel.quiet_explained else ""
            print(f"  {mark:<8} {rel.seller_name[:40]:<40} energy: {', '.join(rel.quarters_traded) or 'none'}{note}")
        print(f"  {r.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
