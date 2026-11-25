#!/usr/bin/env python3
"""
Alliance Broadband usage -> CSV.

Fetches the four usage graphs (daily/weekly/monthly/yearly) from the IPACCT
customer portal (/0/up/grph on your Alliance gateway), reads the traffic curves out
of the SVGs, integrates bits/s over time and writes:

    daily.csv  weekly.csv  monthly.csv  yearly.csv

Each row comes from the finest graph whose window covers the whole period:
    daily graph   48 h  @ 3.6 min/px
    weekly graph  12 d  @ 21.6 min/px
    monthly graph 49 d  @ 88 min/px
    yearly graph  2 y   @ ~22 h/px   (too coarse for single days: not used for daily.csv)

Each run merges into the existing CSVs (complete rows are never replaced by
incomplete ones; a finer source wins, otherwise the stored row is kept).
Run it daily for accurate per-day history; at least every ~6 weeks to keep
any day-level history beyond the 49-day monthly window.

Only works from a connection on Alliance's network (the portal identifies you
by IP, no login). Stdlib only.

Usage:
    python3 alliance_usage.py                 # fetch + write CSVs to usage/ next to the script
    python3 alliance_usage.py -o ~/somewhere  # other output dir
    python3 alliance_usage.py --save-svg      # also archive raw SVGs
    python3 alliance_usage.py --portal https://10.x.x.1/0/up/
                                              # portal address; default $ALLIANCE_PORTAL,
                                              # else auto-detected via traceroute (hop 2-3)
    python3 alliance_usage.py --svg-dir DIR --end "YYYY-MM-DD HH:MM"
                                              # re-process saved g_d/w/m/y.svg
"""
import argparse
import csv
import datetime as dt
import hashlib
import os
import re
import ipaddress
import platform
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Asia/Kolkata")
except Exception:                      # pragma: no cover
    TZ = dt.timezone(dt.timedelta(hours=5, minutes=30))

PORTAL = os.environ.get("ALLIANCE_PORTAL") or None
MAX_BYTES = 20 * 1024 * 1024           # graphs are ~350 KB; refuse anything huge
GRAPHS = {  # key: (window seconds, label)
    "d": (2 * 86400, "daily graph (48 h)"),
    "w": (12 * 86400, "weekly graph (12 d)"),
    "m": (49 * 86400, "monthly graph (49 d)"),
    "y": (730 * 86400, "yearly graph (2 y)"),
}
FINEST_FIRST = ["d", "w", "m", "y"]
PLOT_WIDTH = 800                       # rrdtool --width used by IPACCT

# sha1(glyph path)[:12] -> character, for the axis font IPACCT/rrdtool uses.
GLYPHS = {
    "EMPTY": " ", "aa4b0af7cd49": "'", "1d716cfc20da": "(", "14a5a18295e5": ")",
    "081a47605fe8": "-", "ab3269f74b1c": ".", "4bbc8e6c40b5": "0", "be940c271140": "1",
    "773065e9ed18": "2", "0fc9e2727124": "3", "6bfd19168305": "4", "41a0536783e5": "5",
    "e36f5b5a4d33": "6", "3f17bb14f6df": "7", "3aa58b925989": "8", "ef6082b3988a": "9",
    "0d74a2dbc592": ":", "0ebc157273c0": "M", "189324dfdd83": "k",
}
UNIT = {"": 1, "k": 1e3, "M": 1e6, "G": 1e9}


# ---------------------------------------------------------------- fetching
def fetch(url, timeout=30):
    ctx = ssl.create_default_context()
    ctx.check_hostname = False          # portal uses a self-signed cert on a
    ctx.verify_mode = ssl.CERT_NONE     # private IP; read-only GETs only
    req = urllib.request.Request(url, headers={"User-Agent": "usage-export"})
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as r:
        data = r.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise RuntimeError("response from %s is larger than %d bytes; refusing" % (url, MAX_BYTES))
    return data.decode("utf-8", "replace")


def discover_portal():
    """Find the IPACCT portal on the first private hops after the home router."""
    if platform.system() == "Windows":
        cmd = ["tracert", "-d", "-h", "3", "-w", "1500", "1.1.1.1"]
    else:
        cmd = ["traceroute", "-n", "-m", "3", "-q", "1", "-w", "2", "1.1.1.1"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    hops = []
    for ip in re.findall(r"\b(\d{1,3}(?:\.\d{1,3}){3})\b", out):
        try:
            if ipaddress.ip_address(ip).is_private and ip not in hops and ip != "1.1.1.1":
                hops.append(ip)
        except ValueError:
            pass
    for ip in hops[1:3]:                 # hop 1 is the home router
        for scheme in ("https", "http"):
            url = "%s://%s/0/up/" % (scheme, ip)
            try:
                if "IPACCT" in fetch(url + "grph", timeout=8):
                    return url
            except Exception:
                continue
    return None


def fetch_all(portal):
    portal = portal.rstrip("/") + "/"
    page = fetch(portal + "grph")
    if "IPACCT" not in page:
        raise RuntimeError("%sgrph is not an IPACCT portal page; check --portal" % portal)
    svgs = {}
    for k in GRAPHS:
        m = re.search(r'src="(grph/%s\?[^"]+)"' % k, page)
        if not m:
            raise RuntimeError("graph '%s' not found on portal page (layout changed?)" % k)
        svgs[k] = fetch(portal + m.group(1).replace("&amp;", "&"))
    return svgs


# ---------------------------------------------------------------- SVG parsing
def _points(d):
    return [(float(x), float(y)) for x, y in re.findall(r"[LM] ([-\d.]+) ([-\d.]+)", d)]


def _subpaths(d):
    """Split an SVG path into closed polygons (one per M ... Z)."""
    out = []
    for part in re.split(r"(?=M )", d):
        pts = _points(part)
        if len(pts) >= 3:
            out.append(pts)
    return out


def parse_svg(svg):
    """Return (xmin, xmax, {x: in_bps}, {x: out_bps}, covered_x_set)."""
    defs = {}
    for gid, d in re.findall(r'<g id="(glyph-[\d-]+)">\s*<path[^<>]* d="([^"]*)"', svg):
        defs[gid] = hashlib.sha1(d.encode()).hexdigest()[:12]
    for gid in re.findall(r'<g id="(glyph-[\d-]+)">\s*</g>', svg):
        defs[gid] = "EMPTY"
    body = svg[svg.find("</defs>"):]

    fills = []
    for m in re.finditer(r"<path ([^<>]*)>", body):     # [^<>] keeps this linear
        a = m.group(1)
        d = re.search(r' d="([^"]+)"', a)
        if 'fill="rgb' in a and d:
            fills.extend(_subpaths(d.group(1)))

    # plot frame: first wide filled rectangle (white canvas of the graph area)
    frame = None
    for p in fills:
        xs, ys = [x for x, _ in p], [y for _, y in p]
        if len(set(xs)) == 2 and len(set(ys)) == 2 and max(xs) - min(xs) >= 0.5 * PLOT_WIDTH:
            frame = (min(xs), max(xs), min(ys), max(ys))
            break
    if frame is None:
        raise ValueError("plot frame not found")
    fx0, fx1, fy0, fy1 = frame
    xmin = int(round(fx0))
    xmax = int(round(fx1)) - 1          # rect 67..867 -> pixels 67..866
    if xmax - xmin + 1 != PLOT_WIDTH:
        raise ValueError("plot is %d px wide, expected %d (portal layout changed?)" % (xmax - xmin + 1, PLOT_WIDTH))

    # data areas: filled polygons inside the frame (excluding the frame itself)
    areas = []
    for p in fills:
        xs, ys = [x for x, _ in p], [y for _, y in p]
        if len(p) < 3:
            continue
        if (min(xs), max(xs), min(ys), max(ys)) == frame:
            continue
        if min(xs) >= fx0 - 0.5 and max(xs) <= fx1 + 0.5 and min(ys) >= fy0 - 0.5 and max(ys) <= fy1 + 0.5:
            areas.append(p)
    if not areas:
        raise ValueError("no data areas found in SVG")

    # y axis: decode labels left of the plot and snap each to its gridline
    hgrid = sorted({float(y1) for x1, y1, x2, y2 in re.findall(
        r'd="M ([\d.]+) ([\d.]+) L ([\d.]+) ([\d.]+) "', body)
        if y1 == y2 and float(x2) - float(x1) > 0.5 * PLOT_WIDTH})
    uses = [(float(x), float(y), g) for g, x, y in
            re.findall(r'<use xlink:href="#(glyph-[\d-]+)" x="([-\d.]+)" y="([-\d.]+)"', body)]
    rows = {}
    for x, y, g in uses:
        if 20 < x < fx0 and fy0 - 10 < y < fy1 + 10:   # x<=20 is the rotated axis title
            rows.setdefault(y, []).append((x, g))
    pts = []
    for y, gl in rows.items():
        text = "".join(GLYPHS.get(defs.get(g, ""), "?") for _, g in sorted(gl))
        m = re.fullmatch(r"\s*(-?[\d.]+)\s*([kMG]?)\s*", text)
        if not m:
            print("warning: unreadable y label %r" % text, file=sys.stderr)
            continue
        # rrdtool draws the label baseline 4 px below its gridline; snap to the
        # actual gridline if one is within 1 px of that (minor lines sit between)
        near = [gy for gy in hgrid if abs((y - 4.0) - gy) < 1.0]
        gy = min(near, key=lambda g: abs((y - 4.0) - g)) if near else y - 4.0
        pts.append((gy, float(m.group(1)) * UNIT[m.group(2)]))
    if len(pts) < 2:
        raise ValueError("could not read y-axis labels (%d found)" % len(pts))
    n = len(pts)
    my = sum(p[0] for p in pts) / n
    mv = sum(p[1] for p in pts) / n
    a = sum((p[0] - my) * (p[1] - mv) for p in pts) / sum((p[0] - my) ** 2 for p in pts)
    b = mv - a * my
    base = -b / a                       # y pixel of 0 bps

    # rasterise polygon edges at every integer x (SVG drops collinear points)
    top, bot, covered = {}, {}, set()
    for p in areas:
        for (x1, y1), (x2, y2) in zip(p, p[1:] + p[:1]):
            if x2 < x1:
                x1, y1, x2, y2 = x2, y2, x1, y1
            for xi in range(int(round(x1)), int(round(x2)) + 1):
                if not (xmin <= xi <= xmax):
                    continue
                if x2 - x1 < 1e-9:
                    ys = (y1, y2)
                else:
                    t = min(max((xi - x1) / (x2 - x1), 0.0), 1.0)
                    ys = (y1 + t * (y2 - y1),)
                covered.add(xi)
                for y in ys:
                    top[xi] = min(top.get(xi, y), y)
                    bot[xi] = max(bot.get(xi, y), y)
    din, dout = {}, {}
    for xi in covered:
        if top[xi] < base:
            din[xi] = a * top[xi] + b
        if bot[xi] > base:
            dout[xi] = -(a * bot[xi] + b)
    return xmin, xmax, din, dout, covered


def graph_samples(svg, window, end):
    """List of (t_start, t_end, in_bytes, out_bytes, has_data) per pixel."""
    xmin, xmax, din, dout, covered = parse_svg(svg)
    spp = window / (xmax - xmin + 1)
    out = []
    for x in range(xmin, xmax + 1):
        t1 = end - dt.timedelta(seconds=(xmax - x) * spp)
        t0 = t1 - dt.timedelta(seconds=spp)
        out.append((t0, t1, din.get(x, 0.0) * spp / 8, dout.get(x, 0.0) * spp / 8, x in covered))
    return out, spp


# ---------------------------------------------------------------- bucketing
def day_start(t):
    return t.replace(hour=0, minute=0, second=0, microsecond=0)


def buckets(kind, start, end):
    """Yield (label, b0, b1) covering [start, end)."""
    if kind == "daily":
        b = day_start(start)
        step = lambda t: t + dt.timedelta(days=1)
        lab = lambda t: t.strftime("%Y-%m-%d")
    elif kind == "weekly":            # ISO weeks, Monday start
        b = day_start(start) - dt.timedelta(days=start.weekday())
        step = lambda t: t + dt.timedelta(days=7)
        lab = lambda t: "%d-W%02d" % t.isocalendar()[:2]
    elif kind == "monthly":
        b = day_start(start).replace(day=1)
        step = lambda t: (t.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        lab = lambda t: t.strftime("%Y-%m")
    else:
        b = day_start(start).replace(month=1, day=1)
        step = lambda t: t.replace(year=t.year + 1)
        lab = lambda t: t.strftime("%Y")
    while b < end:
        n = step(b)
        yield lab(b), b, n
        b = n


def integrate(samples, b0, b1):
    """Return (in_bytes, out_bytes, seconds_with_data, seconds_total)."""
    tin = tout = cov = tot = 0.0
    for t0, t1, i, o, has in samples:
        ov = (min(t1, b1) - max(t0, b0)).total_seconds()
        if ov > 0:
            f = ov / (t1 - t0).total_seconds()
            tin += i * f
            tout += o * f
            tot += ov
            if has:
                cov += ov
    return tin, tout, cov, tot


def build_rows(kind, graphs, end):
    rows = []
    # start at the first pixel that has data (skip months before the account existed)
    first = min((s[0] for smp, _ in graphs.values() for s in smp if s[4]), default=None)
    if first is None:
        return rows
    for lab, b0, b1 in buckets(kind, first, end):
        cut = min(b1, end)
        for k in FINEST_FIRST:
            if k not in graphs or (kind == "daily" and k == "y"):
                continue                # 22 h/px is too coarse for days
            samples, spp = graphs[k]
            if samples[0][0] > b0:
                # window doesn't cover the whole bucket. Only acceptable when the
                # account itself starts inside this bucket (data begins after the
                # graph's first pixel), never when the window merely cuts it.
                if not (k == "y" and b0 <= first and first > samples[0][1]):
                    continue
            i, o, cov, tot = integrate(samples, max(b0, first), cut)
            rows.append({
                "period": lab,
                "start": b0.strftime("%Y-%m-%d"),
                "end": (b1 - dt.timedelta(days=1)).strftime("%Y-%m-%d"),
                "download_GB": "%.2f" % (i / 1e9),
                "upload_GB": "%.2f" % (o / 1e9),
                "total_GB": "%.2f" % ((i + o) / 1e9),
                "data_coverage_pct": "%.0f" % (100 * cov / tot if tot else 0),
                "source": GRAPHS[k][1],
                "resolution_min": "%.1f" % (spp / 60),
                "complete": "yes" if b1 <= end else "no",
                "as_of": cut.strftime("%Y-%m-%d %H:%M"),
            })
            break
    return rows


# ---------------------------------------------------------------- CSV merge
FIELDS = ["period", "start", "end", "download_GB", "upload_GB", "total_GB",
          "data_coverage_pct", "source", "resolution_min", "complete", "as_of"]


def _better(new, old):
    """True if `new` should replace `old`."""
    nc, oc = new.get("complete") == "yes", old.get("complete") == "yes"
    if nc != oc:
        return nc                       # complete beats incomplete, never the reverse
    if not nc:                          # both partial: keep the later snapshot
        return (new.get("as_of") or "") >= (old.get("as_of") or "")
    try:                                # both complete: only a strictly finer source
        return float(new["resolution_min"]) < float(old.get("resolution_min") or 1e9)
    except ValueError:                  # wins, so stored history doesn't jitter
        return False


def merge_write(path, new_rows, dry_run=False):
    old = {}
    if os.path.exists(path):
        # utf-8-sig: tolerate a BOM added by Excel/Numbers
        with open(path, newline="", encoding="utf-8-sig") as f:
            rd = csv.DictReader(f)
            if not rd.fieldnames or "period" not in rd.fieldnames or "complete" not in rd.fieldnames:
                raise RuntimeError(
                    "%s has an unexpected header (re-saved by a spreadsheet app with a different "
                    "delimiter?). Not touching it; fix or move it and rerun." % path)
            for r in rd:
                if r.get("period"):
                    old[r["period"]] = r
    if dry_run:
        return len(old)
    for r in new_rows:
        o = old.get(r["period"])
        if o is None or _better(r, o):
            old[r["period"]] = r
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore", restval="")
            w.writeheader()
            for k in sorted(old):
                w.writerow(old[k])
        mask = os.umask(0)
        os.umask(mask)
        os.chmod(tmp, 0o666 & ~mask)    # mkstemp creates 0600; use normal file perms
        os.replace(tmp, path)           # atomic: history is never left half-written
    except BaseException:
        os.unlink(tmp)
        raise
    return len(old)


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "usage"))
    ap.add_argument("--portal", default=PORTAL,
                    help="portal base URL, e.g. https://10.x.x.1/0/up/ (default: env ALLIANCE_PORTAL, "
                         "else auto-detect via traceroute)")
    ap.add_argument("--save-svg", action="store_true", help="archive raw SVGs under OUT/svg/<timestamp>/")
    ap.add_argument("--svg-dir", help="read g_d.svg g_w.svg g_m.svg g_y.svg from DIR instead of fetching")
    ap.add_argument("--end", help='graph generation time for --svg-dir, "YYYY-MM-DD HH:MM" (IST)')
    args = ap.parse_args()

    if args.end and not args.svg_dir:
        sys.exit("--end only makes sense with --svg-dir")
    if args.svg_dir:
        if not args.end:
            sys.exit("--svg-dir needs --end (time the graphs were generated, IST)")
        try:
            end = dt.datetime.strptime(args.end, "%Y-%m-%d %H:%M")
        except ValueError:
            sys.exit('--end must look like "2026-01-15 09:30"')
        svgs = {}
        for k in GRAPHS:
            p = os.path.join(args.svg_dir, "g_%s.svg" % k)
            try:
                with open(p, encoding="utf-8") as f:
                    svgs[k] = f.read()
            except (OSError, UnicodeDecodeError) as e:
                sys.exit("cannot read %s: %s" % (p, e))
    else:
        portal = args.portal
        if not portal:
            portal = discover_portal()
            if not portal:
                sys.exit("could not find the portal automatically. Are you on the Alliance connection? "
                         "Find your gateway (traceroute -n 1.1.1.1, 2nd hop) and pass "
                         "--portal https://<gateway>/0/up/")
            print("portal: %s" % portal, file=sys.stderr)
        if not re.match(r"https?://", portal):
            sys.exit("--portal must start with http:// or https://")
        try:
            svgs = fetch_all(portal)
        except (urllib.error.URLError, OSError, ValueError) as e:
            sys.exit("could not reach the Alliance portal at %s (%s). Are you on the Alliance connection?"
                     % (portal, e))
        except RuntimeError as e:
            sys.exit(str(e))
        # all times are IST, independent of this machine's time zone
        end = dt.datetime.now(TZ).replace(tzinfo=None, microsecond=0)

    os.makedirs(args.out, exist_ok=True)
    if args.save_svg and not args.svg_dir:
        d = os.path.join(args.out, "svg", end.strftime("%Y-%m-%d_%H%M"))
        os.makedirs(d, exist_ok=True)
        for k, s in svgs.items():
            with open(os.path.join(d, "g_%s.svg" % k), "w", encoding="utf-8") as f:
                f.write(s)

    graphs, failed = {}, []
    for k, s in svgs.items():
        try:
            graphs[k] = graph_samples(s, GRAPHS[k][0], end)
        except ValueError as e:
            failed.append(k)
            print("warning: skipping %s: %s" % (GRAPHS[k][1], e), file=sys.stderr)
    if not graphs:
        sys.exit("no graph could be parsed")

    kinds = ("daily", "weekly", "monthly", "yearly")
    for kind in kinds:                  # check every existing CSV before writing any
        try:
            merge_write(os.path.join(args.out, kind + ".csv"), [], dry_run=True)
        except RuntimeError as e:
            sys.exit(str(e))
    for kind in kinds:
        rows = build_rows(kind, graphs, end)
        path = os.path.join(args.out, kind + ".csv")
        n = merge_write(path, rows)
        print("%-8s %3d rows -> %s" % (kind, n, path))
    if failed:
        print("warning: %d graph(s) could not be read; rows came from the remaining graphs. "
              "Rerun with --save-svg and check the portal." % len(failed), file=sys.stderr)
    sys.exit(2 if failed else 0)


if __name__ == "__main__":
    main()
