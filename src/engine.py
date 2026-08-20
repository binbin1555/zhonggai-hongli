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


# 执行文档「第一份·买卖前必查」：513050 是跨境ETF，折溢价可能很大，
# 必须看软件的「折溢价率」栏，不能用昨日单位净值自己算。
PRECHECK_BUY = ("下单前必看折溢价率（软件「折溢价率」栏，别用昨日净值自己算）："
                "<1% 正常 ／ 1–3% 可接受 ／ >3% 今日暂缓，等回落再买")
PRECHECK_SELL = ("下单前必看折溢价率：优先挑溢价高的日子卖。"
                 ">3% 折价时暂缓，等回落")
# 全局委托口径：限价；避开 9:15–9:35 与 14:55–15:00
ORDER_RULE = "限价委托 · 避开 9:15–9:35 与 14:55–15:00"


def _money(x):
    return "{:,}".format(int(round(x)))


def show(part_cfg, code_key="code"):
    """标的的可读写法，如「中概互联ETF(513050)」。配置没写 name 就退回代码。"""
    code = part_cfg.get(code_key) or ""
    nm = part_cfg.get("name")
    num = code[2:] if code[:2] in ("sh", "sz") else code
    return "%s(%s)" % (nm, num) if nm else code


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


def run(hist, cfg, state0=None, injections=None, divs=None,
        frozen=(), ma_frozen=()):
    """逐日重放。

    hist 每项需含：date, p1_px(513050), p2_px(512890), p3_px(515180),
                   sig_px(000922), pb_pct(创业板PB十年分位 0~1，可 None)
    injections: ledger 中晚于 start_date 的流水（分红、人工修正），按日期注入。
    divs: {代码: {除息日: 每份派现}}，见 dividends.py。按当日持仓自动入账。
    frozen: 数据被判定污染的份号集合。这些份只停发新指令，不改变已有持仓
            —— 拿被折算污染的价格去算浮亏并下单，才是真正的规则偏离。
    返回 (state, curve)
    """
    frozen = set(frozen or ())
    ma_frozen = set(ma_frozen or ())
    inj = {}
    for r in (injections or []):
        inj.setdefault(r["date"], []).append(r)

    # 分红表按「份」重排；第三份持仓是 ETF 而非信号指数，取 hold_code。
    # 入账日按执行文档：「收到现金分红后，下一个交易日并入该份的现金池」，
    # 即发放日之后的第一个交易日，不是除息日（两者相隔 3–5 个自然日）。
    divs = divs or {}
    _dates = [h["date"] for h in hist]

    def _credit_day(rec, ex):
        pay = rec.get("pay") if isinstance(rec, dict) else None
        if not pay:
            return ex                       # 无发放日信息时退回除息日
        for x in _dates:
            if x > pay:
                return x
        return None                         # 发放日在数据末尾之后，下次重放再记

    def _remap(code):
        out = {}
        for ex, rec in (divs.get(code) or {}).items():
            per = rec["per"] if isinstance(rec, dict) else rec
            d = _credit_day(rec, ex)
            if d:
                out[d] = out.get(d, 0.0) + per
        return out

    part_div = {1: _remap(cfg["part1"]["code"]),
                2: _remap(cfg["part2"]["code"]),
                3: _remap(cfg["part3"]["hold_code"])}
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
            elif r["action"] == "split":
                # 份额折算：份数按比例变，成本与现金都不动
                f = r["shares"] or 1.0
                before = h["units"]
                h["units"] *= f
                ev(d, r["part"], "份额折算入账",
                   "按 1:%g 折算，份额 %.0f → %.0f，成本与现金不变"
                   % (f, before, h["units"]))

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
                    ev(d, 1, "中概定投买入", "买入 %s 约 %.0f 元（第 %d/%d 次）"
                       % (show(c1), amt, A["dca_done"], c1["dca_weeks"]))

            elif act == "P1_ACCEL":
                amt = min(pd_["amount"], A["cash"])
                if amt > 0 and p1[i]:
                    A["units"] += amt / p1[i]
                    A["cash"] -= amt
                    A["cost"] += amt
                    ev(d, 1, "中概加码买入", "浮亏触发，投入 %.0f 元" % amt)

            elif act == "P1_EXIT":
                if A["units"] and p1[i]:
                    proceeds = A["units"] * p1[i]
                    moved = proceeds + A["cash"]
                    C["cash"] += moved
                    ev(d, 1, "中概止盈清仓",
                       "卖出全部 %s，%.0f 元转入红利网格那份"
                       % (show(c1), moved))
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
                ev(d, 3, "红利网格清仓" if want == 0 else "红利网格买入",
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
                ev(d, 0, "切换至创业板策略",
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
                                      "label": "三份全部清仓 · 转创业板策略",
                                      "detail": "中概、红利低波、红利网格全部卖出，之后执行创业板手册"})
            continue

        # 5. 第一份 · 中概互联
        if not A["exited"]:
            v1_now = A["units"] * (p1[i] or 0) + A["cash"]

            if not A["armed"] and v1_now >= c1["arm_target"]:
                A["armed"] = True
                ev(d, 1, "中概开启止盈保护",
                   "中概这份市值达 %.0f 元，从今天起开始盯 MA250 止盈信号" % v1_now)

            if not A["armed"]:
                base = A["first_buy_date"] or st["start_date"]
                if (_d(d) - _d(base)).days >= c1["arm_timeout_years"] * 365:
                    A["armed"] = True
                    ev(d, 1, "中概开启止盈保护（满3年兜底）",
                       "建仓满 %d 年市值仍未达标，按规则强制开始盯止盈"
                       % c1["arm_timeout_years"])

            # 折算污染的是「现价 vs 历史基准」的比较（成本、MA250），
            # 所以只掐掉止盈与加码；定投是日历规则、按固定金额买当天真实价格，
            # 不受影响，继续照常执行。
            if (1 not in frozen and 1 not in ma_frozen
                    and A["armed"] and A["units"] > 0 and ma1[i]
                    and p1[i] and p1[i] < ma1[i] and not has("P1_EXIT")):
                st["pending"].append({"action": "P1_EXIT", "exec_date": nxt or d,
                                      "part": 1,
                                      "label": "中概互联 · 止盈清仓",
                                      "precheck": PRECHECK_SELL,
                                      "detail": "已开启止盈保护且跌破 MA250，"
                                                "全部卖出 %s 约 %.0f 元"
                                                % (show(c1),
                                                   A["units"] * (p1[i] or 0))})

            elif (1 not in frozen and A["cash"] > 1 and A["cost"] > 0
                    and p1[i] and not has("P1_ACCEL")):
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
                             "label": "中概互联 · 加码买入",
                             "precheck": PRECHECK_BUY,
                             "detail": "浮亏 %.1f%%，投入约 %.0f 元"
                                       % (abs(fp) * 100, amt)})
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
                                          "label": "中概互联 · 定投买入",
                                          "precheck": PRECHECK_BUY,
                                          "detail": "第 %d/%d 次定投，买入 %s 约 %.0f 元"
                                                    % (A["dca_done"] + 1, c1["dca_weeks"],
                                                       show(c1), per)})

        # 6. 第三份 · 中证红利网格
        if (ma3[i] and sig[i] and not has("P3_TIER")
                and 3 not in frozen and 3 not in ma_frozen):
            r = sig[i] / ma3[i]
            if C["tier"] > 0 and r >= c3["exit_ratio"]:
                want = 0
            else:
                want = max(C["tier"], sum(1 for t in c3["buy_tiers"] if r <= t))
            if want != C["tier"]:
                cur3 = C["units"] * (p3[i] or 0)
                tgt3 = (cur3 + C["cash"]) * want / 3.0
                delta = tgt3 - cur3
                if want == 0:
                    lab = "红利网格 · 全部清仓"
                    det = ("清空网格三档，全部卖出 %s，约 %.0f 元"
                           % (show(c3, "hold_code"), cur3))
                else:
                    lab = "红利网格 · 买入"
                    det = ("买入至 %d/3 仓（原 %d/3），本次买入 %s 约 %.0f 元"
                           % (want, C["tier"], show(c3, "hold_code"),
                              max(0.0, delta)))
                st["pending"].append({"action": "P3_TIER", "exec_date": nxt or d,
                                      "part": 3, "tier": want,
                                      "label": lab, "amount": delta,
                                      "detail": det})

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
    """下一步会触发什么，按接近程度降序。last: 最后一日的行情 dict。

    每条附带 dist（还需变动多少）与 near（是否已进入临近区间），
    供推送标题升级与看板高亮使用。阈值见 config 的 near_alert。
    """
    c1, c3 = cfg["part1"], cfg["part3"]
    na = cfg.get("near_alert") or {}
    near_px = float(na.get("price_pct", 1.5))
    near_val = float(na.get("value_pct", 5.0))
    near_pb = float(na.get("pb_pp", 5.0))
    out = []
    A, C = st["p1"], st["p3"]

    if st["switched"]:
        return [{"label": "策略A已停机", "need": "已全部清仓",
                 "now": "改执行创业板手册策略",
                 "cond": "已全部清仓，改执行创业板手册策略",
                 "short": "—", "dist": 0.0, "unit": "%", "near": False}]

    pb = last.get("pb_pct")
    if pb is not None:
        thr = cfg["switch"]["pb_percentile_threshold"]
        out.append({"label": "三份全部清仓 · 转创业板策略",
                    "need": "还需创业板PB再跌 %.1f 个百分点" % ((pb - thr) * 100),
                    "now": "当前十年分位 %.1f%%，要跌到 %.0f%% 以下"
                           % (pb * 100, thr * 100),
                    "cond": "创业板PB十年分位跌到 %.0f%% 以下（触发后策略A停机）"
                            % (thr * 100),
                    "short": "当前 %.1f%%" % (pb * 100),
                    "dist": (pb - thr) * 100, "unit": "pp",
                    "near": (pb - thr) * 100 <= near_pb})

    if not A["exited"]:
        v1 = A["units"] * (last["p1_px"] or 0) + A["cash"]
        if not A["armed"]:
            gap = (c1["arm_target"] / v1 - 1) * 100 if v1 else 999
            out.append({"label": "中概互联 · 开启止盈保护",
                        "need": "还需这份市值再涨 %.1f%%" % gap,
                        "now": "当前 %s 元，要涨到 %s 元"
                               % (_money(v1), _money(c1["arm_target"])),
                        "cond": "涨到后才开始盯 MA250 止盈信号（现在还不盯）",
                        "short": "当前 %.0f 元，还差 %.1f%%" % (v1, gap),
                        "dist": gap, "unit": "%",
                        "near": v1 > 0 and gap <= near_val})
        elif last.get("ma1") and last["p1_px"]:
            buf = last["p1_px"] / last["ma1"] - 1
            out.append({"label": "中概互联 · 止盈清仓",
                        "need": "还需跌 %.2f%%" % (buf * 100),
                        "now": "现价 %.4f，跌破 MA250 %.4f 就全部卖出"
                               % (last["p1_px"], last["ma1"]),
                        "cond": "已开启止盈保护，正在盯 MA250",
                        "short": "尚有 %.2f%% 缓冲" % (buf * 100),
                        "dist": buf * 100, "unit": "%",
                        "near": buf * 100 <= near_px})

    if last.get("ma3") and last["sig_px"]:
        ma = last["ma3"]
        r = last["sig_px"] / ma
        if C["tier"] < 3:
            t = c3["buy_tiers"][C["tier"]]
            need = (ma * t / last["sig_px"] - 1) * 100
            out.append({"label": "红利网格 · 第 %d 档买入" % (C["tier"] + 1),
                        "need": "还需跌 %.2f%%" % abs(need),
                        "now": "中证红利 %.2f，要跌破 %.2f（MA250 × %.2f）"
                               % (last["sig_px"], ma * t, t),
                        "cond": "跌破后买入至 %d/3 仓" % (C["tier"] + 1),
                        "short": "还需跌 %.2f%%" % abs(need),
                        "dist": abs(need), "unit": "%",
                        "near": abs(need) <= near_px})
        if C["tier"] > 0:
            er = c3["exit_ratio"]
            need = (ma * er / last["sig_px"] - 1) * 100
            out.append({"label": "红利网格 · 全部清仓",
                        "need": "还需涨 %.2f%%" % need,
                        "now": "中证红利 %.2f，要涨过 %.2f（MA250 × %.2f）"
                               % (last["sig_px"], ma * er, er),
                        "cond": "涨过后三档一次性全部清空",
                        "short": "还需涨 %.2f%%" % need,
                        "dist": abs(need), "unit": "%",
                        "near": abs(need) <= near_px})

    # 按「还需变动多少」升序 —— 越小越接近触发。
    # 原先按 progress 排序是错的：三条观察点的 progress 用了三种互不可比的
    # 算法（市值/目标、阈值/现值、在带宽里走了多远），拿来互排没有意义。
    out.sort(key=lambda x: x["dist"])
    return out


AUTO_BUY = ("中概定投买入", "中概加码买入", "红利网格买入")
AUTO_SELL = ("中概止盈清仓", "红利网格清仓", "切换至创业板策略")


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


# 这几类事件不需要你下单，但会改变系统之后的行为，必须让你看见。
NOTICE = {
    "中概开启止盈保护": (
        "中概互联 · 已开启止盈保护",
        "浮盈够了，从今天起开始盯 MA250：一旦跌破就提示你全部卖出锁定收益"),
    "中概开启止盈保护（满3年兜底）": (
        "中概互联 · 已开启止盈保护（满3年兜底）",
        "市值没到 40 万但持有已满 3 年，按规则强制开始盯 MA250"),
    "分红入账": ("分红已到账", None),
}


def build_notices(st, last_date, days=14):
    """状态里程碑与分红，做成通知横幅。

    这些不是待办指令（没有单要下），但漏看会让你误判系统状态，
    所以同样占一条横幅、同样要点确认才消失。保留 days 天。
    """
    cutoff = date.fromordinal(_d(last_date).toordinal() - days).isoformat()
    out = []
    for e in st["events"]:
        if e["date"] < cutoff or e["action"] not in NOTICE:
            continue
        title, sub = NOTICE[e["action"]]
        out.append({"key": "note:%s:%s" % (e["date"], e["action"]),
                    "date": e["date"], "part": e.get("part", 0),
                    "label": title,
                    "detail": e["detail"] if sub is None else sub,
                    "extra": e["detail"] if sub is not None else ""})
    return out


# ── 数据污染防线 ────────────────────────────────────────────
# A股 ETF 单日涨跌停是 10%（跨境 ETF 亦然）。超过这个幅度的跳变不可能是行情，
# 只可能是份额折算、拆并份额或数据源出错。512890 在 2021-10-25 就发生过
# 1.639 → 0.801 的折算；若把它当成暴跌，引擎会误判浮亏而下错单。
SPLIT_JUMP = 0.15


def detect_splits(hist, cfg):
    """历史里是否存在只可能来自份额折算的价格跳变。

    扫全历史而不是只看近期 —— 折算污染的是持仓成本基准，
    不会因为时间过去就自己好。处理完后把日期写进 config 的
    resolved_splits，警报与冻结才解除。
    """
    keys = {1: ("p1_px", show(cfg["part1"])),
            2: ("p2_px", show(cfg["part2"])),
            3: ("p3_px", show(cfg["part3"], "hold_code"))}
    done = set(cfg.get("resolved_splits") or ())
    hits = []
    n = len(hist)
    for part, (k, nm) in keys.items():
        for i in range(1, n):
            a, b = hist[i - 1].get(k), hist[i].get(k)
            if not a or not b:
                continue
            ch = b / a - 1
            if abs(ch) > SPLIT_JUMP:
                hits.append({"part": part, "name": nm, "date": hist[i]["date"],
                             "prev": a, "now": b, "change": ch,
                             "resolved": hist[i]["date"] in done,
                             "bars_since": n - 1 - i})
    # 未处理 → 止盈与加码都停（成本基准被污染）
    frozen = {h["part"] for h in hits if not h["resolved"]}
    # 已处理但折算日还在 MA250 窗口内 → 只停止盈（均线仍跨着断点）
    ma_n = max(cfg["part1"]["ma_n"], cfg["part3"]["ma_n"])
    ma_frozen = {h["part"] for h in hits if h["bars_since"] < ma_n}
    return frozen, hits, ma_frozen


def _item(level, title, what, todo):
    return {"level": level, "title": title, "what": what, "todo": todo}


def pb_blind_days(hist, lookback=400):
    """创业板PB 已连续多少个交易日取不到。切换条款靠它，断供即失明。"""
    n = 0
    for r in reversed(hist[-lookback:]):
        if r.get("pb_pct") is None:
            n += 1
        else:
            break
    return n


def build_health(hist, cfg, st, ma_items, dup_warns, split_hits,
                 divs_age=None, skipped=(), fetch_ok=True, stale_days=0,
                 adj=None):
    """把所有故障收敛成一份「出了什么事 + 你该做什么」的清单。

    级别只有两档：critical 必须你动手，warn 可以先观察。
    每条都必须给出可执行的步骤 —— 只说「异常」而不说怎么办等于没说。
    """
    repo = cfg.get("repo_url", "你的 GitHub 仓库")
    out = []

    if not fetch_ok:
        out.append(_item(
            "critical", "所有数据源都取不到行情",
            "四个数据源全部失败，看板停在 %s，策略无法推进。" % hist[-1]["date"],
            ["打开 Actions 日志，看失败的是哪几个源",
             "多半是某个接口改版或限流；单源失效会自动降级，全挂通常是网络或接口同时变更",
             "确认后修改 src/fetch.py 的对应抓取函数"]))
    elif stale_days > int(cfg.get("alert_stale_days", 14)):
        out.append(_item(
            "critical", "行情已停滞 %d 天" % stale_days,
            "最新数据仍是 %s。若这期间A股开过市，说明写入链路断了。" % hist[-1]["date"],
            ["查 Actions 是否还在跑（cron-job.org 执行历史）",
             "若在跑，看日志里 merge 跳过了哪些天、缺哪一列"]))

    blind = pb_blind_days(hist)
    if blind >= int(cfg.get("pb_blind_days", 5)):
        out.append(_item(
            "critical", "切换条款已失明（PB 数据断供 %d 个交易日）" % blind,
            "创业板PB十年分位取不到，「PB≤%.0f%% 就全部清仓转创业板策略」"
            "这条当前没有在监控。这是三份共用的总开关。"
            % (cfg["switch"]["pb_percentile_threshold"] * 100),
            ["登录 lixinger.com 查 token 是否过期或额度用尽",
             "在 %s → Settings → Secrets and variables → Actions "
             "更新 LIXINGER_TOKEN" % repo,
             "更新后到 Actions → 每日监测 → Run workflow 手动跑一次验证"]))

    adj = adj or {}
    ma_n = cfg["part1"]["ma_n"]
    codes = {1: cfg["part1"]["code"], 2: cfg["part2"]["code"],
             3: cfg["part3"]["hold_code"]}
    for h0 in split_hits:
        code = codes[h0["part"]]
        f = (adj.get(code) or {}).get(h0["date"])
        left = ma_n - h0["bars_since"]

        if not h0["resolved"]:
            fx = ("系统按后复权数据推算为 1 : %g" % f if f
                  else "系统未能算出比例，请以公告为准")
            line = ("%s,%d,split,%s,%s,0,0,份额折算"
                    % (h0["date"], h0["part"], code,
                       ("%g" % f) if f else "填折算比例"))
            out.append(_item(
                "critical",
                "%s 发生份额折算 —— 需要你手工修一行" % h0["name"],
                "%s 单日从 %.4f 变到 %.4f（%+.1f%%）。A股ETF涨跌停只有10%%，"
                "这个幅度只可能是份额折算。你券商账户里的份数其实已经按比例变了，"
                "但系统还按旧份数算 —— 所以现在看板上这一份的市值、收益率、浮亏"
                "全是错的。止盈与加码已暂停（避免按错误浮亏下单），定投照常。"
                % (h0["date"], h0["prev"], h0["now"], h0["change"] * 100),
                ["到基金公司公告页核对折算比例（%s）" % fx,
                 "在 ledger.csv 末尾加这一行，比例填在 shares 列：  " + line,
                 "把 %s 加进 config.yaml 的 resolved_splits，例如 "
                 "resolved_splits: [\"%s\"]" % (h0["date"], h0["date"]),
                 "提交推送，然后到 Actions → 每日监测 → Run workflow 跑一次",
                 "注意：修完之后止盈信号还会自动继续冻结约 %d 个交易日"
                 "（约 %.1f 个月），因为 MA250 的窗口里仍跨着折算断点。"
                 "这段时间不需要你做任何事，到期自动恢复。" % (left, left / 20.5)]))
        elif h0["bars_since"] < ma_n:
            out.append(_item(
                "warn",
                "%s 止盈信号仍在恢复中（还需 %d 个交易日）" % (h0["name"], left),
                "%s 的折算你已处理，份额与成本已经对上了。但 MA250 要 250 根"
                "K线，窗口里现在还跨着折算那天的断点，算出来的均线没有意义，"
                "所以止盈信号继续暂停。加码与定投都正常。"
                % h0["date"],
                ["不需要做任何事，%d 个交易日后自动恢复" % left,
                 "这段时间如果中概互联大跌，系统不会提示止盈 —— "
                 "若你想自己判断，请直接看行情软件里的 MA250（它是复权的，没有这个问题）"]))

    for k, nm in (("p1", "中概互联"), ("p2", "红利低波"), ("p3", "红利网格")):
        if st and st[k]["cash"] < -1:
            out.append(_item(
                "critical", "%s 这份现金穿负（%.0f 元）" % (nm, st[k]["cash"]),
                "现金为负说明账实不符，最常见的原因是把引擎已自动执行的成交"
                "又抄进了 ledger.csv，等于同一笔买了两次。",
                ["打开 ledger.csv，删掉与看板「近期事件」重复的行",
                 "记住：照系统说的做了就什么都不用记，账本只记「和系统说的不一样」的部分",
                 "改完提交推送，下次运行会自动重算"]))

    for x in ma_items:
        if not x["ok"]:
            out.append(_item(
                "critical", "均线信号停摆：%s" % x["label"],
                x["msg"] + " 对应的买卖点当前不会触发。",
                ["查 Actions 日志里 merge 跳过了哪些天",
                 "MA250 需要 250 个连续非空值，中间缺一天就要 250 个交易日才恢复",
                 "若确认是数据源问题，修好后历史会自动补齐并自愈"]))

    for w in dup_warns:
        out.append(_item(
            "critical", "账本与系统记录冲突", w,
            ["按上面提示删掉 ledger.csv 里重复的那一行",
             "提交推送后下次运行会自动重算"]))

    if divs_age is not None and divs_age > int(cfg.get("divs_stale_days", 60)):
        out.append(_item(
            "warn", "分红表已 %d 天未成功刷新" % divs_age,
            "分红靠 data/dividends.json 自动入账。表过期不影响买卖信号，"
            "但中证红利每年10月那次分红可能漏记，收益会被低估。",
            ["本地运行 python src/dividends.py 看两个源是否都挂了",
             "天天基金页面改版时需要更新 src/dividends.py 的解析规则"]))

    if skipped:
        out.append(_item(
            "warn", "有 %d 天因数据不全被跳过" % len(skipped),
            "缺日：%s。跳过是为保护 MA250 连续性，但会让均线窗口变旧。"
            % "、".join(list(skipped)[:5]),
            ["多数情况下数据源补发后会自动补齐，无需处理",
             "若连续多天跳过，查 Actions 日志确认是哪一列长期缺失"]))
    return out
