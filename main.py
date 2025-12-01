from typing import List, Dict, Any
import os

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

app = FastAPI(title="PrizePicks Props Proxy")

# =========================
# Config
# =========================

# Default: NFL projections URL you gave earlier.
# You can override this in Render via env var PRIZEPICKS_URL if you ever want.
DEFAULT_PRIZEPICKS_URL = (
    "https://api.prizepicks.com/projections"
    "?league_id=9&per_page=250&single_stat=true&in_game=true&game_mode=prizepools"
)
PRIZEPICKS_URL = os.getenv("PRIZEPICKS_URL", DEFAULT_PRIZEPICKS_URL)

# Fallback dummy props in case PrizePicks call fails
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
# PrizePicks fetch + normalize
# =========================

async def fetch_prizepicks_raw() -> Dict[str, Any]:
    """
    Fetch raw JSON from PrizePicks.

    NOTE: This will only work if PrizePicks allows server-side access from Render's IPs
    and you respect their Terms of Service. If they return 403, you'll see it in logs.
    """
    headers = {
        "Accept": "application/json",
        "User-Agent": "PropsProxy/1.0",
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(PRIZEPICKS_URL, headers=headers)
        if resp.status_code != 200:
            # Bubble this up so /raw and /props.json can see errors
            raise HTTPException(
                status_code=resp.status_code,
                detail=f"PrizePicks API error: {resp.status_code} {resp.text[:200]}",
            )
        return resp.json()


def normalize_prizepicks(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Convert PrizePicks JSON into a flat list of props.

    This uses common field names from their public API:
      - raw["data"]           : projections
      - raw["included"]       : players, games, etc.
      - attributes.line_score : prop line
      - attributes.stat_type  : stat name
    If their schema changes, tweak this function.
    """
    data = raw.get("data", [])
    included = raw.get("included", [])

    players: Dict[str, Dict[str, Any]] = {}
    games: Dict[str, Dict[str, Any]] = {}

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
          <li><a href="/raw">/raw</a> (raw PrizePicks JSON)</li>
          <li><a href="/props.json">/props.json</a> (normalized props for ChatGPT)</li>
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
    Debug endpoint: returns raw PrizePicks JSON so you can inspect fields.
    """
    raw_data = await fetch_prizepicks_raw()
    return JSONResponse(raw_data)


@app.get("/props.json")
async def props_json():
    """
    Main endpoint for ChatGPT: normalized list of props.
    """
    try:
        raw_data = await fetch_prizepicks_raw()
        props = normalize_prizepicks(raw_data)
        if not props:
            # Fallback to dummy if something changes in the schema
            return JSONResponse(DUMMY_PROPS)
        return JSONResponse(props)
    except HTTPException:
        # Pass through real HTTP errors from PrizePicks
        raise
    except Exception:
        # Any other error → fallback to dummy
        return JSONResponse(DUMMY_PROPS)
