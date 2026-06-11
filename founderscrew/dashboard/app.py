import os
import yaml
from pathlib import Path
from fastapi import FastAPI, Request, Form, HTTPException, Depends
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets

security = HTTPBasic()

def auth_required(credentials: HTTPBasicCredentials = Depends(security)):
    """Verifies basic authentication against DASHBOARD_PASSWORD if set."""
    # Check if a password is configured
    configured_password = os.environ.get("DASHBOARD_PASSWORD") or settings.get("dashboard.password")
    
    # If no password is set, allow access
    if not configured_password:
        return True
        
    # Check admin credentials using constant time comparison
    is_correct_admin = secrets.compare_digest(credentials.username, "admin")
    is_correct_admin_pass = secrets.compare_digest(credentials.password, configured_password)
    
    # Check demo judge credentials
    is_correct_judge = secrets.compare_digest(credentials.username, "judge")
    is_correct_judge_pass = secrets.compare_digest(credentials.password, "demo-judge-2026")
    
    is_admin_auth = is_correct_admin and is_correct_admin_pass
    is_judge_auth = is_correct_judge and is_correct_judge_pass
    
    if not (is_admin_auth or is_judge_auth):
        raise HTTPException(
            status_code=401,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Basic"},
        )
    return True

from founderscrew.config import settings, CONFIG_FILE
from founderscrew.state.store import StateStore
from founderscrew.state.models import WorkflowStatus
from founderscrew.webhook.server import router as webhook_router

app = FastAPI(title="Founders.crew Dashboard")

# Include webhook & A2A routes
app.include_router(webhook_router)

# Resolve templates and static directories
BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Mount static files if directory exists
static_dir = BASE_DIR / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

store = StateStore()

@app.get("/.well-known/agent-card.json", response_class=JSONResponse)
async def agent_card():
    """A2A Agent Card — standard discovery endpoint for agent interoperability."""
    return {
        "name": "Founders.crew QA Agent",
        "description": "Visual QA verification agent that captures screenshots and compares them against reference images to detect UI regressions. Part of the Founders.crew multi-agent DevOps system.",
        "version": "0.1.0",
        "provider": {
            "organization": "Founders.crew",
            "url": "https://github.com/The-Week-In-Fantasy/founderscrew"
        },
        "capabilities": {
            "methods": ["execute_qa"],
            "protocols": ["jsonrpc-2.0"]
        },
        "endpoints": {
            "a2a": "/api/v1/a2a/qa"
        },
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL of the page to visually validate."
                }
            },
            "required": ["url"]
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "passed": {"type": "boolean"},
                "similarity_percentage": {"type": "number"},
                "observations": {"type": "string"}
            }
        },
        "models": ["gemini-2.5-flash", "gemini-2.5-pro"],
        "runtime": "Google Cloud Run",
        "framework": "Google Agent Development Kit (ADK)"
    }

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(auth_required)])
async def home(request: Request):
    """Renders the dashboard main interface."""
    states = store.list_states()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"states": states}
    )

@app.get("/run/{session_id}", response_class=HTMLResponse, dependencies=[Depends(auth_required)])
async def run_detail(request: Request, session_id: str):
    """Renders the detailed view of a specific agent run."""
    state = store.load_state(session_id)
    if not state:
        raise HTTPException(status_code=404, detail="Session run not found")
    return templates.TemplateResponse(
        request,
        "issue_detail.html",
        {"state": state}
    )

@app.post("/run/{session_id}/approve", response_class=RedirectResponse, dependencies=[Depends(auth_required)])
async def approve_step(session_id: str, step_type: str = Form(...)):
    """Handles manual approval buttons from the dashboard UI."""
    state = store.load_state(session_id)
    if not state:
        raise HTTPException(status_code=404, detail="Session run not found")
        
    repo_name = state.issue.repository
    issue_number = state.issue.number
    
    # Trigger orchestrator handlers directly
    from founderscrew.webhook.server import orchestrator
    if step_type == "plan":
        await orchestrator.handle_comment_created(
            repo_name=repo_name,
            issue_number=issue_number,
            comment_body="approve",
            commenter="dashboard_user"
        )
    elif step_type == "qa":
        await orchestrator.handle_comment_created(
            repo_name=repo_name,
            issue_number=issue_number,
            comment_body="approve",
            commenter="dashboard_user"
        )
        
    return RedirectResponse(url=f"/run/{session_id}", status_code=303)

@app.post("/run/trigger", response_class=RedirectResponse, dependencies=[Depends(auth_required)])
async def trigger_run(issue_number: int = Form(...)):
    """Manually triggers the orchestrator flow for a GitHub issue by number."""
    repo = settings.get("github.repository")
    if not repo:
        raise HTTPException(status_code=400, detail="Repository not configured in settings")
        
    from founderscrew.webhook.server import orchestrator
    import asyncio
    asyncio.create_task(
        orchestrator.handle_issue_labeled(repo, issue_number, "dashboard_user")
    )
    
    session_id = f"{repo.replace('/', '_')}_{issue_number}"
    return RedirectResponse(url=f"/run/{session_id}", status_code=303)

@app.post("/run/{session_id}/retry", response_class=RedirectResponse, dependencies=[Depends(auth_required)])
async def retry_run(session_id: str):
    """Retries a failed run from the stage where it failed."""
    from founderscrew.webhook.server import orchestrator
    await orchestrator.resume_failed_workflow(session_id)
    return RedirectResponse(url=f"/run/{session_id}", status_code=303)

@app.post("/run/{session_id}/replan", response_class=RedirectResponse, dependencies=[Depends(auth_required)])
async def replan_run(session_id: str, feedback: str = Form("")):
    """Re-runs the planner with user feedback for plan refinement."""
    from founderscrew.webhook.server import orchestrator
    await orchestrator.replan_with_feedback(session_id, feedback)
    return RedirectResponse(url=f"/run/{session_id}", status_code=303)

@app.post("/run/{session_id}/restart", response_class=RedirectResponse, dependencies=[Depends(auth_required)])
async def restart_stage(session_id: str, target_stage: str = Form(...)):
    """Restarts the workflow from a specific stage."""
    from founderscrew.webhook.server import orchestrator
    await orchestrator.restart_from_stage(session_id, target_stage)
    return RedirectResponse(url=f"/run/{session_id}", status_code=303)

@app.get("/logs", response_class=HTMLResponse, dependencies=[Depends(auth_required)])
async def get_logs(request: Request):
    """Renders the system operations log viewer."""
    return templates.TemplateResponse(
        request,
        "logs.html",
        {}
    )

@app.get("/logs/content", response_class=HTMLResponse, dependencies=[Depends(auth_required)])
async def get_logs_content():
    """Returns the tail end of the log file formatted as colored HTML."""
    from pathlib import Path
    from collections import deque
    
    log_file = Path.home() / ".founderscrew" / "logs" / "founderscrew.log"
    if not log_file.exists():
        return "<div style='color: var(--text-muted); font-family: monospace;'>No log file found yet. Operations will be logged once activities occur.</div>"
    
    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = deque(f, 200)
            
        formatted_lines = []
        for line in lines:
            escaped_line = line.replace("<", "&lt;").replace(">", "&gt;")
            if "ERROR" in escaped_line:
                escaped_line = f"<span style='color: var(--danger); font-weight: bold;'>{escaped_line}</span>"
            elif "WARNING" in escaped_line:
                escaped_line = f"<span style='color: var(--warning); font-weight: bold;'>{escaped_line}</span>"
            elif "INFO" in escaped_line:
                if "succeeded" in escaped_line or "Approved" in escaped_line or "success" in escaped_line:
                    escaped_line = f"<span style='color: var(--success);'>{escaped_line}</span>"
                else:
                    escaped_line = f"<span style='color: #a1a1aa;'>{escaped_line}</span>"
            formatted_lines.append(escaped_line)
            
        return f"<pre style='margin: 0; white-space: pre-wrap; font-size: 0.85rem; line-height: 1.5;'>{''.join(formatted_lines)}</pre>"
    except Exception as e:
        return f"<div style='color: var(--danger); font-family: monospace;'>Error reading log file: {e}</div>"

@app.get("/env", response_class=HTMLResponse, dependencies=[Depends(auth_required)])
async def get_env(request: Request):
    """Renders the environment variables manager."""
    env_vars = settings.get("workspace_env", {})
    masked_env = {k: "********" for k in env_vars.keys()}
    return templates.TemplateResponse(
        request,
        "env.html",
        {"env_vars": masked_env}
    )

@app.post("/env", response_class=HTMLResponse, dependencies=[Depends(auth_required)])
async def post_env(request: Request):
    """Saves environment variables to secure config."""
    form = await request.form()
    keys = form.getlist("env_keys")
    values = form.getlist("env_values")
    
    old_env = settings.get("workspace_env", {})
    new_env = {}
    for k, v in zip(keys, values):
        k = k.strip()
        if k:
            if v == "********" and k in old_env:
                new_env[k] = old_env[k]
            else:
                new_env[k] = v
            
    settings.set("workspace_env", new_env)
    settings.save()
    
    masked_env = {k: "********" for k in new_env.keys()}
    return templates.TemplateResponse(
        request,
        "env.html",
        {"env_vars": masked_env, "message": "Environment variables securely saved."}
    )

@app.get("/settings", response_class=HTMLResponse, dependencies=[Depends(auth_required)])
async def get_settings(request: Request):
    """Renders the web configuration settings form."""
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"config": settings.config}
    )

@app.post("/settings", response_class=HTMLResponse, dependencies=[Depends(auth_required)])
async def post_settings(
    request: Request,
    repo: str = Form(...),
    trigger_label: str = Form(...),
    base_branch: str = Form("main"),
    mode: str = Form(...),
    coding_tier1: str = Form(None),
    coding_tier2: str = Form(None),
    coding_tier3: str = Form(None),
    preferred_tool: str = Form(None),
    fallback_tool: str = Form(None),
    fast_tier1: str = Form(None),
    fast_tier2: str = Form(None),
    fast_tier3: str = Form(None),
    fast_model: str = Form(None),
    planning_tier1: str = Form(None),
    planning_tier2: str = Form(None),
    planning_tier3: str = Form(None),
    planning_model: str = Form(None)
):
    """Saves updated configuration from the settings form."""
    t1 = coding_tier1 or preferred_tool or "claude"
    t2 = coding_tier2 or fallback_tool or "cursor"
    t3 = coding_tier3 or "gemini"
    
    f1 = fast_tier1 or fast_model or "gemini-2.5-flash"
    f2 = fast_tier2 or "gemini-2.5-pro"
    f3 = fast_tier3 or "openai/gpt-4o-mini"
    
    p1 = planning_tier1 or planning_model or "gemini-2.5-pro"
    p2 = planning_tier2 or "gemini-2.5-flash"
    p3 = planning_tier3 or "anthropic/claude-3-5-sonnet"
    
    settings.set("github.repository", repo)
    settings.set("github.trigger_label", trigger_label)
    settings.set("github.base_branch", base_branch)
    settings.set("coding_tools.tier1", t1)
    settings.set("coding_tools.tier2", t2)
    settings.set("coding_tools.tier3", t3)
    settings.set("coding_tools.mode", mode)
    settings.set("agents.fast_tier1", f1)
    settings.set("agents.fast_tier2", f2)
    settings.set("agents.fast_tier3", f3)
    settings.set("agents.planning_tier1", p1)
    settings.set("agents.planning_tier2", p2)
    settings.set("agents.planning_tier3", p3)
    
    # Keep backward compatibility
    settings.set("agents.planning_model", p1)
    settings.set("agents.fast_model", f1)
    settings.set("coding_tools.preferred", t1)
    settings.set("coding_tools.fallback", t2)
    
    # Save to file
    settings.save()
    
    return templates.TemplateResponse(
        request,
        "settings.html",
        {"config": settings.config, "message": "Configuration saved successfully!"}
    )
