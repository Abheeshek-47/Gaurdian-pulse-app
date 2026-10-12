"""Fire a fake alert at the webhook, exactly as the wearer app would.

Usage:
  python send_test_alert.py http://localhost:8000 YOUR_WEBHOOK_SECRET [help]
"""

import json
import sys
import urllib.request
import uuid
from datetime import datetime, timezone

url, secret = sys.argv[1].rstrip("/"), sys.argv[2]
help_requested = len(sys.argv) > 3 and sys.argv[3] == "help"

alert = {
    "event_id": str(uuid.uuid4()),
    "wearer_name": "Test Wearer",
    "wearer_phone": "+911234567890",
    "reason": "test",
    "outcome": "help_requested" if help_requested else "no_response",
    "transcript": "help me" if help_requested else "",
    "motion": {"peak_g": 3.2, "orientation": 0.41, "stillness": 0.83, "score": 0.71},
    # UPES Bidholi campus, Dehradun: shows up on the caretaker's map
    "location": {"lat": 30.4159, "lng": 77.9670, "accuracy": 15},
    "occurred_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
}
req = urllib.request.Request(
    f"{url}/webhook/alert",
    data=json.dumps(alert).encode(),
    headers={"Content-Type": "application/json", "X-Webhook-Secret": secret},
    method="POST",
)
with urllib.request.urlopen(req, timeout=15) as r:
    print(r.status, r.read().decode())
