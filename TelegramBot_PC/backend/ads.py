"""Ad manager for the desktop app.

Downloads ad clips listed in a remote manifest (plain JSON) into the writable
data dir, serves them locally for playback, and enforces the ad cadence:
one compulsory ad on launch, then one every hour. The manifest is replaced by
a small push script whenever new creative is dropped in.
"""
import json
import os
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request

# Remote manifest with per-section ad entries. Overridable per deployment.
AD_MANIFEST_URL = os.environ.get(
    "FLIX_AD_MANIFEST",
    "https://raw.githubusercontent.com/Joe2016-alt/Joe1-2049/main/ads/ads.json",
)
AD_INTERVAL = 3600  # 1 ad per hour

_manifest_lock = threading.Lock()
_manifest_cache = {"val": None, "ts": 0.0}

# Hosts the ad manifest is allowed to come from. The manifest decides what URL
# the client then downloads and auto-plays, so it is treated as remote control
# data: only our own raw.githubusercontent.com content is trusted.
_TRUSTED_MANIFEST_HOSTS = ("raw.githubusercontent.com",)


def _is_trusted_manifest_url(url):
    try:
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    return host in _TRUSTED_MANIFEST_HOSTS


def _sha256_file(path, chunk=1024 * 256):
    import hashlib
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()
    except Exception:
        return ""
_clips_lock = threading.Lock()
_clips = {}


class AdService:
    def __init__(self, root):
        self.root = os.path.join(root, "ads")
        self.state_file = os.path.join(root, "ads_state.json")
        self._lock = threading.Lock()
        try:
            os.makedirs(self.root, exist_ok=True)
        except Exception:
            pass
        # seed in-memory clip map from any previously downloaded files
        try:
            for f in os.listdir(self.root):
                if os.path.isfile(os.path.join(self.root, f)):
                    _clips[f] = os.path.join(self.root, f)
        except Exception:
            pass

    def _state(self):
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self, st):
        try:
            with open(self.state_file, "w", encoding="utf-8") as f:
                json.dump(st, f)
        except Exception:
            pass

    def fetch_manifest(self):
        now = time.time()
        with _manifest_lock:
            if _manifest_cache["val"] and now - _manifest_cache["ts"] < 120:
                return _manifest_cache["val"]
        m = None
        # Try the configured manifest first, then let repo_hub find whichever
        # GitHub repo is live. Without this the ad stream would die the moment
        # the primary repo is blocked, even though a replacement already
        # matches one of the known naming patterns.
        urls = [AD_MANIFEST_URL]
        try:
            import repo_hub
            alt = repo_hub.ads_manifest_url(repo_hub.active(want_assets=False))
            if alt and alt not in urls:
                urls.append(alt)
        except Exception as e:
            print(f"[ads] repo failover unavailable: {e}", flush=True)

        for url in urls:
            # The manifest is remote control data. Only fetch it from the hosts
            # we control (raw.githubusercontent.com) so a stray env override
            # cannot point the client at an arbitrary server.
            if not _is_trusted_manifest_url(url):
                print(f"[ads] refusing untrusted manifest host: {url}", flush=True)
                continue
            try:
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "FlixAds/1.0", "Cache-Control": "no-cache"},
                )
                with urllib.request.urlopen(req, timeout=6) as r:
                    m = json.loads(r.read().decode("utf-8", "replace"))
                if m:
                    break
            except Exception:
                m = None
        with _manifest_lock:
            if m:
                _manifest_cache["val"] = m
            _manifest_cache["ts"] = now
        return _manifest_cache["val"]

    def get(self, section):
        """Return {section, path, duration, type, click_url, image_url} for a section's current ad."""
        m = self.fetch_manifest() or {}
        ads_cfg = m.get("ads") or {}
        ad = ads_cfg.get(section) or {}
        duration = int(ad.get("duration") or 0)
        ad_type = str(ad.get("type") or "video").lower()
        click_url = ad.get("click_url") or ad.get("href") or ad.get("redirect_url") or ad.get("url_redirect") or ""
        image_url = ad.get("image") or ad.get("banner") or ad.get("src") or ad.get("thumb") or ""

        # Popunder/redirect/banner ads
        if ad_type in ("link", "redirect", "popunder", "banner", "image"):
            # Still allow manifest failover logic but don't require mp4
            self._drop_stale(ads_cfg)
            return {
                "section": section,
                "path": None,
                "duration": duration or 6,
                "type": ad_type,
                "click_url": click_url,
                "image_url": image_url,
            }

        # The manifest supplies the filename, so treat it as untrusted and keep
        # it a plain basename -- otherwise "file": "..\\..\\x" writes outside
        # the ads directory.
        fname = os.path.basename((ad.get("file") or "").strip()) or ("ad_%s.mp4" % section)
        fname = re.sub(r"[^A-Za-z0-9._-]", "_", fname)
        url = ad.get("url") or ad.get("fallback") or ""
        want_sha = str(ad.get("sha256") or "").strip().lower()
        path = os.path.join(self.root, fname)
        # Only ever fetch a clip over TLS. An ad slide is still remote data
        # that this app writes to disk and auto-plays; plain HTTP would let it
        # be swapped in transit.
        if url and not url.lower().startswith("https://"):
            print(f"[ads] refusing non-https ad url for {section}", flush=True)
            url = ""
        if url:
            try:
                need = (not os.path.exists(path)) or os.path.getsize(path) < 100
                if not need and want_sha:
                    need = _sha256_file(path) != want_sha
                if need:
                    tmp = path + ".part"
                    req = urllib.request.Request(url, headers={"User-Agent": "FlixAds/1.0"})
                    with urllib.request.urlopen(req, timeout=30) as r, open(tmp, "wb") as f:
                        shutil.copyfileobj(r, f)
                    ok = os.path.exists(tmp) and os.path.getsize(tmp) > 100
                    if ok and want_sha and _sha256_file(tmp) != want_sha:
                        print(f"[ads] sha256 mismatch for {section}; discarding", flush=True)
                        ok = False
                    if ok:
                        os.replace(tmp, path)
                    else:
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass
                if os.path.exists(path):
                    with _clips_lock:
                        _clips[fname] = path
                    self._drop_stale(ads_cfg)
                    return {"section": section, "path": path, "duration": duration, "type": "video"}
            except Exception:
                pass
        if os.path.exists(path):
            return {"section": section, "path": path, "duration": duration, "type": "video"}
        return {"section": section, "path": None, "duration": duration, "type": "video"}

    def _drop_stale(self, ads_cfg):
        """Delete old clips that are no longer referenced by the manifest."""
        keep = {
            (e.get("file") or "").strip()
            for e in ads_cfg.values() if isinstance(e, dict) and (e.get("file") or "").strip()
        }
        try:
            for f in os.listdir(self.root):
                if f in keep or f.startswith("."):
                    continue
                full = os.path.join(self.root, f)
                if os.path.isfile(full):
                    try:
                        os.remove(full)
                    except Exception:
                        pass
        except Exception:
            pass

    def require(self):
        """Enforce cadence: launch-ad until acked, then one per hour."""
        now = int(time.time())
        st = self._state()
        launch_acked = bool(st.get("launch_acked"))
        last_h = int(st.get("last_hourly") or 0)
        if not launch_acked:
            return {
                "required": True, "reason": "launch",
                "nextIn": None, "launch_acked": False,
            }
        if now - last_h >= AD_INTERVAL:
            return {
                "required": True, "reason": "hourly",
                "nextIn": AD_INTERVAL - (now - last_h), "launch_acked": True,
            }
        return {
            "required": False, "reason": None,
            "nextIn": AD_INTERVAL - (now - last_h), "launch_acked": True,
        }

    def ack(self, rtype):
        st = self._state()
        now = int(time.time())
        if rtype == "launch":
            st["launch_acked"] = True
            st["last_hourly"] = now
        elif rtype == "hourly":
            st["last_hourly"] = now
        self._save_state(st)
        return self.require()