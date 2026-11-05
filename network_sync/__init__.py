"""Keep every copy of the 4th year folder the same: this PC, the home server and OneDrive.

    python -m network_sync             sync everything now (skips anything offline)
    python -m network_sync --dry-run   show what would be copied, move nothing
    python -m network_sync.ui          open the dashboard
"""

from network_sync.sync import check_remote, get_share_path, push, sync_everything

__all__ = ["check_remote", "get_share_path", "push", "sync_everything"]
