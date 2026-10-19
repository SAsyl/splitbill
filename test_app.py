"""End-to-end API check: python test_app.py (no network, OCR stubbed)."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from typing import Any

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
    params = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id, "first_name": "T"})}
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
            return resp.status, payload if resp.headers["Content-Type"].startswith("image/") else json.loads(payload)
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check_claude_code_backend() -> None:
    """The claude-code backend talks to a fake `claude` that answers like `claude -p --output-format stream-json`."""
    tmp = tempfile.mkdtemp()
    answer = {"items": [{"name": "Плов", "note": "", "qty": 1, "price": 3190}],
              "place": "Нават", "service_pct": 10, "service_amount": 0, "total": 3509}
    fake = os.path.join(tmp, "claude")
    with open(fake, "w") as f:
        f.write("#!/bin/sh\ncat > /dev/null\n"
                "echo '{\"type\":\"system\",\"subtype\":\"init\"}'\n"
                f"echo '{json.dumps({'type': 'result', 'is_error': False, 'structured_output': answer}, ensure_ascii=False)}'\n")
    os.chmod(fake, 0o755)
    saved = (ocr.BACKEND, ocr.CLAUDE_BIN)
    ocr.BACKEND, ocr.CLAUDE_BIN = "claude-code", fake
    try:
        r = ocr.recognize([(b"img", "image/jpeg")])
        assert r["items"] == [{"name": "Плов", "price": 319000}] and r["place"] == "Нават" and r["total"] == 350900
        with open(fake, "w") as f:  # not logged in: Claude Code reports an error result
            f.write("#!/bin/sh\necho '{\"type\":\"result\",\"is_error\":true,\"result\":\"Please run /login\"}'\n")
        try:
            ocr.recognize([(b"img", "image/jpeg")])
            raise AssertionError("error result accepted")
        except ocr.OcrError as e:
            assert "/login" in str(e)
    finally:
        ocr.BACKEND, ocr.CLAUDE_BIN = saved


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
    png = b"\x89PNG fake"
    status, b = call(path + "/photos", raw=png, mime="image/png")
    photo_id = b["photos"][0]["id"]
    assert call(f"{path}/photos/{photo_id}") == (200, png)
    assert call(path + "/photos", raw=b"x", mime="image/gif")[0] == 400
    assert call(f"{path}/ocr", raw=b"")[1]["error"] == "Распознавание выключено администратором"
    call(path + "/photos", raw=b"\xff\xd8 part 2", mime="image/jpeg")

    app.set_setting("ocr", True)
    seen: list[list[tuple[bytes, str]]] = []
    ocr.recognize = lambda images: seen.append(images) or {"items": [{"name": "Плов", "price": 500000}], "service_pct": 10.0, "total": 550000, "place": "Нават"}
    status, b = call(f"{path}/ocr", raw=b"")
    assert status == 200 and b["items"][0]["name"] == "Плов" and b["service_pct"] == 10.0
    assert seen == [[(png, "image/png"), (b"\xff\xd8 part 2", "image/jpeg")]]  # both parts in one request
    assert all(p["ocr"] for p in b["photos"]) and b["place"] == "Нават"
    assert call(f"{path}/ocr", raw=b"")[0] == 400  # nothing new to recognize

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
    app.tg_file = lambda file_id: b"\xff\xd8 " + file_id.encode()
    def album_msg(n: int) -> dict[str, Any]:
        return {"chat": {"id": 5, "type": "private"}, "from": {"id": 555}, "media_group_id": "A1",
                "photo": [{"file_id": f"p{n}", "width": 800, "height": 1200}]}
    for n in range(3):
        app.on_message(album_msg(n))
    bills = app.list_bills("tg555")
    assert len(bills) == 1 and sent == ["sendMessage"], (bills, sent)
    assert len(app.load(bills[0]["id"])["photos"]) == 3

    server.shutdown()
    print("test_app.py: ok")


if __name__ == "__main__":
    main()
