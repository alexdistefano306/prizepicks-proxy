from typing import List, Dict, Any, Optional
from pathlib import Path
from datetime import datetime, timezone
import json
import re

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

app = FastAPI(title="PrizePicks/Underdog Props Proxy – Multi-Sport Board")

# -------------------------------------------------------------------
# Files / paths
# -------------------------------------------------------------------

BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "props.json"
BACKUP_FILE = BASE_DIR / "props_backup.json"

# -------------------------------------------------------------------
# Config / constants
# -------------------------------------------------------------------

SPORTS: Dict[str, Dict[str, Any]] = {
    "nfl": {"name": "NFL", "league_id": "9"},
    "nba": {"name": "NBA", "league_id": "7"},
    "nhl": {"name": "NHL", "league_id": "8"},
    "cbb": {"name": "CBB", "league_id": "20"},
    "cfb": {"name": "CFB", "league_id": "15"},
    "soccer": {"name": "Soccer", "league_id": "82"},
    "tennis": {"name": "Tennis", "league_id": "5"},
    "cs2": {"name": "CS2", "league_id": "265"},
}

# Underdog uses sport_id like "NBA", "CBB", "FIFA", "CS", etc.
UNDERDOG_SPORT_IDS: Dict[str, str] = {
    "nfl": "NFL",
    "nba": "NBA",
    "nhl": "NHL",
    "cbb": "CBB",
    "cfb": "CFB",
    "soccer": "FIFA",   # soccer -> "FIFA"
    "tennis": "TENNIS",
    "cs2": "CS",        # cs2 -> "CS"
}

ALLOWED_TIERS = {"standard", "goblin", "demon"}

# -------------------------------------------------------------------
# Helpers: sport slugs / keys
# -------------------------------------------------------------------


def sport_slug_from_label(label: str) -> str:
    """
    Turn a display name ("League of Legends") into a slug ("league-of-legends").
    Used for extras / dynamic sports.
    """
    s = (label or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s or "unknown"


def get_prop_sport_slug(p: Dict[str, Any]) -> str:
    """
    Return the "sport_slug" for a prop, falling back to:
      - static SPORTS mapping if sport name matches
      - slugified sport/league label otherwise
    """
    slug = str(p.get("sport_slug") or "").strip().lower()
    if slug:
        return slug

    label = (p.get("sport") or p.get("league") or "").strip()
    if not label:
        return ""
    lower_label = label.lower()

    for key, cfg in SPORTS.items():
        if cfg["name"].lower() == lower_label:
            return key

    return sport_slug_from_label(label)

# -------------------------------------------------------------------
# Helpers: load / save props
# -------------------------------------------------------------------


def save_props(props: List[Dict[str, Any]]) -> None:
    """
    Save props to props.json and keep a backup copy.
    """
    text = json.dumps(props, indent=2, ensure_ascii=False)
    DATA_FILE.write_text(text, encoding="utf-8")
    BACKUP_FILE.write_text(text, encoding="utf-8")


def load_file_props_raw_or_empty() -> List[Dict[str, Any]]:
    """
    Load props from disk without adding dummy values.
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
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def get_current_props() -> List[Dict[str, Any]]:
    """
    Load props and drop any whose game_time is already in the past.
    Writes the cleaned list back to disk when it changes.
    No dummy fallback: returns [] if nothing is stored.
    """
    raw = load_file_props_raw_or_empty()
    if not raw:
        return []

    now = datetime.now(timezone.utc)
    filtered: List[Dict[str, Any]] = []
    changed = False

    for p in raw:
        gt = _parse_game_time(p.get("game_time"))
        if gt is None:
            # If no time or can't parse, keep it rather than silently delete.
            filtered.append(p)
            continue
        if gt >= now:
            filtered.append(p)
        else:
            changed = True

    if changed:
        save_props(filtered)

    return filtered

# -------------------------------------------------------------------
# Normalization helpers
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


def normalize_prizepicks(
    raw: Dict[str, Any],
    sport_key: str,
    sport_label: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Turn a raw PrizePicks JSON blob into a simple list of props.

    - For known sports (nfl/nba/...): enforce league_id.
    - For 'extras': skip league_id check and use the custom sport_label.
    """
    sport_key = (sport_key or "").lower()

    if sport_key == "extras":
        sport_name = (sport_label or "Extras").strip() or "Extras"
        expected_league_id = None
        sport_slug = sport_slug_from_label(sport_name)
    else:
        if sport_key not in SPORTS:
            raise ValueError(f"Unknown sport key: {sport_key}")
        sport_cfg = SPORTS[sport_key]
        sport_name = sport_cfg["name"]
        expected_league_id = sport_cfg["league_id"]
        sport_slug = sport_key

    data = raw.get("data", []) or []
    included = raw.get("included", []) or []

    # Validate league_id when possible
    league_ids = set()
    for proj in data:
        attrs = proj.get("attributes", {}) or {}
        rel_league = (proj.get("relationships", {}).get("league") or {}).get("data") or {}
        lid_rel = rel_league.get("id")
        if lid_rel is not None:
            league_ids.add(str(lid_rel))
        lid_attr = attrs.get("league_id")
        if lid_attr is not None:
            league_ids.add(str(lid_attr))

    if expected_league_id and league_ids and league_ids != {expected_league_id}:
        raise ValueError(
            f"League mismatch: selected {sport_name} (league_id {expected_league_id}), "
            f"but JSON contained league ids {sorted(league_ids)}"
        )

    # Build helper maps from "included"
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
            line_val = attrs.get("line_score")
            if line_val is not None:
                try:
                    line_val = float(line_val)
                except Exception:
                    line_val = None

            start_time = (
                game_info.get("start_time")
                or attrs.get("start_time")
                or attrs.get("start_at")
            )

            tier = _extract_tier_from_attrs(attrs)

            if not player or line_val is None or not stat:
                continue

            props.append(
                {
                    "id": pid,
                    "source": "prizepicks",
                    "board": sport_name,
                    "league": league,
                    "sport": sport_name,
                    "sport_slug": sport_slug,
                    "player": player,
                    "team": team,
                    "opponent": opponent,
                    "stat": stat,
                    "market": str(stat).lower().replace(" ", "_"),
                    "line": line_val,
                    "game_time": start_time,
                    "projection_type": "main",
                    "tier": tier,
                    "ud_american_over": None,
                    "ud_american_under": None,
                }
            )
        except Exception:
            # Skip malformed entries rather than killing the whole upload
            continue

    return props


def normalize_underdog(
    raw: Dict[str, Any],
    sport_key: str,
    sport_label: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Normalize Underdog JSON into the same internal prop format.

    - For known sports, checks games[].sport_id against UNDERDOG_SPORT_IDS.
    - For 'extras', uses custom sport_label and skips sport_id check.
    """
    sport_key = (sport_key or "").lower()

    if sport_key == "extras":
        if not sport_label:
            raise ValueError(
                "When uploading as 'extras', you must provide a 'sport_label' (e.g. 'Badminton')."
            )
        sport_name = sport_label.strip()
        if not sport_name:
            raise ValueError("Custom sport label cannot be empty.")
        expected_sport_id = None
        sport_slug = sport_slug_from_label(sport_name)
    else:
        if sport_key not in SPORTS:
            raise ValueError(f"Unknown sport key: {sport_key}")
        sport_name = SPORTS[sport_key]["name"]
        expected_sport_id = UNDERDOG_SPORT_IDS.get(sport_key)
        sport_slug = sport_key

    games = {g.get("id"): g for g in (raw.get("games") or []) if g.get("id") is not None}
    appearances = {
        a.get("id"): a for a in (raw.get("appearances") or []) if a.get("id") is not None
    }

    # Validate sport_id when possible
    if expected_sport_id:
        sport_ids = set()
        for g in games.values():
            sid = g.get("sport_id")
            if sid:
                sport_ids.add(str(sid).upper())
        if sport_ids and sport_ids != {expected_sport_id}:
            raise ValueError(
                f"Sport mismatch: selected {sport_name} (Underdog sport_id {expected_sport_id}), "
                f"but JSON contained sport ids {sorted(sport_ids)}"
            )

    props: List[Dict[str, Any]] = []
    lines = raw.get("over_under_lines") or []

    for line in lines:
        try:
            status = (line.get("status") or "").lower()
            if status and status != "active":
                continue

            over_under = line.get("over_under") or {}
            app_stat = over_under.get("appearance_stat") or {}

            stat_display = app_stat.get("display_stat") or ""
            stat_code = app_stat.get("stat") or ""
            if not stat_display and not stat_code:
                continue

            stat_value = line.get("stat_value")
            try:
                line_val = float(stat_value)
            except Exception:
                continue

            appearance_id = app_stat.get("appearance_id")
            appearance = appearances.get(appearance_id, {})
            match_id = appearance.get("match_id")
            game = games.get(match_id, {})

            game_time = game.get("scheduled_at") or game.get("starts_at")
            matchup = (
                game.get("short_title")
                or game.get("abbreviated_title")
                or game.get("title")
                or ""
            )

            options = line.get("options") or []
            player_name = ""
            over_price: Optional[str] = None
            under_price: Optional[str] = None

            for opt in options:
                choice = (opt.get("choice") or "").lower()
                header = opt.get("selection_header") or ""
                if not player_name and header:
                    player_name = header
                american = opt.get("american_price")
                if choice == "higher":
                    over_price = american
                elif choice == "lower":
                    under_price = american

            if not player_name:
                title = over_under.get("title") or ""
                if " O/U" in title:
                    base = title.split(" O/U", 1)[0].strip()
                    player_name = base.rsplit(" ", 1)[0] or base

            if not player_name:
                continue

            market = stat_code or stat_display.replace(" ", "_").lower() or "unknown"

            props.append(
                {
                    "id": line.get("id"),
                    "source": "underdog",
                    "board": sport_name,
                    "league": sport_name,
                    "sport": sport_name,
                    "sport_slug": sport_slug,
                    "player": player_name,
                    "team": "",
                    "opponent": matchup,
                    "stat": stat_display or stat_code,
                    "market": market,
                    "line": line_val,
                    "game_time": game_time,
                    "projection_type": "main",
                    "tier": "standard",
                    "ud_american_over": over_price,
                    "ud_american_under": under_price,
                }
            )
        except Exception:
            continue

    return props

# -------------------------------------------------------------------
# CSV helpers
# -------------------------------------------------------------------


def _clean_csv_val(v: Any) -> str:
    """Make sure CSV values don't break the row."""
    return str(v).replace(",", " ").replace("\n", " ").strip()


def _model_csv_val(v: Any) -> str:
    """
    Ultra-compact value for model-board CSV pages:
    - remove commas/newlines
    - collapse spaces into underscores so each row has no whitespace
    """
    s = str(v).replace(",", " ").replace("\n", " ").strip()
    parts = s.split()
    return "_".join(parts)

# -------------------------------------------------------------------
# Health
# -------------------------------------------------------------------


@app.get("/health")
def health():
    return {"status": "ok"}

# -------------------------------------------------------------------
# Main odds board UI
# -------------------------------------------------------------------


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
          body {
            margin: 0;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #020617;
            color: #e5e7eb;
          }
          header {
            padding: 1rem 1.25rem;
            border-bottom: 1px solid #1f2937;
            display: flex;
            flex-wrap: wrap;
            justify-content: space-between;
            align-items: center;
            gap: 0.5rem;
            background: #020617;
            position: sticky;
            top: 0;
            z-index: 10;
          }
          header h1 {
            font-size: 1.1rem;
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
            padding: 1rem 1.25rem 1.5rem;
            max-width: 1100px;
            margin: 0 auto;
          }
          .controls {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            margin-bottom: 0.75rem;
            align-items: center;
          }
          .controls input, .controls select {
            padding: 0.45rem 0.65rem;
            border-radius: 9999px;
            border: 1px solid #4b5563;
            background-color: #020617;
            color: #e5e7eb;
            font-size: 0.85rem;
            outline: none;
          }
          .controls input::placeholder { color: #6b7280; }
          .controls button {
            padding: 0.45rem 0.9rem;
            border-radius: 9999px;
            border: none;
            font-size: 0.85rem;
            cursor: pointer;
            background: #22c55e;
            color: white;
          }
          .controls small {
            font-size: 0.75rem;
            color: #9ca3af;
          }
          .table-wrapper {
            border-radius: 0.75rem;
            border: 1px solid #1f2937;
            overflow: hidden;
            background: #020617;
          }
          table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.8rem;
          }
          thead {
            background: #111827;
          }
          thead th {
            text-align: left;
            padding: 0.5rem 0.6rem;
            white-space: nowrap;
            font-weight: 500;
            color: #e5e7eb;
            border-bottom: 1px solid #1f2937;
          }
          tbody tr:nth-child(even) { background-color: #020617; }
          tbody tr:nth-child(odd) { background-color: #020617; }
          tbody td {
            padding: 0.45rem 0.6rem;
            border-bottom: 1px solid #111827;
          }
          tbody tr:hover { background-color: #111827; }
          .pill {
            display: inline-flex;
            align-items: center;
            padding: 0.15rem 0.5rem;
            border-radius: 9999px;
            font-size: 0.7rem;
            border: 1px solid #4b5563;
            color: #e5e7eb;
          }
          .pill.tier-goblin {
            border-color: #fbbf24;
            color: #fbbf24;
          }
          .pill.tier-demon {
            border-color: #f97373;
            color: #fecaca;
          }
          .pill.time {
            border-color: #facc15;
            color: #facc15;
          }
          .pill.league {
            border-color: #22c55e;
            color: #bbf7d0;
          }
        </style>
      </head>
      <body>
        <header>
          <h1><span>Props Board</span> · Multi-Sport Proxy</h1>
          <nav>
            <a href="/">Board</a>
            <a href="/upload">Upload</a>
            <a href="/export">Export</a>
            <a href="/model-index-main">Model Index</a>
          </nav>
        </header>
        <main>
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
                  <th>Opponent / Matchup</th>
                  <th>Stat</th>
                  <th>Line</th>
                  <th>UD Odds (O/U)</th>
                  <th>Tier</th>
                  <th>Game Time</th>
                  <th>Sport</th>
                </tr>
              </thead>
              <tbody id="props-body">
                <tr><td colspan="9">Loading…</td></tr>
              </tbody>
            </table>
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
              td.colSpan = 9;
              td.textContent = "No props match the current filters or nothing has been uploaded yet.";
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
              tdStat.textContent = p.stat || "";
              tr.appendChild(tdStat);

              const tdLine = document.createElement("td");
              tdLine.textContent = p.line != null ? p.line : "";
              tr.appendChild(tdLine);

              const tdOdds = document.createElement("td");
              const over = p.ud_american_over || "";
              const under = p.ud_american_under || "";
              if (over || under) {
                tdOdds.textContent = (over || "?") + " / " + (under || "?");
              } else {
                tdOdds.textContent = "";
              }
              tr.appendChild(tdOdds);

              const tdTier = document.createElement("td");
              const tierRaw = getTierRaw(p);
              if (tierRaw) {
                const pillTier = document.createElement("span");
                let cls = "pill";
                let label = tierRaw;
                if (tierRaw === "goblin") {
                  cls += " tier-goblin";
                  label = "Goblin";
                } else if (tierRaw === "demon") {
                  cls += " tier-demon";
                  label = "Demon";
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

# -------------------------------------------------------------------
# Raw props JSON (for UI & scripts)
# -------------------------------------------------------------------


@app.get("/props.json")
def props_json():
    """
    Raw JSON for the live board, with expired props removed.
    """
    props = get_current_props()
    return JSONResponse(props)

# -------------------------------------------------------------------
# Model-board filtering helpers
# -------------------------------------------------------------------


def _filter_props_for_board(
    sport: str,
    tiers_str: Optional[str],
) -> List[Dict[str, Any]]:
    """
    Common filtering for model-board CSV and HTML views.
    sport: slug (e.g. "nba", "badminton") or "all"/"" for all sports.
    tiers_str: "standard+goblin", "demon", etc. or None for all tiers.
    """
    sport = (sport or "").lower()

    tier_set: Optional[set] = None
    if tiers_str:
        parts = [t.strip().lower() for t in tiers_str.split("+") if t.strip()]
        tmp = set()
        for t in parts:
            if t not in ALLOWED_TIERS:
                raise HTTPException(status_code=400, detail=f"Invalid tier '{t}'")
            tmp.add(t)
        if not tmp:
            raise HTTPException(status_code=400, detail="No valid tiers")
        tier_set = tmp

    all_props = get_current_props()

    if sport in ("", "all"):
        filtered = all_props
    else:
        filtered = [p for p in all_props if get_prop_sport_slug(p) == sport]

    if tier_set is not None:
        filtered = [
            p for p in filtered
            if str(p.get("tier", "")).lower() in tier_set
        ]

    filtered.sort(
        key=lambda p: (
            (p.get("sport") or ""),
            (p.get("game_time") or ""),
            (p.get("player") or ""),
        )
    )
    return filtered


def _build_model_page_text(
    sport_key: str,
    tiers_str: Optional[str],
    page: int,
    page_size: int,
) -> str:
    """
    Build a single CSV page for the model-board endpoints.
    """
    if page < 1:
        raise HTTPException(status_code=400, detail="Page must be >= 1")

    filtered = _filter_props_for_board(sport_key, tiers_str)
    total = len(filtered)
    if total == 0:
        return "sport,player,team,opponent,stat,line,tier,game_time\n"

    total_pages = (total + page_size - 1) // page_size
    if page > total_pages:
        raise HTTPException(
            status_code=404,
            detail=f"Page {page} out of range (total_pages={total_pages})"
        )

    start = (page - 1) * page_size
    end = start + page_size
    page_props = filtered[start:end]

    lines: List[str] = []
    header = "sport,player,team,opponent,stat,line,tier,game_time"
    lines.append(header)

    for p in page_props:
        line = ",".join(
            [
                _model_csv_val(p.get("sport", "")),
                _model_csv_val(p.get("player", "")),
                _model_csv_val(p.get("team", "")),
                _model_csv_val(p.get("opponent", "")),
                _model_csv_val(p.get("stat", "")),
                str(p.get("line", "")),
                _model_csv_val(p.get("tier", "")),
                _model_csv_val(p.get("game_time", "")),
            ]
        )
        lines.append(line)

    return "\n".join(lines)

# -------------------------------------------------------------------
# Model-board CSV endpoints (for your scripts, not for ChatGPT)
# -------------------------------------------------------------------


@app.get("/model-board", response_class=PlainTextResponse)
def model_board():
    """
    Full board as CSV in one page (debugging/scripts).
    For the model, use /model-index-main -> /model-index -> /model-board-view.
    """
    text = _build_model_page_text("all", None, page=1, page_size=100000)
    return PlainTextResponse(text)


@app.get("/model-board/{sport}/page/{page}", response_class=PlainTextResponse)
def model_board_paged(
    sport: str,
    page: int,
    page_size: int = 150,
    tiers: str = "",
):
    """
    Paged CSV board for a single sport (slug).
    """
    text = _build_model_page_text(
        sport_key=sport,
        tiers_str=tiers or None,
        page=page,
        page_size=page_size,
    )
    return PlainTextResponse(text)

# -------------------------------------------------------------------
# Model-board HTML view (for ChatGPT)
# -------------------------------------------------------------------


@app.get("/model-board-view/{sport}/page/{page}", response_class=HTMLResponse)
def model_board_view_html(
    sport: str,
    page: int,
    page_size: int = 150,
    tiers: str = "",
):
    """
    HTML view of a slice of the board, used by ChatGPT.
    """
    sport_slug = (sport or "").lower()
    filtered = _filter_props_for_board(sport_slug, tiers or None)

    if page < 1:
        raise HTTPException(status_code=400, detail="Page must be >= 1")

    total = len(filtered)
    PAGE_SIZE = page_size
    if total == 0:
        page_props: List[Dict[str, Any]] = []
        total_pages = 1
    else:
        total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
        if page > total_pages:
            raise HTTPException(
                status_code=404,
                detail=f"Page {page} out of range (total_pages={total_pages})",
            )
        start = (page - 1) * PAGE_SIZE
        end = start + PAGE_SIZE
        page_props = filtered[start:end]

    label = ""
    for p in filtered:
        label = (p.get("sport") or p.get("league") or "").strip()
        if label:
            break
    if not label:
        label = sport_slug or "Unknown"

    tier_param = (tiers or "").strip()
    tier_desc = f" · tier={tier_param}" if tier_param else ""

    rows_html = ""
    if not page_props:
        rows_html = "<tr><td colspan='9'>No props on this page.</td></tr>"
    else:
        for p in page_props:
            tier_raw = str(p.get("tier", "")).lower()
            tier_label = tier_raw.capitalize() if tier_raw else ""
            tier_class = ""
            if tier_raw == "goblin":
                tier_class = " tier-goblin"
            elif tier_raw == "demon":
                tier_class = " tier-demon"

            ud_over = p.get("ud_american_over") or ""
            ud_under = p.get("ud_american_under") or ""
            if ud_over or ud_under:
                odds_text = f"{ud_over or '?'} / {ud_under or '?'}"
            else:
                odds_text = ""

            rows_html += f"""
            <tr>
              <td>{p.get("player","")}</td>
              <td>{p.get("team","")}</td>
              <td>{p.get("opponent","")}</td>
              <td>{p.get("stat","")}</td>
              <td>{p.get("line","")}</td>
              <td>{odds_text}</td>
              <td><span class="pill{tier_class}">{tier_label}</span></td>
              <td><span class="pill time">{p.get("game_time","")}</span></td>
              <td><span class="pill league">{p.get("sport","")}</span></td>
            </tr>
            """

    prev_link = ""
    next_link = ""
    base_query = ""
    if tier_param:
        base_query = f"?tiers={tier_param}"

    if total > 0:
        if page > 1:
            prev_page = page - 1
            prev_link = (
                f"<a href='/model-board-view/{sport_slug}/page/{prev_page}{base_query}'>"
                "← Prev page</a>"
            )
        if page < total_pages:
            next_page = page + 1
            next_link = (
                f"<a href='/model-board-view/{sport_slug}/page/{next_page}{base_query}'>"
                "Next page →</a>"
            )

    pager_html = ""
    if total > 0:
        pager_html = f"<div class='pager'>Page {page} of {total_pages} · {total} props total</div>"

    return HTMLResponse(
        f"""
    <html>
      <head>
        <title>{label} props · page {page}</title>
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
            max-width: 1100px;
            margin: 0 auto;
            padding: 1.25rem 1.25rem 1.75rem;
          }}
          h1 {{
            font-size: 1.1rem;
            margin-bottom: 0.2rem;
          }}
          p {{
            font-size: 0.85rem;
            color: #9ca3af;
            margin-top: 0;
            margin-bottom: 0.75rem;
          }}
          a {{
            color: #38bdf8;
            text-decoration: none;
            font-size: 0.85rem;
          }}
          a:hover {{
            text-decoration: underline;
          }}
          .back-links {{
            display: flex;
            gap: 0.75rem;
            margin-bottom: 0.75rem;
            flex-wrap: wrap;
          }}
          .table-wrapper {{
            border-radius: 0.75rem;
            border: 1px solid #1f2937;
            overflow: hidden;
            background: #020617;
          }}
          table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.8rem;
          }}
          thead {{
            background: #111827;
          }}
          thead th {{
            text-align: left;
            padding: 0.5rem 0.6rem;
            white-space: nowrap;
            font-weight: 500;
            color: #e5e7eb;
            border-bottom: 1px solid #1f2937;
          }}
          tbody td {{
            padding: 0.45rem 0.6rem;
            border-bottom: 1px solid #111827;
          }}
          tbody tr:nth-child(even) {{ background-color: #020617; }}
          tbody tr:nth-child(odd) {{ background-color: #020617; }}
          tbody tr:hover {{ background-color: #111827; }}
          .pill {{
            display: inline-flex;
            align-items: center;
            padding: 0.15rem 0.5rem;
            border-radius: 9999px;
            font-size: 0.7rem;
            border: 1px solid #4b5563;
            color: #e5e7eb;
          }}
          .pill.tier-goblin {{
            border-color: #fbbf24;
            color: #fbbf24;
          }}
          .pill.tier-demon {{
            border-color: #f97373;
            color: #fecaca;
          }}
          .pill.time {{
            border-color: #facc15;
            color: #facc15;
          }}
          .pill.league {{
            border-color: #22c55e;
            color: #bbf7d0;
          }}
          .pager {{
            margin-top: 0.6rem;
            font-size: 0.8rem;
            color: #9ca3af;
          }}
          .pager-links {{
            margin-top: 0.4rem;
            display: flex;
            gap: 1rem;
            font-size: 0.8rem;
          }}
        </style>
      </head>
      <body>
        <main>
          <div class="back-links">
            <a href="/model-index-main">← Back to Model Index Hub</a>
          </div>
          <h1>{label} props · page {page}{tier_desc}</h1>
          <p>HTML view of this slice of the board. Each row is one prop.</p>

          <div class="table-wrapper">
            <table>
              <thead>
                <tr>
                  <th>Player</th>
                  <th>Team</th>
                  <th>Opponent / Matchup</th>
                  <th>Stat</th>
                  <th>Line</th>
                  <th>UD Odds (O/U)</th>
                  <th>Tier</th>
                  <th>Game Time</th>
                  <th>Sport</th>
                </tr>
              </thead>
              <tbody>
                {rows_html}
              </tbody>
            </table>
          </div>
          {pager_html}
          <div class="pager-links">
            {prev_link}
            {next_link}
          </div>
        </main>
      </body>
    </html>
    """
    )

# -------------------------------------------------------------------
# Main model index hub (sport/tier categories)
# -------------------------------------------------------------------


@app.get("/model-index-main", response_class=HTMLResponse)
def model_index_main():
    """
    Top-level hub that lists which sport/tier categories currently exist.
    Links to /model-index?sport=<slug>[&tier=...] which in turn links to
    /model-board-view pages.
    """
    props = get_current_props()

    by_slug: Dict[str, Dict[str, Any]] = {}

    for p in props:
        label = (p.get("sport") or p.get("league") or "").strip()
        if not label:
            continue
        slug = get_prop_sport_slug(p)
        if not slug:
            continue
        tier = str(p.get("tier", "standard")).lower()
        if tier not in ALLOWED_TIERS:
            tier = "standard"

        bucket = by_slug.setdefault(
            slug,
            {"label": label, "total": 0, "tiers": {t: 0 for t in ALLOWED_TIERS}},
        )
        bucket["total"] += 1
        bucket["tiers"][tier] = bucket["tiers"].get(tier, 0) + 1

    items = sorted(by_slug.items(), key=lambda kv: kv[1]["label"].lower())

    html = """
    <html>
      <head>
        <title>Model Board Hub</title>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
          :root { color-scheme: dark; }
          body {
            margin: 0;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #020617;
            color: #e5e7eb;
          }
          main {
            max-width: 900px;
            margin: 0 auto;
            padding: 1.5rem 1.25rem 2rem;
          }
          h1 {
            font-size: 1.3rem;
            margin-bottom: 0.75rem;
          }
          h2 {
            margin-top: 1.2rem;
            margin-bottom: 0.4rem;
            font-size: 1.05rem;
          }
          p {
            font-size: 0.9rem;
            color: #9ca3af;
          }
          ul {
            list-style: none;
            padding-left: 0;
            margin-top: 0.3rem;
            margin-bottom: 0.8rem;
          }
          li {
            margin-bottom: 0.25rem;
          }
          a {
            color: #38bdf8;
            text-decoration: none;
            font-size: 0.85rem;
          }
          a:hover {
            text-decoration: underline;
          }
          .pill {
            display: inline-block;
            margin-left: 0.4rem;
            padding: 0.1rem 0.5rem;
            border-radius: 9999px;
            border: 1px solid #4b5563;
            font-size: 0.7rem;
            color: #9ca3af;
          }
        </style>
      </head>
      <body>
        <main>
          <h1>Model Board Hub</h1>
          <p>
            This page lists the available sport/tier categories. Each link goes to a
            <code>/model-index?sport=...&amp;tier=...</code> page, which in turn lists
            HTML model-board pages like <code>/model-board-view/nba/page/1?tiers=standard</code>.
          </p>
    """

    if not items:
        html += "<p>No props are loaded yet. Upload from the <a href='/upload'>Upload</a> page.</p>"
    else:
        for slug, info in items:
            label = info["label"]
            total = info["total"]
            tier_counts = info["tiers"]

            html += f"<h2>{label} <span class='pill'>{total} props</span></h2>\n"
            html += "<ul>\n"
            all_url = f"/model-index?sport={slug}"
            html += f"<li><a href='{all_url}'>{label} · all tiers</a></li>\n"

            for tier in ["standard", "goblin", "demon"]:
                count = tier_counts.get(tier, 0)
                if not count:
                    continue
                tier_url = f"/model-index?sport={slug}&tier={tier}"
                label_tier = tier.capitalize()
                html += f"<li><a href='{tier_url}'>{label} · {label_tier} only</a></li>\n"

            html += "</ul>\n"

    html += """
        </main>
      </body>
    </html>
    """
    return HTMLResponse(html)

# -------------------------------------------------------------------
# Filtered model index (one sport / tier slice)
# -------------------------------------------------------------------


@app.get("/model-index", response_class=HTMLResponse)
def model_index(sport: str = "", tier: str = ""):
    """
    HTML index of /model-board-view pages.

    Query params:
      - sport: slug for a sport (e.g. "nba", "nfl", "badminton").
               If omitted → all slugs.
      - tier:  optional tier filter ("standard","goblin","demon").
               If omitted → all tiers.
    """
    props = get_current_props()

    slug_to_label: Dict[str, str] = {}
    counts: Dict[tuple, int] = {}

    for p in props:
        label = (p.get("sport") or p.get("league") or "").strip()
        if not label:
            continue
        slug = get_prop_sport_slug(p)
        if not slug:
            continue
        if slug not in slug_to_label:
            slug_to_label[slug] = label

        t = str(p.get("tier", "standard")).lower()
        if t not in ALLOWED_TIERS:
            t = "standard"

        counts[(slug, t)] = counts.get((slug, t), 0) + 1

    PAGE_SIZE = 150
    tier_filter = tier.lower().strip() if tier else ""

    if sport:
        slugs = [sport.lower()]
    else:
        slugs = sorted(slug_to_label.keys(), key=lambda s: slug_to_label[s].lower())

    html = """
    <html>
      <head>
        <title>Model Board Index</title>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
          :root { color-scheme: dark; }
          body {
            margin: 0;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #020617;
            color: #e5e7eb;
          }
          main {
            max-width: 900px;
            margin: 0 auto;
            padding: 1.5rem 1.25rem 2rem;
          }
          h1 {
            font-size: 1.3rem;
            margin-bottom: 0.75rem;
          }
          h2 {
            margin-top: 1.2rem;
            margin-bottom: 0.4rem;
            font-size: 1.05rem;
          }
          h3 {
            margin-top: 0.6rem;
            margin-bottom: 0.35rem;
            font-size: 0.95rem;
            color: #9ca3af;
          }
          p {
            font-size: 0.9rem;
            color: #9ca3af;
          }
          ul {
            list-style: none;
            padding-left: 0;
          }
          li {
            margin-bottom: 0.25rem;
          }
          a {
            color: #38bdf8;
            text-decoration: none;
            font-size: 0.85rem;
          }
          a:hover {
            text-decoration: underline;
          }
          .pill {
            display: inline-block;
            margin-left: 0.4rem;
            padding: 0.1rem 0.5rem;
            border-radius: 9999px;
            border: 1px solid #4b5563;
            font-size: 0.7rem;
            color: #9ca3af;
          }
        </style>
      </head>
      <body>
        <main>
          <h1>Model Board Index</h1>
          <p>
            This page lists the <code>/model-board-view/&lt;sport&gt;/page/&lt;n&gt;?tiers=...</code> HTML URLs
            for the selected sport/tier slice.
          </p>
          <p><a href="/model-index-main">← Back to Model Index Hub</a></p>
    """

    for slug in slugs:
        label = slug_to_label.get(slug)
        if not label:
            continue

        total_for_slug = sum(
            count
            for (s, t), count in counts.items()
            if s == slug and (not tier_filter or t == tier_filter)
        )
        if total_for_slug == 0:
            continue

        html += f"<h2>{label} <span class='pill'>{total_for_slug} props</span></h2>\n"

        for t in ["standard", "goblin", "demon"]:
            if tier_filter and t != tier_filter:
                continue

            count = counts.get((slug, t), 0)
            if count == 0:
                continue

            pages = (count + PAGE_SIZE - 1) // PAGE_SIZE
            html += f"<h3>{t.capitalize()} <span class='pill'>{count} props · {pages} page(s)</span></h3>\n"
            html += "<ul>\n"
            for page_num in range(1, pages + 1):
                url = f"/model-board-view/{slug}/page/{page_num}?tiers={t}"
                html += f"<li><a href='{url}'>{label} · {t} · page {page_num}</a></li>\n"
            html += "</ul>\n"

    html += """
        </main>
      </body>
    </html>
    """
    return HTMLResponse(html)

# -------------------------------------------------------------------
# Upload page (PrizePicks + Underdog + Extras)
# -------------------------------------------------------------------


@app.get("/upload", response_class=HTMLResponse)
def upload_page():
    return """
    <html>
      <head>
        <title>Upload PrizePicks / Underdog JSON</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0" />
        <style>
          :root { color-scheme: dark; }
          body {
            margin: 0;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #020617;
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
            border: 1px solid #4b5563;
            background-color: #020617;
            color: #e5e7eb;
            font-size: 0.9rem;
            outline: none;
          }
          input[type="text"] {
            width: 100%;
            margin-bottom: 0.8rem;
            padding: 0.55rem 0.7rem;
            border-radius: 0.75rem;
            border: 1px solid #4b5563;
            background-color: #020617;
            color: #e5e7eb;
            font-size: 0.9rem;
            outline: none;
          }
          input[type="text"]::placeholder {
            color: #6b7280;
          }
          textarea {
            width: 100%;
            height: 320px;
            border-radius: 0.75rem;
            border: 1px solid #4b5563;
            background-color: #020617;
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
            background: #38bdf8;
            color: white;
            font-size: 0.9rem;
            cursor: pointer;
          }
          button:hover { filter: brightness(1.07); }
          #status {
            margin-top: 0.7rem;
            font-size: 0.8rem;
            white-space: pre-wrap;
            color: #9ca3af;
          }
        </style>
      </head>
      <body>
        <main>
          <h1>Upload PrizePicks / Underdog JSON</h1>
          <p>
            Choose a sport, then paste either PrizePicks (<code>{"data": [...], "included": [...]}</code>)
            or Underdog (<code>{"games":[...], "over_under_lines":[...]}</code>) JSON and tap
            <strong>Upload</strong>. This will replace any existing props for that sport slice on the board.
          </p>
          <label for="sport">Sport</label>
          <select id="sport">
            <option value="">Select a sport…</option>
            <option value="nfl">NFL</option>
            <option value="nba">NBA</option>
            <option value="nhl">NHL</option>
            <option value="cbb">CBB</option>
            <option value="cfb">CFB</option>
            <option value="soccer">Soccer / FIFA</option>
            <option value="tennis">Tennis</option>
            <option value="cs2">CS2 / CS</option>
            <option value="extras">Extras / Other (manual label)</option>
          </select>

          <div id="extras-label-row" style="display:none;">
            <label for="extras-label">Custom sport name (for extras uploads)</label>
            <input id="extras-label" type="text" placeholder="e.g. Badminton, League of Legends" />
          </div>

          <textarea id="raw" placeholder='Paste raw JSON from PrizePicks or Underdog here…'></textarea>
          <br />
          <button onclick="upload()">Upload</button>
          <div id="status"></div>

          <script>
            async function upload() {
              const status = document.getElementById('status');
              const txt = document.getElementById('raw').value;
              const sport = document.getElementById('sport').value;
              const extrasLabelInput = document.getElementById('extras-label');

              if (!sport) {
                status.textContent = "❌ Please select a sport.";
                return;
              }

              let sport_label = null;
              if (sport === "extras") {
                const lbl = (extrasLabelInput.value || "").trim();
                if (!lbl) {
                  status.textContent = "❌ For extras, please enter a custom sport name (e.g. Badminton).";
                  return;
                }
                sport_label = lbl;
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
                  body: JSON.stringify({ sport, sport_label, raw })
                });
                const data = await res.json();
                if (!res.ok) {
                  status.textContent = "❌ Error: " + (data.detail || JSON.stringify(data));
                  return;
                }
                status.textContent =
                  "✅ Uploaded " + (data.count ?? 0) + " " + (data.sport || "") +
                  " props from " + (data.source || "upload") +
                  ". Total on board: " + (data.total ?? "?") + ".";
              } catch (e) {
                status.textContent = "❌ Network error: " + e;
              }
            }

            document.addEventListener("DOMContentLoaded", () => {
              const sportSelect = document.getElementById('sport');
              const rawTextarea = document.getElementById('raw');
              const statusDiv = document.getElementById('status');
              const extrasRow = document.getElementById('extras-label-row');
              const extrasInput = document.getElementById('extras-label');

              if (sportSelect) {
                sportSelect.addEventListener('change', () => {
                  rawTextarea.value = '';
                  if (statusDiv) statusDiv.textContent = '';
                  if (sportSelect.value === "extras") {
                    if (extrasRow) extrasRow.style.display = "block";
                  } else {
                    if (extrasRow) extrasRow.style.display = "none";
                    if (extrasInput) extrasInput.value = "";
                  }
                });
              }
            });
          </script>
        </main>
      </body>
    </html>
    """

# -------------------------------------------------------------------
# Upload API
# -------------------------------------------------------------------


@app.post("/update-props")
async def update_props(request: Request):
    payload = await request.json()
    sport_key = (payload.get("sport") or "").lower()
    sport_label = (payload.get("sport_label") or "").strip() or None
    raw = payload.get("raw")

    if not sport_key:
        raise HTTPException(status_code=400, detail="Missing 'sport' field.")

    if sport_key != "extras" and sport_key not in SPORTS:
        raise HTTPException(status_code=400, detail="Invalid 'sport' field.")

    if not isinstance(raw, dict):
        raise HTTPException(
            status_code=400,
            detail=(
                "Field 'raw' must be a JSON object containing either PrizePicks "
                "(data+included) or Underdog (games+over_under_lines) payload."
            ),
        )

    # Detect source type
    src_type: Optional[str] = None
    if isinstance(raw.get("data"), list) and isinstance(raw.get("included"), list):
        src_type = "prizepicks"
    elif "over_under_lines" in raw and "games" in raw:
        src_type = "underdog"

    if not src_type:
        raise HTTPException(
            status_code=400,
            detail=(
                "Could not recognize JSON format. Expected PrizePicks "
                '({"data":[...],"included":[...]}) or Underdog '
                '({"games":[...],"over_under_lines":[...]).'
            ),
        )

    try:
        if src_type == "prizepicks":
            new_props = normalize_prizepicks(raw, sport_key, sport_label=sport_label)
        else:
            new_props = normalize_underdog(raw, sport_key, sport_label=sport_label)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    existing = load_file_props_raw_or_empty()

    sport_slug: Optional[str] = None
    out_label: Optional[str] = None
    if new_props:
        sport_slug = get_prop_sport_slug(new_props[0])
        out_label = new_props[0].get("sport")
    else:
        if sport_key == "extras":
            out_label = sport_label or "Extras"
            sport_slug = sport_slug_from_label(out_label)
        else:
            cfg = SPORTS[sport_key]
            out_label = cfg["name"]
            sport_slug = sport_key

    # Remove any old props for this sport_slug, then append new ones
    if sport_slug:
        remaining = [p for p in existing if get_prop_sport_slug(p) != sport_slug]
    else:
        label_lower = (out_label or "").lower()
        remaining = [p for p in existing if (p.get("sport") or "").lower() != label_lower]

    combined = remaining + new_props
    save_props(combined)

    total_live = len(get_current_props())
    return {
        "status": "ok",
        "sport": out_label,
        "sport_key": sport_slug,
        "source": src_type,
        "count": len(new_props),
        "total": total_live,
    }

# -------------------------------------------------------------------
# Export page (multi-sport, pretty CSV)
# -------------------------------------------------------------------


@app.get("/export", response_class=HTMLResponse)
def export_page():
    sport_labels: List[str] = []
    for key, cfg in SPORTS.items():
        name = cfg["name"]
        sport_labels.append(
            f'<label><input type="checkbox" class="sport-checkbox" value="{key}" checked /> {name} ({key})</label>'
        )
    sports_html = "\n".join(sport_labels)

    return """
    <html>
      <head>
        <title>Export Props for ChatGPT</title>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <style>
          :root { color-scheme: dark; }
          body {
            margin: 0;
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            background: #020617;
            color: #e5e7eb;
          }
          main {
            max-width: 900px;
            margin: 0 auto;
            padding: 1.5rem 1.25rem 2rem;
          }
          h1 {
            font-size: 1.3rem;
            margin-bottom: 0.5rem;
          }
          p {
            font-size: 0.9rem;
            color: #9ca3af;
            margin-top: 0;
            margin-bottom: 0.7rem;
          }
          label {
            display: block;
            margin-bottom: 0.25rem;
            font-size: 0.85rem;
            color: #9ca3af;
          }
          .row {
            margin-bottom: 0.9rem;
          }
          .pill-group {
            display: flex;
            flex-wrap: wrap;
            gap: 0.6rem;
            font-size: 0.85rem;
            color: #e5e7eb;
          }
          .pill-group label {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            padding: 0.3rem 0.7rem;
            border-radius: 9999px;
            border: 1px solid #4b5563;
            background-color: #020617;
            margin: 0;
          }
          .pill-group input[type="checkbox"] {
            accent-color: #22c55e;
          }
          input[type="number"] {
            width: 120px;
            padding: 0.4rem 0.6rem;
            border-radius: 0.75rem;
            border: 1px solid #4b5563;
            background-color: #020617;
            color: #e5e7eb;
            font-size: 0.9rem;
            outline: none;
          }
          button {
            margin-top: 0.7rem;
            padding: 0.55rem 1.1rem;
            border-radius: 9999px;
            border: none;
            background: #22c55e;
            color: white;
            font-size: 0.9rem;
            cursor: pointer;
          }
          button:hover { filter: brightness(1.07); }
          #status {
            margin-top: 0.6rem;
            font-size: 0.8rem;
            color: #9ca3af;
            white-space: pre-wrap;
          }
          textarea {
            width: 100%;
            height: 60vh;
            margin-top: 1rem;
            border-radius: 0.75rem;
            border: 1px solid #4b5563;
            background-color: #020617;
            color: #e5e7eb;
            padding: 0.75rem;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 0.8rem;
          }
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
    """ + sports_html + """
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
            async function generateExport() {
              const status = document.getElementById("status");
              const box = document.getElementById("exportBox");

              const selectedSports = Array.from(
                document.querySelectorAll(".sport-checkbox")
              )
                .filter(cb => cb.checked)
                .map(cb => cb.value);

              if (!selectedSports.length) {
                status.textContent = "❌ Please select at least one sport.";
                box.value = "";
                return;
              }

              const tiers = [];
              if (document.getElementById("tier-goblin").checked) tiers.push("goblin");
              if (document.getElementById("tier-standard").checked) tiers.push("standard");
              if (document.getElementById("tier-demon").checked) tiers.push("demon");

              const maxInput = document.getElementById("max").value.trim();
              const maxVal = maxInput ? parseInt(maxInput, 10) : 300;

              status.textContent = "Building export…";
              box.value = "";

              try {
                const res = await fetch("/export-data", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    sports: selectedSports,
                    tiers: tiers,
                    max: maxVal
                  })
                });
                const data = await res.json();
                if (!res.ok) {
                  status.textContent = "❌ Error: " + (data.detail || JSON.stringify(data));
                  return;
                }
                box.value = data.text || "";
                status.textContent = "✅ Export ready (" + (data.count ?? 0) + " props). Long-press / Ctrl+A to copy.";
              } catch (e) {
                status.textContent = "❌ Network error: " + e;
              }
            }
          </script>
        </main>
      </body>
    </html>
    """


@app.post("/export-data")
async def export_data(request: Request):
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
        tier_set = ALLOWED_TIERS.copy()

    try:
        max_props = int(max_props)
    except Exception:
        max_props = 300
    if max_props <= 0:
        max_props = 300

    selected_sport_names = {SPORTS[k]["name"] for k in valid_keys}
    selected_sport_names_lower = {name.lower() for name in selected_sport_names}

    all_props = get_current_props()

    filtered: List[Dict[str, Any]] = []
    for p in all_props:
        sname = (p.get("sport") or "").lower()
        if sname not in selected_sport_names_lower:
            continue
        tier_raw = str(p.get("tier", "")).lower()
        if tier_raw not in tier_set:
            continue
        filtered.append(p)

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
                _clean_csv_val(p.get("sport", "")),
                _clean_csv_val(p.get("player", "")),
                _clean_csv_val(p.get("team", "")),
                _clean_csv_val(p.get("opponent", "")),
                _clean_csv_val(p.get("stat", "")),
                str(p.get("line", "")),
                _clean_csv_val(p.get("tier", "")),
                _clean_csv_val(p.get("game_time", "")),
            ]
        )
        lines.append(line)

    text = "\n".join(lines)
    return {"text": text, "count": len(filtered)}

# -------------------------------------------------------------------
# JSON model-board (for your own tools; model can't see JSON)
# -------------------------------------------------------------------


@app.get("/model-board-json")
def model_board_json(
    sports: str = "all",
    tiers: str = "",
    mode: str = "json",
):
    """
    JSON (or text) version of the model board (for your own scripts).

    Query params:
      - sports: "all" or comma-separated sport keys (nba,nfl,...)
      - tiers:  "standard", "goblin", "demon", or combos like "standard+goblin"
      - mode:   "json" (default) → JSONResponse
                "text"           → CSV-ish PlainTextResponse that ChatGPT can read
    """
    if sports.lower() == "all":
        selected_keys = set(SPORTS.keys())
    else:
        requested = {s.strip().lower() for s in sports.split(",") if s.strip()}
        selected_keys = {k for k in SPORTS.keys() if k in requested}
        if not selected_keys:
            raise HTTPException(status_code=400, detail="No valid sports in 'sports' param.")

    selected_sport_names = {SPORTS[k]["name"] for k in selected_keys}
    selected_sport_names_lower = {name.lower() for name in selected_sport_names}

    tier_set = set()
    if tiers:
        for t in tiers.split("+"):
            t = t.strip().lower()
            if not t:
                continue
            if t not in ALLOWED_TIERS:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Invalid tier '{t}'. Allowed: standard, goblin, demon, "
                        "or combos like standard+goblin."
                    ),
                )
            tier_set.add(t)
    if not tier_set:
        tier_set = ALLOWED_TIERS.copy()

    all_props = get_current_props()
    filtered: List[Dict[str, Any]] = []

    for p in all_props:
        sname = (p.get("sport") or "").lower()
        if sname not in selected_sport_names_lower:
            continue
        tier_raw = str(p.get("tier", "")).lower()
        if tier_raw not in tier_set:
            continue
        filtered.append(p)

    filtered.sort(
        key=lambda p: (
            (p.get("sport") or ""),
            (p.get("game_time") or ""),
            (p.get("player") or ""),
        )
    )

    result: List[Dict[str, Any]] = []
    for p in filtered:
        result.append(
            {
                "sport": p.get("sport"),
                "player": p.get("player"),
                "team": p.get("team"),
                "opponent": p.get("opponent"),
                "stat": p.get("stat"),
                "market": p.get("market"),
                "line": p.get("line"),
                "tier": p.get("tier"),
                "game_time": p.get("game_time"),
                "ud_american_over": p.get("ud_american_over"),
                "ud_american_under": p.get("ud_american_under"),
            }
        )

    # New: allow a plain-text mode for ChatGPT-style consumption
    if mode.lower() == "text":
        lines: List[str] = []
        header = (
            "sport,player,team,opponent,stat,market,line,"
            "tier,game_time,ud_american_over,ud_american_under"
        )
        lines.append(header)

        for p in result:
            line = ",".join(
                [
                    _clean_csv_val(p.get("sport", "")),
                    _clean_csv_val(p.get("player", "")),
                    _clean_csv_val(p.get("team", "")),
                    _clean_csv_val(p.get("opponent", "")),
                    _clean_csv_val(p.get("stat", "")),
                    _clean_csv_val(p.get("market", "")),
                    str(p.get("line", "")),
                    _clean_csv_val(p.get("tier", "")),
                    _clean_csv_val(p.get("game_time", "")),
                    _clean_csv_val(p.get("ud_american_over", "")),
                    _clean_csv_val(p.get("ud_american_under", "")),
                ]
            )
            lines.append(line)

        text = "\n".join(lines)
        return PlainTextResponse(text)

    return JSONResponse(result)
