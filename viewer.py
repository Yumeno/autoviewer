import os
import time
import queue
import tkinter as tk
from tkinter import filedialog, messagebox
from PIL import Image, ImageTk
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
        self.is_fullscreen = False
        self.after_id = None
        
        # タップ・スワイプ判定用
        self.start_x = None
        
        # 監視用
        self.observer = None
        self.image_queue = queue.Queue()

        # UI要素の構築
        self.setup_ui()
        self.sync_interval_ui()
        self.update_play_pause_button()
        
        # キーバインド
        self.root.bind("<Escape>", self.exit_fullscreen)
        self.root.bind("<Right>", lambda e: self.next_image())
        self.root.bind("<Left>", lambda e: self.prev_image())
        self.root.bind("<space>", lambda e: self.next_image())
        self.root.bind("p", self.toggle_play)
        self.root.bind("P", self.toggle_play)

        # キューの定期チェックを開始
        self.check_queue()

    def setup_ui(self):
        """設定画面と画像表示画面の構築"""
        # 画像表示用のラベル
        self.image_label = tk.Label(self.root, bg='black')
        self.image_label.pack(fill=tk.BOTH, expand=True)

        # マウス（タッチ）イベントのバインド
        self.image_label.bind("<ButtonPress-1>", self.on_press)
        self.image_label.bind("<ButtonRelease-1>", self.on_release)

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

        controls_frame = tk.Frame(self.seekbar_frame, bg='#222222')
        controls_frame.pack(fill=tk.X, padx=10, pady=(0, 8))

        self.play_pause_button = tk.Button(
            controls_frame,
            text="一時停止",
            width=10,
            command=self.toggle_play,
            bg='#4CAF50',
            fg='white'
        )
        self.play_pause_button.pack(side=tk.LEFT, padx=(0, 10))

        self.interval_status_var = tk.StringVar()
        tk.Label(
            controls_frame,
            textvariable=self.interval_status_var,
            fg='white',
            bg='#222222'
        ).pack(side=tk.LEFT)

        tk.Button(
            controls_frame,
            text="閉じる",
            width=8,
            command=self.toggle_seekbar
        ).pack(side=tk.RIGHT)

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
        help_text = "【操作方法】\n・Escキー: 設定画面に戻る\n・→ / Space: 次の画像\n・←: 前の画像\n・P: 再生/一時停止\n・中央タップ: 操作パネル表示"
        tk.Label(self.menu_frame, text=help_text, fg='#AAAAAA', bg='#333333', justify=tk.LEFT).pack(pady=10)

    def select_folder(self):
        folder = filedialog.askdirectory()
        if folder:
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

    def cancel_scheduled_image(self):
        """予約済みの自動送りを解除"""
        if self.after_id:
            self.root.after_cancel(self.after_id)
            self.after_id = None

    def schedule_next_image(self, delay_ms=None):
        """次の自動送りを予約"""
        self.cancel_scheduled_image()

        if self.is_playing and self.is_fullscreen:
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
        if self.observer:
            self.observer.stop()
            self.observer.join()
        
        if self.folder_path:
            event_handler = NewImageHandler(self.image_queue)
            self.observer = Observer()
            self.observer.schedule(event_handler, self.folder_path, recursive=False)
            self.observer.start()

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
        self.load_images_from_folder()
        if not self.image_list:
            messagebox.showinfo("情報", "選択したフォルダに画像が見つかりませんでしたが、待機モードに入ります。\n画像が追加されると表示されます。")
        
        self.update_seekbar_range()
        self.start_observer()
        self.menu_frame.place_forget()  # メニューを隠す
        
        # フルスクリーン化
        self.is_fullscreen = True
        self.root.attributes("-fullscreen", True)
        
        self.current_index = -1
        self.is_playing = True
        self.update_play_pause_button()
        self.next_image()

    def exit_fullscreen(self, event=None):
        """フルスクリーン解除・設定メニュー表示"""
        self.is_fullscreen = False
        self.root.attributes("-fullscreen", False)
        self.menu_frame.place(relx=0.5, rely=0.5, anchor=tk.CENTER)
        
        if self.seekbar_visible:
            self.seekbar_frame.place_forget()
            self.seekbar_visible = False
        
        # 自動再生のタイマーをキャンセル
        self.cancel_scheduled_image()
        
        # 監視も一時停止
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None

    def show_image(self, index):
        """指定したインデックスの画像を表示"""
        if not self.image_list or index < 0 or index >= len(self.image_list):
            # 表示する画像がない場合は黒画面
            self.image_label.config(image='')
            return

        image_path = self.image_list[index]
        
        try:
            # 画像が書き込み中の場合を考慮して少し待機してリトライ
            for _ in range(3):
                try:
                    img = Image.open(image_path)
                    break
                except IOError:
                    time.sleep(0.5)
            else:
                print(f"Failed to open image: {image_path}")
                return

            # 画面サイズに合わせてリサイズ (アスペクト比維持)
            screen_width = self.root.winfo_width()
            screen_height = self.root.winfo_height()
            
            # 初期起動時などサイズが1になる場合の回避策
            if screen_width <= 1 or screen_height <= 1:
                screen_width = self.root.winfo_screenwidth()
                screen_height = self.root.winfo_screenheight()

            # 変更点：小さい画像も画面に合わせて拡大・縮小するように計算
            img_width, img_height = img.size
            ratio = min(screen_width / img_width, screen_height / img_height)
            new_width = int(img_width * ratio)
            new_height = int(img_height * ratio)
            
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            self.photo = ImageTk.PhotoImage(img)
            self.image_label.config(image=self.photo)
            self.current_index = index

            # シークバーの値を同期
            if self.seekbar_var.get() != index + 1:
                self.seekbar_var.set(index + 1)

        except Exception as e:
            print(f"Error loading {image_path}: {e}")

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
        if not self.is_fullscreen:
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
        if not self.is_fullscreen:
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
                
                # もし現在最後の画像を表示中で待機状態だったなら、すぐ新しい画像へ進む
                if self.is_fullscreen and self.is_playing and should_resume_from_wait:
                    if self.current_index == -1 or self.current_index < len(self.image_list) - 1:
                        # 500ms待ってから表示（ファイルの書き込み完了を待つため）
                        self.schedule_next_image(delay_ms=500)

        # 500ms後に再チェック
        self.root.after(500, self.check_queue)

    def on_closing(self):
        """アプリ終了時の処理"""
        if self.observer:
            self.observer.stop()
            self.observer.join()
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = ImageViewerApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_closing)
    root.mainloop()
