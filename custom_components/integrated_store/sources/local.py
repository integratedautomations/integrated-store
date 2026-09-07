"""Local filesystem source.

For packages you keep on the same machine, e.g. `/config/private_packages/thing`.
The path must be inside the Home Assistant config directory or one of the
configured allowlisted external directories — otherwise this would be an
arbitrary-file-read primitive exposed over the panel's HTTP API.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from ..const import SOURCE_LOCAL
from ..exceptions import NotFoundError, ValidationError
from ..utils import is_within
from . import PackageMetadata, Source, _make_temp_dir

_LOGGER = logging.getLogger(__package__)

VERSION_FILES = ("manifest.json", "version.json", "package.json")
README_FILES = ("README.md", "Readme.md", "README.MD", "readme.md")


class LocalSource(Source):
    """A package read from a directory on the Home Assistant host."""

    type = SOURCE_LOCAL

    def __init__(self, hass: Any, config: dict[str, Any], options: Any = None) -> None:
        """Validate that the configured path is one we are allowed to read."""
        super().__init__(hass, config, options)

        raw_path = self.config.get("path")
        if not raw_path:
            raise ValidationError("local sources need a 'path'.")

        self.path = Path(str(raw_path))
        if not self.path.is_absolute():
            self.path = Path(self.hass.config.path(str(raw_path)))

        allowed_roots = [Path(self.hass.config.config_dir)]
        allowed_roots.extend(
            Path(item) for item in self.hass.config.allowlist_external_dirs
        )
        if not any(is_within(root, self.path) for root in allowed_roots):
            raise ValidationError(
                f"Local path '{self.path}' is outside the Home Assistant config "
                "directory and is not in allowlist_external_dirs."
            )

        self._version: str | None = None

    def _read_version(self) -> str:
        """Read a version from the directory. Blocking.

        Prefers a declared version in a known metadata file; otherwise derives a
        content signature from file sizes and mtimes, so edits to a local
        package still register as a new version.
        """
        if not self.path.is_dir():
            raise NotFoundError(f"Local path '{self.path}' does not exist.")

        for candidate in VERSION_FILES:
            meta_file = self.path / candidate
            if not meta_file.is_file():
                continue
            try:
                data = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _LOGGER.debug("Could not parse %s", meta_file, exc_info=True)
                continue
            if isinstance(data, dict) and (version := data.get("version")):
                return str(version)

        # No declared version: fingerprint the tree.
        signature = 0
        count = 0
        for item in sorted(self.path.rglob("*")):
            if not item.is_file():
                continue
            try:
                stat = item.stat()
            except OSError:
                continue
            signature ^= hash((item.name, stat.st_size, int(stat.st_mtime)))
            count += 1
        if count == 0:
            raise ValidationError(f"Local path '{self.path}' contains no files.")
        return f"local-{signature & 0xFFFFFFFF:08x}"

    def _read_metadata(self) -> dict[str, Any]:
        """Read name/description from a manifest if there is one. Blocking."""
        manifest = self.path / "manifest.json"
        if manifest.is_file():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return {}
            if isinstance(data, dict):
                return data
        return {}

    async def get_metadata(self) -> PackageMetadata:
        """Return metadata derived from the directory contents."""
        manifest = await self.hass.async_add_executor_job(self._read_metadata)
        return PackageMetadata(
            name=manifest.get("name") or self.path.name,
            description=manifest.get("description", ""),
            latest_version=await self.get_latest_version(),
            homepage=manifest.get("documentation"),
            extra={"path": str(self.path)},
        )

    async def get_latest_version(self) -> str | None:
        """Return the declared or fingerprinted version."""
        return await self.hass.async_add_executor_job(self._read_version)

    def _read_readme(self) -> str | None:
        """Read README.md from the directory, if present. Blocking."""
        for name in README_FILES:
            candidate = self.path / name
            if candidate.is_file():
                try:
                    return candidate.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    return None
        return None

    async def get_readme(self) -> str | None:
        """Return the local README, if the directory has one."""
        return await self.hass.async_add_executor_job(self._read_readme)

    def _copy_to_temp(self) -> Path:
        """Copy the source tree into a temp dir. Blocking.

        Copying rather than installing in place keeps the rest of the pipeline
        uniform, and means a failed validation never leaves the user's own
        source directory modified.
        """
        temp_dir = _make_temp_dir()
        destination = temp_dir / "extracted"
        shutil.copytree(
            self.path,
            destination,
            symlinks=False,
            ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
        )
        return destination

    async def download(self, version: str) -> Path:
        """Copy the directory into a temp dir and return it."""
        _LOGGER.debug("Copying local package from %s", self.path)
        return await self.hass.async_add_executor_job(self._copy_to_temp)
