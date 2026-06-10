@echo off
REM CMS · 轻量 git pull · bind mount 自动加载新代码（不重建镜像）
REM 使用：放桌面双击 / 元川さん 远程登录后双击
REM
REM 跟 redeploy.bat 区别：
REM   - redeploy.bat = 拉代码 + 清缓存 + docker compose build + restart （重型）
REM   - update-cms.bat = git pull + restart streamlit（轻量 · 日常 push 后用这个 · 不 build）
REM     restart 是必须的：shared/ 下被 import 的模块（i18n / auth / cache 等）
REM     是进程启动时求值一次，仅 bind mount 不重启进程不会生效。
REM
REM 何时改用 redeploy.bat:
REM   - 新增 Python 依赖（pyproject.toml / requirements.txt 改了）
REM   - 改 .env / docker-compose.yml / Dockerfile

chcp 65001 >nul
title CMS Update · 元川さん
cd /d C:\Users\smiki\CMS-v230

echo.
echo ============================================
echo   CMS git pull · %date% %time%
echo ============================================
echo.

echo [1/3] git pull origin main...
git pull origin main
if errorlevel 1 (
    echo.
    echo [ERROR] git pull 失败 · 看上面错误
    echo  - 常见：本地有未提交改动 / 网络问题 / merge conflict
    pause
    exit /b 1
)

echo.
echo [2/3] 当前 HEAD:
git log -1 --oneline

echo.
echo [3/4] 重启 streamlit 容器（让 shared/ 等 import 模块改动生效）...
docker compose -f deploy\windows\docker-compose.yml restart streamlit

echo.
echo [4/4] Streamlit 容器状态:
docker compose -f deploy\windows\docker-compose.yml ps streamlit

echo.
echo ============================================
echo   完成 · 新代码已加载 + streamlit 已重启
echo ============================================
echo.
echo 如新增依赖 / 改 .env / 改 Dockerfile · 请改跑 redeploy.bat
echo.
pause
