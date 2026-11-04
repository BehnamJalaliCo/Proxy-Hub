"""Live node pipeline: the owner-triggered test node and the live fleet's slot nodes, fail-closed.

* :mod:`proxyhub.nodes.image` reads ``node-image/`` (versions.lock, provision.sh, host files).
* :mod:`proxyhub.nodes.cloud_init` renders a node's user data (it holds the one-time token).
* :mod:`proxyhub.nodes.service` answers the API and the CLI (request, retire, list, events) and
  evaluates the creation gates on stored data.
* :mod:`proxyhub.nodes.executor` is the only module here that talks to the Hetzner mutation
  module, through ``proxyhub.safety.gates`` permits: for the owner's test node, and for the slot
  nodes the live fleet loop requests or retires (only while the owner has turned dry-run off).
* :mod:`proxyhub.nodes.hooks` moves a node to ``registering`` when its agent registers.
"""
