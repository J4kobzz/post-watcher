#!/usr/bin/env python3
"""Cloud watcher (GitHub Actions): new TikTok/YouTube post -> ntfy push with link, pin comment and a campaign button.
All account details live in repository secrets, nothing identifying is stored in this repo."""
import hashlib, json, os, re, subprocess, time, urllib.error, urllib.request
from pathlib import Path

CFG = json.loads(os.environ["WATCH_CONFIG"])  # {"tiktok": "...", "youtube": "...", "yt_channel": "...", "campaign": "...", "kit": {...}}
YT_CHANNEL = CFG["yt_channel"]
SOURCES = {
    "YouTube": f"https://www.youtube.com/@{CFG['youtube']}/shorts",
    "TikTok": f"https://www.tiktok.com/@{CFG['tiktok']}",
}
LINKS = {"YouTube": "https://www.youtube.com/shorts/{id}", "TikTok": "https://www.tiktok.com/@" + CFG["tiktok"] + "/video/{id}"}
CR = CFG["campaign"]
TOPIC = os.environ["NTFY_TOPIC"]
H = lambda v: hashlib.sha256(v.encode()).hexdigest()[:16]  # store only hashes of post ids
seen = json.loads(Path("seen.json").read_text() or "{}")
live = seen.setdefault("_live", {})      # {kit_key: {"TikTok": ts, "YouTube": ts}}
warned = seen.setdefault("_warned", [])  # kit keys already warned about
# posts only a streamer catch-all (*-any) matched: [campaign hash, platform, ts], kept 2 days. Used to pair a clip
# whose TikTok caption shares no words with its YouTube title, so it isn't reported as missing.
anyhits = [h for h in seen.setdefault("_any_hits", []) if time.time() - h[2] < 2 * 86400]
seen["_any_hits"] = anyhits
kit = CFG.get("kit", {})
first = not seen

def ytdlp(url):
    r = subprocess.run(["yt-dlp", "--flat-playlist", "--playlist-end", "6", "--print", "%(id)s\t%(title)s", url],
                       capture_output=True, text=True, timeout=120)
    return [tuple(l.split("\t", 1)) for l in r.stdout.splitlines() if "\t" in l]

def yt_rss():
    try:
        x = urllib.request.urlopen(f"https://www.youtube.com/feeds/videos.xml?channel_id={YT_CHANNEL}", timeout=30).read().decode()
        ids = re.findall(r"<yt:videoId>([^<]+)</yt:videoId>", x)
        titles = re.findall(r"<entry>.*?<title>([^<]*)</title>", x, re.S)
        return list(zip(ids, titles))
    except Exception:
        return []

def push(title, msg, click, actions, priority="urgent", tags="rotating_light"):
    req = urllib.request.Request(f"https://ntfy.sh/{TOPIC}", data=msg.encode(), method="POST", headers={
        "Title": title.encode("utf-8").decode("latin-1", "ignore") or "New post",
        "Priority": priority, "Tags": tags, "Click": click,
        "Actions": "; ".join(actions)})
    urllib.request.urlopen(req, timeout=30)

def catchall(k):
    """Streamer catch-all entries are exactly '<streamer>-any' (a clip slug can end in '-any' too, e.g. 'trusts-any')."""
    return bool(re.fullmatch(r"[a-z0-9]+-any", k or ""))

def match(title):
    """Most specific kit entry for a post title. A clip's own entry beats the streamer catch-all (*-any), and
    among clip entries the longest matching phrase wins, so the TikTok and YouTube posts of one clip land on
    the same entry (they used to split, which caused false 'post missing' warnings). Scored by total matched
    text, so two real hits ('defends' + 'foltyn') beat one shared word from another clip ('instantly')."""
    t, best = title.lower(), None
    for kk, c in kit.items():
        hits = [m for m in c.get("match", []) if m.lower() in t]
        if hits:
            score = (not catchall(kk), sum(len(m) for m in hits), len(hits))
            if best is None or score > best[0]:
                best = (score, kk, c)
    return (best[1], best[2]) if best else (None, {})

_camps = {}
def campaign(cr_url):
    """{"closed": bool, "platforms": set or None} from Content Rewards' public API. closed = takes no submissions
    (paused, or ended = "not found"). platforms = where it pays. Unknown (network error, odd reply) counts as
    open with unknown platforms, so a real alert is never lost."""
    m = re.search(r"campaigns/([0-9a-f-]{36})", cr_url or "")
    if not m:
        return {"closed": False, "platforms": None}
    cid = m.group(1)
    if cid not in _camps:
        info = {"closed": False, "platforms": None}
        try:
            req = urllib.request.Request(f"https://contentrewards.com/api/campaign/campaigns/{cid}",
                                         headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
            d = json.loads(urllib.request.urlopen(req, timeout=20).read()).get("data") or {}
            info["closed"] = bool(d.get("submissionsClosedAt")) or d.get("status") not in (None, "active")
            pay = (d.get("configuration") or {}).get("payoutModel") or []
            info["platforms"] = {x["platform"].lower() for x in pay if x.get("platform")} or None
        except urllib.error.HTTPError as e:
            info["closed"] = e.code == 404
        except Exception:
            pass
        _camps[cid] = info
    return _camps[cid]

_done = None
def auto_submitted():
    """Post ids Clip HQ (on the Mac) already submitted: its "Auto-submitted" pushes on this same ntfy topic, last 12 h.
    The watcher then sends a quiet "already submitted" note instead of a misleading "submit now!"."""
    global _done
    if _done is None:
        _done = set()
        try:
            r = urllib.request.urlopen(f"https://ntfy.sh/{TOPIC}/json?poll=1&since=12h", timeout=20).read().decode()
            for line in r.splitlines():
                m = json.loads(line)
                if "Auto-submitted" in (m.get("title") or ""):
                    _done.update(re.findall(r"(?:video/|shorts/)([\w-]+)", m.get("message") or ""))
        except Exception:
            pass
    return _done

def campaign_closed(cr_url):
    return campaign(cr_url)["closed"]

found = {}
for platform, src in SOURCES.items():
    items = ytdlp(src)
    if platform == "YouTube":
        items = items + [i for i in yt_rss() if i[0] not in {a for a, _ in items}]
    found[platform] = items

for platform, items in found.items():
    if platform.startswith("_"):
        continue
    if not items:
        print(f"{platform}: nothing loaded (blocked or empty), will retry")
        continue
    old = set(seen.get(platform, []))
    for vid, title in items:
        if H(vid) in old or first:
            continue
        url = LINKS[platform].format(id=vid)
        key, hit = match(title)
        if key and not catchall(key):   # catch-alls can't tell clips apart, so no pairing check for them
            live.setdefault(key, {})[platform] = live.get(key, {}).get(platform) or int(time.time())
        elif key:
            anyhits.append([H(hit.get("campaign", CR)), platform, int(time.time())])
        pin = hit.get("pinned_comment", "ask a question in the comments")
        cr = hit.get("campaign", CR)
        other = "TikTok" if platform == "YouTube" else "YouTube"
        if vid in auto_submitted():
            push(f"{platform} post live - already auto-submitted", f"{title[:80]}\n\nLink: {url}\n\nClip HQ already submitted "
                 f"this to Content Rewards, nothing to do. Just pin the comment: {pin}",
                 url, [f"view, Open post, {url}"], priority="default", tags="white_check_mark")
            print("pushed (already submitted)", platform, vid)
        elif key and campaign_closed(cr):
            push(f"{platform} post live - campaign paused, nothing to submit",
                 f"{title[:80]}\n\nLink: {url}\n\nThis campaign isn't taking submissions right now, so there's "
                 f"nothing to submit. Just pin the comment: {pin}",
                 url, [f"view, Open post, {url}"], priority="default", tags="pause_button")
            print("pushed (campaign closed)", platform, vid)
        else:
            msg = (f"{title[:80]}\n\nLink: {url}\n\n1) Submit this link on Content Rewards (30 min!)\n"
                   f"2) Also submit your {other} post\n3) Pin comment: {pin}")
            push(f"NEW {platform} post live - submit now!", msg, url,
                 [f"view, Content Rewards, {cr}", f"view, Open post, {url}"])
            print("pushed", platform, vid)
    seen[platform] = sorted(old | {H(v) for v, _ in items})

now = int(time.time())
for key, plats in live.items():
    if len(plats) >= 2 or key in warned or catchall(key):
        continue
    have, ts = next(iter(plats.items()))
    missing = "TikTok" if have == "YouTube" else "YouTube"
    camp = H(kit.get(key, {}).get("campaign", CR))
    if any(c == camp and p == missing and abs(t - ts) < 3 * 3600 for c, p, t in anyhits):
        continue   # the other post went live too, it just only matched the streamer catch-all
    pays = campaign(kit.get(key, {}).get("campaign", CR))["platforms"]
    if pays and missing.lower() not in pays:
        continue   # campaign doesn't pay on that platform (e.g. SourJacks is TikTok only), nothing is missing
    if now - ts >= 20 * 60 and found.get(missing):
        name = kit.get(key, {}).get("youtube_title") or kit.get(key, {}).get("tiktok_caption") or key
        name = re.sub(r"#\w+", "", name).strip()[:70]
        then = "" if campaign_closed(kit.get(key, {}).get("campaign", CR)) else ", then submit the link"
        push(f"WARNING: {missing} post missing!",
             f"\"{name}\" is live on {have} but NOT on {missing}.\n\nUpload it to {missing} now (same file){then}.",
             CR, [f"view, Content Rewards, {kit.get(key, {}).get('campaign', CR)}"])
        warned.append(key)
        print("warned missing", missing, key)
Path("seen.json").write_text(json.dumps(seen, indent=1))
