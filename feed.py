import colorsys, html, io, json, os, re, time, urllib.request, urllib.error
import xml.etree.ElementTree as ET

# ===== EDIT HERE: "Name": ("feed URL", webhook number) =====
FEEDS = {
    "r/python": ("https://www.reddit.com/r/python/.rss", 1),
    "Hacker News": ("https://hnrss.org/frontpage", 2),
}
# ===========================================================

SEEN_FILE = "seen.json"
UA = "Mozilla/5.0 (compatible; feed-bot/1.0)"
PURPLE = 0x9B59B6
MAX_PER_FEED = 10  # per run, avoids floods


def load_webhooks():
    hooks = {}
    try:
        secrets = json.loads(os.environ.get("ALL_SECRETS", "{}"))
    except Exception:
        secrets = {}
    for key, val in secrets.items():
        m = re.fullmatch(r"DISCORD_WEBHOOK_(\d+)", key.upper())
        if m and val:
            hooks[int(m.group(1))] = val
    old = secrets.get("DISCORD_WEBHOOK")  # old name still works as #1
    if old and 1 not in hooks:
        hooks[1] = old
    return hooks


WEBHOOKS = load_webhooks()
print("Webhooks found:", sorted(WEBHOOKS))


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read()


def local(tag):
    return tag.split("}")[-1]


def clean(text, limit=300):
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("..." if len(text) > limit else "")


def parse(xml):
    items = []
    for e in ET.fromstring(xml).iter():
        if local(e.tag) not in ("entry", "item"):
            continue
        title = link = summary = image = None
        html_blob = ""
        for c in e:
            t = local(c.tag)
            if t == "title" and title is None:
                title = (c.text or "").strip()
            elif t == "link" and link is None:
                link = c.get("href") or (c.text or "").strip()
            elif t in ("description", "summary", "content", "encoded"):
                html_blob += html.unescape(c.text or "")
                if summary is None:
                    summary = clean(c.text)
            if t in ("thumbnail", "content") and c.get("url"):
                image = image or c.get("url")
            elif t == "enclosure" and "image" in (c.get("type") or ""):
                image = image or c.get("url")
        if not image:
            m = re.search(r'<img[^>]+src=["\']([^"\']+)', html_blob)
            if m:
                image = m.group(1)
        if image and not image.startswith("http"):
            image = None
        if title and link:
            items.append((title, link, summary or "", image))
    return items


def dominant_color(url):
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(fetch(url))).convert("RGB").resize((64, 64))
        best, best_score = None, -1
        for count, (r, g, b) in img.getcolors(64 * 64):
            h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
            score = count * (0.15 + s) * (0.3 + v)
            if score > best_score:
                best, best_score = (r, g, b), score
        r, g, b = best
        return (r << 16) + (g << 8) + b
    except Exception as ex:
        print("Color failed:", ex)
        return PURPLE


def send(webhook, embed):
    body = json.dumps({"username": "Feed Bot", "embeds": [embed]}).encode()
    for _ in range(3):
        req = urllib.request.Request(webhook, data=body, headers={
            "Content-Type": "application/json", "User-Agent": UA})
        try:
            urllib.request.urlopen(req, timeout=30)
            return True
        except urllib.error.HTTPError as ex:
            if ex.code == 429:
                try:
                    wait = float(json.loads(ex.read()).get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(wait + 0.5)
                continue
            print("Discord error:", ex.code)
            return False
        except Exception as ex:
            print("Discord failed:", ex)
            return False
    return False


def post(title, link, summary, image, source, webhook):
    desc = (summary + "\n\n" if summary else "") + f"**[Read here]({link})**"
    embed = {
        "title": title[:256],
        "url": link,
        "description": desc[:4000],
        "color": dominant_color(image) if image else PURPLE,
        "footer": {"text": source},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if image:
        embed["image"] = {"url": image}
    return send(webhook, embed)


first_run = not os.path.exists(SEEN_FILE)
seen = [] if first_run else json.load(open(SEEN_FILE))
seen_set = set(seen)

if first_run:
    for hook in WEBHOOKS.values():
        post(
            "Feed Bot is live 👀",
            "https://github.com/leo-gold16/Feeds",
            "Something's coming... new posts from your feeds will land here automatically.",
            None,
            "Teaser",
            hook,
        )

try:
    for name, (url, num) in FEEDS.items():
        hook = WEBHOOKS.get(num)
        if not hook:
            print(f"Skipped {name}: no secret DISCORD_WEBHOOK_{num}")
            continue
        try:
            items = parse(fetch(url))
        except Exception as ex:
            print("Failed:", name, ex)
            continue
        sent = 0
        for title, link, summary, image in reversed(items):
            if link in seen_set:
                continue
            if first_run or sent >= MAX_PER_FEED:
                seen.append(link)
                seen_set.add(link)
                continue
            if post(title, link, summary, image, name, hook):
                seen.append(link)
                seen_set.add(link)
                sent += 1
                time.sleep(1)
finally:
    json.dump(seen[-3000:], open(SEEN_FILE, "w"))
