#!/usr/bin/env bash
# Sheet Music Labeler: installer for Linux.
#
# Installs everything the app needs and checks at the end that it really works:
#   1. uv (a Python package manager) and Python 3.13, inside this folder (.tools/)
#   2. the Python packages, at the exact versions in requirements.lock, into .venv/
#   3. the OMR model weights
#   4. Audiveris (optional, improves scans), from Flathub, for your user only
#   5. a self-test: the app reads a small test PDF and test score and every note name is compared
#
# Run it from anywhere as your normal user (not root):
#   ./install.sh               ask before using sudo (only needed to install Flatpak)
#   ./install.sh --yes         don't ask, answer yes
#   ./install.sh --no-audiveris   skip Audiveris
#
# Safe to run again: steps that are already done are skipped, broken ones are redone.
# Linux only (x86_64; aarch64 should work but is untested). Needs a glibc-based distribution
# (Debian, Ubuntu, Mint, Fedora, Arch, openSUSE, ...), not Alpine.
#
# Uninstall: delete this folder, and if you want Audiveris gone too:
#   flatpak uninstall --user org.audiveris.audiveris

set -Eeuo pipefail

UV_VERSION=0.13.3       # pinned, so the installer behaves the same tomorrow
PYTHON_VERSION=3.13     # the version the lock file was tested with
AUDIVERIS_APP=org.audiveris.audiveris
FLATHUB_URL=https://dl.flathub.org/repo/flathub.flatpakrepo

# Work from the project folder, wherever the script is started from
ROOT="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
cd "$ROOT"
TOOLS="$ROOT/.tools"
LOG="$TOOLS/install.log"
UV="$TOOLS/bin/uv"
PY="$ROOT/.venv/bin/python"

# Keep everything uv downloads inside this folder: nothing in your home or system changes
export UV_INSTALL_DIR="$TOOLS/bin" UV_NO_MODIFY_PATH=1
export UV_PYTHON_INSTALL_DIR="$TOOLS/python" UV_CACHE_DIR="$TOOLS/cache"

ASSUME_YES=0
WITH_AUDIVERIS=1
for arg in "$@"; do
    case "$arg" in
        -y|--yes) ASSUME_YES=1 ;;
        --no-audiveris) WITH_AUDIVERIS=0 ;;
        -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "Unknown option: $arg (try --help)" >&2; exit 2 ;;
    esac
done

# --- Output helpers -----------------------------------------------------------
if [ -t 1 ]; then
    BOLD=$'\e[1m' GREEN=$'\e[32m' YELLOW=$'\e[33m' RED=$'\e[31m' RESET=$'\e[0m'
else
    BOLD="" GREEN="" YELLOW="" RED="" RESET=""
fi
step() { printf '\n%s==> %s%s\n' "$BOLD" "$*" "$RESET"; }
ok()   { printf '    %s✓%s %s\n' "$GREEN" "$RESET" "$*"; }
warn() { printf '    %s!%s %s\n' "$YELLOW" "$RESET" "$*"; }
die()  { printf '\n%sInstallation failed:%s %s\n' "$RED" "$RESET" "$*" >&2; exit 1; }

# Run a command with its output going to the log file only. On failure, show the end of
# the log so the reason is visible without opening it.
run() {
    local what="$1"; shift
    printf '\n--- %s: %s\n' "$what" "$*" >>"$LOG"
    if ! "$@" >>"$LOG" 2>&1; then
        printf '%s' "$RED" >&2
        tail -n 15 "$LOG" >&2
        printf '%s' "$RESET" >&2
        die "$what (full log: $LOG)"
    fi
}

# An unexpected error anywhere (set -e) says where it happened
trap 'die "unexpected error on line $LINENO of install.sh (log: $LOG)"' ERR

# Ask a yes/no question; without a terminal to ask on, the answer is no (unless --yes)
confirm() {
    [ "$ASSUME_YES" = 1 ] && return 0
    [ -t 0 ] || return 1
    local answer
    read -r -p "    $* [Y/n] " answer
    [[ -z "$answer" || "$answer" =~ ^[YyJj] ]]
}

download() {  # download URL to stdout with curl or wget, whichever exists
    if command -v curl >/dev/null; then curl -fsSL "$1"; else wget -qO- "$1"; fi
}

mkdir -p "$TOOLS"
: >"$LOG"
echo "${BOLD}Sheet Music Labeler: installer for Linux${RESET}"
echo "Installing into: $ROOT"

# --- 0. Check the system ------------------------------------------------------
step "Checking your system"
[ "$(uname -s)" = Linux ] || die "this installer is for Linux only. On Windows or macOS, follow the manual steps in README.md."
[ "$(id -u)" != 0 ] || die "run this as your normal user, not as root or with sudo. It asks for sudo itself when needed."

case "$(uname -m)" in
    x86_64) ok "processor: x86_64" ;;
    aarch64|arm64) warn "processor: $(uname -m), should work but is untested" ;;
    *) die "processor $(uname -m) is not supported (the OMR libraries exist for x86_64 and aarch64 only)" ;;
esac

# The prebuilt Python packages need glibc 2.28 or newer (any mainstream distribution since 2019)
GLIBC="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}')" || true
if [ -z "$GLIBC" ]; then
    die "no glibc found (Alpine and other musl-based systems are not supported by the OMR libraries)"
elif [ "$(printf '%s\n2.28\n' "$GLIBC" | sort -V | head -n1)" != 2.28 ]; then
    die "glibc $GLIBC is too old, 2.28 or newer is needed (e.g. Debian 10+, Ubuntu 20.04+, Fedora 29+)"
fi
ok "glibc $GLIBC"

command -v curl >/dev/null || command -v wget >/dev/null || die "neither curl nor wget is installed; install one of them first"
download https://pypi.org/simple/homr/ >/dev/null 2>&1 || die "no internet connection (could not reach pypi.org)"
ok "internet connection"

# Space: ~1 GB here (Python, packages, model), up to ~2 GB in your home for Audiveris and the
# Flatpak runtime it needs (less if other Flatpak apps already brought that runtime)
free_gb() { df -Pk "$1" | awk 'NR==2 {printf "%.1f", $4 / 1048576}'; }
[ "$(df -Pk "$ROOT" | awk 'NR==2 {print $4}')" -gt 1300000 ] || die "not enough disk space in $ROOT: $(free_gb "$ROOT") GB free, about 1.3 GB needed"
ok "disk space here: $(free_gb "$ROOT") GB free"

# --- 1. uv and Python ---------------------------------------------------------
step "Python $PYTHON_VERSION (inside this folder)"
if [ -x "$UV" ] && [ "$("$UV" --version 2>/dev/null | awk '{print $2}')" = "$UV_VERSION" ]; then
    ok "uv $UV_VERSION already installed"
else
    # The official installer, pinned to one version; it only writes to $UV_INSTALL_DIR
    installer="$TOOLS/uv-install.sh"
    download "https://astral.sh/uv/$UV_VERSION/install.sh" >"$installer" 2>>"$LOG" || die "could not download the uv installer"
    run "installing uv $UV_VERSION" sh "$installer"
    rm -f "$installer"
    ok "uv $UV_VERSION installed"
fi

# Reuse an existing .venv only if its Python is the right version and works; otherwise
# rebuild it (.venv only holds installed packages, nothing of yours)
if [ -d .venv ]; then
    have="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo broken)"
    if [ "$have" != "$PYTHON_VERSION" ]; then
        warn "existing .venv uses Python $have, rebuilding it with $PYTHON_VERSION"
        rm -rf .venv
    fi
fi
if [ ! -d .venv ]; then
    # --managed-python: uv's own Python build, the same on every distribution
    run "creating .venv with Python $PYTHON_VERSION" "$UV" venv --managed-python --python "$PYTHON_VERSION" .venv
fi
ok "$("$PY" --version) in .venv"

# --- 2. Python packages -------------------------------------------------------
step "Python packages (about 600 MB the first time)"
# the exact locked versions; packages already there at the right version are kept
run "installing packages from requirements.lock" "$UV" pip install --python "$PY" -r requirements.lock
ok "packages installed"

# --- 3. OMR model weights -----------------------------------------------------
step "OMR model weights (about 130 MB the first time)"
run "downloading the homr model weights" "$PY" -c "import homr.main as hm; hm.download_weights(False, False, False)"
ok "model weights ready"

# --- 4. Audiveris (optional) --------------------------------------------------
AUDIVERIS_STATE=skipped
install_flatpak() {
    # Flatpak itself comes from the distribution, which needs sudo
    local cmd
    if command -v apt-get >/dev/null; then cmd="sudo apt-get update && sudo apt-get install -y flatpak"
    elif command -v dnf >/dev/null; then cmd="sudo dnf install -y flatpak"
    elif command -v pacman >/dev/null; then cmd="sudo pacman -S --needed --noconfirm flatpak"
    elif command -v zypper >/dev/null; then cmd="sudo zypper --non-interactive install flatpak"
    else
        warn "Flatpak is not installed and this installer doesn't know your package manager."
        warn "Install Flatpak yourself (https://flatpak.org/setup/) and run ./install.sh again."
        return 1
    fi
    warn "Audiveris is installed with Flatpak, which is not on your system yet."
    if ! confirm "Install Flatpak now with: $cmd ?"; then
        warn "Flatpak not installed. Run '$cmd' and then ./install.sh again to add Audiveris."
        return 1
    fi
    # sudo asks for your password on the terminal, so this runs in the foreground
    bash -c "$cmd" || { warn "installing Flatpak failed"; return 1; }
}

if [ "$WITH_AUDIVERIS" = 1 ]; then
    step "Audiveris (improves scans; for your user only)"
    if command -v flatpak >/dev/null || install_flatpak; then
        if flatpak info --user "$AUDIVERIS_APP" >/dev/null 2>&1 || flatpak info "$AUDIVERIS_APP" >/dev/null 2>&1; then
            ok "Audiveris already installed"
            AUDIVERIS_STATE=installed
        elif [ "$(df -Pk "$HOME" | awk 'NR==2 {print $4}')" -lt 2200000 ]; then
            warn "not enough space in your home folder for Audiveris ($(free_gb "$HOME") GB free, about 2 GB needed); skipped"
        else
            # --user: no sudo needed, lives in ~/.local/share/flatpak
            echo "    downloading Audiveris and its runtime, this can take a few minutes..."
            if flatpak remote-add --user --if-not-exists flathub "$FLATHUB_URL" >>"$LOG" 2>&1 &&
               flatpak install --user --noninteractive -y flathub "$AUDIVERIS_APP" >>"$LOG" 2>&1; then
                ok "Audiveris installed"
                AUDIVERIS_STATE=installed
            else
                warn "installing Audiveris failed (see $LOG); the app works without it"
            fi
        fi
    fi
fi

# --- 5. Self-test ---------------------------------------------------------------
step "Testing: reading a test PDF, and a test score like a scan"
# "|| result=$?" keeps a non-zero exit from tripping set -e and the ERR trap
result=0
"$PY" tests/selftest.py >"$TOOLS/selftest.out" 2>>"$LOG" || result=$?
sed 's/^/    /' "$TOOLS/selftest.out"
case "$result" in
    0) ;;
    2) if [ "$AUDIVERIS_STATE" = installed ]; then
           warn "Audiveris is installed but the app could not use it; scans still work without it (see $LOG)"
       fi ;;
    *) die "the self-test failed, so the app would not work (log: $LOG)" ;;
esac

trap - ERR
if [ "$result" = 0 ]; then
    printf '\n%s%sInstalled. Everything works.%s\n\n' "$BOLD" "$GREEN" "$RESET"
else
    printf '\n%s%sInstalled. The app works, without Audiveris.%s\n\n' "$BOLD" "$GREEN" "$RESET"
fi
echo "Start the app:"
echo "    cd \"$ROOT\""
echo "    .venv/bin/python app.py"
echo
echo "Your browser opens the app at http://localhost:8765. Ctrl+C in the terminal stops it."
# the self-test tells whether the app really uses Audiveris, whatever this run did
[ "$result" = 0 ] || echo "Without Audiveris, scans get somewhat more orange marks. Run ./install.sh again to add it."
