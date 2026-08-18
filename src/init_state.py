# -*- coding: utf-8 -*-
"""由 config.yaml + ledger.csv 生成 state.json 的初始状态。

只在首次部署、或需要按流水重建状态时运行：
    python src/init_state.py

引擎日常运行不依赖本脚本 —— 它只负责把「你实际成交了什么」翻译成引擎的起始状态。
"""
import csv
import json
import os
import sys

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from engine import new_state          # noqa: E402


def load_cfg():
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_ledger():
    p = os.path.join(ROOT, "ledger.csv")
    if not os.path.exists(p):
        return []
    rows = []
    with open(p, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if not r.get("date"):
                continue
            rows.append({
                "date": r["date"].strip(),
                "part": int(r["part"]),
                "action": r["action"].strip().lower(),
                "code": (r.get("code") or "").strip(),
                "shares": float(r["shares"] or 0),
                "price": float(r["price"] or 0),
                "amount": float(r["amount"] or 0),
                "note": (r.get("note") or "").strip(),
            })
    return sorted(rows, key=lambda x: (x["date"], x["part"]))


def build(cfg, rows):
    st = new_state(cfg)
    st["p1"]["cash"] = float(cfg["part1"]["capital"])
    st["p2"]["cash"] = float(cfg["part2"]["capital"])
    st["p3"]["cash"] = float(cfg["part3"]["capital"])

    key = {1: "p1", 2: "p2", 3: "p3"}
    for r in rows:
        k = key.get(r["part"])
        if k is None:
            raise ValueError("ledger 第 %s 行 part 必须是 1/2/3" % r["date"])
        h = st[k]
        amt = r["amount"] or r["shares"] * r["price"]
        if r["action"] == "buy":
            if amt > h["cash"] + 1e-6:
                raise ValueError("%s 第%d份买入 %.2f 超出可用现金 %.2f"
                                 % (r["date"], r["part"], amt, h["cash"]))
            h["units"] += r["shares"]
            h["cash"] -= amt
            h["cost"] += amt
            if k == "p1":
                if h["first_buy_date"] is None:
                    h["first_buy_date"] = r["date"]
                if "定投" in r["note"]:
                    h["dca_done"] += 1
        elif r["action"] in ("sell", "exit"):
            h["units"] -= r["shares"]
            h["cash"] += amt
            h["cost"] = max(0.0, h["cost"] - amt)
        elif r["action"] == "dividend":
            h["cash"] += amt                      # 分红并入该份现金池
        else:
            raise ValueError("未知 action: %s" % r["action"])
    return st


def main():
    cfg = load_cfg()
    rows = read_ledger()
    st = build(cfg, rows)

    tot_cash = st["p1"]["cash"] + st["p2"]["cash"] + st["p3"]["cash"]
    tot_cost = st["p1"]["cost"] + st["p2"]["cost"] + st["p3"]["cost"]
    print("按 ledger.csv 重建初始状态（%d 条流水）" % len(rows))
    print("-" * 62)
    for k, nm in (("p1", "第一份 中概互联"), ("p2", "第二份 红利低波"), ("p3", "第三份 红利网格")):
        h = st[k]
        print("  %-14s 份额 %12.0f   成本 %11.2f   现金 %11.2f"
              % (nm, h["units"], h["cost"], h["cash"]))
    print("-" * 62)
    print("  投入合计 %.2f + 现金合计 %.2f = %.2f （应等于 %.2f）"
          % (tot_cost, tot_cash, tot_cost + tot_cash, cfg["total_capital"]))
    diff = abs(tot_cost + tot_cash - cfg["total_capital"])
    if diff > 1:
        print("  !! 差额 %.2f 元，请检查 ledger.csv" % diff)
    else:
        print("  账平（差额 %.2f 元）" % diff)

    out = os.path.join(ROOT, "state.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)
    print("\n已写入 %s" % out)


if __name__ == "__main__":
    main()
