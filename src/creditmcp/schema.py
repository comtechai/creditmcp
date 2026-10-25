"""CreditMCP v0.2 — the I/O contracts for counterparty.market_signals and counterparty.resolve_entity.

These are the locked return shapes, in the style of v0.1's HealthResult: the query as sent, a
plain-language summary, a methodology version and an as-of date, plus what v0.2 adds — provenance
tying every answer to the data snapshot it came from, and standing caveats about the data.

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
    EIA_NAME_HISTORY = "eia_name_history"      # a name its EIA utility filed Form 861 under, any year, as filed and
                                               # with abbreviations spelled out ("El Pwr Assn")


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


# --- market signals -------------------------------------------------------------
class ResultStatus(str, Enum):
    OK = "ok"                      # resolved to one roster company, with EQR contracts or trades in the window
    NO_ACTIVITY = "no_activity"    # resolved, but no EQR seller has contracts or trades with it in the window
    UNRESOLVED = "unresolved"      # the name did not resolve to exactly one roster company


class VolumeTrend(str, Enum):
    UP = "up"                      # energy bought rose 25% or more against the comparison basis
    STEADY = "steady"              # within 25% either way
    DOWN = "down"                  # fell 25% or more
    STARTED = "started"            # bought energy in the latest quarter after none in the four before it
    NONE = "none"                  # no energy bought from EQR sellers in the window


class SellerTrend(str, Enum):
    BROADER = "broader"            # trading energy with at least 2 more sellers, and 25% more, than a year earlier
    STEADY = "steady"              # about the same number of sellers
    NARROWER = "narrower"          # at least 2 fewer sellers, and 25% fewer
    NONE = "none"                  # no seller traded energy with it in either quarter


class SellerStatus(str, Enum):
    KEPT_TRADING = "kept_trading"                # regular seller, traded energy with it in the latest quarter
    NEW = "new"                                  # traded energy with it in the latest quarter, none in the prior year
    OCCASIONAL = "occasional"                    # traded energy with it in the latest quarter; before, not regularly
    CARRIED_OVER = "carried_over"                # would be new, but already traded with an affiliate in its PJM group
    STOPPED = "stopped"                          # regular, contract in force, no trades of any kind in latest quarter
    STOPPED_NOT_COUNTED = "stopped_not_counted"  # stopped selling it energy, explained (see reason)
    CONTRACT_ENDED = "contract_ended"            # regular, no energy trades in the latest quarter, no contract in force
    QUIET = "quiet"                              # not a regular seller, and no energy trades in the latest quarter


class SellerRelationship(BaseModel):
    seller_cid: str = Field(..., description="The seller's FERC company ID")
    seller_name: str = Field(..., description="The seller's latest EQR filer name")
    status: SellerStatus = Field(..., description="What this seller did with the company in the latest quarter")
    reason: str | None = Field(
        None, description="For stopped_not_counted: it still trades other products with the company, kept trading "
                          "under the same agreement with a new buyer name, or kept trading with an affiliate. For "
                          "carried_over: it already traded with an affiliate in the company's PJM group")
    regular: bool = Field(..., description="Traded energy with the company in at least 3 of the 4 prior quarters")
    under_contract_latest: bool = Field(..., description="An eligible power-sale contract was in force in the latest "
                                                         "quarter")
    quarters_traded: list[str] = Field(..., description="Quarters in the window with at least one energy trade")
    quarters_other_products: list[str] = Field(
        default_factory=list,
        description="Quarters with only capacity, booked-out power or requirements-service trades")
    other_buyer_names: list[str] = Field(
        default_factory=list,
        description="Buyer names this seller used on the same agreement that are counted as this company's (a "
                    "rename or a spelling the name map doesn't know)")
    trades_latest: int = Field(..., description="Energy trades in the latest quarter")
    mwh_by_quarter: dict[str, float] = Field(default_factory=dict, description="Energy MWh in each quarter")
    mwh_latest: float
    mwh_same_quarter_last_year: float = 0.0
    share_latest: float | None = Field(None, description="This seller's share of the company's energy MWh in the "
                                                         "latest quarter")


class VolumeSignal(BaseModel):
    mwh_by_quarter: dict[str, float] = Field(default_factory=dict,
                                             description="Energy MWh bought from all EQR sellers, by quarter")
    mwh_latest: float
    mwh_same_quarter_last_year: float
    mwh_prior_avg: float = Field(..., description="Average energy MWh per prior quarter")
    change: float | None = Field(None, description="Change in energy MWh against basis (0.38 = up 38%)")
    basis: str | None = Field(None, description="What change compares against: the same quarter last year, or the "
                                                "prior-quarter average when last year is empty")
    trend: VolumeTrend
    market_median_change: float | None = Field(None, description="Median change across roster companies that bought "
                                                                 "energy in both quarters")


class SellerSignal(BaseModel):
    trading_latest: int = Field(..., description="Sellers that traded energy with it in the latest quarter")
    trading_same_quarter_last_year: int = Field(..., description="The same, a year earlier")
    trend: SellerTrend
    new: int = Field(..., description="Traded energy with it in the latest quarter after no trades in the prior year")
    occasional: int = Field(..., description="Traded energy with it in the latest quarter; traded before, not "
                                             "regularly")
    carried_over: int = Field(0, description="Would be new, but already traded with an affiliate in its PJM group")
    regular: int = Field(..., description="Sellers that traded energy with it in at least 3 of the 4 prior quarters")
    kept_trading: int = Field(..., description="Regular sellers that traded energy with it in the latest quarter")
    stopped: int = Field(..., description="Regular sellers with a contract still in force but no trades of any kind "
                                          "with it in the latest quarter")
    stopped_not_counted: int = Field(0, description="Regular sellers that stopped selling it energy for a reason that "
                                                    "doesn't count as stopping (see each relationship's reason)")
    contract_ended: int = Field(0, description="Regular sellers with no energy trades and no contract in force in the "
                                               "latest quarter")
    other_products_latest: int = Field(0, description="Sellers that traded capacity, booked-out power or requirements "
                                                      "service with it in the latest quarter")
    top_seller: str | None = Field(None, description="The seller that supplied the most energy in the latest quarter")
    top_seller_share: float | None = Field(None, description="Its share of the company's energy MWh")
    top_seller_share_last_year: float | None = Field(
        None, description="The largest seller's share a year earlier (whichever seller that was)")


class EarlyWarning(BaseModel):
    met: bool = Field(..., description="True when the provisional early-warning bar is met")
    regular_under_contract: int = Field(..., description="Regular sellers with a contract in force in the latest "
                                                         "quarter")
    stopped: int = Field(..., description="How many of them stopped trading with it")
    stopped_share: float | None = Field(None, description="stopped / regular_under_contract")
    volume_change: float | None = Field(None, description="The volume change the bar uses")
    rule: str = Field(..., description="The bar, in words")


class CoverageLevel(str, Enum):
    HIGH = "high"                  # EQR shows 60% or more of the wholesale purchases it reported to EIA
    PARTIAL = "partial"            # 25% to 60%
    LOW = "low"                    # under 25%: most of its buying is out of EQR's view
    UNKNOWN = "unknown"            # nothing to compare with, or EQR shows far more than the EIA figure (see note)


class Coverage(BaseModel):
    level: CoverageLevel
    share: float | None = Field(None, description="eqr_mwh / eia_purchases_mwh (0.03 = EQR shows about 3% of what it "
                                                   "reported buying). The periods differ, so read it as a guide")
    eqr_mwh: float = Field(0.0, description="Energy MWh bought from EQR sellers over eqr_quarters")
    eqr_quarters: list[str] = Field(default_factory=list, description="The four quarters eqr_mwh covers")
    eia_year: int | None = Field(None, description="Year of the EIA-861 figures")
    eia_purchases_mwh: float | None = Field(None, description="Wholesale power purchases it reported on EIA Form 861, "
                                                              "from every source")
    eia_net_generation_mwh: float | None = Field(None, description="Its own net generation that year")
    eia_retail_sales_mwh: float | None = Field(None, description="Its retail sales that year")
    eia_sales_for_resale_mwh: float | None = Field(None, description="Its wholesale sales that year")
    eia_utility_id: int | None = Field(None, description="The EIA utility it is tied to by an exact name")
    note: str = Field(..., description="The coverage in words")


class QuantityFix(BaseModel):
    seller_name: str = Field(..., description="The seller whose report was corrected")
    quarter: str
    rows: int = Field(..., description="Energy rows read as kWh in that quarter")
    reported_mwh: float = Field(..., description="Their total as reported in EQR's MWh field")
    counted_mwh: float = Field(..., description="What CreditMCP counts: a thousandth of the reported figure")


class PlaceholderBill(BaseModel):
    seller_name: str = Field(..., description="The seller that reported the bills")
    quarters: list[str] = Field(..., description="Quarters with such bills, among the last four")
    rows: int = Field(..., description="Energy rows billed this way")
    billed_usd: float = Field(..., description="What the bills charged, as reported")


class MarketSignalsResult(BaseModel):
    """Return shape of counterparty.market_signals."""

    name: str = Field(..., description="The query as the caller sent it")
    status: ResultStatus
    company: Candidate | None = Field(None, description="The roster company the name resolved to")
    latest_quarter: str
    same_quarter_last_year: str
    prior_quarters: list[str]
    in_short: str | None = Field(None, description="One line: buying more, less or about the same, and from more, "
                                                   "fewer or the same sellers. Leads with low coverage when EQR shows "
                                                   "under a quarter of what the company buys")
    coverage: Coverage | None = Field(None, description="How much of the company's buying EQR shows: the energy in "
                                                        "these signals against the purchases it reported to EIA")
    placeholder_bills: list[PlaceholderBill] = Field(
        default_factory=list,
        description="Sellers that reported energy sales to it over the last four quarters as bills with a placeholder "
                    "quantity (a few MWh billed at over $10,000 a MWh). That energy can't be counted, so volumes and "
                    "coverage understate its buying")
    placeholder_billing_share: float | None = Field(
        None, description="Those bills as a share of everything EQR sellers billed it for energy over the same four "
                          "quarters: a guide to how far its volumes read low. Under 2% hardly changes them")
    summary: str = Field(..., description="Plain-language read of the signals")
    volume: VolumeSignal | None = None
    sellers: SellerSignal | None = None
    early_warning: EarlyWarning | None = None
    relationships: list[SellerRelationship] = Field(
        default_factory=list,
        description="Every seller relationship: those trading in the latest quarter first, by volume, then regular "
                    "sellers that stopped, then the rest")
    quantity_fixes: list[QuantityFix] = Field(
        default_factory=list,
        description="Energy reports that can't be real sales at the size reported (kWh entered in EQR's MWh field, or "
                    "adjustment rows that repeat a month's MWh), counted at a thousandth of the reported figure")
    market: str = Field(..., description="The same measures across all roster companies, for comparison")
    methodology_version: str
    as_of: str
    provenance: Provenance
    caveats: list[str]
    disclaimer: str = Field(
        "Decision support from public data. Not a credit rating. Changes in trading activity are evidence to check, "
        "not a verdict.",
        description="Non-negotiable framing.",
    )
