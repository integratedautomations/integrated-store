"""HTTP API backing the IntegratedStore panel.

All views require an authenticated admin. Every handler funnels IntegratedStore errors
into a JSON body with a real status code, so the panel can show the user what
went wrong instead of spinning forever.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING, Any

import voluptuous as vol
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers import config_validation as cv

from .const import (
    CATEGORIES,
    DATA_MANAGER,
    DATA_VIEWS_REGISTERED,
    DOMAIN,
    SOURCE_TYPES,
)
from .exceptions import (
    InstallError,
    IntegratedStoreError,
    NotFoundError,
    SourceError,
    ValidationError,
)

if TYPE_CHECKING:
    from . import IntegratedStoreManager

_LOGGER = logging.getLogger(__package__)

ERROR_STATUS: dict[type[Exception], int] = {
    ValidationError: 400,
    NotFoundError: 404,
    SourceError: 502,
    InstallError: 500,
}

PACKAGE_ID_SCHEMA = vol.Schema(
    {
        vol.Required("id"): cv.string,
        vol.Optional("version"): vol.Any(None, cv.string),
    }
)

SOURCE_SCHEMA = vol.Schema(
    {vol.Required("type"): vol.In(SOURCE_TYPES)}, extra=vol.ALLOW_EXTRA
)

CUSTOM_REPO_SCHEMA = vol.Schema(
    {
        vol.Required("id"): cv.string,
        vol.Required("name"): cv.string,
        vol.Required("category"): vol.In(CATEGORIES),
        vol.Required("source"): SOURCE_SCHEMA,
        vol.Optional("description", default=""): cv.string,
        vol.Optional("domain"): vol.Any(None, cv.string),
        vol.Optional("icon"): vol.Any(None, cv.string),
    }
)


def handle_errors(
    func: Callable[..., Awaitable[web.Response]],
) -> Callable[..., Awaitable[web.Response]]:
    """Turn IntegratedStore errors into JSON responses the panel can render."""

    @wraps(func)
    async def wrapper(
        self: IntegratedStoreBaseView, request: web.Request, *args: Any, **kwargs: Any
    ) -> web.Response:
        try:
            return await func(self, request, *args, **kwargs)
        except vol.Invalid as err:
            return self.json({"error": f"Invalid request: {err}"}, status_code=400)
        except IntegratedStoreError as err:
            status = ERROR_STATUS.get(type(err), 500)
            _LOGGER.debug("API error (%s): %s", status, err)
            return self.json({"error": str(err)}, status_code=status)
        except Exception as err:
            _LOGGER.exception("Unhandled error in %s", func.__name__)
            return self.json({"error": f"Unexpected error: {err}"}, status_code=500)

    return wrapper


class IntegratedStoreBaseView(HomeAssistantView):
    """Shared plumbing: admin check and JSON body parsing."""

    requires_auth = True

    def __init__(self, hass: HomeAssistant) -> None:
        """Keep a hass reference; the manager is looked up per request."""
        self.hass = hass

    @property
    def manager(self) -> IntegratedStoreManager:
        """The live manager.

        Looked up per request rather than captured at construction: HA cannot
        unregister HTTP views, so these outlive a config entry reload and must
        not pin a stale manager.
        """
        manager = self.hass.data.get(DOMAIN, {}).get(DATA_MANAGER)
        if manager is None:
            raise IntegratedStoreError("IntegratedStore is not set up.")
        return manager

    @staticmethod
    def _require_admin(request: web.Request) -> None:
        """Reject non-admin users.

        Installing code into the config directory is an administrative action;
        `requires_auth` alone would let any logged-in user do it.
        """
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            raise Unauthorized(
                permission="integrated_store", user_id=getattr(user, "id", None)
            )

    @staticmethod
    async def _body(request: web.Request, schema: vol.Schema) -> dict[str, Any]:
        """Parse and validate the JSON request body."""
        try:
            data = await request.json()
        except ValueError as err:
            raise ValidationError("Request body must be JSON.") from err
        if not isinstance(data, dict):
            raise ValidationError("Request body must be a JSON object.")
        return schema(data)


class IntegratedStorePackagesView(IntegratedStoreBaseView):
    """GET the full package list with install/update status."""

    url = f"/api/{DOMAIN}/packages"
    name = f"api:{DOMAIN}:packages"

    @handle_errors
    async def get(self, request: web.Request) -> web.Response:
        """Return every package plus store-level state."""
        self._require_admin(request)
        return self.json(await self.manager.async_get_state())


class IntegratedStoreInstallView(IntegratedStoreBaseView):
    """POST to install a package."""

    url = f"/api/{DOMAIN}/install"
    name = f"api:{DOMAIN}:install"

    @handle_errors
    async def post(self, request: web.Request) -> web.Response:
        """Install a package, optionally pinning a version."""
        self._require_admin(request)
        data = await self._body(request, PACKAGE_ID_SCHEMA)
        result = await self.manager.async_install(data["id"], data.get("version"))
        return self.json(result)


class IntegratedStoreUpdateView(IntegratedStoreBaseView):
    """POST to update an installed package."""

    url = f"/api/{DOMAIN}/update"
    name = f"api:{DOMAIN}:update"

    @handle_errors
    async def post(self, request: web.Request) -> web.Response:
        """Update a package to the latest (or a given) version."""
        self._require_admin(request)
        data = await self._body(request, PACKAGE_ID_SCHEMA)
        result = await self.manager.async_update(data["id"], data.get("version"))
        return self.json(result)


class IntegratedStoreUninstallView(IntegratedStoreBaseView):
    """POST to uninstall a package."""

    url = f"/api/{DOMAIN}/uninstall"
    name = f"api:{DOMAIN}:uninstall"

    @handle_errors
    async def post(self, request: web.Request) -> web.Response:
        """Remove a package's files and resources."""
        self._require_admin(request)
        data = await self._body(request, PACKAGE_ID_SCHEMA)
        result = await self.manager.async_uninstall(data["id"])
        return self.json(result)


class IntegratedStoreAddCustomRepoView(IntegratedStoreBaseView):
    """POST to add a custom repository to the store."""

    url = f"/api/{DOMAIN}/add_custom_repo"
    name = f"api:{DOMAIN}:add_custom_repo"

    @handle_errors
    async def post(self, request: web.Request) -> web.Response:
        """Validate and add a custom repository."""
        self._require_admin(request)
        data = await self._body(request, CUSTOM_REPO_SCHEMA)
        result = await self.manager.async_add_custom_repo(data)
        return self.json(result)


class IntegratedStoreRemoveCustomRepoView(IntegratedStoreBaseView):
    """POST to forget a custom repository."""

    url = f"/api/{DOMAIN}/remove_custom_repo"
    name = f"api:{DOMAIN}:remove_custom_repo"

    @handle_errors
    async def post(self, request: web.Request) -> web.Response:
        """Remove a custom repository (uninstall it first if installed)."""
        self._require_admin(request)
        data = await self._body(request, vol.Schema({vol.Required("id"): cv.string}))
        result = await self.manager.async_remove_custom_repo(data["id"])
        return self.json(result)


class IntegratedStoreReadmeView(IntegratedStoreBaseView):
    """GET a package's README as markdown."""

    url = f"/api/{DOMAIN}/readme"
    name = f"api:{DOMAIN}:readme"

    @handle_errors
    async def get(self, request: web.Request) -> web.Response:
        """Fetch and return the README for ?id=<package_id>."""
        self._require_admin(request)
        package_id = request.query.get("id")
        if not package_id:
            raise ValidationError("Missing 'id' query parameter.")
        result = await self.manager.async_get_readme(package_id)
        return self.json(result)


class IntegratedStoreRefreshView(IntegratedStoreBaseView):
    """POST to force a version re-check."""

    url = f"/api/{DOMAIN}/refresh"
    name = f"api:{DOMAIN}:refresh"

    @handle_errors
    async def post(self, request: web.Request) -> web.Response:
        """Re-read the catalog and re-poll every source."""
        self._require_admin(request)
        await self.manager.async_refresh(force=True)
        return self.json(await self.manager.async_get_state())


VIEWS: tuple[type[IntegratedStoreBaseView], ...] = (
    IntegratedStorePackagesView,
    IntegratedStoreInstallView,
    IntegratedStoreUpdateView,
    IntegratedStoreUninstallView,
    IntegratedStoreAddCustomRepoView,
    IntegratedStoreRemoveCustomRepoView,
    IntegratedStoreReadmeView,
    IntegratedStoreRefreshView,
)


def async_register_views(hass: HomeAssistant) -> None:
    """Register every IntegratedStore HTTP view. Idempotent across entry reloads."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(DATA_VIEWS_REGISTERED):
        return

    for view in VIEWS:
        hass.http.register_view(view(hass))

    domain_data[DATA_VIEWS_REGISTERED] = True
    _LOGGER.debug("Registered %s HTTP view(s)", len(VIEWS))
