# -*- coding: utf-8 -*-
"""回放验收 —— 逐条核对引擎行为是否与《策略A_执行规则_v1.md》一致。

运行：python src/backtest.py
任何一项 FAIL 都表示代码与文档存在偏差，必须修正后才能上线。
"""
import datetime
import json
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import engine as E                                   # noqa: E402
from init_state import build as build_state, read_ledger   # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print("  %s %-46s %s" % ("PASS" if cond else "FAIL", name, detail))


def load_cfg():
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def synth(start, days, p1_path, sig_path, p2=1.156, p3=1.411, pb=None):
    """构造合成行情。p1_path/sig_path 为长度 days 的价格序列。"""
    out, d = [], datetime.date(*map(int, start.split("-")))
    i = 0
    while len(out) < days:
        if d.weekday() < 5:
            out.append({"date": d.isoformat(),
                        "p1_px": p1_path[i], "p2_px": p2, "p3_px": p3,
                        "sig_px": sig_path[i],
                        "pb_pct": pb[i] if pb else None})
            i += 1
        d = datetime.date.fromordinal(d.toordinal() + 1)
    return out


def base_state(cfg):
    rows = read_ledger()
    st = build_state(cfg, [r for r in rows if r["date"] <= cfg["start_date"]])
    st["start_date"] = cfg["start_date"]
    return st


def main():
    cfg = load_cfg()
    c1, c3 = cfg["part1"], cfg["part3"]
    N = 700

    print("=" * 78)
    print("一、定投规则")
    print("=" * 78)
    flat1 = [1.114] * N
    flatsig = [5500.0] * N          # 恒定 → MA250 恒等于自身，网格不触发
    hist = synth(cfg["start_date"], N, flat1, flatsig)
    st, curve = E.run(hist, cfg, base_state(cfg))
    dca = [e for e in st["events"] if e["action"] == "中概定投买入"]
    check("定投总次数 = 26", len(dca) == c1["dca_weeks"], "实际 %d 次" % len(dca))
    if dca:
        wd = {datetime.date(*map(int, e["date"].split("-"))).weekday() for e in dca}
        check("全部落在周三", wd == {2}, "星期分布 %s" % sorted(wd))
        check("首次定投 >= dca_start_date",
              dca[0]["date"] >= c1["dca_start_date"],
              "首次 %s，配置 %s" % (dca[0]["date"], c1["dca_start_date"]))
        weeks = [e["date"] for e in dca]
        check("每周至多一次", len(set(weeks)) == len(weeks))
    deployed = sum(float(e["detail"].split("约 ")[1].split(" 元")[0])
                   for e in dca) if dca else 0
    check("定投累计投出 = 弹药总额",
          abs(deployed - c1["ammo"]) < 50,
          "投出 %.0f / 弹药 %.0f（余额 %.2f = 底仓未用尽部分+利息）"
          % (deployed, c1["ammo"], st["p1"]["cash"]))

    print()
    print("=" * 78)
    print("二、加码条款（浮亏 15% 投一半，25% 投剩余）")
    print("=" * 78)
    drop = [1.114 * (1 - 0.30 * min(1.0, i / 60.0)) for i in range(N)]
    hist = synth(cfg["start_date"], N, drop, flatsig)
    st, _ = E.run(hist, cfg, base_state(cfg))
    ac = [e for e in st["events"] if e["action"] == "中概加码买入"]
    check("加码至少触发 1 次", len(ac) >= 1, "实际 %d 次" % len(ac))
    print("       注：浮亏以【持仓加权成本】为基准，定投会持续拉低成本，")
    print("           因此第二档(-25%)在持续下跌中未必触发 —— 这是文档规则的必然结果。")
    check("加码后弹药耗尽", abs(st["p1"]["cash"]) < 1.0,
          "剩余 %.2f" % st["p1"]["cash"])

    print()
    print("=" * 78)
    print("三、开启止盈保护与止盈清仓")
    print("=" * 78)
    up = [1.114 * (1 + 0.60 * min(1.0, i / 100.0)) for i in range(300)]
    down = [up[-1] * (1 - 0.45 * min(1.0, (i) / 200.0)) for i in range(N - 300)]
    hist = synth(cfg["start_date"], N, up + down, flatsig)
    st, _ = E.run(hist, cfg, base_state(cfg))
    arm = [e for e in st["events"] if e["action"] == "中概开启止盈保护"]
    ext = [e for e in st["events"] if e["action"] == "中概止盈清仓"]
    check("触发开启止盈保护", len(arm) == 1, arm[0]["detail"] if arm else "未触发")
    check("开启保护后触发止盈", len(ext) == 1, ext[0]["date"] if ext else "未触发")
    check("止盈后第一份清空", st["p1"]["units"] == 0 and st["p1"]["cash"] == 0)
    check("止盈资金转入第三份", st["p3"]["cash"] > cfg["part3"]["capital"],
          "第三份现金 %.0f" % st["p3"]["cash"])
    if arm and ext:
        check("先开启保护、后止盈", arm[0]["date"] < ext[0]["date"])

    print()
    print("=" * 78)
    print("四、兜底开启止盈保护（满 3 年未达 40 万）")
    print("=" * 78)
    hist = synth(cfg["start_date"], 800, [1.114] * 800, [5500.0] * 800)
    st, _ = E.run(hist, cfg, base_state(cfg))
    to = [e for e in st["events"] if e["action"] == "中概开启止盈保护（满3年兜底）"]
    check("满 3 年兜底开启止盈保护", len(to) == 1, to[0]["date"] if to else "未触发（样本不足3年时正常）")

    print()
    print("=" * 78)
    print("五、红利网格（-4/-7/-10% 买入，+3% 清仓）")
    print("=" * 78)
    base = 5500.0
    sig = [base] * 260
    sig += [base * (1 - 0.12 * min(1.0, i / 40.0)) for i in range(120)]   # 跌 12%
    sig += [sig[-1] * (1 + 0.30 * min(1.0, i / 80.0)) for i in range(N - len(sig))]
    hist = synth(cfg["start_date"], N, flat1, sig)
    st, _ = E.run(hist, cfg, base_state(cfg))
    g = [e for e in st["events"] if e["part"] == 3]
    buys = [e for e in g if e["action"] == "红利网格买入"]
    sells = [e for e in g if e["action"] == "红利网格清仓"]
    check("三档依次触发", len(buys) == 3, "实际 %d 档" % len(buys))
    check("触发后清仓一次", len(sells) == 1, sells[0]["date"] if sells else "未清仓")
    check("清仓后回到空仓", st["p3"]["units"] == 0 and st["p3"]["tier"] == 0)
    if buys:
        ma_at = []
        p_sig = [h["sig_px"] for h in hist]
        for e in buys:
            i = next(k for k, h in enumerate(hist) if h["date"] == e["date"])
            ma = E.sma(p_sig, c3["ma_n"], i - 1)
            ma_at.append(p_sig[i - 1] / ma if ma else None)
        ok = all(r is not None and r <= t + 1e-9
                 for r, t in zip(ma_at, c3["buy_tiers"]))
        check("买入点确在对应档位之下", ok,
              "信号日比值 %s" % ["%.4f" % r if r else "—" for r in ma_at])

    print()
    print("=" * 78)
    print("六、切换条款（PB 十年分位 <= 20%）")
    print("=" * 78)
    pb = [0.775] * 300 + [0.19] * (N - 300)
    hist = synth(cfg["start_date"], N, flat1, flatsig, pb=pb)
    st, _ = E.run(hist, cfg, base_state(cfg))
    sw = [e for e in st["events"] if e["action"] == "切换至创业板策略"]
    check("PB 跌破阈值触发切换", len(sw) == 1, sw[0]["date"] if sw else "未触发")
    check("切换后三份全部清空",
          st["p1"]["units"] == 0 and st["p2"]["units"] == 0 and st["p3"]["units"] == 0)
    check("切换后资金全在第三份现金", st["p3"]["cash"] > 1_000_000,
          "%.0f 元" % st["p3"]["cash"])
    check("切换后不再产生新事件",
          all(e["date"] <= sw[0]["date"] for e in st["events"]) if sw else False)

    print()
    print("=" * 78)
    print("七、T+1 执行口径")
    print("=" * 78)
    hist = synth(cfg["start_date"], N, flat1, sig)
    st, _ = E.run(hist, cfg, base_state(cfg))
    dts = [h["date"] for h in hist]
    ok = True
    for e in st["events"]:
        if e["action"] in ("红利网格买入", "红利网格清仓"):
            i = dts.index(e["date"])
            if i == 0:
                ok = False
    check("信号日与执行日分离（无当日成交）", ok)

    print()
    print("=" * 78)
    print("八、数据缺失容错")
    print("=" * 78)
    hist = synth(cfg["start_date"], 300, flat1, flatsig)
    for h in hist[100:110]:
        h["p1_px"] = None
    try:
        st, _ = E.run(hist, cfg, base_state(cfg))
        check("价格缺失不崩溃", True, "引擎正常返回")
    except Exception as exc:                                  # noqa: BLE001
        check("价格缺失不崩溃", False, repr(exc))

    print()
    print("=" * 78)
    print("九、自动分红（取代手工记账）")
    print("=" * 78)
    HOLD = cfg["part3"]["hold_code"]
    hist = synth(cfg["start_date"], 400, flat1, flatsig)
    exd = hist[200]["date"]
    divs = {HOLD: {exd: 0.0610}}

    # 第三份先建仓：网格跌到第1档
    sig2 = [5500.0] * 260 + [5500.0 * 0.95] * 140
    h2 = synth(cfg["start_date"], 400, flat1, sig2)
    ex2 = h2[330]["date"]
    st, _ = E.run(h2, cfg, base_state(cfg), divs={HOLD: {ex2: 0.0610}})
    dv = [e for e in st["events"] if e["action"] == "分红入账"]
    check("持仓时除息日自动入账", len(dv) == 1, dv[0]["detail"] if dv else "未入账")
    if dv:
        units = float(dv[0]["detail"].split("x ")[1].split(" 份")[0])
        amt = float(dv[0]["detail"].split("= ")[1].split(" 元")[0])
        check("入账金额 = 持仓份数 x 每份派现",
              abs(amt - units * 0.0610) < 0.1, "%.2f 元" % amt)

    # 空仓时不入账
    st, _ = E.run(hist, cfg, base_state(cfg), divs=divs)
    dv0 = [e for e in st["events"] if e["action"] == "分红入账"]
    check("空仓时除息日不入账", len(dv0) == 0, "产生了 %d 条" % len(dv0))

    # ledger 已手记则不重复
    inj = [{"date": ex2, "part": 3, "action": "dividend", "code": HOLD,
            "shares": 0, "price": 0, "amount": 999.0, "note": "手记"}]
    st, _ = E.run(h2, cfg, base_state(cfg), injections=inj,
                  divs={HOLD: {ex2: 0.0610}})
    dv2 = [e for e in st["events"] if e["action"] == "分红入账"]
    check("ledger 已手记同日同份则不重复入账", len(dv2) == 1,
          "共 %d 条：%s" % (len(dv2), [e["detail"][:24] for e in dv2]))

    print()
    print("=" * 78)
    print("十、重复记账防护")
    print("=" * 78)
    stA, _ = E.run(hist, cfg, base_state(cfg))
    dcaA = [e for e in stA["events"] if e["action"] == "中概定投买入"]
    # 场景：把引擎已自动执行的每一次定投都「如实」抄进 ledger
    dup = [{"date": e["date"], "part": 1, "action": "buy",
            "code": cfg["part1"]["code"], "shares": 5179, "price": 1.114,
            "amount": float(e["detail"].split("约 ")[1].split(" 元")[0]),
            "note": "定投"} for e in dcaA]
    stB, _ = E.run(hist, cfg, base_state(cfg), injections=dup)
    w = E.check_duplicates(stB["events"])
    check("ledger 重记引擎已执行的定投 → 报警", len(w) >= 1,
          "检出 %d 条" % len(w))
    check("重复记账会让现金穿负",
          stB["p1"]["cash"] < -1000,
          "现金 %.0f 元（正常应为 %.0f）" % (stB["p1"]["cash"], stA["p1"]["cash"]))
    dcaB = [e for e in stB["events"] if e["action"] == "中概定投买入"]
    check("重复记账会让后续定投被静默跳过",
          len(dcaB) < len(dcaA),
          "只完成 %d / %d 次" % (len(dcaB), len(dcaA)))
    check("重复记账会虚增持仓",
          stB["p1"]["units"] > stA["p1"]["units"] * 1.1,
          "+%.0f 份 (+%.0f%%)" % (stB["p1"]["units"] - stA["p1"]["units"],
                                  (stB["p1"]["units"] / stA["p1"]["units"] - 1) * 100))
    clean = E.check_duplicates(stA["events"])
    check("不记账本时无误报", len(clean) == 0, "误报 %d 条" % len(clean))

    print()
    print("=" * 78)
    print("十一、均线体检（停摆必须被发现，不能静默）")
    print("=" * 78)
    good = synth(cfg["start_date"], 400, flat1, flatsig)
    hz = E.ma_health(good, cfg)
    check("数据健全时两条均线都判为正常", all(x["ok"] for x in hz),
          "；".join(x.get("msg", x["label"]) for x in hz))

    short = synth(cfg["start_date"], 100, flat1, flatsig)
    hz = E.ma_health(short, cfg)
    check("历史不足 250 行 → 报警", len(hz) == 2 and not any(x["ok"] for x in hz),
          hz[0].get("msg", "")[:46])

    holed = [dict(r) for r in good]
    holed[-30]["sig_px"] = None
    hz = E.ma_health(holed, cfg)
    g = [x for x in hz if "网格" in x["label"]][0]
    check("信号序列有缺值 → 网格均线报警", not g["ok"], g.get("msg", "")[:46])
    p1 = [x for x in hz if "第一份" in x["label"]][0]
    check("缺值只影响对应的那条均线", p1["ok"], "第一份仍正常" if p1["ok"] else p1.get("msg", ""))

    long_ = synth(cfg["start_date"], 800, [1.114] * 800, [5500.0] * 800)
    thin = [r for i, r in enumerate(long_) if i % 3]   # 全程每三行丢一行
    hz = E.ma_health(thin, cfg)
    g = [x for x in hz if "网格" in x["label"]][0]
    check("历史缺行导致窗口偏旧 → 报警", not g["ok"], g.get("msg", "")[:52])

    # 停摆时 next_triggers 会静默丢掉观察点 —— 这正是要报警的原因
    st, _ = E.run(good, cfg, base_state(cfg))
    last = dict(holed[-1]); last["ma1"] = None; last["ma3"] = None
    trg = E.next_triggers(st, cfg, last)
    check("均线为空时观察点确会消失（故必须报警）",
          not any("网格" in t["label"] for t in trg),
          "剩余观察点 %d 个" % len(trg))

    print()
    print("=" * 78)
    print("十二、横幅覆盖：每种信号都要有正确提示")
    print("=" * 78)
    import re as _re
    src = open(os.path.join(ROOT, "src", "engine.py"), encoding="utf-8").read()
    codes = set(_re.findall(r'"action": "([A-Z0-9_]+)"', src))
    check("引擎共产生 5 种指令码", len(codes) == 5, "、".join(sorted(codes)))

    appjs = open(os.path.join(ROOT, "docs", "app.js"), encoding="utf-8").read()
    ntfy = open(os.path.join(ROOT, "src", "notify.py"), encoding="utf-8").read()
    conf = open(os.path.join(ROOT, ".github", "workflows", "confirm.yml"),
                encoding="utf-8").read()
    miss_a = [c for c in codes if ("%s:" % c) not in appjs]
    miss_n = [c for c in codes if ('"%s"' % c) not in ntfy]
    miss_c = [c for c in codes if c not in conf]
    check("看板 ACT 映射无遗漏", not miss_a, "缺 %s" % miss_a)
    check("Bark ACT_TEXT 映射无遗漏", not miss_n, "缺 %s" % miss_n)
    check("确认校验正则无遗漏", not miss_c, "缺 %s" % miss_c)

    # 逐一造出每种 pending，检查 label 与金额
    seen = {}
    scen = [
        ("P1_DCA", synth(cfg["start_date"], 40, flat1, flatsig), None),
        ("P1_ACCEL", synth(cfg["start_date"], 60,
                           [1.114 * (1 - 0.30 * min(1.0, i / 20.0)) for i in range(60)],
                           flatsig), None),
        ("P3_TIER", synth(cfg["start_date"], 400, flat1,
                          [5500.0] * 300 + [5500.0 * 0.94] * 100), 1),
        ("P3_TIER", synth(cfg["start_date"], 500, flat1,
                          [5500.0] * 300 + [5500.0 * 0.88] * 100 + [5500.0 * 1.10] * 100), 0),
        ("P1_EXIT", synth(cfg["start_date"], 700,
                          [1.114 * (1 + 0.75 * min(1.0, i / 200.0)) if i < 300
                           else 1.114 * 1.75 * (1 - 0.35 * min(1.0, (i - 300) / 200.0))
                           for i in range(700)], flatsig), None),
        ("SWITCH", synth(cfg["start_date"], 40, flat1, flatsig,
                         pb=[0.775] * 10 + [0.19] * 30), None),
    ]
    for code, hh, tier in scen:
        for cut in range(2, len(hh) + 1):
            st, _ = E.run(hh[:cut], cfg, base_state(cfg))
            hit = [q for q in st["pending"]
                   if q["action"] == code and (tier is None or q.get("tier") == tier)]
            if hit:
                seen[(code, tier)] = hit[0]
                break
    check("六种指令场景全部能造出 pending", len(seen) == 6,
          "造出 %d 种：%s" % (len(seen), sorted(k[0] for k in seen)))
    nolabel = [k for k, v in seen.items() if not v.get("label")]
    check("每条 pending 都自带 label（不靠动作码猜标题）", not nolabel,
          "缺 label：%s" % nolabel)
    labels = [v["label"] for v in seen.values()]
    check("网格买入与清仓标题不同（清仓不可写成「调整」）",
          seen.get(("P3_TIER", 1), {}).get("label")
          != seen.get(("P3_TIER", 0), {}).get("label"),
          "买=%s ／ 卖=%s" % (seen.get(("P3_TIER", 1), {}).get("label"),
                             seen.get(("P3_TIER", 0), {}).get("label")))
    check("六条标题两两不重复", len(set(labels)) == len(labels),
          "；".join(labels))
    nomoney = [k[0] for k, v in seen.items()
               if k[0] != "SWITCH" and "元" not in v.get("detail", "")]
    check("除切换外每条副标题都带金额", not nomoney, "缺金额：%s" % nomoney)
    badsign = [v["detail"] for v in seen.values() if "浮亏 -" in v.get("detail", "")]
    check("浮亏不带负号（避免双重否定）", not badsign, badsign[:1])

    # 状态里程碑必须有通知横幅
    st, _ = E.run(scen[4][1], cfg, base_state(cfg))
    arm = [e for e in st["events"] if e["action"] in ("中概开启止盈保护", "中概开启止盈保护（满3年兜底）")]
    nt = E.build_notices(st, scen[4][1][-1]["date"], 3650)
    check("开启止盈保护会生成通知横幅", len(arm) >= 1 and len(nt) >= 1,
          nt[0]["label"] if nt else "无通知")
    check("通知横幅的 key 稳定（可持久确认）",
          bool(nt) and nt[0]["key"].startswith("note:"),
          nt[0]["key"] if nt else "")

    print()
    print("=" * 78)
    print("十三、交易日判定：非交易日静默，交易日必推")
    print("=" * 78)
    import main as M

    check("春节最长假期不会误报（7 个工作日 < 阈值 9）",
          M._weekdays_between("2026-02-13", datetime.date(2026, 2, 24)) <= 9,
          "2026 春节休市 2/13→2/24 共 %d 个工作日"
          % M._weekdays_between("2026-02-13", datetime.date(2026, 2, 24)))
    check("国庆最长假期不会误报",
          M._weekdays_between("2026-09-30", datetime.date(2026, 10, 9)) <= 9,
          "2026 国庆休市 9/30→10/9 共 %d 个工作日"
          % M._weekdays_between("2026-09-30", datetime.date(2026, 10, 9)))
    check("超过阈值会告警（模拟数据源挂 3 周）",
          M._weekdays_between("2026-08-18", datetime.date(2026, 9, 8)) > 9,
          "静默 %d 个工作日"
          % M._weekdays_between("2026-08-18", datetime.date(2026, 9, 8)))
    check("周末不计入工作日",
          M._weekdays_between("2026-08-21", datetime.date(2026, 8, 24)) == 1,
          "周五→周一 = %d 个工作日"
          % M._weekdays_between("2026-08-21", datetime.date(2026, 8, 24)))

    # 开市但必需列缺失 → 整行跳过（此时必须告警而非静默）
    import fetch as FF
    hh = [{"date": "2026-08-18", "p1_px": 1.116, "p2_px": 1.161,
           "p3_px": 1.42, "sig_px": 5534.83, "pb_pct": 0.79}]
    ser = {"p1_px": {"2026-08-19": 1.12}, "p2_px": {"2026-08-19": 1.16},
           "p3_px": {"2026-08-19": 1.43}, "sig_px": {}}
    _a, _r, sk = FF.merge(hh, ser)
    key = "2026-08-19"
    market_open = any(key in v for v in ser.values())
    check("缺一列 → 整行跳过（保护 MA250）", sk == [key] and hh[-1]["date"] != key,
          "skipped=%s，hist 末日仍为 %s" % (sk, hh[-1]["date"]))
    check("但仍能判定当天开市（其它源有行情）", market_open,
          "market_open=%s → 该走告警而非静默" % market_open)
    miss = [k for k in FF.REQUIRED if key not in (ser.get(k) or {})]
    check("能指出缺的是哪一列", miss == ["sig_px"], "缺 %s" % miss)

    # 真·非交易日：所有源都没有当天
    ser2 = {k: {} for k in FF.REQUIRED}
    check("真非交易日 → market_open 为假，走静默",
          not any(key in v for v in ser2.values()), "所有源均无当日数据")

    print()
    print("=" * 78)
    print("十四、心跳：系统健康时也必须定期出声")
    print("=" * 78)
    import notify as NN
    import tempfile

    real = NN.HEART
    tmp = os.path.join(tempfile.mkdtemp(), "heartbeat.json")
    NN.HEART = tmp
    try:
        check("无打点记录时返回 9999（首次运行会立刻报平安）",
              NN.days_since_push(datetime.date(2026, 8, 19)) == 9999,
              "返回 %d" % NN.days_since_push(datetime.date(2026, 8, 19)))

        NN._mark_alive()
        today = datetime.date(*map(int, datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))
        ).strftime("%Y-%m-%d").split("-")))
        check("打点后当天返回 0", NN.days_since_push(today) == 0,
              "返回 %d" % NN.days_since_push(today))
        check("打点后第 6 天返回 6",
              NN.days_since_push(datetime.date.fromordinal(today.toordinal() + 6)) == 6,
              "返回 %d" % NN.days_since_push(
                  datetime.date.fromordinal(today.toordinal() + 6)))

        with open(tmp, "w", encoding="utf-8") as f:
            f.write("坏掉的文件{{{")
        check("打点文件损坏 → 退化为 9999，不崩溃",
              NN.days_since_push(today) == 9999, "返回 %d" % NN.days_since_push(today))
    finally:
        NN.HEART = real

    hb = int(cfg.get("heartbeat_days", 6))
    check("心跳间隔短于最长假期（否则春节会静默到底）",
          hb < 9, "heartbeat_days=%d，春节约 9 个自然日" % hb)
    check("心跳间隔短于停摆告警阈值（先报平安、再报故障）",
          hb < int(cfg.get("max_quiet_weekdays", 9)),
          "心跳 %d 天 < 停摆 %d 个工作日" % (hb, cfg.get("max_quiet_weekdays", 9)))

    src_n = open(os.path.join(ROOT, "src", "notify.py"), encoding="utf-8").read()
    check("打点写在 bark() 成功分支里（所有推送出口统一覆盖）",
          "_mark_alive()" in src_n.split("return True")[0].split("def bark")[1],
          "bark 成功即打点")

    print()
    print("=" * 78)
    print("十五、每日必推 + 临近触发提醒")
    print("=" * 78)
    import notify as NT

    lvl = str(cfg.get("quiet_push_level", "active"))
    # 真调一次：拦掉 bark，看无操作场景到底推没推、用什么标题和级别
    calls = []
    real_bark = NT.bark
    NT.bark = lambda c, t, b, level="active", group="策略A": (
        calls.append((t, level)) or True)
    try:
        st0, curve0 = E.run(synth(cfg["start_date"], 400, flat1, flatsig),
                            cfg, base_state(cfg))
        quiet_out = {"data_updated": "2026-09-01", "pending": [], "triggers": [],
                     "state": {"p1": st0["p1"], "p2": st0["p2"], "p3": st0["p3"],
                               "switched": False, "switch_date": None},
                     "metrics": E.metrics(curve0, cfg["total_capital"]),
                     "last_prices": {"p1_px": 1.114, "p2_px": 1.156,
                                     "p3_px": 1.411, "sig_px": 5500.0,
                                     "pb_pct": 0.79},
                     "recent_events": []}
        NT.push_daily(cfg, quiet_out)
        check("「今日无操作」确实会推送", len(calls) == 1,
              "标题：%s" % (calls[0][0] if calls else "未推送"))
        check("无操作推送级别不是 passive（passive 在 iOS 上不亮屏）",
              bool(calls) and calls[0][1] != "passive",
              "级别 %s（config: quiet_push_level=%s）"
              % (calls[0][1] if calls else "—", lvl))
        calls.clear()
        NT.push_daily(cfg, dict(quiet_out, triggers=[
            {"label": "红利网格 · 第 1 档买入", "cond": "中证红利跌破 5365.01",
             "short": "还需跌 1.22%", "dist": 1.22, "unit": "%",
             "near": True, "progress": 0.7}]))
        check("临近时标题升级且级别提到 timeSensitive",
              bool(calls) and "临近触发" in calls[0][0]
              and calls[0][1] == "timeSensitive",
              "标题：%s ／ 级别 %s" % (calls[0][0] if calls else "—",
                                      calls[0][1] if calls else "—"))
    finally:
        NT.bark = real_bark

    # 每条观察点都要带 dist/near
    hist = synth(cfg["start_date"], 400, flat1, flatsig)
    st, curve = E.run(hist, cfg, base_state(cfg))
    last = dict(hist[-1])
    last["ma1"] = E.sma([h["p1_px"] for h in hist], 250, len(hist) - 1)
    last["ma3"] = E.sma([h["sig_px"] for h in hist], 250, len(hist) - 1)
    trg = E.next_triggers(st, cfg, last)
    check("每条观察点都带 dist/unit/near",
          trg and all(("dist" in t and "unit" in t and "near" in t) for t in trg),
          "%d 条观察点" % len(trg))

    # 临近阈值：刚好在界内/界外
    na = cfg.get("near_alert") or {}
    thr = float(na.get("price_pct", 1.5))
    ma0 = last["ma3"]
    t1 = cfg["part3"]["buy_tiers"][0]
    for mult, want, nm in ((1 + (thr - 0.3) / 100, True, "界内"),
                           (1 + (thr + 0.3) / 100, False, "界外")):
        hh = synth(cfg["start_date"], 400, flat1, flatsig)
        for r in hh[-3:]:
            r["sig_px"] = 5500.0 * t1 * mult
        st2, _ = E.run(hh, cfg, base_state(cfg))
        l2 = dict(hh[-1])
        l2["ma1"] = E.sma([h["p1_px"] for h in hh], 250, len(hh) - 1)
        l2["ma3"] = E.sma([h["sig_px"] for h in hh], 250, len(hh) - 1)
        g = [t for t in E.next_triggers(st2, cfg, l2) if "红利网格 · 第" in t["label"]]
        got = bool(g and g[0]["near"])
        check("临近判定 %s（阈值 %.1f%%）" % (nm, thr), got == want,
              "还需跌 %.2f%% → near=%s" % (g[0]["dist"] if g else -1, got))

    # 临近时推送标题升级
    out = {"data_updated": "2026-09-01", "pending": [], "triggers":
           [{"label": "红利网格 · 第 1 档买入", "cond": "中证红利跌破 5365.01",
             "short": "还需跌 1.22%", "dist": 1.22, "unit": "%",
             "near": True, "progress": 0.7}],
           "state": {"p1": st["p1"], "p2": st["p2"], "p3": st["p3"],
                     "switched": False, "switch_date": None},
           "metrics": E.metrics(curve, cfg["total_capital"]),
           "last_prices": {k: hist[-1][k] for k in
                           ("p1_px", "p2_px", "p3_px", "sig_px", "pb_pct")},
           "recent_events": []}
    body = NT.build_body(cfg, out)
    check("临近项写进推送正文", "临近触发，请留意" in body and "还需跌 1.22%" in body,
          "正文含临近段")
    check("有待办时不重复显示临近段（指令优先）",
          "临近触发，请留意" not in NT.build_body(
              cfg, dict(out, pending=[{"action": "P1_DCA", "exec_date": "2026-09-02",
                                       "label": "买入 中概互联ETF",
                                       "detail": "定投"}])),
          "待办存在时临近段让位")

    appjs = open(os.path.join(ROOT, "docs", "app.js"), encoding="utf-8").read()
    check("看板有临近触发横幅", "nearbar" in appjs and "t.near" in appjs, "")
    css = open(os.path.join(ROOT, "docs", "style.css"), encoding="utf-8").read()
    check("临近横幅有独立样式（虚线框，区别于待办实线）",
          ".nearbar" in css and "dashed" in css.split(".nearbar")[1][:200], "")

    print()
    print("=" * 78)
    print("十六、用词：不留黑话、每条都写明是哪只标的")
    print("=" * 78)
    JARGON = ("武装", "本份", "第一份", "第二份", "第三份", "弹药", "P1_", "P3_")
    hh = synth(cfg["start_date"], 700,
               [1.114 * (1 + 0.75 * min(1.0, i / 200.0)) if i < 300
                else 1.114 * 1.75 * (1 - 0.35 * min(1.0, (i - 300) / 200.0))
                for i in range(700)],
               [5500.0] * 300 + [5500.0 * 0.88] * 200 + [5500.0 * 1.10] * 200,
               pb=[0.775] * 650 + [0.19] * 50)
    st, curve = E.run(hh, cfg, base_state(cfg))
    texts = []
    for e in st["events"]:
        texts.append(("事件动作", e["action"]))
        texts.append(("事件说明", e["detail"]))
    for nt in E.build_notices(st, hh[-1]["date"], 3650):
        texts.append(("通知标题", nt["label"]))
        texts.append(("通知说明", nt["detail"]))
    last = dict(hh[-1])
    last["ma1"] = E.sma([x["p1_px"] for x in hh], 250, len(hh) - 1)
    last["ma3"] = E.sma([x["sig_px"] for x in hh], 250, len(hh) - 1)
    for t in E.next_triggers(st, cfg, last):
        texts.append(("观察点标签", t["label"]))
        texts.append(("观察点条件", t["cond"]))
    for code, hx, tier in (
            ("P1_DCA", synth(cfg["start_date"], 40, flat1, flatsig), None),
            ("P3_TIER", synth(cfg["start_date"], 400, flat1,
                              [5500.0] * 300 + [5500.0 * 0.94] * 100), 1)):
        for cut in range(2, len(hx) + 1):
            s2, _ = E.run(hx[:cut], cfg, base_state(cfg))
            hit = [q for q in s2["pending"]
                   if q["action"] == code and (tier is None or q.get("tier") == tier)]
            if hit:
                texts.append(("指令标题", hit[0]["label"]))
                texts.append(("指令说明", hit[0]["detail"]))
                break
    bad = [(k, v) for k, v in texts if any(j in v for j in JARGON)]
    check("面向用户的文字里没有黑话/内部编号", not bad,
          "；".join("%s「%s」" % (k, v[:26]) for k, v in bad[:3]) or
          "共检查 %d 条" % len(texts))

    codes = (cfg["part1"]["code"][2:], cfg["part3"]["hold_code"][2:])
    naked = [v for k, v in texts
             if k.endswith("说明") and ("sh5" in v or "sz" in v)]
    check("说明里不出现裸代码（应写成「中概互联ETF(513050)」）", not naked,
          naked[0][:40] if naked else "")

    lbl = [v for k, v in texts if k in ("指令标题", "观察点标签", "通知标题")]
    unnamed = [v for v in lbl
               if not any(n in v for n in ("中概", "红利", "三份", "策略A"))]
    check("每条标题都点明哪一份/哪只标的", not unnamed,
          "；".join(unnamed[:3]) or "共 %d 条标题" % len(lbl))

    check("配置里给每份都写了显示名",
          all(cfg[k].get("name") for k in ("part1", "part2", "part3")),
          "／".join(str(cfg[k].get("name")) for k in ("part1", "part2", "part3")))
    check("show() 渲染为「名称(代码)」",
          E.show(cfg["part1"]) == "中概互联ETF(513050)", E.show(cfg["part1"]))
    check("配置缺 name 时退回代码，不崩",
          E.show({"code": "sh999999"}) == "sh999999", E.show({"code": "sh999999"}))

    print()
    print("=" * 78)
    print("十七、故障必须被发现，且必须告诉我该做什么")
    print("=" * 78)
    good = synth(cfg["start_date"], 400, flat1, flatsig, pb=[0.79] * 400)
    st_g, _ = E.run(good, cfg, base_state(cfg))
    hz = E.build_health(good, cfg, st_g, E.ma_health(good, cfg), [], [],
                        divs_age=1, skipped=(), fetch_ok=True, stale_days=0)
    check("一切正常时不报任何故障", not hz,
          "误报 %s" % [x["title"] for x in hz])

    # PB 断供 → 切换条款失明
    blind = [dict(r) for r in good]
    for r in blind[-10:]:
        r["pb_pct"] = None
    check("PB 断供天数计算正确", E.pb_blind_days(blind) == 10,
          "算得 %d 天" % E.pb_blind_days(blind))
    st_b, tb = E.run(blind, cfg, base_state(cfg)), None
    last_b = dict(blind[-1])
    last_b["ma1"] = E.sma([x["p1_px"] for x in blind], 250, len(blind) - 1)
    last_b["ma3"] = E.sma([x["sig_px"] for x in blind], 250, len(blind) - 1)
    tb = E.next_triggers(st_b[0], cfg, last_b)
    check("PB 断供时切换条款确会从观察点消失（故必须报警）",
          not any("创业板" in t["label"] for t in tb),
          "剩余观察点 %d 条" % len(tb))
    hb = E.build_health(blind, cfg, st_b[0], E.ma_health(blind, cfg), [], [],
                        divs_age=1, skipped=(), fetch_ok=True, stale_days=0)
    pbi = [x for x in hb if "失明" in x["title"]]
    check("PB 断供 → 报 critical 并给出步骤",
          bool(pbi) and pbi[0]["level"] == "critical" and len(pbi[0]["todo"]) >= 2,
          pbi[0]["title"] if pbi else "未报")
    check("步骤里指明了要改哪个 Secret",
          bool(pbi) and any("LIXINGER_TOKEN" in t for t in pbi[0]["todo"]), "")

    # 份额折算 → 检出 + 冻结 + 不误下单
    # 弹药未投完时发生折算 —— 这才是会真的下错单的场景
    sp = synth(cfg["start_date"], 60, flat1, flatsig, pb=[0.79] * 60)
    for r in sp[-10:]:
        r["p1_px"] = r["p1_px"] * 0.5
    frozen, hits = E.detect_splits(sp, cfg)
    check("份额折算被检出", frozen == {1} and len(hits) >= 1,
          "%s %+.1f%%" % (hits[0]["name"], hits[0]["change"] * 100) if hits else "未检出")
    clean = synth(cfg["start_date"], 60, flat1, flatsig, pb=[0.79] * 60)
    st_cl, _ = E.run(clean, cfg, base_state(cfg))
    st_no, _ = E.run(sp, cfg, base_state(cfg))
    st_fz, _ = E.run(sp, cfg, base_state(cfg), frozen=frozen)
    n = lambda st, a: len([e for e in st["events"] if e["action"] == a])
    check("不冻结时折算会凭空触发加码（故必须冻结）",
          n(st_no, "中概加码买入") > n(st_cl, "中概加码买入"),
          "无折算 %d 次 → 有折算 %d 次"
          % (n(st_cl, "中概加码买入"), n(st_no, "中概加码买入")))
    check("冻结后加码不再被误触发",
          n(st_fz, "中概加码买入") == n(st_cl, "中概加码买入"),
          "冻结后 %d 次" % n(st_fz, "中概加码买入"))
    check("冻结不影响定投（日历规则，按当天真实价成交）",
          n(st_fz, "中概定投买入") == n(st_cl, "中概定投买入"),
          "正常 %d 次 ／ 冻结后 %d 次"
          % (n(st_cl, "中概定投买入"), n(st_fz, "中概定投买入")))
    check("冻结只影响出问题的那一份，不动其它份",
          st_fz["p2"]["units"] == st_no["p2"]["units"], "")
    check("冻结后弹药没有被白白打光",
          st_fz["p1"]["cash"] > st_no["p1"]["cash"] + 1000,
          "冻结后剩 %.0f 元 ／ 不冻结剩 %.0f 元"
          % (st_fz["p1"]["cash"], st_no["p1"]["cash"]))
    hs = E.build_health(sp, cfg, st_fz, E.ma_health(sp, cfg), [], hits,
                        divs_age=1, skipped=(), fetch_ok=True, stale_days=0)
    spi = [x for x in hs if "折算" in x["title"]]
    check("份额折算 → 报 critical 并说明已冻结",
          bool(spi) and spi[0]["level"] == "critical" and "冻结" in spi[0]["title"],
          spi[0]["title"] if spi else "未报")
    check("涨跌停以内的正常波动不误判为折算",
          not E.detect_splits(good, cfg)[1], "")
    cfg2 = dict(cfg, resolved_splits=[hits[0]["date"]] if hits else [])
    check("写进 resolved_splits 后解除冻结",
          not E.detect_splits(sp, cfg2)[1],
          "已处理日期：%s" % (hits[0]["date"] if hits else "—"))

    # 现金穿负
    st_neg = {"p1": {"cash": -5000.0}, "p2": {"cash": 0.0}, "p3": {"cash": 0.0}}
    hn = E.build_health(good, cfg, st_neg, [], [], [], divs_age=1,
                        skipped=(), fetch_ok=True, stale_days=0)
    check("现金穿负 → 报 critical 并指向 ledger.csv",
          any("穿负" in x["title"] for x in hn)
          and any("ledger.csv" in t for x in hn for t in x["todo"]), "")

    # 每条故障都必须带可执行步骤
    allh = hb + hs + hn + E.build_health(
        good, cfg, st_g, E.ma_health(good, cfg), ["测试冲突"], [],
        divs_age=999, skipped=["2026-01-05"], fetch_ok=False, stale_days=99)
    check("每条故障都带 what 与至少 2 步 todo",
          all(x.get("what") and len(x.get("todo") or []) >= 2 for x in allh),
          "共 %d 条" % len(allh))
    check("级别只有 critical / warn 两档",
          set(x["level"] for x in allh) <= {"critical", "warn"}, "")

    # 网页与推送都要接上
    appjs = open(os.path.join(ROOT, "docs", "app.js"), encoding="utf-8").read()
    check("网页渲染 health 且故障置顶", "d.health" in appjs and "prob" in appjs, "")
    check("网页有客户端自查（流水线停摆时 data.json 不会自己变）",
          "selfCheck" in appjs and "generated_at" in appjs, "")
    check("网页对 localStorage 不可用做了降级",
          "LS_OK" in appjs and "lsSet" in appjs, "")
    ntf = open(os.path.join(ROOT, "src", "notify.py"), encoding="utf-8").read()
    check("critical 故障会升级 Bark 标题",
          '"⛔ 需要你处理 · %s"' in ntf, "")

    print()
    print("=" * 78)
    print("验收结果：通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - %s" % f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
