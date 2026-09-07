"""Plain HTTP zip source.

For anything that just publishes a zip somewhere: a CI artifact, an S3 bucket,
a web server. Version discovery is out of band, via a small sidecar JSON:

    { "version": "1.4.2", "url": "https://.../thing-1.4.2.zip" }

`url` in the sidecar is optional; without it the package's own `url` is used,
with `{version}` substituted if present.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..const import CONF_GIT_TOKEN, SOURCE_HTTP_ZIP
from ..exceptions import SourceError, ValidationError
from . import PackageMetadata, Source

_LOGGER = logging.getLogger(__package__)


class HttpZipSource(Source):
    """A package distributed as a zip at a fixed URL."""

    type = SOURCE_HTTP_ZIP

    def __init__(self, hass: Any, config: dict[str, Any], options: Any = None) -> None:
        """Validate the URLs in the catalog block."""
        super().__init__(hass, config, options)

        url = self.config.get("url")
        if not url or not str(url).startswith(("http://", "https://")):
            raise ValidationError(
                "http_zip sources need a 'url' starting with http:// or https://."
            )
        self.url = str(url)

        version_url = self.config.get("version_url")
        if version_url and not str(version_url).startswith(("http://", "https://")):
            raise ValidationError("'version_url' must start with http:// or https://.")
        self.version_url: str | None = str(version_url) if version_url else None

        self._sidecar: dict[str, Any] | None = None

    @property
    def _headers(self) -> dict[str, str]:
        """Optional bearer auth, reusing the self-hosted Git token if set.

        A private artifact server usually sits behind the same credentials as
        the private Git server, and this saves a second field in the UI.
        """
        headers: dict[str, str] = {}
        if token := self.config.get("token") or self.options.get(CONF_GIT_TOKEN):
            if self.config.get("send_token", False) or self.config.get("token"):
                headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _async_sidecar(self) -> dict[str, Any]:
        """Fetch and cache the sidecar JSON, if one is configured."""
        if self._sidecar is None:
            if not self.version_url:
                self._sidecar = {}
            else:
                data = await self._api_json(
                    self.version_url, self._headers, allow_404=True
                )
                if data is None:
                    raise SourceError(f"Version file not found at {self.version_url}.")
                if not isinstance(data, dict):
                    raise ValidationError(
                        "The version file must contain a JSON object with a "
                        "'version' key."
                    )
                self._sidecar = data
        return self._sidecar

    async def get_metadata(self) -> PackageMetadata:
        """Return whatever the sidecar tells us, falling back to the catalog."""
        sidecar = await self._async_sidecar()
        return PackageMetadata(
            name=sidecar.get("name") or self.config.get("name") or "Package",
            description=sidecar.get("description", ""),
            latest_version=await self.get_latest_version(),
            homepage=sidecar.get("homepage"),
        )

    async def get_latest_version(self) -> str | None:
        """Version from the sidecar.

        Without a sidecar there is nothing to compare against, so the package
        reports a fixed version and simply never shows as having an update.
        """
        if not self.version_url:
            return str(self.config.get("version") or "latest")

        sidecar = await self._async_sidecar()
        version = sidecar.get("version")
        if not version:
            raise ValidationError(
                f"The version file at {self.version_url} has no 'version' key."
            )
        return str(version)

    def _download_url(self, version: str, sidecar: dict[str, Any]) -> str:
        """Pick the zip URL for a version."""
        if url := sidecar.get("url"):
            if not str(url).startswith(("http://", "https://")):
                raise ValidationError("The 'url' in the version file must be http(s).")
            return str(url)
        return self.url.replace("{version}", version)

    async def get_readme(self) -> str | None:
        """Fetch the README from an optional `readme_url` in the sidecar."""
        sidecar = await self._async_sidecar()
        readme_url = sidecar.get("readme_url")
        if not readme_url or not str(readme_url).startswith(("http://", "https://")):
            return None
        return await self._api_text(str(readme_url), self._headers)

    async def download(self, version: str) -> Path:
        """Download and extract the zip for a version."""
        sidecar = await self._async_sidecar()
        url = self._download_url(version, sidecar)

        _LOGGER.debug("Downloading %s from %s", version, url)
        return await self._download_and_extract(url, self._headers)
