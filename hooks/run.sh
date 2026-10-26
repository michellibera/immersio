#!/bin/sh
# Runs server/hook.py with the first Python >= 3.8 found. Never fails the session:
# without Python it prints a readable message (SessionStart) and exits 0.
# No external commands (dirname etc.), so it works with an empty PATH too.
case $0 in */*) dir=${0%/*} ;; *) dir=. ;; esac
# "python" first: where it is missing the shell fails instantly, while the Windows Store
# "python3" alias costs ~0.2 s just to print an install hint. The probe rejects it and Python 2.
for py in python python3 "py -3"; do
  if $py -c 'import sys; sys.exit(sys.version_info < (3, 8))' </dev/null >/dev/null 2>&1; then
    exec $py "$dir/../server/hook.py" "$@"
  fi
done
if [ "$1" = start ]; then
  echo '{"systemMessage": "immersio: Python 3.8+ not found (tried python, python3, py -3); viz server not started."}'
fi
exit 0
