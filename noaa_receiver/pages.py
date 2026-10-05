"""Web pages (debug console, pass history), loaded from HTML files.

The markup lives in real .html files under html/ — no Python string
escaping is involved, so JavaScript quotes are written exactly as the
browser expects them.
"""
import os

_HTML_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'html')


def _load_page(name):
    with open(os.path.join(_HTML_DIR, name), encoding='utf-8') as f:
        return f.read()


CONSOLE_HTML = _load_page('console.html')
HISTORY_HTML = _load_page('history.html')
