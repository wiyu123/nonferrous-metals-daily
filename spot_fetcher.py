#!/usr/bin/env python3
"""
自动搜索获取小金属最新现货价格（通过 ddgs）。
锑锗钨钼由同花顺抓取，其余B组品种由此模块负责。
"""

import re, json
from datetime import datetime
from pathlib import Path

try:
    from ddgs import DDGS
    HAS_DDGS = True
except ImportError:
    HAS_DDGS = False

CACHE_PATH = Path(__file__).parent / "output" / "spot_cache.json"

# (搜索词, 单位标签, 最低合理价, 最高合理价) — 范围用于过滤幻觉/错误数据
CONFIG = {
    'ZR': ('锆英砂65%现货价格', '元/吨', 10000, 13000),
    'TI': ('海绵钛1级价格', '元/吨', 45000, 53000),
    'CO': ('电解钴99.8%现货价格', '元/吨', 350000, 450000),
    'MG': ('1号镁锭现货价格', '元/吨', 16000, 18500),
    'IN': ('精铟99.995%现货', '元/千克', 4200, 5200),
    'RE': ('氧化镨钕现货价格', '元/吨', 390000, 500000),
    'PD': ('钯金99.95%现货', '元/克', 480, 550),
    'MN': ('电解锰99.7%现货价格', '元/吨', 11500, 13500),
    'GA': ('金属镓99.99%现货', '元/千克', 2800, 3600),
    'BI': ('精铋99.99%现货价格', '元/吨', 110000, 145000),
    'TE': ('精碲99.99%现货', '元/千克', 900, 1150),
    'TA': ('钽锭99.95%现货', '元/千克', 5800, 7200),
}


def _extract(text: str, lo: float, hi: float) -> float | None:
    """从文本中提取价格，单位自适应。"""
    # 万单位
    m = re.search(r'(\d+\.?\d*)万', text)
    if m:
        v = float(m.group(1)) * 10000
        if lo * 0.7 <= v <= hi * 1.3:
            return round(v)
    # 纯数字+元 (5-7位)
    for n in re.findall(r'(\d{3,7})(?=元[/每])', text):
        v = float(n)
        if lo * 0.7 <= v <= hi * 1.3:
            return round(v)
    # 逗号分隔 426,409
    for n in re.findall(r'(\d{1,3}(?:,\d{3})+)(?=元[/每])', text):
        v = float(n.replace(',', ''))
        if lo * 0.7 <= v <= hi * 1.3:
            return round(v)
    return None


def fetch_today() -> dict:
    """搜索获取今天各品种价格。返回 {symbol: price}。"""
    if not HAS_DDGS:
        return {}
    today = datetime.now().strftime('%Y年%m月%d日')
    results = {}
    ddgs = DDGS()
    for sym, (kw, _, lo, hi) in CONFIG.items():
        try:
            items = list(ddgs.text(f'{today} {kw}', max_results=3))
            if not items:
                items = list(ddgs.text(kw, max_results=3))
            price = None
            for item in items:
                combined = item.get('title', '') + ' ' + item.get('body', '')
                price = _extract(combined, lo, hi)
                if price:
                    break
            results[sym] = price
            print(f'  {sym}: {"OK " + str(price) if price else "NO DATA"}')
        except Exception as e:
            print(f'  {sym}: ERROR {e}')
    return results


def update():
    """更新缓存：移除自延展数据，写入今天真实价格。"""
    if not CACHE_PATH.exists():
        return
    today = datetime.now().strftime('%Y-%m-%d')
    prices = fetch_today()
    if not prices:
        return
    with open(CACHE_PATH, 'r', encoding='utf-8') as f:
        cache = json.load(f)
    updated = 0
    for sym, price in prices.items():
        if not price or sym not in cache:
            continue
        # Remove any existing today entry (auto-extended stale data)
        cache[sym] = [d for d in cache[sym] if d['date'] != today]
        cache[sym].append({'date': today, 'close': price, 'pct_chg': 0})
        cache[sym].sort(key=lambda x: x['date'])
        updated += 1
    if updated:
        with open(CACHE_PATH, 'w', encoding='utf-8') as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
        print(f'Updated {updated}/{len(prices)} metals in cache')


if __name__ == '__main__':
    update()
