# Linda: Process, Status and Roadmap

Last updated: 2026-11-06 · Owner: Muslimbek · Platform: macOS, Python 3.9 (end of life, see roadmap)

Status legend: ✅ seen working in the logs · 🟡 code written, not yet confirmed in your logs · 🔴 broken or open · ⬜ not started

---

## 1. What Linda is

A voice assistant for one person. She listens (Whisper, local), decides what to do (LLM with tools), acts (Python skills, SQLite, markdown notes), and answers by voice and on a web screen. English, Russian and Uzbek.

**Working principle:** code validates and stores, the LLM understands and phrases. The model should look data up itself, not be told rules per topic.

## 2. Architecture today

```
 Mic -> VAD -> Whisper (mlx large-v3-turbo) -> noise filter -> wake word / window -> text
 UI text box -----------------------------------------------------------------------^
                                    |
                 server.run_command (one command at a time, cancellable)
                                    |
   Brain.handle:  chess/quiz/review session -> fastpath rules -> own-facts rules?
                                    |
        +---------------------------+-----------------------------+
        | providers first (Groq, Groq-20b, Mistral)               | Gemini (web questions only,
        | Brain._agent_loop: recall -> write tools -> answer      |  news, weather; 20 req/day free)
        +---------------------------+-----------------------------+
                                    | all down
                          local router (Ollama) + sanitize()
                                    |
              tools.py TABLE -> Agent.do_* handlers (speak canned phrases)
                                    |
        Speaker (edge-tts, `say` fallback) <- sentences    UI <- websocket panels
```

### Where data lives

| Data | Store | Notes |
|---|---|---|
| Birthdays | `linda.db` table `birthdays`, mirrored to `data/birthdays.md` | Mirror rewritten after each change; md edits sync back every 60 s |
| Tasks | `tasks` (id, title, due, status, ...) | Time of day in side table `task_times` |
| Songs | `songs` | Composer and notes in side table `song_meta` |
| Practice | practice table (name not confirmed) | Weekly summary shown in UI |
| Profile, goals, ideas | `data/profile.md`, `goals.md`, `ideas.md` | "## Learned" section is written by memory consolidation |
| Progress log | `data/projects/<project>.md`, one dated line per entry | Indexed for notes search |
| Journal, notes, papers | `data/journal`, `notes`, `papers`, `projects` | Embedded with nomic-embed-text, `search_notes` |
| Conversation memory | summarised every 6 turns into profile "Learned" and journal | Replaced the old "Recent" panel |
| Secrets | `.env` | Gemini, Groq, Mistral, Telegram, Spotify |

### Main files

| File | Role |
|---|---|
| `server.py` | FastAPI, websocket, panels snapshot, birthday sync, memory consolidation |
| `linda/brain.py` | Command routing, quota handling, provider fallback, `_agent_loop` |
| `linda/providers.py` | OpenAI-compatible providers (Groq, Mistral), cooldowns, tool list `CORE` |
| `linda/tools.py` | Tool schemas (`TABLE`), executors, `recall_data`, `memory_index`, helpers |
| `linda/agent.py` | `do_*` skills, chat, chess, Telegram, birthdays |
| `linda/domains.py` | Practice, quiz, tasks, docs, review mixins |
| `linda/router.py` | Local intent classifier and `sanitize()` guard |
| `linda/fastpath.py` | Regex commands that need no LLM |
| `linda/ears.py`, `audio.py`, `stt.py` | Listening, noise filter, Whisper |
| `linda/tts.py` | Speech output |
| `ui/index.html`, `style.css`, `app.js` | Web screen |

## 3. What is done

### Reliability and models
- 🟡 Provider chain: Groq (gpt-oss-120b), Groq-20b, Mistral, then local Ollama. Each has its own cooldown.
- 🟡 Quota message is only logged, never spoken.
- 🟡 Providers are tried first; Gemini is kept for web questions to save its 20 requests per day.
- 🟡 Any Gemini failure (timeout, quota) falls back instead of silence.
- 🟡 Gemini timeout set to a minimum of 10 s (Gemini rejects less).
- 🟡 Empty provider answers move on to the next provider.
- 🟡 Duplicate tool names are removed by keeping the last `TABLE` row.

### Understanding and safety
- ✅ `sanitize()` rejects intents the user's words cannot support (guard active at startup).
- 🟡 Invented numbers for minutes are corrected from what was said; absurd values are asked back.
- 🟡 Noise filter in `ears.py` drops Whisper hallucinations ("Музыка", "Рецепт", "Субтитры...") and unknown single words.
- 🟡 Tool calls leaked as text (`default_api:...`) are never spoken.

### Memory
- 🟡 `recall(kind, query)` lets the model read any store; the prompt only carries a short index.
- 🟡 Agent loop: model recalls, calls a write tool, reads the result, answers in its own words.
- 🟡 Birthdays: DB and `birthdays.md` stay in sync both ways; a changed date updates the record.
- ✅ Progress log: "today I worked on..." is saved with its date and appears in answers.
- 🟡 Memory consolidation: every 6 informative turns (or 10 minutes idle) the LLM extracts lasting facts and one summary line.

### Skills
- ✅ Tasks: several per sentence, duplicates blocked by meaning, due date and time.
- 🟡 `task_done` by id after the model reads the list.
- ✅ Birthdays: own birthday saved under your name, year shown.
- 🟡 Songs: title, composer and notes; duplicates caught by base title.
- 🟡 Practice log with real dates ("yesterday"); `_backdate` was written without seeing `store.py`.
- 🟡 Spotify: search by keys plus AppleScript play; honest message when only the search page opens.

### Language and voice
- 🟡 LLM answers in the language of the user's message; the header selector only sets short built-in phrases.
- 🟡 Uzbek rules: Latin script only, polite "siz", short answers.
- 🟡 Voice choice by script and Uzbek word check in `tts.py`.
- 🟡 Friendlier personality in the system prompt.

### UI
- 🟡 New `index.html`, `style.css`, `app.js`: greeting, orb states, minimal panels, chess, quiz, galaxy. Recent panel removed.

## 4. Open problems

| # | Problem | Impact | Next step |
|---|---|---|---|
| 1 | 🔴 Telegram: tool runs, nothing arrives | Greetings and lists not delivered | Run the `Telegram.send` test; read `telegram.py` |
| 2 | 🔴 Gemini free tier: 20 requests per day per model | News, weather, web answers die after a few uses | Enable billing, or add a flash-lite model |
| 3 | 🔴 Groq free tier: 8K tokens per minute | 429s every few minutes, switching costs about 1 s | Shorter prompt, paid tier, or smaller tool list |
| 4 | 🟡 Local model path has no `recall` and no loop | Offline answers are weak | Ollama tool calling with qwen3:8b (`think=False`) |
| 5 | 🟡 Gemini path still single-shot and speaks canned phrases | Different behaviour from the Groq path | One loop for all providers |
| 6 | 🔴 Whisper hallucinates on silence and noise | Wasted requests, wrong commands | Set `no_speech_threshold` in `stt.py`; use headphones |
| 7 | 🟡 She can hear herself on speakers | Echo commands | Headphones or echo cancellation |
| 8 | 🟡 Canned phrases in `i18n.py` are dry | Tone is inconsistent | Rewrite warm phrases in three languages |
| 9 | 🟡 Uzbek quality on Groq and local models is weak | Wrong words, mixed scripts | Use Gemini for long Uzbek text when available |
| 10 | 🔴 Python 3.9 is end of life | Library warnings, future breakage | Move to 3.11 or newer |
| 11 | 🟡 Same name for two people overwrites a birthday | Data loss | Unique key on name and relation |

## 5. Roadmap

### P0: make it solid (this week)
1. Confirm every patch is applied: `python -m py_compile` on each changed file, one `TABLE` row per tool, startup log clean.
2. Fix Telegram delivery (problem 1).
3. Decide on Gemini billing (problem 2); it unlocks news, weather and long Uzbek replies.
4. Smoke test with the checklist in section 7.

### P1: make it smart
1. One agent loop for every provider, including Gemini and local Ollama (tool calling plus `recall`).
2. Web tools through a provider with browser search, so news and weather survive Gemini limits.
3. Write the warm phrases in `i18n.py`; let the loop phrase confirmations.
4. Whisper settings in `stt.py` and an echo plan (headphones or echo cancellation).
5. Router and tool decision log: table `router_log` (input, route, tool, arguments, result, ms).

### P2: make it trustworthy
1. Permission tiers: read-only runs; reversible runs and announces; sensitive (Telegram, files, calendar edits) asks; destructive needs confirmation.
2. Audit log table with arguments and confirmation.
3. Evaluation set: 30 to 50 commands in English, Russian and Uzbek with expected tool; run before each change.
4. Backups of `linda.db` with rotation; secrets into the macOS Keychain.

### P3: make it always there
1. Wake-word daemon (openWakeWord) started by `launchd`.
2. Semantic memory with `sqlite-vec`; "forget that" and "what do you know about X" voice commands.
3. Plugin-style tools: one file per skill with schema, tier and handler.
4. Menu-bar app, quiet hours, proactive briefings.

## 6. Rules for working on Linda

- Records live in the database, prose lives in markdown; never keep one fact in two places without sync.
- Reads go through `recall`; writes go through a domain tool that enforces safety in code (duplicates, valid dates, real ranges).
- Every write tool is idempotent: saying it twice must not save twice.
- No per-topic rules in the prompt. If the model errs, fix the tool, the data or the context.
- A new skill is: one `TABLE` row, one handler, add to `CORE` if it works offline of Gemini.
- Never say "done" unless the action succeeded; tools return a status.
- Test every change on all three languages.

## 7. Smoke test

| Say | Expect in the log |
|---|---|
| "When is Yusufbek's birthday?" | `step 0 ... recall(birthdays)`, then a spoken date |
| "Yusufbek is 2 October" | `recall`, `add_birthday`, "Updated: ..." and `birthdays.md` rewritten |
| "Add a task: call the bank today at 15:00" | one `task_add` with `time`; repeat gets "already on your list" |
| "I finished the rec system task" | `recall(tasks)`, then `task_done` with an id |
| "I practiced piano 30 minutes yesterday" | `practice_log` with `minutes: 30` and `[practice] backdated` |
| "What did I improve today?" | answer from the progress log |
| "Play The Nights by Avicii" | `music` result; plays, or says it only opened the search |
| "Send me the birthday list on Telegram" | `[telegram] send(... chars) -> ...` and a message on the phone |
| Silence for a minute | `[ears] ignored ...` lines, no model calls |

## 8. Files not yet reviewed

`store.py`, `memory_files.py`, `docs.py`, `stt.py`, `audio.py`, `telegram.py`, `i18n.py`, `planner.py`, `web.py`, `scheduler.py`, `gemini.py` (latest), `system.py`, `tests/`. Several patches (songs columns, practice table, birthday fields) were written without seeing them and should be checked against the real code.

## 9. Config and keys

```yaml
providers:    # tried in order
  - {name: groq,     model: openai/gpt-oss-120b}
  - {name: groq-20b, model: openai/gpt-oss-20b}
  - {name: mistral,  model: mistral-small-latest}   # confirm the name in the console
brain: {history_turns: 6, providers_first: true}
gemini: {timeout_s: 15}      # never below 10
local: {model: qwen3:8b, skip_offline_explain: true}
audio: {vad_aggressiveness: 3, conversation_window_s: 12}
```

`.env`: `GEMINI_API_KEY`, `GROQ_API_KEY`, `MISTRAL_API_KEY`, Telegram token and chat id, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`.

Spotify note: since the 2026 changes, development-mode apps need the owner to hold Premium, are limited to five users, and search returns at most 10 items. Search with app-only keys is unconfirmed; the fallback opens the search page.

## 10. Changelog

- **2026-11-06**: provider fallback chain; router guard; tasks, birthdays, songs, practice and progress log reworked; Spotify play; new UI; noise filter; memory consolidation; `recall` and agent loop.