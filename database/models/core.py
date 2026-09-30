"""User and Company."""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from backend.core.normalize import normalize_company
from database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from database.models.job import Job
    from database.models.resume import Resume


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """The owner of the agent. Detailed profile data lives in config/user_profile.yaml."""

    __tablename__ = "users"

    name: Mapped[str]
    email: Mapped[str] = mapped_column(String(320), unique=True)

    resumes: Mapped[list[Resume]] = relationship(back_populates="user")


class Company(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "companies"

    name: Mapped[str]
    # Lower-cased, whitespace-collapsed name used for dedupe and email matching.
    normalized_name: Mapped[str] = mapped_column(unique=True, index=True)
    website: Mapped[str | None] = mapped_column(String(2048))
    careers_url: Mapped[str | None] = mapped_column(String(2048))
    notes: Mapped[str | None] = mapped_column(Text)

    jobs: Mapped[list[Job]] = relationship(back_populates="company")

    @staticmethod
    def normalize_name(name: str) -> str:
        return normalize_company(name)
