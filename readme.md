# Moodle Sync (template)

Downloads your current Moodle courses (files, a readme per course, deadlines) into a
folder, then keeps that folder identical across your PC, a network share and OneDrive.
Includes a small browser dashboard (`network_sync`) for syncing and resolving conflicts.

## Setup
1. `pip install -r requirements.txt`
2. Copy `config.example.ini` to `config.ini` and fill in your Moodle address, token
   (Moodle -> Preferences -> Security keys) and folders. Without one, the first run asks.
3. Optional, for course websites with slides: copy `secrets.example.ini` to `secrets.ini`,
   then `python -m playwright install chromium`.
4. Run `python main.py` to download and sync, or `python -m network_sync.ui` for the dashboard
   (`network_sync/install_shortcut.py` adds a Start-menu shortcut).

`config.ini`, `secrets.ini`, `_data/` and downloaded course files are git-ignored - never commit them.

## Customising
- The first run asks for your Moodle login, the folder to use and which Moodle category is
  the current year; the answers are saved in `config.ini` (`[Folders]`, `[Courses]`).
- OneDrive is detected from your environment and used automatically; set `onedrive =` blank to turn it off.
- Playwright and Chromium are installed the first time web-page slide printing runs.
