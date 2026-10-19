"""Receipt photos -> list of items, via Claude vision with structured output.

Two backends, same prompt and schema:
- "api": Messages API, needs ANTHROPIC_API_KEY;
- "claude-code": headless Claude Code (`claude -p`) on this machine, uses its login (e.g. a subscription).
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any, TypedDict

import anthropic

MODEL = os.environ.get("OCR_MODEL", "claude-haiku-5-5")
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # API limit per image
MAX_TOTAL_BYTES = 20 * 1024 * 1024  # base64 of all photos must fit the 32 MB request limit
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}

BACKEND = os.environ.get("OCR_BACKEND") or ("api" if os.environ.get("ANTHROPIC_API_KEY") else "claude-code")
if BACKEND not in {"api", "claude-code"}:
    raise ValueError(f"OCR_BACKEND must be 'api' or 'claude-code', got {BACKEND!r}")
# systemd services get a minimal PATH, so fall back to the default install location
CLAUDE_BIN = os.environ.get("CLAUDE_BIN") or shutil.which("claude") or str(Path.home() / ".local/bin/claude")
CLAUDE_CODE_TIMEOUT = 180

PROMPT = """The photos show ONE restaurant/cafe bill (Russian or Kazakh). A long bill may be split
across several photos that can overlap: list every position exactly once.

For each ordered position return:
- name: as printed, cleaned up (fix obvious typos, keep the language).
- note: short modifier printed with this position, e.g. "острое", "без лука" (max 40 chars), else "".
  Never list the contents of a combo/set here.
- qty: quantity (1 if not printed).
- price: the LINE TOTAL for this position (qty x unit price), as a plain number.

Rules:
- Lines listed under a combo/set (its contents, usually without their own price) are NOT separate positions.
- The same dish ordered several times with DIFFERENT modifiers: separate positions, qty 1 each.
  Identical repeats: one position with qty.
- Takeaway packaging, bags, containers with a price ARE positions.
- Subtotal, total, VAT/НДС, payment, change and service charge lines are NOT positions.

Also return:
- place: the venue / restaurant name printed on the bill, else "".
- service_pct: service charge percent if printed (e.g. "Обслуживание 10%"), else 0.
- service_amount: service charge amount if printed, else 0.
- total: the final amount to pay as printed, else 0.
If the photos are not a bill, return an empty items list."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "note": {"type": "string"},
                    "qty": {"type": "number"},
                    "price": {"type": "number"},
                },
                "required": ["name", "note", "qty", "price"],
                "additionalProperties": False,
            },
        },
        "place": {"type": "string"},
        "service_pct": {"type": "number"},
        "service_amount": {"type": "number"},
        "total": {"type": "number"},
    },
    "required": ["items", "place", "service_pct", "service_amount", "total"],
    "additionalProperties": False,
}

Image = tuple[bytes, str]  # (data, media type)


class OcrError(Exception):
    """Receipt could not be recognized."""


class Item(TypedDict):
    name: str
    price: int  # minor units


class Receipt(TypedDict):
    items: list[Item]
    service_pct: float
    total: int  # minor units, 0 if not printed
    place: str  # venue name, "" if not printed


def to_minor(value: float) -> int:
    return int((Decimal(str(value)) * 100).to_integral_value())


def _item_name(raw: dict[str, Any]) -> str:
    name = raw["name"].strip()[:80]
    note = raw["note"].strip()[:40]
    qty = f" ×{raw['qty']:g}" if raw["qty"] != 1 else ""
    return f"{name} ({note}){qty}" if note else f"{name}{qty}"


def _service_pct(raw: dict[str, Any], items_sum: int) -> float:
    if raw["service_pct"] > 0:
        return float(raw["service_pct"])
    if raw["service_amount"] > 0 and items_sum > 0:
        return round(to_minor(raw["service_amount"]) * 100 / items_sum, 2)
    return 0.0


def _validate(images: list[Image]) -> None:
    if not images:
        raise OcrError("No photos to recognize")
    for data, media_type in images:
        if media_type not in IMAGE_TYPES:
            raise OcrError(f"Unsupported image type: {media_type}")
        if len(data) > MAX_IMAGE_BYTES:
            raise OcrError("A photo is larger than 5 MB")
    if sum(len(data) for data, _ in images) > MAX_TOTAL_BYTES:
        raise OcrError("Photos are larger than 20 MB in total")


def _image_block(data: bytes, media_type: str) -> dict[str, Any]:
    return {"type": "image", "source": {
        "type": "base64", "media_type": media_type, "data": base64.standard_b64encode(data).decode(),
    }}


def _ask_claude(images: list[Image]) -> dict[str, Any]:
    response = anthropic.Anthropic().messages.create(
        model=MODEL,
        max_tokens=16000,
        output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{
            "role": "user",
            "content": [*(_image_block(d, m) for d, m in images), {"type": "text", "text": PROMPT}],
        }],
    )
    if response.stop_reason != "end_turn":
        raise OcrError(f"Model stopped with {response.stop_reason!r}")
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


def _claude_code_command() -> list[str]:
    return [
        CLAUDE_BIN, "-p",
        "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
        "--model", MODEL,
        "--json-schema", json.dumps(SCHEMA),
        "--tools", "",  # images come inside the message: no file or shell access needed
        "--strict-mcp-config",
        "--setting-sources", "",  # skip user hooks and plugins
        "--no-session-persistence",
    ]


def _parse_claude_code(stdout: str, returncode: int, stderr: str) -> dict[str, Any]:
    events = [json.loads(line) for line in stdout.splitlines() if line.startswith("{")]
    result = next((e for e in events if e.get("type") == "result"), None)
    if result is None:
        raise OcrError(f"Claude Code exited with code {returncode}: {stderr.strip()[-300:]}")
    if result.get("is_error") or not result.get("structured_output"):
        raise OcrError(f"Claude Code: {str(result.get('result') or result.get('subtype'))[:300]}")
    return result["structured_output"]


def _ask_claude_code(images: list[Image]) -> dict[str, Any]:
    content = [*(_image_block(d, m) for d, m in images), {"type": "text", "text": PROMPT}]
    message = {"type": "user", "message": {"role": "user", "content": content}}
    with tempfile.TemporaryDirectory() as cwd:  # empty dir: no project CLAUDE.md is picked up
        try:
            proc = subprocess.run(
                _claude_code_command(), input=json.dumps(message), capture_output=True,
                text=True, timeout=CLAUDE_CODE_TIMEOUT, cwd=cwd,
            )
        except subprocess.TimeoutExpired as e:
            raise OcrError(f"Claude Code did not answer in {CLAUDE_CODE_TIMEOUT} s") from e
        except OSError as e:
            raise OcrError(f"Cannot start Claude Code ({CLAUDE_BIN}): {e}") from e
    return _parse_claude_code(proc.stdout, proc.returncode, proc.stderr)


def unavailable_reason() -> str | None:
    """Why OCR cannot run with the configured backend, or None if it can."""
    if BACKEND == "api":
        return None if os.environ.get("ANTHROPIC_API_KEY") else "Не задан ANTHROPIC_API_KEY"
    return None if Path(CLAUDE_BIN).exists() else f"Не найден Claude Code: {CLAUDE_BIN}"


def recognize(images: list[Image]) -> Receipt:
    """All photos of one bill in a single request, so overlapping parts are not counted twice."""
    _validate(images)
    try:
        raw = _ask_claude_code(images) if BACKEND == "claude-code" else _ask_claude(images)
    except anthropic.APIError as e:
        raise OcrError(f"Claude API error: {e}") from e
    except TypeError as e:  # raised by the SDK when no API key / credentials are configured
        raise OcrError("Claude API key is not configured") from e
    items: list[Item] = [
        {"name": _item_name(i), "price": to_minor(i["price"])}
        for i in raw["items"]
        if i["price"] >= 0 and i["name"].strip()
    ]
    if not items:
        raise OcrError("No items found on the photos")
    items_sum = sum(i["price"] for i in items)
    return {
        "items": items,
        "service_pct": _service_pct(raw, items_sum),
        "total": to_minor(raw["total"]),
        "place": raw["place"].strip()[:60],
    }
