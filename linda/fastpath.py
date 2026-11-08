"""Trivial commands answered in ~50 ms without any LLM. Anchored patterns only: 'what time is the
meeting' must NOT match, so it goes to the LLM instead."""
import re
from . import system as sysm
from .router import Intent

I = lambda **kw: Intent(**kw)
THANKS = {"en": "You're welcome.", "ru": "Пожалуйста.", "uz": "Marhamat."}
RULES = [
    (r"(thanks|thank you|thanks a lot|thank you linda|спасибо|спасибо большое|rahmat|katta rahmat)",
     lambda a, m, sl: a.say(THANKS[a.lang])),
    (r"(ok|okay|alright|cool|great|nice|amazing|good|хорошо|ладно|отлично|yaxshi|zo'?r)",
     lambda a, m, sl: None),                 # acknowledgement: stay silent
    (r"(what'?s the time|what time is it|what is the time|tell me the time|time|который час|сколько времени|"
     r"сколько сейчас времени|который сейчас час|soat necha|vaqt necha|hozir soat necha)( now| please| сейчас)?",
     lambda a, m, sl: a.do_time(I(intent="time"), "")),
    (r"(set )?(the )?(volume|громкость|ovoz)( to| на| ni)? (\d{1,3})( percent| процентов| foiz)?",
     lambda a, m, sl: a.do_volume(I(intent="volume", level=int(m.group(5))), "")),
    (r"(volume up|louder|turn it up|make it louder|громче|сделай громче|balandroq|ovozni oshir)",
     lambda a, m, sl: a.do_volume(I(intent="volume", level=min(100, sysm.get_volume() + 15)), "")),
    (r"(volume down|quieter|turn it down|make it quieter|тише|сделай тише|pastroq|ovozni kamaytir)",
     lambda a, m, sl: a.do_volume(I(intent="volume", level=max(0, sysm.get_volume() - 15)), "")),
    (r"(unmute|включи звук|ovozni yoq|ovozni yoqing)",
     lambda a, m, sl: a.do_mac(I(intent="mac", action="unmute"), "")),
    (r"(mute|mute sound|без звука|выключи звук|ovozni o'?chir)",
     lambda a, m, sl: a.do_mac(I(intent="mac", action="mute"), "")),
    (r"(pause|pause music|пауза|поставь на паузу|музыку на паузу|pauza|musiqani to'?xtat)",
     lambda a, m, sl: a.do_music(I(intent="music", action="pause"), "")),
    (r"(play|play music|resume|resume music|включи музыку|продолжи|musiqani yoq|davom et)",
     lambda a, m, sl: a.do_music(I(intent="music", action="play"), "")),
    (r"(next|next song|next track|skip|следующая|следующий трек|включи следующую|keyingi|keyingi qo'?shiq)",
     lambda a, m, sl: a.do_music(I(intent="music", action="next"), "")),
    (r"(previous|previous song|previous track|предыдущая|предыдущий трек|oldingi|oldingi qo'?shiq)",
     lambda a, m, sl: a.do_music(I(intent="music", action="previous"), "")),
    (r"(speak english|switch to english|talk in english|говори по английски|ingliz tilida gapir)",
     lambda a, m, sl: sl("en")),
    (r"(speak russian|switch to russian|talk in russian|говори по русски|переключись на русский|ruscha gapir)",
     lambda a, m, sl: sl("ru")),
    (r"(speak uzbek|switch to uzbek|talk in uzbek|говори по узбекски|o'?zbekcha gapir|o'?zbek tilida gapir)",
     lambda a, m, sl: sl("uz")),
]
COMPILED = [(re.compile(f"^{p}$"), fn) for p, fn in RULES]




def normalize(text):
    t = text.lower().replace("’", "'").replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^\w' ]+", " ", t)).strip()


def try_fast(agent, text, set_lang=lambda c: None):
    t = normalize(text)
    if len(t.split()) > 7:
        return False
    for rx, fn in COMPILED:
        m = rx.match(t)
        if m:
            agent.run_action("fastpath", {"command": text},
                             lambda: fn(agent, m, set_lang))
            return True
    return False