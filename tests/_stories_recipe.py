"""Deterministic fake-claude stories-E2E recipe, vendored from bmad-loop core.

These helpers are lifted from core's ``tests/test_stories_e2e.py`` (the fake
CLI + custom-profile-TOML sandbox recipe) plus the two skill-stub helpers from
core's ``tests/conftest.py`` that ``_scaffold`` needs — so ``test_e2e.py`` here
proves the exact same full stack (arg parsing, prompt render, hook-signal
completion, stories read-back, git commit, sprint advance) resolves over herdr
as core proves it does over tmux. The scaffold/assertion helpers are verbatim;
``FAKE_CLI`` is trimmed to the stories-mode branch, the only path the herdr E2E
drives (``_scaffold`` always writes a ``[stories]`` policy, so the sweep/sprint
branches of core's fake could never fire here).

DELIBERATE DIVERGENCE from the vendored original (an upstream candidate): the
fake CLI is Python, not bash, and lands via :func:`write_portable_cli`, so the
same E2E runs on native Windows (this repo's win32 launch surface) where a
bash script cannot be argv[0]. Keep the recipe's BEHAVIOR in sync with core if
the shape there changes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml

# The fake CLI: reads the story id + spec folder from the session env (as the real
# folder+id adapter does), writes the id-keyed story spec BEFORE the Stop event so
# the adapter's read-back finds a terminal spec, and stays alive until the engine
# kills its window. Routes on the existing spec status so one script drives a fresh
# dispatch, a plan-halt leg, its post-checkpoint implement leg, and a blocked halt.
# Python (not bash) so write_portable_cli can spawn it on either platform family.
FAKE_CLI = r"""import glob
import json
import os
import subprocess
import sys
import time

rd = os.environ["BMAD_LOOP_RUN_DIR"]
tid = os.environ["BMAD_LOOP_TASK_ID"]
story = os.environ["BMAD_LOOP_STORY_KEY"]
folder = os.environ["BMAD_LOOP_SPEC_FOLDER"]

ts = time.time_ns()
os.makedirs(os.path.join(rd, "events"), exist_ok=True)


def event(ts_ns, name):
    path = os.path.join(rd, "events", "%d-%s-%s.json" % (ts_ns, tid, name))
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"ts": ts_ns, "event": name, "task_id": tid, "session_id": "fake-1"}, fh)


event(ts, "SessionStart")
baseline = subprocess.run(
    ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
).stdout.strip()

sdir = os.path.join(folder, "stories")
os.makedirs(sdir, exist_ok=True)
spec = os.path.join(sdir, story + "-slug.md")
existing = sorted(glob.glob(os.path.join(sdir, story + "-*.md")))
status = ""
if existing:
    with open(existing[0], encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("status:"):
                status = line.split(":", 1)[1].strip().strip("'\" ")
                break


def write_spec(status_word, body):
    with open(spec, "w", encoding="utf-8") as fh:
        fh.write(
            "---\ntitle: %s\nstatus: %s\nbaseline_commit: %s\n---\n\n# %s\n%s\n"
            % (story, status_word, baseline, story, body)
        )


def write_done():
    with open("src.txt", "a", encoding="utf-8") as fh:
        fh.write("impl for %s\n" % story)
    write_spec("done", "implemented.")


if status in ("ready-for-dev", "in-progress", "draft"):
    write_done()  # re-dispatch after a plan-checkpoint or a re-arm
elif os.environ.get("BMAD_LOOP_PLAN_HALT"):
    write_spec("ready-for-dev", "planned.")  # spec_checkpoint leg 1: halt after planning
elif os.path.isfile(os.path.join(folder, ".block-" + story)):
    # poisoned story: first dispatch blocks
    write_spec("blocked", "\n## Auto Run Result\n\n- Status: blocked\n\nNeeds a human decision.")
else:
    write_done()  # normal fresh dispatch

event(ts + 1, "Stop")
time.sleep(30)
"""

# TOML literal (single-quoted) string for binary: the win32 path contains
# backslashes, which a basic "..." TOML string would treat as escapes.
PROFILE_TOML = """\
name = "fakestories"
binary = '{binary}'
bypass_args = []
usage_parser = "none"
skill_tree = ".claude/skills"

[hooks]
dialect = "claude-settings-json"
config_path = ".claude/settings.json"
events = {{ SessionStart = "SessionStart", Stop = "Stop" }}
"""


def write_portable_cli(directory: Path, name: str, body: str) -> Path:
    """Write a Python fake CLI as a file either platform's launch can spawn as
    argv[0]: a shebanged executable on POSIX (the typed-exec launch resolves
    it through sh), a ``.cmd`` shim around a sibling ``.py`` on win32 (the
    ``agent start`` launch hands argv[0] to CreateProcess, which resolves
    ``.cmd`` but neither shebangs nor ``.py`` associations)."""
    directory.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        script = directory / f"{name}.py"
        script.write_text(body, encoding="utf-8")
        shim = directory / f"{name}.cmd"
        shim.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return shim
    launcher = directory / name
    launcher.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    launcher.chmod(0o755)
    return launcher


SPEC_FOLDER = "_bmad-output/epic-1"
CLI = [sys.executable, "-m", "bmad_loop.cli"]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def _entry(story_id: str, **over) -> dict:
    d = {"id": story_id, "title": f"Story {story_id}", "description": "does a thing"}
    d.update(over)
    return d


def _write_skill_stubs(skills: Path, catalog: dict) -> None:
    """Stub every skill in `catalog` (an install.py {skill: marker_files} map) under
    `skills`. Reading the catalog instead of restating it means a newly required
    skill or marker file fails the scaffolds loudly rather than drifting."""
    for skill, markers in catalog.items():
        d = skills / skill
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(f"# {skill}\n", encoding="utf-8")
        for marker in markers:
            (d / marker).write_text("x\n", encoding="utf-8")


def install_dev_base_skills(root: Path, tree: str = ".claude/skills", *, folder_id: bool) -> Path:
    """Lay down stubs of the upstream skills the orchestrator drives on every dev run
    (`install.DEV_BASE_SKILLS`: bmad-dev-auto + the review hunters) under
    ``root/tree``, so the run-start preflight (`install.missing_base_skills`) passes.

    ``folder_id`` also writes bmad-dev-auto's step-01 carrying the dispatch marker
    `install.missing_stories_support` content-probes for — stories mode needs a newer
    bmad-dev-auto than file existence alone can prove. Returns the skills tree root."""
    from bmad_loop.install import (
        DEV_BASE_SKILLS,
        STORIES_PROBE_FILE,
        STORIES_PROBE_SKILL,
        STORIES_PROBE_TEXT,
    )

    skills = Path(root) / tree
    _write_skill_stubs(skills, DEV_BASE_SKILLS)
    if folder_id:
        (skills / STORIES_PROBE_SKILL / STORIES_PROBE_FILE).write_text(
            f"This is a **{STORIES_PROBE_TEXT}** router.\n", encoding="utf-8"
        )
    return skills


def _scaffold(root: Path, entries: list[dict]) -> None:
    """A committed, clean sandbox: git repo, BMAD config + artifact dirs, the
    base-skill stubs the stories preflight requires (incl. the folder+id dispatch
    probe), a stories.yaml + SPEC.md, the fake-CLI profile, and a stories-mode
    policy — everything committed so the run-start worktree_clean gate passes."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "src.txt").write_text("original\n", encoding="utf-8")
    (root / ".gitignore").write_text(".bmad-loop/runs/\n", encoding="utf-8")

    cfg = root / "_bmad" / "bmm"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(
        "implementation_artifacts: '{project-root}/_bmad-output/implementation-artifacts'\n"
        "planning_artifacts: '{project-root}/_bmad-output/planning-artifacts'\n",
        encoding="utf-8",
    )
    for sub in ("implementation-artifacts", "planning-artifacts"):
        (root / "_bmad-output" / sub).mkdir(parents=True, exist_ok=True)
        (root / "_bmad-output" / sub / ".keep").write_text("", encoding="utf-8")

    install_dev_base_skills(root, folder_id=True)  # tree matches PROFILE_TOML's skill_tree

    folder = root / SPEC_FOLDER
    (folder / "stories").mkdir(parents=True)
    (folder / "SPEC.md").write_text("---\ntitle: Epic 1\n---\n# Epic 1\n", encoding="utf-8")
    (folder / "stories.yaml").write_text(yaml.safe_dump(entries, sort_keys=False), encoding="utf-8")

    fake = write_portable_cli(root / ".bmad-loop", "fake-cli", FAKE_CLI)
    profiles = root / ".bmad-loop" / "profiles"
    profiles.mkdir(parents=True)
    (profiles / "fakestories.toml").write_text(
        PROFILE_TOML.format(binary=str(fake)), encoding="utf-8"
    )
    (root / ".bmad-loop" / "policy.toml").write_text(
        '[adapter]\nname = "fakestories"\n\n'
        "[review]\nenabled = false\n\n"
        f'[stories]\nsource = "stories"\nspec_folder = "{SPEC_FOLDER}"\n',
        encoding="utf-8",
    )

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "e2e@test")
    _git(root, "config", "user.name", "e2e")
    _git(root, "config", "core.fsync", "none")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "sandbox")


def _status(root: Path, story_id: str) -> str:
    spec = root / SPEC_FOLDER / "stories" / f"{story_id}-slug.md"
    if not spec.is_file():
        return "pending"
    for line in spec.read_text(encoding="utf-8").splitlines():
        if line.startswith("status:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return "?"


def _commit_count(root: Path) -> int:
    out = subprocess.run(
        ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())
