# IntegratedStore

A HACS-style in-app store for Home Assistant, with your own curated catalog and
support for sources beyond public GitHub.

## What it does

* Adds an **IntegratedStore** entry to the Home Assistant sidebar (admin only).
* Ships a **curated catalog** (`custom_components/integrated_store/catalog.json`) — your
  packages, not the community default list.
* Installs from **four kinds of source**:

  | Type       | For                                             | Auth                          |
  | ---------- | ----------------------------------------------- | ----------------------------- |
  | `github`   | Public *and private* GitHub repositories        | Personal access token         |
  | `git_http` | Self-hosted Gitea, Forgejo or GitLab            | Server token                  |
  | `http_zip` | Any zip URL, with a sidecar JSON for versioning | Optional bearer token         |
  | `local`    | Directories on the Home Assistant host          | —                             |

* Installs three **categories** to the right place:

  | Category      | Installs to                        | Extra                                |
  | ------------- | ---------------------------------- | ------------------------------------ |
  | `integration` | `custom_components/<domain>/`      | Flags that a restart is required     |
  | `lovelace`    | `www/community/<slug>/`            | Registers the Lovelace resource      |
  | `blueprint`   | `blueprints/<type>/<slug>/`        | Type read from the blueprint YAML    |

* Polls every source every three hours for new versions and surfaces available
  updates in the panel.
* Lets you add **custom repositories** from the panel at runtime.

## Installation

### Via HACS (recommended for bootstrapping)

1. HACS → three-dot menu → **Custom repositories**.
2. Add this repository's URL with category **Integration**.
3. Install **IntegratedStore**, then restart Home Assistant.
4. **Settings → Devices & Services → Add Integration → IntegratedStore**.

### Manually

Copy `custom_components/integrated_store/` into your Home Assistant `config/custom_components/`
directory and restart.

## Configuration

Everything in the config flow is optional — the store works against public
sources with no credentials at all.

| Field                          | Purpose                                                                  |
| ------------------------------ | ------------------------------------------------------------------------ |
| GitHub personal access token   | Private repos, and a 5000/hour rate limit instead of 60/hour.             |
| Self-hosted Git base URL       | e.g. `https://git.example.com`. Used by `git_http` packages without one.  |
| Self-hosted Git token          | Access token for that server.                                            |
| Self-hosted Git type           | `gitea` (also Forgejo) or `gitlab`.                                      |

You can change these later from the integration's **Configure** button. Tokens
are stored on the config entry, never in `catalog.json` and never logged. Because
the stored secret is never sent back to the browser, **leaving a token field
blank keeps the current value**; type a single dash (`-`) to delete one.

For a GitHub PAT, a fine-grained token with *Contents: read-only* on the
repositories you care about is enough.

## Catalog format

`custom_components/integrated_store/catalog.json`:

```json
{
  "version": 1,
  "packages": [
    {
      "id": "unique-slug",
      "name": "Display Name",
      "description": "What it does.",
      "category": "integration",
      "source": { "type": "github", "repo": "owner/name" },
      "domain": "target_domain_for_integrations",
      "icon": "mdi:puzzle"
    }
  ]
}
```

`id` must be unique and is used as the directory name for cards and blueprints.
`domain` is optional but recommended for integrations: when set, an install is
refused if the downloaded `manifest.json` declares a different domain, which
stops a mis-pointed catalog entry from overwriting an unrelated integration.
`icon` is either an `mdi:` name or an `https://` URL.

### Source blocks

```jsonc
// GitHub — public or private
{ "type": "github", "repo": "owner/name" }

// Self-hosted Gitea / Forgejo / GitLab
{
  "type": "git_http",
  "base_url": "https://git.mydomain.com",  // optional, falls back to options
  "repo": "me/thing",
  "flavor": "gitea"                        // or "gitlab"; optional
}

// Plain zip. {version} in the URL is substituted.
{
  "type": "http_zip",
  "url": "https://cdn.example.com/thing/{version}.zip",
  "version_url": "https://cdn.example.com/thing/version.json"
}

// Local directory (must be inside the config dir or allowlist_external_dirs)
{ "type": "local", "path": "/config/private_packages/thing" }
```

The `http_zip` sidecar is a small JSON document:

```json
{ "version": "1.4.2", "url": "https://cdn.example.com/thing/1.4.2.zip" }
```

`url` is optional; without it the package's own `url` is used with `{version}`
substituted. Without a `version_url` at all, the package installs once and never
reports an update.

Version resolution for Git sources is: latest release tag → newest tag → default
branch name. Packages that never cut releases still work; they just track the
branch, and "is there an update" degrades to "has it changed".

## HTTP API

All endpoints require an authenticated **admin** user. The panel calls them
through `hass.callApi`, so it never handles tokens itself.

| Method | Path                              | Body                                   |
| ------ | --------------------------------- | -------------------------------------- |
| GET    | `/api/integrated_store/packages`           | —                                      |
| POST   | `/api/integrated_store/install`            | `{"id": "...", "version": "..."}`      |
| POST   | `/api/integrated_store/update`             | `{"id": "...", "version": "..."}`      |
| POST   | `/api/integrated_store/uninstall`          | `{"id": "..."}`                        |
| POST   | `/api/integrated_store/add_custom_repo`    | a catalog entry                        |
| POST   | `/api/integrated_store/remove_custom_repo` | `{"id": "..."}`                        |
| GET    | `/api/integrated_store/readme?id=...`      | —                                      |
| POST   | `/api/integrated_store/refresh`            | `{}`                                   |

`version` is optional and defaults to the newest available. Errors come back as
`{"error": "..."}` with a meaningful status code (400 invalid, 404 unknown, 502
source unreachable, 500 install failure).

## Safety properties

These are deliberate, and worth keeping if you modify the code:

* **Nothing is written outside the config directory.** Every install path is
  resolved and checked with `os.path.commonpath` against the config dir.
  Uninstall additionally refuses any path that is not strictly below
  `custom_components/`, `www/community/` or `blueprints/`.
* **Zip-slip protection.** Archive members are validated *before* extraction:
  absolute paths, drive letters, UNC paths, `..` segments and symlinks all abort
  the whole install. Extraction writes member-by-member rather than using
  `extractall`, so the checks are authoritative.
* **Size caps.** 100 MB per download, 400 MB uncompressed, enforced while
  streaming so a zip bomb never reaches the disk.
* **Atomic-ish installs.** Content is staged in a temporary directory *inside the
  target's parent* (same filesystem), the previous version is renamed aside, the
  staged copy is renamed into place, and only then is the backup deleted. A
  failure at any point restores the backup.
* **No blocking I/O on the event loop.** Zip extraction, file copies and YAML
  parsing all run via `async_add_executor_job`; all network I/O uses the shared
  aiohttp session.
* **One mutation at a time.** Install, update and uninstall are serialised behind
  a lock, so two panel clicks cannot race onto the same directory.
* **Tokens stay put.** They live on the config entry, are injected into sources
  at call time, and are excluded from every API response and log line.

## Development

The panel is vanilla JavaScript with no build step. `panel.js` defines the
`<integrated-store-panel>` custom element that Home Assistant loads; edit it and hard
refresh.

`panel/index.html` is a standalone harness for iterating on the layout without
reloading Home Assistant. With Home Assistant running, open:

```
http://<your-ha>:8123/integrated_store_static/index.html
```

Leave the token field blank for sample data, or paste a long-lived access token
to drive the real API.

To lint locally:

```bash
ruff check custom_components/integrated_store
```

## Renaming

When you pick a real name, change all of these together:

1. `custom_components/integrated_store/` → `custom_components/<newdomain>/`
2. `domain` in `manifest.json`
3. `DOMAIN`, `NAME`, `STATIC_URL_BASE`, `LOVELACE_RESOURCE_BASE` and
   `PANEL_ELEMENT` in `const.py`
4. The `integrated-store-panel` element name in `panel/panel.js` (must match
   `PANEL_ELEMENT`)
5. The `integrated_store/` prefix in `panel.js`'s `_call` helper
6. `name` in `hacs.json` and the paths in `.github/workflows/lint.yml`

Existing installs will not migrate automatically: the storage key changes with
the domain, so uninstall your packages first or migrate
`.storage/integrated_store.data` by hand.

## Known limitations

* Home Assistant cannot unregister HTTP views or static paths, so reloading the
  config entry leaves those registered. The views resolve the manager per
  request and fail cleanly while it is absent.
* If your Lovelace resources are managed in YAML, the resource list is read-only.
  Card installs still copy the files and the panel tells you the exact line to
  add to your dashboard config.
* Uninstalling an integration removes its files, but Home Assistant keeps the
  module loaded until you restart.

## Credits

Built with reference to [HACS](https://github.com/hacs/integration) (MIT
licensed) for panel registration, static path handling and config flow structure.
See [`NOTICE`](NOTICE) for details. No HACS source code was copied verbatim.
