#!/usr/bin/env python3
"""
dashboard.py — Clickable GUI wrapper for fwdvol_scanner.py
"""

import subprocess
import sys
import threading
import tkinter as tk
from tkinter import font, scrolledtext, ttk

PYTHON = sys.executable
SCANNER = "fwdvol_scanner.py"

BUTTONS = [
    ("Dry Run (Mock)",       ["--dry-run"],                    "#2d6a4f"),
    ("Live — All",           [],                               "#1d3557"),
    ("Live — SPX",           ["--underlyings", "SPX"],         "#1d3557"),
    ("Live — SPY",           ["--underlyings", "SPY"],         "#1d3557"),
    ("Live — QQQ",           ["--underlyings", "QQQ"],         "#1d3557"),
    ("Live — IWM",           ["--underlyings", "IWM"],         "#1d3557"),
    ("Live — RUT",           ["--underlyings", "RUT"],         "#1d3557"),
]

FONT_MONO = ("Consolas", 10)
FONT_UI   = ("Segoe UI", 10)
BG        = "#1e1e2e"
BG_OUT    = "#12121c"
FG        = "#cdd6f4"
FG_DIM    = "#6c7086"
ACCENT    = "#89b4fa"


class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Forward Vol Scanner")
        self.configure(bg=BG)
        self.resizable(True, True)
        self._running = False
        self._build_ui()
        self.geometry("900x620")

    def _build_ui(self):
        # ── toolbar ──────────────────────────────────────────────────────────
        toolbar = tk.Frame(self, bg=BG, pady=8)
        toolbar.pack(fill="x", padx=12)

        title = tk.Label(toolbar, text="⚡ Fwd Vol Scanner",
                         font=("Segoe UI", 13, "bold"),
                         bg=BG, fg=ACCENT)
        title.pack(side="left", padx=(0, 20))

        # top-N spinner
        tk.Label(toolbar, text="rows:", font=FONT_UI,
                 bg=BG, fg=FG_DIM).pack(side="left")
        self._top = tk.IntVar(value=8)
        spin = tk.Spinbox(toolbar, from_=1, to=30, width=3,
                          textvariable=self._top,
                          font=FONT_UI, bg="#313244", fg=FG,
                          buttonbackground="#45475a",
                          relief="flat", highlightthickness=0)
        spin.pack(side="left", padx=(2, 16))

        # scan buttons
        for label, extra_args, color in BUTTONS:
            args = extra_args
            btn = tk.Button(
                toolbar, text=label, font=("Segoe UI", 9, "bold"),
                bg=color, fg="white", activebackground=ACCENT,
                activeforeground="#1e1e2e",
                relief="flat", padx=10, pady=5, cursor="hand2",
                command=lambda a=args: self._run_scan(a)
            )
            btn.pack(side="left", padx=3)

        # clear button (right-aligned)
        clr = tk.Button(toolbar, text="Clear", font=("Segoe UI", 9),
                        bg="#45475a", fg=FG, activebackground="#585b70",
                        relief="flat", padx=8, pady=5, cursor="hand2",
                        command=self._clear)
        clr.pack(side="right", padx=3)

        # ── output area ───────────────────────────────────────────────────────
        out_frame = tk.Frame(self, bg=BG)
        out_frame.pack(fill="both", expand=True, padx=12, pady=(0, 4))

        self._out = scrolledtext.ScrolledText(
            out_frame, font=FONT_MONO,
            bg=BG_OUT, fg=FG, insertbackground=FG,
            relief="flat", highlightthickness=1,
            highlightbackground="#313244",
            wrap="none", state="disabled"
        )
        self._out.pack(fill="both", expand=True)

        # colour tags
        self._out.tag_config("header",  foreground=ACCENT, font=("Consolas", 10, "bold"))
        self._out.tag_config("verdict", foreground="#a6e3a1", font=("Consolas", 10, "bold"))
        self._out.tag_config("error",   foreground="#f38ba8")
        self._out.tag_config("dim",     foreground=FG_DIM)

        # ── status bar ────────────────────────────────────────────────────────
        self._status = tk.Label(self, text="Ready", font=("Segoe UI", 9),
                                bg="#181825", fg=FG_DIM,
                                anchor="w", padx=12)
        self._status.pack(fill="x", side="bottom")

    # ── helpers ──────────────────────────────────────────────────────────────

    def _clear(self):
        self._out.config(state="normal")
        self._out.delete("1.0", "end")
        self._out.config(state="disabled")
        self._status.config(text="Cleared")

    def _append(self, text, tag=None):
        self._out.config(state="normal")
        self._out.insert("end", text, tag or "")
        self._out.see("end")
        self._out.config(state="disabled")

    def _set_status(self, text):
        self._status.config(text=text)

    def _run_scan(self, extra_args: list):
        if self._running:
            return
        self._running = True
        top = self._top.get()
        cmd = [PYTHON, SCANNER, "--top", str(top)] + extra_args
        self._set_status(f"Running: {' '.join(cmd[2:])}")

        label = " ".join(a for a in extra_args if not a.startswith("-")) or "all"
        self._append(f"\n{'─'*70}\n", "dim")
        self._append(f"  {' '.join(cmd[1:])}\n", "dim")
        self._append(f"{'─'*70}\n", "dim")

        thread = threading.Thread(target=self._worker, args=(cmd,), daemon=True)
        thread.start()

    def _worker(self, cmd):
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                cwd=__import__("os").path.dirname(__import__("os").path.abspath(__file__))
            )
            for line in proc.stdout:
                tag = None
                if line.startswith("==="):
                    tag = "header"
                elif line.startswith("---") or "CHEAP FWD" in line or "MARGINAL" in line or "NO CAL EDGE" in line:
                    tag = "verdict"
                elif line.startswith("Error") or "Traceback" in line or "RuntimeError" in line:
                    tag = "error"
                self.after(0, self._append, line, tag)
            proc.wait()
            status = "Done (exit 0)" if proc.returncode == 0 else f"Exited {proc.returncode}"
        except Exception as e:
            self.after(0, self._append, f"\nERROR: {e}\n", "error")
            status = "Error"
        self.after(0, self._set_status, status)
        self._running = False


if __name__ == "__main__":
    app = Dashboard()
    app.mainloop()
