"""Update entities: surface installed packages in Home Assistant's own Updates UI.

One entity per installed package, so they show up in Settings > System >
Updates (and the "Updates available" section on the default dashboard)
alongside every other integration's updates — not just inside IntegratedStore's own
panel. Every entity is attached to a per-package device linked, via
`via_device`, to one shared "IntegratedStore" hub device, which is what gives them
their own group in Settings > Devices & Services rather than being scattered
under whatever device each entity would otherwise default to.

Entities are added and removed dynamically as packages are installed and
uninstalled, driven off the same coordinator the panel itself reads from.
"""

from __future__ import annotations

import logging

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import IntegratedStoreManager
from .catalog import repo_url_for
from .const import DATA_MANAGER, DOMAIN, NAME
from .store import InstalledPackage

_LOGGER = logging.getLogger(__package__)

HUB_IDENTIFIER = (DOMAIN, "store")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up update entities, and keep them in sync as packages change."""
    manager: IntegratedStoreManager = hass.data[DOMAIN][DATA_MANAGER]

    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={HUB_IDENTIFIER},
        name=NAME,
        manufacturer=NAME,
        model="Package store",
        entry_type=DeviceEntryType.SERVICE,
    )

    entities: dict[str, IntegratedStorePackageUpdate] = {}

    @callback
    def _sync() -> None:
        """Add an entity for every installed package, remove it when gone."""
        current_ids = set(manager.store.installed)

        new_entities = [
            IntegratedStorePackageUpdate(manager, package_id)
            for package_id in current_ids - entities.keys()
        ]
        for entity in new_entities:
            entities[entity.package_id] = entity
        if new_entities:
            async_add_entities(new_entities)

        for package_id in entities.keys() - current_ids:
            hass.async_create_task(entities.pop(package_id).async_remove())

    _sync()
    entry.async_on_unload(manager.coordinator.async_add_listener(_sync))


class IntegratedStorePackageUpdate(UpdateEntity):
    """One update entity per installed IntegratedStore package."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_supported_features = UpdateEntityFeature.INSTALL

    def __init__(self, manager: IntegratedStoreManager, package_id: str) -> None:
        """Initialise the entity for a single installed package."""
        self._manager = manager
        self.package_id = package_id
        self._attr_unique_id = f"{DOMAIN}_{package_id}_update"

    @property
    def _installed(self) -> InstalledPackage | None:
        """Live-looked-up each time: installs/uninstalls can happen anytime."""
        return self._manager.store.get_installed(self.package_id)

    @property
    def available(self) -> bool:
        """Unavailable once uninstalled, in the brief window before removal."""
        return self._installed is not None

    @property
    def device_info(self) -> DeviceInfo | None:
        """A per-package device, grouped under the shared IntegratedStore hub device."""
        installed = self._installed
        if installed is None:
            return None
        return DeviceInfo(
            identifiers={(DOMAIN, installed.id)},
            name=installed.name,
            manufacturer=NAME,
            model=installed.category.capitalize(),
            via_device=HUB_IDENTIFIER,
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def name(self) -> str | None:
        """Entity name; combined with the device name by has_entity_name."""
        return "Update"

    @property
    def title(self) -> str | None:
        """Package name, shown in the Updates list."""
        installed = self._installed
        return installed.name if installed else None

    @property
    def installed_version(self) -> str | None:
        """Currently installed version."""
        installed = self._installed
        return installed.version if installed else None

    @property
    def latest_version(self) -> str | None:
        """Newest version the coordinator has seen.

        Falls back to the installed version (rather than None) once a package
        is known to be unreachable, so a stale/offline source shows as
        "up to date" instead of a permanent, unactionable update badge.
        """
        installed = self._installed
        if installed is None:
            return None
        status = self._manager.coordinator.status_for(self.package_id)
        return status.latest_version or installed.version

    @property
    def release_url(self) -> str | None:
        """Link to the package's repository, when one can be derived."""
        installed = self._installed
        return repo_url_for(installed.source) if installed else None

    async def async_install(self, version: str | None, backup: bool, **kwargs) -> None:
        """Update the package to `version` (or the latest) via the manager.

        This is the same code path the panel's own Update button uses —
        IntegratedStore has exactly one way to change what's on disk, regardless of
        which UI triggered it.
        """
        await self._manager.async_update(self.package_id, version)
        self.async_write_ha_state()
