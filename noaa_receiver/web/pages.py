"""Web pages (debug console, pass history), loaded from HTML files.

The markup lives in real .html files under html/ — no Python string
escaping is involved. The scripts live as served .js files in the
project root (dashboard.js / console.js / history.js / ais.js); the
HTML files carry no inline JavaScript.
"""
import os

_HTML_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'html')


def _load_page(name):
    with open(os.path.join(_HTML_DIR, name), encoding='utf-8') as f:
        return f.read()


CONSOLE_HTML = _load_page('console.html')
HISTORY_HTML = _load_page('history.html')
ADSB_HTML = _load_page('adsb.html')
AIS_HTML = _load_page('ais.html')
ORBCOMM_HTML = _load_page('orbcomm.html')
APRS_HTML = _load_page('aprs.html')
POCSAG_HTML = _load_page('pocsag.html')
TUNER_HTML = _load_page('tuner.html')
