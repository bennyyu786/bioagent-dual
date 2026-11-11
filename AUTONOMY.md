# v0.5.0 Controlled autonomous capability building

## Lifecycle

`REQUESTED -> PREPARING -> VALIDATING -> AWAITING_APPROVAL -> PUBLISHED`

- Catalog hits: search an operator-managed JSON catalog, optionally refresh a HTTPS index from explicitly allowlisted hosts. Each item is pinned by a tree SHA256 digest. Untrusted/changed content is rejected, **never silently replaced with LLM output**.
- Generation fallback: Claude/Codex through `CapabilityAgent` outputs a JSON list of files; a trusted coordinator validates paths and writes them to a fresh staging directory.
- Validation: existing `DockerSandbox` tests Skills or performs MCP `initialize`, `tools/list`, and configured `tools/call` probes. Rootless Docker on an independent Linux host is required for the real path.
- On failed tests: `REPAIR_PENDING` generates a new candidate using the previous test log, at most `max_repairs` times. Stale candidates do not become executable.
- Human decision: an explicit named approver is required. Successful independent validation alone does not publish.
- Publication: existing `SandboxBuilder` confirms the digest, stores a content-addressed copy, records audit events, registers the capability, and releases tasks only if **all required capabilities** are now satisfied.
- Recovery: interrupted `PREPARING` or `VALIDATING` jobs become `BLOCKED`; an operator must reconcile. Retried requests with the same idempotency key return the prior record.

## Trusted catalog JSON

```json
[
  {
    "capability_id": "skill.variant_qc",
    "kind": "skill",
    "source": "/approved/capabilities/variant_qc",
    "digest": "<64 lowercase hex digits from DockerSandbox.digest_tree(...)>",
    "description": "Validated local Skill source"
  }
]
```

For an allowlisted remote catalog use `TrustedCatalog.from_https_index(url, allowed_hosts=("catalog.example.org",))` and only pinned HTTPS `.tar.gz` archives. This is **not** open web crawling, arbitrary Skill-market installation, or a claim that all remote sources are trusted.

## Programmatic wiring

```python
from biomni_dual import (
    AutonomousBuildCoordinator, TrustedCatalog, CapabilityAgent,
    SandboxBuilder, BuildStore, CapabilityRegistry, DockerSandbox,
    ClaudeSDKRuntime
)

registry = CapabilityRegistry()
builder = SandboxBuilder(BuildStore("builds.sqlite3"), DockerSandbox(), registry)
agent = CapabilityAgent(ClaudeSDKRuntime(), "./staging")
coordinator = AutonomousBuildCoordinator(
    "builds.sqlite3", TrustedCatalog.from_json("catalog.json"), agent, builder
)
request = coordinator.request(
    "skill.variant_qc", "skill", "Implement variant QC",
    request_key="variant-qc-2026-11-09"
)
# await coordinator.prepare(request["id"])
# Review exact source, evidence and dependency policy, then explicitly:
# coordinator.approve_and_publish(request["id"], approver="operator", approve=True)
```

The model SDK constructor and authentication must be configured according to the installed SDK version. Do not grant CapabilityAgent access to production credentials or privileged Docker services.

## Fault and threat model limitations

- Docker's shared kernel is not a sufficient isolation boundary for hostile code. Run the rootless Docker daemon on a dedicated disposable VM; consider microVM/gVisor for untrusted supply-chain inputs.
- The controller is trusted; untrusted Agents cannot access the host Docker socket.
- Network access for tested workloads is off by default. Dependent packages should be prebuilt into audited images.
- On real servers, test timeouts and container cleanup must be observed separately; a crashed process can have uncertain remote side effects.
- Skill/MCP quality tests and image digest checks do not establish absence of malicious behavior. Supply-chain scanning, dependency provenance and human code review remain necessary.
- Only local integration tests were run here; this environment does not provide Docker, authenticated Claude/Codex, or live Biomni.

## Verification

`python -m pytest -q` tests the full legacy suite and new v0.5 controller.

`python examples/autonomy_demo.py` is a **mock** end-to-end demonstration.

On a separate rootless Docker Linux host, use `python scripts/live_smoke.py --check docker` plus a real Skill/MCP candidate; confirm SDK credentials and actual MCP handshake before production approval.
