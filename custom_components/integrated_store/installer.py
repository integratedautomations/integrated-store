"""Installing and removing packages on disk.

The contract for every install is the same:

1. Work out, from the downloaded content, which files belong to the package and
   where they go (per category).
2. Refuse anything that would write outside the Home Assistant config dir.
3. Build the finished directory somewhere else on the *same* filesystem.
4. Swap it into place with a rename, keeping the previous version aside until
   the swap succeeds, so a failure rolls back rather than leaving rubble.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant

from .catalog import CatalogPackage
from .const import (
    BAK_PREFIX,
    CATEGORY_BLUEPRINT,
    CATEGORY_INTEGRATION,
    CATEGORY_LOVELACE,
    LOVELACE_RESOURCE_BASE,
    PATH_BLUEPRINTS,
    PATH_CUSTOM_COMPONENTS,
    PATH_WWW_COMMUNITY,
    RESTART_REQUIRED_CATEGORIES,
    TMP_PREFIX,
)
from .exceptions import InstallError, ValidationError
from .sources import descend_single_root
from .store import InstalledPackage, IntegratedStoreStore
from .utils import ensure_within, is_within, slugify

_LOGGER = logging.getLogger(__package__)

BLUEPRINT_DOMAINS = ("automation", "script", "template")


@dataclass
class InstallResult:
    """Outcome of an install, including anything the user needs to be told."""

    package: InstalledPackage
    restart_required: bool = False
    messages: list[str] = field(default_factory=list)


class Installer:
    """Places downloaded package content into the config directory."""

    def __init__(self, hass: HomeAssistant, store: IntegratedStoreStore) -> None:
        """Initialise the installer."""
        self.hass = hass
        self.store = store
        self.config_dir = Path(hass.config.config_dir).resolve()

    # --- public API ----------------------------------------------------------

    async def async_install(
        self, package: CatalogPackage, version: str, downloaded: Path
    ) -> InstallResult:
        """Install downloaded content for `package` at `version`."""
        content_root = await self.hass.async_add_executor_job(
            descend_single_root, downloaded
        )

        if package.category == CATEGORY_INTEGRATION:
            result = await self._async_install_integration(
                package, version, content_root
            )
        elif package.category == CATEGORY_LOVELACE:
            result = await self._async_install_lovelace(package, version, content_root)
        elif package.category == CATEGORY_BLUEPRINT:
            result = await self._async_install_blueprint(package, version, content_root)
        else:  # pragma: no cover - catalog validation blocks this earlier
            raise ValidationError(f"Cannot install category '{package.category}'.")

        await self.store.async_add_installed(result.package)
        if result.restart_required:
            await self.store.async_set_restart_required(True)

        _LOGGER.info(
            "Installed %s %s to %s",
            package.id,
            version,
            ", ".join(result.package.paths),
        )
        return result

    async def async_uninstall(self, installed: InstalledPackage) -> list[str]:
        """Remove a package's files and Lovelace resources.

        Only paths we recorded at install time are touched, and each is
        re-validated against the config dir before removal.
        """
        messages: list[str] = []

        for url in installed.lovelace_resources:
            if message := await self._async_remove_lovelace_resource(url):
                messages.append(message)

        for relative in installed.paths:
            target = (self.config_dir / relative).resolve()
            if not self._is_managed_path(target):
                _LOGGER.error(
                    "Refusing to remove %s: not a path IntegratedStore manages", target
                )
                messages.append(
                    f"Skipped removing '{relative}' because it is outside the "
                    "directories IntegratedStore manages."
                )
                continue
            await self.hass.async_add_executor_job(_remove_path, target)

        await self.store.async_remove_installed(installed.id)

        if installed.category in RESTART_REQUIRED_CATEGORIES:
            await self.store.async_set_restart_required(True)
            messages.append("Restart Home Assistant to finish removing this package.")

        _LOGGER.info("Uninstalled %s", installed.id)
        return messages

    # --- category handlers ---------------------------------------------------

    async def _async_install_integration(
        self, package: CatalogPackage, version: str, content_root: Path
    ) -> InstallResult:
        """Install into custom_components/<domain>/."""
        source_dir, manifest = await self.hass.async_add_executor_job(
            _find_integration, content_root
        )

        domain = manifest.get("domain")
        if not domain or not isinstance(domain, str):
            raise ValidationError(
                f"{package.name}: the integration's manifest.json has no 'domain'."
            )
        if package.domain and package.domain != domain:
            # A mismatch means the catalog entry and the code disagree about
            # what is being installed; installing anyway could overwrite an
            # unrelated integration.
            raise ValidationError(
                f"{package.name}: expected domain '{package.domain}' but the "
                f"downloaded manifest declares '{domain}'."
            )

        target = self._target(f"{PATH_CUSTOM_COMPONENTS}/{domain}")
        await self._async_swap_dir(source_dir, target)

        return InstallResult(
            package=InstalledPackage(
                id=package.id,
                name=package.name,
                category=package.category,
                version=version,
                source=package.source,
                paths=[f"{PATH_CUSTOM_COMPONENTS}/{domain}"],
                domain=domain,
            ),
            restart_required=True,
            messages=["Restart Home Assistant to load this integration."],
        )

    async def _async_install_lovelace(
        self, package: CatalogPackage, version: str, content_root: Path
    ) -> InstallResult:
        """Install into www/community/<slug>/ and register the resource."""
        slug = slugify(package.id)
        files, main_js = await self.hass.async_add_executor_job(
            _find_lovelace_files, content_root, slug
        )

        target = self._target(f"{PATH_WWW_COMMUNITY}/{slug}")
        common = await self._async_swap_files(files, target)

        # The entrypoint keeps its position relative to the other files, so the
        # resource URL has to follow it rather than assume the top level.
        main_relative = main_js.relative_to(common).as_posix()
        # Tag the URL with the version, the same trick HACS uses for its own
        # entrypoint. Without this, updating a card overwrites the file on
        # disk at the exact same URL the browser (and the frontend's own ES
        # module cache) already has cached, so the old code keeps running
        # until someone manually edits the resource to force a new URL —
        # which is the whole reason this exists.
        resource_url = f"{LOVELACE_RESOURCE_BASE}/{slug}/{main_relative}?integrated_storetag={version}"
        messages: list[str] = []
        registered: list[str] = []

        previous = self.store.get_installed(package.id)
        if previous:
            for stale_url in previous.lovelace_resources:
                if stale_url != resource_url:
                    await self._async_remove_lovelace_resource(stale_url)

        added, message = await self._async_add_lovelace_resource(resource_url)
        if added:
            registered.append(resource_url)
        if message:
            messages.append(message)

        return InstallResult(
            package=InstalledPackage(
                id=package.id,
                name=package.name,
                category=package.category,
                version=version,
                source=package.source,
                paths=[f"{PATH_WWW_COMMUNITY}/{slug}"],
                lovelace_resources=registered,
            ),
            restart_required=False,
            messages=messages or ["Reload your browser to pick up the new card."],
        )

    async def _async_install_blueprint(
        self, package: CatalogPackage, version: str, content_root: Path
    ) -> InstallResult:
        """Install into blueprints/<type>/<slug>/, one directory per type."""
        slug = slugify(package.id)
        grouped = await self.hass.async_add_executor_job(_find_blueprints, content_root)

        paths: list[str] = []
        for blueprint_domain, files in grouped.items():
            relative = f"{PATH_BLUEPRINTS}/{blueprint_domain}/{slug}"
            target = self._target(relative)
            await self._async_swap_files(files, target)
            paths.append(relative)

        return InstallResult(
            package=InstalledPackage(
                id=package.id,
                name=package.name,
                category=package.category,
                version=version,
                source=package.source,
                paths=paths,
            ),
            restart_required=False,
            messages=[
                "Blueprints are picked up on the next automation or script reload."
            ],
        )

    # --- filesystem ----------------------------------------------------------

    def _target(self, relative: str) -> Path:
        """Resolve a config-relative install path, refusing to escape."""
        target = (self.config_dir / relative).resolve()
        return ensure_within(self.config_dir, target)

    def _is_managed_path(self, target: Path) -> bool:
        """True only for paths under a directory IntegratedStore installs into."""
        if not is_within(self.config_dir, target):
            return False
        if target == self.config_dir:
            return False
        roots = (
            self.config_dir / PATH_CUSTOM_COMPONENTS,
            self.config_dir / Path(PATH_WWW_COMMUNITY),
            self.config_dir / PATH_BLUEPRINTS,
        )
        # Must be strictly below a managed root, never the root itself.
        return any(is_within(root, target) and target != root for root in roots)

    async def _async_swap_dir(self, source_dir: Path, target: Path) -> None:
        """Atomically replace `target` with a copy of `source_dir`."""
        await self.hass.async_add_executor_job(
            _swap_into_place,
            lambda staged: shutil.copytree(source_dir, staged, symlinks=False),
            target,
        )

    async def _async_swap_files(self, files: list[Path], target: Path) -> Path:
        """Atomically replace `target` with a directory holding `files`.

        `files` may include nested paths; their layout relative to their common
        parent is preserved so `dist/` subfolders survive. Returns that common
        parent, which is the directory the installed layout is relative to.
        """
        if not files:
            raise ValidationError("Nothing to install: no matching files found.")

        common = Path(os.path.commonpath([str(item.parent) for item in files]))

        def _prepare(staged: Path) -> None:
            staged.mkdir(parents=True, exist_ok=True)
            for item in files:
                destination = staged / item.relative_to(common)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)

        await self.hass.async_add_executor_job(_swap_into_place, _prepare, target)
        return common

    # --- Lovelace resources --------------------------------------------------

    def _lovelace_resources(self) -> tuple[Any | None, str | None]:
        """Return (resource collection, resource mode), tolerating HA versions."""
        data: Any = None
        try:
            from homeassistant.components.lovelace.const import (
                LOVELACE_DATA,
            )

            data = self.hass.data.get(LOVELACE_DATA)
        except ImportError:
            data = None

        if data is None:
            data = self.hass.data.get("lovelace")
        if data is None:
            return None, None

        if isinstance(data, dict):
            return data.get("resources"), data.get("mode")
        return (
            getattr(data, "resources", None),
            getattr(data, "resource_mode", None) or getattr(data, "mode", None),
        )

    async def _async_add_lovelace_resource(self, url: str) -> tuple[bool, str | None]:
        """Register a Lovelace module resource.

        Returns (registered, message). In YAML resource mode the collection is
        read-only, so we return the line the user needs to add themselves rather
        than failing the install.
        """
        resources, mode = self._lovelace_resources()

        if resources is None:
            return False, (
                "Could not reach the Lovelace resource list. Add this resource "
                f"manually: {url}"
            )

        if mode == "yaml" or not hasattr(resources, "async_create_item"):
            return False, (
                "Your Lovelace resources are managed in YAML. Add this to the "
                f"'resources:' section of your dashboard config: url: {url}, "
                "type: module"
            )

        try:
            if not getattr(resources, "loaded", True):
                await resources.async_load()
                resources.loaded = True

            for item in resources.async_items():
                if item.get("url") == url:
                    _LOGGER.debug("Lovelace resource %s already registered", url)
                    return True, None

            await resources.async_create_item({"res_type": "module", "url": url})
        except Exception as err:  # noqa: BLE001 - never fail an install on this
            _LOGGER.error("Could not register Lovelace resource %s: %s", url, err)
            return False, (
                f"Files installed, but registering the Lovelace resource failed. "
                f"Add it manually: {url}"
            )

        _LOGGER.debug("Registered Lovelace resource %s", url)
        return True, None

    async def _async_remove_lovelace_resource(self, url: str) -> str | None:
        """Unregister a Lovelace resource we previously added."""
        resources, mode = self._lovelace_resources()
        if resources is None or mode == "yaml":
            return f"Remove the Lovelace resource '{url}' from your YAML config."

        try:
            if not getattr(resources, "loaded", True):
                await resources.async_load()
                resources.loaded = True

            for item in resources.async_items():
                if item.get("url") == url:
                    await resources.async_delete_item(item["id"])
                    return None
        except Exception as err:  # noqa: BLE001 - never block an uninstall
            _LOGGER.error("Could not remove Lovelace resource %s: %s", url, err)
            return f"Could not remove the Lovelace resource '{url}': {err}"

        return None


# --- executor-side helpers (blocking) ----------------------------------------


def _swap_into_place(prepare: Callable[[Path], None], target: Path) -> None:
    """Build the new content aside, then rename it into `target`. Blocking.

    Staging happens inside `target.parent` so the final rename is a same-
    filesystem operation, which is what makes it atomic. The previous version is
    moved aside rather than deleted, and restored if anything goes wrong.
    """
    parent = target.parent
    parent.mkdir(parents=True, exist_ok=True)

    staged = parent / f"{TMP_PREFIX}{uuid4().hex}"
    backup = parent / f"{BAK_PREFIX}{uuid4().hex}"
    backed_up = False

    try:
        prepare(staged)
        if not staged.is_dir():
            raise InstallError("Nothing was staged for installation.")

        if target.exists():
            os.replace(target, backup)
            backed_up = True

        os.replace(staged, target)
    except BaseException as err:
        if backed_up and not target.exists():
            with suppress(OSError):
                os.replace(backup, target)
            backed_up = False
        shutil.rmtree(staged, ignore_errors=True)
        if isinstance(err, OSError):
            raise InstallError(f"Could not install to '{target}': {err}") from err
        raise
    finally:
        if backed_up:
            shutil.rmtree(backup, ignore_errors=True)


def _remove_path(target: Path) -> None:
    """Delete a file or directory, tolerating it already being gone. Blocking."""
    if target.is_dir():
        shutil.rmtree(target, ignore_errors=True)
    elif target.exists():
        try:
            target.unlink()
        except OSError:
            _LOGGER.warning("Could not remove %s", target, exc_info=True)


def _find_integration(root: Path) -> tuple[Path, dict[str, Any]]:
    """Locate the integration directory and its manifest. Blocking.

    Handles the three layouts in the wild: a repo with `custom_components/<x>/`,
    a repo whose root *is* the integration, and a repo with the integration one
    level down.
    """
    candidates: list[Path] = []

    components = root / PATH_CUSTOM_COMPONENTS
    if components.is_dir():
        candidates.extend(
            item for item in sorted(components.iterdir()) if item.is_dir()
        )
    if (root / "manifest.json").is_file():
        candidates.append(root)
    candidates.extend(item for item in sorted(root.iterdir()) if item.is_dir())

    for candidate in candidates:
        manifest_file = candidate / "manifest.json"
        if not manifest_file.is_file():
            continue
        try:
            manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(manifest, dict) or "domain" not in manifest:
            continue
        if not any(candidate.glob("*.py")):
            continue
        return candidate, manifest

    raise ValidationError(
        "Could not find an integration in the download: no directory with a "
        "manifest.json containing a 'domain' and at least one .py file."
    )


def _find_lovelace_files(root: Path, slug: str) -> tuple[list[Path], Path]:
    """Locate the JS files to install and the entrypoint. Blocking.

    Prefers a `dist/` directory when present (the usual build output), falling
    back to JS files at the repository root. Returns the entrypoint as a path so
    the caller can work out its location relative to the installed directory —
    a bundle in a subfolder still needs a correct resource URL.
    """
    dist = root / "dist"
    if dist.is_dir():
        files = [item for item in sorted(dist.rglob("*")) if item.is_file()]
        # Prefer a bundle at the top of dist/ over a nested chunk.
        js_files = [
            item for item in files if item.suffix == ".js" and item.parent == dist
        ] or [item for item in files if item.suffix == ".js"]
    else:
        files = [
            item
            for item in sorted(root.iterdir())
            if item.is_file() and item.suffix in (".js", ".map", ".mjs")
        ]
        js_files = [item for item in files if item.suffix in (".js", ".mjs")]

    if not js_files:
        raise ValidationError(
            "Could not find any JavaScript files to install for this card."
        )

    # Pick the entrypoint: an exact slug match, else the shortest name, which
    # in practice is the bundle rather than a chunk or a minified variant.
    named = [item for item in js_files if item.stem == slug]
    main = named[0] if named else min(js_files, key=lambda item: len(item.name))

    return files, main


def _find_blueprints(root: Path) -> dict[str, list[Path]]:
    """Group blueprint YAML files by their declared domain. Blocking."""
    from homeassistant.util.yaml import load_yaml

    grouped: dict[str, list[Path]] = {}

    for item in sorted(root.rglob("*")):
        if not item.is_file() or item.suffix not in (".yaml", ".yml"):
            continue
        try:
            content = load_yaml(str(item))
        except Exception:
            _LOGGER.debug("Skipping unparsable YAML %s", item, exc_info=True)
            continue

        if not isinstance(content, dict):
            continue
        blueprint = content.get("blueprint")
        if not isinstance(blueprint, dict):
            continue

        domain = blueprint.get("domain")
        if domain not in BLUEPRINT_DOMAINS:
            _LOGGER.debug("Skipping blueprint %s with domain %s", item, domain)
            continue

        grouped.setdefault(str(domain), []).append(item)

    if not grouped:
        raise ValidationError(
            "Could not find any blueprints in the download: no YAML file with a "
            "'blueprint:' block declaring a supported domain."
        )
    return grouped
