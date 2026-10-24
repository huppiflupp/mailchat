// Opens the chat page in a Thunderbird tab (or focuses it if already open).

const CHAT_URL = messenger.runtime.getURL("chat.html");

async function openChat() {
  const tabs = await messenger.tabs.query({});
  const existing = tabs.find((t) => t.url && t.url.startsWith(CHAT_URL));
  if (existing) {
    await messenger.tabs.update(existing.id, { active: true });
    await messenger.windows.update(existing.windowId, { focused: true });
  } else {
    await messenger.tabs.create({ url: CHAT_URL });
  }
}

messenger.action.onClicked.addListener(openChat);
messenger.commands.onCommand.addListener((cmd) => {
  if (cmd === "open-chat") openChat();
});

// Thunderbird finds updates through update_url in the manifest. Apply them right
// away instead of at the next restart; the chat tab reloads with the new version.
messenger.runtime.onUpdateAvailable.addListener(() => messenger.runtime.reload());
