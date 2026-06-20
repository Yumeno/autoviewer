import os
import shutil
import subprocess
import sys
import time
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox
from collections import OrderedDict
from PIL import Image, ImageTk, ExifTags, UnidentifiedImageError

# 対応する画像フォーマット
SUPPORTED_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}
MIN_INTERVAL_SECONDS = 1.0
MAX_INTERVAL_SECONDS = 30.0
INTERVAL_STEP_SECONDS = 0.5

# フォルダ監視 (ポーリング差分方式)
POLL_INTERVAL_DEFAULT_MS = 250
POLL_INTERVAL_MIN_MS = 100
POLL_INTERVAL_MAX_MS = 5000
# 確定までの最低安定時間 (新規 / 変化を image_list へ反映するまで mtime/size が
# 変わらない期間)。実値は max(POLL_SETTLE_MULTIPLIER * interval, POLL_SETTLE_MIN_MS)。
POLL_SETTLE_MULTIPLIER = 2
POLL_SETTLE_MIN_MS = 500
# 単発チェックボタンの 2 回スキャン間隔
MANUAL_CHECK_GAP_MS = 100

# ズーム (fit 表示を 1.0x の基準にした相対倍率)
ZOOM_MIN = 1.0
ZOOM_MAX = 5.0
ZOOM_STEP_SLIDER = 0.1
ZOOM_STEP_WHEEL = 0.25

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
IMAGE_OPEN_RETRY_DELAY_MS = 500
IMAGE_OPEN_MAX_ATTEMPTS = 3
METADATA_CACHE_MAX_ITEMS = 256


def scan_folder_snapshot(folder_path):
    """フォルダ直下を 1 回だけ scandir で走査してスナップショットを返す。

    戻り値: (snapshot, stat_failed)
        snapshot: {path: (mtime_ns, size)} stat に成功した画像
        stat_failed: listdir には見えるが stat に失敗したパス集合
                     呼び出し側は「削除扱いしない / スナップショットは前回値を保つ」と
                     解釈する。

    listdir 自体が失敗した場合は OSError を素通しする。呼び出し側は
    「この poll サイクルをスキップ」と解釈し、状態は一切変更しない。
    """
    snapshot = {}
    stat_failed = set()
    with os.scandir(folder_path) as it:
        for entry in it:
            try:
                if entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                stat_failed.add(entry.path)
                continue
            ext = os.path.splitext(entry.name)[1].lower()
            if ext not in SUPPORTED_EXTS:
                continue
            try:
                stat_result = entry.stat()
                snapshot[entry.path] = (stat_result.st_mtime_ns, stat_result.st_size)
            except OSError:
                stat_failed.add(entry.path)
    return snapshot, stat_failed


def diff_snapshots(prev, current, stat_failed):
    """前回・今回のスナップショットから (added, removed, modified) を計算する。

    - added:    current にあって prev に無いパス
    - removed:  prev にあって current にも stat_failed にも無いパス
                (listdir が成功した上で見えなくなった = 確定削除)
    - modified: 両方にあって (mtime_ns, size) が異なるパス
    """
    prev_keys = set(prev)
    current_keys = set(current)
    added = current_keys - prev_keys
    removed = prev_keys - current_keys - stat_failed
    modified = {p for p in prev_keys & current_keys if prev[p] != current[p]}
    return added, removed, modified


def find_path_index(image_list, path):
    """normcase 正規化込みで image_list 内の path のインデックスを返す。無ければ -1。"""
    if path is None:
        return -1
    target = os.path.normcase(os.path.normpath(path))
    for i, p in enumerate(image_list):
        if os.path.normcase(os.path.normpath(p)) == target:
            return i
    return -1


def compute_zoom_crop(source_size, fit_size, zoom, pan_x, pan_y):
    """ズーム + パンを反映した「source 側のクロップ範囲」を求める。

    pan_x / pan_y は **viewport (fit) ピクセル座標** での「中央からの平行移動量」。
    返り値の pan_x_clamped / pan_y_clamped は viewport 端を超えないようにクランプされ
    たもの。呼び出し側は受け取ったクランプ済み値で state を更新すること。

    zoom <= 1.0 の場合: source 全体をクロップ範囲とし、pan は 0 に強制 (フィット表示)。
    zoom > 1.0 の場合: 中央から (source / zoom) サイズを切り出し、pan ぶんずらす。
    """
    source_w, source_h = source_size
    fit_w, fit_h = fit_size

    if zoom <= 1.0 or source_w <= 0 or source_h <= 0 or fit_w <= 0 or fit_h <= 0:
        return (0, 0, source_w, source_h), 0.0, 0.0

    max_pan_x = fit_w * (zoom - 1) / 2
    max_pan_y = fit_h * (zoom - 1) / 2
    pan_x = max(-max_pan_x, min(max_pan_x, pan_x))
    pan_y = max(-max_pan_y, min(max_pan_y, pan_y))

    crop_w = source_w / zoom
    crop_h = source_h / zoom
    px_src = pan_x * source_w / (fit_w * zoom)
    py_src = pan_y * source_h / (fit_h * zoom)

    crop_left = (source_w / 2) + px_src - (crop_w / 2)
    crop_top = (source_h / 2) + py_src - (crop_h / 2)
    crop_right = crop_left + crop_w
    crop_bottom = crop_top + crop_h

    # 端で丸め誤差を吸収しつつ source 範囲内へクランプ。
    # 極小画像 (source_w/h が 1〜数 px) や高 zoom で crop が 0 幅 / 0 高にならないよう、
    # right > left, bottom > top を強制する。
    crop_left = max(0, min(source_w - 1, int(round(crop_left))))
    crop_top = max(0, min(source_h - 1, int(round(crop_top))))
    crop_right = max(crop_left + 1, min(source_w, int(round(crop_right))))
    crop_bottom = max(crop_top + 1, min(source_h, int(round(crop_bottom))))

    return (crop_left, crop_top, crop_right, crop_bottom), pan_x, pan_y


def move_to_trash(path):
    """指定ファイルを OS のゴミ箱へ移動する (cross-platform)。

    Windows: ctypes 経由で SHFileOperationW を呼ぶ
    macOS:   osascript で Finder の delete を呼び出す
    Linux:   gio trash → trash (trash-cli) の順でフォールバック

    失敗時は OSError を raise する。
    """
    abs_path = os.path.abspath(path)
    if sys.platform.startswith("win"):
        _trash_windows(abs_path)
    elif sys.platform == "darwin":
        _trash_macos(abs_path)
    elif sys.platform.startswith("linux"):
        _trash_linux(abs_path)
    else:
        raise OSError(f"ゴミ箱への移動はこの OS では未対応です: {sys.platform}")


def _trash_windows(abs_path):
    """Windows: SHFileOperationW(FO_DELETE, FOF_ALLOWUNDO) でゴミ箱へ送る"""
    import ctypes
    from ctypes import wintypes

    FO_DELETE = 3
    FOF_ALLOWUNDO = 0x40
    FOF_NOCONFIRMATION = 0x10
    FOF_SILENT = 0x04
    FOF_NOERRORUI = 0x400

    class SHFILEOPSTRUCTW(ctypes.Structure):
        # pFrom / pTo は LPCWSTR ではなく c_void_p にする。
        # LPCWSTR (c_wchar_p) は ctypes が \0 で文字列を打ち切るため、
        # SHFileOperationW が要求する「ダブル \0 終端」を渡せない。
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", ctypes.c_void_p),
            ("pTo", ctypes.c_void_p),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    # ファイル一覧 "path1\0path2\0...\0" + 末尾 \0 (リスト終端)。
    # create_unicode_buffer が末尾 \0 を 1 個自動付与するので、こちらでも \0 を付けて
    # 「path\0\0」になるようにする。GC されないよう参照を保持する。
    buf = ctypes.create_unicode_buffer(abs_path + "\0")
    op = SHFILEOPSTRUCTW()
    op.wFunc = FO_DELETE
    op.pFrom = ctypes.cast(buf, ctypes.c_void_p).value
    op.fFlags = FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI

    func = ctypes.windll.shell32.SHFileOperationW
    func.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    func.restype = ctypes.c_int

    result = func(ctypes.byref(op))
    if result != 0:
        raise OSError(f"SHFileOperationW failed: code 0x{result:X}")
    if op.fAnyOperationsAborted:
        raise OSError("ゴミ箱への移動が中断されました。")


def _trash_macos(abs_path):
    """macOS: osascript で Finder の delete を呼び出す。

    パスは AppleScript 文字列に埋め込まず argv 経由で渡すので、改行や
    特殊文字を含むパスでも壊れない。"""
    script = (
        "on run argv\n"
        "    tell application \"Finder\" to delete POSIX file (item 1 of argv)\n"
        "end run\n"
    )
    completed = subprocess.run(
        ["osascript", "-e", script, abs_path],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise OSError(
            "osascript によるゴミ箱移動が失敗しました: "
            f"{completed.stderr.strip() or completed.stdout.strip() or 'unknown error'}"
        )


def _trash_linux(abs_path):
    """Linux: gio trash → trash-put → trash の順で試行 (失敗もフォールバック)"""
    # 各コマンドを順に試し、見つかって 0 で返したら成功。
    # 見つかって失敗したものはエラーを集めておき、全滅したらまとめて報告する。
    attempts = []
    for cmd in ("gio", "trash-put", "trash"):
        if not shutil.which(cmd):
            continue
        if cmd == "gio":
            argv = ["gio", "trash", "--", abs_path]
        else:
            argv = [cmd, "--", abs_path]
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            return
        err = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        attempts.append(f"{cmd}: {err}")

    if attempts:
        raise OSError(
            "ゴミ箱への移動に失敗しました。\n" + "\n".join(attempts)
        )
    raise OSError(
        "ゴミ箱への移動には 'gio' または 'trash-cli' のインストールが必要です。"
    )


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
        self.thumbnail_labels = {}
        self.current_source_image = None
        self.current_source_path = None
        self.current_render_image = None
        self.current_render_path = None
        self.current_render_size = None
        self.current_render_resample = None
        self.current_render_zoom = None
        self.current_render_pan = None
        self.photo = None

        # ズーム / パン状態 (fit 表示を 1.0x の基準)
        self.zoom_level = 1.0
        self.pan_offset_x = 0.0
        self.pan_offset_y = 0.0
        # ドラッグによるパン操作の途中状態
        self.pan_drag_active = False
        self.pan_drag_start = None  # (mouse_x, mouse_y, pan_x_start, pan_y_start)

        self.metadata_panel_visible = False
        self.metadata_text_value = None
        self.metadata_cache = OrderedDict()
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
        self.image_open_retry_after_id = None
        self.image_open_retry_request = None
        self.last_root_size = None
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
        
        # フォルダ監視 (ポーリング差分方式)
        self.folder_snapshot = {}  # {path: (mtime_ns, size)} 確定済みの基準スナップショット
        self.pending_changes = {}  # {path: ((mtime_ns, size), first_seen_perf)} settle 待ち
        self.poll_after_id = None
        self.poll_interval_ms = POLL_INTERVAL_DEFAULT_MS
        self.polling_enabled = True
        self.manual_check_after_id = None
        self.poll_interval_setting_var = tk.IntVar(value=POLL_INTERVAL_DEFAULT_MS)
        self.polling_enabled_setting_var = tk.BooleanVar(value=True)

        # UI要素の構築
        self.setup_ui()
        self.sync_interval_ui()
        self.update_play_pause_button()
        self.update_fullscreen_button()
        self.update_polling_status_ui()
        self.apply_panel_layout()
        self.set_metadata_text("画像情報を表示するには、再生中に情報欄を開いてください。")
        
        # キーバインド
        self.root.bind("<Escape>", self.on_escape)
        self.root.bind("<Right>", lambda e: self.next_image())
        self.root.bind("<Left>", lambda e: self.prev_image())
        self.root.bind("<space>", lambda e: self.next_image())
        self.root.bind("p", self.toggle_play)
        self.root.bind("P", self.toggle_play)
        self.root.bind("<Control-c>", self.copy_current_image_to_clipboard)
        self.root.bind("<Configure>", self.on_root_configure)

        # ポーリングは start_slideshow / refresh_current_folder のタイミングで開始される。

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
        self.image_label.bind("<B1-Motion>", self.on_pan_drag_motion)
        self.image_label.bind("<Button-3>", self.show_context_menu)
        # Ctrl + マウスホイールでズーム
        self.image_label.bind("<Control-MouseWheel>", self.on_ctrl_mousewheel)
        self.image_area.bind("<Control-MouseWheel>", self.on_ctrl_mousewheel)

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
        self.thumbnail_frame.bind("<Shift-MouseWheel>", self.on_thumbnail_mousewheel)
        self.thumbnail_canvas.bind("<Shift-MouseWheel>", self.on_thumbnail_mousewheel)
        self.thumbnail_inner.bind("<Shift-MouseWheel>", self.on_thumbnail_mousewheel)

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

        self.return_to_menu_button = tk.Button(
            primary_controls,
            text="設定画面へ戻る",
            width=14,
            command=self.return_to_menu
        )
        self.return_to_menu_button.pack(side=tk.RIGHT, padx=(10, 0))

        self.quit_button = tk.Button(
            primary_controls,
            text="終了",
            width=8,
            command=self.on_closing
        )
        self.quit_button.pack(side=tk.RIGHT, padx=(10, 0))

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
        self.metadata_toggle_button.pack(side=tk.LEFT, padx=(0, 10))

        self.polling_toggle_button = tk.Button(
            secondary_controls,
            text="監視ON",
            width=10,
            command=self.toggle_polling,
        )
        self.polling_toggle_button.pack(side=tk.LEFT, padx=(0, 10))

        self.manual_check_button = tk.Button(
            secondary_controls,
            text="即時チェック",
            width=12,
            command=self.request_manual_check,
        )
        self.manual_check_button.pack(side=tk.LEFT, padx=(0, 10))

        # 削除ボタン: 一時停止中のみ enable。常に確認ダイアログを出してから削除する。
        self.delete_button = tk.Button(
            secondary_controls,
            text="削除",
            width=8,
            command=self.delete_current_image,
            bg='#C62828',
            fg='white',
            disabledforeground='#777777',
            state=tk.DISABLED,
        )
        self.delete_button.pack(side=tk.LEFT)

        # 3 行目: 右クリックメニュー相当のファイル操作 (タッチ環境向け)
        tertiary_controls = tk.Frame(self.seekbar_frame, bg='#222222')
        tertiary_controls.pack(fill=tk.X, padx=10, pady=(0, 6))

        self.reveal_button = tk.Button(
            tertiary_controls,
            text="ファイラで表示",
            width=14,
            command=self.reveal_current_image_in_file_manager,
            state=tk.DISABLED,
        )
        self.reveal_button.pack(side=tk.LEFT, padx=(0, 10))

        self.copy_button = tk.Button(
            tertiary_controls,
            text="コピー",
            width=10,
            command=self.copy_current_image_to_clipboard,
            state=tk.DISABLED,
        )
        self.copy_button.pack(side=tk.LEFT)

        # 4 行目: ズーム操作
        zoom_controls = tk.Frame(self.seekbar_frame, bg='#222222')
        zoom_controls.pack(fill=tk.X, padx=10, pady=(0, 6))

        tk.Label(zoom_controls, text="ズーム", fg='white', bg='#222222').pack(side=tk.LEFT, padx=(0, 8))

        self.zoom_var = tk.DoubleVar(value=1.0)
        self.zoom_scale = tk.Scale(
            zoom_controls,
            variable=self.zoom_var,
            from_=ZOOM_MIN,
            to=ZOOM_MAX,
            resolution=ZOOM_STEP_SLIDER,
            orient=tk.HORIZONTAL,
            showvalue=False,
            bg='#222222',
            fg='white',
            troughcolor='#555555',
            highlightthickness=0,
            length=220,
            command=self.on_zoom_slider_change,
        )
        self.zoom_scale.pack(side=tk.LEFT, padx=(0, 8))

        self.zoom_label_var = tk.StringVar(value="1.0x")
        tk.Label(
            zoom_controls,
            textvariable=self.zoom_label_var,
            fg='white',
            bg='#222222',
            width=6,
        ).pack(side=tk.LEFT)

        self.zoom_reset_button = tk.Button(
            zoom_controls,
            text="リセット",
            width=8,
            command=self.reset_zoom,
        )
        self.zoom_reset_button.pack(side=tk.LEFT, padx=(8, 0))

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

        # 監視設定 (起動時のデフォルト値)。再生中の操作パネルとは分離してある。
        watch_frame = tk.Frame(self.menu_frame, bg='#333333')
        watch_frame.pack(pady=5)
        tk.Checkbutton(
            watch_frame,
            text="自動監視を有効にする",
            variable=self.polling_enabled_setting_var,
            fg='white',
            bg='#333333',
            selectcolor='#222222',
            activebackground='#333333',
            activeforeground='white',
        ).pack(side=tk.LEFT, padx=(0, 10))
        tk.Label(watch_frame, text="監視間隔 (ms):", fg='white', bg='#333333').pack(side=tk.LEFT)
        tk.Entry(
            watch_frame,
            textvariable=self.poll_interval_setting_var,
            width=6,
        ).pack(side=tk.LEFT, padx=5)
        tk.Label(
            self.menu_frame,
            text=f"  ※ 範囲 {POLL_INTERVAL_MIN_MS}〜{POLL_INTERVAL_MAX_MS} ms。実検知レイテンシは目安 2 × 監視間隔。",
            fg='#888888',
            bg='#333333',
            justify=tk.LEFT,
        ).pack(pady=(0, 5))

        tk.Button(self.menu_frame, text="2. スライドショー開始", command=self.start_slideshow, width=20, bg='#4CAF50', fg='white').pack(pady=20)

        # 操作説明
        help_text = "【操作方法】\n・Escキー: フルスクリーン切替\n・→ / Space: 次の画像\n・←: 前の画像\n・P: 再生/一時停止\n・中央タップ: 操作パネル表示\n・操作パネル: 設定画面へ戻る / 最大化切替 / フォルダ変更 / 終了\n・操作パネル: 監視ON/OFF / 即時チェック (新規画像を手動で取り込む)"
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

    def update_polling_status_ui(self):
        """監視 ON/OFF トグルボタンの表示を現在状態に合わせる"""
        button = getattr(self, "polling_toggle_button", None)
        if button is None:
            return
        if self.polling_enabled:
            button.config(text="監視ON", bg='#4CAF50', fg='white')
        else:
            button.config(text="監視OFF", bg='#FF9800', fg='white')

    def toggle_polling(self):
        """再生中の監視 ON/OFF を切り替える"""
        if not self.is_slideshow_active:
            return
        self.polling_enabled = not self.polling_enabled
        # ON 復帰時は pending をリセットして、中断中の変化も含めて差分を取り直す。
        # OFF 時は次回 poll 予約を解除するだけで folder_snapshot は据え置く。
        self.pending_changes = {}
        if self.polling_enabled:
            self.start_polling()
        else:
            self.cancel_polling()
        self.update_polling_status_ui()

    def sync_zoom_ui(self):
        """ズームの state を UI ウィジェットへ反映する。"""
        if hasattr(self, "zoom_var"):
            if abs(self.zoom_var.get() - self.zoom_level) > 1e-3:
                self.zoom_var.set(self.zoom_level)
        if hasattr(self, "zoom_label_var"):
            self.zoom_label_var.set(f"{self.zoom_level:.1f}x")

    def reset_zoom_state(self, *, request_render=True):
        """ズーム倍率と注視位置を初期値 (1.0x / 中央) に戻す。

        画像切替やフォルダ変更の直後に呼ばれ、新しい画像はまずフィット表示で
        見せる挙動を担保する。
        """
        changed = (
            self.zoom_level != 1.0
            or self.pan_offset_x != 0.0
            or self.pan_offset_y != 0.0
        )
        self.zoom_level = 1.0
        self.pan_offset_x = 0.0
        self.pan_offset_y = 0.0
        self.pan_drag_active = False
        self.pan_drag_start = None
        self.sync_zoom_ui()
        if changed and request_render:
            self.request_render(image=True)

    def reset_zoom(self):
        """ズームリセットボタンのハンドラ。"""
        self.reset_zoom_state(request_render=True)

    def set_zoom_level(self, new_zoom, *, anchor_xy=None):
        """ズーム倍率を絶対値で設定する。

        anchor_xy: (x, y) を指定すると、その viewport 座標が同じ位置に
        居続けるように pan を調整 (= マウス位置を軸にしたズーム)。None なら
        中央軸のまま pan は据え置き。
        """
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, new_zoom))
        if abs(new_zoom - self.zoom_level) < 1e-3:
            return

        old_zoom = self.zoom_level
        if anchor_xy is not None and self.current_source_image is not None:
            self._apply_anchored_zoom(new_zoom, anchor_xy[0], anchor_xy[1])
        else:
            self.zoom_level = new_zoom
        # 倍率変更後は pan が範囲外になり得る。1.0x に戻したら強制 0。
        if self.zoom_level <= 1.0:
            self.pan_offset_x = 0.0
            self.pan_offset_y = 0.0
        if old_zoom != self.zoom_level:
            self.sync_zoom_ui()
            self.request_render(image=True)

    def _apply_anchored_zoom(self, new_zoom, anchor_x, anchor_y):
        """マウス位置を軸にズームし、anchor 位置が画面上で動かないように pan 更新。

        anchor_x / anchor_y は image_area 座標系 (px)。fit 表示領域の外なら無視。
        """
        if self.current_source_image is None:
            self.zoom_level = new_zoom
            return

        viewport_w, viewport_h = self.get_image_viewport_size()
        img_w, img_h = self.current_source_image.size
        if img_w <= 0 or img_h <= 0:
            self.zoom_level = new_zoom
            return

        ratio = min(viewport_w / img_w, viewport_h / img_h)
        fit_w = int(img_w * ratio)
        fit_h = int(img_h * ratio)
        if fit_w <= 0 or fit_h <= 0:
            self.zoom_level = new_zoom
            return

        # image_label 内で fit 画像は中央に置かれる。anchor を fit 座標系へ変換。
        # image_label は image_area いっぱい (fill=BOTH expand=True) なので、
        # image_area 座標 ≒ image_label 座標として扱える。
        fit_origin_x = (viewport_w - fit_w) / 2
        fit_origin_y = (viewport_h - fit_h) / 2
        fit_anchor_x = anchor_x - fit_origin_x
        fit_anchor_y = anchor_y - fit_origin_y

        # fit 画像の外でクリックされた場合は中央 (fit_w/2, fit_h/2) を anchor として
        # 中央軸ズームに流す。「pan 据え置きでズーム値だけ変える」と画像中心が
        # 微妙に動くのでここで意図的に中央を保持する。
        if not (0 <= fit_anchor_x <= fit_w and 0 <= fit_anchor_y <= fit_h):
            fit_anchor_x = fit_w / 2
            fit_anchor_y = fit_h / 2

        z_old = self.zoom_level
        z_new = new_zoom

        # 旧 viewport (= fit) 内での anchor 位置を、画像の正規化座標へ
        # 旧 scaled 座標系での anchor 位置
        scaled_anchor_old_x = fit_w * (z_old - 1) / 2 + self.pan_offset_x + fit_anchor_x
        scaled_anchor_old_y = fit_h * (z_old - 1) / 2 + self.pan_offset_y + fit_anchor_y
        # 0..1 の正規化座標
        frac_x = scaled_anchor_old_x / (fit_w * z_old)
        frac_y = scaled_anchor_old_y / (fit_h * z_old)

        # 新 scaled 座標系で同じ画像位置を anchor として再計算
        scaled_anchor_new_x = frac_x * fit_w * z_new
        scaled_anchor_new_y = frac_y * fit_h * z_new
        new_pan_x = scaled_anchor_new_x - fit_anchor_x - fit_w * (z_new - 1) / 2
        new_pan_y = scaled_anchor_new_y - fit_anchor_y - fit_h * (z_new - 1) / 2

        self.zoom_level = z_new
        self.pan_offset_x = new_pan_x
        self.pan_offset_y = new_pan_y

    def on_zoom_slider_change(self, value):
        """ズームスライダー変更時のハンドラ。"""
        try:
            new_zoom = round(float(value) / ZOOM_STEP_SLIDER) * ZOOM_STEP_SLIDER
        except (TypeError, ValueError):
            return
        self.set_zoom_level(new_zoom)

    def on_ctrl_mousewheel(self, event):
        """Ctrl + マウスホイールで拡大 / 縮小。マウス位置を軸にする。"""
        if not self.is_slideshow_active or self.current_source_image is None:
            return None
        # event.delta: Windows は ±120/notch、macOS は ±1〜数
        direction = 1 if event.delta > 0 else -1
        step = ZOOM_STEP_WHEEL * direction
        # image_area 座標系でのアンカー位置 (event.x_root が画面座標)
        anchor_x = event.x_root - self.image_area.winfo_rootx()
        anchor_y = event.y_root - self.image_area.winfo_rooty()
        self.set_zoom_level(self.zoom_level + step, anchor_xy=(anchor_x, anchor_y))
        return "break"

    def is_zoomed(self):
        """ズーム中 (>1.0x) かどうか。タップ/スワイプとパンの分岐に使う。"""
        return self.zoom_level > 1.0 + 1e-3

    def on_pan_drag_motion(self, event):
        """ズーム中のドラッグでパンする。"""
        if not self.pan_drag_active or self.pan_drag_start is None:
            return
        start_mx, start_my, start_px, start_py = self.pan_drag_start
        dx = event.x - start_mx
        dy = event.y - start_my
        # UX: 画像をつかんで動かす感覚にする。ドラッグ右 → 画像が右へ流れる →
        # viewport は画像の左側を見ている = pan_offset_x はマイナス方向へ。
        # つまり pan_offset の符号はドラッグ方向と逆になる。
        self.pan_offset_x = start_px - dx
        self.pan_offset_y = start_py - dy
        self.request_render(image=True)

    def update_delete_button_state(self):
        """削除・コピー・ファイラ表示ボタンの enable / disable を現在状態に合わせる。

        削除は破壊的操作なので一時停止中限定、コピー / ファイラ表示は非破壊なので
        現在画像があれば常に有効。
        """
        delete_button = getattr(self, "delete_button", None)
        if delete_button is not None:
            enabled = (
                self.is_slideshow_active
                and not self.is_playing
                and self.get_current_image_path() is not None
            )
            delete_button.config(state=tk.NORMAL if enabled else tk.DISABLED)

        has_current = (
            self.is_slideshow_active
            and self.get_current_image_path() is not None
        )
        for attr in ("reveal_button", "copy_button"):
            button = getattr(self, attr, None)
            if button is not None:
                button.config(state=tk.NORMAL if has_current else tk.DISABLED)

    def delete_current_image(self):
        """現在表示中の画像をゴミ箱へ送る (確認ダイアログ付き)。

        二重ガード:
        - スライドショー中・一時停止中・現在画像あり、の 3 条件成立時のみ実行
        - messagebox.askyesno で明示的に「はい」を選んだ場合のみ削除
        """
        if not self.is_slideshow_active or self.is_playing:
            return
        image_path = self.get_current_image_path()
        if not image_path:
            return

        confirmed = messagebox.askyesno(
            "削除確認",
            (
                "以下の画像をゴミ箱へ移動しますか？\n\n"
                f"ファイル: {os.path.basename(image_path)}\n"
                f"パス: {image_path}"
            ),
            default=messagebox.NO,
        )
        if not confirmed:
            return

        try:
            move_to_trash(image_path)
        except Exception as exc:
            messagebox.showerror(
                "削除エラー",
                f"画像をゴミ箱へ移動できませんでした。\n\n{exc}",
            )
            return

        # 即座に image_list / folder_snapshot から外して UI を進める。
        # ポーリングが次のサイクルでこの削除を検知しても、folder_snapshot に
        # 既に居ないので二重発火しない。
        self.apply_folder_diff(added=set(), removed={image_path}, modified=set())
        self.update_delete_button_state()

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
        """現在画像まわりのキャッシュをクリアする"""
        self.cancel_image_open_retry()
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
        self.current_render_zoom = None
        self.current_render_pan = None

    def cancel_image_open_retry(self):
        """画像オープンの遅延リトライ予約を取り消す"""
        if self.image_open_retry_after_id:
            self.root.after_cancel(self.image_open_retry_after_id)
            self.image_open_retry_after_id = None
        self.image_open_retry_request = None

    def schedule_image_open_retry(
        self,
        image_path,
        *,
        resample,
        refresh_metadata,
        refresh_thumbnail,
        use_preview_source,
        open_attempt,
    ):
        """生成途中画像向けのオープン再試行を予約する"""
        request = (
            image_path,
            resample,
            refresh_metadata,
            refresh_thumbnail,
            use_preview_source,
            open_attempt,
        )
        if self.image_open_retry_request == request and self.image_open_retry_after_id:
            return

        self.cancel_image_open_retry()
        self.image_open_retry_request = request

        if use_preview_source:
            self.set_debug_hud_value("preview_status", "pending")
        else:
            self.set_debug_hud_value("render_status", "pending")

        def retry():
            self.image_open_retry_after_id = None
            request_to_run = self.image_open_retry_request
            self.image_open_retry_request = None
            if request_to_run is None:
                return
            if self.get_current_image_path() != image_path:
                return
            self.render_current_image(
                resample=resample,
                refresh_metadata=refresh_metadata,
                refresh_thumbnail=refresh_thumbnail,
                use_preview_source=use_preview_source,
                open_attempt=open_attempt,
            )

        self.image_open_retry_after_id = self.root.after(IMAGE_OPEN_RETRY_DELAY_MS, retry)

    def begin_resize_session(self):
        """リサイズ中の軽量表示モードへ切り替え"""
        if self.is_live_resizing:
            self.log_resize_debug("begin_resize_session skipped: already live resizing")
            return

        start = time.perf_counter()
        self.log_resize_debug(
            f"begin_resize_session start metadata_visible={self.metadata_visible} "
            f"panel_visible={self.metadata_panel_visible} thumbnail_visible={self.thumbnail_visible}"
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

        self.cancel_image_open_retry()
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

    def apply_panel_layout(self):
        """サムネイル帯と情報欄の表示状態をレイアウトへ反映"""
        if not self.is_slideshow_active:
            if self.metadata_panel_visible:
                self.metadata_frame.grid_remove()
                self.metadata_panel_visible = False
            self.thumbnail_frame.grid_remove()
            self.update_thumbnail_buttons()
            self.update_metadata_button()
            return

        show_metadata_panel = self.metadata_visible and not self.panels_hidden_for_resize
        show_thumbnail_strip = self.thumbnail_visible and not self.panels_hidden_for_resize

        if show_metadata_panel:
            if not self.metadata_panel_visible:
                self.metadata_frame.grid()
                self.metadata_panel_visible = True
        elif self.metadata_panel_visible:
            self.metadata_frame.grid_remove()
            self.metadata_panel_visible = False

        if show_thumbnail_strip:
            self.thumbnail_frame.grid()
        else:
            self.thumbnail_frame.grid_remove()

        self.update_thumbnail_buttons()
        self.update_metadata_button()

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
        """現在画像のコンテキストメニューを表示する"""
        if not self.get_current_image_path():
            return

        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def copy_current_image_to_clipboard(self, event=None):
        """現在表示中の画像をクリップボードにコピーする"""
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
        """現在表示中の画像をファイルマネージャで開く"""
        image_path = self.get_current_image_path()
        if not image_path:
            return

        try:
            self.reveal_in_file_manager(image_path)
        except Exception as exc:
            messagebox.showerror("エラー", f"ファイルマネージャで開けませんでした。\n{exc}")

    def reveal_in_file_manager(self, image_path):
        """OS に合わせて画像の場所をファイルマネージャで表示する"""
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
            # Linux では選択表示の標準 API が薄いため、親フォルダを開く。
            subprocess.run(["xdg-open", parent_dir], check=True)
            return

        raise OSError(f"Unsupported platform: {sys.platform}")

    def copy_pil_image_to_clipboard(self, image):
        """PIL Image を Windows クリップボードへ転送"""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp_file:
            temp_copy_path = tmp_file.name
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

    def clear_thumbnail_widgets(self):
        """既存サムネイルUIを破棄"""
        self.cancel_thumbnail_follow()
        for widget in self.thumbnail_inner.winfo_children():
            widget.destroy()
        self.thumbnail_photos = {}
        self.thumbnail_buttons = {}
        self.thumbnail_items = {}
        self.thumbnail_labels = {}

    def get_thumbnail_photo(self, image_path):
        """サムネイル画像を必要時だけ生成して返す"""
        cached_photo = self.thumbnail_photos.get(image_path)
        if cached_photo is not None:
            return cached_photo

        try:
            with Image.open(image_path) as thumb_image:
                thumb_image = thumb_image.copy()
                thumb_image.thumbnail((112, 96), Image.Resampling.LANCZOS)
        except (OSError, UnidentifiedImageError):
            thumb_image = Image.new("RGB", (112, 96), color="#444444")

        thumb_photo = ImageTk.PhotoImage(thumb_image)
        self.thumbnail_photos[image_path] = thumb_photo
        return thumb_photo

    def add_thumbnail_item(self, index, image_path):
        """サムネイル帯へ 1 件だけ追加する"""
        item_frame = tk.Frame(self.thumbnail_inner, bg='#161616', padx=4, pady=4)
        item_frame.pack(side=tk.LEFT)

        button = tk.Button(
            item_frame,
            image=self.get_thumbnail_photo(image_path),
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

        for widget in (item_frame, button, label):
            widget.bind("<Shift-MouseWheel>", self.on_thumbnail_mousewheel)

        self.thumbnail_buttons[image_path] = button
        self.thumbnail_items[image_path] = item_frame
        self.thumbnail_labels[image_path] = label

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
            self.add_thumbnail_item(index, image_path)

        self.highlight_current_thumbnail()
        self.on_thumbnail_inner_configure()

    def append_thumbnail_item(self, image_path):
        """末尾追加だけで済む場合にサムネイル帯を差分更新する"""
        self.add_thumbnail_item(len(self.image_list) - 1, image_path)
        self.highlight_current_thumbnail()
        self.on_thumbnail_inner_configure()

    def on_root_configure(self, event=None):
        """ウィンドウサイズ変更時の再描画制御"""
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

        self.end_resize_session()

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
        """現在画像に合わせたサムネイル追従を予約する"""
        self.cancel_thumbnail_follow()
        self.thumbnail_follow_path = image_path
        self.thumbnail_scroll_after_id = self.root.after_idle(self.flush_thumbnail_follow)

    def cancel_thumbnail_follow(self):
        """予約済みのサムネイル追従を取り消す"""
        if self.thumbnail_scroll_after_id:
            self.root.after_cancel(self.thumbnail_scroll_after_id)
            self.thumbnail_scroll_after_id = None
        self.thumbnail_follow_path = None

    def flush_thumbnail_follow(self):
        """予約済みのサムネイル追従を実行する"""
        self.thumbnail_scroll_after_id = None
        if self.thumbnail_follow_path:
            path = self.thumbnail_follow_path
            self.thumbnail_follow_path = None
            self.scroll_thumbnail_into_view(path)

    def scroll_thumbnail_into_view(self, image_path):
        """現在画像のサムネイルが見える位置までスクロールする"""
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
            other_info.append(f"{key}: {self.format_metadata_value(value)}")

        if other_info:
            lines.extend(["", "[Image Info]"] + other_info)

        exif_lines = []
        exif_data = image.getexif()
        if exif_data:
            for tag_id, value in exif_data.items():
                tag_name = ExifTags.TAGS.get(tag_id, str(tag_id))
                exif_lines.append(f"{tag_name}: {self.format_metadata_value(value)}")

        if exif_lines:
            lines.extend(["", "[EXIF]"] + exif_lines)

        if len(lines) == 5:
            lines.extend(["", "画像内メタ情報は見つかりませんでした。"])

        return "\n".join(lines)

    def format_metadata_value(self, value):
        """メタ情報の値を読みやすい文字列へ整形する"""
        if isinstance(value, bytes):
            for encoding in ("utf-8", "utf-16", "latin-1"):
                try:
                    value = value.decode(encoding).rstrip("\x00")
                    break
                except UnicodeDecodeError:
                    continue
            else:
                value = value.hex()

        return str(value)

    def get_metadata_text_for_path(self, image_path, image=None):
        """画像ごとのメタ情報文字列をキャッシュして返す"""
        cached = self.metadata_cache.get(image_path)
        if cached is not None:
            self.metadata_cache.move_to_end(image_path)
            return cached

        close_image = False
        if image is None:
            image = Image.open(image_path)
            close_image = True

        try:
            metadata_text = self.build_metadata_text(image_path, image)
            self.metadata_cache[image_path] = metadata_text
            self.metadata_cache.move_to_end(image_path)
            while len(self.metadata_cache) > METADATA_CACHE_MAX_ITEMS:
                self.metadata_cache.popitem(last=False)
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
        open_attempt=0,
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
                try:
                    with Image.open(image_path) as loaded_image:
                        self.current_source_image = loaded_image.copy()
                    self.current_source_path = image_path
                    self.cancel_image_open_retry()
                except IOError:
                    if open_attempt + 1 < IMAGE_OPEN_MAX_ATTEMPTS:
                        self.schedule_image_open_retry(
                            image_path,
                            resample=resample,
                            refresh_metadata=refresh_metadata,
                            refresh_thumbnail=refresh_thumbnail,
                            use_preview_source=use_preview_source,
                            open_attempt=open_attempt + 1,
                        )
                    else:
                        print(f"Failed to open image: {image_path}")
                        if use_preview_source:
                            self.set_debug_hud_value("preview_status", "idle")
                        else:
                            self.set_debug_hud_value("render_status", "idle")
                    return

            if use_preview_source and self.current_render_image is not None:
                img = self.current_render_image
            else:
                img = self.current_source_image

            screen_width, screen_height = self.get_image_viewport_size()

            img_width, img_height = img.size
            ratio = min(screen_width / img_width, screen_height / img_height)
            fit_width = int(img_width * ratio)
            fit_height = int(img_height * ratio)
            fit_size = (fit_width, fit_height)

            # ズーム + パン適用後の crop 範囲を計算し、クランプされた pan で state を更新
            zoom = self.zoom_level
            crop_box, self.pan_offset_x, self.pan_offset_y = compute_zoom_crop(
                (img_width, img_height),
                fit_size,
                zoom,
                self.pan_offset_x,
                self.pan_offset_y,
            )
            pan_signature = (round(self.pan_offset_x, 2), round(self.pan_offset_y, 2))

            render_size = fit_size
            can_reuse_render = (
                self.current_render_path == image_path
                and self.current_render_size == render_size
                and self.current_render_resample == resample
                and self.current_render_zoom == zoom
                and self.current_render_pan == pan_signature
                and self.photo is not None
            )

            if can_reuse_render:
                self.log_resize_debug(f"render_current_image reused existing render size={render_size}")
            else:
                if zoom > 1.0 and crop_box != (0, 0, img_width, img_height):
                    cropped_image = img.crop(crop_box)
                    rendered_image = cropped_image.resize(render_size, resample)
                else:
                    rendered_image = img.resize(render_size, resample)
                self.current_render_image = rendered_image.copy()
                self.current_render_path = image_path
                self.current_render_size = render_size
                self.current_render_resample = resample
                self.current_render_zoom = zoom
                self.current_render_pan = pan_signature

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

    def start_polling(self):
        """ポーリングループを開始する (既存予約は破棄)"""
        self.cancel_polling()
        if not self.is_slideshow_active or not self.folder_path or not self.polling_enabled:
            return
        self.poll_after_id = self.root.after(self.poll_interval_ms, self.poll_folder)

    def cancel_polling(self):
        """次回ポーリング予約を破棄する"""
        if self.poll_after_id is not None:
            self.root.after_cancel(self.poll_after_id)
            self.poll_after_id = None

    def get_settle_seconds(self):
        """confirm-twice 用の最低安定時間 (秒) を返す"""
        ms = max(self.poll_interval_ms * POLL_SETTLE_MULTIPLIER, POLL_SETTLE_MIN_MS)
        return ms / 1000.0

    def resolve_stable_snapshot(self, current_snapshot, stat_failed):
        """current_snapshot から「確定済みスナップショット」を計算して返す。

        戻り値は「差分検出に使うべき、現時点で信頼できるスナップショット」。
        確定済みの path はそのまま、書き込み途中 (pending) や一時的に消えている
        path は前回確定値を持ち越す。完全新規 (前回確定値なし) は載せない。

        - 既存スナップショットと完全一致 → そのまま確定済みとして残す
        - 変化中 (pending) → pending_changes に積み、settle 秒数経過で新値を確定。
          経過していなければ前回確定値を持ち越して「変化していない」扱いにする。
        - 完全新規 (前回確定値なし) → 戻り値に載せず、pending_changes に積むだけ。
          settle 経過後に初めて返り値へ載る。
        - 確定済みなのに current_snapshot から消えた (atomic rename で書き換え中
          のファイルが一瞬 listdir から見えなくなる等) → vanish-pending に積み、
          settle 秒数連続で消え続けたら確定削除。それまでは前回値を持ち越して
          チラつきを防ぐ。stat_failed は呼び出し側で別途持ち越されるのでここでは
          除外する。
        - 一度 pending に入ったものは観測値が変わるたび first_seen をリセット。

        pending_changes の sentinel: 値=None は「vanish-pending」を意味する。
        """
        now = time.perf_counter()
        settle_seconds = self.get_settle_seconds()

        stable = {}
        new_pending = {}

        # 1. current_snapshot に居る path 群を処理 (確定 / 変化中 / 新規)
        for path, value in current_snapshot.items():
            previous_confirmed = self.folder_snapshot.get(path)
            if previous_confirmed == value:
                stable[path] = value
                continue

            pending_entry = self.pending_changes.get(path)
            if pending_entry is not None and pending_entry[0] == value:
                first_seen = pending_entry[1]
                if now - first_seen >= settle_seconds:
                    # settle 経過 → 新値で確定
                    stable[path] = value
                else:
                    # まだ settle 中 → 前回確定値を持ち越して removed 扱いを防ぐ
                    if previous_confirmed is not None:
                        stable[path] = previous_confirmed
                    new_pending[path] = (value, first_seen)
            else:
                # 初観測 or 値が変わったので first_seen を更新
                if previous_confirmed is not None:
                    stable[path] = previous_confirmed
                new_pending[path] = (value, now)

        # 2. 確定済みなのに current_snapshot から消えた path を処理 (vanish settle)
        missing_paths = set(self.folder_snapshot) - set(current_snapshot) - set(stat_failed)
        for path in missing_paths:
            previous_confirmed = self.folder_snapshot[path]
            pending_entry = self.pending_changes.get(path)
            if pending_entry is not None and pending_entry[0] is None:
                first_seen = pending_entry[1]
                if now - first_seen >= settle_seconds:
                    # settle 経過 → 確定削除 (stable に載せない)
                    pass
                else:
                    # まだ settle 中 → 前回値を持ち越して removed 扱いを防ぐ
                    stable[path] = previous_confirmed
                    new_pending[path] = (None, first_seen)
            else:
                # 消失を初観測 (または直前まで別状態だった) → vanish-pending を開始
                stable[path] = previous_confirmed
                new_pending[path] = (None, now)

        self.pending_changes = new_pending
        return stable

    def poll_folder(self):
        """ポーリング 1 周期 (Tk メインスレッドで動作)"""
        self.poll_after_id = None
        try:
            if not self.is_slideshow_active or not self.folder_path or not self.polling_enabled:
                return
            # 単発チェック中は auto poll が割り込まないようにスキップ。
            # (manual の phase1 と phase2 の間に folder_snapshot を動かすと
            #  phase2 の diff が壊れるため。)
            if self.manual_check_after_id is not None:
                return

            try:
                current_snapshot, stat_failed = scan_folder_snapshot(self.folder_path)
            except OSError as exc:
                # listdir 自体が失敗した場合は状態を変更せずに次の周期へ。
                # (ネットワークドライブの瞬断などで、ここで削除扱いにすると混乱するため)
                print(f"[poll] listdir failed for {self.folder_path}: {exc}")
                return

            stable_snapshot = self.resolve_stable_snapshot(current_snapshot, stat_failed)
            added, removed, modified = diff_snapshots(self.folder_snapshot, stable_snapshot, stat_failed)

            if added or removed or modified:
                new_snapshot = dict(stable_snapshot)
                # stat_failed のパスは前回の確定値があれば持ち越し (transient 失敗扱い)
                for path in stat_failed:
                    if path in self.folder_snapshot:
                        new_snapshot[path] = self.folder_snapshot[path]
                self.apply_folder_diff(added, removed, modified, new_snapshot=new_snapshot)
        finally:
            # 例外が出ても polling は止めない (finally で必ず再予約)
            if self.is_slideshow_active and self.folder_path and self.polling_enabled:
                self.poll_after_id = self.root.after(self.poll_interval_ms, self.poll_folder)

    def request_manual_check(self):
        """単発の更新チェック。

        100ms 間隔で 2 回スキャンし、両方で (mtime_ns, size) が一致したものだけを
        反映する。書き込み途中のファイルは弾く。"""
        if not self.folder_path:
            return
        if self.manual_check_after_id is not None:
            # 既に進行中なら多重起動しない
            return

        try:
            first_snapshot, first_failed = scan_folder_snapshot(self.folder_path)
        except OSError as exc:
            print(f"[manual_check] listdir failed: {exc}")
            return

        self.manual_check_after_id = self.root.after(
            MANUAL_CHECK_GAP_MS,
            lambda: self._manual_check_phase2(first_snapshot, first_failed),
        )

    def _manual_check_phase2(self, first_snapshot, first_failed):
        """単発チェックの 2 回目スキャンと差分反映"""
        self.manual_check_after_id = None
        if not self.folder_path:
            return

        try:
            second_snapshot, second_failed = scan_folder_snapshot(self.folder_path)
        except OSError as exc:
            print(f"[manual_check] listdir failed (2nd pass): {exc}")
            return

        combined_failed = first_failed | second_failed
        baseline = self.folder_snapshot  # auto poll は manual_check_after_id != None で抑止済み

        # 両回で同じ (mtime_ns, size) になったものだけを「安定」として扱う。
        # 値が phase1 と phase2 で異なる場合 (まだ書き込み中) や、phase2 で見えない
        # 場合 (一瞬の rename gap) は、baseline に居れば baseline 値を持ち越す。
        # これがないと、書き換え中ファイルや atomic rename 中の path が一旦 removed
        # 扱いになって表示が揺れる。
        stable_snapshot = {}
        for path, value in second_snapshot.items():
            if first_snapshot.get(path) == value:
                stable_snapshot[path] = value
            elif path in baseline:
                # phase1 と phase2 で値不一致 → 書き込み中扱い、baseline を持ち越す
                stable_snapshot[path] = baseline[path]
            # else: 完全新規かつ不安定 → 載せない (確定するまで保留)

        # baseline にあるが second_snapshot に居ない path をフォロー:
        # - combined_failed: 持ち越し (transient stat 失敗)
        # - first_snapshot には居て second_snapshot で消えた: atomic rename の
        #   隙間 / 進行中の書き換えの可能性 → 持ち越し
        # - 両 phase で listdir からも見えず stat_failed でもない: 確定削除
        for path, baseline_value in baseline.items():
            if path in stable_snapshot or path in second_snapshot:
                continue
            if path in combined_failed:
                stable_snapshot[path] = baseline_value
            elif path in first_snapshot:
                # phase1 では見えたが phase2 で消えた → 隙間に当たった可能性。
                # 両 phase 一致しないと removed 確定にしない (安全側)。
                stable_snapshot[path] = baseline_value
            # else: 両 phase で見えず、stat_failed でもない → 確定削除

        added, removed, modified = diff_snapshots(
            baseline,
            stable_snapshot,
            combined_failed,
        )

        if added or removed or modified:
            new_snapshot = dict(stable_snapshot)
            for path in combined_failed:
                if path in baseline:
                    new_snapshot[path] = baseline[path]
            self.apply_folder_diff(added, removed, modified, new_snapshot=new_snapshot)

        # auto poll 側の pending を更新しておくと、復帰直後の重複検知を抑えられる
        self.pending_changes = {}

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
        """更新日時を基準に時系列ソートするためのキー

        スナップショットがあればそこから mtime_ns を引き、無ければ stat する。
        ソートは「初回ロード時にスキャンで得た値」と「ポーリングで得た値」を
        どちらも同じルールで扱えるようにここで吸収する。"""
        snapshot_entry = self.folder_snapshot.get(image_path)
        if snapshot_entry is not None:
            timestamp = snapshot_entry[0]  # mtime_ns
        else:
            try:
                timestamp = os.stat(image_path).st_mtime_ns
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

    def apply_folder_diff(self, added, removed, modified, new_snapshot=None):
        """検出済みのフォルダ差分を image_list と UI に反映する。

        - added/removed/modified: 個々のパス集合 (string set)
        - new_snapshot: ポーリングで取得したスナップショット全体。
                        渡された場合は folder_snapshot をこれで置き換える。
                        渡されない場合 (watchdog 経由など) は追加パスのみ stat して
                        folder_snapshot に追記する。

        差分の発生源 (watchdog/poll) によらず、ソート・current 再解決・サムネ更新・
        待機再開を一箇所で扱うために存在する。"""
        if not (added or removed or modified):
            if new_snapshot is not None:
                self.folder_snapshot = new_snapshot
            return

        # 差分適用前の current_path と「末尾待機中だったか」を覚えておく
        current_path_before = None
        if 0 <= self.current_index < len(self.image_list):
            current_path_before = self.image_list[self.current_index]
        was_at_end = (
            self.current_index == -1
            or self.current_index == len(self.image_list) - 1
        )
        previous_count = len(self.image_list)

        # current が削除された場合の fallback 候補を、削除前リストから path ベースで確保。
        # current より前の要素も同時に削除されると単純な index 流用で 1 枚飛ぶ事故が起きるため、
        # 「現在より後ろにあり、かつ removed でない最初の path」を覚えておく (= next 候補)。
        # 見つからなければ「現在より前にあり、かつ removed でない最後の path」(= prev 候補) を採用。
        next_candidate_path = None
        prev_candidate_path = None
        if current_path_before is not None and removed:
            for path in self.image_list[self.current_index + 1:]:
                if path not in removed:
                    next_candidate_path = path
                    break
            for path in reversed(self.image_list[:self.current_index]):
                if path not in removed:
                    prev_candidate_path = path
                    break

        # スナップショット更新
        if new_snapshot is not None:
            self.folder_snapshot = new_snapshot
        else:
            for path in added:
                try:
                    st = os.stat(path)
                    self.folder_snapshot[path] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    pass

        # 削除を反映
        for path in removed:
            if path in self.image_list:
                self.image_list.remove(path)
            self.folder_snapshot.pop(path, None)
            self.metadata_cache.pop(path, None)

        # 追加を反映
        for path in added:
            if find_path_index(self.image_list, path) < 0:
                self.image_list.append(path)
            self.metadata_cache.pop(path, None)

        # 上書きを反映 (image_list そのものは不変)
        current_image_replaced = False
        for path in modified:
            self.metadata_cache.pop(path, None)
            if path == self.current_source_path:
                self.clear_current_image_cache()
                current_image_replaced = True

        # ソート (スナップショットから mtime を引くので再 stat されない)
        self.image_list.sort(key=self.get_image_sort_key)

        # current_index を path ベースで再解決
        # ルール: 元の current_path が今もあればその位置。
        # 無い (= 削除された) なら next 候補 → prev 候補 → 空 の順で fallback。
        # next/prev 候補は削除適用前に path で確保済みなので、複数同時削除があっても
        # 「期待した次の画像」へ正しく辿り着ける。
        current_force_render = False
        if current_path_before is not None:
            resolved = find_path_index(self.image_list, current_path_before)
            if resolved >= 0:
                self.current_index = resolved
            else:
                resolved_fallback = -1
                if next_candidate_path is not None:
                    resolved_fallback = find_path_index(self.image_list, next_candidate_path)
                if resolved_fallback < 0 and prev_candidate_path is not None:
                    resolved_fallback = find_path_index(self.image_list, prev_candidate_path)
                if resolved_fallback >= 0:
                    self.current_index = resolved_fallback
                elif self.image_list:
                    # next/prev 候補がいずれも無い (= 現在画像が唯一だった等) → 末尾を採用
                    self.current_index = len(self.image_list) - 1
                else:
                    self.current_index = -1
                current_force_render = True

        # シーク UI / サムネ帯
        self.update_seekbar_range()
        only_appended_at_end = (
            bool(added)
            and not removed
            and not modified
            and len(self.image_list) == previous_count + len(added)
        )
        if only_appended_at_end:
            for path in added:
                idx = find_path_index(self.image_list, path)
                if idx == len(self.image_list) - 1:
                    self.append_thumbnail_item(path)
                else:
                    # 中間に入った追加が混ざる場合は安全側で再構築
                    only_appended_at_end = False
                    break
        if not only_appended_at_end:
            self.refresh_thumbnail_strip()

        # 再描画要求は現在画像が差し替わった / 消えた場合のみ。
        # 単純な追加だけの場合はサムネ追加・seekbar 更新で必要な反映は完了している。
        needs_image_render = current_image_replaced or current_force_render
        if needs_image_render:
            # current が別画像に切り替わった (= path 変化) 場合はズームを 1.0x に戻す。
            # current_image_replaced (= 同 path での上書き) はズーム維持。
            if current_force_render:
                self.reset_zoom_state(request_render=False)
            self.request_render(
                image=True,
                metadata=self.metadata_visible,
                thumbnail_highlight=self.thumbnail_visible,
            )

        # 末尾待機からの即時再開 (追加があったときのみ評価する)
        # 古い実装と同じく、ファイル書き込み完了を待つため 500ms 後に進める。
        # (commit 3 で confirm-twice 導入後は不要だが、watchdog 経由のままなので維持)
        if added and self.is_slideshow_active and self.is_playing and was_at_end:
            if self.current_index == -1 or self.current_index < len(self.image_list) - 1:
                self.schedule_next_image(delay_ms=500)

        # 削除ボタンの enable 条件 (現在画像の有無) が変わるので追従させる
        self.update_delete_button_state()

    def load_images_from_folder(self):
        """対象フォルダをスキャンして image_list と folder_snapshot を構築する。"""
        self.image_list = []
        self.folder_snapshot = {}
        if not self.folder_path:
            return

        try:
            snapshot, _stat_failed = scan_folder_snapshot(self.folder_path)
        except OSError as exc:
            print(f"Failed to scan folder {self.folder_path}: {exc}")
            return

        self.folder_snapshot = snapshot
        self.image_list = list(snapshot.keys())
        self.sort_image_list()

    def set_fullscreen_state(self, enabled):
        """フルスクリーン状態を切り替える"""
        self.is_fullscreen = enabled
        self.root.attributes("-fullscreen", enabled)
        self.update_fullscreen_button()

    def refresh_current_folder(self):
        """現在の対象フォルダを読み直して監視対象も更新"""
        self.clear_current_image_cache()
        self.cancel_polling()
        self.pending_changes = {}
        self.load_images_from_folder()
        self.update_seekbar_range()
        self.refresh_thumbnail_strip()
        self.start_polling()
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

        # current_index がここで初めて確定するので、ファイル操作ボタンの enable
        # 状態を改めて評価する。start_slideshow から呼ばれた直後はまだ -1 で、
        # この時点で初めて 0 (or 引き続き -1) に決まる。
        self.update_delete_button_state()

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

        try:
            poll_val = int(self.poll_interval_setting_var.get())
            if poll_val < POLL_INTERVAL_MIN_MS or poll_val > POLL_INTERVAL_MAX_MS:
                raise ValueError
            self.poll_interval_ms = poll_val
        except (ValueError, tk.TclError):
            messagebox.showwarning(
                "警告",
                f"監視間隔は {POLL_INTERVAL_MIN_MS} 〜 {POLL_INTERVAL_MAX_MS} ms の範囲で指定してください。",
            )
            return
        self.polling_enabled = bool(self.polling_enabled_setting_var.get())
        self.update_polling_status_ui()

        self.sync_interval_ui()
        self.menu_frame.place_forget()  # メニューを隠す
        
        self.is_slideshow_active = True
        self.current_index = -1
        self.is_playing = True
        self.update_play_pause_button()
        self.update_delete_button_state()
        self.set_fullscreen_state(True)
        self.request_render(layout=True)
        self.refresh_current_folder()

    def return_to_menu(self):
        """再生を止めて設定画面へ戻る"""
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
        self.panels_hidden_for_resize = False
        self.resume_play_after_resize = False
        self.cancel_metadata_refresh()
        self.cancel_thumbnail_highlight()
        self.cancel_thumbnail_follow()
        self.set_debug_hud_value("render_status", "idle")
        self.set_debug_hud_value("preview_status", "idle")
        
        # 自動再生のタイマーをキャンセル
        self.cancel_scheduled_image()

        # ポーリング監視も一時停止
        self.cancel_polling()
        self.pending_changes = {}
        if self.manual_check_after_id is not None:
            self.root.after_cancel(self.manual_check_after_id)
            self.manual_check_after_id = None
        self.update_delete_button_state()
        self.request_render(layout=True)

    def on_escape(self, event=None):
        """再生中は Esc でフルスクリーン表示を切り替える"""
        if not self.is_slideshow_active:
            return

        self.toggle_fullscreen_mode()
        return "break"

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
            # 画像が切り替わったらズームと注視位置を初期値へ戻す
            self.reset_zoom_state(request_render=False)
        self.request_render(image=True, metadata=self.metadata_visible, thumbnail_highlight=True)

    def update_seekbar_range(self):
        """画像リストの長さに合わせてシークバーの最大値を更新"""
        max_value = max(1, len(self.image_list))
        self.seekbar.config(to=max_value, state=tk.NORMAL if self.image_list else tk.DISABLED)

    def on_press(self, event):
        """マウス/タッチの押下開始"""
        if self.is_zoomed():
            # ズーム中はドラッグを pan として扱う。タップ/スワイプ系は無効。
            self.pan_drag_active = True
            self.pan_drag_start = (
                event.x,
                event.y,
                self.pan_offset_x,
                self.pan_offset_y,
            )
            self.start_x = None
        else:
            self.pan_drag_active = False
            self.pan_drag_start = None
            self.start_x = event.x

    def on_release(self, event):
        """マウス/タッチのリリース（スワイプ・タップ判定）"""
        # ズーム中のドラッグ pan は on_pan_drag_motion で逐次反映済み。
        # 終了処理だけして、タップ/スワイプ判定はスキップする。
        if self.pan_drag_active:
            self.pan_drag_active = False
            self.pan_drag_start = None
            return

        if self.start_x is None:
            return

        # 押下時は 1.0x だったが、押下中に Ctrl+Wheel / スライダー等で zoom > 1.0x
        # になっていたら tap/swipe は仕様に反するので抑止する。
        if self.is_zoomed():
            self.start_x = None
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
        self.update_delete_button_state()
        if self.is_playing:
            print("Play")
            self.schedule_next_image()
        else:
            print("Pause")
            self.cancel_scheduled_image()

    def on_closing(self):
        """アプリ終了時の処理"""
        self.cancel_polling()
        if self.manual_check_after_id is not None:
            self.root.after_cancel(self.manual_check_after_id)
            self.manual_check_after_id = None
        self.clear_current_image_cache()
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
