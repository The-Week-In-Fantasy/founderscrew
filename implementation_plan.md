# Founders.crew Workflow Review & Improvement Proposals

I've reviewed the entire codebase end-to-end: orchestrator, all 7 agents, tools, worker, webhook, config, state management, and the dashboard. Here's my honest assessment and prioritized recommendations.

---

## What's Working Well ✅

| Area | Assessment |
|------|-----------|
| **Architecture** | Clean stage-based pipeline with queue-backed async execution. Proper separation of concerns. |
| **Model tier fallback** | 3-tier model routing with automatic fallback is solid resilience engineering. |
| **Test self-healing loop** | Builder ↔ Tester feedback loop with configurable max retries is exactly right. |
| **Repo memory / lessons** | Episodic memory that persists gotchas across issues prevents rediscovery of the same bugs. |
| **Workspace locking** | Per-repo async locks prevent concurrent builds from trampling each other. |
| **JSON self-healing** | Re-prompting agents when they output prose instead of JSON is a smart recovery pattern. |
| **Branch pushing** | Pushing WIP after each build step prevents lost work during human approval gates. |

---

## Critical Issues to Fix 🔴

### 1. Reviewer Agent Is Blind — Has No Context

**Problem:** The reviewer receives `state.test_results.model_dump()` — just the test outcomes. It has **zero access** to what actually changed. It can't review code it hasn't seen.

**Fix:** Pass the issue context, plan, files changed, and the actual git diff to the reviewer. Give it a tool to read the working tree diff (`git diff HEAD~1`).

```python
# Current (line 681):
review_data = await self._run_agent_json(
    get_reviewer_agent, session_id,
    state.test_results.model_dump() if state.test_results else {}
)

# Should be:
review_data = await self._run_agent_json(
    get_reviewer_agent, session_id,
    {
        "issue_title": state.issue.title,
        "issue_body": state.issue.body,
        "plan_summary": state.plan.summary if state.plan else "",
        "files_changed": [f for s in (state.plan.steps if state.plan else []) for f in s.files_affected],
        "test_results": state.test_results.model_dump() if state.test_results else {},
        "repository": state.issue.repository,
    }
)
```

---

### 2. Builder Agent Doesn't See Its Own Previous Output on Fix Loops

**Problem:** When the builder is called back to fix test failures (`_builder_fix`), it gets the error message but NOT a reminder of what it already changed. It may make contradictory edits or overwrite its own work.

**Fix:** Include a summary of prior changes in the fix instruction by tracking `modified_files` from the initial build pass on the state model.

---

### 3. No Timeout or Dead-Letter for Stuck Approval Gates

**Problem:** If a run reaches `AWAIT_PLAN_APPROVAL` or `AWAIT_QA_APPROVAL` and the founder never responds, it sits forever. There's no reminder, no expiry, no escalation.

**Fix:** Add a configurable approval timeout (e.g., 48 hours) with:
- An automated reminder comment on the GitHub issue after N hours
- Optional auto-approve for low-complexity issues after timeout
- A "stale" status visible on the dashboard

---

### 4. Deploy Stage Doesn't Pass QA Report or Test Evidence to PR Body

**Problem:** The deployer agent gets `branch_name`, `repository`, `issue_number`, and `plan_summary`. But it doesn't get the QA report, test results, or screenshots. The PR body ends up generic.

**Fix:** Pass the full context so the PR body is rich and useful:
```python
pr_data = {
    "branch_name": state.branch_name,
    "repository": repo_name,
    "issue_number": issue_number,
    "plan_summary": state.plan.summary,
    "test_results": state.test_results.model_dump() if state.test_results else {},
    "qa_summary": state.qa_report.summary if state.qa_report else "",
    "files_changed": [f for s in state.plan.steps for f in s.files_affected],
}
```

---

## High-Impact Improvements 🟡

### 5. Planner Should Read Actual File Contents, Not Just Paths

**Problem:** The planner gets `github_get_file_content` and `github_search_code` as tools, but the orchestrator only passes it `state.issue.model_dump()` and repo memory. The planner must decide on its own to call tools. If it doesn't (or the LLM skips tool calls), the plan is based on guessing.

**Fix:** Pre-read the affected files identified by triage and include their contents (truncated) directly in the planner's input. This guarantees the planner always sees the relevant code.

---

### 6. Builder Fix Loop Should Accumulate Error History

**Problem:** Each fix attempt only sees the *latest* test failure. If the builder oscillates (fixes A but breaks B, then fixes B but re-breaks A), it has no memory of prior failures.

**Fix:** Accumulate all error outputs across attempts and pass the full history:
```
"Previous fix attempts:\n"
"Attempt 1: [error...]\n"
"Attempt 2: [error...]\n"
"Current failure (attempt 3):\n[error...]"
```

---

### 7. No Lint / Type-Check Gate

**Problem:** The workflow runs tests but never runs linting or type-checking. The builder can introduce style violations, unused imports, or type errors that tests won't catch.

**Fix:** Add a lint step between building and testing. Run project-specific lint commands from the repo profile (e.g., `npm run lint`, `eslint`, `ruff check`). Auto-fix lintable issues before testing.

---

### 8. Dev Server Boot Race Condition in QA

**Problem:** The dev server gets 90 seconds to boot (line 104 in shell_tools.py), and the QA screenshot is taken immediately after the HTTP health check passes. But many React/Next.js apps return a 200 with a loading spinner before the actual content renders (which is exactly what happened in issue 327 — the screenshot showed "Loading...").

**Fix:** After the health check passes, wait an additional configurable delay (e.g., 5-10 seconds) for client-side hydration before taking the screenshot. Better yet, add a `waitUntil: 'networkidle'` option to the Playwright screenshot capture.

---

### 9. Worker Has No Crash Recovery / Heartbeat

**Problem:** If the worker process crashes mid-stage, the job is stuck in "claimed" state forever. The 1-hour lease timeout eventually releases it, but there's no proactive detection.

**Fix:** 
- Add a heartbeat mechanism: worker updates a timestamp every N seconds while processing
- Add a reaper: dashboard or a cron job checks for jobs claimed > 2x expected duration and marks them as failed for retry
- Worker startup should check for and reclaim any orphaned jobs

---

## Nice-to-Have Improvements 🟢

### 10. Configurable Stage Skip

Allow certain stages to be skipped via config (e.g., skip code review for simple issues, skip QA for non-UI changes). The triage agent already identifies complexity — use it.

### 11. Parallel Agent Execution

Triage and planning currently run sequentially but could overlap. The reviewer and QA screenshot capture are independent and could run concurrently.

### 12. Dashboard Webhook Activity Log

The dashboard shows run status but no raw activity log. Adding a scrollable event timeline per run would make debugging much easier.

### 13. PR Auto-Merge on Low Complexity

For issues classified as "low" complexity with passing tests and approved QA, auto-merge the PR instead of waiting for manual approval.

---

## Recommended Priority Order

| Priority | Item | Effort | Impact |
|----------|------|--------|--------|
| **P0** | #1 Fix blind reviewer | Small | High — reviewer is useless without code context |
| **P0** | #4 Enrich PR body with evidence | Small | High — PR quality directly visible to founder |
| **P0** | #8 Fix dev server hydration race | Small | High — root cause of "Loading..." QA screenshots |
| **P1** | #2 Builder fix context | Medium | High — prevents oscillating fix loops |
| **P1** | #6 Accumulate error history | Small | Medium — improves self-healing success rate |
| **P1** | #5 Pre-read files for planner | Medium | Medium — improves plan quality |
| **P2** | #3 Approval timeout/reminders | Medium | Medium — prevents abandoned runs |
| **P2** | #7 Lint gate | Medium | Medium — catches issues tests miss |
| **P2** | #9 Worker crash recovery | Medium | Medium — operational reliability |
| **P3** | #10-13 Nice-to-haves | Varies | Lower |

---

## Open Questions

> [!IMPORTANT]
> **Which items would you like me to implement?** I can tackle the P0 items immediately (they're all small, targeted changes), or I can work through the full P0+P1 set. Let me know your preference.

> [!NOTE]
> The QA interactive testing upgrade we just completed addresses one of the biggest gaps. These recommendations build on top of that foundation to make every other stage equally capable.
