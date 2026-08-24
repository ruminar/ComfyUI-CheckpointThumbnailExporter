import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from PIL import Image


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPOSITORY_ROOT / "checkpoint_thumbnail_exporter.py"


def load_module():
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.web = types.SimpleNamespace(json_response=lambda value: value)
    folder_paths = types.ModuleType("folder_paths")
    folder_paths.get_output_directory = lambda: tempfile.gettempdir()
    folder_paths.get_user_directory = lambda: tempfile.gettempdir()
    folder_paths.get_filename_list = lambda _kind: []
    folder_paths.get_full_path = lambda _kind, _name: None

    class Routes:
        def post(self, _path):
            return lambda function: function

    class ServerInstance:
        routes = Routes()

        def send_sync(self, _event, _message):
            return None

    server = types.ModuleType("server")
    server.PromptServer = types.SimpleNamespace(instance=ServerInstance())

    sys.modules["aiohttp"] = aiohttp
    sys.modules["folder_paths"] = folder_paths
    sys.modules["server"] = server
    spec = importlib.util.spec_from_file_location("cte_source_index_test_module", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cte = load_module()


class PersistentSourceIndexTests(unittest.TestCase):
    def record(self, relative_name):
        checkpoint_path = Path("C:/models") / relative_name
        return cte.CheckpointRecord(
            relative_name=relative_name,
            path=checkpoint_path,
            source_keys=cte._checkpoint_source_keys(relative_name),
        )

    def touch(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"not-opened-by-index-tests")

    def run_lookup(self, source_root, index_path, all_records, missing_records):
        with mock.patch.object(cte, "_source_index_path", return_value=index_path):
            return cte._find_sources_with_persistent_index(
                source_root=source_root.resolve(),
                all_records=all_records,
                missing_records=missing_records,
                progress_cb=None,
                node_id="test",
            )

    def test_cold_date_bucket_full_scan_then_warm_negative_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            bucket = root / "anima3" / "20260824"
            self.touch(bucket / "BBB_0001.jpg")
            index_path = Path(temp) / "user" / "source_index.json"
            all_records = [self.record("AAA.safetensors"), self.record("BBB.safetensors")]

            missing = [self.record("AAA.safetensors")]
            first = self.run_lookup(root, index_path, all_records, missing)
            self.assertIsNone(missing[0].source_image)
            self.assertEqual(first["full_scans"], 1)
            self.assertEqual(first["files_seen"], 1)

            data = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(data["entries"][0]["bucket_type"], "date")
            self.assertIn("BBB.safetensors", data["entries"][0]["checkpoints"])
            self.assertTrue(data["entries"][0]["full_scan"]["complete"])

            missing_again = [self.record("AAA.safetensors")]
            second = self.run_lookup(root, index_path, all_records, missing_again)
            self.assertIsNone(missing_again[0].source_image)
            self.assertEqual(second["files_seen"], 0)
            self.assertGreaterEqual(second["negative_hits"], 1)

    def test_push_local_list_style_addition_invalidates_negative_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            bucket = root / "anima3" / "20260824"
            self.touch(bucket / "BBB_0001.jpg")
            index_path = Path(temp) / "source_index.json"
            all_records = [self.record("AAA.safetensors"), self.record("BBB.safetensors")]

            self.run_lookup(root, index_path, all_records, [self.record("AAA.safetensors")])
            old_mtime = bucket.stat().st_mtime_ns
            self.touch(bucket / "AAA_9999.jpg")
            # Make the directory-state transition deterministic even on filesystems
            # with coarse timestamp granularity.
            bucket.touch()
            if bucket.stat().st_mtime_ns == old_mtime:
                bucket_mtime = old_mtime + 1_000_000_000
                import os
                os.utime(bucket, ns=(bucket_mtime, bucket_mtime))

            target = self.record("AAA.safetensors")
            result = self.run_lookup(root, index_path, all_records, [target])
            self.assertEqual(target.source_image, bucket / "AAA_9999.jpg")
            self.assertEqual(result["targeted_scans"], 1)
            self.assertGreaterEqual(result["files_seen"], 2)

    def test_targeted_scan_has_no_image_count_cap(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            bucket = root / "anima3" / "20260824"
            self.touch(bucket / "BBB_0001.jpg")
            index_path = Path(temp) / "source_index.json"
            all_records = [self.record("AAA.safetensors"), self.record("BBB.safetensors")]
            self.run_lookup(root, index_path, all_records, [self.record("AAA.safetensors")])

            old_mtime = bucket.stat().st_mtime_ns
            self.touch(bucket / "AAA_9999.jpg")
            import os
            changed_mtime = max(bucket.stat().st_mtime_ns, old_mtime + 1_000_000_000)
            os.utime(bucket, ns=(changed_mtime, changed_mtime))

            target = self.record("AAA.safetensors")
            result = self.run_lookup(root, index_path, all_records, [target])
            self.assertEqual(target.source_image, bucket / "AAA_9999.jpg")
            self.assertEqual(result["files_seen"], 2)

    def test_deleted_representative_is_repaired_within_date_bucket(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            bucket = root / "anima3" / "20260824"
            first_image = bucket / "AAA_0001.jpg"
            self.touch(first_image)
            index_path = Path(temp) / "source_index.json"
            records = [self.record("AAA.safetensors")]

            first_target = self.record("AAA.safetensors")
            self.run_lookup(root, index_path, records, [first_target])
            self.assertEqual(first_target.source_image, first_image)

            old_bucket_mtime = bucket.stat().st_mtime_ns
            first_image.unlink()
            replacement = bucket / "AAA_0002.jpg"
            self.touch(replacement)
            # A representative deletion must trigger local repair even if a nested
            # filesystem layout leaves the date-directory mtime unchanged.
            import os
            os.utime(bucket, ns=(old_bucket_mtime, old_bucket_mtime))
            target = self.record("AAA.safetensors")
            result = self.run_lookup(root, index_path, records, [target])
            self.assertEqual(target.source_image, replacement)
            self.assertEqual(result["targeted_scans"], 1)

    def test_fallback_bucket_prunes_nested_date_bucket(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            self.touch(root / "pony" / "legacy" / "AAA_0001.jpg")
            self.touch(root / "pony" / "20260824" / "BBB_0001.jpg")
            index_path = Path(temp) / "source_index.json"
            records = [self.record("AAA.safetensors"), self.record("BBB.safetensors")]

            target = self.record("AAA.safetensors")
            self.run_lookup(root, index_path, records, [target])
            self.assertEqual(target.source_image, root / "pony" / "legacy" / "AAA_0001.jpg")

            data = json.loads(index_path.read_text(encoding="utf-8"))
            entries = {(entry["bucket_type"], entry["path"]): entry for entry in data["entries"]}
            self.assertIn(("date", "pony/20260824"), entries)
            self.assertIn(("fallback", "pony"), entries)
            self.assertNotIn("BBB.safetensors", entries[("fallback", "pony")]["checkpoints"])

    def test_source_root_change_replaces_index(self):
        with tempfile.TemporaryDirectory() as temp:
            index_path = Path(temp) / "source_index.json"
            first_root = Path(temp) / "first"
            second_root = Path(temp) / "second"
            self.touch(first_root / "20260823" / "AAA.jpg")
            self.touch(second_root / "20260824" / "BBB.jpg")

            self.run_lookup(first_root, index_path, [self.record("AAA.safetensors")], [self.record("AAA.safetensors")])
            self.run_lookup(second_root, index_path, [self.record("BBB.safetensors")], [self.record("BBB.safetensors")])
            data = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual(data["source_root"], cte._source_root_identity(second_root))
            self.assertEqual([entry["path"] for entry in data["entries"]], ["20260824"])

    def test_unvisited_older_bucket_remains_uninitialized_until_needed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            self.touch(root / "anima3" / "20260824" / "AAA.jpg")
            self.touch(root / "anima3" / "20260823" / "BBB.jpg")
            index_path = Path(temp) / "source_index.json"
            records = [self.record("AAA.safetensors"), self.record("BBB.safetensors")]

            aaa = self.record("AAA.safetensors")
            self.run_lookup(root, index_path, records, [aaa])
            data = json.loads(index_path.read_text(encoding="utf-8"))
            entries = {entry["path"]: entry for entry in data["entries"]}
            self.assertTrue(entries["anima3/20260824"]["initialized"])
            self.assertFalse(entries["anima3/20260823"]["initialized"])

            bbb = self.record("BBB.safetensors")
            result = self.run_lookup(root, index_path, records, [bbb])
            self.assertEqual(bbb.source_image, root / "anima3" / "20260823" / "BBB.jpg")
            self.assertEqual(result["full_scans"], 1)

    def test_existing_json_entry_order_survives_parent_mtime_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            import os

            root = Path(temp) / "source"
            anima = root / "anima3"
            pony = root / "pony"
            self.touch(anima / "20260824" / "AAA.jpg")
            self.touch(pony / "20260824" / "BBB.jpg")
            os.utime(anima, ns=(2_000_000_000, 2_000_000_000))
            os.utime(pony, ns=(1_000_000_000, 1_000_000_000))
            index_path = Path(temp) / "source_index.json"
            records = [self.record(name) for name in ("AAA.safetensors", "BBB.safetensors", "ZZZ.safetensors")]

            self.run_lookup(root, index_path, records, [self.record("ZZZ.safetensors")])
            first = json.loads(index_path.read_text(encoding="utf-8"))
            first_order = [entry["path"] for entry in first["entries"]]
            self.assertEqual(first_order, ["anima3/20260824", "pony/20260824"])

            os.utime(anima, ns=(1_000_000_000, 1_000_000_000))
            os.utime(pony, ns=(3_000_000_000, 3_000_000_000))
            self.run_lookup(root, index_path, records, [self.record("ZZZ.safetensors")])
            second = json.loads(index_path.read_text(encoding="utf-8"))
            self.assertEqual([entry["path"] for entry in second["entries"]], first_order)

    def test_cached_path_cannot_escape_bucket(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            bucket = root / "20260824"
            bucket.mkdir(parents=True)
            outside = root / "outside.jpg"
            self.touch(outside)
            entry = {"path": "20260824", "bucket_type": "date"}
            self.assertIsNone(cte._resolve_cached_image(root.resolve(), entry, "../outside.jpg"))

    def test_malformed_positive_entry_is_removed_and_targeted_scan_repairs_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "source"
            bucket = root / "20260824"
            self.touch(bucket / "AAA.jpg")
            index_path = Path(temp) / "source_index.json"
            records = [self.record("AAA.safetensors")]
            self.run_lookup(root, index_path, records, [self.record("AAA.safetensors")])

            data = json.loads(index_path.read_text(encoding="utf-8"))
            data["entries"][0]["checkpoints"]["AAA.safetensors"] = "broken"
            index_path.write_text(json.dumps(data), encoding="utf-8")

            target = self.record("AAA.safetensors")
            result = self.run_lookup(root, index_path, records, [target])
            self.assertEqual(target.source_image, bucket / "AAA.jpg")
            self.assertEqual(result["targeted_scans"], 1)

    def test_busy_exporter_returns_error_and_exception_releases_lock(self):
        self.assertTrue(cte._EXPORTER_RUN_LOCK.acquire(blocking=False))
        try:
            busy = cte._run_exporter({"operation": "install_missing"})
            self.assertFalse(busy["ok"])
            self.assertIn("already running", busy["report"])
        finally:
            cte._EXPORTER_RUN_LOCK.release()

        with mock.patch.object(cte, "_run_exporter_unlocked", side_effect=RuntimeError("boom")):
            with self.assertRaisesRegex(RuntimeError, "boom"):
                cte._run_exporter({})
        self.assertTrue(cte._EXPORTER_RUN_LOCK.acquire(blocking=False))
        cte._EXPORTER_RUN_LOCK.release()

    def test_dry_run_warms_index_and_execute_reuses_it(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root = root / "source"
            source_image = source_root / "anima3" / "20260824" / "AAA_0001.png"
            source_image.parent.mkdir(parents=True)
            Image.new("RGB", (64, 32), "red").save(source_image)
            checkpoint = root / "models" / "AAA.safetensors"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_bytes(b"checkpoint-placeholder")
            index_path = root / "user" / "source_index.json"

            def get_full_path(_kind, relative_name):
                return str(checkpoint) if relative_name == "AAA.safetensors" else None

            common_payload = {
                "node_id": "test",
                "source_image_root": str(source_root),
                "target_format": cte.TARGET_OGN,
                "operation": "install_missing",
                "max_size": 512,
                "jpeg_quality": 90,
            }
            with (
                mock.patch.object(cte, "_source_index_path", return_value=index_path),
                mock.patch.object(cte.folder_paths, "get_filename_list", return_value=["AAA.safetensors"]),
                mock.patch.object(cte.folder_paths, "get_full_path", side_effect=get_full_path),
            ):
                dry_result = cte._run_install_missing(
                    payload={**common_payload, "run_mode": "dry_run"},
                    progress_cb=None,
                )
                self.assertTrue(dry_result["ok"])
                self.assertEqual(dry_result["stats"]["would_install"], 1)
                self.assertTrue(index_path.is_file())
                self.assertFalse(checkpoint.with_suffix(".jpg").exists())

                execute_result = cte._run_install_missing(
                    payload={**common_payload, "run_mode": "execute"},
                    progress_cb=None,
                )

            thumbnail = checkpoint.with_suffix(".jpg")
            self.assertTrue(execute_result["ok"])
            self.assertEqual(execute_result["stats"]["installed"], 1)
            self.assertIn("Index cache hits: 1", execute_result["report"])
            self.assertIn("Source images scanned: 0", execute_result["report"])
            self.assertTrue(thumbnail.is_file())
            with Image.open(thumbnail) as image:
                self.assertEqual(image.size, (64, 32))
                comment = image.info["comment"].decode("utf-8")
            self.assertIn("comment_schema=cte_comment_v1", comment)

    def test_thumbnail_creation_never_overwrites_existing_sidecar(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.png"
            target = root / "target.jpg"
            Image.new("RGB", (8, 8), "red").save(source)
            target.write_bytes(b"manual-thumbnail")

            with self.assertRaises(FileExistsError):
                cte._create_thumbnail(source, target, 512, 90, "managed=true")
            self.assertEqual(target.read_bytes(), b"manual-thumbnail")
            self.assertEqual(list(root.glob("target.jpg.*.tmp")), [])

    def test_managed_thumbnail_requires_exact_schema_lines(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            managed = root / "managed.jpg"
            manual = root / "manual.jpg"
            required_comment = "\n".join([
                cte.TOOL_NAME,
                cte.MANAGED_MARKER,
                f"tool={cte.TOOL_NAME}",
                f"comment_schema={cte.COMMENT_SCHEMA}",
                f"target={cte.TARGET_OGN}",
            ])
            Image.new("RGB", (4, 4)).save(managed, format="JPEG", comment=required_comment.encode("utf-8"))
            misleading = f"prefix {cte.TOOL_NAME} suffix\nnot-{cte.MANAGED_MARKER}\ntarget={cte.TARGET_OGN}-copy"
            Image.new("RGB", (4, 4)).save(manual, format="JPEG", comment=misleading.encode("utf-8"))

            self.assertTrue(cte._is_managed_thumbnail(managed))
            self.assertFalse(cte._is_managed_thumbnail(manual))

    def test_uninstall_execute_does_not_remove_thumbnail_created_after_dry_run(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkpoint = root / "AAA.safetensors"
            checkpoint.write_bytes(b"checkpoint")

            def get_full_path(_kind, relative_name):
                return str(checkpoint) if relative_name == "AAA.safetensors" else None

            payload = {
                "node_id": "test",
                "target_format": cte.TARGET_OGN,
                "operation": "uninstall_managed",
            }
            with (
                mock.patch.object(cte.folder_paths, "get_filename_list", return_value=["AAA.safetensors"]),
                mock.patch.object(cte.folder_paths, "get_full_path", side_effect=get_full_path),
            ):
                dry = cte._run_uninstall_managed(
                    payload={**payload, "run_mode": "dry_run"},
                    progress_cb=None,
                )
                thumbnail = checkpoint.with_suffix(".jpg")
                comment = "\n".join([
                    cte.TOOL_NAME,
                    cte.MANAGED_MARKER,
                    f"tool={cte.TOOL_NAME}",
                    f"comment_schema={cte.COMMENT_SCHEMA}",
                    f"target={cte.TARGET_OGN}",
                ])
                Image.new("RGB", (4, 4)).save(thumbnail, format="JPEG", comment=comment.encode("utf-8"))
                execute = cte._run_uninstall_managed(
                    payload={**payload, "run_mode": "execute", "confirm_token": dry["confirm_token"]},
                    progress_cb=None,
                )

            self.assertTrue(thumbnail.exists())
            self.assertEqual(execute["stats"]["removed"], 0)
            self.assertIn("not confirmed or changed after dry run", execute["report"])

    def test_uninstall_execute_removes_unchanged_confirmed_thumbnail(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            checkpoint = root / "AAA.safetensors"
            checkpoint.write_bytes(b"checkpoint")
            thumbnail = checkpoint.with_suffix(".jpg")
            comment = "\n".join([
                cte.TOOL_NAME,
                cte.MANAGED_MARKER,
                f"tool={cte.TOOL_NAME}",
                f"comment_schema={cte.COMMENT_SCHEMA}",
                f"target={cte.TARGET_OGN}",
            ])
            Image.new("RGB", (4, 4)).save(thumbnail, format="JPEG", comment=comment.encode("utf-8"))

            def get_full_path(_kind, relative_name):
                return str(checkpoint) if relative_name == "AAA.safetensors" else None

            payload = {"node_id": "test", "target_format": cte.TARGET_OGN, "operation": "uninstall_managed"}
            with (
                mock.patch.object(cte.folder_paths, "get_filename_list", return_value=["AAA.safetensors"]),
                mock.patch.object(cte.folder_paths, "get_full_path", side_effect=get_full_path),
            ):
                dry = cte._run_uninstall_managed(payload={**payload, "run_mode": "dry_run"}, progress_cb=None)
                execute = cte._run_uninstall_managed(
                    payload={**payload, "run_mode": "execute", "confirm_token": dry["confirm_token"]},
                    progress_cb=None,
                )

            self.assertFalse(thumbnail.exists())
            self.assertEqual(execute["stats"]["removed"], 1)


if __name__ == "__main__":
    unittest.main()
