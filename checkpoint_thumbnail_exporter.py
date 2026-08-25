from __future__ import annotations

import os
import re
import time
import asyncio
import hashlib
import json
import secrets
import shutil
import threading
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from aiohttp import web

try:
    from PIL import Image, ImageOps
except Exception as exc:  # pragma: no cover - ComfyUI normally includes Pillow.
    Image = None
    ImageOps = None
    _PIL_IMPORT_ERROR = exc
else:
    _PIL_IMPORT_ERROR = None

import folder_paths
from server import PromptServer

TOOL_NAME = "Checkpoint Thumbnail Exporter"
# TOOL_BUILD is only for reports / debugging. It is intentionally not written to JPEG comments.
TOOL_BUILD = "v1g"
# Stable comment schema used for managed thumbnail ownership checks.
COMMENT_SCHEMA = "cte_comment_v1"
MANAGED_MARKER = "managed=true"
TARGET_OGN = "OGN-ModelManager"

# OGN-ModelManager searches common sidecar image extensions next to model files.
OGN_THUMB_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg"}

# Pillow-readable image extensions used as source images. SVG is intentionally excluded.
SOURCE_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

# We currently create JPEG files only.
OUTPUT_EXT = ".jpg"

# uninstall_managed execute requires a token returned by a previous uninstall dry_run.
_CONFIRM_TOKENS: Dict[str, Dict[str, object]] = {}
_CONFIRM_TOKEN_TTL_SECONDS = 10 * 60


def _now_iso() -> str:
    # local time with offset if available from the platform.
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _clean_old_tokens() -> None:
    now = time.time()
    stale = [token for token, data in _CONFIRM_TOKENS.items() if now - float(data.get("created_at", 0)) > _CONFIRM_TOKEN_TTL_SECONDS]
    for token in stale:
        _CONFIRM_TOKENS.pop(token, None)


def _confirmation_key(payload: Dict[str, object]) -> Tuple[str, str]:
    # source_image_root is intentionally not included because uninstall does not need it.
    return (str(payload.get("operation", "")), str(payload.get("target_format", "")))


def _make_confirm_token(payload: Dict[str, object], managed_snapshot: Optional[Dict[str, Tuple[int, int, int, int]]] = None) -> str:
    _clean_old_tokens()
    token = secrets.token_urlsafe(18)
    _CONFIRM_TOKENS[token] = {
        "created_at": time.time(),
        "key": _confirmation_key(payload),
        "managed_snapshot": managed_snapshot or {},
    }
    return token


def _consume_confirm_token(token: str, payload: Dict[str, object]) -> Optional[Dict[str, object]]:
    _clean_old_tokens()
    if not token:
        return None
    data = _CONFIRM_TOKENS.pop(token, None)
    if not data:
        return None
    if tuple(data.get("key", ())) != _confirmation_key(payload):
        return None
    return data


def _file_identity(path: Path) -> Optional[Tuple[int, int, int, int]]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (int(stat.st_dev), int(stat.st_ino), int(stat.st_size), int(stat.st_mtime_ns))


def _confirmation_path(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:
        return os.path.normcase(str(path.absolute()))


def _sanitize_name(value: str) -> str:
    """Create a folder-safe checkpoint key compatible with common ckpt_name_safe usage."""
    value = value.replace("\\", "/")
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value)
    value = re.sub(r"_+", "_", value)
    value = value.strip(" ._")
    return value or "checkpoint"


def _unique(items: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _checkpoint_source_keys(relative_name: str) -> List[str]:
    """Return candidate source directory names for a checkpoint.

    The first key is the full relative checkpoint name without extension, sanitized.
    The second key is the basename stem, sanitized. This keeps subfolder support while
    still matching the simple source_root/<ckpt_name_safe>/ layout.
    """
    p = Path(relative_name)
    no_ext_posix = Path(relative_name).with_suffix("").as_posix()
    stem = p.stem
    return _unique([
        _sanitize_name(no_ext_posix),
        _sanitize_name(stem),
        stem,
    ])


def _get_source_root(source_image_root: str) -> Path:
    raw = (source_image_root or "").strip().strip('"')
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(folder_paths.get_output_directory()).resolve()


def _safe_stat_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _image_resampling_lanczos():
    try:
        return Image.Resampling.LANCZOS
    except AttributeError:  # Pillow < 9
        return Image.LANCZOS


@dataclass
class CheckpointRecord:
    relative_name: str
    path: Path
    source_keys: List[str]
    existing_thumbnail: Optional[Path] = None
    source_image: Optional[Path] = None


@dataclass
class ExportStats:
    checkpoints: int = 0
    existing_thumbnails: int = 0
    missing_thumbnails: int = 0
    would_install: int = 0
    installed: int = 0
    unmatched: int = 0
    managed_found: int = 0
    would_remove: int = 0
    removed: int = 0
    skipped_unmanaged: int = 0
    errors: int = 0
    examples: List[str] = field(default_factory=list)


ProgressCallback = Callable[[Dict[str, object]], None]

SCAN_PROGRESS_DOT_EVERY_FILES = 100
SCAN_PROGRESS_MAX_DOTS = 120

# Persistent source-image index. Bucket scans intentionally have no image-count
# limit: a capped scan must never become a trustworthy negative cache.
SOURCE_INDEX_SCHEMA_VERSION = 3
SOURCE_INDEX_DIRECTORY = "CheckpointThumbnailExporter"
SOURCE_INDEX_FILENAME = "source_index.json"
DATE_DIRECTORY_RE = re.compile(r"^[0-9]{8}$")
SOURCE_INDEX_SCAN_RETRIES = 3

# The HTTP endpoint runs exporter work in worker threads. Only one exporter may
# mutate thumbnails or the shared source index at a time. A process exit releases
# this in-memory lock automatically; normal exceptions release it in finally.
_EXPORTER_RUN_LOCK = threading.Lock()


def _send_progress(progress_cb: Optional[ProgressCallback], node_id: object, phase: str, current: int, total: int, status: str = "", current_name: str = "", report: str = "") -> None:
    if progress_cb is None:
        return
    progress_cb({
        "node_id": node_id,
        "phase": phase,
        "current": int(current),
        "total": int(total),
        "status": status,
        "current_name": current_name,
        "report": report,
    })


def _get_checkpoint_records() -> List[CheckpointRecord]:
    records: List[CheckpointRecord] = []
    for relative_name in folder_paths.get_filename_list("checkpoints"):
        full = folder_paths.get_full_path("checkpoints", relative_name)
        if not full:
            continue
        path = Path(full)
        if not path.exists():
            continue
        records.append(CheckpointRecord(
            relative_name=relative_name,
            path=path,
            source_keys=_checkpoint_source_keys(relative_name),
        ))
    return records


def _find_existing_sidecar_thumbnail(checkpoint_path: Path) -> Optional[Path]:
    parent = checkpoint_path.parent
    stem = checkpoint_path.stem
    try:
        children = list(parent.iterdir())
    except OSError:
        return None

    matches = [
        child for child in children
        if child.is_file()
        and child.stem == stem
        and child.suffix.lower() in OGN_THUMB_EXTS
    ]
    if not matches:
        return None

    priority = [".jpg", ".jpeg", ".png", ".webp", ".gif", ".svg"]
    matches.sort(key=lambda p: (priority.index(p.suffix.lower()) if p.suffix.lower() in priority else 99, p.name))
    return matches[0]


def _target_jpg_path(checkpoint_path: Path) -> Path:
    return checkpoint_path.with_suffix(OUTPUT_EXT)


def _is_source_image(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in SOURCE_IMAGE_EXTS


def _norm_for_match(value: str) -> str:
    """Normalize path/name text for case-insensitive ckpt_name_safe matching."""
    return _sanitize_name(value).casefold()


def _iter_source_match_candidates(path: Path, source_root: Path) -> Tuple[str, List[str], str, str]:
    """Return normalized matching surfaces for a source image.

    We intentionally match by substring because HandpickerSuite / GM Image Saver output
    layouts may be `prefix/date/label`, `prefix_label_date`, or filenames containing
    the checkpoint safe name. Exact directory segment matches are still preferred.
    """
    try:
        rel = path.relative_to(source_root)
    except (TypeError, ValueError):
        rel = path

    rel_posix = rel.as_posix()
    norm_rel = _norm_for_match(rel_posix)
    norm_parts = [_norm_for_match(part) for part in rel.parts]
    norm_stem = _norm_for_match(path.stem)
    norm_name = _norm_for_match(path.name)
    return norm_rel, norm_parts, norm_stem, norm_name


def _normalized_record_keys(rec: CheckpointRecord) -> List[str]:
    return _unique([key for key in (_norm_for_match(value) for value in rec.source_keys) if key])


def _source_match_rank_for_keys(keys: Sequence[str], surfaces: Tuple[str, List[str], str, str]) -> Optional[int]:
    norm_rel, norm_parts, norm_stem, norm_name = surfaces
    if not keys:
        return None

    parent_parts = norm_parts[:-1]
    for key in keys:
        if key in parent_parts:
            return 0
    for key in keys:
        if any(key in part for part in parent_parts):
            return 1
    for key in keys:
        if key in norm_stem or key in norm_name:
            return 2
    for key in keys:
        if key in norm_rel:
            return 3
    return None


def _source_match_rank(rec: CheckpointRecord, path: Path, source_root: Path) -> Optional[int]:
    """Return a match rank for path -> checkpoint, or None if it does not match.

    Lower is better.

    0: exact normalized parent directory segment match
    1: normalized parent directory segment contains ckpt_name_safe
    2: normalized filename stem contains ckpt_name_safe
    3: normalized relative path contains ckpt_name_safe

    The substring rules are needed for output layouts where the checkpoint safe name is
    embedded in a directory or filename rather than being the whole directory name.
    """
    return _source_match_rank_for_keys(
        _normalized_record_keys(rec),
        _iter_source_match_candidates(path, source_root),
    )


def _source_index_path() -> Path:
    get_user_directory = getattr(folder_paths, "get_user_directory", None)
    if callable(get_user_directory):
        user_root = Path(get_user_directory())
    else:
        user_directory = getattr(folder_paths, "user_directory", None)
        if user_directory:
            user_root = Path(user_directory)
        else:
            user_root = Path(folder_paths.get_output_directory()).resolve().parent / "user"
    return user_root / SOURCE_INDEX_DIRECTORY / SOURCE_INDEX_FILENAME


def _source_root_identity(source_root: Path) -> str:
    return os.path.normcase(str(source_root.resolve()))


def _empty_source_index(source_root: Path) -> Dict[str, object]:
    return {
        "schema_version": SOURCE_INDEX_SCHEMA_VERSION,
        "source_root": _source_root_identity(source_root),
        "entries": [],
    }


def _load_source_index(source_root: Path) -> Tuple[Dict[str, object], bool]:
    path = _source_index_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return _empty_source_index(source_root), True

    if not isinstance(data, dict):
        return _empty_source_index(source_root), True
    if data.get("schema_version") != SOURCE_INDEX_SCHEMA_VERSION:
        return _empty_source_index(source_root), True
    if os.path.normcase(str(data.get("source_root", ""))) != _source_root_identity(source_root):
        return _empty_source_index(source_root), True
    if not isinstance(data.get("entries"), list):
        return _empty_source_index(source_root), True
    return data, False


def _save_source_index(data: Dict[str, object]) -> None:
    path = _source_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with tmp_path.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _is_date_directory_name(name: str) -> bool:
    if not DATE_DIRECTORY_RE.fullmatch(name):
        return False
    try:
        datetime.strptime(name, "%Y%m%d")
    except ValueError:
        return False
    return True


def _path_mtime_ns(path: Path) -> int:
    try:
        return int(path.stat().st_mtime_ns)
    except OSError:
        return 0


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _entry_key(entry: Dict[str, object]) -> Tuple[str, str]:
    return (str(entry.get("bucket_type", "")), str(entry.get("path", "")))


def _bucket_sort_key(entry: Dict[str, object], source_root: Path) -> Tuple[object, ...]:
    bucket_type = str(entry.get("bucket_type", "fallback"))
    rel_path = str(entry.get("path", ""))
    if bucket_type == "date":
        date_value = str(entry.get("date", "0"))
        bucket_path = source_root if rel_path == "." else source_root / rel_path
        parent_mtime = _path_mtime_ns(bucket_path.parent)
        return (0, -_as_int(date_value), -parent_mtime, rel_path.casefold())
    if bucket_type == "fallback":
        bucket_path = source_root / rel_path
        return (1, -_path_mtime_ns(bucket_path), rel_path.casefold())
    return (2, 0, rel_path.casefold())


def _new_bucket_entry(*, path: str, bucket_type: str, date: str = "") -> Dict[str, object]:
    entry: Dict[str, object] = {
        "path": path,
        "bucket_type": bucket_type,
        "checkpoints": {},
        "negative_checks": {},
        "initialized": False,
    }
    if date:
        entry["date"] = date
    return entry


def _discover_source_buckets(source_root: Path, existing_entries: Sequence[object]) -> Tuple[List[Dict[str, object]], bool]:
    """Discover date buckets plus first-level fallback buckets.

    Date directories are terminal discovery boundaries. A fallback bucket scans its
    first-level branch while pruning any date directory below it. Root-level images
    use a small synthetic bucket with path '.'.
    """
    existing: Dict[Tuple[str, str], Dict[str, object]] = {}
    for raw in existing_entries:
        if isinstance(raw, dict):
            existing[_entry_key(raw)] = raw

    discovered: Dict[Tuple[str, str], Dict[str, object]] = {}
    root_has_images = False
    fallback_has_images: Dict[str, bool] = {}

    if not source_root.exists() or not source_root.is_dir():
        return [], bool(existing)

    if _is_date_directory_name(source_root.name):
        key = ("date", ".")
        entry = existing.get(key) or _new_bucket_entry(path=".", bucket_type="date", date=source_root.name)
        entry.update({"path": ".", "bucket_type": "date", "date": source_root.name})
        discovered[key] = entry
    else:
        discovery_failed = False

        def onerror(exc: OSError) -> None:
            nonlocal discovery_failed
            discovery_failed = True

        for dirpath, dirnames, filenames in os.walk(source_root, topdown=True, onerror=onerror):
            current = Path(dirpath)
            dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]

            date_names = [name for name in dirnames if _is_date_directory_name(name)]
            for name in date_names:
                bucket_path = current / name
                rel = bucket_path.relative_to(source_root).as_posix()
                key = ("date", rel)
                entry = existing.get(key) or _new_bucket_entry(path=rel, bucket_type="date", date=name)
                entry.update({"path": rel, "bucket_type": "date", "date": name})
                discovered[key] = entry
            dirnames[:] = [name for name in dirnames if name not in date_names]

            rel_current = current.relative_to(source_root)
            has_source_file = any(Path(name).suffix.lower() in SOURCE_IMAGE_EXTS for name in filenames)
            if rel_current == Path("."):
                root_has_images = root_has_images or has_source_file
            elif rel_current.parts:
                first = rel_current.parts[0]
                fallback_has_images[first] = fallback_has_images.get(first, False) or has_source_file

        for first, has_images in fallback_has_images.items():
            if not has_images:
                continue
            key = ("fallback", first)
            entry = existing.get(key) or _new_bucket_entry(path=first, bucket_type="fallback")
            entry.update({"path": first, "bucket_type": "fallback"})
            discovered[key] = entry
        if root_has_images:
            key = ("root", ".")
            entry = existing.get(key) or _new_bucket_entry(path=".", bucket_type="root")
            entry.update({"path": ".", "bucket_type": "root"})
            discovered[key] = entry

        # A transient permission/I/O error must not make valid cached buckets vanish.
        if discovery_failed:
            for key, entry in existing.items():
                if key in discovered:
                    continue
                rel = str(entry.get("path", ""))
                candidate = source_root if rel == "." else source_root / rel
                if candidate.is_dir():
                    discovered[key] = entry

    old_order = [_entry_key(raw) for raw in existing_entries if isinstance(raw, dict)]
    entries = [discovered[key] for key in old_order if key in discovered]
    new_entries = [entry for key, entry in discovered.items() if key not in existing]
    new_entries.sort(key=lambda entry: _bucket_sort_key(entry, source_root))
    for new_entry in new_entries:
        new_sort_key = _bucket_sort_key(new_entry, source_root)
        insert_at = len(entries)
        for index, existing_entry in enumerate(entries):
            if new_sort_key < _bucket_sort_key(existing_entry, source_root):
                insert_at = index
                break
        entries.insert(insert_at, new_entry)
    old_keys = set(existing)
    new_keys = set(discovered)
    new_order = [_entry_key(entry) for entry in entries]
    changed = old_keys != new_keys or old_order != new_order
    return entries, changed


def _catalog_signature(records: Sequence[CheckpointRecord]) -> str:
    names = "\n".join(sorted((rec.relative_name for rec in records), key=str.casefold))
    return hashlib.sha256(names.encode("utf-8")).hexdigest()


def _resolve_cached_image(source_root: Path, entry: Dict[str, object], image_rel: object) -> Optional[Path]:
    entry_rel = str(entry.get("path", ""))
    bucket_dir = source_root if entry_rel == "." else source_root / entry_rel
    try:
        bucket_resolved = bucket_dir.resolve()
        candidate = (bucket_resolved / str(image_rel)).resolve()
        candidate.relative_to(bucket_resolved)
        candidate.relative_to(source_root.resolve())
    except (OSError, TypeError, ValueError):
        return None
    if not candidate.is_file() or candidate.suffix.lower() not in SOURCE_IMAGE_EXTS:
        return None
    if Image is not None:
        try:
            with Image.open(candidate) as image:
                image.verify()
        except Exception:
            return None
    return candidate


def _scan_bucket_images(
    *,
    source_root: Path,
    entry: Dict[str, object],
    records: Sequence[CheckpointRecord],
    progress_cb: Optional[ProgressCallback],
    node_id: object,
) -> Tuple[Dict[str, Path], Dict[str, object]]:
    """Scan a whole bucket without an image-count limit."""
    entry_rel = str(entry.get("path", ""))
    bucket_type = str(entry.get("bucket_type", "fallback"))
    bucket_dir = source_root if entry_rel == "." else source_root / entry_rel
    record_keys = {id(rec): _normalized_record_keys(rec) for rec in records}
    best: Dict[int, Tuple[float, str, Path]] = {}
    files_seen = 0
    hit_images = 0
    ambiguous_images = 0
    complete = True

    def process_path(path: Path) -> None:
        nonlocal files_seen, hit_images, ambiguous_images
        if not _is_source_image(path):
            return
        files_seen += 1
        surfaces = _iter_source_match_candidates(path, source_root)
        ranked: List[Tuple[int, CheckpointRecord]] = []
        for rec in records:
            rank = _source_match_rank_for_keys(record_keys[id(rec)], surfaces)
            if rank is not None:
                ranked.append((rank, rec))
        if ranked:
            best_rank = min(rank for rank, _ in ranked)
            best_records = {id(rec): rec for rank, rec in ranked if rank == best_rank}
            if len(best_records) == 1:
                rec = next(iter(best_records.values()))
                mtime = _safe_stat_mtime(path)
                tie = path.as_posix()
                current = best.get(id(rec))
                if current is None or (mtime, tie) > (current[0], current[1]):
                    best[id(rec)] = (mtime, tie, path)
                hit_images += 1
            else:
                ambiguous_images += 1

        if files_seen % SCAN_PROGRESS_DOT_EVERY_FILES == 0:
            _send_progress(
                progress_cb,
                node_id,
                "scanning_source",
                files_seen,
                0,
                f"Indexing source images... {files_seen} images",
                entry_rel,
                "\n".join([
                    "Indexing source images...",
                    "",
                    "." * min(files_seen // SCAN_PROGRESS_DOT_EVERY_FILES, SCAN_PROGRESS_MAX_DOTS),
                    f"Bucket: {entry_rel}",
                    f"Scanned: {files_seen} images",
                    f"Hit images: {hit_images}",
                    f"Ambiguous images: {ambiguous_images}",
                ]),
            )

    try:
        if bucket_type == "root":
            for child in bucket_dir.iterdir():
                process_path(child)
        else:
            errors: List[OSError] = []

            def onerror(exc: OSError) -> None:
                errors.append(exc)

            for dirpath, dirnames, filenames in os.walk(bucket_dir, topdown=True, onerror=onerror):
                current = Path(dirpath)
                dirnames[:] = [name for name in dirnames if not (current / name).is_symlink()]
                if bucket_type == "fallback":
                    dirnames[:] = [name for name in dirnames if not _is_date_directory_name(name)]
                for filename in filenames:
                    process_path(current / filename)
            if errors:
                complete = False
    except OSError:
        complete = False

    id_to_record = {id(rec): rec for rec in records}
    found = {
        id_to_record[rec_id].relative_name: value[2]
        for rec_id, value in best.items()
        if rec_id in id_to_record
    }
    return found, {
        "files_seen": files_seen,
        "hit_images": hit_images,
        "ambiguous_images": ambiguous_images,
        "complete": complete,
    }


def _find_sources_with_persistent_index(
    *,
    source_root: Path,
    all_records: Sequence[CheckpointRecord],
    missing_records: Sequence[CheckpointRecord],
    progress_cb: Optional[ProgressCallback],
    node_id: object,
) -> Dict[str, object]:
    """Resolve missing checkpoint sources through date/fallback bucket caches."""
    data, dirty = _load_source_index(source_root)
    entries, discovered_changed = _discover_source_buckets(source_root, data.get("entries", []))
    dirty = dirty or discovered_changed
    data["entries"] = entries

    unresolved: Dict[str, CheckpointRecord] = {rec.relative_name: rec for rec in missing_records}
    catalog_signature = _catalog_signature(all_records)
    summary: Dict[str, object] = {
        "buckets": len(entries),
        "buckets_scanned": 0,
        "full_scans": 0,
        "targeted_scans": 0,
        "files_seen": 0,
        "cache_hits": 0,
        "negative_hits": 0,
        "unstable_scans": 0,
        "save_error": "",
    }

    for entry in entries:
        if not unresolved:
            break

        entry_rel = str(entry.get("path", ""))
        bucket_dir = source_root if entry_rel == "." else source_root / entry_rel
        if not bucket_dir.is_dir():
            dirty = True
            continue

        checkpoints = entry.get("checkpoints")
        if not isinstance(checkpoints, dict):
            checkpoints = {}
            entry["checkpoints"] = checkpoints
            dirty = True
        negatives = entry.get("negative_checks")
        if not isinstance(negatives, dict):
            negatives = {}
            entry["negative_checks"] = negatives
            dirty = True

        # Positive cache entries are trusted while the representative path remains
        # a safe, supported regular image within the bucket.
        force_scan: set[str] = set()
        for relative_name in list(unresolved):
            cached = checkpoints.get(relative_name)
            if not isinstance(cached, dict):
                if relative_name in checkpoints:
                    checkpoints.pop(relative_name, None)
                    negatives.pop(relative_name, None)
                    force_scan.add(relative_name)
                    dirty = True
                continue
            image_path = _resolve_cached_image(source_root, entry, cached.get("image"))
            if image_path is None:
                checkpoints.pop(relative_name, None)
                negatives.pop(relative_name, None)
                force_scan.add(relative_name)
                dirty = True
                continue
            unresolved[relative_name].source_image = image_path
            unresolved.pop(relative_name, None)
            summary["cache_hits"] = int(summary["cache_hits"]) + 1

        if not unresolved:
            break

        bucket_type = str(entry.get("bucket_type", "fallback"))
        current_mtime_ns = _path_mtime_ns(bucket_dir)
        full_scan = entry.get("full_scan")
        full_scan_valid = (
            bucket_type == "date"
            and isinstance(full_scan, dict)
            and bool(full_scan.get("complete"))
            and _as_int(full_scan.get("dir_mtime_ns"), -1) == current_mtime_ns
            and str(full_scan.get("catalog_signature", "")) == catalog_signature
        )

        records_to_scan: List[CheckpointRecord] = []
        for relative_name, rec in unresolved.items():
            if relative_name in force_scan:
                records_to_scan.append(rec)
                continue
            if full_scan_valid:
                summary["negative_hits"] = int(summary["negative_hits"]) + 1
                continue
            negative = negatives.get(relative_name)
            if (
                bucket_type == "date"
                and isinstance(negative, dict)
                and _as_int(negative.get("dir_mtime_ns"), -1) == current_mtime_ns
            ):
                summary["negative_hits"] = int(summary["negative_hits"]) + 1
                continue
            records_to_scan.append(rec)

        if not records_to_scan:
            continue

        is_full_scan = not bool(entry.get("initialized"))
        scan_records = list(all_records) if is_full_scan else records_to_scan
        found: Dict[str, Path] = {}
        scan_info: Dict[str, object] = {"complete": False, "files_seen": 0}
        stable = bucket_type != "date"
        final_mtime_ns = current_mtime_ns

        for _attempt in range(SOURCE_INDEX_SCAN_RETRIES if bucket_type == "date" else 1):
            start_mtime_ns = _path_mtime_ns(bucket_dir)
            found, scan_info = _scan_bucket_images(
                source_root=source_root,
                entry=entry,
                records=scan_records,
                progress_cb=progress_cb,
                node_id=node_id,
            )
            final_mtime_ns = _path_mtime_ns(bucket_dir)
            stable = bucket_type != "date" or start_mtime_ns == final_mtime_ns
            if stable:
                break
            summary["unstable_scans"] = int(summary["unstable_scans"]) + 1

        summary["buckets_scanned"] = int(summary["buckets_scanned"]) + 1
        summary["full_scans" if is_full_scan else "targeted_scans"] = int(
            summary["full_scans" if is_full_scan else "targeted_scans"]
        ) + 1
        summary["files_seen"] = int(summary["files_seen"]) + int(scan_info.get("files_seen", 0))

        for relative_name, image_path in found.items():
            try:
                image_rel = image_path.relative_to(bucket_dir).as_posix()
            except ValueError:
                continue
            checkpoints[relative_name] = {"image": image_rel}
            negatives.pop(relative_name, None)
            dirty = True
            rec = unresolved.get(relative_name)
            if rec is not None:
                rec.source_image = image_path
                unresolved.pop(relative_name, None)

        complete = bool(scan_info.get("complete"))
        if bucket_type == "date" and complete and stable:
            scanned_names = {rec.relative_name for rec in scan_records}
            for relative_name in scanned_names - set(found):
                negatives[relative_name] = {"dir_mtime_ns": final_mtime_ns}
            if is_full_scan:
                entry["full_scan"] = {
                    "complete": True,
                    "dir_mtime_ns": final_mtime_ns,
                    "catalog_signature": catalog_signature,
                }
            dirty = True
        if complete and stable:
            entry["initialized"] = True
            dirty = True

    if dirty:
        try:
            _save_source_index(data)
        except OSError as exc:
            summary["save_error"] = str(exc)
    summary["matched_checkpoints"] = sum(1 for rec in missing_records if rec.source_image is not None)
    summary["unmatched_checkpoints"] = len(missing_records) - int(summary["matched_checkpoints"])
    summary["index_path"] = str(_source_index_path())
    return summary


def _comment_text(
    *,
    checkpoint: str,
    source_root: Path,
    source_image: Path,
    representative_rule: str,
    target_format: str,
) -> str:
    try:
        source_rel = source_image.resolve().relative_to(source_root.resolve()).as_posix()
    except Exception:
        source_rel = source_image.as_posix()

    source_dir = source_image.parent.name
    lines = [
        TOOL_NAME,
        MANAGED_MARKER,
        f"tool={TOOL_NAME}",
        f"comment_schema={COMMENT_SCHEMA}",
        f"target={target_format}",
        f"checkpoint={checkpoint}",
        f"source_dir={source_dir}",
        f"source_file={source_rel}",
        f"representative_rule={representative_rule}",
        f"created_at={_now_iso()}",
    ]
    return "\n".join(lines)


def _create_thumbnail(source_image: Path, target_path: Path, max_size: int, jpeg_quality: int, comment: str) -> None:
    if Image is None or ImageOps is None:
        raise RuntimeError(f"Pillow import failed: {_PIL_IMPORT_ERROR}")

    max_size = max(64, min(int(max_size), 4096))
    jpeg_quality = max(1, min(int(jpeg_quality), 100))

    with Image.open(source_image) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            rgba = img.convert("RGBA")
            background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            background.alpha_composite(rgba)
            img = background.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        img.thumbnail((max_size, max_size), _image_resampling_lanczos())
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.with_name(f"{target_path.name}.{secrets.token_hex(8)}.tmp")
        try:
            img.save(
                tmp_path,
                format="JPEG",
                quality=jpeg_quality,
                optimize=True,
                comment=comment.encode("utf-8"),
            )
            try:
                # A same-directory hard link publishes the completed JPEG atomically
                # and fails rather than overwriting a sidecar created during scanning.
                os.link(tmp_path, target_path)
            except FileExistsError:
                raise
            except OSError:
                # Some network/removable filesystems do not support hard links. The
                # exclusive destination open keeps the no-overwrite guarantee there.
                created_target = False
                try:
                    target = target_path.open("xb")
                    created_target = True
                    with tmp_path.open("rb") as source, target:
                        shutil.copyfileobj(source, target)
                        target.flush()
                        os.fsync(target.fileno())
                except Exception:
                    if created_target:
                        try:
                            target_path.unlink()
                        except FileNotFoundError:
                            pass
                    raise
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass


def _read_jpeg_comment(path: Path) -> str:
    if Image is None:
        return ""
    try:
        with Image.open(path) as img:
            comment = img.info.get("comment", b"")
    except Exception:
        return ""
    if isinstance(comment, bytes):
        return comment.decode("utf-8", errors="replace")
    return str(comment or "")


def _is_managed_thumbnail(path: Path) -> bool:
    if path.suffix.lower() not in {".jpg", ".jpeg"}:
        return False
    comment = _read_jpeg_comment(path)
    lines = {line.strip() for line in comment.splitlines()}
    return {
        TOOL_NAME,
        MANAGED_MARKER,
        f"tool={TOOL_NAME}",
        f"comment_schema={COMMENT_SCHEMA}",
        f"target={TARGET_OGN}",
    }.issubset(lines)


def _format_examples(title: str, items: Sequence[str], limit: int = 12) -> List[str]:
    if not items:
        return []
    lines = [title]
    for item in items[:limit]:
        lines.append(f"  - {item}")
    if len(items) > limit:
        lines.append(f"  ... and {len(items) - limit} more")
    return lines


def _run_install_missing(
    *,
    payload: Dict[str, object],
    progress_cb: Optional[ProgressCallback],
) -> Dict[str, object]:
    node_id = payload.get("node_id")
    run_mode = str(payload.get("run_mode", "dry_run"))
    target_format = str(payload.get("target_format", TARGET_OGN))
    max_size = int(payload.get("max_size", 512))
    jpeg_quality = int(payload.get("jpeg_quality", 90))
    source_root = _get_source_root(str(payload.get("source_image_root", "")))

    stats = ExportStats()
    records = _get_checkpoint_records()
    stats.checkpoints = len(records)

    missing: List[CheckpointRecord] = []
    existing_examples: List[str] = []

    for index, rec in enumerate(records, start=1):
        _send_progress(progress_cb, node_id, "checking_thumbnails", index, len(records), "Checking existing thumbnails...", rec.relative_name)
        rec.existing_thumbnail = _find_existing_sidecar_thumbnail(rec.path)
        if rec.existing_thumbnail is not None:
            stats.existing_thumbnails += 1
            if len(existing_examples) < 5:
                existing_examples.append(f"{rec.relative_name} -> {rec.existing_thumbnail.name}")
        else:
            missing.append(rec)

    stats.missing_thumbnails = len(missing)

    if not missing:
        _send_progress(progress_cb, node_id, "done", len(records), len(records), "All checkpoints already have thumbnails. Source scan was not needed.", "")
        report = [
            "Install complete." if run_mode == "execute" else "Dry run complete.",
            "",
            f"Checkpoints: {stats.checkpoints}",
            f"Existing thumbnails: {stats.existing_thumbnails}",
            "Missing thumbnails: 0",
            "",
            "All checkpoints already have thumbnails.",
            "source_image_root was not scanned.",
        ]
        return {"ok": True, "stats": stats.__dict__, "report": "\n".join(report), "confirm_token": None}

    _send_progress(progress_cb, node_id, "scanning_source", 0, len(missing), "Scanning source images...", str(source_root), "Scanning source images...\n\nSource root: %s" % source_root)
    scan_summary = _find_sources_with_persistent_index(
        source_root=source_root,
        all_records=records,
        missing_records=missing,
        progress_cb=progress_cb,
        node_id=node_id,
    )

    install_examples: List[str] = []
    unmatched_examples: List[str] = []
    error_examples: List[str] = []

    for index, rec in enumerate(missing, start=1):
        _send_progress(progress_cb, node_id, "processing_missing", index, len(missing), "Processing missing thumbnails...", rec.relative_name)
        if rec.source_image is None:
            stats.unmatched += 1
            unmatched_examples.append(rec.relative_name)
            continue

        target_path = _target_jpg_path(rec.path)
        if run_mode == "dry_run":
            stats.would_install += 1
            install_examples.append(f"{rec.relative_name} <- {rec.source_image.name}")
            continue

        try:
            comment = _comment_text(
                checkpoint=rec.relative_name,
                source_root=source_root,
                source_image=rec.source_image,
                representative_rule="latest",
                target_format=target_format,
            )
            _create_thumbnail(rec.source_image, target_path, max_size, jpeg_quality, comment)
            stats.installed += 1
            install_examples.append(f"{rec.relative_name} <- {rec.source_image.name}")
        except FileExistsError:
            # A sidecar appeared after the initial target scan. Preserve it and treat
            # the checkpoint as an existing-thumbnail race rather than an error.
            stats.existing_thumbnails += 1
            if len(existing_examples) < 5:
                existing_examples.append(f"{rec.relative_name} -> {target_path.name} (created during scan)")
        except Exception as exc:
            stats.errors += 1
            error_examples.append(f"{rec.relative_name}: {exc}")

    _send_progress(progress_cb, node_id, "done", len(missing), len(missing), "Done.", "")

    if run_mode == "dry_run":
        header = "Dry run complete."
        changed = "No thumbnail or source-image files were changed. The internal source index may have been updated."
    else:
        header = "🎨 Install complete." if stats.installed > 0 else "Install complete."
        changed = (
            "Thumbnail JPEG files were written next to checkpoint files."
            if stats.installed > 0
            else "No thumbnail files were written."
        )

    report: List[str] = [
        header,
        "",
        f"Source root: {source_root}",
        f"Target: {target_format}",
        "",
        f"Checkpoints: {stats.checkpoints}",
        f"Existing thumbnails: {stats.existing_thumbnails}",
        f"Missing thumbnails: {stats.missing_thumbnails}",
        f"Would install: {stats.would_install}" if run_mode == "dry_run" else f"Installed: {stats.installed}",
        f"Unmatched: {stats.unmatched}",
    ]
    report.extend(_format_examples("Unmatched checkpoints:", unmatched_examples))
    report.append(f"Errors: {stats.errors}")
    report.extend(_format_examples("Error checkpoints:", error_examples))
    report.extend(["", changed])
    report.extend([
        "",
        f"Source index buckets: {scan_summary.get('buckets', 0)}",
        f"Index cache hits: {scan_summary.get('cache_hits', 0)}",
        f"Index negative hits: {scan_summary.get('negative_hits', 0)}",
        f"Buckets scanned: {scan_summary.get('buckets_scanned', 0)}",
        f"Source images scanned: {scan_summary.get('files_seen', 0)}",
    ])
    if scan_summary.get("save_error"):
        report.extend([
            "",
            "Warning: source index could not be saved.",
            str(scan_summary.get("save_error")),
            "The source lookup result is still usable for this run.",
        ])
    if stats.missing_thumbnails > 0 and stats.unmatched == stats.missing_thumbnails:
        report.extend([
            "",
            "Hint:",
            "All missing checkpoints were unmatched.",
            "Please check whether source_image_root points to the folder that actually contains generated images.",
            "If you rely on a junction/symlinked output folder, verify that it still exists.",
        ])

    report.extend(_format_examples("Examples:", install_examples))
    report.extend(_format_examples("Existing thumbnails preserved:", existing_examples))

    return {"ok": stats.errors == 0, "stats": stats.__dict__, "report": "\n".join(report), "confirm_token": None}


def _run_uninstall_managed(
    *,
    payload: Dict[str, object],
    progress_cb: Optional[ProgressCallback],
) -> Dict[str, object]:
    node_id = payload.get("node_id")
    run_mode = str(payload.get("run_mode", "dry_run"))
    target_format = str(payload.get("target_format", TARGET_OGN))
    stats = ExportStats()
    confirmed_snapshot: Dict[str, Tuple[int, int, int, int]] = {}

    if run_mode == "execute":
        token = str(payload.get("confirm_token", ""))
        confirmation = _consume_confirm_token(token, payload)
        if confirmation is None:
            report = "\n".join([
                "Uninstall was requested, but confirmation is missing or expired.",
                "",
                "Run dry_run for uninstall_managed first, then execute.",
                "No files were removed.",
            ])
            _send_progress(progress_cb, node_id, "done", 0, 0, "Confirmation missing. No files were removed.", "")
            return {"ok": False, "stats": stats.__dict__, "report": report, "confirm_token": None}
        raw_snapshot = confirmation.get("managed_snapshot", {})
        if isinstance(raw_snapshot, dict):
            for path_text, identity in raw_snapshot.items():
                if isinstance(identity, (list, tuple)) and len(identity) == 4:
                    confirmed_snapshot[str(path_text)] = tuple(_as_int(value) for value in identity)

    records = _get_checkpoint_records()
    stats.checkpoints = len(records)
    managed_examples: List[str] = []
    skipped_examples: List[str] = []
    error_examples: List[str] = []
    managed_snapshot: Dict[str, Tuple[int, int, int, int]] = {}

    for index, rec in enumerate(records, start=1):
        _send_progress(progress_cb, node_id, "checking_managed", index, len(records), "Checking managed thumbnails...", rec.relative_name)
        thumb = _find_existing_sidecar_thumbnail(rec.path)
        if thumb is None:
            continue
        if not _is_managed_thumbnail(thumb):
            stats.skipped_unmanaged += 1
            if len(skipped_examples) < 8:
                skipped_examples.append(f"{rec.relative_name} -> {thumb.name}")
            continue

        stats.managed_found += 1
        if run_mode == "dry_run":
            stats.would_remove += 1
            managed_examples.append(f"{rec.relative_name} -> {thumb.name}")
            identity = _file_identity(thumb)
            if identity is not None:
                managed_snapshot[_confirmation_path(thumb)] = identity
        else:
            path_key = _confirmation_path(thumb)
            if confirmed_snapshot.get(path_key) != _file_identity(thumb):
                stats.skipped_unmanaged += 1
                if len(skipped_examples) < 8:
                    skipped_examples.append(f"{rec.relative_name} -> {thumb.name} (not confirmed or changed after dry run)")
                continue
            try:
                thumb.unlink()
                stats.removed += 1
                managed_examples.append(f"{rec.relative_name} -> {thumb.name}")
            except Exception as exc:
                stats.errors += 1
                error_examples.append(f"{rec.relative_name}: {exc}")

    _send_progress(progress_cb, node_id, "done", len(records), len(records), "Done.", "")

    confirm_token = None
    if run_mode == "dry_run":
        confirm_token = _make_confirm_token(payload, managed_snapshot)
        header = "Dry run complete."
        changed = "No files were removed."
        action_line = f"Would remove: {stats.would_remove}"
    else:
        header = "❌ Uninstall complete." if stats.removed > 0 else "Uninstall complete."
        changed = "Managed thumbnails were removed." if stats.removed > 0 else "No managed thumbnails were removed."
        action_line = f"Removed: {stats.removed}"

    report: List[str] = [
        header,
        "",
        f"Target: {target_format}",
        "",
        f"Checkpoints: {stats.checkpoints}",
        f"Managed thumbnails found: {stats.managed_found}",
        action_line,
        f"Unmanaged/manual thumbnails skipped: {stats.skipped_unmanaged}",
        f"Errors: {stats.errors}",
    ]
    report.extend(_format_examples("Error checkpoints:", error_examples))
    report.extend(["", changed])
    report.extend(_format_examples("Managed thumbnails:", managed_examples))
    report.extend(_format_examples("Skipped unmanaged/manual thumbnails:", skipped_examples))

    return {"ok": stats.errors == 0, "stats": stats.__dict__, "report": "\n".join(report), "confirm_token": confirm_token}


def _run_exporter_unlocked(payload: Dict[str, object], progress_cb: Optional[ProgressCallback] = None) -> Dict[str, object]:
    operation = str(payload.get("operation", "install_missing"))
    run_mode = str(payload.get("run_mode", "dry_run"))
    target_format = str(payload.get("target_format", TARGET_OGN))

    if target_format != TARGET_OGN:
        return {"ok": False, "stats": {}, "report": f"Unsupported target_format: {target_format}", "confirm_token": None}
    if run_mode not in {"dry_run", "execute"}:
        return {"ok": False, "stats": {}, "report": f"Unsupported run_mode: {run_mode}", "confirm_token": None}
    if operation == "install_missing":
        return _run_install_missing(payload=payload, progress_cb=progress_cb)
    if operation == "uninstall_managed":
        return _run_uninstall_managed(payload=payload, progress_cb=progress_cb)
    return {"ok": False, "stats": {}, "report": f"Unsupported operation: {operation}", "confirm_token": None}


def _run_exporter(payload: Dict[str, object], progress_cb: Optional[ProgressCallback] = None) -> Dict[str, object]:
    if not _EXPORTER_RUN_LOCK.acquire(blocking=False):
        return {
            "ok": False,
            "stats": {},
            "report": "Another Checkpoint Thumbnail Exporter operation is already running.\n\nTry again after it finishes.",
            "confirm_token": None,
        }
    try:
        return _run_exporter_unlocked(payload, progress_cb)
    finally:
        _EXPORTER_RUN_LOCK.release()


@PromptServer.instance.routes.post("/checkpoint-thumbnail-exporter/run")
async def checkpoint_thumbnail_exporter_run(request):
    payload = await request.json()
    node_id = payload.get("node_id")

    def progress_cb(message: Dict[str, object]) -> None:
        try:
            PromptServer.instance.send_sync("checkpoint-thumbnail-exporter-progress", message)
        except Exception:
            pass

    try:
        result = await asyncio.to_thread(_run_exporter, payload, progress_cb)
    except Exception:
        tb = traceback.format_exc()
        _send_progress(progress_cb, node_id, "done", 0, 0, "Error.", "")
        result = {
            "ok": False,
            "stats": {"errors": 1},
            "report": "Unexpected error.\n\n" + tb,
            "confirm_token": None,
        }
    return web.json_response(result)


class CheckpointThumbnailExporter:
    """A standalone utility panel for creating OGN-ModelManager checkpoint thumbnails."""

    CATEGORY = "utils/checkpoint"
    RETURN_TYPES = ()
    FUNCTION = "run"
    OUTPUT_NODE = True

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "source_image_root": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": "Root folder containing HandpickerSuite / GM Image Saver images. Empty = ComfyUI output folder.",
                    },
                ),
                "target_format": ([TARGET_OGN], {"default": TARGET_OGN}),
                "max_size": (
                    "INT",
                    {"default": 512, "min": 64, "max": 4096, "step": 16, "tooltip": "Maximum thumbnail width/height. Shrink only."},
                ),
                "jpeg_quality": (
                    "INT",
                    {"default": 90, "min": 1, "max": 100, "step": 1, "tooltip": "JPEG quality for generated thumbnails."},
                ),
                "operation": (["install_missing", "uninstall_managed"], {"default": "install_missing"}),
                "run_mode": (["dry_run", "execute"], {"default": "dry_run"}),
            }
        }

    def run(self, source_image_root, target_format, operation, run_mode, max_size, jpeg_quality):
        # This node is intended to be controlled by its UI button. Queue execution is a no-op.
        return {"ui": {"text": ["Use the node button to run Checkpoint Thumbnail Exporter."]}, "result": ()}


NODE_CLASS_MAPPINGS = {
    "CheckpointThumbnailExporter": CheckpointThumbnailExporter,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "CheckpointThumbnailExporter": "Checkpoint Thumbnail Exporter",
}
