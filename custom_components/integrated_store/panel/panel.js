/**
 * IntegratedStore panel.
 *
 * Vanilla custom element, no build step. Home Assistant loads this as a
 * `panel_custom` module and assigns `hass`, `narrow`, `route` and `panel`
 * properties on the element. `hass.callApi` signs requests with the logged-in
 * user's token, so the panel never handles credentials itself.
 */

const CATEGORY_LABELS = {
  integration: "Integrations",
  lovelace: "Cards",
  blueprint: "Blueprints",
};

const CATEGORY_ICONS = {
  integration: "mdi:puzzle",
  lovelace: "mdi:view-dashboard",
  blueprint: "mdi:file-document-outline",
};

const SOURCE_LABELS = {
  github: "GitHub",
  git_http: "Self-hosted Git",
  http_zip: "Zip URL",
  local: "Local",
};

// Which other store already put this package on disk, when we can tell.
const CONFLICT_LABELS = {
  hacs: "Installed via HACS",
  yidstore: "Installed via YidStore",
  other: "Already present",
};

const SOURCE_FIELDS = {
  github: [{ key: "repo", label: "Repository (owner/name)", required: true }],
  git_http: [
    { key: "base_url", label: "Base URL (blank to use the configured one)" },
    { key: "repo", label: "Repository (owner/name)", required: true },
    { key: "flavor", label: "Flavour (gitea or gitlab)" },
  ],
  http_zip: [
    { key: "url", label: "Zip URL (may contain {version})", required: true },
    { key: "version_url", label: "Version JSON URL" },
  ],
  local: [{ key: "path", label: "Path inside the config directory", required: true }],
};

const STYLES = `
  :host {
    display: block;
    background: var(--primary-background-color, #f5f5f5);
    color: var(--primary-text-color, #212121);
    min-height: 100vh;
    font-family: var(--paper-font-body1_-_font-family, Roboto, sans-serif);
  }
  header {
    display: flex;
    align-items: center;
    gap: 16px;
    padding: 0 16px;
    height: 64px;
    background: var(--app-header-background-color, var(--primary-color, #03a9f4));
    color: var(--app-header-text-color, #fff);
    box-sizing: border-box;
  }
  header h1 { font-size: 20px; font-weight: 400; margin: 0; flex: 1; }
  .content { padding: 16px; max-width: 1400px; margin: 0 auto; }
  button {
    font: inherit;
    font-size: 14px;
    border: none;
    border-radius: 4px;
    padding: 8px 16px;
    cursor: pointer;
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
  }
  button:disabled { opacity: 0.5; cursor: default; }
  button.ghost, a.ghost {
    background: transparent;
    color: inherit;
    border: 1px solid currentColor;
  }
  a.ghost {
    display: inline-flex;
    align-items: center;
    font: inherit;
    font-size: 14px;
    border-radius: 4px;
    padding: 8px 16px;
    cursor: pointer;
    text-decoration: none;
    box-sizing: border-box;
  }
  button.danger { background: var(--error-color, #db4437); }
  header button.ghost { border-color: rgba(255, 255, 255, 0.6); }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    align-items: center;
    margin-bottom: 16px;
  }
  .chip {
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--card-background-color, #fff);
    color: inherit;
    border-radius: 16px;
    padding: 6px 14px;
  }
  .chip[aria-pressed="true"] {
    background: var(--primary-color, #03a9f4);
    color: var(--text-primary-color, #fff);
    border-color: transparent;
  }
  input[type="search"], input[type="text"], select {
    font: inherit;
    padding: 8px 12px;
    border-radius: 4px;
    border: 1px solid var(--divider-color, #e0e0e0);
    background: var(--card-background-color, #fff);
    color: inherit;
    min-width: 200px;
  }
  label.field { display: block; margin-bottom: 12px; font-size: 13px; }
  label.field span { display: block; margin-bottom: 4px; opacity: 0.75; }
  label.field input, label.field select { width: 100%; box-sizing: border-box; }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
    gap: 16px;
  }
  .card {
    background: var(--card-background-color, #fff);
    border-radius: var(--ha-card-border-radius, 12px);
    box-shadow: var(--ha-card-box-shadow, 0 2px 4px rgba(0,0,0,.12));
    padding: 16px;
    display: flex;
    flex-direction: column;
    gap: 12px;
  }
  .card-head { display: flex; gap: 12px; align-items: flex-start; }
  .card-head img { width: 40px; height: 40px; border-radius: 8px; object-fit: contain; }
  .card h2 { font-size: 16px; margin: 0 0 4px; font-weight: 500; }
  .card p { margin: 0; font-size: 14px; color: var(--secondary-text-color, #727272); }
  .badges { display: flex; flex-wrap: wrap; gap: 6px; }
  .badge {
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: .04em;
    padding: 2px 8px;
    border-radius: 10px;
    background: var(--secondary-background-color, #e0e0e0);
    color: var(--secondary-text-color, #727272);
  }
  .badge.update { background: var(--info-color, #039be5); color: #fff; }
  .badge.installed { background: var(--success-color, #43a047); color: #fff; }
  .badge.error { background: var(--error-color, #db4437); color: #fff; }
  .badge.conflict { background: var(--warning-color, #ffa600); color: #000; }
  .card.clickable { cursor: pointer; }
  .actions { display: flex; gap: 8px; margin-top: auto; }
  .actions button, .actions a { position: relative; z-index: 1; }
  .banner {
    padding: 12px 16px;
    border-radius: 8px;
    margin-bottom: 16px;
    display: flex;
    gap: 12px;
    align-items: center;
  }
  .banner.warn { background: var(--warning-color, #ffa600); color: #000; }
  .banner.error { background: var(--error-color, #db4437); color: #fff; }
  .banner.info { background: var(--info-color, #039be5); color: #fff; }
  .banner button { margin-left: auto; }
  .empty { text-align: center; padding: 48px 16px; color: var(--secondary-text-color, #727272); }
  dialog {
    border: none;
    border-radius: 12px;
    padding: 24px;
    width: min(480px, 92vw);
    background: var(--card-background-color, #fff);
    color: inherit;
  }
  dialog::backdrop { background: rgba(0, 0, 0, .5); }
  dialog h2 { margin-top: 0; font-size: 20px; font-weight: 400; }
  .dialog-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 16px; }
  .spin { animation: spin 1s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  dialog.detail {
    width: min(720px, 92vw);
    max-height: 85vh;
    display: flex;
    flex-direction: column;
    padding: 0;
  }
  dialog.detail .detail-head {
    display: flex;
    gap: 16px;
    align-items: flex-start;
    padding: 24px 24px 0;
  }
  dialog.detail .detail-head img,
  dialog.detail .detail-head ha-icon {
    width: 48px;
    height: 48px;
    border-radius: 10px;
    object-fit: contain;
    --mdc-icon-size: 48px;
    flex-shrink: 0;
  }
  dialog.detail .detail-body {
    padding: 16px 24px;
    overflow-y: auto;
  }
  dialog.detail .dialog-actions { padding: 0 24px 24px; margin-top: 0; }
  dialog.detail .close {
    position: absolute;
    top: 16px;
    right: 16px;
  }
  .readme { font-size: 14px; line-height: 1.6; }
  .readme h1, .readme h2, .readme h3 { font-weight: 500; margin: 20px 0 8px; }
  .readme h1:first-child, .readme h2:first-child, .readme h3:first-child { margin-top: 0; }
  .readme p { margin: 8px 0; }
  .readme a { color: var(--primary-color, #03a9f4); }
  .readme img { max-width: 100%; }
  .readme code {
    font-family: var(--code-font-family, monospace);
    background: var(--secondary-background-color, #eee);
    padding: 1px 5px;
    border-radius: 4px;
    font-size: 13px;
  }
  .readme pre {
    background: var(--secondary-background-color, #eee);
    padding: 12px;
    border-radius: 8px;
    overflow-x: auto;
  }
  .readme pre code { background: none; padding: 0; }
  .readme blockquote {
    border-left: 3px solid var(--divider-color, #e0e0e0);
    margin: 8px 0;
    padding-left: 12px;
    color: var(--secondary-text-color, #727272);
  }
  .readme ul, .readme ol { margin: 8px 0; padding-left: 24px; }
  .readme hr { border: none; border-top: 1px solid var(--divider-color, #e0e0e0); margin: 16px 0; }
`;

class IntegratedStorePanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });

    this._hass = null;
    this._state = null;
    this._loading = true;
    this._error = null;
    this._messages = [];
    this._busy = new Set();
    this._filter = "all";
    this._search = "";
    this._installedOnly = false;
    this._rendered = false;
    // Session-only: avoids re-fetching a README every time the same card is
    // reopened, since it rarely changes between one panel visit and the next.
    this._readmeCache = new Map();
    this._restarting = false;
  }

  /** Home Assistant assigns this whenever its state object changes. */
  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    if (first) {
      this._load();
    }
  }

  get hass() {
    return this._hass;
  }

  connectedCallback() {
    if (!this._rendered) {
      this._renderShell();
      this._rendered = true;
    }
  }

  // --- data ------------------------------------------------------------------

  async _call(method, path, body) {
    try {
      return await this._hass.callApi(method, `integrated_store/${path}`, body);
    } catch (err) {
      // hass.callApi rejects with the parsed body when the server sent JSON.
      const message =
        (err && err.body && err.body.error) ||
        (err && err.error) ||
        (err && err.message) ||
        "Request failed";
      throw new Error(message);
    }
  }

  async _load() {
    this._loading = true;
    this._error = null;
    this._render();
    try {
      this._state = await this._call("get", "packages");
    } catch (err) {
      this._error = err.message;
    } finally {
      this._loading = false;
      this._render();
    }
  }

  async _action(id, endpoint, body) {
    // Captured before the action: the package list is replaced afterwards,
    // and an uninstall removes the entry entirely.
    const before = (this._state ? this._state.packages : []).find(
      (pkg) => pkg.id === id
    );
    let offerReload = false;

    this._busy.add(id);
    this._error = null;
    this._render();
    try {
      const result = await this._call("post", endpoint, body || { id });
      if (result && result.messages && result.messages.length) {
        this._messages = result.messages;
      }
      this._state = await this._call("get", "packages");
      offerReload =
        Boolean(before) &&
        before.category === "lovelace" &&
        (endpoint === "install" || endpoint === "update");
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy.delete(id);
      this._render();
    }

    if (offerReload) {
      // A card's JavaScript is only pulled in when the page loads, so until
      // the frontend reloads it either isn't there at all or is still the
      // previously cached version. Prompt after rendering so the updated
      // card is visible behind the dialog.
      const verb = endpoint === "update" ? "updated" : "installed";
      if (
        window.confirm(
          `${before.name} ${verb}. Reload the page now so the card loads?`
        )
      ) {
        location.reload();
      }
    }
  }

  async _refresh() {
    this._busy.add("__refresh__");
    this._render();
    try {
      this._state = await this._call("post", "refresh", {});
      this._error = null;
    } catch (err) {
      this._error = err.message;
    } finally {
      this._busy.delete("__refresh__");
      this._render();
    }
  }

  async _restartHomeAssistant() {
    if (this._restarting) return;
    if (
      !window.confirm(
        "Restart Home Assistant now? Everything will briefly disconnect."
      )
    ) {
      return;
    }
    this._restarting = true;
    this._render();
    try {
      // Calls the real homeassistant.restart service, the same one the
      // Repairs "Fix" button uses — the panel button is just a shortcut to
      // it from wherever the banner happens to be visible.
      await this._hass.callService("homeassistant", "restart", {});
    } catch (err) {
      this._restarting = false;
      this._error = `Could not restart Home Assistant: ${err.message || err}`;
      this._render();
    }
    // On success there is nothing further to do client-side: the restart
    // itself tears down this connection, and the frontend reconnects once HA
    // is back up.
  }

  // --- rendering -------------------------------------------------------------

  _renderShell() {
    const style = document.createElement("style");
    style.textContent = STYLES;

    const root = document.createElement("div");
    root.id = "root";

    this.shadowRoot.append(style, root);
    this._render();
  }

  _visiblePackages() {
    if (!this._state) return [];
    const search = this._search.trim().toLowerCase();
    return this._state.packages.filter((pkg) => {
      if (this._filter !== "all" && pkg.category !== this._filter) return false;
      if (this._installedOnly && !pkg.installed) return false;
      if (!search) return true;
      return (
        pkg.name.toLowerCase().includes(search) ||
        (pkg.description || "").toLowerCase().includes(search) ||
        pkg.id.toLowerCase().includes(search)
      );
    });
  }

  _render() {
    const root = this.shadowRoot && this.shadowRoot.getElementById("root");
    if (!root) return;

    root.textContent = "";
    root.append(this._header());

    const content = el("div", { class: "content" });

    if (this._error) {
      content.append(
        this._banner("error", this._error, "Dismiss", () => {
          this._error = null;
          this._render();
        })
      );
    }

    if (this._messages.length) {
      content.append(
        this._banner("info", this._messages.join(" "), "Got it", () => {
          this._messages = [];
          this._render();
        })
      );
    }

    if (this._state && this._state.restart_required) {
      content.append(
        this._banner(
          "warn",
          "Restart Home Assistant to finish applying your changes.",
          this._restarting ? "Restarting…" : "Restart Home Assistant",
          () => this._restartHomeAssistant(),
          this._restarting
        )
      );
    }

    if (this._state && this._state.last_update_success === false) {
      content.append(
        this._banner(
          "warn",
          "The last update check failed. Version information may be stale."
        )
      );
    }

    content.append(this._toolbar());

    if (this._state && this._state.hidden_count > 0) {
      const count = this._state.hidden_count;
      content.append(
        el(
          "p",
          { style: "color: var(--secondary-text-color, #727272); font-size: 13px;" },
          `${count} package${count === 1 ? "" : "s"} hidden: repository not found. ` +
            "If any are private, add an access token in the IntegratedStore options."
        )
      );
    }

    if (this._loading) {
      content.append(el("div", { class: "empty" }, "Loading packages…"));
    } else {
      const packages = this._visiblePackages();
      if (!packages.length) {
        content.append(
          el("div", { class: "empty" }, "No packages match your filters.")
        );
      } else {
        const grid = el("div", { class: "grid" });
        packages.forEach((pkg) => grid.append(this._card(pkg)));
        content.append(grid);
      }
    }

    root.append(content);
  }

  _header() {
    const header = el("header");
    header.append(el("h1", {}, "IntegratedStore"));

    const refresh = el(
      "button",
      { class: "ghost" },
      this._busy.has("__refresh__") ? "Checking…" : "Check for updates"
    );
    refresh.disabled = this._busy.has("__refresh__");
    refresh.addEventListener("click", () => this._refresh());

    const add = el("button", { class: "ghost" }, "Add custom repository");
    add.addEventListener("click", () => this._openAddDialog());

    header.append(refresh, add);
    return header;
  }

  _toolbar() {
    const toolbar = el("div", { class: "toolbar" });

    const categories = ["all", ...(this._state ? this._state.categories : [])];
    categories.forEach((category) => {
      const chip = el(
        "button",
        { class: "chip", "aria-pressed": String(this._filter === category) },
        category === "all" ? "All" : CATEGORY_LABELS[category] || category
      );
      chip.addEventListener("click", () => {
        this._filter = category;
        this._render();
      });
      toolbar.append(chip);
    });

    const search = el("input", { type: "search", placeholder: "Search packages" });
    search.value = this._search;
    search.addEventListener("input", (event) => {
      this._search = event.target.value;
      this._render();
      // Re-focus: the input is recreated on each render.
      const next = this.shadowRoot.querySelector('input[type="search"]');
      if (next) {
        next.focus();
        next.setSelectionRange(next.value.length, next.value.length);
      }
    });
    toolbar.append(search);

    const installed = el(
      "button",
      { class: "chip", "aria-pressed": String(this._installedOnly) },
      "Installed only"
    );
    installed.addEventListener("click", () => {
      this._installedOnly = !this._installedOnly;
      this._render();
    });
    toolbar.append(installed);

    return toolbar;
  }

  _card(pkg) {
    const card = el("div", { class: "card clickable" });
    card.addEventListener("click", (event) => {
      if (event.target.closest("button, a")) return;
      this._openDetailDialog(pkg);
    });

    const head = el("div", { class: "card-head" });

    head.append(this._icon(pkg));

    const titles = el("div", {});
    titles.append(el("h2", {}, pkg.name));
    titles.append(el("p", {}, pkg.description || "No description."));
    head.append(titles);
    card.append(head);

    card.append(this._badges(pkg));

    if (pkg.error) {
      card.append(
        el("p", { style: "color: var(--error-color, #db4437)" }, pkg.error)
      );
    }

    card.append(this._actions(pkg));
    return card;
  }

  _badges(pkg) {
    const badges = el("div", { class: "badges" });
    badges.append(
      el("span", { class: "badge" }, CATEGORY_LABELS[pkg.category] || pkg.category)
    );
    badges.append(
      el("span", { class: "badge" }, SOURCE_LABELS[pkg.source_type] || pkg.source_type)
    );
    if (pkg.custom) badges.append(el("span", { class: "badge" }, "Custom"));
    if (pkg.installed) {
      badges.append(
        el(
          "span",
          { class: "badge installed" },
          `Installed ${pkg.installed_version || ""}`.trim()
        )
      );
    }
    if (pkg.update_available) {
      badges.append(
        el(
          "span",
          { class: "badge update" },
          `Update available V${pkg.available_version}`
        )
      );
    }
    if (pkg.needs_restart_on_change) {
      badges.append(el("span", { class: "badge" }, "Needs restart"));
    }
    if (pkg.error) {
      badges.append(el("span", { class: "badge error" }, "Unreachable"));
    }
    if (pkg.external_conflict) {
      badges.append(
        el(
          "span",
          { class: "badge conflict" },
          CONFLICT_LABELS[pkg.external_conflict_source] || "Already present"
        )
      );
    }
    return badges;
  }

  /**
   * Try Home Assistant's own brands database first, then the repo-derived
   * fallback (an explicit catalog icon, or the owner's GitHub avatar), then
   * an mdi icon. We can't know ahead of time which packages are actually
   * registered in home-assistant/brands without a network round trip per
   * package at catalog-load time, so instead the browser just tries loading
   * the image and falls through to the next candidate on a 404.
   */
  _icon(pkg) {
    const candidates = [];
    if (pkg.brand_icon) candidates.push(pkg.brand_icon);
    if (pkg.icon && /^https?:\/\//.test(pkg.icon)) candidates.push(pkg.icon);

    if (!candidates.length) {
      return this._mdiIcon(pkg);
    }

    const img = el("img", { src: candidates[0], alt: "", loading: "lazy" });
    let next = 1;
    img.addEventListener("error", () => {
      if (next < candidates.length) {
        img.src = candidates[next];
        next += 1;
      } else {
        img.replaceWith(this._mdiIcon(pkg));
      }
    });
    return img;
  }

  _mdiIcon(pkg) {
    // ha-icon is provided by the Home Assistant frontend.
    const icon = document.createElement("ha-icon");
    icon.setAttribute(
      "icon",
      pkg.icon && pkg.icon.startsWith("mdi:")
        ? pkg.icon
        : CATEGORY_ICONS[pkg.category] || "mdi:package-variant"
    );
    icon.style.setProperty("--mdc-icon-size", "40px");
    return icon;
  }

  /**
   * `onStart`, if given, fires once an action is actually about to run —
   * after any confirm() the user had to accept, never on a cancelled one.
   * The detail dialog uses it to close itself only once something really
   * started, instead of on every button click regardless of outcome.
   */
  _actions(pkg, onStart) {
    const actions = el("div", { class: "actions" });
    const busy = this._busy.has(pkg.id);

    if (!pkg.installed) {
      const install = el("button", {}, busy ? "Installing…" : "Install");
      install.disabled = busy || Boolean(pkg.error);
      install.addEventListener("click", () => {
        if (
          pkg.external_conflict &&
          !window.confirm(`${pkg.external_conflict}\n\nInstall anyway?`)
        ) {
          return;
        }
        if (onStart) onStart();
        this._action(pkg.id, "install");
      });
      actions.append(install);
    } else {
      if (pkg.update_available) {
        const update = el("button", {}, busy ? "Updating…" : "Update");
        update.disabled = busy;
        update.addEventListener("click", () => {
          if (onStart) onStart();
          this._action(pkg.id, "update");
        });
        actions.append(update);
      }
      const uninstall = el(
        "button",
        { class: "danger" },
        busy ? "Working…" : "Uninstall"
      );
      uninstall.disabled = busy;
      uninstall.addEventListener("click", () => {
        if (
          window.confirm(
            `Remove ${pkg.name}? Its files will be deleted from your config directory.`
          )
        ) {
          if (onStart) onStart();
          this._action(pkg.id, "uninstall");
        }
      });
      actions.append(uninstall);
    }

    if (pkg.custom && !pkg.installed) {
      const forget = el("button", { class: "ghost" }, "Forget");
      forget.disabled = busy;
      forget.addEventListener("click", () => {
        if (onStart) onStart();
        this._action(pkg.id, "remove_custom_repo");
      });
      actions.append(forget);
    }

    return actions;
  }

  _banner(kind, text, actionLabel, onAction, disabled) {
    const banner = el("div", { class: `banner ${kind}` }, text);
    if (actionLabel) {
      const button = el("button", { class: "ghost" }, actionLabel);
      button.disabled = Boolean(disabled);
      button.addEventListener("click", onAction);
      banner.append(button);
    }
    return banner;
  }

  // --- package detail dialog ---------------------------------------------------

  _openDetailDialog(pkg) {
    const dialog = document.createElement("dialog");
    dialog.className = "detail";

    const close = el("button", { class: "ghost close" }, "Close");
    close.addEventListener("click", () => dialog.close());
    dialog.append(close);

    const head = el("div", { class: "detail-head" });
    head.append(this._icon(pkg));
    const titles = el("div", {});
    titles.append(el("h2", {}, pkg.name));
    titles.append(el("p", {}, pkg.description || "No description."));
    titles.append(this._badges(pkg));
    head.append(titles);
    dialog.append(head);

    const body = el("div", { class: "detail-body" });
    const readme = el("div", { class: "readme" }, "Loading README…");
    body.append(readme);
    dialog.append(body);

    const actions = el("div", { class: "dialog-actions" });
    if (pkg.repo_url) {
      const link = el(
        "a",
        {
          class: "ghost",
          href: pkg.repo_url,
          target: "_blank",
          rel: "noopener noreferrer",
        },
        "View repository"
      );
      actions.append(link);
    }
    actions.append(this._actions(pkg, () => dialog.close()));
    dialog.append(actions);

    dialog.addEventListener("close", () => dialog.remove());
    this.shadowRoot.append(dialog);
    dialog.showModal();

    this._loadReadme(pkg.id, readme);
  }

  async _loadReadme(packageId, container) {
    if (this._readmeCache.has(packageId)) {
      container.innerHTML = this._readmeCache.get(packageId);
      return;
    }
    try {
      const result = await this._call("get", `readme?id=${encodeURIComponent(packageId)}`);
      const rendered = result.readme
        ? renderMarkdown(result.readme)
        : "<p>No README available for this package.</p>";
      this._readmeCache.set(packageId, rendered);
      container.innerHTML = rendered;
    } catch (err) {
      container.textContent = `Could not load README: ${err.message}`;
    }
  }

  // --- add custom repository dialog -------------------------------------------

  _openAddDialog() {
    const dialog = document.createElement("dialog");
    dialog.append(el("h2", {}, "Add custom repository"));

    const form = el("div", {});
    const inputs = {};

    const addField = (key, label, options) => {
      const wrapper = el("label", { class: "field" });
      wrapper.append(el("span", {}, label));
      let input;
      if (options) {
        input = document.createElement("select");
        options.forEach(([value, text]) => {
          const option = document.createElement("option");
          option.value = value;
          option.textContent = text;
          input.append(option);
        });
      } else {
        input = el("input", { type: "text" });
      }
      wrapper.append(input);
      form.append(wrapper);
      inputs[key] = input;
      return input;
    };

    addField("id", "Unique id (slug)");
    addField("name", "Display name");
    addField(
      "category",
      "Category",
      Object.entries(CATEGORY_LABELS).map(([value, text]) => [value, text])
    );
    addField("domain", "Integration domain (integrations only)");
    const typeSelect = addField(
      "type",
      "Source type",
      Object.entries(SOURCE_LABELS).map(([value, text]) => [value, text])
    );

    const sourceFields = el("div", {});
    form.append(sourceFields);

    const renderSourceFields = () => {
      sourceFields.textContent = "";
      (SOURCE_FIELDS[typeSelect.value] || []).forEach((field) => {
        const wrapper = el("label", { class: "field" });
        wrapper.append(el("span", {}, field.label));
        const input = el("input", { type: "text" });
        input.dataset.key = field.key;
        input.dataset.required = field.required ? "1" : "";
        wrapper.append(input);
        sourceFields.append(wrapper);
      });
    };
    typeSelect.addEventListener("change", renderSourceFields);
    renderSourceFields();

    const error = el("div", { class: "banner error", style: "display:none" });
    form.append(error);

    dialog.append(form);

    const actions = el("div", { class: "dialog-actions" });
    const cancel = el("button", { class: "ghost" }, "Cancel");
    cancel.addEventListener("click", () => dialog.close());
    const submit = el("button", {}, "Add");

    submit.addEventListener("click", async () => {
      const source = { type: typeSelect.value };
      let missing = null;

      sourceFields.querySelectorAll("input").forEach((input) => {
        const value = input.value.trim();
        if (!value) {
          if (input.dataset.required) missing = input.dataset.key;
          return;
        }
        source[input.dataset.key] = value;
      });

      const payload = {
        id: inputs.id.value.trim(),
        name: inputs.name.value.trim(),
        category: inputs.category.value,
        source,
      };
      if (inputs.domain.value.trim()) payload.domain = inputs.domain.value.trim();

      if (!payload.id || !payload.name) missing = missing || "id and name";
      if (missing) {
        error.textContent = `Please fill in: ${missing}`;
        error.style.display = "";
        return;
      }

      submit.disabled = true;
      submit.textContent = "Checking…";
      try {
        await this._call("post", "add_custom_repo", payload);
        dialog.close();
        await this._load();
      } catch (err) {
        error.textContent = err.message;
        error.style.display = "";
        submit.disabled = false;
        submit.textContent = "Add";
      }
    });

    actions.append(cancel, submit);
    dialog.append(actions);

    dialog.addEventListener("close", () => dialog.remove());
    this.shadowRoot.append(dialog);
    dialog.showModal();
  }
}

/** Small element helper: tag, attributes, optional text child. */
function el(tag, attributes, text) {
  const node = document.createElement(tag);
  Object.entries(attributes || {}).forEach(([key, value]) => {
    node.setAttribute(key, value);
  });
  if (text !== undefined) node.textContent = text;
  return node;
}

// --- minimal, safe markdown rendering ---------------------------------------
//
// READMEs come from repositories we do not control, so this is intentionally
// small rather than pulling in a markdown dependency: every raw character is
// HTML-escaped up front, and every tag it produces afterwards is one we chose
// to emit — nothing from the source ever passes through as literal HTML. The
// only place an attribute value comes from the source is href/src, which are
// restricted to http(s) URLs before use to keep out `javascript:` links.

function escapeHtml(text) {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function safeUrl(url) {
  return /^https?:\/\//i.test(url) ? url : null;
}

function renderInline(text) {
  let result = text.replace(/`([^`]+)`/g, "<code>$1</code>");

  result = result.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (match, alt, url) => {
    const safe = safeUrl(url);
    return safe
      ? `<img src="${safe}" alt="${alt}" loading="lazy" referrerpolicy="no-referrer">`
      : alt;
  });

  result = result.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (match, label, url) => {
    const safe = safeUrl(url);
    return safe
      ? `<a href="${safe}" target="_blank" rel="noopener noreferrer">${label}</a>`
      : label;
  });

  result = result.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  result = result.replace(/(^|[^*])\*([^*]+)\*(?!\*)/g, "$1<em>$2</em>");

  return result;
}

/** Render a README as markdown-lite HTML. Input is untrusted. */
function renderMarkdown(source) {
  const lines = escapeHtml(String(source)).split(/\r\n|\r|\n/);

  const out = [];
  let inCode = false;
  let codeLines = [];
  let listType = null;
  let paragraph = [];

  const flushParagraph = () => {
    if (paragraph.length) {
      out.push(`<p>${renderInline(paragraph.join(" "))}</p>`);
      paragraph = [];
    }
  };
  const closeList = () => {
    if (listType) {
      out.push(listType === "ul" ? "</ul>" : "</ol>");
      listType = null;
    }
  };

  for (const line of lines) {
    if (/^```/.test(line.trim())) {
      if (inCode) {
        out.push(`<pre><code>${codeLines.join("\n")}</code></pre>`);
        codeLines = [];
        inCode = false;
      } else {
        flushParagraph();
        closeList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      codeLines.push(line);
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      flushParagraph();
      closeList();
      const level = heading[1].length;
      out.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
      continue;
    }

    if (/^(-{3,}|\*{3,}|_{3,})$/.test(line.trim())) {
      flushParagraph();
      closeList();
      out.push("<hr>");
      continue;
    }

    // '>' was already escaped to '&gt;' by escapeHtml above.
    const quote = line.match(/^&gt;\s?(.*)$/);
    if (quote) {
      flushParagraph();
      closeList();
      out.push(`<blockquote>${renderInline(quote[1])}</blockquote>`);
      continue;
    }

    const unordered = line.match(/^[-*]\s+(.*)$/);
    const ordered = line.match(/^\d+\.\s+(.*)$/);
    if (unordered || ordered) {
      flushParagraph();
      const wantType = unordered ? "ul" : "ol";
      if (listType !== wantType) {
        closeList();
        out.push(wantType === "ul" ? "<ul>" : "<ol>");
        listType = wantType;
      }
      out.push(`<li>${renderInline((unordered || ordered)[1])}</li>`);
      continue;
    }

    if (line.trim() === "") {
      flushParagraph();
      closeList();
      continue;
    }

    closeList();
    paragraph.push(line.trim());
  }

  flushParagraph();
  closeList();
  if (inCode && codeLines.length) {
    out.push(`<pre><code>${codeLines.join("\n")}</code></pre>`);
  }

  return out.join("\n");
}

if (!customElements.get("integrated-store-panel")) {
  customElements.define("integrated-store-panel", IntegratedStorePanel);
}

export { IntegratedStorePanel };
