#!/usr/bin/env python3
"""nrf24gui - tkinter front end for the nrf24-sniffer dongle.

Exposes every dongle setting (wiring and radio parameters), lets the decoder be
switched at runtime, and shows received frames as a table with a detail pane.

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
MAX_ROWS = 2000       # keep the table bounded during long captures


class App:
    def __init__(self, root, initial_port=None):
        self.root = root
        self.dongle = None
        self.frames = {}       # tree item id -> (pipe, data)
        self.logfile = None
        self.frame_count = 0
        root.title("nrf24-sniffer")
        root.geometry("1080x760")

        self._build_connection_bar()
        self._build_settings()
        self._build_output()
        self._build_statusbar()

        if initial_port:
            self.port_box.set(initial_port)
        self._refresh_ports(select_first=not initial_port)
        self._set_connected(False)
        self.root.after(POLL_MS, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # --- layout -------------------------------------------------------------

    def _build_connection_bar(self):
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill="x")

        ttk.Label(bar, text="Port").pack(side="left")
        self.port_box = ttk.Combobox(bar, width=34, state="readonly")
        self.port_box.pack(side="left", padx=6)
        ttk.Button(bar, text="Refresh", command=self._refresh_ports).pack(side="left")
        self.connect_btn = ttk.Button(bar, text="Connect", command=self._toggle_connect)
        self.connect_btn.pack(side="left", padx=6)

        ttk.Label(bar, text="Decoder").pack(side="left", padx=(20, 4))
        self.parser_box = ttk.Combobox(bar, width=24, state="readonly",
                                       values=[p.label for p in parsers.all_parsers()])
        self.parser_box.pack(side="left")
        self.parser_box.set(parsers.get("bthome").label)
        self.parser_box.bind("<<ComboboxSelected>>", self._on_parser_changed)

        self.greeting = ttk.Label(bar, text="not connected", foreground="#888")
        self.greeting.pack(side="right")

    def _build_settings(self):
        outer = ttk.Frame(self.root, padding=(8, 0))
        outer.pack(fill="x")

        # -- wiring --
        hw = ttk.LabelFrame(outer, text="Wiring (hwset)", padding=6)
        hw.pack(side="left", fill="y")
        self.hw_vars = {}
        for column, (key, default) in enumerate(
                [("ce", "9"), ("csn", "10"), ("irq", "2"),
                 ("led_rx", "8"), ("led_tx", "A1")]):
            ttk.Label(hw, text=key).grid(row=0, column=column, padx=3)
            var = tk.StringVar(value=default)
            ttk.Entry(hw, textvariable=var, width=6).grid(row=1, column=column, padx=3)
            self.hw_vars[key] = var
        ttk.Button(hw, text="Apply", command=self._apply_hwset).grid(row=2, column=0, columnspan=2,
                                                                    sticky="ew", pady=(6, 0))
        ttk.Button(hw, text="Clear stored", command=lambda: self._send("hwclear")).grid(
            row=2, column=2, columnspan=3, sticky="ew", pady=(6, 0))

        # -- radio --
        radio = ttk.LabelFrame(outer, text="Radio (listen)", padding=6)
        radio.pack(side="left", fill="y", padx=8)

        self.ch = tk.StringVar(value="100")
        self.rate = tk.StringVar(value="250")
        self.crc = tk.StringVar(value="16")
        self.aw = tk.StringVar(value="5")
        self.pa = tk.StringVar(value="low")
        self.plsize = tk.StringVar(value="32")
        self.ack = tk.BooleanVar(value=False)
        self.dpl = tk.BooleanVar(value=True)

        def field(col, text, widget):
            ttk.Label(radio, text=text).grid(row=0, column=col, padx=3)
            widget.grid(row=1, column=col, padx=3)

        field(0, "ch", ttk.Entry(radio, textvariable=self.ch, width=5))
        field(1, "rate", ttk.Combobox(radio, textvariable=self.rate, width=6, state="readonly",
                                      values=["250", "1000", "2000"]))
        field(2, "crc", ttk.Combobox(radio, textvariable=self.crc, width=4, state="readonly",
                                     values=["0", "8", "16"]))
        field(3, "aw", ttk.Combobox(radio, textvariable=self.aw, width=4, state="readonly",
                                    values=["3", "4", "5"]))
        field(4, "pa", ttk.Combobox(radio, textvariable=self.pa, width=6, state="readonly",
                                    values=["min", "low", "high", "max"]))
        field(5, "plsize", ttk.Entry(radio, textvariable=self.plsize, width=5))
        ttk.Checkbutton(radio, text="ack", variable=self.ack).grid(row=1, column=6, padx=6)
        ttk.Checkbutton(radio, text="dpl", variable=self.dpl).grid(row=1, column=7, padx=2)

        ttk.Label(radio, text="pipe addresses (blank = off)").grid(
            row=2, column=0, columnspan=8, sticky="w", pady=(6, 0))
        self.pipe_vars = {}
        for number in range(6):
            ttk.Label(radio, text=str(number)).grid(row=3, column=number, sticky="e")
            var = tk.StringVar(value="42:54:48:4D:45" if number == 1 else "")
            ttk.Entry(radio, textvariable=var, width=15).grid(row=4, column=number, padx=2)
            self.pipe_vars[number] = var

        buttons = ttk.Frame(radio)
        buttons.grid(row=5, column=0, columnspan=8, sticky="ew", pady=(6, 0))
        for text, command in [("Listen", self._apply_listen),
                              ("Stop", lambda: self._send("stop")),
                              ("Info", lambda: self._send("info")),
                              ("Scan", lambda: self._send("scan"))]:
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=2)
        self.repeats = tk.BooleanVar(value=True)
        ttk.Checkbutton(buttons, text="show repeats", variable=self.repeats,
                        command=self._apply_repeats).pack(side="left", padx=10)

    def _build_output(self):
        panes = ttk.PanedWindow(self.root, orient="vertical")
        panes.pack(fill="both", expand=True, padx=8, pady=6)

        table_frame = ttk.Frame(panes)
        columns = ("time", "pipe", "len", "summary")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=14)
        for column, text, width in [("time", "Time", 90), ("pipe", "Pipe", 45),
                                    ("len", "Len", 45), ("summary", "Decoded", 800)]:
            self.tree.heading(column, text=text)
            self.tree.column(column, width=width, anchor="w" if column == "summary" else "center")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        # Frames the decoder objected to stand out.
        self.tree.tag_configure("flagged", foreground="#b00000")
        panes.add(table_frame, weight=3)

        lower = ttk.Frame(panes)
        self.detail = tk.Text(lower, height=12, wrap="none", font=("Consolas", 9))
        detail_scroll = ttk.Scrollbar(lower, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=detail_scroll.set)
        self.detail.pack(side="left", fill="both", expand=True)
        detail_scroll.pack(side="right", fill="y")
        panes.add(lower, weight=2)

    def _build_statusbar(self):
        bar = ttk.Frame(self.root, padding=(8, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="Command").pack(side="left")
        self.command = ttk.Entry(bar)
        self.command.pack(side="left", fill="x", expand=True, padx=6)
        self.command.bind("<Return>", self._send_typed)
        ttk.Button(bar, text="Send", command=self._send_typed).pack(side="left")
        ttk.Button(bar, text="Clear", command=self._clear).pack(side="left", padx=4)
        self.log_btn = ttk.Button(bar, text="Log to file...", command=self._toggle_log)
        self.log_btn.pack(side="left")
        self.status = ttk.Label(bar, text="0 frames", foreground="#666")
        self.status.pack(side="right")

    # --- connection ---------------------------------------------------------

    def _refresh_ports(self, select_first=True):
        ports = dongle.available_ports()
        labels = [f"{device}  {description}".strip() for device, description in ports]
        self.port_box["values"] = labels
        self._port_devices = [device for device, _ in ports]
        if select_first and labels and not self.port_box.get():
            self.port_box.current(0)

    def _selected_port(self):
        text = self.port_box.get()
        if not text:
            return None
        # Combobox shows "COM18  USB-SERIAL CH340"; the device is the first word.
        return text.split()[0]

    def _toggle_connect(self):
        if self.dongle is not None:
            self._disconnect()
            return
        port = self._selected_port()
        if not port:
            self._log("[no port selected]")
            return
        try:
            self.dongle = dongle.Dongle(port)
            self.dongle.open()
        except Exception as exc:
            self.dongle = None
            self._log(f"[could not open {port}: {exc}]")
            return
        self._set_connected(True)
        self._log(f"[connected to {port} @ {dongle.DEFAULT_BAUD}]")

    def _disconnect(self):
        if self.dongle is not None:
            self.dongle.close()
            self.dongle = None
        self._set_connected(False)
        self.greeting.configure(text="not connected", foreground="#888")
        self._log("[disconnected]")

    def _set_connected(self, connected):
        self.connect_btn.configure(text="Disconnect" if connected else "Connect")

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

    def _apply_hwset(self):
        self._send(dongle.build_hwset(
            self.hw_vars["ce"].get(), self.hw_vars["csn"].get(), self.hw_vars["irq"].get(),
            self.hw_vars["led_rx"].get(), self.hw_vars["led_tx"].get()))

    def _apply_listen(self):
        self._send(dongle.build_listen(
            self.ch.get(), self.rate.get(), self.crc.get(), self.aw.get(), self.pa.get(),
            self.ack.get(), self.dpl.get(),
            {n: v.get() for n, v in self.pipe_vars.items()}, self.plsize.get()))

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
        # Re-render every row that is already in the table with the new decoder.
        for item in self.tree.get_children():
            pipe, data = self.frames[item]
            summary, flagged = self._summarise(parser, data)
            self.tree.item(item, values=(self.tree.set(item, "time"), pipe, len(data), summary),
                           tags=("flagged",) if flagged else ())
        self._on_select()

    @staticmethod
    def _summarise(parser, data):
        try:
            summary = parser.summary(data)
        except Exception as exc:
            return f"(decoder error: {exc})", True
        return summary, ("!!" in summary or "rejected" in summary.lower())

    def _add_frame(self, pipe, data):
        parser = self._parser()
        summary, flagged = self._summarise(parser, data)
        stamp = time.strftime("%H:%M:%S")
        item = self.tree.insert("", "end", values=(stamp, pipe, len(data), summary),
                                tags=("flagged",) if flagged else ())
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
        text = [f"pipe {pipe}, {len(data)} bytes, decoder: {parser.label}", ""]
        text += lines
        text += ["", "raw:"] + parsers.hexdump(data)
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", "\n".join(text))

    def _log(self, text):
        self.detail.insert("end", text + "\n")
        self.detail.see("end")
        if self.logfile:
            self.logfile.write(text + "\n")
            self.logfile.flush()

    def _clear(self):
        self.tree.delete(*self.tree.get_children())
        self.frames.clear()
        self.frame_count = 0
        self.status.configure(text="0 frames")
        self.detail.delete("1.0", "end")

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
        if self.logfile:
            self.logfile.write(line + "\n")

        received = dongle.parse_rx(line)
        if received is not None:
            self._add_frame(*received)
            return

        greeting = dongle.parse_greeting(line)
        if greeting is not None:
            api = greeting.get("api")
            summary = " ".join(f"{k}={v}" for k, v in greeting.items())
            mismatch = api is not None and api != str(dongle.EXPECTED_API)
            self.greeting.configure(
                text=summary,
                foreground="#b00000" if mismatch or greeting.get("hw") == "failed" else "#060")
            self._log(line)
            if mismatch:
                self._log(f"[warning] firmware api={api}, this tool expects "
                          f"api={dongle.EXPECTED_API}")
            # Prefill the wiring fields from what the dongle reports.
            for key, var in self.hw_vars.items():
                if key in greeting:
                    var.set(greeting[key])
            return

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
