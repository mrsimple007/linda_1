"""Telegram bot that only ever messages YOU (the chat id in .env). You forward what you like."""
import os
import requests


class Telegram:
    def __init__(self, token, chat_id):
        self.token, self.chat_id = token, chat_id

    @classmethod
    def from_env(cls):
        token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
        return cls(token, chat) if token and chat else None

    def _url(self, method):
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, text):
        if not text or not text.strip():
            print("[telegram] refused: empty text")
            return False
        for i in range(0, len(text), 4000):
            r = requests.post(self._url("sendMessage"),
                            data={"chat_id": self.chat_id, "text": text[i:i + 4000]}, timeout=20)
            r.raise_for_status()
        print(f"[telegram] sent {len(text)} chars")
        return True

    def send_file(self, path, caption=""):
        with open(path, "rb") as f:
            r = requests.post(self._url("sendDocument"),
                              data={"chat_id": self.chat_id, "caption": caption},
                              files={"document": f}, timeout=60)
        r.raise_for_status()