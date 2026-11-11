"""Web regression: completed results open in the DAG, without separate menu."""
from pathlib import Path


def test_completed_result_opens_inside_dag():
    page=(Path(__file__).resolve().parents[1]/'biomni_dual/web/collaboration.html').read_text()
    assert 'data-page="results"' not in page
    assert "navigate('results')" not in page
    assert 'id="dag-result-detail"' in page or 'id=\"dag-result-detail\"' in page
    assert "if(el('dag-result-detail'))loadDagResult(selectedTask,false)" in page
    assert "root()+'/tasks/'+encodeURIComponent(id)+'/result'" in page
    assert 'function showResultView(' in page
    assert 'dagResultCache.clear()' in page
    assert 'data-page="reviews"' not in page  # No manual review navigation in auto-delivery mode
