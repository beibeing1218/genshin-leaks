#!/usr/bin/env python3
"""原神內鬼情報站：自動抓巴哈姆特原神板「內鬼資訊」、Reddit r/Genshin_Impact_Leaks、
HoYoLAB 官方資訊與外媒爆料報導，英文用免費的 Google 翻譯轉成中文，產生一頁看板。
全程不呼叫任何 AI 模型，不消耗 token。只用 Python 標準庫，不需要 pip install。

用法：
  python leaks.py build   更新資料並產生網頁（排程用）
  python leaks.py open    開啟看板（必要時在背景啟動本機伺服器，網頁上的「立即更新」才能用）
  python leaks.py serve   只啟動本機伺服器
"""
import gzip
import html
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
DATA_DIR = BASE / "data"
TEMPLATE = BASE / "template.html"
OUT_HTML = BASE / "genshin-leaks.html"      # 可直接雙擊開啟的靜態版
# 資料檔同時是「歷史存檔」：來源一次只給最近幾十則，靠每次合併舊資料才能保留 DAYS 天
# 雲端（GitHub Actions）的存檔與快取會存回 repo；本機用另一組不進版控的檔，兩邊才不會互相衝突
CLOUD = bool(os.environ.get("GITHUB_ACTIONS"))
LEAKS_JSON = DATA_DIR / ("leaks.json" if CLOUD else "leaks.local.json")
CACHE = DATA_DIR / ("translations.json" if CLOUD else "translations.local.json")
LOG = DATA_DIR / "log.txt"

PORT = 8766
DAYS = 7            # 保留幾天內的消息
STALE_HOURS = 1     # 開啟看板時，資料超過幾小時就自動更新
TW = timezone(timedelta(hours=8))
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36"
# Reddit 會擋冒充瀏覽器的程式，要用「平台:名稱:版本」格式的自我介紹才放行
REDDIT_UA = "windows:genshin-leak-board:v1.0 (personal RSS reader)"
ATOM = "{http://www.w3.org/2005/Atom}"
MEDIA = "{http://search.yahoo.com/mrss/}"

BAHA_BOARD = "https://forum.gamer.com.tw/B.php?bsn=36730&subbsn=10"   # 原神板「內鬼資訊」子板
BAHA_THREADS = 4    # 最多追蹤幾個活躍的內鬼消息串
BAHA_PAGES = 2      # 每串讀最後幾頁（一頁 20 樓）
REDDIT_FEED = "https://www.reddit.com/r/Genshin_Impact_Leaks/new/.rss?limit=100"
HOYOLAB_NEWS = "https://bbs-api-os.hoyolab.com/community/post/wapi/getNewsList?gids=2&page_size=20&type=3"
MEDIA_FEED = ("https://news.google.com/rss/search?q="
              + urllib.parse.quote(f'"Genshin Impact" (leak OR leaks OR leaked) when:{DAYS}d')
              + "&hl=en-US&gl=US&ceid=US:en")

# (代號, 顯示名稱)
SOURCES = [("baha", "巴哈姆特內鬼串"), ("reddit", "Reddit 內鬼板"), ("hoyolab", "HoYoLAB 官方"), ("media", "外媒報導")]

# 看得到但程式抓不到的一手來源，只在網頁上列出連結
MANUAL_SOURCES = [
    ("X（Twitter）爆料帳號", "大部分內鬼消息最早出現在這裡；右側「爆料者排行」會列出最近被引用最多的帳號", "https://x.com/search?q=genshin%20leak&f=live"),
    ("NGA 原神專區", "中國大陸玩家論壇，簡體中文，常有第一手搬運；擋程式抓取", "https://bbs.nga.cn/thread.php?fid=650"),
    ("Honey Hunter World", "測試服解包資料庫：角色數值、技能、武器、聖遺物", "https://gensh.honeyhunterworld.com/?lang=CHT"),
    ("Project Amber（Yatta）", "解包資料庫，可切換繁中，版本上線前後資料最齊", "https://gi.yatta.moe/cht/archive/avatar"),
]

# Google 翻譯常把遊戲名詞直譯或留英文，這裡改回台服常用譯名
ZH_FIX = {"原神影響": "原神", "Genshin 影響": "原神", "Genshin Impact": "原神",
          "洩漏": "爆料", "洩露": "爆料", "洩密": "爆料", "外洩": "爆料",
          "橫幅": "卡池", "旗幟": "卡池", "重播": "復刻", "Stygian Onslaught": "幽境危戰"}
ZH_NAMES = {"Furina": "芙寧娜", "Mavuika": "瑪薇卡", "Nahida": "納西妲", "Neuvillette": "那維萊特",
            "Venti": "溫迪", "Zhongli": "鍾離", "Skirk": "絲柯克", "Escoffier": "愛可菲",
            "Columbina": "哥倫比婭", "Sandrone": "桑多涅", "Mitya": "米提亞", "Danica": "達妮卡",
            "Anastasya": "安娜斯塔夏", "Linnea": "莉奈婭", "Zibai": "茲白", "Vesna": "薇斯納",
            "Paimon": "派蒙", "Childe": "達達利亞", "Yelan": "夜蘭", "Xianyun": "閑雲",
            "Tighnari": "提納里", "Cyno": "賽諾", "Ningguang": "凝光", "Snezhnaya": "至冬", "Nod-Krai": "挪德卡萊"}
ZH_NAMES_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(map(re.escape, ZH_NAMES)) + r")(?![A-Za-z])")

VERSION_RE = re.compile(r"(?<![\d.])(?:GI)?([5-9]\.\d)(?![\d.%x×])", re.I)
VIA_RE = re.compile(r"\s*[\(\[]?\b(?:via|from|by|credit(?:s)?(?: to)?|source:?)\s+([^()\[\]]+?)[\)\]]?\s*$", re.I)
TWITTER_RE = re.compile(r"https?://(?:www\.|mobile\.)?(?:twitter|x)\.com/(\w{2,15})/status/", re.I)

build_lock = threading.Lock()


def log(msg):
    DATA_DIR.mkdir(exist_ok=True)
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}"
    print(line)
    lines = LOG.read_text(encoding="utf-8").splitlines() if LOG.exists() else []
    lines = (lines + [line])[-300:]
    LOG.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_atomic(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def http_get(url, timeout=25, ua=BROWSER_UA, headers=None):
    h = {"User-Agent": ua}
    h.update(headers or {})
    last = None
    for attempt in range(3):          # 偶爾會被限流或拿到壞掉的回應，稍等重試
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                body = r.read()
            if body[:2] == b"\x1f\x8b":
                body = gzip.decompress(body)
            return body
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
        time.sleep(4 * (attempt + 1))
    raise last


# ---------- 文字處理 ----------

def unesc(s):
    return html.unescape(html.unescape(s or ""))


def html_text(s, limit=None):
    """HTML 轉純文字，保留換行；網址另外列成連結，這裡拿掉避免版面太亂。"""
    s = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", s or "")
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(div|p|li|blockquote|h\d|tr)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = unesc(s).replace("\xa0", " ")
    s = re.sub(r"https?://\S+", "", s)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in s.split("\n")]
    s = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    if limit and len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return s


def versions(text):
    return sorted({m.group(1) for m in VERSION_RE.finditer(text or "")}, reverse=True)


def split_names(s):
    parts = re.split(r"\s*(?:,|&|\+|/|、|\band\b)\s*", s.strip(), flags=re.I)
    out = []
    for p in parts:
        p = p.strip(" .:-@")
        if p and len(p) <= 30 and len(p.split()) <= 3:
            out.append(p)
    return out


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def link_label(url):
    host = urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.").removeprefix("m.")
    return {"x.com": "X", "twitter.com": "X", "youtube.com": "YouTube", "youtu.be": "YouTube",
            "reddit.com": "Reddit", "bilibili.com": "Bilibili", "t.me": "Telegram"}.get(host, host)


# ---------- 巴哈姆特：原神板「內鬼資訊」 ----------

def baha_list_time(s, now):
    """列表上的「31 分前」「4 小時前」「昨天 10:22」「09-22 15:22」「2025-12-02」轉成時間。"""
    s = s.strip()
    local = now.astimezone(TW)
    if m := re.match(r"(\d+)\s*分", s):
        return now - timedelta(minutes=int(m.group(1)))
    if m := re.match(r"(\d+)\s*小時", s):
        return now - timedelta(hours=int(m.group(1)))
    if m := re.match(r"(昨天|前天)\s*(\d+):(\d+)", s):
        d = local - timedelta(days=1 if m.group(1) == "昨天" else 2)
        return d.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
    if m := re.match(r"(\d{4})-(\d+)-(\d+)", s):
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=TW)
    if m := re.match(r"(\d+)-(\d+)\s+(\d+):(\d+)", s):
        d = local.replace(month=int(m.group(1)), day=int(m.group(2)), hour=int(m.group(3)),
                          minute=int(m.group(4)), second=0, microsecond=0)
        return d if d <= local + timedelta(days=1) else d.replace(year=d.year - 1)
    return None


def baha_threads(now):
    page = http_get(BAHA_BOARD).decode("utf-8", "replace")
    out = []
    for row in page.split('class="b-list__row b-list-item')[1:]:
        title = re.search(r'class="b-list__main__title[^>]*>([^<]+)', row)
        href = re.search(r'href="(C\.php\?bsn=36730&snA=(\d+)&tnum=(\d+)[^"]*)"', row)
        if not title or not href:
            continue
        last = re.search(r'title="觀看最新回覆文章"[^>]*>([^<]+)', row)
        thumb = re.search(r'data-thumbnail="([^"]+)"', row)
        brief = re.search(r'class="b-list__brief">([^<]*)', row)
        gp = re.search(r'b-list__summary__gp[^>]*>(\d+)', row)
        pop = re.search(r'title="人氣：([\d,]+)"', row)
        reply = re.search(r'title="互動：([\d,]+)"', row)
        when = baha_list_time(last.group(1), now) if last else None
        out.append({
            "title": unesc(title.group(1)).strip(),
            "link": "https://forum.gamer.com.tw/" + unesc(href.group(1)).split("&subbsn")[0],
            "snA": href.group(2), "tnum": int(href.group(3)),
            "brief": unesc(brief.group(1)).strip() if brief else "",
            "thumb": unesc(thumb.group(1)) if thumb else "",
            "gp": int(gp.group(1)) if gp else 0,
            "views": int(pop.group(1).replace(",", "")) if pop else 0,
            "replies": int(reply.group(1).replace(",", "")) if reply else 0,
            "last": when.isoformat() if when else None,
        })
    return out


def baha_real_link(url):
    url = unesc(url)
    if "ref.gamer.com.tw/redir.php" in url:
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("url")
        if q:
            url = q[0]
    return url


def baha_floors(thread, cutoff):
    pages = max(1, math.ceil(thread["tnum"] / 20))
    out = []
    for p in range(max(1, pages - BAHA_PAGES + 1), pages + 1):
        url = f"https://forum.gamer.com.tw/C.php?bsn=36730&snA={thread['snA']}&page={p}"
        page = http_get(url).decode("utf-8", "replace")
        for sec in page.split('<section class="c-section"  id="post_')[1:]:
            sn = sec[:sec.find('"')]
            floor = re.search(r'data-floor="(\d+)"', sec)
            name = re.search(r'class="username"[^>]*>([^<]*)<', sec)
            uid = re.search(r'class="userid"[^>]*>([^<]*)<', sec)
            mtime = re.search(r'data-mtime="([^"]+)"', sec)
            gp = re.search(r'class="postgp">推<span>(\d+)', sec)
            body = re.search(r'class="c-article__content">(.*?)</article>', sec, re.S)
            if not (floor and mtime and body):
                continue
            date = datetime.strptime(mtime.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TW)
            if date < cutoff:
                continue
            # 引用別樓的內容會重複，整段拿掉
            raw = re.sub(r'(?s)<blockquote class="c-article-quote">.*?</blockquote>', "", body.group(1))
            raw = re.sub(r">在新視窗開啟(?:圖片|影片)<", "><", raw)
            imgs = []
            for u in re.findall(r'(?:class="photoswipe-image" href|data-src)="(https://truth\.bahamut\.com\.tw/[^"?]+)', raw):
                if u not in imgs:
                    imgs.append(u)
            links = []
            for u in re.findall(r'href="(https?://[^"]+)"', raw):
                u = baha_real_link(u)
                if "bahamut.com.tw" in u or "gamer.com.tw" in u or u in links:
                    continue
                links.append(u)
            text = html_text(raw, 1500)
            if not text and not imgs and not links:
                continue
            short = re.sub(r"^(RE:)?【[^】]*】", "", thread["title"]).split("<")[0].strip()
            out.append({
                "id": f"baha-{sn}", "source": "baha", "lang": "zh",
                "title": "", "text": text, "link": f"https://forum.gamer.com.tw/Co.php?bsn=36730&sn={sn}",
                "date": date.isoformat(), "author": (unesc(name.group(1)) if name else "") or (uid.group(1) if uid else ""),
                "thread": short, "thread_link": thread["link"], "floor": int(floor.group(1)),
                "gp": int(gp.group(1)) if gp else 0,
                "images": [{"thumb": u + "?w=600", "full": u} for u in imgs[:8]],
                "links": [{"url": u, "label": link_label(u)} for u in links[:6]],
                "leakers": sorted({m.group(1) for m in TWITTER_RE.finditer(" ".join(links))}),
                "versions": versions(text) or versions(thread["title"]),
            })
    return out


def fetch_baha(now, cutoff):
    threads = baha_threads(now)
    # 只追蹤最近有人回的「情報」消息串；閒聊串雜訊太多先略過
    active = [t for t in threads if t["title"].startswith("【情報】") and t["last"]
              and datetime.fromisoformat(t["last"]) >= cutoff][:BAHA_THREADS]
    items = []
    for t in active:
        items.extend(baha_floors(t, cutoff))
    return items, threads[:12]


# ---------- Reddit r/Genshin_Impact_Leaks ----------

def fetch_reddit(now, cutoff):
    root = ET.fromstring(http_get(REDDIT_FEED, ua=REDDIT_UA))
    out = []
    for e in root.findall(f"{ATOM}entry"):
        title = unesc(e.findtext(f"{ATOM}title", "")).strip()
        link_el = e.find(f"{ATOM}link")
        link = link_el.get("href") if link_el is not None else ""
        date = parse_date(e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated"))
        if not title or not link or not date or date < cutoff:
            continue
        content = e.findtext(f"{ATOM}content", "")
        selftext = re.search(r'<div class="md">(.*?)</div>', content, re.S)
        text = html_text(selftext.group(1), 1200) if selftext else ""
        target = re.search(r'<a href="([^"]+)">\[link\]</a>', content)
        target = unesc(target.group(1)) if target else ""
        thumb_el = e.find(f"{MEDIA}thumbnail")
        thumb = unesc(thumb_el.get("url")) if thumb_el is not None else ""
        images, links = [], []
        if re.search(r"https://i\.redd\.it/\S+\.(?:jpe?g|png|gif|webp)$", target, re.I):
            images.append({"thumb": thumb or target, "full": target})
        elif thumb:
            images.append({"thumb": thumb, "full": thumb})
        if target and target != link and "i.redd.it" not in target:
            label = {"v.redd.it": "影片", "www.reddit.com": "圖集"}.get(urllib.parse.urlsplit(target).netloc, link_label(target))
            links.append({"url": target if "/gallery/" not in target else link, "label": label})
        # 內文裡的連結（X、YouTube…）
        if selftext:
            for u in re.findall(r'href="(https?://[^"]+)"', selftext.group(1)):
                u = unesc(u)
                if all(x["url"] != u for x in links) and len(links) < 6:
                    links.append({"url": u, "label": link_label(u)})
        # 「… via LoveHappy」：via 後面就是爆料者
        leakers, head = [], title
        if m := VIA_RE.search(title):
            leakers = split_names(m.group(1))
            if leakers:
                head = title[:m.start()].strip(" -–|:")
        leakers += [m.group(1) for m in TWITTER_RE.finditer(" ".join(x["url"] for x in links))]
        author = e.findtext(f"{ATOM}author/{ATOM}name", "").removeprefix("/u/")
        out.append({
            "id": "reddit-" + link.rstrip("/").split("/comments/")[-1].split("/")[0],
            "source": "reddit", "lang": "en", "title": head, "text": text, "link": link,
            "date": date.isoformat(), "author": author, "images": images, "links": links,
            "leakers": list(dict.fromkeys(leakers)), "versions": versions(title + " " + text),
        })
    return out


# ---------- HoYoLAB 官方資訊（繁中） ----------

def fetch_hoyolab(now, cutoff):
    data = json.loads(http_get(HOYOLAB_NEWS, headers={"x-rpc-language": "zh-tw"}))
    out = []
    for x in data["data"]["list"]:
        p = x["post"]
        date = datetime.fromtimestamp(int(p["created_at"]), timezone.utc)
        if date < cutoff - timedelta(days=DAYS):       # 官方消息少，多留一週
            continue
        img = (x.get("image_list") or [{}])[0].get("url") or p.get("cover") or ""
        text = html_text(p.get("content", ""), 400)
        out.append({
            "id": f"hoyolab-{p['post_id']}", "source": "hoyolab", "lang": "zh",
            "title": p["subject"], "text": text, "link": f"https://www.hoyolab.com/article/{p['post_id']}",
            "date": date.isoformat(), "author": "原神官方", "images": [{"thumb": img, "full": img}] if img else [],
            "links": [], "leakers": [], "versions": versions(p["subject"] + " " + text),
        })
    return out


# ---------- 外媒報導（Google News） ----------

def fetch_media(now, cutoff):
    root = ET.fromstring(http_get(MEDIA_FEED))
    out = []
    for it in root.findall(".//item"):
        title = unesc(it.findtext("title", "")).strip()
        src = unesc(it.findtext("source", "")).strip()
        link = it.findtext("link", "")
        date = parse_date(it.findtext("pubDate"))
        if src and title.endswith(f" - {src}"):
            title = title[: -len(src) - 3]
        if not date or date < cutoff or not re.search(r"genshin", title, re.I) or not re.search(r"leak", title, re.I):
            continue
        out.append({
            "id": "media-" + re.sub(r"\W+", "", title.lower())[:80], "source": "media", "lang": "en",
            "title": title, "text": "", "link": link, "date": date.isoformat(), "author": src,
            "images": [], "links": [], "leakers": [], "versions": versions(title),
        })
    return out


FETCHERS = {"baha": fetch_baha, "reddit": fetch_reddit, "hoyolab": fetch_hoyolab, "media": fetch_media}


# ---------- 免費翻譯（Google 翻譯網頁端點，不需金鑰、不耗 token） ----------

def translate(s):
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=en&tl=zh-TW&dt=t&q="
           + urllib.parse.quote(s))
    data = json.loads(http_get(url, timeout=20))
    return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()


def fix_zh(s):
    for a, b in ZH_FIX.items():
        s = s.replace(a, b)
    s = re.sub(r"(?<![A-Za-z0-9])C(\d)(?![A-Za-z0-9])", r"\1 命", s)
    return ZH_NAMES_RE.sub(lambda m: ZH_NAMES[m.group(1)], s)


def translate_all(strings):
    cache = json.loads(CACHE.read_text(encoding="utf-8")) if CACHE.exists() else {}
    todo = [s for s in strings if s not in cache]
    failures = 0

    def work(s):
        nonlocal failures
        if failures >= 5:          # 連續被擋就先停，下次更新再補翻
            return
        try:
            cache[s] = translate(s)
        except Exception:
            failures += 1

    if todo:
        with ThreadPoolExecutor(4) as ex:
            list(ex.map(work, todo))
    kept = {s: cache[s] for s in strings if s in cache}   # 只保留目前用得到的
    write_atomic(CACHE, json.dumps(kept, ensure_ascii=False))
    return kept, len(todo), failures


# ---------- 產生資料與網頁 ----------

def leaker_ranking(items):
    """統計最近被引用最多的爆料者（Reddit 標題的 via、內文與巴哈樓層裡的 X 帳號）。"""
    count, shown, x_handle = Counter(), {}, set()
    for it in items:
        for name in it["leakers"]:
            k = name.lower()
            count[k] += 1
            shown.setdefault(k, name)
            if any(re.search(rf"(?:x|twitter)\.com/{re.escape(name)}/", l["url"], re.I) for l in it["links"]):
                x_handle.add(k)
    out = []
    for k, n in count.most_common(15):
        name = shown[k]
        url = (f"https://x.com/{name}" if k in x_handle else
               "https://www.reddit.com/r/Genshin_Impact_Leaks/search/?restrict_sr=1&sort=new&q="
               + urllib.parse.quote(f'"via {name}"'))
        out.append({"name": name, "count": n, "url": url})
    return out


def build():
    with build_lock:
        t0 = time.time()
        DATA_DIR.mkdir(exist_ok=True)
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=DAYS)
        old = json.loads(LEAKS_JSON.read_text(encoding="utf-8")) if LEAKS_JSON.exists() else {}
        old_items = {it["id"]: it for it in old.get("items", [])}
        threads = old.get("threads", [])
        items, status = {}, []

        def run(src):
            try:
                return src, FETCHERS[src](now, cutoff), None
            except Exception as e:
                return src, None, f"{type(e).__name__}: {e}"

        with ThreadPoolExecutor(4) as ex:
            for src, got, err in ex.map(run, [s for s, _ in SOURCES]):
                if src == "baha" and got:
                    got, threads = got
                name = dict(SOURCES)[src]
                status.append({"id": src, "name": name, "count": len(got or []), "error": err})
                if err:
                    log(f"[來源失敗] {name}: {err}")
                for it in got or []:
                    items[it["id"]] = it

        # 合併舊資料：來源一次只給最近的幾十則，這樣才能累積滿 DAYS 天；某來源失敗時也不會整批消失
        keep_after = {"hoyolab": cutoff - timedelta(days=DAYS)}
        for k, it in old_items.items():
            if k not in items and datetime.fromisoformat(it["date"]) >= keep_after.get(it["source"], cutoff):
                items[k] = it
        items = sorted(items.values(), key=lambda x: x["date"], reverse=True)

        strings = sorted({s for it in items if it["lang"] == "en" for s in (it["title"], it["text"]) if s})
        tr, n_new, n_fail = translate_all(strings)
        for it in items:
            if it["lang"] == "en":
                it["title_zh"] = fix_zh(tr.get(it["title"], "")) if it["title"] else ""
                it["text_zh"] = fix_zh(tr.get(it["text"], "")) if it["text"] else ""

        data = {
            "generated": now.isoformat(),
            "days": DAYS,
            "sources": status,
            "manual": [{"name": n, "desc": d, "url": u} for n, d, u in MANUAL_SOURCES],
            "threads": threads,
            "leakers": leaker_ranking(items),
            "items": items,
        }
        write_atomic(LEAKS_JSON, json.dumps(data, ensure_ascii=False))
        write_atomic(OUT_HTML, render(data))
        log(f"更新完成：{len(items)} 則消息，新翻譯 {n_new - n_fail} 條"
            f"{f'（{n_fail} 條失敗）' if n_fail else ''}，耗時 {time.time() - t0:.0f} 秒")
        return data


def render(data):
    tpl = TEMPLATE.read_text(encoding="utf-8")
    js = json.dumps(data, ensure_ascii=False).replace("</", "<\\/") if data else "null"
    # 雲端版（GitHub Actions）會設定 LEAKS_RUN_URL，讓「立即更新」連到手動執行頁
    cfg = json.dumps({"staleHours": STALE_HOURS, "sources": dict(SOURCES),
                      "runUrl": os.environ.get("LEAKS_RUN_URL", "")}, ensure_ascii=False)
    return tpl.replace("/*__DATA__*/null", js).replace("/*__CFG__*/{}", cfg)


# ---------- 本機伺服器（只接受本機連線，讓「立即更新」按鈕能運作） ----------

ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            return self._send(403, b"forbidden", "text/plain")
        path = self.path.split("?")[0]
        if path == "/ping":
            return self._send(200, b"genshin-leaks", "text/plain")
        if path in ("/", "/index.html"):
            data = json.loads(LEAKS_JSON.read_text(encoding="utf-8")) if LEAKS_JSON.exists() else None
            return self._send(200, render(data).encode("utf-8"), "text/html; charset=utf-8")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        # 自訂標頭會觸發瀏覽器 CORS 預檢，其他網站因此無法代你按「更新」
        if (self.path != "/refresh" or self.headers.get("Host") not in ALLOWED_HOSTS
                or self.headers.get("X-Leaks") != "1"):
            return self._send(403, b"forbidden", "text/plain")
        try:
            build()
            self._send(200, b'{"ok":true}', "application/json")
        except Exception as e:
            log(f"[更新失敗] {e}")
            self._send(500, json.dumps({"ok": False, "error": str(e)}).encode(), "application/json")

    def log_message(self, *args):
        pass


def server_running():
    try:   # 不走 http_get：伺服器沒開時要立刻回答，不要重試等待
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/ping", timeout=2) as r:
            return r.read() == b"genshin-leaks"
    except Exception:
        return False


def serve():
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


def open_board():
    if not server_running():
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = str(pyw if pyw.exists() else sys.executable)
        flags = 0x00000008 | 0x00000200 | 0x08000000   # 背景執行、不開黑色視窗
        subprocess.Popen([exe, str(Path(__file__).resolve()), "serve"], creationflags=flags, close_fds=True)
        for _ in range(40):
            if server_running():
                break
            time.sleep(0.25)
    webbrowser.open(f"http://127.0.0.1:{PORT}/")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "open"
    {"build": build, "serve": serve, "open": open_board}.get(cmd, open_board)()
