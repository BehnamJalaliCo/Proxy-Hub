"""cloud-init user data of a node: rendered from the committed template and node-image files.

Checked with the real tools: PyYAML (the parser cloud-init itself uses), the cloud-init package of
Ubuntu 24.04 (strict schema validation, and its own write_files decoding, which shows the node gets
byte-identical files), shellcheck on the embedded scripts, and the size limit Hetzner enforces.
"""

from __future__ import annotations

import base64
import gzip
import json
import logging
import secrets
import shutil
import uuid
from pathlib import Path

import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric import ec
from test_agent_support import run_tool
from test_live_support import (
    AGENT_DOMAIN,
    CLOUD_INIT_CHECK,
    NODE_IMAGE,
    cloud_init_python,
    requires_node_image,
)

from proxyhub.nodes.cloud_init import MAX_USER_DATA_BYTES, UserData, UserDataError, render_user_data
from proxyhub.nodes.image import NodeImage, NodeImageError, load_node_image, parse_versions_lock
from proxyhub.pki.ca import build_ca_certificate, to_pem

pytestmark = requires_node_image


@pytest.fixture(scope="module")
def image() -> NodeImage:
    return load_node_image(NODE_IMAGE)


@pytest.fixture(scope="module")
def ca_pem() -> str:
    return to_pem(build_ca_certificate(ec.generate_private_key(ec.SECP256R1())))


def render(image: NodeImage, ca_pem: str, **kw: object) -> tuple[UserData, str, uuid.UUID]:
    token = str(kw.pop("token", secrets.token_urlsafe(32)))
    node_id = uuid.UUID(str(kw.pop("node_id", uuid.uuid4())))
    domain = str(kw.pop("agent_domain", AGENT_DOMAIN))
    return (
        render_user_data(image, node_id=node_id, agent_domain=domain, token=token, ca_pem=ca_pem),
        token,
        node_id,
    )


def test_user_data_carries_the_reviewed_files_and_the_node_identity(image: NodeImage, ca_pem: str) -> None:
    ud, token, node_id = render(image, ca_pem)
    assert ud.text.startswith("#cloud-config\n")
    assert 0 < ud.size <= MAX_USER_DATA_BYTES == 32 * 1024
    doc = yaml.safe_load(ud.text)
    assert set(doc) == {"ssh_pwauth", "write_files", "runcmd"} and doc["ssh_pwauth"] is False
    files = {f["path"]: f for f in doc["write_files"]}
    expected = {
        "/opt/proxyhub-node/versions.lock": (image.root / "versions.lock").read_bytes(),
        "/opt/proxyhub-node/provision.sh": (image.root / "provision.sh").read_bytes(),
        **{f"/opt/proxyhub-node/files/{p.name}": p.read_bytes() for p in (image.root / "files").iterdir()},
    }
    for path, content in expected.items():
        entry = files[path]
        assert entry["encoding"] == "gz+b64" and entry["owner"] == "root:root"

        assert gzip.decompress(base64.b64decode(entry["content"])) == content, path
    assert files["/opt/proxyhub-node/provision.sh"]["permissions"] == "0700"
    agent = json.loads(files["/etc/proxyhub-agent/agent.json"]["content"])
    assert agent["panel_url"] == f"https://{AGENT_DOMAIN}" and agent["node_id"] == str(node_id)
    assert agent["telemt_version"] == image.lock["TELEMT_VERSION"]
    assert files["/etc/proxyhub-agent/agent.json"]["permissions"] == "0600"
    assert base64.b64decode(files["/etc/proxyhub-agent/ca.pem"]["content"]).decode() == ca_pem
    tok = files["/etc/proxyhub-agent/registration_token"]
    assert tok["permissions"] == "0600" and base64.b64decode(tok["content"]).decode() == token
    assert doc["runcmd"] == [
        [
            "/bin/bash",
            "/opt/proxyhub-node/provision.sh",
            "--agent-url",
            f"https://{AGENT_DOMAIN}/agent/v1/artifacts/proxyhub-agent",
            "--start",
        ]
    ]
    # Exactly the files node-image/ holds: a new host file can not be forgotten.
    assert set(files) == set(expected) | {
        "/etc/proxyhub-agent/agent.json",
        "/etc/proxyhub-agent/ca.pem",
        "/etc/proxyhub-agent/registration_token",
    }


def test_rendering_is_deterministic_except_for_the_node(image: NodeImage, ca_pem: str) -> None:
    node_id = uuid.uuid4()
    token = secrets.token_urlsafe(32)
    a, _, _ = render(image, ca_pem, node_id=node_id, token=token)
    b, _, _ = render(image, ca_pem, node_id=node_id, token=token)
    assert a.text == b.text  # gzip without timestamps: reviewable, comparable


def test_user_data_never_shows_itself(
    image: NodeImage, ca_pem: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    ud, token, _ = render(image, ca_pem)
    token_b64 = base64.b64encode(token.encode()).decode()
    logging.getLogger("proxyhub.test").info("rendered %s / %r / %s", ud, ud, [ud])
    for shown in (repr(ud), str(ud), caplog.text, f"{ud}"):
        assert token not in shown and token_b64 not in shown and "#cloud-config" not in shown
    import pickle

    with pytest.raises(TypeError):
        pickle.dumps(ud)


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"agent_domain": "10.0.0.1"}, UserDataError),
        ({"agent_domain": "localhost"}, UserDataError),
        ({"agent_domain": "agent.example.com\n  - path: /etc/x"}, UserDataError),
        ({"agent_domain": "Agent.Example.com"}, UserDataError),
        ({"token": "short"}, UserDataError),
        ({"token": "a" * 42 + "'"}, UserDataError),
    ],
)
def test_values_that_could_break_the_yaml_are_refused(
    image: NodeImage, ca_pem: str, change: dict[str, str], error: type[Exception]
) -> None:
    with pytest.raises(error):
        render(image, ca_pem, **change)


def test_ca_must_be_one_pem_certificate(image: NodeImage, ca_pem: str) -> None:
    with pytest.raises(UserDataError):
        render(image, "not a pem")
    with pytest.raises(UserDataError):
        render(image, ca_pem + ca_pem)


def test_template_telemt_version_must_match_the_lock(image: NodeImage, ca_pem: str) -> None:
    stale = NodeImage(
        root=image.root,
        lock={**image.lock, "TELEMT_VERSION": "3.5.17"},
        versions_lock=image.versions_lock,
        provision_sh=image.provision_sh,
        files=image.files,
        template=image.template,
    )
    with pytest.raises(NodeImageError):
        render(stale, ca_pem)


def test_oversized_user_data_is_refused(image: NodeImage, ca_pem: str) -> None:
    big = NodeImage(
        root=image.root,
        lock=image.lock,
        versions_lock=image.versions_lock,
        provision_sh=image.provision_sh + secrets.token_bytes(40 * 1024),  # incompressible
        files=image.files,
        template=image.template,
    )
    with pytest.raises(UserDataError, match="32768"):
        render(big, ca_pem)


def test_versions_lock_rules() -> None:
    lock = (NODE_IMAGE / "versions.lock").read_text()
    assert parse_versions_lock(lock)["TELEMT_VERSION"] == "3.5.16"
    for bad in (
        lock + "AGENT_SHA256=" + "0" * 64 + "\n",  # duplicate key
        lock.replace("AGENT_SHA256=", "AGENT_SHA256=x"),  # not a sha256
        lock.replace("TELEMT_URL=https://github.com/telemt/", "TELEMT_URL=https://example.com/telemt/"),
        "\n".join(line for line in lock.splitlines() if not line.startswith("AGENT_SHA256")),
        lock + "SPACE = value\n",
    ):
        with pytest.raises(NodeImageError):
            parse_versions_lock(bad)


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="needs shellcheck")
def test_embedded_scripts_pass_shellcheck(image: NodeImage, ca_pem: str, tmp_path: Path) -> None:
    ud, _, _ = render(image, ca_pem)

    for entry in yaml.safe_load(ud.text)["write_files"]:
        if entry["path"].endswith(".sh"):
            script = tmp_path / Path(entry["path"]).name
            script.write_bytes(gzip.decompress(base64.b64decode(entry["content"])))
            result = run_tool("shellcheck", script)
            assert result.returncode == 0, result.stdout
    # provision.sh knows --agent-url and only accepts the agent site's artifact URL.
    text = (image.root / "provision.sh").read_text()
    assert "--agent-url) [ $# -ge 2 ] || usage; AGENT_URL=" in text
    assert "https://*/agent/v1/artifacts/proxyhub-agent) ;;" in text
    assert 'check_sha256 "$AGENT_BINARY" "$(lock AGENT_SHA256)"' in text


def test_provision_refuses_bad_arguments_before_doing_anything(tmp_path: Path) -> None:
    """Argument checks run first (no root, no Ubuntu needed), so they are tested for real."""
    script = NODE_IMAGE / "provision.sh"
    for args in (
        [],
        ["--agent-url", "http://agent.example.com/agent/v1/artifacts/proxyhub-agent"],
        ["--agent-url", "https://agent.example.com/other"],
        [
            "--agent-binary",
            str(tmp_path / "x"),
            "--agent-url",
            "https://a.example.com/agent/v1/artifacts/proxyhub-agent",
        ],
        ["--bogus"],
    ):
        result = run_tool("bash", script, *args)
        assert result.returncode == 2, (args, result.stdout, result.stderr)


def test_user_data_passes_the_real_cloud_init(image: NodeImage, ca_pem: str, tmp_path: Path) -> None:
    """Ubuntu 24.04's own cloud-init: strict schema validation, and its write_files decoding."""
    runner = cloud_init_python()
    if runner is None:
        pytest.skip(
            "cloud-init not importable (it is on every Ubuntu server; elsewhere unpack the Ubuntu "
            "24.04 cloud-init .deb and set PROXYHUB_CLOUD_INIT_PYTHONPATH)"
        )
    python, extra = runner
    ud, token, _ = render(image, ca_pem)
    doc = tmp_path / "user-data"
    doc.write_text(ud.text)
    checker = tmp_path / "check.py"
    checker.write_text(CLOUD_INIT_CHECK)
    result = run_tool(python, "-I", checker, extra, doc)
    assert result.returncode == 0, result.stderr[-2000:]
    out = json.loads(result.stdout)
    files = out["files"]
    assert (
        bytes.fromhex(files["/opt/proxyhub-node/provision.sh"]["hex"])
        == (image.root / "provision.sh").read_bytes()
    )
    assert files["/opt/proxyhub-node/provision.sh"]["mode"] == 0o700
    for name, data in image.files:
        assert bytes.fromhex(files[f"/opt/proxyhub-node/files/{name}"]["hex"]) == data, name
    assert bytes.fromhex(files["/etc/proxyhub-agent/registration_token"]["hex"]).decode() == token
    assert files["/etc/proxyhub-agent/registration_token"]["mode"] == 0o600
    agent = json.loads(bytes.fromhex(files["/etc/proxyhub-agent/agent.json"]["hex"]))
    assert agent["metrics_listen"] == "127.0.0.1:9090"

    # Negative control: the same validator refuses a document with a wrong type.
    broken = tmp_path / "broken"
    broken.write_text(ud.text.replace("ssh_pwauth: false", "ssh_pwauth: [1, 2]"))
    assert run_tool(python, "-I", checker, extra, broken).returncode != 0
