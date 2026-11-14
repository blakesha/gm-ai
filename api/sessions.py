"""Recorded GM chat sessions.

Each conversation with the GM agent is saved on the server (one JSON file per
session under CHAT_DIR) so it can be listed, reopened, continued, downloaded
or deleted. The UI starts a session, sends its id with every /chat request, and
main.agent_chat records each exchange through record_turn().
"""
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

CHAT_DIR = Path(os.getenv("CHAT_DIR", "/data/chats"))
ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
DEFAULT_TITLE = "New session"

router = APIRouter(prefix="/sessions", tags=["sessions"])


def _path(session_id: str) -> Path:
    if not ID_RE.match(session_id or ""):
        raise HTTPException(400, "invalid session id")
    return CHAT_DIR / f"{session_id}.json"


def _read(session_id: str) -> dict:
    p = _path(session_id)
    if not p.exists():
        raise HTTPException(404, "session not found")
    return json.loads(p.read_text(encoding="utf-8"))


def _write(session: dict) -> None:
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    p = _path(session["id"])
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(session, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)  # atomic


class NewSession(BaseModel):
    title: Optional[str] = None
    game_system: str = "AD&D 2nd Edition"


class Rename(BaseModel):
    title: str


@router.get("")
def list_sessions():
    CHAT_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for p in CHAT_DIR.glob("*.json"):
        try:
            s = json.loads(p.read_text(encoding="utf-8"))
            out.append({"id": s["id"], "title": s.get("title") or DEFAULT_TITLE, "updated": s.get("updated", 0),
                        "game_system": s.get("game_system", ""), "messages": len(s.get("messages", []))})
        except Exception:
            continue
    out.sort(key=lambda s: s["updated"], reverse=True)
    return {"sessions": out}


@router.post("")
def create_session(body: NewSession):
    now = time.time()
    sid = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + secrets.token_hex(2)
    session = {"id": sid, "title": (body.title or "").strip() or DEFAULT_TITLE, "game_system": body.game_system,
               "created": now, "updated": now, "messages": []}
    _write(session)
    return session


@router.get("/{session_id}")
def get_session(session_id: str):
    return _read(session_id)


@router.put("/{session_id}")
def rename_session(session_id: str, body: Rename):
    session = _read(session_id)
    session["title"] = body.title.strip() or DEFAULT_TITLE
    _write(session)
    return {"id": session_id, "title": session["title"]}


@router.delete("/{session_id}")
def delete_session(session_id: str):
    p = _path(session_id)
    if not p.exists():
        raise HTTPException(404, "session not found")
    p.unlink()
    try:
        import hooks
        from kg import KnowledgeGraphManager
        hooks.clear_session(KnowledgeGraphManager(), session_id)  # drop its hook resolutions
    except Exception:
        pass
    return {"deleted": session_id}


def title_of(session_id: str) -> str:
    try:
        return _read(session_id).get("title") or ""
    except Exception:
        return ""


def record_turn(session_id: Optional[str], user_text: str, result: dict) -> None:
    """Append one exchange (the player's message and the GM's reply) to a session.
    Never raises: losing a transcript line must not break the chat."""
    if not session_id or not ID_RE.match(session_id):
        return
    try:
        session = _read(session_id)
        reply = result.get("message", {}).get("content") or ""
        session["messages"].append({"role": "user", "content": user_text, "time": time.time()})
        entry = {"role": "assistant", "content": reply, "time": time.time()}
        if result.get("grounded"):
            entry["grounded"] = result["grounded"]      # text before narration
            entry["narrated_by"] = result.get("narrated_by")
        session["messages"].append(entry)
        if session.get("title", DEFAULT_TITLE) == DEFAULT_TITLE and user_text.strip():
            session["title"] = " ".join(user_text.split())[:60]
        session["updated"] = time.time()
        _write(session)
    except Exception as exc:
        print(f"sessions: could not record turn for {session_id}: {exc}")
