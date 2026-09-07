"""GitHub source: public and private repositories, authenticated with a PAT.

Uses the REST API for metadata/versions and the zipball endpoint for content,
which works identically for public repos, private repos and repos without any
published release.
"""

from __future__ import annotations

import base64
import binascii
import logging
from pathlib import Path
from typing import Any

from ..const import CONF_GITHUB_TOKEN, SOURCE_GITHUB
from ..exceptions import NotFoundError, SourceError, ValidationError
from . import PackageMetadata, Source

_LOGGER = logging.getLogger(__package__)

API_ROOT = "https://api.github.com"


class GitHubSource(Source):
    """A package hosted in a GitHub repository."""

    type = SOURCE_GITHUB

    def __init__(self, hass: Any, config: dict[str, Any], options: Any = None) -> None:
        """Validate the catalog block up front so errors surface early."""
        super().__init__(hass, config, options)

        repo = self.config.get("repo")
        if not repo or not isinstance(repo, str) or repo.count("/") != 1:
            raise ValidationError(
                "GitHub sources need a 'repo' of the form 'owner/name'."
            )
        self.repo = repo.strip("/")
        self._repo_info: dict[str, Any] | None = None

    @property
    def _headers(self) -> dict[str, str]:
        """Request headers, including the PAT when one is configured."""
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token := self.options.get(CONF_GITHUB_TOKEN):
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _async_repo_info(self) -> dict[str, Any]:
        """Fetch and cache the repository record."""
        if self._repo_info is None:
            data = await self._api_json(
                f"{API_ROOT}/repos/{self.repo}", self._headers, allow_404=True
            )
            if data is None:
                raise NotFoundError(
                    f"GitHub repository '{self.repo}' was not found. If it is "
                    "private, add a personal access token in the IntegratedStore options."
                )
            self._repo_info = data
        return self._repo_info

    async def get_metadata(self) -> PackageMetadata:
        """Return repository description, homepage and newest version."""
        info = await self._async_repo_info()
        return PackageMetadata(
            name=info.get("name") or self.repo.split("/")[-1],
            description=info.get("description") or "",
            latest_version=await self.get_latest_version(),
            homepage=info.get("html_url"),
            extra={
                "stars": info.get("stargazers_count"),
                "default_branch": info.get("default_branch"),
                "private": info.get("private", False),
            },
        )

    async def get_latest_version(self) -> str | None:
        """Newest release tag, else newest tag, else the default branch name.

        Falling back to the default branch means packages that never cut
        releases still work; the version string is then the branch name and
        version comparison degrades to "changed / not changed".
        """
        release = await self._api_json(
            f"{API_ROOT}/repos/{self.repo}/releases/latest",
            self._headers,
            allow_404=True,
        )
        if release and (tag := release.get("tag_name")):
            return str(tag)

        tags = await self._api_json(
            f"{API_ROOT}/repos/{self.repo}/tags?per_page=1",
            self._headers,
            allow_404=True,
        )
        if isinstance(tags, list) and tags and (name := tags[0].get("name")):
            return str(name)

        info = await self._async_repo_info()
        return info.get("default_branch")

    async def get_readme(self) -> str | None:
        """Fetch the repository's README via the contents API."""
        data = await self._api_json(
            f"{API_ROOT}/repos/{self.repo}/readme", self._headers, allow_404=True
        )
        if not data or data.get("encoding") != "base64" or not data.get("content"):
            return None
        try:
            return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
        except (binascii.Error, ValueError):
            return None

    async def download(self, version: str) -> Path:
        """Download the zipball for a tag, branch or SHA."""
        if not version:
            raise ValidationError("No version to download for this package.")

        _LOGGER.debug("Downloading %s@%s from GitHub", self.repo, version)
        try:
            return await self._download_and_extract(
                f"{API_ROOT}/repos/{self.repo}/zipball/{version}", self._headers
            )
        except SourceError as err:
            raise SourceError(
                f"Could not download {self.repo}@{version} from GitHub: {err}"
            ) from err
