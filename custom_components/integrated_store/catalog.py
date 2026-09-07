"""Loading and merging the package catalog.

The curated catalog ships as `catalog.json` next to this module. User-added
custom repositories are merged on top of it at runtime, so both kinds of package
flow through exactly the same install path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

from .const import CATALOG_FILENAME, CATEGORIES, CATEGORY_INTEGRATION, SOURCE_TYPES
from .exceptions import ValidationError
from .store import CustomRepo, IntegratedStoreStore

_LOGGER = logging.getLogger(__package__)


@dataclass
class CatalogPackage:
    """One installable package, from the curated catalog or a custom repo."""

    id: str
    name: str
    category: str
    source: dict[str, Any]
    description: str = ""
    domain: str | None = None
    icon: str | None = None
    brand_icon: str | None = None
    custom: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, custom: bool = False) -> CatalogPackage:
        """Build from a catalog entry, validating the required shape."""
        package_id = data.get("id")
        if not package_id or not isinstance(package_id, str):
            raise ValidationError("Every package needs a string 'id'.")

        category = data.get("category")
        if category not in CATEGORIES:
            raise ValidationError(
                f"Package '{package_id}' has category '{category}'; expected one "
                f"of {CATEGORIES}."
            )

        source = data.get("source")
        if not isinstance(source, dict) or source.get("type") not in SOURCE_TYPES:
            raise ValidationError(
                f"Package '{package_id}' has an invalid 'source'. 'type' must be "
                f"one of {SOURCE_TYPES}."
            )

        domain = data.get("domain")

        return cls(
            id=package_id,
            name=data.get("name") or package_id,
            category=category,
            source=source,
            description=data.get("description", ""),
            domain=domain,
            icon=data.get("icon") or _default_icon(source),
            brand_icon=_brand_icon(category, domain),
            custom=custom,
        )

    @classmethod
    def from_custom_repo(cls, repo: CustomRepo) -> CatalogPackage:
        """Build from a stored custom repository."""
        return cls.from_dict(repo.to_dict(), custom=True)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for the HTTP API. Never includes credentials."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "domain": self.domain,
            "icon": self.icon,
            "brand_icon": self.brand_icon,
            "custom": self.custom,
            "source_type": self.source.get("type"),
            "repo_url": repo_url_for(self.source),
        }


def _brand_icon(category: str, domain: str | None) -> str | None:
    """URL of the package's icon in Home Assistant's brands database, if any.

    Not verified to exist: brands.home-assistant.io only covers integrations
    that went through a PR to home-assistant/brands, so most niche or private
    ones will 404. The panel tries this first and falls back to `icon` on
    error, rather than this module making an extra network request per
    package just to find out — a broken <img> retried once client-side is
    cheap; blocking catalog load on ~25 HTTP calls is not.
    """
    if category != CATEGORY_INTEGRATION or not domain:
        return None
    return f"https://brands.home-assistant.io/{domain}/icon.png"


def repo_url_for(source: dict[str, Any]) -> str | None:
    """A browsable homepage URL for the source, if one can be derived for free.

    Only covers `github` (always resolvable) and `git_http` when the package
    itself declares a `base_url` (a global base URL lives in the config entry,
    which this module — the catalog — never sees). `http_zip` and `local`
    sources have no natural webpage to link to.
    """
    source_type = source.get("type")
    repo = source.get("repo")
    if not isinstance(repo, str) or not repo:
        return None

    if source_type == "github":
        return f"https://github.com/{repo}"
    if source_type == "git_http":
        base_url = source.get("base_url")
        if isinstance(base_url, str) and base_url:
            return f"{base_url.rstrip('/')}/{repo}"
    return None


def _default_icon(source: dict[str, Any]) -> str | None:
    """Best-effort icon when the catalog entry doesn't specify one.

    Uses the repo owner's GitHub avatar rather than a repo-specific logo: there
    is no standard place a repo declares an icon (unlike the curated
    home-assistant/brands database HACS itself relies on, which this project
    deliberately doesn't depend on), so guessing at file paths like `icon.png`
    would silently 404 for most repos. The owner avatar is always present at a
    fixed URL and needs no extra request to look up, at the cost of being
    account-level branding rather than project-level. Falls back to the
    built-in category icon (handled client-side) for every other source type.
    """
    if source.get("type") != "github":
        return None
    repo = source.get("repo")
    if not isinstance(repo, str) or "/" not in repo:
        return None
    owner = repo.split("/", 1)[0].strip()
    if not owner:
        return None
    return f"https://github.com/{owner}.png"


def _read_catalog_file(path: Path) -> dict[str, Any]:
    """Read and parse catalog.json. Blocking."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        _LOGGER.warning(
            "No %s found at %s, starting with an empty catalog", CATALOG_FILENAME, path
        )
        return {"version": 1, "packages": []}
    except (OSError, ValueError) as err:
        raise ValidationError(f"Could not read {CATALOG_FILENAME}: {err}") from err


async def async_load_catalog(
    hass: HomeAssistant, store: IntegratedStoreStore
) -> dict[str, CatalogPackage]:
    """Return every known package, keyed by id.

    Malformed entries are logged and skipped rather than failing the whole
    catalog: one bad entry should not take down the store.
    """
    catalog_path = Path(__file__).parent / CATALOG_FILENAME
    raw = await hass.async_add_executor_job(_read_catalog_file, catalog_path)

    packages: dict[str, CatalogPackage] = {}

    for entry in raw.get("packages", []):
        try:
            package = CatalogPackage.from_dict(entry)
        except ValidationError as err:
            _LOGGER.error("Skipping catalog entry: %s", err)
            continue
        packages[package.id] = package

    # Custom repos win on id collision: the user's explicit choice beats ours.
    for repo in store.custom_repos.values():
        try:
            package = CatalogPackage.from_custom_repo(repo)
        except ValidationError as err:
            _LOGGER.error("Skipping custom repository '%s': %s", repo.id, err)
            continue
        packages[package.id] = package

    _LOGGER.debug("Catalog loaded with %s package(s)", len(packages))
    return packages
