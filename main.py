from typing import List, Dict, Any
from pathlib import Path
import json

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

app = FastAPI(title="PrizePicks Props Proxy – Multi-Sport Board")

# -------------------------------------------------------------------
# Files
# -------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "props.json"
BACKUP_FILE = BASE_DIR / "props_backup.json"

# -------------------------------------------------------------------
# Config / constants
# -------------------------------------------------------------------

DUMMY_PROPS: List[Dict[str, Any]] = [
    {
        "id": "dummy-1",
        "source": "demo",
        "board": "NFL",
        "league": "NFL",
        "sport": "NFL",
        "player": "Joe Burrow",
        "team": "CIN",
        "opponent": "BAL",
        "stat": "Passing Yards",
        "market": "passing_yards",
        "line": 255.5,
        "game_time": "2025-12-01T20:15:00-05:00",
        "projection_type": "main",
        "tier": "standard",
    },
    {
        "id": "dummy-2",
        "source": "demo",
        "board": "NFL",
        "league": "NFL",
        "sport": "NFL",
        "player": "Ja'Marr Chase",
        "team": "CIN",
        "opponent": "BAL",
        "stat": "Receiving Yards",
        "market": "receiving_yards",
        "line": 74.5,
        "game_time": "2025-12-01T20:15:00-05:00",
        "projection_type": "main",
        "tier": "standard",
    },
]

# Sport config: UI name + expected league_id (None = no strict check)
SPORTS: Dict[str, Dict[str, Any]] = {
    "nfl": {"name": "NFL", "league_id": "9"},
    "nba": {"name": "NBA", "league_id": "7"},
    "nhl": {"name": "NHL", "league_id": "8"},
    "cbb": {"name": "CBB", "league_id": None},   # unknown league_id, skip validation
    "cfb": {"name": "CFB", "league_id": "15"},
    "soccer": {"name": "Soccer", "league_id": "82"},
    "tennis": {"name": "Tennis", "league_id": "5"},
    "cs2": {"name": "CS2", "league_id": "265"},
}


# -------------------------------------------------------------------
# Helpers: load / save props
# -------------------------------------------------------------------

def save_props(props: List[Dict[str, Any]]) -> None:
    """
    Save props to props.json and keep a backup in props_backup.json.
    """
    text = json.dumps(props, indent=2)
    DATA_FILE.write_text(text, encoding="utf-8")
    BACKUP_FILE.write_text(text, encoding="utf-8")


def load_file_props() -> List[Dict[str, Any]]:
    """
    Load props for serving (board/model). Prefer main file, fall back to backup,
    fall back to dummy props.
    """
    # Main file first
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Backup file next
    if BACKUP_FILE.exists():
        try:
            with BACKUP_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    # Final fallback
    return DUMMY_PROPS


def load_file_props_raw_or_empty() -> List[Dict[str, Any]]:
    """
    Load props for internal merging when updating. Prefer main, then backup.
    If both fail, return [] instead of dummy props.
    """
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    if BACKUP_FILE.exists():
        try:
            with BACKUP_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    return []


# -------------------------------------------------------------------
# Normalization of PrizePicks JSON
# -------------------------------------------------------------------

def normalize_prizepicks(raw: Dict[str, Any], sport_key: str) -> List[Dict[str, Any]]:
    """
    Turn a raw PrizePicks JSON blob into a simple list of props.
    Also enforces that the JSON's league_id matches the selected sport
    (when we know the league_id).
    """
    if sport_key not in SPORTS:
        raise ValueError(f"Unknown sport key: {sport_key}")
    sport_cfg = SPORTS[sport_key]
    sport_name = sport_cfg["name"]
    expected_league_id = sport_cfg["league_id"]

    data = raw.get("data", []) or []
    included = raw.get("included", []) or []

    # --- Validate league_id against the selected sport (where we know it) ---
    league_ids = set()
    for proj in data:
        attrs = proj.get("attributes", {}) or {}
        # From relationships
        rel_league = (proj.get("relationships", {}).get("league") or {}).get("data") or {}
        lid_rel = rel_league.get("id")
        if lid_rel is not None:
            league_ids.add(str(lid_rel))
        # Sometimes league_id might live in attributes
        lid_attr = attrs.get("league_id")
        if lid_attr is not None:
            league_ids.add(str(lid_attr))

    if expected_league_id and league_ids and league_ids != {expected_league_id}:
        raise ValueError(
            f"League mismatch: selected {sport_name} (league_id {expected_league_id}), "
            f"but JSON contained league ids {sorted(league_ids)}"
        )

    # --- Build helper maps from included ---
    players: Dict[str, Dict[str, Any]] = {}
    games: Dict[str, Dict[str, Any]] = {}
    teams: Dict[str, Dict[str, Any]] = {}

    for item in included:
        itype = item.get("type")
        attrs = item.get("attributes", {}) or {}
        iid = item.get("id")

        if not iid:
            continue

        # Player info
        if itype in ("new_player", "player"):
            players[iid] = {
                "name": attrs.get("name"),
                "team": attrs.get("team")
                or attrs.get("team_abbreviation")
                or "",
                "league": attrs.get("league") or sport_name,
            }

        # Team info (for mapping opponents)
        elif itype == "team":
            teams[iid] = {
                "abbreviation": attrs.get("abbreviation") or "",
                "name": attrs.get("name") or "",
                "market": attrs.get("market") or "",
            }

        # Game info + which teams are home/away
        elif itype == "game":
            rel = item.get("relationships", {}) or {}
            home_rel = (rel.get("home_team_data") or {}).get("data") or {}
            away_rel = (rel.get("away_team_data") or {}).get("data") or {}
            games[iid] = {
                "home_team_id": home_rel.get("id"),
                "away_team_id": away_rel.get("id"),
                "start_time": attrs.get("start_time") or attrs.get("start_at"),
            }

    # --- Build simple props list ---
    props: List[Dict[str, Any]] = []

    for proj in data:
        try:
            pid = proj.get("id")
            attrs = proj.get("attributes", {}) or {}
            rel = proj.get("relationships", {}) or {}

            if not pid:
                continue

            # Player & game relationships
            player_rel = (rel.get("new_player") or rel.get("player") or {}).get("data") or {}
            game_rel = (rel.get("game") or {}).get("data") or {}

            player_id = player_rel.get("id")
            game_id = game_rel.get("id")

            player_info = players.get(player_id, {})
            game_info = games.get(game_id, {})

            player = player_info.get("name") or "Unknown"
            team = player_info.get("team") or ""
            league = player_info.get("league") or sport_name

            # Resolve home/away teams to abbreviations
            home_team_abbr = None
            away_team_abbr = None
            if game_info:
                home_team_id = game_info.get("home_team_id")
                away_team_id = game_info.get("away_team_id")
                if home_team_id and home_team_id in teams:
                    home_team_abbr = teams[home_team_id]["abbreviation"]
                if away_team_id and away_team_id in teams:
                    away_team_abbr = teams[away_team_id]["abbreviation"]

            # Figure out opponent
            opponent = ""
            if team and home_team_abbr and away_team_abbr:
                if team == home_team_abbr:
                    opponent = away_team_abbr
                elif team == away_team_abbr:
                    opponent = home_team_abbr

            # Fallback: sometimes description holds a team abbr
            if not opponent and home_team_abbr and away_team_abbr:
                desc_team = attrs.get("description")
                if desc_team == home_team_abbr:
                    opponent = away_team_abbr
                elif desc_team == away_team_abbr:
                    opponent = home_team_abbr

            stat = (
                attrs.get("stat_type")
                or attrs.get("stat")
                or attrs.get("stat_display_name")
                or ""
            )
            line = attrs.get("line_score")
            if line is not None:
                line = float(line)

            start_time = game_info.get("start_time")

            # Tier (goblin/standard/demon) – if your JSON has it, use it; else standard
            tier = attrs.get("tier") or "standard"

            # Skip incomplete rows
            if not player or line is None or not stat:
                continue

            props.append(
                {
                    "id": pid,
                    "source": "uploaded",
                    "board": sport_name,
                    "league": league,
                    "sport": sport_name,
                    "player": player,
                    "team": team,
                    "opponent": opponent,
                    "stat": stat,
                    "market": str(stat).lower().replace(" ", "_"),
                    "line": line,
                    "game_time": start_time,
                    "projection_type": "main",
                    "tier": tier,
                }
            )
        except Exception:
            # If a single row blows up, just skip it
            continue

    return props


# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def board_view():
    """
    Main odds board with filters and tier pills.
    """
    return """
    <html>
      <head>
        <title>Props Board Viewer</title>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
          :root { color-scheme: dark; }
          * { box-sizing: border-box; }
          body {
            margin: 0;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: radial-gradient(circle at top, #1f2933, #020617);
            color: #e5e7eb;
          }
          header {
            padding: 1.25rem 1.5rem;
            border-bottom: 1px solid rgba(148, 163, 184, 0.3);
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 0.75rem;
            backdrop-filter: blur(10px);
            background: linear-gradient(to right, rgba(15,23,42,.95), rgba(15,23,42,.88));
            position: sticky;
            top: 0;
            z-index: 10;
          }
          header h1 {
            font-size: 1.15rem;
            margin: 0;
          }
          header h1 span {
            font-weight: 600;
            color: #38bdf8;
          }
          header nav a {
            color: #9ca3af;
            text-decoration: none;
            font-size: 0.9rem;
            margin-left: 0.75rem;
          }
          header nav a:hover { color: #e5e7eb; }
          main {
            padding: 1.25rem 1.5rem 2rem;
            max-width: 1200px;
            margin: 0 auto;
          }
          .summary {
            display: flex;
            flex-wrap: wrap;
            gap: 1rem;
            margin-bottom: 1.25rem;
          }
          .summary-card {
            flex: 1 1 220px;
            padding: 0.9rem 1rem;
            border-radius: 0.75rem;
            background: linear-gradient(135deg, rgba(15,23,42,0.95), rgba(30,64,175,0.6));
            border: 1px solid rgba(148, 163, 184, 0.35);
            box-shadow: 0 18px 40px rgba(15,23,42,0.6);
          }
          .summary-card h2 {
            margin: 0 0 0.25rem;
            font-size: 0.95rem;
            color: #9ca3af;
            font-weight: 500;
          }
          .summary-card p {
            margin: 0;
            font-size: 1.1rem;
            font-weight: 600;
          }
          .summary-card span {
            font-size: 0.8rem;
            color: #9ca3af;
          }
          .controls {
            display: flex;
            flex-wrap: wrap;
            gap: 0.75rem;
            margin-bottom: 1rem;
            align-items: center;
          }
          .controls input, .controls select {
            padding: 0.55rem 0.7rem;
            border-radius: 9999px;
            border: 1px solid rgba(148, 163, 184, 0.6);
            background-color: rgba(15,23,42,0.95);
            color: #e5e7eb;
            font-size: 0.9rem;
            outline: none;
          }
          .controls input::placeholder { color: #6b7280; }
          .controls button {
            padding: 0.55rem 0.9rem;
            border-radius: 9999px;
            border: none;
            font-size: 0.85rem;
            cursor: pointer;
            background: linear-gradient(to right, #22c55e, #16a34a);
            color: white;
            box-shadow: 0 10px 25px rgba(34,197,94,0.35);
          }
          .controls button:hover { filter: brightness(1.08); }
          .controls small {
            font-size: 0.75rem;
            color: #9ca3af;
          }
          .table-wrapper {
            border-radius: 0.9rem;
            border: 1px solid rgba(148, 163, 184, 0.4);
            overflow: hidden;
            background: rgba(15,23,42,0.96);
            box-shadow: 0 22px 45px rgba(15,23,42,0.7);
          }
          table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.82rem;
          }
          thead {
            background: linear-gradient(to right, rgba(30,64,175,0.9), rgba(55,65,81,0.95));
          }
          thead th {
            text-align: left;
            padding: 0.6rem 0.75rem;
            white-space: nowrap;
            font-weight: 500;
            color: #e5e7eb;
            border-bottom: 1px solid rgba(148, 163, 184, 0.5);
          }
          tbody tr:nth-child(even) { background-color: rgba(15,23,42,0.96); }
          tbody tr:nth-child(odd) { background-color: rgba(17,24,39,0.96); }
          tbody td {
            padding: 0.5rem 0.75rem;
            border-bottom: 1px solid rgba(55, 65, 81, 0.8);
          }
          tbody tr:hover { background-color: rgba(30,64,175,0.3); }
          .pill {
            display: inline-flex;
            align-items: center;
            padding: 0.15rem 0.5rem;
            border-radius: 9999px;
            font-size: 0.7rem;
            border: 1px solid rgba(148,163,184,0.7);
            color: #e5e7eb;
          }
          .pill.stat {
            border-color: rgba(56,189,248,0.8);
            color: #67e8f9;
          }
          .pill.league {
            border-color: rgba(74,222,128,0.8);
            color: #bbf7d0;
          }
          .pill.time {
            border-color: rgba(251,191,36,0.85);
            color: #fed7aa;
          }
          .pill.tier-standard {
            border-color: rgba(148,163,184,0.9);
            color: #e5e7eb;
          }
          .pill.tier-goblin {
            border-color: rgba(251,191,36,0.9);
            color: #fed7aa;
          }
          .pill.tier-demon {
            border-color: rgba(248,113,113,0.9);
            color: #fecaca;
          }
          .footer {
            margin-top: 0.75rem;
            font-size: 0.75rem;
            color: #6b7280;
            display: flex;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 0.5rem;
          }
          .footer a {
            color: #9ca3af;
            text-decoration: none;
          }
          .footer a:hover { color: #e5e7eb; }
          @media (max-width: 768px) {
            header { padding: 0.9rem 1rem; }
            main { padding: 1rem; }
            thead { font-size: 0.78rem; }
            tbody td { padding: 0.45rem 0.5rem; }
          }
        </style>
      </head>
      <body>
        <header>
          <h1><span>Props Board</span> · Multi-Sport Proxy</h1>
          <nav>
            <a href="/">Board</a>
            <a href="/upload">Upload JSON</a>
            <a href="/props.json">Raw JSON</a>
            <a href="/model-board">Model List</a>
          </nav>
        </header>
        <main>
          <div class="summary">
            <div class="summary-card">
              <h2>Total Props</h2>
              <p id="summary-count">–</p>
              <span>Currently shown on this board</span>
            </div>
            <div class="summary-card">
              <h2>Unique Players</h2>
              <p id="summary-players">–</p>
              <span>Across all stats & sports</span>
            </div>
            <div class="summary-card">
              <h2>Sports</h2>
              <p id="summary-sports">–</p>
              <span>Distinct sports in this view</span>
            </div>
          </div>

          <div class="controls">
            <input id="search" type="text" placeholder="Search player, team, opponent…" />
            <select id="stat-filter">
              <option value="">All stats</option>
            </select>
            <select id="sport-filter">
              <option value="">All sports</option>
            </select>
            <button type="button" onclick="reloadProps()">Reload</button>
            <small id="status">Loading props…</small>
          </div>

          <div class="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Player</th>
                  <th>Team</th>
                  <th>Opponent</th>
                  <th>Stat</th>
                  <th>Line</th>
                  <th>Tier</th>
                  <th>Game Time</th>
                  <th>Sport</th>
                </tr>
              </thead>
              <tbody id="props-body">
                <tr><td colspan="8">Loading…</td></tr>
              </tbody>
            </table>
          </div>

          <div class="footer">
            <span id="footer-count"></span>
            <span>Upload new boards at <a href="/upload">/upload</a>.</span>
          </div>
        </main>

        <script>
          let allProps = [];

          function sportName(p) {
            return p.sport || p.league || "";
          }

          function formatTime(isoString) {
            if (!isoString) return "";
            try {
              const d = new Date(isoString);
              if (isNaN(d.getTime())) return isoString;
              return d.toLocaleString();
            } catch (e) {
              return isoString;
            }
          }

          function getTierRaw(p) {
            if (!p.tier) return "";
            return String(p.tier).toLowerCase();
          }

          function renderSummary(props) {
            const countEl = document.getElementById("summary-count");
            const playersEl = document.getElementById("summary-players");
            const sportsEl = document.getElementById("summary-sports");
            const footerCount = document.getElementById("footer-count");

            const total = props.length;
            const players = new Set(props.map(p => p.player || "").filter(Boolean));
            const sports = new Set(props.map(p => sportName(p)).filter(Boolean));

            countEl.textContent = total.toString();
            playersEl.textContent = players.size.toString();
            sportsEl.textContent = sports.size ? Array.from(sports).join(", ") : "–";
            footerCount.textContent = total ? `Showing ${total} props` : "No props loaded.";
          }

          function renderStatFilter(props) {
            const select = document.getElementById("stat-filter");
            const current = select.value;
            const stats = Array.from(new Set(props.map(p => p.stat || "").filter(Boolean))).sort();

            select.innerHTML = "";
            const all = document.createElement("option");
            all.value = "";
            all.textContent = "All stats";
            select.appendChild(all);

            for (const stat of stats) {
              const opt = document.createElement("option");
              opt.value = stat;
              opt.textContent = stat;
              select.appendChild(opt);
            }

            if (current) select.value = current;
          }

          function renderSportFilter(props) {
            const select = document.getElementById("sport-filter");
            const current = select.value;
            const sports = Array.from(new Set(props.map(p => sportName(p)).filter(Boolean))).sort();

            select.innerHTML = "";
            const all = document.createElement("option");
            all.value = "";
            all.textContent = "All sports";
            select.appendChild(all);

            for (const s of sports) {
              const opt = document.createElement("option");
              opt.value = s;
              opt.textContent = s;
              select.appendChild(opt);
            }

            if (current) select.value = current;
          }

          function renderTable(props) {
            const tbody = document.getElementById("props-body");
            tbody.innerHTML = "";

            if (!props.length) {
              const tr = document.createElement("tr");
              const td = document.createElement("td");
              td.colSpan = 8;
              td.textContent = "No props match the current filters.";
              tr.appendChild(td);
              tbody.appendChild(tr);
              return;
            }

            for (const p of props) {
              const tr = document.createElement("tr");

              const tdPlayer = document.createElement("td");
              tdPlayer.textContent = p.player || "";
              tr.appendChild(tdPlayer);

              const tdTeam = document.createElement("td");
              tdTeam.textContent = p.team || "";
              tr.appendChild(tdTeam);

              const tdOpp = document.createElement("td");
              tdOpp.textContent = p.opponent || "";
              tr.appendChild(tdOpp);

              const tdStat = document.createElement("td");
              const pillStat = document.createElement("span");
              pillStat.className = "pill stat";
              pillStat.textContent = p.stat || "";
              tdStat.appendChild(pillStat);
              tr.appendChild(tdStat);

              const tdLine = document.createElement("td");
              tdLine.textContent = p.line != null ? p.line : "";
              tr.appendChild(tdLine);

              const tdTier = document.createElement("td");
              const tierRaw = getTierRaw(p);
              if (tierRaw) {
                const pillTier = document.createElement("span");
                let cls = "pill tier-standard";
                let label = tierRaw;
                if (tierRaw === "goblin") {
                  cls = "pill tier-goblin";
                  label = "Goblin";
                } else if (tierRaw === "demon") {
                  cls = "pill tier-demon";
                  label = "Demon";
                } else if (tierRaw === "standard") {
                  cls = "pill tier-standard";
                  label = "Standard";
                }
                pillTier.className = cls;
                pillTier.textContent = label;
                tdTier.appendChild(pillTier);
              }
              tr.appendChild(tdTier);

              const tdTime = document.createElement("td");
              if (p.game_time) {
                const pillTime = document.createElement("span");
                pillTime.className = "pill time";
                pillTime.textContent = formatTime(p.game_time);
                tdTime.appendChild(pillTime);
              }
              tr.appendChild(tdTime);

              const tdSport = document.createElement("td");
              const pillLeague = document.createElement("span");
              pillLeague.className = "pill league";
              pillLeague.textContent = sportName(p);
              tdSport.appendChild(pillLeague);
              tr.appendChild(tdSport);

              tbody.appendChild(tr);
            }
          }

          function applyFilters() {
            const searchVal = document.getElementById("search").value.toLowerCase().trim();
            const statVal = document.getElementById("stat-filter").value;
            const sportVal = document.getElementById("sport-filter").value;
            let filtered = allProps.slice();

            if (searchVal) {
              filtered = filtered.filter(p => {
                return (
                  (p.player || "").toLowerCase().includes(searchVal) ||
                  (p.team || "").toLowerCase().includes(searchVal) ||
                  (p.opponent || "").toLowerCase().includes(searchVal)
                );
              });
            }

            if (statVal) {
              filtered = filtered.filter(p => p.stat === statVal);
            }

            if (sportVal) {
              filtered = filtered.filter(p => sportName(p) === sportVal);
            }

            renderSummary(filtered);
            renderTable(filtered);
          }

          async function reloadProps() {
            const status = document.getElementById("status");
            status.textContent = "Refreshing…";
            try {
              const res = await fetch("/props.json");
              const data = await res.json();
              if (!Array.isArray(data)) {
                status.textContent = "Unexpected data format from /props.json";
                return;
              }
              allProps = data;
              renderStatFilter(allProps);
              renderSportFilter(allProps);
              applyFilters();
              status.textContent = "Loaded " + allProps.length + " props.";
            } catch (e) {
              status.textContent = "Error loading props: " + e;
            }
          }

          document.addEventListener("DOMContentLoaded", () => {
            document.getElementById("search").addEventListener("input", applyFilters);
            document.getElementById("stat-filter").addEventListener("change", applyFilters);
            document.getElementById("sport-filter").addEventListener("change", applyFilters);
            reloadProps();
          });
        </script>
      </body>
    </html>
    """


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/props.json")
def props_json():
    """
    Raw JSON that the model (me) will read.
    """
    props = load_file_props()
    return JSONResponse(props)


@app.get("/model-board", response_class=PlainTextResponse)
def model_board():
    """
    Simple line-based view of all props, easy for the model to parse.

    Format:
    id | sport | league | player | team | opponent | stat | market | line | tier | game_time
    """
    props = load_file_props()
    lines = []
    header = "id | sport | league | player | team | opponent | stat | market | line | tier | game_time"
    lines.append(header)
    for p in props:
        line = " | ".join(
            [
                str(p.get("id", "")),
                str(p.get("sport", "")),
                str(p.get("league", "")),
                str(p.get("player", "")),
                str(p.get("team", "")),
                str(p.get("opponent", "")),
                str(p.get("stat", "")),
                str(p.get("market", "")),
                str(p.get("line", "")),
                str(p.get("tier", "")),
                str(p.get("game_time", "")),
            ]
        )
        lines.append(line)
    return "\n".join(lines)


# -------------------------------------------------------------------
# Upload page
# -------------------------------------------------------------------

@app.get("/upload", response_class=HTMLResponse)
def upload_page():
    return """
    <html>
      <head>
        <title>Upload PrizePicks JSON</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <style>
          :root { color-scheme: dark; }
          body {
            margin: 0;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: radial-gradient(circle at top, #020617, #020617);
            color: #e5e7eb;
          }
          main {
            max-width: 800px;
            margin: 0 auto;
            padding: 1.5rem 1.25rem 2rem;
          }
          h1 {
            font-size: 1.4rem;
            margin-bottom: 0.4rem;
          }
          p {
            font-size: 0.9rem;
            color: #9ca3af;
            margin-top: 0;
            margin-bottom: 0.8rem;
          }
          label {
            display: block;
            margin-bottom: 0.25rem;
            font-size: 0.85rem;
            color: #9ca3af;
          }
          select {
            width: 100%;
            margin-bottom: 0.8rem;
            padding: 0.55rem 0.7rem;
            border-radius: 0.75rem;
            border: 1px solid rgba(148,163,184,0.7);
            background-color: rgba(15,23,42,0.95);
            color: #e5e7eb;
            font-size: 0.9rem;
            outline: none;
          }
          textarea {
            width: 100%;
            height: 320px;
            border-radius: 0.75rem;
            border: 1px solid rgba(148,163,184,0.7);
            background-color: rgba(15,23,42,0.95);
            color: #e5e7eb;
            padding: 0.75rem;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 0.82rem;
            outline: none;
          }
          textarea::placeholder { color: #6b7280; }
          button {
            margin-top: 0.7rem;
            padding: 0.55rem 1.1rem;
            border-radius: 9999px;
            border: none;
            background: linear-gradient(to right, #38bdf8, #2563eb);
            color: white;
            font-size: 0.9rem;
            cursor: pointer;
            box-shadow: 0 15px 30px rgba(37,99,235,0.45);
          }
          button:hover { filter: brightness(1.07); }
          #status {
            margin-top: 0.7rem;
            font-size: 0.8rem;
            white-space: pre-wrap;
            color: #9ca3af;
          }
          a {
            color: #93c5fd;
            text-decoration: none;
          }
          a:hover { color: #bfdbfe; }
        </style>
      </head>
      <body>
        <main>
          <h1>Upload PrizePicks JSON</h1>
          <p>
            Choose a sport, then paste the raw JSON from the PrizePicks API
            (<code>{"data": [...], "included": [...]}</code>) and tap <strong>Upload</strong>.
            This will replace any existing props for that sport on the combined board.
          </p>
          <label for="sport">Sport</label>
          <select id="sport">
            <option value="">Select a sport…</option>
            <option value="nfl">NFL (league_id 9)</option>
            <option value="nba">NBA (league_id 7)</option>
            <option value="nhl">NHL (league_id 8)</option>
            <option value="cbb">CBB (league_id unknown / not checked)</option>
            <option value="cfb">CFB (league_id 15)</option>
            <option value="soccer">Soccer (league_id 82)</option>
            <option value="tennis">Tennis (league_id 5)</option>
            <option value="cs2">CS2 (league_id 265)</option>
          </select>

          <textarea id="raw" placeholder='{"data": [...], "included": [...]}'></textarea>
          <br />
          <button onclick="upload()">Upload</button>
          <div id="status"></div>

          <script>
            async function upload() {
              const status = document.getElementById('status');
              const txt = document.getElementById('raw').value;
              const sport = document.getElementById('sport').value;

              if (!sport) {
                status.textContent = "❌ Please select a sport.";
                return;
              }

              let raw;
              try {
                raw = JSON.parse(txt);
              } catch (e) {
                status.textContent = "❌ Invalid JSON: " + e;
                return;
              }

              status.textContent = "Uploading and processing…";
              try {
                const res = await fetch("/update-props", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ sport, raw })
                });
                const data = await res.json();
                if (!res.ok) {
                  status.textContent = "❌ Error: " + (data.detail || JSON.stringify(data));
                  return;
                }
                status.textContent =
                  "✅ Uploaded " + (data.count ?? 0) + " " + (data.sport || "") +
                  " props. Total on board: " + (data.total ?? "?") + ".";
              } catch (e) {
                status.textContent = "❌ Network error: " + e;
              }
            }

            // Clear textarea + status whenever sport changes
            document.addEventListener("DOMContentLoaded", () => {
              const sportSelect = document.getElementById('sport');
              const rawTextarea = document.getElementById('raw');
              const statusDiv = document.getElementById('status');
              if (sportSelect && rawTextarea) {
                sportSelect.addEventListener('change', () => {
                  rawTextarea.value = '';
                  if (statusDiv) statusDiv.textContent = '';
                });
              }
            });
          </script>
        </main>
      </body>
    </html>
    """


# -------------------------------------------------------------------
# Update props API
# -------------------------------------------------------------------

@app.post("/update-props")
async def update_props(request: Request):
    """
    Accept raw PrizePicks JSON from the client (your phone/browser),
    normalize it for the selected sport, and save to props.json,
    preserving other sports and updating a backup.
    """
    payload = await request.json()
    sport_key = payload.get("sport")
    raw = payload.get("raw")

    if not sport_key or sport_key not in SPORTS:
        raise HTTPException(status_code=400, detail="Invalid or missing 'sport' field.")

    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=400,
            detail="Field 'raw' must be an object containing the PrizePicks JSON (with 'data' and 'included').",
        )

    try:
        new_props = normalize_prizepicks(raw, sport_key)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    existing = load_file_props_raw_or_empty()
    sport_name = SPORTS[sport_key]["name"]

    # Remove old props for this sport
    remaining = [p for p in existing if p.get("sport") != sport_name]

    combined = remaining + new_props
    save_props(combined)

    return {
        "status": "ok",
        "sport": sport_name,
        "count": len(new_props),
        "total": len(combined),
    }
