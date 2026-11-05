@echo off
rem Double-click to open the network sync dashboard.
cd /d "%~dp0.."
start "" pythonw -m network_sync.ui
