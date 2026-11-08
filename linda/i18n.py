"""Fixed phrases in English / Russian / Uzbek (Latin). Open-ended text comes from Gemini."""
LANGS = ("en", "ru", "uz")
LANG_NAME = {"en": "English", "ru": "Russian", "uz": "Uzbek (Latin script)"}

MONTHS = {
    "en": ["January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December"],
    "ru": ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря"],
    "uz": ["yanvar", "fevral", "mart", "aprel", "may", "iyun", "iyul", "avgust",
           "sentabr", "oktabr", "noyabr", "dekabr"],
}
WEEKDAYS = {
    "en": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
    "ru": ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"],
    "uz": ["dushanba", "seshanba", "chorshanba", "payshanba", "juma", "shanba", "yakshanba"],
}
JOIN = {"en": " and ", "ru": " и ", "uz": " va "}


def _(en, ru, uz):
    return {"en": en, "ru": ru, "uz": uz}


WHEN = {"today": _("today", "сегодня", "bugun"),
        "tomorrow": _("tomorrow", "завтра", "ertaga"),
        "week": _("this week", "на этой неделе", "shu hafta")}


def fmt_date(d, lang):
    m = MONTHS[lang][d.month - 1]
    return {"en": f"{m} {d.day}", "ru": f"{d.day} {m}", "uz": f"{d.day}-{m}"}[lang]


def fmt_today(d, lang):
    return f"{WEEKDAYS[lang][d.weekday()]}, {fmt_date(d, lang)}"


def day_label(d, lang):
    import datetime
    today = datetime.date.today()
    if d == today:
        return WHEN["today"][lang]
    if d == today + datetime.timedelta(days=1):
        return WHEN["tomorrow"][lang]
    return f"{WEEKDAYS[lang][d.weekday()]} {fmt_date(d, lang)}"


def join_names(names, lang):
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + JOIN[lang] + names[-1]


S = {
    "ready": _("Linda online.", "Линда на связи.", "Linda tayyor."),
    "listening": _("Yes?", "Слушаю.", "Eshitaman."),
    "working": _("One moment.", "Секунду.", "Bir soniya."),
    "done": _("Done.", "Готово.", "Tayyor."),
    "error": _("Sorry, something went wrong.", "Извини, что-то пошло не так.", "Kechirasiz, xatolik yuz berdi."),
    "need_internet": _("I need an internet connection for that.", "Для этого нужен интернет.", "Buning uchun internet kerak."),
    "time": _("It's {time}.", "Сейчас {time}.", "Soat {time}."),
    "battery": _("Battery is at {n} percent.", "Заряд батареи {n} процентов.", "Batareya {n} foiz."),
    "opened": _("Opening {app}.", "Открываю {app}.", "{app} ochilmoqda."),
    "volume": _("Volume {n}.", "Громкость {n}.", "Ovoz {n}."),
    "timer_set": _("Timer set for {n} seconds.", "Таймер на {n} секунд.", "{n} soniyaga taymer qo'ydim."),
    "timer_done": _("Timer finished.", "Таймер сработал.", "Taymer tugadi."),
    "timer_done_label": _("Reminder: {label}.", "Напоминание: {label}.", "Eslatma: {label}."),
    "note_saved": _("Got it, I'll remember that.", "Запомнила.", "Eslab qoldim."),
    "good_morning": _("Good morning{user}. Today is {date}.", "Доброе утро{user}. Сегодня {date}.", "Xayrli tong{user}. Bugun {date}."),
    "clip_empty": _("The clipboard is empty.", "Буфер обмена пуст.", "Bufer bo'sh."),
    "screenshot": _("Screenshot saved to the Desktop.", "Скриншот сохранён на рабочем столе.", "Skrinshot ish stoliga saqlandi."),
    # birthdays (names stay in nominative to avoid declension problems)
    "bday_saved": _("Saved: {name}, birthday on {date}.", "Запомнила: {name}, день рождения {date}.", "Saqladim: {name}, tug'ilgan kun {date}."),
    "bday_bad": _("I didn't catch the date. Say the day and the month.", "Не поняла дату. Назови день и месяц.", "Sanani tushunmadim. Kun va oyni ayting."),
    "bday_none": _("No birthdays saved yet.", "Пока нет сохранённых дней рождения.", "Saqlangan tug'ilgan kunlar yo'q."),
    "bday_deleted": _("Deleted {name}.", "Удалила: {name}.", "O'chirdim: {name}."),
    "bday_notfound": _("I couldn't find {name}.", "Не нашла: {name}.", "Topolmadim: {name}."),
    "bday_today": _("Birthday today: {names}.", "Сегодня день рождения: {names}.", "Bugun tug'ilgan kun: {names}."),
    "bday_tomorrow": _("Birthday tomorrow: {names}.", "Завтра день рождения: {names}.", "Ertaga tug'ilgan kun: {names}."),
    "bday_item": _("{name}, {date}, in {n} days.", "{name} — {date}, осталось дней: {n}.", "{name} — {date}, {n} kun qoldi."),
    "greeting_header": _("🎂 Birthday today: {who}. Greeting below, forward it if you like 👇",
                         "🎂 Сегодня день рождения: {who}. Поздравление ниже, перешли, если понравится 👇",
                         "🎂 Bugun tug'ilgan kun: {who}. Tabrik quyida, yoqsa yuborib qo'ying 👇"),
    # telegram
    "tg_sent": _("Sent to your Telegram.", "Отправила тебе в Телеграм.", "Telegramingizga yubordim."),
    "tg_missing": _("Telegram isn't set up yet.", "Телеграм ещё не настроен.", "Telegram hali sozlanmagan."),
    "tg_fail": _("I couldn't reach Telegram.", "Не получилось связаться с Телеграмом.", "Telegramga ulana olmadim."),
    "tg_nothing": _("I have nothing to send yet.", "Пока нечего отправлять.", "Hozircha yuboradigan narsa yo'q."),
    # calendar
    "cal_missing": _("I don't have calendar access.", "Нет доступа к календарю.", "Taqvimga ruxsat yo'q."),
    "cal_added": _("Added to your calendar: {title}, {day} at {time}.", "Добавила в календарь: {title}, {day} в {time}.", "Taqvimga qo'shdim: {title}, {day} soat {time}."),
    "cal_undo_ok": _("Removed the last event I added.", "Удалила последнее добавленное событие.", "Oxirgi qo'shilgan tadbirni o'chirdim."),
    "cal_undo_none": _("I haven't added anything I could remove.", "Мне нечего удалять.", "O'chiradigan narsa yo'q."),
    "cal_none": _("No events {when}.", "Событий нет: {when}.", "{when} tadbirlar yo'q."),
    "cal_count": _("Events {when}: {n}.", "События {when}: {n}.", "{when} tadbirlar: {n} ta."),
    "cal_item": _("{day} at {time}: {title}.", "{day} в {time}: {title}.", "{day} soat {time}: {title}."),
    "cal_allday": _("{day}, all day: {title}.", "{day}, весь день: {title}.", "{day}, kun bo'yi: {title}."),
    "cal_soon": _("In {n} minutes: {title}.", "Через {n} минут: {title}.", "{n} daqiqadan keyin: {title}."),
    # files
    "no_file": _("I can't find a file in Downloads.", "Не нашла файл в загрузках.", "Yuklashlar papkasida fayl topmadim."),
    "file_working": _("Reading {name}.", "Читаю файл {name}.", "{name} faylini o'qiyapman."),
    "file_done": _("Done. Saved as {name} in Downloads.", "Готово. Сохранила как {name} в загрузках.", "Tayyor. {name} nomi bilan Yuklashlar papkasiga saqladim."),
    "file_bad": _("I can't read this file type.", "Не могу прочитать этот тип файла.", "Bu fayl turini o'qiy olmayman."),
    # chess
    "chess_white": _("Let's play. You are white. Say your move.", "Давай сыграем. Ты играешь белыми. Говори свой ход.", "Keling, o'ynaymiz. Siz oq donalar bilan o'ynaysiz. Yurishingizni ayting."),
    "chess_black": _("Let's play. You are black. I'll start.", "Давай сыграем. Ты играешь чёрными. Я хожу первой.", "Keling, o'ynaymiz. Siz qora donalar bilan o'ynaysiz. Men boshlayman."),
    "chess_illegal": _("I didn't get a legal move. Say it again, or say stop chess.", "Не поняла ход. Повтори или скажи «стоп шахматы».", "Yurishni tushunmadim. Qaytaring yoki «shaxmatni to'xtat» deng."),
    "chess_my_move": _("My move: {move}.", "Мой ход: {move}.", "Mening yurishim: {move}."),
    "chess_check": _("Check.", "Шах.", "Shoh."),
    "chess_win_user": _("Checkmate. You win!", "Мат. Победа за тобой!", "Mat. Siz yutdingiz!"),
    "chess_win_me": _("Checkmate. I win!", "Мат. Я выиграла!", "Mat. Men yutdim!"),
    "chess_draw": _("It's a draw.", "Ничья.", "Durang."),
    "chess_stop": _("Okay, chess stopped.", "Хорошо, шахматы остановлены.", "Yaxshi, shaxmat to'xtatildi."),
    "chess_undo": _("Taking back the last two moves.", "Отменяю последние два хода.", "Oxirgi ikki yurish bekor qilindi."),
    "chess_hint": _("Try {move}.", "Попробуй: {move}.", "Buni sinab ko'ring: {move}."),
}


from .i18n_study import S_STUDY  # noqa: E402
S.update(S_STUDY)


def t(key, lang="en", **kw):
    return (S[key].get(lang) or S[key]["en"]).format(**kw)
