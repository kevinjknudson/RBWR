#!/usr/bin/env python3
"""
Fills in blank results, margins and opponents in picks.csv.

Runs inside the GitHub Action, straight after the sheet is downloaded.
It only ever fills cells the sheet left blank — anything you typed wins.
If it cannot find a game with confidence it leaves the row alone.

Scores come from the CollegeFootballData API, which needs a free key:
  1. get one at https://collegefootballdata.com/key
  2. add it to the repo as a secret named CFBD_API_KEY

    python3 enrich.py picks.csv            # fill in and save
    python3 enrich.py picks.csv --dry-run  # report only, change nothing
    python3 enrich.py --self-test          # check the arithmetic, no network
"""
import csv, sys, os, json, math, datetime, urllib.request, urllib.error

API = "https://api.collegefootballdata.com/games"
LOOKBACK_DAYS = 10             # a pick's game is within this many days of the run

ALIAS = {
    "ga tech": "georgia tech", "gatech": "georgia tech", "okie state": "oklahoma state",
    "isu": "iowa state", "kstate": "kansas state", "mich state": "michigan state",
    "cincinatti": "cincinnati", "cincinnatti": "cincinnati", "fresno": "fresno state",
    "fresno st": "fresno state", "bama": "alabama", "unt": "north texas",
    "wvu": "west virginia", "louisiana laf": "louisiana", "san jose st": "san jose state",
    "kent st": "kent state", "vandy": "vanderbilt", "uva": "virginia",
    "fsu": "florida state", "va tech": "virginia tech", "niu": "northern illinois",
    "asu": "arizona state", "ecu": "east carolina", "usf": "south florida",
    "ga state": "georgia state", "app state": "appalachian state", "unc": "north carolina",
    "pitt": "pittsburgh", "mississippi": "ole miss", "uconn": "connecticut",
    "umass": "massachusetts", "ndsu": "north dakota state", "sdsu": "south dakota state",
    "southern california": "usc", "miami fl": "miami", "miami oh": "miami ohio",
}

def norm(s):
    s = (s or "").strip().lower()
    s = s.replace("&", "and").replace(".", "").replace("'", "")
    s = ALIAS.get(s, s)
    return " ".join(s.split())

def cover(bet, points, margin=None, total=None):
    """How far the result landed the right side of the number. Positive means a win."""
    if bet == "Favorite":  return margin - points
    if bet == "Dog":       return margin + points
    if bet == "Over":      return total - points
    if bet == "Under":     return points - total
    return None

def verdict(c):
    """Result letter and the whole points needed to flip it."""
    if c is None: return None, None
    if abs(c) < 1e-9: return "Push", "-"
    return ("Win" if c > 0 else "Loss"), str(math.ceil(abs(c) - 1e-9))

def fetch_year(year, key):
    """Every game of a season from CollegeFootballData. One call, cached by caller."""
    import urllib.parse
    url = API + "?" + urllib.parse.urlencode({"year": year, "seasonType": "both"})
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
        "User-Agent": "rbwr-sync/2.0",
    })
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = ""
        try: body = e.read().decode()[:180]
        except Exception: pass
        print(f"  ! CFBD {year}: HTTP {e.code} {body}", file=sys.stderr)
        if e.code in (401, 403):
            print("    (check the CFBD_API_KEY secret)", file=sys.stderr)
    except Exception as e:
        print(f"  ! CFBD {year}: {e}", file=sys.stderr)
    return []

def completed_games(years, key):
    """Finished games in the recent window, shaped like the matcher expects."""
    cutoff = datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)
    out = []
    for y in sorted(years):
        raw = fetch_year(y, key)
        print(f"  CFBD {y}: {len(raw)} games returned")
        for g in raw:
            hp, ap = g.get("homePoints", g.get("home_points")), g.get("awayPoints", g.get("away_points"))
            if hp is None or ap is None:
                continue
            ds = (g.get("startDate") or g.get("start_date") or "")[:10]
            try:
                d = datetime.date.fromisoformat(ds)
            except Exception:
                continue
            if d < cutoff:
                continue
            ht = g.get("homeTeam", g.get("home_team", ""))
            at = g.get("awayTeam", g.get("away_team", ""))
            out.append({"home": (ht, ht, int(hp)), "away": (at, at, int(ap)),
                        "date": ds})
    return out

def names_of(side):
    return {norm(side[0]), norm(side[1])}

def find_game(games, teams):
    """A game involving every named team. Exactly one match, or nothing."""
    want = {norm(t) for t in teams}
    hits = [g for g in games
            if all(any(w in names_of(g[s]) for s in ("home", "away")) for w in want)]
    return hits[0] if len(hits) == 1 else None

def enrich(path, dry=False):
    rows = list(csv.DictReader(open(path, newline="")))
    if not rows:
        print("no rows"); return 0
    cols = list(rows[0].keys())
    todo = [r for r in rows if not (r.get("result") or "").strip()]
    print(f"{len(rows)} rows · {len(todo)} awaiting a result")
    if not todo:
        return 0

    key = os.environ.get("CFBD_API_KEY", "").strip()
    if not key:
        print("  ! CFBD_API_KEY is not set — nothing can be looked up", file=sys.stderr)
        return 0
    years = {int(r["year"]) for r in todo if str(r.get("year", "")).strip().isdigit()}
    games = completed_games(years, key)
    print(f"{len(games)} completed games in the last {LOOKBACK_DAYS} days")

    filled = 0
    for r in todo:
        bet = (r.get("type") or "").strip()
        raw = (r.get("team") or "").strip()
        teams = [t.strip() for t in raw.split("/")] if "/" in raw else [raw]
        g = find_game(games, teams)
        if not g:
            print(f"  – {r['name']:5s} {raw:26s} no single match, left blank")
            continue
        try:
            pts = float(r["points"])
        except Exception:
            continue
        (hloc, hname, hp), (aloc, aname, ap) = g["home"], g["away"]
        if bet in ("Over", "Under"):
            c = cover(bet, pts, total=hp + ap)
            opp = ""
        else:
            picked_home = norm(teams[0]) in names_of(g["home"])
            margin = (hp - ap) if picked_home else (ap - hp)
            other = aloc if picked_home else hloc
            c = cover(bet, pts, margin=margin)
            opp = ("vs " if picked_home else "at ") + other
        res, diff = verdict(c)
        if res is None:
            continue
        r["result"] = res
        r["diff"] = diff
        if "opponent" in cols and not (r.get("opponent") or "").strip():
            r["opponent"] = opp
        filled += 1
        print(f"  ✓ {r['name']:5s} {raw:26s} {bet:9s} {pts:>6} → {res:5s} by {diff:3s} {opp}")

    if filled and not dry:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader(); w.writerows(rows)
        print(f"wrote {filled} result{'' if filled == 1 else 's'} to {path}")
    elif dry:
        print(f"dry run — {filled} row(s) would have been filled")
    return filled

def self_test():
    cases = [
        # bet,      number, margin, total,  expect result, expect diff
        ("Favorite", 7,     10,     None,   "Win",  "3"),
        ("Favorite", 7,      7,     None,   "Push", "-"),
        ("Favorite", 7.5,    7,     None,   "Loss", "1"),   # 0.5 short rounds up to 1
        ("Favorite", 13.5,  20,     None,   "Win",  "7"),   # 6.5 clear rounds up to 7
        ("Dog",      3,     -1,     None,   "Win",  "2"),
        ("Dog",      3,     -3,     None,   "Push", "-"),
        ("Dog",      6.5,  -10,     None,   "Loss", "4"),   # 3.5 short rounds up to 4
        ("Over",     47,   None,     52,    "Win",  "5"),
        ("Under",    47,   None,     52,    "Loss", "5"),
        ("Under",    49.5, None,     44,    "Win",  "6"),   # 5.5 clear rounds up to 6
        ("Over",     50,   None,     50,    "Push", "-"),
    ]
    bad = 0
    for bet, pts, m, t, er, ed in cases:
        res, diff = verdict(cover(bet, pts, margin=m, total=t))
        ok = (res == er and diff == ed)
        bad += not ok
        print(f"  {'ok ' if ok else 'FAIL'} {bet:9s} {pts:>6} "
              f"{'margin '+str(m) if m is not None else 'total '+str(t):12s} "
              f"→ {res} {diff}   expected {er} {ed}")
    print("self-test:", "all passed" if not bad else f"{bad} FAILED")
    return bad

if __name__ == "__main__":
    args = sys.argv[1:]
    if "--self-test" in args:
        sys.exit(1 if self_test() else 0)
    if not args:
        print(__doc__); sys.exit(2)
    sys.exit(0 if enrich(args[0], dry="--dry-run" in args) is not None else 1)
