"""CreditMCP v0.2 MCP server.

Two tools over stdio: `counterparty.market_signals` (what does the market's trading record show
about this company?) and `counterparty.resolve_entity` (which company is this name?). Both read
local files built from a reality-check run; nothing calls the network at request time.

Like v0.1, it raises when its data is missing rather than returning a guess. An empty candidate
list is a real answer (the name is not on the PJM or MISO roster), not a failure.

Run locally:
    CREDITMCP_HOME=/path/to/creditmcp python -m creditmcp.server     # speaks MCP over stdio
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from creditmcp.resolve import EntityResolver
from creditmcp.schema import MarketSignalsResult, ResolveResult
from creditmcp.signals import DEFAULT_RUN, SignalsIndex

log = logging.getLogger("creditmcp.server")

mcp = FastMCP(
    "creditmcp",
    instructions=(
        "CreditMCP reads the wholesale power trading record that sellers file with FERC (Electric Quarterly "
        "Reports) and turns it into market signals on energy counterparties in PJM and MISO, most of them "
        "private. Use counterparty.market_signals for any question about how a company trades or what the "
        "market shows about it: whether it is buying more or less power than a year ago, who it trades with, "
        "which sellers are new, which regular sellers kept trading or stopped, how concentrated its supply is, "
        "how it compares with the market, and whether trading partners are pulling back. Use "
        "counterparty.resolve_entity when a name needs pinning down first, or for identifiers: which company a "
        "name is, its FERC and EIA IDs, and its PJM affiliate group. Both work from names; market_signals "
        "resolves the name itself."
    ),
)


def _home() -> Path:
    env = os.environ.get("CREDITMCP_HOME")
    return Path(env).expanduser() if env else Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def _resolver() -> EntityResolver:
    return EntityResolver.from_dir(_home())


@lru_cache(maxsize=1)
def _signals() -> SignalsIndex:
    run = os.environ.get("CREDITMCP_TRADES_RUN", DEFAULT_RUN)
    return SignalsIndex.from_dir(_home(), run=run, resolver=_resolver())


@mcp.tool(name="counterparty.market_signals")
def market_signals(name: str) -> MarketSignalsResult:
    """Market signals on an energy company in PJM and MISO: what its wholesale power trading shows, up or down.

    Use this for questions about how a company trades or what the market's trading record shows about
    it: is it buying more or less power than last year; who sells to it and who its biggest suppliers
    are; which sellers are new, which regular sellers kept trading and which stopped; whether trading
    partners are pulling back or cutting exposure; how concentrated its supply is; and how it compares
    with the market. It reads FERC Electric Quarterly Reports, where sellers report every bilateral
    wholesale power sale. It describes the company's trading, not whether it is registered or active.

    The latest quarter is compared with the same quarter last year (so seasonal buyers read fairly)
    and with the four quarters before it. Returns:
      - in_short: one line, e.g. "Buying more, from more sellers."
      - volume: energy MWh bought by quarter, the change, a trend (up, steady, down, started, none;
        up or down means 25% or more) and the market median change.
      - sellers: how many traded energy with it now and a year earlier, how many are new, and what
        each regular seller (energy trades in 3 of the 4 prior quarters) did: kept trading, stopped
        while still under contract, or reached the end of its contract; plus the largest seller's share.
      - early_warning: met when 2 or more regular sellers under contract stopped, they are at least
        half of them, and volume fell by half or more. Provisional thresholds.
      - every seller relationship with its status and volume by quarter, a market-wide comparison,
        a plain-language summary, provenance and caveats.
      - quantity_fixes: seller reports that can't be real sales at the size reported: kWh entered in
        EQR's MWh field, or adjustment rows that repeat a month's MWh. Those count at a thousandth, and
        the summary names them.
      - placeholder_bills: sellers that reported their sales to it as monthly bills with a placeholder
        quantity of a few MWh. That energy can't be counted. placeholder_billing_share sizes those
        bills against everything EQR sellers billed it for energy, and the summary says whether they
        make its volume and coverage read low.
      - coverage: how much of the company's buying EQR shows, comparing the energy here over the last
        four quarters with the wholesale purchases it reported to EIA (Form 861), as a level (high,
        partial, low, unknown), a share and a sentence. When EQR shows under a quarter of its buying,
        in_short leads with that: read those signals as a slice of the company's buying.

    The name is resolved first; an ambiguous or unknown name returns status "unresolved" rather than a
    guess. Renamed buyers and moves within a PJM affiliate group are not counted as new or stopped
    sellers. A change in trading is evidence to check (seasonality, matured hedges, purchases moved to
    the ISO markets), not a verdict.
    """
    try:
        return _signals().lookup(name)
    except Exception as exc:
        log.exception("market_signals(%r) failed", name)
        raise RuntimeError(
            f"CreditMCP could not read market signals for '{name}': {type(exc).__name__}: {exc}. This is a data "
            "or setup failure, not a judgment about the company."
        ) from exc


@mcp.tool(name="counterparty.resolve_entity")
def resolve_entity(name: str, limit: int = 5) -> ResolveResult:
    """Resolve an energy counterparty's name to the company behind it, for PJM and MISO.

    Use this when a counterparty name comes from a contract, a filing or a conversation and you
    need to know which company it is, or need its identifiers. EQR names are typed freely by each
    seller, so one company appears under many spellings ("Tenaska Power Service Co.", "TENASKA POWER
    SERVICES").

    Returns up to `limit` roster companies (default 5), best first. Each has a confidence
    ("match" or "possible"), how it matched and its score, its FERC company IDs, EIA and PUDL
    utility IDs, its PJM affiliate group, and the EQR evidence behind it. `ambiguous` is true when
    two different companies match equally well. Every result carries provenance: the data
    snapshot hashes, contract window and PUDL release it was built from.

    An empty list means the name is not on the PJM or MISO roster. Raises if the resolver's data
    files are missing, rather than guessing.
    """
    try:
        return _resolver().resolve(name, limit=limit)
    except Exception as exc:
        log.exception("resolve_entity(%r) failed", name)
        raise RuntimeError(
            f"CreditMCP could not resolve '{name}': {type(exc).__name__}: {exc}. This is a data or "
            "setup failure, not a judgment about the company."
        ) from exc


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
