"""Shared helpers for the content engine's tests (decision D51).

* :class:`LocalSite`: a real HTTPS server on 127.0.0.1 with a throwaway CA, answering by host name
  and path from a table the test fills. :func:`site_http` gives a :class:`SafeHttp` that resolves the
  test's names (``*.example``) to it and treats 127.0.0.1 as the "public internet", while any other
  address (10.0.0.5, 169.254.169.254) stays forbidden exactly as in production. Nothing external is
  imitated: these are our own pages and feeds.
* Real files: :func:`png`, :func:`jpeg_header`, :func:`mp4` build valid PNG, JPEG and MP4 bytes;
  :func:`real_mp4` makes a real, playable MP4 with ffmpeg itself (:func:`need_ffmpeg` skips a test
  without ffmpeg, or fails it where ``PROXYHUB_REQUIRE_FFMPEG=1``: CI and the test image).
"""

from __future__ import annotations

import asyncio
import functools
import os
import shutil
import ssl
import struct
import subprocess
import tempfile
import zlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from proxyhub.content.net import SafeHttp

CLEAN_SQL = (
    "DELETE FROM content_qc_calls",
    "DELETE FROM settings WHERE key LIKE 'content.qc%'",
    "DELETE FROM secrets WHERE name = 'anthropic_api_key'",
    "DELETE FROM content_media",
    "UPDATE posts SET content_candidate_id = NULL",
    "DELETE FROM content_candidates",
    "DELETE FROM content_sources",
)


async def clean_content_tables(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        for sql in CLEAN_SQL:
            await conn.execute(text(sql))


# --- files ----------------------------------------------------------------------------------------


def png(width: int = 4, height: int = 3, seed: int = 0) -> bytes:
    """A real PNG (RGB, every pixel ``seed``)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + bytes([seed % 256, 10, 20]) * width for _ in range(height))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    )


def jpeg_header(width: int, height: int) -> bytes:
    """The start of a baseline JPEG up to its SOF0 marker (what the size check reads), then EOI."""
    app0 = b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof0 = (
        b"\xff\xc0\x00\x11\x08"
        + struct.pack(">HH", height, width)
        + b"\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    )
    return b"\xff\xd8" + app0 + sof0 + b"\xff\xd9"


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def mp4(width: int = 640, height: int = 360, seconds: int = 12, brand: bytes = b"isom") -> bytes:
    """An ISO base media file: ftyp, moov (mvhd with the duration, a trak whose tkhd has the
    picture size) and an mdat."""
    ftyp = _box(b"ftyp", brand + b"\x00\x00\x02\x00" + brand + b"mp41")
    mvhd = _box(
        b"mvhd", b"\x00\x00\x00\x00" + struct.pack(">IIII", 0, 0, 1000, seconds * 1000) + b"\x00" * 80
    )
    tkhd_body = b"\x00\x00\x00\x07" + b"\x00" * 72 + struct.pack(">II", width << 16, height << 16)
    trak = _box(b"trak", _box(b"tkhd", tkhd_body))
    return ftyp + _box(b"moov", mvhd + trak) + _box(b"mdat", b"\x00" * 64)


def need_ffmpeg() -> None:
    """Skip without ffmpeg/ffprobe; where PROXYHUB_REQUIRE_FFMPEG=1 (CI, the test image) fail instead."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    if os.environ.get("PROXYHUB_REQUIRE_FFMPEG") == "1":
        pytest.fail("ffmpeg/ffprobe are required here (PROXYHUB_REQUIRE_FFMPEG=1) but not installed")
    pytest.skip("ffmpeg/ffprobe not installed")


@functools.cache
def real_mp4(seconds: float = 8, width: int = 320, height: int = 180, rate: int = 5) -> bytes:
    """A real MP4 made by ffmpeg itself: a moving test pattern (MPEG-4 Part 2, ffmpeg's own encoder,
    so no external codec library is needed) and a silent AAC track, its index at the start."""
    need_ffmpeg()
    with tempfile.TemporaryDirectory(prefix="phv-") as tmp:
        out = os.path.join(tmp, "v.mp4")
        subprocess.run(  # noqa: S603 - fixed argv, our own output path
            [
                shutil.which("ffmpeg") or "ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"testsrc2=size={width}x{height}:rate={rate}:duration={seconds}",
                "-f",
                "lavfi",
                "-i",
                f"anullsrc=r=8000:cl=mono:d={seconds}",
                "-c:v",
                "mpeg4",
                "-q:v",
                "8",
                "-c:a",
                "aac",
                "-b:a",
                "16k",
                "-shortest",
                "-movflags",
                "+faststart",
                "-f",
                "mp4",
                out,
            ],
            check=True,
            timeout=120,
            stdin=subprocess.DEVNULL,
        )
        with open(out, "rb") as f:
            return f.read()


# --- a local HTTPS site ---------------------------------------------------------------------------


def tls_material(directory: Path, names: list[str]) -> tuple[Path, Path, Path]:
    """A throwaway CA and one server certificate for ``names``. Returns (ca.pem, cert.pem, key.pem)."""
    now = datetime.now(UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "content-test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path, cert_path, key_path = directory / "ca.pem", directory / "cert.pem", directory / "key.pem"
    ca_path.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    cert_path.write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    return ca_path, cert_path, key_path


@dataclass
class Page:
    status: int = 200
    body: bytes = b""
    headers: Mapping[str, str] = field(default_factory=dict)
    send_length: bool = True  # False: no Content-Length, the body ends when the connection closes


Handler = Callable[[], Page]
SITE_NAMES = ["news.example", "cdn.example", "mixed.example", "evil.example"]


class LocalSite:
    """A real TLS server; ``pages[(host, path)]`` is the answer (a Page, or a callable giving one)."""

    def __init__(self, directory: Path) -> None:
        self.ca, cert, key = tls_material(directory, SITE_NAMES)
        self._ssl = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self._ssl.load_cert_chain(cert, key)
        self.pages: dict[tuple[str, str], Page | Handler] = {}
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> LocalSite:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=self._ssl)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    def client_ssl(self) -> ssl.SSLContext:
        return ssl.create_default_context(cafile=str(self.ca))

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ssl.SSLError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        _, target, _ = lines[0].split(" ", 2)
        headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in lines[1:] if h)}
        host = headers.get("host", "").split(":")[0]
        self.requests.append((host, target, headers))
        found = self.pages.get((host, target))
        page = found() if callable(found) else (found or Page(404, b"not here"))
        out = [f"HTTP/1.1 {page.status} X", "Connection: close"]
        if page.send_length and not any(k.lower() == "content-length" for k in page.headers):
            out.append(f"Content-Length: {len(page.body)}")
        out += [f"{k}: {v}" for k, v in page.headers.items()]
        writer.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + page.body)
        try:
            await writer.drain()
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, ssl.SSLError):
            pass


ADDRESSES = {
    "news.example": ["127.0.0.1"],
    "cdn.example": ["127.0.0.1"],
    "mixed.example": ["127.0.0.1", "10.1.1.1"],
    "evil.example": ["10.0.0.5"],
    "metadata.example": ["169.254.169.254"],
}


async def resolve_for_tests(host: str, port: int) -> list[str]:
    return list(ADDRESSES.get(host, []))


def site_http(site: LocalSite) -> SafeHttp:
    """SafeHttp for the local site: only 127.0.0.1 counts as public here; everything else is as in
    production (https on 443 in the URL, every hop checked, the same limits)."""
    return SafeHttp(
        resolver=resolve_for_tests,
        allow=lambda a: a == "127.0.0.1",
        ssl_context=site.client_ssl(),
        connect_port=site.port,
    )
