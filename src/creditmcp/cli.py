"""One-command lookups: python -m creditmcp.cli "Tenaska Power Service Co." [--json] [--limit N]"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from creditmcp.resolve import EntityResolver


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Resolve a counterparty name against the PJM and MISO roster.")
    p.add_argument("name", nargs="+", help="Counterparty name(s); quote each one")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--json", action="store_true", help="Print the full result as JSON")
    p.add_argument("--home", default=os.environ.get("CREDITMCP_HOME") or str(Path(__file__).resolve().parents[2]))
    args = p.parse_args(argv)
    r = EntityResolver.from_dir(args.home)
    for name in args.name:
        res = r.resolve(name, limit=args.limit)
        if args.json:
            print(res.model_dump_json(indent=2))
            continue
        print(f"\n{name}  ->  {res.normalized}")
        for i, c in enumerate(res.candidates, 1):
            group = c.affiliate_group.name if c.affiliate_group else "-"
            print(f"  {i}. {c.confidence.value:<8} {c.score:.3f}  {c.name}  [{'+'.join(c.isos)}]  "
                  f"group: {group}  via {c.alias_source.value} '{c.matched_alias}'")
        print(f"  {res.summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
