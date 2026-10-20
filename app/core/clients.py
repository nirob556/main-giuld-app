# -*- coding: utf-8 -*-
# app/core/clients.py — client (squad/server) CRUD, persisted to clients.json.
# Every client is owned by a user (owner_id); admins can see/touch all of them,
# regular users only their own — enforced in app/api/clients.py, this module
# just exposes owner-aware list/get helpers so the route layer stays thin.
import json
import os
import time
import uuid
from typing import Any, Dict, List, Optional

CLIENTS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "clients.json")


def _load() -> List[Dict[str, Any]]:
    if not os.path.exists(CLIENTS_FILE):
        return []
    try:
        with open(CLIENTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(clients: List[Dict[str, Any]]) -> None:
    with open(CLIENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(clients, f, indent=2, ensure_ascii=False)


def list_clients() -> List[Dict[str, Any]]:
    return _load()


def list_clients_for_owner(owner_id: str) -> List[Dict[str, Any]]:
    return [c for c in _load() if c.get("owner_id") == owner_id]


def count_clients_for_owner(owner_id: str) -> int:
    return sum(1 for c in _load() if c.get("owner_id") == owner_id)


def get_client(client_id: str) -> Optional[Dict[str, Any]]:
    for c in _load():
        if c["id"] == client_id:
            return c
    return None


def create_client(owner_id: str, name: str, mode: str, captain_uid: str = "", clan_id: str = "",
                   bot_count: Optional[int] = None, name_prefix: str = "FYT_",
                   notes: str = "") -> Dict[str, Any]:
    mode = mode if mode in ("with_me", "normal") else "normal"
    if bot_count is None:
        bot_count = 3 if mode == "with_me" else 4
    bot_count = max(1, min(4, int(bot_count)))
    client = {
        "id": uuid.uuid4().hex[:12],
        "owner_id": owner_id,
        "name": name.strip() or f"Squad-{uuid.uuid4().hex[:6]}",
        "mode": mode,
        "captain_uid": captain_uid.strip(),
        "clan_id": clan_id.strip(),
        "bot_count": bot_count,
        "name_prefix": name_prefix.strip() or "FYT_",
        "notes": notes.strip(),
        "created_at": time.time(),
        "running": False,
    }
    clients = _load()
    clients.append(client)
    _save(clients)
    return client


def update_client(client_id: str, **fields) -> Optional[Dict[str, Any]]:
    clients = _load()
    for c in clients:
        if c["id"] == client_id:
            for k, v in fields.items():
                if v is not None:
                    c[k] = v
            _save(clients)
            return c
    return None


def set_running(client_id: str, running: bool) -> None:
    clients = _load()
    for c in clients:
        if c["id"] == client_id:
            c["running"] = running
            _save(clients)
            return


def delete_client(client_id: str) -> bool:
    clients = _load()
    new = [c for c in clients if c["id"] != client_id]
    if len(new) == len(clients):
        return False
    _save(new)
    return True
