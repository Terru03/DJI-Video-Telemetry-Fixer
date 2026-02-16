@echo off
setlocal

:: --- CONFIGURATION ---
:: Set the path to your Python installation (or use 'python' if in PATH)
set PYTHON_EXE=python

:: Set the path to your source folder (containing Original MP4s and SRT files)
set SOURCE_DIR="C:\Path\To\Your\Source\Folder"

:: Set the path to your export folder (containing Edited/Final MP4s)
set EXPORT_DIR="C:\Path\To\Your\Export\Folder"

:: Options:
:: --threads N        : Number of parallel threads (Default: 4)
:: --delete-source    : Moves original source MP4 to Recycle Bin after successful processing (Space Saver)
:: --force            : Re-process videos even if they already have metadata
set OPTIONS=--threads 4 

:: ---------------------

echo Starting DJI Metadata Injection...
echo Source: %SOURCE_DIR%
echo Export: %EXPORT_DIR%
echo Options: %OPTIONS%
echo.

"%PYTHON_EXE%" "inject_dji_metadata.py" %SOURCE_DIR% %EXPORT_DIR% %OPTIONS%

echo.
echo ------------------------------------------------------------------
echo Script finished. Review output above for details.
pause
