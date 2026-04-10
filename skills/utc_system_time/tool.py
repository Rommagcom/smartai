from __future__ import annotations

import json
from datetime import UTC, datetime


def utc_system_time() -> str:
    now = datetime.now(UTC)
    payload = {
        "timezone": "UTC",
        "iso": now.isoformat(),
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M:%S"),
        "unix": int(now.timestamp()),
    }
    return json.dumps(payload, ensure_ascii=True)
