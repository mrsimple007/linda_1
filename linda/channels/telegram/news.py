"""News digest (RSS -> LLM picks and groups by index, links added by code) and the morning message."""
import concurrent.futures
import contextlib
import datetime
import html as _html
import re
import threading
import time
import xml.etree.ElementTree as ET
import requests
from linda import tools
from linda.channels.telegram.telegram import esc

# ============================ news ===============================================
def _fetch_feed(url, per):
    r = requests.get(url, timeout=8, headers={"User-Agent": "Linda/1.0"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    source = (root.findtext("./channel/title") or url.split("/")[2]).split(" - ")[0].split(":")[0].strip()
    out = []
    for it in root.iter("item"):
        title = _html.unescape((it.findtext("title") or "").strip())
        link = (it.findtext("link") or "").strip()
        desc = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _html.unescape(it.findtext("description") or ""))).strip()
        if title and link.startswith("http"):
            out.append({"title": title, "summary": desc[:200], "link": link, "source": source})
        if len(out) >= per:
            break
    return out


def rss_items(agent, per_feed=6):
    """Items from the feeds (config: news.feeds), fetched in parallel, duplicates removed."""
    feeds = (agent.cfg.get("news") or {}).get("feeds") or FEEDS
    items, seen = [], set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(feeds)) as pool:
        futs = [pool.submit(_fetch_feed, u, per_feed) for u in feeds]
        for u, f in zip(feeds, futs):
            try:
                for it in f.result():
                    key = re.sub(r"\W+", "", it["title"].lower())[:40]
                    if key not in seen:
                        seen.add(key)
                        items.append(it)
            except Exception as e:
                print("[rss]", u, repr(e)[:100])
    return items


def organize(agent, items, n, lang):
    """The LLM chooses and groups by index and rewrites in `lang`; code attaches the links. Falls back to raw items."""
    raw = [{"topic": it["source"], "title": it["title"], "summary": it["summary"][:140], "link": it["link"]}
           for it in items[:n]]
    listing = "\n".join(f"{i}. [{it['source']}] {it['title']}. {it['summary'][:160]}" for i, it in enumerate(items))
    system = (f"You are a news editor. From the numbered items choose the {n} most important and varied stories "
              "(never two items about the same event). Group them by a short topic name (World, Politics, Economy, "
              "Tech, Science, Health, Sport, Other). For each write a clear headline of at most 12 words and one "
              f"plain sentence of context, in {LANG_NAME[lang]}. Use only facts from the item text. Never output "
              'links. Return ONLY JSON: {"items":[{"i":0,"topic":"World","title":"...","summary":"..."}]}')
    obj = _json_obj(llm_text(agent, system, listing))
    out, used = [], set()
    for e in (obj or {}).get("items", []) if isinstance(obj, dict) else []:
        try:
            i = int(e["i"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= i < len(items)) or i in used or not str(e.get("title", "")).strip():
            continue
        used.add(i)
        out.append({"topic": str(e.get("topic") or "World")[:24], "title": str(e["title"]).strip(),
                    "summary": str(e.get("summary") or "").strip(), "link": items[i]["link"]})
    if len(out) < min(3, len(items)):
        print("[news] LLM unavailable or unusable, using raw headlines")
        return raw
    return out[:n]


def _gemini_news(agent, n, lang):
    """Last resort (no links): Gemini + Google Search."""
    if getattr(agent, "gemini", None) is None or not agent.cloud():
        return []
    q = (f"The {n} most important world news stories of the last 24 hours. Write in {LANG_NAME[lang]}. "
         "One story per line in exactly this form: Headline | one short sentence of context. "
         "No numbering, no markdown, no introduction.")
    try:
        text = "".join(_chunk_text(c) for c in agent.gemini.stream(q, agent._chat_system(), grounding=True))
    except Exception as e:
        print("[news gemini]", repr(e)[:120])
        return []
    out = []
    for line in text.splitlines():
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).replace("**", "").strip()
        if "|" in line:
            head, ctx = (x.strip() for x in line.split("|", 1))
            if head:
                out.append({"topic": "World", "title": head, "summary": ctx, "link": ""})
    return out[:n]


def get_news(agent, n=8, lang="en"):
    items = rss_items(agent)
    return organize(agent, items, n, lang) if items else _gemini_news(agent, n, lang)


def news_sections(digest):
    groups = {}
    for d in digest:
        title = esc(d["title"])
        head = (f'<a href="{_html.escape(d["link"], quote=True)}"><b>{title}</b></a>' if d["link"] else f"<b>{title}</b>")
        groups.setdefault(d["topic"], []).append(f"• {head}" + (f"\n   {esc(d['summary'])}" if d["summary"] else ""))
    return list(groups.items())


# ============================ silent tool runner =================================
_LOCK = threading.RLock()


class _Mute:
    """Stands in for the speaker: collects what Linda would say, so nothing is spoken on the Mac."""
    def __init__(self, real):
        self.real, self.lines = real, []

    def say(self, text, lang="en", **_):
        self.lines.append(str(text))

    def __getattr__(self, name):
        return getattr(self.real, name)


@contextlib.contextmanager
def muted(agent, text=""):
    with _LOCK:
        real, lang = agent.speaker, getattr(agent, "lang", "en")
        try:
            from linda.brain import reply_lang
            agent.lang = reply_lang(text, agent.cfg.get("default_language", "en")) if text else agent.cfg.get("default_language", "en")
        except Exception:
            pass
        m = _Mute(real)
        agent.speaker = m
        try:
            yield m
        finally:
            agent.speaker, agent.lang = real, lang


def _do(agent, name, args, text):
    """Run one of Linda's tools the way tools.execute does (incl. run_action's bookkeeping)."""
    def invoke():
        if name in tools.SPECIAL:
            tools.SPECIAL[name](agent, args, text)
        else:
            fields = tools.coerce(args, tools.SPEC[name][4])
            getattr(agent, "do_" + name)(tools.Intent(intent=name, **fields), text)
    agent.run_action(name, args, invoke)


# ============================ morning ============================================
def _open_today(agent):
    today = str(datetime.date.today())
    return [r for r in agent.store.open_tasks() if not r[2] or str(r[2]) <= today]


def send_morning(agent, tg, n_news=8):
    t, user = S[_lang(agent)], agent.cfg.get("user_name") or ""
    sections = news_sections(get_news(agent, n_news, _lang(agent)))
    try:
        rows = _open_today(agent)[:5]
        if rows:
            sections.append((t["tasks"], [f"• {esc(r[1])}" for r in rows]))
    except Exception as e:
        print("[morning tasks]", repr(e)[:100])
    try:
        names = [b.name for b in agent.store.upcoming(366) if b.n == 0]
        if names:
            sections.append((t["bday"], [f"🎂 {esc(x)}" for x in names]))
    except Exception as e:
        print("[morning birthdays]", repr(e)[:100])
    agent.plans_until = time.time() + PLANS_WINDOW
    return tg.send_styled(t["morning"].format(user=user).strip(", "),
                          sections, footer=t["footer"], icon="☀️", intro=t["intro"])


# names from the sibling modules (imported last, so mutual use works)
from linda.channels.telegram.common import FEEDS, LANG_NAME, PLANS_WINDOW, S, _chunk_text, _json_obj, _lang, llm_text  # noqa: E402
