"""Where things live on each supported OS.

Linux (Raspberry Pi OS, Debian, Ubuntu, NVIDIA Jetson / Tegra) follows the FHS:
config in /etc, state in /var/lib, code in /opt (or an --in-place checkout).

Windows has no such layout, so everything machine-wide goes under
%ProgramData%\\broadband-monitor — the conventional home for service state that
is not tied to one user profile, and readable by the SYSTEM account that the
scheduled tasks run as.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"

# The checkout this package was imported from.
REPO_ROOT = Path(__file__).resolve().parent.parent

if IS_WINDOWS:
    _BASE = Path(os.environ.get("PROGRAMDATA") or r"C:\ProgramData") / "broadband-monitor"
    CONF_DIR = _BASE
    DATA_DIR = _BASE / "data"
    APP_DIR = REPO_ROOT
else:
    CONF_DIR = Path("/etc/broadband-monitor")
    DATA_DIR = Path("/var/lib/broadband-monitor")
    APP_DIR = Path("/opt/broadband-monitor")

CONFIG_FILE = CONF_DIR / "config.yaml"


def find_binary(name: str) -> str | None:
    """Resolve an executable on PATH, then next to the running interpreter.

    The second lookup matters for anything pip installs into the virtualenv
    (speedtest-cli on Windows, for one): the scheduler starts the venv's python
    directly, so the venv's bin/ or Scripts/ directory is never on PATH.
    """
    if os.path.dirname(name):  # an explicit path in the config
        return shutil.which(name)
    return shutil.which(name) or shutil.which(name, path=str(Path(sys.executable).parent))
