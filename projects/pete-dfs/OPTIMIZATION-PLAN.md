# Pete DFS Optimization Plan

## Current State

Scripts are preserved exactly from production root for continuity:

- `scripts/pete-nba-pipeline.py`
- `scripts/PeteDFS_engine.py`
- `scripts/draftstars_final.py`

## Observed Gaps

1. Hardcoded machine paths (`/Users/jjbot/...`) reduce portability.
2. Mixed concerns in single scripts (fetch, model, reporting) make testing harder.
3. No deterministic fixture-driven regression suite for optimizer behavior.
4. API calls are uncached and can exceed practical request budgets.
5. Draftstars brute-force loops can be expensive and non-deterministic in runtime.

## Optimization Phases

### Phase 1: Portability and Contracts

- Introduce a shared config module for workspace/input/output paths.
- Keep logic unchanged while removing hardcoded absolute paths.
- Define input/output contracts for each script.

### Phase 2: Deterministic Data Layer

- Cache odds/game responses per run date.
- Add explicit timeout/retry strategy and failure fallbacks.
- Add fixture replay mode for local testing.

### Phase 3: Optimizer Performance

- Benchmark current runtime and output quality.
- Replace nested brute-force loops with constrained search or MILP consistently.
- Add pruning bounds with correctness checks.

### Phase 4: Delivery Safety

- Add output validation (salary cap, position constraints, duplicate detection).
- Add run summary metrics and anomaly flags.
- Wire reports into canonical workspace files for Clerk normalization.

## Immediate Next Patch

- Move runtime paths to env/config variables.
- Add script entrypoint wrappers under `projects/pete-dfs`.
- Preserve root scripts as references until cutover.
