#!/usr/bin/env python3
"""Mocked unittest coverage for fetch_datasheet_vibe.py; never calls the API."""

from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fetch_datasheet_vibe as vibe  # noqa: E402

FAKE_API_KEY = "sk-super-secret-test-key-0001"
FAKE_BASE_URL = "https://datasheet-fetch.example.invalid"
KEY_A = "a" * 64
KEY_B = "b" * 64
PDF_BYTES = b"%PDF-1.4\nfake document\n%%EOF"
NOT_PDF_BYTES = b"<html>not a PDF</html>"


def _make_http_response(body: bytes):
    response = mock.MagicMock()
    response.__enter__.return_value.read.return_value = body
    response.__exit__.return_value = False
    return response


def _component_json(mpn: str = "TPS61023DRLR") -> bytes:
    return json.dumps(
        {
            "ok": True,
            "mpn": mpn,
            "mfr": "Example Manufacturer",
            "datasheets": [
                {
                    "key": KEY_A,
                    "pdf": f"/pdf/{KEY_A}",
                    "mpn": mpn,
                    "mfr": "Example Manufacturer",
                    "url": "https://untrusted.example/never-follow-this.pdf",
                    "source_url": "https://private.example/source",
                    "strategy": "catalog",
                    "validated": True,
                    "ts": "2026-10-01T00:00:00Z",
                    "notes": "NEVER PERSIST THIS NOTE",
                    "specs": {"private": "NEVER PERSIST SPECS"},
                    "log": "NEVER PERSIST THIS LOG",
                },
                {
                    "key": KEY_B,
                    "pdf": f"/pdf/{KEY_B}",
                    "mpn": mpn,
                    "mfr": "Example Manufacturer",
                    "url": "https://untrusted.example/also-never-follow.pdf",
                    "source_url": "https://private.example/other-source",
                    "strategy": "catalog",
                    "validated": True,
                    "ts": "2026-10-02T00:00:00Z",
                },
            ],
            "docs": [
                {
                    "doc_id": "doc/manual-1",
                    "name": "Application guide",
                    "doctype": "application_note",
                    "filename": "guide.PDF",
                    "size": len(PDF_BYTES),
                    "ts": "2026-10-03T00:00:00Z",
                },
                {
                    "doc_id": "datasheet-copy",
                    "name": "Duplicate datasheet",
                    "doctype": "datasheet",
                    "filename": "datasheet.pdf",
                    "size": len(PDF_BYTES),
                    "ts": "2026-10-04T00:00:00Z",
                },
                {
                    "doc_id": "text-doc",
                    "name": "Text file",
                    "doctype": "pdf",
                    "filename": "readme.txt",
                    "size": 20,
                    "ts": "2026-10-05T00:00:00Z",
                },
            ],
        }
    ).encode("utf-8")


def _call_path(call) -> str:
    return call.args[0].full_url.removeprefix(FAKE_BASE_URL)


class RedirectSafetyTests(unittest.TestCase):
    def test_redirect_is_rejected_without_requesting_destination(self):
        redirect_error = HTTPError(
            FAKE_BASE_URL + "/api/component/X",
            302,
            "Found",
            {"Location": "https://other-origin.example/redirected.pdf"},
            None,
        )
        with mock.patch.object(vibe, "urlopen", side_effect=redirect_error) as opener:
            with self.assertRaises(vibe.VibeRequestError) as ctx:
                vibe._request(
                    "/api/component/X",
                    api_key=FAKE_API_KEY,
                    base_url=FAKE_BASE_URL,
                    timeout_s=1,
                )
        opener.assert_called_once()
        self.assertNotIn("other-origin.example", str(ctx.exception))
        self.assertNotIn(FAKE_API_KEY, str(ctx.exception))


class PathValidationTests(unittest.TestCase):
    def test_datasheet_path_requires_matching_64_hex_key(self):
        self.assertEqual(vibe.validate_datasheet_path(KEY_A, f"/pdf/{KEY_A}"), f"/pdf/{KEY_A}")
        invalid_paths = (
            f"https://evil.example/pdf/{KEY_A}",
            f"//evil.example/pdf/{KEY_A}",
            f"/pdf/{KEY_B}",
            f"/fetch?url=https://evil.example/{KEY_A}",
            f"/pdf/{KEY_A}/../other",
        )
        for path in invalid_paths:
            with self.subTest(path=path), self.assertRaises(vibe.VibeRequestError):
                vibe.validate_datasheet_path(KEY_A, path)
        with self.assertRaises(vibe.VibeRequestError):
            vibe.validate_datasheet_path("short", "/pdf/short")

    def test_document_route_encodes_path_segments_and_fixed_query(self):
        self.assertEqual(
            vibe._document_path("A/B + C", "doc/id?other=x"),
            "/api/component/A%2FB%20%2B%20C/docs/doc%2Fid%3Fother%3Dx?dl=1",
        )

    def test_component_path_encodes_mpn_as_single_segment(self):
        self.assertEqual(
            vibe._component_path("TPS/61023 DR+LR"),
            "/api/component/TPS%2F61023%20DR%2BLR",
        )

    def test_filename_components_are_sanitized_and_collision_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = vibe.resolve_asset_path("../A", "doc", "folder/../x", root)
            second = vibe.resolve_asset_path("..\\A", "doc", "folder/../x", root)
            self.assertEqual(first.resolve().parent, root.resolve())
            self.assertEqual(second.resolve().parent, root.resolve())
            self.assertNotEqual(first.name, second.name)
            self.assertNotIn("/", first.name)
            self.assertNotIn("\\", first.name)


class FetchComponentTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.output_dir = Path(self._tmpdir.name) / "datasheets"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _fetch(self, **kwargs):
        defaults = {
            "api_key": FAKE_API_KEY,
            "base_url": FAKE_BASE_URL,
        }
        defaults.update(kwargs)
        return vibe.fetch_component("TPS61023DRLR", self.output_dir, **defaults)

    def test_downloads_all_datasheets_and_pdf_docs_regardless_of_doctype(self):
        responses = [_make_http_response(_component_json())] + [
            _make_http_response(PDF_BYTES) for _ in range(4)
        ]
        with mock.patch.object(vibe, "urlopen", side_effect=responses) as mocked:
            result = self._fetch()

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["counts"]["downloaded"], 4)
        self.assertEqual(result["counts"]["total"], 4)
        self.assertEqual(result["skipped_non_pdf_docs"], 1)
        self.assertEqual([asset["type"] for asset in result["assets"]], [
            "datasheet", "datasheet", "doc", "doc",
        ])
        self.assertEqual(len(list(self.output_dir.glob("*.pdf"))), 4)
        self.assertEqual(mocked.call_count, 5)
        inventory = json.loads(
            (self.output_dir / "vibe" / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(inventory["version"], 1)
        entry = inventory["mpns"]["TPS61023DRLR"]
        self.assertEqual(entry["status"], "complete")
        self.assertFalse(entry["stale"])
        for asset in entry["assets"].values():
            self.assertEqual(set(asset), {
                "type", "id", "filepath", "sha256", "size", "status", "metadata",
            })
            self.assertNotIn(FAKE_API_KEY, json.dumps(asset))
            self.assertNotIn('"url":', json.dumps(asset).lower())
            self.assertNotIn('"source_url":', json.dumps(asset).lower())
            self.assertNotIn('"notes":', json.dumps(asset).lower())
            self.assertNotIn('"specs":', json.dumps(asset).lower())
            self.assertNotIn('"log":', json.dumps(asset).lower())
        self.assertNotIn("NEVER PERSIST", json.dumps(inventory))
        paths = [_call_path(call) for call in mocked.call_args_list]
        self.assertEqual(paths[0], "/api/component/TPS61023DRLR")
        self.assertIn(f"/pdf/{KEY_A}", paths)
        self.assertIn(f"/pdf/{KEY_B}", paths)
        self.assertIn("/api/component/TPS61023DRLR/docs/doc%2Fmanual-1?dl=1", paths)
        self.assertIn("/api/component/TPS61023DRLR/docs/datasheet-copy?dl=1", paths)
        self.assertFalse(any("readme.txt" in path for path in paths))
        self.assertFalse(any("untrusted.example" in path for path in paths))
        self.assertFalse(any("source_url" in path for path in paths))

    def test_api_key_is_bearer_only_and_never_in_results_or_errors(self):
        with mock.patch.object(
            vibe,
            "urlopen",
            side_effect=[_make_http_response(_component_json()), _make_http_response(PDF_BYTES)]
            + [_make_http_response(PDF_BYTES) for _ in range(3)],
        ) as mocked:
            result = self._fetch()
        request = mocked.call_args_list[0].args[0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(request.get_header("Authorization"), f"Bearer {FAKE_API_KEY}")
        self.assertNotIn(FAKE_API_KEY, request.full_url)
        self.assertNotIn(FAKE_API_KEY, str(result))

        http_error = HTTPError("https://private-url.invalid/path", 401, FAKE_API_KEY, None, None)
        with mock.patch.object(vibe, "urlopen", side_effect=http_error):
            failed = self._fetch()
        self.assertEqual(failed["status"], "error")
        self.assertNotIn(FAKE_API_KEY, str(failed))
        self.assertNotIn("private-url.invalid", str(failed))

    def test_bad_datasheet_path_is_rejected_without_request_and_other_assets_continue(self):
        component = json.loads(_component_json())
        component["datasheets"] = [component["datasheets"][0]]
        component["datasheets"][0]["pdf"] = "https://evil.example/pdf/" + KEY_A
        component["docs"] = component["docs"][:1]
        responses = [_make_http_response(json.dumps(component).encode()), _make_http_response(PDF_BYTES)]
        with mock.patch.object(vibe, "urlopen", side_effect=responses) as mocked:
            result = self._fetch()
        self.assertEqual(mocked.call_count, 2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["assets"][0]["status"], "error")
        self.assertEqual(result["assets"][1]["status"], "downloaded")
        self.assertTrue(self.output_dir.exists())

    def test_per_asset_failure_continues_to_later_assets(self):
        component = json.loads(_component_json())
        component["docs"] = component["docs"][:2]
        bad_pdf_path = f"{FAKE_BASE_URL}/pdf/{KEY_A}"
        http_error = HTTPError(bad_pdf_path, 503, "unavailable", None, None)
        responses = [
            _make_http_response(json.dumps(component).encode()),
            http_error,
            _make_http_response(PDF_BYTES),
            _make_http_response(NOT_PDF_BYTES),
            _make_http_response(PDF_BYTES),
        ]
        with mock.patch.object(vibe, "urlopen", side_effect=responses) as mocked:
            result = self._fetch()
        self.assertEqual(mocked.call_count, 5)
        self.assertEqual(result["status"], "partial")
        statuses = [asset["status"] for asset in result["assets"]]
        self.assertEqual(statuses, ["error", "downloaded", "non_pdf", "downloaded"])
        self.assertEqual(result["counts"]["error"], 1)
        self.assertEqual(result["counts"]["non_pdf"], 1)
        self.assertEqual(result["counts"]["downloaded"], 2)
        self.assertEqual(len(list(self.output_dir.glob("*.pdf"))), 2)
        self.assertNotIn("private", str(result))

    def test_not_found_component_and_request_error_are_reported(self):
        http_404 = HTTPError("https://private.invalid/component", 404, "missing", None, None)
        with mock.patch.object(vibe, "urlopen", side_effect=http_404):
            not_found = self._fetch()
        self.assertEqual(not_found["status"], "not_found")
        self.assertEqual(not_found["counts"]["total"], 0)

        with mock.patch.object(vibe, "urlopen", side_effect=URLError("offline")):
            failed = self._fetch()
        self.assertEqual(failed["status"], "error")
        self.assertNotIn("offline", str(failed))

    def test_asset_404_marks_component_result_partial(self):
        component = json.loads(_component_json())
        component["datasheets"] = [component["datasheets"][0]]
        component["docs"] = []
        missing_pdf = HTTPError(
            f"{FAKE_BASE_URL}/pdf/{KEY_A}", 404, "missing PDF", None, None
        )
        with mock.patch.object(
            vibe,
            "urlopen",
            side_effect=[
                _make_http_response(json.dumps(component).encode()),
                missing_pdf,
            ],
        ):
            result = self._fetch()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["assets"][0]["status"], "not_found")
        self.assertEqual(result["counts"]["not_found"], 1)

    def test_identity_cache_requires_valid_pdf_and_force_refetches(self):
        responses = [_make_http_response(_component_json())] + [
            _make_http_response(PDF_BYTES) for _ in range(4)
        ] + [_make_http_response(_component_json())] + [_make_http_response(_component_json())] + [
            _make_http_response(PDF_BYTES) for _ in range(4)
        ]
        with mock.patch.object(vibe, "urlopen", side_effect=responses) as mocked:
            first = self._fetch()
            second = self._fetch()
            forced = self._fetch(force=True)
        self.assertEqual(first["counts"]["downloaded"], 4)
        self.assertEqual(second["counts"]["cached"], 4)
        self.assertEqual(forced["counts"]["downloaded"], 4)
        self.assertEqual(mocked.call_count, 1 + 4 + 1 + 1 + 4)

    def test_manifest_cache_hash_hit_and_tamper_refetch(self):
        inventory = _component_json()
        with mock.patch.object(
            vibe, "urlopen", side_effect=[_make_http_response(inventory)]
            + [_make_http_response(PDF_BYTES) for _ in range(4)]
            + [_make_http_response(inventory)]
            + [_make_http_response(inventory)]
            + [_make_http_response(PDF_BYTES)]
        ) as mocked:
            first = self._fetch()
            second = self._fetch()
            manifest_path = self.output_dir / "vibe" / "manifest.json"
            record = json.loads(manifest_path.read_text(encoding="utf-8"))["mpns"][
                "TPS61023DRLR"
            ]["assets"][f"datasheet:{KEY_A}"]
            (self.output_dir / record["filepath"]).write_bytes(b"%PDF-tampered")
            third = self._fetch()
        self.assertEqual(first["counts"]["downloaded"], 4)
        self.assertEqual(second["counts"]["cached"], 4)
        self.assertEqual(third["counts"]["downloaded"], 1)
        self.assertEqual(third["counts"]["cached"], 3)
        self.assertEqual(mocked.call_count, 1 + 4 + 1 + 1 + 1)

    def test_inventory_failure_preserves_assets_and_marks_entry_stale(self):
        with mock.patch.object(
            vibe, "urlopen", side_effect=[_make_http_response(_component_json())]
            + [_make_http_response(PDF_BYTES) for _ in range(4)]
        ):
            self._fetch()
        with mock.patch.object(
            vibe, "urlopen", side_effect=URLError("offline")
        ):
            failed = self._fetch()
        self.assertEqual(failed["status"], "error")
        stored = json.loads(
            (self.output_dir / "vibe" / "manifest.json").read_text(encoding="utf-8")
        )["mpns"]["TPS61023DRLR"]
        self.assertEqual(stored["status"], "failed")
        self.assertTrue(stored["stale"])
        self.assertEqual(len(stored["assets"]), 4)
        self.assertEqual(failed["counts"]["total"], 0)

    def test_manifest_merges_distinct_normalized_mpns(self):
        components = [
            json.dumps({"ok": True, "mpn": mpn, "datasheets": [], "docs": []}).encode()
            for mpn in ("TPS61023DRLR", "LM2596-ADJ")
        ]
        with mock.patch.object(
            vibe, "urlopen", side_effect=[_make_http_response(item) for item in components]
        ):
            self._fetch()
            vibe.fetch_component(
                "LM2596-ADJ", self.output_dir, api_key=FAKE_API_KEY, base_url=FAKE_BASE_URL
            )
        mpns = json.loads(
            (self.output_dir / "vibe" / "manifest.json").read_text(encoding="utf-8")
        )["mpns"]
        self.assertEqual(set(mpns), {"TPS61023DRLR", "LM2596-ADJ"})

    def test_corrupt_manifest_is_preserved_and_manifest_write_failure_is_reported(self):
        manifest_path = self.output_dir / "vibe" / "manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text("{not-json", encoding="utf-8")
        with mock.patch.object(
            vibe, "urlopen", side_effect=[_make_http_response(_component_json())]
            + [_make_http_response(PDF_BYTES) for _ in range(4)]
        ):
            result = self._fetch()
        self.assertEqual(manifest_path.read_text(encoding="utf-8"), "{not-json")
        self.assertIn("manifest_error", result)
        self.assertEqual(result["counts"]["downloaded"], 4)
        self.assertEqual(len(list(self.output_dir.glob("*.pdf"))), 4)

        (manifest_path.parent / "manifest.json").unlink()
        real_replace = vibe.os.replace

        def fail_manifest_replace(source, destination):
            if str(destination).endswith("manifest.json"):
                raise OSError("blocked")
            return real_replace(source, destination)

        with mock.patch.object(vibe.os, "replace", side_effect=fail_manifest_replace):
            with mock.patch.object(
                vibe, "urlopen", side_effect=[_make_http_response(_component_json())]
                + [_make_http_response(PDF_BYTES) for _ in range(4)]
            ):
                failed_write = self._fetch(force=True)
        self.assertIn("manifest_error", failed_write)
        self.assertEqual(failed_write["counts"]["downloaded"], 4)
        self.assertEqual(len(list(self.output_dir.glob("*.pdf"))), 4)
        self.assertFalse(manifest_path.exists())

    def test_cached_legacy_mpn_pdf_does_not_skip_component_inventory(self):
        self.output_dir.mkdir()
        (self.output_dir / "TPS61023DRLR.pdf").write_bytes(PDF_BYTES)
        responses = [_make_http_response(_component_json())] + [
            _make_http_response(PDF_BYTES) for _ in range(4)
        ]
        with mock.patch.object(vibe, "urlopen", side_effect=responses) as mocked:
            result = self._fetch()
        self.assertEqual(mocked.call_count, 5)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["counts"]["downloaded"], 4)
        self.assertTrue((self.output_dir / "TPS61023DRLR.pdf").exists())

    def test_component_json_validates_ok_and_mpn(self):
        bad_shapes = [
            b"not json",
            json.dumps({"ok": True, "mpn": "X", "datasheets": [], "docs": []}).encode(),
            json.dumps({"ok": True, "mpn": "OTHER", "datasheets": [], "docs": []}).encode(),
            json.dumps({"ok": False, "mpn": "TPS61023DRLR", "datasheets": [], "docs": []}).encode(),
        ]
        for payload in bad_shapes:
            with self.subTest(payload=payload), mock.patch.object(
                vibe, "urlopen", return_value=_make_http_response(payload)
            ):
                result = self._fetch()
            self.assertIn(result["status"], ("error", "not_found"))
            self.assertEqual(result["counts"]["total"], 0)


class DryRunAndCliTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.root = Path(self._tmpdir.name)
        self.output_dir = self.root / "datasheets"

    def tearDown(self):
        self._tmpdir.cleanup()

    def _run_main(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr), redirect_stdout(stdout):
            code = vibe.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_dry_run_unknown_counts_no_network_writes_or_key(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
            vibe, "urlopen"
        ) as mocked:
            code, stdout, _ = self._run_main(
                ["TPS61023DRLR", "--output", str(self.output_dir), "--dry-run", "--json"]
            )
        mocked.assert_not_called()
        self.assertEqual(code, 0)
        self.assertFalse(self.output_dir.exists())
        manifest = json.loads(stdout)
        self.assertEqual(manifest["asset_inventory"], "unknown")
        self.assertIsNone(manifest["asset_counts"])
        self.assertEqual(manifest["results"][0]["asset_count"], None)
        self.assertEqual(manifest["results"][0]["status"], "would_fetch")

    def test_dry_run_reads_only_current_manifest_without_writes_or_network(self):
        initial = _component_json()
        with mock.patch.object(
            vibe, "urlopen", side_effect=[_make_http_response(initial)]
            + [_make_http_response(PDF_BYTES) for _ in range(4)]
        ):
            vibe.fetch_component(
                "TPS61023DRLR", self.output_dir,
                api_key=FAKE_API_KEY, base_url=FAKE_BASE_URL,
            )
        manifest_path = self.output_dir / "vibe" / "manifest.json"
        before = manifest_path.read_bytes()
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
            vibe, "urlopen"
        ) as mocked, mock.patch.object(vibe, "_write_manifest") as writer:
            result = vibe.preview_component("TPS61023DRLR", self.output_dir)
        mocked.assert_not_called()
        writer.assert_not_called()
        self.assertEqual(result["asset_inventory"], "cached")
        self.assertEqual(result["asset_count"], 4)
        self.assertEqual(manifest_path.read_bytes(), before)

        cached_paths = json.loads(before)["mpns"]["TPS61023DRLR"]["assets"]
        (self.output_dir / next(iter(cached_paths.values()))["filepath"]).write_bytes(b"tampered")
        with mock.patch.object(vibe, "urlopen") as mocked:
            unknown = vibe.preview_component("TPS61023DRLR", self.output_dir)
        mocked.assert_not_called()
        self.assertEqual(unknown["asset_inventory"], "unknown")
        self.assertIsNone(unknown["asset_count"])

    def test_dry_run_never_calls_existing_single_pdf_complete_cache(self):
        self.output_dir.mkdir()
        (self.output_dir / "TPS61023DRLR.pdf").write_bytes(PDF_BYTES)
        with mock.patch.object(vibe, "urlopen") as mocked:
            result = vibe.preview_component("TPS61023DRLR", self.output_dir)
        mocked.assert_not_called()
        self.assertEqual(result["status"], "would_fetch")
        self.assertEqual(result["asset_inventory"], "unknown")
        self.assertTrue(result["legacy_single_pdf_present"])

    def test_explicit_mpn_cli_compatibility_and_json_manifest(self):
        responses = [_make_http_response(_component_json())] + [
            _make_http_response(PDF_BYTES) for _ in range(4)
        ]
        with mock.patch.dict(
            "os.environ",
            {vibe.API_KEY_ENV: FAKE_API_KEY, vibe.BASE_URL_ENV: FAKE_BASE_URL},
            clear=True,
        ), mock.patch.object(vibe, "urlopen", side_effect=responses) as mocked:
            code, stdout, _ = self._run_main(
                ["TPS61023DRLR", "--output", str(self.output_dir), "--json"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(mocked.call_count, 5)
        manifest = json.loads(stdout)
        self.assertEqual(manifest["results"][0]["mpn"], "TPS61023DRLR")
        self.assertEqual(manifest["counts"]["assets"]["downloaded"], 4)
        self.assertNotIn(FAKE_API_KEY, stdout)

    def test_prompts_without_echo_when_key_environment_variable_is_absent(self):
        responses = [_make_http_response(json.dumps({
            "ok": True, "mpn": "X", "mfr": "", "datasheets": [], "docs": [],
        }).encode())]
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
            vibe, "urlopen", side_effect=responses
        ) as mocked, mock.patch.object(vibe.getpass, "getpass", return_value=FAKE_API_KEY) as prompt:
            code, stdout, stderr = self._run_main(["X", "--output", str(self.output_dir), "--json"])
        prompt.assert_called_once()
        self.assertEqual(code, 0)
        self.assertEqual(mocked.call_count, 1)
        self.assertNotIn(FAKE_API_KEY, stdout + stderr)
        self.assertEqual(mocked.call_args.args[0].get_header("Authorization"), f"Bearer {FAKE_API_KEY}")

    def test_missing_key_after_prompt_is_error_without_network(self):
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(
            vibe, "urlopen"
        ) as mocked, mock.patch.object(vibe.getpass, "getpass", return_value=""):
            code, stdout, _ = self._run_main(["X", "--output", str(self.output_dir), "--json"])
        mocked.assert_not_called()
        self.assertEqual(code, 1)
        self.assertFalse(self.output_dir.exists())
        self.assertEqual(json.loads(stdout)["results"][0]["status"], "error")

    def test_roster_still_fetches_only_candidates(self):
        roster_path = self.root / "review_coverage.json"
        roster_path.write_text(
            json.dumps({
                "candidates": [{"mpn": "TPS61023DRLR"}],
                "missing": [{"mpn": "MUST-NOT-FETCH"}],
            }),
            encoding="utf-8",
        )
        with mock.patch.dict("os.environ", {}, clear=True), mock.patch.object(vibe, "urlopen") as mocked:
            code, stdout, _ = self._run_main([
                "--roster", str(roster_path), "--output", str(self.output_dir), "--dry-run", "--json",
            ])
        mocked.assert_not_called()
        self.assertEqual(code, 0)
        mpns = [item["mpn"] for item in json.loads(stdout)["results"]]
        self.assertEqual(mpns, ["TPS61023DRLR"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
