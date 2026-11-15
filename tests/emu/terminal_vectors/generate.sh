#!/bin/sh
# Regenerates the golden vectors in this directory from the real protocol-8 sender and decoder
# (gen_vectors.cpp says which). Needs a C++17 compiler, gzip and python3, and checkouts of:
#
#   NSPANEL_TERMINAL  nspanel-terminal (default ~/nspanel/nspanel-terminal): the display-side
#                     decoder (firmware/src), and the sender unless NEXTION_TERMINAL is set
#   NEXTION_TERMINAL  optional: the NSPanel firmware's lib/NextionTerminal, to take the sender from
#                     there instead (the firmware's copy of the same StreamEngine)
#
#   tests/emu/terminal_vectors/generate.sh
#
# Writes <case>.stream, <case>.fb.gz and manifest.json. With both checkouts, it also checks that the
# two senders produce identical streams.
set -e
here=$(cd "$(dirname "$0")" && pwd)
nt="${NSPANEL_TERMINAL:-$HOME/nspanel/nspanel-terminal}"
build="${TMPDIR:-/tmp}/nextion-tools-terminal-vectors"
rm -rf "$build"
mkdir -p "$build/out"
[ -f "$nt/firmware/src/stream_protocol.h" ] || { echo "no nspanel-terminal at $nt; set NSPANEL_TERMINAL" >&2; exit 2; }

# The decoder is C; -fpermissive for its casts of ROM addresses to function pointers.
cxx="${CXX:-c++} -std=c++17 -O1 -w -fpermissive -I$nt/firmware/src"
upstream="-I$nt/components/nspanel_terminal"
for profile in us eu; do
  define=""
  [ "$profile" = us ] && define="-DPROFILE_US"
  $cxx $define $upstream "$here/gen_vectors.cpp" -o "$build/gen_$profile"
  if [ -n "$NEXTION_TERMINAL" ]; then
    $cxx $define -DNT_FIRMWARE -I"$NEXTION_TERMINAL" "$here/gen_vectors.cpp" -o "$build/gen_fw_$profile"
  fi
done

: > "$build/manifest.jsonl"
for profile in us eu; do
  "$build/gen_$profile" "$build/out" >> "$build/manifest.jsonl"
done
if [ -n "$NEXTION_TERMINAL" ]; then
  mkdir -p "$build/fw"
  for profile in us eu; do
    "$build/gen_fw_$profile" "$build/fw" > /dev/null
  done
  for f in "$build/out"/*.stream "$build/out"/*.fb; do
    cmp "$f" "$build/fw/$(basename "$f")" || { echo "the firmware's sender differs: $(basename "$f")" >&2; exit 1; }
  done
  echo "the firmware's sender ($NEXTION_TERMINAL) produces the same streams"
fi

rm -f "$here"/*.stream "$here"/*.fb.gz
cp "$build/out"/*.stream "$here/"
for f in "$build/out"/*.fb; do
  gzip -9n < "$f" > "$here/$(basename "$f").gz"
done
python3 - "$build/manifest.jsonl" "$here/manifest.json" "$nt" <<'PY'
import hashlib, json, subprocess, sys
src, dst, nt = sys.argv[1:]
cases = [json.loads(line) for line in open(src)]
try:
    commit = subprocess.check_output(["git", "-C", nt, "rev-parse", "--short", "HEAD"], text=True).strip()
except Exception:
    commit = "unknown"
out = {"generator": "tests/emu/terminal_vectors/generate.sh", "nspanel_terminal": commit,
       "report_fields": ["status", "consumed", "seq", "rectangles"], "cases": cases}
with open(dst, "w") as f:
    json.dump(out, f, indent=1)
    f.write("\n")
PY
ls -l "$here"
