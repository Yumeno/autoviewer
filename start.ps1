# 実行ポリシーを一時的にバイパスしてスクリプトを実行できるようにする
$ErrorActionPreference = "Stop"

Write-Host "=========================================="
Write-Host "画像ビューアー 起動準備中..."
Write-Host "=========================================="

# Pythonがインストールされているか確認
try {
    $test = python --version
} catch {
    Write-Host "[エラー] Pythonが見つかりません。Pythonをインストールしてから再度実行してください。" -ForegroundColor Red
    Write-Host "※インストール時、「Add python.exe to PATH」にチェックを入れてください。"
    Read-Host "Enterキーを押して終了します"
    exit
}

# 必要なライブラリのインストール
Write-Host "必要な機能（Pillow, watchdog）を確認・インストールしています..."
python -m pip install --upgrade pip | Out-Null
python -m pip install Pillow watchdog

# アプリケーションの起動
Write-Host "準備完了！ビューアーを起動します。"
python viewer.py