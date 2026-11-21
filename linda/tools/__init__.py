"""Linda's tools (what the AI can call). Split by job; every name is still available as linda.tools.<name>:
  registry.py   TABLE of tool declarations, SPEC, SEQUENTIAL, declarations()
  tasks.py      tasks, projects, tasks.md, progress log, argument helpers
  handlers.py   the other write handlers, SPECIAL, execute(), missing()
  linda/memory/recall.py   memory read: recall, prefetch, memory_index"""
from linda.tools.registry import *  # noqa: F401,F403
from linda.tools.tasks import *  # noqa: F401,F403
from linda.tools.handlers import *  # noqa: F401,F403
from linda.memory.recall import *  # noqa: F401,F403
from linda.tools.registry import _TYPE  # noqa: F401
from linda.tools.tasks import _GENERIC, _NUMW, _STOP, _belongs, _calendar_add, _can_retry, _dmy, _existing_project, _ids, _key, _log_done, _log_progress, _norm, _resolve_project, _same, _task_add, _task_done, _task_project, _tt_db  # noqa: F401
from linda.tools.handlers import _birthday_list, _bot_stats, _clients, _finance_report, _is_me, _mem, _news, _practice_delete, _practice_log, _query_data, _read_note, _resolve_day, _song_add, _song_db, _web_answer  # noqa: F401
from linda.memory.recall import _dates_in, _diary, _done_on, _dump, _expand, _file_lines, _filter, _gather, _has, _lines, _match, _mentions_person, _practice_on, _size, _stem, _tok  # noqa: F401

import datetime
import difflib
import json
import pathlib
import re
import sqlite3
import threading
import time
from google.genai import types
from linda.config import ROOT
from linda.i18n import day_label, fmt_date
from linda.router import Intent
from linda.memory import memory_files
from linda.memory import reference
