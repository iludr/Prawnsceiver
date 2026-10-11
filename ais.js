const MSG_NAMES = {
    1: 'position (A)', 2: 'position (A)', 3: 'position (A)',
    4: 'base station', 5: 'static/voyage', 9: 'SAR aircraft',
    11: 'UTC response', 18: 'position (B)', 19: 'position ext (B)',
    21: 'aid to nav', 24: 'static (B)', 27: 'long range'
};
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
// The server logs in ITS timezone (the container runs UTC) — every
// entry also carries unix ts, so the list shows browser-local time
function fmtTime(ts) {
    return new Date(ts * 1000).toLocaleTimeString([], { hour12: false });
}
function cell(v, cls, dash) { return '<td' + (cls ? ' class="' + cls + '"' : '') + '>' + (v === null || v === undefined ? '<span class="na">' + (dash || '—') + '</span>' : v) + '</td>'; }

// ---------- persistent ship registry (all ships ever received) ----------
const regOpen = new Set();       // MMSIs whose message history is expanded
let registryShips = {};          // MMSI -> name, for track popups
let trackMode = false;           // 'show path on map' multi-select active
const selectedShips = new Set(); // MMSIs whose path is drawn
const trackLayers = {};          // MMSI -> Leaflet polyline
const TRACK_COLORS = ['#ff6b6b', '#ffd93d', '#6bcb77', '#4d96ff', '#c77dff',
                      '#ff9770', '#70d6ff', '#b5e48c', '#f7a072', '#9bd1e5'];
const REG_HEAD_PLAIN = '<th>name</th><th>MMSI</th><th>class</th><th>first seen</th>'
    + '<th>last seen</th><th>total msgs</th><th>message history</th>';
const REG_HEAD_TRACK = '<th>path</th>' + REG_HEAD_PLAIN;
function fmtDateTime(ts) {
    const d = new Date(ts * 1000);
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour12: false });
}
// Persistent ship registry render — shared by the /ais_ships.json
// fallback poll and the WS 'ais_ships' topic push
function applyRegistry(ships) {
    document.getElementById('regcount').textContent = ships.length;
        registryShips = {};
        for (const s of ships) registryShips[s.mmsi] = s.name || ('MMSI ' + s.mmsi);
        const rows = document.getElementById('regrows');
        if (!ships.length) {
            rows.innerHTML = '<tr><td colspan="' + (trackMode ? 8 : 7)
                + '" class="muted">No ships in the registry yet — it fills as ships come into range.</td></tr>';
            return;
        }
        let html = '';
        for (const s of ships) {
            const recent = s.recent || [];
            let msgs = '';
            for (const e of recent.slice().reverse()) {   // newest first
                const t = (e.ts !== undefined && e.ts !== null) ? fmtTime(e.ts) : e.time;
                msgs += '<tr><td class="t">' + esc(t) + '</td>'
                    + '<td class="ch">' + esc(e.ch) + '</td>'
                    + '<td class="type">' + esc(e.msg + ' ' + (MSG_NAMES[e.msg] || '?')) + '</td>'
                    + '<td class="nmea">' + esc(e.nmea || '') + '</td></tr>';
            }
            html += '<tr class="cls-' + esc(s.cls || '') + '">'
                + (trackMode
                    ? '<td><input type="checkbox"'
                        + (selectedShips.has(s.mmsi) ? ' checked' : '')
                        + ' onchange="trackToggle(' + esc(s.mmsi) + ', this.checked)"></td>'
                    : '')
                + '<td class="name">' + esc(s.name || '(unnamed)') + '</td>'
                + '<td class="mmsi">' + esc(s.mmsi) + '</td>'
                + '<td class="type">' + esc(s.cls || '') + '</td>'
                + '<td class="t">' + (s.first_seen ? fmtDateTime(s.first_seen) : '—') + '</td>'
                + '<td class="t">' + (s.last_seen ? fmtDateTime(s.last_seen) : '—') + '</td>'
                + '<td>' + (s.msgs || 0) + '</td>'
                + '<td><details' + (regOpen.has(s.mmsi) ? ' open' : '')
                + ' ontoggle="regToggle(' + esc(s.mmsi) + ', this.open)">'
                + '<summary>' + recent.length + ' messages (last 10)</summary>'
                + '<table class="regmsgs"><tbody>' + msgs + '</tbody></table>'
                + '</details></td></tr>';
        }
        rows.innerHTML = html;
}

async function loadRegistry() {
    try {
        applyRegistry(await (await fetch('/ais_ships.json')).json());
    } catch (err) {
        console.error('Ship registry load failed:', err);
    }
}
function toggleTrackMode() {
    trackMode = !trackMode;
    if (!trackMode) {
        for (const m of Array.from(selectedShips)) removeTrack(m);
        selectedShips.clear();
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
    el.textContent = selectedShips.size
        ? selectedShips.size + ' path(s) drawn (last 24 h) — uncheck to remove'
        : 'select ships below to draw their last 24 h path';
}
async function trackToggle(mmsi, on) {
    if (on) {
        selectedShips.add(mmsi);
        await drawTrack(mmsi);
    } else {
        selectedShips.delete(mmsi);
        removeTrack(mmsi);
    }
    updateTrackHint();
}
function removeTrack(mmsi) {
    if (trackLayers[mmsi] && map) map.removeLayer(trackLayers[mmsi]);
    delete trackLayers[mmsi];
}
async function drawTrack(mmsi) {
    if (!map) { alert('The map library is unavailable — paths cannot be drawn.'); return; }
    try {
        const res = await fetch('/ship_tracks.json?mmsi='
                                 + encodeURIComponent(mmsi) + '&hours=24');
        const tracks = await res.json();
        const pts = tracks[mmsi] || [];
        removeTrack(mmsi);
        if (!pts.length) return;   // no positions in the window: nothing to draw
        const color = TRACK_COLORS[selectedShips.size % TRACK_COLORS.length];
        const line = L.polyline(pts.map(p => [p.lat, p.lon]),
                                { color: color, weight: 3, opacity: 0.85 }).addTo(map);
        const t0 = new Date(pts[0].ts * 1000).toLocaleTimeString([], { hour12: false });
        const t1 = new Date(pts[pts.length - 1].ts * 1000).toLocaleTimeString([], { hour12: false });
        line.bindPopup('<b>' + esc(registryShips[mmsi] || mmsi) + '</b><br>'
            + pts.length + ' positions (24 h)<br>' + t0 + ' — ' + t1);
        trackLayers[mmsi] = line;
        map.fitBounds(line.getBounds().pad(0.2));
    } catch (err) {
        console.error('Track load failed for ' + mmsi, err);
    }
}
function regToggle(mmsi, open) {
    if (open) regOpen.add(mmsi); else regOpen.delete(mmsi);
}
setInterval(() => { if (!aisWsLive()) loadRegistry(); }, 30000);
loadRegistry();

// ---------- live ship map (Leaflet + OpenStreetMap tiles) ----------
// Ships are placed by their latest position report and rotated by their
// true heading (fallback: course over ground; 511 = n/a per AIS spec).
// Class colors match the table: A green, B blue, everything else orange;
// aids to navigation (ATON) get a diamond instead of a ship arrow.
let map = null, markers = {}, fitted = false;
function initMap() {
    if (typeof L === 'undefined') {
        const d = document.getElementById('map');
        d.innerHTML = '<div class="muted">Map library (Leaflet CDN) unreachable — the log below still works.</div>';
        d.style.height = 'auto';
        return;
    }
    map = L.map('map').setView([48.99, 12.09], 13);   // Danube at Regensburg, refit to ships on data
    L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
        maxZoom: 19,
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
    }).addTo(map);
    document.getElementById('fitbtn').onclick = () => { fitted = false; fitShips(); };
}
function shipPopup(s, age) {
    const pos = (s.lat !== undefined && s.lat !== null)
        ? s.lat.toFixed(4) + ', ' + s.lon.toFixed(4) : '—';
    const seenAt = new Date(s.last_seen * 1000).toLocaleTimeString();
    return '<b>' + esc(s.name || '(unnamed)') + '</b> · class ' + esc(s.cls || '?') + '<br>'
        + 'MMSI ' + esc(s.mmsi) + (s.callsign ? ' · ' + esc(s.callsign) : '') + '<br>'
        + 'sog ' + (s.sog !== undefined && s.sog !== null ? s.sog + ' kn' : '—')
        + ' · cog ' + (s.cog !== undefined && s.cog !== null ? s.cog + '\u00b0' : '—')
        + ' · hdg ' + (s.heading !== undefined && s.heading !== null ? s.heading + '\u00b0' : '—') + '<br>'
        + pos + ' · last seen ' + seenAt + ' (' + age + ' s ago)';
}
function shipIcon(s) {
    const rot = (s.heading !== undefined && s.heading !== null && s.heading !== 511)
        ? s.heading
        : (s.cog !== undefined && s.cog !== null ? s.cog : 0);
    const color = s.cls === 'A' ? '#53d769' : (s.cls === 'B' ? '#4da6ff' : '#f39c12');
    const shape = s.cls === 'ATON'
        ? '<svg width="18" height="18" viewBox="0 0 24 24"><path d="M12 2 L22 12 L12 22 L2 12 Z" fill="' + color + '" stroke="#10131f" stroke-width="1.4"/></svg>'
        : '<svg width="22" height="22" viewBox="0 0 24 24"><path d="M12 1 L17.5 11 L14.2 11 L14.2 17 L9.8 17 L9.8 11 L6.5 11 Z" fill="' + color + '" stroke="#10131f" stroke-width="1"/></svg>';
    const stale = (Date.now() / 1000 - s.last_seen) > 600;   // silent for 10+ min
    return L.divIcon({
        className: '',
        iconSize: [28, 28],
        iconAnchor: [14, 14],
        popupAnchor: [0, -12],
        html: '<div class="shipIcon' + (stale ? ' stale' : '') + '">'
            + '<div class="rot" style="transform: rotate(' + rot + 'deg)">' + shape + '</div>'
            + (s.name ? '<div class="lbl">' + esc(s.name) + '</div>' : '')
            + '</div>'
    });
}
function fitShips() {
    if (!map || !fitted) return;
    const ms = Object.values(markers);
    if (!ms.length) return;
    map.fitBounds(L.latLngBounds(ms.map(m => m.getLatLng())).pad(0.25));
}
function updateMap(ships) {
    if (!map) return;
    const now = Date.now() / 1000;
    const seen = new Set();
    let onMap = 0, noPos = 0, staleCount = 0;
    for (const s of ships) {
        if (s.lat === undefined || s.lat === null || s.lon === undefined || s.lon === null) {
            noPos++;
            continue;
        }
        seen.add(String(s.mmsi));
        onMap++;
        const age = Math.max(0, Math.round(now - s.last_seen));
        if (age > 600) staleCount++;
        let m = markers[s.mmsi];
        if (!m) {
            m = markers[s.mmsi] = L.marker([s.lat, s.lon], { title: s.name || String(s.mmsi) }).addTo(map);
            m.bindPopup(shipPopup(s, age));
        } else {
            m.setLatLng([s.lat, s.lon]);
        }
        m.setIcon(shipIcon(s));           // latest position AND rotation every refresh
        m.setPopupContent(shipPopup(s, age));
    }
    for (const k of Object.keys(markers)) {
        if (!seen.has(k)) { map.removeLayer(markers[k]); delete markers[k]; }
    }
    if (onMap && !fitted) fitted = true, fitShips();
    document.getElementById('mapstats').textContent =
        onMap + ' ship' + (onMap === 1 ? '' : 's') + ' with position on the map'
        + (staleCount ? ' · ' + staleCount + ' faded (not heard for 10+ min)' : '')
        + (noPos ? ' · ' + noPos + ' without position yet' : '');
}
initMap();

// AIS status header + ship map — shared by the /ais.json fallback poll
// and the WS 'ais' topic push
function applyAisStatus(s) {
    const ch = s.channels || {};
        const chA = ch.A ? ch.A.frames : 0, chB = ch.B ? ch.B.frames : 0;
        document.getElementById('stats').textContent =
            (s.enabled ? 'Receiver live on dongle ' + s.dongle
                        : 'AIS disabled (AIS dongle not added) — showing the persisted log')
            + ' · frames decoded: ch A ' + chA + ' + ch B ' + chB
            + ' (' + (chA + chB) + ' this run) · ships tracked: ' + s.ships.length;
    updateMap(s.ships);
}

// Log tail render — shared by the /aislog.json fallback poll and the WS
// 'aislog' topic push (the server pushes the subscribed count)
function applyAisLog(log) {
    document.getElementById('loadinfo').textContent = ' · showing ' + log.length + ' log lines';
    const rows = document.getElementById('rows');
        if (!log.length) {
            rows.innerHTML = '<tr><td colspan="11" class="muted">No AIS frames decoded yet — the log fills as ships come into range.</td></tr>';
            return;
        }
        let html = '';
        for (const e of log) {
            const t = (e.ts !== undefined && e.ts !== null) ? fmtTime(e.ts) : e.time;
            const pos = (e.lat !== null && e.lat !== undefined)
                ? '<a href="https://www.google.com/maps?q=' + e.lat + ',' + e.lon + '" target="_blank" rel="noopener">' + e.lat.toFixed(4) + ', ' + e.lon.toFixed(4) + '</a>'
                : null;
            html += '<tr class="cls-' + esc(e.cls || '') + '" title="' + esc(e.nmea || '') + '">'
                + cell(esc(t), 't')
                + cell(esc(e.ch), 'ch')
                + cell(esc(e.msg + ' ' + (MSG_NAMES[e.msg] || '?')), 'type')
                + cell(esc(e.mmsi), 'mmsi')
                + cell(esc(e.name || ''), 'name')
                + cell(pos, 'pos')
                + cell(e.sog !== null && e.sog !== undefined ? e.sog + ' kn' : null)
                + cell(e.cog !== null && e.cog !== undefined ? e.cog + '\u00b0' : null)
                + cell(e.hdg !== null && e.hdg !== undefined ? e.hdg + '\u00b0' : null)
                + cell(esc(e.status || e.callsign || ''))
                + cell(esc(e.nmea || ''), 'nmea')
                + '</tr>';
        }
    rows.innerHTML = html;
}

async function load() {
    try {
        const n = document.getElementById('count').value;
        applyAisLog(await (await fetch('/aislog.json?count=' + n)).json());
        applyAisStatus(await (await fetch('/ais.json')).json());
    } catch (err) {
        console.error('AIS log load failed:', err);
    }
}

document.getElementById('count').addEventListener('change', () => {
    // new tail length: re-subscribe (the server pushes the new count
    // at once); without the socket the HTTP fallback loads it directly
    if (aisWsLive()) sendAisSub(); else load();
});
let timer = null;
function setTimer() {
    if (timer) clearInterval(timer);
    if (document.getElementById('autorefresh').checked)
        timer = setInterval(() => { if (!aisWsLive()) load(); }, 10000);
}
document.getElementById('autorefresh').addEventListener('change', setTimer);
load();
setTimer();

// ---------- WebSocket live updates (JSON topic push over /waterfall.ws) ----
// Subscribes to the AIS topics; the HTTP polling above keeps running
// as the fallback only while the socket is down.
let aisSocket = null, aisWsBackoff = 2000;
function aisWsLive() { return aisSocket && aisSocket.readyState === 1; }
function sendAisSub() {
    try { aisSocket.send('sub=ais,ais_ships,aislog=' + (document.getElementById('count').value || 200)); } catch (e) {}
}
function connectAisWs() {
    try {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        const ws = new WebSocket(proto + location.host + '/waterfall.ws');
        aisSocket = ws;
        ws.binaryType = 'arraybuffer';
        ws.onopen = () => { aisWsBackoff = 2000; sendAisSub(); };
        ws.onmessage = ev => {
            if (typeof ev.data === 'string') return;
            const u = new Uint8Array(ev.data);
            if (u.length < 4 || u[0] !== 2) return;   // no waterfall rows here
            const nlen = (u[1] << 8) | u[2];
            if (u.length < 3 + nlen) return;
            const topic = new TextDecoder().decode(u.subarray(3, 3 + nlen));
            try {
                const data = JSON.parse(new TextDecoder().decode(u.subarray(3 + nlen)));
                if (topic === 'ais') applyAisStatus(data);
                else if (topic === 'aislog') applyAisLog(data);
                else if (topic === 'ais_ships') applyRegistry(data);
            } catch (e) { console.error('WS topic ' + topic + ' failed:', e); }
        };
        ws.onclose = () => {
            aisSocket = null;
            setTimeout(connectAisWs, aisWsBackoff);
            aisWsBackoff = Math.min(60000, aisWsBackoff * 2);
        };
        ws.onerror = () => { try { ws.close(); } catch (e) {} };
    } catch (e) {
        aisSocket = null;
        setTimeout(connectAisWs, aisWsBackoff);
        aisWsBackoff = Math.min(60000, aisWsBackoff * 2);
    }
}
connectAisWs();

        document.getElementById('trackmode-btn').addEventListener('click', toggleTrackMode);
