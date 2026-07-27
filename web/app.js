"use strict";

const $ = (id) => document.getElementById(id);
const MAX_ROWS = 5000;

let connected = false;
let frames = [];      // every decoded frame, in arrival order
let groups = [];      // table rows: frames folded by event identity
let selected = -1;

// What the dongle last said about itself, from the server's `info` snapshot -
// or null while nothing has answered yet. Every status display in this file
// reads from here and from nowhere else. The setup fields are an editor of this
// value, never a description of it: assembling the toolbar line out of them is
// what let a freshly loaded page claim ch100 while the dongle sat on ch90.
let deviceRadio = null;

// --- helpers ---------------------------------------------------------------

async function post(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  // The firmware's own words when there are any: a command that waited for its
  // reply carries the ERR line, which says what was wrong with it.
  if (!res.ok || data.ok === false) log(`[${data.error || data.reply || res.statusText}]`, "err");
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
const scanning = () => state === "scanning";

function setState(text, cls) {
  state = text;
  $("state-text").textContent = text;
  $("state").className = "pill " + cls;
  // One button for one thing: whether the radio is receiving. Two buttons meant
  // one of them was always the wrong one to press. The scan button works the
  // same way, and the radio can only do one of the two at a time.
  $("run").textContent = listening() ? "Stop" : "Start";
  $("run").classList.toggle("primary", !listening());
  $("scan-run").textContent = scanning() ? "Stop scan" : "Scan";
  $("scan-run").classList.toggle("primary", scanning());
}

// Everything that is neither idle nor live is amber, except the states that
// mean the dongle is not going to answer - those have to read as a problem.
function stateClass(text, isConnected) {
  if (!isConnected) return "idle";
  if (text === "listening" || text === "scanning") return "live";
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

// Marks addresses the radio could not accept. A property of what is typed, so
// unlike the summary below it does belong to the fields.
function markPipes() {
  const { bad } = pipeAddresses();
  for (const n of PIPES) $("pipe" + n).classList.toggle("invalid", bad.has(n));
}

// The toolbar line, written from the dongle's answer and nothing else. Where it
// cannot know something it says so: a line that quietly keeps showing the last
// known value is indistinguishable from one that is right.
function renderSummary() {
  const el = $("summary");
  const r = deviceRadio;
  el.classList.toggle("unknown", !r || !r.configured);
  if (!connected) { el.textContent = "no dongle connected"; return; }
  if (!r) { el.textContent = "asking the dongle…"; return; }
  if (!r.hwReady) { el.textContent = "no wiring — Setup…, then Apply"; return; }

  const wiring = `ce=${r.wiring.ce} csn=${r.wiring.csn}`;
  if (!r.configured) {
    el.textContent = `${wiring}  |  radio not configured — Setup…, then Apply`;
    return;
  }
  // Pipes 2-5 are reported as the full address they listen on, which is what
  // the old line showed too - the radio joins their one byte to pipe 1's rest.
  const pipes = Object.keys(r.pipes).map(Number).sort((a, b) => a - b)
    .map((n) => `p${n}=${r.pipes[n]}`).join(" ");
  el.textContent = `${wiring}  |  ch${r.channel} ${r.rate}k crc${r.crc} ` +
                   `aw${r.aw} pa=${r.pa}  |  ${pipes}`;
  // Measured or merely intended - the difference is worth having, but not worth
  // a badge in the toolbar of a tool whose radio is normally listening.
  el.title = r.src === "chip"
    ? "read back from the chip's registers"
    : "as the firmware holds it — the registers describe the configuration only "
      + "while the radio is listening";
}

// The setup fields hold the dongle's configuration, so that opening the dialog
// shows what the radio is doing rather than what index.html was written with.
// Not while it is open: the heartbeat would pull the field out from under the
// cursor. Closing without applying re-seeds, so the wish never outlives the
// dialog it was typed into.
function seedSetup() {
  const r = deviceRadio;
  if (!r || $("setup").open) return;
  for (const key of WIRING) {
    if (r.wiring[key] !== undefined) $(key).value = r.wiring[key];
  }
  if (r.repeats !== undefined) $("repeats").checked = r.repeats === 1;
  if (!r.configured) return;

  $("ch").value = r.channel;
  $("rate").value = r.rate;
  $("crc").value = r.crc;
  $("aw").value = r.aw;
  $("pa").value = r.pa;
  $("plsize").value = r.plsize;
  $("ack").checked = r.ack === 1;
  $("dpl").checked = r.dpl === 1;
  for (const n of PIPES) {
    const address = r.pipes[n];
    // A pipe 2-5 field holds that pipe's own byte; the dongle reports the whole
    // address it listens on, whose first byte that is.
    $("pipe" + n).value = address === undefined ? ""
                        : (n >= 2 ? address.slice(0, 2) : address);
  }
  for (const n of [0, 1]) $("pipe" + n).maxLength = pipeBytes(n) * 3 - 1;
  markPipes();
}

// Writes the fields to the dongle. hwset is refused while the radio listens and
// it clears the configuration, so the three go together and in this order; the
// reply of each is waited for, because a listen sent after a failed hwset would
// configure nothing and report success.
async function applySetup() {
  const { errors } = pipeAddresses();
  if (errors.length) {
    for (const message of errors) log(`[${message}]`, "err");
    showTab("terminal");
    return false;
  }
  const running = deviceRadio && (deviceRadio.state === "listening" ||
                                  deviceRadio.state === "scanning");
  const ok = await sendAll([...(running ? ["stop"] : []),
                            buildHwset(), buildListen()]);
  // Only on success: a dialog that closes on an ERR takes the typed values with
  // it and leaves the user to retype them from memory.
  if (ok) $("setup").close();
  else showTab("terminal");
  return ok;
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

// Each line waits for the firmware's own OK/ERR before the next one goes out,
// and an ERR ends the sequence. The fixed pause it replaces was a guess at how
// long a command takes, and a guess is wrong in both directions.
async function sendAll(lines) {
  for (const line of lines) {
    const data = await post("/api/command", { line, wait: true });
    if (data.ok === false) return false;
  }
  return true;
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
  // it read as though the protocol had said it. Ungrouped, every row stands for
  // one frame, so the column has nothing left to say and goes away.
  { key: "repeats", label: "Repeats", cls: "c-rep", grouped: true,
    title: "Frames folded into this row, and the span from the first to the last" },
];
let decoderColumns = [];

const radioColumns = () =>
  RADIO_COLUMNS.filter((column) => !column.grouped || $("group").checked);

function setColumns(columns) {
  if (columns) decoderColumns = columns;
  const head = $("head");
  head.replaceChildren();
  for (const { label, cls, title } of radioColumns()) {
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

// How far back to look when the frames carry no device timestamp and the
// window cannot be applied. Two events' repeats interleaving is the case this
// exists for, so a handful of rows is plenty.
const GROUP_SCAN_DEPTH = 8;

function groupable(frame) {
  if (!$("group").checked || !groups.length) return null;
  // Not just the last row: two events' repeats arrive interleaved (a stale
  // frame carrying the previous packet id lands in the middle of the current
  // burst), so the group this frame belongs to is often one row further back.
  // Matching only against the last row split one event across three rows and
  // made a single click look like five.
  for (let i = groups.length - 1; i >= 0; i--) {
    const group = groups[i];
    const first = group.frames[0];
    // A window as well as an identity: a sender stuck on one packet id must not
    // collapse minutes of traffic into a row that hides when any of it happened.
    // Groups are in arrival order, so once one is out of the window the older
    // ones are too.
    if (frame.deviceMs !== null && first.deviceMs !== null) {
      if (frame.deviceMs - first.deviceMs > GROUP_WINDOW_MS) break;
    } else if (groups.length - i > GROUP_SCAN_DEPTH) {
      break;
    }
    if (group.identity === frame.identity) return group;
  }
  return null;
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
  const values = {
    time: head.time,
    delta: group.gap === null ? "" : group.gap.toFixed(1),
    pipe: head.pipe,
    len: head.len,
    repeats: count > 1 ? `×${count}` + (ms !== null ? `  ${ms} ms` : "") : "",
  };
  const cells = radioColumns().map(({ key, cls }) => [values[key], cls, null]);
  for (const column of decoderColumns) {
    // Skipped counter values are marked on the packet number itself: a row of
    // its own said the same thing in far more space, and pushed the frames
    // apart to say it.
    const lost = column.packet && group.missing ? group.missing : null;
    cells.push([head.cells[column.key] ?? "", "", lost]);
  }

  const tds = group.tr.children;
  while (tds.length > cells.length) group.tr.removeChild(group.tr.lastChild);
  cells.forEach(([text, cls, lost], i) => {
    const td = tds[i] || group.tr.appendChild(document.createElement("td"));
    td.className = cls;
    td.textContent = text;
    td.title = "";
    if (lost) {
      const mark = document.createElement("span");
      mark.className = "lost";
      mark.textContent = `−${lost}`;
      td.appendChild(mark);
      td.title = missingNote(head, lost);
    }
  });
  group.tr.classList.toggle("flagged", head.flagged);
  group.tr.classList.toggle("repeated", count > 1);
}

// Packet counters run in sequence per sender, so a jump in one is the only
// direct evidence a sniffer has that something never arrived. The counter is a
// byte, hence the wrap.
const lastPacket = new Map();   // source -> furthest packet id seen
let missingTotal = 0;

// Ids do not arrive in order. A sender repeats each event, and a frame left
// over from an earlier transmission can arrive in the middle of the current
// burst carrying the id before it. Read frame to frame, every such step back
// counts as a jump forward of 255 and reports 254 packets lost - the mistake
// the capture summary was fixed for, still living here until now. Only a step
// FORWARD from the furthest id seen means something never arrived; anything
// behind it is a repeat or a straggler and says nothing about loss.
const BEHIND_TOLERANCE = 64;

function missingBefore(frame) {
  if (frame.packetId === null || frame.packetId === undefined || !frame.source) return 0;
  const furthest = lastPacket.get(frame.source);
  if (furthest === undefined) {
    lastPacket.set(frame.source, frame.packetId);
    return 0;
  }
  const ahead = (frame.packetId - furthest) & 0xFF;
  // Equal means a retransmission of the event we just saw, not a new one.
  if (ahead === 0) return 0;
  if (ahead > 0xFF - BEHIND_TOLERANCE) return 0;   // behind: a repeat or a stale frame
  lastPacket.set(frame.source, frame.packetId);
  return ahead - 1;
}

function missingNote(frame, missing) {
  const from = (frame.packetId - missing) & 0xFF;
  const range = missing === 1 ? `#${from}` : `#${from} to #${(frame.packetId - 1) & 0xFF}`;
  // The counter is a byte, so a gap of n is really n, n+256, n+512... For small
  // gaps that is pedantry; past half a cycle a wrap is plausible enough to say.
  const floor = missing >= 128 ? "at least " : "";
  return `${floor}${missing} packet${missing === 1 ? "" : "s"} never arrived before this `
       + `one — ${range} from ${frame.source}`;
}

function addRow(frame) {
  frames.push(frame);

  const missing = missingBefore(frame);
  if (missing) missingTotal += missing;

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
    const group = { identity: frame.identity, frames: [frame], gap, missing,
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
  const parts = [];
  if (groups.length && groups.length !== total) parts.push(`${groups.length} events`);
  parts.push(`${total} frame${total === 1 ? "" : "s"}`);
  if (missingTotal) parts.push(`${missingTotal} missing`);
  $("count").textContent = parts.join(" · ");
  $("count").classList.toggle("has-loss", missingTotal > 0);
}

function select(index) {
  const group = groups[index];
  if (!group) return;
  selected = index;
  // The row is held on the group rather than found by index: gap rows sit in
  // the same tbody, so the two no longer line up.
  for (const tr of $("rows").children) tr.classList.remove("sel");
  group.tr.classList.add("sel");

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
  lastPacket.clear();
  missingTotal = 0;
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
  $("panel-terminal").hidden = name !== "terminal";
}

// --- channel scan ----------------------------------------------------------

const CHANNELS = 126;   // 0..125 = 2400..2525 MHz

function renderScan(event) {
  const note = $("scan-note");
  if (event.state === "running") {
    // Live reports arrive every few hundred ms; clearing the chart each time
    // would make it flicker rather than update.
    if (!$("scan-chart").children.length) {
      note.textContent = "Scanning all 126 channels…  (reception is paused meanwhile)";
    }
    showTab("scan");
    return;
  }

  const hits = event.hits || {};
  const busiest = Math.max(0, ...Object.values(hits).map(Number));
  // The channel the radio is on, not the one the form asks for: marking a bar
  // "tuned here" that the dongle never tuned to is a chart that lies quietly.
  const tuned = deviceRadio && deviceRadio.configured ? deviceRadio.channel : -1;

  // Scaled against the passes in this report, not against the busiest channel
  // in it: a relative scale would make every live report redraw to full height
  // and the bars would say nothing about how busy anything actually is.
  const chart = $("scan-chart");
  if (chart.children.length !== CHANNELS) {
    chart.replaceChildren();
    for (let ch = 0; ch < CHANNELS; ch++) {
      chart.appendChild(document.createElement("div"));
    }
  }
  for (let ch = 0; ch < CHANNELS; ch++) {
    const count = Number(hits[ch] || 0);
    const bar = chart.children[ch];
    bar.className = "scan-bar" + (ch === tuned ? " here" : "");
    bar.style.setProperty("--h", `${Math.round((count / event.passes) * 100)}%`);
    bar.title = `ch ${ch} — ${2400 + ch} MHz — ${count}/${event.passes} passes`
                + (ch === tuned ? " (tuned here)" : "");
  }

  const found = Object.keys(hits).length;
  const live = scanning() ? " · live" : "";
  note.textContent = found
    ? `${found} of 126 channels showed activity in ${event.passes} passes`
      + `  ·  busiest ${busiest}/${event.passes}${live}`
    // Not the same as "nothing is transmitting": the detector only reports
    // carriers above roughly -64 dBm, and one report covers a fraction of a
    // second, so an occasional sender is very unlikely to be caught in it.
    : `No channel exceeded the detector threshold in ${event.passes} passes`
      + `${live}. It only sees carriers above about −64 dBm, and a sender that`
      + " transmits once a minute will almost never be caught by a sweep.";
}

// --- event stream ----------------------------------------------------------

function handle(event) {
  if (event.type === "frame") {
    addRow(event);
  } else if (event.type === "greeting") {
    log(event.text, "ok");
    showFirmware(event);
    // The wiring it carries is not copied into the fields here: the `info` the
    // server asks for next reports the same wiring and the configuration with
    // it, so there is one path into those fields instead of two.
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
    showPort(event.port);
    // Carried on every status event, so disconnecting clears it without a rule
    // of its own: the server drops the greeting with the port.
    showFirmware(event.greeting);
    const text = event.state || (connected ? "connected" : "not connected");
    setState(text, stateClass(text, connected));
    renderSummary();
  } else if (event.type === "radio") {
    deviceRadio = event.radio;
    renderSummary();
    seedSetup();
  } else if (event.type === "reset") {
    // Either someone cleared the history, or this connection cannot be
    // continued from what is on screen - a different server process, or a
    // clear we were disconnected across. What follows is a full replay, so
    // the table has to be empty to receive it.
    rebuild([]);
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
    // Log only. The state comes from status events - reading it out of log text
    // meant teaching every new state to two places, and forgetting one silently.
    const kind = { error: "err", warn: "warn", ok: "ok", sent: "sent" }[event.kind] || "";
    log(event.text, kind);
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

// While a port is open, which one it is comes from the server and is shown as
// text; the dropdown steps aside. It is an input - its value is whatever was
// last picked in this tab, which is how a page capturing from COM18 came to
// display COM9, the port of something else entirely.
function showPort(port) {
  $("port").hidden = connected;
  $("port-text").hidden = !connected;
  $("port-text").textContent = port || "";
  $("port-text").title = port ? `connected on ${port}` : "";
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
  renderSummary();
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
    markPipes();
  });
  for (const id of [...RADIO, ...PIPES.map((n) => "pipe" + n)]) {
    $(id).addEventListener("input", markPipes);
    $(id).addEventListener("change", markPipes);
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

  $("setup-open").addEventListener("click", () => {
    seedSetup();               // whatever the dongle says right now, not before
    $("setup").showModal();
  });
  // Esc comes free with showModal(); clicking the backdrop does not. The form
  // fills the dialog box, so a click landing on the dialog itself is outside.
  $("setup").addEventListener("click", (e) => {
    if (e.target === $("setup")) $("setup").close();
  });
  // However it was closed - Apply, Close, Esc, backdrop - the fields go back to
  // describing the dongle. An edit that was not applied changed nothing, and a
  // field left showing it would be the old lie in a smaller box.
  $("setup").addEventListener("close", seedSetup);
  $("apply").addEventListener("click", applySetup);

  // Start is about reception, not about configuration: the dongle keeps what it
  // was given until something changes it, so starting again resumes with the
  // configuration it actually has. Only a dongle that has none - fresh off a
  // reset - is configured from the fields, because nothing else knows yet.
  $("run").addEventListener("click", () => {
    if (listening()) return void send("stop");
    if (deviceRadio && deviceRadio.configured) return void send("listen");
    applySetup();
  });
  for (const btn of document.querySelectorAll("[data-cmd]")) {
    btn.addEventListener("click", () => send(btn.dataset.cmd));
  }

  $("scan-run").addEventListener("click", () => {
    if (scanning()) return void send("scan off");
    $("scan-chart").replaceChildren();     // a new scan, not an update of the old
    send($("scan-live").checked ? "scan live" : "scan");
  });
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

  // No local rebuild: the server answers a clear with a reset event, which
  // empties this table on the same path as every other tab's. Doing it here as
  // well would be the one tab that clears for a different reason than the rest.
  $("clear").addEventListener("click", () => post("/api/clear"));

  // Regrouping is a pure view change, so it works on what is already here.
  // The header changes with it: ungrouped, the repeat column has nothing to say.
  $("group").addEventListener("change", () => {
    const list = frames.slice();
    setColumns();
    rebuild(list);
  });

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

  showBuild();
  // Polled, not pushed: the answer changes when a file on disk changes, which
  // the server has no event for. A minute is soon enough to catch an edit
  // before it wastes an hour of debugging.
  setInterval(showBuild, 60000);
}

// The dongle's own build, from the greeting - the only line that carries it,
// and one the server replays to every tab that opens later. Shown beside this
// server's version because it answers the same question about the other end of
// the serial link: which build am I actually talking to?
function showFirmware(greeting) {
  const el = $("fw");
  el.hidden = !greeting;
  if (!greeting) return;
  const fields = greeting.fields || {};
  el.textContent = `fw ${fields.fw || "?"} · api ${fields.api || "?"}`;
  el.classList.toggle("mismatch", !greeting.apiOk);
  el.classList.toggle("dim", Boolean(greeting.apiOk));
  el.title = greeting.apiOk
    ? `Dongle firmware ${fields.fw}, speaking command protocol api=${fields.api}`
    : `Dongle speaks api=${fields.api}, this ui expects api=${greeting.expectedApi} — `
      + "commands may be understood differently at each end";
}

// --- which build is answering ----------------------------------------------

let staleWarned = false;

async function showBuild() {
  let app;
  try {
    app = (await (await fetch("/api/state")).json()).app;
  } catch {
    return;  // the stream's own error handling already says the server is gone
  }
  if (!app) return;

  const el = $("build");
  const started = new Date(app.started * 1000).toLocaleString();
  if (app.stale && app.stale.length) {
    el.classList.add("stale");
    el.classList.remove("dim");
    el.textContent = `v${app.version} — restart to load changes`;
    el.title = `Changed on disk since this server started (${started}):\n` +
               app.stale.join("\n") +
               "\n\nPython keeps imported modules in memory, so what you see " +
               "is the older code.";
    if (!staleWarned) {
      staleWarned = true;
      log(`[warning] source changed since start (${app.stale.join(", ")}) - ` +
          `restart the server, this page is showing older behaviour`, "warn");
    }
  } else {
    el.classList.remove("stale");
    el.classList.add("dim");
    el.textContent = `v${app.version}`;
    el.title = `Running since ${started}`;
    staleWarned = false;
  }
}

init();
