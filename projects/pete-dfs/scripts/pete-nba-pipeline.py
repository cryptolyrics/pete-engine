#!/usr/bin/env python3
"""
Pete's Daily NBA Pipeline

Current mode:
- Data source: Tank01 snapshots first; API-Sports fallback only
- Wagering output: fail-closed by default until quant controls are enabled

Environment:
- NBA_API_KEY (optional, only needed if API-Sports fallback is used)
- NBA_API_BASE_URL (optional, default: https://v2.nba.api-sports.io)
- OPENCLAW_WORKSPACE (optional, default: ./.pete-workspace)
- PETE_ENABLE_WAGERING=1 to allow bet recommendations
- PETE_QUANT_RULES_PATH (optional) custom quant rules JSON path
- PETE_LEARNING_STATE_PATH (optional) custom learning state JSON path
- PETE_MAJOR_OUTS_PATH (optional) JSON file of major injury outs
- PETE_ESPN_INJURIES_PATH (optional) ESPN injury sync JSON path
"""

import argparse
import csv
import json
import math
import os
import statistics
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests
try:
    import pandas as pd
except Exception:  # pragma: no cover - optional dependency
    pd = None

WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE", str(Path.cwd() / ".pete-workspace")))
LOG_DIR = WORKSPACE / "logs" / "Pete"
TODAY = datetime.now().strftime("%Y-%m-%d")
DEFAULT_SEASON = str(datetime.now().year)
DEFAULT_SALARY_CAP = 50000  # Standard NBA DFS salary cap
NBA_API_BASE_URL = os.environ.get("NBA_API_BASE_URL", "https://v2.nba.api-sports.io").rstrip("/")
NBA_API_TIMEOUT = int(os.environ.get("NBA_API_TIMEOUT", "30"))
TANK01_MARKET_MAP = {
    "pts": "PTS",
    "reb": "REB",
    "ast": "AST",
    "stl": "STL",
    "blk": "BLK",
    "turnover": "TOV",
    "threes": "3PM",
    "ptsreb": "PR",
    "ptsast": "PA",
    "rebast": "RA",
    "ptsrebast": "PRA",
    "stlblk": "SB",
}
TANK01_TEAM_ALIASES = {
    "GS": "GSW",
    "NO": "NOP",
    "SA": "SAS",
    "NY": "NYK",
    "PHO": "PHX",
    "BRK": "BKN",
}


TANK01_BASE_URL = "https://tank01-fantasy-stats.p.rapidapi.com"
TANK01_HOST = "tank01-fantasy-stats.p.rapidapi.com"
TANK01_DEFAULT_TIMEOUT = int(os.environ.get("TANK01_TIMEOUT", "30"))
INJURY_OUT_TAGS = {"out", "doubtful", "inactive", "ruled out", "suspended"}
INJURY_QUESTIONABLE_TAGS = {"questionable", "gtd", "game time decision"}
INJURY_PROBABLE_TAGS = {"probable"}

def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def normalize_team_key(name: str) -> str:
    return str(name or "").strip().lower()


def normalize_team_code(name: str) -> str:
    code = str(name or "").strip().upper()
    if not code:
        return ""
    if code in TANK01_TEAM_ALIASES:
        return TANK01_TEAM_ALIASES[code]
    if len(code) == 3:
        return code
    return code


def safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def load_env_secrets() -> str:
    """Load secrets from ~/.env.pete and return NBA API key."""
    env_file = Path.home() / ".env.pete"
    if env_file.exists():
        with open(env_file, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, val = line.split("=", 1)
                    os.environ[key] = val

    return os.environ.get("NBA_API_KEY", "")




def tank01_api_key() -> str:
    return os.environ.get("TANK01_RAPIDAPI_KEY") or os.environ.get("RAPIDAPI_KEY") or ""


def tank01_get(endpoint: str, params: Optional[dict] = None, timeout_s: Optional[int] = None) -> dict:
    api_key = tank01_api_key()
    if not api_key:
        return {"body": [], "errors": {"auth": "TANK01_RAPIDAPI_KEY missing"}}

    url = f"{TANK01_BASE_URL}/{endpoint.lstrip('/')}"
    headers = {
        "x-rapidapi-host": TANK01_HOST,
        "x-rapidapi-key": api_key,
        "Accept": "application/json",
    }
    try:
        response = requests.get(url, headers=headers, params=params or {}, timeout=timeout_s or TANK01_DEFAULT_TIMEOUT)
        if response.status_code != 200:
            return {
                "body": [],
                "errors": {"http": f"status={response.status_code}", "body": response.text[:500]},
            }
        payload = response.json()
        if isinstance(payload, dict):
            return payload
        return {"body": payload}
    except Exception as exc:
        return {"body": [], "errors": {"exception": str(exc)}}


def _tank01_body_list(payload: dict) -> list:
    if not isinstance(payload, dict):
        return []
    body = payload.get("body", payload.get("response", []))
    if isinstance(body, list):
        return body
    return []


def _save_tank01_snapshot(payload: dict, data_root: str, category: str, run_date: str) -> Path:
    root = Path(data_root) / "nba" / category
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{run_date}.json"
    if isinstance(payload, dict) and "body" in payload:
        snapshot = payload
    else:
        snapshot = {"body": payload}
    snapshot.setdefault("meta", {})
    snapshot["meta"].update({"source": "tank01-live", "fetched_at": datetime.now().isoformat(timespec="seconds")})
    target.write_text(json.dumps(snapshot, indent=2), encoding="utf-8")
    return target


def classify_injury_status(status_text: str) -> str:
    text = str(status_text or "").strip().lower()
    if not text:
        return "available"
    if any(tag in text for tag in INJURY_OUT_TAGS):
        return "out"
    if any(tag in text for tag in INJURY_QUESTIONABLE_TAGS):
        return "questionable"
    if any(tag in text for tag in INJURY_PROBABLE_TAGS):
        return "probable"
    return "available"


def _canonical_player_name(value: str) -> str:
    text = str(value or "").lower()
    text = "".join(ch if ch.isalnum() or ch.isspace() else " " for ch in text)
    text = " ".join(text.split())
    for suffix in (" jr", " sr", " ii", " iii", " iv", " v"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    return text

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




def refresh_tank01_snapshots(run_date: str, data_root: str, season: str) -> dict:
    status = {"ok": True, "errors": {}, "paths": {}}

    def _fetch(endpoint: str, params: dict, category: str):
        payload = tank01_get(endpoint, params=params)
        if payload.get("errors"):
            status["ok"] = False
            status["errors"][endpoint] = payload.get("errors")
        path = _save_tank01_snapshot(payload, data_root, category, run_date)
        status["paths"][category] = str(path)
        return payload

    _fetch("getNBAPlayerList", {}, "players")
    _fetch("getNBABettingOdds", {"gameDate": _dash_to_compact_date(run_date)}, "betting-props")
    _fetch("getNBAInjuryList", {}, "injuries")
    _fetch("getNBADFS", {"slate": "main"}, "dfs")
    _fetch("getNBATeams", {}, "teams")
    _fetch("getNBAScoresOnly", {"gameDate": _dash_to_compact_date(run_date)}, "scores")
    _fetch("getNBAProjections", {"numDays": 7}, "projections")
    return status


def load_tank01_dfs_pool(run_date: str, data_root: str, max_lag_days: int = 2) -> dict:
    source_path, lag_days = _resolve_dated_json(Path(data_root) / "nba" / "dfs", run_date, max_lag_days)
    if source_path is None:
        return {"players": [], "source": "", "source_lag_days": -1}
    payload = _load_json(source_path)
    rows = _tank01_body_list(payload)
    return {"players": rows, "source": str(source_path), "source_lag_days": int(lag_days)}


def load_tank01_projections(run_date: str, data_root: str, max_lag_days: int = 2) -> dict:
    source_path, lag_days = _resolve_dated_json(Path(data_root) / "nba" / "projections", run_date, max_lag_days)
    if source_path is None:
        return {"rows": [], "source": "", "source_lag_days": -1}
    payload = _load_json(source_path)
    rows = _tank01_body_list(payload)
    return {"rows": rows, "source": str(source_path), "source_lag_days": int(lag_days)}


def build_projection_map_from_tank01(rows: list) -> dict:
    projection = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("playerID") or row.get("playerId") or row.get("player_id") or "").strip()
        name = str(row.get("longName") or row.get("playerName") or row.get("name") or "").strip()
        raw_fp = row.get("fantasyPoints") or row.get("fantasyPointsTotal") or row.get("FP") or row.get("FPTS")
        fp = safe_float(raw_fp, 0.0)
        if fp <= 0:
            fp = (
                safe_float(row.get("pts"), 0.0) * POINTS_WEIGHT
                + safe_float(row.get("reb"), 0.0) * REBOUNDS_WEIGHT
                + safe_float(row.get("ast"), 0.0) * ASSISTS_WEIGHT
                + safe_float(row.get("stl"), 0.0) * STEALS_WEIGHT
                + safe_float(row.get("blk"), 0.0) * BLOCKS_WEIGHT
                + safe_float(row.get("tov"), 0.0) * TURNOVERS_WEIGHT
            )
        if fp <= 0:
            continue
        if pid:
            projection[pid] = fp
        if name:
            projection[_canonical_player_name(name)] = fp
    return projection


def load_tank01_injury_status_map(run_date: str, data_root: str, max_lag_days: int = 2) -> dict:
    source_path, lag_days = _resolve_dated_json(Path(data_root) / "nba" / "injuries", run_date, max_lag_days)
    if source_path is None:
        return {"by_name": {}, "source": "", "source_lag_days": -1}
    payload = _load_json(source_path)
    rows = []
    _collect_tank01_dicts(payload.get("body", payload), rows)
    status = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("longName") or row.get("playerName") or row.get("player") or "").strip()
        if not name:
            continue
        status_text = row.get("injuryStatus") or row.get("status") or row.get("injury") or ""
        status[_canonical_player_name(name)] = classify_injury_status(status_text)
    return {"by_name": status, "source": str(source_path), "source_lag_days": int(lag_days)}


def load_tank01_scores(run_date: str, data_root: str, max_lag_days: int = 2) -> dict:
    source_path, lag_days = _resolve_dated_json(Path(data_root) / "nba" / "scores", run_date, max_lag_days)
    if source_path is None:
        return {"rows": [], "source": "", "source_lag_days": -1}
    payload = _load_json(source_path)
    rows = _tank01_body_list(payload)
    return {"rows": rows, "source": str(source_path), "source_lag_days": int(lag_days)}


def load_tank01_teams(run_date: str, data_root: str, max_lag_days: int = 2) -> dict:
    source_path, lag_days = _resolve_dated_json(Path(data_root) / "nba" / "teams", run_date, max_lag_days)
    if source_path is None:
        return {"rows": [], "source": "", "source_lag_days": -1}
    payload = _load_json(source_path)
    rows = _tank01_body_list(payload)
    return {"rows": rows, "source": str(source_path), "source_lag_days": int(lag_days)}

def _response_list(payload: dict) -> list:
    values = payload.get("response", [])
    if isinstance(values, list):
        return values
    return []


def _parse_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None

    formats = [
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%H:%M",
    ]
    for fmt in formats:
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == "%H:%M":
                today = datetime.now()
                parsed = parsed.replace(year=today.year, month=today.month, day=today.day)
            return parsed
        except Exception:
            continue

    return None


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
                "home_code": home.get("code") or "",
                "away_code": away.get("code") or "",
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

        teams_blob = row.get("teams", {}) if isinstance(row.get("teams"), dict) else {}
        home_blob = teams_blob.get("home", {}) if isinstance(teams_blob, dict) else {}
        away_blob = teams_blob.get("visitors", {}) if isinstance(teams_blob, dict) else {}

        home_name = home_blob.get("name") or row.get("home") or ""
        away_name = away_blob.get("name") or row.get("away") or ""

        normalized = {
            "home": home_name,
            "away": away_name,
            "home_code": home_blob.get("code") or "",
            "away_code": away_blob.get("code") or "",
            "start": row.get("date"),
            "odds": {},
        }

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
                        odd = safe_float(odd_raw, 0.0)
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


def _parse_dated_stem(path: Path) -> Optional[datetime]:
    try:
        return datetime.strptime(path.stem, "%Y-%m-%d")
    except Exception:
        return None


def _resolve_dated_json(directory: Path, run_date: str, max_lag_days: int) -> Tuple[Optional[Path], int]:
    target = datetime.strptime(run_date, "%Y-%m-%d")
    exact = directory / f"{run_date}.json"
    if exact.exists():
        return exact, 0
    if not directory.exists():
        return None, -1

    best_path: Optional[Path] = None
    best_lag = 10**9
    for path in directory.glob("*.json"):
        parsed = _parse_dated_stem(path)
        if parsed is None:
            continue
        lag = (target - parsed).days
        if lag < 0 or lag > max(0, max_lag_days):
            continue
        if lag < best_lag:
            best_lag = lag
            best_path = path
    if best_path is None:
        return None, -1
    return best_path, best_lag


def _load_json(path: Path) -> dict:
    if not path or not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        return {}
    return {}


def _dash_to_compact_date(value: str) -> str:
    text = str(value or "").strip()
    if len(text) == 10 and text.count("-") == 2:
        return text.replace("-", "")
    return text


def _compact_to_dash_date(value: str) -> str:
    text = str(value or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[0:4]}-{text[4:6]}-{text[6:8]}"
    return text


def _parse_dash_date(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d")
    except Exception:
        return None


def infer_slate_date(run_date: str, source_summary: dict, odds_data: dict) -> str:
    # Prefer dated Tank01 snapshot path when available.
    snapshot_path = str(source_summary.get("tank01_odds_source", "")).strip()
    if snapshot_path:
        try:
            stem = Path(snapshot_path).stem
            parsed = _parse_dash_date(stem)
            if parsed is not None:
                return parsed.strftime("%Y-%m-%d")
        except Exception:
            pass

    # Fallback to first game start field when date-formatted.
    for game in odds_data.get("games", []):
        if not isinstance(game, dict):
            continue
        start_raw = str(game.get("start", "")).strip()
        start_dash = _compact_to_dash_date(start_raw)
        parsed = _parse_dash_date(start_dash)
        if parsed is not None:
            return parsed.strftime("%Y-%m-%d")

    return str(run_date)


def american_to_decimal(value: str) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    if text.startswith("+"):
        n = safe_float(text[1:], 0.0)
        return round(1.0 + (n / 100.0), 4) if n > 0 else 0.0
    if text.startswith("-"):
        n = safe_float(text[1:], 0.0)
        return round(1.0 + (100.0 / n), 4) if n > 0 else 0.0
    dec = safe_float(text, 0.0)
    return dec if dec > 1.0 else 0.0


def load_tank01_players_index(run_date: str, data_root: str, max_lag_days: int = 2, explicit_players_json: str = "") -> dict:
    if explicit_players_json:
        source_path = Path(explicit_players_json)
        lag_days = 0
    else:
        source_path, lag_days = _resolve_dated_json(Path(data_root) / "nba" / "players", run_date, max_lag_days)
    if source_path is None:
        return {"by_id": {}, "source": "", "source_lag_days": -1}

    payload = _load_json(source_path)
    body = payload.get("body", []) if isinstance(payload, dict) else []
    rows = body if isinstance(body, list) else []
    by_id = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        player_id = str(row.get("playerID", "")).strip()
        name = str(row.get("longName", "")).strip()
        team = normalize_team_code(row.get("team", ""))
        if player_id and name:
            by_id[player_id] = {"name": name, "team": team}

    return {"by_id": by_id, "source": str(source_path), "source_lag_days": int(lag_days)}


def load_tank01_odds(run_date: str, data_root: str, max_lag_days: int = 2, explicit_props_json: str = "") -> dict:
    if explicit_props_json:
        source_path = Path(explicit_props_json)
        lag_days = 0
    else:
        source_path, lag_days = _resolve_dated_json(Path(data_root) / "nba" / "betting-props", run_date, max_lag_days)
    if source_path is None:
        return {"games": [], "note": "Tank01 props file not found", "source": "", "source_lag_days": -1}

    payload = _load_json(source_path)
    body = payload.get("body", []) if isinstance(payload, dict) else []
    rows = body if isinstance(body, list) else []
    games = []

    for row in rows:
        if not isinstance(row, dict):
            continue
        home = normalize_team_code(row.get("homeTeam", ""))
        away = normalize_team_code(row.get("awayTeam", ""))
        if not home or not away:
            continue

        normalized = {
            "home": home,
            "away": away,
            "home_code": home,
            "away_code": away,
            "start": str(row.get("gameDate", "")),
            "odds": {},
            "source": "tank01",
            "market_context": {
                "totals": [],
                "spread_by_team": {home: [], away: []},
            },
        }

        books = row.get("sportsBooks", [])
        if isinstance(books, list):
            for book in books:
                if not isinstance(book, dict):
                    continue
                odds = book.get("odds", {}) if isinstance(book.get("odds"), dict) else {}
                home_ml = american_to_decimal(odds.get("homeTeamML", ""))
                away_ml = american_to_decimal(odds.get("awayTeamML", ""))
                if home_ml > 1.0:
                    prev = safe_float(normalized["odds"].get(home), 0.0)
                    normalized["odds"][home] = max(prev, home_ml)
                if away_ml > 1.0:
                    prev = safe_float(normalized["odds"].get(away), 0.0)
                    normalized["odds"][away] = max(prev, away_ml)

                total_over = safe_float(odds.get("totalOver"), math.nan)
                total_under = safe_float(odds.get("totalUnder"), math.nan)
                if not math.isnan(total_over):
                    normalized["market_context"]["totals"].append(total_over)
                if not math.isnan(total_under):
                    normalized["market_context"]["totals"].append(total_under)

                home_spread = safe_float(odds.get("homeTeamSpread"), math.nan)
                away_spread = safe_float(odds.get("awayTeamSpread"), math.nan)
                if not math.isnan(home_spread):
                    normalized["market_context"]["spread_by_team"][home].append(home_spread)
                if not math.isnan(away_spread):
                    normalized["market_context"]["spread_by_team"][away].append(away_spread)

        if normalized["odds"]:
            totals = [safe_float(v, math.nan) for v in normalized["market_context"].get("totals", [])]
            totals = [v for v in totals if not math.isnan(v)]
            spread_by_team = normalized["market_context"].get("spread_by_team", {})
            spread_summary = {}
            for team_code, values in spread_by_team.items():
                valid = [safe_float(v, math.nan) for v in values]
                valid = [v for v in valid if not math.isnan(v)]
                if valid:
                    spread_summary[team_code] = round(float(statistics.median(valid)), 3)
            normalized["market_context"] = {
                "consensus_total": round(float(statistics.median(totals)), 3) if totals else 0.0,
                "spread_by_team": spread_summary,
            }
            games.append(normalized)

    return {
        "games": games,
        "note": f"Tank01 odds loaded: {len(games)} games",
        "source": str(source_path),
        "source_lag_days": int(lag_days),
    }


def load_tank01_games(run_date: str, data_root: str, max_lag_days: int = 2, explicit_props_json: str = "") -> dict:
    odds_feed = load_tank01_odds(
        run_date,
        data_root,
        max_lag_days=max_lag_days,
        explicit_props_json=explicit_props_json,
    )
    games = []
    for row in odds_feed.get("games", []):
        if not isinstance(row, dict):
            continue
        home_code = normalize_team_code(row.get("home_code") or row.get("home"))
        away_code = normalize_team_code(row.get("away_code") or row.get("away"))
        if not home_code or not away_code:
            continue
        games.append(
            {
                "game_id": f"{_dash_to_compact_date(run_date)}_{away_code}@{home_code}",
                "home_team": home_code,
                "away_team": away_code,
                "home_name": row.get("home") or home_code,
                "away_name": row.get("away") or away_code,
                "home_code": home_code,
                "away_code": away_code,
                "status": "Scheduled",
                "start_time": _compact_to_dash_date(str(row.get("start", ""))),
                "source": "tank01",
            }
        )
    return {
        "games": games,
        "note": f"Tank01 games loaded: {len(games)}",
        "source": odds_feed.get("source", ""),
        "source_lag_days": int(odds_feed.get("source_lag_days", -1)),
    }


def load_tank01_teams_played_on_date(
    run_date: str,
    data_root: str,
    max_lag_days: int = 0,
    explicit_props_json: str = "",
) -> Set[str]:
    odds_feed = load_tank01_odds(
        run_date,
        data_root,
        max_lag_days=max(0, max_lag_days),
        explicit_props_json=explicit_props_json,
    )
    teams: Set[str] = set()
    for game in odds_feed.get("games", []):
        home = normalize_team_code(game.get("home_code") or game.get("home"))
        away = normalize_team_code(game.get("away_code") or game.get("away"))
        if home:
            teams.add(normalize_team_key(home))
        if away:
            teams.add(normalize_team_key(away))
    return teams


def _collect_tank01_dicts(node, sink: List[dict]) -> None:
    if isinstance(node, dict):
        sink.append(node)
        for value in node.values():
            _collect_tank01_dicts(value, sink)
    elif isinstance(node, list):
        for item in node:
            _collect_tank01_dicts(item, sink)


def load_tank01_major_out_teams(
    run_date: str,
    data_root: str,
    max_lag_days: int = 2,
    explicit_injuries_json: str = "",
) -> Set[str]:
    if explicit_injuries_json:
        source_path = Path(explicit_injuries_json)
        if not source_path.exists():
            return set()
    else:
        source_path, _ = _resolve_dated_json(Path(data_root) / "nba" / "injuries", run_date, max_lag_days)
        if source_path is None:
            return set()

    payload = _load_json(source_path)
    rows: List[dict] = []
    _collect_tank01_dicts(payload.get("body", payload), rows)

    outs: Set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        team_raw = row.get("team") or row.get("teamAbv") or row.get("teamCode") or row.get("teamName") or ""
        team = normalize_team_code(team_raw)
        if not team:
            continue
        status_text = " ".join(
            str(row.get(field, "")).strip()
            for field in ["status", "injuryStatus", "designation", "description", "injury", "note", "comment"]
        ).upper()
        if any(token in status_text for token in [" OUT", "OUT ", "OUT", "DOUBTFUL", "INACTIVE", "IR"]):
            outs.add(normalize_team_key(team))
    return outs


def merge_odds_data(primary: dict, secondary: dict) -> dict:
    merged = {}

    def key_for(game: dict) -> str:
        home = normalize_team_key(game.get("home_code") or game.get("home"))
        away = normalize_team_key(game.get("away_code") or game.get("away"))
        return f"{away}@{home}"

    for feed in [primary or {}, secondary or {}]:
        for game in feed.get("games", []):
            key = key_for(game)
            if not key:
                continue
            if key not in merged:
                merged[key] = {
                    "home": game.get("home"),
                    "away": game.get("away"),
                    "home_code": game.get("home_code") or game.get("home"),
                    "away_code": game.get("away_code") or game.get("away"),
                    "start": game.get("start", ""),
                    "odds": {},
                    "sources": set(),
                    "market_context": {},
                }
            current = merged[key]
            current["sources"].add(game.get("source", "unknown"))
            if isinstance(game.get("market_context"), dict):
                incoming = game.get("market_context", {})
                current_total = safe_float(current.get("market_context", {}).get("consensus_total"), 0.0)
                incoming_total = safe_float(incoming.get("consensus_total"), 0.0)
                if incoming_total > 0.0 and current_total <= 0.0:
                    current["market_context"] = incoming
                elif incoming_total > 0.0 and current_total > 0.0:
                    # Prefer richer spread coverage.
                    current_spreads = len((current.get("market_context", {}) or {}).get("spread_by_team", {}))
                    incoming_spreads = len(incoming.get("spread_by_team", {}))
                    if incoming_spreads >= current_spreads:
                        current["market_context"] = incoming
            odds = game.get("odds", {})
            if isinstance(odds, dict):
                for team, odd in odds.items():
                    price = safe_float(odd, 0.0)
                    if price <= 1.0:
                        continue
                    prev = safe_float(current["odds"].get(team), 0.0)
                    current["odds"][team] = max(prev, price)

    games = []
    for row in merged.values():
        row["source"] = "+".join(sorted(row.pop("sources")))
        if row["odds"]:
            games.append(row)

    return {
        "games": games,
        "note": f"Merged odds games: {len(games)}",
    }


def _candidate_rules_paths() -> List[Path]:
    script_dir = Path(__file__).resolve().parent
    return [
        Path(os.environ.get("PETE_QUANT_RULES_PATH", "")),
        script_dir.parent / "config" / "quant_rules.json",
        Path.cwd() / "projects" / "pete-dfs" / "config" / "quant_rules.json",
    ]


def _candidate_major_outs_paths() -> List[Path]:
    script_dir = Path(__file__).resolve().parent
    return [
        Path(os.environ.get("PETE_MAJOR_OUTS_PATH", "")),
        script_dir.parent / "config" / "major_outs.json",
        Path.cwd() / "projects" / "pete-dfs" / "config" / "major_outs.json",
    ]


def _candidate_espn_injuries_paths() -> List[Path]:
    script_dir = Path(__file__).resolve().parent
    return [
        Path(os.environ.get("PETE_ESPN_INJURIES_PATH", "")),
        Path.cwd() / "projects" / "pete-dfs" / "data-lake" / "nba" / "injuries" / "latest.json",
        script_dir.parent / "data-lake" / "nba" / "injuries" / "latest.json",
    ]


def learning_state_path() -> Path:
    env = os.environ.get("PETE_LEARNING_STATE_PATH", "").strip()
    if env:
        return Path(env)
    return LOG_DIR / "learning_state.json"


def load_quant_rules() -> dict:
    defaults = {
        "enabled": False,
        "min_edge_pct": 0.03,
        "min_model_prob": 0.52,
        "min_edge_dollars_per_1u": 0.40,
        "parlay_min_edge_pct": 0.03,
        "parlay_min_edge_dollars_per_1u": 0.10,
        "parlay_min_legs": 2,
        "home_team_model_boost_pct": 0.10,
        "max_single_bet_decimal_odds": 3.0,
        "max_parlay_legs": 3,
        "smokie_percentile": 0.35,
        "smokie_min_projection_delta": 1.0,
        "prop_call_haircut_pct": 0.10,
        "prop_min_line_edge": 0.35,
        "prop_min_model_edge_pct": 0.03,
        "prop_min_success_rate": 0.55,
        "prop_min_abs_trend": 0.10,
        "prop_relaxed_line_edge_scale": 0.60,
        "prop_relaxed_model_edge_scale": 0.60,
        "prop_relaxed_min_success_rate": 0.50,
        "prop_relaxed_min_abs_trend": 0.05,
        "prop_max_legs": 3,
        "prop_trend_weight": 0.35,
        "prop_market_prior_weight": 0.25,
        "prop_context_bias_step": 0.02,
        "prop_context_max_bias": 0.05,
        "prop_context_total_over_threshold": 233.0,
        "prop_context_total_under_threshold": 220.0,
        "prop_context_favorite_spread_threshold": 6.5,
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


def load_major_out_teams(override_path: Optional[str] = None) -> Set[str]:
    candidates: List[Path] = []
    if override_path:
        candidates.append(Path(override_path))
    candidates.extend(_candidate_major_outs_paths())

    for candidate in candidates:
        if not candidate or str(candidate).strip() == "":
            continue
        if not candidate.exists():
            continue

        try:
            parsed = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue

        values: List[str] = []
        if isinstance(parsed, dict):
            teams = parsed.get("teams", [])
            if isinstance(teams, list):
                values.extend(str(v) for v in teams)
            for team, is_out in parsed.get("flags", {}).items() if isinstance(parsed.get("flags"), dict) else []:
                if bool(is_out):
                    values.append(str(team))
        elif isinstance(parsed, list):
            values.extend(str(v) for v in parsed)

        return {normalize_team_key(v) for v in values if str(v).strip()}

    return set()


def load_espn_major_out_teams(override_path: Optional[str] = None) -> Set[str]:
    candidates: List[Path] = []
    if override_path:
        candidates.append(Path(override_path))
    candidates.extend(_candidate_espn_injuries_paths())

    for candidate in candidates:
        if not candidate or str(candidate).strip() == "":
            continue
        if not candidate.exists():
            continue

        try:
            parsed = json.loads(candidate.read_text(encoding="utf-8"))
        except Exception:
            continue

        # Expected schema from sync_espn_injuries.py:
        # { "major_out_teams": ["LAL", ...], "teams": { "LAL": [{major_out:true}, ...] } }
        major = parsed.get("major_out_teams", []) if isinstance(parsed, dict) else []
        from_major = {normalize_team_key(v) for v in major if str(v).strip()}
        if from_major:
            return from_major

        teams = parsed.get("teams", {}) if isinstance(parsed, dict) else {}
        extracted = set()
        if isinstance(teams, dict):
            for team, items in teams.items():
                if not isinstance(items, list):
                    continue
                if any(bool(item.get("major_out")) for item in items if isinstance(item, dict)):
                    extracted.add(normalize_team_key(team))
        if extracted:
            return extracted

    return set()


def default_prop_market_stats() -> dict:
    return {
        "PTS": {"over_hits": 1, "over_misses": 1, "under_hits": 1, "under_misses": 1},
        "REB": {"over_hits": 1, "over_misses": 1, "under_hits": 1, "under_misses": 1},
        "AST": {"over_hits": 1, "over_misses": 1, "under_hits": 1, "under_misses": 1},
        "STL": {"over_hits": 1, "over_misses": 1, "under_hits": 1, "under_misses": 1},
        "BLK": {"over_hits": 1, "over_misses": 1, "under_hits": 1, "under_misses": 1},
        "TOV": {"over_hits": 1, "over_misses": 1, "under_hits": 1, "under_misses": 1},
        "3PM": {"over_hits": 1, "over_misses": 1, "under_hits": 1, "under_misses": 1},
        "PR": {"over_hits": 1, "over_misses": 1, "under_hits": 1, "under_misses": 1},
        "PA": {"over_hits": 1, "over_misses": 1, "under_hits": 1, "under_misses": 1},
        "RA": {"over_hits": 1, "over_misses": 1, "under_hits": 1, "under_misses": 1},
        "PRA": {"over_hits": 1, "over_misses": 1, "under_hits": 1, "under_misses": 1},
        "SB": {"over_hits": 1, "over_misses": 1, "under_hits": 1, "under_misses": 1},
    }


def _coerce_market_stats(raw: dict) -> dict:
    base = default_prop_market_stats()
    if not isinstance(raw, dict):
        return base

    for market, values in raw.items():
        key = normalize_market(market)
        if key not in base or not isinstance(values, dict):
            continue
        for field in ["over_hits", "over_misses", "under_hits", "under_misses"]:
            base[key][field] = int(max(0, safe_float(values.get(field), base[key][field])))
    return base


def load_learning_state() -> dict:
    defaults = {
        "player_adjustments": {},
        "team_adjustments": {},
        "player_prop_adjustments": {},
        "player_prop_opp_adjustments": {},
        "prop_market_stats": default_prop_market_stats(),
        "meta": {"dfs_samples": 0, "bet_samples": 0, "prop_samples": 0, "updated_at": "", "last_feedback_file": ""},
    }

    path = learning_state_path()
    if not path.exists():
        return defaults

    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            defaults.update(parsed)
            if not isinstance(defaults.get("player_adjustments"), dict):
                defaults["player_adjustments"] = {}
            if not isinstance(defaults.get("team_adjustments"), dict):
                defaults["team_adjustments"] = {}
            if not isinstance(defaults.get("player_prop_adjustments"), dict):
                defaults["player_prop_adjustments"] = {}
            if not isinstance(defaults.get("player_prop_opp_adjustments"), dict):
                defaults["player_prop_opp_adjustments"] = {}
            defaults["prop_market_stats"] = _coerce_market_stats(defaults.get("prop_market_stats"))
            if not isinstance(defaults.get("meta"), dict):
                defaults["meta"] = {
                    "dfs_samples": 0,
                    "bet_samples": 0,
                    "prop_samples": 0,
                    "updated_at": "",
                    "last_feedback_file": "",
                }
    except Exception:
        return defaults

    return defaults


def save_learning_state(state: dict) -> None:
    path = learning_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    state.setdefault("meta", {})["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _coerce_boolish(value) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
    token = str(value).strip().lower()
    if token in {"1", "true", "t", "yes", "y", "won", "win"}:
        return True
    if token in {"0", "false", "f", "no", "n", "lost", "loss"}:
        return False
    return None


def _scaled_weight(samples: int, max_weight: float = 1.5) -> float:
    count = max(1, int(samples))
    return clamp(1.0 + (0.10 * (count - 1)), 1.0, max_weight)


def _update_learning_state_from_feedback_pandas(
    state: dict,
    payload: dict,
    feedback_path: Optional[str],
) -> dict:
    player_adj = state.setdefault("player_adjustments", {})
    team_adj = state.setdefault("team_adjustments", {})
    prop_adj = state.setdefault("player_prop_adjustments", {})
    prop_opp_adj = state.setdefault("player_prop_opp_adjustments", {})
    market_stats = _coerce_market_stats(state.get("prop_market_stats"))
    state["prop_market_stats"] = market_stats
    meta = state.setdefault("meta", {})

    learning_rate_dfs = 0.08
    learning_rate_bet = 0.06
    learning_rate_prop = 0.08

    dfs_samples = payload.get("dfs", []) if isinstance(payload, dict) else []
    if isinstance(dfs_samples, list) and dfs_samples:
        dfs_df = pd.DataFrame([row for row in dfs_samples if isinstance(row, dict)])
        if not dfs_df.empty and {"player", "projected_fp", "actual_fp"}.issubset(dfs_df.columns):
            dfs_df["player"] = dfs_df["player"].astype(str).str.strip()
            dfs_df = dfs_df[dfs_df["player"] != ""].copy()
            dfs_df["projected_fp"] = pd.to_numeric(dfs_df["projected_fp"], errors="coerce")
            dfs_df["actual_fp"] = pd.to_numeric(dfs_df["actual_fp"], errors="coerce")
            dfs_df = dfs_df.dropna(subset=["projected_fp", "actual_fp"]).copy()
            if not dfs_df.empty:
                dfs_df["error"] = dfs_df["actual_fp"] - dfs_df["projected_fp"]
                grouped = dfs_df.groupby("player", as_index=False).agg(
                    error_mean=("error", "mean"),
                    n=("error", "size"),
                )
                for row in grouped.itertuples(index=False):
                    player = str(row.player).strip()
                    current = safe_float(player_adj.get(player, 0.0), 0.0)
                    delta = learning_rate_dfs * safe_float(row.error_mean, 0.0) * _scaled_weight(int(row.n))
                    player_adj[player] = round(clamp(current + delta, -8.0, 8.0), 4)
                meta["dfs_samples"] = int(meta.get("dfs_samples", 0)) + int(len(dfs_df.index))

    bet_samples = payload.get("bets", []) if isinstance(payload, dict) else []
    if isinstance(bet_samples, list) and bet_samples:
        bet_df = pd.DataFrame([row for row in bet_samples if isinstance(row, dict)])
        if not bet_df.empty and {"team", "model_prob", "won"}.issubset(bet_df.columns):
            bet_df["team_key"] = bet_df["team"].astype(str).str.strip().map(normalize_team_key)
            bet_df = bet_df[bet_df["team_key"] != ""].copy()
            bet_df["model_prob"] = pd.to_numeric(bet_df["model_prob"], errors="coerce")
            bet_df["won_float"] = bet_df["won"].map(
                lambda v: 1.0 if _coerce_boolish(v) is True else (0.0 if _coerce_boolish(v) is False else math.nan)
            )
            bet_df = bet_df.dropna(subset=["model_prob", "won_float"]).copy()
            if not bet_df.empty:
                bet_df["model_prob"] = bet_df["model_prob"].clip(lower=0.01, upper=0.99)
                bet_df["calibration_error"] = bet_df["won_float"] - bet_df["model_prob"]
                grouped = bet_df.groupby("team_key", as_index=False).agg(
                    error_mean=("calibration_error", "mean"),
                    n=("calibration_error", "size"),
                )
                for row in grouped.itertuples(index=False):
                    team_key = str(row.team_key).strip()
                    current = safe_float(team_adj.get(team_key, 0.0), 0.0)
                    delta = learning_rate_bet * safe_float(row.error_mean, 0.0) * _scaled_weight(int(row.n), max_weight=1.4)
                    team_adj[team_key] = round(clamp(current + delta, -0.12, 0.12), 6)
                meta["bet_samples"] = int(meta.get("bet_samples", 0)) + int(len(bet_df.index))

    prop_samples = payload.get("props", []) if isinstance(payload, dict) else []
    if isinstance(prop_samples, list) and prop_samples:
        prop_df = pd.DataFrame([row for row in prop_samples if isinstance(row, dict)])
        if not prop_df.empty and {"player", "market", "projected", "actual"}.issubset(prop_df.columns):
            prop_df["player_key"] = prop_df["player"].astype(str).str.strip().str.lower()
            prop_df["market_norm"] = prop_df["market"].map(normalize_market)
            prop_df["projected"] = pd.to_numeric(prop_df["projected"], errors="coerce")
            prop_df["actual"] = pd.to_numeric(prop_df["actual"], errors="coerce")
            prop_df["opponent_key"] = prop_df.get("opponent", "").map(normalize_team_key) if "opponent" in prop_df.columns else ""
            prop_df = prop_df[
                (prop_df["player_key"] != "")
                & (prop_df["market_norm"] != "")
            ].dropna(subset=["projected", "actual"]).copy()
            if not prop_df.empty:
                prop_df["error"] = prop_df["actual"] - prop_df["projected"]
                by_market = prop_df.groupby(["player_key", "market_norm"], as_index=False).agg(
                    error_mean=("error", "mean"),
                    n=("error", "size"),
                )
                for row in by_market.itertuples(index=False):
                    key = f"{row.player_key}::{row.market_norm}"
                    current = safe_float(prop_adj.get(key, 0.0), 0.0)
                    delta = learning_rate_prop * safe_float(row.error_mean, 0.0) * _scaled_weight(int(row.n))
                    prop_adj[key] = round(clamp(current + delta, -3.0, 3.0), 4)

                if "opponent_key" in prop_df.columns:
                    by_opp = prop_df[prop_df["opponent_key"] != ""].groupby(
                        ["player_key", "opponent_key", "market_norm"], as_index=False
                    ).agg(error_mean=("error", "mean"), n=("error", "size"))
                    for row in by_opp.itertuples(index=False):
                        key = f"{row.player_key}::{row.opponent_key}::{row.market_norm}"
                        current = safe_float(prop_opp_adj.get(key, 0.0), 0.0)
                        delta = 0.10 * safe_float(row.error_mean, 0.0) * _scaled_weight(int(row.n))
                        prop_opp_adj[key] = round(clamp(current + delta, -2.5, 2.5), 4)

                if "direction" in prop_df.columns:
                    prop_df["direction"] = prop_df["direction"].astype(str).str.strip().str.upper()
                else:
                    prop_df["direction"] = ""
                if "line" in prop_df.columns:
                    prop_df["line"] = pd.to_numeric(prop_df["line"], errors="coerce")
                else:
                    prop_df["line"] = math.nan
                if "won" in prop_df.columns:
                    prop_df["won_bool"] = prop_df["won"].map(_coerce_boolish)
                else:
                    prop_df["won_bool"] = None

                # Infer outcome from actual vs line when explicit won/loss is missing.
                mask_over = (
                    prop_df["won_bool"].isna()
                    & (prop_df["direction"] == "OVER")
                    & prop_df["line"].notna()
                )
                mask_under = (
                    prop_df["won_bool"].isna()
                    & (prop_df["direction"] == "UNDER")
                    & prop_df["line"].notna()
                )
                prop_df.loc[mask_over, "won_bool"] = prop_df.loc[mask_over, "actual"] > prop_df.loc[mask_over, "line"]
                prop_df.loc[mask_under, "won_bool"] = prop_df.loc[mask_under, "actual"] < prop_df.loc[mask_under, "line"]

                stats_df = prop_df[
                    prop_df["direction"].isin(["OVER", "UNDER"]) & prop_df["won_bool"].notna()
                ].copy()
                if not stats_df.empty:
                    grouped = stats_df.groupby(["market_norm", "direction", "won_bool"], as_index=False).agg(
                        n=("market_norm", "size")
                    )
                    for row in grouped.itertuples(index=False):
                        market = str(row.market_norm)
                        direction = str(row.direction)
                        won_value = bool(row.won_bool)
                        stats = market_stats.setdefault(
                            market, {"over_hits": 1, "over_misses": 1, "under_hits": 1, "under_misses": 1}
                        )
                        if direction == "OVER":
                            stat_key = "over_hits" if won_value else "over_misses"
                        else:
                            stat_key = "under_hits" if won_value else "under_misses"
                        stats[stat_key] = int(stats.get(stat_key, 0)) + int(row.n)

                meta["prop_samples"] = int(meta.get("prop_samples", 0)) + int(len(prop_df.index))

    if feedback_path:
        meta["last_feedback_file"] = str(Path(feedback_path))
    meta["learning_backend"] = "pandas"
    return state


def update_learning_state_from_feedback(state: dict, feedback_path: Optional[str]) -> dict:
    if not feedback_path:
        return state

    path = Path(feedback_path)
    if not path.exists():
        return state

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return state

    if pd is not None and os.environ.get("PETE_DISABLE_PANDAS_LEARNING", "0") != "1":
        try:
            return _update_learning_state_from_feedback_pandas(state, payload, feedback_path)
        except Exception:
            pass

    dfs_samples = payload.get("dfs", []) if isinstance(payload, dict) else []
    bet_samples = payload.get("bets", []) if isinstance(payload, dict) else []
    prop_samples = payload.get("props", []) if isinstance(payload, dict) else []

    player_adj = state.setdefault("player_adjustments", {})
    team_adj = state.setdefault("team_adjustments", {})
    prop_adj = state.setdefault("player_prop_adjustments", {})
    prop_opp_adj = state.setdefault("player_prop_opp_adjustments", {})
    market_stats = _coerce_market_stats(state.get("prop_market_stats"))
    state["prop_market_stats"] = market_stats
    meta = state.setdefault("meta", {})

    learning_rate_dfs = 0.08
    learning_rate_bet = 0.06

    for sample in dfs_samples if isinstance(dfs_samples, list) else []:
        if not isinstance(sample, dict):
            continue
        player = str(sample.get("player", "")).strip()
        projected = safe_float(sample.get("projected_fp"), 0.0)
        actual = safe_float(sample.get("actual_fp"), 0.0)
        if not player:
            continue

        error = actual - projected
        current = safe_float(player_adj.get(player, 0.0), 0.0)
        player_adj[player] = round(clamp(current + learning_rate_dfs * error, -8.0, 8.0), 4)
        meta["dfs_samples"] = int(meta.get("dfs_samples", 0)) + 1

    for sample in bet_samples if isinstance(bet_samples, list) else []:
        if not isinstance(sample, dict):
            continue
        team = str(sample.get("team", "")).strip()
        model_prob = safe_float(sample.get("model_prob"), 0.5)
        won = 1.0 if bool(sample.get("won", False)) else 0.0
        if not team:
            continue

        calibration_error = won - clamp(model_prob, 0.01, 0.99)
        key = normalize_team_key(team)
        current = safe_float(team_adj.get(key, 0.0), 0.0)
        team_adj[key] = round(clamp(current + learning_rate_bet * calibration_error, -0.12, 0.12), 6)
        meta["bet_samples"] = int(meta.get("bet_samples", 0)) + 1

    learning_rate_prop = 0.08
    for sample in prop_samples if isinstance(prop_samples, list) else []:
        if not isinstance(sample, dict):
            continue
        player = str(sample.get("player", "")).strip()
        market = normalize_market(sample.get("market"))
        projected = safe_float(sample.get("projected"), 0.0)
        actual = safe_float(sample.get("actual"), 0.0)
        if not player or not market:
            continue

        key = f"{player.lower()}::{market}"
        error = actual - projected
        current = safe_float(prop_adj.get(key, 0.0), 0.0)
        prop_adj[key] = round(clamp(current + learning_rate_prop * error, -3.0, 3.0), 4)

        opponent = normalize_team_key(sample.get("opponent", ""))
        if opponent:
            opp_key = f"{player.lower()}::{opponent}::{market}"
            opp_current = safe_float(prop_opp_adj.get(opp_key, 0.0), 0.0)
            prop_opp_adj[opp_key] = round(clamp(opp_current + (0.10 * error), -2.5, 2.5), 4)

        direction = str(sample.get("direction", "")).strip().upper()
        line = safe_float(sample.get("line"), math.nan)
        won_value = sample.get("won")
        if won_value is None and direction in {"OVER", "UNDER"} and not math.isnan(line):
            if direction == "OVER":
                won_value = actual > line
            else:
                won_value = actual < line

        if direction in {"OVER", "UNDER"} and won_value is not None:
            stats = market_stats.setdefault(
                market, {"over_hits": 1, "over_misses": 1, "under_hits": 1, "under_misses": 1}
            )
            if direction == "OVER":
                key_name = "over_hits" if bool(won_value) else "over_misses"
            else:
                key_name = "under_hits" if bool(won_value) else "under_misses"
            stats[key_name] = int(stats.get(key_name, 0)) + 1

        meta["prop_samples"] = int(meta.get("prop_samples", 0)) + 1

    if feedback_path:
        meta["last_feedback_file"] = str(Path(feedback_path))
    meta["learning_backend"] = "python"

    return state


def _split_positions(value: str) -> List[str]:
    text = str(value or "").replace(" ", "")
    if not text:
        return []
    return [part for part in text.split("/") if part]


def _draftstars_slot_filter(players: List[dict], slot: str) -> List[dict]:
    if slot == "all":
        return players

    with_times = [p for p in players if p.get("start_dt") is not None]
    if not with_times:
        return players

    sorted_times = sorted(p["start_dt"] for p in with_times)
    pivot = sorted_times[len(sorted_times) // 2]

    if slot == "early":
        return [p for p in players if p.get("start_dt") is None or p["start_dt"] <= pivot]
    return [p for p in players if p.get("start_dt") is None or p["start_dt"] > pivot]


def load_draftstars_players(csv_path: str, slot: str, learning_state: dict) -> Tuple[List[dict], int]:
    path = Path(csv_path)
    if not path.exists():
        return [], 100000

    rows: List[dict] = []
    player_adj = learning_state.get("player_adjustments", {}) if isinstance(learning_state, dict) else {}

    with path.open("r", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for raw in reader:
            status = str(raw.get("Playing Status", "")).upper()
            if "OUT" in status or "QUESTIONABLE" in status or "DOUBTFUL" in status:
                continue

            salary = int(safe_float(raw.get("Salary"), 0.0))
            if salary <= 0:
                continue

            fppg = safe_float(raw.get("FPPG"), 0.0)
            form = safe_float(raw.get("Form"), fppg)
            name = str(raw.get("Name", "")).strip()
            team = str(raw.get("Team", "")).strip()
            positions = _split_positions(raw.get("Position", ""))
            if not name or not positions:
                continue

            start_raw = raw.get("Start") or raw.get("Start Time") or raw.get("Game Start") or ""
            start_dt = _parse_time(start_raw)

            adjustment = safe_float(player_adj.get(name, 0.0), 0.0)
            projected = (0.65 * form) + (0.35 * fppg) + adjustment
            projected = round(max(projected, 0.0), 3)

            rows.append(
                {
                    "name": name,
                    "team": team,
                    "positions": positions,
                    "salary": salary,
                    "fppg": round(fppg, 3),
                    "form": round(form, 3),
                    "projected": projected,
                    "value_score": round((projected / salary) * 1000.0, 4),
                    "start_dt": start_dt,
                }
            )

    return _draftstars_slot_filter(rows, slot), 100000




def load_tank01_dfs_players(
    run_date: str,
    data_root: str,
    slot: str,
    learning_state: dict,
    max_lag_days: int = 2,
    projections_rows: Optional[list] = None,
    injury_status: Optional[dict] = None,
) -> Tuple[List[dict], int]:
    dfs_payload = load_tank01_dfs_pool(run_date, data_root, max_lag_days=max_lag_days)
    rows = dfs_payload.get("players", []) if isinstance(dfs_payload, dict) else []
    if not rows:
        return [], DEFAULT_SALARY_CAP

    projection_map = build_projection_map_from_tank01(projections_rows or [])
    injury_map = injury_status or {}
    player_adj = learning_state.get("player_adjustments", {}) if isinstance(learning_state, dict) else {}

    players: List[dict] = []
    max_salary = 0
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("longName") or raw.get("playerName") or raw.get("name") or "").strip()
        if not name:
            continue
        status_text = raw.get("injuryStatus") or raw.get("status") or ""
        status = classify_injury_status(status_text)
        if status in {"out", "doubtful"}:
            continue
        if injury_map.get(_canonical_player_name(name)) in {"out", "doubtful"}:
            continue

        salary = int(safe_float(raw.get("salary") or raw.get("Salary"), 0.0))
        if salary <= 0:
            continue
        max_salary = max(max_salary, salary)
        positions_raw = raw.get("position") or raw.get("pos") or raw.get("Position") or ""
        positions = _split_positions(str(positions_raw))
        if not positions:
            continue

        fppg = safe_float(raw.get("fppg") or raw.get("FPPG") or raw.get("avgFP") or raw.get("avgFantasyPoints"), 0.0)
        form = safe_float(raw.get("form") or raw.get("Form"), fppg)

        pid = str(raw.get("playerID") or raw.get("playerId") or raw.get("player_id") or "").strip()
        proj_fp = 0.0
        if pid and pid in projection_map:
            proj_fp = projection_map.get(pid, 0.0)
        if not proj_fp:
            proj_fp = projection_map.get(_canonical_player_name(name), 0.0)
        if proj_fp > 0:
            form = proj_fp

        adjustment = safe_float(player_adj.get(name, 0.0), 0.0)
        projected = (0.65 * form) + (0.35 * fppg) + adjustment
        projected = round(max(projected, 0.0), 3)

        start_raw = raw.get("Start") or raw.get("startTime") or raw.get("start") or ""
        start_dt = _parse_time(start_raw)
        team = str(raw.get("team") or raw.get("teamAbv") or raw.get("Team") or "").strip()

        players.append(
            {
                "name": name,
                "team": team,
                "positions": positions,
                "salary": salary,
                "fppg": round(fppg, 3),
                "form": round(form, 3),
                "projected": projected,
                "value_score": round((projected / salary) * 1000.0, 4),
                "start_dt": start_dt,
            }
        )

    salary_cap = DEFAULT_SALARY_CAP
    if max_salary and max_salary <= 12000:
        salary_cap = 50000

    return _draftstars_slot_filter(players, slot), salary_cap

def _lineup_optimizer(players: List[dict], salary_cap: int = 100000) -> List[dict]:
    slot_order = ["PG", "PG", "SG", "SG", "SF", "SF", "PF", "PF", "C"]

    candidates_by_slot: Dict[str, List[dict]] = {}
    for slot in set(slot_order):
        candidates = [p for p in players if slot in p.get("positions", [])]
        candidates.sort(key=lambda p: (p["projected"], p["value_score"]), reverse=True)
        candidates_by_slot[slot] = candidates[:22]

    for slot in set(slot_order):
        if not candidates_by_slot.get(slot):
            return []

    best_lineup: List[dict] = []
    best_projection = -1.0

    def dfs(idx: int, chosen: List[dict], used_names: Set[str], salary_used: int, projection_sum: float) -> None:
        nonlocal best_lineup, best_projection

        if salary_used > salary_cap:
            return

        if idx == len(slot_order):
            if projection_sum > best_projection:
                best_projection = projection_sum
                best_lineup = chosen.copy()
            return

        slot = slot_order[idx]
        remaining_slots = slot_order[idx:]
        optimistic_remaining = 0.0
        for rem in remaining_slots:
            top = candidates_by_slot.get(rem, [])
            if top:
                optimistic_remaining += top[0]["projected"]
        if projection_sum + optimistic_remaining <= best_projection:
            return

        for player in candidates_by_slot.get(slot, []):
            if player["name"] in used_names:
                continue

            used_names.add(player["name"])
            chosen.append({**player, "slot": slot})
            dfs(
                idx + 1,
                chosen,
                used_names,
                salary_used + player["salary"],
                projection_sum + player["projected"],
            )
            chosen.pop()
            used_names.remove(player["name"])

    dfs(0, [], set(), 0, 0.0)
    return best_lineup


def find_smokies(players: List[dict], rules: dict) -> List[dict]:
    if not players:
        return []

    sorted_salaries = sorted(p["salary"] for p in players)
    pct = clamp(safe_float(rules.get("smokie_percentile", 0.35), 0.35), 0.1, 0.6)
    index = max(0, min(len(sorted_salaries) - 1, int((len(sorted_salaries) - 1) * pct)))
    salary_cutoff = sorted_salaries[index]
    min_delta = safe_float(rules.get("smokie_min_projection_delta", 1.0), 1.0)

    candidates = []
    for player in players:
        if player["salary"] > salary_cutoff:
            continue
        delta = player["projected"] - player["fppg"]
        if delta < min_delta:
            continue
        score = delta + (0.25 * player["value_score"])
        candidates.append(
            {
                "player": player["name"],
                "team": player["team"],
                "salary": player["salary"],
                "fppg": player["fppg"],
                "projected": player["projected"],
                "delta": round(delta, 3),
                "value_score": player["value_score"],
                "score": round(score, 3),
            }
        )

    candidates.sort(key=lambda item: (item["score"], item["projected"]), reverse=True)
    return candidates[:3]


def build_best_lineup(
    _games_data: dict,
    draftstars_csv: Optional[str] = None,
    slot: str = "all",
    learning_state: Optional[dict] = None,
    rules: Optional[dict] = None,
    run_date: Optional[str] = None,
    tank01_data_root: Optional[str] = None,
    tank01_max_lag_days: int = 2,
    tank01_projections_rows: Optional[list] = None,
    tank01_injury_map: Optional[dict] = None,
    use_tank01_dfs: bool = False,
) -> dict:
    if not draftstars_csv and not use_tank01_dfs:
        return {
            "lineup": [],
            "format": "draftstars-classic",
            "total_salary": 0,
            "projected_points": 0,
            "smokies": [],
            "projection_map": {},
            "note": "No Draftstars CSV supplied. Pass --draftstars-csv or enable Tank01 DFS feed.",
        }

    state = learning_state or load_learning_state()
    quant_rules = rules or load_quant_rules()

    salary_cap = DEFAULT_SALARY_CAP
    if draftstars_csv:
        players, salary_cap = load_draftstars_players(draftstars_csv, slot, state)
        format_name = "draftstars-classic"
    else:
        players, salary_cap = load_tank01_dfs_players(
            run_date=str(run_date or TODAY),
            data_root=str(tank01_data_root or Path.cwd() / "projects" / "pete-dfs" / "data-lake"),
            slot=slot,
            learning_state=state,
            max_lag_days=max(0, tank01_max_lag_days),
            projections_rows=tank01_projections_rows,
            injury_status=(tank01_injury_map or {}).get("by_name", {}),
        )
        format_name = "tank01-dfs"

    if not players:
        return {
            "lineup": [],
            "format": format_name,
            "total_salary": 0,
            "projected_points": 0,
            "smokies": [],
            "projection_map": {},
            "note": "No eligible players after filtering.",
        }

    lineup = _lineup_optimizer(players, salary_cap=salary_cap)
    smokies = find_smokies(players, quant_rules)

    if not lineup:
        return {
            "lineup": [],
            "format": "draftstars-classic",
            "total_salary": 0,
            "projected_points": 0,
            "smokies": smokies,
            "projection_map": {p["name"].lower(): p["projected"] for p in players},
            "note": "Optimization failed to find a valid lineup under constraints.",
        }

    total_salary = sum(player["salary"] for player in lineup)
    projected_points = sum(player["projected"] for player in lineup)
    lineup_rows = [
        {
            "slot": player["slot"],
            "name": player["name"],
            "team": player["team"],
            "salary": player["salary"],
            "projected": player["projected"],
            "fppg": player["fppg"],
        }
        for player in lineup
    ]

    return {
        "lineup": lineup_rows,
        "format": f"{format_name}-2-2-2-2-1",
        "total_salary": total_salary,
        "projected_points": round(projected_points, 3),
        "smokies": smokies,
        "projection_map": {p["name"].lower(): p["projected"] for p in players},
        "note": f"Optimized from CSV using slot={slot}",
    }


def get_pivots(lineup: dict) -> List[dict]:
    smokies = lineup.get("smokies", []) if isinstance(lineup, dict) else []
    if len(smokies) >= 2:
        return [
            {
                "player": smokies[0]["player"],
                "reason": f"Smokie candidate: +{smokies[0]['delta']} vs FPPG at ${smokies[0]['salary']}",
            },
            {
                "player": smokies[1]["player"],
                "reason": f"Smokie candidate: +{smokies[1]['delta']} vs FPPG at ${smokies[1]['salary']}",
            },
        ]

    return [
        {"player": "TBD", "reason": "Awaiting stronger value delta from live slate"},
        {"player": "TBD", "reason": "Awaiting late injury confirmation"},
    ]


def decimal_to_aus(decimal_odds: float) -> str:
    if decimal_odds >= 2:
        return f"+{int((decimal_odds - 1) * 100)}"
    return f"-{int(100 / (decimal_odds - 1))}"


def wagering_enabled(rules: dict) -> bool:
    env_enabled = os.environ.get("PETE_ENABLE_WAGERING", "0") == "1"
    rules_enabled = bool(rules.get("enabled", False))
    return env_enabled and rules_enabled


def fetch_teams_played_on_date(season: str, date: str) -> Set[str]:
    if not date:
        return set()

    payload = fetch_nba_games(season, date)
    teams = set()
    for game in payload.get("games", []):
        for key in ["home_team", "away_team", "home_name", "away_name", "home_code", "away_code"]:
            value = game.get(key)
            if value:
                teams.add(normalize_team_key(value))
    return teams


def team_is_blocked(team: str, blocked_set: Set[str]) -> bool:
    return normalize_team_key(team) in blocked_set


def candidate_expected_return(model_prob: float, odds: float) -> float:
    return (model_prob * odds) - 1.0


def _is_home_team(team: str, game: dict) -> bool:
    key = normalize_team_key(team)
    return key in {normalize_team_key(game.get("home")), normalize_team_key(game.get("home_code"))}


def model_probability_for_team(team: str, game: dict, implied_prob: float, learning_state: dict, rules: dict) -> float:
    model = implied_prob
    if _is_home_team(team, game):
        model *= 1.0 + safe_float(rules.get("home_team_model_boost_pct", 0.10), 0.10)

    team_adjustments = learning_state.get("team_adjustments", {}) if isinstance(learning_state, dict) else {}
    model += safe_float(team_adjustments.get(normalize_team_key(team), 0.0), 0.0)

    model = max(model, safe_float(rules.get("min_model_prob", 0.52), 0.52))
    return clamp(model, 0.01, 0.95)


def _iter_candidates(
    odds_data: dict,
    rules: dict,
    learning_state: dict,
    no_b2b_teams: Optional[Set[str]] = None,
    major_out_teams: Optional[Set[str]] = None,
) -> Iterable[dict]:
    blocked_b2b = no_b2b_teams or set()
    blocked_major = major_out_teams or set()

    for game in odds_data.get("games", []):
        home_tags = {
            normalize_team_key(game.get("home")),
            normalize_team_key(game.get("home_code")),
        }
        away_tags = {
            normalize_team_key(game.get("away")),
            normalize_team_key(game.get("away_code")),
        }

        if (home_tags & blocked_b2b) or (away_tags & blocked_b2b):
            continue
        if (home_tags & blocked_major) or (away_tags & blocked_major):
            continue

        odds = game.get("odds", {})
        for team, odd in odds.items():
            price = safe_float(odd, 0.0)
            if price <= 1.0 or price > safe_float(rules.get("max_single_bet_decimal_odds", 3.0), 3.0):
                continue

            implied = 1.0 / price
            model = model_probability_for_team(team, game, implied, learning_state, rules)
            edge = model - implied
            expected_return = candidate_expected_return(model, price)

            yield {
                "pick": team,
                "odds": price,
                "aus_odds": decimal_to_aus(price),
                "implied_prob": implied,
                "model_prob": model,
                "edge": edge,
                "edge_dollars_per_1u": expected_return,
                "game": f"{game.get('away')} @ {game.get('home')}",
            }


def build_parlay(
    _games_data: dict,
    odds_data: dict,
    rules: dict,
    learning_state: Optional[dict] = None,
    no_b2b_teams: Optional[Set[str]] = None,
    major_out_teams: Optional[Set[str]] = None,
) -> dict:
    if not wagering_enabled(rules):
        return {
            "legs": [],
            "total_odds": 0,
            "edge_notes": "NO_PARLAY: wagering disabled until quant controls are enabled",
        }

    state = learning_state or load_learning_state()
    min_edge_pct = safe_float(rules.get("parlay_min_edge_pct", rules.get("min_edge_pct", 0.03)), 0.03)
    min_edge_dollars = safe_float(
        rules.get("parlay_min_edge_dollars_per_1u", rules.get("min_edge_dollars_per_1u", 0.40)),
        0.40,
    )
    min_parlay_legs = int(max(1, safe_float(rules.get("parlay_min_legs", 2), 2)))

    candidates = [
        c
        for c in _iter_candidates(
            odds_data,
            rules,
            state,
            no_b2b_teams=no_b2b_teams,
            major_out_teams=major_out_teams,
        )
        if c["edge"] >= min_edge_pct and c["edge_dollars_per_1u"] >= min_edge_dollars
    ]

    candidates.sort(key=lambda c: (c["edge_dollars_per_1u"], c["edge"]), reverse=True)

    legs = []
    seen_games = set()
    for candidate in candidates:
        if candidate["game"] in seen_games:
            continue
        seen_games.add(candidate["game"])
        legs.append(
            {
                "team": candidate["pick"],
                "odds": candidate["odds"],
                "aus_odds": candidate["aus_odds"],
                "game": candidate["game"],
                "edge": round(candidate["edge"], 4),
                "edge_dollars_per_1u": round(candidate["edge_dollars_per_1u"], 4),
            }
        )
        if len(legs) >= int(rules.get("max_parlay_legs", 3)):
            break

    total_odds = 1.0
    for leg in legs:
        total_odds *= leg["odds"]

    if len(legs) < min_parlay_legs:
        return {
            "legs": [],
            "total_odds": 0,
            "edge_notes": f"NO_PARLAY: fewer than {min_parlay_legs} eligible legs",
        }

    return {
        "legs": legs,
        "total_odds": round(total_odds, 2) if legs else 0,
        "edge_notes": "Quant-gated candidate parlay" if legs else "NO_PARLAY: no eligible legs",
    }


def get_bet_pick(
    _games_data: dict,
    odds_data: dict,
    rules: dict = None,
    learning_state: Optional[dict] = None,
    no_b2b_teams: Optional[Set[str]] = None,
    major_out_teams: Optional[Set[str]] = None,
) -> dict:
    rules = rules or load_quant_rules()

    if not wagering_enabled(rules):
        return {
            "pick": "NO_BET",
            "odds": 0,
            "aus_odds": "N/A",
            "implied_prob": 0,
            "model_prob": 0,
            "edge": 0,
            "edge_dollars_per_1u": 0,
            "band": "N/A",
            "game": "N/A",
            "reason": "Wagering disabled. Set PETE_ENABLE_WAGERING=1 and enable quant_rules.json",
        }

    state = learning_state or load_learning_state()
    min_edge_pct = safe_float(rules.get("min_edge_pct", 0.03), 0.03)
    min_edge_dollars = safe_float(rules.get("min_edge_dollars_per_1u", 0.40), 0.40)

    all_candidates = list(
        _iter_candidates(
            odds_data,
            rules,
            state,
            no_b2b_teams=no_b2b_teams,
            major_out_teams=major_out_teams,
        )
    )
    if not all_candidates:
        return {
            "pick": "NO_BET",
            "odds": 0,
            "aus_odds": "N/A",
            "implied_prob": 0,
            "model_prob": 0,
            "edge": 0,
            "edge_dollars_per_1u": 0,
            "band": "N/A",
            "game": "N/A",
            "reason": "No eligible teams after B2B/major-out and odds filters",
        }

    edge_pass = [candidate for candidate in all_candidates if candidate["edge"] >= min_edge_pct]
    if not edge_pass:
        best_edge = max(candidate["edge"] for candidate in all_candidates)
        return {
            "pick": "NO_BET",
            "odds": 0,
            "aus_odds": "N/A",
            "implied_prob": 0,
            "model_prob": 0,
            "edge": 0,
            "edge_dollars_per_1u": 0,
            "band": "N/A",
            "game": "N/A",
            "reason": f"No candidate met min_edge_pct ({min_edge_pct * 100:.2f}%); best was {best_edge * 100:.2f}%",
        }

    ev_pass = [candidate for candidate in edge_pass if candidate["edge_dollars_per_1u"] >= min_edge_dollars]
    if not ev_pass:
        best_ev = max(candidate["edge_dollars_per_1u"] for candidate in edge_pass)
        return {
            "pick": "NO_BET",
            "odds": 0,
            "aus_odds": "N/A",
            "implied_prob": 0,
            "model_prob": 0,
            "edge": 0,
            "edge_dollars_per_1u": 0,
            "band": "N/A",
            "game": "N/A",
            "reason": f"No candidate met min_edge_dollars_per_1u ({min_edge_dollars:.2f}); best was {best_ev:.2f}",
        }

    best = None
    for candidate in ev_pass:
        candidate["band"] = "rule-gated"
        candidate["reason"] = "Candidate passed quant, edge, and risk filters"
        if best is None or candidate["edge_dollars_per_1u"] > best["edge_dollars_per_1u"]:
            best = candidate

    return best


def normalize_market(value) -> str:
    token = str(value or "").strip().lower().replace(" ", "")
    aliases = {
        "points": "PTS",
        "pts": "PTS",
        "rebounds": "REB",
        "reb": "REB",
        "assists": "AST",
        "ast": "AST",
        "steals": "STL",
        "stl": "STL",
        "blocks": "BLK",
        "blk": "BLK",
        "turnovers": "TOV",
        "turnover": "TOV",
        "to": "TOV",
        "tov": "TOV",
        "threes": "3PM",
        "3ptm": "3PM",
        "3pm": "3PM",
        "threepointersmade": "3PM",
        "ptsreb": "PR",
        "pr": "PR",
        "ptsast": "PA",
        "pa": "PA",
        "rebast": "RA",
        "ra": "RA",
        "ptsrebast": "PRA",
        "pra": "PRA",
        "stlblk": "SB",
        "sb": "SB",
    }
    return aliases.get(token, token.upper())


def market_prior_success_rate(learning_state: dict, market: str, direction: str) -> float:
    stats_map = _coerce_market_stats(learning_state.get("prop_market_stats", {}))
    stats = stats_map.get(normalize_market(market), {})
    if str(direction).upper() == "OVER":
        hits = int(stats.get("over_hits", 1))
        misses = int(stats.get("over_misses", 1))
    else:
        hits = int(stats.get("under_hits", 1))
        misses = int(stats.get("under_misses", 1))
    return clamp(hits / max(1, hits + misses), 0.05, 0.95)


def build_learning_summary(learning_state: dict) -> dict:
    meta = learning_state.get("meta", {}) if isinstance(learning_state, dict) else {}
    player_adj = learning_state.get("player_adjustments", {}) if isinstance(learning_state, dict) else {}
    prop_adj = learning_state.get("player_prop_adjustments", {}) if isinstance(learning_state, dict) else {}

    top_dfs = sorted(player_adj.items(), key=lambda item: abs(safe_float(item[1])), reverse=True)[:3]
    top_prop = sorted(prop_adj.items(), key=lambda item: abs(safe_float(item[1])), reverse=True)[:3]

    return {
        "dfs_samples": int(meta.get("dfs_samples", 0)),
        "bet_samples": int(meta.get("bet_samples", 0)),
        "prop_samples": int(meta.get("prop_samples", 0)),
        "learning_backend": str(meta.get("learning_backend", "python")),
        "last_feedback_file": str(meta.get("last_feedback_file", "")),
        "top_dfs_adjustments": [{"player": k, "adj": round(safe_float(v), 4)} for k, v in top_dfs],
        "top_prop_adjustments": [{"player_market": k, "adj": round(safe_float(v), 4)} for k, v in top_prop],
    }




def write_run_snapshot(run_date: str, payload: dict) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{run_date}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def load_run_snapshot(run_date: str) -> dict:
    path = LOG_DIR / f"{run_date}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _extract_scores_results(rows: list) -> dict:
    results = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        home = normalize_team_code(row.get("homeTeam") or row.get("home") or row.get("homeTeamAbv") or "")
        away = normalize_team_code(row.get("awayTeam") or row.get("away") or row.get("awayTeamAbv") or "")
        if not home or not away:
            continue
        home_score = safe_float(row.get("homeScore") or row.get("homePts") or row.get("homePoints"), math.nan)
        away_score = safe_float(row.get("awayScore") or row.get("awayPts") or row.get("awayPoints"), math.nan)
        if math.isnan(home_score) or math.isnan(away_score):
            continue
        winner = home if home_score > away_score else away
        results[(home, away)] = winner
        results[(away, home)] = winner
    return results


def _tank01_game_log_actuals(payload: dict, target_date: str) -> Optional[dict]:
    rows = _tank01_body_list(payload)
    if not rows:
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        date_raw = str(row.get("gameDate") or row.get("date") or row.get("game_date") or "").strip()
        date_raw = _compact_to_dash_date(date_raw)
        if date_raw != target_date:
            continue
        stats = {
            "pts": safe_float(row.get("pts") or row.get("points"), 0.0),
            "reb": safe_float(row.get("reb") or row.get("rebounds"), 0.0),
            "ast": safe_float(row.get("ast") or row.get("assists"), 0.0),
            "stl": safe_float(row.get("stl") or row.get("steals"), 0.0),
            "blk": safe_float(row.get("blk") or row.get("blocks"), 0.0),
            "tov": safe_float(row.get("tov") or row.get("turnovers"), 0.0),
            "3pm": safe_float(row.get("threePM") or row.get("3PM") or row.get("threes"), 0.0),
        }
        fp = (
            stats["pts"] * POINTS_WEIGHT
            + stats["reb"] * REBOUNDS_WEIGHT
            + stats["ast"] * ASSISTS_WEIGHT
            + stats["stl"] * STEALS_WEIGHT
            + stats["blk"] * BLOCKS_WEIGHT
            + stats["tov"] * TURNOVERS_WEIGHT
        )
        return {"fp": fp, **stats}
    return None


def build_tank01_feedback(run_date: str, data_root: str) -> Optional[Path]:
    snapshot = load_run_snapshot(run_date)
    if not snapshot:
        return None

    projection_map = snapshot.get("projection_map", {}) if isinstance(snapshot.get("projection_map"), dict) else {}
    bet_pick = snapshot.get("bet", {}) if isinstance(snapshot.get("bet"), dict) else {}

    players_index = load_tank01_players_index(run_date, data_root, max_lag_days=3)
    if not players_index.get("by_id") and tank01_api_key():
        payload = tank01_get("getNBAPlayerList", {})
        _save_tank01_snapshot(payload, data_root, "players", run_date)
        players_index = load_tank01_players_index(run_date, data_root, max_lag_days=3)
    name_to_id = {}
    for pid, meta in players_index.get("by_id", {}).items():
        name = _canonical_player_name(meta.get("name", ""))
        if name:
            name_to_id[name] = pid

    dfs_samples = []
    for name, projected in projection_map.items():
        canonical = _canonical_player_name(name)
        pid = name_to_id.get(canonical)
        if not pid:
            continue
        payload = tank01_get("getNBAGamesForPlayer", {"playerID": pid})
        actual = _tank01_game_log_actuals(payload, run_date)
        if not actual:
            continue
        dfs_samples.append({"player": name, "projected_fp": projected, "actual_fp": round(actual["fp"], 4)})

    scores = load_tank01_scores(run_date, data_root, max_lag_days=3)
    if not scores.get("rows") and tank01_api_key():
        payload = tank01_get("getNBAScoresOnly", {"gameDate": _dash_to_compact_date(run_date)})
        _save_tank01_snapshot(payload, data_root, "scores", run_date)
        scores = load_tank01_scores(run_date, data_root, max_lag_days=3)
    score_rows = scores.get("rows", []) if isinstance(scores, dict) else []
    winners = _extract_scores_results(score_rows)
    bet_samples = []
    pick = str(bet_pick.get("pick", "")).strip()
    if pick and pick != "NO_BET":
        home = normalize_team_code(pick)
        won = None
        for (team_a, team_b), winner in winners.items():
            if home in {team_a, team_b}:
                won = winner == home
                break
        if won is not None:
            bet_samples.append({"team": pick, "model_prob": bet_pick.get("model_prob", 0.5), "won": won})

    payload = {"dfs": dfs_samples, "bets": bet_samples, "props": []}
    if not dfs_samples and not bet_samples:
        return None

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"feedback-{run_date}.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path

def build_prop_game_context(odds_data: dict) -> dict:
    context = {}
    for game in odds_data.get("games", []):
        if not isinstance(game, dict):
            continue
        home = normalize_team_code(game.get("home_code") or game.get("home"))
        away = normalize_team_code(game.get("away_code") or game.get("away"))
        if not home or not away:
            continue

        market_ctx = game.get("market_context", {}) if isinstance(game.get("market_context"), dict) else {}
        consensus_total = safe_float(market_ctx.get("consensus_total"), 0.0)
        spread_by_team = market_ctx.get("spread_by_team", {}) if isinstance(market_ctx.get("spread_by_team"), dict) else {}
        home_spread = safe_float(spread_by_team.get(home), math.nan)
        away_spread = safe_float(spread_by_team.get(away), math.nan)

        # Derive spread proxy from moneyline when spread missing.
        odds = game.get("odds", {}) if isinstance(game.get("odds"), dict) else {}
        home_price = safe_float(odds.get(home), math.nan)
        away_price = safe_float(odds.get(away), math.nan)
        if (math.isnan(home_spread) or math.isnan(away_spread)) and home_price > 1.0 and away_price > 1.0:
            home_prob = 1.0 / home_price
            away_prob = 1.0 / away_price
            spread_proxy = (away_prob - home_prob) * 20.0
            if math.isnan(home_spread):
                home_spread = round(spread_proxy, 3)
            if math.isnan(away_spread):
                away_spread = round(-spread_proxy, 3)

        context[f"{normalize_team_key(home)}::{normalize_team_key(away)}"] = {
            "team": home,
            "opponent": away,
            "consensus_total": consensus_total,
            "team_spread": home_spread,
        }
        context[f"{normalize_team_key(away)}::{normalize_team_key(home)}"] = {
            "team": away,
            "opponent": home,
            "consensus_total": consensus_total,
            "team_spread": away_spread,
        }
    return context


def prop_game_context_bias(candidate: dict, direction: str, game_context: dict, rules: dict) -> float:
    team = normalize_team_key(candidate.get("team", ""))
    opponent = normalize_team_key(candidate.get("opponent", ""))
    market = normalize_market(candidate.get("market"))
    ctx = game_context.get(f"{team}::{opponent}", {})
    if not ctx:
        return 0.0

    total = safe_float(ctx.get("consensus_total"), 0.0)
    spread = safe_float(ctx.get("team_spread"), math.nan)

    bias_step = clamp(safe_float(rules.get("prop_context_bias_step", 0.02), 0.02), 0.0, 0.05)
    max_bias = clamp(safe_float(rules.get("prop_context_max_bias", 0.05), 0.05), 0.0, 0.10)
    total_over_th = safe_float(rules.get("prop_context_total_over_threshold", 233.0), 233.0)
    total_under_th = safe_float(rules.get("prop_context_total_under_threshold", 220.0), 220.0)
    favorite_spread_th = safe_float(rules.get("prop_context_favorite_spread_threshold", 6.5), 6.5)

    scoring_markets = {"PTS", "AST", "3PM"}
    market_weight = 1.0 if market in scoring_markets else 0.5

    bias = 0.0
    if total >= total_over_th:
        bias += bias_step if str(direction).upper() == "OVER" else -bias_step
    elif total > 0 and total <= total_under_th:
        bias += bias_step if str(direction).upper() == "UNDER" else -bias_step

    if not math.isnan(spread):
        if spread <= -favorite_spread_th:
            bias += (bias_step * market_weight) if str(direction).upper() == "OVER" else -(bias_step * market_weight)
        elif spread >= favorite_spread_th:
            underdog_weight = 0.5 * market_weight
            bias += (bias_step * underdog_weight) if str(direction).upper() == "UNDER" else -(bias_step * underdog_weight)

    return clamp(bias, -max_bias, max_bias)


def _prop_history_values(prop: dict, h2h_lookup: dict) -> List[float]:
    direct = (
        prop.get("last5_vs_opp")
        or prop.get("last5_vs_opponent")
        or prop.get("last_5")
        or prop.get("history")
        or []
    )
    values = [safe_float(v, math.nan) for v in direct if str(v).strip() != ""]
    values = [v for v in values if not math.isnan(v)]
    if values:
        return values

    player_key = str(prop.get("player", "")).strip().lower()
    team_key = normalize_team_key(prop.get("team", ""))
    opp_key = normalize_team_key(prop.get("opponent", ""))
    market = normalize_market(prop.get("market"))
    return h2h_lookup.get(f"{player_key}::{team_key}::{opp_key}::{market}", [])


def _seed_history_from_line(line: float, samples: int = 5) -> List[float]:
    anchor = max(0.0, safe_float(line, 0.0))
    if anchor <= 0.0:
        return []
    multipliers = [0.96, 1.04, 0.98, 1.02, 1.00]
    seeded = [round(anchor * m, 3) for m in multipliers[: max(1, samples)]]
    while len(seeded) < samples:
        seeded.append(round(anchor, 3))
    return seeded[:samples]


def _history_value_entry(raw) -> Optional[dict]:
    if isinstance(raw, dict):
        value = math.nan
        for key in ["value", "stat", "points", "rebounds", "assists", "steals", "three_pm"]:
            if key not in raw:
                continue
            value = safe_float(raw.get(key), math.nan)
            if not math.isnan(value):
                break
        if math.isnan(value):
            return None
        minutes = safe_float(raw.get("minutes"), math.nan)
        game_date = str(raw.get("game_date", "")).strip()
        return {"value": value, "minutes": minutes, "game_date": game_date}

    value = safe_float(raw, math.nan)
    if math.isnan(value):
        return None
    return {"value": value, "minutes": math.nan, "game_date": ""}


def _normalize_history_entries(values: List) -> List[dict]:
    entries: List[dict] = []
    for raw in values if isinstance(values, list) else []:
        entry = _history_value_entry(raw)
        if entry is None:
            continue
        entries.append(entry)

    # Sort newest-first when we have game dates.
    with_dates = [e for e in entries if e.get("game_date")]
    if len(with_dates) >= 2:
        entries.sort(key=lambda e: e.get("game_date", ""), reverse=True)
    return entries


def _filter_injury_noise(entries: List[dict], min_abs_minutes: float = 12.0, min_ratio_median: float = 0.55) -> Tuple[List[dict], dict]:
    if not entries:
        return [], {"removed": 0, "minutes_floor": 0.0}

    minutes = [safe_float(row.get("minutes"), math.nan) for row in entries]
    minutes = [m for m in minutes if not math.isnan(m) and m > 0.0]
    if not minutes:
        return entries, {"removed": 0, "minutes_floor": 0.0}

    median_minutes = float(statistics.median(minutes))
    minutes_floor = max(float(min_abs_minutes), median_minutes * float(min_ratio_median))
    filtered = [
        row for row in entries if math.isnan(safe_float(row.get("minutes"), math.nan)) or safe_float(row.get("minutes"), 0.0) >= minutes_floor
    ]
    removed = max(0, len(entries) - len(filtered))
    if not filtered:
        return entries, {"removed": 0, "minutes_floor": round(minutes_floor, 2)}
    return filtered, {"removed": removed, "minutes_floor": round(minutes_floor, 2)}


def _finalize_history_window(values: List, target_size: int = 5) -> Tuple[List[float], dict]:
    entries = _normalize_history_entries(values)
    filtered, meta = _filter_injury_noise(entries)
    window = filtered[: max(1, target_size)]

    # If filtering got too aggressive, backfill from original samples.
    if len(window) < target_size and entries:
        seen = {(row.get("game_date", ""), row.get("value")) for row in window}
        for row in entries:
            key = (row.get("game_date", ""), row.get("value"))
            if key in seen:
                continue
            window.append(row)
            seen.add(key)
            if len(window) >= target_size:
                break

    return [float(row.get("value")) for row in window[:target_size]], meta


def _collect_local_recent_market_history(data_root: str, max_games: int = 5) -> dict:
    root = Path(data_root)
    lookup: Dict[str, List[dict]] = {}
    pattern = "nba/season=*/processed/player_boxscore_jsonl/*.jsonl"
    for file_path in root.glob(pattern):
        try:
            parsed_rows = []
            for line in file_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    continue
                parsed_rows.append(row)

            teams_in_game = sorted(
                {
                    normalize_team_key(normalize_team_code(row.get("team", "")))
                    for row in parsed_rows
                    if normalize_team_key(normalize_team_code(row.get("team", "")))
                }
            )
            opponent_by_team = {}
            if len(teams_in_game) == 2:
                opponent_by_team = {teams_in_game[0]: teams_in_game[1], teams_in_game[1]: teams_in_game[0]}

            for row in parsed_rows:
                name = str(row.get("player_name", "")).strip().lower()
                team = normalize_team_key(normalize_team_code(row.get("team", "")))
                opponent = opponent_by_team.get(team, "")
                game_date = str(row.get("game_date", "")).strip()
                if not name or not team or not game_date:
                    continue
                minutes = safe_float(row.get("minutes"), math.nan)
                values = {
                    "PTS": safe_float(row.get("points"), math.nan),
                    "REB": safe_float(row.get("rebounds"), math.nan),
                    "AST": safe_float(row.get("assists"), math.nan),
                    "STL": safe_float(row.get("steals"), math.nan),
                    "BLK": safe_float(row.get("blocks"), math.nan),
                    "TOV": safe_float(row.get("turnovers"), math.nan),
                    "3PM": safe_float(row.get("three_pm"), math.nan),
                }
                points = values.get("PTS", math.nan)
                rebounds = values.get("REB", math.nan)
                assists = values.get("AST", math.nan)
                steals = values.get("STL", math.nan)
                blocks = values.get("BLK", math.nan)
                if not math.isnan(points) and not math.isnan(rebounds):
                    values["PR"] = points + rebounds
                if not math.isnan(points) and not math.isnan(assists):
                    values["PA"] = points + assists
                if not math.isnan(rebounds) and not math.isnan(assists):
                    values["RA"] = rebounds + assists
                if not math.isnan(points) and not math.isnan(rebounds) and not math.isnan(assists):
                    values["PRA"] = points + rebounds + assists
                if not math.isnan(steals) and not math.isnan(blocks):
                    values["SB"] = steals + blocks
                for market, value in values.items():
                    if math.isnan(value):
                        continue
                    entry = {"game_date": game_date, "value": float(value), "minutes": minutes}
                    key = f"{name}::{team}::{market}"
                    lookup.setdefault(key, []).append(entry)
                    if opponent:
                        opp_key = f"{name}::{team}::{opponent}::{market}"
                        lookup.setdefault(opp_key, []).append(entry)
        except Exception:
            continue

    compact = {}
    for key, rows in lookup.items():
        rows.sort(key=lambda item: item.get("game_date", ""), reverse=True)
        compact[key] = rows[: max(max_games, 10)]
    return compact


def load_h2h_lookup(path: str) -> dict:
    if not path:
        return {}
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    rows = payload.get("matchups", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return {}

    lookup = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        player = str(row.get("player", "")).strip().lower()
        team = normalize_team_key(row.get("team", ""))
        opp = normalize_team_key(row.get("opponent", ""))
        market = normalize_market(row.get("market"))
        values = [safe_float(v, math.nan) for v in row.get("values", []) if str(v).strip() != ""]
        values = [v for v in values if not math.isnan(v)]
        if not player or not team or not opp or not market or not values:
            continue
        lookup[f"{player}::{team}::{opp}::{market}"] = values
    return lookup


def load_prop_candidates(props_json_path: str, h2h_json_path: str = "") -> List[dict]:
    if not props_json_path:
        return []
    props_path = Path(props_json_path)
    if not props_path.exists():
        return []

    try:
        payload = json.loads(props_path.read_text(encoding="utf-8"))
    except Exception:
        return []

    games = payload.get("games", []) if isinstance(payload, dict) else []
    if not isinstance(games, list):
        return []

    h2h_lookup = load_h2h_lookup(h2h_json_path)
    candidates: List[dict] = []

    for game in games:
        if not isinstance(game, dict):
            continue
        home = str(game.get("home", "")).strip()
        away = str(game.get("away", "")).strip()
        props = game.get("props", [])
        if not isinstance(props, list):
            continue

        for prop in props:
            if not isinstance(prop, dict):
                continue
            player = str(prop.get("player", "")).strip()
            team = str(prop.get("team", "")).strip()
            if not player or not team:
                continue

            opponent = str(prop.get("opponent", "")).strip() or (away if normalize_team_key(team) == normalize_team_key(home) else home)
            market = normalize_market(prop.get("market"))
            if market not in {"PTS", "REB", "AST", "STL", "BLK", "TOV", "3PM", "PR", "PA", "RA", "PRA", "SB"}:
                continue

            line = safe_float(prop.get("line"), math.nan)
            if math.isnan(line):
                continue

            history = _prop_history_values({**prop, "opponent": opponent, "market": market}, h2h_lookup)
            if len(history) < 5:
                continue

            candidates.append(
                {
                    "player": player,
                    "team": team,
                    "opponent": opponent,
                    "market": market,
                    "line": line,
                    "odds_over": safe_float(prop.get("odds_over"), safe_float(prop.get("odds"), 1.87)),
                    "odds_under": safe_float(prop.get("odds_under"), safe_float(prop.get("odds"), 1.87)),
                    "last5": history[:5],
                    "game": f"{away} @ {home}",
                }
            )
    return candidates


def load_tank01_prop_candidates(
    run_date: str,
    data_root: str,
    h2h_json_path: str = "",
    max_lag_days: int = 2,
    default_odds: float = 1.87,
    explicit_props_json: str = "",
    explicit_players_json: str = "",
) -> Tuple[List[dict], dict]:
    players_index = load_tank01_players_index(
        run_date,
        data_root,
        max_lag_days=max_lag_days,
        explicit_players_json=explicit_players_json,
    )
    by_id = players_index.get("by_id", {})

    if explicit_props_json:
        props_path = Path(explicit_props_json)
        lag_days = 0
    else:
        props_path, lag_days = _resolve_dated_json(Path(data_root) / "nba" / "betting-props", run_date, max_lag_days)
    if props_path is None:
        return [], {"source": "", "source_lag_days": -1, "candidates": 0}

    payload = _load_json(props_path)
    games = payload.get("body", []) if isinstance(payload, dict) else []
    if not isinstance(games, list):
        return [], {"source": str(props_path), "source_lag_days": int(lag_days), "candidates": 0}

    h2h_lookup = load_h2h_lookup(h2h_json_path)
    local_history = _collect_local_recent_market_history(data_root)
    candidates: List[dict] = []
    synthetic_history_candidates = 0
    history_noise_removed_total = 0
    for game in games:
        if not isinstance(game, dict):
            continue
        home = normalize_team_code(game.get("homeTeam", ""))
        away = normalize_team_code(game.get("awayTeam", ""))
        if not home or not away:
            continue
        props = game.get("playerProps", [])
        if not isinstance(props, list):
            continue

        for row in props:
            if not isinstance(row, dict):
                continue
            player_id = str(row.get("playerID", "")).strip()
            player_meta = by_id.get(player_id, {})
            player = str(player_meta.get("name", "")).strip()
            team = normalize_team_code(player_meta.get("team", ""))
            if not player or not team:
                continue
            opponent = away if normalize_team_key(team) == normalize_team_key(home) else home

            bets = row.get("propBets", {}) if isinstance(row.get("propBets"), dict) else {}
            for raw_market, market in TANK01_MARKET_MAP.items():
                line = safe_float(bets.get(raw_market), math.nan)
                if math.isnan(line):
                    continue

                history_source = "h2h"
                history = _prop_history_values(
                    {
                        "player": player,
                        "team": team,
                        "opponent": opponent,
                        "market": market,
                    },
                    h2h_lookup,
                )
                if len(history) < 5:
                    local_h2h_key = f"{player.lower()}::{normalize_team_key(team)}::{normalize_team_key(opponent)}::{market}"
                    history = local_history.get(local_h2h_key, [])
                    if len(history) >= 5:
                        history_source = "local_h2h"
                if len(history) < 5:
                    local_key = f"{player.lower()}::{normalize_team_key(team)}::{market}"
                    history = local_history.get(local_key, [])
                    if len(history) >= 5:
                        history_source = "local_recent"

                history, history_meta = _finalize_history_window(history, target_size=5)
                history_noise_removed_total += int(history_meta.get("removed", 0))
                if len(history) < 5:
                    history = _seed_history_from_line(line, samples=5)
                    history_source = "synthetic_line"
                if len(history) < 5:
                    continue
                if history_source == "synthetic_line":
                    synthetic_history_candidates += 1

                candidates.append(
                    {
                        "player": player,
                        "team": team,
                        "opponent": opponent,
                        "market": market,
                        "line": line,
                        "odds_over": max(1.01, safe_float(default_odds, 1.87)),
                        "odds_under": max(1.01, safe_float(default_odds, 1.87)),
                        "last5": history[:5],
                        "game": f"{away} @ {home}",
                        "source": "tank01",
                        "history_source": history_source,
                        "history_noise_removed": int(history_meta.get("removed", 0)),
                        "history_minutes_floor": float(history_meta.get("minutes_floor", 0.0)),
                        "player_id": player_id,
                    }
                )

    return candidates, {
        "source": str(props_path),
        "source_lag_days": int(lag_days),
        "candidates": len(candidates),
        "players_mapped": len(by_id),
        "synthetic_history_candidates": synthetic_history_candidates,
        "history_noise_removed_total": history_noise_removed_total,
    }


def merge_prop_candidates(primary: List[dict], secondary: List[dict]) -> List[dict]:
    merged = {}
    for row in (primary or []) + (secondary or []):
        if not isinstance(row, dict):
            continue
        key = (
            str(row.get("player", "")).strip().lower(),
            normalize_team_key(row.get("team", "")),
            normalize_team_key(row.get("opponent", "")),
            normalize_market(row.get("market")),
        )
        if not all(key):
            continue
        current = merged.get(key)
        if current is None:
            merged[key] = row
            continue
        # Prefer candidate with longer history; then higher available odds.
        current_hist = len(current.get("last5", []))
        new_hist = len(row.get("last5", []))
        current_best_odds = max(safe_float(current.get("odds_over"), 0.0), safe_float(current.get("odds_under"), 0.0))
        new_best_odds = max(safe_float(row.get("odds_over"), 0.0), safe_float(row.get("odds_under"), 0.0))
        if (new_hist, new_best_odds) > (current_hist, current_best_odds):
            merged[key] = row
    return list(merged.values())


def summarize_prop_markets(candidates: List[dict]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in candidates if isinstance(candidates, list) else []:
        if not isinstance(row, dict):
            continue
        market = normalize_market(row.get("market"))
        if not market:
            continue
        counts[market] = int(counts.get(market, 0)) + 1
    return dict(sorted(counts.items(), key=lambda item: item[0]))


def _prop_projection(candidate: dict, rules: dict, learning_state: dict, dfs_projection_map: dict) -> dict:
    values = candidate.get("last5", [])
    avg_last5 = sum(values) / len(values)
    trend = (values[-1] - values[0]) / max(1, len(values) - 1)

    player_key = str(candidate.get("player", "")).strip().lower()
    market = normalize_market(candidate.get("market"))
    prop_adj = learning_state.get("player_prop_adjustments", {}) if isinstance(learning_state, dict) else {}
    prop_opp_adj = learning_state.get("player_prop_opp_adjustments", {}) if isinstance(learning_state, dict) else {}
    learned = safe_float(prop_adj.get(f"{player_key}::{market}", 0.0), 0.0)
    opponent_key = normalize_team_key(candidate.get("opponent", ""))
    learned_opp = safe_float(prop_opp_adj.get(f"{player_key}::{opponent_key}::{market}", 0.0), 0.0)

    dfs_proj = safe_float(dfs_projection_map.get(player_key, 0.0), 0.0)
    dfs_bonus = clamp((dfs_proj - 30.0) * 0.015, -0.4, 0.4) if dfs_proj > 0 else 0.0

    trend_weight = safe_float(rules.get("prop_trend_weight", 0.35), 0.35)
    projected = avg_last5 + (trend * trend_weight) + learned + learned_opp + dfs_bonus

    haircut = clamp(safe_float(rules.get("prop_call_haircut_pct", 0.10), 0.10), 0.10, 0.35)

    return {
        "avg_last5": avg_last5,
        "trend": trend,
        "learned_adj": learned,
        "learned_opp_adj": learned_opp,
        "dfs_bonus": dfs_bonus,
        "projected": projected,
        "haircut_pct": haircut,
    }


def build_player_prop_parlay(
    prop_candidates: List[dict],
    rules: dict,
    learning_state: Optional[dict] = None,
    dfs_projection_map: Optional[dict] = None,
    odds_data: Optional[dict] = None,
) -> dict:
    if not wagering_enabled(rules):
        return {"legs": [], "total_odds": 0, "note": "NO_PROP_PARLAY: wagering disabled"}
    if not prop_candidates:
        return {"legs": [], "total_odds": 0, "note": "NO_PROP_PARLAY: no eligible prop candidates"}

    state = learning_state or load_learning_state()
    projection_map = dfs_projection_map or {}
    game_context = build_prop_game_context(odds_data or {})
    min_line_edge = safe_float(rules.get("prop_min_line_edge", 0.35), 0.35)
    min_model_edge = safe_float(rules.get("prop_min_model_edge_pct", 0.03), 0.03)
    min_success_rate = clamp(safe_float(rules.get("prop_min_success_rate", 0.55), 0.55), 0.45, 0.75)
    min_abs_trend = clamp(safe_float(rules.get("prop_min_abs_trend", 0.10), 0.10), 0.01, 0.35)
    market_prior_weight = clamp(safe_float(rules.get("prop_market_prior_weight", 0.25), 0.25), 0.0, 0.6)

    def _score_candidates(
        edge_floor: float,
        model_edge_floor: float,
        success_floor: float,
        trend_floor: float,
    ) -> List[dict]:
        scored_rows = []
        for candidate in prop_candidates:
            model = _prop_projection(candidate, rules, state, projection_map)
            line = safe_float(candidate.get("line"), 0.0)
            # Apply safety haircut to edge from line (not absolute projection),
            # so we reduce confidence without biasing toward UNDER by default.
            raw_edge = model["projected"] - line
            line_edge = raw_edge * (1.0 - model["haircut_pct"])
            safe_projection = line + line_edge
            if abs(line_edge) < edge_floor:
                continue

            direction = "OVER" if line_edge > 0 else "UNDER"
            odds = candidate["odds_over"] if direction == "OVER" else candidate["odds_under"]
            if odds <= 1.0:
                continue

            values = candidate.get("last5", [])
            if not values:
                continue
            pushes = sum(1 for v in values if v == line)
            over_rate = (sum(1 for v in values if v > line) + (0.5 * pushes)) / len(values)
            under_rate = (sum(1 for v in values if v < line) + (0.5 * pushes)) / len(values)
            success_rate = over_rate if direction == "OVER" else under_rate
            prior_rate = market_prior_success_rate(state, candidate["market"], direction)
            trend = model["trend"]
            if success_rate < success_floor and abs(trend) < trend_floor:
                continue

            implied = 1.0 / odds
            trend_signal = clamp(trend * 0.05, -0.1, 0.1)
            line_signal = clamp(abs(line_edge) * 0.05, 0.0, 0.12)
            blended_rate = ((1.0 - market_prior_weight) * success_rate) + (market_prior_weight * prior_rate)
            model_prob = clamp(blended_rate + trend_signal + line_signal, 0.05, 0.92)
            context_bias = prop_game_context_bias(candidate, direction, game_context, rules)
            model_prob = clamp(model_prob + context_bias, 0.05, 0.92)
            edge_prob = model_prob - implied
            if edge_prob < model_edge_floor:
                continue

            ev = candidate_expected_return(model_prob, odds)
            if ev <= 0:
                continue

            safe_call = math.floor(safe_projection) if direction == "OVER" else math.ceil(safe_projection)
            safe_call = max(safe_call, 0)
            context_key = f"{normalize_team_key(candidate.get('team', ''))}::{normalize_team_key(candidate.get('opponent', ''))}"
            context_row = game_context.get(context_key, {})
            scored_rows.append(
                {
                    "player": candidate["player"],
                    "team": candidate["team"],
                    "opponent": candidate["opponent"],
                    "market": candidate["market"],
                    "direction": direction,
                    "line": round(line, 2),
                    "safe_call": safe_call,
                    "projected_raw": round(model["projected"], 3),
                    "projected_safe": round(safe_projection, 3),
                    "haircut_pct": round(model["haircut_pct"] * 100.0, 1),
                    "odds": odds,
                    "aus_odds": decimal_to_aus(odds),
                    "last5": values,
                    "last5_avg": round(model["avg_last5"], 3),
                    "trend": round(trend, 3),
                    "success_rate": round(success_rate, 3),
                    "market_prior_rate": round(prior_rate, 3),
                    "implied_prob": round(implied, 4),
                    "model_prob": round(model_prob, 4),
                    "edge_prob": round(edge_prob, 4),
                    "ev": round(ev, 4),
                    "learned_adj": round(model["learned_adj"], 3),
                    "learned_opp_adj": round(model["learned_opp_adj"], 3),
                    "game": candidate["game"],
                    "history_source": str(candidate.get("history_source", "")),
                    "history_noise_removed": int(safe_float(candidate.get("history_noise_removed", 0), 0)),
                    "context_bias": round(context_bias, 4),
                    "context_total": round(safe_float(context_row.get("consensus_total"), 0.0), 3),
                    "context_spread": round(safe_float(context_row.get("team_spread"), 0.0), 3),
                }
            )
        return scored_rows

    scored = _score_candidates(
        edge_floor=min_line_edge,
        model_edge_floor=min_model_edge,
        success_floor=min_success_rate,
        trend_floor=min_abs_trend,
    )
    relaxed_used = False
    if not scored:
        relaxed_edge_scale = clamp(safe_float(rules.get("prop_relaxed_line_edge_scale", 0.60), 0.60), 0.25, 1.0)
        relaxed_model_scale = clamp(safe_float(rules.get("prop_relaxed_model_edge_scale", 0.60), 0.60), 0.25, 1.0)
        relaxed_success = clamp(
            safe_float(rules.get("prop_relaxed_min_success_rate", 0.50), 0.50),
            0.45,
            min_success_rate,
        )
        relaxed_trend = clamp(
            safe_float(rules.get("prop_relaxed_min_abs_trend", 0.05), 0.05),
            0.01,
            min_abs_trend,
        )
        relaxed_line_edge = max(0.10, min_line_edge * relaxed_edge_scale)
        relaxed_model_edge = max(0.01, min_model_edge * relaxed_model_scale)
        scored = _score_candidates(
            edge_floor=relaxed_line_edge,
            model_edge_floor=relaxed_model_edge,
            success_floor=relaxed_success,
            trend_floor=relaxed_trend,
        )
        relaxed_used = bool(scored)

    if not scored:
        return {"legs": [], "total_odds": 0, "note": "NO_PROP_PARLAY: candidates failed edge/safety filters"}

    scored.sort(key=lambda row: (row["edge_prob"], row["ev"], row["success_rate"]), reverse=True)
    legs = []
    seen = set()
    for row in scored:
        key = str(row.get("player", "")).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        legs.append(row)
        if len(legs) >= int(rules.get("prop_max_legs", 3)):
            break

    total_odds = 1.0
    for leg in legs:
        total_odds *= leg["odds"]

    return {
        "legs": legs,
        "total_odds": round(total_odds, 3) if legs else 0,
        "note": (
            "Prop parlay built from last-5 matchup history with 10%+ safety haircut (relaxed fallback gates)"
            if relaxed_used
            else "Prop parlay built from last-5 matchup history with 10%+ safety haircut"
        ),
    }


def resolve_market_feeds(
    season: str,
    run_date: str,
    tank01_enable: bool,
    tank01_data_root: str,
    tank01_max_lag_days: int = 2,
    tank01_betting_props_json: str = "",
    api_sports_fallback: bool = False,
) -> Tuple[dict, dict, dict]:
    if not tank01_enable:
        games = fetch_nba_games(season, run_date)
        odds = fetch_nba_odds(run_date, season)
        return games, odds, {"primary": "api-sports", "fallback_used": False, "fallback_components": []}

    tank_games = load_tank01_games(
        run_date,
        tank01_data_root,
        max_lag_days=max(0, tank01_max_lag_days),
        explicit_props_json=tank01_betting_props_json,
    )
    tank_odds = load_tank01_odds(
        run_date,
        tank01_data_root,
        max_lag_days=max(0, tank01_max_lag_days),
        explicit_props_json=tank01_betting_props_json,
    )

    games = tank_games
    odds = tank_odds
    fallback_components: List[str] = []

    if not games.get("games") and api_sports_fallback:
        games = fetch_nba_games(season, run_date)
        fallback_components.append("games")
    if not odds.get("games") and api_sports_fallback:
        odds = fetch_nba_odds(run_date, season)
        fallback_components.append("odds")

    return games, odds, {
        "primary": "tank01",
        "fallback_used": bool(fallback_components),
        "fallback_components": fallback_components,
        "tank01_odds_source": tank_odds.get("source", ""),
        "tank01_odds_lag_days": int(tank_odds.get("source_lag_days", -1)),
        "tank01_games_source": tank_games.get("source", ""),
        "tank01_games_lag_days": int(tank_games.get("source_lag_days", -1)),
    }


def generate_report(
    games_data: dict,
    odds_data: dict,
    lineup: dict,
    pivots: list,
    parlay: dict,
    prop_parlay: dict,
    bet: dict,
    season: str,
    slot: str,
    b2b_count: int,
    major_out_count: int,
    learning_summary: dict,
    data_source_summary: Optional[dict] = None,
    b2b_reference_date: str = "",
) -> str:
    source_summary = data_source_summary or {}
    market_primary = source_summary.get("primary", "unknown")
    fallback_components = source_summary.get("fallback_components", [])
    fallback_text = ", ".join(fallback_components) if fallback_components else "none"
    report = f"""# Pete NBA Daily - {TODAY}
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M %Z')}
Season: {season}

## Data Sources
- Primary market feed: {market_primary}
- API-Sports fallback used: {fallback_text}

## Games Today ({games_data.get('note', '')})
"""

    for game in games_data.get("games", [])[:10]:
        report += f"- {game.get('away_team')} @ {game.get('home_team')} ({game.get('start_time', 'TBD')})\n"

    report += f"""
## Odds Snapshot ({odds_data.get('note', '')})
- Games with normalized odds: {len(odds_data.get('games', []))}

## Best Lineup ({lineup['format']})
- Slot window: {slot}
- Total Salary: ${lineup['total_salary']:,}
- Projected Points: {lineup.get('projected_points', 0)}
- Note: {lineup['note']}
"""

    if lineup.get("lineup"):
        report += "- Selected Lineup:\n"
        for row in lineup["lineup"]:
            report += (
                f"  - {row['slot']}: {row['name']} ({row['team']}) "
                f"${row['salary']} proj={row['projected']}\n"
            )

    smokies = lineup.get("smokies", [])
    report += "\n## Smokies\n"
    if smokies:
        for idx, smokie in enumerate(smokies, 1):
            report += (
                f"{idx}. {smokie['player']} ({smokie['team']}) - salary ${smokie['salary']}, "
                f"proj delta +{smokie['delta']}\n"
            )
    else:
        report += "- No smokies met threshold\n"

    report += f"""
## Late News Pivots
1. **{pivots[0]['player']}**: {pivots[0]['reason']}
2. **{pivots[1]['player']}**: {pivots[1]['reason']}

## Quant-Gated Parlay ({parlay.get('total_odds', 0)}x)
"""

    if parlay.get("legs"):
        for idx, leg in enumerate(parlay["legs"], 1):
            report += (
                f"{idx}. {leg['team']} @ {leg['aus_odds']} ({leg['odds']}) - {leg['game']} "
                f"edge={leg.get('edge', 0) * 100:.2f}% ev={leg.get('edge_dollars_per_1u', 0):.2f}\n"
            )
    else:
        report += "- NO_PARLAY\n"

    report += f"- Note: {parlay.get('edge_notes', '')}\n"

    report += f"""
## Bet Decision
- Pick: **{bet['pick']}**
- Odds: {bet.get('aus_odds', 'N/A')} ({bet.get('odds', 0)})
- Edge: {bet.get('edge', 0) * 100:.2f}%
- EV per 1u: {bet.get('edge_dollars_per_1u', 0):.2f}
- Game: {bet.get('game', 'N/A')}
- Reason: {bet.get('reason', 'N/A')}
"""

    report += f"""
## Parlay of the Day (Player Props) ({prop_parlay.get('total_odds', 0)}x)
"""
    if prop_parlay.get("legs"):
        for idx, leg in enumerate(prop_parlay["legs"], 1):
            report += (
                f"{idx}. {leg['player']} {leg['direction']} {leg['safe_call']} {leg['market']} "
                f"(book line {leg['line']}) @ {leg['aus_odds']} ({leg['odds']}) - {leg['game']} "
                f"edge={leg['edge_prob'] * 100:.2f}% ev={leg['ev']:.2f}\n"
            )
            report += (
                f"   last5={leg['last5']} avg={leg['last5_avg']} trend={leg['trend']} "
                f"haircut={leg['haircut_pct']}% history={leg.get('history_source', 'n/a')} "
                f"noise_removed={leg.get('history_noise_removed', 0)} "
                f"context(total={leg.get('context_total', 0)}, spread={leg.get('context_spread', 0)}, bias={leg.get('context_bias', 0)})\n"
            )
    else:
        report += "- NO_PROP_PARLAY\n"

    report += f"- Note: {prop_parlay.get('note', '')}\n"

    report += f"""
## Risk Filters
- B2B reference date: {b2b_reference_date or 'N/A'}
- B2B blocked teams tracked: {b2b_count}
- Major-out teams blocked: {major_out_count}
- Rule: Home teams get +{safe_float(load_quant_rules().get('home_team_model_boost_pct', 0.10), 0.10) * 100:.0f}% model boost
"""

    report += f"""
## Learning Engine
- DFS samples learned: {learning_summary.get('dfs_samples', 0)}
- Bet samples learned: {learning_summary.get('bet_samples', 0)}
- Prop samples learned: {learning_summary.get('prop_samples', 0)}
- Learning backend: {learning_summary.get('learning_backend', 'python')}
- Last feedback file: {learning_summary.get('last_feedback_file') or 'N/A'}
"""

    if learning_summary.get("top_dfs_adjustments"):
        report += "- Top DFS adjustments:\n"
        for row in learning_summary["top_dfs_adjustments"]:
            report += f"  - {row['player']}: {row['adj']:+.3f}\n"

    if learning_summary.get("top_prop_adjustments"):
        report += "- Top prop adjustments:\n"
        for row in learning_summary["top_prop_adjustments"]:
            report += f"  - {row['player_market']}: {row['adj']:+.3f}\n"

    report += """
---
Safety: Wagering is gated by PETE_ENABLE_WAGERING and quant_rules.json controls.
"""

    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Pete NBA Daily Pipeline")
    parser.add_argument("--dry-run", action="store_true", help="Print report to stdout")
    parser.add_argument("--date", default=TODAY, help="Date in YYYY-MM-DD")
    parser.add_argument("--season", default=DEFAULT_SEASON, help="Season start year (e.g., 2026)")
    parser.add_argument("--draftstars-csv", default="", help="Path to Draftstars player CSV")
    parser.add_argument("--slot", choices=["all", "early", "late"], default="all", help="Slate slot window")
    parser.add_argument("--feedback-json", default="", help="Path to feedback JSON for learning updates")
    parser.add_argument("--major-outs-json", default="", help="Override path to major-out teams JSON")
    parser.add_argument("--espn-injuries-json", default="", help="Override path to ESPN injury sync JSON")
    parser.add_argument("--props-json", default="", help="Path to player props JSON payload")
    parser.add_argument("--h2h-json", default="", help="Path to optional last-5 matchup JSON payload")
    parser.add_argument(
        "--tank01-enable",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Enable Tank01 odds/props integration for bet and parlay (default: true)",
    )
    parser.add_argument(
        "--tank01-data-root",
        default=str(Path.cwd() / "projects" / "pete-dfs" / "data-lake"),
        help="Tank01 data-lake root",
    )
    parser.add_argument("--tank01-max-lag-days", type=int, default=2, help="Maximum dated-file lag for Tank01 snapshots")
    parser.add_argument("--tank01-props-default-odds", type=float, default=1.87, help="Fallback decimal odds for Tank01 props when per-prop odds are unavailable")
    parser.add_argument("--tank01-betting-props-json", default="", help="Optional explicit Tank01 betting-props JSON path")
    parser.add_argument("--tank01-players-json", default="", help="Optional explicit Tank01 players JSON path")
    parser.add_argument("--tank01-injuries-json", default="", help="Optional explicit Tank01 injuries JSON path")
    parser.add_argument(
        "--api-sports-fallback",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Allow API-Sports fallback when Tank01 snapshots are unavailable (default: false)",
    )
    args = parser.parse_args()

    print(f"[Pete NBA] Starting pipeline for date={args.date} season={args.season} slot={args.slot}")

    api_key = load_env_secrets()
    if not api_key and (not args.tank01_enable or args.api_sports_fallback):
        print("[Pete NBA] WARNING: NBA_API_KEY not set; API-Sports fallback is unavailable")

    tank01_refresh = None
    if args.tank01_enable and tank01_api_key():
        tank01_refresh = refresh_tank01_snapshots(args.date, args.tank01_data_root, args.season)

    rules = load_quant_rules()
    learning_state = load_learning_state()
    learning_state = update_learning_state_from_feedback(learning_state, args.feedback_json)
    if args.tank01_enable and tank01_api_key():
        learning_date = (datetime.strptime(args.date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        feedback_path = build_tank01_feedback(learning_date, args.tank01_data_root)
        if feedback_path:
            learning_state = update_learning_state_from_feedback(learning_state, str(feedback_path))
    save_learning_state(learning_state)

    games_data, odds_for_wagering, source_summary = resolve_market_feeds(
        args.season,
        args.date,
        tank01_enable=bool(args.tank01_enable),
        tank01_data_root=args.tank01_data_root,
        tank01_max_lag_days=max(0, args.tank01_max_lag_days),
        tank01_betting_props_json=args.tank01_betting_props_json,
        api_sports_fallback=bool(args.api_sports_fallback),
    )
    tank01_odds_meta = {
        "source": source_summary.get("tank01_odds_source", ""),
        "source_lag_days": int(source_summary.get("tank01_odds_lag_days", -1)),
        "games": len(odds_for_wagering.get("games", [])) if source_summary.get("primary") == "tank01" else 0,
        "fallback_components": source_summary.get("fallback_components", []),
    }

    slate_date = infer_slate_date(args.date, source_summary, odds_for_wagering)
    previous_date = (datetime.strptime(slate_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    if args.tank01_enable:
        b2b_teams = load_tank01_teams_played_on_date(
            previous_date,
            args.tank01_data_root,
            max_lag_days=0,
        )
        if not b2b_teams and args.api_sports_fallback:
            b2b_teams = fetch_teams_played_on_date(args.season, previous_date)
    else:
        b2b_teams = fetch_teams_played_on_date(args.season, previous_date)

    major_out_manual = load_major_out_teams(args.major_outs_json)
    major_out_tank01 = set()
    if args.tank01_enable:
        major_out_tank01 = load_tank01_major_out_teams(
            args.date,
            args.tank01_data_root,
            max_lag_days=max(0, args.tank01_max_lag_days),
            explicit_injuries_json=args.tank01_injuries_json,
        )
    major_out_espn = load_espn_major_out_teams(args.espn_injuries_json) if not major_out_tank01 else set()
    major_out_teams = set(major_out_manual) | set(major_out_tank01) | set(major_out_espn)

    tank01_projections = load_tank01_projections(args.date, args.tank01_data_root, max_lag_days=max(0, args.tank01_max_lag_days))
    tank01_injuries = load_tank01_injury_status_map(args.date, args.tank01_data_root, max_lag_days=max(0, args.tank01_max_lag_days))

    lineup = build_best_lineup(
        games_data,
        draftstars_csv=args.draftstars_csv,
        slot=args.slot,
        learning_state=learning_state,
        rules=rules,
        run_date=args.date,
        tank01_data_root=args.tank01_data_root,
        tank01_max_lag_days=max(0, args.tank01_max_lag_days),
        tank01_projections_rows=tank01_projections.get("rows", []),
        tank01_injury_map=tank01_injuries,
        use_tank01_dfs=bool(args.tank01_enable and not args.draftstars_csv),
    )
    pivots = get_pivots(lineup)
    parlay = build_parlay(
        games_data,
        odds_for_wagering,
        rules,
        learning_state=learning_state,
        no_b2b_teams=b2b_teams,
        major_out_teams=major_out_teams,
    )
    bet = get_bet_pick(
        games_data,
        odds_for_wagering,
        rules,
        learning_state=learning_state,
        no_b2b_teams=b2b_teams,
        major_out_teams=major_out_teams,
    )
    prop_candidates_primary = load_prop_candidates(args.props_json, args.h2h_json)
    tank01_prop_meta = {"candidates": 0, "source": "", "source_lag_days": -1}
    tank01_prop_candidates = []
    if args.tank01_enable:
        tank01_prop_candidates, tank01_prop_meta = load_tank01_prop_candidates(
            args.date,
            args.tank01_data_root,
            h2h_json_path=args.h2h_json,
            max_lag_days=max(0, args.tank01_max_lag_days),
            default_odds=safe_float(args.tank01_props_default_odds, 1.87),
            explicit_props_json=args.tank01_betting_props_json,
            explicit_players_json=args.tank01_players_json,
        )
    if args.tank01_enable and tank01_prop_candidates:
        prop_candidates = merge_prop_candidates(tank01_prop_candidates, prop_candidates_primary)
    else:
        prop_candidates = merge_prop_candidates(prop_candidates_primary, tank01_prop_candidates)
    prop_market_counts = summarize_prop_markets(prop_candidates)
    prop_parlay = build_player_prop_parlay(
        prop_candidates,
        rules,
        learning_state=learning_state,
        dfs_projection_map=lineup.get("projection_map", {}),
        odds_data=odds_for_wagering,
    )
    learning_summary = build_learning_summary(learning_state)

    report = generate_report(
        games_data,
        odds_for_wagering,
        lineup,
        pivots,
        parlay,
        prop_parlay,
        bet,
        args.season,
        slot=args.slot,
        b2b_count=len(b2b_teams),
        major_out_count=len(major_out_teams),
        learning_summary=learning_summary,
        data_source_summary=source_summary,
        b2b_reference_date=previous_date,
    )

    run_snapshot = {
        "run_date": args.date,
        "slot": args.slot,
        "projection_map": lineup.get("projection_map", {}),
        "lineup": lineup.get("lineup", []),
        "bet": bet,
        "parlay": parlay,
        "prop_parlay": prop_parlay,
        "tank01_refresh": tank01_refresh or {},
    }
    write_run_snapshot(args.date, run_snapshot)

    report += (
        "\n## Tank01 Integration\n"
        f"- Odds source: {tank01_odds_meta.get('source', 'N/A')}\n"
        f"- Odds lag days: {tank01_odds_meta.get('source_lag_days', -1)}\n"
        f"- Odds games loaded: {tank01_odds_meta.get('games', 0)}\n"
        f"- API fallback components: {', '.join(tank01_odds_meta.get('fallback_components', [])) or 'none'}\n"
        f"- Prop source: {tank01_prop_meta.get('source', 'N/A')}\n"
        f"- Prop lag days: {tank01_prop_meta.get('source_lag_days', -1)}\n"
        f"- Prop candidates merged: {len(prop_candidates)}\n"
        f"- Prop candidates (synthetic history): {tank01_prop_meta.get('synthetic_history_candidates', 0)}\n"
        f"- Prop history noise removed: {tank01_prop_meta.get('history_noise_removed_total', 0)}\n"
        f"- Prop market coverage: {', '.join([f'{k}:{v}' for k, v in prop_market_counts.items()]) or 'none'}\n"
    )

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
