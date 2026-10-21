// HVSR Analyzer pages. Every user-visible string comes from the #i18n JSON catalog subset.
// The upload token is kept in localStorage and sent only in the Authorization header.
"use strict";

(function () {
  const STRINGS = JSON.parse(document.getElementById("i18n").textContent);
  const TOKEN_KEY = "hvsr.uploadToken";
  const POLL_MS = 2000;

  function S(key, values) {
    let text = STRINGS[key];
    if (text === undefined) throw new Error("i18n:" + key);
    for (const [k, v] of Object.entries(values || {})) text = text.split("{" + k + "}").join(String(v));
    return text;
  }

  function el(tag, text, attrs) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    return node;
  }

  function show(target, ...nodes) {
    target.replaceChildren(...nodes);
  }

  function token() {
    const input = document.querySelector("[data-token]");
    const value = (input && input.value.trim()) || localStorage.getItem(TOKEN_KEY) || "";
    if (input && input.value.trim()) localStorage.setItem(TOKEN_KEY, input.value.trim());
    return value;
  }

  function authHeaders() {
    const value = token();
    return value ? { Authorization: "Bearer " + value } : {};
  }

  function detailText(body) {
    if (!body) return "";
    const d = body.detail !== undefined ? body.detail : body;
    if (typeof d === "string") return d;
    if (d && d.detail) return d.detail;
    if (d && d.problems) return d.problems.join("; ");
    return JSON.stringify(d);
  }

  async function readJson(resp) {
    try {
      return await resp.json();
    } catch (e) {
      return null;
    }
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function pollJob(url, onState) {
    for (;;) {
      const resp = await fetch(url, { headers: { Accept: "application/json" } });
      const job = await readJson(resp);
      if (!resp.ok || !job) throw new Error(S("error.request_failed"));
      onState(job);
      if (job.state === "succeeded" || job.state === "dead") return job;
      await sleep(POLL_MS);
    }
  }

  // ------------------------------------------------------------------ token field
  const tokenInput = document.querySelector("[data-token]");
  if (tokenInput && localStorage.getItem(TOKEN_KEY)) tokenInput.placeholder = S("upload.token_saved");
  const forget = document.querySelector("[data-js=forget-token]");
  if (forget)
    forget.addEventListener("click", () => {
      localStorage.removeItem(TOKEN_KEY);
      if (tokenInput) {
        tokenInput.value = "";
        tokenInput.placeholder = S("upload.token_forgotten");
      }
    });

  // ------------------------------------------------------------ on-request downloads
  document.querySelectorAll("a[data-ondemand]").forEach((link) => {
    link.addEventListener("click", async (event) => {
      event.preventDefault();
      const status = link.parentElement.parentElement.querySelector(".status") || link.nextElementSibling;
      const say = (text) => status && (status.textContent = " " + text);
      try {
        for (;;) {
          const resp = await fetch(link.href, { headers: { Accept: "application/json" }, redirect: "manual" });
          if (resp.status !== 202) {
            if (resp.status === 503) {
              const body = await readJson(resp);
              say(S("download.failed", { error: detailText(body) }));
              return;
            }
            window.location.href = link.href;
            return;
          }
          const body = await readJson(resp);
          say(S("download.generating"));
          const job = await pollJob(body.job_url, () => {});
          if (job.state === "dead") {
            say(S("download.failed", { error: job.error || "" }));
            return;
          }
        }
      } catch (e) {
        say(e.message);
      }
    });
  });

  // ---------------------------------------------------------------------- reports
  const report = document.querySelector("form[data-js=report]");
  if (report)
    report.addEventListener("submit", async (event) => {
      event.preventDefault();
      const status = report.querySelector(".status");
      const resp = await fetch(report.action, { method: "POST", body: new FormData(report) });
      status.textContent = resp.ok ? S("report.sent") : detailText(await readJson(resp));
      if (resp.ok) report.reset();
    });

  // ----------------------------------------------------------------- configuration
  function setPath(obj, path, value) {
    const keys = path.split(".");
    let node = obj;
    for (const k of keys.slice(0, -1)) {
      if (node[k] === null || node[k] === undefined) node[k] = {};
      node = node[k];
    }
    node[keys[keys.length - 1]] = value;
  }

  function fieldValue(input) {
    const type = input.dataset.type;
    if (type === "bool") return input.checked;
    if (type === "number") return Number(input.value);
    if (type === "number_or_null") return input.value === "" ? null : Number(input.value);
    return input.value;
  }

  const configure = document.querySelector("form[data-js=configure]");
  if (configure)
    configure.addEventListener("submit", async (event) => {
      event.preventDefault();
      const status = document.getElementById("configure-status");
      const req = JSON.parse(document.getElementById("request-defaults").textContent);
      configure.querySelectorAll("[data-path]").forEach((input) => setPath(req, input.dataset.path, fieldValue(input)));
      const bp = req.config.preprocessing.bandpass;
      if (bp && bp.low_hz === null && bp.high_hz === null) req.config.preprocessing.bandpass = null;
      if (!token()) {
        show(status, el("p", S("upload.token_missing")));
        return;
      }
      const resp = await fetch("/api/v1/measurements/" + configure.dataset.measurement + "/analyses", {
        method: "POST",
        headers: Object.assign({ "Content-Type": "application/json" }, authHeaders()),
        body: JSON.stringify(req),
      });
      const body = await readJson(resp);
      if (!resp.ok) {
        const d = body && body.detail;
        const nodes = [el("p", d && d.error === "compute_budget" ? S("configure.budget") : detailText(body))];
        if (d && d.problems) nodes.push(el("p", d.problems.join("; ")), el("p", d.advice));
        show(status, ...nodes);
        return;
      }
      const estimate = el("pre", JSON.stringify(body.estimate, null, 1));
      const state = el("p", S("configure.state", { job: body.job_id, state: body.state }));
      show(status, el("p", S("configure.estimate")), estimate, state);
      const job = await pollJob(body.job_url, (j) => (state.textContent = S("configure.state", { job: j.job_id, state: j.state })));
      if (job.state === "succeeded") {
        const link = el("a", S("configure.open_run"), { href: "/runs/" + job.result.run_id });
        show(status, el("p", S("configure.done")), link);
      } else {
        show(status, el("p", S("configure.failed", { error: job.error || "" })));
      }
    });

  // ------------------------------------------------------------------------- upload
  const upload = document.querySelector("form[data-js=upload]");
  if (!upload) return;
  const f = (name) => upload.elements[name];
  const text = (name) => (f(name).value.trim() === "" ? null : f(name).value.trim());
  const number = (name) => (f(name).value === "" ? null : Number(f(name).value));

  function mapping(prefix) {
    const out = {};
    for (const c of ["N", "E", "Z"]) if (text(prefix + c)) out[c] = text(prefix + c);
    return Object.keys(out).length ? out : null;
  }

  function waveformOptions() {
    return {
      csv_layout: f("csv_layout").value,
      csv_sampling_rate_hz: number("csv_sampling_rate_hz"),
      csv_start_time_utc: text("csv_start_time_utc"),
      csv_timestamps_declared_utc: f("csv_timestamps_declared_utc").checked,
      csv_column_map: mapping("col_"),
      miniseed_mapping: mapping("ms_"),
      canonical_only: f("canonical_only").checked,
    };
  }

  function files(withExtras) {
    const form = new FormData();
    for (const file of f("waveform").files) {
      form.append("files", file);
      form.append("roles", "waveform");
    }
    const extras = withExtras ? ["stationxml", "photo_sensor", "photo_area"] : ["stationxml"];
    for (const role of extras)
      for (const file of f(role).files) {
        form.append("files", file);
        form.append("roles", role);
      }
    return form;
  }

  upload.querySelector("[data-js=preview]").addEventListener("click", async () => {
    const out = document.getElementById("preview");
    show(out, el("p", S("upload.checking")));
    const form = files(false);
    form.append("waveform", JSON.stringify(waveformOptions()));
    const resp = await fetch("/api/v1/previews", { method: "POST", body: form, headers: authHeaders() });
    const body = await readJson(resp);
    if (!resp.ok) {
      show(out, el("h3", S("preview.errors")), el("p", detailText(body)));
      return;
    }
    const s = body.summary;
    const dl = el("dl", null, { class: "kv" });
    const rows = [
      [S("preview.fs"), s.sampling_rate_hz],
      [S("preview.components"), s.channels.map((c) => c.component + " (" + (c.units || "?") + ")").join(", ")],
      [S("preview.start"), s.start_utc || S("value.none")],
      [S("preview.duration"), s.duration_s],
      [S("preview.samples"), s.samples_per_channel],
      [S("preview.gaps"), s.gaps],
      [S("preview.clipping"), Object.values(s.clipped_samples).reduce((a, b) => a + b, 0)],
      [S("preview.timing"), s.timing_irregular ? S("value.yes") : S("value.no")],
    ];
    for (const [k, v] of rows) dl.append(el("dt", k), el("dd", v));
    const nodes = [el("h3", S("preview.title")), dl];
    if (s.warnings.length) {
      const ul = el("ul");
      s.warnings.forEach((w) => ul.append(el("li", w)));
      nodes.push(el("h4", S("preview.warnings")), ul);
    }
    show(out, ...nodes);
  });

  upload.addEventListener("submit", async (event) => {
    event.preventDefault();
    const out = document.getElementById("submit-status");
    if (!token()) {
      show(out, el("p", S("upload.token_missing")));
      return;
    }
    show(out, el("p", S("upload.submitting")));
    const channels = [];
    if (f("quantity").value !== "unknown" || text("units"))
      for (const c of ["N", "E", "Z"]) channels.push({ component: c, quantity: f("quantity").value, units: text("units") });
    const submission = {
      idempotency_key: crypto.randomUUID(),
      waveform: waveformOptions(),
      metadata: {
        latitude: number("latitude"),
        longitude: number("longitude"),
        wind: f("wind").value,
        traffic: f("traffic").value,
        measurement_context: f("measurement_context").value,
        ground_surface: text("ground_surface"),
        floor_or_height: text("floor_or_height"),
        coupling_notes: text("coupling_notes"),
        observations: text("observations"),
        site_group_id: text("site_group_id"),
        sensor: {
          manufacturer: text("manufacturer"),
          model: text("model"),
          instrument_id: text("instrument_id"),
          sensor_type: text("sensor_type"),
          natural_frequency_hz: number("natural_frequency_hz"),
          usable_fmin_hz: number("usable_fmin_hz"),
          usable_fmax_hz: number("usable_fmax_hz"),
          channels: channels,
        },
      },
      consent: {
        consent_checked: f("consent_checked").checked,
        rights_confirmed: f("rights_confirmed").checked,
        attribution: f("attribution").value.trim(),
        source_citation: text("source_citation"),
        consent_version: f("consent_version").value,
      },
    };
    const form = files(true);
    form.append("submission", JSON.stringify(submission));
    const resp = await fetch("/api/v1/submissions", { method: "POST", body: form, headers: authHeaders() });
    const body = await readJson(resp);
    if (!resp.ok) {
      show(out, el("p", S("upload.rejected", { status: resp.status, detail: detailText(body) })));
      return;
    }
    show(out, el("p", S("upload.accepted", { code: body.record_code })), el("a", S("upload.open"), { href: body.page_url }));
  });
})();
