"""Self-hosted Git source: Gitea/Forgejo and GitLab.

Both expose a releases list, a tags list and a zip archive endpoint, but at
different paths and with different auth headers, so the flavour is selected
per package (defaulting to the integration-wide setting, then to Gitea).
"""

from __future__ import annotations

import base64
import binascii
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ..const import (
    CONF_GIT_BASE_URL,
    CONF_GIT_FLAVOR,
    CONF_GIT_TOKEN,
    GIT_FLAVOR_GITEA,
    GIT_FLAVOR_GITLAB,
    GIT_FLAVORS,
    SOURCE_GIT_HTTP,
)
from ..exceptions import NotFoundError, SourceError, ValidationError
from . import PackageMetadata, Source

_LOGGER = logging.getLogger(__package__)

README_CANDIDATES = ("README.md", "Readme.md", "README.MD", "readme.md")


class GitHttpSource(Source):
    """A package hosted on a self-hosted Gitea/Forgejo or GitLab instance."""

    type = SOURCE_GIT_HTTP

    def __init__(self, hass: Any, config: dict[str, Any], options: Any = None) -> None:
        """Resolve base URL, repo and flavour, preferring per-package values."""
        super().__init__(hass, config, options)

        base_url = self.config.get("base_url") or self.options.get(CONF_GIT_BASE_URL)
        if not base_url:
            raise ValidationError(
                "This package needs a self-hosted Git base URL. Set one on the "
                "package or in the IntegratedStore options."
            )
        if not str(base_url).startswith(("http://", "https://")):
            raise ValidationError(
                f"Git base URL '{base_url}' must start with http:// or https://."
            )
        self.base_url = str(base_url).rstrip("/")

        repo = self.config.get("repo")
        if not repo or not isinstance(repo, str) or "/" not in repo:
            raise ValidationError(
                "git_http sources need a 'repo' of the form 'owner/name'."
            )
        self.repo = repo.strip("/")

        flavor = (
            self.config.get("flavor")
            or self.options.get(CONF_GIT_FLAVOR)
            or GIT_FLAVOR_GITEA
        )
        if flavor not in GIT_FLAVORS:
            raise ValidationError(
                f"Unknown Git flavour '{flavor}'. Expected one of {GIT_FLAVORS}."
            )
        self.flavor: str = flavor

        self._repo_info: dict[str, Any] | None = None

    # --- flavour plumbing ----------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        """Auth headers; the two forges disagree on the header name."""
        headers = {"Accept": "application/json"}
        if token := self.options.get(CONF_GIT_TOKEN):
            if self.flavor == GIT_FLAVOR_GITLAB:
                headers["PRIVATE-TOKEN"] = token
            else:
                headers["Authorization"] = f"token {token}"
        return headers

    @property
    def _project(self) -> str:
        """The repo identifier as each API wants it."""
        if self.flavor == GIT_FLAVOR_GITLAB:
            # GitLab takes a URL-encoded "group/project" path, slash included.
            return quote(self.repo, safe="")
        return self.repo

    @property
    def _api_base(self) -> str:
        """Base URL of the project's API namespace."""
        if self.flavor == GIT_FLAVOR_GITLAB:
            return f"{self.base_url}/api/v4/projects/{self._project}"
        return f"{self.base_url}/api/v1/repos/{self._project}"

    # --- Source interface ----------------------------------------------------

    async def _async_repo_info(self) -> dict[str, Any]:
        """Fetch and cache the project record."""
        if self._repo_info is None:
            data = await self._api_json(self._api_base, self._headers, allow_404=True)
            if data is None:
                raise NotFoundError(
                    f"Repository '{self.repo}' was not found on {self.base_url}. "
                    "If it is private, check the token in the IntegratedStore options."
                )
            self._repo_info = data
        return self._repo_info

    async def get_metadata(self) -> PackageMetadata:
        """Return project description, web URL and newest version."""
        info = await self._async_repo_info()
        return PackageMetadata(
            name=info.get("name") or self.repo.split("/")[-1],
            description=info.get("description") or "",
            latest_version=await self.get_latest_version(),
            homepage=info.get("html_url") or info.get("web_url"),
            extra={
                "flavor": self.flavor,
                "default_branch": info.get("default_branch"),
            },
        )

    async def get_latest_version(self) -> str | None:
        """Newest release tag, else newest tag, else the default branch."""
        limit = "per_page=1" if self.flavor == GIT_FLAVOR_GITLAB else "limit=1"

        releases = await self._api_json(
            f"{self._api_base}/releases?{limit}", self._headers, allow_404=True
        )
        if isinstance(releases, list) and releases:
            if tag := releases[0].get("tag_name"):
                return str(tag)

        tags_path = "repository/tags" if self.flavor == GIT_FLAVOR_GITLAB else "tags"
        tags = await self._api_json(
            f"{self._api_base}/{tags_path}?{limit}", self._headers, allow_404=True
        )
        if isinstance(tags, list) and tags and (name := tags[0].get("name")):
            return str(name)

        info = await self._async_repo_info()
        return info.get("default_branch")

    async def get_readme(self) -> str | None:
        """Fetch the project's README, trying common filenames in order."""
        if self.flavor == GIT_FLAVOR_GITLAB:
            info = await self._async_repo_info()
            ref = info.get("default_branch") or "main"
            for name in README_CANDIDATES:
                text = await self._api_text(
                    f"{self._api_base}/repository/files/"
                    f"{quote(name, safe='')}/raw?ref={quote(ref, safe='')}",
                    self._headers,
                )
                if text is not None:
                    return text
            return None

        for name in README_CANDIDATES:
            data = await self._api_json(
                f"{self._api_base}/contents/{name}", self._headers, allow_404=True
            )
            if data and data.get("encoding") == "base64" and data.get("content"):
                try:
                    return base64.b64decode(data["content"]).decode(
                        "utf-8", errors="replace"
                    )
                except (binascii.Error, ValueError):
                    continue
        return None

    async def download(self, version: str) -> Path:
        """Download the zip archive for a tag, branch or SHA."""
        if not version:
            raise ValidationError("No version to download for this package.")

        if self.flavor == GIT_FLAVOR_GITLAB:
            url = (
                f"{self._api_base}/repository/archive.zip?sha={quote(version, safe='')}"
            )
        else:
            url = f"{self._api_base}/archive/{quote(version, safe='')}.zip"

        _LOGGER.debug("Downloading %s@%s from %s", self.repo, version, self.base_url)
        try:
            return await self._download_and_extract(url, self._headers)
        except SourceError as err:
            raise SourceError(
                f"Could not download {self.repo}@{version} from {self.base_url}: {err}"
            ) from err
