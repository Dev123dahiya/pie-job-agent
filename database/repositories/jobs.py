from sqlalchemy import select

from database.models import Company, Job
from database.repositories.base import Repository


class CompanyRepository(Repository[Company]):
    model = Company

    def get_or_create(self, name: str) -> Company:
        normalized = Company.normalize_name(name)
        company = self.session.scalar(select(Company).where(Company.normalized_name == normalized))
        if company is None:
            company = self.add(Company(name=name.strip(), normalized_name=normalized))
        return company


class JobRepository(Repository[Job]):
    model = Job

    def get_by_source_id(self, source: str, source_job_id: str) -> Job | None:
        stmt = select(Job).where(Job.source == source, Job.source_job_id == source_job_id)
        return self.session.scalar(stmt)
