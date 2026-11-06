"""SNI names Xray itself warns about as a REALITY target (P19, after P16 dropped www.apple.com).

Xray-core 26.10.31 (the pinned version, node-image/versions.lock), ``infra/conf/transport_security.go``,
``REALITYConfig.Build``, logs this for every server name::

    sn = strings.ToLower(sn)
    if strings.HasSuffix(sn, ".ru") || strings.HasSuffix(sn, ".ir") || strings.HasSuffix(sn, ".cn") ||
        strings.Contains(sn, "apple") || strings.Contains(sn, "icloud") || strings.Contains(sn, "microsoft") {
        // REALITY: Choosing "<sn>" as the target will increase the likelihood of your server's IP
        // being blocked by the GFW
    }

:func:`risky_sni` is exactly that check (after our usual normalisation: spaces and a trailing dot
off), so "apple" anywhere in the name counts, as a whole label or as part of one
(``www.apple.com``, ``appleid.example.net``, ``ICLOUD.com``). Xray only warns; the panel refuses: such a
name is never a candidate site, never chosen as the REALITY target or server name, and a scope whose
key still carries one moves off it (proxyhub/targets/choice.py, proxyhub/reality/policy.py).

It is an Xray REALITY rule. MTProto's fake-TLS domain is Telemt's (another program, whose FAQ only
asks for an innocuous name that is not blocked) and stays as the owner set it (D40, D42).
"""

from __future__ import annotations

from typing import Final

RISKY_SUFFIXES: Final = (".ru", ".ir", ".cn")
RISKY_WORDS: Final = ("apple", "icloud", "microsoft")

# Shown to the owner (the API's message_fa, the dashboard): what is refused, and why.
RISKY_RULE_FA: Final = (
    "نامی که apple، icloud یا microsoft در آن باشد، یا به ‎.ru، ‎.ir یا ‎.cn ختم شود، سایت هدف Reality "
    "نمیشود: خود Xray هشدار میدهد که چنین SNIای احتمال بسته شدن IP سرور را بالا میبرد."
)


def risky_sni(name: str) -> bool:
    """Xray's own warning rule for a REALITY server name (case-insensitive)."""
    sn = name.strip().lower().rstrip(".")
    return sn.endswith(RISKY_SUFFIXES) or any(word in sn for word in RISKY_WORDS)


def risky_target(target: str, server_name: str) -> bool:
    """A key's (``host:port`` target, server name) pair: risky if either name is."""
    host = target.rsplit(":", 1)[0] if ":" in target else target
    return risky_sni(host) or risky_sni(server_name)
