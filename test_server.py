"""End-to-end test of the webhook server in log-only mode (no Firebase needed).

Run:  pytest -q test_server.py
"""

import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["WEBHOOK_SECRET"] = "w-secret"
os.environ["CARETAKER_KEY"] = "c-key"

from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402

W = {"X-Webhook-Secret": "w-secret"}
C = {"X-Caretaker-Key": "c-key"}

ALERT = {
    "event_id": "evt-1",
    "wearer_name": "Asha",
    "reason": "fall",
    "outcome": "no_response",
    "transcript": "uh",
    "motion": {"peak_g": 3.1, "orientation": 0.4, "stillness": 0.8, "score": 0.68},
}


def test_full_flow():
    with TestClient(main.app) as client:
        assert client.get("/health").json()["push_enabled"] is False

        # wrong secrets are rejected
        assert client.post("/webhook/alert", json=ALERT).status_code == 401
        assert client.get("/alerts").status_code == 401

        assert client.post("/devices/register", json={"token": "t1", "name": "Pixel"}, headers=C).json()[
            "devices"
        ] == 1

        r = client.post("/webhook/alert", json=ALERT, headers=W).json()
        assert r == {"id": "evt-1", "duplicate": False, "pushed_to": 0}

        # a retry from the phone is not stored or pushed twice
        assert client.post("/webhook/alert", json=ALERT, headers=W).json()["duplicate"] is True

        alerts = client.get("/alerts", headers=C).json()
        assert len(alerts) == 1 and alerts[0]["status"] == "open"
        assert alerts[0]["motion"]["score"] == 0.68

        assert client.get("/webhook/alert/evt-1/status", headers=W).json()["status"] == "open"
        acked = client.post("/alerts/evt-1/ack", json={"by": "Priya"}, headers=C).json()
        assert acked["status"] == "acknowledged" and acked["ack_by"] == "Priya"
        status = client.get("/webhook/alert/evt-1/status", headers=W).json()
        assert status["status"] == "acknowledged" and status["ack_by"] == "Priya"

        # a second caretaker's ack does not overwrite the first
        assert client.post("/alerts/evt-1/ack", json={"by": "Ramesh"}, headers=C).json()["ack_by"] == "Priya"
        assert client.post("/alerts/nope/ack", json={}, headers=C).status_code == 404

        # the caretaker app's test button goes through the same path
        assert client.post("/devices/test", json={"by": "Priya"}, headers=C).json()["duplicate"] is False
        assert len(client.get("/alerts", headers=C).json()) == 2


def test_titles():
    a = main.AlertIn(wearer_name="Asha", outcome="help_requested", reason="manual")
    assert main.push_title_body(a)[0] == "Asha asked for help"
    a = main.AlertIn(wearer_name="Asha", reason="test", transcript="hmm")
    assert main.push_title_body(a) == ("Asha did not respond (TEST)", 'Heard: "hmm"')
