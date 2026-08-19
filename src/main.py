# -*- coding: utf-8 -*-
"""每日主流程：抓数据 → 从头重放 → 写状态 → 推送。

架构要点
--------
真相来源只有三样：config.yaml（规则）、ledger.csv（真实成交）、data/history.json（行情）。
state.json / docs/data.json 都是**推导产物**，每次从头重算 —— 幂等、可自愈、可审计。

退出码：0 正常；1 数据或引擎异常（已告警）。
"""
import datetime
import json
import os
import time
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import engine as E          # noqa: E402
import fetch as F           # noqa: E402
import notify as N          # noqa: E402
from init_state import build as build_state, read_ledger   # noqa: E402
import dividends as DIV                                    # noqa: E402

HIST = os.path.join(ROOT, "data", "history.json")
STATE = os.path.join(ROOT, "state.json")
DOCS = os.path.join(ROOT, "docs", "data.json")
ACK = os.path.join(ROOT, "acknowledged.json")


def today_bj():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date()


def load_json(p, default):
    if not os.path.exists(p):
        return default
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:                                        # noqa: BLE001
        return default


def _weekdays_between(a, b):
    """(a, b] 之间的工作日个数。a 为字符串日期，b 为 date。"""
    d = datetime.date(*map(int, a.split("-")))
    n = 0
    while d < b:
        d = datetime.date.fromordinal(d.toordinal() + 1)
        if d.weekday() < 5:
            n += 1
    return n


def main():
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    token = (os.environ.get("LIXINGER_TOKEN") or "").strip()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    today = now.date()

    hist = load_json(HIST, [])
    log, fetch_ok, added, revised, skipped = [], True, [], [], []
    # 今天 A 股到底开没开市 —— 独立判定，不从「有没有写进 hist」反推。
    # 只要任一数据源给出了今日行情，就说明开市了。
    market_open, too_early = None, False

    # ── 1. 抓取 ────────────────────────────────────────────────
    try:
        days = 1200 if len(hist) < 300 else 120
        series, log = F.fetch_all(cfg, token, days=days)
        key = today.strftime("%Y-%m-%d")
        market_open = any(key in v for v in series.values())
        # 盘中保护：收盘结算前抓到的当日数据是实时价，丢弃
        if now.hour < 17:
            if market_open:
                too_early = True
                for v in series.values():
                    v.pop(key, None)
                log.append("     丢弃 %s 盘中数据（当前 %02d:%02d 早于收盘结算）"
                           % (key, now.hour, now.minute))
        added, revised, skipped = F.merge(hist, series)
    except F.AllSourcesFailed as exc:
        fetch_ok = False
        log.append("ALL SOURCES FAILED: %s" % exc)
    except Exception as exc:                                 # noqa: BLE001
        fetch_ok = False
        log.append("UNEXPECTED: %r" % exc)

    for line in log:
        print("  " + line)
    if skipped:
        print("  !! 跳过 %d 天（必需列缺失，未写入以保护 MA250 连续性）：%s"
              % (len(skipped), skipped[:5]))

    if not hist:
        N.bark(cfg, "⚠️ 无行情数据", "首次运行未能获取任何数据，请检查网络与数据源。",
               level="timeSensitive")
        return 1

    last_date = hist[-1]["date"]
    stale_days = (today - datetime.date(*map(int, last_date.split("-")))).days
    stale = stale_days > int(cfg.get("alert_stale_days", 14))

    # ── 1b. 分红表（每周刷一次即可）────────────────────────────
    # 从头重放的架构下，晚几天发现某次分红不影响结果 ——
    # 下次重放会按正确的除息日补记回去，自愈。
    divs = DIV.load()
    if DIV.age_days() > 7:
        try:
            res, dlog = DIV.collect(DIV.part_codes(cfg))
            for line in dlog:
                print("  " + line)
            divs = DIV.save(res)
        except Exception as e:                              # noqa: BLE001
            print("  WARN 分红表刷新失败，沿用上次：%r" % (e,))

    # ── 2. 从头重放 ────────────────────────────────────────────
    st = curve = met = trig = None
    ledger_warns = []
    ma_warns = []
    notices = []
    try:
        rows = read_ledger()
        state0 = build_state(cfg, [r for r in rows if r["date"] <= cfg["start_date"]])
        state0["start_date"] = cfg["start_date"]
        later = [r for r in rows if r["date"] > cfg["start_date"]]
        st, curve = E.run(hist, cfg, state0, injections=later, divs=divs)
        met = E.metrics(curve, cfg["total_capital"])
        last = dict(hist[-1])
        p1 = [h["p1_px"] for h in hist]
        sig = [h["sig_px"] for h in hist]
        last["ma1"] = E.sma(p1, cfg["part1"]["ma_n"], len(hist) - 1)
        last["ma3"] = E.sma(sig, cfg["part3"]["ma_n"], len(hist) - 1)
        trig = E.next_triggers(st, cfg, last)
        notices = E.build_notices(st, last_date,
                                  int(cfg.get("notice_days", 14)))
        for x in E.ma_health(hist, cfg):
            if x["ok"]:
                print("  OK   %s = %.4f（窗口跨 %d 天）" % (x["label"], x["value"], x["span"]))
            else:
                print("  WARN " + x["msg"])
                ma_warns.append(x["msg"])
        for w in E.check_duplicates(st["events"]):
            print("  WARN 重复记账：" + w)
            ledger_warns.append(w)
    except Exception as exc:                                 # noqa: BLE001
        import traceback
        traceback.print_exc()
        print("  策略引擎失败: %r" % exc)

    # ── 3. 落盘 ────────────────────────────────────────────────
    with open(HIST, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, separators=(",", ":"))

    ack = load_json(ACK, {})
    out = {
        "generated_at": now.strftime("%Y-%m-%d %H:%M:%S+08:00"),
        "data_updated": last_date,
        "stale_days": stale_days,
        "fetch_ok": fetch_ok,
        "added": added, "revised": revised, "skipped": skipped,
        "log": log,
        "acknowledged": ack,
        "ledger_warns": ledger_warns,
        "ma_warns": ma_warns,
        "notices": notices,
        "config": {"total_capital": cfg["total_capital"],
                   "p1_code": cfg["part1"]["code"],
                   "p2_code": cfg["part2"]["code"],
                   "p3_code": cfg["part3"]["hold_code"],
                   "sig_code": cfg["part3"]["signal_code"],
                   "exit_ratio": cfg["part3"]["exit_ratio"],
                   "buy_tiers": cfg["part3"]["buy_tiers"]},
    }
    if st:
        out.update({
            "state": {"p1": st["p1"], "p2": st["p2"], "p3": st["p3"],
                      "switched": st["switched"], "switch_date": st["switch_date"]},
            "pending": st["pending"],
            "metrics": met,
            "triggers": trig,
            "recent_events": st["events"][-15:],
            "last_prices": {"p1_px": hist[-1]["p1_px"], "p2_px": hist[-1]["p2_px"],
                            "p3_px": hist[-1]["p3_px"], "sig_px": hist[-1]["sig_px"],
                            "pb_pct": hist[-1].get("pb_pct")},
            "curve": [{"d": c["date"], "v": round(c["total"], 2)} for c in curve[-250:]],
        })
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    os.makedirs(os.path.dirname(DOCS), exist_ok=True)
    with open(DOCS, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    # ── 4. 推送 ────────────────────────────────────────────────
    if not fetch_ok or stale:
        why = "所有数据源均不可达" if not fetch_ok else "数据已停滞 %d 天" % stale_days
        N.bark(cfg, "⚠️ 数据异常",
               "%s\n最新数据仍为 %s\n请检查接口是否改版或被限流。" % (why, last_date),
               level="timeSensitive")
        return 1
    if not st:
        N.bark(cfg, "⚠️ 策略引擎失败",
               "数据已更新至 %s，但引擎报错，请查看 Actions 日志。" % last_date,
               level="timeSensitive")
        return 1

    is_trading_day = last_date == today.strftime("%Y-%m-%d")
    if (os.environ.get("TEST_PUSH") or "").strip().lower() in ("1", "true", "yes"):
        N.push_daily(cfg, out, force=True)
        return 0

    if not is_trading_day:
        # 开市了却没写进 hist —— 这不是非交易日，是数据缺失，必须告警
        if market_open and not too_early:
            miss = [k for k in F.REQUIRED
                    if today.strftime("%Y-%m-%d") not in (series.get(k) or {})]
            N.bark(cfg, "⚠️ 今日数据不全 · 已跳过",
                   ("%s 是交易日（其它数据源已有行情），但 %s 未取到。"
                    "整行未写入，以保护 MA250 连续性。"
                    "看板仍停在 %s，请查看 Actions 日志。")
                   % (today, "、".join(miss) or "某必需列", last_date),
                   level="timeSensitive")
            print("  !! 开市但数据不全，缺 %s —— 已告警" % miss)
            return 1
        # 连续静默过久也要告警：A股最长假期（春节）约 7 个工作日，阈值 9 留余量
        quiet = _weekdays_between(last_date, today)
        if quiet > int(cfg.get("max_quiet_weekdays", 9)):
            N.bark(cfg, "⚠️ 连续 %d 个工作日无新数据" % quiet,
                   ("最新数据仍为 %s。已超过任何法定假期长度，"
                    "数据源很可能已失效，请检查 Actions 日志。") % last_date,
                   level="timeSensitive")
            print("  !! 连续静默 %d 个工作日 —— 已告警" % quiet)
            return 1
        # 心跳：长假期间也要定期出声。否则「没收到推送」与「系统已死」
        # 在手机上长得一模一样，而人对「没发生的事」是无感的。
        gap = N.days_since_push(today)
        hb = int(cfg.get("heartbeat_days", 6))
        if gap >= hb:
            how = ("首次运行" if gap > 9000 else "已 %d 天无推送" % gap)
            N.bark(cfg, "💚 系统正常 · 心跳（%s）" % how,
                   N.build_body(cfg, out), level="passive")
            print("  心跳：%s（阈值 %d 天），已报平安" % (how, hb))
            return 0
        print("  非交易日（最新数据 %s ≠ 今日 %s，静默 %d 个工作日）"
              % (last_date, today, quiet))
        return 0
    N.push_daily(cfg, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
