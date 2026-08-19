import uuid
from pathlib import Path
from dataclasses import asdict

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import analyzer
import llm

app = FastAPI(title="Glass API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# in-memory session store (MVP scope — fine for a demo, not for prod)
SESSIONS: dict = {}


class AnalyzeRequest(BaseModel):
    github_url: str
    task: str


class ApproveRequest(BaseModel):
    session_id: str
    approved_files: list  # list of file paths the user approved


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    try:
        root = analyzer.clone_repo(req.github_url)
        analyzer.detect_primary_language(root)
        graph, file_texts, test_files = analyzer.build_dependency_graph(root)
        keywords = llm.extract_keywords(req.task)
        scored = analyzer.score_files(graph, file_texts, test_files, keywords)
    except analyzer.UnsupportedRepoError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except analyzer.RepoTooLargeError as e:
        raise HTTPException(status_code=413, detail=str(e))

    scored_dicts = [
        {
            "path": fs.path,
            "tier": fs.tier,
            "score": fs.score,
            "is_test": fs.is_test,
            "evidence": [{"kind": e.kind, "detail": e.detail} for e in fs.evidence],
        }
        for fs in scored[:20]
    ]

    plan = llm.narrate_plan(req.task, scored_dicts)

    session_id = str(uuid.uuid4())
    SESSIONS[session_id] = {
        "root": str(root),
        "file_texts": file_texts,
        "task": req.task,
    }

    return {
        "session_id": session_id,
        "keywords": keywords,
        "files": scored_dicts,
        "plan": plan,
    }


@app.post("/api/execute")
def execute(req: ApproveRequest):
    session = SESSIONS.get(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired.")

    results = []
    for path in req.approved_files:
        content = session["file_texts"].get(path)
        if content is None:
            results.append({"file": path, "error": "file not found in session"})
            continue
        raw = llm.generate_diff(session["task"], path, content)
        rationale, _, new_file = raw.partition("---NEW_FILE---")
        results.append({
            "file": path,
            "rationale": rationale.replace("RATIONALE:", "").strip(),
            "new_content": new_file.strip(),
            "old_content": content,
        })
    return {"results": results}


@app.delete("/api/session/{session_id}")
def cleanup_session(session_id: str):
    session = SESSIONS.pop(session_id, None)
    if session:
        analyzer.cleanup(Path(session["root"]))
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"status": "ok"}
