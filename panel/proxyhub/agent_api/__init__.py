"""Node agent API (``/agent/v1``): one-time registration, mTLS identity, desired state, reports.

Transport and trust (production): Caddy serves ``agent.<domain>`` with client-certificate
verification against the panel's agent CA (``python -m proxyhub.pki ca-pem``) and forwards to the
API two headers that it sets itself, overwriting anything the client sent:

* ``X-ProxyHub-Agent-Proxy-Secret``: the content of the ``agent_proxy_secret`` file, proving the
  request came through that Caddy site (the public panel site never adds it);
* ``X-ProxyHub-Client-Cert-Sha256``: ``{http.request.tls.client.fingerprint}``, the SHA-256 of the
  client certificate Caddy verified (empty when the client sent none, e.g. at registration).

The API trusts the fingerprint only when the proxy secret matches (constant-time) and the
fingerprint belongs to a certificate it issued that is unrevoked and currently valid.
"""
