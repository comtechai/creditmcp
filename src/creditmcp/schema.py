"""CreditMCP v0.2 — the counterparty.resolve_entity I/O contract.

This is the locked return shape for resolve_entity, in the style of v0.1's HealthResult:
the query as sent, a plain-language summary, a methodology version and an as-of date, plus
what v0.2 adds — provenance tying every answer to the data snapshot it came from, and
standing caveats about the data.

Design rule, carried over from v0.1: nothing unsourced. Every candidate names the roster and
the alias it matched on, and every result carries the snapshot hashes behind it.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Confidence(str, Enum):
    MATCH = "match"          # exact alias, same name without spaces, or Jaro-Winkler 0.97 or above
    POSSIBLE = "possible"    # Jaro-Winkler 0.90 to 0.97: a person should check it


class Method(str, Enum):
    EXACT = "exact"                    # same name after normalization
    EXACT_NOSPACE = "exact_nospace"    # same name once spaces are removed ("FirstEnergy" vs "First Energy")
    FUZZY = "fuzzy"                    # close spelling (Jaro-Winkler), same first letter, same numbers


class AliasSource(str, Enum):
    ROSTER = "roster"                          # the name on the PJM member list or the EIA MISO list
    EQR_CUSTOMER = "eqr_customer"              # a name EQR sellers use for this company as their customer
    EQR_SELLER = "eqr_seller"                  # the company's own name as an EQR filer
    EQR_SELLER_HISTORY = "eqr_seller_history"  # another name filed under the same FERC company ID (CID)
    EIA_UTILITY = "eia_utility"                # the company's EIA utility name


class Identifiers(BaseModel):
    ferc_cids: list[str] = Field(default_factory=list, description="FERC company IDs the company files EQR under")
    eia_utility_ids: list[int] = Field(default_factory=list, description="EIA utility IDs")
    pudl_utility_ids: list[int] = Field(default_factory=list, description="PUDL utility IDs (EIA crosswalk)")


class AffiliateGroup(BaseModel):
    name: str = Field(..., description="The PJM voting member that marks this company's corporate family")
    entity_id: str | None = Field(None, description="That voting member's roster entity id, if it is on the roster")
    is_voting_member: bool = Field(..., description="True when this company is itself the group's voting member")
    source: str = Field(..., description="Where the grouping comes from")


class Evidence(BaseModel):
    eqr_power_contracts: int = Field(0, description="EQR power contracts naming this company as the customer")
    eqr_last_quarter: str | None = Field(None, description="Latest quarter it appears as an EQR customer")
    eqr_spellings: list[str] = Field(default_factory=list, description="Raw customer spellings EQR sellers used")


class Candidate(BaseModel):
    entity_id: str = Field(..., description="Roster entity id from the reality check run")
    name: str = Field(..., description="Company name as the roster lists it")
    isos: list[str] = Field(..., description="Rosters the company is on: PJM, MISO")
    confidence: Confidence
    method: Method
    score: float = Field(..., ge=0, le=1, description="1.0 exact, 0.995 same without spaces, else Jaro-Winkler")
    matched_alias: str = Field(..., description="The normalized name the query matched")
    alias_source: AliasSource
    identifiers: Identifiers
    affiliate_group: AffiliateGroup | None = Field(None, description="PJM corporate family; None if not on PJM's list")
    evidence: Evidence
    source: str = Field(..., description="The public sources behind this candidate — required, no exceptions")


class Provenance(BaseModel):
    contract_window: str = Field(..., description="EQR contract quarters the aliases were built from")
    snapshots: dict[str, str] = Field(..., description="Snapshot file -> SHA-256")
    snapshot_taken: str = Field(..., description="When the EQR snapshot was pulled (YYYY-MM-DD)")
    pudl_release: str = Field(..., description="PUDL data release behind the snapshot")
    inputs: dict[str, str] = Field(default_factory=dict, description="Resolver input file -> SHA-256")
    decisions_sha256: str | None = Field(None, description="Hash of the match-decisions file applied, if any")


class ResolveResult(BaseModel):
    """Return shape of counterparty.resolve_entity."""

    name: str = Field(..., description="The query as the caller sent it")
    normalized: str = Field(..., description="The query after name normalization")
    candidates: list[Candidate] = Field(..., description="Up to five roster companies, best first")
    ambiguous: bool = Field(..., description="True when the top two are both matches for different companies")
    summary: str = Field(..., description="Plain-language read of the result")
    methodology_version: str = Field(..., description="Versioned resolver id, e.g. 'resolve-0.1.0'")
    as_of: str = Field(..., description="As-of date of the underlying data (YYYY-MM-DD)")
    provenance: Provenance
    caveats: list[str] = Field(..., description="Standing caveats about the data")
    disclaimer: str = Field(
        "Identity resolution from public data, for decision support. Not a credit rating. "
        "Check possible matches before relying on them.",
        description="Non-negotiable framing.",
    )


# --- dormancy -----------------------------------------------------------------
class DormancyFlag(str, Enum):
    WATCH = "watch"                # 2+ regular sellers went quiet, they are at least half of its regular sellers,
                                   # and its energy volume fell by at least half
    SOME = "some"                  # at least one regular seller went quiet
    NONE = "none"                  # every regular seller still traded energy with it in the latest quarter
    NO_BASELINE = "no_baseline"    # no seller traded energy with it regularly in the prior quarters
    UNRESOLVED = "unresolved"      # the name did not resolve to exactly one roster company


class Relationship(BaseModel):
    seller_cid: str = Field(..., description="The seller's FERC company ID")
    seller_name: str = Field(..., description="The seller's latest EQR filer name")
    quarters_traded: list[str] = Field(..., description="Quarters in the window with at least one energy trade")
    quarters_other_products: list[str] = Field(
        default_factory=list,
        description="Quarters with capacity, booked-out power or requirements-service trades only; these don't count")
    regular: bool = Field(..., description="Traded energy in at least 3 of the 4 prior quarters")
    under_contract_latest: bool = Field(..., description="An eligible power-sale contract was still in force")
    traded_latest: bool = Field(..., description="At least one energy trade in the latest quarter, under any agreement")
    went_quiet: bool = Field(..., description="Regular, still under contract, no energy trades in the latest quarter, "
                                              "and not explained by a renamed or affiliated buyer")
    quiet_explained: str | None = Field(
        None, description="Why a relationship that looked quiet doesn't count: the seller kept trading under the "
                          "same agreement with a new buyer name, or with an affiliate in the same PJM group")
    trades_latest: int = Field(..., description="Energy trades in the latest quarter")
    mwh_latest: float
    mwh_prior_avg: float = Field(..., description="Average energy MWh per prior quarter")


class DormancyResult(BaseModel):
    """Return shape of counterparty.dormancy."""

    name: str = Field(..., description="The query as the caller sent it")
    company: Candidate | None = Field(None, description="The roster company the name resolved to")
    latest_quarter: str
    prior_quarters: list[str]
    flag: DormancyFlag
    regular_sellers: int = Field(..., description="Sellers that traded energy regularly and are still under contract")
    went_quiet: int = Field(..., description="How many of them showed no energy trades in the latest quarter")
    explained: int = Field(0, description="Regular sellers that stopped under this name but kept trading under the "
                                          "same agreement with a new buyer name or with an affiliate; not counted")
    quiet_share: float | None = Field(None, description="went_quiet / regular_sellers")
    mwh_latest: float = Field(..., description="Energy MWh bought from all EQR sellers in the latest quarter")
    mwh_prior_avg: float = Field(..., description="Average energy MWh per prior quarter, all EQR sellers")
    volume_change: float | None = Field(None, description="mwh_latest / mwh_prior_avg - 1; None with no prior volume")
    relationships: list[Relationship] = Field(..., description="Every seller relationship, quiet ones first")
    baseline: str = Field(..., description="How common going quiet is across the roster, for comparison")
    summary: str
    methodology_version: str
    as_of: str
    provenance: Provenance
    caveats: list[str]
    disclaimer: str = Field(
        "Decision support from public data. Not a credit rating. A quiet quarter is evidence to check, "
        "not a verdict.",
        description="Non-negotiable framing.",
    )
