"""Owner login for the panel (docs/10 section 3.9, owner decisions D12 and D22).

Username + password (argon2id) + TOTP code from an authenticator app, one-time setup link,
single-use recovery codes, server-side sessions, lockout after failed attempts, and an audit
trail in ``actions_log``.

Security invariants (CLAUDE.md rules 3 and 7):
- no password, token, TOTP secret, recovery code or session cookie is ever logged or stored in
  clear; tokens are stored as SHA-256, the TOTP secret is AES-256-GCM encrypted with the master key;
- no client IP is read, stored or logged anywhere in this package.
"""
