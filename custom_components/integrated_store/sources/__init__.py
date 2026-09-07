"""Package sources.

A Source knows how to talk to one kind of backend (GitHub, a self-hosted Git
server, a plain zip URL, a local directory) and can answer three questions:
what is this package, what is the newest version, and give me the files for a
version.

Everything a source returns is untrusted input; validation happens here (size
caps, archive member sanitisation) and again in the installer.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from ..const import (
    MAX_DOWNLOAD_BYTES,
    MAX_UNCOMPRESSED_BYTES,
    SOURCE_GIT_HTTP,
    SOURCE_GITHUB,
    SOURCE_HTTP_ZIP,
    SOURCE_LOCAL,
)
from ..exceptions import SourceError, ValidationError
from ..utils import safe_archive_member

_LOGGER = logging.getLogger(__package__)

DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=300)
API_TIMEOUT = aiohttp.ClientTimeout(total=60)


@dataclass
class PackageMetadata:
    """What a source can tell us about a package without downloading it."""

    name: str
    description: str = ""
    latest_version: str | None = None
    homepage: str | None = None
    # Free-form extras a source wants to surface (stars, default branch, ...).
    extra: dict[str, Any] | None = None


class Source(ABC):
    """Abstract base for all package sources."""

    #: The `type` value in a catalog entry's `source` block.
    type: str = ""

    def __init__(
        self,
        hass: HomeAssistant,
        config: dict[str, Any],
        options: dict[str, Any] | None = None,
    ) -> None:
        """Initialise with the catalog `source` block and integration options.

        `options` carries credentials from the config entry. They are read here
        and never written back into `config`, which may be persisted or shown.
        """
        self.hass = hass
        self.config = config
        self.options = options or {}

    @property
    def session(self) -> aiohttp.ClientSession:
        """The shared HA aiohttp session."""
        return async_get_clientsession(self.hass)

    @abstractmethod
    async def get_metadata(self) -> PackageMetadata:
        """Return descriptive metadata for the package."""

    @abstractmethod
    async def get_latest_version(self) -> str | None:
        """Return the newest available version, or None if unknown."""

    @abstractmethod
    async def download(self, version: str) -> Path:
        """Download `version` into a fresh temp directory and return it.

        The caller owns the returned directory and must remove it (see
        `async_cleanup`).
        """

    async def get_readme(self) -> str | None:
        """Return the package's README as markdown, or None if unavailable.

        Not abstract: it is decorative, so a source that cannot supply one
        (http_zip without a sidecar readme_url) just returns None rather than
        forcing every implementation to opt out explicitly.
        """
        return None

    # --- shared helpers ------------------------------------------------------

    async def _api_text(
        self, url: str, headers: dict[str, str] | None = None
    ) -> str | None:
        """GET a plain-text endpoint, best-effort.

        Used for README fetches: a missing or unreachable README should never
        turn into an error dialog, so failures of every kind become None here
        rather than raising.
        """
        try:
            async with self.session.get(
                url, headers=headers, timeout=API_TIMEOUT
            ) as response:
                if response.status >= 400:
                    return None
                return await response.text()
        except (aiohttp.ClientError, TimeoutError, UnicodeError):
            return None

    async def _api_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        allow_404: bool = False,
    ) -> Any | None:
        """GET a JSON endpoint with uniform error handling.

        Returns None on 404 when `allow_404` is set, which lets callers treat
        "no releases yet" as a normal outcome rather than a failure.
        """
        try:
            async with self.session.get(
                url, headers=headers, timeout=API_TIMEOUT
            ) as response:
                if response.status == 404 and allow_404:
                    return None
                if response.status in (401, 403):
                    raise SourceError(
                        f"Access denied by {_host_of(url)} (HTTP {response.status}). "
                        "Check that the configured token is valid and has access."
                    )
                if response.status >= 400:
                    raise SourceError(
                        f"{_host_of(url)} returned HTTP {response.status} for this package."
                    )
                return await response.json(content_type=None)
        except aiohttp.ClientError as err:
            raise SourceError(f"Could not reach {_host_of(url)}: {err}") from err
        except TimeoutError as err:
            raise SourceError(f"Timed out contacting {_host_of(url)}.") from err

    async def _download_and_extract(
        self, url: str, headers: dict[str, str] | None = None
    ) -> Path:
        """Stream a zip from `url`, extract it safely, return the extract dir."""
        target = await self.hass.async_add_executor_job(_make_temp_dir)
        archive = target / "download.zip"

        try:
            await self._stream_to_file(url, archive, headers)
            extracted = target / "extracted"
            await self.hass.async_add_executor_job(_extract_zip, archive, extracted)
            await self.hass.async_add_executor_job(archive.unlink)
        except BaseException:
            await async_cleanup(self.hass, target)
            raise

        return extracted

    async def _stream_to_file(
        self, url: str, destination: Path, headers: dict[str, str] | None = None
    ) -> None:
        """Stream a URL to disk, aborting if it exceeds the size cap."""
        try:
            async with self.session.get(
                url, headers=headers, timeout=DOWNLOAD_TIMEOUT
            ) as response:
                if response.status in (401, 403):
                    raise SourceError(
                        f"Access denied downloading from {_host_of(url)} "
                        f"(HTTP {response.status})."
                    )
                if response.status >= 400:
                    raise SourceError(
                        f"Download from {_host_of(url)} failed with HTTP "
                        f"{response.status}."
                    )

                declared = response.content_length
                if declared is not None and declared > MAX_DOWNLOAD_BYTES:
                    raise ValidationError(
                        f"Download is {declared} bytes, which exceeds the "
                        f"{MAX_DOWNLOAD_BYTES} byte limit."
                    )

                # Buffer in memory in bounded chunks, write on the executor:
                # open()/write() are blocking calls.
                total = 0
                chunks: list[bytes] = []
                async for chunk in response.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValidationError(
                            f"Download exceeded the {MAX_DOWNLOAD_BYTES} byte limit."
                        )
                    chunks.append(chunk)
                    if len(chunks) >= 64:
                        await self.hass.async_add_executor_job(
                            _append_bytes, destination, b"".join(chunks)
                        )
                        chunks = []
                if chunks:
                    await self.hass.async_add_executor_job(
                        _append_bytes, destination, b"".join(chunks)
                    )
                if total == 0:
                    raise SourceError(f"{_host_of(url)} returned an empty download.")
        except aiohttp.ClientError as err:
            raise SourceError(f"Download from {_host_of(url)} failed: {err}") from err
        except TimeoutError as err:
            raise SourceError(f"Download from {_host_of(url)} timed out.") from err


# --- module-level helpers (executor-side, blocking) --------------------------


def _make_temp_dir() -> Path:
    """Create a private temp directory. Blocking."""
    return Path(tempfile.mkdtemp(prefix="integrated_store_"))


def _append_bytes(destination: Path, data: bytes) -> None:
    """Append bytes to a file, creating it if needed. Blocking."""
    with open(destination, "ab") as handle:
        handle.write(data)


def _extract_zip(archive: Path, destination: Path) -> None:
    """Extract a zip with zip-slip protection. Blocking.

    Every member is validated before anything is written, so a hostile archive
    aborts the install instead of leaving half its payload on disk. We build the
    output paths ourselves rather than using ZipFile.extractall, which is what
    makes the traversal checks authoritative.
    """
    destination.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(archive) as zip_file:
            members = zip_file.infolist()

            uncompressed = sum(info.file_size for info in members)
            if uncompressed > MAX_UNCOMPRESSED_BYTES:
                raise ValidationError(
                    f"Archive expands to {uncompressed} bytes, which exceeds the "
                    f"{MAX_UNCOMPRESSED_BYTES} byte limit."
                )

            plan: list[tuple[zipfile.ZipInfo, Path]] = []
            for info in members:
                # Reject symlinks outright: the upper 16 bits of
                # external_attr hold the Unix mode, 0xA000 is S_IFLNK.
                if (info.external_attr >> 16) & 0xF000 == 0xA000:
                    raise ValidationError(
                        f"Archive member '{info.filename}' is a symlink, which is "
                        "not allowed."
                    )

                relative = safe_archive_member(info.filename)
                if relative is None:
                    continue

                out_path = destination / Path(*relative.parts)
                resolved_root = destination.resolve()
                # Resolve the parent (the file itself does not exist yet).
                out_parent = out_path.parent
                out_parent.mkdir(parents=True, exist_ok=True)
                if resolved_root not in out_parent.resolve().parents and (
                    out_parent.resolve() != resolved_root
                ):
                    raise ValidationError(
                        f"Archive member '{info.filename}' escapes the extract "
                        "directory."
                    )
                plan.append((info, out_path))

            if not plan:
                raise ValidationError("Archive contained no usable files.")

            for info, out_path in plan:
                with zip_file.open(info) as source, open(out_path, "wb") as target:
                    shutil.copyfileobj(source, target)
    except zipfile.BadZipFile as err:
        raise ValidationError(
            "The downloaded file is not a valid zip archive."
        ) from err


async def async_cleanup(hass: HomeAssistant, path: Path | None) -> None:
    """Remove a temp directory, never raising. Safe to call with None."""
    if path is None:
        return
    try:
        await hass.async_add_executor_job(
            lambda: shutil.rmtree(path, ignore_errors=True)
        )
    except OSError:  # pragma: no cover - ignore_errors makes this unlikely
        _LOGGER.debug("Could not clean up temp dir %s", path, exc_info=True)


def _host_of(url: str) -> str:
    """Best-effort host name for error messages, never leaking query strings."""
    try:
        from urllib.parse import urlparse

        return urlparse(url).netloc or url
    except ValueError:
        return "the remote server"


def descend_single_root(path: Path) -> Path:
    """Return the real content root of an extracted archive.

    Git forges wrap archives in a single `owner-repo-sha/` directory; step
    through those so callers see the repository contents directly. Blocking
    (it stats the directory), so call it from the executor.
    """
    current = path
    for _ in range(4):  # bounded, so a pathological archive cannot loop us
        try:
            entries = list(current.iterdir())
        except OSError:
            return current
        if len(entries) == 1 and entries[0].is_dir():
            current = entries[0]
            continue
        return current
    return current


# --- factory -----------------------------------------------------------------


def async_get_source(
    hass: HomeAssistant,
    source_config: dict[str, Any],
    options: dict[str, Any] | None = None,
) -> Source:
    """Build the right Source for a catalog `source` block.

    Credentials are injected from `options` (the config entry) here, which is
    the only place they enter a source.
    """
    if not isinstance(source_config, dict):
        raise ValidationError("Package source must be an object.")

    source_type = source_config.get("type")

    # Imported lazily to keep the module graph acyclic and import cost low.
    from .git_http import GitHttpSource
    from .github import GitHubSource
    from .http_zip import HttpZipSource
    from .local import LocalSource

    if source_type == SOURCE_GITHUB:
        return GitHubSource(hass, source_config, options)
    if source_type == SOURCE_GIT_HTTP:
        return GitHttpSource(hass, source_config, options)
    if source_type == SOURCE_HTTP_ZIP:
        return HttpZipSource(hass, source_config, options)
    if source_type == SOURCE_LOCAL:
        return LocalSource(hass, source_config, options)

    raise ValidationError(f"Unknown source type '{source_type}'.")


__all__ = [
    "PackageMetadata",
    "Source",
    "async_cleanup",
    "async_get_source",
    "descend_single_root",
]
