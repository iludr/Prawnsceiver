// ISM page: sensor registry (latest data per sensor) + 24 h hover graphs
// backed by /ism_history.json (the persistent ism_messages log).
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function fmtTime(ts) {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}
function fmtTemp(t) { return (t === null || t === undefined) ? null : t.toFixed(1) + ' °C'; }
const PROTO_NAMES = { nexus: '🌡 Nexus', rubicson: '🌡 Rubicson', pwm48: '📶 PWM (unnamed)' };

function cell(v, cls) {
    return '<td' + (cls ? ' class="' + cls + '"' : '') + '>'
        + (v === null || v === undefined || v === '' ? '<span class="muted">—</span>' : v) + '</td>';
}

function sensorHtml(s) {
    return '<tr data-sid="' + esc(s.id || '') + '">'
        + cell(esc(PROTO_NAMES[s.proto] || s.proto || ''), 'proto')
        + cell(esc(s.id || ''), 'id')
        + cell(s.channel !== null && s.channel !== undefined ? esc(s.channel) : null)
        + cell(esc(fmtTemp(s.temp_c) || ''), 'temp')
        + cell(s.humidity !== null && s.humidity !== undefined ? esc(s.humidity + ' %RH') : null, 'hum')
        + cell(s.battery ? esc(s.battery) : null, s.battery === 'low' ? 'batt-low' : 'batt-ok')
        + cell(s.last_seen_age_s !== undefined ? esc(s.last_seen_age_s.toFixed(0) + ' s ago') : null, 't')
        + cell(esc(s.msgs || 0))
        + '</tr>';
}

// ---------- live status (sensor registry) ----------
function applyStatus(s) {
    const sensors = s.sensors || [];
    document.getElementById('senscount').textContent = sensors.length;
    document.getElementById('stats').textContent =
        (s.enabled ? 'Receiver live on dongle ' + s.dongle
                   : 'ISM decoding disabled (no dongle in the ISM role)')
        + ' · ' + (s.freq_mhz ? s.freq_mhz.toFixed(4) + ' MHz' : '') + ' · '
        + sensors.length + ' sensor' + (sensors.length === 1 ? '' : 's') + ' known';
    const srows = document.getElementById('sensorrows');
    srows.innerHTML = sensors.length
        ? sensors.map(sensorHtml).join('')
        : '<tr><td colspan="8" class="muted">No sensors yet — they appear as the nearest 433 MHz thermometers transmit.</td></tr>';
}

// ---------- 24 h hover graphs ----------
const graphDiv = document.getElementById('ismgraph');
const graphTitle = document.getElementById('ismgraph-title');
const graphCv = document.getElementById('ismgraph-cv');
const histCache = {};        // sid -> {fetched, rows}
const HIST_TTL = 60000;      // refetch a sensor's series at most once a minute

function drawSeries(rows, metric) {
    const ctx = graphCv.getContext('2d');
    const W = graphCv.width, H = graphCv.height;
    ctx.clearRect(0, 0, W, H);
    const pts = rows.filter(r => r[metric] !== null && r[metric] !== undefined);
    if (pts.length < 2) {
        ctx.fillStyle = '#666';
        ctx.fillText('not enough data yet', 12, H / 2);
        return;
    }
    const t0 = pts[0].ts, t1 = pts[pts.length - 1].ts;
    let lo = Infinity, hi = -Infinity;
    for (const r of pts) {
        lo = Math.min(lo, r[metric]);
        hi = Math.max(hi, r[metric]);
    }
    if (hi === lo) { hi = lo + 1; lo = lo - 1; }
    else { const pad = (hi - lo) * 0.1; lo -= pad; hi += pad; }
    const px = ts => 38 + (ts - t0) / (t1 - t0 || 1) * (W - 48);
    const py = v => 8 + (hi - v) / (hi - lo) * (H - 26);
    ctx.fillStyle = '#888';
    ctx.font = '10px monospace';
    ctx.fillText(hi.toFixed(1), 2, 14);
    ctx.fillText(lo.toFixed(1), 2, H - 8);
    ctx.fillText(fmtTime(t0), 40, H - 2);
    const tLabel = fmtTime(t1);
    ctx.fillText(tLabel, W - tLabel.length * 6 - 4, H - 2);
    ctx.strokeStyle = '#53d769';
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    pts.forEach((r, i) => {
        const x = px(r.ts), y = py(r[metric]);
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    });
    ctx.stroke();
    // mark the latest point
    const last = pts[pts.length - 1];
    ctx.fillStyle = '#53d769';
    ctx.beginPath();
    ctx.arc(px(last.ts), py(last[metric]), 2.5, 0, 2 * Math.PI);
    ctx.fill();
}

async function showHistory(tr, metric, x, y) {
    const sid = tr.dataset.sid;
    if (!sid) return;
    let cache = histCache[sid];
    if (!cache || Date.now() - cache.fetched > HIST_TTL) {
        try {
            const rows = await (await fetch('/ism_history.json?id='
                + encodeURIComponent(sid) + '&hours=24')).json();
            cache = histCache[sid] = { fetched: Date.now(), rows };
        } catch (e) { return; }
    }
    // the row may have been re-rendered by a status update while fetching
    if (!document.querySelector('tr[data-sid="' + CSS.escape(sid) + '"]')) return;
    graphDiv.hidden = false;
    graphTitle.textContent = 'id ' + sid + ' — last 24 h';
    graphDiv.style.left = Math.min(x + 14, window.innerWidth - 300) + 'px';
    graphDiv.style.top = (y + 12) + 'px';
    drawSeries(cache.rows, metric);
}

function hideHistory() { graphDiv.hidden = true; }

// ---------- live updates (HTTP fallback poll + WebSocket push) ----------
async function load() {
    try {
        applyStatus(await (await fetch('/ism.json')).json());
    } catch (err) {
        console.error('ISM load failed:', err);
    }
}
setInterval(() => { if (!ismWsLive()) load(); }, 10000);
load();

// ---------- WebSocket live updates ----------
let ismSocket = null, ismWsBackoff = 2000;
function ismWsLive() { return ismSocket && ismSocket.readyState === 1; }
function sendIsmSub() {
    try { ismSocket.send('sub=ism'); } catch (e) {}
}
function connectIsmWs() {
    try {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        const ws = new WebSocket(proto + location.host + '/waterfall.ws');
        ismSocket = ws;
        ws.onopen = () => { ismWsBackoff = 2000; sendIsmSub(); };
        ws.onmessage = ev => {
            if (typeof ev.data === 'string') return;
            const u = new Uint8Array(ev.data);
            if (u.length < 4 || u[0] !== 2) return;
            const nlen = (u[1] << 8) | u[2];
            if (u.length < 3 + nlen) return;
            const topic = new TextDecoder().decode(u.subarray(3, 3 + nlen));
            try {
                const data = JSON.parse(new TextDecoder().decode(u.subarray(3 + nlen)));
                if (topic === 'ism') applyStatus(data);
            } catch (e) { console.error('WS topic ' + topic + ' failed:', e); }
        };
        ws.onclose = () => {
            ismSocket = null;
            setTimeout(connectIsmWs, ismWsBackoff);
            ismWsBackoff = Math.min(60000, ismWsBackoff * 2);
        };
        ws.onerror = () => { try { ws.close(); } catch (e) {} };
    } catch (e) {
        ismSocket = null;
        setTimeout(connectIsmWs, ismWsBackoff);
        ismWsBackoff = Math.min(60000, ismWsBackoff * 2);
    }
}
connectIsmWs();

// ---------- hover wiring (delegated: rows re-render on every poll) ----------
const srows = document.getElementById('sensorrows');
srows.addEventListener('mouseover', e => {
    const td = e.target.closest('td');
    const tr = e.target.closest('tr');
    if (!td || !tr) return;
    const metric = td.classList.contains('temp') ? 'temp_c'
                 : (td.classList.contains('hum') ? 'humidity' : null);
    if (metric) showHistory(tr, metric, e.pageX, e.pageY);
});
srows.addEventListener('mouseleave', hideHistory);
