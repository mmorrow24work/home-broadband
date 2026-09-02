#!/usr/bin/env bash
#
# home-broadband installer for Raspberry Pi OS / Debian / Ubuntu.
#
#   sudo ./scripts/install.sh                 # copy to /opt/broadband-monitor
#   sudo ./scripts/install.sh --in-place      # run from this checkout
#   sudo ./scripts/install.sh --app-dir PATH  # run from somewhere specific
#
# --in-place is the one to use if you keep your clone in ~/git and want to edit
# it there. It keeps one copy of the code instead of two that can drift, but the
# service user needs to reach your home directory, so the installer relaxes
# ProtectHome= on the units and grants group traversal — see below.
#
# Idempotent: safe to re-run after a git pull to pick up code changes.
set -euo pipefail

DATA_DIR=/var/lib/broadband-monitor
CONF_DIR=/etc/broadband-monitor
SERVICE_USER=broadband
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-/opt/broadband-monitor}"

say()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[x]\033[0m %s\n' "$*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --in-place) APP_DIR="$SRC_DIR"; shift ;;
    --app-dir)  APP_DIR="${2:?--app-dir needs a path}"; shift 2 ;;
    -h|--help)  sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \?//'; exit 0 ;;
    *)          die "unknown option: $1" ;;
  esac
done
APP_DIR="${APP_DIR%/}"

[[ $EUID -eq 0 ]] || die "run me with sudo"
command -v systemctl >/dev/null || die "this installer expects systemd"

# A previous run may have used a different location; clean up after it later.
PREV_APP_DIR=""
[[ -f "$CONF_DIR/install.env" ]] && . "$CONF_DIR/install.env" && PREV_APP_DIR="${APP_DIR_INSTALLED:-}"

# ---------------------------------------------------------------- packages
say "Installing OS packages"
export DEBIAN_FRONTEND=noninteractive

# An earlier attempt may have added Ookla's packagecloud repo for 'raspbian',
# which does not exist. apt-get update then fails for EVERY package on the
# machine, not just that one, so clear it out before doing anything else.
for src in /etc/apt/sources.list.d/ookla*speedtest*; do
  [[ -e "$src" ]] || continue
  if grep -qi 'raspbian' "$src" 2>/dev/null; then
    warn "Removing broken Ookla apt source left by an earlier attempt: $src"
    rm -f "$src"
  fi
done

# One unreachable third-party repo must never be able to abort the install.
apt-get update || warn "apt-get update reported errors; continuing with what apt has"

apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip \
  iputils-ping curl ca-certificates git dnsutils sqlite3

# ----------------------------------------------------- throughput engines
# Three engines exist; the config lists whichever are actually available here.
ENGINES=()

# --- 1. speedtest-cli (Debian's python client) — installs anywhere ---------
say "Installing speedtest-cli (python client)"
if apt-get install -y --no-install-recommends speedtest-cli; then
  ENGINES+=("speedtest-cli")
else
  warn "speedtest-cli install failed"
fi

# --- 2. official Ookla CLI ------------------------------------------------
# Debian's speedtest-cli package also installs a 'speedtest' alias, so the mere
# existence of that name proves nothing. Only Ookla's client says "Ookla" in its
# version banner; enabling the engine on the strength of the name alone means
# running the python client with Ookla's flags, which fails every time.
is_ookla() {
  command -v speedtest >/dev/null 2>&1 || return 1
  speedtest --version 2>&1 | head -1 | grep -qi 'ookla'
}

ARCH="$(dpkg --print-architecture)"
say "Package architecture: $ARCH"
if is_ookla; then
  say "Ookla Speedtest CLI already present: $(speedtest --version 2>&1 | head -1)"
  ENGINES+=("ookla")
elif command -v speedtest >/dev/null 2>&1; then
  warn "'speedtest' on PATH is $(speedtest --version 2>&1 | head -1), not Ookla's client."
  warn "That is Debian's speedtest-cli package, which ships a 'speedtest' alias too."
  warn "Not enabling the 'ookla' engine — speedtest-cli already covers speedtest.net."
  warn "To use the real client, install it and set speed.ookla.binary to its path."
elif [[ "$ARCH" == "armhf" || "$ARCH" == "i386" ]]; then
  # Ookla publishes amd64, arm64 and armel — but not armhf. A 32-bit Raspberry
  # Pi OS install simply cannot have it, which is why speedtest-cli is above.
  warn "Ookla publishes no $ARCH packages — skipping the 'ookla' engine."
  warn "Re-image with 64-bit Raspberry Pi OS (arch arm64) if you want it;"
  warn "speedtest-cli also uses speedtest.net and is enabled instead."
else
  say "Installing the official Ookla Speedtest CLI"
  # packagecloud's script keys off the distro id. On Raspberry Pi OS that is
  # 'raspbian', for which Ookla has no repository, so pin it to Debian.
  CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-bookworm}")"
  if curl -fsSL https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh \
       | os=debian dist="$CODENAME" bash; then
    # Never leave a repository behind that apt cannot read — it would break
    # every future `apt update` on this machine, not just this install.
    if apt-get update; then
      apt-get install -y speedtest && ENGINES+=("ookla") || warn "Ookla package install failed"
    else
      warn "Ookla's repository is unusable on this system — removing it again."
      rm -f /etc/apt/sources.list.d/ookla*speedtest*
      apt-get update -qq || true
    fi
  else
    warn "Could not add Ookla's repository."
  fi
  is_ookla || warn "Ookla CLI unavailable — the other engines still work."
fi

# --- 3. cloudflare needs nothing installed --------------------------------
ENGINES+=("cloudflare")

# ------------------------------------------------------------------- user
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  say "Creating system user $SERVICE_USER"
  useradd --system --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 "$DATA_DIR" "$DATA_DIR/pages"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$DATA_DIR/.ssh"
install -d -m 0755 "$CONF_DIR"

# ---------------------------------------------------- unprivileged ICMP
say "Allowing unprivileged ICMP for group $SERVICE_USER"
GID="$(id -g "$SERVICE_USER")"
cat >/etc/sysctl.d/60-broadband-monitor.conf <<EOF
# Lets the broadband-monitor service open ICMP datagram sockets without root.
net.ipv4.ping_group_range = $GID $GID
EOF
sysctl -q --system || warn "sysctl reload failed; ping may need CAP_NET_RAW"

# ------------------------------------------------------------ application
# Running from a home directory needs two things the default install does not:
# systemd's ProtectHome= must be relaxed (it masks /home outright), and the
# service user must be able to traverse into the directory.
PROTECT_HOME=yes
IN_HOME=0
case "$APP_DIR" in
  /home/*|/root/*|/Users/*) IN_HOME=1; PROTECT_HOME=no ;;
esac

if [[ "$SRC_DIR" == "$APP_DIR" ]]; then
  say "Running in place from $APP_DIR (no copy)"
else
  say "Installing application to $APP_DIR"
  install -d -m 0755 "$APP_DIR"
  cp -a "$SRC_DIR/collector" "$SRC_DIR/site" "$SRC_DIR/systemd" "$SRC_DIR/scripts" "$APP_DIR/"
  [[ -d "$SRC_DIR/.git" ]] && cp -a "$SRC_DIR/.git" "$APP_DIR/" || true
  cp -a "$SRC_DIR/config" "$APP_DIR/"
fi

if [[ $IN_HOME -eq 1 ]]; then
  OWNER="$(stat -c '%U' "$APP_DIR")"
  OWNER_GROUP="$(stat -c '%G' "$APP_DIR")"
  say "Granting $SERVICE_USER access to $APP_DIR (owned by $OWNER)"
  # Keep your ownership — just let the service user in via the owning group.
  usermod -aG "$OWNER_GROUP" "$SERVICE_USER"
  chmod g+rX "$APP_DIR"
  # Every parent must be traversable, or the service user cannot reach it.
  probe="$(dirname "$APP_DIR")"
  while [[ "$probe" != "/" ]]; do
    chmod g+x "$probe" 2>/dev/null || warn "could not chmod g+x $probe"
    probe="$(dirname "$probe")"
  done
  warn "Members of group '$OWNER_GROUP' can now traverse the path to $APP_DIR."
  warn "The service user was added to that group; no other account gained access."
  # The publisher writes into the checkout (git pull), so it must stay writable.
  chown -R "$OWNER:$OWNER_GROUP" "$APP_DIR"
  chmod -R g+rwX "$APP_DIR"

  # TWO accounts now write to this git repo: the human, and the service user
  # running publish.sync_code's `git pull`. By default each creates objects the
  # other cannot overwrite, and the next pull dies with "insufficient permission
  # for adding an object to repository database". core.sharedRepository makes git
  # create group-writable objects; setgid makes new directories keep the group.
  if [[ -d "$APP_DIR/.git" ]]; then
    say "Marking $APP_DIR as a group-shared git repository"
    sudo -u "$OWNER" -H git -C "$APP_DIR" config core.sharedRepository group \
      || warn "could not set core.sharedRepository; sync_code may break your pulls"
    find "$APP_DIR" -type d -exec chmod g+s {} + 2>/dev/null || true
  fi
else
  chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR"
fi

# git refuses to operate on a repository owned by another user ("detected
# dubious ownership"), which would make publish.sync_code fail silently on an
# --in-place install. Declare it safe for the service user specifically.
if [[ -d "$APP_DIR/.git" ]]; then
  # -H so HOME is the service user's, or `git config --global` writes the
  # setting into the *invoking* user's ~/.gitconfig, where it does nothing.
  sudo -u "$SERVICE_USER" -H git config --global --add safe.directory "$APP_DIR" 2>/dev/null || true
  if sudo -u "$SERVICE_USER" -H git -C "$APP_DIR" rev-parse --git-dir >/dev/null 2>&1; then
    say "$APP_DIR is readable by $SERVICE_USER (sync_code will work)"
  else
    warn "git still refuses $APP_DIR for $SERVICE_USER; setting it system-wide"
    git config --system --add safe.directory "$APP_DIR" || \
      warn "could not mark $APP_DIR safe; publish.sync_code will not work"
  fi
fi

say "Creating the Python virtualenv"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet PyYAML requests dnspython
if [[ $IN_HOME -eq 1 ]]; then
  chown -R "$OWNER:$OWNER_GROUP" "$APP_DIR/.venv"
  chmod -R g+rX "$APP_DIR/.venv"
else
  chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/.venv"
fi

# ---------------------------------------------------------------- config
if [[ ! -f "$CONF_DIR/config.yaml" ]]; then
  say "Seeding $CONF_DIR/config.yaml"
  cp "$SRC_DIR/config/config.example.yaml" "$CONF_DIR/config.yaml"
  # Only list engines whose binary actually exists on this machine.
  ENGINE_LIST="$(IFS=,; echo "${ENGINES[*]}" | sed 's/,/, /g')"
  sed -i "s/^  engines: \[.*\]$/  engines: [$ENGINE_LIST]/" "$CONF_DIR/config.yaml"
  say "Enabled throughput engines: $ENGINE_LIST"
  chown root:"$SERVICE_USER" "$CONF_DIR/config.yaml"
  chmod 0640 "$CONF_DIR/config.yaml"
  NEEDS_EDIT=1
else
  say "Keeping existing $CONF_DIR/config.yaml"
  ENGINE_LIST="$(IFS=,; echo "${ENGINES[*]}" | sed 's/,/, /g')"
  say "Engines available on this machine: $ENGINE_LIST"

  # A config seeded by an earlier run can name engines this machine cannot run —
  # every one of those turns into a failed test on the dashboard, forever. Say so
  # rather than leaving it to be discovered an hour later in the journal.
  CONFIGURED="$(sed -n 's/^  engines: \[\(.*\)\]$/\1/p' "$CONF_DIR/config.yaml" | tr -d ' ')"
  MISSING=""
  for want in ${CONFIGURED//,/ }; do
    case " ${ENGINES[*]} " in
      *" $want "*) ;;
      *) MISSING="$MISSING $want" ;;
    esac
  done
  if [[ -n "$MISSING" ]]; then
    warn "config.yaml lists engine(s) not available here:$MISSING"
    warn "Every run of those will be recorded as a failed test. Fix with:"
    warn "  sudo sed -i 's/^  engines: \\[.*\\]\$/  engines: [$ENGINE_LIST]/' $CONF_DIR/config.yaml"
  fi
  NEEDS_EDIT=0
fi

# ------------------------------------------------------------- deploy key
KEY="$DATA_DIR/.ssh/id_ed25519"
if [[ ! -f "$KEY" ]]; then
  say "Generating a GitHub deploy key"
  sudo -u "$SERVICE_USER" ssh-keygen -t ed25519 -N '' -C "broadband-monitor@$(hostname)" -f "$KEY" -q
  NEW_KEY=1
else
  NEW_KEY=0
fi

# Seed the host key now, while a human is watching, rather than relying on
# accept-new inside a non-interactive service. Without a known_hosts entry the
# publish fails with "Host key verification failed" and nothing explains why.
KNOWN_HOSTS="$DATA_DIR/.ssh/known_hosts"
GIT_HOST="$(sed -n 's#^  remote: .*git@\([^:]*\):.*#\1#p' "$CONF_DIR/config.yaml" | head -1)"
GIT_HOST="${GIT_HOST:-github.com}"
if ! sudo -u "$SERVICE_USER" ssh-keygen -F "$GIT_HOST" -f "$KNOWN_HOSTS" >/dev/null 2>&1; then
  say "Recording the SSH host key for $GIT_HOST"
  if ssh-keyscan -T 10 -t rsa,ecdsa,ed25519 "$GIT_HOST" 2>/dev/null >>"$KNOWN_HOSTS"; then
    chown "$SERVICE_USER:$SERVICE_USER" "$KNOWN_HOSTS"
    chmod 0644 "$KNOWN_HOSTS"
  else
    warn "Could not reach $GIT_HOST to record its host key; the first publish"
    warn "will fall back to accept-new, which needs $KNOWN_HOSTS to be writable."
  fi
fi

# ------------------------------------------------------------------ units
# The shipped units name /opt/broadband-monitor so they are readable on their
# own; rewrite them for wherever this install actually lives.
say "Installing systemd units (APP_DIR=$APP_DIR, ProtectHome=$PROTECT_HOME)"
for unit in "$SRC_DIR"/systemd/*.service "$SRC_DIR"/systemd/*.timer; do
  sed -e "s#/opt/broadband-monitor#$APP_DIR#g" \
      -e "s#^ProtectHome=yes\$#ProtectHome=$PROTECT_HOME#" \
      "$unit" > "/etc/systemd/system/$(basename "$unit")"
  chmod 0644 "/etc/systemd/system/$(basename "$unit")"
done
systemctl daemon-reload

# publish.repo_dir is where the publisher reads site/ from and runs `git pull`,
# so it always follows the code. Kept in step on every run.
say "Setting publish.repo_dir to $APP_DIR"
sed -i "s#^  repo_dir: .*#  repo_dir: \"$APP_DIR\"#" "$CONF_DIR/config.yaml"
cat >"$CONF_DIR/install.env" <<EOF
# Written by install.sh — used by uninstall.sh and re-runs.
APP_DIR_INSTALLED="$APP_DIR"
EOF

if [[ -n "$PREV_APP_DIR" && "$PREV_APP_DIR" != "$APP_DIR" && -d "$PREV_APP_DIR" ]]; then
  warn "A previous install exists at $PREV_APP_DIR and is no longer used."
  warn "Remove it once you are happy:  sudo rm -rf $PREV_APP_DIR"
fi

# `python -m collector.main` resolves the package from the working directory, so
# this MUST run from $APP_DIR. Running it from wherever the installer was invoked
# fails with "No module named 'collector'" — the service user usually cannot read
# your home directory anyway.
say "Initialising the database"
(cd "$APP_DIR" && sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/python" -m collector.main \
  --config "$CONF_DIR/config.yaml" init-db)

say "Verifying the installation"
(cd "$APP_DIR" && sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/python" -m collector.main \
  --config "$CONF_DIR/config.yaml" status)

systemctl enable --now broadband-latency.timer broadband-speed.timer \
                        broadband-publish.timer broadband-prune.timer

say "Done."
cat <<EOF

Next steps
----------
1. Edit the config:            sudo nano $CONF_DIR/config.yaml
     - publish.remote          your repo's SSH URL
     - site.domain             your custom domain (or "")
     - latency.targets         your gateway IP and the hosts you care about
     - site.isp.*              advertised and guaranteed speeds

2. Add this deploy key to the repo with **write access**
   (Settings -> Deploy keys -> Add deploy key -> tick "Allow write access"):

$(cat "${KEY}.pub")

3. In the repo: Settings -> Pages -> Source = "Deploy from a branch",
   branch "gh-pages", folder "/ (root)".

4. Smoke test, then watch it run (run these from $APP_DIR — 'python -m' needs it):
     cd $APP_DIR
     sudo -u $SERVICE_USER .venv/bin/python -m collector.main latency --dry-run
     sudo -u $SERVICE_USER .venv/bin/python -m collector.main speed --force --engine speedtest-cli
     sudo systemctl start broadband-publish.service
     journalctl -u broadband-publish -n 50 --no-pager
     systemctl list-timers 'broadband-*'
EOF

[[ $NEW_KEY -eq 1 ]] || echo "(Deploy key already existed — reuse the one on GitHub.)"
[[ $NEEDS_EDIT -eq 1 ]] && warn "config.yaml is the stock example — edit it before the first publish."
exit 0
