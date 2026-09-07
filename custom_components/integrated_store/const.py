"""Constants for the IntegratedStore integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "integrated_store"
NAME: Final = "IntegratedStore"

# Keep in sync with manifest.json; used to cache-bust the panel bundle.
VERSION: Final = "0.1.0"

# --- Storage -----------------------------------------------------------------

STORAGE_VERSION: Final = 1
STORAGE_KEY: Final = f"{DOMAIN}.data"

# Keys inside hass.data[DOMAIN].
DATA_MANAGER: Final = "manager"
DATA_VIEWS_REGISTERED: Final = "views_registered"
DATA_STATIC_REGISTERED: Final = "static_registered"
# Set the first time async_setup_entry runs after a process start. hass.data
# is a fresh dict on every real Home Assistant restart but survives a plain
# config-entry reload, which is what lets us tell the two apart.
DATA_BOOT_SEEN: Final = "boot_seen"

# homeassistant.helpers.issue_registry issue id for the pending-restart Repair.
RESTART_ISSUE_ID: Final = "restart_required"

# Keys inside the persisted store payload.
STORE_INSTALLED: Final = "installed"
STORE_CUSTOM_REPOS: Final = "custom_repos"
STORE_RESTART_REQUIRED: Final = "restart_required"

# --- Config entry / options keys ---------------------------------------------

CONF_GITHUB_TOKEN: Final = "github_token"
CONF_GIT_BASE_URL: Final = "git_base_url"
CONF_GIT_TOKEN: Final = "git_token"
CONF_GIT_FLAVOR: Final = "git_flavor"

GIT_FLAVOR_GITEA: Final = "gitea"
GIT_FLAVOR_GITLAB: Final = "gitlab"
GIT_FLAVORS: Final = [GIT_FLAVOR_GITEA, GIT_FLAVOR_GITLAB]

# --- Categories --------------------------------------------------------------

CATEGORY_INTEGRATION: Final = "integration"
CATEGORY_LOVELACE: Final = "lovelace"
CATEGORY_BLUEPRINT: Final = "blueprint"

CATEGORIES: Final = [CATEGORY_INTEGRATION, CATEGORY_LOVELACE, CATEGORY_BLUEPRINT]

# Categories whose installation only takes effect after a Home Assistant restart.
RESTART_REQUIRED_CATEGORIES: Final = {CATEGORY_INTEGRATION}

# --- Source types ------------------------------------------------------------

SOURCE_GITHUB: Final = "github"
SOURCE_GIT_HTTP: Final = "git_http"
SOURCE_HTTP_ZIP: Final = "http_zip"
SOURCE_LOCAL: Final = "local"

SOURCE_TYPES: Final = [SOURCE_GITHUB, SOURCE_GIT_HTTP, SOURCE_HTTP_ZIP, SOURCE_LOCAL]

# --- Filesystem layout (all relative to the HA config dir) -------------------

PATH_CUSTOM_COMPONENTS: Final = "custom_components"
PATH_WWW_COMMUNITY: Final = "www/community"
PATH_BLUEPRINTS: Final = "blueprints"

# URL the www/community directory is served from. We mount it ourselves rather
# than relying on Home Assistant's /local mapping: /local only exists if `www/`
# was present at startup, so a first-ever card install would 404 until restart.
LOVELACE_RESOURCE_BASE: Final = "/integrated_store_files"

# --- Frontend ----------------------------------------------------------------

PANEL_URL_PATH: Final = DOMAIN
PANEL_TITLE: Final = NAME
PANEL_ICON: Final = "mdi:storefront-outline"
PANEL_ELEMENT: Final = "integrated-store-panel"

# Static mount for our own panel assets.
STATIC_URL_BASE: Final = "/integrated_store_static"
PANEL_MODULE_URL: Final = f"{STATIC_URL_BASE}/panel.js"

# --- API ---------------------------------------------------------------------

API_BASE: Final = f"/api/{DOMAIN}"

# --- Coordinator -------------------------------------------------------------

UPDATE_INTERVAL: Final = timedelta(hours=3)

# --- Misc --------------------------------------------------------------------

CATALOG_FILENAME: Final = "catalog.json"

# Temporary/backup prefixes used during atomic installs. Anything matching these
# is ours and safe to remove.
TMP_PREFIX: Final = ".integrated_store_tmp_"
BAK_PREFIX: Final = ".integrated_store_bak_"

# Cap on downloaded archive size (bytes) and on the uncompressed size of an
# archive, to keep a hostile or broken source from filling the disk.
MAX_DOWNLOAD_BYTES: Final = 100 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES: Final = 400 * 1024 * 1024
