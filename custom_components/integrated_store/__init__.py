"""The IntegratedStore integration.

Sets up the store: persistent state, the catalog, the update coordinator, the
sidebar panel and its static assets, and the HTTP API the panel talks to.

IntegratedStoreManager is the single orchestration point. Everything that mutates the
system (install, update, uninstall) goes through it, behind one lock, so two
panel clicks cannot race each other onto the same directory.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from homeassistant.components import frontend
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from .api import async_register_views
from .catalog import CatalogPackage, async_load_catalog, repo_url_for
from .const import (
    CATEGORIES,
    CATEGORY_INTEGRATION,
    CATEGORY_LOVELACE,
    DATA_BOOT_SEEN,
    DATA_MANAGER,
    DATA_STATIC_REGISTERED,
    DOMAIN,
    LOVELACE_RESOURCE_BASE,
    PANEL_ELEMENT,
    PANEL_ICON,
    PANEL_MODULE_URL,
    PANEL_TITLE,
    PANEL_URL_PATH,
    PATH_CUSTOM_COMPONENTS,
    PATH_WWW_COMMUNITY,
    RESTART_REQUIRED_CATEGORIES,
    STATIC_URL_BASE,
    VERSION,
)
from .coordinator import IntegratedStoreCoordinator
from .exceptions import (
    IntegratedStoreError,
    NotFoundError,
    SourceError,
    ValidationError,
)
from .installer import Installer
from .sources import async_cleanup, async_get_source
from .store import CustomRepo, IntegratedStoreStore
from .utils import async_register_static_path, slugify, version_is_newer

_LOGGER = logging.getLogger(__package__)

PLATFORMS: list[Platform] = [Platform.UPDATE]

#: HACS's own storage file. Reading it is best-effort: the schema is not a
#: public API and is only used to upgrade a generic "files already exist"
#: warning into a more specific "already installed via HACS" one.
HACS_STORAGE_FILE = ".storage/hacs.repositories"


def _predict_target_path(package: CatalogPackage) -> str | None:
    """Best-effort: where this package would land on disk if installed.

    Used only to warn about pre-existing files before anything is downloaded.
    The installer works out the real path once files are in hand, since an
    integration's true install path depends on the domain declared inside its
    downloaded manifest.json, not just the catalog entry.
    """
    if package.category == CATEGORY_INTEGRATION:
        if not package.domain:
            return None
        return f"{PATH_CUSTOM_COMPONENTS}/{package.domain}"
    if package.category == CATEGORY_LOVELACE:
        try:
            return f"{PATH_WWW_COMMUNITY}/{slugify(package.id)}"
        except ValidationError:
            return None
    return None


class IntegratedStoreManager:
    """Owns the store's runtime state and every mutating operation."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, store: IntegratedStoreStore
    ) -> None:
        """Initialise the manager."""
        self.hass = hass
        self.entry = entry
        self.store = store
        self.installer = Installer(hass, store)
        self.packages: dict[str, CatalogPackage] = {}
        self.coordinator = IntegratedStoreCoordinator(
            hass,
            entry,
            lambda: self.packages,
            lambda: dict(self.entry.options),
        )
        # Serialises install/update/uninstall: these touch shared directories.
        self._lock = asyncio.Lock()

    # --- lifecycle -----------------------------------------------------------

    async def async_setup(self) -> None:
        """Load the catalog and do a first version check."""
        self.packages = await async_load_catalog(self.hass, self.store)
        # Deliberately not async_config_entry_first_refresh(): an unreachable
        # source should not stop the panel from loading and showing what is
        # already installed.
        await self.coordinator.async_refresh()

    async def async_refresh(self, force: bool = False) -> None:
        """Reload the catalog and re-poll every source."""
        self.packages = await async_load_catalog(self.hass, self.store)
        if force:
            await self.coordinator.async_refresh()
        else:
            await self.coordinator.async_request_refresh()

    # --- reads ---------------------------------------------------------------

    def _serialise(self, package: CatalogPackage) -> dict[str, Any]:
        """Build the panel's view of one package. Never includes tokens."""
        installed = self.store.get_installed(package.id)
        status = self.coordinator.status_for(package.id)
        latest = status.latest_version

        return {
            **package.to_dict(),
            "installed": installed is not None,
            "installed_version": installed.version if installed else None,
            "available_version": latest,
            "update_available": bool(
                installed and version_is_newer(latest, installed.version)
            ),
            "needs_restart_on_change": package.category in RESTART_REQUIRED_CATEGORIES,
            "error": status.error,
            "external_conflict": None,
            "external_conflict_via_hacs": False,
        }

    async def _async_external_conflicts(self) -> dict[str, dict[str, Any]]:
        """Best-effort: not-yet-installed packages with files already on disk.

        Covers anything that put files at a package's target path outside of
        IntegratedStore's own records: a manual copy, or another store entirely. HACS's
        storage file is cross-referenced, when present, to distinguish the two:
        each result carries both a confirm-before-install message and a
        `via_hacs` flag the panel uses to label the badge accordingly.
        """
        candidates: dict[str, str] = {}
        for package in self.packages.values():
            if self.store.get_installed(package.id):
                continue
            target = _predict_target_path(package)
            if target:
                candidates[package.id] = target

        if not candidates:
            return {}

        config_dir = Path(self.hass.config.config_dir)
        hacs_repos = await self._async_hacs_repo_names()
        packages = self.packages

        def _scan() -> dict[str, dict[str, Any]]:
            found: dict[str, dict[str, Any]] = {}
            for package_id, relative in candidates.items():
                if not (config_dir / relative).exists():
                    continue
                package = packages[package_id]
                repo = (
                    str(package.source.get("repo", "")).lower()
                    if package.source.get("type") == "github"
                    else None
                )
                via_hacs = bool(repo and repo in hacs_repos)
                message = (
                    f"Already installed via HACS at '{relative}'. Installing "
                    "here will overwrite those files."
                    if via_hacs
                    else (
                        f"Files already exist at '{relative}' from outside "
                        "IntegratedStore (possibly HACS or a manual install). Installing "
                        "will overwrite them."
                    )
                )
                found[package_id] = {"message": message, "via_hacs": via_hacs}
            return found

        return await self.hass.async_add_executor_job(_scan)

    async def _async_hacs_repo_names(self) -> set[str]:
        """Best-effort: lower-cased 'owner/repo' names HACS has installed.

        HACS's storage schema is not a public API and has changed shape before,
        so any failure to read or parse it is swallowed and treated as "nothing
        known to be installed via HACS" rather than raising.
        """
        path = Path(self.hass.config.path(*HACS_STORAGE_FILE.split("/")))

        def _read() -> set[str]:
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                return set()
            try:
                parsed = json.loads(raw)
            except ValueError:
                return set()

            # homeassistant.helpers.storage.Store wraps saved content as
            # {"version": ..., "key": ..., "data": <actual content>}.
            content = parsed
            if isinstance(parsed, dict) and isinstance(parsed.get("data"), dict):
                content = parsed["data"]
            if not isinstance(content, dict):
                return set()

            names: set[str] = set()
            for entry in content.values():
                if not isinstance(entry, dict) or not entry.get("installed"):
                    continue
                if full_name := entry.get("full_name"):
                    names.add(str(full_name).lower())
            return names

        try:
            return await self.hass.async_add_executor_job(_read)
        except OSError:
            return set()

    async def async_get_readme(self, package_id: str) -> dict[str, Any]:
        """Fetch a package's README, best-effort.

        A missing or unreachable README is not an error: it just means the
        panel shows "No README available" instead of a red error banner.
        """
        package = self._package(package_id)
        source = async_get_source(self.hass, package.source, dict(self.entry.options))
        try:
            readme = await source.get_readme()
        except IntegratedStoreError as err:
            _LOGGER.debug("Could not fetch README for %s: %s", package_id, err)
            readme = None
        return {"id": package_id, "readme": readme}

    async def async_get_state(self) -> dict[str, Any]:
        """Full state for the panel."""
        conflicts = await self._async_external_conflicts()

        visible: list[CatalogPackage] = []
        hidden_count = 0
        for package in self.packages.values():
            # A "not found" repo is hidden rather than shown disabled: for a
            # private repo with no usable token this is the common case, not
            # an outage, so a permanent red badge would just be noise. Once
            # installed, a package always stays visible regardless, so it can
            # still be uninstalled if its source later becomes unreachable.
            status = self.coordinator.status_for(package.id)
            if status.not_found and not self.store.get_installed(package.id):
                hidden_count += 1
                continue
            visible.append(package)

        packages = [self._serialise(package) for package in visible]
        for item in packages:
            if conflict := conflicts.get(item["id"]):
                item["external_conflict"] = conflict["message"]
                item["external_conflict_via_hacs"] = conflict["via_hacs"]
        packages.sort(
            key=lambda item: (
                not item["update_available"],
                item["category"],
                item["name"].lower(),
            )
        )

        # Anything installed that has since left the catalog still needs to be
        # visible, otherwise the user cannot uninstall it.
        for installed in self.store.installed.values():
            if installed.id in self.packages:
                continue
            packages.append(
                {
                    "id": installed.id,
                    "name": installed.name,
                    "description": "No longer in the catalog.",
                    "category": installed.category,
                    "domain": installed.domain,
                    "icon": None,
                    "brand_icon": None,
                    "custom": True,
                    "source_type": installed.source.get("type"),
                    "repo_url": repo_url_for(installed.source),
                    "installed": True,
                    "installed_version": installed.version,
                    "available_version": None,
                    "update_available": False,
                    "needs_restart_on_change": installed.category
                    in RESTART_REQUIRED_CATEGORIES,
                    "error": None,
                    "orphaned": True,
                    "external_conflict": None,
                    "external_conflict_via_hacs": False,
                }
            )

        return {
            "packages": packages,
            "categories": CATEGORIES,
            "restart_required": self.store.restart_required,
            "last_update_success": self.coordinator.last_update_success,
            "hidden_count": hidden_count,
        }

    # --- mutations -----------------------------------------------------------

    def _package(self, package_id: str) -> CatalogPackage:
        """Look up a catalog package or fail with a clear message."""
        package = self.packages.get(package_id)
        if package is None:
            raise NotFoundError(f"Unknown package '{package_id}'.")
        return package

    async def _async_download_and_install(
        self, package: CatalogPackage, version: str | None
    ) -> dict[str, Any]:
        """Resolve a version, download it, and install. Assumes the lock held."""
        options = dict(self.entry.options)
        source = async_get_source(self.hass, package.source, options)

        resolved = version or await source.get_latest_version()
        if not resolved:
            raise SourceError(
                f"Could not determine a version to install for {package.name}."
            )

        downloaded: Path | None = None
        try:
            downloaded = await source.download(resolved)
            result = await self.installer.async_install(package, resolved, downloaded)
        finally:
            # download() returns <temproot>/extracted; clean the whole temproot.
            if downloaded is not None:
                await async_cleanup(self.hass, downloaded.parent)

        await self.coordinator.async_request_refresh()

        return {
            "ok": True,
            "id": package.id,
            "version": resolved,
            "restart_required": result.restart_required,
            "messages": result.messages,
            "package": self._serialise(package),
        }

    async def async_install(
        self, package_id: str, version: str | None = None
    ) -> dict[str, Any]:
        """Install a package."""
        async with self._lock:
            package = self._package(package_id)
            if self.store.get_installed(package_id):
                raise ValidationError(
                    f"{package.name} is already installed. Use update instead."
                )
            _LOGGER.info("Installing %s", package_id)
            return await self._async_download_and_install(package, version)

    async def async_update(
        self, package_id: str, version: str | None = None
    ) -> dict[str, Any]:
        """Update an installed package in place.

        The installer's swap keeps the old version until the new one is staged,
        so a failed update leaves the working copy untouched.
        """
        async with self._lock:
            package = self._package(package_id)
            if not self.store.get_installed(package_id):
                raise ValidationError(
                    f"{package.name} is not installed, so there is nothing to update."
                )
            _LOGGER.info("Updating %s", package_id)
            return await self._async_download_and_install(package, version)

    async def async_uninstall(self, package_id: str) -> dict[str, Any]:
        """Uninstall a package."""
        async with self._lock:
            installed = self.store.get_installed(package_id)
            if installed is None:
                raise NotFoundError(f"'{package_id}' is not installed.")

            _LOGGER.info("Uninstalling %s", package_id)
            messages = await self.installer.async_uninstall(installed)
            await self.coordinator.async_request_refresh()

            return {
                "ok": True,
                "id": package_id,
                "messages": messages,
                "restart_required": self.store.restart_required,
            }

    async def async_add_custom_repo(self, data: dict[str, Any]) -> dict[str, Any]:
        """Add a custom repository, verifying it is reachable first.

        Checking now means a typo in a URL or a missing token is reported in the
        dialog, not silently, three hours later, as a red badge on a card.
        """
        repo = CustomRepo.from_dict(data)

        if repo.id in self.packages and not self.packages[repo.id].custom:
            raise ValidationError(
                f"'{repo.id}' is already in the curated catalog. Pick another id."
            )

        # Constructing the source validates the config block.
        source = async_get_source(self.hass, repo.source, dict(self.entry.options))
        metadata = await source.get_metadata()

        if not repo.description and metadata.description:
            repo.description = metadata.description

        await self.store.async_add_custom_repo(repo)
        await self.async_refresh(force=True)

        _LOGGER.info("Added custom repository %s", repo.id)
        return {
            "ok": True,
            "id": repo.id,
            "latest_version": metadata.latest_version,
            "package": self._serialise(self.packages[repo.id]),
        }

    async def async_remove_custom_repo(self, repo_id: str) -> dict[str, Any]:
        """Forget a custom repository."""
        if repo_id not in self.store.custom_repos:
            raise NotFoundError(f"'{repo_id}' is not a custom repository.")
        if self.store.get_installed(repo_id):
            raise ValidationError(
                "Uninstall this package before removing its repository."
            )

        await self.store.async_remove_custom_repo(repo_id)
        await self.async_refresh(force=True)

        _LOGGER.info("Removed custom repository %s", repo_id)
        return {"ok": True, "id": repo_id}


# --- integration setup -------------------------------------------------------


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up via YAML: nothing to do, IntegratedStore is config-entry only."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up IntegratedStore from a config entry."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    # hass.data starts empty on a genuine Home Assistant restart but survives
    # a plain config-entry reload (e.g. after changing options), so this is
    # true only the first time we run since the process started.
    is_first_setup_this_boot = not domain_data.get(DATA_BOOT_SEEN)
    domain_data[DATA_BOOT_SEEN] = True

    store = IntegratedStoreStore(hass)
    await store.async_load()

    if is_first_setup_this_boot and store.restart_required:
        # Whatever previously needed a restart has now had one, by definition
        # of this being the first setup since boot — otherwise the flag would
        # sit there forever, and Repairs would never clear itself.
        await store.async_set_restart_required(False)

    manager = IntegratedStoreManager(hass, entry, store)
    await manager.async_setup()

    domain_data[DATA_MANAGER] = manager

    await _async_register_static_paths(hass)
    async_register_views(hass)
    _async_register_panel(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    _LOGGER.info(
        "IntegratedStore ready with %s package(s), %s installed",
        len(manager.packages),
        len(store.installed),
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload the config entry.

    HTTP views and static paths cannot be unregistered in Home Assistant, so
    they are left in place; the views resolve the manager per request and fail
    cleanly while it is absent.
    """
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    frontend.async_remove_panel(hass, PANEL_URL_PATH)
    hass.data.get(DOMAIN, {}).pop(DATA_MANAGER, None)
    return unloaded


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when credentials change, so sources pick up the new tokens."""
    await hass.config_entries.async_reload(entry.entry_id)


async def _async_register_static_paths(hass: HomeAssistant) -> None:
    """Serve the panel assets and the installed-cards directory."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_STATIC_REGISTERED):
        return

    panel_dir = Path(__file__).parent / "panel"
    await async_register_static_path(
        hass, STATIC_URL_BASE, str(panel_dir), cache_headers=False
    )

    community_dir = Path(hass.config.path(*PATH_WWW_COMMUNITY.split("/")))
    await hass.async_add_executor_job(lambda: os.makedirs(community_dir, exist_ok=True))
    await async_register_static_path(
        hass, LOVELACE_RESOURCE_BASE, str(community_dir), cache_headers=True
    )

    domain_data[DATA_STATIC_REGISTERED] = True


def _async_register_panel(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Register the sidebar panel.

    A custom panel (rather than an iframe) so the element is handed the `hass`
    object and can make authenticated API calls without touching tokens itself.
    The version query string busts the browser cache on upgrade.
    """
    frontend.async_register_built_in_panel(
        hass,
        component_name="custom",
        sidebar_title=PANEL_TITLE,
        sidebar_icon=PANEL_ICON,
        frontend_url_path=PANEL_URL_PATH,
        require_admin=True,
        config={
            "_panel_custom": {
                "name": PANEL_ELEMENT,
                "embed_iframe": False,
                "trust_external": False,
                "module_url": f"{PANEL_MODULE_URL}?v={VERSION}",
            }
        },
    )


__all__ = [
    "IntegratedStoreError",
    "IntegratedStoreManager",
    "async_setup_entry",
    "async_unload_entry",
]
