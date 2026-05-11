const MAX_TEXT_CHARS = 60000;
const MIN_TEXT_CHARS = 10;
let lastHash = "";
let pendingTimer = null;

function conversationId() {
  const match = location.pathname.match(/\/c\/([^/?#]+)/);
  if (match) return match[1];
  const projectMatch = location.pathname.match(/\/app\/([^/?#]+)/);
  if (projectMatch) return projectMatch[1];
  return location.pathname;
}

function visibleText() {
  const main = document.querySelector("main") || document.body;
  const fallbackText = (document.body?.innerText || main?.innerText || "").trim();
  const messageNodes = [
    ...main.querySelectorAll("[data-message-author-role], article, .markdown, [data-testid]")
  ];
  const parts = [];
  for (const node of messageNodes) {
    const text = (node.innerText || "").trim();
    if (text && text.length > 20) parts.push(text);
  }
  const joined = parts.length ? parts.join("\n\n") : fallbackText;
  if (joined.trim().length < MIN_TEXT_CHARS && fallbackText.length >= MIN_TEXT_CHARS) {
    return fallbackText.slice(0, MAX_TEXT_CHARS);
  }
  return joined.trim().slice(0, MAX_TEXT_CHARS);
}

async function sha256(text) {
  const data = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function sendSnapshot() {
  const text = visibleText();
  if (!text || text.length < MIN_TEXT_CHARS) return;
  const hash = await sha256(`${location.href}\n${text}`);
  if (hash === lastHash) return;
  lastHash = hash;
  const payload = {
    app: location.hostname,
    title: document.title || "Untitled conversation",
    url: location.href,
    conversation_id: conversationId(),
    captured_at: new Date().toISOString(),
    content_hash: hash,
    text
  };
  try {
    await chrome.runtime.sendMessage({type: "activity_journal_snapshot", payload});
  } catch (_error) {
  }
}

function scheduleSnapshot(delay = 1500) {
  if (pendingTimer) clearTimeout(pendingTimer);
  pendingTimer = setTimeout(sendSnapshot, delay);
}

const observer = new MutationObserver(() => scheduleSnapshot());
observer.observe(document.documentElement, {childList: true, subtree: true, characterData: true});
window.addEventListener("popstate", () => scheduleSnapshot(500));
window.addEventListener("focus", () => scheduleSnapshot(500));
scheduleSnapshot(1000);
