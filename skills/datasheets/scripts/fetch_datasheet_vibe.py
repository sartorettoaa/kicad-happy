#!/usr/bin/env python3
"""Download every PDF asset for MPNs in the Tractian Vibe component catalog.

For each requested MPN, the script fetches the component record from
``GET /api/component/{mpn}``, downloads every listed datasheet PDF, and
 downloads every ``docs`` entry whose filename ends in ``.pdf`` (case
insensitive). Other document types are ignored, regardless of ``doctype``.
Only the API-provided relative ``/pdf/{key}`` datasheet path and the
component-scoped document download route are used; arbitrary source URLs are
never followed.

Credentials:
    TRACTIAN_DATASHEET_API_KEY  — optional environment variable. If absent,
                                  the interactive CLI prompts with getpass.
                                  The value is sent only as a Bearer token and
                                  is never logged or included in output.
    TRACTIAN_DATASHEET_BASE_URL — optional HTTPS base URL override (default:
                                  https://datasheet-fetch.vibe.tractian.com).

Usage:
    python3 fetch_datasheet_vibe.py TPS61023DRLR
    python3 fetch_datasheet_vibe.py TPS61023DRLR LM2596-ADJ --project /path/to/project
    python3 fetch_datasheet_vibe.py --roster review_coverage.json --project /path/to/project
    python3 fetch_datasheet_vibe.py TPS61023DRLR --dry-run --json

A real run always queries the component endpoint before consulting the
persistent cache in ``<output_dir>/vibe/manifest.json``. Cached PDFs are used
only when their recorded path, signature, size, and SHA-256 still match. Dry-run
makes no network calls or writes; it reads a current manifest entry only to
report its cached asset count.

Dependencies: Python standard library only.
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

API_KEY_ENV = "TRACTIAN_DATASHEET_API_KEY"
BASE_URL_ENV = "TRACTIAN_DATASHEET_BASE_URL"
DEFAULT_BASE_URL = "https://datasheet-fetch.vibe.tractian.com"
DEFAULT_TIMEOUT_S = 15.0
DOWNLOAD_TIMEOUT_S = 120.0
PDF_STATUSES = ("downloaded", "cached", "not_found", "error", "non_pdf")
MANIFEST_VERSION = 1
MANIFEST_RELATIVE_PATH = Path("vibe") / "manifest.json"
DISPLAY_METADATA_FIELDS = {
    "datasheet": ("mpn", "mfr", "strategy", "validated", "ts"),
    "doc": ("name", "doctype", "filename", "size", "ts"),
}
_MANIFEST_STATUSES = ("complete", "partial", "failed")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

_UNSAFE_CHARS_RE = re.compile(r"[^A-Za-z0-9_.-]")
_DATASHEET_KEY_RE = re.compile(r"[0-9a-fA-F]{64}\Z")


def _manifest_path(output_dir: Path) -> Path:
    """Return the manifest path while rejecting a redirected ``vibe`` directory."""
    root = output_dir.resolve()
    path = output_dir / MANIFEST_RELATIVE_PATH
    resolved_parent = path.parent.resolve()
    if resolved_parent != root and root not in resolved_parent.parents:
        raise OSError("Vibe manifest directory escaped the output directory")
    return path


def _empty_manifest() -> Dict[str, object]:
    return {"version": MANIFEST_VERSION, "mpns": {}}


def _manifest_asset_key(asset_type: str, asset_id: str) -> str:
    stable_id = asset_id.lower() if asset_type == "datasheet" else asset_id
    return f"{asset_type}:{stable_id}"


def _valid_manifest_metadata(value: object, asset_type: str) -> bool:
    if not isinstance(value, dict):
        return False
    allowed = set(DISPLAY_METADATA_FIELDS[asset_type])
    if any(key not in allowed for key in value):
        return False
    for item in value.values():
        if isinstance(item, bool) or isinstance(item, int):
            continue
        if not isinstance(item, str) or len(item) > 512:
            return False
        if "://" in item or item.lower().startswith("www."):
            return False
    return True


def _validate_manifest(data: object) -> Dict[str, object]:
    """Validate the complete persisted schema; reject unknown fields fail-closed."""
    if not isinstance(data, dict) or set(data) != {"version", "mpns"}:
        raise ValueError("Invalid Vibe manifest structure")
    if data.get("version") != MANIFEST_VERSION or not isinstance(data.get("mpns"), dict):
        raise ValueError("Unsupported Vibe manifest version")
    for normalized_mpn, entry in data["mpns"].items():
        if not isinstance(normalized_mpn, str) or not normalized_mpn:
            raise ValueError("Invalid Vibe manifest MPN key")
        required = {
            "requested_mpn", "returned_mpn", "checked_at", "status", "stale", "assets"
        }
        if not isinstance(entry, dict) or set(entry) != required:
            raise ValueError("Invalid Vibe manifest MPN entry")
        requested = entry.get("requested_mpn")
        returned = entry.get("returned_mpn")
        if (
            not isinstance(requested, str)
            or _normalize_mpn(requested) != normalized_mpn
            or (returned is not None and (
                not isinstance(returned, str) or _normalize_mpn(returned) != normalized_mpn
            ))
            or not isinstance(entry.get("checked_at"), str)
            or entry.get("status") not in _MANIFEST_STATUSES
            or not isinstance(entry.get("stale"), bool)
            or not isinstance(entry.get("assets"), dict)
        ):
            raise ValueError("Invalid Vibe manifest MPN entry")
        for asset_key, record in entry["assets"].items():
            if not isinstance(record, dict) or set(record) != {
                "type", "id", "filepath", "sha256", "size", "status", "metadata"
            }:
                raise ValueError("Invalid Vibe manifest asset record")
            asset_type = record.get("type")
            asset_id = record.get("id")
            filepath = record.get("filepath")
            sha256 = record.get("sha256")
            size = record.get("size")
            if (
                asset_type not in DISPLAY_METADATA_FIELDS
                or not isinstance(asset_id, str)
                or asset_key != _manifest_asset_key(asset_type, asset_id)
                or not isinstance(filepath, str)
                or not isinstance(record.get("status"), str)
                or (sha256 is not None and (
                    not isinstance(sha256, str) or not _SHA256_RE.fullmatch(sha256)
                ))
                or (size is not None and (not isinstance(size, int) or size < 0))
                or not _valid_manifest_metadata(record.get("metadata"), asset_type)
            ):
                raise ValueError("Invalid Vibe manifest asset record")
    return data


def _load_manifest(output_dir: Path) -> Tuple[Optional[Dict[str, object]], bool]:
    """Read a valid manifest; return ``(None, True)`` for corrupt/unreadable data."""
    try:
        path = _manifest_path(output_dir)
        if not path.exists():
            return _empty_manifest(), False
        data = json.loads(path.read_text(encoding="utf-8"))
        return _validate_manifest(data), False
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        return None, True


def _write_manifest(output_dir: Path, data: Dict[str, object]) -> None:
    """Atomically replace the manifest with validated JSON from a sibling temp file."""
    _validate_manifest(data)
    path = _manifest_path(output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=".manifest-", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(data, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(str(temporary_path), str(path))
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except OSError:
                pass


def _safe_display_metadata(asset_type: str, raw: object, api_key: str) -> Dict[str, object]:
    """Select small, non-sensitive display fields without copying raw API data."""
    if not isinstance(raw, dict):
        return {}
    metadata: Dict[str, object] = {}
    for field in DISPLAY_METADATA_FIELDS[asset_type]:
        value = raw.get(field)
        if isinstance(value, bool) or isinstance(value, int):
            metadata[field] = value
        elif isinstance(value, str) and len(value) <= 512:
            if api_key and api_key in value:
                continue
            if "://" in value or value.lower().startswith("www."):
                continue
            metadata[field] = value
    return metadata


def _relative_pdf_path(output_dir: Path, pdf_path: Path) -> str:
    """Return a safe relative PDF path contained by the output directory."""
    root = output_dir.resolve()
    resolved = pdf_path.resolve()
    if resolved == root or root not in resolved.parents:
        raise VibeRequestError("Resolved datasheet path escaped the destination directory")
    relative = resolved.relative_to(root)
    if relative.suffix.lower() != ".pdf" or len(relative.parts) != 1:
        raise VibeRequestError("Manifest asset path is not a contained PDF filename")
    return PurePosixPath(*relative.parts).as_posix()


def _manifest_pdf_path(output_dir: Path, filepath: object) -> Optional[Path]:
    """Resolve only a single-component relative PDF path contained by output_dir."""
    if not isinstance(filepath, str) or "\\" in filepath:
        return None
    relative = PurePosixPath(filepath)
    if (
        relative.is_absolute()
        or len(relative.parts) != 1
        or relative.parts[0] in ("", ".", "..")
        or relative.suffix.lower() != ".pdf"
    ):
        return None
    candidate = output_dir / relative.parts[0]
    root = output_dir.resolve()
    resolved = candidate.resolve()
    if resolved == root or root not in resolved.parents:
        return None
    return candidate


def _verified_manifest_asset(
    record: object,
    *,
    asset_type: str,
    asset_id: str,
    expected_path: Path,
    output_dir: Path,
) -> Optional[Tuple[int, str]]:
    """Verify identity, contained path, PDF signature, recorded size, and hash."""
    if not isinstance(record, dict):
        return None
    if record.get("type") != asset_type or record.get("id") != (
        asset_id.lower() if asset_type == "datasheet" else asset_id
    ):
        return None
    path = _manifest_pdf_path(output_dir, record.get("filepath"))
    if path is None or path.resolve() != expected_path.resolve():
        return None
    try:
        body = path.read_bytes()
    except OSError:
        return None
    digest = hashlib.sha256(body).hexdigest()
    if (
        not body.startswith(b"%PDF")
        or record.get("size") != len(body)
        or record.get("sha256") != digest
        or record.get("status") not in ("downloaded", "cached")
    ):
        return None
    return len(body), digest


def _record_manifest_entry(
    output_dir: Path,
    mpn: str,
    entry: Dict[str, object],
) -> Optional[str]:
    """Merge one MPN record into the latest valid manifest and atomically save it."""
    manifest, corrupt = _load_manifest(output_dir)
    if corrupt or manifest is None:
        return "Vibe manifest is invalid or unreadable; it was not modified"
    mpns = manifest["mpns"]
    mpns[_normalize_mpn(mpn)] = entry
    try:
        _write_manifest(output_dir, manifest)
    except (OSError, TypeError, ValueError):
        return "Vibe manifest could not be written"
    return None


def _manifest_entry_from_result(
    mpn: str,
    returned_mpn: str,
    assets: List[Dict[str, object]],
    raw_assets: List[Tuple[str, str, object]],
    output_dir: Path,
    api_key: str,
    status: str,
) -> Dict[str, object]:
    """Build the minimal allowlisted inventory record from a completed fetch."""
    records: Dict[str, Dict[str, object]] = {}
    for asset, (asset_type, asset_id, raw_entry) in zip(assets, raw_assets):
        if asset.get("status") not in ("downloaded", "cached"):
            continue
        output = asset.get("output")
        size = asset.get("size_bytes")
        digest = asset.get("sha256")
        if not isinstance(output, str) or not isinstance(size, int) or not isinstance(digest, str):
            continue
        try:
            filepath = _relative_pdf_path(output_dir, Path(output))
        except (OSError, VibeRequestError):
            continue
        records[_manifest_asset_key(asset_type, asset_id)] = {
            "type": asset_type,
            "id": asset_id.lower() if asset_type == "datasheet" else asset_id,
            "filepath": filepath,
            "sha256": digest,
            "size": size,
            "status": asset["status"],
            "metadata": _safe_display_metadata(asset_type, raw_entry, api_key),
        }
    return {
        "requested_mpn": mpn,
        "returned_mpn": returned_mpn,
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status,
        "stale": status != "complete",
        "assets": records,
    }


def _failed_manifest_entry(
    mpn: str,
    previous: Optional[Dict[str, object]],
) -> Dict[str, object]:
    """Mark the latest inventory attempt stale while preserving last-known assets."""
    old_assets = previous.get("assets", {}) if isinstance(previous, dict) else {}
    if not isinstance(old_assets, dict):
        old_assets = {}
    return {
        "requested_mpn": mpn,
        "returned_mpn": None,
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": "failed",
        "stale": True,
        "assets": old_assets,
    }


class _RejectRedirectHandler(HTTPRedirectHandler):
    """Reject redirects so bearer credentials never cross API origins."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = build_opener(_RejectRedirectHandler)


def urlopen(request: Request, timeout: float):
    """Open a request without following redirects.

    Kept as a module-level seam so the network can be fully mocked in tests.
    """
    return _NO_REDIRECT_OPENER.open(request, timeout=timeout)


class VibeRequestError(RuntimeError):
    """Safe-to-report API/request failure; never includes a URL or credential."""

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


class RosterError(ValueError):
    """Malformed ``--roster`` input; roster processing fails closed."""


def get_api_key() -> str:
    """Read the API key only from the documented environment variable."""
    return os.environ.get(API_KEY_ENV, "").strip()


def get_base_url() -> str:
    """Read the catalog base URL and remove trailing slashes."""
    return os.environ.get(BASE_URL_ENV, DEFAULT_BASE_URL).rstrip("/")


def _validate_base_url(base_url: str) -> None:
    """Require an HTTPS origin before attaching the bearer credential."""
    parsed = urlsplit(base_url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise VibeRequestError("Refusing an invalid or non-HTTPS Vibe base URL")


def _headers(api_key: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {api_key}", "Accept": "application/json, application/pdf"}


def _normalize_mpn(mpn: str) -> str:
    return mpn.strip().upper()


def sanitize_mpn_for_path(mpn: str) -> str:
    """Convert an MPN to a traversal-safe, single-component filename stem."""
    cleaned = _UNSAFE_CHARS_RE.sub("_", mpn.strip()).strip(". ")
    return cleaned or "_"


def resolve_pdf_path(mpn: str, output_dir: Path) -> Path:
    """Return the legacy ``<MPN>.pdf`` path, contained under ``output_dir``."""
    candidate = output_dir / f"{sanitize_mpn_for_path(mpn)}.pdf"
    resolved_dir = output_dir.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate != resolved_dir and resolved_dir not in resolved_candidate.parents:
        raise VibeRequestError("Resolved datasheet path escaped the destination directory")
    return candidate


def _validate_relative_path(path: str) -> None:
    """Accept only a safe, relative same-origin URL path."""
    parsed = urlsplit(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or "\\" in path
        or any(ord(char) < 32 for char in path)
        or any(segment in (".", "..") for segment in parsed.path.split("/"))
    ):
        raise VibeRequestError("Rejected unsafe Vibe request path")


def validate_datasheet_path(key: object, pdf_path: object) -> str:
    """Validate the API's datasheet key/path pair and return the safe path.

    Datasheet links are accepted only as the exact relative path
    ``/pdf/{64-hex-key}``; arbitrary hosts, URL forms, and mismatched keys are
    rejected without being requested.
    """
    if not isinstance(key, str) or not _DATASHEET_KEY_RE.fullmatch(key):
        raise VibeRequestError("Invalid datasheet key")
    expected_path = f"/pdf/{key}"
    if not isinstance(pdf_path, str) or pdf_path != expected_path:
        raise VibeRequestError("Invalid datasheet PDF path")
    _validate_relative_path(pdf_path)
    return pdf_path


def _component_path(mpn: str) -> str:
    """Build the component endpoint path with the MPN encoded as one segment."""
    value = mpn.strip()
    if not value or value in (".", ".."):
        raise VibeRequestError("MPN must be a non-empty path segment")
    path = f"/api/component/{quote(value, safe='')}"
    _validate_relative_path(path)
    return path


def _document_path(mpn: str, doc_id: str) -> str:
    """Build the component-scoped document route using encoded path segments."""
    mpn_value = mpn.strip()
    if not mpn_value or mpn_value in (".", ".."):
        raise VibeRequestError("MPN must be a non-empty path segment")
    if not doc_id or doc_id in (".", ".."):
        raise VibeRequestError("Invalid document id")
    path = (
        f"/api/component/{quote(mpn_value, safe='')}/docs/"
        f"{quote(doc_id, safe='')}?dl=1"
    )
    _validate_relative_path(path)
    return path


def _request(path: str, *, api_key: str, base_url: str, timeout_s: float) -> bytes:
    """Issue a same-origin GET; errors never disclose the URL or bearer key."""
    _validate_base_url(base_url)
    _validate_relative_path(path)
    request = Request(
        base_url.rstrip("/") + path,
        headers=_headers(api_key),
        method="GET",
    )
    try:
        with urlopen(request, timeout=timeout_s) as response:
            return response.read()
    except HTTPError as exc:
        raise VibeRequestError(
            f"Vibe HTTP {exc.code}", status_code=exc.code
        ) from None
    except (URLError, TimeoutError, OSError) as exc:
        raise VibeRequestError(
            f"Vibe request failed ({exc.__class__.__name__})"
        ) from None


def _mpn_filename_prefix(mpn: str) -> str:
    """Make an MPN prefix that stays short and avoids sanitized-name clashes."""
    safe = sanitize_mpn_for_path(mpn)[:36]
    digest = hashlib.sha256(_normalize_mpn(mpn).encode("utf-8")).hexdigest()[:10]
    return f"{safe}-{digest}"


def _safe_id_piece(identity: str) -> str:
    """Keep an identifier readable while ensuring sanitized collisions differ."""
    safe = _UNSAFE_CHARS_RE.sub("_", identity.strip()).strip(". ") or "asset"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{safe[:36]}-{digest}"


def resolve_asset_path(
    mpn: str,
    asset_type: str,
    identity: str,
    output_dir: Path,
) -> Path:
    """Resolve a deterministic, identity-based asset filename under output_dir."""
    if asset_type == "datasheet":
        identity_piece = identity.lower()
    elif asset_type == "doc":
        identity_piece = _safe_id_piece(identity)
    else:
        raise VibeRequestError("Unsupported PDF asset type")
    candidate = output_dir / f"{_mpn_filename_prefix(mpn)}_{asset_type}_{identity_piece}.pdf"
    resolved_dir = output_dir.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate != resolved_dir and resolved_dir not in resolved_candidate.parents:
        raise VibeRequestError("Resolved asset path escaped the destination directory")
    return candidate


def load_roster_mpns(roster_path: Path) -> List[str]:
    """Load and validate only ``candidates[*].mpn`` from a review roster.

    Invalid input fails closed. Returned MPNs preserve first-seen order and
    spelling, while duplicates are removed case/whitespace-insensitively.
    """
    try:
        raw = roster_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RosterError(f"Cannot read roster file: {roster_path}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RosterError(f"Roster file is not valid JSON: {roster_path}") from exc
    if not isinstance(data, dict):
        raise RosterError(
            "Roster JSON must be an object with a \"candidates\" list "
            f"(got {type(data).__name__})"
        )
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        raise RosterError('Roster JSON must have a "candidates" list')

    seen: Dict[str, None] = {}
    mpns: List[str] = []
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise RosterError(f"candidates[{index}] is not a JSON object")
        mpn = candidate.get("mpn")
        if not isinstance(mpn, str) or not mpn.strip():
            raise RosterError(f'candidates[{index}]["mpn"] must be a non-empty string')
        normalized = _normalize_mpn(mpn)
        if normalized not in seen:
            seen[normalized] = None
            mpns.append(mpn)
    return mpns


def _asset_error(
    asset_type: str,
    asset_id: str,
    message: str,
    *,
    status: str = "error",
) -> Dict[str, object]:
    return {"type": asset_type, "id": asset_id, "status": status, "error": message}


def _download_asset(
    *,
    asset_type: str,
    asset_id: str,
    request_path: str,
    output_path: Path,
    output_dir: Path,
    api_key: str,
    base_url: str,
    timeout_s: float,
    force: bool,
    cache_record: Optional[Dict[str, object]],
) -> Dict[str, object]:
    """Download/cache one current inventory identity, atomically writing valid PDFs."""
    result: Dict[str, object] = {
        "type": asset_type,
        "id": asset_id,
        "output": str(output_path),
    }
    try:
        resolved_dir = output_dir.resolve()
        resolved_path = output_path.resolve()
        if resolved_path != resolved_dir and resolved_dir not in resolved_path.parents:
            raise VibeRequestError("Resolved asset path escaped the destination directory")

        cached = None if force else _verified_manifest_asset(
            cache_record,
            asset_type=asset_type,
            asset_id=asset_id,
            expected_path=output_path,
            output_dir=output_dir,
        )
        if cached is not None:
            size, digest = cached
            result.update(status="cached", size_bytes=size, sha256=digest)
            return result

        body = _request(
            request_path,
            api_key=api_key,
            base_url=base_url,
            timeout_s=timeout_s,
        )
    except VibeRequestError as exc:
        status = "not_found" if exc.status_code == 404 else "error"
        result.update(status=status, error=str(exc))
        return result
    except OSError as exc:
        result.update(status="error", error=f"Local file operation failed ({exc.__class__.__name__})")
        return result

    if not body.startswith(b"%PDF"):
        result.update(status="non_pdf", error="Vibe response did not contain a PDF")
        return result

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        # Recheck after creation in case the destination path was replaced.
        resolved_dir = output_dir.resolve()
        resolved_path = output_path.resolve()
        if resolved_path != resolved_dir and resolved_dir not in resolved_path.parents:
            raise VibeRequestError("Resolved asset path escaped the destination directory")
        temporary_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=output_dir, prefix=".vibe-pdf-", suffix=".tmp", delete=False
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(body)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(str(temporary_path), str(output_path))
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass
    except (OSError, VibeRequestError) as exc:
        if isinstance(exc, VibeRequestError):
            message = str(exc)
        else:
            message = f"Local file operation failed ({exc.__class__.__name__})"
        result.update(status="error", error=message)
        return result

    result.update(
        status="downloaded",
        size_bytes=len(body),
        sha256=hashlib.sha256(body).hexdigest(),
    )
    return result


def _process_datasheet(
    mpn: str,
    index: int,
    entry: object,
    *,
    output_dir: Path,
    api_key: str,
    base_url: str,
    timeout_s: float,
    force: bool,
    cache_records: Dict[str, Dict[str, object]],
) -> Dict[str, object]:
    """Validate and process one API ``datasheets[]`` entry."""
    fallback_id = f"index-{index}"
    if not isinstance(entry, dict):
        return _asset_error("datasheet", fallback_id, "Invalid datasheet entry")
    key = entry.get("key")
    asset_id = key if isinstance(key, str) else fallback_id
    try:
        path = validate_datasheet_path(key, entry.get("pdf"))
        output_path = resolve_asset_path(mpn, "datasheet", key, output_dir)
    except VibeRequestError as exc:
        return _asset_error("datasheet", asset_id, str(exc))
    return _download_asset(
        asset_type="datasheet",
        asset_id=asset_id,
        request_path=path,
        output_path=output_path,
        output_dir=output_dir,
        api_key=api_key,
        base_url=base_url,
        timeout_s=timeout_s,
        force=force,
        cache_record=cache_records.get(_manifest_asset_key("datasheet", key)),
    )


def _process_document(
    mpn: str,
    entry: object,
    index: int,
    *,
    output_dir: Path,
    api_key: str,
    base_url: str,
    timeout_s: float,
    force: bool,
    cache_records: Dict[str, Dict[str, object]],
) -> Tuple[Optional[Dict[str, object]], bool]:
    """Process a PDF document; return (result, was_skipped_non_pdf)."""
    fallback_id = f"index-{index}"
    if not isinstance(entry, dict):
        return _asset_error("doc", fallback_id, "Invalid document entry"), False
    filename = entry.get("filename")
    if not isinstance(filename, str):
        return _asset_error("doc", fallback_id, "Invalid document filename"), False
    if not filename.lower().endswith(".pdf"):
        return None, True

    doc_id = entry.get("doc_id")
    asset_id = doc_id if isinstance(doc_id, str) else fallback_id
    try:
        if not isinstance(doc_id, str) or not doc_id.strip():
            raise VibeRequestError("Invalid document id")
        request_path = _document_path(mpn, doc_id)
        output_path = resolve_asset_path(mpn, "doc", doc_id, output_dir)
    except VibeRequestError as exc:
        return _asset_error("doc", asset_id, str(exc)), False
    return _download_asset(
        asset_type="doc",
        asset_id=asset_id,
        request_path=request_path,
        output_path=output_path,
        output_dir=output_dir,
        api_key=api_key,
        base_url=base_url,
        timeout_s=timeout_s,
        force=force,
        cache_record=cache_records.get(_manifest_asset_key("doc", doc_id)),
    ), False


def _asset_counts(assets: List[Dict[str, object]]) -> Dict[str, int]:
    counts = {status: 0 for status in PDF_STATUSES}
    for asset in assets:
        status = asset.get("status")
        if status in counts:
            counts[status] += 1
    counts["total"] = len(assets)
    return counts


def fetch_component(
    mpn: str,
    output_dir: Path,
    *,
    api_key: str,
    base_url: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    download_timeout_s: float = DOWNLOAD_TIMEOUT_S,
    force: bool = False,
) -> Dict[str, object]:
    """Enumerate and process all datasheet and PDF-document assets for an MPN."""
    result: Dict[str, object] = {"mpn": mpn, "assets": [], "skipped_non_pdf_docs": 0}
    saved_manifest, manifest_corrupt = _load_manifest(output_dir)
    old_entry: Optional[Dict[str, object]] = None
    if saved_manifest is not None:
        candidate = saved_manifest["mpns"].get(_normalize_mpn(mpn))
        if isinstance(candidate, dict):
            old_entry = candidate
    cache_records: Dict[str, Dict[str, object]] = {}
    if old_entry and isinstance(old_entry.get("assets"), dict):
        cache_records = old_entry["assets"]

    def inventory_failure(status: str, message: str) -> Dict[str, object]:
        result.update(status=status, error=message, counts=_asset_counts([]))
        failed_record = _failed_manifest_entry(mpn, old_entry)
        manifest_error = _record_manifest_entry(output_dir, mpn, failed_record)
        if manifest_error:
            result["manifest_error"] = manifest_error
        return result

    try:
        component_path = _component_path(mpn)
        body = _request(
            component_path,
            api_key=api_key,
            base_url=base_url,
            timeout_s=timeout_s,
        )
    except VibeRequestError as exc:
        if exc.status_code == 404:
            return inventory_failure("not_found", "Component not found")
        return inventory_failure("error", str(exc))

    try:
        component = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return inventory_failure("error", "Invalid component JSON response")

    if not isinstance(component, dict) or not isinstance(component.get("ok"), bool):
        return inventory_failure("error", "Invalid component response")
    if not component["ok"]:
        return inventory_failure("not_found", "Component not found")

    returned_mpn = component.get("mpn")
    if not isinstance(returned_mpn, str) or _normalize_mpn(returned_mpn) != _normalize_mpn(mpn):
        return inventory_failure("error", "Component response MPN did not match request")
    datasheets = component.get("datasheets")
    documents = component.get("docs")
    if not isinstance(datasheets, list) or not isinstance(documents, list):
        return inventory_failure("error", "Component response is missing asset lists")

    # A corrupt manifest is never used as cache data and will not be overwritten.
    if manifest_corrupt:
        cache_records = {}

    assets: List[Dict[str, object]] = []
    raw_assets: List[Tuple[str, str, object]] = []
    for index, entry in enumerate(datasheets):
        asset = _process_datasheet(
            mpn,
            index,
            entry,
            output_dir=output_dir,
            api_key=api_key,
            base_url=base_url,
            timeout_s=download_timeout_s,
            force=force,
            cache_records=cache_records,
        )
        assets.append(asset)
        if isinstance(entry, dict) and isinstance(entry.get("key"), str):
            raw_assets.append(("datasheet", entry["key"], entry))
        else:
            raw_assets.append(("datasheet", str(asset.get("id", f"index-{index}")), entry))

    skipped_non_pdf_docs = 0
    for index, entry in enumerate(documents):
        asset, skipped = _process_document(
            mpn,
            entry,
            index,
            output_dir=output_dir,
            api_key=api_key,
            base_url=base_url,
            timeout_s=download_timeout_s,
            force=force,
            cache_records=cache_records,
        )
        if asset is not None:
            assets.append(asset)
            doc_id = entry.get("doc_id") if isinstance(entry, dict) else None
            raw_assets.append(("doc", str(doc_id if isinstance(doc_id, str) else asset.get("id")), entry))
        if skipped:
            skipped_non_pdf_docs += 1

    counts = _asset_counts(assets)
    status = (
        "partial"
        if counts["not_found"] or counts["error"] or counts["non_pdf"]
        else "complete"
    )
    result.update(
        status=status,
        assets=assets,
        counts=counts,
        skipped_non_pdf_docs=skipped_non_pdf_docs,
    )
    manifest_entry = _manifest_entry_from_result(
        mpn, returned_mpn, assets, raw_assets, output_dir, api_key, status
    )
    manifest_error = _record_manifest_entry(output_dir, mpn, manifest_entry)
    if manifest_error:
        result["manifest_error"] = manifest_error
    return result


def _manifest_entry_is_current(entry: object, output_dir: Path) -> bool:
    if not isinstance(entry, dict):
        return False
    records = entry.get("assets")
    if not isinstance(records, dict):
        return False
    for record in records.values():
        if not isinstance(record, dict):
            return False
        asset_type = record.get("type")
        asset_id = record.get("id")
        path = _manifest_pdf_path(output_dir, record.get("filepath"))
        if (
            asset_type not in DISPLAY_METADATA_FIELDS
            or not isinstance(asset_id, str)
            or path is None
            or _verified_manifest_asset(
                record,
                asset_type=asset_type,
                asset_id=asset_id,
                expected_path=path,
                output_dir=output_dir,
            ) is None
        ):
            return False
    return True


def preview_component(mpn: str, output_dir: Path, *, force: bool = False) -> Dict[str, object]:
    """Preview without network/writes; asset inventory is necessarily unknown."""
    result: Dict[str, object] = {
        "mpn": mpn,
        "status": "would_fetch",
        "asset_inventory": "unknown",
        "asset_count": None,
        "asset_counts": None,
    }
    try:
        legacy_path = resolve_pdf_path(mpn, output_dir)
    except VibeRequestError as exc:
        result.update(status="error", error=str(exc))
        return result
    # This legacy single-PDF path cannot reveal remote inventory completeness.
    result["legacy_single_pdf_present"] = legacy_path.exists()
    if not force:
        saved_manifest, corrupt = _load_manifest(output_dir)
        if not corrupt and saved_manifest is not None:
            entry = saved_manifest["mpns"].get(_normalize_mpn(mpn))
            if (
                isinstance(entry, dict)
                and entry.get("status") == "complete"
                and entry.get("stale") is False
                and isinstance(entry.get("assets"), dict)
                and _manifest_entry_is_current(entry, output_dir)
            ):
                result.update(
                    asset_inventory="cached",
                    asset_count=len(entry["assets"]),
                    asset_counts={"total": len(entry["assets"])},
                )
    if force:
        result["force"] = True
    return result


def resolve_output_dir(project: Optional[str], output: Optional[str]) -> Path:
    """Resolve the datasheets directory from --project / --output / cwd."""
    if output:
        return Path(output)
    if project:
        project_path = Path(project)
        if project_path.is_file():
            project_path = project_path.parent
        return project_path / "datasheets"
    return Path.cwd() / "datasheets"


def _prompt_api_key() -> str:
    """Prompt once without echo if the environment did not provide a key."""
    try:
        return getpass.getpass("Vibe API key: ").strip()
    except (EOFError, KeyboardInterrupt):
        return ""


def _manifest_counts(results: List[Dict[str, object]]) -> Dict[str, object]:
    mpn_counts = {"complete": 0, "partial": 0, "not_found": 0, "error": 0}
    asset_counts = {status: 0 for status in PDF_STATUSES}
    asset_counts["total"] = 0
    skipped_non_pdf_docs = 0
    for result in results:
        status = result.get("status")
        if status in mpn_counts:
            mpn_counts[status] += 1
        counts = result.get("counts")
        if isinstance(counts, dict):
            for asset_status in PDF_STATUSES:
                asset_counts[asset_status] += int(counts.get(asset_status, 0))
            asset_counts["total"] += int(counts.get("total", 0))
        skipped_non_pdf_docs += int(result.get("skipped_non_pdf_docs", 0))
    return {
        "mpns": len(results),
        "mpn_statuses": mpn_counts,
        "assets": asset_counts,
        "skipped_non_pdf_docs": skipped_non_pdf_docs,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download all datasheet and PDF-document assets from Vibe."
    )
    parser.add_argument(
        "mpn",
        nargs="*",
        help="One or more manufacturer part numbers (mutually exclusive with --roster)",
    )
    parser.add_argument(
        "--roster",
        help="review_coverage.json roster; fetches only candidates[*].mpn",
    )
    location = parser.add_mutually_exclusive_group()
    location.add_argument(
        "--project",
        help="KiCad project path (directory or .kicad_sch file); PDFs land in <project>/datasheets/",
    )
    location.add_argument("--output", "-o", help="Datasheets directory to use directly")
    parser.add_argument("--force", action="store_true", help="Refetch every selected PDF asset")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S)
    parser.add_argument("--json", action="store_true", help="Emit a JSON results manifest to stdout")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview with no network calls, writes, or API-key requirement; asset counts remain unknown",
    )
    args = parser.parse_args(argv)

    if args.roster and args.mpn:
        parser.error("argument mpn: not allowed with argument --roster")
    if not args.roster and not args.mpn:
        parser.error("one of the mpn arguments or --roster is required")
    if args.roster:
        try:
            mpn_list = load_roster_mpns(Path(args.roster))
        except RosterError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 2
    else:
        mpn_list = args.mpn

    output_dir = resolve_output_dir(args.project, args.output)
    if args.dry_run:
        results: List[Dict[str, object]] = []
        for mpn in mpn_list:
            print(f"[Vibe] {mpn} (dry-run)...", file=sys.stderr)
            result = preview_component(mpn, output_dir, force=args.force)
            results.append(result)
            if result["status"] == "error":
                print(f"  error: {result.get('error', 'unknown error')}", file=sys.stderr)
            else:
                legacy_note = "; legacy single PDF present" if result.get("legacy_single_pdf_present") else ""
                if result.get("asset_count") is None:
                    count_note = "asset count unknown"
                else:
                    count_note = f"{result['asset_count']} cached assets from current manifest"
                print(f"  would query component; {count_note}{legacy_note}", file=sys.stderr)
        manifest: Dict[str, object] = {
            "output_dir": str(output_dir),
            "dry_run": True,
            "asset_inventory": "unknown",
            "asset_counts": None,
            "results": results,
            "counts": {
                "mpns": len(results),
                "would_fetch": sum(1 for item in results if item["status"] == "would_fetch"),
                "errors": sum(1 for item in results if item["status"] == "error"),
                "asset_inventory_unknown": sum(
                    1 for item in results if item.get("asset_inventory") == "unknown"
                ),
            },
        }
        if args.json:
            json.dump(manifest, sys.stdout, indent=2)
            sys.stdout.write("\n")
        return 1 if manifest["counts"]["errors"] else 0

    api_key = get_api_key()
    if not api_key:
        api_key = _prompt_api_key()
    base_url = get_base_url()

    results = []
    for mpn in mpn_list:
        print(f"[Vibe] {mpn}...", file=sys.stderr)
        if not api_key:
            result = {
                "mpn": mpn,
                "status": "error",
                "error": "API key was not provided",
                "assets": [],
                "counts": _asset_counts([]),
                "skipped_non_pdf_docs": 0,
            }
        else:
            result = fetch_component(
                mpn,
                output_dir,
                api_key=api_key,
                base_url=base_url,
                timeout_s=args.timeout,
                force=args.force,
            )
        results.append(result)
        for asset in result.get("assets", []):
            status = asset.get("status")
            if status in ("downloaded", "cached"):
                print(f"  {status}: {asset.get('output', '')}", file=sys.stderr)
            else:
                print(f"  {status}: {asset.get('error', '')}", file=sys.stderr)
        if result.get("manifest_error"):
            print(f"  manifest error: {result['manifest_error']}", file=sys.stderr)
        if result["status"] in ("not_found", "error"):
            print(f"  {result['status']}: {result.get('error', '')}", file=sys.stderr)
        elif result["status"] == "partial":
            print("  partial: one or more PDF assets failed validation or download", file=sys.stderr)
        elif not result.get("assets"):
            print("  complete: no PDF assets found", file=sys.stderr)

    counts = _manifest_counts(results)
    manifest = {"output_dir": str(output_dir), "results": results, "counts": counts}
    if args.json:
        json.dump(manifest, sys.stdout, indent=2)
        sys.stdout.write("\n")

    mpn_statuses = counts["mpn_statuses"]
    asset_counts = counts["assets"]
    if (
        mpn_statuses["error"]
        or mpn_statuses["partial"]
        or any(result.get("manifest_error") for result in results)
        or asset_counts["error"]
        or asset_counts["non_pdf"]
    ):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
