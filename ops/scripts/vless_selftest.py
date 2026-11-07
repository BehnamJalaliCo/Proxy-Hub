#!/usr/bin/env python3
"""Self-test of the channel's VLESS links from the panel server (outside Iran), for P29.

Each active link (TCP, XHTTP or WS) is used exactly as a user's app would use it: a local Xray
client (the release pinned in node-image/versions.lock, fetched and checked by
node-image/fetch-xray.sh) with a SOCKS inbound on 127.0.0.1, then curl through it:

* ``204``: https://www.google.com/generate_204 (does anything pass at all);
* ``down``: 20 MB from speed.cloudflare.com (download, Mbit/s);
* ``up``: 10 MB to speed.cloudflare.com (upload, Mbit/s).

A link that works here and not from Iran is blocked or throttled in Iran (the config itself is
right); a link that fails here too is broken on our side (key, user, inbound, DNS name).

Input: JSON lines ``{"key": ..., "slot": ..., "url": "vless://..."}`` on stdin (P29 prints them from
the panel's own link provider). Output: one line per link with the slot, transport, port, server
name and the three results, and the last Xray warning with every IPv4 shortened to its last two
parts and every uuid removed. No uuid, key, short id or full address is printed; the client
configs live in a private temporary directory that is removed at the end.

    python3 ops/scripts/vless_selftest.py --xray DIR/xray < links.jsonl
    python3 ops/scripts/vless_selftest.py --dry < links.jsonl   # only parse; print masked configs
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

DOWN_URL = "https://speed.cloudflare.com/__down?bytes=20000000"
UP_URL = "https://speed.cloudflare.com/__up"
PING_URL = "https://www.google.com/generate_204"
UP_BYTES = 10_000_000
_IPV4 = re.compile(r"\b(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})\b")
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)


def parse_vless(url: str) -> dict[str, Any]:
    """The parts of a ``vless://`` link this panel makes (proxyhub/reality/links.py)."""
    parts = urlsplit(url)
    if parts.scheme != "vless" or not parts.hostname or not parts.port or not parts.username:
        raise ValueError("not a vless link")
    q = {k: v[0] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
    return {
        "uuid": unquote(parts.username),
        "host": parts.hostname,
        "port": int(parts.port),
        "security": q.get("security", "none"),
        "type": q.get("type", "tcp"),
        "sni": q.get("sni", ""),
        "fp": q.get("fp", "chrome"),
        "pbk": q.get("pbk", ""),
        "sid": q.get("sid", ""),
        "flow": q.get("flow", ""),
        "path": q.get("path", "/"),
        "mode": q.get("mode", "auto"),
        "ws_host": q.get("host", ""),
        "alpn": [a for a in q.get("alpn", "").split(",") if a],
    }


def client_config(link: dict[str, Any], socks_port: int) -> dict[str, Any]:
    """An Xray client config: SOCKS on 127.0.0.1:socks_port, one VLESS outbound for the link."""
    user: dict[str, Any] = {"id": link["uuid"], "encryption": "none"}
    if link["flow"]:
        user["flow"] = link["flow"]
    stream: dict[str, Any] = {"network": link["type"], "security": link["security"]}
    if link["security"] == "reality":
        stream["realitySettings"] = {
            "serverName": link["sni"],
            "fingerprint": link["fp"],
            "publicKey": link["pbk"],
            "shortId": link["sid"],
        }
    elif link["security"] == "tls":
        tls: dict[str, Any] = {"serverName": link["sni"], "fingerprint": link["fp"]}
        if link["alpn"]:
            tls["alpn"] = link["alpn"]
        stream["tlsSettings"] = tls
    if link["type"] == "xhttp":
        stream["xhttpSettings"] = {"path": link["path"], "mode": link["mode"]}
    elif link["type"] == "ws":
        stream["wsSettings"] = {"path": link["path"], "host": link["ws_host"] or link["host"]}
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"listen": "127.0.0.1", "port": socks_port, "protocol": "socks", "settings": {"udp": False}}
        ],
        "outbounds": [
            {
                "protocol": "vless",
                "settings": {"vnext": [{"address": link["host"], "port": link["port"], "users": [user]}]},
                "streamSettings": stream,
            }
        ],
    }


def mask(text: str) -> str:
    text = _UUID.sub("<uuid>", text)
    return _IPV4.sub(lambda m: f"…{m.group(3)}.{m.group(4)}", text)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_port(port: int, seconds: float) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.2)
    return False


def _curl(port: int, url: str, *, upload: str | None = None, seconds: int = 40) -> str:
    cmd = ["curl", "-s", "-o", "/dev/null", "--max-time", str(seconds), "-x", f"socks5h://127.0.0.1:{port}"]
    if upload is None:
        cmd += ["-w", "%{http_code} %{speed_download} %{time_total}", url]
    else:
        cmd += ["-w", "%{http_code} %{speed_upload} %{time_total}", "--data-binary", f"@{upload}", url]
    # A fixed argument list, no shell: the only inputs are our own port, file and fixed URLs.
    out = subprocess.run(cmd, capture_output=True, text=True, check=False).stdout.strip()  # noqa: S603
    try:
        code, speed, total = out.split()
        mbit = float(speed) * 8 / 1_000_000
        return f"{code} {mbit:.1f}Mbit {float(total):.1f}s"
    except ValueError:
        return f"curl:{out or 'no answer'}"


def run_one(xray: str, work: str, row: dict[str, Any], upfile: str) -> str:
    link = parse_vless(str(row["url"]))
    port = _free_port()
    cfg = os.path.join(work, f"c{port}.json")
    with open(os.open(cfg, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
        json.dump(client_config(link, port), f)
    log = os.path.join(work, f"c{port}.log")
    head = f"slot={row.get('slot')} {link['type']}:{link['port']} sec={link['security']} sni={link['sni']}"
    with open(log, "w") as lf:
        proc = subprocess.Popen([xray, "run", "-c", cfg], stdout=lf, stderr=subprocess.STDOUT)  # noqa: S603
    try:
        if not _wait_port(port, 8):
            return f"{head} | xray did not start"
        ping = _curl(port, PING_URL, seconds=20)
        down = _curl(port, DOWN_URL)
        up = _curl(port, UP_URL, upload=upfile)
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
    with open(log) as lf:
        warn = [line.strip() for line in lf if "[Warning]" in line or "[Error]" in line or "failed" in line]
    last = mask(warn[-1])[-160:] if warn else "-"
    return f"{head} | 204: {ping} | down: {down} | up: {up} | xray: {last}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xray", help="path of the pinned xray binary")
    ap.add_argument("--dry", action="store_true", help="only parse the links and print masked configs")
    args = ap.parse_args()
    rows = [json.loads(line) for line in sys.stdin if line.strip()]
    if args.dry:
        for row in rows:
            cfg = client_config(parse_vless(str(row["url"])), 10808)
            print(mask(json.dumps(cfg["outbounds"][0]["streamSettings"], sort_keys=True)))
        return 0
    if not args.xray or not os.access(args.xray, os.X_OK):
        print("--xray must be the pinned xray binary (node-image/fetch-xray.sh)", file=sys.stderr)
        return 2
    if shutil.which("curl") is None:
        print("curl is missing", file=sys.stderr)
        return 2
    work = tempfile.mkdtemp(prefix="ph-selftest-")
    os.chmod(work, 0o700)
    try:
        upfile = os.path.join(work, "up.bin")
        with open(upfile, "wb") as f:
            f.write(os.urandom(UP_BYTES))
        print(f"{len(rows)} link(s); tests: {PING_URL} | {DOWN_URL} | {UP_URL} ({UP_BYTES} bytes)")
        for row in rows:
            try:
                print(run_one(args.xray, work, row, upfile), flush=True)
            except ValueError as exc:
                print(f"slot={row.get('slot')} | bad link: {exc}", flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
