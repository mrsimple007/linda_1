"""Every skill as a function declaration the LLM can call: TABLE, SPEC (by name), SEQUENTIAL, declarations()."""
from google.genai import types

S, I, B = "string", "integer", "boolean"
TG = {"telegram": (B, "true if the user also wants the result sent to their Telegram")}
# (name, description, {param: (type, description)}, required, alias {tool param -> Intent field})


TABLE = [
    ("process_download", "Do something with the NEWEST file in the user's Downloads folder (convert, rewrite, summarize, extract, translate).",
     {"instruction": (S, "what to do, in the user's words"),
      "preset": (S, "'quiz_txt' to turn a quiz/test document into plain text: numbered question, +right answer, -wrong answers; otherwise empty"), **TG}, [], {}),
    ("news", "Spoken world-news briefing (uses Google Search).",
     {"language": (S, "en, ru or uz, only if the user names a language"), "topic": (S, "optional topic"), **TG}, [], {}),
    ("morning", "Good-morning briefing: greeting, birthdays, today's calendar, weather, news.", {}, [], {}),
    ("weather", "Weather report.", {"topic": (S, "city, only if the user names one"), **TG}, [], {}),
    ("add_birthday", "Save someone's birthday.",
     {"name": (S, "person"), "relation": (S, "mother, friend..."), "day": (I, "1-31"), "month": (I, "1-12"),
      "year": (I, "birth year if said"), "language": (S, "greeting language en/ru/uz if said")}, ["name", "day", "month"], {}),
    ("list_birthdays", "List the saved birthdays. For 'send me the birthday list on Telegram' set telegram=true: the full list is built from the database, never write it yourself.", {**TG}, [], {}),
    ("delete_birthday", "Delete a saved birthday.", {"name": (S, "person")}, ["name"], {}),
    ("birthday_greeting", "Write a birthday greeting and send it to the user's Telegram so they can forward it.",
     {"name": (S, "person"), "relation": (S, "relation"), "language": (S, "en, ru or uz")}, ["name"], {}),
    ("calendar_add", "Add an event to the calendar.",
     {"title": (S, "short title in the user's language, no date words"),
      "start": (S, "local ISO datetime, e.g. 2026-10-28T15:00 (09:00 if no time said)"),
      "duration_minutes": (I, "default 60")}, ["title", "start"], {}),
    ("calendar_agenda", "Read what is on the calendar.", {"when": (S, "today, tomorrow or week")}, [], {}),
    ("calendar_undo", "Remove the calendar event that was just added.", {}, [], {}),
    ("telegram_send", "Send something to the user's own Telegram (not lists of stored data: use that tool with telegram=true).",
     {"text": (S, "the full message; greetings and wishes warm, personal, several sentences. Empty only for 'send that' about your last answer")}, ["text"], {}),
    ("clipboard", "Work with the text the user copied (summarize, translate, fix, explain).",
     {"instruction": (S, "what to do"), **TG}, [], {}),
    ("mac", "Mac control.", {"action": (S, "lock, mute, unmute or screenshot")}, ["action"], {}),
    ("chess", "Start a chess game with the user.", {"color": (S, "white or black")}, [], {}),
    ("timer", "Countdown timer or reminder after a delay.",
     {"seconds": (I, "total seconds"), "text": (S, "what to be reminded about")}, ["seconds"], {}),
    ("open_app", "Open a macOS application.", {"app": (S, "application name")}, ["app"], {}),
    ("volume", "Set the volume.", {"level": (I, "0-100")}, ["level"], {}),
    ("time", "Tell the current local time.", {}, [], {}),
    ("battery", "Tell the battery level.", {}, [], {}),
    ("remember", "Remember a small one-off fact or reminder. Lasting facts about the user go to save_note instead.",
     {"text": (S, "the fact, one full sentence")}, ["text"], {}),
    ("plan", "Plan how to use the time the user has (practice, driving theory...). Explains why.",
     {"minutes": (I, "time available"), "topic": (S, "'practice' if they ask only about practice")}, [], {}),
    ("analyze_playing", "Listen to the user play an instrument and give feedback from pitch/timing analysis.",
     {"name": (S, "song, if said"), "instrument": (S, "piano or guitar")}, [], {"instrument": "action"}),
    ("tune", "Tune an instrument: listens to one note.", {}, [], {}),
    ("quiz", "Start a voice quiz (driving theory and other question banks).",
     {"topic": (S, "question set name if said"), "total": (I, "number of questions if said")}, [], {}),
    ("log_driving", "Save a driving test result.",
     {"score": (I, "points or correct answers"), "total": (I, "maximum"), "kind": (S, "theory, practical or mock_test"),
      "notes": (S, "notes")}, ["score", "total"], {"kind": "topic", "notes": "text"}),
    ("task_add", "Add a task. If the user says today or tomorrow, put that real date in due. ONE call per separate to-do: never merge different things into one title. "
     "Also use it when the user says they NEED or WANT to do something later (e.g. practice in the evening): put a time if said. "
     "Not for work already finished, and not if a matching open task exists. "
     "Set project to the ongoing project it belongs to, by meaning, if any.",
     {"title": (S, "the task"),
      "due": (S, "due date YYYY-MM-DD if said"),
      "time": (S, "time of day HH:MM if said (evening = 20:00)"),
      "project": (S, "exact name of the ongoing project this belongs to, if any")}, ["title"], {}),
    ("task_list", "List the open tasks.", {}, [], {}),
    ("task_done", "Close open tasks the user finished (match by meaning; speech-to-text may distort words). Use ids from "
     "the open tasks list; ids='3,5,7' closes several. 'Everything done except X, Y' -> keep_ids='<ids of X, Y>' (all "
     "OTHER open tasks close). Tasks still in progress stay open.",
     {"id": (I, "id of ONE task"),
      "ids": (S, "ids to close, e.g. '3,5,7'"),
      "keep_ids": (S, "ids that STAY open; every other open task closes"),
      "text": (S, "words from the task, only if you have no id"),
      "note": (S, "2 sentences in English: what finishing it achieved (single task; goes to the project log)"),
      "project": (S, "exact name of the ongoing project it belongs to, if any")}, [], {}),
      ("song_add", "Add a song or piece to the repertoire. FIRST work out the proper title and the composer or artist from your own knowledge, then save with full details. Never save a bare guess.",
     {"name": (S, "proper title, e.g. 'Game of Thrones (Main Theme)'"), "composer": (S, "composer or artist, e.g. 'Ramin Djawadi'"),
      "instrument": (S, "piano or guitar"), "status": (S, "wishlist (wants to learn it), learning, polishing or learned"),
      "notes": (S, "one short line (difficulty, key, arrangement) if known")}, ["name"], {}),
    ("song_status", "Change a song's status when the user says they want to learn it, started it, are polishing it, learned/finished it, or paused/dropped it.",
     {"name": (S, "song title as said"), "status": (S, "wishlist, learning, polishing, learned or archived"),
      "instrument": (S, "piano or guitar, if said")}, ["name", "status"], {}),
    ("song_suggest", "Suggestions about the user's music: what to practice now, what to learn next from their want-to-learn list, or new pieces that fit what they have learned. Returns their songs grouped by status; then YOU give the advice.",
     {"focus": (S, "practice, next or new_ideas")}, [], {}),
    ("music", "Control Spotify. action=play with query plays that specific song or artist (e.g. query 'The Nights Avicii'); action=play without query resumes; also pause, next, previous. ONLY when the user explicitly asks for music.",
     {"action": (S, "play, pause, next or previous"), "query": (S, "song and artist to play")}, ["action"], {"query": "text"}),
    ("journal_add", "Personal diary only: feelings, thoughts, reflections. NOT for work done or things improved (use task_done or log_progress).",
     {"text": (S, "the entry")}, ["text"], {}),
    ("search_notes", "Search the user's OWN notes, papers, project docs and journal.",
     {"query": (S, "the question"), "source": (S, "notes, papers, projects or journal if said")}, ["query"],
     {"query": "text", "source": "topic"}),
    ("stats", "Progress and statistics.", {"topic": (S, "practice, chess, driving or tasks; empty for everything"), **TG}, [], {}),
    ("web_answer", "Answer a question that needs CURRENT information from the web (news, sports, prices, 'latest', 'today').",
     {"query": (S, "the question"), **TG}, ["query"], {}),
    ("save_note", "Save to a personal file: profile = lasting facts about the user; goals = targets; ideas = thoughts and "
     "project ideas; decisions = a choice the user made ('we decided', 'I chose', 'going with', 'decided not to'), "
     "with the reason if given. Not course/project facts (save_reference), dated tasks (task_add) or diary (journal_add).",
     {"file": (S, "profile, goals, ideas or decisions"),
      "text": (S, "the note in full, with 'it'/'that' replaced by what they mean; never the instruction itself"),
      "section": (S, "optional heading such as Yearly, Monthly, Weekly, Music; for decisions the project name")},
     ["file", "text"], {}),
    ("complete_goal", "Mark a goal in goals.md as done.", {"text": (S, "a few words from the goal")}, ["text"], {}),
    ("remove_note", "Delete an outdated or wrong line from profile, goals, ideas or decisions.",
     {"file": (S, "profile, goals, ideas or decisions"), "text": (S, "words from the line to delete")}, ["file", "text"], {}),
    ("log_progress", "Record finished work that matches NO open task (if one matches, use task_done). Use the exact name "
     "of the ongoing project it belongs to by meaning; new_project=true only for clearly new work. One sentence in English.",
     {"project": (S, "exact name of an ongoing project, or the new project's name"),
      "text": (S, "what was done and what is now possible"),
      "new_project": (B, "true only if this is a new project not in the ongoing list"),
      "no_matching_task": (B, "true only after you checked the open tasks and none matches")}, ["project", "text"], {}),
    ("practice_log", "Log a practice session the user ALREADY DID (past tense: 'I practiced', 'I played'). "
     "If the user says they need, want or plan to practice, that is NOT a session: use task_add instead. "
     "date is the real YYYY-MM-DD if it was not today ('yesterday' = yesterday's actual date).",
     {"minutes": (I, "duration in minutes, exactly as said"), "name": (S, "song, if said"), "instrument": (S, "piano or guitar"),
      "date": (S, "YYYY-MM-DD if not today"), "rating": (I, "self-rating 1-5 if said"), "focus": (S, "what was practiced"),
      "already_done": (B, "true only if the user said the practice already happened")},
     ["minutes", "already_done"], {}),
    ("practice_delete", "Remove wrongly logged practice sessions. Use when the user says a session was logged by mistake or did not happen. "
     "Removes the most recent ones (count), or the most recent ones of one date.",
     {"count": (I, "how many of the latest sessions to remove, default 1"),
      "date": (S, "YYYY-MM-DD, only if the user names a day")}, [], {}),
    ("read_note", "Read out the user's saved profile, goals, ideas or decisions, or answer questions about them "
     "('what are my goals', 'what did we decide about X').",
     {"file": (S, "profile, goals, ideas or decisions")}, ["file"], {}),
    ("birthday_lookup", "Tell when a specific person's birthday is. 'my birthday' means the user's own.",
     {"name": (S, "person's name, a relation such as 'brother', or 'me'")}, [], {}),
    ("save_reference", "Save information to a topic note (data/notes/<topic>.md): a course outline, lesson list, schedule, "
     "resources, how-to, checklist, AND facts about an ongoing course or project (its students, start date, rules), even "
     "short ones. Copy the user's text in full, keeping its lines. Not for finished work (log_progress).",     {"topic": (S, "the subject, used as the file name, e.g. 'IT course'"),
      "text": (S, "the user's full text, copied exactly with its line breaks")}, ["topic", "text"], {}),
    ("read_reference", "Read a saved reference note in full (course outline, lessons, resources). Use it when the user asks "
     "about the content of such a note.",
     {"topic": (S, "which note, e.g. 'IT course'; empty lists the saved notes")}, [], {}),
("query_data", "Answer a QUESTION about the user's stored data by querying it: tasks, practice and songs, finance, clients, "
 "chess games. Use for counts, totals, 'latest', 'how many', per-period or per-person questions. Not for saving.",
 {"question": (S, "the question in full"), "domain": (S, "finance, tasks, practice")}, ["question", "domain"], {}),

("smart_home", "Control the user's smart home devices (Yandex «Дом с Алисой»): lamps / lights on, off, brightness, "
 "warm or cold white, colour; action=list names the devices.",
 {"action": (S, "on, off, brightness, warm, cold, color or list"),
  "device": (S, "device name or room as the user said it; empty = all lights"),
  "value": (S, "brightness 1-100, or the colour name")}, ["action"], {}),
("bot_stats", "Answer ANY question about the user's own Telegram bots (Simple Quizzer, Simple Slides): new users, "
 "active users, files/quizzes/presentations made, payments, groups, per day/month/year, comparisons. Runs a read-only "
 "SQL query on the bots' database. Pass the question in full.",
 {"question": (S, "the user's question in full, with the period"),
  "bot": (S, "quizzer, slides, or empty for both")}, ["question"], {}),
("client_add", "Save an agency client's details (study, work, language courses in Germany) to the clients database and "
 "Google Sheet: a new client, OR new info for an existing one (it is matched by email, passport or name and updated). "
 "Partial details are fine: save them, never ask for missing ones. Copy the details exactly.",
 {"details": (S, "everything the user wrote about the client, copied exactly with line breaks")}, ["details"], {}),
("client_update", "Change an EXISTING agency client: mark progress steps done or not done, or set one field. Steps: "
 "1 Konsultatsiya, 3 Proton mail, 4 TLS akkaunt, 5 TLS terminni olib berish, 6 Til markazi to'lov + hujjatlari, "
 "7 Expatrio hisob ochish, 8 Expatrio hujjatlar, 9 CV / Motivatsion xat, 10 Xujjat tarjimalari, 11 Videx forma, "
 "12 Viza kutilmoqda.",
 {"name": (S, "the client's name as the user wrote it"),
  "steps": (S, "step numbers to change, comma separated, e.g. '4' or '4,5'; 'all' when the whole process is "
           "finished ('progress 11/11', 'got the visa', 'successfully completed'). '11/11' is a count, never step 11"),
  "done": (B, "true = done (default), false = not done"),
  "field": (S, "phone, email, dob, address, certificate, course, city, visa_applied, visa_answer, payment, comment or account. "
           "A visa that was received or approved goes in visa_answer, not visa_applied"),
  "value": (S, "the new value; for account: 'Service: login / password'")}, ["name"], {}),
("client_info", "Show one agency client's details, accounts and progress. Use for any question about a specific client.",
 {"name": (S, "the client's name")}, ["name"], {}),
("client_list", "List the agency's clients with their progress; with step, only clients where that step is not done "
 "yet ('who has no TLS account yet') or, with done=true, where it is done.",
 {"step": (S, "step number, optional"), "done": (B, "false (default) = step NOT done yet")}, [], {}),
("client_undo", "Undo the last change to the clients base (a new client or a changed step/field).", {}, [], {}),

("finance_report", "Any question about the user's money (revenue, income, expenses, spending, balance, purchases, "
 "transactions). Handled by code behind a PIN: never answer it yourself and never state finance numbers.",
 {"question": (S, "the user's question in full")}, ["question"], {}),
]



# Tools that WRITE data run one after another in the order requested; the rest can run in parallel.
SEQUENTIAL = {"add_birthday", "delete_birthday", "calendar_add", "calendar_undo", "remember", "song_add", "song_status", "practice_log",
              "practice_delete", "log_driving", "task_add", "task_done", "journal_add", "timer", "chess", "quiz", "mac",
              "volume", "music", "save_note", "complete_goal", "remove_note", "log_progress"}
SEQUENTIAL.update({"client_add", "client_update", "client_undo"})
SEQUENTIAL.update({"process_download", "clipboard", "telegram_send", "birthday_greeting", "save_reference", "read_reference"})
LANG_WORDS = {"english": "en", "russian": "ru", "uzbek": "uz"}
SPEC = {t[0]: t for t in TABLE}
_TYPE = {S: types.Type.STRING, I: types.Type.INTEGER, B: types.Type.BOOLEAN}


def declarations():
    out = []
    for name, desc, params, required, _ in {r[0]: r for r in TABLE}.values():
        kw = {"name": name, "description": desc}
        if params:
            kw["parameters"] = types.Schema(
                type=types.Type.OBJECT, required=required or None,
                properties={k: types.Schema(type=_TYPE[tp], description=d) for k, (tp, d) in params.items()})
        out.append(types.FunctionDeclaration(**kw))
    return out
