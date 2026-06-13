import os
import hmac
import json
import hashlib
import logging
from typing import Dict, Any
from fastapi import APIRouter, Header, Request, HTTPException
from pydantic import BaseModel
from google.adk import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService

from founderscrew.orchestrator import Orchestrator
from founderscrew.agents.qa_agent import get_qa_agent
from founderscrew.config import settings

logger = logging.getLogger("founderscrew.webhook")
router = APIRouter()
orchestrator = Orchestrator()

class JsonRpcRequest(BaseModel):
    jsonrpc: str
    method: str
    params: Dict[str, Any]
    id: Any

@router.post("/webhook/github")
async def github_webhook(
    request: Request,
    x_github_event: str = Header(None)
):
    """GitHub webhook receiver endpoint. Processes issue labeling and commenting."""
    if not x_github_event:
        raise HTTPException(status_code=400, detail="Missing X-GitHub-Event header")

    body = await request.body()

    # Verify the GitHub HMAC signature when a webhook secret is configured.
    # Without this, anyone who discovers the public URL could forge approval
    # comments and trigger builds/deployments.
    secret = os.getenv("GITHUB_WEBHOOK_SECRET") or settings.get("github.webhook_secret")
    if secret:
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(str(secret).encode(), body, hashlib.sha256).hexdigest()
        if not signature or not hmac.compare_digest(signature, expected):
            logger.warning("Rejected webhook delivery with missing/invalid X-Hub-Signature-256.")
            raise HTTPException(status_code=401, detail="Invalid webhook signature")
    else:
        logger.warning("GITHUB_WEBHOOK_SECRET is not configured; webhook deliveries are NOT verified.")

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")
    
    # 1. Handle issue labeled (trigger crew:ready)
    if x_github_event == "issues":
        action = payload.get("action")
        issue = payload.get("issue", {})
        repo = payload.get("repository", {})
        repo_name = repo.get("full_name")
        issue_number = issue.get("number")
        
        # Check for label added
        if action == "labeled":
            label_name = payload.get("label", {}).get("name")
            # Default to crew:ready trigger label
            trigger_label = settings.get("github.trigger_label", "crew:ready")
            
            if label_name == trigger_label:
                sender = payload.get("sender", {}).get("login", "unknown")
                logger.info(f"Trigger label '{label_name}' detected on issue #{issue_number}")
                await orchestrator.handle_issue_labeled(repo_name, issue_number, sender)
                return {"status": "triggered", "session_id": f"{repo_name.replace('/', '_')}_{issue_number}"}

    # 2. Handle issue comments (founder approvals)
    elif x_github_event == "issue_comment":
        action = payload.get("action")
        comment = payload.get("comment", {})
        issue = payload.get("issue", {})
        repo = payload.get("repository", {})
        repo_name = repo.get("full_name")
        issue_number = issue.get("number")
        
        if action == "created":
            commenter = comment.get("user", {}).get("login")
            comment_body = comment.get("body", "")
            logger.info(f"Comment received on issue #{issue_number} from {commenter}")
            await orchestrator.handle_comment_created(repo_name, issue_number, comment_body, commenter)
            return {"status": "processing_comment"}
            
    return {"status": "ignored"}

@router.post("/api/v1/a2a/qa")
async def a2a_qa_endpoint(rpc_req: JsonRpcRequest):
    """A2A protocol REST endpoint for the QA Agent using JSON-RPC 2.0."""
    if rpc_req.jsonrpc != "2.0":
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32600, "message": "Invalid Request: must be jsonrpc 2.0"},
            "id": rpc_req.id
        }
        
    if rpc_req.method != "execute_qa":
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32601, "message": f"Method not found: {rpc_req.method}"},
            "id": rpc_req.id
        }
        
    params = rpc_req.params
    url = params.get("url")
    if not url:
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32602, "message": "Invalid params: 'url' is required"},
            "id": rpc_req.id
        }
        
    # Execute QA Agent logic
    try:
        # Invoke agent locally to perform visual checks
        agent = get_qa_agent()
        session_service = InMemorySessionService()
        runner = Runner(agent=agent, session_service=session_service, app_name="founders-crew", auto_create_session=True)
        
        output = ""
        async for event in runner.run_async(
            user_id="a2a_caller",
            session_id="a2a_qa_run",
            state_delta={"input": f"{url}"}
        ):
            if event.output is not None:
                output = event.output
                
        # Parse result
        import re, json
        match = re.search(r"```json\s*(.*?)\s*```", output, re.DOTALL)
        json_str = match.group(1).strip() if match else output.strip()
        data = json.loads(json_str)
        
        return {
            "jsonrpc": "2.0",
            "result": {
                "passed": data.get("passed", True),
                "similarity_percentage": data.get("similarity_percentage", 100.0),
                "observations": data.get("observations", "Passed visual validation checks.")
            },
            "id": rpc_req.id
        }
    except Exception as e:
        return {
            "jsonrpc": "2.0",
            "error": {"code": -32000, "message": f"Internal agent error: {e}"},
            "id": rpc_req.id
        }
