"""Public Binance Vision downloader with checksums, retries, and resume."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from research_lab.historical.checksums import parse_checksum_text, verify_archive
from research_lab.historical.constants import (
    BASE_BACKOFF_SECONDS,
    HTTP_TIMEOUT_SECONDS,
    MAX_BACKOFF_SECONDS,
    MAX_RETRIES,
    MAX_WORKERS,
    RAW_AGGTRADES,
    RAW_KLINES,
    VISION_BASE,
)
from research_lab.historical.planning import ArchiveSpec
from research_lab.isolation import assert_isolated_path

logger = logging.getLogger(__name__)


@dataclass
class DownloadResult:
    spec: ArchiveSpec
    status: str
    archive_path: str | None = None
    checksum_path: str | None = None
    sha256: str | None = None
    content_length: int | None = None
    error: str | None = None
    attempts: int = 0


@dataclass
class DownloadPlan:
    archives: list[ArchiveSpec]
    estimated_bytes: int
    known_sizes: int
    unknown_sizes: int
    free_bytes: int
    reserve_bytes: int
    safe: bool
    reasons: list[str] = field(default_factory=list)


def vision_url(relative_path: str) -> str:
    return f"{VISION_BASE}/{relative_path.lstrip('/')}"


def raw_dir_for(kind: str) -> Path:
    return RAW_KLINES if kind == "klines_1m" else RAW_AGGTRADES


def head_content_length(
    url: str,
    *,
    opener: Callable[..., object] | None = None,
) -> int | None:
    request = urllib.request.Request(url, method="HEAD")
    open_fn = opener or urllib.request.urlopen
    try:
        with open_fn(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # type: ignore[arg-type]
            length = response.headers.get("Content-Length")
            if length is None:
                return None
            return int(length)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError, OSError):
        return None


def build_download_plan(
    archives: list[ArchiveSpec],
    *,
    free_bytes: int,
    reserve_bytes: int,
    size_lookup: Callable[[ArchiveSpec], int | None] | None = None,
) -> DownloadPlan:
    estimated = 0
    known = 0
    unknown = 0
    lookup = size_lookup or (lambda spec: head_content_length(vision_url(spec.relative_path)))
    for spec in archives:
        destination = raw_dir_for(spec.kind) / spec.filename
        if destination.exists():
            continue
        size = lookup(spec)
        if size is None:
            unknown += 1
        else:
            known += 1
            estimated += size
            # CHECKSUM files are tiny; ignore for planning.
    reasons: list[str] = []
    required = estimated + reserve_bytes
    safe = True
    if unknown > 0:
        reasons.append(
            f"{unknown} archive size(s) unknown via HEAD; estimate is a lower bound."
        )
    if free_bytes < required:
        safe = False
        reasons.append(
            f"Free space {free_bytes} bytes is below estimated download "
            f"{estimated} plus reserve {reserve_bytes}."
        )
    if free_bytes < reserve_bytes:
        safe = False
        reasons.append("Free space is already below the configured reserve.")
    return DownloadPlan(
        archives=list(archives),
        estimated_bytes=estimated,
        known_sizes=known,
        unknown_sizes=unknown,
        free_bytes=free_bytes,
        reserve_bytes=reserve_bytes,
        safe=safe,
        reasons=reasons,
    )


def _partial_path(destination: Path) -> Path:
    """Use destination.name + '.part' so multi-suffix names stay stable."""
    return destination.with_name(destination.name + ".part")


def _download_bytes(
    url: str,
    destination: Path,
    *,
    opener: Callable[..., object] | None = None,
) -> int:
    destination = assert_isolated_path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = _partial_path(destination)
    open_fn = opener or urllib.request.urlopen
    existing = partial.stat().st_size if partial.exists() else 0
    headers = {}
    if existing > 0:
        headers["Range"] = f"bytes={existing}-"
    request = urllib.request.Request(url, headers=headers)
    mode = "ab" if existing > 0 else "wb"
    written = existing
    try:
        with open_fn(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # type: ignore[arg-type]
            # If server ignores Range and returns 200, restart.
            status = getattr(response, "status", None) or response.getcode()
            if existing > 0 and status == 200:
                written = 0
                mode = "wb"
            with partial.open(mode) as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    handle.write(chunk)
                    written += len(chunk)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise FileNotFoundError(f"Archive not found: {url}") from error
        raise
    return written


def download_one(
    spec: ArchiveSpec,
    *,
    force: bool = False,
    opener: Callable[..., object] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> DownloadResult:
    directory = assert_isolated_path(raw_dir_for(spec.kind))
    directory.mkdir(parents=True, exist_ok=True)
    archive_path = directory / spec.filename
    checksum_path = directory / spec.checksum_filename
    if archive_path.exists() and checksum_path.exists() and not force:
        try:
            expected = parse_checksum_text(
                checksum_path.read_text(encoding="utf-8"),
                expected_filename=spec.filename,
            )
            digest = verify_archive(archive_path, expected)
            return DownloadResult(
                spec=spec,
                status="already_verified",
                archive_path=str(archive_path),
                checksum_path=str(checksum_path),
                sha256=digest,
                content_length=archive_path.stat().st_size,
                attempts=0,
            )
        except ValueError as error:
            logger.warning("Re-downloading %s after verify failure: %s", spec.filename, error)

    attempts = 0
    last_error: str | None = None
    backoff = BASE_BACKOFF_SECONDS
    while attempts < MAX_RETRIES:
        attempts += 1
        try:
            checksum_url = vision_url(spec.relative_path + ".CHECKSUM")
            _download_bytes(checksum_url, checksum_path, opener=opener)
            checksum_partial = _partial_path(checksum_path)
            if not checksum_partial.exists():
                raise FileNotFoundError(f"Missing checksum download for {spec.checksum_filename}.")
            checksum_partial.replace(checksum_path)
            expected = parse_checksum_text(
                checksum_path.read_text(encoding="utf-8"),
                expected_filename=spec.filename,
            )
            size = _download_bytes(vision_url(spec.relative_path), archive_path, opener=opener)
            partial = _partial_path(archive_path)
            if not partial.exists():
                raise FileNotFoundError(f"Missing partial download for {spec.filename}.")
            digest = verify_archive(partial, expected)
            partial.replace(archive_path)
            return DownloadResult(
                spec=spec,
                status="downloaded",
                archive_path=str(archive_path),
                checksum_path=str(checksum_path),
                sha256=digest,
                content_length=size,
                attempts=attempts,
            )
        except FileNotFoundError as error:
            return DownloadResult(
                spec=spec,
                status="unavailable",
                error=str(error),
                attempts=attempts,
            )
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as error:
            last_error = f"{type(error).__name__}: {error}"
            logger.warning(
                "Download attempt %s/%s failed for %s: %s",
                attempts,
                MAX_RETRIES,
                spec.filename,
                last_error,
            )
            if attempts >= MAX_RETRIES:
                break
            sleep(backoff)
            backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
    return DownloadResult(
        spec=spec,
        status="failed",
        error=last_error or "unknown error",
        attempts=attempts,
    )


def download_many(
    archives: list[ArchiveSpec],
    *,
    workers: int = MAX_WORKERS,
    force: bool = False,
    opener: Callable[..., object] | None = None,
) -> list[DownloadResult]:
    results: list[DownloadResult] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {
            pool.submit(download_one, spec, force=force, opener=opener): spec
            for spec in archives
        }
        for future in as_completed(futures):
            results.append(future.result())
    results.sort(key=lambda item: (item.spec.kind, item.spec.period))
    return results


def write_download_log(path: Path, results: list[DownloadResult]) -> Path:
    path = assert_isolated_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "written_at_utc": datetime.now(timezone.utc).isoformat(),
        "results": [
            {
                **asdict(result),
                "spec": asdict(result.spec),
            }
            for result in results
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path
