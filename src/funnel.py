"""Tailscale Funnel health: check the Mini App through the public ingress and re-apply Funnel."""

from __future__ import annotations

import json
import socket
import ssl
import subprocess
import urllib.parse
import urllib.request

TIMEOUT = 10
OPERATOR_HINT = "Один раз выполните на ноутбуке: sudo tailscale set --operator=$USER"


class FunnelError(Exception):
    """Funnel could not be checked or re-applied."""


def _public_ip(host: str) -> str:
    # The laptop's own resolver answers with the tailnet IP (MagicDNS) and would skip the public ingress
    url = "https://1.1.1.1/dns-query?" + urllib.parse.urlencode({"name": host, "type": "A"})
    req = urllib.request.Request(url, headers={"Accept": "application/dns-json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        answers = [a["data"] for a in json.load(resp).get("Answer", []) if a["type"] == 1]
    if not answers:
        raise FunnelError(f"{host} нет в публичном DNS")
    return answers[0]


def check(public_url: str) -> str | None:
    """None if the page answers 200 via the public ingress, as Telegram sees it; else the reason."""
    host = urllib.parse.urlsplit(public_url).hostname or ""
    try:
        ip = _public_ip(host)
        with socket.create_connection((ip, 443), timeout=TIMEOUT) as raw, \
                ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
            tls.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            status = tls.recv(64).split(b"\r\n")[0].decode(errors="replace")
    except (OSError, ValueError, FunnelError) as e:  # ssl and urllib errors are OSError
        return str(e) or type(e).__name__
    return None if " 200 " in status else f"ответ {status!r}"


def repair(port: int) -> None:
    """Re-apply Funnel from scratch: makes the coordination server re-announce this node."""
    for args in (["reset"], ["--bg", str(port)]):
        try:
            proc = subprocess.run(["tailscale", "funnel", *args], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise FunnelError(f"tailscale не запускается: {e}") from e
        if proc.returncode:
            error = (proc.stderr or proc.stdout).strip()[-300:]
            raise FunnelError(f"{error}\n{OPERATOR_HINT}" if "denied" in error.lower() else error)
