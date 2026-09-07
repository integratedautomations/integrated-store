"""Exceptions for the IntegratedStore integration.

Every error raised inside IntegratedStore that a user could plausibly need to see
carries a human-readable message, so the HTTP API can hand it straight to the
panel instead of failing silently.
"""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class IntegratedStoreError(HomeAssistantError):
    """Base error. The string form is shown to the user in the panel."""


class SourceError(IntegratedStoreError):
    """A package source could not be reached, authenticated to, or understood."""


class NotFoundError(IntegratedStoreError):
    """The requested package, version or file does not exist."""


class ValidationError(IntegratedStoreError):
    """Downloaded content failed validation and was not installed."""


class InstallError(IntegratedStoreError):
    """Installing or removing files on disk failed."""
