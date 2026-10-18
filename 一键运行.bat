@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title qqread-dl - 一键运行

echo ============================================================
echo   qqread-dl · QQ阅读小说下载器（book.qq.com）
echo ============================================================
echo.

rem ---------- 1) 定位 Python 解释器 ----------
if not "%QQREAD_PY%"=="" (
    set "PY=%QQREAD_PY%"
    goto :check
)
where python >nul 2>nul && set "PY=python" && goto :check
where py >nul 2>nul && set "PY=py -3" && goto :check

echo [错误] 未找到 Python 解释器。
echo.
echo   请先安装 Python 3.8 及以上：https://www.python.org/downloads/
echo   安装时务必勾选 "Add Python to PATH"。
echo.
echo   已安装但不在 PATH 时，可手动指定：
echo       set QQREAD_PY=D:\你的路径\python.exe
echo.
pause
exit /b 1

:check
%PY% -c "import sys; sys.exit(0 if sys.version_info>=(3,8) else 1)" >nul 2>nul
if errorlevel 1 (
    echo [错误] Python 版本过低，需要 3.8 及以上。
    echo        当前解释器：%PY%
    echo.
    pause
    exit /b 1
)

echo [1/2] 解释器：%PY%
for /f "delims=" %%v in ('%PY% -c "import sys;print(sys.version.split()[0])" 2^>nul') do set "PYVER=%%v"
echo       版本：%PYVER%

rem ---------- 2) 运行主程序 ----------
echo [2/2] 启动主程序（首次运行会重建字形索引，可能等待数分钟）...
echo.
echo ------------------------------------------------------------
%PY% main.py
set "RC=%ERRORLEVEL%"
echo ------------------------------------------------------------
echo.

if "%RC%"=="0" (
    echo [完成] 输出文件在 output 目录：
    echo          output\搜索结果.json   搜索前 10 条
    echo          output\^<书名^>.txt     小说正文
) else (
    echo [失败] 退出码 %RC%，请查看上方错误信息。
    echo        排查环境问题可双击运行 环境自检.bat
)
echo.
pause
endlocal
