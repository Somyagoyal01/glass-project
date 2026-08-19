"""
Glass LLM layer — Groq backend (free tier, no credit card needed).

Same design principle as before: the LLM never invents relevance. It only:
1) extracts search keywords from the user's task description
2) narrates a plan given ALREADY-COMPUTED deterministic evidence (analyzer.py)
3) generates an actual code diff, but only for files the user approved
"""
import json
from groq import Groq

MODEL = "openai/gpt-oss-120b"

client = Groq()  # reads GROQ_API_KEY from env


def _chat(prompt: str, max_tokens: int = 800) -> str:
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.choices[0].message.content.strip()


def extract_keywords(task: str) -> list:
    prompt = f"""Extract 4-8 short search keywords/terms (single words or short phrases,
lowercase, no punctuation) that would help locate relevant code files for this task.
Return ONLY a JSON array of strings, nothing else, no markdown fences.

Task: {task}"""
    text = _chat(prompt, max_tokens=200)
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        kws = json.loads(text)
        return [str(k) for k in kws][:8]
    except (json.JSONDecodeError, ValueError):
        return [w.strip(".,!?").lower() for w in task.split() if len(w) > 3][:8]


def narrate_plan(task: str, scored_files: list, top_n: int = 6) -> list:
    top = scored_files[:top_n]
    evidence_block = "\n".join(
        f"- {f['path']} [{f['tier']}, score={f['score']:.1f}]: "
        + "; ".join(e['detail'] for e in f['evidence'][:4])
        for f in top
    )
    prompt = f"""You are Glass, an AI coding agent that must justify its plan using ONLY the
evidence provided below. Do not invent facts about files you don't have evidence for.

Task: {task}

Deterministically-computed relevance evidence (from static analysis, not from you):
{evidence_block}

Write a step-by-step execution plan as a JSON array. Each step:
{{"file": "<path from the evidence above>", "action": "inspect" | "modify" | "add_test",
  "why": "<one sentence, grounded in the evidence given, plain language>"}}

Rules:
- Only reference files listed in the evidence above.
- Order steps logically (inspect before modify; tests last).
- 3 to 6 steps total. Return ONLY the JSON array, no markdown fences."""
    text = _chat(prompt, max_tokens=800)
    text = text.replace("```json", "").replace("```", "").strip()
    try:
        steps = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        steps = []
    valid_paths = {f["path"] for f in top}
    out = []
    for i, s in enumerate(steps):
        if s.get("file") not in valid_paths:
            continue
        out.append({
            "order": i + 1,
            "file": s["file"],
            "action": s.get("action", "inspect"),
            "why": s.get("why", ""),
        })
    return out


def generate_diff(task: str, file_path: str, file_content: str) -> str:
    prompt = f"""Task: {task}

You are editing this file: {file_path}

Current content:
---
{file_content[:8000]}
---

Propose the minimal change needed to address the task. Respond in this exact format:

RATIONALE: <one paragraph>
---NEW_FILE---
<the full new file content>"""
    return _chat(prompt, max_tokens=4000)
