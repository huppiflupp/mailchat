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
const SPARKS = "thunderai-sparks@micz.it"; // opens Thunderbird's event dialog for us

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
  if (!a || a.hasAttribute("download")) return;
  ev.preventDefault();
  if (a.dataset.mail) openMail(a.dataset.mail);
  else if (a.dataset.ext) messenger.windows.openDefaultBrowser(a.href);
});

// ------------------------------------------------------------------ files

const filesEl = document.getElementById("files");
const fileInput = document.getElementById("file");
let pending = [];            // files for the next question: {name, file, state, id?, error?}
const fileStore = new Map(); // file id -> File, kept for drafts later in the chat

function fileChip(name, cls = "", extra = "") {
  return `<span class="chip ${cls}">📄 ${esc(name)}${extra}</span>`;
}

function renderPending() {
  filesEl.hidden = !pending.length;
  filesEl.innerHTML = pending.map((f, i) => {
    const note = f.state === "busy" ? ` · ${esc(_("fileReading"))}`
      : f.state === "bad" ? ` · ${esc(f.error)}`
      : f.state === "unreadable" ? ` · ${esc(_("fileNoText"))}` : "";
    return fileChip(f.name, f.state === "busy" ? "busy" : f.state === "bad" ? "bad" : "",
      `${note}<button type="button" data-drop="${i}" title="${esc(_("fileRemove"))}">✕</button>`);
  }).join("");
}

filesEl.addEventListener("click", (ev) => {
  const b = ev.target.closest("[data-drop]");
  if (!b) return;
  pending.splice(Number(b.dataset.drop), 1);
  renderPending();
});

function fileName(id) {
  return fileStore.get(id)?.name || id;
}

function toBase64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1] || "");
    r.onerror = () => reject(r.error);
    r.readAsDataURL(file);
  });
}

// The server extracts the text once (OCR for scans and photos); the model reads it by id.
async function addFiles(list) {
  for (const file of list) {
    const f = { name: file.name, file, state: "busy" };
    pending.push(f);
    renderPending();
    try {
      const resp = await fetch(`${API}/file`, {
        method: "POST", headers: HEADERS,
        body: JSON.stringify({ name: file.name, type: file.type, data: await toBase64(file) }),
      });
      const r = await resp.json();
      if (r.error) throw new Error(r.error);
      f.id = r.id;
      f.state = r.readable ? "ok" : "unreadable";
      fileStore.set(r.id, file);
    } catch (e) {
      f.state = "bad";
      f.error = e.message;
    }
    renderPending();
  }
}

document.getElementById("attach").addEventListener("click", () => fileInput.click());
fileInput.addEventListener("change", () => {
  addFiles([...fileInput.files]);
  fileInput.value = "";
});
document.addEventListener("dragover", (ev) => {
  if (!ev.dataTransfer?.types.includes("Files")) return;
  ev.preventDefault();
  document.body.classList.add("drop");
});
document.addEventListener("dragleave", (ev) => {
  if (!ev.relatedTarget) document.body.classList.remove("drop");
});
document.addEventListener("drop", (ev) => {
  document.body.classList.remove("drop");
  if (!ev.dataTransfer?.files.length) return;
  ev.preventDefault();
  addFiles([...ev.dataTransfer.files]);
});

// ------------------------------------------------------------------ calendar and drafts

function icsText(e) {
  const day = (s) => s.slice(0, 8);
  const fold = (s) => s.replace(/[\\;,]/g, (c) => "\\" + c).replace(/\n/g, "\\n");
  const stamp = new Date().toISOString().replace(/[-:]/g, "").slice(0, 15) + "Z";
  return ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//mailchat//EN", "BEGIN:VEVENT",
    `UID:${crypto.randomUUID()}@mailchat`, `DTSTAMP:${stamp}`,
    e.forceAllDay ? `DTSTART;VALUE=DATE:${day(e.startDate)}` : `DTSTART:${e.startDate}`,
    e.forceAllDay ? `DTEND;VALUE=DATE:${day(e.endDate)}` : `DTEND:${e.endDate}`,
    `SUMMARY:${fold(e.summary)}`,
    ...(e.location ? [`LOCATION:${fold(e.location)}`] : []),
    ...(e.description ? [`DESCRIPTION:${fold(e.description)}`] : []),
    "END:VEVENT", "END:VCALENDAR", ""].join("\r\n");
}

// Returns null when the dialog opened, else a reason.
async function openEvent(e) {
  // Never any attendees: the event is for the user's own calendar only.
  const data = {
    summary: e.summary, startDate: e.startDate, endDate: e.endDate,
    forceAllDay: e.forceAllDay, location: e.location, description: e.description,
    use_timezone: true, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
  };
  try {
    const r = await messenger.runtime.sendMessage(SPARKS,
      { action: "openCalendarEventDialog", calendar_event_data: JSON.stringify(data) });
    return r === "ok" ? null : String(r?.error || r);
  } catch (err) {
    return "nosparks";
  }
}

async function showEvent(e) {
  const meta = [e.forceAllDay && e.start_text === e.end_text ? e.start_text
    : `${e.start_text} – ${e.end_text}`, e.location].filter(Boolean).join(" · ");
  const card = add("msg", `<div class="card"><span class="icon">📅</span>
      <span class="title">${esc(e.summary)}</span><button type="button">${esc(_("openAgain"))}</button>
      <span class="meta">${esc(meta)}</span></div>`).querySelector(".card");
  const open = async () => {
    card.querySelector(".note")?.remove();
    const why = await openEvent(e);
    if (!why) return;
    const url = URL.createObjectURL(new Blob([icsText(e)], { type: "text/calendar" }));
    const name = e.summary.replace(/[^\p{L}\p{N} _-]+/gu, "").slice(0, 60) || "event";
    card.insertAdjacentHTML("beforeend", `<span class="note">${
      esc(why === "nosparks" ? _("noSparks") : _("eventFailed", why))}
      <a href="${url}" download="${esc(name)}.ics">${esc(_("downloadIcs"))}</a></span>`);
  };
  card.querySelector("button").addEventListener("click", open);
  await open();
}

async function mailAttachment(msgid, name) {
  const page = await messenger.messages.query({ headerMessageId: msgid });
  for (const m of page.messages) {
    const att = (await messenger.messages.listAttachments(m.id)).find((a) => a.name === name);
    if (att) return messenger.messages.getAttachmentFile(m.id, att.partName);
  }
  return null;
}

function bodyHtml(text) {
  return text.split(/\n{2,}/).map((p) => `<p>${esc(p).replace(/\n/g, "<br>")}</p>`).join("");
}

async function openCompose(d) {
  const files = [], missing = [];
  for (const a of d.attachments) {
    let file = null;
    try {
      file = a.file ? fileStore.get(a.file) : await mailAttachment(a.msgid, a.name);
    } catch (e) { /* reported below */ }
    if (file) files.push({ file, name: a.name });
    else missing.push(a.name);
  }
  // Both bodies: Thunderbird then uses the format the identity is set to.
  await messenger.compose.beginNew({
    to: d.to, subject: d.subject, body: bodyHtml(d.body), plainTextBody: d.body,
    attachments: files,
  });
  return missing;
}

async function showCompose(d) {
  const meta = [d.to.join(", "), ...d.attachments.map((a) => "📎 " + a.name)].join("\n");
  const card = add("msg", `<div class="card"><span class="icon">✉️</span>
      <span class="title">${esc(d.subject || _("noSubject"))}</span>
      <button type="button">${esc(_("openAgain"))}</button>
      <span class="meta">${esc(meta)}</span></div>`).querySelector(".card");
  const open = async () => {
    card.querySelector(".note")?.remove();
    let note = "";
    try {
      const missing = await openCompose(d);
      if (missing.length) note = _("attachMissing", missing.join(", "));
    } catch (e) {
      note = _("composeFailed", e.message);
    }
    if (note) card.insertAdjacentHTML("beforeend", `<span class="note">${esc(note)}</span>`);
  };
  card.querySelector("button").addEventListener("click", open);
  await open();
}

function describeTool(ev) {
  const a = ev.args || {};
  if (ev.name === "read_mail") return _("toolRead", a.id);
  if (ev.name === "read_attachment") return _("toolAttachment", a.name || `#${a.id}`);
  if (ev.name === "read_file") return _("toolFile", fileName(a.file));
  if (ev.name === "find_contact") return _("toolContact", a.name);
  if (ev.name === "create_event") return _("toolEvent", a.summary);
  if (ev.name === "compose_mail") return _("toolCompose", (a.to || []).join(", "));
  const parts = [];
  if (a.query) parts.push(_("toolQuery", a.query));
  if (a.sender) parts.push(_("toolFrom", a.sender));
  if (a.folder) parts.push(_("toolIn", a.folder));
  if (a.after) parts.push(_("toolAfter", a.after));
  if (a.before) parts.push(_("toolBefore", a.before));
  return _("toolSearch", parts.join(" ") || _("toolAll"));
}

async function ask(question, files = []) {
  // The model sees the files as [file id: "name"]; the user sees chips.
  const content = question + files.map((f) => `\n[file ${f.id}: "${f.name}"]`).join("");
  history.push({ role: "user", content });
  add("msg user", esc(question) + (files.length
    ? "<div>" + files.map((f) => fileChip(f.name)).join("") + "</div>" : ""));
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
        } else if (ev.type === "event") {
          await showEvent(ev.data);
        } else if (ev.type === "compose") {
          await showCompose(ev.data);
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
  pending = [];
  renderPending();
  log.innerHTML = "";
  input.focus();
});

form.addEventListener("submit", (ev) => {
  ev.preventDefault();
  const q = input.value.trim();
  if (!q || sendBtn.disabled) return;
  if (pending.some((f) => f.state === "busy")) {
    add("msg error", esc(_("fileStillReading")));
    return;
  }
  const files = pending.filter((f) => f.id);
  pending = [];
  renderPending();
  input.value = "";
  ask(q, files);
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
