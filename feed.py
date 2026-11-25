import json, os, time, urllib.request
import xml.etree.ElementTree as ET

FEEDS = [
    "https://www.reddit.com/r/AnikageCommunity/",
    "https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us",
]
WEBHOOK = os.environ["DISCORD_WEBHOOK"]
SEEN_FILE = "seen.json"
UA = "Mozilla/5.0 (compatible; feed-bot/1.0)"

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=30).read()

def parse(xml):
    items = []
    for e in ET.fromstring(xml).iter():
        tag = e.tag.split("}")[-1]
        if tag == "entry":
            title = link = None
            for c in e:
                t = c.tag.split("}")[-1]
                if t == "title": title = c.text
                if t == "link": link = c.get("href")
            items.append((title, link))
        elif tag == "item":
            items.append((e.findtext("title"), e.findtext("link")))
    return [(t, l) for t, l in items if t and l]

def post(title, link, source):
    body = json.dumps({
        "username": "Feed Bot",
        "embeds": [{"title": title[:256], "url": link,
                    "description": source, "color": 5814783}],
    }).encode()
    req = urllib.request.Request(WEBHOOK, data=body, headers={
        "Content-Type": "application/json", "User-Agent": UA})
    urllib.request.urlopen(req, timeout=30)

first_run = not os.path.exists(SEEN_FILE)
seen = set(json.load(open(SEEN_FILE))) if not first_run else set()

for feed in FEEDS:
    try:
        items = parse(fetch(feed))
    except Exception as ex:
        print("Failed:", feed, ex)
        continue
    for title, link in reversed(items):
        if link in seen:
            continue
        seen.add(link)
        if not first_run:
            post(title, link, feed)
            time.sleep(1)

json.dump(list(seen)[-2000:], open(SEEN_FILE, "w"))
