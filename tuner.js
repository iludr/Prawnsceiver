(function () {
    'use strict';

    const FREQUENCIES = {
        adsb: 1090, ais: 162, orbcomm: 137.5, aprs: 144.8,
        pocsag: 466, satellite: 137.9
    };
    const elements = Object.fromEntries([
        'dongle', 'service', 'geometry', 'initial', 'initial-cm', 'initial-mm',
        'nominal', 'start', 'session', 'session-state', 'instruction', 'measure',
        'stop', 'report-form', 'length', 'note', 'element-hint', 'feedback',
        'converged', 'final-length', 'final-note', 'history', 'points', 'plot'
    ].map(id => [id, document.getElementById(id)]));
    let sessionDongle = null;
    let active = false;
    let dongles = [];

    function selectedAntenna() {
        return document.querySelector('input[name="antenna"]:checked').value;
    }

    function selectedWaveFactor() {
        return parseFloat(document.getElementById('wave-factor').value);
    }

    function lengthRecommendation() {
        const frequency = FREQUENCIES[elements.service.value];
        const antenna = selectedAntenna();
        const waveFactor = selectedWaveFactor();
        const nominal = 28500 * waveFactor / frequency;
        return { nominal, start: nominal * 1.05, waveFactor };
    }

    function updateRecommendation() {
        const antenna = selectedAntenna();
        const wf = selectedWaveFactor();
        const result = lengthRecommendation();
        elements.initial.classList.remove('hidden');
        elements['initial-cm'].textContent = result.start.toFixed(2) + ' cm';
        elements['initial-mm'].textContent = (result.start * 10).toFixed(1) + ' mm';
        let nominalText = '';
        if (antenna === 'whip') {
            nominalText = 'Electrical length: wave_factor=' + wf + ' &lambda; (Element length)';
        } else {
            nominalText = 'Electrical length: wave_factor=' + wf + ' &lambda; (Each leg length)';
        }
        elements.nominal.textContent = nominalText + ': ' + result.nominal.toFixed(2) + ' cm';
        if (antenna === 'whip') {
            elements.geometry.textContent = (wf === 0.25 ? 'Quarter' : wf === 0.5 ? 'Half' : 'Full') + '-wave whip. Provide a ground plane.';
        } else {
            const totalLen = (result.nominal * 2).toFixed(2);
            elements.nominal.textContent += ' (Total ~' + totalLen + ' cm for both legs)';
            elements.geometry.textContent = (wf === 0.25 ? 'V-Dipole with half-wave total length' :
                                          wf === 0.5 ? 'V-Dipole with full-wave total length' :
                                          'V-Dipole with 2 &lambda; total length') +
                ': two equal legs opened ~120° apart, drooping.';
        }
    }

    function makeUrl(path, params) {
        const url = new URL(path, document.baseURI);
        Object.entries(params || {}).forEach(([key, value]) => {
            if (value !== undefined && value !== null) url.searchParams.set(key, value);
        });
        return url.href;
    }

    async function requestJson(path, params, options) {
        const requestUrl = makeUrl(path, params);
        const response = await fetch(requestUrl, options || {});
        const data = await response.json();
        if (!response.ok || data.success === false) {
            throw new Error(data.error || ('Receiver returned ' + response.status));
        }
        return data;
    }

    function roleName(dongle) {
        for (const role of ['ais', 'orbcomm', 'aprs', 'pocsag', 'adsb']) {
            if (dongle[role]) return role.toUpperCase();
        }
        return dongle.manual_frequency_mhz == null ? 'satellite' : 'manual';
    }

    
    function updateWaveFactorChoices() {
        const antenna = selectedAntenna();
        const select = document.getElementById('wave-factor');
        Array.from(select.options).forEach(opt => { opt.disabled = false; });
        if (antenna === 'whip') {
            // Whip is always quarter-wave monopole
            Array.from(select.options).forEach(opt => { opt.disabled = opt.value !== '0.25'; });
            if (select.value !== '0.25') select.value = '0.25';
            select.title = 'Whip is a quarter-wave monopole; wave factor fixed to 0.25';
        } else {
            select.title = 'For V-Dipole: 0.25 = half-wave total (legs λ/4 each), 0.5 = full-wave total (legs λ/2 each), 1.0 = two wavelength total (legs λ each)';
        }
        updateRecommendation();
    }

async function loadDongles() {
        const donglesUrl = 'dongles.json';
        const response = await fetch(donglesUrl);
        if (!response.ok) throw new Error('Could not load registered dongles');
        dongles = await response.json();
        const statusUrl = 'status.json';
        const statusResponse = await fetch(statusUrl);
        const station = statusResponse.ok ? await statusResponse.json() : {};
        const select = elements.dongle;
        select.replaceChildren();
        if (!dongles.length) {
            select.add(new Option('No registered dongles', ''));
            elements.start.disabled = true;
            return;
        }
        const available = [];
        dongles.forEach(dongle => {
            const role = roleName(dongle);
            const option = new Option(
                (dongle.primary ? '★ ' : '') + dongle.label + ' · ' + role +
                    (dongle.connected ? '' : ' · disconnected'),
                dongle.id);
            const hasRole = ['AIS', 'ORBCOMM', 'APRS', 'POCSAG', 'ADSB'].includes(role.toUpperCase());
            const protectedPass = dongle.primary && station.pass_active;
            const unavailable = hasRole || dongle.paused || !dongle.connected ||
                dongle.recording || dongle.manual_recording || protectedPass ||
                (dongle.scan && dongle.scan.active);
            option.disabled = unavailable;
            if (unavailable) option.textContent += protectedPass ? ' · pass protected' : ' · busy';
            else available.push(dongle.id);
            select.add(option);
        });
        const latestUrl = 'tuner.json';
        const latestResponse = await fetch(latestUrl);
        const latest = latestResponse.ok ? await latestResponse.json() : {};
        if (latest.dongle && dongles.some(d => d.id === latest.dongle)) {
            sessionDongle = latest.dongle;
            select.value = latest.dongle;
            active = latest.status !== 'idle';
            render(latest);
        } else if (available.length) {
            select.value = available[0];
        }
        elements.start.disabled = active || available.length === 0;
        updateRecommendation();
        document.getElementById('wave-factor').addEventListener('change', updateRecommendation);
    document.querySelectorAll('input[name="antenna"]').forEach{r => r.addEventListener('change', updateRecommendation)};
    }

    function setFeedback(message, isError) {
        elements.feedback.textContent = message || '';
        elements.feedback.style.color = isError ? 'var(--red)' : 'var(--muted)';
    }

    function renderPlot(points) {
        const canvas = elements.plot;
        const ctx = canvas.getContext('2d');
        const w = canvas.width, h = canvas.height;
        ctx.clearRect(0, 0, w, h);
        ctx.fillStyle = '#0d1726';
        ctx.fillRect(0, 0, w, h);
        const valid = points.filter(p => p.valid && Number.isFinite(p.peak_dbfs));
        if (!valid.length) {
            ctx.fillStyle = '#8996aa';
            ctx.font = '16px monospace';
            ctx.fillText('Peak dBFS will appear after the first measurement', 20, 34);
            return;
        }
        const minX = Math.min(...valid.map(p => p.length_cm));
        const maxX = Math.max(...valid.map(p => p.length_cm));
        const minY = Math.min(...valid.map(p => p.peak_dbfs));
        const maxY = Math.max(...valid.map(p => p.peak_dbfs));
        const x = value => 36 + (maxX === minX ? .5 : (value - minX) / (maxX - minX)) * (w - 60);
        const y = value => h - 28 - (maxY === minY ? .5 : (value - minY) / (maxY - minY)) * (h - 50);
        ctx.strokeStyle = '#315477';
        ctx.beginPath(); ctx.moveTo(36, 12); ctx.lineTo(36, h - 28); ctx.lineTo(w - 18, h - 28); ctx.stroke();
        ctx.fillStyle = '#8996aa'; ctx.font = '12px monospace';
        ctx.fillText(maxY.toFixed(1) + ' dBFS', 42, 18);
        ctx.fillText(minX.toFixed(2) + ' cm', 36, h - 8);
        ctx.fillText(maxX.toFixed(2) + ' cm', w - 100, h - 8);
        const ordered = valid.slice().sort((a, b) => a.length_cm - b.length_cm);
        ctx.strokeStyle = '#53d769'; ctx.lineWidth = 2; ctx.beginPath();
        ordered.forEach((point, i) => i ? ctx.lineTo(x(point.length_cm), y(point.peak_dbfs)) : ctx.moveTo(x(point.length_cm), y(point.peak_dbfs)));
        ctx.stroke();
        ordered.forEach(point => {
            ctx.fillStyle = '#f0a84a'; ctx.beginPath();
            ctx.arc(x(point.length_cm), y(point.peak_dbfs), 4, 0, Math.PI * 2); ctx.fill();
        });
    }

    function renderPoints(points) {
        const body = elements.points;
        body.replaceChildren();
        if (!points.length) {
            const row = body.insertRow();
            const cell = row.insertCell(); cell.colSpan = 7;
            cell.className = 'small'; cell.textContent = 'No measurements yet.';
            renderPlot(points);
            return;
        }
        points.forEach((point, index) => {
            const row = body.insertRow();
            const values = [
                String(index + 1), point.length_cm.toFixed(2) + ' cm',
                point.peak_dbfs == null ? '—' : point.peak_dbfs.toFixed(2) + ' dBFS',
                point.floor_dbfs == null ? '—' : point.floor_dbfs.toFixed(2) + ' dBFS',
                point.ratio_db == null ? '—' : point.ratio_db.toFixed(2) + ' dB',
                point.decodes == null ? 'n/a' : String(point.decodes),
                point.valid ? 'valid' : ('INVALID · ' + (point.invalid_reason || 'no data'))
            ];
            values.forEach((value, i) => {
                const cell = row.insertCell(); cell.textContent = value;
                if (i === 6) cell.className = point.valid ? 'valid' : 'invalid';
            });
        });
        renderPlot(points);
    }

    function render(data) {
        if (!data || !data.session) return;
        active = data.status !== 'idle';
        sessionDongle = data.dongle;
        elements.session.classList.remove('hidden');
        elements.history.classList.remove('hidden');
        elements.initial.classList.remove('hidden');
        elements['initial-cm'].textContent = data.initial_length_cm.toFixed(2) + ' cm';
        elements['initial-mm'].textContent = (data.initial_length_cm * 10).toFixed(1) + ' mm';
        elements.nominal.textContent = 'Quarter-wave nominal: ' + data.nominal_cm.toFixed(2) +
            ' cm' + (data.antenna === 'vdipole' ? ' per leg.' : '.');
        elements.length.value = data.length_cm.toFixed(2);
        elements['element-hint'].textContent = data.antenna === 'vdipole'
            ? 'Enter the length of each equal dipole leg.' : 'Enter the radiating whip length.';
        elements['session-state'].textContent = data.status === 'measuring'
            ? 'Measuring ' + data.service.toUpperCase() + ' · about ' +
                Math.max(0, data.measurement_remaining || data.dwell_secs) + ' s remaining.'
            : 'Session ' + data.status + ' · ' + data.service.toUpperCase() +
                ' · ' + data.dwell_secs + ' s dwell.';
        elements.instruction.textContent = data.instruction;
        elements.instruction.className = data.status === 'converged' ? 'good' :
            (data.retry_only ? 'bad' : '');
        elements.measure.disabled = !active || data.status === 'measuring';
        elements.report-form.classList.toggle('hidden', !active ||
            data.status === 'measuring' || data.status === 'converged' || data.retry_only);
        elements.measure.classList.toggle('hidden', !active || data.status === 'converged');
        elements.stop.disabled = !active;
        const selectedDongle = elements.dongle.selectedOptions[0];
        elements.start.disabled = active || !selectedDongle || selectedDongle.disabled;
        elements.converged.style.display = data.status === 'converged' ? 'block' : 'none';
        if (data.status === 'converged') {
            elements['final-length'].textContent = data.converged_length_cm.toFixed(2) + ' cm';
            elements['final-note'].textContent = data.antenna === 'vdipole'
                ? ' per leg · ' + (data.converged_length_cm * 2).toFixed(2) + ' cm total wire'
                : ' whip element';
        }
        renderPoints(data.points || []);
    }

    async function poll() {
        const did = sessionDongle || elements.dongle.value;
        const params = did ? { d: did } : {};
        try {
            const data = await requestJson('tuner.json', params);
            if (data.session) render(data);
        } catch (error) {
            setFeedback(error.message, true);
        }
    }

    elements.service.addEventListener('change', updateRecommendation);
    document.querySelectorAll('input[name="antenna"]').forEach(input =>
        input.addEventListener('change', updateRecommendation));
    elements.dongle.addEventListener('change', () => {
        sessionDongle = null;
        const selected = elements.dongle.selectedOptions[0];
        elements.start.disabled = active || !selected || selected.disabled;
    });

    elements.start.addEventListener('click', async () => {
        setFeedback('');
        try {
            const data = await requestJson('tuner_start', {
                d: elements.dongle.value, service: elements.service.value,
                antenna: selectedAntenna(),
                wave_factor: selectedWaveFactor()
            }, { method: 'POST' });
            render(data);
        } catch (error) {
            setFeedback(error.message, true);
        }
    });

    elements['report-form'].addEventListener('submit', async event => {
        event.preventDefault();
        setFeedback('');
        try {
            const data = await requestJson('tuner_point', {
                d: sessionDongle, length_cm: elements.length.value,
                note: elements.note.value
            }, { method: 'POST' });
            elements.note.value = '';
            render(data);
        } catch (error) {
            setFeedback(error.message, true);
        }
    });

    elements.measure.addEventListener('click', async () => {
        setFeedback('');
        try {
            render(await requestJson('tuner_measure', { d: sessionDongle }, { method: 'POST' }));
        } catch (error) {
            setFeedback(error.message, true);
        }
    });

    elements.stop.addEventListener('click', async () => {
        setFeedback('');
        try {
            render(await requestJson('tuner_stop', { d: sessionDongle }, { method: 'POST' }));
            active = false;
            sessionDongle = null;
            await loadDongles();
        } catch (error) {
            setFeedback(error.message, true);
        }
    });

    window.addEventListener('pagehide', () => {
        if (!active || !sessionDongle) return;
        const stopUrl = makeUrl('tuner_stop', { d: sessionDongle });
        navigator.sendBeacon(stopUrl, new Blob([], { type: 'text/plain' }));
    });

    updateRecommendation();
        document.getElementById('wave-factor').addEventListener('change', updateRecommendation);
    document.querySelectorAll('input[name="antenna"]').forEach{r => r.addEventListener('change', updateRecommendation)};
    loadDongles().catch(error => setFeedback(error.message, true));
    window.setInterval(poll, 1000);
})();