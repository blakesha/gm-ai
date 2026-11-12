"""Optional time window for background ingestion.

Ingestion keeps the GPU busy for hours, so it can be restricted to a window
(e.g. 22:00-06:00 on weeknights) so it doesn't compete with game sessions.

* Outside the window the worker starts no new job.
* A job that is running when the window closes pauses at the next chunk
  boundary, goes back to the queue with its checkpoint intact, and resumes
  from that chunk when the window reopens. At most one chunk of work overruns.

The configuration lives in the jobs database (so it survives restarts) and is
edited from the UI via GET/PUT /ingest/window. Manual single-chunk ingestion
(/ingest) is interactive and is never gated.
"""
import json
import os
import re
import time
from datetime import datetime, time as dtime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

SETTING_KEY = "ingest_window"
DEFAULT = {
    "enabled": False,
    "start": "22:00",
    "end": "06:00",
    "days": [0, 1, 2, 3, 4, 5, 6],   # Monday=0; the day the window OPENS (an overnight window belongs to its start day)
    "timezone": os.getenv("INGEST_TZ") or os.getenv("TZ") or "UTC",
}
_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CACHE_SECONDS = 5


def _parse_time(value: str) -> dtime:
    m = _HHMM.match(str(value).strip())
    if not m:
        raise ValueError(f"'{value}' is not a time like 22:00")
    return dtime(int(m.group(1)), int(m.group(2)))


def validate(cfg: dict) -> dict:
    """Return a clean copy of `cfg` or raise ValueError."""
    out = {**DEFAULT, **{k: v for k, v in cfg.items() if k in DEFAULT}}
    out["enabled"] = bool(out["enabled"])
    out["start"] = _parse_time(out["start"]).strftime("%H:%M")
    out["end"] = _parse_time(out["end"]).strftime("%H:%M")
    days = sorted({int(d) for d in out["days"]})
    if any(d < 0 or d > 6 for d in days):
        raise ValueError("days must be 0 (Monday) to 6 (Sunday)")
    if not days:
        raise ValueError("choose at least one day")
    out["days"] = days
    try:
        ZoneInfo(out["timezone"])
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise ValueError(f"unknown timezone '{out['timezone']}' (use e.g. Australia/Perth)")
    return out


def _intervals(cfg: dict, now_local: datetime) -> list:
    """Open intervals (aware datetimes) around `now_local`, merged."""
    tz = now_local.tzinfo
    start, end = _parse_time(cfg["start"]), _parse_time(cfg["end"])
    spans = []
    for offset in range(-2, 10):
        day = (now_local + timedelta(days=offset)).date()
        if day.weekday() not in cfg["days"]:
            continue
        begin = datetime.combine(day, start, tzinfo=tz)
        if start == end:
            finish = begin + timedelta(days=1)            # same start and end: open all day
        elif start < end:
            finish = datetime.combine(day, end, tzinfo=tz)
        else:
            finish = datetime.combine(day + timedelta(days=1), end, tzinfo=tz)   # overnight
        spans.append((begin, finish))
    spans.sort()
    merged = []
    for b, f in spans:
        if merged and b <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], f))
        else:
            merged.append((b, f))
    return merged


def evaluate(cfg: dict, now: Optional[datetime] = None) -> dict:
    """{"enabled", "open", "next_change" (iso, local) or None, "now" (iso, local)}"""
    cfg = validate(cfg)
    tz = ZoneInfo(cfg["timezone"])
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    if not cfg["enabled"]:
        return {"enabled": False, "open": True, "next_change": None, "now": now_local.isoformat()}
    for begin, finish in _intervals(cfg, now_local):
        if begin <= now_local < finish:
            effectively_always = finish - now_local > timedelta(days=7)   # e.g. start == end on every day
            return {"enabled": True, "open": True, "next_change": None if effectively_always else finish.isoformat(),
                    "now": now_local.isoformat()}
        if begin > now_local:
            return {"enabled": True, "open": False, "next_change": begin.isoformat(), "now": now_local.isoformat()}
    return {"enabled": True, "open": False, "next_change": None, "now": now_local.isoformat()}


class IngestWindow:
    """Store-backed window with a short cache so per-chunk checks stay cheap."""

    def __init__(self, store):
        self.store = store
        self._cache = (0.0, None)

    def config(self) -> dict:
        raw = self.store.get_setting(SETTING_KEY)
        if not raw:
            return dict(DEFAULT)
        try:
            return validate(json.loads(raw))
        except Exception:
            return dict(DEFAULT)

    def set(self, cfg: dict) -> dict:
        clean = validate(cfg)
        self.store.set_setting(SETTING_KEY, json.dumps(clean))
        self._cache = (0.0, None)
        return clean

    def status(self) -> dict:
        cfg = self.config()
        return {"config": cfg, **evaluate(cfg)}

    def is_open(self) -> bool:
        stamp, value = self._cache
        if value is None or time.time() - stamp > CACHE_SECONDS:
            value = evaluate(self.config())["open"]
            self._cache = (time.time(), value)
        return value


class WindowUpdate(BaseModel):
    enabled: bool
    start: str = "22:00"
    end: str = "06:00"
    days: List[int] = [0, 1, 2, 3, 4, 5, 6]
    timezone: str = DEFAULT["timezone"]


def make_router(window: IngestWindow) -> APIRouter:
    router = APIRouter(prefix="/ingest", tags=["ingestion"])

    @router.get("/window")
    def get_window():
        return window.status()

    @router.put("/window")
    def put_window(body: WindowUpdate):
        try:
            window.set(body.model_dump())
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        return window.status()

    return router
