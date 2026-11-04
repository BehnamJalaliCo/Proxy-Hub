"""Reachability probes from Iran (docs/10 M4.1, groundwork for M4.2). This round only RECORDS
verdicts; nothing is replaced automatically.

* ``globalping``: Globalping public API (no token) from Iranian networks chosen by ASN.
* ``checkhost``: check-host.net's Iranian nodes, an independent second source.
* ``verdict``: pure logic. A verdict needs at least two rounds from BOTH sources, and in every one
  of them a control target that proves the probes themselves work.
* ``service``: one probe cycle (I/O), storage, the summary for the dashboard.

No end-user data exists here (rule 7): targets are our own nodes, a public control host and our
own DNS names; results are per measurement network, never per person.
"""
