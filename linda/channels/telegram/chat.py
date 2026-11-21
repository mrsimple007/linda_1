"""Free text through the AI agent loop: Telegram rules, nudges, what was saved (files, related links)."""
import datetime
import re
import threading
import time
from linda import tools
from linda.memory import reference
from linda.channels.telegram.telegram import esc
from linda import providers as _providers

TG_RULES = (                     # always sent; rules for other tools travel with the tool (providers.TOOL_RULES)
    "Channel: a Telegram text chat, not voice. Handle EVERYTHING the message contains with tools in the SAME turn "
    "(several calls at once are fine); don't reply until every item is handled. Mapping: new to-do -> task_add, ONE "
    "call per separate to-do (never a combined title; 'and also ...' is a new to-do; never re-add an open task); "
    "finished tasks -> task_done with ids from the open tasks list ('all done except X' -> keep_ids; tasks 'still in "
    "progress' stay open); finished work matching no open task -> log_progress (project 'General' if none), written as "
    "what was done; an event ahead is task_add AND preparation already finished is ALSO log_progress; ideas / goals / "
    "lasting facts about the user -> save_note file=ideas / goals / profile; feelings -> journal_add; tiny one-off "
    "reminders -> remember. A message saying work was done ALWAYS ends in a tool call, never praise alone. "
    "A QUESTION is a read: answer from the stored data, save nothing, and answer completely (every matching person or "
    "item with the details asked, one per line). After SAVING reply with ONE short friendly line; wishes and greetings "
    "are written in full. Never say 'all set' before the thing itself is saved. Call them 'tasks' (never 'to-do list'). "
    "A decision ('we decided', 'I chose', 'going with', 'decided not to') -> save_note file=decisions, section = the "
    "project, text = the decision + reason. 'What did I say / we discuss about X' -> recall kind=conversations."
)



DONE_WORK = re.compile(r"\b(studied|improved|finished|completed|implemented|built|fixed|worked on|practiced|"
                       r"learned|prepared|wrote|did)\b", re.I)
PLANNING = re.compile(r"\b(will|plan|planning|tomorrow|need to|going to|want to|have to)\b", re.I)
NUDGE = (" You answered without recording anything. This message reports finished work: call task_done if it matches "
         "an open task, otherwise call log_progress (no_matching_task=true, project 'General' if no project fits).")

PREP_DONE = re.compile(r"\b(?:ready|prepared|finished|done)\b", re.I)
NUDGE_PREP = (" The message also says preparation is already finished. Call log_progress for that (project it belongs to, "
              "no_matching_task=true). Do not call task_add again.")

LOG_ICONS = {"log_progress": ("🛠", "Progress"), "save_note": ("📌", "Saved"), "journal_add": ("📓", "Journal"),
             "remember": ("📝", "Note"), "practice_log": ("🎹", "Practice"), "song_add": ("🎵", "Song"),
             "save_reference": ("📚", "Reference")}


def _tool_lines(brain, agent=None):
    out = []
    for name, args, res in brain.tool_log:
        if name in LOG_ICONS and not res.lower().startswith("error"):
            icon, label = LOG_ICONS[name]
            if name == "save_note" and str(args.get("file") or "").lower().startswith("decision"):
                icon, label = "⚖️", "Decision"
            proj = f" ({esc(args['project'])})" if name == "log_progress" and args.get("project") else ""
            if name == "save_reference":
                shown = f"{args.get('topic') or 'notes'}: {len(str(args.get('text') or ''))} characters saved in full"
            else:
                shown = str(args.get("text") or args.get("name") or res)[:300]
            line = f"{icon} <b>{label}</b>{proj}: {esc(shown)}"
            where = reference.where_saved(agent, name, args)
            if where:
                line += f"\n   📁 <code>{esc(where)}</code>"
            line += _related(agent, name, args, where)
            out.append(line)
    return out

LINKED = {"save_reference", "save_note", "log_progress", "journal_add"}


def _related(agent, name, args, where):
    """'\n   🔗 data/notes/x.md · data/projects/y.md': saved files about the same thing ('' if none)."""
    if agent is None or name not in LINKED:
        return ""
    from linda.memory import links
    path = agent.data_dir / where[5:] if where.startswith("data/") else None
    found = links.related(agent, args.get("text") or "", exclude=[path] if path else [])
    if not found:
        return ""
    if path is not None and name == "save_reference":
        try:
            links.annotate(agent, path, found)
        except Exception as e:
            print("[links annotate]", repr(e)[:120])
    return "\n   🔗 " + " · ".join(f"<code>{esc(reference.rel(agent, p))}</code>" for p in found)
_providers.TOOL_RULES.update({
    "save_reference": "Longer information (course outlines, lesson or topic lists, schedules, resources, how-tos, "
        "checklists) and facts about an ongoing course or project -> save_reference: topic = the subject, text = the "
        "user's full text copied exactly (never a summary). Scholarships, grants, programmes, calls for applications -> "
        "save_reference topic 'Scholarships' (or 'Opportunities'); if the user wants to apply, ALSO task_add "
        "'Apply: <programme> (deadline <date time>)' with due = the deadline. Show a saved note with read_reference.",
    "client_add": "The user's AGENCY CLIENTS (people going to Germany): client_add for a new client or pasted details "
        "(partial is fine: save, never ask for more), client_update for steps/fields of an existing one, client_info / "
        "client_list for questions. Never task_add, save_note or remember client data.",
    "bot_stats": "Questions about the user's OWN BOTS (Simple Quizzer, Simple Slides: users, new/active users, quizzes, "
        "presentations, files, payments, any period) -> bot_stats with the question in full; never state bot numbers yourself.",
    "finance_report": "Any question about the user's money (revenue, income, expenses, balance, purchases) -> "
        "finance_report with the question in full. Never state finance numbers and never save such a question.",
    "remove_note": "To delete or correct something stored, call remove_note (file + a few words of the line) and let the "
        "tool decide; never say 'not found' yourself. Near-identical names (Xolid / Xolida) are the same person.",
})

PLAN_HINT = ("The user was just asked for today's plans: treat what they list as new tasks due today (task_add), "
             "unless a line clearly is something else.")


def _converse(agent, tg, text, in_window):
    """One LLM agent loop (same as voice mode: recall + tools + answer). Returns False if no provider answered."""
    brain = getattr(agent, "brain", None)
    if brain is None or not getattr(brain, "providers", None):
        return False
    before = [r[1] for r in agent.store.open_tasks()]
    agent.card_sent = False                                 # set by tools that send their own card (bot stats, clients)
    agent.done_files = {}                                   # filled by tools.finish_task
    agent.added_files = {}                                  # filled by tools.add_task_once
    brain.tool_log = []                                      # NEW
    try:
        with muted(agent, text) as mute:
            agent._produced = False
            if agent.pending_actions and agent.handle_confirmation(text):
                ok = True
            else:
                brain.cancel = threading.Event()
                from linda.skills import proactive
                week = proactive.week_window(agent)              # replying to the Sunday weekly review
                brain.extra_rules = (TG_RULES + (" " + PLAN_HINT if in_window else "")
                                     + (" " + proactive.WEEK_HINT if week else ""))
                brain.read_only = bool(reference.MEM_ASK.search(text))       # a question can never trigger a save
                try:
                    ok = brain._agent_loop(text)
                    acted = any(n != "recall" for n, _, _ in brain.tool_log)
                    if ok and DONE_WORK.search(text) and not PLANNING.search(text) and not acted:   # NEW
                        mute.lines.clear()
                        brain.extra_rules += NUDGE
                        ok = brain._agent_loop(text)
                    names = {n for n, _, _ in brain.tool_log}
                    if ok and "task_add" in names and PREP_DONE.search(text) and not names & {"log_progress", "task_done"}:
                        mute.lines.clear()
                        brain.extra_rules += NUDGE_PREP
                        ok = brain._agent_loop(text)                    
                    
                    
                    elif ok and not acted and reference.looks_like_reference(text):
                        mute.lines.clear()                                       # long dictated info, nothing saved yet
                        brain.extra_rules += reference.NUDGE_REF
                        ok = brain._agent_loop(text)
                finally:
                    brain.extra_rules = ""
                    brain.read_only = False
                if week and ok:
                    proactive.close_week_window(agent)          # one reply answers the review question
    except Exception as e:
        print("[telegram brain]", repr(e)[:200])
        return False
    if not ok:
        return False
    reply = " ".join(mute.lines).strip() or "Done."
    after = [r[1] for r in agent.store.open_tasks()]
    b, a = {tools._norm(x) for x in before}, {tools._norm(x) for x in after}
    # what really changed, read back from the database (not what the model claims)
    changes = [f"☑️ <b>Closed</b>: {esc(x)}" + done_files(agent, x) for x in before if tools._norm(x) not in a]
    changes += [f"➕ <b>Task added</b>: {esc(x)}" + done_files(agent, x, "added_files")
                for x in after if tools._norm(x) not in b]
    changes += _tool_lines(brain, agent)
    brain.history = (brain.history + [("user", text), ("model", reply)])[-2 * brain.cfg["brain"]["history_turns"]:]
    _remember_turn(agent, brain, text, reply, changes)
    if getattr(agent, "card_sent", False) and not changes:
        return True                                          # the card IS the answer; skip "I'll check that for you."
    if GREET_WORDS.search(text):
        tg.send(reply)                                       # plain text: easy to forward
    else:
        tg.send_styled(S[_lang(agent)]["hint_title"], [("", changes)] if changes else [],
                       icon="💬", intro=reply)
    return True


def _remember_turn(agent, brain, text, reply, changes):
    """Keep the chat (searchable later) and re-index files this turn changed."""
    from linda.memory import history
    used = [n for n, _, _ in brain.tool_log]
    shown = reply + (" | " + " | ".join(changes) if changes else "")
    history.log_turn(agent.cfg, text, getattr(agent, "lang", "en"), used, shown, channel="telegram")
    history.reindex(agent)                                   # the chat log itself + any file saved this turn


def _free_text(agent, tg, text):
    t = S[_lang(agent)]
    from linda.skills import smart_home
    said = smart_home.quick(agent, text)
    if said:
        return tg.send(said)
    in_window = time.time() < getattr(agent, "plans_until", 0)
    tg.typing()
    if NEWS_ASK.search(text) and len(text.split()) <= 14:
        return _news(agent, tg)
    if in_window:
        agent.plans_until = time.time() + PLANS_WINDOW
    if _converse(agent, tg, text, in_window):
        return
    # ---- fallback only: every provider is down -> the old classifier ----
    if reference.looks_like_reference(text):          # providers down: never let the classifier cut it to 400 chars
        return _save_reference_direct(agent, tg, text)
    if "\n" not in text and QUESTION.search(text):
        return _answer(agent, tg, text)
    items, reply = classify(agent, text, in_window)
    if items is None:
        if in_window and parse_plans(text):
            return _plans_reply(agent, tg, parse_plans(text))
        return tg.send_styled(t["hint_title"], icon="💬", intro=t["no_ai"])
    if in_window:
        for it in items:
            if it["type"] == "task" and not it["due"]:
                it["due"] = str(datetime.date.today())
    if not items:
        return _answer(agent, tg, text, fallback=reply or t["nothing"])
    return _save_items(agent, tg, items, text)


# names from the sibling modules (imported last, so mutual use works)
from linda.channels.telegram.common import NEWS_ASK, PLANS_WINDOW, S, _lang, _save_reference_direct  # noqa: E402
from linda.channels.telegram.news import muted  # noqa: E402
from linda.channels.telegram.commands import _news  # noqa: E402
from linda.channels.telegram.filing import GREET_WORDS, QUESTION, _answer, _plans_reply, _save_items, classify, done_files, parse_plans  # noqa: E402
