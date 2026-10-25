// ISM page: decoded 433.92 MHz sensor telemetry, from /ism.json and
// /ismlog.json (HTTP fallback poll) or the WS 'ism' / 'ismlog' topic push.
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function cell(v, cls) {
    return '<td' + (cls ? ' class="' + cls + '"' : '') + '>'
        + (v === null || v === undefined || v === '' ? '<span class="muted">—</span>' : v) + '</td>';
}
function fmtTime(ts) {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}
function fmtTemp(t) { return (t === null || t === undefined) ? null : t.toFixed(1) + ' °C'; }
const PROTO_NAMES = { nexus: '🌡 Nexus', rubicson: '🌡 Rubicson', pwm48: '📶 PWM (unnamed)' };

function sensorHtml(s) {
    return '<tr>'
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

function packetHtml(p) {
    return '<tr>'
        + cell(esc(fmtTime(p.ts)), 't')
        + cell(p.freq_mhz !== undefined ? esc(p.freq_mhz.toFixed(4) + ' MHz') : null, 'proto')
        + cell(esc(PROTO_NAMES[p.proto] || p.proto || ''), 'proto')
        + cell(esc(p.id || ''), 'id')
        + cell(p.channel !== null && p.channel !== undefined ? esc(p.channel) : null)
        + cell(esc(fmtTemp(p.temp_c) || ''), 'temp')
        + cell(p.humidity !== null && p.humidity !== undefined ? esc(p.humidity + ' %RH') : null, 'hum')
        + cell(p.battery ? esc(p.battery) : null, p.battery === 'low' ? 'batt-low' : 'batt-ok')
        + cell(esc(p.hex || ''), 'hex')
        + '</tr>';
}

// ---------- live status (sensor registry + recent packets) ----------
function applyStatus(s) {
    const sensors = s.sensors || [];
    document.getElementById('senscount').textContent = sensors.length;
    document.getElementById('stats').textContent =
        (s.enabled ? 'Receiver live on dongle ' + s.dongle
                   : 'ISM decoding disabled (no dongle in the ISM role) — the packet log below persists')
        + ' · ' + (s.freq_mhz ? s.freq_mhz.toFixed(4) + ' MHz' : '') + ' · '
        + sensors.length + ' sensor' + (sensors.length === 1 ? '' : 's') + ' known';
    const srows = document.getElementById('sensorrows');
    srows.innerHTML = sensors.length
        ? sensors.map(sensorHtml).join('')
        : '<tr><td colspan="8" class="muted">No sensors yet — they appear as the nearest 433 MHz thermometers transmit.</td></tr>';
    if (s.recent && s.recent.length) {
        const rows = document.getElementById('rows');
        if (rows._from !== 'status' || !rows.children.length) {
            rows._from = 'status';
            rows.innerHTML = s.recent.slice().reverse().map(packetHtml).join('');
        }
    }
}

function applyLog(packets) {
    const rows = document.getElementById('rows');
    rows._from = 'log';
    rows.innerHTML = packets.length
        ? packets.map(packetHtml).join('')
        : '<tr><td colspan="9" class="muted">No packets decoded yet — the nearest 433 MHz sensors fill this log as they transmit.</td></tr>';
}

async function load() {
    try {
        applyStatus(await (await fetch('/ism.json')).json());
        applyLog(await (await fetch('/ismlog.json?count=200')).json());
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
    try { ismSocket.send('sub=ism,ismlog=200'); } catch (e) {}
}
function connectIsmWs() {
    try {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        const ws = new WebSocket(proto + location.host + '/waterfall.ws');
        ismSocket = ws;
        ws.binaryType = 'arraybuffer';
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
                else if (topic === 'ismlog') applyLog(data);
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
