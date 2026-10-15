// APRS packet log page: persistent decoded-frame table plus the live
// station registry (/aprs.json). Like orbcomm.js it renders from the
// HTTP endpoints and switches to the JSON-topic WebSocket push
// (/waterfall.ws: 'aprs', 'aprslog=N') when the socket is up.
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

function cell(v, cls) {
    return '<td' + (cls ? ' class="' + cls + '"' : '') + '>'
        + (v === null || v === undefined ? '<span class="na">—</span>' : v) + '</td>';
}

// The server logs in ITS timezone (the station runs UTC) — every
// entry also carries unix ts, so the times below are rendered
// browser-local (like the AIS page; the server-side "time" strings
// stay in the JSON only as a fallback).
function fmtTime(ts) {
    if (!ts) return '';
    return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
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
// WS 'aprslog' topic push. Identical frames (same call, destination,
// path and text — the typical beacon repeated every few minutes)
// collapse into one row: ×count, newest time, position from the newest
// frame that carries one. The map is fed per-callsign from the same
// window (latest decoded position of each station).
function applyLog(log) {
    const groups = [];               // unique frames, newest occurrence first
    const byKey = new Map();
    const callPos = new Map();       // call -> newest log entry with a position
    const callCount = new Map();     // call -> frames in this window
    for (const e of log) {
        callCount.set(e.call, (callCount.get(e.call) || 0) + 1);
        if (e.lat !== null && e.lat !== undefined && !callPos.has(e.call))
            callPos.set(e.call, e);            // iteration is newest -> oldest
        const k = e.call + '\u0001' + e.dst + '\u0001' + (e.via || '') + '\u0001' + (e.text || '');
        let g = byKey.get(k);
        if (!g) {
            g = { newest: e, oldest: e, count: 0, pos: null };
            byKey.set(k, g);
            groups.push(g);
        }
        g.count++;
        g.oldest = e;
        if (g.pos === null && e.lat !== null && e.lat !== undefined) g.pos = e;
    }
    document.getElementById('loadinfo').textContent =
        ' · ' + log.length + ' frame' + (log.length === 1 ? '' : 's')
        + ' → ' + groups.length + ' unique';
    const rows = document.getElementById('rows');
    if (!groups.length) {
        rows.innerHTML = '<tr><td colspan="7" class="muted">No APRS frames decoded yet — the log fills as stations beacon.</td></tr>';
        updateMap(callPos, callCount);
        return;
    }
    let html = '';
    for (const g of groups) {
        let pos = null;
        if (g.pos) {
            pos = '<a href="https://www.google.com/maps?q=' + g.pos.lat + ',' + g.pos.lon
                + '" target="_blank" rel="noopener">' + g.pos.lat.toFixed(4) + ', ' + g.pos.lon.toFixed(4) + '</a>'
                + ' ' + esc(g.pos.sym || '');
        }
        const cnt = g.count > 1
            ? '<td class="cnt" title="first ' + fmtTime(g.oldest.ts) + ' · newest ' + fmtTime(g.newest.ts) + '">&times;' + g.count + '</td>'
            : '<td></td>';
        html += '<tr>'
            + cell(fmtTime(g.newest.ts), 't')
            + cnt
            + cell(esc(g.newest.call), 'call')
            + cell(esc(g.newest.dst), 'via')
            + cell(esc(g.newest.via || ''), 'via')
            + cell(pos, 'pos')
            + cell(esc(g.newest.text || ''), 'txt')
            + '</tr>';
    }
    rows.innerHTML = html;
    updateMap(callPos, callCount);
}

// ---------- station map (Leaflet + OpenStreetMap, like the AIS page) ----
// One marker per callsign at its newest decoded position; the popup
// shows the frame count of the loaded window and the last frame text.
let map = null, markers = {}, fitted = false;
function initMap() {
    if (typeof L === 'undefined') {
        const d = document.getElementById('map');
        d.innerHTML = '<div class="muted">Map library (Leaflet CDN) unreachable — the log below still works.</div>';
        d.style.height = 'auto';
        return;
    }
    map = L.map('map').setView([48.99, 12.09], 13);   // Regensburg, refit to stations on data
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    }).addTo(map);
    document.getElementById('fitbtn').onclick = () => { fitted = false; fitStations(); };
}
function stationIcon(call) {
    return L.divIcon({
        className: '',
        iconSize: [28, 28],
        iconAnchor: [14, 14],
        popupAnchor: [0, -12],
        html: '<div class="stnIcon"><div class="dot"></div>'
            + '<div class="lbl">' + esc(call) + '</div></div>'
    });
}
function stationPopup(e, count) {
    return '<b>' + esc(e.call) + '</b> · ' + count + ' frame' + (count === 1 ? '' : 's') + '<br>'
        + 'last: ' + fmtTime(e.ts) + ' · ' + esc(e.dst) + (e.via ? ' via ' + esc(e.via) : '') + '<br>'
        + '<a href="https://www.google.com/maps?q=' + e.lat + ',' + e.lon + '" target="_blank" rel="noopener">'
        + e.lat.toFixed(4) + ', ' + e.lon.toFixed(4) + '</a>' + (e.sym ? ' · ' + esc(e.sym) : '') + '<br>'
        + esc(e.text || '');
}
function fitStations() {
    if (!map || !fitted) return;
    const ms = Object.values(markers);
    if (!ms.length) return;
    map.fitBounds(L.latLngBounds(ms.map(m => m.getLatLng())).pad(0.25));
}
function updateMap(callPos, callCount) {
    if (!map) return;
    const seen = new Set();
    let onMap = 0;
    for (const [call, e] of callPos) {
        seen.add(call);
        onMap++;
        let m = markers[call];
        if (!m) {
            m = markers[call] = L.marker([e.lat, e.lon], { title: call }).addTo(map);
            m.bindPopup(stationPopup(e, callCount.get(call) || 1));
        } else {
            m.setLatLng([e.lat, e.lon]);
            m.setPopupContent(stationPopup(e, callCount.get(call) || 1));
        }
    }
    for (const k of Object.keys(markers)) {
        if (!seen.has(k)) { map.removeLayer(markers[k]); delete markers[k]; }
    }
    const noPos = callCount.size - onMap;
    if (onMap && !fitted) { fitted = true; fitStations(); }
    document.getElementById('mapstats').textContent =
        onMap + ' station' + (onMap === 1 ? '' : 's') + ' with position on the map'
        + (noPos > 0 ? ' · ' + noPos + ' without position yet' : '');
}
initMap();

// ---------- full history: station registry (every station ever -------
// received, like the AIS ship registry) with its own map, fed by
// /aprs_stations.json and the WS 'aprs_stations' topic push
const regOpen = new Set();       // callsigns whose packet history is expanded
let hmap = null, hmarkers = {}, hfitted = false;
function initHistoryMap() {
    if (typeof L === 'undefined') {
        const d = document.getElementById('hmap');
        d.innerHTML = '<div class="muted">Map library (Leaflet CDN) unreachable — the registry below still works.</div>';
        d.style.height = 'auto';
        return;
    }
    hmap = L.map('hmap').setView([48.99, 12.09], 13);   // Regensburg, refit to stations on data
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    }).addTo(hmap);
    document.getElementById('hfitbtn').onclick = () => { hfitted = false; fitAllStations(); };
}
function fmtDateTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour12: false });
}
function regPosLink(s) {
    if (s.lat === null || s.lat === undefined) return null;
    return '<a href="https://www.google.com/maps?q=' + s.lat + ',' + s.lon
        + '" target="_blank" rel="noopener">' + s.lat.toFixed(4) + ', ' + s.lon.toFixed(4) + '</a>';
}
function regPopup(s) {
    const link = regPosLink(s);
    return '<b>' + esc(s.call) + '</b> · ' + s.msgs + ' frame' + (s.msgs === 1 ? '' : 's') + ' ever<br>'
        + 'first seen ' + fmtDateTime(s.first_seen) + '<br>'
        + 'last seen ' + fmtDateTime(s.last_seen) + ' · ' + esc(s.dst) + (s.via ? ' via ' + esc(s.via) : '') + '<br>'
        + (link ? link + (s.sym ? ' · ' + esc(s.sym) : '') : '—') + '<br>'
        + esc(s.text || '');
}
function fitAllStations() {
    if (!hmap || !hfitted) return;
    const ms = Object.values(hmarkers);
    if (!ms.length) return;
    hmap.fitBounds(L.latLngBounds(ms.map(m => m.getLatLng())).pad(0.25));
}
// Persistent station registry render — shared by the /aprs_stations.json
// fallback poll and the WS 'aprs_stations' topic push
function applyRegistry(sts) {
    document.getElementById('regcount').textContent = sts.length;
    if (hmap) {
        const seen = new Set();
        let onMap = 0, noPos = 0;
        for (const s of sts) {
            if (s.lat === null || s.lat === undefined) { noPos++; continue; }
            seen.add(s.call);
            onMap++;
            let m = hmarkers[s.call];
            if (!m) {
                m = hmarkers[s.call] = L.marker([s.lat, s.lon], { title: s.call }).addTo(hmap);
                m.bindPopup(regPopup(s));
            } else {
                m.setLatLng([s.lat, s.lon]);
                m.setPopupContent(regPopup(s));
            }
        }
        for (const k of Object.keys(hmarkers)) {
            if (!seen.has(k)) { hmap.removeLayer(hmarkers[k]); delete hmarkers[k]; }
        }
        if (onMap && !hfitted) { hfitted = true; fitAllStations(); }
        document.getElementById('hmapstats').textContent =
            onMap + ' station' + (onMap === 1 ? '' : 's') + ' with position on the map'
            + (noPos > 0 ? ' · ' + noPos + ' without position ever' : '');
    }
    const rows = document.getElementById('regrows');
    if (!sts.length) {
        rows.innerHTML = '<tr><td colspan="6" class="muted">No stations in the registry yet — it fills as stations beacon.</td></tr>';
        return;
    }
    let html = '';
    for (const s of sts) {
        const recent = s.recent || [];
        let msgs = '';
        for (const e of recent.slice().reverse()) {   // newest first
            msgs += '<tr><td class="t">' + fmtTime(e.ts) + '</td>'
                + '<td class="txt">' + esc(e.text || '') + '</td></tr>';
        }
        html += '<tr>'
            + cell(esc(s.call), 'call')
            + cell(regPosLink(s), 'pos')
            + cell(esc(s.msgs))
            + cell(esc(fmtDateTime(s.first_seen)), 't')
            + cell(esc(fmtDateTime(s.last_seen)), 't')
            + '<td><details' + (regOpen.has(s.call) ? ' open' : '')
            + ' data-call="' + esc(s.call) + '" ontoggle="regToggle(this.dataset.call, this.open)">'
            + '<summary>' + recent.length + ' packets (last 10)</summary>'
            + '<table class="regmsgs"><tbody>' + msgs + '</tbody></table>'
            + '</details></td></tr>';
    }
    rows.innerHTML = html;
    // telemetry section: stations with a decoded T# beacon
    const telm = sts.filter(s => s.telemetry);
    document.getElementById('telmwrap').style.display = telm.length ? '' : 'none';
    if (!telm.length) return;
    document.getElementById('telmcount').textContent =
        '— ' + telm.length + ' station' + (telm.length === 1 ? '' : 's') + ' with telemetry';
    let th = '';
    for (const s of telm) {
        const t = s.telemetry;
        const chans = (t.channels || []).map(c =>
            esc(c.name) + ' <b>' + (c.value === null || c.value === undefined ? '—' : esc(c.value))
            + '</b>' + (c.unit ? ' ' + esc(c.unit) : '')).join(' · ');
        const bits = (t.bits || []).map(b =>
            esc(b.name) + '=' + (b.value === null || b.value === undefined ? '—' : esc(b.value)))
            .join(' ');
        th += '<tr>'
            + cell(esc(s.call), 'call')
            + cell(fmtTime(t.ts), 't')
            + cell(t.seq === null || t.seq === undefined ? null : esc(t.seq))
            + cell(chans, 'txt')
            + cell(bits ? bits + (t.label ? ' · ' + esc(t.label) : '') : null, 'txt')
            + '</tr>';
    }
    document.getElementById('telmrows').innerHTML = th;
}
function regToggle(call, open) {
    if (open) regOpen.add(call); else regOpen.delete(call);
}
async function loadRegistry() {
    try {
        applyRegistry(await (await fetch('/aprs_stations.json')).json());
    } catch (err) {
        console.error('Station registry load failed:', err);
    }
}
initHistoryMap();
setInterval(() => { if (!aprsWsLive()) loadRegistry(); }, 30000);
loadRegistry();

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
    try { aprsSocket.send('sub=aprs,aprs_stations,aprslog=' + (document.getElementById('count').value || 200)); } catch (e) {}
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
                else if (topic === 'aprs_stations') applyRegistry(data);
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
