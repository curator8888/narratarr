#!/usr/bin/env bash
#
#  Narratarr installer: https://github.com/narratarr/narratarr
#
#    curl -fsSL https://raw.githubusercontent.com/narratarr/narratarr/main/install.sh | bash
#
#  Walks you through installing Narratarr with Docker or natively (via uv),
#  optionally starting it when you log in. Run with --help for options.
#
#  Written for the bash 3.2 that ships with macOS, so no bash-4 features.

set -euo pipefail

REPO="narratarr/narratarr"
IMAGE="${NARRATARR_IMAGE:-ghcr.io/narratarr/narratarr:latest}"
PACKAGE="${NARRATARR_PACKAGE:-}"
DATA_DIR="${NARRATARR_DATA:-$HOME/.narratarr}"
LABEL="com.narratarr"

# ---- options -----------------------------------------------------------------

METHOD="" LIBRARY="" PORT="" AUTOSTART="" ASSUME_YES=0 UNINSTALL=0 DOCKER_DIR="" NEEDS_ACCOUNT=0

usage() {
  cat <<EOF
Narratarr installer

Usage: install.sh [options]

  --docker            Install with Docker (docker compose)
  --native            Install natively with uv (macOS or Linux)
  --library PATH      Where finished audiobooks go (default: ~/Audiobooks)
  --port N            Web UI port (default: 8484)
  --dir PATH          Docker only: folder for docker-compose.yml + config (default: ~/narratarr)
  --autostart         Native only: start Narratarr when you log in
  --no-autostart      Native only: don't
  -y, --yes           Accept defaults for anything not given; no questions
  --uninstall         Remove Narratarr (your audiobooks and settings are kept)
  -h, --help          Show this help

Run with no options for the guided installer.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --docker) METHOD=docker ;;
    --native) METHOD=native ;;
    --library) LIBRARY="${2:?--library needs a path}"; shift ;;
    --port) PORT="${2:?--port needs a number}"; shift ;;
    --dir) DOCKER_DIR="${2:?--dir needs a path}"; shift ;;
    --autostart) AUTOSTART=yes ;;
    --no-autostart) AUTOSTART=no ;;
    -y|--yes) ASSUME_YES=1 ;;
    --uninstall) UNINSTALL=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

# ---- terminal ------------------------------------------------------------------

TTY=/dev/tty
INTERACTIVE=0
if [ "$ASSUME_YES" = 0 ] && { : <"$TTY"; } 2>/dev/null; then INTERACTIVE=1; fi

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != dumb ]; then
  B=$'\033[1m' D=$'\033[2m' R=$'\033[0m'
  CORAL=$'\033[38;2;236;111;85m' GOLD=$'\033[38;2;248;198;110m' PEACH=$'\033[38;2;242;150;98m'
  GREEN=$'\033[38;2;126;211;145m' RED=$'\033[38;2;240;98;98m' MUTED=$'\033[38;2;150;140;150m'
  FANCY=1
else
  B="" D="" R="" CORAL="" GOLD="" PEACH="" GREEN="" RED="" MUTED="" FANCY=0
fi

LOG="$(mktemp "${TMPDIR:-/tmp}/narratarr-install.XXXXXX")"

cleanup() {
  [ "$FANCY" = 1 ] && printf '\033[?25h' || true      # show the cursor again
  [ "$INTERACTIVE" = 1 ] && stty sane <"$TTY" 2>/dev/null || true
}
trap cleanup EXIT
trap 'echo; exit 130' INT

say()  { printf '%s\n' "$*"; }
info() { printf '  %s%s%s\n' "$MUTED" "$*" "$R"; }
ok()   { printf '  %s✔%s %s\n' "$GREEN" "$R" "$*"; }
warn() { printf '  %s!%s %s\n' "$GOLD" "$R" "$*"; }
die()  { printf '\n  %s✖ %s%s\n\n' "$RED" "$*" "$R" >&2; exit 1; }

banner() {
  say ""
  printf '   %s▁▃%s▅▇%s█▇%s▅▃▁%s   %s%sN A R R A T A R R%s\n' "$GOLD" "$PEACH" "$CORAL" "$PEACH" "$R" "$B" "$CORAL" "$R"
  printf '   %s            your audiobooks: found, fetched, and playing%s\n' "$MUTED" "$R"
  say ""
}

STEP=0 STEPS=4
step() {
  STEP=$((STEP + 1))
  printf '\n  %s%s●%s %s%s%s  %sstep %s of %s%s\n\n' "$B" "$CORAL" "$R" "$B" "$*" "$R" "$MUTED" "$STEP" "$STEPS" "$R"
}

# menu "Title|description|why-disabled" ... -> sets CHOICE to the picked index (0-based)
menu() {
  local n=$# i key rest sel=0 opts=("$@") label desc off
  # Start on the first option that isn't disabled.
  for ((i = 0; i < n; i++)); do off="${opts[$i]##*|}"; [ -z "$off" ] && { sel=$i; break; }; done
  if [ "$INTERACTIVE" = 0 ]; then CHOICE=$sel; return; fi

  # Don't let keypresses echo between redraws, or the menu drifts down the screen.
  local saved
  saved="$(stty -g <"$TTY")"
  stty -echo -icanon <"$TTY"
  printf '\033[?25l'
  while true; do
    for ((i = 0; i < n; i++)); do
      IFS='|' read -r label desc off <<<"${opts[$i]}"
      if [ -n "$off" ]; then
        printf '\033[2K    %s○ %-14s %s%s\n' "$MUTED" "$label" "$off" "$R"
      elif [ "$i" = "$sel" ]; then
        printf '\033[2K  %s❯ ◉ %s%-14s%s %s\n' "$CORAL" "$B" "$label" "$R" "$desc"
      else
        printf '\033[2K    %s○%s %-14s %s%s%s\n' "$MUTED" "$R" "$label" "$MUTED" "$desc" "$R"
      fi
    done
    printf '\033[2K\n\033[2K    %s↑/↓ to move · enter to choose%s\n' "$D" "$R"

    IFS= read -rsn1 key <"$TTY" || key=""
    if [ "$key" = $'\033' ]; then
      IFS= read -rsn2 -t 1 rest <"$TTY" || rest=""
      key="$key$rest"
    fi
    case "$key" in
      $'\033[A'|k|K) i=$sel; while :; do i=$(((i - 1 + n) % n)); off="${opts[$i]##*|}"; [ -z "$off" ] && break; done; sel=$i ;;
      $'\033[B'|j|J) i=$sel; while :; do i=$(((i + 1) % n)); off="${opts[$i]##*|}"; [ -z "$off" ] && break; done; sel=$i ;;
      [1-9]) i=$((key - 1)); if [ "$i" -lt "$n" ] && [ -z "${opts[$i]##*|}" ]; then sel=$i; fi ;;
      "") break ;;
    esac
    printf '\033[%sA' $((n + 2))
  done
  # Collapse the menu down to the answer.
  printf '\033[%sA' $((n + 2))
  for ((i = 0; i < n + 2; i++)); do printf '\033[2K\n'; done
  printf '\033[%sA' $((n + 2))
  IFS='|' read -r label desc off <<<"${opts[$sel]}"
  printf '    %s✔%s %s%s%s\n' "$GREEN" "$R" "$B" "$label" "$R"
  printf '\033[?25h'
  stty "$saved" <"$TTY"
  CHOICE=$sel
}

# ask "Question" "default" -> ANSWER
ask() {
  local q="$1" def="${2:-}" reply=""
  if [ "$INTERACTIVE" = 0 ]; then ANSWER="$def"; return; fi
  printf '  %s?%s %s %s%s%s\n  %s›%s ' "$CORAL" "$R" "$q" "$MUTED" "${def:+($def)}" "$R" "$CORAL" "$R"
  IFS= read -r reply <"$TTY" || reply=""
  # Show what was accepted when they just pressed enter.
  [ -z "$reply" ] && [ -n "$def" ] && printf '\033[1A\033[2K  %s›%s %s\n' "$CORAL" "$R" "$def"
  ANSWER="${reply:-$def}"
}

# confirm "Question" y|n -> returns 0 for yes
confirm() {
  local q="$1" def="${2:-y}" reply="" hint="Y/n"
  [ "$def" = n ] && hint="y/N"
  if [ "$INTERACTIVE" = 0 ]; then [ "$def" = y ]; return; fi
  printf '  %s?%s %s %s(%s)%s ' "$CORAL" "$R" "$q" "$MUTED" "$hint" "$R"
  IFS= read -r reply <"$TTY" || reply=""
  reply="$(printf '%s' "${reply:-$def}" | tr '[:upper:]' '[:lower:]')"
  [ "$reply" = y ] || [ "$reply" = yes ]
}

# run "message" command... -> spinner while it runs; shows the log if it fails
run() {
  local msg="$1"; shift
  if [ "$FANCY" = 0 ]; then
    say "  … $msg"
    "$@" >>"$LOG" 2>&1 || { tail -n 25 "$LOG" >&2; die "$msg failed (full log: $LOG)"; }
    ok "$msg"; return
  fi
  "$@" >>"$LOG" 2>&1 &
  local pid=$! i=0
  local frames=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏)
  printf '\033[?25l'
  while kill -0 "$pid" 2>/dev/null; do
    printf '\r\033[2K  %s%s%s %s' "$CORAL" "${frames[i++ % 10]}" "$R" "$msg"
    sleep 0.08
  done
  printf '\033[?25h'
  if wait "$pid"; then
    printf '\r\033[2K'; ok "$msg"
  else
    printf '\r\033[2K  %s✖%s %s\n\n' "$RED" "$R" "$msg"
    tail -n 25 "$LOG" | sed 's/^/      /' >&2
    die "That step failed. Full log: $LOG"
  fi
}

pretty() { case "$1" in "$HOME"*) printf '~%s' "${1#"$HOME"}" ;; *) printf '%s' "$1" ;; esac; }
expand() { case "$1" in "~") printf '%s' "$HOME" ;; "~/"*) printf '%s/%s' "$HOME" "${1#\~/}" ;; *) printf '%s' "$1" ;; esac; }
port_busy() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }
# Busy with something other than Narratarr (an update replaces a running Narratarr, so that's fine).
port_taken() { port_busy "$1" && ! curl -fsS "http://127.0.0.1:$1/api/health" >/dev/null 2>&1; }
json_str() { printf '"%s"' "$(printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"; }

wait_healthy() {
  local i
  for i in $(seq 1 60); do
    curl -fsS "http://127.0.0.1:$1/api/health" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

open_browser() {
  local url="$1"
  [ "$INTERACTIVE" = 1 ] || return 0
  if command -v open >/dev/null 2>&1 && [ "$(uname)" = Darwin ]; then open "$url" >/dev/null 2>&1 || true
  elif [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && command -v xdg-open >/dev/null 2>&1; then xdg-open "$url" >/dev/null 2>&1 || true
  fi
}

vislen() { local s; s="$(printf '%s' "$1" | sed $'s/\033\\[[0-9;]*m//g')"; printf '%s' "${#s}"; }

box() {   # box "line" "line" ...
  local line w=0 n
  for line in "$@"; do n="$(vislen "$line")"; [ "$n" -gt "$w" ] && w=$n; done
  printf '\n  %s╭%s╮%s\n' "$CORAL" "$(printf '─%.0s' $(seq 1 $((w + 4))))" "$R"
  for line in "$@"; do
    n="$(vislen "$line")"
    printf '  %s│%s  %s%*s  %s│%s\n' "$CORAL" "$R" "$line" $((w - n)) "" "$CORAL" "$R"
  done
  printf '  %s╰%s╯%s\n\n' "$CORAL" "$(printf '─%.0s' $(seq 1 $((w + 4))))" "$R"
}

# ---- shared questions ----------------------------------------------------------

choose_library() {
  [ -n "$LIBRARY" ] || { ask "Where should finished audiobooks go?" "~/Audiobooks"; LIBRARY="$ANSWER"; }
  LIBRARY="$(expand "$LIBRARY")"
  mkdir -p "$LIBRARY" || die "Couldn't create $LIBRARY"
}

choose_port() {
  local def="${PORT:-8484}"
  [ -n "$PORT" ] || { ask "Which port should the web page use?" "$def"; PORT="$ANSWER"; }
  case "$PORT" in ''|*[!0-9]*) die "\"$PORT\" isn't a port number." ;; esac
}

# create_account narratarr-command... -> makes the admin account, unless there already is one
create_account() {
  if "$@" users any >/dev/null 2>&1; then ok "Your accounts are already set up"; return 0; fi
  if [ "$INTERACTIVE" = 0 ]; then
    NEEDS_ACCOUNT=1; info "You'll create the admin account the first time you open Narratarr."; return 0
  fi
  info "Everyone signs in to Narratarr. This first account is the admin: it runs settings,"
  info "downloads, and who else can sign in. Add family and friends as listeners later."
  say ""
  local name pw again
  while :; do
    ask "Username" "$(id -un 2>/dev/null || echo admin)"
    name="$ANSWER"
    case "$name" in
      [!A-Za-z0-9]*|*[!A-Za-z0-9._@-]*) warn "Use letters, numbers, dots, dashes, underscores or @, starting with a letter or number." ;;
      *) [ "${#name}" -le 32 ] && break; warn "Keep it to 32 characters." ;;
    esac
  done
  while :; do
    printf '  %s?%s Password %s(at least 8 characters; it won'"'"'t show)%s\n  %s›%s ' "$CORAL" "$R" "$MUTED" "$R" "$CORAL" "$R"
    IFS= read -rs pw <"$TTY" || pw=""; printf '\n'
    if [ "${#pw}" -lt 8 ]; then warn "That's shorter than 8 characters."; continue; fi
    printf '  %s?%s Once more %s›%s ' "$CORAL" "$R" "$CORAL" "$R"
    IFS= read -rs again <"$TTY" || again=""; printf '\n'
    [ "$pw" = "$again" ] && break
    warn "Those didn't match. Try again."
  done
  # The password goes in on stdin, so it never appears in the process list or shell history.
  if printf '%s\n' "$pw" | "$@" users add "$name" --role admin --password-stdin >>"$LOG" 2>&1; then
    ok "Created ${B}$name${R}, the admin account"
  else
    NEEDS_ACCOUNT=1
    warn "Couldn't create the account: $(tail -n 1 "$LOG")"
    info "You'll be asked to create it when you open Narratarr."
  fi
  pw="" again=""
}

first_visit_line() { [ "$NEEDS_ACCOUNT" = 1 ] && printf '%s' "First visit: create the admin account" || printf '%s' "Sign in:     with the account you just made"; }

# ---- native (uv) -----------------------------------------------------------------

ensure_uv() {
  if command -v uv >/dev/null 2>&1; then ok "uv is installed ($(uv --version 2>/dev/null | awk '{print $2}'))"; return; fi
  for p in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    [ -x "$p" ] && { export PATH="$(dirname "$p"):$PATH"; ok "uv is installed"; return; }
  done
  info "Narratarr installs with uv, a fast Python app installer from Astral (astral.sh/uv)."
  info "It brings its own Python, so nothing on your system changes."
  confirm "Install uv now?" y || die "Narratarr needs uv. Install it from https://docs.astral.sh/uv/ and run this again."
  run "Installing uv" sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || die "uv installed, but isn't on PATH yet. Open a new terminal and run this again."
}

seed_settings() {
  # Point a brand-new install at the chosen library. Existing settings are left alone.
  [ -f "$DATA_DIR/settings.json" ] && return 0
  mkdir -p "$DATA_DIR"
  printf '{\n  "library_dir": %s\n}\n' "$(json_str "$LIBRARY")" >"$DATA_DIR/settings.json"
  chmod 600 "$DATA_DIR/settings.json"
}

autostart_macos() {
  local plist="$HOME/Library/LaunchAgents/$LABEL.plist"
  mkdir -p "$HOME/Library/LaunchAgents" "$DATA_DIR"
  cat >"$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN</string>
    <string>--port</string><string>$PORT</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DATA_DIR/narratarr.log</string>
  <key>StandardErrorPath</key><string>$DATA_DIR/narratarr.log</string>
</dict>
</plist>
EOF
  local domain="gui/$(id -u)" i
  launchctl bootout "$domain/$LABEL" >/dev/null 2>&1 || true
  # bootout returns before launchd has finished stopping the old copy, and starting the
  # new one too soon fails with "Bootstrap failed: 5: Input/output error". Wait, then retry.
  for i in $(seq 1 20); do
    launchctl print "$domain/$LABEL" >/dev/null 2>&1 || break
    sleep 0.5
  done
  for i in 1 2 3 4 5; do
    launchctl bootstrap "$domain" "$plist" && return 0
    sleep 1
  done
  return 1
}

autostart_linux() {
  local unit="$HOME/.config/systemd/user/narratarr.service"
  mkdir -p "$(dirname "$unit")"
  cat >"$unit" <<EOF
[Unit]
Description=Narratarr audiobook manager
After=network-online.target

[Service]
ExecStart=$BIN --port $PORT
Restart=on-failure

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable --now narratarr.service
}

install_native() {
  STEPS=5
  step "Getting the tools"
  ensure_uv

  step "Your library"
  choose_library
  choose_port
  ok "Books will go to ${B}$(pretty "$LIBRARY")${R}"

  step "Installing Narratarr"
  local pkg="$PACKAGE"
  if [ -z "$pkg" ]; then
    if command -v git >/dev/null 2>&1 && git --version >/dev/null 2>&1; then pkg="git+https://github.com/$REPO"
    else pkg="https://github.com/$REPO/archive/refs/heads/main.zip"; fi
  fi
  run "Downloading and installing Narratarr" \
    uv tool install --force --reinstall-package narratarr --refresh-package narratarr "$pkg"
  BIN="$(uv tool dir --bin 2>/dev/null)/narratarr"
  [ -x "$BIN" ] || BIN="$(command -v narratarr || true)"
  [ -x "$BIN" ] || die "Installed, but couldn't find the narratarr command."
  seed_settings
  ok "Installed $("$BIN" --version 2>/dev/null)"

  step "Your account"
  create_account env NARRATARR_DATA="$DATA_DIR" "$BIN"

  step "Starting it"
  local can_autostart=""
  case "$(uname)" in
    Darwin) can_autostart=launchd ;;
    Linux) command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1 && can_autostart=systemd ;;
  esac
  if [ -z "$AUTOSTART" ]; then
    if [ -n "$can_autostart" ]; then
      menu "Automatically|Starts now and whenever you log in. Recommended.|" \
           "Myself|I'll run the narratarr command when I want it.|"
      [ "$CHOICE" = 0 ] && AUTOSTART=yes || AUTOSTART=no
    else
      AUTOSTART=no
    fi
  fi

  local url="http://127.0.0.1:$PORT"
  if [ "$AUTOSTART" = yes ] && [ -n "$can_autostart" ]; then
    port_taken "$PORT" && warn "Something is already using port $PORT; Narratarr may not be able to start."
    if [ "$can_autostart" = launchd ]; then run "Setting it to start when you log in" autostart_macos
    else run "Setting it to start when you log in" autostart_linux; fi
    run "Starting Narratarr" wait_healthy "$PORT"
    open_browser "$url"
    box "${B}Narratarr is running.${R}" "" \
        "Open it:     ${CORAL}$url${R}" \
        "$(first_visit_line)" \
        "Library:     $(pretty "$LIBRARY")" \
        "Settings:    $(pretty "$DATA_DIR")" "" \
        "Update:      run the install command again" \
        "Uninstall:   run it again with --uninstall"
  else
    case ":$PATH:" in *":$(dirname "$BIN"):"*) ;; *) uv tool update-shell >>"$LOG" 2>&1 || true
      warn "Open a new terminal so the ${B}narratarr${R} command is found." ;; esac
    box "${B}Narratarr is installed.${R}" "" \
        "Start it:    ${CORAL}narratarr --port $PORT${R}" \
        "Then open:   $url" \
        "$(first_visit_line)" \
        "Library:     $(pretty "$LIBRARY")" "" \
        "Update:      run the install command again"
  fi
}

# ---- docker ------------------------------------------------------------------------

compose() {
  if docker compose version >/dev/null 2>&1; then docker compose "$@"; else docker-compose "$@"; fi
}

install_docker() {
  STEPS=4
  step "Checking Docker"
  command -v docker >/dev/null 2>&1 || die "Docker isn't installed. Get it from https://docs.docker.com/get-docker/"
  docker info >/dev/null 2>&1 || die "Docker is installed but not running. Start Docker (Desktop) and run this again."
  { docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1; } \
    || die "Docker Compose is missing. See https://docs.docker.com/compose/install/"
  ok "Docker is ready ($(docker version --format '{{.Server.Version}}' 2>/dev/null))"

  step "Your library"
  choose_library
  choose_port
  [ -n "$DOCKER_DIR" ] || { ask "Where should Narratarr keep its settings?" "~/narratarr"; DOCKER_DIR="$ANSWER"; }
  DOCKER_DIR="$(expand "$DOCKER_DIR")"
  mkdir -p "$DOCKER_DIR/config"
  ok "Books will go to ${B}$(pretty "$LIBRARY")${R}"

  step "Installing Narratarr"
  cat >"$DOCKER_DIR/docker-compose.yml" <<EOF
# Made by the Narratarr installer. Change values in .env, then run: docker compose up -d
services:
  narratarr:
    image: \${NARRATARR_IMAGE}
    container_name: narratarr
    restart: unless-stopped
    ports:
      - "\${NARRATARR_PORT}:8484"
    environment:
      - PUID=\${PUID}
      - PGID=\${PGID}
      - TZ=\${TZ}
    volumes:
      - ./config:/config
      - \${NARRATARR_LIBRARY}:/audiobooks
EOF
  local tz
  tz="$(readlink /etc/localtime 2>/dev/null | sed 's#.*/zoneinfo/##')"
  cat >"$DOCKER_DIR/.env" <<EOF
NARRATARR_IMAGE=$IMAGE
NARRATARR_PORT=$PORT
NARRATARR_LIBRARY='$LIBRARY'
PUID=$(id -u)
PGID=$(id -g)
TZ=${tz:-Etc/UTC}
EOF
  chmod 600 "$DOCKER_DIR/.env"
  ok "Wrote $(pretty "$DOCKER_DIR")/docker-compose.yml"

  cd "$DOCKER_DIR"
  if port_busy "$PORT" && ! docker ps --format '{{.Names}}' | grep -qx narratarr; then
    warn "Something is already using port $PORT; the container may not be able to start."
  fi
  if ! run_quiet compose pull; then
    docker image inspect "$IMAGE" >/dev/null 2>&1 || die "Couldn't download $IMAGE. Check your internet connection."
    warn "Couldn't check for a newer image; using the one already here."
  else
    ok "Downloaded the Narratarr image"
  fi
  run "Starting the container" compose up -d
  run "Starting Narratarr" wait_healthy "$PORT"

  step "Your account"
  create_account compose exec -T -u "$(id -u):$(id -g)" narratarr narratarr

  local url="http://127.0.0.1:$PORT"
  open_browser "$url"
  box "${B}Narratarr is running in Docker.${R}" "" \
      "Open it:     ${CORAL}$url${R}" \
      "$(first_visit_line)" \
      "Library:     $(pretty "$LIBRARY")" \
      "Settings:    $(pretty "$DOCKER_DIR")" "" \
      "Update:      cd $(pretty "$DOCKER_DIR") && docker compose pull && docker compose up -d" \
      "Logs:        docker logs -f narratarr"
}

run_quiet() {   # like run, but returns the status instead of stopping the installer
  local status=0
  if [ "$FANCY" = 1 ]; then printf '  %s⠿%s Downloading the Narratarr image (this can take a minute)' "$CORAL" "$R"; fi
  "$@" >>"$LOG" 2>&1 || status=$?
  [ "$FANCY" = 1 ] && printf '\r\033[2K'
  return $status
}

# ---- uninstall ---------------------------------------------------------------------

uninstall() {
  STEPS=1
  step "Removing Narratarr"
  local found=0 dir="${DOCKER_DIR:-$HOME/narratarr}"
  dir="$(expand "$dir")"
  if [ "$(uname)" = Darwin ] && [ -f "$HOME/Library/LaunchAgents/$LABEL.plist" ]; then
    launchctl bootout "gui/$(id -u)/$LABEL" >/dev/null 2>&1 || true
    rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"; ok "Stopped starting at login"; found=1
  fi
  if [ -f "$HOME/.config/systemd/user/narratarr.service" ]; then
    systemctl --user disable --now narratarr.service >/dev/null 2>&1 || true
    rm -f "$HOME/.config/systemd/user/narratarr.service"; ok "Removed the systemd service"; found=1
  fi
  if command -v uv >/dev/null 2>&1 && uv tool list 2>/dev/null | grep -q '^narratarr '; then
    run "Removing the narratarr command" uv tool uninstall narratarr; found=1
  fi
  if [ -f "$dir/docker-compose.yml" ] && command -v docker >/dev/null 2>&1; then
    (cd "$dir" && run "Stopping and removing the container" compose down); found=1
    info "Docker settings are still in $(pretty "$dir") (delete that folder to remove them)."
  fi
  [ "$found" = 1 ] || warn "Didn't find a Narratarr install to remove."
  info "Your audiobooks and $(pretty "$DATA_DIR") were left untouched."
  say ""
}

# ---- main ----------------------------------------------------------------------------

main() {
  banner
  if [ "$UNINSTALL" = 1 ]; then uninstall; return; fi

  case "$(uname)" in Darwin|Linux) ;; *) die "This installer supports macOS and Linux. On Windows, use Docker Desktop (see the README)." ;; esac
  command -v curl >/dev/null 2>&1 || die "curl is required."
  if [ "$INTERACTIVE" = 0 ] && [ -z "$METHOD" ] && [ "$ASSUME_YES" = 0 ]; then
    die "No terminal to ask questions in. Re-run with --docker or --native (add --yes to accept defaults)."
  fi

  if [ -z "$METHOD" ]; then
    local docker_off=""
    command -v docker >/dev/null 2>&1 || docker_off="Docker isn't installed"
    say "  How would you like to run Narratarr?"
    say ""
    menu "Native|Installs like a normal app. Simplest on a Mac or PC.|" \
         "Docker|Runs in a container. Great for servers and NAS boxes.|$docker_off"
    [ "$CHOICE" = 0 ] && METHOD=native || METHOD=docker
  fi

  if [ "$METHOD" = docker ]; then install_docker; else install_native; fi
  rm -f "$LOG"
}

main
