"""Secret vault: real AES-256-GCM, real PostgreSQL (CLAUDE.md rule 3)."""

from __future__ import annotations

import logging
import pickle
import secrets
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from test_fleet_support import assert_absent_from_database, clean_fleet_tables, make_vault, new_token

from proxyhub.vault import (
    SecretValue,
    Vault,
    VaultDecryptError,
    VaultError,
    VaultKeyError,
    delete_secret,
    secret_exists,
)


@pytest.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    await clean_fleet_tables(eng)
    yield eng
    await clean_fleet_tables(eng)
    await eng.dispose()


# --- key handling (no database) ---------------------------------------------------------------


def test_key_file_must_be_64_hex_chars(tmp_path: Path) -> None:
    key_material = secrets.token_hex(16)  # only 32 hex chars
    bad = tmp_path / "short"
    bad.write_text(key_material)
    with pytest.raises(VaultKeyError) as exc:
        Vault.from_key_file(bad)
    assert key_material not in str(exc.value)
    notes = tmp_path / "nothex"
    notes.write_text("z" * 64)
    with pytest.raises(VaultKeyError):
        Vault.from_key_file(notes)
    with pytest.raises(VaultKeyError):
        Vault.from_key_file(tmp_path / "missing")
    with pytest.raises(VaultKeyError):
        Vault(b"\x00" * 16)


def test_world_writable_key_file_refused(tmp_path: Path) -> None:
    path = tmp_path / "master_key"
    path.write_text(secrets.token_hex(32))
    path.chmod(0o666)
    with pytest.raises(VaultKeyError):
        Vault.from_key_file(path)


def test_secret_value_never_shows_itself() -> None:
    token = new_token()
    value = SecretValue(token)
    assert value.reveal() == token
    for rendered in (repr(value), str(value), f"{value}", f"{value!r}", repr([value]), repr({"t": value})):
        assert token not in rendered
    with pytest.raises(TypeError):
        pickle.dumps(value)
    with pytest.raises(ValueError):
        SecretValue("")


def test_encrypt_is_randomised_and_bound_to_name(tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    token = new_token()
    n1, c1 = vault.encrypt("hcloud:a", token)
    n2, c2 = vault.encrypt("hcloud:a", token)
    assert n1 != n2 and c1 != c2  # fresh nonce every time
    assert token.encode() not in c1
    assert vault.decrypt("hcloud:a", n1, c1, 1).reveal() == token
    with pytest.raises(VaultDecryptError):
        vault.decrypt("hcloud:b", n1, c1, 1)  # ciphertext moved to another name
    with pytest.raises(VaultDecryptError):
        vault.decrypt("hcloud:a", n1, c1[:-1] + bytes([c1[-1] ^ 1]), 1)  # tampered
    with pytest.raises(VaultDecryptError):
        vault.decrypt("hcloud:a", n1, c1, 2)  # unknown key version
    with pytest.raises(VaultError):
        vault.encrypt("Bad Name!", token)
    with pytest.raises(VaultError):
        vault.encrypt("hcloud:a", "")


# --- against the real database ----------------------------------------------------------------


@pytest.mark.db
async def test_round_trip_replace_and_delete(engine: AsyncEngine, tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    first, second = new_token(), new_token()
    async with engine.begin() as conn:
        await vault.put(conn, "test:round-trip", first)
    async with engine.connect() as conn:
        got = await vault.get(conn, "test:round-trip")
        assert got is not None and got.reveal() == first
        assert await secret_exists(conn, "test:round-trip")
        rotated = (
            await conn.execute(text("SELECT rotated_at FROM secrets WHERE name = 'test:round-trip'"))
        ).scalar_one()
        assert rotated is None
    async with engine.begin() as conn:
        await vault.put(conn, "test:round-trip", SecretValue(second))
    async with engine.connect() as conn:
        got = await vault.get(conn, "test:round-trip")
        assert got is not None and got.reveal() == second
        row = (
            await conn.execute(
                text(
                    "SELECT rotated_at, key_version, octet_length(nonce) "
                    "FROM secrets WHERE name = 'test:round-trip'"
                )
            )
        ).one()
        assert row[0] is not None and row[1] == 1 and row[2] == 12
    async with engine.begin() as conn:
        assert await delete_secret(conn, "test:round-trip")
        assert not await delete_secret(conn, "test:round-trip")
        assert await vault.get(conn, "test:round-trip") is None


@pytest.mark.db
async def test_wrong_master_key_cannot_read(engine: AsyncEngine, tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    right, wrong = make_vault(tmp_path / "a"), make_vault(tmp_path / "b")
    async with engine.begin() as conn:
        await right.put(conn, "test:wrong-key", new_token())
    async with engine.connect() as conn:
        with pytest.raises(VaultDecryptError) as exc:
            await wrong.get(conn, "test:wrong-key")
        assert "test:wrong-key" in str(exc.value)


@pytest.mark.db
async def test_row_copied_to_other_name_does_not_decrypt(engine: AsyncEngine, tmp_path: Path) -> None:
    vault = make_vault(tmp_path)
    async with engine.begin() as conn:
        await vault.put(conn, "test:original", new_token())
        await conn.execute(
            text(
                "INSERT INTO secrets (name, ciphertext, nonce, key_version) "
                "SELECT 'test:copy', ciphertext, nonce, key_version FROM secrets WHERE name = 'test:original'"
            )
        )
    async with engine.connect() as conn:
        with pytest.raises(VaultDecryptError):
            await vault.get(conn, "test:copy")


@pytest.mark.db
async def test_no_plaintext_anywhere_in_database_or_logs(
    engine: AsyncEngine, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    vault = make_vault(tmp_path)
    token = new_token()
    caplog.set_level(logging.DEBUG)
    async with engine.begin() as conn:
        await vault.put(conn, "test:plaintext", token)
    async with engine.connect() as conn:
        assert (await vault.get(conn, "test:plaintext")) is not None
    await assert_absent_from_database(engine, token)
    assert token not in caplog.text
    async with engine.connect() as conn:
        raw = (
            await conn.execute(text("SELECT ciphertext FROM secrets WHERE name = 'test:plaintext'"))
        ).scalar_one()
    assert token.encode() not in bytes(raw)


@pytest.mark.db
async def test_database_constraints_reject_malformed_rows(engine: AsyncEngine) -> None:
    for nonce_len, ct_len, version in ((11, 32, 1), (12, 8, 1), (12, 32, 0)):
        with pytest.raises(IntegrityError):
            async with engine.begin() as conn:
                await conn.execute(
                    text(
                        "INSERT INTO secrets (name, ciphertext, nonce, key_version) "
                        "VALUES ('test:bad', :c, :n, :v)"
                    ),
                    {"c": b"\x01" * ct_len, "n": b"\x02" * nonce_len, "v": version},
                )
