# netpath

🌍 A network path visualizer. `netpath` traces the route to a host, enriches
every hop with geolocation and ASN/ISP data, and plots the real physical path
your traffic takes on an interactive world map.

Built for understanding satellite vs terrestrial routing.

## Features

- **Traceroute** — shells out to the system `traceroute` (`tracert` on Windows),
  numeric mode, one probe per hop, stops early when the destination is reached
- **Geolocation + ASN** — resolves each hop to city / country / ISP / ASN via
  [ip-api.com](http://ip-api.com), with in-run caching and private/CGNAT range
  detection
- **Terminal path table** — colour-coded RTT with latency bars, country flags,
  and highlighted country transitions
- **Speed test** — optional download / upload / ping / jitter measurement
- **Interactive maps** — Leaflet world map of the path, hand-written into a
  single self-contained HTML file
- **Run history + comparison** — save runs to JSON, diff two of them in a
  table, or draw several paths on one colour-coded map

## Requirements

- Python 3.9+
- `traceroute` (or `tracert` on Windows)

  ```bash
  sudo apt install traceroute      # Debian/Ubuntu
  sudo dnf install traceroute      # Fedora
  brew install traceroute          # macOS (also ships /usr/sbin/traceroute)
  ```

  **No root required.** Linux `traceroute` uses unprivileged UDP probes by
  default. netpath searches `PATH` plus `/usr/sbin`, `/sbin`, `/usr/local/sbin`,
  `/usr/bin`, and `/bin`, since distros install into sbin — which isn't always
  on a non-root `PATH`. If it can't be found, you get an install hint for your
  package manager rather than a traceback.

- Internet access — for hop geolocation, for the speed test, and for *viewing*
  the map (Leaflet and the CARTO basemap load from CDNs).

## Installation

```bash
git clone <repo-url> netpath
cd netpath
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Only two dependencies: `rich` and `requests`. The script also self-installs them
on first run if they're missing (`pip install --break-system-packages`), so it
works standalone outside a venv.

## Usage

```bash
python3 netpath.py <target>                   # trace + terminal table
python3 netpath.py <target> --map             # also write + open an HTML map
python3 netpath.py <target> --speed           # also run a speed test
python3 netpath.py <target> --save NAME       # log this run to history
python3 netpath.py --compare RUN1 RUN2        # diff two saved runs
python3 netpath.py --compare-map RUN...       # draw 2+ saved runs on one map
```

| Flag | Effect |
|---|---|
| `--map` | Generate an interactive HTML map and open it in a browser |
| `--speed` | Measure download, upload, ping, and jitter |
| `--save NAME` | Save the run as `netpath_history/NAME.json` |
| `--compare RUN1 RUN2` | Table diff of two saved runs |
| `--compare-map RUN...` | Multi-path map, one colour per run |
| `--max-hops N` | Hop limit (default 30) |
| `--no-open` | Write the map but don't launch a browser |

### Examples

```bash
python3 netpath.py 8.8.8.8 --map
python3 netpath.py nhif.or.ke --map --speed --save starlink_to_kenya
python3 netpath.py --compare-map starlink_to_kenya fibre_to_kenya
```

## Output

Every run prints a live hop table and a path summary (hop count, countries
traversed, end-to-end latency, networks crossed). With `--map`, an HTML file is
written to `netpath_history/` and opened in your browser — pan/zoom the world
map, click markers for per-hop ISP and ASN detail, click the hop list to fly to
a location.

Saved runs and generated maps all live in `netpath_history/`, created next to
the script.

## Notes

- **Geolocation is approximate.** IP-to-location databases place a hop at the
  registered location of its network, which is often the operator's head office
  rather than the router's physical position. Treat the map as network topology,
  not GPS.
- **Rate limit.** ip-api.com's free tier allows 45 requests/minute; the script
  sleeps 0.4s per hop to stay under it. No API key needed.
- **Privacy.** Hop IPs are sent to ip-api.com over plain HTTP to be geolocated.
- **The speed test measures your connection, not the traced path** — it runs
  against Cloudflare's speed endpoint, independent of the target. Both
  directions are capped at a hard 15s so a slow link can't hang the run.

## Project layout

```
netpath/
├── README.md
├── requirements.txt
├── netpath.py           # everything: trace, geo, speed, maps, history
└── netpath_history/     # saved runs (.json) + generated maps (.html)
```

## License

MIT
