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


def _windows_git_candidates() -> list[Path]:
    """Where Git for Windows lands: machine-wide, or per user in a profile."""
    roots = [os.environ.get(v) for v in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")]
    found = [Path(r) / "Git" / "cmd" / "git.exe" for r in roots if r]
    found.append(Path(r"C:\Program Files\Git\cmd\git.exe"))
    # A per-user install (the winget / installer default without admin) is on
    # that user's PATH only. Scheduled tasks run as SYSTEM and cannot see it.
    users = Path(os.environ.get("SYSTEMDRIVE", "C:") + "\\") / "Users"
    found += sorted(users.glob("*/AppData/Local/Programs/Git/cmd/git.exe"))
    return found


def find_git(configured: str = "") -> str:
    """The git executable: publish.git_binary, PATH, then the usual install dirs."""
    if configured:
        return configured
    on_path = shutil.which("git")
    if on_path:
        return on_path
    if IS_WINDOWS:
        for candidate in _windows_git_candidates():
            if candidate.is_file():
                return str(candidate)
    return "git"  # let the error name it
