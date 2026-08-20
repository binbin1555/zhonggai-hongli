# -*- coding: utf-8 -*-
"""ETF 分红自动抓取 —— 取代手工在 ledger.csv 里记分红。

两个独立来源互相校验，与 fetch.py 同样的多源降级思路：
  主源  天天基金 F10 分红送配页（权威：除息日 + 每份派现精确到 0.0001）
  校验  腾讯后复权/不复权价的比值跳变反推（无需额外站点，但有舍入噪声）

两源都拿到时必须吻合（差异 <5%），否则记 warn 并以主源为准。
主源失败时用校验源兜底，但只认跳变 >= 1.5% 的（低于此为舍入噪声）。

用法：
    python src/dividends.py          # 刷新 data/dividends.json 并打印
"""
import json
import os
import re
import ssl
import urllib.request
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "dividends.json")
ADJ = os.path.join(ROOT, "data", "adjust.json")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

NOISE_FLOOR = 0.015          # 比值跳变低于 1.5% 视为舍入噪声，不认作分红
AGREE_TOL = 0.05             # 两源相对差异容忍度


def _get(url, referer=None, timeout=30):
    h = {"User-Agent": UA}
    if referer:
        h["Referer"] = referer
    req = urllib.request.Request(url, headers=h)
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_CTX))
    return op.open(req, timeout=timeout).read().decode("utf-8", "ignore")


# ── 主源：天天基金 F10 ────────────────────────────────────────
def eastmoney(code):
    """返回 {除息日: {"per": 每份派现, "pay": 分红发放日}}。

    除息日与发放日相隔 3–5 个自然日。执行文档规定「收到现金分红后，
    下一个交易日并入该份的现金池」，所以入账要按发放日算，不是除息日。
    无分红的 ETF 返回空 dict（正常，非错误）。
    """
    t = _get("https://fundf10.eastmoney.com/fhsp_%s.html" % code,
             "https://fundf10.eastmoney.com/")
    blk = re.search(r"分红送配详情.*?</table>", t, re.S)
    if not blk:
        raise RuntimeError("eastmoney %s: 未找到分红表" % code)
    out = {}
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", blk.group(0), re.S):
        c = [re.sub(r"<[^>]+>", "", x).strip()
             for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]
        if len(c) < 4 or not re.match(r"^\d{4}-\d{2}-\d{2}$", c[2] or ""):
            continue
        m = re.search(r"每(\d+)份派现金([\d.]+)元", c[3] or "")
        if not m:
            continue
        pay = c[4] if len(c) > 4 and re.match(r"^\d{4}-\d{2}-\d{2}$", c[4] or "") else None
        out[c[2]] = {"per": float(m.group(2)) / float(m.group(1)), "pay": pay}
    return out


# ── 校验源：腾讯复权价比值 ───────────────────────────────────
def _tencent(symbol, fq, start="2015-01-01"):
    import datetime
    end = datetime.date.today()
    out = {}
    for y in range(int(start[:4]), end.year + 1):
        a = "%d-01-01" % y
        b = min(end, datetime.date(y, 12, 31)).isoformat()
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
               "?param=%s,day,%s,%s,320,%s" % (symbol, a, b, fq))
        try:
            n = json.loads(_get(url))["data"][symbol]
        except Exception:
            continue
        for r in (n.get("hfqday") or n.get("day") or []):
            if r[2]:
                out[r[0]] = float(r[2])
    return out


SPLIT_JUMP = 0.15          # 与 engine.SPLIT_JUMP 一致：超过涨跌停的跳变只可能是折算


def split_factors(symbol):
    """份额折算的精确比例。

    折算当天不复权价按比例跳水，后复权价则连续。两者比值的跳变幅度
    就是折算比例，且已剔除当天真实涨跌 —— 用收盘价直接相除会把涨跌
    混进去（512890 那次实测差 2.31%）。
    """
    raw, hfq = _tencent(symbol, ""), _tencent(symbol, "hfq")
    ds = sorted(set(raw) & set(hfq))
    if len(ds) < 30:
        raise RuntimeError("tencent %s: 复权序列不足" % symbol)
    out = {}
    for i in range(1, len(ds)):
        d, dp = ds[i], ds[i - 1]
        if abs(raw[d] / raw[dp] - 1) <= SPLIT_JUMP:
            continue                      # 不复权价没大跳，不是折算
        f = (hfq[d] / raw[d]) / (hfq[dp] / raw[dp])
        if f > 1.05:                      # 比值必须放大，且幅度可观
            out[d] = round(f, 6)
    return out


def infer_from_hfq(symbol):
    """后复权/不复权比值在除息日跳变，跳变幅度反推每份分红。

    仅认跳变 >= NOISE_FLOOR 的；更小的是两条序列小数位不同造成的舍入噪声。
    份额折算也会造成跳变，但方向相反（比值下降），已排除。
    """
    raw, hfq = _tencent(symbol, ""), _tencent(symbol, "hfq")
    ds = sorted(set(raw) & set(hfq))
    if len(ds) < 30:
        raise RuntimeError("tencent %s: 复权序列不足" % symbol)
    out = {}
    for i in range(1, len(ds)):
        d, dp = ds[i], ds[i - 1]
        r, rp = hfq[d] / raw[d], hfq[dp] / raw[dp]
        if r <= rp:                      # 比值必须上升才是分红
            continue
        D = raw[dp] * (1 - rp / r)
        if D / raw[dp] >= NOISE_FLOOR:
            out[d] = round(D, 4)
    return out


# ── 汇总 ─────────────────────────────────────────────────────
def collect(codes):
    """codes: {'sh515180': '515180', ...}。返回 (结果, 日志行列表)"""
    res, log = {}, []
    for symbol, code in codes.items():
        pri = sec = None
        try:
            pri = eastmoney(code)
            log.append("OK   eastmoney %s 分红 %d 条" % (code, len(pri)))
        except Exception as e:
            log.append("FAIL eastmoney %s: %s" % (code, e))
        try:
            sec = infer_from_hfq(symbol)
            log.append("OK   复权推断 %s 分红 %d 条" % (symbol, len(sec)))
        except Exception as e:
            log.append("FAIL 复权推断 %s: %s" % (symbol, e))

        if pri is not None and sec:
            for d, v in sec.items():
                pv = (pri.get(d) or {}).get("per", 0) if d in pri else 0
                if pv > 0:
                    if abs(v - pv) / pv > AGREE_TOL:
                        log.append("WARN %s %s 两源不符：主 %.4f / 校验 %.4f"
                                   % (code, d, pv, v))
                else:
                    log.append("WARN %s %s 校验源有 %.4f，主源无记录"
                               % (code, d, v))
        # 校验源只有金额没有发放日，退化时按除息日入账并标注
        res[symbol] = (pri if pri is not None
                       else {d: {"per": v, "pay": None} for d, v in (sec or {}).items()})
        if pri is None and sec is None:
            log.append("FAIL %s 两源全挂，本次不更新该标的" % code)
            res.pop(symbol, None)
    return res, log


def part_codes(cfg):
    """三份各自持有的 ETF 代码。第三份的信号是指数、持仓是 ETF，取 hold_code。"""
    syms = [cfg["part1"]["code"], cfg["part2"]["code"], cfg["part3"]["hold_code"]]
    return {s: s[2:] for s in syms}


def load():
    """返回 {代码: {除息日: 每份派现}}；文件缺失或损坏都退化为空表，绝不抛错。"""
    try:
        with open(OUT, encoding="utf-8") as f:
            d = json.load(f)
        return {k: v for k, v in d.items() if not k.startswith("_")}
    except Exception:                                       # noqa: BLE001
        return {}


def age_days():
    """距上次成功刷新的天数。取 json 内的时间戳 ——
    不能用文件 mtime：GitHub Actions 每次全新 checkout，mtime 恒为当次运行时刻。"""
    try:
        with open(OUT, encoding="utf-8") as f:
            ts = json.load(f).get("_refreshed_at")
        if not ts:
            return 9999
        d = date(*map(int, ts[:10].split("-")))
        return (date.today() - d).days
    except Exception:                                       # noqa: BLE001
        return 9999


def save(res):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    old = load()
    old.update(res)                     # 抓失败的标的保留上次结果，不清空
    out = dict(old)
    out["_refreshed_at"] = date.today().isoformat()
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, sort_keys=True)
    return old


def load_adj():
    try:
        with open(ADJ, encoding="utf-8") as f:
            return {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    except Exception:                                       # noqa: BLE001
        return {}


def main():
    import yaml
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    codes = part_codes(cfg)
    res, log = collect(codes)
    adj = {}
    for sym in codes:
        try:
            adj[sym] = split_factors(sym)
            if adj[sym]:
                log.append("!!   %s 检出份额折算 %s" % (sym, adj[sym]))
        except Exception as e:                              # noqa: BLE001
            log.append("FAIL 折算比例 %s: %s" % (sym, e))
    if adj:
        adj["_refreshed_at"] = date.today().isoformat()
        os.makedirs(os.path.dirname(ADJ), exist_ok=True)
        with open(ADJ, "w", encoding="utf-8") as f:
            json.dump(adj, f, ensure_ascii=False, indent=1, sort_keys=True)
    for line in log:
        print("  " + line)
    all_ = save(res)
    print()
    for sym in sorted(all_):
        rec = all_[sym]
        if not rec:
            print("  %s 无分红记录" % sym)
        else:
            last = sorted(rec)[-1]
            r = rec[last]
            print("  %s 共 %d 次，最近除息 %s 每份 %.4f 元，发放日 %s"
                  % (sym, len(rec), last, r["per"], r.get("pay") or "未知"))
    print("\n已写入 %s" % OUT)


if __name__ == "__main__":
    main()
