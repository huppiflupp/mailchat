"""Chat loop: the local LLM searches the index through tool calls, then answers."""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import urllib.request
from collections.abc import Iterator

from . import config, i18n, llmhost
from .search import mail_ref, read_mail, search

MAX_ROUNDS = 8
MIN_SEARCHES = 3

SYSTEM = """You are a search assistant for the user's e-mail mailbox. Today is {today}.
You know NOTHING about the mails except what the tools return.

How to search:
- Search with `search_mails`. It matches whole words and word beginnings, but not parts in the
  middle of a word ("invoice" finds "invoices", German "antrag" finds "Antragsbuch" but not
  "Parteitagsantrag").
- If there are few or unsuitable hits, try variants: synonyms, abbreviations, both languages
  if the mailbox is mixed, sender names, single words instead of phrases, the `sender` filter,
  date ranges.
- For questions like "Do I still have to do something?" read all matching mails (newsletters
  and circulars too) and list open tasks, deadlines and appointments.
- Read promising mails with `read_mail` before you state details such as links, dates or amounts.
- Never invent links, content or meanings of abbreviations. Quote links exactly as they appear
  in the tool result, preferably as a Markdown link [text](URL).
- Every new question needs a new search. Earlier answers in the conversation do not replace it.

Answer:
- In {language}, short and friendly{informal}. Refer to every mail you mention with [#id],
  e.g. [#4711] – only ids from tool results.
- Give requested links/documents directly. With several candidates, newest first, and tell
  them apart briefly.
- If nothing was found, say so and say what you searched for."""

NUDGE_SEARCH = ("(Instruction from the search system, not from the user: you have not found any "
                "mail yet. Call search_mails now, with other words or a single word.)")
NUDGE_READ = ("(Instruction from the search system, not from the user: read the most relevant mail "
              "with read_mail before you answer – search snippets miss dates and amounts.)")

NUDGE_ANSWER = ("(Instruction from the search system, not from the user: your reply was empty. "
                "Answer the user's question now from the tool results.)")

TOOLS = [
    {"type": "function", "function": {
        "name": "search_mails",
        "description": "Full-text search over all mails (subject, sender, recipients, body, "
                       "link texts, attachment names). Returns id, date, sender, subject, folders, "
                       "snippet, matching links.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "Search words, e.g. 'invoice electricity'. "
                                                        "All words must occur; otherwise falls back to any of them."},
            "sender": {"type": "string", "description": "Part of the sender name or address"},
            "after": {"type": "string", "description": "Only mails from date YYYY-MM-DD on"},
            "before": {"type": "string", "description": "Only mails before date YYYY-MM-DD"},
            "folder": {"type": "string", "description": "Part of the folder name, e.g. 'Archive'"},
            "sort": {"type": "string", "enum": ["relevance", "newest"]},
            "limit": {"type": "integer", "description": "max. hits (default 10, max 30)"},
        }}}},
    {"type": "function", "function": {
        "name": "read_mail",
        "description": "Reads one mail completely: headers, body, all links, attachment names.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer"}}, "required": ["id"]}}},
]


def llm(messages: list[dict], tools: str = "auto", timeout: int = 300) -> dict:
    """tools: "auto", "required" (must call a tool) or "none" (must answer)."""
    body = {"model": config.LLM_MODEL, "messages": messages, "temperature": 0.3,
            # Thinking off: much faster and not needed for search. The first key is
            # read by llama-server, the second by Ollama; each ignores the other.
            "chat_template_kwargs": {"enable_thinking": False}, "reasoning_effort": "none"}
    if tools != "none":
        body["tools"] = TOOLS
        body["tool_choice"] = tools
    headers = {"Content-Type": "application/json"}
    if config.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"
    req = urllib.request.Request(config.LLM_URL, data=json.dumps(body).encode(), headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        msg = json.loads(resp.read())["choices"][0]["message"]
    llmhost.touch()
    return msg


def _links(lines: list[str]) -> list[dict]:
    """Index stores links as "text -> url"; the model gets them as objects so it
    does not copy the arrow into its answer."""
    out = []
    for line in lines:
        text, sep, url = line.rpartition(" -> ")
        out.append({"text": text, "url": url} if sep else {"url": line})
    return out


def _call_tool(con: sqlite3.Connection, name: str, args: dict) -> dict:
    if name == "search_mails":
        allowed = {"query", "sender", "after", "before", "folder", "sort", "limit"}
        res = search(con, **{k: v for k, v in args.items() if k in allowed})
        for r in res.get("results", []):
            r["matching_links"] = _links(r["matching_links"])
        if not res.get("results"):
            res["hint"] = ("0 hits. Do not give up: search on with variants – written "
                           "together instead of hyphenated, only the word stem, synonyms, "
                           "single words, the sender.")
        return res
    if name == "read_mail":
        res = read_mail(con, int(args.get("id", 0)))
        if "links" in res:
            res["links"] = _links(res["links"])
        return res
    return {"error": f"unknown tool {name}"}


def _urls(result: dict) -> set[str]:
    rows = result.get("results") or [result]
    return {l["url"] for r in rows
            for l in (r.get("matching_links") or r.get("links") or []) if l.get("url")}


def chat(con: sqlite3.Connection, history: list[dict], lang: str = "en") -> Iterator[dict]:
    """Yield progress events; the last one has type 'answer'.

    history: [{"role": "user"|"assistant", "content": str}, …] — plain chat turns
    from the client; tool traffic of earlier turns is not kept.
    """
    for text in llmhost.ensure(lang):
        yield {"type": "status", "text": text}
    today = dt.date.today().strftime("%A, %Y-%m-%d")
    lang = i18n.norm(lang)
    system = SYSTEM.format(today=today, language=i18n.LANGUAGE_NAMES[lang],
                           informal=", addressing the user informally (du)" if lang == "de" else "")
    messages = [{"role": "system", "content": system}]
    messages += [{"role": m["role"], "content": str(m["content"])}
                 for m in history if m.get("role") in ("user", "assistant")]
    # Mails the model may legitimately cite: tool results of this turn and
    # mails already cited in earlier answers. url -> mail id for uncited links.
    known_ids = {int(x) for m in history if m.get("role") == "assistant"
                 for x in re.findall(r"#(\d+)", str(m.get("content", "")))}
    url_owner: dict[str, int] = {}
    read_any = False

    for rnd in range(MAX_ROUNDS + 1):
        size = sum(len(str(m.get("content", ""))) for m in messages)
        # Without a forced first search the model happily answers follow-up
        # questions from the chat history alone — and invents mails.
        # Small models give up after one empty search, so they must keep searching
        # until something turns up (at most MIN_SEARCHES rounds). And they answer from
        # search snippets, which miss deadlines and amounts, so once there are hits
        # they must read at least one mail before answering.
        must_search = not known_ids and rnd < MIN_SEARCHES
        must_read = known_ids and not read_any and rnd < MIN_SEARCHES + 1
        mode = ("none" if rnd == MAX_ROUNDS or size > config.LLM_CONTEXT_CHARS
                else "required" if rnd == 0 or must_search or must_read
                else "auto")
        msg = llm(messages, tools=mode)
        calls = msg.get("tool_calls") or []
        if not calls and mode == "required":
            # Not every server enforces tool_choice (Ollama ignores it), so the
            # rule is repeated as a message and the premature answer dropped.
            messages.append({"role": "user", "content": NUDGE_READ if known_ids else NUDGE_SEARCH})
            continue
        if not calls:
            text = (msg.get("content") or "").strip()
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
            if not text and rnd < MAX_ROUNDS:  # an empty reply happens now and then
                messages.append({"role": "user", "content": NUDGE_ANSWER})
                continue
            cited = [int(x) for x in re.findall(r"#(\d+)", text)]
            cited += [mid for url, mid in url_owner.items() if url in text]
            refs, invented = [], []
            for mid in dict.fromkeys(cited):
                if mid not in known_ids:
                    invented.append(mid)
                elif (ref := mail_ref(con, mid)):
                    refs.append(ref)
            if invented:
                text += i18n.t(lang, "invented", ids=", ".join(f"#{i}" for i in invented),
                               pl=i18n.t(lang, "invented_pl") if len(invented) > 1 else "")
            yield {"type": "answer", "text": text, "mails": refs}
            return
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         "tool_calls": calls})
        for call in calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            yield {"type": "tool", "name": name, "args": args}
            read_any |= name == "read_mail"
            try:
                result = _call_tool(con, name, args)
            except Exception as e:
                result = {"error": str(e)}
            rows = result.get("results") if name == "search_mails" else [result]
            for r in rows or []:
                if "id" in r:
                    known_ids.add(r["id"])
                    for url in _urls(r):
                        url_owner.setdefault(url, r["id"])
            if name == "search_mails":
                yield {"type": "hits", "count": len(rows or []),
                       "subjects": [h["subject"] for h in (rows or [])[:5]]}
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                             "content": json.dumps(result, ensure_ascii=False)})
    # unreachable: the last round runs without tools and always returns
