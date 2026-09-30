"""No personal data in committed files.

Reads the real (git-ignored) config/user_profile.yaml and checks that none of its identity
and contact values appear in any file git tracks or would add. Skipped when the profile or
git is not available (e.g. on a fresh clone). Failure messages name the file and the kind of
value only, never the value itself.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = REPO_ROOT / "config" / "user_profile.yaml"


def _personal_values() -> dict[str, str]:
    facts = (yaml.safe_load(PROFILE.read_text(encoding="utf-8")) or {}).get(
        "verified_profile"
    ) or {}
    values: dict[str, str] = {}
    name = str(facts.get("name") or "").strip()
    if name:
        values["full name"] = name
        surname = name.split()[-1]
        if len(surname) >= 5:
            values["surname"] = surname
    if facts.get("email"):
        values["email"] = str(facts["email"])
    phone = re.sub(r"\D", "", str(facts.get("phone") or ""))
    if len(phone) >= 10:
        values["phone"] = phone[-10:]
    for kind, link in (facts.get("links") or {}).items():
        handle = str(link).rstrip("/").split("/")[-1]
        if len(handle) >= 5:
            values[f"{kind} handle"] = handle
    # Where you work and study: "employer 1" means the first work_experience entry.
    for i, job in enumerate(facts.get("work_experience") or [], 1):
        brand = re.split(r"[\s.,]", str(job.get("company") or "").strip())[0]
        if len(brand) >= 5:
            values[f"employer {i}"] = brand
    for i, edu in enumerate(facts.get("education") or [], 1):
        if len(str(edu.get("institution") or "")) >= 5:
            values[f"institution {i}"] = str(edu["institution"])
    return values


def _files_git_would_commit() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [REPO_ROOT / line for line in out.splitlines() if line]


@pytest.mark.skipif(not PROFILE.exists(), reason="no real user profile on this machine")
@pytest.mark.skipif(
    shutil.which("git") is None or not (REPO_ROOT / ".git").exists(), reason="not a git checkout"
)
def test_no_personal_data_in_committed_files() -> None:
    values = _personal_values()
    assert values, "the profile has no identity values to check"
    # Whole words only, so a company named like an ordinary English word does not match
    # inside a longer word in code or comments.
    patterns = {
        kind: re.compile(rf"(?<![a-z0-9]){re.escape(v.lower())}(?![a-z0-9])")
        for kind, v in values.items()
    }
    leaks: list[str] = []
    for path in _files_git_would_commit():
        if path == PROFILE or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        rel = path.relative_to(REPO_ROOT)
        leaks += [f"{rel}: {kind}" for kind, p in patterns.items() if p.search(text)]
    assert not leaks, "personal data in committed files:\n" + "\n".join(leaks)
