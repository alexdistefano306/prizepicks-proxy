import json
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse

app = FastAPI()

# ---------------------------------------------------------------------------
# Upload page HTML
#   - SAME textarea for JSON
#   - Sport dropdown now has "Extras"
#   - When "Extras" is chosen, a text box appears to label the sport
# ---------------------------------------------------------------------------

UPLOAD_PAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Prop Upload</title>
    <style>
        body {
            font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
            margin: 0;
            padding: 2rem;
            background: #0f172a;
            color: #e5e7eb;
        }
        h1 {
            margin-bottom: 1rem;
        }
        form {
            max-width: 960px;
            margin-bottom: 2rem;
            padding: 1.5rem;
            border-radius: 1rem;
            background: #020617;
            box-shadow: 0 16px 40px rgba(15, 23, 42, 0.6);
        }
        label {
            display: block;
            margin-top: 0.75rem;
            margin-bottom: 0.25rem;
            font-weight: 500;
        }
        textarea, select, input[type="text"] {
            width: 100%;
            box-sizing: border-box;
            padding: 0.5rem 0.75rem;
            border-radius: 0.5rem;
            border: 1px solid #1f2937;
            background: #020617;
            color: #e5e7eb;
            font-family: monospace;
            font-size: 0.9rem;
        }
        textarea {
            min-height: 260px;
            resize: vertical;
        }
        button {
            margin-top: 1rem;
            padding: 0.6rem 1.4rem;
            border-radius: 999px;
            border: none;
            font-weight: 600;
            background: linear-gradient(135deg, #22c55e, #22d3ee);
            color: #020617;
            cursor: pointer;
        }
        button:hover {
            opacity: 0.9;
        }
        .row {
            display: flex;
            gap: 1rem;
            margin-top: 0.5rem;
        }
        .row > div {
            flex: 1;
        }
        .hint {
            font-size: 0.8rem;
            color: #9ca3af;
            margin-top: 0.15rem;
        }
        .preview {
            max-width: 960px;
            padding: 1rem 1.25rem;
            border-radius: 0.75rem;
            background: #020617;
            border: 1px solid #1f2937;
            font-size: 0.85rem;
            white-space: pre-wrap;
        }
        code {
            font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
        }
    </style>
</head>
<body>
    <h1>Upload PrizePicks / Underdog JSON</h1>
    <form method="post" action="/upload">
        <label for="payload">Raw JSON (PrizePicks or Underdog)</label>
        <textarea id="payload" name="payload" spellcheck="false"
                  placeholder='Paste the raw PrizePicks or Underdog JSON here...'></textarea>

        <div class="row">
            <div>
                <label for="sport">Sport</label>
                <select id="sport" name="sport">
                    <option value="nba">NBA</option>
                    <option value="nfl">NFL</option>
                    <option value="nhl">NHL</option>
                    <option value="mlb">MLB</option>
                    <option value="tennis">Tennis</option>
                    <option value="soccer">Soccer</option>
                    <option value="cs2">CS2</option>
                    <!-- NEW: Extras option so you can label an arbitrary sport -->
                    <option value="extras">Extras</option>
                </select>
                <div class="hint">
                    This field is for how you want the board stored (NBA/NFL/etc.).
                    For Underdog, we still auto-detect book format.
                </div>
            </div>
            <div id="extras-wrapper" style="display:none;">
                <label for="extras_sport_label">Extras sport label</label>
                <input type="text" id="extras_sport_label" name="extras_sport_label"
                       placeholder="e.g. WNBA, LOL, TableTennis" />
                <div class="hint">
                    Used only when <strong>Extras</strong> is selected. This becomes the stored sport name.
                </div>
            </div>
        </div>

        <button type="submit">Upload &amp; Parse</button>
    </form>

    %PREVIEW_BLOCK%
    <script>
        const sportSelect = document.getElementById('sport');
        const extrasWrapper = document.getElementById('extras-wrapper');

        function toggleExtras() {
            if (sportSelect.value === 'extras') {
                extrasWrapper.style.display = 'block';
            } else {
                extrasWrapper.style.display = 'none';
            }
        }

        sportSelect.addEventListener('change', toggleExtras);
        toggleExtras();
    </script>
</body>
</html>
"""


def render_upload_page(preview_text: str = "") -> HTMLResponse:
    """Render the upload page, optionally with a small preview of parsed picks."""
    if preview_text:
        preview_html = f"""
        <div class="preview">
            <strong>Parsed preview</strong><br><br>
            <code>{preview_text}</code>
        </div>
        """
    else:
        preview_html = ""

    html = UPLOAD_PAGE_HTML.replace("%PREVIEW_BLOCK%", preview_html)
    return HTMLResponse(content=html)


@app.get("/", response_class=HTMLResponse)
async def get_upload_page() -> HTMLResponse:
    return render_upload_page()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# NEW: mapping for Underdog sport_id naming differences
UD_SPORT_MAP: Dict[str, str] = {
    "fifa": "soccer",  # Underdog "FIFA" → your "soccer"
    "cs": "cs2",       # Underdog "CS"   → your "cs2"
}


def normalize_ud_sport(sport_id: Optional[str]) -> Optional[str]:
    if not sport_id:
        return None
    key = sport_id.lower()
    return UD_SPORT_MAP.get(key, key)


def detect_provider(payload: Dict[str, Any]) -> str:
    """
    Very small heuristic:
    - PrizePicks board JSON: typically has "data" + "included"
    - Underdog: "over_under_lines" + "games" (and usually "appearances")
    """
    if "data" in payload and "included" in payload:
        return "prizepicks"
    if "over_under_lines" in payload and "games" in payload:
        return "underdog"
    raise HTTPException(status_code=400, detail="Cannot detect provider (not PrizePicks or Underdog JSON).")


# ---------------------------------------------------------------------------
# NEW: Underdog parser
#   Produces a generic list of dicts in the same logical shape you're already
#   using for PrizePicks boards (sport, player, team, opponent, stat, line, tier, game_time).
#   You can plug this into your existing CSV / storage pipeline.
# ---------------------------------------------------------------------------

def parse_underdog_payload(payload: Dict[str, Any], default_sport_label: str) -> List[Dict[str, Any]]:
    """
    Flatten Underdog `over_under_lines` structure into a list of prop rows.

    Notes:
    - Uses games[].sport_id but normalizes "FIFA"→"soccer", "CS"→"cs2".
    - League IDs do NOT exist here; we leave league-related fields empty.
    - Tier is defaulted to "standard" (you can change this if you add tier logic later).
    """
    appearances = {a["id"]: a for a in payload.get("appearances", [])}
    games = {g["id"]: g for g in payload.get("games", [])}

    rows: List[Dict[str, Any]] = []

    for line in payload.get("over_under_lines", []):
        over_under = line.get("over_under") or {}
        appearance_stat = over_under.get("appearance_stat") or {}

        appearance_id = appearance_stat.get("appearance_id")
        if not appearance_id:
            # Some non-player markets might not have an appearance; skip them.
            continue

        appearance = appearances.get(appearance_id)
        if not appearance:
            continue

        match_id = appearance.get("match_id")
        game = games.get(match_id)
        if not game:
            continue

        # Sport: prefer normalized Underdog sport_id; fall back to default label
        ud_sport_raw = game.get("sport_id")
        normalized_ud_sport = normalize_ud_sport(ud_sport_raw)
        sport = (normalized_ud_sport or default_sport_label).lower()

        # Team / opponent from abbreviated_title + team_id mapping
        team_id = appearance.get("team_id")
        team_abbr = ""
        opp_abbr = ""

        abbr = game.get("abbreviated_title") or ""
        if "@" in abbr:
            away_abbr, home_abbr = [p.strip() for p in abbr.split("@", 1)]
            if team_id == game.get("home_team_id"):
                team_abbr = home_abbr
                opp_abbr = away_abbr
            elif team_id == game.get("away_team_id"):
                team_abbr = away_abbr
                opp_abbr = home_abbr

        # Player name from option header (same for higher/lower)
        # (We just use the first option; both share the same player)
        options = line.get("options") or []
        player_name = ""
        if options:
            player_name = options[0].get("selection_header") or ""

        # Stat label + key
        display_stat = appearance_stat.get("display_stat") or ""
        stat_key = appearance_stat.get("stat") or ""

        # Line value
        try:
            stat_value = float(line.get("stat_value"))
        except (TypeError, ValueError):
            continue

        # Game time
        game_time = game.get("scheduled_at")

        # Build row in the same logical shape you already use
        rows.append(
            {
                "id": line.get("id"),
                "sport": sport,
                "league": "",             # Underdog does not provide league IDs
                "player": player_name,
                "search_player": player_name,
                "opponent": opp_abbr,
                "display_stat": display_stat,
                "stat_key": stat_key,
                "line": stat_value,
                "tier": "standard",       # you can adjust this later if you add Underdog tiers
                "game_time": game_time,
                "source": "underdog",
            }
        )

    return rows


# ---------------------------------------------------------------------------
# PrizePicks parser
# ---------------------------------------------------------------------------

def parse_prizepicks_payload(payload: Dict[str, Any], sport_label: str) -> List[Dict[str, Any]]:
    """
    Placeholder for your existing PrizePicks flattening logic.

    IMPORTANT:
    - Replace the body of this function with whatever you're already using
      to transform PrizePicks JSON into your CSV/prop-row structure.
    - The Underdog parser above is designed to mirror the same logical
      fields (id, sport, league, player, search_player, opponent,
      display_stat, stat_key, line, tier, game_time).

    If you already have this implemented elsewhere in your current main.py,
    you can simply KEEP your existing function and ignore this stub.
    """
    raise NotImplementedError("Use your existing PrizePicks parsing logic here.")


# ---------------------------------------------------------------------------
# Upload handler
#   - Uses same textarea for both providers
#   - Detects PrizePicks vs Underdog
#   - Handles Extras sport label
# ---------------------------------------------------------------------------

@app.post("/upload", response_class=HTMLResponse)
async def upload(
    payload: str = Form(...),
    sport: str = Form(...),
    extras_sport_label: str = Form(""),
) -> HTMLResponse:
    # Resolve the effective sport label used for storage
    sport = sport.lower()
    if sport == "extras":
        label = extras_sport_label.strip()
        if not label:
            raise HTTPException(
                status_code=400,
                detail="When 'Extras' is selected, you must provide an extras_sport_label.",
            )
        effective_sport = label
    else:
        effective_sport = sport

    try:
        data = json.loads(payload)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {e}")

    provider = detect_provider(data)

    if provider == "prizepicks":
        # Use your existing PrizePicks logic here
        rows = parse_prizepicks_payload(data, effective_sport)
    elif provider == "underdog":
        # NEW: Underdog handling (with FIFA/CS mapping and no league IDs)
        rows = parse_underdog_payload(data, effective_sport)
    else:
        # detect_provider should already have raised, but keep this guard
        raise HTTPException(status_code=400, detail=f"Unsupported provider: {provider}")

    # At this point `rows` should be in the same shape as your existing pipeline expects.
    # You can drop in your existing CSV writing / DB insert code here.
    #
    # For safety, this example ONLY shows a small preview on the page and does
    # not touch disk. Replace this with your current persistence code.

    preview_rows = rows[:10]
    preview_text = "\n".join(
        f"{r.get('sport')} | {r.get('player')} | {r.get('display_stat')} | {r.get('line')} @ {r.get('game_time')}"
        for r in preview_rows
    )
    if len(rows) > len(preview_rows):
        preview_text += f"\n\n... and {len(rows) - len(preview_rows)} more rows"

    return render_upload_page(preview_text=preview_text)
