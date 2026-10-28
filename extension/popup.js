const DEFAULTS = { videoButton: true, imageButton: true, catchDownloads: true };
const keys = Object.keys(DEFAULTS);

chrome.storage.sync.get(DEFAULTS, (s) => {
  for (const k of keys) {
    const el = document.getElementById(k);
    el.checked = !!s[k];
    el.addEventListener("change", () => chrome.storage.sync.set({ [k]: el.checked }));
  }
});

function setStatus(ok) {
  document.getElementById("dot").className = "dot " + (ok ? "on" : "off");
  document.getElementById("statusText").textContent =
    ok ? "Connected to OpenDM app" : "OpenDM app is not running";
}

chrome.runtime.sendMessage({ type: "opendm-ping" }, (res) => setStatus(res && res.ok));

document.getElementById("grab").addEventListener("click", async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !/^https?:/i.test(tab.url || "")) return;
  const btn = document.getElementById("grab");
  btn.textContent = "Sending...";
  chrome.runtime.sendMessage(
    { type: "opendm-download", req: { kind: "media", page_url: tab.url, referer: tab.url, title: tab.title } },
    (res) => {
      btn.textContent = res && res.ok ? "Sent to OpenDM" : (res && res.error) || "Failed";
      setTimeout(() => window.close(), 1200);
    }
  );
});
