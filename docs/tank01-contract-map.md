# Tank01 Contract Map (Pete Engine)

> **Note:** Examples below are **illustrative** based on fields consumed by Pete’s pipeline. Actual Tank01 responses may include additional fields.

## 1) `getNBAPlayerList`
**Purpose:** Player index (IDs, team, name)

### Expected schema (used)
```json
{
  "body": [
    {
      "playerID": "string|int",
      "longName": "string",
      "team": "string" 
    }
  ]
}
```

### Example payload
```json
{
  "body": [
    {
      "playerID": "203076",
      "longName": "Anthony Davis",
      "team": "LAL"
    }
  ]
}
```

---

## 2) `getNBABettingOdds`
**Purpose:** Moneylines, totals, spreads (market context)

### Expected schema (used)
```json
{
  "body": [
    {
      "homeTeam": "string",
      "awayTeam": "string",
      "gameDate": "YYYYMMDD",
      "sportsBooks": [
        {
          "odds": {
            "homeTeamML": "-110",
            "awayTeamML": "+105",
            "totalOver": 232.5,
            "totalUnder": 232.5,
            "homeTeamSpread": -4.5,
            "awayTeamSpread": 4.5
          }
        }
      ]
    }
  ]
}
```

### Example payload
```json
{
  "body": [
    {
      "homeTeam": "LAL",
      "awayTeam": "BOS",
      "gameDate": "20260303",
      "sportsBooks": [
        {
          "odds": {
            "homeTeamML": "-135",
            "awayTeamML": "+120",
            "totalOver": 231.5,
            "totalUnder": 231.5,
            "homeTeamSpread": -3.5,
            "awayTeamSpread": 3.5
          }
        }
      ]
    }
  ]
}
```

---

## 3) `getNBAInjuryList`
**Purpose:** Injury status to filter lineups and flag major outs

### Expected schema (used)
```json
{
  "body": [
    {
      "longName": "string",
      "injuryStatus": "string",
      "injury": "string"
    }
  ]
}
```

### Example payload
```json
{
  "body": [
    {
      "longName": "LeBron James",
      "injuryStatus": "Questionable",
      "injury": "Ankle"
    }
  ]
}
```

---

## 4) `getNBADFS`
**Purpose:** DFS player pool (salary + position)

### Expected schema (used)
```json
{
  "body": [
    {
      "playerID": "string|int",
      "longName": "string",
      "team": "string",
      "position": "string (e.g., 'PG/SG')",
      "salary": 9300,
      "fppg": 41.2,
      "form": 43.8,
      "injuryStatus": "string",
      "startTime": "YYYY-MM-DDTHH:MM:SSZ"
    }
  ]
}
```

### Example payload
```json
{
  "body": [
    {
      "playerID": "1628369",
      "longName": "Jayson Tatum",
      "team": "BOS",
      "position": "SF/PF",
      "salary": 9800,
      "fppg": 45.1,
      "form": 47.3,
      "injuryStatus": "Available",
      "startTime": "2026-03-03T11:00:00Z"
    }
  ]
}
```

---

## 5) `getNBATeams`
**Purpose:** Team list / normalization

### Expected schema (used)
```json
{
  "body": [
    {
      "teamAbv": "string",
      "teamName": "string"
    }
  ]
}
```

### Example payload
```json
{
  "body": [
    { "teamAbv": "LAL", "teamName": "Los Angeles Lakers" }
  ]
}
```

---

## 6) `getNBAScoresOnly`
**Purpose:** Final scores for learning (bet result validation)

### Expected schema (used)
```json
{
  "body": [
    {
      "homeTeam": "string",
      "awayTeam": "string",
      "homeScore": 112,
      "awayScore": 108,
      "gameDate": "YYYYMMDD"
    }
  ]
}
```

### Example payload
```json
{
  "body": [
    {
      "homeTeam": "LAL",
      "awayTeam": "BOS",
      "homeScore": 112,
      "awayScore": 108,
      "gameDate": "20260303"
    }
  ]
}
```

---

## 7) `getNBAGamesForPlayer`
**Purpose:** Player game logs (actuals vs projected)

### Expected schema (used)
```json
{
  "body": [
    {
      "gameDate": "YYYYMMDD",
      "pts": 28,
      "reb": 9,
      "ast": 6,
      "stl": 1,
      "blk": 2,
      "tov": 3
    }
  ]
}
```

### Example payload
```json
{
  "body": [
    {
      "gameDate": "20260303",
      "pts": 28,
      "reb": 9,
      "ast": 6,
      "stl": 1,
      "blk": 2,
      "tov": 3
    }
  ]
}
```

---

## 8) `getNBAProjections` (numDays=7 only)
**Purpose:** 7‑day rolling totals (form signal)

### Expected schema (used)
```json
{
  "body": [
    {
      "playerID": "string|int",
      "longName": "string",
      "fantasyPoints": 210.5,
      "pts": 160,
      "reb": 45,
      "ast": 38,
      "stl": 7,
      "blk": 6,
      "tov": 18
    }
  ]
}
```

### Example payload
```json
{
  "body": [
    {
      "playerID": "203076",
      "longName": "Anthony Davis",
      "fantasyPoints": 212.4,
      "pts": 162,
      "reb": 49,
      "ast": 36,
      "stl": 6,
      "blk": 8,
      "tov": 16
    }
  ]
}
```

---

## Notes
- **Date handling:** Pete normalizes `YYYYMMDD` ↔ `YYYY-MM-DD` internally.
- **Injury tags:** `out`, `doubtful`, `inactive`, `ruled out`, `suspended` (excluded).
- **Fantasy points:** if `fantasyPoints` missing in projections, Pete derives FP from pts/reb/ast/stl/blk/tov weights.
- **Snapshot files:** stored under `projects/pete-dfs/data-lake/nba/<category>/<YYYY-MM-DD>.json`.
