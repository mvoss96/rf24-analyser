"use strict";

const $ = (id) => document.getElementById(id);
const MAX_ROWS = 5000;

let connected = false;
let frames = [];      // decoded frame objects, parallel to the table rows
let selected = -1;

// --- helpers ---------------------------------------------------------------

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) log(`[${data.error || res.statusText}]`, "err");
  return data;
}

function log(text, kind) {
  const pre = $("log");
  const span = document.createElement("span");
  if (kind) span.className = kind;
  span.textContent = text + "\n";
  pre.appendChild(span);
  pre.scrollTop = pre.scrollHeight;
}

function setState(text, cls) {
  $("state-text").textContent = text;
  $("state").className = "pill " + cls;
}

// --- setup strip -----------------------------------------------------------

const WIRING = ["ce", "csn", "irq", "led_rx", "led_tx"];
const RADIO = ["ch", "rate", "crc", "aw", "pa", "plsize"];
const PIPES = [0, 1, 2, 3, 4, 5];

function updateSummary() {
  const pipes = PIPES
    .map((n) => [n, $("pipe" + n).value.trim()])
    .filter(([, a]) => a)
    .map(([n, a]) => `p${n}=${a}`)
    .join(" ");
  $("summary").textContent =
    `ce=${$("ce").value} csn=${$("csn").value}  |  ch${$("ch").value} ` +
    `${$("rate").value}k crc${$("crc").value} aw${$("aw").value} pa=${$("pa").value}` +
    `  |  ${pipes}`;
}

function buildHwset() {
  const v = (id) => $(id).value.trim() || "none";
  return `hwset ce=${v("ce")} csn=${v("csn")} irq=${v("irq")} ` +
         `led_rx=${v("led_rx")} led_tx=${v("led_tx")}`;
}

function buildListen() {
  const parts = [
    `listen ch=${$("ch").value}`, `rate=${$("rate").value}`, `crc=${$("crc").value}`,
    `aw=${$("aw").value}`, `pa=${$("pa").value}`,
    `ack=${$("ack").checked ? 1 : 0}`, `dpl=${$("dpl").checked ? 1 : 0}`,
  ];
  if (!$("dpl").checked) parts.push(`plsize=${$("plsize").value}`);
  for (const n of PIPES) {
    const address = $("pipe" + n).value.trim();
    if (address) parts.push(`pipe${n}=${address}`);
  }
  return parts.join(" ");
}

const send = (line) => post("/api/command", { line });

// --- frame table -----------------------------------------------------------

function addRow(frame) {
  const tbody = $("rows");
  const tr = document.createElement("tr");
  if (frame.flagged) tr.className = "flagged";
  const cells = [
    [frame.time, "c-time"],
    [frame.delta === null ? "" : frame.delta.toFixed(1), "c-delta"],
    [frame.pipe, "c-pipe"],
    [frame.len, "c-len"],
    [frame.summary, ""],
  ];
  for (const [text, cls] of cells) {
    const td = document.createElement("td");
    if (cls) td.className = cls;
    td.textContent = text;
    tr.appendChild(td);
  }
  const index = frames.length;
  frames.push(frame);
  tr.addEventListener("click", () => select(index));
  tbody.appendChild(tr);

  while (tbody.children.length > MAX_ROWS) tbody.removeChild(tbody.firstChild);
  $("empty").hidden = true;
  $("count").textContent = `${frames.length} frame${frames.length === 1 ? "" : "s"}`;
  if ($("follow").checked) {
    const wrap = document.querySelector(".table-wrap");
    wrap.scrollTop = wrap.scrollHeight;
  }
}

function select(index) {
  const frame = frames[index];
  if (!frame) return;
  selected = index;
  for (const tr of $("rows").children) tr.classList.remove("sel");
  const offset = frames.length - $("rows").children.length;
  const row = $("rows").children[index - offset];
  if (row) row.classList.add("sel");
  $("detail").textContent = frame.detail.join("\n");
  $("raw").textContent = frame.hex.join("\n");
  showTab("detail");
}

function rebuild(list) {
  $("rows").replaceChildren();
  frames = [];
  for (const frame of list) addRow(frame);
  if (!list.length) {
    $("empty").hidden = false;
    $("count").textContent = "0 frames";
  }
  $("detail").textContent = "";
  $("raw").textContent = "";
}

function showTab(name) {
  for (const tab of document.querySelectorAll(".tab")) {
    tab.classList.toggle("active", tab.dataset.tab === name);
  }
  $("panel-detail").hidden = name !== "detail";
  $("panel-log").hidden = name !== "log";
}

// --- event stream ----------------------------------------------------------

function handle(event) {
  if (event.type === "frame") {
    addRow(event);
  } else if (event.type === "greeting") {
    log(event.text, "ok");
    for (const key of WIRING) {
      if (event.fields[key] !== undefined) $(key).value = event.fields[key];
    }
    updateSummary();
    if (!event.apiOk) {
      setState(`api ${event.fields.api} mismatch`, "bad");
      log(`[warning] firmware api=${event.fields.api}, this ui expects api=${event.expectedApi}`, "warn");
    } else if (event.fields.hw === "failed") {
      setState("wiring failed", "bad");
    } else {
      setState(event.fields.state || "connected", "warn");
    }
  } else if (event.type === "status") {
    connected = event.connected;
    $("connect").textContent = connected ? "Disconnect" : "Connect";
    const text = event.state || (connected ? "connected" : "not connected");
    setState(text, !connected ? "idle" : text === "listening" ? "live" : "warn");
  } else if (event.type === "line") {
    const kind = { error: "err", warn: "warn", ok: "ok", sent: "sent" }[event.kind] || "";
    log(event.text, kind);
    if (event.text.startsWith("OK listening")) setState("listening", "live");
    else if (event.text.startsWith("OK stopped")) setState("idle", "warn");
    else if (event.text.startsWith("ERR")) setState("error", "bad");
  }
}

// --- wiring up -------------------------------------------------------------

const LAST_PORT = "nrf24.lastPort";

async function loadPorts() {
  const ports = await (await fetch("/api/ports")).json();
  const select = $("port");
  const previous = select.value || localStorage.getItem(LAST_PORT);
  select.replaceChildren();
  for (const p of ports) {
    const option = document.createElement("option");
    option.value = p.device;
    option.textContent = p.description ? `${p.device} — ${p.description}` : p.device;
    select.appendChild(option);
  }
  // Only ever preselect a port that actually worked before. Guessing from the
  // description picks the wrong adapter as readily as the right one, and a
  // wrong guess here reads as "the dongle is broken" rather than "wrong port".
  if (previous && ports.some((p) => p.device === previous)) select.value = previous;
}

async function loadParsers() {
  const list = await (await fetch("/api/parsers")).json();
  const select = $("decoder");
  select.replaceChildren();
  for (const p of list) {
    const option = document.createElement("option");
    option.value = p.name;
    option.textContent = p.unavailable ? `${p.label} (unavailable)` : p.label;
    option.disabled = Boolean(p.unavailable);
    option.title = p.description || "";
    if (p.name === "bthome") option.selected = true;
    select.appendChild(option);
  }
}

function init() {
  loadPorts();
  loadParsers();
  updateSummary();

  for (const id of [...WIRING, ...RADIO, ...PIPES.map((n) => "pipe" + n)]) {
    $(id).addEventListener("input", updateSummary);
    $(id).addEventListener("change", updateSummary);
  }

  $("refresh").addEventListener("click", loadPorts);
  $("connect").addEventListener("click", async () => {
    if (connected) return void post("/api/disconnect");
    const port = $("port").value;
    const data = await post("/api/connect", { port });
    if (data.ok !== false) localStorage.setItem(LAST_PORT, port);
  });

  $("setup-toggle").addEventListener("click", () => {
    const body = $("setup-body");
    body.hidden = !body.hidden;
    $("setup-caret").textContent = body.hidden ? "▸" : "▾";
    $("setup-toggle").setAttribute("aria-expanded", String(!body.hidden));
  });
  $("pipes-toggle").addEventListener("click", () => {
    const more = $("pipes-more");
    more.hidden = !more.hidden;
    $("pipes-toggle").textContent = more.hidden ? "more" : "less";
  });

  // Start is the one primary action: the wiring and the radio config always go
  // together, and always in that order.
  $("start").addEventListener("click", async () => {
    await send(buildHwset());
    setTimeout(() => send(buildListen()), 150);
  });
  for (const btn of document.querySelectorAll("[data-cmd]")) {
    btn.addEventListener("click", () => send(btn.dataset.cmd));
  }
  $("repeats").addEventListener("change", () => send(`repeats ${$("repeats").checked ? 1 : 0}`));

  $("decoder").addEventListener("change", async () => {
    const data = await post("/api/parser", { name: $("decoder").value });
    if (data.frames) rebuild(data.frames);
  });

  for (const tab of document.querySelectorAll(".tab")) {
    tab.addEventListener("click", () => showTab(tab.dataset.tab));
  }

  $("cmdform").addEventListener("submit", (e) => {
    e.preventDefault();
    const line = $("cmd").value.trim();
    if (line) { send(line); $("cmd").value = ""; }
  });

  $("clear").addEventListener("click", async () => {
    await post("/api/clear");
    rebuild([]);
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (document.activeElement.tagName === "INPUT") return;
      e.preventDefault();
      select(Math.min(frames.length - 1, Math.max(0, selected + (e.key === "ArrowDown" ? 1 : -1))));
    }
  });

  const stream = new EventSource("/api/events");
  stream.onmessage = (e) => handle(JSON.parse(e.data));
  stream.onerror = () => setState("server lost", "bad");
}

init();
