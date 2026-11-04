"""Where the publisher finds live proxy links (CROSS-TEAM CONTRACT item 3).

Each provider returns the links of one protocol (``proxyhub.mtproto.links.provide_links``,
``proxyhub.reality.links.provide_links``), both listed here. The publisher
(proxyhub/posts/links.py) calls every provider inside its own SAVEPOINT, so a provider that fails
costs only its own links for that round.
"""

from __future__ import annotations

from proxyhub.links.contract import Provider
from proxyhub.mtproto.links import provide_links as mtproto_links
from proxyhub.reality.links import provide_links as reality_links

PROVIDERS: list[Provider] = [mtproto_links, reality_links]
