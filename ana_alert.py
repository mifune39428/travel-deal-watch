#!/usr/bin/env python3
"""ANA・JALのセールが始まったらメールで知らせる。

クラウド（GitHub Actions）が6時間ごとに集めている docs/deals.json を読み、
sources.json の alerts に書いた条件に合う告知が新しく出ていたら1通送る。
ANAの公式ページは素のHTTPだと一覧が取れない（固定URLが無く404）ので、
告知記事（TRAICY・トラベルWatch など）を見るのがいちばん早くて確実。

この Mac の launchd で動かす。Gmailのパスワードを GitHub に置きたくないため
（他のツールと同じ config_unified.json の email_settings を借りる）。

    python3 ana_alert.py            # 新しいセールがあれば送る
    python3 ana_alert.py --dry-run  # 送らずに判定とプレビューだけ
    python3 ana_alert.py --test     # 直近のセールを使って試しに1通送る
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import re
import smtplib
import sys
import time
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SOURCES_PATH = os.path.join(BASE_DIR, "sources.json")
LOCAL_PATH = os.path.join(BASE_DIR, "alert_local.json")
STATE_PATH = os.path.join(BASE_DIR, "alert_state.json")
PREVIEW_PATH = os.path.join(BASE_DIR, "alert_preview.html")
LOCAL_DEALS = os.path.join(BASE_DIR, "docs", "deals.json")

# 手元の docs/deals.json は git pull しないと古いままなので、公開サイトのほうを読む。
LIVE_DEALS = "https://mifune39428.github.io/travel-deal-watch/deals.json"
SITE_URL = "https://mifune39428.github.io/travel-deal-watch/"

# これより古い記事は「いま始まった」とは言えないので見ない。
FRESH_DAYS = 3

JST = dt.timezone(dt.timedelta(hours=9))

BORDER = "#e3e6ea"
INK = "#1f2933"
MUTED = "#6b7684"
ANA_BLUE = "#13448f"
JAL_RED = "#c8102e"
HOT = "#b4442c"


# ---------------------------------------------------------------- 読み込み

def load_deals() -> dict:
    try:
        req = urllib.request.Request(LIVE_DEALS + f"?t={int(time.time())}",
                                     headers={"User-Agent": "travel-deal-watch/ana_alert"})
        with urllib.request.urlopen(req, timeout=30) as res:
            return json.loads(res.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — 取れなければ手元のもので判定する
        print(f"公開サイトのデータを読めませんでした（{exc}）。手元の docs/deals.json を使います。")
        with open(LOCAL_DEALS, encoding="utf-8") as fh:
            return json.load(fh)


def load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
        fh.write("\n")


def load_email_settings() -> dict:
    with open(LOCAL_PATH, encoding="utf-8") as fh:
        path = json.load(fh)["email_config"]
    with open(path, encoding="utf-8") as fh:
        settings = json.load(fh).get("email_settings") or {}
    for key in ("sender_email", "sender_password", "receiver_email"):
        if not settings.get(key):
            raise RuntimeError(f"email_settings.{key} が空です: {path}")
    return settings


# ---------------------------------------------------------------- 判定

def matches(title: str, rule: dict) -> bool:
    # must も「どれか1つ」。ジャルパックの記事は見出しに JAL と書かれないことがあるため。
    if not any(word in title for word in rule["must"]):
        return False
    if not any(word in title for word in rule["any"]):
        return False
    if not any(word in title for word in rule["context"]):
        return False
    return not any(word in title for word in rule["exclude"])


def fresh(deal: dict, today: dt.date) -> bool:
    if not deal.get("published"):
        return False
    try:
        age = (today - dt.date.fromisoformat(deal["published"])).days
    except ValueError:
        return False
    return 0 <= age <= FRESH_DAYS


def deal_id(deal: dict) -> str:
    return deal.get("url") or deal["title"]


# ---------------------------------------------------------------- メール

def jp_date(iso: str | None) -> str:
    if not iso:
        return ""
    d = dt.date.fromisoformat(iso)
    return f"{d.month}月{d.day}日"


SLASH_TILL = re.compile(r"(\d{1,2})/(\d{1,2})\s*(?:まで|迄)")


def slash_end(title: str, published: str | None) -> str | None:
    """「8/31まで」の形の締切。collect.py は「8月31日まで」しか読まないので、ここで補う。"""
    m = SLASH_TILL.search(title)
    if not m or not published:
        return None
    base = dt.date.fromisoformat(published)
    for year in (base.year, base.year + 1):
        try:
            end = dt.date(year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            return None
        if end >= base:
            return end.isoformat()
    return None


def period_text(hits: list[dict]) -> str:
    starts = sorted(d["starts"] for d in hits if d.get("starts"))
    ends = sorted(e for e in (d.get("ends") or slash_end(d["title"], d.get("published")) for d in hits) if e)
    if starts and ends:
        return f"{jp_date(starts[0])}から{jp_date(ends[-1])}まで"
    if ends:
        return f"{jp_date(ends[-1])}まで"
    if starts:
        return f"{jp_date(starts[0])}から"
    return ""


def upcoming_start(hits: list[dict], today: dt.date) -> str | None:
    """開始日がまだ先なら、その日。JALは一般販売の1週間ほど前に「開催決定」の記事が出る。"""
    starts = sorted(d["starts"] for d in hits if d.get("starts"))
    future = [s for s in starts if s > today.isoformat()]
    return future[0] if future else None


def headline(rule: dict, hits: list[dict], today: dt.date) -> str:
    start = upcoming_start(hits, today)
    if start:
        return f"{rule['name']}：{jp_date(start)}から開催"
    return f"{rule['name']}が始まりました"


def build_html(rule: dict, hits: list[dict], now: dt.datetime, test: bool) -> str:
    period = period_text(hits)
    akita = any("秋田" in d["title"] for d in hits)
    rows = []
    for d in hits:
        size = (f'<span style="background:#fbeae5;color:{HOT};font-size:12px;font-weight:700;'
                f'padding:1px 7px;border-radius:9px;margin-right:6px;">{html.escape(d["size"])}</span>'
                if d.get("size") else "")
        rows.append(
            f'<tr><td style="padding:12px 0;border-bottom:1px solid {BORDER};">'
            f'<div style="font-size:15px;font-weight:700;line-height:1.5;">'
            f'<a href="{html.escape(d["url"])}" style="color:{INK};text-decoration:none;">'
            f'{html.escape(d["title"])}</a></div>'
            f'<div style="font-size:12px;color:{MUTED};padding-top:4px;">{size}'
            f'{html.escape(d.get("published") or "")}・{html.escape(d.get("source") or "")}</div>'
            f'</td></tr>'
        )

    note = ("見出しに秋田が出ています。秋田発着便が対象の可能性が高いです。" if akita else
            "秋田発着（羽田・伊丹・新千歳・中部）が対象かどうかは、公式の対象路線で確かめてください。")
    banner = (f'<div style="background:#fff4d6;color:#8a5a00;font-size:13px;padding:8px 12px;'
              f'border-radius:6px;margin-bottom:12px;">これはテスト送信です。直近のセールの記事を使っています。</div>'
              if test else "")

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f7f8fa;">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%" style="background:#f7f8fa;padding:24px 12px;">
<tr><td align="center">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" width="640" style="max-width:640px;background:#ffffff;border:1px solid {BORDER};border-radius:10px;padding:24px;font-family:-apple-system,'Hiragino Sans','Helvetica Neue',sans-serif;">
<tr><td>
  {banner}
  <div style="font-size:12px;color:{brand_color(rule)};font-weight:700;letter-spacing:.04em;">✈️ 旅トク セール速報</div>
  <div style="font-size:20px;font-weight:700;color:{INK};padding-top:4px;">{html.escape(headline(rule, hits, now.date()))}</div>
  <div style="font-size:14px;color:{INK};padding-top:8px;">{html.escape(period) if period else "期間は記事で確かめてください"}</div>
  <div style="font-size:13px;color:{MUTED};padding-top:4px;">{note}</div>
  <div style="padding:16px 0 4px 0;">
    <a href="{html.escape(rule["official"])}" style="display:inline-block;background:{brand_color(rule)};color:#ffffff;font-size:14px;font-weight:700;text-decoration:none;padding:9px 18px;border-radius:7px;">{html.escape(carrier(rule))}公式で運賃を見る</a>
  </div>
</td></tr>
<tr><td style="padding-top:14px;">
  <div style="font-size:13px;color:{MUTED};padding-bottom:2px;">出ている告知（{len(hits)}件）</div>
  <table role="presentation" cellpadding="0" cellspacing="0" border="0" width="100%">{"".join(rows)}</table>
</td></tr>
<tr><td style="padding-top:16px;font-size:12px;color:{MUTED};">
  同じセールの続報は{rule["cooldown_days"]}日間送りません。
  <a href="{SITE_URL}" style="color:{ANA_BLUE};">旅トクで他のセールも見る</a><br>
  {now.strftime("%Y-%m-%d %H:%M")} 確認
</td></tr>
</table>
</td></tr></table>
</body></html>"""


def carrier(rule: dict) -> str:
    return "JAL" if rule["name"].startswith("JAL") else "ANA"


def brand_color(rule: dict) -> str:
    return JAL_RED if carrier(rule) == "JAL" else ANA_BLUE


def build_subject(rule: dict, hits: list[dict], test: bool, today: dt.date) -> str:
    # 件名だけで「どの路線がいくらか」が分かるよう、金額の入った見出しを優先して添える。
    lead = next((d for d in hits if d.get("size") or "円" in d["title"]), hits[0])
    snippet = lead["title"]
    if len(snippet) > 34:
        snippet = snippet[:34] + "…"
    head = "【テスト】" if test else ""
    # 見出しに「：9月8日から開催」が入ることがあるので、記事との区切りは縦線にする。
    return f"{head}[旅トク] {headline(rule, hits, today)} ｜ {snippet}"


def close_quietly(server: smtplib.SMTP | None) -> None:
    """後片付け。ここでの失敗は送信の成否と関係ないので黙って捨てる。"""
    if server is None:
        return
    try:
        server.quit()
    except Exception:  # noqa: BLE001
        try:
            server.close()
        except Exception:  # noqa: BLE001
            pass


def send_mail(subject: str, body: str, settings: dict, retries: int = 3) -> bool:
    """1通送る。

    **送り直していいのは、1通も送っていないと分かっているときだけ。**
    2026-09-24に、送信そのものは成功したのに接続を閉じるところで
    Gmailの応答待ちがタイムアウトし、「失敗」と見て送り直した結果
    同じメールが2通届いた。そのため、接続・ログインまでの失敗は送り直し、
    送信中に切れた場合は届いたものとして扱う。
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr(("旅トク セール速報", settings["sender_email"]))
    msg["To"] = settings["receiver_email"]
    msg.attach(MIMEText(body, "html", "utf-8"))

    for attempt in range(1, retries + 1):
        server = None
        try:
            server = smtplib.SMTP(settings.get("smtp_server", "smtp.gmail.com"),
                                  int(settings.get("smtp_port", 587)), timeout=60)
            server.ehlo()
            server.starttls()
            server.login(settings["sender_email"], settings["sender_password"])
        except Exception as exc:  # noqa: BLE001 — まだ1通も送っていないので、やり直してよい
            close_quietly(server)
            print(f"  つながりませんでした ({attempt}/{retries}): {exc}")
            if attempt < retries:
                time.sleep(5 * attempt)
            continue

        try:
            server.sendmail(settings["sender_email"], settings["receiver_email"], msg.as_string())
        except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused,
                smtplib.SMTPDataError, smtplib.SMTPNotSupportedError) as exc:
            # サーバーがはっきり断った＝届いていない。やり直す。
            close_quietly(server)
            print(f"  受け取ってもらえませんでした ({attempt}/{retries}): {exc}")
            if attempt < retries:
                time.sleep(5 * attempt)
            continue
        except Exception as exc:  # noqa: BLE001
            # 送っている途中で切れた。届いたかどうか分からない。
            # ここでやり直すと同じメールが2通届くので、届いたものとして扱う。
            close_quietly(server)
            print(f"  送信中に接続が切れました。二重送信を避けるため送り直しません: {exc}")
            return True

        close_quietly(server)
        return True
    return False


# ---------------------------------------------------------------- 本体

def main() -> int:
    parser = argparse.ArgumentParser(description="ANA・JALのセール開始をメールで知らせる")
    parser.add_argument("--dry-run", action="store_true", help="送らずに判定とプレビューだけ")
    parser.add_argument("--test", action="store_true", help="直近のセールで試しに1通送る")
    args = parser.parse_args()

    with open(SOURCES_PATH, encoding="utf-8") as fh:
        rules = json.load(fh).get("alerts", [])
    data = load_deals()
    state = load_state()
    now = dt.datetime.now(JST)
    today = now.date()
    print(f"{now:%Y-%m-%d %H:%M} 確認（データの更新 {data.get('updated')}）")

    for rule in rules:
        name = rule["name"]
        # 基準づくりは条件ごと。あとから JAL などを足したときも、いま出ている古いセールで送らない。
        first_run = name not in state
        memo = state.setdefault(name, {"seen": [], "notified": None})
        seen = set(memo["seen"])
        found = [d for d in data.get("deals", []) if matches(d["title"], rule)]

        if args.test:
            # 直近のセールの記事をまとめて試しに送る（状態は変えない）。
            latest = max((d.get("published") or "" for d in found), default="")
            hits = [d for d in found if d.get("published") and latest
                    and (dt.date.fromisoformat(latest) - dt.date.fromisoformat(d["published"])).days <= FRESH_DAYS]
            if not hits:
                print(f"  {name}: 試しに送れる記事がありません")
                continue
            hits.sort(key=lambda d: d.get("published") or "", reverse=True)
            body = build_html(rule, hits[:6], now, test=True)
            with open(PREVIEW_PATH, "w", encoding="utf-8") as fh:
                fh.write(body)
            ok = send_mail(build_subject(rule, hits, True, today), body, load_email_settings())
            print(f"  {name}: テスト送信 {'しました' if ok else 'に失敗しました'}（記事 {len(hits)}件）")
            continue

        hits = [d for d in found if fresh(d, today)]
        new = [d for d in hits if deal_id(d) not in seen]

        if first_run:
            # 初回は基準を作るだけ。いま出ている古いセールで知らせない（apple_price_watch と同じ蓋）。
            memo["seen"] = sorted({deal_id(d) for d in found})
            dates = [d["published"] for d in found if d.get("published")]
            memo["notified"] = max(dates) if dates else None
            print(f"  {name}: 初回なので基準だけ作りました（既存の告知 {len(found)}件、直近 {memo['notified']}）")
            continue

        if not new:
            print(f"  {name}: 新しい告知なし")
            continue

        last = dt.date.fromisoformat(memo["notified"]) if memo.get("notified") else None
        quiet = last is not None and (today - last).days < rule["cooldown_days"]
        memo["seen"] = sorted(seen | {deal_id(d) for d in new})[-300:]

        if quiet:
            # 同じセールの続報（「まもなく開始」→「開催中」など）。黙って既読にする。
            print(f"  {name}: 続報 {len(new)}件（{memo['notified']} に知らせ済み）→ 送らない")
            continue

        hits.sort(key=lambda d: d.get("published") or "", reverse=True)
        subject = build_subject(rule, hits, False, today)
        body = build_html(rule, hits[:6], now, test=False)
        with open(PREVIEW_PATH, "w", encoding="utf-8") as fh:
            fh.write(body)
        if args.dry_run:
            print(f"  {name}: 送るはずの1通 → {subject}（プレビュー: {PREVIEW_PATH}）")
            continue
        if send_mail(subject, body, load_email_settings()):
            memo["notified"] = today.isoformat()
            print(f"  {name}: 送りました → {subject}")
        else:
            # 送れなかったら既読にしない。次の回にもう一度送る。
            memo["seen"] = sorted(seen)
            print(f"  {name}: 送れませんでした。次の回にもう一度試します")

    if not args.test and not args.dry_run:
        save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
