"""v0.9.4 selected Session/Task results share the composer without changing the rail."""
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / "biomni_dual" / "web" / "collaboration.html"


def test_rail_and_three_column_shell_stay_unchanged():
    html = HTML.read_text(encoding="utf-8")
    rail = html[html.index('<div class="icon-rail"'):html.index('<aside class="history-sidebar"')]
    assert 'grid-template-columns:58px 304px minmax(0,1fr)' in html
    for page in ("home", "overview", "tasks", "topology", "workers", "logs", "models", "members"):
        assert f'data-page="{page}"' in rail
    assert 'id="home-messages"' in html
    assert 'id="home-form"' in html
    assert html.index('id="home-messages"') < html.index('id="home-form"')
    assert ".conversation-column .homecomposer{position:relative!important" in html
    assert ".conversation-column .homemessages{overflow-y:auto" in html


def test_session_and_task_selection_are_distinct():
    html = HTML.read_text(encoding="utf-8")
    assert "historySelection={kind:'session',id:chatSession}" in html
    assert "historySelection={kind:'task',id:btn.dataset.historyTask}" in html
    assert "historySelection.kind==='task'&&historySelection.id===x.id" in html
    assert "if(historySelection.kind==='task'){await loadSelectedTask();return;}" in html
    assert "historySelection.kind!=='session'" in html
    assert "data-task-dag" in html


def test_task_results_and_append_only_followups_are_scoped():
    html = HTML.read_text(encoding="utf-8")
    assert "taskFollowupSession=id=>'task-followup-'+id" in html
    assert "root()+'/tasks/'+encodeURIComponent(id)+'/trace'" in html
    assert "root()+'/tasks/'+encodeURIComponent(id)+'/result'" in html
    assert "root()+'/assistant/session/'+encodeURIComponent(taskFollowupSession(id))" in html
    assert "selection.kind==='task'?taskFollowupSession(selection.id):chatSession" in html
    assert "selection.id!==historySelection.id" in html
    assert "if(ws!==workspace||historySelection.kind!=='task'||historySelection.id!==id" in html
    assert "id=\"home-input\"" in html
    assert "el('home-form').onsubmit=sendUnifiedTurn" in html
