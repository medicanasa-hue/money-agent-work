"""Export durable local workspace state without credentials or media."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import zipfile

from app.utils import utils


BACKUP_MANIFEST_FILENAME = "backup_manifest.json"

_DURABLE_STATE_FILES = (
    "history/production_history.json",
    "history/metrics_sync_run.json",
    "history/review_feedback.json",
    "local_videos/.material_catalog.json",
    "render_quality_baseline.json",
)
_EXCLUDED_PATHS = (
    "config.toml",
    "tasks/",
    "cache_videos/",
    "local_videos media files",
    "task-state/",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_within_root(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _safe_state_file(root: Path, candidate: Path) -> Path | None:
    if candidate.is_symlink() or not candidate.is_file():
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    return resolved if _is_within_root(resolved, root) else None


def _state_backup_sources(root: Path) -> list[tuple[Path, str]]:
    sources: list[tuple[Path, str]] = []
    for relative_path in _DURABLE_STATE_FILES:
        source = _safe_state_file(root, root / relative_path)
        if source is not None:
            sources.append((source, relative_path))

    presets_dir = root / "presets"
    if presets_dir.is_dir() and not presets_dir.is_symlink():
        for candidate in sorted(presets_dir.glob("*.json"), key=lambda item: item.name):
            source = _safe_state_file(root, candidate)
            if source is not None:
                sources.append((source, f"presets/{candidate.name}"))
    return sources


def default_state_backup_path(
    storage_root: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> str:
    """Return a timestamped default archive location under local storage."""
    root = Path(storage_root) if storage_root is not None else Path(utils.storage_dir())
    timestamp = (now or _utc_now()).astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return str(root / "backups" / f"mpt-state-{timestamp}.zip")


def export_state_backup(
    backup_path: str | Path | None = None,
    *,
    storage_root: str | Path | None = None,
    now: datetime | None = None,
) -> dict:
    """Export durable local state without configuration, credentials, or media.

    The archive is intentionally export-only. It includes history, review data,
    saved presets, local-material tags, and the render-quality baseline, but
    never generated media, video caches, transient task state, or config files.
    """
    root = Path(storage_root) if storage_root is not None else Path(utils.storage_dir())
    summary = {"ok": False, "archive": "", "files": [], "errors": []}
    if root.exists() and not root.is_dir():
        summary["errors"].append("storage root is unavailable")
        return summary

    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        summary["errors"].append("storage root is unavailable")
        return summary

    output = Path(backup_path) if backup_path else Path(
        default_state_backup_path(resolved_root, now=now)
    )
    if output.suffix.lower() != ".zip":
        summary["errors"].append("backup path must end in .zip")
        return summary

    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        summary["errors"].append("backup directory is unavailable")
        return summary

    sources = _state_backup_sources(resolved_root)
    manifest = {
        "version": 1,
        "created_at": (now or _utc_now()).astimezone(timezone.utc).isoformat(),
        "files": [archive_name for _, archive_name in sources],
        "excluded": list(_EXCLUDED_PATHS),
    }
    created = False
    try:
        with output.open("xb") as file_object:
            created = True
            with zipfile.ZipFile(
                file_object,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                for source, archive_name in sources:
                    archive.write(source, archive_name)
                archive.writestr(
                    BACKUP_MANIFEST_FILENAME,
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
    except FileExistsError:
        summary["errors"].append("backup already exists")
        return summary
    except (OSError, zipfile.BadZipFile):
        if created:
            try:
                output.unlink()
            except OSError:
                pass
        summary["errors"].append("state backup export failed")
        return summary

    summary.update(
        {
            "ok": True,
            "archive": str(output.resolve()),
            "files": manifest["files"],
        }
    )
    return summary
