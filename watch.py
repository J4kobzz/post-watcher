#!/usr/bin/env python3
"""Cloud watcher (GitHub Actions): new TikTok/YouTube post -> ntfy push with link, pin comment and a campaign button.
All account details live in repository secrets, nothing identifying is stored in this repo."""
import hashlib, json, os, re, subprocess, time, urllib.request
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

def push(title, msg, click, actions):
    req = urllib.request.Request(f"https://ntfy.sh/{TOPIC}", data=msg.encode(), method="POST", headers={
        "Title": title.encode("utf-8").decode("latin-1", "ignore") or "New post",
        "Priority": "high", "Tags": "rotating_light", "Click": click,
        "Actions": "; ".join(actions)})
    urllib.request.urlopen(req, timeout=30)

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
        key, hit = next(((kk, c) for kk, c in kit.items()
                         if any(m.lower() in title.lower() for m in c.get("match", []))), (None, {}))
        if key:
            live.setdefault(key, {})[platform] = live.get(key, {}).get(platform) or int(time.time())
        pin = hit.get("pinned_comment", "ask a question in the comments")
        cr = hit.get("campaign", CR)
        other = "TikTok" if platform == "YouTube" else "YouTube"
        msg = (f"{title[:80]}\n\nLink: {url}\n\n1) Submit this link on Content Rewards (30 min!)\n"
               f"2) Also submit your {other} post\n3) Pin comment: {pin}")
        push(f"NEW {platform} post live - submit now!", msg, url,
             [f"view, Content Rewards, {cr}", f"view, Open post, {url}"])
        print("pushed", platform, vid)
    seen[platform] = sorted(old | {H(v) for v, _ in items})

now = int(time.time())
for key, plats in live.items():
    if len(plats) >= 2 or key in warned:
        continue
    have, ts = next(iter(plats.items()))
    missing = "TikTok" if have == "YouTube" else "YouTube"
    if now - ts >= 20 * 60 and found.get(missing):
        name = kit.get(key, {}).get("youtube_title") or kit.get(key, {}).get("tiktok_caption") or key
        name = re.sub(r"#\w+", "", name).strip()[:70]
        push(f"WARNING: {missing} post missing!",
             f"\"{name}\" is live on {have} but NOT on {missing}.\n\nUpload it to {missing} now (same file), then submit the link.",
             CR, [f"view, Content Rewards, {kit.get(key, {}).get('campaign', CR)}"])
        warned.append(key)
        print("warned missing", missing, key)
Path("seen.json").write_text(json.dumps(seen, indent=1))
