#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""北京时间早间足球筛选：只研究香港马会当天开售的比赛。"""

from __future__ import annotations

import argparse
import json
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
API = "https://v3.football.api-sports.io"
FD_API = "https://api.football-data.org/v4"
# football-data.org 免费档能读到当前赛季。这里用程序内部的联赛名，方便沿用原有优先级。
FD_DISPLAY = {
    "PL": ("England", "Premier League"),
    "PD": ("Spain", "La Liga"),
    "SA": ("Italy", "Serie A"),
    "BL1": ("Germany", "Bundesliga"),
    "FL1": ("France", "Ligue 1"),
    "DED": ("Netherlands", "Eredivisie"),
    "ELC": ("England", "Championship"),
    "PPL": ("Portugal", "Primeira Liga"),
    "BSA": ("Brazil", "Serie A"),
}


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
SEARCH_PROVIDER = os.environ.get("SEARCH_PROVIDER", "tavily").lower()
SEARCH_API_KEY = os.environ.get("SEARCH_API_KEY", "")
PUSH_TYPE = os.environ.get("PUSH_TYPE", "stdout").lower()
PUSH_WEBHOOK = os.environ.get("PUSH_WEBHOOK", "")
PUSH_KEYWORD = os.environ.get("PUSH_KEYWORD", "足球")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MAX_DEEP = int(os.environ.get("MAX_DEEP", "8"))
MAX_STANDINGS = int(os.environ.get("MAX_STANDINGS", "30"))


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


def is_cup(name: str) -> bool:
    n = norm(name)
    return any(k in n for k in ("cup", "copa", "coupe", "pokal", "trophy"))


def is_serious_cup(name: str) -> bool:
    n = norm(name)
    return any(k in n for k in ("fa cup", "copa del rey", "dfb-pokal", "dfb pokal"))


def is_caution_cup(country: str, name: str) -> bool:
    c, n = norm(country), norm(name)
    if ("emirates" in c or c == "uae") and "president" in n:
        return True
    if "saudi" in c and is_cup(name):
        return True
    return False


def is_big5(country: str, name: str) -> bool:
    c, n = norm(country), norm(name)
    table = {
        "england": "premier league",
        "spain": "la liga",
        "italy": "serie a",
        "germany": "bundesliga",
        "france": "ligue 1",
    }
    return table.get(c) == n


def is_focus_league(country: str, name: str) -> bool:
    c, n = norm(country), norm(name)
    if c == "finland" and "veikkausliiga" in n:
        return True
    if c == "norway" and "eliteserien" in n:
        return True
    if "saudi" in c and "pro league" in n:
        return True
    if c == "japan" and n in ("j1 league", "j1"):
        return True
    return False


def style_hint(country: str, name: str) -> str:
    c, n = norm(country), norm(name)
    if c == "netherlands" or "eredivisie" in n:
        return "联赛先验：大球较多。只有这场近况也支持大球时才加重。"
    if "saudi" in c or "emirates" in c or c == "uae":
        return "联赛先验：大球较多。只有这场近况也支持大球时才加重。"
    if c == "australia" or "a-league" in n:
        return "联赛先验：大球、大角较多。只有这场近况也支持时才加重。"
    if "korea" in c or "k league" in n:
        return "联赛先验：小球较多。小球不做，只有让球方或大角自己成立才保留。"
    return ""


def football_data_code(country: str, name: str) -> str | None:
    key = (norm(country), norm(name))
    aliases = {
        ("england", "premier league"): "PL",
        ("spain", "la liga"): "PD",
        ("spain", "primera division"): "PD",
        ("italy", "serie a"): "SA",
        ("germany", "bundesliga"): "BL1",
        ("france", "ligue 1"): "FL1",
        ("netherlands", "eredivisie"): "DED",
        ("england", "championship"): "ELC",
        ("portugal", "primeira liga"): "PPL",
        ("brazil", "serie a"): "BSA",
    }
    return aliases.get(key)


def squash_name(name: str) -> str:
    text = norm(name).replace("&", " and ")
    for word in ("fc", "afc", "cf", "sc", "ac"):
        text = re.sub(rf"\b{word}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def priority(country: str, name: str) -> int:
    if is_big5(country, name):
        return 0
    if is_focus_league(country, name):
        return 1
    if is_serious_cup(name):
        return 2
    if is_caution_cup(country, name):
        return 5
    if is_cup(name):
        return 4
    return 3


def cache_path(day: str) -> Path:
    folder = ROOT / ".cache"
    folder.mkdir(exist_ok=True)
    return folder / f"standings-{day}.json"


def load_disk(day: str) -> dict:
    path = cache_path(day)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_disk(day: str, cache: dict) -> None:
    cache_path(day).write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")


class ApiFootball:
    def __init__(self, key: str):
        self.s = requests.Session()
        self.s.headers["x-apisports-key"] = key

    def get(self, path: str, params: dict) -> list:
        items = []
        page = 1
        total = 1
        while page <= total:
            query = dict(params)
            if page > 1:
                query["page"] = page
            resp = self.s.get(API + path, params=query, timeout=40)
            if resp.status_code == 429:
                raise ApiError("API-Football 请求额度用尽")
            resp.raise_for_status()
            body = resp.json()
            errors = body.get("errors")
            if errors:
                if page > 1 and "page" in str(errors).lower():
                    break
                raise ApiError(str(errors))
            items.extend(body.get("response") or [])
            total = int((body.get("paging") or {}).get("total") or 1)
            page += 1
        return items


def load_standings(api: ApiFootball, league_id: int, season: int, day: str, disk: dict):
    key = f"{league_id}:{season}"
    if key in disk:
        return disk[key]
    blocks = api.get("/standings", {"league": league_id, "season": season})
    table = None
    if blocks:
        groups = blocks[0]["league"]["standings"]
        if len(groups) == 1:
            table = groups[0]
    disk[key] = table
    save_disk(day, disk)
    return table


def tag_rows(table: list[dict]) -> dict[int, dict]:
    total = len(table)
    out = {}
    for row in table:
        rank = int(row["rank"])
        flags = []
        if rank <= 3:
            flags.append("前三")
        if total >= 6 and rank > total - 3:
            flags.append("后三")
        elif total < 6 and rank > 3:
            flags.append("后段")
        out[int(row["team"]["id"])] = {
            "rank": rank,
            "total": total,
            "flags": flags,
            "name": row["team"].get("name") or "",
        }
    return out


def rank_text(info: dict | None) -> str:
    if not info:
        return "排名未知"
    flags = "、".join(info["flags"]) if info["flags"] else "中游"
    return f"第{info['rank']}/{info['total']}（{flags}）"


def why_text(home_info, away_info) -> str:
    parts = []
    if home_info and home_info["flags"]:
        parts.append("主队" + "、".join(home_info["flags"]))
    if away_info and away_info["flags"]:
        parts.append("客队" + "、".join(away_info["flags"]))
    return "，".join(parts)


def kickoff_local(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)


def match_title(match: dict) -> str:
    home = match.get("home_ch") or match["home"]
    away = match.get("away_ch") or match["away"]
    number = match.get("hkjc_no") or ""
    return f"{number} {home} vs {away}".strip()


def squash_name(name: str) -> str:
    text = norm(name).replace("&", " and ")
    for word in ("fc", "afc", "cf", "sc", "ac"):
        text = re.sub(rf"\b{word}\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def football_data_code(country: str, name: str) -> str | None:
    key = (norm(country), norm(name))
    aliases = {
        ("england", "premier league"): "PL",
        ("spain", "la liga"): "PD",
        ("spain", "primera division"): "PD",
        ("italy", "serie a"): "SA",
        ("germany", "bundesliga"): "BL1",
        ("france", "ligue 1"): "FL1",
        ("netherlands", "eredivisie"): "DED",
        ("england", "championship"): "ELC",
        ("portugal", "primeira liga"): "PPL",
        ("brazil", "serie a"): "BSA",
    }
    return aliases.get(key)


class FootballData:
    def __init__(self, key: str):
        self.s = requests.Session()
        self.s.headers["X-Auth-Token"] = key

    def get(self, path: str, params: dict | None = None) -> dict:
        resp = self.s.get(FD_API + path, params=params or {}, timeout=40)
        if resp.status_code == 429:
            time.sleep(7)
            resp = self.s.get(FD_API + path, params=params or {}, timeout=40)
        if resp.status_code == 429:
            raise ApiError("football-data.org 请求过于频繁")
        resp.raise_for_status()
        return resp.json()


def load_fd_standings(fd: FootballData, code: str, day: str, disk: dict):
    key = f"fd:{code}"
    if key in disk:
        return disk[key]
    body = fd.get(f"/competitions/{code}/standings")
    table = None
    for block in body.get("standings") or []:
        if block.get("type") == "TOTAL":
            table = [
                {
                    "rank": row["position"],
                    "team": {"id": row["team"]["id"], "name": row["team"]["name"]},
                }
                for row in block.get("table") or []
            ]
            break
    disk[key] = table
    save_disk(day, disk)
    return table


def fd_matches_as_fixtures(fd: FootballData, day: str) -> list[dict]:
    day_dt = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    # dateTo 是开区间。多取前后各一天，再按北京时间过滤。
    start = (day_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    end = (day_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    body = fd.get("/matches", {"dateFrom": start, "dateTo": end})
    fixtures = []
    for match in body.get("matches") or []:
        code = (match.get("competition") or {}).get("code")
        if code not in FD_DISPLAY:
            continue
        when = kickoff_local(match["utcDate"])
        if when.strftime("%Y-%m-%d") != day:
            continue
        country, name = FD_DISPLAY[code]
        status = match.get("status") or ""
        short = "NS" if status in ("TIMED", "SCHEDULED") else status
        fixtures.append({
            "league": {
                "id": 1_000_000 + int(match["competition"]["id"]),
                "name": name,
                "country": country,
                "season": 2026,
            },
            "fixture": {"date": match["utcDate"], "status": {"short": short}},
            "teams": {
                "home": {
                    "id": match["homeTeam"]["id"],
                    "name": match["homeTeam"].get("shortName") or match["homeTeam"]["name"],
                },
                "away": {
                    "id": match["awayTeam"]["id"],
                    "name": match["awayTeam"].get("shortName") or match["awayTeam"]["name"],
                },
            },
        })
    return fixtures


# API-Football 免费档不给超出近三天的赛程。这些联赛用 ESPN 的公开比分榜补上，积分榜也从同一来源取，球队编号才能对上。
ESPN_LEAGUES = [
    ("jpn.1", "Japan", "J1 League"),
    ("ksa.1", "Saudi-Arabia", "Pro League"),
    ("nor.1", "Norway", "Eliteserien"),
    ("fin.1", "Finland", "Veikkausliiga"),
    ("aus.1", "Australia", "A-League"),
    ("usa.1", "USA", "MLS"),
    ("eng.1", "England", "Premier League"),
    ("esp.1", "Spain", "La Liga"),
    ("ita.1", "Italy", "Serie A"),
    ("ger.1", "Germany", "Bundesliga"),
    ("fra.1", "France", "Ligue 1"),
    ("ned.1", "Netherlands", "Eredivisie"),
    ("por.1", "Portugal", "Primeira Liga"),
    ("bra.1", "Brazil", "Serie A"),
    ("eng.2", "England", "Championship"),
    ("afc.champions", "Asia", "AFC Champions League Elite"),
    ("den.1", "Denmark", "Superliga"),
    ("fra.2", "France", "Ligue 2"),
]


def espn_fixtures(day: str, include_covered: bool) -> list[dict]:
    fixtures = []
    selected = [
        (index, slug, country, name)
        for index, (slug, country, name) in enumerate(ESPN_LEAGUES)
        if include_covered or not football_data_code(country, name)
    ]
    for index, slug, country, name in selected:
        resp = requests.get(
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard",
            params={"dates": day.replace("-", "")},
            timeout=30,
        )
        if resp.status_code == 400:
            continue
        resp.raise_for_status()
        for event in resp.json().get("events") or []:
            when = kickoff_local(event["date"])
            if when.strftime("%Y-%m-%d") != day:
                continue
            competition = event["competitions"][0]
            status_name = ((competition.get("status") or {}).get("type") or {}).get("name") or ""
            short = "NS" if status_name in ("STATUS_SCHEDULED", "STATUS_PREGAME") else status_name
            teams = {item.get("homeAway"): item.get("team") or {} for item in competition.get("competitors") or []}
            home = teams.get("home") or {}
            away = teams.get("away") or {}
            if not home.get("id") or not away.get("id"):
                continue
            fixtures.append({
                "league": {
                    "id": 3_000_000 + index,
                    "name": name,
                    "country": country,
                    "season": 2026,
                    "espn": slug,
                },
                "fixture": {"date": event["date"], "status": {"short": short}},
                "teams": {
                    "home": {"id": 2_000_000 + int(home["id"]), "name": home.get("displayName") or home.get("name")},
                    "away": {"id": 2_000_000 + int(away["id"]), "name": away.get("displayName") or away.get("name")},
                },
            })
    return fixtures


def load_espn_standings(slug: str, day: str, disk: dict):
    key = f"espn:{slug}"
    if key in disk:
        return disk[key]
    resp = requests.get(
        f"https://site.api.espn.com/apis/v2/sports/soccer/{slug}/standings",
        timeout=30,
    )
    resp.raise_for_status()
    children = resp.json().get("children") or []
    table = None
    if children:
        entries = ((children[0].get("standings") or {}).get("entries")) or []
        rows = []
        for entry in entries:
            rank = next((s.get("value") for s in entry.get("stats") or [] if s.get("name") == "rank"), None)
            team = entry.get("team") or {}
            if rank is None or not team.get("id"):
                continue
            rows.append({
                "rank": int(rank),
                "team": {
                    "id": 2_000_000 + int(team["id"]),
                    "name": team.get("displayName") or team.get("name") or "",
                },
            })
        if rows:
            table = rows
    disk[key] = table
    save_disk(day, disk)
    return table


HKJC_LEAGUES = {
    "English Premier": ("England", "Premier League"),
    "English Championship": ("England", "Championship"),
    "Spanish Division 1": ("Spain", "La Liga"),
    "Italian Division 1": ("Italy", "Serie A"),
    "German Division 1": ("Germany", "Bundesliga"),
    "French Division 1": ("France", "Ligue 1"),
    "Dutch Division 1": ("Netherlands", "Eredivisie"),
    "Portuguese Premier": ("Portugal", "Primeira Liga"),
    "Brazilian Division 1": ("Brazil", "Serie A"),
    "Asian Champions League Elite": ("Asia", "AFC Champions League Elite"),
    "Danish Superliga": ("Denmark", "Superliga"),
    "French Division 2": ("France", "Ligue 2"),
}

# 这些赛事不按积分榜前三后三筛，按强队身份、进球和近况看。
FOCUS_LEAGUE_NAMES = {
    "AFC Champions League Elite",
    "Superliga",
    "Ligue 1",
    "Ligue 2",
    "Primeira Liga",
}

STRONG_CLUBS = (
    ("al ittihad", "沙特传统强队"),
    ("al ahli", "沙特传统强队"),
    ("al hilal", "沙特传统强队"),
    ("al nassr", "沙特传统强队"),
    ("al ain", "阿联酋强队，上赛季联赛不败拿到亚冠资格"),
    ("benfica", "葡超豪门"),
    ("porto", "葡超豪门"),
    ("sporting", "葡超豪门"),
    ("paris saint germain", "法甲豪门"),
)

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


def hkjc_card() -> list[dict]:
    def load():
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
                    "fbOddsTypes": ["HDC", "HIL", "CHL"],
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
        rows = []
        for match in (body.get("data") or {}).get("matches") or []:
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
            })
        return rows

    return cached_json("hkjc-open-card.json", load, max_age_hours=0.25)


def rank_by_name(ranks: dict | None, name: str) -> dict | None:
    if not ranks or not name:
        return None
    best = None
    best_score = 0
    for info in ranks.values():
        score = name_score(name, info.get("name") or "")
        if score > best_score:
            best_score = score
            best = info
    if best_score >= 60:
        return best
    return None


def slate_window(day: str) -> tuple[datetime, datetime]:
    """触发日中午到次日中午。26 日 9:00 跑，就是 26 日 12:00 到 27 日 12:00。"""
    trigger = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
    start = trigger.replace(hour=12, minute=0, second=0, microsecond=0)
    end = (trigger + timedelta(days=1)).replace(hour=12, minute=0, second=0, microsecond=0)
    return start, end


def build_candidates(api: ApiFootball | None, fd: FootballData | None, day: str):
    start, end = slate_window(day)
    notes = [f"研究 {start:%m-%d %H:%M} 至 {end:%m-%d %H:%M} 的马会比赛。"]
    try:
        card = hkjc_card()
    except (ApiError, requests.RequestException) as exc:
        notes.append(f"马会赛程失败：{exc}")
        return [], notes, [], [], [], []
    now = datetime.now(TZ)
    playing = []
    started = 0
    for item in card:
        if not item.get("kickoff"):
            continue
        when = kickoff_local(item["kickoff"])
        if not (start <= when < end):
            continue
        if item["status"] != "PREEVENT" or when < now:
            started += 1
            continue
        if is_women_or_youth(item["tournament_en"]) or is_friendly(item["tournament_en"]):
            continue
        playing.append((when, item))
    if started:
        notes.append(f"这个时间窗里另有 {started} 场已开赛，没有研究。")
    if not playing:
        notes.append("这个时间窗里马会没有未开赛的场次。")
        return [], notes, [], [], [], []

    disk = load_disk(day)
    leagues: dict[str, dict] = {}
    for when, item in playing:
        country, league_name = HKJC_LEAGUES.get(item["tournament_en"], ("", item["tournament_en"]))
        key = item["tournament_en"]
        bucket = leagues.get(key)
        if bucket is None:
            bucket = {
                "name": league_name,
                "country": country,
                "mapped": bool(country),
                "ranks": None,
                "items": [],
            }
            leagues[key] = bucket
        bucket["items"].append((when, item))

    for league in leagues.values():
        if not league["mapped"] or league["name"] in FOCUS_LEAGUE_NAMES:
            continue
        code = football_data_code(league["country"], league["name"])
        slug = espn_slug_for(league["country"], league["name"])
        try:
            if code and fd is not None:
                table = load_fd_standings(fd, code, day, disk)
            elif slug:
                table = load_espn_standings(slug, day, disk)
            else:
                table = None
        except (ApiError, requests.RequestException) as exc:
            notes.append(f"积分榜失败 {league['name']}：{exc}")
            continue
        if table:
            league["ranks"] = tag_rows(table)
        else:
            notes.append(f"无单一积分榜：{league['name']}")

    candidates = []
    plain = []
    focus = []
    board = []
    for league in leagues.values():
        for when, item in league["items"]:
            home_info = rank_by_name(league["ranks"], item["home"]) if league["mapped"] else None
            away_info = rank_by_name(league["ranks"], item["away"]) if league["mapped"] else None
            hdc = item["hdc"] or "让球未开"
            row = {
                "kickoff": when.strftime("%H:%M"),
                "sort": when.isoformat(),
                "country": league["country"],
                "league": item["tournament_ch"],
                "league_en": league["name"],
                "home": item["home"],
                "away": item["away"],
                "home_ch": item["home_ch"],
                "away_ch": item["away_ch"],
                "hkjc_no": item["no"],
                "priority": priority(league["country"], league["name"]) if league["mapped"] else 3,
                "style": style_hint(league["country"], league["name"]) if league["mapped"] else "",
                "caution": False,
                "serious_cup": False,
                "home_rank": rank_text(home_info),
                "away_rank": rank_text(away_info),
                "hkjc": {
                    "home_line": item["home_line"],
                    "total": item["total"],
                    "total_text": item.get("total_text"),
                    "corner_line": item["corner_line"],
                    "hdc": item["hdc"],
                    "book": "马会",
                },
                "line_text": hdc,
            }
            home_hit = bool(home_info and home_info["flags"])
            away_hit = bool(away_info and away_info["flags"])
            if league["name"] in FOCUS_LEAGUE_NAMES:
                row["why"] = "按强队身份、进球和近况"
                focus.append(row)
            elif league["mapped"] and (home_hit or away_hit):
                row["why"] = why_text(home_info, away_info)
                candidates.append(row)
            elif league["mapped"] and home_info and away_info:
                row["why"] = "中游对中游"
                plain.append(row)
            else:
                if not league["mapped"]:
                    row["why"] = "马会有开，没有用来判断前三后三的积分榜"
                elif not league["ranks"]:
                    row["why"] = "积分榜没读到，不能判断是不是前三后三"
                else:
                    row["why"] = "队名没有对上积分榜"
                board.append(row)
    candidates.sort(key=lambda match: (match["priority"], match["sort"]))
    plain.sort(key=lambda match: (match["priority"], match["sort"]))
    focus.sort(key=lambda match: match["sort"])
    board.sort(key=lambda match: match["sort"])
    return candidates, notes, [], board, plain, focus



def search(query: str) -> list[str]:
    if SEARCH_PROVIDER == "serper":
        resp = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SEARCH_API_KEY, "Content-Type": "application/json"},
            json={"q": query, "num": 5, "hl": "zh-cn"},
            timeout=30,
        )
        resp.raise_for_status()
        return [
            f"{item.get('title', '')} | {item.get('snippet', '')}"
            for item in (resp.json().get("organic") or [])[:5]
        ]
    resp = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": SEARCH_API_KEY,
            "query": query,
            "max_results": 5,
            "search_depth": "basic",
            "include_answer": False,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return [
        f"{item.get('title', '')} | {(item.get('content') or '')[:400]}"
        for item in (resp.json().get("results") or [])[:5]
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


def recent_lines_for_team(slug: str, team: dict, before: datetime) -> list[dict]:
    team_id = str(team["id"])

    def load_schedule():
        body = espn_get(f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/teams/{team_id}/schedule")
        rows = []
        for event in body.get("events") or []:
            competition = (event.get("competitions") or [{}])[0]
            status = ((competition.get("status") or {}).get("type") or {}).get("name") or ""
            if status not in ("STATUS_FULL_TIME", "STATUS_FINAL", "STATUS_FINAL_AET", "STATUS_FINAL_PEN"):
                continue
            if "friendly" in (event.get("name") or "").lower():
                continue
            rows.append({
                "id": event.get("id"),
                "date": event.get("date"),
                "name": event.get("name") or "",
                "competitors": [
                    {
                        "id": item.get("team", {}).get("id") or item.get("id"),
                        "name": item.get("team", {}).get("displayName") or item.get("team", {}).get("name"),
                        "homeAway": item.get("homeAway"),
                        "score": item.get("score"),
                    }
                    for item in competition.get("competitors") or []
                ],
            })
        return rows

    run_day = datetime.now(TZ).strftime("%Y-%m-%d")
    schedule = cached_json(
        f"espn-schedule-{slug}-{team_id}-{run_day}.json",
        load_schedule,
        max_age_hours=12,
    )
    played = []
    for event in schedule:
        if not event.get("date"):
            continue
        when = kickoff_local(event["date"])
        if when >= before:
            continue
        played.append(event)
    played.sort(key=lambda item: item["date"], reverse=True)
    games = []
    for event in played[:6]:
        facts = match_facts(slug, event)
        if facts is None:
            continue
        own = next((item for item in facts["sides"] if str(item["id"]) == team_id), None)
        if own is None:
            continue
        opponent = next((item for item in facts["sides"] if str(item["id"]) != team_id), None)
        own_score = own.get("score")
        opp_score = opponent.get("score") if opponent else None
        outcome = None
        margin = None
        if own_score is not None and opp_score is not None:
            margin = own_score - opp_score
            if margin > 0:
                outcome = "胜"
            elif margin < 0:
                outcome = "负"
            else:
                outcome = "平"
        place = "主场" if own["homeAway"] == "home" else "客场"
        line = own.get("line")
        result = own.get("result")
        if line is None or not result:
            role = "让球未知"
        elif line < 0:
            role = f"让{line:+g} 让球方{result}"
        elif line > 0:
            role = f"让{line:+g} 受让{result}，不算穿盘"
        else:
            role = f"让0 {result}"
        total_text = ""
        if facts.get("total_line") is not None and facts.get("total_result"):
            total_text = f" 大{facts['total_line']:g}{facts['total_result']}"
        if own.get("corners") is not None and facts.get("corner_total") is not None:
            corner_text = f" 本队角球{own['corners']} 全场{facts['corner_total']}"
        else:
            corner_text = " 角球未知"
        date = kickoff_local(event["date"]).strftime("%m-%d")
        outcome_text = f"{outcome} " if outcome else ""
        games.append({
            "date": date,
            "text": f"{date} {place} {outcome_text}{facts['scoreline']} {role}{total_text}{corner_text}",
            "line": line,
            "result": result,
            "outcome": outcome,
            "margin": margin,
            "total_line": facts.get("total_line"),
            "total_result": facts.get("total_result"),
            "corner_total": facts.get("corner_total"),
        })
    return games


def parse_signed_line(raw) -> float | None:
    if raw is None:
        return None
    text = str(raw).strip().lower().replace("+", "")
    if text in ("pk", "even", "0", "0.0"):
        return 0.0
    try:
        return float(text)
    except ValueError:
        return None


def parse_home_line(odds: dict) -> float | None:
    point = ((odds.get("pointSpread") or {}).get("home") or {}).get("close") or {}
    if point.get("line") not in (None, ""):
        return parse_signed_line(point.get("line"))
    if odds.get("spread") not in (None, ""):
        return parse_signed_line(odds.get("spread"))
    return None


def parse_total_line(odds: dict) -> float | None:
    if odds.get("overUnder") not in (None, ""):
        try:
            return float(odds["overUnder"])
        except (TypeError, ValueError):
            pass
    raw = (((odds.get("total") or {}).get("over") or {}).get("close") or {}).get("line")
    if raw in (None, ""):
        return None
    return parse_signed_line(str(raw).lower().replace("o", "").replace("u", ""))


def match_facts(slug: str, event: dict) -> dict | None:
    event_id = event.get("id")
    if not event_id:
        return None

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
        odds = (body.get("odds") or [None])[0] or {}
        return {
            "corners": corners,
            "home_line": parse_home_line(odds),
            "total_line": parse_total_line(odds),
        }

    try:
        extra = cached_json(f"espn-summary2-{event_id}.json", load)
    except requests.RequestException:
        return None
    def score_value(raw) -> int | None:
        if isinstance(raw, dict):
            raw = raw.get("displayValue") if raw.get("displayValue") is not None else raw.get("value")
        if raw is None:
            return None
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return None

    sides = []
    home_goals = away_goals = None
    home_name = away_name = ""
    for item in event.get("competitors") or []:
        goals = score_value(item.get("score"))
        side = {
            "id": item.get("id"),
            "name": item.get("name"),
            "homeAway": item.get("homeAway"),
            "score": goals,
            "corners": extra["corners"].get(str(item.get("id"))),
            "line": None,
            "result": None,
        }
        if item.get("homeAway") == "home":
            home_goals = goals
            home_name = item.get("name") or ""
        else:
            away_goals = goals
            away_name = item.get("name") or ""
        sides.append(side)
    if extra.get("home_line") is not None and home_goals is not None and away_goals is not None:
        for side in sides:
            line = extra["home_line"] if side["homeAway"] == "home" else -extra["home_line"]
            margin = (home_goals - away_goals) if side["homeAway"] == "home" else (away_goals - home_goals)
            side["line"] = line
            side["result"] = settle_asian(margin, line)
    corner_values = [side["corners"] for side in sides if side.get("corners") is not None]
    total_line = extra.get("total_line")
    total_result = None
    if total_line is not None and home_goals is not None and away_goals is not None:
        total_result = settle_asian(home_goals + away_goals, -total_line)
    return {
        "scoreline": f"{home_name} {home_goals}-{away_goals} {away_name}",
        "corner_total": sum(corner_values) if len(corner_values) == 2 else None,
        "total_line": total_line,
        "total_result": total_result,
        "sides": sides,
    }


def result_counts(games: list[dict], role: str) -> tuple[list[dict], dict[str, int]]:
    picked = []
    for game in games:
        line = game.get("line")
        if line is None or not game.get("result"):
            continue
        if role == "giving" and line < 0:
            picked.append(game)
        elif role == "receiving" and line > 0:
            picked.append(game)
    bag: dict[str, int] = {}
    for game in picked:
        bag[game["result"]] = bag.get(game["result"], 0) + 1
    return picked, bag


def count_text(bag: dict[str, int]) -> str:
    parts = [f"{key}{bag[key]}" for key in ("全赢", "赢半", "走水", "输半", "全输") if bag.get(key)]
    return "，".join(parts) if parts else "没有结算"


def team_summary(name: str, games: list[dict]) -> str:
    giving, giving_bag = result_counts(games, "giving")
    receiving, receiving_bag = result_counts(games, "receiving")
    text = f"{name}：让球方{len(giving)}场，{count_text(giving_bag)}。"
    received_wins = receiving_bag.get("全赢", 0)
    if received_wins:
        text += f"受让全赢{received_wins}场，不计入穿盘。"
    return text


def team_report(slug: str, label: str, name: str, before: datetime) -> dict:
    team = resolve_espn_team(slug, name)
    if team is None:
        return {"name": name, "label": label, "games": [], "lines": [], "summary": "", "missing": f"{label}{name}：公开赛程没有对上球队"}
    try:
        games = recent_lines_for_team(slug, team, before)
    except requests.RequestException as exc:
        return {"name": name, "label": label, "games": [], "lines": [], "summary": "", "missing": f"{label}{name}：近况读取失败 {exc}"}
    if not games:
        return {"name": name, "label": label, "games": [], "lines": [], "summary": "", "missing": f"{label}{name}：没有读到近6场"}
    return {
        "name": name,
        "label": label,
        "games": games,
        "lines": [game["text"] for game in games],
        "summary": team_summary(name, games),
        "missing": None,
    }


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[(len(ordered) - 1) // 2]


def scoreboard_rows(slug: str, espn_day: str) -> list[dict]:
    def load():
        body = espn_get(
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard",
            {"dates": espn_day.replace("-", "")},
        )
        rows = []
        for event in body.get("events") or []:
            when = kickoff_local(event["date"])
            competition = (event.get("competitions") or [{}])[0]
            teams = {item.get("homeAway"): item.get("team") or {} for item in competition.get("competitors") or []}
            odds = (competition.get("odds") or [None])[0] or {}
            rows.append({
                "beijing_day": when.strftime("%Y-%m-%d"),
                "home": (teams.get("home") or {}).get("displayName") or "",
                "away": (teams.get("away") or {}).get("displayName") or "",
                "home_line": parse_home_line(odds),
                "total": parse_total_line(odds),
            })
        return rows

    return cached_json(f"espn-board2-{slug}-{espn_day}.json", load, max_age_hours=6)


def today_odds(slug: str, match: dict) -> dict:
    kick = datetime.fromisoformat(match["sort"])
    beijing_day = kick.astimezone(TZ).strftime("%Y-%m-%d")
    utc_day = kick.astimezone(timezone.utc).strftime("%Y-%m-%d")
    rows = []
    for espn_day in dict.fromkeys((utc_day, beijing_day)):
        try:
            rows.extend(scoreboard_rows(slug, espn_day))
        except requests.RequestException:
            continue
    best = None
    best_score = 0
    for row in rows:
        if row["beijing_day"] != beijing_day:
            continue
        home_score = name_score(match["home"], row["home"])
        away_score = name_score(match["away"], row["away"])
        if home_score < 60 or away_score < 60:
            continue
        if home_score + away_score > best_score:
            best_score = home_score + away_score
            best = row
    return best or {}


def give_label(home_line: float, hdc: str | None, home_is_favorite: bool) -> str:
    if hdc:
        text = hdc.replace("+", "").replace("-", "")
    else:
        text = f"{abs(home_line):g}"
    return ("主让" if home_is_favorite else "客让") + text


def handicap_case(report: dict, line_label: str, today_abs: float, book: str = "") -> dict:
    if report.get("missing"):
        return {"ok": False, "reason": f"当场{line_label}，但{report['missing']}。"}
    giving, bag = result_counts(report["games"], "giving")
    wins = bag.get("全赢", 0)
    losses = bag.get("全输", 0)
    summary = report["summary"]
    if len(giving) < 3 or not (wins > len(giving) / 2 and wins > losses):
        return {"ok": False, "reason": f"{summary}让球方全赢没有占多数。"}
    covered = [abs(game["line"]) for game in giving if game["result"] == "全赢"]
    covered_mid = median(covered)
    if covered_mid is not None and today_abs > covered_mid + 0.01:
        return {"ok": False, "reason": f"{summary}赢过的让球中位数{covered_mid:g}，当场{line_label}更深。"}
    confidence = "中"
    if len(giving) >= 4 and wins / len(giving) >= 0.75 and giving[0]["result"] != "全输":
        confidence = "高"
    deep = [
        f"{game['date']}让{game['line']:+g}全输"
        for game in giving
        if game["line"] <= -1.5 and game["result"] == "全输"
    ]
    risk = "盘口是马会让球。" if book == "马会" else "这是美式让分收盘，不是亚盘，下单前再对一次亚盘。"
    if deep:
        risk += "深盘全输：" + "、".join(deep[:3]) + "。"
    return {
        "ok": True,
        "confidence": confidence,
        "reason": f"当场{line_label}。{summary}",
        "risk": risk,
        "evidence": [report["summary"], *report["lines"][:4]],
    }


def total_case(home: dict, away: dict, today_total) -> dict:
    if today_total is None:
        return {"ok": False, "reason": "当场没有大小球盘，不推大球。"}
    notes = []
    evidence = []
    ratios = []
    for report in (home, away):
        if report.get("missing"):
            return {"ok": False, "reason": f"{report['missing']}，不推大球。"}
        priced = [game for game in report["games"] if game.get("total_result")]
        wins = sum(1 for game in priced if game["total_result"] == "全赢")
        losses = sum(1 for game in priced if game["total_result"] == "全输")
        win_lines = [game["total_line"] for game in priced if game["total_result"] == "全赢" and game.get("total_line") is not None]
        text = f"{report['name']}大球全赢{wins}/有盘{len(priced)}"
        notes.append(text)
        evidence.append(text)
        if len(priced) < 4 or not (wins > len(priced) / 2 and wins > losses):
            return {"ok": False, "reason": f"{text}，大球全赢没有占多数。"}
        mid = median(win_lines)
        if mid is not None and today_total > mid + 0.01:
            return {"ok": False, "reason": f"{report['name']}赢过的大球线中位数{mid:g}，当场{today_total:g}更高。"}
        ratios.append(wins / len(priced))
    confidence = "高" if ratios and min(ratios) >= 0.75 else "中"
    return {
        "ok": True,
        "confidence": confidence,
        "reason": f"当场大{today_total:g}。" + "。".join(notes) + "。",
        "evidence": evidence,
    }


def corner_case(home: dict, away: dict, line) -> dict:
    if line is None:
        return {"ok": False, "reason": corner_note(home, away)}
    parts = []
    ratios = []
    for report in (home, away):
        if report.get("missing"):
            return {"ok": False, "reason": f"{report['missing']}，不推大角。"}
        totals = [game["corner_total"] for game in report.get("games") or [] if game.get("corner_total") is not None]
        wins = sum(1 for total in totals if total > line)
        text = f"{report['name']}全场角球大于{line:g}的有{wins}/{len(totals)}"
        if len(totals) < 4 or wins <= len(totals) / 2:
            return {"ok": False, "reason": f"{text}，没有占多数。"}
        parts.append(text)
        ratios.append(wins / len(totals))
    confidence = "高" if ratios and min(ratios) >= 0.75 else "中"
    return {
        "ok": True,
        "confidence": confidence,
        "reason": f"当场角球大{line:g}。" + "。".join(parts) + "。",
        "evidence": parts,
    }


def corner_note(home: dict, away: dict) -> str:
    parts = []
    for report in (home, away):
        totals = [game["corner_total"] for game in report.get("games") or [] if game.get("corner_total") is not None]
        if len(totals) < 4:
            continue
        mid = median([float(item) for item in totals])
        parts.append(f"{report['name']}全场角球中位数{mid:g}")
    if not parts:
        return ""
    return " " + "，".join(parts) + "。没有角球盘，不推大角。"


def judge_one(match: dict) -> dict:
    title = match_title(match)
    home = match.get("home_report") or {"name": match["home"], "games": [], "lines": [], "summary": "", "missing": "没有近况"}
    away = match.get("away_report") or {"name": match["away"], "games": [], "lines": [], "summary": "", "missing": "没有近况"}
    today = match.get("today") or {}
    home_line = today.get("home_line")
    evidence = [item for item in (home.get("summary"), away.get("summary")) if item]
    evidence.extend((home.get("lines") or [])[:3])
    evidence.extend((away.get("lines") or [])[:3])
    book = today.get("book") or ""
    if home_line is not None and home_line < -0.01:
        side, report, label = "主队让球", home, give_label(home_line, today.get("hdc"), True)
    elif home_line is not None and home_line > 0.01:
        side, report, label = "客队让球", away, give_label(home_line, today.get("hdc"), False)
    else:
        side, report, label = None, None, ""
    if report is not None:
        decision = handicap_case(report, label, abs(home_line), book)
        if decision["ok"]:
            opponent = away if report is home else home
            _, received = result_counts(opponent.get("games") or [], "receiving")
            risk = decision["risk"]
            if received.get("全赢", 0) >= 3:
                risk += f"对手受让全赢{received['全赢']}场。"
            return {
                "match": title,
                "market": "让球",
                "side": side,
                "line": label,
                "confidence": decision["confidence"],
                "reason": decision["reason"],
                "evidence": decision["evidence"],
                "risk": risk,
            }
        handicap_reason = decision["reason"]
    else:
        handicap_reason = "当场没有读到让分，不判断谁是让球方。"
    totals = total_case(home, away, today.get("total"))
    if totals["ok"]:
        return {
            "match": title,
            "market": "大球",
            "side": "大球",
            "line": f"{today['total']:g}",
            "confidence": totals["confidence"],
            "reason": totals["reason"],
            "evidence": totals["evidence"],
            "risk": "盘口是马会大细。" if book == "马会" else "大小球用的是美式总分收盘，下单前再对一次亚盘大小球。",
        }
    corners = corner_case(home, away, today.get("corner_line"))
    if corners["ok"]:
        return {
            "match": title,
            "market": "大角",
            "side": "大角",
            "line": f"{today['corner_line']:g}",
            "confidence": corners["confidence"],
            "reason": corners["reason"],
            "evidence": corners["evidence"],
            "risk": "盘口是马会角球大细。",
        }
    reason = handicap_reason + totals["reason"] + corners["reason"]
    return {
        "match": title,
        "market": "跳过",
        "side": "无",
        "line": "无",
        "confidence": "低",
        "reason": reason,
        "evidence": evidence,
        "risk": "",
    }


def form_line(report: dict) -> str:
    if report.get("missing"):
        return report["missing"]
    games = report.get("games") or []
    wins = sum(1 for game in games if game.get("outcome") == "胜")
    draws = sum(1 for game in games if game.get("outcome") == "平")
    losses = sum(1 for game in games if game.get("outcome") == "负")
    priced = [game for game in games if game.get("total_result")]
    over_wins = sum(1 for game in priced if game["total_result"] == "全赢")
    corners = [float(game["corner_total"]) for game in games if game.get("corner_total") is not None]
    if len(corners) >= 4:
        corner_text = f"角球中位数{median(corners):g}"
    else:
        corner_text = f"角球样本{len(corners)}"
    return (
        f"{report['name']}近{len(games)}场{wins}胜{draws}平{losses}负，"
        f"大球全赢{over_wins}/{len(priced)}，{corner_text}"
    )


def outcome_case(report: dict, line_label: str, today_abs: float) -> dict:
    if report.get("missing"):
        return {"ok": False, "reason": f"{report['missing']}，不按胜负看让球。"}
    games = [game for game in report.get("games") or [] if game.get("outcome") and game.get("margin") is not None]
    covered = [
        game for game in games
        if game["outcome"] == "胜" and settle_asian(game["margin"], -today_abs) == "全赢"
    ]
    losses = sum(1 for game in games if game["outcome"] == "负")
    wins = sum(1 for game in games if game["outcome"] == "胜")
    summary = f"{report['name']}近{len(games)}场{wins}胜，净胜能让当天让球全赢的有{len(covered)}场"
    if len(games) < 4 or not (len(covered) > len(games) / 2 and len(covered) > losses):
        return {"ok": False, "reason": f"{summary}，没有占多数。"}
    confidence = "中"
    if len(covered) / len(games) >= 0.75 and games[0]["outcome"] != "负":
        confidence = "高"
    return {
        "ok": True,
        "confidence": confidence,
        "reason": f"当场{line_label}。{summary}。这是按近况胜负看让球方，不是按穿盘。",
    }


def judge_plain(match: dict) -> dict:
    title = match_title(match)
    home = match.get("home_report") or {"name": match["home"], "games": [], "lines": [], "summary": "", "missing": "没有近况"}
    away = match.get("away_report") or {"name": match["away"], "games": [], "lines": [], "summary": "", "missing": "没有近况"}
    today = match.get("today") or {}
    home_line = today.get("home_line")
    book = today.get("book") or ""
    forms = f"{form_line(home)}。{form_line(away)}。"
    evidence = [forms]
    evidence.extend((home.get("lines") or [])[:3])
    evidence.extend((away.get("lines") or [])[:3])
    totals = total_case(home, away, today.get("total"))
    if totals["ok"]:
        return {
            "match": title,
            "market": "大球",
            "side": "大球",
            "line": f"{today['total']:g}",
            "confidence": totals["confidence"],
            "reason": f"中游场。{forms}{totals['reason']}",
            "evidence": totals["evidence"],
            "risk": "盘口是马会大细。" if book == "马会" else "大小球用的是美式总分收盘，下单前再对一次亚盘大小球。",
        }
    if home_line is not None and home_line < -0.01:
        side, report, label = "主队让球", home, give_label(home_line, today.get("hdc"), True)
        opponent = away
    elif home_line is not None and home_line > 0.01:
        side, report, label = "客队让球", away, give_label(home_line, today.get("hdc"), False)
        opponent = home
    else:
        side, report, label, opponent = None, None, "", None
    if report is not None:
        decision = outcome_case(report, label, abs(home_line))
        if decision["ok"]:
            risk = "盘口是马会让球。按近况胜负，不是按历史穿盘。"
            opp_games = [game for game in (opponent or {}).get("games") or [] if game.get("outcome")]
            opp_wins = sum(1 for game in opp_games if game["outcome"] == "胜")
            if opp_games and opp_wins > len(opp_games) / 2:
                risk += "对手近期胜场也过半。"
            return {
                "match": title,
                "market": "让球",
                "side": side,
                "line": label,
                "confidence": decision["confidence"],
                "reason": f"中游场。{forms}{decision['reason']}",
                "evidence": evidence,
                "risk": risk,
            }
        result_reason = decision["reason"]
    else:
        result_reason = "当场没有让球方，胜负近况不单独开方向。"
    corners = corner_case(home, away, today.get("corner_line"))
    if corners["ok"]:
        return {
            "match": title,
            "market": "大角",
            "side": "大角",
            "line": f"{today['corner_line']:g}",
            "confidence": corners["confidence"],
            "reason": f"中游场。{forms}{corners['reason']}",
            "evidence": corners["evidence"],
            "risk": "盘口是马会角球大细。",
        }
    return {
        "match": title,
        "market": "跳过",
        "side": "无",
        "line": "无",
        "confidence": "低",
        "reason": forms + totals["reason"] + result_reason + corners["reason"],
        "evidence": evidence,
        "risk": "",
    }


def analyze_plain(matches: list[dict]) -> dict:
    return {"picks": [judge_plain(match) for match in matches]}


def name_is(name: str, key: str) -> bool:
    folded = fold_name(name)
    if folded == key or folded.startswith(key + " "):
        return True
    if " " not in key and folded.endswith(" " + key):
        return True
    return False


def strong_tag(name: str) -> str:
    for key, tag in STRONG_CLUBS:
        if name_is(name, key):
            return tag
    return ""


def quarter_label(value: float) -> str:
    quarters = int(round(abs(value) * 4))
    if quarters % 2:
        left = (quarters - 1) / 4
        right = (quarters + 1) / 4
        return f"{left:g}/{right:g}"
    return f"{quarters / 4:g}"


def extra_slugs(league_en: str, name: str) -> list[str]:
    slugs = []
    tag = strong_tag(name)
    if tag.startswith("沙特"):
        slugs.append("ksa.1")
    if tag.startswith("葡超") or league_en == "Primeira Liga":
        slugs.extend(["por.1", "uefa.europa", "uefa.champions"])
    if tag.startswith("法甲") or league_en == "Ligue 1":
        slugs.extend(["fra.1", "uefa.champions", "uefa.europa"])
    if league_en == "Superliga":
        slugs.extend(["uefa.champions", "uefa.europa"])
    return slugs


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
            "slug": slug,
        })
    games.sort(key=lambda item: item["sort"], reverse=True)
    return games


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
        "tag": strong_tag(name),
        "games": games,
        "same": [game for game in games if game["same"]],
        "missing": None if matched else f"{name}没有对上公开赛程",
    }


def focus_handicap(match: dict, home: dict, away: dict, home_line: float) -> dict | None:
    favorite_home = home_line < -0.01
    favorite = home if favorite_home else away
    opponent = away if favorite_home else home
    if name_is(favorite["name"], "paris saint germain") and match.get("league_en") == "Ligue 1":
        return None
    depth = abs(home_line)
    place = "主队" if favorite_home else "客队"
    label = ("主让" if favorite_home else "客让") + quarter_label(depth)
    tag = favorite["tag"]
    recent = favorite["games"][:6]
    same = favorite["same"]
    opp_same = opponent["same"]
    cn = match["home_ch"] if favorite_home else match["away_ch"]
    opp_cn = match["away_ch"] if favorite_home else match["home_ch"]

    if len(recent) >= 5 and all(game["outcome"] != "负" for game in recent) and depth <= 0.5 + 0.01:
        return {
            "side": place + "不败",
            "line": label,
            "reason": f"{cn}近{len(recent)}场没有输球，盘口是{label}。支持不败。",
            "evidence": [f"{game['date']} {game['goals_for']}-{game['goals_against']} {game['opponent']}" for game in recent[:4]],
        }
    if tag and 1.5 - 0.01 <= depth <= 2.0 + 0.01 and len(opp_same) >= 2 and all(game["outcome"] != "胜" for game in opp_same):
        return {
            "side": place + "让球",
            "line": label,
            "reason": f"{opp_cn}本届{len(opp_same)}场没有赢，{cn}是{tag}。可以看{label}穿盘。",
            "evidence": [f"{game['date']} {game['goals_for']}-{game['goals_against']} {game['opponent']}" for game in opp_same[:4]],
        }
    wins = [game for game in recent if game["outcome"] == "胜"]
    losses = sum(1 for game in recent if game["outcome"] == "负")
    win_mid = median([float(game["margin"]) for game in wins])
    if (
        len(recent) >= 5
        and len(wins) >= 4
        and losses <= 2
        and win_mid is not None
        and win_mid >= 2 - 0.01
        and 1.0 - 0.01 <= depth <= min(1.5, win_mid) + 0.01
    ):
        return {
            "side": place + "让球",
            "line": label,
            "reason": f"{cn}近{len(recent)}场{len(wins)}胜，赢球净胜中位数{win_mid:g}。可以看{label}。",
            "evidence": [f"{game['date']} {game['goals_for']}-{game['goals_against']} {game['opponent']}" for game in recent[:4]],
        }
    if tag and len(same) >= 2 and all(game["outcome"] == "胜" for game in same):
        margins = [float(game["margin"]) for game in same]
        mid = median(margins)
        if mid is not None and depth <= mid + 0.01 and depth <= 1.5 + 0.01:
            return {
                "side": place + "让球",
                "line": label,
                "reason": f"{cn}是{tag}，本届{len(same)}场都赢，净胜中位数{mid:g}。可以看{label}。",
                "evidence": [f"{game['date']} {game['goals_for']}-{game['goals_against']} {game['opponent']}" for game in same[:4]],
            }
    scored = sum(1 for game in recent if game["goals_for"] >= 1)
    opponent_scores = any(game["goals_for"] > 0 for game in opp_same)
    opponent_concedes = any(game["goals_against"] > 0 for game in opp_same)
    if (
        tag
        and 0.5 - 0.01 <= depth <= 1.0 + 0.01
        and len(recent) >= 4
        and scored >= 4
        and opponent_scores
        and opponent_concedes
    ):
        return {
            "side": place + "让球",
            "line": label,
            "reason": f"{cn}是{tag}，近况有进球。{opp_cn}本届能进球也能丢球。看好{place}拿下，{label}。",
            "evidence": [f"{game['date']} {game['goals_for']}-{game['goals_against']} {game['opponent']}" for game in recent[:4]],
        }
    return None


def average_total(games: list[dict]) -> float | None:
    sample = games[:6]
    if len(sample) < 4:
        return None
    return sum(game["total"] for game in sample) / len(sample)


def hkjc_line_text(raw: str | None, number: float) -> str:
    if raw:
        return str(raw).replace("+", "").replace("-", "")
    return quarter_label(number)


def over_view(games: list[dict], today_total: float, today_text: str | None, reason: str) -> dict:
    return {
        "line": hkjc_line_text(today_text, today_total),
        "reason": reason,
        "risk": "盘口是马会大细。",
        "evidence": [f"{game['date']} 总进球{game['total']} {game['opponent']}" for game in games[:4]],
    }


def judge_focus(match: dict) -> list[dict]:
    title = f"{match['kickoff']} {match_title(match)}"
    before = datetime.fromisoformat(match["sort"])
    slug = espn_slug_for(match["country"], match.get("league_en") or "")
    empty = {"name": "", "tag": "", "games": [], "same": [], "missing": "没有公开赛程"}
    if slug is None:
        home, away = empty, empty
    else:
        home = team_goal_profile(slug, match["home"], before, match.get("league_en") or "")
        away = team_goal_profile(slug, match["away"], before, match.get("league_en") or "")
    today = match.get("hkjc") or {}
    home_line = today.get("home_line")
    picks = []
    handicap = None
    if home_line is not None and abs(home_line) > 0.01 and slug is not None:
        handicap = focus_handicap(match, home, away, home_line)
    if handicap:
        picks.append({
            "match": title,
            "market": "让球",
            "side": handicap["side"],
            "line": handicap["line"],
            "confidence": "中",
            "reason": handicap["reason"],
            "evidence": handicap["evidence"],
            "risk": "盘口是马会让球。",
        })
    favorite = None
    if home_line is not None and home_line < -0.01:
        favorite = home
    elif home_line is not None and home_line > 0.01:
        favorite = away
    elif home["tag"] and not away["tag"]:
        favorite = home
    elif away["tag"] and not home["tag"]:
        favorite = away
    over = None
    today_total = today.get("total")
    today_text = today.get("total_text")
    shown = hkjc_line_text(today_text, today_total) if today_total is not None else ""
    if today_total is not None and handicap and favorite and favorite["tag"] and 0 < len(favorite["same"]) < 4:
        sample = favorite["same"][:6]
        if all(game["total"] > today_total + 0.01 for game in sample):
            over = over_view(
                sample,
                today_total,
                today_text,
                f"近{len(sample)}场总进球都大于{shown}。可以看大{shown}。",
            )
    elif today_total is not None and not handicap:
        home_avg = average_total(home["games"])
        away_avg = average_total(away["games"])
        if home_avg is not None and away_avg is not None and min(home_avg, away_avg) + 0.01 >= today_total:
            over = over_view(
                (favorite or home)["games"],
                today_total,
                today_text,
                f"两队近况场均总进球{home_avg:.1f}和{away_avg:.1f}。可以看大{shown}。",
            )
    if over:
        reason = over["reason"]
        if match.get("league_en") == "Ligue 1" and (
            name_is(match["home"], "paris saint germain") or name_is(match["away"], "paris saint germain")
        ):
            reason = "联赛和欧冠不是一套阵容，胜负不稳定，不看让球。" + reason
        picks.append({
            "match": title,
            "market": "大球",
            "side": "大球",
            "line": over["line"],
            "confidence": "中",
            "reason": reason,
            "evidence": over["evidence"],
            "risk": over["risk"],
        })
    if picks:
        return picks
    if name_is(match["home"], "paris saint germain") or name_is(match["away"], "paris saint germain"):
        note = "联赛和欧冠不是一套阵容，胜负不稳定，不看让球。"
    elif home.get("missing") or away.get("missing"):
        note = (home.get("missing") or "") + (away.get("missing") or "")
    else:
        note = "强队身份、进球和近况没有同时成立。"
    return [{
        "match": title,
        "market": "跳过",
        "side": "无",
        "line": "无",
        "confidence": "低",
        "reason": note,
        "evidence": [],
        "risk": "",
    }]


def gather_context(matches: list[dict]) -> None:
    for match in matches:
        slug = espn_slug_for(match["country"], match.get("league_en") or match["league"])
        before = datetime.fromisoformat(match["sort"])
        hkjc = match.get("hkjc") or {}
        if slug is None:
            empty = {"games": [], "lines": [], "summary": "", "missing": "这个联赛没有公开近况"}
            match["home_report"] = {**empty, "name": match["home"], "label": "主队"}
            match["away_report"] = {**empty, "name": match["away"], "label": "客队"}
            today = {}
        else:
            match["home_report"] = team_report(slug, "主队", match["home"], before)
            match["away_report"] = team_report(slug, "客队", match["away"], before)
            need_espn = hkjc.get("home_line") is None or hkjc.get("total") is None
            today = today_odds(slug, match) if need_espn else {}
        if hkjc.get("home_line") is not None:
            today["home_line"] = hkjc["home_line"]
            today["hdc"] = hkjc.get("hdc")
            today["book"] = "马会"
        if hkjc.get("total") is not None:
            today["total"] = hkjc["total"]
            today["book"] = "马会"
        if hkjc.get("corner_line") is not None:
            today["corner_line"] = hkjc["corner_line"]
        match["today"] = today
        recent = []
        for report in (match["home_report"], match["away_report"]):
            if report.get("missing"):
                recent.append(report["missing"])
            elif report.get("summary"):
                recent.append(report["summary"])
                recent.extend(report["lines"])
        match["recent"] = recent


def analyze(matches: list[dict]) -> dict:
    return {"picks": [judge_one(match) for match in matches]}


def parse_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    return json.loads(text)


def llm(messages: list[dict]) -> dict:
    url = LLM_BASE_URL + "/chat/completions"
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": LLM_MODEL,
        "temperature": 0.2,
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


def accept_pick(pick: dict) -> bool:
    market = pick.get("market", "")
    side = str(pick.get("side") or "")
    line = str(pick.get("line") or "")
    if market not in ("让球", "大球", "大角"):
        return False
    if pick.get("confidence") == "低":
        return False
    if not pick.get("evidence"):
        return False
    if market == "让球":
        if side not in ("主队让球", "客队让球", "主队不败", "客队不败"):
            return False
        if any(word in side or word in line for word in ("受让", "加号", "下盘", "+")):
            return False
    if market == "大球" and "小" in side:
        return False
    if market == "大角" and ("小" in side or side != "大角"):
        return False
    return True


def render(day, candidates, deep, result, notes, cups, analyzed, board) -> str:
    start, end = slate_window(day)
    lines = [f"{PUSH_KEYWORD}早筛 {day}｜{start:%m-%d %H:%M} 至 {end:%m-%d %H:%M}", ""]
    picks = []
    skipped = []
    mentioned = set()
    if result:
        for pick in result.get("picks") or []:
            mentioned.add(pick.get("match"))
            if accept_pick(pick):
                picks.append(pick)
            else:
                skipped.append(pick)
    for match in deep:
        title = match_title(match)
        if title not in mentioned:
            skipped.append({"match": title, "reason": "模型未给出可用结论"})

    lines.append("【可做】")
    if not analyzed:
        lines.append("这轮没有做近况检索，下面是按积分榜挑出的候选。")
    elif not picks:
        lines.append("今天没有读出让球方、大球或大角。")
    for index, pick in enumerate(picks, 1):
        lines.append(
            f"{index}. {pick.get('match')}｜{pick.get('side')} {pick.get('line')}｜{pick.get('confidence')}"
        )
        lines.append(f"   {pick.get('reason')}")
        if pick.get("risk"):
            lines.append(f"   风险：{pick.get('risk')}")

    lines.append("")
    lines.append("【读过但跳过】")
    if not skipped:
        lines.append("无")
    for pick in skipped:
        lines.append(f"- {pick.get('match')}：{pick.get('reason') or pick.get('market')}")

    deep_ids = {match_title(match) for match in deep}
    rest = [match for match in candidates if match_title(match) not in deep_ids]
    lines.append("")
    lines.append("【前三/后三候选，供再筛】")
    if not rest:
        lines.append("无")
    for match in rest[:30]:
        lines.append(
            f"- {match['kickoff']} {match['league']} {match_title(match)}｜{match['why']}"
            f"｜主{match['home_rank']} 客{match['away_rank']}"
        )
    if len(rest) > 30:
        lines.append(f"- 另有 {len(rest) - 30} 场未展开")

    if board:
        lines.append("")
        lines.append("【马会有开，未做近况】")
        for match in board:
            lines.append(
                f"- {match['kickoff']} {match['league']} {match_title(match)}｜{match['why']}｜{match.get('line_text')}"
            )

    if cups:
        lines.append("")
        lines.append("【杯赛】")
        for match in cups[:15]:
            flag = "谨慎，青年军风险" if match["caution"] else match["why"]
            lines.append(f"- {match['kickoff']} {match['league']} {match_title(match)}｜{flag}")
        if len(cups) > 15:
            lines.append(f"- 另有 {len(cups) - 15} 场杯赛未展开")

    if notes:
        lines.append("")
        lines.append("【数据备注】")
        lines.extend(f"- {note}" for note in notes[:12])
    lines.append("")
    lines.append("下单前再看一眼马会即时盘。这是筛选简报。")
    return "\n".join(lines)


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


def fill_focus_ranks_from_search(leagues: list[dict], notes: list[str]) -> None:
    pending = [
        league for league in leagues
        if league.get("ranks") is None
        and not is_cup(league["name"])
        and is_focus_league(league["country"], league["name"])
    ]
    if not pending:
        return
    if not SEARCH_API_KEY or not LLM_API_KEY:
        notes.append("芬兰、挪威、沙特、日本有比赛，但没有搜索密钥，积分榜未核对。")
        return
    packs = []
    for league in pending:
        teams = []
        for item in league["fixtures"]:
            teams.extend([item["teams"]["home"]["name"], item["teams"]["away"]["name"]])
        try:
            snippets = search(f"{league['country']} {league['name']} current standings table 2026")
        except Exception as exc:
            notes.append(f"积分榜检索失败 {league['name']}：{exc}")
            snippets = []
        packs.append({
            "league": league["name"],
            "country": league["country"],
            "teams": sorted(set(teams)),
            "snippets": snippets[:8],
        })
    try:
        result = llm([
            {
                "role": "system",
                "content": "只根据检索摘要提取积分榜名次。摘要没有写明的球队不要输出，禁止编造名次。",
            },
            {
                "role": "user",
                "content": (
                    "返回 JSON："
                    "{\"tables\":[{\"league\":\"联赛名\",\"total\":球队总数,"
                    "\"rows\":[{\"team\":\"必须是给定名单里的原名\",\"rank\":1}]}]}\n"
                    + json.dumps(packs, ensure_ascii=False)
                ),
            },
        ])
    except Exception as exc:
        notes.append(f"重点联赛积分榜解析失败：{exc}")
        return
    by_name = {league["name"]: league for league in pending}
    for table in result.get("tables") or []:
        league = by_name.get(table.get("league"))
        if league is None:
            continue
        try:
            total = int(table.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        id_by_name = {}
        for item in league["fixtures"]:
            for side in ("home", "away"):
                team = item["teams"][side]
                id_by_name[squash_name(team["name"])] = (team["id"], team["name"])
        ranks = {}
        for row in table.get("rows") or []:
            found = id_by_name.get(squash_name(str(row.get("team") or "")))
            if not found:
                continue
            try:
                rank = int(row.get("rank"))
            except (TypeError, ValueError):
                continue
            if rank <= 0 or (total and rank > total):
                continue
            team_id, team_name = found
            flags = []
            if rank <= 3:
                flags.append("前三")
            if total >= 6 and rank > total - 3:
                flags.append("后三")
            ranks[team_id] = {
                "rank": rank,
                "total": total or rank,
                "flags": flags,
                "name": team_name,
            }
        if ranks:
            league["ranks"] = ranks
            notes.append(f"{league['name']} 的名次来自检索，只核到本场球队。")


def main() -> None:
    parser = argparse.ArgumentParser(description="9:00 触发时，研究当天中午到次日中午的马会比赛")
    parser.add_argument("--date", help="触发日 YYYY-MM-DD，默认今天。26 日会研究 26 日 12:00 到 27 日 12:00")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不推送")
    args = parser.parse_args()
    if not APIFOOTBALL_KEY and not FOOTBALL_DATA_KEY:
        raise SystemExit("缺少 APIFOOTBALL_KEY 或 FOOTBALL_DATA_KEY")

    day = args.date or datetime.now(TZ).strftime("%Y-%m-%d")
    api = ApiFootball(APIFOOTBALL_KEY) if APIFOOTBALL_KEY else None
    fd = FootballData(FOOTBALL_DATA_KEY) if FOOTBALL_DATA_KEY else None
    candidates, notes, cups, board, plain, focus = build_candidates(api, fd, day)
    deep = [match for match in candidates if not match["caution"]][:MAX_DEEP]
    researching = deep + plain
    picks = []
    analyzed = False
    if researching:
        gather_context(researching)
        if deep:
            picks.extend(analyze(deep)["picks"])
        if plain:
            picks.extend(analyze_plain(plain)["picks"])
        analyzed = True
        notes.append("前三后三先看让球方穿盘，再看大球、大角。没有前三后三的场次先看大球，再用胜负看让球方，最后看大角。")
    if focus:
        for match in focus:
            picks.extend(judge_focus(match))
        analyzed = True
        notes.append("亚冠、法甲、法乙、丹超、葡超按强队身份、进球和近况判断。让球只做让球方或不败。大球看实际总进球。")
    result = {"picks": picks} if analyzed else None

    text = render(day, candidates, researching, result, notes, cups, analyzed, board)
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
