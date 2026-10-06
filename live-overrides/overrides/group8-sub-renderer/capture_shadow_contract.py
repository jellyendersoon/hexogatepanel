#!/usr/bin/env python3
"""Capture subscription contracts through a chosen Unix socket.

Bodies are written mode 0600 and no token, URL, or body is printed.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import urllib.parse


USERS = {
    "pure-g8": 27,
    "pure-g1": 167,
    "pure-g7": 437,
    "multi-5-8-10": 45411,
    "broad-multi": 137,
    "no-group": 54699,
}
MANUAL_FORMATS = (
    "links",
    "links_base64",
    "xray",
    "wireguard",
    "sing_box",
    "clash",
    "clash_meta",
    "outline",
)
AUTO_CLIENTS = {
    "auto-happ": "Happ/3.4.1",
    "auto-v2box": "V2Box/1.11.8",
    "auto-streisand": "Streisand/1.6.50",
    "auto-sfa": "SFA/1.12.0",
}


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str, timeout: int = 120):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self.socket_path)


def mint_admin_token() -> str:
    code = (
        "import asyncio; from app.utils.jwt import create_admin_token; "
        "print(asyncio.run(create_admin_token(1, 'jellyenderson')))"
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "exec",
            "-T",
            "pasarguard",
            "/code/.venv/bin/python",
            "-c",
            code,
        ],
        cwd="/opt/pasarguard",
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip().splitlines()[-1]


def api_get(auth: str, path: str) -> dict:
    connection = UnixHTTPConnection("/var/lib/pasarguard/pasarguard.socket", 60)
    connection.request("GET", path, headers={"Authorization": f"Bearer {auth}"})
    response = connection.getresponse()
    body = response.read()
    connection.close()
    if response.status >= 400:
        raise RuntimeError(f"API GET returned {response.status}")
    return json.loads(body)


def request_path(url: str, suffix: str | None = None) -> str:
    parsed = urllib.parse.urlsplit(url)
    path = parsed.path.rstrip("/")
    if suffix:
        path += f"/{suffix}"
    return urllib.parse.urlunsplit(("", "", path, parsed.query, ""))


def fetch(socket_path: str, path: str, user_agent: str) -> tuple[int, str, bytes]:
    connection = UnixHTTPConnection(socket_path)
    connection.request(
        "GET",
        path,
        headers={
            "Accept": "*/*",
            "Host": "panel.hexogate.net",
            "User-Agent": user_agent,
            "X-Forwarded-For": "127.0.0.1",
            "X-Forwarded-Proto": "https",
        },
    )
    response = connection.getresponse()
    body = response.read()
    status = response.status
    content_type = response.getheader("Content-Type", "")
    connection.close()
    return status, content_type, body


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit("usage: capture_shadow_contract.py SOCKET OUTPUT_DIRECTORY")
    socket_path = sys.argv[1]
    output = Path(sys.argv[2]).resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    auth = mint_admin_token()
    summary: list[dict[str, object]] = []
    for user_class, user_id in USERS.items():
        user = api_get(auth, f"/api/user/by-id/{user_id}")
        base_path = request_path(user["subscription_url"])
        user_dir = output / user_class
        user_dir.mkdir(mode=0o700)
        cases = [
            (name, request_path(user["subscription_url"], name), "curl/8.10")
            for name in MANUAL_FORMATS
        ]
        cases.extend((name, base_path, agent) for name, agent in AUTO_CLIENTS.items())
        for name, path, user_agent in cases:
            status, content_type, body = fetch(socket_path, path, user_agent)
            body_path = user_dir / f"{name}.body"
            body_path.write_bytes(body)
            body_path.chmod(0o600)
            summary.append(
                {
                    "user_class": user_class,
                    "case": name,
                    "status": status,
                    "content_type": content_type,
                    "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
            )
    write_json(output / "summary.json", summary)
    failures = [row for row in summary if row["status"] != 200]
    print(
        json.dumps(
            {
                "users": len(USERS),
                "requests": len(summary),
                "passed": len(summary) - len(failures),
                "failed": len(failures),
            },
            separators=(",", ":"),
        )
    )
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    os.umask(0o077)
    main()
