from typing import List, Dict, Any
from pathlib import Path
import json

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

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
        "game_time": "2025-12-01T20:15:00-05:00",
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
        "game_time": "2025-12-01T20:15:00-05:00",
        "projection_type": "main",
    },
]

# ============================================================
# Helpers
# ============================================================


def normalize_prizepicks(raw: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Take raw PrizePicks-style JSON:
      {
        "data": [... projections ...],
        "included": [... players/games ...]
      }

    and convert it into a flat list of props for the model + UI.
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
                "home_team": attrs.get("home_team")
                or attrs.get("home_team_abbreviation"),
                "away_team": attrs.get("away_team")
                or attrs.get("away_team_abbreviation"),
                "start_time": attrs.get("start_time") or attrs.get("start_at"),
            }

    props: List[Dict[str, Any]] = []

    for proj in data:
        try:
            pid = proj.get("id")
            attrs = proj.get("attributes", {})
            rel = proj.get("relationships", {})

            player_rel = (
                (rel.get("new_player") or rel.get("player") or {}).get("data") or {}
            )
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

            props.append(
                {
                    "id": pid,
                    "source": "uploaded",
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
            )
        except Exception:
            # Skip malformed rows
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


# ============================================================
# Routes
# ============================================================


@app.get("/", response_class=HTMLResponse)
def board_view():
    """
    Main UI: nice board viewer that pulls from /props.json via JS.
    """
    return """
    <html>
      <head>
        <title>Props Board Viewer</title>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
          :root {
            color-scheme: dark;
          }
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
          header nav a:hover {
            color: #e5e7eb;
          }
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
          .controls input::placeholder {
            color: #6b7280;
          }
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
          .controls button:hover {
            filter: brightness(1.08);
          }
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
          tbody tr:nth-child(even) {
            background-color: rgba(15,23,42,0.96);
          }
          tbody tr:nth-child(odd) {
            background-color: rgba(17,24,39,0.96);
          }
          tbody td {
            padding: 0.5rem 0.75rem;
            border-bottom: 1px solid rgba(55, 65, 81, 0.8);
          }
          tbody tr:hover {
            background-color: rgba(30,64,175,0.3);
          }
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
          .footer a:hover {
            color: #e5e7eb;
          }
          @media (max-width: 768px) {
            header {
              padding: 0.9rem 1rem;
            }
            main {
              padding: 1rem;
            }
            thead {
              font-size: 0.78rem;
            }
            tbody td {
              padding: 0.45rem 0.5rem;
            }
          }
        </style>
      </head>
      <body>
        <header>
          <h1><span>Props Board</span> · PrizePicks Proxy</h1>
          <nav>
            <a href="/">Board</a>
            <a href="/upload">Upload JSON</a>
            <a href="/props.json">Raw JSON</a>
          </nav>
        </header>
        <main>
          <div class="summary">
            <div class="summary-card">
              <h2>Total Props</h2>
              <p id="summary-count">–</p>
              <span>Currently loaded on this board</span>
            </div>
            <div class="summary-card">
              <h2>Unique Players</h2>
              <p id="summary-players">–</p>
              <span>Across all stats & markets</span>
            </div>
            <div class="summary-card">
              <h2>Leagues</h2>
              <p id="summary-leagues">–</p>
              <span>Detected from the upload</span>
            </div>
          </div>

          <div class="controls">
            <input id="search" type="text" placeholder="Search player, team, opponent…" />
            <select id="stat-filter">
              <option value="">All stats</option>
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
                  <th>Game Time</th>
                  <th>League</th>
                </tr>
              </thead>
              <tbody id="props-body">
                <tr><td colspan="7">Loading…</td></tr>
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

          function renderSummary(props) {
            const countEl = document.getElementById("summary-count");
            const playersEl = document.getElementById("summary-players");
            const leaguesEl = document.getElementById("summary-leagues");
            const footerCount = document.getElementById("footer-count");

            const total = props.length;
            const players = new Set(props.map(p => p.player || "").filter(Boolean));
            const leagues = new Set(props.map(p => p.league || "").filter(Boolean));

            countEl.textContent = total.toString();
            playersEl.textContent = players.size.toString();
            leaguesEl.textContent = leagues.size ? Array.from(leagues).join(", ") : "–";
            footerCount.textContent = total ? `Showing ${total} props` : "No props loaded.";
          }

          function renderStatFilter(props) {
            const select = document.getElementById("stat-filter");
            const current = select.value;
            const stats = Array.from(new Set(props.map(p => p.stat || "").filter(Boolean))).sort();

            // Clear options
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

            // restore selected if possible
            if (current) {
              select.value = current;
            }
          }

          function renderTable(props) {
            const tbody = document.getElementById("props-body");
            tbody.innerHTML = "";

            if (!props.length) {
              const tr = document.createElement("tr");
              const td = document.createElement("td");
              td.colSpan = 7;
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
              const pill = document.createElement("span");
              pill.className = "pill stat";
              pill.textContent = p.stat || "";
              tdStat.appendChild(pill);
              tr.appendChild(tdStat);

              const tdLine = document.createElement("td");
              tdLine.textContent = p.line != null ? p.line : "";
              tr.appendChild(tdLine);

              const tdTime = document.createElement("td");
              if (p.game_time) {
                const pillTime = document.createElement("span");
                pillTime.className = "pill time";
                pillTime.textContent = formatTime(p.game_time);
                tdTime.appendChild(pillTime);
              }
              tr.appendChild(tdTime);

              const tdLeague = document.createElement("td");
              const pillLeague = document.createElement("span");
              pillLeague.className = "pill league";
              pillLeague.textContent = p.league || "";
              tdLeague.appendChild(pillLeague);
              tr.appendChild(tdLeague);

              tbody.appendChild(tr);
            }
          }

          function applyFilters() {
            const searchVal = document.getElementById("search").value.toLowerCase().trim();
            const statVal = document.getElementById("stat-filter").value;
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
              applyFilters();
              status.textContent = "Loaded " + allProps.length + " props.";
            } catch (e) {
              status.textContent = "Error loading props: " + e;
            }
          }

          document.addEventListener("DOMContentLoaded", () => {
            document.getElementById("search").addEventListener("input", applyFilters);
            document.getElementById("stat-filter").addEventListener("change", applyFilters);
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
    Machine-readable props endpoint for the model (and raw JSON if you want).
    """
    props = load_file_props()
    return JSONResponse(props)


@app.get("/upload", response_class=HTMLResponse)
def upload_page():
    """
    Simple HTML page with a textarea to paste raw PrizePicks JSON into
    from your phone or browser.
    """
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
          textarea {
            width: 100%;
            height: 340px;
            border-radius: 0.75rem;
            border: 1px solid rgba(148,163,184,0.7);
            background-color: rgba(15,23,42,0.95);
            color: #e5e7eb;
            padding: 0.75rem;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 0.82rem;
            outline: none;
          }
          textarea::placeholder {
            color: #6b7280;
          }
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
          button:hover {
            filter: brightness(1.07);
          }
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
          a:hover {
            color: #bfdbfe;
          }
        </style>
      </head>
      <body>
        <main>
          <h1>Upload PrizePicks JSON</h1>
          <p>
            Paste the raw JSON from the PrizePicks API
            (<code>{"data": [...], "included": [...]}</code>) and tap <strong>Upload</strong>.
            This will overwrite the current board used by <code>/props.json</code>.
          </p>
          <p>When you’re done, visit <a href="/">the Board view</a> to see the props nicely formatted.</p>
          <textarea id="raw" placeholder='{"data": [...], "included": [...]}'></textarea>
          <br />
          <button onclick="upload()">Upload</button>
          <div id="status"></div>

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

              status.textContent = "Uploading and processing…";
              try {
                const res = await fetch("/update-props", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify(raw)
                });
                const data = await res.json();
                if (!res.ok) {
                  status.textContent = "❌ Error: " + (data.detail || JSON.stringify(data));
                  return;
                }
                status.textContent = "✅ Uploaded " + (data.count ?? 0) + " props. Go back to the Board to view them.";
              } catch (e) {
                status.textContent = "❌ Network error: " + e;
              }
            }
          </script>
        </main>
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
