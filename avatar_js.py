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
  const FALLBACK = host.location.origin + '/app/static/avatar.jpg';
  let tried = 0;
  function loadImage() {
    // Preferred: picture embedded in the page by the assets component (no static file needed).
    const src = tried === 0 ? (host.__govAvatarSrc || FALLBACK) : (tried === 1 ? FALLBACK : (host.__govAvatarSrc || ''));
    if (!src) return;
    img.src = src;
  }
  img.onerror = function () { tried++; if (tried < 3) host.setTimeout(loadImage, tried === 2 ? 800 : 0); };
  loadImage();

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
      if (!ready) { tried = 0; loadImage(); }
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
    host.__govSession = (host.__govSession || 0) + 1;
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
  // Use the parent page's speech engine (shared by every message) when reachable.
  const SYN = (function () {
    try { if (window.parent && window.parent !== window && window.parent.speechSynthesis && window.parent.SpeechSynthesisUtterance) return window.parent; } catch (e) {}
    return window;
  })();
  function bump() { SYN.__govSession = (SYN.__govSession || 0) + 1; return SYN.__govSession; }
  function cur() { return SYN.__govSession || 0; }

  // Script-range detection: covers every language whose script is NOT shared with English,
  // so this does not depend on a hand-picked language list. Order matters only where two
  // ranges could both match the same text, which does not happen here (each block is unique).
  function detectLanguage(v) {
    const RANGES = [
      [/[\u0D80-\u0DFF]/, 'si-LK'],   // Sinhala
      [/[\u0B80-\u0BFF]/, 'ta-IN'],   // Tamil
      [/[\u0900-\u097F]/, 'hi-IN'],   // Devanagari (Hindi, Marathi, Nepali, Sanskrit)
      [/[\u0980-\u09FF]/, 'bn-IN'],   // Bengali / Assamese
      [/[\u0A00-\u0A7F]/, 'pa-IN'],   // Gurmukhi (Punjabi)
      [/[\u0A80-\u0AFF]/, 'gu-IN'],   // Gujarati
      [/[\u0B00-\u0B7F]/, 'or-IN'],   // Odia
      [/[\u0C00-\u0C7F]/, 'te-IN'],   // Telugu
      [/[\u0C80-\u0CFF]/, 'kn-IN'],   // Kannada
      [/[\u0D00-\u0D7F]/, 'ml-IN'],   // Malayalam
      [/[\u0E00-\u0E7F]/, 'th-TH'],   // Thai
      [/[\u0E80-\u0EFF]/, 'lo-LA'],   // Lao
      [/[\u1000-\u109F]/, 'my-MM'],   // Myanmar (Burmese)
      [/[\u1780-\u17FF]/, 'km-KH'],   // Khmer
      [/[\u0600-\u06FF\u0750-\u077F]/, 'ar-SA'],  // Arabic (also covers Urdu/Persian/Pashto script)
      [/[\u0590-\u05FF]/, 'he-IL'],   // Hebrew
      [/[\u0370-\u03FF]/, 'el-GR'],   // Greek
      [/[\u0530-\u058F]/, 'hy-AM'],   // Armenian
      [/[\u10A0-\u10FF]/, 'ka-GE'],   // Georgian
      [/[\u3040-\u30FF]/, 'ja-JP'],   // Japanese kana - checked BEFORE Han, since Japanese
                                        // sentences usually mix kanji with kana, but Chinese never has kana
      [/[\u4E00-\u9FFF]/, 'zh-CN'],   // Chinese (Han, no kana present)
      [/[\uAC00-\uD7AF]/, 'ko-KR'],   // Korean (Hangul)
      [/[\u0400-\u04FF]/, 'ru-RU'],   // Cyrillic (Russian and others)
    ];
    for (const [re, lang] of RANGES) if (re.test(v)) return lang;

    // Latin-script languages other than English: a light heuristic using accented letters
    // and a handful of very common function words. Good enough to pick a closer voice/accent;
    // anything it cannot place falls back to English, which can still read Latin text aloud.
    const low = ' ' + v.toLowerCase() + ' ';
    // Checked in order of how UNIQUE each signal is, so two languages that share an accented
    // letter do not fight over the same text - the most distinctive marker wins.
    const LATIN_HINTS = [
      ['vi-VN', /[ạảđịọụưăơ]/],                 // Vietnamese tone/diacritic marks
      ['tr-TR', /[ığşİ]/],                       // Turkish dotless i / cedilla s
      ['es-ES', /[ñ¿¡]/],                        // Spanish-only punctuation/letter
      ['de-DE', /[ß]|[äöü].*[äöü]/],             // German sharp s, or more than one umlaut
      ['fr-FR', /œ|[éèê].*[éèê]|[ç]/],           // French ligature, repeated é/è/ê, or ç
      ['pt-PT', /[ãõ]/],                         // Portuguese nasal vowels
      ['it-IT', /\b\w+(?:are|ere|ire|zione)\b/], // common Italian verb/word endings
      ['id-ID', / (yang|dengan|tidak|adalah|saya) /],
      ['sw-KE', / (na|ya|kwa|hii|ni) /],
    ];
    for (const [lang, re] of LATIN_HINTS) if (re.test(low)) return lang;
    return 'en-US';
  }

  // Friendly names for status messages. Covers every code detectLanguage() can return,
  // for ANY language - not just Sinhala - so the same message works for all of them.
  const LANG_NAMES = {
    'si-LK': 'Sinhala', 'ta-IN': 'Tamil', 'hi-IN': 'Hindi', 'bn-IN': 'Bengali', 'pa-IN': 'Punjabi',
    'gu-IN': 'Gujarati', 'or-IN': 'Odia', 'te-IN': 'Telugu', 'kn-IN': 'Kannada', 'ml-IN': 'Malayalam',
    'th-TH': 'Thai', 'lo-LA': 'Lao', 'my-MM': 'Burmese', 'km-KH': 'Khmer', 'ar-SA': 'Arabic',
    'he-IL': 'Hebrew', 'el-GR': 'Greek', 'hy-AM': 'Armenian', 'ka-GE': 'Georgian', 'zh-CN': 'Chinese',
    'ja-JP': 'Japanese', 'ko-KR': 'Korean', 'ru-RU': 'Russian', 'vi-VN': 'Vietnamese', 'tr-TR': 'Turkish',
    'de-DE': 'German', 'fr-FR': 'French', 'pt-PT': 'Portuguese', 'es-ES': 'Spanish', 'it-IT': 'Italian',
    'id-ID': 'Indonesian', 'sw-KE': 'Swahili', 'en-US': 'English',
  };
  function langName(code) { return LANG_NAMES[code] || code; }

  // speechSynthesis.getVoices() is often empty for a few hundred ms after the page loads, because
  // the browser fetches the voice list asynchronously - calling it too early makes EVERY language
  // look like it has no installed voice. This waits for the real list (or gives up after 1.2s).
  function voicesReady() {
    const list = SYN.speechSynthesis.getVoices();
    if (list && list.length) return Promise.resolve(list);
    return new Promise(function (resolve) {
      let done = false;
      const finish = function () { if (done) return; done = true; resolve(SYN.speechSynthesis.getVoices() || []); };
      try { SYN.speechSynthesis.addEventListener('voiceschanged', finish, { once: true }); } catch (e) {}
      setTimeout(finish, 1200);
    });
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

  let cloudAudio = null;
  function stopSpeech(root) {
    bump();
    try { SYN.speechSynthesis.cancel(); } catch (e) {}
    try { if (cloudAudio) { cloudAudio.pause(); cloudAudio.src = ''; cloudAudio = null; } } catch (e) {}
    try { getAvatar().stop(); } catch (e) {}
    if (root) setStatus(root, 'Speech stopped.');
  }

  function setStatus(root, msg) {
    const el = root && root.querySelector('.gov-status');
    if (el) el.textContent = msg;
  }

  // Online fallback: Google Translate TTS audio. Unlike browser speech voices,
  // this does not require a language voice to be installed on the user's device.
  // The request is made only after the user presses Listen and the local voice is missing.
  function speakCloud(root, pieces, language, mySession, avatar) {
    const base = (language || 'en-US').split('-')[0].toLowerCase();
    let index = 0;
    setStatus(root, 'No device voice found. Trying online ' + langName(language) + ' voice…');
    function playNext() {
      if (mySession !== cur()) return;
      if (index >= pieces.length) {
        cloudAudio = null;
        setStatus(root, 'Cloud audio finished.');
        if (avatar) avatar.stop();
        return;
      }
      const q = encodeURIComponent(pieces[index++]);
      const url = 'https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl=' + encodeURIComponent(base) + '&q=' + q;
      const audio = new Audio(url);
      cloudAudio = audio;
      audio.onended = playNext;
      audio.onerror = function () {
        if (mySession !== cur()) return;
        cloudAudio = null;
        setStatus(root, 'Online ' + langName(language) + ' voice failed. Check internet and try Listen again.');
        if (avatar) avatar.stop();
      };
      const result = audio.play();
      if (result && result.catch) result.catch(function () {
        if (mySession !== cur()) return;
        cloudAudio = null;
        setStatus(root, 'Cloud audio was blocked. Click Listen again or allow audio playback.');
        if (avatar) avatar.stop();
      });
    }
    playNext();
  }

  function speak(root, text, requestedLanguage) {
    if (!('speechSynthesis' in SYN) || !('SpeechSynthesisUtterance' in SYN)) {
      setStatus(root, 'Speech is not supported by this browser. Use Chrome or Edge.');
      return;
    }
    const spoken = cleanForSpeech(text);
    if (!spoken) { setStatus(root, 'Nothing to read.'); return; }
    // Respect the language selected in the Listening language menu.
    // "auto" keeps script-based detection for the original answer.
    const language = requestedLanguage && requestedLanguage !== 'auto'
      ? requestedLanguage : detectLanguage(spoken);
    const pieces = chunkText(spoken);
    const mySession = bump();

    try { SYN.speechSynthesis.cancel(); } catch (e) {}
    let avatar = null;
    try { avatar = getAvatar(); avatar.start(); } catch (e) { avatar = null; }
    setStatus(root, 'Starting…');

    function findVoice(voices) {
      const base = language.split('-')[0].toLowerCase();
      // Prefer an exact match (e.g. "si-LK"), then fall back to the same language family
      // (e.g. any "si-*" voice) so a slightly different region code still counts as installed.
      return voices.find(function (v) { return v.lang && v.lang.toLowerCase() === language.toLowerCase(); })
          || voices.find(function (v) { return v.lang && v.lang.toLowerCase().startsWith(base); })
          || null;
    }

    function next(i, voice, hadVoice) {
      if (mySession !== cur()) return;
      if (i >= pieces.length) {
        setStatus(root, '');
        if (avatar) avatar.stop();
        return;
      }
      const u = new SYN.SpeechSynthesisUtterance(pieces[i]);
      u.lang = language; u.rate = 1;
      if (voice) u.voice = voice;
      u.onstart = function () {
        if (mySession !== cur()) return;
        setStatus(root, hadVoice
          ? 'Speaking (' + langName(language) + ')…'
          : 'Speaking (' + langName(language) + ')… this device has no ' + langName(language) +
            ' voice installed, so it may sound wrong or use a different accent.');
      };
      u.onboundary = function () { if (avatar) avatar.pulse(); };
      u.onend = function () { next(i + 1, voice, hadVoice); };
      u.onerror = function (ev) {
        if (mySession !== cur()) return;
        if (ev && (ev.error === 'canceled' || ev.error === 'interrupted')) return;
        const reason = ev && ev.error ? ev.error : 'unknown';
        setStatus(root, 'Could not speak this ' + langName(language) + ' text (' + reason + ').' +
          (hadVoice ? '' : ' No ' + langName(language) + ' voice is installed on this device - on ' +
            'Android: Settings > System > Languages & input > Text-to-speech output > install the ' +
            langName(language) + ' voice; on a computer, install that language in Chrome/Windows/macOS ' +
            'text-to-speech settings.'));
        if (avatar) avatar.stop();
      };
      SYN.speechSynthesis.speak(u);
      if (SYN.speechSynthesis.paused) SYN.speechSynthesis.resume();
    }

    voicesReady().then(function (voices) {
      if (mySession !== cur()) return;
      const voice = findVoice(voices);
      // If this device has no matching voice, use online audio instead of letting
      // the browser silently read Sinhala/other scripts with an unrelated accent.
      if (!voice) {
        speakCloud(root, pieces, language, mySession, avatar);
        return;
      }
      // Chrome needs a short pause between cancel() and speak().
      setTimeout(function () { next(0, voice, true); }, 120);
    });
  }

  async function copyText(root, text) {
    let ok = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) { await navigator.clipboard.writeText(text); ok = true; }
    } catch (e) { ok = false; }
    if (!ok) {
      try {
        const f = document.createElement('textarea');
        f.value = text; f.setAttribute('readonly', '');
        f.style.position = 'fixed'; f.style.left = '-9999px';
        document.body.appendChild(f); f.select();
        ok = document.execCommand('copy'); f.remove();
      } catch (e) { ok = false; }
    }
    setStatus(root, ok ? 'Copied!' : 'Copy failed — select the text and press Ctrl+C.');
  }

  function onClick(ev) {
    const t = ev.target;
    const btn = t && t.closest ? t.closest('button[data-action]') : null;
    if (!btn) return;
    const root = btn.closest('[data-gov-tools]');
    if (!root) return;
    const action = btn.getAttribute('data-action');
    const text = root.getAttribute('data-text') || '';
    const requestedLanguage = root.getAttribute('data-lang') || 'auto';
    try {
      if (action === 'copy') copyText(root, text);
      else if (action === 'listen') speak(root, text, requestedLanguage);
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


# ---------------------------------------------------------------------------
# Dictation (🎤 Speak). Runs inside an iframe; uses the parent page's speech
# recognition (so the normal microphone prompt appears) and types the result into
# the parent page's chat box, then presses send. No page reload.
# ---------------------------------------------------------------------------
STT_JS = r"""
(function () {
  const $ = function (id) { return document.getElementById(id); };
  const startBtn = $('sttBtn'), stopBtn = $('sttStopBtn'), status = $('sttStatus'), langSel = $('sttLang');
  function setStatus(v) { status.textContent = v; }
  const P = (function () { try { if (window.parent && window.parent.document) return window.parent; } catch (e) {} return window; })();
  try {
    const saved = P.sessionStorage.getItem('chat_stt_lang');
    if (saved) langSel.value = saved;
    langSel.addEventListener('change', function () { try { P.sessionStorage.setItem('chat_stt_lang', langSel.value); } catch (e) {} });
  } catch (e) {}

  const Rec = P.SpeechRecognition || P.webkitSpeechRecognition || window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Rec) { startBtn.disabled = true; setStatus('Voice dictation is not supported. Use Chrome or Edge.'); return; }

  const GRAD = 'linear-gradient(120deg,#6366f1,#8b5cf6)';
  let rec = null, listening = false;

  function sendToChat(transcript) {
    const d = P.document;
    const box = d.querySelector('textarea[data-testid="stChatInputTextArea"]') || d.querySelector('[data-testid="stChatInput"] textarea');
    if (!box) { setStatus('Chat box not found. Please type your message.'); return; }
    const setter = Object.getOwnPropertyDescriptor(P.HTMLTextAreaElement.prototype, 'value').set;
    setter.call(box, transcript);
    box.dispatchEvent(new P.Event('input', { bubbles: true }));
    setStatus('Sending: ' + transcript);
    setTimeout(function () {
      const send = d.querySelector('[data-testid="stChatInputSubmitButton"]') || d.querySelector('[data-testid="stChatInput"] button');
      if (send && !send.disabled) send.click();
      else box.dispatchEvent(new P.KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));
    }, 200);
  }

  startBtn.addEventListener('click', function () {
    if (listening) return;
    try {
      rec = new Rec();
      rec.continuous = false; rec.interimResults = false; rec.maxAlternatives = 1;
      rec.lang = langSel.value;
      rec.onresult = function (ev) {
        const t = ev.results && ev.results[0] && ev.results[0][0] ? ev.results[0][0].transcript.trim() : '';
        if (!t) { setStatus('No speech detected. Please try again.'); return; }
        sendToChat(t);
      };
      rec.onerror = function (ev) { setStatus('Microphone error: ' + ev.error + '. Allow the microphone for this site.'); };
      rec.onend = function () { listening = false; startBtn.style.background = GRAD; };
      rec.start();
      listening = true;
      startBtn.style.background = '#e69500';
      setStatus('Listening (' + rec.lang + ')… speak now');
    } catch (e) {
      listening = false;
      setStatus('Could not start the microphone: ' + (e && e.message ? e.message : e));
    }
  });
  stopBtn.addEventListener('click', function () {
    try { if (rec && listening) rec.stop(); } catch (e) {}
    setStatus('Dictation stopped.');
  });
})();
"""
