#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════╗
║   NETPATH  🌍  — Network Path Visualizer & Infrastructure Mapper     ║
║   Traceroute + geolocation + ASN/ISP + interactive world map         ║
╚══════════════════════════════════════════════════════════════════════╝

Shows the real physical path your traffic takes — every hop's city,
country, ISP, ASN, and latency — then plots it on an interactive map.
Built for understanding satellite vs terrestrial routing, especially
across African infrastructure.

USAGE:
    python3 netpath.py <target>                 e.g. python3 netpath.py google.com
    python3 netpath.py <target> --map           also open an HTML world map
    python3 netpath.py <target> --save name     log this run to history
    python3 netpath.py --compare run1 run2       compare two saved runs
    python3 netpath.py <target> --max-hops 40    change hop limit

EXAMPLES:
    python3 netpath.py 8.8.8.8 --map
    python3 netpath.py nhif.or.ke --map --save starlink_to_kenya
    python3 netpath.py liquidtelecom.net --map

Dependencies (auto-installed): rich, requests
No API key needed. Uses ip-api.com (free, 45 req/min).
"""

import sys, os, re, json, time, shutil, socket, subprocess, platform, argparse, threading
from datetime import datetime
from pathlib import Path

# ── Auto-install ──────────────────────────────────────────────────────────────
def _need(mod, pip=None):
    try:
        __import__(mod)
    except ImportError:
        print(f"Installing {pip or mod}…")
        subprocess.check_call([sys.executable, "-m", "pip", "install",
                               pip or mod, "-q", "--break-system-packages"])

for m, p in [("rich", "rich"), ("requests", "requests")]:
    _need(m, p)

import requests
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn
from rich import box
from rich.text import Text

console = Console()

# ── Config ────────────────────────────────────────────────────────────────────
HISTORY_DIR = Path(__file__).resolve().parent / "netpath_history"
HISTORY_DIR.mkdir(exist_ok=True)
GEO_URL     = "http://ip-api.com/json/{ip}?fields=status,country,countryCode,region,regionName,city,lat,lon,isp,org,as,asname,query,mobile,proxy,hosting"
GEO_CACHE   = {}

# Flag emoji from country code
def flag(cc: str) -> str:
    if not cc or len(cc) != 2:
        return "  "
    return chr(0x1F1E6 + ord(cc[0].upper()) - 65) + chr(0x1F1E6 + ord(cc[1].upper()) - 65)

# ── Traceroute ────────────────────────────────────────────────────────────────
# Distros install traceroute into sbin, which isn't always on PATH for
# non-root users (cron, systemd units, some IDE terminals). Search there too.
EXTRA_TRACE_DIRS = ["/usr/sbin", "/sbin", "/usr/local/sbin", "/usr/bin", "/bin"]

# Package-manager → install command, first match wins.
INSTALL_HINTS = [
    ("apt-get", "sudo apt install traceroute"),
    ("dnf",     "sudo dnf install traceroute"),
    ("yum",     "sudo yum install traceroute"),
    ("pacman",  "sudo pacman -S traceroute"),
    ("apk",     "sudo apk add traceroute"),
    ("zypper",  "sudo zypper install traceroute"),
]

def find_traceroute():
    """Locate the traceroute/tracert binary. Returns a path, or None."""
    name = "tracert" if platform.system().lower() == "windows" else "traceroute"
    found = shutil.which(name)
    if found:
        return found
    for d in EXTRA_TRACE_DIRS:
        cand = os.path.join(d, name)
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return None

def _missing_traceroute_exit():
    """Say what's missing and how to install it, instead of a bare traceback."""
    system = platform.system().lower()
    name = "tracert" if system == "windows" else "traceroute"
    console.print(Panel(
        f"[bold red]{name} not found[/]\n\n"
        f"NetPath shells out to the system [bold]{name}[/] binary, but it isn't "
        f"installed or isn't on PATH.\n"
        f"[dim]Searched PATH plus: {', '.join(EXTRA_TRACE_DIRS)}[/]",
        title="[bold red]MISSING DEPENDENCY[/]", border_style="red"))
    if system == "windows":
        console.print(r"[yellow]tracert ships with Windows — check that "
                      r"%SystemRoot%\System32 is on PATH.[/]")
    elif system == "darwin":
        console.print("[yellow]macOS ships traceroute at /usr/sbin/traceroute — "
                      "check that directory is on PATH.[/]")
    else:
        hint = next((cmd for mgr, cmd in INSTALL_HINTS if shutil.which(mgr)), None)
        console.print(f"[yellow]Install with: {hint}[/]" if hint else
                      "[yellow]Install your distribution's 'traceroute' package.[/]")
    sys.exit(1)

def _traceroute_failed_exit(cmd, returncode, stderr_lines):
    """traceroute produced no hops — show what it actually said."""
    said = "\n".join(stderr_lines[:8]) if stderr_lines else "(no error output)"
    console.print(Panel(
        f"[bold red]traceroute produced no hops[/]  [dim](exit {returncode})[/]\n\n"
        f"[bold]Command:[/] [dim]{' '.join(cmd)}[/]\n\n"
        f"[bold]It reported:[/]\n{said}",
        title="[bold red]TRACEROUTE FAILED[/]", border_style="red"))
    sys.exit(1)

def run_traceroute(target: str, max_hops: int = 30):
    """Run system traceroute/tracert, yield (hop_num, ip, rtt_ms) tuples."""
    system = platform.system().lower()

    binary = find_traceroute()
    if binary is None:
        _missing_traceroute_exit()

    if system == "windows":
        cmd = [binary, "-d", "-h", str(max_hops), "-w", "1500", target]
    else:
        # -n numeric, -w wait, -q 1 query per hop for speed
        cmd = [binary, "-n", "-w", "2", "-q", "1", "-m", str(max_hops), target]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)
    except FileNotFoundError:
        # Vanished between lookup and exec, or a dangling alternatives symlink.
        _missing_traceroute_exit()

    ip_re  = re.compile(r'(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})')
    rtt_re = re.compile(r'([\d.]+)\s*ms')

    # Drain stderr on a thread: reading it after the stdout loop would deadlock
    # if traceroute ever filled the pipe buffer while we were still reading stdout.
    stderr_lines = []
    def _drain_stderr():
        for err in proc.stderr:
            err = err.strip()
            if err:
                stderr_lines.append(err)
    drainer = threading.Thread(target=_drain_stderr, daemon=True)
    drainer.start()

    yielded = 0
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue

        # hop number is first integer on the line
        hop_match = re.match(r'^\s*(\d+)', line)
        if not hop_match:
            continue
        hop_num = int(hop_match.group(1))

        ips  = ip_re.findall(line)
        rtts = rtt_re.findall(line)

        yielded += 1
        if ips:
            ip  = ips[0]
            rtt = float(rtts[0]) if rtts else None
            yield hop_num, ip, rtt
        else:
            # timeout hop (* * *)
            yield hop_num, None, None

    proc.wait()
    drainer.join(timeout=2)

    if not yielded:
        # Nothing parsed: bad flag, unreachable network, resolution failure.
        _traceroute_failed_exit(cmd, proc.returncode, stderr_lines)
    elif proc.returncode != 0 or stderr_lines:
        # Partial path — keep what we got, but don't swallow the complaint.
        for err in stderr_lines[:3]:
            # traceroute prefixes its own messages; don't double it up.
            err = re.sub(r'^tracer(oute|t):\s*', '', err, flags=re.I)
            console.print(f"[yellow]traceroute:[/] [dim]{err}[/]")

# ── Geolocation ───────────────────────────────────────────────────────────────
def geolocate(ip: str) -> dict:
    """Look up geo/ASN data for an IP, with caching + private-range handling."""
    if ip in GEO_CACHE:
        return GEO_CACHE[ip]

    # Private / reserved ranges
    if (ip.startswith("192.168.") or ip.startswith("10.") or ip.startswith("127.")
            or re.match(r'^172\.(1[6-9]|2\d|3[01])\.', ip)
            or ip.startswith("100.6") or ip.startswith("100.7")):  # CGNAT
        data = {"query": ip, "country": "Private/CGNAT", "countryCode": "",
                "city": "Local network", "isp": "Private range", "as": "",
                "asname": "", "lat": None, "lon": None, "_private": True}
        GEO_CACHE[ip] = data
        return data

    try:
        r = requests.get(GEO_URL.format(ip=ip), timeout=8)
        d = r.json()
        if d.get("status") == "success":
            GEO_CACHE[ip] = d
            return d
    except Exception:
        pass

    data = {"query": ip, "country": "Unknown", "countryCode": "",
            "city": "", "isp": "", "as": "", "asname": "",
            "lat": None, "lon": None}
    GEO_CACHE[ip] = data
    return data

# ── Speed test (download + upload + latency jitter) ──────────────────────────
def run_speedtest(status_cb=None):
    """
    Measure download, upload, and latency jitter using Cloudflare's speed
    endpoint. Hard time budget so it can never hang. Adapts test size to the
    measured link speed. status_cb(str) optionally reports progress.
    Returns dict with down_mbps, up_mbps, ping_ms, jitter_ms.
    """
    result = {"down_mbps": None, "up_mbps": None, "ping_ms": None, "jitter_ms": None}
    def say(msg):
        if status_cb: status_cb(msg)

    DOWN_URL = "https://speed.cloudflare.com/__down?bytes={n}"
    UP_URL   = "https://speed.cloudflare.com/__up"

    # ── Latency + jitter: 5 tiny requests ─────────────────────────────────────
    say("measuring latency…")
    try:
        pings = []
        for _ in range(5):
            t0 = time.time()
            requests.get(DOWN_URL.format(n=1000), timeout=6)
            pings.append((time.time() - t0) * 1000)
        pings.sort()
        result["ping_ms"] = sum(pings) / len(pings)
        diffs = [abs(pings[i] - pings[i-1]) for i in range(1, len(pings))]
        result["jitter_ms"] = sum(diffs) / len(diffs) if diffs else 0.0
    except Exception:
        pass

    # ── Download: 10MB, hard 15s cap, abort cleanly if slow ───────────────────
    say("testing download…")
    try:
        size = 10_000_000  # 10 MB — plenty to measure, quick on slow links
        t0 = time.time()
        downloaded = 0
        r = requests.get(DOWN_URL.format(n=size), stream=True, timeout=(6, 15))
        for chunk in r.iter_content(chunk_size=65536):
            downloaded += len(chunk)
            if time.time() - t0 > 15:   # hard wall — stop and use what we have
                break
        r.close()
        elapsed = time.time() - t0
        if elapsed > 0.3 and downloaded > 100_000:
            result["down_mbps"] = (downloaded * 8) / elapsed / 1e6
    except Exception:
        pass

    # ── Upload: 5MB, hard 15s cap ─────────────────────────────────────────────
    say("testing upload…")
    try:
        payload = b"0" * 5_000_000  # 5 MB
        t0 = time.time()
        requests.post(UP_URL, data=payload, timeout=(6, 15))
        elapsed = time.time() - t0
        if elapsed > 0.3:
            result["up_mbps"] = (len(payload) * 8) / elapsed / 1e6
    except Exception:
        pass

    say("done")
    return result


def display_speedtest(sp: dict):
    """Render a speed test result panel."""
    def fmt(v, unit, thresholds, higher_better=True):
        if v is None:
            return "[dim]n/a[/]"
        lo, hi = thresholds
        if higher_better:
            c = "bright_green" if v >= hi else "yellow" if v >= lo else "red"
        else:
            c = "bright_green" if v <= lo else "yellow" if v <= hi else "red"
        return f"[{c}]{v:.1f} {unit}[/]"

    down = fmt(sp["down_mbps"], "Mbps", (10, 50))
    up   = fmt(sp["up_mbps"],   "Mbps", (5, 20))
    ping = fmt(sp["ping_ms"],   "ms",   (40, 100), higher_better=False)
    jit  = fmt(sp["jitter_ms"], "ms",   (10, 30),  higher_better=False)

    body = (f"[bold]⬇ Download:[/] {down}    "
            f"[bold]⬆ Upload:[/] {up}\n"
            f"[bold]⏱ Ping:[/] {ping}    "
            f"[bold]〜 Jitter:[/] {jit}")
    console.print(Panel(body, title="[bold cyan]⚡ SPEED TEST[/]",
                        border_style="cyan", padding=(0,1)))


# ── Latency colour ────────────────────────────────────────────────────────────
def rtt_color(rtt):
    if rtt is None:  return "dim"
    if rtt < 30:     return "bright_green"
    if rtt < 80:     return "green"
    if rtt < 150:    return "yellow"
    return "red"

def rtt_bar(rtt, max_rtt):
    if rtt is None or max_rtt == 0:
        return "[dim]· · ·[/]"
    filled = int(min(rtt / max_rtt, 1.0) * 12)
    c = rtt_color(rtt)
    return f"[{c}]{'█'*filled}{'░'*(12-filled)}[/]"

# ── Main trace + display ──────────────────────────────────────────────────────
def trace_and_display(target: str, max_hops: int = 30, run_speed: bool = False):
    # Resolve target
    try:
        dest_ip = socket.gethostbyname(target)
    except socket.gaierror:
        console.print(f"[red]Cannot resolve {target}[/]")
        sys.exit(1)

    console.print(Panel.fit(
        f"[bold cyan]🌍  NETPATH[/]  —  tracing route to [bold yellow]{target}[/] "
        f"[dim]({dest_ip})[/]\n"
        f"[dim]{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  ·  max {max_hops} hops[/]",
        border_style="cyan"))

    hops = []
    with Progress(SpinnerColumn(), TextColumn("[cyan]{task.description}"),
                  console=console, transient=True) as prog:
        task = prog.add_task("Running traceroute…", total=None)
        for hop_num, ip, rtt in run_traceroute(target, max_hops):
            if ip:
                prog.update(task, description=f"Hop {hop_num}: {ip}  (geolocating…)")
                geo = geolocate(ip)
                time.sleep(0.4)  # stay under ip-api 45/min limit
            else:
                geo = None
            hops.append({"hop": hop_num, "ip": ip, "rtt": rtt, "geo": geo})
            # stop if we reached the destination
            if ip == dest_ip:
                break

    # ── Build the table ──────────────────────────────────────────────────────
    valid_rtts = [h["rtt"] for h in hops if h["rtt"] is not None]
    max_rtt = max(valid_rtts) if valid_rtts else 100

    t = Table(box=box.ROUNDED, border_style="cyan", show_lines=False,
              header_style="bold white on dark_cyan", expand=True,
              title=f"[bold cyan]NETWORK PATH  →  {target}[/]")
    t.add_column("#",        justify="right", style="dim", width=3)
    t.add_column("IP Address", style="bold", min_width=15)
    t.add_column("RTT",      justify="right", width=8)
    t.add_column("Latency",  width=14)
    t.add_column("",         justify="center", width=3)  # flag
    t.add_column("Location", min_width=18)
    t.add_column("ISP / Network", min_width=20)
    t.add_column("ASN",      style="dim", min_width=8)

    prev_country = None
    for h in hops:
        if h["ip"] is None:
            t.add_row(str(h["hop"]), "[dim]* * *  (no reply)[/]", "", "", "", "", "", "")
            continue

        geo = h["geo"]
        rtt = h["rtt"]
        cc  = geo.get("countryCode", "")
        country = geo.get("country", "")
        city    = geo.get("city", "")
        isp     = geo.get("isp") or geo.get("org", "")
        asn     = geo.get("as", "").split()[0] if geo.get("as") else ""

        loc = f"{city}, {country}" if city and not geo.get("_private") else country
        # Highlight country transitions
        country_changed = (country != prev_country and prev_country is not None
                           and not geo.get("_private"))
        loc_style = "[bold]" if country_changed else ""

        rc = rtt_color(rtt)
        rtt_str = f"[{rc}]{rtt:.1f}ms[/]" if rtt is not None else "[dim]—[/]"

        t.add_row(
            str(h["hop"]),
            h["ip"],
            rtt_str,
            rtt_bar(rtt, max_rtt),
            flag(cc),
            f"{loc_style}{loc}[/]" if loc_style else loc,
            isp[:28],
            asn,
        )
        if not geo.get("_private"):
            prev_country = country

    console.print(t)

    # ── Summary insights ─────────────────────────────────────────────────────
    countries = []
    for h in hops:
        if h["geo"] and not h["geo"].get("_private"):
            c = h["geo"].get("country")
            if c and c not in ("Unknown",) and (not countries or countries[-1] != c):
                countries.append(c)

    isps = []
    for h in hops:
        if h["geo"] and not h["geo"].get("_private"):
            i = h["geo"].get("isp") or h["geo"].get("org")
            if i and i not in isps:
                isps.append(i)

    final_rtt = next((h["rtt"] for h in reversed(hops) if h["rtt"] is not None), None)

    summary_lines = [
        f"[bold]Hops:[/] {len(hops)}   "
        f"[bold]Countries traversed:[/] {' → '.join(countries) if countries else 'n/a'}",
        f"[bold]End-to-end latency:[/] "
        f"[{rtt_color(final_rtt)}]{final_rtt:.1f}ms[/]" if final_rtt else "n/a",
        f"[bold]Networks:[/] {', '.join(isps[:5])}" + (" …" if len(isps) > 5 else ""),
    ]
    console.print(Panel("\n".join(summary_lines),
                        title="[bold]PATH SUMMARY[/]", border_style="green"))

    speed = None
    if run_speed:
        with Progress(SpinnerColumn(), TextColumn("[cyan]{task.description}"),
                      console=console, transient=True) as prog:
            task = prog.add_task("Speed test starting…", total=None)
            speed = run_speedtest(
                status_cb=lambda m: prog.update(task, description=f"Speed test — {m}")
            )
        display_speedtest(speed)

    return {"target": target, "dest_ip": dest_ip,
            "timestamp": datetime.now().isoformat(), "hops": hops,
            "countries": countries, "final_rtt": final_rtt,
            "speed": speed}

# ── HTML world map ────────────────────────────────────────────────────────────
def build_html_map(result: dict, open_after=True):
    """Generate an interactive Leaflet world map of the path."""
    pts = []
    for h in result["hops"]:
        geo = h.get("geo")
        if geo and geo.get("lat") is not None and geo.get("lon") is not None:
            pts.append({
                "hop":  h["hop"],
                "ip":   h["ip"],
                "rtt":  h["rtt"],
                "lat":  geo["lat"],
                "lon":  geo["lon"],
                "city": geo.get("city", ""),
                "country": geo.get("country", ""),
                "cc":   geo.get("countryCode", ""),
                "isp":  geo.get("isp") or geo.get("org", ""),
                "asn":  geo.get("as", ""),
            })

    pts_json = json.dumps(pts)
    target   = result["target"]
    ts       = result["timestamp"]

    html = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>NetPath — %TARGET%</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root { --bg:#0d1b2a; --panel:#12263a; --accent:#2E86AB; --gold:#F4A261; }
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:#e8e8e8; }
  #header { padding:14px 20px; background:linear-gradient(90deg,#0d1b2a,#1b4f72);
            border-bottom:2px solid var(--accent); }
  #header h1 { font-size:18px; color:#fff; }
  #header .sub { font-size:12px; color:var(--gold); margin-top:3px; }
  #map { height:60vh; width:100%; }
  #hops { padding:12px 20px; max-height:32vh; overflow-y:auto; }
  .hop { display:flex; align-items:center; gap:10px; padding:7px 10px; margin:3px 0;
         background:var(--panel); border-radius:6px; font-size:13px;
         border-left:3px solid var(--accent); }
  .hop .n { font-weight:bold; color:var(--gold); min-width:24px; }
  .hop .ip { font-family:monospace; min-width:120px; color:#8ecae6; }
  .hop .loc { flex:1; }
  .hop .rtt { font-weight:bold; min-width:60px; text-align:right; }
  .g { color:#4ade80; } .y { color:#fbbf24; } .r { color:#f87171; } .d { color:#64748b; }
  .leaflet-popup-content { font-size:13px; }
</style></head>
<body>
<div id="header">
  <h1>🌍 NetPath — route to %TARGET%</h1>
  <div class="sub">%TS% · %NHOPS% mapped hops · hover markers for detail</div>
</div>
<div id="map"></div>
<div id="hops"></div>
<script>
const pts = %PTS%;
const map = L.map('map', {worldCopyJump:true}).setView([20,0], 2);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution:'© OpenStreetMap © CARTO', subdomains:'abcd', maxZoom:19
}).addTo(map);

function rttClass(r){ if(r==null)return 'd'; if(r<30)return 'g'; if(r<80)return 'g'; if(r<150)return 'y'; return 'r'; }
function rttColor(r){ if(r==null)return '#64748b'; if(r<80)return '#4ade80'; if(r<150)return '#fbbf24'; return '#f87171'; }

const latlngs = [];
pts.forEach((p,i) => {
  latlngs.push([p.lat, p.lon]);
  const m = L.circleMarker([p.lat,p.lon], {
    radius: 7, color:'#fff', weight:1.5,
    fillColor: rttColor(p.rtt), fillOpacity:0.9
  }).addTo(map);
  m.bindPopup(
    `<b>Hop ${p.hop}</b> — ${p.ip}<br>`+
    `${p.city?p.city+', ':''}${p.country}<br>`+
    `<b>ISP:</b> ${p.isp}<br>`+
    `<b>ASN:</b> ${p.asn}<br>`+
    `<b>RTT:</b> ${p.rtt!=null?p.rtt+' ms':'n/a'}`
  );
  m.bindTooltip(`${p.hop}`, {permanent:true, direction:'center', className:'hoplabel'});
});

if(latlngs.length>1){
  const line = L.polyline(latlngs, {color:'#2E86AB', weight:2.5, opacity:0.7, dashArray:'6,6'}).addTo(map);
  // animated direction arrows via decorator-less approach: just fit bounds
  map.fitBounds(line.getBounds().pad(0.2));
}

// hop list
const hopsDiv = document.getElementById('hops');
pts.forEach(p => {
  const div = document.createElement('div');
  div.className = 'hop';
  div.innerHTML =
    `<span class="n">${p.hop}</span>`+
    `<span class="ip">${p.ip}</span>`+
    `<span class="loc">${p.city?p.city+', ':''}<b>${p.country}</b> — ${p.isp}</span>`+
    `<span class="rtt ${rttClass(p.rtt)}">${p.rtt!=null?p.rtt+' ms':'—'}</span>`;
  div.onclick = () => { map.setView([p.lat,p.lon], 5); };
  hopsDiv.appendChild(div);
});
</script>
</body></html>"""

    html = (html.replace("%TARGET%", target)
                .replace("%TS%", ts[:19].replace("T", " "))
                .replace("%NHOPS%", str(len(pts)))
                .replace("%PTS%", pts_json))

    out = HISTORY_DIR / f"map_{target.replace('.','_').replace('/','_')}_{datetime.now().strftime('%H%M%S')}.html"
    out.write_text(html, encoding="utf-8")
    console.print(f"[green]🗺  Map written to:[/] {out}")

    if open_after:
        import webbrowser
        webbrowser.open(f"file://{out}")
    return out

# ── Save / compare ────────────────────────────────────────────────────────────
def save_run(result: dict, name: str):
    # strip geo cache bloat, keep essentials
    slim = {
        "target": result["target"], "dest_ip": result["dest_ip"],
        "timestamp": result["timestamp"], "countries": result["countries"],
        "final_rtt": result["final_rtt"], "speed": result.get("speed"),
        "hops": [{"hop": h["hop"], "ip": h["ip"], "rtt": h["rtt"],
                  "geo": (h["geo"] if h["geo"] else None)} for h in result["hops"]],
    }
    f = HISTORY_DIR / f"{name}.json"
    f.write_text(json.dumps(slim, indent=2), encoding="utf-8")
    console.print(f"[green]💾 Saved run as:[/] {f}")

def compare_runs(name1: str, name2: str):
    f1, f2 = HISTORY_DIR / f"{name1}.json", HISTORY_DIR / f"{name2}.json"
    if not f1.exists() or not f2.exists():
        console.print(f"[red]One or both saved runs not found in {HISTORY_DIR}[/]")
        return
    r1, r2 = json.loads(f1.read_text()), json.loads(f2.read_text())

    t = Table(title=f"[bold]COMPARISON: {name1}  vs  {name2}[/]",
              box=box.ROUNDED, border_style="magenta", header_style="bold white on dark_magenta")
    t.add_column("Metric", style="bold")
    t.add_column(name1, justify="right")
    t.add_column(name2, justify="right")
    t.add_row("Target", r1["target"], r2["target"])
    t.add_row("Hops", str(len(r1["hops"])), str(len(r2["hops"])))
    t.add_row("End latency",
              f"{r1['final_rtt']:.1f}ms" if r1['final_rtt'] else "n/a",
              f"{r2['final_rtt']:.1f}ms" if r2['final_rtt'] else "n/a")
    t.add_row("Countries",
              " → ".join(r1["countries"]), " → ".join(r2["countries"]))

    # Speed rows if available
    s1 = r1.get("speed") or {}
    s2 = r2.get("speed") or {}
    def _sp(v, unit="Mbps"):
        return f"{v:.1f} {unit}" if v is not None else "n/a"
    if s1 or s2:
        t.add_row("⬇ Download", _sp(s1.get("down_mbps")), _sp(s2.get("down_mbps")))
        t.add_row("⬆ Upload",   _sp(s1.get("up_mbps")),   _sp(s2.get("up_mbps")))
        t.add_row("〜 Jitter",   _sp(s1.get("jitter_ms"),"ms"), _sp(s2.get("jitter_ms"),"ms"))
    console.print(t)

# ── Combined multi-path comparison map ────────────────────────────────────────
def build_comparison_map(run_names: list, open_after=True):
    """Draw multiple saved runs on one world map, each path a different color."""
    PALETTE = ["#2E86AB", "#F4A261", "#2A9D5C", "#E63946", "#9D4EDD", "#00B4D8"]

    paths = []
    for i, name in enumerate(run_names):
        f = HISTORY_DIR / f"{name}.json"
        if not f.exists():
            console.print(f"[red]Run '{name}' not found, skipping[/]")
            continue
        r = json.loads(f.read_text())
        pts = []
        for h in r["hops"]:
            geo = h.get("geo")
            if geo and geo.get("lat") is not None and geo.get("lon") is not None:
                pts.append({
                    "hop": h["hop"], "ip": h["ip"], "rtt": h["rtt"],
                    "lat": geo["lat"], "lon": geo["lon"],
                    "city": geo.get("city", ""), "country": geo.get("country", ""),
                    "isp": geo.get("isp") or geo.get("org", ""),
                })
        sp = r.get("speed") or {}
        paths.append({
            "name": name, "color": PALETTE[i % len(PALETTE)],
            "points": pts, "final_rtt": r.get("final_rtt"),
            "countries": r.get("countries", []), "hops": len(r["hops"]),
            "down": sp.get("down_mbps"), "up": sp.get("up_mbps"),
            "jitter": sp.get("jitter_ms"),
        })

    if not paths:
        console.print("[red]No valid runs to map[/]")
        return

    paths_json = json.dumps(paths)

    html = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>NetPath Comparison</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:-apple-system,Segoe UI,Roboto,sans-serif; background:#0d1b2a; color:#e8e8e8; }
  #header { padding:14px 20px; background:linear-gradient(90deg,#0d1b2a,#1b4f72);
            border-bottom:2px solid #2E86AB; }
  #header h1 { font-size:18px; color:#fff; }
  #header .sub { font-size:12px; color:#F4A261; margin-top:3px; }
  #map { height:64vh; width:100%; }
  #legend { padding:12px 20px; display:flex; gap:20px; flex-wrap:wrap; }
  .legcard { background:#12263a; border-radius:8px; padding:10px 14px; min-width:200px;
             border-left:4px solid; }
  .legcard h3 { font-size:14px; margin-bottom:4px; }
  .legcard .m { font-size:12px; color:#8ecae6; }
  .legcard .spd { font-size:12px; color:#4ade80; margin-top:4px; font-weight:600; }
  .legcard .c { font-size:11px; color:#94a3b8; margin-top:3px; }
</style></head>
<body>
<div id="header">
  <h1>🌍 NetPath — Multi-Path Comparison</h1>
  <div class="sub">Same destination, different uplinks · each colored line is one path out of Moshi</div>
</div>
<div id="map"></div>
<div id="legend"></div>
<script>
const paths = %PATHS%;
const map = L.map('map', {worldCopyJump:true}).setView([10,10], 2);
L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
  attribution:'© OpenStreetMap © CARTO', subdomains:'abcd', maxZoom:19
}).addTo(map);

let allBounds = [];
paths.forEach(path => {
  const latlngs = [];
  path.points.forEach(p => {
    latlngs.push([p.lat, p.lon]);
    allBounds.push([p.lat, p.lon]);
    L.circleMarker([p.lat,p.lon], {
      radius:6, color:'#fff', weight:1.2, fillColor:path.color, fillOpacity:0.9
    }).addTo(map).bindPopup(
      `<b>${path.name} — Hop ${p.hop}</b><br>${p.ip}<br>`+
      `${p.city?p.city+', ':''}${p.country}<br><b>ISP:</b> ${p.isp}<br>`+
      `<b>RTT:</b> ${p.rtt!=null?p.rtt+' ms':'n/a'}`
    );
  });
  if(latlngs.length>1){
    L.polyline(latlngs, {color:path.color, weight:3, opacity:0.75, dashArray:'8,6'}).addTo(map);
  }
});

if(allBounds.length) map.fitBounds(allBounds, {padding:[40,40]});

const leg = document.getElementById('legend');
paths.forEach(path => {
  const div = document.createElement('div');
  div.className = 'legcard';
  div.style.borderLeftColor = path.color;
  const dn = path.down!=null ? path.down.toFixed(0)+' Mbps' : 'n/a';
  const up = path.up!=null ? path.up.toFixed(0)+' Mbps' : 'n/a';
  const jit = path.jitter!=null ? path.jitter.toFixed(0)+' ms jitter' : '';
  div.innerHTML =
    `<h3 style="color:${path.color}">${path.name}</h3>`+
    `<div class="m">${path.final_rtt!=null?path.final_rtt+' ms latency':'n/a'} · ${path.hops} hops</div>`+
    `<div class="spd">⬇ ${dn}  ⬆ ${up}${jit?'  · '+jit:''}</div>`+
    `<div class="c">${path.countries.join(' → ')}</div>`;
  leg.appendChild(div);
});
</script>
</body></html>"""

    html = html.replace("%PATHS%", paths_json)
    out = HISTORY_DIR / f"comparison_{'_vs_'.join(run_names)}.html"
    out.write_text(html, encoding="utf-8")
    console.print(f"[green]🗺  Comparison map written to:[/] {out}")
    if open_after:
        import webbrowser
        webbrowser.open(f"file://{out}")
    return out


# ── CLI ───────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="NetPath — network path visualizer")
    ap.add_argument("target", nargs="?", help="hostname or IP to trace")
    ap.add_argument("--map", action="store_true", help="generate + open HTML world map")
    ap.add_argument("--save", metavar="NAME", help="save this run to history")
    ap.add_argument("--compare", nargs=2, metavar=("RUN1", "RUN2"), help="compare two saved runs")
    ap.add_argument("--compare-map", nargs="+", metavar="RUN", help="draw 2+ saved runs on one map")
    ap.add_argument("--max-hops", type=int, default=30, help="max hops (default 30)")
    ap.add_argument("--speed", action="store_true", help="also run a download/upload/jitter speed test")
    ap.add_argument("--no-open", action="store_true", help="write map but don't auto-open")
    args = ap.parse_args()

    if args.compare:
        compare_runs(*args.compare)
        return

    if args.compare_map:
        build_comparison_map(args.compare_map, open_after=not args.no_open)
        return

    if not args.target:
        ap.print_help()
        return

    result = trace_and_display(args.target, args.max_hops, run_speed=args.speed)

    if args.save:
        save_run(result, args.save)
    if args.map:
        build_html_map(result, open_after=not args.no_open)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[cyan]Interrupted.[/]")
