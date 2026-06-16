import asyncio
import logging
from typing import Optional

from founderscrew.config import settings
from founderscrew.orchestrator import Orchestrator
from founderscrew.runtime_diagnostics import log_runtime_fingerprint
from founderscrew.workflow_queue import WorkflowJob, WorkflowQueue

logger = logging.getLogger("founderscrew.worker")


async def run_worker_once(
    orchestrator: Optional[Orchestrator] = None,
    queue: Optional[WorkflowQueue] = None,
    lease_seconds: int = 3600,
) -> bool:
    """Claims and executes one queued workflow job. Returns True if work ran."""
    orchestrator = orchestrator or Orchestrator()
    queue = queue or WorkflowQueue()
    job = queue.claim_next(lease_seconds=lease_seconds)
    if not job:
        return False

    skip_reason = _job_skip_reason(orchestrator, job)
    if skip_reason:
        logger.warning(f"Skipping workflow job {job.id}: {skip_reason}")
        queue.complete(job.id)
        return True

    try:
        logger.info(f"Running workflow job {job.id}: {job.session_id} stage={job.stage}")
        await orchestrator.run_queued_stage(job.session_id, job.stage, job.payload)
        queue.complete(job.id)
        logger.info(f"Completed workflow job {job.id}: {job.session_id} stage={job.stage}")
        return True
    except Exception as e:
        logger.exception(f"Workflow job {job.id} failed: {e}")
        _mark_state_failed(orchestrator, job, str(e))
        queue.fail(job, str(e))
        return True


async def run_worker_loop(
    poll_interval: float = 5.0,
    lease_seconds: int = 3600,
) -> None:
    orchestrator = Orchestrator()
    queue = WorkflowQueue()
    logger.info("Founders.crew worker started.")
    log_runtime_fingerprint("worker-loop")
    while True:
        ran = await run_worker_once(orchestrator, queue, lease_seconds=lease_seconds)
        if not ran:
            await asyncio.sleep(poll_interval)


def _mark_state_failed(orchestrator: Orchestrator, job: WorkflowJob, error: str) -> None:
    state = orchestrator.store.load_state(job.session_id)
    if not state:
        return
    stage = job.stage
    if stage in {"build", "build_test_review"}:
        stage = "building"
    orchestrator._fail(state, stage, f"Queued workflow job failed: {error}")


def _job_skip_reason(orchestrator: Orchestrator, job: WorkflowJob) -> Optional[str]:
    state = orchestrator.store.load_state(job.session_id)
    if not state:
        return f"no workflow state found for session {job.session_id}"

    configured_repo = settings.get("github.repository") or ""
    if configured_repo and state.issue.repository != configured_repo:
        return (
            f"queued repo {state.issue.repository!r} does not match configured "
            f"repo {configured_repo!r}"
        )

    return None
