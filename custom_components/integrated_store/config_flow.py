"""Config and options flow for IntegratedStore.

Setup asks for nothing mandatory: the store works out of the box against public
sources. Credentials are optional and can be added or changed later through the
options flow. Everything entered here is stored on the config entry, which is
the only place tokens ever live.
"""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_GIT_BASE_URL,
    CONF_GIT_FLAVOR,
    CONF_GIT_TOKEN,
    CONF_GITHUB_TOKEN,
    DOMAIN,
    GIT_FLAVOR_GITEA,
    GIT_FLAVORS,
    NAME,
)

_LOGGER = logging.getLogger(__package__)

#: Typed into a token field to delete the stored token, since a blank field
#: has to mean "leave it alone" (we never send the secret back to the browser).
CLEAR_TOKEN = "-"

PASSWORD_FIELD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
URL_FIELD = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
FLAVOR_FIELD = SelectSelector(
    SelectSelectorConfig(options=GIT_FLAVORS, mode=SelectSelectorMode.DROPDOWN)
)


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the credentials form, pre-filled with current values.

    Tokens are deliberately *not* pre-filled — the stored value is never sent
    back to the browser. An empty token field means "leave it as it is".
    """
    return vol.Schema(
        {
            vol.Optional(CONF_GITHUB_TOKEN): PASSWORD_FIELD,
            vol.Optional(
                CONF_GIT_BASE_URL,
                description={"suggested_value": defaults.get(CONF_GIT_BASE_URL, "")},
            ): URL_FIELD,
            vol.Optional(CONF_GIT_TOKEN): PASSWORD_FIELD,
            vol.Optional(
                CONF_GIT_FLAVOR,
                default=defaults.get(CONF_GIT_FLAVOR, GIT_FLAVOR_GITEA),
            ): FLAVOR_FIELD,
        }
    )


def _clean(user_input: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Normalise submitted options, preserving unchanged secrets."""
    options = dict(previous)

    for key in (CONF_GITHUB_TOKEN, CONF_GIT_TOKEN):
        value = (user_input.get(key) or "").strip()
        if value == CLEAR_TOKEN:
            options.pop(key, None)
        elif value:
            options[key] = value
        # Left blank: keep whatever is already stored. The field is never
        # pre-filled with the secret, so blank has to mean "unchanged".

    base_url = (user_input.get(CONF_GIT_BASE_URL) or "").strip().rstrip("/")
    if base_url:
        options[CONF_GIT_BASE_URL] = base_url
    else:
        options.pop(CONF_GIT_BASE_URL, None)

    flavor = user_input.get(CONF_GIT_FLAVOR)
    if flavor in GIT_FLAVORS:
        options[CONF_GIT_FLAVOR] = flavor

    return options


def _validate(options: dict[str, Any]) -> dict[str, str]:
    """Return a mapping of field -> error key for anything invalid."""
    errors: dict[str, str] = {}

    base_url = options.get(CONF_GIT_BASE_URL)
    if base_url and not str(base_url).startswith(("http://", "https://")):
        errors[CONF_GIT_BASE_URL] = "invalid_url"

    if options.get(CONF_GIT_TOKEN) and not base_url:
        errors[CONF_GIT_BASE_URL] = "token_without_url"

    return errors


class IntegratedStoreConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect optional credentials and create the entry."""
        # One store per Home Assistant instance; a second would fight over the
        # same panel URL and the same files on disk.
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")

        errors: dict[str, str] = {}

        if user_input is not None:
            options = _clean(user_input, {})
            errors = _validate(options)
            if not errors:
                return self.async_create_entry(title=NAME, data={}, options=options)

        return self.async_show_form(
            step_id="user", data_schema=_schema({}), errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> IntegratedStoreOptionsFlow:
        """Return the options flow."""
        return IntegratedStoreOptionsFlow()


class IntegratedStoreOptionsFlow(OptionsFlow):
    """Edit credentials after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the credentials form."""
        current = dict(self.config_entry.options)
        errors: dict[str, str] = {}

        if user_input is not None:
            options = _clean(user_input, current)
            errors = _validate(options)
            if not errors:
                _LOGGER.debug("Updating IntegratedStore options")
                return self.async_create_entry(title="", data=options)

        return self.async_show_form(
            step_id="init",
            data_schema=_schema(current),
            errors=errors,
            description_placeholders={
                "github_token_set": "yes" if current.get(CONF_GITHUB_TOKEN) else "no",
                "git_token_set": "yes" if current.get(CONF_GIT_TOKEN) else "no",
            },
        )
