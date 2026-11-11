"""MCP metadata import, explicit approval and delegation to Biomni's add_mcp."""
from pathlib import Path
import yaml
from .models import Capability, CapabilityKind
from .registry import CapabilityRegistry

class MCPManager:
    def __init__(self, registry: CapabilityRegistry):
        self.registry = registry
        self.config_path: Path | None = None
        self.servers: dict = {}

    def load_config(self, path: str | Path) -> int:
        """Read metadata only; do not start servers or execute commands."""
        path = Path(path).resolve()
        cfg = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        servers = cfg.get('mcp_servers', {})
        if not isinstance(servers, dict):
            raise ValueError('mcp_servers must be a mapping')
        count = 0
        for server, settings in servers.items():
            if not isinstance(settings, dict) or not settings.get('enabled', True):
                continue
            for tool in settings.get('tools', []) or []:
                if not isinstance(tool, dict) or not tool.get('name'):
                    continue
                cap = Capability(id=f"mcp.{server}.{tool['name']}", description=tool.get('description', ''), kind=CapabilityKind.MCP, metadata={'server': server, 'tool': tool['name']})
                self.registry.register(cap, replace=True)
                count += 1
        self.config_path = path
        self.servers = servers
        return count

    def attach_to_biomni(self, agent, *, allow_server_start: bool = False):
        """Explicit opt-in: Biomni add_mcp may spawn subprocesses and execute commands."""
        if not allow_server_start:
            raise PermissionError('MCP startup requires allow_server_start=True')
        if self.config_path is None:
            raise ValueError('Load a configuration before attaching MCP')
        return agent.add_mcp(str(self.config_path))
