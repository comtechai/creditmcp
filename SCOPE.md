# CreditMCP v0.2 scope (frozen Oct 11, 2026)

## Bars, set Oct 10 before the first run
Roster resolution 30% · Volume coverage 60% · Usable dates 80%

## Results
Window 2023q3–2026q2 (12 quarters, 2,364,125 contract rows) · transactions 2026q2 (2,591,766 rows)

Contracts snapshot sha256 `9e852e3805cbaf9c…` · transactions snapshot sha256 `e53ca75008193b39…`
Identity snapshot `8663e3c14295565f…` · EIA utilities snapshot `8dbe8881f041b150…`
Full hashes, and the hash of the name-review file, are in `rc/manifest.json`.

| Gate | Value | Bar | Result |
|---|---|---|---|
| Roster resolution (found as a power buyer) | 40.3% (512 of 1,270) | 30% | Pass |
| PJM contracts | 76.2% (860 of 1,129) | 60% | Pass |
| PJM traded energy, 2026q2 | 83.7% | 60% | Pass |
| MISO contracts | 63.3% (421 of 665) | 60% | Pass |
| MISO traded energy, 2026q2 | 91.7% | 60% | Pass |
| Usable dates | 99.6% | 80% | Pass |

MISO contract coverage was 58.6% before the name review and 63.3% after. The review accepted same-company names the fuzzy matcher had held back (renames, abbreviations, typos) and removed 9 wrong automatic matches. The MISO result doesn't depend on any of the review's judgment calls.

## Rosters
- PJM: the pjm.com member list (as of Oct 8, 2026), Voting and Affiliate Members only: 1,048 rows.
- MISO: utilities that report operating in MISO on EIA Form 861 (PUDL `core_eia861__yearly_utility_data_rto`, report year 2025): 253 rows. This is a proxy. It lists utilities, not every MISO market participant.
- Combined: 1,301 rows, 1,270 distinct companies.
- Name review: `rc/review_queue_marked.csv`, with 166 matches accepted and 748 rejected, and a reason on every row.

## Decisions
1. **Coverage:** PJM and MISO both ship in v0.2. Each clears every bar, so neither ships labeled "partial".
2. **Tenor source:** trade dates. Contract dates carry tenor for only 19.0% of roster-linked contracts; the rest are open-ended, evergreen or over 10 years. Trade dates are usable on 99.4% of in-scope trade rows (95.9% of MWh), and 129 of 265 trading roster companies have 20 or more dated trades in 2026q2. One quarter shows the age of a book, not its appetite for tenor, so compression is each company's mix trended across quarters.
3. **Early termination:** a candidate score input, behind a seller mass-termination filter. There are 188 net events in the window across 98 roster companies, and usable dates are at 99.6%. The filter drops a seller's events for any year in which it holds 40% or more of that year's events (10 or more events). Today that removes NRG Power Marketing (44 of 61 events in 2023) and Western Area Power Administration (39 of 57 in 2026, probably tied to the SPP RTO expansion on April 1, 2026). PSEG Energy Resources & Trade (31 events across 2024–2026, one per customer) gets checked before it counts. That leaves 105 events, or 74 without PSEG; both clear the 25-event floor.
4. **Dormancy:** stays in Weeks 3–6. The join on (seller CID, service agreement ID) matches 100% of in-scope trade rows. Measure dormancy per seller–customer relationship, not per contract: 51.7% of active roster-linked PJM/MISO contracts traded in 2026q2 under their own agreement, and 67.9% counting any agreement with the same seller.
5. **Retail supply stack:** deferred to v0.3 (decided Oct 11). It needs several quarters of trades, and this run pulled one. Retailers that buy straight from the PJM or MISO market don't appear in EQR at all, and Weeks 3–6 have no room for it. Its core, wholesale sellers going quiet and trades getting shorter, is already covered by dormancy and tenor. v0.2 keeps the retail angle by running those two signals on retail suppliers as a group, with a backtest on a public retail supplier failure if one falls inside the data window.

## Not in v0.2
- Retail supply stack as its own signal (decision 5). For v0.3 it needs four or more quarters of trades, a list of retail suppliers (EIA-861 retail power marketers) and a public retail failure to test against.
- NYISO: there's no NYISO roster yet. Contract coverage there is 47.6%, measured but not gated.
- Other ISOs and RTOs: SPP, ERCOT, CAISO and ISO-NE.
- Scoping by delivery point: only 4.0% of eligible contracts name one. Scope comes from the counterparty and from trades.
- Tenor from contract dates.
- Matching a bare group name in EQR (such as "NextEra Energy" or "Constellation") to one company. resolve_entity can map those to the affiliate group later.
- Silent exits as a signal: 972 of 2,315 roster-linked contracts stopped being reported with no termination date, and 711 of those were re-keyed by a name or ID change. This needs re-key detection first.
- Live callouts on named private companies. Write-ups are backtests on public failures.

## Amendment, Oct 14, 2026: market signals

Recorded after the dormancy tool had been built and run on real data, and before publication.
The frozen text above is unchanged.

- **Framing.** The v0.2 signals are market signals: what the EQR trading record shows about a
  counterparty, in either direction. The scope above framed every signal as deterioration
  (dormancy, tenor compression, early termination). The data reads both ways, and a one-sided
  test misread companies whose trading had shifted rather than stopped.
- **One tool.** `counterparty.market_signals` replaces the dormancy tool. For each company: energy
  volume against the same quarter last year (up, steady or down, in 25% bands), sellers trading
  now and a year earlier, new sellers, what each regular seller did (kept trading, stopped while
  still under contract, or reached the end of its contract), the largest seller's share, and the
  market's median for comparison.
- **Dormancy (decision 4) is kept** as the tool's early-warning part, with its relationship test
  unchanged: 2 or more regular sellers under contract stopped, at least half of them, and energy
  volume down by half or more. The thresholds stay provisional until the backtests.
- **Renames, both ways.** A buyer name that a seller used on the same agreement as a roster
  company, one name after the other, counts toward that company, so a rename shows up neither as
  a lost seller nor as a new one.
- **Tenor and early termination (decisions 2 and 3)** read both ways when they are built: deals
  getting longer or shorter, and contracts starting as well as ending.
- **Unchanged:** the coverage bars, the rosters, decisions 1 and 5, everything under "Not in v0.2",
  and no live callouts on named private companies, in either direction.

## Amendment, Oct 18, 2026: quantity check

Recorded after the first full market signals run on real data, before publication. The frozen text
and the Oct 14 amendment are unchanged.

- **What the run found.** One seller's 2026q2 report carried 38,193,307 MWh, about a thousand times
  its other quarters: kWh entered in EQR's MWh field. Checking it turned up a second seller that
  had scaled both the MWh and the dollars by a thousand for three quarters, then corrected itself.
- **The check.** `reality_check.py` now pulls each trade's total charge. An energy row priced under
  $0.50 a MWh, on at least 1,000 MWh as reported, can't be a full-price sale of that much energy, so
  it counts at a thousandth. In the reference run that is 4 rows from 3 sellers, out of 9.3 million:
  the kWh report above, two adjustment rows that repeat a month's MWh, and one row for a buyer
  outside the rosters. Market signals name every report they corrected. Methodology `signals-0.4.0`.
- **What it can't see.** A seller that scales both the MWh and the dollars passes the price test.
  The build report lists thousandfold jumps within a seller relationship for a check by hand. The
  one case found so far is for a buyer outside the rosters, so no company's signals use it.
- **Coverage, recomputed with the check.** 2026q2 traded energy tied to a roster company: MISO
  80.6% (91.7% before, because the 38-million-MWh row was a roster company's) and PJM 83.7%
  (unchanged). Both clear the 60% bar set on Oct 10.
