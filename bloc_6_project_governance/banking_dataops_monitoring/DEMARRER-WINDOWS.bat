@echo off
REM ---------------------------------------------------------------------------
REM  Double-clickable launcher.
REM
REM  WHY THIS FILE EXISTS
REM    PowerShell refuses to run unsigned .ps1 files by default, and the error
REM    it shows is opaque to anyone who has not seen it. This wrapper bypasses
REM    the policy for this single invocation only - it changes nothing on the
REM    machine - and passes the task through.
REM
REM  USAGE
REM    Double-click for the guided first run, or from a terminal:
REM      DEMARRER-WINDOWS.bat smoke
REM ---------------------------------------------------------------------------

cd /d "%~dp0"

if "%~1"=="" (
    echo.
    echo   ================================================================
    echo    banking-dataops-monitoring  -  premier demarrage
    echo   ================================================================
    echo.
    echo   Quatre etapes, dans cet ordre :
    echo.
    echo     1. setup    detecte les ports libres et construit l'image
    echo     2. start    demarre tout et attend que le tableau de bord reponde
    echo     3. train    entraine le modele
    echo     4. smoke    verifie tout  -  doit afficher DEMO READY
    echo.
    echo   Lancement de l'etape 1...
    echo.
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" setup
    echo.
    echo   Etape suivante :  DEMARRER-WINDOWS.bat start
    echo.
    pause
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
)
