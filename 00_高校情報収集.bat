@echo off
cd /d "%~dp0"
set PYTHONUTF8=1

echo =====================================================
echo  高校受験ダッシュボード 収集＆公開
echo =====================================================

REM === Phase1: スクレイプ＆HTML生成 ===
python high_school_dashboard.py
if errorlevel 1 (
    echo [ERROR] Python実行に失敗しました
    pause
    exit /b 1
)

REM === Phase2: GitHub Pages 自動デプロイ ===
echo.
echo ----- GitHub Pagesに公開中 -----
git add index.html reports/index.html 03_config.json 2>nul
git diff --cached --quiet
if errorlevel 1 (
    git -c user.email="kssdrnoone-design@users.noreply.github.com" -c user.name="kssdrnoone-design" commit -m "update: %date% %time%" >nul
    git push origin main
    if errorlevel 1 (
        echo [WARN] push失敗。ネット接続または認証を確認してください
    ) else (
        echo [OK] GitHub Pages更新完了
        echo 公開URL: https://kssdrnoone-design.github.io/hs-dashboard/
    )
) else (
    echo [SKIP] HTMLに変更なし（push不要）
)

echo.
echo 完了！
pause
