# Running on Windows 10 / 11

The collector runs on Windows unchanged: the same probes, the same SQLite
database, the same publish to GitHub Pages. Only the plumbing differs.

| | Linux (Pi, Jetson, Debian, Ubuntu) | Windows 10 / 11 |
|---|---|---|
| Scheduler | systemd timers | Task Scheduler, folder `\broadband-monitor\` |
| Runs as | `broadband` system user | `SYSTEM` (no console window, keeps running when you sign out) |
| Config | `/etc/broadband-monitor/config.yaml` | `C:\ProgramData\broadband-monitor\config.yaml` |
| Database | `/var/lib/broadband-monitor/broadband.db` | `C:\ProgramData\broadband-monitor\data\broadband.db` |
| Logs | `journalctl -u broadband-*` | `C:\ProgramData\broadband-monitor\logs\*.log` |
| Deploy key | `/var/lib/broadband-monitor/.ssh/` | `C:\ProgramData\broadband-monitor\ssh\` |
| ICMP | iputils `ping` | `ping.exe` |
| HTTP timing | `curl` | `curl.exe` (ships with Windows) |
| Link speed | `/sys/class/net` | `Get-NetAdapter` |

## 1. Prerequisites

From an **elevated** terminal (right-click → *Run as administrator*):

```powershell
winget install -e --id Python.Python.3.12
winget install -e --id Git.Git
```

Then open a *new* elevated terminal so both are on `PATH`.

Use the python.org build that winget installs, not the Microsoft Store Python.
The scheduled tasks run as `SYSTEM`, and `SYSTEM` cannot start the Store build.
The installer refuses the Store build and tells you why.

`curl.exe`, `ping.exe` and the OpenSSH client are already part of Windows 10
1803+ and Windows 11.

**Use Ethernet.** Wi-Fi measurements tell you about Wi-Fi, not your line. Also
stop the PC sleeping (*Settings → System → Power → Screen and sleep → Never* when
plugged in), because a sleeping PC records an outage every night.

## 2. Install

```powershell
git clone https://github.com/<you>/home-broadband.git $HOME\git\home-broadband
cd $HOME\git\home-broadband
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install-windows.ps1 -InstallOokla
```

The installer:

1. creates `.venv` in the clone and installs the requirements plus
   `speedtest-cli` and `tzdata`. Windows has no system timezone database, and
   without `tzdata` every day boundary would silently fall back to UTC.
2. with `-InstallOokla`, downloads the official Ookla CLI into
   `C:\ProgramData\broadband-monitor\bin`, which enables the `ookla` engine.
   Leave the switch off if you don't want to accept Ookla's licence.
3. seeds `C:\ProgramData\broadband-monitor\config.yaml` with Windows paths and
   the engines it found
4. generates a deploy key and locks its ACL to SYSTEM and Administrators.
   Windows OpenSSH refuses a key that anyone else can read.
5. records GitHub's SSH host key
6. marks the clone as a git `safe.directory`. The publisher runs `git pull` as
   SYSTEM, and the clone belongs to you.
7. initialises the database and registers the four scheduled tasks

Re-run it whenever you like. It is idempotent.

## 3. Configure, add the key, publish

```powershell
notepad C:\ProgramData\broadband-monitor\config.yaml
```

Set `publish.remote`, `site.domain` and `site.isp.*`. Then set the
**Router** target to your gateway: `ipconfig` shows it as *Default Gateway*.

**Sharing a site with another collector**, such as a wired Jetson: use the
same `publish.remote` and `site.domain`, and give this PC its own id and label.

```yaml
site:
  collector: "Windows PC (Wi-Fi)"   # shown on the switch; hides the hostname
publish:
  collector_id: "pc"                # its folder on the site: data/pc/
```

Each collector keeps the other's data when it publishes, and the dashboard
shows a Wired / Wi-Fi switch.

Add the public key the installer printed to the repo under **Settings → Deploy
keys** and tick **Allow write access**. Then:

```powershell
cd $HOME\git\home-broadband
$cfg = 'C:\ProgramData\broadband-monitor\config.yaml'
.venv\Scripts\python -m collector.main --config $cfg latency --dry-run
.venv\Scripts\python -m collector.main --config $cfg speed --force --engine cloudflare
Start-ScheduledTask -TaskPath '\broadband-monitor\' -TaskName publish
Get-Content C:\ProgramData\broadband-monitor\logs\publish.log -Tail 30
```

Finally, set **Settings → Pages → Source = Deploy from a branch → `gh-pages` / (root)**.
The DNS and custom-domain steps are the same as in [INSTALL.md](INSTALL.md).

## Day to day

```powershell
Get-ScheduledTask -TaskPath '\broadband-monitor\' | Get-ScheduledTaskInfo   # last run / result
Get-Content C:\ProgramData\broadband-monitor\logs\latency.log -Wait          # follow a log
.venv\Scripts\python -m collector.main --config $cfg status
```

`LastTaskResult` of `0` means success. `267009` means the task is running right
now. Each log rotates at 2 MB and keeps three old copies.

**Updating:** run `git pull` in the clone. The publisher also pulls before every
publish when `publish.sync_code` is true. Re-run the installer if
`requirements.txt` changed.

**Removing:** run `.\scripts\uninstall-windows.ps1`. Add `-Purge` to also delete
`C:\ProgramData\broadband-monitor`, which holds the database.

## Windows-specific notes

* **Sub-millisecond pings.** `ping.exe` prints `time<1ms` for a LAN hop, and the
  collector records that as 0.5 ms. Windows only reports whole milliseconds, so
  a router's RTT looks coarser than it would on Linux.
* **"Destination host unreachable" is loss.** `ping.exe` counts those replies
  as *received* and reports 0 % loss. The collector counts only replies that
  carry a round-trip time.
* **Non-English Windows** works. The ping parser matches numbers and
  punctuation, not words.
* **Throughput** on a desktop PC is rarely CPU-bound, so `speedtest-cli` keeps
  up far better than it does on a Pi.
* **Security software** that intercepts TLS can inflate the HTTP TLS and TTFB
  figures. Compare the trend over time rather than the absolute values.
* **`CRYPT_E_NO_REVOCATION_CHECK`** came from `curl.exe` failing an HTTPS check
  whenever it could not reach the CA's revocation server, which work networks
  often block. The HTTP probe now passes `--ssl-revoke-best-effort`. That
  ignores an unreachable revocation server but still rejects a certificate
  that has actually been revoked.
* **Proxies and VPNs.** The Cloudflare engine ignores the Windows proxy setting
  (`speed.cloudflare.use_system_proxy: false`). A work laptop with a corporate
  proxy or an always-on VPN still measures that path rather than your line, so
  disconnect the VPN, or don't treat the laptop's numbers as evidence against
  the ISP.
