@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

if exist "%~dp0start_config.bat" call "%~dp0start_config.bat"

if defined NEWS_PYTHON goto UseConfiguredPython

where py.exe >nul 2>nul
if not errorlevel 1 (
  py -3 -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
  if not errorlevel 1 (
    py -3 "%~dp0launcher.py"
    set "STARTUP_EXIT=!ERRORLEVEL!"
    goto Finish
  )
)

where python.exe >nul 2>nul
if not errorlevel 1 (
  python -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
  if not errorlevel 1 (
    python "%~dp0launcher.py"
    set "STARTUP_EXIT=!ERRORLEVEL!"
    goto Finish
  )
)

call :InstallPython
if errorlevel 1 goto Failed
"%PYTHON_EXE%" "%~dp0launcher.py"
set "STARTUP_EXIT=!ERRORLEVEL!"
goto Finish

:UseConfiguredPython
if not exist "%NEWS_PYTHON%" (
  echo NEWS_PYTHON does not point to an existing Python executable.
  goto Failed
)
"%NEWS_PYTHON%" -c "import sys; raise SystemExit(sys.version_info < (3, 10))" >nul 2>nul
if errorlevel 1 (
  echo NEWS_PYTHON must point to Python 3.10 or newer.
  goto Failed
)
"%NEWS_PYTHON%" "%~dp0launcher.py"
set "STARTUP_EXIT=!ERRORLEVEL!"
goto Finish

:InstallPython
set "PYTHON_RELEASE=3.13.16"
set "INSTALL_SUFFIX="
set "EXPECTED_HASH="
set "WINDOWS_ARCH=%PROCESSOR_ARCHITECTURE%"
if defined PROCESSOR_ARCHITEW6432 set "WINDOWS_ARCH=%PROCESSOR_ARCHITEW6432%"
if /i "%WINDOWS_ARCH%"=="AMD64" (
  set "INSTALL_SUFFIX=-amd64"
  set "EXPECTED_HASH=fb4f9f5d438b2396da0086dc70b935c530cb578e37adc6d354f7ad2037fee83b"
) else if /i "%WINDOWS_ARCH%"=="ARM64" (
  set "INSTALL_SUFFIX=-arm64"
  set "EXPECTED_HASH=696e2226062c6ec3622c13f143d28336a856968518e49cb4f4c62513215a4f5d"
) else if /i "%WINDOWS_ARCH%"=="X86" (
  set "EXPECTED_HASH=394ca84150ea428db952cb617c1163c49fbc30e47ec53048c26e6ae44265df07"
) else (
  echo Automatic Python installation is unavailable for %WINDOWS_ARCH%.
  exit /b 1
)

set "INSTALLER=%TEMP%\NewsHtmlReader-python-%PYTHON_RELEASE%-%RANDOM%.exe"
set "INSTALLER_URL=https://www.python.org/ftp/python/%PYTHON_RELEASE%/python-%PYTHON_RELEASE%%INSTALL_SUFFIX%.exe"
where curl.exe >nul 2>nul
if errorlevel 1 (
  echo curl.exe is required to download Python. Install Python 3.10 or newer and retry.
  exit /b 1
)
echo Python 3.10 or newer was not found. Downloading the verified Python %PYTHON_RELEASE% installer...
curl.exe --fail --location --silent --show-error "%INSTALLER_URL%" --output "%INSTALLER%"
if errorlevel 1 (
  echo Python download failed.
  exit /b 1
)
certutil.exe -hashfile "%INSTALLER%" SHA256 | findstr.exe /i /c:"%EXPECTED_HASH%" >nul
if errorlevel 1 (
  del /q "%INSTALLER%" >nul 2>nul
  echo Python installer checksum verification failed. Installation was stopped.
  exit /b 1
)

set "PYTHON_HOME=%LOCALAPPDATA%\Programs\Python\Python313"
"%INSTALLER%" /quiet InstallAllUsers=0 "TargetDir=%PYTHON_HOME%" Include_launcher=0 Include_pip=1 Include_tcltk=1 Include_test=0 PrependPath=0 Shortcuts=0
set "INSTALL_EXIT=%ERRORLEVEL%"
del /q "%INSTALLER%" >nul 2>nul
if not "%INSTALL_EXIT%"=="0" (
  echo Python installation failed with code %INSTALL_EXIT%.
  exit /b 1
)
set "PYTHON_EXE=%PYTHON_HOME%\python.exe"
if not exist "%PYTHON_EXE%" (
  echo Python installation finished but python.exe was not found.
  exit /b 1
)
exit /b 0

:Finish
if not "%STARTUP_EXIT%"=="0" goto Failed
exit /b 0

:Failed
echo.
echo Startup did not complete. See the message above.
pause
exit /b 1
