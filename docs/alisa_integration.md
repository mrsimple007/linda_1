# Linda + Yandex Station Alisa: integration plan

Goal: Alisa is the ears and mouth (STT/TTS). Linda is the brain (md files, history, tools). Smart home and scenarios run through the Yandex IoT API.

> Written from memory of the Yandex platform. Check current rules and field names at https://dialogs.yandex.ru/developer and https://yandex.ru/dev/dialogs/smart-home before building. Items marked **(verify)** are the ones I am least sure about.

---

## 1. How it works

```
You: "Алиса, запусти навык Линда"
Station STT (Yandex)  -> text -> POST https://your-domain/alice/<SECRET>
                                          |
                                   Linda: fastpath -> brain -> tools -> md files
                                          |
Station TTS (Yandex)  <- {"response": {"text": "..."}}
```

- Alisa turns your speech into text and sends it to your webhook (a Yandex Dialogs "skill").
- Linda answers in JSON, and Alisa reads the text aloud.
- Linda controls the lamp and scenarios by calling the Yandex Smart Home (IoT) API.
- Telegram and voice share the same brain, files and history.

You cannot take over the Station's raw microphone or speaker. Only text goes in and out.

## 2. Limits to know first

1. **3-second limit.** Alisa waits about 3 seconds for a reply. The Groq agent loop is often slower. Use a fast path for simple commands and a timeout fallback ("answer will come to Telegram").
2. **Session mode.** After "запусти навык Линда", everything you say goes to Linda until you say "хватит". "Включи лампу" inside the skill reaches Linda, not Alisa's own smart home, so Linda needs her own lamp tool.
3. **Language.** Alisa reads Russian well, English and Uzbek poorly. Russian replies work best.
4. **Public HTTPS URL required.** Your Mac must be on. Use a Cloudflare Tunnel or ngrok.
5. **Security.** The webhook is public and Linda has destructive tools. Use a secret in the URL and accept only your own `user_id`.
6. **Length.** Keep replies short (limit about 1000 characters).
7. **Proactive speech.** The Station cannot talk first through a skill. For a morning briefing, use an Alisa scenario that speaks a text, triggered through the IoT API **(verify)**.

## 3. Steps

### Step 1. Public URL (tunnel)

Cloudflare Tunnel example:

```bash
brew install cloudflared
cloudflared tunnel --url http://localhost:<PORT_OF_YOUR_SERVER>
```

This prints a temporary `https://....trycloudflare.com` URL. For a permanent URL, create a named tunnel with your own domain.

### Step 2. Create the skill

1. Open https://dialogs.yandex.ru/developer and create a skill of type "Skill in Alisa".
2. Webhook URL: `https://<your-domain>/alice/<SECRET>`
3. Activation name: "Линда" **(verify that the name is accepted)**.
4. Leave it unpublished. A draft works on devices signed in to your own Yandex account.

Generate the secret and put it in `.env`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(24))"
```

```
ALICE_SECRET=<generated string>
ALICE_OWNER_ID=<filled in step 4>
```

### Step 3. Webhook code: `linda/alice.py`

```python
import concurrent.futures, threading
from . import fastpath

POOL = concurrent.futures.ThreadPoolExecutor(max_workers=2)
ALICE_RULES = ("Channel: voice through a Yandex Station. Reply in 1-2 short sentences, no markdown or emoji. "
               "Use smart_home for lamps and devices.")


def _think(agent, text):
    from .telegram_linda.briefing import muted
    brain = agent.brain
    with muted(agent, text) as mute:
        if fastpath.try_fast(agent, text, lambda c: None):
            return " ".join(mute.lines)
        brain.cancel = threading.Event()
        brain.extra_rules = ALICE_RULES
        try:
            brain._agent_loop(text)
        finally:
            brain.extra_rules = ""
    return " ".join(mute.lines)


def handle(agent, payload, owner_id):
    sess = payload.get("session", {})
    uid = (sess.get("user") or {}).get("user_id") or sess.get("user_id")
    base = {"version": payload.get("version", "1.0")}
    req = payload.get("request", {})
    if req.get("type") == "SimpleUtterance" and uid != owner_id:
        return {**base, "response": {"text": "Нет доступа.", "end_session": True}}
    text = (req.get("original_utterance") or "").strip()
    if sess.get("new") or not text:
        return {**base, "response": {"text": "Линда на связи. Слушаю.", "end_session": False}}
    if text.lower() in ("хватит", "выход", "стоп"):
        return {**base, "response": {"text": "Пока!", "end_session": True}}
    fut = POOL.submit(_think, agent, text)
    try:
        answer = fut.result(timeout=2.5)
    except concurrent.futures.TimeoutError:
        def later(f):
            try:
                agent.tg.send(f.result())
            except Exception as e:
                print("[alice late]", repr(e)[:100])
        fut.add_done_callback(later)
        answer = "Думаю долго, ответ пришлю в Телеграм."
    return {**base, "response": {"text": answer[:1000] or "Готово.", "end_session": False}}
```

Notes:
- Skill pings (`request.type == "ping"`) go through the same function. Make sure it returns a valid JSON with `version` and `response` for them as well.
- `_think` uses `muted()` so nothing is spoken on the Mac.
- `brain.tool_log` and `brain.history` are shared with Telegram, which is the point.

### Step 4. Route in your web server

`server.py` framework is unknown to me. FastAPI version:

```python
import os
from fastapi import HTTPException
from linda import alice as alice_mod

@app.post("/alice/{secret}")
async def alice(secret: str, payload: dict):
    if secret != os.getenv("ALICE_SECRET"):
        raise HTTPException(404)
    return alice_mod.handle(agent, payload, os.getenv("ALICE_OWNER_ID"))
```

For Flask, use `@app.post("/alice/<secret>")` and `request.get_json()`.

Find your `user_id`: on the first call, add `print("[alice uid]", uid)` in `handle`, say anything to the skill, copy the value into `ALICE_OWNER_ID`, remove the print, restart.

### Step 5. Smart home access

1. Create an app at https://oauth.yandex.ru with the scopes `iot:view` and `iot:control` **(verify exact scope names)**.
2. Get an OAuth token for your account and put it in `.env`:

```
YANDEX_IOT_TOKEN=<token>
```

3. List your devices once and note the ids:

```bash
curl -H "Authorization: Bearer $YANDEX_IOT_TOKEN" https://api.iot.yandex.net/v1.0/user/info
```

4. Add the ids to `config.yaml`:

```yaml
alice_devices:
  lamp: "<device id of the smart lamp>"
```

### Step 6. Smart-home tool

In `linda/tools.py` (add `import os, requests` at the top):

```python
def _smart_home(agent, a, text):
    name, on = str(a.get("device") or "").lower(), a.get("on")
    devs = agent.cfg.get("alice_devices", {})
    did = next((v for k, v in devs.items() if k in name or name in k), None)
    if not did:
        return agent.say("error: unknown device. Known: " + ", ".join(devs))
    acts = [{"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": bool(on)}}]
    if a.get("brightness") is not None:
        acts.append({"type": "devices.capabilities.range",
                     "state": {"instance": "brightness", "value": int(a["brightness"])}})
    r = requests.post("https://api.iot.yandex.net/v1.0/devices/actions",
                      headers={"Authorization": "Bearer " + os.environ["YANDEX_IOT_TOKEN"]},
                      json={"devices": [{"id": did, "actions": acts}]}, timeout=5)
    agent.say("Готово." if r.ok else f"error: {r.status_code}")


SPECIAL["smart_home"] = _smart_home
```

Add to `TABLE`:

```python
    ("smart_home", "Turn a smart-home device on or off, or set brightness (lamp, light).",
     {"device": (S, "device name, e.g. lamp"), "on": (B, "true to turn on, false to turn off"),
      "brightness": (I, "0-100 if said")}, ["device", "on"], {}),
```

Register it in four places:

| File | Change |
|---|---|
| `linda/providers.py` | add `"smart_home"` to `CORE` |
| `linda/providers.py` | add to `EXTRA`: `(r"lamp\|light\|лампа\|свет\|chiroq", {"smart_home"})` |
| `linda/safety.py` | add `"smart_home"` to `REVERSIBLE` |
| `linda/tools.py` | add `"smart_home"` to `SEQUENTIAL` |

**Scenarios.** Alisa scenarios (for example "movie evening") can be run with `POST /v1.0/scenarios/{id}/actions` **(verify)**. Make a second tool `run_scenario` the same way, with a name-to-id map in `config.yaml`.

### Step 7. Test, in this order

1. Local check, no Alisa:
   ```bash
   curl -X POST http://localhost:<PORT>/alice/<SECRET> -H "Content-Type: application/json" \
     -d '{"version":"1.0","session":{"new":false,"user":{"user_id":"test"}},"request":{"type":"SimpleUtterance","original_utterance":"сколько времени"}}'
   ```
   (It will say "Нет доступа" until `ALICE_OWNER_ID` is set to the id you send. Test with `"user_id":"<your id>"`.)
2. In the Dialogs console, use the built-in "Testing" tab.
3. On the Station: "Алиса, запусти навык Линда", then "который час".
4. Lamp: "включи лампу".
5. Memory: "что у меня сегодня по задачам", then check that a task added in Telegram is visible.

## 4. Speed tuning (do this after it works)

- Fast path first. Put time, tasks list, lamp on/off, and timers in `fastpath` so they never reach the LLM.
- For voice, offer a short tool list and a lower step count. `_agent_loop` uses `max_steps = 8` when `extra_rules` is set (Telegram mode). Voice should use 4, so add a separate flag instead of reusing `extra_rules`.
- Keep prompts small. Each step is about 5K tokens, and 429 errors on Groq are the main cause of slow replies.
- Add more providers in `config.yaml` so a 429 moves to the next model without waiting (see below).

## 5. Provider chain (from the previous work)

Models that exist for your Groq key and support tools: `openai/gpt-oss-120b`, `openai/gpt-oss-20b`, `qwen/qwen3.8-27b`, `openai/gpt-oss-safeguard-20b`.

Gemini keys: quota is per Google Cloud project, not per key, so extra keys only help if each one comes from a separate project. Check Google's terms first. Add them as OpenAI-compatible providers:

```yaml
  - {name: gemini-1, base_url: "https://generativelanguage.googleapis.com/v1beta/openai/", key_env: GEMINI_KEY_1, model: gemini-2.5-flash}
  - {name: gemini-2, base_url: "https://generativelanguage.googleapis.com/v1beta/openai/", key_env: GEMINI_KEY_2, model: gemini-2.5-flash}
```

Check the current model name in AI Studio.

## 6. Security checklist

- [ ] Secret in the webhook path, and `ALICE_OWNER_ID` check.
- [ ] Destructive tools (`remove_note`, `delete_birthday`) still require "confirm delete". By voice this needs a second utterance in the same session, so test it.
- [ ] Do not read finance data by voice. Finance reports are behind a PIN in Telegram and should stay there.
- [ ] Keep `.env` out of git.

## 7. Known risks

- Yandex may change skill rules, scopes or limits. Re-check the docs before starting.
- If the tunnel URL changes, update the webhook in the Dialogs console.
- Reasoning models can exceed the 3-second limit even on the first call. Rely on the timeout fallback.

## 8. Suggested order of work

1. Tunnel + webhook + route, test "который час".
2. Lamp tool.
3. Scenarios tool.
4. Speed tuning and voice-specific step limit.
5. Morning briefing through an Alisa scenario **(verify)**.