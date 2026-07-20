#!/usr/bin/env bash
# Run ONCE inside a fresh Ubuntu 24.04 VM to build the golden image.
# Afterwards: reboot, power off, and take a VMware snapshot named "clean".
#
# This installs OS-level dependencies (sudo is fine here -- this is a human
# building the base image, NOT the agentic-hil install under test) and a
# prebuilt firmware fixture, but DELIBERATELY does NOT install uv / pipx /
# agentic-hil. Those are the subject of the repeatable install test.
set -euo pipefail

echo "== apt dependencies"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  ca-certificates curl git python3 python3-venv \
  openocd socat usbutils \
  cmake ninja-build gcc-arm-none-eabi \
  open-vm-tools

# Node + Claude Code: only needed for the optional agent-eval layer (Schicht C).
# Harmless for the deterministic layer; comment out if you never run the agent path.
echo "== node + claude-code (optional agent-eval layer)"
if ! command -v node >/dev/null 2>&1; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y nodejs
fi
sudo npm install -g @anthropic-ai/claude-code || true

# ST-Link access without root. OpenOCD ships udev rules; put the user in the
# serial/usb groups (takes effect after the reboot before the snapshot).
echo "== usb/serial group membership"
sudo usermod -aG dialout,plugdev "$USER" || true

# STM32CubeProgrammer CLI (ST's standard tool) enables the second, stlink backend
# variant. ST gates the download behind a login, so it CANNOT be apt-installed or
# auto-downloaded here. To enable the stlink variant, install it manually into
# this image before snapshotting:
#   sudo apt-get install -y default-jre
#   unzip en.stm32cubeprg-lin-*.zip -d /tmp/cubeprog          # ST's Linux package
#   /tmp/cubeprog/SetupSTM32CubeProgrammer-*.linux -console   # or an auto-install.xml
# run-all.sh auto-detects it at:
#   ~/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI
# If it is not present at eval time the stlink variant is cleanly skipped and the
# OpenOCD variant still runs.
echo "== STM32CubeProgrammer CLI (stlink backend, optional)"
if command -v STM32_Programmer_CLI >/dev/null 2>&1 || [ -x "$HOME/STMicroelectronics/STM32Cube/STM32CubeProgrammer/bin/STM32_Programmer_CLI" ]; then
  echo "STM32CubeProgrammer CLI detected -- stlink variant will run."
else
  echo "STM32CubeProgrammer CLI NOT installed -- stlink variant will be skipped until you install it."
  echo "Installing a JRE now so a later headless CubeProgrammer install works:"
  sudo apt-get install -y --no-install-recommends default-jre || true
fi

# Fixture: the standalone Nucleo demo firmware project. We clone the repo only to
# copy the demo subtree + the software NTC adapter -- the agentic-hil source tree
# itself is NOT vendored into the fixture (that is one of the things assert.sh checks).
echo "== fixture firmware project"
rm -rf "$HOME/fixture" /tmp/ahil-src
git clone --depth 1 https://github.com/agentic-hil/agentic-hil /tmp/ahil-src
cp -r /tmp/ahil-src/examples/nucleo-f446re_demo "$HOME/fixture"
mkdir -p "$HOME/fixture/tools"
cp /tmp/ahil-src/examples/adapters/sim_ntc_adapter.py "$HOME/fixture/tools/"
chmod +x "$HOME/fixture/tools/sim_ntc_adapter.py"
rm -rf /tmp/ahil-src

# Prebuild the firmware so every eval run flashes a byte-identical artifact.
echo "== prebuild firmware"
cd "$HOME/fixture"
cmake --preset Debug
cmake --build --preset Debug
test -f build/Debug/nucleo-f446re_demo.elf

echo
echo "Golden provisioning done."
echo "Next steps:"
echo "  1. sudo reboot            # picks up dialout/plugdev membership"
echo "  2. Connect the Nucleo-F446RE (ST-Link) and, in VMware, VM > Removable Devices,"
echo "     set the ST-Link to auto-connect to this VM."
echo "  3. Power off the VM."
echo "  4. Take a snapshot named 'clean' (with the ST-Link device attached)."
