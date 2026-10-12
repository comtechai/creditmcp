"""CreditMCP v0.2 MCP server.

Two tools over stdio: `counterparty.resolve_entity` (who is this?) and `counterparty.dormancy`
(have its sellers gone quiet?). Both read local files built from a reality-check run; nothing calls
the network at request time.

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

from creditmcp.dormancy import DEFAULT_RUN, DormancyIndex
from creditmcp.resolve import EntityResolver
from creditmcp.schema import DormancyResult, ResolveResult

log = logging.getLogger("creditmcp.server")

mcp = FastMCP(
    "creditmcp",
    instructions=(
        "CreditMCP resolves energy counterparties in PJM and MISO to one company, using FERC EQR "
        "filings, EIA and PUDL identifiers, and the PJM member list. Use counterparty.resolve_entity "
        "when a counterparty name needs pinning down before any credit question: which company it "
        "is, its FERC and EIA IDs, and its PJM affiliate group. Use counterparty.dormancy to see whether "
        "the sellers that trade with it regularly have gone quiet while their contracts stay in force."
    ),
)


def _home() -> Path:
    env = os.environ.get("CREDITMCP_HOME")
    return Path(env).expanduser() if env else Path(__file__).resolve().parents[2]


@lru_cache(maxsize=1)
def _resolver() -> EntityResolver:
    return EntityResolver.from_dir(_home())


@mcp.tool(name="counterparty.resolve_entity")
def resolve_entity(name: str, limit: int = 5) -> ResolveResult:
    """Resolve an energy counterparty's name to the company behind it, for PJM and MISO.

    Use this when a counterparty name comes from a contract, a filing or a conversation and you
    need to know which company it is. EQR names are typed freely by each seller, so one company
    appears under many spellings ("Tenaska Power Service Co.", "TENASKA POWER SERVICES").

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


@lru_cache(maxsize=1)
def _dormancy() -> DormancyIndex:
    run = os.environ.get("CREDITMCP_DORMANCY_RUN", DEFAULT_RUN)
    return DormancyIndex.from_dir(_home(), run=run, resolver=_resolver())


@mcp.tool(name="counterparty.dormancy")
def dormancy(name: str) -> DormancyResult:
    """Check whether an energy counterparty's regular sellers have gone quiet, for PJM and MISO.

    A seller relationship has gone quiet when the seller traded energy with the company in at least
    3 of the 4 prior quarters, its power-sale contract is still in force, and FERC EQR shows no
    energy trades with the company in the latest quarter. A seller that kept trading under the same
    agreement with a new buyer name, or with an affiliate in the company's PJM group, doesn't count.
    Sellers going quiet while contracts stay in force can mean they are cutting exposure without
    terminating. Watch also needs the company's energy volume down by at least half.

    Returns a flag (watch, some, none, no_baseline, or unresolved), how many regular sellers went
    quiet, each seller relationship with the quarters it traded, energy volumes and their change,
    a roster-wide baseline for comparison, provenance and caveats. The name is resolved first; an
    ambiguous or unknown name returns "unresolved" rather than a guess. A quiet quarter is evidence
    to check (seasonality, matured hedges), not a verdict.
    """
    try:
        return _dormancy().lookup(name)
    except Exception as exc:
        log.exception("dormancy(%r) failed", name)
        raise RuntimeError(
            f"CreditMCP could not read dormancy for '{name}': {type(exc).__name__}: {exc}. This is a data or "
            "setup failure, not a judgment about the company."
        ) from exc


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
