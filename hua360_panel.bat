@echo off
rem Huawei 360 camera: control panel + OBS Virtual Camera. Close the panel to quit.
rem Extra options can be added, e.g. hua360_panel.bat --window --body blur
cd /d "%~dp0"
python hua360_view.py --res 9 --size 1280x720 --vcam --reset %*
if errorlevel 1 pause
