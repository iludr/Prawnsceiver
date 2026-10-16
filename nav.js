/* Site chrome (burger menu + shared header), injected into every page:
 * dashboard and all log/history sub-pages. One shared file so the look
 * never diverges: each HTML page only carries <script src="/nav.js">.
 *
 * The pages provide the same header markup (<div id="header"> with
 * .head-left / .head-right); the styles for it live here. The prefix
 * derivation below keeps everything working behind the notebook's
 * prefix-proxy (/prawnceiver/): nginx rewrites the src attribute in
 * the HTML, so the script tag knows the app root even when the JS
 * body itself is not rewritten (sub_filter defaults to text/html). */
(function () {
    /* Each entry: [path, emoji, label, thumbnail, full image] — the last
       two are relative to the app root (prefix) and optional. The menu
       shows the thumbnail; clicking it opens the full-size original in
       a lightbox, clicking the label navigates as before. */
    var PAGES = [
        ['/',        '🦀',  'Dashboard',                 'prawnceiver.jpg',         'prawnceiver_full.jpg'],
        ['/history', '📚',  'Pass History & Recordings', 'img/thumbs/history.jpg', 'img/history.png'],
        ['/ais',     '🚢',  'AIS Ship Traffic',          'img/thumbs/AIS.jpg',     'img/AIS.png'],
        ['/orbcomm', '🛰',  'Orbcomm Downlink',          'img/thumbs/Orbcomm.jpg', 'img/Orbcomm.png'],
        ['/aprs',    '📡',  'APRS Packet Log',           'img/thumbs/APRS.jpg',    'img/APRS.png'],
        ['/pocsag',  '📟',  'POCSAG Paging',            'img/thumbs/POCSAG.jpg',  'img/POCSAG.png'],
        ['/console', '🛠️', 'Debug Console',             'img/thumbs/debug.jpg',   'img/debug.png']
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
        /* --- shared header band: same chrome on dashboard and sub-pages --- */
        '#header { background: #16213e; border-bottom: 2px solid #0f3460;' +
        ' padding: 12px 20px; display: flex; justify-content: space-between;' +
        ' align-items: center; gap: 14px; flex-wrap: wrap; }' +
        '#header .head-left { display: flex; align-items: center; gap: 12px;' +
        ' min-width: 0; }' +
        '#header .head-left img.logo { height: 48px; width: auto;' +
        ' border-radius: 6px; border: 1px solid #0f3460; flex-shrink: 0; }' +
        '#header h1 { font-size: 1.25em; color: #53d769; font-weight: bold; }' +
        '#header .info { font-size: 0.85em; color: #888; margin-top: 3px; }' +
        '#header .head-right { font-size: 0.85em; color: #888;' +
        ' text-align: right; margin-left: auto; }' +
        '#header .info a, #header .head-right a { color: #53d769;' +
        ' text-decoration: none; }' +
        '#header .info a:hover, #header .head-right a:hover {' +
        ' text-decoration: underline; }' +
        /* --- burger menu --- */
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
        '#nav-burger.in-header { position: static; flex-shrink: 0; }' +
        '#nav-panel { display: none; position: fixed; z-index: 9998;' +
        ' min-width: 235px; background: #16213e; border: 1px solid #0f3460;' +
        ' border-left: 3px solid #53d769; border-radius: 4px; padding: 6px 0;' +
        ' box-shadow: 0 6px 18px rgba(0,0,0,0.6); }' +
        '#nav-panel.open { display: block; }' +
        '#nav-panel a { display: flex; align-items: center; gap: 10px;' +
        ' padding: 8px 14px; color: #a8b8d8; text-decoration: none;' +
        ' font-size: 0.95em; white-space: nowrap; }' +
        '#nav-panel a:hover { background: #0f3460; color: #e0e0e0; }' +
        '#nav-panel a.current { color: #53d769; font-weight: bold; }' +
        '#nav-panel a.current::after { content: " ◀"; font-size: 0.7em; }' +
        '#nav-panel img.nav-thumb { width: 84px; height: 52px; object-fit:' +
        ' cover; border: 1px solid #0f3460; border-radius: 3px;' +
        ' flex-shrink: 0; cursor: zoom-in; }' +
        /* --- lightbox: full-size picture opened from a menu thumbnail --- */
        '#nav-lightbox { display: none; position: fixed; top: 0; left: 0;' +
        ' right: 0; bottom: 0; background: rgba(0,0,0,0.85); z-index: 10000;' +
        ' align-items: center; justify-content: center; cursor: zoom-out; }' +
        '#nav-lightbox img { max-width: 92vw; max-height: 92vh;' +
        ' border-radius: 8px; box-shadow: 0 0 40px rgba(0,0,0,0.8); }';
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

    var lightbox = document.createElement('div');
    lightbox.id = 'nav-lightbox';
    var lbImg = document.createElement('img');
    lightbox.appendChild(lbImg);
    lightbox.addEventListener('click', function () {
        lightbox.style.display = 'none';
        lbImg.removeAttribute('src');
    });

    PAGES.forEach(function (p) {
        var a = document.createElement('a');
        a.href = (p[0] === '/') ? prefix : prefix + p[0].slice(1);
        if (p[0] === here) a.className = 'current';
        if (p[3]) {
            var img = document.createElement('img');
            img.className = 'nav-thumb';
            img.src = prefix + p[3];
            img.alt = p[2] + ' picture (click to view full size)';
            img.addEventListener('click', function (e) {
                // Click on the thumbnail: show the original picture,
                // do NOT follow the menu link around it
                e.preventDefault();
                e.stopPropagation();
                lbImg.src = prefix + p[4];
                lbImg.alt = p[2] + ' (full size)';
                lightbox.style.display = 'flex';
            });
            a.appendChild(img);
        }
        a.appendChild(document.createTextNode(p[1] + ' ' + p[2]));
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
        if (e.key === 'Escape') {
            lightbox.style.display = 'none';
            lbImg.removeAttribute('src');
            close();
        }
    });

    var header = document.getElementById('header');
    var mount = (header && header.querySelector('.head-left')) || header;
    if (mount) {
        // Burger lives in the header band, left of the logo.
        btn.classList.add('in-header');
        mount.insertBefore(btn, mount.firstChild);
    } else {
        // Page without a header band: fixed in the top-left corner,
        // content shifted right so it clears the button.
        document.body.style.paddingLeft = '50px';
        document.body.appendChild(btn);
    }
    document.body.appendChild(panel);
    document.body.appendChild(lightbox);
})();
