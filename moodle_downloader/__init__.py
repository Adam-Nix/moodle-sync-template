"""Download course files from TUS Moodle into this repo.

    python main.py    download everything, then push to the network share
"""

from moodle_downloader.config import Settings, load_settings
from moodle_downloader.downloader import download_all

__all__ = ["Settings", "download_all", "load_settings"]
