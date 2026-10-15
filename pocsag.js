// POCSAG page: tracked paging channels + decoded pages, from /pocsag.json
// and /pocsaglog.json (HTTP fallback poll) or the WS 'pocsag' /
// 'pocsaglog' topic push.
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function cell(v, cls) {
    return '<td' + (cls ? ' class="' + cls + '"' : '') + '>'
        + (v === null || v === undefined ? '<span class="muted">—</span>' : v) + '</td>';
}
// The server logs in ITS timezone (the container runs UTC) — every
// entry also carries unix ts, so the list shows browser-local time
function fmtTime(ts) {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}

// ---------- live status (channels + recent pages) ----------
function applyStatus(s) {
    const ch = s.channels || [];
    document.getElementById('chcount').textContent = ch.length;
    document.getElementById('stats').textContent =
        (s.enabled ? 'Receiver live on dongle ' + s.dongle
                   : 'POCSAG disabled (no dongle in the paging role) — the page log below persists')
        + ' · ' + (s.freq_mhz ? s.freq_mhz.toFixed(3) + ' MHz' : '') + ' · ' + ch.length + ' channel' + (ch.length === 1 ? '' : 's') + ' tracked';
    const crows = document.getElementById('chrows');
    if (!ch.length) {
        crows.innerHTML = '<tr><td colspan="4" class="muted">No paging channels tracked yet — they appear as the site keys up.</td></tr>';
    } else {
        let h = '';
        for (const c of ch) {
            h += '<tr>'
                + cell(esc(c.freq_mhz !== undefined ? c.freq_mhz.toFixed(4) + ' MHz' : ''), 'ch')
                + cell(esc(c.level_dbfs !== undefined ? c.level_dbfs.toFixed(1) + ' dBFS' : ''))
                + cell(esc(c.last_seen_age_s !== undefined ? c.last_seen_age_s.toFixed(1) + ' s ago' : ''), 't')
                + cell(esc(c.pages))
                + '</tr>';
        }
        crows.innerHTML = h;
    }
    // newest pages from the status ring (short) - the full log below
    if (s.pages && s.pages.length) {
        const rows = document.getElementById('rows');
        if (rows._from !== 'status' || !rows.children.length) {
            rows._from = 'status';
            rows.innerHTML = pagesHtml(s.pages);
        }
    }
}

function pagesHtml(pages) {
    let html = '';
    for (const p of pages) {
        html += '<tr>'
            + cell(esc(fmtTime(p.ts)), 't')
            + cell(esc(p.channel_mhz !== undefined ? p.channel_mhz.toFixed(4) : ''), 'ch')
            + cell(esc(p.ric !== undefined ? p.ric : ''), 'ric')
            + cell(esc(p.func !== undefined ? p.func : ''))
            + cell(esc(p.type || ''), 'type')
            + cell(esc(p.text || ''), 'txt')
            + '</tr>';
    }
    return html;
}

// ---------- full page log ----------
function applyLog(pages) {
    const rows = document.getElementById('rows');
    if (!pages.length) {
        rows._from = 'log';
        rows.innerHTML = '<tr><td colspan="6" class="muted">No pages decoded yet — the log fills as the paging site keys up.</td></tr>';
        return;
    }
    rows._from = 'log';
    rows.innerHTML = pagesHtml(pages);
}

async function load() {
    try {
        applyStatus(await (await fetch('/pocsag.json')).json());
        applyLog(await (await fetch('/pocsaglog.json?count=200')).json());
    } catch (err) {
        console.error('POCSAG load failed:', err);
    }
}
setInterval(() => { if (!pocsagWsLive()) load(); }, 10000);
load();

// ---------- WebSocket live updates ----------
let pocsagSocket = null, pocsagWsBackoff = 2000;
function pocsagWsLive() { return pocsagSocket && pocsagSocket.readyState === 1; }
function sendPocsagSub() {
    try { pocsagSocket.send('sub=pocsag,pocsaglog=200'); } catch (e) {}
}
function connectPocsagWs() {
    try {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        const ws = new WebSocket(proto + location.host + '/waterfall.ws');
        pocsagSocket = ws;
        ws.binaryType = 'arraybuffer';
        ws.onopen = () => { pocsagWsBackoff = 2000; sendPocsagSub(); };
        ws.onmessage = ev => {
            if (typeof ev.data === 'string') return;
            const u = new Uint8Array(ev.data);
            if (u.length < 4 || u[0] !== 2) return;
            const nlen = (u[1] << 8) | u[2];
            if (u.length < 3 + nlen) return;
            const topic = new TextDecoder().decode(u.subarray(3, 3 + nlen));
            try {
                const data = JSON.parse(new TextDecoder().decode(u.subarray(3 + nlen)));
                if (topic === 'pocsag') applyStatus(data);
                else if (topic === 'pocsaglog') applyLog(data);
            } catch (e) { console.error('WS topic ' + topic + ' failed:', e); }
        };
        ws.onclose = () => {
            pocsagSocket = null;
            setTimeout(connectPocsagWs, pocsagWsBackoff);
            pocsagWsBackoff = Math.min(60000, pocsagWsBackoff * 2);
        };
        ws.onerror = () => { try { ws.close(); } catch (e) {} };
    } catch (e) {
        pocsagSocket = null;
        setTimeout(connectPocsagWs, pocsagWsBackoff);
        pocsagWsBackoff = Math.min(60000, pocsagWsBackoff * 2);
    }
}
connectPocsagWs();
