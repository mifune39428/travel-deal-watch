#!/usr/bin/env python3
"""国内旅行（宿が主役）のセール告知を集めて docs/deals.json に書き出す。

秋田発・毎月1回旅行する人が「今どのセールが走っていて、次はいつ来るか」を
1画面で分かるようにするのが目的。値段そのものは追わない（セール告知だけ）。

LLMもAPIキーも使わない。やっているのは
  ① 旅行セール系メディアのRSS＋Googleニュース検索RSSから告知記事を拾う
  ② 素のHTTPで開催中セール名が読める社（楽天トラベル・一休・Yahoo!トラベル）を直に見る
  ③ 毎月かならず来る定番を sources.json のカレンダーから今月の日付に直す
の3つだけ。
"""

from __future__ import annotations

import datetime as dt
import html as htmllib
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(BASE_DIR, "sources.json")
OUTPUT_PATH = os.path.join(BASE_DIR, "docs", "deals.json")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
FETCH_TIMEOUT = 30
FETCH_INTERVAL = 0.8          # 相手先を連打しないための待ち
KEEP_DAYS = 120               # 告知を残す期間
KEEP_MAX = 400

JST = dt.timezone(dt.timedelta(hours=9))

# ---------------------------------------------------------------- 取得

def fetch(url: str) -> str | None:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,en;q=0.8",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT, context=ssl.create_default_context()) as res:
            raw = res.read()
            enc = res.headers.get_content_charset() or "utf-8"
            return raw.decode(enc, "replace")
    except Exception as exc:                      # noqa: BLE001 — 落ちた先は飛ばして続ける
        print(f"  取得できず: {url} ({type(exc).__name__}: {str(exc)[:60]})", file=sys.stderr)
        return None


def strip_tags(fragment: str) -> str:
    text = re.sub(r"(?s)<[^>]+>", " ", fragment)
    return re.sub(r"\s+", " ", htmllib.unescape(text)).strip()


# ---------------------------------------------------------------- 判定

# 「セールの告知である」と見なす語。ひとつも無い記事は落とす。
DEAL_WORDS = re.compile(
    r"セール|クーポン|割引|キャンペーン|OFF|ＯＦＦ|オフ|半額|お得|おトク|特価|"
    r"ポイント\d+倍|還元|무|割|値下げ|安く|格安|タイムセール|先得|トクだ値|フェア"
)

# セールに見えて中身が違うもの。増便・就航は運賃の話ではない。
NOISE_WORDS = re.compile(
    r"増便|減便|運休|就航|欠航|遅延|事故|墜落|決算|株主|人事|採用|訴訟|逮捕|"
    r"リニューアルオープン|グランドオープン|新メニュー|ランキング|おすすめ\d+選|"
    r"絶景|グルメ|食べ放題|ラーメン|スイーツ|"
    r"発売終了|販売終了|終了を発表|値上げ|改悪|廃止"
)

# 宿1軒だけの紹介記事。セールの告知ではなく宿の広告なので落とす。
# （「【楽天トラベルセール】◯◯県「宿名」が今だけ特別価格に！」の形が毎日十数本流れてくる）
SINGLE_HOTEL = re.compile(
    r"が今だけ特別価格|が特別価格で登場|大幅ポイント還元中|でお得に予約|"
    r"実質\d+％?オフ！?\s*「|オフでお得に予約|が今だけ\d+[％%]"
)

# 「宿名」を鉤括弧でくくって割引と一緒に出す形も、宿1軒の広告。
# 例：楽天トラベルで500円オフ！「白浜温泉 紀州半島」で全室源泉掛け流しの……
SINGLE_HOTEL_QUOTED = re.compile(r"「[^」]{0,30}(?:温泉|旅館|ホテル|リゾート|ヴィラ|の宿)[^」]{0,20}」")

# 旅行以外の楽天サービス。「クーポン」だけで引っかかってくる。
NOT_TRAVEL = re.compile(r"楽天モバイル|楽天カード|楽天市場|楽天銀行|楽天証券|楽天ペイ|ふるさと納税の返礼品ランキング")

# 海外路線だけの話。国内旅行が主役なので落とす（「国内」と併記されていれば残す）。
OVERSEAS = re.compile(
    r"日韓線|韓国線|中国線|台湾線|チェジュ|ジンエアー|ティーウェイ|エアプサン|エアソウル|"
    r"大韓航空|アシアナ|エバー航空|スターラックス|キャセイ|ベトジェット|エティハド|エミレーツ|"
    r"ハワイ|グアム|サイパン|台北|ソウル|釜山|バンコク|シンガポール|パリ|ロンドン|ニューヨーク"
)

# 読めない見出し（丸数字・二重感嘆符・貼り付け事故の英数字ID）はまとめ記事の切れ端。
JUNK = re.compile(r"[①-⑳]|‼|\([A-Za-z0-9]{8,}")

# 種別。上から順に当てる（航空券とツアーが両方あるときは航空券を優先しない設計）。
KINDS = [
    ("宿", re.compile(r"宿|ホテル|旅館|温泉|宿泊|楽天トラベル|じゃらん|一休|Yahoo!?トラベル|"
                      r"るるぶ|JTB|日本旅行|ふるさと納税|リゾート|旅館")),
    ("ツアー", re.compile(r"ツアー|パック|ダイナミックパッケージ|楽パック|旅行商品")),
    ("航空券", re.compile(r"航空券|国内線|国際線|ANA|JAL|ジェットスター|ピーチ|Peach|スカイマーク|"
                        r"AIRDO|ソラシド|スターフライヤー|フジドリーム|FDA|運賃|タイムセール.*便")),
    ("鉄道・バス", re.compile(r"新幹線|JR|えきねっと|トクだ値|きっぷ|青春18|高速バス|夜行バス|"
                          r"バス|鉄道|フリーパス")),
]

# 割引の大きさ。見出しからそのまま拾える形だけ。
DISCOUNT_RE = re.compile(r"(最大)?\s*(\d{1,2}(?:\.\d)?)\s*(?:%|％)\s*(?:OFF|ＯＦＦ|オフ|引き|割引)?")
# 「1万2000円割引」を「2000円」と読み違えないよう、万の桁ごと拾う。
YEN_RE = re.compile(r"(最大)?\s*(\d{1,3}万\d{0,4}|[0-9,]{3,7})\s*円(?:引き|割引|分|クーポン|オフ|OFF)")
HALF_RE = re.compile(r"半額|最大50")

# 期間。「8月20日から」「9月30日まで」を拾う。年は書かれないことがほとんど。
FROM_RE = re.compile(r"(\d{1,2})月(\d{1,2})日(?:から|より|開始)")
TILL_RE = re.compile(r"(\d{1,2})月(\d{1,2})日(?:まで|迄)")


def classify(title: str) -> str:
    for name, pattern in KINDS:
        if pattern.search(title):
            return name
    return "その他"


def find_service(title: str) -> str:
    table = [
        ("楽天トラベル", r"楽天トラベル|楽パック"),
        ("じゃらん", r"じゃらん"),
        ("一休.com", r"一休"),
        ("Yahoo!トラベル", r"Yahoo!?トラベル"),
        ("JTB", r"JTB"),
        ("日本旅行", r"日本旅行|赤い風船"),
        ("るるぶトラベル", r"るるぶ"),
        ("ANA", r"\bANA\b|全日空|全日本空輸"),
        ("JAL", r"\bJAL\b|日本航空"),
        ("JR東日本", r"JR東日本|えきねっと|トクだ値"),
        ("ふるさと納税", r"ふるさと納税"),
    ]
    for name, pattern in table:
        if re.search(pattern, title):
            return name
    return ""


def extract_size(title: str) -> str:
    """割引の大きさを見出しから1つだけ拾う。無ければ空。"""
    best = ""
    m = DISCOUNT_RE.search(title)
    if m:
        best = f"{'最大' if m.group(1) else ''}{m.group(2)}%OFF"
    m = YEN_RE.search(title)
    if m and not best:
        best = f"{'最大' if m.group(1) else ''}{m.group(2)}円"
    if not best and HALF_RE.search(title):
        best = "半額級"
    return best


def resolve_date(month: int, day: int, today: dt.date) -> str | None:
    """月日だけの表記に年を当てる。半年以上前の月なら翌年とみなす。"""
    for year in (today.year, today.year + 1, today.year - 1):
        try:
            cand = dt.date(year, month, day)
        except ValueError:
            continue
        if -200 <= (cand - today).days <= 300:
            return cand.isoformat()
    return None


def extract_period(title: str, today: dt.date) -> tuple[str | None, str | None]:
    starts = ends = None
    m = FROM_RE.search(title)
    if m:
        starts = resolve_date(int(m.group(1)), int(m.group(2)), today)
    m = TILL_RE.search(title)
    if m:
        ends = resolve_date(int(m.group(1)), int(m.group(2)), today)
    return starts, ends


def home_match(title: str, home: dict) -> bool:
    """秋田で使える話か。県名・地方名が出ているものだけ。

    路線名（羽田・大阪など）まで拾うと「東京〜金沢が9000円」のような
    まったく関係のない見出しが秋田印になってしまうので、エリア名だけを見る。
    """
    return any(w in title for w in home.get("areas", []))


# 全国規模のセールは秋田からでも必ず使えるので、別の印を付けて上位に出す。
NATIONWIDE = re.compile(r"全国|国内線|全路線|全国\d+|日本全国|国内対象|国内ツアー|国内宿泊")


# ---------------------------------------------------------------- RSS

def parse_rss(xml: str, source: str, site: str) -> list[dict]:
    items = []
    blocks = re.findall(r"<item[\s>](.*?)</item>", xml, re.S) or re.findall(r"<entry[\s>](.*?)</entry>", xml, re.S)
    for block in blocks:
        tm = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", block, re.S)
        lm = re.search(r"<link>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</link>", block, re.S) or \
             re.search(r'<link[^>]*href="([^"]+)"', block)
        if not tm or not lm:
            continue
        title = strip_tags(htmllib.unescape(tm.group(1)))
        link = htmllib.unescape(lm.group(1)).strip()
        dm = re.search(r"<(?:pubDate|dc:date|published|updated)>(.*?)</", block, re.S)
        items.append({
            "title": title,
            "url": link,
            "source": source,
            "site": site,
            "published": parse_date(dm.group(1).strip()) if dm else None,
        })
    return items


def parse_date(text: str) -> str | None:
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return dt.datetime.strptime(text, fmt).astimezone(JST).date().isoformat()
        except ValueError:
            continue
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def collect_news(sources: dict, today: dt.date) -> tuple[list[dict], int, int]:
    raw: list[dict] = []
    ok = fail = 0

    for feed in sources["feeds"]:
        xml = fetch(feed["url"])
        time.sleep(FETCH_INTERVAL)
        if not xml:
            fail += 1
            continue
        ok += 1
        raw.extend(parse_rss(xml, feed["name"], feed.get("site", "")))

    # Googleニュースは1クエリ1キーワード。ORを入れると結果が壊れる。
    for query in sources["news_queries"]:
        url = ("https://news.google.com/rss/search?q="
               + urllib.parse.quote(query) + "&hl=ja&gl=JP&ceid=JP:ja")
        xml = fetch(url)
        time.sleep(FETCH_INTERVAL)
        if not xml:
            fail += 1
            continue
        ok += 1
        for item in parse_rss(xml, "Googleニュース", "https://news.google.com/"):
            # 「タイトル - 媒体名」の形なので媒体名を出所として持ち直す。
            if " - " in item["title"]:
                head, _, tail = item["title"].rpartition(" - ")
                if len(tail) <= 24:
                    item["title"], item["source"] = head.strip(), tail.strip()
            item["query"] = query
            raw.append(item)

    return raw, ok, fail


# ---------------------------------------------------------------- 選別と重複除去

STOP = re.compile(r"[【】\[\]（）()「」『』、。！？!?・…～〜\-—:：/／\s]+")


def norm_key(title: str) -> set[str]:
    text = STOP.sub(" ", title)
    text = re.sub(r"\d{4}年|\d{1,2}月\d{1,2}日|\d{1,2}月", " ", text)
    return {t for t in text.split() if len(t) >= 2}


def same_deal(a: dict, b: dict) -> bool:
    """同じセールを別媒体が書いたものを1つにまとめる。見出しの語の重なりで判定する。"""
    if a["service"] and b["service"] and a["service"] != b["service"]:
        return False
    ka, kb = a["_key"], b["_key"]
    if not ka or not kb:
        return False
    overlap = len(ka & kb) / min(len(ka), len(kb))
    return overlap >= 0.6


def build_deals(raw: list[dict], home: dict, today: dt.date,
                block_sources: list[str]) -> list[dict]:
    deals: list[dict] = []
    for item in raw:
        title = item["title"]
        if not title or len(title) < 8:
            continue
        if not DEAL_WORDS.search(title) or NOISE_WORDS.search(title):
            continue
        if SINGLE_HOTEL.search(title) or NOT_TRAVEL.search(title) or JUNK.search(title):
            continue
        # 宿名が鉤括弧で入っていて、かつ割引が付いていれば1軒だけの広告。
        # セール名そのものに鉤括弧が使われる場合（「じゃらんのお得な10日間」）は
        # 中身が施設名ではないので残る。
        if SINGLE_HOTEL_QUOTED.search(title) and re.search(r"オフ|OFF|円引|特別価格|還元", title):
            continue
        if OVERSEAS.search(title) and "国内" not in title:
            continue
        if item.get("source", "") in block_sources:
            continue

        published = item.get("published")
        if published:
            try:
                age = (today - dt.date.fromisoformat(published)).days
            except ValueError:
                age = 0
            if age > KEEP_DAYS or age < -3:
                continue

        starts, ends = extract_period(title, today)
        if ends and ends < today.isoformat():
            continue                                   # もう終わっている

        deal = {
            "title": title,
            "url": item["url"],
            "source": item.get("source", ""),
            "kind": classify(title),
            "service": find_service(title),
            "size": extract_size(title),
            "starts": starts,
            "ends": ends,
            "published": published,
            "home": home_match(title, home),
            "nationwide": bool(NATIONWIDE.search(title)),
            "also": [],
        }
        if deal["kind"] == "その他" and not deal["service"]:
            continue
        deal["_key"] = norm_key(title)

        for existing in deals:
            if same_deal(existing, deal):
                if deal["source"] and deal["source"] not in existing["also"] \
                        and deal["source"] != existing["source"]:
                    existing["also"].append(deal["source"])
                # 情報量の多いほうを残す（期限や割引率が入っているもの）。
                if (bool(deal["size"]) + bool(deal["ends"])) > (bool(existing["size"]) + bool(existing["ends"])):
                    existing.update({k: deal[k] for k in ("title", "url", "size", "starts", "ends", "_key")})
                break
        else:
            deals.append(deal)

    for deal in deals:
        deal.pop("_key", None)

    for deal in deals:
        deal["score"] = score_deal(deal, today)
    deals.sort(key=lambda d: (-d["score"], d["published"] or "0000-00-00"), reverse=False)
    deals.sort(key=lambda d: (-d["score"], d["published"] or "0000-00-00"))
    return deals[:KEEP_MAX]


def score_deal(deal: dict, today: dt.date) -> int:
    """上に出す順を決める点数。新しさ・使える範囲・割引の大きさ・秋田で効くかの合計。"""
    score = 0
    if deal["published"]:
        try:
            age = (today - dt.date.fromisoformat(deal["published"])).days
        except ValueError:
            age = 999
        score += 40 if age <= 7 else 25 if age <= 14 else 10 if age <= 30 else 0
    if deal["home"]:
        score += 20
    if deal["nationwide"]:
        score += 15
    if deal["size"]:
        score += 10
    score += {"宿": 12, "ツアー": 8, "航空券": 8, "鉄道・バス": 6}.get(deal["kind"], 0)
    if deal["service"]:
        score += 5
    if deal["ends"]:
        try:
            left = (dt.date.fromisoformat(deal["ends"]) - today).days
            if 0 <= left <= 21:
                score += 10          # 締切が近いものは目立たせる
        except ValueError:
            pass
    if len(deal["also"]) >= 1:
        score += 5                   # 複数媒体が書いている＝大きめのセール
    return score


# ---------------------------------------------------------------- 開催中のセール（公式ページ）

AREA_NOISE = re.compile(r"[?&](are|lma|lc|ppc|rc|st|adc)=")
NAV_NOISE = re.compile(r"^(全国|東京|横浜|名古屋|京都|大阪|神戸|沖縄|北海道|東北|一人旅|"
                       r"ふるさと納税|サステナビリティ|会員特典|キャンペーン・特集)$")

# セール名ではないリンク（案内文・宿側向けの募集・他サービスの勧誘）。
CHIP_NOISE = re.compile(r"こちら|掲載申請|お申し込み|募集|楽天モバイル|楽天カード|楽天銀行|"
                        r"抽選で|初めて利用|会員登録|アプリ|ログイン|使い方")


def collect_official(sources: dict, previous: dict, today: dt.date) -> tuple[list[dict], int, int]:
    """公式ページから開催中のセール名を読む。

    一休.com と Yahoo!トラベル は GitHub Actions のIPからだと 403 を返す
    （自宅から素のHTTPで叩けば通る）。取れなかった回に空で上書きすると
    「セールが消えた」ように見えるので、前回読めた内容をそのまま残して
    「いつ時点のものか」を添える。
    """
    prev_items = {r["name"]: r for r in previous.get("running", [])}
    running: list[dict] = []
    ok = fail = 0
    for site in sources["official"]:
        entry = {
            "name": site["name"],
            "kind": site["kind"],
            "mode": site["mode"],
            "url": site["url"],
            "why": site.get("why", ""),
            "items": [],
        }
        if site["mode"] == "auto":
            html = fetch(site["url"])
            time.sleep(FETCH_INTERVAL)
            if not html:
                fail += 1
                old = prev_items.get(site["name"], {})
                if old.get("items"):
                    # 前回読めた内容を残す。いつ時点かはサイト側で出す。
                    entry["items"] = old["items"]
                    entry["seen"] = old.get("seen") or today.isoformat()
                    entry["stale"] = True
                else:
                    entry["mode"] = "link"
                    entry["why"] = "今回は取得できなかった"
            else:
                ok += 1
                entry["seen"] = today.isoformat()
                pattern = re.compile(site["link_pattern"])
                seen: set[str] = set()
                for m in re.finditer(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S | re.I):
                    href = htmllib.unescape(m.group(1))
                    if not pattern.search(href) or AREA_NOISE.search(href):
                        continue
                    label = strip_tags(m.group(2))
                    if not label:
                        alt = re.search(r'alt="([^"]+)"', m.group(2))
                        label = htmllib.unescape(alt.group(1)).strip() if alt else ""
                    if not label or len(label) > 44 or label in seen or NAV_NOISE.match(label):
                        continue
                    if CHIP_NOISE.search(label):
                        continue
                    seen.add(label)
                    entry["items"].append({
                        "label": label,
                        "url": urllib.parse.urljoin(site["url"], href),
                        "size": extract_size(label),
                    })
                entry["items"] = entry["items"][:8]
                if not entry["items"]:
                    old = prev_items.get(site["name"], {})
                    if old.get("items"):
                        entry["items"] = old["items"]
                        entry["seen"] = old.get("seen")
                        entry["stale"] = True
        running.append(entry)
    return running, ok, fail


# ---------------------------------------------------------------- 今月のカレンダー

def build_calendar(sources: dict, today: dt.date) -> list[dict]:
    rows = []
    for rule in sources["calendar"]:
        row = {
            "name": rule["name"],
            "kind": rule["kind"],
            "note": rule["note"],
            "url": rule["url"],
            "hint": rule.get("hint", ""),
            "dates": [],
            "next": None,
        }
        for day in rule.get("days", []):
            try:
                date = dt.date(today.year, today.month, day)
            except ValueError:
                continue
            row["dates"].append(date.isoformat())
            if row["next"] is None and date >= today:
                row["next"] = date.isoformat()
        if rule.get("days") and row["next"] is None:
            # 今月ぶんが終わっていたら来月の最初の日を出す。
            nxt = (today.replace(day=1) + dt.timedelta(days=32)).replace(day=min(rule["days"]))
            row["next"] = nxt.isoformat()
        rows.append(row)
    return rows


# ---------------------------------------------------------------- 書き出し

def main() -> int:
    with open(SOURCES_PATH, encoding="utf-8") as fh:
        sources = json.load(fh)

    now = dt.datetime.now(JST)
    today = now.date()

    print("セール告知を集めています…")
    raw, news_ok, news_fail = collect_news(sources, today)
    print(f"  記事 {len(raw)} 本（取得成功 {news_ok} / 失敗 {news_fail}）")

    deals = build_deals(raw, sources["home"], today, sources.get("block_sources", []))
    print(f"  セール告知として残ったもの {len(deals)} 件")

    previous = {}
    if os.path.exists(OUTPUT_PATH):
        try:
            with open(OUTPUT_PATH, encoding="utf-8") as fh:
                previous = json.load(fh)
        except (OSError, ValueError):
            previous = {}

    running, off_ok, off_fail = collect_official(sources, previous, today)
    print(f"  公式ページ 自動取得 {off_ok} 件 / 取れず {off_fail} 件")

    calendar = build_calendar(sources, today)

    # 半分以上が取れなかった回は書き込まない。一時的な通信失敗で
    # 「セールが全部消えた」ように見えるのを防ぐ（apple_price_watch と同じ蓋）。
    total, failed = news_ok + off_ok + news_fail + off_fail, news_fail + off_fail
    if total and failed > total / 2:
        print("取得できなかった先が多すぎるため、今回は書き込みを中止します。", file=sys.stderr)
        return 1
    if not deals and os.path.exists(OUTPUT_PATH):
        print("セール告知が1件も取れなかったため、前回の内容を残します。", file=sys.stderr)
        return 1

    payload = {
        "updated": now.strftime("%Y-%m-%d %H:%M"),
        "home": sources["home"],
        "deals": deals,
        "running": running,
        "calendar": calendar,
        "stats": {"articles": len(raw), "deals": len(deals),
                  "fetched": news_ok + off_ok, "failed": failed},
    }

    body = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True)
    if os.path.exists(OUTPUT_PATH):
        with open(OUTPUT_PATH, encoding="utf-8") as fh:
            old = fh.read()
        # updated だけ違う回は書かない。空コミットが積み上がるため。
        if re.sub(r'"updated":[^\n]*', "", old) == re.sub(r'"updated":[^\n]*', "", body):
            print("前回と中身が同じでした。書き込みません。")
            return 0

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    print(f"書き出しました: {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
