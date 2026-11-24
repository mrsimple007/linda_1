"""Finance messages inside Linda's existing Telegram router (briefing.on_telegram_text).

No polling here: telegram.py already listens, only for your chat id, and skips old messages at startup.
handle(text) returns a reply string, or None when the text is not a finance message so the caller
carries on with its normal flow (commands, LLM filing, questions).
"""
from __future__ import annotations

import re
import time
from typing import Optional

from linda.business.finance import FinanceError, FinanceService, ParseError, Tx, fmt_amount

YES = {"yes", "y", "ok", "ha", "da", "ага", "да", "👍"}
NO = {"no", "n", "cancel", "/cancel", "yo'q", "yoq", "нет", "отмена"}
UNDO = {"undo", "/undo", "bekor", "отменить"}
ADD = {"add", "add it", "merge", "same", "same row", "qo'sh", "qosh", "qo'shish", "добавь", "добавить", "туда"}
NEW = {"new", "new row", "separate", "own row", "yangi", "alohida", "новая", "новую", "отдельно"}
SUMMARY = ("/summary", "summary", "/balance", "balance", "/finance")
# a transaction starts with an optional sheet prefix, then + or − and a digit: "-40 000 ...", "c: +670 000 ..."
TX_START = re.compile(r"^\s*(?:(?:c|co|biz|company|kompaniya|компания|m|my|personal|shaxsiy|личное)\s*[:,]\s*)?"
                      r"[+\-\u2212\u2013\u2014]\s*\d", re.I)


class FinanceChat:
    def __init__(self, service: FinanceService, confirm_ttl: int = 90, clock=time.time):
        self.svc, self.confirm_ttl, self.clock = service, confirm_ttl, clock
        self.pending: Optional[tuple[Tx, float]] = None   # company entry waiting for yes / no
        self.merge_q: Optional[tuple[Tx, float]] = None   # entry that looks like an existing row: add / new

    def awaiting(self) -> bool:
        """A yes/no or add/new answer is expected (the router sends such replies here first)."""
        return bool(self.pending or self.merge_q)

    def matches(self, text: str) -> bool:
        low = text.strip().lower().rstrip(".!")
        return bool(TX_START.match(text) or low in UNDO or low.split(" ")[0] in SUMMARY
                    or (self.pending and (low in YES or low in NO))
                    or (self.merge_q and (low in YES or low in NO or low in ADD or low in NEW)))

    def _dropped(self) -> str:
        """A new entry arrived while one was still waiting: say the old one was not saved."""
        old = self.pending or self.merge_q
        self.pending = self.merge_q = None
        if not old or self.clock() > old[1]:
            return ""
        return f"(Not saved: “{old[0].description}” {fmt_amount(old[0].amount)}, it was waiting for your answer.)\n\n"

    def handle(self, text: str) -> Optional[str]:
        if not self.matches(text):
            return None
        t = text.strip()
        low = t.lower().rstrip(".!")
        try:
            if low in NO:
                self.pending = self.merge_q = None
                return "Cancelled."
            if low in UNDO:
                if self.pending or self.merge_q:
                    self.pending = self.merge_q = None
                    return "Cancelled, nothing was written."
                tx = self.svc.undo_last()
                if tx.merged:
                    return (f"↩️ Took {fmt_amount(tx.amount)} back out of “{tx.description}” "
                            f"({tx.sheet}, row {tx.row}): now {fmt_amount(tx.prev_amount or 0)} UZS")
                return f"↩️ Removed from {tx.sheet} (row {tx.row}): {fmt_amount(tx.amount)} {tx.description}"
            if self.merge_q and (low in ADD or low in YES or low in NEW):
                tx, until = self.merge_q
                self.merge_q = None
                if self.clock() > until:
                    return "That question expired, nothing was written. Send the entry again."
                return self._write(tx, confirmed=True, merge=False if low in NEW else tx.ask)
            if low in YES:
                return self._confirm()
            if low.split(" ")[0] in SUMMARY:
                parts = t.split()
                return self.svc.format_summary(parts[1] if len(parts) > 1 else None)

            tx = self.svc.parse(t)
            note = self._dropped()
            if self.svc.needs_confirmation(tx):
                self.pending = (tx, self.clock() + self.confirm_ttl)
                return f"{note}{self.svc.describe(tx)}\n\nReply yes to add, no to cancel."
            return note + self._write(tx, confirmed=False)
        except ParseError as e:
            return f"❓ {e}"
        except FinanceError as e:
            return f"⚠️ {e}"

    def _confirm(self) -> str:
        if not self.pending or self.clock() > self.pending[1]:
            self.pending = None
            return "Nothing waiting for confirmation (it may have expired)."
        tx, _ = self.pending
        self.pending = None
        return self._write(tx, confirmed=True)

    def _write(self, tx: Tx, confirmed: bool, merge=None) -> str:
        if merge is not None:
            tx.ask = None
        tx = self.svc.add(tx, confirmed=confirmed, source="telegram", merge=merge)
        if tx.ask:                                       # looks like an existing row: the user decides
            self.merge_q = (tx, self.clock() + self.confirm_ttl)
            prev = tx.prev_amount or 0
            return (f"“{tx.description}” looks like your “{tx.ask}” row this month ({fmt_amount(prev)} UZS).\n\n"
                    f"Reply add to put it there ({fmt_amount(prev)} → {fmt_amount(prev + tx.amount)}), "
                    f"or new for its own row.")
        kind = "Income" if tx.type == "income" else "Expense"
        if tx.merged:
            before = tx.prev_amount or 0
            return (f"✅ {tx.sheet}, row {tx.row}: added {fmt_amount(tx.amount)} to the existing “{tx.description}”: "
                    f"{fmt_amount(before)} → {fmt_amount(before + tx.amount)} UZS\nReply undo to take it back out.")
        return (f"✅ {tx.sheet}, row {tx.row}: {kind} {fmt_amount(tx.amount)} UZS, "
                f"{tx.date:%d.%m.%Y}, {tx.description}\nReply undo to remove.")