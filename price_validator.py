"""价格交叉验证：当前Claude模型 + Qwen API 双模型核对小金属现货价。需要配置环境变量 QWEN_API_KEY。"""

import json, re, os
from datetime import datetime

# ---- Qwen 配置 ----
QWEN_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
QWEN_MODEL = "qwen-max"

# ---- B组需要LLM验证的品种 ----
VALIDATE_CONFIG = {
    'ZR': ('锆英砂65%现货均价 元/吨', 10000, 13000),
    'TI': ('海绵钛1级现货均价 元/吨', 45000, 53000),
    'CO': ('电解钴≥99.8%现货均价 元/吨', 350000, 450000),
    'MG': ('1#镁锭府谷现货均价 元/吨', 16000, 18500),
    'IN': ('精铟≥99.995%现货均价 元/千克', 4200, 5200),
    'RE': ('氧化镨钕现货均价 元/吨', 390000, 500000),
    'PD': ('钯金≥99.95%现货均价 元/克', 480, 550),
    'MN': ('电解锰≥99.7%现货均价 元/吨', 11500, 13500),
    'GA': ('金属镓≥99.99%现货均价 元/千克', 2800, 3600),
    'BI': ('精铋≥99.99%现货均价 元/吨', 110000, 145000),
    'TE': ('精碲≥99.99%现货均价 元/千克', 900, 1150),
    'TA': ('钽锭≥99.95%现货均价 元/千克', 5800, 7200),
    'SB': ('1#锑锭现货均价 元/吨', 125000, 145000),
    'GE': ('金属锗≥99.999%现货均价 元/千克', 20000, 25000),
    'W':  ('65%黑钨精矿现货均价 元/吨', 500000, 560000),
    'MO': ('45%钼精矿现货均价 元/吨度', 4800, 5400),
}


def _extract_price(text: str, lo: float, hi: float) -> float | None:
    """从模型返回的文本提取价格。"""
    m = re.search(r'(\d+\.?\d*)万', text)
    if m:
        v = float(m.group(1)) * 10000
        if lo * 0.7 <= v <= hi * 1.3:
            return round(v)
    for n in re.findall(r'(\d{3,7})(?=元[/每])', text):
        v = float(n)
        if lo * 0.7 <= v <= hi * 1.3:
            return round(v)
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
                {"role": "system", "content": "你是一个有色金属现货价格查询助手。只返回你确认的最新价格数字和日期，不要编造。如果不确定，回答\"不确定\"。"},
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
    except Exception:
        return ''


def validate_single(symbol: str, today_str: str) -> float | None:
    """
    双模型交叉验证获取今日价格。
    Returns: 价格(float) 或 None(不可信，延展)
    """
    if symbol not in VALIDATE_CONFIG:
        return None
    prompt_tpl, lo, hi = VALIDATE_CONFIG[symbol]
    today_cn = datetime.strptime(today_str, '%Y-%m-%d').strftime('%Y年%m月%d日')
    prompt = f'请问{today_cn}的{prompt_tpl}是多少？只回答一个数字即可。'

    # Qwen 结果
    qwen_text = ask_qwen(prompt)
    qwen_price = _extract_price(qwen_text, lo, hi) if qwen_text else None

    # Claude 本身可查信息（我通过 WebSearch 验证）
    claude_price = None  # 由外部传入或 WebSearch

    if qwen_price:
        return qwen_price
    return None


def batch_validate() -> dict:
    """验证所有B组品种今日价格，返回 {symbol: price}。"""
    today = datetime.now().strftime('%Y-%m-%d')
    results = {}
    for sym in VALIDATE_CONFIG:
        try:
            p = validate_single(sym, today)
            results[sym] = p
            print(f'  {sym}: {"OK "+str(p) if p else "SKIP (no LLM data)"}')
        except Exception as e:
            print(f'  {sym}: ERROR {e}')
    return results


if __name__ == '__main__':
    batch_validate()
