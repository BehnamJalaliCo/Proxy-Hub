"""Rules from CLAUDE.md enforced on the source tree itself.

Each rule has a negative control (a deliberately bad snippet) so we know the check works.
"""

from __future__ import annotations

import re
from pathlib import Path

PANEL = Path(__file__).resolve().parents[1] / "proxyhub"
REPO = PANEL.parents[1]

# Rule 6: posts are never deleted. No Telegram delete call may exist anywhere in the code.
TG_DELETE = re.compile(r"delete_?messages?\b|deleteMessages?\b", re.IGNORECASE)
# Rule 5: only proxyhub/hetzner/mutations.py may create or delete servers.
HCLOUD_MUTATION = re.compile(r"\.servers\.(create|delete|rebuild)\(|\bdelete_server\(|\bcreate_server\(")
# Rule 7: never call APIs that return user IPs (Xray online-IP stats, telemt IP lists).
IP_APIS = re.compile(
    r"GetStatsOnlineIpList|GetUsersStats|statsUserOnline\"?\s*[:=]\s*true|"
    r"/v1/stats/users/active-ips|/v1/users\b|active_ips|unique_ips_list",
    re.IGNORECASE,
)
ALLOWED_MUTATION_FILES = {PANEL / "hetzner" / "mutations.py"}


def _sources() -> list[Path]:
    return [p for p in PANEL.rglob("*.py") if "migrations" not in p.parts]


def _strip_comments(text: str) -> str:
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_negative_controls_match() -> None:
    assert TG_DELETE.search("await bot.delete_message(chat, 1)")
    assert TG_DELETE.search('requests.post(url + "/deleteMessage")')
    assert HCLOUD_MUTATION.search("client.servers.delete(server)")
    assert IP_APIS.search('httpx.get("http://127.0.0.1:9091/v1/users")')
    assert IP_APIS.search('"statsUserOnline": true')


def test_no_telegram_delete_anywhere() -> None:
    hits = [p for p in _sources() if TG_DELETE.search(_strip_comments(p.read_text(encoding="utf-8")))]
    assert not hits, f"rule 6 violated in {hits}"


def test_server_mutations_only_in_mutations_module() -> None:
    hits = [
        p
        for p in _sources()
        if p not in ALLOWED_MUTATION_FILES
        and HCLOUD_MUTATION.search(_strip_comments(p.read_text(encoding="utf-8")))
    ]
    assert not hits, f"rule 5 violated: server create/delete outside mutations.py in {hits}"


def test_no_user_ip_apis() -> None:
    roots = [PANEL, REPO / "agent", REPO / "node-image"]
    files = [
        p for r in roots if r.exists() for p in r.rglob("*") if p.suffix in {".py", ".go", ".toml", ".json"}
    ]
    hits = [p for p in files if IP_APIS.search(_strip_comments(p.read_text(encoding="utf-8")))]
    assert not hits, f"rule 7 violated in {hits}"
