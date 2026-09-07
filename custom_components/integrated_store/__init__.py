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
from collections.abc import Callable
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

#: Other stores' own storage files. Reading them is best-effort: neither
#: schema is a public API, and they are only used to label an already-present
#: package with which store put it there.
HACS_STORAGE_FILE = ".storage/hacs.repositories"
YIDSTORE_STORAGE_FILE = ".storage/yidstore_packages"

#: YidStore namespaces cards it installs from its Gitea store under this
#: folder (www/community/onoff/<repo>), while GitHub cards use the plain
#: HACS-style path.
YIDSTORE_VENDOR_FOLDER = "onoff"

SOURCE_LABELS = {
    "hacs": "HACS",
    "yidstore": "YidStore",
}


def _repo_name(package: CatalogPackage) -> str | None:
    """The bare repository name, e.g. 'Bubble-Card' for 'Clooos/Bubble-Card'."""
    repo = package.source.get("repo")
    if not isinstance(repo, str) or "/" not in repo:
        return None
    return repo.split("/")[-1].strip() or None


def _predict_target_paths(package: CatalogPackage) -> list[str]:
    """Config-relative paths this package plausibly already occupies.

    More than one, because every store names card directories differently:
    we use the catalog id, HACS uses the bare repository name, and YidStore
    uses either that or its own `onoff/` vendor folder. Checking only our own
    naming is why HACS-installed cards previously went undetected.

    Integrations are simpler — the domain decides the directory for everyone.
    """
    if package.category == CATEGORY_INTEGRATION:
        if not package.domain:
            return []
        return [f"{PATH_CUSTOM_COMPONENTS}/{package.domain}"]

    if package.category == CATEGORY_LOVELACE:
        candidates: list[str] = []
        try:
            candidates.append(f"{PATH_WWW_COMMUNITY}/{slugify(package.id)}")
        except ValidationError:
            pass
        if repo_name := _repo_name(package):
            candidates.append(f"{PATH_WWW_COMMUNITY}/{repo_name}")
            candidates.append(
                f"{PATH_WWW_COMMUNITY}/{YIDSTORE_VENDOR_FOLDER}/{repo_name}"
            )
        # Deduplicate while keeping order: our own path is the most specific.
        return list(dict.fromkeys(candidates))

    return []


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
            "external_conflict_source": None,
        }

    async def _async_external_conflicts(self) -> dict[str, dict[str, Any]]:
        """Best-effort: packages already installed by something other than us.

        Two independent signals, because either alone misses cases:

        * Another store's own records (HACS, YidStore), matched by repository.
          This is definitive and needs no path guessing, which matters because
          every store names card directories differently.
        * Files sitting at any path the package plausibly occupies. This is
          the only signal for a hand-copied install, which no store recorded.

        A registry match counts on its own — requiring a path match too is
        what previously hid HACS-installed cards, since HACS names their
        directory after the repository and we name ours after the catalog id.
        """
        pending = {
            package.id: package
            for package in self.packages.values()
            if not self.store.get_installed(package.id)
        }
        if not pending:
            return {}

        hacs_repos = await self._async_hacs_repo_names()
        yidstore_repos = await self._async_yidstore_repo_names()

        config_dir = Path(self.hass.config.config_dir)
        candidates = {
            package_id: _predict_target_paths(package)
            for package_id, package in pending.items()
        }

        def _scan() -> dict[str, str | None]:
            """Return the first existing path per package, if any. Blocking."""
            found: dict[str, str | None] = {}
            for package_id, relatives in candidates.items():
                found[package_id] = next(
                    (rel for rel in relatives if (config_dir / rel).exists()), None
                )
            return found

        existing = await self.hass.async_add_executor_job(_scan)

        conflicts: dict[str, dict[str, Any]] = {}
        for package_id, package in pending.items():
            repo = str(package.source.get("repo", "")).lower()
            path = existing.get(package_id)

            if repo and repo in hacs_repos:
                source = "hacs"
            elif repo and repo in yidstore_repos:
                source = "yidstore"
            elif path:
                source = "other"
            else:
                continue

            location = f" at '{path}'" if path else ""
            if source == "other":
                message = (
                    f"Files already exist{location} from outside IntegratedStore "
                    "(another store, or a manual install). Installing will "
                    "overwrite them."
                )
            else:
                message = (
                    f"Already installed via {SOURCE_LABELS[source]}{location}. "
                    "Installing here will overwrite those files, and may leave "
                    "the card registered twice."
                )

            conflicts[package_id] = {"message": message, "source": source}

        return conflicts

    async def _async_hacs_repo_names(self) -> set[str]:
        """Best-effort: lower-cased 'owner/repo' names HACS has installed.

        HACS's storage schema is not a public API and has changed shape before,
        so any failure to read or parse it is swallowed and treated as "nothing
        known to be installed via HACS" rather than raising.
        """

        def _extract(content: dict[str, Any]) -> set[str]:
            names: set[str] = set()
            for entry in content.values():
                if not isinstance(entry, dict) or not entry.get("installed"):
                    continue
                if full_name := entry.get("full_name"):
                    names.add(str(full_name).lower())
            return names

        return await self._async_read_repo_names(HACS_STORAGE_FILE, _extract)

    async def _async_yidstore_repo_names(self) -> set[str]:
        """Best-effort: lower-cased 'owner/repo' names YidStore has installed.

        YidStore records each package with separate `owner` and `repo` fields
        rather than a combined name, so both shapes are accepted. As with
        HACS, this is another project's private storage format, so anything
        unexpected degrades to "nothing known" instead of failing.
        """

        def _extract(content: dict[str, Any]) -> set[str]:
            names: set[str] = set()
            for entry in content.values():
                if not isinstance(entry, dict):
                    continue
                if full_name := entry.get("full_name"):
                    names.add(str(full_name).lower())
                    continue
                owner, repo = entry.get("owner"), entry.get("repo")
                if owner and repo:
                    names.add(f"{owner}/{repo}".lower())
            return names

        return await self._async_read_repo_names(YIDSTORE_STORAGE_FILE, _extract)

    async def _async_read_repo_names(
        self,
        storage_file: str,
        extract: Callable[[dict[str, Any]], set[str]],
    ) -> set[str]:
        """Read another store's storage file and pull repository names from it."""
        path = Path(self.hass.config.path(*storage_file.split("/")))

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

            try:
                return extract(content)
            except (AttributeError, TypeError, ValueError):
                _LOGGER.debug("Could not parse %s", storage_file, exc_info=True)
                return set()

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
                item["external_conflict_source"] = conflict["source"]
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
                    "external_conflict_source": None,
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
        """Install a package.

        Refuses outright if the package looks like it's already on disk via
        another store or a manual copy, rather than merely warning: installing
        over it would create a second copy under a different directory name
        and, for a card, a duplicate Lovelace resource — silently breaking the
        card rather than updating it. The panel disables the button for the
        same reason, but that's only a courtesy; this is the real block, since
        the API can be called directly.
        """
        async with self._lock:
            package = self._package(package_id)
            if self.store.get_installed(package_id):
                raise ValidationError(
                    f"{package.name} is already installed. Use update instead."
                )

            conflicts = await self._async_external_conflicts()
            if conflict := conflicts.get(package_id):
                fix = (
                    f"Uninstall it from {SOURCE_LABELS[conflict['source']]} first"
                    if conflict["source"] in SOURCE_LABELS
                    else "Remove the existing files first"
                )
                raise ValidationError(
                    f"{conflict['message']} {fix}, then install it here."
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
