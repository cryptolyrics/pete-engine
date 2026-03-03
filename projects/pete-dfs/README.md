# Pete DFS Project

This is Pete's preserved workstream and optimization track.

## Current Safety State

- Data source moved to API-Sports in `scripts/pete-nba-pipeline.py`.
- Betting recommendations are fail-closed by default:
  - output is `NO_BET` unless quant rules are enabled and env gating is explicit.

Enable wagering only when both are true:
1. `PETE_ENABLE_WAGERING=1`
2. `projects/pete-dfs/config/quant_rules.json` has `"enabled": true`

## Scripts

- `scripts/pete-nba-pipeline.py`
- `scripts/PeteDFS_engine.py`
- `scripts/draftstars_final.py`

## Fixtures

- `fixtures/sample_games.json`
- `fixtures/sample_odds.json`

## Tests

Run baseline tests:

```bash
python3 -m unittest discover -s projects/pete-dfs/tests -v
```

## Optimization Roadmap

See `OPTIMIZATION-PLAN.md`.
