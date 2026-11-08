# Linda Project Overview

Linda is a local-first, hybrid voice assistant for macOS designed to act like a personal AI helper for one user. She can understand spoken commands in English, Russian, or Uzbek, carry out tasks on the Mac, and keep relevant personal data in a single local SQLite database.

The project combines:

- local voice processing and command routing
- browser-based interface in web mode
- Gemini-powered reasoning for open-ended requests and external knowledge
- Python automation for system actions, reminders, calendar, notes, and task workflows
- local-first storage so the assistant can function without depending on a remote service for core tasks

## Main idea

Linda is built around a "JARVIS for one person" concept: the assistant listens for commands, decides whether the request should be handled locally or with Gemini, executes the action, and speaks back with a natural voice.

The app supports two modes:

- Web mode (default): browser-based experience with speech recognition and UI panels
- Classic mode: terminal-based, local Whisper-driven version for older or lower-setup workflows

## Key capabilities

- quick system actions: time, volume, mute, lock screen, screenshot, music playback, timers, reminders
- app launching and clipboard support
- calendar management and reminders
- birthdays and personalized greeting workflows
- Telegram messaging and file delivery
- chess play and saved games
- music practice tracking and analysis
- quizzes and learning flows
- task tracking, journaling, notes, and personal knowledge search
- news, weather, and general Q&A with Gemini or local fallback logic

## How it works

Linda uses a hybrid processing model:

1. The speech layer listens for the wake word and captures audio.
2. A local model or lightweight router decides what kind of request it is.
3. Fast, deterministic actions run locally in Python when possible.
4. Complex or open-ended tasks are routed to Gemini.
5. Results are spoken back through the voice pipeline.
6. Important state is stored in a local SQLite database so the assistant can recall personal context over time.

This makes the system faster for simple commands while still enabling richer reasoning when needed.

## Project structure

```text
linda/
├── main.py                 # classic terminal UI / local voice assistant
├── server.py              # web app entry point
├── app_window.py          # app/window integration helpers
├── check_setup.py         # setup validation script
├── config.yaml            # project configuration
├── .env                   # environment secrets (not committed)
├── .env.example           # sample environment file
├── requirements.txt       # Python dependencies for web mode
├── linda.db               # local SQLite database
├── ui/                    # browser frontend files
├── tests/                 # test suite
├── README.md              # detailed project documentation
└── ...
```

## Configuration and environment

The project expects environment variables and config values such as:

- Gemini API key
- Telegram credentials
- local app preferences
- database and data paths

A sample environment file is provided in `.env.example`, and runtime settings are loaded from `config.yaml`.

## Running the project

### Web mode

```bash
python server.py
```

Then open the local UI in a browser at:

```text
http://127.0.0.1:4700
```

### Classic mode

```bash
python main.py
```

## Privacy and local-first design

Linda is designed to keep personal data local. Core memory, task history, notes, and preferences are stored in the local database instead of being spread across multiple services.

However, the browser-based web mode uses Chrome speech recognition, which sends recognized audio to Google's speech service. The project documentation also notes that Gemini access may require internet connectivity for certain features.

## Use cases

This project is oriented toward personal productivity and daily assistance, including:

- personal reminders and scheduling
- voice-driven task and note management
- contextual memory over time
- embedded learning workflows like quizzes and practice planning
- automation for local macOS workflows

## Summary

Linda is a personal Mac voice assistant that combines local automation, browser UI, and Gemini-powered intelligence. It is designed to be useful every day without forcing the user to depend on a large cloud-only assistant for regular tasks.


# Linda: Gaps, Improvements and Roadmap

Review of the current architecture against a "JARVIS for one person" local-first design.
Linda already has strong features (voice, router, tools, SQLite memory, 3 languages). The gaps are mostly **infrastructure, safety and robustness**, not features.

---

### 1.2 Wake word is not always-on
A browser tab cannot reliably listen all day.

**Fix**
- Run a background daemon (`openWakeWord` or Porcupine) started via `launchd`.
- The daemon runs independently of the UI and wakes the server when triggered.
- Add VAD (Silero) to detect start and end of speech.

### 1.3 Router is underspecified
"A local model or lightweight router decides" needs a concrete design.

**Fix**
- Define a fixed intent/tool schema (JSON function calling).
- Add a confidence threshold with clear rules:
  - high confidence: run locally
  - medium: ask the user to confirm
  - low: send to Gemini
- Log every routing decision (input, chosen route, confidence, result, latency) to a `router_log` table.
- Review misroutes weekly and turn them into test cases.

### 1.4 No safety and permission layer
Linda can lock the screen, message people on Telegram and send files.

**Fix**
- **Permission tiers**
  | Tier | Examples | Behavior |
  |---|---|---|
  | 0 Read-only | time, weather, notes search | run immediately |
  | 1 Reversible | volume, timer, open app | run, announce |
  | 2 Sensitive | Telegram send, send file, calendar edit | ask for confirmation |
  | 3 Destructive | delete files, shell commands | confirmation plus explicit phrase |
- **Audit log** table: timestamp, tool, arguments, result, confirmed_by.
- **Prompt-injection protection**: text from news, web pages or incoming messages is *data*, never instructions. Tool calls must come only from the user's own utterance, not from fetched content.
- Never run arbitrary shell commands from LLM output without a sandbox and confirmation.

### 1.5 Memory is storage, not recall
SQLite stores data, but there is no semantic retrieval.

**Fix**
- Add embeddings with `sqlite-vec` (keeps everything in `linda.db`) and a local embedding model.
- Retrieve top-k relevant memories before each Gemini/LLM call.
- Summarize old conversations into long-term facts.
- Add a "memory manager" UI: view, edit, delete, export what Linda remembers.
- Voice commands: "forget that", "what do you know about X".

---

## 2. High-value improvements

### 2.1 Voice quality
- Barge-in: interrupt Linda while she is speaking.
- Echo cancellation so she does not hear her own voice.
- Sentence-level TTS streaming (speak the first sentence while the rest generates).
- Local TTS option (Piper / Kokoro) for offline use.
- Target: under 2 s from end of speech to start of reply for local commands.

### 2.2 Uzbek support
- Whisper is weak on Uzbek. Options: a dedicated Uzbek STT model, fine-tuning, or cloud STT only for Uzbek.
- Per-utterance language detection to handle code-switching (Uzbek/Russian/English in one sentence).
- Test TTS voice quality per language separately.

### 2.3 Proactivity
- A scheduler / event loop so Linda speaks first:
  - morning briefing (weather, calendar, tasks, birthdays)
  - upcoming-event warnings
  - reminders even when the browser UI is closed
- "Do not disturb" and quiet hours settings.

### 2.4 Context awareness
- Know the active app, selected text and clipboard.
- Optional screen understanding with a local vision model (Qwen-VL / LLaVA).
- Commands like "summarize this page" or "explain what's on my screen".

### 2.5 Data protection
- `linda.db` contains very personal data:
  - encrypt it (SQLCipher) or at minimum rely on FileVault
  - automatic encrypted backups with rotation
  - move secrets from `.env` to macOS Keychain
  - redact sensitive data before sending anything to Gemini
  - document exactly what leaves the machine

### 2.6 Reliability
- Timeouts, retries and circuit breakers for Gemini and Telegram.
- Graceful degradation: when offline, say so and handle what can be done locally.
- Health endpoint and an extended `check_setup.py` (mic permission, models present, DB integrity, API reachability).
- Structured logging with rotation.

---

## 3. Quality and engineering

### 3.1 Evaluation
- Build a test set of commands in English, Russian and Uzbek with expected intent and tool.
- Measure routing accuracy, STT word error rate and end-to-end latency.
- Run it in CI or before each release.
- Keep a regression suite from real misroutes.

### 3.2 Extensibility
- Plugin or MCP-style tool interface: each skill is a module with a schema, permission tier and handler.
- New skills should not require editing core files.
- Per-skill enable/disable in `config.yaml`.

### 3.3 macOS integration
- Menu-bar app (rumps / Tauri) with status: listening, thinking, muted, offline.
- Proper handling of Microphone, Accessibility, Automation and Screen Recording permissions, with guided setup.
- Global hotkey for push-to-talk as a wake-word alternative.
- Auto-start via `launchd`.

### 3.4 UX
- Visible state in the UI: wake detected, transcript, chosen route, tool being run.
- "Why did you do that?" explanation from the audit log.
- Text input fallback and conversation history view.
- Mute indicator and a hardware-style kill switch.

---

## 4. Nice-to-have later
- Email integration (read, summarize, draft; send requires confirmation).
- Home Assistant / smart home control.
- Multi-step agent plans ("book it, add to calendar, remind me").
- Speaker verification so only your voice triggers sensitive actions.
- Daily and weekly self-review (journaling insights, music practice trends).
- Mobile companion (Telegram already covers part of this).

---

## 5. Suggested build order

| # | Task | Why first |
|---|---|---|
| 1 | Safety layer: permission tiers, confirmations, audit log | cheap, prevents real damage |
| 2 | Local STT (faster-whisper) replacing Chrome STT | true privacy |
| 3 | Router schema, thresholds and logging | foundation for everything |
| 4 | Background wake-word daemon | real always-on behavior |
| 5 | Semantic memory (sqlite-vec) | recall and personalization |
| 6 | Ollama fallback | works offline |
| 7 | Evaluation suite (3 languages) | measure progress, prevent regressions |
| 8 | Proactive scheduler | feels like JARVIS |
| 9 | DB encryption, Keychain, backups | protect personal data |
| 10 | Menu-bar app and plugin interface | polish and growth |

---

## 6. Target architecture

```
 Mic
  │
  ▼
[Wake word daemon] ─► [VAD] ─► [Local STT: faster-whisper]
                                      │
                                      ▼
                          ┌────────────────────────┐
                          │ Router (schema + conf.)│──► router_log
                          └────────────────────────┘
                            │          │          │
                  local tools     Ollama (local)   Gemini (cloud, optional)
                            │          │          │
                            └────┬─────┴──────────┘
                                 ▼
                    [Permission layer] ─► audit_log
                                 │
                     [Tools / Plugins: Mac, Telegram, Calendar...]
                                 │
              [Memory: SQLite + sqlite-vec, encrypted]
                                 │
                       [TTS: Piper / Kokoro / cloud]
                                 ▼
                              Speaker
```

---

## 7. Definition of done for "truly local"
- [ ] Core commands work with the internet disconnected
- [ ] No audio leaves the machine unless explicitly enabled
- [ ] Every sensitive action is confirmed and logged
- [ ] Wake word works with the UI closed
- [ ] Memory is searchable, editable and encrypted
- [ ] Routing accuracy is measured per language