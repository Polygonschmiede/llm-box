#!/usr/bin/env bash
# ============================================================================
#  One-time system setup  (needs sudo)
#  Run as:   sudo bash setup-system.sh
# ============================================================================
#  Does: packages, GPU access, SSH at boot, services surviving logout and
#        starting at boot (linger), Wake-on-LAN, firewall rules, systemd units.
#
#  Everything that belongs to THIS machine (user, repository path, network
#  adapter, subnet, card numbers) is detected rather than assumed. Overridable:
#     LLM_NIC=eth0         network adapter for Wake-on-LAN
#     LLM_LAN=<subnet>     subnet for the firewall  ('none' = no rules)
#     LLM_BIND=0.0.0.0     make the services reachable on the network
#                          (default 127.0.0.1, i.e. local only - see SECURITY.md)
#     LLM_BACKEND=vulkan   install the Vulkan build dependencies instead of
#                          expecting ROCm (default: whichever is already there)
#
#  Pass them THROUGH sudo - it resets the environment, so setting them in front
#  of 'sudo' has no effect here:
#     sudo env LLM_BIND=0.0.0.0 bash setup-system.sh
# ============================================================================
set -e
if [ "$(id -u)" -ne 0 ]; then
  echo "Please run with sudo:  sudo bash setup-system.sh"; exit 1
fi
U="${SUDO_USER:?Run this with 'sudo bash setup-system.sh', not as a root login}"
H="$(getent passwd "$U" | cut -d: -f6)"
[ -n "$H" ] || { echo "No home directory found for '$U'."; exit 1; }
#  The repository is wherever this script is - not necessarily ~/llm.
SRC="$(cd -- "$(dirname -- "$(readlink -f -- "$0")")" && pwd)"
RUN_AS() { sudo -u "$U" XDG_RUNTIME_DIR="/run/user/$(id -u "$U")" "$@"; }

echo ">>> Setting up for user: $U"
echo ">>> Repository: $SRC"

echo ">>> 1/8  Installing packages"
#  Refreshing the lists is NOT worth aborting the whole run for: Ubuntu's daily
#  apt timer holds the lock now and then, and 'set -e' would otherwise stop here
#  before a single unit is installed. The install below is the step that matters,
#  and it says clearly what went wrong.
if ! apt-get update -qq; then
  echo "    could not refresh the package lists (another apt process, or no network)."
  echo "    Continuing - the packages below may already be installed."
fi
#  For building llama.cpp/whisper.cpp and for the registry. ROCm itself is
#  deliberately NOT installed here: several GB, and a different path per
#  distribution and card generation - see the check in step 2.
if ! apt-get install -y build-essential cmake git curl ca-certificates pkg-config \
                        libcurl4-openssl-dev jq ethtool wakeonlan procps; then
  echo "    Installing the packages failed."
  echo "    If it mentions a lock: another apt process is running (often the daily"
  echo "    timer). Wait for it and run this script again - it is repeatable."
  exit 1
fi

#  Two ways to run models on a GPU here, and this step only reports on the one
#  that applies. ROCm is the faster path on supported AMD cards but is several GB
#  with a different path per distribution and card generation, so it is
#  deliberately not installed automatically. Vulkan works on anything with a
#  Vulkan driver - including AMD cards ROCm does not support, Intel and NVIDIA -
#  and its build dependencies are two small distribution packages, so those ARE
#  installed when that is the chosen backend.
#
#  Chosen how: LLM_BACKEND if set, otherwise whichever is already present, with
#  ROCm winning when both are. 'llm init --backend' records the decision.
echo ">>> 2/8  Checking the compute backend"
BACKEND="${LLM_BACKEND:-}"
if [ -z "$BACKEND" ]; then
  if command -v rocm-smi >/dev/null && command -v hipcc >/dev/null; then BACKEND=rocm
  elif command -v vulkaninfo >/dev/null; then BACKEND=vulkan
  else BACKEND=rocm; fi
fi
echo "    backend: $BACKEND   (override with  sudo env LLM_BACKEND=... bash setup-system.sh)"
if [ "$BACKEND" = vulkan ]; then
  #  glslc compiles the shaders, libvulkan-dev has the headers and the link-time
  #  library, spirv-headers the cmake config ggml's find_package looks for, and
  #  vulkan-tools brings vulkaninfo - which is what the card detection reads.
  #  Determined by building it: leaving any of the three out fails at configure.
  if ! apt-get install -y glslc libvulkan-dev spirv-headers vulkan-tools; then
    echo "    Installing the Vulkan build dependencies failed."
    echo "    By hand:  sudo apt-get install glslc libvulkan-dev spirv-headers vulkan-tools"
  fi
  if command -v vulkaninfo >/dev/null && vulkaninfo --summary 2>/dev/null | grep -q "deviceName"; then
    echo "    ok: $(command -v vulkaninfo) reports a device"
  else
    echo "    WARNING: vulkaninfo reports no device."
    echo "    A driver is still needed: mesa on AMD and Intel (mesa-vulkan-drivers),"
    echo "    the proprietary one on NVIDIA. Without it nothing runs on the GPU."
  fi
else
  missing=""
  for t in rocm-smi hipcc; do command -v "$t" >/dev/null || missing="$missing $t"; done
  if [ -n "$missing" ]; then
    echo "    MISSING:$missing"
    echo "    On Ubuntu usually:  sudo apt-get install rocm-smi rocminfo hipcc rocm-device-libs"
    echo "    Otherwise follow AMD's guide: https://rocm.docs.amd.com/"
    echo "    Nothing runs on the GPU without ROCm - install it, then continue here."
    echo "    Or use Vulkan instead, which needs none of it:"
    echo "      sudo env LLM_BACKEND=vulkan bash setup-system.sh"
  else
    echo "    ok: $(command -v rocm-smi), $(command -v hipcc)"
  fi
fi

echo ">>> 3/8  GPU access (groups render, video)"
usermod -aG render,video "$U"

echo ">>> 4/8  Enabling SSH at boot"
systemctl enable --now ssh 2>/dev/null || echo "    no ssh service installed - skipped"

echo ">>> 5/8  Letting services run without a login and after boot (linger)"
loginctl enable-linger "$U"

echo ">>> 6/8  Wake-on-LAN"
#  Derive the adapter from the default route instead of guessing a name.
NIC="${LLM_NIC:-$(ip -o -4 route show to default 2>/dev/null | awk '{print $5; exit}')}"
if [ -n "$NIC" ] && [ -e "/sys/class/net/$NIC" ]; then
  install -m 644 "$SRC/systemd/wol@.service" /etc/systemd/system/wol@.service
  systemctl daemon-reload
  systemctl enable --now "wol@$NIC.service"
  ethtool "$NIC" 2>/dev/null | grep -i wake-on || true
  echo "    active for $NIC   (to wake it:  wakeonlan \$(cat /sys/class/net/$NIC/address))"
else
  echo "    no adapter found - skipped (set LLM_NIC=<name> to force one)"
fi

echo ">>> 7/8  Firewall: making the services reachable on your network"
#  Without these rules nothing arrives from another machine - and it fails
#  silently: the service runs, the log says nothing, because the packets never
#  get there. Deliberately your own subnet only, not the whole world.
LAN="${LLM_LAN:-$(ip -o -4 route show dev "${NIC:-lo}" scope link 2>/dev/null | awk '{print $1; exit}')}"
if [ "$LAN" = none ]; then
  echo "    LLM_LAN=none - no firewall rules (services stay reachable locally)"
elif command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "^Status: active"; then
  if [ -n "$LAN" ]; then
    for p in 8080 8081 3000 8188; do
      ufw allow from "$LAN" to any port "$p" proto tcp comment "llm-box" >/dev/null
    done
    echo "    opened for $LAN: 8080 (LLM API), 8081 (registry), 3000 (chat UI), 8188 (ComfyUI)"
  else
    echo "    subnet not detected. If you need it, by hand:"
    echo "      sudo ufw allow from <your-subnet> to any port 8080 proto tcp"
  fi
else
  echo "    ufw not active - nothing to do"
fi

echo ">>> 8/8  Installing the user services and enabling autostart"
#  llama-swap = LLM API (8080), llm-api = registry for agents (8081),
#  open-webui = chat UI (3000). ComfyUI stays on demand (it holds VRAM).
#  The files in systemd/ are templates: @LLM_HOME@ and @LLM_BIND@ are substituted
#  here, so the repository can live anywhere.
#  Loopback by default: nobody accidentally puts a chat interface on the network.
#  Open it up on purpose with LLM_BIND=0.0.0.0.
BIND="${LLM_BIND:-127.0.0.1}"
if [ "$BIND" = 127.0.0.1 ]; then
  echo "    services will listen on $BIND (local only; for the network:"
  echo "      sudo env LLM_BIND=0.0.0.0 bash setup-system.sh   - read SECURITY.md first)"
else
  echo "    services will listen on $BIND"
fi
install -d -o "$U" -g "$U" "$H/.config/systemd/user"
for unit in llama-swap llm-api open-webui comfyui; do
  sed -e "s|@LLM_HOME@|$SRC|g" -e "s|@LLM_BIND@|$BIND|g" "$SRC/systemd/$unit.service" \
    > "$H/.config/systemd/user/$unit.service"
  chown "$U:$U" "$H/.config/systemd/user/$unit.service"
  chmod 644 "$H/.config/systemd/user/$unit.service"
done
RUN_AS systemctl --user daemon-reload 2>/dev/null || true
RUN_AS systemctl --user enable llama-swap llm-api open-webui 2>/dev/null || true

cat <<EOF

============================================================
  DONE with the part that needs sudo.

  -> Please log out and back in ONCE (for the render/video
     groups). After that everything also starts on boot.

  Continue as your normal user:
     $SRC/bin/llm init          # create the configuration from the template
                                #   (--backend rocm|vulkan to force one)
     $SRC/bin/llm setup         # create the Python environments
     $SRC/bin/llm update swap   # fetch the llama-swap binary
     $SRC/bin/llm update llama  # fetch and build llama.cpp
     $SRC/bin/llm doctor        # check the whole chain
     $SRC/bin/llm url           # show the addresses
============================================================
EOF
