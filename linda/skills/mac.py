"""This Mac: time, battery, apps, volume, music, screenshots/lock, timers, clipboard, latest download."""
import datetime
from linda import system as sysm
from linda.files import UnsupportedFile, latest_download, prepare_input, save_text, split_title
from linda.i18n import LANG_NAME

FILE_WRAPPER = """Task: {task}

Work on the content provided above (original file name: {orig}).
Output format: the first line must be exactly "TITLE: <short descriptive file name based on the document's main topic or its original name, no extension>", then a blank line, then the result as plain text only. No markdown fences, no commentary."""


class MacMixin:
    """Commands that act on this computer."""

    def do_time(self, it, text):
        self.k("time", time=datetime.datetime.now().strftime("%H:%M"))

    def do_battery(self, it, text):
        n = sysm.battery_percent()
        self.k("battery", n=n) if n is not None else self.k("error")

    def do_open_app(self, it, text):
        if it.app and sysm.open_app(it.app):
            self.k("opened", app=it.app)
        else:
            self.k("error")

    def do_volume(self, it, text):
        if it.level < 0:
            return self.k("error")
        self.k("volume", n=sysm.set_volume(it.level))

    def do_music(self, it, text):
        res = sysm.music(it.action, it.text)
        status, detail = res if isinstance(res, tuple) else (("ok", it.action) if res else ("fail", ""))
        L = {"played": {"en": f"Playing {detail}. Enjoy!", "ru": f"Включаю {detail}. Приятного!", "uz": f"{detail} qo'yaman. Rohat qiling!"},
             "opened": {"en": f"I opened Spotify's search for {detail}, but I can't press play myself without Spotify keys.",
                        "ru": f"Открыла поиск Spotify по запросу {detail}, но сама включить не могу без ключей Spotify.",
                        "uz": f"Spotify qidiruvini ochdim, lekin Spotify kalitlarisiz o'zim qo'sha olmayman."},
             "ok": {"en": "Done!", "ru": "Готово!", "uz": "Tayyor!"}}
        if status in L:
            self.say(L[status][self.lang])
        else:
            self.say({"en": "I couldn't control Spotify. Is the app installed?", "ru": "Не получилось управлять Spotify.",
                      "uz": "Spotify'ni boshqara olmadim."}[self.lang])

    def do_mac(self, it, text):
        a = it.action.lower()
        if a == "screenshot":
            sysm.screenshot()
            return self.k("screenshot")
        if a in ("mute", "unmute"):
            sysm.mute(a == "mute")
        elif a == "lock":
            self.k("done")
            sysm.lock()
            return
        else:
            return self.k("error")
        self.k("done")

    def do_timer(self, it, text):
        n, lang, label = max(1, it.seconds), self.lang, it.text.strip()
        sysm.start_timer(n, lambda: self.k("timer_done_label", lang, label=label) if label
                         else self.k("timer_done", lang))
        self.k("timer_set", n=n)

    def do_clipboard(self, it, text):
        clip = sysm.clipboard().strip()
        if not clip:
            return self.k("clip_empty")
        if not self.cloud():
            return self.k("need_internet")
        prompt = (f"{it.instruction or 'Summarize this briefly'}.\nAnswer in {LANG_NAME[self.lang]}, in plain "
                  f"spoken sentences, no markdown.\n\n---\n{clip[:20000]}")
        self._out(reply=self._stream_say(self.gemini.stream(prompt), self.lang))

    def do_process_download(self, it, text):
        if not self.cloud():
            return self.k("need_internet")
        folder = self.cfg["downloads_dir"]
        path = latest_download(folder)
        if not path:
            return self.k("no_file")
        presets = self.cfg.get("presets", {})
        task = presets[it.preset]["prompt"] if it.preset in presets else (it.instruction or text)
        self.k("file_working", name=path.name)
        try:
            parts = prepare_input(path)
        except UnsupportedFile:
            return self.k("file_bad")
        out = self.gemini.text([*parts, FILE_WRAPPER.format(task=task, orig=path.name)])
        title, body = split_title(out, path.stem)
        saved = save_text(folder, title, body)
        sysm.reveal(saved)
        self._out(file=saved)
        self.k("file_done", name=saved.name)
