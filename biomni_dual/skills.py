"""Local read-only SKILL.md discovery and dependency declaration parsing."""
from pathlib import Path
import yaml
from .models import Capability, CapabilityKind
from .registry import CapabilityRegistry

class SkillManager:
    def __init__(self, registry: CapabilityRegistry, root: str | Path):
        self.registry = registry
        self.root = Path(root).resolve()
        self.paths: dict[str, Path] = {}

    def discover(self) -> list[Capability]:
        if not self.root.is_dir():
            return []
        found = []
        for path in sorted(self.root.rglob('SKILL.md')):
            if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != self.root):
                continue
            raw = path.read_text(encoding='utf-8')
            if not raw.startswith('---\n'):
                continue
            parts = raw.split('---\n', 2)
            if len(parts) != 3:
                continue
            meta = yaml.safe_load(parts[1]) or {}
            if not isinstance(meta, dict):
                continue
            name = meta.get('name')
            if not isinstance(name, str) or not name:
                continue
            requirement = meta.get('requires', {}) or {}
            if isinstance(requirement, dict):
                requirement = requirement.get('tools', []) or []
            if not isinstance(requirement, list) or not all(isinstance(x, str) for x in requirement):
                raise ValueError(f"Invalid requirements in {path}")
            item = Capability(id=f"skill.{name}", description=str(meta.get('description', '')), kind=CapabilityKind.SKILL, version=str(meta.get('version','1.0.0')), requires=tuple(requirement), tags=tuple(meta.get('tags', []) or []), approval_required=bool(meta.get('approval_required', False)), metadata={'source': str(path)})
            self.registry.register(item, replace=True)
            self.paths[item.id] = path
            found.append(item)
        return found

    def load_instructions(self, skill_id: str) -> str:
        """Return instructions as untrusted context, never execute markdown automatically."""
        path = self.paths[skill_id]
        if not path.is_relative_to(self.root):
            raise PermissionError('Skill file outside allowed directory')
        return path.read_text(encoding='utf-8')
