/* Burger menu (top left) — injected into every page: dashboard and all
 * log/history sub-pages. One shared file so the menu never goes stale:
 * each HTML page only carries <script src="/nav.js"></script>.
 *
 * The prefix derivation below keeps it working behind the notebook's
 * prefix-proxy (/prawnceiver/): nginx rewrites the src attribute in the
 * HTML, so the script tag knows the app root even when the JS body
 * itself is not rewritten (sub_filter defaults to text/html). */
(function () {
    var PAGES = [
        ['/',        '🦀',  'Dashboard'],
        ['/history', '📚',  'Pass History & Recordings'],
        ['/ais',     '🚢',  'AIS Ship Traffic'],
        ['/orbcomm', '🛰',  'Orbcomm Downlink'],
        ['/aprs',    '📡',  'APRS Packet Log'],
        ['/pocsag',  '📟',  'POCSAG Paging'],
        ['/console', '🛠️', 'Debug Console']
    ];

    var tag = document.querySelector('script[src*="nav.js"]');
    var prefix = tag ? tag.getAttribute('src').replace(/nav\.js.*$/, '') : '/';
    if (prefix.charAt(prefix.length - 1) !== '/') prefix += '/';

    var here = location.pathname;
    if (prefix !== '/' && here.indexOf(prefix) === 0) {
        here = here.slice(prefix.length - 1);
    }
    here = here.replace(/(\/[^./]+)\.html$/, '$1').replace(/\/+$/, '') || '/';

    var style = document.createElement('style');
    style.textContent =
        '#nav-burger { position: fixed; top: 10px; left: 10px; width: 38px;' +
        ' height: 34px; background: #16213e; border: 1px solid #0f3460;' +
        ' border-radius: 4px; cursor: pointer; z-index: 9999;' +
        ' display: flex; flex-direction: column; justify-content: center;' +
        ' align-items: center; gap: 4px; padding: 0;' +
        ' transition: background 0.2s; }' +
        '#nav-burger:hover { background: #16294e; }' +
        '#nav-burger span { display: block; width: 20px; height: 2px;' +
        ' background: #53d769; border-radius: 1px;' +
        ' transition: transform 0.2s, opacity 0.2s; }' +
        '#nav-burger.open span:nth-child(1) { transform: translateY(6px) rotate(45deg); }' +
        '#nav-burger.open span:nth-child(2) { opacity: 0; }' +
        '#nav-burger.open span:nth-child(3) { transform: translateY(-6px) rotate(-45deg); }' +
        '#nav-burger.in-header { position: static; flex-shrink: 0;' +
        ' margin-right: 12px; }' +
        '#nav-panel { display: none; position: fixed; z-index: 9998;' +
        ' min-width: 235px; background: #16213e; border: 1px solid #0f3460;' +
        ' border-left: 3px solid #53d769; border-radius: 4px; padding: 6px 0;' +
        ' box-shadow: 0 6px 18px rgba(0,0,0,0.6); }' +
        '#nav-panel.open { display: block; }' +
        '#nav-panel a { display: block; padding: 8px 14px; color: #a8b8d8;' +
        ' text-decoration: none; font-size: 0.95em; white-space: nowrap; }' +
        '#nav-panel a:hover { background: #0f3460; color: #e0e0e0; }' +
        '#nav-panel a.current { color: #53d769; font-weight: bold; }' +
        '#nav-panel a.current::after { content: " ◀"; font-size: 0.7em; }';
    document.head.appendChild(style);

    var btn = document.createElement('button');
    btn.id = 'nav-burger';
    btn.type = 'button';
    btn.title = 'Jump to…';
    btn.setAttribute('aria-label', 'Site menu');
    btn.setAttribute('aria-expanded', 'false');
    for (var i = 0; i < 3; i++) btn.appendChild(document.createElement('span'));

    var panel = document.createElement('nav');
    panel.id = 'nav-panel';
    PAGES.forEach(function (p) {
        var a = document.createElement('a');
        a.href = (p[0] === '/') ? prefix : prefix + p[0].slice(1);
        a.textContent = p[1] + ' ' + p[2];
        if (p[0] === here) a.className = 'current';
        panel.appendChild(a);
    });

    function close() {
        panel.classList.remove('open');
        btn.classList.remove('open');
        btn.setAttribute('aria-expanded', 'false');
    }

    btn.addEventListener('click', function (e) {
        e.stopPropagation();
        var open = !panel.classList.contains('open');
        close();
        if (open) {
            var r = btn.getBoundingClientRect();
            panel.style.top = (r.bottom + 6) + 'px';
            panel.style.left = Math.max(r.left, 10) + 'px';
            panel.classList.add('open');
            btn.classList.add('open');
            btn.setAttribute('aria-expanded', 'true');
        }
    });
    panel.addEventListener('click', function (e) { e.stopPropagation(); });
    document.addEventListener('click', close);
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') close();
    });

    var header = document.getElementById('header');
    if (header) {
        // Dashboard: slot the burger into the header bar, left of the logo.
        btn.classList.add('in-header');
        header.insertBefore(btn, header.firstChild);
    } else {
        // Sub-pages: fixed in the top-left corner; shift content right so
        // the h1 clears the button.
        document.body.style.paddingLeft = '50px';
        document.body.appendChild(btn);
    }
    document.body.appendChild(panel);
})();
