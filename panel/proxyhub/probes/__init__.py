"""Reachability probes from Iran (docs/10 M4.1/M4.2; P15, 2026-11-05: real TLS handshakes decide).

* ``globalping``: Globalping public API (no token) from Iranian networks: ``http`` (HTTPS) to the
  node's own name on 443 and the MTProto port (what decides), TCP/ICMP/DNS as information.
* ``checkhost``: check-host.net's Iranian nodes, TCP information only (MCI and Rightel answer a TCP
  SYN themselves for blocked addresses, so TCP never makes a node reachable).
* ``verdict``: pure logic. ``blocked`` needs TLS failing from MCI AND Rightel on every port in at
  least two rounds, each with a TLS control that proves the probes themselves work.
* ``service``: one probe cycle (I/O) within Globalping's hourly allowance, storage, the per-node
  location record the fleet scores locations with, and the summary for the dashboard.

The live fleet (proxyhub/fleet/live.py) acts on a fresh ``blocked`` verdict only when the node's own
agent says the probed service is up. No end-user data exists here (rule 7): targets are our own
nodes, public control and candidate sites and our own DNS names; results are per measurement
network, never per person.
"""
