// The relay's console: reads the API with the key the user enters (kept in this tab's
// sessionStorage only), sets the relay up (relay/setup.py writes the files) and offers the few
// actions an owner needs at hand. No outside scripts, and no HTML built from text.
"use strict";

const KEY = "relay.apiKey";
const REFRESH_MS = 10000;
let timer = null;
let deployment = "";
let setupCache = null;      // GET /v1/setup, refreshed with each view
let catalogCache = null;    // GET /v1/setup/catalog, loaded once

function store() {
  try { return window.sessionStorage; } catch (e) { return null; }
}

function key() {
  const s = store();
  return (s && s.getItem(KEY)) || "";
}

// A change in .env reaches the relay only when its container is made again: the Restart button
// below restarts the process, which keeps the environment it started with.
const ENV_RESTART = "then run relay restart on the server (without the install script: docker compose up -d). The Restart button here does not read .env again.";

async function api(path, options = {}) {
  const r = await fetch(path, {
    ...options,
    headers: { "Authorization": "Bearer " + key(), "Content-Type": "application/json" },
    cache: "no-store",
  });
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  if (r.status === 401) { signOut("The key was not accepted."); throw new Error("unauthorised"); }
  // Locked out after wrong keys, or an address the allowlist does not name: no key helps now.
  const detail = (body && typeof body.detail === "string") ? body.detail : "";
  if ((r.status === 429 && detail.includes("wrong API keys")) || (r.status === 403 && detail.includes("ALLOW_IPS"))) {
    signOut(detail);
    throw new Error("refused");
  }
  return { status: r.status, ok: r.ok, body };
}

function send(path, method, data) {
  return api(path, { method, body: data === undefined ? undefined : JSON.stringify(data) });
}

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === false || v == null) continue;
    if (k === "class") node.className = v;
    else if (k === "for") node.htmlFor = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

function table(rows, columns, empty) {
  if (!rows.length) return el("p", { class: "empty" }, empty);
  const head = el("tr", {}, ...columns.map(([label]) => el("th", { scope: "col" }, label)));
  const body = rows.map((row) => el("tr", {}, ...columns.map(([, value]) => {
    const v = value(row);
    return el("td", {}, v instanceof Node ? v : (v == null || v === "" ? "–" : String(v)));
  })));
  return el("div", { class: "scroll" }, el("table", {}, el("thead", {}, head), el("tbody", {}, ...body)));
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return isNaN(d) ? iso : d.toLocaleString();
}

function fill(id, ...nodes) {
  document.getElementById(id).replaceChildren(...nodes);
}

function mark(ok) {
  return el("span", { class: ok ? "ok" : "missing" }, ok ? "✓" : "✗");
}

function copyBlock(text) {
  const pre = el("pre", { class: "code" }, text);
  const b = el("button", { type: "button", class: "small", onclick: async () => {
    try { await navigator.clipboard.writeText(text); b.textContent = "Copied"; } catch (e) { b.textContent = "Select and copy"; }
    setTimeout(() => { b.textContent = "Copy"; }, 2000);
  } }, "Copy");
  return el("div", { class: "copy" }, pre, b);
}

function message(node, ok, text) {
  node.className = ok ? "result ok" : "result error";
  node.textContent = text;
}

function randomSecret() {
  const bytes = new Uint8Array(24);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
}

function envName(prefix, name, suffix) {
  return `RELAY_${prefix}_${name.toUpperCase().replace(/[^A-Z0-9]/g, "_")}_${suffix}`;
}

function needRestart() {
  document.getElementById("restart-banner").hidden = false;
}

// ---- status, home -------------------------------------------------------------------------------

async function loadStatus() {
  const { body: s } = await api("/v1/status");
  deployment = s.deployment;
  const badges = [
    el("span", { class: "badge " + (s.dry_run ? "paper" : "live") }, s.dry_run ? "PAPER" : "LIVE"),
    el("span", {}, `${s.deployment} · ${s.venue} (${s.environment})`),
  ];
  if (s.halted) badges.push(el("span", { class: "badge halted" }, "HALTED: " + s.halted));
  else if (!s.entries_enabled) badges.push(el("span", { class: "badge halted" }, "entries off"));
  if (!s.engine_running) badges.push(el("span", { class: "badge halted" }, "engine stopped"));
  document.getElementById("status").replaceChildren(...badges);
}

async function loadSetup() {
  const r = await api("/v1/setup");
  setupCache = r.body;
  if (setupCache.restart_needed) needRestart();
  return setupCache;
}

function allSet(variables) {
  const v = Object.values(variables || {});
  return v.length > 0 && v.every(Boolean);
}

async function loadHome() {
  const s = await loadSetup();
  const steps = [
    [s.deployment_file, "An exchange is chosen", "Setup, step 1"],
    [s.keys_set, "The exchange's keys are set (needed for live, and to read the account)", "Setup, step 1"],
    [s.missing_limits.length === 0, "The core limits are filled in", "Setup, step 2"],
    [s.sources.length > 0, "A signal source can send", "Setup, step 3"],
    [s.sinks.length > 0, "Notifications reach you", "Setup, step 4"],
  ];
  const open = steps.filter(([ok]) => !ok);
  const { body: positions } = await api("/v1/positions?status=open");
  const { body: events } = await api("/v1/events?limit=5");
  fill("home",
    el("p", {}, s.paper ? "Paper trading: orders are simulated, nothing reaches the exchange."
                        : "LIVE: orders go to the exchange."),
    el("h3", {}, open.length ? "Next steps" : "Set up"),
    el("ul", { class: "checklist" }, ...steps.map(([ok, what, where]) =>
      el("li", {}, mark(ok), " ", what, ok ? "" : el("span", { class: "muted" }, ` - ${where}`)))),
    el("h3", {}, "Now"),
    el("p", {}, `${positions.length} open position(s).`),
    table(events, [["When", (e) => when(e.created_at)],
                   ["Severity", (e) => el("span", { class: "sev " + e.severity }, e.severity)],
                   ["Message", (e) => e.message]], "No events yet."));
}

// ---- setup: the exchange -----------------------------------------------------------------------------

async function deploymentFile(s) {
  if (!s.deployment_file) return {};
  const r = await api(`/v1/setup/deployment/${encodeURIComponent(s.deployment)}`);
  return r.ok ? r.body : {};
}

async function saveDeployment(data, out) {
  const s = setupCache;
  const r = await send(`/v1/setup/deployment/${encodeURIComponent(s.deployment)}`, "PUT", data);
  if (r.ok) {
    message(out, !r.body.back_to_paper, r.body.back_to_paper
      ? "Saved, and the file is back on paper: the change touched what going live checked (the exchange or a core limit). Go live again in step 6."
      : "Saved.");
    needRestart();
  } else message(out, false, r.body.reason || "Not saved.");
  return r.ok;
}

async function renderVenue(s) {
  if (!catalogCache) {
    fill("setup-venue", el("p", { class: "muted" }, "Loading the exchanges…"));
    catalogCache = (await api("/v1/setup/catalog")).body;
  }
  const cat = catalogCache;
  const file = await deploymentFile(s);
  const venue = file.venue || {};
  const choices = [cat.native, ...cat.profiled];
  const select = el("select", { id: "v-exchange" },
    ...choices.map((c) => el("option", { value: c.exchange }, c.title + (c.verified ? " (verified)" : " (community)"))),
    el("option", { value: "_other" }, "Another CCXT exchange…"));
  const other = el("input", { id: "v-other", placeholder: "its CCXT id, e.g. kraken", list: "ccxt-ids" });
  const ids = el("datalist", { id: "ccxt-ids" }, ...cat.others.map((x) => el("option", { value: x })));
  const env = el("select", { id: "v-env" });
  const market = el("select", { id: "v-market" }, el("option", { value: "perpetual" }, "perpetual futures"),
    el("option", { value: "spot" }, "spot (buy coins, long only)"));
  const dedicated = el("input", { id: "v-dedicated", type: "checkbox" });
  const info = el("div", { class: "info" });
  const out = el("p", { class: "result", role: "status" });
  const current = venue.exchange || "bybit";
  const known = choices.find((c) => c.exchange === current);
  select.value = known ? current : "_other";
  if (!known) other.value = current;
  market.value = venue.market || "perpetual";
  dedicated.checked = !!venue.dedicated_account;

  function chosen() {
    if (select.value !== "_other") return choices.find((c) => c.exchange === select.value);
    const id = other.value.trim().toLowerCase();
    return { exchange: id, title: id, verified: false, environments: ["testnet", "live"], keys:
      [`RELAY_${id.toUpperCase()}_API_KEY`, `RELAY_${id.toUpperCase()}_API_SECRET`], can: null, spot: true };
  }
  function update() {
    const c = chosen();
    other.hidden = select.value !== "_other";
    const native = c.exchange === "bybit" && market.value !== "spot";
    env.replaceChildren(...(native ? [el("option", { value: "" }, "set by RELAY_BYBIT_DEMO / RELAY_TESTNET in .env")]
      : c.environments.map((e) => el("option", { value: e }, e === "live" ? "live (real money)" : e))));
    if (!native && venue.environment && c.environments.includes(venue.environment)) env.value = venue.environment;
    else if (!native) env.value = c.environments[0];
    const keys = native ? cat.native.keys : c.keys;
    const set = Object.fromEntries((s.keys || []).map((k) => [k.variable, k.set]));
    const can = c.can ? Object.entries(c.can).filter(([, v]) => !v).map(([k]) => k.replace("_", " ")) : [];
    info.replaceChildren(
      el("p", {}, c.verified ? "Verified: run against a real test account by the project."
        : "Community connector: run the venue check (step 5) on its test account before trusting it."),
      can.length ? el("p", {}, "Cannot: " + can.join(", ") + ". Settings that need these are refused.") : "",
      c.notes ? el("p", { class: "muted" }, c.notes) : "",
      el("p", {}, "Keys: set these on the server (trade-only keys, never withdrawal) with relay secret set <NAME>, " + ENV_RESTART + " Not needed on paper."),
      copyBlock(keys.map((k) => `${k}=`).join("\n")),
      el("ul", { class: "checklist" }, ...keys.map((k) => el("li", {}, mark(set[k]), " ", k,
        set[k] ? " is set" : (k in set ? " is not set" : " (saved exchange not this one yet)")))));
    dedicated.parentElement.hidden = market.value !== "spot";
  }
  for (const n of [select, other, market]) n.addEventListener("change", update);
  other.addEventListener("input", update);

  const save = el("button", { type: "button", onclick: async () => {
    const c = chosen();
    if (!/^[a-z0-9]{2,30}$/.test(c.exchange)) { message(out, false, "Choose an exchange."); return; }
    const native = c.exchange === "bybit" && market.value !== "spot";
    if (env.value === "live" && !window.confirm("Live means real money once the deployment leaves paper. Save live as the environment?")) return;
    const data = { ...file, venue: { exchange: c.exchange, environment: native ? null : env.value,
      market: market.value, dedicated_account: market.value === "spot" ? dedicated.checked : false } };
    if (await saveDeployment(data, out)) refresh();
  } }, "Save the exchange");

  fill("setup-venue",
    el("label", { for: "v-exchange" }, "Exchange"), select, other, ids,
    el("label", { for: "v-market" }, "Market"), market,
    el("label", { for: "v-env" }, "Environment"), env,
    el("label", { class: "check" }, dedicated, " This account is used by the relay only (spot needs it: every coin held is a position)"),
    info, save, out);
  update();
}

// ---- setup: the limits -------------------------------------------------------------------------------

const LIMITS = [
  ["General", [
    ["enabled", "bool", "Take new entries", "Off: no new positions; closes, stops and the emergency stop still work.", true],
    ["require_stop", "bool", "Every entry needs a stop", "Recommended. Off only for venues or strategies without one.", true],
    ["paper_equity", "number", "Paper account, USDT", "Only for paper without exchange keys: the balance it pretends to have. Empty: paper entries are refused until keys are set."],
    ["leverage", "number", "Leverage set on the exchange", "Empty: the account's own leverage, never changed."],
    ["isolated_margin", "tri", "Margin mode", "Empty: the account's own mode, never changed."],
  ]],
  ["Size and exposure", [
    ["rails.max_notional_usd", "number", "Largest position, USDT", "One of these two is needed to go live."],
    ["rails.max_notional_pct", "number", "Largest position, % of equity", ""],
    ["rails.max_gross_leverage", "number", "Largest book, gross leverage", "Open positions plus the new one, over equity. Needed to go live."],
    ["rails.on_limit", "select:clip,reject", "Over a limit", "clip shrinks the order to fit; reject refuses it."],
    ["risk.max_positions", "int", "Positions at once", "Needed to go live."],
    ["risk.max_exposure_pct", "number", "Total exposure, % of equity", ""],
    ["risk.min_balance", "number", "Lowest balance to trade on, USDT", ""],
  ]],
  ["Losses and stops", [
    ["risk.daily_loss_limit_pct", "number", "Daily loss limit, % of equity", "New entries are refused for the rest of the day. Needed to go live."],
    ["risk.min_stop_distance_pct", "number", "Closest stop, % from entry", ""],
    ["risk.max_stop_distance_pct", "number", "Farthest stop, % from entry", ""],
    ["risk.cooldown_hours", "number", "Pause on a symbol after a loss, hours", ""],
    ["rails.stop_before_bankruptcy", "bool", "Stop before the bankruptcy price (isolated margin)", "", true],
  ]],
  ["Managing open positions (defaults for signals that do not say)", [
    ["position_management.take_profit_pct", "number", "Take-profit, % from entry", ""],
    ["position_management.trailing_distance_pct", "number", "Trailing stop distance, %", ""],
    ["position_management.trailing_activate_pct", "number", "Trailing starts at, % profit", ""],
    ["position_management.break_even_trigger_pct", "number", "Stop to entry at, % profit", ""],
    ["position_management.break_even_buffer_pct", "number", "Break-even buffer, %", ""],
    ["position_management.max_hold_hours", "number", "Time stop: close after, hours", "A position open this long is closed at market."],
  ]],
  ["Symbols", [
    ["rails.symbols.allow", "list", "Only these symbols", "Comma-separated, e.g. BTCUSDT, ETHUSDT. Empty: any."],
    ["rails.symbols.deny", "list", "Never these symbols", "Comma-separated."],
  ]],
];

function getPath(obj, path) {
  return path.split(".").reduce((o, k) => (o && typeof o === "object" ? o[k] : undefined), obj);
}

function setPath(obj, path, value) {
  const parts = path.split(".");
  let o = obj;
  for (const k of parts.slice(0, -1)) { if (!o[k] || typeof o[k] !== "object") o[k] = {}; o = o[k]; }
  if (value === undefined) delete o[parts[parts.length - 1]]; else o[parts[parts.length - 1]] = value;
}

function field([path, type, label, help, def]) {
  const id = "f-" + path.replace(/\./g, "-");
  let input;
  if (type === "bool") input = el("input", { id, type: "checkbox" });
  else if (type === "tri") input = el("select", { id }, el("option", { value: "" }, "leave as it is"),
    el("option", { value: "true" }, "isolated"), el("option", { value: "false" }, "cross"));
  else if (type.startsWith("select:")) input = el("select", { id }, ...type.slice(7).split(",").map((v) => el("option", { value: v }, v)));
  else input = el("input", { id, inputmode: type === "list" ? "text" : "decimal", placeholder: "empty = off" });
  return { path, type, def, input, node: el("div", { class: "field" },
    type === "bool" ? el("label", { class: "check", for: id }, input, " ", label) : [el("label", { for: id }, label), input],
    help ? el("small", { class: "muted" }, help) : "") };
}

function readField(f) {
  const v = f.input.type === "checkbox" ? f.input.checked : f.input.value.trim();
  if (f.type === "bool") return v;
  if (f.type === "tri") return v === "" ? undefined : v === "true";
  if (f.type.startsWith("select:")) return v;
  if (v === "") return undefined;
  if (f.type === "list") return v.split(",").map((x) => x.trim()).filter(Boolean);
  const n = Number(v.replace(",", "."));
  if (!isFinite(n)) throw new Error(`${f.path}: "${v}" is not a number`);
  if (f.type === "int" && !Number.isInteger(n)) throw new Error(`${f.path}: a whole number`);
  return n;
}

async function renderLimits(s) {
  if (!s.deployment_file) { fill("setup-limits", el("p", { class: "muted" }, "Choose the exchange first (step 1).")); return; }
  const file = await deploymentFile(s);
  const fields = [];
  const groups = LIMITS.map(([title, defs]) => el("fieldset", {}, el("legend", {}, title), ...defs.map((d) => {
    const f = field(d);
    const v = getPath(file, f.path);
    if (f.type === "bool") f.input.checked = v == null ? !!f.def : !!v;
    else if (f.type === "tri") f.input.value = v == null ? "" : String(v);
    else if (f.type.startsWith("select:")) f.input.value = v || f.type.slice(7).split(",")[0];
    else if (f.type === "list") f.input.value = (v || []).join(", ");
    else f.input.value = v == null ? "" : String(v);
    fields.push(f);
    return f.node;
  })));
  const out = el("p", { class: "result", role: "status" });
  const save = el("button", { type: "button", onclick: async () => {
    const data = JSON.parse(JSON.stringify(file));
    try { for (const f of fields) setPath(data, f.path, readField(f)); } catch (e) { message(out, false, e.message); return; }
    if (await saveDeployment(data, out)) refresh();
  } }, "Save the limits");
  const missing = s.missing_limits.length ? el("p", { class: "warn" }, "Needed before going live: " + s.missing_limits.join(", ") + ".") : "";
  fill("setup-limits", missing, ...groups,
    el("p", { class: "muted" }, "Take-profit ladders, trading hours and target-book settings are edited in the file (examples/config/ explains them); saving here keeps them."),
    save, out);
}

// ---- setup: sources -----------------------------------------------------------------------------------

const ACTIONS = ["open", "close", "reduce", "flip", "close_all", "move_stop", "target"];

function renderSources(s) {
  const list = table(s.sources, [
    ["Name", (x) => x.name], ["Proof", (x) => x.auth === "hmac" ? "signature" : "passphrase + addresses"],
    ["May send", (x) => x.actions.length ? x.actions.join(", ") : "everything"],
    ["Secret", (x) => el("span", {}, mark(allSet(x.variables)), " ", x.secret_env)],
    ["Running", (x) => x.running ? "yes" : "after a restart"], ["", (x) => el("button", { type: "button", class: "small danger", onclick: async (ev) => {
      if (!window.confirm(`Remove the source ${x.name}? Its signals are refused after the restart.`)) return;
      const r = await send(`/v1/setup/source/${encodeURIComponent(x.name)}`, "DELETE");
      if (r.ok) { needRestart(); ev.target.disabled = true; } else window.alert(r.body.reason);
    } }, "Remove")],
  ], "No source yet: nobody can send signals.");

  const name = el("input", { id: "s-name", placeholder: "e.g. tradingview or mybot" });
  const auth = el("select", { id: "s-auth" },
    el("option", { value: "passphrase" }, "Passphrase (TradingView, no-code tools)"),
    el("option", { value: "hmac" }, "Signature (your own scripts and agents)"));
  const ips = el("input", { id: "s-ips", placeholder: "the sender's addresses, comma-separated" });
  const anyIp = el("input", { id: "s-any", type: "checkbox" });
  const rate = el("input", { id: "s-rate", inputmode: "numeric", placeholder: "empty = no limit" });
  const acts = ACTIONS.map((a) => el("input", { type: "checkbox", value: a, checked: a === "open" || a === "close" }));
  const env = el("div", { class: "info" });
  const out = el("p", { class: "result", role: "status" });
  let secret = randomSecret();
  function update() {
    const n = name.value.trim().toLowerCase() || "<name>";
    const v = envName("SOURCE", n, "SECRET");
    ips.parentElement.hidden = auth.value !== "passphrase";
    env.replaceChildren(el("p", {}, "Add this line to .env (a fresh random secret, made in this browser), " + ENV_RESTART),
      copyBlock(`${v}=${secret}`),
      el("p", { class: "muted" }, "Keep it: the relay never shows it again. The sender needs it too (Connect tab)."));
  }
  name.addEventListener("input", () => { secret = randomSecret(); update(); });   // a new source, a new secret
  auth.addEventListener("change", update);
  const save = el("button", { type: "button", onclick: async () => {
    const n = name.value.trim().toLowerCase();
    const data = { auth: auth.value, secret_env: envName("SOURCE", n, "SECRET"), deployments: [s.deployment],
                   actions: acts.filter((c) => c.checked).map((c) => c.value) };
    if (!data.actions.length) { message(out, false, "Tick at least one thing it may send."); return; }
    if (data.actions.length === ACTIONS.length) delete data.actions;
    if (rate.value.trim()) data.max_signals_per_minute = Number(rate.value.trim());
    if (auth.value === "passphrase") {
      data.allow_ips = anyIp.checked ? ["0.0.0.0/0", "::/0"] : ips.value.split(",").map((x) => x.trim()).filter(Boolean);
      if (!data.allow_ips.length) { message(out, false, "List the sender's addresses (TradingView publishes its own), or allow any address."); return; }
    }
    const r = await send(`/v1/setup/source/${encodeURIComponent(n)}`, "PUT", data);
    if (r.ok) { message(out, true, "Saved. Add the .env line above, " + ENV_RESTART); needRestart(); }
    else message(out, false, r.body.reason || "Not saved.");
  } }, "Add the source");
  fill("setup-sources", list, el("h3", {}, "Add a source"),
    el("label", { for: "s-name" }, "Name"), name,
    el("label", { for: "s-auth" }, "How it proves itself"), auth,
    el("div", {}, el("label", { for: "s-ips" }, "Allowed addresses"), ips,
      el("label", { class: "check" }, anyIp, " Allow any address (for tools without fixed addresses: then the passphrase alone protects it, so keep what it may send small)")),
    el("label", { for: "s-rate" }, "Most signals per minute"), rate,
    el("fieldset", {}, el("legend", {}, "It may send"), ...acts.map((c) => el("label", { class: "check inline" }, c, " ", c.value))),
    env, save, out);
  update();
}

// ---- setup: notifications ---------------------------------------------------------------------------

const SINKS = {
  telegram: [["bot_token_env", "TOKEN", "the bot's token, from @BotFather"], ["chat_id_env", "CHAT", "the chat's id"]],
  discord: [["webhook_url_env", "URL", "the channel's webhook address (it starts with https)"]],
  webhook: [["url_env", "URL", "your receiver's address (it starts with https)"], ["secret_env", "SECRET", "optional: signs each event"]],
};

function renderSinks(s) {
  const list = table(s.sinks, [["Name", (x) => x.name], ["Type", (x) => x.type], ["From", (x) => x.min_severity],
    ["Values in .env", (x) => el("span", {}, mark(allSet(x.variables)), " ", Object.keys(x.variables).join(", "))],
    ["Running", (x) => x.running ? "yes" : "after a restart"],
    ["", (x) => el("button", { type: "button", class: "small danger", onclick: async (ev) => {
      const r = await send(`/v1/setup/sink/${encodeURIComponent(x.name)}`, "DELETE");
      if (r.ok) { needRestart(); ev.target.disabled = true; } else window.alert(r.body.reason);
    } }, "Remove")]], "No notifications yet.");
  const name = el("input", { id: "n-name", placeholder: "e.g. phone" });
  const type = el("select", { id: "n-type" }, ...Object.keys(SINKS).map((t) => el("option", { value: t }, t)));
  const sev = el("select", { id: "n-sev" }, ...["info", "warning", "critical"].map((v) => el("option", { value: v }, v)));
  const env = el("div", { class: "info" });
  const out = el("p", { class: "result", role: "status" });
  function vars() {
    const n = name.value.trim().toLowerCase() || "<name>";
    return SINKS[type.value].map(([k, suffix, what]) => [k, envName("SINK", n, suffix), what]);
  }
  function update() {
    env.replaceChildren(el("p", {}, "Add these lines to .env with their values (relay secret set <NAME>), " + ENV_RESTART),
      copyBlock(vars().map(([, v, what]) => `${v}=   # ${what}`).join("\n")));
  }
  name.addEventListener("input", update);
  type.addEventListener("change", update);
  const save = el("button", { type: "button", onclick: async () => {
    const n = name.value.trim().toLowerCase();
    const data = { type: type.value, min_severity: sev.value };
    for (const [k, v] of vars()) if (k !== "secret_env") data[k] = v;
    const r = await send(`/v1/setup/sink/${encodeURIComponent(n)}`, "PUT", data);
    if (r.ok) { message(out, true, "Saved. Add the .env lines, " + ENV_RESTART); needRestart(); }
    else message(out, false, r.body.reason || "Not saved.");
  } }, "Add the notification");
  fill("setup-sinks", list, el("h3", {}, "Add one"), el("label", { for: "n-name" }, "Name"), name,
    el("label", { for: "n-type" }, "Where"), type, el("label", { for: "n-sev" }, "From which severity"), sev,
    env, save, out);
  update();
}

// ---- setup: test ------------------------------------------------------------------------------------

function renderTest(s) {
  const sym = el("input", { id: "t-symbol", placeholder: "e.g. BTCUSDT" });
  const side = el("select", { id: "t-side" }, el("option", { value: "long" }, "long"), el("option", { value: "short" }, "short"));
  const size = el("input", { id: "t-size", inputmode: "decimal", placeholder: "USDT" });
  const stop = el("input", { id: "t-stop", inputmode: "decimal", placeholder: "%" });
  const out = el("p", { class: "result", role: "status" });
  const go = el("button", { type: "button", onclick: async () => {
    const body = { action: "open", symbol: sym.value.trim().toUpperCase(), side: side.value,
                   size: { notional_usd: Number(size.value) } };
    if (stop.value.trim()) body.stop = { pct: Number(stop.value) };
    const r = await send("/v1/setup/test-signal", "POST", body);
    if (!r.ok) { message(out, false, (r.body.reasons || [r.body.detail]).join("; ")); return; }
    message(out, true, "Sent on paper. Waiting for the outcome…");
    for (let i = 0; i < 20; i++) {
      await new Promise((res) => setTimeout(res, 1000));
      const row = (await api(`/v1/signals/${r.body.signal}`)).body;
      if (row.status !== "accepted") {
        message(out, row.status === "executed", `${row.status}${row.reasons && row.reasons.length ? ": " + row.reasons.join("; ") : ""}`);
        return;
      }
    }
  } }, "Send a paper test signal");

  const checkable = s.venue && !(s.venue.exchange === "bybit" && s.venue.market !== "spot");
  const check = s.venue_check;
  const vsym = el("input", { id: "c-symbol", placeholder: "e.g. BTCUSDT" });
  const vsize = el("input", { id: "c-size", inputmode: "decimal", placeholder: "USDT (small)" });
  const vstop = el("input", { id: "c-stop", inputmode: "decimal", placeholder: "%" });
  const venv = el("select", { id: "c-env" }, el("option", { value: "testnet" }, "testnet"), el("option", { value: "demo" }, "demo"));
  if (s.venue && ["demo", "testnet"].includes(s.venue.environment)) venv.value = s.venue.environment;
  const vout = el("p", { class: "result", role: "status" });
  const vgo = el("button", { type: "button", onclick: async () => {
    if (!window.confirm("This trades on the exchange's TEST account: it opens a small position, moves its stop and closes it. Go on?")) return;
    const r = await send("/v1/setup/venue-check", "POST", { symbol: vsym.value.trim().toUpperCase(), notional: Number(vsize.value),
                                                           stop_pct: Number(vstop.value), environment: venv.value });
    if (r.ok) { message(vout, true, "Running: this takes a minute or two."); watchCheck(); }
    else message(vout, false, r.body.reason || "Not started.");
  } }, "Run the venue check");
  fill("setup-test",
    el("h3", {}, "A paper test signal"),
    el("p", {}, "It runs the whole way through the relay - checks, limits, the simulated fill - and never reaches the exchange."),
    el("div", { class: "grid" }, el("div", {}, el("label", { for: "t-symbol" }, "Symbol"), sym),
      el("div", {}, el("label", { for: "t-side" }, "Side"), side), el("div", {}, el("label", { for: "t-size" }, "Size, USDT"), size),
      el("div", {}, el("label", { for: "t-stop" }, "Stop, % from entry"), stop)), go, out,
    el("h3", {}, "The venue check"),
    ...(checkable ? [
      el("p", {}, "Before going live on a community connector, run it once on the exchange's test account with the test keys set."),
      el("div", { class: "grid" }, el("div", {}, el("label", { for: "c-symbol" }, "Symbol"), vsym),
        el("div", {}, el("label", { for: "c-size" }, "Size, USDT"), vsize), el("div", {}, el("label", { for: "c-stop" }, "Stop, %"), vstop),
        el("div", {}, el("label", { for: "c-env" }, "Test account"), venv)),
      vgo, vout,
      check ? el("div", {}, el("p", {}, `Last check (${check.environment}, ${when(check.created_at)}): `,
        el("strong", { class: check.status === "passed" ? "ok" : (check.status === "running" ? "" : "missing") }, check.status)),
        table(check.steps || [], [["Step", (x) => x.name], ["", (x) => x.ok == null ? "skipped" : mark(x.ok)], ["Detail", (x) => x.detail]], "")) : "",
    ] : [el("p", { class: "muted" }, "Not needed for the native Bybit connector (checked by the project); for a venue plugin, its author runs relay verify-venue.")]));
}

async function watchCheck() {
  // The check runs on the server; its steps are shown when it ends (the form above stays as typed).
  for (let i = 0; i < 120; i++) {
    await new Promise((res) => setTimeout(res, 3000));
    const s = await loadSetup();
    if (!s.venue_check || s.venue_check.status !== "running") { renderTest(s); renderLive(s); return; }
  }
}

// ---- setup: going live ------------------------------------------------------------------------------

function renderLive(s) {
  const gates = el("ul", { class: "checklist" }, ...s.live_gates.map((g) => el("li", {}, mark(g.ok), " ", g.gate.replace("_", " "), ": ", g.how)));
  const ready = s.live_gates.every((g) => g.ok);
  const confirm = el("input", { id: "l-confirm", autocomplete: "off", spellcheck: "false" });
  const out = el("p", { class: "result", role: "status" });
  const live = el("button", { type: "button", class: "danger", disabled: !ready || !s.paper || s.dry_run_file === false, onclick: async () => {
    const r = await send("/v1/setup/live", "POST", { confirm: confirm.value });
    if (r.ok) {
      message(out, true, r.body.env_says_paper ? "The file says live. Now set RELAY_DRY_RUN=false in .env, " + ENV_RESTART + " Until then it stays on paper."
                                               : "The file says live. Restart to trade live.");
      needRestart();
    } else message(out, false, r.body.reason + (r.body.gates ? ": " + r.body.gates.map((g) => g.gate).join(", ") : ""));
  } }, "Go live");
  const paper = el("button", { type: "button", onclick: async () => {
    const r = await send("/v1/setup/paper", "POST");
    message(out, r.ok, r.ok ? "On paper from now on." : "Not changed.");
    refresh();
  } }, "Back to paper now");
  const state = s.paper ? (s.dry_run_file === false ? "The file says live, the environment (RELAY_DRY_RUN) still says paper: paper." : "Paper.") : "LIVE.";
  fill("setup-live", el("p", {}, el("strong", {}, state)),
    el("p", {}, "Going live takes two keys: this button writes dry_run: false into the deployment file once every gate below is open, and you set RELAY_DRY_RUN=false in .env, " + ENV_RESTART),
    gates, el("label", { for: "l-confirm" }, `Type the deployment's name (${s.deployment}) to confirm`), confirm,
    el("div", { class: "row" }, live, paper), out);
}

async function loadSetupTab() {
  const s = await loadSetup();
  const note = document.getElementById("setup-note");
  note.hidden = s.config_writable && !s.deployment_problem;
  if (s.deployment_problem) note.replaceChildren(el("p", { class: "error" }, "The deployment file cannot be used: " + s.deployment_problem));
  else if (!s.config_writable) note.replaceChildren(el("p", {}, "The relay cannot write its config folder (", el("code", {}, s.config_dir),
    "), so this page can only show. With Docker on Linux, run once: ", el("code", {}, "sudo chown -R 10001:10001 config"),
    " in the relay's folder; and keep ", el("code", {}, "./config:/config"), " without :ro in compose.yaml."));
  await renderVenue(s);
  await renderLimits(s);
  renderSources(s);
  renderSinks(s);
  renderTest(s);
  renderLive(s);
}

// ---- connect ---------------------------------------------------------------------------------------

function signalExample(s, src) {
  const body = { version: "1", id: "<a new id for every signal>", source: src.name, deployment: s.deployment,
                 sent_at: "<now, ISO-8601 UTC>", kind: "event", action: "open", symbol: "BTCUSDT", side: "long",
                 size: { notional_usd: "<USDT>" }, stop: { pct: "<PERCENT>" } };
  return body;
}

async function loadConnect() {
  const s = await loadSetup();
  if (!s.sources.length) { fill("connect", el("p", { class: "empty" }, "Add a signal source first (Setup, step 3).")); return; }
  const base = window.location.origin;
  const blocks = s.sources.map((src) => {
    const url = `${base}/v1/signals`;
    const example = signalExample(s, src);
    const parts = [el("h3", {}, src.name), el("p", {}, "Send to: "), copyBlock(url)];
    if (src.auth === "passphrase") {
      const tv = { ...example, id: "{{ticker}}-{{timenow}}-open", sent_at: "{{timenow}}", symbol: "{{ticker}}", passphrase: `<the value of ${src.secret_env}>` };
      parts.push(el("p", {}, "TradingView: in the alert, tick Webhook URL, paste the address above, and put this in the message (one line is fine):"),
        copyBlock(JSON.stringify(tv, null, 2)),
        el("p", { class: "muted" }, "Replace \"<USDT>\" and \"<PERCENT>\" with your numbers, without the quotes, and the passphrase with its value. Every alert needs its own id ending (-open, -close …); in a strategy, {{strategy.order.id}}-{{timenow}} is unique per order."),
        el("p", {}, "n8n, Make, Zapier and other no-code tools: an HTTP request, POST, body type JSON, the same message with your own values (the passphrase in the body)."));
    } else {
      parts.push(el("p", {}, "Every request is signed: X-Relay-Signature: t=<unix time>,v1=<HMAC-SHA256 of \"<t>.<body>\" with the secret>."),
        el("p", {}, "Python:"), copyBlock(PY.replace("SOURCE", src.name).replace("SECRET_ENV", src.secret_env).replace("URL", url).replace("DEPLOYMENT", s.deployment)),
        el("p", {}, "From a shell (curl and openssl):"), copyBlock(SH.replace("SOURCE", src.name).replace("SECRET_ENV", src.secret_env).replace("URL", url).replace("DEPLOYMENT", s.deployment)),
        el("p", { class: "muted" }, "An AI agent or LLM tool: give it the Python function above as its tool. The relay's limits apply to whatever it sends, and the source's list of what it may send keeps it to that."));
    }
    parts.push(el("p", { class: "muted" }, "May send: " + (src.actions.length ? src.actions.join(", ") : "everything") +
      ". The whole contract: /docs on this relay, and docs/SPEC.md."));
    return el("div", { class: "connect" }, ...parts);
  });
  fill("connect", ...blocks);
}

const PY = `import hashlib, hmac, json, os, time, uuid, urllib.request
from datetime import datetime, timezone

def send_signal(action, symbol, side=None, notional_usd=None, stop_pct=None):
    body = {"version": "1", "id": uuid.uuid4().hex, "source": "SOURCE", "deployment": "DEPLOYMENT",
            "sent_at": datetime.now(timezone.utc).isoformat(), "kind": "event", "action": action, "symbol": symbol}
    if side: body["side"] = side
    if notional_usd: body["size"] = {"notional_usd": notional_usd}
    if stop_pct: body["stop"] = {"pct": stop_pct}
    raw = json.dumps(body).encode()
    t = str(int(time.time()))
    mac = hmac.new(os.environ["SECRET_ENV"].encode(), t.encode() + b"." + raw, hashlib.sha256).hexdigest()
    req = urllib.request.Request("URL", data=raw, method="POST", headers={
        "Content-Type": "application/json", "X-Relay-Signature": f"t={t},v1={mac}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)`;

const SH = `BODY='{"version":"1","id":"'$(uuidgen)'","source":"SOURCE","deployment":"DEPLOYMENT","sent_at":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'","kind":"event","action":"close","symbol":"BTCUSDT"}'
T=$(date +%s); SIG=$(printf '%s.%s' "$T" "$BODY" | openssl dgst -sha256 -hmac "$SECRET_ENV" -hex | sed 's/^.* //')
curl -sS -X POST URL -H 'Content-Type: application/json' -H "X-Relay-Signature: t=$T,v1=$SIG" -d "$BODY"`;

// ---- positions, signals, events ---------------------------------------------------------------------

async function loadPositions() {
  const { body } = await api("/v1/positions?status=open");
  fill("positions", table(body, [
    ["Symbol", (p) => p.symbol], ["Side", (p) => p.side], ["Size", (p) => p.size],
    ["Entry", (p) => p.entry_price], ["Stop", (p) => p.stop_price], ["Mode", (p) => (p.dry_run ? "paper" : "live")],
    ["Status", (p) => p.status], ["Opened", (p) => when(p.opened_at)],
    ["", (p) => p.status === "OPEN" ? closeButton(p) : ""],
  ], "No open positions."));
}

function closeButton(p) {
  const b = el("button", { class: "danger small" }, "Close");
  b.addEventListener("click", async () => {
    if (!window.confirm(`Close ${p.symbol} ${p.side} (${p.size}) at market?`)) return;
    b.disabled = true;
    const r = await api(`/v1/positions/${p.id}/close`, { method: "POST" });
    if (!r.ok) window.alert("Not closed: " + JSON.stringify(r.body));
    refresh();
  });
  return b;
}

async function loadSignals() {
  const { body } = await api("/v1/signals?limit=100");
  fill("signals", table(body, [
    ["Received", (s) => when(s.received_at)], ["Source", (s) => s.source], ["Kind", (s) => s.kind],
    ["Symbol", (s) => s.symbol], ["Status", (s) => el("span", { class: "st " + s.status }, s.status)],
    ["Why", (s) => (s.reasons || []).join("; ")],
  ], "No signals yet."));
}

async function loadEvents() {
  const { body } = await api("/v1/events?limit=100");
  fill("events", table(body, [
    ["When", (e) => when(e.created_at)], ["Severity", (e) => el("span", { class: "sev " + e.severity }, e.severity)],
    ["Kind", (e) => e.kind], ["Message", (e) => e.message],
  ], "No events yet."));
}

// ---- the frame -------------------------------------------------------------------------------------

function currentTab() {
  return document.querySelector('[role="tab"][aria-selected="true"]').dataset.tab;
}

async function refresh() {
  try {
    await loadStatus();
    const tab = currentTab();
    if (tab === "home") await loadHome();
    if (tab === "positions") await loadPositions();
    if (tab === "signals") await loadSignals();
    if (tab === "events") await loadEvents();
    document.getElementById("updated").textContent = "Updated " + new Date().toLocaleTimeString();
  } catch (e) {
    if (e.message !== "unauthorised") document.getElementById("updated").textContent = "Could not reach the relay.";
  }
}

async function show(tab) {
  for (const b of document.querySelectorAll('[role="tab"]')) b.setAttribute("aria-selected", String(b.dataset.tab === tab));
  for (const p of document.querySelectorAll('[role="tabpanel"]')) p.hidden = p.id !== "tab-" + tab;
  await refresh();
  try {
    // Forms are built when their tab opens, not on every refresh (that would wipe what is typed).
    if (tab === "setup") await loadSetupTab();
    if (tab === "connect") await loadConnect();
  } catch (e) {
    if (e.message !== "unauthorised") document.getElementById("updated").textContent = "Could not load: " + e.message;
  }
}

function signOut(text) {
  const s = store();
  if (s) s.removeItem(KEY);
  clearInterval(timer);
  document.getElementById("console").hidden = true;
  document.getElementById("login").hidden = false;
  document.getElementById("status").replaceChildren();
  document.getElementById("login-error").textContent = text || "";
}

function signIn() {
  document.getElementById("login").hidden = true;
  document.getElementById("console").hidden = false;
  show(currentTab());
  timer = setInterval(refresh, REFRESH_MS);
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("login-form").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const s = store();
    if (s) s.setItem(KEY, document.getElementById("key").value.trim());
    document.getElementById("key").value = "";
    signIn();
  });
  document.getElementById("logout").addEventListener("click", () => signOut(""));
  for (const b of document.querySelectorAll('[role="tab"]')) b.addEventListener("click", () => show(b.dataset.tab));
  document.getElementById("restart").addEventListener("click", async () => {
    const r = await send("/v1/setup/restart", "POST");
    document.getElementById("restart-banner").hidden = true;
    document.getElementById("updated").textContent = r.ok && r.body.in_container
      ? "Restarting: the console reconnects in a few seconds."
      : "The relay stopped: start it again (outside Docker it does not restart by itself).";
  });
  document.getElementById("stop-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const action = ev.submitter ? ev.submitter.value : "close-all";
    const out = document.getElementById("stop-result");
    if (!deployment) {                     // the status never loaded: there is nothing to type yet
      out.textContent = "The deployment's name is not known yet: reload, and check the API key and the relay.";
      return;
    }
    if (document.getElementById("confirm").value !== deployment) {
      out.textContent = `Type ${deployment} exactly to confirm.`;
      return;
    }
    out.textContent = "Working…";
    const r = await api("/v1/" + action, { method: "POST" });
    const failed = action === "resume" ? "Not resumed: " : "NOT everything was closed - check the venue now: ";
    out.textContent = (r.ok ? "Done: " : failed) + JSON.stringify(r.body);
    document.getElementById("confirm").value = "";
    refresh();
  });
  if (key()) signIn();
});
