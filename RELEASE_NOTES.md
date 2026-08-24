# Release Notes

## Unreleased

### Added

- Added a disposable persistent source-image index under the ComfyUI user directory.
- Added `YYYYMMDD` date buckets, first-level fallback buckets, and root-level source handling.
- Added cold full scans and warm targeted scans with uncapped date-bucket traversal.
- Added date-bucket negative caching and PushLocalList-style change recovery.
- Added local index repair for removed representatives and removed date directories.
- Added regression tests for index lifecycle, fallback pruning, dry-run warm-up, execute reuse, and uninstall confirmation safety.

### Changed

- `install_missing + dry_run` may update the internal source index while leaving thumbnail and source-image files unchanged.
- Valid indexed representatives remain stable instead of requiring an exact global-newest image on every run.
- Concurrent exporter operations now use a first-wins, non-blocking process lock.
- Thumbnail publication now uses unique temporary files and never overwrites a sidecar created during scanning.
- Managed-thumbnail detection now requires exact stable-schema marker lines.
- Uninstall confirmation is bound to the exact managed files observed during dry run.

## 0.1.0

Initial release of **ComfyUI-CheckpointThumbnailExporter**.

Checkpoint Thumbnail Exporter is a standalone ComfyUI utility node that creates missing checkpoint thumbnails for **OGN-ModelManager** from checkpoint-associated generated image folders.

### Added

- Added `Checkpoint Thumbnail Exporter` utility node.
- Added support for installing missing OGN-ModelManager checkpoint thumbnails.
- Added support for uninstalling only thumbnails managed by this node.
- Added dry-run mode for both install and uninstall operations.
- Added one-button UI with operation-aware labels:
  - `🎨 [Dry Run] Find Missing Thumbnails`
  - `🎨 [Execute!] Install Missing Thumbnails`
  - `❌ [Dry Run] Find Managed Thumbnails`
  - `❌ [Execute!] Uninstall Managed Thumbnails`
- Added compact initial Ready report explaining the `operation` + `run_mode` matrix.
- Added in-node progress bar and read-only report area.
- Added automatic reset of `run_mode` to `dry_run` when `operation` changes.
- Added automatic reset of `run_mode` to `dry_run` after execute operations.
- Added dry-run confirmation token requirement before executing `uninstall_managed`.
- Added JPEG thumbnail generation using Pillow.
- Added JPEG comment metadata for managed thumbnail detection.
- Added source image matching by checkpoint-associated path, directory, or filename.
- Added early target-side thumbnail check so source folders are not scanned when no update is needed.
- Added support for empty `source_image_root`, which uses the current ComfyUI output directory.
- Added troubleshooting hints when all checkpoints are unmatched.
- Added English README, Japanese README, `pyproject.toml`, `RELEASE_NOTES.md`, and `.spec/` documentation.

### Behavior

- Existing thumbnails are skipped and never overwritten.
- Only `.jpg` thumbnails are created.
- Thumbnail images are resized with aspect ratio preserved.
- Images are shrunk only, not enlarged.
- OGN-ModelManager internal APIs, cache, and state are not modified.
- The node only places thumbnail files next to checkpoint files using the file layout already supported by OGN-ModelManager.
- `uninstall_managed` removes only thumbnails that contain this node's managed JPEG comment marker.
- Manual thumbnails and unmanaged thumbnails are not removed by `uninstall_managed`.

### Current Scope

- Target format is currently fixed to `OGN-ModelManager`.
- External thumbnail download services such as Civitai are not supported.
- Existing thumbnail overwrite or refresh modes are not included.
- Sidecar `.txt` memo generation is not included.
- Thumbnail overlay or tag badge rendering is not included.

### Notes

Generated images do not need to come specifically from GM Image Saver. Any generated image can be used as a source if its directory path or filename can be associated with the checkpoint name.

If newly created thumbnails do not appear immediately in OGN-ModelManager, reload the OGN-ModelManager view or restart ComfyUI.
