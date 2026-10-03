"""Settings: read, preview (diff) and save, all validated in the backend."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from backend.core.config import Settings, get_settings
from backend.services.settings_editor import (
    SettingsChange,
    SettingsError,
    current,
    plan_changes,
    save_changes,
)

router = APIRouter(prefix="/settings", tags=["settings"])
SettingsDep = Annotated[Settings, Depends(get_settings)]


def backup_dir_dep() -> Any:
    from backend.services.settings_editor import BACKUP_DIR

    return BACKUP_DIR


@router.get("")
def read(settings: SettingsDep) -> dict[str, Any]:
    return current(settings)


@router.post("/preview")
def preview(change: SettingsChange, settings: SettingsDep) -> dict[str, Any]:
    """What would change, as a diff per file. Nothing is written."""
    try:
        files = plan_changes(settings, change)
    except SettingsError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"files": [{"name": f.name, "diff": f.diff()} for f in files]}


@router.post("")
def save(
    change: SettingsChange,
    settings: SettingsDep,
    backup_dir: Annotated[Any, Depends(backup_dir_dep)],
) -> dict[str, Any]:
    """Validate, back up the old files, save. Takes effect on the next run or page load."""
    try:
        files = save_changes(settings, change, backup_dir)
    except SettingsError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {
        "saved": [f.name for f in files],
        "files": [{"name": f.name, "diff": f.diff()} for f in files],
    }
