"""Run once:  python setup_data.py
Creates the data/ folders and starter files. It never overwrites anything that already exists.

  data/songs.yaml        your repertoire (edit this!)
  data/quizzes/          driving-theory (or any) question banks, in the  1. / + / -  format
  data/notes/            your notes (.md .txt .pdf .docx)
  data/papers/           research papers (PDF)
  data/projects/         project documentation
  data/journal/          daily entries (Linda also writes here: reviews, 'add to journal')"""
import datetime, pathlib, shutil

ROOT = pathlib.Path(__file__).resolve().parent
D = ROOT / "data"

FILES = {
    "songs.yaml": '''# Your repertoire. Linda imports this at every start (new songs are added, edited ones updated).
# status: learning | polishing | repertoire | archived      priority: 1 (low) .. 3 (high)
# melody (optional): single-note line, e.g. "E5 D#5 E5". Lets Linda check your notes when she listens.
# target_bpm (optional): lets Linda compare your tempo (assumes one note per beat in the melody).
songs:
  - {title: "Fur Elise", artist: "Beethoven", instrument: piano, status: learning, priority: 3, target_bpm: 100, melody: "E5 D#5 E5 D#5 E5 B4 D5 C5 A4"}
  - {title: "Blackbird", artist: "The Beatles", instrument: guitar, status: polishing, priority: 2, target_bpm: 94}
  - {title: "C major scale", instrument: piano, status: repertoire, priority: 1, target_bpm: 80, melody: "C4 D4 E4 F4 G4 A4 B4 C5"}
''',
    "quizzes/driving_theory_SAMPLE.txt": '''1. What does a solid red traffic light require a driver to do?
+ Stop before the stop line
- Slow down and continue if the road is clear
- Speed up to clear the intersection
- Flash the headlights and continue

2. When must you use the turn signal?
+ Before turning or changing lanes
- Only at night
- Only on highways
- Only when other cars are close

3. What should you do when an emergency vehicle with a siren approaches from behind?
+ Pull over safely and give way
- Speed up
- Stop in the middle of the lane
- Ignore it if you have the right of way

(SAMPLE: replace this file with your real questions. Use Linda: "convert my latest download to a quiz",
then move the .txt from Downloads into this folder. File name = topic name, e.g. road_signs.txt.)
''',
    "notes/welcome.md": '''# Notes
Put anything here: lesson notes, practice tips from your teacher, ideas. Linda indexes these files and
can answer "what did I write about fingering in Fur Elise?".
''',
    "projects/README.md": '''# Projects
One file or folder per project: goals, decisions, setup steps, TODOs. Example question later:
"Linda, what did I decide about the database for project X?"
''',
    "papers/README.txt": "Drop research papers (PDF) here. Linda embeds them locally.\n",
    f"journal/{datetime.date.today()}.md": f"- {datetime.datetime.now():%H:%M} Set up Linda's data folders.\n",
}

for rel, content in FILES.items():
    p = D / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(content, encoding="utf-8")
        print("created", p.relative_to(ROOT))
for folder in ("notes", "papers", "projects", "journal", "quizzes"):
    (D / folder).mkdir(parents=True, exist_ok=True)
env, ex = ROOT / ".env", ROOT / ".env.example"
if not env.exists() and ex.exists():
    shutil.copy(ex, env)
    print("created .env (fill in your keys)")
print("\nNext: edit data/songs.yaml, set goals.driving_test_date in config.yaml, then: python main.py")
