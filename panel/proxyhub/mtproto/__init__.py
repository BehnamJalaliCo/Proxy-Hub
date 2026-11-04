"""MTProto proxies on the nodes (docs/11_MTPROTO_SPONSOR.md, decisions D24-D26).

* :mod:`proxyhub.mtproto.settings`: the owner's settings (key ``mtproto``): port, fake-TLS domain,
  link modes.
* :mod:`proxyhub.mtproto.policy`: every node coming up or in service gets the Telemt settings and
  ONE long-lived Telemt user (its public secret); users of nodes that leave service are disabled.
  Driven by the ``mtproto`` control loop (:mod:`proxyhub.reconcilers.mtproto`).
* :mod:`proxyhub.mtproto.service`: what the panel knows about each proxy (host, port, link,
  connection counts), for the API and the two contracts below.
* :mod:`proxyhub.mtproto.links`: ``provide_links`` for the channel posts (links contract).
* :mod:`proxyhub.mtproto.sponsor_api`: ``proxies_to_register`` / ``apply_ad_tag`` for @MTProxybot
  registration (sponsor contract).

Rule 3: a proxy secret is opened only to build the public link the owner publishes on purpose or to
register the proxy; it never reaches a log line, an error or ``actions_log``. Rule 7: only aggregate
counters per secret, never an end-user IP.
"""
