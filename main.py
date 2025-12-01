from typing import List, Dict, Any
from pathlib import Path
import json

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

app = FastAPI(title="PrizePicks Props Proxy")

# =========================
# Config
# =========================

# Base PrizePicks projections URL (we'll pass query params separately)
PRIZEPICKS_URL = "https://api.prizepicks.com/projections"

# Query params – tweak league_id/game_mode if you want different leagues later
PRIZEPICKS_PARAMS = {
    "league_id": "9",           # 9 = NFL (change if needed)
    "per_page": "250",
    "single_stat": "true",
    "in_game": "true",
    "game_mode": "prizepools",
}

# Your custom User-Agent header
PRIZEPICKS_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/142.0.0.0 Safari/537.36"
    ),
}

# Local file we use as a fallback
DATA_FILE = Path(__file__).parent / "props.json"

# Dummy props if everything else fails
DUMMY_PROPS: List[Dict[str, Any]] = [
    {
        "id": "dummy-1",
        "source": "demo",
        "board": "NFL",
        "league": "NFL",
        "player": "Joe Burrow",
        "team": "CIN",
        "opponent": "BAL",
        "stat": "Passing Yards",
        "market": "passing_yards",
        "line": 255.5,
        "game_time": "2025-11-30T20:15:00-05:00",
        "projection_type": "main",
    },
    {
        "id": "dummy-2",
        "source": "demo",
        "board": "NFL",
        "league": "NFL",
        "player": "Ja'Marr Chase",
        "team": "CIN",
        "opponent": "BAL",
        "stat": "Receiving Yards",
        "market": "receiving_yards",
        "line": 74.5,
        "game_time": "2025-11-30T20:15:00-05:00",
        "projection_type": "main",
    },
]


# =========================
# Helper functions
# =========================

async def fetch_prizepicks_raw() -> Dict[str, Any]:
    """
    Try to fetch raw JSON from PrizePicks.
    They may still return 4xx (e.g., 403) depending on their rules.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            PRIZEPICKS_URL,
            headers=PRIZEPICKS_HEADERS,
            params=PRIZEPICKS_PARAMS,
        )

        if resp.status_code != 200:
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"PrizePicks API error: {resp.status_code} {resp.text[:200]}",
            )

        return resp.json()


def normalize_prizepicks(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Convert PrizePicks-style JSON into a flat list of props.
    """
    data = raw.get("data", [])
    included = raw.get("included", [])

    players: Dict[str, Dict[str, Any]] = {}
    games: Dict[str, Dict[str, Any]] = {}

    # Build lookup tables from included[]
    for item in included:
        itype = item.get("type")
        attrs = item.get("attributes", {})
        iid = item.get("id")

        if itype in ("new_player", "player"):
            players[iid] = {
                "name": attrs.get("name"),
                "team": attrs.get("team") or attrs.get("team_abbreviation") or "",
                "league": attrs.get("league") or "",
            }
        elif itype == "game":
            games[iid] = {
                "home_team": attrs.get("home_team") or attrs.get("home_team_abbreviation"),
                "away_team": attrs.get("away_team") or attrs.get("away_team_abbreviation"),
                "start_time": attrs.get("start_time") or attrs.get("start_at"),
            }

    props: List[Dict[str, Any]] = []

    for proj in data:
        try:
            pid = proj.get("id")
            attrs = proj.get("attributes", {})
            rel = proj.get("relationships", {})

            player_rel = (rel.get("new_player") or rel.get("player") or {}).get("data") or {}
            game_rel = (rel.get("game") or {}).get("data") or {}

            player_id = player_rel.get("id")
            game_id = game_rel.get("id")

            player_info = players.get(player_id, {})
            game_info = games.get(game_id, {})

            player = player_info.get("name") or "Unknown"
            team = player_info.get("team") or ""
            league = player_info.get("league") or "NFL"

            home_team = game_info.get("home_team")
            away_team = game_info.get("away_team")
            start_time = game_info.get("start_time")

            opponent = ""
            if team and home_team and away_team:
                opponent = away_team if team == home_team else home_team

            stat = attrs.get("stat_type") or attrs.get("stat") or ""
            line = attrs.get("line_score")
            if line is not None:
                line = float(line)

            # Skip incomplete entries
            if not player or line is None or not stat:
                continue

            prop_obj = {
                "id": pid,
                "source": "prizepicks",
                "board": league,
                "league": league,
                "player": player,
                "team": team,
                "opponent": opponent,
                "stat": stat,
                "market": stat.lower().replace(" ", "_"),
                "line": line,
                "game_time": start_time,
                "projection_type": "main",
            }
            props.append(prop_obj)
        except Exception:
            # Skip malformed rows
            continue

    return props


def load_file_props() -> List[Dict[str, Any]]:
    """
    Load props from props.json if it exists, else use dummy.
    """
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return DUMMY_PROPS
    return DUMMY_PROPS


# =========================
# Routes
# =========================

@app.get("/", response_class=HTMLResponse)
def index():
    return """
    <html>
      <head><title>PrizePicks Props Proxy</title></head>
      <body>
        <h1>PrizePicks Props Proxy ✅</h1>
        <p>Useful endpoints:</p>
        <ul>
          <li><a href="/health">/health</a></li>
          <li><a href="/raw">/raw</a> (PrizePicks raw JSON, if reachable)</li>
          <li><a href="/props.json">/props.json</a> (normalized props with fallback)</li>
        </ul>
      </body>
    </html>
    """


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/raw")
async def raw():
    """
    Try to return raw PrizePicks JSON using headers.
    """
    raw_data = await fetch_prizepicks_raw()
    return JSONResponse(raw_data)


@app.get("/props.json")
async def props_json():
    """
    Main endpoint:
    - Try PrizePicks with headers
    - If that fails or returns nothing useful, fall back to props.json or dummy
    """
    try:
        raw_data = await fetch_prizepicks_raw()
        props = normalize_prizepicks(raw_data)
        if not props:
            return JSONResponse(load_file_props())
        return JSONResponse(props)
    except HTTPException:
        # PrizePicks returned 4xx/5xx → use local data
        return JSONResponse(load_file_props())
    except Exception:
        # Anything else → use local data
        return JSONResponse(load_file_props())
