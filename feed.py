import colorsys, datetime as dt, html, io, ipaddress, json, os, re, socket, sys, time
import urllib.error, urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from functools import lru_cache
from html.parser import HTMLParser
from urllib.parse import urlparse, urljoin, urlunparse, parse_qsl, urlencode

# ===== EDIT HERE: "Feed name": ("feed URL", webhook number) =====
FEEDS = {
    "r/AnikageCommunity": ("https://www.reddit.com/r/AnikageCommunity/.rss", 1),
    "Anime News Network": ("https://www.animenewsnetwork.com/all/rss.xml?ann-edition=us", 2),
    "Otaku Down": ("https://otakudown.com/feed/", 3),
    "Otaku USA Magazine": ("https://otakuusamagazine.com/anime/feed/", 2),
    "Anime Daily": ("https://www.theanimedaily.com/feed/", 2),
    "Anime Herald": ("https://animeherald.com/feed/", 2),
}
FOOTER = "Anikage Community"
# ================================================================

PURPLE = 0x9B59B6
MAX_PER_FEED = 10            # max new posts per feed per run
SEEN_FILE = "seen.json"
SEEN_LIMIT = 5000
FEED_MAX_BYTES = 5 * 1024 * 1024
IMG_MAX_BYTES = 8 * 1024 * 1024
TIMEOUT = 25
UA = "Mozilla/5.0 (compatible; feed-bot/2.0)"
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp")
MRSS = "http://search.yahoo.com/mrss/"
TEXT_NS = {"", "http://www.w3.org/2005/Atom", "http://purl.org/rss/1.0/",
           "http://purl.org/rss/1.0/modules/content/", "http://purl.org/dc/elements/1.1/",
           "http://www.w3.org/1999/xhtml"}
HOOK_RE = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+/?$")
ESC = re.compile(r"([\\*_~`|\[\]])")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
JUNK_IMG = re.compile(r"(pixel|spacer|blank\.gif|emoji|gravatar|doubleclick|feedburner\.com/~|/ads?/)", re.I)


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- URL safety
def safe_url(u, base=None):
    u = html.unescape(u or "").strip()
    if not u or re.search(r"[\x00-\x1f\x7f]", u):
        return None
    try:
        if base:
            u = urljoin(base, u)
        elif u.startswith("//"):
            u = "https:" + u
        p = urlparse(u)
        if p.scheme not in ("http", "https") or not p.netloc or p.username or p.password:
            return None
    except ValueError:
        return None
    u = u.replace(" ", "%20").replace("<", "%3C").replace(">", "%3E").replace('"', "%22")
    return u if len(u) <= 2000 else None


def md_url(u):
    return u.replace("(", "%28").replace(")", "%29")


@lru_cache(maxsize=512)
def host_is_public(host):
    try:
        for info in socket.getaddrinfo(host, None):
            if not ipaddress.ip_address(info[4][0].split("%")[0]).is_global:
                return False
        return True
    except (OSError, ValueError):
        return False


def is_public_url(u):
    try:
        p = urlparse(u)
        return (p.scheme in ("http", "https") and bool(p.hostname)
                and p.port in (None, 80, 443) and host_is_public(p.hostname))
    except ValueError:
        return False


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not is_public_url(urljoin(req.full_url, newurl)):
            raise urllib.error.URLError("redirect to non-public address blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


OPENER = urllib.request.build_opener(SafeRedirect)


def http_get(url, max_bytes, accept="*/*"):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})
    with OPENER.open(req, timeout=TIMEOUT) as r:
        data = r.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("response too large")
    return data


def norm_url(u):
    p = urlparse(u)
    q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
         if not re.match(r"^(utm_|fbclid|gclid|mc_)", k, re.I)]
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", "", urlencode(q), ""))


def fix_feed_url(u):
    p = urlparse(u)
    if (p.hostname or "").endswith("reddit.com") and ".rss" not in p.path:
        return urlunparse(p._replace(path=p.path.rstrip("/") + "/.rss"))
    return u


# ------------------------------------------------------------- text cleaning
def plain(s):
    s = html.unescape(s or "")
    s = re.sub(r"<[^>]*>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def esc(s):
    return ESC.sub(r"\\\1", s)


def neutralize(s):
    s = s.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    return re.sub(r"<(@|#)", "<\u200b\\1", s)


def clip(s, n):
    s = (s or "").strip()
    if len(s) <= n:
        return s
    cut = s[:n - 1]
    sp = cut.rfind(" ")
    if sp > n * 0.6:
        cut = cut[:sp]
    return cut.rstrip(" ,.;:-") + "…"


def md_clip(s, n):
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(" ")
    if sp > n * 0.6:
        cut = cut[:sp]
    cut = re.sub(r"\[[^\]]*\]\([^)]*$", "", cut)
    cut = re.sub(r"\[[^\]]*$", "", cut).rstrip()
    t = re.sub(r"\\.", "", cut)
    for m in ("**", "~~", "`"):
        if t.count(m) % 2:
            cut += m
    if t.replace("**", "").count("*") % 2:
        cut += "*"
    return cut + "…"


class MdConverter(HTMLParser):
    """Converts untrusted HTML into safe Discord markdown. Drops scripts, styles, iframes etc."""
    SKIP = {"script", "style", "iframe", "object", "noscript", "svg", "form", "button",
            "head", "template", "select", "textarea"}
    BLOCK = {"p", "div", "section", "article", "ul", "ol", "table", "tr", "figure", "figcaption"}
    HEAD = {"h1", "h2", "h3", "h4", "h5", "h6"}

    def __init__(self, base=None):
        super().__init__(convert_charrefs=True)
        self.base, self.out, self.skip = base, [], 0
        self.links, self.quotes, self.code = [], [], 0

    def _nl(self, n):
        if not self.out:
            return
        tail = "".join(self.out[-3:])
        have = len(tail) - len(tail.rstrip("\n"))
        if have < n:
            self.out.append("\n" * (n - have))

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        a = dict(attrs)
        if tag in ("b", "strong"):
            self.out.append("**")
        elif tag in ("i", "em"):
            self.out.append("*")
        elif tag in ("s", "strike", "del"):
            self.out.append("~~")
        elif tag in self.HEAD:
            self._nl(2); self.out.append("**")
        elif tag == "code":
            self.code += 1; self.out.append("`")
        elif tag == "pre":
            self._nl(1); self.code += 1; self.out.append("```\n")
        elif tag == "a":
            self.links.append((len(self.out), safe_url(a.get("href"), self.base)))
        elif tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self._nl(1); self.out.append("• ")
        elif tag == "blockquote":
            self._nl(2); self.quotes.append(len(self.out))
        elif tag in self.BLOCK:
            self._nl(2 if tag in ("p", "div", "section", "article") else 1)

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("b", "strong"):
            self.out.append("**")
        elif tag in ("i", "em"):
            self.out.append("*")
        elif tag in ("s", "strike", "del"):
            self.out.append("~~")
        elif tag in self.HEAD:
            self.out.append("**"); self._nl(2)
        elif tag == "code":
            self.code = max(0, self.code - 1); self.out.append("`")
        elif tag == "pre":
            self.code = max(0, self.code - 1); self.out.append("\n```"); self._nl(1)
        elif tag == "a" and self.links:
            idx, href = self.links.pop()
            text = re.sub(r"\s+", " ", "".join(self.out[idx:])).strip()
            del self.out[idx:]
            if text:
                self.out.append(f"[{text}]({md_url(href)})" if href else text)
        elif tag == "blockquote" and self.quotes:
            idx = self.quotes.pop()
            text = "".join(self.out[idx:]).strip()
            del self.out[idx:]
            if text:
                self.out.append("> " + text.replace("\n\n", "\n").replace("\n", "\n> "))
                self._nl(2)
        elif tag in self.BLOCK or tag == "li":
            self._nl(2 if tag in ("p", "div", "section", "article") else 1)

    def handle_data(self, data):
        if self.skip:
            return
        if self.code:
            self.out.append(data.replace("`", "'"))
        else:
            self.out.append(ESC.sub(r"\\\1", re.sub(r"\s+", " ", data)))

    def result(self):
        s = "".join(self.out)
        s = re.sub(r"[ \t]+\n", "\n", s)
        s = re.sub(r"\n[ \t]+", "\n", s)
        s = re.sub(r"(\*\*|~~)(\s*)\1", r"\2", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        s = re.sub(r" {2,}", " ", s)
        return neutralize(s.strip())


def html_to_md(blob, base=None):
    if not blob:
        return ""
    try:
        p = MdConverter(base)
        p.feed(blob)
        p.close()
        return p.result()
    except Exception:
        return neutralize(esc(plain(blob)))


# ------------------------------------------------------------------- parsing
def local(tag):
    return tag.rsplit("}", 1)[-1]


def ns(tag):
    return tag[1:].split("}")[0] if tag.startswith("{") else ""


def etext(c):
    t = (c.text or "").strip()
    if not t and len(c):
        t = "".join(ET.tostring(x, encoding="unicode") for x in c)
    return t


def looks_image(url, mtype="", medium=""):
    if (mtype or "").startswith("image/") or medium == "image":
        return True
    return urlparse(url).path.lower().endswith(IMG_EXT)


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except Exception:
        try:
            d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    now = dt.datetime.now(dt.timezone.utc)
    if d.year < 2000 or d > now + dt.timedelta(days=1):
        return None
    return min(d, now)


def load_xml(data):
    if re.search(rb"<!ENTITY", data, re.I):
        raise ValueError("XML entity declarations are not allowed")
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        text = data.decode("utf-8", "ignore").lstrip("\ufeff \t\r\n")
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
        text = re.sub(r"^<\?xml[^>]*\?>", "", text)
        return ET.fromstring(text)


def parse_item(e, feed_url):
    d = {"title": "", "link": "", "raw_link": "", "id": "", "summary": "", "content": "",
         "author": "", "date": None, "updated": None, "cats": [], "cands": []}
    kids = []
    for c in e:
        kids.append(c)
        if local(c.tag) == "group":
            kids.extend(list(c))
    for c in kids:
        try:
            t, n, text = local(c.tag), ns(c.tag), (c.text or "").strip()
            if t == "content" and (n == MRSS or c.get("url")):
                u = c.get("url")
                if u and looks_image(u, c.get("type"), c.get("medium")):
                    d["cands"].append((1, u))
            elif t == "thumbnail" and c.get("url"):
                d["cands"].append((5, c.get("url")))
            elif t == "enclosure" and c.get("url") and looks_image(c.get("url"), c.get("type")):
                d["cands"].append((1, c.get("url")))
            elif t == "image" and c.get("href"):
                d["cands"].append((2, c.get("href")))
            elif t == "category" or t == "subject":
                v = c.get("label") or c.get("term") or text
                if v:
                    d["cats"].append(plain(v))
            elif n not in TEXT_NS:
                continue
            elif t == "title" and not d["title"]:
                d["title"] = plain(etext(c))
            elif t == "link":
                href, rel = c.get("href"), c.get("rel") or "alternate"
                if href and rel == "enclosure" and looks_image(href, c.get("type")):
                    d["cands"].append((1, href))
                elif href and rel == "alternate" and not d["raw_link"]:
                    d["raw_link"] = href
                elif not href and text and not d["raw_link"]:
                    d["raw_link"] = text
            elif t in ("id", "guid") and not d["id"]:
                d["id"] = text
            elif t in ("description", "summary") and not d["summary"]:
                d["summary"] = etext(c)
            elif t in ("content", "encoded") and not d["content"]:
                d["content"] = etext(c)
            elif t in ("published", "pubDate", "date") and not d["date"]:
                d["date"] = parse_date(text)
            elif t in ("updated", "modified") and not d["updated"]:
                d["updated"] = parse_date(text)
            elif t in ("author", "creator") and not d["author"]:
                name = ""
                for x in c:
                    if local(x.tag) == "name":
                        name = (x.text or "").strip()
                name = name or text
                m = re.search(r"\(([^)]+)\)", name)
                if m:
                    name = m.group(1)
                d["author"] = "" if EMAIL.match(name) else plain(name)
        except Exception:
            continue

    d["link"] = safe_url(d["raw_link"], feed_url) or safe_url(d["id"]) or ""
    blob = (d["content"] or "") + "\n" + (d["summary"] or "")
    for m in re.finditer(r'<a\b[^>]*?\bhref=["\']([^"\']+)["\']', blob, re.I):
        u = html.unescape(m.group(1))
        if urlparse(u).path.lower().endswith(IMG_EXT):
            d["cands"].append((3, u))
    for m in re.finditer(r"<img\b([^>]*)>", blob, re.I):
        attrs = m.group(1)
        wh = [re.search(r'\b%s=["\']?(\d+)' % k, attrs) for k in ("width", "height")]
        if any(x and int(x.group(1)) < 50 for x in wh):
            continue
        for key in ("data-src", "data-lazy-src", "src"):
            s = re.search(r'\b%s=["\']([^"\']+)["\']' % key, attrs)
            if s:
                d["cands"].append((4, s.group(1)))
                break
    seen, out = set(), []
    for _, u in sorted(d["cands"], key=lambda x: x[0]):
        u = safe_url(u, d["link"] or feed_url)
        if u and u not in seen and not JUNK_IMG.search(u):
            seen.add(u)
            out.append(u)
    d["cands"] = out[:5]
    return d


def parse_feed(data, feed_url):
    root = load_xml(data)
    items = [parse_item(e, feed_url) for e in root.iter() if local(e.tag) in ("item", "entry")]
    items = [i for i in items if i["link"]]
    keys = [i["date"] or i["updated"] for i in items]
    if items and all(keys):
        items.sort(key=lambda i: i["date"] or i["updated"])
    else:
        items.reverse()
    return items  # oldest first


# -------------------------------------------------------------------- images
def dominant_color(img, Image):
    rgba = img.convert("RGBA")
    rgba.thumbnail((96, 96))
    bg = Image.new("RGB", rgba.size, (32, 34, 37))
    bg.paste(rgba, mask=rgba.split()[3])
    q = bg.quantize(colors=8)
    pal = q.getpalette()
    best, best_score = None, -1
    for count, idx in q.getcolors():
        r, g, b = pal[idx * 3:idx * 3 + 3]
        _, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
        score = count * (0.1 + s) * (0.25 + v)
        if score > best_score:
            best, best_score = (r, g, b), score
    return max((best[0] << 16) + (best[1] << 8) + best[2], 0x010101)


def check_image(url):
    """Returns (url, colour) only if the image really loads and Discord can show it."""
    if not url or not is_public_url(url):
        return None
    try:
        data = http_get(url, IMG_MAX_BYTES, "image/jpeg,image/png,image/gif,image/webp,*/*;q=0.5")
    except Exception as ex:
        log("  image skipped:", type(ex).__name__)
        return None
    try:
        from PIL import Image
    except ImportError:
        return (url, PURPLE) if urlparse(url).path.lower().endswith(IMG_EXT) else None
    try:
        Image.MAX_IMAGE_PIXELS = 40_000_000
        img = Image.open(io.BytesIO(data))
        if img.format not in ("JPEG", "PNG", "GIF", "WEBP") or min(img.size) < 50:
            return None
        return (url, dominant_color(img, Image))
    except Exception as ex:
        log("  image invalid:", type(ex).__name__)
        return None


# -------------------------------------------------------------------- embeds
def iso(d):
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_embed(d, feed_name, feed_url):
    link = d["link"]
    lp = urlparse(link)
    reddit = (urlparse(feed_url).hostname or "").endswith("reddit.com")
    raw = d["content"] or d["summary"]
    body, sub, src = "", None, None

    if reddit:
        m = (re.search(r'<div class="md">(.*)</div>\s*<!--\s*SC_ON', raw, re.S)
             or re.search(r'<div class="md">(.*?)</div>', raw, re.S))
        body = html_to_md(m.group(1), link) if m else ""
        ml = re.search(r'href=["\']([^"\']+)["\']\s*>\s*\[link\]', raw)
        src = safe_url(ml.group(1)) if ml else None
        sub = re.sub(r"^/?r/", "", d["cats"][0]) if d["cats"] else None
        if not sub:
            ms = re.search(r"/r/([A-Za-z0-9_]+)", feed_url)
            sub = ms.group(1) if ms else None
        limit = 700
    else:
        body = html_to_md(d["summary"] or d["content"], link)
        if len(body) < 20 and d["content"]:
            body = html_to_md(d["content"], link)
        body = re.sub(r"\s*The post .*? appeared first on .*?\.?\s*$", "", body, flags=re.S | re.I)
        limit = 500

    image, color = None, PURPLE
    for u in d["cands"][:4]:
        res = check_image(u)
        if res:
            image, color = res
            break

    links = [f"**[Read here]({md_url(link)})**"]
    if reddit and src and src != link and not looks_image(src) \
            and not (urlparse(src).hostname or "").endswith(("reddit.com", "redd.it")):
        links.append(f"[Source]({md_url(src)})")
    desc = "\n\n".join(x for x in (md_clip(body, limit) if body else "", " • ".join(links)) if x)

    when = d["date"] or d["updated"]
    fields = []
    if reddit:
        if sub:
            ok = re.fullmatch(r"[A-Za-z0-9_]{2,21}", sub)
            fields.append({"name": "Subreddit", "inline": True,
                           "value": f"[r/{sub}](https://www.reddit.com/r/{sub})" if ok else esc("r/" + sub)})
        au = re.sub(r"^/?u/", "", d["author"])
        if au:
            ok = re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", au)
            fields.append({"name": "Author", "inline": True,
                           "value": f"[u/{au}](https://www.reddit.com/user/{au})" if ok else esc("u/" + au)})
        if when:
            fields.append({"name": "Posted", "inline": True, "value": f"<t:{int(when.timestamp())}:R>"})
        kind = "🖼️ Image" if image else "📝 Text" if body else "🔗 Link" if src else "📌 Post"
        fields.append({"name": "Type", "inline": True, "value": kind})
    else:
        if d["author"]:
            fields.append({"name": "Author", "inline": True, "value": esc(d["author"])})
        if when:
            fields.append({"name": "Published", "inline": True, "value": f"<t:{int(when.timestamp())}:R>"})
        tags = [esc(c) for c in dict.fromkeys(d["cats"]) if c][:4]
        if tags:
            fields.append({"name": "Tags", "inline": True, "value": ", ".join(tags)})

    embed = {
        "title": clip(d["title"] or feed_name, 256),
        "url": link,
        "description": desc[:4000],
        "color": color,
        "footer": {"text": FOOTER},
        "timestamp": iso(when or dt.datetime.now(dt.timezone.utc)),
    }
    if fields:
        embed["fields"] = fields[:25]
    if image:
        embed["image"] = {"url": image}
    return embed


# ------------------------------------------------------------------- discord
def send(hook, embed):
    """Returns True if Discord accepted the message."""
    body = json.dumps({"embeds": [embed], "allowed_mentions": {"parse": []}}).encode()
    for attempt in range(4):
        req = urllib.request.Request(hook, data=body, method="POST", headers={
            "Content-Type": "application/json", "User-Agent": UA})
        try:
            urllib.request.urlopen(req, timeout=TIMEOUT).read()
            return True
        except urllib.error.HTTPError as ex:
            if ex.code == 429:
                try:
                    wait = float(json.loads(ex.read()).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                time.sleep(min(wait, 30) + 0.5)
                continue
            log("  Discord rejected the message: HTTP", ex.code)
            return False
        except Exception as ex:
            log("  Discord request failed:", type(ex).__name__, ex)
            time.sleep(2)
    return False


def load_webhooks():
    hooks = {}
    try:
        secrets = json.loads(os.environ.get("ALL_SECRETS") or "{}")
    except ValueError:
        secrets = {}
    for key, val in secrets.items():
        # also accepts the typo DISCORD_WEBHOOM_3
        m = re.fullmatch(r"DISCORD_WEBHOO[KM]_(\d+)", key.upper())
        if not m or not val:
            continue
        val = val.strip()
        if HOOK_RE.match(val):
            hooks[int(m.group(1))] = val
        else:
            log(f"Secret {key} is not a valid Discord webhook URL - ignored")
    return hooks


# ---------------------------------------------------------------------- main
def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return [x for x in data if isinstance(x, str)]
    except (OSError, ValueError):
        return []


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(seen[-SEEN_LIMIT:], f)


def main():
    hooks = load_webhooks()
    log("Webhooks found:", sorted(hooks))
    if not hooks:
        log("ERROR: no valid DISCORD_WEBHOOK_<number> secrets found")
        return 1

    first_run = not os.path.exists(SEEN_FILE)
    seen = load_seen()
    seen_set = set(seen)

    def mark(key):
        if key not in seen_set:
            seen.append(key)
            seen_set.add(key)

    try:
        for name, (url, num) in FEEDS.items():
            hook = hooks.get(num)
            if not hook:
                log(f"Skipped {name}: no secret DISCORD_WEBHOOK_{num}")
                continue
            feed_url = fix_feed_url(url)
            log(f"Checking {name}")
            try:
                if not is_public_url(feed_url):
                    raise ValueError("feed URL is not public")
                items = parse_feed(http_get(feed_url, FEED_MAX_BYTES,
                                            "application/rss+xml,application/atom+xml,application/xml,*/*;q=0.5"),
                                   feed_url)
            except Exception as ex:
                log(f"  FAILED to read {name}: {type(ex).__name__}: {ex}")
                continue

            new = []
            for it in items:  # oldest first
                key = norm_url(it["link"])
                if key in seen_set or it["link"] in seen_set:
                    continue
                new.append((key, it))

            if first_run:
                for key, _ in new:
                    mark(key)
                log(f"  first run: marked {len(new)} posts as seen")
                continue

            # too many new posts: skip the oldest, send only the newest ones
            for key, _ in new[:-MAX_PER_FEED] if len(new) > MAX_PER_FEED else []:
                mark(key)
            new = new[-MAX_PER_FEED:]

            sent = 0
            for key, it in new:
                try:
                    embed = build_embed(it, name, feed_url)
                except Exception as ex:
                    log(f"  could not build embed ({type(ex).__name__}: {ex}) - skipped")
                    mark(key)
                    continue
                if send(hook, embed):
                    mark(key)
                    sent += 1
                    time.sleep(1)
                else:
                    log("  stopping this feed, will retry next run")
                    break
            log(f"  sent {sent} new post(s)")
    finally:
        save_seen(seen)
    return 0


if __name__ == "__main__":
    sys.exit(main())
