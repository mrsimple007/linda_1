"""Finance messages inside Linda's existing Telegram router (briefing.on_telegram_text).

No polling here: telegram.py already listens, only for your chat id, and skips old messages at startup.
handle(text) returns a reply string, or None when the text is not a finance message so the caller
carries on with its normal flow (commands, LLM filing, questions).
"""
from __future__ import annotations

import re
import time
from typing import Optional

from .finance import FinanceError, FinanceService, ParseError, Tx, fmt_amount

YES = {"yes", "y", "ok", "ha", "da", "ага", "да", "👍"}
NO = {"no", "n", "cancel", "/cancel", "yo'q", "yoq", "нет", "отмена"}
UNDO = {"undo", "/undo", "bekor", "отменить"}
SUMMARY = ("/summary", "summary", "/balance", "balance", "/finance")
# a transaction starts with an optional sheet prefix, then + or − and a digit: "-40 000 ...", "c: +670 000 ..."
TX_START = re.compile(r"^\s*(?:(?:c|co|biz|company|kompaniya|компания|m|my|personal|shaxsiy|личное)\s*[:,]\s*)?"
                      r"[+\-\u2212\u2013\u2014]\s*\d", re.I)


class FinanceChat:
    def __init__(self, service: FinanceService, confirm_ttl: int = 90, clock=time.time):
        self.svc, self.confirm_ttl, self.clock = service, confirm_ttl, clock
        self.pending: Optional[tuple[Tx, float]] = None

    def matches(self, text: str) -> bool:
        low = text.strip().lower()
        return bool(TX_START.match(text) or low in UNDO or low.split(" ")[0] in SUMMARY
                    or (self.pending and (low in YES or low in NO)))

    def handle(self, text: str) -> Optional[str]:
        if not self.matches(text):
            return None
        t = text.strip()
        low = t.lower()
        try:
            if low in NO:
                self.pending = None
                return "Cancelled."
            if low in UNDO:
                if self.pending:
                    self.pending = None
                    return "Cancelled, nothing was written."
                tx = self.svc.undo_last()
                if tx.merged:
                    return (f"↩️ Took {fmt_amount(tx.amount)} back out of “{tx.description}” "
                            f"({tx.sheet}, row {tx.row}): now {fmt_amount(tx.prev_amount or 0)} UZS")
                return f"↩️ Removed from {tx.sheet} (row {tx.row}): {fmt_amount(tx.amount)} {tx.description}"
            if low in YES:
                return self._confirm()
            if low.split(" ")[0] in SUMMARY:
                parts = t.split()
                return self.svc.format_summary(parts[1] if len(parts) > 1 else None)

            tx = self.svc.parse(t)
            if self.svc.needs_confirmation(tx):
                self.pending = (tx, self.clock() + self.confirm_ttl)
                return f"{self.svc.describe(tx)}\n\nReply yes to add, no to cancel."
            return self._write(tx, confirmed=False)
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

    def _write(self, tx: Tx, confirmed: bool) -> str:
        tx = self.svc.add(tx, confirmed=confirmed, source="telegram")
        kind = "Income" if tx.type == "income" else "Expense"
        if tx.merged:
            before = tx.prev_amount or 0
            return (f"✅ {tx.sheet}, row {tx.row}: added {fmt_amount(tx.amount)} to the existing “{tx.description}”: "
                    f"{fmt_amount(before)} → {fmt_amount(before + tx.amount)} UZS\nReply undo to take it back out.")
        return (f"✅ {tx.sheet}, row {tx.row}: {kind} {fmt_amount(tx.amount)} UZS, "
                f"{tx.date:%d.%m.%Y}, {tx.description}\nReply undo to remove.")