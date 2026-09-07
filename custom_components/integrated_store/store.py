"""Persistent state for IntegratedStore.

Wraps homeassistant.helpers.storage.Store. Holds what is installed (and at what
version, and which files we put on disk so we can cleanly remove them), the
user's custom repositories, and whether a restart is pending.

Never holds tokens — those live in the config entry.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store

from .const import (
    DOMAIN,
    RESTART_ISSUE_ID,
    STORAGE_KEY,
    STORAGE_VERSION,
    STORE_CUSTOM_REPOS,
    STORE_INSTALLED,
    STORE_RESTART_REQUIRED,
)

_LOGGER = logging.getLogger(__package__)


@dataclass
class InstalledPackage:
    """A package we have installed, and everything needed to remove it again."""

    id: str
    name: str
    category: str
    version: str
    source: dict[str, Any]
    # Config-dir-relative paths we created. Removal only ever touches these.
    paths: list[str] = field(default_factory=list)
    # Lovelace resource URLs we registered, so uninstall can unregister them.
    lovelace_resources: list[str] = field(default_factory=list)
    domain: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> InstalledPackage:
        """Build from stored JSON, tolerating keys added in later versions."""
        return cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            category=data["category"],
            version=data.get("version", ""),
            source=data.get("source", {}),
            paths=list(data.get("paths", [])),
            lovelace_resources=list(data.get("lovelace_resources", [])),
            domain=data.get("domain"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise for storage."""
        return asdict(self)


@dataclass
class CustomRepo:
    """A user-added repository, shaped like a catalog entry."""

    id: str
    name: str
    category: str
    source: dict[str, Any]
    description: str = ""
    domain: str | None = None
    icon: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CustomRepo:
        """Build from stored JSON."""
        return cls(
            id=data["id"],
            name=data.get("name", data["id"]),
            category=data["category"],
            source=data.get("source", {}),
            description=data.get("description", ""),
            domain=data.get("domain"),
            icon=data.get("icon"),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialise for storage."""
        return asdict(self)


class IntegratedStoreStore:
    """Async, in-memory-cached view over the on-disk store."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialise the store."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.installed: dict[str, InstalledPackage] = {}
        self.custom_repos: dict[str, CustomRepo] = {}
        self.restart_required: bool = False
        self._loaded = False

    async def async_load(self) -> None:
        """Load persisted state. Safe to call more than once."""
        if self._loaded:
            return

        data = await self._store.async_load() or {}

        for raw in data.get(STORE_INSTALLED, []):
            try:
                package = InstalledPackage.from_dict(raw)
            except (KeyError, TypeError):
                _LOGGER.warning("Discarding malformed installed entry: %s", raw)
                continue
            self.installed[package.id] = package

        for raw in data.get(STORE_CUSTOM_REPOS, []):
            try:
                repo = CustomRepo.from_dict(raw)
            except (KeyError, TypeError):
                _LOGGER.warning("Discarding malformed custom repo entry: %s", raw)
                continue
            self.custom_repos[repo.id] = repo

        self.restart_required = bool(data.get(STORE_RESTART_REQUIRED, False))
        self._loaded = True
        # Reflect whatever was loaded as a Repair immediately: if HA crashed or
        # was killed before a previous session cleared this, the Repair should
        # still be there rather than silently missing until the next change.
        self._sync_restart_issue()

        _LOGGER.debug(
            "Loaded %s installed package(s) and %s custom repo(s)",
            len(self.installed),
            len(self.custom_repos),
        )

    async def async_save(self) -> None:
        """Persist current state."""
        await self._store.async_save(
            {
                STORE_INSTALLED: [
                    package.to_dict() for package in self.installed.values()
                ],
                STORE_CUSTOM_REPOS: [
                    repo.to_dict() for repo in self.custom_repos.values()
                ],
                STORE_RESTART_REQUIRED: self.restart_required,
            }
        )

    # --- installed packages --------------------------------------------------

    def get_installed(self, package_id: str) -> InstalledPackage | None:
        """Return the installed record for a package, if any."""
        return self.installed.get(package_id)

    async def async_add_installed(self, package: InstalledPackage) -> None:
        """Record a package as installed and persist."""
        self.installed[package.id] = package
        await self.async_save()

    async def async_remove_installed(self, package_id: str) -> None:
        """Forget a package and persist."""
        self.installed.pop(package_id, None)
        await self.async_save()

    # --- custom repositories -------------------------------------------------

    async def async_add_custom_repo(self, repo: CustomRepo) -> None:
        """Add or replace a custom repository and persist."""
        self.custom_repos[repo.id] = repo
        await self.async_save()

    async def async_remove_custom_repo(self, repo_id: str) -> None:
        """Remove a custom repository and persist."""
        self.custom_repos.pop(repo_id, None)
        await self.async_save()

    # --- restart flag --------------------------------------------------------

    async def async_set_restart_required(self, value: bool) -> None:
        """Set the pending-restart flag, persist it, and sync the Repair."""
        if self.restart_required != value:
            self.restart_required = value
            await self.async_save()
        self._sync_restart_issue()

    def _sync_restart_issue(self) -> None:
        """Create or clear the Settings > System > Repairs entry.

        This is the actual Home Assistant Repairs integration
        (homeassistant.helpers.issue_registry), not just our own panel banner
        — the two are kept in sync from this one place so they can never
        disagree.
        """
        if self.restart_required:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                RESTART_ISSUE_ID,
                is_fixable=True,
                severity=ir.IssueSeverity.WARNING,
                translation_key=RESTART_ISSUE_ID,
            )
        else:
            ir.async_delete_issue(self.hass, DOMAIN, RESTART_ISSUE_ID)
