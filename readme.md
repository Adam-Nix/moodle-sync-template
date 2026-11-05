# Moodle Sync (template)

Downloads your current Moodle courses (files, a readme per course, deadlines) into a
folder, then keeps that folder identical across your PC, a network share and OneDrive.
Includes a small browser dashboard (`network_sync`) for syncing and resolving conflicts.

## Setup
1. `pip install -r requirements.txt`
2. Run `python -m network_sync.ui` (or double-click `network_sync/sync-ui.cmd`). The dashboard opens in
   your browser and walks you through **Settings**: your Moodle address and key, where to keep files,
   which Moodle category is this year, an optional semester end date, and any course websites.
   Nothing needs editing by hand.
3. Press **Download** to fetch your files. Use the location icon to add USB drives, OneDrive or a
   network share to keep copies in.
4. Optional: `network_sync/install_shortcut.py` adds a Start-menu shortcut.

Prefer the terminal? `python main.py` asks the same questions, and `config.example.ini` shows every option.

`config.ini`, `secrets.ini`, `_data/` and downloaded course files are git-ignored - never commit them.

## Good to know
- Settings are saved in `config.ini` (and website logins in `secrets.ini`); both stay on your PC and are git-ignored.
- OneDrive is detected automatically and offered under Locations.
- Playwright and Chromium (for printing slides from course websites) install themselves the first time they're needed.
