from enum import Enum
from typing import List, Optional
from pydantic import BaseModel, Field

class WorkflowStatus(str, Enum):
    TRIAGE = "triage"
    PLANNING = "planning"
    AWAIT_PLAN_APPROVAL = "await_plan_approval"
    BUILDING = "building"
    TESTING = "testing"
    REVIEWING = "reviewing"
    QA = "qa"
    AWAIT_QA_APPROVAL = "await_qa_approval"
    DEPLOYING = "deploying"
    AWAIT_PR_APPROVAL = "await_pr_approval"
    MERGED = "merged"
    FAILED = "failed"

class IssueContext(BaseModel):
    number: int
    title: str
    body: Optional[str] = ""
    creator: str
    labels: List[str] = []
    repository: str
    classification: Optional[str] = "bug"  # bug, feature, enhancement
    affected_files: List[str] = []
    complexity: Optional[str] = "medium"  # low, medium, high

class PlanStep(BaseModel):
    step_number: int
    description: str
    files_affected: List[str] = []
    status: str = "pending"  # pending, completed, failed

class ImplementationPlanModel(BaseModel):
    summary: str
    steps: List[PlanStep] = []
    approved: bool = False
    feedback: Optional[str] = ""

class TestOutcome(BaseModel):
    test_name: str
    passed: bool
    output: Optional[str] = ""

class TestResultsModel(BaseModel):
    passed: bool
    outcomes: List[TestOutcome] = []
    coverage: Optional[float] = 0.0
    screenshot_paths: List[str] = []

class QAReportModel(BaseModel):
    passed: bool
    summary: str
    screenshots: List[str] = []
    approved: bool = False
    feedback: Optional[str] = ""

class WorkflowStateModel(BaseModel):
    session_id: str
    issue: IssueContext
    status: WorkflowStatus = WorkflowStatus.TRIAGE
    branch_name: Optional[str] = ""
    test_command: Optional[str] = None
    plan: Optional[ImplementationPlanModel] = None
    test_results: Optional[TestResultsModel] = None
    qa_report: Optional[QAReportModel] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    error_message: Optional[str] = None
    # Stage identifier ("triage", "planning", "building", "testing", "qa",
    # "deploy") recorded when status becomes FAILED, so retries resume at the
    # right stage without parsing error message text
    failed_stage: Optional[str] = None
