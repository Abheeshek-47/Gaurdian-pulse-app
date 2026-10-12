"""GuardianPulse caretaker webhook server.

The wearer's phone POSTs an alert to /webhook/alert when the wearer does not answer
"I'm OK" (or asks for help). The server stores it and sends a Firebase push to every
registered caretaker phone. The caretaker app acknowledges with /alerts/{id}/ack, and
the wearer's phone can read that back from /webhook/alert/{id}/status.

Run locally:  uvicorn main:app --host 0.0.0.0 --port 8000
"""

import json
import logging
import os
import sqlite3
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Literal, Optional

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("guardianpulse")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# ------------------------------------------------------------------ config
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me-wearer")
CARETAKER_KEY = os.environ.get("CARETAKER_KEY", "change-me-caretaker")
DB_PATH = os.environ.get("DB_PATH", "guardian.db")
# Either a path to the service-account JSON file, or the JSON itself (handy on Render).
FIREBASE_CREDENTIALS = os.environ.get("FIREBASE_CREDENTIALS", "")
FIREBASE_CREDENTIALS_JSON = os.environ.get("FIREBASE_CREDENTIALS_JSON", "")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ------------------------------------------------------------------ firebase
_messaging = None


def init_firebase() -> None:
    """Enable push if credentials are present; otherwise run in log-only mode."""
    global _messaging
    if not (FIREBASE_CREDENTIALS or FIREBASE_CREDENTIALS_JSON):
        log.warning("No Firebase credentials set: running in LOG-ONLY mode (no pushes sent)")
        return
    import firebase_admin
    from firebase_admin import credentials, messaging

    if FIREBASE_CREDENTIALS_JSON:
        cred = credentials.Certificate(json.loads(FIREBASE_CREDENTIALS_JSON))
    else:
        cred = credentials.Certificate(FIREBASE_CREDENTIALS)
    firebase_admin.initialize_app(cred)
    _messaging = messaging
    log.info("Firebase push enabled")


# ------------------------------------------------------------------ database
SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id            TEXT PRIMARY KEY,
    received_at   TEXT NOT NULL,
    occurred_at   TEXT,
    wearer_name   TEXT NOT NULL,
    reason        TEXT NOT NULL,
    outcome       TEXT NOT NULL,
    transcript    TEXT,
    payload       TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open',
    ack_by        TEXT,
    ack_at        TEXT,
    pushed_to     INTEGER NOT NULL DEFAULT 0
);
-- Live location trail sent by the wearer phone while an alert is active.
CREATE TABLE IF NOT EXISTS locations (
    alert_id      TEXT NOT NULL,
    lat           REAL NOT NULL,
    lng           REAL NOT NULL,
    accuracy      REAL,
    at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS locations_by_alert ON locations (alert_id, at);
CREATE TABLE IF NOT EXISTS devices (
    token         TEXT PRIMARY KEY,
    name          TEXT,
    registered_at TEXT NOT NULL
);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with db() as conn:
        conn.executescript(SCHEMA)


# ------------------------------------------------------------------ models
class Motion(BaseModel):
    peak_g: Optional[float] = None
    orientation: Optional[float] = None
    stillness: Optional[float] = None
    score: Optional[float] = None


class Location(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy: Optional[float] = None  # metres, as reported by the phone


class AlertIn(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    wearer_name: str = "Wearer"
    wearer_phone: Optional[str] = None
    reason: Literal["fall", "manual", "test"] = "fall"
    # help_requested = wearer said "help" or pressed Request Help;
    # no_response    = nobody answered "I'm OK" within 30 s.
    outcome: Literal["help_requested", "no_response"] = "no_response"
    transcript: Optional[str] = None
    motion: Optional[Motion] = None
    location: Optional[Location] = None
    occurred_at: Optional[str] = None


class DeviceIn(BaseModel):
    token: str
    name: Optional[str] = None


class AckIn(BaseModel):
    by: str = "Caretaker"


# ------------------------------------------------------------------ auth
def wearer_auth(x_webhook_secret: str = Header(default="")) -> None:
    if x_webhook_secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="bad webhook secret")


def caretaker_auth(x_caretaker_key: str = Header(default="")) -> None:
    if x_caretaker_key != CARETAKER_KEY:
        raise HTTPException(status_code=401, detail="bad caretaker key")


# ------------------------------------------------------------------ helpers
def save_location(conn: sqlite3.Connection, alert_id: str, loc: Location) -> None:
    conn.execute(
        "INSERT INTO locations (alert_id, lat, lng, accuracy, at) VALUES (?, ?, ?, ?, ?)",
        (alert_id, loc.lat, loc.lng, loc.accuracy, now_iso()),
    )


def latest_location(conn: sqlite3.Connection, alert_id: str) -> Optional[dict]:
    r = conn.execute(
        "SELECT lat, lng, accuracy, at AS updated_at FROM locations"
        " WHERE alert_id = ? ORDER BY rowid DESC LIMIT 1",
        (alert_id,),
    ).fetchone()
    return dict(r) if r else None


def row_to_alert(r: sqlite3.Row, conn: sqlite3.Connection) -> dict:
    out = json.loads(r["payload"])
    out.update(
        id=r["id"],
        received_at=r["received_at"],
        status=r["status"],
        ack_by=r["ack_by"],
        ack_at=r["ack_at"],
        pushed_to=r["pushed_to"],
        location=latest_location(conn, r["id"]),
    )
    return out


def push_title_body(a: AlertIn) -> tuple[str, str]:
    test = " (TEST)" if a.reason == "test" else ""
    if a.outcome == "help_requested":
        title = f"{a.wearer_name} asked for help{test}"
    elif a.reason == "fall":
        title = f"Possible fall: {a.wearer_name} did not respond{test}"
    else:
        title = f"{a.wearer_name} did not respond{test}"
    body = f'Heard: "{a.transcript}"' if a.transcript else "No answer to the check-in."
    return title, body


def send_push(alert_id: str, a: AlertIn) -> int:
    """Send a data-only, high-priority push so the app can show a full-screen alarm.

    Returns the number of phones it reached (0 in log-only mode)."""
    title, body = push_title_body(a)
    with db() as conn:
        tokens = [r["token"] for r in conn.execute("SELECT token FROM devices")]
    if _messaging is None:
        log.info("LOG-ONLY push to %d device(s): %s | %s", len(tokens), title, body)
        return 0
    if not tokens:
        log.warning("Alert %s received but no caretaker phone is registered", alert_id)
        return 0

    data = {
        "type": "alert",
        "alert_id": alert_id,
        "title": title,
        "body": body,
        "wearer_name": a.wearer_name,
        "wearer_phone": a.wearer_phone or "",
        "outcome": a.outcome,
        "reason": a.reason,
        "lat": "" if a.location is None else str(a.location.lat),
        "lng": "" if a.location is None else str(a.location.lng),
    }
    msg = _messaging.MulticastMessage(
        tokens=tokens,
        data=data,
        android=_messaging.AndroidConfig(priority="high", ttl=600),
    )
    resp = _messaging.send_each_for_multicast(msg)

    # Forget tokens Firebase says are gone (app uninstalled or data cleared).
    dead = [
        tokens[i]
        for i, r in enumerate(resp.responses)
        if not r.success and isinstance(r.exception, _messaging.UnregisteredError)
    ]
    if dead:
        with db() as conn:
            conn.executemany("DELETE FROM devices WHERE token = ?", [(t,) for t in dead])
    log.info("Push for %s: %d sent, %d failed", alert_id, resp.success_count, resp.failure_count)
    return resp.success_count


# ------------------------------------------------------------------ app
@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    init_firebase()
    yield


app = FastAPI(title="GuardianPulse caretaker webhook", lifespan=lifespan)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "push_enabled": _messaging is not None, "time": now_iso()}


# ---- called by the wearer's phone
@app.post("/webhook/alert", dependencies=[Depends(wearer_auth)])
def receive_alert(a: AlertIn) -> dict:
    return store_and_push(a)


def store_and_push(a: AlertIn) -> dict:
    with db() as conn:
        existing = conn.execute("SELECT * FROM alerts WHERE id = ?", (a.event_id,)).fetchone()
        if existing:  # phone retried: do not push twice
            return {"id": a.event_id, "duplicate": True, "pushed_to": existing["pushed_to"]}
        conn.execute(
            "INSERT INTO alerts (id, received_at, occurred_at, wearer_name, reason, outcome,"
            " transcript, payload) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                a.event_id,
                now_iso(),
                a.occurred_at,
                a.wearer_name,
                a.reason,
                a.outcome,
                a.transcript,
                a.model_dump_json(),
            ),
        )
        if a.location is not None:
            save_location(conn, a.event_id, a.location)
    log.info("Alert %s from %s: %s / %s", a.event_id, a.wearer_name, a.reason, a.outcome)

    pushed = send_push(a.event_id, a)
    with db() as conn:
        conn.execute("UPDATE alerts SET pushed_to = ? WHERE id = ?", (pushed, a.event_id))
    return {"id": a.event_id, "duplicate": False, "pushed_to": pushed}


@app.get("/webhook/alert/{alert_id}/status", dependencies=[Depends(wearer_auth)])
def alert_status(alert_id: str) -> dict:
    """Lets the wearer's phone show "Help is coming" once a caretaker acknowledges."""
    with db() as conn:
        r = conn.execute(
            "SELECT status, ack_by, ack_at FROM alerts WHERE id = ?", (alert_id,)
        ).fetchone()
    if not r:
        raise HTTPException(status_code=404, detail="unknown alert")
    return dict(r)


@app.post("/webhook/alert/{alert_id}/location", dependencies=[Depends(wearer_auth)])
def update_location(alert_id: str, loc: Location) -> dict:
    """Live location updates from the wearer phone while the alert is active."""
    with db() as conn:
        if not conn.execute("SELECT 1 FROM alerts WHERE id = ?", (alert_id,)).fetchone():
            raise HTTPException(status_code=404, detail="unknown alert")
        save_location(conn, alert_id, loc)
    return {"ok": True}


# ---- called by the caretaker app
@app.post("/devices/register", dependencies=[Depends(caretaker_auth)])
def register_device(d: DeviceIn) -> dict:
    with db() as conn:
        conn.execute(
            "INSERT INTO devices (token, name, registered_at) VALUES (?, ?, ?)"
            " ON CONFLICT(token) DO UPDATE SET name = excluded.name",
            (d.token, d.name, now_iso()),
        )
        count = conn.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
    log.info("Registered caretaker device %s (%d total)", d.name or "?", count)
    return {"ok": True, "devices": count}


@app.post("/devices/test", dependencies=[Depends(caretaker_auth)])
def test_push(d: AckIn) -> dict:
    """"Send test alert" button in the caretaker app: a fake alert through the real path."""
    return store_and_push(
        AlertIn(wearer_name="Test Wearer", reason="test", transcript=f"test sent by {d.by}")
    )


@app.get("/alerts", dependencies=[Depends(caretaker_auth)])
def list_alerts(limit: int = 50) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY received_at DESC LIMIT ?", (min(limit, 200),)
        ).fetchall()
        return [row_to_alert(r, conn) for r in rows]


@app.get("/alerts/{alert_id}", dependencies=[Depends(caretaker_auth)])
def get_alert(alert_id: str) -> dict:
    with db() as conn:
        r = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="unknown alert")
        return row_to_alert(r, conn)


@app.post("/alerts/{alert_id}/ack", dependencies=[Depends(caretaker_auth)])
def ack_alert(alert_id: str, body: AckIn) -> dict:
    with db() as conn:
        r = conn.execute("SELECT status FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        if not r:
            raise HTTPException(status_code=404, detail="unknown alert")
        if r["status"] == "open":
            conn.execute(
                "UPDATE alerts SET status = 'acknowledged', ack_by = ?, ack_at = ? WHERE id = ?",
                (body.by, now_iso(), alert_id),
            )
        r = conn.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        log.info("Alert %s acknowledged by %s", alert_id, r["ack_by"])
        return row_to_alert(r, conn)
