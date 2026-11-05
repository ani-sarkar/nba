"""
Stadium score server.

Polls ESPN's public NBA scoreboard in a background thread and serves the
current game as small flat JSON at /score for the ESP32 to poll.

Every time something worth reacting to happens (a basket, a new quarter, the
game starting/ending), `seq` goes up and `event` says what it was. The ESP32
only has to remember the last `seq` it saw and act when it changes.
"""

import logging
import os
import threading
import time

import requests
from flask import Flask, jsonify

SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"

# Team to follow by tricode, e.g. "LAL". Blank = follow the first live game.
TEAM = os.environ.get("TEAM", "").strip().upper()
LIVE_POLL_SECONDS = float(os.environ.get("LIVE_POLL_SECONDS", 5))
IDLE_POLL_SECONDS = float(os.environ.get("IDLE_POLL_SECONDS", 60))
ERROR_POLL_SECONDS = 10

# Game status values sent to the ESP32
PREGAME, LIVE, FINAL = 1, 2, 3
ESPN_STATES = {"pre": PREGAME, "in": LIVE, "post": FINAL}

# ESPN abbreviations that differ from the NBA tricodes the firmware uses.
ESPN_TO_NBA = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("stadium")

app = Flask(__name__)
lock = threading.Lock()
state = {
    "seq": 0,             # bumps on every event; ESP32 acts when this changes
    "event": "none",      # none | game | score | period | status
    "game_id": "",
    "status": 0,          # 0 none, 1 pregame, 2 live, 3 final
    "status_text": "No game",
    "period": 0,
    "clock": "",
    "home_tri": "",
    "home_score": 0,
    "home_delta": 0,      # points home scored in this event (score events only)
    "away_tri": "",
    "away_score": 0,
    "away_delta": 0,
    "ok": False,          # last scoreboard fetch succeeded
    "error": "",          # why the last fetch failed, if it did
    "updated": 0,         # unix time of last successful fetch
}
games_today = []
poller_pid = None  # process the poll thread is running in


def tricode(competitor):
    abbr = competitor["team"]["abbreviation"]
    return ESPN_TO_NBA.get(abbr, abbr)


def normalize(event):
    """Flatten one ESPN scoreboard event into just the fields we use."""
    status = event["status"]
    teams = {c["homeAway"]: c for c in event["competitions"][0]["competitors"]}
    home, away = teams["home"], teams["away"]
    live = status["type"]["state"] == "in"
    return {
        "game_id": event["id"],
        "status": ESPN_STATES.get(status["type"]["state"], 0),
        "status_text": status["type"]["shortDetail"],
        "period": status["period"],
        "clock": status["displayClock"] if live else "",
        "home_tri": tricode(home),
        "home_score": int(home.get("score") or 0),
        "away_tri": tricode(away),
        "away_score": int(away.get("score") or 0),
    }


def pick_game(games, current_id):
    if TEAM:
        return next((g for g in games if TEAM in (g["home_tri"], g["away_tri"])), None)

    live = [g for g in games if g["status"] == LIVE]
    if live:
        # Stay on the game we're already showing rather than hopping around.
        return next((g for g in live if g["game_id"] == current_id), live[0])
    # Nothing live: keep showing the current game's final score if we have one,
    # otherwise show the next game to tip off.
    current = next((g for g in games if g["game_id"] == current_id), None)
    if current:
        return current
    upcoming = [g for g in games if g["status"] == PREGAME]
    return (upcoming or games or [None])[0]


def apply_game(game):
    with lock:
        if game["game_id"] != state["game_id"]:
            event, home_delta, away_delta = "game", 0, 0
        else:
            home_delta = game["home_score"] - state["home_score"]
            away_delta = game["away_score"] - state["away_score"]
            # Status first so the final buzzer isn't masked by a last-second basket.
            if game["status"] != state["status"]:
                event = "status"
            elif home_delta or away_delta:
                event = "score"
            elif game["period"] != state["period"]:
                event = "period"
            else:
                event = None

        if event:
            state["seq"] += 1
            state["event"] = event
            state["home_delta"] = home_delta
            state["away_delta"] = away_delta
            log.info("%s: %s %d - %d %s (%s)", event, game["away_tri"], game["away_score"],
                     game["home_score"], game["home_tri"], game["status_text"])

        state.update(game)


def poll_once():
    global games_today
    resp = requests.get(SCOREBOARD_URL, timeout=10)
    resp.raise_for_status()
    games = [normalize(e) for e in resp.json().get("events", [])]
    games_today = games
    game = pick_game(games, state["game_id"])
    if game:
        apply_game(game)
    with lock:
        state["ok"] = True
        state["error"] = ""
        state["updated"] = int(time.time())


def poll_forever():
    while True:
        try:
            poll_once()
            delay = LIVE_POLL_SECONDS if state["status"] == LIVE else IDLE_POLL_SECONDS
        except Exception as e:
            log.exception("Scoreboard fetch failed")
            with lock:
                state["ok"] = False
                state["error"] = f"{type(e).__name__}: {e}"[:200]
            delay = ERROR_POLL_SECONDS
        time.sleep(delay)


@app.before_request
def ensure_poller():
    """Start the poll thread in whichever process is serving requests.

    Starting it at import breaks under gunicorn --preload (or several workers):
    the app is imported in the master, the thread stays there, and the forked
    worker serving /score keeps the empty state forever.
    """
    global poller_pid
    if poller_pid == os.getpid():
        return
    with lock:
        if poller_pid != os.getpid():
            poller_pid = os.getpid()
            threading.Thread(target=poll_forever, daemon=True).start()
            log.info("Poller started in pid %d", poller_pid)


@app.get("/")
def health():
    return "ok"


@app.get("/score")
def score():
    with lock:
        return jsonify(state)


@app.get("/games")
def games():
    """Today's games, handy for finding tricodes / checking what's on."""
    return jsonify([
        {
            "game_id": g["game_id"],
            "status": g["status"],
            "status_text": g["status_text"],
            "away": f'{g["away_tri"]} {g["away_score"]}',
            "home": f'{g["home_tri"]} {g["home_score"]}',
        }
        for g in games_today
    ])


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
