"""Analytics: traffic, load and cost of our servers, and growth of our channels (aggregates only).

* ``hetzner_usage``: READ-ONLY Hetzner calls (server traffic counters, the metrics API).
* ``sampling``: what the ``analytics`` loop stores every 15 minutes / hour, thresholds and the
  owner's alerts, and :func:`~proxyhub.analytics.sampling.capacity_signals` for the fleet.
* ``members``: ``chat_member`` updates -> join/leave events (D7 fields only), tracking links.
* ``report``: the numbers the dashboard's «آمار» page shows.

Nothing here stores or returns a proxy user's IP or traffic (CLAUDE.md rule 7).
"""
