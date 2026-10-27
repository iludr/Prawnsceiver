// DAB+ page: ensemble + programme info + MOT slideshow, backed by
// /dab.json (welle-cli status, see noaa_receiver/decoding/dab.py).
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

let currentSid = null;   // service id currently playing
let services = [];       // the ensemble's service list (last /dab.json)

function applyStatus(s) {
    try {
        services = s.services || [];
        document.getElementById('servcount').textContent = services.length;
        const ens = s.ensemble || {};
        ensLabel = ens.label || ens.shortlabel || '—';
        document.getElementById('ensname').textContent = ensLabel;
        const rows = document.getElementById('servrows');
        rows.innerHTML = services.length
            ? services.map(serviceHtml).join('')
            : '<tr><td colspan="4" class="muted">No stations yet — tune the receiver to an ensemble.</td></tr>';
        updateNowPlaying();
    } catch (e) { console.error('Error applying DAB+ status:', e); }
}

function serviceHtml(s) {
    return '<tr data-sid="' + esc(s.sid) + '" class="' + (s.sid === currentSid ? 'active' : '') + '">'
        + '<td class="id">' + esc(s.label || s.shortlabel || s.sid) + '</td>'
        + '<td>' + esc(s.shortlabel || '') + '</td>'
        + '<td class="txt dls">' + esc(s.dls || '') + '</td>'
        + '<td class="mot">' + (s.mot ? '🖼' : '') + '</td>'
        + '</tr>';
}

function updateNowPlaying() {
    const cur = services.find(s => s.sid === currentSid);
    const name = cur ? (cur.label || cur.shortlabel || cur.sid) : '—';
    document.getElementById('npname').textContent = name;
    document.getElementById('npens').textContent = ensLabel;
    document.getElementById('npdls').textContent = cur ? (cur.dls || '') : '';
    document.querySelectorAll('#servrows tr').forEach(tr => {
        tr.classList.toggle('active', tr.dataset.sid === String(currentSid));
    });
    updateSlide(cur);
}

// ---------- playback + station switching ----------
function play(sid) {
    currentSid = sid;
    const player = document.getElementById('player');
    player.src = '/dab_audio/' + encodeURIComponent(sid);
    player.play().catch(() => {});
    updateNowPlaying();
}

function step(delta) {
    if (!services.length) return;
    let i = services.findIndex(s => s.sid === currentSid);
    i = i < 0 ? 0 : (i + delta + services.length) % services.length;
    play(services[i].sid);
}

document.getElementById('prevbtn').addEventListener('click', () => step(-1));
document.getElementById('nextbtn').addEventListener('click', () => step(1));
document.getElementById('servrows').addEventListener('click', e => {
    const tr = e.target.closest('tr');
    if (tr && tr.dataset.sid) play(tr.dataset.sid);
});

// ---------- live updates (HTTP fallback poll + WebSocket push) ----------
async function load() {
    try {
        applyStatus(await (await fetch('/dab.json')).json());
    } catch (err) {
        console.error('Error applying DAB+ status:', err);
    }
}
setInterval(() => { if (!dabWsLive()) load(); }, 5000);
load();

let dabSocket = null, dabWsBackoff = 2000;
function dabWsLive() { return dabSocket && dabSocket.readyState === 1; }
function sendDabSub() {
    try { dabSocket.send('sub=dab'); } catch (e) {}
}
function connectDabWs() {
    try {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        const ws = new WebSocket(proto + location.host + '/waterfall.ws');
        dabSocket = ws;
        ws.onopen = () => { dabWsBackoff = 2000; sendDabSub(); };
        ws.onmessage = ev => {
            if (typeof ev.data === 'string') return;
            const u = new Uint8Array(ev.data);
            if (u.length < 4 || u[0] !== 2) return;
            const nlen = (u[1] << 8) | u[2];
            if (u.length < 3 + nlen) return;
            const topic = new TextDecoder().decode(u.subarray(3, 3 + nlen));
            try {
                const data = JSON.parse(new TextDecoder().decode(u.subarray(3 + nlen)));
                if (topic === 'dab') applyStatus(data);
            } catch (e) { console.error('WS topic ' + topic + ' failed:', e); }
        };
        ws.onclose = () => {
            dabSocket = null;
            setTimeout(connectDabWs, dabWsBackoff);
            dabWsBackoff = Math.min(60000, dabWsBackoff * 2);
        };
        ws.onerror = () => { try { ws.close(); } catch (e) {} };
    } catch (e) {
        dabSocket = null;
        setTimeout(connectDabWs, dabWsBackoff);
        dabWsBackoff = Math.min(60000, dabWsBackoff * 2);
    }
}
connectDabWs();
