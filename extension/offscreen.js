let current = null;

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message?.target !== "offscreen" || message.type !== "play") return false;
  if (current) current.pause();
  current = new Audio(message.src);
  current.volume = Math.min(1, Math.max(0, Number(message.volume ?? 0.8)));
  current
    .play()
    .then(() => sendResponse({ ok: true }))
    .catch((error) => sendResponse({ ok: false, detail: String(error?.message || error) }));
  return true;
});
