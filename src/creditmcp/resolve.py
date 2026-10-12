"""CreditMCP v0.2 — resolve_entity.

counterparty name  ->  up to five roster companies, best first, each with its FERC, EIA and
PUDL identifiers, its PJM affiliate group and the EQR evidence behind the match.

Inputs, all local (nothing calls the network at request time):
  rc/alias_seed.csv     every roster company and each alias the reality check tied to it,
                        match decisions included
  rc/manifest.json      snapshot hashes, contract window and decisions hash (provenance)
  pjm_members.csv       PJM member list; its Voting Member column marks each affiliate's
                        corporate family
  rc/snapshot/core_ferceqr__quarterly_identity_*.parquet
                        optional: every name each FERC company ID has filed under, so a
                        seller's earlier names resolve too

Matching mirrors reality_check.py, so the two agree on every name: normalize the query, then
try an exact alias, then the same name with spaces removed, then Jaro-Winkler on names that
start with the same letter and carry the same numbers. 0.97 or above is a match, 0.90 to 0.97
a possible.

Like v0.1's resolver, it is injectable: EntityResolver(rows) builds from in-memory alias rows,
so the matching logic is testable without data files. EntityResolver.from_dir() loads a run.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import duckdb

from creditmcp.names import FUZZY_HIGH, FUZZY_MIN_LEN, FUZZY_REVIEW, NUM_TOKEN_RE, normalize_name
from creditmcp.schema import (
    AffiliateGroup,
    AliasSource,
    Candidate,
    Confidence,
    Evidence,
    Identifiers,
    Method,
    Provenance,
    ResolveResult,
)

METHODOLOGY_VERSION = "resolve-0.1.0"
CAVEATS = [
    "EQR customer names are free text typed by each seller, so one company appears under many "
    "spellings and some are wrong. A match is evidence of identity, not proof.",
    "PUDL marks its FERC EQR tables as experimental and rebuilds them in place, so every result is "
    "tied to the snapshot hashes in provenance.",
]
SOURCE_RANK = {s.value: i for i, s in enumerate(AliasSource)}
ROSTER_LABEL = {"PJM": "PJM member list", "MISO": "EIA-861 utilities operating in MISO"}
MAX_SPELLINGS = 8
# Tokens of one or two characters (state codes, initials) carry identity: a fuzzy match whose short
# tokens differ is capped at "possible". This is stricter than reality_check.py, on purpose. OF, US
# and CO are left out because they come and go between spellings of the same company.
SHORTS_SQL = ("list_sort(list_filter(string_split({col}, ' '), "
              "x -> length(x) <= 2 AND x NOT IN ('OF', 'US', 'CO')))")


class DataError(RuntimeError):
    """Raised when the resolver's input files are missing or unreadable."""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _int(v) -> int | None:
    try:
        return int(float(v)) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _shorts(name: str) -> list[str]:
    """Python twin of SHORTS_SQL."""
    return sorted(t for t in name.split() if len(t) <= 2 and t not in ("OF", "US", "CO"))


def _link_status(row: dict, roster_norm: dict[str, str]) -> str:
    """Trust check for one alias_seed row: "ok", "capped" or "drop".

    The reality check's own fuzzy links skip the short-token guard. Where both names carry short
    tokens and they differ ("PALMCO POWER PA" <- "PALMCO POWER MA"), the link is wrong: drop it.
    Where only one side has them ("AC ENERGY" <- "ABC ENERGY"), keep the name for matching but cap
    it at possible and keep its IDs and evidence off the company.
    """
    eid = str(row.get("entity_id"))
    if str(row.get("method") or "") != "fuzzy" or eid not in roster_norm:
        return "ok"
    a, b = _shorts(" ".join(str(row.get("alias_norm") or "").split())), _shorts(roster_norm[eid])
    if a == b:
        return "ok"
    return "drop" if a and b else "capped"


def _sql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


FIXTURE_PROVENANCE = Provenance(contract_window="n/a", snapshots={}, snapshot_taken="n/a",
                                pudl_release="n/a (in-memory rows)")


class EntityResolver:
    def __init__(self, rows: list[dict], *, groups: dict[str, AffiliateGroup] | None = None,
                 provenance: Provenance | None = None, as_of: str = "n/a",
                 rejected: set[tuple[str, str]] | None = None):
        """`rows` are alias_seed-shaped dicts: entity_id, entity_name, isos, source, alias_norm, and
        optionally method, seller_cid, utility_id_eia, utility_id_pudl, eqr_power_contracts,
        eqr_last_quarter, raw_examples. `rejected` holds (entity_id, normalized name) pairs a person
        ruled out in the match review; those never come back for that name."""
        self.provenance = provenance or FIXTURE_PROVENANCE
        self.as_of = as_of
        self._groups = groups or {}
        self._rejected = rejected or set()
        self._entities: dict[str, dict] = {}
        self.seed_dropped: list[tuple[str, str]] = []
        roster_norm = {str(r["entity_id"]): normalize_name(r.get("entity_name"))
                       for r in rows if r.get("source") == AliasSource.ROSTER.value}
        records: dict[tuple[str, str, str], tuple] = {}
        for r in rows:
            eid = str(r["entity_id"])
            ent = self._entities.setdefault(eid, {
                "name": str(r.get("entity_name") or ""), "isos": [i for i in str(r.get("isos") or "").split("+") if i],
                "cids": set(), "eia": set(), "pudl": set(), "contracts": 0, "last_q": None, "spellings": [],
            })
            src = str(r.get("source") or "")
            if src not in SOURCE_RANK:
                continue
            alias = " ".join(str(r.get("alias_norm") or "").split())
            # The reality check's own fuzzy links skip the short-token guard. Where both names carry
            # short tokens and they differ ("PALMCO POWER PA" <- "PALMCO POWER MA"), the link is wrong:
            # drop it. Where only one side has them ("AC ENERGY" <- "ABC ENERGY"), keep the name for
            # matching but cap it at possible and leave its IDs and evidence off the company.
            status = _link_status(r, roster_norm)
            if status == "drop":
                self.seed_dropped.append((eid, alias))
                continue
            capped = status == "capped"
            if alias:
                key = (eid, alias, src)
                records.setdefault(key, (eid, alias, src, alias.replace(" ", ""), SOURCE_RANK[src], capped))
            if capped:
                continue
            if r.get("seller_cid"):
                ent["cids"].add(str(r["seller_cid"]))
            if _int(r.get("utility_id_eia")) is not None:
                ent["eia"].add(_int(r["utility_id_eia"]))
            if _int(r.get("utility_id_pudl")) is not None:
                ent["pudl"].add(_int(r["utility_id_pudl"]))
            if src == AliasSource.EQR_CUSTOMER.value:
                ent["contracts"] += _int(r.get("eqr_power_contracts")) or 0
                lq = r.get("eqr_last_quarter") or None
                if lq and (ent["last_q"] is None or lq > ent["last_q"]):
                    ent["last_q"] = lq
                for sp in str(r.get("raw_examples") or "").split(" ; "):
                    sp = sp.strip()
                    if sp and sp not in ent["spellings"]:
                        ent["spellings"].append(sp)
        self._con = duckdb.connect()
        self._con.execute("CREATE TABLE aliases (entity_id VARCHAR, alias_norm VARCHAR, source VARCHAR, "
                          "ns VARCHAR, src_rank INTEGER, capped BOOLEAN)")
        recs = list(records.values())
        for i in range(0, len(recs), 500):
            chunk = recs[i:i + 500]
            self._con.execute("INSERT INTO aliases VALUES " + ", ".join(["(?, ?, ?, ?, ?, ?)"] * len(chunk)),
                              [v for rec in chunk for v in rec])
        self._con.execute("ALTER TABLE aliases ADD COLUMN nums VARCHAR[]")
        self._con.execute("ALTER TABLE aliases ADD COLUMN shorts VARCHAR[]")
        self._con.execute(f"UPDATE aliases SET nums = list_sort(regexp_extract_all(alias_norm, {_sql_str(NUM_TOKEN_RE)})), "
                          f"shorts = {SHORTS_SQL.format(col='alias_norm')}")
        self.n_aliases = len(recs)

    # --- construction ------------------------------------------------------
    @classmethod
    def from_dir(cls, home: str | Path, *, rc: str = "rc", pjm_members: str = "pjm_members.csv") -> "EntityResolver":
        """Load a reality-check run: `home` holds pjm_members.csv and the rc/ output folder."""
        home = Path(home).expanduser()
        rc_dir = home / rc
        seed, manifest_path = rc_dir / "alias_seed.csv", rc_dir / "manifest.json"
        for p in (seed, manifest_path):
            if not p.exists():
                raise DataError(f"Missing {p}. Run reality_check.py with --out {rc} first, or set CREDITMCP_HOME.")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        with open(seed, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        inputs = {"rc/alias_seed.csv": _sha256(seed), "rc/manifest.json": _sha256(manifest_path)}

        # Earlier names filed under each CID the seed already ties to a roster company.
        snap_dir = rc_dir / "snapshot"
        ident = sorted(snap_dir.glob("core_ferceqr__quarterly_identity_*.parquet")) if snap_dir.exists() else []
        if ident:
            roster_norm = {str(r["entity_id"]): normalize_name(r.get("entity_name"))
                           for r in rows if r.get("source") == AliasSource.ROSTER.value}
            cid_owner: dict[str, list[dict]] = {}
            for r in rows:
                # Only CIDs from trusted links: a dropped or capped fuzzy link must not bring its
                # company's other names along.
                if (r.get("source") == AliasSource.EQR_SELLER.value and r.get("seller_cid")
                        and _link_status(r, roster_norm) == "ok"):
                    cid_owner.setdefault(r["seller_cid"], []).append(r)
            if cid_owner:
                hist = duckdb.sql(
                    f"SELECT DISTINCT company_id_ferc, company_name FROM read_parquet({_sql_str(ident[-1].as_posix())}) "
                    "WHERE upper(coalesce(filer_unique_id, '')) LIKE 'FS%' AND company_id_ferc IS NOT NULL "
                    "AND company_name IS NOT NULL").fetchall()
                for cid, cname in hist:
                    for owner in cid_owner.get(cid, []):
                        rows.append({"entity_id": owner["entity_id"], "entity_name": owner["entity_name"],
                                     "isos": owner["isos"], "source": AliasSource.EQR_SELLER_HISTORY.value,
                                     "alias_norm": normalize_name(cname), "seller_cid": cid})
                inputs[f"rc/snapshot/{ident[-1].name}"] = manifest.get("snapshots", {}).get(
                    "core_ferceqr__quarterly_identity", {}).get("sha256") or _sha256(ident[-1])

        # Pairs a person rejected in the match review stay rejected here too.
        rejected: set[tuple[str, str]] = set()
        dec_rel = (manifest.get("decisions") or {}).get("file") or f"{rc}/review_queue_marked.csv"
        dec_path = home / dec_rel
        if dec_path.exists():
            inputs[dec_rel] = _sha256(dec_path)
            by_norm = {normalize_name(r.get("entity_name")): str(r["entity_id"])
                       for r in rows if r.get("source") == AliasSource.ROSTER.value}
            with open(dec_path, newline="", encoding="utf-8-sig") as f:
                for d in csv.DictReader(f):
                    if (d.get("decision_y_n") or "").strip().lower() in ("n", "no", "0", "false", "f", "reject"):
                        eid = by_norm.get(normalize_name(d.get("entity_name")))
                        if eid:
                            rejected.add((eid, normalize_name(d.get("eqr_customer_norm"))))

        groups: dict[str, AffiliateGroup] = {}
        pjm_path = home / pjm_members
        if pjm_path.exists():
            inputs[pjm_members] = _sha256(pjm_path)
            groups = _pjm_groups(pjm_path, rows)

        snaps = manifest.get("snapshots", {})
        taken = sorted(str(v.get("taken_at", ""))[:10] for v in snaps.values() if v.get("taken_at"))
        window = manifest.get("contract_window") or ["?", "?"]
        provenance = Provenance(
            contract_window=f"{window[0]}–{window[-1]}",
            snapshots={v.get("file", k): v.get("sha256", "") for k, v in snaps.items()},
            snapshot_taken=taken[0] if taken else "unknown",
            pudl_release=_pudl_release(manifest),
            inputs=inputs,
            decisions_sha256=(manifest.get("decisions") or {}).get("sha256"),
        )
        return cls(rows, groups=groups, provenance=provenance, as_of=taken[-1] if taken else "unknown",
                   rejected=rejected)

    # --- bulk use ----------------------------------------------------------
    def name_map(self) -> dict[str, str]:
        """Normalized name -> entity_id for every trusted alias that names exactly one company.
        Bulk joins (dormancy) use this exact map only: no close spellings, nothing capped or rejected."""
        rows = self._con.execute("""SELECT alias_norm, list(DISTINCT entity_id) FROM aliases
                                    WHERE NOT capped GROUP BY 1""").fetchall()
        out = {}
        for alias, eids in rows:
            eids = [e for e in eids if (e, alias) not in self._rejected]
            if len(eids) == 1:
                out[alias] = eids[0]
        return out

    def company(self, entity_id: str) -> dict:
        ent = self._entities[entity_id]
        return {"name": ent["name"], "isos": ent["isos"]}

    def affiliate_key(self, entity_id: str) -> str | None:
        """The company's PJM affiliate group as a comparable key; None when it is not on PJM's list."""
        g = self._groups.get(entity_id)
        return normalize_name(g.name) if g else None

    # --- resolution --------------------------------------------------------
    def resolve(self, query: str, limit: int = 5) -> ResolveResult:
        norm = normalize_name(query or "")
        limit = max(1, min(int(limit), 10))
        hits = [h for h in (self._hits(norm) if norm else []) if (h[0], norm) not in self._rejected]
        cands = [self._candidate(*h) for h in hits]
        cands.sort(key=lambda c: (c.confidence != Confidence.MATCH, -c.score,
                                  list(Method).index(c.method), -c.evidence.eqr_power_contracts, c.name))
        cands = cands[:limit]
        ambiguous = (len(cands) >= 2 and cands[0].confidence == Confidence.MATCH
                     and cands[1].confidence == Confidence.MATCH)
        return ResolveResult(name=query or "", normalized=norm, candidates=cands, ambiguous=ambiguous,
                             summary=_summary(cands, ambiguous), methodology_version=METHODOLOGY_VERSION,
                             as_of=self.as_of, provenance=self.provenance, caveats=CAVEATS)

    def _hits(self, norm: str) -> list[tuple]:
        num = _sql_str(NUM_TOKEN_RE)
        return self._con.execute(f"""
        WITH q AS (SELECT ?::VARCHAR AS qn),
             q2 AS (SELECT qn, replace(qn, ' ', '') AS qns, list_sort(regexp_extract_all(qn, {num})) AS qnums,
                           {SHORTS_SQL.format(col='qn')} AS qshorts FROM q),
             hits AS (
               SELECT a.entity_id, a.alias_norm, a.source, a.src_rank, 'exact' AS method, 1.0::DOUBLE AS score, 1 AS tier,
                      true AS shorts_agree, a.capped
               FROM aliases a, q2 WHERE a.alias_norm = q2.qn
               UNION ALL
               SELECT a.entity_id, a.alias_norm, a.source, a.src_rank, 'exact_nospace', 0.995, 2, true, a.capped
               FROM aliases a, q2 WHERE a.ns = q2.qns AND a.alias_norm <> q2.qn
               UNION ALL
               SELECT a.entity_id, a.alias_norm, a.source, a.src_rank, 'fuzzy',
                      jaro_winkler_similarity(a.alias_norm, q2.qn), 3, a.shorts = q2.qshorts, a.capped
               FROM aliases a, q2
               WHERE left(a.alias_norm, 1) = left(q2.qn, 1) AND a.ns <> q2.qns AND a.nums = q2.qnums
                 AND length(a.alias_norm) >= {FUZZY_MIN_LEN} AND length(q2.qn) >= {FUZZY_MIN_LEN}
                 AND jaro_winkler_similarity(a.alias_norm, q2.qn) >= {FUZZY_REVIEW}),
             judged AS (SELECT *, (NOT capped AND (tier < 3 OR (score >= {FUZZY_HIGH} AND shorts_agree))) AS is_match
                        FROM hits),
             best AS (SELECT *, row_number() OVER (PARTITION BY entity_id
                                                   ORDER BY is_match DESC, tier, score DESC, src_rank, alias_norm)
                              AS rn FROM judged)
        SELECT entity_id, alias_norm, source, method, score, is_match FROM best WHERE rn = 1""",
                                 [norm]).fetchall()

    def _candidate(self, eid: str, alias: str, source: str, method: str, score: float, is_match: bool) -> Candidate:
        ent = self._entities[eid]
        # A close spelling counts as a match only if the short tokens agree as well: "PALMCO POWER PA"
        # and "PALMCO POWER VA" score 0.973 but are different companies (state codes, initials).
        conf = Confidence.MATCH if is_match else Confidence.POSSIBLE
        rosters = " and ".join(ROSTER_LABEL.get(i, i) for i in ent["isos"]) or "roster"
        snap = next(iter(self.provenance.snapshots.values()), "")[:12]
        src = (f"{rosters}; matched on {AliasSource(source).value.replace('_', ' ')} name '{alias}'; "
               f"FERC EQR via PUDL, contracts {self.provenance.contract_window}"
               + (f", snapshot {snap}" if snap else ""))
        return Candidate(
            entity_id=eid, name=ent["name"], isos=ent["isos"], confidence=conf, method=Method(method),
            score=round(float(score), 4), matched_alias=alias, alias_source=AliasSource(source),
            identifiers=Identifiers(ferc_cids=sorted(ent["cids"]), eia_utility_ids=sorted(ent["eia"]),
                                    pudl_utility_ids=sorted(ent["pudl"])),
            affiliate_group=self._groups.get(eid),
            evidence=Evidence(eqr_power_contracts=ent["contracts"], eqr_last_quarter=ent["last_q"],
                              eqr_spellings=ent["spellings"][:MAX_SPELLINGS]),
            source=src)


# --- helpers ---------------------------------------------------------------
def _pjm_groups(path: Path, rows: list[dict]) -> dict[str, AffiliateGroup]:
    """entity_id -> PJM affiliate group, from the Voting Member column of the PJM member list."""
    by_norm: dict[str, str] = {}
    for r in rows:
        if r.get("source") == AliasSource.ROSTER.value:
            by_norm.setdefault(str(r.get("alias_norm") or ""), str(r["entity_id"]))
    groups: dict[str, AffiliateGroup] = {}
    with open(path, newline="", encoding="utf-8-sig") as f:
        for m in csv.DictReader(f):
            company = (m.get("Company") or "").strip()
            eid = by_norm.get(normalize_name(company))
            if not eid:
                continue
            voting = (m.get("Voting Member") or "").strip()
            is_voting = (m.get("Member Type") or "").strip().lower() == "voting member"
            # A voting member anchors its own group; an affiliate with no named voting member
            # ("Not applicable") is treated as its own group too.
            unnamed = voting.lower() in ("", "not applicable", "n/a")
            group_name = company if is_voting or unnamed else voting
            groups[eid] = AffiliateGroup(
                name=group_name, entity_id=by_norm.get(normalize_name(group_name)), is_voting_member=is_voting,
                source=f"PJM member list ({path.name}), Voting Member column")
    return groups


def _pudl_release(manifest: dict) -> str:
    eia = str((manifest.get("source") or {}).get("eia_utilities") or "")
    parts = [p for p in eia.split("/") if p]
    rel = None
    if "pudl.catalyst.coop" in parts:
        i = parts.index("pudl.catalyst.coop")
        rel = parts[i + 1] if i + 1 < len(parts) - 1 else None
    eia_part = f"EIA crosswalk from PUDL '{rel}'" if rel else "EIA crosswalk from a local file"
    return f"{eia_part}; FERC EQR from PUDL's unversioned ferceqr build, pinned by snapshot hash"


SOURCE_PHRASE = {
    "roster": "its roster name",
    "eqr_customer": "a name EQR sellers use for it",
    "eqr_seller": "its own EQR filer name",
    "eqr_seller_history": "an earlier name filed under its FERC company ID",
    "eia_utility": "its EIA utility name",
}


def _bare(name: str) -> str:
    return name[:-1] if name.endswith(".") else name


def _summary(cands: list[Candidate], ambiguous: bool) -> str:
    if not cands:
        return ("No PJM or MISO roster company matches this name. The roster covers PJM voting and affiliate "
                "members and EIA-861 utilities operating in MISO.")
    top = cands[0]
    if ambiguous:
        return (f"Two roster companies match equally well: {top.name} and {_bare(cands[1].name)}. "
                "Use the identifiers or the affiliate group to choose.")
    if top.confidence != Confidence.MATCH:
        return f"No confident match. The closest is {top.name}; check it before relying on it."
    how = {"exact": "exact match on", "exact_nospace": "same name, spacing aside, as",
           "fuzzy": f"close spelling (score {top.score:.3f}) of"}[top.method.value]
    group = ""
    if top.affiliate_group and not top.affiliate_group.is_voting_member:
        group = f" Its PJM affiliate group is {_bare(top.affiliate_group.name)}."
    return f"Matches {top.name} ({', '.join(top.isos)}): {how} {SOURCE_PHRASE[top.alias_source.value]}.{group}"
