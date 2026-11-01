'use strict';
// wf-worker.js — dedicated waterfall paint thread for the prawnceiver
// dashboard.
//
// The main thread keeps the signal math (dB LUT mapping, time-range
// decimation, replay) and ships FINISHED RGBA ImageData buffers here;
// this thread owns each block's visible canvas (transferred
// OffscreenCanvas) and executes every canvas operation, so pixel work
// never competes with the WebSocket feed, layout or the JSON topic
// handlers on the main thread.
//
// Message API (id = dongle id "host:port"):
//   {type:'init', id, canvas}   register a block's transferred canvas
//   {type:'put',  id, buf, h}   putImageData a finished RGBA frame
//                               (h = 1 for a live row, 300 for a replay)
//   {type:'mark', id, x0, w}    red AIS frame box across the canvas top
//   {type:'clear', id}          blacken the canvas

const WF_WIDTH = 512, WF_HEIGHT = 300;
const blocks = {};   // dongle id -> OffscreenCanvasRenderingContext2D

onmessage = e => {
    const m = e.data;
    if (m.type === 'init') {
        blocks[m.id] = m.canvas.getContext('2d');
        return;
    }
    const ctx = blocks[m.id];
    if (!ctx) return;
    if (m.type === 'put') {
        ctx.putImageData(new ImageData(new Uint8ClampedArray(m.buf),
                                       WF_WIDTH, m.h || WF_HEIGHT), 0, 0);
    } else if (m.type === 'mark') {
        ctx.fillStyle = 'rgba(231, 76, 60, 0.95)';
        ctx.fillRect(m.x0, 0, m.w, 3);
    } else if (m.type === 'clear') {
        ctx.fillStyle = '#000';
        ctx.fillRect(0, 0, WF_WIDTH, WF_HEIGHT);
    }
};
