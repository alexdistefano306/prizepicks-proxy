import os
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    JSONResponse,
    RedirectResponse,
)

app = FastAPI(title="PrizePicks Prop Board")

# CORS so you can hit this from browser / mobile, etc.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Config / constants
# -----------------------------

DATA_FILE = "stored_props.json"

SPORTS: Dict[str, Dict[str, Any]] = {
    "nfl": {"name": "NFL", "league_id": 9},
    "nba": {"name": "NBA", "league_id": 7},
    "nhl": {"name": "NHL", "league_id": 8},
    "cbb": {"name": "CBB", "league_id": 20},
    "cfb": {"name": "CFB", "league_id": 15},
    "soccer": {"name": "Soccer", "league_id": 82},
    "tennis": {"name": "Tennis", "league_id": 5},
    "cs2": {"name": "CS2", "league_id": 265},
}

ALLOWED_TIERS = ["standard", "goblin", "demon"]

CSV_COLUMNS = [
    "sport",
    "tier",
    "player",
    "team",
    "opponent",
    "stat",
    "line",
    "game_time",
    "league_id",
    "sport_key",
]

CURRENT_PROPS: List[Dict[str, Any]] = []


# -----------------------------
# Utility helpers
# -----------------------------


def league_to_sport_key(league_id: Optional[int]) -> Optional[str]:
    if league_id is None:
        return None
    for key, cfg in SPORTS.items():
        if cfg["league_id"] == league_id:
            return key
    return None


def normalize_tier(raw: Dict[str, Any]) -> str:
    val = (
        raw.get("tier")
        or raw.get("odds_Type")
        or raw.get("odds_type")
        or raw.get("oddsType")
        or ""
    )
    val = str(val).strip().lower()

    if val in ALLOWED_TIERS:
        return val
    if val.startswith("goblin"):
        return "goblin"
    if val.startswith("demon"):
        return "demon"
    return "standard"


def infer_sport_key_from_prop(p: Dict[str, Any]) -> Optional[str]:
    sk = p.get("sport_key")
    if isinstance(sk, str) and sk in SPORTS:
        return sk

    # Try mapping from friendly name
    s_label = p.get("sport")
    if isinstance(s_label, str):
        low = s_label.lower()
        for key, cfg in SPORTS.items():
            if low == cfg["name"].lower():
                return key

    # Try league_id
    league_id = p.get("league_id")
    try:
        lid = int(league_id)
    except Exception:
        lid = None
    if lid is not None:
        sk = league_to_sport_key(lid)
        if sk:
            return sk

    return None


def normalize_prop(raw: Dict[str, Any], default_sport_key: Optional[str] = None) -> Dict[str, Any]:
    # Basic fields, with defensive fallbacks
    player = (
        raw.get("player")
        or raw.get("name")
        or raw.get("Player")
        or raw.get("player_name")
        or ""
    )
    team = (
        raw.get("team")
        or raw.get("Team")
        or raw.get("team_abbrev")
        or raw.get("team_name")
        or ""
    )
    opponent = (
        raw.get("opponent")
        or raw.get("Opponent")
        or raw.get("opp")
        or raw.get("opponent_abbrev")
        or ""
    )
    stat = (
        raw.get("stat")
        or raw.get("Stat")
        or raw.get("market")
        or raw.get("stat_type")
        or ""
    )
    category = (
        raw.get("category")
        or raw.get("Category")
        or raw.get("stat_category")
        or stat
    )

    line_val = (
        raw.get("line")
        or raw.get("Line")
        or raw.get("line_score")
        or raw.get("value")
        or raw.get("projection")
    )
    try:
        line_f: Optional[float] = float(line_val) if line_val is not None else None
    except Exception:
        line_f = None

    league_id_val = (
        raw.get("league_id")
        or raw.get("LeagueId")
        or raw.get("league")
        or raw.get("leagueID")
    )
    try:
        league_id_int: Optional[int] = (
            int(league_id_val)
            if league_id_val is not None and str(league_id_val).strip() != ""
            else None
        )
    except Exception:
        league_id_int = None

    # Detect sport key
    sport_val = raw.get("sport") or raw.get("Sport")
    sport_key: Optional[str] = None
    if isinstance(sport_val, str):
        sv = sport_val.strip().lower()
        if sv in SPORTS:
            sport_key = sv

    if sport_key is None and league_id_int is not None:
        sport_key = league_to_sport_key(league_id_int)

    if sport_key is None and default_sport_key:
        sport_key = default_sport_key

    sport_label = (
        SPORTS[sport_key]["name"] if sport_key and sport_key in SPORTS else (sport_val or "")
    )

    tier = normalize_tier(raw)
    game_time = (
        raw.get("game_time")
        or raw.get("start_time")
        or raw.get("GameTime")
        or raw.get("kickoff")
    )

    pid = raw.get("id") or raw.get("prop_id") or raw.get("projection_id")
    if pid is None:
        pid = f"{player}-{sport_label}-{stat}-{line_f}"

    return {
        "id": str(pid),
        "player": player,
        "team": team,
        "opponent": opponent,
        "stat": stat,
        "category": category,
        "line": line_f,
        "sport": sport_label,
        "sport_key": sport_key,
        "league_id": league_id_int,
        "tier": tier,
        "game_time": game_time,
    }


def normalize_uploaded_blob(blob: Any, selected_sport_key: Optional[str]) -> List[Dict[str, Any]]:
    props: List[Dict[str, Any]] = []

    if isinstance(blob, dict):
        if "props" in blob and isinstance(blob["props"], list):
            items = blob["props"]
        elif "projections" in blob and isinstance(blob["projections"], list):
            items = blob["projections"]
        else:
            items = [blob]
    elif isinstance(blob, list):
        items = blob
    else:
        raise ValueError("Uploaded JSON must be an object or an array of objects.")

    for raw in items:
        if not isinstance(raw, dict):
            continue
        props.append(normalize_prop(raw, default_sport_key=selected_sport_key))

    return props


def load_props_from_disk() -> List[Dict[str, Any]]:
    global CURRENT_PROPS
    if CURRENT_PROPS:
        return CURRENT_PROPS

    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                CURRENT_PROPS = json.load(f)
                return CURRENT_PROPS
        except Exception:
            pass

    # Fallback dummy props if nothing stored yet
    CURRENT_PROPS = [
        {
            "id": "demo-nba-1",
            "player": "Demo Player",
            "team": "LAL",
            "opponent": "BOS",
            "stat": "Points",
            "category": "PTS",
            "line": 24.5,
            "sport": "NBA",
            "sport_key": "nba",
            "league_id": 7,
            "tier": "standard",
            "game_time": None,
        }
    ]
    return CURRENT_PROPS


def save_props_to_disk(props: List[Dict[str, Any]]) -> None:
    global CURRENT_PROPS
    CURRENT_PROPS = props
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(props, f, ensure_ascii=False)
    except Exception:
        # Not fatal if write fails on Render
        pass


def get_current_props() -> List[Dict[str, Any]]:
    return load_props_from_disk()


def props_to_csv_rows(props: List[Dict[str, Any]]) -> str:
    lines: List[str] = [",".join(CSV_COLUMNS)]
    for p in props:
        row: List[str] = []
        for col in CSV_COLUMNS:
            v = p.get(col, "")
            if v is None:
                v = ""
            sval = str(v)
            if "," in sval or '"' in sval or "\n" in sval:
                sval = '"' + sval.replace('"', '""') + '"'
            row.append(sval)
        lines.append(",".join(row))
    return "\n".join(lines)


# -----------------------------
# Routes
# -----------------------------


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    props = get_current_props()

    # Sort by sport, then game time, then player
    def sort_key(p: Dict[str, Any]) -> Tuple[str, str, str]:
        skey = infer_sport_key_from_prop(p) or ""
        t = p.get("game_time") or ""
        pl = p.get("player") or ""
        return (skey, t, pl)

    props_sorted = sorted(props, key=sort_key)

    # Build table rows
    rows_html = ""
    for p in props_sorted:
        skey = infer_sport_key_from_prop(p) or ""
        sport_label = p.get("sport") or ""
        tier = str(p.get("tier", "standard")).lower()
        player = p.get("player", "")
        team = p.get("team", "")
        opp = p.get("opponent", "")
        stat = p.get("stat", "")
        line = p.get("line", "")
        game_time = p.get("game_time") or ""

        rows_html += f"""
        <tr data-sport="{skey}" data-tier="{tier}">
          <td>{sport_label}</td>
          <td>{tier.capitalize()}</td>
          <td>{player}</td>
          <td>{team}</td>
          <td>{opp}</td>
          <td>{stat}</td>
          <td>{line}</td>
          <td>{game_time}</td>
        </tr>
        """

    # Sport filter options
    sport_options_html = ""
    for skey, cfg in SPORTS.items():
        sport_options_html += f'<option value="{skey}">{cfg["name"]}</option>'

    html = f"""
    <html>
      <head>
        <title>Odds Board</title>
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
            max-width: 1200px;
            margin: 0 auto;
            padding: 1.5rem 1.25rem 2rem;
          }}
          h1 {{
            font-size: 1.4rem;
            margin-bottom: 0.25rem;
          }}
          p.sub {{
            font-size: 0.85rem;
            color: #9ca3af;
            margin-bottom: 1rem;
          }}
          .toolbar {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.75rem;
            align-items: center;
            margin-bottom: 1rem;
          }}
          select, button {{
            background: #020617;
            color: #e5e7eb;
            border-radius: 9999px;
            border: 1px solid #4b5563;
            padding: 0.35rem 0.75rem;
            font-size: 0.8rem;
          }}
          table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 0.8rem;
          }}
          thead tr {{
            background: #020617;
            position: sticky;
            top: 0;
            z-index: 5;
          }}
          th, td {{
            padding: 0.4rem 0.5rem;
            border-bottom: 1px solid #111827;
            text-align: left;
            white-space: nowrap;
          }}
          th {{
            font-weight: 600;
            color: #9ca3af;
          }}
          tbody tr:nth-child(even) {{
            background: #020617;
          }}
          tbody tr:nth-child(odd) {{
            background: #030712;
          }}
          .badge {{
            display: inline-block;
            font-size: 0.7rem;
            padding: 0.1rem 0.45rem;
            border-radius: 9999px;
          }}
          .tier-standard {{
            border: 1px solid #4b5563;
            color: #e5e7eb;
          }}
          .tier-goblin {{
            border: 1px solid #22c55e;
            color: #22c55e;
          }}
          .tier-demon {{
            border: 1px solid #f97316;
            color: #f97316;
          }}
        </style>
      </head>
      <body>
        <main>
          <h1>Odds Board</h1>
          <p class="sub">
            This table reflects the last uploaded props. Use sport/tier filters below to view slices.
          </p>

          <div class="toolbar">
            <label>
              Sport:
              <select id="sportFilter">
                <option value="">All</option>
                {sport_options_html}
              </select>
            </label>
            <label>
              Tier:
              <select id="tierFilter">
                <option value="">All</option>
                <option value="standard">Standard</option>
                <option value="goblin">Goblin</option>
                <option value="demon">Demon</option>
              </select>
            </label>
          </div>

          <div style="overflow-x:auto; border-radius: 0.75rem; border: 1px solid #111827;">
            <table>
              <thead>
                <tr>
                  <th>Sport</th>
                  <th>Tier</th>
                  <th>Player</th>
                  <th>Team</th>
                  <th>Opponent</th>
                  <th>Stat</th>
                  <th>Line</th>
                  <th>Game Time</th>
                </tr>
              </thead>
              <tbody id="propsBody">
                {rows_html}
              </tbody>
            </table>
          </div>

        </main>

        <script>
          const sportFilter = document.getElementById('sportFilter');
          const tierFilter = document.getElementById('tierFilter');
          const tbody = document.getElementById('propsBody');

          function applyFilters() {{
            const sportVal = sportFilter.value;
            const tierVal = tierFilter.value;
            const rows = tbody.querySelectorAll('tr');

            rows.forEach(row => {{
              const rsport = row.getAttribute('data-sport') || '';
              const rtier = row.getAttribute('data-tier') || '';
              let show = true;
              if (sportVal && rsport !== sportVal) show = false;
              if (tierVal && rtier !== tierVal) show = false;
              row.style.display = show ? '' : 'none';
            }});
          }}

          sportFilter.addEventListener('change', applyFilters);
          tierFilter.addEventListener('change', applyFilters);
        </script>
      </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/upload", response_class=HTMLResponse)
def upload_get(request: Request, sport: str = "", saved: str = "") -> HTMLResponse:
    # sport dropdown
    sport_options_html = ""
    for skey, cfg in SPORTS.items():
        selected = "selected" if skey == sport else ""
        sport_options_html += f'<option value="{skey}" {selected}>{cfg["name"]}</option>'

    saved_msg = ""
    if saved:
        saved_msg = "<p style='color:#22c55e;font-size:0.85rem;margin-top:0.5rem;'>Props uploaded and saved.</p>"

    html = f"""
    <html>
      <head>
        <title>Upload Props JSON</title>
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
          label {{
            font-size: 0.9rem;
            display: block;
            margin-bottom: 0.25rem;
          }}
          select {{
            background: #020617;
            color: #e5e7eb;
            border-radius: 9999px;
            border: 1px solid #4b5563;
            padding: 0.35rem 0.75rem;
            font-size: 0.8rem;
          }}
          textarea {{
            width: 100%;
            min-height: 260px;
            background: #020617;
            color: #e5e7eb;
            border-radius: 0.75rem;
            border: 1px solid #111827;
            padding: 0.75rem;
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 0.8rem;
          }}
          button {{
            margin-top: 0.75rem;
            background: #22c55e;
            color: #020617;
            border-radius: 9999px;
            border: none;
            padding: 0.45rem 0.9rem;
            font-size: 0.85rem;
            font-weight: 600;
            cursor: pointer;
          }}
          button:hover {{
            filter: brightness(1.05);
          }}
          p.sub {{
            font-size: 0.85rem;
            color: #9ca3af;
            margin-bottom: 1rem;
          }}
        </style>
      </head>
      <body>
        <main>
          <h1>Upload Props JSON</h1>
          <p class="sub">
            Paste processed JSON (array or object with <code>props</code> / <code>projections</code>).
            The selected sport will be applied if league IDs are missing.
          </p>
          <form method="post">
            <label for="sport">Sport</label>
            <select id="sport" name="sport">
              {sport_options_html}
            </select>
            {saved_msg}
            <label for="raw_json" style="margin-top:1rem;">JSON</label>
            <textarea id="raw_json" name="raw_json" placeholder='[ {{ "player": "...", "team": "...", ... }} ]'></textarea>
            <button type="submit">Upload</button>
          </form>
        </main>

        <script>
          document.addEventListener('DOMContentLoaded', function() {{
            const sportSelect = document.getElementById('sport');
            const textarea = document.getElementById('raw_json');
            let lastSport = sportSelect.value;

            sportSelect.addEventListener('change', function() {{
              // clear textarea anytime sport changes, to avoid cross-sport paste
              textarea.value = '';
              lastSport = sportSelect.value;
            }});
          }});
        </script>
      </body>
    </html>
    """
    return HTMLResponse(html)


@app.post("/upload", response_class=HTMLResponse)
async def upload_post(
    sport: str = Form(...),
    raw_json: str = Form(...),
) -> HTMLResponse:
    skey = sport.lower()
    if skey not in SPORTS:
        raise HTTPException(status_code=400, detail="Unknown sport key.")

    raw_json_stripped = (raw_json or "").strip()
    if not raw_json_stripped:
        raise HTTPException(status_code=400, detail="No JSON provided.")

    try:
        blob = json.loads(raw_json_stripped)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    new_props = normalize_uploaded_blob(blob, selected_sport_key=skey)

    # Optional sanity: check for obvious cross-sport mistakes when league_id present
    mismatches: List[Dict[str, Any]] = []
    for p in new_props:
        pk = infer_sport_key_from_prop(p)
        if pk is not None and pk != skey:
            mismatches.append(p)

    if mismatches:
        raise HTTPException(
            status_code=400,
            detail=f"Some props appear to belong to a different sport than '{skey}'. Check league_id / JSON.",
        )

    # Merge: replace this sport's props, keep others
    existing = get_current_props()
    remaining = [p for p in existing if infer_sport_key_from_prop(p) != skey]
    combined = remaining + new_props
    save_props_to_disk(combined)

    # Redirect to GET /upload so refresh doesn't resubmit
    return RedirectResponse(url=f"/upload?saved=1&sport={skey}", status_code=303)


@app.get("/props.json", response_class=JSONResponse)
def props_json() -> JSONResponse:
    return JSONResponse(get_current_props())


@app.get("/model-board-json", response_class=JSONResponse)
def model_board_json_all() -> JSONResponse:
    """Raw JSON model board (for you; JSON is opaque to the model)."""
    return JSONResponse(get_current_props())


@app.get(
    "/model-board/{sport}/page/{page}",
    response_class=PlainTextResponse,
)
def model_board_paged(
    sport: str,
    page: int,
    page_size: int = 80,  # smaller pages to avoid truncation
    tiers: str = "",
) -> PlainTextResponse:
    skey = sport.lower()
    if skey not in SPORTS:
        raise HTTPException(status_code=400, detail="Unknown sport key.")

    props = get_current_props()
    filtered = [p for p in props if infer_sport_key_from_prop(p) == skey]

    tiers_str = (tiers or "").strip().lower()
    if tiers_str:
        allowed: set[str] = set()
        for token in tiers_str.replace(",", "+").split("+"):
            t = token.strip()
            if t in ALLOWED_TIERS:
                allowed.add(t)
        if allowed:
            filtered = [
                p
                for p in filtered
                if (str(p.get("tier", "standard")).lower() in allowed)
            ]

    # Sort by game_time then player for stability
    def sort_key(p: Dict[str, Any]) -> Tuple[str, str]:
        t = p.get("game_time") or ""
        pl = p.get("player") or ""
        return (t, pl)

    filtered.sort(key=sort_key)

    if page < 1:
        raise HTTPException(status_code=400, detail="Page must be >= 1")

    start = (page - 1) * page_size
    end = start + page_size
    page_items = filtered[start:end]

    csv_text = props_to_csv_rows(page_items)
    return PlainTextResponse(csv_text)


@app.get("/model-index", response_class=HTMLResponse)
def model_index(sport: str = "", tier: str = "") -> HTMLResponse:
    """
    HTML index of /model-board pages.

    Query params:
      - sport: optional sport key (nfl,nba,nhl,cbb,cfb,soccer,tennis,cs2).
               If omitted → all sports.
      - tier:  optional tier filter (standard,goblin,demon).
               If omitted → all tiers.

    Example (for v4 prompts):
      /model-index?sport=nba&tier=standard
    """
    props = get_current_props()

    # Optional sport filter
    sport_filter_keys: List[str]
    if sport:
        skey = sport.lower()
        if skey not in SPORTS:
            raise HTTPException(status_code=400, detail="Unknown sport key.")
        sport_filter_keys = [skey]
    else:
        sport_filter_keys = list(SPORTS.keys())

    # Optional tier filter
    tier_filter = tier.lower().strip() if tier else ""

    # Count props per (sport_key, tier)
    counts: Dict[Tuple[str, str], int] = {}
    for p in props:
        skey = infer_sport_key_from_prop(p)
        if not skey or skey not in sport_filter_keys:
            continue
        t = str(p.get("tier", "standard")).lower()
        if t not in ALLOWED_TIERS:
            t = "standard"
        if tier_filter and t != tier_filter:
            continue
        k = (skey, t)
        counts[k] = counts.get(k, 0) + 1

    PAGE_SIZE = 80  # keep in sync with model_board_paged

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
            Each link below is a CSV page from <code>/model-board/&lt;sport&gt;/page/&lt;n&gt;?tiers=...</code>.
            For best results with the model, call this page with a <code>sport</code> and optional <code>tier</code> query:
            e.g. <code>/model-index?sport=nba&tier=standard</code>.
          </p>
    """

    # For each sport in filter
    for skey in sport_filter_keys:
        cfg = SPORTS[skey]
        sname = cfg["name"]

        # Does this sport have any props at all (respecting tier filter)?
        total_for_sport = sum(
            count
            for (s, t), count in counts.items()
            if s == skey and (not tier_filter or t == tier_filter)
        )
        if total_for_sport == 0:
            continue

        html += f"<h2>{sname}</h2>\n"

        for t in ["standard", "goblin", "demon"]:
            if tier_filter and t != tier_filter:
                continue

            count = counts.get((skey, t), 0)
            if count == 0:
                continue

            pages = (count + PAGE_SIZE - 1) // PAGE_SIZE
            html += f"<h3>{t.capitalize()} <span class='pill'>{count} props · {pages} page(s)</span></h3>\n"
            html += "<ul>\n"
            for page in range(1, pages + 1):
                url = f"/model-board/{skey}/page/{page}?tiers={t}"
                html += f"<li><a href='{url}'>{sname} · {t} · page {page}</a></li>\n"
            html += "</ul>\n"

    html += """
        </main>
      </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/model-index-main", response_class=HTMLResponse)
def model_index_main() -> HTMLResponse:
    """
    Small 'hub' page that links to sport/tier-specific model-index pages.

    Example links:
      /model-index?sport=nba&tier=standard
      /model-index?sport=nba&tier=goblin
      /model-index?sport=nba&tier=demon
      etc.

    This page never inspects props; it's always tiny and safe from truncation.
    """
    html = """
    <html>
      <head>
        <title>Model Index Hub</title>
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
          ul {
            list-style: none;
            padding-left: 0;
          }
          li {
            margin-bottom: 0.2rem;
          }
          a {
            color: #38bdf8;
            text-decoration: none;
            font-size: 0.85rem;
          }
          a:hover {
            text-decoration: underline;
          }
          .tier-label {
            font-size: 0.8rem;
            color: #9ca3af;
            margin-right: 0.4rem;
          }
        </style>
      </head>
      <body>
        <main>
          <h1>Model Index Hub</h1>
          <p style="font-size:0.9rem;color:#9ca3af;">
            This page links to sport/tier-specific model indexes (which then list CSV pages).
            For prompts, you can just give the model this URL and say which sport/tier to use.
          </p>
    """
    # Build link sections for each sport
    for skey, cfg in SPORTS.items():
        sname = cfg["name"]
        html += f"<h2>{sname}</h2>\n<ul>\n"
        for tier in ALLOWED_TIERS:
            url = f"/model-index?sport={skey}&tier={tier}"
            html += f"<li><span class='tier-label'>{tier.capitalize()}:</span><a href='{url}'>{url}</a></li>\n"
        html += "</ul>\n"

    html += """
        </main>
      </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/health", response_class=PlainTextResponse)
def health() -> PlainTextResponse:
    return PlainTextResponse("ok")