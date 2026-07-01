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


def fetch_range_akshare(code, start, end):
    """
    使用 akshare 获取个股历史数据，包括每日涨跌幅和20日/60日涨幅
    
    返回: True 成功 / False 失败
    """
    try:
        import akshare as ak
        from datetime import datetime, timedelta
        
        # 计算需要获取的起始日期（往前推70个交易日以确保有足够数据计算60日涨幅）
        try:
            s_dt = datetime.strptime(start, '%Y%m%d')
            fetch_start = (s_dt - timedelta(days=100)).strftime('%Y%m%d')
        except:
            fetch_start = start
        
        # akshare 股票历史数据
        df = ak.stock_zh_a_hist(symbol=code, period="daily", 
                                start_date=fetch_start, end_date=end,
                                adjust="qfq")
        
        if df is None or df.empty:
            print(f"akshare {code} 无数据")
            return False
        
        cache = _load_price_cache()
        with _cache_lock:
            # 存储每日涨跌幅和累计涨幅
            for i in range(len(df)):
                date_str = str(df.iloc[i]['日期']).replace('-', '')[:8]
                pct = float(df.iloc[i]['涨跌幅'])
                cache[f'{code}_{date_str}'] = round(pct, 2)
                
                # 计算20日涨幅（如果不足20日，从上市第一天算）
                try:
                    close_today = float(df.iloc[i]['收盘'])
                    idx_20d = max(0, i - 19)  # 不足20天就从第0天算
                    close_20d_ago = float(df.iloc[idx_20d]['收盘'])
                    if close_20d_ago > 0 and i > 0:  # 排除第一天
                        pct_20d = (close_today - close_20d_ago) / close_20d_ago * 100
                        cache[f'{code}_{date_str}_20d'] = round(pct_20d, 2)
                except:
                    pass
                
                # 计算60日涨幅（如果不足60日，从上市第一天算）
                try:
                    close_today = float(df.iloc[i]['收盘'])
                    idx_60d = max(0, i - 59)  # 不足60天就从第0天算
                    close_60d_ago = float(df.iloc[idx_60d]['收盘'])
                    if close_60d_ago > 0 and i > 0:  # 排除第一天
                        pct_60d = (close_today - close_60d_ago) / close_60d_ago * 100
                        cache[f'{code}_{date_str}_60d'] = round(pct_60d, 2)
                except:
                    pass
                
                # 计算10日均线并判断是否跌破
                # 需要 at least 10 天数据来计算均线
                try:
                    close_today = float(df.iloc[i]['收盘'])
                    if i >= 9:  # 有足够数据计算10日均线
                        ma10 = sum(float(df.iloc[j]['收盘']) for j in range(i-9, i+1)) / 10
                        cache[f'{code}_{date_str}_ma10'] = round(ma10, 2)
                        # 判断是否跌破10日线（收盘价 < 10日均线）
                        below_ma10 = close_today < ma10
                        cache[f'{code}_{date_str}_below_ma10'] = below_ma10
                    else:
                        # 数据不足10天，不判断
                        cache[f'{code}_{date_str}_below_ma10'] = False
                except:
                    pass
            
            _save_price_cache()
        return True
    except Exception as e:
        print(f"akshare获取{code}失败: {e}")
        return False


def fetch_range(code, start, end):
    """
    获取个股涨幅数据，使用akshare
    """
    return fetch_range_akshare(code, start, end)


def get_pct_change(code, date):
    """从缓存读取某日涨跌幅, 未命中返回 None"""
    cache = _load_price_cache()
    return cache.get(f'{code}_{date}')


def get_pct_20d(code, date):
    """从缓存读取某日20日涨幅, 未命中返回 None"""
    cache = _load_price_cache()
    return cache.get(f'{code}_{date}_20d')


def get_pct_60d(code, date):
    """从缓存读取某日60日涨幅, 未命中返回 None"""
    cache = _load_price_cache()
    return cache.get(f'{code}_{date}_60d')


def get_ma10(code, date):
    """从缓存读取某日10日均线, 未命中返回 None"""
    cache = _load_price_cache()
    return cache.get(f'{code}_{date}_ma10')


def is_below_ma10(code, date):
    """判断某日是否跌破10日线, 未命中返回 None"""
    cache = _load_price_cache()
    return cache.get(f'{code}_{date}_below_ma10')


def prefetch_prices(codes, start, end):
    """批量预取涨幅: 使用akshare获取，检查查询日期范围内是否有数据"""
    cache = _load_price_cache()
    
    # 生成查询范围内的所有日期
    from datetime import datetime, timedelta
    try:
        s_dt = datetime.strptime(start, '%Y%m%d')
        e_dt = datetime.strptime(end, '%Y%m%d')
    except:
        return
    
    query_dates = []
    current = s_dt
    while current <= e_dt:
        # 只统计工作日（简单判断：周一到周五）
        if current.weekday() < 5:
            query_dates.append(current.strftime('%Y%m%d'))
        current += timedelta(days=1)
    
    # 检查每只股票是否在查询范围内有足够数据
    need_fetch = []
    for c in codes:
        # 检查是否有任意一天的日涨幅数据
        has_any_data = any(f'{c}_{d}' in cache for d in query_dates)
        
        # 如果完全没有数据，需要获取
        if not has_any_data:
            need_fetch.append(c)
            continue
        
        # 如果有数据，检查是否缺少20日/60日涨幅
        missing_20d = any(f'{c}_{d}' in cache and f'{c}_{d}_20d' not in cache for d in query_dates)
        if missing_20d:
            need_fetch.append(c)
    
    if not need_fetch:
        print(f"所有 {len(codes)} 只股票涨幅数据已缓存")
        return
    
    print(f"需要获取 {len(need_fetch)} 只股票的涨幅数据...")
    import time
    success = 0
    for i, c in enumerate(need_fetch):
        try:
            if fetch_range(c, start, end):
                success += 1
            # 降低请求频率，避免被封IP
            time.sleep(0.5)
        except Exception as e:
            print(f"获取 {c} 失败: {e}")
            time.sleep(1)  # 失败后多等一会
        if (i + 1) % 20 == 0:
            print(f"已处理 {i + 1}/{len(need_fetch)}，成功 {success}")
    
    print(f"完成: 成功获取 {success}/{len(need_fetch)} 只股票数据")


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
            pct_20d = get_pct_20d(code, d) if with_price else None
            pct_60d = get_pct_60d(code, d) if with_price else None
            below_ma10 = is_below_ma10(code, d) if with_price else None
            ma10 = get_ma10(code, d) if with_price else None
            if rec:
                track[d] = {
                    'desc': make_desc(rec['连扳数'], rec['末次时间']),
                    'lianban': rec['连扳数'],
                    'time': rec['末次时间'],
                    'reason': rec['原因'],
                    'pct': pct,
                    'pct_20d': pct_20d,
                    'pct_60d': pct_60d,
                    'ma10': ma10,
                    'below_ma10': below_ma10,
                    'present': True,
                }
            else:
                track[d] = {
                    'present': False, 
                    'pct': pct,
                    'pct_20d': pct_20d,
                    'pct_60d': pct_60d,
                    'ma10': ma10,
                    'below_ma10': below_ma10
                }
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
    block_removed_stocks = {}  # block -> {code: remove_date} 被移除的股票及其移除日期
    for blk in selected_blocks:
        block_cum_stocks[blk] = []
        block_seen_codes[blk] = set()
        block_removed_stocks[blk] = {}

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
            
            # 检查每只股票是否跌破10日线，标记移除状态
            for st in cum:
                track = st['track'].get(d)
                if track and track.get('below_ma10') == True:
                    # 检查是否涨停（涨停则不移除）
                    pct = track.get('pct')
                    is_limit_up = False
                    if pct is not None:
                        if st['is_kcb_cyb']:
                            is_limit_up = pct >= 19.4  # 科创板/创业板涨停
                        else:
                            is_limit_up = pct >= 9.8   # 普通股票涨停
                    
                    # 检查是否一字板（9:25涨停）
                    desc = track.get('desc', '')
                    if '一字板' in desc:
                        is_limit_up = True
                    
                    # 跌破10日线且未涨停，记录移除日期
                    if not is_limit_up:
                        if st['code'] not in block_removed_stocks[blk]:
                            block_removed_stocks[blk][st['code']] = d
                            st['remove_date'] = d
                            st['remove_reason'] = '跌破10日线'
            
            # 计算当前仍在跟踪的股票（未被移除或在移除当天仍显示）
            active_stocks = []
            removed_today = []
            for st in cum:
                remove_date = st.get('remove_date')
                if remove_date is None:
                    # 未被移除，继续跟踪
                    active_stocks.append(st)
                elif remove_date == d:
                    # 当天移除，仍然显示但标记为已移除
                    active_stocks.append(st)
                    removed_today.append(st['code'])
                # else: 已移除且不是当天，不再显示
            
            # 计算板块是否即将移除（所有股票都已移除）
            all_removed = len(cum) > 0 and len(active_stocks) == len([st for st in cum if st.get('remove_date')])
            block_removing = all_removed and len(removed_today) > 0
            
            # 任何入选过的板块每天都显示(含累积阵容为空的早期日期), 保证列纵向对齐
            new_today = sum(1 for st in cum if st['first_date'] == d)
            day_blocks.append({
                'block': blk,
                'cum_count': len(cum),               # 截至当天累计入选票数
                'active_count': len(active_stocks),  # 当前仍在跟踪的票数
                'new_count': new_today,              # 当天新入选票数
                'removed_today': removed_today,      # 当天移除的股票代码列表
                'times_count': block_times[blk],     # 板块累计出现次数(全局)
                'total_count': block_total[blk],     # 板块累计票数(全局)
                'active': blk in daily[d],           # 当天该板块是否在复盘中出现
                'removing': block_removing,          # 板块是否即将移除
                'stocks': [dict(st) for st in active_stocks],
            })
        if day_blocks:
            by_date.append({'date': d, 'blocks': day_blocks})

    # === 板块图表汇总数据 ===
    # 每个板块: 逐日平均涨幅 / 逐日累计平均涨幅 / 逐日累计入选票数 / 各入选票逐日涨幅序列
    blocks_summary = []
    for blk in selected_blocks:
        roster = block_cum_stocks[blk]
        if not roster:
            continue
        avg_series = {}          # 单日平均涨幅
        cum_avg_series = {}      # 累计平均涨幅
        cum_series = {}
        for d in dates:
            # 截至当天累计入选的票
            cum_stocks = [st for st in roster if st['first_date'] <= d]
            cum_series[d] = len(cum_stocks)
            # 单日平均涨幅
            pcts = [st['track'][d].get('pct') for st in cum_stocks
                    if isinstance(st['track'][d].get('pct'), (int, float))]
            avg_series[d] = round(sum(pcts) / len(pcts), 2) if pcts else None
        # 计算累计平均涨幅
        cum_sum = 0
        cum_count = 0
        for d in dates:
            if avg_series[d] is not None:
                cum_sum += avg_series[d]
                cum_count += 1
            cum_avg_series[d] = round(cum_sum, 2) if cum_count > 0 else None
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
            'avg_pct': cum_avg_series,   # 改为累计平均涨幅
            'daily_avg_pct': avg_series, # 单日平均涨幅（保留）
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
