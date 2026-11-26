"""Resolve the deployment root before reading its runtime configuration."""
import os
from pathlib import Path


def resolve_root():
    # Keep the lookup order in sync with runtime-root.sh.
    if root := os.environ.get("FERTILE_ROOT"):
        return Path(root)
    if box := os.environ.get("FERTILE_BOX_HOME"):
        return Path(box) / "fertile"
    if os.environ.get("FERTILE_PROFILE") == "stable":
        return Path("/var/lib/fertile")
    return Path("/home/box/fertile")
