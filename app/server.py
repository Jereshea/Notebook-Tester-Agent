"""Notebook Tester web dashboard.

A thin web front end over the Hermes Kanban board. Each button creates a task
on the `notebooks` board for one agent profile; the gateway's dispatcher runs
the agent, and the page polls the task until the agent calls kanban_complete.

  Clone & Check  -> nb_fetcher  (clone repo, check the notebook exists)
  Test Notebook  -> nb_tester   (run the notebook, report success / error)

Run with Hermes's Python (it already has FastAPI + uvicorn):
  ~/.hermes/hermes-agent/venv/bin/python ~/notebook-tester/app/server.py
"""

import json
import os
import functools
import logging
import shutil
import time
import re
import shlex
import subprocess
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

BOARD = "notebooks"
HERMES = os.environ.get("HERMES_BIN") or shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")
HERE = Path(__file__).resolve().parent
WORKDIR = os.environ.get("NB_TESTER_HOME", str(HERE.parent))  # repo root; clones go to <WORKDIR>/repos
OUTPUTS = os.path.join(WORKDIR, "outputs")      # one folder per test run: fresh venv(s), executed notebooks, logs, corrections
REPOS = os.path.join(WORKDIR, "repos")          # one checkout per repo/branch/PR
TOOLS = os.path.join(WORKDIR, "agents")         # run_notebook.py and nb_tool.py, used by the agents
AGENT_LOGS = os.path.expanduser(f"~/.hermes/kanban/boards/{BOARD}/logs")  # Hermes worker log per task
ACTIVE = ("todo", "ready", "running")
RESULT_FILES = {  # files each step writes into its run folder (outputs/...)
    "nb_tester": {"executed_notebook": "executed.ipynb", "run_log": "run.log"},
    "nb_fixer": {"corrected_notebook": "corrected.ipynb", "corrected_executed": "corrected-executed.ipynb",
                 "changes_file": "changes.md", "fix_log": "fix.log"},
}
TASK_ID = re.compile(r"^t_[0-9a-f]{6,}$")
BRANCH = re.compile(r"^#?[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")  # git branch name or PR number

app = FastAPI(title="Notebook Tester")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notebook-tester")


def run_hermes(cmd: list[str]) -> dict:
    """Run one `hermes kanban ... --json` command and return its parsed JSON output."""
    log.info("exec: %s", shlex.join(cmd))
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if proc.returncode != 0:
        raise HTTPException(500, f"{shlex.join(cmd[:4])} failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return json.loads(proc.stdout)


def kanban_create(title: str, assignee: str, body: str, max_runtime: str) -> str:
    """Trigger an agent: put a task on the board, assigned to a Hermes profile.

    Equivalent shell command:
      hermes kanban --board notebooks create "<title>" \
          --assignee <profile> --body "<body>" \
          --workspace dir:<WORKDIR> --max-runtime <max_runtime> \
          --max-retries 1 --created-by notebook-tester-ui --json

    The task starts in status "ready". The Hermes gateway's dispatcher picks it
    up within a few seconds and starts the profile as a worker:  hermes -p <profile>
    """
    cmd = [
        HERMES, "kanban", "--board", BOARD, "create", title,
        "--assignee", assignee,
        "--body", body,
        "--workspace", f"dir:{WORKDIR}",
        "--max-runtime", max_runtime,
        "--max-retries", "1",
        "--created-by", "notebook-tester-ui",
        "--json",
    ]
    task_id = run_hermes(cmd)["id"]
    log.info("created task %s for profile %s", task_id, assignee)
    return task_id


def kanban_show(task_id: str) -> dict:
    """Read a task's status and the agent's result.

    Equivalent shell command:
      hermes kanban --board notebooks show <task_id> --json
    """
    return run_hermes([HERMES, "kanban", "--board", BOARD, "show", task_id, "--json"])


class FetchRequest(BaseModel):
    repo_url: str
    notebook_path: str
    branch: str = "main"   # branch name, or a pull request: "#42" / "42" / "pr/42"


class TestRequest(BaseModel):
    repo_dir: str
    notebook_abs: str


class FixRequest(BaseModel):
    test_task_id: str


@functools.lru_cache(maxsize=256)
def pr_details(repo: str, number: int) -> dict | None:
    """Read-only PR lookup (title, author, state...) with the local gh login. Cached per PR."""
    proc = subprocess.run(["gh", "pr", "view", str(number), "--repo", repo, "--json",
                           "title,author,headRefName,state,url"], capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        log.info("gh pr view %s#%s failed: %s", repo, number, proc.stderr.strip()[:200])
        return None
    d = json.loads(proc.stdout)
    return {"number": number, "title": d.get("title"), "author": (d.get("author") or {}).get("login"),
            "head_branch": d.get("headRefName"), "state": d.get("state"), "url": d.get("url")}


def clean_summary(text):
    """Keep only the agent's sentence: drop any tool-call markup a model sometimes leaks into it."""
    if not text:
        return text
    return re.split(r"</?[A-Za-z_:]+[ >]", text, maxsplit=1)[0].strip()


def body_fields(body: str) -> dict:
    """Parse the KEY: value lines this server writes into task bodies."""
    out = {}
    for line in (body or "").splitlines():
        key, sep, value = line.partition(": ")
        if sep and key.isupper():
            out[key] = value.strip()
    return out


def active_tasks() -> list[dict]:
    tasks = run_hermes([HERMES, "kanban", "--board", BOARD, "list", "--json"])
    return [t for t in tasks if t["status"] in ACTIVE]


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.post("/api/fetch")
def fetch(req: FetchRequest):
    url, path = req.repo_url.strip(), req.notebook_path.strip().lstrip("/")
    if not url or not path:
        raise HTTPException(400, "Enter both the repository URL and the notebook path.")
    if "github.com" not in url:
        raise HTTPException(400, "Enter a GitHub repository URL, e.g. https://github.com/owner/repo")
    branch = req.branch.strip() or "main"
    if not BRANCH.match(branch) or ".." in branch:
        raise HTTPException(400, "Enter a valid branch name (e.g. main) or a pull request number (e.g. #42).")
    name = url.rstrip("/").removesuffix(".git").split("github.com")[-1].strip("/:")
    # Button "Clone & Check"  ->  hermes kanban create ... --assignee nb_fetcher
    task_id = kanban_create(
        title=f"Clone & check: {name}@{branch} :: {path}",
        assignee="nb_fetcher",
        body=f"REPO_URL: {url}\nBRANCH: {branch}\nNOTEBOOK_PATH: {path}\nREPOS_DIR: {REPOS}\nTOOLS_DIR: {TOOLS}",
        max_runtime="20m",
    )
    return {"task_id": task_id}


@app.post("/api/test")
def test(req: TestRequest):
    repo_dir, nb = req.repo_dir.strip(), req.notebook_abs.strip()
    root = os.path.realpath(REPOS)
    if not os.path.realpath(nb).startswith(root + os.sep):
        raise HTTPException(400, "Notebook must be inside a repo cloned by Clone & Check.")
    # Every test run gets its own folder: outputs/<owner>__<repo>__<branch>/<timestamp>__<notebook>/
    stem = Path(nb).stem.replace(" ", "_")
    output_dir = os.path.join(OUTPUTS, os.path.basename(repo_dir.rstrip("/")), f"{time.strftime('%Y%m%d-%H%M%S')}__{stem}")
    os.makedirs(output_dir, exist_ok=True)
    # Button "Test Notebook"  ->  hermes kanban create ... --assignee nb_tester
    task_id = kanban_create(
        title=f"Test notebook: {os.path.relpath(nb, root)}",
        assignee="nb_tester",
        body=f"REPO_DIR: {repo_dir}\nNOTEBOOK_ABS: {nb}\nOUTPUT_DIR: {output_dir}\nTOOLS_DIR: {TOOLS}",
        max_runtime="3h",
    )
    return {"task_id": task_id, "output_dir": output_dir}


@app.post("/api/fix")
def fix(req: FixRequest):
    if not TASK_ID.match(req.test_task_id):
        raise HTTPException(400, "Bad task id")
    d = kanban_show(req.test_task_id)
    t, run = d["task"], (d["runs"][-1] if d.get("runs") else {})
    meta = run.get("metadata") or {}
    if t["assignee"] != "nb_tester" or t["status"] != "done":
        raise HTTPException(409, "Only a finished notebook test can be corrected.")
    if meta.get("success"):
        raise HTTPException(409, "This notebook already ran successfully. Nothing to correct.")
    f = body_fields(t["body"])
    error = {k: meta.get(k) for k in ("error_type", "error_message", "failed_cell", "failed_cell_source", "traceback_tail", "explanation")}
    # Button "Correct the notebook"  ->  hermes kanban create ... --assignee nb_fixer
    task_id = kanban_create(
        title=f"Correct notebook: {os.path.relpath(f['NOTEBOOK_ABS'], REPOS)}",
        assignee="nb_fixer",
        body=(f"REPO_DIR: {f['REPO_DIR']}\nNOTEBOOK_ABS: {f['NOTEBOOK_ABS']}\nOUTPUT_DIR: {f['OUTPUT_DIR']}\nTOOLS_DIR: {TOOLS}\n"
              f"ERROR: {json.dumps(error)}"),
        max_runtime="30m",
    )
    return {"task_id": task_id}


ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
NOISE = re.compile(r"^\s*┊ \S+ preparing .*…\s*$")   # Hermes "tool is starting" lines; the real line follows


@app.get("/api/task/{task_id}/log")
def task_log(task_id: str, offset: int = 0):
    """Live activity log of one step (what the agent ran and wrote), in increments.

    Returns only complete new lines after `offset`; pass the returned `offset` back on the next call.
    """
    if not TASK_ID.match(task_id):
        raise HTTPException(400, "Bad task id")
    path = os.path.join(AGENT_LOGS, f"{task_id}.log")
    if not os.path.exists(path):
        return {"text": "", "offset": 0, "exists": False}
    offset = min(max(0, offset), os.path.getsize(path))
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read(256 * 1024)
    end = chunk.rfind(b"\n") + 1          # stop at the last complete line
    text = chunk[:end].decode("utf-8", "replace")
    lines = [ANSI.sub("", line) for line in text.splitlines()]
    lines = [line for line in lines if not NOISE.match(line)]
    return {"text": "\n".join(lines) + ("\n" if lines else ""), "offset": offset + end, "exists": True}


@app.get("/api/info")
def info():
    """Where results and logs are stored (shown on the page)."""
    return {"outputs": OUTPUTS, "repos": REPOS, "agent_logs": AGENT_LOGS,
            "server_log": os.environ.get("NB_TESTER_LOG") or os.path.join(WORKDIR, "server.log")}


@app.post("/api/clear")
def clear():
    """Delete every test run folder (its venvs, executed/corrected notebooks, logs) and any
    legacy per-repo .nbtest-venv. Cloned code is kept."""
    busy = active_tasks()
    if busy:
        raise HTTPException(409, f"{len(busy)} step(s) still running. Wait for them to finish, then clear.")
    removed_venvs, removed_runs, freed = 0, 0, 0

    def size(path: str) -> int:
        return sum(os.path.getsize(os.path.join(r, n)) for r, _, fs in os.walk(path) for n in fs
                   if not os.path.islink(os.path.join(r, n)))

    for name in (os.listdir(REPOS) if os.path.isdir(REPOS) else []):
        venv = os.path.join(REPOS, name, ".nbtest-venv")
        if os.path.isdir(venv):
            freed += size(venv); shutil.rmtree(venv); removed_venvs += 1
    for name in (os.listdir(OUTPUTS) if os.path.isdir(OUTPUTS) else []):
        path = os.path.join(OUTPUTS, name)
        removed_runs += len(os.listdir(path)) if os.path.isdir(path) else 1
        freed += size(path) if os.path.isdir(path) else os.path.getsize(path)
        shutil.rmtree(path) if os.path.isdir(path) else os.remove(path)
    log.info("clear: removed %d venvs, %d test outputs, %.1f MB", removed_venvs, removed_runs, freed / 1e6)
    return {"venvs": removed_venvs, "runs": removed_runs, "freed_mb": round(freed / 1e6, 1)}


@app.get("/api/task/{task_id}")
def task(task_id: str):
    if not TASK_ID.match(task_id):
        raise HTTPException(400, "Bad task id")
    d = kanban_show(task_id)
    t = d["task"]
    run = d["runs"][-1] if d.get("runs") else {}
    block_reason = None
    if t["status"] in ("blocked", "triage"):
        for ev in reversed(d.get("events", [])):
            payload = ev.get("payload") or {}
            if isinstance(payload, dict) and (payload.get("reason") or payload.get("error")):
                block_reason = payload.get("reason") or payload.get("error")
                break
        if block_reason and "not alive" in block_reason:
            block_reason = ("The agent process stopped unexpectedly. Check that the model server is "
                            "reachable, then try again. Details: hermes kanban --board notebooks log " + t["id"])
    # Fill in result file paths from the run folder, so the page never depends on the
    # agent remembering to list them.
    meta = dict(run.get("metadata") or {})
    if t["assignee"] == "nb_fixer" and t["status"] == "done":
        meta.setdefault("fixed", False)          # the agent sometimes states it only in its text
    pr = meta.get("pr")
    if t["assignee"] == "nb_fetcher" and isinstance(pr, dict) and pr.get("number") and not pr.get("title") and meta.get("repo"):
        meta["pr"] = pr_details(meta["repo"], int(pr["number"])) or pr
    out_dir = body_fields(t["body"]).get("OUTPUT_DIR")
    if out_dir and t["assignee"] in RESULT_FILES:
        meta.setdefault("output_dir", out_dir)
        for key, name in RESULT_FILES[t["assignee"]].items():
            path = os.path.join(out_dir, name)
            if not meta.get(key) and os.path.exists(path):
                meta[key] = path
    return {
        "id": t["id"],
        "status": t["status"],
        "assignee": t["assignee"],
        "created_at": t["created_at"],
        "started_at": t["started_at"],
        "completed_at": t["completed_at"],
        "summary": clean_summary(d.get("latest_summary") or run.get("summary")),
        "agent_log": os.path.join(AGENT_LOGS, f"{t['id']}.log"),
        "metadata": meta,
        "error": run.get("error"),
        "block_reason": block_reason,
        "comments": [c.get("body") for c in d.get("comments", [])][-3:],
    }


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(os.environ.get("PORT", "8090")))
