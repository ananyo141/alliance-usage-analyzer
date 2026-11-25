# alliance-usage-analyzer

Export your **Alliance Broadband** (Kolkata, AS23860) data usage to CSV.

Alliance's customer portal (built on [IPACCT](https://ipacct.com)) only shows
usage as four graphs: daily, weekly, monthly and yearly. There is no download
or API for subscribers. This script reads those graphs (they are SVG vector
images, so the curves can be measured directly), integrates bits/s over time
and writes per-day, per-week, per-month and per-year totals:

```
usage/daily.csv  usage/weekly.csv  usage/monthly.csv  usage/yearly.csv
```

Example (made-up values):

```
period,start,end,download_GB,upload_GB,total_GB,data_coverage_pct,source,resolution_min,complete,as_of
2026-01-14,2026-01-14,2026-01-14,12.40,3.10,15.50,100,daily graph (48 h),3.6,yes,2026-01-15 00:00
```

- Python 3.9+, standard library only. macOS, Linux; Windows should work but is
  untested (use `py` instead of `python3`).
- Read-only: it fetches the graphs page and the four graph images. It never
  logs in, never changes anything, and never saves the portal page (which shows
  your name, address and client ID).

## Usage

You must be on an **Alliance connection**: the portal sits on Alliance's
private network and identifies you by IP, with no login.

```bash
python3 alliance_usage.py              # writes CSVs to usage/ next to the script
python3 alliance_usage.py --save-svg   # also archive the raw graphs (~1.4 MB/run)
python3 alliance_usage.py -o ~/somewhere
python3 alliance_usage.py -h
```

### Portal address

The portal runs on your Alliance gateway, which differs between areas. By
default the script finds it automatically: it runs a short traceroute and
checks hops 2–3 for the IPACCT page. You can also set it explicitly:

```bash
python3 alliance_usage.py --portal https://10.x.x.1/0/up/
export ALLIANCE_PORTAL=https://10.x.x.1/0/up/     # or set it once
```

To find it by hand, take the second hop of a traceroute (the first is your own
router), or read **Default Gateway** on your router's Internet/WAN status page:

```bash
traceroute -n 1.1.1.1        # Windows: tracert -d 1.1.1.1
```

Opening `http://<gateway>/0/up/` in a browser should show the
"IPACCT | User information portal" page. If it doesn't:

- Turn off any VPN / iCloud Private Relay and use the home connection.
- If hop 2 isn't a `10.x` address you may be behind a second router (double
  NAT): try hop 3, or the gateway shown on the outer router.
- Accept the self-signed certificate warning, or try `http://`.
- Some areas are served by local operators on other systems; if no hop serves
  an IPACCT page, ask Alliance support for the customer usage portal link.

### Re-processing saved graphs

```bash
python3 alliance_usage.py --svg-dir usage/svg/2026-01-15_0930 --end "2026-01-15 09:30"
```

`--end` is the time the graphs were generated (the folder name), in IST.

## How often to run it

| Graph | Window | Resolution | Used for |
|---|---|---|---|
| daily | 48 h | 3.6 min/px | days, weeks |
| weekly | 12 days | 21.6 min/px | days, weeks |
| monthly | 49 days | 88 min/px | days, weeks, months |
| yearly | 2 years | ~22 h/px | weeks, months, years (not days) |

Each row uses the finest graph whose window covers the whole period. Runs
merge into the existing CSVs: complete rows are never replaced by incomplete
ones, a finer source replaces a coarser one, and otherwise the stored row is
kept so history doesn't jitter between runs.

- **Run it daily** (cron / launchd / Task Scheduler) for accurate per-day
  history: each day then gets recorded from the daily or weekly graph.
- Run it at least every ~6 weeks to keep any day-level rows at all; older
  single days only exist in the yearly graph, which is too coarse for them.

Example cron entry (10:00 every day):

```
0 10 * * * /usr/bin/python3 /path/to/alliance_usage.py --save-svg >/dev/null 2>&1
```

## Accuracy

Measured by comparing overlapping graphs and repeated snapshots:

- Long spans (weeks of data and more): within about 1–3 % of the portal's own
  legend averages.
- Days from the daily or weekly graph: about ±10 %.
- Days from the monthly graph (88 min/px): individual days can be off by
  20–30 %; treat them as rough.
- Weeks and months from the yearly graph (~22 h/px): roughly ±10–25 % per
  week, and a month with a big spike next to its boundary can move ~8 %
  between runs.
- Averages hide short events: no peak speeds, nothing shorter than a few
  minutes. Whole-household traffic only, no per-device split.

## How it works

1. Fetch `/0/up/grph` and the four `grph/{d,w,m,y}` SVGs.
2. Find the plot frame (800 px wide) and the filled In/Out areas inside it.
3. Decode the y-axis labels (glyphs are identified by a hash of their path
   data) and fit pixel → bits/s.
4. Rasterise the area outlines at every pixel column, convert to bytes using
   the graph's time per pixel, and sum into calendar buckets (IST).

### Why TLS verification is off

The portal uses a self-signed certificate on a private `10.x` address, so the
script disables certificate checks for its requests (the portal URL you give
or the one it detects, including any redirects). It only sends GET requests,
sends no credentials, and caps response size.

## Limitations

- Only works from an Alliance connection.
- Depends on the portal's current rendering (rrdtool-style SVG, 800 px plot,
  its font). A graph that can't be read is skipped with a warning and exit
  code 2; the CSVs are then updated from the remaining graphs only. If no
  graph can be read, nothing is written. Use `--save-svg` so raw graphs are
  kept and can be re-processed after a fix.
- Existing CSVs re-saved by a spreadsheet app with a different delimiter are
  refused (nothing is written) rather than overwritten.
- Other ISPs using IPACCT may work with `--portal`, but their font may differ.

Not affiliated with Alliance Broadband or IPACCT.

## License

MIT
