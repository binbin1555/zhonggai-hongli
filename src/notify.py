# -*- coding: utf-8 -*-
"""Bark 推送。

推送策略
--------
有待执行动作   强提醒（timeSensitive），第一行就是命令
今日已执行     强提醒，说明发生了什么
无事           静默提醒（passive），标题即答案
故障           强提醒，无论是否交易日
"""
import json
import datetime
import os
import urllib.request

BARK_KEY = (os.environ.get("BARK_KEY") or "").strip()
BARK_HOST = ((os.environ.get("BARK_HOST") or "").strip()
             or "https://api.day.app").rstrip("/")

# 兜底标题。正常情况下每条 pending 自带 label。
ACT_TEXT = {
    "P1_DCA": "中概互联 · 定投买入",
    "P1_ACCEL": "中概互联 · 加码买入",
    "P1_EXIT": "中概互联 · 止盈清仓",
    "P3_TIER": "红利网格 · 调整仓位",
    "SWITCH": "三份全部清仓 · 转创业板策略",
}


HEART = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "data", "heartbeat.json")


def _mark_alive():
    """推送成功即打点。心跳靠它判断系统「多久没出过声」。"""
    try:
        os.makedirs(os.path.dirname(HEART), exist_ok=True)
        day = datetime.datetime.now(
            datetime.timezone(datetime.timedelta(hours=8))).strftime("%Y-%m-%d")
        with open(HEART, "w", encoding="utf-8") as f:
            json.dump({"last_push": day}, f, ensure_ascii=False)
    except Exception as exc:                                  # noqa: BLE001
        print("[bark] 心跳打点失败: %r" % exc)


def days_since_push(today):
    """距上次成功推送的天数。无记录返回 9999。"""
    try:
        with open(HEART, encoding="utf-8") as f:
            d = json.load(f).get("last_push")
        return (today - datetime.date(*map(int, d.split("-")))).days
    except Exception:                                         # noqa: BLE001
        return 9999


def bark(cfg, title, body, level="active", group="策略A"):
    if not BARK_KEY:
        print("[bark] 未配置 BARK_KEY，跳过推送")
        return False
    payload = {"device_key": BARK_KEY, "title": title, "body": body,
               "level": level, "group": group, "isArchive": 1}
    url = (cfg.get("page_url") or "").strip()
    if url.startswith("http"):
        payload["url"] = url
    try:
        req = urllib.request.Request(
            BARK_HOST + "/push",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"})
        with urllib.request.urlopen(req, timeout=30) as r:
            ok = json.loads(r.read().decode("utf-8", "ignore"))
        print("[bark] %s" % ok.get("message", ok))
        _mark_alive()
        return True
    except Exception as exc:                                  # noqa: BLE001
        print("[bark] 推送失败: %r" % exc)
        return False


def _money(x):
    return "{:,.0f}".format(x)


def _pct(x, digits=2):
    if x is None:
        return "—"
    if abs(x) < 5e-5:
        return ("%." + str(digits) + "f%%") % 0.0
    return ("%+." + str(digits) + "f%%") % (x * 100)


def build_body(cfg, out):
    st, met = out["state"], out.get("metrics") or {}
    px = out["last_prices"]
    lines = []

    for w in (out.get("ma_warns") or []):
        lines.append("⚠️ 均线停摆：" + w)
    if out.get("ma_warns"):
        lines.append("")

    pend = out.get("pending") or []
    if pend:
        p = pend[0]
        when = p["exec_date"]
        lines.append("【今日指令】%s 收盘执行：%s"
                     % (when, p.get("label") or ACT_TEXT.get(p["action"], p["action"])))
        lines.append("　" + p.get("detail", ""))
    else:
        lines.append("【今日指令】不操作，继续持有")
    lines.append("")

    v1 = st["p1"]["units"] * px["p1_px"] + st["p1"]["cash"]
    v2 = st["p2"]["units"] * px["p2_px"] + st["p2"]["cash"]
    v3 = st["p3"]["units"] * px["p3_px"] + st["p3"]["cash"]
    lines.append("① 中概互联 %s（持仓 %s ／ 待投现金 %s）"
                 % (_money(v1), _money(st["p1"]["units"] * px["p1_px"]),
                    _money(st["p1"]["cash"])))
    lines.append("② 红利低波 %s（满仓持有，不操作）" % _money(v2))
    lines.append("③ 红利网格 %s（%d/3 仓，其余为待投现金）"
                 % (_money(v3), st["p3"]["tier"]))
    lines.append("")
    lines.append("总资产 %s（%s）" % (_money(met.get("equity", 0)),
                                     _pct(met.get("total_return"))))
    if met.get("cagr") is not None:
        lines.append("年化 %s ／ 最大回撤 %s"
                     % (_pct(met["cagr"]), _pct(met.get("max_drawdown"))))
    else:
        lines.append("最大回撤 %s（运行未满一月，年化暂不显示）"
                     % _pct(met.get("max_drawdown")))

    nr = _near(out)
    if nr and not pend:
        lines.append("")
        lines.append("── 临近触发，请留意 ──")
        for t in nr:
            lines.append("⚠️ %s：%s" % (t["label"], t["short"]))
            lines.append("　" + t["cond"])

    trig = out.get("triggers") or []
    if trig:
        lines.append("")
        lines.append("── %s ──" % ("其它观察点" if pend else "观察点（均未触发）"))
        for t in trig[:3]:
            lines.append("距「%s」：%s" % (t["label"], t["short"]))
    lines.append("")
    lines.append("数据截至 %s" % out["data_updated"])
    return "\n".join(lines)


def _near(out):
    """已进入临近区间的观察点，按接近程度排序（next_triggers 已排好）。"""
    return [t for t in (out.get("triggers") or []) if t.get("near")]


def push_daily(cfg, out, force=False):
    body = build_body(cfg, out)
    near = _near(out)
    pend = out.get("pending") or []
    acted = [e for e in (out.get("recent_events") or [])
             if e["date"] == out["data_updated"]]

    if force:
        return bark(cfg, "✅ 测试推送 · 配置正常",
                    "能看到这条，说明 BARK_KEY 与 Actions 全链路都通了。\n\n" + body,
                    level="timeSensitive")
    if out.get("ma_warns"):
        return bark(cfg, "🟡 均线信号停摆 · 需检查", body, level="timeSensitive")
    if pend:
        head = pend[0].get("label") or ACT_TEXT.get(pend[0]["action"], "需操作")
        return bark(cfg, "🔴 明日需操作 · %s" % head, body, level="timeSensitive")
    if acted:
        title = "🟠 今日已执行 · " + "／".join(e["action"] for e in acted)
        detail = "\n".join("· %s" % e["detail"] for e in acted)
        return bark(cfg, title, detail + "\n\n" + body, level="timeSensitive")
    if near:
        n = near[0]
        return bark(cfg,
                    "🟡 临近触发 · %s（%s）" % (n["label"], n["short"]),
                    body, level="timeSensitive")
    # 无操作也每个交易日推一条。级别可在 config 调（passive 为静默投递，不亮屏）
    return bark(cfg, "⚪ %s · 今日无操作" % out["data_updated"], body,
                level=str(cfg.get("quiet_push_level", "active")))
