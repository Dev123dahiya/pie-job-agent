"""Reading the backend's log files for the Logs page (REQUIREMENTS 19.2.11).

Source: data/logs/backend.log and its rotated copies (backend.log.1 ... .5), written by
backend/core/logging.py, which already redacts every line it writes. Lines are redacted again
on the way out, so a secret that slipped into an old file is still never shown or downloaded.
Filtering runs on the stored text and only the lines returned are redacted again: redacting
all ~200k lines on every request took close to a minute.

Filters:
- level: this level and above (DEBUG < INFO < WARNING < ERROR < CRITICAL)
- run: lines that mention the run id (agent lines carry "run=<id>")
- platform: lines that say "platform=<name>", or name the platform (Naukri, LinkedIn; for
  company sites the supported ATS names). Detected from the text, so it is a best effort.
- date: the line's date (the log's local time)
- q: text search, case-insensitive
"""

import re
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal

from backend.core.logging import redact
from backend.services.platforms import FORM_SOURCES, Platform

Level = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LEVELS: tuple[Level, ...] = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LINE = re.compile(
    r"^(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d{3} (?P<level>[A-Z]+)\s+(?P<logger>[^:]+): "
    r"(?P<message>.*)$"
)
_PLATFORM_WORDS: dict[Platform, re.Pattern[str]] = {
    "naukri": re.compile(r"(?i)\bnaukri\b"),
    "linkedin": re.compile(r"(?i)\blinked ?in\b"),
    "company_site": re.compile(
        r"(?i)platform=company_site|\b(" + "|".join(sorted(FORM_SOURCES - {"mock"})) + r")\b"
    ),
}


@dataclass
class LogLine:
    at: datetime
    level: str
    logger: str
    message: str  # includes continuation lines (tracebacks), redacted

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["at"] = self.at.isoformat()
        return data

    def text(self) -> str:
        stamp = self.at.strftime("%Y-%m-%d %H:%M:%S")
        return f"{stamp} {self.level:<8} {self.logger}: {self.message}"


@dataclass(frozen=True)
class LogFilter:
    q: str | None = None
    level: Level | None = None
    run: str | None = None
    platform: Platform | None = None
    date_from: date | None = None
    date_to: date | None = None

    def matches(self, line: LogLine) -> bool:
        if self.level and LEVELS.index(_level(line.level)) < LEVELS.index(self.level):
            return False
        if self.date_from and line.at.date() < self.date_from:
            return False
        if self.date_to and line.at.date() > self.date_to:
            return False
        if self.run and self.run.lower() not in line.message.lower():
            return False
        if self.platform:
            text = line.message
            if f"platform={self.platform}" not in text and not _PLATFORM_WORDS[
                self.platform
            ].search(text):
                return False
        if self.q:
            haystack = f"{line.logger} {line.message}".lower()
            if self.q.lower() not in haystack:
                return False
        return True


def _level(name: str) -> Level:
    return name if name in LEVELS else "INFO"  # type: ignore[return-value]


def log_files(log_dir: Path) -> list[Path]:
    """Oldest first: backend.log.5 ... backend.log.1, then backend.log."""
    rotated = sorted(
        (p for p in log_dir.glob("backend.log.*") if p.suffix[1:].isdigit()),
        key=lambda p: int(p.suffix[1:]),
        reverse=True,
    )
    current = log_dir / "backend.log"
    return rotated + ([current] if current.is_file() else [])


def _redacted(line: LogLine) -> LogLine:
    return LogLine(at=line.at, level=line.level, logger=line.logger, message=redact(line.message))


def read_lines(log_dir: Path) -> Iterator[LogLine]:
    """Every log entry, oldest first, as stored (callers redact what they return). Lines
    that don't start an entry (tracebacks) are appended to the entry before them."""
    for path in log_files(log_dir):
        entry: LogLine | None = None
        try:
            handle = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with handle:
            for raw in handle:
                raw = raw.rstrip("\r\n")
                m = _LINE.match(raw)
                if m:
                    if entry is not None:
                        yield entry
                    entry = LogLine(
                        at=datetime.fromisoformat(m["at"]),
                        level=m["level"],
                        logger=m["logger"].strip(),
                        message=m["message"],
                    )
                elif entry is not None and raw:
                    entry.message += "\n" + raw
        if entry is not None:
            yield entry


@dataclass
class LogPage:
    items: list[LogLine]
    total: int


def search(log_dir: Path, flt: LogFilter, limit: int, offset: int) -> LogPage:
    """Newest first."""
    matched = [line for line in read_lines(log_dir) if flt.matches(line)]
    matched.reverse()
    page = [_redacted(line) for line in matched[offset : offset + limit]]
    return LogPage(items=page, total=len(matched))


def export_text(log_dir: Path, flt: LogFilter, max_lines: int = 20_000) -> str:
    """The filtered log as a text file (oldest first, the newest max_lines), redacted."""
    matched = [line for line in read_lines(log_dir) if flt.matches(line)][-max_lines:]
    lines = [_redacted(line).text() for line in matched]
    return "\n".join(lines) + ("\n" if lines else "")
