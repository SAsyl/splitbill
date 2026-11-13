#!/usr/bin/env python3
"""Bill splitter: web UI / Telegram Mini App + Telegram bot in one process.

Env:
    PUBLIC_URL          https URL where this server is reachable (required for the bot)
    BOT_TOKEN           Telegram bot token (optional: without it only the web UI runs)
    WEBAPP_SHORT_NAME   Mini App short name from @BotFather /newapp (optional)
    ADMIN_ID            Telegram user id of the admin: /admin panel, default payee
    ADMIN_NAME, ADMIN_PHONE, ADMIN_BANKS   default payee requisites (editable via /requisites)
    ANTHROPIC_API_KEY   receipt recognition via the API; without it local Claude Code is used
    OCR_BACKEND         force "api" or "claude-code"; CLAUDE_BIN: path to the claude CLI
    OCR_DAILY_LIMIT     max recognitions per day for everyone together (default 20)
    OCR_TRIAL_SCANS     free recognitions per new user before admin approval (default 2)
    BILL_TTL_DAYS       bills are deleted this many days after the last change (default 14)
    PORT, DB_PATH
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import takewhile
from pathlib import Path
from typing import Any, Callable, TypedDict

import funnel
import ocr
from split import Bill, compute

PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
WEBAPP_SHORT_NAME = os.environ.get("WEBAPP_SHORT_NAME", "")
ADMIN_ID = int(os.environ.get("ADMIN_ID") or 0)
ADMIN_PID = f"tg{ADMIN_ID}"
OCR_DAILY_LIMIT = int(os.environ.get("OCR_DAILY_LIMIT", "20"))
OCR_TRIAL_SCANS = int(os.environ.get("OCR_TRIAL_SCANS", "2"))
MAX_USER_MONTHLY_LIMIT = 1000
BILL_TTL_DAYS = int(os.environ.get("BILL_TTL_DAYS", "14"))
PURGE_INTERVAL = 6 * 3600
PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = os.environ.get("DB_PATH", "bills.db")
INDEX_HTML = (Path(__file__).parent / "index.html").read_bytes()

MAX_BODY = 6 * 1024 * 1024
MAX_ITEMS = 300
MAX_PEOPLE = 100
MAX_PHOTOS = 10
MAX_BANKS = 8
INIT_DATA_TTL = 24 * 3600
ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

DEFAULT_SETTINGS: dict[str, Any] = {
    "ocr": False,
    "admin_name": os.environ.get("ADMIN_NAME", "Админ"),
    "admin_phone": os.environ.get("ADMIN_PHONE", ""),
    "admin_banks": [b.strip() for b in os.environ.get("ADMIN_BANKS", "").split(",") if b.strip()],
}

bot_username = ""


class BadRequest(Exception):
    """Client sent something invalid; message is shown to the user."""


class OcrUser(TypedDict):
    """OCR access of one Telegram user. status: trial | pending | approved | rejected."""
    pid: str
    name: str
    username: str
    status: str
    monthly_limit: int | None  # approved only; None means unlimited
    used: int  # all-time recognitions, trial included
    month: str  # "YYYY-MM" of month_used
    month_used: int  # approved recognitions this month; trial scans do not count


# ---------- storage ----------

# ponytail: one global lock for read-modify-write; per-bill locks if traffic ever matters
_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    with _db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS bills (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS photos "
            "(id TEXT PRIMARY KEY, bill_id TEXT NOT NULL, mime TEXT NOT NULL, data BLOB NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ocr_users (pid TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "username TEXT NOT NULL, status TEXT NOT NULL, monthly_limit INTEGER, used INTEGER NOT NULL, "
            "month TEXT NOT NULL, month_used INTEGER NOT NULL)"
        )
        rows = conn.execute("SELECT data FROM bills").fetchall()
    # ponytail: bills saved before timestamps existed start their TTL now; remove after one TTL cycle
    for (data,) in rows:
        bill = json.loads(data)
        if "created_at" not in bill:
            bill.update(created_at=int(time.time()), place="", creator=None)
            save(bill)


def load(bill_id: str) -> Bill:
    with _db() as conn:
        row = conn.execute("SELECT data FROM bills WHERE id = ?", (bill_id,)).fetchone()
    if row is None:
        raise KeyError(bill_id)
    return json.loads(row[0])


def save(bill: Bill) -> None:
    bill["updated_at"] = int(time.time())
    with _db() as conn:
        conn.execute("INSERT OR REPLACE INTO bills VALUES (?, ?)", (bill["id"], json.dumps(bill)))


def settings() -> dict[str, Any]:
    with _db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {**DEFAULT_SETTINGS, **{k: json.loads(v) for k, v in rows}}


def set_setting(key: str, value: Any) -> None:
    with _db() as conn:
        conn.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, json.dumps(value)))


def load_photo(bill_id: str, photo_id: str) -> tuple[bytes, str]:
    with _db() as conn:
        row = conn.execute(
            "SELECT data, mime FROM photos WHERE id = ? AND bill_id = ?", (photo_id, bill_id)
        ).fetchone()
    if row is None:
        raise KeyError(photo_id)
    return row[0], row[1]


def create_bill(title: str, creator: str | None) -> Bill:
    bill: Bill = {
        "id": secrets.token_urlsafe(12),  # unguessable: the link is the access token
        "title": title[:100] or "Счёт",
        "place": "",
        "creator": creator,
        "created_at": int(time.time()),
        "service_pct": 0,
        "payer": None,
        "receipt_total": 0,
        "people": {},
        "items": [],
        "photos": [],
        "version": 1,
    }
    with _lock:
        save(bill)
    return bill


def apply_receipt(bill: Bill, receipt: ocr.Receipt) -> None:
    room = MAX_ITEMS - len(bill["items"])
    bill["items"] += [{**i, "id": secrets.token_hex(4), "shares": {}} for i in receipt["items"][:room]]
    bill["service_pct"] = receipt["service_pct"] or bill["service_pct"]
    bill["receipt_total"] += receipt["total"]
    bill["place"] = bill["place"] or receipt["place"]


def summary(bill: Bill) -> dict[str, Any]:
    """One line of the bills list: when, where, how many people."""
    return {
        "id": bill["id"], "title": bill["title"], "place": bill["place"],
        "created_at": bill["created_at"], "people": len(bill["people"]), "url": share_url(bill["id"]),
    }


def list_bills(pid: str) -> list[dict[str, Any]]:
    """Bills the person created or joined; the admin sees all of them."""
    with _db() as conn:
        rows = conn.execute("SELECT data FROM bills").fetchall()
    # ponytail: full scan in Python; the table stays small because of BILL_TTL_DAYS
    bills = [json.loads(data) for (data,) in rows]
    mine = [b for b in bills if pid == ADMIN_PID or b["creator"] == pid or pid in b["people"]]
    return [summary(b) for b in sorted(mine, key=lambda b: b["updated_at"], reverse=True)]


def purge_old_bills() -> int:
    """Delete bills (with their photos) untouched for BILL_TTL_DAYS."""
    cutoff = time.time() - BILL_TTL_DAYS * 86400
    with _lock, _db() as conn:
        ids = [(r[0],) for r in conn.execute(
            "SELECT id FROM bills WHERE json_extract(data, '$.updated_at') < ?", (cutoff,)
        )]
        conn.executemany("DELETE FROM photos WHERE bill_id = ?", ids)
        conn.executemany("DELETE FROM bills WHERE id = ?", ids)
    return len(ids)


def run_purger() -> None:
    while True:
        try:
            if removed := purge_old_bills():
                print(f"purged {removed} bills older than {BILL_TTL_DAYS} days")
        except sqlite3.Error as e:
            print(f"purge failed: {e}")
        time.sleep(PURGE_INTERVAL)


def is_funnel() -> bool:
    return (urllib.parse.urlsplit(PUBLIC_URL).hostname or "").endswith(".ts.net")


def share_url(bill_id: str) -> str:
    if bot_username and WEBAPP_SHORT_NAME:
        return f"https://t.me/{bot_username}/{WEBAPP_SHORT_NAME}?startapp={bill_id}"
    return f"{PUBLIC_URL}/b/{bill_id}"


def pay_to(bill: Bill, s: dict[str, Any]) -> dict[str, Any]:
    """Whom to transfer to: the admin by default, otherwise the chosen payer."""
    payer = bill["payer"]
    if payer is None or payer == ADMIN_PID:
        return {"name": s["admin_name"], "phone": s["admin_phone"], "banks": s["admin_banks"], "admin": True}
    person = bill["people"][payer]
    return {"name": person["name"], "phone": person.get("phone", ""), "banks": person.get("banks", []), "admin": False}


def view(bill: Bill) -> dict[str, Any]:
    s = settings()
    return {
        **bill,
        "calc": compute(bill),
        "share_url": share_url(bill["id"]),
        "ocr": s["ocr"],
        "pay_to": pay_to(bill, s),
        "admin_name": s["admin_name"],
        "admin_pid": ADMIN_PID if ADMIN_ID else None,
        "max_photos": MAX_PHOTOS,
    }


# ---------- Telegram identity ----------

def verified_pid(init_data: str) -> str | None:
    """Person id from Telegram Mini App initData, only if its signature is valid."""
    user = verified_user(init_data)
    return None if user is None else f"tg{user['id']}"


def verified_user(init_data: str) -> dict[str, Any] | None:
    """Telegram user (id, first_name, username...) from initData, only if its signature is valid."""
    if not init_data or not BOT_TOKEN:
        return None
    params = dict(urllib.parse.parse_qsl(init_data))
    received = params.pop("hash", "")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    if time.time() - int(params.get("auth_date", "0")) > INIT_DATA_TTL:
        return None
    user = json.loads(params["user"])
    if not isinstance(user, dict) or not isinstance(user.get("id"), int):
        return None
    return user


def _require_self(pid: str, body: dict[str, Any]) -> None:
    """Telegram participants are edited only by themselves: protects their phone number."""
    if pid.startswith("tg") and body.get("_uid") != pid:
        raise BadRequest("Эти данные может менять только сам участник (откройте счёт в Telegram)")


# ---------- input validation ----------

def _str(body: dict[str, Any], key: str, limit: int = 100) -> str:
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        raise BadRequest(f"'{key}' must be a non-empty string")
    return value.strip()[:limit]


def _money(body: dict[str, Any], key: str) -> int:
    try:
        value = Decimal(str(body.get(key)).replace(",", ".").replace(" ", ""))
    except InvalidOperation as e:
        raise BadRequest(f"'{key}' must be a number") from e
    if not value.is_finite() or not 0 <= value <= 100_000_000:
        raise BadRequest(f"'{key}' is out of range")
    return int((value * 100).to_integral_value())


def _int(body: dict[str, Any], key: str, lo: int, hi: int) -> int:
    value = body.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
        raise BadRequest(f"'{key}' must be an integer in [{lo}, {hi}]")
    return value


def normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if not 10 <= len(digits) <= 15:
        raise BadRequest("Номер телефона должен содержать 10–15 цифр")
    return digits


def normalize_banks(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(b, str) for b in value):
        raise BadRequest("'banks' must be a list of strings")
    banks = list(dict.fromkeys(b.strip()[:30] for b in value if b.strip()))
    if not 1 <= len(banks) <= MAX_BANKS:
        raise BadRequest(f"Укажите от 1 до {MAX_BANKS} банков")
    return banks


def _person(bill: Bill, body: dict[str, Any]) -> str:
    pid = _str(body, "pid", 64)
    if pid not in bill["people"]:
        raise BadRequest("Unknown person")
    return pid


def _item(bill: Bill, body: dict[str, Any]) -> dict[str, Any]:
    item_id = _str(body, "item_id", 16)
    for item in bill["items"]:
        if item["id"] == item_id:
            return item
    raise BadRequest("Unknown item")


def _photo(bill: Bill, photo_id: str) -> dict[str, Any]:
    for photo in bill["photos"]:
        if photo["id"] == photo_id:
            return photo
    raise KeyError(photo_id)


# ---------- operations ----------

def op_join(bill: Bill, body: dict[str, Any]) -> None:
    pid = _str(body, "pid", 64)
    _require_self(pid, body)
    if pid not in bill["people"] and len(bill["people"]) >= MAX_PEOPLE:
        raise BadRequest("Too many people")
    bill["people"].setdefault(pid, {"paid": False})["name"] = _str(body, "name", 40)


def op_remove_person(bill: Bill, body: dict[str, Any]) -> None:
    pid = _person(bill, body)
    del bill["people"][pid]
    for item in bill["items"]:
        item["shares"].pop(pid, None)
    if bill["payer"] == pid:
        bill["payer"] = None


def op_add_item(bill: Bill, body: dict[str, Any]) -> None:
    if len(bill["items"]) >= MAX_ITEMS:
        raise BadRequest("Too many items")
    item = {"id": secrets.token_hex(4), "name": _str(body, "name"), "price": _money(body, "price"), "shares": {}}
    bill["items"].append(item)


def op_update_item(bill: Bill, body: dict[str, Any]) -> None:
    item = _item(bill, body)
    item["name"] = _str(body, "name")
    item["price"] = _money(body, "price")


def op_delete_item(bill: Bill, body: dict[str, Any]) -> None:
    item = _item(bill, body)
    bill["items"].remove(item)


def op_set_share(bill: Bill, body: dict[str, Any]) -> None:
    item = _item(bill, body)
    pid = _person(bill, body)
    count = _int(body, "count", 0, 99)
    if count:
        item["shares"][pid] = count
    else:
        item["shares"].pop(pid, None)


def op_share_all(bill: Bill, body: dict[str, Any]) -> None:
    item = _item(bill, body)
    everyone = {pid: 1 for pid in bill["people"]}
    item["shares"] = {} if item["shares"] == everyone else everyone


def op_set_service(bill: Bill, body: dict[str, Any]) -> None:
    value = body.get("service_pct")
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not -100 <= value <= 100:
        raise BadRequest("'service_pct' must be a number in [-100, 100]")
    bill["service_pct"] = value


def op_set_title(bill: Bill, body: dict[str, Any]) -> None:
    bill["title"] = _str(body, "title")


def op_set_place(bill: Bill, body: dict[str, Any]) -> None:
    value = body.get("place")
    if not isinstance(value, str):
        raise BadRequest("'place' must be a string")
    bill["place"] = value.strip()[:60]


def op_set_payer(bill: Bill, body: dict[str, Any]) -> None:
    bill["payer"] = _person(bill, body) if body.get("pid") else None


def op_set_paid(bill: Bill, body: dict[str, Any]) -> None:
    bill["people"][_person(bill, body)]["paid"] = bool(body.get("paid"))


def op_set_requisites(bill: Bill, body: dict[str, Any]) -> None:
    pid = _person(bill, body)
    _require_self(pid, body)
    person = bill["people"][pid]
    person["phone"] = normalize_phone(_str(body, "phone", 30))
    person["banks"] = normalize_banks(body.get("banks"))


def op_delete_photo(bill: Bill, body: dict[str, Any]) -> None:
    photo = _photo(bill, _str(body, "photo_id", 64))
    bill["photos"].remove(photo)
    with _db() as conn:
        conn.execute("DELETE FROM photos WHERE id = ?", (photo["id"],))


OPS: dict[str, Callable[[Bill, dict[str, Any]], None]] = {
    name[3:]: fn for name, fn in globals().items() if name.startswith("op_")
}


def apply_op(bill_id: str, body: dict[str, Any]) -> Bill:
    op = OPS.get(str(body.get("op")))
    if op is None:
        raise BadRequest(f"Unknown op: {body.get('op')!r}")
    with _lock:
        bill = load(bill_id)
        op(bill, body)
        bill["version"] += 1
        save(bill)
    return bill


# ---------- photos & OCR ----------

def add_photo(bill_id: str, data: bytes) -> Bill:
    mime = ocr.sniff(data)  # real type from the bytes; the client's Content-Type is ignored
    if mime is None:
        raise BadRequest("Поддерживаются только фото (JPEG, PNG, WebP) и PDF")
    if len(data) > ocr.MAX_IMAGE_BYTES:
        raise BadRequest("Файл должен быть не больше 5 МБ")
    with _lock:
        bill = load(bill_id)
        if len(bill["photos"]) >= MAX_PHOTOS:
            raise BadRequest(f"Не больше {MAX_PHOTOS} фото на счёт")
        photo_id = secrets.token_hex(8)
        with _db() as conn:
            conn.execute("INSERT INTO photos VALUES (?, ?, ?, ?)", (photo_id, bill_id, mime, data))
        bill["photos"].append({"id": photo_id, "ocr": False, "mime": mime})
        bill["version"] += 1
        save(bill)
    return bill


# ponytail: global safety cap in memory, resets on restart; per-user counters live in ocr_users
_ocr_usage = {"day": "", "count": 0}

OCR_COLUMNS = ("pid", "name", "username", "status", "monthly_limit", "used", "month", "month_used")
TRIAL_OVER = ("Бесплатные распознавания закончились. Я отправил админу запрос на доступ, "
              "а пока позиции можно внести вручную.")
DENIALS = {
    "pending": "Запрос на доступ к распознаванию ждёт решения админа. Пока позиции можно внести вручную.",
    "rejected": "Админ не открыл вам доступ к распознаванию. Позиции можно внести вручную.",
}


def _ocr_user_row(row: tuple[Any, ...]) -> OcrUser:
    pid, name, username, status, monthly_limit, used, month, month_used = row
    return OcrUser(pid=pid, name=name, username=username, status=status, monthly_limit=monthly_limit,
                   used=used, month=month, month_used=month_used)


def load_ocr_user(conn: sqlite3.Connection, pid: str) -> OcrUser | None:
    row = conn.execute(f"SELECT {', '.join(OCR_COLUMNS)} FROM ocr_users WHERE pid = ?", (pid,)).fetchone()
    return None if row is None else _ocr_user_row(row)


def save_ocr_user(conn: sqlite3.Connection, user: OcrUser) -> None:
    conn.execute("INSERT OR REPLACE INTO ocr_users VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
        user["pid"], user["name"], user["username"], user["status"],
        user["monthly_limit"], user["used"], user["month"], user["month_used"]))


def list_ocr_users() -> list[OcrUser]:
    with _db() as conn:
        rows = conn.execute(f"SELECT {', '.join(OCR_COLUMNS)} FROM ocr_users ORDER BY used DESC").fetchall()
    return [_ocr_user_row(row) for row in rows]


def new_ocr_user(pid: str) -> OcrUser:
    return OcrUser(pid=pid, name="", username="", status="trial", monthly_limit=None, used=0, month="", month_used=0)


def ocr_user_for(conn: sqlite3.Connection, tg_user: dict[str, Any]) -> OcrUser:
    """Stored record of this Telegram user (a new one starts on trial) with fresh name and username."""
    pid = f"tg{tg_user['id']}"
    user = load_ocr_user(conn, pid) or new_ocr_user(pid)
    name = " ".join(str(tg_user.get(k) or "") for k in ("first_name", "last_name")).strip()
    user["name"] = name[:64] or pid
    user["username"] = str(tg_user.get("username") or "")[:32]
    return user


def month_used(user: OcrUser, month: str) -> int:
    return user["month_used"] if user["month"] == month else 0


def user_denial(user: OcrUser, month: str) -> str | None:
    """Why this user may not recognize now, or None if allowed."""
    if user["status"] == "trial":
        return None if user["used"] < OCR_TRIAL_SCANS else TRIAL_OVER
    if user["status"] in DENIALS:
        return DENIALS[user["status"]]
    limit = user["monthly_limit"]
    if limit is not None and month_used(user, month) >= limit:
        return f"Ваш лимит — {limit} распознаваний в месяц, он исчерпан. Новые будут с 1-го числа."
    return None


def quota_text(user: OcrUser, month: str) -> str:
    """What is left for this user, in words they understand."""
    match user["status"]:
        case "trial":
            left = max(OCR_TRIAL_SCANS - user["used"], 0)
            if left:
                return f"осталось {left} из {OCR_TRIAL_SCANS} бесплатных"
            return "бесплатные закончились. Нажмите «Распознать» в счёте, и я отправлю админу запрос на доступ"
        case "pending":
            return "запрос на доступ ждёт решения админа"
        case "rejected":
            return "недоступно, позиции вносятся вручную"
    limit = user["monthly_limit"]
    if limit is None:
        return "без лимита"
    left = max(limit - month_used(user, month), 0)
    return f"осталось {left} из {limit} в этом месяце" if left else \
        f"лимит {limit} в месяц исчерпан, новые будут с 1-го числа"


def ocr_quota(tg_id: int) -> tuple[bool, str]:
    """(can recognize right now, what is left) for a Telegram user; the admin has no personal limit."""
    pid = f"tg{tg_id}"
    if pid == ADMIN_PID:
        return True, "без лимита (вы админ)"
    month = time.strftime("%Y-%m")
    with _db() as conn:
        user = load_ocr_user(conn, pid) or new_ocr_user(pid)
    return user_denial(user, month) is None, quota_text(user, month)


def count_ocr_use(user: OcrUser, month: str) -> None:
    if user["status"] == "approved":  # trial scans are free: "3 a month" means 3 after approval
        user["month_used"] = month_used(user, month) + 1
        user["month"] = month
    user["used"] += 1


def take_ocr_quota(tg_user: dict[str, Any] | None) -> None:
    """Per-user limit (admin is exempt), then the global OCR_DAILY_LIMIT; raises BadRequest if denied."""
    if tg_user is None:
        raise BadRequest("Распознавание работает только в Telegram: откройте счёт через бота")
    today, month = time.strftime("%Y-%m-%d"), time.strftime("%Y-%m")
    with _lock, _db() as conn:
        user = None if f"tg{tg_user['id']}" == ADMIN_PID else ocr_user_for(conn, tg_user)
        denial = user_denial(user, month) if user else None
        newly_pending = user is not None and denial == TRIAL_OVER
        if newly_pending and user is not None:
            user["status"] = "pending"  # exactly one request: later attempts see "pending"
        if _ocr_usage["day"] != today:
            _ocr_usage.update(day=today, count=0)
        if denial is None and _ocr_usage["count"] >= OCR_DAILY_LIMIT:
            denial = "Дневной лимит распознаваний исчерпан, попробуйте завтра"
        if denial is None:
            _ocr_usage["count"] += 1
            if user is not None:
                count_ocr_use(user, month)
        if user is not None:
            save_ocr_user(conn, user)
    if user is not None and newly_pending:
        request_ocr_access(user)  # outside the lock: network call
    if denial:
        raise BadRequest(denial)


def parse_access(value: str) -> tuple[str, int | None]:
    """'unlimited' | 'off' | N (scans per month) -> (status, monthly_limit)."""
    if value == "unlimited":
        return "approved", None
    if value == "off":
        return "rejected", None
    if value.isdigit() and 1 <= int(value) <= MAX_USER_MONTHLY_LIMIT:
        return "approved", int(value)
    raise BadRequest(f"Лимит: число от 1 до {MAX_USER_MONTHLY_LIMIT}, unlimited или off")


def set_ocr_access(pid: str, value: str) -> OcrUser:
    status, limit = parse_access(value)
    with _lock, _db() as conn:
        user = load_ocr_user(conn, pid)
        if user is None:
            raise BadRequest("Такой пользователь ещё не пользовался распознаванием")
        user["status"], user["monthly_limit"] = status, limit
        save_ocr_user(conn, user)
    return user


def recognize_pending(bill_id: str, tg_user: dict[str, Any] | None) -> Bill:
    """All not-yet-recognized photos in one request: parts of one long bill are read together."""
    if not settings()["ocr"]:
        raise BadRequest("Распознавание выключено администратором")
    pending = [p["id"] for p in load(bill_id)["photos"] if not p["ocr"]]
    if not pending:
        raise BadRequest("Нет новых фото для распознавания")
    take_ocr_quota(tg_user)
    receipt = ocr.recognize([load_photo(bill_id, pid) for pid in pending])  # slow: outside the lock
    with _lock:
        bill = load(bill_id)
        photos = [p for p in bill["photos"] if p["id"] in pending]
        if any(p["ocr"] for p in photos):
            raise BadRequest("Эти фото уже распознаны")
        apply_receipt(bill, receipt)
        for photo in photos:
            photo["ocr"] = True
        bill["version"] += 1
        save(bill)
    return bill


# ---------- HTTP ----------

class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str, cache: str = "no-store") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        if not content_type.startswith(("text/html", "application/json")):  # user files: never run as a page
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "sandbox; default-src 'none'")
        if content_type == ocr.PDF:  # download, not render on our origin
            self.send_header("Content-Disposition", 'attachment; filename="receipt.pdf"')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json")

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise BadRequest("Request too large")
        return self.rfile.read(length)

    def _json_body(self) -> dict[str, Any]:
        body = json.loads(self._body() or b"{}")
        if not isinstance(body, dict):
            raise BadRequest("JSON object expected")
        body["_uid"] = verified_pid(self.headers.get("X-Init-Data", ""))  # server-set, never trusted from client
        return body

    def _route(self, handler: Callable[[list[str]], Any]) -> None:
        parts = [p for p in self.path.split("?")[0].split("/") if p]
        try:
            if any(not ID_RE.match(p) for p in parts[2:3] + parts[4:5]):
                raise KeyError(self.path)
            result = handler(parts)
            if isinstance(result, tuple):  # (bytes, mime): a stored photo
                self._send(HTTPStatus.OK, result[0], result[1], "private, max-age=31536000, immutable")
            else:
                self._json(HTTPStatus.OK, result)
        except KeyError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "Не найдено"})
        except (BadRequest, ocr.OcrError, json.JSONDecodeError) as e:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(e)})

    def do_GET(self) -> None:
        if not self.path.startswith("/api/"):
            self._send(HTTPStatus.OK, INDEX_HTML, "text/html; charset=utf-8")
            return
        self._route(self._get)

    def do_POST(self) -> None:
        self._route(self._post)

    def _get(self, parts: list[str]) -> Any:
        match parts:
            case ["api", "bills"]:
                pid = verified_pid(self.headers.get("X-Init-Data", ""))
                if pid is None:
                    raise BadRequest("Список счетов доступен в Telegram")
                return {"ttl_days": BILL_TTL_DAYS, "bills": list_bills(pid)}
            case ["api", "bills", bill_id]:
                return view(load(bill_id))
            case ["api", "bills", bill_id, "photos", photo_id]:
                return load_photo(bill_id, photo_id)
        raise KeyError(self.path)

    def _post(self, parts: list[str]) -> Any:
        match parts:
            case ["api", "bills"]:
                body = self._json_body()
                return view(create_bill(str(body.get("title") or ""), body["_uid"]))
            case ["api", "bills", bill_id]:
                return view(apply_op(bill_id, self._json_body()))
            case ["api", "bills", bill_id, "photos"]:
                return view(add_photo(bill_id, self._body()))
            case ["api", "bills", bill_id, "ocr"]:
                tg_user = verified_user(self.headers.get("X-Init-Data", ""))
                bill = recognize_pending(bill_id, tg_user)
                return {**view(bill), "ocr_quota": ocr_quota(tg_user["id"])[1] if tg_user else ""}
        raise KeyError(self.path)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # keep logs quiet: bill ids in URLs act as access tokens


# ---------- Telegram bot ----------

def tg(method: str, **params: Any) -> Any:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=json.dumps(params).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:  # Telegram explains 4xx errors in the JSON body
        data = json.loads(e.read() or b"{}")
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
    return data["result"]


def tg_file(file_id: str) -> bytes:
    path = tg("getFile", file_id=file_id)["file_path"]
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{path}", timeout=60) as resp:
        return resp.read()


def reply_with_bill(chat_id: int, bill: Bill, text: str) -> None:
    tg("sendMessage", chat_id=chat_id, text=text, reply_markup={
        "inline_keyboard": [[{"text": "🧾 Открыть счёт", "url": share_url(bill["id"])}]],
    })


# Telegram delivers an album as separate messages sharing media_group_id
# ponytail: in-memory, trimmed to the latest albums; albums arrive within seconds
_album_bills: dict[str, str] = {}
MAX_TRACKED_ALBUMS = 200


def is_bill_document(msg: dict[str, Any]) -> bool:
    mime = msg.get("document", {}).get("mime_type", "")
    return mime.startswith("image/") or mime == ocr.PDF  # other files are not ours: ignore silently


def on_photo(chat_id: int, msg: dict[str, Any]) -> None:
    # A photo, or an image/PDF sent as a file; the type is checked by add_photo
    if "photo" in msg:
        file = max(msg["photo"], key=lambda p: p["width"] * p["height"])
    elif msg["document"].get("file_size", 0) > ocr.MAX_IMAGE_BYTES:
        tg("sendMessage", chat_id=chat_id, text="Файл больше 5 МБ — пришлите фото или PDF поменьше.")
        return
    else:
        file = msg["document"]
    data = tg_file(file["file_id"])
    if ocr.sniff(data) is None:
        tg("sendMessage", chat_id=chat_id, text="Принимаю только фото чека (JPEG, PNG, WebP) или PDF.")
        return
    album = msg.get("media_group_id")
    if album in _album_bills:  # next part of a long bill: same bill, no new reply
        add_photo(_album_bills[album], data)
        return
    bill = create_bill(msg.get("caption") or "Счёт", f"tg{msg['from']['id']}")
    if album:
        _album_bills[album] = bill["id"]
        while len(_album_bills) > MAX_TRACKED_ALBUMS:
            _album_bills.pop(next(iter(_album_bills)))
    add_photo(bill["id"], data)
    reply_with_bill(chat_id, bill, f"Счёт создан, фото чека прикреплено. {photo_hint(msg['from']['id'])} 👇")


def photo_hint(tg_id: int) -> str:
    if not settings()["ocr"]:
        return "Внесите позиции вручную."
    allowed, left = ocr_quota(tg_id)
    if allowed:
        return f"Нажмите «Распознать» в счёте — все фото прочитаются как один чек ({left})."
    return f"Распознавание: {left}. Внесите позиции вручную."


def bill_label(b: dict[str, Any]) -> str:
    when = time.strftime("%d.%m %H:%M", time.localtime(b["created_at"]))
    parts = [when, b["place"], f"{b['people']} чел.", b["title"]]
    return " · ".join(p for p in parts if p)[:64]


def on_bills(msg: dict[str, Any]) -> None:
    chat_id = msg["chat"]["id"]
    if msg["chat"]["type"] != "private":
        tg("sendMessage", chat_id=chat_id, text="Список счетов — в личке с ботом: /bills")
        return
    bills = list_bills(f"tg{msg['from']['id']}")[:30]
    if not bills:
        tg("sendMessage", chat_id=chat_id, text=f"За последние {BILL_TTL_DAYS} дней счетов нет. /new — создать новый.")
        return
    tg("sendMessage", chat_id=chat_id,
       text=f"Счета за последние {BILL_TTL_DAYS} дней (дата · заведение · людей · название):",
       reply_markup={"inline_keyboard": [[{"text": bill_label(b), "url": b["url"]}] for b in bills]})


def admin_panel() -> tuple[str, dict[str, Any]]:
    s = settings()
    requisites = f"{s['admin_phone']} · {', '.join(s['admin_banks'])}" if s["admin_phone"] else "не заданы"
    text = (
        "⚙️ Админка\n\n"
        f"OCR: {'включён ✅' if s['ocr'] else 'выключен'} ({'Claude Code' if ocr.BACKEND == 'claude-code' else 'API'})\n"
        f"Распознаваний сегодня: {_ocr_usage['count']} / {OCR_DAILY_LIMIT}\n"
        f"Реквизиты по умолчанию: {requisites}\n\n"
        "Изменить реквизиты (банки по приоритету):\n/requisites 87001234567 Freedom Kaspi\n\n"
        f"/ocr — кто пользуется распознаванием и их лимиты (новым — {OCR_TRIAL_SCANS} бесплатно)\n"
        f"/bills — все счета (хранятся {BILL_TTL_DAYS} дней после последнего изменения)"
    )
    button = {"text": "Выключить OCR" if s["ocr"] else "Включить OCR", "callback_data": "ocr_toggle"}
    rows = [[button]]
    if is_funnel():
        rows.append([{"text": "🔧 Проверить и починить Mini App", "callback_data": "funnel_fix"}])
    return text, {"inline_keyboard": rows}


def parse_requisites(text: str) -> tuple[str, list[str]]:
    """'/requisites 8 705 123 45 67 Freedom, Home Credit' -> phone digits + banks in order."""
    args = text.split()[1:]
    phone_parts = list(takewhile(lambda a: re.fullmatch(r"[+\d()-]+", a), args))
    rest = " ".join(args[len(phone_parts):])
    return "".join(phone_parts), rest.split(",") if "," in rest else rest.split()


def limit_text(user: OcrUser) -> str:
    return "без лимита" if user["monthly_limit"] is None else f"{user['monthly_limit']} в месяц"


def describe_access(user: OcrUser) -> str:
    match user["status"]:
        case "trial":
            return f"пробный, {user['used']} из {OCR_TRIAL_SCANS}"
        case "pending":
            return "ждёт решения"
        case "rejected":
            return "отклонён"
    if user["monthly_limit"] is None:
        return "без лимита"
    return f"{limit_text(user)}, в этом месяце {month_used(user, time.strftime('%Y-%m'))}"


def user_label(user: OcrUser) -> str:
    username = f"@{user['username']}" if user["username"] else "без username"
    return f"{user['name']} ({username}, id {user['pid'][2:]})"


def access_keyboard(pid: str) -> dict[str, Any]:
    def button(text: str, value: str) -> dict[str, str]:
        return {"text": text, "callback_data": f"ocr:{pid}:{value}"}
    return {"inline_keyboard": [
        [button("✅ 3 в месяц", "3"), button("✅ 5 в месяц", "5"), button("✅ 10 в месяц", "10")],
        [button("♾ Без лимита", "unlimited"), button("❌ Отклонить", "off")],
    ]}


def request_ocr_access(user: OcrUser) -> None:
    """Ask the admin once, right after the user's trial runs out."""
    if not ADMIN_ID:
        return
    tg("sendMessage", chat_id=ADMIN_ID, reply_markup=access_keyboard(user["pid"]), text=(
        f"🔍 Запрос доступа к распознаванию\n{user_label(user)}\n"
        f"Использовал {user['used']} бесплатных из {OCR_TRIAL_SCANS}.\n\n"
        f"Другой лимит в месяц: /ocr {user['pid'][2:]} 7"
    ))


def grant_ocr_access(pid: str, value: str) -> OcrUser:
    """Apply the admin's decision and tell the user; they may never have started the bot."""
    user = set_ocr_access(pid, value)
    text = ("❌ Админ не открыл доступ к распознаванию чеков." if user["status"] == "rejected"
            else f"✅ Доступ к распознаванию чеков открыт: {limit_text(user)}.")
    try:
        tg("sendMessage", chat_id=int(pid[2:]), text=text)
    except RuntimeError as e:
        print(f"ocr access: user not notified: {e}")
    return user


def find_ocr_pid(target: str) -> str:
    """'@username' or a numeric Telegram id -> pid of a known OCR user."""
    users = list_ocr_users()
    if target.startswith("@"):
        matches = [u["pid"] for u in users if u["username"].lower() == target[1:].lower()]
    else:
        matches = [u["pid"] for u in users if u["pid"] == f"tg{target}"]
    if not matches:
        raise BadRequest(f"{target}: такой пользователь ещё не пользовался распознаванием")
    return matches[0]


def ocr_users_text() -> str:
    users = list_ocr_users()
    lines = [f"• {user_label(u)} — {describe_access(u)}, всего {u['used']}" for u in users[:50]]
    return (
        "🔍 Распознавание по пользователям\n\n" + ("\n".join(lines) or "Пока никто не пользовался.") +
        "\n\nИзменить: /ocr @username 5 (в месяц) | unlimited | off"
    )


def on_ocr_command(chat_id: int, text: str) -> None:
    args = text.split()[1:]
    if not args:
        tg("sendMessage", chat_id=chat_id, text=ocr_users_text())
        return
    try:
        if len(args) != 2:
            raise BadRequest("Формат: /ocr @username 5 | unlimited | off")
        user = grant_ocr_access(find_ocr_pid(args[0]), args[1].lower())
    except BadRequest as e:
        tg("sendMessage", chat_id=chat_id, text=str(e))
        return
    tg("sendMessage", chat_id=chat_id, text=f"Готово: {user_label(user)} — {describe_access(user)}")


def on_admin_command(msg: dict[str, Any], text: str) -> None:
    chat_id = msg["chat"]["id"]
    if text.startswith("/ocr"):
        on_ocr_command(chat_id, text)
        return
    if text.startswith("/requisites"):
        phone, banks = parse_requisites(text)
        try:
            set_setting("admin_phone", normalize_phone(phone))
            set_setting("admin_banks", normalize_banks(banks))
        except BadRequest as e:
            tg("sendMessage", chat_id=chat_id, text=f"{e}\nПример: /requisites 87001234567 Freedom Kaspi")
            return
        set_setting("admin_name", msg["from"].get("first_name") or "Админ")
    panel, keyboard = admin_panel()
    tg("sendMessage", chat_id=chat_id, text=panel, reply_markup=keyboard)


def on_access_decision(query: dict[str, Any]) -> None:
    """Approve / Reject button under an access request: 'ocr:<pid>:<value>'."""
    _, pid, value = query["data"].split(":", 2)
    try:
        user = grant_ocr_access(pid, value)
    except BadRequest as e:
        tg("answerCallbackQuery", callback_query_id=query["id"], text=str(e), show_alert=True)
        return
    msg = query["message"]
    tg("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"],
       text=f"{msg.get('text', '')}\n\nРешение: {describe_access(user)}")
    tg("answerCallbackQuery", callback_query_id=query["id"], text=describe_access(user))


def on_callback(query: dict[str, Any]) -> None:
    data = query.get("data") or ""
    if query["from"]["id"] != ADMIN_ID or not (data in {"ocr_toggle", "funnel_fix"} or data.startswith("ocr:")):
        tg("answerCallbackQuery", callback_query_id=query["id"], text="Недоступно")
        return
    if data.startswith("ocr:"):
        on_access_decision(query)
        return
    if query["data"] == "funnel_fix":
        tg("answerCallbackQuery", callback_query_id=query["id"], text="Проверяю, до 40 секунд…")
        # slow (network + settle time): off the polling thread so the bot keeps answering
        threading.Thread(target=check_funnel_now, args=(query["message"]["chat"]["id"],), daemon=True).start()
        return
    enable = not settings()["ocr"]
    reason = ocr.unavailable_reason()
    if enable and reason:
        tg("answerCallbackQuery", callback_query_id=query["id"], text=reason, show_alert=True)
        return
    set_setting("ocr", enable)
    panel, keyboard = admin_panel()
    msg = query["message"]
    tg("editMessageText", chat_id=msg["chat"]["id"], message_id=msg["message_id"], text=panel, reply_markup=keyboard)
    tg("answerCallbackQuery", callback_query_id=query["id"], text="OCR включён" if enable else "OCR выключен")


def limits_text(tg_id: int) -> str:
    if not settings()["ocr"]:
        return "🔍 Распознавание чеков сейчас выключено админом. Позиции вносятся вручную."
    return (
        f"🔍 Распознавание чеков: {ocr_quota(tg_id)[1]}.\n\n"
        f"Бесплатных распознаваний у нового пользователя: {OCR_TRIAL_SCANS}. "
        "Когда они закончатся, бот сам попросит у админа доступ, и он решит, сколько распознаваний "
        "дать в месяц. Позиции всегда можно внести вручную."
    )


def start_text(tg_id: int) -> str:
    ocr_line = ""
    if settings()["ocr"]:
        ocr_line = f"\n\n🔍 Распознавание чеков: {ocr_quota(tg_id)[1]}. /limits — подробнее."
    return (
        "Делю счёт без калькулятора.\n\n"
        "📸 Пришлите фото чека — создам счёт. Длинный чек — несколько фото одним альбомом.\n"
        "✍️ /new [название] — пустой счёт.\n"
        f"📋 /bills — ваши счета за {BILL_TTL_DAYS} дней.\n\n"
        "Потом каждый открывает ссылку и отмечает, что ел. "
        f"Суммы округляются так, что сходятся без остатка.{ocr_line}"
    )


def on_message(msg: dict[str, Any]) -> None:
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    is_admin = bool(ADMIN_ID) and msg.get("from", {}).get("id") == ADMIN_ID and msg["chat"]["type"] == "private"
    if msg.get("photo") or is_bill_document(msg):
        on_photo(chat_id, msg)
    elif is_admin and re.match(r"^/(admin|requisites|ocr)\b", text):
        on_admin_command(msg, text)
    elif re.match(r"^/bills\b", text):
        on_bills(msg)
    elif re.match(r"^/limits\b", text):
        tg("sendMessage", chat_id=chat_id, text=limits_text(msg["from"]["id"]))
    elif re.match(r"^/(new|split)(@\w+)?", text):
        title = text.split(maxsplit=1)[1] if " " in text else msg["chat"].get("title", "Счёт")
        bill = create_bill(title, f"tg{msg['from']['id']}")
        reply_with_bill(chat_id, bill, "Счёт создан, добавьте позиции или фото чека 👇")
    elif text.startswith("/start") or text.startswith("/help"):
        if is_admin:
            setup_admin_commands()
        tg("sendMessage", chat_id=chat_id, text=start_text(msg["from"]["id"]))


def handle_safely(update: dict[str, Any]) -> None:
    try:
        if "message" in update:
            on_message(update["message"])
        elif "callback_query" in update:
            on_callback(update["callback_query"])
    except Exception as e:  # one bad update must not kill the polling thread
        print(f"bot: failed to handle update: {type(e).__name__}: {e}")


# ---------- Funnel watchdog: the bot polls Telegram, but the Mini App needs inbound HTTPS ----------

FUNNEL_CHECK_INTERVAL = 5 * 60
FUNNEL_SETTLE = 20  # seconds for the ingress to pick the node up again
FUNNEL_CONFIRM_TRIES = 3  # a lone TLS timeout is a network blip, not an outage
FUNNEL_RETRY_DELAY = 20


def notify_admin(text: str) -> None:
    if ADMIN_ID:
        tg("sendMessage", chat_id=ADMIN_ID, text=text)


def heal_funnel() -> str:
    """Re-apply Funnel, re-check from outside and describe the result for the admin."""
    try:
        funnel.repair(PORT)
    except funnel.FunnelError as e:
        return f"❌ Не удалось перезапустить Funnel: {e}"
    time.sleep(FUNNEL_SETTLE)
    reason = funnel.check(PUBLIC_URL)
    return "✅ Перезапустил Funnel, Mini App снова доступен" if reason is None else \
        f"❌ Перезапустил Funnel, но Mini App всё ещё недоступен: {reason}"


def confirmed_funnel_failure() -> str | None:
    """Reason the Mini App is down, only if every one of the spaced checks fails; else None."""
    reason = None
    for attempt in range(FUNNEL_CONFIRM_TRIES):
        if attempt:
            time.sleep(FUNNEL_RETRY_DELAY)
        reason = funnel.check(PUBLIC_URL)
        if reason is None:
            return None
    return reason


def funnel_watchdog_step(alerted: bool) -> bool:
    """One check-and-heal round; returns whether the admin now knows the Mini App is down."""
    reason = confirmed_funnel_failure()
    if reason is None:
        if alerted:
            notify_admin("✅ Mini App снова доступен")
        return False
    result = heal_funnel()
    print(f"funnel down ({reason}): {result}")
    healed = result.startswith("✅")
    if healed or not alerted:  # one alert per outage, retries stay quiet
        notify_admin(f"⚠️ Mini App был недоступен извне ({reason}).\n{result}")
    return not healed


def run_funnel_watchdog() -> None:
    alerted = False
    while True:
        time.sleep(FUNNEL_CHECK_INTERVAL)
        try:
            alerted = funnel_watchdog_step(alerted)
        except Exception as e:  # e.g. Telegram unreachable: try again next round
            print(f"funnel watchdog: {type(e).__name__}: {e}")


def check_funnel_now(chat_id: int) -> None:
    reason = funnel.check(PUBLIC_URL)
    tg("sendMessage", chat_id=chat_id, text="✅ Mini App доступен извне" if reason is None else
       f"⚠️ Mini App недоступен извне ({reason}).\n{heal_funnel()}")


def run_bot() -> None:
    offset = 0
    while True:
        try:
            for update in tg("getUpdates", offset=offset, timeout=50):
                offset = update["update_id"] + 1
                handle_safely(update)
        except (urllib.error.URLError, RuntimeError, TimeoutError) as e:
            print(f"bot error: {e}")
            time.sleep(5)


PUBLIC_COMMANDS = [
    {"command": "new", "description": "Новый счёт: /new Пятница"},
    {"command": "bills", "description": "Мои счета"},
    {"command": "limits", "description": "Сколько распознаваний чеков осталось"},
    {"command": "help", "description": "Как пользоваться"},
]


def setup_admin_commands() -> None:
    """Admin-only menu; Telegram allows it only after the admin has pressed Start in the bot."""
    if not ADMIN_ID:
        return
    try:
        tg("setMyCommands", scope={"type": "chat", "chat_id": ADMIN_ID}, commands=PUBLIC_COMMANDS + [
            {"command": "admin", "description": "Админка: OCR, реквизиты"},
            {"command": "requisites", "description": "Реквизиты: /requisites 8700… Freedom Kaspi"},
            {"command": "ocr", "description": "Распознавание: пользователи и лимиты"},
        ])
    except RuntimeError as e:
        print(f"admin menu not set ({e}): open the bot from ADMIN_ID and press Start")


def setup_bot() -> None:
    """Command menus and the "Счёт" button next to the input field."""
    tg("setMyCommands", commands=PUBLIC_COMMANDS)
    setup_admin_commands()
    tg("setChatMenuButton", menu_button={"type": "web_app", "text": "Счёт", "web_app": {"url": PUBLIC_URL}})


def main() -> None:
    global bot_username
    init_db()
    threading.Thread(target=run_purger, daemon=True).start()
    if BOT_TOKEN:
        if not PUBLIC_URL.startswith("https://"):
            raise SystemExit("PUBLIC_URL must be an https:// URL when BOT_TOKEN is set")
        bot_username = tg("getMe")["username"]
        setup_bot()
        threading.Thread(target=run_bot, daemon=True).start()
        print(f"bot @{bot_username} polling")
    if is_funnel():
        threading.Thread(target=run_funnel_watchdog, daemon=True).start()
    print(f"listening on :{PORT}")
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
