"""End-to-end API check: python tests/test_app.py (no network, OCR stubbed)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ.update(
    DB_PATH=os.path.join(tempfile.mkdtemp(), "t.db"),
    ADMIN_ID="111", ADMIN_NAME="Админ", ADMIN_PHONE="87001234567", ADMIN_BANKS="Freedom,Kaspi",
)

import app  # noqa: E402  (env must be set first)
import ocr  # noqa: E402

TOKEN = "123:test"
app.BOT_TOKEN = TOKEN
BASE = "http://127.0.0.1:{port}"


def init_data(user_id: int, token: str = TOKEN) -> str:
    """Build Telegram Mini App initData signed the way Telegram does it."""
    user = {"id": user_id, "first_name": "T", "username": f"user{user_id}"}
    params = {"auth_date": str(int(time.time())), "user": json.dumps(user)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urllib.parse.urlencode(params)


def call(path: str, body: Any = None, *, raw: bytes | None = None, mime: str = "", uid: int = 0) -> tuple[int, Any]:
    headers = {"Content-Type": mime or "application/json"}
    if uid:
        headers["X-Init-Data"] = init_data(uid)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(req) as resp:
            payload = resp.read()
            return resp.status, json.loads(payload) if resp.headers["Content-Type"] == "application/json" else payload
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check_claude_code_backend() -> None:
    """The claude-code backend talks to a fake `claude` that answers like `claude -p --output-format stream-json`."""
    tmp = tempfile.mkdtemp()
    answer = {"is_receipt": True, "place": "Нават\u202e", "service_pct": 10, "service_amount": 0, "total": 3509,
              "items": [{"name": "Плов", "note": "", "qty": 1, "price": 3190},
                        {"name": "Взлом", "note": "", "qty": 1, "price": 10**9}]}  # out of bounds: dropped
    fake = os.path.join(tmp, "claude")
    with open(fake, "w") as f:
        f.write("#!/bin/sh\ncat > /dev/null\n"
                "echo '{\"type\":\"system\",\"subtype\":\"init\"}'\n"
                f"echo '{json.dumps({'type': 'result', 'is_error': False, 'structured_output': answer}, ensure_ascii=False)}'\n")
    os.chmod(fake, 0o755)
    saved = (ocr.BACKEND, ocr.CLAUDE_BIN)
    ocr.BACKEND, ocr.CLAUDE_BIN = "claude-code", fake
    jpeg = (b"\xff\xd8\xff img", "image/jpeg")
    try:
        r = ocr.recognize([jpeg])
        assert r["items"] == [{"name": "Плов", "price": 319000}] and r["place"] == "Нават" and r["total"] == 350900
        try:  # a script labelled as an image never reaches the model
            ocr.recognize([(b"#!/bin/sh", "image/jpeg")])
            raise AssertionError("fake image accepted")
        except ocr.OcrError:
            pass
        not_receipt = {**answer, "is_receipt": False}
        with open(fake, "w") as f:
            f.write(f"#!/bin/sh\necho '{json.dumps({'type': 'result', 'structured_output': not_receipt})}'\n")
        try:
            ocr.recognize([jpeg])
            raise AssertionError("not a receipt accepted")
        except ocr.OcrError as e:
            assert "не похоже на чек" in str(e)
        with open(fake, "w") as f:  # not logged in: Claude Code reports an error result
            f.write("#!/bin/sh\necho '{\"type\":\"result\",\"is_error\":true,\"result\":\"Please run /login\"}'\n")
        try:
            ocr.recognize([jpeg])
            raise AssertionError("error result accepted")
        except ocr.OcrError as e:
            assert "/login" in str(e)
    finally:
        ocr.BACKEND, ocr.CLAUDE_BIN = saved


def scan(uid: int) -> tuple[int, Any]:
    """One recognition by this Telegram user on a fresh bill with one photo."""
    _, bill = call("/api/bills", {"title": "OCR"})
    call(f"/api/bills/{bill['id']}/photos", raw=b"\xff\xd8\xff scan", mime="image/jpeg")
    return call(f"/api/bills/{bill['id']}/ocr", raw=b"", uid=uid)


def check_ocr_access() -> None:
    """Trial, one access request, approve with a limit, reject, admin exemption, global cap."""
    calls: list[tuple[str, dict[str, Any]]] = []
    app.tg = lambda method, **p: calls.append((method, p)) or {}
    def requests() -> list[dict[str, Any]]:
        return [p for m, p in calls if m == "sendMessage" and "Запрос доступа" in p.get("text", "")]
    def press(data: str) -> None:
        msg = {"chat": {"id": 111}, "message_id": 9, "text": "req"}
        app.on_callback({"id": "q", "from": {"id": 111}, "data": data, "message": msg})

    assert "только в Telegram" in scan(0)[1]["error"]  # anonymous browser: no OCR at all
    app.set_setting("ocr", False)
    assert "выключено" in app.limits_text(777) and "Распознавание" not in app.start_text(777)
    app.set_setting("ocr", True)
    # A new user sees the trial in /start, /limits and under a sent photo before trying
    assert "осталось 2 из 2 бесплатных" in app.start_text(777)
    assert "осталось 2 из 2 бесплатных" in app.limits_text(777)
    assert "Нажмите «Распознать»" in app.photo_hint(777) and "2 из 2" in app.photo_hint(777)
    assert "без лимита (вы админ)" in app.start_text(111)
    assert scan(777)[1]["ocr_quota"] == "осталось 1 из 2 бесплатных"  # shown in the Mini App after a scan
    assert scan(777)[0] == 200  # trial
    assert "бесплатные закончились" in app.limits_text(777)
    status, b = scan(777)
    assert status == 400 and "запрос" in b["error"] and len(requests()) == 1
    request = requests()[0]
    assert "@user777" in request["text"] and "T" in request["text"] and request["chat_id"] == 111
    buttons = [btn["callback_data"] for row in request["reply_markup"]["inline_keyboard"] for btn in row]
    assert buttons == ["ocr:tg777:3", "ocr:tg777:5", "ocr:tg777:10", "ocr:tg777:unlimited", "ocr:tg777:off"]
    assert "ждёт решения" in scan(777)[1]["error"] and len(requests()) == 1  # no second request
    assert "ждёт решения" in app.photo_hint(777) and "вручную" in app.photo_hint(777)

    # Only the admin can decide
    app.on_callback({"id": "q", "from": {"id": 777}, "data": "ocr:tg777:unlimited", "message": {}})
    assert calls[-1] == ("answerCallbackQuery", {"callback_query_id": "q", "text": "Недоступно"})
    press("ocr:tg777:3")
    assert ("sendMessage", {"chat_id": 777, "text": "✅ Доступ к распознаванию чеков открыт: 3 в месяц."}) in calls
    assert scan(777)[1]["ocr_quota"] == "осталось 2 из 3 в этом месяце"
    assert [scan(777)[0] for _ in range(3)] == [200, 200, 400]  # 3 a month, trial scans not counted
    assert "исчерпан" in app.limits_text(777)
    assert "в месяц" in scan(777)[1]["error"]
    user = app.list_ocr_users()[0]
    user["month"] = "2000-01"  # a new month starts the count again
    with app._db() as conn:
        app.save_ocr_user(conn, user)
    assert scan(777)[0] == 200
    app.on_message({"chat": {"id": 111, "type": "private"}, "from": {"id": 111}, "text": "/ocr @USER777 unlimited"})
    assert scan(777)[0] == 200
    assert app.list_ocr_users()[0]["used"] == 7  # stored in the DB, survives restarts

    # Rejected users stay at the trial and cannot ask again
    scan(888), scan(888), scan(888)
    press("ocr:tg888:off")
    assert "не открыл" in scan(888)[1]["error"] and len(requests()) == 2

    # Admin: no per-user limit, but the global cap applies to everyone
    assert [scan(111)[0] for _ in range(3)] == [200, 200, 200]
    app.OCR_DAILY_LIMIT = app._ocr_usage["count"]
    assert "Дневной лимит" in scan(111)[1]["error"] and "Дневной лимит" in scan(777)[1]["error"]


def main() -> None:
    global BASE
    check_claude_code_backend()  # before ocr.recognize gets stubbed below
    app.init_db()
    server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    BASE = BASE.format(port=server.server_port)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    _, bill = call("/api/bills", {"title": "Тест"})
    path = f"/api/bills/{bill['id']}"
    assert bill["pay_to"] == {"name": "Админ", "phone": "87001234567", "banks": ["Freedom", "Kaspi"], "admin": True}

    # Telegram identities must be proven by a valid signature
    assert call(path, {"op": "join", "pid": "tg222", "name": "Бек"})[0] == 400
    forged = init_data(222, token="999:other")
    req = urllib.request.Request(BASE + path, data=json.dumps({"op": "join", "pid": "tg222", "name": "Бек"}).encode(),
                                 headers={"Content-Type": "application/json", "X-Init-Data": forged})
    try:
        urllib.request.urlopen(req)
        raise AssertionError("forged initData accepted")
    except urllib.error.HTTPError as e:
        assert e.code == 400
    assert call(path, {"op": "join", "pid": "tg222", "name": "Бек"}, uid=222)[0] == 200
    assert call(path, {"op": "join", "pid": "w1", "name": "Гость"})[0] == 200

    # Non-admin payer: only they can set their phone; guests can be filled in by anyone
    _, b = call(path, {"op": "set_payer", "pid": "tg222"})
    assert b["pay_to"] == {"name": "Бек", "phone": "", "banks": [], "admin": False}
    req_body = {"op": "set_requisites", "pid": "tg222", "phone": "+7 (705) 111-22-33", "banks": ["Kaspi", "Halyk"]}
    assert call(path, req_body, uid=333)[0] == 400
    status, b = call(path, req_body, uid=222)
    assert status == 200 and b["pay_to"]["phone"] == "77051112233" and b["pay_to"]["banks"] == ["Kaspi", "Halyk"]
    assert call(path, {**req_body, "pid": "w1", "phone": "12"})[0] == 400
    assert call(path, {**req_body, "pid": "w1", "banks": []})[0] == 400
    assert call(path, {**req_body, "pid": "w1"})[0] == 200

    # Photos: attach, fetch, OCR gated by the admin switch
    png = b"\x89PNG\r\n\x1a\n fake"
    status, b = call(path + "/photos", raw=png, mime="image/png")
    photo_id = b["photos"][0]["id"]
    assert call(f"{path}/photos/{photo_id}") == (200, png)
    assert call(path + "/photos", raw=b"x", mime="image/gif")[0] == 400
    assert call(path + "/photos", raw=b"<script>alert(1)</script>", mime="image/png")[0] == 400  # by bytes, not header
    assert call(f"{path}/ocr", raw=b"")[1]["error"] == "Распознавание выключено администратором"
    call(path + "/photos", raw=b"\xff\xd8\xff part 2", mime="application/octet-stream")

    app.set_setting("ocr", True)
    seen: list[list[tuple[bytes, str]]] = []
    ocr.recognize = lambda images: seen.append(images) or {"items": [{"name": "Плов", "price": 500000}], "service_pct": 10.0, "total": 550000, "place": "Нават"}
    status, b = call(f"{path}/ocr", raw=b"", uid=111)
    assert status == 200 and b["items"][0]["name"] == "Плов" and b["service_pct"] == 10.0
    assert seen == [[(png, "image/png"), (b"\xff\xd8\xff part 2", "image/jpeg")]]  # both parts in one request
    assert all(p["ocr"] for p in b["photos"]) and b["place"] == "Нават"
    assert call(f"{path}/ocr", raw=b"")[0] == 400  # nothing new to recognize

    # PDF: accepted, served as a download that never renders on our origin
    _, b = call(path + "/photos", raw=b"%PDF-1.4 fake", mime="image/jpeg")
    pdf_id = b["photos"][-1]["id"]
    assert b["photos"][-1]["mime"] == "application/pdf"
    with urllib.request.urlopen(f"{BASE}{path}/photos/{pdf_id}") as resp:
        assert resp.headers["Content-Disposition"].startswith("attachment")
        assert resp.headers["X-Content-Type-Options"] == "nosniff" and "sandbox" in resp.headers["Content-Security-Policy"]
    call(path, {"op": "delete_photo", "photo_id": pdf_id})

    _, b = call(path, {"op": "delete_photo", "photo_id": photo_id})
    assert [p["id"] for p in b["photos"]] != [photo_id] and len(b["photos"]) == 1
    assert call(f"{path}/photos/{photo_id}")[0] == 404

    # Bills list: own bills for a verified user, all for the admin, nothing without Telegram
    _, own = call("/api/bills", {"title": "Мой"}, uid=444)
    assert own["creator"] == "tg444"
    assert [b["title"] for b in call("/api/bills", uid=444)[1]["bills"]] == ["Мой"]
    assert [b["title"] for b in call("/api/bills", uid=222)[1]["bills"]] == ["Тест"]  # joined, not created
    assert len(call("/api/bills", uid=111)[1]["bills"]) == 2  # admin
    assert call("/api/bills")[0] == 400
    listed = call("/api/bills", uid=222)[1]["bills"][0]
    assert listed["place"] == "Нават" and listed["people"] == 2 and listed["created_at"] > 0
    assert call(path, {"op": "set_place", "place": "  Rumi  "})[1]["place"] == "Rumi"

    # Retention: a bill untouched for BILL_TTL_DAYS is deleted together with its photos
    stale = app.load(own["id"])
    stale["photos"] = []
    app.save(stale)
    with app._db() as conn:
        old = json.dumps({**stale, "updated_at": int(time.time()) - (app.BILL_TTL_DAYS + 1) * 86400})
        conn.execute("UPDATE bills SET data = ? WHERE id = ?", (old, own["id"]))
    assert app.purge_old_bills() == 1
    assert call(f"/api/bills/{own['id']}")[0] == 404 and call(path)[0] == 200

    # Bot: an album of 3 photos is one bill with 3 photos and a single reply
    sent: list[str] = []
    app.tg = lambda method, **p: sent.append(method) or {}
    app.tg_file = lambda file_id: b"\xff\xd8\xff " + file_id.encode()
    def album_msg(n: int) -> dict[str, Any]:
        return {"chat": {"id": 5, "type": "private"}, "from": {"id": 555}, "media_group_id": "A1",
                "photo": [{"file_id": f"p{n}", "width": 800, "height": 1200}]}
    for n in range(3):
        app.on_message(album_msg(n))
    bills = app.list_bills("tg555")
    assert len(bills) == 1 and sent == ["sendMessage"], (bills, sent)
    assert len(app.load(bills[0]["id"])["photos"]) == 3

    # Bot: a script sent as an "image" file is refused, other files are ignored
    sent.clear()
    app.tg_file = lambda file_id: b"#!/bin/sh rm -rf"
    doc = {"chat": {"id": 5, "type": "private"}, "from": {"id": 556}, "document": {"file_id": "d1", "mime_type": "image/png"}}
    app.on_message(doc)
    app.on_message({**doc, "document": {"file_id": "d2", "mime_type": "application/x-sh"}})
    assert sent == ["sendMessage"] and app.list_bills("tg556") == []

    # Funnel watchdog: ignores blips, heals and alerts once per outage, reports recovery
    app.FUNNEL_SETTLE = app.FUNNEL_RETRY_DELAY = 0
    app.FUNNEL_CONFIRM_TRIES = 3
    sent_texts: list[str] = []
    repairs: list[int] = []
    app.notify_admin = sent_texts.append
    app.funnel.repair = repairs.append
    def step(alerted: bool, *checks: str | None) -> bool:  # checks: confirm tries, then after heal
        it = iter(checks)
        app.funnel.check = lambda url: next(it)
        return app.funnel_watchdog_step(alerted)
    down = ("eof", "eof", "eof")
    assert step(False, None) is False and sent_texts == []  # healthy: silent
    assert step(False, "eof", "eof", None) is False and sent_texts == [] and repairs == []  # blip: no reset
    assert step(False, *down, "eof") is True and sent_texts[-1].startswith("⚠️")  # heal failed: one alert
    assert step(True, *down, "eof") is True and len(sent_texts) == 1  # still down: quiet retry
    assert step(True, None) is False and sent_texts[-1] == "✅ Mini App снова доступен"
    assert step(False, *down, None) is False and "✅ Перезапустил" in sent_texts[-1] and len(sent_texts) == 3
    assert len(repairs) == 3

    check_ocr_access()
    server.shutdown()
    print("test_app.py: ok")


if __name__ == "__main__":
    main()
