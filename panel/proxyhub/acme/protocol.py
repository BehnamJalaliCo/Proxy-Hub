"""The ACME conversation (RFC 8555) with certbot's ``acme`` library: account, order, DNS-01, finalize.

Synchronous (the library uses ``requests``); :mod:`proxyhub.acme.service` runs every call here in a
worker thread. This is the only module that imports ``acme`` or ``josepy``.

Keys: the account key and each certificate key are ECDSA P-256, made here with ``cryptography``.
They leave this module only as :class:`~proxyhub.vault.SecretValue` (for the vault). The library
logs request and response bodies at DEBUG; its logger is held at INFO here, so even a debug run of
the panel never prints a JWS, a CSR or a certificate. Errors become :class:`AcmeError` with a short
code: the CA's problem text is never passed on (it can quote names and values).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import requests
from acme import challenges, crypto_util, errors, messages
from acme.client import ClientNetwork, ClientV2
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from josepy.jwa import ES256
from josepy.jwk import JWK, JWKEC

from proxyhub.vault import SecretValue

LETS_ENCRYPT: Final = "https://acme-v02.api.letsencrypt.org/directory"
LETS_ENCRYPT_STAGING: Final = "https://acme-staging-v02.api.letsencrypt.org/directory"
USER_AGENT: Final = "proxy-hub-panel"
NETWORK_TIMEOUT: Final = 30

for _name in ("acme", "josepy"):
    logging.getLogger(_name).setLevel(logging.INFO)


class AcmeError(Exception):
    """A failed ACME step. ``code`` is short and safe to log and store; ``retry_after`` is the CA's
    own wait (rate limits), if it said one."""

    def __init__(self, code: str, retry_after: datetime | None = None) -> None:
        super().__init__(code)
        self.code = code[:60]
        self.retry_after = retry_after


def new_private_key_pem() -> SecretValue:
    """A fresh ECDSA P-256 key as unencrypted PKCS#8 PEM."""
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return SecretValue(pem.decode("ascii"))


def _account_jwk(key_pem: SecretValue) -> JWKEC:
    key = serialization.load_pem_private_key(key_pem.reveal().encode("ascii"), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise AcmeError("account_key_type")
    return JWKEC(key=key)


@dataclass(frozen=True)
class Challenge:
    """One DNS-01 challenge: publish ``value`` as a TXT record at ``record_name``."""

    identifier: str  # "example.com" for the wildcard *.example.com
    record_name: str  # "_acme-challenge.example.com"
    value: str = field(repr=False)


@dataclass(frozen=True)
class Issued:
    chain_pem: str  # leaf first, then the intermediates (public)
    key_pem: SecretValue
    not_before: datetime
    not_after: datetime
    serial_hex: str
    fingerprint_sha256: str


@dataclass
class Order:
    """An order in flight (kept in memory only, for one run)."""

    resource: messages.OrderResource
    key_pem: SecretValue
    pending: list[tuple[messages.ChallengeBody, challenges.DNS01Response]]
    challenges: list[Challenge]


def leaf_facts(chain_pem: str) -> tuple[datetime, datetime, str, str]:
    """(not_before, not_after, serial hex, SHA-256 fingerprint hex) of the first certificate."""
    leaf = x509.load_pem_x509_certificates(chain_pem.encode("ascii"))[0]
    return (
        leaf.not_valid_before_utc,
        leaf.not_valid_after_utc,
        format(leaf.serial_number, "x"),
        leaf.fingerprint(hashes.SHA256()).hex(),
    )


def _code(exc: BaseException) -> AcmeError:
    if isinstance(exc, AcmeError):
        return exc
    if isinstance(exc, messages.Error):
        return AcmeError(f"acme_{exc.code or 'error'}")
    if isinstance(exc, errors.ValidationError):
        return AcmeError("acme_validation_failed")
    if isinstance(exc, errors.TimeoutError):
        return AcmeError("acme_timeout")
    if isinstance(exc, requests.RequestException):
        return AcmeError("acme_unreachable")
    if isinstance(exc, errors.Error):
        return AcmeError("acme_client_error")
    return AcmeError("acme_unexpected")


class _Network(ClientNetwork):
    """ClientNetwork that trusts an extra CA file (a test ACME server such as Pebble) instead of
    the system roots; with ``ca_file`` None it is exactly the library's own."""

    def __init__(self, key: JWK, ca_file: str | None) -> None:
        super().__init__(key=key, alg=ES256, user_agent=USER_AGENT, timeout=NETWORK_TIMEOUT)
        if ca_file is not None:
            # requests takes a CA bundle path where the library expects a bool.
            self.verify_ssl = ca_file  # type: ignore[assignment]


class AcmeSession:
    """One account at one ACME directory. Every method may raise :class:`AcmeError` only."""

    def __init__(
        self,
        directory_url: str,
        account_key: SecretValue,
        *,
        account_uri: str | None = None,
        ca_file: str | None = None,
    ) -> None:
        self._jwk = _account_jwk(account_key)
        self._net = _Network(self._jwk, ca_file)
        try:
            directory = ClientV2.get_directory(directory_url, self._net)
        except Exception as exc:  # every library failure becomes a code
            raise _code(exc) from None
        self._client = ClientV2(directory, self._net)
        if account_uri is not None:
            self._net.account = messages.RegistrationResource(body=messages.Registration(), uri=account_uri)

    def close(self) -> None:
        self._net.session.close()

    def register(self) -> str:
        """The account's URI at this directory: a new account (terms agreed, no contact: nothing
        about the owner is sent), or the existing one for this key."""
        try:
            regr = self._client.new_account(messages.NewRegistration.from_data(terms_of_service_agreed=True))
            return str(regr.uri)
        except errors.ConflictError as exc:  # the key already has an account here
            uri = str(exc.location)
            self._net.account = messages.RegistrationResource(body=messages.Registration(), uri=uri)
            return uri
        except Exception as exc:
            raise _code(exc) from None

    def new_order(self, domain: str) -> Order:
        """An order for the wildcard ``*.<domain>`` with a fresh certificate key; returns the DNS-01
        challenges still to be answered (none when the CA reuses a valid authorization)."""
        key_pem = new_private_key_pem()
        try:
            csr = crypto_util.make_csr(key_pem.reveal().encode("ascii"), [f"*.{domain}"])
            order = self._client.new_order(csr)
        except Exception as exc:
            raise _code(exc) from None
        pending: list[tuple[messages.ChallengeBody, challenges.DNS01Response]] = []
        found: list[Challenge] = []
        for authz in order.authorizations:
            if authz.body.status == messages.STATUS_VALID:
                continue
            identifier = str(authz.body.identifier.value)
            body = next((c for c in authz.body.challenges if isinstance(c.chall, challenges.DNS01)), None)
            if body is None:
                raise AcmeError("acme_no_dns01")
            chall: Any = body.chall
            response, validation = chall.response_and_validation(self._jwk)
            pending.append((body, response))
            found.append(
                Challenge(
                    identifier=identifier,
                    record_name=chall.validation_domain_name(identifier),
                    value=str(validation),
                )
            )
        return Order(resource=order, key_pem=key_pem, pending=pending, challenges=found)

    def finalize(self, order: Order, *, timeout: timedelta = timedelta(minutes=3)) -> Issued:
        """Tell the CA the records are in place, wait for validation, send the CSR, fetch the chain."""
        try:
            for body, response in order.pending:
                self._client.answer_challenge(body, response)
            # The library compares deadlines with naive local time.
            deadline = datetime.now() + timeout
            done = self._client.poll_and_finalize(order.resource, deadline)
        except Exception as exc:
            raise _code(exc) from None
        chain = str(done.fullchain_pem or "")
        if "BEGIN CERTIFICATE" not in chain:
            raise AcmeError("acme_no_certificate")
        not_before, not_after, serial, fingerprint = leaf_facts(chain)
        return Issued(
            chain_pem=chain,
            key_pem=order.key_pem,
            not_before=not_before.astimezone(UTC),
            not_after=not_after.astimezone(UTC),
            serial_hex=serial,
            fingerprint_sha256=fingerprint,
        )


def directory_label(url: str) -> str:
    """A stable short label of a directory URL (where an account URI is remembered)."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]
