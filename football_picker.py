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


MARKET_TYPES = ["HAD", "TTG", "HDC", "HIL", "CHL", "FHA", "FHL", "HFT", "FTS"]
MARKET_BATCHES = [["HDC", "HIL", "CHL"], ["HAD", "TTG"], ["FHA", "FHL"], ["HFT", "FTS"]]
MARKET_NAMES = {
    "HAD": "主客和",
    "TTG": "总入球",
    "HDC": "让球",
    "HIL": "入球大细",
    "CHL": "角球大细",
    "FHA": "半场主客和",
    "FHL": "半场大细",
    "HFT": "半全场",
    "FTS": "首队入球",
}


def line_picks(line: dict) -> dict[str, float] | None:
    """一条档位里所有仍在售卖的选择，返回 {str: 赔率}。"""
    if str(line.get("status") or "") != "AVAILABLE":
        return None
    picks = {}
    for combo in line.get("combinations") or []:
        if str(combo.get("status") or "") != "AVAILABLE":
            return None
        odds = parse_odds(combo.get("currentOdds"))
        if odds is None:
            return None
        picks[str(combo.get("str") or "")] = odds
    return picks or None


def pool_lines(match: dict, odds_type: str) -> list[dict]:
    """赛前盘口的全部档位（含备用线），已过滤滚球和已停售。"""
    out = []
    for pool in match.get("foPools") or []:
        if pool.get("oddsType") != odds_type or pool.get("inplay"):
            continue
        for line in pool.get("lines") or []:
            picks = line_picks(line)
            if not picks:
                continue
            out.append({
                "line": parse_split_line(line.get("condition")),
                "text": str(line.get("condition") or ""),
                "main": bool(line.get("main")),
                "picks": picks,
                "uid": str(line.get("lineId") or line.get("condition") or ""),
            })
    # 主盘排前面，便于展示
    out.sort(key=lambda item: (not item["main"], -abs(item["line"] or 0)))
    return out


def hkjc_card() -> list[dict]:
    def load():
        merged: dict[str, dict] = {}
        for batch in MARKET_BATCHES:
            for match in hkjc_query(batch):
                key = match.get("frontEndId") or ""
                if not key:
                    continue
                target = merged.setdefault(key, match)
                if target is not match:
                    target["foPools"] = (target.get("foPools") or []) + (match.get("foPools") or [])
        rows = []
        for match in merged.values():
            tournament = match.get("tournament") or {}
            books = {name: pool_lines(match, name) for name in MARKET_TYPES}
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
                "books": books,
            })
        return rows

    return cached_json("hkjc-card-v3.json", load, max_age_hours=0.25)


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


MAX_GOALS = 12


TTG_TAIL_SPLIT = (0.45, 0.28, 0.16, 0.11)
MODEL_TAG = "market-v2"


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


def goal_number(raw) -> int | None:
    if isinstance(raw, dict):
        raw = raw.get("displayValue") if raw.get("displayValue") is not None else raw.get("value")
    if raw is None or raw == "":
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


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

# ---------------------------------------------------------------------------
# 概率层（只用马会盘口，完全不看球队近况）
#
# 1. 每条档位先去掉抽水，得到该盘口的公平概率。
# 2. 入球大细/角球大细/半场大细都是多档挂牌，用最小二乘拟合泊松均值，
#    于是任何没挂牌的线也能定价，而且多档之间的不一致会直接体现成残差。
# 3. 主客和给出主胜概率，配合总入球期望反解主客各自的期望入球。
# 4. 让球、半场主客和、半全场、首队入球全部由上面这套分布派生。
# 5. 每个方向最后只有三个数：命中率 prob、保本赔率 (1-push)/prob、马会赔率 odds。
#    马会赔率低于保本赔率就是负期望，直接标「不下」。
# ---------------------------------------------------------------------------

MIN_ODDS = float(os.environ.get("MIN_ODDS", "1.20"))
MIN_EDGE_ADVICE = float(os.environ.get("MIN_EDGE_ADVICE", "0.03"))
MAX_ADVICE_ODDS = float(os.environ.get("MAX_ADVICE_ODDS", "8.0"))
MIN_ADVICE_PROB = float(os.environ.get("MIN_ADVICE_PROB", "0.12"))
SHOW_LIMIT = int(os.environ.get("SHOW_LIMIT", "14"))
TOP_MATCHES = int(os.environ.get("TOP_MATCHES", "3"))
MAX_GOALS_GRID = 12
MODEL_TAG = "market-v3"
MARKET_ORDER = ["HAD", "HDC", "HIL", "CHL", "TTG", "FHA", "FHL", "HFT", "FTS"]
SETTLE_MTYPES = {"HAD", "HDC", "HIL", "CHL", "TTG", "FHA", "FHL", "HFT"}


def devig(picks: dict[str, float]) -> dict[str, float]:
    inverse = {key: 1.0 / value for key, value in picks.items() if value and value > 1.0000001}
    total = sum(inverse.values())
    return {key: value / total for key, value in inverse.items()} if total > 0 else {}


def poisson_pmf(lam: float, top: int = MAX_GOALS_GRID) -> list[float]:
    if lam <= 0:
        return [0.0] * (top + 1)
    values = [math.exp(-lam)]
    for k in range(1, top + 1):
        values.append(values[-1] * lam / k)
    return values


def tail(pmf: list[float], k: int) -> float:
    return sum(pmf[k:]) if 0 <= k < len(pmf) else 0.0


def is_half(line: float) -> bool:
    return abs(line * 2 - round(line * 2)) < 1e-9 and round(line * 2) % 2 == 1


def is_quarter(line: float) -> bool:
    return abs(line * 4 - round(line * 4)) < 1e-9 and round(line * 4) % 2 == 1


def over_under(pmf: list[float], line: float) -> tuple[float, float]:
    """大球 (命中率, 走水率)。四分之一盘拆两注各半，整数盘有走水。"""
    if is_quarter(line):
        low = over_under(pmf, line - 0.25)
        high = over_under(pmf, line + 0.25)
        return (low[0] + high[0]) / 2, (low[1] + high[1]) / 2
    if is_half(line):
        return tail(pmf, int(math.floor(line)) + 1), 0.0
    k = int(round(line))
    return tail(pmf, k + 1), (pmf[k] if 0 <= k < len(pmf) else 0.0)


def flip(dist: dict[int, float]) -> dict[int, float]:
    return {-key: value for key, value in dist.items()}


def asian_view(dist: dict[int, float], line: float) -> tuple[float, float]:
    """亚盘命中率/走水率。dist 是押注方视角的净胜球，line 是让出的球（让为负）。

    和 settle_asian 同一个式子：净胜球 + line 与 0 比较，所以门槛是 -line。
    """
    if not dist:
        return 0.0, 0.0
    parts = [line - 0.25, line + 0.25] if is_quarter(line) else [line]
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


def component_fair(pmf: list[float], line: float) -> float | None:
    """单条整数/半档线的模型公平大球概率。整数档按「无走水」条件化，才能和市场内嵌概率直接比。"""
    if is_half(line):
        return tail(pmf, int(math.floor(line)) + 1)
    k = int(round(line))
    push = pmf[k] if 0 <= k < len(pmf) else 0.0
    denom = 1.0 - push
    return tail(pmf, k + 1) / denom if denom > 0.01 else None


def model_fair_over(pmf: list[float], line: float) -> float | None:
    if is_quarter(line):
        low = component_fair(pmf, line - 0.25)
        high = component_fair(pmf, line + 0.25)
        if low is None or high is None:
            return None
        return (low + high) / 2
    return component_fair(pmf, line)


def fit_poisson(points: list[tuple[float, float]]) -> tuple[float | None, int]:
    """points 是 (盘口线, 市场公平大球概率)，最小二乘找泊松均值。"""
    usable = [(line, p) for line, p in points if 0.004 < p < 0.996]
    if len(usable) < 3:
        return None, len(usable)
    best = None
    step = 1
    while step <= 900:
        lam = step / 100
        pmf = poisson_pmf(lam)
        err = 0.0
        for line, target in usable:
            value = model_fair_over(pmf, line)
            if value is None:
                continue
            err += (value - target) ** 2
            if best is not None and err > best[1]:
                break
        if best is None or err < best[1]:
            best = (lam, err)
        step += 1
    return (best[0] if best else None), len(usable)


def ou_points(lines: list[dict]) -> list[tuple[float, float]]:
    """多档大细 -> (盘口线, 市场公平大球概率)。半档、四点档、整数档全部用上。"""
    out = []
    for item in lines:
        line = item.get("line")
        if line is None:
            continue
        fair = devig(item["picks"])
        if "H" not in fair or not (0.004 < fair["H"] < 0.996):
            continue
        out.append((line, fair["H"]))
    return out


def fit_market_pmf(lines: list[dict]) -> tuple[float | None, list[float] | None, dict]:
    """返回 (λ, PMF, 拟合信息)。样本点太少就不给分布，避免 2 点拟合出假信号。"""
    points = ou_points(lines)
    lam, used = fit_poisson(points)
    info = {"points": used, "residual": None}
    if lam is None:
        return None, None, info
    pmf = poisson_pmf(lam)
    residual = 0.0
    for line, target in points:
        value = model_fair_over(pmf, line)
        if value is not None:
            residual = max(residual, abs(value - target))
    info["residual"] = residual
    return lam, pmf, info


def margin_pmf(lam_home: float, lam_away: float) -> dict[int, float]:
    home, away = poisson_pmf(lam_home), poisson_pmf(lam_away)
    out: dict[int, float] = {}
    for i, pi in enumerate(home):
        if pi < 1e-12:
            continue
        for j, pj in enumerate(away):
            if pj < 1e-12:
                continue
            out[i - j] = out.get(i - j, 0.0) + pi * pj
    return out


def split_lambdas(total: float, home_win: float | None) -> tuple[float, float]:
    """总期望入球已知，用主胜概率反解主客配比。"""
    if total <= 0.05:
        return 0.0, 0.0
    if home_win is None:
        return total / 2, total / 2
    low, high = 0.02, total - 0.02
    for _ in range(50):
        mid = (low + high) / 2
        margins = margin_pmf(mid, total - mid)
        prob = sum(value for key, value in margins.items() if key > 0)
        if prob < home_win:
            low = mid
        else:
            high = mid
    lam_home = (low + high) / 2
    return lam_home, total - lam_home


def result_of(margin: int) -> str:
    return "H" if margin > 0 else ("A" if margin < 0 else "D")


def ht_ft_view(lam_home: float, lam_away: float, share: float) -> dict[str, float]:
    """半全场九宫格：半场结果:全场结果。"""
    home_1, away_1 = poisson_pmf(lam_home * share), poisson_pmf(lam_away * share)
    home_2, away_2 = poisson_pmf(lam_home * (1 - share)), poisson_pmf(lam_away * (1 - share))
    out: dict[str, float] = {}
    for i, pi in enumerate(home_1):
        if pi < 1e-12:
            continue
        for j, pj in enumerate(away_1):
            first = result_of(i - j)
            head = pi * pj
            if head < 1e-12:
                continue
            for k, pk in enumerate(home_2):
                for l, pl in enumerate(away_2):
                    key = f"{first}:{result_of(i + k - j - l)}"
                    out[key] = out.get(key, 0.0) + head * pk * pl
    return out


def first_goal_view(lam_home: float, lam_away: float) -> dict[str, float]:
    total = lam_home + lam_away
    if total <= 0:
        return {"H": 0.0, "A": 0.0, "N": 1.0}
    none = math.exp(-total)
    return {"H": (1 - none) * lam_home / total, "A": (1 - none) * lam_away / total, "N": none}


def ttg_buckets(pmf: list[float]) -> dict[str, float]:
    out = {}
    for k in range(0, 7):
        out[str(k)] = pmf[k] if k < len(pmf) else 0.0
    out["7+"] = tail(pmf, 7)
    return out


def card_matches(day: str, notes: list[str]) -> list[dict]:
    """窗口内所有未开赛且有空盘的马会比赛。"""
    start, end = slate_window(day)
    now = datetime.now(TZ)
    try:
        card = hkjc_card()
    except Exception as exc:
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
        books = {name: lines for name, lines in (item.get("books") or {}).items() if lines}
        if not books:
            continue
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
            "books": books,
        })
    out.sort(key=lambda match: match["when"])
    return out


def build_model(match: dict, notes: list[str]) -> dict | None:
    """一场比赛的核心分布，全部来自盘口。"""
    books = match["books"]
    lam_total, pmf_total, total_fit = fit_market_pmf(books.get("HIL") or [])
    total_source = "入球大细多档"
    if lam_total is None:
        # 大细档位不够时退回总入球盘，退回来的分布不算「自拟合」。
        for item in books.get("TTG") or []:
            pmf = ttg_pmf(item["picks"])
            if pmf:
                pmf_total = pmf
                lam_total = sum(index * prob for index, prob in enumerate(pmf))
                total_source = "总入球盘"
                break
    if not lam_total or not pmf_total:
        return None

    home_win = None
    for item in books.get("HAD") or []:
        fair = devig(item["picks"])
        if "H" in fair:
            home_win = fair["H"]
            break

    lam_home, lam_away = split_lambdas(lam_total, home_win)
    margins = margin_pmf(lam_home, lam_away)

    lam_corner, pmf_corner, corner_fit = fit_market_pmf(books.get("CHL") or [])
    lam_half, pmf_half, half_fit = fit_market_pmf(books.get("FHL") or [])
    share = 0.45
    half_fitted = bool(lam_half and lam_total > 0.05)
    if half_fitted:
        share = min(0.60, max(0.30, lam_half / lam_total))
    margins_half = margin_pmf(lam_home * share, lam_away * share)

    return {
        "lam_total": lam_total,
        "lam_home": lam_home,
        "lam_away": lam_away,
        "pmf_total": pmf_total,
        "margins": margins,
        "lam_corner": lam_corner,
        "pmf_corner": pmf_corner,
        "corner_fit": corner_fit,
        "share": share,
        "half_fitted": half_fitted,
        "pmf_half": pmf_half if pmf_half else poisson_pmf(lam_total * share),
        "margins_half": margins_half,
        "total_source": total_source,
        "total_fit": total_fit,
        "half_fit": half_fit,
    }


# 哪些盘口的分布是用它自己的报价拟合出来的。自己拟合自己，残差就是假信号，
# 必须等档位数够多、残差够小才能当价值看。
SELF_FIT = {
    "HIL": ("total_fit", "total_source"),
    "CHL": ("corner_fit", None),
    "FHL": ("half_fit", "half_fitted"),
}
MIN_FIT_POINTS = 4
MAX_FIT_RESIDUAL = 0.025


def confidence(mtype: str, model: dict) -> tuple[str, str]:
    """返回 (等级, 说明)。等级 low 的方向只展示不推荐。"""
    spec = SELF_FIT.get(mtype)
    if spec is None:
        return "high", "由其他盘口派生"
    fit_key, flag_key = spec
    fit = model.get(fit_key) or {}
    if flag_key and not model.get(flag_key):
        return "low", "该盘口档位不足，退回先验"
    if mtype == "CHL" and not model.get("pmf_corner"):
        return "low", "没有角球分布"
    points = int(fit.get("points") or 0)
    residual = fit.get("residual")
    if points < MIN_FIT_POINTS:
        return "low", f"只 {points} 个半档点，拟合不可靠"
    if residual is not None and residual > MAX_FIT_RESIDUAL:
        return "low", f"档位之间残差 {residual:.1%}，互相矛盾"
    return "high", f"{points} 个半档点，残差{residual:.1%}" if residual is not None else "自拟合"


def selection_prob(mtype: str, side: str, line: float | None, model: dict) -> tuple[float, float] | None:
    """某个方向的 (命中率, 走水率)。算不出来返回 None。"""
    pmf_total = model["pmf_total"]
    margins = model["margins"]
    if mtype == "HAD":
        return {
            "H": sum(v for k, v in margins.items() if k > 0),
            "D": margins.get(0, 0.0),
            "A": sum(v for k, v in margins.items() if k < 0),
        }.get(side), 0.0
    if mtype == "TTG":
        return ttg_buckets(pmf_total).get(side), 0.0
    if mtype == "HDC":
        if line is None:
            return None
        if side == "H":
            return asian_view(margins, line)
        if side == "A":
            return asian_view(flip(margins), -line)
        return None
    if mtype == "HIL":
        if line is None:
            return None
        over, push = over_under(pmf_total, line)
        return (over, push) if side == "H" else (1 - over - push, push)
    if mtype == "CHL":
        if line is None or not model.get("pmf_corner"):
            return None
        over, push = over_under(model["pmf_corner"], line)
        return (over, push) if side == "H" else (1 - over - push, push)
    if mtype == "FHA":
        half = model["margins_half"]
        return {
            "H": sum(v for k, v in half.items() if k > 0),
            "D": half.get(0, 0.0),
            "A": sum(v for k, v in half.items() if k < 0),
        }.get(side), 0.0
    if mtype == "FHL":
        if line is None:
            return None
        over, push = over_under(model["pmf_half"], line)
        return (over, push) if side == "H" else (1 - over - push, push)
    if mtype == "HFT":
        if side not in ht_ft_view(model["lam_home"], model["lam_away"], model["share"]):
            return None
        return ht_ft_view(model["lam_home"], model["lam_away"], model["share"])[side], 0.0
    if mtype == "FTS":
        return first_goal_view(model["lam_home"], model["lam_away"]).get(side), 0.0
    return None


def side_label(mtype: str, side: str, line_text: str, home: str, away: str) -> str:
    if mtype == "HAD":
        return {"H": f"{home}胜", "D": "和", "A": f"{away}胜"}.get(side, side)
    if mtype == "FHA":
        return {"H": f"半场{home}", "D": "半场和", "A": f"半场{away}"}.get(side, side)
    if mtype == "TTG":
        return f"{side}球"
    if mtype == "HDC":
        who = home if side == "H" else away
        return f"{who} {line_text}"
    if mtype == "HIL":
        return f"{'大' if side == 'H' else '细'}{line_text}"
    if mtype == "CHL":
        return f"角球{'大' if side == 'H' else '细'}{line_text}"
    if mtype == "FHL":
        return f"半场{'大' if side == 'H' else '细'}{line_text}"
    if mtype == "HFT":
        first, _, full = side.partition(":")
        def tag(code):
            return {"H": home, "D": "和", "A": away}.get(code, code)
        return f"半场{tag(first)}/全场{tag(full)}"
    if mtype == "FTS":
        return {"H": f"{home}首球", "A": f"{away}首球", "N": "无入球"}.get(side, side)
    return side


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


def evaluate(match: dict, model: dict) -> list[dict]:
    """把这场所有盘口的每个方向都定价。"""
    out = []
    meta = match_meta(match)
    for mtype in MARKET_ORDER:
        for item in match["books"].get(mtype) or []:
            line = item.get("line")
            book_sum = sum(1.0 / odds for odds in item["picks"].values() if odds > 1.0)
            # 簿面异常说明报价过期或数据错乱，这种档位一律不算。
            if book_sum < 1.02 or book_sum > 1.6:
                continue
            fair = devig(item["picks"])
            for side, odds in item["picks"].items():
                if not odds or odds < MIN_ODDS:
                    continue
                view = selection_prob(mtype, side, line, model)
                if not view or view[0] is None:
                    continue
                prob, push = view
                if prob <= 0.0005:
                    continue
                edge = prob * odds - 1 + push
                breakeven = (1 - push) / prob if prob > 0 else None
                level, why = confidence(mtype, model)
                out.append({
                    "match": f"{match['kickoff']} {match['home_ch']} vs {match['away_ch']}",
                    "mtype": mtype,
                    "market": MARKET_NAMES.get(mtype, mtype),
                    "side_code": side,
                    "side": side_label(mtype, side, item["text"], match["home_ch"], match["away_ch"]),
                    "line": line,
                    "line_text": item["text"],
                    "main": item["main"],
                    "odds": odds,
                    "prob": prob,
                    "push": push,
                    "edge": edge,
                    "breakeven": breakeven,
                    "fair": fair.get(side),
                    "conf": level,
                    "conf_why": why,
                    "meta": meta,
                })
    return out


def vig_table(quotes: list[dict]) -> list[tuple[str, float, int]]:
    bag: dict[str, list[float]] = {}
    for quote in quotes:
        if quote.get("fair") and quote["fair"] > 0:
            bag.setdefault(quote["mtype"], []).append(1.0 / quote["odds"] - quote["fair"])
    rows = []
    for mtype, values in bag.items():
        rows.append((MARKET_NAMES.get(mtype, mtype), sum(values) / len(values), len(values)))
    rows.sort(key=lambda item: item[1])
    return rows


def screen(day: str, notes: list[str]) -> tuple[list[dict], list[dict]]:
    """返回 (全部方向, 参与比较的场次)。"""
    matches = card_matches(day, notes)
    if not matches:
        notes.append("窗口内没有可分析的比赛。")
        return [], []
    quotes: list[dict] = []
    modelled: list[dict] = []
    skipped = []
    for match in matches:
        model = build_model(match, notes)
        if not model:
            skipped.append(match)
            continue
        match["model"] = model
        modelled.append(match)
        quotes.extend(evaluate(match, model))
    notes.append(
        f"窗口内 {len(matches)} 场，{len(modelled)} 场能用盘口反推分布，"
        f"摊平出 {len(quotes)} 个可下方向。"
    )
    if skipped:
        names = "、".join(f"{item['home_ch']}vs{item['away_ch']}" for item in skipped[:3])
        notes.append(f"{len(skipped)} 场没有足够的大细档位，算不出分布，已跳过：{names}。")
    quotes.sort(key=lambda q: q["edge"], reverse=True)
    return quotes, modelled


def ranked_all(picks: list[dict], stats: dict | None = None) -> list[dict]:
    return sorted(picks, key=lambda pick: (pick.get("edge") or -9), reverse=True)


def rule_top3(picks: list[dict], stats: dict | None = None) -> list[dict]:
    return distinct_matches(ranked_all(picks), TOP_MATCHES)


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


def history_row(day: str, pick: dict) -> dict | None:
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
        "mtype": pick.get("mtype"),
        "market": pick.get("market"),
        "side_code": pick.get("side_code"),
        "side": pick.get("side"),
        "line": pick.get("line"),
        "line_text": pick.get("line_text"),
        "model": MODEL_TAG,
        "odds": pick.get("odds"),
        "prob": pick.get("prob"),
        "push": pick.get("push"),
        "edge": pick.get("edge"),
        "breakeven": pick.get("breakeven"),
        "settlement": None,
        "home_goals": None,
        "away_goals": None,
        "ht_home": None,
        "ht_away": None,
        "corner_total": None,
        "settled_day": None,
        "settle_attempts": 0,
    }


REFRESH_FIELDS = ("model", "odds", "prob", "push", "edge", "breakeven", "line_text",
                  "league", "league_en", "country")


def record_history(day: str, picks: list[dict]) -> tuple[int, int]:
    """返回 (新增条数, 补全条数)。"""
    rows = load_history()
    index = {
        (row.get("run_day"), row.get("no"), row.get("mtype"), row.get("side_code"), row.get("line")): row
        for row in rows
    }
    added = 0
    refreshed = 0
    for pick in picks:
        row = history_row(day, pick)
        if row is None:
            continue
        key = (day, row["no"], row["mtype"], row["side_code"], row["line"])
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
                linescores = item.get("linescores") or []
                sides[item.get("homeAway")] = {
                    "id": str((item.get("team") or {}).get("id") or ""),
                    "name": (item.get("team") or {}).get("displayName") or "",
                    "score": goal_number(item.get("score")),
                    # 半场比分优先取 linescores 第一段，拿不到就留空
                    "half": goal_number(linescores[0].get("value")) if linescores else None,
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
    mtype = row.get("mtype")
    side = row.get("side_code")
    line = row.get("line")
    total = home_goals + away_goals
    margin = home_goals - away_goals
    if mtype == "HAD":
        return {"H": "全赢" if margin > 0 else "全输",
                "D": "全赢" if margin == 0 else "全输",
                "A": "全赢" if margin < 0 else "全输"}.get(side)
    if mtype == "TTG":
        goals = total
        if side and side.endswith("+"):
            hit = goals >= int(side[:-1])
        else:
            hit = side is not None and goals == int(side)
        return "全赢" if hit else "全输"
    if mtype == "HDC":
        if line is None or side not in ("H", "A"):
            return None
        value = margin if side == "H" else -margin
        return settle_asian(value, line if side == "H" else -float(line))
    if mtype == "HIL":
        if line is None or side not in ("H", "L"):
            return None
        value = total if side == "H" else -total
        return settle_asian(value, -float(line) if side == "H" else float(line))
    if mtype == "CHL":
        values = [int(value) for value in (corners or {}).values()]
        if line is None or len(values) != 2 or side not in ("H", "L"):
            return None
        corners_total = sum(values)
        value = corners_total if side == "H" else -corners_total
        return settle_asian(value, -float(line) if side == "H" else float(line))
    if mtype == "FHA":
        ht_home, ht_away = result.get("ht_home"), result.get("ht_away")
        if ht_home is None or ht_away is None:
            return None
        diff = ht_home - ht_away
        return {"H": "全赢" if diff > 0 else "全输",
                "D": "全赢" if diff == 0 else "全输",
                "A": "全赢" if diff < 0 else "全输"}.get(side)
    if mtype == "FHL":
        ht_home, ht_away = result.get("ht_home"), result.get("ht_away")
        if line is None or ht_home is None or ht_away is None or side not in ("H", "L"):
            return None
        value = ht_home + ht_away
        return settle_asian(value if side == "H" else -value,
                            -float(line) if side == "H" else float(line))
    if mtype == "HFT":
        ht_home, ht_away = result.get("ht_home"), result.get("ht_away")
        if ht_home is None or ht_away is None or not side:
            return None
        first, _, full = side.partition(":")
        return "全赢" if (result_of(ht_home - ht_away) == first and result_of(margin) == full) else "全输"
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
        # 旧版本写的历史没有 mtype，结算不了，直接跳过，别占待结算名额。
        if not row.get("mtype"):
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
        need_corners = row.get("mtype") == "CHL"
        if need_corners:
            corners = event_corners(result["slug"], result["id"])
            corner_budget -= 1
        row["home_goals"] = result["home"]["score"]
        row["away_goals"] = result["away"]["score"]
        if result["home"].get("half") is not None and result["away"].get("half") is not None:
            row["ht_home"] = result["home"]["half"]
            row["ht_away"] = result["away"]["half"]
        values = [int(value) for value in (corners or {}).values()]
        if len(values) == 2:
            row["corner_total"] = sum(values)
        row["settled_day"] = day
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
        if row.get("settlement")
        and row.get("kickoff") and parse_kickoff(row["kickoff"]) >= horizon
    ]
    recent.sort(key=lambda row: row["kickoff"])
    stats: dict[str, dict[str, int]] = {}
    for row in recent:
        bag = stats.setdefault(row.get("mtype") or row.get("market") or "", {})
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
    """用已结算场次检验「模型命中率」是否可信，也就是唯一能验证的口径。"""
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
    lines = [f"概率校准：{len(graded)} 个方向，平均预测 {predicted:.0%}，实际命中 {actual:.0%}。"]
    if predicted - actual > 0.06:
        lines.append("预测偏高，模型偏乐观，注码要压小。")
    elif actual - predicted > 0.06:
        lines.append("实际命中高于预测，模型偏保守。")
    score = brier(graded, "prob")
    if score is not None:
        lines.append(f"- Brier {score:.3f}（0.25 等于瞎猜，越小越准）")
    return lines


def tab_text(row: dict) -> str:
    score = ""
    if row.get("home_goals") is not None:
        score = f"{row['home_goals']}-{row['away_goals']}"
    corner = ""
    if row.get("mtype") == "CHL" and row.get("corner_total") is not None:
        corner = f" 角球{row['corner_total']}"
    date = (row.get("kickoff") or "")[5:10]
    return (
        f"{date} {row.get('league')} {row.get('side')} "
        f"｜{score}{corner}｜{row.get('settlement')}"
    )


def bag_line(market: str, bag: dict[str, int]) -> str:
    parts = [f"{key}{bag[key]}" for key in ("全赢", "赢半", "走水", "输半", "全输") if bag.get(key)]
    return f"{market}：{'，'.join(parts) if parts else '没有结算'}"


def review_lines(day: str, recent: list[dict], stats: dict[str, dict[str, int]], pending: int = 0) -> list[str]:
    horizon = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ) - timedelta(days=REVIEW_DAYS)
    label = f"{horizon:%m-%d} 至 {day[5:]}"
    if not recent:
        lines = [f"过去 {REVIEW_DAYS} 天（{label}）没有已结算的推荐。"]
        if pending:
            lines.append(f"- 另有 {pending} 个方向还没对上 ESPN 赛果，复盘样本偏少。")
        return lines
    wins = sum(bag.get("全赢", 0) + bag.get("赢半", 0) for bag in stats.values())
    losses = sum(bag.get("全输", 0) + bag.get("输半", 0) for bag in stats.values())
    lines = [f"过去 {REVIEW_DAYS} 天（{label}）已结算 {len(recent)} 个方向：赢 {wins}，输 {losses}。"]
    for mtype in sorted(stats, key=lambda key: -sum(stats[key].values())):
        name = MARKET_NAMES.get(mtype, mtype)
        lines.append("- " + bag_line(name, stats[mtype]))
    lines.extend("- " + tab_text(row) for row in recent[-8:])
    if pending:
        lines.append(f"- 另有 {pending} 个方向没对上 ESPN 赛果，没算进胜率。")
    return lines


def weekly_payload(recent: list[dict]) -> list[dict]:
    return [
        {
            "date": (row.get("kickoff") or "")[:10],
            "league": row.get("league"),
            "market": row.get("market"),
            "side": row.get("side"),
            "line": row.get("line_text") or row.get("line"),
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
            "odds": pick.get("odds"),
            "model_prob": round(pick.get("prob") or 0, 4),
            "edge": round(pick.get("edge") or 0, 4),
            "breakeven": round(pick.get("breakeven") or 0, 3),
        }
        for index, pick in enumerate(picks, 1)
    ]


def stake_hint(pick: dict) -> str:
    edge = pick.get("edge")
    if edge is None:
        return "没赔率"
    if pick.get("conf") != "high":
        return "不下（分布不可信）"
    if edge >= MIN_EDGE_ADVICE:
        return "正常注"
    if edge >= 0.0:
        return "小注"
    return "不下"


def edge_text(pick: dict) -> str:
    parts = []
    if pick.get("odds") is not None:
        parts.append(f"赔率{pick['odds']:g}")
    if pick.get("prob") is not None:
        parts.append(f"模型{pick['prob']:.0%}")
    if pick.get("breakeven") is not None:
        parts.append(f"保本{pick['breakeven']:.2f}")
    if pick.get("edge") is not None:
        parts.append(f"期望{pick['edge']:+.1%}")
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
                    "你是足球盘口复盘助手。只能用用户给的已结算记录和今日候选，"
                    "禁止编造比分、盘口、角球、赔率或球队。不能新增候选，不能改玩法、方向、盘口，只能排序并说明理由。"
                    "候选里的 model_prob 是模型命中率，breakeven 是保本赔率（1/命中率），"
                    "edge = 命中率×赔率-1 并已加上走水。edge 为负一律不建议下注。"
                    "如果全部候选 edge 都为负，必须在 lessons 里明确说今天应该空仓。"
                    "输出 JSON：{\"lessons\":[最多3条中文经验],"
                    "\"top\":[{\"index\":候选序号,\"reason\":中文理由}],"
                    "\"drop\":[{\"index\":候选序号,\"why\":中文原因}]}。top 最多 3 个，按 edge 从高到低。"
                ),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ])
    except Exception as exc:
        notes.append(f"模型复盘失败，排名直接按期望值：{exc}")
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
            reason = (
                f"命中率{fill['prob']:.0%}，保本赔率{fill['breakeven']:.2f}，"
                f"马会{fill['odds']:g}，期望{fill['edge']:+.1%}。"
            )
            fill["top_reason"] = "模型没排这场，按期望补位：" + reason
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
    return f"{pick.get('market')}｜{pick.get('side')}｜{edge_text(pick).lstrip('｜')}｜{stake_hint(pick)}"


def advisable(pool: list[dict]) -> list[dict]:
    """可以下注的方向。只收高可信、期望够、赔率不夸张的，而且同一场只留一条。

    只推能结算的盘口：结算不了就没法验证，也学不到东西。
    """
    picked = []
    seen = set()
    for pick in pool:
        if pick.get("conf") != "high" or pick.get("mtype") not in SETTLE_MTYPES:
            continue
        if (pick.get("edge") or -9) < MIN_EDGE_ADVICE:
            continue
        if pick["odds"] > MAX_ADVICE_ODDS or pick["prob"] < MIN_ADVICE_PROB:
            continue
        key = pick["meta"]["no"]
        if key in seen:
            continue
        seen.add(key)
        picked.append(pick)
        if len(picked) >= TOP_MATCHES:
            break
    return picked


def divergence_lines(pool: list[dict], limit: int = 5) -> list[str]:
    """模型和市场分歧最大的方向。没有正期望时用来看哪里可能有价。"""
    rows = [pick for pick in pool if pick.get("conf") == "high"]
    rows.sort(key=lambda pick: pick["edge"], reverse=True)
    out = []
    for pick in rows[:limit]:
        out.append(
            f"- {pick['edge']:+.1%}｜{pick.get('match')}｜{pick.get('market')} {pick.get('side')}"
            f"｜模型{pick['prob']:.1%} 保本{pick['breakeven']:.2f} 马会{pick['odds']:g}"
        )
    return out


def advice_block(top: list[dict], advice: list[dict], pool: list[dict],
                 vigs: list[tuple[str, float, int]]) -> list[str]:
    lines = ["【投资建议】"]
    if advice:
        lines.append(f"筛出 {len(advice)} 个可下方向（高可信、期望 ≥{MIN_EDGE_ADVICE:.0%}、"
                     f"赔率 ≤{MAX_ADVICE_ODDS:g}、同一场只留一注）：")
        for index, pick in enumerate(top, 1):
            kelly = (pick["prob"] * pick["odds"] - 1 + pick["push"]) / (pick["odds"] - 1)
            stake = max(0.005, min(0.03, kelly / 4))
            if pick["odds"] > 4:
                stake = min(stake, 0.01)
            lines.append(f"{index}. {pick.get('match')}")
            lines.append(f"   {pick_line(pick)}｜建议注码约本金的 {stake:.1%}（四分之一凯利，高赔率封顶 1%）")
            lines.append(f"   可信度：{pick.get('conf_why')}")
            if pick.get("top_reason"):
                lines.append(f"   理由：{pick['top_reason']}")
        return lines

    lines.append("今天建议空仓：没有一个方向同时满足「高可信 + 期望 ≥"
                 f"{MIN_EDGE_ADVICE:.0%} + 赔率 ≤{MAX_ADVICE_ODDS:g}」。")
    lines.append("这不是程序没算出来，而是单靠马会自家盘口，概率和赔率同源，乘起来必然等于 1 减抽水。")
    best = next((pick for pick in pool if pick.get("conf") == "high"), None)
    if best:
        lines.append(f"可信方向里期望最好的是 {best['market']} {best['side']}（{best['match']}），"
                     f"只有 {best['edge']:+.1%}，还在抽水范围内，不值得下。")
    lines.append("马会内部能做的只有：")
    cheap = [row for row in vigs if row[1] < 0.06]
    if cheap:
        lines.append("- 挑抽水最低的盘口：" + "、".join(f"{name}{rate:.1%}" for name, rate, _ in cheap[:4]) + "。")
    lines.append("- 拿下面「保本赔率」去别家公司比价，外部赔率高于保本赔率才真正有价值。")
    lines.append("- 档位互相矛盾或档位太少的场次直接跳过，那类场次算出来的分布不可信。")
    return lines


def breakeven_block(picks: list[dict]) -> list[str]:
    rows = [pick for pick in picks if pick.get("conf") == "high"][:3]
    if not rows:
        return []
    lines = ["【保本赔率（外部比价用：外部赔率高于它才值得下）】"]
    for pick in rows:
        lines.append(
            f"- {pick.get('match')}｜{pick.get('side')}｜"
            f"命中率{pick['prob']:.0%}｜保本赔率 {pick['breakeven']:.2f}｜马会 {pick['odds']:g}"
        )
    return lines


def candidate_lines(shown: list[dict], top: list[dict]) -> list[str]:
    used = {(pick.get("match"), pick.get("mtype"), pick.get("side_code"), pick.get("line")) for pick in top}
    rest = [pick for pick in shown
            if (pick.get("match"), pick.get("mtype"), pick.get("side_code"), pick.get("line")) not in used]
    if not rest:
        return []
    lines = ["【候选排行（按期望值，从好到差）】"]
    for pick in rest:
        lines.append(f"- {pick['edge']:+.1%}｜{pick.get('match')}｜{pick_line(pick)}")
    return lines


def vig_block(vigs: list[tuple[str, float, int]]) -> list[str]:
    if not vigs:
        return []
    lines = ["【各盘口抽水率（越低越划算）】"]
    for name, rate, count in vigs:
        lines.append(f"- {name}：{rate:.1%}（{count} 个方向）")
    return lines


def render(day: str, shown: list[dict], top: list[dict], advice: list[dict], pool: list[dict],
           notes: list[str], review: list[str], vigs: list[tuple[str, float, int]]) -> str:
    start, end = slate_window(day)
    lines = [
        f"{PUSH_KEYWORD}早筛 {day}｜窗口 {start:%m-%d %H:%M} 至 {end:%m-%d %H:%M}",
        "口径：完全不看球队近况。命中率由马会多档盘口（入球大细、角球大细、半场大细）"
        "去抽水后拟合分布得到，让球/半场/半全场/首球都由同一套分布派生。",
        "",
    ]
    lines.extend(advice_block(top, advice, pool, vigs))
    diverge = divergence_lines(pool)
    if diverge:
        lines.append("")
        lines.append("【模型与盘口分歧最大（观察用，不构成下注建议）】")
        lines.extend(diverge)
    lines.append("")
    lines.extend(breakeven_block(top or shown))
    extra = candidate_lines(shown, top)
    if extra:
        lines.append("")
        lines.extend(extra)
    vigor = vig_block(vigs)
    if vigor:
        lines.append("")
        lines.extend(vigor)
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
    pool, modelled = screen(day, notes)
    vigs = vig_table(pool)
    shown = pool[:SHOW_LIMIT]
    advice = advisable(pool)
    shortlist = advice or shown

    if pool:
        best = next((pick for pick in pool if pick.get("conf") == "high"), pool[0])
        notes.append(
            f"{len(pool)} 个方向里，可信范围期望最高的是 {best['market']} {best['side']}"
            f"（{best['match']}），期望{best['edge']:+.1%}、保本赔率{best['breakeven']:.2f}、马会{best['odds']:g}。"
        )
        reliable = sum(1 for pick in pool if pick.get("conf") == "high")
        notes.append(f"其中 {reliable} 个方向的分布可信（档位够多、残差小），{len(pool) - reliable} 个档位不足只作参考。")
        leagues = {}
        for match in modelled:
            leagues[match["league"]] = leagues.get(match["league"], 0) + 1
        notes.append(f"覆盖 {len(leagues)} 个联赛：" + "、".join(
            f"{name}{count}场" for name, count in sorted(leagues.items(), key=lambda kv: -kv[1])[:6]
        ))

    history = load_history()
    settled, pending = settle_history(history, day)
    if (settled or pending) and not args.dry_run:
        save_history(history)
    recent, stats = weekly_review(day, history)
    if not args.dry_run and shortlist:
        recordable = [pick for pick in shortlist if pick["mtype"] in SETTLE_MTYPES]
        added, refreshed = record_history(day, recordable)
        if added:
            notes.append(f"今天 {added} 个方向已记入历史，下次跑用 ESPN 赛果结算。")
        if refreshed:
            notes.append(f"历史里 {refreshed} 条补上了赔率和概率。")

    top, lessons = ai_top3(day, shortlist, recent, stats, notes)
    review = review_lines(day, recent, stats, pending)
    review.extend(calibration_report(day, history))
    review.extend(f"- 经验：{lesson}" for lesson in lessons)

    text = render(day, shown, top, advice, pool, notes, review, vigs)
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
