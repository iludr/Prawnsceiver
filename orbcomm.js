// Orbcomm packet log page: persistent decoded-packet table plus the
// live channel registry (/orbcomm.json). Like ais.js it renders from
// the HTTP endpoints and switches to the JSON-topic WebSocket push
// (/waterfall.ws: 'orbcomm', 'orbcommlog=N') when the socket is up.
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

function cell(v, cls) {
    return '<td' + (cls ? ' class="' + cls + '"' : '') + '>'
        + (v === null || v === undefined ? '<span class="na">—</span>' : v) + '</td>';
}

// Live channel registry render — shared by /orbcomm.json and the WS
// 'orbcomm' topic push
function applyStatus(s) {
    const chans = s.channels || [];
    document.getElementById('stats').textContent =
        (s.enabled ? 'Receiver live on dongle ' + s.dongle
                   : 'Orbcomm disabled (no Orbcomm dongle) — showing the persisted log')
        + ' · ' + chans.length + ' satellite channel' + (chans.length === 1 ? '' : 's')
        + ' · ' + chans.reduce((a, ch) => a + (ch.packets || 0), 0) + ' packets this run';
    const el = document.getElementById('channels');
    if (!chans.length) {
        el.innerHTML = '<tr><td colspan="6" class="muted">No satellite carriers in the band right now.</td></tr>';
        return;
    }
    let html = '';
    for (const ch of chans) {
        let pos = null;
        if (ch.lat !== null && ch.lat !== undefined) {
            pos = '<a href="https://www.google.com/maps?q=' + ch.lat + ',' + ch.lon
                + '" target="_blank" rel="noopener">' + ch.lat.toFixed(3) + ', ' + ch.lon.toFixed(3) + '</a>'
                + ' · ' + Math.round(ch.alt) + ' km';
        }
        html += '<tr>'
            + cell(esc(ch.sat_id ? ch.sat_id.toUpperCase() : '(no Sync yet)'), 'sat')
            + cell(esc(ch.key))
            + cell(esc(ch.freq_khz.toFixed(1) + ' kHz'), 'ch')
            + cell(esc(ch.packets))
            + cell(esc(ch.bad))
            + cell(pos, 'pos')
            + '</tr>';
    }
    el.innerHTML = html;
}

// Log tail render — shared by the /orbcommlog.json fallback poll and
// the WS 'orbcommlog' topic push (the server pushes the subscribed count)
function applyLog(log) {
    document.getElementById('loadinfo').textContent = ' · showing ' + log.length + ' packets';
    const rows = document.getElementById('rows');
    if (!log.length) {
        rows.innerHTML = '<tr><td colspan="7" class="muted">No Orbcomm packets decoded yet — the log fills as satellites pass over.</td></tr>';
        return;
    }
    let html = '';
    for (const e of log) {
        let pos = null;
        if (e.lat !== null && e.lat !== undefined) {
            pos = '<a href="https://www.google.com/maps?q=' + e.lat + ',' + e.lon
                + '" target="_blank" rel="noopener">' + e.lat.toFixed(3) + ', ' + e.lon.toFixed(3) + '</a>';
        }
        html += '<tr>'
            + cell(esc(e.time), 't')
            + cell(esc(e.dongle))
            + cell(esc(e.freq_khz + ' kHz'), 'ch')
            + cell(esc(e.type), 'type')
            + cell(esc(e.sat_id ? e.sat_id.toUpperCase() : ''), 'sat')
            + cell(pos, 'pos')
            + cell(esc(e.hex), 'hex')
            + '</tr>';
    }
    rows.innerHTML = html;
}

async function load() {
    try {
        const n = document.getElementById('count').value;
        applyLog(await (await fetch('/orbcommlog.json?count=' + n)).json());
        applyStatus(await (await fetch('/orbcomm.json')).json());
    } catch (err) {
        console.error('Orbcomm log load failed:', err);
    }
}

document.getElementById('count').addEventListener('change', () => {
    // new tail length: re-subscribe (the server pushes the new count
    // at once); without the socket the HTTP fallback loads it directly
    if (orbWsLive()) sendOrbSub(); else load();
});
let timer = null;
function setTimer() {
    if (timer) clearInterval(timer);
    if (document.getElementById('autorefresh').checked)
        timer = setInterval(() => { if (!orbWsLive()) load(); }, 10000);
}
document.getElementById('autorefresh').addEventListener('change', setTimer);
load();
setTimer();

// ---------- WebSocket live updates (JSON topic push over /waterfall.ws) ----
// Subscribes to the Orbcomm topics; the HTTP polling above keeps
// running as the fallback only while the socket is down.
let orbSocket = null, orbWsBackoff = 2000;
function orbWsLive() { return orbSocket && orbSocket.readyState === 1; }
function sendOrbSub() {
    try { orbSocket.send('sub=orbcomm,orbcommlog=' + (document.getElementById('count').value || 200)); } catch (e) {}
}
function connectOrbWs() {
    try {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        const ws = new WebSocket(proto + location.host + '/waterfall.ws');
        orbSocket = ws;
        ws.binaryType = 'arraybuffer';
        ws.onopen = () => { orbWsBackoff = 2000; sendOrbSub(); };
        ws.onmessage = ev => {
            if (typeof ev.data === 'string') return;
            const u = new Uint8Array(ev.data);
            if (u.length < 4 || u[0] !== 2) return;   // no waterfall rows here
            const nlen = (u[1] << 8) | u[2];
            if (u.length < 3 + nlen) return;
            const topic = new TextDecoder().decode(u.subarray(3, 3 + nlen));
            try {
                const data = JSON.parse(new TextDecoder().decode(u.subarray(3 + nlen)));
                if (topic === 'orbcomm') applyStatus(data);
                else if (topic === 'orbcommlog') applyLog(data);
            } catch (e) { console.error('WS topic ' + topic + ' failed:', e); }
        };
        ws.onclose = () => {
            orbSocket = null;
            setTimeout(connectOrbWs, orbWsBackoff);
            orbWsBackoff = Math.min(60000, orbWsBackoff * 2);
        };
        ws.onerror = () => { try { ws.close(); } catch (e) {} };
    } catch (e) {
        orbSocket = null;
        setTimeout(connectOrbWs, orbWsBackoff);
        orbWsBackoff = Math.min(60000, orbWsBackoff * 2);
    }
}
connectOrbWs();
