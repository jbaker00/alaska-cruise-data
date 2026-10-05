#!/usr/bin/env bash
# Install the cruise schedule watcher as a systemd user timer on the Pi.
# Run from a push-able clone of alaska-cruise-data on the Pi:  ./pipeline/pi5/install.sh
set -euo pipefail

PIPELINE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$HOME/.local/share/cruise-watcher/venv"
ENV_FILE="$HOME/.config/cruise-watcher.env"
UNIT_DIR="$HOME/.config/systemd/user"

echo "→ Python venv at $VENV"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -r "$PIPELINE_DIR/requirements.txt"

if [[ ! -f "$ENV_FILE" ]]; then
  mkdir -p "$(dirname "$ENV_FILE")"
  cat > "$ENV_FILE" <<CONF
# Optional: ntfy.sh topic or full URL for phone notifications
NTFY_TOPIC=
CONF
  echo "→ Wrote $ENV_FILE (optional: set NTFY_TOPIC for phone alerts)"
fi

mkdir -p "$UNIT_DIR"
for unit in cruise-watcher cruise-verify; do
  sed -e "s#@PIPELINE_DIR@#$PIPELINE_DIR#g" -e "s#@REPO_DIR@#$(dirname "$PIPELINE_DIR")#g" \
    "$PIPELINE_DIR/pi5/$unit.service" > "$UNIT_DIR/$unit.service"
  cp "$PIPELINE_DIR/pi5/$unit.timer" "$UNIT_DIR/"
done

# Keep user timers running without an active login session.
sudo loginctl enable-linger "$USER" || echo "  (could not enable linger — timer only runs while logged in)"
systemctl --user daemon-reload
systemctl --user enable --now cruise-watcher.timer cruise-verify.timer
# Long-running live-position proxy (aisstream.io → gs://globalvibes-ship-positions/positions.json).
sed -e "s#@PIPELINE_DIR@#$PIPELINE_DIR#g" -e "s#@REPO_DIR@#$(dirname "$PIPELINE_DIR")#g" \
  "$PIPELINE_DIR/pi5/cruise-tracker.service" > "$UNIT_DIR/cruise-tracker.service"
systemctl --user daemon-reload
systemctl --user enable cruise-tracker.service
systemctl --user restart cruise-tracker.service

echo "✓ Installed. Useful commands:"
echo "    systemctl --user start cruise-watcher      # run now"
echo "    journalctl --user -u cruise-watcher -n 50  # logs"
echo "    systemctl --user start cruise-verify       # sailing check slice, now"
echo "    journalctl --user -u cruise-tracker -f     # live ship tracker"
echo "    systemctl --user list-timers 'cruise-*'"
