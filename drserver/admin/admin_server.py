"""Admin HTTP server — in-process web dashboard for server management.

Ported from the C# DR_Server Build/dr_admin.py. Runs an HTTP server using
stdlib http.server in a daemon thread. Serves the dashboard.html SPA and a
REST-ish API that calls server primitives directly.

Unlike the C# version: no process management, no admin_commands bridge table,
and config is managed through config.yaml (read-only from the panel for now).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse, parse_qs

from ..core import log
from ..core.config import ServerConfig
from ..db import game_database as db
from . import primitives

SESSION_HOURS = 12

_sessions: Dict[str, dict] = {}

# Set by start_admin_server.
_server_ref: Optional["GameServer"] = None
_config_ref: Optional[ServerConfig] = None
_start_time: Optional[datetime] = None
_log_lines: list[dict] = []
MAX_LOG = 2000


def _get_db():
    conn = db.get_connection()
    conn.row_factory = db.get_connection().row_factory
    return conn


def _q_all(sql, params=None):
    conn = _get_db()
    return [dict(r) for r in conn.execute(sql, params or {}).fetchall()]


def _q_one(sql, params=None):
    conn = _get_db()
    r = conn.execute(sql, params or {}).fetchone()
    return dict(r) if r else None


def _q_val(sql, params=None, default=0):
    conn = _get_db()
    r = conn.execute(sql, params or {}).fetchone()
    return list(r)[0] if r else default


def _q_exec(sql, params=None):
    conn = _get_db()
    conn.execute(sql, params or {})
    conn.commit()


# ── Auth ──


def _hash_pw(password, salt):
    h = hashlib.sha256((salt + password).encode("utf-8")).digest()
    return base64.b64encode(h).decode("ascii")


def _verify_login(username, password):
    row = _q_one("SELECT id,username,password_hash,salt,is_admin FROM accounts "
                  "WHERE username=:u COLLATE NOCASE", {"u": username})
    if not row:
        return None
    if not row["is_admin"]:
        return None
    stored_hash = row.get("password_hash", "") or ""
    stored_salt = row.get("salt", "") or ""
    if not stored_hash or not stored_salt:
        if not password:
            return None
        salt_bytes = secrets.token_bytes(16)
        new_salt = base64.b64encode(salt_bytes).decode("ascii")
        new_hash = _hash_pw(password, new_salt)
        _q_exec("UPDATE accounts SET password_hash=:h,salt=:s WHERE id=:i",
                {"h": new_hash, "s": new_salt, "i": row["id"]})
        return {"id": row["id"], "username": row["username"], "admin": True}
    if _hash_pw(password, stored_salt) != stored_hash:
        return None
    return {"id": row["id"], "username": row["username"], "admin": True}


def _create_session(user_info):
    token = secrets.token_hex(32)
    _sessions[token] = {**user_info, "expires": datetime.now() + timedelta(hours=SESSION_HOURS)}
    now = datetime.now()
    for k in list(_sessions):
        if _sessions[k]["expires"] < now:
            del _sessions[k]
    return token


def _check_session(cookie_header):
    if not cookie_header:
        return None
    c = SimpleCookie()
    try:
        c.load(cookie_header)
    except Exception:
        return None
    if "dr_session" not in c:
        return None
    token = c["dr_session"].value
    s = _sessions.get(token)
    if not s or s["expires"] < datetime.now():
        if s:
            del _sessions[token]
        return None
    return s


# ── API helpers ──


def _api_status():
    srv = _server_ref
    game_up = srv is not None
    online = 0
    if srv:
        online = sum(1 for c in srv.connections.values() if c.is_spawned)
    cfg = _config_ref
    upt = ""
    if _start_time:
        s = int((datetime.now() - _start_time).total_seconds())
        h, r = divmod(s, 3600)
        m, sec = divmod(r, 60)
        upt = f"{h}h {m}m {sec}s"

    db_path = cfg.database_path if cfg else ""
    db_exists = os.path.exists(db_path) if db_path else False
    db_size = round(os.path.getsize(db_path) / 1048576, 2) if db_exists else 0

    return {
        "running": game_up,
        "uptime": upt,
        "pid": os.getpid(),
        "game_port": cfg.game_server_port if cfg else 2603,
        "auth_port": cfg.auth_server_port if cfg else 2110,
        "game_up": game_up,
        "auth_up": game_up,
        "game_ip": cfg.game_server_ip if cfg else "0.0.0.0",
        "auth_ip": cfg.auth_server_ip if cfg else "0.0.0.0",
        "server_name": cfg.game_server_name if cfg else "Dungeon Runners",
        "db_exists": db_exists,
        "db_size_mb": db_size,
        "api_uptime": str(datetime.now() - _start_time).split(".")[0] if _start_time else "",
        "scheduled": None,
        "connections": online,
        "auth_connections": 0,
    }


def _api_stats():
    try:
        return {
            "accounts": _q_val("SELECT COUNT(*) FROM accounts") or 0,
            "characters": _q_val("SELECT COUNT(*) FROM characters") or 0,
            "members": _q_val("SELECT COUNT(*) FROM accounts WHERE is_member=1") or 0,
            "banned": _q_val("SELECT COUNT(*) FROM accounts WHERE is_banned=1") or 0,
            "admins": _q_val("SELECT COUNT(*) FROM accounts WHERE is_admin=1") or 0,
            "avg_level": _q_val("SELECT ROUND(AVG(level),1) FROM characters") or 0,
            "max_level": _q_val("SELECT MAX(level) FROM characters") or 0,
            "total_gold": _q_val("SELECT SUM(gold) FROM characters") or 0,
            "quests_done": _q_val("SELECT COUNT(*) FROM completed_quests") or 0,
            "items_dropped": 0,
            "zones": _q_all("SELECT current_zone as zone, COUNT(*) as n FROM characters GROUP BY current_zone ORDER BY n DESC"),
            "classes": _q_all("SELECT class_name as cls, COUNT(*) as n FROM characters GROUP BY class_name ORDER BY n DESC"),
            "recent": _q_all("SELECT username, last_login FROM accounts WHERE last_login IS NOT NULL ORDER BY last_login DESC LIMIT 15"),
            "richest": _q_all("SELECT name, gold, level, class_name FROM characters ORDER BY gold DESC LIMIT 10"),
            "highest": _q_all("SELECT name, level, class_name, experience FROM characters ORDER BY level DESC, experience DESC LIMIT 10"),
        }
    except Exception as e:
        return {"accounts": 0, "characters": 0, "error_detail": str(e)}


def _api_online():
    srv = _server_ref
    if not srv:
        return {"online_count": 0, "players": [], "connections": []}
    players = primitives.get_online_players(srv)
    # Enrich with DB data for HP/MP/gold
    for p in players:
        if p.get("char_sql_id"):
            c = _q_one("SELECT * FROM characters WHERE id=:i", {"i": p["char_sql_id"]})
            if c:
                p["current_hp"] = c.get("current_hp", 0)
                p["current_mana"] = c.get("current_mana", 0)
                p["max_hp"] = c.get("max_hp", 0)
                p["max_mana"] = c.get("max_mana", 0)
                p["gold"] = c.get("gold", 0)
                p["experience"] = c.get("experience", 0)
                p["account_name"] = p["login_name"]
                p["current_zone"] = c.get("current_zone", "")
                p["stat_strength"] = c.get("stat_strength", 0)
                p["stat_agility"] = c.get("stat_agility", 0)
                p["stat_intellect"] = c.get("stat_intellect", 0)
                p["stat_endurance"] = c.get("stat_endurance", 0)
                p["is_member"] = _q_val("SELECT is_member FROM accounts WHERE username=:u",
                                         {"u": p["login_name"]}, 0)
                p["is_admin"] = _q_val("SELECT is_admin FROM accounts WHERE username=:u",
                                        {"u": p["login_name"]}, 0)
    conns = []
    if srv:
        for c in srv.connections.values():
            if c.is_spawned:
                conns.append({"ip": str(c.conn_id), "remote": c.login_name or "?"})
    return {"online_count": len(players), "players": players, "connections": conns}


def _api_characters(params):
    pg = int(params.get("page", ["1"])[0])
    pp = int(params.get("per_page", ["50"])[0])
    s = params.get("search", [""])[0]
    sort = params.get("sort", ["level"])[0]
    order = params.get("order", ["desc"])[0]
    ok = {"level", "name", "gold", "experience", "class_name", "current_zone", "id"}
    if sort not in ok:
        sort = "level"
    if order not in ("asc", "desc"):
        order = "desc"
    w_clause = ""
    w_params = {}
    if s:
        w_clause = "WHERE c.name LIKE :s OR a.username LIKE :s2"
        w_params = {"s": f"%{s}%", "s2": f"%{s}%"}
    total = _q_val(f"SELECT COUNT(*) FROM characters c JOIN accounts a ON c.account_id=a.id {w_clause}", w_params)
    try:
        rows = _q_all(f"""SELECT c.id,c.name,c.class_name,c.level,c.experience,c.gold,c.current_zone,
            c.current_hp,c.current_mana,c.max_hp,c.max_mana,c.stat_strength,c.stat_agility,
            c.stat_intellect,c.stat_endurance,c.created_at,
            a.username as account_name,a.is_member,a.is_admin,a.is_banned
            FROM characters c JOIN accounts a ON c.account_id=a.id
            {w_clause} ORDER BY c.{sort} {order} LIMIT :pp OFFSET :off""",
            {**w_params, "pp": pp, "off": (pg - 1) * pp})
    except Exception:
        rows = []
    return {"total": total, "page": pg, "rows": rows}


def _api_char_detail(cid):
    c = _q_one("SELECT c.*,a.username as account_name,a.is_member,a.is_admin,a.is_banned "
               "FROM characters c JOIN accounts a ON c.account_id=a.id WHERE c.id=:i", {"i": cid})
    if not c:
        return {"error": "Not found"}
    c["equipment"] = _q_all("SELECT slot,gc_class,rarity,stored_level FROM character_equipment WHERE character_id=:i", {"i": cid})
    c["inventory"] = _q_all("SELECT gc_class,slot_x,slot_y,count,rarity,stored_level FROM character_inventory WHERE character_id=:i", {"i": cid})
    c["skills"] = _q_all("SELECT skill_gc_class,level,hotbar_slot FROM character_skills WHERE character_id=:i", {"i": cid})
    c["active_quests"] = _q_all("SELECT quest_id,accepted_at,status FROM character_quests WHERE character_id=:i", {"i": cid})
    c["completed_quests"] = _q_all("SELECT quest_id,completed_at FROM completed_quests WHERE character_id=:i", {"i": cid})
    c["checkpoints"] = _q_all("SELECT checkpoint_id FROM character_checkpoints WHERE character_id=:i", {"i": cid})
    return c


def _api_accounts(params):
    pg = int(params.get("page", ["1"])[0])
    pp = int(params.get("per_page", ["50"])[0])
    s = params.get("search", [""])[0]
    w_clause = ""
    w_params = {}
    if s:
        w_clause = "WHERE username LIKE :s OR email LIKE :s2"
        w_params = {"s": f"%{s}%", "s2": f"%{s}%"}
    total = _q_val(f"SELECT COUNT(*) FROM accounts {w_clause}", w_params)
    rows = _q_all(f"SELECT id,username,email,is_member,is_banned,is_admin,created_at,last_login "
                  f"FROM accounts {w_clause} ORDER BY id DESC LIMIT :pp OFFSET :off",
                  {**w_params, "pp": pp, "off": (pg - 1) * pp})
    for r in rows:
        r["char_count"] = _q_val("SELECT COUNT(*) FROM characters WHERE account_id=:i", {"i": r["id"]})
    return {"total": total, "page": pg, "rows": rows}


def _api_update_account(aid, body):
    ok = {"is_member", "is_banned", "is_admin"}
    sets = []
    params = {}
    for k in ok:
        if k in body:
            sets.append(f"{k}=:v_{k}")
            params[f"v_{k}"] = int(body[k])
    if not sets:
        return {"error": "Nothing"}
    params["id"] = aid
    _q_exec(f"UPDATE accounts SET {','.join(sets)} WHERE id=:id", params)
    return {"ok": True}


def _api_update_char(cid, body):
    old = _q_one("SELECT level,gold,experience FROM characters WHERE id=:i", {"i": cid})
    sets = []
    params = {}
    ok = {"level", "experience", "gold", "current_zone", "current_hp", "current_mana"}
    for k in ok:
        if k in body:
            sets.append(f"{k}=:v_{k}")
            params[f"v_{k}"] = body[k]
    if not sets:
        return {"error": "Nothing"}
    params["id"] = cid
    _q_exec(f"UPDATE characters SET {','.join(sets)} WHERE id=:id", params)
    return {"ok": True}


def _api_items(params):
    s = params.get("search", [""])[0]
    pg = int(params.get("page", ["1"])[0])
    pp = int(params.get("per_page", ["30"])[0])
    w_parts = ["i.gc_type NOT LIKE '%.mod%' AND i.gc_type NOT LIKE '%.description%'"]
    w_params = {}
    if s:
        w_parts.append("(i.label LIKE :s OR i.gc_type LIKE :s2)")
        w_params = {"s": f"%{s}%", "s2": f"%{s}%"}
    where = "WHERE " + " AND ".join(w_parts)
    try:
        total = _q_val(f"SELECT COUNT(*) FROM items i {where}", w_params)
        rows = _q_all(f"SELECT i.gc_type, i.label AS name, i.gc_gold_value FROM items i "
                      f"{where} ORDER BY i.label LIMIT :pp OFFSET :off",
                      {**w_params, "pp": pp, "off": (pg - 1) * pp})
        from ..data import item_catalog
        for r in rows:
            gc = r.get("gc_type", "")
            stripped = gc.lower()
            r["gold_value"] = item_catalog.get_buy_price(gc)
            wdata = _q_one("SELECT inventory_icon,weapon_class,damage,slot_type,description FROM weapons WHERE LOWER(gc_type)=:g", {"g": stripped})
            if not wdata:
                wdata = _q_one("SELECT inventory_icon,'' as weapon_class,defense_rating as damage,slot_type,description FROM armor WHERE LOWER(gc_type)=:g", {"g": stripped})
            if wdata:
                r["icon"] = wdata.get("inventory_icon", "")
                r["weapon_class"] = wdata.get("weapon_class", "")
                r["damage"] = wdata.get("damage", 0)
                r["slot_type"] = wdata.get("slot_type", "")
                r["description"] = wdata.get("description", "")
            else:
                r["icon"] = ""; r["weapon_class"] = ""; r["damage"] = 0
                r["slot_type"] = ""; r["description"] = ""
            gcl = gc.lower()
            if "mythic" in gcl: r["rarity"] = "Mythic"
            elif "unique" in gcl: r["rarity"] = "Unique"
            elif "quest" in gcl: r["rarity"] = "Quest"
            else: r["rarity"] = "Normal"
            t = r.get("weapon_class", "") or r.get("slot_type", "")
            if not t:
                for kw, tp in [("sword","Melee"),("axe","Melee"),("mace","Melee"),("dagger","Melee"),
                               ("staff","Magic"),("wand","Magic"),("xbow","Ranged"),("shield","Shield"),
                               ("helm","Helmet"),("boot","Boots"),("glove","Gloves"),("shoulder","Shoulders"),
                               ("armor","Armor"),("chest","Armor"),("ring","Ring"),("amulet","Amulet")]:
                    if kw in gcl: t = tp; break
            r["item_type"] = t
        return {"total": total, "page": pg, "rows": rows}
    except Exception as e:
        return {"total": 0, "page": 1, "rows": [], "error": str(e)}


def _api_send_item(body):
    char_id = body.get("character_id")
    gc_class = body.get("gc_class", "")
    count = int(body.get("count", 1))
    if not char_id or not gc_class:
        return {"error": "Need character_id and gc_class"}
    char = _q_one("SELECT id,name FROM characters WHERE id=:i", {"i": char_id})
    if not char:
        return {"error": "Character not found"}
    from ..data import item_catalog
    gc_clean = item_catalog.normalize_key(gc_class)
    w, h = item_catalog.get_item_size(gc_class)
    rar = 1
    gcl = gc_class.lower()
    if "mythic" in gcl: rar = 5
    elif "unique" in gcl: rar = 4
    elif "rare" in gcl: rar = 3
    elif "superior" in gcl: rar = 2
    _q_exec("""CREATE TABLE IF NOT EXISTS pending_item_grants (
        id INTEGER PRIMARY KEY AUTOINCREMENT, character_id INTEGER NOT NULL,
        gc_class TEXT NOT NULL, count INTEGER DEFAULT 1, width INTEGER DEFAULT 1,
        height INTEGER DEFAULT 1, rarity INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now')))""")
    _q_exec("INSERT INTO pending_item_grants (character_id,gc_class,count,width,height,rarity) VALUES (:c,:g,:n,:w,:h,:r)",
            {"c": char_id, "g": gc_clean, "n": count, "w": w, "h": h, "r": rar})
    return {"ok": True, "character": char["name"], "item": gc_clean, "count": count, "size": f"{w}x{h}"}


def _api_grant_gold(body):
    char_id = body.get("character_id")
    amount = int(body.get("amount", 0))
    if not char_id or not amount:
        return {"error": "Need character_id and amount"}
    char = _q_one("SELECT id,name,gold FROM characters WHERE id=:i", {"i": char_id})
    if not char:
        return {"error": "Character not found"}
    new_gold = max(0, int(char.get("gold", 0) or 0) + amount)
    _q_exec("UPDATE characters SET gold=:g WHERE id=:i", {"g": new_gold, "i": char_id})
    return {"ok": True, "character": char["name"], "old_gold": char["gold"], "new_gold": new_gold, "granted": amount}


def _api_set_level(body):
    char_id = body.get("character_id")
    level = int(body.get("level", 1))
    if not char_id or level < 1 or level > 100:
        return {"error": "Need character_id and level 1-100"}
    char = _q_one("SELECT id,name FROM characters WHERE id=:i", {"i": char_id})
    if not char:
        return {"error": "Character not found"}
    _q_exec("UPDATE characters SET level=:l WHERE id=:i", {"l": level, "i": char_id})
    return {"ok": True, "character": char["name"], "level": level}


def _api_set_xp(body):
    char_id = body.get("character_id")
    xp = int(body.get("xp", 0))
    if not char_id:
        return {"error": "Need character_id"}
    char = _q_one("SELECT id,name FROM characters WHERE id=:i", {"i": char_id})
    if not char:
        return {"error": "Character not found"}
    _q_exec("UPDATE characters SET experience=:x WHERE id=:i", {"x": xp, "i": char_id})
    return {"ok": True, "character": char["name"], "xp": xp}


def _api_send_command(body):
    srv = _server_ref
    cmd = body.get("command", "")
    if not cmd or not srv:
        return {"error": "Server not available"}

    admin_name = _check_session_from_body(body) or "admin"

    if cmd == "broadcast":
        msg = body.get("message", "")
        if not msg:
            return {"error": "No message"}
        count = primitives.broadcast_all(srv, msg, body.get("color", "#FF4444"), body.get("effect", "glow"))
        primitives.log_activity("broadcast", "", msg, admin_name)
        return {"ok": True, "result": f"Sent to {count} players"}

    elif cmd == "whisper":
        msg = body.get("message", "")
        player = body.get("player", "")
        if not msg or not player:
            return {"error": "Need player and message"}
        if primitives.send_admin_tell(srv, player, f"[ADMIN] {msg}"):
            primitives.log_activity("whisper", player, msg, admin_name)
            return {"ok": True, "result": "Sent"}
        return {"error": "Player not found"}

    elif cmd == "kick":
        player = body.get("player", "")
        reason = body.get("reason", "")
        if not player:
            return {"error": "Need player name"}
        if primitives.kick_player(srv, player, reason, admin_name):
            return {"ok": True, "result": f"Kicked {player}"}
        return {"error": "Player not found"}

    elif cmd == "ban":
        player = body.get("player", "")
        reason = body.get("reason", "")
        if not player:
            return {"error": "Need player name"}
        if primitives.ban_player(srv, player, reason, admin_name):
            return {"ok": True, "result": f"Banned {player}"}
        return {"error": "Failed"}

    elif cmd == "unban":
        player = body.get("player", "")
        if not player:
            return {"error": "Need player name"}
        if primitives.unban_player(srv, player, admin_name):
            return {"ok": True, "result": f"Unbanned {player}"}
        return {"error": "Failed"}

    elif cmd == "teleport":
        player = body.get("player", "")
        zone = body.get("zone", "")
        if not player or not zone:
            return {"error": "Need player and zone"}
        if primitives.teleport_player(srv, player, zone, admin_name):
            return {"ok": True, "result": f"Teleported to {zone}"}
        return {"error": "Player not found or invalid zone"}

    elif cmd in ("boost_xp", "boost_gold", "boost_stop", "maintenance"):
        return {"ok": True, "result": "Boosts and maintenance mode not yet implemented in Python server"}

    return {"error": f"Unknown command: {cmd}"}


def _check_session_from_body(body):
    return body.get("_admin_user", "")


def _api_chatlog(params):
    count = int(params.get("count", ["200"])[0])
    search = params.get("search", [""])[0]
    if search:
        rows = _q_all("SELECT * FROM chat_log WHERE message LIKE :s OR sender LIKE :s2 ORDER BY id DESC LIMIT :c",
                      {"s": f"%{search}%", "s2": f"%{search}%", "c": count})
    else:
        rows = _q_all("SELECT * FROM chat_log ORDER BY id DESC LIMIT :c", {"c": count})
    rows.reverse()
    return {"rows": rows, "count": len(rows)}


def _api_activity(params):
    count = int(params.get("count", ["200"])[0])
    event_type = params.get("type", [""])[0]
    if event_type:
        rows = _q_all("SELECT * FROM activity_log WHERE event_type=:t ORDER BY id DESC LIMIT :c",
                      {"t": event_type, "c": count})
    else:
        rows = _q_all("SELECT * FROM activity_log ORDER BY id DESC LIMIT :c", {"c": count})
    rows.reverse()
    return {"rows": rows, "count": len(rows)}


def _api_leaderboard(params):
    limit = int(params.get("limit", ["50"])[0])
    sort = params.get("sort", ["pvp_rating"])[0]
    if sort not in ("pvp_rating", "pvp_wins", "level", "gold"):
        sort = "pvp_rating"
    rows = _q_all(f"""SELECT c.id,c.name,c.class_name,c.level,c.gold,
        c.pvp_wins,c.pvp_rating,a.username as account_name
        FROM characters c LEFT JOIN accounts a ON c.account_id=a.id
        ORDER BY c.{sort} DESC LIMIT :l""", {"l": limit})
    return {"rows": rows, "count": len(rows)}


def _api_bans(params):
    rows = _q_all("SELECT * FROM ban_log ORDER BY id DESC LIMIT 100")
    return {"rows": rows}


def _api_config_read():
    cfg = _config_ref
    if not cfg:
        try:
            from ..core.config import ServerConfig
            cfg = ServerConfig.load("config.yaml")
        except Exception:
            return {"config": {}}
    return {"config": {
        "adminPanelPort": str(cfg.admin_panel_port if cfg else 8080),
        "authIP": cfg.auth_server_ip if cfg else "0.0.0.0",
        "authPort": str(cfg.auth_server_port if cfg else 2110),
        "gameIP": cfg.game_server_ip if cfg else "0.0.0.0",
        "gamePort": str(cfg.game_server_port if cfg else 2603),
        "serverName": cfg.game_server_name if cfg else "Dungeon Runners",
        "maxPlayers": str(cfg.max_players if cfg else 100),
        "enableDebugLog": str(bool(cfg and cfg.enable_debug_logging)).lower(),
    }}


def _api_create_account(body):
    username = body.get("username", "").strip()
    password = body.get("password", "").strip()
    is_member = int(body.get("is_member", 0))
    is_admin = int(body.get("is_admin", 0))
    if not username or not password:
        return {"error": "Username and password required"}
    existing = _q_one("SELECT id FROM accounts WHERE username=:u COLLATE NOCASE", {"u": username})
    if existing:
        return {"error": f"Account '{username}' already exists"}
    # Use same hashing as account_repository
    from ..db import account_repository
    account_repository.create_account(username, password)
    if is_member or is_admin:
        sets = []
        params = {"u": username}
        if is_member:
            sets.append("is_member=1")
        if is_admin:
            sets.append("is_admin=1")
        _q_exec(f"UPDATE accounts SET {','.join(sets)} WHERE username=:u", params)
    return {"ok": True, "username": username}


# ── Login page (embedded) ──

LOGIN_HTML = """<!DOCTYPE html><html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dungeon Runners - Admin Login</title>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;700;900&family=Inter:wght@400;600&display=swap" rel="stylesheet">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d0b0a;color:#d4c4a0;font-family:'Inter',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;
background:radial-gradient(ellipse at center,#1a1612 0%,#0d0b0a 100%)}
.box{background:linear-gradient(180deg,#1a1612ee,#141210ee);border:1px solid #3d2e1e;border-radius:8px;padding:40px;width:380px;text-align:center;position:relative;z-index:1}
.box::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;background:linear-gradient(90deg,transparent,#c9a44a,transparent)}
.title{font:900 28px 'Cinzel',serif;color:#c9a44a;text-shadow:0 0 20px #c9a44a22}
.sub{font:700 12px 'Cinzel',serif;color:#6b5a3a;letter-spacing:3px;margin:4px 0 24px}
input{width:100%;background:#0d0b0a;border:1px solid #3d2e1e;color:#d4c4a0;padding:12px 16px;border-radius:4px;font-size:14px;margin-bottom:14px;outline:none;font-family:'Inter',sans-serif}
input:focus{border-color:#c9a44a;box-shadow:0 0 8px #c9a44a22}
input::placeholder{color:#4a3a28}
.btn{width:100%;background:linear-gradient(180deg,#8b6914,#6b4f0e);border:1px solid #c9a44a;color:#fff;padding:12px;border-radius:4px;font:700 14px 'Cinzel',serif;cursor:pointer;letter-spacing:1px;text-shadow:0 1px 2px rgba(0,0,0,.5)}
.btn:hover{background:linear-gradient(180deg,#c9a44a,#8b6914);color:#0d0b0a}
.btn2{width:100%;background:transparent;border:1px solid #3d2e1e;color:#6b5a3a;padding:10px;border-radius:4px;font-size:12px;cursor:pointer;margin-top:10px;font-family:'Inter',sans-serif}
.btn2:hover{border-color:#c9a44a;color:#c9a44a}
.err{color:#cc2e2e;font-size:13px;margin-bottom:12px;display:none}
.ok{color:#2ecc40;font-size:13px;margin-bottom:12px;display:none}
.note{color:#4a3a28;font-size:11px;margin-top:16px}
#reset-panel{display:none}
</style></head><body>
<div class="box"><div class="title">DUNGEON RUNNERS</div><div class="sub">ADMIN PANEL</div>
<div id="login-panel">
<div class="err" id="err"></div><input id="user" placeholder="Admin Username" autofocus><input id="pass" type="password" placeholder="Password">
<button class="btn" onclick="doLogin()">ENTER THE DUNGEON</button>
<button class="btn2" onclick="showReset()">Forgot Password / Reset</button>
<div class="note">First login? Enter username + choose a password.</div></div>
<div id="reset-panel"><div class="err" id="rerr"></div><div class="ok" id="rok"></div>
<div style="font:700 14px Cinzel,serif;color:#c9a44a;margin-bottom:16px">Reset Admin Password</div>
<input id="ruser" placeholder="Admin Username"><input id="rnew" type="password" placeholder="New Password">
<input id="rnew2" type="password" placeholder="Confirm New Password">
<button class="btn" onclick="doReset()">RESET PASSWORD</button>
<button class="btn2" onclick="showLogin()">Back to Login</button></div></div>
<script>
document.getElementById('pass').addEventListener('keydown',e=>{if(e.key==='Enter')doLogin()});
function showReset(){document.getElementById('login-panel').style.display='none';document.getElementById('reset-panel').style.display='block'}
function showLogin(){document.getElementById('login-panel').style.display='block';document.getElementById('reset-panel').style.display='none'}
async function doLogin(){
  const u=document.getElementById('user').value,p=document.getElementById('pass').value;
  if(!u||!p){document.getElementById('err').style.display='block';document.getElementById('err').textContent='Enter username and password';return}
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
  const d=await r.json();if(d.ok)location.href='/';
  else{document.getElementById('err').style.display='block';document.getElementById('err').textContent=d.error||'Login failed'}}
async function doReset(){
  const u=document.getElementById('ruser').value,p=document.getElementById('rnew').value,p2=document.getElementById('rnew2').value;
  document.getElementById('rerr').style.display='none';document.getElementById('rok').style.display='none';
  if(!u||!p){document.getElementById('rerr').style.display='block';document.getElementById('rerr').textContent='Fill in all fields';return}
  if(p!==p2){document.getElementById('rerr').style.display='block';document.getElementById('rerr').textContent='Passwords do not match';return}
  if(p.length<4){document.getElementById('rerr').style.display='block';document.getElementById('rerr').textContent='Password too short';return}
  const r=await fetch('/api/reset-password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:u,password:p})});
  const d=await r.json();if(d.ok){document.getElementById('rok').style.display='block';document.getElementById('rok').textContent='Password reset! You can now login.';setTimeout(()=>{showLogin();document.getElementById('user').value=u},2000)}
  else{document.getElementById('rerr').style.display='block';document.getElementById('rerr').textContent=d.error||'Reset failed'}}
</script></body></html>"""


# ── Dashboard loader ──

def _load_dashboard():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    return "<html><body><h1>dashboard.html not found</h1></body></html>"


# ── HTTP request handler ──


class _Handler(BaseHTTPRequestHandler):

    def _json(self, data, status=200):
        body = json.dumps(data, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _html(self, html):
        self.send_response(200)
        self.send_header("Content-Type", "text/html;charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _redirect(self, url):
        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()

    def _session(self):
        return _check_session(self.headers.get("Cookie"))

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n)) if n else {}

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/")
        params = parse_qs(urlparse(self.path).query)

        if path in ("/login", "/login.html"):
            return self._html(LOGIN_HTML)

        # Serve static images — no auth needed
        if path.startswith("/images/"):
            img_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")
            fname = os.path.basename(path)
            fpath = os.path.join(img_dir, fname)
            if os.path.exists(fpath) and ".." not in path:
                ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
                ct = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                      "gif": "image/gif", "webp": "image/webp", "svg": "image/svg+xml",
                      "ico": "image/x-icon"}.get(ext, "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Cache-Control", "public, max-age=3600")
                self.end_headers()
                with open(fpath, "rb") as f:
                    self.wfile.write(f.read())
                return
            else:
                self.send_response(404)
                self.end_headers()
                return

        sess = self._session()
        if not sess:
            if path.startswith("/api/"):
                return self._json({"error": "Unauthorized"}, 401)
            return self._redirect("/login")

        if path in ("", "/", "/index.html"):
            return self._html(_load_dashboard())

        try:
            if path == "/api/me":
                self._json({"username": sess["username"], "admin": sess.get("admin", False)})
            elif path == "/api/status":
                self._json(_api_status())
            elif path == "/api/stats":
                self._json(_api_stats())
            elif path == "/api/online":
                self._json(_api_online())
            elif path == "/api/characters":
                self._json(_api_characters(params))
            elif path.startswith("/api/characters/"):
                self._json(_api_char_detail(int(path.split("/")[-1])))
            elif path == "/api/accounts":
                self._json(_api_accounts(params))
            elif path.startswith("/api/accounts/"):
                aid = int(path.split("/")[-1])
                self._json(_q_one("SELECT id,username,email,is_member,is_banned,is_admin,created_at,last_login FROM accounts WHERE id=:i", {"i": aid}) or {"error": "Not found"})
            elif path == "/api/connections":
                self._json(_api_online())
            elif path == "/api/config":
                self._json(_api_config_read())
            elif path == "/api/items":
                self._json(_api_items(params))
            elif path == "/api/logs":
                count = int(params.get("count", ["200"])[0])
                self._json(_log_lines[-count:] if _log_lines else [])
            elif path == "/api/chatlog":
                self._json(_api_chatlog(params))
            elif path == "/api/activity":
                self._json(_api_activity(params))
            elif path == "/api/leaderboard":
                self._json(_api_leaderboard(params))
            elif path == "/api/bans":
                self._json(_api_bans(params))
            elif path == "/api/boosts":
                self._json({"xp_boost": "inactive", "gold_boost": "inactive", "maintenance": "off"})
            elif path == "/api/scheduled-announces":
                self._json({"rows": []})
            else:
                self._json({"error": "Not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")

        if path == "/api/login":
            body = self._body()
            user = _verify_login(body.get("username", ""), body.get("password", ""))
            if not user:
                return self._json({"error": "Invalid credentials or not an admin"}, 401)
            token = _create_session(user)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Set-Cookie", f"dr_session={token};Path=/;HttpOnly;Max-Age={SESSION_HOURS*3600};SameSite=Lax")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
            return

        if path == "/api/reset-password":
            body = self._body()
            uname = body.get("username", ""); newpw = body.get("password", "")
            if not uname or not newpw:
                return self._json({"error": "Username and password required"})
            row = _q_one("SELECT id,username,is_admin FROM accounts WHERE username=:u COLLATE NOCASE", {"u": uname})
            if not row:
                return self._json({"error": "Account not found"})
            if not row["is_admin"]:
                return self._json({"error": "Not an admin account"})
            salt_bytes = secrets.token_bytes(16)
            new_salt = base64.b64encode(salt_bytes).decode("ascii")
            new_hash = _hash_pw(newpw, new_salt)
            _q_exec("UPDATE accounts SET password_hash=:h,salt=:s WHERE id=:i",
                    {"h": new_hash, "s": new_salt, "i": row["id"]})
            return self._json({"ok": True})

        sess = self._session()
        if not sess:
            return self._json({"error": "Unauthorized"}, 401)

        try:
            body = self._body()
            if path == "/api/items/send":
                self._json(_api_send_item(body))
            elif path == "/api/gold/grant":
                self._json(_api_grant_gold(body))
            elif path == "/api/level/set":
                self._json(_api_set_level(body))
            elif path == "/api/xp/set":
                self._json(_api_set_xp(body))
            elif path == "/api/command":
                self._json(_api_send_command(body))
            elif path == "/api/accounts/create":
                self._json(_api_create_account(body))
            elif path == "/api/scheduled-announces":
                self._json({"ok": True})
            else:
                self._json({"error": "Not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_PUT(self):
        sess = self._session()
        if not sess:
            return self._json({"error": "Unauthorized"}, 401)
        path = urlparse(self.path).path.rstrip("/")
        body = self._body()
        try:
            if path.startswith("/api/accounts/"):
                self._json(_api_update_account(int(path.split("/")[-1]), body))
            elif path.startswith("/api/characters/"):
                self._json(_api_update_char(int(path.split("/")[-1]), body))
            elif path == "/api/config":
                self._json({"error": "Config editing via admin panel is read-only. Edit config.yaml instead."})
            elif path.startswith("/api/scheduled-announces/"):
                self._json({"ok": True})
            else:
                self._json({"error": "Not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def do_DELETE(self):
        sess = self._session()
        if not sess:
            return self._json({"error": "Unauthorized"}, 401)
        path = urlparse(self.path).path.rstrip("/")
        try:
            if path.startswith("/api/characters/"):
                cid = int(path.split("/")[-1])
                _q_exec("DELETE FROM characters WHERE id=:i", {"i": cid})
                self._json({"ok": True})
            elif path.startswith("/api/scheduled-announces/"):
                self._json({"ok": True})
            else:
                self._json({"error": "Not found"}, 404)
        except Exception as e:
            self._json({"error": str(e)}, 500)

    def log_message(self, fmt, *args):
        pass


# ── Startup ──


def start_admin_server(config: ServerConfig, game_server: "GameServer") -> threading.Thread:
    """Start the admin HTTP server in a daemon thread."""
    global _server_ref, _config_ref, _start_time

    _server_ref = game_server
    _config_ref = config
    _start_time = datetime.now()

    port = getattr(config, "admin_panel_port", None) or 8080

    # Ensure audit tables exist
    primitives._ensure_audit_tables()

    t = threading.Thread(
        target=_run_http_server,
        args=(port,),
        daemon=True,
        name="admin-http",
    )
    t.start()

    log.info(f"[ADMIN] panel listening on http://127.0.0.1:{port}")
    log.info(f"[ADMIN] login with any account that has is_admin=1 in the accounts table")
    return t


def _run_http_server(port: int) -> None:
    httpd = HTTPServer(("127.0.0.1", port), _Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        httpd.server_close()


# ── Hook: log chat messages from the game server ──

def hook_chat_message(sender: str, message: str, channel: str = "say", zone: str = "") -> None:
    """Called by the game server when a chat message is sent (for chat log)."""
    primitives.log_chat(sender, message, channel, zone)
