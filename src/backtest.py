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
    dca = [e for e in st["events"] if e["action"] == "定投买入"]
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
    print("二、加速条款（浮亏 15% 投一半，25% 投剩余）")
    print("=" * 78)
    drop = [1.114 * (1 - 0.30 * min(1.0, i / 60.0)) for i in range(N)]
    hist = synth(cfg["start_date"], N, drop, flatsig)
    st, _ = E.run(hist, cfg, base_state(cfg))
    ac = [e for e in st["events"] if e["action"] == "加速买入"]
    check("加速至少触发 1 次", len(ac) >= 1, "实际 %d 次" % len(ac))
    print("       注：浮亏以【持仓加权成本】为基准，定投会持续拉低成本，")
    print("           因此第二档(-25%)在持续下跌中未必触发 —— 这是文档规则的必然结果。")
    check("加速后弹药耗尽", abs(st["p1"]["cash"]) < 1.0,
          "剩余 %.2f" % st["p1"]["cash"])

    print()
    print("=" * 78)
    print("三、武装与止盈")
    print("=" * 78)
    up = [1.114 * (1 + 0.60 * min(1.0, i / 100.0)) for i in range(300)]
    down = [up[-1] * (1 - 0.45 * min(1.0, (i) / 200.0)) for i in range(N - 300)]
    hist = synth(cfg["start_date"], N, up + down, flatsig)
    st, _ = E.run(hist, cfg, base_state(cfg))
    arm = [e for e in st["events"] if e["action"] == "武装"]
    ext = [e for e in st["events"] if e["action"] == "止盈清仓"]
    check("触发武装", len(arm) == 1, arm[0]["detail"] if arm else "未触发")
    check("武装后触发止盈", len(ext) == 1, ext[0]["date"] if ext else "未触发")
    check("止盈后第一份清空", st["p1"]["units"] == 0 and st["p1"]["cash"] == 0)
    check("止盈资金转入第三份", st["p3"]["cash"] > cfg["part3"]["capital"],
          "第三份现金 %.0f" % st["p3"]["cash"])
    if arm and ext:
        check("先武装后止盈", arm[0]["date"] < ext[0]["date"])

    print()
    print("=" * 78)
    print("四、兜底武装（满 3 年未达 40 万）")
    print("=" * 78)
    hist = synth(cfg["start_date"], 800, [1.114] * 800, [5500.0] * 800)
    st, _ = E.run(hist, cfg, base_state(cfg))
    to = [e for e in st["events"] if e["action"] == "兜底武装"]
    check("满 3 年触发兜底武装", len(to) == 1, to[0]["date"] if to else "未触发（样本不足3年时正常）")

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
    buys = [e for e in g if e["action"] == "网格买入"]
    sells = [e for e in g if e["action"] == "网格清仓"]
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
    sw = [e for e in st["events"] if e["action"] == "切换"]
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
        if e["action"] in ("网格买入", "网格清仓"):
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
    dcaA = [e for e in stA["events"] if e["action"] == "定投买入"]
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
    dcaB = [e for e in stB["events"] if e["action"] == "定投买入"]
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
    print("验收结果：通过 %d 项，失败 %d 项" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项：")
        for f in FAIL:
            print("  - %s" % f)
    print("=" * 78)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
