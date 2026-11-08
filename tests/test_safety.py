"""Permission and audit regression checks. Run: python tests/test_safety.py"""
import pathlib, sys, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from linda.agent import Agent
from linda.router import Intent
from linda.safety import permission_tier, request_is_supported
from linda.store import Store


class Speaker:
    def __init__(self):
        self.said = []

    def say(self, text, lang="en"):
        self.said.append((lang, text))


class HallucinatingRouter:
    def route(self, text):
        return Intent(intent="process_download", instruction="do something with newest file",
                      preset="internet_connection")


def main():
    store = Store(str(pathlib.Path(tempfile.mkdtemp()) / "audit.db"))
    agent = Agent.__new__(Agent)
    agent.store, agent.speaker = store, Speaker()
    agent.lang, agent.pending_actions = "en", []

    ran = []
    assert permission_tier("time") == 0
    assert permission_tier("new_unclassified_tool") == 2
    assert permission_tier("calendar_add") == 2
    assert permission_tier("delete_birthday") == 3
    assert not request_is_supported("process_download", "Can you help me with possible ideas?")
    assert request_is_supported("process_download", "Convert my latest download to a quiz")
    assert not request_is_supported("timer", "Say confirm within 90 seconds")
    assert request_is_supported("timer", "Set a timer for 90 seconds")
    assert not request_is_supported("morning", "Can you help me?")
    assert request_is_supported("morning", "Good morning")
    assert not agent.run_action("telegram_send", {"text": "hello"},
                                lambda: ran.append("sent"))
    assert ran == []
    assert agent.handle_confirmation("yes")
    assert ran == []
    assert agent.handle_confirmation("confirm")
    assert ran == ["sent"]

    assert not agent.run_action("delete_birthday", {"name": "A"},
                                lambda: ran.append("deleted"))
    assert ran == ["sent"]
    assert agent.handle_confirmation("confirm delete")
    assert ran == ["sent", "deleted"]
    assert not agent.run_action("clipboard", {}, lambda: ran.append("copied"))
    assert agent.handle_confirmation("I heard say confirm within 90 seconds")
    assert ran == ["sent", "deleted"]
    assert not agent.pending_actions
    assert agent.handle_confirmation("cancel") is False
    agent.cfg, agent.router = {"presets": {}}, HallucinatingRouter()
    agent.chess = agent.quiz = None
    agent.pending_review = 0
    agent.run("Can you help me with possible ideas?")
    assert not agent.pending_actions

    entries = store.q("SELECT tool,arguments,result,confirmed_by FROM audit_log ORDER BY id")
    assert [(row[0], row[2], row[3]) for row in entries] == [
        ("telegram_send", "confirmation_required", None),
        ("telegram_send", "completed", "user"),
        ("delete_birthday", "confirmation_required", None),
        ("delete_birthday", "completed", "user"),
        ("clipboard", "confirmation_required", None),
        ("clipboard", "superseded_by_new_request", None),
        ("process_download", "rejected_unsubstantiated", None),
    ]
    assert '"hello"' in entries[0][1] and '"A"' in entries[2][1]

    print("Safety and audit checks passed")


if __name__ == "__main__":
    main()
