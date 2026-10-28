// OpenDM Browser Integration | background service worker (MIT License)

const APP = "http://127.0.0.1:9614";

const DEFAULTS = {
  videoButton: true,
  imageButton: true,
  catchDownloads: true,
  minImageSize: 120
};

const CATCH_EXTS = new Set([
  "zip", "rar", "7z", "tar", "gz", "bz2", "xz", "iso", "img", "cab",
  "exe", "msi", "msix", "apk", "dmg", "deb", "rpm", "appimage",
  "mp4", "mkv", "webm", "avi", "mov", "flv", "wmv", "m4v", "3gp", "mpg", "mpeg",
  "mp3", "m4a", "aac", "wav", "flac", "ogg", "opus", "wma",
  "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "epub",
  "bin", "torrent"
]);

async function getSettings() {
  const s = await chrome.storage.sync.get(DEFAULTS);
  return { ...DEFAULTS, ...s };
}

async function ping(timeoutMs = 800) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(APP + "/ping", { signal: ctrl.signal, cache: "no-store" });
    const j = await r.json();
    return !!j.ok;
  } catch (e) {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

async function cookiesFor(urls) {
  const out = [];
  const seen = new Set();
  for (const u of urls) {
    if (!u || !/^https?:/i.test(u)) continue;
    let list = [];
    try {
      list = await chrome.cookies.getAll({ url: u });
    } catch (e) {
      continue;
    }
    for (const c of list) {
      const key = c.domain + "|" + c.path + "|" + c.name;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({
        name: c.name, value: c.value, domain: c.domain, path: c.path,
        secure: c.secure, hostOnly: c.hostOnly, httpOnly: c.httpOnly,
        expirationDate: c.expirationDate || 0
      });
    }
  }
  return out;
}

// Sends a request to the desktop app. Cookies are only ever sent to 127.0.0.1.
async function sendToApp(req) {
  const payload = {
    kind: req.kind || "auto",
    url: req.url || "",
    page_url: req.page_url || "",
    referer: req.referer || "",
    title: req.title || "",
    filename: req.filename || "",
    user_agent: navigator.userAgent,
    cookies: await cookiesFor([req.url, req.page_url, req.referer])
  };
  try {
    const r = await fetch(APP + "/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || !j.ok) return { ok: false, error: j.error || "OpenDM refused the request" };
    return { ok: true };
  } catch (e) {
    return { ok: false, error: "OpenDM is not running. Start the OpenDM app and try again." };
  }
}

// ---------------------------------------------------------------- messages from pages

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg && msg.type === "opendm-download") {
    sendToApp(msg.req).then(sendResponse);
    return true;
  }
  if (msg && msg.type === "opendm-ping") {
    ping().then((ok) => sendResponse({ ok }));
    return true;
  }
  return false;
});

// ---------------------------------------------------------------- context menus

function createMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "opendm-link", title: "Download link with OpenDM", contexts: ["link"]
    });
    chrome.contextMenus.create({
      id: "opendm-media", title: "Download this with OpenDM", contexts: ["image", "video", "audio"]
    });
    chrome.contextMenus.create({
      id: "opendm-page", title: "Download video from this page with OpenDM", contexts: ["page", "frame"]
    });
  });
}

chrome.runtime.onInstalled.addListener(createMenus);
chrome.runtime.onStartup.addListener(createMenus);

function notifyTab(tabId, frameId, text) {
  if (tabId == null || tabId < 0) return;
  chrome.tabs.sendMessage(tabId, { type: "opendm-toast", text }, { frameId: frameId || 0 })
    .catch(() => {});
}

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const page = info.frameUrl || info.pageUrl || (tab && tab.url) || "";
  let req;
  if (info.menuItemId === "opendm-link") {
    req = { kind: "auto", url: info.linkUrl, page_url: info.linkUrl, referer: page };
  } else if (info.menuItemId === "opendm-media") {
    const src = info.srcUrl || "";
    if (info.mediaType === "image" && /^https?:/i.test(src)) {
      req = { kind: "file", url: src, referer: page };
    } else {
      req = { kind: "media", url: /^https?:/i.test(src) ? src : "", page_url: page, referer: page };
    }
  } else {
    req = { kind: "media", url: "", page_url: page, referer: page, title: tab && tab.title };
  }
  const res = await sendToApp(req);
  notifyTab(tab && tab.id, info.frameId, res.ok ? "Sent to OpenDM" : res.error);
});

// ---------------------------------------------------------------- take over browser downloads

function extFromUrl(u) {
  try {
    const p = new URL(u).pathname;
    const m = p.match(/\.([a-z0-9]{1,9})$/i);
    return m ? m[1].toLowerCase() : "";
  } catch (e) {
    return "";
  }
}

function shouldCatch(item) {
  const url = item.finalUrl || item.url || "";
  if (!/^https?:/i.test(url)) return false;          // blob:, data: and file: stay in the browser
  const ext = extFromUrl(url) || (item.filename ? item.filename.split(".").pop().toLowerCase() : "");
  if (CATCH_EXTS.has(ext)) return true;
  const mime = (item.mime || "").toLowerCase();
  if (mime.startsWith("video/") || mime.startsWith("audio/")) return true;
  if (/^application\/(zip|x-zip|x-rar|x-7z|octet-stream|x-msdownload|x-msi|vnd\.android|pdf|x-iso)/.test(mime)) return true;
  return false;
}

chrome.downloads.onCreated.addListener(async (item) => {
  const s = await getSettings();
  if (!s.catchDownloads || item.state !== "in_progress" || !shouldCatch(item)) return;
  if (!(await ping(600))) return;                    // app closed: let the browser handle it
  try {
    await chrome.downloads.cancel(item.id);
    await chrome.downloads.erase({ id: item.id });
  } catch (e) {
    return;
  }
  const url = item.finalUrl || item.url;
  const name = item.filename ? item.filename.split(/[\\/]/).pop() : "";
  await sendToApp({ kind: "file", url, referer: item.referrer || "", filename: name });
});
