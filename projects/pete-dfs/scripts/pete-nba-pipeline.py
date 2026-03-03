#!/usr/bin/env python3
"""
Pete's Daily NBA Pipeline

Current mode:
- Data source: API-Sports (NBA)
- Wagering output: fail-closed by default until quant controls are enabled

Environment:
- NBA_API_KEY (required for API calls)
- NBA_API_BASE_URL (optional, default: https://v2.nba.api-sports.io)
- OPENCLAW_WORKSPACE (optional, default: /Users/jjbot/.openclaw/workspace)
- PETE_ENABLE_WAGERING=1 to allow bet recommendations
- PETE_QUANT_RULES_PATH (optional) custom quant rules JSON path
"""

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

import requests

WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE", "/Users/jjbot/.openclaw/workspace"))
LOG_DIR = WORKSPACE / "logs" / "Pete"
TODAY = datetime.now().strftime("%Y-%m-%d")
DEFAULT_SEASON = str(datetime.now().year)
NBA_API_BASE_URL = os.environ.get("NBA_API_BASE_URL", "https://v2.nba.api-sports.io").rstrip("/")
NBA_API_TIMEOUT = int(os.environ.get("NBA_API_TIMEOUT", "30"))


def load_env_secrets() -> str:
    """Load secrets from ~/.env.pete and return NBA API key."""
    env_file = Path.home() / ".env.pete"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, val = line.split("=", 1)
                    os.environ[key] = val

    return os.environ.get("NBA_API_KEY", "")


def api_sports_get(path: str, params: dict) -> dict:
    api_key = os.environ.get("NBA_API_KEY", "")
    if not api_key:
        return {"response": [], "errors": {"auth": "NBA_API_KEY missing"}}

    url = f"{NBA_API_BASE_URL}/{path.lstrip('/')}"
    headers = {
        "x-apisports-key": api_key,
        "Accept": "application/json",
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=NBA_API_TIMEOUT)
        if response.status_code != 200:
            return {
                "response": [],
                "errors": {"http": f"status={response.status_code}", "body": response.text[:500]},
            }

        data = response.json()
        if not isinstance(data, dict):
            return {"response": [], "errors": {"format": "non-object response"}}
        return data
    except Exception as exc:
        return {"response": [], "errors": {"exception": str(exc)}}


def _response_list(payload: dict) -> list:
    values = payload.get("response", [])
    if isinstance(values, list):
        return values
    return []


def fetch_nba_games(season: str, date: str) -> dict:
    """Fetch game slate from API-Sports."""
    payload = api_sports_get("games", {"season": season, "date": date})
    rows = _response_list(payload)

    games = []
    for row in rows:
        teams = row.get("teams", {}) if isinstance(row, dict) else {}
        home = teams.get("home", {}) if isinstance(teams, dict) else {}
        away = teams.get("visitors", {}) if isinstance(teams, dict) else {}
        status = row.get("status", {}) if isinstance(row, dict) else {}

        games.append(
            {
                "game_id": row.get("id"),
                "home_team": home.get("code") or home.get("name") or "",
                "away_team": away.get("code") or away.get("name") or "",
                "home_name": home.get("name") or "",
                "away_name": away.get("name") or "",
                "status": status.get("long") or status.get("short") or "",
                "start_time": row.get("date") or "",
            }
        )

    note = f"Found {len(games)} games"
    if payload.get("errors"):
        note = f"{note}; errors={payload.get('errors')}"

    return {"games": games, "note": note}


def fetch_nba_odds(date: str, season: str) -> dict:
    """Fetch odds from API-Sports and normalize to internal shape."""
    payload = api_sports_get("odds", {"date": date, "season": season})
    rows = _response_list(payload)

    games = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        home_name = (
            row.get("teams", {}).get("home", {}).get("name")
            if isinstance(row.get("teams"), dict)
            else row.get("home")
        )
        away_name = (
            row.get("teams", {}).get("visitors", {}).get("name")
            if isinstance(row.get("teams"), dict)
            else row.get("away")
        )

        normalized = {"home": home_name or "", "away": away_name or "", "start": row.get("date"), "odds": {}}

        bookmakers = row.get("bookmakers", [])
        if isinstance(bookmakers, list):
            for book in bookmakers:
                bets = book.get("bets", []) if isinstance(book, dict) else []
                for bet in bets:
                    values = bet.get("values", []) if isinstance(bet, dict) else []
                    for value in values:
                        team = value.get("value") if isinstance(value, dict) else None
                        odd_raw = value.get("odd") if isinstance(value, dict) else None
                        if not team or odd_raw is None:
                            continue
                        try:
                            odd = float(odd_raw)
                        except Exception:
                            continue
                        if odd <= 1.0:
                            continue
                        previous = normalized["odds"].get(team)
                        if previous is None or odd > previous:
                            normalized["odds"][team] = odd

        if normalized["odds"]:
            games.append(normalized)

    note = f"Found odds for {len(games)} games"
    if payload.get("errors"):
        note = f"{note}; errors={payload.get('errors')}"

    return {"games": games, "note": note}


def build_best_lineup(_games_data: dict) -> dict:
    return {
        "lineup": [],
        "format": "classic",
        "total_salary": 0,
        "note": "Lineup model not enabled in this script",
    }


def get_pivots(_games_data: dict) -> list:
    return [
        {"player": "TBD", "reason": "Awaiting injury and lineup feed"},
        {"player": "TBD", "reason": "Awaiting late swap data"},
    ]


def decimal_to_aus(decimal_odds: float) -> str:
    if decimal_odds >= 2:
        return f"+{int((decimal_odds - 1) * 100)}"
    return f"-{int(100 / (decimal_odds - 1))}"


def _candidate_rules_paths() -> list:
    script_dir = Path(__file__).resolve().parent
    return [
        Path(os.environ.get("PETE_QUANT_RULES_PATH", "")),
        script_dir.parent / "config" / "quant_rules.json",
        Path.cwd() / "projects" / "pete-dfs" / "config" / "quant_rules.json",
    ]


def load_quant_rules() -> dict:
    defaults = {
        "enabled": False,
        "min_edge_pct": 0.03,
        "min_model_prob": 0.52,
        "max_single_bet_decimal_odds": 3.0,
        "max_parlay_legs": 3,
    }

    for candidate in _candidate_rules_paths():
        if not candidate or str(candidate).strip() == "":
            continue
        if candidate.exists():
            try:
                parsed = json.loads(candidate.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    defaults.update(parsed)
                break
            except Exception:
                pass

    return defaults


def wagering_enabled(rules: dict) -> bool:
    env_enabled = os.environ.get("PETE_ENABLE_WAGERING", "0") == "1"
    rules_enabled = bool(rules.get("enabled", False))
    return env_enabled and rules_enabled


def build_parlay(_games_data: dict, odds_data: dict, rules: dict) -> dict:
    if not wagering_enabled(rules):
        return {
            "legs": [],
            "total_odds": 0,
            "edge_notes": "NO_PARLAY: wagering disabled until quant controls are enabled",
        }

    legs = []
    for game in odds_data.get("games", []):
        odds = game.get("odds", {})
        if not odds:
            continue

        # Pick the best-priced team under risk bounds.
        best_team = None
        best_price = 0.0
        for team, price in odds.items():
            try:
                p = float(price)
            except Exception:
                continue
            if p <= 1.0 or p > float(rules.get("max_single_bet_decimal_odds", 3.0)):
                continue
            if p > best_price:
                best_price = p
                best_team = team

        if best_team:
            legs.append(
                {
                    "team": best_team,
                    "odds": best_price,
                    "aus_odds": decimal_to_aus(best_price),
                    "game": f"{game.get('away')} @ {game.get('home')}",
                }
            )

        if len(legs) >= int(rules.get("max_parlay_legs", 3)):
            break

    total_odds = 1.0
    for leg in legs:
        total_odds *= leg["odds"]

    return {
        "legs": legs,
        "total_odds": round(total_odds, 2) if legs else 0,
        "edge_notes": "Quant-gated candidate parlay" if legs else "NO_PARLAY: no eligible legs",
    }


def get_bet_pick(_games_data: dict, odds_data: dict, rules: dict = None) -> dict:
    rules = rules or load_quant_rules()

    if not wagering_enabled(rules):
        return {
            "pick": "NO_BET",
            "odds": 0,
            "aus_odds": "N/A",
            "implied_prob": 0,
            "model_prob": 0,
            "edge": 0,
            "band": "N/A",
            "game": "N/A",
            "reason": "Wagering disabled. Set PETE_ENABLE_WAGERING=1 and enable quant_rules.json",
        }

    best = None
    for game in odds_data.get("games", []):
        odds = game.get("odds", {})
        for team, odd in odds.items():
            try:
                price = float(odd)
            except Exception:
                continue
            if price <= 1.0 or price > float(rules.get("max_single_bet_decimal_odds", 3.0)):
                continue

            implied = 1 / price
            # Placeholder model until full quant model is integrated.
            model = max(float(rules.get("min_model_prob", 0.52)), 0.55)
            edge = model - implied

            if edge < float(rules.get("min_edge_pct", 0.03)):
                continue

            candidate = {
                "pick": team,
                "odds": price,
                "aus_odds": decimal_to_aus(price),
                "implied_prob": implied,
                "model_prob": model,
                "edge": edge,
                "band": "rule-gated",
                "game": f"{game.get('away')} @ {game.get('home')}",
                "reason": "Candidate passed minimum quant gates",
            }

            if best is None or candidate["edge"] > best["edge"]:
                best = candidate

    if best is None:
        return {
            "pick": "NO_BET",
            "odds": 0,
            "aus_odds": "N/A",
            "implied_prob": 0,
            "model_prob": 0,
            "edge": 0,
            "band": "N/A",
            "game": "N/A",
            "reason": "No candidate met quant thresholds",
        }

    return best


def generate_report(games_data: dict, odds_data: dict, lineup: dict, pivots: list, parlay: dict, bet: dict, season: str) -> str:
    report = f"""# Pete NBA Daily - {TODAY}
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M %Z')}
Season: {season}

## Data Sources
- Games/Odds: API-Sports ({NBA_API_BASE_URL})

## Games Today ({games_data.get('note', '')})
"""

    for game in games_data.get("games", [])[:10]:
        report += f"- {game.get('away_team')} @ {game.get('home_team')} ({game.get('start_time', 'TBD')})\n"

    report += f"""
## Odds Snapshot ({odds_data.get('note', '')})
- Games with normalized odds: {len(odds_data.get('games', []))}

## Best Lineup ({lineup['format']})
- Total Salary: ${lineup['total_salary']:,}
- Note: {lineup['note']}

## Late News Pivots
1. **{pivots[0]['player']}**: {pivots[0]['reason']}
2. **{pivots[1]['player']}**: {pivots[1]['reason']}

## Quant-Gated Parlay ({parlay.get('total_odds', 0)}x)
"""

    if parlay.get("legs"):
        for idx, leg in enumerate(parlay["legs"], 1):
            report += f"{idx}. {leg['team']} @ {leg['aus_odds']} ({leg['odds']}) - {leg['game']}\n"
    else:
        report += "- NO_PARLAY\n"

    report += f"- Note: {parlay.get('edge_notes', '')}\n"

    report += f"""
## Bet Decision
- Pick: **{bet['pick']}**
- Odds: {bet.get('aus_odds', 'N/A')} ({bet.get('odds', 0)})
- Edge: {bet.get('edge', 0) * 100:.2f}%
- Game: {bet.get('game', 'N/A')}
- Reason: {bet.get('reason', 'N/A')}

---
Safety: Script defaults to NO_BET until quant controls are explicitly enabled.
"""

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Pete NBA Daily Pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Print report to stdout")
    parser.add_argument("--date", default=TODAY, help="Date in YYYY-MM-DD")
    parser.add_argument("--season", default=DEFAULT_SEASON, help="Season start year (e.g., 2026)")
    args = parser.parse_args()

    print(f"[Pete NBA] Starting pipeline for date={args.date} season={args.season}")

    api_key = load_env_secrets()
    if not api_key:
        print("[Pete NBA] WARNING: NBA_API_KEY not set")

    games_data = fetch_nba_games(args.season, args.date)
    odds_data = fetch_nba_odds(args.date, args.season)

    rules = load_quant_rules()
    lineup = build_best_lineup(games_data)
    pivots = get_pivots(games_data)
    parlay = build_parlay(games_data, odds_data, rules)
    bet = get_bet_pick(games_data, odds_data, rules)

    report = generate_report(games_data, odds_data, lineup, pivots, parlay, bet, args.season)

    if args.dry_run:
        print(report)
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    output_file = LOG_DIR / f"{args.date}.md"
    with open(output_file, "w", encoding="utf-8") as handle:
        handle.write(report)

    print(f"[Pete NBA] Report written: {output_file}")


if __name__ == "__main__":
    main()
