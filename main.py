from typing import List, Dict
from fastapi import FastAPI
from fastapi.responses import JSONResponse

app = FastAPI(title="Props Proxy Demo")

# Dummy props so you can see it working locally / on Render
DUMMY_PROPS: List[Dict] = [
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

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/props.json")
def props_json():
    """
    This is the endpoint I'll eventually read from.
    For now it just returns DUMMY_PROPS so you can verify deployment.
    """
    return JSONResponse(DUMMY_PROPS)
