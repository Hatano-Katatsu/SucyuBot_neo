@echo off
setlocal
echo Starting SucyuBot...
echo Config: data\config.yml
echo State:  data\state.json
echo Web:    http://127.0.0.1:8787
echo.
python -m telegram_comfyui_selfie %*
if errorlevel 1 (
  echo.
  echo SucyuBot exited with code %ERRORLEVEL%.
  echo Check logs/ for details.
  pause
)
