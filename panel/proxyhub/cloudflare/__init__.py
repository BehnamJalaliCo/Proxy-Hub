"""Cloudflare DNS and the domain pool (docs/10 section 3.10, decisions D13 and D35).

* ``client``: Cloudflare API v4 over httpx. The token travels only in the Authorization header.
* ``records``: the managed-record rule. The panel touches a DNS record only if the record carries
  the comment marker ``managed-by=proxy-hub`` AND its own table lists it; every other record is
  never changed or deleted.
* ``pool``: the domain pool state machine (none, active, standby, filtered, retired, deleted).
* ``filtering``: pure logic of the filter check from Iran (Globalping DNS, 10.10.34.0/24).
* ``rotation``: the pool run by the panel: new zones join as standby, a filtered domain leaves
  rotation and a standby takes over, the owner is told.
* ``deletion``: a filtered zone is deleted from Cloudflare after a grace period (the only caller
  of ``CloudflareClient.delete_zone``).
* ``service``: token verification and storage, zone refresh, desired records, applying a plan.
"""

# The vault entry name of the Cloudflare token (not a secret itself).
TOKEN_SECRET = "cloudflare:token"  # noqa: S105
