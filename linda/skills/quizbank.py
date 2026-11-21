"""Quiz banks in the format Linda already writes:  1. Question / + right / - wrong / - wrong ...
Drop .txt files into data/quizzes/ (e.g. driving theory questions)."""
import collections, hashlib, random, re

Q = collections.namedtuple("Q", "bank text right wrong h")
START = re.compile(r"^\s*\d+[.)]\s*(.*)")
NUM = {1: ["1", "one", "first", "bir", "birinchi", "один", "раз", "первый"],
       2: ["2", "two", "second", "ikki", "ikkinchi", "два", "второй"],
       3: ["3", "three", "third", "uch", "учинчи", "три", "третий"],
       4: ["4", "four", "fourth", "to'rt", "tort", "четыре", "четвертый"]}


def parse(text, bank):
    out, cur, where = [], None, None

    def flush():
        if cur and cur["q"] and cur["right"] and cur["wrong"] and cur["right"] != "???":
            out.append(Q(bank, cur["q"], cur["right"], cur["wrong"],
                         hashlib.md5(cur["q"].encode("utf-8")).hexdigest()[:10]))

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        m = START.match(s)
        if m:
            flush()
            cur, where = {"q": m.group(1), "right": "", "wrong": []}, "q"
        elif cur is None:
            continue
        elif s.startswith("+"):
            cur["right"], where = s[1:].strip(), "r"
        elif s.startswith("-"):
            cur["wrong"].append(s[1:].strip()); where = "w"
        elif where == "q":
            cur["q"] += " " + s
        elif where == "r":
            cur["right"] += " " + s
        elif where == "w" and cur["wrong"]:
            cur["wrong"][-1] += " " + s
    flush()
    return out


def load_banks(folder):
    banks = {}
    if folder.exists():
        for p in sorted(folder.glob("*.txt")):
            qs = parse(p.read_text(encoding="utf-8", errors="replace"), p.stem)
            if qs:
                banks[p.stem] = qs
    return banks


def pick(store, banks, n, only=None):
    """Weighted: questions you got wrong last time first, then unseen, then known ones."""
    pool = [q for b, qs in banks.items() if not only or only.lower() in b.lower() for q in qs]
    last = dict(store.quiz_last_all())
    weight = lambda q: 3.0 if last.get(q.h) == 0 else (2.0 if q.h not in last else 0.5)
    chosen = []
    while pool and len(chosen) < n:
        q = random.choices(pool, weights=[weight(x) for x in pool])[0]
        chosen.append(q)
        pool.remove(q)
    return chosen


def parse_choice(text, n):
    t = text.lower()
    for k, words in NUM.items():
        if k <= n and any(re.search(rf"(?<!\w){re.escape(w)}(?!\w)", t) for w in words):
            return k
    return None
