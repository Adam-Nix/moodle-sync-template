#!/bin/sh
# Open the sync dashboard (Linux/macOS). Make it runnable once with:  chmod +x network_sync/sync-ui.sh
cd "$(dirname "$0")/.." || exit 1
exec python3 -m network_sync.ui
