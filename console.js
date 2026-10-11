const el = document.getElementById('console');
const countEl = document.getElementById('count');
function escapeHtml(s) {
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}
async function poll() {
    try {
        const res = await fetch('/console.json');
        const lines = await res.json();
        const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
        el.innerHTML = lines.map(l =>
            '<div class="line ' + l.level + '"><span class="t">' + l.time + '</span>' + escapeHtml(l.msg) + '</div>'
        ).join('');
        countEl.textContent = lines.length + ' messages';
        if (document.getElementById('autoscroll').checked && atBottom) el.scrollTop = el.scrollHeight;
    } catch(e) {}
}
poll();
setInterval(poll, 2000);
