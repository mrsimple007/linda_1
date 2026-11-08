"""Latest-download handling: find, read (docx tables incl. bold/highlight hints), save .txt."""
import mimetypes, pathlib, re
from google.genai import types

SKIP = {".crdownload", ".part", ".download", ".tmp", ".opdownload"}


class UnsupportedFile(Exception):
    pass


def latest_download(folder):
    d = pathlib.Path(folder).expanduser()
    files = [p for p in d.iterdir()
             if p.is_file() and not p.name.startswith(".") and p.suffix.lower() not in SKIP]
    return max(files, key=lambda p: p.stat().st_mtime, default=None)


def _runs(p):
    """Paragraph text with markers: **bold**, ==highlight==, ^^colored^^ (answers are often marked so)."""
    out = []
    for r in p.runs:
        s = r.text
        if not s.strip():
            out.append(s)
            continue
        color = r.font.color.rgb if r.font.color is not None and r.font.color.type is not None else None
        if r.bold:
            s = f"**{s}**"
        elif r.font.highlight_color is not None:
            s = f"=={s}=="
        elif color is not None and str(color) not in ("000000", "auto"):
            s = f"^^{s}^^"
        out.append(s)
    return "".join(out)


def docx_to_text(path):
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    doc = Document(str(path))
    lines = []
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            lines.append(_runs(Paragraph(child, doc)))
        elif child.tag.endswith("}tbl"):
            for row in Table(child, doc).rows:
                cells, seen = [], set()
                for c in row.cells:
                    if id(c._tc) in seen:        # merged cells repeat
                        continue
                    seen.add(id(c._tc))
                    cells.append(" ".join(_runs(p) for p in c.paragraphs).strip())
                lines.append(" | ".join(cells))
            lines.append("")
    return "\n".join(lines)


def prepare_input(path):
    s = path.suffix.lower()
    if s == ".docx":
        return [docx_to_text(path)]
    if s in (".txt", ".md", ".csv", ".json", ".html"):
        return [path.read_text(errors="replace")]
    mime = mimetypes.guess_type(path.name)[0] or ""
    if mime == "application/pdf" or mime.startswith("image/"):
        return [types.Part.from_bytes(data=path.read_bytes(), mime_type=mime)]
    raise UnsupportedFile(path.suffix)


def split_title(output, fallback):
    out = re.sub(r"^```\w*\n|\n```\s*$", "", output.strip())
    first, _, rest = out.partition("\n")
    if first.upper().startswith("TITLE:"):
        return first[6:].strip() or fallback, rest.strip()
    return fallback, out


def save_text(folder, title, content):
    d = pathlib.Path(folder).expanduser()
    stem = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title).strip(" .")[:80] or "Linda output"
    p, i = d / f"{stem}.txt", 2
    while p.exists():
        p, i = d / f"{stem} ({i}).txt", i + 1
    p.write_text(content + "\n", encoding="utf-8")
    return p
