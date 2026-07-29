"""Small, explicit catalog helpers for locally uploaded video materials."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from typing import Iterable
import unicodedata

from PIL import Image

from app.utils import file_security, utils


CATALOG_FILE_NAME = ".material_catalog.json"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".flv", ".mkv"}
PUBLIC_DOMAIN_SOURCES = {
    "usgs": {
        "label": "USGS",
        "license": "USGS public domain (unless otherwise indicated)",
        "attribution": "U.S. Geological Survey",
    },
    "nps_yellowstone": {
        "label": "NPS Yellowstone",
        "license": "NPS Yellowstone public domain (when verified on the source page)",
        "attribution": "National Park Service",
    },
}


def _library_dir(storage_dir: str | os.PathLike[str] | None = None) -> Path:
    if storage_dir is None:
        directory = Path(utils.storage_dir("local_videos", create=True))
    else:
        directory = Path(storage_dir)
        directory.mkdir(parents=True, exist_ok=True)
    return directory.resolve()


def _material_kind(path: Path) -> str | None:
    suffix = path.suffix.casefold()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    return None


def _normalise_tag(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    return " ".join(text.split())


def _normalise_tags(tags: Iterable[object] | str | None) -> list[str]:
    if isinstance(tags, str):
        tags = tags.split(",")
    if not tags:
        return []

    normalised = []
    seen = set()
    for raw_tag in tags:
        tag = _normalise_tag(raw_tag)
        key = tag.casefold()
        if not tag or len(tag) > 80 or key in seen:
            continue
        normalised.append(tag)
        seen.add(key)
        if len(normalised) == 24:
            break
    return normalised


def _source_record(source_id: object) -> dict[str, str] | None:
    if not isinstance(source_id, str):
        return None
    source = PUBLIC_DOMAIN_SOURCES.get(source_id)
    if source is None:
        return None
    return {"id": source_id, **source}


def list_public_domain_sources() -> list[dict[str, str]]:
    """List known source labels for locally curated public-domain materials."""
    return [
        _source_record(source_id)
        for source_id in PUBLIC_DOMAIN_SOURCES
        if _source_record(source_id) is not None
    ]


def _read_catalog(directory: Path) -> dict[str, dict[str, object]]:
    catalog_path = directory / CATALOG_FILE_NAME
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}

    if not isinstance(data, dict):
        return {}

    catalog = {}
    for file_name, entry in data.items():
        if not isinstance(file_name, str) or file_name != os.path.basename(file_name):
            continue
        raw_tags = entry.get("tags") if isinstance(entry, dict) else entry
        catalog_entry: dict[str, object] = {"tags": _normalise_tags(raw_tags)}
        source = _source_record(entry.get("source_id") if isinstance(entry, dict) else None)
        if source is not None:
            catalog_entry["source_id"] = source["id"]
        if catalog_entry["tags"] or source is not None:
            catalog[file_name] = catalog_entry
    return catalog


def _write_catalog(directory: Path, catalog: dict[str, dict[str, object]]) -> None:
    catalog_path = directory / CATALOG_FILE_NAME
    payload = {}
    for file_name, catalog_entry in sorted(
        catalog.items(), key=lambda item: item[0].casefold()
    ):
        tags = _normalise_tags(catalog_entry.get("tags"))
        source = _source_record(catalog_entry.get("source_id"))
        entry = {}
        if tags:
            entry["tags"] = tags
        if source is not None:
            entry["source_id"] = source["id"]
        if entry:
            payload[file_name] = entry
    descriptor, temporary_path = tempfile.mkstemp(
        dir=directory,
        prefix=".material_catalog_",
        suffix=".tmp",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary_file:
            json.dump(payload, temporary_file, ensure_ascii=False, indent=2, sort_keys=True)
            temporary_file.write("\n")
        os.replace(temporary_path, catalog_path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _resolve_catalog_material(directory: Path, file_name: str) -> Path:
    if not isinstance(file_name, str) or not file_name or file_name != os.path.basename(file_name):
        raise ValueError("material name must be a file name inside the local library")
    resolved = Path(
        file_security.resolve_path_within_directory(str(directory), file_name)
    )
    if _material_kind(resolved) is None:
        raise ValueError("unsupported local material type")
    return resolved


def check_local_material_health(file_path: str | os.PathLike[str]) -> dict[str, object]:
    """Return a small, non-destructive readability result for one local file."""
    path = Path(file_path)
    kind = _material_kind(path)
    if not path.is_file() or kind is None:
        return {"ok": False, "detail": "Material file is unavailable"}

    if kind == "image":
        try:
            with Image.open(path) as image:
                image.verify()
        except (OSError, ValueError):
            return {"ok": False, "detail": "Image file could not be read"}
        return {"ok": True, "detail": "Image file found"}

    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "detail": "Video stream could not be read"}

    if result.returncode == 0 and "video" in (result.stdout or "").casefold():
        return {"ok": True, "detail": "Video stream found"}
    return {"ok": False, "detail": "Video stream could not be read"}


def list_local_materials(
    storage_dir: str | os.PathLike[str] | None = None,
    *,
    check_health: bool = False,
) -> list[dict[str, object]]:
    """List supported files in the local-material directory without selecting them."""
    directory = _library_dir(storage_dir)
    catalog = _read_catalog(directory)
    entries = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        kind = _material_kind(path)
        if kind is None:
            continue

        health_result = check_local_material_health(path) if check_health else None
        catalog_entry = catalog.get(path.name, {})
        source = _source_record(catalog_entry.get("source_id"))
        entries.append(
            {
                "name": path.name,
                "path": str(path.resolve()),
                "kind": kind,
                "size_bytes": path.stat().st_size,
                "tags": catalog_entry.get("tags", []),
                "source_id": source["id"] if source is not None else None,
                "source_label": source["label"] if source is not None else "",
                "license": source["license"] if source is not None else "",
                "attribution": source["attribution"] if source is not None else "",
                "health": health_result["ok"] if health_result else None,
                "health_detail": health_result["detail"] if health_result else "",
            }
        )
    return sorted(entries, key=lambda entry: str(entry["name"]).casefold())


def save_local_material_tags(
    file_name: str,
    tags: Iterable[object] | str | None,
    storage_dir: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Persist manually supplied tags for one existing local material file."""
    directory = _library_dir(storage_dir)
    material_path = _resolve_catalog_material(directory, file_name)
    catalog = _read_catalog(directory)
    normalised_tags = _normalise_tags(tags)
    catalog_entry = catalog.get(material_path.name, {})
    if normalised_tags:
        catalog_entry["tags"] = normalised_tags
        catalog[material_path.name] = catalog_entry
    else:
        catalog_entry.pop("tags", None)
        if catalog_entry:
            catalog[material_path.name] = catalog_entry
        else:
            catalog.pop(material_path.name, None)
    _write_catalog(directory, catalog)
    return normalised_tags


def save_local_material_source(
    file_name: str,
    source_id: str | None,
    storage_dir: str | os.PathLike[str] | None = None,
) -> dict[str, str] | None:
    """Save or clear a verified public-domain source for one local material."""
    source = _source_record(source_id)
    if source_id is not None and source is None:
        raise ValueError("unsupported public-domain material source")

    directory = _library_dir(storage_dir)
    material_path = _resolve_catalog_material(directory, file_name)
    catalog = _read_catalog(directory)
    catalog_entry = catalog.get(material_path.name, {})
    if source is None:
        catalog_entry.pop("source_id", None)
    else:
        catalog_entry["source_id"] = source["id"]

    if catalog_entry:
        catalog[material_path.name] = catalog_entry
    else:
        catalog.pop(material_path.name, None)
    _write_catalog(directory, catalog)
    return source


def _search_tokens(value: object) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return {token for token in re.findall(r"[^\W_]+", text, flags=re.UNICODE) if len(token) > 1}


def recommend_local_materials(
    subject_or_keyword: str,
    storage_dir: str | os.PathLike[str] | None = None,
    *,
    limit: int = 5,
) -> list[dict[str, object]]:
    """Return tag/name matches as suggestions; callers decide whether to use them."""
    subject_tokens = _search_tokens(subject_or_keyword)
    if not subject_tokens or limit <= 0:
        return []

    recommendations = []
    for entry in list_local_materials(storage_dir=storage_dir):
        tag_tokens = _search_tokens(" ".join(entry["tags"]))
        name_tokens = _search_tokens(Path(str(entry["name"])).stem)
        score = len(subject_tokens & tag_tokens) * 3 + len(subject_tokens & name_tokens)
        if score <= 0:
            continue
        recommendations.append({**entry, "match_score": score})

    recommendations.sort(
        key=lambda entry: (-int(entry["match_score"]), str(entry["name"]).casefold())
    )
    return recommendations[:limit]
