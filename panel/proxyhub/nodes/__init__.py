"""Live node pipeline (team live): ONE owner-triggered test node, fail-closed.

* :mod:`proxyhub.nodes.image` reads ``node-image/`` (versions.lock, provision.sh, host files).
* :mod:`proxyhub.nodes.cloud_init` renders a node's user data (it holds the one-time token).
* :mod:`proxyhub.nodes.service` answers the API and the CLI (request, retire, list, events) and
  evaluates the creation gates on stored data.
* :mod:`proxyhub.nodes.executor` is the only module here that talks to the Hetzner mutation
  module, through ``proxyhub.safety.gates`` permits; it handles test nodes only (the 9-slot fleet
  stays dry-run: ``proxyhub.reconcilers.fleet.FLEET_LIVE_ENABLED`` is False).
* :mod:`proxyhub.nodes.hooks` moves a node to ``registering`` when its agent registers.
"""
