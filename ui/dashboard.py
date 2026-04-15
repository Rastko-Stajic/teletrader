"""
Dashboard - FastAPI backend serving the local monitoring UI.
Exposes endpoints for live account info, positions, signal log, and kill switch.
"""

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import os
from datetime import datetime
from pathlib import Path

LOG_DIR = "logs"

app = FastAPI(title="TeleTrader Dashboard")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory state shared with main process
_state = {
    "kill_switch":   False,
    "gold_enabled":  False,   # Gold disabled by default
    "signals":       [],      # last 50 parsed signals
    "trades":        [],      # last 50 executed trades
    "account":       {},
    "positions":     [],
    "errors":        [],
    "unrecognized":  [],
}


def get_state():
    return _state


@app.get("/", response_class=HTMLResponse)
async def index():
    html_path = Path(__file__).parent / "dashboard.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>TeleTrader</h1><p>Dashboard HTML not found.</p>")


@app.get("/api/status")
async def status():
    return JSONResponse({
        "running":      True,
        "kill_switch":  _state["kill_switch"],
        "gold_enabled": _state["gold_enabled"],
        "timestamp":    datetime.utcnow().isoformat(),
    })


@app.get("/api/account")
async def account():
    return JSONResponse(_state.get("account", {}))


@app.get("/api/positions")
async def positions():
    return JSONResponse(_state.get("positions", []))


@app.get("/api/signals")
async def signals():
    return JSONResponse(_state.get("signals", []))


@app.get("/api/trades")
async def trades():
    return JSONResponse(_state.get("trades", []))


@app.post("/api/kill-switch/toggle")
async def toggle_kill_switch():
    _state["kill_switch"] = not _state["kill_switch"]
    return JSONResponse({
        "kill_switch": _state["kill_switch"],
        "message": "Kill switch ACTIVATED" if _state["kill_switch"] else "Kill switch deactivated"
    })


@app.post("/api/gold/toggle")
async def toggle_gold():
    _state["gold_enabled"] = not _state["gold_enabled"]
    state = "enabled" if _state["gold_enabled"] else "disabled"
    return JSONResponse({
        "gold_enabled": _state["gold_enabled"],
        "message": f"Gold trading {state}"
    })


@app.get("/api/logs")
async def get_logs():
    log_path = Path(LOG_DIR) / "teletrader.log"
    if not log_path.exists():
        return JSONResponse({"lines": []})
    with open(log_path, "r", encoding="utf-8") as f:
        lines = f.readlines()[-100:]
    return JSONResponse({"lines": [l.rstrip() for l in lines]})


@app.get("/api/errors")
async def get_errors():
    return JSONResponse(_state.get("errors", []))


@app.get("/api/unrecognized")
async def get_unrecognized():
    return JSONResponse(_state.get("unrecognized", []))


# ── State push helpers (called from main.py) ──────────────────────────────────

def push_signal(signal_dict: dict):
    _state["signals"].insert(0, signal_dict)
    _state["signals"] = _state["signals"][:50]


def push_trade(trade_dict: dict):
    _state["trades"].insert(0, trade_dict)
    _state["trades"] = _state["trades"][:50]


def update_account(account_dict: dict):
    _state["account"] = account_dict


def update_positions(positions_list: list):
    _state["positions"] = positions_list


def push_error(message: str):
    _state.setdefault("errors", [])
    _state["errors"].insert(0, {
        "message":   message,
        "timestamp": datetime.utcnow().isoformat(),
    })
    _state["errors"] = _state["errors"][:20]


def push_unrecognized(entry: dict):
    _state.setdefault("unrecognized", [])
    _state["unrecognized"].insert(0, entry)
    _state["unrecognized"] = _state["unrecognized"][:50]
