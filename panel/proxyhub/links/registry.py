"""Where the publisher finds live proxy links (CROSS-TEAM CONTRACT item 3).

Each provider returns the links of one protocol (``proxyhub.mtproto.links.provide_links``,
``proxyhub.reality.links.provide_links``); the integrator appends them here. The publisher
(proxyhub/posts/links.py) calls every provider inside its own SAVEPOINT, so a provider that fails
costs only its own links for that round.
"""

from __future__ import annotations

from proxyhub.links.contract import Provider

PROVIDERS: list[Provider] = []
