# Installation runbook

End to end, from a fresh Pi to a live dashboard on your own domain. Allow about
30 minutes, most of it waiting for DNS.

---

## 0. Before you start

* A Raspberry Pi **wired to the router over Ethernet**. Wi-Fi measurements
  characterise your Wi-Fi, not your line, and an ISP will say so immediately.
* Raspberry Pi OS Bookworm or later (any systemd Debian derivative works).
* A GitHub repository. It can be public or private — but **GitHub Pages on a
  private repo requires a paid plan**, so a public repo is the usual choice.
  Nothing sensitive is published: speeds, latencies, your ISP name and the
  external IP recorded by the speed-test provider.
* If you use a custom domain, access to its DNS.

Give the Pi a static lease on the router. A monitor that changes IP is annoying
to reach and its ARP churn shows up in your own measurements.

---

## 1. Create the repository

Create an empty repo (no README — the clone below provides the content), then on
the Pi:

```bash
git clone https://github.com/<you>/home-broadband.git ~/git/broadband-monitor
cd ~/git/broadband-monitor
sudo ./scripts/install.sh --in-place
```

The installer will:

1. install `python3-venv`, `iputils-ping`, `curl`, `git`, `dnsutils`, `sqlite3`
2. set up every throughput engine it can (see below) and write the working ones
   into `speed.engines`
3. create the `broadband` system user and its data directory
4. write `/etc/sysctl.d/60-broadband-monitor.conf` so that group may open ICMP
   sockets without root
5. build a virtualenv at `<app dir>/.venv`
6. seed `/etc/broadband-monitor/config.yaml` from the example
7. generate an SSH deploy key at `/var/lib/broadband-monitor/.ssh/id_ed25519`
8. install and enable the four systemd timers

It is idempotent — re-run it after a `git pull` to pick up code changes without
touching your config or database.

### Where the code lives

| Command | Result |
|---|---|
| `sudo ./scripts/install.sh` | Copies the code to `/opt/broadband-monitor` and runs from there. |
| `sudo ./scripts/install.sh --in-place` | Runs from this checkout, wherever it is. |
| `sudo ./scripts/install.sh --app-dir /srv/bb` | Copies it somewhere you choose. |

`--in-place` is the one to use if you keep your clone in `~/git` and want to
edit it there: one copy of the code, and `git pull` in that directory is the
update. The default copy-to-`/opt` keeps the running code out of your home
directory, at the cost of two trees that can drift.

Running from a home directory needs two adjustments, both of which the installer
makes and announces:

* **`ProtectHome=` is switched from `yes` to `no`** on the generated units.
  With it on, systemd masks `/home` entirely and the service cannot see its own
  code — the failure looks like a missing file, not a sandbox.
* **The service user is added to your login group**, and each directory on the
  path gets `g+x` so it can traverse in. Your ownership is unchanged, and no
  account outside that group gains anything.
* **The checkout is marked shared with git** — `safe.directory` for the service
  user (git otherwise refuses a repo owned by someone else, and `sync_code`'s
  pull would fail silently), plus `core.sharedRepository=group` and setgid
  directories so objects either account writes stay writable by the other.

Whichever you pick, `publish.repo_dir` is rewritten to match on every run, and
the choice is recorded in `/etc/broadband-monitor/install.env` so `uninstall.sh`
knows what to remove — and knows to leave your working clone alone.

The units are generated from `systemd/*.service`, so edit those in the repo and
re-run the installer rather than editing `/etc/systemd/system` by hand.

> `python -m collector.main` resolves the package from the working directory.
> Every manual invocation must be run from the app directory, or it fails with
> `No module named 'collector'`.

### Throughput engines

The installer enables whichever it can get working:

| Engine | Availability |
|---|---|
| `speedtest-cli` | `apt install speedtest-cli` — works on every Debian derivative, including 32-bit. Always enabled. |
| `ookla` | Ookla's apt repo. **They publish amd64, arm64 and armel, but no armhf**, so 32-bit Raspberry Pi OS cannot install it. The installer detects this and skips the engine instead of failing. |
| `cloudflare` | Needs nothing. Always enabled. |

Check which architecture you are on:

```bash
dpkg --print-architecture     # armhf = 32-bit, arm64 = 64-bit
```

If that says `armhf` and you want the Ookla engine — the only one producing
result URLs an ISP will accept — the fix is a 64-bit Raspberry Pi OS image on a
Pi 3 or newer. Until then `speedtest-cli` hits the same speedtest.net servers,
so the measurements are comparable even though the evidence is weaker.

One more note on Ookla's repository: packagecloud's setup script keys off the
distro id, which on Raspberry Pi OS is `raspbian` — for which Ookla has no
repository at all. The installer pins it to `os=debian`, which is why adding
the repo by hand from Ookla's own instructions may not work.

---

## 2. Give the Pi write access to the repo

Copy the public key the installer printed (or `sudo cat
/var/lib/broadband-monitor/.ssh/id_ed25519.pub`) and add it under:

**Repo → Settings → Deploy keys → Add deploy key**

* Title: `raspberry-pi-broadband`
* Key: the printed line
* ✅ **Allow write access** — without this every publish fails with
  `ERROR: The key you are authenticating with has been marked as read only`

A deploy key is scoped to this one repository. Prefer it to a personal access
token: if the Pi is ever compromised, the blast radius is one repo, and
revoking it is one click.

Test it:

```bash
sudo -u broadband GIT_SSH_COMMAND='ssh -i /var/lib/broadband-monitor/.ssh/id_ed25519 -o IdentitiesOnly=yes' \
  ssh -T git@github.com
# "Hi <you>/home-broadband! You've successfully authenticated, but GitHub does
#  not provide shell access." — that message means success.
```

---

## 3. Configure

```bash
sudo nano /etc/broadband-monitor/config.yaml
```

The five things that matter:

```yaml
site:
  domain: "home-broadband.example.com"   # or "" for the default github.io URL
  timezone: "Europe/London"
  isp:
    advertised_down_mbps: 500            # the headline "average" speed sold to you
    guaranteed_min_down_mbps: 250        # the Ofcom guaranteed minimum on your contract

latency:
  targets:
    - name: "Router"
      host: "192.168.1.1"                # YOUR gateway — check with `ip route | grep default`
      group: "lan"
      checks: [icmp]
    # …

publish:
  remote: "git@github.com:<you>/home-broadband.git"   # SSH, not HTTPS
```

Both ISP figures are on your contract or in the order confirmation email. If
you do not know them, leave them at 0 and the Ofcom panel simply hides.

Validate and take a first sample:

```bash
cd ~/git/broadband-monitor        # or wherever you installed — this cd matters
RUN="sudo -u broadband .venv/bin/python -m collector.main"

$RUN latency --dry-run
$RUN speed --force --engine cloudflare      # fastest engine to smoke-test with
$RUN speed --force --engine speedtest-cli
$RUN status
```

A config error prints the offending target and exits — nothing is written.

---

## 4. First publish

```bash
sudo systemctl start broadband-publish.service
journalctl -u broadband-publish -n 40 --no-pager
```

You are looking for `exported N day files` followed by `pushed gh-pages`.
Confirm on GitHub that the `gh-pages` branch now exists and contains
`index.html`, `data/`, `.nojekyll` and (if you set a domain) `CNAME`.

---

## 5. Turn on GitHub Pages

**Repo → Settings → Pages**

* Source: **Deploy from a branch**
* Branch: **`gh-pages`**, folder **`/ (root)`**
* Save

Within a minute the site is live at
`https://<you>.github.io/home-broadband/`. Check it works there **before**
adding a custom domain — it removes DNS from the equation if something is wrong.

The publisher writes `.nojekyll`, which stops GitHub running Jekyll over the
branch. Without it, anything under a `_`-prefixed path is silently dropped.

---

## 6. Custom domain

### DNS

For a subdomain such as `home-broadband.example.com`, one CNAME:

| Type | Name | Value | Proxy |
|---|---|---|---|
| CNAME | `home-broadband` | `<you>.github.io` | **DNS only** |

> **If your DNS is on Cloudflare, the orange cloud must be OFF (DNS only).**
> With the proxy on, GitHub cannot complete the ACME HTTP-01 challenge and
> certificate issuance fails with "Certificate provisioning is in progress" that
> never finishes. Turn the proxy off, wait for the certificate to issue, and
> only then turn it back on if you want Cloudflare in front — and if you do, set
> the SSL/TLS mode to **Full (strict)**, never Flexible, or you get a redirect
> loop.

For an apex domain, use the four GitHub A records instead
(185.199.108–111.153) plus the matching AAAA records; the current list is in
[GitHub's docs](https://docs.github.com/pages/configuring-a-custom-domain-for-your-github-pages-site).

### GitHub side

Set `site.domain` in `config.yaml` and republish — the publisher writes the
`CNAME` file for you. Then in **Settings → Pages** confirm the custom domain is
recognised, wait for the certificate, and tick **Enforce HTTPS**.

Verify:

```bash
dig +short home-broadband.example.com          # → <you>.github.io. → 185.199.x.x
curl -sI https://home-broadband.example.com | head -3
```

> Each publish force-pushes the branch, and the `CNAME` file is rewritten from
> `site.domain` every time. If you set the domain through the GitHub UI without
> also setting it in `config.yaml`, the next publish removes it and your domain
> breaks. Set it in `config.yaml`.

---

## 7. Confirm the timers

```bash
systemctl list-timers 'broadband-*'
```

Expect four: latency (every minute), speed (hourly), publish (hourly), prune
(daily 04:17). To change a cadence, edit the timer and keep `config.yaml` in
step — the dashboard reads the configured interval for its captions:

```bash
sudo systemctl edit broadband-speed.timer     # e.g. OnCalendar=*:00/30
sudo systemctl daemon-reload
sudo systemctl restart broadband-speed.timer
```

Leave it a day. The charts need a night and a peak-hours evening before they say
anything interesting.

---

## Updating

```bash
cd ~/git/broadband-monitor
git pull
sudo ./scripts/install.sh --in-place   # idempotent; keeps config and database
```

(Drop `--in-place` and use `sudo git pull` if you installed to `/opt`.)

With `publish.sync_code: true` (the default) the Pi also pulls `main` before
each publish, so a dashboard tweak pushed from your laptop goes live within the
hour without touching the Pi.

## Removing

```bash
sudo ./scripts/uninstall.sh          # keeps the database and your checkout
sudo ./scripts/uninstall.sh --purge  # also removes the database and config
```

An `--in-place` install is never deleted by `uninstall.sh` — it is your working
clone. Only the units, the sysctl drop-in and (with `--purge`) the state under
`/var/lib` and `/etc` are removed.
