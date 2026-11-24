#!/bin/sh
# Run from any directory. Keep this terminal open while using SCOPE.
cd -- "$(dirname -- "$0")" || exit 1
exec python3 app.py start
