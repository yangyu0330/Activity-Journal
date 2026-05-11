const ACTIVITY_JOURNAL_ENDPOINT = "http://127.0.0.1:8765/events";

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (!message || message.type !== "activity_journal_snapshot" || !message.payload) {
    return false;
  }
  fetch(ACTIVITY_JOURNAL_ENDPOINT, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(message.payload)
  })
    .then((response) => sendResponse({ok: response.ok, status: response.status}))
    .catch((error) => sendResponse({ok: false, error: String(error)}));
  return true;
});
