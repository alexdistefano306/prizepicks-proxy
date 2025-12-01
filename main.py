from typing import List, Dict, Any, Optional
from pathlib import Path
from datetime import datetime, timezone
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
    }
]

# Sport config: display name + expected league_id (None = no strict check)
SPORTS: Dict[str, Dict[str, Any]] = {
    "nfl": {"name": "NFL", "league_id": "9"},
    "nba": {"name": "NBA", "league_id": "7"},
    "nhl": {"name": "NHL", "league_id": "8"},
    "cbb": {"name": "CBB", "league_id": "20"},   # updated CBB league id
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


def load_file_props_raw_or_empty() -> List[Dict[str, Any]]:
    """
    Load props from disk without adding dummy props.
    Prefer main file, then backup. Return [] if nothing valid.
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


def _parse_game_time(value: Any) -> Optional[datetime]:
    """
    Best-effort parse of the game_time string into an aware datetime in UTC.
    Returns None if parsing fails or value is empty.
    """
    if not value:
        return None
    try:
        s = str(value).strip()
        # Handle trailing 'Z'
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        # If naive, assume UTC
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def get_current_props() -> List[Dict[str, Any]]:
    """
    Load props and drop any whose game_time is already in the past.
    Writes the cleaned list back to props.json / props_backup.json.
    If there are no persisted props, return DUMMY_PROPS for first-run debugging.
    """
    raw = load_file_props_raw_or_empty()
    now = datetime.now(timezone.utc)

    filtered: List[Dict[str, Any]] = []
    changed = False

    for p in raw:
        gt = _parse_game_time(p.get("game_time"))
        # If no game time or unparsable, keep it (safer than deleting)
        if gt is None:
            filtered.append(p)
            continue

        if gt >= now:
            filtered.append(p)
        else:
            # game has started/ended → drop it
            changed = True

    if changed:
        save_props(filtered)

    if not filtered and not DATA_FILE.exists() and not BACKUP_FILE.exists():
        return DUMMY_PROPS

    return filtered

# -------------------------------------------------------------------
# Normalization of PrizePicks JSON
# -------------------------------------------------------------------

def _extract_tier_from_attrs(attrs: Dict[str, Any]) -> str:
    """
    Map attributes['odds_Type'] (or variations) into "goblin" / "standard" / "demon".
    """
    raw = (
        attrs.get("odds_Type")
        or attrs.get("odds_type")
        or attrs.get("oddsType")
        or attrs.get("tier")
    )
    if not raw:
        return "standard"

    t = str(raw).strip().lower()

    if "goblin" in t:
        return "goblin"
    if "demon" in t:
        return "demon"
    if "standard" in t or "normal" in t:
        return "standard"

    return "standard"


def normalize_prizepicks(raw: Dict[str, Any], sport_key: str) -> List[Dict[str, Any]]:
    """
    Turn a raw PrizePicks JSON blob into a simple list of props.
    Enforces that the JSON's league_id matches the selected sport (when known).
    """
    if sport_key not in SPORTS:
        raise ValueError(f"Unknown sport key: {sport_key}")
    sport_cfg = SPORTS[sport_key]
    sport_name = sport_cfg["name"]
    expected_league_id = sport_cfg["league_id"]

    data = raw.get("data", []) or []
    included = raw.get("included", []) or []

    # --- Validate league_id where we know it ---
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

        if itype in ("new_player", "player"):
            players[iid] = {
                "name": attrs.get("name"),
                "team": attrs.get("team")
                or attrs.get("team_abbreviation")
                or "",
                "league": attrs.get("league") or sport_name,
            }
        elif itype == "team":
            teams[iid] = {
                "abbreviation": attrs.get("abbreviation") or "",
                "name": attrs.get("name") or "",
                "market": attrs.get("market") or "",
            }
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

            player_rel = (rel.get("new_player") or rel.get("player") or {}).get("data") or {}
            game_rel = (rel.get("game") or {}).get("data") or {}

            player_id = player_rel.get("id")
            game_id = game_rel.get("id")

            player_info = players.get(player_id, {})
            game_info = games.get(game_id, {})

            player = player_info.get("name") or "Unknown"
            team = player_info.get("team") or ""
            league = player_info.get("league") or sport_name

            home_team_abbr = None
            away_team_abbr = None
            if game_info:
                home_team_id = game_info.get("home_team_id")
                away_team_id = game_info.get("away_team_id")
                if home_team_id and home_team_id in teams:
                    home_team_abbr = teams[home_team_id]["abbreviation"]
                if away_team_id and away_team_id in teams:
                    away_team_abbr = teams[away_team_id]["abbreviation"]

            opponent = ""
            if team and home_team_abbr and away_team_abbr:
                if team == home_team_abbr:
                    opponent = away_team_abbr
                elif team == away_team_abbr:
                    opponent = home_team_abbr

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
            tier = _extract_tier_from_attrs(attrs)

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
            # Skip any malformed entry
            continue

    return props

# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------

@app.get("/health")
def health():
    return {"status": "ok"}

# ------------------ Main board (odds table) ------------------------

@app.get("/", response_class=HTMLResponse)
def board_view():
    """
    Main odds board UI. Data is fetched from /props.json (which uses get_current_props()).
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
            <a href="/export">Export</a>
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
            <span>Upload new boards at <a href="/upload">/upload</a>. Export for ChatGPT at <a href="/export">/export</a>.</span>
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

@app.get("/props.json")
def props_json():
    """
    Raw JSON for the live board, with expired props removed.
    """
    props = get_current_props()
    return JSONResponse(props)

# ------------------ Model board (CSV full + paged) ------------------------

@app.get("/model-board", response_class=PlainTextResponse)
def model_board():
    """
    CSV-style model board of all live props (all sports).

    Format (header + rows):
      sport,player,team,opponent,stat,line,tier,game_time
    """
    props = get_current_props()

    def clean(v: Any) -> str:
        return str(v).replace(",", " ").replace("\n", " ").strip()

    props_sorted = sorted(
        props,
        key=lambda p: (
            (p.get("sport") or ""),
            (p.get("game_time") or ""),
            (p.get("player") or ""),
        ),
    )

    lines: List[str] = []
    header = "sport,player,team,opponent,stat,line,tier,game_time"
    lines.append(header)

    for p in props_sorted:
        line = ",".join(
            [
                clean(p.get("sport", "")),
                clean(p.get("player", "")),
                clean(p.get("team", "")),
                clean(p.get("opponent", "")),
                clean(p.get("stat", "")),
                str(p.get("line", "")),
                clean(p.get("tier", "")),
                clean(p.get("game_time", "")),
            ]
        )
        lines.append(line)

    return "\n".join(lines)


@app.get("/model-board/{sport}/{tiers}/page/{page}", response_class=PlainTextResponse)
def model_board_paged_tiered(sport: str, tiers: str, page: int, page_size: int = 200):
    """
    Paged CSV-style model board, filtered by sport AND tier(s).

    Examples:
      /model-board/nba/standard/page/1
      /model-board/nba/goblin/page/1
      /model-board/nba/demon/page/1
      /model-board/nba/standard+goblin/page/1
      /model-board/nba/goblin+demon/page/1
      /model-board/nba/standard+demon/page/1
      /model-board/all/goblin/page/1
    """
    sport_key = sport.lower()

    if sport_key != "all" and sport_key not in SPORTS:
        raise HTTPException(status_code=404, detail="Unknown sport key")

    # --- Parse tiers like "standard", "goblin", "demon", "standard+goblin", etc. ---
    allowed_tiers = {"standard", "goblin", "demon"}
    parts = [t.strip().lower() for t in tiers.split("+") if t.strip()]
    tier_set = set()

    for t in parts:
        if t not in allowed_tiers:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid tier '{t}'. Allowed: standard, goblin, demon, or combos like standard+goblin.",
            )
        tier_set.add(t)

    if not tier_set:
        raise HTTPException(status_code=400, detail="No valid tiers provided.")

    all_props = get_current_props()

    # --- Filter by sport (or 'all') ---
    if sport_key == "all":
        filtered = all_props
    else:
        sport_name = SPORTS[sport_key]["name"]
        filtered = [
            p for p in all_props
            if (p.get("sport") or "").lower() == sport_name.lower()
        ]

    # --- Filter by tier(s) ---
    filtered = [
        p
        for p in filtered
        if str(p.get("tier", "")).lower() in tier_set
    ]

    # Stable sort: sport (for 'all'), then game_time, then player
    filtered.sort(
        key=lambda p: (
            (p.get("sport") or ""),
            (p.get("game_time") or ""),
            (p.get("player") or ""),
        )
    )

    total = len(filtered)
    if total == 0:
        # Still return the header so format is consistent
        return PlainTextResponse(
            "sport,player,team,opponent,stat,line,tier,game_time\n"
        )

    if page < 1:
        raise HTTPException(status_code=400, detail="Page must be >= 1")

    total_pages = (total + page_size - 1) // page_size
    if page > total_pages:
        raise HTTPException(
            status_code=404,
            detail=f"Page {page} out of range (total_pages={total_pages})",
        )

    start = (page - 1) * page_size
    end = start + page_size
    page_props = filtered[start:end]

    def clean(v: Any) -> str:
        return str(v).replace(",", " ").replace("\n", " ").strip()

    lines: List[str] = []
    header = "sport,player,team,opponent,stat,line,tier,game_time"
    lines.append(header)

    for p in page_props:
        line = ",".join(
            [
                clean(p.get("sport", "")),
                clean(p.get("player", "")),
                clean(p.get("team", "")),
                clean(p.get("opponent", "")),
                clean(p.get("stat", "")),
                str(p.get("line", "")),
                clean(p.get("tier", "")),
                clean(p.get("game_time", "")),
            ]
        )
        lines.append(line)

    return PlainTextResponse("\n".join(lines))


# ------------------ Upload page ------------------------

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
            <option value="cbb">CBB (league_id 20)</option>
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

@app.post("/update-props")
async def update_props(request: Request):
    """
    Accept raw PrizePicks JSON from the client, normalize it for the selected sport,
    and save to props.json, preserving other sports and updating a backup.
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

    total_live = len(get_current_props())
    return {
        "status": "ok",
        "sport": sport_name,
        "count": len(new_props),
        "total": total_live,
    }

# ------------------ Export page (multi-sport, mobile-friendly) ------------------------

@app.get("/export", response_class=HTMLResponse)
def export_page():
    """
    Export UI: pill-style multi-select sports, tier filters, and max size.
    """
    # Build sport checkbox HTML from SPORTS config
    sport_labels = []
    for key, cfg in SPORTS.items():
        name = cfg["name"]
        # default to checked so you get everything unless you uncheck
        sport_labels.append(
            f'<label><input type="checkbox" class="sport-checkbox" value="{key}" checked /> {name} ({key})</label>'
        )
    sports_html = "\n".join(sport_labels)

    return f"""
    <html>
      <head>
        <title>Export Props for ChatGPT</title>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
          :root {{ color-scheme: dark; }}
          body {{
            margin: 0;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #020617;
            color: #e5e7eb;
          }}
          main {{
            max-width: 900px;
            margin: 0 auto;
            padding: 1.5rem 1.25rem 2rem;
          }}
          h1 {{
            font-size: 1.3rem;
            margin-bottom: 0.5rem;
          }}
          p {{
            font-size: 0.9rem;
            color: #9ca3af;
            margin-top: 0;
            margin-bottom: 0.7rem;
          }}
          label {{
            display: block;
            margin-bottom: 0.25rem;
            font-size: 0.85rem;
            color: #9ca3af;
          }}
          .row {{
            margin-bottom: 0.9rem;
          }}
          .pill-group {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.6rem;
            font-size: 0.85rem;
            color: #e5e7eb;
          }}
          .pill-group label {{
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            padding: 0.3rem 0.7rem;
            border-radius: 9999px;
            border: 1px solid rgba(148,163,184,0.7);
            background-color: rgba(15,23,42,0.95);
            margin: 0;
          }}
          .pill-group input[type="checkbox"] {{
            accent-color: #22c55e;
          }}
          input[type="number"] {{
            width: 120px;
            padding: 0.4rem 0.6rem;
            border-radius: 0.75rem;
            border: 1px solid rgba(148,163,184,0.7);
            background-color: rgba(15,23,42,0.95);
            color: #e5e7eb;
            font-size: 0.9rem;
            outline: none;
          }}
          button {{
            margin-top: 0.7rem;
            padding: 0.55rem 1.1rem;
            border-radius: 9999px;
            border: none;
            background: linear-gradient(to right, #22c55e, #16a34a);
            color: white;
            font-size: 0.9rem;
            cursor: pointer;
            box-shadow: 0 12px 25px rgba(16,185,129,0.4);
          }}
          button:hover {{ filter: brightness(1.07); }}
          #status {{
            margin-top: 0.6rem;
            font-size: 0.8rem;
            color: #9ca3af;
            white-space: pre-wrap;
          }}
          textarea {{
            width: 100%;
            height: 60vh;
            margin-top: 1rem;
            border-radius: 0.75rem;
            border: 1px solid rgba(148,163,184,0.7);
            background-color: #030712;
            color: #e5e7eb;
            padding: 0.75rem;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 0.8rem;
          }}
        </style>
      </head>
      <body>
        <main>
          <h1>Export Props for ChatGPT</h1>
          <p>
            Tap the sports and tiers you want, optionally set a max number of props,
            then tap <strong>Generate Export</strong>. Expired props are removed using
            <code>game_time</code>. Format per line:
            <code>sport,player,team,opponent,stat,line,tier,game_time</code>.
          </p>

          <div class="row">
            <label>Sports</label>
            <div class="pill-group" id="sports-group">
              {sports_html}
            </div>
          </div>

          <div class="row">
            <label>Tiers</label>
            <div class="pill-group">
              <label><input type="checkbox" id="tier-goblin" value="goblin" checked /> Goblin</label>
              <label><input type="checkbox" id="tier-standard" value="standard" checked /> Standard</label>
              <label><input type="checkbox" id="tier-demon" value="demon" checked /> Demon</label>
            </div>
          </div>

          <div class="row">
            <label for="max">Max props (optional, default 300)</label>
            <input id="max" type="number" min="1" max="5000" placeholder="300" />
          </div>

          <button type="button" onclick="generateExport()">Generate Export</button>
          <div id="status"></div>
          <textarea id="exportBox" readonly placeholder="Your export will appear here…"></textarea>

          <script>
            async function generateExport() {{
              const status = document.getElementById("status");
              const box = document.getElementById("exportBox");

              const selectedSports = Array.from(
                document.querySelectorAll(".sport-checkbox")
              )
                .filter(cb => cb.checked)
                .map(cb => cb.value);

              if (!selectedSports.length) {{
                status.textContent = "❌ Please select at least one sport.";
                box.value = "";
                return;
              }}

              const tiers = [];
              if (document.getElementById("tier-goblin").checked) tiers.push("goblin");
              if (document.getElementById("tier-standard").checked) tiers.push("standard");
              if (document.getElementById("tier-demon").checked) tiers.push("demon");

              const maxInput = document.getElementById("max").value.trim();
              const maxVal = maxInput ? parseInt(maxInput, 10) : 300;

              status.textContent = "Building export…";
              box.value = "";

              try {{
                const res = await fetch("/export-data", {{
                  method: "POST",
                  headers: {{ "Content-Type": "application/json" }},
                  body: JSON.stringify({{
                    sports: selectedSports,
                    tiers: tiers,
                    max: maxVal
                  }})
                }});
                const data = await res.json();
                if (!res.ok) {{
                  status.textContent = "❌ Error: " + (data.detail || JSON.stringify(data));
                  return;
                }}
                box.value = data.text || "";
                status.textContent = "✅ Export ready (" + (data.count ?? 0) + " props). Long-press / Ctrl+A to copy.";
              }} catch (e) {{
                status.textContent = "❌ Network error: " + e;
              }}
            }}
          </script>
        </main>
      </body>
    </html>
    """

@app.post("/export-data")
async def export_data(request: Request):
    """
    Backend for /export. Accepts:
      {
        "sports": ["nfl", "nba", ...],
        "tiers": ["goblin", "standard", "demon"],
        "max": 300
      }

    Returns compact text:
      sport,player,team,opponent,stat,line,tier,game_time
    """
    payload = await request.json()
    sports = payload.get("sports")
    tiers = payload.get("tiers") or []
    max_props = payload.get("max") or 300

    if not isinstance(sports, list) or not sports:
        raise HTTPException(status_code=400, detail="Field 'sports' must be a non-empty list.")

    requested_keys = {str(s).lower() for s in sports}
    valid_keys = {k for k in SPORTS.keys() if k in requested_keys}
    if not valid_keys:
        raise HTTPException(status_code=400, detail="No valid sports keys provided.")

    tier_set = {str(t).lower() for t in tiers if t}
    if not tier_set:
        tier_set = {"goblin", "standard", "demon"}

    try:
        max_props = int(max_props)
    except Exception:
        max_props = 300
    if max_props <= 0:
        max_props = 300

    selected_sport_names = {SPORTS[k]["name"] for k in valid_keys}
    selected_sport_names_lower = {name.lower() for name in selected_sport_names}

    all_props = get_current_props()

    def clean(v: Any) -> str:
        return str(v).replace(",", " ").replace("\n", " ").strip()

    filtered: List[Dict[str, Any]] = []
    for p in all_props:
        sname = (p.get("sport") or "").lower()
        if sname not in selected_sport_names_lower:
            continue
        tier_raw = str(p.get("tier", "")).lower()
        if tier_raw not in tier_set:
            continue
        filtered.append(p)

    # Sort and cap
    filtered.sort(
        key=lambda p: (
            (p.get("sport") or ""),
            (p.get("game_time") or ""),
            (p.get("player") or ""),
        )
    )

    if len(filtered) > max_props:
        filtered = filtered[:max_props]

    lines: List[str] = []
    header = "sport,player,team,opponent,stat,line,tier,game_time"
    lines.append(header)

    for p in filtered:
        line = ",".join(
            [
                clean(p.get("sport", "")),
                clean(p.get("player", "")),
                clean(p.get("team", "")),
                clean(p.get("opponent", "")),
                clean(p.get("stat", "")),
                str(p.get("line", "")),
                clean(p.get("tier", "")),
                clean(p.get("game_time", "")),
            ]
        )
        lines.append(line)

    text = "\n".join(lines)
    return {"text": text, "count": len(filtered)}
