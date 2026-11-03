#!/usr/bin/env bash
# A sandbox home for ariadne, so Organize, Move and Undo can run for real without touching your files.
# The five roots are mirrored with hardlinks: renames, moves, unlinks and trash only touch the
# sandbox's own directory entries. NEVER write into a file's content in the sandbox: it is your file.
# Only HOME is boxed: the app's sidebar still lists the real drives, and a click there acts on them.
#   make    build ~/.cache/ariadne-sandbox/home (roots, index with paths rewritten, state, key)
#   up      start the sandbox daemon (unit ariadne-sandbox, port 8299) and check it is boxed in
#   app     start a headless sway, a virtual pointer and the file manager on the sandbox's Downloads
#   run CMD run a command inside that sway (grim, wtype, swaymsg); point with: echo "move X Y" > $VP
#   down    stop every ariadne-sandbox unit        reset   down, then delete the sandbox
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(dirname "$here")
real=/home/snek
box=$real/.cache/ariadne-sandbox
SB=$box/home
data=$SB/.local/share/ariadne
port=8299
VP=$XDG_RUNTIME_DIR/ariadne-sandbox-vp
display_file=$XDG_RUNTIME_DIR/ariadne-sandbox-display
roots=(Documents Downloads Pictures Desktop Videos)
skip=(Documents/Claude Documents/Codex Pictures/h)  # config.SKIP_PATHS

# What every sandbox process sees: a home, XDG dirs and a port that are all the sandbox's.
boxed=(-E HOME="$SB" -E XDG_DATA_HOME="$SB/.local/share" -E XDG_CONFIG_HOME="$SB/.config"
       -E XDG_CACHE_HOME="$SB/.cache" -E XDG_STATE_HOME="$SB/.local/state" -E ARIADNE_PORT=$port
       -E PATH="$SB/.local/bin:/usr/local/bin:/usr/bin")

say() { printf '%s\n' "$*" >&2; }
alive() { curl -sf -m 2 "127.0.0.1:$port/activity" > /dev/null; }

make_box() {
  [ -e "$SB" ] && { say "sandbox: $SB exists; run reset first"; exit 1; }
  mkdir -p "$SB"
  local t0=$SECONDS
  # cp carries on past a file it may not link (fs.protected_hardlinks refuses files you do not own)
  for r in "${roots[@]}"; do if [ -d "$real/$r" ]; then cp -al "$real/$r" "$SB/$r" 2>> "$box/make-errors.log" || true; fi; done
  if [ -s "$box/make-errors.log" ]; then say "not linked ($(wc -l < "$box/make-errors.log")):"; cat "$box/make-errors.log" >&2; fi
  for s in "${skip[@]}"; do rm -rf "${SB:?}/$s"; done  # unlinks only
  say "roots: $(find "$SB" -type f | wc -l) files hardlinked in $((SECONDS - t0))s"

  mkdir -p "$data" "$SB/.config/typesafe" "$SB/.config/yorha" "$SB/.local/bin" "$SB/.local/share/Trash/files" "$SB/.local/share/Trash/info"
  # A consistent copy of the live WAL database, then every stored path moved into the sandbox.
  sqlite3 "file:$real/.local/share/ariadne/index.db?mode=ro" ".backup '$data/index.db'"
  sqlite3 "$data/index.db" "update files set path = '$SB/' || substr(path, 12) where path like '$real/%';"
  say "index: $(sqlite3 "$data/index.db" "select count(*) from files where path like '$SB/%'") of" \
      "$(sqlite3 "$data/index.db" "select count(*) from files") paths now in the sandbox"
  for f in moves.jsonl kept.json tidy.json rejected.json; do
    if [ -f "$real/.local/share/ariadne/$f" ]; then sed "s#$real/#$SB/#g" "$real/.local/share/ariadne/$f" > "$data/$f"; fi
  done
  install -m 600 "$real/.config/typesafe/api_key" "$SB/.config/typesafe/api_key"
  if [ -d "$real/.config/ariadne" ]; then cp -r "$real/.config/ariadne" "$SB/.config/ariadne"; fi
  cp "$real/.config/yorha/palette.toml" "$SB/.config/yorha/"
  cp "$real/.config/user-dirs.dirs" "$SB/.config/"  # "$HOME/Downloads" and so on: the sidebar
  # The file manager runs `ariadne up` when the daemon does not answer; that must never reach the real unit.
  printf '#!/bin/sh\necho "sandbox: start the daemon with demo/sandbox.sh up" >&2\nexit 1\n' > "$SB/.local/bin/ariadne"
  chmod +x "$SB/.local/bin/ariadne"
  say "made in $((SECONDS - t0))s"
  prove
}

# A throwaway file hardlinked in, moved and removed inside the sandbox; the original must not notice.
prove() {
  local orig=$box/proof-orig.txt
  echo "sandbox proof $(date +%s)" > "$orig"
  local before; before=$(stat -c '%h %s %Y' "$orig")
  ln "$orig" "$SB/Downloads/.sandbox-proof.txt"
  mv "$SB/Downloads/.sandbox-proof.txt" "$SB/Documents/.sandbox-proof-moved.txt"
  [ "$(stat -c '%h' "$orig")" = 2 ] || { say "proof: link count is not 2"; exit 1; }
  rm "$SB/Documents/.sandbox-proof-moved.txt"
  local after; after=$(stat -c '%h %s %Y' "$orig")
  [ "$before" = "$after" ] && grep -q "sandbox proof" "$orig" || { say "proof FAILED: $before vs $after"; exit 1; }
  rm "$orig"
  say "proof: hardlinked, moved and unlinked in the sandbox; the original kept its content, size and mtime"
}

up() {
  alive && { say "sandbox: a daemon already answers on $port"; exit 1; }
  # The model runs on the CPU: a second copy on the card pushed it to the real daemon's VRAM guard,
  # which stopped the real daemon (2026-11-02). The copied index needs no re-embedding anyway.
  systemd-run --user --collect --unit=ariadne-sandbox --working-directory="$repo" "${boxed[@]}" \
    -E HF_HUB_OFFLINE=1 -E ARIADNE_BACKLOG=0 -E HIP_VISIBLE_DEVICES=-1 -E CUDA_VISIBLE_DEVICES=-1 \
    -p StandardOutput=append:"$data/daemon.log" -p StandardError=append:"$data/daemon.log" \
    "$repo/.venv/bin/python3" -m ariadne.cli serve
  for _ in $(seq 180); do alive && break; sleep 1; done
  alive || { say "sandbox: daemon did not answer; see $data/daemon.log"; exit 1; }
  check
}

# The daemon must hold the sandbox's home, database and roots, and nothing of yours.
check() {
  local pid; pid=$(systemctl --user show ariadne-sandbox -p MainPID --value)
  [ "$pid" != 0 ] || { say "check: ariadne-sandbox is not running"; exit 1; }
  tr '\0' '\n' < "/proc/$pid/environ" | grep -E '^(HOME|XDG_[A-Z_]+|ARIADNE_PORT)=' >&2
  [ "$(tr '\0' '\n' < "/proc/$pid/environ" | sed -n 's/^HOME=//p')" = "$SB" ] || { say "check: HOME is not the sandbox"; exit 1; }
  local dbs; dbs=$(ls -l "/proc/$pid/fd" | grep -o '/[^ ]*index.db[^ ]*' | sort -u)
  say "open databases: $dbs"
  grep -qv "^$data/" <<< "$dbs" && { say "check: an index outside the sandbox is open"; exit 1; }
  local hits; hits=$(curl -sf "127.0.0.1:$port/search?q=invoice&n=20" | python3 -c \
    'import json,sys; print("\n".join(h["path"] for h in json.load(sys.stdin)["hits"]))')
  grep -qv "^$SB/" <<< "$hits" && { say "check: a search hit lies outside the sandbox"; exit 1; }
  say "check: HOME, database and all $(wc -l <<< "$hits") search hits are inside $SB"
}

stage_run() { WAYLAND_DISPLAY=$(cat "$display_file") SWAYSOCK="$XDG_RUNTIME_DIR/ariadne-sandbox-stage.sock" "$@"; }

app() {
  alive || { say "sandbox: no daemon on $port; run up first"; exit 1; }
  if ! systemctl --user -q is-active ariadne-sandbox-stage; then
    local before after
    before=$(ls "$XDG_RUNTIME_DIR" | grep -E '^wayland-[0-9]+$' | sort || true)
    systemd-run --user --collect --unit=ariadne-sandbox-stage -E WLR_BACKENDS=headless -E WLR_HEADLESS_OUTPUTS=1 \
      -E WLR_LIBINPUT_NO_DEVICES=1 -E XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
      -E SWAYSOCK="$XDG_RUNTIME_DIR/ariadne-sandbox-stage.sock" sway -c "$here/sway.conf"
    for _ in $(seq 50); do
      after=$(ls "$XDG_RUNTIME_DIR" | grep -E '^wayland-[0-9]+$' | sort || true)
      comm -13 <(echo "$before") <(echo "$after") | head -1 > "$display_file"
      [ -s "$display_file" ] && break
      sleep 0.2
    done
    [ -s "$display_file" ] || { say "sandbox: sway did not come up"; exit 1; }
  fi
  local wl; wl=$(cat "$display_file")
  [ -p "$VP" ] || mkfifo "$VP"
  systemctl --user -q is-active ariadne-sandbox-vp || systemd-run --user --collect --unit=ariadne-sandbox-vp \
    -E WAYLAND_DISPLAY="$wl" -E XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" \
    bash -c "tail -f '$VP' | '$here/vpointer/vpointer' 1600 1000"
  systemctl --user stop ariadne-sandbox-fm 2> /dev/null || true
  systemd-run --user --collect --unit=ariadne-sandbox-fm --working-directory="$SB" "${boxed[@]}" \
    -E WAYLAND_DISPLAY="$wl" -E XDG_RUNTIME_DIR="$XDG_RUNTIME_DIR" -E RUST_LOG=warn \
    "$repo/cosmic-files/target/debug/cosmic-files" --no-daemon "${1:-$SB/Downloads}"
  sleep 4
  stage_run swaymsg fullscreen enable > /dev/null
  say "app on $wl; pointer: echo 'move X Y' > $VP; keys: $0 run wtype ...; shot: $0 run grim FILE"
}

down() {
  for u in ariadne-sandbox-fm ariadne-sandbox-vp ariadne-sandbox-stage ariadne-sandbox; do
    systemctl --user stop "$u" 2> /dev/null || true
  done
  rm -f "$VP" "$display_file"
}

case "${1:-}" in
  make) make_box ;;
  up) up ;;
  check) check ;;
  app) shift; app "$@" ;;
  run) shift; stage_run "$@" ;;
  down) down ;;
  reset) down; rm -rf "${box:?}"; say "sandbox deleted (unlinked; your files are untouched)" ;;
  *) sed -n 2,9p "$0" >&2; exit 2 ;;
esac
