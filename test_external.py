"""外部比价功能自检。直接跑：python test_external.py

外部赔率已改为自动抓取（Pinnacle guest 接口），所以这里全部用构造数据测解析、
换算、配对和定价逻辑，不联网，方便在 CI 里跑。
"""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

spec = importlib.util.spec_from_file_location("fp", Path(__file__).with_name("football_picker.py"))
fp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fp)

FAILED = []


def check(label: str, actual, expected) -> None:
    ok = actual == expected
    if not ok:
        FAILED.append(label)
    print(f"{'ok  ' if ok else 'FAIL'} {label}  得到={actual!r} 期望={expected!r}")


def close(label: str, actual, expected, tol=1e-6) -> None:
    ok = actual is not None and abs(actual - expected) <= tol
    if not ok:
        FAILED.append(label)
    print(f"{'ok  ' if ok else 'FAIL'} {label}  得到={actual!r} 期望≈{expected!r}")


# --------------------------------------------------------------------------
# 1. 美式赔率换算
# --------------------------------------------------------------------------
close("+150 美式转小数", fp.american_to_decimal(150), 2.5)
close("-200 美式转小数", fp.american_to_decimal(-200), 1.5)
close("-100 是平赔", fp.american_to_decimal(-100), 2.0)
check("0 赔率无效", fp.american_to_decimal(0), None)
check("非数字无效", fp.american_to_decimal("abc"), None)

# --------------------------------------------------------------------------
# 2. Pinnacle 盘口映射
# --------------------------------------------------------------------------
def market(key, prices, alternate=False, matchup=7):
    return {
        "key": key,
        "matchupId": matchup,
        "isAlternate": alternate,
        "prices": [{"designation": d, "price": p, "points": None} for d, p in prices],
    }


markets = [
    market("s;0;m", [("home", 150), ("draw", 250), ("away", 300)]),
    market("s;0;ou;2.5", [("over", -110), ("under", -110)]),
    market("s;0;s;-0.5", [("home", 120), ("away", -140)], alternate=True),
    market("s;1;m", [("home", 200), ("draw", 100), ("away", 400)]),
    market("s;1;ou;1.0", [("over", 110), ("under", -130)]),
    market("s;0;tt;0.5;home", [("over", -200), ("under", 170)]),   # 队总入球，不映射
    market("s;0;m", [("over", 110), ("under", -130)]),             # 缺 designations，丢弃
]
lines = fp.pinnacle_lines(markets)

check("全场主客和映射", sorted(lines["HAD"][0]["picks"]), ["A", "D", "H"])
close("主客和赔率换算", lines["HAD"][0]["picks"]["D"], 3.5)
close("大小球档位", lines["HIL"][0]["line"], 2.5)
close("大小球赔率", lines["HIL"][0]["picks"]["H"], 1 + 100 / 110)
close("让球档位取主队让球", lines["HDC"][0]["line"], -0.5)
check("让球被标记为副盘", lines["HDC"][0]["main"], False)
check("半场主客和映射", sorted(lines["FHA"][0]["picks"]), ["A", "D", "H"])
close("半场大细档位", lines["FHL"][0]["line"], 1.0)
check("队总入球不进盘口表", all(m not in lines for m in ("TT", "TT_HOME")), True)
check("缺 designations 的市场被丢弃", len(lines["HAD"]), 1)
check("盘口种类齐全", sorted(lines), ["FHA", "FHL", "HAD", "HDC", "HIL"])

# --------------------------------------------------------------------------
# 3. 让球文字必须翻到客队视角
# --------------------------------------------------------------------------
check("客队让球翻号", fp.side_label("HDC", "A", "-2.0/-2.5", "賓菲加", "艾華卡"), "艾華卡 +2.0/+2.5")
check("客队受让翻号", fp.side_label("HDC", "A", "+0.5", "甲", "乙"), "乙 -0.5")
check("主队保持原样", fp.side_label("HDC", "H", "-2.0/-2.5", "賓菲加", "艾華卡"), "賓菲加 -2.0/-2.5")
check("平手不出现负零", fp.side_label("HDC", "A", "0", "甲", "乙"), "乙 0")
check("大小球标注", fp.side_label("HIL", "L", "2.5", "甲", "乙"), "细2.5")

# --------------------------------------------------------------------------
# 4. 赛程配对：名字与开赛时间都要对得上
# --------------------------------------------------------------------------
kick = datetime(2026, 11, 2, 20, 15, tzinfo=timezone.utc)


def fixture(fid, home, away, start, league="Test"):
    return {"id": fid, "home": home, "away": away,
            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "league": league}


def match_stub(no, home, away, when=kick):
    return {"no": no, "home": home, "away": away, "when": when}


fixtures = [
    fixture(1, "Benfica", "Alverca", kick),
    fixture(2, "Benfica", "Alverca", kick + timedelta(days=3)),      # 同队不同轮
    fixture(3, "Sporting CP", "Porto", kick),
]
matched = fp.match_external_fixture(
    [match_stub("M1", "Benfica", "Alverca")], fixtures)
check("按时间选中同轮", matched["M1"]["id"], 1)

matched = fp.match_external_fixture(
    [match_stub("M1", "Benfica", "Alverca", kick + timedelta(hours=2))], fixtures)
check("开赛时间容差内仍匹配", matched["M1"]["id"], 1)

matched = fp.match_external_fixture(
    [match_stub("M1", "Benfica", "Alverca", kick + timedelta(hours=30))], fixtures)
check("超时容差不匹配", matched, {})

matched = fp.match_external_fixture(
    [match_stub("M1", "Unrelated FC", "Nobody FC")], fixtures)
check("队名不同不匹配", matched, {})

# --------------------------------------------------------------------------
# 5. 融合权重：只有一边就用那一边
# --------------------------------------------------------------------------
close("两边都有按权重融合", fp.blend_lambdas(2.0, 4.0, 0.5)[0], 3.0)
close("偏重外部", fp.blend_lambdas(2.0, 4.0, 0.75)[0], 3.5)
close("只有马会", fp.blend_lambdas(2.4, None)[0], 2.4)
close("只有外部", fp.blend_lambdas(None, 3.1)[0], 3.1)
check("都没有返回空", fp.blend_lambdas(None, None)[0], None)
check("配比说明", fp.blend_lambdas(2.0, 4.0, 0.5)[1], "马会50%+外部50%")

# --------------------------------------------------------------------------
# 6. 档位标识：让球必须保留正负号
# --------------------------------------------------------------------------
check("让球负号保留", fp.line_key("HDC", -2.25), "-2.25")
check("让球正号保留", fp.line_key("HDC", 1.5), "+1.5")
check("大小球同一档", fp.line_key("HIL", 2.5) == fp.line_key("HIL", 2.5), True)
check("无边盘口", fp.line_key("HAD", None), "side")

# --------------------------------------------------------------------------
# 7. 凯利封顶
# --------------------------------------------------------------------------
close("凯利正常", fp.kelly_stake({"edge": 0.05, "odds": 2.0}), 0.05 * 0.25 / 1.0)
check("凯利不超上限", fp.kelly_stake({"edge": 0.9, "odds": 2.0}) <= fp.MAX_STAKE_PCT, True)
check("高赔率额外封顶", fp.kelly_stake({"edge": 0.9, "odds": 6.0}) <= fp.MAX_STAKE_PCT / 3 + 1e-9, True)
check("负期望不给注码", fp.kelly_stake({"edge": -0.05, "odds": 2.0}), 0.0)

# --------------------------------------------------------------------------
# 8. 可疑报价识别
# --------------------------------------------------------------------------
check("高于马会 30% 判可疑", bool(fp.suspicious_reason(
    {"odds": 2.4, "hkjc_odds": 1.62, "edge": 0.3})), True)
check("正常价差不判可疑", fp.suspicious_reason(
    {"odds": 1.70, "hkjc_odds": 1.62, "edge": 0.02}), None)
check("未挂牌且超额期望判可疑", bool(fp.suspicious_reason(
    {"odds": 2.6, "hkjc_odds": None, "edge": 1.35})), True)
check("未挂牌小幅期望放行", fp.suspicious_reason(
    {"odds": 2.6, "hkjc_odds": None, "edge": 0.05}), None)

# --------------------------------------------------------------------------
# 9. 套利：跨公司必须能识别，单家必须被否决
# --------------------------------------------------------------------------
def make_match(no, home, away, hdc=None, hil=None, had=None, external=None):
    books = {}
    if hdc:
        books["HDC"] = [{"line": 1.5, "text": "1.5", "main": True, "picks": hdc, "uid": "1"}]
    if hil:
        books["HIL"] = [{"line": 2.5, "text": "2.5", "main": True, "picks": hil, "uid": "2"}]
    if had:
        books["HAD"] = [{"line": 0.0, "text": "0.0", "main": True, "picks": had, "uid": "3"}]
    match = {
        "no": no,
        "when": kick,
        "kickoff": "11-03 00:00",
        "league": "测试联赛",
        "league_en": "Test",
        "tournament": "Test",
        "country": "Test",
        "home": home,
        "away": away,
        "home_ch": home,
        "away_ch": away,
        "books": books,
        "external": external or {},
    }
    match["model"] = {
        "pmf_total": fp.poisson_pmf(2.6),
        "margins": fp.margin_pmf(1.6, 1.0),
        "margins_half": fp.margin_pmf(0.8, 0.5),
        "pmf_half": fp.poisson_pmf(1.3),
        "pmf_corner": None,
        "lam_home": 1.6,
        "lam_away": 1.0,
        "share": 0.45,
        "half_fitted": False,
        "total_fit": {"points": 5, "residual": 0.005},
        "corner_fit": {"points": 0, "residual": None},
        "half_fit": {"points": 5, "residual": 0.005},
    }
    return match


def pin_books(mtype, line, text, picks):
    return {mtype: [{"line": line, "text": text, "main": True, "picks": picks, "uid": "x"}]}


# 跨公司：马会买大，Pinnacle 买细，两边倒数之和 < 1
hk = make_match("T1", "主队", "客队", hil={"H": 2.15, "L": 1.62})
hk["external"] = pin_books("HIL", 2.5, "2.5", {"H": 1.62, "L": 2.10})
model = hk["model"]
pool = fp.evaluate_book(hk, model, hk["books"], fp.HKJC_BOOK)
pool += fp.evaluate_book(hk, model, hk["external"], fp.PINNACLE_BOOK)
arms, rejected = fp.arb_scan([hk], pool)
check("跨公司套利识别", len(arms), 1)
check("假套利未误报", len(rejected), 0)
close("套利倒数之和", arms[0]["inverse"], 1 / 2.15 + 1 / 2.10)
close("套利利润率", arms[0]["profit"], 1 / (1 / 2.15 + 1 / 2.10) - 1)
check("套利两腿跨公司", len({leg["book"] for leg in arms[0]["legs"]}), 2)
close("套利注码占比合计", sum(leg["share"] for leg in arms[0]["legs"]), 1.0)

# 同一家公司两边都高赔 = 负抽水，只能是数据错误。
# 这场马会没开大细，所以两边只能都取自 Pinnacle，才能测到「单家负抽水」。
hk_bad = make_match("T2", "主队", "客队", hdc={"H": 1.9, "A": 1.9})
hk_bad["external"] = pin_books("HIL", 2.5, "2.5", {"H": 2.45, "L": 2.00})
pool_bad = fp.evaluate_book(hk_bad, hk_bad["model"], hk_bad["books"], fp.HKJC_BOOK)
pool_bad += fp.evaluate_book(hk_bad, hk_bad["model"], hk_bad["external"], fp.PINNACLE_BOOK)
arms_bad, rejected_bad = fp.arb_scan([hk_bad], pool_bad)
check("单家假套利不通过", len(arms_bad), 0)
check("单家假套利被记录", len(rejected_bad), 1)
check("否决理由含负抽水", "负抽水" in rejected_bad[0]["reject"], True)

# 三边盘口套利：三条腿必须分别来自三家，同家凑齐等于负抽水
hk3 = make_match("T3", "甲", "乙", had={"H": 2.0, "D": 3.6, "A": 4.0})
pool3 = fp.evaluate_book(hk3, hk3["model"], hk3["books"], fp.HKJC_BOOK)
for book_name, side, odds in (("A公司", "H", 2.30), ("B公司", "D", 4.20), ("C公司", "A", 5.00)):
    leg = {"HAD": [{"line": 0.0, "text": "0.0", "main": True, "picks": {side: odds}, "uid": side}]}
    pool3 += fp.evaluate_book(hk3, hk3["model"], leg, book_name)
arms3, _ = fp.arb_scan([hk3], pool3)
check("三边套利识别", len(arms3), 1)
close("三边套利倒数之和", arms3[0]["inverse"], 1 / 2.30 + 1 / 4.20 + 1 / 5.00)
check("三边套利腿数", len(arms3[0]["legs"]), 3)

# --------------------------------------------------------------------------
# 10. 价值方向筛选：只认马会、必须两家同档位、期望要有上限
# --------------------------------------------------------------------------
def value_row(**kw):
    row = {"book": fp.HKJC_BOOK, "ref_odds": 3.0, "conf": "high",
           "edge": 0.05, "suspicious": None}
    row.update(kw)
    return row


check("合格方向被选中", len(fp.value_rows([value_row()])), 1)
check("Pinnacle 自家不推荐", len(fp.value_rows([value_row(book=fp.PINNACLE_BOOK)])), 0)
check("只有一边挂盘不算", len(fp.value_rows([value_row(ref_odds=None)])), 0)
check("分布不可信不算", len(fp.value_rows([value_row(conf="low")])), 0)
check("期望过小不算", len(fp.value_rows([value_row(edge=0.001)])), 0)
check("期望高得离谱不算", len(fp.value_rows([value_row(edge=fp.MAX_VALUE_EDGE + 0.05)])), 0)
check("可疑报价不算", len(fp.value_rows([value_row(suspicious="过期")])), 0)

# --------------------------------------------------------------------------
# 11. 缺主客和盘不能硬按 50/50 拆分
# --------------------------------------------------------------------------
no_had = make_match("T4", "甲", "乙", hil={"H": 2.0, "L": 1.8})
check("缺主客和返回空模型", fp.build_model(no_had, []), None)


def hk_only(no, hil, had):
    stub = make_match(no, "甲", "乙", hil=hil, had=had)
    stub["external"] = {}
    return stub


multi_hil = [
    {"line": line, "text": f"{line}", "main": line == 2.5, "picks": picks, "uid": f"u{line}"}
    for line, picks in ((2.0, {"H": 1.62, "L": 2.15}),
                        (2.5, {"H": 2.10, "L": 1.65}),
                        (3.0, {"H": 2.80, "L": 1.38}))
]
hk_both = hk_only("T5", {"H": 2.1, "L": 1.65},
                  {"H": 2.0, "D": 3.2, "A": 4.0})
hk_both["books"]["HIL"] = multi_hil
check("有主客和能建模", fp.build_model(hk_both, []) is not None, True)
only_ext_model = fp.build_model(hk_both, [], use_hk=False)
check("关掉马会后没主客和则为空", only_ext_model, None)

# --------------------------------------------------------------------------
# 12. 下注门槛不能把锐价基准算进去
# --------------------------------------------------------------------------
base = {"conf": "high", "mtype": "HIL", "edge": 0.08, "odds": 2.2, "prob": 0.5}
check("马会合格方向可下", fp.meets_advice(dict(base, book=fp.HKJC_BOOK)), True)
check("锐价基准不下", fp.meets_advice(dict(base, book=fp.PINNACLE_BOOK)), False)
check("锐价提示为观察", fp.stake_hint(dict(base, book=fp.PINNACLE_BOOK)), "仅观察（锐价基准）")

# --------------------------------------------------------------------------
# 13. 自动抓取失败不能拖垮主流程
# --------------------------------------------------------------------------
original = fp.pinnacle_raw_matchups
try:
    fp.pinnacle_raw_matchups = lambda: (_ for _ in ()).throw(RuntimeError("断网"))
    notes = []
    books, corners = fp.fetch_external_books([{"no": "T1", "home": "A", "away": "B", "when": kick}], notes)
    check("抓取失败返回空", (books, corners), ({}, {}))
    check("抓取失败写进备注", bool(notes) and "只按马会定价" in notes[0], True)
finally:
    fp.pinnacle_raw_matchups = original

print()
if FAILED:
    print(f"失败 {len(FAILED)} 项：")
    for name in FAILED:
        print("  -", name)
    raise SystemExit(1)
print("全部通过")
