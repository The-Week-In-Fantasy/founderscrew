"""Persistent repository memory for the agent crew.

Two kinds of knowledge are kept per target repository, stored via the
StateStore (SQLite locally, Firestore on Cloud Run):

- profile: deterministic facts scanned from the workspace clone (languages,
  test framework/command, test file conventions, dev server, layout). Keyed by
  the workspace HEAD SHA so it auto-refreshes when the base branch moves.
- lessons: short episodic records of what previous workflows learned (what
  failed, what fixed it), so later issues don't rediscover the same gotchas.
"""
import json
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("founderscrew.repo_profile")

PROFILE_VERSION = 1
MAX_LESSONS = 20

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next", "coverage"}
_TEST_FILE_SUFFIXES = (".spec.js", ".spec.ts", ".spec.jsx", ".spec.tsx", ".spec.mjs",
                       ".test.js", ".test.ts", ".test.jsx", ".test.tsx", ".test.mjs")


def _git_head_sha(workdir: Path) -> str:
    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(workdir), capture_output=True, text=True
        )
        return res.stdout.strip() if res.returncode == 0 else ""
    except Exception:
        return ""


def _scan_test_layout(root: Path) -> Dict[str, Any]:
    """Finds where test files live and what naming conventions they follow."""
    test_dirs = set()
    naming = set()
    count = 0
    for path in root.rglob("*"):
        if count >= 200:
            break
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        name = path.name
        is_test = (
            name.endswith(_TEST_FILE_SUFFIXES)
            or (name.startswith("test_") and name.endswith(".py"))
            or (name.endswith("_test.py"))
        )
        if not is_test:
            continue
        count += 1
        test_dirs.add(path.parent.relative_to(root).as_posix())
        if name.endswith(".py"):
            naming.add("test_*.py" if name.startswith("test_") else "*_test.py")
        else:
            for suffix in _TEST_FILE_SUFFIXES:
                if name.endswith(suffix):
                    naming.add(f"*{suffix}")
                    break
    return {"test_dirs": sorted(test_dirs)[:10], "test_naming": sorted(naming)}


def build_repo_profile(repo_name: str, workdir: str) -> Dict[str, Any]:
    """Deterministically scans a workspace clone and returns a repo profile."""
    root = Path(workdir)
    profile: Dict[str, Any] = {
        "version": PROFILE_VERSION,
        "repo": repo_name,
        "head_sha": _git_head_sha(root),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "languages": [],
        "frameworks": [],
        "scripts": {},
        "test_framework": None,
        "test_command": None,
        "dev_server_command": None,
    }

    pkg_file = root / "package.json"
    if pkg_file.exists():
        profile["languages"].append("javascript/typescript")
        try:
            pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not parse package.json for {repo_name}: {e}")
            pkg = {}

        scripts = pkg.get("scripts", {}) or {}
        profile["scripts"] = {k: v for k, v in scripts.items()
                              if k in ("test", "dev", "start", "build", "preview", "lint")}

        deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
        known = ["react", "vue", "svelte", "next", "vite", "tailwindcss",
                 "@playwright/test", "jest", "vitest", "express", "fastify"]
        profile["frameworks"] = [k for k in known if k in deps]

        if "pnpm-lock.yaml" in (p.name for p in root.iterdir()):
            profile["package_manager"] = "pnpm"
        elif (root / "yarn.lock").exists():
            profile["package_manager"] = "yarn"
        else:
            profile["package_manager"] = "npm"

        test_script = scripts.get("test", "")
        if "@playwright/test" in deps or "playwright" in test_script:
            profile["test_framework"] = "playwright"
        elif "vitest" in deps or "vitest" in test_script:
            profile["test_framework"] = "vitest"
        elif "jest" in deps or "jest" in test_script:
            profile["test_framework"] = "jest"
        if "test" in scripts:
            profile["test_command"] = "npm test"

        dev_script = scripts.get("dev") or scripts.get("start") or ""
        if "vite" in dev_script or "vite" in deps:
            profile["dev_server_command"] = "npx vite --port 3001 --strictPort"

    if (root / "pyproject.toml").exists() or (root / "requirements.txt").exists():
        profile["languages"].append("python")
        if not profile["test_command"]:
            profile["test_framework"] = profile["test_framework"] or "pytest"
            profile["test_command"] = "pytest"

    profile.update(_scan_test_layout(root))

    try:
        profile["top_level"] = sorted(
            p.name + ("/" if p.is_dir() else "")
            for p in root.iterdir()
            if p.name not in _SKIP_DIRS and not p.name.startswith(".")
        )[:30]
    except Exception:
        profile["top_level"] = []

    return profile


def get_repo_memory(store, repo_name: str, workdir: Optional[str] = None) -> Dict[str, Any]:
    """Returns {"profile": ..., "lessons": [...]} for a repository.

    When a workdir is given, the profile is (re)built if missing or if the
    workspace HEAD SHA no longer matches the cached one — this is the staleness
    guard. Lessons always survive profile refreshes.
    """
    record = store.load_repo_memory(repo_name) or {}
    profile = record.get("profile")
    lessons = record.get("lessons", [])

    if workdir:
        try:
            current_sha = _git_head_sha(Path(workdir))
            if not profile or (current_sha and profile.get("head_sha") != current_sha):
                profile = build_repo_profile(repo_name, workdir)
                store.save_repo_memory(repo_name, {"profile": profile, "lessons": lessons})
                logger.info(f"Repo profile for {repo_name} refreshed (sha {current_sha[:8] if current_sha else 'unknown'}).")
        except Exception as e:
            logger.warning(f"Could not build repo profile for {repo_name}: {e}")

    return {"profile": profile, "lessons": lessons}


def add_repo_lesson(store, repo_name: str, lesson: Dict[str, Any]) -> None:
    """Appends an episodic lesson to a repository's memory (capped at MAX_LESSONS)."""
    try:
        record = store.load_repo_memory(repo_name) or {}
        lessons = record.get("lessons", [])
        lesson = {**lesson, "date": datetime.now(timezone.utc).isoformat()}
        lessons.append(lesson)
        record["lessons"] = lessons[-MAX_LESSONS:]
        store.save_repo_memory(repo_name, record)
    except Exception as e:
        logger.warning(f"Could not record repo lesson for {repo_name}: {e}")


def format_repo_memory(memory: Dict[str, Any]) -> str:
    """Renders profile + lessons as a compact prompt block. Empty string if nothing known."""
    profile = memory.get("profile") or {}
    lessons = memory.get("lessons") or []
    if not profile and not lessons:
        return ""

    lines = []
    if profile:
        lines.append("REPO PROFILE (cached knowledge — verify paths before relying on them):")
        if profile.get("languages"):
            lines.append(f"- Languages: {', '.join(profile['languages'])}")
        if profile.get("frameworks"):
            lines.append(f"- Frameworks: {', '.join(profile['frameworks'])}")
        if profile.get("scripts"):
            scripts = "; ".join(f"{k}='{v}'" for k, v in profile["scripts"].items())
            lines.append(f"- npm scripts: {scripts}")
        if profile.get("test_framework") or profile.get("test_command"):
            lines.append(f"- Tests: framework={profile.get('test_framework') or 'unknown'}, run with: {profile.get('test_command') or 'unknown'}")
        if profile.get("test_dirs"):
            naming = f" (naming: {', '.join(profile.get('test_naming') or [])})" if profile.get("test_naming") else ""
            lines.append(f"- Test locations: {', '.join(profile['test_dirs'])}{naming}")
        if profile.get("dev_server_command"):
            lines.append(f"- Dev server: {profile['dev_server_command']}")
        if profile.get("top_level"):
            lines.append(f"- Top-level entries: {', '.join(profile['top_level'])}")

    if lessons:
        lines.append("LESSONS FROM PREVIOUS WORK ON THIS REPO:")
        for lesson in lessons[-8:]:
            issue = f"#{lesson['issue']} " if lesson.get("issue") else ""
            lines.append(f"- {issue}{lesson.get('summary', '')}")

    return "\n".join(lines)
