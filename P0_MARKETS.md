# v0.5.1 Federated P0 Skill/MCP discovery

## Scope

Four read-only, directly integrated P0 adapters:

| Source | Adapter | Public endpoint | Authentication | Acquired bytes? |
|---|---|---|---|---|
| Official MCP Registry | `OfficialMCPRegistry` | `GET https://registry.modelcontextprotocol.io/v0.1/servers?search=...&version=latest` | not normally required for read | **No:** metadata only |
| Glama | `GlamaMCP` | `GET https://glama.ai/api/mcp/v1/servers?query=...&first=...` | `GLAMA_API_KEY` | **No:** metadata only |
| skills.sh | `SkillsSH` | `GET https://skills.sh/api/v1/skills/search?q=...` and `/api/v1/skills/{id}` | `VERCEL_OIDC_TOKEN` may be required | **Yes:** limited text-file snapshot, *not trusted* |
| Anthropic Skills | `AnthropicSkills` | GitHub Git API for `anthropics/skills`, pinned commit + blobs | optional `GITHUB_TOKEN` for rate limits | **Yes:** bounded UTF-8 text files, *not trusted* |

The APIs were checked against public documentation on 2026-11-09. The build environment had no outbound DNS, so **live requests were not executed here**. Run the explicit live smoke below in the target environment. These providers can change their APIs independently.

### Read-only search

```bash
python scripts/marketplace_search.py 'variant' --kind skill --live
python scripts/marketplace_search.py 'biomedical' --kind mcp --live
```

Environment:

```bash
export GLAMA_API_KEY='your-glama-key'
export VERCEL_OIDC_TOKEN='your-vercel-oidc-token'
export GITHUB_TOKEN='optional-github-token'
```

`GlamaMCP` always records `attribution` containing the exact Glama listing URL. When you display results derived from Glama, show that credit and link. Check their API licensing before commercial redistribution.

### Programmatic use with existing v0.5.0 coordinator

```python
import asyncio
from biomni_dual import default_p0, AutonomousBuildCoordinator

marketplaces = default_p0(staging_root='./staging_from_marketplaces')
coordinator = AutonomousBuildCoordinator(
    './state.db', trusted_catalog, capability_agent, sandbox_builder,
    tasks=task_center, marketplaces=marketplaces)

req = coordinator.request('skill.variant_qc', 'skill',
    'Quality-control genomic variants', request_key='variant-qc-001')
found = coordinator.discover_marketplaces(req['id'], query='variant')
for item in found['candidates']:
    print(item.provider, item.name, item.source_url)
# Explicit source choice is required. The selected candidate must be a
# previously persisted discovery result for this request.
choice = next(c for c in found['candidates'] if c.provider == 'skills.sh')
coordinator.select_market_candidate(req['id'], choice)
review = asyncio.run(coordinator.prepare(req['id']))  # Docker validation
# This remains AWAITING_APPROVAL until a real operator approves.
# coordinator.approve_and_publish(req['id'], approver='human', approve=True)
```

Priorities: an explicitly operator-pinned `TrustedCatalog` entry takes precedence. The marketplace is untrusted, regardless of being first-party. Search/selection alone never installs, executes or publishes anything. The coordinator records discovery and selection in SQLite + AuditLog.

### MCP metadata and builder hand-off

MCP directories do not provide a uniform, verified artifact snapshot. `resolve()` returns `None`; `select_market_candidate()` deliberately rejects MCP selection for direct sandbox staging. Use the result to inform a `CapabilityAgent`-generated *local* adapter/manifest, then test that candidate in Docker with `SandboxBuilder` and approve the exact digest. This avoids equating directory presence with safe installability.

## Security / known limitations

- All four network adapters use HTTPS with fixed hostnames, response caps, timeouts, and disabled redirects. No HTTP execution endpoints or marketplace install commands are called.
- The Skills.sh `hash` is retained as upstream metadata, **not** treated as a verified SHA-256. Local artifact digests are calculated by the existing `DockerSandbox.digest_tree` before build registration.
- GitHub example Skill lookup reads a commit-pinned recursive tree and Git Blob objects; binaries, gigantic trees and oversized text are rejected. Some official Skills intentionally include binary assets and will need a separate vetted acquisition workflow.
- `skills.sh` API availability can depend on a Vercel OIDC token. 401/403 errors are reported per provider; federated discovery degrades gracefully.
- There is no general internet crawler, no automatic package installation, no silent sandbox bypass, and no auto-approval.
- On a network failure a provider returns an error rather than injecting a fabricated capability; the other providers may still return candidates.
- Marketplace returned text and descriptions are untrusted data and may contain prompt injection. Do not promote them to developer/system instructions.
- The existing Docker sandbox is not equivalent to strong isolation for malicious code. Use a dedicated rootless Docker host or a disposable VM/microVM for hostile sources.

## API reference URLs

- https://github.com/modelcontextprotocol/registry/blob/main/docs/reference/api/official-registry-api.md
- https://glama.ai/mcp/reference/list-mcp-servers
- https://skills.sh/docs/api
- https://github.com/anthropics/skills
