from __future__ import annotations

import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled, DownloadError
except ImportError: 
    yt_dlp = None
    DownloadCancelled = Exception
    DownloadError = Exception


MP4_QUALITIES = ("Best", "1080p", "720p", "480p", "360p")
MP3_QUALITIES = ("320 kbps", "192 kbps", "128 kbps")
URL_RE = re.compile(r"https?://\S+")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

APP = None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YouTube downloader")
        self.resizable(False, False)

        self.queue: queue.Queue = queue.Queue()
        self.cancel_event = threading.Event()
        self.worker: threading.Thread | None = None
        self._spinning = False

        self.mode = tk.StringVar(value="mp4")
        self.quality = tk.StringVar(value="Best")
        self.folder = tk.StringVar(value=str(Path.home() / "Downloads" / "YouTube"))
        self.playlist = tk.BooleanVar(value=False)

        self._build()
        self._center()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Return>", lambda _event: self.start())
        self.bind("<Escape>", lambda _event: self.cancel())
        self.after(80, self._poll)
        self.url_entry.focus_set()

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        try:
            url = normalize_url(self.url_entry.get())
        except ValueError as exc:
            messagebox.showinfo("Link", str(exc), parent=self)
            return
        folder = self.folder.get().strip()
        if not folder:
            messagebox.showinfo("Folder", "Pick a folder first.", parent=self)
            return
        tools = missing_tools()
        if tools:
            messagebox.showerror("Missing", "Install these first:\n\n" + "\n".join(tools), parent=self)
            return

        self.cancel_event = threading.Event()
        self._set_busy(True)
        self.status.configure(text="Starting...")
        self._set_progress(None)

        kind = self.mode.get()
        quality = self.quality.get()
        playlist = bool(self.playlist.get())
        cancel_event = self.cancel_event

        def emit(kind, payload=None):
            self.queue.put((kind, payload))

        def worker():
            run_download(url, kind, quality, folder, playlist, cancel_event, emit)

        self.worker = threading.Thread(target=worker, daemon=True)
        self.worker.start()

    def _handle(self, kind, payload):
        if kind == "log":
            text = str(payload)
            if text.startswith(("Warning", "Saved", "ERROR")):
                self.status.configure(text=text)
        elif kind == "status":
            self.status.configure(text=str(payload))
        elif kind == "progress":
            self._set_progress(payload.get("percent"))
            text = payload.get("text")
            if text:
                self.status.configure(text=text)
        elif kind == "done":
            self._set_busy(False)
            state = payload.get("state")
            message = payload.get("message") or ""
            if self._spinning:
                self.progress.stop()
                self._spinning = False
            self.progress.configure(mode="determinate", maximum=100)
            if state == "ok":
                self.progress.configure(value=100)
            else:
                self.progress.configure(value=0)
            self.status.configure(text=message)

    def _build(self):
        frm = ttk.Frame(self, padding=10)
        frm.grid(sticky="nsew")
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Link").grid(row=0, column=0, sticky="w", pady=3)
        self.url_entry = ttk.Entry(frm, width=46)
        self.url_entry.grid(row=0, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(frm, text="Paste", command=self.paste).grid(row=0, column=2, pady=3)

        ttk.Label(frm, text="Format").grid(row=1, column=0, sticky="w", pady=3)
        fmt = ttk.Frame(frm)
        fmt.grid(row=1, column=1, columnspan=2, sticky="w", pady=3)
        ttk.Radiobutton(fmt, text="MP4", value="mp4", variable=self.mode, command=lambda: self._set_mode("mp4")).pack(side="left")
        ttk.Radiobutton(fmt, text="MP3", value="mp3", variable=self.mode, command=lambda: self._set_mode("mp3")).pack(side="left", padx=(8, 0))
        self.quality_caption = ttk.Label(fmt, text="Quality")
        self.quality_caption.pack(side="left", padx=(14, 4))
        self.quality_box = ttk.Combobox(fmt, textvariable=self.quality, values=MP4_QUALITIES, state="readonly", width=12)
        self.quality_box.pack(side="left")

        ttk.Label(frm, text="Save to").grid(row=2, column=0, sticky="w", pady=3)
        ttk.Entry(frm, textvariable=self.folder).grid(row=2, column=1, sticky="ew", padx=6, pady=3)
        ttk.Button(frm, text="Browse", command=self.browse).grid(row=2, column=2, pady=3)

        ttk.Checkbutton(frm, text="Whole playlist", variable=self.playlist).grid(row=3, column=1, sticky="w", padx=6, pady=3)

        buttons = ttk.Frame(frm)
        buttons.grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 3))
        self.action = ttk.Button(buttons, text="Download", command=self.start)
        self.action.pack(side="left")
        ttk.Button(buttons, text="Open folder", command=self.open_folder).pack(side="left", padx=6)

        self.progress = ttk.Progressbar(frm, maximum=100)
        self.progress.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(6, 3))

        self.status = ttk.Label(frm, text="Ready", wraplength=420)
        self.status.grid(row=6, column=0, columnspan=3, sticky="w", pady=(2, 0))

        missing = missing_tools()
        if missing:
            self.status.configure(text="Missing: " + ", ".join(missing))
        else:
            hint = runtime_hint()
            if hint:
                self.status.configure(text=hint)

    def paste(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            messagebox.showinfo("Clipboard", "Clipboard is empty.", parent=self)
            return
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, text.strip())
        self.url_entry.focus_set()

    def _poll(self):
        try:
            while True:
                item = self.queue.get_nowait()
                try:
                    kind, payload = item
                    self._handle(kind, payload)
                except Exception as exc:
                    self.status.configure(text=f"Error: {exc}")
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(80, self._poll)

    def cancel(self):
        if self.worker and self.worker.is_alive():
            self.cancel_event.set()
            self.status.configure(text="Cancelling...")

    def browse(self):
        current = self.folder.get().strip() or str(Path.home())
        chosen = filedialog.askdirectory(initialdir=current if os.path.isdir(current) else str(Path.home()))
        if chosen:
            self.folder.set(chosen)

    def _set_mode(self, mode):
        self.mode.set(mode)
        values = MP3_QUALITIES if mode == "mp3" else MP4_QUALITIES
        self.quality_box.configure(values=values)
        if self.quality.get() not in values:
            self.quality.set("192 kbps" if mode == "mp3" else "Best")
        self.quality_caption.configure(text="Bitrate" if mode == "mp3" else "Quality")

    def open_folder(self):
        folder = self.folder.get().strip()
        if not folder:
            return
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)

    def _set_progress(self, percent):
        if percent is None:
            if not self._spinning:
                self.progress.configure(mode="indeterminate")
                self.progress.start(12)
                self._spinning = True
            return
        if self._spinning:
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=100)
            self._spinning = False
        self.progress.configure(value=max(0, min(100, percent)))

    def _center(self):
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = max(0, (self.winfo_screenwidth() - width) // 2)
        y = max(0, (self.winfo_screenheight() - height) // 2)
        self.geometry(f"+{x}+{y}")

    def _on_close(self):
        self.cancel_event.set()
        self.destroy()

    def _set_busy(self, busy):
        if busy:
            self.action.configure(text="Cancel", command=self.cancel)
        else:
            self.action.configure(text="Download", command=self.start)


def run_download(url, kind, quality, folder, playlist, cancel_event, emit):
    if yt_dlp is None:
        emit("done", {"state": "error", "message": "yt-dlp is not installed."})
        return
    if shutil.which("ffmpeg") is None:
        emit("done", {"state": "error", "message": "ffmpeg is not installed."})
        return

    save_dir = folder
    os.makedirs(save_dir, exist_ok=True)
    saved: list[str] = []
    last_ui = [0.0]

    def emit_progress(percent, text):
        emit("progress", {"percent": percent, "text": text})

    def progress_hook(data):
        if cancel_event.is_set():
            raise DownloadCancelled("Cancelled")
        status = data.get("status")
        if status == "downloading":
            now = time.monotonic()
            if now - last_ui[0] < 0.15:
                return
            last_ui[0] = now
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            done = data.get("downloaded_bytes") or 0
            percent = (done / total * 100) if total else None
            text = progress_label(data, percent)
            emit_progress(percent, text)
            emit("status", text)
        elif status == "finished":
            emit("status", "Processing...")
            emit_progress(None, "Processing...")

    def post_hook(data):
        if cancel_event.is_set():
            raise DownloadCancelled("Cancelled")
        if data.get("status") != "started":
            return
        label = postprocess_label(data.get("postprocessor") or "")
        emit("status", label)
        emit_progress(None, label)

    def remember(path):
        if path and path not in saved:
            saved.append(path)
            emit("log", f"Saved {path}")

    postprocessors = []
    if kind == "mp3":
        bitrate = quality.split()[0]
        postprocessors.append(
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": bitrate,
            }
        )

    opts = {
        "format": format_selector(kind, quality),
        "outtmpl": {"default": "%(title).180B [%(id)s].%(ext)s"},
        "paths": {"home": save_dir},
        "windowsfilenames": True,
        "noplaylist": not playlist,
        "quiet": True,
        "noprogress": True,
        "color": "never",
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "socket_timeout": 30,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [post_hook],
        "post_hooks": [remember],
        "postprocessors": postprocessors,
        "logger": _Logger(emit),
    }
    if kind == "mp4":
        opts["merge_output_format"] = "mp4"
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        opts["ffmpeg_location"] = os.path.dirname(ffmpeg)
    runtimes = javascript_runtimes()
    if runtimes:
        opts["js_runtimes"] = runtimes

    emit("status", "Fetching video info...")
    emit_progress(None, "Fetching video info...")
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            code = ydl.download([url])
    except DownloadCancelled:
        emit("done", {"state": "cancel", "message": "Cancelled."})
        return
    except DownloadError as exc:
        message = short_error(exc)
        emit("log", message)
        emit("done", {"state": "error", "message": message})
        return
    except Exception as exc:
        message = short_error(exc)
        emit("log", message)
        emit("done", {"state": "error", "message": message})
        return

    if cancel_event.is_set():
        emit("done", {"state": "cancel", "message": "Cancelled."})
        return
    if code not in (0, None):
        emit("done", {"state": "error", "message": "Download failed."})
        return
    if len(saved) == 1:
        message = f"Saved {os.path.basename(saved[0])}"
    elif saved:
        message = f"Saved {len(saved)} files"
    else:
        message = "Download finished."
    emit("done", {"state": "ok", "message": message})


def missing_tools() -> list[str]:
    missing = []
    if yt_dlp is None:
        missing.append("yt-dlp  (pip install -r requirements.txt)")
    if shutil.which("ffmpeg") is None:
        missing.append("ffmpeg  (winget install Gyan.FFmpeg)")
    return missing


def format_selector(kind: str, quality: str) -> str:
    if kind == "mp3":
        return "bestaudio/best"
    heights = {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
    height = heights.get(quality)
    if height is None:
        return (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[ext=mp4]+bestaudio/"
            "best[ext=mp4]/best"
        )
    limit = f"[height<={height}]"
    return (
        f"bestvideo{limit}[ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo{limit}[ext=mp4]+bestaudio/"
        f"best{limit}[ext=mp4]/"
        f"best{limit}/best"
    )


def human_eta(seconds: float) -> str:
    total = max(0, int(seconds))
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def progress_label(data: dict, percent: float | None) -> str:
    info = data.get("info_dict") or {}
    vcodec = info.get("vcodec")
    acodec = info.get("acodec")
    video = vcodec not in (None, "none")
    audio = acodec not in (None, "none")
    if video and not audio:
        what = "video"
    elif audio and not video:
        what = "audio"
    else:
        what = "file"

    bits = []
    if percent is not None:
        bits.append(f"{percent:.0f}%")
    speed = data.get("speed")
    if speed:
        bits.append(human_rate(speed))
    eta = data.get("eta")
    if eta is not None:
        bits.append(f"{human_eta(eta)} left")
    detail = " · ".join(bits)
    if detail:
        return f"Downloading {what} · {detail}"
    return f"Downloading {what}"


def javascript_runtimes() -> dict:
    """Runtimes new enough for yt-dlp's YouTube solver."""
    home = Path.home()
    candidates = {
        "deno": (
            (2, 3, 0),
            ["--version"],
            r"^deno (\S+)",
            [shutil.which("deno"), str(home / ".deno" / "bin" / "deno.exe"), str(home / ".deno" / "bin" / "deno")],
        ),
        "node": (
            (22, 0, 0),
            ["--version"],
            r"^v(\S+)",
            [shutil.which("node")],
        ),
        "bun": (
            (1, 2, 11),
            ["--version"],
            r"^(\S+)",
            [shutil.which("bun")],
        ),
    }
    found = {}
    for name, (minimum, args, pattern, paths) in candidates.items():
        for path in paths:
            if not path or not os.path.isfile(path):
                continue
            version = _runtime_version(path, args, pattern)
            if version and version >= minimum:
                found[name] = {"path": path}
                break
    return found


class _Logger:
    def __init__(self, emit):
        self._emit = emit

    def debug(self, message):
        if message is None:
            return
        raw = str(message)
        text = raw.strip()
        if not text or text.startswith("[debug]") or "\r" in raw:
            return
        self._emit("log", text)

    def info(self, message):
        self.debug(message)

    def warning(self, message):
        text = "" if message is None else str(message).strip()
        if text:
            self._emit("log", f"Warning: {text}")

    def error(self, message):
        # The exception handler shows the error onec.
        return


def normalize_url(raw: str) -> str:
    text = raw.strip()
    match = URL_RE.search(text)
    if match:
        return match.group(0).rstrip(").,>\"'")
    if text.startswith("www."):
        return "https://" + text.split()[0]
    raise ValueError("Paste a video link first.")


def short_error(exc: BaseException) -> str:
    text = ANSI_RE.sub("", str(exc)).strip()
    text = re.sub(r"^ERROR:\s*", "", text)
    line = text.splitlines()[0] if text else "Download failed."
    if len(line) > 180:
        return line[:177] + "..."
    return line


def postprocess_label(name: str) -> str:
    if "ExtractAudio" in name:
        return "Converting to MP3..."
    if "Merge" in name:
        return "Merging video and audio..."
    return "Finishing..."

def runtime_hint() -> str | None:
    if javascript_runtimes():
        return None
    return "Install Deno or Node.js 22+ or Bun 1.2.11+ for YouTube support."


def human_rate(speed: float) -> str:
    value = float(speed)
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if value < 1024 or unit == "GB/s":
            number = f"{value:.0f}" if unit == "B/s" else f"{value:.1f}"
            return f"{number} {unit}"
        value /= 1024
    return ""


def _version_tuple(text: str) -> tuple[int, ...]:
    parts = []
    for piece in text.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _runtime_version(path: str, args: list[str], pattern: str) -> tuple[int, ...] | None:
    try:
        completed = subprocess.run(
            [path, *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = f"{completed.stdout}\n{completed.stderr}"
    match = re.search(pattern, output, re.MULTILINE)
    if not match:
        return None
    return _version_tuple(match.group(1))


def _enable_dpi():
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            return


def main():
    global APP
    _enable_dpi()
    APP = App()
    APP.mainloop()


if __name__ == "__main__":
    main()
