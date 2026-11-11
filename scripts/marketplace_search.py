#!/usr/bin/env python3
"""Read-only market search; explicit --live required to call external APIs."""
import argparse
import json
from biomni_dual import default_p0


def main():
    ap = argparse.ArgumentParser(description='Read-only Skill/MCP P0 catalog search')
    ap.add_argument('query')
    ap.add_argument('--kind', choices=('skill', 'mcp'), required=True)
    ap.add_argument('--limit', type=int, default=10)
    ap.add_argument('--live', action='store_true', help='Explicitly allow outbound HTTPS calls')
    args = ap.parse_args()
    if not args.live:
        ap.error('Use --live to explicitly enable network queries; no automatic installation occurs')
    result = default_p0().search(args.query, args.kind, limit=args.limit)
    payload = {'kind': args.kind, 'query': args.query, 'providers': result['providers'],
               'candidates': [c.to_dict() for c in result['candidates']],
               'errors': result['errors'],
               'warning': 'Discovery metadata is untrusted. No candidate was installed or published.'}
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
