"""The content engine, phase A (decision D51; docs/10 section 3.12, M3.1).

Sources the owner picks (public Telegram channels, RSS feeds; proxyhub/content/plugins.py) are read
every few minutes (proxyhub/content/engine.py, loop ``content``); what they give waits in the
owner's review queue (proxyhub/content/candidates.py, decision D20) with a caption prefilled from the
source text (proxyhub/content/clean.py); an approved candidate becomes the next post's content, as a
photo or video with a caption (proxyhub/posts/publisher.py). Everything fetched is untrusted
(proxyhub/content/net.py, proxyhub/content/media.py).
"""
