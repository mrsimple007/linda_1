"""Telegram bot that only ever messages YOU (the chat id in .env). You forward what you like.

send()         plain text: use it for text you want to forward (greetings, wishes)
send_html()    HTML-formatted, falls back to plain text if Telegram rejects it
send_styled()  a "card": title, sections, footer. Sent as a widget only when telegram.widgets is true
send_pretty()  any text, wrapped in a titled quote block (news, weather, stats...)
listen()       long-polls your replies; only messages from your own chat id reach the callback
"""
import html as _html
import json
import os
import re
import threading
import time
import zlib

import requests

WIDGET_MAX_PACKED = 3800   # server limit is 4096 bytes after zlib level 9; keep a margin
WIDGET_MAX_RAW = 32768
TAG = re.compile(r"<[^>]+>")
STYLES = {"news": ("📰", "World news"), "weather": ("🌤", "Weather"), "stats": ("📊", "Progress"),
          "plan": ("🗓", "Plan"), "morning": ("☀️", "Good morning"), "tasks": ("✅", "Tasks")}


def esc(s):
    return _html.escape(str(s), quote=False)


def plain_of(html_text):
    return _html.unescape(TAG.sub("", html_text.replace("<br>", "\n"))).strip()


def card(title, sections=(), footer="", icon="✨", intro=""):
    """sections: [(heading, [html lines])]. Lines are raw HTML (escape user text with esc()).
    title, intro, footer and headings are escaped here. Returns (html, plain)."""
    out = [f"<b>{icon} {esc(title)}</b>"]
    if intro:
        out.append(f"\n{esc(intro)}")
    for heading, lines in sections:
        if not lines:
            continue
        body = "\n".join(lines) if isinstance(lines, (list, tuple)) else str(lines)
        head = f"<b>{esc(heading)}</b>\n" if heading else ""
        out.append(f"\n{head}<blockquote>{body}</blockquote>")
    if footer:
        out.append(f"\n<i>{esc(footer)}</i>")
    h = "\n".join(out)
    return h, plain_of(h)


def widget_doc(html_text):
    """Card HTML -> mini-widget document (only div/b/i/br and one tiny stylesheet)."""
    body = (html_text.replace("<blockquote>", "<div class=q>").replace("</blockquote>", "</div>")
            .replace("\n", "<br>"))
    return ("<!doctype html><meta charset=utf-8><style>"
            "body{margin:0;padding:12px;font:15px/1.45 system-ui,sans-serif}"
            ".q{margin:4px 0 10px;padding:8px 10px;border-radius:10px;background:rgba(127,127,127,.15)}"
            "</style>" + body)


def _chunks(text, limit=4000):
    """Split on blank lines so every HTML block (and its tags) stays whole."""
    out, cur = [], ""
    for p in text.split("\n\n"):
        if cur and len(cur) + len(p) + 2 > limit:
            out.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return out


ICONS = (("birthday", "🎂"), ("news", "📰"), ("headline", "📰"), ("task", "✅"), ("plan", "🗓"),
         ("weather", "🌤"), ("practice", "🎹"), ("goal", "🎯"), ("song", "🎵"))
BULLET = re.compile(r"^\s*(?:[-*•–]|\d+[.)])\s+(.*)$")
DASH = re.compile(r"\s[–—]\s|\s-\s")


def beautify(text):
    """List-like plain text -> styled HTML card. Returns None for ordinary prose (greetings stay plain,
    so they forward cleanly)."""
    lines = text.strip().splitlines()
    if sum(1 for l in lines if BULLET.match(l)) < 3:
        return None
    title, out, quote = "Linda", [], []

    def flush():
        if quote:
            out.append("<blockquote>" + "\n".join(quote) + "</blockquote>")
            quote.clear()

    for i, l in enumerate(lines):
        m = BULLET.match(l)
        if m:
            parts = DASH.split(m.group(1), maxsplit=1)
            quote.append("• " + (f"<b>{esc(parts[0])}</b> — {esc(parts[1])}" if len(parts) == 2 else esc(m.group(1))))
        elif not l.strip():
            flush()
        elif i == 0 and len(l) < 90:
            title = l.strip().rstrip(":")
        else:
            flush()
            out.append(esc(l.strip()))
    flush()
    icon = next((ic for k, ic in ICONS if k in title.lower()), "📌")
    return f"<b>{icon} {esc(title)}</b>\n\n" + "\n\n".join(out)


class Telegram:
    def __init__(self, token, chat_id, widgets=False, api="https://api.telegram.org"):
        self.token, self.chat_id = token, chat_id
        self.widgets, self.api = widgets, api.rstrip("/")
        self._offset = 0

    @classmethod
    def from_env(cls, cfg=None):
        token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        if not (token and chat):
            return None
        widgets = bool(((cfg or {}).get("telegram") or {}).get("widgets"))
        return cls(token, chat, widgets, os.getenv("TELEGRAM_API", "https://api.telegram.org"))

    def _url(self, method):
        return f"{self.api}/bot{self.token}/{method}"

    # ---- plain ----------------------------------------------------------
    def send(self, text):
        """Lists are sent as a styled card automatically; prose (greetings, wishes) stays plain."""
        h = beautify(text) if text and text.strip() else None
        if h:
            return self.send_html(h, text)
        return self._send_plain(text)

    def _send_plain(self, text):
        if not text or not text.strip():
            print("[telegram] refused: empty text")
            return False
        for i in range(0, len(text), 4000):
            r = requests.post(self._url("sendMessage"),
                              data={"chat_id": self.chat_id, "text": text[i:i + 4000]}, timeout=20)
            r.raise_for_status()
        print(f"[telegram] sent {len(text)} chars")
        return True

    def send_file(self, path, caption=""):
        with open(path, "rb") as f:
            r = requests.post(self._url("sendDocument"),
                              data={"chat_id": self.chat_id, "caption": caption},
                              files={"document": f}, timeout=60)
        r.raise_for_status()

    # ---- styled ---------------------------------------------------------
    def send_html(self, body, plain=None):
        if not body or not body.strip():
            print("[telegram] refused: empty text")
            return False
        try:
            for part in _chunks(body):
                r = requests.post(self._url("sendMessage"), data={
                    "chat_id": self.chat_id, "text": part, "parse_mode": "HTML",
                    "link_preview_options": json.dumps({"is_disabled": True})}, timeout=20)
                r.raise_for_status()
            print(f"[telegram] sent html, {len(body)} chars")
            return True
        except Exception as e:
            print("[telegram] html failed, sending plain:", repr(e)[:120])
            return self._send_plain(plain or plain_of(body))

    def send_widget(self, html_text, caption=""):
        doc = widget_doc(html_text)
        packed = len(zlib.compress(doc.encode("utf-8"), 9))
        if len(doc.encode("utf-8")) > WIDGET_MAX_RAW or packed > WIDGET_MAX_PACKED:
            raise ValueError(f"widget too large ({packed} B packed)")
        # VERIFY the parameter names against the Bot API 10.4 beta (scripts/conformance/test-bot.ts in widget-sandbox)
        r = requests.post(self._url("sendWidget"), json={
            "chat_id": self.chat_id, "html": doc, "width": 0, "height": 0, "caption": caption[:1024]}, timeout=20)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "sendWidget failed"))
        return True

    def send_styled(self, title, sections=(), footer="", icon="✨", intro=""):
        h, plain = card(title, sections, footer, icon, intro)
        # a widget's caption is the fallback on clients that cannot render it, so it must hold the whole text
        if self.widgets and len(plain) <= 1000:
            try:
                return self.send_widget(h, caption=plain)
            except Exception as e:
                print("[telegram] widget failed, sending html:", repr(e)[:120])
        return self.send_html(h, plain)

    def send_pretty(self, text, kind=None):
        """Any text in a titled quote block. Long text goes out plain."""
        text = (text or "").strip()
        if not text:
            return False
        if len(text) > 3500:
            return self._send_plain(text)
        icon, title = STYLES.get(kind, ("💬", "Linda"))
        return self.send_html(f"<b>{icon} {esc(title)}</b>\n\n<blockquote>{esc(text)}</blockquote>", text)

    # ---- bot menu / typing indicator --------------------------------------
    def set_commands(self, commands):
        """commands: [(name, description)] -> the "/" menu in the chat."""
        try:
            r = requests.post(self._url("setMyCommands"), json={
                "commands": [{"command": c, "description": d} for c, d in commands]}, timeout=15)
            r.raise_for_status()
        except Exception as e:
            print("[telegram] setMyCommands failed:", repr(e)[:100])

    def typing(self):
        try:
            requests.post(self._url("sendChatAction"), data={"chat_id": self.chat_id, "action": "typing"}, timeout=5)
        except Exception:
            pass

    # ---- incoming -------------------------------------------------------
    def listen(self, on_text):
        """Long-poll in a daemon thread. Only text from your own chat id reaches on_text(text)."""
        def skip_backlog():
            try:
                r = requests.get(self._url("getUpdates"), params={"offset": -1, "timeout": 0}, timeout=15).json()
                res = r.get("result") or []
                if res:
                    self._offset = res[-1]["update_id"] + 1
            except Exception as e:
                print("[telegram] backlog check failed:", repr(e)[:100])

        def loop():
            skip_backlog()
            while True:
                try:
                    r = requests.get(self._url("getUpdates"), params={
                        "offset": self._offset, "timeout": 25, "allowed_updates": json.dumps(["message"])},
                        timeout=40).json()
                    for u in r.get("result") or []:
                        self._offset = u["update_id"] + 1
                        m = u.get("message") or {}
                        if str((m.get("chat") or {}).get("id")) != str(self.chat_id):
                            continue
                        text = (m.get("text") or "").strip()
                        if text:
                            try:
                                on_text(text)
                            except Exception as e:
                                print("[telegram] handler failed:", repr(e)[:150])
                except Exception as e:
                    print("[telegram] poll error:", repr(e)[:100])
                    time.sleep(5)

        threading.Thread(target=loop, daemon=True).start()