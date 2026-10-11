const el = document.getElementById('console');
const countEl = document.getElementById('count');
function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
// Console render — shared by the /console.json fallback poll and the WS
// 'console' topic push
function applyLines(lines) {
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    el.innerHTML = lines.map(l =>
        '<div class="line ' + l.level + '"><span class="t">' + l.time + '</span>' + escapeHtml(l.msg) + '</div>'
    ).join('');
    countEl.textContent = lines.length + ' messages';
    if (document.getElementById('autoscroll').checked && atBottom) el.scrollTop = el.scrollHeight;
}
async function poll() {
    try { applyLines(await (await fetch('/console.json')).json()); } catch(e) {}
}
poll();

// ---------- WebSocket live updates (JSON topic push over /waterfall.ws) ----
// Subscribes to the console topic; the HTTP polling above keeps
// running as the fallback only while the socket is down.
let conSocket = null, conWsBackoff = 2000;
function conWsLive() { return conSocket && conSocket.readyState === 1; }
function connectConWs() {
    try {
        const proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
        const ws = new WebSocket(proto + location.host + '/waterfall.ws');
        conSocket = ws;
        ws.binaryType = 'arraybuffer';
        ws.onopen = () => { conWsBackoff = 2000; try { ws.send('sub=console'); } catch (e) {} };
        ws.onmessage = ev => {
            if (typeof ev.data === 'string') return;
            const u = new Uint8Array(ev.data);
            if (u.length < 4 || u[0] !== 2) return;
            const nlen = (u[1] << 8) | u[2];
            if (u.length < 3 + nlen) return;
            const topic = new TextDecoder().decode(u.subarray(3, 3 + nlen));
            if (topic !== 'console') return;
            try { applyLines(JSON.parse(new TextDecoder().decode(u.subarray(3 + nlen)))); }
            catch (e) { console.error('WS console push failed:', e); }
        };
        ws.onclose = () => {
            conSocket = null;
            setTimeout(connectConWs, conWsBackoff);
            conWsBackoff = Math.min(60000, conWsBackoff * 2);
        };
        ws.onerror = () => { try { ws.close(); } catch (e) {} };
    } catch (e) {
        conSocket = null;
        setTimeout(connectConWs, conWsBackoff);
        conWsBackoff = Math.min(60000, conWsBackoff * 2);
    }
}
connectConWs();
setInterval(() => { if (!conWsLive()) poll(); }, 2000);
