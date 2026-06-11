from __future__ import annotations

import os
import re
import time
import secrets
import traceback
from dataclasses import dataclass, field
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
TOOL_BUILD = "v1b"
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


def _make_confirm_token(payload: Dict[str, object]) -> str:
    _clean_old_tokens()
    token = secrets.token_urlsafe(18)
    _CONFIRM_TOKENS[token] = {
        "created_at": time.time(),
        "key": _confirmation_key(payload),
    }
    return token


def _consume_confirm_token(token: str, payload: Dict[str, object]) -> bool:
    _clean_old_tokens()
    if not token:
        return False
    data = _CONFIRM_TOKENS.pop(token, None)
    if not data:
        return False
    return tuple(data.get("key", ())) == _confirmation_key(payload)


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


def _send_progress(progress_cb: Optional[ProgressCallback], node_id: object, phase: str, current: int, total: int, status: str = "", current_name: str = "") -> None:
    if progress_cb is None:
        return
    progress_cb({
        "node_id": node_id,
        "phase": phase,
        "current": int(current),
        "total": int(total),
        "status": status,
        "current_name": current_name,
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


def _build_source_index(source_root: Path, missing_records: Sequence[CheckpointRecord], progress_cb: Optional[ProgressCallback], node_id: object) -> None:
    """Fill record.source_image with the latest source image found under matching source dirs.

    This scans source_root once. A source image belongs to a checkpoint if any parent
    directory name under source_root matches the checkpoint's source key.
    """
    key_to_records: Dict[str, List[CheckpointRecord]] = {}
    for rec in missing_records:
        for key in rec.source_keys:
            key_to_records.setdefault(key, []).append(rec)

    # Avoid unsafe ambiguous basename matches.
    unique_key_to_record = {key: owners[0] for key, owners in key_to_records.items() if len(owners) == 1}
    if not source_root.exists() or not source_root.is_dir() or not unique_key_to_record:
        return

    best: Dict[str, Tuple[float, str, Path]] = {}
    files_seen = 0
    last_progress = 0.0

    for dirpath, dirnames, filenames in os.walk(source_root):
        # Do not follow symlinked directories by default. This avoids surprise loops.
        dirnames[:] = [d for d in dirnames if not Path(dirpath, d).is_symlink()]

        try:
            rel_parent = Path(dirpath).resolve().relative_to(source_root)
            parent_parts = set(rel_parent.parts)
        except Exception:
            parent_parts = set(Path(dirpath).parts)

        matched_keys = [key for key in parent_parts if key in unique_key_to_record]
        if not matched_keys:
            continue

        for filename in filenames:
            path = Path(dirpath, filename)
            if not _is_source_image(path):
                continue
            files_seen += 1
            mtime = _safe_stat_mtime(path)
            tie = path.name
            for key in matched_keys:
                current = best.get(key)
                if current is None or (mtime, tie) > (current[0], current[1]):
                    best[key] = (mtime, tie, path)

        now = time.time()
        if now - last_progress > 0.25:
            last_progress = now
            _send_progress(
                progress_cb,
                node_id,
                "scanning_source",
                files_seen,
                0,
                f"Scanning source images... files checked: {files_seen}",
                str(source_root),
            )

    for key, (_, _, image_path) in best.items():
        rec = unique_key_to_record[key]
        current = rec.source_image
        if current is None:
            rec.source_image = image_path
        else:
            # If several candidate keys point to the same checkpoint, keep the latest image.
            cur_mtime = _safe_stat_mtime(current)
            new_mtime = _safe_stat_mtime(image_path)
            if (new_mtime, image_path.name) > (cur_mtime, current.name):
                rec.source_image = image_path


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
        tmp_path = target_path.with_name(target_path.name + ".tmp")
        img.save(
            tmp_path,
            format="JPEG",
            quality=jpeg_quality,
            optimize=True,
            comment=comment.encode("utf-8"),
        )
        os.replace(tmp_path, target_path)


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
    return TOOL_NAME in comment and MANAGED_MARKER in comment and f"target={TARGET_OGN}" in comment


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
            "Install missing thumbnails: complete." if run_mode == "execute" else "Dry run complete.",
            "",
            f"Checkpoints: {stats.checkpoints}",
            f"Existing thumbnails: {stats.existing_thumbnails}",
            "Missing thumbnails: 0",
            "",
            "All checkpoints already have thumbnails.",
            "source_image_root was not scanned.",
        ]
        return {"ok": True, "stats": stats.__dict__, "report": "\n".join(report), "confirm_token": None}

    _send_progress(progress_cb, node_id, "scanning_source", 0, len(missing), "Scanning source images for missing thumbnails...", str(source_root))
    _build_source_index(source_root, missing, progress_cb, node_id)

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
        except Exception as exc:
            stats.errors += 1
            error_examples.append(f"{rec.relative_name}: {exc}")

    _send_progress(progress_cb, node_id, "done", len(missing), len(missing), "Done.", "")

    if run_mode == "dry_run":
        header = "Dry run complete."
        changed = "No files were changed."
    else:
        header = "Install complete."
        changed = "Thumbnail JPEG files were written next to checkpoint files."

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
        f"Errors: {stats.errors}",
        "",
        changed,
    ]
    report.extend(_format_examples("", []))
    report.extend(_format_examples("Examples:", install_examples))
    report.extend(_format_examples("Unmatched checkpoints:", unmatched_examples))
    report.extend(_format_examples("Errors:", error_examples))

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

    if run_mode == "execute":
        token = str(payload.get("confirm_token", ""))
        if not _consume_confirm_token(token, payload):
            report = "\n".join([
                "Uninstall was requested, but confirmation is missing or expired.",
                "",
                "Run dry_run for uninstall_managed first, then execute.",
                "No files were removed.",
            ])
            _send_progress(progress_cb, node_id, "done", 0, 0, "Confirmation missing. No files were removed.", "")
            return {"ok": False, "stats": stats.__dict__, "report": report, "confirm_token": None}

    records = _get_checkpoint_records()
    stats.checkpoints = len(records)
    managed_examples: List[str] = []
    skipped_examples: List[str] = []
    error_examples: List[str] = []

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
        else:
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
        confirm_token = _make_confirm_token(payload)
        header = "Dry run complete."
        changed = "No files were removed."
        action_line = f"Would remove: {stats.would_remove}"
    else:
        header = "Uninstall complete."
        changed = "Managed thumbnails were removed."
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
        "",
        changed,
    ]
    if run_mode == "dry_run":
        report.extend([
            "",
            "To remove these files, switch run_mode to execute and press the button.",
            "The confirmation expires after 10 minutes.",
        ])
    report.extend(_format_examples("Managed thumbnails:", managed_examples))
    report.extend(_format_examples("Skipped unmanaged/manual thumbnails:", skipped_examples))
    report.extend(_format_examples("Errors:", error_examples))

    return {"ok": stats.errors == 0, "stats": stats.__dict__, "report": "\n".join(report), "confirm_token": confirm_token}


def _run_exporter(payload: Dict[str, object], progress_cb: Optional[ProgressCallback] = None) -> Dict[str, object]:
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
        result = _run_exporter(payload, progress_cb=progress_cb)
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
                "operation": (["install_missing", "uninstall_managed"], {"default": "install_missing"}),
                "run_mode": (["dry_run", "execute"], {"default": "dry_run"}),
                "max_size": (
                    "INT",
                    {"default": 512, "min": 64, "max": 4096, "step": 16, "tooltip": "Maximum thumbnail width/height. Shrink only."},
                ),
                "jpeg_quality": (
                    "INT",
                    {"default": 90, "min": 1, "max": 100, "step": 1, "tooltip": "JPEG quality for generated thumbnails."},
                ),
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
