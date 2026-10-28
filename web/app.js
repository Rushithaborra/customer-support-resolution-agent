// Shared helpers for the demo UI. Plain browser JS, no build step.
//
// Two rules hold everywhere:
// - Every value goes into the page through textContent (el() below), never innerHTML.
//   Customer messages and tool output are untrusted text (CLAUDE.md rule 7).
// - Customer-facing wording is never composed here. The UI shows the backend's
//   `outcome` field, which is built from provider-confirmed state only (rule 8).

const SESSION_KEY = "support-demo-session";

const session = {
  get() {
    try { return JSON.parse(sessionStorage.getItem(SESSION_KEY)); } catch { return null; }
  },
  set(value) { sessionStorage.setItem(SESSION_KEY, JSON.stringify(value)); },
  clear() { sessionStorage.removeItem(SESSION_KEY); },
};

class ApiError extends Error {
  constructor(status, detail) {
    const code = detail && typeof detail === "object" ? detail.code : null;
    super(code ? `${status} ${code}${detail.message ? ": " + detail.message : ""}` : `${status} ${detail ?? ""}`);
    this.status = status;
    this.code = code;
  }
}

async function api(path, { method = "GET", body, auth = true } = {}) {
  const headers = { Accept: "application/json" };
  const s = session.get();
  if (auth && s) headers.Authorization = `Bearer ${s.token}`;
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  const data = r.headers.get("content-type")?.includes("json") ? await r.json() : null;
  if (!r.ok) throw new ApiError(r.status, data?.detail ?? r.statusText);
  return data;
}

// el("li", {class: "x", onclick: fn}, "text", childNode, ...): children are strings (text) or nodes.
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (k === "class") node.className = v;
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function mount(target, ...children) {
  const node = typeof target === "string" ? document.getElementById(target) : target;
  node.replaceChildren(...children.flat().filter((c) => c !== null && c !== undefined && c !== false));
  return node;
}

// Money arrives as integer paise; format only, never compute.
function rupees(minor) {
  if (!Number.isInteger(minor)) return "?";
  return "₹" + (minor / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function time(iso) {
  return iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
}

const TONE = {
  resolved: "ok", succeeded: "ok", completed: "ok", executed: "ok", approved: "ok",
  awaiting_approval: "info", pending_approval: "info", queued: "info", running: "info", paused: "info",
  open: "info", reserved: "info",
  awaiting_provider: "warn", submitted: "warn", processing: "warn", pending_reconciliation: "warn",
  escalated: "warn", superseded: "warn", expired: "warn",
  failed: "bad", rejected: "bad", dead: "bad",
};

function badge(status) {
  return el("span", { class: `badge ${TONE[status] || ""}` }, status ?? "-");
}

function errorBox(err) {
  return el("div", { class: "error" }, err instanceof Error ? err.message : String(err));
}

// Repeats fn every ms milliseconds; the next run starts only after the previous one finished.
function poll(fn, ms) {
  let stopped = false;
  const tick = async () => {
    if (stopped) return;
    try { await fn(); } catch (e) { console.warn(e); }
    if (!stopped) setTimeout(tick, ms);
  };
  tick();
  return () => { stopped = true; };
}

// Page guard + header: redirect to the persona picker unless the right kind of user is signed in.
async function startPage(allowedRoles) {
  const s = session.get();
  if (!s || !allowedRoles.includes(s.role)) {
    location.href = "/ui/";
    return null;
  }
  const header = document.querySelector("header.top");
  if (header) {
    let model = "";
    try { model = (await api("/health", { auth: false })).model; } catch { /* label is optional */ }
    mount(header,
      el("h1", {}, "Support Resolution Agent"),
      el("span", { class: "who" }, `Signed in as ${s.label}`),
      model ? el("span", { class: "model-tag", title: "LLM backend" }, `model: ${model}`) : null,
      el("span", { class: "spacer" }),
      el("button", { onclick: () => { session.clear(); location.href = "/ui/"; } }, "Switch user"));
  }
  return s;
}
