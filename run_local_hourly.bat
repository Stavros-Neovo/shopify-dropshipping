@echo off
REM ==========================================================================
REM Windows Task-Scheduler-Wrapper
REM Lege im Windows-Aufgabenplaner eine Aufgabe an, die diese .bat stündlich
REM ausführt. Pfad in der Aufgabe = vollständiger Pfad zu dieser Datei.
REM ==========================================================================
cd /d %~dp0
python sync.py --live >> logs\cron.log 2>&1
