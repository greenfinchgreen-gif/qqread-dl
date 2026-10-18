@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title qqread-dl - 配置账号凭据

echo ============================================================
echo   配置账号凭据（下载 VIP 章节才需要）
echo ============================================================
echo.
echo 本脚本会做三件事：
echo   1. 在【项目之外】的凭据目录生成 credentials.json 模板
echo   2. 打印凭据目录位置并自动打开它
echo   3. 你把自己的 Cookie 粘进 cookie 字段后重新运行即可生效
echo.
echo 说明：凭据只存在项目外，不会被 git 提交，也不会写进源码。
echo.

rem ---------- 定位 Python ----------
if not "%QQREAD_PY%"=="" (
    set "PY=%QQREAD_PY%"
    goto :gen
)
where python >nul 2>nul && set "PY=python" && goto :gen
where py >nul 2>nul && set "PY=py -3" && goto :gen

echo [错误] 未找到 Python 解释器。
echo        请先安装 Python 3.8+ 并勾选 "Add Python to PATH"。
echo.
pause
exit /b 1

:gen
echo [1/3] 生成凭据模板 ...
echo.
rem 先把模板路径写到临时文件，再用 for 读回，避免中文路径与管道编码问题
%PY% -c "import qqread_config as c; p=c.write_credential_template(); print(p or '')" > "%TEMP%\qqread_cred_path.txt" 2>nul
set "CREDPATH="
for /f "usebackq delims=" %%p in ("%TEMP%\qqread_cred_path.txt") do set "CREDPATH=%%p"
del "%TEMP%\qqread_cred_path.txt" >nul 2>nul

if "%CREDPATH%"=="" (
    echo [警告] 未能生成模板（凭据目录全部不可写？）。
    echo        可手动设置凭据目录后重试：
    echo            set QQREAD_HOME=D:\你的私有目录\.qqread
    echo.
) else (
    echo       模板已就绪：%CREDPATH%
)

echo.
echo [2/3] 当前配置状态：
echo.
%PY% qqread_config.py

echo.
echo [3/3] 打开凭据所在目录 ...
if not "%CREDPATH%"=="" (
    explorer /select,"%CREDPATH%" >nul 2>nul
) else (
    explorer . >nul 2>nul
)

echo.
echo ============================================================
echo   接下来请手动完成（约 1 分钟）：
echo.
echo     a. 用 Edge/Chrome 打开 https://book.qq.com 并登录
echo     b. 按 F12 -^> Application/应用程序 -^> Cookies -^> book.qq.com
echo     c. 全选复制所有 Cookie，或使用 Cookie 导出插件导出
echo     d. 粘进 credentials.json 的 "cookie" 字段并保存（保持 JSON 格式）
echo     e. 重新双击本脚本，看到「实际生效来源」是你的文件即成功
echo.
echo   验证：python qqread_config.py
echo   Cookie 关键字段需包含 uid / fuid / usid，缺了只能下免费章。
echo ============================================================
echo.
pause
endlocal
