"""Three-panel Tkinter console: search options | ranked results | preview."""
from __future__ import annotations

import io
import os
import queue
import sys
import threading
import tkinter as tk
import webbrowser
from datetime import datetime, timezone
from tkinter import font as tkfont, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from .health import compute_health
from .models import FILE_TYPES, TorrentResult, human_size
from .providers import PROVIDERS, Provider, http_get

try:  # Optional: lets us show JPEG cover art (Tk alone only does PNG/GIF).
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

RATING_COLORS = {
    "Excellent": "#cdeccd",
    "Good": "#e4f4cf",
    "Fair": "#fbf1c7",
    "Poor": "#fbdcc4",
    "Dead": "#f3cccc",
}

# (id, heading, width, anchor, stretch)
COLUMNS = [
    ("health", "Health", 64, "e", False),
    ("rating", "Rating", 76, "w", False),
    ("name", "Name", 420, "w", True),
    ("size", "Size", 80, "e", False),
    ("seeders", "Seeds", 64, "e", False),
    ("leechers", "Leech", 64, "e", False),
    ("type", "Type", 90, "w", False),
    ("source", "Source", 150, "w", False),
    ("added", "Added", 90, "w", False),
]

SORT_KEYS = {
    "health": lambda r: r.health,
    "rating": lambda r: r.health,
    "name": lambda r: r.name.lower(),
    "size": lambda r: r.size,
    "seeders": lambda r: r.seeders,
    "leechers": lambda r: r.leechers,
    "type": lambda r: r.file_type,
    "source": lambda r: source_label(r).lower(),
    "added": lambda r: r.added.timestamp() if r.added else 0,
}


def source_label(r: TorrentResult) -> str:
    """Compact site list for the table; the preview panel shows the full detail."""
    return ", ".join(sorted({s.removesuffix(" (via Knaben)") for s in r.sources}))


def open_external(target: str) -> None:
    if sys.platform.startswith("win"):
        os.startfile(target)  # hands magnet: links to the registered torrent client
    else:
        webbrowser.open(target)


class TorrentFinderApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Torrent Health Finder")
        self.geometry("1500x860")
        self.minsize(1100, 600)

        self.results: dict[str, TorrentResult] = {}
        self.provider_by_source: dict[str, Provider] = {p.name: p for p in PROVIDERS}
        self.messages: queue.Queue = queue.Queue()
        self.search_id = 0
        self.pending = 0
        self.site_status: dict[str, str] = {}
        self.sort_col, self.sort_desc = "health", True
        self.selected: TorrentResult | None = None
        self.details_cache: dict[str, str] = {}
        self.image_cache: dict[str, object] = {}

        self._setup_style()
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)
        paned.add(self._build_search_panel(paned), weight=0)
        paned.add(self._build_results_panel(paned), weight=3)
        paned.add(self._build_preview_panel(paned), weight=2)

        self.after(100, self._poll_messages)
        self.query_entry.focus_set()

    # ------------------------------------------------------------------ layout
    def _setup_style(self):
        style = ttk.Style(self)
        if "clam" in style.theme_names():
            style.theme_use("clam")  # honours Treeview row colours on every platform
        style.configure("Header.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Title.TLabel", font=("Segoe UI", 12, "bold"))
        style.configure("Muted.TLabel", foreground="#666666")
        linespace = tkfont.nametofont("TkDefaultFont").metrics("linespace")
        style.configure("Treeview", rowheight=linespace + 8)  # scales with DPI
        style.configure("Penalty.TLabel", foreground="#b02a2a")

    def _build_search_panel(self, parent):
        frame = ttk.Frame(parent, padding=10, width=270)

        ttk.Label(frame, text="Search", style="Header.TLabel").pack(anchor="w")
        self.query_var = tk.StringVar()
        self.query_entry = ttk.Entry(frame, textvariable=self.query_var, width=32)
        self.query_entry.pack(fill="x", pady=(4, 6))
        self.query_entry.bind("<Return>", lambda _e: self.start_search())
        self.search_btn = ttk.Button(frame, text="Search", command=self.start_search)
        self.search_btn.pack(fill="x")

        types_box = ttk.LabelFrame(frame, text="File types", padding=8)
        types_box.pack(fill="x", pady=(12, 0))
        self.type_vars = {t: tk.BooleanVar(value=True) for t in FILE_TYPES}
        self.all_types_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(types_box, text="All types", variable=self.all_types_var,
                        command=self._toggle_all_types).pack(anchor="w")
        ttk.Separator(types_box).pack(fill="x", pady=3)
        for ftype, var in self.type_vars.items():
            ttk.Checkbutton(types_box, text=ftype, variable=var,
                            command=self._on_type_toggle).pack(anchor="w")

        sites_box = ttk.LabelFrame(frame, text="Sites", padding=8)
        sites_box.pack(fill="x", pady=(12, 0))
        self.site_vars = {p.key: tk.BooleanVar(value=True) for p in PROVIDERS}
        for p in PROVIDERS:
            ttk.Checkbutton(sites_box, text=p.name, variable=self.site_vars[p.key]).pack(anchor="w")
            ttk.Label(sites_box, text="    " + p.note, style="Muted.TLabel").pack(anchor="w")

        opts = ttk.LabelFrame(frame, text="Options", padding=8)
        opts.pack(fill="x", pady=(12, 0))
        opts.columnconfigure(1, weight=1)
        ttk.Label(opts, text="Min seeders").grid(row=0, column=0, sticky="w")
        self.min_seeders_var = tk.IntVar(value=1)
        ttk.Spinbox(opts, from_=0, to=100000, width=7, textvariable=self.min_seeders_var,
                    command=self.refresh_table).grid(row=0, column=1, sticky="e", pady=2)
        ttk.Label(opts, text="Results per site").grid(row=1, column=0, sticky="w")
        self.limit_var = tk.IntVar(value=100)
        ttk.Spinbox(opts, from_=10, to=300, increment=10, width=7,
                    textvariable=self.limit_var).grid(row=1, column=1, sticky="e", pady=2)
        self.min_seeders_var.trace_add("write", lambda *_: self.refresh_table())

        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(16, 4))
        self.status_var = tk.StringVar(value="Enter a search term and press Search.")
        ttk.Label(frame, textvariable=self.status_var, wraplength=250,
                  justify="left").pack(anchor="w", fill="x")

        ttk.Label(frame, text="Only download content you have the right to.",
                  style="Muted.TLabel", wraplength=250).pack(side="bottom", anchor="w")
        return frame

    def _build_results_panel(self, parent):
        frame = ttk.Frame(parent, padding=(6, 10))
        bar = ttk.Frame(frame)
        bar.pack(fill="x")
        ttk.Label(bar, text="Results", style="Header.TLabel").pack(side="left")
        self.count_var = tk.StringVar(value="")
        ttk.Label(bar, textvariable=self.count_var, style="Muted.TLabel").pack(side="left", padx=8)
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_: self.refresh_table())
        ttk.Entry(bar, textvariable=self.filter_var, width=24).pack(side="right")
        ttk.Label(bar, text="Filter:").pack(side="right", padx=4)

        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True, pady=(6, 0))
        self.tree = ttk.Treeview(table, columns=[c[0] for c in COLUMNS], show="headings",
                                 selectmode="browse")
        for col, heading, width, anchor, stretch in COLUMNS:
            self.tree.heading(col, text=heading, anchor=anchor,
                              command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=width, anchor=anchor, stretch=stretch, minwidth=40)
        for rating, color in RATING_COLORS.items():
            self.tree.tag_configure(rating, background=color)
        vsb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1)
        table.columnconfigure(0, weight=1)

        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda _e: self.open_magnet())
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.menu = tk.Menu(self, tearoff=False)
        self.menu.add_command(label="Copy magnet link", command=self.copy_magnet)
        self.menu.add_command(label="Open in torrent client", command=self.open_magnet)
        self.menu.add_command(label="Open details page", command=self.open_details)
        self._update_headings()
        return frame

    def _build_preview_panel(self, parent):
        frame = ttk.Frame(parent, padding=10)
        ttk.Label(frame, text="Preview", style="Header.TLabel").pack(anchor="w")

        self.title_var = tk.StringVar(value="Select a torrent to see its details.")
        self.title_label = ttk.Label(frame, textvariable=self.title_var, style="Title.TLabel",
                                     wraplength=420, justify="left")
        self.title_label.pack(anchor="w", fill="x", pady=(4, 6))
        frame.bind("<Configure>", lambda e: self.title_label.configure(wraplength=max(200, e.width - 30)))

        top = ttk.Frame(frame)
        top.pack(fill="x")
        self.image_label = ttk.Label(top)
        self.image_label.pack(side="left", anchor="n", padx=(0, 10))
        info = ttk.Frame(top)
        info.pack(side="left", fill="x", expand=True, anchor="n")

        score_row = ttk.Frame(info)
        score_row.pack(fill="x")
        self.score_var = tk.StringVar(value="")
        self.score_label = tk.Label(score_row, textvariable=self.score_var,
                                    font=("Segoe UI", 16, "bold"), padx=8, pady=2)
        self.score_label.pack(side="left")
        self.score_bar = ttk.Progressbar(info, maximum=100)
        self.score_bar.pack(fill="x", pady=(4, 8))

        grid = ttk.Frame(info)
        grid.pack(fill="x")
        grid.columnconfigure(1, weight=1)
        self.detail_vars: dict[str, tk.StringVar] = {}
        for i, label in enumerate(["Size", "Seeders", "Leechers", "Type", "Category", "Uploader",
                                   "Added", "Files", "Downloads", "Found on", "Info hash"]):
            ttk.Label(grid, text=label + ":", style="Muted.TLabel").grid(row=i, column=0, sticky="nw", padx=(0, 6))
            var = tk.StringVar()
            ttk.Label(grid, textvariable=var, wraplength=320, justify="left").grid(row=i, column=1, sticky="w")
            self.detail_vars[label] = var

        magnet_box = ttk.LabelFrame(frame, text="Magnet link", padding=6)
        magnet_box.pack(side="bottom", fill="x", pady=(8, 0))
        self.magnet_text = tk.Text(magnet_box, height=4, wrap="char", font=("Consolas", 9),
                                   relief="flat", background="#f6f6f6")
        self.magnet_text.pack(fill="x")
        self.magnet_text.configure(state="disabled")
        btns = ttk.Frame(magnet_box)
        btns.pack(fill="x", pady=(6, 0))
        ttk.Button(btns, text="Copy magnet", command=self.copy_magnet).pack(side="left")
        ttk.Button(btns, text="Open in torrent client", command=self.open_magnet).pack(side="left", padx=6)
        ttk.Button(btns, text="Details page", command=self.open_details).pack(side="left")

        notebook = ttk.Notebook(frame)
        notebook.pack(fill="both", expand=True, pady=(10, 0))
        self.health_tab = ttk.Frame(notebook, padding=8)
        notebook.add(self.health_tab, text="Health breakdown")
        self.details_text = ScrolledText(notebook, wrap="word", font=("Consolas", 9), height=10)
        self.details_text.configure(state="disabled")
        notebook.add(self.details_text, text="Files / Description")
        return frame

    # ----------------------------------------------------------------- search
    def _toggle_all_types(self):
        for var in self.type_vars.values():
            var.set(self.all_types_var.get())
        self.refresh_table()

    def _on_type_toggle(self):
        self.all_types_var.set(all(v.get() for v in self.type_vars.values()))
        self.refresh_table()

    def _selected_types(self) -> list[str]:
        return [t for t, v in self.type_vars.items() if v.get()]

    def start_search(self):
        query = self.query_var.get().strip()
        if not query:
            return
        types = self._selected_types()
        providers = [p for p in PROVIDERS if self.site_vars[p.key].get()]
        if not types or not providers:
            messagebox.showwarning("Nothing to search", "Select at least one file type and one site.")
            return
        try:
            limit = max(10, min(300, int(self.limit_var.get())))
        except (tk.TclError, ValueError):
            limit = 100

        self.search_id += 1
        self.results.clear()
        self.details_cache.clear()
        self.pending = len(providers)
        self.site_status = {p.name: "searching..." for p in providers}
        self._show_preview(None)
        self.refresh_table()
        self.progress.start(12)
        self._update_status()

        for p in providers:
            threading.Thread(target=self._run_provider, daemon=True,
                             args=(self.search_id, p, query, types, limit)).start()

    def _run_provider(self, sid, provider, query, types, limit):
        try:
            found = provider.search(query, types, limit)
            self.messages.put(("results", sid, provider.name, found, None))
        except Exception as exc:  # noqa: BLE001 - surface any site failure in the status area
            self.messages.put(("results", sid, provider.name, [], exc))

    def _poll_messages(self):
        try:
            while True:
                self._handle_message(self.messages.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._poll_messages)

    def _handle_message(self, msg):
        kind = msg[0]
        if kind == "results":
            _, sid, site, found, error = msg
            if sid != self.search_id:
                return  # stale results from a previous search
            for r in found:
                if not r.info_hash:
                    continue
                existing = self.results.get(r.info_hash)
                if existing:
                    existing.merge(r)
                else:
                    self.results[r.info_hash] = r
            now = datetime.now(timezone.utc)
            for r in self.results.values():
                compute_health(r, now)
            self.site_status[site] = f"error - {error}" if error else f"{len(found)} results"
            self.pending -= 1
            if self.pending <= 0:
                self.progress.stop()
            self._update_status()
            self.refresh_table()
        elif kind == "details":
            _, info_hash, text = msg
            self.details_cache[info_hash] = text
            if self.selected and self.selected.info_hash == info_hash:
                self._set_details_text(self._details_for(self.selected))
        elif kind == "image":
            _, url, data = msg
            self.image_cache[url] = self._make_photo(data)
            if self.selected and self.selected.image_url == url:
                self._set_image(url)

    def _update_status(self):
        lines = [f"{site}: {state}" for site, state in self.site_status.items()]
        head = "Searching..." if self.pending > 0 else f"Done - {len(self.results)} unique torrents."
        self.status_var.set("\n".join([head, *lines]))

    # ------------------------------------------------------------------ table
    def _visible_results(self) -> list[TorrentResult]:
        types = set(self._selected_types())
        try:
            min_seeds = int(self.min_seeders_var.get())
        except (tk.TclError, ValueError):
            min_seeds = 0
        needle = self.filter_var.get().strip().lower()
        rows = [r for r in self.results.values()
                if r.file_type in types and r.seeders >= min_seeds
                and (not needle or needle in r.name.lower())]
        rows.sort(key=SORT_KEYS[self.sort_col], reverse=self.sort_desc)
        return rows

    def refresh_table(self):
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        rows = self._visible_results()
        for r in rows:
            self.tree.insert("", "end", iid=r.info_hash, tags=(r.rating,), values=(
                f"{r.health:.0f}", r.rating, r.name, human_size(r.size), f"{r.seeders:,}",
                f"{r.leechers:,}", r.file_type, source_label(r),
                r.added.strftime("%Y-%m-%d") if r.added else "",
            ))
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
            self.tree.see(selected[0])
        hidden = len(self.results) - len(rows)
        self.count_var.set(f"{len(rows)} shown" + (f", {hidden} hidden by filters" if hidden else ""))

    def _sort_by(self, col):
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            # Numbers read best biggest-first; text reads best A-Z.
            self.sort_col, self.sort_desc = col, col not in ("name", "type", "source")
        self._update_headings()
        self.refresh_table()

    def _update_headings(self):
        for col, heading, *_ in COLUMNS:
            arrow = (" ▼" if self.sort_desc else " ▲") if col == self.sort_col else ""
            self.tree.heading(col, text=heading + arrow)

    def _show_context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            self.tree.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)

    # ---------------------------------------------------------------- preview
    def _on_select(self, _event=None):
        sel = self.tree.selection()
        self._show_preview(self.results.get(sel[0]) if sel else None)

    def _show_preview(self, r: TorrentResult | None):
        self.selected = r
        for child in self.health_tab.winfo_children():
            child.destroy()
        if r is None:
            self.title_var.set("Select a torrent to see its details.")
            self.score_var.set("")
            self.score_label.configure(background=self.cget("background"))
            self.score_bar["value"] = 0
            for var in self.detail_vars.values():
                var.set("")
            self._set_magnet("")
            self._set_details_text("")
            self.image_label.configure(image="")
            return

        self.title_var.set(r.name)
        self.score_var.set(f"{r.health:.0f} / 100  {r.rating}")
        self.score_label.configure(background=RATING_COLORS.get(r.rating, "#ffffff"))
        self.score_bar["value"] = r.health
        ratio = f"{r.seeders / r.leechers:.1f} : 1" if r.leechers else "no leechers"
        values = {
            "Size": human_size(r.size),
            "Seeders": f"{r.seeders:,}",
            "Leechers": f"{r.leechers:,}   (ratio {ratio})",
            "Type": r.file_type,
            "Category": r.category or "-",
            "Uploader": " ".join(x for x in (r.uploader, f"[{r.uploader_status}]" if r.uploader_status else "") if x) or "-",
            "Added": r.added.strftime("%Y-%m-%d %H:%M UTC") if r.added else "-",
            "Files": str(r.num_files) if r.num_files else "-",
            "Downloads": f"{r.downloads:,}" if r.downloads else "-",
            "Found on": ", ".join(sorted(r.sources)),
            "Info hash": r.info_hash,
        }
        for key, var in self.detail_vars.items():
            var.set(values[key])
        self._set_magnet(r.magnet)
        self._render_breakdown(r)
        self._set_details_text(self._details_for(r))
        self._request_details(r)
        self._request_image(r)

    def _render_breakdown(self, r: TorrentResult):
        tab = self.health_tab
        tab.columnconfigure(1, weight=1)
        row = 0
        for label, pts, max_pts, note in r.breakdown:
            ttk.Label(tab, text=label).grid(row=row, column=0, sticky="w", padx=(0, 8))
            bar = ttk.Progressbar(tab, maximum=max_pts, value=pts, length=120)
            bar.grid(row=row, column=1, sticky="ew", pady=2)
            ttk.Label(tab, text=f"{pts:4.1f} / {max_pts:g}").grid(row=row, column=2, sticky="e", padx=6)
            ttk.Label(tab, text=note, style="Muted.TLabel").grid(row=row, column=3, sticky="w")
            row += 1
        for label, pts in r.penalties:
            ttk.Label(tab, text=f"-{pts:g}  {label}", style="Penalty.TLabel").grid(
                row=row, column=0, columnspan=4, sticky="w", pady=(4, 0))
            row += 1
        if r.seeders == 0:
            ttk.Label(tab, text="No seeders - this torrent cannot currently complete (score capped at 5).",
                      style="Penalty.TLabel").grid(row=row, column=0, columnspan=4, sticky="w", pady=(4, 0))

    def _details_for(self, r: TorrentResult) -> str:
        parts = [r.description] if r.description else []
        if r.info_hash in self.details_cache:
            parts.append(self.details_cache[r.info_hash])
        elif self._details_provider(r):
            parts.append("Loading file list...")
        return "\n\n".join(parts) or "No description available for this torrent."

    def _details_provider(self, r: TorrentResult) -> Provider | None:
        for source in r.sources:
            p = self.provider_by_source.get(source)
            if p and p.has_details:
                return p
        if "tpb_id" in r.meta:
            return self.provider_by_source["The Pirate Bay"]
        return None

    def _request_details(self, r: TorrentResult):
        provider = self._details_provider(r)
        if not provider or r.info_hash in self.details_cache:
            return
        self.details_cache[r.info_hash] = "Loading file list..."

        def work():
            try:
                text = provider.fetch_details(r)
            except Exception as exc:  # noqa: BLE001
                text = f"(could not load details: {exc})"
            self.messages.put(("details", r.info_hash, text))
        threading.Thread(target=work, daemon=True).start()

    def _request_image(self, r: TorrentResult):
        self.image_label.configure(image="")
        if not (HAS_PIL and r.image_url):
            return
        if r.image_url in self.image_cache:
            self._set_image(r.image_url)
            return

        def work():
            try:
                data = http_get(r.image_url)
            except Exception:  # noqa: BLE001 - a missing poster is not worth an error
                data = None
            self.messages.put(("image", r.image_url, data))
        threading.Thread(target=work, daemon=True).start()

    def _make_photo(self, data):
        if not data:
            return None
        try:
            img = Image.open(io.BytesIO(data))
            img.thumbnail((150, 225))
            return ImageTk.PhotoImage(img)
        except Exception:  # noqa: BLE001
            return None

    def _set_image(self, url):
        photo = self.image_cache.get(url)
        self.image_label.configure(image=photo or "")
        self.image_label.image = photo

    def _set_magnet(self, magnet: str):
        self.magnet_text.configure(state="normal")
        self.magnet_text.delete("1.0", "end")
        self.magnet_text.insert("1.0", magnet)
        self.magnet_text.configure(state="disabled")

    def _set_details_text(self, text: str):
        self.details_text.configure(state="normal")
        self.details_text.delete("1.0", "end")
        self.details_text.insert("1.0", text)
        self.details_text.configure(state="disabled")

    # ---------------------------------------------------------------- actions
    def copy_magnet(self):
        if not self.selected:
            return
        self.clipboard_clear()
        self.clipboard_append(self.selected.magnet)
        self.update()  # keep clipboard contents after the app closes
        self.status_var.set("Magnet link copied to clipboard.")

    def open_magnet(self):
        if not self.selected:
            return
        try:
            open_external(self.selected.magnet)
        except OSError as exc:
            messagebox.showerror("No torrent client",
                                 f"Could not open the magnet link. Is a torrent client installed?\n\n{exc}")

    def open_details(self):
        if self.selected and self.selected.details_url:
            webbrowser.open(self.selected.details_url)


def main():
    if sys.platform.startswith("win"):
        try:  # crisp text on scaled (high-DPI) displays instead of a blurry bitmap stretch
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    TorrentFinderApp().mainloop()
