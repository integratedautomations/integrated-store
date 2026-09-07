"""Update coordinator: polls every known package for its newest version.

Failures are recorded per package rather than failing the whole refresh — one
unreachable private Git server should not blank out the rest of the store.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .catalog import CatalogPackage
from .const import DOMAIN, UPDATE_INTERVAL
from .exceptions import IntegratedStoreError, NotFoundError
from .sources import async_get_source

_LOGGER = logging.getLogger(__package__)

# Cap on concurrent version checks, to stay friendly to API rate limits and to
# small self-hosted forges.
MAX_CONCURRENT_CHECKS = 5


@dataclass
class PackageStatus:
    """The result of checking one package for updates."""

    latest_version: str | None = None
    error: str | None = None
    # True specifically for "repository not found" — which covers both a typo
    # and a private repo with no usable token, since GitHub/Gitea/GitLab return
    # the same 404 for both by design. Other errors (timeouts, rate limits,
    # auth-but-wrong-scope) are not treated this way: those are usually
    # transient or fixable in place, and hiding a package over them would make
    # a real outage harder to notice, not easier.
    not_found: bool = False


class IntegratedStoreCoordinator(DataUpdateCoordinator[dict[str, PackageStatus]]):
    """Polls package sources for the latest available versions."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        get_packages: Callable[[], dict[str, CatalogPackage]],
        get_options: Callable[[], dict[str, Any]],
    ) -> None:
        """Initialise the coordinator.

        The catalog and the options are read through callables because both can
        change while we are running (custom repos added, options reconfigured).
        """
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
            config_entry=entry,
        )
        self._get_packages = get_packages
        self._get_options = get_options

    async def _async_update_data(self) -> dict[str, PackageStatus]:
        """Check every known package for its newest version."""
        packages = self._get_packages()
        options = self._get_options()
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_CHECKS)

        async def _check(package: CatalogPackage) -> tuple[str, PackageStatus]:
            async with semaphore:
                try:
                    source = async_get_source(self.hass, package.source, options)
                    version = await source.get_latest_version()
                except NotFoundError as err:
                    _LOGGER.debug("Version check failed for %s: %s", package.id, err)
                    return package.id, PackageStatus(error=str(err), not_found=True)
                except IntegratedStoreError as err:
                    _LOGGER.debug("Version check failed for %s: %s", package.id, err)
                    return package.id, PackageStatus(error=str(err))
                except Exception as err:
                    _LOGGER.exception("Unexpected error checking %s", package.id)
                    return package.id, PackageStatus(error=f"Unexpected error: {err}")
                return package.id, PackageStatus(latest_version=version)

        results = await asyncio.gather(
            *(_check(package) for package in packages.values())
        )

        statuses = dict(results)
        failed = sum(1 for status in statuses.values() if status.error)
        if failed:
            _LOGGER.info(
                "Checked %s package(s) for updates, %s could not be reached",
                len(statuses),
                failed,
            )
        return statuses

    def status_for(self, package_id: str) -> PackageStatus:
        """Return the last known status for a package."""
        if not self.data:
            return PackageStatus()
        return self.data.get(package_id, PackageStatus())
