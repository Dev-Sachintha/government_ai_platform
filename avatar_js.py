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


# ---------------------------------------------------------------------------
# Copy / Listen / Stop handler. Installed ONCE per page; it listens for clicks on
# any button[data-action] inside a [data-gov-tools] block, so it does not matter
# when Streamlit runs each message's <script>. The message text is read from the
# block's data-text attribute.
# ---------------------------------------------------------------------------
TOOLS_JS = AVATAR_JS + r"""
(function () {
  const hosts = [window];
  try { if (window.parent && window.parent !== window && window.parent.document) hosts.push(window.parent); } catch (e) {}

  function detectLanguage(v) {
    if (/[\u0D80-\u0DFF]/.test(v)) return 'si-LK';
    if (/[\u0B80-\u0BFF]/.test(v)) return 'ta-IN';
    if (/[\u0900-\u097F]/.test(v)) return 'hi-IN';
    if (/[\u0600-\u06FF]/.test(v)) return 'ar-SA';
    if (/[\u4E00-\u9FFF]/.test(v)) return 'zh-CN';
    if (/[\u3040-\u30FF]/.test(v)) return 'ja-JP';
    if (/[\uAC00-\uD7AF]/.test(v)) return 'ko-KR';
    if (/[\u0400-\u04FF]/.test(v)) return 'ru-RU';
    return 'en-US';
  }

  function cleanForSpeech(t) {
    return t.replace(/```[\s\S]*?```/g, ' ').replace(/https?:\/\/\S+/g, ' ')
            .replace(/[#*_`>|~]/g, ' ').replace(/\s+/g, ' ').trim();
  }

  // Split into short pieces: Chrome silently stops long utterances after ~15 s.
  function chunkText(t) {
    const parts = t.match(/[^.!?\u0964\u0DF4\n]+[.!?\u0964\u0DF4]*/g) || [t];
    const out = []; let cur = '';
    parts.forEach(function (p) {
      if ((cur + p).length > 180 && cur) { out.push(cur.trim()); cur = p; } else { cur += p; }
    });
    if (cur.trim()) out.push(cur.trim());
    return out;
  }

  let session = 0;

  function stopSpeech(root) {
    session++;
    try { window.speechSynthesis.cancel(); } catch (e) {}
    try { getAvatar().stop(); } catch (e) {}
    if (root) setStatus(root, 'Speech stopped.');
  }

  function setStatus(root, msg) {
    const el = root && root.querySelector('.gov-status');
    if (el) el.textContent = msg;
  }

  function speak(root, text) {
    if (!('speechSynthesis' in window) || !('SpeechSynthesisUtterance' in window)) {
      setStatus(root, 'Speech is not supported by this browser. Use Chrome or Edge.');
      return;
    }
    const spoken = cleanForSpeech(text);
    if (!spoken) { setStatus(root, 'Nothing to read.'); return; }
    const language = detectLanguage(spoken);
    const pieces = chunkText(spoken);
    const mySession = ++session;

    try { window.speechSynthesis.cancel(); } catch (e) {}
    let avatar = null;
    try { avatar = getAvatar(); avatar.start(); } catch (e) { avatar = null; }
    setStatus(root, 'Starting…');

    function pickVoice() {
      const voices = window.speechSynthesis.getVoices() || [];
      const base = language.split('-')[0].toLowerCase();
      return voices.find(function (v) { return v.lang && v.lang.toLowerCase().startsWith(base); }) || null;
    }

    function next(i) {
      if (mySession !== session) return;
      if (i >= pieces.length) {
        setStatus(root, '');
        if (avatar) avatar.stop();
        return;
      }
      const u = new SpeechSynthesisUtterance(pieces[i]);
      u.lang = language; u.rate = 1;
      const v = pickVoice(); if (v) u.voice = v;
      u.onstart = function () { if (mySession === session) setStatus(root, 'Speaking (' + language + ')…'); };
      u.onboundary = function () { if (avatar) avatar.pulse(); };
      u.onend = function () { next(i + 1); };
      u.onerror = function (ev) {
        if (mySession !== session) return;
        if (ev && (ev.error === 'canceled' || ev.error === 'interrupted')) return;
        setStatus(root, 'Speech error: ' + (ev && ev.error ? ev.error : 'unknown') +
          (language !== 'en-US' ? ' (is a ' + language + ' voice installed?)' : ''));
        if (avatar) avatar.stop();
      };
      window.speechSynthesis.speak(u);
      if (window.speechSynthesis.paused) window.speechSynthesis.resume();
    }
    // Chrome needs a short pause between cancel() and speak().
    setTimeout(function () { next(0); }, 120);
  }

  async function copyText(root, text) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const f = document.createElement('textarea');
        f.value = text; f.setAttribute('readonly', '');
        f.style.position = 'fixed'; f.style.left = '-9999px';
        document.body.appendChild(f); f.select();
        const ok = document.execCommand('copy'); f.remove();
        if (!ok) throw new Error('Clipboard unavailable');
      }
      setStatus(root, 'Copied!');
    } catch (e) { setStatus(root, 'Copy failed — check browser permissions.'); }
  }

  function onClick(ev) {
    const t = ev.target;
    const btn = t && t.closest ? t.closest('button[data-action]') : null;
    if (!btn) return;
    const root = btn.closest('[data-gov-tools]');
    if (!root) return;
    const action = btn.getAttribute('data-action');
    const text = root.getAttribute('data-text') || '';
    try {
      if (action === 'copy') copyText(root, text);
      else if (action === 'listen') speak(root, text);
      else if (action === 'stop') stopSpeech(root);
    } catch (e) { setStatus(root, 'Error: ' + (e && e.message ? e.message : e)); }
  }

  hosts.forEach(function (w) {
    const d = w.document;
    if (d.__govToolsClick) return;
    d.__govToolsClick = true;
    d.addEventListener('click', onClick, true);
  });
})();
"""
