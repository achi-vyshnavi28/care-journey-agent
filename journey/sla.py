"""Decision deadlines from the CMS Interoperability and Prior Authorization Final Rule (CMS-0057-F), in force for
impacted payers from January 2026: 72 hours for expedited (urgent) requests, 7 calendar days for standard requests.

A journey is at risk once less than a quarter of its window remains, and breached when the deadline has passed.
"""
from datetime import datetime, timedelta, timezone
from typing import Literal

WINDOW = {"expedited": timedelta(hours=72), "standard": timedelta(days=7)}
AT_RISK_FRACTION = 0.25
Status = Literal["on_track", "at_risk", "breached", "closed"]


def due_at(received_at: datetime, urgency: str) -> datetime:
    return received_at + WINDOW[urgency]


def status(received_at: datetime, urgency: str, now: datetime | None = None, closed: bool = False) -> dict:
    now = now or datetime.now(timezone.utc)
    due = due_at(received_at, urgency)
    left = due - now
    if closed:
        state: Status = "closed"
    elif left <= timedelta(0):
        state = "breached"
    elif left < WINDOW[urgency] * AT_RISK_FRACTION:
        state = "at_risk"
    else:
        state = "on_track"
    return {"urgency": urgency, "due_at": due.isoformat(), "hours_left": round(left.total_seconds() / 3600, 1),
            "status": state, "rule": "CMS-0057-F: 72h expedited / 7 calendar days standard"}
