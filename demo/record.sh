#!/usr/bin/env bash
# Records the demo inside the headless stage (./stage up first, daemon up). Two takes: the file
# manager, then the CLI and drop zone in a terminal. Each step logs "seconds<TAB>caption" so
# captions can be burned in afterwards (./caption.sh).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
out="$here/out"
fifo="$XDG_RUNTIME_DIR/ariadne-vp"
mkdir -p "$out"
stage() { "$here/stage" run "$@"; }

t0=0
mark() { printf '%s\t%s\n' "$(echo "$(date +%s.%N) - $t0" | bc)" "$1" >> "$captions"; }
type_slow() { stage wtype -s 400 -d 45 "$1"; }  # the pause lets the app take the new keymap
key() { stage wtype "$@"; }
pointer() { echo "$1" > "$fifo"; }
clear_search() {  # ctrl+a in the box selects files, not text: use the box's clear button
    pointer "move 1447 23"
    sleep 0.4
    pointer "click left"
    sleep 0.6
    key -M ctrl f -m ctrl
}

record() {
    local name=$1
    captions="$out/$name.captions"
    : > "$captions"
    stage wf-recorder -y -o HEADLESS-1 -c libx264 -p preset=veryfast -p crf=18 -f "$out/$name.raw.mp4" \
        > "$out/$name.recorder.log" 2>&1 &
    rec=$!
    sleep 1
    t0=$(date +%s.%N)
}
stop() {
    sleep 1.5
    kill -INT "$rec"
    wait "$rec" || true
}

# --- take 1: the file manager --------------------------------------------------------
for p in $(pgrep -x cosmic-files); do kill "$p"; done
[ -p "$fifo" ] || mkfifo "$fifo"
for p in $(pgrep -x vpointer); do kill "$p"; done
stage setsid -f bash -c "tail -f '$fifo' | '$here/vpointer/vpointer' 1600 1000" > "$out/vpointer.log" 2>&1
stage setsid -f bash -c "exec '$here/../cosmic-files/target/release/cosmic-files' ~/Documents" > "$out/cosmic-files.log" 2>&1
sleep 3
pointer "move 1590 990"

record fm
mark "COSMIC Files, with ariadne behind its search box"
sleep 2.5
key -M ctrl f -m ctrl
mark "Search by meaning: the PDFs are just called 'Order Details'"
type_slow "receipt or order confirmation from amazon"
sleep 3.5
clear_search
mark "Pictures are in the same space as text"
type_slow "a normal map texture, purple and blue"
sleep 3.5
clear_search
mark "Text inside PDFs and notes counts too"
type_slow "level 2 paladin character sheet"
sleep 3.5
mark "Right click, Find similar files"
pointer "move 560 214"  # Edric_Paladin_L2.pdf, second row
sleep 0.8
pointer "click right"
sleep 1.5
pointer "move 640 340"  # the menu opens at the cursor; Find similar files is 126 px down
sleep 1
pointer "click left"
mark "Its copy in Downloads, the character's notes, the class rules"
sleep 4.5
stop
