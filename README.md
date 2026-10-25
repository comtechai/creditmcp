# CreditMCP

**Public data. Private counterparties. Clear signals.**

Market signals on energy counterparties in the PJM and MISO power markets, from public FERC data,
served as an open MCP server. Every wholesale power seller that files with FERC reports each
bilateral sale it makes. Read from the buyer's side, those reports are a trading record: how much
power a company buys, from whom, and how that is changing. CreditMCP turns the record into signals
you can ask about in plain language, which matters most for the many counterparties that are
private and publish no financial statements.

It is **decision support, not a credit rating**. Every answer names its public sources and the
data snapshot it came from, and is meant for human review.

Built by [ComtechAI](https://comtechai.com). It follows
[Counterparty Credit](https://github.com/comtechai/counterparty-credit) (v0.1), which scores
public energy companies from SEC filings and market data.

## Status: v0.2.0

Two tools ship:

| Tool | Question it answers | Methodology |
|---|---|---|
| `counterparty.market_signals` | What does the trading record show about this company? Buying more or less than a year ago, from which sellers, who is new, who stopped, how concentrated, against the market, and how much of its buying EQR shows. | `signals-0.6.0` |
| `counterparty.resolve_entity` | Which company is this name, and what are its FERC, EIA and PUDL IDs? | `resolve-0.2.0` |

`SCOPE.md` records the v0.2 scope, frozen on Oct 11, 2026, before either tool was built, and
dated amendments since: on Oct 14 the seller signal it calls dormancy widened into two-sided
market signals, and on Oct 18 the quantity check and coverage were added. The coverage bars the
data had to clear were set on Oct 10, before the first data run.
Two more signals in that scope, tenor and early termination, are not built yet. The early-warning
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
alias seed built from FERC EQR and EIA Form 861, the hand-reviewed match decisions and FERC's
filer identity table). Market signals need five quarters of trades, which you pull once (see
below).

## Use it

### Market signals

Pull five consecutive quarters of FERC EQR trades, then build. The pull keeps power delivered in
PJM, MISO and NYISO, plus every roster company's purchases wherever they were delivered, and takes
several minutes. Reruns into the same folder reuse it, unless a rerun adds buyer names (by
accepting a match, say).

```bash
python reality_check.py --roster pjm_members.csv --roster miso_members.csv --out rc_trades \
  --decisions rc/review_queue_marked.csv --quarters 2023q3:2026q2 \
  --tx-quarters 2025q2,2025q3,2025q4,2026q1,2026q2
python -m creditmcp.signals build
python -m creditmcp.signals "Company name"
```

The build writes per-company results to `rc_trades/signals/`, which git ignores. Lookups refuse
a build made under a different methodology version, so rebuild after updating.

### Resolve a name

```bash
python -m creditmcp.cli "Tenaska Power Service Co." "Wabash Valley Power Alliance"
python -m creditmcp.cli "Hoosier Energy Rural Electric Cooperative" --json
```

### From Claude Desktop

Copy `claude_desktop_config.example.json` into your Claude Desktop config, set `command` to this
repo's `.venv` Python and `CREDITMCP_HOME` to this repo (absolute paths), then restart Claude and
ask something like *"Is [company] buying more or less power than a year ago, and from whom?"*
Claude calls `counterparty.market_signals` and reads back the trend, the sellers behind it and
its sources. Questions about which company a name is, or its IDs, go to
`counterparty.resolve_entity`.

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
- **EIA Form 861, schedule 2B** (sources and disposition of electricity), via PUDL: the name each
  utility filed under each year, and its wholesale purchases, own generation and sales.

PUDL keeps one version of its EQR tables and overwrites it on each build, and FERC filings get
revised, so every run snapshots its inputs. `rc/manifest.json` holds the SHA-256 hash of each
snapshot, and every tool result carries those hashes in its `provenance`. A fresh pull may
differ from the snapshots here; the hashes show which data a result came from.

### market_signals

EQR reports every trade, so a change in how the market trades with a company can show up before
a rating action or a headline, and for a private company there may be nothing else to go on. Each
company's latest quarter is compared with the same quarter a year earlier, so seasonal buyers read
fairly, and with the four quarters before it. A relationship is one seller and one roster company.

- **Volume:** energy MWh bought from all EQR sellers, by quarter, against the same quarter a year
  earlier (the prior-quarter average when that quarter is empty). Up or down means a change of 25%
  or more; a change of 10% to 25% reads as buying a little more or a little less. Every result
  also carries the market's median change.
- **Sellers:** how many sellers traded energy with the company in the latest quarter and a year
  earlier (broader or narrower means at least 2 and 25% more or fewer), and how many are new:
  trading with it after no trades in the prior four quarters.
- **Regular sellers:** sellers that traded energy with the company in at least 3 of the 4 prior
  quarters. In the latest quarter each one kept trading, stopped while its contract stayed in
  force, or reached the end of its contract. Only energy trades make a seller regular; capacity,
  booked-out power and requirements service come in lumps by design.
- **Concentration:** the largest seller's share of the company's energy, now and a year earlier.
- **Early warning:** 2 or more regular sellers under contract stopped trading with the company,
  they are at least half of them, and its energy volume fell by half or more. Sellers going quiet
  while their contracts stay in force can mean they are cutting credit exposure without
  terminating.
- **Coverage:** how much of the company's buying EQR shows. EQR sees only bilateral sales by
  sellers that file it, so for some companies it is most of their buying and for others a sliver.
  The energy EQR shows over the last four quarters is compared with the wholesale purchases the
  company reported on EIA Form 861 for its latest year, from every source: high (60% or more),
  partial (25% to 60%), low (under 25%) or unknown (not tied to an EIA utility, no recent figure,
  or EQR showing more than 150% of the EIA figure, as it does for some trading companies, where
  the two don't measure the same thing). The periods differ, so the share is a guide, not a
  measurement.
- **In short:** one line that puts volume and sellers together, such as "Buying more, from more
  sellers" or "Buying about the same, with a change of sellers". When coverage is low it leads:
  "EQR shows only about 3% of its power purchases. In that slice: buying about the same, from
  the same seller." The summary gives the detail.

Not counted as stopped or as new: a seller that stopped selling energy but still trades other
products with the company (it is still exposed to it); a renamed buyer; and moves within the
company's PJM affiliate group. A buyer name counts as a rename when a seller used the same
agreement ID with the company and that one other name, one after the other; its trades then count
toward the company. Agreement IDs used with two known companies, placeholder IDs and placeholder
buyer names never count as renames.

Some sellers enter kWh in EQR's MWh field, which makes a quarter look a thousand times too big,
and some bill an adjustment as a separate row that repeats a month's MWh. `reality_check.py` counts
an energy row priced under $0.50 a MWh, on at least 1,000 MWh as reported, at a thousandth when it
can't be a real sale: when it is 100,000 MWh or more, when the same seller reported that buyer in
kWh that quarter, or, in PJM, MISO or NYISO, when it is priced at least 300 times below that
seller's usual price to that buyer. Every answer names the reports it corrected. Other cheap rows
count as filed: a price that low is not proof of an error, since energy does trade near zero at
times. Some sellers report a month's sales as a bill with a placeholder quantity of a few MWh (10
MWh or less, billed at over $10,000 a MWh), and that energy can't be counted. Every answer names
the sellers that did and sizes their bills against what EQR sellers billed the company for energy:
under 2% hardly changes its volumes, and more means they read low. In the reference data (2025q2 to
2026q2), 43 rows from 5 sellers count at a thousandth, 17 other cheap rows count as filed, and 266
rows from 19 sellers are placeholder bills ($66.7 million). Other reporting errors pass through as
filed.

Trades are pulled by buyer as well as by delivery area: a co-op in MISO can buy from the utility
next door, delivered in that utility's area or with no area filled in, so every trade under a
buyer name sellers use for a roster company in their contracts is kept, wherever it was delivered.
Customers are tied to companies through `resolve_entity`'s exact name map and those agreement
links only, so a trade under an unknown spelling is missed rather than misassigned. The
reference data keeps 13.8 million trade rows (2025q2 to 2026q2), 4.4 million of them roster
companies' purchases outside PJM, MISO and NYISO. Of the energy delivered in PJM and MISO, 77% and
82% ties to a roster company. In a given quarter only about 3 in 10 seller relationships under
contract trade energy at all, which is why the seller tests rest on regular trading.

### resolve_entity

Each roster company carries aliases from six sources: its roster name, the spellings EQR sellers
use for it as a customer, its own name as an EQR filer, earlier names filed under the same FERC
company ID, its EIA utility name, and every name its EIA utility has filed Form 861 under. That
last one catches renamed companies that sellers still know by the old name. EIA abbreviates ("El
Pwr Assn"), so each EIA name is also matched with the abbreviations spelled out. A company gets
EIA names only when one of them matches it exactly and the match is one company to one utility,
and they add exact matches only, never close spellings. A query is normalized (case,
punctuation, legal suffixes, co-op shorthand, "&" and "and", a leading or trailing "The") and
matched in tiers:

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

## Limitations

These are deliberate, and stated plainly.

- **Not a credit rating.** Decision support from public data: no non-public information, no
  issuer engagement, no rating-agency methodology.
- **What EQR can't see.** EQR covers bilateral sales by sellers that file with FERC. Purchases
  from the ISO markets, exchange trades and sellers that don't file EQR (many co-ops, public
  power and federal power agencies) are invisible, so a company can look quiet in EQR while
  trading elsewhere. Each answer's coverage says how much of the company's buying that leaves
  out, where it can be measured.
- **Names are free text.** Each EQR seller types its customers' names its own way.
  `resolve_entity` reports close spellings as "possible" rather than guessing, and market signals
  use only exact known spellings and agreement-linked renames.
- **A change in trading is evidence to check, not a verdict.** Seasonal buyers, hedges that
  matured, contract structure and purchases moved to the ISO markets also move bilateral volumes,
  up or down. The thresholds are provisional.
- **Lag.** EQR is filed quarterly, about a month after each quarter ends.
- **Coverage.** PJM and MISO only, and the MISO roster is the utility proxy described above.
- **No published lists.** The repo publishes the method and the tools, not lists of named
  companies, in either direction. Write-ups are backtests on public failures.

## Layout

```
src/creditmcp/
  schema.py      # the locked response shapes (MarketSignalsResult, ResolveResult)
  names.py       # name normalization, shared with reality_check.py
  signals.py     # the market signals build and lookups
  resolve.py     # resolve_entity
  server.py      # MCP server: counterparty.market_signals, counterparty.resolve_entity
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
