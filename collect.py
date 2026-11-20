#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BTC 五层决策看板 · 云端采集脚本（由 GitHub Actions 定时运行）

使用方法:
  双击本文件 (Windows/Mac 需已安装 Python 3.8+), 或在终端运行:  python BTC看板_一键启动.py
  首次运行会自动安装依赖 (requests / pandas / lxml), 然后自动打开浏览器。
  退出: 在运行窗口按 Ctrl+C 。

数据: 全部来自免费公开源 (Farside ETF流向 / FRED宏观 / DefiLlama稳定币 /
      Coinbase溢价 / Binance合约与现货 / 恐惧贪婪 / mempool算力), 无需 API Key, 无需手填。
      数据快照自动保存在本文件同目录的 snapshot.json 中。

仅供研究与数据观察, 不构成投资建议。加密资产波动极大, 杠杆可能造成超出本金的损失。
"""
import io
import json
import subprocess
import sys
import threading
import time
import webbrowser
import datetime as dt
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path


def _ensure_deps():
    missing = []
    for m in ("requests", "pandas", "lxml"):
        try:
            __import__(m)
        except ImportError:
            missing.append(m)
    if missing:
        print("首次运行: 正在安装依赖", missing, "……")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", *missing])


_ensure_deps()

import pandas as pd  # noqa: E402
import requests  # noqa: E402

ROOT = Path(__file__).resolve().parent
SNAP = ROOT / "snapshot.json"
REFRESH_S = 900          # 数据后台刷新间隔（秒）
PORT = 8000
UA = {"User-Agent": "Mozilla/5.0 (btc-dashboard/4.0)"}
LOCK = threading.Lock()



def now_iso():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def get(url, params=None, text=False):
    r = requests.get(url, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    return r.text if text else r.json()


def day_of_ms(ms):
    return dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def num(x):
    """解析 Farside 单元格: '(12.3)' 为负数, '-' 或空为缺失。"""
    s = str(x).strip().replace(",", "")
    if s in ("", "-", "nan", "None"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


# ---------------- 免费数据源 ----------------

def fetch_etf():
    html = get("https://farside.co.uk/btc/", text=True)
    tables = pd.read_html(io.StringIO(html))
    df = max(tables, key=len)
    df.columns = [str(c[-1]) if isinstance(c, tuple) else str(c) for c in df.columns]
    dates = pd.to_datetime(df.iloc[:, 0].astype(str), format="%d %b %Y", errors="coerce")
    total_cols = [c for c in df.columns if c.strip().lower() == "total"]
    if not total_cols:
        raise ValueError("Farside 表格中未找到 Total 列 (页面结构可能已变)")
    vals = df[total_cols[-1]].map(num)
    out = {}
    for d, v in zip(dates, vals):
        if pd.notna(d) and v is not None:
            out[d.strftime("%Y-%m-%d")] = v
    if not out:
        raise ValueError("未解析到任何日期行")
    return out


def fetch_stable():
    rows = get("https://stablecoins.llama.fi/stablecoincharts/all")
    return {day_of_ms(int(r["date"]) * 1000): float(r["totalCirculatingUSD"]["peggedUSD"])
            for r in rows}


def fred(series):
    txt = get("https://fred.stlouisfed.org/graph/fredgraph.csv", {"id": series}, text=True)
    d = pd.read_csv(io.StringIO(txt), na_values=["."])
    d.columns = ["date", "v"]
    d = d.dropna()
    return {r.date: float(r.v) for r in d.itertuples()}


def fetch_macro():
    y10 = fred("DGS10")
    walcl = fred("WALCL")      # 百万美元 (周频)
    tga = fred("WTREGEN")      # 百万美元 (周频)
    rrp = fred("RRPONTSYD")    # 十亿美元 (日频)
    liq = {}                   # 净流动性, 十亿美元
    for d in walcl:
        if d in tga and d in rrp:
            liq[d] = walcl[d] / 1000 - tga[d] / 1000 - rrp[d]
    ds = sorted(liq)
    if len(ds) < 6:
        raise ValueError("净流动性数据不足")
    last_d = ds[-1]
    target = (dt.date.fromisoformat(last_d) - dt.timedelta(days=28)).isoformat()
    prev_d = [d for d in ds if d <= target][-1]
    chg = liq[last_d] / liq[prev_d] - 1
    ys = sorted(y10)
    return {
        "net_liq_bn": round(liq[last_d], 1),
        "net_liq_date": last_d,
        "net_liq_chg_4w": round(chg, 4),
        "liq_state": 1 if chg > 0.01 else -1 if chg < -0.01 else 0,  # 启发式
        "dgs10": y10[ys[-1]],
        "dgs10_date": ys[-1],
    }


def fetch_premium():
    cb = float(get("https://api.exchange.coinbase.com/products/BTC-USD/ticker")["price"])
    bn = float(get("https://api.binance.com/api/v3/ticker/price", {"symbol": "BTCUSDT"})["price"])
    return {"coinbase": cb, "binance_usdt": bn, "premium": round(cb / bn - 1, 5)}


def fetch_derivs():
    base = "https://fapi.binance.com/futures/data/"
    p = {"symbol": "BTCUSDT", "period": "1d", "limit": 500}

    def series(ep, field):
        return {day_of_ms(r["timestamp"]): float(r[field]) for r in get(base + ep, p)}

    out = {
        "global_ls_account": series("globalLongShortAccountRatio", "longShortRatio"),
        "top_ls_position": series("topLongShortPositionRatio", "longShortRatio"),
        "taker_buy_sell": series("takerlongshortRatio", "buySellRatio"),
        "oi_usd": series("openInterestHist", "sumOpenInterestValue"),
    }
    fund = {}
    for r in get("https://fapi.binance.com/fapi/v1/fundingRate",
                 {"symbol": "BTCUSDT", "limit": 1000}):
        fund.setdefault(day_of_ms(r["fundingTime"]), []).append(float(r["fundingRate"]))
    out["funding_daily_avg"] = {k: sum(v) / len(v) for k, v in fund.items()}
    return out


def fetch_trend():
    rows = get("https://api.binance.com/api/v3/klines",
               {"symbol": "BTCUSDT", "interval": "1d", "limit": 500})
    closes = pd.Series([float(r[4]) for r in rows[:-1]])  # 去掉未收盘的当日K线
    last = closes.iloc[-1]
    s200 = closes.rolling(200).mean().iloc[-1]
    s50 = closes.rolling(50).mean().iloc[-1]
    return {
        "close": round(float(last), 2),
        "sma50": round(float(s50), 2),
        "sma200": round(float(s200), 2),
        "above200": bool(last > s200),
        "golden": bool(s50 > s200),
        "dist200": round(float(last / s200 - 1), 4),
    }


FETCHERS = {
    "etf": fetch_etf,
    "stable": fetch_stable,
    "macro": fetch_macro,
    "premium": fetch_premium,
    "derivs": fetch_derivs,
    "trend": fetch_trend,
}


# ---------------- 快照与后台刷新 ----------------

def read_snap():
    try:
        return json.loads(SNAP.read_text(encoding="utf-8"))
    except Exception:
        return {}


def collect():
    prev = read_snap()
    out = {"updated": now_iso(), "sources": {}}
    for k, fn in FETCHERS.items():
        try:
            out[k] = fn()
            out["sources"][k] = {"ok": True, "at": now_iso()}
        except Exception as e:
            old = prev.get(k)
            out[k] = old if old else None
            out["sources"][k] = {"ok": False, "at": now_iso(), "error": str(e)[:160],
                                 "fallback": "上次成功值" if old else "无可用数据"}
    with LOCK:
        SNAP.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def background():
    while True:
        try:
            collect()
            print(time.strftime("%H:%M:%S"), "数据已更新")
        except Exception as e:
            print("数据更新失败:", e)
        time.sleep(REFRESH_S)


if __name__ == "__main__":
    out = collect()
    print("updated", out["updated"], {k: v["ok"] for k, v in out["sources"].items()})
