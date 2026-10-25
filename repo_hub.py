"""repo_hub.py -- GitHub repo discovery + failover for the update/ads pipeline.

DESIGN RULE (this is the load rule, do not break it):
    The happy path costs exactly ONE request. `active()` probes only the repo we
    already know about and returns. The ordered candidate list is walked ONLY
    when that probe FAILS -- i.e. the repo we need does not exist any more (it
    was blocked, deleted, renamed or taken down). A 404 sweep is never paid for
    on a healthy system.

Naming families, in preference order:

  1. FLIX_REPO (pin, used by tests)
  2. FLIX_REPO_CANDIDATES (extra pins)
  3. Joe-2049/Flixy                <- the live repo
  4. Joe{n}-2049 / Joe({n})-2049 / Joe({n})-204({n}) / Joe-204({n})   n = 1..MAX_N
  5. date/time stamps, e.g. 30_September_2026_Wednesday_09_57_42_PM,
     2026_09_30_Wed_09_57_42_PM, 09_57_42_PM_30_September_2026_Wednesday, ...

  Joe(n)-2049 variants are ALWAYS preferred over the date/time stamps, so a
  deliberately-named replacement is found before a timestamped one.

The date/time family is combinatorially huge, so it is a CURATED ordered set
(orders x time arrangements x month/weekday styles) rather than every
permutation -- the same judgement that produced the "formats I'd actually
consider useful" list. It is expanded around *now*, because a replacement repo
is created at the moment the block is noticed, and newest-first.

TWO GATES, in order -- the yes/no check that decides "proceed":

  1. REPO GATE (required)  -> the repo answers and version.json parses to a
     plausible version string. Only a repo passing this can become active.
  2. ASSET GATE (reported) -> release assets exist for that version. Reported,
     not required: a release is built and published before users should be
     pointed at it, so "repo ok, assets not yet" is a normal transient state and
     must NOT knock the repo out of rotation.
"""
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

# ---------------------------------------------------------------- config
PRIMARY = "Joe2016-alt/Joe1-2049"
REPO_NAME = "Flixy"
MAX_N = 40                      # how far the n-patterns are enumerated

PROBE_TIMEOUT = 5               # seconds per candidate
FAST_PROBES = 60                # sweep budget for an online request
DEEP_PROBES = 700               # sweep budget for the offline deep scan
SWEEP_TIMEOUT = 20              # wall-clock ceiling for the online sweep
CACHE_TTL_S = 6 * 3600          # re-probe this rarely
PIN_TTL_S = 24 * 3600           # a discovered repo is a deliberate act: trust it
VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")

# date/time expansion window
DT_LOOKBACK_MIN = 90            # minute-by-minute for the last 90 minutes
DT_HOURS_BACK = 48              # then hourly back to 48h

try:
    from paths import DATA_ROOT
except Exception:
    DATA_ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(DATA_ROOT, "repo_active.json")

_lock = threading.RLock()
_cache = {"val": None, "ts": 0.0}
_dt_cache = {"key": None, "val": []}


# ============================================================== name parts
MONTHS_FULL = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]
DAYS_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
             "Saturday", "Sunday"]

# part keys
#   D  day of month          MN/MS/Mn  month full/short/2-digit
#   Y  year                  WD/WS/Wn  weekday full/short/ISO number
#   WK ISO week number       H  hour 12h   H24 hour 24h
#   MI minute                S  second      AP AM/PM

DATE_ORDERS = [
    ("D", "MN", "Y", "WD"),      # 30_September_2026_Wednesday
    ("D", "MN", "Y"),            # 30_September_2026
    ("MN", "D", "Y", "WD"),      # September_30_2026_Wednesday
    ("MN", "D", "Y"),            # September_30_2026
    ("Y", "MN", "D", "WD"),      # 2026_September_30_Wednesday
    ("Y", "MN", "D"),            # 2026_September_30
    ("WD", "D", "MN", "Y"),      # Wednesday_30_September_2026
    ("WD", "MN", "D", "Y"),      # Wednesday_September_30_2026
    ("WD", "Y", "MN", "D"),      # Wednesday_2026_September_30
    ("WD", "Y", "D", "MN"),      # Wednesday_2026_30_September
    ("D", "MS", "Y", "WS"),      # 30_Sep_2026_Wed          (compact)
    ("D", "Mn", "Y", "WS"),      # 30_09_2026_Wed           (numeric)
    ("D", "MN", "Y", "WS"),      # 30_September_2026_Wed    (mixed)
    ("Y", "Mn", "D", "WS"),      # 2026_09_30_Wed           (ISO-inspired)
    ("Y", "Mn", "D", "WD"),      # 2026_09_30_Wednesday     (grouped date+time)
    ("D", "MN", "Y", "lblWeek", "WK", "WD"),   # 30_September_2026_Week_40_Wednesday
    ("Y", "Mn", "D", "lblWeek", "WK", "WD"),   # 2026_09_30_Week_40_Wednesday
    ("Y", "Mn", "D", "WK", "WD"),             # 2026_09_30_40_Wednesday
    ("D", "MN", "Y", "WK", "WD"),             # 30_September_2026_40_Wednesday
]

TIME_ORDERS = [
    ("H", "MI", "S", "AP"),      # 09_57_42_PM              (standard)
    ("AP", "H", "MI", "S"),      # PM_09_57_42              (AMPM first)
    ("H", "AP", "MI", "S"),      # 09_PM_57_42              (hour then AMPM)
    ("H", "S", "MI", "AP"),      # 09_42_57_PM              (hour, seconds, minutes)
    ("MI", "H", "S", "AP"),      # 57_09_42_PM              (minutes first)
    ("S", "H", "MI", "AP"),      # 42_09_57_PM              (seconds first)
    ("H", "MI", "AP", "S"),      # 09_57_PM_42
    ("H", "AP", "S", "MI"),      # 09_PM_42_57
]

# explicitly semantic time blocks, appended to each date order
SEMANTIC_TIME = [
    ("H", "lblHour", "MI", "lblMinute", "S", "lblSecond", "AP"),
    ("lblHour", "H", "lblMinute", "MI", "lblSecond", "S", "AP"),
]


def _parts(dt):
    return {
        "D": "%02d" % dt.day,
        "MN": MONTHS_FULL[dt.month - 1],
        "MS": dt.strftime("%b"),
        "Mn": "%02d" % dt.month,
        "Y": "%04d" % dt.year,
        "WD": DAYS_FULL[dt.weekday()],
        "WS": dt.strftime("%a"),
        "Wn": str(dt.isoweekday()),
        "WK": "%02d" % dt.isocalendar()[1],
        "H": "%02d" % (dt.hour % 12 or 12),
        "H24": "%02d" % dt.hour,
        "MI": "%02d" % dt.minute,
        "S": "%02d" % dt.second,
        "AP": "AM" if dt.hour < 12 else "PM",
        "lblHour": "Hour",
        "lblMinute": "Minute",
        "lblSecond": "Second",
        "lblWeek": "Week",
    }


def _join(order, p):
    return "_".join(p[k] for k in order)


def names_for(dt):
    """Every curated name for one instant, most-likely first, deduped."""
    p = _parts(dt)
    out = []

    def add(name):
        if name and name not in out:
            out.append(name)

    # 1) date order + standard time, and the time-first variants
    for do in DATE_ORDERS:
        base = _join(do, p)
        for to in TIME_ORDERS:
            add(base + "_" + _join(to, p))
        for to in SEMANTIC_TIME:
            add(base + "_" + _join(to, p))
        # A trailing weekday may also sit AFTER the time block, e.g.
        # 30_September_2026_09_57_42_PM_Wednesday
        if do[-1] == "WD":
            stem = _join(do[:-1], p)
            for to in TIME_ORDERS:
                add(stem + "_" + _join(to, p) + "_" + p["WD"])
        if do[-1] == "WS":
            stem = _join(do[:-1], p)
            for to in TIME_ORDERS:
                add(stem + "_" + _join(to, p) + "_" + p["WS"])
    # 2) time first (from the "useful formats" list)
    for to in TIME_ORDERS[:1]:
        for do in (("D", "MN", "Y", "WD"), ("D", "MS", "Y", "WS"), ("Y", "Mn", "D", "WS")):
            add(_join(to, p) + "_" + _join(do, p))
    # 3) 24-hour variants of the two most common shapes, because a repo may
    #    have been named either way
    for do in (("D", "MN", "Y", "WD"), ("Y", "Mn", "D", "WS")):
        add(_join(do, p) + "_" + "%s_%s_%s" % (p["H24"], p["MI"], p["S"]))
    return out


def _iter_datetime_candidates(now=None):
    """Timestamp-stamped candidates as a LAZY generator: newest instant
    first, and within an instant the curated name order.

    Lazy on purpose. Materialising this family is tens of thousands of strings;
    the sweep only ever consumes as many as its probe budget allows, so
    building the whole list up front would be pure waste."""
    now = now or datetime.now()
    seen = set()
    anchors = []
    cur = now.replace(second=0, microsecond=0)
    for i in range(DT_LOOKBACK_MIN):
        anchors.append(cur - timedelta(minutes=i))
    h = cur.replace(minute=0)
    for i in range(1, DT_HOURS_BACK + 1):
        anchors.append(h - timedelta(hours=i))
    for a in anchors:
        for nm in names_for(a):
            full = "%s/%s" % (nm, REPO_NAME)
            if full not in seen:
                seen.add(full)
                yield full


def datetime_candidates(now=None):
    """Counted form, for status output and tests only."""
    return list(_iter_datetime_candidates(now))


# ============================================================== candidates
def _pattern_candidates():
    out = []
    for n in range(1, MAX_N + 1):
        out.append("Joe%d-2049/%s" % (n, REPO_NAME))
        out.append("Joe(%d)-2049/%s" % (n, REPO_NAME))
        out.append("Joe(%d)-204(%d)/%s" % (n, n, REPO_NAME))
        out.append("Joe-204(%d)/%s" % (n, REPO_NAME))
    return out


def _iter_candidates(include_datetime=True):
    """Lazy ordered probe list. Joe(n)-2049 variants always precede the
    date/time stamps."""
    head = []
    pin = (os.environ.get("FLIX_REPO") or "").strip()
    if pin:
        head.append(pin if "/" in pin else "%s/%s" % (pin, REPO_NAME))
    extra = (os.environ.get("FLIX_REPO_CANDIDATES") or "").strip()
    if extra:
        for c in extra.split(","):
            c = c.strip()
            if c:
                head.append(c if "/" in c else "%s/%s" % (c, REPO_NAME))
    if PRIMARY not in head:
        head.append(PRIMARY)
    for c in head:                      # pins + the live repo come first
        yield c
    seen = set(head)
    for c in _pattern_candidates():
        if c not in seen:
            seen.add(c)
            yield c
    if include_datetime:
        for c in _iter_datetime_candidates():
            if c not in seen:
                seen.add(c)
                yield c


def iter_candidates(include_datetime=True):
    return _iter_candidates(include_datetime)


def candidates(include_datetime=True):
    """Materialised form -- status output and tests only. The sweep uses
    iter_candidates() so it never builds the whole list."""
    return list(_iter_candidates(include_datetime))


def pattern_count():
    return 1 + len(_pattern_candidates())


# ==================================================================== urls
def raw_url(full, path, cache_bust=False):
    url = "https://raw.githubusercontent.com/%s/main/%s" % (full, path.lstrip("/"))
    return url + ("?t=%d" % int(time.time() * 1000) if cache_bust else "")


def api_url(full):
    return "https://api.github.com/repos/%s" % full


def version_url(full, cache_bust=True):
    return raw_url(full, "version.json", cache_bust)


def ads_manifest_url(full):
    return raw_url(full, "ads/ads.json", True)


def release_page(full, version):
    return "https://github.com/%s/releases/tag/v%s" % (full, version)


def release_asset_url(full, version, filename):
    return "https://github.com/%s/releases/download/v%s/%s" % (full, version, filename)


# =================================================================== probe
def _head_ok(url, timeout=PROBE_TIMEOUT):
    """Does a release asset really exist? A ranged GET beats HEAD against
    GitHub's download CDN (it 302s to objects.githubusercontent)."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0",
        "Range": "bytes=0-1",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status in (200, 206)
    except Exception:
        return False


def probe(full, want_assets=True):
    """Gate 1 (+2). Returns a record dict, or None if this repo is unusable."""
    try:
        req = urllib.request.Request(version_url(full), headers={
            "User-Agent": "flix-repo-hub", "Accept": "*/*"})
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            body = r.read(4096)
    except Exception:
        return None
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except Exception:
        return None
    version = str((data or {}).get("version") or "").strip()
    if not VERSION_RE.match(version):
        return None

    rec = {
        "full": full,
        "owner": full.split("/")[0],
        "repo": full.split("/")[-1],
        "version": version,
        "download_url": data.get("download_url") or "",
        "probed_at": time.time(),
        "assets": {},
    }
    if want_assets:
        v = version
        rec["assets"] = {
            "ads": _head_ok(release_asset_url(full, v, "FlixWorld-Setup-%s.exe" % v)),
            "noads": _head_ok(release_asset_url(
                full, v, "FlixWorld-Setup-NoAds-%s.exe" % v)),
        }
        rec["assets_ok"] = any(rec["assets"].values())
    else:
        rec["assets_ok"] = None
    return rec


# =================================================================== state
def _save(rec):
    try:
        d = os.path.dirname(os.path.abspath(STATE_FILE)) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".repo_", suffix=".tmp", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, STATE_FILE)
    except Exception:
        pass


def _load():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            rec = json.load(f)
        if isinstance(rec, dict) and rec.get("full"):
            return rec
    except Exception:
        pass
    return None


def _cached():
    with _lock:
        if _cache["val"]:
            return _cache["val"]
    return _load()


def _remember(rec):
    with _lock:
        _cache["val"] = rec
        _cache["ts"] = time.time()
    _save(rec)
    return rec


def known_repo():
    """The repo we already believe in. No network."""
    rec = _cached()
    if rec and rec.get("full"):
        return rec["full"]
    pin = (os.environ.get("FLIX_REPO") or "").strip()
    if pin:
        return pin if "/" in pin else "%s/%s" % (pin, REPO_NAME)
    return PRIMARY


# ======================================================== fast path (1 call)
def probe_known(force=False, want_assets=True):
    """Probe ONLY the repo we already know about. Returns None if it is gone.

    This is the entire happy path: one request, no list walking."""
    rec = _cached()
    full = known_repo()
    age = time.time() - float((rec or {}).get("probed_at") or 0)
    ttl = PIN_TTL_S if (rec and rec.get("full") != PRIMARY) else CACHE_TTL_S
    if not force and rec and rec.get("full") == full and age < ttl:
        return rec
    new = probe(full, want_assets=want_assets)
    if new:
        new["candidates_checked"] = 1
        return _remember(new)
    return None


# ============================================================ sweep (rare)
def resolve(force=False, want_assets=True, deep=False, log=None):
    """Fast path first; sweep the ordered list ONLY if the known repo failed."""
    fast = probe_known(force=force, want_assets=want_assets)
    if fast:
        return fast

    budget = DEEP_PROBES if deep else FAST_PROBES
    deadline = time.time() + (SWEEP_TIMEOUT * 6 if deep else SWEEP_TIMEOUT)
    tried = 0
    for full in _iter_candidates():
        if tried >= budget or time.time() > deadline:
            break
        tried += 1
        rec = probe(full, want_assets=want_assets)
        if rec:
            rec["candidates_checked"] = tried
            rec["swept"] = True
            if log:
                log("resolved %s after %d candidates" % (rec["full"], tried))
            return _remember(rec)
    if log:
        log("sweep exhausted: %d candidates tried, none answered" % tried)
    return None


def active(force=False, want_assets=True):
    """The repo to use. Never returns None."""
    # Load blocked
    blocked = set()
    try:
        bp = os.path.join(DATA_ROOT, "repo_blocked.txt")
        if os.path.exists(bp):
            with open(bp, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        blocked.add(line)
    except Exception:
        pass

    rec = resolve(force=force, want_assets=want_assets)
    if rec:
        full = rec["full"]
        if full in blocked:
            # Force sweep to find next
            rec2 = resolve(force=True, want_assets=want_assets, deep=False)
            if rec2 and rec2["full"] not in blocked:
                return rec2["full"]
            # Try harder
            rec3 = resolve(force=True, want_assets=want_assets, deep=True)
            if rec3 and rec3["full"] not in blocked:
                return rec3["full"]
            # Fallback to Joe1-2049/Flixy
            cand = "Joe1-2049/Flixy"
            if cand not in blocked:
                return cand
        return full
    return PRIMARY


def status(force=False, want_assets=True, deep=False):
    """Everything an operator needs for the yes/no decision."""
    rec = resolve(force=force, want_assets=want_assets, deep=deep)
    if not rec:
        return {
            "ok": False,
            "repo_gate": False,
            "asset_gate": False,
            "error": "no candidate repo answered",
            "known_repo": known_repo(),
        }
    age = time.time() - float(rec.get("probed_at") or 0)
    return {
        "ok": True,
        "repo": rec["full"],
        "repo_gate": True,                         # gate 1 passed
        "asset_gate": bool(rec.get("assets_ok")),  # gate 2
        "version": rec.get("version"),
        # The version above can be served from cache; age_s + ?force=1 say so.
        "version_probed_s_ago": int(age),
        "version_is_cached": age > 5,
        "assets": rec.get("assets") or {},
        "download_url": rec.get("download_url") or "",
        "version_json": version_url(rec["full"], False),
        "release_page": release_page(rec["full"], rec.get("version") or "0"),
        "swept": bool(rec.get("swept")),
        "candidates_checked": rec.get("candidates_checked", 1),
        "preference": "Joe(n)-2049 patterns, then date/time stamps",
        "load_rule": "one probe on the happy path; the list is walked only "
                     "when the known repo fails",
    }


if __name__ == "__main__":
    import sys
    deep = "--deep" in sys.argv
    s = status(force=True, deep=deep)
    s["candidate_counts"] = {
        "joe_patterns": pattern_count(),
        "datetime": len(datetime_candidates()),
        "total": len(candidates()),
    }
    s["datetime_examples"] = names_for(datetime.now())[:6]
    print(json.dumps(s, indent=2))
