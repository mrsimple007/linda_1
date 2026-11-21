"""Connections: when something is saved, find the existing notes / projects / files closest in meaning
(local embeddings, docs.py) and link them. Topic notes keep a 'Related:' line; the Telegram reply shows the links."""
import pathlib, re

MIN_SCORE = {True: 0.68, False: 0.6}   # nomic-embed-text: unrelated ~0.55-0.62; bge-m3: unrelated ~0.45-0.55
NEAR_TOP = 0.08           # keep only links close to the best one


def _real(agent, path):
    """notes/root_goals.md is a search copy of data/goals.md: link the real file."""
    p = pathlib.Path(path)
    if p.name.startswith("root_"):
        return agent.data_dir / p.name[5:]
    return p


def related(agent, text, exclude=(), k=3):
    """-> [Path] of up to k saved files about the same thing (never chat logs or the file just written)."""
    docs = getattr(agent, "docs", None)
    text = " ".join(str(text or "").split())
    if docs is None or len(text) < 12:
        return []
    skip = {pathlib.Path(x).name for x in exclude if x}
    out, top = [], None
    try:
        for h in docs.search(text[:2000], 15, None, 0.0):
            p = _real(agent, h.path)
            if p.name.startswith("activity-") or p.name in skip or p.name in ("tasks.md", "README.md"):
                continue
            top = h.score if top is None else top
            if h.score < MIN_SCORE[bool(getattr(docs, "nomic", True))] or h.score < top - NEAR_TOP:
                break
            if p not in out and p.exists():
                out.append(p)
            if len(out) >= k:
                break
    except Exception as e:
        print("[links]", repr(e)[:120])
    return out


def annotate(agent, path, links):
    """Keep one 'Related: ...' line at the end of a topic note (data/notes/<topic>.md)."""
    p = pathlib.Path(path)
    if not links or p.parent != agent.data_dir / "notes" or p.name.startswith("root_") or not p.exists():
        return
    names = [x.relative_to(agent.data_dir).as_posix() if x.is_relative_to(agent.data_dir) else x.name for x in links]
    body = p.read_text(encoding="utf-8")
    old = re.search(r"\n*Related: (.+)\n*$", body)
    have = [x.strip() for x in old.group(1).split("·")] if old else []
    merged = list(dict.fromkeys(have + names))[:8]
    body = (body[:old.start()] if old else body).rstrip() + "\n\nRelated: " + " · ".join(merged) + "\n"
    p.write_text(body, encoding="utf-8")
