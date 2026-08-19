"""
Glass static analysis engine.

Fully deterministic — no LLM calls happen in this file. This module answers:
"given a repo and some keywords, which files are relevant, and why?"

The output is a list of Evidence objects, each with concrete, checkable facts
(imported-by counts, keyword hit locations, test references) so the relevance
tier is never "the AI just said so."
"""
import ast
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import networkx as nx

SUPPORTED_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx"}
IGNORE_DIRS = {
    "node_modules", ".git", "__pycache__", "venv", ".venv", "dist",
    "build", ".next", "site-packages", "vendor", "coverage",
}
MAX_FILES = 4000
MAX_FILE_BYTES = 300_000
CLONE_TIMEOUT_SECONDS = 60


@dataclass
class Evidence:
    kind: str          # e.g. "import", "keyword", "test_reference"
    detail: str         # human-readable evidence line


@dataclass
class FileScore:
    path: str
    tier: str            # HIGH / MEDIUM / LOW
    score: float
    evidence: list = field(default_factory=list)
    is_test: bool = False


class RepoTooLargeError(Exception):
    pass


class UnsupportedRepoError(Exception):
    pass


def clone_repo(github_url: str) -> Path:
    """Shallow-clone a repo into a temp dir. Raises on failure/timeout."""
    workdir = Path(tempfile.mkdtemp(prefix="glass_"))
    try:
        subprocess.run(
            ["git", "clone", "--depth", "1", github_url, str(workdir)],
            check=True,
            timeout=CLONE_TIMEOUT_SECONDS,
            capture_output=True,
        )
    except subprocess.CalledProcessError as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise UnsupportedRepoError(f"Could not clone repo: {e.stderr.decode(errors='ignore')[:300]}")
    except subprocess.TimeoutExpired:
        shutil.rmtree(workdir, ignore_errors=True)
        raise UnsupportedRepoError("Clone timed out — repo may be too large.")
    return workdir


def _iter_source_files(root: Path):
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = Path(fn).suffix
            if ext in SUPPORTED_EXTENSIONS:
                full = Path(dirpath) / fn
                try:
                    if full.stat().st_size > MAX_FILE_BYTES:
                        continue
                except OSError:
                    continue
                count += 1
                if count > MAX_FILES:
                    raise RepoTooLargeError(f"Repo exceeds {MAX_FILES} supported source files.")
                yield full


def _rel(root: Path, p: Path) -> str:
    return str(p.relative_to(root)).replace(os.sep, "/")


def _read(p: Path) -> Optional[str]:
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None


# ---------- import extraction ----------

def _py_imports(source: str) -> list:
    """Return list of module strings imported by a Python file."""
    mods = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return mods
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                mods.append(("." * (node.level or 0)) + node.module)
    return mods


_JS_IMPORT_RE = re.compile(
    r"""(?:import\s+(?:[\w*{}\s,]+\s+from\s+)?|require\()\s*['"]([^'"]+)['"]"""
)


def _js_imports(source: str) -> list:
    return _JS_IMPORT_RE.findall(source)


def _resolve_import_to_file(root: Path, importer: Path, mod: str, all_files_by_stem: dict) -> Optional[str]:
    """Best-effort resolution of an import string to a repo-relative file path.
    Deterministic, no guessing beyond standard resolution rules."""
    # relative JS/TS import: './x' or '../x'
    if mod.startswith("."):
        base = (importer.parent / mod).resolve()
        for ext in SUPPORTED_EXTENSIONS:
            cand = Path(str(base) + ext)
            if cand.exists():
                return _rel(root, cand)
            cand = base / ("index" + ext)
            if cand.exists():
                return _rel(root, cand)
        # python relative "from . import x" style already dotted; try stem match
        stem = mod.lstrip(".").split(".")[-1]
        if stem in all_files_by_stem:
            return all_files_by_stem[stem]
        return None
    # python absolute module "pkg.sub.mod" -> try last component as filename stem
    stem = mod.split(".")[-1]
    if stem in all_files_by_stem:
        return all_files_by_stem[stem]
    return None


def build_dependency_graph(root: Path):
    """Parse every supported file, return (DiGraph, file_texts, test_files_set)."""
    graph = nx.DiGraph()
    file_texts = {}
    stems = {}

    files = list(_iter_source_files(root))
    for f in files:
        rel = _rel(root, f)
        graph.add_node(rel)
        stems.setdefault(f.stem, rel)  # first match wins, deterministic order = os.walk order

    for f in files:
        rel = _rel(root, f)
        src = _read(f)
        if src is None:
            continue
        file_texts[rel] = src
        if f.suffix == ".py":
            mods = _py_imports(src)
        else:
            mods = _js_imports(src)
        for mod in mods:
            target = _resolve_import_to_file(root, f, mod, stems)
            if target and target != rel:
                graph.add_edge(rel, target)  # rel imports target

    test_files = {
        rel for rel in file_texts
        if "test" in rel.lower() or rel.lower().startswith("tests/")
    }
    return graph, file_texts, test_files


# ---------- scoring ----------

def score_files(graph, file_texts: dict, test_files: set, keywords: list) -> list:
    """Deterministic relevance scoring. Returns FileScore list sorted by score desc."""
    keywords = [k.lower() for k in keywords if k.strip()]
    scores: dict = {}

    def get(path):
        if path not in scores:
            scores[path] = FileScore(path=path, tier="LOW", score=0.0, is_test=path in test_files)
        return scores[path]

    # 1) direct keyword hits in file content / path
    for path, src in file_texts.items():
        fs = get(path)
        lower_src = src.lower()
        lower_path = path.lower()
        hits = 0
        for kw in keywords:
            if kw in lower_path:
                fs.score += 3
                fs.evidence.append(Evidence("keyword", f"filename/path matches '{kw}'"))
            count = lower_src.count(kw)
            if count:
                hits += count
                fs.score += min(count, 5) * 1.5
        if hits:
            fs.evidence.append(Evidence("keyword", f"{hits} keyword match(es) in file content"))

    # 2) import-graph proximity: files imported BY a keyword-hit file, or that import one
    seed_paths = [p for p, fs in scores.items() if fs.score > 0]
    for seed in seed_paths:
        for neighbor in graph.successors(seed):  # seed imports neighbor
            fs = get(neighbor)
            fs.score += 2
            fs.evidence.append(Evidence("import", f"imported by {seed}"))
        for pred in graph.predecessors(seed):  # pred imports seed
            fs = get(pred)
            fs.score += 1.5
            fs.evidence.append(Evidence("import", f"imports {seed}, a keyword-relevant file"))

    # 3) in-degree signal (widely-imported files are structurally important, small boost)
    for path in list(scores.keys()):
        indeg = graph.in_degree(path) if path in graph else 0
        if indeg >= 3:
            scores[path].evidence.append(Evidence("import", f"imported by {indeg} other files in repo"))
            scores[path].score += min(indeg, 10) * 0.3

    # 4) test coverage signal
    for path, fs in list(scores.items()):
        if fs.is_test:
            continue
        referencing_tests = [
            t for t in test_files
            if path in graph and (t in graph.successors(path) or path in graph.predecessors(t))
        ]
        if referencing_tests:
            fs.score += 2
            fs.evidence.append(Evidence("test_reference", f"referenced by {len(referencing_tests)} test file(s): {', '.join(sorted(referencing_tests)[:3])}"))

    results = list(scores.values())
    for fs in results:
        if fs.score >= 6:
            fs.tier = "HIGH"
        elif fs.score >= 2.5:
            fs.tier = "MEDIUM"
        else:
            fs.tier = "LOW"
    results.sort(key=lambda x: x.score, reverse=True)
    return results


def detect_primary_language(root: Path) -> str:
    counts = {"python": 0, "javascript": 0}
    for f in _iter_source_files(root):
        if f.suffix == ".py":
            counts["python"] += 1
        else:
            counts["javascript"] += 1
    if counts["python"] == 0 and counts["javascript"] == 0:
        raise UnsupportedRepoError("No supported source files found (Python or JS/TS only in v1).")
    return "python" if counts["python"] >= counts["javascript"] else "javascript"


def cleanup(root: Path):
    shutil.rmtree(root, ignore_errors=True)
