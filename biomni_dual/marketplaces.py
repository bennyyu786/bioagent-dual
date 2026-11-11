"""Read-only P0 marketplace adapters.

A marketplace is an *untrusted discovery source*, never a capability registry.
Only exact, bounded, path-checked Skill files may be staged for separate sandbox
validation. MCP catalog records contain metadata, not executable MCP servers.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Protocol
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

MAX_JSON = 2 * 1024 * 1024
MAX_TOTAL_FILES = 5 * 1024 * 1024
MAX_FILES = 100


class MarketplaceError(RuntimeError):
    pass


class MarketplaceAuthError(MarketplaceError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MarketplaceError('Marketplace redirects are not allowed')


class MarketplaceHTTP:
    """HTTPS-only bounded JSON fetch with fixed hosts and no redirects."""
    def __init__(self, *, fetch=None, timeout=12):
        self.fetch = fetch
        self.timeout = timeout

    def json(self, url: str, *, host: str, token: str | None = None) -> dict:
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or parsed.hostname != host or parsed.port not in (None, 443)
                or parsed.username or parsed.password or parsed.fragment):
            raise MarketplaceError('Marketplace URL not allowed')
        if self.fetch:
            result = self.fetch(url, host=host, token=token)
            if not isinstance(result, dict):
                raise MarketplaceError('Invalid marketplace JSON')
            return result
        headers = {'Accept': 'application/json', 'User-Agent': 'BiomniDualCenter/0.5.1'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        try:
            with build_opener(_NoRedirect()).open(Request(url, headers=headers), timeout=self.timeout) as r:
                data = r.read(MAX_JSON + 1)
            if len(data) > MAX_JSON:
                raise MarketplaceError('Marketplace response too large')
            result = json.loads(data)
            if not isinstance(result, dict):
                raise MarketplaceError('Invalid marketplace JSON object')
            return result
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise MarketplaceAuthError(f'{host} requires valid API authentication ({exc.code})') from exc
            raise MarketplaceError(f'{host} HTTP {exc.code}') from exc


@dataclass(frozen=True)
class MarketCandidate:
    id: str
    kind: str
    name: str
    description: str
    provider: str
    source_url: str
    canonical_source: str
    version: str = ''
    license: str = ''
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            'id': self.id, 'kind': self.kind, 'name': self.name,
            'description': self.description, 'provider': self.provider,
            'source_url': self.source_url, 'canonical_source': self.canonical_source,
            'version': self.version, 'license': self.license,
            'metadata': self.metadata,
        }


class MarketplaceAdapter(Protocol):
    name: str
    kind: str
    def search(self, query: str, limit: int = 10) -> list[MarketCandidate]: ...
    def resolve(self, candidate: MarketCandidate) -> 'ResolvedSkill | None': ...


@dataclass(frozen=True)
class ResolvedSkill:
    candidate: MarketCandidate
    files: dict[str, str]
    provenance: dict[str, Any]

    def stage(self, root: str | Path) -> Path:
        """Write inert text files; does not execute/approve the candidate."""
        if not (1 <= len(self.files) <= MAX_FILES) or 'SKILL.md' not in self.files:
            raise MarketplaceError('Skill bundle requires SKILL.md and valid file count')
        total = 0
        checked = {}
        for name, contents in self.files.items():
            rel = PurePosixPath(name)
            if (not name or rel.is_absolute() or '..' in rel.parts or '\\' in name
                    or any(part in ('', '.') for part in rel.parts) or len(rel.parts) > 12):
                raise MarketplaceError('Invalid skill file path')
            if not isinstance(contents, str):
                raise MarketplaceError('Only text Skill files are supported')
            total += len(contents.encode('utf-8'))
            if total > MAX_TOTAL_FILES:
                raise MarketplaceError('Skill bundle exceeds size limit')
            checked[name] = contents
        root = Path(root).resolve()
        root.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(prefix='market-skill-', dir=root))
        for name, contents in checked.items():
            path = folder / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding='utf-8')
        return folder


def _safe_limit(limit: int) -> int:
    if not isinstance(limit, int) or not 1 <= limit <= 50:
        raise ValueError('limit must be between 1 and 50')
    return limit


def _get_dict(value):
    return value if isinstance(value, dict) else {}


class OfficialMCPRegistry:
    name, kind = 'official-mcp-registry', 'mcp'
    base = 'https://registry.modelcontextprotocol.io'

    def __init__(self, http: MarketplaceHTTP | None = None):
        self.http = http or MarketplaceHTTP()

    def search(self, query: str, limit: int = 10) -> list[MarketCandidate]:
        limit = _safe_limit(limit)
        url = self.base + '/v0.1/servers?' + urlencode({'search': query, 'version': 'latest', 'limit': limit})
        raw = self.http.json(url, host='registry.modelcontextprotocol.io')
        results = []
        for entry in raw.get('servers', [])[:limit]:
            server = _get_dict(entry.get('server'))
            name = server.get('name')
            if not isinstance(name, str) or not name:
                continue
            meta = _get_dict(entry.get('_meta'))
            official = _get_dict(meta.get('io.modelcontextprotocol.registry/official'))
            if official.get('isLatest') is False:
                continue
            repo = _get_dict(server.get('repository')).get('url', '')
            website = server.get('websiteUrl', '')
            url = repo or website or self.base + '/v0.1/servers/' + quote(name, safe='') + '/versions/latest'
            results.append(MarketCandidate(
                id='mcp:' + name, kind='mcp', name=name,
                description=str(server.get('description') or ''), provider=self.name,
                source_url=url, canonical_source=(repo or name).rstrip('/'),
                version=str(server.get('version') or ''),
                metadata={'packages': server.get('packages', []), 'remotes': server.get('remotes', []),
                          'listing': 'https://registry.modelcontextprotocol.io',
                          'verification': 'metadata-only'},
            ))
        return results

    def resolve(self, candidate: MarketCandidate) -> None:
        return None  # Registry does not ship pinned executable bytes.


class GlamaMCP:
    name, kind = 'glama', 'mcp'
    base = 'https://glama.ai'

    def __init__(self, http: MarketplaceHTTP | None = None, api_key: str | None = None):
        self.http = http or MarketplaceHTTP()
        self.api_key = api_key

    def search(self, query: str, limit: int = 10) -> list[MarketCandidate]:
        limit = _safe_limit(limit)
        key = self.api_key or os.environ.get('GLAMA_API_KEY')
        if not key:
            raise MarketplaceAuthError('GLAMA_API_KEY is required')
        url = self.base + '/api/mcp/v1/servers?' + urlencode({'query': query, 'first': limit, 'sort': 'search-relevance:desc'})
        raw = self.http.json(url, host='glama.ai', token=key)
        results = []
        for item in raw.get('servers', [])[:limit]:
            if not isinstance(item, dict):
                continue
            slug = str(item.get('slug') or item.get('id') or '')
            if not slug:
                continue
            listing = item.get('url') or f'https://glama.ai/mcp/servers/{item.get("namespace", "unknown")}/{slug}'
            repo = _get_dict(item.get('repository')).get('url', '')
            results.append(MarketCandidate(
                id='glama:' + str(item.get('id') or slug), kind='mcp',
                name=str(item.get('name') or slug), description=str(item.get('description') or ''),
                provider=self.name, source_url=listing, canonical_source=(repo or listing).rstrip('/'),
                license=str(item.get('spdxLicense') or ''),
                metadata={'repository': repo, 'tools': [t.get('name') for t in item.get('tools', [])
                           if isinstance(t, dict)], 'quality_score': item.get('qualityScore'),
                          'attribution': 'Data from Glama — ' + listing,
                          'verification': 'metadata-only'},
            ))
        return results

    def resolve(self, candidate: MarketCandidate) -> None:
        return None


class SkillsSH:
    name, kind = 'skills.sh', 'skill'
    base = 'https://skills.sh'

    def __init__(self, http: MarketplaceHTTP | None = None, oidc_token: str | None = None):
        self.http = http or MarketplaceHTTP()
        self.oidc_token = oidc_token

    def _token(self):
        return self.oidc_token or os.environ.get('VERCEL_OIDC_TOKEN')

    def search(self, query: str, limit: int = 10) -> list[MarketCandidate]:
        limit = _safe_limit(limit)
        if len(query.strip()) < 2:
            raise ValueError('skills.sh queries require at least 2 characters')
        url = self.base + '/api/v1/skills/search?' + urlencode({'q': query, 'limit': limit})
        raw = self.http.json(url, host='skills.sh', token=self._token())
        results = []
        for item in raw.get('data', [])[:limit]:
            if not isinstance(item, dict):
                continue
            key = item.get('id')
            if not isinstance(key, str) or not key or '..' in key or key.startswith('/'):
                continue
            results.append(MarketCandidate(
                id='skill:' + key, kind='skill', name=str(item.get('name') or item.get('slug') or key),
                description=str(item.get('description') or ''), provider=self.name,
                source_url=str(item.get('url') or self.base + '/' + key),
                canonical_source='skill:' + key,
                metadata={'skill_id': key, 'installs': item.get('installs'),
                          'is_duplicate': bool(item.get('isDuplicate', False)),
                          'verification': 'not-validated'},
            ))
        return results

    def resolve(self, candidate: MarketCandidate) -> ResolvedSkill:
        if candidate.provider != self.name:
            raise MarketplaceError('Candidate belongs to another provider')
        key = candidate.metadata.get('skill_id', '')
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.\-/]{3,220}', key) or '..' in key:
            raise MarketplaceError('Invalid skills.sh ID')
        raw = self.http.json(self.base + '/api/v1/skills/' + quote(key, safe='/'),
                             host='skills.sh', token=self._token())
        files = {}
        for f in raw.get('files') or []:
            if isinstance(f, dict) and isinstance(f.get('path'), str) and isinstance(f.get('contents'), str):
                files[f['path']] = f['contents']
        if not files:
            raise MarketplaceError('No downloadable skill snapshot')
        return ResolvedSkill(candidate, files,
                             {'catalog': 'skills.sh', 'skill_id': key, 'remote_hash': raw.get('hash'),
                              'snapshot_sha256': hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()})


class AnthropicSkills:
    name, kind = 'anthropic-skills', 'skill'
    api = 'https://api.github.com'
    repo = 'anthropics/skills'

    def __init__(self, http: MarketplaceHTTP | None = None, github_token: str | None = None):
        self.http = http or MarketplaceHTTP()
        self.github_token = github_token
        self._tree: tuple[str, list[dict]] | None = None

    def _token(self):
        return self.github_token or os.environ.get('GITHUB_TOKEN')

    def _repository_tree(self):
        if self._tree is not None:
            return self._tree
        commit = self.http.json(self.api + '/repos/anthropics/skills/commits/main',
                                host='api.github.com', token=self._token())
        sha = commit.get('sha')
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40}', sha):
            raise MarketplaceError('GitHub commit SHA missing')
        tree = _get_dict(_get_dict(commit.get('commit')).get('tree')).get('sha')
        if not isinstance(tree, str) or not re.fullmatch(r'[0-9a-f]{40}', tree):
            raise MarketplaceError('GitHub tree SHA missing')
        raw = self.http.json(self.api + '/repos/anthropics/skills/git/trees/' + tree + '?recursive=1',
                             host='api.github.com', token=self._token())
        if raw.get('truncated'):
            raise MarketplaceError('GitHub tree truncated')
        entries = raw.get('tree', [])
        if not isinstance(entries, list):
            raise MarketplaceError('Invalid GitHub tree')
        self._tree = (sha, entries)
        return self._tree

    def search(self, query: str, limit: int = 10) -> list[MarketCandidate]:
        limit = _safe_limit(limit)
        sha, entries = self._repository_tree()
        words = [s.lower() for s in query.split() if s]
        candidates = []
        for e in entries:
            path = e.get('path', '') if isinstance(e, dict) else ''
            if not isinstance(e, dict) or e.get('type') != 'blob' or not path.endswith('/SKILL.md') or not path.startswith('skills/'):
                continue
            folder = path[:-len('/SKILL.md')]
            slug = folder.rsplit('/', 1)[-1]
            if words and not all(w in (folder.lower() + ' ' + slug.replace('-', ' ')) for w in words):
                continue
            candidates.append(MarketCandidate(
                id='skill:anthropics/skills/' + folder, kind='skill', name=slug,
                description='Anthropic official example Skill: ' + slug,
                provider=self.name,
                source_url=f'https://github.com/anthropics/skills/tree/{sha}/{folder}',
                canonical_source=f'https://github.com/anthropics/skills/tree/{folder}',
                version=sha,
                metadata={'folder': folder, 'commit': sha, 'verification': 'not-validated',
                          'license': 'Check repository skill-specific LICENSE before reuse'},
            ))
            if len(candidates) >= limit:
                break
        return candidates

    def resolve(self, candidate: MarketCandidate) -> ResolvedSkill:
        if candidate.provider != self.name:
            raise MarketplaceError('Candidate belongs to another provider')
        sha = candidate.metadata.get('commit', '')
        folder = candidate.metadata.get('folder', '')
        if not re.fullmatch(r'[0-9a-f]{40}', sha) or not re.fullmatch(r'skills/[\w.\-/]+', folder) or '..' in folder:
            raise MarketplaceError('Invalid pinned Anthropic Skill metadata')
        tree_sha, entries = self._repository_tree()
        if sha != tree_sha:
            raise MarketplaceError('Pinned commit changed; create a new search result')
        subtree = [e for e in entries if isinstance(e, dict) and e.get('type') == 'blob'
                   and str(e.get('path', '')).startswith(folder + '/')]
        if not subtree or len(subtree) > MAX_FILES:
            raise MarketplaceError('Skill files missing or exceed limit')
        files = {}
        budget = 0
        for e in subtree:
            relative = e['path'][len(folder) + 1:]
            if not relative or e.get('size', 0) > MAX_TOTAL_FILES:
                raise MarketplaceError('Oversize skill file')
            # GitHub Git Blob API pins each object to its own content SHA.
            blob_sha = e.get('sha')
            if not isinstance(blob_sha, str) or not re.fullmatch(r'[0-9a-f]{40}', blob_sha):
                raise MarketplaceError('Invalid GitHub blob ID')
            blob = self.http.json(self.api + '/repos/anthropics/skills/git/blobs/' + blob_sha,
                                  host='api.github.com', token=self._token())
            import base64
            if blob.get('encoding') != 'base64':
                raise MarketplaceError('Unsupported GitHub blob encoding')
            encoded = blob.get('content', '')
            try:
                content = base64.b64decode(encoded, validate=False).decode('utf-8')
            except (ValueError, UnicodeError) as exc:
                raise MarketplaceError('Only UTF-8 text skill assets are supported') from exc
            budget += len(content.encode())
            if budget > MAX_TOTAL_FILES:
                raise MarketplaceError('Skill bundle exceeds size limit')
            files[relative] = content
        return ResolvedSkill(candidate, files, {'catalog': 'anthropic/skills', 'commit': sha})


class FederatedDiscovery:
    """Provider failures are isolated; no auto-install, no arbitrary host fetch."""
    def __init__(self, providers: list[MarketplaceAdapter], *, staging_root: str | Path | None = None):
        self.providers = {p.name: p for p in providers}
        self.staging_root = Path(staging_root or tempfile.gettempdir())

    def search(self, query: str, kind: str, limit: int = 10) -> dict:
        limit = _safe_limit(limit)
        if kind not in ('skill', 'mcp'):
            raise ValueError('kind must be skill or mcp')
        candidates, errors, seen = [], {}, set()
        for provider in self.providers.values():
            if provider.kind != kind:
                continue
            try:
                matches = provider.search(query, limit)
            except (MarketplaceError, OSError, ValueError) as exc:
                errors[provider.name] = str(exc)
                continue
            for candidate in matches:
                key = (candidate.kind, candidate.canonical_source.lower().removesuffix('.git'))
                if key in seen:
                    continue
                seen.add(key)
                candidates.append(candidate)
        return {'candidates': candidates[:limit], 'errors': errors,
                'providers': [p.name for p in self.providers.values() if p.kind == kind]}

    def stage(self, candidate: MarketCandidate) -> Path:
        provider = self.providers.get(candidate.provider)
        if provider is None or provider.kind != 'skill' or candidate.kind != 'skill':
            raise MarketplaceError('Only Skill candidates with supported provider snapshots can be staged')
        resolved = provider.resolve(candidate)
        if resolved is None:
            raise MarketplaceError('Provider does not support materializing verified bytes')
        return resolved.stage(self.staging_root)

    async def stage_async(self, candidate: MarketCandidate) -> Path:
        return await asyncio.to_thread(self.stage, candidate)


def default_p0(*, staging_root: str | Path | None = None, http: MarketplaceHTTP | None = None):
    return FederatedDiscovery([OfficialMCPRegistry(http), GlamaMCP(http),
                               SkillsSH(http), AnthropicSkills(http)], staging_root=staging_root)
