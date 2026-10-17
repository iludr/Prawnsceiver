const decodingInProcess = {};
let historyData = [];
let recordingsData = [];
const HIST_PAGE_SIZE = 25;     // pass list: 25 cards per page
let histPage = 0;              // page 0 = the 25 most recent passes
function productsHtml(rec) {
    // Non-image products only (dataset/cadu/log): every PNG sits in the
    // slideshow above - listing both would duplicate the same files
    const files = (rec.products || []).filter(p => !p.path.toLowerCase().endsWith('.png'));
    if (!files.length) return '';
    return '<div class="filename" style="margin-top:6px;">' + '📂 Products (' + files.length + '):'
        + files.map(p => '<div style="padding:1px 0 1px 14px;"><a href="/products/' + encPath(p.path) + '" target="_blank">'
            + esc(p.path) + '</a> <span style="color:#888;">(' + p.size_kb + ' kB)</span></div>').join('')
        + '</div>';
}

// Path with each segment encoded separately (no %2F: the slashes stay
// real so nginx and the endpoints see a normal multi-segment path)
function encPath(p) { return p.split('/').map(encodeURIComponent).join('/'); }

// ---------- decoded-image slideshow + full-size lightbox ----------
// Every decoded image of one recording - the flat composite first, then
// all SatDump product images - as a click-through slideshow. Clicking the
// shown image opens the ORIGINAL in the in-page lightbox (displayed, not
// downloaded); arrows/left-right work while the lightbox is open.
const slideshowPos = {};   // recording id -> current index, survives refreshes
let slideshowData = {};     // recording id -> [{thumb, full, label}] of the last render

function decodedImages(rec) {
    const imgs = [];
    if (rec.decoded && rec.png)
        imgs.push({ thumb: '/thumbs/' + encPath(rec.png), full: '/images/' + encPath(rec.png), label: rec.png });
    (rec.products || []).forEach(p => {
        if (p.path.toLowerCase().endsWith('.png'))
            imgs.push({ thumb: '/thumbs/' + encPath(p.path), full: '/products/' + encPath(p.path), label: p.path });
    });
    return imgs;
}

function slideshowHtml(rec) {
    const imgs = decodedImages(rec);
    if (!imgs.length) return '';
    const id = safeId(rec.filename);
    slideshowData[id] = imgs;
    let i = slideshowPos[id] || 0;
    if (i >= imgs.length) i = 0;
    slideshowPos[id] = i;
    return '<div class="slideshow" data-id="' + id + '">'
        + '<div class="slideshow-img" onclick="openLightbox(\'' + id + '\')" title="Click to view the original image">'
        + '<img id="slideimg-' + id + '" src="' + imgs[i].thumb + '" alt="Decoded image" loading="lazy"></div>'
        + '<div class="slideshow-bar">'
        + '<button onclick="slideStep(\'' + id + '\',-1)" title="Previous image">&#9664;</button>'
        + '<span class="count" id="slidecount-' + id + '">' + (i + 1) + ' / ' + imgs.length + '</span>'
        + '<button onclick="slideStep(\'' + id + '\',1)" title="Next image">&#9654;</button>'
        + '<span class="lbl" id="slidelbl-' + id + '">' + esc(imgs[i].label) + '</span>'
        + '</div></div>';
}

function slideStep(id, d) {
    const imgs = slideshowData[id];
    if (!imgs || !imgs.length) return;
    const i = ((slideshowPos[id] || 0) + d + imgs.length) % imgs.length;
    slideshowPos[id] = i;
    document.getElementById('slideimg-' + id).src = imgs[i].thumb;
    document.getElementById('slidecount-' + id).textContent = (i + 1) + ' / ' + imgs.length;
    document.getElementById('slidelbl-' + id).textContent = imgs[i].label;
}

let lightboxImgs = null, lightboxPos = 0;

function updateLightbox() {
    const img = lightboxImgs[lightboxPos];
    document.getElementById('lb-img').src = img.full;
    document.getElementById('lb-caption').textContent =
        img.label + '  (' + (lightboxPos + 1) + ' / ' + lightboxImgs.length + ')';
}

function openLightbox(id) {
    const imgs = slideshowData[id];
    if (!imgs || !imgs.length) return;
    lightboxImgs = imgs;
    lightboxPos = slideshowPos[id] || 0;
    updateLightbox();
    document.getElementById('img-lightbox').classList.add('show');
}

function closeLightbox(ev) {
    if (ev && ev.target && ev.target.id !== 'img-lightbox') return;  // only the backdrop closes it
    document.getElementById('img-lightbox').classList.remove('show');
    lightboxImgs = null;
}

function lightboxStep(d) {
    if (!lightboxImgs) return;
    lightboxPos = (lightboxPos + d + lightboxImgs.length) % lightboxImgs.length;
    updateLightbox();
}

// One-off lightbox for an image outside a slideshow (the image that a
// Decode button just produced)
function openSingleLightbox(png) {
    lightboxImgs = [{ full: '/images/' + encPath(png), label: png }];
    lightboxPos = 0;
    updateLightbox();
    document.getElementById('img-lightbox').classList.add('show');
}

// Arrow keys click through the lightbox, Esc closes it
document.addEventListener('keydown', e => {
    if (!lightboxImgs) return;
    if (e.key === 'ArrowLeft') lightboxStep(-1);
    else if (e.key === 'ArrowRight') lightboxStep(1);
    else if (e.key === 'Escape') closeLightbox();
});

function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
function safeId(f) { return f.replace(/[^a-zA-Z0-9]/g, ''); }

function qualityBadge(q) {
    if (q === null || q === undefined) return '';
    const color = q >= 70 ? '#53d769' : (q >= 40 ? '#f39c12' : '#e74c3c');
    return ' <span style="color:' + color + ';font-weight:bold;" title="Reception quality (sync + signal integrity)">&#128246; ' + esc(q) + '%</span>';
}

// Decode button(s) for an un-decoded recording: recordings whose decode
// already failed once (server-side attempt marker) show the reason and a
// Retry button (?force=1) instead of auto-re-decoding on every page load
function decodeButtonsHtml(r) {
    if (r.decoded) return '';
    if (r.decode_attempted)
        return '<div class="result" style="color:#e74c3c;">&#10060; ' + esc(r.decode_error || 'previous decode attempt failed') + '</div>'
             + '<button onclick="decodeRecording(\'' + esc(r.filename) + '\', true)">&#128260; Retry Decode</button>';
    return '<button onclick="decodeRecording(\'' + esc(r.filename) + '\')">&#128260; Decode APT Image</button>';
}

function render() {
    const list = document.getElementById('history-list');
    const pager = document.getElementById('histpager');
    if (historyData.length === 0) {
        list.innerHTML = '<div class="muted">No passes logged yet. Passes appear here after they complete.</div>';
        pager.style.display = 'none';
    } else {
        // Newest first, then only one page of HIST_PAGE_SIZE cards
        const newest = [...historyData].reverse();
        const pages = Math.max(1, Math.ceil(newest.length / HIST_PAGE_SIZE));
        if (histPage >= pages) histPage = pages - 1;
        const start = histPage * HIST_PAGE_SIZE;
        const slice = newest.slice(start, start + HIST_PAGE_SIZE);
        pager.style.display = newest.length > HIST_PAGE_SIZE ? '' : 'none';
        document.getElementById('hist-prev').disabled = histPage === 0;
        document.getElementById('hist-next').disabled = histPage >= pages - 1;
        document.getElementById('hist-pageinfo').textContent =
            'passes ' + (start + 1) + '–' + (start + slice.length)
            + ' of ' + newest.length;
        let html = '';
        for (const h of slice) {
            const q = h.max_alt >= 35 ? 'high' : (h.max_alt >= 15 ? 'medium' : 'low');
            const qi = q === 'high' ? '🟢' : (q === 'medium' ? '🟡' : '🔴');
            const rec = h.wav ? recordingsData.find(r => r.filename === h.wav) : null;
            const rid = h.wav ? 'result-' + safeId(h.wav) : '';
            // SatNOGS DB snapshot stored with the pass (hover for transmitters)
            let satLine = '';
            if (h.satnogs) {
                const s = h.satnogs;
                const bits = [];
                if (s.names) bits.push(esc(s.names));
                if (s.launched) bits.push('launched ' + esc(s.launched));
                if (s.status) bits.push(esc(s.status));
                if (s.countries) bits.push(esc(s.countries));
                const tip = (s.transmitters || []).map(t => {
                    const dl = t.downlink_hz ? ' ' + (t.downlink_hz / 1e6).toFixed(4) + ' MHz' : '';
                    return (t.description || '?') + dl + (t.mode ? ' [' + t.mode + ']' : '') + (t.alive ? '' : ' (inactive)');
                }).join('\n');
                satLine = '<div class="card-meta" style="margin:-4px 0 8px;color:#666;" title="' + esc(tip) + '">🛰 ' + bits.join(' · ') + '</div>';
            }
            html += '<div class="pass-card ' + q + '">'
                + '<div class="card-head"><span class="sat">' + qi + ' ' + esc(h.sat_name) + '</span>'
                + ' &nbsp;' + esc(h.rise_local) + ' → ' + esc(h.set_local || '?')
                + ' &nbsp;|&nbsp; ' + esc(h.frequency_mhz) + ' MHz</div>'
                + '<div class="card-meta">max ' + esc(h.max_alt) + '° &nbsp;|&nbsp; '
                + esc(h.duration_min) + ' min &nbsp;|&nbsp; peak: ' + esc(h.signal_peak || '—')
                + ' &nbsp;|&nbsp; ' + (h.decoded ? '🖼️ decoded' : '❌ not decoded')
                + qualityBadge(h.quality !== null && h.quality !== undefined ? h.quality : (rec ? rec.quality : null)) + '</div>'
                + satLine;
            if (rec) {
                html += '<div class="filename">' + esc(rec.filename) + ' (' + esc(rec.size_mb) + ' MB)</div>'
                    + (rec.iq ? '<div class="filename" style="color:#888;">' + '📡 raw IQ: '
                       + esc(rec.iq.filename) + ' (' + rec.iq.size_mb + ' MB) - deleted with the recording</div>' : '');
                if (rec.recording_in_progress) {
                    html += '<div class="result" style="color:#f39c12;">🔴 Pass is being recorded right now — audio and decoding are available after the pass ends.</div>';
                } else {
                    html += decodeButtonsHtml(rec)
                        + '<button class="danger" onclick="deleteRecording(\'' + esc(rec.filename) + '\')">🗑 Delete</button>'
                        + '<audio controls preload="none"><source src="/audio/' + esc(rec.filename) + '" type="audio/wav"></audio>'
                        + '<div class="result" id="' + rid + '"></div>'
                        + slideshowHtml(rec)
                        + (rec.products && rec.products.length ? productsHtml(rec) : '');
                }
            } else {
                html += '<div class="muted" style="padding:4px 0;">' + (h.wav ? 'recording file deleted' : 'no recording on disk') + '</div>';
            }
            html += '</div>';
        }
        list.innerHTML = html;
    }
    const unmatched = recordingsData.filter(r => !historyData.some(h => h.wav === r.filename));
    const utable = document.getElementById('unmatched-list');
    document.getElementById('unmatched-title').style.display = unmatched.length ? '' : 'none';
    let uhtml = '';
    for (const r of unmatched) {
        const rid = 'result-' + safeId(r.filename);
        uhtml += '<div class="pass-card">'
            + '<div class="card-head"><span class="sat">🎧 ' + esc(r.filename) + '</span> &nbsp;|&nbsp; ' + esc(r.size_mb) + ' MB' + qualityBadge(r.quality) + '</div>'
            + (r.iq ? '<div class="filename" style="color:#888;">' + '📡 raw IQ: '
                + esc(r.iq.filename) + ' (' + r.iq.size_mb + ' MB) - deleted with the recording</div>' : '')
            + (r.recording_in_progress
                ? '<div class="result" style="color:#f39c12;">🔴 Pass is being recorded right now — audio and decoding are available after the pass ends.</div>'
                : decodeButtonsHtml(r)
                    + '<button class="danger" onclick="deleteRecording(\'' + esc(r.filename) + '\')">🗑 Delete</button>'
                    + '<audio controls preload="none"><source src="/audio/' + esc(r.filename) + '" type="audio/wav"></audio>'
                    + '<div class="result" id="' + rid + '"></div>')
            + slideshowHtml(r)
            + (r.products && r.products.length ? productsHtml(r) : '')
            + '</div>';
    }
    utable.innerHTML = uhtml;
    document.getElementById('crab-count').textContent = recordingsData.filter(r => r.decoded).length;
    // Auto-decode recordings that are not decoded yet (never the one being written)
    for (const r of recordingsData) {
        if (!r.decoded && !r.recording_in_progress && !r.decode_attempted && !decodingInProcess[r.filename]) {
            decodingInProcess[r.filename] = true;
            setTimeout(() => decodeRecording(r.filename), 3000);
        }
    }
}

async function decodeRecording(filename, force) {
    const resultDiv = document.getElementById('result-' + safeId(filename));
    if (resultDiv) resultDiv.innerHTML = '<span style="color:#f39c12;">⏳ Decoding...</span>';
    try {
        const res = await fetch('/decode/' + filename + (force ? '?force=1' : ''));
        const data = await res.json();
        if (data.success && resultDiv) {
            resultDiv.innerHTML = '<span style="color:#53d769;">✅ Decoded!</span>' + qualityBadge(data.quality) + '<img class="decoded-img" src="/thumbs/' + esc(data.png) + '?t=' + Date.now() + '" alt="Decoded image" loading="lazy" title="Click to view the original image" onclick="openSingleLightbox(\'' + esc(data.png) + '\')" style="cursor:zoom-in;">';
        } else if (resultDiv) {
            resultDiv.innerHTML = '<span style="color:#e74c3c;">❌ ' + esc(data.error || 'Decode failed') + '</span>';
        }
    } catch(e) {
        if (resultDiv) resultDiv.innerHTML = '<span style="color:#e74c3c;">❌ Error: ' + esc(e) + '</span>';
    }
}

async function deleteRecording(filename) {
    if (!confirm('Delete ' + filename + ' with its raw IQ capture and decoded products? This cannot be undone.')) return;
    try {
        const res = await fetch('/delete/' + filename);
        const data = await res.json();
        if (data.success) {
            delete decodingInProcess[filename];
            refresh();
        } else {
            alert('Delete failed: ' + (data.error || 'unknown error'));
        }
    } catch(e) {
        alert('Delete failed: ' + e);
    }
}

async function refresh() {
    try {
        const [h, r] = await Promise.all([fetch('/pass_history.json'), fetch('/recordings.json')]);
        historyData = await h.json();
        recordingsData = await r.json();
        render();
    } catch(e) { console.error('History refresh failed:', e); }
}

// ---------- WebSocket live updates (JSON topic push over /waterfall.ws) ----
// Subscribes to the history topics; the HTTP refresh above keeps
// running as the fallback only while the socket is down.
let histSocket = null, histWsBackoff = 2000;
function histWsLive() { return histSocket && histSocket.readyState === 1; }
function connectHistWs() {
    try {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        const ws = new WebSocket(proto + location.host + '/waterfall.ws');
        histSocket = ws;
        ws.binaryType = 'arraybuffer';
        ws.onopen = () => { histWsBackoff = 2000; try { ws.send('sub=pass_history,recordings'); } catch (e) {} };
        ws.onmessage = ev => {
            if (typeof ev.data === 'string') return;
            const u = new Uint8Array(ev.data);
            if (u.length < 4 || u[0] !== 2) return;
            const nlen = (u[1] << 8) | u[2];
            if (u.length < 3 + nlen) return;
            const topic = new TextDecoder().decode(u.subarray(3, 3 + nlen));
            try {
                const data = JSON.parse(new TextDecoder().decode(u.subarray(3 + nlen)));
                if (topic === 'pass_history') historyData = data;
                else if (topic === 'recordings') recordingsData = data;
                else return;
                render();
            } catch (e) { console.error('WS topic ' + topic + ' failed:', e); }
        };
        ws.onclose = () => {
            histSocket = null;
            setTimeout(connectHistWs, histWsBackoff);
            histWsBackoff = Math.min(60000, histWsBackoff * 2);
        };
        ws.onerror = () => { try { ws.close(); } catch (e) {} };
    } catch (e) {
        histSocket = null;
        setTimeout(connectHistWs, histWsBackoff);
        histWsBackoff = Math.min(60000, histWsBackoff * 2);
    }
}
connectHistWs();

document.getElementById('hist-prev').addEventListener('click',
    () => { if (histPage > 0) { histPage--; render(); } });
document.getElementById('hist-next').addEventListener('click',
    () => { histPage++; render(); });

refresh();
setInterval(() => { if (!histWsLive()) refresh(); }, 8000);

