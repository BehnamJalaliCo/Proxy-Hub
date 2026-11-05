"""The panel's own TLS certificates: Let's Encrypt over ACME (RFC 8555) with DNS-01 (2026-11-05).

Why: from Iran a VLESS Reality link whose SNI is a Google name on a Hetzner IP did not even open a
website, while the owner's own VLESS + WebSocket + TLS config with a REAL certificate for the
server's OWN domain (grey-cloud A record) carried 200 Mbps from the same phone. So every serving
node gets a DNS name of its own (``slot<N>.<domain>``, a test node ``t<8 hex>.<domain>``) and serves
the wildcard certificate ``*.<active pool domain>`` that this package obtains.

* :mod:`proxyhub.acme.protocol`: the ACME conversation (certbot's ``acme`` library, synchronous,
  run in a worker thread). The only module that imports ``acme`` or ``josepy``.
* :mod:`proxyhub.acme.doh`: DNS-over-HTTPS (RFC 8484) TXT lookups, to see the challenge record
  live before the CA is asked to validate it.
* :mod:`proxyhub.acme.store`: the certificate's public facts (table ``acme_certificates``); the
  chain, its private key and the account key live in the vault only (rule 3).
* :mod:`proxyhub.acme.service`: one order end to end. The ``_acme-challenge`` TXT records are
  created and removed under the managed-record rule (purpose ``acme``,
  proxyhub/cloudflare/records.py), like every other record the panel writes.
* The ``acme`` control loop (:mod:`proxyhub.reconcilers.acme`) orders a certificate for the active
  pool domain and renews it when fewer than 30 days remain, one order per domain at a time, with
  backoff after a failure (Let's Encrypt rate limits).

Nothing here logs a key, a certificate body or a CA message: errors are short codes.
"""
