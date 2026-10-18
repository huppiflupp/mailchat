// Chat page: talks to the local mailchat server, renders answers, opens cited mails.

const API = "http://127.0.0.1:8095/api";
const log = document.getElementById("log");
const input = document.getElementById("input");
const form = document.getElementById("form");
const sendBtn = document.getElementById("send");
const statusEl = document.getElementById("status");
const LANG = messenger.i18n.getUILanguage();
const _ = (key, arg) => messenger.i18n.getMessage(key, arg === undefined ? undefined : [String(arg)]);
const HEADERS = { "Content-Type": "application/json", "Accept-Language": LANG };

for (const el of document.querySelectorAll("[data-i18n]")) el.textContent = _(el.dataset.i18n);
for (const el of document.querySelectorAll("[data-i18n-title]")) el.title = _(el.dataset.i18nTitle);
for (const el of document.querySelectorAll("[data-i18n-placeholder]")) el.placeholder = _(el.dataset.i18nPlaceholder);
document.documentElement.lang = LANG;

let history = [];
const mailsById = new Map(); // id -> {msgid, subject, …} for [#id] chips

// ------------------------------------------------------------------ helpers

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// Tiny Markdown subset: paragraphs, lists, **bold**, `code`, [text](url), bare URLs, [#id].
function renderMarkdown(md) {
  const inline = (s) => {
    // Links go into placeholders first so later rules never touch URLs.
    const slots = [];
    const keep = (html) => `\u0000${slots.push(html) - 1}\u0000`;
    let t = esc(s);
    t = t.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, (m0, label, url) => {
      label = label.replace(/\s*-&gt;\s*https?:\/\/\S+$/, ""); // "Text -> URL" from the index
      return keep(`<a href="${url}" data-ext="1">${label}</a>`);
    });
    t = t.replace(/https?:\/\/[^\s<)\u0000]+/g, (url) => {
      // Markdown/punctuation glued to the end ("…pdf**", "…pdf.") is not part of the URL.
      const tail = url.match(/[*_.,;:!?'"]+$/)?.[0] || "";
      url = url.slice(0, url.length - tail.length);
      return keep(`<a href="${url}" data-ext="1">${url}</a>`) + tail;
    });
    t = t.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    t = t.replace(/`([^`]+)`/g, "<code>$1</code>");
    t = t.replace(/\[?#(\d+)\]?/g, (m0, id) =>
      `<a class="cite" data-mail="${id}" title="${esc(_("openMailTitle"))}">#${id}</a>`);
    return t.replace(/\u0000(\d+)\u0000/g, (m0, i) => slots[i]);
  };
  const out = [];
  let list = null;
  for (const raw of md.split("\n")) {
    const line = raw.trimEnd();
    const li = line.match(/^\s*(?:[-*•]|\d+\.)\s+(.*)$/);
    if (li) {
      if (!list) { list = []; }
      list.push(`<li>${inline(li[1])}</li>`);
      continue;
    }
    if (list) { out.push(`<ul>${list.join("")}</ul>`); list = null; }
    if (line.trim() === "") continue;
    out.push(`<p>${inline(line.replace(/^#+\s*/, ""))}</p>`);
  }
  if (list) out.push(`<ul>${list.join("")}</ul>`);
  return out.join("");
}

function add(cls, html) {
  const hint = log.querySelector(".hint");
  if (hint) hint.remove();
  const div = document.createElement("div");
  div.className = cls;
  div.innerHTML = html;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}

// ------------------------------------------------------------------ actions

async function openMail(id) {
  const m = mailsById.get(Number(id));
  if (!m) return;
  try {
    await messenger.messageDisplay.open({ headerMessageId: m.msgid, location: "tab" });
  } catch (e) {
    // Fallback: look the message up ourselves (e.g. older Thunderbird).
    try {
      const page = await messenger.messages.query({ headerMessageId: m.msgid });
      if (page.messages.length) {
        await messenger.messageDisplay.open({ messageId: page.messages[0].id, location: "tab" });
        return;
      }
    } catch (e) { /* ignore */ }
    add("msg error", esc(_("mailNotFound", m.subject)));
  }
}

log.addEventListener("click", (ev) => {
  const a = ev.target.closest("a, .mail");
  if (!a) return;
  ev.preventDefault();
  if (a.dataset.mail) openMail(a.dataset.mail);
  else if (a.dataset.ext) messenger.windows.openDefaultBrowser(a.href);
});

function describeTool(ev) {
  const a = ev.args || {};
  if (ev.name === "read_mail") return _("toolRead", a.id);
  const parts = [];
  if (a.query) parts.push(_("toolQuery", a.query));
  if (a.sender) parts.push(_("toolFrom", a.sender));
  if (a.folder) parts.push(_("toolIn", a.folder));
  if (a.after) parts.push(_("toolAfter", a.after));
  if (a.before) parts.push(_("toolBefore", a.before));
  return _("toolSearch", parts.join(" ") || _("toolAll"));
}

async function ask(question) {
  history.push({ role: "user", content: question });
  add("msg user", esc(question));
  const steps = add("steps", "");
  sendBtn.disabled = true;
  try {
    const resp = await fetch(`${API}/chat`, {
      method: "POST",
      headers: HEADERS,
      body: JSON.stringify({ messages: history, lang: LANG }),
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    let answered = false;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl);
        buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.type === "status") {
          steps.insertAdjacentHTML("beforeend", `<div>${esc(ev.text)}</div>`);
        } else if (ev.type === "tool") {
          steps.insertAdjacentHTML("beforeend", `<div>${esc(describeTool(ev))}</div>`);
        } else if (ev.type === "hits") {
          steps.lastElementChild?.insertAdjacentText("beforeend", _("hits", ev.count));
        } else if (ev.type === "answer") {
          answered = true;
          for (const m of ev.mails) mailsById.set(m.id, m);
          history.push({ role: "assistant", content: ev.text });
          let html = renderMarkdown(ev.text);
          if (ev.mails.length) {
            html += `<div class="mails">` + ev.mails.map((m) =>
              `<div class="mail" data-mail="${m.id}" title="${esc(_("openMailTitle"))}">
                 <span class="id">#${m.id}</span><span class="subj">${esc(m.subject || _("noSubject"))}</span>
                 <span class="meta">${esc(m.date)} · ${esc(m.from)} · ${esc(m.folders.join(", "))}</span>
               </div>`).join("") + `</div>`;
          }
          add("msg", html);
        } else if (ev.type === "error") {
          add("msg error", esc(ev.text));
        }
        log.scrollTop = log.scrollHeight;
      }
    }
    if (!answered) history.pop();
  } catch (e) {
    history.pop();
    add("msg error", esc(_("noServer", e.message)));
  } finally {
    sendBtn.disabled = false;
    input.focus();
  }
}

// ------------------------------------------------------------------ status

async function refreshStatus() {
  try {
    const s = await (await fetch(`${API}/status`)).json();
    const parts = [_("statusMails", s.mails.toLocaleString(LANG))];
    if (s.indexing) parts.push(_("statusIndexing"));
    else if (s.last_index) parts.push(_("statusLastIndex", s.last_index.slice(11, 16)));
    if (s.index_error) parts.push(_("statusIndexError", s.index_error));
    const llmBad = !s.llm && !s.llm_on_demand;
    if (!s.llm) parts.push(s.llm_on_demand ? _("statusLlmOnDemand") : _("statusLlmMissing", s.llm_model));
    // Voice input is optional: without a whisper server the button simply goes away.
    micBtn.hidden = !s.whisper;
    if (micState === "idle") input.placeholder = _(s.whisper ? "placeholder" : "placeholderNoVoice");
    statusEl.textContent = parts.join(" · ");
    statusEl.classList.toggle("bad", llmBad || !!s.index_error);
  } catch (e) {
    statusEl.textContent = _("statusNoServer");
    statusEl.classList.add("bad");
  }
}

document.getElementById("reindex").addEventListener("click", async () => {
  try { await fetch(`${API}/reindex`, { method: "POST" }); } catch (e) { /* status shows it */ }
  setTimeout(refreshStatus, 500);
});

document.getElementById("clear").addEventListener("click", () => {
  history = [];
  log.innerHTML = "";
  input.focus();
});

form.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const q = input.value.trim();
  if (!q || sendBtn.disabled) return;
  input.value = "";
  ask(q);
});

input.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    form.requestSubmit();
  }
});

// ------------------------------------------------------------------ voice

const micBtn = document.getElementById("mic");
let micState = "idle"; // idle | rec | busy

async function toggleVoice() {
  if (micState === "busy") return;
  if (micState === "idle") {
    try {
      const r = await (await fetch(`${API}/voice/start`, { method: "POST", headers: HEADERS })).json();
      if (r.error) { add("msg error", esc(r.error)); return; }
      micState = "rec";
      micBtn.classList.add("rec");
      input.placeholder = _("recording");
    } catch (e) {
      add("msg error", esc(_("recFailed")));
    }
    return;
  }
  micState = "busy";
  micBtn.classList.remove("rec");
  micBtn.classList.add("busy");
  input.placeholder = _("recognizing");
  try {
    const r = await (await fetch(`${API}/voice/stop`, { method: "POST", headers: HEADERS })).json();
    if (r.error) add("msg error", esc(r.error));
    else if (r.text) {
      input.value = (input.value ? input.value + " " : "") + r.text;
      form.requestSubmit();
    } else add("msg error", esc(_("nothingHeard")));
  } catch (e) {
    add("msg error", esc(_("voiceFailed", e.message)));
  } finally {
    micState = "idle";
    micBtn.classList.remove("busy");
    input.placeholder = _("placeholder");
    input.focus();
  }
}

async function cancelVoice() {
  if (micState !== "rec") return;
  try { await fetch(`${API}/voice/cancel`, { method: "POST", headers: HEADERS }); } catch (e) { /* ignore */ }
  micState = "idle";
  micBtn.classList.remove("rec");
  input.placeholder = _("placeholder");
}

micBtn.addEventListener("click", toggleVoice);
document.addEventListener("keydown", (ev) => {
  if (ev.ctrlKey && ev.code === "Space" && !micBtn.hidden) { ev.preventDefault(); toggleVoice(); }
  else if (ev.key === "Escape") cancelVoice();
});
window.addEventListener("beforeunload", () => {
  if (micState === "rec") navigator.sendBeacon?.(`${API}/voice/cancel`);
});

refreshStatus();
setInterval(refreshStatus, 15000);
