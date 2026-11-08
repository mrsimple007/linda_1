# Linda

A local-first, hybrid voice assistant for macOS ("JARVIS for one person"), built around a MacBook Pro M1 Pro with 16 GB RAM.
You talk to her in **English, Russian or Uzbek**. She runs as a small local web server with a browser interface: Chrome listens
and speaks, **one streaming Gemini call with native tool calling** understands the command, Python carries it out, and
everything she knows about you lives in one local SQLite database. (The older terminal version with local Whisper is still
included as `main.py`, see [Two ways to run](#two-ways-to-run).)

- [What she can do](#what-she-can-do)
- [Two ways to run](#two-ways-to-run)
- [How it works](#how-it-works)
- [Project structure](#project-structure)
- [Install](#install)
- [Files you fill in](#files-you-fill-in)
- [Using Linda](#using-linda)
- [Run it always](#run-it-always)
- [Configuration](#configuration)
- [The database](#the-database)
- [Privacy: what leaves your Mac](#privacy-what-leaves-your-mac)
- [Limitations](#limitations)
- [Testing and troubleshooting](#testing-and-troubleshooting)
- [Extending Linda](#extending-linda)

## What she can do

| Area | Examples |
|---|---|
| **Quick actions** | time, battery, open an app, volume, mute, lock screen, screenshot, music play/pause/next, timers and labelled reminders, "remember that...", work with your clipboard |
| **Latest download** | "Convert my latest download to a quiz": finds the newest file in Downloads, sends it to Gemini, writes a `.txt` named after the topic. Reads Word tables (including bold/highlighted/coloured right answers), PDFs, images, text |
| **Information** | world news (en/ru/uz), weather, morning briefing, any question (Gemini + Google Search; the local model answers offline) |
| **Birthdays** | store with relation and greeting language, daily voice and notification, a ready-to-forward greeting sent to *your* Telegram after confirmation |
| **Calendar** | add events by voice, read today/tomorrow/this week, undo, a warning 10 minutes before each event (macOS Calendar, so Google/iCloud accounts work) |
| **Telegram** | sends messages and files to you only; "send that to my Telegram" |
| **Chess** | voice moves, Stockfish opponent, graphical board, hint, undo, every game saved as PGN |
| **Music practice** | repertoire in `songs.yaml`, practice log, explainable session planner, real pitch/timing analysis of your playing, guitar tuner |
| **Driving theory** | voice quiz from your own question files; questions you miss come back more often; results logged |
| **Tasks, journal, reviews** | to-do list, journal entries, evening check-in that extracts tomorrow's focus and new tasks |
| **Your notes** | semantic search over notes, papers, project docs and journal: "what did I decide about the database?" |

## Two ways to run

| | **Web mode** (default): `python server.py` | **Classic mode**: `python main.py` |
|---|---|---|
| Ears | Chrome speech recognition (free, streaming, live transcript) | local Whisper + voice activity detection |
| Understanding | one Gemini call with function calling (36 tools) | small local model classifies ~35 intents |
| Speed | first audio in about 1 to 2 s; trivial commands (time, volume, mute, music) answered by regex in ~50 ms without any LLM | several model calls in series |
| Interface | orb, live transcript, calendar/tasks/plan panels, quiz card, chess board (click to move), notes galaxy | terminal + Tk chess window |
| Interrupt | say "Linda" or "stop", or optional voice barge-in with headphones | press Enter |
| Offline | fast path + small local model as fallback | everything except Gemini features |
| Audio privacy | **Chrome sends everything it hears to Google's speech service** | audio stays local until after the wake word |

Web mode in short: the browser page at `http://127.0.0.1:4700` listens continuously, checks for the wake word "Linda" in the
transcript, then sends only the command text to the server. In-progress chess/quiz/review answers and the regex fast path never
touch the LLM. Everything else goes to one streaming Gemini call: its text is spoken sentence by sentence while its tool calls
run (independent tools in parallel, tools that write data in order, speech always in the order requested). Speech is synthesised
by edge-tts per sentence and played in the browser as it arrives. Each command prints a per-stage latency line in the terminal.

## How it works (classic pipeline)

```
 mic ──► VAD ──► Whisper (local, MLX) ──► wake word "Linda"? ──► (optional) Gemini re-transcribes the command
                                                                    │
                                              small local model (Ollama) classifies it into an INTENT (JSON)
                                                                    │
                       ┌────────────────────────────────────────────┴───────────────────────────┐
                       ▼                                                                        ▼
        Python skills (time, apps, chess, quiz,                                  Gemini (files, news, weather,
        planner, audio analysis, tasks, DB)                                      questions, greetings, explanations)
                       └────────────────────────────┬───────────────────────────────────────────┘
                                                    ▼
                         ONE SQLite database  ◄── scheduler (briefings, reminders, check-ins)
                                                    ▼
                    TTS (edge-tts online, macOS `say` offline) ──► speaker      Telegram ──► you
```

Design rules:

1. **The small model decides *what*, never *what is true*.** It only turns speech into a structured intent. Open-ended
   answers come from Gemini; numbers come from Python and the database.
2. **Explanations are grounded.** The planner and the audio analyser compute results and reasons in Python; the LLM only
   phrases them and may not invent or reorder items.
3. **Hybrid by default.** No internet: routing, chess, quiz, tasks, planning, audio analysis, search and chat (small local
   model) still work. Gemini-only features say "I need an internet connection."
4. **One database, not six assistants.** Practice history informs the planner, quiz results inform the planner, reviews
   feed the journal, the journal is searchable.

### A request, end to end

> "Linda, I have 30 minutes tonight. What should I practice?"

Whisper hears it, "Linda" is stripped, the local model returns `plan(minutes=30, topic=practice)`. `planner.py` scores every
song (status, priority, days since last practice, last playback check, your last self-rating) and the driving-theory quiz
(days to your test, last quiz, weakest question set), picks up to three items that fit 30 minutes and records *why*. Gemini
turns that into 3-4 spoken sentences. Speech is streamed sentence by sentence.

## Project structure

```
linda/
├── server.py               WEB MODE entry point: FastAPI + WebSocket, serves ui/
├── main.py                 classic terminal mode (local Whisper)
├── ui/                     index.html, style.css, app.js (speech recognition, orb, panels, board, galaxy)
├── config.yaml             your preferences (missing keys fall back to built-in defaults)
├── .env                    secrets (GEMINI_API_KEY, TELEGRAM_*)
├── requirements.txt        web mode
├── requirements-classic.txt   extra packages for main.py only
├── setup_data.py           creates data/ and starter files (never overwrites)
├── check_setup.py          checklist: what is missing, what to fill in
├── telegram_setup.py       finds your Telegram chat id
├── service.py              start at login / restart on crash
├── linda.db                the central database (created on first run)
├── data/
│   ├── songs.yaml          repertoire
│   ├── quizzes/            question banks (.txt: "1. / + / -" format)
│   ├── notes/ papers/ projects/   files for semantic search (.md .txt .pdf .docx)
│   └── journal/            daily entries (Linda writes here too)
├── tests/
│   ├── smoke.py            calendar, Telegram, files, chess (fakes)
│   ├── smoke_v3.py         study modules: planner, quiz, search, scheduler, audio analysis
│   └── test_server.py      web server end to end: security, fast path, tools, chess, quiz, interrupt
└── linda/
    ├── brain.py            web mode: session intercepts -> regex fast path -> one Gemini call with tools
    ├── tools.py            all 36 skills as Gemini function declarations (replaces the router)
    ├── fastpath.py         regex commands answered without any LLM
    ├── web.py              thread<->browser bridge: streaming TTS, ordered speech, latency timer
    ├── recorder.py         raw mic recording for music analysis
    ├── agent.py            executes intents; core skills, chess, Telegram, calendar, news
    ├── domains.py          practice, quiz, tasks, journal/search, review + scheduler jobs
    ├── router.py           small-model intent classifier (classic mode and offline fallback)
    ├── planner.py          explainable session planner
    ├── scheduler.py        daily jobs with grace windows
    ├── store.py            the database layer
    ├── docs.py             chunking, local embeddings, semantic search
    ├── quizbank.py         parse quiz files, weighted question picking
    ├── audio.py            mic capture, voice activity detection, recorder
    ├── audio_analysis.py   pitch (pyin) + onset/timing analysis, tuner
    ├── stt.py  tts.py      speech in / out
    ├── gemini.py           Gemini API wrapper (text, structured, audio transcription)
    ├── files.py            latest download, .docx tables with formatting hints, saving .txt
    ├── chess_game.py  board.py   chess logic; graphical board (separate process)
    ├── calendar_skill.py   macOS Calendar via EventKit
    ├── telegram.py         send to yourself
    ├── i18n.py i18n_study.py   fixed phrases in en / ru / uz
    └── system.py config.py util.py
```

## Install

Requirements: macOS on Apple Silicon, Python 3.9+ (3.12 recommended: Google libraries warn that 3.9 is end-of-life).

```bash
# 1. system tools
brew install ollama stockfish
brew install python-tk@3.12            # chess board window; match your Python version

# 2. local models
ollama pull gemma4:e2b-it-qat          # small router/offline model  (verify the tag in the Ollama library)
ollama pull nomic-embed-text           # embeddings for search

# 3. Python environment
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 4. data + secrets
python setup_data.py                   # creates data/ and .env
# edit .env, config.yaml, data/songs.yaml (see next section)
python telegram_setup.py               # after putting the bot token in .env and pressing Start on your bot

# 5. verify, then run
python check_setup.py                  # ✅ / ⚠️ / ❌ with the exact fix for each
python server.py                       # web mode: opens Chrome in app mode at http://127.0.0.1:4700
# python main.py                       # classic terminal mode (needs: pip install -r requirements-classic.txt)
```

On first run Chrome asks for the **microphone** (click Start on the page, then Allow) and macOS asks for **Calendars** access
(allow Terminal). Pin the tab or keep the app window open: Linda only hears you while the page is open. Use the 🎙 button in the
header to mute her.

## Files you fill in

| File | What to put in it |
|---|---|
| `.env` | `GEMINI_API_KEY` (aistudio.google.com/apikey), `TELEGRAM_BOT_TOKEN` (from @BotFather `/newbot`); `TELEGRAM_CHAT_ID` is written by `telegram_setup.py` |
| `config.yaml` | at least `user_name`, `default_language`, `goals.driving_test_date` (quoted `"YYYY-MM-DD"`), `quiz.language`, `city`, `birthdays.greeting_language`; optionally `audio.music_device` |
| `data/songs.yaml` | your repertoire (below) |
| `data/quizzes/*.txt` | question banks; the file name is the topic |
| `data/notes/`, `papers/`, `projects/` | anything you want to search later |

`data/songs.yaml`:

```yaml
songs:
  - {title: "Fur Elise", artist: "Beethoven", instrument: piano, status: learning, priority: 3,
     target_bpm: 100, melody: "E5 D#5 E5 D#5 E5 B4 D5 C5 A4"}
  - {title: "Blackbird", instrument: guitar, status: polishing, priority: 2}
```

`status`: learning / polishing / repertoire / archived. `priority`: 1 to 3. `melody` (single-note line) and `target_bpm` are
optional; they let Linda check your notes and tempo when she listens.

Quiz files use the format Linda itself writes (so "convert my latest download to a quiz" produces a ready bank):

```
1. Question text
+ Right answer
- Wrong answer
- Wrong answer
- Wrong answer
```

## Using Linda

Say her name, then the command, or say her name and speak within 20 seconds. After she answers you can keep talking without the
name for 20 seconds. **Press Enter in the terminal to interrupt her.** Examples (all work in ru/uz too):

| Say | What happens |
|---|---|
| "Linda, convert my latest download to a quiz" | Gemini rewrites it in the `+`/`-` format, saves `<topic>.txt` in Downloads, reveals it in Finder |
| "...and send it to my Telegram" | works with almost any command |
| "Good morning" | greeting, birthdays, today's calendar, weather, news |
| "News in Uzbek" / "What's the weather?" | grounded in Google Search |
| "Save Aziza's birthday, March 15, she's my mother, greeting in Uzbek" | stored; on the day Linda reminds you, but never messages automatically |
| "Write a birthday greeting for Aziza" | asks you to confirm before sending it to your Telegram |
| "Meeting with Ali tomorrow at 3 for an hour" / "What's on my calendar this week?" / "Undo that" | calendar |
| "I have 30 minutes. What should I practice?" | explainable plan |
| "I practiced Blackbird for 20 minutes, rate it 4" | practice log |
| "Listen to me play Fur Elise" | records 20 s, analyses pitch and timing, explains |
| "Tune my guitar" | records 3 s: "E, 17 cents flat, tune up" |
| "Start a quiz" | questions by voice; answer with the option number |
| "Log driving mock test, 38 of 40" / "How's my driving progress?" | results and stats |
| "Let's play chess" / "knight f3" / "hint" / "undo" / "stop chess" | chess with board window |
| "Add task book a driving lesson, tomorrow" / "What are my tasks?" / "Task done, driving lesson" | tasks |
| "Add to my journal: hands felt tired today" | writes to `data/journal/` and indexes it |
| "What did I write about fingering in Fur Elise?" | semantic search over your notes |
| "Summarize what I copied" | clipboard |

**Scheduler** (each job once a day, may still run if Linda starts late): birthdays 09:00, practice reminder 18:30 (only if
you haven't practiced), evening check-in 21:30 (she asks about your day; the answer becomes a journal entry, summary, tomorrow's
focus and new tasks), morning briefing 07:30 (off by default). Edit the `schedule:` list in `config.yaml`.

### Confirmations and action history

Linda runs read-only and reversible actions immediately. She asks for an exact confirmation before sending to Telegram,
uploading clipboard or download contents to Gemini, taking a screenshot, or changing the calendar. Deleting a birthday or
removing a saved memory note requires the stronger phrase `confirm delete` (Russian and Uzbek phrases are spoken in the
confirmation prompt). Confirmations expire after 90 seconds; say `cancel` to discard pending actions. Birthday reminders
never send Telegram messages on their own.

Action attempts, arguments, outcomes, and whether the user confirmed them are stored in the `audit_log` table in `linda.db`.
This includes text or file paths involved in an action, so treat the audit log as private data too.

## Run it always

```bash
python service.py login-item --awake    # recommended
python service.py install               # alternative: background LaunchAgent
python service.py status | logs | uninstall
```

- `login-item` creates `~/Linda.command` (a restart loop) and adds it to Login Items. It runs inside Terminal, so the microphone
  and calendar permissions you gave Terminal apply. Minimize the window.
- `install` runs Linda as a background LaunchAgent with auto-restart. macOS often does not show permission prompts for background
  Python: if she is deaf or has no calendar, use `login-item` instead.
- `--awake` prevents idle sleep while Linda runs (costs battery). Also start Ollama at login (Ollama app setting or
  `brew services start ollama`).

## Configuration

Every key has a built-in default (`linda/config.py`), so a partial `config.yaml` is fine. The most useful keys:

| Key | Meaning |
|---|---|
| `router: local \| gemini` | classic mode only: who classifies commands |
| `server.*` | `host`, `port` (4700), `open_browser`, `browser` ("Google Chrome"), `app_mode` (borderless app window) |
| `gemini.brain_model` | optional faster/cheaper model for the web-mode brain (e.g. a Flash-Lite), empty = `gemini.model` |
| `gemini.minimal_thinking` | ask for minimal reasoning to cut first-token latency; auto-disabled if the model rejects it |
| `brain.history_turns` | how many past exchanges the brain remembers |
| `local.model` | Ollama model for routing and offline chat |
| `gemini.model` | check the current name in Google AI Studio |
| `stt.refine_with_gemini` | re-transcribe only the command (after the wake word) with Gemini: much better Uzbek |
| `tts.engine: edge \| browser \| say` | edge = natural voices incl. Uzbek (online, synthesised by the server); browser = instant browser voices (no Uzbek); say = classic mode offline voices |
| `audio.input_device` / `audio.music_device` | choose mics by name part, e.g. `"MacBook Pro Microphone"` for playing |
| `goals.driving_test_date` | enables the countdown in the planner |
| `docs.answer_with: gemini \| local` | `local` keeps your notes on this Mac |
| `quiz.language` | language the questions are written in (for reading them aloud) |
| `schedule` | jobs: `id`, `at`, `action`, `grace_h`, optional `days`, `enabled` |
| `presets` | named instructions for "latest download" (the quiz preset lives here; add your own) |

## The database

`linda.db` (SQLite, one file; back it up by copying it): `songs`, `practice_logs`, `analyses`, `chess_games`, `quiz_answers`,
`driving_tests`, `tasks`, `reviews`, `birthdays`, `notes`, `kv`, `doc_files`, `chunks` (embeddings for search), and
`audit_log` (action history, including confirmation status).

## Privacy: what leaves your Mac

| Stays local | Goes to Gemini | Goes elsewhere |
|---|---|---|
| (classic mode) wake-word detection and transcription (Whisper), command routing (unless `router: gemini`), the database, embeddings, audio analysis, chess engine, quiz grading, scheduling, offline chat | the command audio *after* the wake word (if `refine_with_gemini`), questions and chat history, files you ask her to process, news/weather queries, text of calendar requests, evening-review text, note excerpts used to answer about your notes (unless `docs.answer_with: local`), notes you told her to "remember" (included in chat context), chess move text and positions when a spoken move is ambiguous | **Chrome speech recognition (web mode)**: all audio the page hears is streamed to Google's speech service, not only commands, because the wake word is detected in the returned text. Mute with 🎙, close the tab, or use classic mode if that is not acceptable. **Telegram**: only to your own chat id. **edge-tts**: each sentence she speaks is sent to Microsoft's online speech service (use `tts.engine: say` to avoid) |

If you use Gemini's free tier, read Google's current data-use terms before sending private documents.

## Limitations

- **Written and tested offline with fakes.** Logic is covered by `tests/`; the real microphone, Ollama, Gemini, EventKit, the
  chess window, edge-tts and launchd could not be exercised by the author. Expect small fixes on first run.
- **Model names change.** Verify `local.model` (Ollama) and `gemini.model` (AI Studio).
- **Audio analysis is for single-note lines** (scales, guitar melodies, a piano right-hand line, tuning). Chords and two-handed
  piano are flagged unreliable; timing still works. Notes are compared by name (the tracker's octave errors are ignored). Tempo
  assumes one note per beat. Use a good mic: Bluetooth/AirPods mics give poor results.
- **Uzbek** is the weakest language: local Whisper is mediocre (hence `refine_with_gemini`), and macOS has no Uzbek voice (edge-tts does, online).
- **Interrupting:** web mode lets you say "Linda" or "stop" while she talks (only those, because her own voice from the speakers can otherwise be mistaken for you); the optional voice barge-in checkbox needs headphones. Classic mode: press Enter.
- **Uzbek in Chrome:** the browser recogniser's Uzbek (`uz-UZ`) quality varies and is chosen per language in the header; test it. Classic mode with Gemini re-transcription is the alternative.
- **Routing:** ~35 intents is a lot for a 2-4B model; switch to `router: gemini` if it misroutes.
- **Driving module is theory-only** (quiz + manual result logging); practical lessons are not tracked.
- Lock screen sleeps the display (locks if "require password immediately" is on). `edge-tts` is an unofficial client.

## Testing and troubleshooting

```bash
python tests/smoke.py        # calendar, Telegram, file conversion, chess
python tests/smoke_v3.py     # planner, quiz, scheduler, semantic search, pitch/timing analysis
python tests/test_server.py  # web server: security, fast path, tool calls, chess, quiz, interrupt
python check_setup.py        # your machine: keys, models, folders, mic, optional tools
```

| Symptom | Try |
|---|---|
| `KeyError` for a config key | update to the current `config.py` (defaults fill gaps) |
| She doesn't react to her name | watch the terminal `[heard:...]` lines; add the spelling Whisper produced to `audio.wake_pattern` |
| Commands do the wrong thing | web mode: watch the terminal for `[tool ...]` errors and the `[latency]` line; classic mode: `router: gemini` |
| Page says "no speech recognition" | use Chrome (not Safari/Firefox) |
| Mic blocked | Chrome address bar lock icon -> Microphone -> Allow, reload |
| First reply is slow | set `gemini.brain_model` to a Flash-Lite model; check `[latency]` to see which stage is slow |
| No Uzbek voice | needs internet and `tts.engine: edge` |
| Calendar disabled | `pip install pyobjc-framework-EventKit`, allow Calendars in Privacy settings |
| No chess window | `brew install python-tk@<your python version>`; chess still prints in the terminal |
| Search finds nothing | `ollama pull nomic-embed-text`, add files to `data/`, restart |
| Analysis is unreliable | closer mic, one note at a time, set `audio.music_device`, avoid Bluetooth mics |

## Extending Linda

A new skill is four small steps:

1. Add the intent name to the `Literal` in `linda/router.py` (and any new fields to `Intent`).
2. Add a row to `TABLE` in `linda/tools.py` (name, description for the LLM, parameters). Classic mode also needs one line in `PROMPT` in `router.py`.
3. Write `do_<intent>(self, it, text)` in `agent.py` or a mixin in `domains.py`; use `self.k("phrase_key", ...)` to speak. Tools that write data go into `SEQUENTIAL` in `tools.py`.
4. Add the phrase to `i18n.py` / `i18n_study.py` in en, ru and uz. For a timed behaviour, write `job_<name>()` and add it to `schedule`.

Ideas not built yet: Groq Whisper as a speech-recognition fallback, weekly progress report on Telegram, a browsing screen for songs/practice/games, practical
driving-lesson log, a local wake-word model for "Linda" (so ambient audio need not go to Google).