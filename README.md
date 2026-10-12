# CreditMCP

An open MCP server for energy counterparty credit in the PJM and MISO power markets, built
from public FERC data. Most energy counterparties are private, so instead of reading financial
statements it reads what companies do in the wholesale market: who they buy power from, under
which contracts, and whether those sellers keep trading with them.

It is **decision support, not a credit rating**. Every answer names its public sources and the
data snapshot it came from, and is meant for human review.

Built by [ComtechAI](https://comtechai.com). It follows
[Counterparty Credit](https://github.com/comtechai/counterparty-credit) (v0.1), which scores
public energy companies from SEC filings and market data.

## Status: v0.2.0

Two tools ship:

| Tool | Question it answers | Methodology |
|---|---|---|
| `counterparty.resolve_entity` | Which company is this name, and what are its FERC, EIA and PUDL IDs? | `resolve-0.1.0` |
| `counterparty.dormancy` | Have the sellers that trade with it regularly gone quiet while their contracts stay in force? | `dormancy-0.2.0` |

`SCOPE.md` records the v0.2 scope, frozen on Oct 11, 2026, before either tool was built. The
coverage bars the data had to clear were set on Oct 10, before the first data run. Two more
signals in that scope, tenor compression and early termination, are not built yet. The dormancy
thresholds are provisional until they have been backtested against public failures.

## Install

Python 3.10 to 3.14:

```bash
git clone https://github.com/comtechai/creditmcp.git && cd creditmcp
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

`resolve_entity` works straight away: the repo includes everything it reads (the rosters, the
alias seed built from FERC EQR, the hand-reviewed match decisions and FERC's filer identity
table). Dormancy needs five quarters of trades, which you pull once (see below).

## Use it

### Resolve a name

```bash
python -m creditmcp.cli "Tenaska Power Service Co." "Wabash Valley Power Alliance"
python -m creditmcp.cli "Hoosier Energy Rural Electric Cooperative" --json
```

### Dormancy

Pull five consecutive quarters of FERC EQR trades for PJM, MISO and NYISO delivery, then build.
The pull is about 9 million rows and takes several minutes; reruns into the same folder reuse it.

```bash
python reality_check.py --roster pjm_members.csv --roster miso_members.csv --out rc_dormancy \
  --decisions rc/review_queue_marked.csv --quarters 2023q3:2026q2 \
  --tx-quarters 2025q2,2025q3,2025q4,2026q1,2026q2
python -m creditmcp.dormancy build
python -m creditmcp.dormancy "Company name"
```

The build writes per-company results to `rc_dormancy/dormancy/`, which git ignores. Lookups
refuse a build made under a different methodology version, so rebuild after updating.

### From Claude Desktop

Copy `claude_desktop_config.example.json` into your Claude Desktop config, set `command` to this
repo's `.venv` Python and `CREDITMCP_HOME` to this repo (absolute paths), then restart Claude and
ask something like *"Which company is 'Tenaska Power Service Co.' in PJM?"* Claude calls
`counterparty.resolve_entity` and reads back the company, its IDs and its sources.

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

If the data a tool needs is missing, it returns an error rather than a guess. An empty
candidate list is a real answer: the name isn't on the PJM or MISO roster.

## How it works

### Data

- **FERC Electric Quarterly Reports (EQR)**, via Catalyst Cooperative's
  [PUDL](https://catalyst.coop/pudl/): contracts, transactions and filer identity. Window
  2023q3 to 2026q2: 2,364,125 contract rows.
- **Rosters:** the [PJM member list](https://www.pjm.com/about-pjm/member-services/member-list)
  as of Oct 8, 2026 (voting and affiliate members, 1,048 rows), and utilities that report
  operating in MISO on EIA Form 861 (2025, 253 rows). The MISO list is a proxy: it covers
  utilities, not every market participant. Together: 1,270 companies.
- **EIA–PUDL utility crosswalk**, for EIA and PUDL IDs.

PUDL keeps one version of its EQR tables and overwrites it on each build, and FERC filings get
revised, so every run snapshots its inputs. `rc/manifest.json` holds the SHA-256 hash of each
snapshot, and every tool result carries those hashes in its `provenance`. A fresh pull may
differ from the snapshots here; the hashes show which data a result came from.

### resolve_entity

Each roster company carries aliases from five sources: its roster name, the spellings EQR
sellers use for it as a customer, its own name as an EQR filer, earlier names filed under the
same FERC company ID, and its EIA utility name. A query is normalized (case, punctuation, legal
suffixes, co-op shorthand, "&" and "and", a leading or trailing "The") and matched in tiers:

1. Exact alias.
2. The same once spaces are removed ("FirstEnergy" and "First Energy").
3. Close spelling (Jaro-Winkler): 0.97 or above is a match, 0.90 to 0.97 is "possible". Numbers
   must agree ("Solar 1" never matches "Solar 2"), so must state codes and initials ("PALMco
   Power PA" is not "PALMco Power VA"), and names under six characters never match on spelling.

Pairs rejected in the hand review stay rejected: 914 pairs were checked by hand, with 166
accepted and 748 rejected (`rc/review_queue_marked.csv`). Results include the PJM affiliate
group from the member list's Voting Member column, EQR evidence (power contracts, the latest
quarter, raw spellings) and a source line. When the top two candidates are both matches for
different companies, the result is flagged `ambiguous` rather than picking one.

Test set: 40 names checked by hand (`tests/golden.csv`), with the pass bar set before building.
The right company comes first for 35 of the 35 that should resolve (the bar was 32), and none
of the 5 that shouldn't resolve comes back as a match.

### dormancy

A relationship is one seller and one roster company.

- **Regular:** the seller traded energy with the company in at least 3 of the 4 prior quarters.
  Only energy trades count; capacity, booked-out power and requirements service come in lumps
  by design.
- **Went quiet:** regular, an eligible power-sale contract still in force in the latest quarter,
  and no energy trades in it, under any agreement.
- **Not counted as quiet:** the seller kept trading under the same agreement with a new buyer
  name (a rename), or with an affiliate in the company's PJM group. Agreement IDs shared across
  buyers, placeholder IDs, and an agreement taken over by an unrelated known company don't
  qualify.
- **Flags:** `watch` when 2 or more regular sellers went quiet, they are at least half of the
  company's regular sellers, and its energy volume from all EQR sellers fell by at least half;
  `some` when at least one went quiet; `none`; `no_baseline` when no seller traded energy with it
  regularly; `unresolved` when the name doesn't resolve to exactly one company.

Customers are tied to companies through `resolve_entity`'s exact name map only, so a trade
under an unknown spelling is missed rather than misassigned. Every result includes a
roster-wide baseline for comparison.

The reference build (2026q2 against 2025q2 to 2026q1) tied 89.9% of 9.3 million trade rows to a
roster company (80.0% of energy MWh). In a given quarter, only about 1 in 10 seller relationships
under contract trades energy at all, which is why the test rests on regular trading. Of 526
regular relationships, 53 (10.1%) went quiet in 2026q2.

## Limitations

These are deliberate, and stated plainly.

- **Not a credit rating.** Decision support from public data: no non-public information, no
  issuer engagement, no rating-agency methodology.
- **What EQR can't see.** EQR covers bilateral sales by sellers that file with FERC. Purchases
  from the ISO markets, exchange trades and sellers that don't file EQR are invisible, so a
  company can look quiet in EQR while trading elsewhere.
- **Names are free text.** Each EQR seller types its customers' names its own way.
  `resolve_entity` reports close spellings as "possible" rather than guessing, and dormancy
  only uses exact known spellings.
- **A quiet quarter is evidence to check, not a verdict.** Seasonal buyers, hedges that matured
  and contract structure also produce quiet quarters. The thresholds are provisional.
- **Lag.** EQR is filed quarterly, about a month after each quarter ends.
- **Coverage.** PJM and MISO only, and the MISO roster is the utility proxy described above.
- **No published watch lists.** The repo publishes the method and the tools, not lists of
  flagged companies. Write-ups are backtests on public failures.

## Layout

```
src/creditmcp/
  schema.py      # the locked response shapes (ResolveResult, DormancyResult)
  names.py       # name normalization, shared with reality_check.py
  resolve.py     # resolve_entity
  dormancy.py    # the dormancy build and lookups
  server.py      # MCP server: counterparty.resolve_entity, counterparty.dormancy
  cli.py         # command-line lookups
reality_check.py # pulls and snapshots FERC EQR via PUDL; builds the alias seed and match review
SCOPE.md         # the frozen v0.2 scope, coverage bars and decisions
rc/              # the reference run: manifest, alias seed, match decisions, identity snapshot
tests/           # offline logic tests, a name-normalization drift guard, the 40-name test set
```

## Data sources and licenses

- Code: Apache-2.0 (`LICENSE`).
- FERC EQR and EIA Form 861 data via the
  [Public Utility Data Liberation Project](https://registry.opendata.aws/catalyst-cooperative-pudl)
  (Catalyst Cooperative), licensed [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/).
  The files in `rc/` are derived from it.
- PJM member list from pjm.com, as of Oct 8, 2026.

## Disclaimer

CreditMCP produces decision support from public data. It is not a credit rating, not
investment advice, and not affiliated with FERC, PJM, MISO, EIA, Catalyst Cooperative or the
companies it covers. Outputs are sourced and intended for human review.
