#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
有色金属价格监控系统
===================
功能：从新浪财经获取有色金属期货价格，生成10日/30日走势折线图，邮件发送汇总报告。

使用方法：
    python metal_monitor.py              # 正常模式：获取数据、生成图表、发送邮件
    python metal_monitor.py --dry-run    # 仅生成图表，不发送邮件
    python metal_monitor.py --no-email   # 获取数据并生成图表，不发送邮件
    python metal_monitor.py --config path/to/config.yaml  # 指定配置文件

数据来源：
    新浪财经期货K线API（免费、无需注册）
    - 上期所期货主连：铜(CU0)、铝(AL0)、锌(ZN0)、铅(PB0)、镍(NI0)、锡(SN0)、
      氧化铝(AO0)、黄金(AU0)、白银(AG0)
    - 备用：东方财富行情API

定时运行建议（Windows）：
    任务计划程序 -> 创建基本任务 -> 每天15:30运行 ->
    启动程序：python D:/MYCODE/metal_monitor/metal_monitor.py

定时运行建议（Linux/Mac）：
    crontab -e
    30 15 * * 1-5 cd /path/to/metal_monitor && python metal_monitor.py >> output/cron.log 2>&1
"""

import argparse
import logging
import os
import re
import sys
import smtplib
import json
import csv
import io
import base64
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
import yaml
import matplotlib
matplotlib.use('Agg')  # 非交互式后端，无需GUI
import matplotlib.pyplot as plt
from matplotlib.font_manager import FontProperties

# ============================================================
# 日志配置
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(
            os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'output', 'monitor.log'),
            encoding='utf-8'
        )
    ]
)
logger = logging.getLogger(__name__)

# ============================================================
# 常量配置
# ============================================================
BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = BASE_DIR / 'output'
DEFAULT_CONFIG = BASE_DIR / 'config.yaml'

# ---- 新浪财经API ----
SINA_KLINE_URL = (
    'https://stock2.finance.sina.com.cn/futures/api/jsonp.php'
    '/var%20_{symbol}=/InnerFuturesNewService.getDailyKLine'
)
SINA_REALTIME_URL = 'http://hq.sinajs.cn/list={symbol}'

# ---- 东方财富API（备用） ----
EASTMONEY_KLINE_URL = 'https://push2his.eastmoney.com/api/qt/stock/kline/get'

# ---- HTTP会话（禁用代理，直连） ----
def _create_session():
    session = requests.Session()
    session.trust_env = False
    return session

SESSION = _create_session()

HEADERS_SINA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://finance.sina.com.cn/',
}

HEADERS_EASTMONEY = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://quote.eastmoney.com/',
}


# ============================================================
# 中文字体检测与配置
# ============================================================
def setup_chinese_font() -> FontProperties:
    """检测并配置中文字体，返回可用的中文字体对象。"""
    font_candidates = [
        'SimHei',              # Windows 黑体
        'Microsoft YaHei',     # Windows 微软雅黑
        'PingFang SC',         # Mac
        'Heiti SC',            # Mac
        'Noto Sans CJK SC',    # Linux
        'WenQuanYi Micro Hei', # Linux
        'WenQuanYi Zen Hei',   # Linux
        'AR PL UMing CN',      # Linux
        'sans-serif',
    ]

    available = [f.name for f in matplotlib.font_manager.fontManager.ttflist]

    for font_name in font_candidates:
        if font_name in available:
            logger.info(f'使用中文字体: {font_name}')
            font = FontProperties(family=font_name)
            plt.rcParams['font.family'] = font_name
            plt.rcParams['axes.unicode_minus'] = False
            return font

    logger.warning(
        '未找到中文字体，图表中文可能显示为方框。'
        'Windows 用户请安装 SimHei 字体，'
        'Linux 用户请安装 fonts-wqy-zenhei'
    )
    plt.rcParams['font.family'] = 'sans-serif'
    plt.rcParams['axes.unicode_minus'] = False
    return FontProperties()


# ============================================================
# 配置加载
# ============================================================
def load_config(config_path: Path) -> dict:
    """加载并验证YAML配置文件。"""
    if not config_path.exists():
        logger.error(f'配置文件不存在: {config_path}')
        sys.exit(1)

    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    if 'metals' not in config or not config['metals']:
        logger.error('配置文件中未定义 metals 列表')
        sys.exit(1)

    if 'email' not in config:
        logger.warning('配置文件中未定义 email 设置，将跳过邮件发送')
        config['email'] = {}

    # 设置默认值
    config.setdefault('chart', {})
    config['chart'].setdefault('short_period', 10)
    config['chart'].setdefault('long_period', 30)
    config['chart'].setdefault('figure_width', 14)
    config['chart'].setdefault('figure_height', 6)
    config['chart'].setdefault('dpi', 150)

    logger.info(f'加载配置成功: {len(config["metals"])} 个金属品种')
    for m in config['metals']:
        logger.info(f'  - {m["name"]} ({m["symbol"]})')

    return config


# ============================================================
# 数据获取模块
# ============================================================
def fetch_sina_kline(symbol: str, limit: int = 60) -> Optional[List[dict]]:
    """
    从新浪财经获取期货日K线数据（主力连续合约）。

    Args:
        symbol: 新浪期货代码，如 'CU0' (铜主连), 'AL0' (铝主连)
        limit: 需要的最近数据条数

    Returns:
        [{date, open, close, high, low, volume, position, settlement,
          pct_chg}, ...] 按日期升序排列，或 None
    """
    url = (
        f'https://stock2.finance.sina.com.cn/futures/api/jsonp.php'
        f'/var%20_{symbol}=/InnerFuturesNewService.getDailyKLine'
    )
    params = {'symbol': symbol}

    try:
        resp = SESSION.get(url, params=params, headers=HEADERS_SINA, timeout=15)
        resp.raise_for_status()

        # 解析JSONP: var _CU0=([{...},{...}]);
        text = resp.text
        match = re.search(r'\(\s*(\[.*\])\s*\)', text, re.DOTALL)
        if not match:
            logger.warning(f'新浪API JSONP解析失败 (symbol={symbol})')
            return None

        raw_data = json.loads(match.group(1))
        if not raw_data:
            logger.warning(f'新浪API返回空数据 (symbol={symbol})')
            return None

        result = []
        for item in raw_data:
            close_price = float(item['c'])
            open_price = float(item['o'])
            # 计算当日涨跌幅（相较于前一日收盘）
            if result:
                prev_close = result[-1]['close']
                pct_chg = ((close_price - prev_close) / prev_close * 100
                          if prev_close != 0 else 0)
            else:
                pct_chg = 0

            result.append({
                'date': item['d'],               # 日期 YYYY-MM-DD
                'open': open_price,              # 开盘价
                'close': close_price,            # 收盘价
                'high': float(item['h']),        # 最高价
                'low': float(item['l']),         # 最低价
                'volume': float(item['v']),      # 成交量
                'position': float(item.get('p', 0)),  # 持仓量
                'settlement': float(item.get('s', 0)), # 结算价
                'pct_chg': round(pct_chg, 2),    # 涨跌幅%
            })

        # 返回最近 limit 条
        return result[-limit:] if len(result) > limit else result

    except requests.Timeout:
        logger.error(f'新浪API请求超时 (symbol={symbol})')
        return None
    except requests.RequestException as e:
        logger.error(f'新浪API网络请求失败 (symbol={symbol}): {e}')
        return None
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        logger.error(f'新浪API数据解析失败 (symbol={symbol}): {e}')
        return None


def fetch_sina_stock_kline(stock_code: str, limit: int = 60) -> Optional[List[dict]]:
    """
    从新浪财经获取A股日K线数据（用于无期货的小金属，以龙头股为价格参考）。

    Args:
        stock_code: 新浪股票代码，如 'sh600549' (厦门钨业), 'sz002428' (云南锗业)
        limit: 需要的最近数据条数

    Returns:
        标准化K线数据列表 [{date, open, close, high, low, volume, pct_chg}, ...]
        按日期升序排列，或 None
    """
    url = (
        'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php'
        '/CN_MarketData.getKLineData'
    )
    params = {
        'symbol': stock_code,
        'scale': '240',      # 日K线
        'ma': 'no',
        'datalen': limit + 15,  # 多取一些
    }

    try:
        resp = SESSION.get(url, params=params, headers=HEADERS_SINA, timeout=15)
        resp.raise_for_status()

        raw_data = json.loads(resp.text)
        if not raw_data:
            logger.warning(f'新浪股票API返回空数据 (code={stock_code})')
            return None

        result = []
        for item in raw_data:
            close_price = float(item['close'])
            open_price = float(item['open'])
            if result:
                prev_close = result[-1]['close']
                pct_chg = ((close_price - prev_close) / prev_close * 100
                          if prev_close != 0 else 0)
            else:
                pct_chg = 0

            result.append({
                'date': item['day'],           # 日期 YYYY-MM-DD
                'open': open_price,            # 开盘价
                'close': close_price,          # 收盘价
                'high': float(item['high']),   # 最高价
                'low': float(item['low']),     # 最低价
                'volume': float(item['volume']),  # 成交量
                'pct_chg': round(pct_chg, 2),  # 涨跌幅%
            })

        return result[-limit:] if len(result) > limit else result

    except requests.Timeout:
        logger.error(f'新浪股票API请求超时 (code={stock_code})')
        return None
    except requests.RequestException as e:
        logger.error(f'新浪股票API网络请求失败 (code={stock_code}): {e}')
        return None
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        logger.error(f'新浪股票API数据解析失败 (code={stock_code}): {e}')
        return None


# ---- 同花顺现货数据抓取 + 本地历史缓存 ----
SPOT_CACHE_FILE = OUTPUT_DIR / 'spot_cache.json'

TONGHUASHUN_KEYWORD_MAP = {
    'SB': ['锑', '1#锑', '锑锭'],
    'GE': ['锗', '锗锭', '金属锗'],
    'W':  ['黑钨精矿', '65%黑钨精矿', '钨精矿', '黑钨'],
    'MO': ['钼精矿', '45%钼精矿', '钼'],
    'TA': ['钽', '钽锭'],
    'ZR': ['锆', '锆英砂', '氧氯化锆'],
    'TI': ['钛', '海绵钛', '钛白粉'],
    'CO': ['钴', '电解钴', '硫酸钴'],
    'MG': ['镁', '镁锭', '金属镁'],
    'IN': ['铟', '精铟', '铟锭'],
}


# 今日已知文章URL（程序自动发现失败时的种子URL）
# 每天运行后缓存累积，6-7天即可覆盖30日历史
# 同花顺种子URL已移除，改为缓存自动延展机制

TONGHUASHUN_ROOT = 'https://goodsfu.10jqka.com.cn/'


# 今日已知文章URL（程序自动发现失败时的种子URL）
# 每天运行后缓存累积，6-7天即可覆盖30日历史
# 同花顺种子URL已移除，改为缓存自动延展机制


def _fetch_tonghuashun_article_list() -> dict:
    """获取同花顺首页当天所有现货报价文章URL，按关键词匹配。返回 {symbol: url}。"""
    try:
        resp = SESSION.get(TONGHUASHUN_ROOT, headers=HEADERS_EASTMONEY, timeout=15)
        resp.raise_for_status()
        resp.encoding = 'gbk'  # root=GBK, articles=UTF-8
        html = resp.text
    except Exception as e:
        logger.debug(f'同花顺首页请求失败: {e}')
        return {}

    # 匹配文章链接和标题
    # 页面结构: <a href="http://goodsfu.10jqka.com.cn/YYYYMMDD/cXXXXX.shtml">标题文本</a>
    pattern = re.compile(
        r'<a\s[^>]*href="(http://goodsfu\.10jqka\.com\.cn/\d{8}/c\d+\.shtml)"[^>]*>'
        r'(.*?)</a>',
        re.DOTALL
    )
    matches = pattern.findall(html)

    result = {}
    for url, raw_title in matches:
        title = re.sub(r'<[^>]+>', '', raw_title).strip()
        for symbol, keywords in TONGHUASHUN_KEYWORD_MAP.items():
            if symbol in result:
                continue  # 已匹配
            for kw in keywords:
                if kw in title:
                    result[symbol] = url
                    logger.debug(f'  同花顺匹配 [{symbol}]: {title[:60]}')
                    break

    return result


def _parse_tonghuashun_article(url: str, unit: str = '元/吨') -> list:
    """
    解析同花顺现货日报页面，提取价格表及meta中的历史涨跌信息。
    页面表格只有4-5天数据，meta描述含近一周/近一月累计涨跌，
    可反推历史价格实现近似补全。

    Returns: [{date, close, pct_chg}, ...] 按日期升序
    """
    try:
        resp = SESSION.get(url, headers=HEADERS_EASTMONEY, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.debug(f'同花顺文章请求失败 {url}: {e}')
        return []

    html = resp.text

    # 从页面标题提取单价信息判断是否万元
    title_match = re.search(r'<title>(.*?)</title>', html)
    title_text = title_match.group(1) if title_match else ''
    is_wan = '万' in title_text and '/吨' in title_text

    # === 提取表格（最近4-5天） ===
    table_match = re.search(r'<table[^>]*>(.*?)</table>', html, re.DOTALL)
    if not table_match:
        return []

    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_match.group(1), re.DOTALL)
    results = []
    year = datetime.now().year

    for row in rows:
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL)
        clean_cells = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]

        if len(clean_cells) < 2:
            continue
        if '日期' in clean_cells[0] or '价格' in clean_cells[0]:
            continue

        date_str = clean_cells[0]
        date_match = re.match(r'(\d{1,2})月(\d{1,2})日', date_str)
        if not date_match:
            continue

        month, day = int(date_match.group(1)), int(date_match.group(2))
        date_iso = f'{year}-{month:02d}-{day:02d}'

        price_str = clean_cells[1].replace(',', '').replace(' ', '')
        try:
            price = float(price_str)
        except ValueError:
            continue

        if is_wan:
            price *= 10000
        elif '万' in unit:
            price *= 10000

        pct_chg = 0.0
        if len(clean_cells) >= 4:
            chg_str = clean_cells[3].replace('%', '').replace('+', '')
            try:
                pct_chg = float(chg_str)
            except ValueError:
                pct_chg = 0.0

        results.append({
            'date': date_iso,
            'close': price,
            'pct_chg': pct_chg,
        })

    results.sort(key=lambda x: x['date'])
    if not results:
        return []

    # === 从meta description提取历史涨跌信息，反推早期价格 ===
    meta_match = re.search(
        r'<meta[^>]*name="description"[^>]*content="([^"]+)"',
        html
    )
    latest_price = results[-1]['close']
    latest_date = datetime.strptime(results[-1]['date'], '%Y-%m-%d')

    if meta_match:
        desc = meta_match.group(1)

        # 近一周累计涨跌 — 匹配金额
        week_chg_match = re.search(
            r'近一[周週].*?累计.*?(\d+\.?\d*)\s*(万)?\s*(?:元|美)',
            desc
        )
        if week_chg_match:
            chg_val = float(week_chg_match.group(1))
            if week_chg_match.group(2):  # 万元
                chg_val *= 10000
            if '跌' in week_chg_match.group(0):
                chg_val = -chg_val
            week_ago_date = (latest_date - timedelta(days=7)).strftime('%Y-%m-%d')
            week_ago_price = round(latest_price - chg_val, 2)
            existing_dates = {r['date'] for r in results}
            if week_ago_date not in existing_dates and week_ago_price > 0:
                results.append({
                    'date': week_ago_date,
                    'close': week_ago_price,
                    'pct_chg': 0,
                })
                logger.debug(f'  补齐7天前价格: {week_ago_date} = {week_ago_price}')

        # 近一月累计涨跌
        month_chg_match = re.search(
            r'近一[个個]?月.*?累计.*?(\d+\.?\d*)\s*(万)?\s*(?:元|美)',
            desc
        )
        if month_chg_match:
            chg_val = float(month_chg_match.group(1))
            if month_chg_match.group(2):  # 万元
                chg_val *= 10000
            if '跌' in month_chg_match.group(0):
                chg_val = -chg_val
            month_ago_date = (latest_date - timedelta(days=30)).strftime('%Y-%m-%d')
            month_ago_price = round(latest_price - chg_val, 2)
            existing_dates = {r['date'] for r in results}
            if month_ago_date not in existing_dates and month_ago_price > 0:
                results.append({
                    'date': month_ago_date,
                    'close': month_ago_price,
                    'pct_chg': 0,
                })
                logger.debug(f'  补齐30天前价格: {month_ago_date} = {month_ago_price}')

    # === 补充15天前参考点（线性插值） ===
    date_set = {r['date'] for r in results}
    if len(date_set) < 15:
        week_key = (latest_date - timedelta(days=7)).strftime('%Y-%m-%d')
        month_key = (latest_date - timedelta(days=30)).strftime('%Y-%m-%d')
        mid_key = (latest_date - timedelta(days=15)).strftime('%Y-%m-%d')

        w_price = next((r['close'] for r in results if r['date'] == week_key), None)
        m_price = next((r['close'] for r in results if r['date'] == month_key), None)
        if w_price and m_price and mid_key not in date_set:
            mid_price = round(m_price + (w_price - m_price) * (15/23), 2)
            results.append({
                'date': mid_key,
                'close': mid_price,
                'pct_chg': 0,
            })

    # === 补充每日数据（线性插值填充连续交易日之间的gap） ===
    results.sort(key=lambda x: x['date'])
    final_results = []
    for i, r in enumerate(results):
        final_results.append(r)
        if i < len(results) - 1:
            curr_date = datetime.strptime(r['date'], '%Y-%m-%d')
            next_date = datetime.strptime(results[i + 1]['date'], '%Y-%m-%d')
            gap = (next_date - curr_date).days
            # 如果两参考点间隔>3天，插入1-2个中间点
            if gap > 3 and gap <= 20:
                n_insert = min(2, gap // 3)
                for j in range(1, n_insert + 1):
                    interp_date = curr_date + timedelta(days=round(gap * j / (n_insert + 1)))
                    interp_date_str = interp_date.strftime('%Y-%m-%d')
                    if interp_date_str not in date_set:
                        # 线性插值价格
                        interp_price = round(
                            r['close'] +
                            (results[i + 1]['close'] - r['close']) * j / (n_insert + 1),
                            2
                        )
                        if interp_price > 0:
                            final_results.append({
                                'date': interp_date_str,
                                'close': interp_price,
                                'pct_chg': 0,
                            })

    final_results.sort(key=lambda x: x['date'])
    logger.info(f'  同花顺解析: 表格{len([r for r in results if r.get("pct_chg") != 0])}天 + 补齐{len(final_results) - len(list(set(r["date"] for r in results if r.get("pct_chg") != 0)))}天 = 共{len(final_results)}天')
    return final_results



def _auto_extend_spot_cache():
    """自动将缓存中所有品种的最新数据延续到今天（若缺失）。"""
    cache = load_spot_cache()
    if not cache:
        return
    today_str = datetime.now().strftime('%Y-%m-%d')
    modified = False
    for sym, data in cache.items():
        if not data:
            continue
        data.sort(key=lambda x: x['date'])
        last_date = data[-1]['date']
        if last_date < today_str:
            last_close = data[-1]['close']
            data.append({'date': today_str, 'close': last_close, 'pct_chg': 0})
            logger.debug(f'  自动延展 {sym}: {last_date} -> {today_str} (沿用 {last_close})')
            modified = True
    if modified:
        save_spot_cache(cache)

def load_spot_cache() -> dict:
    """加载本地现货价格缓存 {symbol: [{date, close, pct_chg}, ...]}。"""
    if SPOT_CACHE_FILE.exists():
        try:
            with open(SPOT_CACHE_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return {}
    return {}


def save_spot_cache(cache: dict):
    """保存现货价格缓存到本地文件。"""
    SPOT_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SPOT_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def update_spot_cache(metal_cfg: dict, article_urls: dict) -> list:
    """
    从同花顺抓取现货价格并更新缓存。
    返回合并后的完整价格列表 [{date, close, pct_chg}, ...]。
    """
    symbol = metal_cfg['symbol']
    keywords = TONGHUASHUN_KEYWORD_MAP.get(symbol, [metal_cfg['name']])
    unit = metal_cfg.get('unit', '元/吨')

    # 加载已有缓存
    cache = load_spot_cache()
    cached_data = cache.get(symbol, [])

    # 尝试从同花顺抓取最新数据
    new_data = []
    url = article_urls.get(symbol, '')
    if url:
        new_data = _parse_tonghuashun_article(url, unit)
        if new_data:
            logger.info(f'  [{symbol}] 同花顺现货: {len(new_data)} 天数据')
        else:
            logger.warning(f'  [{symbol}] 同花顺文章解析失败: {url}')

    # 合并缓存+新数据，去重
    date_set = {d['date'] for d in cached_data}
    for item in new_data:
        if item['date'] not in date_set:
            cached_data.append(item)
            date_set.add(item['date'])

    # 按日期排序，只保留最近60条
    cached_data.sort(key=lambda x: x['date'])
    cached_data = cached_data[-60:]

    # 保存并自动延展到今日
    cache[symbol] = cached_data
    save_spot_cache(cache)
    _auto_extend_spot_cache()

    return cached_data


def spot_cache_to_kline(spot_data: list, unit: str = '元/吨') -> list:
    """
    将现货缓存数据转换为标准K线格式（兼容现有chart函数）。
    现货只有收盘价，开盘/最高/最低用收盘价填充。
    """
    result = []
    prev_close = None
    for item in spot_data:
        close_price = item['close']
        pct_chg = item.get('pct_chg', 0)
        if prev_close and pct_chg == 0:
            pct_chg = round(
                (close_price - prev_close) / prev_close * 100, 2
            ) if prev_close != 0 else 0

        result.append({
            'date': item['date'],
            'open': close_price,
            'close': close_price,
            'high': close_price,
            'low': close_price,
            'volume': 0,
            'pct_chg': pct_chg,
        })
        prev_close = close_price
    return result


def fetch_eastmoney_kline(secid: str, limit: int = 60) -> Optional[List[dict]]:
    """
    从东方财富获取日K线数据（备用数据源）。

    Args:
        secid: 东方财富证券代码，如 '113.CU0'
        limit: 获取的数据条数

    Returns:
        [{date, open, close, ...}, ...] 或 None
    """
    params = {
        'secid': secid,
        'fields1': 'f1,f2,f3,f4,f5,f6',
        'fields2': 'f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61',
        'klt': '101',
        'fqt': '0',
        'end': '20500101',
        'lmt': limit,
    }

    try:
        resp = SESSION.get(EASTMONEY_KLINE_URL, params=params,
                          headers=HEADERS_EASTMONEY, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        if data.get('rc') != 0 or not data.get('data'):
            logger.debug(f'东方财富API返回异常 (secid={secid}): rc={data.get("rc")}')
            return None

        klines = data['data'].get('klines', [])
        if not klines:
            return None

        result = []
        for line in klines:
            parts = line.split(',')
            if len(parts) < 7:
                continue
            result.append({
                'date': parts[0],
                'open': float(parts[1]),
                'close': float(parts[2]),
                'high': float(parts[3]),
                'low': float(parts[4]),
                'volume': float(parts[5]),
                'amount': float(parts[6]),
                'pct_chg': float(parts[8]) if len(parts) > 8 else 0,
            })

        return result

    except Exception as e:
        logger.debug(f'东方财富API失败 (secid={secid}): {e}')
        return None


def fetch_kline_data(metal_cfg: dict, limit: int = 60) -> Optional[List[dict]]:
    """
    统一的数据获取入口：优先使用新浪API，失败则尝试东方财富备用。

    Args:
        metal_cfg: 金属配置字典 (来自 config.yaml)
        limit: 需要获取的数据条数

    Returns:
        标准化的K线数据列表，或 None
    """
    symbol = metal_cfg['symbol']
    sina_code = metal_cfg.get('sina_code', None)
    eastmoney_secid = metal_cfg.get('eastmoney_secid', None)
    sina_stock_code = metal_cfg.get('sina_stock_code', None)
    source = metal_cfg.get('source', 'shfe_futures')

    # 0. 小金属现货数据源：缓存 > 同花顺 > 龙头股（仅当缓存无数据时）
    if source == 'stock_proxy':
        # 先用同花顺抓最新几天并更新缓存
        ths_url = metal_cfg.get('_ths_url', '')
        if ths_url:
            spot_data = update_spot_cache(metal_cfg, {symbol: ths_url})

        # 读缓存（含手工写入的30天历史）
        spot_data = load_spot_cache().get(symbol, [])
        if len(spot_data) >= 5:
            data = spot_cache_to_kline(spot_data)
            logger.info(f'  [{symbol}] 现货缓存: {len(data)} 条')
            return data

        # 缓存无数据，最后回退龙头股
        if sina_stock_code:
            logger.debug(f'  缓存无数据，回退新浪股票: {sina_stock_code}')
            data = fetch_sina_stock_kline(sina_stock_code, limit=limit)
            if data:
                logger.info(f'  [{symbol}] 新浪股票: {len(data)} 条')
                return data
        return None

    # 单位修正系数
    divisor = metal_cfg.get('price_divisor', 1)
    multiplier = metal_cfg.get('price_multiplier', 1)

    # 1. 优先尝试新浪期货API
    if sina_code:
        logger.debug(f'  尝试新浪API: {sina_code}')
        data = fetch_sina_kline(sina_code, limit=limit)
        if data:
            logger.info(f'  [{symbol}] 新浪API成功, 获取 {len(data)} 条')
            if divisor != 1 or multiplier != 1:
                for d in data:
                    d['close'] = d['close'] * multiplier / divisor
                    d['open'] = d['open'] * multiplier / divisor
                    d['high'] = d['high'] * multiplier / divisor
                    d['low'] = d['low'] * multiplier / divisor
                logger.info(f'  [{symbol}] 单位修正: *{multiplier}/÷{divisor}')
            return data
    else:
        logger.debug(f'  [{symbol}] 未配置新浪代码，跳过新浪API')

    # 2. 备用：东方财富API
    if eastmoney_secid:
        logger.debug(f'  尝试东方财富API: {eastmoney_secid}')
        data = fetch_eastmoney_kline(eastmoney_secid, limit=limit)
        if data:
            logger.info(f'  [{symbol}] 东方财富API成功, 获取 {len(data)} 条')
            if divisor != 1 or multiplier != 1:
                for d in data:
                    d['close'] = d['close'] * multiplier / divisor
                    d['open'] = d['open'] * multiplier / divisor
                    d['high'] = d['high'] * multiplier / divisor
                    d['low'] = d['low'] * multiplier / divisor
                logger.info(f'  [{symbol}] 单位修正: *{multiplier}/÷{divisor}')
            return data

    # 3. 都失败了
    logger.warning(f'  [{symbol}] 所有数据源均失败')
    return None


# ============================================================
# 数据处理
# ============================================================
def extract_period_data(
    kline_data: List[dict], periods: int
) -> Tuple[List[str], List[float]]:
    """
    从K线数据中提取最近N个交易日的数据。

    Returns:
        (dates_list, close_prices_list)
    """
    if not kline_data:
        return [], []

    recent = kline_data[-periods:] if len(kline_data) >= periods else kline_data

    dates = [item['date'] for item in recent]
    closes = [item['close'] for item in recent]

    return dates, closes


def calculate_change(data: List[float]) -> Optional[float]:
    """计算区间涨跌幅（%）。"""
    if len(data) < 2:
        return None
    return round((data[-1] - data[0]) / data[0] * 100, 2)


def extract_latest_info(kline_data: List[dict]) -> dict:
    """提取最新一个交易日的行情摘要。"""
    if not kline_data:
        return {}
    latest = kline_data[-1]
    return {
        'date': latest['date'],
        'close': latest['close'],
        'open': latest['open'],
        'high': latest['high'],
        'low': latest['low'],
        'pct_chg': latest.get('pct_chg', 0),
        'volume': latest.get('volume', 0),
    }


# ============================================================
# 图表生成模块
# ============================================================
COLOR_UP = '#DC143C'     # 红色（涨）
COLOR_DOWN = '#228B22'   # 绿色（跌）
COLOR_LINE = '#1E90FF'   # 主折线颜色（蓝色）
COLOR_SHORT = '#FF6347'  # 短期标注色
COLOR_LONG = '#4169E1'   # 长期标注色
COLOR_BG = '#FAFAFA'     # 背景色
COLOR_GRID = '#E5E5E5'   # 网格线颜色


def generate_single_metal_chart(
    metal_name: str,
    symbol: str,
    unit: str,
    kline_data: List[dict],
    short_period: int,
    long_period: int,
    font: FontProperties,
    source: str = 'shfe_futures',
) -> Optional[Path]:
    """
    A组期货品种：近3个月K线图 + MA5/10/20/60 + 10日/30日涨跌子图
    B组小金属品种：近30日折线图 + 10日详细走势（无OHLC数据，保持原样）
    """
    if not kline_data or len(kline_data) < 3:
        logger.warning(f'{metal_name}: 数据不足，无法生成图表')
        return None

    if source == 'stock_proxy':
        return _generate_spot_line_chart(
            metal_name, symbol, unit, kline_data,
            short_period, long_period, font, source
        )
    else:
        return _generate_futures_kline_chart(
            metal_name, symbol, unit, kline_data,
            short_period, long_period, font, source
        )


def _compute_ma(data: List[dict], period: int) -> List[float]:
    """计算移动平均线，不足period的填None"""
    closes = [d['close'] for d in data]
    mas = [None] * (period - 1)
    for i in range(period - 1, len(closes)):
        mas.append(sum(closes[i - period + 1:i + 1]) / period)
    return mas


def _generate_futures_kline_chart(
    metal_name, symbol, unit, kline_data,
    short_period, long_period, font, source
) -> Optional[Path]:
    """A组期货：K线蜡烛图 + MA5/10/20/60均线"""
    recent = kline_data[-65:] if len(kline_data) > 65 else kline_data
    dates = [d['date'] for d in recent]
    opens = [d['open'] for d in recent]
    highs = [d['high'] for d in recent]
    lows = [d['low'] for d in recent]
    closes = [d['close'] for d in recent]

    ma5 = _compute_ma(recent, 5)
    ma10 = _compute_ma(recent, 10)
    ma20 = _compute_ma(recent, 20)
    ma60 = _compute_ma(recent, 60)

    fig, ax = plt.subplots(figsize=(20, 8), dpi=150, facecolor='#16213e')
    ax.set_facecolor('#1a1a2e')
    ax.grid(True, linestyle='--', alpha=0.15, color='#ffffff')

    for i, (o, h, l, c) in enumerate(zip(opens, highs, lows, closes)):
        color = COLOR_UP if c >= o else COLOR_DOWN
        body_bottom = min(o, c)
        body_height = abs(c - o) or 0.0001
        ax.bar(i, body_height, bottom=body_bottom, width=0.6,
               color=color, edgecolor=color, linewidth=0.5, zorder=3)
        ax.plot([i, i], [l, h], color=color, linewidth=0.8, zorder=3)

    for data, label, clr, lw in [
        (ma5, 'MA5', '#FFD700', 1.2),
        (ma10, 'MA10', '#00CED1', 1.2),
        (ma20, 'MA20', '#FF69B4', 1.5),
        (ma60, 'MA60', '#FF4500', 2.0),
    ]:
        valid_x = [i for i, v in enumerate(data) if v is not None]
        valid_y = [v for v in data if v is not None]
        if valid_x:
            ax.plot(valid_x, valid_y, color=clr, linewidth=lw, label=label, zorder=5)

    step = max(1, len(recent) // 15)
    tick_pos = list(range(0, len(recent), step))
    if tick_pos[-1] != len(recent) - 1:
        tick_pos.append(len(recent) - 1)
    ax.set_xticks(tick_pos)
    ax.set_xticklabels([dates[i][5:] for i in tick_pos],
                       fontsize=8, rotation=30, ha='right')
    ax.set_xlim(-0.5, len(recent) - 0.5)

    # MA cross 标注
    num = len(recent)
    short_chg_val = calculate_change(closes[-short_period:])
    long_chg_val = calculate_change(closes[-long_period:])
    short_sgn = '+' if (short_chg_val or 0) > 0 else ''
    long_sgn = '+' if (long_chg_val or 0) > 0 else ''

    ax.set_title(
        f'{metal_name}({symbol}) - 近{num}交易日K线  |  '
        f'{short_period}日:{short_sgn}{short_chg_val}%  |  '
        f'{long_period}日:{long_sgn}{long_chg_val}%',
        fontsize=14, fontweight='bold', color='white',
        fontproperties=font, pad=12)
    ax.set_ylabel(f'价格 ({unit})', fontsize=9, color='white', fontproperties=font)
    ax.tick_params(axis='y', colors='white', labelsize=8)
    ax.tick_params(axis='x', colors='#aaa', labelsize=8)
    ax.legend(loc='upper left', fontsize=8, ncol=4,
              framealpha=0.6, facecolor='#1a1a2e', edgecolor='#444',
              labelcolor='white')

    latest_c = closes[-1]
    ax.annotate(f'{latest_c:.0f}', xy=(len(recent)-1, latest_c),
                xytext=(15, 0), textcoords='offset points',
                fontsize=10, color='white', fontweight='bold',
                va='center', fontproperties=font)

    plt.tight_layout(pad=2)
    filename = f'{metal_name}_{datetime.now().strftime("%Y%m%d")}.png'
    filepath = OUTPUT_DIR / filename
    fig.savefig(filepath, dpi=150, bbox_inches='tight',
               facecolor='#16213e', edgecolor='none')
    plt.close(fig)
    logger.info(f'图表已生成: {filepath}')
    return filepath

def _generate_spot_line_chart(
    metal_name, symbol, unit, kline_data,
    short_period, long_period, font, source
) -> Optional[Path]:
    """B组小金属：折线图，与之前一致（无OHLC）"""
    source_label = '（现货参考价）'
    short_dates, short_closes = extract_period_data(kline_data, short_period)
    long_dates, long_closes = extract_period_data(kline_data, long_period)
    short_change = calculate_change(short_closes)
    long_change = calculate_change(long_closes)
    change_color = COLOR_UP if (long_change or 0) >= 0 else COLOR_DOWN

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 10), dpi=150, facecolor='white')

    # Upper: 30-day
    ax1.set_facecolor(COLOR_BG)
    ax1.grid(True, linestyle='--', alpha=0.5, color=COLOR_GRID)
    if long_dates:
        x_long = range(len(long_dates))
        ax1.plot(x_long, long_closes, color=COLOR_LONG, linewidth=2,
                marker='o', markersize=4, markerfacecolor='white',
                markeredgewidth=1.5, markeredgecolor=COLOR_LONG,
                label=f'收盘价 ({long_period}日)', zorder=3)
        ax1.fill_between(x_long, long_closes, min(long_closes)*0.995,
                        alpha=0.15, color=COLOR_LONG)
        step = max(1, len(long_dates)//8)
        tpos = list(range(0, len(long_dates), step))
        ax1.set_xticks(tpos)
        ax1.set_xticklabels([long_dates[i][5:] for i in tpos],
                           fontsize=8, rotation=30, ha='right')
    ax1.set_title(f'{metal_name}({symbol}) - 近{long_period}交易日价格走势{source_label}',
                 fontsize=14, fontweight='bold', fontproperties=font, pad=15)
    ax1.set_ylabel(f'价格 ({unit})', fontsize=10, fontproperties=font)
    ax1.legend(loc='upper left', prop=font, fontsize=9)
    if long_change is not None:
        sgn = '+' if long_change > 0 else ''
        ax1.text(0.99, 0.05, f'{long_period}日涨跌: {sgn}{long_change}%',
                transform=ax1.transAxes, fontsize=11, fontweight='bold',
                color=change_color, ha='right', va='bottom',
                fontproperties=font,
                bbox=dict(boxstyle='round,pad=0.4', facecolor='white',
                         edgecolor=change_color, alpha=0.9))

    # Lower: 10-day
    ax2.set_facecolor(COLOR_BG)
    ax2.grid(True, linestyle='--', alpha=0.5, color=COLOR_GRID)
    if short_dates:
        x_short = range(len(short_dates))
        ax2.plot(x_short, short_closes, color=COLOR_SHORT, linewidth=2.5,
                marker='s', markersize=5, markerfacecolor='white',
                markeredgewidth=2, markeredgecolor=COLOR_SHORT,
                label=f'收盘价 ({short_period}日)', zorder=3)
        ax2.fill_between(x_short, short_closes, min(short_closes)*0.998,
                        alpha=0.2, color=COLOR_SHORT)
        for j, (d, cv) in enumerate(zip(short_dates, short_closes)):
            if j == 0 or j == len(short_dates)-1 or j % 3 == 0:
                ax2.annotate(f'{cv:.0f}', xy=(j, cv),
                            xytext=(0, 10), textcoords='offset points',
                            fontsize=7, color='#555', ha='center',
                            fontproperties=font)
        ax2.set_xticks(range(len(short_dates)))
        ax2.set_xticklabels([d[5:] for d in short_dates],
                           fontsize=9, rotation=30, ha='right')
    ax2.set_title(f'{metal_name}({symbol}) - 近{short_period}交易日价格走势（详细）{source_label}',
                 fontsize=14, fontweight='bold', fontproperties=font, pad=15)
    ax2.set_ylabel(f'价格 ({unit})', fontsize=10, fontproperties=font)
    ax2.set_xlabel('日期 (MM-DD)', fontsize=10, fontproperties=font)
    ax2.legend(loc='upper left', prop=font, fontsize=9)
    if short_change is not None:
        sgn = '+' if short_change > 0 else ''
        ax2.text(0.99, 0.05, f'{short_period}日涨跌: {sgn}{short_change}%',
                transform=ax2.transAxes, fontsize=11, fontweight='bold',
                color=COLOR_UP if short_change>=0 else COLOR_DOWN,
                ha='right', va='bottom', fontproperties=font,
                bbox=dict(boxstyle='round,pad=0.4', facecolor='white',
                         edgecolor=COLOR_UP if short_change>=0 else COLOR_DOWN, alpha=0.9))

    plt.tight_layout(pad=3)
    filename = f'{metal_name}_{datetime.now().strftime("%Y%m%d")}.png'
    filepath = OUTPUT_DIR / filename
    fig.savefig(filepath, dpi=150, bbox_inches='tight',
               facecolor='white', edgecolor='none')
    plt.close(fig)
    logger.info(f'图表已生成: {filepath}')
    return filepath

def generate_comparison_charts_grouped(
    metals_data: List[dict],
    long_period: int,
    font: FontProperties,
    group_size: int = 5,
    prefix: str = '',
) -> List[Path]:
    """
    生成对比图，每张最多 group_size 个品种。返回文件路径列表。
    """
    valid = [m for m in metals_data if m.get('dates_30') and len(m['dates_30']) >= 5]
    if not valid:
        return []

    files = []
    n_groups = (len(valid) + group_size - 1) // group_size
    colors = ['#E74C3C','#3498DB','#2ECC71','#F39C12','#9B59B6',
              '#1ABC9C','#E67E22','#2980B9','#27AE60','#8E44AD',
              '#D35400','#16A085','#C0392B']

    for gi in range(n_groups):
        chunk = valid[gi * group_size:(gi + 1) * group_size]
        start_n = gi * group_size + 1
        end_n = gi * group_size + len(chunk)
        fig, ax = plt.subplots(figsize=(16, 8), dpi=150, facecolor='white')
        ax.set_facecolor(COLOR_BG)
        ax.grid(True, linestyle='--', alpha=0.5, color=COLOR_GRID)

        for i, metal in enumerate(chunk):
            color = colors[i % len(colors)]
            closes = metal['closes_30']
            normalized = [c / closes[0] * 100 for c in closes] if closes[0] != 0 else closes
            x = range(len(normalized))
            ax.plot(x, normalized, color=color, linewidth=2.5, marker='o',
                   markersize=5, markerfacecolor='white', markeredgewidth=2,
                   label=f"{metal['name']}({metal['symbol']})",
                   markevery=max(1, len(x)//6), zorder=3)

        ax.axhline(y=100, color='#999', linestyle='-', linewidth=1, alpha=0.7, zorder=1)

        all_dates = chunk[0]['dates_30']
        step = max(1, len(all_dates) // 8)
        tick_pos = list(range(0, len(all_dates), step))
        ax.set_xticks(tick_pos)
        ax.set_xticklabels([all_dates[i][5:] for i in tick_pos],
                          fontsize=8, rotation=30, ha='right')

        ax.set_title(
            f'走势对比（近{long_period}交易日，基准日=100）[{start_n}-{end_n}]',
            fontsize=15, fontweight='bold', fontproperties=font, pad=15)
        ax.set_ylabel('相对价格 (基准=100)', fontsize=11, fontproperties=font)
        ax.set_xlabel('日期 (MM-DD)', fontsize=11, fontproperties=font)
        ax.legend(loc='best', prop=font, fontsize=9, ncol=1,
                 framealpha=0.9, edgecolor='#ddd')

        plt.tight_layout(pad=2)
        dt = datetime.now().strftime('%Y%m%d')
        tag = f'{prefix}_' if prefix else ''
        filepath = OUTPUT_DIR / f'comparison_{tag}{gi+1}_{dt}.png'
        fig.savefig(filepath, dpi=150, bbox_inches='tight', facecolor='white', edgecolor='none')
        plt.close(fig)
        files.append(filepath)
        logger.info(f'对比图[{start_n}-{end_n}]已生成: {filepath}')

    return files


# CID image tracking for send_email
_COMPARISON_CID_FILES = []


# ============================================================
# 邮件发送模块
# ============================================================
def image_to_base64(filepath: Path) -> str:
    """将图片文件编码为 base64 字符串（用于HTML内嵌）。"""
    with open(filepath, 'rb') as f:
        return base64.b64encode(f.read()).decode('utf-8')

# === 区间价格 + 概览 + 免责 ===

def _fmt_price(price: float, unit: str) -> str:
    """将精确价格转为区间描述"""
    if '元/吨' in unit and '度' not in unit:
        wan = price / 10000
        return f'约{wan:.2f}万元/吨'
    elif '元/千克' in unit:
        return f'约{price:,.0f}元/千克'
    elif '元/吨度' in unit:
        return f'约{price:,.0f}元/吨度'
    elif '元/克' in unit:
        return f'约{price:,.0f}元/克'
    else:
        return f'约{price:,.0f}'


def _gen_overview(metals_data: list) -> str:
    """基于5日涨跌生成市场概览HTML"""
    valid = [m for m in metals_data if m.get('change_5d') is not None]
    if not valid:
        return ''
    up = [m for m in valid if m['change_5d'] > 0]
    down = [m for m in valid if m['change_5d'] < 0]
    up_names = '、'.join(m['name'] for m in up) if up else '无'
    down_names = '、'.join(m['name'] for m in down) if down else '无'
    up_avg = sum(m['change_5d'] for m in up) / len(up) if up else 0
    down_avg = sum(m['change_5d'] for m in down) / len(down) if down else 0
    total_avg = sum(m['change_5d'] for m in valid) / len(valid)
    if total_avg > 1: mood, mood_clr = '偏强', '#DC143C'
    elif total_avg < -1: mood, mood_clr = '偏弱', '#228B22'
    elif total_avg >= 0: mood, mood_clr = '震荡', '#E67E22'
    else: mood, mood_clr = '震荡偏弱', '#228B22'
    supply = []; demand = []; macro = []
    if len(down) > len(up):
        macro.append('宏观预期偏弱，美元走强对大宗形成压制')
        demand.append('下游采购偏谨慎，以按需补库为主')
        supply.append('部分品种库存回升，供给压力有所显现')
    elif len(up) > len(down):
        macro.append('宏观情绪回暖，市场风险偏好有所提升')
        demand.append('新能源、军工等领域需求保持韧性')
        supply.append('矿山供给偏紧，部分品种现货流通趋紧')
    else:
        macro.append('宏观面多空交织，市场等待进一步政策信号')
        demand.append('下游需求分化，结构性差异明显')
        supply.append('供给端整体平稳，局部存在扰动因素')
    if up:
        demand.append('、'.join(m['name'] for m in up[:5]) + '受需求支撑及供给收缩影响，价格坚挺')
    if down:
        supply.append('、'.join(m['name'] for m in down[:5]) + '受库存回升及下游观望影响，价格承压')
    return (
        '<div style="background:linear-gradient(135deg,#f5f7fa 0%,#e8ecf1 100%);'
        'border-radius:10px;padding:20px;margin:15px 0;border:1px solid #d0d5dd">'
        '<h3 style="color:#1a1a2e;margin-top:0;font-size:16px">&#x1f4ca; 本周市场概览</h3>'
        f'<p style="font-size:14px;line-height:1.8;color:#333">'
        f'本周有色金属板块整体呈现<strong style="color:{mood_clr}">{mood}调整</strong>态势，'
        f'受宏观预期与供需变化影响，多数品种出现不同程度波动。</p>'
        f'<p style="font-size:13px;line-height:1.6;color:#555;margin:10px 0 5px 0">'
        f'<span style="color:#DC143C;font-weight:bold">▲ 上涨品种（{len(up)}个）：</span>{up_names}'
        f'&nbsp;|&nbsp; 平均 +{up_avg:.2f}%</p>'
        f'<p style="font-size:13px;line-height:1.6;color:#555;margin:5px 0">'
        f'<span style="color:#228B22;font-weight:bold">▼ 下跌品种（{len(down)}个）：</span>{down_names}'
        f'&nbsp;|&nbsp; 平均 {down_avg:.2f}%</p>'
        '<div style="background:#fff;border:1px solid #e0e0e0;border-radius:8px;'
        'padding:15px;margin:15px 0 5px 0">'
        '<h4 style="color:#2c3e50;margin:0 0 10px 0;font-size:13px">&#x1f50d; 驱动因素分析</h4>'
        f'<p style="font-size:12px;color:#666;line-height:1.8;margin:0">'
        f'<strong>• 宏观面：</strong>{";".join(macro)}<br>'
        f'<strong>• 需求端：</strong>{";".join(demand)}<br>'
        f'<strong>• 供给端：</strong>{";".join(supply)}<br></p></div></div>'
    )


_DISCLAIMER = (
    '<div style="background:#fff8e1;border:2px solid #f9a825;border-radius:8px;'
    'padding:18px;margin:25px 0 0 0">'
    '<h3 style="color:#f57f17;margin:0 0 10px 0;font-size:14px">&#x26a0;&#xfe0f; 重要声明</h3>'
    '<p style="font-size:11px;color:#795548;line-height:1.8;margin:0">'
    '本报告仅基于公开市场信息进行整理与分析，所有价格信息为非精确参考值，不代表任何交易所或数据商的官方行情。<br>'
    '我方不具备证券、期货投资咨询资质，所有内容不构成任何交易建议，使用者据此做出的任何决策，风险自行承担。<br>'
    '本内容为我方独立整理的行业信息，禁止未经授权的转载、复制或商用。'
    '</p></div>'
)



def build_html_table(
    metals_data: list, short_period: int, long_period: int,
    table_title: str,
) -> str:
    # 按5日涨跌幅倒序，其次10日涨跌幅倒序
    sorted_data = sorted(
        [m for m in metals_data if m.get('latest')],
        key=lambda m: (m.get('change_5d') if m.get('change_5d') is not None else -999,
                      m.get('change_short') if m.get('change_short') is not None else -999),
        reverse=True
    )
    rows_html = ''
    for m in sorted_data:
        latest = m['latest']
        chg_5d = m.get('change_5d')
        chg_short = m.get('change_short')
        chg_long = m.get('change_long')
        price = latest['close']
        price_text = _fmt_price(price, m.get('unit', ''))
        inv = m.get('inventory', '-')
        sd = m.get('supply_demand', '-')

        def color_span(val):
            if val is None:
                return '<td style="color:#999;text-align:center;font-size:11px">-</td>'
            sgn = '+' if val > 0 else ''
            c = '#DC143C' if val >= 0 else '#228B22'
            return '<td style="color:' + c + ';text-align:center;font-weight:bold;font-size:11px">' + sgn + str(val) + '%</td>'

        name_style = 'text-align:center;font-weight:bold'
        source_note = ''
        if m.get('source') == 'stock_proxy':
            name_style += ';color:#E67E22'
            source_note = ' <span style="font-size:10px;color:#E67E22">*</span>'

        rows_html += (
            '<tr>'
            '<td style="' + name_style + ';padding:8px">' + m['name'] + source_note + '</td>'
            '<td style="text-align:center;font-size:11px;color:#555;padding:8px">' + price_text + '</td>'
            + color_span(chg_5d) +
            color_span(chg_short) +
            color_span(chg_long) +
            '<td style="text-align:left;font-size:10px;color:#555;padding:6px 8px;line-height:1.4">' + inv + '</td>'
            '<td style="text-align:left;font-size:10px;color:#555;padding:6px 10px;line-height:1.4">' + sd + '</td>'
            '</tr>'
        )

    return (
        '<h3 style="color:#2c3e50;border-left:4px solid #3498db;padding-left:10px">' + table_title + '</h3>'
        '<table style="width:100%;border-collapse:collapse;font-size:12px;border:1px solid #ddd;margin-top:10px;margin-bottom:20px">'
        '<thead><tr style="background:#2c3e50;color:white">'
        '<th style="padding:8px;border:1px solid #2c3e50;width:4%">品种</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:10%">参考价格</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:5%">5日</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:5%">' + str(short_period) + '日</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:5%">' + str(long_period) + '日</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:33%">行业库存</th>'
        '<th style="padding:8px;border:1px solid #2c3e50;width:38%">供需简析</th>'
        '</tr></thead><tbody>' + rows_html + '</tbody></table>'
    )


def build_email_html(
    metals_data: List[dict],
    short_period: int,
    long_period: int,
    comparison_b64_list_a: List[str],
    comparison_b64_list_b: List[str],
    font: FontProperties,
) -> str:
    """构建完整的HTML邮件内容。"""
    now_str = datetime.now().strftime('%Y年%m月%d日 %H:%M')

    # 计算5日涨跌 + 简化单位
    for m in metals_data:
        closes = m.get('closes_10', [])
        if len(closes) >= 5:
            c5 = closes[-5:]
            if c5 and c5[0] != 0:
                m['change_5d'] = round((c5[-1] - c5[0]) / c5[0] * 100, 2)
            else:
                m['change_5d'] = 0
        else:
            m['change_5d'] = None
        u = m.get('unit', '')
        m['unit_short'] = u.split('（')[0] if '（' in u else u

    # 拆分为两组
    futures_metals = [
        m for m in metals_data
        if m.get('source') == 'shfe_futures' and m.get('latest')
    ]
    small_metals = [
        m for m in metals_data
        if m.get('source') == 'stock_proxy' and m.get('latest')
    ]

    table_html_a = build_html_table(
        futures_metals, short_period, long_period,
        '📋 A组：传统有色金属  数据截至：' + (futures_metals[0]['latest']['date'] if futures_metals else now_str)
    )

    table_html_b = ''
    small_note = ''
    if small_metals:
        table_html_b = build_html_table(
            small_metals, short_period, long_period,
            '📋 B组：稀缺小金属  数据截至：' + (small_metals[0]['latest']['date'] if small_metals else now_str)
        )
        small_note = ''

    # 市场概览
    _COMPARISON_CID_FILES.clear()
    overview_html = _gen_overview(metals_data)

    comparison_section = ''

    comparison_cid_files = []  # [(cid, filepath)] for send_email

    if comparison_b64_list_a:
        comparison_section += '<h3 style="color:#2c3e50;border-left:4px solid #3498db;padding-left:10px">\U0001f4c8 A组：期货品种走势对比（基准日=100）</h3>'
        for idx in range(len(comparison_b64_list_a)):
            cid = 'cmp_a_%d@chart' % (idx+1)
            label = '[%d/%d]' % (idx+1, len(comparison_b64_list_a))
            comparison_section += ('<p style="font-size:12px;color:#888;margin:5px 0 2px 0">%s</p>'
                                  '<img src="cid:%s" style="max-width:100%%;border:1px solid #ddd;border-radius:4px;margin:5px 0 10px 0;box-shadow:0 2px 8px rgba(0,0,0,0.1)" alt="A组走势对比图" />') % (label, cid)
            data = (cid, comparison_b64_list_a[idx], 'cmp_a_%d.png' % (idx+1))
            comparison_cid_files.append(data)
            _COMPARISON_CID_FILES.append(data)

    if comparison_b64_list_b:
        comparison_section += '<h3 style="color:#2c3e50;border-left:4px solid #E67E22;padding-left:10px">\U0001f4c8 B组：稀缺小金属走势对比（基准日=100）</h3>'
        for idx in range(len(comparison_b64_list_b)):
            cid = 'cmp_b_%d@chart' % (idx+1)
            label = '[%d/%d]' % (idx+1, len(comparison_b64_list_b))
            comparison_section += ('<p style="font-size:12px;color:#888;margin:5px 0 2px 0">%s</p>'
                                  '<img src="cid:%s" style="max-width:100%%;border:1px solid #ddd;border-radius:4px;margin:5px 0 10px 0;box-shadow:0 2px 8px rgba(0,0,0,0.1)" alt="B组走势对比图" />') % (label, cid)
            data = (cid, comparison_b64_list_b[idx], 'cmp_b_%d.png' % (idx+1))
            comparison_cid_files.append(data)
            _COMPARISON_CID_FILES.append(data)

    up_count = sum(1 for m in metals_data
                   if (m.get('change_short') or 0) > 0)
    down_count = sum(1 for m in metals_data
                     if (m.get('change_short') or 0) < 0)
    flat_count = len(metals_data) - up_count - down_count

    summary_html = f'''
    <div style="background:#f8f9fa;border-radius:8px;
                padding:15px;margin:15px 0">
        <strong>📊 近{short_period}日概览：</strong>
        <span style="color:#DC143C">上涨 {up_count} 个</span> &nbsp;|&nbsp;
        <span style="color:#228B22">下跌 {down_count} 个</span> &nbsp;|&nbsp;
        <span>持平/无数据 {flat_count} 个</span>
    </div>
    '''

    return f'''
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family:'Microsoft YaHei','PingFang SC',sans-serif;
                 max-width:900px;margin:0 auto;padding:20px;
                 background:#f5f5f5">
    <div style="background:white;border-radius:8px;padding:25px;
                box-shadow:0 2px 12px rgba(0,0,0,0.08)">

        <h2 style="color:#2c3e50;border-bottom:3px solid #3498db;
                   padding-bottom:10px">
            🏭 有色金属价格监控报告
        </h2>
        <p style="color:#666;font-size:13px">
            生成时间：{now_str} &nbsp;|&nbsp;
            
            统计周期：近{short_period}个/近{long_period}个交易日
        </p>

        {overview_html}
        {summary_html}

        {table_html_a}
        {small_note}
        {table_html_b}

        {comparison_section}

        <hr style="border:none;border-top:1px solid #eee;margin:20px 0" />

        <p style="color:#999;font-size:11px;text-align:center">
            本报告仅供参考，不构成投资建议
        </p>
    </div>
    {_DISCLAIMER}
    </body>
    </html>
    '''


def load_recipients_from_csv(csv_path: Path) -> List[str]:
    """从CSV加载仍在有效期内的收件人，自动去重。CSV列: email, expiry_date"""
    today = datetime.now().strftime('%Y-%m-%d')
    recipients = set()
    if not csv_path.exists():
        logger.warning(f'收件人CSV不存在: {csv_path}')
        return []
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.reader(f)
        header = next(reader, None)
        for row in reader:
            if len(row) < 2:
                continue
            email = row[0].strip()
            expiry = row[1].strip()
            if not email or not expiry:
                continue
            if expiry >= today:
                recipients.add(email)
    result = sorted(recipients)
    logger.info(f'有效收件人: {len(result)} 个 (已去重，过滤到期日<{today})')
    return result


def send_email(
    config: dict,
    html_content: str,
    attachments: List[Path],
    font: FontProperties,
) -> bool:
    """发送HTML邮件（含附件）。GitHub Actions环境变量优先于配置文件。"""
    email_cfg = config.get('email', {})
    if not email_cfg:
        logger.warning('邮件配置为空，跳过发送')
        return False

    # 环境变量优先（GitHub Secrets），回退配置文件
    sender = os.getenv('SMTP_USER', email_cfg.get('sender', ''))
    password = os.getenv('SMTP_PASS', email_cfg.get('password', ''))
    smtp_server = os.getenv('SMTP_HOST', email_cfg.get('smtp_server', 'smtp.qq.com'))
    smtp_port = int(os.getenv('SMTP_PORT', email_cfg.get('smtp_port', 465)))
    use_ssl = os.getenv('SMTP_SSL', str(email_cfg.get('use_ssl', True))).lower() == 'true'

    # 从CSV加载收件人（优先），回退到config中的recipients
    csv_path = BASE_DIR / 'recipients.csv'
    recipients = load_recipients_from_csv(csv_path)
    if not recipients:
        recipients = email_cfg.get('recipients', [])
    subject_prefix = email_cfg.get(
        'subject_prefix', '【有色金属价格监控】'
    )

    if not sender or not password:
        logger.error('邮件发送方或密码未配置')
        return False
    if not recipients:
        logger.error('邮件收件人未配置')
        return False

    now_str = datetime.now().strftime('%Y-%m-%d')
    subject = f'{subject_prefix}{now_str}'

    msg = MIMEMultipart('mixed')
    msg['Subject'] = subject
    msg['From'] = sender
    msg['To'] = ', '.join(recipients)
    msg['Date'] = datetime.now().strftime('%a, %d %b %Y %H:%M:%S +0800')
    msg['Message-ID'] = f'<metals-{datetime.now().strftime("%Y%m%d%H%M%S")}@{sender.split("@")[-1]}>'
    msg['X-Priority'] = '3'
    msg['X-Mailer'] = 'Metals Daily Reporter'

    # HTML正文 with CID inline images
    html_part = MIMEMultipart('related')

    # Rebuild comparison images as CID inline from _COMPARISON_CID_FILES
    cids_used = set()
    for cid, b64, fname in _COMPARISON_CID_FILES:
        if cid in cids_used:
            continue
        cids_used.add(cid)
        try:
            img_data = base64.b64decode(b64)
            img = MIMEImage(img_data, _subtype='png')
            img.add_header('Content-ID', f'<{cid}>')
            img.add_header('Content-Disposition', 'inline', filename=fname)
            html_part.attach(img)
        except Exception:
            pass  # skip broken b64

    html_part.attach(MIMEText(html_content, 'html', 'utf-8'))
    msg.attach(html_part)

    # Regular attachments (chart PNGs for download)
    for filepath in attachments:
        if filepath and filepath.exists():
            with open(filepath, 'rb') as f:
                attachment = MIMEBase('application', 'octet-stream')
                attachment.set_payload(f.read())
                encoders.encode_base64(attachment)
                attachment.add_header(
                    'Content-Disposition', 'attachment',
                    filename=('utf-8', '', filepath.name)
                )
                msg.attach(attachment)

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
            server.starttls()

        server.login(sender, password)
        server.sendmail(sender, recipients, msg.as_string())
        server.quit()

        logger.info(f'邮件发送成功 -> {", ".join(recipients)}')
        return True

    except smtplib.SMTPAuthenticationError:
        logger.error('SMTP认证失败，请检查邮箱地址和授权码')
        return False
    except smtplib.SMTPConnectError:
        logger.error(f'无法连接到SMTP服务器 {smtp_server}:{smtp_port}')
        return False
    except Exception as e:
        logger.error(f'邮件发送失败: {e}')
        return False


# ============================================================
# 主流程
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description='有色金属价格监控系统'
    )
    parser.add_argument(
        '--config', '-c', type=Path, default=DEFAULT_CONFIG,
        help=f'配置文件路径（默认: {DEFAULT_CONFIG}）'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='测试模式：仅获取数据和生成图表，不发送邮件'
    )
    parser.add_argument(
        '--no-email', action='store_true',
        help='跳过邮件发送'
    )
    parser.add_argument(
        '--verbose', '-v', action='store_true',
        help='详细日志输出'
    )
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    logger.info('=' * 60)
    logger.info('有色金属价格监控系统 启动')
    logger.info('=' * 60)

    # 1. 加载配置
    config = load_config(args.config)

    # 2. 设置中文字体
    font = setup_chinese_font()

    # 3. 创建输出目录
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 4. 获取数据
    short_period = config['chart']['short_period']
    long_period = config['chart']['long_period']
    # A组期货需要3个月K线(+MA60)，B组只需要30天现货
    fetch_limit_futures = 75  # 3 months of trading days
    fetch_limit_spot = long_period + 15

    logger.info(
        f'开始获取 {len(config["metals"])} 个品种的价格数据...'
    )
    logger.info(f'短周期: {short_period}日, 长周期: {long_period}日')

    # 预先获取同花顺文章列表（用于小金属现货）
    ths_articles = _fetch_tonghuashun_article_list()
    if ths_articles:
        logger.info(f'同花顺现货文章匹配: {len(ths_articles)} 个品种')
    else:
        logger.info('同花顺首页无匹配（将使用龙头股回退）')

    metals_data = []
    chart_files = []

    for metal_cfg in config['metals']:
        name = metal_cfg['name']
        # 附加同花顺文章URL
        if metal_cfg.get('source') == 'stock_proxy':
            ths_found = ths_articles.get(metal_cfg['symbol'], '')
            metal_cfg['_ths_url'] = ths_found
        symbol = metal_cfg['symbol']
        unit = metal_cfg.get('unit', '')

        logger.info(f'正在获取 {name}({symbol}) 数据...')
        source_type = metal_cfg.get('source', 'shfe_futures')
        flimit = fetch_limit_futures if source_type == 'shfe_futures' else fetch_limit_spot
        kline_data = fetch_kline_data(metal_cfg, limit=flimit)

        if not kline_data:
            logger.warning(f'  {name}({symbol}): 未能获取数据，跳过')
            metals_data.append({
                'name': name,
                'symbol': symbol,
                'unit': unit,
                'error': '数据获取失败',
            })
            continue

        logger.info(
            f'  {name}({symbol}): 获取到 {len(kline_data)} 条数据'
        )

        # 提取不同周期的数据
        short_dates, short_closes = extract_period_data(
            kline_data, short_period
        )
        long_dates, long_closes = extract_period_data(
            kline_data, long_period
        )
        latest = extract_latest_info(kline_data)

        change_short = calculate_change(short_closes)
        change_long = calculate_change(long_closes)

        metal_entry = {
            'name': name,
            'symbol': symbol,
            'unit': unit,
            'source': metal_cfg.get('source', 'shfe_futures'),
            'recommend_stock': metal_cfg.get('recommend_stock', '-'),
            'latest': latest,
            'dates_10': short_dates,
            'closes_10': short_closes,
            'dates_30': long_dates,
            'closes_30': long_closes,
            'inventory': metal_cfg.get('inventory', '-'),
            'supply_demand': metal_cfg.get('supply_demand', '-'),
            'change_5d': calculate_change(short_closes[-5:]) if len(short_closes) >= 5 else None,
            'change_short': change_short,
            'change_long': change_long,
            'kline_data': kline_data,
        }
        metals_data.append(metal_entry)

        # 单品种图表已移除（仅保留对比图）

    # 5. 生成对比图（A/B分家，先按5日涨跌幅排序，每组最多6品种）
    logger.info('生成价格对比图...')
    sort_key = lambda m: (m.get('change_5d') if m.get('change_5d') is not None else -999,
                          m.get('change_short') if m.get('change_short') is not None else -999)
    futures_data = sorted(
        [m for m in metals_data if m.get('source') == 'shfe_futures'],
        key=sort_key, reverse=True
    )
    small_data = sorted(
        [m for m in metals_data if m.get('source') == 'stock_proxy'],
        key=sort_key, reverse=True
    )

    comparison_b64_list_a = []
    comparison_b64_list_b = []

    if futures_data:
        paths = generate_comparison_charts_grouped(futures_data, long_period, font, group_size=6, prefix="a")
        for p in paths:
            comparison_b64_list_a.append(image_to_base64(p))
            chart_files.append(p)

    if small_data:
        paths = generate_comparison_charts_grouped(small_data, long_period, font, group_size=6, prefix="b")
        for p in paths:
            comparison_b64_list_b.append(image_to_base64(p))
            chart_files.append(p)

    # 6. 输出控制台摘要
    def print_table(group_name, group_data):
        header = (f'{"金属":<6} {"最新价":>10} {"单位":<12} {"5日涨跌":>8} '
                  f'{short_period}日涨跌{"":>4} {long_period}日涨跌{"":>4}')
        logger.info('')
        logger.info(f'--- {group_name} ---')
        logger.info(header)
        logger.info('-' * len(header))
        for m in group_data:
            if m.get('latest'):
                l = m['latest']
                cs = m.get('change_short')
                cl = m.get('change_long')
                cs_str = f'{cs:+.2f}%' if cs is not None else 'N/A'
                cl_str = f'{cl:+.2f}%' if cl is not None else 'N/A'
                unit_str = m.get('unit', '')
                logger.info(
                    f'{m["name"]:<6} {l["close"]:>10.0f} {unit_str:<12} '
                    f'{m.get("change_5d",0) if m.get("change_5d") is not None else 0:>+8.2f}% {cs_str:>10} {cl_str:>10}'
                )
            else:
                logger.info(f'{m["name"]:<6} {"获取失败":>10}')

    futures = [m for m in metals_data if m.get('source') == 'shfe_futures']
    small = [m for m in metals_data if m.get('source') == 'stock_proxy']
    if futures:
        print_table('A组：传统有色金属', futures)
    if small:
        print_table('B组：稀缺小金属', small)

    logger.info('')
    logger.info(f'共生成 {len(chart_files)} 个图表文件')

    # 7. 发送邮件
    if args.dry_run:
        logger.info('[DRY-RUN] 测试模式，跳过邮件发送')
        logger.info(f'图表已保存至: {OUTPUT_DIR}')
        return

    if args.no_email:
        logger.info('已指定 --no-email，跳过邮件发送')
        logger.info(f'图表已保存至: {OUTPUT_DIR}')
        return

    email_cfg = config.get('email', {})
    sender = email_cfg.get('sender', '')
    if not sender or 'your_email' in sender:
        logger.warning(
            '邮件发件人未配置或使用默认值，跳过发送。'
            '请编辑 config.yaml 中的 email 设置。'
        )
        logger.info(f'图表已保存至: {OUTPUT_DIR}')
        return

    html = build_email_html(
        metals_data, short_period, long_period,
        comparison_b64_list_a, comparison_b64_list_b, font
    )

    success = send_email(config, html, chart_files, font)
    if success:
        logger.info('[OK] 全部完成！邮件已发送，图表见附件。')
    else:
        logger.warning('[WARN] 邮件发送失败，但图表已生成。')
        logger.info(f'图表保存位置: {OUTPUT_DIR}')


if __name__ == '__main__':
    main()
