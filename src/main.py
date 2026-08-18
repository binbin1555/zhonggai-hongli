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


def main():
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    token = (os.environ.get("LIXINGER_TOKEN") or "").strip()
    now = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8)))
    today = now.date()

    hist = load_json(HIST, [])
    log, fetch_ok, added, revised, skipped = [], True, [], [], []

    # ── 1. 抓取 ────────────────────────────────────────────────
    try:
        days = 1200 if len(hist) < 300 else 120
        series, log = F.fetch_all(cfg, token, days=days)
        # 盘中保护：收盘结算前抓到的当日数据是实时价，丢弃
        if now.hour < 17:
            key = today.strftime("%Y-%m-%d")
            if any(key in v for v in series.values()):
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
        print("  非交易日（最新数据 %s ≠ 今日 %s），静默" % (last_date, today))
        return 0
    N.push_daily(cfg, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
