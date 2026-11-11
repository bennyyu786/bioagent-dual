"""v0.9.3: professional rail, durable history sidebar, conversation canvas."""
from pathlib import Path

HTML = Path(__file__).resolve().parents[1] / 'biomni_dual' / 'web' / 'collaboration.html'


def test_three_column_landmarks_and_order():
    html = HTML.read_text(encoding='utf-8')
    assert 'grid-template-columns:58px 304px minmax(0,1fr)' in html
    first = html.index('<div class="icon-rail"')
    second = html.index('<aside class="history-sidebar"')
    third = html.index('<main class="conversation-column"')
    assert first < second < third
    assert 'id="nav"' in html[first:second]
    assert 'id="history-index"' in html[second:third]
    assert 'id="page"' in html[third:]
    for page in ('home','overview','tasks','topology','workers','logs','models','members'):
        assert 'data-page="'+page+'"' in html[first:second]


def test_new_chat_retains_persistence_and_workspace_scoping():
    html = HTML.read_text(encoding='utf-8')
    assert "el('sidebar-new-chat').onclick" in html
    assert "chatSession='chat-'+crypto.randomUUID()" in html
    assert "refreshHistory(true)" in html
    assert "'/assistant/history?kind=sessions" in html
    assert "'/assistant/history?kind=tasks" in html
    assert '.history-sidebar{display:none!important}' in html
