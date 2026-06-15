#!/usr/bin/env python3
"""
小金属现货价格自动抓取。
锑锗钨钼：同花顺抓取（主流程已含）
其余 12 种：Qwen API 查询 + 范围验证 + DDGS 补漏
"""

import json, os, sys
from datetime import datetime
from pathlib import Path

CACHE_PATH = Path(__file__).parent / "output" / "spot_cache.json"

# ---- Qwen ----
QWEN_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
QWEN_MODEL = "qwen-max"

# 其余12种需要LLM验证的品种：(搜索描述, 最低合理价, 最高合理价)
LLM_CONFIG = {
    # (搜索描述, 合理最低, 合理最高) — 描述必须带完整规格防止大模型返回不同品位的价格
    'ZR': ('锆英砂ZrO2≥65%现货均价 元/吨 注：不是氧氯化锆', 10000, 13000),
    'TI': ('海绵钛0级或1级现货均价 元/吨 注：不是钛白粉价格', 45000, 53000),
    'CO': ('电解钴Co≥99.8%现货均价 元/吨 注：不是钴粉或硫酸钴', 350000, 450000),
    'MG': ('1#镁锭Mg≥99.9%府谷出厂均价 元/吨 注：不是镁合金或镁粉', 16000, 18500),
    'IN': ('精铟In≥99.995%现货均价 元/千克 注：不是粗铟', 4200, 5200),
    'RE': ('氧化镨钕PrNd≥99%现货均价 元/吨 注：不是金属镨钕或氧化镝', 390000, 500000),
    'PD': ('钯金Pd≥99.95%现货均价 元/克 注：不是钯碳催化剂回收价', 480, 550),
    'MN': ('电解锰片DJMn99.7%现货均价 元/吨 注：不是电解二氧化锰', 11500, 13500),
    'GA': ('金属镓Ga≥99.99%现货均价 元/千克 注：不是氧化镓或砷化镓', 2800, 3600),
    'BI': ('精铋Bi≥99.99%现货均价 元/吨 注：不是氧化铋或次铋', 110000, 145000),
    'TE': ('精碲Te≥99.99%现货均价 元/千克 注：不是二氧化碲', 900, 1150),
    'TA': ('钽锭Ta≥99.95%现货均价 元/千克 注：不是钽粉或氧化钽', 5800, 7200),
}


def _extract_price(text: str, lo: float, hi: float) -> float | None:
    """从文本提取价格数字。支持 万/元/逗号 格式。"""
    import re
    # 万元单位
    m = re.search(r'(\d+\.?\d*)万', text)
    if m:
        v = float(m.group(1)) * 10000
        if lo * 0.7 <= v <= hi * 1.3:
            return round(v)
    # 纯数字+元
    for n in re.findall(r'(\d{3,7})(?=元[/每])', text):
        v = float(n)
        if lo * 0.7 <= v <= hi * 1.3:
            return round(v)
    # 逗号分隔数字+元
    for n in re.findall(r'(\d{1,3}(?:,\d{3})+)(?=元[/每])', text):
        v = float(n.replace(',', ''))
        if lo * 0.7 <= v <= hi * 1.3:
            return round(v)
    return None


def ask_qwen(prompt: str) -> str:
    """调用 Qwen API 获取回答。"""
    key = os.getenv('QWEN_API_KEY', '')
    if not key:
        return ''
    try:
        import urllib.request
        data = json.dumps({
            "model": QWEN_MODEL,
            "messages": [
                {"role": "system",
                 "content": "你是专业的有色金属现货价格数据库。用户问某品种某日价格，你如实回复一个最新数字+单位，不确定就说\"不确定\"，严禁编造。"},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.1,
            "max_tokens": 200
        }).encode('utf-8')
        req = urllib.request.Request(QWEN_API_URL, data=data, headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = json.loads(resp.read().decode('utf-8'))
            return body['choices'][0]['message']['content']
    except Exception as e:
        print(f'    Qwen API error: {e}')
        return ''


def fetch_today() -> dict:
    """用 Qwen 查询12个品种的今日价格。返回 {symbol: price}。"""
    key = os.getenv('QWEN_API_KEY', '')
    if not key:
        print('QWEN_API_KEY not set, skipping LLM validation')
        return {}

    # 构建批量查询prompt
    lines = []
    for sym, (desc, lo, hi) in LLM_CONFIG.items():
        lines.append(f'{desc}')
    batch_prompt = (
        '你是专业的有色金属现货报价员。请如实回答以下品种今天的最新现货均价（元/吨或元/千克按品种标注），每个一行，格式：品种=价格。\n'
        '严格注意：只回答该规格的价格，不要把其他品位/形态的价格混淆。不确定的写"未知"，严禁编造。\n'
        + '\n'.join(lines)
    )

    print(f'  Asking Qwen for {len(LLM_CONFIG)} prices...')
    text = ask_qwen(batch_prompt)
    if not text:
        return {}

    results = {}
    for sym, (desc, lo, hi) in LLM_CONFIG.items():
        # 在回复中匹配每行
        for line in text.split('\n'):
            if desc[:6] in line or desc.split('现货')[0] in line:
                p = _extract_price(line, lo, hi)
                if p:
                    results[sym] = p
                    print(f'  {sym}: {p}')
                    break
        if sym not in results:
            print(f'  {sym}: no valid price in Qwen reply')

    return results


def update():
    """更新缓存。"""
    if not CACHE_PATH.exists():
        print('Cache file not found')
        return

    today = datetime.now().strftime('%Y-%m-%d')
    prices = fetch_today()
    if not prices:
        print('No prices obtained from Qwen')
        return

    with open(CACHE_PATH, 'r', encoding='utf-8') as f:
        cache = json.load(f)

    updated = 0
    skipped = 0
    for sym, price in prices.items():
        if price is None or sym not in cache:
            continue

        # 与前日价格对比，日涨跌幅超±15%视为不同规格混入，拒绝写入
        cache[sym].sort(key=lambda x: x['date'])
        if len(cache[sym]) > 0:
            prev = cache[sym][-1]['close']
            if prev > 0:
                day_chg = abs(price - prev) / prev
                if day_chg > 0.15:
                    print(f'  REJECT {sym}: day change {day_chg*100:.1f}% exceeds 15% (prev={prev}, got={price})')
                    skipped += 1
                    continue

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
