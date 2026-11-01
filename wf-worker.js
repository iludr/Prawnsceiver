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
//   {type:'row',  id, buf}      live row: scroll 1px down, put the new row
//   {type:'put',  id, buf, h}   putImageData a finished full frame (replay)
//   {type:'mark', id, x0, w}    red AIS frame box across the canvas top
//   {type:'clear', id}          blacken the canvas

const WF_WIDTH = 512, WF_HEIGHT = 300;
const blocks = {};   // dongle id -> OffscreenCanvasRenderingContext2D
const slate = new OffscreenCanvas(WF_WIDTH, WF_HEIGHT);      // scroll slate
const slateCtx = slate.getContext('2d');

onmessage = e => {
    const m = e.data;
    if (m.type === 'init') {
        blocks[m.id] = m.canvas.getContext('2d');
        return;
    }
    const ctx = blocks[m.id];
    if (!ctx) return;
    if (m.type === 'row') {
        // live row: save the canvas on the slate, blit it back shifted
        // 1px down (never self-referential — see dashboard.js
        // drawWaterfall), then write the new row into the vacated top
        slateCtx.drawImage(ctx.canvas, 0, 0);
        ctx.drawImage(slate, 0, 1);
        ctx.putImageData(new ImageData(new Uint8ClampedArray(m.buf),
                                       WF_WIDTH, 1), 0, 0);
    } else if (m.type === 'put') {
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
