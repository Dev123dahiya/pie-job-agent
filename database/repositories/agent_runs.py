from sqlalchemy import select

from database.models import AgentRun
from database.repositories.base import Repository


class AgentRunRepository(Repository[AgentRun]):
    model = AgentRun

    def latest(self) -> AgentRun | None:
        return self.session.scalar(select(AgentRun).order_by(AgentRun.started_at.desc()).limit(1))
