#!/usr/bin/env bash
# Run as the normal satellite user; sudo is used only for system changes.
set -euo pipefail
export PATH="$HOME/.local/bin:$PATH"
cd "$(dirname "$0")/.."
mkdir -p logs
exec > >(tee -a logs/install-alerts.log) 2>&1
set -x
sudo apt-get update
sudo apt-get install -y python3 python3-venv pipewire pipewire-pulse wireplumber pulseaudio-utils alsa-utils mpv jq acl curl ca-certificates
sudo usermod -aG audio "$(id -un)"
sudo loginctl enable-linger "$(id -un)"
# Current user manager predates audio group membership. This ACL enables live
# testing now; after reboot audio group membership handles hotplug too.
sudo setfacl -m "u:$(id -un):rw" /dev/snd/*
systemctl --user daemon-reload
systemctl --user enable --now pipewire.socket pipewire-pulse.socket wireplumber.service
systemctl --user restart pipewire pipewire-pulse wireplumber
sleep 3
/usr/bin/python3 scripts/configure.py
bash scripts/install-tts.sh
systemctl --user daemon-reload
systemctl --user enable --now jeeves-alerts.service
systemctl --user is-enabled jeeves-alerts.service
systemctl --user is-active jeeves-alerts.service
