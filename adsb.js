// ADS-B page: live aircraft map + registry + frame log, mirroring ais.js.
// Subscribes to the 'adsb', 'adsblog' and 'adsb_aircrafts' WS topics; the
// HTTP polling below is the fallback only while the socket is down.

const TC_NAMES = {
    1: 'ID', 2: 'ID', 3: 'ID', 4: 'vel HS', 5: 'vel HS', 6: 'ground',
    7: 'ground', 8: 'ground', 9: 'vel GS', 10: 'vel GS', 11: 'pos',
    12: 'pos', 13: 'pos+vel', 14: 'pos', 15: 'pos', 16: 'surface',
    17: 'surface', 18: 'surface', 19: 'vel air', 20: 'vel air',
    21: 'vel air', 22: 'vel air', 23: 'test', 24: 'reserved',
    25: 'reserved', 26: 'reserved', 27: 'reserved', 28: ' aircraft state',
    29: 'met', 30: 'met2', 31: 'status'
};

function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }

function fmtTime(ts) {
    return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}

function fmtDateTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour12: false });
}

function cell(v, cls, dash) { return '<td' + (cls ? ' class="' + cls + '"' : '') + '>' + (v === null || v === undefined ? '<span class="na">' + (dash || '—') + '</span>' : v) + '</td>'; }

// ---------- persistent aircraft registry (every aircraft ever received) ----------
// One row per plane with everything merged (latest callsign, altitude,
// speed, track, vertical rate and position of that aircraft) plus the
// message history — the track-mode checkbox column draws the last 24 h
// path on the map above, like the AIS page's 'Show path on map'.
const regOpen = new Set();       // ICAOs whose message history is expanded
let trackMode = false;           // 'show path on map' multi-select active
const selectedAircraft = new Set(); // ICAOs whose path is drawn
const trackLayers = {};          // ICAO -> Leaflet polyline
const TRACK_COLORS = ['#ff6b6b', '#ffd93d', '#6bcb77', '#4d96ff', '#c77dff',
                      '#ff9770', '#70d6ff', '#b5e48c', '#f7a072', '#9bd1e5'];
const REG_HEAD_PLAIN = '<th>callsign</th><th>ICAO</th><th>alt</th>'
    + '<th>speed</th><th>track</th><th>v/s</th><th>position</th>'
    + '<th>first seen</th><th>last seen</th><th>total msgs</th>'
    + '<th>message history</th>';
const REG_HEAD_TRACK = '<th>path</th>' + REG_HEAD_PLAIN;

function applyRegistry(aircraft) {
    document.getElementById('regcount').textContent = aircraft.length;
    const rows = document.getElementById('regrows');
    if (!aircraft.length) {
        rows.innerHTML = '<tr><td colspan="' + (trackMode ? 12 : 11)
            + '" class="muted">No aircraft in the registry yet — it fills as aircraft come into range.</td></tr>';
        return;
    }
    let html = '';
    for (const a of aircraft) {
        const recent = a.recent || [];
        let msgs = '';
        for (const e of recent.slice().reverse()) {   // newest first
            const t = (e.ts !== undefined && e.ts !== null) ? fmtTime(e.ts) : e.time;
            const pos = (e.lat !== null && e.lat !== undefined)
                ? e.lat.toFixed(4) + ', ' + e.lon.toFixed(4) : '—';
            msgs += '<tr><td class="t">' + esc(t) + '</td>'
                + '<td class="df">' + esc(e.df) + '</td>'
                + '<td class="type">' + esc(TC_NAMES[e.tc] || (e.tc ? 'tc ' + e.tc : '')) + '</td>'
                + '<td class="cs">' + esc(e.callsign || '') + '</td>'
                + '<td>' + esc(pos) + '</td>'
                + '<td class="hex">' + esc(e.hex || '') + '</td></tr>';
        }
        const pos = (a.lat !== null && a.lat !== undefined)
            ? '<a href="https://www.google.com/maps?q=' + a.lat + ',' + a.lon
              + '" target="_blank" rel="noopener">' + a.lat.toFixed(4) + ', ' + a.lon.toFixed(4) + '</a>'
            : null;
        const alt = a.alt_ft !== null && a.alt_ft !== undefined ? a.alt_ft + ' ft' : null;
        const spd = a.speed_kt !== null && a.speed_kt !== undefined ? a.speed_kt + ' kt' : null;
        const trk = a.track_deg !== null && a.track_deg !== undefined ? a.track_deg + '\u00b0' : null;
        const vs = a.vs_fpm !== null && a.vs_fpm !== undefined ? a.vs_fpm + ' fpm' : null;
        html += '<tr>'
            + (trackMode
                ? '<td><input type="checkbox"'
                    + (selectedAircraft.has(a.icao) ? ' checked' : '')
                    + ' onchange="trackToggle(\'' + esc(a.icao) + '\', this.checked)"></td>'
                : '')
            + '<td class="cs">' + esc(a.callsign || '') + '</td>'
            + '<td class="icao">' + esc(a.icao) + '</td>'
            + cell(alt)
            + cell(spd)
            + cell(trk)
            + cell(vs)
            + cell(pos, 'pos')
            + '<td class="t">' + (a.first_seen ? fmtDateTime(a.first_seen) : '—') + '</td>'
            + '<td class="t">' + (a.last_seen ? fmtDateTime(a.last_seen) : '—') + '</td>'
            + '<td>' + (a.msgs || 0) + '</td>'
            + '<td><details' + (regOpen.has(a.icao) ? ' open' : '')
            + ' ontoggle="regToggle(\'' + esc(a.icao) + '\', this.open)">'
            + '<summary>' + recent.length + ' messages (last 10)</summary>'
            + '<table class="regmsgs"><thead><tr><th>time</th><th>DF</th>'
            + '<th>type</th><th>callsign</th><th>position</th><th>raw frame</th>'
            + '</tr></thead><tbody>' + msgs + '</tbody></table>'
            + '</details></td></tr>';
    }
    rows.innerHTML = html;
}

async function loadRegistry() {
    try {
        applyRegistry(await (await fetch('/adsb_aircrafts.json')).json());
    } catch (err) {
        console.error('Aircraft registry load failed:', err);
    }
}

function regToggle(icao, open) { if (open) regOpen.add(icao); else regOpen.delete(icao); }

// ---------- path history on the map (like the AIS ship tracks) ----------
function toggleTrackMode() {
    trackMode = !trackMode;
    if (!trackMode) {
        for (const m of Array.from(selectedAircraft)) removeTrack(m);
        selectedAircraft.clear();
    }
    document.getElementById('trackmode-btn').innerHTML =
        trackMode ? '&#10060; Exit path mode' : '&#128506; Show path on map';
    document.getElementById('reghead').innerHTML =
        trackMode ? REG_HEAD_TRACK : REG_HEAD_PLAIN;
    updateTrackHint();
    loadRegistry();
}

function updateTrackHint() {
    const el = document.getElementById('trackmode-hint');
    if (!trackMode) { el.textContent = ''; return; }
    el.textContent = selectedAircraft.size
        ? selectedAircraft.size + ' path(s) drawn (last 24 h) — uncheck to remove'
        : 'select aircraft below to draw their last 24 h path';
}

async function trackToggle(icao, on) {
    if (on) {
        selectedAircraft.add(icao);
        await drawTrack(icao);
    } else {
        selectedAircraft.delete(icao);
        removeTrack(icao);
    }
    updateTrackHint();
}

function removeTrack(icao) {
    if (trackLayers[icao] && map) map.removeLayer(trackLayers[icao]);
    delete trackLayers[icao];
}

async function drawTrack(icao) {
    if (!map) { alert('The map library is unavailable — paths cannot be drawn.'); return; }
    try {
        const res = await fetch('/adsb_tracks.json?icao='
                                + encodeURIComponent(icao) + '&hours=24');
        const tracks = await res.json();
        const pts = tracks[icao] || [];
        removeTrack(icao);
        if (!pts.length) return;   // no positions in the window: nothing to draw
        const color = TRACK_COLORS[selectedAircraft.size % TRACK_COLORS.length];
        const line = L.polyline(pts.map(p => [p.lat, p.lon]),
                                { color: color, weight: 3, opacity: 0.85 }).addTo(map);
        const t0 = new Date(pts[0].ts * 1000).toLocaleTimeString([], { hour12: false });
        const t1 = new Date(pts[pts.length - 1].ts * 1000).toLocaleTimeString([], { hour12: false });
        const alt0 = pts[0].alt_ft, alt1 = pts[pts.length - 1].alt_ft;
        line.bindPopup('<b>' + esc(icao) + '</b><br>'
            + pts.length + ' positions (24 h)<br>' + t0 + ' — ' + t1
            + (alt1 ? '<br>' + (alt0 || '—') + ' → ' + alt1 + ' ft' : ''));
        trackLayers[icao] = line;
        map.fitBounds(line.getBounds().pad(0.2));
    } catch (err) {
        console.error('Track load failed for ' + icao, err);
    }
}

setInterval(() => { if (!adsbWsLive()) loadRegistry(); }, 30000);
loadRegistry();
document.getElementById('trackmode-btn').addEventListener('click', toggleTrackMode);

// ---------- live aircraft map (Leaflet + OpenStreetMap tiles) ----------
// Aircraft are placed by their latest position and rotated by their
// track; markers fade when silent for 10+ minutes.

let map = null, markers = {}, fitted = false;

function initMap() {
    if (typeof L === 'undefined') {
        const d = document.getElementById('map');
        d.innerHTML = '<div class="muted">Map library (Leaflet CDN) unreachable — the log below still works.</div>';
        d.style.height = 'auto';
        return;
    }
    map = L.map('map').setView([48.99, 12.09], 13);
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    }).addTo(map);
    document.getElementById('fitbtn').onclick = () => { fitted = false; fitAircraft(); };
}

function planePopup(a, age) {
    const pos = (a.lat !== undefined && a.lat !== null)
        ? a.lat.toFixed(4) + ', ' + a.lon.toFixed(4) : '—';
    const seenAt = new Date(a.last_seen * 1000).toLocaleTimeString();
    return '<b>' + esc(a.callsign || a.icao) + '</b> · ' + esc(a.icao) + '<br>'
        + (a.alt_ft !== undefined && a.alt_ft !== null ? a.alt_ft + ' ft' : '—')
        + ' · ' + (a.speed_kt !== undefined && a.speed_kt !== null ? a.speed_kt + ' kt' : '—')
        + ' · ' + (a.track_deg !== undefined && a.track_deg !== null ? a.track_deg + '°' : '—')
        + '<br>' + pos + ' · last seen ' + seenAt + ' (' + age + ' s ago)';
}

function planeIcon(a) {
    const rot = (a.track_deg !== undefined && a.track_deg !== null) ? a.track_deg : 0;
    const color = a.onground ? '#f39c12' : '#4da6ff';
    const shape = '<svg width="22" height="22" viewBox="0 0 24 24">'
        + '<path d="M12 1 L14 9 L21 12 L14 14.5 L14 19 L12 23 L10 19 L10 14.5 L3 12 L10 9 Z"'
        + ' fill="' + color + '" stroke="#10131f" stroke-width="0.8"/></svg>';
    const stale = (Date.now() / 1000 - a.last_seen) > 600;
    return L.divIcon({
        className: '',
        iconSize: [28, 28],
        iconAnchor: [14, 14],
        popupAnchor: [0, -12],
        html: '<div class="planeIcon' + (stale ? ' stale' : '') + '">'
            + '<div class="rot" style="transform: rotate(' + rot + 'deg)">' + shape + '</div>'
            + (a.callsign ? '<div class="lbl">' + esc(a.callsign) + '</div>' : '')
            + '</div>'
    });
}

function fitAircraft() {
    if (!map || !fitted) return;
    const ms = Object.values(markers);
    if (!ms.length) return;
    map.fitBounds(L.latLngBounds(ms.map(m => m.getLatLng())).pad(0.25));
}

function updateMap(aircraft) {
    if (!map) return;
    const now = Date.now() / 1000;
    const seen = new Set();
    let onMap = 0, noPos = 0, staleCount = 0;
    for (const a of aircraft) {
        if (a.lat === undefined || a.lat === null || a.lon === undefined || a.lon === null) { noPos++; continue; }
        seen.add(String(a.icao));
        onMap++;
        const age = Math.max(0, Math.round(now - a.last_seen));
        if (age > 600) staleCount++;
        let m = markers[a.icao];
        if (!m) {
            m = markers[a.icao] = L.marker([a.lat, a.lon], { title: a.callsign || a.icao }).addTo(map);
            m.bindPopup(planePopup(a, age));
        } else {
            m.setLatLng([a.lat, a.lon]);
        }
        m.setIcon(planeIcon(a));
        m.setPopupContent(planePopup(a, age));
    }
    for (const k of Object.keys(markers)) {
        if (!seen.has(k)) { map.removeLayer(markers[k]); delete markers[k]; }
    }
    if (onMap && !fitted) fitted = true, fitAircraft();
    document.getElementById('mapstats').textContent =
        onMap + ' aircraft with position on the map'
        + (staleCount ? ' · ' + staleCount + ' faded (not heard for 10+ min)' : '')
        + (noPos ? ' · ' + noPos + ' without position yet' : '');
}

initMap();

// ---------- status header + map (WS 'adsb' / /adsb.json) ----------
function applyAdsbStatus(s) {
    const st = s.stats || {};
    let frames = 0;
    const statTxt = Object.keys(st).map(k => k + ' ' + st[k]).join(' · ');
    for (const k in st) if (/frames$/.test(k)) frames += st[k];
    document.getElementById('stats').textContent =
        (s.enabled ? 'Receiver live on dongle ' + s.dongle
                   : 'ADS-B disabled (no dongle in the ADS-B role) — showing the persisted log')
        + ' · ' + (s.freq_mhz || 1090.0) + ' MHz'
        + (statTxt ? ' · ' + statTxt : '')
        + ' · aircraft tracked: ' + (s.aircraft ? s.aircraft.length : 0);
    updateMap(s.aircraft || []);
}

// ---------- frame log tail (WS 'adsblog' / /adsblog.json) ----------
function applyAdsbLog(log) {
    document.getElementById('loadinfo').textContent = ' · showing ' + log.length + ' log lines';
    const rows = document.getElementById('rows');
    if (!log.length) {
        rows.innerHTML = '<tr><td colspan="11" class="muted">No ADS-B frames decoded yet — the log fills as aircraft come into range.</td></tr>';
        return;
    }
    let html = '';
    for (const e of log) {
        const t = (e.ts !== undefined && e.ts !== null) ? fmtTime(e.ts) : e.time;
        const pos = (e.lat !== null && e.lat !== undefined)
            ? '<a href="https://www.google.com/maps?q=' + e.lat + ',' + e.lon + '" target="_blank" rel="noopener">' + e.lat.toFixed(4) + ', ' + e.lon.toFixed(4) + '</a>'
            : null;
        const alt = e.alt_ft !== null && e.alt_ft !== undefined ? e.alt_ft + ' ft' : null;
        const spd = e.speed_kt !== null && e.speed_kt !== undefined ? e.speed_kt + ' kt' : null;
        const trk = e.track_deg !== null && e.track_deg !== undefined ? e.track_deg + '\u00b0' : null;
        const vs = e.vs_fpm !== null && e.vs_fpm !== undefined ? e.vs_fpm + ' fpm' : null;
        html += '<tr>'
            + cell(esc(t), 't')
            + cell(esc(e.df), 'df')
            + cell(esc(TC_NAMES[e.tc] || (e.tc !== undefined && e.tc !== null ? 'tc ' + e.tc : '')), 'type')
            + cell(esc(e.icao), 'icao')
            + cell(esc(e.callsign || ''), 'cs')
            + cell(pos, 'pos')
            + cell(alt)
            + cell(spd)
            + cell(trk)
            + cell(vs)
            + cell(esc(e.hex || ''), 'hex')
            + '</tr>';
    }
    rows.innerHTML = html;
}

async function load() {
    try {
        const n = document.getElementById('count').value;
        applyAdsbLog(await (await fetch('/adsblog.json?count=' + n)).json());
        applyAdsbStatus(await (await fetch('/adsb.json')).json());
    } catch (err) {
        console.error('ADS-B log load failed:', err);
    }
}

document.getElementById('count').addEventListener('change', () => {
    if (adsbWsLive()) sendAdsbSub(); else load();
});

let timer = null;
function setTimer() {
    if (timer) clearInterval(timer);
    if (document.getElementById('autorefresh').checked)
        timer = setInterval(() => { if (!adsbWsLive()) load(); }, 10000);
}
document.getElementById('autorefresh').addEventListener('change', setTimer);
load();
setTimer();

// ---------- WebSocket live updates (JSON topic push over /waterfall.ws) ----------
let adsbSocket = null, adsbWsBackoff = 2000;

function adsbWsLive() { return adsbSocket && adsbSocket.readyState === 1; }

function sendAdsbSub() {
    try { adsbSocket.send('sub=adsb,adsb_aircrafts,adsblog=' + (document.getElementById('count').value || 200)); } catch (e) {}
}

function connectAdsbWs() {
    try {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        const ws = new WebSocket(proto + location.host + '/waterfall.ws');
        adsbSocket = ws;
        ws.binaryType = 'arraybuffer';
        ws.onopen = () => { adsbWsBackoff = 2000; sendAdsbSub(); };
        ws.onmessage = ev => {
            if (typeof ev.data === 'string') return;
            const u = new Uint8Array(ev.data);
            if (u.length < 4 || u[0] !== 2) return;
            const nlen = (u[1] << 8) | u[2];
            if (u.length < 3 + nlen) return;
            const topic = new TextDecoder().decode(u.subarray(3, 3 + nlen));
            try {
                const data = JSON.parse(new TextDecoder().decode(u.subarray(3 + nlen)));
                if (topic === 'adsb') applyAdsbStatus(data);
                else if (topic === 'adsblog') applyAdsbLog(data);
                else if (topic === 'adsb_aircrafts') applyRegistry(data);
            } catch (e) { console.error('WS topic ' + topic + ' failed:', e); }
        };
        ws.onclose = () => {
            adsbSocket = null;
            setTimeout(connectAdsbWs, adsbWsBackoff);
            adsbWsBackoff = Math.min(60000, adsbWsBackoff * 2);
        };
        ws.onerror = () => { try { ws.close(); } catch (e) {} };
    } catch (e) {
        adsbSocket = null;
        setTimeout(connectAdsbWs, adsbWsBackoff);
        adsbWsBackoff = Math.min(60000, adsbWsBackoff * 2);
    }
}
connectAdsbWs();
