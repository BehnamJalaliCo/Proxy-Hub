"""Cloudflare DNS and the domain pool (docs/10 section 3.10, decision D13).

* ``client``: Cloudflare API v4 over httpx. The token travels only in the Authorization header.
* ``records``: the managed-record rule. The panel touches a DNS record only if the record carries
  the comment marker ``managed-by=proxy-hub`` AND its own table lists it; every other record is
  never changed or deleted.
* ``pool``: the domain pool state machine (none, active, standby, filtered, retired).
* ``service``: token verification and storage, zone refresh, desired records, applying a plan.
"""
