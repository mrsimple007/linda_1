# Projects — Project Knowledge Base

## Purpose

This folder contains the goals, architecture, technical decisions, setup instructions, progress, and TODOs for my personal and professional projects.

Linda should use these notes as the source of truth when answering questions about my projects. It should help me resume work after a break, remember why I made technical decisions, identify unfinished tasks, and avoid repeating work.

## Instructions for Linda

When I ask about a project:

1. Search the relevant project folder and notes before answering.
2. Distinguish confirmed decisions from ideas, suggestions, and unresolved questions.
3. Explain why a technical decision was made when that information is available.
4. Check TODOs and the latest progress notes before recommending next steps.
5. If two notes conflict, prefer the most recent explicitly confirmed decision and flag the conflict when necessary.
6. Never invent project status, implementation details, credentials, benchmarks, or completed tasks.
7. If information is missing, tell me what is unknown rather than assuming.
8. When I make a new decision, help me record it with its date, rationale, alternatives, and consequences.
9. When I finish a task, update the project status and outstanding TODOs.
10. When useful, connect related projects, reusable components, and technical lessons.

## Project File Structure

Each substantial project should have its own folder.

Recommended structure:

- `README.md` — overview, goals, current status, and quick links.
- `ARCHITECTURE.md` — system design, components, data flow, and integrations.
- `DECISIONS.md` — important technical and product decisions, including rationale.
- `SETUP.md` — environment setup, dependencies, configuration, and run instructions.
- `TODO.md` — prioritized tasks, blockers, and next steps.
- `PROGRESS.md` — dated development updates, completed milestones, experiments, and results.
- `NOTES.md` — ideas, debugging discoveries, research notes, and miscellaneous information.

Keep small projects simple. Not every project needs every file.

## Project Index

Maintain an index of active, paused, completed, and archived projects.

### Personal AI Assistant — JARVIS / Linda
- Goal: Build a personal AI assistant that works locally on my MacBook Pro M1 Pro with 16 GB RAM, with optional cloud models.
- Planned capabilities: voice interaction, persistent memory, project search, daily planning, reminders, piano practice, chess analysis, driving-license preparation, and a searchable second brain.
- Important considerations: local privacy, modular architecture, low resource usage, reliable memory, and approval before consequential actions.
- Status: Initial architecture and starter implementation.
- Next steps: establish the core assistant, persistent memory, searchable notes, project-aware retrieval, and scheduled morning/evening routines.

### MenVaSen
- Goal: Build a multilingual couples app for the Uzbekistan market.
- Main technologies: Flutter, Supabase, PostgreSQL, Row Level Security, Realtime, Storage, and Edge Functions.
- Planned features: shared savings, wishlists, loyalty cards, document storage, shared tasks, and time-capsule notes.
- Track: product requirements, database schema, authentication, security policies, UI decisions, and release milestones.

### Educational AI Bots
- Goal: Develop and maintain AI-powered educational Telegram bots.
- Relevant products: SimpleQuizzer, SimpleSlides, SimpleStudy, and SimpleLearn AI.
- Main technologies: Python, LLM APIs, asynchronous processing, Supabase, and external AI services.
- Track: users, generated files, reliability, costs, feature requests, deployment, and growth experiments.
- Keep separate technical notes for each bot when their architectures or requirements differ.

### Bellissimo ML Systems
- Goal: Develop and maintain production machine-learning systems for restaurant operations.
- Relevant systems: pizza quality scoring, product recommendations, sales prediction, pizza size/type detection, and cooking-time prediction.
- Technologies used across projects include Python, FastAPI, Kafka, Docker, Kubernetes, MLflow, ONNX Runtime, object storage, and monitoring tools.
- Track: model versions, datasets, evaluation metrics, deployment architecture, API contracts, experiments, monitoring, and measurable business impact.
- Keep each ML system in its own project folder where appropriate.

### Personal Local AI / Second Brain
- Goal: Build a searchable knowledge base for notes, research papers, project documentation, and selected personal work notes.
- Track: ingestion, document parsing, embeddings, retrieval, citations, privacy, and integrations with the main assistant.
- Important distinction: retrieved information should be traceable to its source.

## How to Record Decisions

Use this format in `DECISIONS.md`:

### [YYYY-MM-DD] Decision: [Short title]

- **Status:** Proposed / Confirmed / Superseded
- **Context:** What problem needed to be solved?
- **Decision:** What did I choose?
- **Reasoning:** Why did I choose it?
- **Alternatives:** What else did I consider?
- **Consequences:** What does this choice affect?
- **Revisit when:** What would justify changing the decision?

Only mark a decision as confirmed when I have actually approved it.

## How to Record Progress

Use this format in `PROGRESS.md`:

### [YYYY-MM-DD] — [Milestone or update]

- **Completed:** What was implemented or verified?
- **Results:** What tests, metrics, or observations support the result?
- **Problems:** What remains broken or uncertain?
- **Decisions:** Any decisions made today.
- **Next steps:** The most important tasks to do next.

## How to Maintain TODOs

Organize tasks by priority:

- **P0 — Critical:** Blocking issues or essential functionality.
- **P1 — High:** Important work for the next milestone.
- **P2 — Medium:** Useful improvements.
- **P3 — Low:** Optional enhancements and ideas.

For each task, include its status, dependencies, and acceptance criteria when useful. Distinguish planned work from work that has actually been completed.

## Example Questions I May Ask Linda

- "What did I decide about the database for MenVaSen?"
- "Why did I choose ONNX Runtime for the pizza quality scorer?"
- "What are the next three tasks for JARVIS?"
- "Which projects are currently blocked?"
- "What model evaluation results did I record?"
- "Did I already try a different architecture for this feature?"
- "Summarize what changed in this project over the last month."
- "Which TODOs have been open the longest?"
- "Compare the architecture of two of my projects."
- "Find the original note explaining why I chose this approach."

## General Rule

Project notes should help me reconstruct not only what I built, but also why I built it that way.

Preserve important reasoning, dates, actual results, and unresolved questions. Prefer concise, accurate, searchable notes over long descriptions that become outdated.
