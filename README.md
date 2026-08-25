# ComfyUI-CheckpointThumbnailExporter

`Checkpoint Thumbnail Exporter` is a standalone ComfyUI utility node for creating missing checkpoint thumbnails for `OGN-ModelManager` from checkpoint-associated generated image folders.

It is meant for this workflow:

```text
Generate images associated with each checkpoint
↓
Checkpoint-specific images accumulate
↓
Checkpoint Thumbnail Exporter picks a recent representative image for each checkpoint
↓
Missing OGN-ModelManager thumbnails are installed automatically
```

It works especially well with HandpickerSuite / GM Image Saver workflows, but the source images do not have to come from GM Image Saver. Any generated image can be used if its directory path or filename can be associated with the checkpoint name.

## What it does

- Gets the ComfyUI checkpoint list.
- Checks whether each checkpoint already has an OGN-compatible sidecar thumbnail.
- Scans source image folders only for checkpoints without thumbnails.
- Uses a recent source image for each checkpoint and keeps valid indexed representatives stable.
- Writes a resized `.jpg` next to the checkpoint file.
- Adds a JPEG comment so it can later uninstall only thumbnails created by this node.
- Does not touch OGN-ModelManager internals, cache, or private APIs.

Example output layout:

```text
ComfyUI/models/checkpoints/foo.safetensors
ComfyUI/models/checkpoints/foo.jpg
```

For checkpoint subfolders:

```text
ComfyUI/models/checkpoints/subdir/foo.safetensors
ComfyUI/models/checkpoints/subdir/foo.jpg
```

## Why this exists

Setting thumbnails manually for 100+ checkpoints is painful.

This node uses images that were actually generated in your own environment, rather than downloading catalog images from an external service.

It can also be used as a lightweight visual checkpoint catalog based on images generated in your own environment.

## Requirements

- ComfyUI
- Pillow

GraphicsMagick is **not** required.

Pillow is already included in most ComfyUI environments. It is used to resize and write thumbnail JPEG files.

## Installation

Copy this folder into your ComfyUI `custom_nodes` directory:

```text
ComfyUI/custom_nodes/ComfyUI-CheckpointThumbnailExporter
```

Restart ComfyUI.

## Node

Node name:

```text
Checkpoint Thumbnail Exporter
```

Category:

```text
utils/checkpoint
```

This is a standalone utility node. It does not need to be connected to other nodes.

## Widgets

```text
source_image_root
```

Root folder containing generated images associated with checkpoints.

Leave empty to use the current ComfyUI output folder.

```text
target_format
```

Version 0.1.0 supports only:

```text
OGN-ModelManager
```

```text
max_size
```

Maximum thumbnail width/height. Images are shrunk only and aspect ratio is preserved.

```text
jpeg_quality
```

JPEG quality for generated thumbnails.

```text
operation
```

Options:

```text
install_missing
uninstall_managed
```

```text
run_mode
```

Options:

```text
dry_run
execute
```

Changing `operation` resets `run_mode` to `dry_run`.
After an `execute` run completes, `run_mode` is reset to `dry_run`.

## Button behavior

The node has one button. Its label changes depending on `operation` and `run_mode`.

```text
install_missing + dry_run   -> 🎨 [Dry Run] Find Missing Thumbnails
install_missing + execute   -> 🎨 [Execute!] Install Missing Thumbnails
uninstall_managed + dry_run -> ❌ [Dry Run] Find Managed Thumbnails
uninstall_managed + execute -> ❌ [Execute!] Uninstall Managed Thumbnails
```

A progress bar and report area are shown inside the node.

The initial report shows a compact guide:

```text
Ready.

Button behavior = operation + run_mode.

🎨 install_missing
  dry_run : find missing thumbnails
  execute : install missing thumbnails

❌ uninstall_managed
  dry_run : find managed thumbnails
  execute : uninstall managed thumbnails

dry_run does not modify thumbnails or source images.
The internal source index may be updated.
Empty source_image_root uses ComfyUI output.
```

Report first-line icons are intentionally stricter than button icons:

- Dry run reports do not use operation icons because no files were changed.
- Install execute reports start with `🎨 Install complete.` only when thumbnail files were actually written.
- Uninstall execute reports start with `❌ Uninstall complete.` only when managed thumbnail files were actually removed.

## Safe uninstall

`uninstall_managed` removes only JPEG thumbnails whose JPEG comment contains this node's exact tool, management, stable-schema, and target marker lines.

Manual thumbnails, OGN-uploaded thumbnails, and unmanaged sidecar images are skipped.

For safety, `uninstall_managed + execute` requires a fresh `uninstall_managed + dry_run` first. The confirmation expires after 10 minutes.
The confirmation is bound to the managed thumbnail files seen by that dry run; newly created or changed files are skipped.

## Important behavior

`install_missing` checks target thumbnails first.

If all checkpoints already have thumbnails, `source_image_root` is not scanned.

Existing thumbnails are not overwritten in version 0.1.0.

To refresh thumbnails managed by this node, run `uninstall_managed` first, then run `install_missing` again. Manual or unmanaged thumbnails are not removed and will not be overwritten.

## Source image matching

For each checkpoint, the exporter generates `ckpt_name_safe`-style candidate keys from the checkpoint relative path and basename. Source images are matched when that key appears in a parent folder, filename stem, or relative path.

Typical layouts:

```text
inventory/
  waiNSFWIllustrious_v150/
    image_0001.jpg
    image_0002.jpg
  noobai_xl_vpred/
    image_0001.jpg
```

Also supported:

```text
output/prefix/date/waiNSFWIllustrious_v150/image_0001.jpg
output/prefix_waiNSFWIllustrious_v150_20260611_0001.jpg
output/prefix/date/prefix_waiNSFWIllustrious_v150_0001.jpg
```

Exact directory matches are preferred over substring matches. If one source image matches multiple checkpoints at the same best priority, it is skipped as ambiguous.

When no valid indexed representative exists, the latest matching image by modification time is preferred within each scanned bucket. A valid cached representative remains selected even when newer images appear.

## Persistent source index

Source lookup uses a disposable JSON index under the ComfyUI user directory. The filesystem remains the source of truth.

- Valid calendar-date `YYYYMMDD` directories are indexed as date buckets.
- Source images outside date directories use first-level fallback buckets.
- A cold or new bucket scan indexes the current checkpoint catalog in one pass.
- Existing buckets use targeted scans for unresolved checkpoints.
- `install_missing + dry_run` may update the internal index, while thumbnail and source-image files remain unchanged.
- `execute` reuses the index warmed by dry run.
- Date-bucket scans have no image-count limit.
- If PushLocalList adds an image for a previously unmatched checkpoint, the changed date-directory state invalidates that negative cache and the next lookup scans the affected date bucket again.
- If `source_image_root` changes, the disposable index is rebuilt for the new root.

Valid cached representatives are reused without requiring an exact global-newest image. If a cached representative is deleted, only that checkpoint entry is repaired. If a date directory is moved outside `source_image_root`, its date entry is removed lazily.

The report lists checkpoint names immediately below `Unmatched:` and `Errors:`. Use an unmatched name with HandpickerSuite PushLocalList to generate a source image, then run the exporter again; a changed date bucket is rescanned for that checkpoint.

## OGN-ModelManager refresh

This node only places files where OGN-ModelManager already looks for them.

If thumbnails do not appear immediately, reload OGN-ModelManager or restart ComfyUI.

## Troubleshooting

If all checkpoints are reported as unmatched, first check `source_image_root`.

When `source_image_root` is empty, the exporter uses the current ComfyUI output directory only. If GM Image Saver is configured with an explicit output folder, or if your ComfyUI `output` folder is expected to be a junction/symlink, verify that it still points to the folder that actually contains generated images.

## 0.1.0 scope

- OGN-ModelManager only
- `.jpg` output only
- cold-scan candidate preference fixed to `latest`; valid cached representatives remain stable
- no overwrite mode
- no Civitai download
- no tag/favorite overlay burn-in
- no `.txt` memo generation
- no GraphicsMagick backend

## Design note

This node is intentionally file-system based.

It does not call OGN-ModelManager thumbnail APIs and does not modify OGN-ModelManager internal state.

## Development note

This project keeps implementation specs under `.spec/*.md`. The zip/build label (`TOOL_BUILD`, for example `v1e`) is intentionally not written to JPEG comments because build labels may change frequently during ChatGPT-assisted iterations. Generated JPEG comments use a stable `comment_schema=cte_comment_v1` marker instead.
