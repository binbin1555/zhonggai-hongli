# -*- coding: utf-8 -*-
"""策略A 引擎 —— 与《策略A_执行规则_v1.md》逐条对应。

!! 本文件是规则的唯一实现。任何改动必须同步修改执行文档，反之亦然。!!

全局口径
--------
执行     所有信号按【当日收盘价】判定，【次日收盘】执行（T+1）
再平衡   三份之间不做再平衡
闲置资金 按 cash_rate_annual 计息（货币ETF / 逆回购）
分红     由 dividends.py 自动抓取除息日与每份派现，引擎按当日持仓自动入账。
         ledger.csv 里的 dividend 行仍然有效，且优先于自动入账（同日同份不重复计）。
"""
from datetime import date

TD_PER_YEAR = 243


def _d(s):
    return date(*map(int, s.split("-")))


def next_weekday(s):
    """下一个工作日（粗略交易日）。遇节假日时 exec_date 到期判定会自动顺延。"""
    d = _d(s)
    while True:
        d = date.fromordinal(d.toordinal() + 1)
        if d.weekday() < 5:
            return d.isoformat()


def _week_key(s):
    iso = _d(s).isocalendar()
    return "%d-W%02d" % (iso[0], iso[1])


def sma(xs, n, i):
    """xs[i] 的 n 日均值；数据不足或含缺失返回 None。"""
    if i < n - 1:
        return None
    w = xs[i - n + 1:i + 1]
    return None if any(v is None for v in w) else sum(w) / n


def new_state(cfg):
    """全新初始状态。实际启动由 init_state.py 用真实成交填充。"""
    return {
        "start_date": cfg["start_date"],
        "p1": {"units": 0.0, "cash": float(cfg["part1"]["ammo"]),
               "cost": 0.0, "dca_done": 0, "last_dca_week": None,
               "armed": False, "exited": False, "accel_fired": [],
               "first_buy_date": None},
        "p2": {"units": 0.0, "cash": 0.0, "cost": 0.0},
        "p3": {"units": 0.0, "cash": float(cfg["part3"]["capital"]),
               "tier": 0, "cost": 0.0},
        "switched": False, "switch_date": None,
        "pending": [],
        "events": [],
    }


def run(hist, cfg, state0=None, injections=None, divs=None):
    """逐日重放。

    hist 每项需含：date, p1_px(513050), p2_px(512890), p3_px(515180),
                   sig_px(000922), pb_pct(创业板PB十年分位 0~1，可 None)
    injections: ledger 中晚于 start_date 的流水（分红、人工修正），按日期注入。
    divs: {代码: {除息日: 每份派现}}，见 dividends.py。按当日持仓自动入账。
    返回 (state, curve)
    """
    inj = {}
    for r in (injections or []):
        inj.setdefault(r["date"], []).append(r)

    # 分红表按「份」重排；第三份持仓是 ETF 而非信号指数，取 hold_code
    divs = divs or {}
    part_div = {1: divs.get(cfg["part1"]["code"], {}),
                2: divs.get(cfg["part2"]["code"], {}),
                3: divs.get(cfg["part3"]["hold_code"], {})}
    c1, c3 = cfg["part1"], cfg["part3"]
    n = len(hist)
    dates = [h["date"] for h in hist]
    p1 = [h["p1_px"] for h in hist]
    p2 = [h["p2_px"] for h in hist]
    p3 = [h["p3_px"] for h in hist]
    sig = [h["sig_px"] for h in hist]
    pbp = [h.get("pb_pct") for h in hist]

    ma1 = [sma(p1, c1["ma_n"], i) for i in range(n)]
    ma3 = [sma(sig, c3["ma_n"], i) for i in range(n)]

    st = state0 or new_state(cfg)
    rc = (1 + cfg["cash_rate_annual"]) ** (1.0 / TD_PER_YEAR) - 1
    curve = []

    def ev(d, part, action, detail):
        st["events"].append({"date": d, "part": part,
                             "action": action, "detail": detail})

    start_i = next((i for i, d in enumerate(dates) if d >= st["start_date"]), n)

    for i in range(start_i, n):
        d = dates[i]
        A, B, C = st["p1"], st["p2"], st["p3"]

        # 0. 注入 ledger 流水（分红并入该份现金池；人工买卖修正持仓）
        for r in inj.get(d, []):
            h = {1: A, 2: B, 3: C}.get(r["part"])
            if h is None:
                continue
            amt = r["amount"] or r["shares"] * r["price"]
            if r["action"] == "dividend":
                h["cash"] += amt
                ev(d, r["part"], "分红入账", "%.2f 元并入现金池" % amt)
            elif r["action"] == "buy":
                h["units"] += r["shares"]; h["cash"] -= amt; h["cost"] += amt
                ev(d, r["part"], "手工买入", r["note"] or "ledger 记录")
            elif r["action"] in ("sell", "exit"):
                h["units"] -= r["shares"]; h["cash"] += amt
                h["cost"] = max(0.0, h["cost"] - amt)
                ev(d, r["part"], "手工卖出", r["note"] or "ledger 记录")

        # 0b. 分红自动入账（同日同份若 ledger 已手记，跳过以免重复）
        manual_div = {r["part"] for r in inj.get(d, [])
                      if r["action"] == "dividend"}
        for pi, h in ((1, A), (2, B), (3, C)):
            per = part_div[pi].get(d)
            if not per or pi in manual_div or h["units"] <= 0:
                continue
            amt = h["units"] * per
            h["cash"] += amt
            ev(d, pi, "分红入账",
               "每份 %.4f 元 x %.0f 份 = %.2f 元，自动并入现金池" % (per, h["units"], amt))

        # 1. 现金计息（持仓市值由 units*price 直接算，无需逐日更新）
        if i > start_i:
            A["cash"] *= 1 + rc
            B["cash"] *= 1 + rc
            C["cash"] *= 1 + rc

        # 2. 执行昨日挂起的动作（T+1）
        still = []
        for pd_ in st["pending"]:
            if d < pd_["exec_date"]:
                still.append(pd_)
                continue
            act = pd_["action"]

            if act == "P1_DCA":
                amt = min(pd_["amount"], A["cash"])
                if amt > 0 and p1[i]:
                    A["units"] += amt / p1[i]
                    A["cash"] -= amt
                    A["cost"] += amt
                    A["dca_done"] += 1
                    if A["first_buy_date"] is None:
                        A["first_buy_date"] = d
                    ev(d, 1, "定投买入", "买入 %s 约 %.0f 元（第 %d/%d 次）"
                       % (c1["code"], amt, A["dca_done"], c1["dca_weeks"]))

            elif act == "P1_ACCEL":
                amt = min(pd_["amount"], A["cash"])
                if amt > 0 and p1[i]:
                    A["units"] += amt / p1[i]
                    A["cash"] -= amt
                    A["cost"] += amt
                    ev(d, 1, "加速买入", "浮亏触发，投入 %.0f 元" % amt)

            elif act == "P1_EXIT":
                if A["units"] and p1[i]:
                    proceeds = A["units"] * p1[i]
                    moved = proceeds + A["cash"]
                    C["cash"] += moved
                    ev(d, 1, "止盈清仓",
                       "卖出全部 %s，%.0f 元转入第三份" % (c1["code"], moved))
                    A["units"] = 0.0
                    A["cash"] = 0.0
                    A["exited"] = True

            elif act == "P3_TIER":
                want = pd_["tier"]
                if p3[i]:
                    cur = C["units"] * p3[i]
                    pool = cur + C["cash"]
                    tgt = pool * want / 3.0
                    if cur < tgt:
                        buy = min(tgt - cur, C["cash"])
                        C["units"] += buy / p3[i]
                        C["cash"] -= buy
                        C["cost"] += buy
                    elif cur > tgt:
                        sell = cur - tgt
                        C["units"] -= sell / p3[i]
                        C["cash"] += sell
                        C["cost"] = C["cost"] * (tgt / cur) if cur > 0 else 0.0
                old = C["tier"]
                C["tier"] = want
                ev(d, 3, "网格清仓" if want == 0 else "网格买入",
                   ("涨过 MA250x%.2f，三档全部清空" % c3["exit_ratio"]) if want == 0
                   else ("买入至 %d/3 仓（原 %d/3）" % (want, old)))

            elif act == "SWITCH":
                tot = (A["units"] * (p1[i] or 0) + A["cash"]
                       + B["units"] * (p2[i] or 0) + B["cash"]
                       + C["units"] * (p3[i] or 0) + C["cash"])
                A["units"] = 0.0
                A["cash"] = 0.0
                B["units"] = 0.0
                B["cash"] = 0.0
                C["units"] = 0.0
                C["cash"] = tot
                st["switched"] = True
                st["switch_date"] = d
                ev(d, 0, "切换",
                   "创业板PB分位<=%.0f%%，策略A全部清仓，转执行创业板手册策略"
                   % (cfg["switch"]["pb_percentile_threshold"] * 100))

        st["pending"] = still

        # 3. 记录净值
        v1 = A["units"] * (p1[i] or 0) + A["cash"]
        v2 = B["units"] * (p2[i] or 0) + B["cash"]
        v3 = C["units"] * (p3[i] or 0) + C["cash"]
        curve.append({"date": d, "total": v1 + v2 + v3,
                      "p1": v1, "p2": v2, "p3": v3, "tier": C["tier"]})

        if st["switched"]:
            continue

        # 历史最后一天没有「明天」，用下一个工作日推算，否则末日永远排不出待执行
        nxt = dates[i + 1] if i + 1 < n else next_weekday(d)
        has = lambda a: any(x["action"] == a for x in st["pending"])

        # 4. 切换判定（最高优先级）
        if pbp[i] is not None and pbp[i] <= cfg["switch"]["pb_percentile_threshold"]:
            if not has("SWITCH"):
                st["pending"].append({"action": "SWITCH", "exec_date": nxt or d,
                                      "part": 0,
                                      "detail": "全部清仓，转创业板手册策略"})
            continue

        # 5. 第一份 · 中概互联
        if not A["exited"]:
            v1_now = A["units"] * (p1[i] or 0) + A["cash"]

            if not A["armed"] and v1_now >= c1["arm_target"]:
                A["armed"] = True
                ev(d, 1, "武装", "本份总市值达 %.0f 元，止盈检测启动" % v1_now)

            if not A["armed"]:
                base = A["first_buy_date"] or st["start_date"]
                if (_d(d) - _d(base)).days >= c1["arm_timeout_years"] * 365:
                    A["armed"] = True
                    ev(d, 1, "兜底武装", "建仓满 %d 年未武装，视同武装"
                       % c1["arm_timeout_years"])

            if (A["armed"] and A["units"] > 0 and ma1[i] and p1[i]
                    and p1[i] < ma1[i] and not has("P1_EXIT")):
                st["pending"].append({"action": "P1_EXIT", "exec_date": nxt or d,
                                      "part": 1,
                                      "detail": "全部卖出 %s" % c1["code"]})

            elif A["cash"] > 1 and A["cost"] > 0 and p1[i] and not has("P1_ACCEL"):
                fp = A["units"] * p1[i] / A["cost"] - 1
                for k, rule in enumerate(c1["accel"]):
                    if k in A["accel_fired"]:
                        continue
                    if fp <= -rule["drawdown"]:
                        amt = A["cash"] * rule["deploy"]
                        A["accel_fired"].append(k)
                        st["pending"].append(
                            {"action": "P1_ACCEL", "exec_date": nxt or d,
                             "part": 1, "amount": amt,
                             "detail": "浮亏 %.1f%%，投入 %.0f 元" % (fp * 100, amt)})
                        break

            # 定投：目标在每周指定星期几（config 的 dca_weekday，周一=0）。
            # 注意：定投不受武装状态影响 —— 文档只规定「15万分26周投完」，
            #       不存在「武装后停止」的条款。2026-08-18 移除该越权条件。
            # 若该日休市，则顺延到当周之后的第一个交易日。
            # 信号在前一个交易日收盘发出，次日执行 —— 因此推送会提前一晚到达。
            if (A["cash"] > 1 and A["dca_done"] < c1["dca_weeks"]
                    and nxt and not has("P1_DCA")):
                wk = _week_key(nxt)
                if (wk != A["last_dca_week"]
                        and _d(nxt).weekday() >= c1.get("dca_weekday", 2)
                        and nxt >= c1.get("dca_start_date", "0000-00-00")):
                    A["last_dca_week"] = wk
                    per = min(c1["ammo"] / c1["dca_weeks"], A["cash"])
                    st["pending"].append({"action": "P1_DCA", "exec_date": nxt,
                                          "part": 1, "amount": per,
                                          "detail": "定投买入 %s 约 %.0f 元"
                                                    % (c1["code"], per)})

        # 6. 第三份 · 中证红利网格
        if ma3[i] and sig[i] and not has("P3_TIER"):
            r = sig[i] / ma3[i]
            if C["tier"] > 0 and r >= c3["exit_ratio"]:
                want = 0
            else:
                want = max(C["tier"], sum(1 for t in c3["buy_tiers"] if r <= t))
            if want != C["tier"]:
                st["pending"].append({"action": "P3_TIER", "exec_date": nxt or d,
                                      "part": 3, "tier": want,
                                      "detail": ("清空网格" if want == 0
                                                 else "买入至 %d/3 仓" % want)})

    return st, curve


def metrics(curve, capital):
    """总收益、年化、最大回撤。"""
    if not curve:
        return {}
    eq = [c["total"] for c in curve]
    peak, mdd = -1e18, 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    years = len(eq) / TD_PER_YEAR
    total = eq[-1] / capital - 1
    cagr = (eq[-1] / capital) ** (1 / years) - 1 if years > 1 / 12 else None
    return {"equity": eq[-1], "total_return": total, "cagr": cagr,
            "max_drawdown": mdd, "days": len(eq), "years": years}


def next_triggers(st, cfg, last):
    """下一步会触发什么，按接近程度降序。last: 最后一日的行情 dict。"""
    c1, c3 = cfg["part1"], cfg["part3"]
    out = []
    A, C = st["p1"], st["p3"]

    if st["switched"]:
        return [{"label": "策略A已停机", "cond": "已切换至创业板手册策略",
                 "short": "—", "progress": 1.0}]

    pb = last.get("pb_pct")
    if pb is not None:
        thr = cfg["switch"]["pb_percentile_threshold"]
        out.append({"label": "切换创业板策略",
                    "cond": "创业板PB十年分位 <= %.0f%%" % (thr * 100),
                    "short": "当前 %.1f%%" % (pb * 100),
                    "progress": min(1.0, thr / pb) if pb > 0 else 1.0})

    if not A["exited"]:
        v1 = A["units"] * (last["p1_px"] or 0) + A["cash"]
        if not A["armed"]:
            out.append({"label": "第一份武装",
                        "cond": "本份总市值达 %.0f 元" % c1["arm_target"],
                        "short": "当前 %.0f 元，还差 %.1f%%"
                                 % (v1, (c1["arm_target"] / v1 - 1) * 100 if v1 else 0),
                        "progress": min(1.0, v1 / c1["arm_target"])})
        elif last.get("ma1") and last["p1_px"]:
            buf = last["p1_px"] / last["ma1"] - 1
            out.append({"label": "第一份止盈",
                        "cond": "跌破 MA250 %.4f" % last["ma1"],
                        "short": "尚有 %.2f%% 缓冲" % (buf * 100),
                        "progress": max(0.0, 1 - min(1.0, buf / 0.15))})

    if last.get("ma3") and last["sig_px"]:
        ma = last["ma3"]
        r = last["sig_px"] / ma
        if C["tier"] < 3:
            t = c3["buy_tiers"][C["tier"]]
            need = (ma * t / last["sig_px"] - 1) * 100
            out.append({"label": "网格第 %d 档买入" % (C["tier"] + 1),
                        "cond": "中证红利跌破 %.2f（MA250 x %.2f）" % (ma * t, t),
                        "short": "还需跌 %.2f%%" % abs(need),
                        "progress": max(0.0, min(1.0, (1 - r) / (1 - t))) if t < 1 else 0})
        if C["tier"] > 0:
            er = c3["exit_ratio"]
            need = (ma * er / last["sig_px"] - 1) * 100
            out.append({"label": "网格清仓",
                        "cond": "中证红利涨过 %.2f（MA250 x %.2f）" % (ma * er, er),
                        "short": "还需涨 %.2f%%" % need,
                        "progress": max(0.0, min(1.0, r / er))})

    out.sort(key=lambda x: -x["progress"])
    return out


AUTO_BUY = ("定投买入", "加速买入", "网格买入")
AUTO_SELL = ("止盈清仓", "网格清仓", "切换")


def check_duplicates(events, window=3):
    """检出 ledger 手工流水与引擎自动动作的疑似重复记账。

    引擎自己会模拟每一次定投/加码/网格成交。若你「如实」把同一笔又记进
    ledger.csv，持仓会翻倍、现金会穿负 —— 这是本系统最容易犯的错。
    ledger 只该记「和系统说的不一样」的部分。
    """
    warns = []
    auto = [e for e in events if e["action"] in AUTO_BUY + AUTO_SELL]
    for e in events:
        if e["action"] not in ("手工买入", "手工卖出"):
            continue
        side = AUTO_BUY if e["action"] == "手工买入" else AUTO_SELL
        for a in auto:
            if a["part"] != e["part"] or a["action"] not in side:
                continue
            if abs(_days(a["date"]) - _days(e["date"])) <= window:
                warns.append(
                    "%s 第%d份：ledger 记了「%s」，但引擎同期已自动执行「%s」(%s)。"
                    "若这是同一笔成交，会被重复计算，请从 ledger.csv 删除该行。"
                    % (e["date"], e["part"], e["action"], a["action"], a["date"]))
                break
    return warns


def _days(ds):
    return date(*map(int, ds.split("-"))).toordinal()


def ma_health(hist, cfg):
    """体检两条 MA250 是否真的在监控。

    next_triggers 遇到 MA 为 None 会静默跳过对应观察点 —— 看板看上去一切正常，
    实际上那个信号已经停摆。历史里少几行不会让 MA 变 None，但会让窗口悄悄变旧，
    所以跨度也一并检查。
    """
    out = []
    n = len(hist)
    specs = (("第一份止盈 MA%d", cfg["part1"]["ma_n"], "p1_px"),
             ("红利网格 MA%d", cfg["part3"]["ma_n"], "sig_px"))
    for label, k, key in specs:
        xs = [h.get(key) for h in hist]
        name = label % k
        v = sma(xs, k, n - 1) if n else None
        if v is None:
            if n < k:
                why = "历史仅 %d 行，需 %d 行" % (n, k)
            else:
                miss = [hist[i]["date"] for i in range(n - k, n) if xs[i] is None]
                why = "窗口内 %d 天缺值（%s）" % (len(miss), "、".join(miss[:3]))
            out.append({"ok": False, "label": name,
                        "msg": "%s 不可用：%s —— 该信号当前未在监控" % (name, why)})
            continue
        span = (_d(hist[n - 1]["date"]) - _d(hist[n - k]["date"])).days
        exp = k / float(TD_PER_YEAR) * 365.25
        if span > exp * 1.15:
            out.append({"ok": False, "label": name,
                        "msg": "%s 窗口跨 %d 个自然日（正常约 %d）：历史缺行，均线偏旧"
                               % (name, span, int(round(exp)))})
        else:
            out.append({"ok": True, "label": name, "value": v, "span": span})
    return out
