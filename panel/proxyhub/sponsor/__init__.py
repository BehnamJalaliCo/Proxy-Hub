"""@MTProxybot registration and Proxy Sponsor automation (docs/11 section 2; decisions D25, D37).

* ``provider``: the live MTProto proxies (team mtproto's ``sponsor_api``) as a Protocol.
* ``channels``: public channel links (pure).
* ``replies``: reading the bot's replies and masking secrets (pure; real reply texts as test data).
* ``conversation``: the registration and promotion conversations as a state machine (pure).
* ``campaigns``: which sponsor channel is wanted when (pure).
* ``pacing``: human pace (5 s gap, 30 messages an hour) in PostgreSQL, with the kill switch.
* ``store``: settings, registrations with their flow rows, campaigns.
* ``driver``: one conversation over ``proxyhub.mtproxy.userclient.BotChat``.
* ``service``: the reconciler's pass and the API's reads and writes.
"""
