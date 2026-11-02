#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""北京时间早间足球筛选：只研究香港马会当天开售的比赛。"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import smtplib
import sys
import time
import traceback
import unicodedata
from email.message import EmailMessage
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

TZ = ZoneInfo("Asia/Shanghai")
ROOT = Path(__file__).resolve().parent
# football-data.org 免费档能读到当前赛季。这里用程序内部的联赛名，方便沿用原有优先级。


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv(ROOT / ".env")

APIFOOTBALL_KEY = os.environ.get("APIFOOTBALL_KEY", "")
FOOTBALL_DATA_KEY = os.environ.get("FOOTBALL_DATA_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-flash")
PUSH_TYPE = os.environ.get("PUSH_TYPE", "stdout").lower()
PUSH_WEBHOOK = os.environ.get("PUSH_WEBHOOK", "")
PUSH_KEYWORD = os.environ.get("PUSH_KEYWORD", "足球")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


class ApiError(RuntimeError):
    pass


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def is_women_or_youth(name: str) -> bool:
    n = norm(name)
    keys = ("women", "woman", "femenin", "feminina", "u17", "u18", "u19", "u20", "u21", "u23", "youth")
    return any(k in n for k in keys)


def is_friendly(name: str) -> bool:
    n = norm(name)
    return "friendly" in n or "friendlies" in n








def squash_name(name: str) -> str:
    text = norm(name).replace("&", " and ")
    for word in ("fc", "afc", "cf", "sc", "ac"):
        text = re.sub(rf"\b{word}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()









def kickoff_local(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)


def squash_name(name: str) -> str:
    text = norm(name).replace("&", " and ")
    for word in ("fc", "afc", "cf", "sc", "ac"):
        text = re.sub(rf"\b{word}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()





# API-Football 免费档不给超出近三天的赛程。这些联赛用 ESPN 的公开比分榜补上，积分榜也从同一来源取，球队编号才能对上。



HKJC_LEAGUES = {
    "English Premier": ("England", "Premier League"),
    "English Premier League": ("England", "Premier League"),
    "English Championship": ("England", "Championship"),
    "Spanish Division 1": ("Spain", "La Liga"),
    "Spanish Division 2": ("Spain", "La Liga 2"),
    "Italian Division 1": ("Italy", "Serie A"),
    "Italian Division 2": ("Italy", "Serie B"),
    "German Division 1": ("Germany", "Bundesliga"),
    "German Division 2": ("Germany", "2. Bundesliga"),
    "French Division 1": ("France", "Ligue 1"),
    "French Division 2": ("France", "Ligue 2"),
    "Dutch Division 1": ("Netherlands", "Eredivisie"),
    "Dutch Division 2": ("Netherlands", "Eerste Divisie"),
    "Portuguese Premier": ("Portugal", "Primeira Liga"),
    "Brazilian Division 1": ("Brazil", "Serie A"),
    "Brazilian Division 2": ("Brazil", "Serie B"),
    "Argentine Division 1": ("Argentina", "Liga Profesional"),
    "Chilean Division 1": ("Chile", "Primera Division"),
    "Uruguayan Division 1": ("Uruguay", "Primera Division"),
    "Swedish Division 1": ("Sweden", "Superettan"),
    "Finnish Division 1": ("Finland", "Ykkosliiga"),
    "Russian Premier": ("Russia", "Premier League"),
    "Thai League 1": ("Thailand", "Thai League 1"),
    "Asian Champions League Elite": ("Asia", "AFC Champions League Elite"),
    "Danish Superliga": ("Denmark", "Superliga"),
}


HKJC_QUERY = '''
query matchList($startIndex: Int, $endIndex: Int,$startDate: String, $endDate: String, $matchIds: [String], $tournIds: [String], $fbOddsTypes: [FBOddsType]!, $fbOddsTypesM: [FBOddsType]!, $inplayOnly: Boolean, $featuredMatchesOnly: Boolean, $frontEndIds: [String], $earlySettlementOnly: Boolean, $showAllMatch: Boolean) {
    matches(startIndex: $startIndex,endIndex: $endIndex, startDate: $startDate, endDate: $endDate, matchIds: $matchIds, tournIds: $tournIds, fbOddsTypes: $fbOddsTypesM, inplayOnly: $inplayOnly, featuredMatchesOnly: $featuredMatchesOnly, frontEndIds: $frontEndIds, earlySettlementOnly: $earlySettlementOnly, showAllMatch: $showAllMatch) {
      id
      frontEndId
      matchDate
      kickOffTime
      status
      updateAt
      sequence
      esIndicatorEnabled
      homeTeam {
        id
        name_en
        name_ch
      }
      awayTeam {
        id
        name_en
        name_ch
      }
      tournament {
        id
        frontEndId
        nameProfileId
        isInteractiveServiceAvailable
        code
        name_en
        name_ch
      }
      isInteractiveServiceAvailable
      inplayDelay
      venue {
        code
        name_en
        name_ch
      }
      tvChannels {
        code
        name_en
        name_ch
      }
      liveEvents {
        id
        code
      }
      featureStartTime
      featureMatchSequence
      poolInfo {
        normalPools
        inplayPools
        sellingPools
        ntsInfo
        entInfo
        definedPools
      }
      runningResult {
        homeScore
        awayScore
        corner
        homeCorner
        awayCorner
      }
      runningResultExtra {
        homeScore
        awayScore
        corner
        homeCorner
        awayCorner
      }
      adminOperation {
        remark {
          typ
        }
      }
      foPools(fbOddsTypes: $fbOddsTypes) {
        id
        status
        oddsType
        instNo
        inplay
        name_ch
        name_en
        updateAt
        expectedSuspendDateTime
        lines {
          lineId
          status
          condition
          main
          combinations {
            combId
            str
            status
            offerEarlySettlement
            currentOdds
            selections {
              selId
              str
              name_ch
              name_en
            }
          }
        }
      }
    }
  }
'''


def parse_split_line(condition: str | None) -> float | None:
    if not condition:
        return None
    try:
        numbers = [float(part) for part in str(condition).split("/") if part]
    except ValueError:
        return None
    if not numbers:
        return None
    return sum(numbers) / len(numbers)


def hkjc_main_condition(match: dict, odds_type: str) -> str | None:
    pools = [pool for pool in match.get("foPools") or [] if pool.get("oddsType") == odds_type]
    if not pools:
        return None
    # 赛前的大细、角球有时也会被标成 inplay，盘口本身是开着的。
    prematch = [pool for pool in pools if not pool.get("inplay")]
    pools = prematch or pools
    lines = pools[0].get("lines") or []
    mains = [line for line in lines if line.get("main")]
    chosen = (mains or lines or [None])[0]
    if not chosen:
        return None
    return chosen.get("condition")


def parse_odds(value) -> float | None:
    try:
        odds = float(value)
    except (TypeError, ValueError):
        return None
    if odds <= 1.01 or odds > 100:
        return None
    return odds


def hkjc_prices(match: dict, odds_type: str) -> list[dict]:
    """主盘每条选择的赔率。取 line 的顺序和 hkjc_main_condition 保持一致。"""
    pools = [pool for pool in match.get("foPools") or [] if pool.get("oddsType") == odds_type]
    if not pools:
        return []
    prematch = [pool for pool in pools if not pool.get("inplay")]
    pools = prematch or pools
    lines = pools[0].get("lines") or []
    mains = [line for line in lines if line.get("main")]
    chosen = (mains or lines or [None])[0]
    if not chosen:
        return []
    rows = []
    for combo in chosen.get("combinations") or []:
        if str(combo.get("status") or "") not in ("", "AVAILABLE"):
            continue
        selection = (combo.get("selections") or [{}])[0] or {}
        rows.append({
            "str": str(combo.get("str") or ""),
            "name_ch": selection.get("name_ch") or "",
            "odds": parse_odds(combo.get("currentOdds")),
        })
    return rows


def hkjc_query(odds_types: list[str]) -> list[dict]:
    resp = requests.post(
        "https://info.cld.hkjc.com/graphql/base/",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
            "Referer": "https://bet.hkjc.com/",
            "Origin": "https://bet.hkjc.com",
        },
        json={
            "query": HKJC_QUERY,
            "variables": {
                "fbOddsTypes": odds_types,
                "fbOddsTypesM": ["HDC", "HIL", "CHL"],
                "featuredMatchesOnly": False,
                "earlySettlementOnly": False,
                "showAllMatch": False,
            },
        },
        timeout=40,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("errors"):
        raise ApiError(body["errors"][0].get("message") or "马会查询失败")
    return (body.get("data") or {}).get("matches") or []


def hkjc_fixed(match: dict, odds_type: str) -> dict[str, float]:
    """主客和、总入球这类固定选项盘，返回 {选项: 赔率}。"""
    return {
        row["str"]: row["odds"]
        for row in hkjc_prices(match, odds_type)
        if row.get("odds")
    }


def hkjc_card() -> list[dict]:
    def load():
        # 马会一次最多查 4 个盘口类型，所以拆两次请求再按场次合并。
        primary = hkjc_query(["HDC", "HIL", "CHL"])
        extra = {item.get("frontEndId"): item for item in hkjc_query(["HAD", "TTG"])}
        rows = []
        for match in primary:
            other = extra.get(match.get("frontEndId")) or {}
            match["foPools"] = (match.get("foPools") or []) + (other.get("foPools") or [])
            hdc = hkjc_main_condition(match, "HDC")
            total = hkjc_main_condition(match, "HIL")
            corner = hkjc_main_condition(match, "CHL")
            tournament = match.get("tournament") or {}
            rows.append({
                "no": match.get("frontEndId") or "",
                "kickoff": match.get("kickOffTime") or "",
                "status": match.get("status") or "",
                "tournament_en": tournament.get("name_en") or "",
                "tournament_ch": tournament.get("name_ch") or tournament.get("name_en") or "",
                "home": (match.get("homeTeam") or {}).get("name_en") or "",
                "home_ch": (match.get("homeTeam") or {}).get("name_ch") or "",
                "away": (match.get("awayTeam") or {}).get("name_en") or "",
                "away_ch": (match.get("awayTeam") or {}).get("name_ch") or "",
                "home_id": (match.get("homeTeam") or {}).get("id") or match.get("frontEndId"),
                "away_id": (match.get("awayTeam") or {}).get("id") or match.get("frontEndId"),
                "hdc": hdc,
                "home_line": parse_split_line(hdc),
                "total_text": total,
                "total": parse_split_line(total),
                "corner_line": parse_split_line(corner),
                "prices": {
                    "HDC": hkjc_prices(match, "HDC"),
                    "HIL": hkjc_prices(match, "HIL"),
                    "CHL": hkjc_prices(match, "CHL"),
                },
                "markets": {
                    "had": hkjc_fixed(match, "HAD"),
                    "ttg": hkjc_fixed(match, "TTG"),
                },
            })
        return rows

    return cached_json("hkjc-open-card-v2.json", load, max_age_hours=0.25)


def slate_window(day: str) -> tuple[datetime, datetime]:
    """触发日中午到次日中午。26 日 9:00 跑，就是 26 日 12:00 到 27 日 12:00。"""
    trigger = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    start = trigger.replace(hour=12, minute=0, second=0, microsecond=0)
    end = (trigger + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    return start, end




ESPN_LEAGUES = [
    ("jpn.1", "Japan", "J1 League"),
    ("ksa.1", "Saudi-Arabia", "Pro League"),
    ("nor.1", "Norway", "Eliteserien"),
    ("fin.1", "Finland", "Veikkausliiga"),
    ("aus.1", "Australia", "A-League"),
    ("usa.1", "USA", "MLS"),
    ("eng.1", "England", "Premier League"),
    ("esp.1", "Spain", "La Liga"),
    ("esp.2", "Spain", "La Liga 2"),
    ("ita.1", "Italy", "Serie A"),
    ("ita.2", "Italy", "Serie B"),
    ("ger.1", "Germany", "Bundesliga"),
    ("ger.2", "Germany", "2. Bundesliga"),
    ("fra.1", "France", "Ligue 1"),
    ("ned.1", "Netherlands", "Eredivisie"),
    ("ned.2", "Netherlands", "Eerste Divisie"),
    ("por.1", "Portugal", "Primeira Liga"),
    ("bra.1", "Brazil", "Serie A"),
    ("bra.2", "Brazil", "Serie B"),
    ("arg.1", "Argentina", "Liga Profesional"),
    ("chi.1", "Chile", "Primera Division"),
    ("uru.1", "Uruguay", "Primera Division"),
    ("swe.2", "Sweden", "Superettan"),
    ("fin.2", "Finland", "Ykkosliiga"),
    ("rus.1", "Russia", "Premier League"),
    ("tha.1", "Thailand", "Thai League 1"),
    ("eng.2", "England", "Championship"),
    ("afc.champions", "Asia", "AFC Champions League Elite"),
    ("den.1", "Denmark", "Superliga"),
    ("fra.2", "France", "Ligue 2"),
]


def espn_slug_for(country: str, name: str) -> str | None:
    for slug, league_country, league_name in ESPN_LEAGUES:
        if norm(league_country) == norm(country) and norm(league_name) == norm(name):
            return slug
    return None


NAME_ALIASES = {
    "mineiro": "atletico mg",
    "atletico mineiro": "atletico mg",
    "m gladbach": "monchengladbach",
    "borussia m gladbach": "monchengladbach",
    "gladbach": "monchengladbach",
    "atleti": "atletico madrid",
    "psg": "paris saint germain",
    "lyon": "olympique lyonnais",
    "neftchi farg ona": "neftchi fergana",
    "copenhagen": "kobenhavn",
    "al ahli jeddah": "al ahli",
}


def fold_name(name: str) -> str:
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    for src, dst in (("ø", "o"), ("æ", "ae"), ("å", "a"), ("ö", "o"), ("ä", "a"), ("ü", "u")):
        text = text.replace(src, dst)
    text = re.sub(r"[''`´]", " ", text).replace("-", " ")
    text = squash_name(text)
    return NAME_ALIASES.get(text, text)


def name_score(query: str, candidate: str) -> int:
    left = fold_name(query)
    right = fold_name(candidate)
    if not left or not right:
        return 0
    if left == right or right.startswith(left) or left.startswith(right):
        return 100
    aliases = {"man": "manchester", "nottm": "nottingham", "utd": "united", "spurs": "tottenham"}
    stop = {"fc", "afc", "cf"}

    def expand(text: str) -> set[str]:
        tokens = {token for token in text.split() if token not in stop}
        expanded = set(tokens)
        for token in tokens:
            if token in aliases:
                expanded.add(aliases[token])
        return expanded

    query_tokens = expand(left)
    candidate_tokens = expand(right)
    overlap = query_tokens & candidate_tokens
    if query_tokens and query_tokens <= candidate_tokens:
        return 80
    if candidate_tokens and candidate_tokens <= query_tokens:
        return 70
    significant = overlap - set(aliases)
    if len(significant) >= 2 or (len(significant) == 1 and len(left.split()) == 1):
        return 60
    return 0


def espn_get(url: str, params: dict | None = None) -> dict:
    resp = requests.get(url, params=params or {}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def cached_json(name: str, loader, max_age_hours: float | None = None):
    path = ROOT / ".cache" / name
    if path.exists():
        fresh = True
        if max_age_hours is not None:
            fresh = time.time() - path.stat().st_mtime < max_age_hours * 3600
        if fresh:
            return json.loads(path.read_text(encoding="utf-8"))
    data = loader()
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return data


def espn_teams(slug: str) -> list[dict]:
    def load():
        body = espn_get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/teams")
        teams = body["sports"][0]["leagues"][0]["teams"]
        return [item["team"] for item in teams]

    return cached_json(f"espn-teams-{slug}.json", load)


def resolve_espn_team(slug: str, name: str) -> dict | None:
    best = None
    best_score = 0
    for team in espn_teams(slug):
        for field in ("shortDisplayName", "displayName", "name", "location", "abbreviation"):
            score = name_score(name, str(team.get(field) or ""))
            if score > best_score:
                best_score = score
                best = team
    if best_score >= 60:
        return best
    return None


def settle_asian(margin: int, line: float) -> str:
    def plain(part: float) -> str:
        diff = margin + part
        if diff > 0.01:
            return "全赢"
        if diff < -0.01:
            return "全输"
        return "走水"

    quarters = line * 4
    if abs(quarters - round(quarters)) < 1e-6 and int(round(quarters)) % 2 != 0:
        left = plain(line - 0.25)
        right = plain(line + 0.25)
        if left == right:
            return left
        pair = {left, right}
        if pair == {"全赢", "走水"}:
            return "赢半"
        if pair == {"全输", "走水"}:
            return "输半"
    return plain(line)


SHRINK_GAMES = 5.0
PRIOR_HOME_GOALS = 1.45
PRIOR_AWAY_GOALS = 1.15
MAX_GOALS = 12


def shrunk_rate(values: list[float], prior: float, weight: float = SHRINK_GAMES) -> float:
    """小样本往基准率收缩。样本越少，结果越接近 prior。"""
    if not values:
        return prior
    return (sum(values) + weight * prior) / (len(values) + weight)


def goal_expectation(home: dict, away: dict) -> tuple[float, float] | None:
    """用近况进球率估两队的期望进球，优先用主客场分开的样本。"""
    home_games = home.get("games") or []
    away_games = away.get("games") or []
    if len(home_games) < 4 or len(away_games) < 4:
        return None

    def rate(report: dict, key: str, venue: str, prior: float) -> float:
        games = report.get("games") or []
        subset = [float(game[key]) for game in games if game.get("venue") == venue]
        if len(subset) < 3:
            subset = [float(game[key]) for game in games]
        return shrunk_rate(subset, prior)

    home_scored = rate(home, "goals_for", "home", PRIOR_HOME_GOALS)
    away_conceded = rate(away, "goals_against", "away", PRIOR_HOME_GOALS)
    away_scored = rate(away, "goals_for", "away", PRIOR_AWAY_GOALS)
    home_conceded = rate(home, "goals_against", "home", PRIOR_AWAY_GOALS)
    lam_home = (home_scored + away_conceded) / 2
    lam_away = (away_scored + home_conceded) / 2
    return round(lam_home, 3), round(lam_away, 3)


TTG_TAIL_SPLIT = (0.45, 0.28, 0.16, 0.11)
MARKET_WEIGHT = 0.8
MODEL_TAG = "market-v2"


def devig(probs: dict[str, float]) -> dict[str, float]:
    """把带抽水的隐含概率归一化。"""
    total = sum(probs.values())
    if total <= 0:
        return {}
    return {key: value / total for key, value in probs.items()}


def ttg_pmf(odds: dict[str, float]) -> list[float] | None:
    """总入球盘去抽水后还原成 0..MAX_GOALS 的分布，7+ 按递减比例摊到后面几档。"""
    if not odds:
        return None
    implied = {str(key): 1.0 / value for key, value in odds.items() if value}
    if len(implied) < 5:
        return None
    fair = devig(implied)
    pmf = [0.0] * (MAX_GOALS + 1)
    for key, prob in fair.items():
        if key.endswith("+"):
            start = int(key[:-1])
            for offset, share in enumerate(TTG_TAIL_SPLIT):
                index = start + offset
                if index <= MAX_GOALS:
                    pmf[index] += prob * share
        else:
            try:
                index = int(key)
            except ValueError:
                continue
            if index <= MAX_GOALS:
                pmf[index] += prob
    total = sum(pmf)
    return [p / total for p in pmf] if total > 0 else None


def home_win_prob(lam_home: float, lam_away: float) -> float:
    ph = poisson_pmf(lam_home)
    pa = poisson_pmf(lam_away)
    prob = 0.0
    for i, hp in enumerate(ph):
        for j, ap in enumerate(pa):
            if i > j:
                prob += hp * ap
    return prob


def market_lambdas(card: dict) -> tuple[float, float] | None:
    """用马会总入球盘定总进球，用主客和盘定主客分配，反推市场隐含的期望进球。"""
    markets = (card or {}).get("markets") or {}
    pmf = ttg_pmf(markets.get("ttg") or {})
    if pmf is None:
        return None
    lam_total = sum(index * prob for index, prob in enumerate(pmf))
    if lam_total <= 0.2:
        return None
    had = markets.get("had") or {}
    fair = devig({key: 1.0 / value for key, value in had.items() if value})
    target_home = fair.get("H")
    if target_home is None:
        return round(lam_total / 2, 3), round(lam_total / 2, 3)
    low, high = 0.05, lam_total - 0.05
    if high <= low:
        return None
    # 主胜概率随主队期望进球单调递增，二分即可。
    for _ in range(32):
        mid = (low + high) / 2
        if home_win_prob(mid, lam_total - mid) < target_home:
            low = mid
        else:
            high = mid
    lam_home = (low + high) / 2
    return round(lam_home, 3), round(lam_total - lam_home, 3)


def blended_lambdas(card: dict, home_report: dict, away_report: dict) -> tuple[float, float] | None:
    """市场锚定为主、近况为辅，避免只用 6 场样本就给出过度自信的概率。"""
    form = goal_expectation(home_report, away_report)
    market = market_lambdas(card)
    if market and form:
        return (
            round(MARKET_WEIGHT * market[0] + (1 - MARKET_WEIGHT) * form[0], 3),
            round(MARKET_WEIGHT * market[1] + (1 - MARKET_WEIGHT) * form[1], 3),
        )
    return market or form


def poisson_pmf(lam: float, top: int = MAX_GOALS) -> list[float]:
    values = [math.exp(-lam)]
    for k in range(1, top + 1):
        values.append(values[-1] * lam / k)
    return values


def split_outcome(line: float) -> list[float]:
    """半一、一球半这类盘拆成两注，各占一半。"""
    quarters = int(round(line * 4))
    if quarters % 2:
        return [line - 0.25, line + 0.25]
    return [line]


def asian_ev(win: float, push: float, odds: float) -> float:
    return win * (odds - 1) - (1 - win - push)





def corner_lambda(home: dict, away: dict) -> float | None:
    totals = [
        float(game["corner_total"])
        for report in (home, away)
        for game in (report.get("games") or [])
        if game.get("corner_total") is not None
    ]
    if len(totals) < 6:
        return None
    return round(shrunk_rate(totals, 10.0), 3)



























def quarter_label(value: float) -> str:
    quarters = int(round(abs(value) * 4))
    if quarters % 2:
        left = (quarters - 1) / 4
        right = (quarters + 1) / 4
        return f"{left:g}/{right:g}"
    return f"{quarters / 4:g}"


def goal_number(raw) -> int | None:
    if isinstance(raw, dict):
        raw = raw.get("displayValue") if raw.get("displayValue") is not None else raw.get("value")
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def recent_goal_games(slug: str, team: dict, before: datetime) -> list[dict]:
    team_id = str(team["id"])

    def load():
        body = espn_get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/teams/{team_id}/schedule")
        rows = []
        for event in body.get("events") or []:
            competition = (event.get("competitions") or [{}])[0]
            status = ((competition.get("status") or {}).get("type") or {}).get("name") or ""
            if status not in ("STATUS_FULL_TIME", "STATUS_FINAL", "STATUS_FINAL_AET", "STATUS_FINAL_PEN"):
                continue
            if "friendly" in (event.get("name") or "").lower():
                continue
            sides = []
            for item in competition.get("competitors") or []:
                team_info = item.get("team") or {}
                sides.append({
                    "id": str(team_info.get("id") or item.get("id") or ""),
                    "name": team_info.get("displayName") or "",
                    "score": goal_number(item.get("score")),
                    "home_away": item.get("homeAway") or "",
                })
            rows.append({"date": event.get("date"), "sides": sides})
        return rows

    run_day = datetime.now(TZ).strftime("%Y-%m-%d")
    try:
        schedule = cached_json(f"espn-goals-{slug}-{team_id}-{run_day}.json", load, max_age_hours=12)
    except requests.RequestException:
        return []
    games = []
    for event in schedule:
        if not event.get("date"):
            continue
        when = kickoff_local(event["date"])
        if when >= before:
            continue
        own = next((item for item in event["sides"] if item["id"] == team_id), None)
        opponent = next((item for item in event["sides"] if item["id"] != team_id), None)
        if own is None or opponent is None or own["score"] is None or opponent["score"] is None:
            continue
        margin = own["score"] - opponent["score"]
        if margin > 0:
            outcome = "胜"
        elif margin < 0:
            outcome = "负"
        else:
            outcome = "平"
        games.append({
            "date": when.strftime("%m-%d"),
            "sort": event["date"],
            "opponent": opponent["name"],
            "goals_for": own["score"],
            "goals_against": opponent["score"],
            "total": own["score"] + opponent["score"],
            "margin": margin,
            "outcome": outcome,
            "venue": own.get("home_away") or "",
            "slug": slug,
        })
    games.sort(key=lambda item: item["sort"], reverse=True)
    return games


def extra_slugs(league_en: str, name: str = "") -> list[str]:
    """国内杯赛/欧战的近况也算进球样本，联赛本身由调用方传入。"""
    slugs = []
    if league_en == "Primeira Liga":
        slugs.extend(["por.1", "uefa.europa", "uefa.champions"])
    if league_en == "Ligue 1":
        slugs.extend(["fra.1", "uefa.champions", "uefa.europa"])
    if league_en == "Superliga":
        slugs.extend(["uefa.champions", "uefa.europa"])
    return slugs


def team_goal_profile(slug: str, name: str, before: datetime, league_en: str) -> dict:
    slugs = []
    for item in [slug, *extra_slugs(league_en, name)]:
        if item and item not in slugs:
            slugs.append(item)
    games = []
    seen = set()
    matched = False
    for item in slugs:
        try:
            team = resolve_espn_team(item, name)
        except requests.RequestException:
            continue
        if team is None:
            continue
        matched = True
        for game in recent_goal_games(item, team, before):
            key = (game["sort"], fold_name(game["opponent"]))
            if key in seen:
                continue
            seen.add(key)
            game["same"] = item == slug
            games.append(game)
    games.sort(key=lambda item: item["sort"], reverse=True)
    return {
        "name": name,
        "games": games,
        "same": [game for game in games if game["same"]],
        "missing": None if matched else f"{name}没有对上公开赛程",
    }








def parse_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 模型偶尔把 JSON 截断，尽量抢救出完整的部分，不要让整封邮件失败。
        cut = text.rfind("}")
        while cut > 0:
            attempt = text[:cut + 1]
            for suffix in ("", "]}", "}]}", "]}", "}"):
                try:
                    return json.loads(attempt + suffix)
                except json.JSONDecodeError:
                    continue
            cut = text.rfind("}", 0, cut)
        raise


def llm(messages: list[dict]) -> dict:
    url = LLM_BASE_URL + "/chat/completions"
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": LLM_MODEL,
        "temperature": 0.2,
        "max_tokens": 4096,
        "messages": messages,
        "response_format": {"type": "json_object"},
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=180)
    if resp.status_code >= 400:
        payload.pop("response_format", None)
        resp = requests.post(url, headers=headers, json=payload, timeout=180)
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return parse_json(content)


# ---------------------------------------------------------------------------
# 概率层
#
# 这里不再有「让球方一定赢」「强队一定打穿」这类人为规则。
# 每个可下注方向都只有一个来源：由市场盘口反推的进球分布（可选叠加近况），
# 然后算命中率 win、走水 push 和期望 edge。
# 公式：概率 = P(净胜球跨过盘口)，期望 = win×(赔率-1) − (1 − win − push)。
# ---------------------------------------------------------------------------

SCREEN_MIN_ODDS = float(os.environ.get("SCREEN_MIN_ODDS", "1.25"))
SCREEN_MIN_PROB = float(os.environ.get("SCREEN_MIN_PROB", "0.55"))
SCREEN_POOL = int(os.environ.get("SCREEN_POOL", "6"))
SHOW_LIMIT = int(os.environ.get("SHOW_LIMIT", "12"))


def card_matches(day: str, notes: list[str]) -> list[dict]:
    """窗口内所有未开赛的马会比赛，只做盘口整理，不做取舍。"""
    start, end = slate_window(day)
    now = datetime.now(TZ)
    try:
        card = hkjc_card()
    except Exception as exc:  # 取不到盘口就让上游给出空结果，别整封邮件消失
        notes.append(f"马会盘口没取到：{exc}")
        return []
    out = []
    for item in card:
        if not item.get("kickoff") or item.get("status") != "PREEVENT":
            continue
        try:
            when = kickoff_local(item["kickoff"])
        except ValueError:
            continue
        if not (start <= when < end) or when < now:
            continue
        if is_friendly(item["tournament_en"]) or is_women_or_youth(item["home"]) or is_women_or_youth(item["away"]):
            continue
        country, league = HKJC_LEAGUES.get(item["tournament_en"], ("", item["tournament_en"]))
        out.append({
            "no": item["no"],
            "when": when,
            "kickoff": when.strftime("%m-%d %H:%M"),
            "league": league,
            "league_en": league,
            "tournament": item["tournament_en"],
            "country": country,
            "home": item["home"],
            "away": item["away"],
            "home_ch": item["home_ch"],
            "away_ch": item["away_ch"],
            "card": item,
            "home_report": None,
            "away_report": None,
        })
    out.sort(key=lambda match: match["when"])
    return out


def goal_distribution(lam: tuple[float, float] | None):
    """(主队期望进球, 客队期望进球) -> (净胜球分布, 总进球分布)。"""
    if not lam or lam[0] <= 0 or lam[1] <= 0:
        return None, None
    home = poisson_pmf(lam[0])
    away = poisson_pmf(lam[1])
    margins: dict[int, float] = {}
    totals: dict[int, float] = {}
    for home_goals, home_prob in enumerate(home):
        if home_prob < 1e-12:
            continue
        for away_goals, away_prob in enumerate(away):
            prob = home_prob * away_prob
            if prob < 1e-12:
                continue
            margins[home_goals - away_goals] = margins.get(home_goals - away_goals, 0.0) + prob
            totals[home_goals + away_goals] = totals.get(home_goals + away_goals, 0.0) + prob
    return margins, totals


def corner_distribution(home_report: dict | None, away_report: dict | None) -> dict[int, float] | None:
    if not home_report or not away_report:
        return None
    lam = corner_lambda(home_report, away_report)
    if not lam or lam <= 0:
        return None
    return {index: prob for index, prob in enumerate(poisson_pmf(lam))}


def flip(dist: dict[int, float]) -> dict[int, float]:
    """换一个方向看同一个分布（客队视角、小球视角）。"""
    return {-key: value for key, value in dist.items()}


def asian_view(dist: dict[int, float], line: float) -> tuple[float, float]:
    """dist 是押注方视角的净胜球（或总进球）分布，line 是让出的球，负数为让。

    和 settle_asian 用同一个式子：value + line 与 0 比较，所以门槛是 -line。
    """
    if not dist:
        return 0.0, 0.0
    parts = split_outcome(line)
    share = 1.0 / len(parts)
    win = push = 0.0
    for value, prob in dist.items():
        for part in parts:
            threshold = -part
            if value > threshold + 1e-9:
                win += prob * share
            elif abs(value - threshold) <= 1e-9:
                push += prob * share
    return win, push


def match_meta(match: dict) -> dict:
    return {
        "no": match["no"],
        "kickoff": match["when"].isoformat(timespec="seconds"),
        "league": match["league"],
        "league_en": match["league_en"],
        "country": match["country"],
        "home": match["home"],
        "away": match["away"],
    }


def quote(match: dict, market: str, side: str, line_text: str, win: float, push: float,
          odds: float, meta: dict) -> dict:
    return {
        "match": f"{match['kickoff']} {match['home_ch']} vs {match['away_ch']}",
        "market": market,
        "side": side,
        "line": line_text,
        "odds": odds,
        "prob": round(win, 4),
        "push": round(push, 4),
        "edge": round(asian_ev(win, push, odds), 4),
        "meta": meta,
    }


def market_quotes(match: dict, lam: tuple[float, float] | None) -> list[dict]:
    """一场比赛里马会开出的每个方向，全部按分布算成候选。"""
    card = match["card"]
    prices = card.get("prices") or {}
    margins, totals = goal_distribution(lam)
    if not margins:
        return []
    corners = corner_distribution(match.get("home_report"), match.get("away_report"))
    out = []

    home_line = card.get("home_line")
    if home_line is not None and abs(home_line) > 1e-6:
        depth = abs(home_line)
        favorite_home = home_line < 0
        for row in prices.get("HDC") or []:
            odds = row.get("odds")
            if not odds or odds < SCREEN_MIN_ODDS:
                continue
            back = "home" if row.get("str") == "H" else "away"
            favorite = (back == "home") == favorite_home
            dist = margins if back == "home" else flip(margins)
            win, push = asian_view(dist, -depth if favorite else depth)
            label = quarter_label(depth)
            meta = match_meta(match)
            meta.update({
                "kind": "handicap",
                "back": back,
                "depth": depth,
                "is_favorite": favorite,
                "favorite": "home" if favorite_home else "away",
            })
            side = f"{'主队' if back == 'home' else '客队'}{'让' if favorite else '受让'}{label}"
            out.append(quote(match, "让球", side, label, win, push, odds, meta))

    total_line = card.get("total")
    if total_line:
        for row in prices.get("HIL") or []:
            odds = row.get("odds")
            if not odds or odds < SCREEN_MIN_ODDS:
                continue
            over = row.get("str") == "H"
            win, push = asian_view(totals if over else flip(totals),
                                   -total_line if over else total_line)
            meta = match_meta(match)
            meta.update({"kind": "total", "direction": "over" if over else "under",
                         "total_value": total_line, "line_value": total_line})
            label = f"{total_line:g}"
            out.append(quote(match, "大球" if over else "小球", "大球" if over else "小球",
                             label, win, push, odds, meta))

    corner_line = card.get("corner_line")
    if corner_line and corners:
        for row in prices.get("CHL") or []:
            odds = row.get("odds")
            if not odds or odds < SCREEN_MIN_ODDS:
                continue
            over = row.get("str") == "H"
            win, push = asian_view(corners if over else flip(corners),
                                   -corner_line if over else corner_line)
            meta = match_meta(match)
            meta.update({"kind": "corner", "direction": "over" if over else "under",
                         "corner_value": corner_line, "line_value": corner_line})
            label = f"{corner_line:g}"
            out.append(quote(match, "大角" if over else "小角", "大角" if over else "小角",
                             label, win, push, odds, meta))
    return out


def team_reports(match: dict) -> tuple[dict, dict] | None:
    slug = espn_slug_for(match["country"], match["league_en"])
    if not slug:
        return None
    try:
        home = team_goal_profile(slug, match["home"], match["when"], match["league_en"])
        away = team_goal_profile(slug, match["away"], match["when"], match["league_en"])
    except requests.RequestException:
        return None
    if home.get("missing") or away.get("missing"):
        return None
    match["home_report"] = home
    match["away_report"] = away
    return home, away


def form_note(market_lam, form_lam, blend) -> str:
    bits = [f"市场期望进球 {market_lam[0]:.2f}-{market_lam[1]:.2f}"]
    if form_lam:
        bits.append(f"近况 {form_lam[0]:.2f}-{form_lam[1]:.2f}")
    bits.append(f"融合 {blend[0]:.2f}-{blend[1]:.2f}")
    return "，".join(bits)


def screen(day: str, notes: list[str]) -> list[dict]:
    """全盘口扫描：先按纯市场分布排，再给概率最高的几场补近况重算。"""
    matches = card_matches(day, notes)
    if not matches:
        notes.append("窗口内没有可分析的比赛。")
        return []
    quotes: list[dict] = []
    no_market = []
    for match in matches:
        lam = market_lambdas(match["card"])
        if not lam:
            no_market.append(match)
            continue
        match["lam_market"] = lam
        for pick in market_quotes(match, lam):
            pick["prob_market"] = pick["prob"]
            quotes.append(pick)

    notes.append(
        f"窗口内 {len(matches)} 场：{len(matches) - len(no_market)} 场能反推期望进球，"
        f"摊平出 {len(quotes)} 个可下方向。"
    )
    if no_market:
        names = "、".join(f"{item['home_ch']}vs{item['away_ch']}" for item in no_market[:3])
        notes.append(f"{len(no_market)} 场没有总入球盘/HAD，算不出分布，已跳过：{names}。")

    by_match: dict[str, list[dict]] = {}
    for pick in quotes:
        by_match.setdefault(pick["meta"]["no"], []).append(pick)
    order = sorted(
        (match for match in matches if match["no"] in by_match),
        key=lambda match: max(pick["prob"] for pick in by_match[match["no"]]),
        reverse=True,
    )
    for match in order[:SCREEN_POOL]:
        reports = team_reports(match)
        if not reports:
            continue
        blend = blended_lambdas(match["card"], reports[0], reports[1])
        form_lam = goal_expectation(reports[0], reports[1])
        if not blend:
            continue
        note = form_note(match["lam_market"], form_lam, blend)
        fresh = {(pick["market"], pick["side"], pick["line"]): pick
                 for pick in market_quotes(match, blend)}
        form_prob = {(pick["market"], pick["side"], pick["line"]): pick["prob"]
                     for pick in market_quotes(match, form_lam)} if form_lam else {}
        for index, old in enumerate(by_match[match["no"]]):
            key = (old["market"], old["side"], old["line"])
            new = fresh.get(key)
            if new is None:
                continue
            new["prob_market"] = old["prob"]
            new["prob_form"] = form_prob.get(key)
            new["reason"] = note
            by_match[match["no"]][index] = new

    pool = [pick for picks in by_match.values() for pick in picks]
    for pick in pool:
        if not pick.get("reason"):
            match = next((item for item in matches if item["no"] == pick["meta"]["no"]), None)
            lam = (match or {}).get("lam_market")
            pick["reason"] = form_note(lam, None, lam) if lam else ""
    pool.sort(key=lambda pick: (pick["prob"], pick["edge"]), reverse=True)
    return pool


def ranked_all(picks: list[dict], stats: dict | None = None) -> list[dict]:
    return sorted(picks, key=lambda pick: ((pick.get("prob") or 0), (pick.get("edge") or 0)), reverse=True)


def rule_top3(picks: list[dict], stats: dict | None = None) -> list[dict]:
    return distinct_matches(ranked_all(picks), 3)


HISTORY_FILE = ROOT / "picks-history.jsonl"
REVIEW_DAYS = 7


def parse_kickoff(text: str) -> datetime:
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    rows = []
    for raw in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def save_history(rows: list[dict]) -> None:
    text = "\n".join(json.dumps(row, ensure_ascii=False) for row in rows)
    HISTORY_FILE.write_text(text + ("\n" if text else ""), encoding="utf-8")


def history_row(day: str, pick: dict, kind: str) -> dict | None:
    meta = pick.get("meta")
    if not meta or not meta.get("no"):
        return None
    return {
        "run_day": day,
        "no": meta["no"],
        "kickoff": meta.get("kickoff"),
        "league": meta.get("league"),
        "league_en": meta.get("league_en"),
        "country": meta.get("country"),
        "home": meta.get("home"),
        "away": meta.get("away"),
        "market": pick.get("market"),
        "side": pick.get("side"),
        "line": pick.get("line"),
        "depth": meta.get("depth"),
        "favorite": meta.get("favorite"),
        "back": meta.get("back"),
        "is_favorite": meta.get("is_favorite"),
        "direction": meta.get("direction"),
        "line_value": meta.get("line_value"),
        "total_value": meta.get("total_value"),
        "corner_value": meta.get("corner_value"),
        "kind": kind,
        "scope": meta.get("scope"),
        "model": MODEL_TAG,
        "confidence": pick.get("confidence"),
        "odds": pick.get("odds"),
        "prob": pick.get("prob"),
        "edge": pick.get("edge"),
        "prob_market": pick.get("prob_market"),
        "prob_form": pick.get("prob_form"),
        "reason": pick.get("reason"),
        "settlement": None,
        "home_goals": None,
        "away_goals": None,
        "corner_total": None,
        "settled_day": None,
        "settle_attempts": 0,
    }


REFRESH_FIELDS = ("kind", "scope", "model", "odds", "prob", "edge", "prob_market", "prob_form",
                  "confidence", "reason",
                  "depth", "favorite", "back", "is_favorite", "direction", "line_value",
                  "total_value", "corner_value", "league", "league_en", "country")


def record_history(day: str, picks: list[dict]) -> tuple[int, int]:
    """返回 (新增条数, 补全条数)。已有的行要补上新算出的赔率和概率，否则校准永远没样本。"""
    rows = load_history()
    index = {
        (row.get("run_day"), row.get("no"), row.get("market"), row.get("side"), row.get("line")): row
        for row in rows
    }
    added = 0
    refreshed = 0
    for pick in picks:
        meta = pick.get("meta") or {}
        row = history_row(day, pick, meta.get("kind") or pick.get("market") or "")
        if row is None:
            continue
        key = (day, row["no"], row["market"], row["side"], row["line"])
        existing = index.get(key)
        if existing is None:
            rows.append(row)
            index[key] = row
            added += 1
            continue
        changed = False
        for field in REFRESH_FIELDS:
            new_value = row.get(field)
            if new_value is None or existing.get(field) == new_value:
                continue
            existing[field] = new_value
            changed = True
        if changed:
            refreshed += 1
    if added or refreshed:
        save_history(rows)
    return added, refreshed


def espn_day_results(slug: str, day: str) -> list[dict]:
    def load():
        body = espn_get(
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard",
            {"dates": day.replace("-", "")},
        )
        rows = []
        for event in body.get("events") or []:
            competition = (event.get("competitions") or [{}])[0]
            status = ((competition.get("status") or {}).get("type") or {}).get("name") or ""
            sides = {}
            for item in competition.get("competitors") or []:
                sides[item.get("homeAway")] = {
                    "id": str((item.get("team") or {}).get("id") or ""),
                    "name": (item.get("team") or {}).get("displayName") or "",
                    "score": goal_number(item.get("score")),
                }
            rows.append({
                "id": str(event.get("id") or ""),
                "status": status,
                "home": sides.get("home") or {},
                "away": sides.get("away") or {},
            })
        return rows

    return cached_json(f"espn-results-{slug}-{day}.json", load, max_age_hours=6)


def find_result(row: dict) -> dict | None:
    slug = espn_slug_for(row.get("country") or "", row.get("league_en") or "")
    if slug is None:
        # 老记录里存的是马会原始赛事名，反查一次映射表。
        mapped = HKJC_LEAGUES.get(row.get("league_en") or "")
        if mapped:
            slug = espn_slug_for(mapped[0], mapped[1])
    if slug is None or not row.get("kickoff"):
        return None
    kick = datetime.fromisoformat(row["kickoff"])
    days = list(dict.fromkeys((
        kick.astimezone(timezone.utc).strftime("%Y-%m-%d"),
        kick.astimezone(TZ).strftime("%Y-%m-%d"),
    )))
    scored = []
    for day in days:
        try:
            events = espn_day_results(slug, day)
        except requests.RequestException:
            continue
        for event in events:
            if event["status"] not in ("STATUS_FULL_TIME", "STATUS_FINAL", "STATUS_FINAL_AET", "STATUS_FINAL_PEN"):
                continue
            home_score = name_score(row.get("home") or "", event["home"].get("name") or "")
            away_score = name_score(row.get("away") or "", event["away"].get("name") or "")
            # 阈值太低会把同城或同前缀球队配错，一旦落库就不再重算。
            if home_score < 75 or away_score < 75:
                continue
            if event["home"].get("score") is None or event["away"].get("score") is None:
                continue
            scored.append((home_score + away_score, event))
    if not scored:
        return None
    scored.sort(key=lambda item: item[0], reverse=True)
    best_total, best = scored[0]
    # 第二名太接近说明匹配有歧义，宁可先不结算，也不写错比分。
    if len(scored) > 1 and best_total - scored[1][0] < 12:
        return None
    return {"slug": slug, **best}


def event_corners(slug: str, event_id: str) -> dict:
    def load():
        body = espn_get(
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/summary",
            {"event": event_id},
        )
        corners = {}
        for team in (body.get("boxscore") or {}).get("teams") or []:
            team_id = str((team.get("team") or {}).get("id") or "")
            for stat in team.get("statistics") or []:
                if stat.get("name") == "wonCorners" and str(stat.get("displayValue") or "").isdigit():
                    corners[team_id] = int(stat["displayValue"])
        return {"corners": corners}

    try:
        data = cached_json(f"espn-corners2-{event_id}.json", load)
    except requests.RequestException:
        return {}
    return data.get("corners") or {}


def settle_row(row: dict, result: dict, corners: dict) -> str | None:
    home_goals = result["home"].get("score")
    away_goals = result["away"].get("score")
    if home_goals is None or away_goals is None:
        return None
    if row.get("kind") == "double_chance":
        # 主队不败 / 客队不败：押的是主客和盘的两个结果，平局算赢，没有走水。
        scope = row.get("scope")
        if scope == "home":
            return "全赢" if home_goals >= away_goals else "全输"
        if scope == "away":
            return "全赢" if away_goals >= home_goals else "全输"
        return None
    market = row.get("market") or ""
    if market == "让球":
        depth = row.get("depth")
        if depth is None:
            return None
        back = row.get("back")
        if back not in ("home", "away"):
            favorite = row.get("favorite")
            if favorite not in ("home", "away"):
                return None
            back = favorite
        is_favorite = row.get("is_favorite")
        if is_favorite is None:
            is_favorite = back == row.get("favorite")
        margin = (home_goals - away_goals) if back == "home" else (away_goals - home_goals)
        # 受让方向就是把盘口翻到另一边，统一成一个公式。
        return settle_asian(margin, -float(depth) if is_favorite else float(depth))
    if market in ("大球", "小球"):
        line = row.get("total_value")
        if line is None:
            return None
        total = home_goals + away_goals
        under = market == "小球" or row.get("direction") == "under"
        return settle_asian(-total if under else total, line if under else -float(line))
    if market in ("大角", "小角"):
        line = row.get("corner_value")
        values = [int(value) for value in (corners or {}).values()]
        if line is None or len(values) != 2:
            return None
        total = sum(values)
        under = market == "小角" or row.get("direction") == "under"
        return settle_asian(-total if under else total, line if under else -float(line))
    return None


def settle_history(rows: list[dict], day: str) -> tuple[int, int]:
    """返回 (结算成功数, 仍在等结果的场次数)。"""
    horizon = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ) - timedelta(days=REVIEW_DAYS + 2)
    now = datetime.now(TZ)
    changed = 0
    pending = 0
    corner_budget = 12
    for row in rows:
        if row.get("settlement") or not row.get("kickoff"):
            continue
        when = parse_kickoff(row["kickoff"])
        if when >= now - timedelta(hours=2) or when < horizon:
            continue
        if int(row.get("settle_attempts") or 0) >= 3:
            pending += 1
            continue
        try:
            result = find_result(row)
        except requests.RequestException:
            continue
        if result is None:
            row["settle_attempts"] = int(row.get("settle_attempts") or 0) + 1
            pending += 1
            continue
        corners = {}
        need_corners = row.get("market") in ("大角", "小角") or (
            row.get("kind") == "observe" and row.get("corner_value") is not None and corner_budget > 0
        )
        if need_corners:
            corners = event_corners(result["slug"], result["id"])
            corner_budget -= 1
        row["home_goals"] = result["home"]["score"]
        row["away_goals"] = result["away"]["score"]
        values = [int(value) for value in (corners or {}).values()]
        if len(values) == 2:
            row["corner_total"] = sum(values)
        row["settled_day"] = day
        if row.get("kind") == "observe":
            row["settlement"] = "已观察"
            changed += 1
            continue
        settlement = settle_row(row, result, corners)
        if settlement is None:
            row["settle_attempts"] = int(row.get("settle_attempts") or 0) + 1
            pending += 1
            continue
        row["settlement"] = settlement
        changed += 1
    return changed, pending


def weekly_review(day: str, rows: list[dict]) -> tuple[list[dict], dict[str, dict[str, int]]]:
    horizon = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ) - timedelta(days=REVIEW_DAYS)
    recent = [
        row for row in rows
        if row.get("settlement") and row.get("kind") != "observe"
        and row.get("kickoff") and parse_kickoff(row["kickoff"]) >= horizon
    ]
    recent.sort(key=lambda row: row["kickoff"])
    stats: dict[str, dict[str, int]] = {}
    for row in recent:
        bag = stats.setdefault(row.get("market") or "", {})
        bag[row["settlement"]] = bag.get(row["settlement"], 0) + 1
    return recent, stats


def brier(rows: list[dict], field: str) -> float | None:
    graded = [row for row in rows if row.get(field) is not None]
    if not graded:
        return None
    return sum(
        (float(row[field]) - (1.0 if row["settlement"] == "全赢" else 0.0)) ** 2
        for row in graded
    ) / len(graded)


def calibration_report(day: str, rows: list[dict]) -> list[str]:
    """用已结算场次比较「市场口径 / 近况口径 / 融合口径」谁更准，这是唯一可验证的准确率口径。"""
    horizon = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ) - timedelta(days=REVIEW_DAYS * 4)
    graded = [
        row for row in rows
        if row.get("prob") is not None
        and row.get("settlement") in ("全赢", "全输")
        and row.get("kickoff") and parse_kickoff(row["kickoff"]) >= horizon
    ]
    if len(graded) < 3:
        return []
    predicted = sum(float(row["prob"]) for row in graded) / len(graded)
    actual = sum(1 for row in graded if row["settlement"] == "全赢") / len(graded)
    lines = [f"概率校准：{len(graded)} 场，平均预测 {predicted:.0%}，实际命中 {actual:.0%}。"]
    if predicted - actual > 0.08:
        lines.append("预测偏高，模型太乐观，注码要压小。")
    elif actual - predicted > 0.08:
        lines.append("实际命中高于预测，模型偏保守。")
    for label, field in (("融合", "prob"), ("市场", "prob_market"), ("近况", "prob_form")):
        score = brier(graded, field)
        if score is not None:
            lines.append(f"- {label}口径 Brier {score:.3f}（越小越准）")
    market_score = brier(graded, "prob_market")
    form_score = brier(graded, "prob_form")
    if market_score is not None and form_score is not None:
        gap = market_score - form_score
        if gap > 0.01:
            lines.append("近况口径更准，可以调高近况权重。")
        elif gap < -0.01:
            lines.append("市场口径更准，近况权重不宜再调高。")
        else:
            lines.append("两种口径差不多，继续以市场为锚。")
    return lines


def tab_text(row: dict) -> str:
    score = ""
    if row.get("home_goals") is not None:
        score = f"{row['home_goals']}-{row['away_goals']}"
    corner = ""
    if row.get("market") in ("大角", "小角") and row.get("corner_total") is not None:
        corner = f" 角球{row['corner_total']}"
    date = (row.get("kickoff") or "")[5:10]
    return (
        f"{date} {row.get('league')} {row.get('side')} {row.get('line')}"
        f"｜{score}{corner}｜{row.get('settlement')}"
    )


def bag_line(market: str, bag: dict[str, int]) -> str:
    parts = [f"{key}{bag[key]}" for key in ("全赢", "赢半", "走水", "输半", "全输") if bag.get(key)]
    return f"{market}：{'，'.join(parts) if parts else '没有结算'}"


def review_lines(day: str, recent: list[dict], stats: dict[str, dict[str, int]], pending: int = 0) -> list[str]:
    horizon = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ) - timedelta(days=REVIEW_DAYS)
    label = f"{horizon:%m-%d} 至 {day[5:]}"
    if not recent:
        lines = [f"过去 {REVIEW_DAYS} 天（{label}）没有已结算的推荐，今天只按模型命中率排。"]
        if pending:
            lines.append(f"- 另有 {pending} 场还没对上 ESPN 赛果，复盘样本偏少。")
        return lines
    wins = sum(bag.get("全赢", 0) + bag.get("赢半", 0) for bag in stats.values())
    losses = sum(bag.get("全输", 0) + bag.get("输半", 0) for bag in stats.values())
    lines = [f"过去 {REVIEW_DAYS} 天（{label}）已结算 {len(recent)} 场：赢 {wins}，输 {losses}。"]
    for market in ("让球", "大球", "大角"):
        if stats.get(market):
            lines.append("- " + bag_line(market, stats[market]))
    lines.extend("- " + tab_text(row) for row in recent[-8:])
    if pending:
        lines.append(f"- 另有 {pending} 场没对上 ESPN 赛果，没算进胜率。")
    return lines


def weekly_payload(recent: list[dict]) -> list[dict]:
    return [
        {
            "date": (row.get("kickoff") or "")[:10],
            "league": row.get("league"),
            "market": row.get("market"),
            "side": row.get("side"),
            "line": row.get("line"),
            "score": f"{row.get('home_goals')}-{row.get('away_goals')}",
            "corner_total": row.get("corner_total"),
            "settlement": row.get("settlement"),
        }
        for row in recent[-20:]
    ]


def pick_payload(picks: list[dict]) -> list[dict]:
    return [
        {
            "index": index,
            "match": pick.get("match"),
            "market": pick.get("market"),
            "side": pick.get("side"),
            "line": pick.get("line"),
            "confidence": pick.get("confidence"),
            "odds": pick.get("odds"),
            "model_prob": pick.get("prob"),
            "edge": pick.get("edge"),
            "stake": stake_hint(pick),
            "reason": (pick.get("reason") or "")[:80],
        }
        for index, pick in enumerate(picks, 1)
    ]


def stake_hint(pick: dict) -> str:
    edge = pick.get("edge")
    if edge is None:
        return "没赔率"
    if edge >= 0.03:
        return "正常注"
    if edge >= -0.03:
        return "最小注"
    return "不下"


def edge_text(pick: dict) -> str:
    parts = []
    if pick.get("odds") is not None:
        parts.append(f"赔率{pick['odds']:g}")
    if pick.get("prob") is not None:
        parts.append(f"模型{pick['prob']:.0%}")
    if pick.get("edge") is not None:
        parts.append(f"期望{pick['edge']:+.2f}")
    return ("｜" + " ".join(parts)) if parts else ""





def match_key(pick: dict) -> str:
    meta = pick.get("meta") or {}
    return str(meta.get("no") or pick.get("match") or "")


def distinct_matches(ranked: list[dict], limit: int = 3, skip: set[str] | None = None) -> list[dict]:
    """同一场只留一条，避免让球和大球同时下注放大相关性。"""
    skip = skip or set()
    out = []
    seen = set(skip)
    for pick in ranked:
        key = match_key(pick)
        if key in seen:
            continue
        seen.add(key)
        out.append(pick)
        if len(out) == limit:
            break
    return out


def ai_top3(day: str, picks: list[dict], recent: list[dict], stats: dict[str, dict[str, int]], notes: list[str]) -> tuple[list[dict], list[str]]:
    if not picks:
        return [], []
    if not LLM_API_KEY:
        notes.append("没有 LLM_API_KEY，前三直接按模型命中率排，没有复盘。")
        return rule_top3(picks, stats), []
    payload = {
        "day": day,
        "recent_settled": weekly_payload(recent),
        "today_candidates": pick_payload(picks),
    }
    try:
        result = llm([
            {
                "role": "system",
                "content": (
                    "你是足球让球、大球、大角的复盘助手。只能使用用户给出的已结算记录和今日候选，"
                    "禁止编造比分、盘口、角球、赔率或球队。不能新增候选，不能修改玩法、方向、盘口，只能排序并说明理由。"
                    "候选里的 model_prob 是用马会主客和盘与总入球盘校准后的命中率，是排序的首要依据；"
                    "edge 是「命中率×赔率-1」，只用来判断值不值得下注。"
                    "优先挑 model_prob 高的方向，edge 明显为负的要说清为什么仍然保留。"
                    "输出 JSON：{\"lessons\":[最多3条中文经验],"
                    "\"top\":[{\"index\":候选序号,\"reason\":中文理由}],"
                    "\"drop\":[{\"index\":候选序号,\"why\":中文原因}]}。top 最多 3 个，按命中率从高到低。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ])
    except Exception as exc:
        notes.append(f"模型复盘失败，前三直接按模型命中率排：{exc}")
        return rule_top3(picks, stats), []
    lessons = [str(item).strip() for item in (result.get("lessons") or []) if str(item).strip()][:3]
    chosen = []
    used = set()
    for item in result.get("top") or []:
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if index < 1 or index > len(picks) or index in used:
            continue
        used.add(index)
        pick = dict(picks[index - 1])
        pick["top_reason"] = str(item.get("reason") or "").strip()
        chosen.append(pick)
        if len(chosen) == 3:
            break
    if not chosen:
        notes.append("模型没有给出可用的前三，直接按模型命中率排。")
        return rule_top3(picks, stats), lessons
    deduped = []
    seen_matches = set()
    for pick in chosen:
        key = match_key(pick)
        if key in seen_matches:
            notes.append(f"同场只留一注，去掉重复的 {pick.get('match')} {pick.get('market')}。")
            continue
        seen_matches.add(key)
        deduped.append(pick)
    chosen = deduped
    if len(chosen) < 3:
        for pick in distinct_matches(ranked_all(picks, stats), 3, skip=seen_matches):
            if len(chosen) == 3:
                break
            fill = dict(pick)
            if fill.get("prob") is not None:
                reason = (
                    f"市场校准命中率{fill['prob']:.0%}，赔率{fill['odds']:g}，期望{fill['edge']:+.2f}。"
                )
            else:
                reason = "没有可用的盘口概率，只按赔率排序。"
            fill["top_reason"] = "模型没排这场，按命中率补位：" + reason
            chosen.append(fill)
            seen_matches.add(match_key(pick))
    chosen_keys = {(item.get("match"), item.get("market")) for item in chosen}
    dropped = 0
    for item in result.get("drop") or []:
        try:
            index = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if index < 1 or index > len(picks):
            continue
        pick = picks[index - 1]
        if (pick.get("match"), pick.get("market")) in chosen_keys:
            continue
        dropped += 1
    if dropped:
        notes.append(f"模型把 {dropped} 个候选排在前三之后，理由已并入上面的排序口径。")
    return chosen, lessons



def mail_recipients() -> list[str]:
    raw = os.environ.get("MAIL_TO", "")
    return [item.strip() for item in raw.replace(";", ",").split(",") if item.strip()]


def send_mail(text: str) -> None:
    recipients = mail_recipients()
    if not recipients:
        return
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host:
        raise RuntimeError("已设置 MAIL_TO，但缺少 SMTP_HOST")
    port = int(os.environ.get("SMTP_PORT", "587") or "587")
    user = os.environ.get("SMTP_USER", "")
    password = os.environ.get("SMTP_PASSWORD", "")
    sender = os.environ.get("MAIL_FROM", user).strip()
    if not sender:
        raise RuntimeError("已设置 MAIL_TO，但缺少 MAIL_FROM")
    message = EmailMessage()
    message["Subject"] = (text.splitlines() or [PUSH_KEYWORD])[0][:120]
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message.set_content(text)
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30) as smtp:
            if user:
                smtp.login(user, password)
            smtp.send_message(message)
        return
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls()
        smtp.ehlo()
        if user:
            smtp.login(user, password)
        smtp.send_message(message)


def push(text: str, dry_run: bool) -> None:
    print(text)
    if dry_run:
        return
    if mail_recipients():
        send_mail(text)
    if PUSH_TYPE == "stdout" or not PUSH_WEBHOOK:
        return
    if PUSH_TYPE == "feishu":
        url = PUSH_WEBHOOK
        body = {"msg_type": "text", "content": {"text": text}}
    elif PUSH_TYPE == "dingtalk":
        url = PUSH_WEBHOOK
        body = {"msgtype": "text", "text": {"content": text}}
    elif PUSH_TYPE == "telegram":
        url = f"https://api.telegram.org/bot{PUSH_WEBHOOK}/sendMessage"
        body = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    else:
        url = PUSH_WEBHOOK
        body = {"text": text}
    resp = requests.post(url, json=body, timeout=30)
    resp.raise_for_status()


def pick_line(pick: dict) -> str:
    return f"{pick.get('market')}｜{pick.get('side')} {pick.get('line')}｜{edge_text(pick).lstrip('｜')}｜{stake_hint(pick)}"


def summary_block(top: list[dict]) -> list[str]:
    lines = ["【概率最优三场】"]
    if not top:
        lines.append("- 没有达到概率门槛的方向，今天空仓。")
        return lines
    for index, pick in enumerate(top, 1):
        lines.append(f"{index}. {pick.get('match')}")
        lines.append(f"   {pick_line(pick)}")
        if pick.get("top_reason"):
            lines.append(f"   复盘理由：{pick['top_reason']}")
        if pick.get("reason"):
            lines.append(f"   分布依据：{pick['reason']}")
    return lines


def candidate_lines(shown: list[dict], top: list[dict]) -> list[str]:
    used = {(pick.get("match"), pick.get("market"), pick.get("side"), pick.get("line")) for pick in top}
    rest = [pick for pick in shown
            if (pick.get("match"), pick.get("market"), pick.get("side"), pick.get("line")) not in used]
    if not rest:
        return []
    lines = ["【候选排行（按模型命中率）】"]
    for pick in rest:
        lines.append(f"- {pick['prob']:.0%}｜{pick.get('match')}｜{pick_line(pick)}")
    return lines


def render(day: str, matches: list[dict], shown: list[dict], top: list[dict],
           notes: list[str], review: list[str]) -> str:
    start, end = slate_window(day)
    lines = [
        f"{PUSH_KEYWORD}早筛 {day}｜窗口 {start:%m-%d %H:%M} 至 {end:%m-%d %H:%M}",
        "口径：全部由概率分布算出。命中率来自马会总入球盘和主客和盘反推的进球分布，近况只做修正；"
        "期望 = 命中率×赔率−1，期望为负一律标「不下」。",
        "",
    ]
    lines.extend(summary_block(top))
    extra = candidate_lines(shown, top)
    if extra:
        lines.append("")
        lines.extend(extra)
    lines.append("")
    lines.append("【复盘】")
    lines.extend(review or ["- 还没有可结算的历史。"])
    if notes:
        lines.append("")
        lines.append("【数据】")
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="9:00 触发时，研究当天中午到次日中午的马会比赛")
    parser.add_argument("--date", help="触发日 YYYY-MM-DD，默认今天。26 日会研究 26 日 12:00 到 27 日 12:00")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    args = parser.parse_args()

    day = args.date or datetime.now(TZ).strftime("%Y-%m-%d")
    notes: list[str] = []
    pool = screen(day, notes)
    shown = [pick for pick in pool if pick["prob"] >= SCREEN_MIN_PROB][:SHOW_LIMIT]
    if not shown and pool:
        shown = pool[:min(3, SHOW_LIMIT)]
        notes.append(f"没有方向达到 {SCREEN_MIN_PROB:.0%} 命中率，下面只是参考排序，不建议下注。")
    if pool:
        best = max(pool, key=lambda pick: pick.get("edge") or -9)
        notes.append(
            f"全场 {len(pool)} 个方向里期望最高的是 {best['market']} {best['side']} {best['line']}"
            f"（{best['match']}，期望{best['edge']:+.3f}）。"
        )
        if not any((pick.get("edge") or 0) > 0.005 for pick in pool):
            notes.append(
                "没有任何方向期望为正：概率本身就来自马会盘口，扣掉抽水后必然为负。"
                "命中率排序只回答「哪个更容易中」，变不出正期望。"
            )

    history = load_history()
    settled, pending = settle_history(history, day)
    if (settled or pending) and not args.dry_run:
        save_history(history)
    recent, stats = weekly_review(day, history)
    if not args.dry_run and shown:
        added, refreshed = record_history(day, shown)
        if added:
            notes.append(f"今天 {added} 个方向已记入历史，下次跑用 ESPN 赛果结算。")
        if refreshed:
            notes.append(f"历史里 {refreshed} 条补上了赔率和概率，校准样本增加。")

    top, lessons = ai_top3(day, shown, recent, stats, notes)
    review = review_lines(day, recent, stats, pending)
    review.extend(calibration_report(day, history))
    review.extend(f"- 经验：{lesson}" for lesson in lessons)

    text = render(day, [], shown, top, notes, review)
    push(text, args.dry_run)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        message = f"{PUSH_KEYWORD}早筛失败：{exc}"
        try:
            push(message, dry_run=False)
        except Exception:
            print(message, file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)
