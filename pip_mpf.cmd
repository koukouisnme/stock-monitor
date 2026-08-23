@echo off
py312\python.exe -m pip install --no-warn-script-location mplfinance matplotlib
echo PIP_EXIT=%ERRORLEVEL%
