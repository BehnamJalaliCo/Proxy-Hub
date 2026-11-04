"""The operator account for @MTProxybot (decision D37, docs/11 section 2.1).

A SECOND Telegram account, never the owner's personal one, logged into the panel so later slices
can register proxies with @MTProxybot. This slice only logs in, checks the session and logs out.

* ``userclient``: the only module that imports Telethon; six operations, an allowlist of requests.
* ``validation``: input formats and the masked phone (pure).
* ``state``: the stored status row, its transitions and the API's status shape (pure).
* ``store``: the settings row, the vault names, the encrypted pending login in Redis, the lock.
* ``service``: the login, logout and check flows used by the API and the worker.
"""
