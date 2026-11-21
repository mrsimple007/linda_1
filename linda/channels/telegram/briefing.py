"""Telegram side of Linda: morning message, news digest with links, commands, and filing what you write.

NEWS      RSS feeds (free, no quota, with links)  ->  an LLM picks/groups/rewrites by INDEX  ->  links are attached
          by code, never by the model, so they cannot be invented or altered. Order of LLMs: Groq/Mistral
          (agent.providers), then Gemini. If every LLM is down you still get the raw headlines with links.
TELEGRAM  /start /help /news /tasks /birthdays /plans /idea /goal ... or just write: an LLM decides whether it is
          an idea, goal, profile fact, task, diary line, progress note or quick note, and saves it with Linda's
          own tools. Nothing is spoken aloud on the Mac while this happens.

Split by job (every name is still available as briefing.<name>): common, news, commands, filing, chat, finance_gate, routing."""
from linda.channels.telegram.common import *  # noqa: F401,F403
from linda.channels.telegram.news import *  # noqa: F401,F403
from linda.channels.telegram.commands import *  # noqa: F401,F403
from linda.channels.telegram.filing import *  # noqa: F401,F403
from linda.channels.telegram.chat import *  # noqa: F401,F403
from linda.channels.telegram.finance_gate import *  # noqa: F401,F403
from linda.channels.telegram.routing import *  # noqa: F401,F403
from linda.channels.telegram.common import _balance_card, _chunk_text, _json_obj, _lang, _save_reference_direct  # noqa: F401
from linda.channels.telegram.news import _LOCK, _Mute, _do, _fetch_feed, _gemini_news, _open_today  # noqa: F401
from linda.channels.telegram.commands import _command, _due_label, _news, _notes_command, _pieces, _tasks, _today, _welcome  # noqa: F401
from linda.channels.telegram.filing import _answer, _complete_tasks, _context, _done_command, _done_tool, _hist, _open_titles, _plans_reply, _save_items, _write_md  # noqa: F401
from linda.channels.telegram.chat import _converse, _free_text, _related, _remember_turn, _tool_lines  # noqa: F401
from linda.channels.telegram.finance_gate import _bar, _fin_answer, _fin_limit, _fin_month, _fin_pin, _fin_plan, _fin_sheets, _money, _month_label, _pin_like, _recent_card, _summary_card  # noqa: F401
from linda.channels.telegram.routing import _is_pointer, _on_telegram_text, _strip_copied_sender, _typing  # noqa: F401

import concurrent.futures
import contextlib
import datetime
import difflib
import html as _html
import json
import re
import threading
import time
import xml.etree.ElementTree as ET
import requests
import os
from linda import tools
from linda.i18n import fmt_date, fmt_today
from linda.memory import reference
from linda.channels.telegram.telegram import esc
from linda.business.finance import FinanceError
from linda import providers as _providers
import hmac
from linda.business.finance import fmt_amount
