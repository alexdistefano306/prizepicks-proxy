from typing import List, Dict, Any
from pathlib import Path
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse

app = FastAPI(title="PrizePicks Props Proxy (Upload-Based)")

# Where we store the latest props on the server
DATA_FILE = Path(__file__).parent / "props.json"

# Fallback dummy props if file is missing or broken
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
# Helpers
# =========================

def normalize_prizepicks(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Take raw PrizePicks-style JSON:
      {
        "data": [... projections ...],
        "included": [... players/games ...]
      }

    and convert it into a flat list of props for the model.
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

            props.append({
                "id": pid,
                "source": "uploaded",   # comes from your upload
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
            })
        except Exception:
            continue

    return props


def load_file_props() -> List[Dict[str, Any]]:
    """
    Load props from props.json if present, else use dummy.
    """
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return DUMMY_PROPS
    return DUMMY_PROPS


def save_props(props: List[Dict[str, Any]]) -> None:
    DATA_FILE.write_text(json.dumps(props, indent=2), encoding="utf-8")


# =========================
# Routes
# =========================

@app.get("/", response_class=HTMLResponse)
def index():
    return """
    <html>
      <head><title>Props Proxy</title></head>
      <body>
        <h1>Props Proxy ✅</h1>
        <p>This server serves whatever props are stored in <code>props.json</code>.</p>
        <ul>
          <li><a href="/health">/health</a></li>
          <li><a href="/props.json">/props.json</a></li>
          <li><a href="/upload">/upload</a> (paste raw PrizePicks JSON here)</li>
        </ul>
      </body>
    </html>
    """


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/props.json")
def props_json():
    """
    This is what ChatGPT / the model will read.
    """
    props = load_file_props()
    return JSONResponse(props)


@app.get("/upload", response_class=HTMLResponse)
def upload_page():
    """
    Simple HTML page with a textarea to paste raw JSON into from your phone.
    """
    return """
    <html>
      <head>
        <title>Upload PrizePicks JSON</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <style>
          body { font-family: sans-serif; padding: 1rem; }
          textarea { width: 100%; height: 300px; }
          button { padding: 0.5rem 1rem; margin-top: 0.5rem; }
          #status { margin-top: 0.5rem; white-space: pre-wrap; }
        </style>
      </head>
      <body>
        <h2>Upload PrizePicks JSON</h2>
        <p>Paste the raw JSON from the PrizePicks API below and tap "Upload".</p>
        <textarea id="raw" placeholder='{"data": [...], "included": [...]}'> </textarea>
        <br />
        <button onclick="upload()">Upload</button>
        <pre id="status"></pre>

        <script>
          async function upload() {
            const status = document.getElementById('status');
            const txt = document.getElementById('raw').value;

            let raw;
            try {
              raw = JSON.parse(txt);
            } catch (e) {
              status.textContent = "❌ Invalid JSON: " + e;
              return;
            }

            status.textContent = "Uploading...";
            try {
              const res = await fetch("/update-props", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(raw)
              });
              const data = await res.json();
              status.textContent = "✅ Uploaded " + (data.count ?? 0) + " props.";
            } catch (e) {
              status.textContent = "❌ Error: " + e;
            }
          }
        </script>
      </body>
    </html>
    """


@app.post("/update-props")
async def update_props(request: Request):
    """
    Accept raw PrizePicks JSON from the client (your phone/browser),
    normalize it, and save to props.json.
    """
    raw = await request.json()
    props = normalize_prizepicks(raw)
    save_props(props)
    return {"status": "ok", "count": len(props)}
