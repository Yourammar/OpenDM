// OpenDM Browser Integration | download button over videos and images (MIT License)
(() => {
  if (window.__opendmLoaded) return;
  window.__opendmLoaded = true;

  const settings = { videoButton: true, imageButton: true, minImageSize: 120 };
  chrome.storage.sync.get(settings, (s) => Object.assign(settings, s || {}));
  chrome.storage.onChanged.addListener((changes) => {
    for (const k in changes) settings[k] = changes[k].newValue;
    if (!settings.videoButton && !settings.imageButton) hide();
  });

  // ------------------------------------------------------------ overlay button (shadow DOM)
  const host = document.createElement("opendm-overlay");
  host.style.cssText = "all:initial;position:fixed;top:0;left:0;width:0;height:0;z-index:2147483647;";
  const shadow = host.attachShadow({ mode: "closed" });
  shadow.innerHTML = `
    <style>
      .btn { position: fixed; display: none; align-items: center; gap: 6px;
        font: 600 13px/1 "Segoe UI", system-ui, sans-serif; color: #fff;
        background: #1a73e8; border: 0; border-radius: 18px; padding: 8px 12px 8px 10px;
        cursor: pointer; box-shadow: 0 2px 10px rgba(0,0,0,.35); user-select: none;
        opacity: .95; transition: opacity .15s, transform .1s; pointer-events: auto; }
      .btn:hover { opacity: 1; background: #1765cc; }
      .btn:active { transform: scale(.96); }
      .btn.small { padding: 6px; border-radius: 50%; }
      .btn.small .label { display: none; }
      .btn svg { width: 16px; height: 16px; flex: none; }
      .toast { position: fixed; right: 16px; bottom: 16px; max-width: 360px; display: none;
        font: 13px/1.4 "Segoe UI", system-ui, sans-serif; color: #fff; background: #202124;
        padding: 10px 14px; border-radius: 8px; box-shadow: 0 4px 16px rgba(0,0,0,.4); }
    </style>
    <button class="btn" title="Download with OpenDM">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
           stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11"/><path d="M6.5 10.5 12 16l5.5-5.5"/><path d="M5 20h14"/></svg>
      <span class="label">Download video</span>
    </button>
    <div class="toast"></div>`;
  const btn = shadow.querySelector(".btn");
  const label = shadow.querySelector(".label");
  const toastEl = shadow.querySelector(".toast");

  function mount() {
    if (!host.isConnected) (document.documentElement || document.body).appendChild(host);
  }

  let current = null;     // element the button is attached to
  let hideTimer = null;
  let toastTimer = null;

  function toast(text) {
    mount();
    toastEl.textContent = text;
    toastEl.style.display = "block";
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toastEl.style.display = "none"), 3500);
  }

  function hide() {
    btn.style.display = "none";
    current = null;
  }

  function place(el) {
    const r = el.getBoundingClientRect();
    const isVideo = el.tagName === "VIDEO";
    btn.classList.toggle("small", !isVideo);
    label.textContent = isVideo ? "Download video" : "Download image";
    btn.style.display = "flex";
    const bw = btn.offsetWidth || 140;
    const top = Math.max(4, Math.min(window.innerHeight - 40, r.top + 10));
    const left = Math.max(4, Math.min(window.innerWidth - bw - 4, r.right - bw - 10));
    btn.style.top = top + "px";
    btn.style.left = left + "px";
  }

  function show(el) {
    mount();
    current = el;
    clearTimeout(hideTimer);
    place(el);
  }

  // ------------------------------------------------------------ find media under the pointer
  function visibleRect(el) {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 ? r : null;
  }

  function videoAt(x, y) {
    let best = null;
    for (const v of document.querySelectorAll("video")) {
      const r = visibleRect(v);
      if (!r || r.width < 120 || r.height < 80) continue;
      if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
        if (!best || r.width * r.height < best.r.width * best.r.height) best = { v, r };
      }
    }
    return best && best.v;
  }

  function imageAt(x, y) {
    const min = settings.minImageSize || 120;
    for (const el of document.elementsFromPoint(x, y)) {
      if (el === host) continue;
      if (el.tagName === "IMG") {
        const r = visibleRect(el);
        if (r && r.width >= min && r.height >= min) return el;
      }
    }
    return null;
  }

  let pending = false;
  let lastX = 0, lastY = 0;
  document.addEventListener("mousemove", (e) => {
    lastX = e.clientX;
    lastY = e.clientY;
    if (pending) return;
    pending = true;
    requestAnimationFrame(() => {
      pending = false;
      if (!settings.videoButton && !settings.imageButton) return;
      const target = (settings.videoButton && videoAt(lastX, lastY)) ||
                     (settings.imageButton && imageAt(lastX, lastY));
      if (target) {
        if (target !== current) show(target);
        else { clearTimeout(hideTimer); place(target); }
      } else if (current) {
        clearTimeout(hideTimer);
        hideTimer = setTimeout(hide, 1200);
      }
    });
  }, { passive: true, capture: true });

  window.addEventListener("scroll", () => current && place(current), { passive: true, capture: true });
  btn.addEventListener("mouseenter", () => clearTimeout(hideTimer));

  // ------------------------------------------------------------ work out what to download
  // Links that identify one post/video on common sites. Used to find the right page for yt-dlp
  // when a video sits inside a feed (Instagram, Facebook, X, YouTube home, TikTok...).
  const ITEM_PATTERNS = [
    /\/(?:p|reel|reels|tv)\/[\w-]+/,          // instagram
    /\/watch\?v=[\w-]+/, /\/shorts\/[\w-]+/,  // youtube
    /\/status\/\d+/,                          // x / twitter
    /\/video\/\d+/,                           // tiktok
    /\/videos\/[\w.-]+/, /\/reel\/\d+/, /\/watch\/\?v=\d+/, // facebook
    /\/stories\/[\w.-]+\/\d+/                 // instagram stories
  ];

  function itemKey(href) {
    for (const re of ITEM_PATTERNS) {
      const m = href.match(re);
      if (m) return m[0];
    }
    return null;
  }

  function postUrlFor(el) {
    const here = location.href;
    const r = el.getBoundingClientRect();
    const bigOnPage = r.width * r.height > window.innerWidth * window.innerHeight * 0.25;
    if (itemKey(here) && bigOnPage) return here;
    let node = el.parentElement;
    for (let depth = 0; node && depth < 14; depth++, node = node.parentElement) {
      const keys = new Map();
      for (const a of node.querySelectorAll("a[href]")) {
        const k = itemKey(a.href);
        if (k && !keys.has(k)) keys.set(k, a.href);
      }
      if (keys.size === 1) {
        const href = [...keys.values()][0];
        try {
          const u = new URL(href);
          // keep youtube ?v= and facebook ?v=, drop tracking junk elsewhere
          if (!/[?&]v=/.test(u.search)) u.search = "";
          u.hash = "";
          return u.href;
        } catch (e) {
          return href;
        }
      }
      if (keys.size > 1) break;
    }
    return here;
  }

  function bestImageSrc(img) {
    let best = img.currentSrc || img.src;
    let bestW = img.naturalWidth || 0;
    const srcset = img.getAttribute("srcset");
    if (srcset) {
      for (const part of srcset.split(",")) {
        const [u, d] = part.trim().split(/\s+/);
        const w = d && d.endsWith("w") ? parseInt(d, 10) : d && d.endsWith("x") ? parseFloat(d) * 1000 : 0;
        if (u && w > bestW) { best = new URL(u, location.href).href; bestW = w; }
      }
    }
    return best;
  }

  function videoSrc(v) {
    let s = v.currentSrc || v.src || "";
    if (!s) {
      const src = v.querySelector("source[src]");
      if (src) s = src.src;
    }
    return s;
  }

  function buildRequest(el) {
    if (el.tagName === "IMG") {
      return { kind: "file", url: bestImageSrc(el), referer: location.href, title: document.title };
    }
    const src = videoSrc(el);
    const direct = /^https?:/i.test(src) && /\.(mp4|webm|m4v|mov|ogv|mkv)(\?|$)/i.test(src);
    const page = postUrlFor(el);
    if (direct && !itemKey(page)) {
      return { kind: "file", url: src, referer: location.href, title: document.title };
    }
    return {
      kind: "media", url: /^https?:/i.test(src) ? src : "", page_url: page,
      referer: location.href, title: document.title
    };
  }

  btn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (!current) return;
    const req = buildRequest(current);
    label.textContent = "Sending...";
    chrome.runtime.sendMessage({ type: "opendm-download", req }, (res) => {
      const err = chrome.runtime.lastError;
      if (err) {
        toast("OpenDM extension was updated. Reload this page and try again.");
      } else if (!res || !res.ok) {
        toast((res && res.error) || "OpenDM did not answer.");
      } else {
        toast("Sent to OpenDM");
      }
      if (current) place(current);
    });
  }, true);

  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "opendm-toast") toast(msg.text);
  });
})();
