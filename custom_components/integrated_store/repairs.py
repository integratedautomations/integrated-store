"""Repairs for IntegratedStore.

Currently backs exactly one issue: RESTART_ISSUE_ID. Without this module, HA
would fall back to the built-in ConfirmRepairFlow for any issue marked
`is_fixable=True` — that only clears the issue on confirm, it doesn't perform
the restart itself, which would leave the user thinking they'd restarted when
nothing actually happened.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant import data_entry_flow
from homeassistant.components.repairs import ConfirmRepairFlow, RepairsFlow
from homeassistant.core import HomeAssistant

from .const import RESTART_ISSUE_ID


class _RestartRequiredFlow(RepairsFlow):
    """Confirm, then actually call homeassistant.restart."""

    async def async_step_init(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Start at the confirmation step."""
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, str] | None = None
    ) -> data_entry_flow.FlowResult:
        """Show a confirmation, then trigger the restart when accepted."""
        if user_input is not None:
            # Not blocking: the restart tears down this very websocket
            # connection, so awaiting its completion would just hang.
            await self.hass.services.async_call(
                "homeassistant", "restart", blocking=False
            )
            return self.async_create_entry(data={})

        return self.async_show_form(step_id="confirm", data_schema=vol.Schema({}))


async def async_create_fix_flow(
    hass: HomeAssistant,
    issue_id: str,
    data: dict[str, str | int | float | None] | None,
) -> RepairsFlow:
    """Return the fix flow for an IntegratedStore Repairs issue."""
    if issue_id == RESTART_ISSUE_ID:
        return _RestartRequiredFlow()
    # No other fixable issue exists today; fall back rather than raise so a
    # future issue type added without updating this dispatch fails softly.
    return ConfirmRepairFlow()
