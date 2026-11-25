# Wi-Fi: troubleshooting notes and decisions

A record of diagnosing slow Wi-Fi on the home network in November 2026, what
was measured, what was ruled out, and what was decided. The tooling built along
the way (the `lan` iperf3 test and `network.home_gateways`) is described at the
end, with how to repeat the measurements.

## The question

The line had just gone from ~155 Mbps to ~930 Mbps symmetric. The wired
collector (a Jetson, `tegra-ubuntu`) saw the new speed straight away; the
Windows laptop collector, on Wi-Fi, reported a fraction of it. Is that the
line, the Wi-Fi, or the laptop?

## How it was measured

Two collectors on the same line answer most of it on their own. One more test
isolates the Wi-Fi completely: **iperf3 between the laptop (Wi-Fi) and the
Jetson (wired)**. The internet is not involved, so whatever that reads is the
Wi-Fi alone.

```bash
# on the wired box
iperf3 -s -D
# on the Wi-Fi laptop: download (to the laptop), then upload
iperf3 -c <wired box> -t 10 -P 4 -R
iperf3 -c <wired box> -t 10 -P 4
```

The radio's own view comes from `netsh wlan show interfaces` (Windows) or
`iw dev <if> link` (Linux): band, channel, signal (RSSI) and the negotiated
receive and transmit link rates.

## What was measured

| | Download | Upload |
|---|---|---|
| Jetson, wired, internet speed test | ~930 Mbps | ~930 Mbps |
| Laptop, Wi-Fi, internet speed test | ~220–240 | ~470 |
| **Laptop → Jetson over Wi-Fi only (iperf3), usual desk** | **245** | **475** |
| Laptop → Jetson over Wi-Fi only, 1 m from the router, no walls | 402 | 393 |

Radio, throughout: 5 GHz, **channel 52**, 802.11ac (Wi-Fi 5), 80 MHz. The
laptop transmitted at 866.7 Mbps link rate but received at only **234–351**.
RSSI at the desk read −36 to −52 dBm.

## Findings

1. **The line is not the bottleneck.** Wired reaches ~930 Mbps both ways.
2. **The Wi-Fi is the whole of the shortfall.** The Wi-Fi-only iperf3 result
   matched the laptop's internet results almost exactly.
3. **Download is the weak direction, and it is the router's side.** The laptop
   sends at full link rate; the router sends to it at a third of that. Download
   was the same with one stream as with four, with no retransmissions —
   interference would have shown up as retries and variance.
4. **Distance costs about 40% of download.** At 1 m the result rose to ~400
   Mbps each way, which is roughly the practical ceiling of a 2-stream Wi-Fi 5
   router (400–550 Mbps). So even perfect placement could not get near the line.
5. **The 5 GHz channel is shared.** A neighbour's router (and a hidden network
   on the same unit) sit on channel 52; 100 and 108 are used by others;
   **36–48 were empty**.

## Ruled out

* **Windows' TCP receive window** — `netsh int tcp show global` shows
  auto-tuning `normal`.
* **A second access point** — only one BSSID broadcasts the network, so the
  laptop was on the router, not on a far mesh node.
* **iperf3 UDP results** — UDP tests on Windows read ~250 Mbps regardless of
  conditions while TCP varied, so they measure iperf3-on-Windows, not the radio.
  Use TCP.

## Constraints: the ISP hub

The router is the ISP-supplied **Vodafone Wi-Fi Hub** (2019, Wi-Fi 5,
firmware 19.4.x). On current firmware:

* **There is no channel setting** — not in the web interface (Expert mode,
  with "Split Wi-Fi bands" on or off), and not in the Vodafone Broadband app.
  Vodafone's "Wi-Fi Doctor" picks channels itself. Vodafone support can change
  it (191 from a Vodafone phone, 03333 040 191 otherwise).
* Newer hubs exist (Power Hub, Wi-Fi 6; Ultra Hub, Wi-Fi 6E), which may be
  available on request with a gigabit plan.

## A side discovery: the hub's flood protection

After long, saturating iperf3 runs (and especially a 600 Mbit/s UDP test) the
hub **stopped answering pings from the laptop — and dropped its pings to the
internet — for about 30 minutes**, then recovered on its own. Web traffic and
speed tests were unaffected, and the wired Jetson was never affected.

It matters because the collectors measure reachability with ping: for those 30
minutes the laptop collector logged every target as unreachable, which reads
as an outage on the dashboard. Short tests (5 s per direction) and the
collector's own hourly internet speed tests did not trigger it.

**Rule of thumb on this network: keep LAN throughput tests short, and do not
run UDP floods from a collector.**

## Decisions

| Decision | Why |
|---|---|
| **Measure the Wi-Fi separately from the line** — a `lan` collector test: iperf3 to the wired Jetson, hourly at :45, recording band/channel/RSSI/link rates with each result, charted as "Wi-Fi throughput" on the dashboard. | Internet tests from a Wi-Fi collector mix the two; side by side they show which one is the bottleneck, and the effect of any change over time. |
| **5 seconds per direction**, sharing the speed-test lock. | Long runs trip the hub's flood protection (above) and create false outages. Never overlaps an internet test, which would saturate the same Wi-Fi. |
| **Only measure on the home network** — `network.home_gateways` lists the home router's IP and MAC; when the laptop is elsewhere (e.g. tethered to a phone) its sweeps and tests are skipped. | 4G and other networks' figures were landing in the line's history. A MAC is the stronger test: 192.168.1.x addresses are on every network. |
| **Add a Wi-Fi 6 access point: TP-Link Archer AX55 (AX3000)**, cabled to the hub, in **Access Point mode**, near the desk. | The hub caps Wi-Fi at ~400 Mbps even at 1 m and offers no channel control. AP mode leaves the hub routing (no double NAT) and the home gateway unchanged, so `home_gateways` stays valid. |
| **5 GHz on channel 36**, trying **160 MHz** first and falling back to **80 MHz** if unstable. | 36–48 were empty. 160 MHz from 36 overlaps 52–64, where the neighbour is; 80 MHz on 36 avoids them. The laptop (Intel AX211) supports 160 MHz. |

## Baseline to beat

From the usual desk, Wi-Fi only (iperf3 to the wired Jetson), on the Vodafone
hub: **245 Mbps down / 475 up**; a later 5-second collector run read 217 / 290.
At 1 m: ~400 / ~400.

The first scheduled `lan` run (25 Nov 2026, 14:51, from the desk) read
**225 down / 447 up** — 5 GHz channel 52, −61 dBm, link rate 390 in / 867 out.
That is the "before" point on the dashboard's Wi-Fi throughput chart.

## Status (25 Nov 2026)

* The Jetson runs `iperf3 -s` on port 5201, started at boot from its crontab.
* The Windows laptop collector runs the `lan` test hourly at about :45 and
  skips it when off the home network.
* The dashboard charts the results as **Wi-Fi throughput**.

## Repeating the measurement

On the wired box, keep an iperf3 server running (survives reboots):

```bash
crontab -e
@reboot /usr/bin/iperf3 -s -D -p 5201
```

On the Wi-Fi collector, in `config.yaml`:

```yaml
lan:
  enabled: true
  server: "192.168.1.184"   # the wired box
```

Then on Windows re-run `install-windows.ps1` (registers the `lan` task); on
Linux `install.sh` (enables `broadband-lan.timer`). A one-off run:

```bash
python -m collector.main lan --dry-run
```

Results appear on the dashboard's **Wi-Fi throughput** chart with the radio
details of the latest run.

## Open items

* Set up the Archer AX55 and measure it against the baseline above.
