# Pete Timezone Rule

**Critical: Australia is 1 day ahead of the US.**

## The Problem
- Today in Australia = Yesterday in the US (NBA slate)
- Tank01 API returns US dates
- Pete's data pipeline must account for this

## The Fix
When running Pete for today's date (AU):
- Use `--date` as AU date (2026-03-04)
- Fetch Tank01 data for US date (2026-03-03)
- OR use cached data from the previous US date

## Example
- AU Date: March 4, 2026
- US Date: March 3, 2026
- Tank01 API: `gameDate=20260303`

## Cron Rule
The 9am AEST cron should:
1. Map AU date → US date (subtract 1 day)
2. Fetch Tank01 props/odds for US date
3. Use Draftstars CSV for current day's slate

## Files
- Tank01 data: `data-lake/nba/betting-props/{US_DATE}.json`
- Draftstars CSV: `INPUTS/draftstars-{AU_DATE}.csv`

---
*Added: 2026-03-04*
