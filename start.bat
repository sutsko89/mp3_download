@echo off
title MP3 Downloader
cd /d "%~dp0"

echo Запуск MP3 Downloader...
start http://localhost:8501
streamlit run app.py

pause