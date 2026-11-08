"""One command, in this order: sessions (chess/quiz/review) -> regex fast path -> providers (Groq/Mistral agent loop)
-> Gemini (web questions, or when providers are down) -> local Ollama router.
The agent loop lets the model READ the user's data (recall), act with a tool, read the result and then answer."""
#linda.brain.py
import contextvars, datetime, inspect, json, re, threading, time
from concurrent.futures import ThreadPoolExecutor
from google.genai import types
from . import fastpath, memory_files, tools
from .i18n import LANGS
from .providers import build_providers, openai_tools, pick
from .util import online
from .web import CHANNEL, TURN


SYSTEM = """You are {name}, {user}'s personal assistant and friend: warm, upbeat, a little playful, never stiff or corporate. Talk like a close friend who is also very capable: use his name now and then, celebrate small wins, add a quick encouraging word.
Reply in the language of the user's latest message (English, Russian or Uzbek), even if earlier messages used another. Uzbek: Latin letters only (o‘, g‘, sh, ch), polite "siz", never mix scripts.
Everything you write is read aloud: 1-3 short sentences, no markdown, lists, emojis or URLs, unless the user asks for detail or a post.
You have tools. When the user wants something DONE, call the matching tool (several in one turn if needed); before a tool call say a very short, varied lead-in (2-6 words). Answer general knowledge, translations, maths and advice yourself. Anything needing current web information (news, weather, sports, prices, "latest") uses web_answer.
Memory: the index below lists what is stored. Before answering anything about the user's own data, and before saving, changing or deleting anything, call recall for that store and read the result, then decide whether to create, update or skip. Never claim something is missing without checking. After tools run, answer in the user's language, in your own words.
Use the music tool only when the user explicitly asks for music. If a required detail is missing, ask ONE short question.
Now: {now}.{notes}"""
SENT = re.compile(r"(?<=[.!?…])\s+|\n+")
WEB = re.compile(r"\b(news|weather|latest|forecast|score|price|rate)\b|новост|погод|курс\b|yangilik|ob-?havo", re.I)
QUICK = {"timer", "volume", "music", "mac", "open_app", "time", "battery", "telegram_send"}


LOCAL_MEMORY = [
    (re.compile(r"^\s*(?:(?:add|write|save|put)\s+(?:an?\s+|this\s+)?)?(?:idea|идея|g'oya)\b\s*[:,\-–]\s*(.+)$", re.I | re.S), "ideas"),
    (re.compile(r"^\s*(?:my\s+goal\s+is|goal\b|цель\b|maqsad(?:im)?\b)\s*[:,\-–]?\s*(?:to\s+)?(.+)$", re.I | re.S), "goals"),
    (re.compile(r"^\s*remember\s+(?:that\s+)?((?:i\b|i'm\b|my\b).+)$", re.I | re.S), "profile"),
]


def is_quota_error(e):
    s = str(e)
    return getattr(e, "code", None) == 429 or "RESOURCE_EXHAUSTED" in s or s.lstrip().startswith("429")


def retry_seconds(e, default=3600.0):
    s = str(e)
    m = re.search(r"retryDelay['\"]?:\s*['\"]?(\d+(?:\.\d+)?)s", s)
    if m:
        return float(m.group(1))
    m = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s", s)
    if m:
        return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3))
    return default


def _guard(fn, on_quota):
    def call(*a, **k):
        try:
            r = fn(*a, **k)
        except Exception as e:
            if is_quota_error(e):
                on_quota(e)
            raise
        if inspect.isgenerator(r):
            def gen():
                try:
                    yield from r
                except Exception as e:
                    if is_quota_error(e):
                        on_quota(e)
                    raise
            return gen()
        return r
    return call


def _parts(chunk):
    for cand in (getattr(chunk, "candidates", None) or []):
        for p in (getattr(getattr(cand, "content", None), "parts", None) or []):
            yield p


def _undouble(s):
    """Some small models answer 'X.X.': keep one."""
    s = (s or "").strip()
    for sep in ("", " "):
        h = (len(s) - len(sep)) // 2
        if h > 8 and s[:h] == s[h + len(sep):] and s[h:h + len(sep)] == sep:
            return s[:h]
    return s


class Capture:
    """Stands in for the speaker while a tool runs: collects what it would say, so the model can read it."""
    def __init__(self, real):
        self.real, self.lines = real, []

    def say(self, text, lang="en", **_):
        self.lines.append(text)

    def __getattr__(self, name):
        return getattr(self.real, name)


class Brain:
    def __init__(self, cfg, agent, speaker, gemini, set_lang=lambda c: None):
        self.cfg, self.agent, self.speaker, self.gemini, self.set_lang = cfg, agent, speaker, gemini, set_lang
        self.decls = tools.declarations() if gemini else []
        self.history, self.cancel, self._spoke = [], threading.Event(), False
        self.blocked_until, self._announced = 0.0, 0.0
        if gemini is not None:
            if not getattr(gemini, "_quota_guarded", False):
                for n in dir(gemini):
                    if not n.startswith("_") and inspect.ismethod(getattr(gemini, n)):
                        setattr(gemini, n, _guard(getattr(gemini, n), self._on_quota))
                gemini._quota_guarded = True
            cloud = agent.cloud
            agent.cloud = lambda: (not self.blocked()) and cloud()
            if getattr(agent, "router", None) is not None:
                agent.router.gemini_ok = lambda: not self.blocked()
        self.providers = build_providers(cfg)
        self.oa_tools = openai_tools() if self.providers else []
        agent.facts, agent.providers = self._facts, self.providers
        print(f"[providers] fallback chain: {[p.name for p in self.providers] or 'none'} -> local")

    # ---- quota ---------------------------------------------------------
    def blocked(self):
        return time.time() < self.blocked_until

    def _on_quota(self, e):
        wait = min(max(retry_seconds(e), 30.0), 600.0)
        self.blocked_until = time.time() + wait
        print(f"[gemini] quota reached: skipping it for {int(wait // 60)} min")

    def _announce(self):
        if time.time() >= self._announced:
            self._announced = self.blocked_until
            print("[gemini] blocked -> providers / local")

    def interrupt(self):
        self.cancel.set()
        self.speaker.stop()

    # ---- one command ---------------------------------------------------
    def handle(self, text, lang, timer):
        ag = self.agent
        print(f"\n[you] ({lang}) {text}")
        self.cancel = ev = threading.Event()
        TURN.set(ev)
        ag.lang, ag._produced, self.speaker.turn_log = (lang if lang in LANGS else "en"), False, []
        try:
            if ag.chess and ag.chess_turn(text):
                timer.route = "session:chess"
            elif ag.quiz and ag.quiz_turn(text):
                timer.route = "session:quiz"
            elif ag.pending_review and time.time() < ag.pending_review:
                ag.pending_review = 0
                ag._finish_review(text)
                timer.route = "session:review"
            elif fastpath.try_fast(ag, text, self.set_lang):
                timer.route = "fast"
            elif (self.cfg["brain"].get("providers_first", True) and self.providers
                  and not WEB.search(text) and self._remote_fallback(text)):
                timer.route = "provider"
            elif self.gemini is None or not ag.cloud():
                timer.route = "local" if self.blocked() else "offline"
                self._local(text, notify=self.gemini is not None and self.blocked())
            else:
                timer.route = "llm"
                try:
                    self._llm(text, ev, timer)
                except Exception as e:
                    if not self._spoke and not ev.is_set():
                        q = is_quota_error(e)
                        if q and not self.blocked():
                            self._on_quota(e)
                        print("[brain] gemini failed, falling back:", repr(e)[:120])
                        timer.route = "fallback"
                        self._local(text, notify=q)
                    else:
                        raise
        except Exception as e:
            print("[brain error]", repr(e))
            ag.k("error")
        finally:
            spoken = " ".join(self.speaker.turn_log).strip()
            print(f"[linda] {spoken or '(said nothing)'} | route={timer.route}")
            if spoken:
                self.history = (self.history + [("user", text), ("model", spoken)])[-2 * self.cfg["brain"]["history_turns"]:]

    def _local(self, text, notify=False):
        ag = self.agent
        if notify:
            self._announce()
        if self._remote_fallback(text):
            return
        for pat, file in LOCAL_MEMORY:
            m = pat.match(text)
            if m:
                memory_files.handle(ag, "save_note", {"file": file, "text": m.group(1).strip()})
                return
        if ag.router is not None:
            ag.run(text, ag.lang)
        else:
            ag.k("need_internet")

    # ---- prompt --------------------------------------------------------
    def _facts(self):
        try:
            return "Memory index (read any store with recall):\n" + tools.memory_index(self.agent)
        except Exception as e:
            print("[facts]", repr(e))
            return ""

    def _system(self, full=True):
        notes = self.agent.store.notes(20)
        block = ("\nThings the user asked you to remember:\n- " + "\n- ".join(notes)) if notes else ""
        ctx = memory_files.context_block()
        return (SYSTEM.format(name=self.cfg["name"], user=self.cfg.get("user_name") or "the user",
                              now=datetime.datetime.now().strftime("%A %Y-%m-%d %H:%M"), notes=block)
                + "\n\n" + (ctx if full else ctx[:1200]) + "\n\n" + self._facts())

    # ---- provider agent loop (Groq / Mistral) --------------------------
    def _remote_fallback(self, text):
        return bool(self.providers) and online() and self._agent_loop(text)

    def _call(self, name, args, text):
        if name == "recall":
            return tools.recall_data(self.agent, args)
        ag, real = self.agent, self.agent.speaker
        cap = Capture(real)
        ag.speaker = cap
        try:
            tools.execute(ag, name, args, text)
        except Exception as e:
            print(f"[tool {name} failed]", repr(e)[:150])
            return f"error: {e}"
        finally:
            ag.speaker = real
        res = " ".join(cap.lines) or "done"
        print(f"[tool result] {name} -> {res[:200]}")
        return res

    def _agent_loop(self, text):
        ag, ev, acted, last = self.agent, self.cancel, False, []
        defs = pick(self.oa_tools, text)
        msgs = [{"role": "system", "content": self._system(full=False)}]
        msgs += [{"role": "assistant" if r == "model" else "user", "content": m} for r, m in self.history]
        msgs.append({"role": "user", "content": f"{text}\n\n[Reply only in the language of the message above.]"})

        for step in range(4):
            print(f"[loop] step {step}: ~{(len(json.dumps(msgs, default=str)) + len(json.dumps(defs))) // 4} tokens in, {len(defs)} tools")
            m, used = None, ""
            for p in self.providers:
                if not p.ready():
                    continue
                try:
                    r = p.chat(msgs, self.oa_tools if step < 3 else None)
                except Exception as e:
                    p.cool(e)
                    print(f"[{p.name}] failed: {repr(e)[:150]}")
                    continue
                if not (r.content or "").strip() and not r.tool_calls and step == 0:
                    print(f"[{p.name}] empty answer, trying the next one")
                    continue
                m, used = r, p.name
                break
            if m is None:
                if acted:
                    ag.k("error")
                    return True
                return False
            calls = m.tool_calls or []
            print(f"[{used}] step {step} text={(m.content or '').strip()[:150]!r} calls={[(c.function.name, c.function.arguments) for c in calls]}")
            if ev.is_set():
                return True
            if not calls:
                ag.say(_undouble(m.content) or " ".join(last) or "Done.")
                self._spoke = True
                return True
            if (m.content or "").strip():
                ag.say(_undouble(m.content))                       # the short friendly lead-in
                self._spoke = True
            msgs.append({"role": "assistant", "content": m.content or "", "tool_calls": [
                {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments or "{}"}}
                for c in calls]})
            outs = []
            for c in calls:
                try:
                    args = json.loads(c.function.arguments or "{}")
                except ValueError:
                    args = {}
                res = self._call(c.function.name, args, text)
                acted = acted or c.function.name != "recall"
                outs.append((c.function.name, res))
                msgs.append({"role": "tool", "tool_call_id": c.id, "content": res[:6000]})
            last = [o for n, o in outs if n != "recall"]
            if all(n in QUICK for n, _ in outs):
                ag.say(" ".join(o for _, o in outs))
                self._spoke = True
                return True
        return True

    # ---- Gemini path (web questions or when providers are down) ----------
    def _flush(self, buf):
        parts = SENT.split(buf)
        for s in parts[:-1]:
            self.agent.say(s)
            self._spoke = True
        rest = parts[-1]
        if not self._spoke and len(rest) >= 40:
            m = re.match(r"(.{25,}?[,;:])\s", rest)
            if m:
                self.agent.say(m.group(1))
                self._spoke, rest = True, rest[m.end():]
        return rest

    def _llm(self, text, ev, timer):
        contents = [types.Content(role=r, parts=[types.Part.from_text(text=m)]) for r, m in self.history]
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=text)]))
        buf, calls, self._spoke = "", [], False
        for chunk in self.gemini.stream_tools(contents, self._system(), self.decls,
                                              self.cfg["gemini"].get("brain_model") or None):
            if ev.is_set():
                return
            for p in _parts(chunk):
                if getattr(p, "thought", False):
                    continue
                if getattr(p, "text", None):
                    if "default_api" in (buf + p.text):          # a tool call leaked as text: never speak it
                        buf = "default_api"
                        continue
                    timer.mark("first_token")
                    buf = self._flush(buf + p.text)
                fc = getattr(p, "function_call", None)
                if fc is not None and getattr(fc, "name", None):
                    timer.mark("first_token")
                    calls.append((fc.name, dict(fc.args or {})))
        print(f"[gemini] text={buf.strip()[:200]!r} calls={calls}")
        if buf.strip() and "default_api" not in buf and not ev.is_set():
            self.agent.say(buf)
        if calls and not ev.is_set():
            timer.mark("tools_start")
            self._run_tools(calls, text)

    def _run_tools(self, calls, text):
        seq = self.speaker.seq
        chans = [seq.new() for _ in calls]

        def run_one(name, args, ch):
            tok = CHANNEL.set(ch)
            try:
                tools.execute(self.agent, name, args, text)
            except Exception as e:
                print(f"[tool {name} failed]", repr(e))
                self.agent.k("error")
            finally:
                CHANNEL.reset(tok)
                seq.finish(ch)

        ordered = [(c, ch) for c, ch in zip(calls, chans) if c[0] in tools.SEQUENTIAL]
        parallel = [(c, ch) for c, ch in zip(calls, chans) if c[0] not in tools.SEQUENTIAL]

        def run_ordered():
            for (name, args), ch in ordered:
                run_one(name, args, ch)

        with ThreadPoolExecutor(max_workers=len(calls) + 1) as pool:
            futs = [pool.submit(contextvars.copy_context().run, run_ordered)] if ordered else []
            for (name, args), ch in parallel:
                futs.append(pool.submit(contextvars.copy_context().run, run_one, name, args, ch))
            for f in futs:
                f.result()