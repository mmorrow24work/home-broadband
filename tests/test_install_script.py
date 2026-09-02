"""Guards on install.sh.

The installer rewrites the systemd units and config.yaml with `sed`. Those
patterns are invisible coupling: rename a key or reflow a unit and the install
silently stops matching, which is exactly the sort of failure you only find on
the Pi. These tests pin the contract from the other side.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "scripts" / "install.sh"
UNITS = sorted((ROOT / "systemd").glob("*.service")) + sorted((ROOT / "systemd").glob("*.timer"))


def test_install_and_uninstall_are_valid_bash():
    for script in (INSTALL, ROOT / "scripts" / "uninstall.sh"):
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_units_use_the_placeholder_path_the_installer_rewrites():
    """Every absolute reference to the app must be the string install.sh seds."""
    for unit in UNITS:
        text = unit.read_text()
        for line in text.splitlines():
            if line.startswith(("ExecStart=", "WorkingDirectory=")):
                assert "/opt/broadband-monitor" in line, f"{unit.name}: {line}"


def test_installer_rewrite_relocates_every_path(tmp_path):
    """Reproduce the installer's sed and check nothing is left behind."""
    app_dir = "/home/mickm/git/broadband-monitor"
    for unit in UNITS:
        out = tmp_path / unit.name
        subprocess.run(
            [
                "sed",
                "-e", f"s#/opt/broadband-monitor#{app_dir}#g",
                "-e", "s#^ProtectHome=yes$#ProtectHome=no#",
                str(unit),
            ],
            stdout=out.open("w"),
            check=True,
        )
        text = out.read_text()
        assert "/opt/broadband-monitor" not in text, f"{unit.name} still points at /opt"
        if "ProtectHome" in unit.read_text():
            # ProtectHome=yes masks /home entirely, so an in-home install needs it off.
            assert "ProtectHome=no" in text, f"{unit.name} would be unable to read {app_dir}"


def test_service_units_that_need_the_app_dir_declare_it_writable():
    """The publisher runs `git pull` in the app dir, so it cannot be read-only."""
    publish = (ROOT / "systemd" / "broadband-publish.service").read_text()
    read_write = [line for line in publish.splitlines() if line.startswith("ReadWritePaths=")]
    assert read_write, "publish service must declare ReadWritePaths"
    assert "/opt/broadband-monitor" in read_write[0]


def test_config_lines_the_installer_edits_still_match():
    """install.sh rewrites these two lines with anchored patterns."""
    text = (ROOT / "config" / "config.example.yaml").read_text()
    assert re.search(r"^  engines: \[.*\]$", text, re.M), "engines line no longer matches"
    assert re.search(r"^  repo_dir: .*$", text, re.M), "repo_dir line no longer matches"


@pytest.mark.parametrize("engines", ["cloudflare", "speedtest-cli, cloudflare",
                                     "ookla, speedtest-cli, cloudflare"])
def test_engine_rewrite_produces_a_loadable_config(tmp_path, engines):
    """Whatever install.sh detects must still parse and validate afterwards."""
    from collector.config import load_config

    target = tmp_path / "config.yaml"
    target.write_text((ROOT / "config" / "config.example.yaml").read_text())
    subprocess.run(
        ["sed", "-i", rf"s/^  engines: \[.*\]$/  engines: [{engines}]/", str(target)], check=True
    )
    subprocess.run(
        ["sed", "-i", rf"s#^  repo_dir: .*#  repo_dir: \"{tmp_path}\"#", str(target)], check=True
    )

    parsed = yaml.safe_load(target.read_text())
    assert parsed["speed"]["engines"] == [e.strip() for e in engines.split(",")]
    assert parsed["publish"]["repo_dir"] == str(tmp_path)

    cfg = load_config(target)          # full validation, not just YAML parsing
    assert cfg.get("speed.engines") == [e.strip() for e in engines.split(",")]


def test_python_m_invocations_run_from_the_app_dir():
    """`python -m collector.main` resolves the package from the cwd.

    Running it from wherever the installer was invoked fails with
    "No module named 'collector'" — this pins the fix in place.
    """
    text = INSTALL.read_text()
    for line_no, line in enumerate(text.splitlines(), start=1):
        if "-m collector.main" in line and not line.lstrip().startswith("#"):
            context = "\n".join(text.splitlines()[max(0, line_no - 3):line_no])
            assert 'cd "$APP_DIR"' in context or "cd $APP_DIR" in context, (
                f"install.sh:{line_no} runs collector.main without cd'ing to $APP_DIR"
            )


def test_apt_update_can_never_abort_the_install():
    """A single unreachable third-party repo must not kill `set -e`.

    Ookla's packagecloud script adds a 'raspbian' repository that does not
    exist. Once that is on the box, a bare `apt-get update` fails for every
    package, and with `set -e` the installer dies before doing anything.
    """
    lines = INSTALL.read_text().splitlines()
    for line_no, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped.startswith("apt-get update"):
            continue
        guarded = "||" in stripped or stripped.startswith(("if ", "&& "))
        # `if apt-get update; then` reads as its own statement on some lines
        guarded = guarded or lines[line_no - 1].strip().startswith("if ")
        assert guarded, f"install.sh:{line_no}: unguarded `{stripped}` will abort under set -e"


def test_installer_clears_a_broken_ookla_source_before_updating():
    """Self-healing: a previous failed run must not permanently wedge apt."""
    lines = INSTALL.read_text().splitlines()
    code = [(n, ln.strip()) for n, ln in enumerate(lines) if not ln.strip().startswith("#")]

    cleanup = next(n for n, ln in code if "sources.list.d/ookla" in ln)
    first_update = next(n for n, ln in code if ln.startswith("apt-get update"))
    assert cleanup < first_update, "the broken-source cleanup must run before apt-get update"

    block = "\n".join(lines[cleanup : cleanup + 8])
    assert "rm -f" in block, "the cleanup must actually delete the source"


def test_installer_removes_the_ookla_repo_if_it_turns_out_unusable():
    """Adding a repo that apt cannot read would break the whole machine."""
    text = INSTALL.read_text()
    add = text.index("packagecloud.io/install/repositories/ookla")
    window = text[add : add + 900]
    assert "rm -f /etc/apt/sources.list.d/ookla" in window


def test_app_dir_is_marked_safe_for_git():
    """An --in-place checkout is owned by the human, not the service user.

    git then refuses to touch it ("detected dubious ownership"), and
    publish.sync_code fails silently on every run because the publisher treats
    a failed pull as non-fatal.
    """
    text = INSTALL.read_text()
    assert "safe.directory" in text, "install.sh must declare the app dir safe for git"
    assert '--add safe.directory "$APP_DIR"' in text


def test_environment_values_containing_spaces_are_quoted():
    """systemd reads Environment= as a SPACE-SEPARATED list of assignments.

    An unquoted `Environment=GIT_SSH_COMMAND=ssh -i key -o Foo=bar` therefore
    sets GIT_SSH_COMMAND=ssh and silently discards everything after it. The
    symptom is "Host key verification failed" on publish, with nothing in the
    unit that looks wrong.
    """
    for unit in UNITS:
        for line_no, line in enumerate(unit.read_text().splitlines(), start=1):
            if not line.startswith("Environment="):
                continue
            value = line[len("Environment=") :]
            if " " in value:
                assert value.startswith('"') and value.rstrip().endswith('"'), (
                    f"{unit.name}:{line_no}: Environment= value contains spaces and "
                    f"must be double-quoted, or systemd will truncate it: {value}"
                )


def test_publish_unit_pins_the_deploy_key_and_host_key_policy():
    """The publisher runs non-interactively; ssh must not be able to prompt."""
    text = (ROOT / "systemd" / "broadband-publish.service").read_text()
    ssh = next(
        ln for ln in text.splitlines()
        if "GIT_SSH_COMMAND" in ln and not ln.lstrip().startswith("#")
    )
    assert "-i /var/lib/broadband-monitor/.ssh/id_ed25519" in ssh, "deploy key not pinned"
    assert "IdentitiesOnly=yes" in ssh, "ssh may offer an unrelated agent key first"
    assert "StrictHostKeyChecking=" in ssh, "ssh would fall back to interactive 'ask'"
    assert "UserKnownHostsFile=" in ssh, "accept-new needs a writable known_hosts"


def test_installer_seeds_the_git_host_key():
    text = INSTALL.read_text()
    assert "ssh-keyscan" in text, "host key must be recorded while a human is watching"
    assert "known_hosts" in text


def test_ookla_engine_requires_the_real_ookla_client():
    """Debian's speedtest-cli package also installs a 'speedtest' alias.

    Enabling the ookla engine on the strength of that name means running the
    python client with Ookla's flags, which fails on every scheduled run.
    """
    text = INSTALL.read_text()
    assert "is_ookla()" in text, "must probe the binary, not just its name"
    assert "grep -qi 'ookla'" in text, "detection must key on the version banner"

    lines = [ln.strip() for ln in text.splitlines() if not ln.strip().startswith("#")]
    enabling = [n for n, ln in enumerate(lines) if 'ENGINES+=("ookla")' in ln]
    assert enabling, "the ookla engine is never enabled"
    for index in enabling:
        window = " ".join(lines[max(0, index - 6) : index + 1])
        assert "is_ookla" in window or "apt-get install -y speedtest" in window, (
            "ookla enabled without verifying the binary is really Ookla's"
        )


def test_architecture_is_reported():
    """Whether Ookla is even installable depends on it, so never leave it implicit."""
    assert 'say "Package architecture: $ARCH"' in INSTALL.read_text()


def test_patch_files_are_ignored():
    """A stray downloaded .patch was committed to the repo once; not again."""
    assert "*.patch" in (ROOT / ".gitignore").read_text()


def test_in_home_checkout_is_marked_group_shared():
    """A home-directory install is written by two accounts: the human and the
    service user doing sync_code's `git pull`. Without core.sharedRepository and
    setgid directories, whichever writes second creates objects the other cannot
    overwrite, and the next pull fails with 'insufficient permission for adding
    an object to repository database'.
    """
    text = INSTALL.read_text()
    assert "core.sharedRepository group" in text
    assert "chmod g+s" in text, "new directories must inherit the owning group"
