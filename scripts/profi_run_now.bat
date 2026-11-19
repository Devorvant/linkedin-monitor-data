@echo off
setlocal EnableExtensions
chcp 65001 >nul
title Profi.ru - manual run

set "APP_DIR=H:\Users\kusc\AppData\Local\Programs\Python\Python311\linkedin_desktop_automation"
set "PYTHON_EXE=%APP_DIR%\..\python.exe"
set "ENTRY_FILE=%APP_DIR%\cache\profi_worker_entry_runtime.py"
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
set "RUN_EXIT=1"

if not exist "%PYTHON_EXE%" goto :python_missing
pushd "%APP_DIR%"
if errorlevel 1 goto :folder_error

echo Обновляю запускатель Профи...
"%PYTHON_EXE%" -u -c "import pathlib,sys; from urllib.request import Request,urlopen; p=pathlib.Path(sys.argv[1]); r=Request('https://raw.githubusercontent.com/Devorvant/linkedin-monitor-data/main/scripts/profi_worker_entry.py', headers={'User-Agent':'profi-manual-launcher','Cache-Control':'no-cache'}); data=urlopen(r,timeout=30).read(); compile(data.decode('utf-8-sig'),str(p),'exec'); p.parent.mkdir(parents=True,exist_ok=True); t=p.with_suffix('.download.tmp'); t.write_bytes(data); t.replace(p)" "%ENTRY_FILE%"
if errorlevel 1 goto :update_failed

echo Запускаю сбор Профи сейчас.
echo Сборщик закроет прежние окна Chrome. Во время сбора не переключайте окна.
echo Этот запуск не назначает выключение компьютера.
echo.
"%PYTHON_EXE%" -u "%ENTRY_FILE%" -a
set "RUN_EXIT=%ERRORLEVEL%"
popd
echo.
if not "%RUN_EXIT%"=="0" goto :run_failed
echo [OK] Сбор Профи завершён.
goto :finish

:run_failed
echo [ERROR] Сбор завершился с кодом %RUN_EXIT%.
echo Скопируйте текст ошибки из этого окна.
goto :finish

:python_missing
echo [ERROR] Не найден Python:
echo "%PYTHON_EXE%"
goto :finish

:update_failed
popd
echo.
echo [ERROR] Не удалось обновить запускатель. Сбор не запущен.
echo Скопируйте текст ошибки выше.
goto :finish

:folder_error
echo [ERROR] Не удалось открыть папку программы:
echo "%APP_DIR%"

:finish
echo.
pause
endlocal & exit /b %RUN_EXIT%
