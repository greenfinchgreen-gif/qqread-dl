@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title qqread-dl - 安装依赖

echo ============================================================
echo   安装依赖（只需运行一次）
echo ============================================================
echo.

rem ---------- 1) 定位 Python ----------
if not "%QQREAD_PY%"=="" (
    set "PY=%QQREAD_PY%"
    goto :pip
)
where python >nul 2>nul && set "PY=python" && goto :pip
where py >nul 2>nul && set "PY=py -3" && goto :pip

echo [错误] 未找到 Python 解释器，无法安装依赖。
echo        请先安装 Python 3.8+：https://www.python.org/downloads/
echo        安装时务必勾选 "Add Python to PATH"。
echo.
pause
exit /b 1

rem ---------- 2) 安装 numpy + Pillow ----------
:pip
echo [1/3] Python 解释器：%PY%
echo.
echo [2/3] 安装字体还原层依赖：numpy、Pillow
echo       （免费章下载不需要它们，但 VIP 章的字体还原必须要）
echo.
%PY% -m pip install --upgrade numpy pillow
if errorlevel 1 (
    echo.
    echo [警告] 安装失败。常见原因与处理：
    echo        * 网络问题 -^> 可加国内镜像重试：
    echo            %PY% -m pip install numpy pillow -i https://pypi.tuna.tsinghua.edu.cn/simple
    echo        * 权限问题 -^> 用管理员身份重新运行本脚本
    echo.
) else (
    echo.
    echo [成功] numpy / Pillow 已就绪。
)

rem ---------- 3) 检测 Node.js（VIP 章解密需要） ----------
echo.
echo [3/3] 检测 Node.js（只有下载 VIP 章节才需要）
if not "%NODE_EXE%"=="" (
    echo       NODE_EXE = %NODE_EXE%
    goto :verify
)
where node >nul 2>nul && (
    for /f "delims=" %%v in ('node -v 2^>nul') do echo       已找到 Node.js %%v
    goto :verify
)

echo       [提示] 未检测到 Node.js。
echo              只下免费章可以忽略；要下 VIP 章请安装：
echo                  https://nodejs.org/   或   winget install OpenJS.NodeJS
echo              已安装但不在 PATH 时：set NODE_EXE=D:\你的路径\node.exe

:verify
echo.
echo ============================================================
echo   依赖安装流程结束
echo   下一步：双击 配置账号.bat（想下 VIP 章）或 一键运行.bat
echo ============================================================
echo.
pause
endlocal
