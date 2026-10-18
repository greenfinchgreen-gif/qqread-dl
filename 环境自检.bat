@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title qqread-dl - 环境自检

echo ============================================================
echo   环境自检
echo ============================================================
echo.

if not "%QQREAD_PY%"=="" (
    set "PY=%QQREAD_PY%"
    goto :run
)
where python >nul 2>nul && set "PY=python" && goto :run
where py >nul 2>nul && set "PY=py -3" && goto :run

echo [×] 未找到 Python 解释器
echo     处理：安装 Python 3.8+（勾选 Add Python to PATH）
echo           或设置 QQREAD_PY 指向 python.exe
echo.
goto :node

:run
echo [1] Python 解释器
%PY% -c "import sys;print('    OK',sys.executable,sys.version.split()[0])" 2>nul
if errorlevel 1 (
    echo     [×] 解释器不可用
    goto :node
)

echo.
echo [2] Python 依赖
%PY% -c "import numpy;print('    OK numpy',numpy.__version__)" 2>nul
if errorlevel 1 echo     [×] 缺少 numpy -^> 双击 安装依赖.bat
%PY% -c "import PIL;print('    OK Pillow',PIL.__version__)" 2>nul
if errorlevel 1 echo     [×] 缺少 Pillow -^> 双击 安装依赖.bat

echo.
echo [3] 参照字体（VIP 章字体还原必需）
%PY% -c "import qqread_font as f;print('    OK',f._find_ref_font())" 2>nul
if errorlevel 1 (
    echo     [×] 找不到参照字体，尝试：
    echo         set QQREAD_REF_FONT=C:\Windows\Fonts\msyh.ttc
)

echo.
echo [4] 项目文件完整性
%PY% -c "import qqread_config, qqread_vip, qqread_font;print('    OK 主程序模块可导入')" 2>nul
if errorlevel 1 echo     [×] 模块导入失败，请查看上方报错

echo.
echo [5] 凭据配置状态
%PY% qqread_config.py 2>&1

:node
echo.
echo [6] Node.js（VIP 章解密必需，免费章不需要）
if not "%NODE_EXE%"=="" (
    echo     OK NODE_EXE=%NODE_EXE%
    goto :end
)
where node >nul 2>nul && (
    for /f "delims=" %%v in ('node -v 2^>nul') do echo     OK Node.js %%v
    goto :end
)
echo     [!] 未检测到 Node.js -^> 下载 https://nodejs.org/
echo         已安装但不在 PATH：set NODE_EXE=D:\你的路径\node.exe

:end
echo.
echo ============================================================
echo   自检结束。全部 OK 即可双击 一键运行.bat
echo ============================================================
echo.
pause
endlocal
