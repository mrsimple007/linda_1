"""One command, in this order: sessions (chess/quiz/review) -> regex fast path -> providers (Groq/Mistral agent loop)
-> Gemini (web questions, or when providers are down) -> local Ollama router.
The agent loop lets the model READ the user's data (recall), act with a tool, read the result and then answer."""
# linda/brain.py
import contextvars, datetime, inspect, json, re, threading, time
from concurrent.futures import ThreadPoolExecutor
from google.genai import types
from . import fastpath, memory_files, tools
from .providers import build_providers, openai_tools, pick
from .util import online
from .web import CHANNEL, TURN


SYSTEM = """You are {name}, {user}'s personal assistant and friend: warm, upbeat, a little playful, never stiff or corporate. Talk like a close friend who is also very capable: use his name now and then, celebrate small wins, add a quick encouraging word.
Language: the user's message ends with a tag like [Reply in English.]. Follow that tag exactly, even if stored data or earlier messages are in another language. Never mix languages in one answer. Uzbek: Latin letters only (o‘, g‘, sh, ch), polite "siz".
Everything you write is read aloud: 1-3 short sentences, no markdown, lists, emojis or URLs, unless the user asks for detail or a post.
You have tools. When the user wants something DONE, call the matching tool (several in one turn if needed); before a tool call say a very short, varied lead-in (2-6 words). Answer general knowledge, translations, maths and advice yourself. Anything needing current web information (news, weather, sports, prices, "latest") uses web_answer.
Memory: the index below lists what is stored. Before answering anything about the user's own data, and before saving, changing or deleting anything, read it (stored data may already be attached below; otherwise call recall for that store) and decide whether to create, update or skip. Never claim something is missing without checking. State stored facts exactly as stored; do not add details that are not in the data. After tools run, answer in your own words.
Use the music tool only when the user explicitly asks for music. If a required detail is missing, ask ONE short question.
Now: {now}.{notes}"""
NEWS_ASK = re.compile(r"\b(?:news|headlines)\b|новост|yangilik", re.I)
BDAY_ASK = re.compile(r"\bbirthdays?\b.*\blist\b|\blist\b.*\bbirthdays?\b|список.*дн\w+ рожд|дни рождения.*список", re.I)
SENT = re.compile(r"(?<=[.!?…])\s+|\n+")
WEB = re.compile(r"\b(news|weather|latest|forecast|score|price|rate)\b|новост|погод|курс\b|yangilik|ob-?havo", re.I)
QUICK = {"timer", "volume", "music", "mac", "open_app", "time", "battery",
         "telegram_send", "log_progress", "remember"}

# Advice, planning, wishes: slower, longer, warmer, with the full picture of the user (voice commands stay fast and short).
THINK = re.compile(
    r"what should i|what (?:do|can) i do (?:today|now|tonight|next|with)|what to do|suggest|advice|recommend|any ideas|ideas for|"
    r"i'?m bored|plan (?:my|the) (?:day|week|evening|weekend)|what do you think|help me (?:decide|choose|think|plan)|\bshould i\b|"
    r"how (?:can|do) i (?:improve|learn|get better)|\bwish(?:es)?\b|congratulat|\bgift|present for|"
    r"что мне делать|чем заняться|посоветуй|предложи|идеи|что думаешь|поздрав|подарок|"
    r"nima qilsam|nima qilay|maslahat|taklif|fikring|tabrik|sovg['‘’ʻ`]?a", re.I)
THINK_RULES = (
    "Mode: thoughtful friend and coach for {user}. Think about what you know of him first: goals, ideas, interests, "
    "projects, open tasks, recent work, practice, the journal. This mode overrides the 1-3 sentence limit: answer in 4-7 "
    "warm, natural sentences, still no markdown, lists or emojis, because it is read aloud.\n"
    "- 'What should I do' and similar: give 2-3 concrete suggestions tied to his goals and plans, say briefly why each "
    "fits, then ask which one to put on his task list. Do NOT just read back his tasks. When he agrees (even with 'yes' "
    "or 'the first one'), call task_add once per task, using the suggestions from the conversation.\n"
    "- Wishes, greetings, messages, posts: write the full text, warm and personal to that person (relation, age from "
    "'turning', shared facts from about_person). Write it in the recipient's greeting_lang if given; otherwise in the "
    "language that person most likely speaks (Uzbek for Uzbek names) and say which language you chose. For a birthday, "
    "add 2-3 specific gift ideas. When he asks to send it, call telegram_send with the whole text (the Reply tag governs "
    "your spoken answer, not the language of the message you send), then tell him in 2-3 sentences what you sent, "
    "including the gift ideas.\n"
    "- Be honest and useful: give your own view and a reason, not flattery.")

LANG_NAME = {"en": "English", "ru": "Russian", "uz": "Uzbek"}
UZ_MARK = re.compile(r"\b(qachon|nima|mening|menga|bugun|kecha|qanday|iltimos|bormi|tug['‘’ʻ`]?ilgan|yo['‘’ʻ`]q)\b", re.I)


def reply_lang(text, tag):
    """The reply language is decided here, not by the model.
    Cyrillic majority -> ru; Uzbek marker words -> uz; the Whisper/UI tag only counts on longer phrases; else English."""
    letters = re.findall(r"[^\W\d_]", text or "")
    if letters and sum("\u0400" <= c <= "\u04ff" for c in letters) / len(letters) > 0.5:
        return "ru"                       # a Cyrillic name inside an English sentence does not flip it
    if UZ_MARK.search(text or ""):
        return "uz"
    if tag in ("ru", "uz") and len((text or "").split()) >= 5:
        return tag
    return "en"


LOCAL_MEMORY = [
    (re.compile(r"^\s*(?:(?:add|write|save|put)\s+(?:an?\s+|this\s+)?)?(?:idea|идея|g'oya)\b\s*[:,\-–]\s*(.+)$", re.I | re.S), "ideas"),
    (re.compile(r"^\s*(?:my\s+goal\s+is|goal\b|цель\b|maqsad(?:im)?\b)\s*[:,\-–]?\s*(?:to\s+)?(.+)$", re.I | re.S), "goals"),
    (re.compile(r"^\s*remember\s+(?:that\s+)?((?:i\b|i'm\b|my\b).+)$", re.I | re.S), "profile"),
]


def is_quota_error(e):
    s = str(e)
    return getattr(e, "code", None) == 429 or "RESOURCE_EXHAUSTED" in s or s.lstrip().startswith("429") \
        or "RateLimitError" in repr(e) or "rate limit" in s.lower()


def retry_seconds(e, default=3600.0):
    s = str(e)
    m = re.search(r"retryDelay['\"]?:\s*['\"]?(\d+(?:\.\d+)?)s", s)
    if m:
        return float(m.group(1))
    m = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?(\d+(?:\.\d+)?)s", s)
    if m:
        return int(m.group(1) or 0) * 3600 + int(m.group(2) or 0) * 60 + float(m.group(3))
    return default


def short_wait(e):
    """Seconds a provider asks us to wait ('try again in 2.5s' / '1m3.2s'); 99 if unknown."""
    m = re.search(r"try again in (?:(\d+)m)?(\d+(?:\.\d+)?)s", str(e))
    if m:
        return int(m.group(1) or 0) * 60 + float(m.group(2))
    m = re.search(r"try again in (\d+)ms", str(e))
    return int(m.group(1)) / 1000 if m else 99.0


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


def _tool_name(d):
    return (d.get("function") or {}).get("name") if isinstance(d, dict) else None


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
        self._spoke = False
        ag.lang, ag._produced, self.speaker.turn_log = reply_lang(text, lang), False, []
        try:
            if ag.pending_actions and ag.handle_confirmation(text):
                timer.route = "confirmation"
            elif ag.chess and ag.chess_turn(text):
                timer.route = "session:chess"
            elif ag.quiz and ag.quiz_turn(text):
                timer.route = "session:quiz"
            elif ag.pending_review and time.time() < ag.pending_review:
                ag.pending_review = 0
                ag._finish_review(text)
                timer.route = "session:review"
            elif fastpath.try_fast(ag, text, self.set_lang):
                timer.route = "fast"
            elif NEWS_ASK.search(text) and len(text.split()) <= 14:      # RSS + LLM: never waits on Gemini's quota
                timer.route = "news"
                tools.execute(ag, "news", {}, text)
                self._spoke = True
            elif BDAY_ASK.search(text) and len(text.split()) <= 14:      # full list straight from the database
                timer.route = "birthdays"
                tools.execute(ag, "list_birthdays", {}, text)
                self._spoke = True
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
        # Provider path (full=False): no profile/notes dump. The prefetch and recall bring only the matching
        # lines, which saves ~1K tokens a step and stops stale Uzbek profile text from competing with the data.
        notes = self.agent.store.notes(20) if full else []
        block = ("\nThings the user asked you to remember:\n- " + "\n- ".join(notes)) if notes else ""
        ctx = memory_files.context_block() if full else ""
        return (SYSTEM.format(name=self.cfg["name"], user=self.cfg.get("user_name") or "the user",
                              now=datetime.datetime.now().strftime("%A %Y-%m-%d %H:%M"), notes=block)
                + ("\n\n" + ctx if ctx else "") + "\n\n" + self._facts())

    # ---- provider agent loop (Groq / Mistral) --------------------------
    def _remote_fallback(self, text):
        return bool(self.providers) and online() and self._agent_loop(text)

    def _call(self, name, args, text):
        if name == "recall":
            return tools.recall_data(self.agent, args, text)
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

    def _try(self, p, msgs, tool_defs, step, think=False):
        """One provider call. Mid-turn, a short rate-limit wait is better than dropping to a weaker model."""
        opts = {"effort": "medium", "temp": 0.7, "timeout": 25} if think else {}
        try:
            return p.chat(msgs, tool_defs, **opts)
        except Exception as e:
            wait = short_wait(e)
            if is_quota_error(e) and step > 0 and wait <= 6:
                print(f"[{p.name}] rate limit, waiting {wait:.1f}s")
                time.sleep(wait + 0.3)
                return p.chat(msgs, tool_defs, **opts)
            raise

    def _agent_loop(self, text):
        ag, ev, acted, last, done = self.agent, self.cancel, False, [], []
        L = LANG_NAME.get(ag.lang, "English")
        defs = list(pick(self.oa_tools, text))
        if not any(_tool_name(d) == "recall" for d in defs):          # reading must always be possible
            defs += [d for d in self.oa_tools if _tool_name(d) == "recall"]
        think = bool(THINK.search(text))
        ctx = tools.prefetch(ag, text, think=think)
        system = self._system(full=think)
        if ctx:
            system += ("\n\nStored data already searched for this message. Answer from it. Call recall only if "
                       "something is missing, and never say something is missing before reading this:\n" + ctx)
        system += (f"\n\nRules: answer in {L}. State relations exactly as stored (nephew stays nephew; add no "
                   "older/younger or other details). A birthday row with match=partial is a different person with a "
                   "similar name: say so. If several people fit (e.g. several brothers), list them all. "
                   "For a question about one day, use recall(kind='day') once.")
        if think:
            system += "\n\n" + THINK_RULES.format(user=self.cfg.get("user_name") or "the user")
        msgs = [{"role": "system", "content": system}]
        n_hist = int(self.cfg["brain"].get("provider_history_turns", 2) or 0)       # follow-ups ("yes, add it") need the last turns
        n_hist = max(n_hist, 3) if think else n_hist
        for role, m in (self.history[-2 * n_hist:] if n_hist else []):
            msgs.append({"role": "assistant" if role == "model" else "user", "content": m})
        msgs.append({"role": "user", "content": f"{text}\n\n[Reply in {L}.]"})

        for step in range(4):
            tool_defs = defs if step < 3 else None
            print(f"[loop] step {step}: ~{(len(json.dumps(msgs, default=str)) + len(json.dumps(defs))) // 4} tokens in, {len(defs)} tools")
            m, used = None, ""
            for p in self.providers:
                if not p.ready():
                    continue
                try:
                    r = self._try(p, msgs, tool_defs, step, think)
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
                    ok = [x for x in done if not x.startswith("error")]
                    if ok:
                        ag.say(" ".join(ok))             # the action worked: say its result, not a bare error
                    else:
                        ag.k("error")
                    self._spoke = True
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
            # the short lead-in is spoken only before real actions, not before a silent recall
            if (m.content or "").strip() and any(c.function.name != "recall" for c in calls):
                ag.say(_undouble(m.content))
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
                # the language tag goes to the model only; `outs` stays clean because QUICK results are spoken verbatim
                msgs.append({"role": "tool", "tool_call_id": c.id, "content": res[:6000] + f"\n[Reply in {L}.]"})

            last = [o for n, o in outs if n != "recall"]
            done += last
            if ag.pending_actions:                       # a tool asked for confirmation: say it once, stop
                ag.say(" ".join(last))
                self._spoke = True
                return True
            quick = QUICK - {"telegram_send"} if think else QUICK      # in think mode the model says what it sent
            if last and all(n in quick for n, _ in outs if n != "recall"):
                ag.say(" ".join(last))
                self._spoke = True
                return True
            # otherwise fall through: next step, the model reads the tool results and answers
        return True                                      # after the for loop, NOT inside it

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
        L = LANG_NAME.get(self.agent.lang, "English")
        contents = [types.Content(role=r, parts=[types.Part.from_text(text=m)]) for r, m in self.history]
        contents.append(types.Content(role="user", parts=[types.Part.from_text(text=f"{text}\n\n[Reply in {L}.]")]))
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