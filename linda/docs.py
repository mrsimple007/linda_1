"""Semantic search over YOUR notes, papers, project docs and journal (local embeddings via Ollama)."""
import collections, pathlib, re
import numpy as np
import ollama

EXT = {".md", ".txt", ".pdf", ".docx"}
Hit = collections.namedtuple("Hit", "score path kind text")


def chunk(text, size=900, overlap=150):
    chunks, cur = [], ""
    for p in (x.strip() for x in re.split(r"\n\s*\n", text) if x.strip()):
        if len(cur) + len(p) + 2 <= size:
            cur = f"{cur}\n\n{p}" if cur else p
            continue
        if cur:
            chunks.append(cur)
        if len(p) > size:
            chunks += [p[i:i + size] for i in range(0, len(p), size - overlap)]
            cur = ""
        else:
            cur = p
    if cur:
        chunks.append(cur)
    return chunks


class Docs:
    def __init__(self, store, cfg, root):
        self.store, self.model, self.folders = store, cfg["embed_model"], cfg["folders"]
        self.root, self.cache = pathlib.Path(root).expanduser(), None
        self.on_hits = None          # callback(hits) used by the UI galaxy

    def _read(self, p):
        s = p.suffix.lower()
        if s in (".md", ".txt"):
            return p.read_text(encoding="utf-8", errors="replace")
        if s == ".pdf":
            from pypdf import PdfReader
            return "\n\n".join((pg.extract_text() or "") for pg in PdfReader(str(p)).pages)
        from .files import docx_to_text
        return docx_to_text(p)

    def _embed(self, texts, prefix):
        out = []
        for i in range(0, len(texts), 16):
            out += ollama.embed(model=self.model, input=[prefix + t for t in texts[i:i + 16]]).embeddings
        a = np.array(out, dtype=np.float32)
        return a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)

    def index_file(self, p, kind):
        p = pathlib.Path(p)
        parts = chunk(self._read(p))
        emb = self._embed(parts, "search_document: ") if parts else []
        st = p.stat()
        with self.store.lock:
            self.store.x("DELETE FROM chunks WHERE path = ?", (str(p),))
            self.store.xmany("INSERT INTO chunks(path,kind,idx,text,emb) VALUES (?,?,?,?,?)",
                             [(str(p), kind, i, c, e.tobytes()) for i, (c, e) in enumerate(zip(parts, emb))])
            self.store.x("INSERT OR REPLACE INTO doc_files VALUES (?,?,?,?)", (str(p), kind, st.st_mtime, st.st_size))
        self.cache = None

    def index(self):
        """Incremental: only new or changed files are (re)embedded."""
        seen, n = set(), 0
        for kind in self.folders:
            for p in sorted((self.root / kind).rglob("*")):
                if not p.is_file() or p.suffix.lower() not in EXT or p.name.startswith("."):
                    continue
                seen.add(str(p))
                st = p.stat()
                row = self.store.q("SELECT mtime, size FROM doc_files WHERE path = ?", (str(p),))
                if row and row[0][0] == st.st_mtime and row[0][1] == st.st_size:
                    continue
                try:
                    self.index_file(p, kind)
                    n += 1
                except Exception as e:
                    print(f"[index] {p.name}: {e}")
        for (path,) in self.store.q("SELECT path FROM doc_files"):
            if path not in seen:
                self.store.x("DELETE FROM chunks WHERE path = ?", (path,))
                self.store.x("DELETE FROM doc_files WHERE path = ?", (path,))
                self.cache = None
        return n

    def search(self, query, k=5, kind=None, min_score=0.3):
        if self.cache is None:
            rows = self.store.q("SELECT path, kind, text, emb FROM chunks")
            meta = [(r[0], r[1], r[2]) for r in rows]
            mat = np.vstack([np.frombuffer(r[3], dtype=np.float32) for r in rows]) if rows else None
            self.cache = (meta, mat)
        meta, mat = self.cache
        if mat is None:
            return []
        scores = mat @ self._embed([query], "search_query: ")[0]
        hits = []
        for i in np.argsort(-scores):
            if scores[i] < min_score or len(hits) >= k:
                break
            if kind and meta[i][1] != kind:
                continue
            hits.append(Hit(float(scores[i]), meta[i][0], meta[i][1], meta[i][2]))
        if self.on_hits and hits:
            try:
                self.on_hits(hits)
            except Exception:
                pass
        return hits

    def graph(self, neighbors=2):
        """Notes galaxy: one node per file, links between the most similar files."""
        rows = self.store.q("SELECT path, kind, emb FROM chunks")
        by = {}
        for path, kind, emb in rows:
            by.setdefault(path, (kind, []))[1].append(np.frombuffer(emb, dtype=np.float32))
        if not by:
            return {"nodes": [], "links": []}
        paths = list(by)
        cent = np.vstack([np.mean(v[1], axis=0) for v in by.values()])
        cent /= (np.linalg.norm(cent, axis=1, keepdims=True) + 1e-9)
        sim, links = cent @ cent.T, set()
        for i in range(len(paths)):
            for j in np.argsort(-sim[i])[1:neighbors + 1]:
                if sim[i, j] > 0.2:
                    links.add((min(i, int(j)), max(i, int(j))))
        return {"nodes": [{"id": p, "label": pathlib.Path(p).stem, "kind": by[p][0], "size": len(by[p][1])} for p in paths],
                "links": [{"source": paths[i], "target": paths[j], "w": round(float(sim[i, j]), 2)} for i, j in sorted(links)]}