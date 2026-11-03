"""node-image: pinned versions, a reproducible agent binary, and the host hardening it installs.

The provision script itself only runs on a real Ubuntu 24.04 node (docs/10 M2.4); here its files
are checked statically and with the real tools that exist on a developer machine or CI
(shellcheck, nft, the Go toolchain). The Telemt hashes in versions.lock were taken from the
official release asset and match the release's own .sha256 files (2026-11-03); tests do not
download anything.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
from test_agent_support import REPO, requires_go, run_tool

NODE_IMAGE = REPO / "node-image"
FILES = NODE_IMAGE / "files"
HEX64 = re.compile(r"[0-9a-f]{64}")

# The panel's test container holds only panel/; node-image is checked from a full checkout.
pytestmark = pytest.mark.skipif(not NODE_IMAGE.is_dir(), reason="node-image/ is not in this checkout")


def lock() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (NODE_IMAGE / "versions.lock").read_text().splitlines():
        if not line or line.startswith("#"):
            continue
        key, _, value = line.partition("=")
        assert re.fullmatch(r"[A-Z0-9_]+", key) and re.fullmatch(r"[A-Za-z0-9._:/+-]+", value), line
        assert key not in values, f"duplicate {key}"
        values[key] = value
    return values


def test_versions_lock_pins_every_artifact() -> None:
    v = lock()
    assert v["TELEMT_VERSION"] == "3.5.16"
    assert v["TELEMT_URL"] == (
        "https://github.com/telemt/telemt/releases/download/3.5.16/telemt-x86_64-linux-gnu.tar.gz"
    )
    for key in ("TELEMT_TARBALL_SHA256", "TELEMT_BINARY_SHA256", "AGENT_SHA256"):
        assert HEX64.fullmatch(v[key]), key
    assert v["AGENT_SHA256"] != "0" * 64
    assert re.fullmatch(r"\d+\.\d+\.\d+", v["AGENT_VERSION"]) and re.fullmatch(
        r"\d+\.\d+\.\d+", v["AGENT_GO_VERSION"]
    )
    # The agent reports the Telemt version the image pins (cloud-init template).
    template = (NODE_IMAGE / "cloud-init" / "node-user-data.yaml.tmpl").read_text()
    assert f'"telemt_version": "{v["TELEMT_VERSION"]}"' in template


@requires_go
def test_agent_binary_is_reproducible_and_pinned(tmp_path: Path) -> None:
    v = lock()
    go_version = subprocess.run(  # noqa: S603
        [shutil.which("go") or "go", "env", "GOVERSION"], capture_output=True, text=True, check=True
    ).stdout.strip()
    if go_version != f"go{v['AGENT_GO_VERSION']}":
        pytest.skip(f"pinned toolchain is go{v['AGENT_GO_VERSION']}, this machine has {go_version}")
    built = run_tool("bash", NODE_IMAGE / "build-agent.sh", tmp_path / "proxyhub-agent", timeout=600)
    assert built.returncode == 0, built.stderr
    digest = built.stdout.split()[0]
    assert digest == v["AGENT_SHA256"], (
        "agent/ changed: rebuild with node-image/build-agent.sh and update AGENT_SHA256 in versions.lock"
    )


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="needs shellcheck")
def test_scripts_pass_shellcheck() -> None:
    result = run_tool("shellcheck", NODE_IMAGE / "provision.sh", NODE_IMAGE / "build-agent.sh")
    assert result.returncode == 0, result.stdout


@pytest.mark.skipif(shutil.which("nft") is None, reason="needs nft")
def test_nftables_ruleset_is_valid(tmp_path: Path) -> None:
    result = run_tool("nft", "-c", "-f", FILES / "nftables.conf")
    if result.returncode != 0 and "Operation not permitted" in result.stderr:
        pytest.skip("nft -c needs CAP_NET_ADMIN here")
    assert result.returncode == 0, result.stderr


def test_firewall_blocks_mail_private_ranges_and_metadata() -> None:
    nft = (FILES / "nftables.conf").read_text()
    assert "tcp dport { 25, 465, 587 } counter drop" in nft
    for net in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",
        "169.254.0.0/16",
        "fc00::/7",
    ):
        assert net in nft
    assert "ip daddr 169.254.169.254 counter drop" in nft
    assert "tcp dport { 22, 9090, 9091 } counter drop" in nft
    rules = [line.split("#", 1)[0] for line in nft.splitlines()]
    assert not [r for r in rules if re.search(r"\blog\b", r)]  # no rule logs packets (addresses)
    lockdown = (FILES / "proxyhub-lockdown.service").read_text()
    assert "After=cloud-final.service" in lockdown and "flush chain inet proxyhub boot_window" in lockdown


def test_telemt_unit_never_runs_unconfigured_or_logs() -> None:
    unit = (FILES / "telemt.service").read_text()
    for line in (
        "ConditionPathExists=/etc/telemt/telemt.toml",
        "User=telemt",
        "StandardOutput=null",
        "StandardError=null",
        "UnsetEnvironment=RUST_LOG",
        "NoNewPrivileges=true",
        "CapabilityBoundingSet=CAP_NET_BIND_SERVICE",
        "LimitCORE=0",
        "ExecReload=/bin/kill -HUP $MAINPID",
    ):
        assert line in unit.splitlines(), line
    assert "CAP_NET_ADMIN" not in unit


def test_host_hardening_files() -> None:
    journald = (FILES / "journald-proxyhub.conf").read_text().splitlines()
    assert "Storage=volatile" in journald and "ForwardToSyslog=no" in journald
    sysctl = (FILES / "sysctl-proxyhub.conf").read_text().splitlines()
    for line in (
        "net.core.default_qdisc = fq",
        "net.ipv4.tcp_congestion_control = bbr",
        "kernel.core_pattern = |/bin/false",
    ):
        assert line in sysctl
    provision = (NODE_IMAGE / "provision.sh").read_text()
    for snippet in (
        "apt-get purge -y -q openssh-server rsyslog",
        "systemctl mask ssh.service ssh.socket",
        'check_sha256 "$WORK/telemt.tar.gz" "$(lock TELEMT_TARBALL_SHA256)"',
        'check_sha256 "$WORK/x/telemt" "$(lock TELEMT_BINARY_SHA256)"',
        'check_sha256 "$AGENT_BINARY" "$(lock AGENT_SHA256)"',
        "rm -rf /var/log/journal",
        "tcp_bbr",
    ):
        assert snippet in provision, snippet
    template = (NODE_IMAGE / "cloud-init" / "node-user-data.yaml.tmpl").read_text()
    token_block = template.split("registration_token\n", 1)[1]
    assert "permissions: '0600'" in token_block.split("- path:", 1)[0]
