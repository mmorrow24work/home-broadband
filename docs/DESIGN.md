# Design notes

Why things are the way they are. Useful if you want to change them.

## Short-lived processes, not a daemon

Every collector run is a one-shot process started by a systemd timer. There is
no long-running loop, no internal scheduler and no state held in memory, so a
crash or a hang costs exactly one sample and the next timer fires regardless.
`journalctl -u broadband-latency` gives per-run history for free, and
`systemd-analyze` shows drift. A daemon would need its own supervision, its own
scheduling and its own logging to reach the same place.

The cost is process startup roughly once a minute — about 60 ms of Python on a
Pi 4, which is noise next to a 5-echo ICMP probe.

## The system binaries do the measuring

`ping` and `curl` do the actual work rather than Python libraries:

* **`ping`** uses ICMP datagram sockets on modern Linux, so it needs no
  privileges once `net.ipv4.ping_group_range` covers the service group. A raw
  socket implementation would need `CAP_NET_RAW` on a process that also makes
  outbound HTTP requests — a much worse trade.
* **`curl --write-out`** gives a DNS / TCP / TLS / TTFB split that is genuinely
  hard to reproduce accurately from `requests`, and it is on every Pi already.

The corresponding risk is output-format drift between distributions, so the
parsers are the most heavily tested part of the codebase — including BusyBox's
wording, the `+N errors` variant, and fractional loss percentages.

## Three throughput engines, rotated

No single throughput test is trustworthy on its own. Ookla's client is the one
an ISP recognises but has no armhf build, so a 32-bit Pi cannot run it at all.
Debian's `speedtest-cli` installs anywhere and hits the same servers, but it is
pure Python and becomes the bottleneck itself on a Pi above roughly 200 Mbps.
Cloudflare needs no install but measures a different path.

Rotating all three turns that weakness into a signal: when they agree, the line
is the constraint; when only `speedtest-cli` is low, the Pi is; when Ookla and
Cloudflare diverge, the test path is. The rotation is stateless — the next
engine is derived from the last row in the `speed` table, so a failed run still
advances it and dropping an engine from the config recovers immediately.

One trap is worth naming: the two speedtest.net clients report throughput in
different units. Ookla's gives **bytes** per second, `speedtest-cli` gives
**bits** per second. The conversion lives in exactly one function per engine,
and both are pinned to the same line speed in the same test, because an 8x
error here would look plausible enough to survive review.

## The Cloudflare engine is implemented here, not wrapped

Rather than depending on a third-party wrapper whose API has changed repeatedly,
`probes/cloudflare.py` talks to `speed.cloudflare.com` directly. That makes the
measurement method explicit and stable:

* **Latency** — 12 small requests; report the minimum (least contaminated by
  scheduling noise) and the mean absolute successive difference as jitter, which
  is how Ookla defines it, so the two engines' numbers are comparable.
* **Throughput** — N concurrent streams incrementing a shared byte counter, with
  throughput measured only over the window *after* a warm-up. Without that
  warm-up, TCP slow start and the congestion-window ramp are averaged into the
  result and a fast line reads 20–30% slow. This is the single most common bug
  in home-grown speed tests.

## SQLite is the system of record; JSON is disposable

Every raw sample lands in SQLite on the Pi and stays there for
`database.retention_days`. The published JSON is a lossy, derived view: 48 hours
at full resolution, older days at 5-minute buckets, older months at hourly.

Because it is derived, published history has no value — which is what licenses
the force-push. The publisher rewrites `gh-pages` as one orphan commit each
time, so the repository stays the size of one snapshot forever instead of
accumulating a commit an hour. Anything you want back at full resolution is a
`config.yaml` change and a republish away.

## Bucketing hides short outages, so outages are computed before bucketing

A 90-second outage inside a 5-minute bucket averages to 30% loss, not 100% — so
outage detection would silently stop working on any view older than 48 hours if
the browser derived it from bucketed data. Instead the publisher computes the
outage list at full resolution and ships it inside each day and month file; the
dashboard derives outages itself only for the full-resolution recent window.

Removing a target from `config.yaml` does not retract its history: the exporter
selects by time window, not by the current target list, so a retired target
keeps its series until it falls out of the window. That is intentional — a chart
should not silently rewrite the past — but it does mean the "Monitored targets"
table (which reflects the current config) can list fewer names than the charts
show.

The definition is deliberately conservative: an outage is when **every non-LAN
target** is unreachable in the same sweep. One target going dark is that
target's problem, or that path's. The LAN target is what separates "the internet
is down" from "the router is down", and it is why the example config includes
the gateway.

## Two accounts share the installation

The collector runs as an unprivileged `broadband` user, but with `--in-place`
the code lives in a human's home directory and is owned by them. Three separate
things break on that boundary, and each is fixed once rather than worked around
repeatedly:

* **systemd's `ProtectHome=yes` masks `/home` outright**, so the service cannot
  see its own code. The installer generates the units rather than copying them
  and relaxes that setting only when the app directory is under a home.
* **git refuses a repository owned by another user** ("dubious ownership"), and
  because the publisher treats a failed `git pull` as non-fatal, `sync_code`
  would fail *silently* forever. The installer marks the directory safe for the
  service user specifically, and sets `core.sharedRepository=group` so objects
  each account writes stay writable by the other.
* **Copying the checkout replicates its permissions.** `shutil.copytree` carries
  the source's mode onto the destination, and a setgid directory — which is
  exactly what the shared-repo fix creates — cannot be reproduced by the
  publisher in its own scratch area, so every publish aborted with EPERM. The
  publisher copies file *contents* only; directory metadata is meaningless on a
  scratch tree or a git branch.

There is a simpler configuration available for anyone who does not want the
boundary at all: install to `/opt` (the default), where the service user owns
everything, or set `sync_code: false` so nothing but the human ever writes the
checkout.

## The SQLite file belongs to the service user

The database runs in WAL mode, so opening it creates `-wal` and `-shm` sidecars
owned by whoever opened it. A single `sudo sqlite3` leaves root-owned sidecars
that the collector can no longer write, and every timer then fails with an error
naming the database rather than the command that caused it. Every documented
example therefore reads `sudo -u broadband sqlite3`.

## Charts

The palette is the validated reference instance from Anthropic's data-viz
guidance: eight categorical hues in a fixed order, with separate steps selected
for the dark surface rather than an automatic flip. Both sets pass the OKLab
CVD-separation, normal-vision and lightness-band gates; the light set carries a
sub-3:1 contrast warning on three hues, mitigated by the always-present legend
and the value tables, so identity is never carried by colour alone.

Three rules the code enforces:

* **Colour follows the entity, not its rank.** A target's colour comes from its
  index in the config's target list, so hiding a series in the legend does not
  repaint the survivors, and the same host is the same colour in the latency,
  loss and TTFB charts. Two exceptions, both deliberate. With more targets than
  the palette's eight slots, two entities on the *same* chart could land on one
  hue; within-chart uniqueness wins, and the later series moves to a free slot —
  so a target on nine-plus configs may differ between charts. And a target
  removed from `config.yaml` still has published history, so it keeps a series
  until it ages out; it is given a slot *after* the configured targets rather
  than defaulting to slot 0 and impersonating the first one.
* **One axis per chart.** Download and upload share a chart because both are
  Mbps; latency and loss do not, because they are not.
* **Clip loudly, never silently.** The latency axis clips at the 99.5th
  percentile when outliers would otherwise flatten the everyday band, and the
  caption states the clip point and the true peak.

## What was considered and rejected

| Option | Why not |
|---|---|
| Grafana + Prometheus/InfluxDB on the Pi | Far heavier than the job needs, and it cannot be published as a static page. This whole system is ~1,500 lines and a 17 MB repo. |
| Commit every result | Thousands of commits a year and a repo dominated by history of disposable data. |
| Push to a time-series service | Adds an account, an API key and a dependency that can disappear. GitHub Pages is already there and free. |
| Only the official Ookla client | It has no armhf packages, so 32-bit Raspberry Pi OS could not run this project at all. `speedtest-cli` covers that gap. |
| Only `speedtest-cli` | Unmaintained upstream, no jitter or loss, and CPU-bound on a Pi. Fine as one of three; poor as the only source of truth. |
| A JS framework for the dashboard | A build step, a `node_modules`, and a CI job, to render four charts and three tables. |
| `iperf3` against a VPS | The most accurate option if you have a well-connected endpoint, but it measures your path to *that box*, and an ISP will not accept it as evidence. Worth adding as a third engine if you already run one. |
