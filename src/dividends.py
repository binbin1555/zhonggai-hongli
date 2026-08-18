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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "data", "dividends.json")

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
    """返回 {除息日: 每份派现}。无分红的 ETF 返回空 dict（正常，非错误）。"""
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
        out[c[2]] = float(m.group(2)) / float(m.group(1))
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
                if d in pri and pri[d] > 0:
                    if abs(v - pri[d]) / pri[d] > AGREE_TOL:
                        log.append("WARN %s %s 两源不符：主 %.4f / 校验 %.4f"
                                   % (code, d, pri[d], v))
                else:
                    log.append("WARN %s %s 校验源有 %.4f，主源无记录"
                               % (code, d, v))
        res[symbol] = pri if pri is not None else (sec or {})
        if pri is None and sec is None:
            log.append("FAIL %s 两源全挂，本次不更新该标的" % code)
            res.pop(symbol, None)
    return res, log


def part_codes(cfg):
    """三份各自持有的 ETF 代码。第三份的信号是指数、持仓是 ETF，取 hold_code。"""
    syms = [cfg["part1"]["code"], cfg["part2"]["code"], cfg["part3"]["hold_code"]]
    return {s: s[2:] for s in syms}


def load():
    if not os.path.exists(OUT):
        return {}
    with open(OUT, encoding="utf-8") as f:
        return json.load(f)


def save(res):
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    old = load()
    old.update(res)                     # 抓失败的标的保留上次结果，不清空
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(old, f, ensure_ascii=False, indent=1, sort_keys=True)
    return old


def main():
    import yaml
    with open(os.path.join(ROOT, "config.yaml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    codes = part_codes(cfg)
    res, log = collect(codes)
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
            print("  %s 共 %d 次，最近 %s 每份 %.4f 元"
                  % (sym, len(rec), last, rec[last]))
    print("\n已写入 %s" % OUT)


if __name__ == "__main__":
    main()
