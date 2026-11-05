# Moodle Sync (template)

Downloads your current Moodle courses (files, a readme per course, deadlines) into a
folder on your PC. That is all it does by default.
If you want, you can add other locations (a network share, OneDrive, a USB drive) and the
downloaded Moodle files are copied there too. Keeping the rest of the folder in step across
locations is a separate option, off unless you switch it on.
Includes a small browser dashboard (`network_sync`) for downloading and managing locations.

## Setup
1. `pip install -r requirements.txt`
2. Run `python -m network_sync.ui` (or double-click `network_sync/sync-ui.cmd`). The dashboard opens in
   your browser and walks you through **Settings**: your Moodle address and key, where to keep files,
   which Moodle category is this year, an optional semester end date, and any course websites.
   Nothing needs editing by hand.
3. Press **Download** to fetch your files. Optional: use the location icon to add USB drives, OneDrive
   or a network share to keep extra copies of the Moodle files in. None are set up by default.
4. Optional: `python -m network_sync.install_shortcut` adds a **Start-menu** entry on Windows or an
   **applications-menu** entry on Linux; then pin or favourite it.

**Windows and Linux** both work (Python 3.10+). On Linux you can also run `network_sync/sync-ui.sh`.
A network share must be mounted first (e.g. `/mnt/server`); then add that folder under Locations.
For printing slides on Linux, `python -m playwright install chromium` may also need `--with-deps`.

Prefer the terminal? `python main.py` asks the same questions, and `config.example.ini` shows every option.

`config.ini`, `secrets.ini`, `_data/` and downloaded course files are git-ignored - never commit them.

## Good to know
- Settings are saved in `config.ini` (and website logins in `secrets.ini`); both stay on your PC and are git-ignored.
- OneDrive is offered under Locations but is never used until you add it.
- Only the `materials` folder (the Moodle downloads) is copied to your locations. To also keep every
  other file in the course folder identical across locations, set `sync_other_files = true` under
  `[Network]` in `config.ini`.
- Playwright and Chromium (for printing slides from course websites) install themselves the first time they're needed.
