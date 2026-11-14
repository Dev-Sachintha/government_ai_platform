"""Talking-avatar JavaScript (plain string, single braces) used by chat_app._render_message_tools.

When 🔊 Listen is pressed the avatar panel pops up automatically and the character's
mouth opens and closes while the browser speaks. Image: static/avatar.jpg (720x720).
"""

AVATAR_JS = r"""
function getAvatar() {
  const host = (function () { try { if (window.parent && window.parent.document) return window.parent; } catch (e) {} return window; })();
  if (host.__govAvatar) return host.__govAvatar;

  const doc = host.document;
  const wrap = doc.createElement('div');
  wrap.id = 'gov-avatar';
  wrap.style.cssText = 'position:fixed;right:22px;bottom:22px;width:min(300px,70vw);z-index:99999;border-radius:20px;overflow:hidden;' +
    'background:#0b1030;box-shadow:0 18px 50px rgba(0,0,0,.45);border:2px solid rgba(255,255,255,.4);display:none;opacity:0;' +
    'transform:translateY(24px) scale(.96);transition:opacity .3s ease,transform .3s ease;font-family:Inter,system-ui,sans-serif;';
  wrap.innerHTML = '<canvas width="720" height="720" style="width:100%;display:block"></canvas>' +
    '<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;color:#fff;font-size:12px;font-weight:600;background:rgba(0,0,0,.6)">' +
    '<span>🔊 Speaking…</span><button type="button" title="Stop" style="background:rgba(255,255,255,.18);color:#fff;border:none;border-radius:999px;padding:3px 10px;cursor:pointer;font-size:12px">⏹ Stop</button></div>';
  doc.body.appendChild(wrap);

  const canvas = wrap.querySelector('canvas');
  const ctx = canvas.getContext('2d');
  const img = new Image();
  let ready = false, strip = null;

  // Mouth geometry (avatar pixels): split line under the upper lip, jaw strip that drops down.
  const CX = 354, YS = 541, SX = 314, SW = 80, SH = 109, MAXDY = 30;

  img.onload = function () {
    strip = doc.createElement('canvas'); strip.width = SW; strip.height = SH;
    const sctx = strip.getContext('2d');
    sctx.drawImage(img, SX, YS, SW, SH, 0, 0, SW, SH);
    try {
      const mask = doc.createElement('canvas'); mask.width = SW; mask.height = SH;
      const mctx = mask.getContext('2d');
      mctx.filter = 'blur(5px)';
      mctx.fillStyle = '#fff';
      mctx.beginPath();
      if (mctx.roundRect) mctx.roundRect(6, 0, SW - 12, SH - 8, 30); else mctx.rect(6, 0, SW - 12, SH - 8);
      mctx.fill();
      sctx.globalCompositeOperation = 'destination-in';
      sctx.drawImage(mask, 0, 0);
    } catch (e) { /* feathering is optional */ }
    ready = true;
    draw(0);
  };
  img.src = host.location.origin + '/app/static/avatar.jpg';

  function draw(m) {
    if (!ready) return;
    ctx.drawImage(img, 0, 0);
    const dy = Math.round(MAXDY * m);
    if (dy < 1) return;
    ctx.fillStyle = 'rgb(34,8,10)';
    ctx.beginPath(); ctx.ellipse(CX, YS + (dy - 2) / 2, 27, (dy + 12) / 2, 0, 0, Math.PI * 2); ctx.fill();
    if (m > 0.35) { ctx.fillStyle = 'rgb(222,210,190)'; ctx.fillRect(CX - 17, YS - 2, 34, 6); }
    ctx.fillStyle = 'rgb(120,40,48)';
    ctx.beginPath(); ctx.ellipse(CX, YS + dy - 5, 13, 6, 0, 0, Math.PI * 2); ctx.fill();
    ctx.drawImage(strip, SX, YS + dy);
  }

  let speaking = false, m = 0, lastBoundary = 0, raf = 0, hideTimer = 0;
  function frame(t) {
    let target = 0;
    if (speaking) {
      const since = t - lastBoundary;
      const base = 0.30 + 0.45 * Math.abs(Math.sin(t / 110)) * (0.7 + 0.3 * Math.sin(t / 37));
      const spike = since < 140 ? 1 - since / 140 : 0;
      target = Math.min(1, base + 0.35 * spike);
    }
    m += (target - m) * 0.4;
    draw(m);
    if (speaking || m > 0.03) raf = host.requestAnimationFrame(frame);
    else { raf = 0; draw(0); }
  }

  const api = {
    start: function () {
      host.clearTimeout(hideTimer);
      speaking = true;
      wrap.style.display = 'block';
      host.requestAnimationFrame(function () { wrap.style.opacity = '1'; wrap.style.transform = 'none'; });
      if (!raf) raf = host.requestAnimationFrame(frame);
    },
    pulse: function () { lastBoundary = host.performance.now(); },
    stop: function () {
      speaking = false;
      wrap.style.opacity = '0'; wrap.style.transform = 'translateY(24px) scale(.96)';
      hideTimer = host.setTimeout(function () { wrap.style.display = 'none'; }, 350);
    },
    onUserStop: null
  };
  wrap.querySelector('button').addEventListener('click', function () {
    try { host.speechSynthesis.cancel(); } catch (e) {}
    api.stop();
  });
  host.__govAvatar = api;
  return api;
}
"""
