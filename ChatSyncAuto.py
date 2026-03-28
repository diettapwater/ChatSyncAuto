from __future__ import annotations
import json, os, re, time, hashlib, queue, sys, threading, urllib.request, urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
import tkinter as tk
from tkinter import messagebox, filedialog, simpledialog
import customtkinter as ctk
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# --- Path Detection ---
def _is_probably_bannerlord_root(p: Path) -> bool: return (p / "Bannerlord.exe").exists() or ((p / "bin").is_dir() and (p / "Modules").is_dir())
def _walk_up_to_root(start: Path, max_up: int = 10) -> Optional[Path]:
    cur = start
    for _ in range(max_up + 1):
        if _is_probably_bannerlord_root(cur): return cur
        if cur.parent == cur: break
        cur = cur.parent
    return None
def _try_modules(root: Path) -> Optional[Path]:
    cand = root / "Modules" / "AIInfluence" / "save_data"
    return cand if cand.is_dir() else None
def _steam_libraryfolders(steam_root: Path) -> List[Path]:
    vdf = steam_root / "steamapps" / "libraryfolders.vdf"
    libs: List[Path] = []
    if vdf.exists():
        try:
            txt = vdf.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r"\"path\"\s*\"([^\"]+)\"", txt): libs.append(Path(m.group(1).replace("\\\\", "\\")))
        except Exception: pass
    libs.append(steam_root)
    uniq, seen = [], set()
    for p in libs:
        rp = p.resolve() if p.exists() else p
        if rp not in seen and rp.exists():
            seen.add(rp)
            uniq.append(rp)
    return uniq
def _try_workshop(lib_root: Path) -> Optional[Path]:
    ws = lib_root / "steamapps" / "workshop" / "content" / "261550"
    if not ws.is_dir(): return None
    try:
        for moddir in ws.iterdir():
            cand = moddir / "Modules" / "AIInfluence" / "save_data"
            if cand.is_dir(): return cand
    except Exception: pass
    return None
def _try_gamepass() -> Optional[Path]:
    cand = Path("C:/XboxGames/Mount & Blade II- Bannerlord/Content/Modules/AIInfluence/save_data")
    if cand.is_dir(): return cand
    winapps = Path("C:/Program Files/WindowsApps")
    if winapps.exists():
        try:
            for child in winapps.glob("Mount*Bannerlord*"):
                cand2 = child / "Content" / "Modules" / "AIInfluence" / "save_data"
                if cand2.is_dir(): return cand2
        except Exception: pass
    return None
def find_save_data(script_dir: Path) -> Optional[Path]:
    root = _walk_up_to_root(script_dir)
    if root:
        m = _try_modules(root)
        if m: return m
        for parent in [root] + list(root.parents):
            if parent.name.lower() == "steamapps":
                lib_root = parent.parent
                ws = _try_workshop(lib_root)
                if ws: return ws
                for lib in _steam_libraryfolders(lib_root):
                    ws2 = _try_workshop(lib)
                    if ws2: return ws2
                break
    cand_roots = []
    pf86 = os.environ.get("ProgramFiles(x86)")
    if pf86: cand_roots.append(Path(pf86) / "Steam")
    cand_roots += [Path("C:/Program Files (x86)/Steam"), Path.home() / "AppData/Local/Steam", Path("C:/Steam")]
    for sr in cand_roots:
        if not sr.exists(): continue
        ws = _try_workshop(sr)
        if ws: return ws
        for lib in _steam_libraryfolders(sr):
            ws2 = _try_workshop(lib)
            if ws2: return ws2
    gp = _try_gamepass()
    if gp: return gp
    return None

# --- App Configuration ---
APP_TITLE = "ChatSyncAuto - v7.6 Stable Vault Edition"
DEFAULT_REPLY_WAIT, DEFAULT_REPLY_TIMEOUT, DEFAULT_DEBOUNCE, DEDUP_TAIL_CHECK = 5.0, 60.0, 0.8, 12
PRESET_FILE = "ChatSyncAuto_presets.json"
ctk.set_default_color_theme("blue")

# --- Helper Functions ---
def normalize_display_name(stem: str) -> str: return stem.split(" (")[0].strip()
def extract_character_name(d: Dict[str, Any], stem: str) -> Optional[str]:
    for k in ("Name", "CharacterName", "NPCName", "DisplayName"):
        v = d.get(k)
        if isinstance(v, str) and v.strip(): return v.strip()
    for nk in ("CharacterObject", "characterObject", "Character", "character"):
        sub = d.get(nk)
        if isinstance(sub, dict):
            for k in ("Name", "CharacterName", "NPCName", "DisplayName"):
                v = sub.get(k)
                if isinstance(v, str) and v.strip(): return v.strip()
    n = normalize_display_name(stem)
    return n if n else None
def extract_location(d: Any) -> Set[str]:
    locs = set()
    if not isinstance(d, dict): return locs
    task = d.get("CurrentTask", "")
    if task:
        id_match = re.search(r"\(id:\s*([^)]+)\)", task)
        if id_match: locs.add(id_match.group(1).lower().strip())
    return locs
def entry_speaker(entry: Any) -> Optional[str]:
    if isinstance(entry, str):
        m = re.match(r"^\s*([^:\n\r]{1,64})\s*:\s*", entry)
        if m: return m.group(1).strip()
        return None
    if isinstance(entry, dict):
        for k in ("Speaker", "speaker", "From", "from", "Name", "name", "Author", "author"):
            v = entry.get(k)
            if isinstance(v, str) and v.strip(): return v.strip()
        meta = entry.get("Meta") or entry.get("meta")
        if isinstance(meta, dict):
            for k in ("Speaker", "speaker", "Name", "name"):
                v = meta.get(k)
                if isinstance(v, str) and v.strip(): return v.strip()
        for k in ("Text", "text", "Line", "line", "Message", "message"):
            v = entry.get(k)
            if isinstance(v, str):
                m = re.match(r"^\s*([^:\n\r]{1,64})\s*:\s*", v)
                if m: return m.group(1).strip()
    return None
def entry_hash(entry: Any) -> str:
    s = entry if isinstance(entry, str) else json.dumps(entry, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(s.encode("utf-8", errors="ignore")).hexdigest()
def safe_load_json(path: Path, retries: int = 3, sleep_sec: float = 0.05) -> Optional[dict]:
    for i in range(max(1, retries)):
        try: return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            if i < retries - 1: time.sleep(sleep_sec)
    return None
def safe_write_json(path: Path, data: dict) -> bool:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError: return False
def safe_write_text(path: Path, content: str) -> bool:
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        return True
    except OSError: return False
def load_presets(p: Path) -> dict:
    if not p.exists(): return {"scene_presets": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if "scene_presets" not in data: data["scene_presets"] = {}
        return data
    except Exception: return {"scene_presets": {}}
def save_presets(p: Path, presets: dict) -> str:
    try:
        p.write_text(json.dumps(presets, ensure_ascii=False, indent=2), encoding="utf-8")
        return ""
    except Exception as e: return str(e)

class FindDialog(ctk.CTkToplevel):
    def __init__(self, master, target_text_widget):
        super().__init__(master)
        self.title("Find in File")
        self.geometry("320x100")
        self.target = target_text_widget
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.search_var = ctk.StringVar()
        self.entry = ctk.CTkEntry(self, textvariable=self.search_var, placeholder_text="Type to search...")
        self.entry.pack(padx=10, pady=(15, 10), fill="x")
        self.entry.bind("<Return>", self.find_next)
        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10)
        ctk.CTkButton(btn_frame, text="Find Next", command=self.find_next, height=28).pack(side="left", expand=True, padx=2)
        self.last_idx = "1.0"
        self.after(100, self.entry.focus_set)
    def find_next(self, event=None):
        query = self.search_var.get()
        if not query: return
        self.target.tag_remove("search_highlight", "1.0", tk.END)
        idx = self.target.search(query, self.last_idx, nocase=True, stopindex=tk.END)
        if not idx:
            idx = self.target.search(query, "1.0", nocase=True, stopindex=tk.END)
            if not idx:
                messagebox.showinfo("Find", "No matches found.", parent=self)
                return
        end_idx = f"{idx}+{len(query)}c"
        self.target.tag_add("search_highlight", idx, end_idx)
        self.target.tag_config("search_highlight", background="#3B8ED0", foreground="white")
        self.target.see(idx)
        self.target.mark_set(tk.INSERT, end_idx)
        self.last_idx = end_idx

class JSONFileChangeHandler(FileSystemEventHandler):
    def __init__(self, update_queue: queue.Queue): self.update_queue = update_queue
    def on_modified(self, event):
        if not event.is_directory and event.src_path.endswith('.json'):
            if "ChatSyncSagas" not in event.src_path and "ChatSyncLetters" not in event.src_path:
                self.update_queue.put(Path(event.src_path))

@dataclass
class PendingExchange:
    file_path: Path
    interlocutor: str
    player_index: int
    player_entry: Any
    started_at: float
    allow_complete_at: float

class AutoEngine:
    def __init__(self, app: "ChatSyncAutoApp"):
        self.app = app
        self.last_len: Dict[Path, int] = {}
        self.pending: Dict[Path, PendingExchange] = {}
        self.ignore: Dict[Path, Set[str]] = {}
        self.ignore_ttl: Dict[Tuple[Path, str], float] = {}
        self.auto_archived_milestones: Dict[Path, int] = {}

    def set_files(self, files: List[Path]):
        self.last_len.clear(); self.pending.clear(); self.ignore.clear(); self.ignore_ttl.clear(); self.auto_archived_milestones.clear()
        for p in files:
            if "ChatSyncSagas" in str(p) or "ChatSyncLetters" in str(p): continue
            d = safe_load_json(p) or {}
            ch = d.get("ConversationHistory", [])
            self.last_len[p] = len(ch) if isinstance(ch, list) else 0

    def mark_written(self, path: Path, entries: List[Any], ttl: float = 120.0):
        now = time.time()
        s = self.ignore.setdefault(path, set())
        for e in entries:
            h = entry_hash(e)
            s.add(h)
            self.ignore_ttl[(path, h)] = now + ttl

    def _cleanup(self):
        now = time.time()
        for (p, h), t in list(self.ignore_ttl.items()):
            if t < now:
                self.ignore_ttl.pop((p, h), None)
                if p in self.ignore and h in self.ignore[p]: self.ignore[p].remove(h)

    def process_file(self, p: Path, now: float):
        if "ChatSyncSagas" in str(p) or "ChatSyncLetters" in str(p): return
        self._cleanup()
        if p.name in ["dynamic_events.json", "diplomatic_statements.json"]: self.app._vault_world_events()
        d = safe_load_json(p)
        if not isinstance(d, dict): return
        ch = d.get("ConversationHistory", [])
        if not isinstance(ch, list): ch = []
        prev_len = self.last_len.get(p, 0)
        cur_len = len(ch)
        npc_plain = extract_character_name(d, p.stem) or self.app.path_to_plain.get(p, normalize_display_name(p.stem))
        last_milestone = self.auto_archived_milestones.get(p, 0)
        
        if cur_len < last_milestone:
            self.auto_archived_milestones[p] = 0
            last_milestone = 0

        if self.app.auto_archive_enabled.get():
            try: threshold = int(self.app.auto_archive_threshold_var.get())
            except ValueError: threshold = 200 
            if cur_len >= threshold and (last_milestone == 0 or cur_len >= last_milestone + threshold):
                if str(p) not in self.app.currently_archiving:
                    self.auto_archived_milestones[p] = cur_len
                    self.app.currently_archiving.add(str(p))
                    self.app.log(f"[Auto-Archive] {npc_plain} hit {cur_len} messages! Triggering background Lorebook creation...")
                    threading.Thread(target=self.app._archive_saga_process, args=(p, d, True), daemon=True).start()
                    return

        if not self.app.auto_enabled.get() or cur_len <= prev_len:
            self.last_len[p] = cur_len
            return
        new = ch[prev_len:cur_len]
        self.last_len[p] = cur_len
        ignore_set = self.ignore.get(p, set())
        new2 = [e for e in new if entry_hash(e) not in ignore_set]

        pend = self.pending.get(p)
        if pend:
            reply = None
            for idx in range(pend.player_index + 1, len(ch)):
                e = ch[idx]
                sp = (entry_speaker(e) or "").strip().lower()
                if sp == "player": continue
                if entry_hash(e) in ignore_set: continue
                if sp and sp != npc_plain.lower(): continue
                reply = e
                break

            if reply and now >= min(pend.allow_complete_at, pend.started_at + 0.2):
                actual_name = entry_speaker(reply)
                inter = actual_name.strip() if actual_name and actual_name.strip().lower() != "player" else pend.interlocutor
                p_text = pend.player_entry.get("Text", str(pend.player_entry)) if isinstance(pend.player_entry, dict) else (re.match(r"^\s*[^:\n\r]+\s*:\s*(.*)", pend.player_entry).group(1) if re.match(r"^\s*[^:\n\r]+\s*:\s*(.*)", pend.player_entry) else pend.player_entry) if isinstance(pend.player_entry, str) else ""
                p_text = p_text.strip()
                is_director_prompt = False
                if self.app.smart_filter_prompts.get() and ((p_text.startswith("(") and p_text.endswith(")")) or "(continue)" in p_text.lower()): is_director_prompt = True

                if self.app.mirror_npc_only.get() or is_director_prompt:
                    if is_director_prompt and not self.app.mirror_npc_only.get(): self.app.log(f"[Smart Filter] Ignored player OOC prompt.")
                    self._mirror(interlocutor=inter, entries=[reply], incoming=True)
                else: self._mirror(interlocutor=inter, entries=[pend.player_entry, reply])
                self.pending.pop(p, None)
            return

        if not new2: return
        for i, e in enumerate(new2):
            if (entry_speaker(e) or "").lower() == "player":
                self.pending[p] = PendingExchange(p, npc_plain, (cur_len - len(new2)) + i, e, now, now + float(self.app.reply_wait.get()))
                self.app.set_interlocutor(npc_plain)
                self.app.log(f"[Auto] Detected Player -> {npc_plain}. Waiting for reply...")
                return

        if self.app.handle_incoming.get():
            incoming = None
            for e in reversed(new2):
                sp = (entry_speaker(e) or "").strip().lower()
                if sp == "player": continue
                if sp and sp != npc_plain.lower(): continue
                incoming = e
                break
            if incoming:
                inc_text = incoming.get("Text", str(incoming)) if isinstance(incoming, dict) else (re.match(r"^\s*[^:\n\r]+\s*:\s*(.*)", incoming).group(1) if re.match(r"^\s*[^:\n\r]+\s*:\s*(.*)", incoming) else incoming) if isinstance(incoming, str) else ""
                is_letter = False
                if self.app.smart_filter_prompts.get() and (re.search(r"\[.*MESSENGER.*\]|\[.*LETTER.*\]", inc_text, re.IGNORECASE)): is_letter = True
                if is_letter:
                    self.app.log(f"[Smart Filter] Ignored incoming letter event from {npc_plain}. Saved to Mailbox Vault.")
                    self.app._archive_letter(npc_plain, inc_text)
                else:
                    self.app.set_interlocutor(npc_plain)
                    self.app.log(f"[Auto] Incoming line from {npc_plain}.")
                    self._mirror(interlocutor=npc_plain, entries=[incoming], incoming=True)

    def _mirror(self, interlocutor: str, entries: List[Any], incoming: bool = False):
        dry = self.app.dry_run.get()
        if self.app.auto_add_local.get() and interlocutor:
            p = self.app.plain_to_path.get(interlocutor)
            if p:
                d = safe_load_json(p)
                loc = extract_location(d)
                if loc:
                    added = 0
                    for display, path in self.app.characters:
                        if display not in self.app.scene_members:
                            sub_d = safe_load_json(path)
                            if loc.intersection(extract_location(sub_d)):
                                self.app.scene_members.add(display)
                                added += 1
                    if added > 0:
                        self.app._rebuild_all_list(); self.app._rebuild_scene_list(); self.app._set_status()
                        task_str = d.get("CurrentTask", "")
                        name_match = re.search(r"(?:in|at|near|to)\s+([^(]+)\s*\(id:", task_str)
                        display_loc = name_match.group(1).strip() if name_match else list(loc)[0]
                        self.app.scene_location_var.set(f"Loc: {display_loc}")

        targets = self.app.get_scene_targets(interlocutor)
        norm_entries = []
        for i, e in enumerate(entries):
            sp = (entry_speaker(e) or "").strip()
            if sp:
                norm_entries.append(e)
                continue
            msg = e.get("Text", str(e)) if isinstance(e, dict) else str(e)
            if incoming: norm_entries.append(f"{interlocutor}: {msg}")
            else: norm_entries.append(f"{'Player' if i == 0 else interlocutor}: {msg}")

        self._pending_undo_backups = []
        changed_any = False
        if targets:
            for t in targets:
                if dry: continue
                ok, changed = self._append_to_file(t, norm_entries)
                if ok and changed:
                    changed_any = True
                    self.mark_written(t, norm_entries)
            if not dry and changed_any: self.app._push_undo_group(f"Auto mirror from {interlocutor}", self._pending_undo_backups)
            self.app.log(f"[Auto] Mirrored {len(norm_entries)} line(s) to {len(targets)} scene member(s).")
        if self.app.auto_copy_npc.get():
            npc_entry = next((e for e in reversed(norm_entries) if (entry_speaker(e) or "").lower() != "player"), None)
            if npc_entry: self.app.copy_to_clipboard(npc_entry)

    def _append_to_file(self, path: Path, entries: List[Any]) -> tuple[bool, bool]:
        try: old_text = path.read_text(encoding="utf-8")
        except OSError: old_text = ""
        d = safe_load_json(path)
        if not isinstance(d, dict): return False, False
        ch = d.get("ConversationHistory", [])
        if not isinstance(ch, list): ch = []
        tail_hashes = {entry_hash(e) for e in ch[-DEDUP_TAIL_CHECK:]}
        appended = False
        for e in entries:
            if entry_hash(e) not in tail_hashes:
                ch.append(e)
                appended = True
        if not appended: return True, False
        d["ConversationHistory"] = ch
        if safe_write_json(path, d):
            if hasattr(self, "_pending_undo_backups"): self._pending_undo_backups.append((path, old_text))
            return True, True
        return False

class ChatSyncAutoApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1200x880")
        self.minsize(1000, 700)
        self.bind("<Control-f>", self._open_find_dialog)
        self.bind("<Control-F>", self._open_find_dialog)

        self.script_dir = Path(sys.executable).parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
        self.save_data_dir: Optional[Path] = None
        self.campaign_dir: Optional[Path] = None
        self.characters: List[Tuple[str, Path]] = []
        self.plain_to_path: Dict[str, Path] = {}
        self.path_to_plain: Dict[Path, str] = {}
        self.scene_members: Set[str] = set()

        self.auto_enabled = ctk.BooleanVar(value=False)
        self.auto_include_interlocutor = ctk.BooleanVar(value=True)
        self.handle_incoming = ctk.BooleanVar(value=True)
        self.mirror_npc_only = ctk.BooleanVar(value=False)
        self.smart_filter_prompts = ctk.BooleanVar(value=True) 
        self.auto_copy_npc = ctk.BooleanVar(value=False)
        self.auto_add_local = ctk.BooleanVar(value=False) 
        self.dry_run = ctk.BooleanVar(value=False)
        self.reply_wait = ctk.DoubleVar(value=DEFAULT_REPLY_WAIT)
        
        self.presets_path = self.script_dir / PRESET_FILE
        self.presets = load_presets(self.presets_path)

        self.auto_archive_enabled = ctk.BooleanVar(value=self.presets.get("auto_archive_enabled", False))
        self.auto_archive_threshold_var = ctk.StringVar(value=str(self.presets.get("auto_archive_threshold", 200)))
        self.currently_archiving = set()
        
        self.auto_archive_enabled.trace_add("write", self._save_settings_trigger)
        self.auto_archive_threshold_var.trace_add("write", self._save_settings_trigger)
        
        self.currently_editing_path: Optional[Path] = None
        self.current_interlocutor: Optional[str] = None
        self.current_world_file: Optional[Path] = None
        self.undo_stack: list[dict] = []
        self.max_undo = 20
        
        self.ui_theme = ctk.StringVar(value=self.presets.get("appearance_mode", "Dark"))
        self.ui_font_family = ctk.StringVar(value=self.presets.get("font_family", "Helvetica"))
        self.ui_font_size = ctk.StringVar(value=str(self.presets.get("font_size", "12")))
        self.api_language_var = ctk.StringVar(value=self.presets.get("api_language", "English"))
        self.player_name_var = ctk.StringVar(value=self.presets.get("player_name", "Player"))
        
        ctk.set_appearance_mode(self.ui_theme.get())
        self.file_queue = queue.Queue()
        self.observer = None

        self._build_ui()
        self._apply_appearance_settings()
        self.engine = AutoEngine(self)
        self._init_paths()
        self.after(200, self._process_file_queue)

    def _open_find_dialog(self, event=None):
        target = None
        focus = self.focus_get()
        if isinstance(focus, tk.Text): target = focus
        else:
            active_main_tab = self.tabview.get()
            if active_main_tab == "World Editor": target = self.world_textbox._textbox
            elif active_main_tab == "Lore Library": target = self.lore_textbox._textbox
            elif active_main_tab == "Mailbox": target = self.mail_textbox._textbox
            elif active_main_tab == "World Events": target = self.events_textbox._textbox
            else:
                active_editor_tab = self.editor_tabview.get()
                if active_editor_tab == "Raw JSON": target = self.json_text._textbox
                elif active_editor_tab == "Chat History": target = self.history_text._textbox
                elif active_editor_tab == "Dialogue Reader": target = self.reader_text._textbox

        if target:
            if not hasattr(self, "find_window") or not self.find_window.winfo_exists(): self.find_window = FindDialog(self, target)
            else:
                self.find_window.target = target
                self.find_window.focus()

    def _save_settings_trigger(self, *args):
        self.presets["auto_archive_enabled"] = self.auto_archive_enabled.get()
        try: self.presets["auto_archive_threshold"] = int(self.auto_archive_threshold_var.get())
        except ValueError: pass 
        self.presets["player_name"] = self.player_name_var.get()
        save_presets(self.presets_path, self.presets)

    def _build_ui(self):
        top = ctk.CTkFrame(self)
        top.pack(side="top", fill="x", padx=10, pady=10)
        ctk.CTkLabel(top, text="Campaign:").pack(side="left", padx=(5, 5))
        self.campaign_combo = ctk.CTkComboBox(top, values=[], width=250, command=self._on_campaign_change)
        self.campaign_combo.set("Select a campaign...") 
        self.campaign_combo.pack(side="left", padx=(0, 10))
        ctk.CTkButton(top, text="Browse save_data…", command=self._browse_save_data, width=120).pack(side="left", padx=5)
        ctk.CTkButton(top, text="Refresh", command=self.refresh, width=80).pack(side="left", padx=5)
        self.undo_btn = ctk.CTkButton(top, text="App Undo (Files)", command=self.undo_last, state="disabled", width=120)
        self.undo_btn.pack(side="left", padx=5)
        self.status_var = ctk.StringVar(value="Ready.")
        self.scene_location_var = ctk.StringVar(value="Loc: —")
        ctk.CTkLabel(top, textvariable=self.status_var).pack(side="right", padx=10)
        ctk.CTkLabel(top, text="|").pack(side="right", padx=5)
        ctk.CTkLabel(top, textvariable=self.scene_location_var).pack(side="right", padx=10)

        main_frame = ctk.CTkFrame(self, fg_color="transparent")
        main_frame.pack(fill="both", expand=True, padx=10, pady=5)
        main_frame.grid_columnconfigure(0, weight=1); main_frame.grid_columnconfigure(1, weight=1); main_frame.grid_columnconfigure(2, weight=1)
        main_frame.grid_rowconfigure(0, weight=1)

        npc_mgr = ctk.CTkFrame(main_frame)
        controls = ctk.CTkFrame(main_frame)
        json_panel = ctk.CTkFrame(main_frame)
        npc_mgr.grid(row=0, column=0, sticky="nsew", padx=5)
        controls.grid(row=0, column=1, sticky="nsew", padx=5)
        json_panel.grid(row=0, column=2, sticky="nsew", padx=5)

        ctk.CTkLabel(npc_mgr, text="NPC Manager", font=ctk.CTkFont(weight="bold")).pack(pady=(10, 5))
        self.npc_search_var = ctk.StringVar()
        self.npc_search_var.trace_add("write", lambda *args: self._rebuild_all_list())
        ctk.CTkEntry(npc_mgr, textvariable=self.npc_search_var, placeholder_text="Search characters...").pack(fill="x", padx=10, pady=(0, 5))

        list_frame = ctk.CTkFrame(npc_mgr, fg_color="transparent")
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)
        self.all_list = tk.Listbox(list_frame, selectmode="extended", highlightthickness=0, borderwidth=0)
        self.all_list.pack(side="left", fill="both", expand=True, padx=(0,5))
        self.all_list.bind("<Double-Button-1>", lambda e: self._add_selected_to_scene())
        self.all_list.bind("<<ListboxSelect>>", self._on_listbox_select) 

        mid_btns = ctk.CTkFrame(list_frame, fg_color="transparent")
        mid_btns.pack(side="left", fill="y", padx=5)
        ctk.CTkButton(mid_btns, text="Add >>", command=self._add_selected_to_scene, width=60).pack(pady=(50, 5))
        ctk.CTkButton(mid_btns, text="<< Remove", command=self._remove_selected_from_scene, width=60).pack(pady=5)
        ctk.CTkButton(mid_btns, text="Clear", command=self._clear_scene, width=60).pack(pady=20)

        self.scene_list = tk.Listbox(list_frame, selectmode="extended", highlightthickness=0, borderwidth=0)
        self.scene_list.pack(side="left", fill="both", expand=True, padx=(5,0))
        self.scene_list.bind("<Double-Button-1>", lambda e: self._remove_selected_from_scene())
        self.scene_list.bind("<<ListboxSelect>>", self._on_listbox_select) 

        ctk.CTkLabel(npc_mgr, text="Scene Presets", font=ctk.CTkFont(weight="bold")).pack(pady=(5, 0))
        preset_frame = ctk.CTkFrame(npc_mgr, fg_color="transparent")
        preset_frame.pack(fill="x", padx=10, pady=5)
        preset_names = list(self.presets.get("scene_presets", {}).keys())
        self.preset_combo = ctk.CTkComboBox(preset_frame, values=preset_names, width=150)
        if preset_names: self.preset_combo.set(preset_names[0])
        else: self.preset_combo.set("Type a new name...")
        self.preset_combo.pack(side="left", padx=(0, 5))
        ctk.CTkButton(preset_frame, text="Load", command=self._load_preset, width=50).pack(side="left", padx=2)
        ctk.CTkButton(preset_frame, text="Save", command=self._save_preset, width=50).pack(side="left", padx=2)
        ctk.CTkButton(preset_frame, text="Del", command=self._delete_preset, width=40, fg_color="#8B0000", hover_color="#600000").pack(side="left", padx=2)

        ctk.CTkLabel(npc_mgr, text="Manual Location Sync", font=ctk.CTkFont(weight="bold")).pack(pady=(5, 0))
        loc_frame = ctk.CTkFrame(npc_mgr, fg_color="transparent")
        loc_frame.pack(fill="x", padx=10, pady=(5, 10))
        ctk.CTkButton(loc_frame, text="Add Everyone Near Selected NPC", command=self._add_local_from_selected).pack(fill="x", expand=True, padx=2)

        self.tabview = ctk.CTkTabview(controls)
        self.tabview.pack(fill="both", expand=True, padx=10, pady=(10, 5))
        self.tab_auto = self.tabview.add("Auto Sync")
        self.tab_adv = self.tabview.add("Advanced")
        self.tab_lore = self.tabview.add("Lore Library")
        self.tab_events = self.tabview.add("World Events") 
        self.tab_letters = self.tabview.add("Mailbox")
        self.tab_world = self.tabview.add("World Editor")
        self.tab_app = self.tabview.add("Appearance")

        # --- AUTO SYNC TAB ---
        ctk.CTkSwitch(self.tab_auto, text="Enable Auto Scene Sync", variable=self.auto_enabled).pack(anchor="w", padx=10, pady=10)
        ctk.CTkCheckBox(self.tab_auto, text="Auto-add Local Characters (Exact Settlement)", variable=self.auto_add_local).pack(anchor="w", padx=10, pady=5)
        ctk.CTkCheckBox(self.tab_auto, text="Auto-include Interlocutor", variable=self.auto_include_interlocutor).pack(anchor="w", padx=10, pady=5)
        ctk.CTkCheckBox(self.tab_auto, text="Handle Incoming NPC Lines", variable=self.handle_incoming).pack(anchor="w", padx=10, pady=5)
        ctk.CTkCheckBox(self.tab_auto, text="Smart Filter: Ignore Player Prompts & NPC Letters", variable=self.smart_filter_prompts).pack(anchor="w", padx=10, pady=5)
        ctk.CTkCheckBox(self.tab_auto, text="Mirror ONLY NPC Reply (Always Skip Player Line)", variable=self.mirror_npc_only).pack(anchor="w", padx=10, pady=5)
        ctk.CTkCheckBox(self.tab_auto, text="Auto-copy NPC to Clipboard", variable=self.auto_copy_npc).pack(anchor="w", padx=10, pady=5)
        
        ctk.CTkLabel(self.tab_auto, text="Automation Limits", font=ctk.CTkFont(weight="bold")).pack(pady=(15, 5))
        arc_frame = ctk.CTkFrame(self.tab_auto, fg_color="transparent")
        arc_frame.pack(fill="x", padx=10)
        ctk.CTkSwitch(arc_frame, text="Enable Auto-Archiving", variable=self.auto_archive_enabled).grid(row=0, column=0, sticky="w", pady=5)
        ctk.CTkLabel(arc_frame, text="Trigger at Messages:").grid(row=1, column=0, sticky="w", pady=5)
        ctk.CTkEntry(arc_frame, textvariable=self.auto_archive_threshold_var, width=60).grid(row=1, column=1, sticky="w", padx=10)
        
        # --- LORE LIBRARY TAB ---
        ctk.CTkLabel(self.tab_lore, text="Saga Memory Archives", font=ctk.CTkFont(weight="bold")).pack(pady=(10, 5))
        lore_split = ctk.CTkFrame(self.tab_lore, fg_color="transparent")
        lore_split.pack(fill="both", expand=True, padx=5, pady=5)
        self.lore_char_list = tk.Listbox(lore_split, selectmode="single", highlightthickness=0, borderwidth=0, width=20)
        self.lore_char_list.pack(side="left", fill="y", padx=(0, 5))
        self.lore_char_list.bind("<<ListboxSelect>>", self._on_lore_char_select)
        lore_read_frame = ctk.CTkFrame(lore_split, fg_color="transparent")
        lore_read_frame.pack(side="left", fill="both", expand=True)
        self.lore_chapter_combo = ctk.CTkComboBox(lore_read_frame, values=["Select Chapter..."], command=self._on_lore_chapter_select)
        self.lore_chapter_combo.pack(fill="x", pady=(0, 5))
        self.lore_textbox = ctk.CTkTextbox(lore_read_frame, wrap="word", undo=True)
        self.lore_textbox.pack(fill="both", expand=True)

        # --- WORLD EVENTS TAB ---
        ctk.CTkLabel(self.tab_events, text="Dynamic World Events & Diplomacy", font=ctk.CTkFont(weight="bold")).pack(pady=(10, 5))
        event_top_frame = ctk.CTkFrame(self.tab_events, fg_color="transparent")
        event_top_frame.pack(fill="x", padx=5, pady=5)
        self.event_type_combo = ctk.CTkComboBox(event_top_frame, values=["Dynamic Events", "Diplomatic Statements"], command=self._rebuild_world_events_list, width=200)
        self.event_type_combo.pack(side="left", padx=5)
        self.event_type_combo.set("Dynamic Events")
        ctk.CTkButton(event_top_frame, text="Archive Vault to Lorebook", command=self._trigger_world_archive, fg_color="#4B0082", hover_color="#300052", width=180).pack(side="right", padx=5)
        event_split = ctk.CTkFrame(self.tab_events, fg_color="transparent")
        event_split.pack(fill="both", expand=True, padx=5, pady=5)
        self.events_listbox = tk.Listbox(event_split, selectmode="single", highlightthickness=0, borderwidth=0, width=25)
        self.events_listbox.pack(side="left", fill="y", padx=(0, 5))
        self.events_listbox.bind("<<ListboxSelect>>", self._on_world_event_select)
        self.events_textbox = ctk.CTkTextbox(event_split, wrap="word", undo=True)
        self.events_textbox.pack(side="left", fill="both", expand=True)
        self.events_textbox.configure(state="disabled")

        # --- MAILBOX TAB ---
        ctk.CTkLabel(self.tab_letters, text="NPC Mailbox & Letters", font=ctk.CTkFont(weight="bold")).pack(pady=(10, 5))
        mail_top_frame = ctk.CTkFrame(self.tab_letters, fg_color="transparent")
        mail_top_frame.pack(fill="x", padx=5, pady=5)
        ctk.CTkButton(mail_top_frame, text="Scan Campaign for Past Letters", command=self._scan_past_letters, fg_color="#8B4513", hover_color="#5C2E0B").pack(side="left", padx=5)
        ctk.CTkButton(mail_top_frame, text="Archive Mail to Lorebook", command=self._trigger_mail_archive, fg_color="#4B0082", hover_color="#300052").pack(side="right", padx=5)
        mail_split = ctk.CTkFrame(self.tab_letters, fg_color="transparent")
        mail_split.pack(fill="both", expand=True, padx=5, pady=5)
        self.mail_char_list = tk.Listbox(mail_split, selectmode="single", highlightthickness=0, borderwidth=0, width=20)
        self.mail_char_list.pack(side="left", fill="y", padx=(0, 5))
        self.mail_char_list.bind("<<ListboxSelect>>", self._on_mail_char_select)
        mail_read_frame = ctk.CTkFrame(mail_split, fg_color="transparent")
        mail_read_frame.pack(side="left", fill="both", expand=True)
        self.mail_combo = ctk.CTkComboBox(mail_read_frame, values=["Select Letter..."], command=self._on_mail_select)
        self.mail_combo.pack(fill="x", pady=(0, 5))
        self.mail_textbox = ctk.CTkTextbox(mail_read_frame, wrap="word", undo=True)
        self.mail_textbox.pack(fill="both", expand=True)

        # --- WORLD EDITOR TAB ---
        ctk.CTkLabel(self.tab_world, text="Global Mod Files Editor", font=ctk.CTkFont(weight="bold")).pack(pady=(10, 5))
        world_top = ctk.CTkFrame(self.tab_world, fg_color="transparent")
        world_top.pack(fill="x", padx=5, pady=5)
        world_files = ["world.txt", "world_secrets.json", "world_info.json", "cultural_traditions.json", "tts_voices.txt", "playerdescription.txt", "actionrules.txt", "eventsanalyzerrules.txt", "eventsgeneratorrules.txt", "kingdomstatementrules.txt"]
        self.world_file_combo = ctk.CTkComboBox(world_top, values=world_files, command=self._load_world_file, width=200)
        self.world_file_combo.pack(side="left", padx=5)
        self.world_file_combo.set("Select Global File...")
        self.world_status_var = ctk.StringVar(value="Ready")
        self.world_status_label = ctk.CTkLabel(world_top, textvariable=self.world_status_var, font=ctk.CTkFont(weight="bold"))
        self.world_status_label.pack(side="left", padx=10)
        self.world_textbox = ctk.CTkTextbox(self.tab_world, wrap="none", undo=True)
        self.world_textbox.pack(fill="both", expand=True, padx=10, pady=(5, 5))
        self.world_textbox.bind("<KeyRelease>", self._check_world_syntax)
        world_btn_frame = ctk.CTkFrame(self.tab_world, fg_color="transparent")
        world_btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkButton(world_btn_frame, text="Format JSON", width=80, command=self._format_world_editor).pack(side="left", padx=2)
        ctk.CTkButton(world_btn_frame, text="Toggle Wrap", width=80, fg_color="#4682B4", hover_color="#4169E1", command=self._toggle_world_wrap).pack(side="left", padx=2)
        self.btn_save_world = ctk.CTkButton(world_btn_frame, text="Save Global File", fg_color="#228B22", hover_color="#006400", command=self._save_world_editor)
        self.btn_save_world.pack(side="right", padx=2)

        # --- ADVANCED TAB ---
        ctk.CTkLabel(self.tab_adv, text="Manual Sync to Scene", font=ctk.CTkFont(weight="bold")).pack(pady=(10, 5))
        adv_form = ctk.CTkFrame(self.tab_adv, fg_color="transparent")
        adv_form.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(adv_form, text="Search Source:").grid(row=0, column=0, sticky="w", pady=5)
        self.manual_source_filter_var = ctk.StringVar()
        self.manual_source_filter_var.trace_add("write", self._filter_manual_source_combo)
        ctk.CTkEntry(adv_form, textvariable=self.manual_source_filter_var, width=200, placeholder_text="Type to find character...").grid(row=0, column=1, padx=10, pady=5)
        ctk.CTkLabel(adv_form, text="Source Character:").grid(row=1, column=0, sticky="w", pady=5)
        self.manual_source_combo = ctk.CTkComboBox(adv_form, values=["Select Source..."], width=200)
        self.manual_source_combo.grid(row=1, column=1, padx=10, pady=5)
        ctk.CTkLabel(adv_form, text="Lines to Mirror (X):").grid(row=2, column=0, sticky="w", pady=5)
        self.manual_lines_var = ctk.StringVar(value="1")
        ctk.CTkEntry(adv_form, textvariable=self.manual_lines_var, width=50).grid(row=2, column=1, sticky="w", padx=10, pady=5)
        ctk.CTkButton(self.tab_adv, text="Mirror to Scene", command=self._manual_mirror_action).pack(pady=10)

        # AI API Settings Block 
        ctk.CTkLabel(self.tab_adv, text="AI API Settings (For Auto-Archiver)", font=ctk.CTkFont(weight="bold")).pack(pady=(20, 5))
        api_form = ctk.CTkFrame(self.tab_adv, fg_color="transparent")
        api_form.pack(fill="x", padx=10, pady=5)
        ctk.CTkLabel(api_form, text="Provider:").grid(row=0, column=0, sticky="w", pady=5)
        self.api_provider_var = ctk.StringVar(value=self.presets.get("api_provider", "Local Ollama"))
        ctk.CTkComboBox(api_form, values=["Local Ollama", "OpenAI", "Groq", "OpenRouter", "Anthropic (Claude)", "Custom Compatible"], variable=self.api_provider_var, command=self._on_provider_change, width=250).grid(row=0, column=1, padx=10, pady=5)
        ctk.CTkLabel(api_form, text="Endpoint URL:").grid(row=1, column=0, sticky="w", pady=5)
        self.api_url_var = ctk.StringVar(value=self.presets.get("api_url", "http://localhost:11434/api/chat"))
        self.api_url_var.trace_add("write", self._save_api_setting)
        ctk.CTkEntry(api_form, textvariable=self.api_url_var, width=250).grid(row=1, column=1, padx=10, pady=5)
        ctk.CTkLabel(api_form, text="Model Name:").grid(row=2, column=0, sticky="w", pady=5)
        self.api_model_var = ctk.StringVar(value=self.presets.get("api_model", "llama3"))
        self.api_model_var.trace_add("write", self._save_api_setting)
        ctk.CTkEntry(api_form, textvariable=self.api_model_var, width=250).grid(row=2, column=1, padx=10, pady=5)
        ctk.CTkLabel(api_form, text="API Key:").grid(row=3, column=0, sticky="w", pady=5)
        self.api_key_var = ctk.StringVar(value=self.presets.get("api_key", ""))
        self.api_key_var.trace_add("write", self._save_api_setting)
        ctk.CTkEntry(api_form, textvariable=self.api_key_var, width=250, show="*").grid(row=3, column=1, padx=10, pady=5)
        ctk.CTkLabel(api_form, text="Output Language:").grid(row=4, column=0, sticky="w", pady=5)
        ctk.CTkComboBox(api_form, values=["English", "Russian", "Spanish", "French", "German", "Chinese", "Korean", "Japanese", "Portuguese"], variable=self.api_language_var, command=self._save_api_setting, width=250).grid(row=4, column=1, padx=10, pady=5)
        self.api_language_var.trace_add("write", self._save_api_setting)
        ctk.CTkButton(api_form, text="Test Connection", command=self._test_api_connection, width=120).grid(row=5, column=0, pady=10, sticky="w")
        self.api_status_var = ctk.StringVar(value="")
        ctk.CTkLabel(api_form, textvariable=self.api_status_var).grid(row=5, column=1, sticky="w", padx=10)

        # APPEARANCE TAB & HERO NAME
        ctk.CTkLabel(self.tab_app, text="Main Hero Name (For Dialogue Reader):").pack(anchor="w", padx=20, pady=(15, 5))
        ctk.CTkEntry(self.tab_app, textvariable=self.player_name_var, width=250).pack(anchor="w", padx=20)
        self.player_name_var.trace_add("write", self._save_settings_trigger)
        ctk.CTkLabel(self.tab_app, text="Theme Style:").pack(anchor="w", padx=20, pady=(15, 5))
        ctk.CTkOptionMenu(self.tab_app, values=["Dark", "Light", "System"], variable=self.ui_theme, command=self._apply_appearance_settings).pack(anchor="w", padx=20)
        ctk.CTkLabel(self.tab_app, text="Font Family:").pack(anchor="w", padx=20, pady=(15, 5))
        ctk.CTkOptionMenu(self.tab_app, values=["Helvetica", "Consolas", "Courier New", "Arial", "Times New Roman"], variable=self.ui_font_family, command=self._apply_appearance_settings).pack(anchor="w", padx=20)
        ctk.CTkLabel(self.tab_app, text="Font Size:").pack(anchor="w", padx=20, pady=(15, 5))
        ctk.CTkOptionMenu(self.tab_app, values=["10", "12", "14", "16", "18"], variable=self.ui_font_size, command=self._apply_appearance_settings).pack(anchor="w", padx=20)

        ctk.CTkLabel(controls, text="Log", font=ctk.CTkFont(weight="bold")).pack(pady=(5, 5))
        self.log_text = ctk.CTkTextbox(controls, wrap="word", height=150)
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_text.configure(state="disabled")

        # --- JSON EDITOR PANEL ---
        ctk.CTkLabel(json_panel, text="NPC Profile Editor", font=ctk.CTkFont(weight="bold")).pack(pady=(10, 0))
        self.json_msg_count_var = ctk.StringVar(value="Total Messages: 0")
        ctk.CTkLabel(json_panel, textvariable=self.json_msg_count_var, text_color="gray").pack(pady=(0, 5))
        
        btn_frame_top = ctk.CTkFrame(json_panel, fg_color="transparent")
        btn_frame_top.pack(pady=(0, 5), fill="x", padx=10)
        ctk.CTkButton(btn_frame_top, text="Archive Saga to Memory", command=self._trigger_manual_archive, fg_color="#4B0082", hover_color="#300052").pack(side="top", expand=True, fill="x", padx=2)

        rewind_frame = ctk.CTkFrame(json_panel, fg_color="transparent")
        rewind_frame.pack(fill="x", padx=10, pady=(0, 5))
        ctk.CTkButton(rewind_frame, text="⏪ Undo Last AI Reply", width=120, command=self._undo_last_ai_reply, fg_color="#B22222", hover_color="#8B0000").pack(side="left", padx=2, expand=True)
        ctk.CTkButton(rewind_frame, text="⏪ Undo Last Exchange", width=120, command=self._undo_last_exchange, fg_color="#B22222", hover_color="#8B0000").pack(side="left", padx=2, expand=True)

        self.editor_tabview = ctk.CTkTabview(json_panel, command=self._update_save_button_state)
        self.editor_tabview.pack(fill="both", expand=True, padx=10, pady=(0, 5))
        self.tab_easy = self.editor_tabview.add("Easy Editor")
        self.tab_reader = self.editor_tabview.add("Dialogue Reader")
        self.tab_history = self.editor_tabview.add("Chat History")
        self.tab_raw = self.editor_tabview.add("Raw JSON")

        # --- EASY EDITOR TAB ---
        self.easy_scroll = ctk.CTkScrollableFrame(self.tab_easy, fg_color="transparent")
        self.easy_scroll.pack(fill="both", expand=True)

        ctk.CTkLabel(self.easy_scroll, text="Character Description (Core Personality)", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(0, 2))
        self.easy_char_desc = ctk.CTkTextbox(self.easy_scroll, height=100, wrap="word", undo=True)
        self.easy_char_desc.pack(fill="x", pady=(0, 15))
        ctk.CTkLabel(self.easy_scroll, text="AI Generated Personality", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(0, 2))
        self.easy_ai_pers = ctk.CTkTextbox(self.easy_scroll, height=100, wrap="word", undo=True)
        self.easy_ai_pers.pack(fill="x", pady=(0, 15))
        ctk.CTkLabel(self.easy_scroll, text="AI Generated Backstory", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(0, 2))
        self.easy_ai_back = ctk.CTkTextbox(self.easy_scroll, height=100, wrap="word", undo=True)
        self.easy_ai_back.pack(fill="x", pady=(0, 15))
        ctk.CTkLabel(self.easy_scroll, text="AI Generated Speech Quirks", font=ctk.CTkFont(weight="bold")).pack(anchor="w", pady=(0, 2))
        self.easy_ai_quirks = ctk.CTkTextbox(self.easy_scroll, height=80, wrap="word", undo=True)
        self.easy_ai_quirks.pack(fill="x", pady=(0, 15))
        ctk.CTkLabel(self.easy_scroll, text="Known Info (Type one piece of info per line)", font=ctk.CTkFont(weight="bold"), text_color="#5CE1E6").pack(anchor="w", pady=(0, 2))
        self.easy_known_info = ctk.CTkTextbox(self.easy_scroll, height=100, wrap="none", undo=True)
        self.easy_known_info.pack(fill="x", pady=(0, 15))
        ctk.CTkLabel(self.easy_scroll, text="Known Secrets (Type one secret per line)", font=ctk.CTkFont(weight="bold"), text_color="#F08080").pack(anchor="w", pady=(0, 2))
        self.easy_known_secrets = ctk.CTkTextbox(self.easy_scroll, height=100, wrap="none", undo=True)
        self.easy_known_secrets.pack(fill="x", pady=(0, 10))

        # --- DIALOGUE READER TAB ---
        ctk.CTkLabel(self.tab_reader, text="Read-Only Script View", font=ctk.CTkFont(weight="bold"), text_color="gray").pack(pady=(0, 2))
        self.reader_text = ctk.CTkTextbox(self.tab_reader, wrap="none")
        self.reader_text.pack(fill="both", expand=True, pady=(0, 5))
        self.reader_text.tag_config("player_name", foreground="#5CE1E6", justify="right")
        self.reader_text.tag_config("player_text", foreground="#E0E0E0", justify="right")
        self.reader_text.tag_config("npc_name", foreground="#73D073", justify="left")
        self.reader_text.tag_config("npc_text", foreground="#E0E0E0", justify="left")
        self.reader_text.tag_config("lore_text", foreground="#DDA0DD", justify="center")
        self.reader_text.configure(state="disabled")

        # --- CHAT HISTORY TAB ---
        self.history_syntax_var = ctk.StringVar(value="✅ Valid Array")
        self.history_syntax_label = ctk.CTkLabel(self.tab_history, textvariable=self.history_syntax_var, text_color="#228B22", font=ctk.CTkFont(weight="bold"))
        self.history_syntax_label.pack(pady=(0, 2))
        self.history_text = ctk.CTkTextbox(self.tab_history, wrap="none", undo=True)
        self.history_text.pack(fill="both", expand=True, pady=(0, 5))
        self.history_text.bind("<KeyRelease>", self._check_history_syntax)

        # --- RAW JSON TAB ---
        self.json_syntax_var = ctk.StringVar(value="✅ Valid JSON")
        self.json_syntax_label = ctk.CTkLabel(self.tab_raw, textvariable=self.json_syntax_var, text_color="#228B22", font=ctk.CTkFont(weight="bold"))
        self.json_syntax_label.pack(pady=(0, 2))
        self.json_text = ctk.CTkTextbox(self.tab_raw, wrap="none", undo=True)
        self.json_text.pack(fill="both", expand=True, pady=(0, 5))
        self.json_text.bind("<KeyRelease>", self._check_json_syntax)

        # --- EDITOR CONTROLS ---
        editor_btn_frame = ctk.CTkFrame(json_panel, fg_color="transparent")
        editor_btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        ctk.CTkButton(editor_btn_frame, text="Format", width=70, command=self._format_json_editor).pack(side="left", padx=2)
        ctk.CTkButton(editor_btn_frame, text="Discard", width=70, fg_color="#8B0000", hover_color="#600000", command=self._refresh_json_editor).pack(side="left", padx=2)
        ctk.CTkButton(editor_btn_frame, text="Toggle Wrap", width=80, fg_color="#4682B4", hover_color="#4169E1", command=self._toggle_json_wrap).pack(side="left", padx=2)
        self.btn_save_json = ctk.CTkButton(editor_btn_frame, text="Save Changes", fg_color="#228B22", hover_color="#006400", command=self._save_json_editor)
        self.btn_save_json.pack(side="right", padx=2)

    def _toggle_world_wrap(self):
        current = self.world_textbox._textbox.cget("wrap")
        self.world_textbox._textbox.configure(wrap="none" if current == "word" else "word")

    def _toggle_json_wrap(self):
        active_tab = self.editor_tabview.get()
        if active_tab == "Raw JSON":
            current = self.json_text._textbox.cget("wrap")
            self.json_text._textbox.configure(wrap="none" if current == "word" else "word")
        elif active_tab == "Chat History":
            current = self.history_text._textbox.cget("wrap")
            self.history_text._textbox.configure(wrap="none" if current == "word" else "word")
        elif active_tab == "Dialogue Reader":
            current = self.reader_text._textbox.cget("wrap")
            self.reader_text._textbox.configure(wrap="none" if current == "word" else "word")
        elif active_tab == "Easy Editor":
            current = self.easy_known_info._textbox.cget("wrap")
            self.easy_known_info._textbox.configure(wrap="none" if current == "word" else "word")
            self.easy_known_secrets._textbox.configure(wrap="none" if current == "word" else "word")

    def _vault_world_events(self):
        if not self.campaign_dir: return
        vault_dir = self.campaign_dir / "ChatSyncVaults"
        vault_dir.mkdir(exist_ok=True)
        dyn_path = self.campaign_dir / "dynamic_events.json"
        if dyn_path.exists():
            vault_dyn = vault_dir / "vaulted_dynamic_events.json"
            new_data = safe_load_json(dyn_path)
            vault_data = safe_load_json(vault_dyn) or []
            if isinstance(new_data, list): new_events = new_data
            elif isinstance(new_data, dict) and "diplomatic_events" in new_data: new_events = new_data.get("diplomatic_events", [])
            else: new_events = []
            vaulted_ids = {e.get("id") for e in vault_data if "id" in e}
            added = 0
            for ev in new_events:
                if ev.get("id") and ev.get("id") not in vaulted_ids:
                    vault_data.append(ev)
                    added += 1
            if added > 0: safe_write_json(vault_dyn, vault_data)
        dip_path = self.campaign_dir / "diplomatic_statements.json"
        if dip_path.exists():
            vault_dip = vault_dir / "vaulted_diplomatic_statements.json"
            new_data = safe_load_json(dip_path)
            vault_data = safe_load_json(vault_dip) or []
            if isinstance(new_data, list):
                vaulted_hashes = {hashlib.sha1(str(e).encode()).hexdigest() for e in vault_data}
                added = 0
                for stmt in new_data:
                    h = hashlib.sha1(str(stmt).encode()).hexdigest()
                    if h not in vaulted_hashes:
                        vault_data.append(stmt)
                        added += 1
                if added > 0: safe_write_json(vault_dip, vault_data)

    def _trigger_world_archive(self):
        if not self.campaign_dir: return
        vault_dir = self.campaign_dir / "ChatSyncVaults"
        vault_dyn = vault_dir / "vaulted_dynamic_events.json"
        vault_dip = vault_dir / "vaulted_diplomatic_statements.json"
        dyn_data = safe_load_json(vault_dyn) or []
        dip_data = safe_load_json(vault_dip) or []
        if len(dyn_data) < 5 and len(dip_data) < 5:
            self.log("[Archive] Not enough global events vaulted to create a World Chronicle yet.")
            return
        self.log("[Archive] Sending Vaulted World Events to AI for summarization...")
        threading.Thread(target=self._process_global_archive, args=(dyn_data, dip_data, "World_Chronicle"), daemon=True).start()

    def _trigger_mail_archive(self):
        sel = self.mail_char_list.curselection()
        if not sel:
            self.log("[Archive] Select an NPC from the Mailbox list first.")
            return
        char_name = self.mail_char_list.get(sel[0])
        mail_file = self.campaign_dir / "ChatSyncLetters" / f"{char_name}.json"
        data = safe_load_json(mail_file)
        if not data or len(data.get("letters", [])) < 3:
            self.log("[Archive] Not enough letters to archive. Need at least 3.")
            return
        self.log(f"[Archive] Sending Letters for {char_name} to AI for summarization...")
        threading.Thread(target=self._process_global_archive, args=(data["letters"], [], f"Letters_{char_name}"), daemon=True).start()

    def _process_global_archive(self, data_1, data_2, filename_stem):
        combined = {"Data1": data_1, "Data2": data_2}
        prompt = f"You are a master historian for an RPG. Read the following JSON events/statements and summarize them into a beautiful, dense, third-person historical chronicle chapter. Focus on major plot points, wars, threats, and character actions. Output ONLY valid JSON containing a single array of strings with exactly ONE element containing your summary. Do not include markdown formatting or explanations. Start with [ and end with ]. Language MUST be in {self.api_language_var.get()}."
        provider = self.api_provider_var.get()
        url = self.api_url_var.get().strip()
        model = self.api_model_var.get().strip()
        api_key = self.api_key_var.get().strip()
        headers = {'Content-Type': 'application/json'}
        if provider == "Anthropic (Claude)":
            headers['x-api-key'] = api_key
            headers['anthropic-version'] = '2023-06-01'
            payload = {"model": model, "max_tokens": 4000, "system": prompt, "messages": [{"role": "user", "content": json.dumps(combined)[:12000]}]}
        else:
            if api_key: headers['Authorization'] = f'Bearer {api_key}'
            payload = {"model": model, "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(combined)[:12000]}], "stream": False}
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=120) as response:
                res_json = json.loads(response.read())
                content = res_json["content"][0]["text"].strip() if provider == "Anthropic (Claude)" else res_json.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                s_idx, e_idx = content.find('['), content.rfind(']')
                if s_idx != -1 and e_idx != -1: content = content[s_idx:e_idx+1]
                archive_array = json.loads(content)
                saga_dir = self.campaign_dir / "ChatSyncSagas"
                saga_dir.mkdir(exist_ok=True)
                saga_file = saga_dir / f"{filename_stem}.json"
                saga_data = safe_load_json(saga_file) or {"character": filename_stem, "chapters": []}
                if "chapters" not in saga_data: saga_data["chapters"] = []
                saga_data["chapters"].append({"chapter": len(saga_data.get("chapters", [])) + 1, "content": archive_array[0]})
                safe_write_json(saga_file, saga_data)
                self.after(0, lambda: self.log(f"[Archive] Successfully created Lorebook Chapter for {filename_stem}!"))
                self.after(0, self._rebuild_lore_library)
        except Exception as e:
            self.after(0, lambda err=e: self.log(f"[Archive] API Error: {err}"))

    def _rebuild_world_events_list(self, *args):
        self.events_listbox.delete(0, tk.END)
        self.events_textbox.configure(state="normal", undo=False)
        self.events_textbox.delete("1.0", tk.END)
        self.events_textbox.configure(state="disabled", undo=True)
        try: self.events_textbox._textbox.edit_reset()
        except Exception: pass
        if not self.campaign_dir: return
        mode = self.event_type_combo.get()
        vault_dir = self.campaign_dir / "ChatSyncVaults"
        if mode == "Dynamic Events":
            path = vault_dir / "vaulted_dynamic_events.json"
            if not path.exists(): path = self.campaign_dir / "dynamic_events.json"
            data = safe_load_json(path)
            if isinstance(data, list):
                for idx, event in enumerate(data): self.events_listbox.insert(tk.END, f"{idx}: {event.get('title', f'Event #{idx}')}")
            elif isinstance(data, dict) and "diplomatic_events" in data:
                for idx, event in enumerate(data.get("diplomatic_events", [])): self.events_listbox.insert(tk.END, f"{idx}: {event.get('title', f'Event #{idx}')}")
        elif mode == "Diplomatic Statements":
            path = vault_dir / "vaulted_diplomatic_statements.json"
            if not path.exists(): path = self.campaign_dir / "diplomatic_statements.json"
            data = safe_load_json(path)
            if isinstance(data, list):
                for idx, stmt in enumerate(data):
                    self.events_listbox.insert(tk.END, f"{idx}: {str(stmt.get('kingdom_id', 'Unknown')).capitalize()} - {str(stmt.get('action', 'Unknown'))}")

    def _on_world_event_select(self, event):
        sel = self.events_listbox.curselection()
        if not sel: return
        idx = int(self.events_listbox.get(sel[0]).split(":")[0])
        mode = self.event_type_combo.get()
        self.events_textbox.configure(state="normal", undo=False)
        self.events_textbox.delete("1.0", tk.END)
        vault_dir = self.campaign_dir / "ChatSyncVaults"
        if mode == "Dynamic Events":
            path = vault_dir / "vaulted_dynamic_events.json"
            if not path.exists(): path = self.campaign_dir / "dynamic_events.json"
            data = safe_load_json(path)
            events = data if isinstance(data, list) else data.get("diplomatic_events", []) if isinstance(data, dict) else []
            if 0 <= idx < len(events):
                ev = events[idx]
                text = f"👑 TITLE: {ev.get('title', 'Unknown')}\n🏷️ TYPE: {ev.get('type', 'Unknown').capitalize()}\n⭐ IMPORTANCE: {ev.get('importance', 'N/A')}/10\n🌍 KINGDOMS INVOLVED: {', '.join(ev.get('kingdoms_involved', []))}\n📅 CREATED ON (DAYS): {ev.get('creation_campaign_days', 'N/A')}\n" + "-" * 40 + f"\n\n📜 DESCRIPTION:\n{ev.get('description', 'No description provided.')}\n"
                self.events_textbox.insert(tk.END, text)
        elif mode == "Diplomatic Statements":
            path = vault_dir / "vaulted_diplomatic_statements.json"
            if not path.exists(): path = self.campaign_dir / "diplomatic_statements.json"
            data = safe_load_json(path)
            if isinstance(data, list) and 0 <= idx < len(data):
                stmt = data[idx]
                text = f"👑 KINGDOM: {str(stmt.get('kingdom_id', 'Unknown')).capitalize()}\n🎯 TARGET: {str(stmt.get('target_kingdom_id', 'Unknown')).capitalize()}\n⚔️ ACTION: {stmt.get('action', 'None')}\n📅 DAYS: {stmt.get('campaign_days', 'N/A')}\n" + "-" * 40 + f"\n\n🗣️ STATEMENT:\n\"{stmt.get('statement_text', '')}\"\n\n" + "-" * 40 + f"\n🧠 REASON:\n{stmt.get('reason', '')}\n"
                self.events_textbox.insert(tk.END, text)
        self.events_textbox.configure(state="disabled", undo=True)
        try: self.events_textbox._textbox.edit_reset()
        except Exception: pass

    def _load_world_file(self, choice):
        if not self.save_data_dir: return
        self.current_world_file = self.save_data_dir.parent / choice
        self.world_textbox.configure(undo=False)
        self.world_textbox.delete("1.0", tk.END)
        if self.current_world_file.exists():
            self.world_textbox.insert(tk.END, self.current_world_file.read_text(encoding="utf-8"))
        else:
            self.world_status_var.set("⚠️ File Not Found (Will create on Save)")
            self.world_status_label.configure(text_color="#B8860B")
        self.world_textbox.configure(undo=True)
        try: self.world_textbox._textbox.edit_reset()
        except Exception: pass
        self._check_world_syntax()

    def _check_world_syntax(self, event=None):
        if not self.current_world_file: return
        raw_text = self.world_textbox.get("1.0", tk.END).strip()
        if self.current_world_file.suffix == ".json":
            if not raw_text:
                self.world_status_var.set("⚠️ Empty JSON")
                self.world_status_label.configure(text_color="#B8860B")
                self.btn_save_world.configure(state="disabled")
                return
            try:
                json.loads(raw_text)
                self.world_status_var.set("✅ Valid JSON")
                self.world_status_label.configure(text_color="#228B22")
                self.btn_save_world.configure(state="normal")
            except json.JSONDecodeError as e:
                self.world_status_var.set(f"🔴 Invalid JSON: {e}")
                self.world_status_label.configure(text_color="#8B0000")
                self.btn_save_world.configure(state="disabled")
        else:
            self.world_status_var.set("✅ Plain Text Mode")
            self.world_status_label.configure(text_color="#228B22")
            self.btn_save_world.configure(state="normal")

    def _format_world_editor(self):
        if not self.current_world_file or self.current_world_file.suffix != ".json": 
            messagebox.showinfo("Format Info", "Only JSON files can be formatted.")
            return
        raw_text = self.world_textbox.get("1.0", tk.END).strip()
        if not raw_text: return
        try:
            data = json.loads(raw_text)
            self.world_textbox.delete("1.0", tk.END)
            self.world_textbox.insert(tk.END, json.dumps(data, indent=2, ensure_ascii=False))
            self._check_world_syntax()
        except json.JSONDecodeError:
            messagebox.showwarning("Format Error", "Cannot format the code. The JSON is currently invalid.")

    def _save_world_editor(self):
        if not self.current_world_file: return
        raw_text = self.world_textbox.get("1.0", tk.END).strip()
        try:
            old_text = self.current_world_file.read_text(encoding="utf-8") if self.current_world_file.exists() else ""
            if self.current_world_file.suffix == ".json": safe_write_json(self.current_world_file, json.loads(raw_text))
            else: safe_write_text(self.current_world_file, raw_text)
            self._push_undo_group(f"Edit {self.current_world_file.name}", [(self.current_world_file, old_text)])
            self.log(f"[World Editor] Successfully saved global file: {self.current_world_file.name}")
        except json.JSONDecodeError as e:
            messagebox.showerror("JSON Error", f"Invalid JSON:\n\n{e}")

    def _clear_mod_caches(self, data: dict):
        data["LastAIResponseJson"] = None
        data["LastDynamicResponse"] = None

    def _undo_last_ai_reply(self):
        if not self.currently_editing_path: return
        data = safe_load_json(self.currently_editing_path)
        if not data: return
        old_text = self.currently_editing_path.read_text(encoding="utf-8")
        ch = data.get("ConversationHistory", [])
        if not ch: return
        last_sp = (entry_speaker(ch[-1]) or "").strip().lower()
        if last_sp != "player" and not str(ch[-1]).startswith("System: [MEMORY ARCHIVE]"): ch.pop()
        data["ConversationHistory"] = ch
        self._clear_mod_caches(data)
        if safe_write_json(self.currently_editing_path, data):
            self._push_undo_group(f"Undo Last AI Reply for {self.currently_editing_path.stem}", [(self.currently_editing_path, old_text)])
            self.log(f"[Quick Rewind] Deleted last AI reply and cleared caches for {self.currently_editing_path.stem}.")
            self._refresh_json_editor()

    def _undo_last_exchange(self):
        if not self.currently_editing_path: return
        data = safe_load_json(self.currently_editing_path)
        if not data: return
        old_text = self.currently_editing_path.read_text(encoding="utf-8")
        ch = data.get("ConversationHistory", [])
        if not ch: return
        last_sp = (entry_speaker(ch[-1]) or "").strip().lower()
        if last_sp != "player" and not str(ch[-1]).startswith("System: [MEMORY ARCHIVE]"): ch.pop()
        if ch and (entry_speaker(ch[-1]) or "").strip().lower() == "player": ch.pop()
        data["ConversationHistory"] = ch
        self._clear_mod_caches(data)
        if safe_write_json(self.currently_editing_path, data):
            self._push_undo_group(f"Undo Last Exchange for {self.currently_editing_path.stem}", [(self.currently_editing_path, old_text)])
            self.log(f"[Quick Rewind] Deleted last exchange (Player + AI) and cleared caches for {self.currently_editing_path.stem}.")
            self._refresh_json_editor()

    def _update_save_button_state(self, *args):
        active_tab = self.editor_tabview.get()
        if active_tab == "Easy Editor": self.btn_save_json.configure(state="normal")
        elif active_tab == "Chat History": self._check_history_syntax()
        elif active_tab == "Raw JSON": self._check_json_syntax()
        elif active_tab == "Dialogue Reader": self.btn_save_json.configure(state="disabled")

    def _check_json_syntax(self, event=None):
        raw_text = self.json_text.get("1.0", tk.END).strip()
        if not raw_text:
            self.json_syntax_var.set("⚠️ Empty File")
            self.json_syntax_label.configure(text_color="#B8860B")
            if self.editor_tabview.get() == "Raw JSON": self.btn_save_json.configure(state="disabled")
            return
        try:
            json.loads(raw_text)
            self.json_syntax_var.set("✅ Valid JSON")
            self.json_syntax_label.configure(text_color="#228B22")
            if self.editor_tabview.get() == "Raw JSON": self.btn_save_json.configure(state="normal")
        except json.JSONDecodeError as e:
            self.json_syntax_var.set(f"🔴 Invalid JSON: {e}")
            self.json_syntax_label.configure(text_color="#8B0000")
            if self.editor_tabview.get() == "Raw JSON": self.btn_save_json.configure(state="disabled")

    def _check_history_syntax(self, event=None):
        raw_text = self.history_text.get("1.0", tk.END).strip()
        if not raw_text:
            self.history_syntax_var.set("⚠️ Empty File")
            self.history_syntax_label.configure(text_color="#B8860B")
            if self.editor_tabview.get() == "Chat History": self.btn_save_json.configure(state="disabled")
            return
        try:
            if not isinstance(json.loads(raw_text), list): raise ValueError("Must be an array [...]")
            self.history_syntax_var.set("✅ Valid History Array")
            self.history_syntax_label.configure(text_color="#228B22")
            if self.editor_tabview.get() == "Chat History": self.btn_save_json.configure(state="normal")
        except Exception as e:
            self.history_syntax_var.set(f"🔴 Invalid Array: {e}")
            self.history_syntax_label.configure(text_color="#8B0000")
            if self.editor_tabview.get() == "Chat History": self.btn_save_json.configure(state="disabled")

    def _format_json_editor(self):
        active_tab = self.editor_tabview.get()
        if active_tab == "Raw JSON":
            raw_text = self.json_text.get("1.0", tk.END).strip()
            if not raw_text: return
            try:
                data = json.loads(raw_text)
                self.json_text.delete("1.0", tk.END)
                self.json_text.insert(tk.END, json.dumps(data, indent=2, ensure_ascii=False))
                self._check_json_syntax()
            except json.JSONDecodeError: messagebox.showwarning("Format Error", "Cannot format the code. The JSON is currently invalid.")
        elif active_tab == "Chat History":
            raw_text = self.history_text.get("1.0", tk.END).strip()
            if not raw_text: return
            try:
                data = json.loads(raw_text)
                self.history_text.delete("1.0", tk.END)
                self.history_text.insert(tk.END, json.dumps(data, indent=2, ensure_ascii=False))
                self._check_history_syntax()
            except Exception: messagebox.showwarning("Format Error", "Cannot format the array. The JSON is currently invalid.")

    def _insert_reader_dialogue(self, speaker: str, text: str):
        hero_name = self.player_name_var.get().strip() or "Player"
        if speaker.lower() == "player":
            self.reader_text.insert(tk.END, f"{hero_name} 👤\n", "player_name")
            self.reader_text.insert(tk.END, f"{text}\n\n", "player_text")
        else:
            self.reader_text.insert(tk.END, f"🗣️ {speaker}\n", "npc_name")
            self.reader_text.insert(tk.END, f"{text}\n\n", "npc_text")

    def _refresh_json_editor(self):
        if not self.currently_editing_path: return
        data = safe_load_json(self.currently_editing_path)
        widgets = [self.json_text, self.history_text, self.easy_char_desc, self.easy_ai_pers, self.easy_ai_back, self.easy_ai_quirks, self.easy_known_info, self.easy_known_secrets]
        for w in widgets:
            w.configure(undo=False, state="normal")
            w.delete("1.0", tk.END)
        self.reader_text.configure(state="normal")
        self.reader_text.delete("1.0", tk.END)
        hero_name = self.player_name_var.get().strip() or "Player"
        if data:
            self.json_text.insert(tk.END, json.dumps(data, indent=2, ensure_ascii=False))
            ch = data.get("ConversationHistory", [])
            self.history_text.insert(tk.END, json.dumps(ch, indent=2, ensure_ascii=False))
            self.json_msg_count_var.set(f"Total Messages: {len(ch)}")
            reader_args = []
            for entry in ch:
                if isinstance(entry, str):
                    if "System: [MEMORY ARCHIVE]" in entry: reader_args.extend((f"\n{'='*50}\n📚 {entry}\n{'='*50}\n\n", "lore_text"))
                    else:
                        parts = entry.split(":", 1)
                        if len(parts) == 2:
                            sp, tx = parts[0].strip(), parts[1].strip()
                            if sp.lower() == "player": reader_args.extend((f"{hero_name} 👤\n", "player_name", f"{tx}\n\n", "player_text"))
                            else: reader_args.extend((f"🗣️ {sp}\n", "npc_name", f"{tx}\n\n", "npc_text"))
                        else: reader_args.extend((f"{entry}\n\n", "npc_text"))
                elif isinstance(entry, dict):
                    sp = entry.get("Speaker", entry.get("speaker", "Unknown"))
                    tx = entry.get("Text", entry.get("text", ""))
                    if sp.lower() == "player": reader_args.extend((f"{hero_name} 👤\n", "player_name", f"{tx}\n\n", "player_text"))
                    else: reader_args.extend((f"🗣️ {sp}\n", "npc_name", f"{tx}\n\n", "npc_text"))
            if reader_args: self.reader_text._textbox.insert(tk.END, *reader_args)
            self.easy_char_desc.insert(tk.END, data.get("CharacterDescription") or "")
            self.easy_ai_pers.insert(tk.END, data.get("AIGeneratedPersonality") or "")
            self.easy_ai_back.insert(tk.END, data.get("AIGeneratedBackstory") or "")
            self.easy_ai_quirks.insert(tk.END, data.get("AIGeneratedSpeechQuirks") or "")
            info_arr = data.get("KnownInfo", [])
            if isinstance(info_arr, list): self.easy_known_info.insert(tk.END, "\n".join([str(i) for i in info_arr]))
            secrets_arr = data.get("KnownSecrets", [])
            if isinstance(secrets_arr, list): self.easy_known_secrets.insert(tk.END, "\n".join([str(s) for s in secrets_arr]))
            self.json_syntax_var.set("✅ Valid JSON")
            self.json_syntax_label.configure(text_color="#228B22")
            self.history_syntax_var.set("✅ Valid History Array")
            self.history_syntax_label.configure(text_color="#228B22")
            self.reader_text.see(tk.END)
            self.history_text.see(tk.END)
            self.json_text.see(tk.END)
        else: 
            self.json_msg_count_var.set("Total Messages: 0")
            self.json_syntax_var.set("✅ Valid JSON")
            self.history_syntax_var.set("✅ Valid Array")
            self.reader_text.insert(tk.END, "No chat history found.")
        self.reader_text.configure(state="disabled")
        for w in widgets:
            w.configure(undo=True)
            try: w._textbox.edit_reset()
            except Exception: pass
        self._update_save_button_state()

    def _save_json_editor(self):
        if not self.currently_editing_path: return
        active_tab = self.editor_tabview.get()
        if active_tab == "Dialogue Reader": return 
        if active_tab == "Raw JSON":
            raw_text = self.json_text.get("1.0", tk.END).strip()
            if not raw_text: return
            try:
                data = json.loads(raw_text)
                old_text = self.currently_editing_path.read_text(encoding="utf-8")
                old_data = safe_load_json(self.currently_editing_path)
                if old_data and len(data.get("ConversationHistory", [])) < len(old_data.get("ConversationHistory", [])):
                    self._clear_mod_caches(data)
                    self.log("[Auto-Sanitizer] Wiped C# caches due to manual raw JSON edit.")
                if safe_write_json(self.currently_editing_path, data):
                    self._push_undo_group(f"Manual JSON edit of {self.currently_editing_path.stem}", [(self.currently_editing_path, old_text)])
                    self.log(f"[Editor] Saved raw JSON changes to {self.currently_editing_path.stem}")
                    self._refresh_json_editor() 
            except json.JSONDecodeError as e: messagebox.showerror("JSON Error", f"Invalid JSON:\n\n{e}")
        elif active_tab == "Chat History":
            raw_text = self.history_text.get("1.0", tk.END).strip()
            if not raw_text: return
            try:
                new_ch = json.loads(raw_text)
                if not isinstance(new_ch, list): raise ValueError("History must be a JSON array.")
                data = safe_load_json(self.currently_editing_path)
                old_text = self.currently_editing_path.read_text(encoding="utf-8")
                if new_ch != data.get("ConversationHistory", []):
                    self._clear_mod_caches(data)
                    self.log("[Auto-Sanitizer] Wiped C# caches due to manual chat history edit.")
                data["ConversationHistory"] = new_ch
                if safe_write_json(self.currently_editing_path, data):
                    self._push_undo_group(f"History Edit of {self.currently_editing_path.stem}", [(self.currently_editing_path, old_text)])
                    self.log(f"[Editor] Saved isolated chat history for {self.currently_editing_path.stem}")
                    self._refresh_json_editor()
            except Exception as e: messagebox.showerror("JSON Error", f"Invalid History JSON:\n\n{e}")
        elif active_tab == "Easy Editor":
            data = safe_load_json(self.currently_editing_path)
            if not data: return
            old_text = self.currently_editing_path.read_text(encoding="utf-8")
            data["CharacterDescription"] = self.easy_char_desc.get("1.0", tk.END).strip()
            data["AIGeneratedPersonality"] = self.easy_ai_pers.get("1.0", tk.END).strip() or None
            data["AIGeneratedBackstory"] = self.easy_ai_back.get("1.0", tk.END).strip() or None
            data["AIGeneratedSpeechQuirks"] = self.easy_ai_quirks.get("1.0", tk.END).strip() or None
            data["KnownInfo"] = [l.strip() for l in self.easy_known_info.get("1.0", tk.END).strip().split('\n') if l.strip()]
            data["KnownSecrets"] = [l.strip() for l in self.easy_known_secrets.get("1.0", tk.END).strip().split('\n') if l.strip()]
            if safe_write_json(self.currently_editing_path, data):
                self._push_undo_group(f"Easy Profile Edit of {self.currently_editing_path.stem}", [(self.currently_editing_path, old_text)])
                self.log(f"[Editor] Saved profile edits to {self.currently_editing_path.stem}")
                self._refresh_json_editor() 

    def _filter_manual_source_combo(self, *args):
        term = self.manual_source_filter_var.get().lower()
        all_names = [display for display, _ in self.characters]
        filtered = [n for n in all_names if term in n.lower()] if term else all_names
        self.manual_source_combo.configure(values=filtered if filtered else ["No matches found"])
        if filtered and self.manual_source_combo.get() not in filtered:
            self.manual_source_combo.set(filtered[0])

    def _on_provider_change(self, choice):
        if choice == "Local Ollama": self.api_url_var.set("http://localhost:11434/api/chat"); self.api_model_var.set("llama3")
        elif choice == "OpenAI": self.api_url_var.set("https://api.openai.com/v1/chat/completions"); self.api_model_var.set("gpt-4o-mini")
        elif choice == "Groq": self.api_url_var.set("https://api.groq.com/openai/v1/chat/completions"); self.api_model_var.set("llama3-8b-8192")
        elif choice == "OpenRouter": self.api_url_var.set("https://openrouter.ai/api/v1/chat/completions"); self.api_model_var.set("mistralai/mixtral-8x7b-instruct")
        elif choice == "Anthropic (Claude)": self.api_url_var.set("https://api.anthropic.com/v1/messages"); self.api_model_var.set("claude-3-haiku-20240307")
        self._save_api_setting()

    def _test_api_connection(self):
        self.api_status_var.set("⏳ Testing connection...")
        threading.Thread(target=self._run_api_test, daemon=True).start()

    def _run_api_test(self):
        provider = self.api_provider_var.get()
        url = self.api_url_var.get().strip()
        model = self.api_model_var.get().strip()
        api_key = self.api_key_var.get().strip()
        headers = {'Content-Type': 'application/json'}
        payload = {}
        if provider == "Anthropic (Claude)":
            headers['x-api-key'] = api_key
            headers['anthropic-version'] = '2023-06-01'
            payload = {"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "Say 'OK'"}]}
        else:
            if api_key: headers['Authorization'] = f'Bearer {api_key}'
            payload = {"model": model, "messages": [{"role": "user", "content": "Say 'OK'"}], "stream": False}
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=10) as response:
                self.after(0, lambda: self.api_status_var.set("✅ Connection Successful!"))
        except Exception as e:
            self.after(0, lambda err=e: self.api_status_var.set(f"❌ Error: {err}"))

    def _save_api_setting(self, *args):
        self.presets["api_provider"] = self.api_provider_var.get()
        self.presets["api_url"] = self.api_url_var.get()
        self.presets["api_model"] = self.api_model_var.get()
        self.presets["api_key"] = self.api_key_var.get()
        self.presets["api_language"] = self.api_language_var.get()
        save_presets(self.presets_path, self.presets)

    def _trigger_manual_archive(self):
        if not self.currently_editing_path:
            self.log("[Archive] No file selected to archive.")
            return
        data = safe_load_json(self.currently_editing_path)
        if not data: return
        history = data.get("ConversationHistory", [])
        if len(history) <= 10:
            self.log(f"[Archive] {self.currently_editing_path.stem} only has {len(history)} messages. Need >10 for an Archive.")
            return
        self.json_msg_count_var.set(f"Total Messages: {len(history)} (⏳ Archiving Saga via AI...)")
        self.log(f"[Archive] Starting background Lorebook archiving for {self.currently_editing_path.stem}...")
        threading.Thread(target=self._archive_saga_process, args=(self.currently_editing_path, data, False), daemon=True).start()

    def _archive_saga_process(self, path: Path, data: dict, is_auto: bool):
        npc_plain = extract_character_name(data, path.stem) or normalize_display_name(path.stem)
        history = data.get("ConversationHistory", [])
        has_archive = history and isinstance(history[0], str) and "System: [MEMORY ARCHIVE]" in history[0]
        to_archive = history[1:-10] if has_archive else history[:-10]
        kept_messages = history[-10:]
        if len(to_archive) < 5:
            self.after(0, lambda: self.log(f"[Archive] Not enough new messages to archive for {npc_plain}."))
            if is_auto: self.currently_archiving.discard(str(path))
            return
        prompt = f"You are an expert at creating concise, third-person Lorebooks and Memory Archives for RPG characters. Read the following 'ConversationHistory' and write a dense, third-person summary of the events. Focus on major plot points, promises made, locations visited, injuries, and relationship shifts. Output ONLY valid JSON containing a single array of strings with exactly ONE element containing your summary. Do not include markdown formatting, code blocks, conversational filler, or explanations. Start your response with [ and end with ]. Language MUST be in {self.api_language_var.get()}."
        provider = self.api_provider_var.get()
        url = self.api_url_var.get().strip()
        model = self.api_model_var.get().strip()
        api_key = self.api_key_var.get().strip()
        headers = {'Content-Type': 'application/json'}
        if provider == "Anthropic (Claude)":
            headers['x-api-key'] = api_key
            headers['anthropic-version'] = '2023-06-01'
            payload = {"model": model, "max_tokens": 4000, "system": prompt, "messages": [{"role": "user", "content": json.dumps(to_archive)}]}
        else:
            if api_key: headers['Authorization'] = f'Bearer {api_key}'
            payload = {"model": model, "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": json.dumps(to_archive)}], "stream": False}
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
            with urllib.request.urlopen(req, timeout=120) as response:
                res_json = json.loads(response.read())
                content = res_json["content"][0]["text"].strip() if provider == "Anthropic (Claude)" else res_json.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
                s_idx, e_idx = content.find('['), content.rfind(']')
                if s_idx != -1 and e_idx != -1: content = content[s_idx:e_idx+1]
                archive_array = json.loads(content)
                chapter_text = archive_array[0].replace("System: [MEMORY ARCHIVE]", "").strip()
                saga_dir = self.campaign_dir / "ChatSyncSagas"
                saga_dir.mkdir(exist_ok=True)
                saga_file = saga_dir / f"{npc_plain}.json"
                saga_data = safe_load_json(saga_file) or {"character": npc_plain, "chapters": []}
                if "chapters" not in saga_data: saga_data["chapters"] = []
                saga_data["chapters"].append({"chapter": len(saga_data.get("chapters", [])) + 1, "content": chapter_text})
                safe_write_json(saga_file, saga_data)
                full_archive_string = "System: [MEMORY ARCHIVE]\n\n" + "\n\n".join([f"--- CHAPTER {c['chapter']} ---\n{c['content']}" for c in saga_data["chapters"]])
                new_history = [full_archive_string] + kept_messages
                self.after(0, lambda: self._apply_summary(path, new_history, is_auto=is_auto))
                self.after(0, self._rebuild_lore_library)
        except Exception as e:
            if is_auto: self.currently_archiving.discard(str(path))
            self.after(0, lambda err=e: self.log(f"[Archive] API Error: {err}"))
            self.after(0, lambda: self.json_msg_count_var.set(f"Total Messages: {len(history)} (Failed)"))

    def _apply_summary(self, path: Path, new_history: list, is_auto=False):
        data = safe_load_json(path)
        if data:
            old_text = path.read_text(encoding="utf-8")
            data["ConversationHistory"] = new_history
            if safe_write_json(path, data):
                self._push_undo_group(f"AI Saga Archive of {path.stem}", [(path, old_text)])
                msg = f"🔔 [AUTO-ARCHIVE COMPLETE] {path.stem} saved to Lore Library! RESTART GAME TO LOAD." if is_auto else f"[Archive] Successfully compressed context for {path.stem}!"
                if is_auto: self.currently_archiving.discard(str(path))
                self.log(msg)
                if self.currently_editing_path and self.currently_editing_path.resolve() == path.resolve(): self._refresh_json_editor()

    def _archive_letter(self, npc_plain: str, text: str, timestamp: str = None):
        if not self.campaign_dir: return
        mail_dir = self.campaign_dir / "ChatSyncLetters"
        mail_dir.mkdir(exist_ok=True)
        mail_file = mail_dir / f"{npc_plain}.json"
        data = safe_load_json(mail_file) or {"character": npc_plain, "letters": []}
        ts = timestamp or time.strftime("%b %d, %Y - %I:%M %p")
        if not any(l["content"] == text for l in data["letters"]):
            data["letters"].append({"timestamp": ts, "content": text})
            safe_write_json(mail_file, data)
            self.after(0, self._rebuild_mail_library)

    def _scan_past_letters(self):
        if not self.campaign_dir: return
        self.log("[Mailbox] Scanning campaign for old letters. This might take a few seconds...")
        found = 0
        for display, path in self.characters:
            d = safe_load_json(path)
            if not isinstance(d, dict): continue
            ch = d.get("ConversationHistory", [])
            npc_plain = extract_character_name(d, path.stem) or normalize_display_name(path.stem)
            letter_count = 1
            for e in ch:
                sp = (entry_speaker(e) or "").strip().lower()
                if sp == "player": continue
                text = e.get("Text", str(e)) if isinstance(e, dict) else (re.match(r"^\s*[^:\n\r]+\s*:\s*(.*)", e).group(1) if re.match(r"^\s*[^:\n\r]+\s*:\s*(.*)", e) else e) if isinstance(e, str) else ""
                if "System: [MEMORY ARCHIVE]" in text: continue
                if re.search(r"\[.*MESSENGER.*\]|\[.*LETTER.*\]", text, re.IGNORECASE):
                    self._archive_letter(npc_plain, text, timestamp=f"Recovered Letter {letter_count}")
                    letter_count += 1
                    found += 1
        self.log(f"[Mailbox] Scan complete! Recovered {found} old letters.")
        self._rebuild_mail_library()

    def _rebuild_mail_library(self):
        self.mail_char_list.delete(0, tk.END)
        self.mail_combo.set("Select Letter...")
        self.mail_combo.configure(values=["Select Letter..."])
        self.mail_textbox.configure(state="normal")
        self.mail_textbox.delete("1.0", tk.END)
        self.mail_textbox.configure(state="disabled")
        if not self.campaign_dir: return
        mail_dir = self.campaign_dir / "ChatSyncLetters"
        if not mail_dir.exists(): return
        for f in sorted(mail_dir.glob("*.json")): self.mail_char_list.insert(tk.END, f.stem)

    def _on_mail_char_select(self, event):
        sel = self.mail_char_list.curselection()
        if not sel: return
        char_name = self.mail_char_list.get(sel[0])
        mail_file = self.campaign_dir / "ChatSyncLetters" / f"{char_name}.json"
        data = safe_load_json(mail_file)
        if not data: return
        letters = [f"[{i+1}] {l.get('timestamp', 'Unknown Date')}" for i, l in enumerate(data.get("letters", []))]
        if letters:
            self.mail_combo.configure(values=letters)
            self.mail_combo.set(letters[-1]) 
            self._on_mail_select(letters[-1])

    def _on_mail_select(self, choice):
        if not choice or choice == "Select Letter...": return
        sel = self.mail_char_list.curselection()
        if not sel: return
        char_name = self.mail_char_list.get(sel[0])
        mail_file = self.campaign_dir / "ChatSyncLetters" / f"{char_name}.json"
        data = safe_load_json(mail_file)
        if not data: return
        try:
            idx = int(choice.split("]")[0].replace("[", "")) - 1
            letter_data = data["letters"][idx]
            self.mail_textbox.configure(state="normal")
            self.mail_textbox.delete("1.0", tk.END)
            self.mail_textbox.insert(tk.END, f"--- Sent: {letter_data.get('timestamp')} ---\n\n{letter_data['content']}")
            self.mail_textbox.configure(state="disabled")
        except (IndexError, ValueError): pass

    def _rebuild_lore_library(self):
        self.lore_char_list.delete(0, tk.END)
        self.lore_chapter_combo.set("Select Chapter...")
        self.lore_chapter_combo.configure(values=["Select Chapter..."])
        self.lore_textbox.configure(state="normal")
        self.lore_textbox.delete("1.0", tk.END)
        self.lore_textbox.configure(state="disabled")
        if not self.campaign_dir: return
        saga_dir = self.campaign_dir / "ChatSyncSagas"
        if not saga_dir.exists(): return
        for f in sorted(saga_dir.glob("*.json")): self.lore_char_list.insert(tk.END, f.stem)

    def _on_lore_char_select(self, event):
        sel = self.lore_char_list.curselection()
        if not sel: return
        char_name = self.lore_char_list.get(sel[0])
        saga_file = self.campaign_dir / "ChatSyncSagas" / f"{char_name}.json"
        data = safe_load_json(saga_file)
        if not data: return
        chapters = [f"Chapter {c['chapter']}" for c in data.get("chapters", [])]
        if chapters:
            self.lore_chapter_combo.configure(values=chapters)
            self.lore_chapter_combo.set(chapters[0])
            self._on_lore_chapter_select(chapters[0])

    def _on_lore_chapter_select(self, choice):
        if not choice or choice == "Select Chapter...": return
        sel = self.lore_char_list.curselection()
        if not sel: return
        char_name = self.lore_char_list.get(sel[0])
        saga_file = self.campaign_dir / "ChatSyncSagas" / f"{char_name}.json"
        data = safe_load_json(saga_file)
        if not data: return
        chapter_num = int(choice.replace("Chapter ", ""))
        for c in data.get("chapters", []):
            if c["chapter"] == chapter_num:
                self.lore_textbox.configure(state="normal")
                self.lore_textbox.delete("1.0", tk.END)
                self.lore_textbox.insert(tk.END, c["content"])
                self.lore_textbox.configure(state="disabled")
                break

    def _apply_appearance_settings(self, event=None):
        theme = self.ui_theme.get()
        font_fam = self.ui_font_family.get()
        font_sz = int(self.ui_font_size.get())
        ctk.set_appearance_mode(theme)
        if theme.lower() == "light" or (theme.lower() == "system" and ctk.get_appearance_mode().lower() == "light"):
            list_bg, list_fg, list_sel = "#EBEBEB", "#242424", "#3B8ED0"
        else:
            list_bg, list_fg, list_sel = "#2b2b2b", "white", "#1f538d"
        tk_font = (font_fam, font_sz)
        for lst in [self.all_list, self.scene_list, self.lore_char_list, self.mail_char_list, self.events_listbox]:
            lst.configure(bg=list_bg, fg=list_fg, selectbackground=list_sel, font=tk_font)
        ctk_font = ctk.CTkFont(family=font_fam, size=font_sz)
        for w in [self.log_text, self.json_text, self.easy_char_desc, self.easy_ai_pers, self.easy_ai_back, self.easy_ai_quirks, self.easy_known_info, self.easy_known_secrets, self.history_text, self.reader_text, self.lore_textbox, self.mail_textbox, self.world_textbox, self.events_textbox]:
            w.configure(font=ctk_font)
        self.presets["appearance_mode"] = theme
        self.presets["font_family"] = font_fam
        self.presets["font_size"] = font_sz
        err = save_presets(self.presets_path, self.presets)
        if err and hasattr(self, 'log_text'): self.log(f"[Warning] Failed to save settings: {err}")

    def _start_file_watcher(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
        if self.campaign_dir and self.campaign_dir.is_dir():
            self.observer = Observer()
            self.observer.schedule(JSONFileChangeHandler(self.file_queue), str(self.campaign_dir), recursive=True)
            self.observer.start()
            self.log(f"[Watcher] Started monitoring {self.campaign_dir.name}")

    def _process_file_queue(self):
        while not self.file_queue.empty():
            try:
                changed_file = self.file_queue.get_nowait()
                self.engine.process_file(changed_file, time.time())
                if self.currently_editing_path and self.currently_editing_path.resolve() == changed_file.resolve():
                    self._refresh_json_editor()
            except queue.Empty: break
        self.after(200, self._process_file_queue)

    def _init_paths(self):
        last_sd = self.presets.get("last_save_data")
        if last_sd and Path(last_sd).is_dir():
            self.save_data_dir = Path(last_sd)
            self.log(f"[System] Resumed memory. Found folder: {self.save_data_dir.name}")
            self.refresh()
            return
        sd = find_save_data(self.script_dir)
        if sd:
            self.save_data_dir = sd
            self.log("[System] Auto-detected save_data folder.")
            self.refresh()
        else: self.log("[System] Could not auto-detect save_data. Please browse manually.")

    def _browse_save_data(self):
        d = filedialog.askdirectory(title="Select AIInfluence save_data folder")
        if d and Path(d).is_dir():
            self.save_data_dir = Path(d)
            self.presets["last_save_data"] = str(self.save_data_dir)
            save_presets(self.presets_path, self.presets)
            self.refresh()

    def _list_campaigns(self) -> List[str]:
        if not self.save_data_dir or not self.save_data_dir.is_dir(): return []
        camps = sorted([c.name for c in self.save_data_dir.iterdir() if c.is_dir()])
        if not camps and any(self.save_data_dir.glob("*.json")): return ["(Current Folder)"]
        return camps

    def _on_campaign_change(self, event=None):
        if self.save_data_dir:
            name = self.campaign_combo.get().strip()
            if name == "(Current Folder)": self.campaign_dir = self.save_data_dir
            else: self.campaign_dir = self.save_data_dir / name
            (self.campaign_dir / "ChatSyncSagas").mkdir(exist_ok=True)
            (self.campaign_dir / "ChatSyncLetters").mkdir(exist_ok=True)
            self.presets["last_save_data"] = str(self.save_data_dir)
            self.presets["last_campaign"] = name
            save_presets(self.presets_path, self.presets)
            self._vault_world_events()
            self.refresh_characters()
            self._rebuild_lore_library()
            self._rebuild_mail_library()
            self._rebuild_world_events_list()
            self._start_file_watcher()

    def refresh(self):
        camps = self._list_campaigns()
        self.campaign_combo.configure(values=camps)
        if camps:
            saved_camp = self.presets.get("last_campaign")
            current = self.campaign_combo.get()
            if current in camps: self.campaign_combo.set(current)
            elif saved_camp in camps: self.campaign_combo.set(saved_camp)
            else: self.campaign_combo.set(camps[0])
            self._on_campaign_change()
        else:
            self.campaign_combo.set("No campaigns found")
            self.campaign_dir = None
            self.refresh_characters()

    def refresh_characters(self):
        if not self.campaign_dir or not self.campaign_dir.is_dir(): return
        chars, names_for_combo = [], []
        for f in sorted(self.campaign_dir.rglob("*.json")):
            if "ChatSyncSagas" in str(f) or "ChatSyncLetters" in str(f) or "ChatSyncVaults" in str(f): continue
            d = safe_load_json(f)
            if not isinstance(d, dict): continue
            name = extract_character_name(d, f.stem)
            rel = f.relative_to(self.campaign_dir).as_posix().rsplit(".", 1)[0]
            display = f"{name} ({rel})" if name and name.lower() not in f.stem.lower() else rel
            chars.append((display, f))
            names_for_combo.append(display)
        self.characters = chars
        self.plain_to_path = {normalize_display_name(d): p for d, p in chars}
        self.path_to_plain = {p: normalize_display_name(d) for d, p in chars}
        valid = {d for d, _ in chars}
        self.scene_members = {d for d in self.scene_members if d in valid}
        self.engine.set_files([p for _, p in chars])
        self._rebuild_all_list()
        self._rebuild_scene_list()
        if names_for_combo:
            self.manual_source_combo.configure(values=names_for_combo)
            self.manual_source_combo.set(names_for_combo[0])
        self.log(f"[Info] Loaded {len(chars)} characters.")
        self._set_status()

    def _refresh_preset_combo(self):
        names = list(self.presets.get("scene_presets", {}).keys())
        self.preset_combo.configure(values=names)
        if not names: self.preset_combo.set("Type a new name...")

    def _save_preset(self):
        name = self.preset_combo.get().strip()
        if not name or name == "Type a new name...":
            self.log("[Presets] Please type a valid name.")
            return
        if not self.scene_members:
            self.log("[Presets] Scene is empty!")
            return
        self.presets["scene_presets"][name] = list(self.scene_members)
        err = save_presets(self.presets_path, self.presets)
        if err: self.log(f"[Error] Failed to save preset: {err}")
        else:
            self._refresh_preset_combo()
            self.preset_combo.set(name)
            self.log(f"[Presets] Saved preset: '{name}'.")

    def _load_preset(self):
        name = self.preset_combo.get().strip()
        scene_presets = self.presets.get("scene_presets", {})
        if name in scene_presets:
            saved_members = scene_presets[name]
            valid_chars = {d for d, _ in self.characters}
            self.scene_members.clear()
            for member in saved_members:
                if member in valid_chars: self.scene_members.add(member)
            self._rebuild_all_list()
            self._rebuild_scene_list()
            self._set_status()
            self.log(f"[Presets] Loaded preset: '{name}'.")

    def _delete_preset(self):
        name = self.preset_combo.get().strip()
        if name in self.presets.get("scene_presets", {}):
            del self.presets["scene_presets"][name]
            save_presets(self.presets_path, self.presets)
            self._refresh_preset_combo()
            self.log(f"[Presets] Deleted preset: '{name}'.")

    def _add_local_from_selected(self):
        target_path = None
        sel = self.all_list.curselection()
        if sel:
            display_name = self.all_list.get(sel[0]).replace("  [in scene]", "")
            target_path = self.plain_to_path.get(normalize_display_name(display_name))
        if not target_path:
            sel_scene = self.scene_list.curselection()
            if sel_scene:
                display_name = self.scene_list.get(sel_scene[0])
                target_path = self.plain_to_path.get(normalize_display_name(display_name))
        if not target_path:
            self.log("[Location] Select a character first.")
            return
        d = safe_load_json(target_path)
        loc = extract_location(d)
        if not loc:
            self.log(f"[Location] FAILED: No Location data in {target_path.stem}.")
            return
        added_count = 0
        for display, path in self.characters:
            if display not in self.scene_members:
                sub_d = safe_load_json(path)
                sub_loc = extract_location(sub_d)
                if loc.intersection(sub_loc):
                    self.scene_members.add(display)
                    added_count += 1
        if added_count > 0:
            self._rebuild_all_list()
            self._rebuild_scene_list()
            self._set_status()
            task_str = d.get("CurrentTask", "")
            name_match = re.search(r"(?:in|at|near|to)\s+([^(]+)\s*\(id:", task_str)
            display_loc = name_match.group(1).strip() if name_match else list(loc)[0]
            self.scene_location_var.set(f"Loc: {display_loc}")
            self.log(f"[Location] Found {target_path.stem} exactly at '{display_loc}'. Added {added_count} characters.")
        else: self.log(f"[Location] Found {target_path.stem}, but no one else is exactly at that location ID.")

    def _manual_mirror_action(self):
        source_display = self.manual_source_combo.get().strip()
        source_plain = normalize_display_name(source_display)
        if not source_display or source_display == "Select Source...":
            self.log("[Manual] Select a source character first.")
            return
        try: lines_to_copy = int(self.manual_lines_var.get())
        except ValueError:
            self.log("[Manual] Please enter a valid number of lines to copy.")
            return
        path = self.plain_to_path.get(source_plain)
        if not path:
            self.log(f"[Manual] Could not find file for {source_plain}.")
            return
        d = safe_load_json(path)
        if not isinstance(d, dict): return
        ch = d.get("ConversationHistory", [])
        if not ch or lines_to_copy <= 0:
            self.log(f"[Manual] No lines found in {source_plain} to copy.")
            return
        entries_to_mirror = ch[-lines_to_copy:]
        targets = self.get_scene_targets(source_plain)
        if not targets:
            self.log("[Manual] No valid targets in the scene to mirror to.")
            return
        self.engine._pending_undo_backups = []
        changed_any = False
        for t in targets:
            ok, changed = self.engine._append_to_file(t, entries_to_mirror)
            if ok and changed:
                changed_any = True
                self.engine.mark_written(t, entries_to_mirror)
        if changed_any:
            self._push_undo_group(f"Manual mirror from {source_plain}", self.engine._pending_undo_backups)
            self.log(f"[Manual] Mirrored last {lines_to_copy} line(s) from {source_plain} to {len(targets)} scene member(s).")

    def _on_listbox_select(self, event):
        widget = event.widget
        sel = widget.curselection()
        if not sel: return
        display_name = widget.get(sel[0]).replace("  [in scene]", "")
        path = self.plain_to_path.get(normalize_display_name(display_name))
        if not path: return
        self.currently_editing_path = path
        self._refresh_json_editor()

    def _rebuild_all_list(self):
        self.all_list.delete(0, tk.END)
        term = self.npc_search_var.get().strip().lower()
        for display, _ in self.characters:
            if term and term not in display.lower(): continue
            if display in self.scene_members: self.all_list.insert(tk.END, f"{display}  [in scene]")
            else: self.all_list.insert(tk.END, display)

    def _rebuild_scene_list(self):
        self.scene_list.delete(0, tk.END)
        for display in sorted(self.scene_members, key=lambda s: s.lower()):
            self.scene_list.insert(tk.END, display)

    def _add_selected_to_scene(self):
        for idx in self.all_list.curselection():
            item = self.all_list.get(idx).replace("  [in scene]", "")
            if item in {x for x, _ in self.characters}: self.scene_members.add(item)
        self._rebuild_all_list()
        self._rebuild_scene_list()
        self._set_status()

    def _remove_selected_from_scene(self):
        for i in self.scene_list.curselection():
            self.scene_members.discard(self.scene_list.get(i))
        self._rebuild_all_list()
        self._rebuild_scene_list()
        self._set_status()

    def _clear_scene(self):
        self.scene_members.clear()
        self._rebuild_all_list()
        self._rebuild_scene_list()
        self._set_status()

    def log(self, msg: str):
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _set_status(self):
        count = len(self.scene_members)
        talk = self.current_interlocutor or "—"
        self.status_var.set(f"Scene: {count} | Talking to: {talk}")

    def set_interlocutor(self, plain: str):
        self.current_interlocutor = plain
        self._set_status()

    def get_scene_targets(self, interlocutor_plain: str) -> List[Path]:
        selected = [(normalize_display_name(d), p) for d, p in self.characters if d in self.scene_members]
        if self.auto_include_interlocutor.get() and self.current_interlocutor:
            p = self.plain_to_path.get(self.current_interlocutor)
            if p and all(n.lower() != self.current_interlocutor.lower() for n, _ in selected):
                selected.append((self.current_interlocutor, p))
        return [p for n, p in selected if n.lower() != interlocutor_plain.lower()]

    def copy_to_clipboard(self, entry: Any):
        try:
            text = entry if isinstance(entry, str) else json.dumps(entry, ensure_ascii=False)
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
            self.log("[Clipboard] Copied NPC line.")
        except Exception as e:
            self.log(f"[Clipboard] Failed: {e}")

    def _push_undo_group(self, desc: str, backups: list[tuple[Path, str]]):
        if not backups: return
        self.undo_stack.append({"desc": desc, "files": backups})
        if len(self.undo_stack) > self.max_undo: self.undo_stack = self.undo_stack[-self.max_undo:]
        self.undo_btn.configure(state="normal")

    def undo_last(self):
        if not self.undo_stack: return
        step = self.undo_stack.pop()
        ok = sum(1 for p, text in step.get("files", []) if safe_write_text(p, text))
        self.log(f"[Undo] Restored {ok} file(s) from {step.get('desc')}.")
        if not self.undo_stack: self.undo_btn.configure(state="disabled")

    def destroy(self):
        if self.observer:
            self.observer.stop()
            self.observer.join()
        super().destroy()

if __name__ == "__main__":
    app = ChatSyncAutoApp()
    app.mainloop()
