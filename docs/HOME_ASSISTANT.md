# Optional HA-managed LVA frontend

The default standalone Hermes frontend and native HA conversation adapter need **none**
of the package/ESPHome steps below; see [backend setup](BACKENDS.md) instead. This guide
is only for intentionally replacing standalone voice with Linux Voice Assistant (LVA),
using HA Assist for the voice pipeline. Server-side changes must be applied by that
server's administrator.

## Install LVA locally

After the audio installer and private alert configuration exist:

```bash
cd "$HOME/voice-assistant"
export PATH="$HOME/.local/bin:$PATH"
bash scripts/install-satellite.sh
systemctl --user status jeeves-satellite --no-pager
```

The installer pins an LVA source revision, creates its Python environment, disables
`jeeves-voice`, and enables LVA. Retain generated `locks/lva-requirements.txt` for
repeat installations. It initially binds all IPv4 interfaces and uses a hostname-based
device name. Adjust `JEEVES_BIND_ADDRESS`/`JEEVES_NAME` in private `satellite.env` to
limit/rename it. Do not enable both microphone frontends at boot.

On the HA server, pair the satellite through **ESPHome**, using your voice machine's
LAN hostname/address and port 6053. Assign its room and an English Assist pipeline
with working Whisper STT, HA conversation and Piper TTS. Record the actual
`assist_satellite.*` entity ID. No fixed entity ID is assumed.

## Enable the alert API for HA access

The standalone default is loopback-only. To allow HA to call the local scheduler,
choose an explicit IPv4 bind address and allow HA's actual source IP. From the repo:

```bash
read -r -p 'HA server source IP: ' HA_SERVER_IP
python3 scripts/configure.py --host 0.0.0.0 --allow-client "$HA_SERVER_IP"
systemctl --user restart jeeves-alerts
```

Use the real source IP visible to the voice host (container routing can differ). Permit
only needed LAN ports in existing firewalls; do not disable the firewall or port-forward
the APIs. Alert HTTP is unencrypted; use a trusted LAN or TLS/VPN boundary.

## Apply the HA package on your server

1. Back up HA configuration. Enable packages if not already enabled by merging this
   into the existing `homeassistant:` mapping in `/config/configuration.yaml`:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

   Do not duplicate an existing key/include. Use the existing package directory if configured.
2. Copy repo `ha/packages/jeeves.yaml` to `/config/packages/jeeves.yaml`.
3. Add these deployment values to HA `/config/secrets.yaml` using your actual values:

   ```yaml
   jeeves_alert_command_url: "http://VOICE_HOST:5124/command"
   jeeves_satellite_entity: "assist_satellite.YOUR_ENTITY_ID"
   jeeves_alert_authorization: "Bearer YOUR_PRIVATE_ALERT_TOKEN"
   ```

   Replace every placeholder. The voice host's private `~/.config/jeeves/ha-secret.yaml`
   provides the authorization entry; transfer it privately, never through chat or git.
   Do not duplicate existing entries. A regenerated alert key requires an HA secret update.
4. Run HA's configuration check and restart HA. Test from the actual paired satellite:
   “set a timer for five seconds”, verify its automation trace and local scheduler record,
   then confirm audible playback. Repeat reminders, alarms, stop and cancel.

Conversation triggers forward the recognized sentence and use `to_json` for safe payload
serialization. Their guard scopes requests to the configured satellite. Typed Assist
requests without that satellite ID are intentionally rejected; use local CLI for API tests.
Verify these triggers win over native timers before relying on persistence. This optional
frontend's HA confirmations are separate from standalone Jeeves's quiet-mode policy.

If selecting an LLM inside this HA-managed pipeline later, enable **Prefer handling
commands locally** and re-test custom triggers. Offline HA prevents new voice requests
in this frontend, but saved local schedules still ring.

## Return to standalone voice

```bash
bash scripts/install-voice.sh
python3 scripts/voicectl.py backend hermes
python3 scripts/voicectl.py health --wait 120
```

References: [LVA installation](https://github.com/OHF-Voice/linux-voice-assistant/blob/main/docs/install_application.md),
[HA sentence triggers](https://www.home-assistant.io/voice_control/custom_sentences/),
[HA REST commands](https://www.home-assistant.io/integrations/rest_command/).
