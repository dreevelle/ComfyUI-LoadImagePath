// Crop and pad on the Load Image From Path preview.
//
// Drag on the picture to crop it, drag inside the box to move it, drag a handle
// to resize it, double-click to reset. crop_ratio locks the shape. Pads show as
// bands in the fill colour around the crop, and the size caption reads
// "source → output".
//
// Core draws a loader's preview through its `$$canvas-image-preview` widget, and
// draws the picture in a deferred pass after the node, so an overlay painted in
// onDrawForeground would end up underneath it. For this node only, the widget's
// drawWidget is wrapped to draw the picture itself — with the same rule core
// uses: fitted, centred, never upscaled, a 15px caption band when
// Comfy.Node.AllowImageSizeDraw is on — plus the crop and pads, and its
// onPointerDown is wrapped so a press on the picture edits the crop instead of
// dragging the node.
//
// Everything else stays core's: node.imgs, the mask editor, clipspace and the
// `image` combo are untouched, and the crop only ever lives in the crop_* widgets
// the Python side reads. If anything here throws, the wrapper falls back to
// core's own drawing and pointer handling, so the worst case is a plain loader.

import { app } from "../../scripts/app.js";

const NODE_NAME = "LoadImageFromPath";
const PREVIEW_WIDGET = "$$canvas-image-preview";
const SIZE_SETTING = "Comfy.Node.AllowImageSizeDraw";
const CAPTION_HEIGHT = 15;
const MIN_CROP = 8;
const RATIOS = {
  "1:1": 1, "2:3": 2 / 3, "3:2": 3 / 2, "3:4": 3 / 4, "4:3": 4 / 3,
  "4:5": 4 / 5, "5:4": 5 / 4, "9:16": 9 / 16, "16:9": 16 / 9,
};

const patched = new WeakSet();
const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

const widget = (node, name) => node.widgets?.find((w) => w.name === name);
const value = (node, name, fallback = 0) => {
  const v = widget(node, name)?.value;
  if (typeof fallback === "number") return Number.isFinite(Number(v)) ? Number(v) : fallback;
  return v ?? fallback;
};

function setValues(node, values) {
  for (const [name, v] of Object.entries(values)) {
    const w = widget(node, name);
    if (!w) continue;
    w.value = v;
    w.callback?.(v);
  }
  node.setDirtyCanvas?.(true, true);
}

function currentImage(node) {
  const img = node.imgs?.[node.imageIndex ?? 0];
  return img?.naturalWidth && img?.naturalHeight ? img : null;
}

function sizeDrawEnabled() {
  return app.ui?.settings?.getSettingValue?.(SIZE_SETTING) ?? true;
}

function fillStyle(node) {
  const raw = String(value(node, "fill_color", "#808080")).trim();
  return /^#?([0-9a-f]{3}|[0-9a-f]{6})$/i.test(raw) ? (raw.startsWith("#") ? raw : `#${raw}`) : "#808080";
}

// Where everything sits, in node-local coordinates. Mirrors the Python clamps,
// so the box drawn is the box that runs.
function layout(node, previewWidget, img, width) {
  const nw = img.naturalWidth, nh = img.naturalHeight;
  const cx = clamp(value(node, "crop_x"), 0, nw - 1);
  const cy = clamp(value(node, "crop_y"), 0, nh - 1);
  const rawW = value(node, "crop_width"), rawH = value(node, "crop_height");
  const cw = rawW > 0 ? Math.min(rawW, nw - cx) : nw - cx;
  const ch = rawH > 0 ? Math.min(rawH, nh - cy) : nh - cy;
  const [pl, pt, pr, pb] = ["pad_left", "pad_top", "pad_right", "pad_bottom"].map((n) => Math.max(0, value(node, n)));

  // Fit the picture and the padded crop together, so pads past the picture's edge show.
  const minX = Math.min(0, cx - pl), minY = Math.min(0, cy - pt);
  const maxX = Math.max(nw, cx + cw + pr), maxY = Math.max(nh, cy + ch + pb);
  const bw = maxX - minX, bh = maxY - minY;
  const band = sizeDrawEnabled() ? CAPTION_HEIGHT : 0;
  const areaW = width ?? node.size[0];
  const areaH = Math.max(1, (previewWidget.computedHeight ?? 0) - band);
  const s = Math.min(areaW / bw, areaH / bh, 1);
  const ox = (areaW - bw * s) / 2;
  const oy = previewWidget.y + (areaH - bh * s) / 2;
  const map = (x, y) => [ox + (x - minX) * s, oy + (y - minY) * s];
  const toSource = (x, y) => [(x - ox) / s + minX, (y - oy) / s + minY];

  return {
    nw, nh, cx, cy, cw, ch, pl, pt, pr, pb, s, map, toSource, band,
    imgRect: [...map(0, 0), nw * s, nh * s],
    cropRect: [...map(cx, cy), cw * s, ch * s],
    outRect: [...map(cx - pl, cy - pt), (cw + pl + pr) * s, (ch + pt + pb) * s],
    bottom: oy + bh * s,
    cropped: cx !== 0 || cy !== 0 || cw !== nw || ch !== nh,
    padded: pl > 0 || pt > 0 || pr > 0 || pb > 0,
  };
}

function handleSize() {
  return clamp(8 / (app.canvas?.ds?.scale || 1), 4, 24);
}

function handles(L) {
  const [x, y, w, h] = L.cropRect;
  return {
    nw: [x, y], n: [x + w / 2, y], ne: [x + w, y], e: [x + w, y + h / 2],
    se: [x + w, y + h], s: [x + w / 2, y + h], sw: [x, y + h], w: [x, y + h / 2],
  };
}

// The rectangle `outer` minus the rectangle `inner`, as up to four strips.
function ring(ctx, outer, inner) {
  const [ox, oy, ow, oh] = outer, [ix, iy, iw, ih] = inner;
  const strips = [
    [ox, oy, ow, iy - oy], [ox, iy + ih, ow, oy + oh - (iy + ih)],
    [ox, iy, ix - ox, ih], [ix + iw, iy, ox + ow - (ix + iw), ih],
  ];
  for (const [x, y, w, h] of strips) if (w > 0 && h > 0) ctx.fillRect(x, y, w, h);
}

function draw(ctx, node, previewWidget, img, width) {
  const L = layout(node, previewWidget, img, width);
  ctx.save();
  ctx.drawImage(img, ...L.imgRect);
  if (L.cropped) {
    ctx.fillStyle = "rgba(0, 0, 0, 0.55)";
    ring(ctx, L.imgRect, L.cropRect);
  }
  if (L.padded) {
    ctx.fillStyle = fillStyle(node);
    ring(ctx, L.outRect, L.cropRect);
    ctx.setLineDash([4, 3]);
    ctx.strokeStyle = "rgba(255, 255, 255, 0.6)";
    ctx.lineWidth = 1;
    ctx.strokeRect(...L.outRect);
    ctx.setLineDash([]);
  }
  if (L.cropped || L.padded) {
    ctx.strokeStyle = "#4aa3ff";
    ctx.lineWidth = 1.5;
    ctx.strokeRect(...L.cropRect);
  }
  if (L.cropped) {
    const hs = handleSize();
    ctx.fillStyle = "#4aa3ff";
    for (const [x, y] of Object.values(handles(L))) ctx.fillRect(x - hs / 2, y - hs / 2, hs, hs);
  }
  if (L.band) {
    const outW = L.cw + L.pl + L.pr, outH = L.ch + L.pt + L.pb;
    const text = L.cropped || L.padded
      ? `${L.nw} × ${L.nh} → ${outW} × ${outH}`
      : `${L.nw} × ${L.nh}`;
    ctx.fillStyle = window.LiteGraph?.NODE_TEXT_COLOR ?? "#AAA";
    ctx.textAlign = "center";
    ctx.font = "10px sans-serif";
    ctx.fillText(text, (width ?? node.size[0]) / 2, L.bottom + 10);
  }
  ctx.restore();
}

// New box from a drag, or a box corner anchored opposite (ax, ay), kept on the
// picture and, when a ratio is set, to that shape.
function boxFrom(ax, ay, qx, qy, L, ratio) {
  let x1 = clamp(qx, 0, L.nw), y1 = clamp(qy, 0, L.nh);
  let w = Math.abs(x1 - ax), h = Math.abs(y1 - ay);
  const dx = x1 >= ax ? 1 : -1, dy = y1 >= ay ? 1 : -1;
  if (ratio) {
    if (w / Math.max(h, 1e-6) > ratio) w = h * ratio; else h = w / ratio;
    // room left on the picture in the drag direction
    const roomW = dx > 0 ? L.nw - ax : ax, roomH = dy > 0 ? L.nh - ay : ay;
    const k = Math.min(1, roomW / Math.max(w, 1e-6), roomH / Math.max(h, 1e-6));
    w *= k; h *= k;
  }
  return { x: dx > 0 ? ax : ax - w, y: dy > 0 ? ay : ay - h, w, h };
}

// Resize from one edge, keeping the box centred on the other axis.
function edgeResize(edge, start, qx, qy, L, ratio) {
  let { x, y, w, h } = start;
  if (edge === "e") w = clamp(qx, x + MIN_CROP, L.nw) - x;
  if (edge === "w") { const r = x + w; x = clamp(qx, 0, r - MIN_CROP); w = r - x; }
  if (edge === "s") h = clamp(qy, y + MIN_CROP, L.nh) - y;
  if (edge === "n") { const b = y + h; y = clamp(qy, 0, b - MIN_CROP); h = b - y; }
  if (ratio) {
    if (edge === "e" || edge === "w") {
      const cy = start.y + start.h / 2;
      h = w / ratio;
      const maxH = 2 * Math.min(cy, L.nh - cy);
      if (h > maxH) { h = maxH; const nw = h * ratio; if (edge === "w") x += w - nw; w = nw; }
      y = cy - h / 2;
    } else {
      const cx = start.x + start.w / 2;
      w = h * ratio;
      const maxW = 2 * Math.min(cx, L.nw - cx);
      if (w > maxW) { w = maxW; const nh = w / ratio; if (edge === "n") y += h - nh; h = nh; }
      x = cx - w / 2;
    }
  }
  return { x, y, w, h };
}

function commit(node, L, box) {
  let x = clamp(Math.round(box.x), 0, L.nw - 1);
  let y = clamp(Math.round(box.y), 0, L.nh - 1);
  let w = clamp(Math.round(box.w), 1, L.nw - x);
  let h = clamp(Math.round(box.h), 1, L.nh - y);
  if (w < MIN_CROP || h < MIN_CROP) return;
  setValues(node, { crop_x: x, crop_y: y, crop_width: w, crop_height: h });
}

function resetCrop(node) {
  setValues(node, { crop_x: 0, crop_y: 0, crop_width: 0, crop_height: 0 });
}

// The largest box of the locked shape inside the current crop, centred on it.
function snapToRatio(node) {
  const img = currentImage(node);
  const pw = node.widgets?.find((w) => w.name === PREVIEW_WIDGET);
  const ratio = RATIOS[value(node, "crop_ratio", "free")];
  if (!img || !pw || !ratio) return;
  const L = layout(node, pw, img);
  let w = L.cw, h = L.ch;
  if (w / h > ratio) w = h * ratio; else h = w / ratio;
  commit(node, L, { x: L.cx + (L.cw - w) / 2, y: L.cy + (L.ch - h) / 2, w, h });
}

function onPress(node, previewWidget, pointer) {
  const down = pointer.eDown;
  if (!down || down.button !== 0) return false;
  const img = currentImage(node);
  if (!img) return false;
  const L = layout(node, previewWidget, img);
  const lx = down.canvasX - node.pos[0], ly = down.canvasY - node.pos[1];
  const [px, py] = L.toSource(lx, ly);

  const hs = handleSize() * 1.5;
  const hit = L.cropped
    ? Object.entries(handles(L)).find(([, [x, y]]) => Math.abs(lx - x) <= hs && Math.abs(ly - y) <= hs)?.[0]
    : null;
  const onPicture = px >= 0 && px <= L.nw && py >= 0 && py <= L.nh;
  if (!hit && !onPicture) return false; // the pads and margins still drag the node

  const ratio = RATIOS[value(node, "crop_ratio", "free")];
  const start = { x: L.cx, y: L.cy, w: L.cw, h: L.ch };
  const inCrop = px >= L.cx && px <= L.cx + L.cw && py >= L.cy && py <= L.cy + L.ch;
  const mode = hit ?? (L.cropped && inCrop ? "move" : "new");
  const anchor = {
    nw: [start.x + start.w, start.y + start.h], ne: [start.x, start.y + start.h],
    se: [start.x, start.y], sw: [start.x + start.w, start.y],
  };

  pointer.onDrag = (e) => {
    const [qx, qy] = L.toSource(e.canvasX - node.pos[0], e.canvasY - node.pos[1]);
    let box;
    if (mode === "move") {
      box = {
        x: clamp(start.x + qx - px, 0, L.nw - start.w),
        y: clamp(start.y + qy - py, 0, L.nh - start.h),
        w: start.w, h: start.h,
      };
    } else if (mode === "new") {
      box = boxFrom(clamp(px, 0, L.nw), clamp(py, 0, L.nh), qx, qy, L, ratio);
    } else if (anchor[mode]) {
      box = boxFrom(anchor[mode][0], anchor[mode][1], qx, qy, L, ratio);
    } else {
      box = edgeResize(mode, start, qx, qy, L, ratio);
    }
    commit(node, L, box);
  };
  pointer.onDoubleClick = () => resetCrop(node);
  pointer.onClick = () => {}; // a press without a drag changes nothing
  return true;
}

function patchPreview(node) {
  const previewWidget = node.widgets?.find((w) => w.name === PREVIEW_WIDGET);
  if (!previewWidget || patched.has(previewWidget)) return;
  patched.add(previewWidget);

  const coreDraw = previewWidget.drawWidget?.bind(previewWidget);
  const corePress = previewWidget.onPointerDown?.bind(previewWidget);

  previewWidget.drawWidget = function (ctx, options) {
    const img = currentImage(node);
    if (!img) return coreDraw?.(ctx, options);
    try {
      draw(ctx, node, this, img, options?.width);
    } catch (error) {
      console.warn("[LoadImagePath] crop preview failed, using core's", error);
      return coreDraw?.(ctx, options);
    }
  };
  previewWidget.onPointerDown = function (pointer, ...rest) {
    try {
      if (onPress(node, this, pointer)) return true;
    } catch (error) {
      console.warn("[LoadImagePath] crop drag failed", error);
    }
    return corePress?.(pointer, ...rest);
  };
}

app.registerExtension({
  name: "dreevelle.LoadImagePath.Crop",

  nodeCreated(node) {
    if ((node.comfyClass ?? node.type) !== NODE_NAME) return;

    // Core adds the preview widget only once a picture has loaded, and again on
    // every new picture, so check before each draw.
    const onDrawBackground = node.onDrawBackground;
    node.onDrawBackground = function () {
      try { patchPreview(node); } catch (error) { console.warn("[LoadImagePath] crop setup failed", error); }
      return onDrawBackground?.apply(this, arguments);
    };

    // A new picture gets a fresh crop; the old numbers belong to the old one.
    const imageWidget = widget(node, "image");
    if (imageWidget) {
      let previous = imageWidget.value;
      // A loaded workflow sets the value without the callback; take it as the
      // baseline so its saved crop survives the first preview refresh.
      const onConfigure = node.onConfigure;
      node.onConfigure = function () {
        const result = onConfigure?.apply(this, arguments);
        previous = imageWidget.value;
        return result;
      };
      const callback = imageWidget.callback;
      imageWidget.callback = function (v, ...rest) {
        const result = callback?.apply(this, [v, ...rest]);
        if (v !== previous) {
          previous = v;
          resetCrop(node);
        }
        return result;
      };
    }

    const ratioWidget = widget(node, "crop_ratio");
    if (ratioWidget) {
      const callback = ratioWidget.callback;
      ratioWidget.callback = function (...args) {
        const result = callback?.apply(this, args);
        snapToRatio(node);
        return result;
      };
    }
  },
});
