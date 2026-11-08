"""Permission tiers and explicit confirmation phrases for Linda actions."""
import re


CONFIRMATIONS = {
    2: {"en": "confirm", "ru": "подтверждаю", "uz": "tasdiqlayman"},
    3: {"en": "confirm delete", "ru": "подтверждаю удаление",
        "uz": "o'chirishni tasdiqlayman"},
}
CANCELLATIONS = {
    "cancel", "stop", "stop it", "no", "отмена", "нет", "стоп", "хватит",
    "bekor", "bekor qil", "yo'q", "to'xta", "toxta",
}

READ_ONLY = {
    "time", "battery", "weather", "news", "morning", "list_birthdays",
    "calendar_agenda", "stats", "task_list", "search_notes", "read_note",
    "birthday_lookup", "web_answer", "plan", "chat",
}
SENSITIVE = {
    "calendar_add", "calendar_undo", "clipboard", "process_download", "delete_birthday", "remove_note",
}
DESTRUCTIVE = {"delete_birthday", "remove_note"}
REVERSIBLE = {
    "add_birthday", "birthday_greeting", "complete_goal", "fastpath",
    "journal_add", "log_driving", "mac", "music", "open_app", "practice_log",
    "remember", "save_note", "song_add", "task_add", "task_done", "timer",
    "tune", "analyze_playing", "chess", "quiz", "volume",
}
REQUEST_CUES = {
    "process_download": (
        re.compile(r"\b(convert|rewrite|summari[sz]e|extract|translate|rewrite|turn|make)\b|"
                   r"конвер|перепиш|суммир|извлек|перевед|"
                   r"o'gir|tarjima|qisqartir|xulosa", re.I),
        re.compile(r"download|downloads|file|document|pdf|скача|загруз|файл|документ|"
                   r"yuklab|fayl|hujjat", re.I),
    ),
    "morning": (
        re.compile(r"good morning|morning briefing|доброе утро|утренн\w* сводк|"
                   r"xayrli tong|ertalabki ma'lumot", re.I),
    ),
    "timer": (
        re.compile(r"\b(timer|remind|reminder|alarm|таймер|напомн|будильник|"
                   r"eslat|taymer|signal)\b", re.I),
        re.compile(r"\b(set|start|create|remind me|wake me|напомни|поставь|заведи|"
                   r"o'rnat|eslatib qo'y|uyg'ot)\b", re.I),
    ),
    "telegram_send": (
        re.compile(r"telegram|телеграм|телеграмм", re.I),
        re.compile(r"\b(send|forward|message|share)\b|отправ|перешл|yubor|jo'nat", re.I),
    ),
    "calendar_add": (
        re.compile(r"calendar|event|meeting|appointment|календар|событи|встреч|"
                   r"taqvim|uchrashuv|tadbir", re.I),
    ),
    "calendar_undo": (
        re.compile(r"undo|cancel|remove|delete|отмен|удал|bekor|o'chir|qaytar", re.I),
        re.compile(r"calendar|event|meeting|appointment|календар|событи|встреч|"
                   r"taqvim|uchrashuv|tadbir", re.I),
    ),
    "clipboard": (
        re.compile(r"clipboard|copied|copy|буфер|скопир|nusxa|ko'chir", re.I),
    ),
    "delete_birthday": (
        re.compile(r"birthday|день рождения|tug'ilgan kun", re.I),
        re.compile(r"delete|remove|forget|удал|забуд|o'chir|unut", re.I),
    ),
    "remove_note": (
        re.compile(r"note|memory|goal|idea|замет|памят|цель|иде|eslatma|xotira|maqsad", re.I),
        re.compile(r"delete|remove|forget|удал|забуд|o'chir|unut", re.I),
    ),
}


def permission_tier(tool, arguments=None):
    args = arguments or {}
    if tool in DESTRUCTIVE:
        return 3
    action = str(args.get("action") or "").lower()
    if tool in SENSITIVE or (tool == "mac" and action == "screenshot"):
        return 2
    if tool in READ_ONLY:
        return 0
    return 1 if tool in REVERSIBLE else 2


def request_is_supported(tool, text):
    """Require user-utterance evidence before weak routing can trigger an action."""
    cues = REQUEST_CUES.get(tool)
    return cues is None or all(pattern.search(text) for pattern in cues)


def normalize_confirmation(text):
    text = text.casefold().replace("’", "'")
    return re.sub(r"[^\w']+", " ", text, flags=re.UNICODE).strip()


def confirmation_phrase(tier, lang):
    options = CONFIRMATIONS[tier]
    return options.get(lang, options["en"])


def is_confirmation(text, tier, lang):
    return normalize_confirmation(text) == normalize_confirmation(confirmation_phrase(tier, lang))


def is_cancellation(text):
    return normalize_confirmation(text) in CANCELLATIONS
