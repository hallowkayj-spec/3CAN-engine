#!/bin/sh

session_orientation=false
if [ "${1-}" = "--session-orientation" ]; then
    session_orientation=true
fi

current=$PWD
boundary=
while :; do
    if [ -e "$current/.git" ]; then
        boundary=$current
        break
    fi
    [ "$current" = "/" ] && break
    current=${current%/*}
    [ -n "$current" ] || current=/
done

if [ "$session_orientation" = false ]; then
    state_path=$boundary/.codex/runtimehook/state.json
    if [ -z "$boundary" ] || { [ ! -e "$state_path" ] && [ ! -L "$state_path" ]; }; then
        while IFS= read -r _line; do :; done
        exit 0
    fi
fi

untrusted_root=${boundary:-$PWD}
python=
old_ifs=$IFS
IFS=:
for raw_directory in ${PATH-}; do
    [ -n "$raw_directory" ] || continue
    case "$raw_directory" in
        /*) ;;
        *) continue ;;
    esac
    physical_directory=$(CDPATH= cd -P "$raw_directory" 2>/dev/null && pwd -P) || continue
    candidate=$physical_directory/python3
    [ -f "$candidate" ] && [ -x "$candidate" ] || continue
    case "$candidate" in
        "$untrusted_root"|"$untrusted_root"/*) continue ;;
    esac
    resolved=$(
        "$candidate" -c 'import pathlib, sys; print(pathlib.Path(sys.executable).resolve() if sys.version_info.major == 3 else "")' </dev/null 2>/dev/null
    ) || continue
    case "$resolved" in
        /*) ;;
        *) continue ;;
    esac
    case "$resolved" in
        "$untrusted_root"|"$untrusted_root"/*) continue ;;
    esac
    [ -f "$resolved" ] && [ -x "$resolved" ] || continue
    python=$resolved
    break
done
IFS=$old_ifs

if [ -z "$python" ]; then
    printf '%s\n' '{"systemMessage":"RuntimeHook semantic context is UNAVAILABLE: Python 3 is not available on trusted PATH entries. Safe local work may continue; independent project evidence gates remain authoritative."}'
    exit 0
fi

controller=$PLUGIN_ROOT/skills/3can-runtimehook/scripts/3can_runtimehook.py
if [ ! -f "$controller" ]; then
    printf '%s\n' '{"systemMessage":"RuntimeHook semantic context is UNAVAILABLE: the bundled controller is missing. Safe local work may continue; independent project evidence gates remain authoritative."}'
    exit 0
fi

if [ "$session_orientation" = true ]; then
    "$python" "$controller" hook --session-orientation
else
    "$python" "$controller" hook
fi
exit_code=$?
if [ "$exit_code" -ne 0 ]; then
    printf '%s\n' '{"systemMessage":"RuntimeHook semantic context is UNAVAILABLE: Python 3 could not execute the bundled controller. Safe local work may continue; independent project evidence gates remain authoritative."}'
fi
exit 0
