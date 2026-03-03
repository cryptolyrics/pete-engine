#!/usr/bin/env python3
"""Pete DFS engine with ESPN history ingestion, lineup optimization, and rolling backtest."""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import requests

try:
    from scipy.optimize import Bounds, LinearConstraint, milp

    SCIPY_AVAILABLE = True
except Exception:
    SCIPY_AVAILABLE = False
    Bounds = None
    LinearConstraint = None
    milp = None

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={yyyymmdd}"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary?event={event_id}"

ROSTER_SLOTS = {"PG": 2, "SG": 2, "SF": 2, "PF": 2, "C": 1}
DEFAULT_SALARY_CAP = 100_000
WORKSPACE = Path(os.environ.get("OPENCLAW_WORKSPACE", str(Path.cwd() / ".pete-workspace")))
LOG_DIR = WORKSPACE / "logs" / "Pete"
DEFAULT_TANK01_PROPS_WEIGHT = 0.20
DEFAULT_TANK01_PROPS_CAP = 8.0

INJURY_OUT_TAGS = {"out", "doubtful", "inactive", "ruled out"}
INJURY_QUESTIONABLE_TAGS = {"questionable", "gtd", "game time decision"}
INJURY_PROBABLE_TAGS = {"probable"}

TEAM_CODE_ALIASES = {
    "ATLANTA HAWKS": "ATL",
    "BOSTON CELTICS": "BOS",
    "BROOKLYN NETS": "BKN",
    "CHARLOTTE HORNETS": "CHA",
    "CHICAGO BULLS": "CHI",
    "CLEVELAND CAVALIERS": "CLE",
    "DALLAS MAVERICKS": "DAL",
    "DENVER NUGGETS": "DEN",
    "DETROIT PISTONS": "DET",
    "GOLDEN STATE WARRIORS": "GSW",
    "HOUSTON ROCKETS": "HOU",
    "INDIANA PACERS": "IND",
    "LOS ANGELES CLIPPERS": "LAC",
    "LA CLIPPERS": "LAC",
    "LOS ANGELES LAKERS": "LAL",
    "LAKERS": "LAL",
    "MEMPHIS GRIZZLIES": "MEM",
    "MIAMI HEAT": "MIA",
    "MILWAUKEE BUCKS": "MIL",
    "MINNESOTA TIMBERWOLVES": "MIN",
    "NEW ORLEANS PELICANS": "NOP",
    "NEW YORK KNICKS": "NYK",
    "OKLAHOMA CITY THUNDER": "OKC",
    "ORLANDO MAGIC": "ORL",
    "PHILADELPHIA 76ERS": "PHI",
    "PHOENIX SUNS": "PHX",
    "PORTLAND TRAIL BLAZERS": "POR",
    "SACRAMENTO KINGS": "SAC",
    "SAN ANTONIO SPURS": "SAS",
    "TORONTO RAPTORS": "TOR",
    "UTAH JAZZ": "UTA",
    "WASHINGTON WIZARDS": "WAS",
    "GS": "GSW",
    "NO": "NOP",
    "SA": "SAS",
    "NY": "NYK",
    "PHO": "PHX",
    "UTA": "UTA",
    "BKN": "BKN",
    "BRK": "BKN",
    "NJN": "BKN",
}

NAME_SUFFIX_TOKENS = {"jr", "sr", "ii", "iii", "iv", "v"}

# Draftstars scoring weights
POINTS_WEIGHT = 1.0
REBOUNDS_WEIGHT = 1.25
ASSISTS_WEIGHT = 1.5
STEALS_WEIGHT = 2.0
BLOCKS_WEIGHT = 2.0
TURNOVERS_WEIGHT = -0.5


@dataclass
class EngineResult:
    success: bool
    reason: str
    lineup: List[dict]
    total_salary: float
    projected_form: float
    backtest: dict
    scrape: dict
    injury_summary: Optional[dict] = None
    h2h_summary: Optional[dict] = None
    tank01_summary: Optional[dict] = None
    value_summary: Optional[dict] = None
    tank01_backtest: Optional[dict] = None


def _clean_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _clean_player_name(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _canonical_person_name(value: str) -> str:
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    tokens = text.split(" ")
    while tokens and tokens[-1] in NAME_SUFFIX_TOKENS:
        tokens.pop()
    return " ".join(tokens)


def normalize_team_code(value: str) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if text in TEAM_CODE_ALIASES:
        return TEAM_CODE_ALIASES[text]
    if len(text) == 3 and text.isalpha():
        return text
    return TEAM_CODE_ALIASES.get(text, text[:3])


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


def _safe_float(value, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)

    text = str(value).strip()
    if not text:
        return default

    if ":" in text:
        return default
    if "/" in text:
        return default

    text = text.replace(",", "").replace("%", "")
    try:
        return float(text)
    except Exception:
        return default


def _request_json(session: requests.Session, url: str, timeout: int = 20, retries: int = 3, backoff_s: float = 0.35) -> Optional[dict]:
    for attempt in range(retries):
        try:
            response = session.get(url, timeout=timeout)
            if response.status_code == 200:
                payload = response.json()
                if isinstance(payload, dict):
                    return payload
            if response.status_code in {400, 401, 403, 404}:
                return None
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(backoff_s * (attempt + 1))
    return None


def _find_event_ids(scoreboard_payload: dict) -> List[str]:
    events = scoreboard_payload.get("events", []) if isinstance(scoreboard_payload, dict) else []
    ids: List[str] = []
    for row in events if isinstance(events, list) else []:
        if not isinstance(row, dict):
            continue
        event_id = str(row.get("id", "")).strip()
        if event_id:
            ids.append(event_id)
    return ids


def _get_stat_labels(stat_group: dict) -> List[str]:
    for key in ("labels", "names", "keys"):
        raw = stat_group.get(key)
        if isinstance(raw, list) and raw:
            labels = [str(item).strip() for item in raw]
            if any(labels):
                return labels
    return []


def _stat_value(stats: Dict[str, float], labels: Sequence[str]) -> float:
    for label in labels:
        key = _clean_label(label)
        if key in stats:
            return _safe_float(stats[key], 0.0)
    return 0.0


def _to_stat_map(labels: Sequence[str], values: Sequence) -> Dict[str, float]:
    mapping: Dict[str, float] = {}
    if labels:
        for idx, raw in enumerate(values):
            if idx >= len(labels):
                continue
            mapping[_clean_label(labels[idx])] = _safe_float(raw, 0.0)
    else:
        fallback = {
            6: "reb",
            7: "ast",
            8: "stl",
            9: "blk",
            10: "to",
            12: "pts",
        }
        for idx, label in fallback.items():
            if idx < len(values):
                mapping[label] = _safe_float(values[idx], 0.0)
    return mapping


def _fpts_from_stats(stats: Dict[str, float]) -> float:
    points = _stat_value(stats, ["pts", "points"])
    rebounds = _stat_value(stats, ["reb", "rebounds", "totalrebounds"])
    assists = _stat_value(stats, ["ast", "assists"])
    steals = _stat_value(stats, ["stl", "steals"])
    blocks = _stat_value(stats, ["blk", "blocks"])
    turnovers = _stat_value(stats, ["to", "tov", "turnovers"])
    return (
        points * POINTS_WEIGHT
        + rebounds * REBOUNDS_WEIGHT
        + assists * ASSISTS_WEIGHT
        + steals * STEALS_WEIGHT
        + blocks * BLOCKS_WEIGHT
        + turnovers * TURNOVERS_WEIGHT
    )


def extract_player_fpts_from_summary(summary_payload: dict) -> List[dict]:
    rows: List[dict] = []
    boxscore = summary_payload.get("boxscore", {}) if isinstance(summary_payload, dict) else {}
    teams = boxscore.get("players", []) if isinstance(boxscore, dict) else []

    for team_blob in teams if isinstance(teams, list) else []:
        if not isinstance(team_blob, dict):
            continue
        team_info = team_blob.get("team", {}) if isinstance(team_blob.get("team"), dict) else {}
        team = str(team_info.get("abbreviation") or team_info.get("shortDisplayName") or "").strip().upper()

        stat_groups = team_blob.get("statistics", []) if isinstance(team_blob.get("statistics"), list) else []
        if not stat_groups:
            continue

        # First stat group usually has full totals and stable labels.
        stat_group = stat_groups[0] if isinstance(stat_groups[0], dict) else {}
        labels = _get_stat_labels(stat_group)
        athletes = stat_group.get("athletes", []) if isinstance(stat_group, dict) else []

        for athlete_blob in athletes if isinstance(athletes, list) else []:
            if not isinstance(athlete_blob, dict):
                continue
            athlete = athlete_blob.get("athlete", {}) if isinstance(athlete_blob.get("athlete"), dict) else {}
            name = str(athlete.get("displayName") or athlete.get("shortName") or "").strip()
            stats_raw = athlete_blob.get("stats", []) if isinstance(athlete_blob.get("stats"), list) else []
            if not name or not stats_raw:
                continue

            stats = _to_stat_map(labels, stats_raw)
            rows.append({"Name": name, "Team": team, "FP": round(_fpts_from_stats(stats), 4)})

    return rows


def collect_espn_history_logs(
    lookback_days: int = 10,
    now: Optional[datetime] = None,
    request_timeout: int = 20,
) -> Tuple[pd.DataFrame, dict]:
    pivot = now or datetime.now()

    records: List[dict] = []
    days_scanned = 0
    events_seen = 0
    events_loaded = 0

    with requests.Session() as session:
        session.headers.update({"User-Agent": "elevate-flow-pete-dfs/1.0"})

        for offset in range(max(1, lookback_days)):
            days_scanned += 1
            game_date = (pivot - timedelta(days=offset)).date()
            yyyymmdd = game_date.strftime("%Y%m%d")

            scoreboard = _request_json(session, ESPN_SCOREBOARD_URL.format(yyyymmdd=yyyymmdd), timeout=request_timeout)
            if not scoreboard:
                continue

            event_ids = _find_event_ids(scoreboard)
            events_seen += len(event_ids)

            for event_id in event_ids:
                summary = _request_json(session, ESPN_SUMMARY_URL.format(event_id=event_id), timeout=request_timeout)
                if not summary:
                    continue

                players = extract_player_fpts_from_summary(summary)
                if players:
                    events_loaded += 1
                for row in players:
                    row["Date"] = game_date.isoformat()
                    row["EventId"] = event_id
                    records.append(row)

    history_df = pd.DataFrame(records)
    scrape = {
        "days_scanned": days_scanned,
        "events_seen": events_seen,
        "events_loaded": events_loaded,
        "records": len(history_df.index),
    }
    return history_df, scrape


def compute_variance_map(history_df: pd.DataFrame) -> Dict[str, float]:
    if history_df.empty:
        return {}

    cols = set(history_df.columns)
    if "Name" not in cols or "FP" not in cols:
        return {}

    variance = history_df.groupby("Name")["FP"].std(ddof=0).fillna(0.0)
    return {_clean_player_name(name): float(value) for name, value in variance.items()}


def _load_json_payload(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


def _parse_date_from_stem(path: Path) -> Optional[date]:
    try:
        return datetime.strptime(path.stem, "%Y-%m-%d").date()
    except Exception:
        return None


def _resolve_dated_file_with_lag(directory: Path, run_date: str, max_lag_days: int) -> Tuple[Path, int]:
    run_dt = datetime.strptime(run_date, "%Y-%m-%d").date()
    exact = directory / f"{run_date}.json"
    if exact.exists():
        return exact, 0

    if not directory.exists():
        return Path(""), -1

    candidates: List[Tuple[int, Path]] = []
    for path in directory.glob("*.json"):
        parsed = _parse_date_from_stem(path)
        if parsed is None:
            continue
        lag = (run_dt - parsed).days
        if lag < 0:
            continue
        if lag <= max(0, max_lag_days):
            candidates.append((lag, path))

    if not candidates:
        return Path(""), -1

    candidates.sort(key=lambda row: (row[0], row[1].name))
    lag, path = candidates[0]
    return path, lag


def _candidate_tank01_players_paths(run_date: str, data_root: Path) -> List[Path]:
    return [
        data_root / "nba" / "players" / f"{run_date}.json",
        Path.cwd() / "projects" / "pete-dfs" / "data-lake" / "nba" / "players" / f"{run_date}.json",
    ]


def _candidate_tank01_props_paths(run_date: str, data_root: Path) -> List[Path]:
    return [
        data_root / "nba" / "betting-props" / f"{run_date}.json",
        Path.cwd() / "projects" / "pete-dfs" / "data-lake" / "nba" / "betting-props" / f"{run_date}.json",
    ]


def _candidate_tank01_injuries_paths(run_date: str, data_root: Path) -> List[Path]:
    return [
        data_root / "nba" / "injuries" / f"{run_date}.json",
        Path.cwd() / "projects" / "pete-dfs" / "data-lake" / "nba" / "injuries" / f"{run_date}.json",
    ]


def _candidate_tank01_boxscore_paths(run_date: str, data_root: Path) -> List[Path]:
    yyyymmdd = run_date.replace("-", "")
    return [
        data_root / "nba" / "boxscores" / f"{run_date}.json",
        data_root / "nba" / "boxscores" / f"{yyyymmdd}.json",
        Path.cwd() / "projects" / "pete-dfs" / "data-lake" / "nba" / "boxscores" / f"{run_date}.json",
        Path.cwd() / "projects" / "pete-dfs" / "data-lake" / "nba" / "boxscores" / f"{yyyymmdd}.json",
    ]


def load_tank01_players_index(run_date: str, data_root: Path, max_lag_days: int = 2) -> dict:
    payload = {}
    source = ""
    source_lag_days = -1
    for base in [data_root / "nba" / "players", Path.cwd() / "projects" / "pete-dfs" / "data-lake" / "nba" / "players"]:
        path, lag = _resolve_dated_file_with_lag(base, run_date, max_lag_days=max_lag_days)
        if not path:
            continue
        payload = _load_json_payload(path)
        if payload:
            source = str(path)
            source_lag_days = lag
            break
    body = payload.get("body", []) if isinstance(payload, dict) else []
    rows = body if isinstance(body, list) else []

    by_name: Dict[str, dict] = {}
    by_canonical_team: Dict[Tuple[str, str], List[dict]] = {}
    by_id: Dict[str, dict] = {}
    team_count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("playerID", "")).strip()
        name = str(row.get("longName") or row.get("name") or "").strip()
        team = normalize_team_code(row.get("team", ""))
        if team:
            team_count += 1
        if not pid or not name:
            continue
        player_entry = {
            "player_id": pid,
            "name": name,
            "team": team,
            "pos": str(row.get("pos", "")).strip().upper(),
            "name_key": _clean_player_name(name),
            "canonical_name": _canonical_person_name(name),
        }
        by_id[pid] = player_entry
        by_name[_clean_player_name(name)] = player_entry
        canon = player_entry["canonical_name"]
        if team and canon:
            by_canonical_team.setdefault((team, canon), []).append(player_entry)

    return {
        "source": source,
        "source_lag_days": int(source_lag_days),
        "fallback_used": bool(source_lag_days > 0),
        "rows": len(rows),
        "mapped": len(by_name),
        "team_rows": team_count,
        "by_name": by_name,
        "by_canonical_team": by_canonical_team,
        "by_id": by_id,
    }


def _tank01_prop_to_fp(prop_bets: dict) -> float:
    if not isinstance(prop_bets, dict):
        return 0.0
    points = _safe_float(prop_bets.get("pts"), 0.0)
    rebounds = _safe_float(prop_bets.get("reb"), 0.0)
    assists = _safe_float(prop_bets.get("ast"), 0.0)
    steals = _safe_float(prop_bets.get("stl"), 0.0)
    blocks = _safe_float(prop_bets.get("blk"), 0.0)
    turnovers = _safe_float(prop_bets.get("turnover"), _safe_float(prop_bets.get("to"), 0.0))
    return (
        points * POINTS_WEIGHT
        + rebounds * REBOUNDS_WEIGHT
        + assists * ASSISTS_WEIGHT
        + steals * STEALS_WEIGHT
        + blocks * BLOCKS_WEIGHT
        + turnovers * TURNOVERS_WEIGHT
    )


def load_tank01_props_index(run_date: str, data_root: Path, max_lag_days: int = 2) -> dict:
    payload = {}
    source = ""
    source_lag_days = -1
    for base in [data_root / "nba" / "betting-props", Path.cwd() / "projects" / "pete-dfs" / "data-lake" / "nba" / "betting-props"]:
        path, lag = _resolve_dated_file_with_lag(base, run_date, max_lag_days=max_lag_days)
        if not path:
            continue
        payload = _load_json_payload(path)
        if payload:
            source = str(path)
            source_lag_days = lag
            break

    body = payload.get("body", []) if isinstance(payload, dict) else []
    games = body if isinstance(body, list) else []

    by_player_id: Dict[str, dict] = {}
    player_rows = 0
    for game in games:
        if not isinstance(game, dict):
            continue
        player_props = game.get("playerProps", []) if isinstance(game.get("playerProps"), list) else []
        for row in player_props:
            if not isinstance(row, dict):
                continue
            pid = str(row.get("playerID", "")).strip()
            if not pid:
                continue
            prop_fp = _tank01_prop_to_fp(row.get("propBets", {}))
            player_rows += 1
            current = by_player_id.get(pid)
            if current is None or prop_fp > current.get("prop_fp", 0.0):
                by_player_id[pid] = {
                    "player_id": pid,
                    "prop_fp": round(prop_fp, 4),
                    "prop_bets": row.get("propBets", {}),
                    "game_id": str(game.get("gameID", "")),
                    "home_team": normalize_team_code(game.get("homeTeam", "")),
                    "away_team": normalize_team_code(game.get("awayTeam", "")),
                }

    return {
        "source": source,
        "source_lag_days": int(source_lag_days),
        "fallback_used": bool(source_lag_days > 0),
        "games": len(games),
        "player_rows": player_rows,
        "players_with_props": len(by_player_id),
        "by_player_id": by_player_id,
    }


def load_tank01_injury_index(run_date: str, data_root: Path, max_lag_days: int = 2) -> dict:
    payload = {}
    source = ""
    source_lag_days = -1
    for base in [data_root / "nba" / "injuries", Path.cwd() / "projects" / "pete-dfs" / "data-lake" / "nba" / "injuries"]:
        path, lag = _resolve_dated_file_with_lag(base, run_date, max_lag_days=max_lag_days)
        if not path:
            continue
        payload = _load_json_payload(path)
        if payload:
            source = str(path)
            source_lag_days = lag
            break

    body = payload.get("body", []) if isinstance(payload, dict) else []
    rows = body if isinstance(body, list) else []

    records: Dict[Tuple[str, str], dict] = {}
    by_name: Dict[str, List[dict]] = {}

    for row in rows:
        if not isinstance(row, dict):
            continue
        player = str(row.get("longName") or row.get("playerName") or row.get("name") or "").strip()
        if not player:
            continue
        team = normalize_team_code(row.get("team", ""))
        status = str(row.get("status") or row.get("injuryStatus") or row.get("designation") or "").strip()
        detail = str(row.get("description") or row.get("injury") or "").strip()
        category = classify_injury_status(f"{status} {detail}")
        entry = {"player": player, "team": team, "status": status, "detail": detail, "category": category}
        key = (team, _clean_player_name(player))
        records[key] = entry
        by_name.setdefault(_clean_player_name(player), []).append(entry)

    return {
        "records": records,
        "name_records": by_name,
        "source": source,
        "source_lag_days": int(source_lag_days),
        "fallback_used": bool(source_lag_days > 0),
        "count": len(records),
    }


def merge_injury_indexes(primary: dict, secondary: dict) -> dict:
    merged_records = dict(primary.get("records", {}))
    merged_by_name: Dict[str, List[dict]] = {}

    for idx in [primary, secondary]:
        for key, items in idx.get("name_records", {}).items():
            merged_by_name.setdefault(key, []).extend(items if isinstance(items, list) else [])
        for key, row in idx.get("records", {}).items():
            if key not in merged_records:
                merged_records[key] = row

    return {
        "records": merged_records,
        "name_records": merged_by_name,
        "source": f"{primary.get('source', '')}|{secondary.get('source', '')}".strip("|"),
        "count": len(merged_records),
    }


def apply_tank01_player_mapping(df: pd.DataFrame, players_index: dict) -> Tuple[pd.DataFrame, dict]:
    if df.empty:
        return df, {"csv_rows": 0, "matched": 0, "unmatched": 0, "source": players_index.get("source", "")}

    work = df.copy()
    by_name = players_index.get("by_name", {})
    by_canonical_team = players_index.get("by_canonical_team", {})
    matches = 0
    canonical_matches = 0
    work["Tank01PlayerID"] = ""
    work["Tank01Name"] = ""
    work["Tank01Team"] = ""
    work["Tank01MatchType"] = ""
    work["NameKey"] = work["Name"].map(_clean_player_name)
    work["CanonicalName"] = work["Name"].map(_canonical_person_name)
    for idx, row in work.iterrows():
        mapped = by_name.get(row["NameKey"])
        match_type = "exact" if mapped else ""
        if not mapped:
            team = normalize_team_code(row.get("Team", ""))
            canon = row.get("CanonicalName", "")
            candidates = by_canonical_team.get((team, canon), []) if team and canon else []
            if len(candidates) == 1:
                mapped = candidates[0]
                match_type = "canonical_team"
        if not mapped:
            continue
        work.at[idx, "Tank01PlayerID"] = mapped.get("player_id", "")
        work.at[idx, "Tank01Name"] = mapped.get("name", "")
        work.at[idx, "Tank01Team"] = mapped.get("team", "")
        work.at[idx, "Tank01MatchType"] = match_type
        matches += 1
        if match_type == "canonical_team":
            canonical_matches += 1

    summary = {
        "source": players_index.get("source", ""),
        "csv_rows": int(len(work.index)),
        "matched": int(matches),
        "unmatched": int(len(work.index) - matches),
        "coverage_pct": round((matches / len(work.index)) * 100.0, 2) if len(work.index) else 0.0,
        "canonical_matches": int(canonical_matches),
    }
    return work.drop(columns=["NameKey", "CanonicalName"]), summary


def apply_tank01_prop_projection_signal(
    df: pd.DataFrame,
    props_index: dict,
    weight: float,
    cap_abs: float,
) -> Tuple[pd.DataFrame, dict]:
    if df.empty:
        return df, {"players_with_props": 0, "players_adjusted": 0, "source": props_index.get("source", "")}

    work = df.copy()
    by_player_id = props_index.get("by_player_id", {})
    with_props = 0
    adjusted = 0
    work["Tank01PropFP"] = np.nan
    work["Tank01PropAdj"] = 0.0
    work["Tank01ValueScore"] = np.nan

    for idx, row in work.iterrows():
        pid = str(row.get("Tank01PlayerID", "")).strip()
        if not pid:
            continue
        prop_row = by_player_id.get(pid)
        if not prop_row:
            continue
        with_props += 1
        prop_fp = _safe_float(prop_row.get("prop_fp"), 0.0)
        form = _safe_float(row.get("Form"), 0.0)
        raw_delta = prop_fp - form
        adj = max(-cap_abs, min(cap_abs, raw_delta * weight))
        if abs(adj) > 0:
            adjusted += 1
        salary = max(1.0, _safe_float(row.get("Salary"), 1.0))
        value_score = (prop_fp * 1000.0) / salary
        work.at[idx, "Tank01PropFP"] = round(prop_fp, 4)
        work.at[idx, "Tank01PropAdj"] = round(adj, 4)
        work.at[idx, "Tank01ValueScore"] = round(value_score, 4)

    summary = {
        "source": props_index.get("source", ""),
        "games": int(props_index.get("games", 0)),
        "players_with_props_feed": int(props_index.get("players_with_props", 0)),
        "players_with_props": int(with_props),
        "players_adjusted": int(adjusted),
        "weight": float(weight),
        "cap_abs": float(cap_abs),
    }
    return work, summary


def build_value_detection_summary(df: pd.DataFrame, max_items: int = 8) -> dict:
    if df.empty:
        return {"candidate_count": 0, "top_edges": []}
    if "Tank01PropFP" not in df.columns:
        return {"candidate_count": 0, "top_edges": []}

    work = df.copy()
    work = work.dropna(subset=["Tank01PropFP"]).copy()
    if work.empty:
        return {"candidate_count": 0, "top_edges": []}

    work["ValueEdgeFP"] = work["Tank01PropFP"] - work["Form"].astype(float)
    work["SalaryK"] = work["Salary"].astype(float) / 1000.0
    work["ValueRankScore"] = work["ValueEdgeFP"] + ((12.0 - work["SalaryK"]).clip(lower=0.0) * 0.35)
    ranked = work.sort_values(["ValueRankScore", "ValueEdgeFP"], ascending=[False, False]).head(max_items)

    edges = []
    for _, row in ranked.iterrows():
        edges.append(
            {
                "player": str(row.get("Name", "")),
                "team": str(row.get("Team", "")),
                "position": str(row.get("Position", "")),
                "salary": float(_safe_float(row.get("Salary"), 0.0)),
                "form_projection": float(_safe_float(row.get("Form"), 0.0)),
                "tank01_prop_fp": float(_safe_float(row.get("Tank01PropFP"), 0.0)),
                "value_edge_fp": float(round(_safe_float(row.get("ValueEdgeFP"), 0.0), 3)),
                "value_score": float(round(_safe_float(row.get("Tank01ValueScore"), 0.0), 3)),
            }
        )

    return {"candidate_count": int(len(work.index)), "top_edges": edges}


def _collect_tank01_stat_rows(node, sink: List[dict]) -> None:
    if isinstance(node, dict):
        keys = set(node.keys())
        has_player = any(k in keys for k in ["playerID", "playerId", "player_id"])
        has_stat = any(k in keys for k in ["pts", "points", "reb", "assists", "ast"])
        if has_player and has_stat:
            sink.append(node)
        for value in node.values():
            _collect_tank01_stat_rows(value, sink)
    elif isinstance(node, list):
        for item in node:
            _collect_tank01_stat_rows(item, sink)


def _tank01_actual_fp(row: dict) -> float:
    points = _safe_float(row.get("pts", row.get("points")), 0.0)
    rebounds = _safe_float(row.get("reb", row.get("rebounds")), 0.0)
    assists = _safe_float(row.get("ast", row.get("assists")), 0.0)
    steals = _safe_float(row.get("stl", row.get("steals")), 0.0)
    blocks = _safe_float(row.get("blk", row.get("blocks")), 0.0)
    turnovers = _safe_float(row.get("turnover", row.get("to", row.get("tov"))), 0.0)
    return (
        points * POINTS_WEIGHT
        + rebounds * REBOUNDS_WEIGHT
        + assists * ASSISTS_WEIGHT
        + steals * STEALS_WEIGHT
        + blocks * BLOCKS_WEIGHT
        + turnovers * TURNOVERS_WEIGHT
    )


def _load_tank01_actuals_by_date(run_date: str, data_root: Path) -> Dict[str, float]:
    payload = {}
    for path in _candidate_tank01_boxscore_paths(run_date, data_root):
        payload = _load_json_payload(path)
        if payload:
            break
    if not payload:
        return {}

    rows: List[dict] = []
    _collect_tank01_stat_rows(payload.get("body", payload), rows)
    by_player_id: Dict[str, float] = {}
    for row in rows:
        pid = str(row.get("playerID") or row.get("playerId") or row.get("player_id") or "").strip()
        if not pid:
            continue
        by_player_id[pid] = _tank01_actual_fp(row)
    return by_player_id


def run_tank01_props_backtest(data_root: Path, lookback_days: int = 21) -> dict:
    props_dir = data_root / "nba" / "betting-props"
    if not props_dir.exists():
        return {"samples": 0, "dates_with_props": 0, "dates_with_actuals": 0, "mae": None, "rmse": None}

    prop_files = sorted(props_dir.glob("*.json"))
    if not prop_files:
        return {"samples": 0, "dates_with_props": 0, "dates_with_actuals": 0, "mae": None, "rmse": None}

    prop_files = prop_files[-max(1, lookback_days) :]
    abs_errors: List[float] = []
    sq_errors: List[float] = []
    dates_with_actuals = 0

    for path in prop_files:
        run_date = path.stem
        props_index = load_tank01_props_index(run_date, data_root)
        if not props_index.get("by_player_id"):
            continue
        actuals = _load_tank01_actuals_by_date(run_date, data_root)
        if not actuals:
            continue
        dates_with_actuals += 1
        for pid, prop_row in props_index["by_player_id"].items():
            if pid not in actuals:
                continue
            pred = _safe_float(prop_row.get("prop_fp"), 0.0)
            actual = _safe_float(actuals.get(pid), 0.0)
            err = actual - pred
            abs_errors.append(abs(err))
            sq_errors.append(err * err)

    if not abs_errors:
        return {
            "samples": 0,
            "dates_with_props": len(prop_files),
            "dates_with_actuals": dates_with_actuals,
            "mae": None,
            "rmse": None,
        }

    return {
        "samples": len(abs_errors),
        "dates_with_props": len(prop_files),
        "dates_with_actuals": dates_with_actuals,
        "mae": round(float(np.mean(abs_errors)), 4),
        "rmse": round(float(np.sqrt(np.mean(sq_errors))), 4),
    }


def _candidate_espn_injury_paths(explicit_path: str = "") -> List[Path]:
    script_dir = Path(__file__).resolve().parent
    return [
        Path(explicit_path) if explicit_path else Path(""),
        Path.cwd() / "projects" / "pete-dfs" / "data-lake" / "nba" / "injuries" / "latest.json",
        script_dir.parent / "data-lake" / "nba" / "injuries" / "latest.json",
    ]


def resolve_espn_injury_path(explicit_path: str = "") -> Path:
    for candidate in _candidate_espn_injury_paths(explicit_path):
        if not candidate or str(candidate).strip() == "":
            continue
        if candidate.exists():
            return candidate
    return (
        Path(explicit_path)
        if explicit_path and str(explicit_path).strip() != ""
        else (Path.cwd() / "projects" / "pete-dfs" / "data-lake" / "nba" / "injuries" / "latest.json")
    )


def refresh_espn_injuries(run_date: str, out_path: Path) -> dict:
    try:
        import importlib.util
        import sys

        script_path = Path(__file__).resolve().parent / "sync_espn_injuries.py"
        spec = importlib.util.spec_from_file_location("sync_espn_injuries", script_path)
        if spec is None or spec.loader is None:
            return {"ok": False, "error": "sync_espn_injuries spec unavailable", "path": str(out_path)}
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        module.sync_espn_injuries(target_date=run_date, out_path=out_path, sleep_ms=0)
        return {"ok": True, "path": str(out_path)}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "path": str(out_path)}


def load_espn_injury_index(path: Path) -> dict:
    if not path.exists():
        return {"records": {}, "name_records": {}, "source": str(path), "count": 0}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"records": {}, "name_records": {}, "source": str(path), "count": 0}

    teams = payload.get("teams", {}) if isinstance(payload, dict) else {}
    records: Dict[Tuple[str, str], dict] = {}
    by_name: Dict[str, List[dict]] = {}
    count = 0

    for team_raw, players in teams.items():
        team = normalize_team_code(team_raw)
        for row in players if isinstance(players, list) else []:
            if not isinstance(row, dict):
                continue
            player = str(row.get("player", "")).strip()
            if not player:
                continue
            status = str(row.get("status", "")).strip()
            detail = str(row.get("detail", "")).strip()
            merged_text = f"{status} {detail}".strip()
            category = classify_injury_status(merged_text)
            player_key = _clean_player_name(player)
            entry = {
                "player": player,
                "team": team,
                "status": status,
                "detail": detail,
                "category": category,
            }
            records[(team, player_key)] = entry
            by_name.setdefault(player_key, []).append(entry)
            count += 1

    return {"records": records, "name_records": by_name, "source": str(path), "count": count}


def apply_injury_overlays(
    df: pd.DataFrame,
    injury_index: dict,
    questionable_penalty: float = 1.75,
) -> Tuple[pd.DataFrame, dict]:
    if df.empty:
        return df, {"rows_total": 0, "rows_removed_hard_out": 0, "questionable_soft_penalized": 0, "source_mix": {}}

    work = df.copy()
    work["Team"] = work["Team"].map(normalize_team_code)
    work["CSV Status"] = work["Playing Status"].astype(str)
    work["ESPN Status"] = ""
    work["Merged Status"] = "available"
    work["Status Source"] = "none"
    work["InjuryPenalty"] = 0.0

    records = injury_index.get("records", {})
    name_records = injury_index.get("name_records", {})
    source_mix: Dict[str, int] = {"none": 0, "csv_primary": 0, "espn_only": 0}
    questionable_count = 0
    removed_count = 0

    def find_espn_entry(team: str, player_key: str) -> Optional[dict]:
        exact = records.get((team, player_key))
        if exact:
            return exact
        candidates = name_records.get(player_key, [])
        if len(candidates) == 1:
            return candidates[0]
        return None

    for idx, row in work.iterrows():
        player_key = _clean_player_name(row.get("Name", ""))
        team = normalize_team_code(row.get("Team", ""))
        csv_status = str(row.get("CSV Status", "")).strip()
        csv_cat = classify_injury_status(csv_status)
        espn = find_espn_entry(team, player_key)
        espn_status = ""
        espn_cat = "available"
        if espn:
            espn_status = str(espn.get("status") or espn.get("detail") or "").strip()
            espn_cat = str(espn.get("category") or "available")

        # CSV is source-of-truth when it carries an explicit status.
        if csv_cat in {"out", "questionable", "probable"}:
            merged = csv_cat
        else:
            if espn_cat in {"out", "questionable", "probable"}:
                merged = espn_cat
            else:
                merged = "available"

        if csv_cat != "available":
            source = "csv_primary"
        elif espn:
            source = "espn_only"
        else:
            source = "none"
        source_mix[source] = source_mix.get(source, 0) + 1

        penalty = questionable_penalty if merged == "questionable" else 0.0
        if merged == "questionable":
            questionable_count += 1
        if merged == "out":
            removed_count += 1

        work.at[idx, "ESPN Status"] = espn_status
        work.at[idx, "Merged Status"] = merged
        work.at[idx, "Status Source"] = source
        work.at[idx, "InjuryPenalty"] = penalty

    filtered = work[work["Merged Status"] != "out"].copy().reset_index(drop=True)
    summary = {
        "rows_total": int(len(work.index)),
        "rows_removed_hard_out": int(removed_count),
        "rows_after_filter": int(len(filtered.index)),
        "questionable_soft_penalized": int(questionable_count),
        "source_mix": source_mix,
        "source_of_truth": "draftstars_csv",
        "injury_feed_records": int(injury_index.get("count", 0)),
        "injury_feed_path": injury_index.get("source", ""),
    }
    return filtered, summary


def _candidate_schedule_paths(data_root: Path) -> List[Path]:
    if not data_root.exists():
        return []
    return sorted(data_root.glob("nba/season=*/schedule.json"))


def load_schedule_rows(data_root: Path) -> List[dict]:
    rows: List[dict] = []
    for path in _candidate_schedule_paths(data_root):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        games = payload.get("games", []) if isinstance(payload, dict) else []
        for row in games if isinstance(games, list) else []:
            if not isinstance(row, dict):
                continue
            rows.append(
                {
                    "game_id": str(row.get("game_id", "")),
                    "game_date": str(row.get("game_date", "")),
                    "home_team": normalize_team_code(row.get("home_team", "")),
                    "away_team": normalize_team_code(row.get("away_team", "")),
                    "status_num": int(row.get("status_num") or 0),
                }
            )
    return [row for row in rows if row.get("game_id")]


def build_today_opponent_map(schedule_rows: List[dict], run_date: str) -> Dict[str, str]:
    opponents: Dict[str, str] = {}
    for row in schedule_rows:
        if row.get("game_date") != run_date:
            continue
        home = normalize_team_code(row.get("home_team", ""))
        away = normalize_team_code(row.get("away_team", ""))
        if home and away:
            opponents[home] = away
            opponents[away] = home
    return opponents


def _load_game_boxscore_rows(game_id: str, data_root: Path, cache: Dict[str, List[dict]]) -> List[dict]:
    if game_id in cache:
        return cache[game_id]

    candidates = sorted(data_root.glob(f"nba/season=*/processed/player_boxscore_jsonl/{game_id}.jsonl"))
    rows: List[dict] = []
    for path in candidates:
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
        except Exception:
            continue
    cache[game_id] = rows
    return rows


def _h2h_fp(row: dict) -> float:
    points = _safe_float(row.get("points"), 0.0)
    rebounds = _safe_float(row.get("rebounds"), 0.0)
    assists = _safe_float(row.get("assists"), 0.0)
    steals = _safe_float(row.get("steals"), 0.0)
    turnovers = _safe_float(row.get("turnovers"), 0.0)
    return (
        points * POINTS_WEIGHT
        + rebounds * REBOUNDS_WEIGHT
        + assists * ASSISTS_WEIGHT
        + steals * STEALS_WEIGHT
        + turnovers * TURNOVERS_WEIGHT
    )


def apply_h2h_adjustments(
    df: pd.DataFrame,
    run_date: str,
    data_root: Path,
    weight: float = 0.25,
    cap_abs: float = 8.0,
    min_samples: int = 3,
) -> Tuple[pd.DataFrame, dict]:
    if df.empty:
        return df, {"players_with_h2h": 0, "players_adjusted": 0, "opponents_found": 0}

    work = df.copy()
    work["Team"] = work["Team"].map(normalize_team_code)
    work["Opponent"] = ""
    work["H2HSamples"] = 0
    work["H2HAvgFP"] = np.nan
    work["H2HAdj"] = 0.0

    schedule_rows = load_schedule_rows(data_root)
    opponents = build_today_opponent_map(schedule_rows, run_date)
    game_cache: Dict[str, List[dict]] = {}

    adjusted = 0
    with_h2h = 0

    for idx, row in work.iterrows():
        team = normalize_team_code(row.get("Team", ""))
        opponent = opponents.get(team, "")
        work.at[idx, "Opponent"] = opponent
        if not team or not opponent:
            continue

        prior_games = [
            g
            for g in schedule_rows
            if g.get("game_date", "") < run_date
            and {g.get("home_team"), g.get("away_team")} == {team, opponent}
            and int(g.get("status_num") or 0) >= 2
        ]
        prior_games.sort(key=lambda g: (g.get("game_date", ""), g.get("game_id", "")), reverse=True)
        target_games = prior_games[:5]
        if not target_games:
            continue

        player_key = _clean_player_name(row.get("Name", ""))
        values: List[float] = []
        for game in target_games:
            box_rows = _load_game_boxscore_rows(game.get("game_id", ""), data_root, game_cache)
            for box in box_rows:
                if normalize_team_code(box.get("team", "")) != team:
                    continue
                if _clean_player_name(box.get("player_name", "")) != player_key:
                    continue
                values.append(_h2h_fp(box))
                break

        if not values:
            continue

        with_h2h += 1
        avg_fp = float(np.mean(values))
        raw_delta = avg_fp - float(row.get("Form", 0.0))
        adj = 0.0
        if len(values) >= min_samples:
            adj = max(-cap_abs, min(cap_abs, raw_delta * weight))
            if abs(adj) > 0:
                adjusted += 1

        work.at[idx, "H2HSamples"] = int(len(values))
        work.at[idx, "H2HAvgFP"] = round(avg_fp, 4)
        work.at[idx, "H2HAdj"] = round(float(adj), 4)

    summary = {
        "opponents_found": len(opponents),
        "players_with_h2h": int(with_h2h),
        "players_adjusted": int(adjusted),
        "h2h_weight": float(weight),
        "h2h_cap_abs": float(cap_abs),
        "h2h_min_samples": int(min_samples),
    }
    return work, summary


def _canonical_column_map(columns: Iterable[str]) -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for raw in columns:
        token = _clean_label(raw)
        mapping[token] = raw
    return mapping


def load_and_clean_daily_csv(daily_csv_path: str) -> pd.DataFrame:
    frame = pd.read_csv(daily_csv_path)
    colmap = _canonical_column_map(frame.columns)

    required = {
        "name": ["name", "player", "playername", "displayname"],
        "salary": ["salary", "sal"],
        "form": ["form", "projection", "projected", "fppg"],
        "position": ["position", "pos"],
    }

    resolved: Dict[str, str] = {}
    for key, candidates in required.items():
        source = next((colmap[c] for c in candidates if c in colmap), "")
        if not source:
            raise ValueError(f"Missing required column for {key}: {candidates}")
        resolved[key] = source

    status_col = next((colmap[c] for c in ["playingstatus", "status", "injurystatus"] if c in colmap), "")
    team_col = next((colmap[c] for c in ["team", "teamname"] if c in colmap), "")
    fppg_col = next((colmap[c] for c in ["fppg", "avg", "average"] if c in colmap), "")

    cleaned = pd.DataFrame()
    cleaned["Name"] = frame[resolved["name"]]
    cleaned["Position"] = frame[resolved["position"]]
    cleaned["Salary"] = pd.to_numeric(frame[resolved["salary"]], errors="coerce")
    cleaned["Form"] = pd.to_numeric(frame[resolved["form"]], errors="coerce")
    cleaned["FPPG"] = pd.to_numeric(frame[fppg_col], errors="coerce") if fppg_col else np.nan
    cleaned["Playing Status"] = frame[status_col] if status_col else ""
    cleaned["Team"] = frame[team_col] if team_col else ""

    cleaned = cleaned.dropna(subset=["Name", "Position", "Salary", "Form"]).copy()
    cleaned["Name"] = cleaned["Name"].astype(str).str.strip()
    cleaned["Position"] = cleaned["Position"].astype(str).str.upper().str.strip()
    cleaned["Playing Status"] = cleaned["Playing Status"].astype(str)
    cleaned["Team"] = cleaned["Team"].astype(str).map(normalize_team_code)

    return cleaned.reset_index(drop=True)


def _to_builtin_rows(rows: List[dict]) -> List[dict]:
    normalized: List[dict] = []
    for row in rows:
        clean: Dict[str, object] = {}
        for key, value in row.items():
            if isinstance(value, (np.floating, np.integer)):
                clean[key] = float(value)
            else:
                clean[key] = value
        normalized.append(clean)
    return normalized


def _slot_counts(lineup_rows: List[dict]) -> Dict[str, int]:
    counts = {slot: 0 for slot in ROSTER_SLOTS}
    for row in lineup_rows:
        for slot in _position_set(str(row.get("Position", ""))):
            counts[slot] += 1
    return counts


def _derive_smokies(candidate_df: pd.DataFrame, lineup_rows: List[dict], max_items: int = 3) -> List[dict]:
    if candidate_df.empty or not lineup_rows:
        return []

    if "FPPG" not in candidate_df.columns:
        return []

    work = candidate_df.copy()
    if work["FPPG"].isna().all():
        return []

    lineup_names = {_clean_player_name(row.get("Name", "")) for row in lineup_rows}
    work["NameKey"] = work["Name"].map(_clean_player_name)
    work["Delta"] = work["Form"] - work["FPPG"].fillna(work["Form"])
    work = work[work["NameKey"].isin(lineup_names)].copy()
    if work.empty:
        return []

    salary_cutoff = float(work["Salary"].quantile(0.45))
    work = work[(work["Salary"] <= salary_cutoff) & (work["Delta"] > 0.0)].copy()
    if work.empty:
        return []

    work = work.sort_values(["Delta", "Form"], ascending=[False, False]).head(max_items)
    smokies: List[dict] = []
    for _, row in work.iterrows():
        smokies.append(
            {
                "player": str(row.get("Name", "")),
                "team": str(row.get("Team", "")),
                "position": str(row.get("Position", "")),
                "salary": float(row.get("Salary", 0.0)),
                "projection": float(row.get("Form", 0.0)),
                "baseline": float(row.get("FPPG", 0.0)),
                "delta": float(round(row.get("Delta", 0.0), 3)),
            }
        )
    return smokies


def build_mission_control_payload(
    result: EngineResult,
    daily_csv_path: str,
    slot: str,
    lookback_days: int,
    salary_cap: int,
    risk_penalty: float,
    train_days: int,
    run_date: str,
    smokies: Optional[List[dict]] = None,
) -> dict:
    generated_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
    status = "done" if result.success else "blocked"
    call_id = f"pete-dfs-{run_date}-{slot}"

    lineup_rows = _to_builtin_rows(result.lineup)
    salary_used = float(result.total_salary or 0.0)
    projected_form = float(result.projected_form or 0.0)
    smokie_rows = smokies or []
    selection_reasons: List[dict] = []
    for row in lineup_rows:
        selection_reasons.append(
            {
                "player": row.get("Name", ""),
                "team": row.get("Team", ""),
                "baseline_form": _safe_float(row.get("Form"), 0.0),
                "h2h_adjustment": _safe_float(row.get("H2HAdj"), 0.0),
                "tank01_prop_adjustment": _safe_float(row.get("Tank01PropAdj"), 0.0),
                "tank01_prop_fp": _safe_float(row.get("Tank01PropFP"), 0.0),
                "injury_penalty": _safe_float(row.get("InjuryPenalty"), 0.0),
                "final_projection": _safe_float(row.get("SelectedProjection"), _safe_float(row.get("Form"), 0.0)),
                "h2h_samples": int(_safe_float(row.get("H2HSamples"), 0.0)),
                "opponent": row.get("Opponent", ""),
                "merged_status": row.get("Merged Status", "available"),
                "status_source": row.get("Status Source", "none"),
                "tank01_player_id": row.get("Tank01PlayerID", ""),
            }
        )

    return {
        "schema_version": "1.0",
        "module": "pete_dfs",
        "queue_item": {
            "call_id": call_id,
            "owner": "Pete",
            "due_at": f"{run_date}T09:00:00",
            "status": status,
            "priority": "high",
            "blocker": None if result.success else result.reason,
            "last_update": generated_at,
        },
        "config": {
            "daily_csv_path": str(daily_csv_path),
            "slot": slot,
            "lookback_days": int(lookback_days),
            "train_days": int(train_days),
            "salary_cap": int(salary_cap),
            "risk_penalty": float(risk_penalty),
        },
        "dfs_lineup": {
            "success": bool(result.success),
            "reason": result.reason,
            "format": "2 PG, 2 SG, 2 SF, 2 PF, 1 C",
            "selected_count": len(lineup_rows),
            "slot_counts": _slot_counts(lineup_rows),
            "salary_cap": int(salary_cap),
            "salary_used": round(salary_used, 2),
            "salary_remaining": round(float(salary_cap) - salary_used, 2),
            "projected_form": round(projected_form, 3),
            "lineup": lineup_rows,
            "smokies": smokie_rows,
            "selection_reasons": selection_reasons,
        },
        "model_quality": {
            "backtest": result.backtest,
            "scrape": result.scrape,
        },
        "injury_source_summary": result.injury_summary or {},
        "h2h_summary": result.h2h_summary or {},
        "tank01_summary": result.tank01_summary or {},
        "tank01_backtest": result.tank01_backtest or {},
        "value_detection": result.value_summary or {},
    }


def write_mission_control_payload(payload: dict, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return out_path


def _position_set(raw_position: str) -> set:
    tokens = re.split(r"[\s,/|-]+", str(raw_position or "").upper())
    return {tok for tok in tokens if tok in ROSTER_SLOTS}


def _lineup_constraints(df: pd.DataFrame, salary_cap: int) -> List:
    n = len(df.index)
    constraints: List = []

    constraints.append(LinearConstraint(np.ones(n), 9, 9))

    for slot, count in ROSTER_SLOTS.items():
        mask = np.array([1.0 if slot in _position_set(pos) else 0.0 for pos in df["Position"]], dtype=float)
        constraints.append(LinearConstraint(mask, count, count))

    constraints.append(LinearConstraint(df["Salary"].astype(float).to_numpy(), -np.inf, salary_cap))

    for name in df["Name"].astype(str).tolist():
        mask = (df["Name"].astype(str) == name).astype(float).to_numpy()
        if mask.sum() > 1:
            constraints.append(LinearConstraint(mask, -np.inf, 1.0))

    return constraints


def optimize_dfs_lineup(df: pd.DataFrame, variance_map: Dict[str, float], salary_cap: int = DEFAULT_SALARY_CAP, risk_penalty: float = 0.15) -> dict:
    if not SCIPY_AVAILABLE:
        return {"success": False, "reason": "scipy unavailable", "lineup": pd.DataFrame()}

    if df.empty:
        return {"success": False, "reason": "no candidates after cleaning", "lineup": pd.DataFrame()}

    risk = np.array([variance_map.get(_clean_player_name(name), 0.0) for name in df["Name"]], dtype=float)
    projection_col = "AdjForm" if "AdjForm" in df.columns else "Form"
    objective = -(df[projection_col].astype(float).to_numpy() - (risk_penalty * risk))

    constraints = _lineup_constraints(df, salary_cap=salary_cap)

    result = milp(
        c=objective,
        constraints=constraints,
        integrality=np.ones(len(df.index), dtype=int),
        bounds=Bounds(np.zeros(len(df.index)), np.ones(len(df.index))),
    )

    if not getattr(result, "success", False):
        return {
            "success": False,
            "reason": f"optimization failed: {getattr(result, 'message', 'unknown')}",
            "lineup": pd.DataFrame(),
        }

    picks = np.where(np.rint(result.x).astype(int) == 1)[0]
    lineup = df.iloc[picks].copy().reset_index(drop=True)
    lineup["RiskStd"] = lineup["Name"].map(lambda name: variance_map.get(_clean_player_name(name), 0.0))
    lineup["SelectedProjection"] = lineup[projection_col].astype(float)

    return {
        "success": True,
        "reason": "ok",
        "lineup": lineup,
        "solver_fun": float(getattr(result, "fun", 0.0)),
    }


def run_rolling_backtest(history_df: pd.DataFrame, train_days: int = 7) -> dict:
    if history_df.empty or "Date" not in history_df.columns:
        return {"samples": 0, "windows": 0, "mae": None, "rmse": None}

    work = history_df.copy()
    work["Date"] = pd.to_datetime(work["Date"], errors="coerce").dt.date
    work = work.dropna(subset=["Date", "Name", "FP"])
    if work.empty:
        return {"samples": 0, "windows": 0, "mae": None, "rmse": None}

    unique_dates = sorted(work["Date"].unique())
    if len(unique_dates) <= train_days:
        return {"samples": 0, "windows": 0, "mae": None, "rmse": None}

    abs_errors: List[float] = []
    sq_errors: List[float] = []
    windows = 0

    for idx in range(train_days, len(unique_dates)):
        train_set = set(unique_dates[idx - train_days : idx])
        test_date = unique_dates[idx]

        train_df = work[work["Date"].isin(train_set)]
        test_df = work[work["Date"] == test_date]
        if train_df.empty or test_df.empty:
            continue

        per_player_mean = train_df.groupby("Name")["FP"].mean().to_dict()
        test_df = test_df[test_df["Name"].isin(per_player_mean.keys())]
        if test_df.empty:
            continue

        pred = test_df["Name"].map(per_player_mean).astype(float)
        actual = test_df["FP"].astype(float)
        errs = (actual - pred).to_numpy()

        abs_errors.extend(np.abs(errs).tolist())
        sq_errors.extend((errs ** 2).tolist())
        windows += 1

    if not abs_errors:
        return {"samples": 0, "windows": windows, "mae": None, "rmse": None}

    mae = float(np.mean(abs_errors))
    rmse = float(np.sqrt(np.mean(sq_errors)))
    return {"samples": len(abs_errors), "windows": windows, "mae": round(mae, 4), "rmse": round(rmse, 4)}


def run_pete_dfs_engine(
    daily_csv_path: str,
    run_date: str,
    lookback_days: int = 10,
    salary_cap: int = DEFAULT_SALARY_CAP,
    risk_penalty: float = 0.15,
    train_days: int = 7,
    espn_injuries_json: str = "",
    data_root: str = "",
    refresh_injuries: bool = False,
    h2h_weight: float = 0.25,
    h2h_cap_abs: float = 8.0,
    h2h_min_samples: int = 3,
    questionable_penalty: float = 1.75,
    tank01_props_weight: float = DEFAULT_TANK01_PROPS_WEIGHT,
    tank01_props_cap_abs: float = DEFAULT_TANK01_PROPS_CAP,
    tank01_backtest_days: int = 21,
    tank01_max_lag_days: int = 2,
) -> EngineResult:
    print(f"PETE DFS ENGINE: processing {daily_csv_path}")
    print(f"Collecting ESPN history over last {lookback_days} days...")

    history_df, scrape = collect_espn_history_logs(lookback_days=lookback_days)
    variance_map = compute_variance_map(history_df)
    backtest = run_rolling_backtest(history_df, train_days=train_days)

    df = load_and_clean_daily_csv(daily_csv_path)
    root = Path(data_root) if data_root else (Path.cwd() / "projects" / "pete-dfs" / "data-lake")

    tank01_players_index = load_tank01_players_index(run_date, root, max_lag_days=max(0, tank01_max_lag_days))
    df, tank01_players_summary = apply_tank01_player_mapping(df, tank01_players_index)

    tank01_props_index = load_tank01_props_index(run_date, root, max_lag_days=max(0, tank01_max_lag_days))
    df, tank01_props_summary = apply_tank01_prop_projection_signal(
        df,
        tank01_props_index,
        weight=max(0.0, tank01_props_weight),
        cap_abs=max(0.0, tank01_props_cap_abs),
    )

    tank01_injury_index = load_tank01_injury_index(run_date, root, max_lag_days=max(0, tank01_max_lag_days))
    tank01_backtest = run_tank01_props_backtest(root, lookback_days=max(1, tank01_backtest_days))
    tank01_summary = {
        "players": tank01_players_summary,
        "props": tank01_props_summary,
        "injuries": {
            "source": tank01_injury_index.get("source", ""),
            "records": int(tank01_injury_index.get("count", 0)),
        },
    }

    injury_path = resolve_espn_injury_path(espn_injuries_json)
    refresh_status = {"ok": False, "path": str(injury_path), "skipped": True}
    if refresh_injuries:
        refresh_status = refresh_espn_injuries(run_date, injury_path)
        refresh_status["skipped"] = False
    espn_injury_index = load_espn_injury_index(injury_path)
    injury_index = merge_injury_indexes(tank01_injury_index, espn_injury_index)
    df, injury_summary = apply_injury_overlays(df, injury_index, questionable_penalty=questionable_penalty)
    injury_summary["refresh"] = refresh_status
    injury_summary["feeds"] = {
        "tank01_records": int(tank01_injury_index.get("count", 0)),
        "espn_records": int(espn_injury_index.get("count", 0)),
    }

    df, h2h_summary = apply_h2h_adjustments(
        df,
        run_date=run_date,
        data_root=root,
        weight=h2h_weight,
        cap_abs=h2h_cap_abs,
        min_samples=max(1, h2h_min_samples),
    )

    if "H2HAdj" not in df.columns:
        df["H2HAdj"] = 0.0
    if "InjuryPenalty" not in df.columns:
        df["InjuryPenalty"] = 0.0
    if "Tank01PropAdj" not in df.columns:
        df["Tank01PropAdj"] = 0.0
    df["AdjForm"] = (
        df["Form"].astype(float)
        + df["H2HAdj"].astype(float)
        + df["Tank01PropAdj"].astype(float)
        - df["InjuryPenalty"].astype(float)
    )
    value_summary = build_value_detection_summary(df)
    result = optimize_dfs_lineup(df, variance_map, salary_cap=salary_cap, risk_penalty=risk_penalty)

    if not result["success"]:
        print(f"Optimization failed: {result['reason']}")
        return EngineResult(
            success=False,
            reason=result["reason"],
            lineup=[],
            total_salary=0.0,
            projected_form=0.0,
            backtest=backtest,
            scrape=scrape,
            injury_summary=injury_summary,
            h2h_summary=h2h_summary,
            tank01_summary=tank01_summary,
            value_summary=value_summary,
            tank01_backtest=tank01_backtest,
        )

    lineup_df = result["lineup"].sort_values(["Position", "Name"]).reset_index(drop=True)
    total_salary = float(lineup_df["Salary"].sum())
    projected_form = float(lineup_df["SelectedProjection"].sum() if "SelectedProjection" in lineup_df.columns else lineup_df["Form"].sum())
    smokies = _derive_smokies(df, lineup_df.to_dict(orient="records"))

    print("\n--- FINAL OPTIMIZED LINEUP ---")
    display_cols = [
        "Position",
        "Name",
        "Team",
        "Salary",
        "Form",
        "H2HAdj",
        "Tank01PropAdj",
        "InjuryPenalty",
        "SelectedProjection",
        "RiskStd",
    ]
    display_cols = [col for col in display_cols if col in lineup_df.columns]
    print(lineup_df[display_cols].to_string(index=False))
    print(f"\nTotal Salary: ${total_salary:,.0f} | Projected Form: {projected_form:.2f}")
    if smokies:
        print("Top Smokies:")
        for idx, row in enumerate(smokies, 1):
            print(f"{idx}. {row['player']} ({row['position']}) salary=${row['salary']:.0f} delta=+{row['delta']:.2f}")
    print(f"Backtest: {backtest}")
    print(f"Scrape: {scrape}")
    print(f"Injury Summary: {injury_summary}")
    print(f"H2H Summary: {h2h_summary}")
    print(f"Tank01 Summary: {tank01_summary}")
    print(f"Tank01 Backtest: {tank01_backtest}")
    print(f"Value Detection: {value_summary}")

    return EngineResult(
        success=True,
        reason="ok",
        lineup=lineup_df.to_dict(orient="records"),
        total_salary=total_salary,
        projected_form=projected_form,
        backtest=backtest,
        scrape=scrape,
        injury_summary=injury_summary,
        h2h_summary=h2h_summary,
        tank01_summary=tank01_summary,
        value_summary=value_summary,
        tank01_backtest=tank01_backtest,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Pete DFS engine with ESPN history and lineup optimization")
    parser.add_argument("daily_csv_path", help="Path to Draftstars daily player CSV")
    parser.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"), help="Run date in YYYY-MM-DD")
    parser.add_argument("--slot", choices=["all", "early", "late"], default="all", help="Slate window label")
    parser.add_argument("--lookback-days", type=int, default=10, help="Days of ESPN history to ingest")
    parser.add_argument("--salary-cap", type=int, default=DEFAULT_SALARY_CAP, help="Salary cap for optimizer")
    parser.add_argument("--risk-penalty", type=float, default=0.15, help="Penalty multiplier for high-variance players")
    parser.add_argument("--train-days", type=int, default=7, help="Rolling train window days for backtest")
    parser.add_argument("--espn-injuries-json", default="", help="Path to ESPN injuries JSON (sync target and fallback)")
    parser.add_argument(
        "--refresh-espn-injuries",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Refresh ESPN injury feed before optimization (default: false; CSV remains source-of-truth)",
    )
    parser.add_argument(
        "--data-root",
        default=str(Path.cwd() / "projects" / "pete-dfs" / "data-lake"),
        help="Local data-lake root for H2H lookups",
    )
    parser.add_argument("--h2h-weight", type=float, default=0.25, help="Weight for H2H adjustment blending")
    parser.add_argument("--h2h-cap-abs", type=float, default=8.0, help="Absolute cap for H2H adjustment points")
    parser.add_argument("--h2h-min-samples", type=int, default=3, help="Minimum H2H samples before applying adjustment")
    parser.add_argument("--questionable-penalty", type=float, default=1.75, help="Projection penalty for questionable players")
    parser.add_argument("--tank01-props-weight", type=float, default=DEFAULT_TANK01_PROPS_WEIGHT, help="Tank01 props blend weight into projection")
    parser.add_argument("--tank01-props-cap-abs", type=float, default=DEFAULT_TANK01_PROPS_CAP, help="Absolute cap for Tank01 props adjustment points")
    parser.add_argument("--tank01-backtest-days", type=int, default=21, help="How many recent Tank01 prop dates to include in backtest summary")
    parser.add_argument("--tank01-max-lag-days", type=int, default=2, help="Max file-date lag to accept for Tank01 dated snapshots")
    parser.add_argument(
        "--mission-control-json",
        default="",
        help="Output path for Mission Control JSON payload (default: OPENCLAW_WORKSPACE/logs/Pete/<date>-pete-dfs.json)",
    )
    args = parser.parse_args()

    result = run_pete_dfs_engine(
        args.daily_csv_path,
        run_date=args.date,
        lookback_days=max(1, args.lookback_days),
        salary_cap=max(1000, args.salary_cap),
        risk_penalty=max(0.0, args.risk_penalty),
        train_days=max(1, args.train_days),
        espn_injuries_json=args.espn_injuries_json,
        data_root=args.data_root,
        refresh_injuries=bool(args.refresh_espn_injuries),
        h2h_weight=max(0.0, args.h2h_weight),
        h2h_cap_abs=max(0.0, args.h2h_cap_abs),
        h2h_min_samples=max(1, args.h2h_min_samples),
        questionable_penalty=max(0.0, args.questionable_penalty),
        tank01_props_weight=max(0.0, args.tank01_props_weight),
        tank01_props_cap_abs=max(0.0, args.tank01_props_cap_abs),
        tank01_backtest_days=max(1, args.tank01_backtest_days),
        tank01_max_lag_days=max(0, args.tank01_max_lag_days),
    )

    lineup_rows = result.lineup if result.success else []
    smokies = []
    if lineup_rows:
        try:
            candidate_df = load_and_clean_daily_csv(args.daily_csv_path)
            smokies = _derive_smokies(candidate_df, lineup_rows)
        except Exception:
            smokies = []

    payload = build_mission_control_payload(
        result=result,
        daily_csv_path=args.daily_csv_path,
        slot=args.slot,
        lookback_days=max(1, args.lookback_days),
        salary_cap=max(1000, args.salary_cap),
        risk_penalty=max(0.0, args.risk_penalty),
        train_days=max(1, args.train_days),
        run_date=args.date,
        smokies=smokies,
    )

    target = (
        Path(args.mission_control_json)
        if args.mission_control_json
        else (LOG_DIR / f"{args.date}-pete-dfs.json")
    )
    saved = write_mission_control_payload(payload, target)
    print(f"Mission Control payload written: {saved}")


if __name__ == "__main__":
    main()
