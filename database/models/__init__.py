"""Importing this package registers every model on Base.metadata (needed by Alembic)."""

from database.models.agent import AgentAction, AgentRun
from database.models.application import Application, ApplicationEvent
from database.models.core import Company, User
from database.models.email import Email
from database.models.execution import (
    ApplicationAnswer,
    BrowserSession,
    ExternalActionLog,
    SubmissionAttempt,
)
from database.models.job import Job, JobAnalysis
from database.models.lead import EmployerBoard, JobLead
from database.models.resume import Resume, ResumeVersion

__all__ = [
    "AgentAction",
    "AgentRun",
    "Application",
    "ApplicationAnswer",
    "ApplicationEvent",
    "BrowserSession",
    "Company",
    "Email",
    "EmployerBoard",
    "ExternalActionLog",
    "Job",
    "JobAnalysis",
    "JobLead",
    "Resume",
    "ResumeVersion",
    "SubmissionAttempt",
    "User",
]
