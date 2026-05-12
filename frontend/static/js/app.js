/* Master Data Validator - Frontend Controller */
function app() {
  return {
    // ─── State ───────────────────────────────────────────
    user: null,
    login: { username: "", password: "", error: "", loading: false },
    view: "repository",

    // Environment identity — "production" or "staging". Drives the
    // page title prefix and the top banner. Defaults to "production"
    // so the banner doesn't flash on first load; it'll get updated
    // the moment /api/health returns (in checkAuth or first request).
    env: "production",
    envBannerDismissed: false,

    repo: { files: [] },
    modules: [],
    filters: { status: "", module: "" },

    session: { loaded: false },
    dashboard: {},
    decisions: [],
    // v62: session LTMC overrides — populated by loadDecisions() from the
    // /api/session/decisions endpoint. Map of {sap_field: value}, e.g.
    // {ALAND:'IN', WAERS:'INR', BWKEY:'PE01'}. Rendered as chips below
    // the Decisions page subtitle.
    ltmcOverrides: {},
    // v66: Modal state for cross-file decision guidance (sheet=LongText
    // or AlternateUnits). Populated by openCrossFileGuidance(); rendered
    // by a modal block in index.html that shows the affected MATNRs,
    // the file the SME needs to fix, and a re-upload prompt.
    crossFileModal: { open: false },
    filter: "all",
    search: "",
    allErrors: [],
    // Errors-endpoint pagination metadata. allErrors holds the visible
    // page; if the backend's total exceeds it (PP/Routing files with
    // 100k+ errors), errorsTruncated=true and the UI shows a banner.
    errorsTruncated: false,
    errorsTotalCount: 0,
    _toastedTruncation: false,
    gridFilter: { sheet: "", rule: "", search: "" },

    // Error Grid perf — cache filtered results so we don't re-scan
    // 27,000+ rows on every template read. Updated via watchers only
    // when filters or the underlying data change.
    gridCache: {
      rows: [],       // visible slice (max 500)
      total: 0,       // full filtered count
      truncated: false,
      searchDebounce: null,
    },

    // Changes Summary (on Export page)
    changesSummary: null,   // {changes: [...], total, by_type, by_sheet}
    changesFilter: { type: "", sheet: "", search: "" },
    _filteredChangesCache: null,  // memoisation for filteredChanges()
    record: { data: null, edits: {}, customerName: "", customerNum: "", showAll: false,
              // Flow context for "Fix individually" sessions — when set,
              // the record editor shows a decision-scoped top banner and
              // Save & Next advances through the remaining affected rows.
              flow: null,
            },

    // Reference data for dropdowns in record editor
    refData: {
      indiaStates: [],   // [{code, name}]
      countries: [],     // [{code, name}]
      loaded: false,
    },

    // Session audit (in-memory session log + persistent per-file audit)
    audit: {
      log: [],           // in-memory session log (for undo via audit_index)
      persistent: [],    // per-file persistent audit from Postgres
      // Admin audit page
      entries: [],
      filterAction: "",
      filterUser: "",
      filterSearch: "",
      uniqueUsers() {
        const s = new Set();
        (this.entries ?? []).forEach(e => e.username && s.add(e.username));
        return Array.from(s).sort();
      },
      filteredEntries() {
        let list = this.entries ?? [];
        if (this.filterAction) list = list.filter(e => e.action === this.filterAction);
        if (this.filterUser) list = list.filter(e => e.username === this.filterUser);
        if (this.filterSearch) {
          const q = this.filterSearch.toLowerCase();
          list = list.filter(e =>
            ((e.filename ?? "") + " " + (e.sheet ?? "") + " " + (e.reason ?? "") + " " + (e.module ?? ""))
              .toLowerCase().includes(q)
          );
        }
        return list;
      },
    },

    admin: { users: [] },

    modal: { type: null },
    toasts: [],
    dragOver: false,

    // ─── Loading / busy state ────────────────────────────
    busyCount: 0,              // how many requests currently in-flight (for top bar)
    busyAction: null,          // string key for which specific action is busy (for spinner on button)
    busyRow: null,             // row identifier currently being opened

    get isBusy() { return this.busyCount > 0; },

    // Wrap any async handler so button shows spinner + global top bar while it runs.
    // action is a string key you use to query "is this exact button busy?"
    async runBusy(action, fn) {
      this.busyCount++;
      if (action) this.busyAction = action;
      document.body.classList.add("is-loading");
      try {
        return await fn();
      } finally {
        this.busyCount = Math.max(0, this.busyCount - 1);
        if (this.busyAction === action) this.busyAction = null;
        if (this.busyCount === 0) document.body.classList.remove("is-loading");
      }
    },

    busyFor(key) { return this.busyAction === key; },

    // ─── Lifecycle ───────────────────────────────────────
    async init() {
      ["view", "modal", "user", "session", "decisions", "dashboard",
       "record", "repo", "modules", "allErrors", "admin", "toasts"].forEach(k => {
        this.$watch(k, () => this.refreshIcons());
      });
      this.$watch("audit.entries", () => this.refreshIcons());

      await this.checkAuth();
      // Load reference data for smart editors (small payload, cache for session)
      this.loadRefData();
      // Load environment identity — lets UI show a STAGING banner when
      // this is not the production environment.
      this.loadEnv();

      document.addEventListener("keydown", (e) => {
        if (!this.user) return;

        // Ctrl+S / Cmd+S — always intercept so the browser never gets a
        // chance to "save the webpage". During a Fix-Individually flow
        // it saves AND advances; otherwise it just saves.
        if (e.key === "s" && (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey) {
          e.preventDefault();
          if (this.view === "records" && this.record.data) {
            if (this.inFixFlow()) {
              this.saveAndNext();
            } else {
              this.saveRecord();
            }
          } else {
            this.toast("Nothing to save on this page", "info", "info");
          }
          return;
        }

        // Ctrl+Enter in the record editor — Save & Next (flow) or Save (not).
        // Works anywhere on the page, including inside input fields, since
        // it's a strong "commit this" gesture.
        if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey) {
          if (this.view === "records" && this.record.data) {
            e.preventDefault();
            if (this.inFixFlow()) {
              this.saveAndNext();
            } else {
              this.saveRecord();
            }
          }
          return;
        }

        const target = e.target;
        if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.tagName === "SELECT") {
          if (e.key === "Escape") {
            // Priority: close modal if open, else discard pending edits
            // on this record, else just blur the input. This makes Escape
            // a predictable "undo / back out" gesture everywhere.
            if (this.modal.type) {
              this.closeModal();
              e.preventDefault();
              return;
            }
            if (this.view === "records" && this.record.data && Object.keys(this.record.edits).length > 0) {
              this.record.edits = {};
              target.blur();
              this.toast("Changes discarded", "info", "info");
              e.preventDefault();
              return;
            }
            // Nothing pending — just blur so arrow keys / scroll work again
            target.blur();
            e.preventDefault();
          }
          return;
        }
        if (this.modal.type && e.key === "Escape") {
          if (this.modal.type === "confirm") {
            this.onCancel();
          } else {
            this.closeModal();
          }
          return;
        }
        if (this.view === "records" && this.record.data) {
          // Escape outside an input: exit flow back to decisions if in
          // one, else go back to decisions regardless. Predictable "back
          // out" gesture from the record editor.
          if (e.key === "Escape") {
            if (this.record.flow) {
              this.backToDecisions();
            } else {
              this.setView("decisions");
            }
            e.preventDefault();
            return;
          }
          // j / k navigation only — arrow keys must stay free for normal
          // page scroll. Hijacking ArrowDown/ArrowUp globally broke scrolling
          // on record pages with lots of fields.
          if (e.key === "j") { this.navigateError(1); e.preventDefault(); }
          else if (e.key === "k") { this.navigateError(-1); e.preventDefault(); }
        }
      });

      // BULLETPROOF: document-level click delegation for critical modal actions.
      // Even if Alpine's @click bindings fail for any reason, elements with
      // data-mdv-action attributes will still fire these handlers via event
      // bubbling. This is a belt-and-suspenders safety net for demo reliability.
      document.addEventListener("click", (e) => {
        const actionEl = e.target.closest("[data-mdv-action]");
        if (!actionEl) return;
        const action = actionEl.getAttribute("data-mdv-action");
        console.log("[MDV] document click action:", action);
        try {
          if (action === "confirm-ok") { e.preventDefault(); e.stopPropagation(); this.onConfirm(); }
          else if (action === "confirm-cancel") { e.preventDefault(); e.stopPropagation(); this.onCancel(); }
          else if (action === "action-apply") { e.preventDefault(); e.stopPropagation(); this.onActionConfirm(); }
          else if (action === "action-cancel") { e.preventDefault(); e.stopPropagation(); this.closeModal(); }
        } catch (err) {
          console.error("[MDV] handler error:", err);
        }
      });

      this.refreshIcons();
    },

    refreshIcons() {
      this.$nextTick(() => {
        if (window.lucide) window.lucide.createIcons();
      });
    },

    // ─── Auth ────────────────────────────────────────────
    async checkAuth() {
      try {
        const res = await fetch("/api/auth/me");
        if (res.ok) {
          this.user = await res.json();
          await this.loadModules();
          await this.loadRepo();
          if (this.user.role === "admin") await this.loadAdminUsers();
        }
      } catch (e) { }
    },

    /** Pull the environment identity from the health endpoint and
     *  update document.title so the browser tab makes it obvious
     *  when the user is looking at STAGING vs production. Doesn't
     *  require auth — health is public. */
    async loadEnv() {
      try {
        const res = await fetch("/api/health");
        if (!res.ok) return;
        const data = await res.json();
        this.env = data.env || "production";
        if (this.env !== "production") {
          document.title = `[${this.env.toUpperCase()}] Master Data Validator`;
        } else {
          document.title = "Master Data Validator";
        }
      } catch (e) { }
    },

    async doLogin() {
      this.login.error = "";
      this.login.loading = true;
      try {
        const res = await fetch("/api/auth/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username: this.login.username, password: this.login.password }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail ?? "Invalid credentials");
        }
        this.user = await res.json();
        this.toast(`Welcome, ${this.user.display_name}`, "success", "check-circle-2");
        await this.loadModules();
        await this.loadRepo();
        if (this.user.role === "admin") await this.loadAdminUsers();
      } catch (e) {
        this.login.error = e.message;
      } finally {
        this.login.loading = false;
      }
    },

    async doLogout() {
      await fetch("/api/auth/logout", { method: "POST" });
      this.user = null;
      this.session = { loaded: false };
      this.dashboard = {};
      this.login = { username: "", password: "", error: "", loading: false };
      this.toast("Signed out", "info", "log-out");
    },

    // ─── Modules + Repo ──────────────────────────────────
    async loadModules() {
      const res = await fetch("/api/repo/modules");
      const data = await res.json();
      this.modules = data.modules;
    },

    async loadRepo() {
      const q = new URLSearchParams();
      if (this.filters.module) q.append("module", this.filters.module);
      if (this.filters.status) q.append("status", this.filters.status);
      const res = await fetch("/api/repo/files?" + q);
      const data = await res.json();
      this.repo.files = data.files;
    },

    filterByModule(code) {
      this.filters.module = code;
      this.loadRepo();
      this.setView("repository");
    },

    moduleFileTotal() {
      return this.modules.filter(m => m.accessible).reduce((sum, m) => sum + m.file_count, 0) || "";
    },

    canValidate(module) {
      if (!this.user) return false;
      if (this.user.role === "admin") return true;
      if (this.user.role === "module" && this.user.module === module) return true;
      return false;
    },

    canValidateCurrent() {
      return this.canValidate(this.dashboard.module);
    },

    canRevoke(file) {
      if (!this.user) return false;
      if (this.user.role === "admin") return true;
      return file.validated_by === this.user.username;
    },

    canMarkLtmc() {
      return this.user && (this.user.role === "admin" || this.user.role === "it");
    },

    canDelete(file) {
      if (!this.user) return false;
      if (this.user.role === "admin") return true;
      if (this.user.role === "it") return true;
      return file.uploaded_by === this.user.username;
    },

    // ─── Upload ──────────────────────────────────────────
    openUploadModal() {
      const firstModule = this.modules.find(m => m.accessible && m.code === "SD")
                       ?? this.modules.find(m => m.accessible);
      this.modal = {
        type: "upload",
        module: this.user.module ?? firstModule?.code ?? "SD",
        uploading: false,
        uploadProgress: 0,
        uploadStatus: "",
        uploadDetail: "",
        dragOver: false,
        // MM-specific: per-slot state. Each slot is null until a file is picked,
        // then { file, filename, status, column_count, data_rows, reason }.
        mm: { main: null, alt_uom: null, longtext: null },
        // PP-specific: BOM (required) + Routing (optional). Each slot is
        // null until a file is picked, then { file, filename, status,
        // reason }. Same shape as MM's slot state so the slot rendering
        // template can be re-used.
        pp: { bom: null, routing: null },
      };
    },

    /** When the user switches module in the upload modal, reset the upload
     *  state so stale data from the other flow doesn't linger. */
    onUploadModuleChange() {
      this.modal.uploading = false;
      this.modal.uploadProgress = 0;
      this.modal.uploadStatus = "";
      this.modal.dragOver = false;
      this.modal.mm = { main: null, alt_uom: null, longtext: null };
      // PP slots — bom (required) + routing (optional). Same shape
      // as the MM slots so the modal can re-use the slot rendering
      // pattern. Reset on every module change to avoid stale entries
      // from a previous flow lingering in the UI.
      this.modal.pp = { bom: null, routing: null };
    },

    /** A file was picked for one of the MM slots. Store it locally and run
     *  the server-side format check so the user sees ✓/✗ before submitting.
     *  This doesn't actually upload yet — that happens on submitMmUpload().
     *
     *  v62: the main slot accepts .xlsx (customer source format) OR .xml
     *  (SAP LTMC source data form — the canonical 29-sheet template).
     *  Alt UoM and Long Text slots stay xlsx-only — when the main file
     *  is an LTMC form, those slots' data is extracted from the form's
     *  own embedded sheets.
     */
    async handleMmSlotFile(slot, file) {
      if (!file) return;
      const name = file.name.toLowerCase();
      const isXlsx = name.endsWith(".xlsx");
      const isXml = name.endsWith(".xml");
      if (slot === "main") {
        if (!isXlsx && !isXml) {
          this.toast("Main slot requires a .xlsx (customer format) or .xml (LTMC source data form) file", "error", "x");
          return;
        }
      } else {
        if (!isXlsx) {
          this.toast(`${slot} slot requires a .xlsx file`, "error", "x");
          return;
        }
      }
      // Optimistic state so the UI shows "checking" immediately
      this.modal.mm[slot] = {
        file,
        filename: file.name,
        status: "checking",
        column_count: null,
        data_rows: null,
        reason: "Checking format…",
      };
      try {
        const form = new FormData();
        form.append("slot", slot);
        form.append("file", file);
        const res = await fetch("/api/mm/format-check", { method: "POST", body: form });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail ?? "Format check failed");
        }
        const info = await res.json();
        this.modal.mm[slot] = {
          file,
          filename: file.name,
          status: info.matches_slot ? "ok" : "bad",
          detected_role: info.detected_role,
          column_count: info.column_count,
          data_rows: info.data_rows,
          reason: info.matches_slot
            ? info.reason
            : `File detected as "${info.detected_role}" — expected "${slot}". ${info.reason}`,
        };
        // Alpine.js reactivity: force re-render of the icons since we
        // changed status-dependent markup.
        this.$nextTick?.(() => (window.lucide?.createIcons?.()));
      } catch (e) {
        this.modal.mm[slot] = {
          file,
          filename: file.name,
          status: "bad",
          reason: e.message,
        };
      }
    },

    /** True when the MM upload is ready to submit.
     *
     *  Only the MAIN slot is required. Alt UoM and Long Text are
     *  optional — SMEs commonly start with just the Main material file
     *  and add the others later. The submit button enables when:
     *    - main slot has status "ok", AND
     *    - any populated optional slot is also "ok" (no in-progress
     *      "checking" or invalid "bad" file in alt_uom / longtext).
     *  An empty optional slot is fine; it just means the user didn't
     *  pick a file there. */
    mmUploadReady() {
      const mm = this.modal.mm || {};
      // Main is mandatory and must be OK.
      if (!mm.main || mm.main.status !== "ok") return false;
      // Optional slots: if the user picked something, it must be OK
      // (not "checking" or "bad"). If they didn't pick, that's fine.
      for (const slot of ["alt_uom", "longtext"]) {
        const s = mm[slot];
        if (!s || !s.file) continue;       // not picked → ignore
        if (s.status !== "ok") return false; // picked but not OK → block
      }
      return true;
    },

    /** Submit MM files to /api/mm/upload and open the resulting bundle.
     *  The Main file is always sent; Alt UoM and Long Text are only sent
     *  if the user picked them. The backend's mm_upload endpoint accepts
     *  this main-only shape and the merger handles the resulting None
     *  values for the absent slots. */
    async submitMmUpload() {
      if (!this.mmUploadReady()) {
        this.toast("The Main file is required before uploading", "warn", "alert-triangle");
        return;
      }
      this.modal.uploading = true;
      this.modal.uploadProgress = 5;

      // Build a dynamic file list that only includes the slots the user
      // actually populated. This keeps the upload payload minimal when
      // the user is doing a main-only upload.
      const hasAlt = !!this.modal.mm.alt_uom?.file;
      const hasLt  = !!this.modal.mm.longtext?.file;
      const slotCount = 1 + (hasAlt ? 1 : 0) + (hasLt ? 1 : 0);
      this.modal.uploadStatus = `Uploading ${slotCount} file${slotCount === 1 ? "" : "s"}`;

      try {
        const form = new FormData();
        form.append("main_file", this.modal.mm.main.file);
        if (hasAlt) form.append("alt_uom_file", this.modal.mm.alt_uom.file);
        if (hasLt)  form.append("longtext_file", this.modal.mm.longtext.file);

        const entry = await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open("POST", "/api/mm/upload");
          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
              this.modal.uploadProgress = Math.min(50, 5 + (e.loaded / e.total) * 45);
            }
          };
          // Once the XHR send completes, the server starts parsing — for a
          // 12 MB main file that takes ~50 seconds in openpyxl. Without
          // visual feedback users think the app froze. Tick the bar slowly
          // from 50→90% over an estimated time-to-validate based on the
          // total upload size. If validation finishes early the success
          // handler skips it to 100%; if it overruns, the bar caps at 90%.
          let serverPhaseTimer = null;
          xhr.upload.onload = () => {
            // XHR send finished — server is now parsing. Start ticking.
            this.modal.uploadProgress = 50;
            this.modal.uploadStatus = "Server parsing + validating (this can take 30–90s for large files)…";
            const startedAt = Date.now();
            // Estimate: ~5s per MB of main file, capped at 120s
            const totalBytes =
              (this.modal.mm.main.file?.size ?? 0) +
              (this.modal.mm.alt_uom?.file?.size ?? 0) +
              (this.modal.mm.longtext?.file?.size ?? 0);
            const estMs = Math.min(120_000, Math.max(20_000, (totalBytes / 1_000_000) * 5_000));
            serverPhaseTimer = setInterval(() => {
              const elapsed = Date.now() - startedAt;
              const frac = Math.min(1, elapsed / estMs);
              // 50 → 90 over the estimate; ease out so it slows near the end
              const eased = 1 - Math.pow(1 - frac, 2);
              this.modal.uploadProgress = 50 + eased * 40;
              if (elapsed > estMs * 1.5) {
                this.modal.uploadStatus = "Still working… large files can take a while";
              }
            }, 500);
          };
          xhr.onload = () => {
            if (serverPhaseTimer) { clearInterval(serverPhaseTimer); serverPhaseTimer = null; }
            if (xhr.status < 400) {
              this.modal.uploadProgress = 95;
              this.modal.uploadStatus = "Opening bundle…";
              resolve(JSON.parse(xhr.responseText));
            } else {
              try { reject(new Error(JSON.parse(xhr.responseText).detail ?? "Upload failed")); }
              catch { reject(new Error("Upload failed")); }
            }
          };
          xhr.onerror = () => {
            if (serverPhaseTimer) { clearInterval(serverPhaseTimer); }
            reject(new Error("Network error"));
          };
          xhr.send(form);
        });

        this.modal.uploadProgress = 85;
        this.modal.uploadStatus = "Opening bundle…";
        await this.loadRepo();
        await this.loadModules();
        await this.openFile(entry.file_id);

        // Capture filename BEFORE closeModal — closeModal resets
        // this.modal to {type: null}, so accessing this.modal.mm.main
        // afterwards crashes with "Cannot read properties of undefined".
        // The crash propagates to the outer catch and shows a misleading
        // "MM upload failed" toast even though the upload succeeded.
        const mainFilename = this.modal.mm?.main?.filename ?? entry.filename ?? "file";

        this.modal.uploadProgress = 100;
        this.closeModal();
        this.toast(
          `Uploaded ${mainFilename} + 2 others · ${entry.row_count ?? 0} materials`,
          "success", "check-circle-2",
        );
      } catch (e) {
        this.toast("MM upload failed: " + e.message, "error", "x");
        this.modal.uploading = false;
      }
    },

    /** A file was picked for one of the PP slots ('bom' or 'routing').
     *  Store it locally and run /api/pp/format-check so the user sees
     *  ✓/✗ before submitting. The backend's detector counts BOM-vs-
     *  Routing anchor sheets and returns role="bom"/"routing"/"unknown".
     *
     *  Files >100MB skip the format check (uploading 5 GB twice is too
     *  painful) — the real upload's parser will surface format errors. */
    async handlePpSlotFile(slot, file) {
      if (!file) return;
      if (!file.name.toLowerCase().endsWith(".xlsx")) {
        this.toast(`${slot} slot requires a .xlsx file`, "error", "x");
        return;
      }

      const FORMAT_CHECK_MAX_BYTES = 100 * 1024 * 1024; // 100 MB

      // Optimistic state so the UI shows "checking" immediately
      this.modal.pp[slot] = {
        file,
        filename: file.name,
        status: "checking",
        reason: "Checking format…",
      };

      // Skip server-side check for huge files; let the upload do it.
      if (file.size > FORMAT_CHECK_MAX_BYTES) {
        this.modal.pp[slot] = {
          file,
          filename: file.name,
          status: "ok",
          reason: `Large file (${(file.size / 1024 / 1024).toFixed(1)} MB) — pre-upload sheet check skipped; the real upload will validate the format.`,
        };
        this.$nextTick?.(() => (window.lucide?.createIcons?.()));
        return;
      }

      try {
        const form = new FormData();
        form.append("file", file);
        const res = await fetch("/api/pp/format-check", { method: "POST", body: form });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.detail ?? "Format check failed");
        }
        const info = await res.json();

        // Map backend's role to expected slot. PP slot 'bom' wants
        // role='bom', slot 'routing' wants role='routing'.
        const expectedRole = (slot === "bom") ? "bom" : "routing";
        const matches = (info.role === expectedRole);

        this.modal.pp[slot] = {
          file,
          filename: file.name,
          status: matches ? "ok" : "bad",
          detected_role: info.role,
          reason: matches
            ? info.reason
            : `File detected as "${info.role}" — expected "${expectedRole}". ${info.reason}`,
        };
        this.$nextTick?.(() => (window.lucide?.createIcons?.()));
      } catch (e) {
        this.modal.pp[slot] = {
          file,
          filename: file.name,
          status: "bad",
          reason: e.message,
        };
      }
    },

    /** True when the PP upload is ready to submit.
     *
     *  Only the BOM slot is required. Routing is optional — SMEs
     *  commonly start with just the BOM and add Routing later. Submit
     *  enables when:
     *    - bom slot has status "ok", AND
     *    - any populated optional slot is also "ok" (no in-progress
     *      "checking" or invalid "bad" file in routing).
     *  An empty optional slot is fine. */
    ppUploadReady() {
      const pp = this.modal.pp || {};
      if (!pp.bom || pp.bom.status !== "ok") return false;
      const r = pp.routing;
      if (r && r.file && r.status !== "ok") return false;
      return true;
    },

    /** Submit PP files to /api/pp/upload. BOM is always sent;
     *  Routing only if picked. Same progress-bar pattern as MM. */
    async submitPpUpload() {
      if (!this.ppUploadReady()) {
        this.toast("The BOM file is required before uploading", "warn", "alert-triangle");
        return;
      }
      this.modal.uploading = true;
      this.modal.uploadProgress = 5;

      const hasRouting = !!this.modal.pp.routing?.file;
      const slotCount = 1 + (hasRouting ? 1 : 0);
      this.modal.uploadStatus = `Uploading ${slotCount} file${slotCount === 1 ? "" : "s"}`;

      try {
        const form = new FormData();
        form.append("bom_file", this.modal.pp.bom.file);
        if (hasRouting) form.append("routing_file", this.modal.pp.routing.file);

        const entry = await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open("POST", "/api/pp/upload");
          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
              this.modal.uploadProgress = Math.min(50, 5 + (e.loaded / e.total) * 45);
            }
          };
          // Server-phase progress simulator (same pattern as MM). PP
          // parsing is comparable to MM speed-wise: ~3s for the customer's
          // 1883-material BOM, ~10s for the 72k-row Routing.
          let serverPhaseTimer = null;
          xhr.upload.onload = () => {
            this.modal.uploadProgress = 50;
            this.modal.uploadStatus = "Server parsing + validating (this can take 30–90s for large files)…";
            const startedAt = Date.now();
            const totalBytes =
              (this.modal.pp.bom.file?.size ?? 0) +
              (this.modal.pp.routing?.file?.size ?? 0);
            const estMs = Math.min(120_000, Math.max(20_000, (totalBytes / 1_000_000) * 5_000));
            serverPhaseTimer = setInterval(() => {
              const elapsed = Date.now() - startedAt;
              const frac = Math.min(1, elapsed / estMs);
              const eased = 1 - Math.pow(1 - frac, 2);
              this.modal.uploadProgress = 50 + eased * 40;
              if (elapsed > estMs * 1.5) {
                this.modal.uploadStatus = "Still working… large files can take a while";
              }
            }, 500);
          };
          xhr.onload = () => {
            if (serverPhaseTimer) { clearInterval(serverPhaseTimer); serverPhaseTimer = null; }
            if (xhr.status < 400) {
              this.modal.uploadProgress = 95;
              this.modal.uploadStatus = "Opening bundle…";
              resolve(JSON.parse(xhr.responseText));
            } else {
              try { reject(new Error(JSON.parse(xhr.responseText).detail ?? "Upload failed")); }
              catch { reject(new Error("Upload failed")); }
            }
          };
          xhr.onerror = () => {
            if (serverPhaseTimer) { clearInterval(serverPhaseTimer); }
            reject(new Error("Network error"));
          };
          xhr.send(form);
        });

        this.modal.uploadProgress = 85;
        this.modal.uploadStatus = "Opening bundle…";
        await this.loadRepo();
        await this.loadModules();
        await this.openFile(entry.file_id);

        // Capture filename BEFORE closeModal — same gotcha as MM.
        const bomFilename = this.modal.pp?.bom?.filename ?? entry.filename ?? "file";
        const summary = hasRouting
          ? `${bomFilename} + Routing · ${entry.row_count ?? 0} materials`
          : `${bomFilename} · ${entry.row_count ?? 0} materials`;

        this.modal.uploadProgress = 100;
        this.closeModal();
        this.toast(`Uploaded ${summary}`, "success", "check-circle-2");
      } catch (e) {
        this.toast("PP upload failed: " + e.message, "error", "x");
        this.modal.uploading = false;
      }
    },

    handleUploadDrop(e) {
      this.modal.dragOver = false;
      const f = e.dataTransfer.files[0];
      if (f) this.handleUploadFile(f);
    },

    async handleUploadFile(file) {
      if (!file) return;
      if (this.modal.module === "MM") {
        // MM uploads use the 3-slot flow — this single-file handler
        // shouldn't be reachable when module=MM but guard anyway.
        this.toast("MM requires 3 files — use the slots below", "info", "info");
        return;
      }
      if (this.modal.module === "PP") {
        // PP uploads use the BOM + Routing slot flow — same guard.
        this.toast("PP requires using the BOM/Routing slots below", "info", "info");
        return;
      }
      if (this.modal.module !== "SD") {
        this.toast("Only SD, MM, and PP are active. Others coming soon.", "warn", "alert-triangle");
        return;
      }
      this.modal.uploading = true;
      this.modal.uploadProgress = 5;
      this.modal.uploadStatus = "Uploading " + file.name;
      this.modal.uploadDetail = (file.size / 1024).toFixed(0) + " KB";

      try {
        const form = new FormData();
        form.append("module", this.modal.module);
        form.append("file", file);

        const entry = await new Promise((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhr.open("POST", "/api/repo/upload");
          xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
              // Upload phase: 0–50% of the bar
              this.modal.uploadProgress = Math.min(50, 5 + (e.loaded / e.total) * 45);
            }
          };
          xhr.onload = () => {
            if (xhr.status < 400) {
              this.modal.uploadStatus = "File uploaded · registering...";
              this.modal.uploadProgress = 55;
              resolve(JSON.parse(xhr.responseText));
            } else {
              try { reject(new Error(JSON.parse(xhr.responseText).detail)); }
              catch { reject(new Error("Upload failed")); }
            }
          };
          xhr.onerror = () => reject(new Error("Network error"));
          xhr.send(form);
        });

        // Status updates through the (slow) parse+validate phase
        this.modal.uploadStatus = "Parsing XML structure...";
        this.modal.uploadProgress = 65;
        await this.loadRepo();

        this.modal.uploadStatus = "Running validation rules...";
        this.modal.uploadProgress = 80;
        await this.loadModules();

        // Now the heavy step: open+validate on backend. Show progress while waiting.
        this.modal.uploadStatus = `Validating ${entry.row_count ?? ''} records · this may take a few seconds for large files...`;
        this.modal.uploadProgress = 88;
        await this.openFile(entry.file_id);

        this.modal.uploadProgress = 100;
        this.modal.uploadStatus = "Done";
        this.closeModal();
        this.toast(`Opened ${entry.filename} · ${entry.row_count ?? 0} records`, "success", "check-circle-2");
      } catch (e) {
        this.toast("Upload failed: " + e.message, "error", "x");
        this.modal.uploading = false;
      }
    },

    // ─── File actions ────────────────────────────────────
    async openFile(fileId) {
      this.busyRow = fileId;
      // Reset per-file UI flags so banners re-appear for the new file.
      this._toastedTruncation = false;
      this.errorsTruncated = false;
      this.errorsTotalCount = 0;
      await this.runBusy(`open:${fileId}`, async () => {
        try {
          const res = await fetch(`/api/session/open/${fileId}`, { method: "POST" });
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail ?? "Failed to open");
          }
          const data = await res.json();
          this.session.loaded = true;
          await this.reloadAll();
          this.setView("dashboard");
          this.toast(`Opened ${data.filename}`, "info", "file-text");
        } catch (e) {
          this.toast(e.message, "error", "x");
        }
      });
      this.busyRow = null;
    },

    async markValidated(fileId) {
      const { ok } = await this.ui_confirm({
        title: "Mark file as validated?",
        message: "All errors should be resolved before marking as validated. This action is tracked in the audit trail.",
        confirmLabel: "Mark validated",
        tone: "default",
        icon: "check-circle-2",
      });
      if (!ok) return;
      await this.runBusy(`validate:${fileId}`, async () => {
        try {
          const res = await fetch(`/api/repo/files/${fileId}/validated`, { method: "POST" });
          if (!res.ok) throw new Error((await res.json()).detail);
          await this.loadRepo();
          await this.loadModules();
          this.toast("Marked as validated", "success", "check-circle-2");
        } catch (e) {
          this.toast(e.message, "error", "x");
        }
      });
    },

    async markValidatedCurrent() {
      if (!this.dashboard.file_id) return;
      if (this.dashboard.pending_errors > 0) {
        this.toast(`Resolve ${this.dashboard.pending_errors} remaining errors first`, "warn", "alert-triangle");
        return;
      }
      await this.markValidated(this.dashboard.file_id);
    },

    /** Voluntarily close the loaded file. Frees server RAM immediately.
     *  Working copy is already persisted on bulk actions & export, so this
     *  is safe — no unsaved data loss. Does NOT delete the file from the
     *  repository; user can reopen it. */
    async closeCurrentFile() {
      if (!this.dashboard.file_id) {
        this.setView("repository");
        return;
      }
      this.record.flow = null;
      try {
        await fetch("/api/session/close", { method: "POST" });
      } catch { /* best-effort — backend sweep will catch it too */ }
      this.session.loaded = false;
      this.dashboard = {};
      this.setView("repository");
      this.toast("Closed · returned to repository", "info", "check-circle-2");
    },

    async revokeValidation(fileId) {
      const { ok } = await this.ui_confirm({
        title: "Revoke validation?",
        message: "File will return to 'In Progress'. This action is tracked in the audit trail.",
        confirmLabel: "Revoke",
        tone: "warn",
        icon: "rotate-ccw",
      });
      if (!ok) return;
      await this.runBusy(`revoke:${fileId}`, async () => {
        try {
          const res = await fetch(`/api/repo/files/${fileId}/revoke`, { method: "POST" });
          if (!res.ok) throw new Error((await res.json()).detail);
          await this.loadRepo();
          this.toast("Validation revoked", "info", "rotate-ccw");
        } catch (e) {
          this.toast(e.message, "error", "x");
        }
      });
    },

    async markLtmc(fileId) {
      const { ok } = await this.ui_confirm({
        title: "Mark as uploaded to LTMC?",
        message: "Confirms the file has been loaded into SAP LTMC. Terminal status — the file will be locked.",
        confirmLabel: "Mark LTMC uploaded",
        tone: "default",
        icon: "check-circle-2",
      });
      if (!ok) return;
      await this.runBusy(`ltmc:${fileId}`, async () => {
        try {
          const res = await fetch(`/api/repo/files/${fileId}/ltmc_uploaded`, { method: "POST" });
          if (!res.ok) throw new Error((await res.json()).detail);
          await this.loadRepo();
          this.toast("Marked as LTMC uploaded", "success", "check-circle-2");
        } catch (e) {
          this.toast(e.message, "error", "x");
        }
      });
    },

    async deleteFile(fileId) {
      const { ok } = await this.ui_confirm({
        title: "Delete this file permanently?",
        message: "The file and all its audit history will be removed. This cannot be undone.",
        confirmLabel: "Delete file",
        tone: "danger",
        icon: "trash-2",
      });
      if (!ok) return;
      await this.runBusy(`delete:${fileId}`, async () => {
        try {
          const res = await fetch(`/api/repo/files/${fileId}`, { method: "DELETE" });
          if (!res.ok) throw new Error((await res.json()).detail);
          await this.loadRepo();
          this.toast("File deleted", "info", "trash-2");
        } catch (e) {
          this.toast(e.message, "error", "x");
        }
      });
    },

    async downloadFile(fileId, filename) {
      const a = document.createElement("a");
      a.href = `/api/repo/files/${fileId}/download`;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
    },

    // v63: Friendly-label resolver for SAP codes shown in compact UI
    // spots (LTMC defaults chip row, audit log condensations, etc.)
    // where the full backend Decision object isn't directly available.
    // Mirrors the canonical map in mm_ltmc_mandatory.py — kept in sync
    // by code review since the set is small and stable. If a code isn't
    // in the map we fall back to the code itself so the UI never shows
    // a blank label.
    ltmcFriendlyLabel(sapCode) {
      const MAP = {
        BWKEY: "Valuation Area",
        ALAND: "Country/Region",
        WAERS: "Currency",
        CURTP: "Currency Type",
        SPRAS: "Language Key",
        TATYP1: "Tax Category 1",
        TAXM1: "Tax Classification 1",
        BERID: "MRP Area",
        LGNUM: "Warehouse Number",
        LGTYP: "Storage Type",
        ART: "Inspection Type",
        RQGRP: "Requirement Group",
      };
      return MAP[sapCode] || sapCode;
    },

    // ─── Session (opened file) ───────────────────────────
    async reloadAll() {
      await Promise.all([
        this.loadDashboard(),
        this.loadDecisions(),
        this.loadAudit(),
        this.loadErrors(),
      ]);
    },

    async loadDashboard() {
      const res = await fetch("/api/session/dashboard");
      const data = await res.json();
      this.dashboard = data;
      this.session.loaded = data.loaded === true;
    },

    async loadDecisions() {
      const res = await fetch("/api/session/decisions?status=pending");
      const data = await res.json();
      this.decisions = data.decisions ?? [];
      // v62: session-level LTMC overrides set via "Set LTMC default value"
      // actions. Surfaced as chips under the Decisions panel header so the
      // SME can see at a glance which fields they've defaulted (e.g.
      // ALAND=IN, WAERS=INR, BWKEY=PE01) before exporting LTMC.
      this.ltmcOverrides = data.ltmc_overrides ?? {};
    },

    async loadAudit() {
      const res = await fetch("/api/session/audit");
      const data = await res.json();
      this.audit.log = data.log ?? [];
      this.audit.persistent = data.persistent ?? [];
    },

    async loadErrors() {
      // Backend paginates this endpoint (default limit 10000). For SD/MM
      // sessions the count rarely exceeds a few thousand so the first
      // page is the whole set. For PP/Routing sessions a Routing file
      // can produce 100k+ errors — we show the first page and tell the
      // user the rest are still on the server, fixable in the source
      // file (PP doesn't yet support inline editing).
      const res = await fetch("/api/session/errors?limit=10000");
      const data = await res.json();
      this.allErrors = data.errors ?? [];
      this.errorsTruncated = !!data.truncated;
      this.errorsTotalCount = data.total ?? this.allErrors.length;
      if (data.truncated && !this._toastedTruncation) {
        this._toastedTruncation = true;
        this.toast(
          `Showing first ${this.allErrors.length.toLocaleString()} of ` +
          `${this.errorsTotalCount.toLocaleString()} errors. ` +
          `Fix issues in the source file and re-upload to see the rest.`,
          "warn",
          "alert-triangle",
        );
      }
      this.recomputeGridCache();
    },

    filteredDecisions() {
      let list = this.decisions;
      if (this.filter !== "all") list = list.filter(d => d.kind === this.filter);
      if (this.search) {
        const q = this.search.toLowerCase();
        list = list.filter(d =>
          (d.rule_name + " " + d.column_label + " " + d.sheet + " " + (d.sample_value ?? "")).toLowerCase().includes(q)
        );
      }
      return list;
    },

    /** Recompute the filtered errors and cache the result. Called only
     *  when filters change or the error set reloads — not on every
     *  template read. This is the single big win for Error Grid perf. */
    recomputeGridCache() {
      let list = this.allErrors;
      if (this.gridFilter.sheet) list = list.filter(e => e.sheet === this.gridFilter.sheet);
      if (this.gridFilter.rule)  list = list.filter(e => e.rule_name === this.gridFilter.rule);
      if (this.gridFilter.search) {
        const q = this.gridFilter.search.toLowerCase();
        list = list.filter(e =>
          (e.column_label + " " + e.value + " " + e.message).toLowerCase().includes(q)
        );
      }
      this.gridCache.total = list.length;
      this.gridCache.rows = list.slice(0, 500);
      this.gridCache.truncated = list.length > 500;
    },

    /** Debounced search trigger — user types fast, we scan once 150ms
     *  after they stop. Keeps the UI responsive while typing. */
    onGridSearchInput() {
      clearTimeout(this.gridCache.searchDebounce);
      this.gridCache.searchDebounce = setTimeout(() => {
        this.recomputeGridCache();
      }, 150);
    },

    uniqueRules() {
      const s = new Set();
      this.allErrors.forEach(e => s.add(e.rule_name));
      return Array.from(s).sort();
    },

    reductionRatio() {
      const e = this.dashboard.pending_errors ?? 0;
      const d = this.dashboard.pending_decisions ?? 0;
      return d === 0 ? 0 : Math.round(e / d);
    },

    patternCount() { return this.decisions.filter(d => d.kind === "pattern").length; },
    individualCount() { return this.decisions.filter(d => d.kind === "individual").length; },

    getFileStatus() {
      const file = this.repo.files.find(f => f.file_id === this.dashboard.file_id);
      return file?.status ?? "in_progress";
    },

    // ─── Decision actions ────────────────────────────────
    // Holds the pending decision action. Invoked by the Apply button via onActionConfirm().
    _actionPending: null,

    async triggerAction(decision, action) {
      if (action.id === "review" || action.id === "navigate") {
        this.previewRows(decision);
        return;
      }

      const isDelete = action.id === "delete_duplicates";
      // If the action carries a fixed_value (e.g. Set as URP always writes
      // 'URP'), we DO NOT prompt for a value — the modal just confirms the
      // action and collects the business reason. Reduces unnecessary typing.
      const hasFixedValue = action.fixed_value !== undefined && action.fixed_value !== null;
      const needsValue = !hasFixedValue && (
        action.requires_value || action.kind === "bulk_replace" || action.kind === "bulk_fill"
      );
      // Every mutating bulk action requires a business reason
      const needsReason = true;

      this._actionPending = { decision, action, needsValue, needsReason };
      this.modal = {
        type: "action",
        title: action.label,
        subtitle: `Affects ${decision.affected_count.toLocaleString()} rows in ${decision.sheet}`,
        requiresValue: needsValue,
        requiresReason: needsReason,
        // Pre-fill value for fixed_value actions so the POST body carries 'URP'.
        value: hasFixedValue ? action.fixed_value : "",
        reason: "",
        confirmLabel: isDelete ? "Delete duplicates" : `Apply to ${decision.affected_count.toLocaleString()} rows`,
        topSuggestion: null,   // populated below for catalog-backed decisions
      };

      // For catalog-backed rules (inco_location_description, *_not_in_kds),
      // fetch the groups info to surface the top KDS suggestion under the
      // input — same purple chip pattern as Group & Replace. We only call
      // this when a value is needed AND the decision has a catalog; avoids
      // a pointless network roundtrip for clear/delete/fixed-value actions.
      if (needsValue && decision.is_categorical) {
        try {
          const encId = encodeURIComponent(decision.decision_id);
          const res = await fetch(`/api/session/decisions/${encId}/groups`);
          if (res.ok) {
            const data = await res.json();
            if (data.has_catalog && data.groups?.length > 0) {
              // Use the top bucket's top suggestion. For bulk_replace on
              // a catalog-backed decision, the most common bad value is
              // usually what the user wants the replacement to target.
              const topGroup = data.groups[0];
              const topSug = topGroup.suggestions?.[0];
              if (topSug) {
                // Guard: if the modal has been closed while the fetch was
                // in flight, don't step on a different modal's state.
                if (this.modal.type === "action") {
                  this.modal.topSuggestion = topSug;
                }
              }
            }
          }
        } catch (e) {
          // Suggestion is a nice-to-have; silent failure is fine here
        }
      }
    },

    /** Called by the Apply button in the Decision Action modal. */
    async onActionConfirm() {
      const pending = this._actionPending;
      if (!pending) return;  // already handled or never set
      // Claim the pending state immediately so double-fire (Alpine + document delegation) is safe
      this._actionPending = null;

      const { decision, action, needsValue, needsReason } = pending;

      if (needsValue && !(this.modal.value || "").trim()) {
        // Restore pending so user can try again
        this._actionPending = pending;
        this.toast("Enter a value", "warn", "alert-triangle");
        return;
      }
      if (needsReason && !(this.modal.reason || "").trim()) {
        this._actionPending = pending;
        this.toast("A business reason is required for this action", "warn", "alert-triangle");
        return;
      }

      const payload = {
        decision_id: decision.decision_id,
        action_id: action.id,
        value: this.modal.value,
        reason: this.modal.reason,
        strategy: this.modal.strategy,
      };
      this.closeModal();

      await this.runBusy(`apply:${decision.decision_id}`, async () => {
        try {
          const res = await fetch("/api/session/decisions/apply", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload),
          });
          if (!res.ok) throw new Error((await res.json()).detail);
          await this.reloadAll();
          // v62: set_ltmc_default doesn't mutate per-row data — it stores
          // a session override that flows into the LTMC export. Use a
          // wording that reflects that ("set as default") rather than
          // the generic "applied to N rows" which is misleading here.
          if (action.id === "set_ltmc_default") {
            this.toast(
              `${decision.sap_field || decision.column_label} set to "${this.modal.value || payload.value}" — will populate every row in LTMC export`,
              "success", "check-circle-2"
            );
          } else {
            this.toast(`Applied to ${decision.affected_count.toLocaleString()} rows`, "success", "check-circle-2");
          }
        } catch (e) {
          this.toast(e.message, "error", "x");
        }
      });
    },

    async previewRows(decision) {
      await this.runBusy(`preview:${decision.decision_id}`, async () => {
        const encId = encodeURIComponent(decision.decision_id);
        const res = await fetch(`/api/session/errors_by_rule/${encId}`);
        const data = await res.json();
        this.modal = {
          type: "preview",
          previewRows: data.rows,
          keyColumns: data.key_columns,
          isDuplicate: data.is_duplicate,
          hasSalesAreas: data.has_sales_areas,
          ruleName: data.rule_name,
          columnLabel: data.column_label,
          severity: data.severity,
          subtitle: decision.rule_name + " · " + decision.column_label,
          currentDecision: decision,
          selectedRows: [],
          truncated: data.truncated,
          totalCount: data.total_count,
          shownCount: data.shown_count,
          flaggedTotal: decision.affected_count,  // total flagged duplicates (unchanged by truncation)
        };
      });
    },

    /** Extract the label from a key-column entry. Preview endpoint sends
     *  key_columns as either:
     *    SD: array of strings            ["Customer Number", "Name", ...]
     *    MM: array of {col_idx, label}   [{col_idx: 1, label: "MATNR"}, ...]
     *  The duplicate-view header already iterates over strings (from SD).
     *  The MM preview branch below needs both shapes handled defensively.
     *  Returns the label string either way; empty string if shape is off. */
    mmColLabel(col) {
      if (col == null) return "";
      if (typeof col === "string") return col;
      if (typeof col === "object") return col.label ?? "";
      return String(col);
    },

    // ─── Group & Replace ─────────────────────────────────────
    /** Open the group-by-value modal for a decision. Fetches distinct
     *  bad values + counts + KDS suggestions from the server, then
     *  shows the per-value replacement grid.
     *
     *  The modal has two phases: 'edit' (user builds replacements)
     *  and 'preview' (summary shown before Apply). Starts in 'edit'. */
    async openGroupReplace(decision) {
      await this.runBusy(`grp:${decision.decision_id}`, async () => {
        const encId = encodeURIComponent(decision.decision_id);
        const res = await fetch(`/api/session/decisions/${encId}/groups`);
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          this.toast(err.detail || "Couldn't load groups", "error", "x");
          return;
        }
        const data = await res.json();
        // Annotate each group with _ticked / _replace for the form.
        // Start all un-ticked — users opt in per row; prevents
        // accidental "applied everything by default".
        data.groups.forEach(g => {
          g._ticked = false;
          g._replace = "";
        });
        this.modal = {
          type: "group_replace",
          phase: "edit",
          currentDecision: decision,
          groups: data,
          autoFill: false,
          groupSearch: "",
          groupReason: "",
          allTicked: false,
        };
      });
    },

    /** Fill EVERY row's Replace with its top KDS suggestion (if it has one).
     *  Rows that already have a user-entered value are left alone — we never
     *  clobber manual edits. Rows with no suggestion are skipped.
     *  Called from the "Apply KDS to all" button at the top of the edit phase. */
    applyKdsToAll() {
      const groups = this.modal.groups?.groups ?? [];
      let filled = 0;
      for (const g of groups) {
        const suggestion = g.suggestions?.[0];
        if (!suggestion) continue;
        if ((g._replace ?? "") !== "") continue;   // don't clobber user edits
        g._replace = suggestion.code;
        g._ticked = true;
        filled += 1;
      }
      if (filled === 0) {
        this.toast("No KDS suggestions to apply", "info", "info");
      } else {
        this.toast(`Filled ${filled} suggestion${filled === 1 ? "" : "s"}`,
                   "success", "check");
      }
    },

    /** True if at least one visible group has a KDS suggestion — controls
     *  whether the "Apply KDS to all" top button should render. */
    groupHasAnyKdsSuggestion() {
      const groups = this.modal.groups?.groups ?? [];
      return groups.some(g => (g.suggestions ?? []).length > 0);
    },

    /** Tick or untick every visible row. Visible = current search filter. */
    toggleAllGroups(checked) {
      this.modal.allTicked = checked;
      for (const g of this.filteredGroupRows()) {
        g._ticked = checked;
      }
    },

    /** Return the groups currently visible given the search filter.
     *  Referenced by x-for in the template — computed each render. */
    filteredGroupRows() {
      const groups = this.modal.groups?.groups ?? [];
      const q = (this.modal.groupSearch ?? "").trim().toLowerCase();
      if (!q) return groups;
      return groups.filter(g => (g.value ?? "").toLowerCase().includes(q));
    },

    /** Groups that are ready to apply (ticked AND have a replacement).
     *  Used to render the preview-phase summary table and to count
     *  rule-count / cell-count for the footer status line. */
    groupRowsReady() {
      const groups = this.modal.groups?.groups ?? [];
      return groups.filter(g => g._ticked && (g._replace ?? "") !== "" && g._replace !== g.value);
    },

    /** Count of distinct replacement rules that will be applied. */
    groupRulesToApply() {
      return this.groupRowsReady().length;
    },

    /** Total cells that would change across all ready rules. */
    groupCellsToChange() {
      return this.groupRowsReady().reduce((sum, g) => sum + (g.count ?? 0), 0);
    },

    /** Post the replacements to the server. Called from the preview
     *  phase; the preview step itself is a client-side summary, not an
     *  extra round trip. Server still enforces reason-required for
     *  batches > 10 via confirm=true. */
    async applyGroupReplace() {
      const decision = this.modal.currentDecision;
      if (!decision) return;
      const replacements = this.groupRowsReady()
        .map(g => ({ find: g.value, replace: g._replace }));
      if (replacements.length === 0) return;

      const totalCells = this.groupCellsToChange();
      const needsReason = totalCells > 10;
      if (needsReason && !(this.modal.groupReason ?? "").trim()) {
        this.toast("Please enter a reason — more than 10 cells will change", "warn", "alert-circle");
        return;
      }

      await this.runBusy("grp-apply", async () => {
        const encId = encodeURIComponent(decision.decision_id);
        const res = await fetch(`/api/session/decisions/${encId}/group_replace`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            replacements,
            reason: this.modal.groupReason ?? "",
            confirm: true,
          }),
        });
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          this.toast(err.detail || "Replace failed", "error", "x");
          return;
        }
        const data = await res.json();
        this.closeModal();
        this.toast(
          `Replaced ${data.replaced_count.toLocaleString()} cells across ${data.distinct_values_replaced} distinct value${data.distinct_values_replaced === 1 ? "" : "s"}`,
          "success", "check-circle-2"
        );
        await Promise.all([
          this.loadDashboard(),
          this.loadDecisions(),
          this.loadErrors(),
          this.loadAudit(),
        ]);
      });
    },

    /** Delete all flagged duplicate rows for this decision in one go (bypasses truncation). */
    async deleteAllFlagged() {
      const decision = this.modal.currentDecision;
      if (!decision) return;
      const total = decision.affected_count;
      this.closeModal();
      const { ok, reason } = await this.ui_confirm({
        title: `Delete all ${total.toLocaleString()} flagged duplicate rows?`,
        message: "This removes every row currently flagged as a duplicate for this decision. Use this when you trust the keep-first strategy and don't need to review each row.",
        confirmLabel: `Delete ${total.toLocaleString()} rows`,
        tone: "danger",
        icon: "trash-2",
        reasonRequired: true,
      });
      if (!ok) return;
      await this.runBusy("delete_all_flagged", async () => {
        try {
          // Use the existing bulk_delete decision action — delete_duplicates with strategy keep_first
          const res = await fetch("/api/session/decisions/apply", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              decision_id: decision.decision_id,
              action_id: "delete_duplicates",
              strategy: "keep_first",
              reason: reason,
            }),
          });
          if (!res.ok) throw new Error((await res.json()).detail);
          this.toast(`Deleted ${total.toLocaleString()} flagged duplicates`, "success", "check-circle-2");
          await this.reloadAll();
        } catch (e) {
          this.toast(e.message, "error", "x");
        }
      });
    },

    async openRecordFromModal(rowIdx) {
      const decision = this.modal.currentDecision;
      const sheet = decision?.sheet || this.modal.previewRows?.[0]?.sheet;
      const did = decision?.decision_id ?? null;
      this.closeModal();
      await this.openRecord(sheet, rowIdx, did);
    },

    // Bulk selection in preview modal
    toggleRowSelection(rowIdx) {
      const sel = this.modal.selectedRows ?? [];
      const i = sel.indexOf(rowIdx);
      if (i >= 0) sel.splice(i, 1);
      else sel.push(rowIdx);
      this.modal.selectedRows = [...sel];
    },

    selectAllDupes() {
      this.modal.selectedRows = (this.modal.previewRows ?? [])
        .filter(r => r.is_flagged_duplicate)
        .map(r => r.row_idx);
    },

    /** Select every row EXCEPT the "BEST" one in each cluster.
     *  Handy when you trust the completeness-score heuristic. */
    selectAllNonBest() {
      this.modal.selectedRows = (this.modal.previewRows ?? [])
        .filter(r => r.is_best !== true)
        .map(r => r.row_idx);
    },

    selectAllVisible() {
      this.modal.selectedRows = (this.modal.previewRows ?? []).map(r => r.row_idx);
    },

    selectNone() {
      this.modal.selectedRows = [];
    },

    async bulkDeleteSelected() {
      const selected = this.modal.selectedRows ?? [];
      if (selected.length === 0) {
        this.toast("No rows selected", "warn", "alert-triangle");
        return;
      }
      const sheet = this.modal.currentDecision?.sheet;
      // Close the preview modal first so the confirm modal takes over
      const savedDecision = this.modal.currentDecision;
      this.closeModal();
      const { ok, reason } = await this.ui_confirm({
        title: `Delete ${selected.length} duplicate row${selected.length === 1 ? "" : "s"}?`,
        message: "The rows will be removed from the file. You can undo this from Recent Activity until you close the file.",
        confirmLabel: `Delete ${selected.length} row${selected.length === 1 ? "" : "s"}`,
        tone: "danger",
        icon: "trash-2",
        reasonRequired: true,
      });
      if (!ok) return;
      await this.runBusy("bulk_delete", async () => {
        try {
          const res = await fetch("/api/session/records/bulk_delete", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              sheet: sheet,
              row_indexes: selected,
              reason: reason,
            }),
          });
          if (!res.ok) throw new Error((await res.json()).detail);
          const result = await res.json();
          this.toast(`Deleted ${result.deleted_count} rows`, "success", "check-circle-2");
          await this.reloadAll();
          // Refresh the preview on the same decision if it still has rows
          if (savedDecision && result.deleted_count > 0) {
            const stillExists = (this.decisions ?? []).find(d => d.decision_id === savedDecision.decision_id);
            if (stillExists) await this.previewRows(stillExists);
          }
        } catch (e) {
          this.toast(e.message, "error", "x");
        }
      });
    },

    // ─── Record editor ───────────────────────────────────
    async openRecord(sheet, rowIdx, decisionId = null) {
      this.busyRow = `${sheet}:${rowIdx}`;
      await this.runBusy(`record:${sheet}:${rowIdx}`, async () => {
        // When entering via a decision (Fix Individually or Preview edit),
        // pass the decision_id so j/k navigation stays within rows flagged
        // by that rule — walking ALL sheet errors is disorienting when
        // the user is focused on one issue type.
        const qs = decisionId ? `?decision_id=${encodeURIComponent(decisionId)}` : "";
        const res = await fetch(`/api/session/records/${encodeURIComponent(sheet)}/${rowIdx}${qs}`);
        if (!res.ok) {
          this.toast("Record not found", "error", "x");
          return;
        }
        const data = await res.json();
        // Remember the decision scope so subsequent navigations (j/k,
        // saveAndNext) preserve it without the caller having to pass it
        // every time.
        this.record.scopedDecisionId = decisionId;
        this.record.data = data;
        this.record.edits = {};
        this.record.showAll = data.errors.length === 0;

        const all = Object.values(data.groups ?? {}).flat();
        const nameField = all.find(f => f.label === "Name");
        const numField = all.find(f => f.label === "Customer Number");
        this.record.customerName = nameField?.value ?? "";
        this.record.customerNum = numField?.value ? `Customer ${numField.value}` : `Row ${data.xml_row}`;

        this.setView("records");
      });
      this.busyRow = null;
    },

    // ─── Navigate-to-record helpers for Changes Summary + Audit Trail ───
    // Bulk actions (replace_with across 126 rows, delete_duplicates) don't
    // point to a single row, so those rows are non-clickable.

    /** Start a "Fix individually" flow. We fetch the full list of affected
     *  rows for this decision and open the first one, while recording the
     *  rest as a work queue. The record editor's top banner shows progress
     *  ("Row 1 of 1,065") and the footer bar exposes Save & Next.
     *
     *  Flow state is cleared on any other navigation (back to decisions,
     *  a jump from Changes Summary, closing the file). That keeps the
     *  banner from appearing in contexts where it's misleading.
     *
     *  v66: For cross-file decisions (sheet=LongText or AlternateUnits) the
     *  Records editor cannot show or edit the source row — those rows live
     *  in the alt-UoM or long-text xlsx file, not in the merged-materials
     *  bundle. Calling openRecord() with sheet=LongText returned 404 →
     *  "Record not found" toast. We now short-circuit to a guidance modal
     *  that tells the SME exactly what to fix in which file and asks them
     *  to re-upload. */
    async fixIndividually(decision) {
      // v66: cross-file decisions get a different UX — show guidance
      // instead of navigating to a Records editor that has no concept
      // of LongText / AlternateUnits source rows.
      const CROSS_FILE_SHEETS = new Set(["LongText", "AlternateUnits"]);
      if (CROSS_FILE_SHEETS.has(decision.sheet)) {
        await this.openCrossFileGuidance(decision);
        return;
      }
      await this.runBusy(`fixind:${decision.decision_id}`, async () => {
        try {
          const enc = encodeURIComponent(decision.decision_id);
          const res = await fetch(`/api/session/errors_by_rule/${enc}`);
          if (!res.ok) {
            this.toast("Could not find affected rows", "error", "x");
            return;
          }
          const data = await res.json();
          const rows = data.rows ?? [];
          if (rows.length === 0) {
            this.toast("No rows affected — this decision may already be resolved", "info", "info");
            return;
          }
          const sheet = decision.sheet;
          if (!sheet) {
            this.toast("Decision has no navigable sheet", "warn", "info");
            return;
          }
          // Store the full ordered list so we can advance without re-fetching
          // on every save. We track the ORIGINAL total so "Row 5 of 100" stays
          // stable even as the underlying error set shrinks during the flow.
          this.record.flow = {
            decision: decision,
            sheet: sheet,
            // When in a flow, we focus the error banner on ONLY the rule
            // being fixed. Other unrelated errors on the same row still
            // exist but aren't shown — reduces cognitive load and keeps
            // the SME on the task they clicked into. All errors come
            // back as soon as they exit the flow.
            focusRuleId: decision.rule_id,
            focusColIdx: decision.col_idx,
            rowList: rows.map(r => r.row_idx).filter(i => i !== undefined && i !== null),
            originalTotal: data.total_count ?? rows.length,
            startedAt: Date.now(),
          };
          await this.openRecord(sheet, this.record.flow.rowList[0], decision.decision_id);
        } catch (e) {
          this.toast("Open failed: " + e.message, "error", "x");
        }
      });
    },

    /** v66: Guidance modal for cross-file decisions (LongText, AlternateUnits).
     *  These errors are in source xlsx files that the Records editor can't
     *  open — the fix is to edit the source file in Excel and re-upload.
     *  This method fetches the affected rows (so we can show MATNRs and
     *  values) and populates a modal that walks the SME through the fix. */
    async openCrossFileGuidance(decision) {
      await this.runBusy(`fixind:${decision.decision_id}`, async () => {
        try {
          const enc = encodeURIComponent(decision.decision_id);
          const res = await fetch(`/api/session/errors_by_rule/${enc}`);
          if (!res.ok) {
            this.toast("Could not load affected rows", "error", "x");
            return;
          }
          const data = await res.json();
          const rows = data.rows ?? [];
          // Friendly file name for the modal title — drives "open your X file"
          // copy below. Stays in sync with the cross-file validator's sheet
          // names (LongText / AlternateUnits).
          const fileLabel = decision.sheet === "LongText"
            ? "Long Text"
            : "Alternate Units of Measure";
          this.crossFileModal = {
            open: true,
            decision: decision,
            fileLabel: fileLabel,
            rows: rows.slice(0, 50),    // cap for display; full list still in audit
            totalCount: data.total_count ?? rows.length,
          };
        } catch (e) {
          this.toast("Failed to load guidance: " + e.message, "error", "x");
        }
      });
    },

    closeCrossFileModal() {
      this.crossFileModal = { open: false };
    },

    /** True if we're currently in a Fix Individually flow with more
     *  rows remaining. Used to show the decision banner + footer bar.
     *
     *  Also falsy when the user has navigated (via j/k or Next row) to
     *  a row that isn't part of the flow — that "wandered out of scope"
     *  case should behave like the flow ended. Otherwise the banner
     *  shows the decision name + "Row 0 of 6" while displaying a row
     *  whose errors have nothing to do with that decision (confusing;
     *  reported by SME with screenshot). */
    inFixFlow() {
      if (!(this.record.flow && this.record.data)) return false;
      const list = this.record.flow.rowList ?? [];
      const cur = this.record.data.row_idx;
      return list.includes(cur);
    },

    /** Current position in the flow (1-indexed for display). */
    flowPosition() {
      if (!this.record.flow || !this.record.data) return 0;
      const list = this.record.flow.rowList;
      const cur = this.record.data.row_idx;
      const idx = list.indexOf(cur);
      return idx >= 0 ? idx + 1 : 0;
    },

    /** How many rows still need fixing after the current one, based on
     *  the ORIGINAL list we captured when the flow started. Even if some
     *  rows got auto-resolved by other actions, this gives a stable
     *  "remaining" count the user can trust. */
    flowRemaining() {
      if (!this.record.flow || !this.record.data) return 0;
      const list = this.record.flow.rowList;
      const cur = this.record.data.row_idx;
      const idx = list.indexOf(cur);
      if (idx < 0) return list.length;
      return Math.max(0, list.length - idx - 1);
    },

    /** Row N of M for the top banner. */
    flowProgressLabel() {
      const pos = this.flowPosition();
      const total = this.record.flow?.rowList?.length ?? 0;
      return `Row ${pos.toLocaleString()} of ${total.toLocaleString()}`;
    },

    /** Save current edits (if any), then jump to the next row in the
     *  flow. If this is the last row, we exit the flow and return to
     *  Decisions with a success toast. */
    async saveAndNext() {
      const hasEdits = Object.keys(this.record.edits).length > 0;
      if (hasEdits) {
        // Build col_idx → sap_field map (see saveRecord for why)
        const sapFieldByCol = {};
        for (const e of (this.record.data.errors ?? [])) {
          if (e.col_idx != null && e.sap_field) {
            sapFieldByCol[e.col_idx] = e.sap_field;
          }
        }
        // Save silently — no toast, because we're chaining to a navigation
        // and the toast would stack with the "moved to next row" feedback.
        try {
          const promises = Object.entries(this.record.edits).map(([colIdx, value]) =>
            fetch("/api/session/records/edit", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({
                sheet: this.record.data.sheet,
                row_idx: this.record.data.row_idx,
                col_idx: parseInt(colIdx),
                value: value,
                sap_field: sapFieldByCol[parseInt(colIdx)] || null,
              }),
            })
          );
          await Promise.all(promises);
        } catch (e) {
          this.toast("Save failed: " + e.message, "error", "x");
          return;
        }
      }
      await this.advanceFlow();
    },

    /** Skip the current row without saving. For rows where the user
     *  decides they're not the right person to fix it, or wants to
     *  come back later. */
    async skipAndNext() {
      // Discard pending edits — user explicitly asked to skip.
      this.record.edits = {};
      await this.advanceFlow();
    },

    /** Move to the next row in the flow, or exit if done. */
    async advanceFlow() {
      if (!this.record.flow) {
        // Flow was exited some other way; nothing to advance.
        return;
      }
      const list = this.record.flow.rowList;
      const cur = this.record.data?.row_idx;
      const curIdx = list.indexOf(cur);
      const nextIdx = curIdx + 1;

      if (nextIdx >= list.length) {
        // We finished the last row in the captured list.
        const decisionName = this.record.flow.decision?.rule_name ?? "this decision";
        this.exitFlow();
        await Promise.all([this.loadDashboard(), this.loadDecisions(), this.loadErrors()]);
        this.setView("decisions");
        this.toast(`Finished fixing "${decisionName}" · review remaining decisions`, "success", "check-circle-2");
        return;
      }

      const nextRow = list[nextIdx];
      // Refresh dashboard in background so pending counts stay accurate.
      this.loadDashboard();
      this.loadDecisions();
      // Preserve decision_id scope — nextRow is still part of this flow
      await this.openRecord(this.record.flow.sheet, nextRow, this.record.flow.decision?.decision_id);
    },

    /** Clear the flow context. Called when user navigates away
     *  (back to decisions, sidebar click, close file, etc.). */
    exitFlow() {
      this.record.flow = null;
    },

    /** Back to decisions — convenience; exits the flow first. */
    backToDecisions() {
      this.exitFlow();
      this.setView("decisions");
    },
    canJumpToRecord(change) {
      return !!(change && change.sheet && change.row_idx !== null && change.row_idx !== undefined && change.row_idx >= 0);
    },
    async jumpToRecordFromChange(change) {
      if (!this.canJumpToRecord(change)) return;
      if (!this.session.loaded) {
        this.toast("Open the file first to view this record", "warn", "info");
        return;
      }
      await this.openRecord(change.sheet, change.row_idx);
    },
    /** Audit entry navigability: we need sheet + details.row_idx,
     *  which only single-cell edits and single-row deletes carry. */
    canJumpToRecordFromAudit(entry) {
      if (!entry || !entry.sheet) return false;
      const d = entry.details ?? {};
      const rowIdx = d.row_idx;
      return rowIdx !== null && rowIdx !== undefined && rowIdx >= 0;
    },
    async jumpToRecordFromAudit(entry) {
      if (!this.canJumpToRecordFromAudit(entry)) return;
      // Audit trail can span multiple files — only jump if this entry
      // belongs to the currently-loaded file.
      if (!this.session.loaded || (entry.file_id && entry.file_id !== this.dashboard.file_id)) {
        this.toast("Open that file first to view this record", "warn", "info");
        return;
      }
      await this.openRecord(entry.sheet, entry.details.row_idx);
    },

    recordTitle() {
      return this.record.customerName || ("Row " + this.record.data?.xml_row);
    },

    allFieldCount() {
      const groups = this.record.data?.groups ?? {};
      return Object.values(groups).reduce((sum, arr) => sum + arr.length, 0);
    },

    getFieldLen(colIdx) {
      const groups = this.record.data?.groups ?? {};
      for (const fields of Object.values(groups)) {
        const f = fields.find(x => x.col_idx === colIdx);
        if (f) return f.ete_length;
      }
      return null;
    },

    // ─── Smart editor helpers ────────────────────────────
    // Date fields come back as ISO strings with time component like
    // "2025-10-28T00:00:00.000". The <input type="date"> widget needs YYYY-MM-DD.
    toDateValue(raw) {
      if (!raw) return "";
      const s = String(raw).trim();
      if (!s) return "";
      // Already YYYY-MM-DD? perfect
      if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return s;
      // ISO with time — take the date portion
      const m = s.match(/^(\d{4}-\d{2}-\d{2})/);
      if (m) return m[1];
      // Try Date.parse for fallbacks like "28-Oct-2025"
      const d = new Date(s);
      if (!isNaN(d.getTime())) {
        return d.toISOString().substring(0, 10);
      }
      return "";
    },

    // When the user picks a date in the picker, we need to emit it back in
    // SAP's expected ISO format with time. Most templates accept ISO-with-T;
    // matching that minimizes the chance of re-formatting by SAP.
    fromDateValue(val) {
      if (!val) return "";
      // Always emit midnight UTC (LTMC tolerates no-time and with-time).
      return val + "T00:00:00.000";
    },

    dateAddYears(fromDate, years) {
      const d = new Date(fromDate);
      d.setFullYear(d.getFullYear() + years);
      const iso = d.toISOString().substring(0, 10);
      return iso + "T00:00:00.000";
    },

    async loadRefData() {
      try {
        const [s, c] = await Promise.all([
          fetch("/api/reference/states/IN").then(r => r.json()).catch(() => ({items: []})),
          fetch("/api/reference/countries").then(r => r.json()).catch(() => ({items: []})),
        ]);
        this.refData.indiaStates = s.items ?? [];
        this.refData.countries = c.items ?? [];
        this.refData.loaded = true;
      } catch {
        this.refData.loaded = true;  // fall through to plain text input
      }
    },

    // Decide which smart editor to use for a given error.
    // Returns one of: "date", "state_in", "country", "length", "text"
    errorEditorType(err) {
      if (!err) return "text";
      const label = (err.column_label || "").toLowerCase();
      if (err.ete_type === "D") return "date";
      // State field — only the one after Country/Region, paired with rule invalid_state_in
      if (err.rule_id === "invalid_state_in" || label === "state") return "state_in";
      if (label === "country/region" || label === "country") return "country";
      if (err.rule_id === "length_exceeded" && err.ete_length) return "length";
      return "text";
    },

    /** Human-readable format/guidance hint shown below the error message.
     *  Complements the red error message with a constructive "here's what to type" line. */
    errorFormatHint(err) {
      if (!err) return "";
      const r = err.rule_id;
      if (r === "invalid_pan")       return "Format: 5 letters · 4 digits · 1 letter (example: ABCDE1234F)";
      if (r === "gstin_length")      return "15 characters: 2-digit state + 10-char PAN + entity + Z + check digit";
      if (r === "gstin_format")      return "Format: NNAAAAA9999A1ZN (example: 29ABCDE1234F1Z5)";
      if (r === "invalid_state_in")  return "Pick from the dropdown · two-digit GST code (01 Jammu & Kashmir ... 38 Ladakh)";
      if (r === "dl_expired")        return "Enter a future date or use +1 year to extend from the existing expiry";
      if (r === "length_exceeded" && err.ete_length) return `Max ${err.ete_length} characters · use Truncate to cut to allowed length`;
      if (r === "junk_value")        return "Clear the field or replace with a meaningful value";
      if (r === "mandatory_missing") return "This field is required — cannot be blank for this record type";
      if (r === "inco_location_description") return "Must match the KDS description for this Incoterm (click Apply above).";
      if (r === "export_postal_code_long") return "Max 10 characters for export customers";
      return "";
    },

    /** Errors shown in the record editor banner. When inside a Fix-
     *  Individually flow we filter to the decision's rule so the user
     *  focuses on one fix per row. Outside a flow, all errors on the
     *  row are shown. */
    focusedErrors() {
      const all = this.record.data?.errors ?? [];
      if (!this.record.flow) return all;
      const targetRule = this.record.flow.focusRuleId;
      const targetCol = this.record.flow.focusColIdx;
      // Match on rule_id AND column — handles edge cases where same rule
      // fires on multiple columns (rare, but possible). If none match
      // (e.g. the row had this rule fired under a different column),
      // fall back to showing all errors so the user still sees something.
      const filtered = all.filter(e =>
        e.rule_id === targetRule && (!targetCol || e.col_idx === targetCol)
      );
      return filtered.length > 0 ? filtered : all;
    },

    /** Look up the char-issue entry for a given position on a PAN/GSTIN
     *  error, or return null. Used by the character-grid template to
     *  decide whether to paint a cell red. */
    charIssueAt(err, i) {
      if (!err || !err.char_issues) return null;
      for (const iss of err.char_issues) {
        if (iss.pos === i) return iss;
      }
      return null;
    },

    async navigateError(delta) {
      if (!this.record.data) return;
      const target = delta > 0 ? this.record.data.next_error_row : this.record.data.prev_error_row;
      if (target === null || target === undefined) {
        this.toast(delta > 0 ? "No more errors" : "At first error", "info", "info");
        return;
      }
      // Preserve the decision scope so navigation stays on this rule's rows
      await this.openRecord(this.record.data.sheet, target, this.record.scopedDecisionId);
    },

    async navigateRow(delta) {
      if (!this.record.data) return;
      const newIdx = this.record.data.row_idx + delta;
      if (newIdx < 0 || newIdx >= this.record.data.total_rows_in_sheet) return;
      await this.openRecord(this.record.data.sheet, newIdx, this.record.scopedDecisionId);
    },

    async deleteCurrentRow() {
      if (!this.record.data) return;
      const { ok, reason } = await this.ui_confirm({
        title: "Delete this row?",
        message: "The row will be removed from the file. You can undo this from Recent Activity until you close the file.",
        confirmLabel: "Delete row",
        tone: "danger",
        icon: "trash-2",
        reasonRequired: true,
      });
      if (!ok) return;
      const sheet = this.record.data.sheet;
      const rowIdx = this.record.data.row_idx;
      await this.runBusy(`del_row:${sheet}:${rowIdx}`, async () => {
        try {
          const res = await fetch(
            `/api/session/records/${encodeURIComponent(sheet)}/${rowIdx}?reason=${encodeURIComponent(reason)}`,
            { method: "DELETE" }
          );
          if (!res.ok) throw new Error((await res.json()).detail);
          await this.reloadAll();
          this.toast("Row deleted", "success", "check-circle-2");
          this.record.data = null;
          this.setView("decisions");
        } catch (e) {
          this.toast(e.message, "error", "x");
        }
      });
    },

    async saveRecord() {
      if (!this.record.data || Object.keys(this.record.edits).length === 0) {
        this.toast("No changes to save", "info", "info");
        return;
      }
      // Build col_idx → sap_field map from the record's errors. For LTMC-
      // default fields (SPRAS, ALAND, WAERS, CURTP, BWKEY) the col_idx is
      // a synthetic value past the end of the source columns; the backend
      // can't reverse-map it without the explicit sap_field. For real
      // source-column edits, sap_field is harmless redundancy.
      const sapFieldByCol = {};
      for (const e of (this.record.data.errors ?? [])) {
        if (e.col_idx != null && e.sap_field) {
          sapFieldByCol[e.col_idx] = e.sap_field;
        }
      }
      await this.runBusy("save:record", async () => {
        const promises = Object.entries(this.record.edits).map(([colIdx, value]) =>
          fetch("/api/session/records/edit", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              sheet: this.record.data.sheet,
              row_idx: this.record.data.row_idx,
              col_idx: parseInt(colIdx),
              value: value,
              // sap_field needed for LTMC-default columns not in source.
              // Optional; backend ignores if it can resolve via col_idx.
              sap_field: sapFieldByCol[parseInt(colIdx)] || null,
            }),
          })
        );
        try {
          await Promise.all(promises);
          this.toast(`Saved ${promises.length} field(s)`, "success", "check");
          await this.openRecord(this.record.data.sheet, this.record.data.row_idx, this.record.scopedDecisionId);
          await Promise.all([this.loadDashboard(), this.loadDecisions(), this.loadAudit(), this.loadErrors()]);
        } catch (e) {
          this.toast("Save failed: " + e.message, "error", "x");
        }
      });
    },

    // ─── Undo ────────────────────────────────────────────
    canUndo(entry) {
      return entry.action !== "review" && entry.action !== "navigate";
    },

    /** Build a human-readable description of what an undo will revert.
     *  Used in confirm modals so users aren't guessing. */
    describeEntryForUndo(entry) {
      if (!entry) return "this action";
      const action = entry.action ?? "";
      const sheet = entry.sheet ?? "";
      const details = entry.details ?? {};
      const col = details.column_label || details.column || "";
      const rows = entry.affected_count ?? 0;
      const act = action.replace(/^decision_/, "");
      if (act === "edit_cell") {
        return `Single-cell edit — ${sheet} row ${(details.row_idx ?? 0) + 9}, column "${col}"`;
      }
      if (act === "replace_with") {
        return `Replace in "${col}" across ${rows} rows (→ "${details.value ?? ''}")`;
      }
      if (act === "fill_with")       return `Fill "${col}" on ${rows} rows (→ "${details.value ?? ''}")`;
      if (act === "clear_all")       return `Clear "${col}" on ${rows} rows`;
      if (act === "set_urp")         return `Set "${col}" to URP on ${rows} rows`;
      if (act === "truncate_all")    return `Truncate "${col}" on ${rows} rows`;
      if (act === "accept_all")      return `Accept-as-is ${rows} errors in "${col}"`;
      if (act === "delete_duplicates") return `Delete ${rows} duplicate rows in ${sheet}`;
      if (act === "delete_rows" || act === "bulk_delete_rows") return `Delete ${rows} rows from ${sheet}`;
      if (act === "delete_row")      return `Delete row ${(details.row_idx ?? 0) + 9} from ${sheet}`;
      return `${act} on ${rows} row${rows === 1 ? '' : 's'}${col ? ` in "${col}"` : ''}`;
    },

    async undoAction(auditIndex, entry = null) {
      const description = entry ? this.describeEntryForUndo(entry) : "the selected action";
      const { ok } = await this.ui_confirm({
        title: "Undo this action?",
        message: description + ".\n\nThe change will be reverted. Only actions made in the current session can be undone.",
        confirmLabel: "Undo",
        tone: "default",
        icon: "undo-2",
      });
      if (!ok) return;
      try {
        const res = await fetch(`/api/session/undo/${auditIndex}`, { method: "POST" });
        if (!res.ok) throw new Error((await res.json()).detail);
        await this.reloadAll();
        this.toast("Undone: " + description, "info", "undo-2");
      } catch (e) {
        this.toast(e.message, "error", "x");
      }
    },

    // Undo from persistent activity: only possible for actions still in the session log
    // (same user, current session). Matches by rule_id + sheet + timestamp proximity.
    canUndoPersistent(entry) {
      if (!entry) return false;
      if (["mark_validated", "revoke_validation", "mark_ltmc_uploaded",
           "file_upload", "file_deleted", "user_deleted"].includes(entry.action)) {
        return false;
      }
      // Only actions from the current session (present in audit.log) are undoable
      const recent = this.audit.log ?? [];
      const match = recent.find(e =>
        e.sheet === entry.sheet &&
        e.action === entry.action?.replace(/^decision_/, "") &&
        Math.abs((e.audit_index ?? 0)) >= 0
      );
      return !!match;
    },

    async tryUndoFromPersistent(entry) {
      // Map persistent entry back to in-session audit_index if possible
      const recent = this.audit.log ?? [];
      const sessionAction = entry.action.replace(/^decision_/, "");
      const match = recent.find(e => e.sheet === entry.sheet && e.action === sessionAction);
      if (!match) {
        this.toast("This action is no longer undoable (saved in prior session)", "warn", "alert-triangle");
        return;
      }
      await this.undoAction(match.audit_index, entry);
    },

    // ─── Export ──────────────────────────────────────────
    async doExport() {
      // Cleaned-format export: SD = XML (original SD format), MM = xlsx
      // (5-sheet bundle: Summary/Main/AlternateUnits/LongText/ChangeLog)
      await this.runBusy("export", async () => {
        try {
          const res = await fetch("/api/session/export");
          if (!res.ok) throw new Error("Export failed");
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          // Pick a sensible filename based on module
          const base = (this.dashboard.filename ?? "data").replace(/\.(xml|xlsx)$/i, "");
          const ext = this.dashboard.module === "MM" ? "_cleaned.xlsx" : "_clean.xml";
          a.download = base + ext;
          document.body.appendChild(a);
          a.click();
          a.remove();
          URL.revokeObjectURL(url);
          this.toast(this.dashboard.module === "MM"
            ? "Downloaded cleaned xlsx bundle"
            : "Downloaded SD XML",
            "success", "check-circle-2");
        } catch (e) {
          this.toast(e.message, "error", "x");
        }
      });
    },

    /** MM-only: download the LTMC-upload-ready SpreadsheetML XML file.
     *  This is the actual file LTMC's import accepts directly.
     *  Distinct from doExport() which produces a cleaned xlsx bundle. */
    /** MM LTMC export — manifest + per-chunk downloads.
     *
     *  Same pattern PP uses: POST returns a chunk manifest, then we GET
     *  each chunk separately and trigger a download per chunk. For
     *  small (<95 MB) MM files this produces a single download named
     *  `<base>_LTMC.xml`. For larger ones (Healthium-scale), 2 chunks
     *  named `<base>_LTMC_part1of2.xml` etc. — each chunk is a complete
     *  standalone LTMC XML that imports independently into SAP. MATNRs
     *  never split across chunks. */
    async doExportLtmc() {
      if (this.dashboard.module !== "MM") {
        this.toast("LTMC export is only available for MM sessions", "warn", "alert-triangle");
        return;
      }
      await this.runBusy("export-ltmc", async () => {
        try {
          // Step 1: ask the server to build the chunks and return a manifest
          const manifestRes = await fetch("/api/session/export_ltmc",
                                          { method: "POST" });
          if (!manifestRes.ok) {
            const err = await manifestRes.json().catch(() => ({}));
            throw new Error(err.detail ?? "LTMC export failed");
          }
          const manifest = await manifestRes.json();
          if (!manifest.chunks || manifest.chunks.length === 0) {
            throw new Error("LTMC export produced no chunks");
          }

          // Step 2: download each chunk. The server already named them
          // (e.g. "FG_codes_master_LTMC_part1of2.xml") so we just pass
          // those filenames through to the browser.
          for (const chunk of manifest.chunks) {
            const r = await fetch(`/api/session/export_ltmc/chunk/${chunk.index}`);
            if (!r.ok) {
              throw new Error(`Chunk ${chunk.index} download failed`);
            }
            const blob = await r.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = chunk.filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
            // Browsers can collapse rapid-fire downloads into one; a small
            // delay between chunks keeps each one as a distinct download
            // entry in the browser's download manager.
            if (manifest.chunks.length > 1) {
              await new Promise(rs => setTimeout(rs, 200));
            }
          }

          if (manifest.single_file) {
            this.toast("Downloaded LTMC XML — ready for SAP upload",
                       "success", "check-circle-2");
          } else {
            this.toast(
              `Downloaded ${manifest.chunks.length} LTMC chunks — ` +
              `upload each separately to SAP. MATNRs stay grouped within each file.`,
              "success", "check-circle-2",
            );
          }
        } catch (e) {
          this.toast(e.message, "error", "x");
        }
      });
    },

    /** Colored review xlsx export — distinct from LTMC XML.
     *
     *  Server emits an xlsx with cells fill-colored by validation rule
     *  and a "DO NOT UPLOAD TO SAP" banner. SMEs use this to review
     *  errors visually in Excel before re-exporting the clean LTMC XML
     *  for actual SAP upload. The file is named "*_Review.xlsx" so it
     *  can't be confused with the LTMC import file. */
    async doExportReviewXlsx() {
      if (this.dashboard.module !== "MM") {
        this.toast("Review xlsx is only available for MM sessions today", "warn", "alert-triangle");
        return;
      }
      await this.runBusy("export-review", async () => {
        try {
          const res = await fetch("/api/session/export_review_xlsx");
          if (!res.ok) {
            const err = await res.json().catch(() => ({}));
            throw new Error(err.detail ?? "Review xlsx export failed");
          }
          const blob = await res.blob();
          const url = URL.createObjectURL(blob);
          const a = document.createElement("a");
          a.href = url;
          const base = (this.dashboard.filename ?? "materials").replace(/\.(xml|xlsx)$/i, "");
          a.download = base + "_Review.xlsx";
          document.body.appendChild(a);
          a.click();
          a.remove();
          URL.revokeObjectURL(url);
          this.toast("Review xlsx downloaded — for review only, do not upload to SAP",
                     "success", "palette");
        } catch (e) {
          this.toast(e.message, "error", "x");
        }
      });
    },

    /** PP BOM export — uses the manifest+chunk pattern.
     *
     *  Step 1: POST /api/pp/session/export_ltmc returns a manifest:
     *    { chunk_count, single_file, material_count, chunks: [{index, filename, size_bytes}, ...] }
     *  Step 2: For each chunk, GET /api/pp/session/export_ltmc/chunk/{index}
     *    streams the XML bytes; we save each as a download.
     *
     *  For a small BOM (<95 MB output) this is one chunk and behaves
     *  like a regular download. For a huge BOM we get multiple chunks
     *  named "..._part1of3.xml" etc. — the user gets one download per
     *  chunk, all preserving MATNR boundaries (the splitter never
     *  splits a material across chunks, which is critical for SAP
     *  LTMC import). */
    async doExportPpBom() {
      if (this.dashboard.module !== "PP") {
        this.toast("BOM export is only available for PP sessions", "warn", "alert-triangle");
        return;
      }
      await this._doExportPpKind({
        busyKey: "export-pp-bom",
        manifestUrl: "/api/pp/session/export_ltmc",
        chunkUrl: "/api/pp/session/export_ltmc/chunk/",
        kindLabel: "BOM",
      });
    },

    /** PP Routing export — same shape as BOM. Only enabled in the UI
     *  when dashboard.pp_stats.has_routing is true (i.e. the user
     *  uploaded a Routing alongside the BOM). */
    async doExportPpRouting() {
      if (this.dashboard.module !== "PP") {
        this.toast("Routing export is only available for PP sessions", "warn", "alert-triangle");
        return;
      }
      if (!this.dashboard.pp_stats?.has_routing) {
        this.toast("This session has no Routing data — re-upload with a Routing file", "warn", "alert-triangle");
        return;
      }
      await this._doExportPpKind({
        busyKey: "export-pp-routing",
        manifestUrl: "/api/pp/session/export_ltmc_routing",
        chunkUrl: "/api/pp/session/export_ltmc_routing/chunk/",
        kindLabel: "Routing",
      });
    },

    /** Shared manifest+chunk download driver for both BOM and Routing
     *  PP exports. Posts to the manifest URL, then GETs each chunk in
     *  sequence and triggers a browser download per chunk.
     *
     *  Why per-chunk downloads rather than concatenating: SAP LTMC
     *  expects each chunk as a separate file upload, so we mirror that
     *  shape on disk. Naming follows the manifest's filenames
     *  ("BOM_PHASE_1_LTMC.xml" for single chunk, "..._part1of3.xml"
     *  for multiple). */
    async _doExportPpKind({ busyKey, manifestUrl, chunkUrl, kindLabel }) {
      await this.runBusy(busyKey, async () => {
        try {
          // Step 1: get the manifest
          const manifestRes = await fetch(manifestUrl, { method: "POST" });
          if (!manifestRes.ok) {
            const err = await manifestRes.json().catch(() => ({}));
            throw new Error(err.detail ?? `${kindLabel} export failed`);
          }
          const manifest = await manifestRes.json();
          if (!manifest.chunks || manifest.chunks.length === 0) {
            throw new Error(`${kindLabel} export produced no chunks`);
          }

          // Step 2: download each chunk
          for (const chunk of manifest.chunks) {
            const r = await fetch(chunkUrl + chunk.index);
            if (!r.ok) {
              throw new Error(`Chunk ${chunk.index} download failed`);
            }
            const blob = await r.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = chunk.filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
            // Small delay between chunk downloads so browsers don't
            // collapse them into one. 200 ms is enough.
            if (manifest.chunks.length > 1) {
              await new Promise(r => setTimeout(r, 200));
            }
          }

          if (manifest.single_file) {
            this.toast(`Downloaded ${kindLabel} LTMC XML — ready for SAP upload`,
                       "success", "check-circle-2");
          } else {
            this.toast(
              `Downloaded ${manifest.chunks.length} ${kindLabel} LTMC chunks — upload each separately to SAP`,
              "success", "check-circle-2",
            );
          }
        } catch (e) {
          this.toast(e.message, "error", "x");
        }
      });
    },

    // ─── Changes Summary (Export page) ────────────────────
    async loadChangesSummary() {
      try {
        const res = await fetch("/api/session/changes_summary");
        if (!res.ok) {
          this.changesSummary = { changes: [], total: 0, by_type: {}, by_sheet: {}, _stamp: Date.now() };
          this._filteredChangesCache = null;
          return;
        }
        const data = await res.json();
        // Stamp the payload so filteredChanges() can detect "different
        // changes list" without deep-equaling the array.
        data._stamp = Date.now();
        this.changesSummary = data;
        this._filteredChangesCache = null;
      } catch (e) {
        this.changesSummary = { changes: [], total: 0, by_type: {}, by_sheet: {}, _stamp: Date.now() };
        this._filteredChangesCache = null;
      }
    },

    /** Filter the full changes list down by the active filter state.
     *
     *  Memoised: with 10k+ entries the filter call inside Alpine's reactive
     *  re-renders becomes a hot loop (each `<template x-for>` and each
     *  x-show on the table chair calls this again, and Alpine re-runs the
     *  whole expression on every dependency tick). The cache keys off
     *  the changesSummary identity + the three filter fields — invalidated
     *  by `loadChangesSummary()` and any filter-input change.
     */
    filteredChanges() {
      const all = this.changesSummary?.changes ?? [];
      const f = this.changesFilter;
      // Cheap cache key. We're not trying for cryptographic identity —
      // just enough to detect "same inputs as last call".
      const key = (this.changesSummary?._stamp ?? 0) + "|" +
                  (f.type || "") + "|" + (f.sheet || "") + "|" + (f.search || "");
      if (this._filteredChangesCache?.key === key) {
        return this._filteredChangesCache.out;
      }
      let out = all;
      if (f.type) out = out.filter(c => c.type === f.type);
      if (f.sheet) out = out.filter(c => c.sheet === f.sheet);
      if (f.search) {
        const q = f.search.toLowerCase();
        out = out.filter(c =>
          (c.column_label || "").toLowerCase().includes(q) ||
          String(c.xml_row || "").includes(q) ||
          (c.old_value || "").toLowerCase().includes(q) ||
          (c.new_value || "").toLowerCase().includes(q) ||
          (c.reason || "").toLowerCase().includes(q)
        );
      }
      this._filteredChangesCache = { key, out };
      return out;
    },

    /** Maximum number of changes we render in the DOM at once.
     *  Above this, the page becomes unresponsive even on fast machines —
     *  Alpine.js x-for with reactive bindings hits a wall around ~3k rows.
     *  500 is plenty for spot-checking; the full list ships via CSV. */
    CHANGES_VISIBLE_CAP: 500,

    /** What we actually render in the table. Slices the filtered list to
     *  the visible cap; the UI shows a notice + CSV download prompt when
     *  truncated. */
    visibleChanges() {
      const filtered = this.filteredChanges();
      if (filtered.length <= this.CHANGES_VISIBLE_CAP) return filtered;
      return filtered.slice(0, this.CHANGES_VISIBLE_CAP);
    },

    changeTypeLabel(t) {
      return ({
        edit: "Edit",
        replace_with: "Replace",
        fill_with: "Fill",
        clear_all: "Clear",
        set_urp: "Set URP",
        truncate_all: "Truncate",
        accept: "Accept",
        delete: "Delete",
      })[t] ?? t;
    },

    changeTypePillClass(t) {
      if (t === "delete") return "pill-danger";
      if (t === "clear_all" || t === "truncate_all") return "pill-warn";
      if (t === "accept") return "pill-neutral";
      return "pill-info";
    },

    async downloadChangesCsv() {
      try {
        const res = await fetch("/api/session/changes_summary.csv");
        if (!res.ok) throw new Error("CSV download failed");
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = (this.dashboard.filename ?? "changes").replace(".xml", "") + "_changes.csv";
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
        this.toast("Downloaded changes CSV", "success", "check-circle-2");
      } catch (e) {
        this.toast(e.message, "error", "x");
      }
    },

    // ─── Admin ───────────────────────────────────────────
    async loadAdminUsers() {
      try {
        const res = await fetch("/api/admin/users");
        if (!res.ok) return;
        const data = await res.json();
        this.admin.users = data.users ?? [];
      } catch (e) { }
    },

    async loadAdminAudit() {
      try {
        const res = await fetch("/api/admin/audit?limit=500");
        if (!res.ok) return;
        const data = await res.json();
        this.audit.entries = data.entries ?? [];
      } catch (e) { }
    },

    openCreateUserModal() {
      this.modal = {
        type: "create_user",
        newUser: { username: "", password: "", display_name: "", role: "module", module: "SD" },
      };
    },

    async submitCreateUser() {
      const u = this.modal.newUser;
      if (!u.username || !u.password || !u.display_name) {
        this.toast("Fill all fields", "warn", "alert-triangle");
        return;
      }
      try {
        const res = await fetch("/api/admin/users", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username: u.username,
            password: u.password,
            display_name: u.display_name,
            role: u.role,
            module: u.role === "module" ? u.module : null,
          }),
        });
        if (!res.ok) throw new Error((await res.json()).detail);
        this.closeModal();
        await this.loadAdminUsers();
        this.toast(`User ${u.username} created`, "success", "user-plus");
      } catch (e) {
        this.toast(e.message, "error", "x");
      }
    },

    openChangePwModal(username) {
      this.modal = { type: "change_pw", pwUsername: username, newPassword: "" };
    },

    /** Open the change-role modal pre-populated with the user's current
     *  role. Admin user itself is blocked at the UI level (button x-show
     *  hides it) AND at the backend (change_role refuses to demote admin). */
    openChangeRoleModal(username, currentRole, currentModule) {
      this.modal = {
        type: "change_role",
        roleUsername: username,
        newRole: currentRole || "module",
        newModule: currentModule || "",
      };
    },

    async submitChangeRole() {
      if (!this.modal.newRole) {
        this.toast("Pick a role", "warn", "alert-triangle");
        return;
      }
      if (this.modal.newRole === "module" && !this.modal.newModule) {
        this.toast("Module roles need a specific module (SD, MM, PP, QM, FICO)", "warn", "alert-triangle");
        return;
      }
      try {
        const res = await fetch("/api/admin/users/role", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            username: this.modal.roleUsername,
            new_role: this.modal.newRole,
            new_module: this.modal.newRole === "module" ? this.modal.newModule : null,
          }),
        });
        if (!res.ok) throw new Error((await res.json()).detail);
        this.closeModal();
        await this.loadAdminUsers();
        this.toast("Role updated", "success", "shield-check");
      } catch (e) {
        this.toast(e.message, "error", "x");
      }
    },

    async submitChangePw() {
      if (!this.modal.newPassword) {
        this.toast("Enter a password", "warn", "alert-triangle");
        return;
      }
      try {
        const res = await fetch("/api/admin/users/password", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username: this.modal.pwUsername, new_password: this.modal.newPassword }),
        });
        if (!res.ok) throw new Error((await res.json()).detail);
        this.closeModal();
        this.toast("Password changed", "success", "key");
      } catch (e) {
        this.toast(e.message, "error", "x");
      }
    },

    async deleteUser(username) {
      const { ok } = await this.ui_confirm({
        title: `Delete user "${username}"?`,
        message: "The user will lose all access immediately. Their past audit trail entries remain.",
        confirmLabel: "Delete user",
        tone: "danger",
        icon: "user-minus",
      });
      if (!ok) return;
      try {
        const res = await fetch(`/api/admin/users/${username}`, { method: "DELETE" });
        if (!res.ok) throw new Error((await res.json()).detail);
        await this.loadAdminUsers();
        this.toast(`User ${username} deleted`, "info", "user-minus");
      } catch (e) {
        this.toast(e.message, "error", "x");
      }
    },

    // ─── Nav ─────────────────────────────────────────────
    setView(v) {
      // If the user navigates to anything OTHER than records or decisions
      // while a Fix-Individually flow is live, the flow is implicitly
      // abandoned — clear its context so the banner doesn't follow them
      // into unrelated pages. Records and decisions are the two valid
      // screens inside the flow (records = fixing, decisions = we just
      // finished and came back), so those don't clear it.
      if (this.record.flow && v !== "records" && v !== "decisions") {
        this.record.flow = null;
      }
      this.view = v;
      if (v === "repository") this.loadRepo();
      if (v === "admin_users") this.loadAdminUsers();
      if (v === "admin_audit") this.loadAdminAudit();
      if (v === "grid") this.loadErrors();
    },

    openRecord_fromGrid(sheet, rowIdx) { return this.openRecord(sheet, rowIdx); },

    // ─── Modal ───────────────────────────────────────────
    closeModal() {
      this.modal = { type: null };
      this._actionPending = null;
    },

    /**
     * Show an in-app confirm / reason prompt modal.
     * Returns a Promise that resolves to { ok: boolean, reason: string }.
     *
     * The resolve function is stored on the app state (not the modal object)
     * because Alpine can be finicky about function properties on reactive state.
     * The template calls onConfirm() / onCancel() directly on the app instance.
     */
    _confirmResolve: null,   // resolver for the active ui_confirm promise
    _confirmResolved: false,  // guards against double-resolve

    ui_confirm({ title, message = "", confirmLabel = "Confirm", cancelLabel = "Cancel",
                 tone = "default", icon = null, reasonRequired = false }) {
      return new Promise((resolve) => {
        this._confirmResolve = resolve;
        this._confirmResolved = false;
        this.modal = {
          type: "confirm",
          title,
          message,
          confirmLabel,
          cancelLabel,
          tone,
          icon: icon ?? (tone === "danger" ? "trash-2" : tone === "warn" ? "alert-triangle" : "help-circle"),
          reasonRequired,
          reason: "",
        };
      });
    },

    /** Called by the Confirm button in the template. */
    onConfirm() {
      console.log("[MDV] onConfirm called", { resolved: this._confirmResolved, hasResolver: !!this._confirmResolve });
      if (this._confirmResolved) return;
      if (this.modal.reasonRequired && !(this.modal.reason || "").trim()) {
        this.toast("Reason is required", "warn", "alert-triangle");
        return;
      }
      const reason = this.modal.reason || "";
      this._confirmResolved = true;
      const resolver = this._confirmResolve;
      this._confirmResolve = null;
      this.modal = { type: null };
      if (resolver) resolver({ ok: true, reason });
    },

    /** Called by the Cancel button / backdrop / Escape. */
    onCancel() {
      console.log("[MDV] onCancel called", { resolved: this._confirmResolved, hasResolver: !!this._confirmResolve });
      if (this._confirmResolved) return;
      this._confirmResolved = true;
      const resolver = this._confirmResolve;
      this._confirmResolve = null;
      this.modal = { type: null };
      if (resolver) resolver({ ok: false, reason: "" });
    },

    // ─── Toasts ──────────────────────────────────────────
    toast(msg, type = "info", icon = null) {
      const id = Date.now() + Math.random();
      const iconMap = { success: "check-circle-2", error: "x-circle", warn: "alert-triangle", info: "info" };
      this.toasts.push({ id, msg, type, icon: icon ?? iconMap[type] });
      setTimeout(() => {
        this.toasts = this.toasts.filter(t => t.id !== id);
      }, 3800);
    },

    // ─── Formatters ──────────────────────────────────────
    formatStatus(s) {
      const map = { in_progress: "In Progress", validated: "Validated", ltmc_uploaded: "LTMC Uploaded" };
      return map[s] ?? s;
    },

    formatTime(ts) {
      if (!ts) return "—";
      const d = new Date(typeof ts === "number" ? ts * 1000 : ts);
      const now = new Date();
      const diff = (now - d) / 1000;
      if (diff < 60) return "just now";
      if (diff < 3600) return `${Math.floor(diff/60)}m ago`;
      if (diff < 86400) return `${Math.floor(diff/3600)}h ago`;
      return d.toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" });
    },

    formatTimeExact(ts) {
      if (!ts) return "—";
      const d = new Date(typeof ts === "number" ? ts * 1000 : ts);
      return d.toLocaleString("en-GB", {
        day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit"
      });
    },

    formatActionLabel(action) {
      const map = {
        file_upload: "Uploaded",
        mark_validated: "Validated",
        revoke_validation: "Revoked",
        mark_ltmc_uploaded: "LTMC Uploaded",
        file_deleted: "Deleted File",
        delete_row: "Deleted Row",
        bulk_delete_rows: "Bulk Delete Rows",
        edit_cell: "Edited Cell",
        user_deleted: "Deleted User",
        user_created: "Created User",
        password_changed: "Changed Password",
        decision_replace_with: "Replaced Values",
        decision_fill_with: "Filled Values",
        decision_clear_all: "Cleared Values",
        decision_set_urp: "Set URP",
        decision_truncate_all: "Truncated",
        decision_delete_duplicates: "Deleted Dupes",
        decision_delete_rows: "Deleted Rows",
        decision_accept_all: "Accepted",
      };
      return map[action] ?? action.replace(/_/g, " ");
    },

    auditActionPillClass(action) {
      if (["file_deleted", "delete_row", "bulk_delete_rows", "user_deleted",
           "decision_delete_duplicates", "decision_delete_rows"].includes(action))
        return "pill-danger";
      if (["mark_validated", "mark_ltmc_uploaded"].includes(action))
        return "pill-success";
      if (["revoke_validation"].includes(action))
        return "pill-warn";
      if (action.startsWith("decision_")) return "pill-info";
      return "pill-neutral";
    },

    describeAction(entry) {
      const map = {
        accept_all: " accepted all as-is on",
        replace_with: " bulk-replaced values on",
        fill_with: " filled blanks on",
        clear_all: " cleared values on",
        set_urp: " set to URP on",
        truncate_all: " truncated values on",
        delete_duplicates: " deleted duplicates in",
        delete_rows: " deleted rows in",
        delete_row: " deleted a row in",
        edit_cell: " edited a cell in",
      };
      return map[entry.action] ?? ` acted on`;
    },
  };
}
