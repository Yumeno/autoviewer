import os
import subprocess
import sys
import time
import queue
import ctypes
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk, ExifTags, UnidentifiedImageError
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

if sys.platform.startswith("win"):
    from ctypes import wintypes

# 対応する画像フォーマット
SUPPORTED_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}
MIN_INTERVAL_SECONDS = 1.0
MAX_INTERVAL_SECONDS = 30.0
INTERVAL_STEP_SECONDS = 0.5

# Development-only debug switches.
# Example:
#   python viewer.py --debug resize,timeline,hud
# End-user launchers do not pass this option, so debug features stay off unless
# a developer enables them explicitly.
def parse_debug_flags(argv):
    flags = set()
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--debug" and index + 1 < len(argv):
            flags.update(part.strip().lower() for part in argv[index + 1].split(",") if part.strip())
            index += 2
            continue
        if arg.startswith("--debug="):
            flags.update(part.strip().lower() for part in arg.split("=", 1)[1].split(",") if part.strip())
        index += 1
    return flags


DEBUG_FLAGS = parse_debug_flags(sys.argv[1:])
DEBUG_RESIZE_LOG = "resize" in DEBUG_FLAGS
DEBUG_TIMELINE_LOG = "timeline" in DEBUG_FLAGS
DEBUG_HUD = "hud" in DEBUG_FLAGS
METADATA_REFRESH_DELAY_MS = 0
THUMBNAIL_HIGHLIGHT_DELAY_MS = 0
DEBUG_HUD_REFRESH_MS = 100

if sys.platform.startswith("win"):
    WM_ENTERSIZEMOVE = 0x0231
    WM_EXITSIZEMOVE = 0x0232
    WM_SIZE = 0x0005
    WM_WINDOWPOSCHANGING = 0x0046
    WM_WINDOWPOSCHANGED = 0x0047
    WM_SYSCOMMAND = 0x0112
    WM_NCLBUTTONDOWN = 0x00A1
    GWLP_WNDPROC = -4
    SW_HIDE = 0
    GA_ROOT = 2
    GA_ROOTOWNER = 3
    GW_OWNER = 4
    SC_SIZE = 0xF000

class NewImageHandler(FileSystemEventHandler):
    """フォルダに新しいファイルが追加されたことを検知するハンドラ"""
    def __init__(self, image_queue):
        self.image_queue = image_queue

    def on_created(self, event):
        if not event.is_directory:
            ext = os.path.splitext(event.src_path)[1].lower()
            if ext in SUPPORTED_EXTS:
                # 新しい画像パスをキューに追加
                self.image_queue.put(event.src_path)

class ImageViewerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Auto Image Viewer")
        self.root.geometry("500x400")
        self.root.configure(bg='black')

        # アプリの状態変数
        self.folder_path = ""
        self.image_list = []
        self.current_index = -1
        self.interval_ms = 5000  # デフォルト5秒
        self.is_playing = True
        self.is_slideshow_active = False
        self.is_fullscreen = False
        self.after_id = None
        self.thumbnail_visible = False
        self.metadata_visible = False
        self.thumbnail_photos = {}
        self.thumbnail_buttons = {}
        self.thumbnail_items = {}
        self.current_source_image = None
        self.current_source_path = None
        self.current_render_image = None
        self.current_render_path = None
        self.current_render_size = None
        self.current_render_resample = None
        self.photo = None
        self.metadata_overlay = None
        self.metadata_overlay_visible = False
        self.metadata_overlay_geometry = ""
        self.metadata_text_value = None
        self.metadata_cache = {}
        self.render_scheduled = False
        self.render_after_id = None
        self.layout_dirty = False
        self.image_dirty = False
        self.metadata_dirty = False
        self.thumbnail_highlight_dirty = False
        self.resize_after_id = None
        self.resize_preview_after_id = None
        self.is_live_resizing = False
        self.panels_hidden_for_resize = False
        self.resume_play_after_resize = False
        self.thumbnail_scroll_after_id = None
        self.metadata_refresh_after_id = None
        self.metadata_refresh_target_path = None
        self.thumbnail_highlight_after_id = None
        self.thumbnail_highlight_target_path = None
        self.thumbnail_follow_path = None
        self.last_root_size = None
        self.native_resize_active = False
        self.native_resize_hook_installed = False
        self.native_resize_hwnd = None
        self.native_resize_hwnds = []
        self.native_resize_hwnd_labels = {}
        self.native_resize_orig_procs = {}
        self.default_wndproc = None
        self.window_proc = None
        self.last_native_resize_enter_at = None
        self.last_native_resize_exit_at = None
        self.last_native_resize_source = None
        self.timeline_event_seq = 0
        self.debug_hud_label = None
        self.debug_hud_after_id = None
        self.debug_hud_dirty = False
        self.debug_hud_data = {}
        self.debug_hud_data.update(
            {
                "render_status": "idle",
                "preview_status": "idle",
                "meta_status": "idle",
                "thumb_status": "idle",
            }
        )
        
        # タップ・スワイプ判定用
        self.start_x = None
        
        # 監視用
        self.observer = None
        self.image_queue = queue.Queue()

        # UI要素の構築
        self.setup_ui()
        self.install_native_resize_hook()
        self.sync_interval_ui()
        self.update_play_pause_button()
        self.update_fullscreen_button()
        self.apply_panel_layout()
        self.set_metadata_text("画像情報を表示するには、再生中に情報欄を開いてください。")
        
        # キーバインド
        self.root.bind("<Escape>", self.exit_fullscreen)
        self.root.bind("<Right>", lambda e: self.next_image())
        self.root.bind("<Left>", lambda e: self.prev_image())
        self.root.bind("<space>", lambda e: self.next_image())
        self.root.bind("p", self.toggle_play)
        self.root.bind("P", self.toggle_play)
        self.root.bind("<Control-c>", self.copy_current_image_to_clipboard)
        self.root.bind("<Configure>", self.on_root_configure)

        # キューの定期チェックを開始
        self.check_queue()

    def setup_ui(self):
        """設定画面と画像表示画面の構築"""
        self.content_frame = tk.Frame(self.root, bg='black')
        self.content_frame.pack(fill=tk.BOTH, expand=True)
        self.content_frame.grid_rowconfigure(0, weight=1)
        self.content_frame.grid_columnconfigure(0, weight=1)
        self.content_frame.grid_columnconfigure(1, weight=0)

        self.image_area = tk.Frame(self.content_frame, bg='black')
        self.image_area.grid(row=0, column=0, sticky="nsew")

        self.metadata_frame = tk.Frame(self.content_frame, bg='#141414', width=360, bd=1, relief=tk.SOLID)
        self.metadata_frame.grid(row=0, column=1, sticky="ns", padx=(0, 10), pady=10)
        self.metadata_frame.grid_remove()
        self.metadata_frame.pack_propagate(False)
        self.metadata_header = tk.Label(
            self.metadata_frame,
            text="Image Info",
            bg='#141414',
            fg='white',
            anchor='w',
            padx=12,
            pady=8,
            font=("Helvetica", 10, "bold")
        )
        self.metadata_header.pack(fill=tk.X)
        self.metadata_text = tk.Text(
            self.metadata_frame,
            bg='#141414',
            fg='white',
            wrap=tk.WORD,
            relief=tk.FLAT,
            highlightthickness=0,
            padx=12,
            pady=12
        )
        self.metadata_scrollbar = tk.Scrollbar(self.metadata_frame, command=self.metadata_text.yview)
        self.metadata_text.configure(yscrollcommand=self.metadata_scrollbar.set, state=tk.DISABLED)
        self.metadata_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.metadata_scrollbar.pack(side=tk.RIGHT, fill=tk.Y, pady=8, padx=(0, 8))

        # 画像表示用のラベル
        self.image_label = tk.Label(self.image_area, bg='black')
        self.image_label.pack(fill=tk.BOTH, expand=True)

        if DEBUG_HUD:
            self.debug_hud_label = tk.Label(
                self.image_area,
                bg='#101010',
                fg='#9FE870',
                justify=tk.LEFT,
                anchor='nw',
                padx=8,
                pady=6,
                font=("Consolas", 9),
                bd=1,
                relief=tk.SOLID,
            )
            self.debug_hud_label.place(x=12, y=12)

        # マウス（タッチ）イベントのバインド
        self.image_label.bind("<ButtonPress-1>", self.on_press)
        self.image_label.bind("<ButtonRelease-1>", self.on_release)
        self.image_label.bind("<Button-3>", self.show_context_menu)

        self.thumbnail_frame = tk.Frame(self.content_frame, bg='#161616', height=164, bd=1, relief=tk.SOLID)
        self.thumbnail_frame.grid(row=1, column=0, columnspan=2, sticky="ew", padx=10, pady=(0, 10))
        self.thumbnail_frame.grid_remove()
        self.thumbnail_frame.grid_propagate(False)
        self.thumbnail_header = tk.Label(
            self.thumbnail_frame,
            text="Thumbnail Carousel",
            bg='#161616',
            fg='white',
            anchor='w',
            padx=12,
            pady=6,
            font=("Helvetica", 10, "bold")
        )
        self.thumbnail_header.pack(fill=tk.X)
        self.thumbnail_canvas = tk.Canvas(
            self.thumbnail_frame,
            bg='#161616',
            height=116,
            highlightthickness=0
        )
        self.thumbnail_scrollbar = tk.Scrollbar(
            self.thumbnail_frame,
            orient=tk.HORIZONTAL,
            command=self.thumbnail_canvas.xview
        )
        self.thumbnail_inner = tk.Frame(self.thumbnail_canvas, bg='#161616')
        self.thumbnail_window = self.thumbnail_canvas.create_window((0, 0), window=self.thumbnail_inner, anchor=tk.NW)
        self.thumbnail_canvas.configure(xscrollcommand=self.thumbnail_scrollbar.set)
        self.thumbnail_canvas.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 0))
        self.thumbnail_scrollbar.pack(fill=tk.X, padx=8, pady=(0, 8))
        self.thumbnail_inner.bind("<Configure>", self.on_thumbnail_inner_configure)
        self.thumbnail_canvas.bind("<Configure>", self.on_thumbnail_canvas_configure)
        self.thumbnail_canvas.bind_all("<Shift-MouseWheel>", self.on_thumbnail_mousewheel)

        # 再生中の操作パネル
        self.seekbar_frame = tk.Frame(self.root, bg='#222222', bd=2, relief=tk.RAISED)
        self.seekbar_var = tk.IntVar()
        self.seekbar = tk.Scale(
            self.seekbar_frame,
            variable=self.seekbar_var,
            orient=tk.HORIZONTAL,
            showvalue=True,
            bg='#222222',
            fg='white',
            troughcolor='#555555',
            highlightthickness=0,
            command=self.on_seek
        )
        self.seekbar.pack(fill=tk.BOTH, expand=True, padx=10, pady=(8, 4))

        primary_controls = tk.Frame(self.seekbar_frame, bg='#222222')
        primary_controls.pack(fill=tk.X, padx=10, pady=(0, 6))

        self.fullscreen_button = tk.Button(
            primary_controls,
            text="最大化解除",
            width=10,
            command=self.toggle_fullscreen_mode
        )
        self.fullscreen_button.pack(side=tk.LEFT, padx=(0, 10))

        self.play_pause_button = tk.Button(
            primary_controls,
            text="一時停止",
            width=10,
            command=self.toggle_play,
            bg='#4CAF50',
            fg='white'
        )
        self.play_pause_button.pack(side=tk.LEFT, padx=(0, 10))

        self.change_folder_button = tk.Button(
            primary_controls,
            text="フォルダ変更",
            width=12,
            command=self.change_folder_during_slideshow
        )
        self.change_folder_button.pack(side=tk.LEFT, padx=(0, 10))

        self.interval_status_var = tk.StringVar()
        tk.Label(
            primary_controls,
            textvariable=self.interval_status_var,
            fg='white',
            bg='#222222'
        ).pack(side=tk.LEFT)

        self.close_panel_button = tk.Button(
            primary_controls,
            text="閉じる",
            width=8,
            command=self.toggle_seekbar
        )
        self.close_panel_button.pack(side=tk.RIGHT)

        secondary_controls = tk.Frame(self.seekbar_frame, bg='#222222')
        secondary_controls.pack(fill=tk.X, padx=10, pady=(0, 6))

        self.thumbnail_toggle_button = tk.Button(
            secondary_controls,
            text="サムネイル表示",
            width=12,
            command=self.toggle_thumbnail_panel
        )
        self.thumbnail_toggle_button.pack(side=tk.LEFT, padx=(0, 10))

        self.metadata_toggle_button = tk.Button(
            secondary_controls,
            text="情報欄表示",
            width=12,
            command=self.toggle_metadata_panel
        )
        self.metadata_toggle_button.pack(side=tk.LEFT)

        self.context_menu = tk.Menu(self.root, tearoff=0)
        self.context_menu.add_command(
            label="画像をファイルマネージャで表示",
            command=self.reveal_current_image_in_file_manager
        )
        self.context_menu.add_command(
            label="画像をクリップボードにコピー",
            command=self.copy_current_image_to_clipboard
        )

        self.interval_scale_var = tk.DoubleVar()
        self.interval_scale = tk.Scale(
            self.seekbar_frame,
            variable=self.interval_scale_var,
            from_=MIN_INTERVAL_SECONDS,
            to=MAX_INTERVAL_SECONDS,
            resolution=INTERVAL_STEP_SECONDS,
            orient=tk.HORIZONTAL,
            showvalue=False,
            bg='#222222',
            fg='white',
            troughcolor='#555555',
            highlightthickness=0,
            label="自動送り間隔 (秒)",
            command=self.on_interval_change
        )
        self.interval_scale.pack(fill=tk.X, padx=10, pady=(0, 8))
        self.seekbar_visible = False

        # 設定メニュー用フレーム
        self.menu_frame = tk.Frame(self.root, bg='#333333', padx=20, pady=20)
        self.menu_frame.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        title_label = tk.Label(self.menu_frame, text="画像ビューアー 設定", fg='white', bg='#333333', font=("Helvetica", 16, "bold"))
        title_label.pack(pady=(0, 20))

        self.folder_var = tk.StringVar(value="フォルダが選択されていません")
        tk.Label(self.menu_frame, textvariable=self.folder_var, fg='white', bg='#333333').pack(pady=5)
        
        tk.Button(self.menu_frame, text="1. フォルダを選択", command=self.select_folder, width=20).pack(pady=5)
        
        self.interval_var = tk.StringVar(value="5")
        interval_frame = tk.Frame(self.menu_frame, bg='#333333')
        interval_frame.pack(pady=5)
        tk.Label(interval_frame, text="自動送り間隔 (秒):", fg='white', bg='#333333').pack(side=tk.LEFT)
        tk.Entry(interval_frame, textvariable=self.interval_var, width=5).pack(side=tk.LEFT, padx=5)

        tk.Button(self.menu_frame, text="2. スライドショー開始", command=self.start_slideshow, width=20, bg='#4CAF50', fg='white').pack(pady=20)

        # 操作説明
        help_text = "【操作方法】\n・Escキー: 設定画面に戻る\n・→ / Space: 次の画像\n・←: 前の画像\n・P: 再生/一時停止\n・中央タップ: 操作パネル表示\n・操作パネル: 最大化切替 / フォルダ変更"
        tk.Label(self.menu_frame, text=help_text, fg='#AAAAAA', bg='#333333', justify=tk.LEFT).pack(pady=10)

    def select_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.set_folder(folder)

    def set_folder(self, folder):
        """対象フォルダの状態表示を更新"""
        self.folder_path = folder
        self.folder_var.set(f"選択中: {self.folder_path}")

    def format_interval_seconds(self, seconds):
        """表示用に秒数を整形"""
        if float(seconds).is_integer():
            return str(int(seconds))
        return f"{seconds:.1f}"

    def sync_interval_ui(self):
        """設定値と再生中UIの秒数表示を同期"""
        interval_seconds = self.interval_ms / 1000
        formatted = self.format_interval_seconds(interval_seconds)
        self.interval_var.set(formatted)
        self.interval_status_var.set(f"自動送り: {formatted}秒")
        if abs(self.interval_scale_var.get() - interval_seconds) > 0.001:
            self.interval_scale_var.set(interval_seconds)

    def update_play_pause_button(self):
        """再生状態に合わせてボタン表示を更新"""
        if self.is_playing:
            self.play_pause_button.config(text="一時停止", bg='#4CAF50')
        else:
            self.play_pause_button.config(text="再開", bg='#FF9800')

    def update_fullscreen_button(self):
        """フルスクリーン状態に合わせてボタン表示を更新"""
        if self.is_fullscreen:
            self.fullscreen_button.config(text="最大化解除")
        else:
            self.fullscreen_button.config(text="最大化")

    def update_thumbnail_buttons(self):
        """サムネイル関連ボタン表示を更新"""
        if self.thumbnail_visible:
            self.thumbnail_toggle_button.config(text="サムネイル非表示")
        else:
            self.thumbnail_toggle_button.config(text="サムネイル表示")

    def update_metadata_button(self):
        """情報欄トグルボタン表示を更新"""
        if self.metadata_visible:
            self.metadata_toggle_button.config(text="情報欄非表示")
        else:
            self.metadata_toggle_button.config(text="情報欄表示")

    def request_render(self, *, layout=False, image=False, metadata=False, thumbnail_highlight=False):
        """必要な描画更新を1回に集約して予約"""
        self.log_timeline(
            f"request_render layout={layout} image={image} metadata={metadata} "
            f"thumbnail_highlight={thumbnail_highlight}"
        )
        self.layout_dirty = self.layout_dirty or layout
        self.image_dirty = self.image_dirty or image
        self.metadata_dirty = self.metadata_dirty or metadata
        self.thumbnail_highlight_dirty = self.thumbnail_highlight_dirty or thumbnail_highlight
        if image:
            self.set_debug_hud_value("render_status", "pending")

        if self.render_scheduled:
            return

        self.render_scheduled = True
        self.render_after_id = self.root.after_idle(self.render_pending_updates)

    def log_resize_debug(self, message):
        """リサイズまわりのデバッグログを必要時だけ出す"""
        if not DEBUG_RESIZE_LOG:
            return
        print(f"[resize-debug {time.perf_counter():.6f}] {message}")

    def log_timeline(self, message):
        """描画と自動送りのタイムラインを必要時だけ出す"""
        if not DEBUG_TIMELINE_LOG:
            return
        self.timeline_event_seq += 1
        print(f"[timeline {time.perf_counter():.6f} #{self.timeline_event_seq}] {message}")

    def log_native_probe(self, hwnd_value, msg, wparam, lparam):
        if not DEBUG_RESIZE_LOG:
            return

        source = self.native_resize_hwnd_labels.get(hwnd_value, str(hwnd_value))
        if msg == WM_NCLBUTTONDOWN:
            self.log_resize_debug(
                f"native probe source={source} msg=WM_NCLBUTTONDOWN wparam={int(wparam)} lparam={int(lparam)}"
            )
        elif msg == WM_SYSCOMMAND:
            command = int(wparam) & 0xFFF0
            suffix = " SC_SIZE" if command == SC_SIZE else ""
            self.log_resize_debug(
                f"native probe source={source} msg=WM_SYSCOMMAND cmd=0x{command:04X} raw=0x{int(wparam):04X}{suffix}"
            )
        elif msg == WM_SIZE:
            self.log_resize_debug(
                f"native probe source={source} msg=WM_SIZE wparam={int(wparam)} lparam={int(lparam)}"
            )

    def set_debug_hud_value(self, key, value):
        """デバッグ HUD 用の最新値を保持する"""
        if not DEBUG_HUD:
            return
        self.debug_hud_data[key] = value
        self.request_debug_hud_refresh()

    def request_debug_hud_refresh(self):
        """HUD の画面更新を低頻度で予約する"""
        if not DEBUG_HUD or self.debug_hud_label is None:
            return
        self.debug_hud_dirty = True
        if self.debug_hud_after_id:
            return
        self.debug_hud_after_id = self.root.after(DEBUG_HUD_REFRESH_MS, self.flush_debug_hud)

    def flush_debug_hud(self):
        """低頻度で HUD 表示を更新する"""
        self.debug_hud_after_id = None
        if not DEBUG_HUD or self.debug_hud_label is None or not self.debug_hud_dirty:
            return

        self.debug_hud_dirty = False
        current_path = self.get_current_image_path()
        current_name = os.path.basename(current_path) if current_path else "-"
        lines = [
            f"idx: {self.current_index}",
            f"img: {current_name}",
            f"play: {self.is_playing}  resize: {self.is_live_resizing}",
            f"thumb: {self.thumbnail_visible}  meta: {self.metadata_visible}",
            f"render: {self.debug_hud_data.get('render_status', '-')} {self.debug_hud_data.get('render_ms', '-')}",
            f"preview: {self.debug_hud_data.get('preview_status', '-')} {self.debug_hud_data.get('preview_ms', '-')}",
            f"meta: {self.debug_hud_data.get('meta_status', '-')} {self.debug_hud_data.get('meta_ms', '-')}",
            f"thumb: {self.debug_hud_data.get('thumb_status', '-')} {self.debug_hud_data.get('thumb_ms', '-')}",
            f"event: {self.debug_hud_data.get('event', '-')}",
        ]
        self.debug_hud_label.config(text="\n".join(lines))

    def render_pending_updates(self):
        """予約済みのUI更新をまとめて反映"""
        start = time.perf_counter()
        self.log_timeline(
            f"render_pending_updates start layout_dirty={self.layout_dirty} image_dirty={self.image_dirty} "
            f"metadata_dirty={self.metadata_dirty} thumbnail_dirty={self.thumbnail_highlight_dirty}"
        )
        self.render_scheduled = False
        self.render_after_id = None

        if self.layout_dirty:
            image_followup = self.image_dirty
            metadata_followup = self.metadata_dirty
            thumbnail_followup = self.thumbnail_highlight_dirty
            self.apply_panel_layout()
            self.layout_dirty = False
            self.image_dirty = False
            self.metadata_dirty = False
            self.thumbnail_highlight_dirty = False

            if image_followup or metadata_followup or thumbnail_followup:
                self.request_render(
                    image=image_followup,
                    metadata=metadata_followup,
                    thumbnail_highlight=thumbnail_followup
                )
            self.log_timeline(f"render_pending_updates end duration={(time.perf_counter() - start) * 1000:.1f}ms")
            return

        if self.image_dirty:
            self.render_current_image()
            self.image_dirty = False
        else:
            if self.metadata_dirty:
                self.schedule_metadata_refresh()
                self.metadata_dirty = False
            if self.thumbnail_highlight_dirty:
                self.schedule_thumbnail_highlight()
                self.thumbnail_highlight_dirty = False
        self.log_timeline(f"render_pending_updates end duration={(time.perf_counter() - start) * 1000:.1f}ms")

    def get_current_image_path(self):
        """現在選択中の画像パスを返す"""
        if 0 <= self.current_index < len(self.image_list):
            return self.image_list[self.current_index]
        return None

    def clear_current_image_cache(self):
        """迴ｾ蝨ｨ逕ｻ蜒上・繧ｭ繝｣繝・す繝･繧貞ｧ｣髯､"""
        if self.current_source_image is not None:
            try:
                self.current_source_image.close()
            except Exception:
                pass
        self.current_source_image = None
        self.current_source_path = None
        self.current_render_image = None
        self.current_render_path = None
        self.current_render_size = None
        self.current_render_resample = None

    def begin_resize_session(self):
        """リサイズ中の軽量表示モードへ切り替え"""
        if self.is_live_resizing:
            self.log_resize_debug("begin_resize_session skipped: already live resizing")
            return

        start = time.perf_counter()
        self.log_resize_debug(
            f"begin_resize_session start metadata_visible={self.metadata_visible} "
            f"overlay_visible={self.metadata_overlay_visible} thumbnail_visible={self.thumbnail_visible}"
        )

        if self.render_after_id:
            self.root.after_cancel(self.render_after_id)
            self.render_after_id = None
            self.log_resize_debug("canceled pending render_after_id")
            self.set_debug_hud_value("render_status", "idle")
        self.render_scheduled = False

        if self.resize_preview_after_id:
            self.root.after_cancel(self.resize_preview_after_id)
            self.resize_preview_after_id = None
            self.log_resize_debug("canceled pending resize_preview_after_id")
            self.set_debug_hud_value("preview_status", "idle")

        self.cancel_metadata_refresh()
        self.cancel_thumbnail_highlight()
        self.resume_play_after_resize = self.is_playing
        if self.resume_play_after_resize:
            self.cancel_scheduled_image()
            self.log_resize_debug("paused scheduled slideshow advance during resize")

        self.is_live_resizing = True
        self.set_debug_hud_value("event", "resize-start")
        self.log_resize_debug(f"begin_resize_session end duration={(time.perf_counter() - start) * 1000:.1f}ms")

    def end_resize_session(self):
        """リサイズ完了後に通常表示へ戻す"""
        start = time.perf_counter()
        self.log_resize_debug("end_resize_session start")
        self.is_live_resizing = False
        self.request_render(
            layout=True,
            image=True,
            metadata=self.metadata_visible,
            thumbnail_highlight=self.thumbnail_visible,
        )
        if self.resume_play_after_resize and self.is_playing and self.is_slideshow_active:
            self.schedule_next_image()
        self.resume_play_after_resize = False
        self.set_debug_hud_value("event", "resize-end")
        self.log_resize_debug(f"end_resize_session end duration={(time.perf_counter() - start) * 1000:.1f}ms")

    def hide_metadata_overlay_now(self):
        """情報欄を可能な限り即時に隠す"""
        if not self.metadata_overlay_visible:
            self.log_resize_debug("hide_metadata_overlay_now skipped")
            return

        start = time.perf_counter()
        self.log_resize_debug("hide_metadata_overlay_now start")
        self.metadata_frame.grid_remove()
        self.metadata_overlay_visible = False
        self.log_resize_debug(f"hide_metadata_overlay_now end duration={(time.perf_counter() - start) * 1000:.1f}ms")

    def apply_panel_layout(self):
        """サムネイル帯と情報欄の表示状態をレイアウトへ反映"""
        if not self.is_slideshow_active:
            if self.metadata_overlay_visible:
                self.metadata_frame.grid_remove()
                self.metadata_overlay_visible = False
            self.thumbnail_frame.grid_remove()
            self.update_thumbnail_buttons()
            self.update_metadata_button()
            return

        show_metadata_overlay = self.metadata_visible and not self.panels_hidden_for_resize
        show_thumbnail_strip = self.thumbnail_visible and not self.panels_hidden_for_resize

        if show_metadata_overlay:
            if not self.metadata_overlay_visible:
                self.metadata_frame.grid()
                self.metadata_overlay_visible = True
        elif self.metadata_overlay_visible:
            self.metadata_frame.grid_remove()
            self.metadata_overlay_visible = False

        if show_thumbnail_strip:
            self.thumbnail_frame.grid()
        else:
            self.thumbnail_frame.grid_remove()

        self.update_thumbnail_buttons()
        self.update_metadata_button()

    def install_native_resize_hook(self):
        """Windows ではネイティブメッセージでサイズ変更開始/終了を拾う"""
        if not sys.platform.startswith("win") or self.native_resize_hook_installed:
            return

        try:
            self.root.update_idletasks()
            user32 = ctypes.windll.user32
            get_parent = user32.GetParent
            get_ancestor = user32.GetAncestor
            get_window = user32.GetWindow
            set_window_long_ptr = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            call_window_proc = user32.CallWindowProcW

            wndproc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )

            get_parent.restype = wintypes.HWND
            get_parent.argtypes = [wintypes.HWND]
            get_ancestor.restype = wintypes.HWND
            get_ancestor.argtypes = [wintypes.HWND, wintypes.UINT]
            get_window.restype = wintypes.HWND
            get_window.argtypes = [wintypes.HWND, wintypes.UINT]
            set_window_long_ptr.restype = ctypes.c_void_p
            set_window_long_ptr.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            call_window_proc.restype = ctypes.c_ssize_t
            call_window_proc.argtypes = [
                ctypes.c_void_p,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]

            def window_proc(hwnd_value, msg, wparam, lparam):
                if msg in {
                    WM_NCLBUTTONDOWN,
                    WM_SYSCOMMAND,
                    WM_SIZE,
                    WM_WINDOWPOSCHANGING,
                    WM_WINDOWPOSCHANGED,
                }:
                    self.log_native_probe(hwnd_value, msg, wparam, lparam)
                if msg == WM_ENTERSIZEMOVE:
                    self.last_native_resize_enter_at = time.perf_counter()
                    self.last_native_resize_source = self.native_resize_hwnd_labels.get(hwnd_value, str(hwnd_value))
                    self.root.after(0, self.on_native_resize_enter)
                elif msg == WM_EXITSIZEMOVE:
                    self.last_native_resize_exit_at = time.perf_counter()
                    self.last_native_resize_source = self.native_resize_hwnd_labels.get(hwnd_value, str(hwnd_value))
                    self.root.after(0, self.on_native_resize_exit)

                default_proc = self.native_resize_orig_procs.get(hwnd_value)
                if default_proc:
                    return call_window_proc(default_proc, hwnd_value, msg, wparam, lparam)
                return 0

            self.window_proc = wndproc_type(window_proc)
            base_hwnd = self.root.winfo_id()
            candidates = []
            seen = set()

            def add_candidate(label, hwnd_value):
                if not hwnd_value or hwnd_value in seen:
                    return
                seen.add(hwnd_value)
                candidates.append((label, hwnd_value))

            parent_hwnd = get_parent(base_hwnd)
            root_hwnd = get_ancestor(base_hwnd, GA_ROOT)
            rootowner_hwnd = get_ancestor(base_hwnd, GA_ROOTOWNER)
            owner_base_hwnd = get_window(base_hwnd, GW_OWNER)
            parent_parent_hwnd = get_parent(parent_hwnd) if parent_hwnd else 0
            parent_root_hwnd = get_ancestor(parent_hwnd, GA_ROOT) if parent_hwnd else 0
            parent_rootowner_hwnd = get_ancestor(parent_hwnd, GA_ROOTOWNER) if parent_hwnd else 0
            owner_parent_hwnd = get_window(parent_hwnd, GW_OWNER) if parent_hwnd else 0
            owner_root_hwnd = get_window(root_hwnd, GW_OWNER) if root_hwnd else 0

            add_candidate("parent", parent_hwnd)
            add_candidate("root", root_hwnd)
            add_candidate("rootowner", rootowner_hwnd)
            add_candidate("owner_base", owner_base_hwnd)
            add_candidate("parent_parent", parent_parent_hwnd)
            add_candidate("parent_root", parent_root_hwnd)
            add_candidate("parent_rootowner", parent_rootowner_hwnd)
            add_candidate("owner_parent", owner_parent_hwnd)
            add_candidate("owner_root", owner_root_hwnd)
            add_candidate("base", base_hwnd)

            self.log_resize_debug(
                "native resize hwnd graph="
                + ", ".join(
                    [
                        f"base:{base_hwnd}",
                        f"parent:{parent_hwnd}",
                        f"root:{root_hwnd}",
                        f"rootowner:{rootowner_hwnd}",
                        f"owner_base:{owner_base_hwnd}",
                        f"parent_parent:{parent_parent_hwnd}",
                        f"parent_root:{parent_root_hwnd}",
                        f"parent_rootowner:{parent_rootowner_hwnd}",
                        f"owner_parent:{owner_parent_hwnd}",
                        f"owner_root:{owner_root_hwnd}",
                    ]
                )
            )

            for label, hwnd in candidates:
                original_proc = set_window_long_ptr(hwnd, GWLP_WNDPROC, self.window_proc)
                if original_proc:
                    self.native_resize_orig_procs[hwnd] = original_proc
                    self.native_resize_hwnd_labels[hwnd] = label
                    self.native_resize_hwnds.append(hwnd)

            if self.native_resize_hwnds:
                self.native_resize_hwnd = self.native_resize_hwnds[0]
                self.default_wndproc = self.native_resize_orig_procs[self.native_resize_hwnd]
                self.native_resize_hook_installed = True
                self.log_resize_debug(
                    "native resize hook candidates="
                    + ", ".join(
                        f"{self.native_resize_hwnd_labels[hwnd]}:{hwnd}"
                        for hwnd in self.native_resize_hwnds
                    )
                )
            else:
                self.native_resize_hook_installed = False
        except Exception as exc:
            print(f"Native resize hook unavailable: {exc}")
            self.window_proc = None
            self.default_wndproc = None
            self.native_resize_hwnd = None
            self.native_resize_hwnds = []
            self.native_resize_hwnd_labels = {}
            self.native_resize_orig_procs = {}
            self.native_resize_hook_installed = False

    def uninstall_native_resize_hook(self):
        """Windows のサブクラス化を元に戻す"""
        if (
            not sys.platform.startswith("win")
            or not self.native_resize_hook_installed
            or not self.native_resize_hwnds
        ):
            return

        try:
            user32 = ctypes.windll.user32
            set_window_long_ptr = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            set_window_long_ptr.restype = ctypes.c_void_p
            set_window_long_ptr.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_void_p]
            for hwnd in self.native_resize_hwnds:
                original_proc = self.native_resize_orig_procs.get(hwnd)
                if original_proc:
                    set_window_long_ptr(hwnd, GWLP_WNDPROC, original_proc)
        except Exception as exc:
            print(f"Failed to restore window procedure: {exc}")
        finally:
            self.native_resize_hook_installed = False
            self.native_resize_hwnd = None
            self.native_resize_hwnds = []
            self.native_resize_hwnd_labels = {}
            self.native_resize_orig_procs = {}
            self.default_wndproc = None
            self.window_proc = None

    def on_native_resize_enter(self):
        """Windows のサイズ変更モード開始を処理"""
        if not self.is_slideshow_active:
            return

        if self.last_native_resize_enter_at is not None:
            delay_ms = (time.perf_counter() - self.last_native_resize_enter_at) * 1000
            self.log_resize_debug(
                f"on_native_resize_enter source={self.last_native_resize_source} callback delay={delay_ms:.1f}ms"
            )
        else:
            self.log_resize_debug("on_native_resize_enter callback delay=unknown")

        self.native_resize_active = True
        self.begin_resize_session()

    def on_native_resize_exit(self):
        """Windows のサイズ変更モード終了を処理"""
        if not self.native_resize_active:
            return

        if self.last_native_resize_exit_at is not None:
            delay_ms = (time.perf_counter() - self.last_native_resize_exit_at) * 1000
            self.log_resize_debug(
                f"on_native_resize_exit source={self.last_native_resize_source} callback delay={delay_ms:.1f}ms"
            )
        else:
            self.log_resize_debug("on_native_resize_exit callback delay=unknown")

        self.native_resize_active = False

        if self.resize_after_id:
            self.root.after_cancel(self.resize_after_id)
            self.resize_after_id = None
        if self.resize_preview_after_id:
            self.root.after_cancel(self.resize_preview_after_id)
            self.resize_preview_after_id = None

        if self.is_slideshow_active:
            self.end_resize_session()
        else:
            self.is_live_resizing = False
            self.panels_hidden_for_resize = False

    def set_metadata_text(self, text):
        """情報欄のテキストを更新"""
        if text == self.metadata_text_value:
            return

        self.metadata_text_value = text
        self.metadata_text.configure(state=tk.NORMAL)
        self.metadata_text.delete("1.0", tk.END)
        self.metadata_text.insert("1.0", text)
        self.metadata_text.configure(state=tk.DISABLED)

    def cancel_metadata_refresh(self):
        """遅延中の情報欄更新を取り消す"""
        if self.metadata_refresh_after_id:
            self.root.after_cancel(self.metadata_refresh_after_id)
            self.metadata_refresh_after_id = None
        self.set_debug_hud_value("meta_status", "idle")
        self.metadata_refresh_target_path = None

    def schedule_metadata_refresh(self, image_path=None):
        """情報欄更新を短く遅延させて予約"""
        self.cancel_metadata_refresh()

        target_path = image_path or self.get_current_image_path()
        if not target_path or not self.metadata_visible:
            return

        self.metadata_refresh_target_path = target_path
        self.set_debug_hud_value("meta_status", "pending")
        if METADATA_REFRESH_DELAY_MS <= 0:
            self.metadata_refresh_after_id = None
            self.flush_metadata_refresh()
            return
        self.metadata_refresh_after_id = self.root.after(
            METADATA_REFRESH_DELAY_MS,
            self.flush_metadata_refresh,
        )

    def flush_metadata_refresh(self):
        """遅延予約した情報欄更新を実行"""
        start = time.perf_counter()
        self.metadata_refresh_after_id = None
        target_path = self.metadata_refresh_target_path
        self.metadata_refresh_target_path = None

        if not self.metadata_visible or not target_path or target_path != self.get_current_image_path():
            self.set_debug_hud_value("meta_status", "idle")
            return

        self.set_debug_hud_value("meta_status", "running")
        self.log_timeline(f"flush_metadata_refresh start path={os.path.basename(target_path)}")
        image = self.current_source_image if self.current_source_path == target_path else None
        self.refresh_metadata_display(image_path=target_path, image=image)
        self.metadata_dirty = False
        duration_ms = (time.perf_counter() - start) * 1000
        self.set_debug_hud_value("meta_ms", f"{duration_ms:.1f}ms")
        self.set_debug_hud_value("meta_status", "done")
        self.set_debug_hud_value("event", "metadata")
        self.log_timeline(f"flush_metadata_refresh end duration={duration_ms:.1f}ms")

    def cancel_thumbnail_highlight(self):
        """遅延中のサムネイル強調更新を取り消す"""
        if self.thumbnail_highlight_after_id:
            self.root.after_cancel(self.thumbnail_highlight_after_id)
            self.thumbnail_highlight_after_id = None
        self.set_debug_hud_value("thumb_status", "idle")
        self.thumbnail_highlight_target_path = None

    def schedule_thumbnail_highlight(self, image_path=None):
        """サムネイル強調更新を短く遅延させて予約"""
        self.cancel_thumbnail_highlight()

        target_path = image_path or self.get_current_image_path()
        if not target_path or not self.thumbnail_visible:
            return

        self.thumbnail_highlight_target_path = target_path
        self.set_debug_hud_value("thumb_status", "pending")
        if THUMBNAIL_HIGHLIGHT_DELAY_MS <= 0:
            self.thumbnail_highlight_after_id = None
            self.flush_thumbnail_highlight()
            return
        self.thumbnail_highlight_after_id = self.root.after(
            THUMBNAIL_HIGHLIGHT_DELAY_MS,
            self.flush_thumbnail_highlight,
        )

    def flush_thumbnail_highlight(self):
        """遅延予約したサムネイル強調更新を実行"""
        start = time.perf_counter()
        self.thumbnail_highlight_after_id = None
        target_path = self.thumbnail_highlight_target_path
        self.thumbnail_highlight_target_path = None

        if not self.thumbnail_visible or not target_path or target_path != self.get_current_image_path():
            self.set_debug_hud_value("thumb_status", "idle")
            return

        self.set_debug_hud_value("thumb_status", "running")
        self.log_timeline(f"flush_thumbnail_highlight start path={os.path.basename(target_path)}")
        self.highlight_current_thumbnail()
        self.thumbnail_highlight_dirty = False
        duration_ms = (time.perf_counter() - start) * 1000
        self.set_debug_hud_value("thumb_ms", f"{duration_ms:.1f}ms")
        self.set_debug_hud_value("thumb_status", "done")
        self.set_debug_hud_value("event", "thumbnail")
        self.log_timeline(f"flush_thumbnail_highlight end duration={duration_ms:.1f}ms")

    def show_context_menu(self, event):
        """逕ｻ蜒上・蜿ｳ繧ｯ繝ｪ繝・け繝｡繝九Η繝ｼ繧呈款縺・"""
        if not self.get_current_image_path():
            return

        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def copy_current_image_to_clipboard(self, event=None):
        """迴ｾ蝨ｨ縺ｮ逕ｻ蜒上ｒ繧ｯ繝ｪ繝・・繝懊・繝峨↓繧ｳ繝斐・縺吶ｋ"""
        image_path = self.get_current_image_path()
        if not image_path:
            return "break"

        if os.name != "nt":
            messagebox.showinfo("情報", "画像のクリップボードコピーは現在 Windows でのみ対応しています。")
            return "break"

        try:
            with Image.open(image_path) as image:
                self.copy_pil_image_to_clipboard(image)
        except Exception as exc:
            messagebox.showerror("エラー", f"画像をクリップボードにコピーできませんでした。\n{exc}")

        return "break"

    def reveal_current_image_in_file_manager(self):
        """迴ｾ蝨ｨ縺ｮ逕ｻ蜒上ｒ繝輔ぃ繧､繝ｫ繝槭ロ繝ｼ繧ｸ繝｣縺ｧ陦ｨ遉ｺ"""
        image_path = self.get_current_image_path()
        if not image_path:
            return

        try:
            self.reveal_in_file_manager(image_path)
        except Exception as exc:
            messagebox.showerror("エラー", f"ファイルマネージャで開けませんでした。\n{exc}")

    def reveal_in_file_manager(self, image_path):
        """OS 縺ｫ蜷医ｏ縺帙※逕ｻ蜒上ｒ繝輔ぃ繧､繝ｫ繝槭ロ繝ｼ繧ｸ繝｣縺ｧ陦ｨ遉ｺ"""
        normalized_path = os.path.abspath(image_path)
        parent_dir = os.path.dirname(normalized_path)

        if sys.platform.startswith("win"):
            if not os.path.exists(normalized_path):
                raise FileNotFoundError(normalized_path)
            subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(normalized_path)])
            return

        if sys.platform == "darwin":
            subprocess.run(["open", "-R", normalized_path], check=True)
            return

        if sys.platform.startswith("linux"):
            # Linux 縺ｧ縺ｯ髢狗匱繝輔ぃ繧､繝ｫ閾ｪ菴懈・縺ｮ蝣ｴ蜷医′澶ｯ縺・◆繧√√∪縺壹・隕九▽縺九ｋ縺ｮ縺ｯ莠悟・繝輔か繝ｫ繝縺ｮ繝ｼ繝励Φ縺ｫ縺ｨ縺ｩ繧√ｋ
            subprocess.run(["xdg-open", parent_dir], check=True)
            return

        raise OSError(f"Unsupported platform: {sys.platform}")

    def copy_pil_image_to_clipboard(self, image):
        """PIL Image を Windows クリップボードへ転送"""
        temp_copy_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".clipboard_copy_tmp.png")
        image.save(temp_copy_path, "PNG")

        script = f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$path = '{temp_copy_path.replace("'", "''")}'
$bitmap = [System.Drawing.Bitmap]::FromFile($path)
try {{
    [System.Windows.Forms.Clipboard]::SetImage($bitmap)
}}
finally {{
    $bitmap.Dispose()
    Remove-Item -LiteralPath $path -ErrorAction SilentlyContinue
}}
"""
        self.run_powershell_script(script)

    def run_powershell_script(self, script):
        """Windows PowerShell スクリプトを実行する"""
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-STA",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            check=True,
        )

    def toggle_thumbnail_panel(self):
        """サムネイル帯の表示・非表示を切り替える"""
        if not self.is_slideshow_active:
            return

        self.thumbnail_visible = not self.thumbnail_visible
        if not self.thumbnail_visible:
            self.cancel_thumbnail_highlight()
            self.cancel_thumbnail_follow()
        self.request_render(layout=True, image=True, thumbnail_highlight=True)

    def toggle_metadata_panel(self):
        """情報欄の表示・非表示を切り替える"""
        if not self.is_slideshow_active:
            return

        self.metadata_visible = not self.metadata_visible
        if not self.metadata_visible:
            self.cancel_metadata_refresh()
        self.request_render(layout=True, metadata=True)

    def clear_image_queue(self):
        """未処理の監視イベントを破棄"""
        while not self.image_queue.empty():
            self.image_queue.get_nowait()

    def clear_thumbnail_widgets(self):
        """既存サムネイルUIを破棄"""
        self.cancel_thumbnail_follow()
        for widget in self.thumbnail_inner.winfo_children():
            widget.destroy()
        self.thumbnail_photos = {}
        self.thumbnail_buttons = {}
        self.thumbnail_items = {}

    def on_thumbnail_inner_configure(self, event=None):
        """サムネイル内側フレームのサイズ変更をキャンバスへ反映"""
        self.thumbnail_canvas.configure(scrollregion=self.thumbnail_canvas.bbox("all"))

    def on_thumbnail_canvas_configure(self, event):
        """サムネイルキャンバス幅を内側フレームへ反映"""
        self.thumbnail_canvas.itemconfigure(self.thumbnail_window, height=event.height)

    def on_thumbnail_mousewheel(self, event):
        """Shift+ホイールでサムネイル帯を横スクロール"""
        if not self.thumbnail_visible:
            return

        step = -1 if event.delta > 0 else 1
        self.thumbnail_canvas.xview_scroll(step * 3, "units")

    def select_image_from_thumbnail(self, index):
        """サムネイルクリックで画像を切り替える"""
        if 0 <= index < len(self.image_list):
            self.show_image(index)
            self.schedule_next_image()

    def refresh_thumbnail_strip(self):
        """画像リストに合わせてサムネイル帯を再構築"""
        self.clear_thumbnail_widgets()

        for index, image_path in enumerate(self.image_list):
            item_frame = tk.Frame(self.thumbnail_inner, bg='#161616', padx=4, pady=4)
            item_frame.pack(side=tk.LEFT)

            try:
                with Image.open(image_path) as thumb_image:
                    thumb_image = thumb_image.copy()
                    thumb_image.thumbnail((112, 96), Image.Resampling.LANCZOS)
            except (OSError, UnidentifiedImageError):
                thumb_image = Image.new("RGB", (112, 96), color="#444444")

            thumb_photo = ImageTk.PhotoImage(thumb_image)
            self.thumbnail_photos[image_path] = thumb_photo

            button = tk.Button(
                item_frame,
                image=thumb_photo,
                width=116,
                height=100,
                bd=2,
                relief=tk.FLAT,
                bg='#222222',
                activebackground='#444444',
                command=lambda i=index: self.select_image_from_thumbnail(i)
            )
            button.pack()

            label = tk.Label(
                item_frame,
                text=str(index + 1),
                bg='#161616',
                fg='white'
            )
            label.pack(pady=(4, 0))
            self.thumbnail_buttons[image_path] = button
            self.thumbnail_items[image_path] = item_frame

        self.highlight_current_thumbnail()
        self.on_thumbnail_inner_configure()

    def on_root_configure(self, event=None):
        """ウィンドウサイズ変更時にオーバーレイ位置を追従"""
        if not self.is_slideshow_active:
            return

        if event is not None and event.widget is not self.root:
            return

        width = event.width if event is not None else self.root.winfo_width()
        height = event.height if event is not None else self.root.winfo_height()
        current_size = (width, height)
        size_changed = current_size != self.last_root_size
        self.last_root_size = current_size

        if not size_changed:
            return

        if self.native_resize_active:
            self.log_resize_debug(f"configure during native resize size={current_size}")

        resize_just_started = not self.is_live_resizing
        if resize_just_started:
            self.log_resize_debug(f"configure begin fallback size={current_size}")
            self.begin_resize_session()

        if self.current_source_image is not None and self.resize_preview_after_id is None:
            self.set_debug_hud_value("preview_status", "pending")
            self.resize_preview_after_id = self.root.after_idle(self.flush_resize_preview)

        if self.resize_after_id:
            self.root.after_cancel(self.resize_after_id)
        self.resize_after_id = self.root.after(120, self.flush_resize_updates)

    def flush_resize_preview(self):
        """リサイズ中の軽量プレビュー描画"""
        self.resize_preview_after_id = None
        if not self.is_slideshow_active or not self.is_live_resizing or self.current_source_image is None:
            self.set_debug_hud_value("preview_status", "idle")
            return

        self.set_debug_hud_value("preview_status", "running")
        self.render_current_image(
            resample=Image.Resampling.BILINEAR,
            refresh_metadata=False,
            refresh_thumbnail=False,
            use_preview_source=True,
        )
        self.set_debug_hud_value("preview_status", "done")

    def flush_resize_updates(self):
        """連続するConfigureイベント後に再描画をまとめて実行"""
        self.resize_after_id = None
        if not self.is_slideshow_active:
            return

        if self.native_resize_active:
            return

        self.end_resize_session()

    def update_metadata_overlay_geometry(self):
        """情報欄は同一ウィンドウ内の右カラムで管理する"""
        return

    def highlight_current_thumbnail(self):
        """現在表示中のサムネイルを強調表示"""
        current_path = None
        if 0 <= self.current_index < len(self.image_list):
            current_path = self.image_list[self.current_index]

        if not self.thumbnail_visible or not current_path:
            self.cancel_thumbnail_follow()
            return

        for image_path, button in self.thumbnail_buttons.items():
            if image_path == current_path:
                button.config(relief=tk.SOLID, bg='#4CAF50', highlightbackground='#4CAF50')
            else:
                button.config(relief=tk.FLAT, bg='#222222', highlightbackground='#222222')

        self.schedule_thumbnail_follow(current_path)

    def schedule_thumbnail_follow(self, image_path):
        """逕ｻ蜒丈ｸ庚曄繧剃ｽ懈・縺励※繧ｵ繝繝阪う繝ｫ追従繧定ｺ育ｴ・"""
        self.cancel_thumbnail_follow()
        self.thumbnail_follow_path = image_path
        self.thumbnail_scroll_after_id = self.root.after_idle(self.flush_thumbnail_follow)

    def cancel_thumbnail_follow(self):
        """譌｢蟄倥＠縺溘し繝繝阪う繝ｫ追従繧貞ｧ｣髯､"""
        if self.thumbnail_scroll_after_id:
            self.root.after_cancel(self.thumbnail_scroll_after_id)
            self.thumbnail_scroll_after_id = None
        self.thumbnail_follow_path = None

    def flush_thumbnail_follow(self):
        """莠育ｴ・∩縺ｮ繧ｵ繝繝阪う繝ｫ追従繧呈悽逕ｻ"""
        self.thumbnail_scroll_after_id = None
        if self.thumbnail_follow_path:
            path = self.thumbnail_follow_path
            self.thumbnail_follow_path = None
            self.scroll_thumbnail_into_view(path)

    def scroll_thumbnail_into_view(self, image_path):
        """迴ｾ蝨ｨ縺ｮ繧ｵ繝繝阪う繝ｫ縺後∪縺ｨ繧∝商隱ｿ蜿ｯ閭ｽ蝣ｴ謨ｰ蜀・・"""
        item_frame = self.thumbnail_items.get(image_path)
        if not self.thumbnail_visible or item_frame is None or not item_frame.winfo_exists():
            return

        inner_width = self.thumbnail_inner.winfo_width()
        canvas_width = self.thumbnail_canvas.winfo_width()
        if inner_width <= 0 or canvas_width <= 0:
            return

        item_x = item_frame.winfo_x()
        item_width = item_frame.winfo_width()
        item_center = item_x + (item_width / 2)
        target_left = item_center - (canvas_width / 2)
        max_left = max(0, inner_width - canvas_width)
        clamped_left = min(max(0, target_left), max_left)
        self.thumbnail_canvas.xview_moveto(clamped_left / max(1, inner_width))

    def build_metadata_text(self, image_path, image):
        """画像メタ情報を表示用文字列へ整形"""
        lines = [
            f"ファイル: {os.path.basename(image_path)}",
            f"パス: {image_path}",
            f"形式: {image.format or 'Unknown'}",
            f"サイズ: {image.size[0]} x {image.size[1]}",
            f"更新日時: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(os.path.getmtime(image_path)))}",
        ]

        png_parameters = image.info.get("parameters")
        if png_parameters:
            lines.extend(["", "[A1111 / PNG Parameters]", png_parameters])

        other_info = []
        for key, value in image.info.items():
            if key == "parameters":
                continue
            other_info.append(f"{key}: {value}")

        if other_info:
            lines.extend(["", "[Image Info]"] + other_info)

        exif_lines = []
        exif_data = image.getexif()
        if exif_data:
            for tag_id, value in exif_data.items():
                tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                exif_lines.append(f"{tag_name}: {value}")

        if exif_lines:
            lines.extend(["", "[EXIF]"] + exif_lines)

        if len(lines) == 5:
            lines.extend(["", "画像内メタ情報は見つかりませんでした。"])

        return "\n".join(lines)

    def get_metadata_text_for_path(self, image_path, image=None):
        """画像ごとのメタ情報文字列をキャッシュして返す"""
        cached = self.metadata_cache.get(image_path)
        if cached is not None:
            return cached

        close_image = False
        if image is None:
            image = Image.open(image_path)
            close_image = True

        try:
            metadata_text = self.build_metadata_text(image_path, image)
            self.metadata_cache[image_path] = metadata_text
            return metadata_text
        finally:
            if close_image:
                image.close()

    def refresh_metadata_display(self, image_path=None, image=None):
        """情報欄が表示中のときだけメタ情報を更新"""
        if not self.metadata_visible:
            return

        current_path = image_path or self.get_current_image_path()
        if not current_path:
            self.set_metadata_text("画像情報を表示するには、再生中に情報欄を開いてください。")
            return

        self.set_metadata_text(self.get_metadata_text_for_path(current_path, image=image))

    def get_image_viewport_size(self):
        """現在の画像表示領域サイズを返す"""
        viewport_width = self.image_area.winfo_width()
        viewport_height = self.image_area.winfo_height()

        if viewport_width <= 1 or viewport_height <= 1:
            viewport_width = self.root.winfo_screenwidth()
            viewport_height = self.root.winfo_screenheight()

        return viewport_width, viewport_height

    def request_image_render_if_needed(self):
        """現在の画像と表示領域が変わったときだけ再描画を予約"""
        image_path = self.get_current_image_path()
        if not image_path or self.current_source_image is None or self.current_source_path != image_path:
            self.request_render(image=True, metadata=self.metadata_visible, thumbnail_highlight=self.thumbnail_visible)
            return

        screen_width, screen_height = self.get_image_viewport_size()
        img_width, img_height = self.current_source_image.size
        ratio = min(screen_width / img_width, screen_height / img_height)
        target_size = (int(img_width * ratio), int(img_height * ratio))

        if self.current_render_path != image_path or self.current_render_size != target_size:
            self.request_render(image=True, metadata=self.metadata_visible, thumbnail_highlight=self.thumbnail_visible)

    def render_current_image(
        self,
        *,
        resample=Image.Resampling.LANCZOS,
        refresh_metadata=True,
        refresh_thumbnail=True,
        use_preview_source=False,
    ):
        """現在選択中の画像を再描画"""
        start = time.perf_counter()
        image_path = self.get_current_image_path()
        if not image_path:
            self.image_label.config(image='')
            self.photo = None
            self.clear_current_image_cache()
            return

        self.log_timeline(
            f"render_current_image start path={os.path.basename(image_path)} "
            f"resample={resample} refresh_metadata={refresh_metadata} "
            f"refresh_thumbnail={refresh_thumbnail} preview={use_preview_source}"
        )
        if use_preview_source:
            self.set_debug_hud_value("preview_status", "running")
        else:
            self.set_debug_hud_value("render_status", "running")

        try:
            if self.current_source_path != image_path or self.current_source_image is None:
                self.clear_current_image_cache()

                for _ in range(3):
                    try:
                        with Image.open(image_path) as loaded_image:
                            self.current_source_image = loaded_image.copy()
                        self.current_source_path = image_path
                        break
                    except IOError:
                        time.sleep(0.5)
                else:
                    print(f"Failed to open image: {image_path}")
                    return

            if use_preview_source and self.current_render_image is not None:
                img = self.current_render_image
            else:
                img = self.current_source_image

            screen_width, screen_height = self.get_image_viewport_size()

            img_width, img_height = img.size
            ratio = min(screen_width / img_width, screen_height / img_height)
            new_width = int(img_width * ratio)
            new_height = int(img_height * ratio)

            render_size = (new_width, new_height)
            can_reuse_render = (
                self.current_render_path == image_path
                and self.current_render_size == render_size
                and self.current_render_resample == resample
                and self.photo is not None
            )

            if can_reuse_render:
                self.log_resize_debug(f"render_current_image reused existing render size={render_size}")
            else:
                rendered_image = img.resize(render_size, resample)
                self.current_render_image = rendered_image.copy()
                self.current_render_path = image_path
                self.current_render_size = render_size
                self.current_render_resample = resample

                self.photo = ImageTk.PhotoImage(rendered_image)
                self.image_label.config(image=self.photo)

            if self.seekbar_var.get() != self.current_index + 1:
                self.seekbar_var.set(self.current_index + 1)

            if refresh_metadata:
                self.schedule_metadata_refresh(image_path=image_path)
                self.metadata_dirty = False
            if refresh_thumbnail:
                self.schedule_thumbnail_highlight(image_path=image_path)
                self.thumbnail_highlight_dirty = False

            duration_ms = (time.perf_counter() - start) * 1000
            if use_preview_source:
                self.set_debug_hud_value("preview_ms", f"{duration_ms:.1f}ms")
                self.set_debug_hud_value("preview_status", "done")
            else:
                self.set_debug_hud_value("render_ms", f"{duration_ms:.1f}ms")
                self.set_debug_hud_value("render_status", "done")
            self.set_debug_hud_value("event", "preview" if use_preview_source else "render")
            self.log_timeline(f"render_current_image end duration={duration_ms:.1f}ms")

        except Exception as e:
            if use_preview_source:
                self.set_debug_hud_value("preview_status", "idle")
            else:
                self.set_debug_hud_value("render_status", "idle")
            print(f"Error loading {image_path}: {e}")

    def stop_observer(self):
        """フォルダ監視を停止"""
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None

    def cancel_scheduled_image(self):
        """予約済みの自動送りを解除"""
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None
        self.set_debug_hud_value("event", "schedule-cancel")

    def schedule_next_image(self, delay_ms=None):
        """次の自動送りを予約"""
        self.cancel_scheduled_image()

        if self.is_playing and self.is_slideshow_active:
            next_delay = self.interval_ms if delay_ms is None else delay_ms
            self.log_timeline(f"schedule_next_image delay_ms={next_delay}")
            self.set_debug_hud_value("event", f"schedule-{next_delay}ms")
            self.after_id = self.root.after(next_delay, self.next_image)

    def get_image_sort_key(self, image_path):
        """更新日時を基準に時系列ソートするためのキー"""
        try:
            timestamp = os.path.getmtime(image_path)
        except OSError:
            timestamp = float('inf')

        normalized_path = os.path.normcase(os.path.normpath(image_path))
        return (timestamp, normalized_path)

    def sort_image_list(self, current_path=None):
        """現在表示中の画像を保ったまま時系列順に並び替える"""
        self.image_list.sort(key=self.get_image_sort_key)

        if current_path is None:
            return

        normalized_current = os.path.normcase(os.path.normpath(current_path))
        self.current_index = -1

        for index, image_path in enumerate(self.image_list):
            if os.path.normcase(os.path.normpath(image_path)) == normalized_current:
                self.current_index = index
                if self.seekbar_var.get() != index + 1:
                    self.seekbar_var.set(index + 1)
                break

    def load_images_from_folder(self):
        self.image_list = []
        if not self.folder_path:
            return

        for file in os.listdir(self.folder_path):
            ext = os.path.splitext(file)[1].lower()
            if ext in SUPPORTED_EXTS:
                self.image_list.append(os.path.join(self.folder_path, file))

        self.sort_image_list()

    def start_observer(self):
        """フォルダ監視の開始"""
        self.stop_observer()
        self.clear_image_queue()
        
        if self.folder_path:
            event_handler = NewImageHandler(self.image_queue)
            self.observer = Observer()
            self.observer.schedule(event_handler, self.folder_path, recursive=False)
            self.observer.start()

    def set_fullscreen_state(self, enabled):
        """フルスクリーン状態を切り替える"""
        self.is_fullscreen = enabled
        self.root.attributes("-fullscreen", enabled)
        self.update_fullscreen_button()

    def refresh_current_folder(self):
        """現在の対象フォルダを読み直して監視対象も更新"""
        self.clear_current_image_cache()
        self.load_images_from_folder()
        self.update_seekbar_range()
        self.refresh_thumbnail_strip()
        self.start_observer()
        self.current_index = -1

        if self.image_list:
            self.current_index = 0
            self.request_render(image=True, metadata=self.metadata_visible, thumbnail_highlight=True)
            self.root.after(80, self.request_image_render_if_needed)
            self.schedule_next_image()
        else:
            self.image_label.config(image='')
            self.current_index = -1
            self.seekbar_var.set(1)
            self.set_metadata_text("画像がありません。")
            self.cancel_scheduled_image()
            if self.is_slideshow_active:
                messagebox.showinfo("情報", "選択したフォルダに画像が見つかりませんでしたが、待機モードに入ります。\n画像が追加されると表示されます。")

    def change_folder_during_slideshow(self):
        """再生中に対象フォルダを切り替える"""
        if not self.is_slideshow_active:
            return

        folder = filedialog.askdirectory(initialdir=self.folder_path or None)
        if not folder:
            return

        self.set_folder(folder)
        self.refresh_current_folder()

    def start_slideshow(self):
        """スライドショーの開始"""
        if not self.folder_path:
            messagebox.showwarning("警告", "フォルダを選択してください。")
            return
        
        try:
            val = float(self.interval_var.get())
            if val < MIN_INTERVAL_SECONDS or val > MAX_INTERVAL_SECONDS:
                raise ValueError
            self.interval_ms = int(val * 1000)
        except ValueError:
            messagebox.showwarning("警告", f"秒数は {MIN_INTERVAL_SECONDS:.0f} 〜 {MAX_INTERVAL_SECONDS:.0f} の範囲で入力してください。")
            return

        self.sync_interval_ui()
        self.menu_frame.place_forget()  # メニューを隠す
        
        self.is_slideshow_active = True
        self.current_index = -1
        self.is_playing = True
        self.update_play_pause_button()
        self.set_fullscreen_state(True)
        self.request_render(layout=True)
        self.refresh_current_folder()

    def exit_fullscreen(self, event=None):
        """フルスクリーン解除・設定メニュー表示"""
        self.is_slideshow_active = False
        self.set_fullscreen_state(False)
        self.menu_frame.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        
        if self.seekbar_visible:
            self.seekbar_frame.place_forget()
            self.seekbar_visible = False

        if self.resize_after_id:
            self.root.after_cancel(self.resize_after_id)
            self.resize_after_id = None
        if self.resize_preview_after_id:
            self.root.after_cancel(self.resize_preview_after_id)
            self.resize_preview_after_id = None
        self.is_live_resizing = False
        self.native_resize_active = False
        self.panels_hidden_for_resize = False
        self.resume_play_after_resize = False
        self.cancel_metadata_refresh()
        self.cancel_thumbnail_highlight()
        self.cancel_thumbnail_follow()
        self.set_debug_hud_value("render_status", "idle")
        self.set_debug_hud_value("preview_status", "idle")
        
        # 自動再生のタイマーをキャンセル
        self.cancel_scheduled_image()
        
        # 監視も一時停止
        self.stop_observer()
        self.clear_image_queue()
        self.request_render(layout=True)

    def show_image(self, index):
        """指定したインデックスの画像を表示"""
        if not self.image_list or index < 0 or index >= len(self.image_list):
            self.image_label.config(image='')
            self.photo = None
            self.current_index = -1
            return

        self.log_timeline(f"show_image index={index}")
        self.current_index = index
        current_path = self.get_current_image_path()
        if current_path != self.current_source_path:
            self.clear_current_image_cache()
        self.request_render(image=True, metadata=self.metadata_visible, thumbnail_highlight=True)

    def update_seekbar_range(self):
        """画像リストの長さに合わせてシークバーの最大値を更新"""
        max_value = max(1, len(self.image_list))
        self.seekbar.config(to=max_value, state=tk.NORMAL if self.image_list else tk.DISABLED)

    def on_press(self, event):
        """マウス/タッチの押下開始"""
        self.start_x = event.x

    def on_release(self, event):
        """マウス/タッチのリリース（スワイプ・タップ判定）"""
        if self.start_x is None:
            return
            
        diff_x = event.x - self.start_x
        width = self.root.winfo_width()
        
        # スワイプ判定 (移動量が50pxより大きい場合)
        if abs(diff_x) > 50:
            if diff_x < 0:
                # 左スワイプ -> 次へ
                self.next_image()
            else:
                # 右スワイプ -> 前へ
                self.prev_image()
        else:
            # タップ判定 (移動量が少ない場合)
            if event.x < width / 3:
                # 画面左1/3 -> 前へ
                self.prev_image()
            elif event.x > width * 2 / 3:
                # 画面右1/3 -> 次へ
                self.next_image()
            else:
                # 画面中央1/3 -> シークバーの表示切替
                self.toggle_seekbar()
                
        self.start_x = None

    def toggle_seekbar(self):
        """再生中の操作パネルの表示・非表示を切り替える"""
        if not self.is_slideshow_active:
            return
            
        if self.seekbar_visible:
            self.seekbar_frame.place_forget()
            self.seekbar_visible = False
        else:
            # 画面下部に配置
            self.seekbar_frame.place(relx=0.5, rely=0.96, anchor=tk.S, relwidth=0.85)
            self.seekbar_visible = True

    def on_seek(self, value):
        """シークバーをドラッグしてページ指定したときの処理"""
        index = int(value) - 1
        if 0 <= index < len(self.image_list) and self.current_index != index:
            self.show_image(index)
            
            # 再生中であれば、移動した先から自動送りを再開
            self.schedule_next_image()

    def on_interval_change(self, value):
        """再生中UIから自動送り間隔を変更"""
        interval_seconds = round(float(value) / INTERVAL_STEP_SECONDS) * INTERVAL_STEP_SECONDS
        self.interval_ms = int(interval_seconds * 1000)
        self.sync_interval_ui()
        self.schedule_next_image()

    def toggle_fullscreen_mode(self):
        """再生中のままフルスクリーン状態を切り替える"""
        if not self.is_slideshow_active:
            return

        self.set_fullscreen_state(not self.is_fullscreen)
        self.request_render(layout=self.metadata_visible, image=True, thumbnail_highlight=self.thumbnail_visible)

    def next_image(self):
        """次の画像を表示"""
        self.log_timeline("next_image start")
        self.cancel_scheduled_image()

        if self.image_list:
            if self.current_index < len(self.image_list) - 1:
                self.show_image(self.current_index + 1)
            else:
                # 末尾まで来たので待機
                print("End of list. Waiting for new images...")
                pass
        
        self.schedule_next_image()
        self.log_timeline("next_image end")

    def prev_image(self):
        """前の画像を表示"""
        self.log_timeline("prev_image start")
        self.cancel_scheduled_image()

        if self.image_list and self.current_index > 0:
            self.show_image(self.current_index - 1)
            
        self.schedule_next_image()
        self.log_timeline("prev_image end")

    def toggle_play(self, event=None):
        """再生/一時停止の切り替え"""
        if not self.is_slideshow_active:
            return
            
        self.is_playing = not self.is_playing
        self.update_play_pause_button()
        if self.is_playing:
            print("Play")
            self.schedule_next_image()
        else:
            print("Pause")
            self.cancel_scheduled_image()

    def check_queue(self):
        """監視スレッドから送られてくる新しい画像の確認"""
        while not self.image_queue.empty():
            new_image = self.image_queue.get()
            # Windowsのパスの大文字小文字などのゆれを吸収するために正規化して比較
            norm_new = os.path.normcase(os.path.normpath(new_image))
            norm_existing = [os.path.normcase(os.path.normpath(p)) for p in self.image_list]
            
            if norm_new not in norm_existing:
                current_path = None
                if 0 <= self.current_index < len(self.image_list):
                    current_path = self.image_list[self.current_index]
                should_resume_from_wait = self.current_index == -1 or self.current_index == len(self.image_list) - 1

                self.image_list.append(new_image)
                self.sort_image_list(current_path=current_path)
                print(f"New image added: {new_image}")
                self.update_seekbar_range()
                self.refresh_thumbnail_strip()
                self.metadata_cache.pop(new_image, None)
                
                # もし現在最後の画像を表示中で待機状態だったなら、すぐ新しい画像へ進む
                if self.is_slideshow_active and self.is_playing and should_resume_from_wait:
                    if self.current_index == -1 or self.current_index < len(self.image_list) - 1:
                        # 500ms待ってから表示（ファイルの書き込み完了を待つため）
                        self.schedule_next_image(delay_ms=500)

        # 500ms後に再チェック
        self.root.after(500, self.check_queue)

    def on_closing(self):
        """アプリ終了時の処理"""
        self.stop_observer()
        self.clear_current_image_cache()
        self.uninstall_native_resize_hook()
        self.cancel_metadata_refresh()
        self.cancel_thumbnail_highlight()
        self.cancel_thumbnail_follow()
        self.set_debug_hud_value("render_status", "idle")
        self.set_debug_hud_value("preview_status", "idle")
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = ImageViewerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()
