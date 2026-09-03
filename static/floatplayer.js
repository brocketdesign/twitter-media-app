/* Floating auto-play player — one <video> in a window that walks a playlist
   clip by clip, on every page of the app.

   How the pieces map to what the user asked for:

   - "Play items one by one": the queue advances itself on 'ended' and wraps
     around at the end, so once started the player is always on.
   - "Stays on top of all windows": only the browser can raise a window over
     other apps, and the button that does it is Picture-in-Picture — the same
     <video> element is handed to the PiP window, so the playlist keeps
     advancing there (its 'ended' still fires on this page).
   - "Becomes transparent under the mouse": hovering the window fades it to
     half opacity and makes it click-through, so the page behind can be read
     and clicked while the clip keeps rolling. Moving the cursor away brings
     the window back.
   - "Grab mode": the toolbar unlocks the window for dragging; a second click
     (on the toolbar or on the window itself) locks it in place.
   - "A thumbnail appears in the menu": every clip that starts is announced
     with a 'floatplayer:playing' event, and the scanner page feeds it to its
     own played-videos tray (#vstrip) — one thumbnail system, not two. The
     control toolbar lives above that tray and swings out when the tray is
     hovered. Pages without a tray (gallery, character editor) get a small
     round pad in the same spot as the hover handle.
   - "Available everywhere": queue, position and settings live in
     localStorage, so each page that loads this script reopens the player and
     resumes the queue (muted — a fresh page has no gesture to spend on
     sound; the toolbar's speaker gives it back).

   Pages feed it items shaped like { origin, srcs: [...], poster, filename,
   text, user, date, duration, tweet_url } through FloatPlayer.start(); the
   first src is tried first and the rest are fallbacks (the scanner lists the
   streaming proxy before the raw CDN URL, for the same reason the grid
   player does). */

(function () {
  'use strict';

  const STORE_KEY = 'twmedia.floatplayer';
  const PEEK_DELAY = 260;     // hover a beat before the window fades away
  const BAR_MARGIN = 12;      // gap between the toolbar and the tray below it
  const BAR_BOTTOM = 14;      // where the tray itself sits
  const DOCK_CLEAR = 100;     // gap kept between the window and the tray below
  const HIDE_DELAY = 280;     // grace period when the cursor leaves the tray
  const MAX_AUTO_SKIPS = 3;   // a queue dead end-to-end stops instead of looping

  const ICON = {
    play: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13a1 1 0 0 0 1.54.84l10-6.5a1 1 0 0 0 0-1.68l-10-6.5A1 1 0 0 0 8 5.5z"/></svg>',
    pause: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7 5h3.4v14H7zM13.6 5H17v14h-3.6z"/></svg>',
    prev: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5H6v14h2z"/><path d="M20 5.9v12.2c0 .78-.87 1.25-1.53.83l-9.2-6.1a.98.98 0 0 1 0-1.66l9.2-6.1A1 1 0 0 1 20 5.9z"/></svg>',
    next: '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M16 5h2v14h-2z"/><path d="M4 5.9v12.2c0 .78.87 1.25 1.53.83l9.2-6.1a.98.98 0 0 0 0-1.66l-9.2-6.1A1 1 0 0 0 4 5.9z"/></svg>',
    soundOn: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M4 9v6h4l5 4V5L8 9H4z"/><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M16.5 8.5a5 5 0 0 1 0 7"/></svg>',
    soundOff: '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M4 9v6h4l5 4V5L8 9H4z"/><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M16.5 9.5l5 5m0-5l-5 5"/></svg>',
    move: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2v20M2 12h20"/><path d="M9 5l3-3 3 3M9 19l3 3 3-3M5 9l-3 3 3 3M19 9l3 3-3 3"/></svg>',
    lock: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="4" y="11" width="16" height="10" rx="2"/><path d="M8 11V7a4 4 0 0 1 8 0v4"/></svg>',
    pip: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><rect x="12" y="12" width="7" height="5" rx="1" fill="currentColor" stroke="none"/></svg>',
    close: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>',
    pad: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 14l6-6 6 6"/></svg>',
  };

  const state = {
    queue: [], index: 0, active: false,
    sound: false, grab: false, pip: false,
    x: 0, y: 0, placed: false,
    attempt: 0, skips: 0, seekHeld: false,
  };

  let root, surface, video, badge, lockBtn, grabHint, miniLbl, prog,
      bar, pad, seek, timeLbl, titleLbl,
      btnToggle, btnSound, btnGrab, btnPip;
  let peekTimer = 0, skipTimer = 0, hideTimer = 0;
  let drag = null;
  const hoverCapable = matchMedia('(hover: hover)').matches;

  function el(html) {
    const t = document.createElement('template');
    t.innerHTML = html.trim();
    return t.content.firstElementChild;
  }

  function fmt(s) {
    s = Math.max(0, Math.round(s || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    const mm = h ? String(m).padStart(2, '0') : String(m);
    return (h ? h + ':' : '') + mm + ':' + String(sec).padStart(2, '0');
  }

  function proxyUrl(u) { return '/api/stream?url=' + encodeURIComponent(u); }

  function normalizeItem(raw) {
    raw = raw || {};
    const srcs = (Array.isArray(raw.srcs) && raw.srcs.length ? raw.srcs : [raw.srcs || raw.origin])
      .filter(Boolean).map(String);
    return {
      origin: String(raw.origin || srcs[0] || ''),
      srcs,
      poster: raw.poster || '',
      filename: raw.filename || '',
      text: raw.text || '',
      user: raw.user || '',
      date: raw.date || '',
      duration: Number(raw.duration) || 0,
      tweet_url: raw.tweet_url || '',
    };
  }

  /* --- storage ------------------------------------------------------------ */

  function loadSaved() {
    try { return JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {}; }
    catch (e) { return {}; }
  }

  function save() {
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify({
        v: 1,
        active: state.active,
        queue: state.queue,
        index: state.index,
        x: Math.round(state.x), y: Math.round(state.y),
        placed: state.placed,
        grab: state.grab,
        sound: state.sound,
      }));
    } catch (e) { /* storage full or disabled — the player still works this page */ }
  }

  /* --- mounting ----------------------------------------------------------- */

  function mount() {
    if (root) return;
    root = el(`
      <div id="fplayerRoot">
        <div class="fplayer" id="fplayer" hidden>
          <video id="fpvideo" playsinline preload="auto"></video>
          <span class="fbadge" id="fpbadge"></span>
          <button type="button" class="flock" id="fplock" hidden
                  title="Lock it in place" aria-label="Lock the player in place">${ICON.lock}</button>
          <span class="fgrabhint" id="fpgrabhint" hidden>Drag to move · Lock to fix it</span>
          <div class="fmini" id="fpmini">
            <span class="fpulse"></span><span id="fpminilbl">Playing over all windows</span>
            <button type="button" id="fpunpip">Bring it back</button>
          </div>
          <div class="fprog"><i id="fpprog"></i></div>
        </div>
        <div class="fbar" id="fpbar" role="toolbar" aria-label="Floating video controls">
          <button type="button" data-act="prev" title="Previous video" aria-label="Previous video">${ICON.prev}</button>
          <button type="button" data-act="toggle" title="Play or pause" aria-label="Play or pause">${ICON.play}</button>
          <button type="button" data-act="next" title="Next video" aria-label="Next video">${ICON.next}</button>
          <span class="fsep"></span>
          <input class="fseek" id="fpseek" type="range" min="0" max="1000" value="0" title="Seek" aria-label="Seek">
          <span class="ftime" id="fptime">0:00 / 0:00</span>
          <span class="fsep"></span>
          <span class="ftitle" id="fptitle"></span>
          <span class="fsep"></span>
          <button type="button" data-act="sound" title="Sound on or off" aria-label="Sound on or off">${ICON.soundOff}</button>
          <button type="button" data-act="grab" title="Move the player (grab mode)" aria-label="Move the player">${ICON.move}</button>
          <button type="button" data-act="pip" title="Float over all windows (Picture-in-Picture)" aria-label="Float over all windows">${ICON.pip}</button>
          <button type="button" data-act="close" title="Stop the floating player" aria-label="Stop the floating player">${ICON.close}</button>
        </div>
        <button type="button" class="fpad" id="fppad" hidden
                title="Floating video controls" aria-label="Floating video controls">${ICON.pad}</button>
      </div>`);
    document.body.appendChild(root);

    surface = root.querySelector('#fplayer');
    video = root.querySelector('#fpvideo');
    badge = root.querySelector('#fpbadge');
    lockBtn = root.querySelector('#fplock');
    grabHint = root.querySelector('#fpgrabhint');
    miniLbl = root.querySelector('#fpminilbl');
    prog = root.querySelector('#fpprog');
    bar = root.querySelector('#fpbar');
    pad = root.querySelector('#fppad');
    seek = root.querySelector('#fpseek');
    timeLbl = root.querySelector('#fptime');
    titleLbl = root.querySelector('#fptitle');
    btnToggle = bar.querySelector('[data-act=toggle]');
    btnSound = bar.querySelector('[data-act=sound]');
    btnGrab = bar.querySelector('[data-act=grab]');
    btnPip = bar.querySelector('[data-act=pip]');

    if (!('pictureInPictureEnabled' in document) || !document.pictureInPictureEnabled) {
      btnPip.hidden = true;   // no OS floating window on this browser
    }

    wire();
  }

  /* The tray the toolbar hangs over: the scanner page's own played-videos
     strip when it exists, otherwise the small fallback pad. */
  function trayTarget() {
    return document.getElementById('vstrip') || pad;
  }

  function wire() {
    bar.addEventListener('click', e => {
      const btn = e.target.closest('[data-act]');
      if (!btn) return;
      const act = btn.dataset.act;
      if (act === 'toggle') video.paused ? video.play().catch(() => {}) : video.pause();
      else if (act === 'next') advance(1);
      else if (act === 'prev') advance(-1);
      else if (act === 'sound') setSound(!state.sound);
      else if (act === 'grab') setGrab(!state.grab);
      else if (act === 'pip') togglePiP();
      else if (act === 'close') close();
    });
    lockBtn.addEventListener('click', () => setGrab(false));
    root.querySelector('#fpunpip').addEventListener('click', () => {
      if (document.pictureInPictureElement) document.exitPictureInPicture().catch(() => {});
    });

    // Hovering the tray swings the toolbar out above it; the grace period on
    // hide covers the gap between the two.
    const target = trayTarget();
    target.addEventListener('mouseenter', showBar);
    target.addEventListener('mouseleave', scheduleHideBar);
    bar.addEventListener('mouseenter', () => clearTimeout(hideTimer));
    bar.addEventListener('mouseleave', scheduleHideBar);
    pad.addEventListener('click', () => {
      if (bar.classList.contains('show')) scheduleHideBar(); else showBar();
    });

    video.addEventListener('play', () => { btnToggle.innerHTML = ICON.pause; });
    video.addEventListener('pause', () => { btnToggle.innerHTML = ICON.play; });
    video.addEventListener('ended', () => advance(1));
    // A play() issued before the element has metadata is sometimes rejected
    // outright; once frames exist, start again if it is still sitting paused.
    // The first frame also becomes the tray thumbnail right away — the
    // scratch capture in captureThumb upgrades it to a slightly later frame.
    video.addEventListener('loadeddata', () => {
      const item = state.queue[state.index];
      if (item) drawFrame(video, item);
      if (state.active && video.paused && !video.ended) video.play().catch(() => {});
    });
    video.addEventListener('timeupdate', () => {
      const d = video.duration;
      prog.style.width = d ? (video.currentTime / d * 100) + '%' : '0';
      if (!state.seekHeld && d) seek.value = Math.round(video.currentTime / d * 1000);
      timeLbl.textContent = `${fmt(video.currentTime)} / ${fmt(d)}`;
    });
    video.addEventListener('error', onVideoError);

    seek.addEventListener('input', () => {
      if (video.duration) video.currentTime = (seek.value / 1000) * video.duration;
    });
    seek.addEventListener('pointerdown', () => { state.seekHeld = true; });
    addEventListener('pointerup', () => { state.seekHeld = false; });

    wirePeek();
    wireDrag();

    video.addEventListener('enterpictureinpicture', () => {
      state.pip = true;
      btnPip.classList.add('on');
      surface.classList.add('mini');
      leavePeek();
      miniLbl.textContent = `Playing over all windows — ${itemTitle(state.queue[state.index])}`;
    });
    video.addEventListener('leavepictureinpicture', () => {
      state.pip = false;
      btnPip.classList.remove('on');
      surface.classList.remove('mini');
    });

    addEventListener('resize', () => {
      if (state.active && state.placed) place(clampX(state.x), clampY(state.y));
      if (bar.classList.contains('show')) showBar();
    });
  }

  /* --- the control toolbar ------------------------------------------------- */

  function showBar() {
    if (!state.active) return;
    clearTimeout(hideTimer);
    // Sit just above whatever tray is below — the tall strip on the scanner,
    // the small pad elsewhere.
    const h = trayTarget().getBoundingClientRect().height;
    bar.style.bottom = Math.round(BAR_BOTTOM + (h > 0 ? h : 84) + BAR_MARGIN) + 'px';
    bar.classList.add('show');
  }

  function scheduleHideBar() {
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => bar.classList.remove('show'), HIDE_DELAY);
  }

  function hideBarNow() {
    clearTimeout(hideTimer);
    bar.classList.remove('show');
  }

  /* The pad only has a job where there is no tray to hover (gallery, the
     character editor); on the scanner the strip itself is the handle. */
  function syncPad() {
    pad.hidden = !state.active || !!document.getElementById('vstrip');
  }

  /* --- hover-to-see-through -----------------------------------------------
     The catch: flipping pointer-events off makes the window stop hearing the
     mouse, so the cursor's exit is watched from the document instead. */

  function wirePeek() {
    if (!hoverCapable) return;
    surface.addEventListener('mouseenter', () => {
      if (state.grab || state.pip) return;
      clearTimeout(peekTimer);
      peekTimer = setTimeout(() => surface.classList.add('peek'), PEEK_DELAY);
    });
    surface.addEventListener('mouseleave', () => clearTimeout(peekTimer));
    document.addEventListener('mousemove', e => {
      if (!surface.classList.contains('peek')) return;
      const r = surface.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right ||
          e.clientY < r.top || e.clientY > r.bottom) {
        leavePeek();
      }
    });
  }

  function leavePeek() {
    clearTimeout(peekTimer);
    surface.classList.remove('peek');
  }

  /* --- grab mode: unlock, drag, lock -------------------------------------- */

  function wireDrag() {
    surface.addEventListener('pointerdown', e => {
      if (!state.grab || state.pip || e.target.closest('button')) return;
      drag = { id: e.pointerId, dx: e.clientX - state.x, dy: e.clientY - state.y };
      surface.setPointerCapture(e.pointerId);
      surface.classList.add('grabbing');
      leavePeek();
    });
    surface.addEventListener('pointermove', e => {
      if (!drag || e.pointerId !== drag.id) return;
      place(clampX(e.clientX - drag.dx), clampY(e.clientY - drag.dy));
    });
    const end = e => {
      if (!drag || e.pointerId !== drag.id) return;
      drag = null;
      surface.classList.remove('grabbing');
      state.placed = true;
      save();
    };
    surface.addEventListener('pointerup', end);
    surface.addEventListener('pointercancel', end);
  }

  function setGrab(on) {
    state.grab = on;
    surface.classList.toggle('grab', on);
    lockBtn.hidden = !on;
    lockBtn.classList.toggle('on', on);
    grabHint.hidden = !on;
    btnGrab.classList.toggle('on', on);
    btnGrab.title = on ? 'Lock it in place' : 'Move the player (grab mode)';
    if (on) leavePeek();
    save();
  }

  /* --- position ----------------------------------------------------------- */

  function defaultX() { return clampX(innerWidth - surface.offsetWidth - 22); }
  function defaultY() { return clampY(innerHeight - surface.offsetHeight - DOCK_CLEAR - 14); }

  function clampX(v) {
    const room = innerWidth - surface.offsetWidth - 8;
    return Math.min(Math.max(8, v), Math.max(8, room));
  }
  function clampY(v) {
    const room = innerHeight - surface.offsetHeight - 8;
    return Math.min(Math.max(8, v), Math.max(8, room));
  }
  function place(x, y) {
    state.x = x; state.y = y;
    surface.style.left = x + 'px';
    surface.style.top = y + 'px';
  }

  /* --- playback ------------------------------------------------------------ */

  function play(i) {
    if (!state.queue.length) return;
    const n = state.queue.length;
    state.index = ((i % n) + n) % n;
    const item = state.queue[state.index];
    state.attempt = 0;
    state.skips = 0;
    clearTimeout(skipTimer); skipTimer = 0;

    video.poster = /^https?:/.test(item.poster) ? proxyUrl(item.poster) : item.poster;
    video.src = item.srcs[0];
    video.muted = !state.sound;
    renderSoundUI();
    video.play().catch(err => {
      // A play() the browser refuses for want of a gesture falls back to
      // muted rather than leaving a frozen frame.
      if (err && err.name === 'NotAllowedError' && !video.muted) {
        setSound(false);
        video.play().catch(() => {});
      }
    });

    badge.textContent = `${state.index + 1} / ${n}`;
    titleLbl.textContent = itemTitle(item);
    titleLbl.title = item.filename || itemTitle(item);
    // The tray is the page's own (showPlayingThumb on the scanner); announce
    // the clip so its thumbnail lands in the existing list.
    window.dispatchEvent(new CustomEvent('floatplayer:playing', { detail: item }));
    // Clips that arrived without a poster (character uploads, scans whose
    // preview never came through) would sit in the tray as a bare ▶ glyph —
    // pull a real frame for them instead.
    captureThumb(item);
    updateMediaSession(item);
    save();
  }

  function itemTitle(item) {
    const parts = [];
    if (item.user) parts.push('@' + item.user);
    if (item.text) parts.push(item.text);
    return parts.join(' · ') || item.filename || 'Video';
  }

  /* Adopt a captured frame: stored with the queue and announced so the
     page's tray can swap its ▶ glyph for the real thumbnail. */
  function adoptThumb(item, dataUrl) {
    item.poster = dataUrl;
    save();
    window.dispatchEvent(new CustomEvent('floatplayer:thumb', {
      detail: { origin: item.origin, poster: dataUrl },
    }));
  }

  /* Draw whatever frame the given element is showing into a thumbnail. */
  function drawFrame(v, item) {
    if (item.poster || !v.videoWidth) return false;
    try {
      const c = document.createElement('canvas');
      const scale = Math.min(1, 160 / v.videoWidth);
      c.width = Math.max(1, Math.round(v.videoWidth * scale));
      c.height = Math.max(1, Math.round(v.videoHeight * scale));
      c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
      adoptThumb(item, c.toDataURL('image/jpeg', 0.72));
      return true;
    } catch (e) {
      return false;   // tainted canvas (a raw cross-origin src) — give up
    }
  }

  /* Pull a real frame for a clip that has no poster: a muted scratch <video>
     loads the clip, seeks a beat in (frame one tends to be black), and the
     canvas becomes the thumbnail. The page's tray is told via
     'floatplayer:thumb' and the frame is saved with the queue, so it survives
     navigation like the rest of the session. Capture needs a same-origin
     source — the stream proxy qualifies (that is part of why it exists); a
     raw CDN URL would taint the canvas, so it is skipped. */
  function captureThumb(item) {
    if (item.poster || item._thumbTried) return;
    const src = item.srcs.find(s => {
      try { return new URL(s, location.href).origin === location.origin; }
      catch (e) { return false; }
    });
    if (!src) return;
    item._thumbTried = true;

    const v = document.createElement('video');
    v.muted = true;
    v.playsInline = true;
    v.preload = 'auto';
    let done = false;
    const finish = dataUrl => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      v.removeAttribute('src');
      v.load();
      v.remove();
      if (!dataUrl) {
        // let a later play() of the same clip try again — a failure is
        // usually the pipeline being busy, not the clip being hopeless
        item._thumbTried = false;
        return;
      }
      adoptThumb(item, dataUrl);
    };
    const timer = setTimeout(() => finish(null), 12000);
    v.addEventListener('loadeddata', () => {
      v.currentTime = Math.min(0.6, (v.duration || 1) / 3);
    });
    v.addEventListener('seeked', () => {
      try {
        const c = document.createElement('canvas');
        const scale = Math.min(1, 160 / (v.videoWidth || 160));
        c.width = Math.max(1, Math.round((v.videoWidth || 160) * scale));
        c.height = Math.max(1, Math.round((v.videoHeight || 90) * scale));
        c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
        finish(c.toDataURL('image/jpeg', 0.72));
      } catch (e) {
        finish(null);   // tainted canvas or no frame — leave the glyph
      }
    });
    v.addEventListener('error', () => finish(null));
    v.src = src;
    v.load();
  }

  function advance(dir) {
    if (state.queue.length < 2) {
      // a one-clip queue loops in place rather than reloading the same src
      video.currentTime = 0;
      video.play().catch(() => {});
      return;
    }
    play(state.index + dir);
  }

  function onVideoError() {
    if (!state.active || !video.src) return;
    const item = state.queue[state.index];
    const next = state.attempt + 1;
    // Same two-source dance as the grid player: proxy first, raw URL second.
    if (next < item.srcs.length) {
      state.attempt = next;
      video.src = item.srcs[next];
      video.load();
      video.play().catch(() => {});
      return;
    }
    state.skips += 1;
    if (state.skips >= MAX_AUTO_SKIPS) {
      badge.textContent = 'stalled — press next';
      return;
    }
    badge.textContent = 'skipping…';
    skipTimer = setTimeout(() => advance(1), 1200);
  }

  function setSound(on) {
    state.sound = on;
    video.muted = !on;
    renderSoundUI();
    save();
  }

  function renderSoundUI() {
    btnSound.innerHTML = state.sound ? ICON.soundOn : ICON.soundOff;
    btnSound.classList.toggle('on', state.sound);
  }

  function togglePiP() {
    if (!document.pictureInPictureEnabled) return;
    if (document.pictureInPictureElement) {
      document.exitPictureInPicture().catch(() => {});
      return;
    }
    video.requestPictureInPicture().catch(err => {
      titleLbl.textContent = 'Floating window refused: ' + (err.message || err.name);
    });
  }

  function updateMediaSession(item) {
    if (!('mediaSession' in navigator)) return;
    try {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: item.filename || 'Video',
        artist: item.user ? '@' + item.user : 'Twitter/X media',
        album: 'Floating player',
        artwork: item.poster
          ? [{ src: /^https?:/.test(item.poster) ? proxyUrl(item.poster) : item.poster }]
          : [],
      });
      navigator.mediaSession.setActionHandler('previoustrack', () => advance(-1));
      navigator.mediaSession.setActionHandler('nexttrack', () => advance(1));
      navigator.mediaSession.setActionHandler('play', () => video.play());
      navigator.mediaSession.setActionHandler('pause', () => video.pause());
    } catch (e) { /* older engines: the playlist still works without it */ }
  }

  /* --- public surface ------------------------------------------------------- */

  function start(items, index) {
    const list = (items || []).map(normalizeItem).filter(i => i.srcs.length);
    if (!list.length) return;
    state.queue = list;
    state.active = true;
    mount();

    // A start() is always a click, so sound is allowed straight away; a
    // browser that still refuses falls back to muted inside play().
    state.sound = true;
    document.body.classList.add('fplayer-open');
    surface.hidden = false;
    surface.classList.remove('mini');
    syncPad();
    setGrabUI();
    if (state.placed) place(clampX(state.x), clampY(state.y));
    else place(defaultX(), defaultY());

    // Grid inline players must not talk over the floating one.
    document.querySelectorAll('.vplayer video').forEach(v => { if (!v.paused) v.pause(); });

    play(Number.isInteger(index) ? index : 0);
  }

  function setGrabUI() {
    // Reflect state.grab without saving (used right after mount).
    const on = state.grab;
    surface.classList.toggle('grab', on);
    lockBtn.hidden = !on;
    lockBtn.classList.toggle('on', on);
    grabHint.hidden = !on;
    btnGrab.classList.toggle('on', on);
    btnGrab.title = on ? 'Lock it in place' : 'Move the player (grab mode)';
  }

  function restore() {
    const saved = loadSaved();
    if (!saved.active || !(saved.queue || []).length) return;
    state.queue = saved.queue.map(normalizeItem).filter(i => i.srcs.length);
    if (!state.queue.length) return;
    state.index = Number(saved.index) || 0;
    state.active = true;
    state.sound = !!saved.sound;
    state.grab = !!saved.grab;
    state.placed = !!saved.placed;
    state.x = Number(saved.x) || 0;
    state.y = Number(saved.y) || 0;

    mount();
    document.body.classList.add('fplayer-open');
    surface.hidden = false;
    syncPad();
    setGrabUI();
    if (state.placed) place(clampX(state.x), clampY(state.y));
    else place(defaultX(), defaultY());
    play(state.index);
  }

  function close() {
    if (root) {
      if (document.pictureInPictureElement) document.exitPictureInPicture().catch(() => {});
      clearTimeout(skipTimer); skipTimer = 0;
      clearTimeout(peekTimer); peekTimer = 0;
      video.pause();
      video.removeAttribute('src');
      video.load();
      surface.hidden = true;
      surface.classList.remove('mini', 'peek', 'grab');
      hideBarNow();
      document.body.classList.remove('fplayer-open');
    }
    state.active = false;
    state.queue = [];
    state.index = 0;
    syncPad();
    // Tell the page so its tray can drop the session's thumbnails.
    window.dispatchEvent(new CustomEvent('floatplayer:closed'));
    save();   // position and grab stay; the queue goes
  }

  function isActive() { return state.active; }

  function jumpTo(origin) {
    if (!state.active) return false;
    const i = state.queue.findIndex(q => q.origin === origin);
    if (i < 0) return false;
    play(i);
    return true;
  }

  restore();

  window.FloatPlayer = { start, restore, close, isActive, jumpTo };
})();
