"use strict";

const $ = (id) => document.getElementById(id);
const MAX_ROWS = 5000;

let connected = false;
let frames = [];      // every decoded frame, in arrival order
let groups = [];      // table rows: frames folded by event identity
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

let state = "not connected";

// Controls that send something to the dongle are pointless without one. Letting
// them be pressed anyway only moved the failure into the log, where "[not
// connected]" is easy to miss and reads like the dongle refused the command.
function setLinkControls(enabled) {
  for (const el of document.querySelectorAll(".needs-link")) {
    el.disabled = !enabled;
    el.title = enabled ? "" : "connect to the dongle first";
  }
}

const listening = () => state === "listening";

function setState(text, cls) {
  state = text;
  $("state-text").textContent = text;
  $("state").className = "pill " + cls;
  // One button for one thing: whether the radio is receiving. Two buttons meant
  // one of them was always the wrong one to press.
  $("run").textContent = listening() ? "Stop" : "Start";
  $("run").classList.toggle("primary", !listening());
}

// Everything that is neither idle nor live is amber, except the states that
// mean the dongle is not going to answer - those have to read as a problem.
function stateClass(text, isConnected) {
  if (!isConnected) return "idle";
  if (text === "listening") return "live";
  if (/no greeting|failed|mismatch|error/.test(text)) return "bad";
  return "warn";
}

// --- setup strip -----------------------------------------------------------

const WIRING = ["ce", "csn", "irq", "led_rx", "led_tx"];
const RADIO = ["ch", "rate", "crc", "aw", "pa", "plsize"];
const PIPES = [0, 1, 2, 3, 4, 5];

// Pipes 0 and 1 carry a full address, which the radio requires to be exactly as
// long as the configured address width. Pipes 2-5 own a single byte.
const pipeBytes = (n) => (n >= 2 ? 1 : Number($("aw").value));
const byteCount = (address) => (address.match(/[0-9a-f]/gi) || []).length / 2;

// Each enabled pipe as [number, configured value, address it listens on]. The
// two differ for pipes 2-5: the firmware takes their one byte, the radio joins
// it to the rest of pipe 1's address. Showing the joined form keeps that from
// being a surprise; sending the short form keeps the two ends honest.
function pipeAddresses() {
  const value = (n) => $("pipe" + n).value.trim();
  const shared = value(1).slice(2);           // "42:54:48:4D:45" -> ":54:48:4D:45"
  const list = [];
  const errors = [];
  const bad = new Set();
  for (const n of PIPES) {
    const own = value(n);
    if (!own) continue;
    const need = pipeBytes(n);
    if (byteCount(own) !== need) {
      errors.push(`pipe ${n} needs ${need} byte${need === 1 ? "" : "s"}, not ${byteCount(own)}` +
                  (n < 2 ? ` - that is what aw=${$("aw").value} means` : ""));
      bad.add(n);
    } else if (n >= 2 && !shared) {
      errors.push(`pipe ${n} needs pipe 1 - the rest of its address comes from there`);
      bad.add(n);
    } else {
      list.push([n, own, n >= 2 ? own + shared : own]);
    }
  }
  return { list, errors, bad };
}

function updateSummary() {
  const { list, bad } = pipeAddresses();
  for (const n of PIPES) $("pipe" + n).classList.toggle("invalid", bad.has(n));
  const pipes = list.map(([n, , listensOn]) => `p${n}=${listensOn}`).join(" ");
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
  for (const [n, configured] of pipeAddresses().list) parts.push(`pipe${n}=${configured}`);
  return parts.join(" ");
}

// Addresses are typed as bare hex and grouped as you go: 4254 becomes 42:54.
// Anything that is not a hex digit is dropped, including separators the user
// types themselves, so a pasted 42-54-48-4D-45 lands in the same shape.
function formatAddress(el, maxBytes) {
  const typedBefore = el.value.slice(0, el.selectionStart ?? el.value.length);
  const digitsBefore = (typedBefore.match(/[0-9a-f]/gi) || []).length;

  const digits = (el.value.match(/[0-9a-f]/gi) || []).join("").toUpperCase().slice(0, maxBytes * 2);
  el.value = (digits.match(/.{1,2}/g) || []).join(":");

  // Every completed pair ahead of the caret pushed it one colon to the right.
  const caret = digitsBefore + Math.max(0, Math.floor((digitsBefore - 1) / 2));
  el.setSelectionRange(caret, caret);
}

const send = (line) => post("/api/command", { line });

async function sendSequence(lines, gap = 150) {
  for (const line of lines) {
    await send(line);
    await new Promise((resolve) => setTimeout(resolve, gap));
  }
}

// --- frame table -----------------------------------------------------------

// Fixed because they describe the reception, not the protocol: when it arrived,
// how long since the last event, which pipe matched, how many bytes.
const RADIO_COLUMNS = [
  { key: "time", label: "Time", cls: "c-time" },
  { key: "delta", label: "Δ ms", cls: "c-delta" },
  { key: "pipe", label: "Pipe", cls: "c-pipe" },
  { key: "len", label: "Len", cls: "c-len" },
  // How many times the sender repeated this event, and over what span. Reception
  // metadata like the rest of this list - appended to the decoder's last column
  // it read as though the protocol had said it.
  { key: "repeats", label: "Repeats", cls: "c-rep",
    title: "Frames folded into this row, and the span from the first to the last" },
];
let decoderColumns = [];

function setColumns(columns) {
  decoderColumns = columns || [];
  const head = $("head");
  head.replaceChildren();
  for (const { label, cls, title } of RADIO_COLUMNS) {
    const th = document.createElement("th");
    th.className = cls;
    th.textContent = label;
    if (title) th.title = title;
    head.appendChild(th);
  }
  for (const column of decoderColumns) {
    const th = document.createElement("th");
    th.textContent = column.label;
    if (column.width) th.style.width = `${column.width}px`;
    head.appendChild(th);
  }
}

// A row is one event, not one frame. A sender repeating an event three times
// produced three near-identical rows that pushed everything else off screen and
// made the interesting number - how far apart the repeats were - something you
// had to reconstruct by subtracting timestamps.
const GROUP_WINDOW_MS = 2000;

function groupable(frame) {
  if (!$("group").checked || !groups.length) return null;
  const last = groups[groups.length - 1];
  if (last.identity !== frame.identity) return null;
  // A window as well as an identity: a sender stuck on one packet id must not
  // collapse minutes of traffic into a row that hides when any of it happened.
  const first = last.frames[0];
  if (frame.deviceMs !== null && first.deviceMs !== null) {
    return frame.deviceMs - first.deviceMs <= GROUP_WINDOW_MS ? last : null;
  }
  return last;
}

function spread(group) {
  const first = group.frames[0], last = group.frames[group.frames.length - 1];
  if (first.deviceMs === null || last.deviceMs === null) return null;
  return last.deviceMs - first.deviceMs;
}

function paintRow(group) {
  const [head] = group.frames;
  const count = group.frames.length;
  const ms = spread(group);
  const cells = [
    [head.time, "c-time"],
    [group.gap === null ? "" : group.gap.toFixed(1), "c-delta"],
    [head.pipe, "c-pipe"],
    [head.len, "c-len"],
    [count > 1 ? `×${count}` + (ms !== null ? `  ${ms} ms` : "") : "", "c-rep"],
  ];
  for (const column of decoderColumns) cells.push([head.cells[column.key] ?? "", ""]);

  const tds = group.tr.children;
  while (tds.length > cells.length) group.tr.removeChild(group.tr.lastChild);
  cells.forEach(([text, cls], i) => {
    const td = tds[i] || group.tr.appendChild(document.createElement("td"));
    td.className = cls;
    td.textContent = text;
  });
  group.tr.classList.toggle("flagged", head.flagged);
  group.tr.classList.toggle("repeated", count > 1);
}

function addRow(frame) {
  frames.push(frame);

  const existing = groupable(frame);
  if (existing) {
    existing.frames.push(frame);
    paintRow(existing);
  } else {
    const tbody = $("rows");
    const previous = groups[groups.length - 1];
    // The gap between events, measured head to head. Between two rows the
    // server's frame-to-frame delta would be the gap to the previous event's
    // last repeat, which is not what the column claims to show.
    let gap = frame.delta;
    if (previous && frame.deviceMs !== null && previous.frames[0].deviceMs !== null) {
      gap = frame.deviceMs - previous.frames[0].deviceMs;
    }
    const group = { identity: frame.identity, frames: [frame], gap,
                    tr: document.createElement("tr") };
    const index = groups.length;
    groups.push(group);
    group.tr.addEventListener("click", () => select(index));
    paintRow(group);
    tbody.appendChild(group.tr);
    while (tbody.children.length > MAX_ROWS) tbody.removeChild(tbody.firstChild);
  }

  $("empty").hidden = true;
  updateCount();
  if ($("follow").checked) {
    const wrap = document.querySelector(".table-wrap");
    wrap.scrollTop = wrap.scrollHeight;
  }
}

function updateCount() {
  const total = frames.length;
  const text = `${total} frame${total === 1 ? "" : "s"}`;
  $("count").textContent =
    groups.length && groups.length !== total ? `${groups.length} events · ${text}` : text;
}

function select(index) {
  const group = groups[index];
  if (!group) return;
  selected = index;
  for (const tr of $("rows").children) tr.classList.remove("sel");
  const offset = groups.length - $("rows").children.length;
  const row = $("rows").children[index - offset];
  if (row) row.classList.add("sel");

  const [head] = group.frames;
  const lines = [...head.detail];
  if (group.frames.length > 1) {
    const gaps = group.frames.slice(1).map((f, i) =>
      f.deviceMs !== null && group.frames[i].deviceMs !== null
        ? `${f.deviceMs - group.frames[i].deviceMs} ms` : "?");
    lines.push("", `  repeats   : ${group.frames.length} frames, ${gaps.join(" + ")} apart`);
  }
  $("detail").textContent = lines.join("\n");
  $("raw").textContent = head.hex.join("\n");
  showTab("detail");
}

function rebuild(list) {
  $("rows").replaceChildren();
  frames = [];
  groups = [];
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
  $("panel-scan").hidden = name !== "scan";
  $("panel-log").hidden = name !== "log";
}

// --- channel scan ----------------------------------------------------------

const CHANNELS = 126;   // 0..125 = 2400..2525 MHz

function renderScan(event) {
  const note = $("scan-note");
  if (event.state === "running") {
    note.textContent = "Scanning all 126 channels…  (reception is paused meanwhile)";
    $("scan-chart").replaceChildren();
    showTab("scan");
    return;
  }

  const hits = event.hits || {};
  const busiest = Math.max(1, ...Object.values(hits).map(Number));
  const listening = Number($("ch").value);

  const chart = $("scan-chart");
  chart.replaceChildren();
  for (let ch = 0; ch < CHANNELS; ch++) {
    const count = Number(hits[ch] || 0);
    const bar = document.createElement("div");
    bar.className = "scan-bar" + (ch === listening ? " here" : "");
    bar.style.setProperty("--h", `${Math.round((count / busiest) * 100)}%`);
    bar.title = `ch ${ch} — ${2400 + ch} MHz — ${count}/${event.passes} passes`
                + (ch === listening ? " (listening here)" : "");
    chart.appendChild(bar);
  }

  const found = Object.keys(hits).length;
  note.textContent = found
    ? `${found} of 126 channels showed activity in ${event.passes} passes`
      + `  ·  busiest ${busiest}/${event.passes}`
    // Not the same as "nothing is transmitting": the detector only reports
    // carriers above roughly -64 dBm, and a scan is over in about a second.
    : `No channel exceeded the detector threshold in ${event.passes} passes.`
      + " The scan takes about a second — a sender that transmits every"
      + " minute is very unlikely to be caught by it.";
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
      const text = event.fields.state || "connected";
      setState(text, stateClass(text, true));
    }
  } else if (event.type === "status") {
    connected = event.connected;
    $("connect").textContent = connected ? "Disconnect" : "Connect";
    setLinkControls(connected);
    const text = event.state || (connected ? "connected" : "not connected");
    setState(text, stateClass(text, connected));
  } else if (event.type === "scan") {
    renderScan(event);
  } else if (event.type === "parser") {
    // Another tab switched decoder. Matching selects first stops this from
    // bouncing: the request below publishes the same event straight back.
    if ($("decoder").value !== event.name) {
      $("decoder").value = event.name;
      setColumns(event.columns);
      post("/api/parser", { name: event.name }).then((d) => d.frames && rebuild(d.frames));
    }
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
    // Whatever the server is actually decoding with - not a guess of our own.
    if (p.active) {
      option.selected = true;
      setColumns(p.columns);
    }
    select.appendChild(option);
  }
}

function init() {
  loadPorts();
  loadParsers();
  updateSummary();
  setLinkControls(false);   // until the first status event says otherwise

  // Formatting first, so the summary below reads the grouped value and not the
  // one keystroke it was a moment ago.
  for (const n of PIPES) {
    $("pipe" + n).addEventListener("input", (e) => formatAddress(e.target, pipeBytes(n)));
  }
  // Narrowing aw has to shorten what is already typed, or the fields would keep
  // claiming an address the radio can no longer use.
  $("aw").addEventListener("change", () => {
    for (const n of [0, 1]) {
      const el = $("pipe" + n);
      el.maxLength = pipeBytes(n) * 3 - 1;
      formatAddress(el, pipeBytes(n));
    }
    updateSummary();
  });
  for (const id of [...WIRING, ...RADIO, ...PIPES.map((n) => "pipe" + n)]) {
    $(id).addEventListener("input", updateSummary);
    $(id).addEventListener("change", updateSummary);
  }

  // No rescan button: the list refreshes as it is opened, which covers the only
  // case one was needed for - a dongle plugged in after the page was loaded.
  $("port").addEventListener("pointerdown", loadPorts);
  $("port").addEventListener("focus", loadPorts);

  $("connect").addEventListener("click", async () => {
    if (connected) return void post("/api/disconnect");
    const port = $("port").value;
    // Opening the port blocks for a moment and the greeting takes ~2s more, so
    // say what is happening instead of letting the pill sit on "not connected".
    setState("connecting…", "warn");
    const data = await post("/api/connect", { port });
    if (data.ok === false) setState("not connected", "idle");
    else localStorage.setItem(LAST_PORT, port);
  });

  $("setup-open").addEventListener("click", () => $("setup").showModal());
  // Esc comes free with showModal(); clicking the backdrop does not. The form
  // fills the dialog box, so a click landing on the dialog itself is outside.
  $("setup").addEventListener("click", (e) => {
    if (e.target === $("setup")) $("setup").close();
  });

  // Starting means wiring and radio config, always together and in that order.
  // The firmware refuses hwset while it is listening, so restarting a capture
  // has to stop first - without that the wiring silently kept what it had.
  $("run").addEventListener("click", () => {
    if (listening()) return void send("stop");
    const { errors } = pipeAddresses();
    if (errors.length) {
      for (const message of errors) log(`[${message}]`, "err");
      return;
    }
    sendSequence([buildHwset(), buildListen()]);
  });
  for (const btn of document.querySelectorAll("[data-cmd]")) {
    btn.addEventListener("click", () => send(btn.dataset.cmd));
  }
  $("repeats").addEventListener("change", () => send(`repeats ${$("repeats").checked ? 1 : 0}`));

  $("decoder").addEventListener("change", async () => {
    const data = await post("/api/parser", { name: $("decoder").value });
    // Header before rows: the rows are laid out against the columns.
    if (data.columns) setColumns(data.columns);
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

  // Regrouping is a pure view change, so it works on what is already here.
  $("group").addEventListener("change", () => rebuild(frames.slice()));

  document.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      if (document.activeElement.tagName === "INPUT") return;
      e.preventDefault();
      select(Math.min(groups.length - 1, Math.max(0, selected + (e.key === "ArrowDown" ? 1 : -1))));
    }
  });

  const stream = new EventSource("/api/events");
  stream.onmessage = (e) => handle(JSON.parse(e.data));
  stream.onerror = () => setState("server lost", "bad");
}

init();
