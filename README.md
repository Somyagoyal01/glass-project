# Glass — see the agent's plan before it changes your code

## Live links

- **Deployed app**: https://glass-project-lmjrgadds-somya9.vercel.app
- **GitHub repo**: https://github.com/Somyagoyal01/glass-project
- **Backend (Render)**: https://glass-project-z596.onrender.com

## Note on LLM provider

This was originally built and tested against Anthropic's Claude API. Partway through, I hit a billing constraint — my Anthropic account didn't have credits available in the assignment window — so I switched the LLM layer to Groq (`openai/gpt-oss-120b`) instead of stalling on it.

The switch only touched `llm.py`. The architecture and the core design decision — evidence computed deterministically in `analyzer.py`, LLM strictly downstream narrating a plan from that evidence — didn't change at all, because that separation is what made the swap possible without touching anything else. I think this is actually a reasonable thing to have happened during a real assignment: a dependency wasn't available, so I adapted the implementation without changing the design.


## What I built and why

Glass is an inspectability layer for AI coding agents. Before any file gets
touched, it shows you: which files it thinks are relevant, the concrete
evidence for each one, and an ordered plan you approve step by step.

This came out of exploring Superbrain directly. Superbrain's own positioning
is built around approval — "no file changes without approval" — and around
its context engine (TokenFold) continuously mapping architecture and
dependencies. But that mapping is internal. As a user, you approve a diff at
the end; you don't see the reasoning that got there, and you can't tell if
"90% confident" (the kind of number these tools often show) is a real signal
or a decoration. I wanted to build the version of approval that happens
*before* execution, grounded in evidence you can actually check.

## Architecture

**Backend (FastAPI, Python)**
- `analyzer.py` — fully deterministic. Clones a repo, parses imports (Python
  `ast`, regex-based for JS/TS), builds a dependency graph with `networkx`,
  and scores every file against task keywords using: keyword hits in
  content/path, import-graph proximity, in-degree (structural importance),
  and test-file references. No LLM involved in this file at all.
- `llm.py` — narrowly scoped. The LLM does exactly two things: (1) extracts
  search keywords from the free-text task description, and (2) writes the
  plan narration and ordering *given* the evidence `analyzer.py` already
  computed — it cannot introduce a file that isn't in the evidence set, and
  the "why" is checked against real evidence strings, not invented.
  A third function generates the actual proposed diff, but only for files
  the user approved.
- `main.py` — three endpoints: `/api/analyze` (clone + score + plan),
  `/api/execute` (generate diffs for approved files only), `/api/session/{id}`
  (cleanup).

**Frontend** — single static HTML/JS file (no build step), calls the backend
directly. Three-stage UI: evidence → plan → approved changes.

## Key design decision: separating evidence from narration

The single most important decision was refusing to let the LLM be the source
of relevance. Confidence scores from an LLM alone are decoration — "90%
according to what?" So relevance tiers (HIGH/MEDIUM/LOW) come entirely from
`analyzer.py`'s deterministic scoring. The LLM's job is strictly downstream:
turn already-computed evidence into a readable plan. If you disagree with a
plan step, you can trace it back to a specific import edge or keyword match,
not a black-box justification.

I also deliberately did not build a "reasoning trace" that exposes the
model's private chain-of-thought — that's both technically dishonest (you
can't faithfully expose that) and not actually what makes an agent
trustworthy. What makes it trustworthy is an auditable plan with checkable
evidence, which is what this exposes instead.

## Scope decisions (what I cut, and why)

- **Languages**: Python and JS/TS only for v1. Supporting every language's
  import syntax properly is a real engineering project on its own; scoping
  down was a conscious tradeoff to ship something solid rather than
  shallow support for everything.
- **Execution**: proposed diffs are generated but not auto-written to a live
  repo or pushed — this is a plan/diff preview tool, not a full agent
  runtime. Extending it to actually apply approved changes to a branch and
  open a PR is the natural next step (see below).
- **No replay/timeline feature** (originally scoped) — cut to keep Plan
  Preview + Evidence + Approval solid rather than spreading thin.
- **Sessions are in-memory** — fine for a demo; would move to Redis/Postgres
  for anything real.

## What I'd change or add next in Superbrain (Q3A)

Expose the context engine's own evidence, the way this project does. If
TokenFold is already computing dependency/architecture maps internally to
decide what to compress, surfacing that map — even a simplified version —
before a task runs would let users catch a wrong context selection before
wasting a run on it, not just review the resulting diff.

## UI issues I'd flag (Q3B)

Approval currently centers on the *code change*, not the *plan that produced
it*. By the time a user sees a diff, the agent has already spent tokens and
time exploring in a direction that may be wrong. Moving the approval/veto
point earlier — to the plan, not just the output — is a small UI change with
a large trust payoff, especially on large or unfamiliar codebases where the
user can't eyeball-verify a diff is complete.

## Running locally

```bash
# backend
cd backend
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-...
uvicorn main:app --reload --port 8000

# frontend — just open frontend/index.html, or serve it:
cd frontend
python3 -m http.server 5500
```

Set `window.GLASS_API_BASE` in `index.html` (or via a small inline script) if
your backend isn't at `localhost:8000`.

## Deployment

- **Frontend**: static site, deploy `frontend/` directly to Vercel.
- **Backend**: FastAPI needs a persistent process (repo cloning + in-memory
  sessions don't fit serverless well) — deploy to Render/Railway/Fly, then
  point the frontend's `GLASS_API_BASE` at that URL.
