#!/usr/bin/env bash
# kindle-butch-gen OnePlus 13 (Adreno 830 GPU + OpenCL) Deploy Script
# Run this script inside standard Termux on the target OnePlus 13 device.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

log() {
    echo -e "${BLUE}[DEPL]${NC} $1"
}

success() {
    echo -e "${GREEN}[OK]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1"
    exit 1
}

AUTOSTART=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -a|--autostart)
            AUTOSTART=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [-a|--autostart]"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [-a|--autostart]"
            exit 1
            ;;
    esac
done

log "Starting deployment of kindle-butch-gen on OnePlus 13..."

# Ask interactively if not passed as CLI argument
if [ "$AUTOSTART" = "false" ]; then
    echo -n -e "${BLUE}[DEPL]${NC} Do you want to configure automatic startup of services (sshd, llama-server, web server) on Termux launch? (y/N): "
    read -r choice || choice=""   # EOF-safe: set -e must not kill a non-interactive run
    case "$choice" in 
        [yY]|[yY][eE][sS])
            AUTOSTART=true
            log "Autostart configuration enabled."
            ;;
        *)
            AUTOSTART=false
            log "Autostart configuration skipped."
            ;;
    esac
fi

# -------------------------------------------------------------
# TASK-32: hold a wake-lock for the WHOLE script, not just the
# autostart-block's future Flask launch. This deploy run includes a
# long GPU compile (llama.cpp) and multi-GB model downloads - without
# this, Android can suspend/kill the whole process if the screen locks
# mid-run (the exact failure mode observed repeatedly this session with
# the manga pipeline before it got the same fix). trap on EXIT/INT/TERM
# (not just normal completion) so a Ctrl+C or an early `set -e` exit
# still releases it - the device must never be left locked-from-sleep
# permanently because this script died partway through.
# -------------------------------------------------------------
log "Acquiring termux-wake-lock for the duration of deployment..."
termux-wake-lock 2>/dev/null || true
# termux-wake-lock only blocks CPU sleep - Android's separate Battery
# Optimization / Adaptive Battery can still kill the whole Termux process
# in the background regardless (observed live: a 4.4GB model download
# lost mid-transfer, whole process gone, no error - just silence). This
# is the actual #1 cause of failed installs on unmodified devices; print
# it loudly, not just in docs nobody reads mid-install.
echo -e "${RED}=====================================================================${NC}"
echo -e "${RED}⚠️  ВАЖЛИВО: вимкніть оптимізацію батареї для Termux ЗАРАЗ${NC}"
echo -e "${RED}=====================================================================${NC}"
echo -e "Android може вбити цей процес у фоні (навіть з екраном увімкненим),"
echo -e "і завантаження на кілька гігабайт доведеться починати заново."
echo -e "Налаштування → Застосунки → Termux → Батарея → ${GREEN}Без обмежень${NC}"
echo -e "(на деяких пристроях: Оптимізація батареї → Усі застосунки → Termux →"
echo -e "${GREEN}Не оптимізувати${NC})"
echo -e "${RED}=====================================================================${NC}"
release_deploy_wake_lock() {
    log "Releasing termux-wake-lock..."
    termux-wake-unlock 2>/dev/null || true
    # P1.1 SIGTERM-test finding: TERMing this script released the wake-lock
    # but ORPHANED the whole container-side child tree (launcher -> proot ->
    # apt/dpkg kept running, holding the container's dpkg lock and breaking
    # an immediate resume re-run) - the deploy-level twin of TASK-40. Kill
    # our own process group on the way out; trap is reset first so the
    # group-TERM reaching ourselves can't recurse. Harmless on a normal
    # successful exit (children are already reaped by then).
    trap - EXIT INT TERM
    kill -TERM -- -$$ 2>/dev/null || true
}
trap release_deploy_wake_lock EXIT INT TERM

# -------------------------------------------------------------
# STEP 0: Pre-flight diagnostics (TASK-50) - fail fast with a clear
# table BEFORE any long download/compile. Hard requirements abort;
# soft ones only warn. Designed for a non-specialist following the
# @GetVydraBot /install instructions on a fresh device.
# -------------------------------------------------------------
log "Running pre-flight diagnostics..."
DIAG_FAILED=0
diag() { # $1=PASS|WARN|FAIL $2=label $3=detail
    case "$1" in
        PASS) echo -e "  ${GREEN}[PASS]${NC} $2 — $3" ;;
        WARN) echo -e "  \033[0;33m[WARN]\033[0m $2 — $3" ;;
        FAIL) echo -e "  ${RED}[FAIL]${NC} $2 — $3"; DIAG_FAILED=1 ;;
    esac
}

case "$(uname -m)" in
    aarch64) diag PASS "Архітектура" "aarch64" ;;
    *) diag FAIL "Архітектура" "$(uname -m) — потрібен aarch64 (64-бітний Android)" ;;
esac

case "${PREFIX:-}" in
    *com.termux*) diag PASS "Середовище" "Termux" ;;
    *) diag FAIL "Середовище" "не Termux — встановіть Termux з F-Droid (НЕ з Play Market)" ;;
esac

MEM_KB=$(grep MemTotal /proc/meminfo 2>/dev/null | awk '{print $2}' || echo 0)
MEM_GB=$((MEM_KB / 1024 / 1024))
if [ "$MEM_GB" -ge 10 ]; then diag PASS "Оперативна пам'ять" "${MEM_GB}GB"
elif [ "$MEM_GB" -ge 6 ]; then diag WARN "Оперативна пам'ять" "${MEM_GB}GB — мінімум; великі книги можуть падати"
else diag FAIL "Оперативна пам'ять" "${MEM_GB}GB — потрібно щонайменше 6GB (модель перекладу займає ~5GB)"; fi

FREE_GB=$(df -Pk "$HOME" 2>/dev/null | awk 'NR==2 {print int($4/1024/1024)}' || echo 0)
if [ "$FREE_GB" -ge 25 ]; then diag PASS "Вільне місце" "${FREE_GB}GB"
elif [ "$FREE_GB" -ge 15 ]; then diag WARN "Вільне місце" "${FREE_GB}GB — вистачить, але впритул (модель 4.4GB + контейнер ~8GB)"
else diag FAIL "Вільне місце" "${FREE_GB}GB — потрібно щонайменше 15GB"; fi

if curl -s -m 8 -o /dev/null "https://github.com"; then diag PASS "Мережа" "github.com доступний"
else diag FAIL "Мережа" "github.com недоступний — перевірте інтернет"; fi
if curl -s -m 8 -o /dev/null "https://huggingface.co"; then diag PASS "Мережа" "huggingface.co доступний (звідти тягнеться модель)"
else diag WARN "Мережа" "huggingface.co недоступний — завантаження моделі може не пройти"; fi

if [ -e /vendor/lib64/libOpenCL.so ]; then diag PASS "GPU" "Adreno OpenCL знайдено — переклад буде GPU-прискорений"
else diag WARN "GPU" "Adreno OpenCL не знайдено — все працюватиме, але на CPU (повільніше)"; fi

# TASK-64: newest ARMv9 SoCs (e.g. Snapdragon 8 Elite 2) advertise SVE in
# /proc/cpuinfo, but Android kernels commonly don't enable userspace SVE
# access - PyTorch's oneDNN/ACL conv backend then dies with SIGILL on the
# first forward pass. translate_manga.py self-detects this at runtime
# (torch reports an SVE capability -> mkldnn is disabled) - nothing to
# configure here; this diagnostic just tells the installer up front which
# code path their device will use.
if grep -qw sve /proc/cpuinfo 2>/dev/null; then
    diag WARN "CPU SVE" "процесор ARMv9 з SVE — OCR-детектор манґи автоматично працюватиме в сумісному (трохи повільнішому) режимі без oneDNN (TASK-64)"
else
    diag PASS "CPU SVE" "SVE відсутній — стандартний швидкий шлях oneDNN"
fi

if [ -d "$HOME/.termux/boot" ] || [ -e "/data/data/com.termux.boot" ]; then
    diag PASS "Termux:Boot" "встановлено — сервіси стартуватимуть після перезавантаження"
else
    diag WARN "Termux:Boot" "не знайдено — встановіть з F-Droid і відкрийте один раз, інакше після перезавантаження телефона сервіси доведеться стартувати вручну (відкриттям Termux)"
fi

if [ "$DIAG_FAILED" -ne 0 ]; then
    error "Діагностика виявила невиконані вимоги (позначені FAIL вище). Виправте їх і запустіть скрипт знову."
fi
success "Діагностика пройдена."
echo -e "${BLUE}[DEPL]${NC} Далі: встановлення пакетів, ~10-20 хв компіляції та завантаження моделі 4.4GB."
echo -e "${BLUE}[DEPL]${NC} ТРИМАЙТЕ ЕКРАН УВІМКНЕНИМ і Termux відкритим — Android вбиває фонові важкі процеси."

# -------------------------------------------------------------
# STEP 1: Install Termux Host Prerequisites
# -------------------------------------------------------------
log "Installing host Termux packages..."
pkg update -y
# TASK-50 fresh-device fix (found on the FIRST real outside install,
# OnePlus 15): a fresh Termux bootstrap ships an old libc++, and newly
# installed packages (ffmpeg 8.x / libplacebo) reference newer symbols -
# "CANNOT LINK EXECUTABLE ... _ZNSt6__ndk1...from_chars..." and dpkg dies.
# A FULL upgrade first brings the runtime to match the repo, exactly as
# Termux's own error message prescribes. Non-interactive-safe; the
# configure -a afterwards heals any half-configured packages left by a
# previously failed attempt (rerunning the script after this fix is
# enough - no manual cleanup needed).
DEBIAN_FRONTEND=noninteractive pkg upgrade -y -o Dpkg::Options::="--force-confnew" || true
dpkg --configure -a 2>/dev/null || true
# libandroid-spawn: provides spawn.h + posix_spawn support - Termux's
# curated libc link-time stub excludes it even on devices whose real
# system bionic has it (confirmed API 36 does). Without this package,
# ANY llama.cpp target that includes vendor/sheredom/subprocess.h
# (llama-mtmd-cli) fails to even compile. Verified end-to-end on a real
# device: header present -> full build succeeds, zero extra link flags
# needed. Missed this on the first pass (assumed a bare platform gap
# without checking Termux's own package repo - Q caught it).
# python-pillow: TASK-76 needs PIL on the HOST (Flask process generates
# character-scan thumbnails directly, no proot involved) - deliberately
# via pkg, NOT pip. The proot Ubuntu container's Pillow install (below,
# apt python3-pil) already hit a source-build dead end with pip because
# proot injects the Termux-bound prefix into a foreign gcc; the plain
# Termux host doesn't have that problem, but Termux's own prebuilt
# python-pillow package is still the safer/faster path (no compile) than
# hoping pip resolves a matching wheel for whatever Python version this
# device's Termux ships.
pkg install -y proot-distro git termux-exec clang cmake make ocl-icd opencl-headers rsync termux-api ffmpeg python python-pip python-pillow libandroid-spawn
pip install --upgrade pip --break-system-packages || true
# ipa-uk intentionally absent: the package was REMOVED from PyPI (404,
# found by the first outside install on OnePlus 15) - a vendored copy
# ships in common/vendor/ipa_uk and bin/tts_helper.py falls back to it.
# ukrainian_word_stress removed from the HOST line (3rd finding of the
# OnePlus 15 install): nothing host-side imports it (it runs in the
# container, installed there as stress-uk), yet it drags stanza -> numpy
# -> source builds that cannot succeed on bionic (ninja/spawn.h death).
pip install Flask flask-httpauth requests tqdm marisa-trie blinker --break-system-packages
success "Termux host packages installed."

# -------------------------------------------------------------
# TASK-32: GPU/Adreno detection - Stage 1 (Termux-side, cheap file
# check, before any Adreno-specific step runs). clinfo doesn't exist
# yet at this point (Step 3 is what installs it inside the container),
# so it can't gate whether we even attempt Step 2/3's Adreno-specific
# work - this file check is the earliest signal available. This is the
# ONE deliberately-designed fallback in this script (everything else
# here is meant to hard-stop via `set -e` on failure) - not a general
# error-tolerance policy.
# -------------------------------------------------------------
ADRENO_DETECTED=false
if [ -e /vendor/lib64/libOpenCL.so ]; then
    ADRENO_DETECTED=true
    success "Adreno GPU detected (/vendor/lib64/libOpenCL.so present)."
else
    log "No Adreno /vendor/lib64/libOpenCL.so found on this device."
    echo -e "${BLUE}[DEPL]${NC} This device's Ubuntu PRoot GPU bind-mounts and OpenCL config are Adreno/OnePlus-13-specific and will be skipped; building llama.cpp CPU-only instead."
fi

# -------------------------------------------------------------
# STEP 2: Configure Ubuntu PRoot Container with GPU Bind Mounts
# -------------------------------------------------------------
# TASK-32/P1.1: the target container alias is parametrized so the REAL
# script can be staged against a scratch container on a production device
# (KBG_DEPLOY_DISTRO=ubuntu-test bash deploy.sh) without touching the
# working 'ubuntu' container or its launcher. Default is production.
DEPLOY_DISTRO="${KBG_DEPLOY_DISTRO:-ubuntu}"
log "Setting up Ubuntu PRoot container (alias: $DEPLOY_DISTRO)..."
# TASK-32 hardware-test fix (second iteration, verified on the real
# device this time): the original check piped `grep -q` into a second
# grep - `grep -q` produces no stdout, so the second grep ALWAYS exited 1
# and the script ALWAYS attempted `proot-distro install ubuntu`, which
# hard-fails (via set -e) on any device where ubuntu is already
# installed. The first fix checked $PREFIX/var/lib/proot-distro/
# installed-rootfs/ubuntu - which is the layout older proot-distro
# versions use, but the production OnePlus 13 runs a version that keeps
# containers in .../containers/ubuntu and prints a different `list`
# format entirely. Check BOTH known directory layouts, with a functional
# login probe as the version-proof tiebreaker. (Deliberately NOT parsing
# `proot-distro list`: the old format lists every AVAILABLE distro with
# "Alias: ubuntu" lines, so a name grep false-positives on a fresh
# device and would skip a genuinely-needed install.)
PROOT_VAR="$PREFIX/var/lib/proot-distro"
if [ -d "$PROOT_VAR/installed-rootfs/$DEPLOY_DISTRO" ] \
   || [ -d "$PROOT_VAR/containers/$DEPLOY_DISTRO" ] \
   || proot-distro login "$DEPLOY_DISTRO" -- /bin/true >/dev/null 2>&1; then
    UBUNTU_INSTALLED=true
else
    UBUNTU_INSTALLED=false
fi
if [ "$UBUNTU_INSTALLED" = "false" ]; then
    # P1.1 fresh-test finding: a bare 'ubuntu' image resolves to the LATEST
    # interim release (25.10/python3.14 at test time), where marker-pdf's
    # 'Pillow<11' pin is unsatisfiable as a binary (Pillow 10.x predates
    # py3.14) and the source build is unwinnable inside proot (Pillow's
    # setup.py detects the bound Termux prefix and injects Android headers
    # into the Ubuntu gcc). Pin the container to the 24.04 LTS image
    # (python3.12), where every pinned dependency has a prebuilt wheel.
    log "Installing Ubuntu 24.04 LTS container via proot-distro (alias: $DEPLOY_DISTRO)..."
    proot-distro install ubuntu:24.04 --override-alias "$DEPLOY_DISTRO"
else
    log "Ubuntu container '$DEPLOY_DISTRO' is already installed."
fi

# Create a launcher script to run Ubuntu - Adreno-bound if detected,
# plain otherwise (TASK-32 Stage 1 detection above). A non-default
# DEPLOY_DISTRO gets its own launcher file so a staging run never
# overwrites the production launcher.
if [ "$DEPLOY_DISTRO" = "ubuntu" ]; then
    LAUNCHER_PATH="$HOME/ubuntu-gpu.sh"
else
    LAUNCHER_PATH="$HOME/ubuntu-gpu.$DEPLOY_DISTRO.sh"
fi
if [ "$ADRENO_DETECTED" = "true" ]; then
    log "Creating GPU-enabled Ubuntu launcher at ${LAUNCHER_PATH}..."
    cat << EOF > "$LAUNCHER_PATH"
#!/usr/bin/env bash
# Runs Ubuntu PRoot container with Android system vendor directories bind-mounted for OpenCL GPU access
proot-distro login $DEPLOY_DISTRO \\
  --bind /vendor:/vendor \\
  --bind /system:/system \\
  --bind /vendor/lib64:/vendor/lib64 \\
  --bind /system/lib64:/system/lib64 \\
  --bind /dev/kgsl:/dev/kgsl \\
  "\$@"
EOF
else
    log "Creating plain (CPU-only) Ubuntu launcher at ${LAUNCHER_PATH}..."
    cat << EOF > "$LAUNCHER_PATH"
#!/usr/bin/env bash
# No Adreno GPU detected on this device - plain Ubuntu PRoot login, no GPU bind-mounts.
proot-distro login $DEPLOY_DISTRO "\$@"
EOF
fi
chmod +x "$LAUNCHER_PATH"
success "Ubuntu launcher created at ${LAUNCHER_PATH}."

# -------------------------------------------------------------
# STEP 3: Clone/update the kindle-butch-gen project itself
# -------------------------------------------------------------
# TASK-32: this MUST happen before Step 4 below - Step 4 writes a setup
# script into $HOME/kindle-butch-gen/, which requires the directory (and
# therefore a real clone) to already exist. The previous script's Step 4
# only did `mkdir -p` + a `chmod ... || true` of a file that may not
# exist - it silently relied on the project already having been placed
# there some other way, which is exactly the gap this fixes.
REPO_URL="https://github.com/maxfraieho/kindle-butch-gen.git"
PROJECT_DIR="$HOME/kindle-butch-gen"
log "Setting up kindle-butch-gen project files..."
if [ -d "$PROJECT_DIR/.git" ]; then
    log "kindle-butch-gen already cloned at ${PROJECT_DIR}, pulling latest..."
    git -C "$PROJECT_DIR" pull --ff-only
    success "kindle-butch-gen updated to latest."
else
    log "Cloning kindle-butch-gen into ${PROJECT_DIR}..."
    git clone "$REPO_URL" "$PROJECT_DIR"
    success "kindle-butch-gen cloned."
fi
chmod +x "$PROJECT_DIR/kbg.sh"

# -------------------------------------------------------------
# STEP 3b (TASK-47/50): Termux-side llama.cpp - what production
# ACTUALLY runs. start-translation-server.sh launches
# ~/llama.cpp/build/bin/llama-server with Android vendor GPU libs on
# LD_LIBRARY_PATH; the container build below never served anything.
# Build here, host-side, with Adreno OpenCL when detected. This is the
# one long compile of the install (~10-20 min) - wake-lock is already
# held and the user was told to keep the screen on.
# -------------------------------------------------------------
if [ -x "$HOME/llama.cpp/build/bin/llama-server" ]; then
    success "Termux-side llama.cpp already present (~/llama.cpp/build/bin) - skipping build."
else
    log "Building llama.cpp Termux-side (this is the long step - 10-20 min)..."
    HOST_CMAKE_GPU_FLAGS=""
    if [ "$ADRENO_DETECTED" = "true" ]; then
        HOST_CMAKE_GPU_FLAGS="-DGGML_OPENCL=ON -DGGML_OPENCL_USE_ADRENO_KERNELS=ON"
    fi
    if [ ! -d "$HOME/llama.cpp/.git" ]; then
        git clone --depth 1 https://github.com/ggerganov/llama.cpp.git "$HOME/llama.cpp"
    fi
    cd "$HOME/llama.cpp"
    # 4th finding of the first outside install (OnePlus 15 / Snapdragon 8
    # Elite 2): -mcpu=native code paths for the newest ARMv9 cores ICE the
    # Termux clang on ggml-cpu/arch/arm/repack.cpp. GGML_NATIVE=OFF builds
    # the portable armv8-a baseline - fine, because heavy lifting runs on
    # the GPU via OpenCL (-ngl 99) anyway. Build dir is wiped first so a
    # previously failed configure cache can never poison the retry.
    rm -rf build
    mkdir -p build && cd build
    cmake .. $HOST_CMAKE_GPU_FLAGS -DGGML_NATIVE=OFF -DLLAMA_CURL=OFF &
    wait $! || error "llama.cpp cmake configure failed"
    make -j"$(nproc)" llama-server llama-cli &
    wait $! || error "llama.cpp build failed"

    # llama-mtmd-cli (premium Vision-QA support): BEST-EFFORT, not fatal.
    # Found on the OnePlus 15 fresh install: vendor/sheredom/subprocess.h
    # (pulled in by mtmd-helper.cpp) does an unconditional `#include
    # <spawn.h>` with no __ANDROID__ guard in upstream llama.cpp - Termux's
    # bionic libc does not ship spawn.h at all (a real platform gap, not a
    # missing package). Blocking every install over a not-yet-shipped
    # feature (TASK-54's Vision-QA isn't built yet) would be wrong -
    # llama-server/llama-cli above are the hard requirement and already
    # verified. Real fix (build mtmd inside the glibc Ubuntu container,
    # which already gets the same GPU vendor-lib bind mounts) is tracked
    # as follow-up work, not attempted here under a live user's install.
    log "Building llama-mtmd-cli (optional, for future premium vision features)..."
    make -j"$(nproc)" llama-mtmd-cli > /tmp/mtmd-build.log 2>&1 &
    wait $! && success "llama-mtmd-cli built." \
        || log "llama-mtmd-cli skipped (Termux bionic lacks spawn.h - known gap, tracked separately). Core translation/manga features are unaffected."
    cd "$HOME"
    [ -x "$HOME/llama.cpp/build/bin/llama-server" ] || error "llama-server binary missing after build"
    success "Termux-side llama.cpp built ($HOST_CMAKE_GPU_FLAGS)."
fi

# The launcher production autostart actually calls - install if absent
# (never overwrite: the production phone's copy may carry local tuning).
if [ ! -f "$HOME/start-translation-server.sh" ]; then
    cp "$PROJECT_DIR/bin/start-translation-server.sh" "$HOME/start-translation-server.sh"
    chmod +x "$HOME/start-translation-server.sh"
    success "start-translation-server.sh installed to \$HOME."
else
    log "start-translation-server.sh already present in \$HOME - not overwriting."
fi

# -------------------------------------------------------------
# STEP 4: Setup OpenCL ICD and Compile llama.cpp inside Ubuntu
# -------------------------------------------------------------
log "Configuring OpenCL and compiling llama.cpp inside Ubuntu container..."

# We write a setup script that will be executed inside the Ubuntu
# container. TASK-32: ADRENO_DETECTED is written first as a plain
# (interpolated) line, since the rest of this heredoc is deliberately
# single-quoted 'EOF' to protect its own $(nproc)/etc. variables (meant
# to be evaluated INSIDE the container at run time, not by this outer
# Termux shell at write time).
# TASK-32 hardware-test finding (2026-07-17): PRODUCTION runs llama-server
# TERMUX-SIDE (~/llama.cpp/build/bin, launched by start-translation-server.sh
# with Android GPU libs on LD_LIBRARY_PATH) - NOT the container build this
# step produces. On the real OnePlus 13 the container check below therefore
# always failed and the script launched a full llama.cpp compile on a device
# that already had a working (GPU-accelerated) server - a massive thermal
# burst that got Termux SIGKILLed by Android. If a working Termux-side build
# exists, skip the container build entirely.
SKIP_LLAMA_BUILD=false
if [ -x "$HOME/llama.cpp/build/bin/llama-server" ]; then
    SKIP_LLAMA_BUILD=true
    success "Working Termux-side llama.cpp build detected (~/llama.cpp/build/bin) - container build will be skipped."
fi

UBUNTU_SETUP_SCRIPT_PATH="/data/data/com.termux/files/home/kindle-butch-gen/ubuntu_setup.sh"
{
    echo "#!/usr/bin/env bash"
    echo "set -euo pipefail"
    echo "ADRENO_DETECTED=$ADRENO_DETECTED"
    echo "SKIP_LLAMA_BUILD=$SKIP_LLAMA_BUILD"
    cat << 'EOF'

echo "=== [Ubuntu Setup] ==="
apt update
# libjpeg-dev/zlib1g-dev/libpng-dev: Pillow has no prebuilt wheel for this
# container's Python and falls back to a source build, which hard-fails
# without them (found by the P1.1 fresh-container run; the production
# container's FreeType-less Pillow is the same disease - it was source-
# built before libfreetype6-dev was in this list). Even with those
# headers, Pillow\'s own setup.py detects the proot-bound Termux prefix
# (/data/data/com.termux/files/usr) and injects Android headers into the
# Ubuntu gcc - the source build is unwinnable here. python3-pil gives a
# proper Ubuntu-built Pillow that pip recognizes and never rebuilds.
apt install -y build-essential cmake git opencl-headers ocl-icd-opencl-dev clinfo python3-pip python3-venv libgomp1 calibre ffmpeg tesseract-ocr unrar-free p7zip-full libfreetype6-dev libjpeg-dev zlib1g-dev libpng-dev wamerican python3-pil

# 1. Configure OpenCL ICD for Qualcomm Adreno GPU - TASK-32 Stage 2: only
# attempted if Stage 1 (Termux-side /vendor/lib64/libOpenCL.so check,
# done before this script was even written) found a GPU. clinfo's own
# result further downgrades CMAKE_GPU_FLAGS to CPU-only even when Stage 1
# passed, covering "the file exists but runtime GPU access is actually
# broken/denied".
CMAKE_GPU_FLAGS=""
if [ "$ADRENO_DETECTED" = "true" ]; then
    echo "Configuring Adreno GPU OpenCL drivers..."
    mkdir -p /etc/OpenCL/vendors
    # Adreno driver on Android is traditionally located in /vendor/lib64/libOpenCL.so
    # KGSL (/dev/kgsl) device permissions are required to access GPU hardware
    echo "/vendor/lib64/libOpenCL.so" > /etc/OpenCL/vendors/adreno.icd

    echo "Verifying OpenCL devices..."
    if clinfo | grep -q -i "platform"; then
        echo "OpenCL platform verified successfully:"
        clinfo | grep -E -i "Name|Vendor|Version"
        CMAKE_GPU_FLAGS="-DGGML_OPENCL=ON -DGGML_OPENCL_USE_ADRENO_KERNELS=ON"
    else
        echo "Warning: clinfo failed to list OpenCL devices even though /vendor/lib64/libOpenCL.so was present - falling back to a CPU-only build."
    fi
else
    echo "Skipping Adreno OpenCL configuration (no GPU detected on this device) - building CPU-only."
fi

# 2. Compile llama.cpp - TASK-32: resumable. Checks the actual install
# destination (/usr/local/bin, persistent inside this PRoot container)
# rather than a /tmp build-directory marker (/tmp is wiped on Android
# reboot) before doing a full rm -rf + reclone + recompile. This also
# correctly handles a build killed mid-compile (nothing yet installed to
# /usr/local/bin => rebuild) vs. a genuinely completed prior run (skip).
if [ "$SKIP_LLAMA_BUILD" = "true" ]; then
    echo "llama.cpp: host has a working Termux-side build (~/llama.cpp/build/bin) that production's start-translation-server.sh actually uses - skipping the container build."
    # Clean any leftovers from a previously interrupted container build
    # (an Android-killed compile once left 800+MB in here).
    rm -rf /tmp/llama.cpp
elif [ -x /usr/local/bin/llama-server ] && [ -x /usr/local/bin/llama-cli ]; then
    echo "llama.cpp binaries already present at /usr/local/bin - skipping recompilation."
else
    echo "Cloning and building llama.cpp (flags: ${CMAKE_GPU_FLAGS:-CPU-only})..."
    cd /tmp
    if [ -d "llama.cpp" ]; then rm -rf llama.cpp; fi
    git clone --depth 1 https://github.com/ggerganov/llama.cpp.git
    cd llama.cpp
    mkdir build && cd build
    cmake .. $CMAKE_GPU_FLAGS
    make -j$(nproc)
    cp bin/llama-cli bin/llama-server /usr/local/bin/
    echo "llama.cpp compiled and installed to /usr/local/bin/."
fi

# 3. Setup Python OCR, Manga translation, and ML dependencies (including stress-uk)
echo "Installing Python dependencies (PyTorch, Transformers, Marker, Manga-OCR, Mokuro, PyTesseract, stress-uk, num2words)..."
pip install --upgrade pip --break-system-packages || true
# Install PyTorch (CPU version is optimized with OpenMP on Snapdragon ARM64)
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu --break-system-packages --ignore-installed
# --ignore-installed: apt (via calibre) ships a debian-built numpy without
# a pip RECORD file - pip cannot uninstall it and hard-fails the whole
# line ("Cannot uninstall numpy... installed by debian", found live on
# the ubuntu-test staging run). Overlay-install instead of uninstalling.
pip install marker-pdf pydantic transformers manga-ocr mokuro pytesseract stress-uk num2words --break-system-packages --ignore-installed

echo "=== [Ubuntu Setup Completed] ==="
EOF
} > "$UBUNTU_SETUP_SCRIPT_PATH"

# Move setup script into the PRoot container space and run it.
# P1.1 SIGTERM-test round 2 finding: bash DEFERS trap execution until the
# current FOREGROUND child exits - so a TERM during this (long, dpkg-lock-
# holding) step used to sit queued while launcher->proot->apt/pip ran on,
# and the group-kill in the trap fired far too late to matter. Running the
# step in the background and `wait`ing is the fix: wait is interruptible,
# the trap runs immediately and its group-kill actually reaches the
# children. (`|| true` on wait: it returns the child's status or 143 on
# signal - set -e must not eat the normal path; failures of setup.sh
# itself surface via UBUNTU_SETUP_STATUS below.)
log "Copying setup script to Ubuntu container and running it..."
"$LAUNCHER_PATH" -- bash -c "cat $UBUNTU_SETUP_SCRIPT_PATH > /tmp/setup.sh && chmod +x /tmp/setup.sh && /tmp/setup.sh" &
UBUNTU_SETUP_PID=$!
UBUNTU_SETUP_STATUS=0
wait "$UBUNTU_SETUP_PID" || UBUNTU_SETUP_STATUS=$?
if [ "$UBUNTU_SETUP_STATUS" -ne 0 ]; then
    error "Ubuntu container setup failed (exit $UBUNTU_SETUP_STATUS) - see output above."
fi
rm -f "$UBUNTU_SETUP_SCRIPT_PATH"

success "Ubuntu compilation and setup finished successfully."

# -------------------------------------------------------------
# STEP 5: Configure Autostart (Optional)
# -------------------------------------------------------------
# TASK-43: all four autostart steps (sshd, llama-server, Flask, and the
# TASK-41 auto-resume-interrupted-conversion check) now live in ONE shared
# script (bin/start-all-services.sh, part of this repo - a fresh clone
# already has it via STEP 3 above) instead of being inlined separately
# into ~/.bashrc. This is called from two independent triggers:
#   - ~/.bashrc, on every new Termux shell session (covers "user manually
#     reopens the Termux app after a crash" - confirmed working live in
#     production: a real Termux crash mid-conversion, on manual restart
#     the interrupted book resumed with zero manual steps)
#   - ~/.termux/boot/start-services.sh (STEP 5b below), on a genuine
#     Android device boot - requires the separate Termux:Boot plugin app,
#     which this script cannot install for you (Android doesn't allow
#     silent APK installation even from Termux) - see the printed
#     instructions in the final summary and
#     docs/deployment/termux-boot-setup.md for the manual step.
if [ "$AUTOSTART" = "true" ]; then
    log "Configuring autostart of services in ~/.bashrc..."

    chmod +x "$HOME/kindle-butch-gen/bin/start-all-services.sh" 2>/dev/null || true

    AUTOSTART_BLOCK=$(cat << 'EOF'

# ── Autostart Services (kindle-butch-gen) ────────────────────────
# See bin/start-all-services.sh for the actual steps - kept as a single
# shared script so ~/.bashrc and ~/.termux/boot/start-services.sh (see
# docs/deployment/termux-boot-setup.md) can't drift out of sync.
bash "$HOME/kindle-butch-gen/bin/start-all-services.sh"
EOF
)

    BASHRC_FILE="$HOME/.bashrc"
    if [ -f "$BASHRC_FILE" ] && grep -q "start-all-services.sh" "$BASHRC_FILE"; then
        log "Autostart is already configured in ~/.bashrc."
    else
        echo "$AUTOSTART_BLOCK" >> "$BASHRC_FILE"
        success "Autostart configured successfully in ~/.bashrc."
    fi

    # -------------------------------------------------------------
    # STEP 5b: Termux:Boot integration (device-reboot autostart)
    # -------------------------------------------------------------
    # ~/.bashrc only fires when a NEW SHELL SESSION starts (i.e. the user
    # manually opens/reopens the Termux app) - it does NOT run on a plain
    # Android device reboot unless the user happens to open Termux
    # afterward. Termux:Boot (a separate, official Termux plugin app) is
    # the only way to get genuine device-boot automation: it runs every
    # executable script under ~/.termux/boot/ on Android's BOOT_COMPLETED
    # broadcast. This step always creates/updates the boot script (safe,
    # additive, works even if the plugin isn't installed yet) - it simply
    # has no effect until the user separately installs the plugin app.
    log "Configuring Termux:Boot integration (requires a separate plugin app - see instructions below)..."
    mkdir -p "$HOME/.termux/boot"
    cat > "$HOME/.termux/boot/start-services.sh" << 'EOF'
#!/data/data/com.termux/files/usr/bin/bash
# Auto-generated by kindle-butch-gen's deploy.sh - runs on Android device
# boot IF the Termux:Boot plugin app is installed. Delegates to the same
# shared script ~/.bashrc uses, so both triggers always stay in sync.
termux-wake-lock 2>/dev/null || true
bash "$HOME/kindle-butch-gen/bin/start-all-services.sh"
EOF
    chmod +x "$HOME/.termux/boot/start-services.sh"
    success "Termux:Boot script written to ~/.termux/boot/start-services.sh."
fi

# -------------------------------------------------------------
# STEP 5c (TASK-60): deterministic, clearly-printed web password.
# Previously kbg_web/app.py silently auto-generated the password on
# Flask's OWN first start - a separate process in a LATER shell session
# (autostart) - and printed it only into ~/kbg-flask.log, never into
# this script's own output. A non-specialist finishing the one-curl
# install had no clear way to find it (Q, 2026-07-17: "треба в самому
# кінці щоб був, чітко"). deploy.sh now owns generation, exports it via
# KBG_WEB_PASSWORD in ~/.bashrc (already the env var app.py prioritizes
# over its own auto-generated file - see kbg_web/app.py:61), and prints
# it in the final banner. Idempotent: a re-run reads the ALREADY-SET
# password back out instead of silently rotating it - important since
# tonight's own testing needed several re-runs of this exact script.
# -------------------------------------------------------------
BASHRC_FILE="$HOME/.bashrc"
if grep -q "^export KBG_WEB_PASSWORD=" "$BASHRC_FILE" 2>/dev/null; then
    WEB_PASSWORD=$(grep "^export KBG_WEB_PASSWORD=" "$BASHRC_FILE" | tail -1 | sed -E "s/^export KBG_WEB_PASSWORD=['\"]?//; s/['\"]?\$//")
    log "Веб-пароль уже налаштований (не змінюємо)."
else
    WEB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(12))")
    # TASK-63: must land BEFORE the autostart trigger line written earlier
    # in this file (STEP 5 above), not after it - "bash
    # start-all-services.sh" spawns Flask as a child process that inherits
    # the environment AS OF THAT LINE, so an export appended below it is
    # invisible to Flask on every single autostart. Confirmed live: this
    # exact ordering bug locked a real user out after every Termux
    # restart, falling back to a random unprintable temporary password.
    # Prepending (not appending) guarantees correct order regardless of
    # what STEP 5 already wrote.
    TMP_BASHRC="$(mktemp)"
    printf 'export KBG_WEB_PASSWORD='"'"'%s'"'"'\n\n' "$WEB_PASSWORD" > "$TMP_BASHRC"
    cat "$BASHRC_FILE" >> "$TMP_BASHRC" 2>/dev/null
    mv "$TMP_BASHRC" "$BASHRC_FILE"
    success "Згенеровано пароль веб-інтерфейсу."
fi

# -------------------------------------------------------------
# STEP 6: Check and Download Required Models (Interactive & Verified)
# -------------------------------------------------------------
log "Checking required models for the translation and TTS pipeline..."

# Helper function to download file with resume and size checks
check_and_download() {
    local label="$1"
    local file_path="$2"
    local url="$3"
    local expected_size="$4"
    
    local dir_path=$(dirname "$file_path")
    mkdir -p "$dir_path"
    
    if [ -f "$file_path" ]; then
        local actual_size=$(stat -c%s "$file_path" 2>/dev/null || stat -f%z "$file_path" 2>/dev/null || echo 0)
        if [ "$actual_size" -eq "$expected_size" ]; then
            success "$label is already present and verified ($actual_size bytes)."
            return 0
        else
            log "$label file size mismatch (found $actual_size, expected $expected_size). Redownloading..."
        fi
    fi
    
    log "Downloading $label..."
    while true; do
        # Use curl -C - to resume, -L to follow redirects, --progress-bar for user-friendly output
        if curl -L -C - --progress-bar -o "$file_path" "$url"; then
            local actual_size=$(stat -c%s "$file_path" 2>/dev/null || stat -f%z "$file_path" 2>/dev/null || echo 0)
            if [ "$actual_size" -eq "$expected_size" ]; then
                success "$label downloaded and verified successfully ($actual_size bytes)."
                return 0
            else
                echo -e "${RED}[ERROR]${NC} Download of $label was incomplete (got $actual_size bytes, expected $expected_size)."
            fi
        else
            echo -e "${RED}[ERROR]${NC} curl command failed during download of $label."
        fi
        
        echo -n -e "${BLUE}[DEPL]${NC} Do you want to resume/retry the download? (Y/n): "
        read -r retry_choice || retry_choice=""   # EOF-safe: set -e must not kill a non-interactive run
        case "$retry_choice" in
            [nN]|[nN][oO])
                log "Download aborted by user."
                return 1
                ;;
            *)
                log "Retrying/resuming download..."
                ;;
        esac
    done
}

# 1. Check/Download Translation Model (Hy-MT2-7B GGUF)
MODEL_DIR="$HOME/models/hy-mt2"
MODEL_PATH="$MODEL_DIR/Hy-MT2-7B-Q4_K_M.gguf"
HY_MT2_SIZE=4624650016
HY_MT2_URL="https://huggingface.co/mradermacher/Hy-MT2-7B-i1-GGUF/resolve/main/Hy-MT2-7B.i1-Q4_K_M.gguf"

if [ -f "$MODEL_PATH" ]; then
    # Double check size of existing GGUF model
    actual_gguf_size=$(stat -c%s "$MODEL_PATH" 2>/dev/null || stat -f%z "$MODEL_PATH" 2>/dev/null || echo 0)
    if [ "$actual_gguf_size" -eq "$HY_MT2_SIZE" ]; then
        success "Translation model Hy-MT2-7B-Q4_K_M.gguf is already present and verified."
    else
        # TASK-32 hardware-test fix: a size mismatch here does NOT prove
        # corruption - it can simply be a different (perfectly working)
        # build of the same model; the expected size only describes the
        # CURRENT default download URL. The previous code did `rm -f`
        # unconditionally BEFORE any user consent, and non-interactively
        # (stdin closed -> read returns EOF -> download skipped) that
        # deleted a working 4.4GB production model with nothing to
        # replace it. Never delete before explicit consent; EOF/default
        # keeps the file.
        log "Translation model at $MODEL_PATH is $actual_gguf_size bytes; the current default download is $HY_MT2_SIZE bytes."
        echo -n -e "${BLUE}[DEPL]${NC} Keep the existing model (recommended if translation works)? (Y/n): "
        read -r keep_choice || keep_choice=""
        case "$keep_choice" in
            [nN]|[nN][oO])
                log "Removing existing model at user's request; the download prompt follows."
                rm -f "$MODEL_PATH"
                ;;
            *)
                success "Keeping the existing translation model as-is."
                ;;
        esac
    fi
fi

if [ ! -f "$MODEL_PATH" ]; then
    echo -e "\n${BLUE}[DEPL]${NC} Translation model Hy-MT2-7B-Q4_K_M.gguf (4.4GB) is missing."
    echo "This model is required for translating book texts."
    echo "Please choose an option:"
    echo "  1) Download the default model from Hugging Face (~4.4GB)"
    echo "  2) Paste a custom download link"
    echo "  3) Skip downloading for now"
    echo -n -e "${BLUE}[DEPL]${NC} Enter choice [1-3]: "
    read -r model_choice || model_choice=""   # EOF-safe: set -e must not kill a non-interactive run
    
    case "$model_choice" in
        1)
            check_and_download "Hy-MT2-7B GGUF Model" "$MODEL_PATH" "$HY_MT2_URL" "$HY_MT2_SIZE"
            ;;
        2)
            echo -n -e "${BLUE}[DEPL]${NC} Please paste the direct download URL for the GGUF model: "
            read -r custom_url || custom_url=""   # EOF-safe: set -e must not kill a non-interactive run
            if [ -n "$custom_url" ]; then
                log "Downloading model from custom URL..."
                curl -L -C - --progress-bar -o "$MODEL_PATH" "$custom_url"
                success "Model downloaded and saved to $MODEL_PATH."
            else
                log "Custom URL was empty. Skipping model download."
            fi
            ;;
        *)
            log "Model download skipped. You will need to manually place the model at $MODEL_PATH."
            ;;
    esac
fi

# 2. Check/Download Supertonic 3 TTS Model
TTS_DIR="$HOME/kindle-butch-gen/models"
TTS_ARCHIVE="$TTS_DIR/sherpa-onnx-supertonic-3-tts-int8-2026-05-11.tar.bz2"
TTS_EXTRACTED_DIR="$TTS_DIR/sherpa-onnx-supertonic-3-tts-int8-2026-05-11"
TTS_SIZE=128774318
TTS_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/tts-models/sherpa-onnx-supertonic-3-tts-int8-2026-05-11.tar.bz2"

if [ -d "$TTS_EXTRACTED_DIR" ] && [ -f "$TTS_EXTRACTED_DIR/vocoder.int8.onnx" ]; then
    success "Supertonic 3 TTS models are already present at $TTS_EXTRACTED_DIR."
else
    echo -e "\n${BLUE}[DEPL]${NC} Supertonic 3 TTS model directory is missing or incomplete."
    echo "This is the premium default TTS voice model required for audiobook synthesis."
    echo -n -e "${BLUE}[DEPL]${NC} Do you want to download and extract Supertonic 3 TTS model? (Y/n): "
    read -r tts_choice || tts_choice=""   # EOF-safe: set -e must not kill a non-interactive run
    case "$tts_choice" in
        [nN]|[nN][oO])
            log "Supertonic 3 TTS model download skipped."
            ;;
        *)
            if check_and_download "Supertonic 3 TTS Archive" "$TTS_ARCHIVE" "$TTS_URL" "$TTS_SIZE"; then
                log "Extracting Supertonic 3 model archive..."
                tar -xf "$TTS_ARCHIVE" -C "$TTS_DIR"
                if [ -d "$TTS_EXTRACTED_DIR" ]; then
                    success "Supertonic 3 TTS models extracted successfully."
                    rm -f "$TTS_ARCHIVE"
                else
                    error "Extraction failed. Directory $TTS_EXTRACTED_DIR was not created."
                fi
            fi
            ;;
    esac
fi

# 3. Check/Download StyleTTS2 Ukrainian voice (optional second TTS engine)
# Gap found 2026-07-17: audio_stage.py/tts_helper.py reference
# models/styletts2/{model.onnx,style.npy} but nothing provisioned them on a
# fresh device (the originals were converted locally once). Published as a
# GitHub release asset of this repo; same size-verified resumable download
# as the other models. Optional: a failure here must not sink the deploy -
# supertonic remains the default engine.
ST2_DIR="$HOME/kindle-butch-gen/models/styletts2"
ST2_BASE="https://github.com/maxfraieho/kindle-butch-gen/releases/download/models-styletts2-uk-v1"
if [ -f "$ST2_DIR/model.onnx" ] && [ -f "$ST2_DIR/style.npy" ]; then
    success "StyleTTS2 Ukrainian voice already present at $ST2_DIR."
else
    log "Downloading StyleTTS2 Ukrainian voice (~313MB, optional second TTS engine)..."
    if check_and_download "StyleTTS2 model.onnx" "$ST2_DIR/model.onnx" "$ST2_BASE/model.onnx" 327779582 \
       && check_and_download "StyleTTS2 style.npy" "$ST2_DIR/style.npy" "$ST2_BASE/style.npy" 1152; then
        success "StyleTTS2 Ukrainian voice installed."
    else
        log "StyleTTS2 voice not installed - engine 'styletts2' will be unavailable (supertonic default works)."
    fi
fi

log "Deployment complete!"
echo -e "\n${GREEN}===================================================================${NC}"
echo -e " kindle-butch-gen is deployed!"
echo -e ""
echo -e " ${GREEN}🔑 Веб-інтерфейс: http://localhost:5000${NC}"
echo -e "    Логін:  ${GREEN}admin${NC}"
echo -e "    Пароль: ${GREEN}${WEB_PASSWORD}${NC}"
echo -e "    (збережено в ~/.bashrc; щоб змінити - відредагуйте рядок"
echo -e "    export KBG_WEB_PASSWORD='...' і перезапустіть Termux)"
if [ "$ADRENO_DETECTED" = "true" ]; then
    echo -e " To enter the GPU-enabled Ubuntu environment, run:"
    echo -e "   👉 ${LAUNCHER_PATH}"
    echo -e " To test Adreno OpenCL acceleration, run inside Ubuntu:"
    echo -e "   👉 clinfo"
    echo -e " To run translation server accelerated by Adreno GPU, run:"
    echo -e "   👉 llama-server -m <model_path> -c 2048 --port 8081 -ngl 99"
else
    echo -e " No Adreno GPU was detected - built CPU-only. To enter Ubuntu, run:"
    echo -e "   👉 ${LAUNCHER_PATH}"
    echo -e " To run the translation server (CPU-only), run:"
    echo -e "   👉 llama-server -m <model_path> -c 2048 --port 8081"
fi
if [ "$AUTOSTART" = "true" ]; then
    echo -e ""
    echo -e " TASK-43: autostart is configured for ~/.bashrc (fires when you"
    echo -e " manually reopen Termux after a crash) - already sufficient for"
    echo -e " services + auto-resuming an interrupted conversion. For it to ALSO"
    echo -e " survive a genuine Android device reboot without you opening Termux"
    echo -e " yourself, install the separate Termux:Boot plugin app (one-time,"
    echo -e " manual - this script cannot install an APK for you):"
    echo -e "   👉 F-Droid: https://f-droid.org/packages/com.termux.boot/"
    echo -e "   👉 Same source as Termux itself (F-Droid or GitHub releases) -"
    echo -e "      installing it from a different source than your Termux app"
    echo -e "      itself is known to be unreliable."
    echo -e "   After installing, open Termux:Boot once (grants it the"
    echo -e "   RECEIVE_BOOT_COMPLETED permission) - the boot script at"
    echo -e "   ~/.termux/boot/start-services.sh is already in place and will"
    echo -e "   run automatically on every future device boot."
    echo -e "   Full details: docs/deployment/termux-boot-setup.md"
fi
echo -e "${GREEN}===================================================================${NC}\n"
