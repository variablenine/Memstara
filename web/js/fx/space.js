// A small 3D toolkit for the screens' canvases: a canvas that keeps its size
// and pixel density, a frame loop that only runs while it is wanted, an orbit
// camera (drag, pinch, scroll after a click, keys) and cached glow sprites.
// Plain canvas 2D, so it needs no WebGL (the app window turns WebGL off) and
// loads nothing from anywhere else.

export const TAU = Math.PI * 2;
export const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
export const lerp = (a, b, t) => a + (b - a) * t;
export const ease = (t) => (t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2);

export function prefersReduced() {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// "full" moves all the time; "reduced" and "off" draw only when something changes
export function moving(look) {
  return !!look && look.motion === "full" && !prefersReduced();
}

// A canvas sized to its box. draw(ctx, w, h, t, dt) gets CSS pixels; the
// context is already scaled for the screen's pixel density (at most 2x).
export function createStage(canvas, { draw, maxFps = 60, onResize } = {}) {
  const ctx = canvas.getContext("2d");
  const st = { w: 0, h: 0, dpr: 1, running: false, raf: 0, last: 0, t0: performance.now(), queued: false };
  const minGap = 1000 / maxFps - 2;

  function size() {
    const r = canvas.getBoundingClientRect();
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.max(1, Math.round(r.width));
    const h = Math.max(1, Math.round(r.height));
    if (w === st.w && h === st.h && dpr === st.dpr) return false;
    st.w = w; st.h = h; st.dpr = dpr;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    return true;
  }

  function paint(now) {
    const t = (now - st.t0) / 1000;
    const dt = st.last ? Math.min(0.1, (now - st.last) / 1000) : 0;
    st.last = now;
    ctx.setTransform(st.dpr, 0, 0, st.dpr, 0, 0);
    draw(ctx, st.w, st.h, t, dt);
  }

  function loop(now) {
    st.raf = 0;
    if (!st.running) return;
    st.raf = requestAnimationFrame(loop);
    if (st.last && now - st.last < minGap) return;
    paint(now);
  }

  const ro = new ResizeObserver(() => {
    if (size()) {
      if (onResize) onResize(st.w, st.h);
      frame();
    }
  });
  ro.observe(canvas);
  size();

  // one frame now (used when not animating, or after a change)
  function frame() {
    if (st.running || st.queued) return;
    st.queued = true;
    requestAnimationFrame((now) => {
      st.queued = false;
      st.last = 0;
      size();
      paint(now);
    });
  }

  return {
    ctx,
    get w() { return st.w; },
    get h() { return st.h; },
    get running() { return st.running; },
    start() {
      if (st.running) return;
      st.running = true;
      st.last = 0;
      size();
      st.raf = requestAnimationFrame(loop);
    },
    stop() {
      st.running = false;
      if (st.raf) cancelAnimationFrame(st.raf);
      st.raf = 0;
    },
    frame,
  };
}

// An orbit camera around `target`. yaw turns around the vertical axis,
// pitch looks from above (positive) or below; dist is how far away it sits.
// fitWidth (optional, a width:height ratio): on a canvas narrower than that,
// the picture shrinks to keep the same width in view (wide scenes on a phone).
export function createCamera({ yaw = 0, pitch = 0.35, dist = 10, fov = 0.9, target = [0, 0, 0], fitWidth = 0 } = {}) {
  const cam = { yaw, pitch, dist, fov, target: [...target], fitWidth, home: { yaw, pitch, dist } };
  let cy, sy, cp, sp, focal, cx0, cy0;
  // call once per frame before project()
  cam.prepare = (w, h) => {
    cy = Math.cos(cam.yaw); sy = Math.sin(cam.yaw);
    cp = Math.cos(cam.pitch); sp = Math.sin(cam.pitch);
    focal = (h / 2) / Math.tan(cam.fov / 2);
    if (cam.fitWidth && w / h < cam.fitWidth) focal *= (w / h) / cam.fitWidth;
    cx0 = w / 2; cy0 = h / 2;
  };
  // world point -> {x, y, d (distance from the camera), s (pixels per world unit)}, or null behind the camera
  cam.project = (p, out = {}) => {
    const qx = p[0] - cam.target[0], qy = p[1] - cam.target[1], qz = p[2] - cam.target[2];
    const x1 = qx * cy - qz * sy;
    const z1 = qx * sy + qz * cy;
    const y2 = qy * cp - z1 * sp;
    const z2 = qy * sp + z1 * cp;
    const d = cam.dist - z2;
    if (d < 0.05) return null;
    const s = focal / d;
    out.x = cx0 + x1 * s;
    out.y = cy0 - y2 * s;
    out.d = d;
    out.s = s;
    return out;
  };
  cam.reset = () => { cam.yaw = cam.home.yaw; cam.pitch = cam.home.pitch; cam.dist = cam.home.dist; };
  return cam;
}

// Drag to turn, pinch or scroll (once the canvas has been clicked) to zoom,
// tap or click to pick. Keys while the canvas has focus: Shift+arrows turn,
// + and - zoom, 0 puts the view back. Plain arrows are left to the screen
// (to step through what's drawn). A vertical swipe on a phone still scrolls
// the page (touch-action: pan-y), so the canvas never traps the reader.
export function attachOrbit(canvas, cam, { onChange, onTap, onHover, onLeave, minDist = 2, maxDist = 80,
  pitchMin = -1.25, pitchMax = 1.25, onInteract } = {}) {
  const pts = new Map();
  let drag = null;
  let pinch = null;
  let armed = false;   // scroll zooms only after the canvas was clicked, so the page still scrolls past it
  const changed = () => { if (onInteract) onInteract(); if (onChange) onChange(); };
  canvas.style.touchAction = "pan-y";

  const local = (e) => {
    const r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  canvas.addEventListener("pointerdown", (e) => {
    pts.set(e.pointerId, local(e));
    if (pts.size === 1) drag = { ...local(e), moved: 0, id: e.pointerId, yaw: cam.yaw, pitch: cam.pitch };
    if (pts.size === 2) {
      const [a, b] = [...pts.values()];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, dist: cam.dist };
      drag = null;
    }
  });
  canvas.addEventListener("pointermove", (e) => {
    const p = local(e);
    if (pts.has(e.pointerId)) pts.set(e.pointerId, p);
    if (pinch && pts.size >= 2) {
      const [a, b] = [...pts.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      cam.dist = clamp(pinch.dist * (pinch.d / d), minDist, maxDist);
      changed();
      return;
    }
    if (drag && e.pointerId === drag.id) {
      const dx = p.x - drag.x, dy = p.y - drag.y;
      drag.moved = Math.max(drag.moved, Math.hypot(dx, dy));
      if (drag.moved > 4) {
        if (!canvas.hasPointerCapture(e.pointerId)) canvas.setPointerCapture(e.pointerId);
        cam.yaw = drag.yaw - dx * 0.006;
        // a mouse also tilts; a finger only turns (vertical swipes scroll the page)
        if (e.pointerType !== "touch") cam.pitch = clamp(drag.pitch + dy * 0.005, pitchMin, pitchMax);
        changed();
      }
      return;
    }
    if (onHover && e.pointerType === "mouse") onHover(p.x, p.y, e);
  });
  const end = (e) => {
    const was = drag;
    pts.delete(e.pointerId);
    if (pts.size < 2) pinch = null;
    if (was && e.pointerId === was.id) {
      drag = null;
      if (was.moved <= 4 && e.type === "pointerup" && onTap) {
        const p = local(e);
        onTap(p.x, p.y, e);
      }
    }
  };
  canvas.addEventListener("pointerup", end);
  canvas.addEventListener("pointercancel", end);
  canvas.addEventListener("pointerleave", (e) => { if (e.pointerType === "mouse" && onLeave) onLeave(); });
  canvas.addEventListener("click", () => { armed = true; });
  canvas.addEventListener("blur", () => { armed = false; });
  canvas.addEventListener("wheel", (e) => {
    if (!armed && !e.ctrlKey) return;   // let the page scroll
    e.preventDefault();
    cam.dist = clamp(cam.dist * Math.exp(e.deltaY * 0.0012), minDist, maxDist);
    changed();
  }, { passive: false });
  canvas.addEventListener("keydown", (e) => {
    let used = true;
    if (e.shiftKey && e.key === "ArrowLeft") cam.yaw += 0.12;
    else if (e.shiftKey && e.key === "ArrowRight") cam.yaw -= 0.12;
    else if (e.shiftKey && e.key === "ArrowUp") cam.pitch = clamp(cam.pitch + 0.08, pitchMin, pitchMax);
    else if (e.shiftKey && e.key === "ArrowDown") cam.pitch = clamp(cam.pitch - 0.08, pitchMin, pitchMax);
    else if (e.key === "+" || e.key === "=") cam.dist = clamp(cam.dist / 1.15, minDist, maxDist);
    else if (e.key === "-" || e.key === "_") cam.dist = clamp(cam.dist * 1.15, minDist, maxDist);
    else if (e.key === "0") cam.reset();
    else used = false;
    if (used) { e.preventDefault(); changed(); }
  });
  return {
    zoom(f) { cam.dist = clamp(cam.dist * f, minDist, maxDist); changed(); },
    reset() { cam.reset(); changed(); },
    get dragging() { return !!drag && drag.moved > 4; },
  };
}

// Soft round glow, drawn once per colour and size, then stamped with
// drawImage (much cheaper than shadowBlur every frame).
const sprites = new Map();
export function glowSprite(rgb, radius, strength = 0.6) {
  const r = Math.max(2, Math.round(radius));
  const key = `${rgb}|${r}|${strength}`;
  let c = sprites.get(key);
  if (c) return c;
  c = document.createElement("canvas");
  c.width = c.height = r * 2;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(r, r, 0, r, r, r);
  grad.addColorStop(0, `rgba(${rgb}, ${strength})`);
  grad.addColorStop(0.25, `rgba(${rgb}, ${strength * 0.45})`);
  grad.addColorStop(1, `rgba(${rgb}, 0)`);
  g.fillStyle = grad;
  g.fillRect(0, 0, r * 2, r * 2);
  if (sprites.size > 400) sprites.clear();
  sprites.set(key, c);
  return c;
}

export function stamp(ctx, sprite, x, y, size) {
  ctx.drawImage(sprite, x - size / 2, y - size / 2, size, size);
}

// "r, g, b" of a CSS custom property on :root (for canvas colours that follow the
// look). Takes "r, g, b" tokens as they are and turns #rgb / #rrggbb into that form.
export function cssRGB(name, fallback = "126, 240, 164") {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  if (!v) return fallback;
  const hex = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(v);
  if (!hex) return v;
  const h = hex[1].length === 3 ? [...hex[1]].map((c) => c + c).join("") : hex[1];
  return [0, 2, 4].map((i) => parseInt(h.slice(i, i + 2), 16)).join(", ");
}

// the nearest of `items` (each with .sx, .sy, .sr) within reach of x, y
export function pick(items, x, y, reach = 14) {
  let best = null;
  let bd = Infinity;
  for (const it of items) {
    if (it.sx === undefined || it.hidden) continue;
    const d = Math.hypot(it.sx - x, it.sy - y) - (it.sr || 0);
    if (d < reach && d < bd) { bd = d; best = it; }
  }
  return best;
}
