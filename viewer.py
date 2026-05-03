import os
import subprocess
import sys
import time
import queue
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk, ExifTags, UnidentifiedImageError
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# 対応する画像フォーマット
SUPPORTED_EXTS = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}
MIN_INTERVAL_SECONDS = 1.0
MAX_INTERVAL_SECONDS = 30.0
INTERVAL_STEP_SECONDS = 0.5

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
        self.current_render_image = None
        self.photo = None
        self.metadata_overlay = None
        self.metadata_overlay_visible = False
        self.metadata_overlay_geometry = ""
        self.metadata_text_value = None
        self.metadata_cache = {}
        self.render_scheduled = False
        self.layout_dirty = False
        self.image_dirty = False
        self.metadata_dirty = False
        self.thumbnail_highlight_dirty = False
        self.resize_after_id = None
        self.resize_preview_after_id = None
        self.is_live_resizing = False
        self.thumbnail_scroll_after_id = None
        self.thumbnail_follow_path = None
        self.last_root_size = None
        
        # タップ・スワイプ判定用
        self.start_x = None
        
        # 監視用
        self.observer = None
        self.image_queue = queue.Queue()

        # UI要素の構築
        self.setup_ui()
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

        self.image_area = tk.Frame(self.content_frame, bg='black')
        self.image_area.grid(row=0, column=0, sticky="nsew")

        self.metadata_overlay = tk.Toplevel(self.root)
        self.metadata_overlay.withdraw()
        self.metadata_overlay.overrideredirect(True)
        self.metadata_overlay.transient(self.root)
        self.metadata_overlay.attributes("-alpha", 0.72)
        self.metadata_overlay.configure(bg='#141414')

        self.metadata_frame = tk.Frame(self.metadata_overlay, bg='#141414', width=360, bd=1, relief=tk.SOLID)
        self.metadata_frame.pack(fill=tk.BOTH, expand=True)
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

        # マウス（タッチ）イベントのバインド
        self.image_label.bind("<ButtonPress-1>", self.on_press)
        self.image_label.bind("<ButtonRelease-1>", self.on_release)
        self.image_label.bind("<Button-3>", self.show_context_menu)

        self.thumbnail_frame = tk.Frame(self.content_frame, bg='#161616', height=164, bd=1, relief=tk.SOLID)
        self.thumbnail_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
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
        self.layout_dirty = self.layout_dirty or layout
        self.image_dirty = self.image_dirty or image
        self.metadata_dirty = self.metadata_dirty or metadata
        self.thumbnail_highlight_dirty = self.thumbnail_highlight_dirty or thumbnail_highlight

        if self.render_scheduled:
            return

        self.render_scheduled = True
        self.root.after_idle(self.render_pending_updates)

    def render_pending_updates(self):
        """予約済みのUI更新をまとめて反映"""
        self.render_scheduled = False

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
            return

        if self.image_dirty:
            self.render_current_image()
            self.image_dirty = False
        else:
            if self.metadata_dirty:
                self.refresh_metadata_display()
                self.metadata_dirty = False
            if self.thumbnail_highlight_dirty:
                self.highlight_current_thumbnail()
                self.thumbnail_highlight_dirty = False

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

    def apply_panel_layout(self):
        """サムネイル帯と情報欄の表示状態をレイアウトへ反映"""
        if not self.is_slideshow_active:
            if self.metadata_overlay is not None and self.metadata_overlay_visible:
                self.metadata_overlay.withdraw()
                self.metadata_overlay_visible = False
            self.thumbnail_frame.grid_remove()
            self.update_thumbnail_buttons()
            self.update_metadata_button()
            return

        if self.metadata_visible:
            self.update_metadata_overlay_geometry()
            if not self.metadata_overlay_visible:
                self.metadata_overlay.deiconify()
                self.metadata_overlay.lift(self.root)
                self.metadata_overlay_visible = True
        elif self.metadata_overlay_visible:
            self.metadata_overlay.withdraw()
            self.metadata_overlay_visible = False

        if self.thumbnail_visible:
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
        self.request_render(layout=True, image=True, thumbnail_highlight=True)

    def toggle_metadata_panel(self):
        """情報欄の表示・非表示を切り替える"""
        if not self.is_slideshow_active:
            return

        self.metadata_visible = not self.metadata_visible
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

        if self.metadata_visible and self.metadata_overlay_visible:
            self.update_metadata_overlay_geometry()
            self.metadata_overlay.lift(self.root)

        self.is_live_resizing = True
        if self.current_source_image is not None and self.resize_preview_after_id is None:
            self.resize_preview_after_id = self.root.after_idle(self.flush_resize_preview)

        if self.resize_after_id:
            self.root.after_cancel(self.resize_after_id)
        self.resize_after_id = self.root.after(120, self.flush_resize_updates)

    def flush_resize_preview(self):
        """リサイズ中の軽量プレビュー描画"""
        self.resize_preview_after_id = None
        if not self.is_slideshow_active or not self.is_live_resizing or self.current_source_image is None:
            return

        self.render_current_image(
            resample=Image.Resampling.BILINEAR,
            refresh_metadata=False,
            refresh_thumbnail=False,
            use_preview_source=True,
        )

    def flush_resize_updates(self):
        """連続するConfigureイベント後に再描画をまとめて実行"""
        self.resize_after_id = None
        if not self.is_slideshow_active:
            return

        self.is_live_resizing = False
        self.request_render(image=True)

    def update_metadata_overlay_geometry(self):
        """右端オーバーレイ情報欄の位置とサイズを更新"""
        area_x = self.image_area.winfo_rootx()
        area_y = self.image_area.winfo_rooty()
        width = self.image_area.winfo_width()
        height = self.image_area.winfo_height()

        if width <= 1 or height <= 1:
            return

        margin = 12
        available_width = max(180, width - (margin * 2))
        available_height = max(140, height - (margin * 2))
        overlay_width = min(420, max(220, int(width * 0.28)), available_width)
        overlay_height = min(max(180, int(height * 0.76)), available_height)
        overlay_x = area_x + max(margin, width - overlay_width - margin)
        overlay_y = area_y + margin
        geometry = f"{overlay_width}x{overlay_height}+{overlay_x}+{overlay_y}"
        if geometry != self.metadata_overlay_geometry:
            self.metadata_overlay.geometry(geometry)
            self.metadata_overlay_geometry = geometry

    def highlight_current_thumbnail(self):
        """現在表示中のサムネイルを強調表示"""
        current_path = None
        if 0 <= self.current_index < len(self.image_list):
            current_path = self.image_list[self.current_index]

        for image_path, button in self.thumbnail_buttons.items():
            if image_path == current_path:
                button.config(relief=tk.SOLID, bg='#4CAF50', highlightbackground='#4CAF50')
            else:
                button.config(relief=tk.FLAT, bg='#222222', highlightbackground='#222222')

        if not self.thumbnail_visible or not current_path:
            self.cancel_thumbnail_follow()
            return

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

    def render_current_image(
        self,
        *,
        resample=Image.Resampling.LANCZOS,
        refresh_metadata=True,
        refresh_thumbnail=True,
        use_preview_source=False,
    ):
        """現在選択中の画像を再描画"""
        image_path = self.get_current_image_path()
        if not image_path:
            self.image_label.config(image='')
            self.photo = None
            self.clear_current_image_cache()
            return

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

            screen_width = self.image_area.winfo_width()
            screen_height = self.image_area.winfo_height()

            if screen_width <= 1 or screen_height <= 1:
                screen_width = self.root.winfo_screenwidth()
                screen_height = self.root.winfo_screenheight()

            img_width, img_height = img.size
            ratio = min(screen_width / img_width, screen_height / img_height)
            new_width = int(img_width * ratio)
            new_height = int(img_height * ratio)

            rendered_image = img.resize((new_width, new_height), resample)
            self.current_render_image = rendered_image.copy()

            self.photo = ImageTk.PhotoImage(rendered_image)
            self.image_label.config(image=self.photo)

            if self.seekbar_var.get() != self.current_index + 1:
                self.seekbar_var.set(self.current_index + 1)

            if refresh_metadata:
                self.refresh_metadata_display(image_path=image_path, image=img)
                self.metadata_dirty = False
            if refresh_thumbnail:
                self.highlight_current_thumbnail()
                self.thumbnail_highlight_dirty = False

        except Exception as e:
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

    def schedule_next_image(self, delay_ms=None):
        """次の自動送りを予約"""
        self.cancel_scheduled_image()

        if self.is_playing and self.is_slideshow_active:
            next_delay = self.interval_ms if delay_ms is None else delay_ms
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
            self.root.after(80, lambda: self.request_render(image=True))
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
        self.cancel_scheduled_image()

        if self.image_list:
            if self.current_index < len(self.image_list) - 1:
                self.show_image(self.current_index + 1)
            else:
                # 末尾まで来たので待機
                print("End of list. Waiting for new images...")
                pass
        
        self.schedule_next_image()

    def prev_image(self):
        """前の画像を表示"""
        self.cancel_scheduled_image()

        if self.image_list and self.current_index > 0:
            self.show_image(self.current_index - 1)
            
        self.schedule_next_image()

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
        if self.metadata_overlay is not None:
            self.metadata_overlay.destroy()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = ImageViewerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()
