"""
Stadium score server.

Polls the NBA live scoreboard (nba_api) in a background thread and serves the
current game as small flat JSON at /score for the ESP32 to poll.

Every time something worth reacting to happens (a basket, a new quarter, the
game starting/ending), `seq` goes up and `event` says what it was. The ESP32
only has to remember the last `seq` it saw and act when it changes.
"""

import logging
import os
import re
import threading
import time

from flask import Flask, jsonify
from nba_api.live.nba.endpoints import scoreboard

# Team to follow by tricode, e.g. "LAL". Blank = follow the first live game.
TEAM = os.environ.get("TEAM", "").strip().upper()
LIVE_POLL_SECONDS = float(os.environ.get("LIVE_POLL_SECONDS", 5))
IDLE_POLL_SECONDS = float(os.environ.get("IDLE_POLL_SECONDS", 60))

# NBA gameStatus values
PREGAME, LIVE, FINAL = 1, 2, 3

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
    "ok": False,          # last NBA fetch succeeded
    "updated": 0,         # unix time of last successful fetch
}
games_today = []


def format_clock(iso_clock):
    """'PT05M32.00S' -> '5:32'. Empty string between periods / when not live."""
    m = re.match(r"PT(\d+)M(\d+)", iso_clock or "")
    if not m:
        return ""
    return f"{int(m.group(1))}:{m.group(2)}"


def pick_game(games, current_id):
    if TEAM:
        for g in games:
            if TEAM in (g["homeTeam"]["teamTricode"], g["awayTeam"]["teamTricode"]):
                return g
        return None

    live = [g for g in games if g["gameStatus"] == LIVE]
    if live:
        # Stay on the game we're already showing rather than hopping around.
        return next((g for g in live if g["gameId"] == current_id), live[0])
    # Nothing live: keep showing the current game's final score if we have one,
    # otherwise show the next game to tip off.
    current = next((g for g in games if g["gameId"] == current_id), None)
    if current:
        return current
    upcoming = [g for g in games if g["gameStatus"] == PREGAME]
    return (upcoming or games or [None])[0]


def apply_game(game):
    home, away = game["homeTeam"], game["awayTeam"]
    with lock:
        if game["gameId"] != state["game_id"]:
            event, home_delta, away_delta = "game", 0, 0
        else:
            home_delta = home["score"] - state["home_score"]
            away_delta = away["score"] - state["away_score"]
            if home_delta or away_delta:
                event = "score"
            elif game["gameStatus"] != state["status"]:
                event = "status"
            elif game["period"] != state["period"]:
                event = "period"
            else:
                event = None

        if event:
            state["seq"] += 1
            state["event"] = event
            state["home_delta"] = home_delta
            state["away_delta"] = away_delta
            log.info("%s: %s %d - %d %s (%s)", event, away["teamTricode"], away["score"],
                     home["score"], home["teamTricode"], game["gameStatusText"].strip())

        state.update(
            game_id=game["gameId"],
            status=game["gameStatus"],
            status_text=game["gameStatusText"].strip(),
            period=game["period"],
            clock=format_clock(game["gameClock"]),
            home_tri=home["teamTricode"],
            home_score=home["score"],
            away_tri=away["teamTricode"],
            away_score=away["score"],
        )


def poll_once():
    global games_today
    games = scoreboard.ScoreBoard(timeout=10).get_dict()["scoreboard"]["games"]
    games_today = games
    game = pick_game(games, state["game_id"])
    if game:
        apply_game(game)
    with lock:
        state["ok"] = True
        state["updated"] = int(time.time())


def poll_forever():
    while True:
        try:
            poll_once()
        except Exception:
            log.exception("NBA fetch failed")
            with lock:
                state["ok"] = False
        time.sleep(LIVE_POLL_SECONDS if state["status"] == LIVE else IDLE_POLL_SECONDS)


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
            "game_id": g["gameId"],
            "status": g["gameStatus"],
            "status_text": g["gameStatusText"].strip(),
            "away": f'{g["awayTeam"]["teamTricode"]} {g["awayTeam"]["score"]}',
            "home": f'{g["homeTeam"]["teamTricode"]} {g["homeTeam"]["score"]}',
        }
        for g in games_today
    ])


# Started at import so it also runs under gunicorn (use a single worker).
threading.Thread(target=poll_forever, daemon=True).start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
