"""``python -m proxyhub.pki ca-pem [--out FILE]``: write the agent CA certificate (public).

The deployer uses it to give Caddy the CA it must trust for agent mTLS, and the fleet uses the same
bytes in each node's cloud-init. The certificate is rebuilt from the CA key deterministically, so
this needs no database and always equals the stored copy. The key itself is never printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from proxyhub.config import get_config
from proxyhub.pki.ca import build_ca_certificate, load_ca_key, to_pem


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m proxyhub.pki")
    sub = parser.add_subparsers(dest="cmd", required=True)
    ca_pem = sub.add_parser("ca-pem", help="write the agent CA certificate (PEM)")
    ca_pem.add_argument("--out", type=Path, help="file to write (default: stdout)")
    args = parser.parse_args(argv)

    pem = to_pem(build_ca_certificate(load_ca_key(get_config().agent_ca_key_file)))
    if args.out is None:
        sys.stdout.write(pem)
        return 0
    tmp = args.out.with_name(args.out.name + ".tmp")
    tmp.write_text(pem, encoding="ascii")
    tmp.chmod(0o644)  # a certificate is public; Caddy runs as another user
    os.replace(tmp, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
