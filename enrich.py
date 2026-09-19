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

Anything it works out is remembered in enriched-cache.csv, so a row is only
ever looked up once. Old seasons are never re-fetched just because their
opponent column is empty — the site already knows those.
    python3 enrich.py picks.csv --dry-run  # report only, change nothing
    python3 enrich.py --self-test          # check the arithmetic, no network
"""
import csv, sys, os, json, math, datetime, urllib.request, urllib.error

API = "https://api.collegefootballdata.com/games"
# Games are found by season and week, not by how recently they were played,
# so a row left blank for a month still gets filled in.

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
    """Every finished game of the seasons in play, with its week."""
    out = []
    for y in sorted(years):
        raw = fetch_year(y, key)
        print(f"  CFBD {y}: {len(raw)} games returned")
        done = 0
        for g in raw:
            hp = g.get("homePoints", g.get("home_points"))
            ap = g.get("awayPoints", g.get("away_points"))
            if hp is None or ap is None:
                continue
            ht = g.get("homeTeam", g.get("home_team", ""))
            at = g.get("awayTeam", g.get("away_team", ""))
            wk = g.get("week")
            out.append({"home": (ht, ht, int(hp)), "away": (at, at, int(ap)),
                        "year": y, "week": wk if isinstance(wk, int) else None})
            done += 1
        print(f"  CFBD {y}: {done} of them finished")
    return out

def names_of(side):
    return {norm(side[0]), norm(side[1])}

def find_game(games, teams, year, week, offset=0):
    """A finished game involving every named team, IN THAT WEEK.

    The week must match. A team having exactly one finished game is not a
    reason to assume it is the one we want — that is how an unplayed pick
    ends up wearing last week's result.
    """
    want = {norm(t) for t in teams}
    involving = [g for g in games
                 if g["year"] == year
                 and all(any(w in names_of(g[s]) for s in ("home", "away")) for w in want)]
    if not involving:
        return None, "no finished game for that team this season"
    if week is None:
        return (involving[0], "") if len(involving) == 1 else (None, "no week to match on")
    target = week + offset
    exact = [g for g in involving if g["week"] == target]
    if len(exact) == 1:
        return exact[0], ""
    if len(exact) > 1:
        return None, f"{len(exact)} games in week {target}"
    return None, f"no finished game in week {target} yet"

def season_offset(rows, games, year, C):
    """Does the sheet's week numbering line up with the feed's?

    Checked against rows whose result is already known, so a season that
    counts weeks differently still backfills correctly.
    """
    known = [r for r in rows
             if str(r.get(C["year"], "")).strip() == str(year)
             and (r.get(C["result"]) or "").strip()
             and str(r.get(C["week"], "")).strip().lstrip("-").isdigit()]
    if len(known) < 4:
        return 0
    best, best_hits = 0, -1
    for off in (0, 1, -1):
        hits = 0
        for r in known:
            raw = (r.get(C["team"]) or "").strip()
            teams = [t.strip() for t in raw.split("/")] if "/" in raw else [raw]
            g, _ = find_game(games, teams, year, int(r[C["week"]]), off)
            if not g:
                continue
            try:
                pts = float(r[C["points"]])
            except Exception:
                continue
            bet = (r.get(C["type"]) or "").strip()
            (hl, hn, hp), (al, an, ap) = g["home"], g["away"]
            if bet in ("Over", "Under"):
                c = cover(bet, pts, total=hp + ap)
            else:
                home = norm(teams[0]) in names_of(g["home"])
                c = cover(bet, pts, margin=(hp - ap) if home else (ap - hp))
            res, _ = verdict(c)
            if res and res[0].upper() == (r.get(C["result"]) or " ")[0].upper():
                hits += 1
        if hits > best_hits:
            best, best_hits = off, hits
    if best_hits >= 0:
        print(f"  {year}: week numbering offset {best:+d} "
              f"({best_hits}/{len(known)} known results agree)")
    return best

def enrich(path, dry=False):
    rows = list(csv.DictReader(open(path, newline="")))
    if not rows:
        print("no rows"); return 0
    cols = list(rows[0].keys())
    # the sheet's headers may be capitalised or reordered
    def col(name):
        for c in cols:
            if c.strip().lower() == name:
                return c
        return None
    C_RESULT, C_DIFF, C_OPP = col("result"), col("diff"), col("opponent")
    C_TYPE, C_TEAM, C_PTS = col("type"), col("team"), col("points")
    C_YEAR, C_WEEK, C_NAME = col("year"), col("week"), col("name")
    def blank(r, k): return bool(k) and not (r.get(k) or "").strip()
    def rowkey(r): return f"{r.get(C_YEAR,'')}-{r.get(C_WEEK,'')}-{r.get(C_NAME,'')}"

    # anything worked out on a previous run
    cache, cpath = {}, os.path.join(os.path.dirname(path) or ".", "enriched-cache.csv")
    if os.path.exists(cpath):
        for c in csv.DictReader(open(cpath, newline="")):
            cache[c["key"]] = c
    applied = 0
    for r in rows:
        c = cache.get(rowkey(r))
        if not c:
            continue
        for col, fld in ((C_RESULT, "result"), (C_DIFF, "diff"), (C_OPP, "opponent")):
            if col and blank(r, col) and (c.get(fld) or "").strip():
                r[col] = c[fld]; applied += 1
    if applied:
        print(f"{applied} cell(s) restored from the cache")

    years_in_file = [int(r[C_YEAR]) for r in rows
                     if str(r.get(C_YEAR, "")).strip().isdigit()]
    CURRENT = max(years_in_file) if years_in_file else None

    # A missing opponent is only worth an API call for the current season.
    # Earlier seasons are already covered by the map baked into the site.
    todo = [r for r in rows
            if blank(r, C_RESULT) or blank(r, C_DIFF)
            or (C_OPP and blank(r, C_OPP)
                and (r.get(C_TYPE) or "") not in ("Over", "Under")
                and str(r.get(C_YEAR, "")).strip() == str(CURRENT))]
    print(f"{len(rows)} rows · {len(todo)} still need a lookup")
    if not todo:
        return 0

    key = os.environ.get("CFBD_API_KEY", "").strip()
    if not key:
        print("  ! CFBD_API_KEY is not set — nothing can be looked up", file=sys.stderr)
        return 0
    years = {int(r[C_YEAR]) for r in todo if str(r.get(C_YEAR, "")).strip().isdigit()}
    if not years:
        print("nothing to look up — no API calls made")
        if applied and not dry:
            with open(path, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=cols); w.writeheader(); w.writerows(rows)
        return applied
    print(f"seasons to fetch: {sorted(years)}  ({len(years)} API call"
          f"{'' if len(years)==1 else 's'})")
    games = completed_games(years, key)
    print(f"{len(games)} finished games to match against")
    C = {"year": C_YEAR, "week": C_WEEK, "team": C_TEAM, "type": C_TYPE,
         "points": C_PTS, "result": C_RESULT}
    offsets = {y: season_offset(rows, games, y, C) for y in years}

    filled = 0
    for r in todo:
        bet = (r.get(C_TYPE) or "").strip()
        raw = (r.get(C_TEAM) or "").strip()
        teams = [t.strip() for t in raw.split("/")] if "/" in raw else [raw]
        yr = int(r[C_YEAR]) if str(r.get(C_YEAR, "")).strip().isdigit() else None
        wk = int(r[C_WEEK]) if str(r.get(C_WEEK, "")).strip().isdigit() else None
        g, why = find_game(games, teams, yr, wk, offsets.get(yr, 0))
        if not g:
            print(f"  – {r[C_NAME]:5s} {raw:26s} {why}, left blank")
            continue
        try:
            pts = float(r[C_PTS])
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
        wrote = []
        if blank(r, C_RESULT):
            r[C_RESULT] = res; wrote.append("result")
        if blank(r, C_DIFF):
            r[C_DIFF] = diff; wrote.append("margin")
        if C_OPP and blank(r, C_OPP) and opp:
            r[C_OPP] = opp; wrote.append("opponent")
        if not wrote:
            continue
        filled += 1
        print(f"  ✓ {r[C_NAME]:5s} {raw:26s} {bet:9s} {pts:>6} → "
              f"{res:5s} by {diff:3s} {opp}   [{', '.join(wrote)}]")

    if (filled or applied) and not dry:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader(); w.writerows(rows)
        print(f"wrote {filled} lookup{'' if filled == 1 else 's'} to {path}")
        for r in rows:
            vals = {"result": (r.get(C_RESULT) or "").strip(),
                    "diff":   (r.get(C_DIFF) or "").strip(),
                    "opponent": (r.get(C_OPP) or "").strip() if C_OPP else ""}
            if any(vals.values()):
                cache[rowkey(r)] = dict(key=rowkey(r), **vals)
        with open(cpath, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["key", "result", "diff", "opponent"])
            w.writeheader(); w.writerows(cache[k] for k in sorted(cache))
        print(f"cache now holds {len(cache)} rows")
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
