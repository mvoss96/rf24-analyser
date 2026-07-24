#!/usr/bin/env python3
"""nrf24gui - tkinter front end for the nrf24-sniffer dongle.

Layout follows what the work actually looks like: you configure once, then watch
frames for a long time. So the setup strip collapses to a one-line summary of the
active configuration, and the frame table gets the window.

The serial protocol lives in nrf24_dongle.py and the decoders in
nrf24_parsers.py, both shared with the nrf24term.py terminal, so there is only
ever one implementation of each.

    python nrf24gui.py [COM18]
"""

import sys
import time
import tkinter as tk
from tkinter import filedialog, ttk

import nrf24_dongle as dongle
import nrf24_parsers as parsers

POLL_MS = 50          # queue drain interval
MAX_ROWS = 5000       # keep the table bounded during long captures
MONO = ("Consolas", 9)


class App:
    def __init__(self, root, initial_port=None):
        self.root = root
        self.dongle = None
        self.frames = {}        # tree item id -> (pipe, data)
        self.logfile = None
        self.frame_count = 0
        self.last_stamp = None  # for the delta column
        self.setup_open = False

        root.title("nrf24-sniffer")
        root.geometry("1120x780")

        self._build_topbar()
        self._build_setup()
        self._build_table()
        self._build_bottom()

        if initial_port:
            self.port_box.set(initial_port)
        self._refresh_ports(select_first=not initial_port)
        self._update_summary()
        self.root.after(POLL_MS, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # --- top bar ------------------------------------------------------------

    def _build_topbar(self):
        bar = ttk.Frame(self.root, padding=(10, 8))
        bar.pack(fill="x")

        ttk.Label(bar, text="Port").pack(side="left")
        self.port_box = ttk.Combobox(bar, width=32, state="readonly")
        self.port_box.pack(side="left", padx=(6, 4))
        ttk.Button(bar, text="Refresh", width=8,
                   command=self._refresh_ports).pack(side="left")
        self.connect_btn = ttk.Button(bar, text="Connect", width=11,
                                      command=self._toggle_connect)
        self.connect_btn.pack(side="left", padx=6)

        self.state_dot = tk.Label(bar, text="●", fg="#999")
        self.state_dot.pack(side="left", padx=(10, 3))
        self.state_label = ttk.Label(bar, text="not connected", foreground="#777")
        self.state_label.pack(side="left")

        ttk.Label(bar, text="Decoder").pack(side="left", padx=(24, 4))
        self.parser_box = ttk.Combobox(bar, width=22, state="readonly",
                                       values=[p.label for p in parsers.all_parsers()])
        self.parser_box.pack(side="left")
        self.parser_box.set(parsers.get("bthome").label)
        self.parser_box.bind("<<ComboboxSelected>>", self._on_parser_changed)

    # --- setup strip --------------------------------------------------------

    def _build_setup(self):
        self.setup_bar = ttk.Frame(self.root, padding=(10, 0))
        self.setup_bar.pack(fill="x")

        header = ttk.Frame(self.setup_bar)
        header.pack(fill="x")
        self.toggle_btn = ttk.Button(header, text="▾ Setup", width=10,
                                     command=self._toggle_setup)
        self.toggle_btn.pack(side="left")
        self.summary = ttk.Label(header, text="", foreground="#666", font=MONO)
        self.summary.pack(side="left", padx=10)

        self.start_btn = ttk.Button(header, text="Start", width=10, command=self._start)
        self.start_btn.pack(side="right")
        for text, command in [("Scan", lambda: self._send("scan")),
                              ("Info", lambda: self._send("info")),
                              ("Stop", lambda: self._send("stop"))]:
            ttk.Button(header, text=text, width=7, command=command).pack(side="right", padx=3)

        self.setup_body = ttk.Frame(self.setup_bar)
        self._build_setup_fields(self.setup_body)
        # Starts collapsed: the summary line already says what we listen to, and
        # the defaults are prefilled - opening it is the exception, not the rule.
        self.toggle_btn.configure(text="▸ Setup")

    def _build_setup_fields(self, parent):
        wiring = ttk.LabelFrame(parent, text="Wiring", padding=6)
        wiring.pack(side="left", fill="y")
        self.hw_vars = {}
        for column, (key, default) in enumerate(
                [("ce", "9"), ("csn", "10"), ("irq", "2"),
                 ("led_rx", "8"), ("led_tx", "A1")]):
            ttk.Label(wiring, text=key).grid(row=0, column=column, padx=3)
            var = tk.StringVar(value=default)
            var.trace_add("write", lambda *_: self._update_summary())
            ttk.Entry(wiring, textvariable=var, width=6).grid(row=1, column=column, padx=3)
            self.hw_vars[key] = var
        ttk.Button(wiring, text="Forget stored wiring",
                   command=lambda: self._send("hwclear")).grid(
            row=2, column=0, columnspan=5, sticky="ew", pady=(6, 0))

        radio = ttk.LabelFrame(parent, text="Radio", padding=6)
        radio.pack(side="left", fill="y", padx=8)

        self.ch = tk.StringVar(value="100")
        self.rate = tk.StringVar(value="250")
        self.crc = tk.StringVar(value="16")
        self.aw = tk.StringVar(value="5")
        self.pa = tk.StringVar(value="low")
        self.plsize = tk.StringVar(value="32")
        self.ack = tk.BooleanVar(value=False)
        self.dpl = tk.BooleanVar(value=True)
        for var in (self.ch, self.rate, self.crc, self.aw, self.pa):
            var.trace_add("write", lambda *_: self._update_summary())

        def field(col, text, widget):
            ttk.Label(radio, text=text).grid(row=0, column=col, padx=3)
            widget.grid(row=1, column=col, padx=3)

        field(0, "ch", ttk.Entry(radio, textvariable=self.ch, width=5))
        field(1, "rate", ttk.Combobox(radio, textvariable=self.rate, width=6,
                                      state="readonly", values=["250", "1000", "2000"]))
        field(2, "crc", ttk.Combobox(radio, textvariable=self.crc, width=4,
                                     state="readonly", values=["0", "8", "16"]))
        field(3, "aw", ttk.Combobox(radio, textvariable=self.aw, width=4,
                                    state="readonly", values=["3", "4", "5"]))
        field(4, "pa", ttk.Combobox(radio, textvariable=self.pa, width=6, state="readonly",
                                    values=["min", "low", "high", "max"]))
        field(5, "plsize", ttk.Entry(radio, textvariable=self.plsize, width=5))
        ttk.Checkbutton(radio, text="ack", variable=self.ack).grid(row=1, column=6, padx=(10, 2))
        ttk.Checkbutton(radio, text="dpl", variable=self.dpl).grid(row=1, column=7, padx=2)
        self.repeats = tk.BooleanVar(value=True)
        ttk.Checkbutton(radio, text="show repeats", variable=self.repeats,
                        command=self._apply_repeats).grid(row=1, column=8, padx=(10, 2))

        pipes = ttk.LabelFrame(parent, text="Pipe addresses", padding=6)
        pipes.pack(side="left", fill="y")
        self.pipe_vars = {}
        # Pipe 1 is what a single-address protocol uses; the rest hide behind
        # "more" so the common case is not buried in five empty fields.
        for number in range(6):
            label = ttk.Label(pipes, text=str(number))
            entry_var = tk.StringVar(value="42:54:48:4D:45" if number == 1 else "")
            entry_var.trace_add("write", lambda *_: self._update_summary())
            entry = ttk.Entry(pipes, textvariable=entry_var, width=16)
            self.pipe_vars[number] = entry_var
            if number == 1:
                label.grid(row=0, column=0, sticky="e")
                entry.grid(row=0, column=1, padx=3)
            else:
                label.grid(row=number + 1, column=0, sticky="e")
                entry.grid(row=number + 1, column=1, padx=3)
                label.grid_remove()
                entry.grid_remove()
                self.pipe_vars.setdefault("_hidden", []).append((label, entry))
        self.more_btn = ttk.Button(pipes, text="more ▾", width=9,
                                   command=self._toggle_pipes)
        self.more_btn.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.pipes_open = False

    def _toggle_pipes(self):
        self.pipes_open = not self.pipes_open
        for label, entry in self.pipe_vars["_hidden"]:
            if self.pipes_open:
                label.grid()
                entry.grid()
            else:
                label.grid_remove()
                entry.grid_remove()
        self.more_btn.configure(text="less ▴" if self.pipes_open else "more ▾")

    def _toggle_setup(self):
        self.setup_open = not self.setup_open
        if self.setup_open:
            self.setup_body.pack(fill="x", pady=(6, 6))
        else:
            self.setup_body.pack_forget()
        self.toggle_btn.configure(text=("▾ Setup" if self.setup_open else "▸ Setup"))

    def _pipes(self):
        return {n: v.get() for n, v in self.pipe_vars.items() if isinstance(n, int)}

    def _update_summary(self, *_):
        pipes = " ".join(f"p{n}={a}" for n, a in sorted(self._pipes().items()) if a.strip())
        self.summary.configure(
            text=f"ce={self.hw_vars['ce'].get()} csn={self.hw_vars['csn'].get()}  |  "
                 f"ch{self.ch.get()} {self.rate.get()}k crc{self.crc.get()} "
                 f"aw{self.aw.get()} pa={self.pa.get()}  |  {pipes}")

    # --- table --------------------------------------------------------------

    def _build_table(self):
        frame = ttk.Frame(self.root, padding=(10, 0))
        frame.pack(fill="both", expand=True)

        columns = ("time", "delta", "pipe", "len", "summary")
        self.tree = ttk.Treeview(frame, columns=columns, show="headings", height=16)
        for column, text, width, anchor in [
                ("time", "Time", 105, "center"), ("delta", "Δ ms", 65, "e"),
                ("pipe", "Pipe", 45, "center"), ("len", "Len", 45, "center"),
                ("summary", "Decoded", 780, "w")]:
            self.tree.heading(column, text=text, anchor=anchor)
            self.tree.column(column, width=width, anchor=anchor)
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        # Frames the decoder objected to stand out without reading every row.
        self.tree.tag_configure("flagged", foreground="#b00000")
        self.tree.tag_configure("odd", background="#f4f4f4")

    # --- bottom notebook ----------------------------------------------------

    def _build_bottom(self):
        notebook = ttk.Notebook(self.root, padding=(10, 6))
        notebook.pack(fill="both", expand=False)

        # Detail: decoded and raw side by side, so both are visible at once.
        detail_tab = ttk.Frame(notebook)
        split = ttk.PanedWindow(detail_tab, orient="horizontal")
        split.pack(fill="both", expand=True)

        left = ttk.Frame(split)
        ttk.Label(left, text="decoded", foreground="#777").pack(anchor="w")
        self.detail = tk.Text(left, height=7, wrap="none", font=MONO)
        self.detail.pack(fill="both", expand=True)
        split.add(left, weight=3)

        right = ttk.Frame(split)
        ttk.Label(right, text="raw", foreground="#777").pack(anchor="w")
        self.rawview = tk.Text(right, height=7, wrap="none", font=MONO, width=52)
        self.rawview.pack(fill="both", expand=True)
        split.add(right, weight=2)
        notebook.add(detail_tab, text="Detail")

        # Log: everything the dongle says, plus the free-text command line -
        # they belong together because the reply lands here.
        log_tab = ttk.Frame(notebook)
        self.log = tk.Text(log_tab, height=7, wrap="none", font=MONO)
        log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set)
        self.log.pack(side="top", fill="both", expand=True)
        log_scroll.place(relx=1.0, rely=0, relheight=1.0, anchor="ne")

        entry_row = ttk.Frame(log_tab)
        entry_row.pack(fill="x", pady=(4, 0))
        ttk.Label(entry_row, text="Command").pack(side="left")
        self.command = ttk.Entry(entry_row)
        self.command.pack(side="left", fill="x", expand=True, padx=6)
        self.command.bind("<Return>", self._send_typed)
        ttk.Button(entry_row, text="Send", command=self._send_typed).pack(side="left")
        notebook.add(log_tab, text="Log")

        status = ttk.Frame(self.root, padding=(10, 4))
        status.pack(fill="x")
        ttk.Button(status, text="Clear frames", command=self._clear).pack(side="left")
        self.log_btn = ttk.Button(status, text="Log to file...", command=self._toggle_log)
        self.log_btn.pack(side="left", padx=6)
        self.status = ttk.Label(status, text="0 frames", foreground="#666")
        self.status.pack(side="right")

    # --- connection ---------------------------------------------------------

    def _refresh_ports(self, select_first=True):
        ports = dongle.available_ports()
        self.port_box["values"] = [f"{device}  {desc}".strip() for device, desc in ports]
        if select_first and ports and not self.port_box.get():
            self.port_box.current(0)

    def _toggle_connect(self):
        if self.dongle is not None:
            self._disconnect()
            return
        text = self.port_box.get()
        if not text:
            self._log("[no port selected]")
            return
        port = text.split()[0]
        try:
            self.dongle = dongle.Dongle(port)
            self.dongle.open()
        except Exception as exc:
            self.dongle = None
            self._log(f"[could not open {port}: {exc}]")
            return
        self.connect_btn.configure(text="Disconnect")
        self._set_state("connecting", "#c08000")
        self._log(f"[connected to {port} @ {dongle.DEFAULT_BAUD}]")

    def _disconnect(self):
        if self.dongle is not None:
            self.dongle.close()
            self.dongle = None
        self.connect_btn.configure(text="Connect")
        self._set_state("not connected", "#777")
        self._log("[disconnected]")

    def _set_state(self, text, colour):
        self.state_dot.configure(fg=colour)
        self.state_label.configure(text=text, foreground=colour)

    # --- commands -----------------------------------------------------------

    def _send(self, line):
        if self.dongle is None:
            self._log("[not connected]")
            return
        try:
            self.dongle.send(line)
        except Exception as exc:
            self._log(f"[send failed: {exc}]")
            return
        self._log(f"> {line}")

    def _send_typed(self, _event=None):
        line = self.command.get().strip()
        if line:
            self._send(line)
            self.command.delete(0, "end")

    def _start(self):
        """One button for what is always the same two steps."""
        self._send(dongle.build_hwset(
            self.hw_vars["ce"].get(), self.hw_vars["csn"].get(), self.hw_vars["irq"].get(),
            self.hw_vars["led_rx"].get(), self.hw_vars["led_tx"].get()))
        self.root.after(150, lambda: self._send(dongle.build_listen(
            self.ch.get(), self.rate.get(), self.crc.get(), self.aw.get(), self.pa.get(),
            self.ack.get(), self.dpl.get(), self._pipes(), self.plsize.get())))

    def _apply_repeats(self):
        self._send(f"repeats {int(self.repeats.get())}")

    # --- decoding and display ----------------------------------------------

    def _parser(self):
        label = self.parser_box.get()
        for parser in parsers.all_parsers():
            if parser.label == label:
                return parser
        return parsers.get("raw")

    def _on_parser_changed(self, _event=None):
        parser = self._parser()
        reason = parser.available()
        if reason:
            self._log(f"[decoder '{parser.label}' unavailable: {reason}]")
            return
        for item in self.tree.get_children():
            pipe, data = self.frames[item]
            summary, flagged = self._summarise(parser, data)
            values = list(self.tree.item(item, "values"))
            values[4] = summary
            tags = [t for t in self.tree.item(item, "tags") if t == "odd"]
            if flagged:
                tags.append("flagged")
            self.tree.item(item, values=values, tags=tuple(tags))
        self._on_select()

    @staticmethod
    def _summarise(parser, data):
        try:
            summary = parser.summary(data)
        except Exception as exc:
            return f"(decoder error: {exc})", True
        return summary, ("!!" in summary or "rejected" in summary.lower())

    def _add_frame(self, pipe, data):
        now = time.time()
        stamp = time.strftime("%H:%M:%S", time.localtime(now)) + f".{int(now % 1 * 1000):03d}"
        delta = "" if self.last_stamp is None else f"{(now - self.last_stamp) * 1000:.1f}"
        self.last_stamp = now

        summary, flagged = self._summarise(self._parser(), data)
        tags = []
        if flagged:
            tags.append("flagged")
        if self.frame_count % 2:
            tags.append("odd")
        item = self.tree.insert("", "end", values=(stamp, delta, pipe, len(data), summary),
                                tags=tuple(tags))
        self.frames[item] = (pipe, data)
        self.frame_count += 1
        self.status.configure(text=f"{self.frame_count} frames")

        children = self.tree.get_children()
        if len(children) > MAX_ROWS:
            for old in children[:len(children) - MAX_ROWS]:
                self.frames.pop(old, None)
                self.tree.delete(old)
        self.tree.see(item)

    def _on_select(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        entry = self.frames.get(selection[0])
        if entry is None:
            return
        pipe, data = entry
        parser = self._parser()
        try:
            lines = parser.detail(data)
        except Exception as exc:
            lines = [f"  (decoder error: {exc})"]
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", "\n".join(
            [f"pipe {pipe}, {len(data)} bytes, {parser.label}", ""] + lines))
        self.rawview.delete("1.0", "end")
        self.rawview.insert("1.0", "\n".join(parsers.hexdump(data)))

    def _log(self, text):
        self.log.insert("end", text + "\n")
        self.log.see("end")
        if self.logfile:
            self.logfile.write(text + "\n")
            self.logfile.flush()

    def _clear(self):
        self.tree.delete(*self.tree.get_children())
        self.frames.clear()
        self.frame_count = 0
        self.last_stamp = None
        self.status.configure(text="0 frames")
        self.detail.delete("1.0", "end")
        self.rawview.delete("1.0", "end")

    def _toggle_log(self):
        if self.logfile:
            self.logfile.close()
            self.logfile = None
            self.log_btn.configure(text="Log to file...")
            return
        path = filedialog.asksaveasfilename(defaultextension=".log",
                                            filetypes=[("Log", "*.log"), ("All", "*.*")])
        if not path:
            return
        self.logfile = open(path, "a", encoding="utf-8")
        self.log_btn.configure(text="Stop logging")

    # --- serial pump --------------------------------------------------------

    def _drain(self):
        if self.dongle is not None:
            while True:
                try:
                    line = self.dongle.lines.get_nowait()
                except Exception:
                    break
                self._handle_line(line)
        self.root.after(POLL_MS, self._drain)

    def _handle_line(self, line):
        received = dongle.parse_rx(line)
        if received is not None:
            self._add_frame(*received)
            if self.logfile:
                self.logfile.write(line + "\n")
            return

        greeting = dongle.parse_greeting(line)
        if greeting is not None:
            self._log(line)
            api = greeting.get("api")
            if api is not None and api != str(dongle.EXPECTED_API):
                self._log(f"[warning] firmware api={api}, this tool expects "
                          f"api={dongle.EXPECTED_API}")
                self._set_state(f"api {api} mismatch", "#b00000")
            elif greeting.get("hw") == "failed":
                self._set_state("wiring failed", "#b00000")
            else:
                self._set_state(greeting.get("state", "connected"), "#0a7a0a")
            for key, var in self.hw_vars.items():
                if key in greeting:
                    var.set(greeting[key])
            return

        if line.startswith("OK listening"):
            self._set_state("listening", "#0a7a0a")
        elif line.startswith("OK stopped"):
            self._set_state("idle", "#c08000")
        elif line.startswith(("ERR", "WARN")):
            self._set_state(line.split(maxsplit=1)[0].lower(), "#b00000")
        self._log(line)

    def _on_close(self):
        if self.dongle is not None:
            self.dongle.close()
        if self.logfile:
            self.logfile.close()
        self.root.destroy()


def main():
    port = sys.argv[1] if len(sys.argv) > 1 else None
    root = tk.Tk()
    App(root, port)
    root.mainloop()


if __name__ == "__main__":
    main()
