#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
热门股追踪 - 数据统计核心模块

数据源: excelDataSource/<日期>_涨停复盘_*.xlsx 的「04提取」sheet
列: 概念板块 | 代码 | 名称 | 末次时间 | 连扳数 | 原因

规则:
  统计范围: 仅概念板块, 排除「市场连板股」
  板块入选(满足任一即加入显示列表, 首次满足后一直保留):
      - 板块内存在 连板>=2 的票
      - 板块内存在 "1/X板" 格式的票 (如 1/3天2板)
      - 板块内存在 9:25 涨停(一字板)的票
  个股入选(已入选板块下, 满足任一):
      - "1/X板" 格式
      - 9:25 涨停
      - 300/688 开头的 1 板
  入选个股每日跟踪: 在日期范围内逐日展示其连板进展/涨幅
  排序: 可切换 'stock_count'(板块内入选票数) / 'days'(板块出现天数)
"""

import sys
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

import os
import re
import glob
import json

import openpyxl


EXCEL_DIR = 'excelDataSource'
EXCLUDED_BLOCKS = {'市场连板股'}
# 04提取 sheet 末尾的校验说明行, 解析时跳过
_META_FIRST_COL = {'【校验结果】', '问题1', '问题2', '问题3', '问题4', '建议', '说明'}


# ============== Excel 解析 ==============

def _date_from_filename(fp):
    """从文件名提取 8 位日期, 如 20260601_涨停复盘_verified.xlsx -> 20260601"""
    m = re.search(r'(\d{8})', os.path.basename(fp))
    return m.group(1) if m else None


def list_excel_dates(excel_dir=EXCEL_DIR):
    """返回 {日期: 文件路径}, 同一日期取任意一个(verified优先)"""
    mapping = {}
    for fp in glob.glob(os.path.join(excel_dir, '*_涨停复盘*.xlsx')):
        if os.path.basename(fp).startswith('~$'):
            continue  # Excel 临时锁文件
        d = _date_from_filename(fp)
        if not d:
            continue
        # verified 优先于 manualcheck
        if d not in mapping or 'verified' in os.path.basename(fp):
            mapping[d] = fp
    return mapping


def parse_excel_04(fp):
    """
    解析单个 Excel 的 04提取 sheet

    返回: list[dict] 每条 {概念板块, 代码, 名称, 末次时间, 连扳数, 原因}
          已排除末尾校验说明行
    """
    wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
    if '04提取' not in wb.sheetnames:
        return []
    ws = wb['04提取']
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r or not r[0]:
            continue
        block = str(r[0]).strip()
        if block in _META_FIRST_COL:
            continue
        code = str(r[1]).strip() if len(r) > 1 and r[1] is not None else ''
        # 必须是 6 位股票代码才算有效个股行
        if not re.match(r'^\d{6}$', code):
            continue
        rows.append({
            '概念板块': block,
            '代码': code,
            '名称': str(r[2]).strip() if len(r) > 2 and r[2] is not None else '',
            '末次时间': str(r[3]).strip() if len(r) > 3 and r[3] is not None else '',
            '连扳数': str(r[4]).strip() if len(r) > 4 and r[4] is not None else '',
            '原因': str(r[5]).strip() if len(r) > 5 and r[5] is not None else '',
        })
    wb.close()
    return rows


# ============== 连扳数解析 ==============

def parse_lianban(lianban):
    """
    解析连扳数字段, 返回结构化信息

    输入示例: '1' / '2' / '3' / '1/18天9板' / '1/3天2板' / '3/7天5板'

    返回: {
        'current': int,      # 当前连板数 (斜杠前的数字, 纯数字则为其本身)
        'is_ratio': bool,    # 是否为 "X/Y天Z板" 断板回封格式
        'days': int|None,    # 周期天数 Y
        'period_count': int|None,  # 周期内涨停次数 Z
        'raw': str,
    }
    """
    raw = (lianban or '').strip()
    info = {'current': 0, 'is_ratio': False, 'days': None,
            'period_count': None, 'raw': raw}
    if not raw:
        return info
    # X/Y天Z板
    m = re.match(r'^(\d+)\s*/\s*(\d+)\s*天\s*(\d+)\s*板', raw)
    if m:
        info['current'] = int(m.group(1))
        info['is_ratio'] = True
        info['days'] = int(m.group(2))
        info['period_count'] = int(m.group(3))
        return info
    # 纯数字
    m = re.match(r'^(\d+)', raw)
    if m:
        info['current'] = int(m.group(1))
    return info


def is_925(last_time):
    """末次时间是否为 9:25 (开盘一字板)"""
    t = (last_time or '').strip().replace('：', ':')
    return t in ('9:25', '09:25', '9:25:00')


def is_kcb_cyb(code):
    """是否 300(创业板)/688(科创板) 开头"""
    return code.startswith('300') or code.startswith('688')


def make_desc(lianban, last_time):
    """
    生成个股描述
    优先级: 9:25一字板 > 1/X板格式原样 > N连板
    """
    info = parse_lianban(lianban)
    parts = []
    if is_925(last_time):
        parts.append('9:25一字板')
    if info['is_ratio']:
        parts.append(info['raw'])  # 如 1/3天2板
    elif info['current'] >= 1:
        parts.append(f"{info['current']}连板")
    return ' '.join(parts) if parts else (lianban or '')


# ============== 入选判定 ==============

def block_qualifies(stocks):
    """
    板块是否入选: 板块内任一票满足 连板>=2 / 1-X板格式 / 9:25涨停
    stocks: 该板块当日的票列表
    """
    for s in stocks:
        info = parse_lianban(s['连扳数'])
        if info['current'] >= 2:
            return True
        if info['is_ratio']:
            return True
        if is_925(s['末次时间']):
            return True
    return False


def stock_qualifies(stock):
    """
    个股是否入选(在已入选板块下):
    1/X板格式 / 9:25涨停 / 300或688开头的1板
    """
    info = parse_lianban(stock['连扳数'])
    if info['is_ratio']:
        return True
    if is_925(stock['末次时间']):
        return True
    if is_kcb_cyb(stock['代码']) and info['current'] >= 1:
        return True
    # 2连板及以上也属于值得跟踪
    if info['current'] >= 2:
        return True
    return False


def daterange_keys(all_dates, start, end):
    """返回 [start,end] 区间内、且存在Excel的日期, 升序"""
    return sorted(d for d in all_dates if start <= d <= end)


# ============== 涨幅获取 (腾讯行情接口 + 本地缓存) ==============
# 说明: 东方财富(akshare默认源)在当前网络环境直连被重置、走代理也不通,
#       改用腾讯行情接口 web.ifzq.gtimg.cn, 直连稳定可用。
#       涨跌幅 = (当日收盘 - 前一日收盘) / 前一日收盘 * 100

_PRICE_CACHE_FILE = '.price_cache.json'
_price_cache = None

import threading
_cache_lock = threading.Lock()

_TX_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36',
}


def _load_price_cache():
    global _price_cache
    if _price_cache is None:
        if os.path.exists(_PRICE_CACHE_FILE):
            try:
                with open(_PRICE_CACHE_FILE, 'r', encoding='utf-8') as f:
                    _price_cache = json.load(f)
            except Exception:
                _price_cache = {}
        else:
            _price_cache = {}
    return _price_cache


def _save_price_cache():
    try:
        with open(_PRICE_CACHE_FILE, 'w', encoding='utf-8') as f:
            json.dump(_price_cache, f, ensure_ascii=False)
    except Exception:
        pass


def _tx_secid(code):
    """股票代码 -> 腾讯行情 secid 前缀"""
    if code.startswith(('6', '9', '5')):
        return 'sh' + code
    return 'sz' + code


def fetch_range(code, start, end):
    """
    获取个股 [start,end] 区间每个交易日涨跌幅, 写入缓存
    用腾讯行情日K(前复权), 多取前置交易日以计算首日涨幅

    返回: True 成功(至少取到数据) / False 失败
    """
    import requests
    from datetime import datetime, timedelta

    cache = _load_price_cache()
    # 起始日往前推 12 个自然日, 确保拿到 start 前一交易日用于算涨幅
    try:
        s_dt = datetime.strptime(start, '%Y%m%d')
        fetch_start = (s_dt - timedelta(days=12)).strftime('%Y-%m-%d')
    except Exception:
        fetch_start = start[:4] + '-' + start[4:6] + '-' + start[6:8]
    fetch_end = end[:4] + '-' + end[4:6] + '-' + end[6:8]

    secid = _tx_secid(code)
    url = (f'https://web.ifzq.gtimg.cn/appstock/app/fqkline/get'
           f'?param={secid},day,{fetch_start},{fetch_end},640,qfq')
    for attempt in range(2):
        try:
            r = requests.get(url, headers=_TX_HEADERS, timeout=10,
                             proxies={'http': None, 'https': None})
            j = r.json()
            data = j.get('data', {}).get(secid, {})
            kline = data.get('qfqday') or data.get('day') or []
            if not kline:
                return False
            # kline: [日期, 开, 收, 高, 低, 成交量]; 按日期算环比涨跌幅
            prev_close = None
            with _cache_lock:
                for row in kline:
                    d = row[0].replace('-', '')   # 20260601
                    close = float(row[2])
                    if prev_close is not None and prev_close > 0:
                        pct = round((close - prev_close) / prev_close * 100, 2)
                        cache[f'{code}_{d}'] = pct
                    prev_close = close
                _save_price_cache()
            return True
        except Exception:
            if attempt == 0:
                import time
                time.sleep(0.8)
                continue
    return False


def get_pct_change(code, date):
    """从缓存读取某日涨跌幅, 未命中返回 None"""
    cache = _load_price_cache()
    return cache.get(f'{code}_{date}')


def prefetch_prices(codes, start, end):
    """批量预取涨幅: 并发请求(腾讯接口), 已有缓存的票跳过, 失败的串行补取一次"""
    from concurrent.futures import ThreadPoolExecutor
    cache = _load_price_cache()
    todo = [c for c in codes
            if not any(k.startswith(f'{c}_') for k in cache)]
    if not todo:
        return
    # 并发拉取(限制并发数避免被限流)
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(lambda c: fetch_range(c, start, end), todo))
    # 并发中失败的票, 串行补取一次(规避偶发限流)
    failed = [c for c, ok in zip(todo, results) if not ok]
    for c in failed:
        fetch_range(c, start, end)


# ============== 主统计逻辑 ==============

def track_hot_stocks(start, end, sort='stock_count', with_price=True,
                     excel_dir=EXCEL_DIR):
    """
    热门股追踪主函数

    参数:
        start, end: 8位日期字符串 YYYYMMDD
        sort: 'stock_count'(按板块内入选票数) | 'days'(按板块出现天数)
        with_price: 是否获取涨幅(akshare)

    返回: dict
        {
          'start','end','sort',
          'dates': [日期...],            # 区间内有数据的交易日
          'by_date': [                   # 按日期分组
            {
              'date': '20260601',
              'blocks': [                # 当日"新加入"的板块
                {
                  'block': 板块名,
                  'new_count': 当日新增入选票数,
                  'total_count': 板块累计入选票数,
                  'stocks': [
                    {'code','name','is_kcb_cyb','first_date',
                     'track': {'20260601': {'desc','pct','present'}, ...}}
                  ]
                }
              ]
            }
          ]
        }
    排序由前端按 new_count / total_count 实时切换, 不需重新请求
    """
    all_files = list_excel_dates(excel_dir)
    dates = daterange_keys(all_files.keys(), start, end)

    # 逐日解析, 构建 {日期: {板块: [票...]}}
    daily = {}  # date -> block -> list[stock]
    for d in dates:
        rows = parse_excel_04(all_files[d])
        bmap = {}
        for s in rows:
            blk = s['概念板块']
            if blk in EXCLUDED_BLOCKS:
                continue
            bmap.setdefault(blk, []).append(s)
        daily[d] = bmap

    # === 板块入选: 逐日扫描, 首次满足即加入(保留) ===
    selected_blocks = {}   # block -> first_date
    for d in dates:
        for blk, stocks in daily[d].items():
            if blk in selected_blocks:
                continue
            if block_qualifies(stocks):
                selected_blocks[blk] = d

    # === 个股入选 + 逐日跟踪 ===
    # 先收集所有入选板块的入选个股代码, 批量预取涨幅
    if with_price:
        all_codes = set()
        for blk in selected_blocks:
            for d in dates:
                for s in daily[d].get(blk, []):
                    if stock_qualifies(s):
                        all_codes.add(s['代码'])
        if all_codes and dates:
            prefetch_prices(all_codes, dates[0], dates[-1])

    def build_track(blk, code):
        """构建某票在某板块下逐日跟踪"""
        track = {}
        for d in dates:
            rec = None
            for s in daily[d].get(blk, []):
                if s['代码'] == code:
                    rec = s
                    break
            pct = get_pct_change(code, d) if with_price else None
            if rec:
                track[d] = {
                    'desc': make_desc(rec['连扳数'], rec['末次时间']),
                    'lianban': rec['连扳数'],
                    'time': rec['末次时间'],
                    'reason': rec['原因'],
                    'pct': pct,
                    'present': True,
                }
            else:
                track[d] = {'present': False, 'pct': pct}
        return track

    # === 按日期分组: 每个日期显示截至当天累计入选的板块及其完整阵容 ===
    # 全局指标(整个区间, 用于一致排序):
    #   block_total: 累计入选票数
    #   block_times: 累计出现次数(区间内该板块出现的天数)
    block_total = {}      # block -> 累计入选票数
    block_times = {}      # block -> 累计出现天数
    for blk in selected_blocks:
        codes = set()
        for d in dates:
            for s in daily[d].get(blk, []):
                if stock_qualifies(s):
                    codes.add(s['代码'])
        block_total[blk] = len(codes)
        block_times[blk] = sum(1 for d in dates if blk in daily[d])

    by_date = []
    # 累积每个板块已入选的票(跨日累加), 实现"每天看完整阵容"
    block_cum_stocks = {}   # block -> list[stock_item] (按入选顺序)
    block_seen_codes = {}   # block -> set(code)
    for blk in selected_blocks:
        block_cum_stocks[blk] = []
        block_seen_codes[blk] = set()

    for d in dates:
        day_blocks = []
        for blk in selected_blocks:
            # 把当天该板块"新入选"的票累加进累积列表
            for s in daily[d].get(blk, []):
                code = s['代码']
                if code in block_seen_codes[blk]:
                    continue
                if stock_qualifies(s):
                    block_seen_codes[blk].add(code)
                    block_cum_stocks[blk].append({
                        'code': code,
                        'name': s['名称'],
                        'is_kcb_cyb': is_kcb_cyb(code),
                        'first_date': d,
                        'track': build_track(blk, code),
                    })
            cum = block_cum_stocks[blk]
            # 任何入选过的板块每天都显示(含累积阵容为空的早期日期), 保证列纵向对齐
            new_today = sum(1 for st in cum if st['first_date'] == d)
            day_blocks.append({
                'block': blk,
                'cum_count': len(cum),               # 截至当天累计入选票数
                'new_count': new_today,              # 当天新入选票数
                'times_count': block_times[blk],     # 板块累计出现次数(全局)
                'total_count': block_total[blk],     # 板块累计票数(全局)
                'active': blk in daily[d],           # 当天该板块是否在复盘中出现
                'stocks': [dict(st) for st in cum],
            })
        if day_blocks:
            by_date.append({'date': d, 'blocks': day_blocks})

    # === 板块图表汇总数据 ===
    # 每个板块: 逐日平均涨幅 / 逐日累计入选票数 / 各入选票逐日涨幅序列
    blocks_summary = []
    for blk in selected_blocks:
        roster = block_cum_stocks[blk]
        if not roster:
            continue
        avg_series = {}
        cum_series = {}
        for d in dates:
            # 截至当天累计入选的票
            cum_stocks = [st for st in roster if st['first_date'] <= d]
            cum_series[d] = len(cum_stocks)
            pcts = [st['track'][d].get('pct') for st in cum_stocks
                    if isinstance(st['track'][d].get('pct'), (int, float))]
            avg_series[d] = round(sum(pcts) / len(pcts), 2) if pcts else None
        stocks_series = [{
            'code': st['code'],
            'name': st['name'],
            'first_date': st['first_date'],
            'pct': {d: st['track'][d].get('pct') for d in dates},
        } for st in roster]
        blocks_summary.append({
            'block': blk,
            'times_count': block_times[blk],
            'total_count': block_total[blk],
            'avg_pct': avg_series,
            'cum_count': cum_series,
            'stocks': stocks_series,
        })

    return {
        'start': start,
        'end': end,
        'sort': sort,
        'dates': dates,
        'by_date': by_date,
        'blocks_summary': blocks_summary,
    }


if __name__ == '__main__':
    # 命令行自测: py hot_track.py 20260601 20260605
    start = sys.argv[1] if len(sys.argv) > 1 else '20260601'
    end = sys.argv[2] if len(sys.argv) > 2 else '20260605'
    data = track_hot_stocks(start, end, with_price=False)
    print(f"日期范围: {start}~{end}, 有数据日: {data['dates']}\n")
    for day in data['by_date']:
        print(f"【{day['date']}】 新增板块 {len(day['blocks'])} 个")
        for b in day['blocks']:
            print(f"  [{b['block']}] 当日新增{b['new_count']}票 累计{b['total_count']}票")
            for s in b['stocks']:
                seq = [f"{d[4:]}:{s['track'][d]['desc']}"
                       for d in data['dates'] if s['track'][d]['present']]
                print(f"      {s['name']}({s['code']}) {' | '.join(seq)}")
        print()
