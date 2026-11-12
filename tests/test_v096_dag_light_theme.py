"""Readable light graph styling without changing task navigation or graph functionality."""
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "biomni_dual" / "web" / "collaboration.html"


def test_dag_and_details_are_light_with_legible_text():
    html = HTML.read_text(encoding="utf-8")
    assert "v0.9.6: accessible light DAG" in html
    assert ".dagviewport,.topoview{" in html
    assert "#f8fafc!important" in html
    assert ".dagdetails,.topodetail{" in html
    assert "background:#fff!important;color:#1a2433!important" in html
    assert ".graphshell .dag-node text{fill:#172334!important}" in html
    assert ".graphshell .dag-node text:nth-of-type(2){fill:#4a596c!important}" in html
    assert '.dag-node" tabindex="0" role="button" aria-selected="${chosen}"' in html
    assert ".graphshell .dag-node[aria-selected=\"true\"] .node-rect" in html


def test_existing_graph_controls_and_rail_remain():
    html = HTML.read_text(encoding="utf-8")
    for control in ("graph-search", "graph-filter", "graph-zoom-in", "graph-zoom-out"):
        assert control in html
    assert 'data-page="tasks"' in html
    assert "function renderGraph(target)" in html
    assert "id=\"dag-result-detail\"" in html
    assert "grid-template-columns:58px 304px minmax(0,1fr)" in html
