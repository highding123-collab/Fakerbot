
import os
import sqlite3
import json
import time
import urllib.parse
import urllib.request
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters


# =========================
# CONFIG
# =========================
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

DB_PATH = os.getenv("DB_PATH", "sports_analytics.db")

THESPORTSDB_KEY = os.getenv("THESPORTSDB_KEY", "").strip() or "1"  # test key fallback
PANDASCORE_TOKEN = os.getenv("PANDASCORE_TOKEN", "").strip()

ALERT_MINUTES = int(os.getenv("ALERT_MINUTES", "10"))

KST = ZoneInfo("Asia/Seoul")
UTC = timezone.utc

# SportsDB endpoints
TSDB_BASE = "https://www.thesportsdb.com/api/v1/json/{key}/"

# PandaScore endpoints
PS_BASE = "https://api.pandascore.co"


# =========================
# DB
# =========================
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL;")
    con.execute("PRAGMA synchronous=NORMAL;")
    return con


def init_db():
    with db() as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS cache (
                k TEXT PRIMARY KEY,
                v TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                sports INTEGER NOT NULL DEFAULT 1,
                esports INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS alerts_sent (
                id TEXT PRIMARY KEY,
                sent_at INTEGER NOT NULL
            );
            """
        )


def cache_get(k: str):
    now = int(time.time())
    with db() as con:
        row = con.execute("SELECT v, expires_at FROM cache WHERE k=?", (k,)).fetchone()
        if not row:
            return None
        if int(row["expires_at"]) < now:
            con.execute("DELETE FROM cache WHERE k=?", (k,))
            return None
        return json.loads(row["v"])


def cache_set(k: str, payload, ttl_seconds: int):
    expires_at = int(time.time()) + ttl_seconds
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO cache(k, v, expires_at) VALUES(?,?,?)",
            (k, json.dumps(payload, ensure_ascii=False), expires_at),
        )


def sub_set(chat_id: int, enabled: bool):
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO subscriptions(chat_id, enabled, sports, esports) "
            "VALUES(?, ?, COALESCE((SELECT sports FROM subscriptions WHERE chat_id=?),1), "
            "COALESCE((SELECT esports FROM subscriptions WHERE chat_id=?),0))",
            (chat_id, 1 if enabled else 0, chat_id, chat_id),
        )


def sub_toggle_domain(chat_id: int, domain: str, on: bool):
    # domain: sports/esports
    with db() as con:
        row = con.execute("SELECT sports, esports, enabled FROM subscriptions WHERE chat_id=?", (chat_id,)).fetchone()
        sports = int(row["sports"]) if row else 1
        esports = int(row["esports"]) if row else 0
        enabled = int(row["enabled"]) if row else 0

        if domain == "sports":
            sports = 1 if on else 0
        elif domain == "esports":
            esports = 1 if on else 0

        con.execute(
            "INSERT OR REPLACE INTO subscriptions(chat_id, enabled, sports, esports) VALUES(?,?,?,?)",
            (chat_id, enabled, sports, esports),
        )


def get_subscriptions_enabled():
    with db() as con:
        return con.execute("SELECT chat_id, sports, esports FROM subscriptions WHERE enabled=1").fetchall()


def alert_sent(alert_id: str) -> bool:
    with db() as con:
        row = con.execute("SELECT 1 FROM alerts_sent WHERE id=?", (alert_id,)).fetchone()
        return row is not None


def mark_alert_sent(alert_id: str):
    with db() as con:
        con.execute(
            "INSERT OR REPLACE INTO alerts_sent(id, sent_at) VALUES(?,?)",
            (alert_id, int(time.time())),
        )


# =========================
# HTTP helpers
# =========================
def _http_get_json(url: str, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = r.read().decode("utf-8")
        return json.loads(data)


async def http_get_json(url: str, headers: dict | None = None) -> dict:
    return await asyncio.to_thread(_http_get_json, url, headers)


# =========================
# SPORTS provider (TheSportsDB)
# =========================
@dataclass
class MatchItem:
    title: str
    start_utc: datetime | None
    league: str | None
    extra: str | None


def tsdb_url(path: str, **params):
    base = TSDB_BASE.format(key=THESPORTSDB_KEY) + path
    if params:
        return base + "?" + urllib.parse.urlencode(params)
    return base


async def sports_today(sport: str, date_ymd: str) -> list[MatchItem]:
    """
    sport examples: Soccer, Basketball, Esports (SportsDB는 Esports도 종종 들어있음)
    """
    cache_key = f"tsdb:eventsday:{sport}:{date_ymd}"
    cached = cache_get(cache_key)
    if cached is None:
        url = tsdb_url("eventsday.php", s=sport, d=date_ymd)
        data = await http_get_json(url)
        cache_set(cache_key, data, ttl_seconds=60)  # 1분 캐시
    else:
        data = cached

    events = data.get("events") or []
    out = []
    for e in events:
        home = (e.get("strHomeTeam") or "").strip()
        away = (e.get("strAwayTeam") or "").strip()
        league = (e.get("strLeague") or "").strip() or None
        t_utc = None
        # SportsDB: strTimestamp often UTC like "2025-01-01 20:00:00"
        ts = (e.get("strTimestamp") or "").strip()
        if ts:
            try:
                t_utc = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
            except Exception:
                t_utc = None

        title = f"{home} vs {away}".strip(" vs ")
        out.append(MatchItem(title=title, start_utc=t_utc, league=league, extra=None))
    return out


async def sports_search_team(team_name: str) -> dict | None:
    cache_key = f"tsdb:searchteam:{team_name.lower()}"
    cached = cache_get(cache_key)
    if cached is None:
        url = tsdb_url("searchteams.php", t=team_name)
        data = await http_get_json(url)
        cache_set(cache_key, data, ttl_seconds=3600)  # 1시간 캐시
    else:
        data = cached
    teams = data.get("teams") or []
    return teams[0] if teams else None


async def sports_last_events(team_id: str) -> list[dict]:
    cache_key = f"tsdb:lastevents:{team_id}"
    cached = cache_get(cache_key)
    if cached is None:
        url = tsdb_url("eventslast.php", id=team_id)
        data = await http_get_json(url)
        cache_set(cache_key, data, ttl_seconds=300)  # 5분 캐시
    else:
        data = cached
    return data.get("results") or []


def _form_from_events(events: list[dict], team_name: str) -> tuple[str, str]:
    """
    간단 폼(W/D/L) + 요약 문자열
    """
    form = []
    lines = []
    team = team_name.lower()

    for e in events[:5]:
        h = (e.get("strHomeTeam") or "")
        a = (e.get("strAwayTeam") or "")
        hs = e.get("intHomeScore")
        as_ = e.get("intAwayScore")

        if hs is None or as_ is None:
            continue
        try:
            hs = int(hs)
            as_ = int(as_)
        except Exception:
            continue

        is_home = (h.lower() == team)
        gf = hs if is_home else as_
        ga = as_ if is_home else hs

        if gf > ga:
            form.append("W")
        elif gf < ga:
            form.append("L")
        else:
            form.append("D")

        lines.append(f"{h} {hs}-{as_} {a}")

    return "".join(form) if form else "N/A", "\n".join(lines) if lines else "최근 경기 데이터 없음"


async def sports_analyze_match(team1: str, team2: str) -> str:
    t1 = await sports_search_team(team1)
    t2 = await sports_search_team(team2)

    if not t1 or not t2:
        return "팀을 찾지 못했어. (철자/영문명/약칭 확인해줘)\n예: .match soccer Arsenal Chelsea"

    t1_name = t1.get("strTeam") or team1
    t2_name = t2.get("strTeam") or team2

    ev1 = await sports_last_events(t1.get("idTeam"))
    ev2 = await sports_last_events(t2.get("idTeam"))

    f1, l1 = _form_from_events(ev1, t1_name)
    f2, l2 = _form_from_events(ev2, t2_name)

    # 매우 단순한 폼 점수(가벼운 분석)
    score_map = {"W": 3, "D": 1, "L": 0}
    s1 = sum(score_map.get(x, 0) for x in f1 if x in score_map)
    s2 = sum(score_map.get(x, 0) for x in f2 if x in score_map)

    if s1 > s2 + 2:
        lean = f"📌 {t1_name} 우세(최근 폼 기준)"
    elif s2 > s1 + 2:
        lean = f"📌 {t2_name} 우세(최근 폼 기준)"
    else:
        lean = "📌 박빙(최근 폼 기준)"

    msg = (
        f"🏟️ MATCH 분석 (스포츠)\n"
        f"{t1_name} vs {t2_name}\n\n"
        f"— 최근 5경기 폼 —\n"
        f"{t1_name}: {f1} (점수 {s1})\n"
        f"{t2_name}: {f2} (점수 {s2})\n"
        f"{lean}\n\n"
        f"— {t1_name} 최근 경기 —\n{l1}\n\n"
        f"— {t2_name} 최근 경기 —\n{l2}"
    )
    return msg


# =========================
# ESPORTS provider (PandaScore) - token required
# =========================
ESPORT_SLUG = {
    "lol": "league-of-legends",
    "cs2": "cs-go",         # PandaScore는 여전히 cs-go 슬러그를 쓰는 경우가 많음(서비스 정책에 따라 다를 수 있음)
    "valorant": "valorant",
    "dota2": "dota-2",
}

async def pandascore_matches_today(game_key: str, day_ymd: str) -> list[MatchItem]:
    if not PANDASCORE_TOKEN:
        return []

    slug = ESPORT_SLUG.get(game_key.lower())
    if not slug:
        return []

    # 오늘 00:00~23:59 UTC 범위
    start = datetime.strptime(day_ymd, "%Y-%m-%d").replace(tzinfo=UTC)
    end = start + timedelta(days=1)

    cache_key = f"ps:today:{slug}:{day_ymd}"
    cached = cache_get(cache_key)
    if cached is None:
        # PandaScore REST: /matches?token=... + range filter
        # 공식 레퍼런스는 match endpoints 제공.  [oai_citation:4‡PandaScore Developers](https://developers.pandascore.co/reference/get_matches_matchidorslug?utm_source=chatgpt.com)
        params = {
            "token": PANDASCORE_TOKEN,
            "sort": "begin_at",
            "range[begin_at]": f"{start.isoformat()},{end.isoformat()}",
            "filter[videogame]": slug,
            "per_page": "50",
        }
        url = f"{PS_BASE}/matches?{urllib.parse.urlencode(params)}"
        data = await http_get_json(url)
        cache_set(cache_key, data, ttl_seconds=60)
    else:
        data = cached

    out = []
    if isinstance(data, list):
        for m in data:
            opp = m.get("opponents") or []
            name1 = (opp[0].get("opponent", {}).get("name") if len(opp) > 0 else None) or "TBD"
            name2 = (opp[1].get("opponent", {}).get("name") if len(opp) > 1 else None) or "TBD"
            league = None
            if m.get("league") and isinstance(m["league"], dict):
                league = m["league"].get("name")
            t_utc = None
            begin_at = m.get("begin_at")
            if begin_at:
                try:
                    t_utc = datetime.fromisoformat(begin_at.replace("Z", "+00:00")).astimezone(UTC)
                except Exception:
                    t_utc = None
            out.append(MatchItem(title=f"{name1} vs {name2}", start_utc=t_utc, league=league, extra=None))
    return out


async def esports_analyze_match(game_key: str, team1: str, team2: str) -> str:
    if not PANDASCORE_TOKEN:
        return (
            "e스포츠 분석은 PandaScore 토큰이 필요해.\n"
            "Railway Variables에 PANDASCORE_TOKEN을 추가해줘."
        )

    slug = ESPORT_SLUG.get(game_key.lower())
    if not slug:
        return "지원 게임: lol / valorant / cs2 / dota2"

    # 간단 버전: 오늘~7일 사이에서 team 포함 매치 찾아서 최근 폼(승패) 요약
    start = datetime.now(UTC)
    end = start + timedelta(days=7)

    params = {
        "token": PANDASCORE_TOKEN,
        "sort": "begin_at",
        "range[begin_at]": f"{start.isoformat()},{end.isoformat()}",
        "filter[videogame]": slug,
        "per_page": "50",
        "search[name]": f"{team1}",  # 완벽 매칭은 아님(간단)
    }
    url = f"{PS_BASE}/matches?{urllib.parse.urlencode(params)}"
    data = await http_get_json(url)

    # team1/team2가 같이 포함된 경기 하나라도 찾기
    found = None
    if isinstance(data, list):
        for m in data:
            opp = m.get("opponents") or []
            names = []
            for o in opp:
                n = o.get("opponent", {}).get("name")
                if n:
                    names.append(n.lower())
            if team1.lower() in " ".join(names) and team2.lower() in " ".join(names):
                found = m
                break

    if not found:
        return (
            f"7일 내에 '{team1}' vs '{team2}' 매치를 바로 찾지 못했어.\n"
            f"팀 이름을 공식 표기(영문)로 다시 시도해줘.\n"
            f"예: .match lol T1 Gen.G"
        )

    opp = found.get("opponents") or []
    name1 = (opp[0].get("opponent", {}).get("name") if len(opp) > 0 else None) or team1
    name2 = (opp[1].get("opponent", {}).get("name") if len(opp) > 1 else None) or team2
    begin_at = found.get("begin_at") or "TBD"
    league = (found.get("league") or {}).get("name") if isinstance(found.get("league"), dict) else None

    msg = (
        f"🎮 MATCH 분석 (e스포츠)\n"
        f"{name1} vs {name2}\n"
        f"게임: {game_key.upper()} | 리그: {league or 'N/A'}\n"
        f"시작: {begin_at}\n\n"
        f"📌 팁: PandaScore 데이터로 더 깊게(최근 전적/상대전적/맵 등) 확장 가능.\n"
        f"원하면 '최근 10경기 폼 + 상대전적'까지 자동으로 뽑는 버전으로 업그레이드해줄게."
    )
    return msg


# =========================
# Formatting helpers
# =========================
def fmt_time_kst(dt_utc: datetime | None) -> str:
    if not dt_utc:
        return "시간 미정"
    return dt_utc.astimezone(KST).strftime("%m/%d %H:%M KST")


def clamp_text(s: str, limit=3500) -> str:
    if len(s) <= limit:
        return s
    return s[:limit] + "\n…(생략)"


# =========================
# Commands (. prefix)
# =========================
HELP_TEXT = (
    "📊 스포츠 + e스포츠 분석봇 (점(.) 명령어)\n\n"
    "기본:\n"
    "• .help\n"
    "• .today soccer            (오늘 축구 경기)\n"
    "• .today basketball        (오늘 농구 경기)\n"
    "• .today lol               (오늘 LoL 경기, PandaScore 토큰 필요)\n"
    "• .match soccer Arsenal Chelsea\n"
    "• .match lol T1 GEN\n"
    "• .team soccer Arsenal\n\n"
    "알림:\n"
    "• .alert on / .alert off\n"
    "• .alert sports on/off\n"
    "• .alert esports on/off\n"
)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def cmd_today(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]):
    if not args:
        await update.message.reply_text("사용법: .today soccer | .today basketball | .today lol")
        return

    key = args[0].lower()
    today = datetime.now(KST).strftime("%Y-%m-%d")

    # Sports
    if key in ("soccer", "basketball", "tennis", "mma", "baseball"):
        sport = key.capitalize()
        items = await sports_today(sport=sport, date_ymd=today)
        if not items:
            await update.message.reply_text(f"오늘({today}) {sport} 경기 데이터를 찾지 못했어.")
            return
        lines = [f"📅 오늘 {sport} ({today})", ""]
        for it in items[:20]:
            lines.append(f"• {it.title}  |  {fmt_time_kst(it.start_utc)}" + (f"  |  {it.league}" if it.league else ""))
        if len(items) > 20:
            lines.append(f"\n…외 {len(items)-20}경기")
        await update.message.reply_text(clamp_text("\n".join(lines)))
        return

    # Esports
    if key in ESPORT_SLUG:
        if not PANDASCORE_TOKEN:
            await update.message.reply_text("e스포츠(.today lol/cs2/valorant/dota2)는 PANDASCORE_TOKEN이 필요해.")
            return
        items = await pandascore_matches_today(game_key=key, day_ymd=today)
        if not items:
            await update.message.reply_text(f"오늘({today}) {key.upper()} 경기 데이터를 찾지 못했어.")
            return
        lines = [f"🎮 오늘 {key.upper()} ({today})", ""]
        for it in items[:20]:
            lines.append(f"• {it.title}  |  {fmt_time_kst(it.start_utc)}" + (f"  |  {it.league}" if it.league else ""))
        if len(items) > 20:
            lines.append(f"\n…외 {len(items)-20}경기")
        await update.message.reply_text(clamp_text("\n".join(lines)))
        return

    await update.message.reply_text("지원: soccer/basketball/... 또는 lol/valorant/cs2/dota2")


async def cmd_team(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]):
    if len(args) < 2:
        await update.message.reply_text("사용법: .team soccer Arsenal")
        return

    domain = args[0].lower()
    name = " ".join(args[1:]).strip()

    if domain in ("soccer", "basketball", "tennis", "mma", "baseball"):
        t = await sports_search_team(name)
        if not t:
            await update.message.reply_text("팀을 못 찾았어. 팀명을 다시 확인해줘.")
            return
        team_name = t.get("strTeam") or name
        ev = await sports_last_events(t.get("idTeam"))
        form, lines = _form_from_events(ev, team_name)
        msg = (
            f"🏷️ TEAM 분석\n"
            f"{team_name}\n"
            f"최근 5경기 폼: {form}\n\n"
            f"{lines}"
        )
        await update.message.reply_text(clamp_text(msg))
        return

    await update.message.reply_text("현재 .team 은 sports(soccer/basketball/...)만 기본 지원이야.")


async def cmd_match(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]):
    if len(args) < 3:
        await update.message.reply_text("사용법: .match soccer Arsenal Chelsea  |  .match lol T1 GEN")
        return

    domain = args[0].lower()
    # team names can be multi-word: split in half? 여기선 간단히 2팀을 1/1로만 받기 어려움
    # 그래서 '|' 구분도 지원
    raw = " ".join(args[1:])
    if "|" in raw:
        left, right = [x.strip() for x in raw.split("|", 1)]
        team1, team2 = left, right
    else:
        # 가장 간단: 마지막 토큰을 team2, 나머지를 team1로 처리
        parts = raw.split()
        team2 = parts[-1]
        team1 = " ".join(parts[:-1])

    if domain in ("soccer", "basketball", "tennis", "mma", "baseball"):
        msg = await sports_analyze_match(team1, team2)
        await update.message.reply_text(clamp_text(msg))
        return

    if domain in ESPORT_SLUG:
        msg = await esports_analyze_match(domain, team1, team2)
        await update.message.reply_text(clamp_text(msg))
        return

    await update.message.reply_text("지원: .match soccer ... 또는 .match lol/cs2/valorant/dota2 ...")


async def cmd_alert(update: Update, context: ContextTypes.DEFAULT_TYPE, args: list[str]):
    chat_id = update.effective_chat.id
    if not args:
        await update.message.reply_text("사용법: .alert on/off  |  .alert sports on/off  |  .alert esports on/off")
        return

    if args[0].lower() in ("on", "off"):
        sub_set(chat_id, enabled=(args[0].lower() == "on"))
        await update.message.reply_text(f"알림 {'ON' if args[0].lower()=='on' else 'OFF'}")
        return

    if len(args) == 2 and args[0].lower() in ("sports", "esports") and args[1].lower() in ("on", "off"):
        sub_toggle_domain(chat_id, args[0].lower(), on=(args[1].lower() == "on"))
        await update.message.reply_text(f"{args[0].lower()} 알림 {'ON' if args[1].lower()=='on' else 'OFF'}")
        return

    await update.message.reply_text("사용법: .alert on/off  |  .alert sports on/off  |  .alert esports on/off")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not text.startswith("."):
        return

    parts = text.split()
    cmd = parts[0].lower()
    args = parts[1:]

    try:
        if cmd == ".help":
            await cmd_help(update, context)
        elif cmd == ".today":
            await cmd_today(update, context, args)
        elif cmd == ".match":
            await cmd_match(update, context, args)
        elif cmd == ".team":
            await cmd_team(update, context, args)
        elif cmd == ".alert":
            await cmd_alert(update, context, args)
        else:
            await update.message.reply_text("알 수 없는 명령어야. .help 를 쳐봐")
    except Exception as e:
        await update.message.reply_text(f"에러: {type(e).__name__}: {e}")


# =========================
# Alerts job
# =========================
async def alert_tick(context: ContextTypes.DEFAULT_TYPE):
    subs = get_subscriptions_enabled()
    if not subs:
        return

    now_utc = datetime.now(UTC)
    today = datetime.now(KST).strftime("%Y-%m-%d")

    # 스포츠: Soccer 기본, 농구도 한 번에 체크(가벼운 버전)
    sports_lists = {}
    async def get_sport_events_cached(sport: str):
        if sport in sports_lists:
            return sports_lists[sport]
        items = await sports_today(sport=sport, date_ymd=today)
        sports_lists[sport] = items
        return items

    # e스포츠: lol/valorant/cs2/dota2
    esports_lists = {}
    async def get_esport_events_cached(game_key: str):
        if game_key in esports_lists:
            return esports_lists[game_key]
        items = await pandascore_matches_today(game_key=game_key, day_ymd=today) if PANDASCORE_TOKEN else []
        esports_lists[game_key] = items
        return items

    lead = timedelta(minutes=ALERT_MINUTES)

    for row in subs:
        chat_id = int(row["chat_id"])
        sports_on = int(row["sports"]) == 1
        esports_on = int(row["esports"]) == 1

        # SPORTS alerts
        if sports_on:
            for sport in ("Soccer", "Basketball"):
                items = await get_sport_events_cached(sport)
                for it in items:
                    if not it.start_utc:
                        continue
                    dt = it.start_utc
                    if dt < now_utc - timedelta(minutes=1):
                        continue
                    if now_utc <= dt <= now_utc + lead:
                        alert_id = f"s:{sport}:{it.title}:{int(dt.timestamp())}:{chat_id}"
                        if alert_sent(alert_id):
                            continue
                        mark_alert_sent(alert_id)
                        await context.bot.send_message(
                            chat_id,
                            f"⏰ {ALERT_MINUTES}분 후 경기 시작!\n"
                            f"[{sport}] {it.title}\n"
                            f"시각: {fmt_time_kst(dt)}" + (f"\n리그: {it.league}" if it.league else "")
                        )

        # ESPORTS alerts
        if esports_on and PANDASCORE_TOKEN:
            for game_key in ("lol", "valorant", "cs2", "dota2"):
                items = await get_esport_events_cached(game_key)
                for it in items:
                    if not it.start_utc:
                        continue
                    dt = it.start_utc
                    if dt < now_utc - timedelta(minutes=1):
                        continue
                    if now_utc <= dt <= now_utc + lead:
                        alert_id = f"e:{game_key}:{it.title}:{int(dt.timestamp())}:{chat_id}"
                        if alert_sent(alert_id):
                            continue
                        mark_alert_sent(alert_id)
                        await context.bot.send_message(
                            chat_id,
                            f"⏰ {ALERT_MINUTES}분 후 경기 시작!\n"
                            f"[{game_key.upper()}] {it.title}\n"
                            f"시각: {fmt_time_kst(dt)}" + (f"\n리그: {it.league}" if it.league else "")
                        )


# =========================
# MAIN
# =========================
def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 필요해.")

    init_db()

    app = Application.builder().token(TOKEN).build()

    # dot-commands router
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # alerts job (1분마다)
    app.job_queue.run_repeating(alert_tick, interval=60, first=10)

    app.run_polling()


if __name__ == "__main__":
    main()
