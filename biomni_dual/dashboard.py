"""Single-page, local-only operations dashboard for Biomni Dual Center.

No CDN, cookies, script dependencies, credentials, research payloads, or writes.
"""
from __future__ import annotations

from pathlib import Path

DASHBOARD_HTML = (Path(__file__).parent / "web" / "dashboard.html").read_text(encoding="utf-8")
