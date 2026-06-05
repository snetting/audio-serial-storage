#!/usr/bin/env python3
from __future__ import annotations

import shlex
import subprocess
import sys
import threading
import queue
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk


APP_DIR = Path(__file__).resolve().parent
ASS_CLI = APP_DIR / "ass.py"
CASSETTE_BITRATE = 1200
CASSETTE_MFSK = 2


class AssGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("ASS GUI")
        self.geometry("980x760")
        self.minsize(920, 700)

        self.proc: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str | tuple[str, int | None]] = queue.Queue()
        self._encode_previous_output = "output.wav"
        self._decode_previous_input = ""

        self._build_vars()
        self._build_ui()
        self._refresh_command_preview()
        self.after(50, self._drain_output)

    def _build_vars(self) -> None:
        self.mode = tk.StringVar(value="encode")
        self.encode_output = tk.StringVar(value="output.wav")
        self.encode_input_file = tk.StringVar(value="")
        self.encode_data = tk.StringVar(value="")
        self.encode_live = tk.BooleanVar(value=False)
        self.encode_bitrate = tk.StringVar(value=str(CASSETTE_BITRATE))
        self.encode_mfsk = tk.StringVar(value=str(CASSETTE_MFSK))
        self.encode_compress = tk.StringVar(value="auto")
        self.encode_interleave = tk.StringVar(value="1")
        self.encode_resync = tk.StringVar(value="0")
        self.encode_noclamp = tk.BooleanVar(value=False)
        self.encode_overwrite = tk.BooleanVar(value=False)

        self.decode_input_file = tk.StringVar(value="")
        self.decode_live = tk.BooleanVar(value=True)
        self.decode_autodetect = tk.BooleanVar(value=False)
        self.decode_bitrate = tk.StringVar(value=str(CASSETTE_BITRATE))
        self.decode_mfsk = tk.StringVar(value=str(CASSETTE_MFSK))
        self.decode_auto_interleave = tk.BooleanVar(value=False)
        self.decode_interleave = tk.StringVar(value="1")
        self.decode_bruteforce = tk.BooleanVar(value=False)
        self.decode_overwrite = tk.BooleanVar(value=False)
        self.decode_input_device = tk.StringVar(value="")
        self.decode_debug_capture = tk.StringVar(value="")
        self.decode_live_monitor = tk.BooleanVar(value=True)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        top = ttk.Frame(root)
        top.pack(fill="x")
        ttk.Label(top, text="ASS GUI", font=("TkDefaultFont", 15, "bold")).pack(side="left")
        ttk.Button(top, text="List devices", command=self._list_devices).pack(side="right")

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, pady=(10, 8))

        self.encode_tab = ttk.Frame(self.notebook, padding=12)
        self.decode_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.encode_tab, text="Encode")
        self.notebook.add(self.decode_tab, text="Decode")
        self.notebook.bind("<<NotebookTabChanged>>", lambda _evt: self._refresh_command_preview())

        self._build_encode_tab(self.encode_tab)
        self._build_decode_tab(self.decode_tab)

        command_frame = ttk.LabelFrame(root, text="Command Preview", padding=10)
        command_frame.pack(fill="x", pady=(0, 8))
        self.command_preview = tk.Text(command_frame, height=3, wrap="word")
        self.command_preview.pack(fill="x", expand=True)
        self.command_preview.configure(state="disabled")

        controls = ttk.Frame(root)
        controls.pack(fill="x", pady=(0, 8))
        self.run_button = ttk.Button(controls, text="Run", command=self._run_current)
        self.run_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop", command=self._stop_running, state="disabled")
        self.stop_button.pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Cassette preset", command=self._apply_cassette_preset).pack(side="left", padx=(8, 0))
        ttk.Button(controls, text="Clear log", command=self._clear_log).pack(side="right")

        log_frame = ttk.LabelFrame(root, text="Output", padding=10)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, wrap="word")
        self.log.pack(fill="both", expand=True, side="left")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        log_scroll.pack(fill="y", side="right")
        self.log.configure(yscrollcommand=log_scroll.set)

        self._bind_refresh_vars()
        self._apply_cassette_preset()

    def _build_encode_tab(self, parent: ttk.Frame) -> None:
        row = 0
        ttk.Label(parent, text="Output WAV").grid(row=row, column=0, sticky="w")
        ttk.Entry(parent, textvariable=self.encode_output, width=50).grid(row=row, column=1, sticky="ew", padx=6)
        ttk.Button(parent, text="Browse", command=self._browse_encode_output).grid(row=row, column=2, sticky="w")
        ttk.Checkbutton(parent, text="Play live instead of writing WAV", variable=self.encode_live,
                        command=self._sync_encode_live).grid(row=row, column=3, sticky="w", padx=(12, 0))

        row += 1
        ttk.Label(parent, text="Input file").grid(row=row, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(parent, textvariable=self.encode_input_file, width=50).grid(row=row, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Button(parent, text="Browse", command=self._browse_encode_input).grid(row=row, column=2, sticky="w", pady=(8, 0))

        row += 1
        ttk.Label(parent, text="Inline data").grid(row=row, column=0, sticky="nw", pady=(8, 0))
        ttk.Entry(parent, textvariable=self.encode_data, width=50).grid(row=row, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0))
        ttk.Label(parent, text="Use input file or inline data").grid(row=row, column=3, sticky="w", padx=(12, 0), pady=(8, 0))

        row += 1
        self._add_numeric_row(parent, row, "Bitrate", self.encode_bitrate, 1, "symbols/s")
        row += 1
        ttk.Label(parent, text="MFSK").grid(row=row, column=0, sticky="w", pady=(8, 0))
        mfsk = ttk.Combobox(parent, textvariable=self.encode_mfsk, values=("2", "4", "8", "16"), width=10, state="readonly")
        mfsk.grid(row=row, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(parent, text="2-FSK is the highest-density safe cassette preset").grid(row=row, column=3, sticky="w", padx=(12, 0), pady=(8, 0))

        row += 1
        ttk.Label(parent, text="Compression").grid(row=row, column=0, sticky="w", pady=(8, 0))
        compress = ttk.Frame(parent)
        compress.grid(row=row, column=1, columnspan=3, sticky="w", padx=6, pady=(8, 0))
        for label, value in (("Auto", "auto"), ("Always", "always"), ("None", "none")):
            ttk.Radiobutton(compress, text=label, value=value, variable=self.encode_compress).pack(side="left", padx=(0, 10))

        row += 1
        self._add_numeric_row(parent, row, "Interleave depth", self.encode_interleave, 1, "1 is backward compatible")
        row += 1
        self._add_numeric_row(parent, row, "Resync interval", self.encode_resync, 0, "0 disables resync markers")

        row += 1
        ttk.Checkbutton(parent, text="Do not clamp bitrate", variable=self.encode_noclamp).grid(row=row, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Checkbutton(parent, text="Overwrite existing output file", variable=self.encode_overwrite).grid(row=row, column=2, columnspan=2, sticky="w", padx=(12, 0), pady=(8, 0))

        parent.columnconfigure(1, weight=1)

    def _build_decode_tab(self, parent: ttk.Frame) -> None:
        row = 0
        ttk.Label(parent, text="Input source").grid(row=row, column=0, sticky="w")
        ttk.Entry(parent, textvariable=self.decode_input_file, width=50).grid(row=row, column=1, sticky="ew", padx=6)
        ttk.Button(parent, text="Browse", command=self._browse_decode_input).grid(row=row, column=2, sticky="w")
        ttk.Checkbutton(parent, text="Live capture (-)", variable=self.decode_live,
                        command=self._sync_decode_live).grid(row=row, column=3, sticky="w", padx=(12, 0))

        row += 1
        ttk.Checkbutton(parent, text="Auto-detect bitrate and tone count", variable=self.decode_autodetect,
                        command=self._sync_decode_auto).grid(row=row, column=1, columnspan=3, sticky="w", padx=6, pady=(8, 0))

        row += 1
        self._add_numeric_row(parent, row, "Bitrate", self.decode_bitrate, 1, "leave blank for auto-detect")
        row += 1
        ttk.Label(parent, text="MFSK").grid(row=row, column=0, sticky="w", pady=(8, 0))
        mfsk = ttk.Combobox(parent, textvariable=self.decode_mfsk, values=("2", "4", "8", "16"), width=10, state="readonly")
        mfsk.grid(row=row, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(parent, text="Leave blank when auto-detect is enabled").grid(row=row, column=3, sticky="w", padx=(12, 0), pady=(8, 0))

        row += 1
        ttk.Checkbutton(parent, text="Auto-interleave search", variable=self.decode_auto_interleave,
                        command=self._sync_decode_interleave).grid(row=row, column=1, sticky="w", padx=6, pady=(8, 0))
        self._add_numeric_row(parent, row, "Interleave depth", self.decode_interleave, 1, "used when auto-interleave is off")

        row += 1
        ttk.Checkbutton(parent, text="Brute-force recovery", variable=self.decode_bruteforce).grid(row=row, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Checkbutton(parent, text="Overwrite decoded output", variable=self.decode_overwrite).grid(row=row, column=2, sticky="w", padx=(12, 0), pady=(8, 0))

        row += 1
        ttk.Label(parent, text="Input device").grid(row=row, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(parent, textvariable=self.decode_input_device, width=50).grid(row=row, column=1, columnspan=2, sticky="ew", padx=6, pady=(8, 0))

        row += 1
        ttk.Label(parent, text="Debug capture").grid(row=row, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(parent, textvariable=self.decode_debug_capture, width=50).grid(row=row, column=1, sticky="ew", padx=6, pady=(8, 0))
        ttk.Button(parent, text="Browse", command=self._browse_debug_capture).grid(row=row, column=2, sticky="w", pady=(8, 0))

        row += 1
        ttk.Checkbutton(parent, text="Live monitor / auto-stop", variable=self.decode_live_monitor).grid(row=row, column=1, sticky="w", padx=6, pady=(8, 0))

        parent.columnconfigure(1, weight=1)

    def _add_numeric_row(self, parent: ttk.Frame, row: int, label: str, var: tk.StringVar, default: int, hint: str) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(parent, textvariable=var, width=12).grid(row=row, column=1, sticky="w", padx=6, pady=(8, 0))
        ttk.Label(parent, text=hint).grid(row=row, column=3, sticky="w", padx=(12, 0), pady=(8, 0))

    def _bind_refresh_vars(self) -> None:
        for var in (
            self.encode_output, self.encode_input_file, self.encode_data, self.encode_live,
            self.encode_bitrate, self.encode_mfsk, self.encode_compress, self.encode_interleave,
            self.encode_resync, self.encode_noclamp, self.encode_overwrite,
            self.decode_input_file, self.decode_live, self.decode_autodetect, self.decode_bitrate,
            self.decode_mfsk, self.decode_auto_interleave, self.decode_interleave,
            self.decode_bruteforce, self.decode_overwrite, self.decode_input_device,
            self.decode_debug_capture, self.decode_live_monitor,
        ):
            if isinstance(var, tk.Variable):
                var.trace_add("write", lambda *_: self._refresh_command_preview())

    def _apply_cassette_preset(self) -> None:
        self.encode_live.set(False)
        self.encode_output.set("output.wav")
        self._encode_previous_output = "output.wav"
        self.encode_input_file.set("")
        self.encode_data.set("")
        self.encode_bitrate.set(str(CASSETTE_BITRATE))
        self.encode_mfsk.set(str(CASSETTE_MFSK))
        self.encode_compress.set("auto")
        self.encode_interleave.set("1")
        self.encode_resync.set("0")
        self.encode_noclamp.set(False)
        self.encode_overwrite.set(False)

        self.decode_live.set(True)
        self.decode_autodetect.set(False)
        self.decode_input_file.set("")
        self._decode_previous_input = ""
        self.decode_bitrate.set(str(CASSETTE_BITRATE))
        self.decode_mfsk.set(str(CASSETTE_MFSK))
        self.decode_auto_interleave.set(False)
        self.decode_interleave.set("1")
        self.decode_bruteforce.set(False)
        self.decode_overwrite.set(False)
        self.decode_input_device.set("")
        self.decode_debug_capture.set("")
        self.decode_live_monitor.set(True)
        self._sync_encode_live()
        self._sync_decode_live()
        self._sync_decode_auto()
        self._sync_decode_interleave()
        self._refresh_command_preview()

    def _sync_encode_live(self) -> None:
        if self.encode_live.get():
            if self.encode_output.get().strip() and self.encode_output.get().strip() != "-":
                self._encode_previous_output = self.encode_output.get().strip()
            self.encode_output.set("-")
        elif self.encode_output.get().strip() == "-":
            self.encode_output.set(self._encode_previous_output or "output.wav")

    def _sync_decode_live(self) -> None:
        if self.decode_live.get():
            if self.decode_input_file.get().strip() and self.decode_input_file.get().strip() != "-":
                self._decode_previous_input = self.decode_input_file.get().strip()
            self.decode_input_file.set("")
            self.decode_live_monitor.set(True)
        elif not self.decode_input_file.get().strip():
            self.decode_input_file.set(self._decode_previous_input)

    def _sync_decode_auto(self) -> None:
        if self.decode_autodetect.get():
            self.decode_bitrate.set("")
            self.decode_mfsk.set("")
        else:
            if not self.decode_bitrate.get().strip():
                self.decode_bitrate.set(str(CASSETTE_BITRATE))
            if not self.decode_mfsk.get().strip():
                self.decode_mfsk.set(str(CASSETTE_MFSK))

    def _sync_decode_interleave(self) -> None:
        if self.decode_auto_interleave.get():
            self.decode_interleave.set("1")

    def _browse_encode_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Select output WAV",
            defaultextension=".wav",
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")],
        )
        if path:
            self._encode_previous_output = path
            self.encode_output.set(path)
            self.encode_live.set(False)
            self._refresh_command_preview()

    def _browse_encode_input(self) -> None:
        path = filedialog.askopenfilename(title="Select input file")
        if path:
            self.encode_input_file.set(path)
            self._refresh_command_preview()

    def _browse_decode_input(self) -> None:
        path = filedialog.askopenfilename(title="Select WAV to decode")
        if path:
            self._decode_previous_input = path
            self.decode_input_file.set(path)
            self.decode_live.set(False)
            self._refresh_command_preview()

    def _browse_debug_capture(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save live capture WAV",
            defaultextension=".wav",
            filetypes=[("WAV files", "*.wav"), ("All files", "*.*")],
        )
        if path:
            self.decode_debug_capture.set(path)
            self._refresh_command_preview()

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="normal")

    def _list_devices(self) -> None:
        command = [sys.executable, str(ASS_CLI), "--list-devices"]
        self._append_log(f"$ {shlex.join(command)}\n")
        threading.Thread(target=self._run_oneoff, args=(command,), daemon=True).start()

    def _run_oneoff(self, command: list[str]) -> None:
        result = subprocess.run(
            command,
            cwd=str(APP_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if result.stdout:
            self.output_queue.put(result.stdout)
        self.output_queue.put(("\0exit", result.returncode))

    def _append_log(self, text: str) -> None:
        self.log.insert("end", text)
        self.log.see("end")

    def _set_command_preview(self, command: list[str]) -> None:
        text = shlex.join(command)
        self.command_preview.configure(state="normal")
        self.command_preview.delete("1.0", "end")
        self.command_preview.insert("1.0", text)
        self.command_preview.configure(state="disabled")

    def _refresh_command_preview(self) -> None:
        try:
            command = self._build_command()
            self._set_command_preview(command)
        except ValueError as exc:
            self._set_command_preview([f"# {exc}"])

    def _build_command(self) -> list[str]:
        mode = self.notebook.tab(self.notebook.select(), "text")
        if mode == "Encode":
            return self._build_encode_command()
        return self._build_decode_command()

    def _build_encode_command(self) -> list[str]:
        output = self.encode_output.get().strip()
        if self.encode_live.get():
            output = "-"
        if not output:
            raise ValueError("Choose an output WAV file or enable live playback.")

        if self.encode_input_file.get().strip():
            source_args = ["--inputfile", self.encode_input_file.get().strip()]
        elif self.encode_data.get().strip():
            source_args = ["--data", self.encode_data.get().strip()]
        else:
            raise ValueError("Provide either an input file or inline data to encode.")

        command = [sys.executable, str(ASS_CLI), "encode", output, *source_args]
        command += ["--bitrate", self._must_int(self.encode_bitrate.get(), "bitrate")]
        command += ["--mfsk", self._must_choice(self.encode_mfsk.get(), (2, 4, 8, 16), "MFSK")]
        command += ["--interleave-depth", self._must_int(self.encode_interleave.get(), "interleave depth")]
        command += ["--resync-interval", self._must_int(self.encode_resync.get(), "resync interval")]
        if self.encode_compress.get() == "always":
            command.append("--alwayscompress")
        elif self.encode_compress.get() == "none":
            command.append("--nocompress")
        else:
            command.append("--autocompress")
        if self.encode_noclamp.get():
            command.append("--noclamp")
        if self.encode_overwrite.get():
            command.append("--overwrite")
        return command

    def _build_decode_command(self) -> list[str]:
        source = "-" if self.decode_live.get() else self.decode_input_file.get().strip()
        if not source:
            raise ValueError("Choose a WAV file or enable live capture.")

        command = [sys.executable, str(ASS_CLI), "decode", source]
        if self.decode_autodetect.get():
            pass
        else:
            bitrate = self.decode_bitrate.get().strip()
            mfsk = self.decode_mfsk.get().strip()
            if bitrate:
                command += ["--bitrate", self._must_int(bitrate, "bitrate")]
            if mfsk:
                command += ["--mfsk", self._must_choice(mfsk, (2, 4, 8, 16), "MFSK")]
        if self.decode_auto_interleave.get():
            command.append("--auto-interleave")
        else:
            command += ["--interleave-depth", self._must_int(self.decode_interleave.get(), "interleave depth")]
        if self.decode_bruteforce.get():
            command.append("--brute-force-recovery")
        if self.decode_overwrite.get():
            command.append("--overwrite")
        if self.decode_input_device.get().strip():
            command += ["--input-device", self.decode_input_device.get().strip()]
        if self.decode_debug_capture.get().strip():
            command += ["--debug-capture", self.decode_debug_capture.get().strip()]
        if self.decode_live.get():
            command.append("--live-monitor" if self.decode_live_monitor.get() else "--no-live-monitor")
        return command

    def _must_int(self, value: str, label: str) -> str:
        try:
            return str(int(value))
        except ValueError as exc:
            raise ValueError(f"{label} must be an integer.") from exc

    def _must_choice(self, value: str, choices: tuple[int, ...], label: str) -> str:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise ValueError(f"{label} must be one of {choices}.") from exc
        if parsed not in choices:
            raise ValueError(f"{label} must be one of {choices}.")
        return str(parsed)

    def _run_current(self) -> None:
        if self.proc is not None:
            messagebox.showinfo("ASS GUI", "A command is already running.")
            return
        try:
            command = self._build_command()
        except ValueError as exc:
            messagebox.showerror("ASS GUI", str(exc))
            return

        self._set_command_preview(command)
        self._append_log(f"$ {shlex.join(command)}\n")
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.proc = subprocess.Popen(
            command,
            cwd=str(APP_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        threading.Thread(target=self._pump_output, daemon=True).start()

    def _pump_output(self) -> None:
        assert self.proc is not None
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.output_queue.put(line)
        rc = self.proc.wait()
        self.output_queue.put(("\0exit", rc))

    def _stop_running(self) -> None:
        if self.proc is None:
            return
        self._append_log("[GUI] Stopping process...\n")
        self.proc.terminate()

    def _drain_output(self) -> None:
        try:
            while True:
                item = self.output_queue.get_nowait()
                if isinstance(item, tuple):
                    _, rc = item
                    self._append_log(f"[GUI] Process exited with code {rc}\n")
                    self.proc = None
                    self.run_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                else:
                    self._append_log(item)
        except queue.Empty:
            pass
        self.after(50, self._drain_output)


def main() -> None:
    app = AssGui()
    app.mainloop()


if __name__ == "__main__":
    main()
