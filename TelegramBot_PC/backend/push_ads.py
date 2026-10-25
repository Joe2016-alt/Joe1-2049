"""
push_ads.py — publish fresh ad clips to the app's ad manifest.

Workflow:
  1. Drop clips into ads_inbox/ named like the section they belong to
     (e.g. ads_inbox/launch.mp4, ads_inbox/hourly.mp4).
  2. $env:GITHUB_TOKEN=... ; python push_ads.py
  3. The clips are uploaded to ads/<file> in the release repo, the manifest
     ads/ads.json is rewritten with their raw URLs and bumped (version+1),
     and the app detects the new version and swaps the section on next poll.

Optional duration override: ads_inbox/launch__20.mp4 means duration 20s.
"""
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.request

REPO_API = "https://api.github.com/repos/Joe2016-alt/Joe1-2049"
RAW_BASE = "https://raw.githubusercontent.com/Joe2016-alt/Joe1-2049/main"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.join(SCRIPT_DIR, "ads_inbox")
ADS_DIR = os.path.join(SCRIPT_DIR, "ads")
MANIFEST = os.path.join(ADS_DIR, "ads.json")


def _headers():
    return {
        "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
        "Accept": "application/vnd.github+json",
        "User-Agent": "flix-ads",
    }


def _resolve_repo():
    """Point REPO_API/RAW_BASE at whichever GitHub repo is live (repo_hub
    walks the candidate list), so ad pushes survive the primary repo being
    blocked. Falls back to the hardcoded primary if repo_hub is unavailable.
    """
    global REPO_API, RAW_BASE
    try:
        import repo_hub
        full = repo_hub.active(want_assets=False)
        REPO_API = "https://api.github.com/repos/%s" % full
        RAW_BASE = "https://raw.githubusercontent.com/%s/main" % full
        print("Target repo:", full)
    except Exception as e:
        print(f"repo_hub unavailable ({e}); using {REPO_API}")


def _put_contents(path, data):
    _resolve_repo()
    body = json.dumps(
        {"message": "ads: " + os.path.basename(path),
         "content": base64.b64encode(data).decode()}
    ).encode()
    req = urllib.request.Request(
        f"{REPO_API}/contents/{path}", data=body, method="PUT", headers=_headers()
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def main():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        print("Set GITHUB_TOKEN first (e.g. $env:GITHUB_TOKEN='...'; python push_ads.py)")
        sys.exit(2)
    if not os.path.isdir(INBOX):
        print("No ads_inbox/ folder found. Drop clips there first.")
        sys.exit(2)

    entries = {}
    duration_over = re.compile(r"^(.*?)__(\d+)\.")
    for f in sorted(os.listdir(INBOX)):
        full = os.path.join(INBOX, f)
        if not os.path.isfile(full):
            continue
        if not re.match(r"(launch|hourly)__", f) and not re.match(r"(launch|hourly)\.", f):
            print("skip (must be launch.* or hourly.*):", f)
            continue
        m = re.match(r"(launch|hourly)(?:__(\d+))?\..*", f)
        section = m.group(1)
        dur = int(m.group(2) or 15)
        remote = "ads/" + f
        with open(full, "rb") as fh:
            data = fh.read()
        print(f"uploading {f} ({len(data)} bytes) -> {remote}")
        st = _put_contents(remote, data)
        print(f"  status {st}")
        if st not in (200, 201):
            print(f"  upload failed for {f}, aborting.")
            sys.exit(1)
        entries[section] = {
            "url": f"{RAW_BASE}/{remote}",
            "fallback": f"{RAW_BASE}/{remote}",
            "file": f,
            "duration": dur,
        }

    if not entries:
        print("Nothing to push (put files in ads_inbox/).")
        sys.exit(0)

    try:
        with open(MANIFEST, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except Exception:
        manifest = {"version": 1, "ads": {}}
    manifest["ads"].update(entries)
    manifest["version"] = int(manifest.get("version") or 0) + 1

    raw = json.dumps(manifest, indent=2).encode()
    print("Updating manifest (version %d)..." % manifest["version"])
    st = _put_contents("ads/ads.json", raw)
    print("  status", st)
    if st not in (200, 201):
        print("Manifest update failed.")
        sys.exit(1)

    print(f"Done. App will pick up v{manifest['version']} on next ad poll.")
    print("The app manifest lives at:", f"{RAW_BASE}/ads/ads.json")


if __name__ == "__main__":
    main()