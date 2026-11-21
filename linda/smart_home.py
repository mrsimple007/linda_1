"""Smart home through Yandex «Дом с Алисой» (the user API at api.iot.yandex.net).
Token: an OAuth token with iot:view + iot:control, in .env as YANDEX_IOT_TOKEN (never in config or code).
Simple commands ('выключи свет', 'turn on the lamp', 'яркость 30', 'тёплый свет') are parsed here without any LLM:
instant, and they cost no AI quota. The smart_home tool covers everything else."""
import difflib, os, re, threading, time

import requests

API = "https://api.iot.yandex.net/v1.0"
LIGHT_WORDS = r"свет\w*|ламп\w*|люстр\w*|светильник\w*|torch|lights?|lamps?|chiroq\w*"
ON = re.compile(r"\b(включи\w*|зажги|вруби|turn on|switch on|yoq\w*)\b", re.I)
OFF = re.compile(r"\b(выключи\w*|погаси\w*|выруби|turn off|switch off|o'?chir\w*)\b", re.I)
BRIGHT = re.compile(r"(?:яркост\w*|brightness|yorqin\w*)\D{0,12}(\d{1,3})|(\d{1,3})\s*(?:%|процент\w*|percent)", re.I)
WARM = re.compile(r"тёпл\w*|тепл\w*|\bwarm\b|\biliq\b", re.I)
COLD = re.compile(r"холодн\w*|\bбел\w*|\bcold\b|\bwhite\b|\boq\b", re.I)
COLORS = {                                                     # word -> RGB as one integer (Yandex 'rgb' instance)
    "красн": 0xFF0000, "red": 0xFF0000, "qizil": 0xFF0000,
    "зелён": 0x00FF00, "зелен": 0x00FF00, "green": 0x00FF00, "yashil": 0x00FF00,
    "син": 0x0000FF, "blue": 0x0000FF, "ko'k": 0x0000FF,
    "жёлт": 0xFFFF00, "желт": 0xFFFF00, "yellow": 0xFFFF00, "sariq": 0xFFFF00,
    "фиолет": 0x8000FF, "purple": 0x8000FF, "розов": 0xFF69B4, "pink": 0xFF69B4,
    "оранж": 0xFF8000, "orange": 0xFF8000,
}
SAID = {
    "on": {"ru": "Включила.", "en": "Turned on.", "uz": "Yoqdim."},
    "off": {"ru": "Выключила.", "en": "Turned off.", "uz": "O'chirdim."},
    "set": {"ru": "Готово.", "en": "Done.", "uz": "Tayyor."},
    "none": {"ru": "Не нашла такое устройство в умном доме.", "en": "I couldn't find that device in your smart home.",
             "uz": "Bunday qurilmani topmadim."},
    "token": {"ru": "Умный дом не подключён: нужен YANDEX_IOT_TOKEN в .env.",
              "en": "Smart home isn't connected: YANDEX_IOT_TOKEN is missing in .env.",
              "uz": "Aqlli uy ulanmagan: .env da YANDEX_IOT_TOKEN kerak."},
    "fail": {"ru": "Умный дом не ответил. Попробуй ещё раз.", "en": "The smart home didn't respond. Try again.",
             "uz": "Aqlli uy javob bermadi."},
}


class SmartHomeError(Exception):
    pass


class Home:
    def __init__(self, token_env="YANDEX_IOT_TOKEN"):
        self.token_env, self._cache, self._at, self._lock = token_env, None, 0.0, threading.Lock()

    @property
    def ready(self):
        return bool(os.getenv(self.token_env))

    def _req(self, method, path, **kw):
        tok = os.getenv(self.token_env)
        if not tok:
            raise SmartHomeError("token")
        r = requests.request(method, API + path, headers={"Authorization": f"Bearer {tok}"}, timeout=4, **kw)
        if r.status_code >= 400:
            raise SmartHomeError(f"HTTP {r.status_code}")          # never echo the response: it may hold ids/tokens
        return r.json()

    def devices(self, fresh=False):
        """[{id, name, type, room, caps}] - cached 5 minutes."""
        with self._lock:
            if self._cache is None or fresh or time.time() - self._at > 300:
                info = self._req("GET", "/user/info")
                rooms = {r["id"]: r.get("name", "") for r in info.get("rooms", [])}
                self._cache = [{"id": d["id"], "name": d.get("name", ""), "type": d.get("type", ""),
                                "room": rooms.get(d.get("room"), ""),
                                "caps": {c.get("type", "").rsplit(".", 1)[-1] for c in d.get("capabilities", [])}}
                               for d in info.get("devices", [])]
                self._at = time.time()
            return self._cache

    def find(self, text):
        """Devices the text names (name or room); every light if it only says 'свет' / 'lamp'."""
        low = text.lower()
        devs = self.devices()
        named = [d for d in devs if d["name"] and d["name"].lower() in low]
        if not named:
            words = re.findall(r"\w{4,}", low)
            named = [d for d in devs if any(difflib.SequenceMatcher(None, w, x).ratio() > 0.8
                                            for w in words for x in re.findall(r"\w{4,}", d["name"].lower()))]
        rooms = [d for d in devs if d["room"] and d["room"].lower()[:5] in low]
        lights = [d for d in devs if d["type"].endswith("light")]
        if named:
            return named
        if rooms:
            return [d for d in rooms if d["type"].endswith("light")] or rooms
        return lights if re.search(LIGHT_WORDS, low) else []

    def act(self, devs, action, value=None):
        if action in ("on", "off"):
            cap = {"type": "devices.capabilities.on_off", "state": {"instance": "on", "value": action == "on"}}
        elif action == "brightness":
            cap = {"type": "devices.capabilities.range",
                   "state": {"instance": "brightness", "value": max(1, min(100, int(value)))}}
        elif action == "temperature":
            cap = {"type": "devices.capabilities.color_setting", "state": {"instance": "temperature_k", "value": int(value)}}
        elif action == "color":
            cap = {"type": "devices.capabilities.color_setting", "state": {"instance": "rgb", "value": int(value)}}
        else:
            raise SmartHomeError(f"unknown action {action}")
        need = cap["type"].rsplit(".", 1)[-1]
        targets = [d for d in devs if need in d["caps"]] or devs
        res = self._req("POST", "/devices/actions",
                        json={"devices": [{"id": d["id"], "actions": [cap]} for d in targets]})
        bad = [c for d in res.get("devices", []) for c in d.get("capabilities", [])
               if (c.get("state", {}).get("action_result") or {}).get("status") == "ERROR"]
        if bad:
            raise SmartHomeError("device refused")
        return targets


HOME = Home()


def parse(text):
    """-> (action, value) for a plain lamp command, or None. Needs a light word or a known device name."""
    t = text.lower()
    m = BRIGHT.search(t)
    if m and (re.search(LIGHT_WORDS, t) or re.search(r"яркост|brightness|yorqin", t)):
        return "brightness", int(m.group(1) or m.group(2))
    color = next((v for k, v in COLORS.items() if k in t), None)
    if color is not None and re.search(LIGHT_WORDS, t):
        return "color", color
    if re.search(LIGHT_WORDS, t) and WARM.search(t):
        return "temperature", 2700
    if re.search(LIGHT_WORDS, t) and COLD.search(t) and not OFF.search(t):
        return "temperature", 6500
    if OFF.search(t):
        return "off", None
    if ON.search(t):
        return "on", None
    return None


def _lang(agent):
    return getattr(agent, "lang", "ru") if getattr(agent, "lang", "ru") in ("ru", "en", "uz") else "ru"


def run(agent, text, action=None, value=None, device=""):
    """Do it and return the sentence to say."""
    L = _lang(agent)
    if not HOME.ready:
        return SAID["token"][L]
    try:
        devs = HOME.find(device or text)
        if not devs and device:
            devs = HOME.find(text)
        if not devs and action in ("brightness", "temperature", "color"):     # only lights have these
            devs = [d for d in HOME.devices() if d["type"].endswith("light")]
        if not devs:
            return SAID["none"][L]
        HOME.act(devs, action, value)
        return SAID["on" if action == "on" else "off" if action == "off" else "set"][L]
    except SmartHomeError as e:
        print("[smart home]", str(e)[:80])
        return SAID["token" if str(e) == "token" else "fail"][L]
    except requests.RequestException as e:
        print("[smart home]", type(e).__name__)
        return SAID["fail"][L]


def quick(agent, text):
    """Plain lamp commands without the LLM. Returns the reply, or None when this is not one."""
    if len(text.split()) > 10:
        return None
    p = parse(text)
    if p is None:
        return None
    if p[0] in ("on", "off") and not re.search(LIGHT_WORDS, text, re.I):
        try:                                                # 'выключи телевизор': only if it names a device we have
            if not (HOME.ready and any(d["name"] and d["name"].lower() in text.lower() for d in HOME.devices())):
                return None
        except Exception:
            return None
    return run(agent, text, *p)


def tool(agent, a, text):
    """smart_home tool: action on/off/brightness/warm/cold/color/list, device = name or room (empty = all lights)."""
    action = str(a.get("action") or "").lower()
    device = str(a.get("device") or "")
    value = a.get("value")
    if action == "list":
        if not HOME.ready:
            return agent.say(SAID["token"][_lang(agent)])
        try:
            devs = HOME.devices(fresh=True)
        except Exception as e:
            print("[smart home]", str(e)[:80])
            return agent.say(SAID["fail"][_lang(agent)])
        return agent.say("; ".join(f"{d['name']}" + (f" ({d['room']})" if d["room"] else "") for d in devs) or "—")
    if action in ("warm", "cold"):
        action, value = "temperature", 2700 if action == "warm" else 6500
    if action == "color":
        value = next((v for k, v in COLORS.items() if k in str(value).lower()), 0xFFFFFF)
    if action not in ("on", "off", "brightness", "temperature", "color"):
        p = parse(text)
        if not p:
            return agent.say({"ru": "Что сделать со светом?", "en": "What should I do with the light?",
                              "uz": "Chiroq bilan nima qilay?"}[_lang(agent)])
        action, value = p
    agent.say(run(agent, text, action, value, device))
