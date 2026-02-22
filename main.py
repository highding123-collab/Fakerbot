import os
import re
import math
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================
# ENV / CONFIG
# =========================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PANDASCORE_TOKEN = os.getenv("PANDASCORE_TOKEN", "").strip()

# (선택) 기본 게임
DEFAULT_GAME = os.getenv("DEFAULT_GAME", "lol").strip().lower()

# PandaScore API
PANDASCORE_BASE = "https://api.pandascore.co"
HTTP_TIMEOUT = 20.0

# 점(.) 커맨드 프리픽스
CMD_PREFIX = "."

# 최근 성적 몇 경기 볼지
RECENT_N = int(os.getenv("RECENT_N", "10"))

# 캐시(팀 검색 결과) - 간단 캐시
TEAM_CACHE_TTL_SEC = 60 * 10  # 10분
_team_cache: Dict[Tuple[str, str], Tuple[float, List[Dict[str, Any]]]] = {}  # (game_slug, query) -> (ts, teams)

# =========================
# GAME SLUG MAP
# =========================
GAME_ALIASES = {
    "lol": "league-of-legends",
    "league": "league-of-legends",
    "lck": "league-of-legends",

    "valo": "valorant",
    "valorant": "valorant",

    "cs": "cs-go",
    "csgo": "cs-go",
    "cs2": "cs-go",

    "dota": "dota-2",
    "dota2": "dota-2",

    "ow": "overwatch",
    "overwatch": "overwatch",

    "r6": "r6-siege",
    "r6s": "r6-siege",

    "pubg": "pubg",
    "apex": "apex-legends",
}

def now_ts() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp())

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def sigmoid(z: float) -> float:
    return 1.0 / (1.0 + math.exp(-z))

def norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()

def game_to_slug(game: str) -> str:
    g = (game or "").strip().lower()
    return GAME_ALIASES.get(g, g)

# =========================
# PandaScore Client
# =========================
class PandaScoreError(Exception):
    pass

@dataclass
class Team:
    id: int
    name: str
    acronym: Optional[str] = None

class PandaScoreClient:
    def __init__(self, token: str):
        self.token = token
        self._client: Optional[httpx.AsyncClient] = None

    async def __aenter__(self):
        headers = {"Authorization": f"Bearer {self.token}"}
        self._client = httpx.AsyncClient(base_url=PANDASCORE_BASE, headers=headers, timeout=HTTP_TIMEOUT)
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._client:
            await self._client.aclose()

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if not self._client:
            raise PandaScoreError("HTTP client not initialized")
        r = await self._client.get(path, params=params or {})
        if r.status_code == 401:
            raise PandaScoreError("PandaScore 토큰이 유효하지 않거나 권한이 없어.")
        if r.status_code >= 400:
            raise PandaScoreError(f"PandaScore API 오류: {r.status_code} - {r.text[:200]}")
        return r.json()

    async def search_teams(self, game_slug: str, query: str, per_page: int = 10) -> List[Team]:
        # 캐시
        key = (game_slug, norm_name(query))
        tnow = now_ts()
        cached = _team_cache.get(key)
        if cached and (tnow - cached[0] < TEAM_CACHE_TTL_SEC):
            return [Team(id=x["id"], name=x["name"], acronym=x.get("acronym")) for x in cached[1]]

        # PandaScore는 팀 검색에 search[name]을 지원하는 경우가 많음
        # videogame은 filter[videogame] 또는 filter[videogame_id]가 케이스마다 다를 수 있어, 여기선 slug 기반으로 matches 쪽에서 주로 제한하고,
        # 팀은 name 검색 후 결과에서 유사도 기준으로 고름.
        data = await self.get("/teams", params={
            "search[name]": query,
            "per_page": per_page
        })

        teams_raw = []
        for it in data or []:
            if "id" in it and "name" in it:
                teams_raw.append({"id": it["id"], "name": it["name"], "acronym": it.get("acronym")})

        _team_cache[key] = (tnow, teams_raw)
        return [Team(id=x["id"], name=x["name"], acronym=x.get("acronym")) for x in teams_raw]

    async def recent_matches_for_team(self, team_id: int, per_page: int = 20) -> List[Dict[str, Any]]:
        # 팀이 등장한 최근 경기들
        # opponent_id 필터는 PandaScore에서 흔히 지원됨
        return await self.get("/matches", params={
            "filter[opponent_id]": team_id,
            "sort": "-begin_at",
            "per_page": per_page
        })

    async def upcoming_matches(self, game_slug: str, per_page: int = 10) -> List[Dict[str, Any]]:
        # 다가오는 경기
        return await self.get("/matches/upcoming", params={
            "filter[videogame]": game_slug,
            "sort": "begin_at",
            "per_page": per_page
        })

# =========================
# Analysis / Recommendation
# =========================
def match_finished(m: Dict[str, Any]) -> bool:
    # PandaScore match status 예: finished, running, not_started 등
    st = (m.get("status") or "").lower()
    return st == "finished"

def extract_opponent_team_ids(m: Dict[str, Any]) -> List[int]:
    opps = m.get("opponents") or []
    ids = []
    for o in opps:
        team = (o or {}).get("opponent") or {}
        tid = team.get("id")
        if isinstance(tid, int):
            ids.append(tid)
    return ids

def winner_team_id(m: Dict[str, Any]) -> Optional[int]:
    wid = m.get("winner_id")
    return wid if isinstance(wid, int) else None

def compute_recent_form(team_id: int, matches: List[Dict[str, Any]], n: int) -> Tuple[int, int, float]:
    """return (wins, games_counted, winrate)"""
    wins = 0
    played = 0
    for m in matches:
        if not match_finished(m):
            continue
        opp_ids = extract_opponent_team_ids(m)
        if team_id not in opp_ids:
            continue
        wid = winner_team_id(m)
        if wid is None:
            continue
        played += 1
        if wid == team_id:
            wins += 1
        if played >= n:
            break
    winrate = (wins / played) if played > 0 else 0.0
    return wins, played, winrate

def head_to_head(team_a: int, team_b: int, matches_a: List[Dict[str, Any]], limit: int = 20) -> Tuple[int, int, int]:
    """
    team_a 관점의 H2H: (a_wins, b_wins, played)
    team_a 최근 경기들 중 team_b와 붙은 경기만 뽑아 계산
    """
    a_w = 0
    b_w = 0
    played = 0
    checked = 0
    for m in matches_a:
        if checked >= limit:
            break
        checked += 1
        if not match_finished(m):
            continue
        opp_ids = extract_opponent_team_ids(m)
        if not (team_a in opp_ids and team_b in opp_ids):
            continue
        wid = winner_team_id(m)
        if wid is None:
            continue
        played += 1
        if wid == team_a:
            a_w += 1
        elif wid == team_b:
            b_w += 1
    return a_w, b_w, played

def recommend(team1_name: str, team2_name: str, t1: Team, t2: Team,
              t1_form: Tuple[int, int, float],
              t2_form: Tuple[int, int, float],
              h2h: Tuple[int, int, int]) -> Tuple[str, float, List[str]]:
    """
    간단 휴리스틱:
    - 최근 승률 차이 + H2H 약간 가중
    - 표본이 적으면 확률을 보수적으로(0.55 근처로) 당김
    """
    (w1, p1, r1) = t1_form
    (w2, p2, r2) = t2_form
    (h1, h2, hp) = h2h

    # base score: winrate diff
    score = (r1 - r2) * 2.0  # [-2,2] 정도
    reasons = []

    reasons.append(f"최근 {RECENT_N}경기 기준: {t1.name} {w1}/{p1} ({r1:.0%}), {t2.name} {w2}/{p2} ({r2:.0%})")

    # H2H
    if hp >= 3:
        h_score = ((h1 - h2) / hp) * 0.8
        score += h_score
        reasons.append(f"상대전(H2H) {hp}경기: {t1.name} {h1}승 {t2.name} {h2}승")
    elif hp > 0:
        h_score = ((h1 - h2) / hp) * 0.4
        score += h_score
        reasons.append(f"상대전(H2H) 표본 적음({hp}경기): {t1.name} {h1}승 {t2.name} {h2}승")

    # 표본 보정: 경기수 적으면 score를 줄여서 확률이 과하게 치우치지 않게
    sample = p1 + p2
    shrink = clamp(sample / (RECENT_N * 2), 0.35, 1.0)  # 최소 0.35까지 축소
    score *= shrink

    # 확률로 변환
    p = sigmoid(score)  # team1이 이길 확률
    # 확률 너무 과대 방지
    p = 0.5 + (p - 0.5) * 0.85

    if p >= 0.5:
        pick = t1.name
        prob = p
        reasons.append(f"추천: {t1.name} (추정 승률 {prob:.0%})")
    else:
        pick = t2.name
        prob = 1.0 - p
        reasons.append(f"추천: {t2.name} (추정 승률 {prob:.0%})")

    # 주의 문구(고정)
    reasons.append("※ 이 추천은 공개 경기 데이터 기반의 간단 휴리스틱이야. 확정/보장 아님.")
    return pick, prob, reasons

# =========================
# Bot Commands (.)
# =========================
HELP_TEXT = (
    "🤖 Fakerbot (e스포츠 분석 봇)\n\n"
    "명령어는 전부 점(.)으로 시작해.\n\n"
    "• .help\n"
    "• .ping\n"
    "• .match <game> <team1> <team2>\n"
    "   예) .match lol T1 gen\n"
    "• .upcoming <game>  (가까운 경기 10개)\n"
    "   예) .upcoming lol\n\n"
    "지원 game 예: lol, valo, cs2, dota2 ...\n"
)

async def send(update: Update, text: str):
    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

def parse_cmd(text: str) -> Tuple[str, List[str]]:
    t = text.strip()
    if not t.startswith(CMD_PREFIX):
        return "", []
    t = t[len(CMD_PREFIX):].strip()
    if not t:
        return "", []
    parts = t.split()
    cmd = parts[0].lower()
    args = parts[1:]
    return cmd, args

async def cmd_ping(update: Update):
    await send(update, "pong ✅")

async def cmd_help(update: Update):
    await send(update, HELP_TEXT)

def pick_best_team(cands: List[Team], query: str) -> Optional[Team]:
    if not cands:
        return None
    q = norm_name(query)
    # exact / acronym / contains 우선
    exact = [t for t in cands if norm_name(t.name) == q]
    if exact:
        return exact[0]
    ac = [t for t in cands if (t.acronym or "").strip().lower() == q]
    if ac:
        return ac[0]
    contains = [t for t in cands if q in norm_name(t.name)]
    if contains:
        return contains[0]
    return cands[0]

async def cmd_match(update: Update, args: List[str]):
    if not PANDASCORE_TOKEN:
        await send(update, "❌ PandaScore 토큰이 없어. Railway Variables에 <b>PANDASCORE_TOKEN</b> 추가해줘.")
        return

    if len(args) < 3:
        await send(update, "사용법: <b>.match &lt;game&gt; &lt;team1&gt; &lt;team2&gt;</b>\n예) <b>.match lol T1 gen</b>")
        return

    game = args[0]
    team1_q = args[1]
    team2_q = args[2]
    game_slug = game_to_slug(game)

    async with PandaScoreClient(PANDASCORE_TOKEN) as api:
        # 팀 검색
        t1_list = await api.search_teams(game_slug, team1_q, per_page=10)
        t2_list = await api.search_teams(game_slug, team2_q, per_page=10)

        t1 = pick_best_team(t1_list, team1_q)
        t2 = pick_best_team(t2_list, team2_q)

        if not t1 or not t2:
            await send(update, "팀을 찾지 못했어. 철자/약칭 확인해서 다시 쳐봐.\n예) <b>.match lol T1 GEN</b>")
            return
        if t1.id == t2.id:
            await send(update, "같은 팀 두 개는 비교 못해 😅")
            return

        # 최근 경기
        m1 = await api.recent_matches_for_team(t1.id, per_page=30)
        m2 = await api.recent_matches_for_team(t2.id, per_page=30)

        t1_form = compute_recent_form(t1.id, m1, RECENT_N)
        t2_form = compute_recent_form(t2.id, m2, RECENT_N)

        # H2H는 t1의 최근 경기에서 t2가 같이 나온 것만 대략 계산
        h2h = head_to_head(t1.id, t2.id, m1, limit=40)

        pick, prob, reasons = recommend(team1_q, team2_q, t1, t2, t1_form, t2_form, h2h)

        # 출력
        lines = []
        lines.append(f"📌 <b>{game_slug}</b> 매치업 분석")
        lines.append(f"• 팀1: <b>{t1.name}</b> (id:{t1.id})")
        lines.append(f"• 팀2: <b>{t2.name}</b> (id:{t2.id})")
        lines.append("")
        lines.extend([f"• {r}" for r in reasons])
        lines.append("")
        lines.append(f"🏆 <b>추천 승리팀:</b> <b>{pick}</b>  (추정 {prob:.0%})")

        await send(update, "\n".join(lines))

async def cmd_upcoming(update: Update, args: List[str]):
    if not PANDASCORE_TOKEN:
        await send(update, "❌ PandaScore 토큰이 없어. Railway Variables에 <b>PANDASCORE_TOKEN</b> 추가해줘.")
        return

    game = args[0] if args else DEFAULT_GAME
    game_slug = game_to_slug(game)

    async with PandaScoreClient(PANDASCORE_TOKEN) as api:
        matches = await api.upcoming_matches(game_slug, per_page=10)

    if not matches:
        await send(update, f"다가오는 경기 정보를 못 찾았어. game 확인해줘: <b>{game_slug}</b>")
        return

    lines = [f"🗓️ <b>{game_slug}</b> Upcoming (최대 10개)"]
    for m in matches:
        begin_at = m.get("begin_at") or ""
        name = m.get("name") or ""
        league = ((m.get("league") or {}).get("name")) or ""
        serie = ((m.get("serie") or {}).get("full_name")) or ""
        lines.append(f"• {begin_at} | {league} {serie} | {name}")

    await send(update, "\n".join(lines))

# =========================
# Router
# =========================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    cmd, args = parse_cmd(text)
    if not cmd:
        return  # 점 커맨드 아니면 무시

    if cmd in ("help", "h"):
        await cmd_help(update)
        return
    if cmd == "ping":
        await cmd_ping(update)
        return
    if cmd in ("match", "m"):
        await cmd_match(update, args)
        return
    if cmd in ("upcoming", "u"):
        await cmd_upcoming(update, args)
        return

    await send(update, f"알 수 없는 명령어야. <b>.help</b> 를 쳐봐")

# =========================
# MAIN
# =========================
def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 필요해.")

    # PandaScore 토큰이 없어도 봇은 켜지게(도움말은 출력 가능)
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
