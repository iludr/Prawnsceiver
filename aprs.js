// APRS packet log page: persistent decoded-frame table plus the live
// station registry (/aprs.json). Like orbcomm.js it renders from the
// HTTP endpoints and switches to the JSON-topic WebSocket push
// (/waterfall.ws: 'aprs', 'aprslog=N') when the socket is up.
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

function cell(v, cls) {
    return '<td' + (cls ? ' class="' + cls + '"' : '') + '>'
        + (v === null || v === undefined ? '<span class="na">—</span>' : v) + '</td>';
}

// Live station registry render — shared by /aprs.json and the WS
// 'aprs' topic push
function applyStatus(s) {
    const st = s.stations || [];
    document.getElementById('stats').textContent =
        (s.enabled ? 'Receiver live on dongle ' + s.dongle
                   : 'APRS disabled (no APRS dongle) — showing the persisted log')
        + (s.freq_mhz ? ' · ' + s.freq_mhz.toFixed(3) + ' MHz'
                      + (s.freq_mhz === 145.825 ? ' (ISS digipeater)' : '') : '')
        + ' · ' + st.length + ' station' + (st.length === 1 ? '' : 's') + ' heard this run'
        + (s.stats && s.stats.frames ? ' · ' + s.stats.frames + ' frames, ' + s.stats.bad + ' bad bursts' : '');
    const el = document.getElementById('stations');
    if (!st.length) {
        el.innerHTML = '<tr><td colspan="5" class="muted">No stations heard yet — the channel is quiet.</td></tr>';
        return;
    }
    let html = '';
    for (const c of st) {
        let pos = null;
        if (c.lat !== null && c.lat !== undefined) {
            pos = '<a href="https://www.google.com/maps?q=' + c.lat + ',' + c.lon
                + '" target="_blank" rel="noopener">' + c.lat.toFixed(4) + ', ' + c.lon.toFixed(4) + '</a>';
        }
        html += '<tr>'
            + cell(esc(c.call), 'call')
            + cell(esc(c.via || ''), 'via')
            + cell(pos, 'pos')
            + cell(esc(c.msgs))
            + cell(esc(c.text || ''), 'txt')
            + '</tr>';
    }
    el.innerHTML = html;
}

// Log tail render — shared by the /aprslog.json fallback poll and the
// WS 'aprslog' topic push (the server pushes the subscribed count)
function applyLog(log) {
    document.getElementById('loadinfo').textContent = ' · showing ' + log.length + ' frames';
    const rows = document.getElementById('rows');
    if (!log.length) {
        rows.innerHTML = '<tr><td colspan="6" class="muted">No APRS frames decoded yet — the log fills as stations beacon.</td></tr>';
        return;
    }
    let html = '';
    for (const e of log) {
        let pos = null;
        if (e.lat !== null && e.lat !== undefined) {
            pos = '<a href="https://www.google.com/maps?q=' + e.lat + ',' + e.lon
                + '" target="_blank" rel="noopener">' + e.lat.toFixed(4) + ', ' + e.lon.toFixed(4) + '</a>'
                + ' ' + esc(e.sym || '');
        }
        html += '<tr>'
            + cell(esc(e.time), 't')
            + cell(esc(e.call), 'call')
            + cell(esc(e.dst), 'via')
            + cell(esc(e.via || ''), 'via')
            + cell(pos, 'pos')
            + cell(esc(e.text || ''), 'txt')
            + '</tr>';
    }
    rows.innerHTML = html;
}

async function load() {
    try {
        const n = document.getElementById('count').value;
        applyLog(await (await fetch('/aprslog.json?count=' + n)).json());
        applyStatus(await (await fetch('/aprs.json')).json());
    } catch (err) {
        console.error('APRS log load failed:', err);
    }
}

document.getElementById('count').addEventListener('change', () => {
    // new tail length: re-subscribe (the server pushes the new count
    // at once); without the socket the HTTP fallback loads it directly
    if (aprsWsLive()) sendAprsSub(); else load();
});
let timer = null;
function setTimer() {
    if (timer) clearInterval(timer);
    if (document.getElementById('autorefresh').checked)
        timer = setInterval(() => { if (!aprsWsLive()) load(); }, 10000);
}
document.getElementById('autorefresh').addEventListener('change', setTimer);
load();
setTimer();

// ---------- WebSocket live updates (JSON topic push over /waterfall.ws) ----
// Subscribes to the APRS topics; the HTTP polling above keeps running
// as the fallback only while the socket is down.
let aprsSocket = null, aprsWsBackoff = 2000;
function aprsWsLive() { return aprsSocket && aprsSocket.readyState === 1; }
function sendAprsSub() {
    try { aprsSocket.send('sub=aprs,aprslog=' + (document.getElementById('count').value || 200)); } catch (e) {}
}
function connectAprsWs() {
    try {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        const ws = new WebSocket(proto + location.host + '/waterfall.ws');
        aprsSocket = ws;
        ws.binaryType = 'arraybuffer';
        ws.onopen = () => { aprsWsBackoff = 2000; sendAprsSub(); };
        ws.onmessage = ev => {
            if (typeof ev.data === 'string') return;
            const u = new Uint8Array(ev.data);
            if (u.length < 4 || u[0] !== 2) return;   // no waterfall rows here
            const nlen = (u[1] << 8) | u[2];
            if (u.length < 3 + nlen) return;
            const topic = new TextDecoder().decode(u.subarray(3, 3 + nlen));
            try {
                const data = JSON.parse(new TextDecoder().decode(u.subarray(3 + nlen)));
                if (topic === 'aprs') applyStatus(data);
                else if (topic === 'aprslog') applyLog(data);
            } catch (e) { console.error('WS topic ' + topic + ' failed:', e); }
        };
        ws.onclose = () => {
            aprsSocket = null;
            setTimeout(connectAprsWs, aprsWsBackoff);
            aprsWsBackoff = Math.min(60000, aprsWsBackoff * 2);
        };
        ws.onerror = () => { try { ws.close(); } catch (e) {} };
    } catch (e) {
        aprsSocket = null;
        setTimeout(connectAprsWs, aprsWsBackoff);
        aprsWsBackoff = Math.min(60000, aprsWsBackoff * 2);
    }
}
connectAprsWs();
