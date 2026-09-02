# Operations and troubleshooting

## Health check

```bash
cd /opt/broadband-monitor    # or ~/git/broadband-monitor for an --in-place install
sudo -u broadband .venv/bin/python -m collector.main status
systemctl list-timers 'broadband-*'
journalctl -u broadband-latency -u broadband-speed -u broadband-publish --since '-2h' --no-pager
```

`systemctl cat broadband-speed.service | grep WorkingDirectory` tells you which
directory this install actually runs from if you have forgotten.

Check what was actually published, which is a different question from what was
collected:

```bash
curl -s https://your-domain/data/manifest.json | python3 -m json.tool | head -40
git ls-remote --heads origin gh-pages          # the branch exists at all
```

`status` prints row counts per table, the last successful throughput test and a
24-hour latency summary per target. If row counts are not growing, the timers
are not firing; if they are growing but the site is stale, publishing is
failing.

---

## Common failures

### `ping: socket: Operation not permitted`

The unprivileged-ICMP sysctl did not apply.

```bash
sysctl net.ipv4.ping_group_range          # should be "<gid> <gid>" for the broadband group
id -g broadband
sudo sysctl --system
```

If your kernel refuses it, grant the capability instead:

```bash
sudo systemctl edit broadband-latency.service
# [Service]
# AmbientCapabilities=CAP_NET_RAW
# CapabilityBoundingSet=CAP_NET_RAW
```

### `Host key verification failed` on publish

github.com is not in the service user's `known_hosts`, and the publisher runs
non-interactively so ssh cannot prompt. Record it:

```bash
sudo -u broadband ssh-keyscan -t rsa,ecdsa,ed25519 github.com \
  | sudo tee -a /var/lib/broadband-monitor/.ssh/known_hosts
sudo chown broadband:broadband /var/lib/broadband-monitor/.ssh/known_hosts
```

If it recurs after that, check the unit's ssh command actually survived:

```bash
systemctl show broadband-publish.service -p Environment
```

`GIT_SSH_COMMAND` must be the whole `ssh -i … -o …` string. If it is just
`ssh`, the `Environment=` line in the unit is missing its double quotes —
systemd reads that setting as a space-separated list of assignments and throws
away everything after the first space. Re-run `install.sh` to regenerate the
unit.

### Every publish fails with "marked as read only"

The deploy key was added without **Allow write access**. Delete it on GitHub and
re-add the same key with the box ticked — no need to regenerate.

### Every timer fails after you opened the database by hand

```
attempt to write a readonly database
```
or a publish that exits non-zero straight after you ran some SQL. Check the
sidecar files:

```bash
ls -la /var/lib/broadband-monitor/
```

If `broadband.db-wal` or `broadband.db-shm` is owned by **root**, that is the
cause — SQLite creates them as the user who opened the database, and a
`sudo sqlite3` leaves root-owned ones the service user cannot write. Repair:

```bash
sudo chown broadband:broadband /var/lib/broadband-monitor/broadband.db*
sudo systemctl start broadband-publish.service
```

Use `sudo -u broadband sqlite3 …` rather than `sudo sqlite3 …` and it cannot
happen.

### `insufficient permission for adding an object to repository database`

Your `git pull` fails in an `--in-place` checkout. Two accounts write to that
repo — you, and the `broadband` service user running `publish.sync_code`'s
hourly pull — and by default each creates objects the other cannot overwrite.

```bash
cd ~/git/broadband-monitor
sudo chown -R "$USER:$USER" .
git config core.sharedRepository group
sudo find . -type d -exec chmod g+s {} +
sudo chmod -R g+w .
git pull
```

`core.sharedRepository` makes git write group-writable objects; the setgid bit
makes new directories keep the owning group, so files the service user creates
stay writable by you. `install.sh` sets both on an in-home install — this is the
manual repair for a checkout created before that.

If you would rather remove the shared-write problem entirely, turn the pull off
and update the code yourself:

```yaml
publish:
  sync_code: false
```

The site's *data* still publishes hourly; only code changes then need a manual
`git pull` (which the installer does anyway when you re-run it).

### `ModuleNotFoundError: No module named 'collector'`

`python -m` resolves the package from the working directory. Run it from the app
directory:

```bash
cd "$(systemctl show broadband-speed.service -p WorkingDirectory --value)"
sudo -u broadband .venv/bin/python -m collector.main status
```

### `The repository 'https://packagecloud.io/ookla/speedtest-cli/raspbian bookworm Release' does not have a Release file`

A failed attempt at adding Ookla's repository has wedged apt for **every**
package on the machine, not just that one. Ookla has no `raspbian` repository,
so the source can never resolve. Remove it:

```bash
sudo rm -f /etc/apt/sources.list.d/ookla*speedtest*
sudo apt update
```

Recent versions of `install.sh` detect and clear this automatically before doing
anything else, and refuse to leave an unreadable repository behind.

### `E: Unable to locate package speedtest`

Ookla publishes amd64, arm64 and armel packages — **not armhf**. On 32-bit
Raspberry Pi OS (`dpkg --print-architecture` says `armhf`) the Ookla engine
simply cannot be installed. Use `speedtest-cli` and `cloudflare`, which is what
`install.sh` configures automatically:

```yaml
speed:
  engines: [speedtest-cli, cloudflare]
```

On a 64-bit image the usual cause is different: packagecloud's setup script keys
off the distro id, which is `raspbian` here, and Ookla has no raspbian
repository. Pin it to Debian:

```bash
curl -fsSL https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh \
  | sudo os=debian dist=bookworm bash
sudo apt install speedtest
```

Failing that, take the `aarch64` tarball from <https://www.speedtest.net/apps/cli>
and drop the binary in `/usr/local/bin`.

### High packet loss to the router only, everything beyond it clean

```
Router        avg 0.98 ms   loss 31.43%
ISP gateway   avg 9.08 ms   loss  0%
Google DNS    avg 8.21 ms   loss  0%
```

This is almost never a fault. Consumer routers rate-limit ICMP **addressed to
themselves** — forwarding traffic is handled in hardware, but a ping to the
router's own IP goes to its slow management CPU, so it is deliberately capped.
Traffic through it is unaffected, which is exactly what the 0% figures for every
target beyond it are telling you.

It is only worth investigating if loss to the gateway rises *at the same time*
as loss to external targets. Since the gateway is in group `lan`, it is excluded
from outage detection for this reason.

To quiet the noise on the loss chart, probe it more gently:

```yaml
latency:
  targets:
    - name: "Router"
      host: "192.168.1.1"
      group: "lan"
      checks: [icmp]
      count: 2          # fewer echoes per sweep, less rate-limiting
```

Confirm the address is really your gateway first — `ip route | grep default`.
A high-loss LAN target that is not the gateway is a different question.

### The installer says the ookla engine is available when it is not

Debian's `speedtest-cli` package installs a `speedtest` alias alongside
`speedtest-cli`, so the name existing proves nothing. Current versions probe the
version banner and only enable the engine for a client that identifies as
Ookla's. If yours reports `speedtest-cli 2.1.3`, the alias is what you have:

```bash
speedtest --version        # "Speedtest by Ookla" vs "speedtest-cli 2.1.3"
```

Remove `ookla` from `speed.engines` — `speedtest-cli` already covers
speedtest.net — or install the real client and point `speed.ookla.binary` at it.

### `speedtest-cli` reports much lower than the others

Usually correct, and usually not your line. The Debian client is pure Python and
single-process; on a Pi it saturates a core before it saturates a fast
connection, typically somewhere above 200 Mbps. Confirm by watching a run:

```bash
cd /opt/broadband-monitor
sudo -u broadband .venv/bin/python -m collector.main speed --force --engine speedtest-cli &
top -bn3 -d2 | grep -i python
```

If it is pinned at ~100% of a core, the client is the bottleneck. That is
precisely why it is one of three engines rather than the only one — treat Ookla
or Cloudflare as the line measurement and `speedtest-cli` as a consistency
check.

It also reports no jitter and no packet loss, so those columns are NULL for its
rows and show as "—" on the dashboard. That is expected, not a collection
failure.

### The engines disagree

Expected up to a point — different servers, different peering, different stream
counts. Read the pattern rather than any single number:

| Pattern | Most likely cause |
|---|---|
| All three low | Your line, or something else saturating it during the test |
| Only `speedtest-cli` low | The Pi's CPU (see above) |
| Ookla and Cloudflare diverge | Congested path to one provider, not a line fault |
| One engine erratic, others steady | That provider's nearest server; pin a better one |

### Automatic server selection picks a server in the wrong country

Server choice follows speedtest.net's geolocation of your public IP, and on
carrier ranges that is frequently wrong — a Coventry line can be told its
nearest server is 540 km away in Amsterdam, then tested against Munich. Every
result is then measuring a long path rather than your line.

```bash
cd /opt/broadband-monitor       # or your app dir
sudo -u broadband .venv/bin/python -m collector.main servers
```

That lists UK servers with their ids and prints the YAML to paste. It also
flags when the nearest offered server is implausibly distant, which is the tell
that geolocation is off. `--country ''` shows everything, `--grep london`
narrows further.

Cross-check what the internet thinks of your connection. Cloudflare's `/meta`
now returns an empty object, so use the trace endpoint — every Cloudflare-fronted
host serves it:

```bash
curl -s https://speed.cloudflare.com/cdn-cgi/trace
```

`colo=` is the edge serving you (LHR or MAN from the UK) and `loc=` is the
country Cloudflare places you in. If `loc` disagrees with where you actually
are, your ISP's IP range is registered elsewhere — which is what makes
speedtest.net offer servers in the wrong country.

**Check which address family is being used first.** `curl -s
https://speed.cloudflare.com/cdn-cgi/trace` shows the `ip=` you egress on. If it
is IPv6, that prefix may be geolocated far worse than your IPv4 one — the
provider registers a large v6 block centrally while the v4 range is regional.
speedtest-cli 2.1.3 has no `--ipv4` flag, but binding a source address forces
the family:

```bash
ip -4 addr show "$(ip -o route show default | awk '{print $5}')" | grep -oP 'inet \K[\d.]+'
speedtest-cli --source <that address> --list | grep -i 'united kingdom'
```

If UK servers appear, make it permanent and pin one:

```yaml
speed:
  speedtest-cli:
    source_ip: "192.168.1.20"
    server_id: <a UK id>
```

If `speedtest-cli --list` still returns only a handful of servers, all far away,
there is no local one to pin: Ookla's legacy static-server endpoint that the 2019
client uses now returns just the few nearest to that wrong location. Run the
Cloudflare engine alone rather than keeping a systematically low second series
in the same chart:

```yaml
speed:
  engines: [cloudflare]
```

To remove server-choice noise from long-term trends, pin a server — at the cost
of depending on it staying healthy:

```bash
sudo -u broadband .venv/bin/python -m collector.main servers   # recommended
speedtest --servers                                            # Ookla, if installed
speedtest-cli --list | grep -i 'united kingdom'                # raw list
```

```yaml
speed:
  ookla:
    server_id: 12345
  speedtest-cli:
    server_id: 12345           # same server = directly comparable numbers
```

Pinning both speedtest.net clients to the *same* server is the cleanest way to
isolate whether a gap is the Pi or the path.

### A `speedtest` on PATH that is not Ookla's

A pip-installed `speedtest-cli` sometimes lands as `speedtest`, which would make
the `ookla` engine silently report different numbers in a different unit.
`install.sh` warns about this. Either remove it, or point the engine at the real
client:

```yaml
speed:
  ookla:
    binary: /usr/local/bin/speedtest
```

### Throughput far below the line rate

**Symmetric results around 92-95 Mbps are the signature of a 100 Mbit link**,
not of your broadband. Fast Ethernet delivers about 94 Mbps of usable TCP
throughput in each direction, so a monitor on such a link reports roughly the
same ceiling for download and upload no matter how fast the line behind it is.

The collector detects this: `status` prints the negotiated link speed, a result
within 15% of it logs a warning, and the dashboard's Download tile is labelled
*"capped by this machine's N Mbit link"*. Confirm directly:

```bash
ip -o route show default                  # which interface is in use
sudo ethtool eth0 | grep -E 'Speed|Duplex'
cat /sys/class/net/eth0/speed             # same number, no root needed
```

In order of likelihood:

1. **The Pi's own NIC**, by far the most common cause:

   | Board | Ethernet | Usable throughput |
   |---|---|---|
   | Pi 1, 2, 3, Zero 2 W | 100 Mbit | ≈ 94 Mbps |
   | Pi 3B+ | Gigabit over USB 2.0 | ≈ 230 Mbps |
   | Pi 4, 5, CM4 | true Gigabit | ≈ 940 Mbps |

   `cat /proc/device-tree/model` names the board. Nothing in software raises
   this ceiling — it is the hardware.
2. **A switch port or cable negotiating at 100 Mbit.** Cat5e or better, and
   check both ends — one bad pair drops a Gigabit link to Fast Ethernet.
3. **Wi-Fi.** Use Ethernet.
4. **Something else saturating the line** during the test.

If the ceiling is the Pi, the latency, jitter, packet-loss and outage
measurements are all still valid — only throughput is bounded. Either move the
collector to a Gigabit-capable Pi, or keep it and treat the throughput series as
a floor rather than a measurement.

Sanity-check against a laptop wired to the same switch before opening a ticket
with your ISP.

### The site shows "Waiting for the collector's first publish"

`data/manifest.json` is missing or unreadable — the branch has no data yet, or
the Pages build used the wrong branch. Check the branch contents on GitHub, then:

```bash
curl -s https://<your-domain>/data/manifest.json | head -c 200
```

A 404 with the branch clearly containing the file usually means Pages is serving
a different branch, or Jekyll ate the directory — confirm `.nojekyll` is
present at the root.

### The custom domain stopped working after a publish

The `CNAME` file is rewritten from `site.domain` on every publish because the
branch is force-pushed. If you set the domain only in the GitHub UI, set it in
`config.yaml` as well.

### Charts are empty for a range that should have data

Day files are dropped from the published set after `publish.keep_daily_days`
(400 by default); older periods only exist as monthly rollups, which the "All
time" view uses. The samples are still in SQLite — raise the setting and
republish to bring them back.

---

## Known limits

Not faults — things that will not change without different hardware or a
different provider, listed so they are not rediagnosed every few months.

| Limit | Cause | Only fix |
|---|---|---|
| Throughput caps near 94 Mbps | 100 Mbit NIC on Pi 1/2/3/Zero 2 W | A Pi 4, 5 or CM4 |
| No `ookla` engine on `armhf` | Ookla publishes no 32-bit ARM package | 64-bit OS, which needs a Pi 3 or later |
| Throughput ping reads high vs ICMP | HTTP-based, and TLS is slow on an ARMv7 core | Read the latency chart instead |
| Absolute TTFB reads high | Same CPU cost | Read the trend, not the value |
| 20–30% loss to the gateway | Router rate-limits ICMP to itself | Nothing; it is excluded from outage detection |
| speedtest.net offers no local servers | Your IP range is geolocated to another country | Nothing on this end — use the Cloudflare engine |

---

## Reading the database directly

**Always go in as the service user.** The database is in WAL mode, so opening it
creates `broadband.db-wal` and `broadband.db-shm` owned by whoever opened it.
Run `sudo sqlite3 …` once and those sidecars belong to root, after which the
collector cannot open its own database and every timer fails.

```bash
sudo -u broadband sqlite3 /var/lib/broadband-monitor/broadband.db
```

```sql
-- worst hours of the last week
SELECT datetime(ts,'unixepoch','localtime') AS t, target, rtt_avg, loss_pct
FROM latency WHERE ts > strftime('%s','now','-7 days') AND loss_pct > 0
ORDER BY loss_pct DESC, rtt_avg DESC LIMIT 20;

-- daily download average by engine (the engine comparison, in one query)
SELECT date(ts,'unixepoch','localtime') AS day, engine,
       COUNT(*) AS n, ROUND(AVG(down_mbps),1) AS avg_down, ROUND(MIN(down_mbps),1) AS worst
FROM speed WHERE ok = 1 GROUP BY day, engine ORDER BY day DESC LIMIT 30;

-- every test below the contractual minimum
SELECT datetime(ts,'unixepoch','localtime') AS t, engine, down_mbps, server, result_url
FROM speed WHERE ok = 1 AND down_mbps < 250 ORDER BY ts DESC;

-- how much data the tests have consumed this month
SELECT engine, ROUND(SUM(bytes_down + bytes_up)/1e9, 1) AS gb
FROM speed WHERE ts > strftime('%s','now','start of month') GROUP BY engine;
```

The last query is the one to run before arguing with anyone about the monitor's
own bandwidth cost.

---

## Backups

The database is the only irreplaceable thing on the Pi. SQLite in WAL mode needs
a proper online backup rather than `cp`:

```bash
sudo -u broadband sqlite3 /var/lib/broadband-monitor/broadband.db \
  ".backup '/var/lib/broadband-monitor/backup-$(date +%F).db'"
```

Worth a monthly cron to somewhere off the Pi — SD cards under a 24/7 write load
do eventually fail. Reducing `latency.interval_seconds` to 120 halves the write
rate if you want to be gentler on the card.

---

## Using the data with your ISP

If you need to escalate, the useful artefacts are:

1. **The 30-day panel** — average against advertised, and the percentage of
   tests below the guaranteed minimum. Under Ofcom's Broadband Speed Code of
   Practice, a speed persistently below the guaranteed minimum that is not fixed
   within 30 days lets you exit the contract without penalty.
2. **Ookla result URLs** — every Ookla row stores a `speedtest.net` result link.
   ISPs accept these; they will not accept a screenshot of your own dashboard.
3. **The outage list**, and the note that every non-LAN target was unreachable
   while the LAN gateway still answered — that distinguishes a line fault from a
   Wi-Fi or in-home problem.

Export what you need:

```sql
.mode csv
.output ~/broadband-evidence.csv
SELECT datetime(ts,'unixepoch','localtime'), engine, down_mbps, up_mbps, ping_ms, server, result_url
FROM speed WHERE ok = 1 AND ts > strftime('%s','now','-30 days') ORDER BY ts;
.output stdout
```

State plainly that measurements come from a device wired to the router — it is
the first thing they will ask, and it closes off the usual deflection.
