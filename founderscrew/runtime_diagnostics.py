import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from founderscrew import __version__
from founderscrew.config import settings

logger = logging.getLogger("founderscrew.runtime")

PROCESS_STARTED_AT = time.time()


def runtime_fingerprint(role: str) -> Dict[str, Any]:
    """Builds a non-secret runtime fingerprint for stale-process diagnosis."""
    package_root = Path(__file__).resolve().parent
    repo_root = package_root.parent
    fingerprint = {
        "role": role,
        "pid": os.getpid(),
        "version": __version__,
        "cwd": str(Path.cwd()),
        "python": sys.executable,
        "source_root": str(package_root),
        "git_commit": _git_output(repo_root, ["git", "rev-parse", "--short", "HEAD"]),
        "git_dirty": bool(_git_output(repo_root, ["git", "status", "--porcelain"])),
        "configured_repo": settings.get("github.repository", ""),
        "coding_mode": settings.get("coding_tools.mode", ""),
        "coding_tiers": [
            settings.get("coding_tools.tier1", ""),
            settings.get("coding_tools.tier2", ""),
            settings.get("coding_tools.tier3", ""),
        ],
        "fast_tiers": [
            settings.get("agents.fast_tier1", ""),
            settings.get("agents.fast_tier2", ""),
            settings.get("agents.fast_tier3", ""),
        ],
        "planning_tiers": [
            settings.get("agents.planning_tier1", ""),
            settings.get("agents.planning_tier2", ""),
            settings.get("agents.planning_tier3", ""),
        ],
    }
    return fingerprint


def log_runtime_fingerprint(role: str) -> None:
    fingerprint = runtime_fingerprint(role)
    logger.info("Runtime fingerprint: %s", json.dumps(fingerprint, sort_keys=True))
    newest_mtime = newest_source_mtime(Path(__file__).resolve().parent)
    if newest_mtime and source_newer_than(PROCESS_STARTED_AT, Path(__file__).resolve().parent):
        logger.warning(
            "Founders.crew source files changed after this process started. "
            "Restart the dashboard/worker to pick up fixes. pid=%s role=%s newest_source_mtime=%s",
            os.getpid(),
            role,
            newest_mtime,
        )


def source_newer_than(timestamp: float, root: Optional[Path] = None) -> bool:
    newest = newest_source_mtime(root or Path(__file__).resolve().parent)
    return bool(newest and newest > timestamp + 1)


def newest_source_mtime(root: Path) -> Optional[float]:
    newest: Optional[float] = None
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        newest = mtime if newest is None else max(newest, mtime)
    return newest


def _git_output(cwd: Path, cmd: list[str]) -> str:
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=5)
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return (result.stdout or "").strip()
