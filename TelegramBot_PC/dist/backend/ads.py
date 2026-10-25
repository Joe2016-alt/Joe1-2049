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
AD_INTERVAL = 900  # 1 ad per 15 minutes

_manifest_lock = threading.Lock()
_manifest_cache = {"val": None, "ts": 0.0}

# Hosts the ad manifest is allowed to come from. The manifest decides what URL
# the client then downloads and auto-plays, so it is treated as remote control
# data: only our own raw.githubusercontent.com content is trusted.
_TRUSTED_MANIFEST_HOSTS = ("raw.githubusercontent.com",)

_ZONE_ID_RE = re.compile(r'data-zoneid\s*=\s*["\']?(\d+)', re.I)
_ZONE_CLASS_RE = re.compile(r'<ins[^>]*\bclass\s*=\s*["\']([^"\']*)["\']', re.I)
_ZONE_ANYCLASS_RE = re.compile(r'<[a-z][^>]*\bclass\s*=\s*["\']([^"\']*)["\']', re.I)
_ZONE_SRC_RE = re.compile(r'<script[^>]*\bsrc\s*=\s*["\']([^"\']+)["\']', re.I)
_ZONE_DEFAULT_SRC = "https://a.magsrv.com/ad-provider.js"


def _zone_from(ad):
    """Normalize an ExoClick banner zone into the values the gate renders.

    Same contract as portal.py::_zone_from: accept the tag pasted verbatim
    ("zone_tag") or the structured form ("zone": {id, cls, src}), and hand the
    page only the zone id + <ins> class + provider script URL. Raw HTML from a
    remote manifest is never injected into the page.
    """
    if not isinstance(ad, dict):
        return None
    z = ad.get("zone")
    zid = cls = src = ""
    width, height = 300, 250
    if isinstance(z, dict):
        zid = str(z.get("id") or z.get("zone_id") or "").strip()
        cls = str(z.get("class") or z.get("cls") or "").strip()
        src = str(z.get("src") or z.get("script") or "").strip()
        width = z.get("width") or width
        height = z.get("height") or height
    tag = ad.get("zone_tag") or (z if isinstance(z, str) else "")
    if not zid and tag:
        m = _ZONE_ID_RE.search(tag)
        zid = m.group(1) if m else ""
        m = _ZONE_CLASS_RE.search(tag) or _ZONE_ANYCLASS_RE.search(tag)
        cls = m.group(1).strip() if m else ""
        m = _ZONE_SRC_RE.search(tag)
        src = m.group(1).strip() if m else ""
    if not zid.isdigit():
        return None
    if src.startswith("//"):
        src = "https:" + src
    if not src.startswith("https://"):
        src = _ZONE_DEFAULT_SRC
    try:
        width = max(120, min(970, int(width)))
        height = max(60, min(640, int(height)))
    except (TypeError, ValueError):
        width, height = 300, 250
    return {"id": zid, "cls": cls, "src": src, "width": width, "height": height}


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
        # Last good manifest, kept outside the clip dir so _drop_stale can't
        # delete it. Lets the gate answer instantly (and offline) on later boots.
        self.manifest_file = os.path.join(root, "ads_manifest.json")
        self._lock = threading.Lock()
        # Launch ad is per PROCESS, not per install: the persisted
        # "launch_acked" used to be written once and honoured forever, so the
        # gate only ever showed on the very first boot after installing.
        self._launch_acked = False
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

    def _fetch_json(self, url, timeout=4):
        """GET a manifest URL.

        The manifest is remote control data (it decides what the client
        downloads and opens), so only our own raw.githubusercontent.com
        content is trusted -- a stray env override must not be able to point
        the client at an arbitrary server.
        """
        if not _is_trusted_manifest_url(url):
            print(f"[ads] refusing untrusted manifest host: {url}", flush=True)
            return None
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "FlixAds/1.0", "Cache-Control": "no-cache"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as r:
                m = json.loads(r.read().decode("utf-8", "replace"))
            return m if isinstance(m, dict) and m else None
        except Exception:
            return None

    def _load_cached_manifest(self):
        try:
            with open(self.manifest_file, "r", encoding="utf-8") as f:
                m = json.load(f)
            return m if isinstance(m, dict) and m else None
        except Exception:
            return None

    def _save_cached_manifest(self, m):
        try:
            tmp = self.manifest_file + ".part"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(m, f)
            os.replace(tmp, self.manifest_file)
        except Exception:
            pass

    def start_warm(self):
        """Fetch the manifest off the request thread.

        The launch gate is the first caller of /api/ads, so without a warm
        thread it pays the entire network cost and can pop up ~20s into the
        session instead of at launch.
        """
        def _run():
            try:
                self.fetch_manifest()
            except Exception:
                pass
        threading.Thread(target=_run, daemon=True, name="ads-warm").start()

    def fetch_manifest(self):
        now = time.time()
        with _manifest_lock:
            if _manifest_cache["val"] and now - _manifest_cache["ts"] < 120:
                return _manifest_cache["val"]

        # 1) The configured manifest is the normal path and must stay cheap.
        #    repo_hub.active() walks GitHub candidates and measured ~20s, so it
        #    is only consulted when the primary is genuinely unreachable.
        m = self._fetch_json(AD_MANIFEST_URL)

        # 2) Failover: find whichever replacement repo is live.
        if not m:
            try:
                import repo_hub
                alt = repo_hub.ads_manifest_url(repo_hub.active(want_assets=False))
                if alt and alt != AD_MANIFEST_URL:
                    m = self._fetch_json(alt)
            except Exception as e:
                print(f"[ads] repo failover unavailable: {e}", flush=True)

        if m:
            with _manifest_lock:
                _manifest_cache["val"] = m
                _manifest_cache["ts"] = now
            self._save_cached_manifest(m)
            return m

        # 3) Offline / blocked: reuse the last good copy so the compulsory gate
        #    still has something to show instead of silently disappearing.
        cached = self._load_cached_manifest()
        if cached:
            with _manifest_lock:
                _manifest_cache["val"] = cached
                _manifest_cache["ts"] = now
            return cached

        with _manifest_lock:
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
        # "image_url" is the key the published manifest actually uses; the
        # shorter aliases are kept for hand-written manifests.
        image_url = (ad.get("image_url") or ad.get("image") or ad.get("banner")
                     or ad.get("src") or ad.get("thumb") or "")

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
                # ExoClick banner zone (CPM). When present the gate renders the
                # network's own creative, so the impression is a real, paid ad
                # request instead of a self-hosted image behind a redirect.
                "zone": _zone_from(ad),
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
        """Enforce cadence: one compulsory ad at every launch, then one per
        AD_INTERVAL (15 min)."""
        now = int(time.time())
        st = self._state()
        launch_acked = self._launch_acked
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
            self._launch_acked = True
            st["last_hourly"] = now
        elif rtype == "hourly":
            st["last_hourly"] = now
        # Drop the legacy key so a stale True can never leak into a future read.
        st.pop("launch_acked", None)
        self._save_state(st)
        return self.require()