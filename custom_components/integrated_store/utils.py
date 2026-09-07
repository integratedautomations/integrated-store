"""Shared helpers: path safety, static path registration, version comparison."""

from __future__ import annotations

import logging
import os
from pathlib import Path, PurePosixPath, PureWindowsPath

from awesomeversion import AwesomeVersion, AwesomeVersionException
from homeassistant.core import HomeAssistant

from .exceptions import ValidationError

_LOGGER = logging.getLogger(__package__)


async def async_register_static_path(
    hass: HomeAssistant,
    url_path: str,
    path: str,
    cache_headers: bool = True,
) -> None:
    """Register a static path, supporting HA before and after 2024.7.

    The modern API is async and takes StaticPathConfig objects; the legacy one
    is a blocking call that must not run on the event loop directly.
    """
    try:
        from homeassistant.components.http import (
            StaticPathConfig,
        )

        await hass.http.async_register_static_paths(
            [StaticPathConfig(url_path, path, cache_headers)]
        )
    except ImportError:
        # HA < 2024.7 — blocking, so push it to the executor.
        await hass.async_add_executor_job(
            lambda: hass.http.register_static_path(url_path, path, cache_headers)
        )


def is_within(base: Path, target: Path) -> bool:
    """Return True if `target` is `base` itself or lives underneath it.

    Both paths are resolved first so symlinks and `..` segments cannot be used
    to escape. Uses os.path.commonpath rather than string prefixes, which would
    wrongly accept `/config-evil` for a base of `/config`.
    """
    try:
        base_resolved = base.resolve()
        target_resolved = target.resolve()
    except OSError:
        return False

    try:
        return os.path.commonpath([str(base_resolved), str(target_resolved)]) == str(
            base_resolved
        )
    except ValueError:
        # Different drives on Windows.
        return False


def ensure_within(base: Path, target: Path) -> Path:
    """Return `target`, raising ValidationError if it escapes `base`."""
    if not is_within(base, target):
        raise ValidationError(f"Refusing to touch '{target}': it is outside '{base}'.")
    return target


def safe_archive_member(name: str) -> PurePosixPath | None:
    """Validate one archive member name, returning a safe relative path.

    Returns None for members that should simply be skipped (directory entries
    and macOS resource-fork noise). Raises ValidationError for anything that
    looks like a path traversal attempt, so a malicious archive aborts the whole
    install rather than being partially extracted.

    Zip archives always use forward slashes, but a hostile archive can embed
    backslashes to target Windows, so both separators are treated as such.
    """
    if not name or name.endswith("/"):
        return None

    normalised = name.replace("\\", "/")

    if normalised.startswith("/"):
        raise ValidationError(f"Archive member '{name}' is an absolute path.")

    # Drive letters / UNC paths (e.g. "C:/evil", "//host/share").
    if PureWindowsPath(normalised).is_absolute() or PureWindowsPath(name).drive:
        raise ValidationError(f"Archive member '{name}' is an absolute path.")

    parts = PurePosixPath(normalised).parts
    if any(part == ".." for part in parts):
        raise ValidationError(f"Archive member '{name}' contains a '..' path segment.")

    # Skip metadata directories some tools add.
    if parts and parts[0] in ("__MACOSX",):
        return None
    if parts and parts[-1] in (".DS_Store",):
        return None

    return PurePosixPath(*parts)


def version_is_newer(candidate: str | None, current: str | None) -> bool:
    """Return True if `candidate` is a strictly newer version than `current`.

    Falls back to a plain string inequality when either value is not a version
    we can parse (branch names, commit SHAs, "local"), which is the best we can
    do while still noticing that something changed.
    """
    if not candidate:
        return False
    if not current:
        return True
    if candidate == current:
        return False

    try:
        return AwesomeVersion(candidate) > AwesomeVersion(current)
    except (AwesomeVersionException, ValueError, TypeError):
        _LOGGER.debug(
            "Could not compare versions %s and %s, falling back to inequality",
            candidate,
            current,
        )
        return True


def normalise_version(value: str | None) -> str | None:
    """Strip a leading 'v' from tag-style versions for display consistency."""
    if value is None:
        return None
    stripped = value.strip()
    if len(stripped) > 1 and stripped[0] in ("v", "V") and stripped[1].isdigit():
        return stripped[1:]
    return stripped


def slugify(value: str) -> str:
    """Reduce an id to a safe single path segment.

    Shared between the installer (deciding where a package lands on disk) and
    the manager (predicting that same path before anything is downloaded, to
    check for pre-existing files).
    """
    cleaned = "".join(
        char if char.isalnum() or char in ("-", "_") else "-"
        for char in value.strip().lower()
    ).strip("-_")
    if not cleaned:
        raise ValidationError(f"Package id '{value}' has no usable characters.")
    return cleaned
