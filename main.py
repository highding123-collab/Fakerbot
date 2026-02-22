import os
import json
import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters


# -----------------------------
# ENV
# -----------------------------
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

PANDASCORE_BASE = "https://api.pandascore.co"
DEFAULT_PER_PAGE = 10

# ✅ 토큰은 "전역변수로 고정"하지 않고, 매번 getenv()로 읽는다 (Railway 변수 반영 문제 100% 방지)
def get_pandascore_token() -> str:
    return os.getenv("PANDASCORE_TOKEN", "").strip()


# -----------------------------
# Utilities
# -----------------------------
def _fmt_dt(iso_str: str | None) -> str:
    if not iso_str:
        return "시간 정보 없음"
    try:
        # PandaScore는 보통 ISO8601 (UTC) 제공
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        # 한국시간(+9) 표시는 원하면 바꿔도 됨. 여기선 UTC 유지.
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso_str

def _norm(s: str) -> str:
    return (s or "").strip().lower()

def _safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default


# -----------------------------
# PandaScore HTTP (no extra deps)
# -----------------------------
async def ps_get(path: str, params: dict | None = None) -> list | dict:
    token = get_pandascore_token()
    if not token:
        raise RuntimeError("NO_TOKEN")

    qs = ""
    if params:
        qs = "?" + urlencode(params, doseq=True)

    url = f"{PANDASCORE_BASE}{path}{qs}"

    def _do_request():
        req = Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "User-Agent": "telegram-bot/1.0",
            },
            method="GET",
        )
        with urlopen(req, timeout=15) as resp:
            data = resp.read().decode("utf-8", errors="ignore")
            return json.loads(data)

    try:
        return await asyncio.to_thread(_do_request)
    except HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            pass
        raise RuntimeError(f"HTTP_{e.code}:{body[:400]}")
    except URLError as e:
        raise RuntimeError(f"URL_ERROR:{e}")
    except Exception as e:
        raise RuntimeError(f"REQ_ERROR:{e}")


# -----------------------------
# Domain models
# -----------------------------
@dataclass
class Team:
    id: int
    name: str
    acronym: str | None = None

@dataclass
class MatchInfo:
    id: int
    begin_at: str | None
    league: str | None
    serie: str | None
    opponents: list  # [{"id":..,"name":..}, ...]
    winner_id: int | None
    status: str | None
    name: str | None


# -----------------------------
# PandaScore helpers (LoL 중심)
# -----------------------------
async def find_lol_team(query: str) -> Team | None:
    q = query.strip()
    if not q:
        return None

    # 1) search[name]
    data = await ps_get(
        "/lol/teams",
        params={
            "search[name]": q,
            "per_page": 10,
        },
    )
    if isinstance(data, list) and data:
        t = data[0]
        return Team(id=_safe_int(t.get("id")), name=t.get("name") or q, acronym=t.get("acronym"))

    # 2) search[acronym] fallback
    data2 = await ps_get(
        "/lol/teams",
        params={
            "search[acronym]": q,
            "per_page": 10,
        },
    )
    if isinstance(data2, list) and data2:
        t = data2[0]
        return Team(id=_safe_int(t.get("id")), name=t.get("name") or q, acronym=t.get("acronym"))

    return None

def _parse_match(m: dict) -> MatchInfo:
    opponents = []
    for o in (m.get("opponents") or []):
        opp = o.get("opponent") or {}
        if opp.get("id") is not None:
            opponents.append({"id": _safe_int(opp.get("id")), "name": opp.get("name") or "Unknown"})
    league = (m.get("league") or {}).get("name")
    serie = (m.get("serie") or {}).get("full_name") or (m.get("serie") or {}).get("name")
    return MatchInfo(
        id=_safe_int(m.get("id")),
        begin_at=m.get("begin_at"),
        league=league,
        serie=serie,
        opponents=opponents,
        winner_id=_safe_int(m.get("winner_id"), None) if m.get("winner_id") is not None else None,
        status=m.get("status"),
        name=m.get("name"),
    )

async def get_upcoming_matches_for_team(team: Team, limit: int = 5) -> list[MatchInfo]:
    # PandaScore 필터가 환경/버전에 따라 다를 수 있어서
    # 1) filter[opponent_id] 시도 -> 2) search[opponents.name] fallback
    params_try = [
        ("/lol/matches/upcoming", {"filter[opponent_id]": team.id, "per_page": limit}),
        ("/lol/matches/upcoming", {"search[opponents.name]": team.name, "per_page": limit}),
    ]

    for path, params in params_try:
        try:
            data = await ps_get(path, params=params)
            if isinstance(data, list) and data:
                return [_parse_match(x) for x in data[:limit]]
        except Exception:
            continue

    return []

async def get_recent_matches_for_team(team: Team, limit: int = 10) -> list[MatchInfo]:
    params_try = [
        ("/lol/matches/past", {"filter[opponent_id]": team.id, "per_page": limit}),
        ("/lol/matches/past", {"search[opponents.name]": team.name, "per_page": limit}),
    ]

    for path, params in params_try:
        try:
            data = await ps_get(path, params=params)
            if isinstance(data, list) and data:
                return [_parse_match(x) for x in data[:limit]]
        except Exception:
            continue

    return []

def calc_winrate(team: Team, matches: list[MatchInfo]) -> tuple[int, int, float]:
    # (wins, total, rate)
    total = 0
    wins = 0
    for m in matches:
        # 결과 없는 경기(취소 등) 제외
        if not m.winner_id:
            continue
        # 팀이 포함된 경기만 카운트(서치 fallback 때문에 가끔 섞일 수 있음)
        ids = [o["id"] for o in m.opponents]
        if team.id not in ids:
            continue
        total += 1
        if m.winner_id == team.id:
            wins += 1
    rate = (wins / total) if total else 0.0
    return wins, total, rate

def predict_winner(team_a: Team, team_b: Team, recent_a: list[MatchInfo], recent_b: list[MatchInfo]) -> tuple[str, str]:
    # 아주 단순 예측: 최근 N경기 승률 비교
    wa, ta, ra = calc_winrate(team_a, recent_a)
    wb, tb, rb = calc_winrate(team_b, recent_b)

    # confidence 메시지
    diff = ra - rb
    if ta == 0 or tb == 0:
        return team_a.name, "데이터가 부족해서 기본값(요청 팀 기준)으로만 추천했어."

    if abs(diff) >= 0.30:
        conf = "꽤 강함"
    elif abs(diff) >= 0.15:
        conf = "중간"
    else:
        conf = "약함(박빙)"

    winner = team_a if diff >= 0 else team_b
    reason = (
        f"최근전적 기준 승률: {team_a.name} {wa}/{ta} ({ra:.0%}) vs {team_b.name} {wb}/{tb} ({rb:.0%})\n"
        f"추천 강도: {conf}"
    )
    return winner.name, reason


# -----------------------------
# Telegram: dot command router
# -----------------------------
HELP_TEXT = (
    "🤖 Fakerbot (e스포츠/스포츠 분석)\n\n"
    "✅ 명령어(점(.)으로 시작)\n"
    "• .help : 도움말\n"
    "• .ping : 살아있나 확인\n"
    "• .team lol <팀명> : 팀 검색(LoL)\n"
    "• .upcoming lol <팀명> : 다가오는 경기\n"
    "• .match lol <팀A> <팀B> : 두 팀 비교 + 추천 승리팀(예측)\n\n"
    "예시)\n"
    "• .team lol T1\n"
    "• .upcoming lol T1\n"
    "• .match lol T1 gen\n"
)

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    if not text.startswith("."):
        return

    parts = text[1:].split()
    if not parts:
        return

    cmd = _norm(parts[0])
    args = parts[1:]

    if cmd in ("help", "h"):
        await update.message.reply_text(HELP_TEXT)
        return

    if cmd == "ping":
        token_ok = "OK" if get_pandascore_token() else "NO_PANDASCORE_TOKEN"
        await update.message.reply_text(f"pong ✅ (PANDASCORE_TOKEN={token_ok})")
        return

    # 토큰 필요한 커맨드들
    if cmd in ("team", "upcoming", "match"):
        if not get_pandascore_token():
            await update.message.reply_text(
                "❌ PandaScore 토큰이 없어.\n"
                "Railway Variables에 `PANDASCORE_TOKEN` 추가하고, 컨테이너 재시작(또는 Redeploy) 해줘.\n"
                "그리고 `.ping`로 토큰 OK 뜨는지 확인!"
            )
            return

    try:
        if cmd == "team":
            # .team lol T1
            if len(args) < 2:
                await update.message.reply_text("사용법: .team lol <팀명>\n예: .team lol T1")
                return
            game = _norm(args[0])
            q = " ".join(args[1:])
            if game != "lol":
                await update.message.reply_text("지금은 lol만 지원해. 예: .team lol T1")
                return

            team = await find_lol_team(q)
            if not team:
                await update.message.reply_text(f"팀을 못 찾았어: {q}")
                return

            await update.message.reply_text(
                f"✅ 팀 찾음\n"
                f"- 이름: {team.name}\n"
                f"- 약자: {team.acronym or '없음'}\n"
                f"- ID: {team.id}"
            )
            return

        if cmd == "upcoming":
            # .upcoming lol T1
            if len(args) < 2:
                await update.message.reply_text("사용법: .upcoming lol <팀명>\n예: .upcoming lol T1")
                return
            game = _norm(args[0])
            q = " ".join(args[1:])
            if game != "lol":
                await update.message.reply_text("지금은 lol만 지원해. 예: .upcoming lol T1")
                return

            team = await find_lol_team(q)
            if not team:
                await update.message.reply_text(f"팀을 못 찾았어: {q}")
                return

            upcoming = await get_upcoming_matches_for_team(team, limit=5)
            if not upcoming:
                await update.message.reply_text(f"다가오는 경기 정보를 못 가져왔어. (팀: {team.name})")
                return

            lines = [f"📅 {team.name} 다가오는 경기(최대 5개)"]
            for m in upcoming:
                opp_names = [o["name"] for o in m.opponents]
                lines.append(
                    f"\n• {_fmt_dt(m.begin_at)}\n"
                    f"  - {m.league or '리그?'} / {m.serie or '시리즈?'}\n"
                    f"  - 매치: {' vs '.join(opp_names) if opp_names else (m.name or 'Unknown')}"
                )
            await update.message.reply_text("\n".join(lines))
            return

        if cmd == "match":
            # .match lol T1 gen
            if len(args) < 3:
                await update.message.reply_text("사용법: .match lol <팀A> <팀B>\n예: .match lol T1 gen")
                return
            game = _norm(args[0])
            if game != "lol":
                await update.message.reply_text("지금은 lol만 지원해. 예: .match lol T1 gen")
                return

            team_a_q = args[1]
            team_b_q = args[2]

            team_a = await find_lol_team(team_a_q)
            team_b = await find_lol_team(team_b_q)

            if not team_a or not team_b:
                await update.message.reply_text(
                    f"팀을 못 찾았어.\n"
                    f"- 팀A: {team_a_q} ({'OK' if team_a else 'NOT FOUND'})\n"
                    f"- 팀B: {team_b_q} ({'OK' if team_b else 'NOT FOUND'})"
                )
                return

            # 최근 전적 기반 예측
            recent_a = await get_recent_matches_for_team(team_a, limit=10)
            recent_b = await get_recent_matches_for_team(team_b, limit=10)

            winner, reason = predict_winner(team_a, team_b, recent_a, recent_b)

            wa, ta, ra = calc_winrate(team_a, recent_a)
            wb, tb, rb = calc_winrate(team_b, recent_b)

            msg = (
                f"🏟️ 매치업 분석 (LoL)\n"
                f"{team_a.name} vs {team_b.name}\n\n"
                f"📈 최근전적(최대 10경기 기준)\n"
                f"- {team_a.name}: {wa}/{ta} ({ra:.0%})\n"
                f"- {team_b.name}: {wb}/{tb} ({rb:.0%})\n\n"
                f"⭐ 추천 승리팀(예측): **{winner}**\n"
                f"{reason}\n\n"
                f"※ 참고: 이건 단순 통계 기반 예측이라 확정 아님."
            )
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        # Unknown command
        await update.message.reply_text("알 수 없는 명령어야. `.help` 쳐봐")
        return

    except RuntimeError as e:
        s = str(e)
        if s == "NO_TOKEN":
            await update.message.reply_text(
                "❌ PandaScore 토큰이 없어.\n"
                "Railway Variables에 `PANDASCORE_TOKEN` 추가하고 재시작(또는 Redeploy) 해줘.\n"
                "그리고 `.ping`로 토큰 OK 확인!"
            )
            return

        # API 에러 상세 출력 (너가 디버깅하기 쉽게)
        await update.message.reply_text(f"⚠️ API 오류: {s[:800]}")
        return

    except Exception as e:
        await update.message.reply_text(f"⚠️ 오류: {type(e).__name__}: {str(e)[:800]}")
        return


def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 환경변수가 없어!")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))
    app.run_polling()


if __name__ == "__main__":
    main()
