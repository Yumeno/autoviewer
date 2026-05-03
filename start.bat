@echo off
chcp 65001 > nul
echo ==========================================
echo 画像ビューアー 起動準備中...
echo ==========================================

REM Pythonがインストールされているか確認
python --version >nul 2>&1
if %errorlevel% neq 0 (
    color 4F
    echo [エラー] Pythonが見つかりません。Pythonをインストールしてから再度実行してください。
    echo ※インストール時、「Add python.exe to PATH」にチェックを入れてください。
    pause
    exit /b
)

REM 必要なライブラリのインストール
echo 必要な機能（Pillow, watchdog）を確認・インストールしています...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install Pillow watchdog

REM アプリケーションの起動
echo 準備完了！ビューアーを起動します。
python viewer.py