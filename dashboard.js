        const startTime = Date.now();
        let tleInfo = null;
        // TLE sync progress bar state: ms timestamp of the last accepted
        // sync request — bridges the gap between the button click and the
        // scheduler picking the request up (~10 s) with a "starting" bar.
        // tleSyncWasActive: the previous poll saw the sync running, so a
        // following inactive poll means it FINISHED — the bar must hide,
        // not fall back into the starting window.
        let tleSyncClickAt = 0;
        let tleSyncWasActive = false;
        const WF_WIDTH = 512, WF_HEIGHT = 300, SPEC_HEIGHT = 110;
        // Waterfall time axis: the canvas height is fixed; the per-dongle
        // Time slider decides how much history those 300 px cover
        // (2 s ... 2 min). Incoming rows are averaged into the pixel
        // they land in, so a longer range lowers the line resolution,
        // never the canvas height
        const WF_TIME_MIN = 2, WF_TIME_MAX = 120, WF_TIME_DEFAULT = 5;
        // dB row ring: long enough for the longest slider range at the
        // WebSocket push rate (120 s x 60 rows/s = 7200 rows, ~7 MB per
        // dongle) — replayWaterfall decimates from it on any change
        const DB_RING_CAP = 7200;
        // Capture span (kHz covered by the 512-bin FFT = SDR_RATE).
        // Set from status.json's sdr_khz - NEVER hardcoded again: the
        // pre-WebSocket frontend assumed 240 kHz from the original
        // low-rate mode while the rows really spanned the full capture
        // (2.4 MHz today) - every axis label and click-to-tune was 10x
        // off (verified live: a 99.6 MHz broadcast station lands at the
        // bin predicted by the 2.4 MHz span, nowhere near the 240 kHz
        // model's position).
        let SDR_KHZ = 2400;
        // One waterfall block per dongle, built dynamically from /dongles.json.
        // Blocks are keyed by dongle id ("host:port" of its rtl_tcp
        // server; dongles are added/removed at runtime via /add_dongle).
        // The primary dongle (live audio, recording) carries the
        // recording-band highlight (wf-highlight) and a ★ badge.
        const wfBlocks = {};   // dongle serial -> {wfCtx, specCtx, overlay, title, led, sig}

        // ---------- Frequency allocation lookup (Germany / ITU Region 1) ----------
        // Coarse receive-side overview of what each part of the R820T range
        // (24–1766 MHz) is normally used or reserved for — orientation for
        // waterfall reading, not an authoritative band plan. When entries
        // overlap, the narrowest match wins (e.g. ADS-B inside the aviation
        // navigation band, 70 cm inside 433 ISM).
        const BANDS = [
            { lo: 24,      hi: 26.965,  name: 'Short-range devices', desc: 'below CB, sparse use' },
            { lo: 26.965,  hi: 27.405,  name: 'CB radio (citizens band)', desc: 'AM/FM, 40 channels, truckers & hobby' },
            { lo: 27.405,  hi: 28,      name: 'Short-range devices', desc: 'ISM fringe' },
            { lo: 28,      hi: 29.7,    name: '10 m amateur band', desc: 'ham shortwave, SSB/CW' },
            { lo: 29.7,    hi: 47,      name: 'Military / PMR low band', desc: 'analog FM land mobile, sparse' },
            { lo: 47,      hi: 68,      name: 'PMR / paging (former VHF TV)', desc: 'land mobile after TV moved out' },
            { lo: 68,      hi: 74.875,  name: 'PMR / rail', desc: 'analog land mobile' },
            { lo: 74.875,  hi: 87.5,    name: 'BOS 4 m band (German fire/police/rescue)', desc: 'analog FM dispatch, the scanner classic' },
            { lo: 87.5,    hi: 108,     name: 'FM broadcast radio', desc: 'public FM radio, RDS, always busy' },
            { lo: 108,     hi: 117.975, name: 'Aviation navigation', desc: 'VOR/ILS beacons, Morse IDs' },
            { lo: 117.975, hi: 137,     name: 'Airband (aviation voice AM)', desc: 'towers & aircraft' },
            { lo: 137,     hi: 138,     name: 'Weather satellites (LRPT)', desc: 'Meteor-M imagery — this station\'s home band' },
            { lo: 138,     hi: 144,     name: 'Space research / satellite downlinks', desc: 'telemetry & data' },
            { lo: 144,     hi: 146,     name: '2 m amateur band', desc: 'APRS DL at 144.8, FM calling 145.5' },
            { lo: 146,     hi: 149.9,   name: 'Commercial PMR', desc: 'taxi, utilities, mostly digital now' },
            { lo: 149.9,   hi: 156.2425,name: 'Rail & commercial PMR', desc: 'shunting & yard radio' },
            { lo: 156.2425,hi: 162.025, name: 'Marine VHF', desc: 'Ch 16 = 156.8 distress & calling' },
            { lo: 162.025, hi: 174,     name: 'Commercial / government PMR', desc: 'mostly digital (DMR/TETRA)' },
            { lo: 174,     hi: 230,     name: 'DAB+ digital radio (band III)', desc: 'former VHF TV, now audio broadcast' },
            { lo: 230,     hi: 400,     name: 'Military aeronautical (NATO UHF)', desc: 'air-ground, mostly encrypted' },
            { lo: 400,     hi: 406,     name: 'Satellite data collection', desc: 'meteorological & Argos' },
            { lo: 406,     hi: 410,     name: 'Radio astronomy / satellite EPIRB', desc: 'passive science; 406.1 distress beacons' },
            { lo: 410,     hi: 430,     name: 'Government / PMR', desc: 'TETRA, digital' },
            { lo: 430,     hi: 440,     name: '70 cm amateur + ISM 433', desc: 'ham + remotes, sensors, weather stations' },
            { lo: 440,     hi: 446,     name: 'Amateur / ISM fringe', desc: 'ATV & experiments' },
            { lo: 446.0,   hi: 446.2,   name: 'PMR446', desc: 'license-free handhelds, analog & digital' },
            { lo: 446.2,   hi: 470,     name: 'Commercial PMR (UHF)', desc: 'analog + digital land mobile' },
            { lo: 470,     hi: 694,     name: 'DVB-T2 digital TV (UHF)', desc: 'channels 21–48, high power' },
            { lo: 694,     hi: 790,     name: 'Mobile networks (700 MHz / 5G)', desc: '5G/LTE downlink' },
            { lo: 790,     hi: 960,     name: 'Mobile networks (GSM/LTE/5G)', desc: 'downlink receive, noise-like signals' },
            { lo: 960,     hi: 1215,    name: 'Aviation navigation (DME/TACAN)', desc: 'distance measuring, pulse pairs' },
            { lo: 1087.5,  hi: 1092.5,  name: 'ADS-B aircraft beacons', desc: 'position squitters at 1090 MHz' },
            { lo: 1215,    hi: 1240,    name: 'GNSS L2 / military', desc: 'GPS L2 1227.6, spread spectrum' },
            { lo: 1240,    hi: 1300,    name: '23 cm amateur band', desc: 'ham, ATV & data' },
            { lo: 1300,    hi: 1400,    name: 'Military radar / ATC (S-band)', desc: 'air surveillance radar, pulse trains' },
            { lo: 1400,    hi: 1427,    name: 'Radio astronomy (passive)', desc: 'Earth exploration, protected' },
            { lo: 1427,    hi: 1525,    name: 'Mobile / space operations', desc: 'mixed government & space' },
            { lo: 1525,    hi: 1559,    name: 'Inmarsat satellite downlink', desc: 'L-band satcom, narrowband channels' },
            { lo: 1559,    hi: 1610,    name: 'GNSS (GPS/Galileo L1)', desc: 'navigation, spread spectrum' },
            { lo: 1610,    hi: 1626.5,  name: 'Iridium satellite downlink', desc: '66-sat constellation, bursty data' },
            { lo: 1626.5,  hi: 1766,    name: 'MSS uplink / aeronautical satcom', desc: 'uplinks — weak on the ground' },
        ];
        // Narrowest containing entry wins (handles nested bands)
        function bandFor(mhz) {
            let best = null;
            for (const b of BANDS) {
                if (mhz >= b.lo && mhz < b.hi && (!best || b.hi - b.lo < best.hi - best.lo)) best = b;
            }
            return best;
        }
        function bandLabel(b) {
            if (!b) return '<span style="color:#666;">no allocation data</span>';
            const fmt = (v) => (+v.toFixed(3)) + ' MHz';
            return `${b.name} <span class="band-range">(${fmt(b.lo)}–${fmt(b.hi)})</span>`;
        }

        // Band jump dropdown: typical use cases, one entry per region of the
        // spectrum. Selecting one manual-tunes the whole station there
        // (pauses satellite tracking, rejected during an active pass);
        // "Auto" hands control back to the scheduler.
        const BAND_PRESETS = [
            { label: '🛰 Auto — satellite tracking', f: 'auto' },
            { label: '📻 FM broadcast radio → 98.0 MHz', f: 98.0 },
            { label: '🚒 BOS 4 m fire/police (analog) → 85.5 MHz', f: 85.5 },
            { label: '✈ Airband voice AM (guard) → 121.5 MHz', f: 121.5 },
            { label: '🛰 Meteor LRPT downlink → 137.9125 MHz', f: 137.9125 },
            { label: '📡 APRS packet radio (DL) → 144.8 MHz', f: 144.8 },
            { label: '📻 2 m amateur FM calling → 145.5 MHz', f: 145.5 },
            { label: '🚢 Marine VHF Ch 16 → 156.8 MHz', f: 156.8 },
            { label: '🎧 DAB+ digital radio → 211.6 MHz', f: 211.6 },
            { label: '🔧 ISM 433 remotes & sensors → 433.92 MHz', f: 433.92 },
            { label: '🛰 ISS / 70 cm amateur → 437.55 MHz', f: 437.55 },
            { label: '📟 PMR446 handhelds → 446.1 MHz', f: 446.1 },
            { label: '📺 DVB-T2 digital TV → 546 MHz', f: 546.0 },
            { label: '📡 ADS-B aircraft beacons → 1090 MHz', f: 1090.0 },
            { label: '🛰 Iridium satellite downlink → 1622.5 MHz', f: 1622.5 },
        ];
        function buildBandJump() {
            const sel = document.getElementById('band-jump');
            const opt = document.createElement('option');
            opt.value = '';
            opt.textContent = '📶 Jump to a band…';
            sel.appendChild(opt);
            for (const p of BAND_PRESETS) {
                const o = document.createElement('option');
                o.value = String(p.f);
                o.textContent = p.label;
                sel.appendChild(o);
            }
            sel.onchange = bandJump;
        }
        async function bandJump() {
            const sel = document.getElementById('band-jump');
            const val = sel.value;
            if (val === '') return;
            if (val === 'auto') {
                try { await fetch('/tune?f=auto'); toast('🛰 Satellite tracking resumed'); } catch (e) {}
                sel.value = '';
                return;
            }
            const mhz = parseFloat(val);
            try {
                const res = await fetch('/tune?f=' + encodeURIComponent(mhz));
                const data = await res.json();
                if (!data.success) {
                    toast('Tune failed: ' + (data.error || res.status), true);
                    sel.value = '';
                } else {
                    // Move ALL dongles along: clear per-dongle overrides so
                    // every dongle rejoins the freshly selected shared
                    // frequency (only after the tune succeeded — on failure,
                    // parked dongles stay parked)
                    try {
                        const dres = await fetch('/dongles.json');
                        const dongles = await dres.json();
                        await Promise.all(dongles
                            .filter(d => d.manual_frequency_mhz !== null && d.manual_frequency_mhz !== undefined)
                            .map(d => fetch('/tune_dongle?d=' + encodeURIComponent(d.id) + '&f=auto')));
                    } catch (e) { /* dongles.json unreachable — overrides survive */ }
                    const b = bandFor(mhz);
                    toast(`Tuned all dongles to ${mhz.toFixed(4)} MHz — ${b ? b.name : 'unknown band'} · 🛰 Auto on this menu resumes tracking`);
                }
            } catch (e) {
                toast('Tune failed: receiver unreachable', true);
                sel.value = '';
            }
        }

        // ---------- waterfall zoom (Ctrl+wheel / trackpad pinch) ----------
        // Per-block view state: zoom is the visible fraction of the
        // capture span (SDR_KHZ), zoomOffKHz pans the window around the
        // tuned center. 1x = the full capture, max 32x = 1/32 of it
        // (16 FFT bins at any SDR_RATE).
        function wfSpanKhz(b) { return SDR_KHZ / (b.zoom || 1); }
        function wfKhzToPct(b, khz) {
            const span = wfSpanKhz(b);
            return 50 + ((khz - (b.zoomOffKHz || 0)) / (span / 2)) * 50;
        }
        function clearWfCanvas(b) {
            // zoom changes the row geometry — the accumulated history would
            // be torn, so the waterfall restarts empty
            b.wfCtx.fillStyle = '#000';
            b.wfCtx.fillRect(0, 0, WF_WIDTH, WF_HEIGHT);
            b.specCtx.fillStyle = '#000';
            b.specCtx.fillRect(0, 0, WF_WIDTH, SPEC_HEIGHT);
            if (b.timeCtx) { b.timeCtx.fillStyle = '#000'; b.timeCtx.fillRect(0, 0, 54, WF_HEIGHT); }
            b.tickCounter = 0;
        }
        function updateZoomBadge(b) {
            if (!b.zoomBadge) return;
            const z = b.zoom || 1;
            if (z > 1.01) {
                b.zoomBadge.style.display = 'block';
                b.zoomBadge.textContent = '🔍 ' + z.toFixed(1) + '×';
            } else {
                b.zoomBadge.style.display = 'none';
            }
        }

        // SDR#-style right sidebar: Zoom / Contrast / Range / Offset
        // sliders, persisted per dongle id in localStorage, + a reset view
        // button. Zoom mirrors the Ctrl+wheel zoom (both recenter); the dB
        // controls drive mapDbRow/drawSpectrum and take effect on the whole
        // retained history via replayWaterfall.
        function buildWfControls(b, meta) {
            let saved = {};
            try { saved = JSON.parse(localStorage.getItem('wfctl.' + meta.id) || '{}'); } catch (e) {}
            b.dbContrast = saved.contrast !== undefined ? saved.contrast : 1;
            b.dbRange = saved.range !== undefined ? saved.range : 80;
            b.dbOffset = saved.offset !== undefined ? saved.offset : -70;
            b.tRangeSec = saved.time !== undefined ? saved.time : WF_TIME_DEFAULT;
            const el = document.createElement('div');
            el.className = 'wf-controls';
            const save = () => { try {
                localStorage.setItem('wfctl.' + meta.id, JSON.stringify(
                    { contrast: b.dbContrast, range: b.dbRange, offset: b.dbOffset,
                      time: b.tRangeSec }));
            } catch (e) {} };
            const mk = (label, min, max, step, value, fmt, oninput) => {
                const wrap = document.createElement('div');
                wrap.className = 'ctl';
                const row = document.createElement('div');
                row.className = 'ctl-row';
                const name = document.createElement('span');
                name.textContent = label;
                const val = document.createElement('span');
                val.className = 'ctl-val';
                row.appendChild(name); row.appendChild(val);
                const inp = document.createElement('input');
                inp.type = 'range';
                inp.min = min; inp.max = max; inp.step = step;
                inp.value = value;
                const upd = () => { val.textContent = fmt(parseFloat(inp.value)); };
                inp.oninput = () => { upd(); oninput(parseFloat(inp.value)); };
                upd();
                wrap.appendChild(row); wrap.appendChild(inp);
                el.appendChild(wrap);
                return { inp, upd };
            };
            const zVal = v => Math.min(32, Math.max(1, Math.pow(2, v / 100 * 5)));
            const zRaw = z => Math.round(Math.log2(z) / 5 * 100);
            const zctl = mk('Zoom', 0, 100, 1, zRaw(b.zoom || 1),
                v => zVal(v).toFixed(1) + '\u00d7', v => {
                    b.zoom = zVal(v); b.zoomOffKHz = 0;   // recenter on zoom
                    updateZoomBadge(b); updateFreqLabels(lastSharedMHz);
                    replayWaterfall(b);
                    if (wfOverlayOn) drawWfOverlay();
                });
            b.syncZoomSlider = () => { zctl.inp.value = zRaw(b.zoom || 1); zctl.upd(); };
            mk('Contrast', 0.3, 3, 0.05, b.dbContrast, v => v.toFixed(2),
                v => { b.dbContrast = v; save(); replayWaterfall(b); });
            mk('Range', 10, 120, 5, b.dbRange, v => v.toFixed(0) + ' dB',
                v => { b.dbRange = v; save(); replayWaterfall(b); });
            mk('Offset', -140, 0, 1, b.dbOffset, v => v.toFixed(0) + ' dB',
                v => { b.dbOffset = v; save(); replayWaterfall(b); });
            const tVal = v => Math.min(WF_TIME_MAX, Math.max(WF_TIME_MIN,
                Math.round(WF_TIME_MIN * Math.pow(2, v / 100 * Math.log2(WF_TIME_MAX / WF_TIME_MIN)))));
            const tRaw = t => Math.round(Math.log2(t / WF_TIME_MIN) / Math.log2(WF_TIME_MAX / WF_TIME_MIN) * 100);
            mk('Time', 0, 100, 1, tRaw(b.tRangeSec || WF_TIME_DEFAULT),
                v => { const t = tVal(v); return t < 60 ? t + ' s' : (t / 60).toFixed(1) + ' min'; },
                v => { b.tRangeSec = tVal(v); save(); replayWaterfall(b); });
            const rst = document.createElement('button');
            rst.textContent = 'Reset view';
            rst.onclick = () => {
                b.zoom = 1; b.zoomOffKHz = 0;
                b.dbContrast = 1; b.dbRange = 80; b.dbOffset = -70;
                b.tRangeSec = WF_TIME_DEFAULT;
                save();
                updateZoomBadge(b); updateFreqLabels(lastSharedMHz);
                el.replaceWith(buildWfControls(b, meta));
                replayWaterfall(b);
                if (wfOverlayOn) drawWfOverlay();
            };
            el.appendChild(rst);
            return el;
        }

        function ensureWfBlock(meta) {
            const idx = meta.id;
            let b = wfBlocks[idx];
            if (b) {
                b.led.className = 'indicator ' + (meta.connected ? 'green' : 'red');
                return b;
            }
            const list = document.getElementById('wf-list');
            const block = document.createElement('div');
            block.className = 'wf-block';
            block.style.cssText = 'margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #0f3460;';
            const head = document.createElement('div');
            head.style.cssText = 'display:flex;align-items:center;gap:6px;font-size:0.9em;color:#a8b8d8;margin-bottom:3px;';
            const led = document.createElement('span');
            led.className = 'indicator yellow';
            const title = document.createElement('span');
            // Name, source and tuned frequency live on the control card —
            // the head carries only the dongle number + connection LED
            title.textContent = `Dongle ${meta.index}${meta.primary ? ' ★' : ''}`;
            const collapseBtn = document.createElement('button');
            collapseBtn.className = 'wf-collapse-btn';
            collapseBtn.textContent = '▾';
            collapseBtn.title = 'Collapse/expand this dongle (waterfall + card)';
            collapseBtn.onclick = () => toggleDongleCollapse(meta.id);
            collapseBtn.style.marginLeft = 'auto';
            head.appendChild(led); head.appendChild(title); head.appendChild(collapseBtn);
            // Row: time gutter | waterfall+spectrum column. The overlays
            // (features, hover, highlight) sit inside the column only, so
            // the hover/click frequency mapping stays relative to the
            // waterfall canvas.
            const row = document.createElement('div');
            row.className = 'wf-block-row';
            const timeCanvas = document.createElement('canvas');
            timeCanvas.width = 54; timeCanvas.height = WF_HEIGHT;
            timeCanvas.className = 'wf-time';
            // CSS height like .wf-canvas: border-box keeps the box at
            // 300 px (attribute height + border would be 302 and drift
            // the gutter off the waterfall it scrolls in sync with)
            timeCanvas.style.height = WF_HEIGHT + 'px';
            const timeCtx = timeCanvas.getContext('2d');
            timeCtx.fillStyle = '#000';
            timeCtx.fillRect(0, 0, 54, WF_HEIGHT);
            // dB scale gutter under the time gutter: same width and
            // look, vertically aligned with the spectrum pane (the
            // 4 px spacer mirrors .wf-spec margin-top)
            const dbScale = document.createElement('canvas');
            dbScale.width = 54; dbScale.height = SPEC_HEIGHT;
            dbScale.className = 'wf-time';
            // same border-box model as .wf-spec (110 px box, border
            // inside): without this the scale sits 2 px lower than the
            // spectrum pane and its 110 canvas px stretch over 112 px
            dbScale.style.height = SPEC_HEIGHT + 'px';
            dbScale.title = 'dBFS scale — Offset sets the center, Range the height';
            const dbScaleCtx = dbScale.getContext('2d');
            const gutter = document.createElement('div');
            gutter.style.cssText = 'display:flex;flex-direction:column;flex-shrink:0;';
            gutter.appendChild(timeCanvas);
            const specGap = document.createElement('div');
            specGap.style.height = '4px';
            gutter.appendChild(specGap);
            gutter.appendChild(dbScale);
            const wrap = document.createElement('div');
            wrap.className = 'wf-wrap';
            // display:flex: the panes are otherwise INLINE canvases, whose
            // line boxes add a baseline descender gap between waterfall
            // and spectrum -- the dB scale gutter could never match that.
            // As flex items they stack exactly 300 + 4 + 110 px, like the
            // gutter column beside them.
            wrap.style.cssText = 'position:relative;flex:1;min-width:0;display:flex;flex-direction:column;';
            const wfCanvas = document.createElement('canvas');
            wfCanvas.width = WF_WIDTH; wfCanvas.height = WF_HEIGHT;
            wfCanvas.className = 'wf-canvas';
            // opaque from the start (a fresh canvas is transparent);
            // the scroll buffers below break the self-referential
            // drawImage(canvas, 0, 1) — the classic source of fixed
            // flickering raster lines on some GPU/driver stacks,
            // while the backing store reads back clean
            const wfCtx0 = wfCanvas.getContext('2d');
            wfCtx0.fillStyle = '#000';
            wfCtx0.fillRect(0, 0, WF_WIDTH, WF_HEIGHT);
            const wfBuf = document.createElement('canvas');
            wfBuf.width = WF_WIDTH; wfBuf.height = WF_HEIGHT;
            const timeBuf = document.createElement('canvas');
            timeBuf.width = 54; timeBuf.height = WF_HEIGHT;
            const specCanvas = document.createElement('canvas');
            specCanvas.width = WF_WIDTH; specCanvas.height = SPEC_HEIGHT;
            specCanvas.className = 'wf-spec';
            wrap.appendChild(wfCanvas); wrap.appendChild(specCanvas);
            let hl = null;
            if (meta.primary) {
                hl = document.createElement('div');
                hl.id = 'wf-highlight';
                // left/width are computed from the zoom view in
                // updateDongleCard (zoom-aware band mapping)
                hl.style.cssText = 'display:none;position:absolute;top:0;bottom:0;pointer-events:none;background:rgba(83,215,105,0.10);border-left:1px solid rgba(83,215,105,0.7);border-right:1px solid rgba(83,215,105,0.7);';
                const lbl = document.createElement('div');
                lbl.id = 'wf-highlight-label';
                lbl.style.cssText = 'position:absolute;top:2px;left:50%;transform:translateX(-50%);background:#53d769;color:#1a1a2e;font-size:0.75em;padding:1px 6px;border-radius:3px;white-space:nowrap;';
                hl.appendChild(lbl);
                wrap.appendChild(hl);
            }
            // zoom badge: shown while zoomed in; click resets to 1x
            const zoomBadge = document.createElement('div');
            zoomBadge.title = 'Click to reset the zoom';
            zoomBadge.style.cssText = 'position:absolute;top:2px;right:4px;background:rgba(0,0,0,0.65);color:#a8b8d8;font-size:0.75em;padding:1px 7px;border-radius:3px;cursor:pointer;display:none;z-index:5;';
            zoomBadge.addEventListener('mousedown', e => e.stopPropagation());
            wrap.appendChild(zoomBadge);
            // Satellite downlink marker: dashed line at the tracked
            // satellite's frequency (active or next pass) inside this view
            const satLine = document.createElement('div');
            satLine.style.cssText = 'display:none;position:absolute;top:0;bottom:0;width:0;border-left:1px dashed rgba(52,152,219,0.85);pointer-events:none;z-index:4;';
            const satLineLbl = document.createElement('div');
            satLineLbl.style.cssText = 'position:absolute;top:2px;left:0;transform:translateX(-50%);background:rgba(52,152,219,0.9);color:#1a1a2e;font-size:0.75em;padding:1px 6px;border-radius:3px;white-space:nowrap;';
            satLine.appendChild(satLineLbl);
            wrap.appendChild(satLine);
            const overlay = document.createElement('canvas');
            overlay.style.cssText = 'display:none;position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;';
            wrap.appendChild(overlay);
            // Selected (recorded) range band: shown while this dongle has a
            // manual frequency or bandwidth selection — the drag result, the
            // card controls, everything lands here
            const selHl = document.createElement('div');
            selHl.style.cssText = 'display:none;position:absolute;top:0;bottom:0;pointer-events:none;background:rgba(243,156,18,0.10);border-left:1px solid rgba(243,156,18,0.75);border-right:1px solid rgba(243,156,18,0.75);';
            const selLbl = document.createElement('div');
            selLbl.style.cssText = 'position:absolute;bottom:2px;left:50%;transform:translateX(-50%);background:#f39c12;color:#1a1a2e;font-size:0.75em;padding:1px 6px;border-radius:3px;white-space:nowrap;';
            selHl.appendChild(selLbl);
            wrap.appendChild(selHl);
            const hover = document.createElement('canvas');
            hover.className = 'wf-hover';
            wrap.appendChild(hover);
            const controls = document.createElement('div');
            controls.className = 'wf-controls';
            row.appendChild(gutter); row.appendChild(wrap); row.appendChild(controls);
            // Per-block frequency labels under this dongle's waterfall
            // (each dongle can be tuned independently via overrides)
            const labels = document.createElement('div');
            labels.className = 'wf-freq-labels';
            const freqLabels = [];
            for (let i = 0; i < 5; i++) {
                const s = document.createElement('span');
                labels.appendChild(s);
                freqLabels.push(s);
            }
            const wfPart = document.createElement('div');
            wfPart.className = 'dongle-block-wf';
            wfPart.appendChild(row); wfPart.appendChild(labels);
            // The dongle control card is created into this holder — waterfall
            // and controls form ONE container, collapsed together
            const cardHolder = document.createElement('div');
            cardHolder.className = 'dongle-block-card';
            const body = document.createElement('div');
            body.className = 'dongle-block-body';
            body.appendChild(wfPart); body.appendChild(cardHolder);
            block.appendChild(head); block.appendChild(body);
            list.appendChild(block);
            b = wfBlocks[idx] = {
                wfCtx: wfCanvas.getContext('2d'),
                specCtx: specCanvas.getContext('2d'),
                timeCtx, dbScaleCtx, overlay, hover, title, led, selHl, selLbl,
                wfBufCtx: wfBuf.getContext('2d'), timeBufCtx: timeBuf.getContext('2d'),
                block, collapseBtn, cardHolder, freqLabels,
                serial: meta.id, centerMHz: null,
                zoom: 1, zoomOffKHz: 0, recHl: hl, zoomBadge, satLine, satLineLbl,
                // dB spectrum state: ring of recent int16 dB*10 rows,
                // newest row, the SDR#-style control values, and the
                // time-range decimation state (pxCarry/accSum/accN;
                // rowTs feeds the row-rate estimate)
                dbRing: [], dbLatest: null, drawPending: 0,
                tRangeSec: WF_TIME_DEFAULT, pxCarry: 0,
                accSum: null, accN: 0, rowTs: [],
            };
            controls.replaceWith(buildWfControls(b, meta));
            drawDbScale(b);   // initial paint before the first row arrives
            wireWfInteractions(b, wrap, wfCanvas, meta);
            zoomBadge.onclick = ev => {
                ev.stopPropagation();
                b.zoom = 1; b.zoomOffKHz = 0;
                replayWaterfall(b);
                updateZoomBadge(b);
                if (b.syncZoomSlider) b.syncZoomSlider();
                updateFreqLabels(lastSharedMHz);
                if (wfOverlayOn) drawWfOverlay();
            };
            applyDongleCollapse(meta.id);
            if (wfOverlayOn) { overlay.style.display = 'block'; drawWfOverlay(); }
            return b;
        }

        // ---------- Per-dongle collapse (waterfall + sidebar card) ----------
        // One toggle on the waterfall head hides both the dongle's waterfall
        // block and its control card; state persists per dongle serial.
        function dongleCollapsed(serial) {
            try { return localStorage.getItem('dongle-collapsed-' + serial) === '1'; }
            catch (e) { return false; }
        }
        function applyDongleCollapse(serial) {
            const collapsed = dongleCollapsed(serial);
            const b = wfBlocks[serial];
            if (b) {
                b.block.classList.toggle('wf-block-collapsed', collapsed);
                b.collapseBtn.classList.toggle('collapsed', collapsed);
            }
        }
        function toggleDongleCollapse(serial) {
            try { localStorage.setItem('dongle-collapsed-' + serial, dongleCollapsed(serial) ? '0' : '1'); }
            catch (e) {}
            applyDongleCollapse(serial);
            if (wfOverlayOn) drawWfOverlay();
        }

        // ---------- Waterfall hover cursor + click-to-tune ----------
        // The whole wrap (waterfall + spectrum) shares one frequency axis:
        // the tuned target sits at the horizontal center, the display spans
        // the full capture (SDR_KHZ). Hovering shows a dotted line with the exact
        // frequency (and its offset from the center); a click tunes THIS
        // dongle there via /tune_dongle (Sync on its card rejoins tracking).
        function wireWfInteractions(b, wrap, wfCanvas, meta) {
            const hover = b.hover;
            let drag = null;   // {x0, x1} while the left button is held
            const freqAt = (clientX) => {
                const r = wfCanvas.getBoundingClientRect();
                const frac = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
                const span = wfSpanKhz(b);
                return { frac, mhz: b.centerMHz + ((b.zoomOffKHz || 0) + (frac - 0.5) * span) / 1000 };
            };
            const spanOf = (x0, x1) => {
                const a = freqAt(Math.min(x0, x1)), c = freqAt(Math.max(x0, x1));
                const fLo = Math.min(a.mhz, c.mhz), fHi = Math.max(a.mhz, c.mhz);
                return { fLo, fHi, center: (fLo + fHi) / 2, bwKhz: (fHi - fLo) * 1000 };
            };
            const drawHover = (clientX) => {
                const w = hover.clientWidth, h = hover.clientHeight;
                if (!w || !h) return;
                const dpr = window.devicePixelRatio || 1;
                if (hover.width !== Math.round(w * dpr) || hover.height !== Math.round(h * dpr)) {
                    hover.width = Math.round(w * dpr); hover.height = Math.round(h * dpr);
                }
                const ctx = hover.getContext('2d');
                ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
                ctx.clearRect(0, 0, w, h);
                if (drag && Math.abs(drag.x1 - drag.x0) >= 1) {
                    // drag in progress: translucent band between the edges,
                    // solid edge lines, live center/bandwidth label on top
                    const xa = Math.min(drag.x0, drag.x1), xb = Math.max(drag.x0, drag.x1);
                    const { frac: fa } = freqAt(xa), { frac: fb } = freqAt(xb);
                    const pa = fa * w, pb = fb * w;
                    ctx.fillStyle = 'rgba(83, 215, 105, 0.12)';
                    ctx.fillRect(pa, 0, pb - pa, h);
                    ctx.strokeStyle = 'rgba(83, 215, 105, 0.9)';
                    ctx.setLineDash([]);
                    for (const px of [pa, pb]) {
                        ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, h); ctx.stroke();
                    }
                    const s = spanOf(drag.x0, drag.x1);
                    // half the dragged width: the demod bandwidth is a
                    // HALF-width (low-pass cutoff, the band shows ±bw)
                    // so the label shows exactly what the drag will set
                    const bw = Math.max(0.5, Math.min(120, s.bwKhz / 2));
                    const label = `→ ${s.center.toFixed(4)} MHz ± ${bw.toFixed(1)} kHz`;
                    ctx.font = '12px monospace';
                    const tw = ctx.measureText(label).width;
                    let lx = (pa + pb) / 2 - tw / 2;
                    lx = Math.min(w - tw - 6, Math.max(6, lx));
                    ctx.fillStyle = 'rgba(0, 0, 0, 0.8)';
                    ctx.fillRect(lx - 3, 4, tw + 6, 17);
                    ctx.fillStyle = '#53d769';
                    ctx.fillText(label, lx, 17);
                    return;
                }
                const { frac, mhz } = freqAt(clientX);
                const x = frac * w;
                ctx.strokeStyle = 'rgba(224, 224, 224, 0.8)';
                ctx.setLineDash([3, 4]);
                ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
                ctx.setLineDash([]);
                const delta = (mhz - b.centerMHz) * 1000;
                let label = `${mhz.toFixed(4)} MHz (${delta >= 0 ? '+' : ''}${delta.toFixed(1)} kHz)`;
                if (b.dbLatest && b.dbLatest.db) {
                    const sBin = wfSrcBin(b)(Math.min(511, Math.max(0, Math.round(frac * WF_WIDTH))));
                    label += ` \u00b7 ${(b.dbLatest.db[sBin] / 10).toFixed(1)} dBFS`;
                }
                ctx.font = '12px monospace';
                const tw = ctx.measureText(label).width;
                let lx = x + 8;
                if (lx + tw + 6 > w) lx = x - tw - 12;
                ctx.fillStyle = 'rgba(0, 0, 0, 0.8)';
                ctx.fillRect(lx - 3, 4, tw + 6, 17);
                ctx.fillStyle = '#e0e0e0';
                ctx.fillText(label, lx, 17);
            };
            // Click tunes this dongle to the clicked frequency; drag spans
            // a band whose CENTER becomes the tune frequency and whose WIDTH
            // becomes the recorded (demod) bandwidth — one request, f + bw.
            const applyTune = (center, bwKhz) => {
                // bwKhz is the FULL dragged span; the demod bandwidth is
                // a HALF-width (cutoff) - sending the span whole made the
                // recorded band twice the selection
                const bw = bwKhz ? Math.max(1, Math.min(120, bwKhz / 2)) : 0;
                fetch('/tune_dongle?d=' + encodeURIComponent(b.serial)
                        + '&f=' + center.toFixed(4) + (bw ? '&bw=' + bw.toFixed(1) : ''))
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            // optimistic: the band geometry shows immediately
                            const c = dongleCards[b.serial];
                            if (c && c.lastD) {
                                c.lastD.manual_frequency_mhz = center;
                                if (bw) c.lastD.manual_bw_khz = bw;
                            }
                            if (c && c.bwInput && bw) {
                                c.bwInput.value = String(Math.round(bw * 10) / 10);
                                c.bwInput._typedAt = 0;
                            }
                            b.centerMHz = center;
                            b.zoomOffKHz = 0;   // the view recenters on the new tune
                            if (wfOverlayOn) drawWfOverlay();
                            toast(bw
                                ? `Dongle ${meta.index}${meta.primary ? ' ★' : ''} → ${center.toFixed(4)} MHz, records ±${bw.toFixed(1)} kHz — Sync on its card to rejoin`
                                : `Tuned dongle ${meta.index}${meta.primary ? ' ★' : ''} to ${center.toFixed(4)} MHz — Sync on its card to rejoin`);
                        } else {
                            toast(`Tune failed: ${data.error || 'unknown error'}`, true);
                        }
                    })
                    .catch(() => toast('Tune failed: receiver unreachable', true));
            };
            // Ctrl+wheel (or trackpad pinch) zooms this waterfall, anchored
            // at the pointer; plain wheel still scrolls the page
            wrap.addEventListener('wheel', e => {
                if (!e.ctrlKey || b.centerMHz === null) return;
                e.preventDefault();
                const r = wfCanvas.getBoundingClientRect();
                const frac = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
                const oldZoom = b.zoom || 1;
                let newZoom = oldZoom * (e.deltaY < 0 ? 1.18 : 1 / 1.18);
                if (newZoom < 1.05) newZoom = 1;
                newZoom = Math.min(32, newZoom);
                if (Math.abs(newZoom - oldZoom) < 0.001) return;   // at the floor/ceiling: nothing to redraw
                const cursorKhz = (b.zoomOffKHz || 0) + (frac - 0.5) * wfSpanKhz(b);
                const newSpan = SDR_KHZ / newZoom;
                let newOff = newZoom === 1 ? 0 : cursorKhz - (frac - 0.5) * newSpan;
                const maxOff = Math.max(0, SDR_KHZ / 2 - newSpan / 2);
                newOff = Math.min(maxOff, Math.max(-maxOff, newOff));
                b.zoom = newZoom;
                b.zoomOffKHz = newOff;
                replayWaterfall(b);
                updateZoomBadge(b);
                if (b.syncZoomSlider) b.syncZoomSlider();
                updateFreqLabels(lastSharedMHz);
                if (wfOverlayOn) drawWfOverlay();
            }, { passive: false });
            wrap.addEventListener('mousedown', e => {
                if (b.centerMHz === null || e.button !== 0) return;
                drag = { x0: e.clientX, x1: e.clientX };
                e.preventDefault();
            });
            wrap.addEventListener('mousemove', e => {
                if (b.centerMHz === null) return;
                if (drag) drag.x1 = e.clientX;   // band follows the pointer
                drawHover(e.clientX);
            });
            wrap.addEventListener('mouseleave', () => {
                drag = null;
                hover.getContext('2d').clearRect(0, 0, hover.width, hover.height);
            });
            wrap.addEventListener('mouseup', e => {
                if (b.centerMHz === null || !drag) return;
                const x0 = drag.x0, x1 = drag.x1;
                drag = null;
                if (Math.abs(x1 - x0) < 5) {
                    applyTune(freqAt(x1).mhz, 0);   // plain click: retune only
                    return;
                }
                const { center, bwKhz } = spanOf(x0, x1);
                applyTune(center, bwKhz);
            });
            wrap.title = 'Click to tune this dongle; drag to set frequency (center) and recorded bandwidth (width)';
        }

        function dongleModeChanged() {
            const usb = document.getElementById('add-dongle-mode').value === 'usb';
            document.getElementById('add-dongle-host').style.display = usb ? 'none' : '';
            document.getElementById('add-dongle-port').style.display = usb ? 'none' : '';
            document.getElementById('add-dongle-usb').style.display = usb ? '' : 'none';
            document.getElementById('add-dongle-refresh').style.display = usb ? '' : 'none';
            if (usb && document.getElementById('add-dongle-usb').options.length <= 1) fetchUsbDongles(false);
        }

        async function fetchUsbDongles(force) {
            const sel = document.getElementById('add-dongle-usb');
            try {
                const res = await fetch('/usb_dongles.json');
                const data = await res.json();
                const dongles = data.dongles || [];
                const prev = sel.value;
                sel.innerHTML = '';
                if (!dongles.length) {
                    sel.innerHTML = '<option value="">no USB dongles found</option>';
                } else {
                    for (const u of dongles) {
                        const o = document.createElement('option');
                        o.value = u.index;
                        o.textContent = u.index + ' · ' + (u.name || '?')
                                        + (u.serial ? ' (SN ' + u.serial + ')' : '');
                        sel.appendChild(o);
                    }
                    if ([...sel.options].some(o => o.value === prev)) sel.value = prev;
                }
                if (force) toast(dongles.length + ' USB dongle(s) found');
            } catch (e) {
                sel.innerHTML = '<option value="">enumeration failed</option>';
            }
        }

        async function addDongle() {
            const mode = document.getElementById('add-dongle-mode').value;
            let q;
            if (mode === 'usb') {
                const idx = document.getElementById('add-dongle-usb').value;
                if (idx === '') { toast('Pick a USB dongle (refresh re-checks the ports)', true); return; }
                q = 'host=usb&port=' + encodeURIComponent(idx);
            } else {
                const host = document.getElementById('add-dongle-host').value.trim();
                const port = document.getElementById('add-dongle-port').value.trim();
                if (!host || !port) { toast('Enter the rtl_tcp server IP and port (e.g. 192.168.3.65:1234)', true); return; }
                q = 'host=' + encodeURIComponent(host) + '&port=' + encodeURIComponent(port);
            }
            try {
                const res = await fetch('/add_dongle?' + q);
                const data = await res.json();
                if (data.success) {
                    document.getElementById('add-dongle-host').value = '';
                    document.getElementById('add-dongle-port').value = '';
                    toast(data.created ? ('Dongle ' + data.id + ' added — connecting...') : ('Dongle ' + data.id + ' is already registered'));
                } else {
                    toast('Add failed: ' + (data.error || 'unknown error'), true);
                }
            } catch (e) { toast('Add failed: receiver unreachable', true); }
        }
        async function removeDongle(id) {
            const usb = id.startsWith('usb:');
            if (!confirm('Remove dongle ' + id + ' from the receiver? ' + (usb ? '(Its rtl_sdr reader is stopped too — re-add the index anytime.)' : '(The rtl_tcp daemon keeps running — re-add the address anytime.)'))) return;
            try {
                const res = await fetch('/remove_dongle?d=' + encodeURIComponent(id));
                const data = await res.json();
                if (data.success) toast('Dongle ' + id + ' removed');
                else toast('Remove failed: unknown dongle', true);
            } catch (e) { toast('Remove failed: receiver unreachable', true); }
        }

        function toast(msg, isError) {
            const t = document.getElementById('toast');
            t.textContent = msg;
            t.className = 'show' + (isError ? ' error' : '');
            clearTimeout(toast._timer);
            toast._timer = setTimeout(() => { t.className = ''; }, 4000);
        }

        function magnitudeToColor(m) {
            m = Math.max(0, Math.min(255, m));
            const t = m / 255;
            let r, g, b;
            if (t < 0.2) { r = 0; g = 0; b = t * 5 * 255; }
            else if (t < 0.4) { r = 0; g = (t-0.2)*5*255; b = 255-(t-0.2)*5*255; }
            else if (t < 0.6) { r = (t-0.4)*5*255; g = 255; b = 0; }
            else if (t < 0.8) { r = 255; g = 255-(t-0.6)*5*128; b = 0; }
            else { r = 255; g = 255-(t-0.8)*5*255; b = (t-0.8)*5*255; }
            return [Math.round(r), Math.round(g), Math.round(b)];
        }

        // dB -> 0..255 palette level with the SDR#-style controls:
        // Offset = the dB level at palette bottom... see the sliders in the
        // block's right sidebar. db10 = dB*10 (int16 off the wire).
        function dbToLevel(b, db10) {
            let t = ((db10 / 10) - b.dbOffset) / b.dbRange;
            t = t < 0 ? 0 : (t > 1 ? 1 : t);
            if (b.dbContrast !== 1) t = Math.pow(t, b.dbContrast);
            return t * 255;
        }
        function mapDbRow(b, dbRow) {
            const out = new Uint8ClampedArray(512);
            for (let i = 0; i < 512; i++) out[i] = dbToLevel(b, dbRow[i]);
            return out;
        }
        // Fallback (polling) rows are per-row normalized 0..255 with no
        // absolute level. Converting them to a FIXED pseudo-dB scale
        // (0..255 -> -130..-30 dBFS) gives every control real meaning on
        // the degraded path - the previous mapping defined the pseudo-dB
        // window RELATIVE to the controls, which made Offset (and Range)
        // algebraically cancel themselves. The absolute anchors are a
        // fabrication, but a consistent one: peaks read ~-30 dBFS.
        function normToDb(norm) {
            const out = new Int16Array(512);
            for (let i = 0; i < 512; i++)
                out[i] = Math.round(((norm[i] / 255) * 100 - 130) * 10);
            return out;
        }
        // srcBin: the visible slice of the 512-bin row is stretched across
        // the canvas (nearest bin); at 1x this is the identity mapping
        function wfSrcBin(b) {
            const zoom = b.zoom || 1;
            if (zoom <= 1.01) return x => x;
            const offKhz = b.zoomOffKHz || 0;
            const binKhz = SDR_KHZ / 512;
            // window centered on 0 kHz = bin 256, not bin 0: without
            // the center the left half of a zoomed canvas clamps to
            // bin 0 and only half the diagram shows
            const zbin0 = 256 + (offKhz - wfSpanKhz(b) / 2) / binKhz;
            const zspan = wfSpanKhz(b) / binKhz;
            return x => {
                const s = Math.floor(zbin0 + (x / WF_WIDTH) * zspan);
                return s < 0 ? 0 : (s >= 512 ? 511 : s);
            };
        }
        function drawWaterfall(b, mappedRow, dbRow) {
            if (!mappedRow) return;
            // scroll one row down via the offscreen buffer: still all
            // GPU-side (no getImageData/putImageData readback), but
            // never self-referential — drawing a canvas onto itself
            // rasterizes with stale-tile seams on some GPU stacks
            // (fixed flickering lines that the backing store never
            // shows)
            b.wfBufCtx.drawImage(b.wfCtx.canvas, 0, 0);
            b.wfCtx.drawImage(b.wfBufCtx.canvas, 0, 1);
            const srcBin = wfSrcBin(b);
            for (let x = 0; x < WF_WIDTH; x++) {
                const [r, g, b2] = magnitudeToColor(mappedRow[srcBin(x)]);
                b.wfCtx.fillStyle = `rgb(${r},${g},${b2})`;
                b.wfCtx.fillRect(x, 0, 1, 1);
            }
            // Time gutter: scroll in lockstep; timestamp by wall clock (the
            // row rate varies with the capture rate / feed rate)
            if (b.timeCtx) {
                b.timeBufCtx.drawImage(b.timeCtx.canvas, 0, 0);
                b.timeCtx.drawImage(b.timeBufCtx.canvas, 0, 1);
                const now = Date.now();
                if (now - (b.lastTickTs || 0) > wfTickSec(b) * 1000) {
                    b.lastTickTs = now;
                    b.timeCtx.fillStyle = '#a8b8d8';
                    b.timeCtx.font = '11px monospace';
                    b.timeCtx.textBaseline = 'top';
                    b.timeCtx.fillText(new Date().toTimeString().slice(0, 8), 2, 1);
                }
            }
            drawSpectrum(b, dbRow, mappedRow, srcBin);
        }
        // dB scale gutter left of the spectrum pane (mirrors the time
        // gutter left of the waterfall): the dB numbers moved out of the
        // diagram onto it; redrawn only when Offset/Range change
        function drawDbScale(b) {
            if (!b.dbScaleCtx) return;
            const key = b.dbOffset + '/' + b.dbRange;
            if (b.dbScaleKey === key) return;
            b.dbScaleKey = key;
            const ctx = b.dbScaleCtx;
            ctx.fillStyle = '#000';
            ctx.fillRect(0, 0, 54, SPEC_HEIGHT);
            const top = b.dbOffset + b.dbRange / 2, bot = b.dbOffset - b.dbRange / 2;
            ctx.font = '9px monospace';
            ctx.textBaseline = 'middle';
            for (let db = Math.ceil(bot / 20) * 20; db <= top; db += 20) {
                const y = (top - db) / (top - bot) * SPEC_HEIGHT;
                // number + tick, right-aligned against the spectrum pane
                ctx.fillStyle = '#5c7290';
                ctx.textAlign = 'right';
                ctx.fillText(String(db), 47, Math.max(5, Math.min(SPEC_HEIGHT - 5, y)));
                ctx.fillRect(50, Math.round(y), 3, 1);
            }
            // unit, rotated along the left edge
            ctx.save();
            ctx.translate(6, SPEC_HEIGHT / 2);
            ctx.rotate(-Math.PI / 2);
            ctx.textAlign = 'center';
            ctx.fillStyle = '#5c7290';
            ctx.fillText('dBFS', 0, 0);
            ctx.restore();
        }

        // SDR#-style spectrum pane: dBFS Y axis scaled by the same
        // offset/range view as the waterfall palette, grid + trace + the
        // green tuning line at the tracked center
        function drawSpectrum(b, dbRow, mappedRow, srcBin) {
            const ctx = b.specCtx;
            ctx.fillStyle = '#000';
            ctx.fillRect(0, 0, WF_WIDTH, SPEC_HEIGHT);
            const top = b.dbOffset + b.dbRange / 2, bot = b.dbOffset - b.dbRange / 2;
            const dbToY = db => (top - db) / (top - bot) * SPEC_HEIGHT;
            drawDbScale(b);
            // grid lines only — the dB numbers live on the left scale
            for (let db = Math.ceil(bot / 20) * 20; db <= top; db += 20) {
                const y = dbToY(db);
                ctx.strokeStyle = 'rgba(15, 52, 96, 0.9)';
                ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(WF_WIDTH, y); ctx.stroke();
            }
            // trace (the palette color, so the pane reads at a glance)
            ctx.strokeStyle = '#53d769';
            ctx.lineWidth = 1;
            ctx.beginPath();
            for (let x = 0; x < WF_WIDTH; x++) {
                const db = dbRow[srcBin(x)] / 10;
                const y = Math.max(0, Math.min(SPEC_HEIGHT - 1, dbToY(db)));
                if (x === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            }
            ctx.stroke();
            // tuning line: the tracked center (kHz 0 in the rotated frame)
            const cx = wfKhzToPct(b, 0) / 100 * WF_WIDTH;
            if (cx >= 0 && cx <= WF_WIDTH) {
                ctx.strokeStyle = '#53d769';
                ctx.setLineDash([4, 3]);
                ctx.beginPath(); ctx.moveTo(cx, 0); ctx.lineTo(cx, SPEC_HEIGHT); ctx.stroke();
                ctx.setLineDash([]);
            }
        }

        // Row-rate estimate: the WebSocket pushes ~60 rows/s, the polling
        // fallback far less — the time-range decimation needs the actual
        // rate. Rolling window over the last 240 row timestamps
        // (= 4 s at the WebSocket rate)
        function estRowsPerSec(b) {
            const t = b.rowTs;
            if (t.length < 12) return 60;
            const dt = (t[t.length - 1] - t[0]) / 1000;
            if (dt < 0.5) return 60;
            return (t.length - 1) / dt;
        }
        // Time-gutter tick step: a nice round spacing that yields
        // roughly 5 labels across the currently shown range
        function wfTickSec(b) {
            const want = (b.tRangeSec || WF_TIME_DEFAULT) / 5;
            for (const sec of [1, 2, 5, 10, 15, 30, 60, 120, 300, 600])
                if (sec >= want) return sec;
            return 600;
        }

        // ---------- WebSocket dB row push (/waterfall.ws) ----------
        // One socket for all dongles; binary frames
        // [type=1][u16be id_len][id utf8][512 x int16le dB*10]. The client
        // sets its own row rate with a text frame ('rate=60'). Whenever the
        // socket is not open, fetchWaterfall falls back to the
        // /waterfall.json polling (normalized rows -> normToDb).
        // One retained INPUT row: { db: Int16Array | null, norm: array | null }
        // - dB rows off the WebSocket, normalized rows off the polling
        // fallback. The ring keeps the raw input (NOT the mapped palette
        // levels), so every control or zoom change can re-map the whole
        // retained history via replayWaterfall - on both feed paths.
        function wfRowIn(b, entry) {
            if (!entry.db && entry.norm) entry.db = normToDb(entry.norm);
            b.rowTs.push(performance.now());
            if (b.rowTs.length > 240) b.rowTs.shift();
            b.dbRing.push(entry);
            if (b.dbRing.length > DB_RING_CAP) b.dbRing.shift();
            b.dbLatest = entry;
            // Time-range decimation: the canvas height is fixed and the
            // Time slider sets the covered span, so every incoming row
            // advances pxPerRow pixel rows — rows landing in the same
            // pixel are averaged into it. More range = coarser lines,
            // never a taller canvas.
            if (!b.accSum) b.accSum = new Float64Array(512);
            for (let i = 0; i < 512; i++) b.accSum[i] += entry.db[i];
            b.accN++;
            b.pxCarry += WF_HEIGHT / Math.max(1, estRowsPerSec(b) * (b.tRangeSec || WF_TIME_DEFAULT));
            if (!b.drawPending) {
                b.drawPending = requestAnimationFrame(() => {
                    b.drawPending = 0;
                    let n = Math.floor(b.pxCarry);
                    if (n <= 0) return;
                    b.pxCarry -= n;
                    if (n > WF_HEIGHT) n = WF_HEIGHT;
                    const mean = new Int16Array(512);
                    for (let i = 0; i < 512; i++) mean[i] = Math.round(b.accSum[i] / b.accN);
                    b.accSum.fill(0); b.accN = 0;
                    const mapped = mapDbRow(b, mean);
                    for (let k = 0; k < n; k++) drawWaterfall(b, mapped, mean);
                });
            }
        }
        let wfSocket = null, wfWsBackoff = 5000;
        function wfWsLive() { return wfSocket && wfSocket.readyState === 1; }
        function connectWaterfallWS() {
            try {
                const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
                const ws = new WebSocket(proto + location.host + '/waterfall.ws');
                wfSocket = ws;
                ws.binaryType = 'arraybuffer';
                // one open handler (the previous duplicate had silently
                // disabled the backoff reset): request the row rate and
                // subscribe to the JSON topics — every periodic fetch
                // below goes silent while the socket is live
                ws.onopen = () => {
                    wfWsBackoff = 5000;   // reset after a good run
                    try {
                        ws.send('rate=60');
                        ws.send('sub=dongles,status,passes,track,ais,orbcomm,aprs,power,recordings');
                    } catch (e) {}
                };
                ws.onmessage = ev => {
                    if (typeof ev.data === 'string') return;
                    const u = new Uint8Array(ev.data);
                    if (u.length < 4) return;
                    const nlen = (u[1] << 8) | u[2];
                    if (u[0] === 2) {
                        // JSON topic push: [type=2][u16be topic_len][topic][json]
                        if (u.length < 3 + nlen) return;
                        const topic = new TextDecoder().decode(u.subarray(3, 3 + nlen));
                        const apply = WS_TOPICS[topic];
                        if (!apply) return;
                        try {
                            apply(JSON.parse(new TextDecoder().decode(u.subarray(3 + nlen))));
                        } catch (e) { console.error('WS topic ' + topic + ' failed:', e); }
                        return;
                    }
                    if (u[0] === 3) {
                        // AIS frame mark: a decoded frame on this dongle -
                        // bake a small red box at the channel frequency
                        // into the canvas TOP, where it scrolls down with
                        // the rows exactly like the signal itself. Baked
                        // pixels do not survive a zoom/contrast replay.
                        if (u.length < 3 + nlen + 2) return;
                        const did = new TextDecoder().decode(u.subarray(3, 3 + nlen));
                        const b = wfBlocks[did];
                        if (!b) return;
                        const mk = new Int16Array(ev.data.slice(3 + nlen))[0] / 10;
                        // the box spans the 25 kHz AIS channel
                        const x1 = wfKhzToPct(b, mk - 12) / 100 * WF_WIDTH;
                        const x2 = wfKhzToPct(b, mk + 12) / 100 * WF_WIDTH;
                        const xa = Math.min(x1, x2);
                        const w = Math.max(2, Math.abs(x2 - x1));
                        const x0 = Math.max(0, xa);
                        if (x0 >= WF_WIDTH || xa + w <= 0) return;
                        b.wfCtx.fillStyle = 'rgba(231, 76, 60, 0.95)';
                        b.wfCtx.fillRect(x0, 0,
                            Math.min(WF_WIDTH, xa + w) - x0, 3);
                        return;
                    }
                    if (u[0] !== 1) return;
                    if (u.length < 3 + nlen + 1024) return;
                    const did = new TextDecoder().decode(u.subarray(3, 3 + nlen));
                    const b = wfBlocks[did];
                    if (!b) return;
                    // slice(): the payload offset is byte-aligned, Int16Array
                    // needs an aligned buffer - the copy also detaches it
                    const row = new Int16Array(ev.data.slice(3 + nlen));
                    wfRowIn(b, { db: row, norm: null });
                };
                // back off 5 -> 10 -> 20 -> 40 -> 60 s while the socket
                // stays unavailable (e.g. nginx not proxying it yet -
                // browsers log one console error per attempt, a 5 s loop
                // was noisy); reset on the first successful open
                ws.onclose = () => {
                    wfSocket = null;
                    setTimeout(connectWaterfallWS, wfWsBackoff);
                    wfWsBackoff = Math.min(60000, wfWsBackoff * 2);
                };
                ws.onerror = () => { try { ws.close(); } catch (e) {} };
            } catch (e) {
                wfSocket = null;
                setTimeout(connectWaterfallWS, wfWsBackoff);
                wfWsBackoff = Math.min(60000, wfWsBackoff * 2);
            }
        }
        // redraw the retained dB history: zoom and dB control changes take
        // effect on the whole waterfall immediately (SDR# behaviour)
        function replayWaterfall(b) {
            b.wfCtx.fillStyle = '#000';
            b.wfCtx.fillRect(0, 0, WF_WIDTH, WF_HEIGHT);
            if (b.timeCtx) { b.timeCtx.fillStyle = '#000'; b.timeCtx.fillRect(0, 0, 54, WF_HEIGHT); }
            b.lastTickTs = 0;
            drawDbScale(b);
            // restart the live decimator from a clean slate
            if (b.accSum) b.accSum.fill(0);
            b.accN = 0; b.pxCarry = 0;
            // redraw the retained history decimated to the CURRENT time
            // range: rate x range rows (clamped to what the ring holds),
            // averaged exactly like the live path so old and new lines
            // end up with the same resolution
            const want = Math.max(1, Math.round(estRowsPerSec(b) * (b.tRangeSec || WF_TIME_DEFAULT)));
            const rows = b.dbRing.slice(-want);
            const pxPerRow = WF_HEIGHT / Math.max(1, rows.length);
            const sum = new Float64Array(512);
            let n = 0, carry = 0;
            for (const e of rows) {
                for (let i = 0; i < 512; i++) sum[i] += e.db[i];
                n++;
                carry += pxPerRow;
                let k = Math.floor(carry);
                if (k > 0) {
                    carry -= k;
                    const mean = new Int16Array(512);
                    for (let i = 0; i < 512; i++) mean[i] = Math.round(sum[i] / n);
                    const mapped = mapDbRow(b, mean);
                    for (let j = 0; j < k; j++) drawWaterfall(b, mapped, mean);
                    sum.fill(0); n = 0;
                }
            }
        }

        function updateFreqLabels(freqMHz) {
            document.getElementById('wf-freq').textContent = freqMHz.toFixed(3) + ' MHz';
            // Per-block labels reflect each dongle's own center frequency
            // (manual overrides retune dongles independently)
            for (const idx in wfBlocks) {
                const b = wfBlocks[idx];
                if (!b.freqLabels) continue;
                const c = (b.centerMHz !== null && b.centerMHz !== undefined) ? b.centerMHz : freqMHz;
                const step = (SDR_KHZ / 1000 / (b.zoom || 1)) / 4;
                const cView = c + (b.zoomOffKHz || 0) / 1000;
                const dec = step >= 0.01 ? 3 : 4;
                b.freqLabels.forEach((el, i) => {
                    el.textContent = (cView + (i - 2) * step).toFixed(dec);
                });
            }
        }

        let wfOverlayOn = false;
        function toggleWfOverlay() {
            wfOverlayOn = !wfOverlayOn;
            for (const idx in wfBlocks) {
                wfBlocks[idx].overlay.style.display = wfOverlayOn ? 'block' : 'none';
            }
            document.getElementById('wf-overlay-btn').textContent = wfOverlayOn ? '📐 Features ✓' : '📐 Features';
            if (wfOverlayOn) drawWfOverlay();
        }

        function wfOverlayLabel(ctx, x, y, text, color, align) {
            ctx.font = '12px monospace';
            ctx.textAlign = align || 'center';
            const tw = ctx.measureText(text).width;
            const bx = align === 'right' ? x - tw : (align === 'left' ? x : x - tw / 2);
            ctx.fillStyle = 'rgba(0, 0, 0, 0.75)';
            ctx.fillRect(bx - 3, y - 10, tw + 6, 14);
            ctx.fillStyle = color;
            ctx.fillText(text, x, y);
        }

        function drawWfOverlay() {
            // Per-dongle feature overlay: dongles can differ in tune
            // frequency (manual overrides) AND in demod (recorded)
            // bandwidth, so each block draws its own actual geometry
            for (const idx in wfBlocks) {
            const block = wfBlocks[idx];
            const canvas = block.overlay;
            const w = canvas.clientWidth, h = canvas.clientHeight;
            if (w === 0 || h === 0) continue;
            const dpr = window.devicePixelRatio || 1;
            canvas.width = Math.round(w * dpr);
            canvas.height = Math.round(h * dpr);
            const ctx = canvas.getContext('2d');
            ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            ctx.clearRect(0, 0, w, h);

            // x position of a frequency offset in kHz relative to the
            // target center — zoom-aware (the visible window is
            // [off - span/2, off + span/2] around the target center)
            const spanHalf = wfSpanKhz(block) / 2, vOff = block.zoomOffKHz || 0;
            const x = khz => w / 2 + ((khz - vOff) / spanHalf) * (w / 2);
            const inView = k => k >= vOff - spanHalf - 0.5 && k <= vOff + spanHalf + 0.5;

            // Actually recorded (demod) bandwidth of THIS dongle: the
            // manual override if set, else the mode default (full ±120 kHz
            // on broadcast-FM tunes, 22 kHz on satellite bands)
            const dmeta = (dongleCards[idx] && dongleCards[idx].lastD) || null;
            let bwKhz;
            if (dmeta && dmeta.manual_bw_khz !== null && dmeta.manual_bw_khz !== undefined) {
                bwKhz = dmeta.manual_bw_khz;
            } else {
                const c = (block.centerMHz !== null && block.centerMHz !== undefined) ? block.centerMHz : lastSharedMHz;
                bwKhz = autoDemodKhz(c);
            }

            // Expected signal band at the target center: ±17.5 kHz FM
            // deviation on the ISS SSTV downlink; the Meteor LRPT stream
            // is wide OQPSK (~±60 kHz)
            const lrpt = !!(satTrackInfo && /meteor/i.test(satTrackInfo.name || ''));
            const sigHalf = lrpt ? 60 : 17.5;
            const sigName = lrpt ? 'LRPT signal \u00b160 kHz' : 'SSTV/FM signal \u00b117.5 kHz';
            ctx.fillStyle = 'rgba(83, 215, 105, 0.08)';
            ctx.fillRect(x(-sigHalf), 0, x(sigHalf) - x(-sigHalf), h);
            ctx.strokeStyle = 'rgba(83, 215, 105, 0.5)';
            ctx.setLineDash([]);
            for (const k of [-sigHalf, sigHalf]) {
                ctx.beginPath(); ctx.moveTo(x(k), 0); ctx.lineTo(x(k), h); ctx.stroke();
            }

            // Recorded (demod) passband edges at the ACTUAL bandwidth
            ctx.strokeStyle = 'rgba(243, 156, 18, 0.8)';
            if (bwKhz < 120) {
                ctx.setLineDash([5, 4]);
                for (const k of [-bwKhz, bwKhz]) {
                    ctx.beginPath(); ctx.moveTo(x(k), 0); ctx.lineTo(x(k), h); ctx.stroke();
                }
                ctx.setLineDash([]);
            }

            // Signal-strength measurement band: center 20 FFT bins ≈ ±4.7 kHz
            ctx.strokeStyle = 'rgba(46, 204, 113, 0.9)';
            for (const k of [-4.7, 4.7]) {
                ctx.beginPath(); ctx.moveTo(x(k), h - 46); ctx.lineTo(x(k), h - 26); ctx.stroke();
            }

            // DC spike: dongle is tuned +60 kHz off, so the spike sits at
            // +60 kHz — the middle of the right half of the display
            ctx.strokeStyle = 'rgba(231, 76, 60, 0.9)';
            ctx.setLineDash([2, 3]);
            ctx.beginPath(); ctx.moveTo(x(60), 0); ctx.lineTo(x(60), h); ctx.stroke();
            ctx.setLineDash([]);

            // kHz axis relative to the target center, along the bottom
            ctx.font = '12px monospace';
            ctx.textAlign = 'center';
            ctx.fillStyle = '#888';
            // axis ticks adapt to the zoom window
            const stepKhz = spanHalf <= 4 ? 1 : spanHalf <= 8 ? 2.5 : spanHalf <= 15 ? 5
                          : spanHalf <= 30 ? 10 : spanHalf <= 60 ? 30 : 60;
            const t0 = Math.ceil((vOff - spanHalf) / stepKhz) * stepKhz;
            for (let k = t0; k <= vOff + spanHalf + 0.01; k += stepKhz) {
                ctx.fillText((k > 0 ? '+' : '') + (Math.round(k * 10) / 10), x(k), h - 4);
            }

            // Labels (only what is inside the visible window)
            if (inView(0)) wfOverlayLabel(ctx, x(0), 44, sigName, '#53d769');
            if (bwKhz < 120) {
                if (inView(-bwKhz)) wfOverlayLabel(ctx, x(-bwKhz) + 6, 16, '◄ recorded ±' + bwKhz + ' kHz', '#f39c12', 'left');
                if (inView(bwKhz)) wfOverlayLabel(ctx, x(bwKhz) - 6, 16, 'recorded ±' + bwKhz + ' kHz ►', '#f39c12', 'right');
            } else {
                if (inView(0)) wfOverlayLabel(ctx, x(0), 16, 'recorded: full ±120 kHz band', '#f39c12');
            }
            if (inView(0)) wfOverlayLabel(ctx, x(0), h - 30, 'signal meter ±5 kHz', '#2ecc71');
            if (inView(60)) wfOverlayLabel(ctx, x(60), h - 12, 'DC spike +60 kHz', '#e74c3c');
            }
        }

        window.addEventListener('resize', () => { if (wfOverlayOn) drawWfOverlay(); });

        async function fetchWaterfall() {
            try {
                applyDongles(await (await fetch('/dongles.json')).json());
            } catch(e) { /* fetch failed — cards keep last state */ }
        }

        // Dongle state onto the waterfall blocks + control cards —
        // shared by the /dongles.json fallback poll and the WS 'dongles'
        // topic push
        function applyDongles(dongles) {
            try {
                // prune blocks/cards of dongles removed at runtime
                for (const rid of Object.keys(wfBlocks)) {
                    if (!dongles.some(d => d.id === rid)) {
                        const rb = wfBlocks[rid];
                        if (rb.block && rb.block.parentNode) rb.block.parentNode.removeChild(rb.block);
                        delete wfBlocks[rid];
                        delete dongleCards[rid];
                    }
                }
                for (const d of dongles) {
                    const b = ensureWfBlock(d);
                    // Frequency axis center for hover/click: the dongle's
                    // manual override if set, else the shared tune frequency
                    // The AIS dongle is parked on 162.000 MHz (both ship
                    // channels at +/-25 kHz), the Orbcomm dongle on
                    // 137.5 MHz (one 2.4 MHz capture spans the whole
                    // 137.0-138.0 MHz downlink band), the APRS dongle on
                    // 144.8 MHz (145.825 with the ISS checkbox set) —
                    // none follow the shared satellite tune
                    b.centerMHz = d.ais ? 162.0 : (d.orbcomm ? 137.5 : (d.aprs ? (d.aprs_lasar ? 436.925 : (d.aprs_iss ? 145.825 : 144.8)) : (d.pocsag ? 466.0 : (d.adsb ? 1090.0 : ((d.manual_frequency_mhz !== null && d.manual_frequency_mhz !== undefined)
                        ? d.manual_frequency_mhz : lastSharedMHz)))));
                    updateDongleCard(d);
                }
                // the overlay follows per-dongle geometry (recorded
                // bandwidth, override frequencies) — redraw with the poll
                if (wfOverlayOn) drawWfOverlay();
            } catch(e) { /* bad data — cards keep last state */ }
        }

        // Polling-fallback row fetch (only while the WebSocket is down):
        // last=1 per dongle — the waterfall scrolls client-side, the full
        // buffer need not be transferred. Runs at 200 ms so the fallback
        // keeps its old ~5 rows/s rate without dragging dongles.json along.
        async function fetchWaterfallRows() {
            for (const idx in wfBlocks) {
                const b = wfBlocks[idx];
                if (b.centerMHz === null || b.centerMHz === undefined) continue;
                try {
                    const r = await fetch('/waterfall.json?d=' + encodeURIComponent(idx) + '&last=1');
                    const data = await r.json();
                    if (data.length > 0)
                        wfRowIn(b, { db: null, norm: data[data.length - 1] });
                } catch(e) {}
            }
        }

        // ---------- Per-dongle status cards (right of the waterfalls) ----------
        const dongleCards = {};      // serial -> card handles
        const MAX_CARD_POINTS = 300; // ~5 min of signal history at 1 Hz
        let lastSharedMHz = null;    // shared tune frequency (from status.json)
        // Satellite downlink marker target: the ACTIVE pass's frequency, or
        // the next upcoming one when idle (from status.json)
        let satTrackInfo = null;
        // Auto demod width for a dongle WITHOUT a manual override:
        // 120 kHz on broadcast FM tunes, else the 22 kHz satellite default
        function autoDemodKhz(centerMHz) {
            if (centerMHz !== null && centerMHz >= 87.5 && centerMHz <= 108) return 120;
            return 22;
        }
        let satRecHighlight = false;  // recording-band label shown (dedupe)

        // Effective MHz of a dongle: its manual override, else the shared one
        function curDongleMHz(serial) {
            const c = dongleCards[serial];
            if (!c || !c.lastD) return null;
            if (c.lastD.ais) return 162.0;   // AIS dongle never follows the shared tune
            if (c.lastD.orbcomm) return 137.5;   // Orbcomm dongle is parked on the band center
            if (c.lastD.aprs) return c.lastD.aprs_lasar ? 436.925 : (c.lastD.aprs_iss ? 145.825 : 144.8);     // APRS dongle is parked on the APRS channel (LASAR/ISS checkboxes: 436.925/145.825)
            if (c.lastD.manual_frequency_mhz !== null && c.lastD.manual_frequency_mhz !== undefined)
                return c.lastD.manual_frequency_mhz;
            return lastSharedMHz;
        }

        // A dedicated role (AIS / Orbcomm / APRS / POCSAG) re-shapes a
        // dongle card at runtime (the role is picked from the card's
        // dropdown, so both shapes must be reachable after the card was
        // built): the satellite-only controls (tuning, bandwidth, scan,
        // record, demod — the dedicated capture threads feed none of
        // them) hide, the dropdown follows the role, and a live-audio
        // stream is dropped for the roles without audio (APRS stays
        // listenable FM).
        function dongleRole(d) {
            if (d.ais) return 'ais';
            if (d.orbcomm) return 'orbcomm';
            if (d.aprs) return 'aprs';
            if (d.pocsag) return 'pocsag';
            if (d.adsb) return 'adsb';
            if (d.manual_frequency_mhz !== null && d.manual_frequency_mhz !== undefined) return 'manual';
            return 'sat';
        }
        function applyDongleRole(c, d) {
            const role = dongleRole(d);
            // Manual controls (tuning, bandwidth, scan, demod) live in
            // Manual mode only — the dedicated roles park the dongle on
            // their band and auto tracking needs no manual input
            const free = role === 'manual' ? '' : 'none';
            c.tuneRow.style.display = free;
            c.nudgeRow.style.display = free;
            c.bwRow.style.display = free;
            c.scanRow.style.display = free;
            c.scanRow2.style.display = free;
            c.secTune.style.display = free;
            c.modeRow.style.display = free;
            c.secMode.style.display = free;
            c.secBw.style.display = free;
            c.secScan.style.display = free;
            // Record (manual raw IQ) and Listen stay available while the
            // dongle is on satellites or in Manual — pass capture and
            // live audio are the point there; the decode roles hide both
            // (APRS keeps its listenable FM audio)
            c.recBtn.style.display = (role === 'sat' || role === 'manual') ? '' : 'none';
            c.listenBtn.style.display = (role === 'sat' || role === 'manual' || role === 'aprs') ? '' : 'none';
            // the ISS digipeater checkbox only exists in APRS mode
            c.aprsIssRow.style.display = role === 'aprs' ? 'flex' : 'none';
            c.aprsLasarRow.style.display = role === 'aprs' ? 'flex' : 'none';
            // the auto-join checkbox matters in the dedicated roles —
            // and stays visible while the dongle is auto-joined
            // mid-pass (role flags cleared, auto_sat still set), so it
            // can be switched off again
            const dedicated = role === 'ais' || role === 'orbcomm' || role === 'aprs' || role === 'pocsag' || role === 'adsb';
            c.autoSatRow.style.display = (dedicated || d.auto_sat) ? 'flex' : 'none';
            if (c.roleSel._last !== role) { c.roleSel._last = role; c.roleSel.value = role; }
            c.roleLast = role;
            if ((role === 'ais' || role === 'orbcomm' || role === 'pocsag' || role === 'adsb') && liveAudioOn === d.id) toggleLiveAudio(d.id);
        }

        function ensureDongleCard(d) {
            let c = dongleCards[d.id];
            if (c) return c;
            // The card lives inside its dongle block's holder (created by
            // ensureWfBlock) — waterfall and controls belong to one container
            const col = (wfBlocks[d.id] && wfBlocks[d.id].cardHolder) || document.getElementById('wf-list');
            const card = document.createElement('div');
            card.className = 'status-item';
            // flex:1 stretches the card to line up with its waterfall block;
            // the border visually separates the dongles
            card.style.cssText = 'flex:1;display:flex;flex-direction:column;border-bottom:1px solid #0f3460;margin-bottom:8px;';
            // Identity header: connection LED, dongle name (USB name +
            // serial, else the rtl_tcp address), the source this dongle is
            // fed from, and the live tuned frequency — the waterfall block
            // head above only carries the dongle number
            const idHead = document.createElement('div');
            idHead.style.cssText = 'display:flex;align-items:center;gap:6px;';
            const idLed = document.createElement('span');
            idLed.className = 'indicator yellow';
            idLed.title = 'green = capture streaming, red = disconnected';
            const idName = document.createElement('span');
            idName.style.cssText = 'font-weight:bold;color:#e0e0e0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
            idName.textContent = (d.usb_name
                ? `${d.usb_name}${d.usb_serial ? ' (SN ' + d.usb_serial + ')' : ''}`
                : d.host) + (d.primary ? ' ★' : '');
            idName.title = d.primary ? 'Primary dongle — live audio and pass recordings come from it'
                                     : 'Secondary dongle';
            idHead.appendChild(idLed); idHead.appendChild(idName);
            const srcLine = document.createElement('div');
            srcLine.style.cssText = 'font-size:0.75em;color:#888;margin-top:1px;';
            srcLine.textContent = d.id.startsWith('usb:')
                ? `🔌 USB device ${d.id.slice(4)} on the receiver host · rtl_sdr`
                : `📡 TCP ${d.label} · rtl_tcp`;
            const freqLine = document.createElement('div');
            freqLine.style.cssText = 'margin-top:5px;font-size:1.05em;font-weight:bold;color:#53d769;';
            freqLine.textContent = '—';
            freqLine.title = 'This dongle\'s effective frequency: its manual override if set, else the shared satellite tune';
            // Small section headers structure the card: identity → signal
            // → recording → tuning → bandwidth → scan
            const mkSec = (label) => {
                const s = document.createElement('div');
                s.style.cssText = 'font-size:0.7em;color:#666;text-transform:uppercase;letter-spacing:0.06em;margin:9px 0 3px 0;border-bottom:1px solid rgba(15,52,96,0.8);padding-bottom:1px;';
                s.textContent = label;
                return s;
            };
            // Band allocation of this dongle's effective frequency (its
            // manual override if set, else the shared tune) — orientation
            // for what the waterfall is currently looking at
            const bandTxt = document.createElement('div');
            bandTxt.style.cssText = 'font-size:0.75em;color:#a8b8d8;background:rgba(15,52,96,0.5);border:1px solid #0f3460;border-radius:3px;padding:3px 5px;line-height:1.5;';
            const sigRow = document.createElement('div');
            sigRow.style.cssText = 'display:flex;align-items:center;gap:6px;margin-top:5px;font-size:0.9em;';
            const sigVal = document.createElement('span');
            sigVal.style.cssText = 'color:#e0e0e0;min-width:44px;';
            const bar = document.createElement('div');
            bar.className = 'signal-bar';
            bar.style.flex = '1';
            const sigFill = document.createElement('div');
            sigFill.className = 'signal-fill';
            sigFill.style.width = '0%';
            bar.appendChild(sigFill);
            sigRow.appendChild(sigVal); sigRow.appendChild(bar);
            const tl = document.createElement('canvas');
            tl.width = 250; tl.height = 46;
            tl.style.cssText = 'width:100%;height:46px;background:#000;border:1px solid #0f3460;border-radius:4px;margin-top:5px;';
            const recRow = document.createElement('div');
            recRow.style.cssText = 'display:flex;align-items:center;gap:6px;margin-top:5px;font-size:0.85em;';
            const recLed = document.createElement('span');
            recLed.className = 'indicator red';
            const recTxt = document.createElement('span');
            recTxt.style.cssText = 'color:#a8b8d8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
            recTxt.textContent = 'Idle';
            recRow.appendChild(recLed); recRow.appendChild(recTxt);
            // Manual raw-IQ recording (/record_dongle): captures this
            // dongle's untouched baseband IQ (no audio) until stopped;
            // the finished analysis zip (raw IQ + description.json +
            // waterfall) downloads automatically. The recording line
            // above still shows the live pass WAV, if any.
            const recBtn = document.createElement('button');
            recBtn.textContent = '⏺ Record';
            recBtn.title = 'Record this dongle\'s raw baseband IQ — the analysis zip (raw IQ + description.json + waterfall) downloads when stopped';
            recBtn.style.cssText = 'flex:0 0 auto;margin-left:auto;padding:2px 8px;background:rgba(231,76,60,0.15);color:#e07890;border:1px solid #e07890;border-radius:3px;cursor:pointer;font-family:monospace;font-size:0.85em;';
            const sn_rec = d.id;
            recBtn.onclick = () => {
                const cur = !!(dongleCards[sn_rec] && dongleCards[sn_rec].lastD && dongleCards[sn_rec].lastD.manual_recording);
                fetch('/record_dongle?d=' + encodeURIComponent(sn_rec) + (cur ? '&stop=1' : ''))
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            toast(cur ? 'Stopping \u2014 finalizing the raw recording, the zip download starts in a moment'
                                      : `Recording raw IQ on dongle ${d.index}${d.primary ? ' \u2605' : ''} \u2014 stop it (or wait for the cap) and the analysis zip downloads`);
                        } else {
                            toast('Record failed: ' + (data.error || 'unknown error'), true);
                        }
                    })
                    .catch(() => toast('Record failed: receiver unreachable', true));
            };
            recRow.appendChild(recBtn);
            // Manual frequency override: 1 kHz steps (input + Tune), quick
            // nudges, and Sync to rejoin the shared frequency
            const tuneRow = document.createElement('div');
            tuneRow.style.cssText = 'display:flex;gap:4px;align-items:center;margin-top:6px;';
            const dinput = document.createElement('input');
            dinput.type = 'number'; dinput.step = 'any'; dinput.min = '24'; dinput.max = '1766';
            dinput.style.cssText = 'flex:1;min-width:0;background:#0f3460;border:1px solid #53d769;color:#e0e0e0;border-radius:3px;padding:2px 4px;font-family:monospace;font-size:0.9em;';
            const dTune = document.createElement('button');
            dTune.textContent = 'Tune';
            dTune.style.cssText = 'padding:2px 8px;background:#53d769;color:#1a1a2e;border:none;border-radius:3px;cursor:pointer;font-family:monospace;font-size:0.85em;';
            tuneRow.appendChild(dinput); tuneRow.appendChild(dTune);
            const nudgeRow = document.createElement('div');
            nudgeRow.style.cssText = 'display:flex;gap:4px;margin-top:4px;';
            const mkNudge = (label) => {
                const b = document.createElement('button');
                b.textContent = label;
                b.style.cssText = 'flex:1;padding:2px 0;background:#0f3460;color:#53d769;border:1px solid #53d769;border-radius:3px;cursor:pointer;font-family:monospace;font-size:0.85em;';
                return b;
            };
            const dMinus = mkNudge('−1 kHz'), dPlus = mkNudge('+1 kHz'), dSync = mkNudge('Sync');
            nudgeRow.appendChild(dMinus); nudgeRow.appendChild(dPlus); nudgeRow.appendChild(dSync);
            // Recorded (demod) bandwidth: the IQ low-pass cutoff ahead of
            // the FM demod — what ends up in the WAV and live audio. Auto =
            // mode default (full band on FM broadcast, 22 kHz on satellites)
            const bwRow = document.createElement('div');
            bwRow.style.cssText = 'display:flex;gap:4px;align-items:center;margin-top:4px;font-size:0.8em;';
            // Free-entry field: any 1-120 kHz value (Enter or Set applies),
            // empty = auto. Datalist offers the log-scale presets.
            const bwInput = document.createElement('input');
            bwInput.type = 'text'; bwInput.inputMode = 'decimal';
            bwInput.placeholder = 'auto';
            bwInput.style.cssText = 'flex:1;min-width:0;background:#0f3460;border:1px solid #53d769;color:#e0e0e0;border-radius:3px;padding:2px 4px;font-family:monospace;font-size:0.9em;text-align:center;';
            bwInput.setAttribute('list', 'bw-presets');
            const bwList = document.createElement('datalist');
            bwList.id = 'bw-presets';
            for (const v of [1, 2, 5, 10, 20, 50, 100, 120]) {
                const o = document.createElement('option');
                o.value = String(v); o.label = v + ' kHz';
                bwList.appendChild(o);
            }
            const bwSet = document.createElement('button');
            bwSet.textContent = 'Set';
            bwSet.style.cssText = 'flex:0 0 auto;padding:2px 8px;background:#53d769;color:#1a1a2e;border:none;border-radius:3px;cursor:pointer;font-family:monospace;font-size:0.85em;';
            const sn_bw = d.id;
            const doBwSet = () => {
                const raw = bwInput.value.trim().toLowerCase();
                const val = (raw === '' || raw === 'auto') ? 'auto' : raw;
                fetch('/tune_dongle?d=' + encodeURIComponent(sn_bw) + '&bw=' + encodeURIComponent(val))
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            bwInput._typedAt = 0;
                            // draw the new geometry immediately — the 200 ms
                            // poll would confirm it a moment later anyway
                            const c = dongleCards[sn_bw];
                            if (c && c.lastD) c.lastD.manual_bw_khz = val === 'auto' ? null : parseFloat(val);
                            if (wfOverlayOn) drawWfOverlay();
                            toast(val === 'auto'
                                ? `Demod bandwidth of dongle ${d.index}${d.primary ? ' ★' : ''} back to auto`
                                : `Dongle ${d.index}${d.primary ? ' ★' : ''} now records ±${val} kHz`);
                        } else {
                            toast(`Bandwidth change failed: ${data.error || 'unknown error'}`, true);
                        }
                    })
                    .catch(() => toast('Bandwidth change failed: receiver unreachable', true));
            };
            bwSet.onclick = doBwSet;
            bwInput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); doBwSet(); } });
            bwInput.addEventListener('input', () => { bwInput._typedAt = Date.now(); });
            // Fit: measure the signal on the current tune (live spectrum)
            // and size the recorded bandwidth to it, so the WAV/live audio
            // band is neither too wide (noise) nor too narrow (clips it)
            const bwFit = document.createElement('button');
            bwFit.textContent = 'Fit';
            bwFit.title = 'Measure the signal on the current tune and fit the recorded bandwidth to it';
            bwFit.style.cssText = 'flex:0 0 auto;padding:2px 8px;background:#0f3460;color:#53d769;border:1px solid #53d769;border-radius:3px;cursor:pointer;font-family:monospace;font-size:0.85em;';
            const sn_fit = d.id;
            bwFit.onclick = () => {
                bwFit.disabled = true;
                fetch('/fit_bw?d=' + encodeURIComponent(sn_fit))
                    .then(r => r.json())
                    .then(data => {
                        bwFit.disabled = false;
                        if (data.success) {
                            const c = dongleCards[sn_fit];
                            if (c && c.lastD) c.lastD.manual_bw_khz = data.bw_khz;
                            if (c && c.bwInput) c.bwInput._typedAt = 0;
                            if (wfOverlayOn) drawWfOverlay();
                            toast('Signal ' + data.width_khz + ' kHz wide \u2192 dongle records \u00b1' + data.bw_khz + ' kHz');
                        } else {
                            toast('Bandwidth fit failed: ' + (data.error || 'unknown error'), true);
                        }
                    })
                    .catch(() => { bwFit.disabled = false; toast('Bandwidth fit failed: receiver unreachable', true); });
            };
            bwRow.appendChild(bwInput); bwRow.appendChild(bwSet); bwRow.appendChild(bwFit); bwRow.appendChild(bwList);
            // Demodulator mode: what the capture thread demodulates into
            // the live audio and the WAV. Auto = FM everywhere except the
            // airband (118-137 MHz) which demodulates AM.
            const modeRow = document.createElement('div');
            modeRow.style.cssText = 'display:flex;gap:4px;align-items:center;margin-top:4px;font-size:0.85em;';
            const modeSel = document.createElement('select');
            modeSel.style.cssText = 'flex:1;min-width:0;background:#0f3460;border:1px solid #53d769;color:#e0e0e0;border-radius:3px;padding:2px 4px;font-family:monospace;font-size:0.9em;cursor:pointer;';
            modeSel.title = 'Demodulator for live audio and recordings';
            for (const [val, label] of [['auto', 'Auto'], ['fm', 'FM'], ['am', 'AM'],
                                        ['usb', 'USB'], ['lsb', 'LSB'], ['cw', 'CW']]) {
                const o = document.createElement('option');
                o.value = val; o.textContent = label;
                modeSel.appendChild(o);
            }
            const modeHint = document.createElement('span');
            modeHint.style.cssText = 'flex:0 0 auto;color:#888;font-size:0.9em;';
            modeRow.appendChild(modeSel); modeRow.appendChild(modeHint);
            const sn_mode = d.id;
            modeSel.addEventListener('change', () => {
                const v = modeSel.value;
                fetch('/tune_dongle?d=' + encodeURIComponent(sn_mode) + '&mode=' + v)
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            toast(v === 'auto' ? '🎙 Demod back to auto' : '🎙 Demod mode: ' + v.toUpperCase());
                        } else {
                            toast('Mode change failed: ' + (data.error || 'unknown error'), true);
                        }
                    })
                    .catch(() => toast('Mode change failed: receiver unreachable', true));
            });
            // Frequency scan: sweep this dongle start\u2192end and park it
            // on the next strong signal (/scan_dongle; see scan.py). The
            // button doubles as Stop while a scan runs; the status line
            // below shows live progress from dongles.json.
            const scanRow = document.createElement('div');
            scanRow.style.cssText = 'display:flex;gap:4px;align-items:center;margin-top:6px;';
            const mkScanInput = () => {
                const i = document.createElement('input');
                i.type = 'number'; i.step = 'any'; i.min = '24'; i.max = '1766';
                i.style.cssText = 'flex:1;min-width:0;background:#0f3460;border:1px solid #53d769;color:#e0e0e0;border-radius:3px;padding:2px 4px;font-family:monospace;font-size:0.85em;';
                i.addEventListener('input', () => { i._typedAt = Date.now(); });
                return i;
            };
            const scanFrom = mkScanInput(); scanFrom.title = 'scan start (MHz)';
            const scanTo = mkScanInput(); scanTo.title = 'scan end (MHz)';
            const scanArrow = document.createElement('span');
            scanArrow.textContent = '\u2192';
            scanArrow.style.cssText = 'color:#888;flex:0 0 auto;';
            const scanBtn = document.createElement('button');
            scanBtn.textContent = '\uD83D\uDD0D Scan';
            scanBtn.title = 'Sweep this dongle and stop on the next strong signal';
            scanBtn.style.cssText = 'flex:0 0 auto;padding:2px 8px;background:#53d769;color:#1a1a2e;border:none;border-radius:3px;cursor:pointer;font-family:monospace;font-size:0.85em;';
            scanRow.appendChild(scanFrom); scanRow.appendChild(scanArrow); scanRow.appendChild(scanTo); scanRow.appendChild(scanBtn);
            const scanRow2 = document.createElement('div');
            scanRow2.style.cssText = 'display:flex;gap:4px;align-items:center;margin-top:4px;font-size:0.8em;';
            const scanStep = document.createElement('input');
            scanStep.type = 'text'; scanStep.inputMode = 'decimal';
            scanStep.placeholder = '200';
            scanStep.title = 'step width in kHz (20-400)';
            scanStep.style.cssText = 'flex:0 0 40px;min-width:0;background:#0f3460;border:1px solid #53d769;color:#e0e0e0;border-radius:3px;padding:2px 4px;font-family:monospace;font-size:0.9em;text-align:center;';
            scanStep.addEventListener('input', () => { scanStep._typedAt = Date.now(); });
            const scanStepLbl = document.createElement('span');
            scanStepLbl.textContent = 'kHz step';
            scanStepLbl.style.cssText = 'color:#888;flex:0 0 auto;';
            const scanTxt = document.createElement('span');
            scanTxt.style.cssText = 'flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#a8b8d8;';
            scanRow2.appendChild(scanStep); scanRow2.appendChild(scanStepLbl); scanRow2.appendChild(scanTxt);
            const sn_scan = d.id;
            const doScanToggle = () => {
                const cur = (dongleCards[sn_scan] && dongleCards[sn_scan].lastD && dongleCards[sn_scan].lastD.scan) || null;
                if (cur && cur.active) {
                    fetch('/scan_dongle?d=' + encodeURIComponent(sn_scan) + '&stop=1')
                        .then(r => r.json())
                        .then(data => { if (data.success) toast('Scan stopping \u2014 dongle stays parked at the current frequency (Sync rejoins tracking)'); })
                        .catch(() => toast('Scan stop failed: receiver unreachable', true));
                    return;
                }
                const from = parseFloat(scanFrom.value), to = parseFloat(scanTo.value);
                if (isNaN(from) || isNaN(to)) { toast('Enter scan start and end frequencies in MHz (24-1766)', true); return; }
                if (from === to) { toast('Scan start and end must differ', true); return; }
                const step = parseFloat(scanStep.value) || 200;
                fetch('/scan_dongle?d=' + encodeURIComponent(sn_scan)
                        + '&start=' + encodeURIComponent(from) + '&end=' + encodeURIComponent(to)
                        + '&step=' + encodeURIComponent(step))
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            toast('Scanning dongle ' + d.index + (d.primary ? ' \u2605' : '') + ': ' + from + ' \u2192 ' + to + ' MHz \u2014 stops on the next strong signal');
                        } else {
                            toast('Scan failed: ' + (data.error || 'unknown error'), true);
                        }
                    })
                    .catch(() => toast('Scan failed: receiver unreachable', true));
            };
            scanBtn.onclick = doScanToggle;
            const sn_t = d.id;
            const tuneD = (mhz) => fetch('/tune_dongle?d=' + encodeURIComponent(sn_t) + '&f=' + mhz);
            // Read the input IMMEDIATELY: clicking Tune blurs the field on
            // mousedown, and the 200 ms status poll may overwrite the typed
            // value with the current frequency before the click handler runs
            // (the tune then went nowhere and looked dead)
            const doTune = () => {
                const v = parseFloat(dinput.value);
                if (isNaN(v)) { toast('Enter a frequency in MHz (24-1766)', true); return; }
                tuneD(v.toFixed(4))
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            dinput._typedAt = 0;
                            toast(`Tuned dongle ${d.index}${d.primary ? ' ★' : ''} to ${v.toFixed(4)} MHz — Sync on its card to rejoin`);
                        } else {
                            toast(`Tune failed: ${data.error || 'unknown error'}`, true);
                        }
                    })
                    .catch(() => toast('Tune failed: receiver unreachable', true));
            };
            dTune.onclick = doTune;
            dinput.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); doTune(); } });
            dinput.addEventListener('input', () => { dinput._typedAt = Date.now(); });
            const nudge = (delta) => {
                const cur = curDongleMHz(sn_t);
                if (cur !== null) tuneD((cur + delta).toFixed(4));
            };
            dMinus.onclick = () => nudge(-0.001);
            dPlus.onclick = () => nudge(0.001);
            dSync.onclick = () => tuneD('auto');
            // Tuning/adjustments infobox (per-dongle correction from the
            // server + the shared offset/gain settings). margin-top:auto
            // pins it (with the Listen button) to the card bottom, against
            // the divider that separates this dongle from the next.
            const info = document.createElement('div');
            info.style.cssText = 'margin-top:auto;font-size:0.75em;color:#888;background:rgba(15,52,96,0.5);border:1px solid #0f3460;border-radius:3px;padding:3px 5px;line-height:1.5;';
            const listenBtn = document.createElement('button');
            listenBtn.style.cssText = 'width:100%;margin-top:5px;padding:4px 10px;background:#0f3460;color:#53d769;border:1px solid #53d769;border-radius:3px;cursor:pointer;font-family:monospace;font-size:0.9em;';
            listenBtn.textContent = '🔊 Listen';
            const sn_card = d.id;
            listenBtn.onclick = () => toggleLiveAudio(sn_card);
            card.appendChild(idHead);
            card.appendChild(srcLine);
            card.appendChild(freqLine);
            card.appendChild(bandTxt);
            card.appendChild(sigRow);
            card.appendChild(tl);
            const secRec = mkSec('Recording');
            card.appendChild(secRec); card.appendChild(recRow);
            const secTune = mkSec('Tuning');
            card.appendChild(secTune); card.appendChild(tuneRow); card.appendChild(nudgeRow);
            const secMode = mkSec('Demodulator');
            card.appendChild(secMode); card.appendChild(modeRow);
            const secBw = mkSec('Recorded bandwidth');
            card.appendChild(secBw); card.appendChild(bwRow);
            const secScan = mkSec('Frequency scan');
            card.appendChild(secScan); card.appendChild(scanRow); card.appendChild(scanRow2);
            const removeBtn = document.createElement('button');
            removeBtn.style.cssText = 'width:100%;margin-top:4px;padding:3px 10px;background:rgba(231,76,60,0.15);color:#e07890;border:1px solid #e07890;border-radius:3px;cursor:pointer;font-family:monospace;font-size:0.85em;';
            removeBtn.textContent = '\u2715 Remove dongle';
            removeBtn.title = 'Unregister this rtl_tcp dongle from the receiver';
            removeBtn.onclick = () => removeDongle(d.id);
            // Role dropdown: what this dongle is dedicated to. The four
            // dedicated roles park the dongle on their band and decode it
            // (the capture thread restarts in the new role server-side);
            // "Satellites" returns it to auto tracking. One select
            // instead of four full-width buttons.
            const roleSel = document.createElement('select');
            roleSel.style.cssText = 'width:100%;margin-top:4px;background:#0f3460;color:#4da6ff;border:1px solid #4da6ff;border-radius:3px;padding:3px 6px;font-family:monospace;font-size:0.85em;cursor:pointer;';
            roleSel.title = 'What this dongle listens for';
            const ROLE_OPTS = [
                ['sat',     '🛰 Satellites — auto tracking'],
                ['manual',  '📻 Manual — free tuning'],
                ['ais',     '🚢 AIS — ship traffic'],
                ['orbcomm', '🛰 Orbcomm — satellite IoT'],
                ['aprs',    '📡 APRS — packet radio'],
                ['pocsag',  '📟 POCSAG — paging'],
                ['adsb',    '✈️ ADS-B — aircraft'],
            ];
            for (const [rv, rt] of ROLE_OPTS) {
                const o = document.createElement('option');
                o.value = rv; o.textContent = rt;
                roleSel.appendChild(o);
            }
            const ROLE_ENDPOINT = {
                ais: 'ais_dongle', orbcomm: 'orbcomm_dongle',
                aprs: 'aprs_dongle', pocsag: 'pocsag_dongle',
                adsb: 'adsb_dongle',
            };
            const ROLE_TOAST = {
                manual: '📻 Manual mode — tune this dongle freely (frequency + bandwidth)',
                ais: '🚢 Switching this dongle to AIS — ship traffic on 161.975/162.025 MHz',
                orbcomm: '🛰 Switching this dongle to Orbcomm — satellite IoT downlink on 137.0-138.0 MHz',
                aprs: '📡 Switching this dongle to APRS — ham packet radio on 144.800 MHz',
                pocsag: '📟 Switching this dongle to POCSAG — paging channels decoded live',
                adsb: '✈️ Switching this dongle to ADS-B — aircraft on 1090 MHz decoded live',
            };
            roleSel.addEventListener('change', () => {
                const want = roleSel.value;
                const ld = c.lastD || {};
                const cur = dongleRole(ld);
                if (want === cur) return;
                const id = encodeURIComponent(d.id);
                const DED = ['ais', 'orbcomm', 'aprs', 'pocsag', 'adsb'];
                // The requests, in order: leaving a dedicated role turns
                // it off; "Manual" parks the dongle at its current
                // frequency (detaching it from the scheduler);
                // "Satellites" also clears a parked manual frequency so
                // the dongle really rejoins auto tracking
                // Page-RELATIVE urls (no leading slash): through the nginx
            // prefix proxy (prawntenna2.derusi.de/prawnceiver/) the
            // browser resolves them against /prawnceiver/, and the
            // sub_filter only rewrites literal fetch('/ strings - these
            // are built into a variable, so the leading slash would send
            // them to the bare root and 404 (observed 2026-10-17: the
            // role dropdown was dead through the proxy)
            const urls = [];
                if (DED.indexOf(cur) >= 0) urls.push(ROLE_ENDPOINT[cur] + '?d=' + id + '&on=0');
                if (want === 'manual') {
                    const f = curDongleMHz(d.id);
                    if (f === null) { toast('Role switch failed: no frequency to park on', true); roleSel.value = cur; return; }
                    urls.push('tune_dongle?d=' + id + '&f=' + f.toFixed(4));
                } else if (want === 'sat'
                        && ld.manual_frequency_mhz !== null && ld.manual_frequency_mhz !== undefined) {
                    urls.push('tune_dongle?d=' + id + '&f=auto');
                }
                if (DED.indexOf(want) >= 0) urls.push(ROLE_ENDPOINT[want] + '?d=' + id + '&on=1');
                const fail = (msg) => { toast('Role switch failed: ' + msg, true); roleSel.value = cur; };
                const run = (i) => {
                    if (i >= urls.length) {
                        toast(want === 'sat' ? '🛰 Dongle rejoined satellite tracking' : ROLE_TOAST[want]);
                        return;
                    }
                    fetch(urls[i])
                        .then(r => r.json())
                        .then(data => { data.success ? run(i + 1) : fail(data.error || 'unknown error'); })
                        .catch(() => fail('receiver unreachable'));
                };
                run(0);
            });
            // Auto-join satellite passes checkbox (shown by
            // applyDongleRole in the dedicated roles): while this dongle
            // listens for AIS/ADS-B/..., it leaves that role when a
            // satellite pass starts — tuning to the pass and recording
            // like any tracker — and returns to the role after set.
            // The checkbox state itself mirrors d.auto_sat from
            // dongles.json
            const autoSatRow = document.createElement('label');
            autoSatRow.style.cssText = 'display:none;align-items:center;width:100%;margin-top:4px;padding:3px 10px;background:#0f2233;color:#a8b8d8;border:1px solid #1a3a5c;border-radius:3px;cursor:pointer;font-family:monospace;font-size:0.82em;box-sizing:border-box;';
            autoSatRow.title = 'Join satellite passes automatically: while this dongle listens in a dedicated role (AIS/ADS-B/...), it leaves that role when a pass starts and returns to it after set — no effect while it tracks the satellites';
            const autoSatBox = document.createElement('input');
            autoSatBox.type = 'checkbox';
            autoSatBox.style.cssText = 'accent-color:#53d769;margin-right:6px;';
            const autoSatTxt = document.createElement('span');
            autoSatTxt.textContent = '🛰 Auto-join satellite passes';
            autoSatRow.appendChild(autoSatBox); autoSatRow.appendChild(autoSatTxt);
            autoSatBox.onchange = () => {
                const on = autoSatBox.checked;
                fetch('/auto_sat_dongle?d=' + encodeURIComponent(d.id) + '&on=' + (on ? 1 : 0))
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            toast(on ? '🛰 Auto-join ON — this dongle leaves its decode role during passes and returns after set'
                                     : '🛰 Auto-join OFF — this dongle stays in its role');
                        } else {
                            autoSatBox.checked = !on;
                            toast('Auto-join switch failed: ' + (data.error || 'unknown error'), true);
                        }
                    })
                    .catch(() => { autoSatBox.checked = !on; toast('Auto-join switch failed: receiver unreachable', true); });
            };
            // ISS digipeater checkbox (APRS mode only, shown by
            // applyDongleRole): retunes the APRS channel from the
            // 144.800 MHz DL simplex channel to the ISS (Zarya)
            // digipeater on 145.825 MHz — server-side the capture
            // thread restarts with the new tune. The checkbox state
            // itself mirrors d.aprs_iss from dongles.json
            const aprsIssRow = document.createElement('label');
            aprsIssRow.style.cssText = 'display:none;align-items:center;width:100%;margin-top:4px;padding:3px 10px;background:#0f2233;color:#a8b8d8;border:1px solid #1a3a5c;border-radius:3px;cursor:pointer;font-family:monospace;font-size:0.82em;box-sizing:border-box;';
            aprsIssRow.title = 'While this dongle listens for APRS: tune to the ISS (Zarya) digipeater on 145.825 MHz instead of the 144.800 MHz simplex channel — frames relayed through the ISS show up during passes';
            const aprsIssBox = document.createElement('input');
            aprsIssBox.type = 'checkbox';
            aprsIssBox.style.cssText = 'accent-color:#53d769;margin-right:6px;';
            const aprsIssTxt = document.createElement('span');
            aprsIssTxt.textContent = '🛰 ISS digipeater (145.825 MHz)';
            aprsIssRow.appendChild(aprsIssBox); aprsIssRow.appendChild(aprsIssTxt);
            // LASARsat digipeater checkbox (APRS mode only): retunes to
            // 436.925 MHz and swaps the demod to GFSK 9600 (G3RUH) —
            // LASARsat relays APRS frames during passes. Mutually
            // exclusive with the ISS checkbox (server-side the flags
            // clear each other; both boxes mirror dongles.json)
            const aprsLasarRow = document.createElement('label');
            aprsLasarRow.style.cssText = 'display:none;align-items:center;width:100%;margin-top:4px;padding:3px 10px;background:#0f2233;color:#a8b8d8;border:1px solid #1a3a5c;border-radius:3px;cursor:pointer;font-family:monospace;font-size:0.82em;box-sizing:border-box;';
            aprsLasarRow.title = 'While this dongle listens for APRS: tune to the LASARsat digipeater on 436.925 MHz instead — GFSK 9600 (not Bell 202), frames relayed through the Czech LASARsat CubeSat show up during passes';
            const aprsLasarBox = document.createElement('input');
            aprsLasarBox.type = 'checkbox';
            aprsLasarBox.style.cssText = 'accent-color:#53d769;margin-right:6px;';
            const aprsLasarTxt = document.createElement('span');
            aprsLasarTxt.textContent = '🛰 LASARsat digipeater (436.925 MHz, GFSK 9k6)';
            aprsLasarRow.appendChild(aprsLasarBox); aprsLasarRow.appendChild(aprsLasarTxt);
            aprsLasarBox.onchange = () => {
                const on = aprsLasarBox.checked;
                fetch('/aprs_lasar?d=' + encodeURIComponent(d.id) + '&on=' + (on ? 1 : 0))
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            toast(on ? '🛰 APRS channel -> 436.925 MHz (LASARsat digipeater, GFSK 9600) — retuning'
                                     : '📡 APRS channel -> 144.800 MHz (DL simplex) — retuning');
                        } else {
                            aprsLasarBox.checked = !on;
                            toast('LASARsat switch failed: ' + (data.error || 'unknown error'), true);
                        }
                    })
                    .catch(() => { aprsLasarBox.checked = !on; toast('LASARsat switch failed: receiver unreachable', true); });
            };
            aprsIssBox.onchange = () => {
                const on = aprsIssBox.checked;
                fetch('/aprs_iss?d=' + encodeURIComponent(d.id) + '&on=' + (on ? 1 : 0))
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            toast(on ? '🛰 APRS channel -> 145.825 MHz (ISS digipeater) — retuning'
                                     : '📡 APRS channel -> 144.800 MHz (DL simplex) — retuning');
                        } else {
                            aprsIssBox.checked = !on;
                            toast('ISS switch failed: ' + (data.error || 'unknown error'), true);
                        }
                    })
                    .catch(() => { aprsIssBox.checked = !on; toast('ISS switch failed: receiver unreachable', true); });
            };
            // Pause/Play: temporarily stops this dongle's capture thread —
            // two dongles recording at once can overload the dongle host's
            // shared USB bus. The dongle stays registered; Play restarts it.
            const pauseBtn = document.createElement('button');
            pauseBtn.style.cssText = 'width:100%;margin-top:4px;padding:3px 10px;background:#0f3460;color:#4da6ff;border:1px solid #4da6ff;border-radius:3px;cursor:pointer;font-family:monospace;font-size:0.85em;';
            pauseBtn.title = 'Temporarily stop this dongle\'s capture — frees the dongle host\'s USB bandwidth (two dongles recording at once overload it). Play restarts the capture.';
            pauseBtn.onclick = () => {
                const on = !(c.lastD && c.lastD.paused);
                fetch('/pause_dongle?d=' + encodeURIComponent(d.id) + '&on=' + (on ? 1 : 0))
                    .then(r => r.json())
                    .then(data => {
                        if (data.success) {
                            toast(on ? '⏸ Paused — capture stopped, USB bandwidth freed'
                                     : '▶ Play — capture restarting');
                        } else {
                            toast('Pause failed: ' + (data.error || 'unknown error'), true);
                        }
                    })
                    .catch(() => toast('Pause failed: receiver unreachable', true));
            };
            card.appendChild(info); card.appendChild(listenBtn); card.appendChild(roleSel); card.appendChild(autoSatRow); card.appendChild(aprsIssRow); card.appendChild(aprsLasarRow); card.appendChild(pauseBtn); card.appendChild(removeBtn);
            col.appendChild(card);
            c = dongleCards[d.id] = {
                cardEl: card,
                sigVal, sigFill, tlCtx: tl.getContext('2d'),
                hist: [], recLed, recTxt, recBtn, info, listenBtn, roleSel, autoSatRow, autoSatBox, aprsIssRow, aprsIssBox, aprsLasarRow, aprsLasarBox, pauseBtn, dinput, lastD: d, lastHistTs: 0,
                bandTxt, bwInput, tuneRow, nudgeRow, bwRow, scanRow, scanRow2, scanFrom, scanTo, scanStep, scanBtn, scanTxt,
                idLed, freqLine, secTune, secMode, secBw, secScan, modeRow, modeSel, modeHint,
            };
            applyDongleRole(c, d);
            return c;
        }

        function drawCardTimeline(c) {
            const ctx = c.tlCtx, w = 250, h = 46;
            ctx.fillStyle = '#000';
            ctx.fillRect(0, 0, w, h);
            if (c.hist.length < 2) return;
            const maxVal = Math.max(...c.hist, 10);
            const step = w / (MAX_CARD_POINTS - 1);
            ctx.beginPath();
            ctx.moveTo(0, h);
            for (let i = 0; i < c.hist.length; i++) {
                ctx.lineTo(i * step, h - (c.hist[i] / maxVal) * h);
            }
            ctx.lineTo((c.hist.length - 1) * step, h);
            ctx.closePath();
            ctx.fillStyle = 'rgba(83, 215, 105, 0.2)';
            ctx.fill();
            ctx.beginPath();
            for (let i = 0; i < c.hist.length; i++) {
                const x = i * step, y = h - (c.hist[i] / maxVal) * h;
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            }
            ctx.strokeStyle = '#53d769';
            ctx.lineWidth = 1;
            ctx.stroke();
        }

        function updateDongleCard(d) {
            const c = ensureDongleCard(d);
            // role flips arrive through dongles.json (ais/orbcomm/
            // aprs/pocsag flags)
            if (c.roleLast !== dongleRole(d)) applyDongleRole(c, d);
            // ISS digipeater checkbox mirrors the server flag (the click
            // already flipped it locally; this only follows external
            // changes, e.g. a second browser tab)
            if (c.issLast !== !!d.aprs_iss) { c.issLast = !!d.aprs_iss; c.aprsIssBox.checked = c.issLast; }
            if (c.lasarLast !== !!d.aprs_lasar) { c.lasarLast = !!d.aprs_lasar; c.aprsLasarBox.checked = c.lasarLast; }
            // Auto-join checkbox mirrors the server flag (the click
            // already flipped it locally; this only follows external
            // changes, e.g. a second browser tab)
            if (c.autoSatLast !== !!d.auto_sat) { c.autoSatLast = !!d.auto_sat; c.autoSatBox.checked = c.autoSatLast; }
            c.sigVal.textContent = d.signal ? d.signal.toFixed(1) : '—';
            // Signal metric: ~150 = silence floor, 800+ = strong satellite carrier
            // with auto-gain — map that range onto the bar so idle noise
            // doesn't peg it at 100%
            const pct = Math.max(0, Math.min(100, ((d.signal || 0) - 150) / 7));
            c.sigFill.style.width = pct + '%';
            const now = Date.now();
            if (now - c.lastHistTs >= 1000) {
                c.lastHistTs = now;
                c.hist.push(d.signal || 0);
                if (c.hist.length > MAX_CARD_POINTS) c.hist.shift();
                drawCardTimeline(c);
            }
            c.recLed.className = 'indicator ' + (d.recording ? 'green' : 'red');
            const rec = d.recording ? (d.wav || 'Recording…') : (d.paused ? '⏸ Paused' : 'Idle');
            if (c.recTxt._last !== rec) { c.recTxt._last = rec; c.recTxt.textContent = rec; }
            // Record button mirrors the manual flag (a running pass
            // recording is independent of it); while recording it
            // counts up from manual_rec_since (the raw-IQ capture's
            // start time)
            const recOn = !!d.manual_recording;
            let recBtnTxt = '⏺ Record';
            if (recOn && d.manual_rec_since) {
                const el = Math.max(0, Math.floor(Date.now() / 1000 - d.manual_rec_since));
                recBtnTxt = '⏹ ' + Math.floor(el / 60) + ':' + String(el % 60).padStart(2, '0');
            } else if (recOn) {
                recBtnTxt = '⏹ Stop Rec';
            }
            if (c.recBtn._last !== recBtnTxt) { c.recBtn._last = recBtnTxt; c.recBtn.textContent = recBtnTxt; }
            // Raw recording finished → wait for the zip, then download
            // it automatically (the server builds it a few seconds
            // after the stop; the wait also covers the retune and
            // role-switch paths, where the recording is finalized
            // without a Stop click)
            const _wasRec = c._prevManualRec;
            c._prevManualRec = recOn;
            if (_wasRec === true && !recOn) {
                c._manualZipWait = Date.now() + 120000;
            }
            if (c._manualZipWait && !recOn && d.manual_zip
                && d.manual_zip !== c._lastManualZip) {
                c._manualZipWait = 0;
                c._lastManualZip = d.manual_zip;
                const a = document.createElement('a');
                a.href = d.manual_zip;
                document.body.appendChild(a);
                a.click();
                a.remove();
                toast('\uD83D\uDCE6 ' + d.manual_zip.split('/').pop() + ' \u2014 download started');
            } else if (c._manualZipWait && Date.now() > c._manualZipWait) {
                c._manualZipWait = 0;    // zip never appeared (build failed?)
            }
            c.recBtn.style.background = recOn ? '#e74c3c' : 'rgba(231,76,60,0.15)';
            c.recBtn.style.color = recOn ? '#1a1a2e' : '#e07890';
            // Pause/Play button mirrors the paused flag (orange while paused)
            const pTxt = d.paused ? '▶ Play' : '⏸ Pause';
            if (c.pauseBtn._last !== pTxt) { c.pauseBtn._last = pTxt; c.pauseBtn.textContent = pTxt; }
            c.pauseBtn.style.background = d.paused ? '#2a1a0f' : '#0f3460';
            c.pauseBtn.style.color = d.paused ? '#f39c12' : '#4da6ff';
            c.pauseBtn.style.borderColor = d.paused ? '#f39c12' : '#4da6ff';
            // Manual tuner state: show the effective frequency, highlight
            // the input while this dongle overrides the shared frequency
            c.lastD = d;
            const eff = curDongleMHz(d.id);
            // Demodulator: manual override or auto (AM on the airband
            // 118-137 MHz, FM everywhere else)
            const manualMode = d.manual_mode || 'auto';
            if (c.modeSel._last !== manualMode && document.activeElement !== c.modeSel) {
                c.modeSel._last = manualMode;
                c.modeSel.value = manualMode;
            }
            const airband = eff !== null && eff >= 117.975 && eff < 136.975;
            const hint = manualMode !== 'auto' ? ''
                : 'auto \u2192 ' + (airband ? 'AM' : 'FM');
            if (c.modeHint._last !== hint) { c.modeHint._last = hint; c.modeHint.textContent = hint; }
            // Identity: connection LED + the live tuned frequency —
            // orange while manually overridden, blue on the AIS channels
            c.idLed.className = 'indicator ' + (d.connected ? 'green' : 'red');
            let fTxt = '—', fCol = '#888';
            if (d.ais) { fTxt = '🚢 AIS 161.975 / 162.025 MHz'; fCol = '#4da6ff'; }
            else if (d.orbcomm) { fTxt = '🛰 Orbcomm 137.0-138.0 MHz (center 137.5)'; fCol = '#b39ddb'; }
            else if (d.aprs) { fTxt = '📡 APRS ' + (d.aprs_lasar ? '436.925 MHz (LASARsat)' : (d.aprs_iss ? '145.825 MHz (ISS)' : '144.800 MHz')); fCol = '#53d769'; }
            else if (d.pocsag) { fTxt = '📟 POCSAG 466.0 MHz (±1.2 MHz)'; fCol = '#f39c12'; }
            else if (d.adsb) { fTxt = '✈️ ADS-B 1090.0 MHz'; fCol = '#4da6ff'; }
            else if (eff !== null) {
                fTxt = eff.toFixed(4) + ' MHz' + (d.manual_frequency_mhz ? ' (manual)' : '');
                fCol = d.manual_frequency_mhz ? '#f39c12' : '#53d769';
            }
            if (c.freqLine._last !== fTxt) { c.freqLine._last = fTxt; c.freqLine.textContent = fTxt; }
            c.freqLine.style.color = fCol;
            // What this frequency is normally used for (band allocation)
            const bandHtml = d.ais
                ? '🚢 AIS — Danube ship traffic · 161.975 / 162.025 MHz · <a href="/ais" style="color:#4da6ff;text-decoration:none;">→ full log</a> · <a href="/ais.json" target="_blank" rel="noopener" style="color:#4da6ff;text-decoration:none;">ais.json</a>'
                : (d.orbcomm
                    ? '🛰 Orbcomm — satellite IoT downlink · 137.0-138.0 MHz · <a href="/orbcomm" style="color:#b39ddb;text-decoration:none;">→ full log</a> · <a href="/orbcomm.json" target="_blank" rel="noopener" style="color:#b39ddb;text-decoration:none;">orbcomm.json</a>'
                    : (d.aprs
                    ? '📡 APRS — ' + (d.aprs_lasar ? 'LASARsat digipeater · 436.925 MHz · GFSK 9600' : (d.aprs_iss ? 'ISS (Zarya) digipeater · 145.825 MHz' : 'ham packet radio · 144.800 MHz')) + ' · <a href="/aprs" style="color:#53d769;text-decoration:none;">→ full log</a> · <a href="/aprs.json" target="_blank" rel="noopener" style="color:#53d769;text-decoration:none;">aprs.json</a>'
                    : (eff !== null
                    ? '📶 ' + bandLabel(bandFor(eff))
                    : '📶 <span style="color:#666;">—</span>')));
            if (c.bandTxt._last !== bandHtml) {
                c.bandTxt._last = bandHtml;
                c.bandTxt.innerHTML = bandHtml;
            }
            if (document.activeElement !== c.dinput && eff !== null
                    && Date.now() - (c.dinput._typedAt || 0) > 3000) {
                c.dinput.value = eff.toFixed(4);
            }
            c.dinput.style.border = '1px solid ' + (d.manual_frequency_mhz ? '#f39c12' : '#53d769');
            if (document.activeElement !== c.bwInput
                    && Date.now() - (c.bwInput._typedAt || 0) > 3000) {
                const want = d.manual_bw_khz !== null && d.manual_bw_khz !== undefined
                    ? String(Math.round(d.manual_bw_khz * 100) / 100) : '';
                if (c.bwInput.value !== want) c.bwInput.value = want;
            }
            // Scan controls: button doubles as Stop while a scan runs, the
            // status line shows live progress / the result; empty from/to
            // fields prefill with the current tune (\u00b15 MHz)
            if (!d.ais && !d.orbcomm && !d.aprs) {
                const sc = d.scan;
                const active = !!(sc && sc.active);
                const btnTxt = active ? '\u23F9 Stop' : '\uD83D\uDD0D Scan';
                if (c.scanBtn._last !== btnTxt) { c.scanBtn._last = btnTxt; c.scanBtn.textContent = btnTxt; }
                c.scanBtn.style.background = active ? '#f39c12' : '#53d769';
                if (eff !== null) {
                    if (c.scanFrom.value === '' && Date.now() - (c.scanFrom._typedAt || 0) > 3000) c.scanFrom.value = eff.toFixed(3);
                    if (c.scanTo.value === '' && Date.now() - (c.scanTo._typedAt || 0) > 3000) c.scanTo.value = (eff + 5).toFixed(3);
                }
                let st = '';
                if (sc) {
                    if (active) st = 'scanning ' + sc.cur_mhz.toFixed(3) + ' MHz\u2026';
                    else if (sc.result === 'found') st = 'found ' + sc.found_mhz.toFixed(4) + ' MHz (+' + sc.found_db + ' dB) \u2014 parked'
                        + (sc.bw_khz ? ', records \u00b1' + sc.bw_khz + ' kHz' : '');
                    else if (sc.result === 'nothing') st = 'done \u2014 nothing found';
                    else if (sc.result === 'stopped') st = 'stopped at ' + sc.cur_mhz.toFixed(3) + ' MHz';
                    else if (sc.result === 'aborted') st = 'aborted \u2014 satellite pass';
                    else if (sc.result === 'nodata') st = 'ended \u2014 no data from this dongle';
                }
                if (c.scanTxt._last !== st) { c.scanTxt._last = st; c.scanTxt.textContent = st; c.scanTxt.title = st; }
            }
            // Selected-range band on the waterfall: visible while a manual
            // frequency or bandwidth selection is active; width = the
            // recorded bandwidth (manual value, else the mode default)
            const selActive = !!d.manual_frequency_mhz
                || (d.manual_bw_khz !== null && d.manual_bw_khz !== undefined);
            const wfB = wfBlocks[d.id];
            if (wfB) {
                if (selActive) {
                    let selBwKhz = d.manual_bw_khz;
                    if (selBwKhz === null || selBwKhz === undefined) {
                        selBwKhz = autoDemodKhz(curDongleMHz(d.id));
                    }
                    const p1 = Math.max(0, wfKhzToPct(wfB, -selBwKhz));
                    const p2 = Math.min(100, wfKhzToPct(wfB, selBwKhz));
                    wfB.selHl.style.display = 'block';
                    wfB.selHl.style.left = p1 + '%';
                    wfB.selHl.style.width = Math.max(0, p2 - p1) + '%';
                    wfB.selLbl.textContent = '\u00b1' + (Math.round(selBwKhz * 10) / 10) + ' kHz';
                } else {
                    wfB.selHl.style.display = 'none';
                }
                // recording band (primary only) follows the zoom too
                if (wfB.recHl) {
                    const p1 = Math.max(0, wfKhzToPct(wfB, -24));
                    const p2 = Math.min(100, wfKhzToPct(wfB, 24));
                    wfB.recHl.style.left = p1 + '%';
                    wfB.recHl.style.width = Math.max(0, p2 - p1) + '%';
                }
                // satellite downlink marker (dashed blue line): active pass
                // if one is up, else the next upcoming — hidden when its
                // frequency is outside this dongle's visible window
                if (satTrackInfo && eff !== null && wfB.satLine) {
                    const offKhz = (satTrackInfo.freqMHz - eff) * 1000;
                    const spanHalf = wfSpanKhz(wfB) / 2, vOff = wfB.zoomOffKHz || 0;
                    if (Math.abs(offKhz - vOff) <= spanHalf + 0.5) {
                        wfB.satLine.style.display = 'block';
                        wfB.satLine.style.left = wfKhzToPct(wfB, offKhz) + '%';
                        // at center during a recording the green highlight
                        // label already names the satellite — skip ours
                        const showLbl = !(satRecHighlight && d.primary && Math.abs(offKhz) < 5);
                        wfB.satLineLbl.style.display = showLbl ? 'block' : 'none';
                        wfB.satLineLbl.textContent = (satTrackInfo.active ? '' : 'next: ') + satTrackInfo.name + ' \u00b7 ' + satTrackInfo.freqMHz + ' MHz';
                    } else {
                        wfB.satLine.style.display = 'none';
                    }
                } else if (wfB.satLine) {
                    wfB.satLine.style.display = 'none';
                }
            }
            // Tuning infobox: this dongle's correction (value + source) and
            // the settings shared by all dongles — the manual override
            // state shows on the frequency line at the top of the card
            const cor = (d.correction_hz || 0) / 1000;
            const infoHtml = `⚙ correction ${cor >= 0 ? '+' : ''}${cor.toFixed(1)} kHz <span style="color:#666">(${d.correction_src || 'n/a'})</span> · IF offset +60 kHz · gain auto`;
            if (c.info._last !== infoHtml) {
                c.info._last = infoHtml;
                c.info.innerHTML = infoHtml;
            }
            const listenTxt = (liveAudioOn === d.id) ? '⏹ Stop Listening' : '🔊 Listen';
            if (c.listenBtn._last !== listenTxt) { c.listenBtn._last = listenTxt; c.listenBtn.textContent = listenTxt; }
            c.listenBtn.style.background = (liveAudioOn === d.id) ? '#53d769' : '#0f3460';
            c.listenBtn.style.color = (liveAudioOn === d.id) ? '#1a1a2e' : '#53d769';
        }

        function escHtml(s) {
            return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
                            .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
        }

        // ---------- SatNOGS DB info card for the active pass ----------
        // Fetched once per pass via the server-side proxy (the DB API has
        // no CORS headers); cached server-side for a day per satellite.
        let satnogsCatnr = null;
        async function fetchSatnogsInfo(catnr, satName) {
            const el = document.getElementById('satnogs-info');
            el.innerHTML = '<div class="sat-card" style="color:#888;">📡 loading satellite info…</div>';
            try {
                const res = await fetch('/satnogs.json?catnr=' + encodeURIComponent(catnr));
                const data = await res.json();
                if (!data.info) { el.innerHTML = ''; return; }
                const s = data.info;
                // Compact contact card: photo, name, key facts, and the
                // transmitter list in a short scrollable area
                let html = '<div class="sat-card">';
                html += '<div class="sat-card-head">';
                if (s.image) html += '<img src="' + escHtml(s.image)
                    + '" alt="satellite" title="View the full-size picture"'
                    + ' style="cursor:zoom-in" onerror="this.remove()">';
                html += '<div><div class="sat-name">' + escHtml(satName) + '</div>';
                if (s.names) html += '<div class="sat-sub">' + escHtml(s.names) + '</div>';
                const subs = [];
                if (s.launched) subs.push('launched ' + escHtml(s.launched));
                if (s.countries) subs.push(escHtml(s.countries));
                if (s.status) subs.push(escHtml(s.status));
                html += '<div class="sat-sub">' + subs.join(' · ') + '</div></div></div>';
                if (s.transmitters && s.transmitters.length) {
                    html += '<div class="sat-tx">';
                    for (const t of s.transmitters) {
                        const dot = t.alive ? 'green' : 'red';
                        const dl = t.downlink_hz ? (t.downlink_hz / 1e6).toFixed(4) + ' MHz' : '';
                        html += '<div class="sat-tx-row"><span class="indicator ' + dot + '"></span>'
                              + escHtml(t.description || '?')
                              + ' <span style="color:#888;">' + dl
                              + (t.mode ? ' · ' + escHtml(t.mode) : '') + '</span></div>';
                    }
                    html += '</div>';
                }
                html += '</div>';
                el.innerHTML = html;
                // Satellite photo: click to view it full size
                const simg = el.querySelector('.sat-card-head img');
                if (simg) simg.addEventListener('click',
                    () => openLogoLightbox(s.image));
            } catch (e) {
                el.innerHTML = '';
            }
        }

        let statusFails = 0;
        // Station status onto the header/banner/passes UI — shared by
        // the /status.json fallback poll and the WS 'status' topic push
        function applyStatus(data) {
            try {
                if (data.sdr_khz) {
                    SDR_KHZ = data.sdr_khz;
                    const sp = document.getElementById('wf-spankhz');
                    if (sp) sp.textContent = Math.round(SDR_KHZ / 2);
                }
                if (statusFails >= 2) document.getElementById('conn-banner').style.display = 'none';
                statusFails = 0;
                document.getElementById('rec-count').textContent = data.recording_count || 0;
                renderRecordPause(!!data.recordings_paused);
                const dsEl = document.getElementById('dongle-status');
                if (dsEl) {
                    const dl = data.dongles || [];
                    const up = dl.filter(d => d.connected).length;
                    dsEl.textContent = dl.length
                        ? (up + '/' + dl.length + ' connected — ' + dl.map(d =>
                            d.label + (d.primary ? ' ★' : '') + (d.ais ? ' (AIS)' : (d.orbcomm ? ' (Orbcomm)' : (d.aprs ? ' (APRS)' : '')))
                            + ': ' + (d.connected ? 'live' : 'offline')).join(' · '))
                        : 'no dongles — add one below';
                }
                tleInfo = data.tle || null;
                renderTleBar();
                updateTleSyncBar(tleInfo);
                const ageEl = document.getElementById('tle-age');
                if (ageEl) ageEl.textContent = (data.tle_age_min !== null && data.tle_age_min !== undefined)
                    ? 'TLEs ' + Math.round(data.tle_age_min) + ' min old' : 'no TLE data yet';

                // Update frequency display
                updateFreqLabels(data.frequency_mhz);
                lastSharedMHz = data.frequency_mhz;
                // Waterfall recording-band highlight (the primary block is
                // built dynamically, so guard for it not existing yet)
                const hl = document.getElementById('wf-highlight');
                if (data.recording) {
                    if (hl) {
                        hl.style.display = 'block';
                        document.getElementById('wf-highlight-label').textContent = `${data.satellite} — ${data.frequency_mhz} MHz`;
                    }
                } else {
                    if (hl) hl.style.display = 'none';
                }

                // Satellite marker target: the active pass's downlink, or
                // the next upcoming pass when idle
                if (data.pass_active && data.current_pass) {
                    satTrackInfo = { name: data.current_pass.sat_name, freqMHz: data.current_pass.frequency_mhz, active: true };
                } else if (data.next_pass) {
                    satTrackInfo = { name: data.next_pass.sat_name, freqMHz: data.next_pass.frequency_mhz, active: false };
                } else {
                    satTrackInfo = null;
                }
                satRecHighlight = !!data.recording;

                // Live image renderer follows the active pass (can be
                // disabled below with LIVE_IMAGE_ENABLED — the code stays)
                const liMode = 'sstv';
                if (LIVE_IMAGE_ENABLED) {
                    if (data.pass_active) {
                        if (!liveImg.active || liveImg.mode !== liMode) liveImgStart(liMode);
                    } else if (liveImg.active) {
                        liveImgStop();
                    }
                } else if (liveImg.active) {
                    liveImgStop();
                }

                // Pass banner
                const banner = document.getElementById('pass-banner');
                if (data.manual_frequency_mhz) {
                    banner.className = 'pass-soon';
                    banner.textContent = `📻 Manual tune: ${data.manual_frequency_mhz} MHz — satellite tracking paused (click 🛰 Auto to resume)`;
                } else if (data.pass_active && data.current_pass) {
                    banner.className = 'pass-active';
                    banner.textContent = `🔴 LIVE PASS: ${data.current_pass.sat_name} (${data.current_pass.frequency_mhz} MHz) — Max ${data.current_pass.max_alt}° — Sets at ${data.current_pass.set_local}` +
                        (data.doppler_hz ? ` — Doppler ${(data.doppler_hz/1000 >= 0 ? '+' : '')}${(data.doppler_hz/1000).toFixed(2)} kHz` : '');
                } else if (data.next_pass) {
                    const mins = data.next_pass.countdown_min;
                    if (mins < 60) {
                        banner.className = 'pass-soon';
                        banner.innerHTML = `⏳ Next pass: ${data.next_pass.sat_name} in <span class="countdown-big">${mins}min</span> — ${data.next_pass.rise_local} — Max ${data.next_pass.max_alt}°`;
                    } else {
                        banner.className = 'pass-idle';
                        banner.textContent = `Next pass: ${data.next_pass.sat_name} at ${data.next_pass.rise_local} — Max ${data.next_pass.max_alt}° (${Math.round(mins/60*10)/10}h)`;
                    }
                } else {
                    banner.className = 'pass-idle';
                    banner.textContent = 'No passes predicted in the next 24h';
                }
                if (data.recordings_paused) banner.textContent += '  —  ⏸ recordings paused';

                // SatNOGS info card: fetch once per pass (per catalog number)
                const activeCatnr = (data.pass_active && data.current_pass) ? data.current_pass.catnr : null;
                if (activeCatnr && activeCatnr !== satnogsCatnr) {
                    satnogsCatnr = activeCatnr;
                    fetchSatnogsInfo(activeCatnr, data.current_pass.sat_name);
                } else if (!activeCatnr) {
                    satnogsCatnr = null;
                }
            } catch(e) {
                console.error('Status apply failed:', e);
            }
        }

        async function fetchStatus() {
            try {
                applyStatus(await (await fetch('/status.json')).json());
            } catch(e) {
                if (++statusFails >= 2) document.getElementById('conn-banner').style.display = 'block';
                console.error('Status fetch failed:', e);
            }
        }

        // Progress bar beneath the TLE info box: visible while a sync is
        // starting (button pressed, scheduler picks it up within ~10 s) or
        // running (status.json tle.active/done/total/current). Hides itself
        // once the sync is over or the starting window (60 s) has elapsed.
        function updateTleSyncBar(tle) {
            const bar = document.getElementById('tle-sync-bar');
            if (!bar) return;
            const active = !!(tle && tle.active);
            if (active) tleSyncWasActive = true;
            const starting = !active && !tleSyncWasActive && tleSyncClickAt && (Date.now() - tleSyncClickAt) < 60000;
            if (active) {
                const label = document.getElementById('tle-sync-label');
                const count = document.getElementById('tle-sync-count');
                const fill = document.getElementById('tle-sync-fill');
                if (label) label.textContent = 'Updating ' + (tle.current || 'TLE data') + '…';
                if (count) count.textContent = tle.total ? tle.done + '/' + tle.total : '';
                if (fill) fill.style.width = (tle.total ? Math.round((tle.done / tle.total) * 100) : 0) + '%';
                bar.style.display = 'block';
            } else if (starting) {
                const label = document.getElementById('tle-sync-label');
                const count = document.getElementById('tle-sync-count');
                const fill = document.getElementById('tle-sync-fill');
                if (label) label.textContent = 'Sync starting…';
                if (count) count.textContent = '';
                if (fill) fill.style.width = '0%';
                bar.style.display = 'block';
            } else {
                bar.style.display = 'none';
                tleSyncClickAt = 0;
                tleSyncWasActive = false;
            }
        }

        function renderTleBar() {
            const list = document.getElementById('passes-list');
            if (!list || !tleInfo || !tleInfo.active) return;
            const pct = tleInfo.total ? Math.round((tleInfo.done / tleInfo.total) * 100) : 0;
            list.innerHTML = `<div style="padding:8px;font-size:0.95em;color:#888;">
                📡 Updating TLE data — ${tleInfo.current || ''} (${tleInfo.done}/${tleInfo.total})
                <div class="signal-bar" style="margin-top:4px;"><div class="signal-fill" style="width:${pct}%;"></div></div>
            </div>`;
        }

        // Upcoming passes render — shared by the /passes.json fallback
        // poll and the WS 'passes' topic push
        function applyPasses(passes) {
            try {
                const list = document.getElementById('passes-list');
                if (passes.length === 0) {
                    list.innerHTML = '<div style="color:#666;padding:10px;">No passes found in next 24h.</div>';
                    return;
                }
                let html = '';
                const now = Date.now() / 1000;
                passes.forEach((p, i) => {
                    const isActive = p.rise_timestamp <= now && p.set_timestamp >= now;
                    const isNext = !isActive && passes.findIndex(x => x.rise_timestamp > now) === i;
                    const cls = isActive ? 'active' : (isNext ? 'next ' : '') + p.quality;
                    const countdown = isActive ? 'NOW' : (p.rise_timestamp - now > 0 ? 
                        `${Math.floor((p.rise_timestamp - now)/60)}min` : '');
                    const qIcon = p.quality === 'high' ? '🟢' : (p.quality === 'medium' ? '🟡' : '🔴');
                    // Receive plan: what this pass is listened to with and how
                    // the recording will be decoded (hover for details)
                    const txDead = p.tx && p.tx.alive === false;
                    // Home-grown transmitter ledger (our own decode
                    // history): dark streak of passes without an image.
                    // Chip next to the curated SatNOGS flag - stale
                    // SatNOGS vs. measured truth
                    const homeDark = p.tx_home && p.tx_home.dark_streak >= 3;
                    const modeCls = 'pass-mode m-' + (p.mode || '').toLowerCase() + (txDead ? ' tx-dead' : '');
                    let planTip = p.plan || '';
                    if (p.tx) planTip += `\nSatNOGS: ${p.tx.description || '?'} [${p.tx.mode || '?'}] ${p.tx.alive === false ? 'inactive' : 'active'} · ${p.tx.downlink_mhz} MHz`;
                    if (p.tx_home) planTip += `\nCrabArk ledger: ${p.tx_home.dark_streak} dark pass(es) of ${p.tx_home.passes} tracked, last image ${p.tx_home.last_heard_ts ? Math.round((now - p.tx_home.last_heard_ts) / 3600) + ' h ago' : 'never'}`;
                    if (p.freq_note) planTip += `\n${p.freq_note}`;
                    const modeChip = (p.mode ? `<span class="${modeCls}">${p.mode}</span>` : '')
                        + (homeDark ? `<span class="pass-mode tx-dead" title="our own decode history: no image in ${p.tx_home.dark_streak} passes">🦀${p.tx_home.dark_streak}×</span>` : '')
                        + (p.freq_note ? `<span class="pass-mode m-sstv" title="${escHtml(p.freq_note)}">🔄</span>` : '');
                    html += `<div class="pass-row ${cls}" title="${escHtml(planTip)}">
                        <div class="pass-sat">${qIcon} ${p.sat_name} ${modeChip}</div>
                        <div class="pass-time">${p.rise_local} → ${p.set_local}</div>
                        <div class="pass-alt">${p.max_alt}°</div>
                        <div class="pass-freq">${p.frequency_mhz} MHz</div>
                        <div class="pass-countdown">${countdown}</div>
                    </div>`;
                });
                list.innerHTML = html;
            } catch(e) { console.error('Error applying passes:', e); }
        }

        async function fetchPasses() {
            try {
                if (tleInfo && tleInfo.active) { renderTleBar(); return; }
                applyPasses(await (await fetch('/passes.json')).json());
            } catch(e) { console.error('Error fetching passes:', e); }
        }

        // Decoded-recording count — shared by the /recordings.json
        // fallback poll and the WS 'recordings' topic push
        function applyRecordings(recordings) {
            const crabEl = document.getElementById('crab-count');
            if (crabEl) crabEl.textContent = recordings.filter(r => r.decoded).length;
        }

        async function fetchCrabCount() {
            try {
                applyRecordings(await (await fetch('/recordings.json')).json());
            } catch(e) { console.error('Error fetching recordings:', e); }
        }

        // Polar pass view — shared by the /track.json fallback poll and
        // the WS 'track' topic push
        function applyTrack(data) {
            try {
                const section = document.getElementById('track-section');
                if (!data.active) {
                    section.style.display = 'none';
                    return;
                }
                section.style.display = 'block';
                document.getElementById('track-sat').textContent = `${data.sat_name} (az ${data.now ? data.now.az : '—'}° / el ${data.now ? data.now.alt : '—'}°)`;
                drawTrack(data);
            } catch(e) { console.error('Error applying track:', e); }
        }

        async function fetchTrack() {
            try {
                applyTrack(await (await fetch('/track.json')).json());
            } catch(e) { console.error('Error fetching track:', e); }
        }

        function drawTrack(data) {
            const canvas = document.getElementById('track-canvas');
            const ctx = canvas.getContext('2d');
            const w = canvas.width, h = canvas.height;
            const cx = w / 2, cy = h / 2;
            const R = Math.min(w, h) / 2 - 22;
            const rad = Math.PI / 180;

            ctx.fillStyle = '#000';
            ctx.fillRect(0, 0, w, h);

            // Elevation rings: 0, 30, 60 degrees
            ctx.strokeStyle = '#16213e';
            ctx.lineWidth = 1;
            for (const alt of [0, 30, 60]) {
                ctx.beginPath();
                ctx.arc(cx, cy, (90 - alt) / 90 * R, 0, 2 * Math.PI);
                ctx.stroke();
            }
            // Cross-hair + azimuth labels
            ctx.strokeStyle = '#0f3460';
            ctx.beginPath();
            ctx.moveTo(cx - R, cy); ctx.lineTo(cx + R, cy);
            ctx.moveTo(cx, cy - R); ctx.lineTo(cx, cy + R);
            ctx.stroke();
            ctx.fillStyle = '#666';
            ctx.font = '11px monospace';
            ctx.textAlign = 'center';
            ctx.fillText('N', cx, cy - R - 6);
            ctx.fillText('S', cx, cy + R + 14);
            ctx.textAlign = 'left';
            ctx.fillText('E', cx + R + 4, cy + 4);
            ctx.textAlign = 'right';
            ctx.fillText('W', cx - R - 4, cy + 4);
            ctx.textAlign = 'center';
            ctx.fillText('90°', cx, cy + 4);

            function project(pt) {
                const r = (90 - pt.alt) / 90 * R;
                const a = pt.az * rad;
                return [cx + r * Math.sin(a), cy - r * Math.cos(a)];
            }

            const nowTs = data.now ? data.now.t : Date.now() / 1000;
            const track = data.track || [];
            if (track.length > 1) {
                // Remaining pass (dashed, dim)
                ctx.strokeStyle = '#444';
                ctx.setLineDash([4, 3]);
                ctx.beginPath();
                let started = false;
                for (const pt of track) {
                    if (pt.t < nowTs) continue;
                    const [x, y] = project(pt);
                    if (!started) { ctx.moveTo(x, y); started = true; }
                    else ctx.lineTo(x, y);
                }
                ctx.stroke();
                ctx.setLineDash([]);
                // Flown portion (solid green)
                ctx.strokeStyle = '#53d769';
                ctx.lineWidth = 1.5;
                ctx.beginPath();
                started = false;
                for (const pt of track) {
                    if (pt.t > nowTs) break;
                    const [x, y] = project(pt);
                    if (!started) { ctx.moveTo(x, y); started = true; }
                    else ctx.lineTo(x, y);
                }
                ctx.stroke();
            }

            // Current position
            if (data.now) {
                const [x, y] = project(data.now);
                ctx.fillStyle = '#53d769';
                ctx.beginPath();
                ctx.arc(x, y, 5, 0, 2 * Math.PI);
                ctx.fill();
                ctx.strokeStyle = 'rgba(83, 215, 105, 0.4)';
                ctx.lineWidth = 2;
                ctx.beginPath();
                ctx.arc(x, y, 10, 0, 2 * Math.PI);
                ctx.stroke();
            }
        }

        function updateUptime() {
            const s = Math.floor((Date.now() - startTime) / 1000);
            const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
            document.getElementById('uptime').textContent = `${h}h ${m}m ${sec}s`;
        }

        let logoLightboxOn = false;
        function toggleLogoLightbox() {
            logoLightboxOn = !logoLightboxOn;
            document.getElementById('logo-lightbox').style.display = logoLightboxOn ? 'flex' : 'none';
        }
        // Shared full-size viewer: any site picture (header logo, the
        // SatNOGS satellite photo, ...) zooms into the logo lightbox
        function openLogoLightbox(src) {
            const img = document.querySelector('#logo-lightbox img');
            if (src && img) img.src = src;
            if (!logoLightboxOn) toggleLogoLightbox();
        }
        document.addEventListener('keydown', e => { if (e.key === 'Escape' && logoLightboxOn) toggleLogoLightbox(); });

        // ---------- Collapsible side panels (dongles / system status) ----------
        // State persists in localStorage; narrow screens start collapsed so
        // the waterfall keeps the full width on mobile devices.
        const PANELS = { 'status-panel': 'panel-status-collapsed' };
        function togglePanel(id) {
            const el = document.getElementById(id);
            const collapsed = !el.classList.contains('collapsed');
            el.classList.toggle('collapsed', collapsed);
            try { localStorage.setItem(PANELS[id], collapsed ? '1' : '0'); } catch (e) {}
            // the waterfall column width changed — redraw the feature overlay
            if (wfOverlayOn) drawWfOverlay();
        }
        function initPanels() {
            const narrow = window.innerWidth < 860;
            for (const [id, key] of Object.entries(PANELS)) {
                let v = null;
                try { v = localStorage.getItem(key); } catch (e) {}
                document.getElementById(id).classList.toggle('collapsed', v === '1' || (v === null && narrow));
            }
        }

        let liveAudioOn = null;   // serial of the dongle currently being listened to
        function toggleLiveAudio(serial) {
            const audio = document.getElementById('live-audio');
            if (liveAudioOn === serial) {
                audio.pause();
                audio.removeAttribute('src');
                audio.load(); // drop the connection
                liveAudioOn = null;
            } else {
                audio.src = '/live.wav?d=' + encodeURIComponent(serial);
                audio.play().catch(e => { console.error('Live audio failed:', e); });
                liveAudioOn = serial;
            }
        }

        async function tuneManual() {
            const f = document.getElementById('tune-freq').value;
            try {
                const res = await fetch('/tune?f=' + encodeURIComponent(f));
                const data = await res.json();
                if (!data.success) alert('Tune failed: ' + (data.error || res.status));
            } catch (e) { alert('Tune failed: ' + e); }
        }

        async function tuneAuto() {
            try { await fetch('/tune?f=auto'); } catch (e) {}
        }

        // Global recording pause (System Status panel): server-side flag,
        // rendered from status.json so the button survives reloads and
        // Station database restore (System Status panel): posts the
        // chosen backup archive to /restore as a raw zip body.
        async function restoreBackup(input) {
            const f = input.files && input.files[0];
            if (!f) return;
            const el = document.getElementById('restore-status');
            if (!confirm('Restore the station database and ALL recordings from ' + f.name + '? The current state is kept as .pre-restore copies.')) {
                input.value = '';
                return;
            }
            el.textContent = 'uploading and restoring...';
            try {
                const res = await fetch('/restore', {method: 'POST', body: f});
                const data = await res.json();
                if (res.ok && data.success) {
                    el.textContent = 'restored: ' + data.summary.passes + ' passes, '
                        + data.summary.ships + ' ships, ' + data.summary.recording_files
                        + ' recording files - restart the receiver to reload everything';
                } else {
                    el.textContent = 'restore failed: ' + (data.error || res.status);
                }
            } catch (e) {
                el.textContent = 'restore failed: ' + e;
            }
            input.value = '';
        }

        // reflects changes made from another browser tab.
        function renderRecordPause(paused) {
            const btn = document.getElementById('rec-pause-btn');
            const st = document.getElementById('rec-pause-state');
            if (btn) {
                btn.dataset.paused = paused ? '1' : '0';
                btn.textContent = paused ? '▶ Resume' : '⏸ Pause';
                btn.className = paused ? 'btn-solid' : 'btn-outline';
            }
            if (st) {
                st.textContent = paused ? '⏸ Paused — nothing is recorded' : 'Active — WAVs recorded during passes';
                st.style.color = paused ? '#f39c12' : '#888';
            }
        }

        async function toggleRecordPause() {
            const btn = document.getElementById('rec-pause-btn');
            const to = (btn && btn.dataset.paused === '1') ? 0 : 1;
            try {
                const res = await fetch('/record_pause?paused=' + to);
                const data = await res.json();
                if (data.success) {
                    renderRecordPause(!!data.recordings_paused);
                    toast(data.recordings_paused ? '⏸ Automatic recordings paused' : '▶ Automatic recordings resumed');
                } else {
                    toast('Pause failed: ' + (data.error || 'unknown error'), true);
                }
            } catch (e) { toast('Pause failed: receiver unreachable', true); }
        }

        async function syncTle() {
            // Show the bar immediately on click; kept up on success (and on
            // "already running") until the scheduler reports progress or the
            // sync finishes — updateTleSyncBar drives it from status.json
            tleSyncClickAt = Date.now();
            updateTleSyncBar(null);
            try {
                const res = await fetch('/sync_tle');
                const data = await res.json();
                if (data.success) {
                    toast('🔄 TLE sync started — pass predictions refresh when it finishes');
                } else {
                    if (!(data.error || '').includes('already running')) {
                        tleSyncClickAt = 0;
                        updateTleSyncBar(null);
                    }
                    toast('Sync failed: ' + (data.error || 'unknown error'), true);
                }
            } catch (e) {
                tleSyncClickAt = 0;
                updateTleSyncBar(null);
                toast('Sync failed: receiver unreachable', true);
            }
        }

        // Live image rendering is disabled (browser-side SSTV decode
        // of the /live.wav stream). Set to true to re-enable — all the
        // renderer code below stays functional.
        const LIVE_IMAGE_ENABLED = false;
        if (!LIVE_IMAGE_ENABLED) {
            const lip = document.getElementById('liveimg-panel');
            if (lip) lip.style.display = 'none';
        }

        // ---------- Live image renderer (ISS Robot 36) ----------
        // Decodes the /live.wav FM-demod audio in the browser and paints the
        // image as it arrives, next to the polar pass view.
        //   Robot 36: 1500-2300 Hz frequency tones, 320x240, 150 ms/line,
        //        synced on the 1900/1200 Hz calibration header + VIS code
        const LI_RATE = 48000;
        const R36 = { W: 320, H: 240, LINE_MS: 150, SYNC_MS: 9, Y_OFF_MS: 12, Y_PX_MS: 0.275, Y_WIN_MS: 2.12, C_OFF_MS: 106, C_PX_MS: 0.1375, C_WIN_MS: 1.06 };
        const liveImg = { active: false, mode: null, streaming: false, gen: 0 };

        // ---- raw PCM ring buffer, absolute sample indexing ----
        const liBuf = { arr: new Float32Array(LI_RATE * 30), start: 0, end: 0, abs: 0 };
        function liAppend(samples) {
            if (liBuf.end + samples.length > liBuf.arr.length) {
                const keep = liBuf.end - liBuf.start;
                if (keep > 0) liBuf.arr.copyWithin(0, liBuf.start, liBuf.end);
                liBuf.abs += liBuf.start;
                liBuf.start = 0;
                liBuf.end = keep;
            }
            if (samples.length > liBuf.arr.length) return; // unreachable at 30 s cap
            if (liBuf.end + samples.length > liBuf.arr.length) {
                // drop oldest data, decoders resync on the next marker
                const drop = liBuf.end + samples.length - liBuf.arr.length;
                liBuf.abs += drop;
                liBuf.start += drop;
            }
            liBuf.arr.set(samples, liBuf.end);
            liBuf.end += samples.length;
        }
        function liAvail() { return liBuf.end - liBuf.start; }
        function liWin(absStart, len, out) {
            const off = absStart - liBuf.abs;
            if (off < 0 || off + len > liBuf.end - liBuf.start) return false;
            out.set(liBuf.arr.subarray(liBuf.start + off, liBuf.start + off + len));
            return true;
        }

        // ---- /live.wav ingestion (skip 44-byte WAV header, byte-align, int16 -> float) ----
        let liSkip = 44, liCarry = new Uint8Array(0), liLastData = 0, liAbort = null;
        function liFeed(bytes) {
            if (liSkip > 0) {
                if (bytes.length <= liSkip) { liSkip -= bytes.length; return; }
                bytes = bytes.subarray(liSkip);
                liSkip = 0;
            }
            let buf = bytes;
            if (liCarry.length) {
                buf = new Uint8Array(liCarry.length + bytes.length);
                buf.set(liCarry); buf.set(bytes, liCarry.length);
                liCarry = new Uint8Array(0);
            }
            const usable = buf.length & ~1;
            if (usable < buf.length) liCarry = buf.subarray(usable);
            const n = usable >> 1;
            const f = new Float32Array(n);
            for (let i = 0; i < n; i++) f[i] = (buf[2 * i] | (buf[2 * i + 1] << 8)) / 32768;
            liAppend(f);
            liLastData = Date.now();
        }

        async function liStream() {
            liveImg.streaming = true;
            const myGen = liveImg.gen;
            let backoff = 2000;
            while (liveImg.active && myGen === liveImg.gen) {
                const ctrl = new AbortController();
                liAbort = ctrl;
                try {
                    const res = await fetch('/live.wav', { signal: ctrl.signal });
                    if (!res.ok || !res.body) throw new Error('stream unavailable');
                    const reader = res.body.getReader();
                    for (;;) {
                        const { done, value } = await reader.read();
                        if (done || !liveImg.active) break;
                        liFeed(value);
                        backoff = 2000;
                    }
                } catch (e) { /* aborted or connection dropped */ }
                if (liveImg.active && myGen === liveImg.gen) {
                    liStatus('reconnecting…');
                    await new Promise(r => setTimeout(r, backoff));
                    backoff = Math.min(backoff * 2, 15000);
                }
            }
            liveImg.streaming = false;
        }

        function liStatus(text) { document.getElementById('liveimg-status').textContent = text; }

        // ---- Goertzel tone power (evaluates the DTFT at an exact frequency;
        // short pixel windows are too coarse for integer-bin rounding) ----
        function goertzel(win, freq) {
            const n = win.length;
            const coeff = 2 * Math.cos(2 * Math.PI * freq / LI_RATE);
            let s1 = 0, s2 = 0;
            for (let i = 0; i < n; i++) { const s0 = win[i] + coeff * s1 - s2; s2 = s1; s1 = s0; }
            return (s1 * s1 + s2 * s2 - coeff * s1 * s2) / (n * n);
        }

        // ---- tone -> level (1500 Hz black .. 2300 Hz white, 1900 Hz = chroma 128) ----
        const LUM_FREQS = [];
        for (let f = 1500; f <= 2300; f += 50) LUM_FREQS.push(f);
        const liBank = new Float64Array(LUM_FREQS.length);
        function toneLevel(win) {
            for (let i = 0; i < LUM_FREQS.length; i++) liBank[i] = goertzel(win, LUM_FREQS[i]);
            let bi = 0;
            for (let i = 1; i < LUM_FREQS.length; i++) if (liBank[i] > liBank[bi]) bi = i;
            let f = LUM_FREQS[bi];
            if (bi > 0 && bi < LUM_FREQS.length - 1) {
                const a = liBank[bi - 1], b = liBank[bi], c = liBank[bi + 1];
                const denom = a - 2 * b + c;
                if (denom !== 0) {
                    const delta = 0.5 * (a - c) / denom;
                    if (delta > -1 && delta < 1) f += delta * 50;
                }
            }
            return Math.max(0, Math.min(255, (f - 1500) / 3.1372549));
        }

        // ---- canvas ----
        let liRowImg = null, liPairImg = null;
        function liCanvasMode() {
            const c = document.getElementById('liveimg-canvas');
            const ctx = c.getContext('2d');
            c.width = R36.W; c.height = R36.H;
            liRowImg = ctx.createImageData(R36.W, 1); liPairImg = ctx.createImageData(R36.W, 2);
            ctx.fillStyle = '#000';
            ctx.fillRect(0, 0, c.width, c.height);
        }

        // ---- Robot 36 decoder ----
        const w480 = new Float32Array(480), w605 = new Float32Array(605), w432 = new Float32Array(432), w102 = new Float32Array(102), w51 = new Float32Array(51), w1440 = new Float32Array(1440);
        const sstv = { scan: 0, state: 'hdr', header: -1, imageStart: -1, lineStart: -1, y: 0, Y: null, C: null, hdrStart: 0, toneRef: 0 };
        const ms = n => Math.round(n * LI_RATE / 1000);

        function liFindSync(from, limit) {
            // Two-stage sync alignment. Coarse: a window dominated by 1200 Hz.
            // Refine: the 432-sample window (sync length) with the highest
            // 1200 Hz power marks the pulse start to ~0.25 ms.
            let hit = -1;
            for (let j = from; j + w605.length <= limit; j += 24) {
                if (!liWin(j, w605.length, w605)) return -1;
                const g12 = goertzel(w605, 1200);
                if (g12 > 4 * goertzel(w605, 1500) && g12 > 4 * goertzel(w605, 1900)) { hit = j; break; }
            }
            if (hit < 0) return -1;
            let best = -1, bestP = -1;
            for (let k = hit - 24; k + w432.length <= limit; k += 24) {
                if (!liWin(k, w432.length, w432)) break;
                const p = goertzel(w432, 1200);
                if (p > bestP) { bestP = p; best = k; }
                if (k > hit + w605.length) break;
            }
            // Reject noise: the sync must be within ~12 dB of the header leader tone
            if (best < 0 || sstv.toneRef <= 0 || bestP < sstv.toneRef / 4) return -1;
            return best;
        }

        function liSstv() {
            const absEnd = liBuf.abs + liAvail();
            if (sstv.state === 'hdr') {
                if (!liSstv.searchShown && sstv.scan > sstv.hdrStart + ms(2000)) {
                    liStatus('Robot 36 — searching for SSTV header…');
                    liSstv.searchShown = true;
                }
                while (sstv.scan + ms(650) <= absEnd) {
                    const s = sstv.scan;
                    const hdr = liWin(s, 480, w480) && goertzel(w480, 1900) > 4 * goertzel(w480, 1200)
                        && liWin(s + ms(300), 480, w480) && goertzel(w480, 1200) > 4 * goertzel(w480, 1900)
                        && liWin(s + ms(310), 480, w480) && goertzel(w480, 1900) > 4 * goertzel(w480, 1200)
                        && liWin(s + ms(610), 480, w480) && goertzel(w480, 1200) > 4 * goertzel(w480, 1900);
                    if (hdr) {
                        sstv.header = s;
                        sstv.toneRef = goertzel(w480, 1900);
                        sstv.state = 'vis';
                        liStatus('Robot 36 — header found, VIS…');
                        break;
                    }
                    sstv.scan += 96; // 2 ms steps
                }
            }
            if (sstv.state === 'vis' && sstv.header + ms(910) <= absEnd) {
                let code = 0, ones = 0, ok = true;
                for (let i = 0; i < 8; i++) {
                    if (!liWin(sstv.header + ms(640) + i * ms(30), 1440, w1440)) { ok = false; break; }
                    if (goertzel(w1440, 1100) > goertzel(w1440, 1300)) { ones++; if (i < 7) code |= 1 << i; }
                }
                if (ok) {
                    if (ones % 2 === 0 && code === 8) { // Robot 36
                        sstv.imageStart = sstv.header + ms(910);
                        sstv.lineStart = -1;
                        sstv.y = 0;
                        sstv.state = 'img';
                        sstv.Y = new Float32Array(R36.W * R36.H);
                        sstv.C = new Float32Array(R36.W * R36.H);
                        liCanvasMode('sstv');
                        liStatus('Robot 36 — image incoming…');
                    } else {
                        liSstvToHdr(sstv.header + 96);
                    }
                }
            }
            if (sstv.state === 'img') {
                while (sstv.state === 'img') {
                    if (sstv.lineStart < 0) {
                        if (sstv.imageStart + ms(150) > absEnd) break;
                        const p = liFindSync(sstv.imageStart, Math.min(absEnd, sstv.imageStart + ms(2500)));
                        if (p < 0) {
                            if (sstv.imageStart + ms(2500) > absEnd) break;
                            liSstvToHdr(sstv.imageStart);
                            break;
                        }
                        sstv.lineStart = p;
                    }
                    if (sstv.lineStart + ms(R36.LINE_MS) > absEnd) break;
                    liDecodeR36Line(sstv.lineStart, sstv.y);
                    sstv.y++;
                    if (sstv.y >= R36.H) {
                        liSstvToHdr(sstv.lineStart + ms(R36.LINE_MS));
                        liStatus('Robot 36 — image complete ✓');
                        break;
                    }
                    const next = liFindSync(sstv.lineStart + ms(R36.LINE_MS), Math.min(absEnd, sstv.lineStart + ms(R36.LINE_MS + 40)));
                    if (next < 0) {
                        if (sstv.lineStart + ms(R36.LINE_MS + 40) > absEnd) break;
                        liSstvToHdr(sstv.lineStart + ms(R36.LINE_MS));
                        liStatus(`Robot 36 — signal lost after ${sstv.y} lines`);
                        break;
                    }
                    sstv.lineStart = next;
                    liStatus(`Robot 36 — line ${sstv.y}/${R36.H}`);
                }
            }
        }

        function liSstvToHdr(scanPos) {
            sstv.state = 'hdr';
            sstv.scan = scanPos;
            sstv.hdrStart = scanPos;
            liSstv.searchShown = false;
        }

        function liDecodeR36Line(start, y) {
            const base = y * R36.W;
            for (let i = 0; i < R36.W; i++) {
                const s = Math.round(start + (R36.Y_OFF_MS + i * R36.Y_PX_MS - R36.Y_WIN_MS / 2) * LI_RATE / 1000);
                if (!liWin(s, 102, w102)) return;
                sstv.Y[base + i] = toneLevel(w102);
            }
            for (let i = 0; i < R36.W; i++) {
                const s = Math.round(start + (R36.C_OFF_MS + i * R36.C_PX_MS - R36.C_WIN_MS / 2) * LI_RATE / 1000);
                if (!liWin(s, 51, w51)) return;
                sstv.C[base + i] = toneLevel(w51);
            }
            // show luma immediately, colorize the line pair once chroma is in
            const ctx = document.getElementById('liveimg-canvas').getContext('2d');
            const d = liRowImg.data;
            for (let x = 0; x < R36.W; x++) {
                const v = sstv.Y[base + x];
                d[4 * x] = d[4 * x + 1] = d[4 * x + 2] = v; d[4 * x + 3] = 255;
            }
            ctx.putImageData(liRowImg, 0, y);
            if (y % 2 === 1) {
                const p = liPairImg.data;
                for (let yy = 0; yy < 2; yy++) {
                    const row = (y - 1 + yy) * R36.W;
                    for (let x = 0; x < R36.W; x++) {
                        const Y = sstv.Y[row + x];
                        const even = ((y - 1 + yy) % 2 === 0);
                        const cb = sstv.C[(even ? y - 1 + yy + 1 : y - 1 + yy) * R36.W + x];
                        const cr = sstv.C[(even ? y - 1 + yy : y - 1 + yy - 1) * R36.W + x];
                        const o = 4 * (yy * R36.W + x);
                        p[o] = Math.max(0, Math.min(255, Y + 1.402 * (cr - 128)));
                        p[o + 1] = Math.max(0, Math.min(255, Y - 0.344136 * (cb - 128) - 0.714136 * (cr - 128)));
                        p[o + 2] = Math.max(0, Math.min(255, Y + 1.772 * (cb - 128)));
                        p[o + 3] = 255;
                    }
                }
                ctx.putImageData(liPairImg, 0, y - 1);
            }
        }

        // ---- lifecycle ----
        function liveImgStart(mode) {
            liveImg.active = true;
            liveImg.gen++;
            liveImg.mode = 'sstv';
            document.getElementById('liveimg-mode').textContent = 'ISS Robot 36';
            liCanvasMode();
            // reset all decoder state
            liBuf.start = liBuf.end = 0; liBuf.abs = 0;
            liSkip = 44; liCarry = new Uint8Array(0); liLastData = Date.now();
            Object.assign(sstv, { scan: 0, state: 'hdr', header: -1, imageStart: -1, lineStart: -1, y: 0, Y: null, C: null, hdrStart: 0, toneRef: 0 });
            liSstv.searchShown = false;
            liStatus('Robot 36 — waiting for transmission…');
            liStream();
        }

        function liveImgStop() {
            liveImg.active = false;
            liStatus('pass ended');
            if (liAbort) { try { liAbort.abort(); } catch (e) {} }
        }

        function liveImgTick() {
            if (!liveImg.active) return;
            if (Date.now() - liLastData > 12000 && liAbort) {
                // stalled stream (server restart): force a reconnect
                try { liAbort.abort(); } catch (e) {}
                liLastData = Date.now();
            }
            liSstv();
        }

        // Notebook power state — shared by the /power.json fallback poll
        // and the WS 'power' topic push; hidden on hosts without batteries
        function applyPower(data) {
                const el = document.getElementById('power-status');
                const bats = (data.supplies || []).filter(s => s.type === 'Battery');
                if (!bats.length) { el.style.display = 'none'; return; }
                const batTxt = bats.map(b => `🔋 ${b.name}: ${b.capacity !== null && b.capacity !== undefined ? b.capacity + '%' : '?'} (${b.status})`).join(' · ');
                el.textContent = (data.ac ? '⚡ AC power' : '🔌 on battery') + ' · ' + batTxt;
            el.style.display = '';
        }

        async function fetchPower() {
            try {
                applyPower(await (await fetch('/power.json')).json());
            } catch (e) { document.getElementById('power-status').style.display = 'none'; }
        }

        // Dashboard AIS section — shared by the /ais.json fallback poll
        // and the WS 'ais' topic push (the full AIS page renders itself)
        function applyAis(data) {
            try {
                const title = document.getElementById('ais-title');
                const section = document.getElementById('ais-section');
                if (!data.enabled) { title.style.display = 'none'; section.style.display = 'none'; return; }
                title.style.display = '';
                section.style.display = '';
                const ch = data.channels || {};
                const chStat = ['A', 'B'].map(c => ch[c] ? (c + ': ' + ch[c].frames + '✓') : (c + ': —')).join(' · ');
                document.getElementById('ais-stats').textContent =
                    data.ships.length + ' ships · ch ' + chStat + ' · dongle ' + data.dongle;
                const now = Date.now() / 1000;
                let html = '';
                data.ships.forEach(sh => {
                    const name = sh.name || ('MMSI ' + sh.mmsi);
                    const sub = [sh.cls, sh.status, sh.callsign].filter(Boolean).join(' · ');
                    const sog = sh.sog != null ? sh.sog.toFixed(1) + ' kn' : '—';
                    const cog = sh.cog != null ? Math.round(sh.cog) + '°' : '—';
                    let pos = '<span style="color:#888">—</span>';
                    if (sh.lat != null) {
                        const maps = 'https://www.google.com/maps?q=' + sh.lat + ',' + sh.lon;
                        pos = '<a href="' + maps + '" target="_blank" rel="noopener">' + sh.lat.toFixed(4) + ', ' + sh.lon.toFixed(4) + '</a>';
                    }
                    const age = Math.max(0, Math.round(now - sh.last_seen));
                    html += '<div class="ais-row">' +
                        '<div><div class="ais-name">' + escHtml(name) + '</div><div class="ais-sub">#' + sh.mmsi + ' · ' + escHtml(sub) + '</div></div>' +
                        '<div class="ais-val">' + sog + '</div>' +
                        '<div class="ais-val">' + cog + '</div>' +
                        '<div class="ais-pos">' + pos + '</div>' +
                        '<div class="ais-val">' + age + 's ago</div>' +
                        '<div class="ais-val" style="color:#4da6ff">' + (sh.last_channel || '') + '</div>' +
                        '</div>';
                });
                document.getElementById('ais-list').innerHTML = html ||
                    '<div style="color:#666;padding:10px;">No ships heard yet.</div>';
                document.getElementById('ais-nmea').textContent = (data.nmea || []).slice(-20).join('\n');
            } catch (e) { console.error('Error applying AIS:', e); }
        }

        async function fetchAIS() {
            try {
                applyAis(await (await fetch('/ais.json')).json());
            } catch (e) { console.error('Error fetching AIS:', e); }
        }

        // Dashboard Orbcomm section — shared by the /orbcomm.json fallback
        // poll and the WS 'orbcomm' topic push (the full log page renders
        // itself). Channels are the satellite carriers the receiver is
        // currently demodulating (freq + packet counters, position once a
        // Sync/Ephemeris pair identified the satellite)
        function applyOrbcomm(data) {
            try {
                const title = document.getElementById('orbcomm-title');
                const section = document.getElementById('orbcomm-section');
                if (!data.enabled) { title.style.display = 'none'; section.style.display = 'none'; return; }
                title.style.display = '';
                section.style.display = '';
                const chans = data.channels || [];
                const totPk = chans.reduce((s, ch) => s + (ch.packets || 0), 0);
                document.getElementById('orbcomm-stats').textContent =
                    chans.length + ' satellite channel' + (chans.length === 1 ? '' : 's')
                    + ' · ' + totPk + ' packets · dongle ' + data.dongle
                    + ' · 137.0-138.0 MHz';
                const now = Date.now() / 1000;
                let html = '';
                chans.forEach(ch => {
                    const name = ch.sat_id ? ('🛰 ' + ch.sat_id.toUpperCase()) : ('carrier ' + ch.freq_khz.toFixed(1) + ' kHz');
                    const sub = ch.freq_khz.toFixed(1) + ' kHz · ch ' + ch.key;
                    let pos = '<span style="color:#888">—</span>';
                    if (ch.lat != null) {
                        const maps = 'https://www.google.com/maps?q=' + ch.lat + ',' + ch.lon;
                        pos = '<a href="' + maps + '" target="_blank" rel="noopener">'
                            + ch.lat.toFixed(3) + ', ' + ch.lon.toFixed(3) + '</a>';
                    } else if (ch.gps_week) {
                        pos = '<span style="color:#888">GPS wk ' + ch.gps_week + '</span>';
                    }
                    const age = Math.max(0, Math.round(now - ch.last_seen));
                    html += '<div class="orbcomm-row">' +
                        '<div><div class="ais-name">' + escHtml(name) + '</div><div class="ais-sub">' + escHtml(sub) + '</div></div>' +
                        '<div class="ais-pos">' + pos + '</div>' +
                        '<div class="ais-val">' + ch.packets + ' pkt</div>' +
                        '<div class="ais-val">' + Math.round(now - ch.first_seen) + 's</div>' +
                        '<div class="ais-val" style="color:' + (ch.bad ? '#f39c12' : '#888') + '">' + ch.bad + ' bad</div>' +
                        '<div class="ais-val">' + age + 's ago</div>' +
                        '</div>';
                });
                document.getElementById('orbcomm-channels').innerHTML = html ||
                    '<div style="color:#666;padding:10px;">No satellite carriers in the band right now.</div>';
                const pk = (data.packets || []).slice(-25).map(e =>
                    e.time + '  ' + e.type +
                    (e.sat_id ? '  sat ' + e.sat_id.toUpperCase() : '') +
                    '  ' + e.freq_khz + ' kHz  ' + e.hex);
                document.getElementById('orbcomm-packets').textContent = pk.join('\n');
            } catch (e) { console.error('Error applying Orbcomm:', e); }
        }

        async function fetchOrbcomm() {
            try {
                applyOrbcomm(await (await fetch('/orbcomm.json')).json());
            } catch (e) { console.error('Error fetching Orbcomm:', e); }
        }

        // Dashboard APRS section — shared by the /aprs.json fallback poll
        // and the WS 'aprs' topic push (the full log page renders itself).
        // Stations are the callsigns heard this run; packets the ring of
        // newest decoded frames.
        function applyAprs(data) {
            try {
                const title = document.getElementById('aprs-title');
                const section = document.getElementById('aprs-section');
                if (!data.enabled) { title.style.display = 'none'; section.style.display = 'none'; return; }
                title.style.display = '';
                section.style.display = '';
                const st = data.stations || [];
                const frames = (data.stats && data.stats.frames) || 0;
                document.getElementById('aprs-stats').textContent =
                    st.length + ' station' + (st.length === 1 ? '' : 's') + ' heard'
                    + ' · ' + frames + ' frames · dongle ' + data.dongle
                    + ' · ' + (data.freq_mhz ? data.freq_mhz.toFixed(3) + ' MHz' : '—');
                let html = '';
                st.slice(0, 8).forEach(c => {
                    let pos = '<span style="color:#888">—</span>';
                    if (c.lat != null) {
                        pos = '<a href="https://www.google.com/maps?q=' + c.lat + ',' + c.lon
                            + '" target="_blank" rel="noopener">' + c.lat.toFixed(4) + ', ' + c.lon.toFixed(4) + '</a>';
                    }
                    html += '<div class="orbcomm-row">' +
                        '<div><div class="ais-name">' + escHtml(c.call) + '</div>' +
                        '<div class="ais-sub">' + escHtml(c.text || '') + '</div></div>' +
                        '<div class="ais-pos">' + pos + '</div>' +
                        '<div class="ais-val">' + (c.via ? escHtml(c.via) : 'direct') + '</div>' +
                        '<div class="ais-val">' + c.msgs + ' frm</div>' +
                        '<div class="ais-val">' + Math.max(0, Math.round(Date.now() / 1000 - c.last_seen)) + 's ago</div>' +
                        '</div>';
                });
                document.getElementById('aprs-stations').innerHTML = html ||
                    '<div style="color:#666;padding:10px;">The APRS channel is quiet — no stations heard this run.</div>';
                const pk = (data.packets || []).slice(-25).map(e =>
                    e.time + '  ' + e.call + '>' + e.dst + '  ' + (e.text || ''));
                document.getElementById('aprs-packets').textContent = pk.join('\n');
            } catch (e) { console.error('Error applying APRS:', e); }
        }

        async function fetchAprs() {
            try {
                applyAprs(await (await fetch('/aprs.json')).json());
            } catch (e) { console.error('Error fetching APRS:', e); }
        }

        // Init
        connectWaterfallWS();
        // WebSocket JSON topics: while the socket is live these replace
        // EVERY periodic fetch — the intervals below only run as the
        // HTTP fallback (same data, same cadences as the pushes)
        const WS_TOPICS = {
            dongles: applyDongles,
            status: applyStatus,
            passes: applyPasses,
            track: applyTrack,
            ais: applyAis,
            orbcomm: applyOrbcomm,
            aprs: applyAprs,
            power: applyPower,
            recordings: applyRecordings,
        };
        setInterval(() => { if (!wfWsLive()) fetchWaterfall(); }, 1000);
        setInterval(() => { if (!wfWsLive()) fetchWaterfallRows(); }, 200);
        setInterval(() => { if (!wfWsLive()) fetchStatus(); }, 3000);
        setInterval(() => { if (!wfWsLive()) fetchPasses(); }, 30000);
        setInterval(() => { if (!wfWsLive()) fetchTrack(); }, 2000);
        setInterval(() => { if (!wfWsLive()) fetchCrabCount(); }, 10000);
        setInterval(() => { if (!wfWsLive()) fetchAIS(); }, 10000);
        setInterval(() => { if (!wfWsLive()) fetchOrbcomm(); }, 10000);
        setInterval(() => { if (!wfWsLive()) fetchAprs(); }, 10000);
        setInterval(() => { if (!wfWsLive()) fetchPower(); }, 15000); fetchPower();
        setInterval(updateUptime, 1000);
        setInterval(liveImgTick, 250);
        document.getElementById('tune-freq').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); tuneManual(); } });
        fetchWaterfall(); fetchStatus(); fetchPasses(); fetchTrack(); fetchCrabCount(); fetchAIS(); fetchOrbcomm(); fetchAprs(); updateUptime(); initPanels(); buildBandJump();

        // ---------- static wiring (the markup carries no inline JS) ----------
        (function wireStaticControls() {
            document.querySelector('#header img').addEventListener('click', toggleLogoLightbox);
            document.getElementById('add-dongle-mode').addEventListener('change', dongleModeChanged);
            document.getElementById('add-dongle-refresh').addEventListener('click', function () { fetchUsbDongles(true); });
            document.getElementById('add-dongle-btn').addEventListener('click', addDongle);
            document.getElementById('wf-overlay-btn').addEventListener('click', toggleWfOverlay);
            document.getElementById('status-panel-toggle').addEventListener('click', function () { togglePanel('status-panel'); });
            document.getElementById('tune-manual-btn').addEventListener('click', tuneManual);
            document.getElementById('tune-auto-btn').addEventListener('click', tuneAuto);
            document.getElementById('tle-sync-btn').addEventListener('click', syncTle);
            document.getElementById('rec-pause-btn').addEventListener('click', toggleRecordPause);
            document.getElementById('restore-file').addEventListener('change', function () { restoreBackup(this); });
            document.getElementById('logo-lightbox').addEventListener('click', toggleLogoLightbox);
            // Header logo: click opens the full-size original
            // (the img already carries cursor:zoom-in)
            document.querySelector('#header img.logo').addEventListener('click',
                () => openLogoLightbox('/prawnceiver_full.jpg'));
            var lbImg = document.querySelector('#logo-lightbox img');
            if (lbImg) lbImg.addEventListener('error', function () {
                if (lbImg.dataset.fallback) return;
                lbImg.dataset.fallback = '1';
                lbImg.src = '/prawnceiver.jpg';
            });
        })();
