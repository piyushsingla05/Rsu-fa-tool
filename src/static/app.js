/* Workbench - application shell.
 *
 * The shell owns sign-in, the sidebar and routing between tools. Each tool
 * supplies its own UI module in TOOL_UI, keyed by the id the server's manifest
 * gives it. Tools do not reference each other, and the shell knows nothing
 * about any tool's subject matter - a reserved tool simply has no entry here.
 *
 * THE ONE RULE inside the tax module: it calculates nothing. Every figure shown
 * is a value the engine already produced in build_context() - the same dict
 * write_workbook() renders. The only thing done to a number is thousands
 * separators for display.
 */
const $ = (s) => document.querySelector(s);
const el = (t, cls, txt) => {
  const n = document.createElement(t);
  if (cls) n.className = cls;
  if (txt !== undefined && txt !== null) n.textContent = txt;
  return n;
};

let TOOLS = [];
let CUR = { tool: null, section: null };

/* ---------------------------------------------------------------- transport */
const TIMEOUT_MARK = "__request_timeout__";

/* `timeoutMs` is an optional extra key on opts, not a fetch() option - pulled
 * out here so a slow request (the run endpoint, in particular) fails with a
 * distinguishable, catchable error instead of leaving the caller waiting on
 * an indefinite spinner with no way to tell "still working" from "hung". */
async function call(url, opts = {}) {
  const { timeoutMs, ...rest } = opts;
  let signal = rest.signal;
  let timer = null;
  if (timeoutMs) {
    const ctrl = new AbortController();
    signal = ctrl.signal;
    timer = setTimeout(() => ctrl.abort(), timeoutMs);
  }
  try {
    const r = await fetch(url, { credentials: "same-origin", ...rest, signal });
    if (r.status === 401) { showLogin(); throw new Error("Sign in required"); }
    if (!r.ok) {
      let d = "";
      try { d = (await r.json()).detail || ""; } catch (e) { d = await r.text(); }
      throw new Error(d || `${r.status}`);
    }
    const ct = r.headers.get("content-type") || "";
    return ct.includes("json") ? r.json() : r.text();
  } catch (err) {
    if (err.name === "AbortError") throw new Error(TIMEOUT_MARK);
    throw err;
  } finally {
    if (timer) clearTimeout(timer);
  }
}
const form = (obj) => {
  const f = new FormData();
  for (const [k, v] of Object.entries(obj)) f.append(k, v);
  return f;
};

/* ---------------------------------------------------------------- shared UI */
const isNum = (v) => typeof v === "number" && isFinite(v);
function fmt(v) {
  if (v === null || v === undefined || v === "") return "";
  if (isNum(v)) {
    const dp = Number.isInteger(v) ? 0 : (Math.abs(v) < 10 ? 4 : 2);
    return v.toLocaleString(undefined,
      { minimumFractionDigits: dp, maximumFractionDigits: dp });
  }
  const s = String(v);
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  return m ? `${m[3]}-${m[2]}-${m[1]}` : s;      // presentational only
}

function table(frame, opts = {}) {
  const wrap = el("div", "scroll");
  if (!frame || !frame.columns || !frame.columns.length) {
    wrap.appendChild(el("div", "empty", opts.empty || "Nothing to show."));
    return wrap;
  }
  // Columns named in opts.optional are full provenance - present in every
  // rendering, hidden until asked for, so a wide evidence table stays readable
  // without anything being dropped from it.
  const optional = new Set(opts.optional || []);
  const opt = frame.columns.map((c) => optional.has(c));
  const hidden = opt.filter(Boolean).length;
  if (hidden) wrap.classList.add("compact");
  const t = el("table");
  const head = el("tr");
  frame.columns.forEach((c, i) => head.appendChild(
    el("th", [frame.backend_from !== null && i >= frame.backend_from ? "grey" : "",
      opt[i] ? "opt" : ""].filter(Boolean).join(" ") || null, c)));
  t.appendChild(el("thead")).appendChild(head);
  if (!frame.rows.length) {
    wrap.appendChild(t);
    wrap.appendChild(el("div", "empty", opts.empty || "No rows."));
    return wrap;
  }
  const body = el("tbody");
  frame.rows.forEach((row) => {
    const tr = el("tr");
    row.forEach((v, i) => {
      const grey = frame.backend_from !== null && i >= frame.backend_from;
      const long = typeof v === "string" && v.length > 60;
      tr.appendChild(el("td", [grey ? "grey" : "", isNum(v) ? "num" : "",
        long ? "wrap" : "", opt[i] ? "opt" : ""].filter(Boolean).join(" ") || null,
        fmt(v)));
    });
    body.appendChild(tr);
  });
  t.appendChild(body);
  wrap.appendChild(t);
  if (hidden) {
    const b = el("button", "linkish dark", `Show full provenance (${hidden} more columns)`);
    b.onclick = () => {
      const on = wrap.classList.toggle("compact");
      b.textContent = on ? `Show full provenance (${hidden} more columns)`
        : "Hide full provenance";
    };
    const holder = el("div", "tablefoot");
    holder.appendChild(b);
    const outer = el("div");
    outer.appendChild(wrap);
    outer.appendChild(holder);
    return outer;
  }
  return wrap;
}

function card(title, sub) {
  const c = el("div", "card");
  if (title) c.appendChild(el("h2", null, title));
  if (sub) c.appendChild(el("p", "sub", sub));
  return c;
}
const banner = (kind, text) => el("div", "banner " + kind, text);

/* ---------------------------------------------------------------- shell */
function showLogin() {
  $("#login").hidden = false;
  $("#shell").hidden = true;
}
function showShell() {
  $("#login").hidden = true;
  $("#shell").hidden = false;
}

$("#loginform").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#loginerr").innerHTML = "";
  try {
    await call("/api/login", { method: "POST", body: form({ password: $("#pw").value }) });
    $("#pw").value = "";
    await boot();
  } catch (err) {
    $("#loginerr").appendChild(banner("err", err.message));
  }
});
$("#signout").addEventListener("click", async () => {
  await call("/api/logout", { method: "POST" });
  showLogin();
});

function renderNav() {
  const nav = $("#nav");
  nav.innerHTML = "";
  TOOLS.forEach((tool) => {
    const box = el("div", "tool"
      + (tool.id === CUR.tool ? " on" : "")
      + (tool.status === "reserved" ? " reserved" : ""));
    const head = el("button", "toolhead");
    head.appendChild(el("span", null, tool.label));
    if (tool.status === "reserved") head.appendChild(el("span", "soon", "Coming soon"));
    head.onclick = () => {
      if (tool.status === "reserved") { go(tool.id, null); return; }
      go(tool.id, tool.sections.length ? tool.sections[0].id : null);
    };
    box.appendChild(head);

    if (tool.id === CUR.tool && tool.sections.length) {
      let group = null;
      tool.sections.forEach((s) => {
        if (s.group && s.group !== group) {
          group = s.group;
          box.appendChild(el("div", "group", group));
        }
        const a = el("a", "sec" + (s.id === CUR.section ? " on" : ""), s.label);
        a.href = "#";
        const ui = TOOL_UI[tool.id];
        if (ui && ui.sectionDisabled && ui.sectionDisabled(s.id)) {
          a.classList.add("disabled");
        }
        if (ui && ui.sectionBadge) {
          const b = ui.sectionBadge(s.id);
          if (b) a.appendChild(el("span", "dot" + (b.bad ? " bad" : ""), b.text));
        }
        a.onclick = (e) => { e.preventDefault(); go(tool.id, s.id); };
        box.appendChild(a);
      });
    }
    nav.appendChild(box);
  });
}

function go(toolId, sectionId) {
  CUR = { tool: toolId, section: sectionId };
  renderNav();
  const tool = TOOLS.find((t) => t.id === toolId);
  const sec = (tool.sections || []).find((s) => s.id === sectionId);
  $("#crumb").textContent = tool.label;
  $("#ptitle").textContent = sec ? sec.label : tool.label;
  const panel = $("#panel");
  panel.innerHTML = "";

  if (tool.status === "reserved") { renderReserved(panel, tool); return; }
  const ui = TOOL_UI[toolId];
  if (!ui) {
    panel.appendChild(banner("warn", "This tool has no interface installed."));
    return;
  }
  ui.render(panel, sectionId);
}

function renderReserved(panel, tool) {
  const c = card(null, null);
  const box = el("div", "reserved-panel");
  box.appendChild(el("div", "big", tool.label));
  box.appendChild(el("p", null, tool.note || "Coming soon."));
  box.appendChild(el("p", null,
    "It is developed separately and plugs into this application when ready. "
    + "Nothing in it touches the tax engine, and nothing in the tax engine "
    + "depends on it."));
  c.appendChild(box);
  panel.appendChild(c);
  $("#barmeta").textContent = "Reserved";
}

/* ================================================================
 * TOOL: Tax / Foreign Assets Working Paper
 * Self-contained. Another tool's module sits beside this one and shares
 * nothing but the helpers above.
 * ================================================================ */
const TaxTool = {
  API: "/api/tools/tax",
  job: null,
  counts: { Blocker: 0, Review: 0, Note: 0 },
  // A run in flight for this job id, so navigating away from Processing and
  // back cannot fire a second POST /run while the first has not returned.
  runInFlight: null,
  // Generous relative to the "seconds to a minute" a run normally takes -
  // long enough that a real run essentially never trips it, short enough
  // that a hung request does not leave the operator staring at a spinner
  // indefinitely with no explanation.
  RUN_TIMEOUT_MS: 120000,

  needsJob(section) {
    return !["documents"].includes(section);
  },
  sectionDisabled(section) {
    if (section === "documents") return false;
    if (!TaxTool.job) return true;
    if (section === "processing") return false;
    return TaxTool.job.state !== "done";
  },
  sectionBadge(section) {
    if (section !== "review" || !TaxTool.job || TaxTool.job.state !== "done") return null;
    const n = TaxTool.counts.Blocker || 0;
    return n ? { text: String(n), bad: true }
             : { text: String(TaxTool.counts.Review || 0), bad: false };
  },

  async render(panel, section) {
    $("#barmeta").innerHTML = "";
    if (TaxTool.job) {
      $("#barmeta").textContent =
        `${TaxTool.job.client} · ${fmt(TaxTool.job.period_start)} to `
        + `${fmt(TaxTool.job.period_end)} · engine ${TaxTool.job.engine_version}`;
    }
    if (section === "documents") return TaxTool.renderDocuments(panel);
    if (section === "processing") return TaxTool.renderProcessing(panel);
    if (section === "export") return TaxTool.renderExport(panel);
    if (!TaxTool.job || TaxTool.job.state !== "done") {
      panel.appendChild(banner("warn",
        "Add documents and run the working paper first."));
      return;
    }
    const load = card(null, null);
    load.innerHTML = '<span class="spin"></span>Loading…';
    panel.appendChild(load);
    let d;
    try { d = await call(`${TaxTool.API}/jobs/${TaxTool.job.id}/section/${section}`); }
    catch (err) { panel.innerHTML = ""; panel.appendChild(banner("err", err.message)); return; }
    panel.innerHTML = "";
    (TaxTool.views[section] || TaxTool.views._raw)(panel, d);
  },

  /* ---- prepare ---- */
  async renderDocuments(panel) {
    const setup = card("Working paper",
      "The reporting period is an input on every run — calendar year, financial "
      + "year, or an older assessment year.");
    if (!TaxTool.job) {
      const f = el("form");
      f.innerHTML = `
        <div class="row">
          <div class="field"><label>Client name</label><input id="t_client" required></div>
          <div class="field"><label>Period from</label><input id="t_start" type="date" required></div>
          <div class="field"><label>Period to</label><input id="t_end" type="date" required></div>
          <button class="btn" type="submit">Create</button>
        </div>
        <div class="checks" style="margin-top:12px">
          <label><input type="checkbox" id="t_limited"> Limited statement set</label>
          <label><input type="checkbox" id="t_entity"> One A3 row per entity, not per lot</label>
        </div>`;
      f.onsubmit = async (e) => {
        e.preventDefault();
        TaxTool.job = await call(`${TaxTool.API}/jobs`, {
          method: "POST", body: form({
            client: $("#t_client").value, period_start: $("#t_start").value,
            period_end: $("#t_end").value,
            limited_statement: $("#t_limited").checked,
            entity_wise: $("#t_entity").checked }) });
        go("tax", "documents");
      };
      setup.appendChild(f);
      panel.appendChild(setup);
      const y = new Date().getFullYear() - 1;
      $("#t_start").value = `${y}-01-01`;
      $("#t_end").value = `${y}-12-31`;
      await TaxTool.renderJobList(panel);
      return;
    }

    const c = card("Source documents",
      "Broker statements, portfolio Excel, Form 1042-S, an SBI TTBR table, or a "
      + "completed Google Finance FX table — any mix. Each file is identified as "
      + "it arrives by the same code the run uses.");
    const drop = el("div", "drop");
    drop.innerHTML = '<div>Drop files here, or <button class="btn ghost" '
      + 'type="button" id="t_pick">choose files</button></div>';
    const input = el("input");
    input.type = "file"; input.multiple = true; input.hidden = true;
    drop.appendChild(input);
    c.appendChild(drop);
    const list = el("div");
    c.appendChild(list);
    const msg = el("div");
    c.appendChild(msg);

    const paint = (analysis) => {
      list.innerHTML = "";
      const seen = analysis
        ? Object.fromEntries(analysis.map((a) => [a.file, a])) : {};
      TaxTool.job.documents.forEach((name) => {
        const a = seen[name];
        const d = el("div", "doc");
        d.appendChild(el("span", "nm", name));
        if (a && a.ok) {
          [a.broker, a.profile, `${Math.round(a.confidence * 100)}% confidence`,
           a.coverage, `${a.rows} row(s)`].forEach((x) =>
            d.appendChild(el("span", "tag", String(x))));
          if (a.is_google_fx) d.appendChild(el("span", "tag", "Google FX table"));
          if (a.ttbr_rows) d.appendChild(el("span", "tag", `${a.ttbr_rows} TTBR rows`));
        } else if (a) {
          d.appendChild(el("span", "tag err", a.error));
        }
        const rm = el("button", "btn ghost", "Remove");
        rm.onclick = async () => {
          const r = await call(
            `${TaxTool.API}/jobs/${TaxTool.job.id}/documents/${encodeURIComponent(name)}`,
            { method: "DELETE" });
          TaxTool.job = r.job; paint();
        };
        d.appendChild(rm);
        list.appendChild(d);
      });
      if (!TaxTool.job.documents.length) {
        list.appendChild(el("p", "note", "No documents yet."));
      }
    };

    const send = async (fileList) => {
      if (!fileList.length) return;
      const fd = new FormData();
      for (const f of fileList) fd.append("files", f);
      msg.innerHTML = "";
      msg.appendChild(banner("ok", "Reading and identifying…"));
      try {
        const res = await call(`${TaxTool.API}/jobs/${TaxTool.job.id}/documents`,
          { method: "POST", body: fd });
        TaxTool.job = res.job;
        msg.innerHTML = "";
        paint(res.files);
        renderNav();
      } catch (err) {
        msg.innerHTML = "";
        msg.appendChild(banner("err", err.message));
      }
    };
    drop.querySelector("#t_pick").onclick = () => input.click();
    input.onchange = (e) => send(e.target.files);
    ["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => {
      e.preventDefault(); drop.classList.add("over"); }));
    ["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
      e.preventDefault(); drop.classList.remove("over"); }));
    drop.addEventListener("drop", (e) => send(e.dataTransfer.files));
    paint();

    const actions = el("div", "row");
    actions.style.marginTop = "16px";
    const next = el("button", "btn", "Continue to processing");
    next.onclick = () => go("tax", "processing");
    actions.appendChild(next);
    const close = el("button", "btn ghost", "Discard and purge");
    close.onclick = async () => {
      if (!confirm("Discard this working paper and purge its source documents?")) return;
      await call(`${TaxTool.API}/jobs/${TaxTool.job.id}`, { method: "DELETE" });
      TaxTool.job = null;
      go("tax", "documents");
    };
    actions.appendChild(close);
    c.appendChild(actions);
    c.appendChild(el("p", "note",
      "Uploads stay in this job's working directory on this machine and are "
      + "purged when the job is discarded or expires. Nothing is transmitted "
      + "anywhere."));
    panel.appendChild(c);
  },

  async renderJobList(panel) {
    const { jobs } = await call(`${TaxTool.API}/jobs`);
    const c = card("Working papers in progress",
      "Jobs and their source documents are purged automatically after 8 hours.");
    if (!jobs.length) { c.appendChild(el("p", "note", "None yet.")); }
    jobs.forEach((j) => {
      const d = el("div", "doc");
      d.appendChild(el("span", "nm", j.client));
      [`${fmt(j.period_start)} to ${fmt(j.period_end)}`, j.state,
       `${j.documents.length} document(s)`].forEach((x) =>
        d.appendChild(el("span", "tag", String(x))));
      const open = el("button", "btn ghost", j.state === "done" ? "Open" : "Continue");
      open.onclick = async () => {
        TaxTool.job = await call(`${TaxTool.API}/jobs/${j.id}`);
        if (TaxTool.job.state === "done") await TaxTool.loadCounts();
        go("tax", TaxTool.job.state === "done" ? "summary" : "documents");
      };
      d.appendChild(open);
      c.appendChild(d);
    });
    panel.appendChild(c);
  },

  async renderProcessing(panel) {
    const c = card("Processing",
      "The engine reads every document, resolves FX through the ranked sources, "
      + "computes the working paper and writes the workbook.");
    const j = TaxTool.job;
    const dl = el("dl", "facts");
    [["Client", j.client],
     ["Period", `${fmt(j.period_start)} to ${fmt(j.period_end)}`],
     ["Documents", String(j.documents.length)],
     ["State", j.state],
     ["Runs so far", String(j.run_count)],
     ["Google FX rates supplied", j.has_google_fx ? "Yes" : "No"],
    ].forEach(([k, v]) => {
      dl.appendChild(el("dt", null, k));
      dl.appendChild(el("dd", null, v));
    });
    c.appendChild(dl);

    const st = el("div");
    st.style.marginTop = "14px";
    c.appendChild(st);
    const btn = el("button", "btn", j.run_count ? "Re-run" : "Run");
    const busy = TaxTool.runInFlight === j.id;
    btn.disabled = !j.documents.length || busy;
    btn.style.marginTop = "14px";
    btn.onclick = () => TaxTool.startRun(j.id, btn, st);
    c.appendChild(btn);
    if (busy) {
      st.appendChild(TaxTool.processingBanner());
    } else if (j.state === "done") {
      st.appendChild(banner("ok", j.message));
    }
    panel.appendChild(c);
  },

  processingBanner() {
    const b = banner("ok", "");
    b.innerHTML = '<span class="spin"></span>Processing documents — reading '
      + 'documents, resolving FX, building the workbook. This can take a '
      + 'little while for a large document set; the page will update as '
      + 'soon as it finishes.';
    return b;
  },

  async startRun(jobId, btn, st) {
    // Re-entrancy guard: a duplicate click, or a second render of this same
    // panel while the first request is still outstanding, must not start a
    // second run for the same job.
    if (TaxTool.runInFlight === jobId) return;
    TaxTool.runInFlight = jobId;
    btn.disabled = true;
    st.innerHTML = "";
    st.appendChild(TaxTool.processingBanner());
    try {
      const res = await call(`${TaxTool.API}/jobs/${jobId}/run`,
        { method: "POST", timeoutMs: TaxTool.RUN_TIMEOUT_MS });
      TaxTool.runInFlight = null;
      TaxTool.job = res.job;
      await TaxTool.loadCounts();
      go("tax", "summary");
    } catch (err) {
      TaxTool.runInFlight = null;
      st.innerHTML = "";
      if (err.message === TIMEOUT_MARK) {
        st.appendChild(TaxTool.timeoutBanner(jobId, btn, st));
      } else {
        st.appendChild(banner("err", "The run did not complete: " + err.message));
      }
      btn.disabled = !(TaxTool.job && TaxTool.job.documents.length);
    }
  },

  // On a client-side timeout the request may well still be running on the
  // server - v1's run is a single synchronous call, so there is nothing to
  // cancel server-side and no way yet to know the outcome. Resubmitting
  // blindly could start a second, overlapping run, so the operator is given
  // a way to find out what actually happened (the existing status endpoint,
  // untouched by this change) rather than being told to just try again.
  timeoutBanner(jobId, btn, st) {
    const wrap = el("div");
    wrap.appendChild(banner("warn",
      "Processing is taking longer than expected. It may still be running "
      + "on the server — check its status before running again, rather than "
      + "resubmitting."));
    const row = el("div", "row");
    row.style.marginTop = "8px";
    const check = el("button", "btn ghost", "Check status");
    check.onclick = async () => {
      check.disabled = true;
      try {
        const s = await call(`${TaxTool.API}/jobs/${jobId}/status`);
        if (s.state === "running") {
          st.innerHTML = "";
          st.appendChild(TaxTool.processingBanner());
          TaxTool.runInFlight = jobId;
          btn.disabled = true;
        } else if (s.state === "done") {
          TaxTool.job = await call(`${TaxTool.API}/jobs/${jobId}`);
          await TaxTool.loadCounts();
          go("tax", "summary");
        } else {
          st.innerHTML = "";
          st.appendChild(banner(s.state === "failed" ? "err" : "warn",
            s.message || `Job status: ${s.state}`));
          btn.disabled = !(TaxTool.job && TaxTool.job.documents.length);
        }
      } catch (err) {
        st.innerHTML = "";
        st.appendChild(banner("err", "Could not check status: " + err.message));
      } finally {
        check.disabled = false;
      }
    };
    row.appendChild(check);
    wrap.appendChild(row);
    return wrap;
  },

  renderExport(panel) {
    const c = card("Export",
      "The workbook is the authoritative deliverable. Every figure on these "
      + "screens comes from the same computation that wrote it.");
    if (!TaxTool.job || TaxTool.job.state !== "done") {
      c.appendChild(banner("warn", "Run the working paper first."));
      panel.appendChild(c);
      return;
    }
    const b = el("button", "btn", "Download workbook (.xlsx)");
    b.onclick = () => { window.location = `${TaxTool.API}/jobs/${TaxTool.job.id}/workbook`; };
    c.appendChild(b);
    c.appendChild(el("p", "note",
      "Eleven tabs: SUMMARY, FA_A2, FA_A3, RSU_MASTER, VESTING_SALES, "
      + "DIVIDENDS_1042, CAPITAL_GAINS, FX_WORKING, FSI_TR_WORKING, "
      + "RECONCILIATION, REVIEW_REQUIRED."));
    panel.appendChild(c);
  },

  async loadCounts() {
    try {
      const r = await call(`${TaxTool.API}/jobs/${TaxTool.job.id}/section/review`);
      TaxTool.counts = r.counts || TaxTool.counts;
    } catch (e) { /* a job that has not run has no register yet */ }
  },

  /* ---- working paper views ---- */
  views: {
    summary(panel, d) {
      const c = card("Summary", "This is a professional tax working paper, not a "
        + "statutory filing. Final treatment requires professional review.");
      c.appendChild(el("div", "verdict " + (d.counts.Blocker ? "bad" : "ok"), d.verdict));
      const tiles = el("div", "tiles");
      d.headlines.forEach((h) => {
        const t = el("div", "tile");
        t.appendChild(el("div", "lbl", h.label));
        t.appendChild(el("div", "val", fmt(h.value)));
        tiles.appendChild(t);
      });
      c.appendChild(tiles);
      c.appendChild(el("h3", null, "Basis of preparation"));
      const dl = el("dl", "facts");
      [["Client", d.client],
       ["Reporting period", `${fmt(d.period_start)} to ${fmt(d.period_end)}`],
       ["Engine version", d.engine_version],
       ["Brokers identified", d.brokers],
       ["Securities identified", d.securities],
       ["Dividend source for FSI", d.dividend_basis],
       ["Statement basis", d.statement_basis],
       ["FX status", d.fx_status],
       ["Market data status", d.market_status],
       ["Source coverage", d.coverage_status],
       ["Exceptions", `${d.counts.Blocker} blocker(s), ${d.counts.Review} review, `
         + `${d.counts.Note} note`]].forEach(([k, v]) => {
        dl.appendChild(el("dt", null, k));
        dl.appendChild(el("dd", null, String(v)));
      });
      c.appendChild(dl);
      panel.appendChild(c);
      const conv = card("Conventions applied",
        "The rules this working paper is built on. They are the engine's, not "
        + "the browser's — the workbook states the same.");
      conv.appendChild(table({ columns: ["Item", "Basis"], backend_from: null,
        rows: d.conventions.map((x) => [x.item, x.basis]) }));
      panel.appendChild(conv);
    },
    fa_a2(panel, d) {
      const c = card("Schedule FA — Table A2: Foreign Custodial Accounts",
        "Peak and closing balances converted at the rate for the relevant date.");
      c.appendChild(table(d.a2, { empty: "No custodial account data." }));
      panel.appendChild(c);
    },
    fa_a3(panel, d) {
      const c = card("Schedule FA — Table A3: Foreign Equity and Debt Interest",
        "One row per lot still held at the period end. Lots fully sold appear "
        + "only through gross proceeds. Grey columns are the backend figures the "
        + "workbook's formulas read.");
      c.appendChild(table(d.a3, { empty: "No holdings at the period end." }));
      panel.appendChild(c);
    },
    capital_gains(panel, d) {
      const c = card("Capital gains",
        "Sale at the sale-date rate; cost at the acquisition-date rate. The "
        + "Indian 24-month test decides short or long term — never the broker's "
        + "own label. A row with no rate on file shows FX_UNAVAILABLE rather "
        + "than a figure converted at zero.");
      c.appendChild(table(d.sales, { empty: "No disposals in the period." }));
      panel.appendChild(c);
    },
    dividends(panel, d) {
      const c = card("Dividends", "Gross, before foreign withholding. Source "
        + `used for Schedule FSI: ${d.dividend_basis}.`);
      c.appendChild(table(d.dividends, { empty: "No dividend transactions." }));
      panel.appendChild(c);
      if (d.form1042s && d.form1042s.rows && d.form1042s.rows.length) {
        const f = card("Form 1042-S", "As filed by the withholding agent.");
        f.appendChild(table(d.form1042s));
        panel.appendChild(f);
      }
      const fsi = card("Schedule FSI / TR",
        "Foreign tax withheld and foreign tax credit claimed are separate "
        + "figures. Form 67 must be filed before the return.");
      fsi.appendChild(table(d.fsi, { empty: "No foreign income to report." }));
      panel.appendChild(fsi);
    },
    fx_working(panel, d) {
      const c = card("FX working", "Every rate used, with its source, the "
        + "provider's own methodology, and how far down the ranked chain it "
        + "came from. No source is ever presented as an SBI TT buying rate.");
      const warn = /SBI TTBR unavailable|INCOMPLETE/.test(d.fx_status || "");
      c.appendChild(banner(warn ? "warn" : "ok", d.fx_status || "SBI TTBR throughout"));
      c.appendChild(table(d.fx_audit, {
        empty: "No conversions.",
        optional: ["Convention", "Rate date is", "Converted to", "Provider",
                   "Cross-rate components", "Derivation", "Provider methodology",
                   "Basis stated in the working paper", "Source"],
      }));
      panel.appendChild(c);
      const h = card("Source hierarchy",
        "The ranked chain, and what each source actually supplied in this run. "
        + "The first source that answers wins outright - a lower-ranked source "
        + "is never preferred merely because its date is closer. Within a "
        + "source: the exact date, else the nearest available date within 30 "
        + "days either way, a tie going to the prior date.");
      h.appendChild(table(d.fx_sources, {
        empty: "No conversions.",
        optional: ["Methodology", "Standing"],
      }));
      panel.appendChild(h);
      TaxTool.renderFXRoundTrip(panel);
      const m = card("Market data",
        "Prices used for the peak and closing values, with full provenance.");
      m.appendChild(table(d.market_audit, { empty: "No market prices used." }));
      panel.appendChild(m);
    },
    reconciliation(panel, d) {
      const c = card("Reconciliation", "Opening + vested + purchased + "
        + "transferred in − sold − withheld − transferred out = closing.");
      c.appendChild(table(d.reconciliation, { empty: "Nothing to reconcile." }));
      panel.appendChild(c);
      if (d.cross_broker && d.cross_broker.rows && d.cross_broker.rows.length) {
        const x = card("Cross-broker reconciliation",
          "Where shares moved between the client's own accounts.");
        x.appendChild(table(d.cross_broker));
        panel.appendChild(x);
      }
    },
    vesting(panel, d) {
      const c = card("Vesting and acquisition register",
        "Every lot the engine tracked, with the rate used for its acquisition date.");
      c.appendChild(table(d.vesting, { empty: "No lots." }));
      panel.appendChild(c);
    },
    review(panel, d) {
      const c = card("Exceptions requiring review before filing",
        "Blocker — a figure cannot be relied on until resolved. Review — a "
        + "documented approximation needing sign-off. Note — disclosure only.");
      const n = d.counts || {};
      c.appendChild(el("div", "verdict " + (n.Blocker ? "bad" : "ok"),
        n.Blocker ? `NOT READY TO FILE — ${n.Blocker} blocker(s) outstanding`
          : "No blockers. Review items still require sign-off."));
      const t = table(d.register, { empty: "No exceptions raised." });
      t.querySelectorAll("tbody tr").forEach((tr) => {
        const cell = tr.children[0];
        if (!cell) return;
        const s = cell.textContent.trim();
        if (["Blocker", "Review", "Note"].includes(s)) {
          cell.innerHTML = "";
          cell.appendChild(el("span", "sev " + s, s));
        }
      });
      c.appendChild(t);
      panel.appendChild(c);
    },
    sources(panel, d) {
      const c = card("Source documents",
        "What each file was taken to be, how confidently, and what it covers.");
      c.appendChild(table(d.source_table, { empty: "No documents ingested." }));
      panel.appendChild(c);
      const m = card("Normalised transaction master",
        "The common internal model. Every downstream figure derives from these "
        + "rows, and each row names the document, sheet and line it came from.");
      m.appendChild(table(d.master, { empty: "No transactions." }));
      panel.appendChild(m);
    },
    _raw(panel, d) {
      const c = card(d.section, null);
      Object.entries(d).forEach(([k, v]) => {
        if (k === "section") return;
        c.appendChild(el("h3", null, k));
        if (v && v.columns) c.appendChild(table(v));
        else c.appendChild(el("p", null, String(v)));
      });
      panel.appendChild(c);
    },
  },

  async renderFXRoundTrip(panel) {
    const c = card("Google Finance FX round-trip",
      "Where SBI has no rate for a date, Google Finance historical FX can supply "
      + "one. GOOGLEFINANCE() is a Google Sheets function, so the engine hands "
      + "you a consolidated request table — one row per currency/date pair, "
      + "never one per transaction. The other historical sources in the chain "
      + "above are read from tables in the configuration folder and need no "
      + "round-trip.");
    panel.appendChild(c);
    let d;
    try { d = await call(`${TaxTool.API}/jobs/${TaxTool.job.id}/fx/requests`); }
    catch (err) { c.appendChild(banner("err", err.message)); return; }

    const steps = el("ol", "steps");
    ["Run the working paper. Any date no ranked source could supply appears below.",
     "Copy the table and paste it into a Google Sheet — the rate column fills itself.",
     "Paste the completed rows back here, or upload the CSV.",
     "Re-run. The rates apply, and every use is labelled on this tab."]
      .forEach((s) => steps.appendChild(el("li", null, s)));
    c.appendChild(steps);

    if (!d.count) {
      c.appendChild(banner("ok",
        "No dates are outstanding — every conversion resolved from the rate table."));
      return;
    }
    c.appendChild(banner("warn",
      `${d.count} currency/date pair(s) have no rate on file.`));
    c.appendChild(table(d.requests));

    const pre = el("pre", "tsv", d.tsv);
    pre.hidden = true;
    const copy = el("button", "btn ghost", "Copy for Google Sheets");
    copy.style.marginTop = "12px";
    copy.onclick = async () => {
      try { await navigator.clipboard.writeText(d.tsv); copy.textContent = "Copied"; }
      catch (e) { pre.hidden = false; copy.textContent = "Select the text below"; }
      setTimeout(() => { copy.textContent = "Copy for Google Sheets"; }, 2500);
    };
    c.appendChild(copy);
    c.appendChild(pre);

    c.appendChild(el("h3", null, "Paste the completed rates back"));
    const ta = el("textarea", "paste");
    ta.placeholder = "date\tbase_currency\tquote_currency\trate\n"
      + "2024-07-01\tUSD\tINR\t83.4500";
    c.appendChild(ta);
    const msg = el("div");
    msg.style.marginTop = "10px";
    const row = el("div", "row");
    row.style.marginTop = "10px";
    const imp = el("button", "btn", "Import rates");
    imp.onclick = async () => {
      imp.disabled = true;
      try {
        const r = await call(`${TaxTool.API}/jobs/${TaxTool.job.id}/fx/rates`,
          { method: "POST", body: form({ text: ta.value }) });
        msg.innerHTML = "";
        msg.appendChild(banner("ok", r.message));
        ta.value = "";
      } catch (err) {
        msg.innerHTML = "";
        msg.appendChild(banner("err", err.message));
      } finally { imp.disabled = false; }
    };
    row.appendChild(imp);
    const up = el("input");
    up.type = "file"; up.accept = ".csv,.tsv,.txt"; up.hidden = true;
    up.onchange = async () => {
      const fd = new FormData();
      fd.append("file", up.files[0]);
      fd.append("text", "");
      try {
        const r = await call(`${TaxTool.API}/jobs/${TaxTool.job.id}/fx/rates`,
          { method: "POST", body: fd });
        msg.innerHTML = "";
        msg.appendChild(banner("ok", r.message));
      } catch (err) {
        msg.innerHTML = "";
        msg.appendChild(banner("err", err.message));
      }
    };
    const upbtn = el("button", "btn ghost", "Upload CSV instead");
    upbtn.onclick = () => up.click();
    row.appendChild(upbtn);
    row.appendChild(up);
    const rerun = el("button", "btn ghost", "Go to processing to re-run");
    rerun.onclick = () => go("tax", "processing");
    row.appendChild(rerun);
    c.appendChild(row);
    c.appendChild(msg);
    c.appendChild(el("p", "note",
      "A row with no usable positive rate is skipped, never read as zero — a "
      + "date Google cannot price must stay unresolved. Imported rates are "
      + "merged into the Google Finance source only, never into the SBI table."));
  },
};

/* Tools register their UI here, keyed by the manifest id. A reserved tool has
 * no entry - the shell renders its placeholder instead. */
const TOOL_UI = { tax: TaxTool };

/* ---------------------------------------------------------------- boot */
async function boot() {
  const s = await call("/api/session");
  document.title = s.app_name;
  $("#appname").textContent = s.app_name;
  $("#loginapp").textContent = `Sign in to ${s.app_name}`;
  if (!s.authenticated) { showLogin(); return; }
  const m = await call("/api/tools");
  TOOLS = m.tools;
  $("#appname").textContent = m.app_name;
  $("#sidemeta").textContent =
    `${TOOLS.filter((t) => t.status === "available").length} tool(s) available`;
  showShell();
  const first = TOOLS.find((t) => t.status === "available") || TOOLS[0];
  go(first.id, first.sections.length ? first.sections[0].id : null);
}
boot();
