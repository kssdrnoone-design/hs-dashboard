@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo =====================================================
echo  高校受験アプリ APK再ビルド
echo =====================================================
echo.
echo ※ 通常は実行不要です。GitHub Pages更新だけで最新情報がアプリに反映されます。
echo ※ オフラインで表示される同梱版HTMLを最新化したい場合だけ実行してください。
echo ※ ビルドには2〜3分かかります。
echo.
pause

REM 最新のindex.htmlをAndroidのassetsへコピー
if not exist "index.html" (
    echo [ERROR] index.html が見つかりません。先に 00_高校情報収集.bat を実行してください
    pause
    exit /b 1
)
copy /Y "index.html" "C:\AndroidProjects\HighSchoolDashboard\app\src\main\assets\index.html" >nul
echo [OK] assets/index.html を最新化しました

REM 環境変数設定
set "JAVA_HOME=C:\Program Files\Android\Android Studio1\jbr"
set "ANDROID_HOME=C:\Users\kssdr\AppData\Local\Android\Sdk"
set "PATH=%JAVA_HOME%\bin;%PATH%"
set "GRADLE=C:\Users\kssdr\.gradle\wrapper\dists\gradle-8.11.1-bin\bpt9gzteqjrbo1mjrsomdt32c\gradle-8.11.1\bin\gradle.bat"

REM Gradleビルド
cd /d "C:\AndroidProjects\HighSchoolDashboard"
echo.
echo ----- Gradle assembleDebug 実行中 -----
call "%GRADLE%" assembleDebug --no-daemon
if errorlevel 1 (
    echo [ERROR] ビルド失敗
    pause
    exit /b 1
)

REM APKを配布用フォルダへコピー
cd /d "%~dp0"
for /f "tokens=1-3 delims=/" %%a in ("%date%") do (
    set YYYY=%%a
    set MM=%%b
    set DD=%%c
)
set DATESTAMP=%YYYY%%MM%%DD%
copy /Y "C:\AndroidProjects\HighSchoolDashboard\app\build\outputs\apk\debug\app-debug.apk" "高校受験_v1.0_%DATESTAMP%.apk"
echo.
echo [OK] APK生成完了: 高校受験_v1.0_%DATESTAMP%.apk
echo.
echo このファイルをGoogleドライブ/LINE等で親と息子のスマホに共有してください。
pause
