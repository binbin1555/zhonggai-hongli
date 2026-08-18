# -*- coding: utf-8 -*-
"""多源降级抓取。

设计原则（吸取 yearline-dashboard 的教训）
----------------------------------------
1. **绝不写半行。** 任一必需序列缺失，整天跳过。
   —— MA250 要求 250 个连续非空值，一个空洞会毁掉之后 250 天的信号。
2. **宁可不更新，绝不用陈旧数据冒充新数据。**
3. **多源降级**，任一源失败自动换下一个。
4. PB 分位是唯一可选列：缺了只表示「今天不判断切换」，是安全的保守行为。

必需序列
--------
p1_px   513050 中概互联ETF     收盘
p2_px   512890 红利低波ETF     收盘
p3_px   515180 中证红利ETF     收盘
sig_px  000922 中证红利指数    收盘（网格信号）

可选序列
--------
pb_pct  创业板指 PB 十年分位（理杏仁）
"""
import datetime
import json
import ssl
import urllib.request

try:
    import requests
except ImportError:
    requests = None

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

LIXINGER_FUND_URL = "https://open.lixinger.com/api/cn/index/fundamental"
LIXINGER_HEADERS = {"Content-Type": "application/json",
                    "Accept-Encoding": "gzip, deflate, br"}

REQUIRED = ("p1_px", "p2_px", "p3_px", "sig_px")
OPTIONAL = ("pb_pct",)


class AllSourcesFailed(Exception):
    pass


def _get(url, timeout=45, encoding="utf-8"):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    op = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_CTX))
    with op.open(req, timeout=timeout) as r:
        return r.read().decode(encoding, "ignore")


# ── 源 1：腾讯（ETF 与指数收盘）───────────────────────────────
def tencent(symbol, days=400):
    """腾讯单次最多返回 320 行，超过一年的区间按自然年分页拉取。"""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    out = {}
    for y in range(start.year, end.year + 1):
        a = max(start, datetime.date(y, 1, 1))
        b = min(end, datetime.date(y, 12, 31))
        url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,320,"
               % (symbol, a.strftime("%Y-%m-%d"), b.strftime("%Y-%m-%d")))
        try:
            j = json.loads(_get(url))
            node = j["data"][symbol]
            rows = node.get("day") or node.get("qfqday") or []
            for r in rows:
                if r[2]:
                    out[r[0]] = float(r[2])
        except Exception:
            continue
    if not out:
        raise RuntimeError("tencent %s empty" % symbol)
    return out


# ── 源 2：新浪（ETF 收盘兜底）─────────────────────────────────
def sina(symbol, count=400):
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=%s&scale=240&ma=no&datalen=%d"
           % (symbol, count))
    rows = json.loads(_get(url))
    out = {r["day"][:10]: float(r["close"]) for r in rows if r.get("close")}
    if not out:
        raise RuntimeError("sina %s empty" % symbol)
    return out


# ── 源 3：中证官网（000922 权威）──────────────────────────────
def csindex(code, days=400):
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    url = ("https://www.csindex.com.cn/csindex-home/perf/index-perf"
           "?indexCode=%s&startDate=%s&endDate=%s"
           % (code, start.strftime("%Y%m%d"), end.strftime("%Y%m%d")))
    j = json.loads(_get(url))
    if j.get("code") != "200":
        raise RuntimeError("csindex %s: %s" % (code, j.get("msg")))
    out = {}
    for r in j.get("data") or []:
        d = r["tradeDate"]
        if r.get("close"):
            out["%s-%s-%s" % (d[:4], d[4:6], d[6:])] = float(r["close"])
    if not out:
        raise RuntimeError("csindex %s empty" % code)
    return out


# ── 源 4：理杏仁（创业板 PB 十年分位）─────────────────────────
def lixinger_pb(token, code="399006", days=400):
    """返回 {date: pb_pct10y}。token 为空或失败时抛异常，由调用方降级。"""
    if not token:
        raise RuntimeError("LIXINGER_TOKEN 未配置")
    if requests is None:
        raise RuntimeError("requests 未安装，无法调用理杏仁")
    end = datetime.date.today()
    start = end - datetime.timedelta(days=days)
    body = {"token": token, "stockCodes": [code],
            "startDate": start.strftime("%Y-%m-%d"),
            "endDate": end.strftime("%Y-%m-%d"),
            "metricsList": ["cp", "pb.y10.mcw.cvpos"]}
    r = requests.post(LIXINGER_FUND_URL, json=body,
                      headers=LIXINGER_HEADERS, timeout=45)
    r.raise_for_status()
    resp = r.json()
    if resp.get("code") not in (1, 200) or not resp.get("data"):
        raise RuntimeError("理杏仁异常: %s" % resp.get("message", resp))
    out = {}
    for item in resp["data"]:
        ds = (item.get("date") or "")[:10]
        v = item.get("pb.y10.mcw.cvpos")
        if ds and v is not None:
            out[ds] = float(v)
    if not out:
        raise RuntimeError("理杏仁返回空")
    return out


# ── 汇总 ──────────────────────────────────────────────────────
def fetch_all(cfg, token="", days=400):
    """返回 (series, log)。series = {列名: {date: value}}"""
    log = []
    series = {}

    def attempt(name, fn):
        try:
            v = fn()
            log.append("OK   %s (%d)" % (name, len(v)))
            return v
        except Exception as exc:                                  # noqa: BLE001
            log.append("FAIL %s: %s %s" % (name, type(exc).__name__, str(exc)[:60]))
            return None

    plan = [("p1_px", cfg["part1"]["code"]),
            ("p2_px", cfg["part2"]["code"]),
            ("p3_px", cfg["part3"]["hold_code"])]
    for col, sym in plan:
        v = (attempt("tencent %s" % sym, lambda s=sym: tencent(s, days))
             or attempt("sina %s" % sym, lambda s=sym: sina(s, min(days, 1000))))
        if v:
            series[col] = v

    sig = cfg["part3"]["signal_code"]
    v = (attempt("csindex %s" % sig, lambda: csindex(sig, days))
         or attempt("tencent sh%s" % sig, lambda: tencent("sh" + sig, days))
         or attempt("sina sh%s" % sig, lambda: sina("sh" + sig, min(days, 1000))))
    if v:
        series["sig_px"] = v

    v = attempt("lixinger 399006 PB", lambda: lixinger_pb(token, days=days))
    if v:
        series["pb_pct"] = v

    if not all(k in series for k in REQUIRED):
        missing = [k for k in REQUIRED if k not in series]
        raise AllSourcesFailed("必需序列缺失: %s" % ",".join(missing))
    return series, log


def merge(hist, series):
    """把新抓到的并入 hist（list of dict，按 date 升序）。

    返回 (added, revised, skipped)
    skipped = 因必需列缺失而未写入的日期，用于告警。
    """
    idx = {h["date"]: h for h in hist}
    last = hist[-1]["date"] if hist else "0000-00-00"

    cands = set()
    for k in REQUIRED:
        cands |= set(series.get(k, {}))

    added, revised, skipped = [], [], []
    for d in sorted(cands):
        vals, ok = {}, True
        for k in REQUIRED:
            v = series.get(k, {}).get(d)
            if v is None:
                ok = False
                break
            vals[k] = round(float(v), 4)
        if not ok:
            if d > last:
                skipped.append(d)
            continue
        for k in OPTIONAL:
            v = series.get(k, {}).get(d)
            vals[k] = round(float(v), 6) if v is not None else None

        if d in idx:
            row = idx[d]
            changed = False
            for k in REQUIRED:
                if row.get(k) is None or abs(row[k] - vals[k]) > max(0.0005, abs(vals[k]) * 1e-4):
                    row[k] = vals[k]
                    changed = True
            for k in OPTIONAL:
                if row.get(k) is None and vals[k] is not None:
                    row[k] = vals[k]
                    changed = True
            if changed:
                revised.append(d)
        elif d > last:
            row = {"date": d}
            row.update(vals)
            hist.append(row)
            added.append(d)

    hist.sort(key=lambda x: x["date"])
    return added, revised, skipped
