"""Job-related emails pulled from Gmail."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, Float, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.base import Base, TimestampMixin, UUIDPrimaryKeyMixin, enum_column
from database.enums import EmailClassification

if TYPE_CHECKING:
    from database.models.application import Application


class Email(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "emails"

    provider_message_id: Mapped[str] = mapped_column(unique=True)
    thread_id: Mapped[str | None] = mapped_column(index=True)
    sender: Mapped[str] = mapped_column(String(512))
    subject: Mapped[str] = mapped_column(String(1024))
    snippet: Mapped[str | None] = mapped_column(Text)
    body_text: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(index=True)

    classification: Mapped[EmailClassification] = mapped_column(
        enum_column(EmailClassification), default=EmailClassification.UNCLASSIFIED
    )
    company_name: Mapped[str | None]
    role: Mapped[str | None]
    application_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("applications.id", ondelete="SET NULL"), index=True
    )
    match_confidence: Mapped[float | None] = mapped_column(Float)
    # Low-confidence matches are never auto-linked; they wait for the user.
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    interpretation: Mapped[str | None] = mapped_column(Text)
    action_required: Mapped[str | None] = mapped_column(Text)
    processed_at: Mapped[datetime | None]

    application: Mapped[Application | None] = relationship(back_populates="emails")
