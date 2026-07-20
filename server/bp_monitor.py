# -*- coding: utf-8 -*-
"""server.bp_monitor - 盯盘: 指数/个股/概念行情 + 产业链实时看板。

原 api_server.py 行 1776-2306 + 2315-3639 + 6193-6308 (chain-board迁回)。
"""
import os
import re
import json
import time
import math
import struct
import threading
from datetime import datetime, timedelta
from collections import defaultdict
from flask import Blueprint, request, jsonify

from .common import (
    ht,  # noqa: F401
    app,
    socketio,
    DEBUG_MODE,
    SERVER_STARTED_AT,
    SERVER_INSTANCE_ID,
    _load_concept_members,
    _read_lc1_minutes_batch,
    _limit_threshold,
)

monitor_bp = Blueprint('monitor', __name__)


def monitor_page():
    """盯盘页面: 上证指数日K + 分时图"""
    return render_template(
        'monitor.html',
        debug_mode=DEBUG_MODE,
        server_instance_id=SERVER_INSTANCE_ID if DEBUG_MODE else '',
    )



def debug_runtime():
    """仅 Debug 模式开放：浏览器诊断器用于识别旧 Flask 进程/旧模板。"""
    if not DEBUG_MODE:
        return jsonify({'success': False, 'error': 'debug mode disabled'}), 404

    def file_info(path):
        try:
            stat = os.stat(path)
            return {'path': path, 'mtime': stat.st_mtime, 'size': stat.st_size}
        except OSError:
            return {'path': path, 'missing': True}

    return jsonify({
        'success': True,
        'debug': True,
        'pid': os.getpid(),
        'instance_id': SERVER_INSTANCE_ID,
        'started_at': SERVER_STARTED_AT,
        'files': {
            'api_server': file_info('api_server.py'),
            'monitor_template': file_info(os.path.join('templates', 'monitor.html')),
        },
    })



@monitor_bp.route('/api/monitor/health', methods=['GET'])

def monitor_health():
    """盯盘健康检查: 诊断 tqcenter / 通达信路径 / mootdx 连通性。
    不持 _tdx_lock、不调 mootdx 客户端(避免自身被卡死), 用独立线程+超时探测。"""
    import time as _time
    import socket as _socket
    import threading
    result = {'ts': _time.time()}

    # 1. 通达信路径 + tqcenter 状态 (从 tdx_source 拿, 不触发 mootdx)
    try:
        import tdx_source as ts
        result['tdx'] = ts.get_tdx_info()
    except Exception as e:
        result['tdx'] = {'error': f'{type(e).__name__}: {e}'}

    # 2. mootdx 远程服务器连通性 (独立线程+5s超时, 不建 mootdx 客户端)
    def _probe_servers():
        servers = [
            ('218.75.126.9', 7709),
            ('115.238.90.165', 7709),
            ('115.238.56.198', 7709),
            ('124.70.199.56', 7709),
        ]
        reachable = []
        for ip, port in servers:
            try:
                _sk = _socket.create_connection((ip, port), timeout=2)
                _sk.close()
                reachable.append(ip)
            except Exception:
                pass
        return reachable
    _probe_result = [None]
    _pt = threading.Thread(target=lambda: _probe_result.__setitem__(0, _probe_servers()), daemon=True)
    _pt.start()
    _pt.join(timeout=8)
    result['mootdx'] = {
        'remote_servers_reachable': _probe_result[0] if _probe_result[0] is not None else [],
        'probe_timeout': _probe_result[0] is None,
    }

    # 3. 综合判断
    tq_ok = result.get('tdx', {}).get('tqcenter_available', False)
    mootdx_ok = bool(result['mootdx']['remote_servers_reachable'])
    result['status'] = 'ok' if (tq_ok or mootdx_ok) else 'error'
    result['tqcenter_ok'] = tq_ok
    result['mootdx_ok'] = mootdx_ok
    return jsonify(result)



# 分时时间轴生成 (9:30-11:30, 13:00-15:00 共240个点)

def _minute_time_axis(n):
    """生成 n 个分时时间点 (HH:MM)。
    A股连续竞价: 9:30-11:30 (120分钟) + 13:00-15:00 (120分钟) = 240点。
    集合竞价 9:25-9:29 的数据由前端固定轴补齐(后端 mootdx minute 不含集合竞价段)。"""
    from datetime import datetime, timedelta
    times = []
    am_start = datetime(2026, 1, 1, 9, 30)
    pm_start = datetime(2026, 1, 1, 13, 0)
    for i in range(min(n, 240)):
        if i < 120:
            t = am_start + timedelta(minutes=i)
        else:
            t = pm_start + timedelta(minutes=i - 120)
        times.append(t.strftime('%H:%M'))
    return times



@monitor_bp.route('/api/monitor/index/daily', methods=['GET'])

def monitor_index_daily():
    """上证指数日K线
    优先 tqcenter index_kline (客户端开着最稳定, 走DLL不封IP), 回退 mootdx index_bars。
    参数: count=120 (取最近N根日K)
    """
    try:
        count = request.args.get('count', default=120, type=int)
        count = max(20, min(count, 800))
        # 优先 tqcenter (带 3s 超时保护)
        try:
            import tdx_source as ts
            if ts.is_available():
                import threading
                _bars_result = [None]
                def _fetch_tq_bars():
                    try:
                        _bars_result[0] = ts.index_kline('000001.SH', count=count, period='1d')
                    except Exception:
                        pass
                _t = threading.Thread(target=_fetch_tq_bars, daemon=True)
                _t.start()
                _t.join(timeout=3.0)
                bars = _bars_result[0]
                if bars and len(bars) >= 5:
                    return jsonify({'success': True, 'bars': bars, 'source': 'tqcenter'})
        except Exception as e:
            print(f'[TQ] 指数K线异常: {e}')
        # 回退 mootdx index_bars (带应用层超时, 防止卡死)
        import hot_track as ht
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            df = ht._tdx_call_with_timeout(client, 'index_bars', timeout=8,
                                           symbol='000001', frequency=9, start=0, offset=count)
        if df is not None and not df.empty:
            bars = []
            for _, row in df.iterrows():
                bars.append({
                    'date': str(row['datetime'])[:10],
                    'open': round(float(row['open']), 2),
                    'close': round(float(row['close']), 2),
                    'high': round(float(row['high']), 2),
                    'low': round(float(row['low']), 2),
                    'vol': float(row['vol']),
                    'amount': float(row['amount']),
                })
            return jsonify({'success': True, 'bars': bars, 'source': 'mootdx'})
        return jsonify({'success': False, 'error': '无数据'}), 404
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



_minute_cache = {'ts': 0, 'data': None}  # 大盘分时缓存

_minute_fail_streak = 0  # 连续失败次数(后端侧)

_minute_cache_ttl = 3    # 动态缓存时间(秒), 失败时自动增大


@monitor_bp.route('/api/monitor/index/minute', methods=['GET'])

def monitor_index_minute():
    """上证指数当日分时 (mootdx minute, 代码用 1A0001)。自适应缓存。"""
    import time as _time
    global _minute_fail_streak, _minute_cache_ttl
    # 动态缓存: 正常3s, 连续失败后自动增大到15s(减轻服务器压力)
    if _minute_cache['data'] and (_time.time() - _minute_cache['ts'] < _minute_cache_ttl):
        return jsonify(_minute_cache['data'])
    try:
        import hot_track as ht
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            df = ht._tdx_call_with_timeout(client, 'minute', timeout=8, symbol='1A0001')
        if df is None or df.empty:
            return jsonify({'success': False, 'error': '无当日分时数据(可能非交易时段)'}), 404
        times = _minute_time_axis(len(df))
        points = []
        for i, row in df.iterrows():
            points.append({
                'time': times[i] if i < len(times) else str(i),
                'price': round(float(row['price']), 2),
                'vol': float(row['vol']),
            })
        result = {'success': True, 'points': points, 'date': 'today', 'source': 'mootdx'}
        # tqcenter 补充: 涨跌幅/上涨下跌家数/均价(领先指标) - 超时1秒防止卡死
        try:
            import tdx_source as ts
            if ts.is_available():
                import threading
                _idx_result = [None]
                def _fetch_idx():
                    try:
                        _idx_result[0] = ts.index_snapshot()
                    except Exception:
                        pass
                _t = threading.Thread(target=_fetch_idx, daemon=True)
                _t.start()
                _t.join(timeout=1.0)
                idx = _idx_result[0]
                if idx:
                    result['index_price'] = idx['price']
                    result['index_pct'] = idx['pct']
                    result['up_home'] = idx['up_home']
                    result['down_home'] = idx['down_home']
                    result['average'] = idx['average']
        except Exception:
            pass
        _minute_cache['data'] = result
        _minute_cache['ts'] = _time.time()
        # 成功: 恢复正常缓存时间
        _minute_fail_streak = 0
        _minute_cache_ttl = 3
        return jsonify(result)
    except Exception as e:
        # 失败: 增大缓存时间, 减轻服务器压力
        _minute_fail_streak += 1
        if _minute_fail_streak >= 2:
            _minute_cache_ttl = 15
        # 连接异常: 尝试切换服务器
        try:
            import hot_track as ht
            with ht._get_tdx_lock():
                ht._reconnect_tdx()
        except Exception:
            pass
        # 缓存还有数据就返回旧数据(降级服务), 避免前端空白
        if _minute_cache['data']:
            return jsonify(_minute_cache['data'])
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



@monitor_bp.route('/api/monitor/index/minutes', methods=['GET'])

def monitor_index_minutes():
    """上证指数历史分时 (mootdx minutes, 代码用 1A0001)
    参数: date=20260703 (YYYYMMDD)
    """
    date = request.args.get('date', '')
    if not re.match(r'^\d{8}$', date):
        return jsonify({'success': False, 'error': 'date 参数需为 YYYYMMDD'}), 400
    try:
        import hot_track as ht
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            df = ht._tdx_call_with_timeout(client, 'minutes', timeout=8, symbol='1A0001', date=int(date))
        if df is None or df.empty:
            return jsonify({'success': False, 'error': f'{date} 无分时数据'}), 404
        times = _minute_time_axis(len(df))
        points = []
        for i, row in df.iterrows():
            points.append({
                'time': times[i] if i < len(times) else str(i),
                'price': round(float(row['price']), 2),
                'vol': float(row['vol']),
            })
        # 格式化日期显示
        date_fmt = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
        return jsonify({'success': True, 'points': points, 'date': date_fmt, 'source': 'mootdx'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



@monitor_bp.route('/api/monitor/index/leading', methods=['GET'])

def monitor_index_leading():
    """上证领先指标(小盘不加权平均): 拉全市场A股实时报价算算术平均涨跌幅。
    返回 leading_pct(领先指标涨跌幅%) 和 index_last_close(上证昨收),
    前端用 index_last_close * (1+leading_pct/100) 换算黄线点位。
    60秒缓存避免频繁拉取全市场报价(单次约2s)。"""
    global _leading_cache
    import time as _time
    if '_leading_cache' not in globals():
        _leading_cache = None
    if _leading_cache and (_time.time() - _leading_cache['ts'] < 60):
        return jsonify(_leading_cache['data'])
    import re as _re
    try:
        import hot_track as ht
        lock = ht._get_tdx_lock()
        with lock:
            client = ht._get_tdx_client()
            # 取沪深全部股票代码(缓存1小时, 代码列表变化少)
            global _astock_codes_cache
            if '_astock_codes_cache' not in globals():
                _astock_codes_cache = None
            if _astock_codes_cache and (_time.time() - _astock_codes_cache['ts'] < 3600):
                codes = _astock_codes_cache['codes']
            else:
                sh = ht._tdx_call_with_timeout(client, 'stocks', timeout=8, market=1)
                sz = ht._tdx_call_with_timeout(client, 'stocks', timeout=8, market=0)
                if sh is None or sz is None:
                    return jsonify({'success': False, 'error': '获取股票列表超时'}), 504
                def _is_a(code):
                    return _re.match(r'^(60|00|30|68)\d{4}$', str(code)) is not None
                codes = [c for c in sh['code'].tolist() if _is_a(c)] + \
                        [c for c in sz['code'].tolist() if _is_a(c)]
                _astock_codes_cache = {'codes': codes, 'ts': _time.time()}
            # 批量拉报价(每次最多80只)
            import pandas as pd
            frames = []
            for i in range(0, len(codes), 80):
                batch = codes[i:i + 80]
                df = ht._tdx_call_with_timeout(client, 'quotes', timeout=8, symbol=batch)
                if df is not None and not df.empty:
                    frames.append(df)
            if not frames:
                return jsonify({'success': False, 'error': '无报价数据'}), 404
            all_q = pd.concat(frames, ignore_index=True)
            valid = all_q[(all_q['last_close'] > 0) & (all_q['price'] > 0)]
            if valid.empty:
                return jsonify({'success': False, 'error': '无有效报价'}), 404
            pct = ((valid['price'] - valid['last_close']) / valid['last_close'] * 100).mean()
            # 取上证指数昨收(用于前端换算黄线点位)
            idx_q = ht._tdx_call_with_timeout(client, 'quotes', timeout=5, symbol='1A0001')
            idx_last_close = float(idx_q['last_close'].iloc[0]) if idx_q is not None and not idx_q.empty else 0
        result = {
            'success': True,
            'leading_pct': round(float(pct), 3),
            'index_last_close': round(idx_last_close, 2),
            'source': 'mootdx',
        }
        _leading_cache = {'data': result, 'ts': _time.time()}
        return jsonify(result)
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



# ===== 个股相关接口 (盯盘第二列) =====


_stock_list_cache = {}  # {(segment, sort): {'data':..., 'ts':...}}

_all_quotes_cache = None  # 全市场A股报价缓存(已废弃, 改用分板块缓存)

_segment_quotes_cache = {}  # {'cyb': {'codes':[], 'names':{}, 'data':[], 'codes_ts':.., 'ts':..}, 'kcb': {...}}

_segment_codes_cache = {}  # 板块代码列表缓存(1小时)



def _get_segment_codes(segment):
    """获取板块的股票代码和名称。优先 tqcenter -> 本地文件缓存 -> mootdx。"""
    import re as _re
    import time as _time
    import os as _os
    import json as _json
    cached = _segment_codes_cache.get(segment)
    if cached and (_time.time() - cached['ts'] < 3600):
        return cached['codes'], cached['names']
    # 本地文件缓存(新股上市才会变化, 每天更新一次足够)
    cache_file = _os.path.join(_os.path.dirname(__file__), f'_codes_{segment}.json')
    if _os.path.exists(cache_file):
        mtime = _os.path.getmtime(cache_file)
        if _time.time() - mtime < 86400:  # 24小时内有效
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = _json.load(f)
            codes = data['codes']
            names = data['names']
            _segment_codes_cache[segment] = {'codes': codes, 'names': names, 'ts': _time.time()}
            return codes, names
    # 优先 tqcenter (走自己账号, 不封IP)
    try:
        import tdx_source as ts
        if ts.is_available():
            codes, names = ts.segment_codes(segment)
            if codes:
                _segment_codes_cache[segment] = {'codes': codes, 'names': names, 'ts': _time.time()}
                try:
                    with open(cache_file, 'w', encoding='utf-8') as f:
                        _json.dump({'codes': codes, 'names': names}, f, ensure_ascii=False)
                except Exception:
                    pass
                return codes, names
    except Exception as e:
        print(f'[TQ] 板块代码 {segment} 异常: {e}')
    # 回退 mootdx
    import hot_track as ht
    with ht._get_tdx_lock():
        client = ht._get_tdx_client()
        if segment == 'cyb':
            # 创业板300开头, 在深市market=0
            sz = ht._tdx_call_with_timeout(client, 'stocks', timeout=8, market=0)
            if sz is None:
                return [], {}
            codes = [c for c in sz['code'].tolist() if _re.match(r'^30\d{4}$', str(c))]
            names = {}
            for _, r in sz.iterrows():
                if _re.match(r'^30\d{4}$', str(r['code'])):
                    names[str(r['code'])] = str(r.get('name', '')).replace('\x00', '').strip()
        else:
            # 科创板688开头, 在沪市market=1
            sh = ht._tdx_call_with_timeout(client, 'stocks', timeout=8, market=1)
            if sh is None:
                return [], {}
            codes = [c for c in sh['code'].tolist() if _re.match(r'^688\d{3}$', str(c))]
            names = {}
            for _, r in sh.iterrows():
                if _re.match(r'^688\d{3}$', str(r['code'])):
                    names[str(r['code'])] = str(r.get('name', '')).replace('\x00', '').strip()
    _segment_codes_cache[segment] = {'codes': codes, 'names': names, 'ts': _time.time()}
    # 写入本地文件
    try:
        with open(cache_file, 'w', encoding='utf-8') as f:
            _json.dump({'codes': codes, 'names': names}, f, ensure_ascii=False)
    except Exception:
        pass
    return codes, names



def _fetch_all_a_quotes():
    """拉全市场A股实时报价并计算涨跌幅, 10秒缓存。cyb/kcb共用, 避免重复拉取。"""
    import time as _time
    global _all_quotes_cache
    if _all_quotes_cache and (_time.time() - _all_quotes_cache['ts'] < 10):
        return _all_quotes_cache['data']
    # 合并两个板块的缓存数据
    all_records = []
    for seg in ('cyb', 'kcb'):
        seg_data = _fetch_segment_quotes(seg)
        if seg_data:
            all_records.extend(seg_data)
    if not all_records:
        return None
    _all_quotes_cache = {'data': all_records, 'ts': _time.time()}
    return all_records



def _fetch_segment_quotes(segment):
    """拉单个板块(创业板/科创板)的实时报价, 10秒缓存。
    优先 tqcenter batch_pricevol (客户端开着最稳定), 回退 mootdx 批量 quotes。"""
    import time as _time
    import hot_track as ht
    cached = _segment_quotes_cache.get(segment)
    if cached and (_time.time() - cached['ts'] < 10):
        return cached['data']
    codes, names = _get_segment_codes(segment)
    if not codes:
        return None
    # 优先 tqcenter batch_pricevol (走DLL, 不封IP, 客户端开着最稳定)
    try:
        import tdx_source as ts
        if ts.is_available():
            pv = ts.batch_pricevol(codes)
            if pv:
                records = []
                for c in codes:
                    info = pv.get(c)
                    if info:
                        records.append({
                            'code': c,
                            'name': names.get(c, ''),
                            'price': info['price'],
                            'pct': info['pct'],
                            'amount': 0.0,
                            'vol': info['vol'],
                        })
                if records:
                    _segment_quotes_cache[segment] = {'data': records, 'ts': _time.time(), 'source': 'tqcenter'}
                    return records
    except Exception as e:
        print(f'[TQ] 板块报价 {segment} 异常: {e}')
    # 回退 mootdx 批量 quotes (带应用层超时, 80只一批)
    try:
        import pandas as pd
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            frames = []
            for i in range(0, len(codes), 80):
                batch = codes[i:i + 80]
                df = ht._tdx_call_with_timeout(client, 'quotes', timeout=8, symbol=batch)
                if df is not None and not df.empty:
                    frames.append(df)
        if frames:
            all_q = pd.concat(frames, ignore_index=True)
            for col in ('last_close', 'price', 'amount', 'vol'):
                if col in all_q.columns:
                    all_q[col] = pd.to_numeric(all_q[col], errors='coerce')
            valid = all_q.dropna(subset=['last_close'])
            valid = valid[valid['last_close'] > 0].copy()
            valid['price'] = valid.apply(
                lambda r: r['price'] if pd.notna(r['price']) and r['price'] > 0 else r['last_close'], axis=1)
            valid['pct'] = valid.apply(
                lambda r: ((r['price'] - r['last_close']) / r['last_close'] * 100)
                          if r['last_close'] > 0 and r['price'] != r['last_close'] else 0.0, axis=1)
            records = []
            for _, row in valid.iterrows():
                code = str(row.get('code', ''))
                records.append({
                    'code': code,
                    'name': names.get(code, ''),
                    'price': round(float(row['price']), 2),
                    'pct': round(float(row['pct']), 2),
                    'amount': round(float(row.get('amount', 0)) / 1e8, 2),
                    'vol': float(row.get('vol', 0)),
                })
            if records:
                _segment_quotes_cache[segment] = {'data': records, 'ts': _time.time(), 'source': 'mootdx'}
                return records
    except Exception as e:
        print(f'[mootdx] 板块报价 {segment} 异常: {e}')
    return None



@monitor_bp.route('/api/monitor/stocks/list', methods=['GET'])

def monitor_stocks_list():
    """创业板/科创板个股列表, 按涨幅/成交额/成交量排序。
    参数: segment=cyb(创业板30开头)|kcb(科创板688开头), sort=pct|amount|vol, limit=50"""
    import time as _time
    segment = request.args.get('segment', 'cyb')
    sort = request.args.get('sort', 'pct')
    limit = request.args.get('limit', default=50, type=int)
    if segment not in ('cyb', 'kcb'):
        return jsonify({'success': False, 'error': 'segment 需为 cyb 或 kcb'}), 400
    if sort not in ('pct', 'amount', 'vol'):
        sort = 'pct'
    limit = max(5, min(limit, 200))
    # 10秒缓存(支持前端自动刷新, 又避免高频打TDX)
    cache_key = (segment, sort)
    cached = _stock_list_cache.get(cache_key)
    if cached and (_time.time() - cached['ts'] < 10):
        return jsonify({'success': True, 'stocks': cached['data'][:limit], 'source': cached.get('source', '')})
    try:
        # 直接拉对应板块(不拉全市场), 10秒缓存
        all_records = _fetch_segment_quotes(segment)
        if not all_records:
            return jsonify({'success': False, 'error': '无报价数据'}), 404
        stocks = all_records
        # 排序
        stocks.sort(key=lambda r: r.get(sort, 0), reverse=True)
        # 从板块报价缓存取数据源标签
        seg_cache = _segment_quotes_cache.get(segment, {})
        src = seg_cache.get('source', '')
        _stock_list_cache[cache_key] = {'data': stocks, 'ts': _time.time(), 'source': src}
        return jsonify({'success': True, 'stocks': stocks[:limit], 'source': src})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



@monitor_bp.route('/api/monitor/stock/daily', methods=['GET'])

def monitor_stock_daily():
    """个股日K线
    优先 tqcenter kline (客户端开着最稳定), 回退 mootdx bars。
    参数: code=300001, count=120"""
    code = request.args.get('code', '')
    if not re.match(r'^\d{6}$', code):
        return jsonify({'success': False, 'error': 'code 需为6位数字'}), 400
    count = request.args.get('count', default=120, type=int)
    count = max(20, min(count, 800))
    # 优先 tqcenter (带 3s 超时保护)
    try:
        import tdx_source as ts
        if ts.is_available():
            import threading
            _bars_result = [None]
            def _fetch_tq_kline():
                try:
                    _bars_result[0] = ts.kline(code, count=count, period='1d', dividend_type='none')
                except Exception:
                    pass
            _t = threading.Thread(target=_fetch_tq_kline, daemon=True)
            _t.start()
            _t.join(timeout=3.0)
            bars = _bars_result[0]
            if bars and len(bars) >= 5:
                # 补 last_close (前一根收盘)
                for i in range(len(bars)):
                    bars[i]['last_close'] = bars[i-1]['close'] if i > 0 else bars[i]['open']
                return jsonify({'success': True, 'bars': bars, 'source': 'tqcenter'})
    except Exception as e:
        print(f'[TQ] 个股K线异常: {e}')
    # 回退 mootdx bars (带应用层超时, 防止卡死)
    try:
        import hot_track as ht
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            df = ht._tdx_call_with_timeout(client, 'bars', timeout=8, symbol=code, frequency=9, offset=count)
        if df is not None and len(df) > 0:
            df = df.sort_index()
            bars = []
            prev_close = None
            for idx, row in df.iterrows():
                close = round(float(row['close']), 2)
                bars.append({
                    'date': str(idx)[:10],
                    'open': round(float(row['open']), 2),
                    'close': close,
                    'high': round(float(row['high']), 2),
                    'low': round(float(row['low']), 2),
                    'vol': float(row['vol']),
                    'amount': float(row.get('amount', 0)),
                    'last_close': prev_close if prev_close is not None else round(float(row['open']), 2),
                })
                prev_close = close
            return jsonify({'success': True, 'bars': bars, 'source': 'mootdx'})
    except Exception as e:
        print(f'[mootdx] 个股K线异常: {e}')
    return jsonify({'success': False, 'error': f'{code} 无数据'}), 404



_stock_minute_cache = {}  # {code: {'ts':.., 'data':..}} 个股分时3秒缓存


@monitor_bp.route('/api/monitor/stock/minute', methods=['GET'])

def monitor_stock_minute():
    """个股当日分时 (mootdx minute, 纯6位代码)。3秒缓存。
    参数: code=300001"""
    import time as _time
    code = request.args.get('code', '')
    if not re.match(r'^\d{6}$', code):
        return jsonify({'success': False, 'error': 'code 需为6位数字'}), 400
    cached = _stock_minute_cache.get(code)
    if cached and (_time.time() - cached['ts'] < 3):
        return jsonify(cached['data'])
    try:
        import hot_track as ht
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            df = ht._tdx_call_with_timeout(client, 'minute', timeout=8, symbol=code)
        if df is None or df.empty:
            return jsonify({'success': False, 'error': '无当日分时数据(可能非交易时段)'}), 404
        times = _minute_time_axis(len(df))
        points = []
        for i, row in df.iterrows():
            points.append({
                'time': times[i] if i < len(times) else str(i),
                'price': round(float(row['price']), 2),
                'vol': float(row['vol']),
            })
        result = {'success': True, 'points': points, 'date': 'today', 'source': 'mootdx'}
        # 补充昨收(供前端算涨幅/涨跌停尺度)
        # mootdx bars 无 last_close 列, 取倒数第二根 close 当昨收(最后一根=当日)
        # 优先 tqcenter 快照(含真实 LastClose), 回退 mootdx bars
        try:
            import tdx_source as ts
            if ts.is_available():
                snap = ts.snapshot(code)
                if snap and snap.get('last_close', 0) > 0:
                    result['last_close'] = snap['last_close']
        except Exception:
            pass
        if 'last_close' not in result:
            try:
                with ht._get_tdx_lock():
                    df_k = ht._tdx_call_with_timeout(client, 'bars', timeout=5, symbol=code, frequency=9, offset=2)
                if df_k is not None and len(df_k) >= 2:
                    result['last_close'] = round(float(df_k.sort_index().iloc[-2]['close']), 3)
            except Exception:
                pass
        _stock_minute_cache[code] = {'ts': _time.time(), 'data': result}
        return jsonify(result)
    except Exception as e:
        try:
            import hot_track as ht
            with ht._get_tdx_lock():
                ht._reconnect_tdx()
        except Exception:
            pass
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



@monitor_bp.route('/api/monitor/stock/minutes', methods=['GET'])

def monitor_stock_minutes():
    """个股历史分时 (mootdx minutes, 纯6位代码)。
    参数: code=300001, date=20260703 (YYYYMMDD)"""
    code = request.args.get('code', '')
    if not re.match(r'^\d{6}$', code):
        return jsonify({'success': False, 'error': 'code 需为6位数字'}), 400
    date = request.args.get('date', '')
    if not re.match(r'^\d{8}$', date):
        return jsonify({'success': False, 'error': 'date 参数需为 YYYYMMDD'}), 400
    try:
        import hot_track as ht
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            df = ht._tdx_call_with_timeout(client, 'minutes', timeout=8, symbol=code, date=int(date))
        if df is None or df.empty:
            return jsonify({'success': False, 'error': f'{date} 无分时数据'}), 404
        times = _minute_time_axis(len(df))
        points = []
        for i, row in df.iterrows():
            points.append({
                'time': times[i] if i < len(times) else str(i),
                'price': round(float(row['price']), 2),
                'vol': float(row['vol']),
            })
        date_fmt = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
        return jsonify({'success': True, 'points': points, 'date': date_fmt, 'source': 'mootdx'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



_m5_cache = {}  # {(code): {'data':..., 'ts':...}} 5日/10日分时缓存, 30秒TTL

_m5_down_until = 0  # mootdx不可达时的熔断时间戳, 避免反复超时卡死


def _load_stock_minutes_n(code, days, label):
    """个股N日分时拼接 (逐日拉 minutes, 拼成一条线)。
    code: 6位股票代码, days: 天数, label: '5'/'10' (用于错误提示)
    30秒缓存, 避免切股票来回切换时重复远程拉取。"""
    import time as _time
    global _m5_down_until
    if not re.match(r'^\d{6}$', code):
        return {'success': False, 'error': 'code 需为6位数字'}, 400
    # 30秒缓存
    ck = (code, days)
    cached = _m5_cache.get(ck)
    if cached and (_time.time() - cached['ts'] < 30):
        return cached['data'], 200
    # 熔断: mootdx近期不可达时快速失败, 避免每次切股票都卡30秒+
    if _time.time() < _m5_down_until:
        return {'success': False, 'error': '分时数据源暂不可达(熔断中), 请开启本地通达信或稍后重试'}, 503
    try:
        import hot_track as ht
        lock = ht._get_tdx_lock()
        with lock:
            client = ht._get_tdx_client()
            # 先取最近N个交易日日期 (timeout=6, 快速失败)
            df_k = ht._tdx_call_with_timeout(client, 'bars', timeout=6, symbol=code, frequency=9, offset=days)
            if df_k is None or len(df_k) == 0:
                # bars超时/失败 -> mootdx可能不可达, 开启30秒熔断
                _m5_down_until = _time.time() + 30
                return {'success': False, 'error': f'{code} 无K线数据(行情源可能不可达)'}, 503
            df_k = df_k.sort_index()
            dates = [str(idx)[:10].replace('-', '') for idx in df_k.index]
            # 逐日拉分时, 拼接 (timeout=4, 单日超时即跳过该日)
            all_points = []
            day_labels = []
            for d in dates:
                df_m = ht._tdx_call_with_timeout(client, 'minutes', timeout=4, symbol=code, date=int(d))
                if df_m is None or df_m.empty:
                    continue
                times = _minute_time_axis(len(df_m))
                day_labels.append(f'{d[4:6]}-{d[6:8]}')
                for i, row in df_m.iterrows():
                    all_points.append({
                        'time': times[i] if i < len(times) else str(i),
                        'price': round(float(row['price']), 2),
                        'vol': float(row['vol']),
                        'day': f'{d[4:6]}-{d[6:8]}',
                    })
        if not all_points:
            return {'success': False, 'error': f'无{label}日分时数据'}, 404
        result = {'success': True, 'points': all_points, 'days': day_labels, 'source': 'mootdx'}
        _m5_cache[ck] = {'data': result, 'ts': _time.time()}
        return result, 200
    except RuntimeError as e:
        # _get_tdx_client 抛 RuntimeError(远程不可达/熔断)
        _m5_down_until = _time.time() + 30
        return {'success': False, 'error': f'分时数据源不可达: {e}'}, 503
    except Exception as e:
        return {'success': False, 'error': f'{type(e).__name__}: {e}'}, 500



@monitor_bp.route('/api/monitor/stock/minutes5', methods=['GET'])

def monitor_stock_minutes5():
    """个股5日分时拼接 (逐日拉 minutes, 拼成一条线)。
    参数: code=300001"""
    code = request.args.get('code', '')
    result, status = _load_stock_minutes_n(code, 5, '5')
    return jsonify(result), status



@monitor_bp.route('/api/monitor/stock/minutes10', methods=['GET'])

def monitor_stock_minutes10():
    """个股近10个交易日分时拼接 (逐日拉 minutes, 拼成一条连续线)。
    参数: code=300001"""
    code = request.args.get('code', '')
    result, status = _load_stock_minutes_n(code, 10, '10')
    return jsonify(result), status



# ===== 自选股接口 =====


@monitor_bp.route('/api/monitor/watchlist', methods=['GET'])

def monitor_watchlist():
    """获取自选股列表(含实时 price/pct, 复用全市场报价缓存)。"""
    try:
        wl = _load_watchlist()
        if not wl:
            return jsonify({'success': True, 'watchlist': []})
        all_records = _fetch_all_a_quotes()
        quote_map = {r['code']: r for r in all_records} if all_records else {}
        result = []
        for item in wl:
            code = item.get('code', '')
            q = quote_map.get(code, {})
            result.append({
                'code': code,
                'name': item.get('name', q.get('name', '')),
                'added_at': item.get('added_at', ''),
                'price': q.get('price'),
                'pct': q.get('pct'),
            })
        return jsonify({'success': True, 'watchlist': result})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



@monitor_bp.route('/api/monitor/watchlist/add', methods=['POST'])

def monitor_watchlist_add():
    """加入自选股 {code, name}"""
    data = request.get_json(silent=True) or {}
    code = str(data.get('code', '')).strip()
    name = str(data.get('name', '')).strip()
    if not re.match(r'^\d{6}$', code):
        return jsonify({'success': False, 'error': 'code 需为6位数字'}), 400
    wl = _load_watchlist()
    if not any(w.get('code') == code for w in wl):
        import time as _time
        wl.append({'code': code, 'name': name, 'added_at': int(_time.time())})
        _save_watchlist(wl)
    return jsonify({'success': True, 'watchlist': wl})



@monitor_bp.route('/api/monitor/watchlist/remove', methods=['POST'])

def monitor_watchlist_remove():
    """移除自选股 {code}"""
    data = request.get_json(silent=True) or {}
    code = str(data.get('code', '')).strip()
    if not re.match(r'^\d{6}$', code):
        return jsonify({'success': False, 'error': 'code 需为6位数字'}), 400
    wl = _load_watchlist()
    wl = [w for w in wl if w.get('code') != code]
    _save_watchlist(wl)
    return jsonify({'success': True, 'watchlist': wl})



# ===== 量比筛选接口 =====

_volume_ratio_cache = {}  # {segment: {data, ts}}



@monitor_bp.route('/api/monitor/stocks/volume-ratio', methods=['GET'])

def monitor_stocks_volume_ratio():
    """计算板块内个股 当日成交量 / 5日均量 比值(量比), 用于条件筛选。
    参数: segment=cyb|kcb。60秒缓存。
    返回 {success, ratios: {code: {day_vol, avg5_vol, ratio, pass}}}
    注: 量比 = 当日总成交量 / 过去5日平均总成交量。两者都用日K的vol(全天总量), 口径一致。"""
    import time as _time
    segment = request.args.get('segment', 'cyb')
    if segment not in ('cyb', 'kcb'):
        return jsonify({'success': False, 'error': 'segment 需为 cyb 或 kcb'}), 400
    cached = _volume_ratio_cache.get(segment)
    if cached and (_time.time() - cached['ts'] < 60):
        return jsonify({'success': True, 'ratios': cached['data']})
    try:
        import hot_track as ht
        import pandas as pd
        # 取板块股票列表(复用列表接口逻辑)
        all_records = _fetch_all_a_quotes()
        if not all_records:
            return jsonify({'success': False, 'error': '无报价数据'}), 404
        prefix = '30' if segment == 'cyb' else '688'
        stocks = [r for r in all_records if r['code'].startswith(prefix)]
        stocks.sort(key=lambda r: r.get('pct', 0), reverse=True)
        stocks = stocks[:50]  # Top50, 与列表一致
        ratios = {}
        # 优先 mootdx bars (稳定, 不卡), 回退 tqcenter
        try:
            with ht._get_tdx_lock():
                client = ht._get_tdx_client()
                for s in stocks:
                    code = s['code']
                    try:
                        # 一次 bars 调用同时取当日量和5日均量(日K vol=全天总成交量, 单位:手)
                        df_k = ht._tdx_call_with_timeout(client, 'bars', timeout=5, symbol=code, frequency=9, offset=6)
                        if df_k is None or len(df_k) == 0:
                            ratios[code] = {'day_vol': 0, 'avg5_vol': 0, 'ratio': 0, 'pass': False}
                            continue
                        df_k = df_k.sort_index()
                        day_vol = float(df_k['vol'].iloc[-1])           # 最后一根 = 当日总量
                        # 盘前/未开盘: 当日量为0时, 回退到最近一个交易日的数据
                        if day_vol == 0 and len(df_k) >= 2:
                            day_vol = float(df_k['vol'].iloc[-2])       # 昨日总量
                            prev_vols = df_k['vol'].iloc[:-2] if len(df_k) > 2 else []
                            avg5_vol = float(prev_vols.iloc[-5:].mean()) if len(prev_vols) > 0 else 0
                        else:
                            prev_vols = df_k['vol'].iloc[:-1]
                            avg5_vol = float(prev_vols.iloc[-5:].mean()) if len(prev_vols) > 0 else 0
                        ratio = (day_vol / avg5_vol) if avg5_vol > 0 else 0
                        ratios[code] = {
                            'day_vol': round(day_vol, 0),
                            'avg5_vol': round(avg5_vol, 0),
                            'ratio': round(ratio, 2),
                            'pass': ratio >= 3.0,
                        }
                    except Exception:
                        ratios[code] = {'day_vol': 0, 'avg5_vol': 0, 'ratio': 0, 'pass': False}
        except Exception as e:
            print(f'[mootdx] 量比异常: {e}')
        _volume_ratio_cache[segment] = {'data': ratios, 'ts': _time.time()}
        return jsonify({'success': True, 'ratios': ratios, 'source': 'mootdx'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



_minute_vol_ratio_cache = {}  # {segment: {data, ts}}



@monitor_bp.route('/api/monitor/stocks/minute-vol-ratio', methods=['GET'])

def monitor_stocks_minute_vol_ratio():
    """计算板块内个股 当日成交量 / 前5天平均成交量。
    用于"当日最大分时量>前5天平均分时量×2"条件筛选(用日总量近似, 避免逐日拉分时太慢)。
    参数: segment=cyb|kcb。60秒缓存。
    返回 {success, ratios: {code: {max_minute_vol, avg5_max_minute_vol, ratio, pass}}}
    数据源: tqcenter kline(日K vol), 走本地DLL不封IP, 50只约8秒。"""
    import time as _time
    segment = request.args.get('segment', 'cyb')
    if segment not in ('cyb', 'kcb'):
        return jsonify({'success': False, 'error': 'segment 需为 cyb 或 kcb'}), 400
    cached = _minute_vol_ratio_cache.get(segment)
    if cached and (_time.time() - cached['ts'] < 60):
        return jsonify({'success': True, 'ratios': cached['data']})
    # 取板块 Top 50 (复用已排序的报价缓存)
    all_records = _fetch_segment_quotes(segment)
    if not all_records:
        return jsonify({'success': False, 'error': '无报价数据'}), 404
    stocks = sorted(all_records, key=lambda r: r.get('pct', 0), reverse=True)[:50]
    ratios = {}
    try:
        import tdx_source as ts
        if not ts.is_available():
            return jsonify({'success': False, 'error': '通达信tq接口不可用'}), 503
        for s in stocks:
            code = s['code']
            try:
                # 用日K vol(总量)近似: 取6根日K, 最后一根=今日, 前5根=前5天
                bars = ts.kline(code, count=6, period='1d')
                if not bars or len(bars) < 2:
                    ratios[code] = {'max_minute_vol': 0, 'avg5_max_minute_vol': 0, 'ratio': 0, 'pass': False}
                    continue
                today_vol = float(bars[-1]['vol'])
                prev_vols = [float(b['vol']) for b in bars[:-1][-5:]]
                avg5_vol = sum(prev_vols) / len(prev_vols) if prev_vols else 0
                ratio = (today_vol / avg5_vol) if avg5_vol > 0 else 0
                ratios[code] = {
                    'max_minute_vol': round(today_vol, 0),
                    'avg5_max_minute_vol': round(avg5_vol, 0),
                    'ratio': round(ratio, 2),
                    'pass': ratio >= 2.0,
                }
            except Exception:
                ratios[code] = {'max_minute_vol': 0, 'avg5_max_minute_vol': 0, 'ratio': 0, 'pass': False}
        _minute_vol_ratio_cache[segment] = {'data': ratios, 'ts': _time.time()}
        return jsonify({'success': True, 'ratios': ratios, 'source': 'tqcenter'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



_open_gain_cache = {}  # {segment: {data, ts}}



@monitor_bp.route('/api/monitor/stocks/open-gain', methods=['GET'])

def monitor_stocks_open_gain():
    """计算板块内个股 开盘涨幅 = (当日开盘价 - 昨收) / 昨收 * 100, 用于"9:25开盘>3%"等条件筛选。
    参数: segment=cyb|kcb。60秒缓存。
    返回 {success, gains: {code: {open, last_close, open_gain, pass}}}
    数据源: 优先 tqcenter batch_snapshots(含Open/LastClose, 走本地DLL不封IP), 回退 mootdx quotes(含open列)。"""
    import time as _time
    segment = request.args.get('segment', 'cyb')
    if segment not in ('cyb', 'kcb'):
        return jsonify({'success': False, 'error': 'segment 需为 cyb 或 kcb'}), 400
    cached = _open_gain_cache.get(segment)
    if cached and (_time.time() - cached['ts'] < 60):
        return jsonify({'success': True, 'gains': cached['data']})
    try:
        import hot_track as ht
        # 取板块 Top50 股票列表(与列表接口一致)
        records = _fetch_segment_quotes(segment)
        if not records:
            return jsonify({'success': False, 'error': '无报价数据'}), 404
        codes = [r['code'] for r in records[:50]]
        gains = {}

        # 辅助: 当 open=0(盘前/未开盘)时, 用日K取最近交易日数据
        # 返回 (open, last_close) 或 None
        def _fallback_prev_day(code):
            try:
                with ht._get_tdx_lock():
                    client = ht._get_tdx_client()
                    df_k = ht._tdx_call_with_timeout(client, 'bars', timeout=5, symbol=code, frequency=9, offset=3)
                if df_k is None or len(df_k) < 3:
                    return None
                df_k = df_k.sort_index()
                prev_open = float(df_k['open'].iloc[-2])      # 昨日开盘价
                prev_prev_close = float(df_k['close'].iloc[-3])  # 前日收盘
                if prev_open <= 0 or prev_prev_close <= 0:
                    return None
                return (prev_open, prev_prev_close)
            except Exception:
                return None

        # 优先 tqcenter batch_snapshots(含 Open + LastClose)
        try:
            import tdx_source as ts
            if ts.is_available():
                snaps = ts.batch_snapshots(codes)
                if snaps:
                    for c in codes:
                        s = snaps.get(c)
                        if s:
                            open_p = s.get('open', 0) or 0
                            last_close = s.get('last_close', 0) or 0
                            # 盘前/未开盘: open=0 时回退到昨日数据
                            if open_p <= 0:
                                fb = _fallback_prev_day(c)
                                if fb:
                                    open_p, last_close = fb
                            og = ((open_p - last_close) / last_close * 100) if last_close > 0 and open_p > 0 else 0
                            gains[c] = {
                                'open': round(open_p, 3),
                                'last_close': round(last_close, 3),
                                'open_gain': round(og, 2),
                                'pass': og > 3.0,
                            }
                    if gains:
                        _open_gain_cache[segment] = {'data': gains, 'ts': _time.time()}
                        return jsonify({'success': True, 'gains': gains, 'source': 'tqcenter'})
        except Exception as e:
            print(f'[TQ] 开盘涨幅 {segment} 异常: {e}')
        # 回退 mootdx quotes(含 open / last_close 列)
        import pandas as pd
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            for i in range(0, len(codes), 80):
                batch = codes[i:i + 80]
                df = ht._tdx_call_with_timeout(client, 'quotes', timeout=8, symbol=batch)
                if df is None or df.empty:
                    continue
                for col in ('open', 'last_close', 'price'):
                    if col in df.columns:
                        df[col] = pd.to_numeric(df[col], errors='coerce')
                for _, row in df.iterrows():
                    c = str(row.get('code', ''))
                    open_p = float(row.get('open', 0) or 0)
                    last_close = float(row.get('last_close', 0) or 0)
                    if last_close <= 0:
                        continue
                    # 盘前/未开盘: open=0 时回退到昨日数据
                    if open_p <= 0:
                        fb = _fallback_prev_day(c)
                        if fb:
                            open_p, last_close = fb
                    og = ((open_p - last_close) / last_close * 100) if open_p > 0 else 0
                    gains[c] = {
                        'open': round(open_p, 3),
                        'last_close': round(last_close, 3),
                        'open_gain': round(og, 2),
                        'pass': og > 3.0,
                    }
        _open_gain_cache[segment] = {'data': gains, 'ts': _time.time()}
        return jsonify({'success': True, 'gains': gains, 'source': 'mootdx'})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



# ===== 概念板块涨停统计 =====

_concept_members_cache = None    # {concept: [(code, name), ...]}, 1小时缓存

_concept_members_ts = 0

_concept_zt_cache = None         # {'data':..., 'ts':...}, 5秒缓存

_concept_zt_timeline = []        # [{ts, '概念':count, ...}, ...], 最多200个点

_concept_zt_top10_ever = set()   # 曾进过top10的概念名(折线图用)

_concept_dist_timelines = {}     # {concept: [{ts, trade_min, buckets:{...}}]}, 每概念最多200点

_concept_dist_cache = {}         # {concept: {'data':..., 'ts':...}}, 5秒缓存

_concept_dist6_timeline = []     # [{ts, trade_min, buckets:{concept:{g3_5,g5_7,zt_up,d3_5,d5_7,zt_down}}}], 最多200点

_concept_zt_session_date = None  # 当前排名时间线所属交易日, 跨日自动清空


# 排除的宽泛概念(成分股过多, 无实际板块意义)

_EXCLUDED_CONCEPTS = {'国企改革', '深股通', '沪股通', '融资融券', '市场连板股', 'ST板块'}


def _load_concept_members():
    """从 ths.concept_member 加载概念->成分股映射(沪深主板60/00 + 创业板30 + 科创板68, 非ST, 排除宽泛概念)。1小时缓存。"""
    global _concept_members_cache, _concept_members_ts
    import time as _time
    if _concept_members_cache and (_time.time() - _concept_members_ts < 3600):
        return _concept_members_cache
    try:
        import db as _db
        conn = _db.get_conn()
        with conn.cursor() as cur:
            # 取主板(60/00) + 创业板(30) + 科创板(68), 排除ST(名称含ST, 在调用方按名称过滤)
            cur.execute("""
                SELECT concept, stock_code FROM ths.concept_member
                WHERE stock_code ~ '^(60|00|30|68)'
                ORDER BY concept, stock_code
            """)
            rows = cur.fetchall()
        conn.close()
        # 按概念分组(排除宽泛概念)
        result = {}
        for concept, code in rows:
            if concept in _EXCLUDED_CONCEPTS:
                continue
            result.setdefault(concept, []).append(code)
        _concept_members_cache = result
        _concept_members_ts = _time.time()
        _report_log = f'[概念] 加载 {len(result)} 个概念, {len(rows)} 条成分股'
        print(_report_log)
        return result
    except Exception as e:
        print(f'[概念] 加载成分股失败: {e}')
        return _concept_members_cache or {}



def _time_to_trade_min(time_str):
    """HH:MM 或 HH:MM:SS -> trade_min (9:25=0, 11:30=125, 13:00=126, 15:00=246)"""
    try:
        parts = str(time_str).strip().split(':')
        h, m = int(parts[0]), int(parts[1])
    except Exception:
        return 0
    hm = h * 60 + m
    if hm < 9 * 60 + 25:
        return 0
    elif hm <= 11 * 60 + 30:
        return hm - (9 * 60 + 25)
    elif hm < 13 * 60:
        return 125
    elif hm <= 15 * 60:
        return hm - (13 * 60) + 126
    else:
        return 246



_concept_hist_minute_cache = {}      # {date_int: {code: [price, ...]}} 多日期分时缓存, 每天只拉一次



def _fetch_zt_minutes(zt_codes, date_int, cancel_check=None):
    """批量拉涨停股的逐分钟分时数据, 按日期缓存(支持多日)。
    返回 {code: [price_per_minute, ...]}, 240个点(9:30-15:00)。
    涨停价 = 当日分时最高价; price == 最高价 即为封板状态。
    优先级: 本地 .lc1 文件(秒级) > mootdx minutes 逐只远程(兜底)。
    cancel_check: 可选的无参回调, 返回True时中止。
    """
    global _concept_hist_minute_cache
    day_cache = _concept_hist_minute_cache.get(date_int, {})
    missing = [c for c in zt_codes if c not in day_cache]
    if not missing:
        return day_cache

    import time as _time
    fetched = 0
    failed = 0
    src = ''

    # ---------- 优先: 本地 .lc1 文件(需通达信盘后下载1分钟线) ----------
    try:
        local = _read_lc1_minutes_batch(missing, date_int)
        if local:
            for code, prices in local.items():
                day_cache[code] = prices
                fetched += 1
                missing.remove(code)
            src = 'lc1'
            print(f'[概念] 分时 .lc1 本地读取 date={date_int}: {len(local)}只')
    except Exception as e:
        print(f'[概念] .lc1 本地读取失败: {e}')

    # ---------- 兜底: mootdx minutes 逐只远程(仅本地缺失的) ----------
    if missing and not (cancel_check and cancel_check()):
        try:
            import hot_track as ht
            with ht._get_tdx_lock():
                client = ht._get_tdx_client()
                for code in missing:
                    if cancel_check and cancel_check():
                        break
                    try:
                        t0 = _time.time()
                        df = ht._tdx_call_with_timeout(client, 'minutes', timeout=5, symbol=code, date=date_int)
                        dt = _time.time() - t0
                        if dt > 2:
                            print(f'[概念] 分时 {code} 耗时{dt:.1f}s (慢)')
                        if df is not None and not df.empty and 'price' in df.columns:
                            prices = df['price'].tolist()
                            if prices:
                                day_cache[code] = prices
                                fetched += 1
                        else:
                            failed += 1
                    except Exception as e:
                        failed += 1
                        print(f'[概念] 分时 {code} 异常: {e}')
            src = src or 'mootdx'
        except Exception as e:
            print(f'[概念] 拉分时失败: {e}')

    _concept_hist_minute_cache[date_int] = day_cache
    print(f'[概念] 分时拉取完成 date={date_int} [{src}]: 成功{fetched} 失败{failed}')

    return day_cache



def _build_historical_timeline(realtime_zt_codes=None, date_str=None, zt_codes_override=None):
    """用逐分钟分时数据还原某日概念涨停数变化(可上可下, 能反映炸板/回封)。
    参数:
    - realtime_zt_codes: 实时报价判定的涨停股(monitor场景, 用今天日期)
    - date_str: 指定日期 YYYYMMDD(概念追踪场景)
    - zt_codes_override: 直接传入涨停股集合
    原理:
    1. 涨停股集合 = zt_codes_override > realtime_zt_codes > zt_stocks
    2. 对每只涨停股拉 mootdx minutes(code, date), 得到240个分钟价格
    3. 涨停价 = 当日分时最高价; price == 涨停价 即为该分钟处于封板状态
    4. 逐分钟统计每个概念有多少只成分股处于封板 -> 可上可下的曲线
    返回 (timeline_list, top_concepts_set) 或 ([], set())"""
    try:
        import db as _db
        from datetime import datetime as _dt

        # 确定日期
        if date_str:
            date_int = int(date_str)
            query_date = date_str
        elif realtime_zt_codes:
            date_int = int(_dt.now().strftime('%Y%m%d'))
            query_date = _dt.now().strftime('%Y-%m-%d')
        else:
            conn = _db.get_conn()
            with conn.cursor() as cur:
                cur.execute("SELECT max(trade_date) FROM zt_stocks")
                row = cur.fetchone()
                if not row or not row[0]:
                    return [], set()
                query_date = row[0]
            conn.close()
            date_int = int(str(query_date).replace('-', ''))

        # 涨停股集合: zt_codes_override > realtime_zt_codes > zt_stocks
        if zt_codes_override:
            zt_codes = set(str(c).zfill(6) for c in zt_codes_override)
        elif realtime_zt_codes:
            zt_codes = set(str(c).zfill(6) for c in realtime_zt_codes)
        else:
            conn = _db.get_conn()
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT code FROM zt_stocks
                    WHERE trade_date = %s AND (code LIKE '60%%' OR code LIKE '00%%')
                      AND name NOT LIKE '%%ST%%'
                """, (query_date,))
                zt_codes = set(r[0].zfill(6) for r in cur.fetchall())
            conn.close()
        if not zt_codes:
            return [], set()

        # 拉分时数据(按日期缓存)
        minute_map = _fetch_zt_minutes(zt_codes, date_int)
        if not minute_map:
            return [], set()

        # 概念体系
        concept_map = _load_concept_members()
        if not concept_map:
            return [], set()

        # code -> concepts 反向映射
        code_to_concepts = {}
        for concept, codes in concept_map.items():
            for c in codes:
                code_to_concepts.setdefault(c, set()).add(concept)

        # 逐分钟(0-239)统计每个概念的封板数
        from collections import defaultdict
        minute_concept_counts = defaultdict(lambda: defaultdict(int))
        final_counts = defaultdict(int)

        for code, prices in minute_map.items():
            if code not in zt_codes:
                continue
            concepts = code_to_concepts.get(code, set())
            if not concepts:
                continue
            max_p = max(prices)
            for i, p in enumerate(prices):
                if abs(p - max_p) < 0.005:  # 该分钟处于涨停价
                    for con in concepts:
                        minute_concept_counts[i][con] += 1
            # 收盘(最后一分钟)封板?
            if abs(prices[-1] - max_p) < 0.005:
                for con in concepts:
                    final_counts[con] += 1

        if not minute_concept_counts:
            return [], set()

        # 取最终封板数 top10 的概念
        top_concepts = set(sorted(final_counts, key=final_counts.get, reverse=True)[:10])

        # 生成 timeline: 只在有变化的时间点输出(减少数据量)
        def min_idx_to_trade_min(idx):
            if idx < 120:
                return idx + 5  # 9:30 = trade_min 5
            else:
                return idx + 6  # 13:00 = idx 120 -> trade_min 126

        all_mins = sorted(minute_concept_counts.keys())
        timeline = []
        prev_counts = {}
        for m in all_mins:
            counts = dict(minute_concept_counts[m])
            if counts != prev_counts:
                tm = min_idx_to_trade_min(m)
                timeline.append({
                    'ts': _time_to_label(tm),
                    'trade_min': tm,
                    'counts': counts,
                })
                prev_counts = counts

        return timeline, top_concepts
    except Exception as e:
        print(f'[概念] 历史回放失败: {e}')
        return [], set()



def _time_to_label(trade_min):
    """trade_min -> HH:MM 标签"""
    if trade_min <= 125:
        h = 9
        m = 25 + trade_min
    else:
        h = 13
        m = trade_min - 126
    h += m // 60
    m = m % 60
    return f'{h}:{m:02d}'


@monitor_bp.route('/api/monitor/concept/zt-stats', methods=['GET'])

def monitor_concept_zt_stats():
    """概念板块涨停统计(仅主板非ST, 5秒缓存)。
    返回: {success, ts, concepts: [...], timeline: [...], top10_ever: [...]}
    concepts: [{concept, zt_count, zt_stocks:[{code,pct}], dist:{lt3,g3_5,g5_7}, total_members}]
    timeline: [{ts, concepts:{概念:涨停数}}]  供折线图用
    """
    import time as _time
    global _concept_zt_cache, _concept_zt_timeline, _concept_zt_top10_ever, _concept_dist6_timeline, _concept_zt_session_date

    # 排名/分时数据只属于当天, 服务跨日运行时必须清空, 避免把昨日曾入榜概念混进今天。
    session_date = _time.strftime('%Y%m%d')
    if _concept_zt_session_date != session_date:
        _concept_zt_session_date = session_date
        _concept_zt_cache = None
        _concept_zt_timeline = []
        _concept_zt_top10_ever = set()
        _concept_dist6_timeline = []

    # 10秒缓存
    if _concept_zt_cache and (_time.time() - _concept_zt_cache['ts'] < 10):
        return jsonify(_concept_zt_cache['data'])

    try:
        import hot_track as ht
        import pandas as pd
        concept_map = _load_concept_members()
        if not concept_map:
            return jsonify({'success': False, 'error': '无概念成分股数据'}), 404

        # 收集所有需要拉报价的主板股票(去重)
        all_codes = set()
        for codes in concept_map.values():
            all_codes.update(codes)
        all_codes = sorted(all_codes)
        if not all_codes:
            return jsonify({'success': False, 'error': '无主板成分股'}), 404

        # 优先 tqcenter batch_pricevol (走DLL, 1秒取全部, 不封IP), 回退 mootdx 批量 quotes
        quote_map = {}
        used_tq = False
        try:
            import tdx_source as ts
            if ts.is_available():
                # TQ批量报价不返回名称，先取股票名排除ST/退市，与mootdx分支口径一致。
                try:
                    stock_rows = ts.stock_list('5', with_name=True) or []
                    excluded_codes = {
                        str(row.get('code', '')).zfill(6)
                        for row in stock_rows
                        if 'ST' in str(row.get('name', '')).upper() or '退' in str(row.get('name', ''))
                    }
                    if excluded_codes:
                        all_codes = [code for code in all_codes if code not in excluded_codes]
                except Exception as e:
                    print(f'[TQ] zt-stats 股票名称/ST过滤异常: {e}')
                pv = ts.batch_pricevol(all_codes)
                if pv:
                    for code, info in pv.items():
                        if info.get('last_close', 0) > 0:
                            quote_map[code] = {
                                'pct': info['pct'],
                                'price': info['price'],
                                'name': '',
                            }
                    used_tq = True
        except Exception as e:
            print(f'[TQ] zt-stats 批量报价异常: {e}')

        if not used_tq:
            # 回退 mootdx 批量拉报价(80只/批)
            with ht._get_tdx_lock():
                client = ht._get_tdx_client()
                frames = []
                for i in range(0, len(all_codes), 80):
                    batch = all_codes[i:i + 80]
                    df = ht._tdx_call_with_timeout(client, 'quotes', timeout=8, symbol=batch)
                    if df is not None and not df.empty:
                        frames.append(df)
            if not frames:
                return jsonify({'success': False, 'error': '无报价数据(可能非交易时段)'}), 404

            all_q = pd.concat(frames, ignore_index=True)
            for col in ('last_close', 'price', 'amount', 'vol'):
                if col in all_q.columns:
                    all_q[col] = pd.to_numeric(all_q[col], errors='coerce')
            # 排除ST(quotes返回的name列)
            if 'name' in all_q.columns:
                all_q = all_q[~all_q['name'].astype(str).str.contains('ST', case=False, na=False)]
            valid = all_q[(all_q['last_close'] > 0) & (all_q['price'] > 0)].copy()
            if valid.empty:
                return jsonify({'success': False, 'error': '无有效报价'}), 404
            valid['pct'] = (valid['price'] - valid['last_close']) / valid['last_close'] * 100
            valid['code'] = valid['code'].astype(str).str.zfill(6)
            quote_map = {r['code']: r for _, r in valid.iterrows()}

        # 按概念分组统计
        now_ts = _time.time()
        now_str = _time.strftime('%H:%M:%S')
        # 交易分钟序号: 9:25=0, 11:30=125, 跳过午休, 13:00=126, 15:00=246
        from datetime import datetime as _dt
        _now_hm = _dt.now().hour * 60 + _dt.now().minute
        if _now_hm < 9 * 60 + 25:
            _trade_min = 0
        elif _now_hm <= 11 * 60 + 30:
            _trade_min = _now_hm - (9 * 60 + 25)
        elif _now_hm < 13 * 60:
            _trade_min = 125  # 午休期间固定为午前最后一刻
        elif _now_hm <= 15 * 60:
            _trade_min = _now_hm - (13 * 60) + 126  # 126 = 上午125分钟 + 1
        else:
            _trade_min = 246
        concepts_result = []
        timeline_entry = {'ts': now_str, 'trade_min': _trade_min, 'concepts': {}}
        dist6_entry = {'ts': now_str, 'trade_min': _trade_min, 'buckets': {}}
        realtime_zt_codes = set()  # 收集所有实时涨停股代码(传给历史回放, 保证数量一致)

        for concept, codes in concept_map.items():
            zt_stocks = []
            dist = {'lt3': 0, 'g3_5': 0, 'g5_7': 0}
            dist6 = {'g3_5': 0, 'g5_7': 0, 'zt_up': 0, 'd3_5': 0, 'd5_7': 0, 'zt_down': 0}
            total = 0
            for code in codes:
                q = quote_map.get(code)
                if q is None:
                    continue
                total += 1
                pct = float(q['pct'])
                apct = abs(pct)
                # 涨跌幅分布(abs口径, 旧)
                if apct < 3:
                    dist['lt3'] += 1
                elif apct < 5:
                    dist['g3_5'] += 1
                elif apct < 7:
                    dist['g5_7'] += 1
                # 6档正负分桶(新, 涨停阈值按板块区分)
                thresh = 19.8 if code.startswith(('30', '68')) else 9.8
                if pct >= thresh:
                    dist6['zt_up'] += 1
                elif pct >= 5:
                    dist6['g5_7'] += 1
                elif pct >= 3:
                    dist6['g3_5'] += 1
                elif pct <= -thresh:
                    dist6['zt_down'] += 1
                elif pct <= -5:
                    dist6['d5_7'] += 1
                elif pct <= -3:
                    dist6['d3_5'] += 1
                # 概念涨停数与6档分布统一使用分板阈值:
                # 创业板/科创板20cm取19.8%, 其余10cm取9.8%。
                if pct >= thresh:
                    zt_stocks.append({'code': code, 'pct': round(pct, 2)})
                    realtime_zt_codes.add(code)
            if total == 0:
                continue
            concepts_result.append({
                'concept': concept,
                'zt_count': len(zt_stocks),
                'zt_stocks': zt_stocks[:20],  # 最多返回20只
                'dist': dist,
                'dist6': dist6,
                'total_members': total,
            })
            timeline_entry['concepts'][concept] = len(zt_stocks)
            dist6_entry['buckets'][concept] = dict(dist6)

        # 按涨停数降序；同数量时按概念名稳定排序，避免每次请求边界抖动。
        concepts_result.sort(key=lambda x: (-x['zt_count'], x['concept']))

        # 取当前前10, 记录曾进top10的概念
        top10_now = [c['concept'] for c in concepts_result if c['zt_count'] > 0][:10]
        _concept_zt_top10_ever.update(top10_now)

        # 返回top15(前端取top10, 多给几条避免边界抖动)
        top_concepts = [c for c in concepts_result if c['zt_count'] > 0][:15]

        def _build_rank_map(counts, candidates):
            """按涨停数生成Top10竞争排名；同数量并列同名次(如1,1,3)。"""
            ordered = sorted(
                [(c, counts.get(c, 0)) for c in candidates if counts.get(c, 0) > 0],
                key=lambda x: (-x[1], x[0])
            )[:10]
            rank_map = {}
            prev_count = None
            current_rank = 0
            for pos, (concept_name, count) in enumerate(ordered, 1):
                if count != prev_count:
                    current_rank = pos
                    prev_count = count
                rank_map[concept_name] = current_rank
            return rank_map

        # 维护时间线(最多200个点, 同一trade_min更新最新值)
        # 交易时段正常采样; 非交易时段(收盘后)也更新最后一点保持与concepts同步
        if _concept_zt_timeline and _concept_zt_timeline[-1].get('trade_min') == _trade_min:
            _concept_zt_timeline[-1] = timeline_entry  # 同分钟更新
        else:
            _concept_zt_timeline.append(timeline_entry)
            if len(_concept_zt_timeline) > 200:
                _concept_zt_timeline = _concept_zt_timeline[-200:]
        if _concept_dist6_timeline and _concept_dist6_timeline[-1].get('trade_min') == _trade_min:
            _concept_dist6_timeline[-1] = dist6_entry  # 同分钟更新
        else:
            _concept_dist6_timeline.append(dist6_entry)
            if len(_concept_dist6_timeline) > 200:
                _concept_dist6_timeline = _concept_dist6_timeline[-200:]

        # 折线图数据: 只保留曾进top10的概念
        timeline_out = []
        ever_list = sorted(_concept_zt_top10_ever)
        for entry in _concept_zt_timeline:
            timeline_out.append({
                'ts': entry['ts'],
                'trade_min': entry.get('trade_min', 0),
                'counts': {c: entry['concepts'].get(c, 0) for c in ever_list},
            })

        # 排名时间线: 每个时间点按涨停数排Top10；同数量并列同名次。
        ranks_out = []
        for entry in _concept_zt_timeline:
            counts = entry.get('concepts', {})
            rank_map = _build_rank_map(counts, ever_list)
            ranks_out.append({
                'ts': entry['ts'],
                'trade_min': entry.get('trade_min', 0),
                'ranks': {c: rank_map.get(c) for c in ever_list},
            })

        # 分桶时间线: 只保留 ever_list 概念的分桶
        dist6_out = []
        for entry in _concept_dist6_timeline:
            dist6_out.append({
                'ts': entry['ts'],
                'trade_min': entry.get('trade_min', 0),
                'buckets': {c: entry['buckets'].get(c, {}) for c in ever_list},
            })

        # 用逐分钟分时数据还原当天涨停轨迹(9:25起), 补全实时点之前的开盘段。
        # 传入实时涨停股集合, 使回放的最终数量与柱状图完全一致。
        # 盘中始终触发(不再仅<3点), 让涨停数/排名曲线从9:25铺满到当前时间。
        hist_timeline, hist_concepts = _build_historical_timeline(realtime_zt_codes)
        if hist_timeline:
            ever_list = sorted(set(hist_concepts) | _concept_zt_top10_ever)
            # 历史回放只覆盖到首个实时点之前; 实时点更精确, 保留实时段。
            # 取实时timeline的首个trade_min作为分界点。
            first_real_min = _concept_zt_timeline[0].get('trade_min', 0) if _concept_zt_timeline else 999
            hist_part = [e for e in hist_timeline if e.get('trade_min', 0) < first_real_min]
            # 合并: 历史段(仅counts) + 实时段
            timeline_out = []
            for entry in hist_part:
                timeline_out.append({
                    'ts': entry['ts'],
                    'trade_min': entry.get('trade_min', 0),
                    'counts': {c: entry['counts'].get(c, 0) for c in ever_list},
                })
            for entry in _concept_zt_timeline:
                timeline_out.append({
                    'ts': entry['ts'],
                    'trade_min': entry.get('trade_min', 0),
                    'counts': {c: entry['concepts'].get(c, 0) for c in ever_list},
                })
            # 排名: 历史段+实时段都用各自counts重建(同数量并列; dist6无历史, 留空)
            ranks_out = []
            for entry in hist_part:
                counts = entry.get('counts', {})
                rank_map = _build_rank_map(counts, ever_list)
                ranks_out.append({
                    'ts': entry['ts'],
                    'trade_min': entry.get('trade_min', 0),
                    'ranks': {c: rank_map.get(c) for c in ever_list},
                })
            for entry in _concept_zt_timeline:
                counts = entry.get('concepts', {})
                rank_map = _build_rank_map(counts, ever_list)
                ranks_out.append({
                    'ts': entry['ts'],
                    'trade_min': entry.get('trade_min', 0),
                    'ranks': {c: rank_map.get(c) for c in ever_list},
                })

        # 追加当前实时数据作为最后一个点, 确保排名/涨停数与concepts完全一致
        # (历史回放的分时封板判断 与 实时报价的pct判断 可能不一致)
        cur_counts = {c['concept']: c['zt_count'] for c in concepts_result}
        current_concepts_map = {c['concept']: c for c in concepts_result}
        cur_rank_map = _build_rank_map(cur_counts, ever_list)
        cur_point = {
            'ts': now_str,
            'trade_min': _trade_min,
            'counts': {c: cur_counts.get(c, 0) for c in ever_list},
        }
        cur_rank_point = {
            'ts': now_str,
            'trade_min': _trade_min,
            'ranks': {c: cur_rank_map.get(c) for c in ever_list},
        }
        # 若最后一点trade_min不同则追加, 相同则替换
        if timeline_out and timeline_out[-1].get('trade_min') == _trade_min:
            timeline_out[-1] = cur_point
            ranks_out[-1] = cur_rank_point
        else:
            timeline_out.append(cur_point)
            ranks_out.append(cur_rank_point)

        # 当前Top10概念名->排名(轻量, 供前端独立接口查股票所属概念)
        top10_concept_ranks = {c: cur_rank_map.get(c) for c in top10_now if cur_rank_map.get(c)}

        result = {
            'success': True,
            'ts': now_str,
            'concepts': top_concepts,
            # 下方曾入榜卡片需要完整当前值，不能仅依赖前15 concepts。
            'ever_current': {
                c: {
                    'concept': c,
                    'zt_count': cur_counts.get(c, 0),
                    'total_members': current_concepts_map.get(c, {}).get('total_members', 0),
                }
                for c in ever_list
            },
            'top10_ever': ever_list,
            'timeline': timeline_out,
            'ranks_timeline': ranks_out,
            'dist6_timeline': dist6_out,
            'top10_concept_ranks': top10_concept_ranks,
            'source': 'tqcenter' if used_tq else 'mootdx',
        }
        _concept_zt_cache = {'data': result, 'ts': now_ts}
        return jsonify(result)
    except Exception as e:
        # 缓存还有数据就返回旧的(降级)
        if _concept_zt_cache:
            return jsonify(_concept_zt_cache['data'])
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



@monitor_bp.route('/api/monitor/stock/concepts', methods=['GET'])

def monitor_stock_concepts():
    """批量查询股票所属的当前Top10概念及排名(轻量, 供个股列表"所属概念"列)。
    参数: codes=300001,300002,688001  (逗号分隔, 最多200只)
    返回: {success, concepts: {code: [[concept, rank], ...]}}
    """
    codes_param = request.args.get('codes', '')
    codes = [c.strip() for c in codes_param.split(',') if re.match(r'^\d{6}$', c.strip())][:200]
    if not codes:
        return jsonify({'success': False, 'error': '缺少 codes 参数'}), 400
    try:
        # 从zt-stats缓存取当前Top10概念排名(不重新计算, 复用10秒缓存)
        cached = _concept_zt_cache
        if not cached or not cached.get('data'):
            return jsonify({'success': True, 'concepts': {}})
        zt_data = cached['data']
        top10_ranks = zt_data.get('top10_concept_ranks', {})
        if not top10_ranks:
            return jsonify({'success': True, 'concepts': {}})

        # 取概念成分股, 只查Top10概念
        concept_map = _load_concept_members()
        # 构建 code -> [(concept, rank)] 只针对请求的codes
        result = {}
        for concept_name, rank in top10_ranks.items():
            members = set(concept_map.get(concept_name, []))
            for code in codes:
                if code in members:
                    result.setdefault(code, []).append([concept_name, rank])
        # 按排名排序
        for code in result:
            result[code].sort(key=lambda x: x[1])
        return jsonify({'success': True, 'concepts': result})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



@monitor_bp.route('/api/monitor/concept/dist-timeline', methods=['GET'])

def monitor_concept_dist_timeline():
    """单概念板块成员股涨跌分布的实时分时(5秒采样, 5秒缓存)。
    参数: concept=概念名
    返回: {success, concept, members_count, timeline:[{ts,trade_min,buckets}], current}
    buckets: {g3_5, g5_7, zt_up, d3_5, d5_7, zt_down} 各档家数
    涨停阈值按板块: 30/68开头=19.8, 其余=9.8
    """
    import time as _time
    from datetime import datetime as _dt

    concept = request.args.get('concept', '').strip()
    if not concept:
        return jsonify({'success': False, 'error': '缺少 concept 参数'}), 400

    # 5秒缓存命中直接返回
    cached = _concept_dist_cache.get(concept)
    if cached and (_time.time() - cached['ts'] < 5):
        return jsonify(cached['data'])

    try:
        import hot_track as ht
        import pandas as pd

        concept_map = _load_concept_members()
        codes = concept_map.get(concept)
        if not codes:
            return jsonify({'success': False, 'error': f'概念「{concept}」无成分股'}), 404

        # mootdx 批量拉报价(80只/批, 同 zt-stats 模式)
        with ht._get_tdx_lock():
            client = ht._get_tdx_client()
            frames = []
            for i in range(0, len(codes), 80):
                batch = codes[i:i + 80]
                df = ht._tdx_call_with_timeout(client, 'quotes', timeout=8, symbol=batch)
                if df is not None and not df.empty:
                    frames.append(df)

        # 分6档计数(仅看正/负, 不计abs; 涨停按板块阈值)
        buckets = {'g3_5': 0, 'g5_7': 0, 'zt_up': 0, 'd3_5': 0, 'd5_7': 0, 'zt_down': 0}
        members_count = 0
        if frames:
            all_q = pd.concat(frames, ignore_index=True)
            for col in ('last_close', 'price'):
                if col in all_q.columns:
                    all_q[col] = pd.to_numeric(all_q[col], errors='coerce')
            if 'name' in all_q.columns:
                all_q = all_q[~all_q['name'].astype(str).str.contains('ST', case=False, na=False)]
            valid = all_q[(all_q['last_close'] > 0) & (all_q['price'] > 0)].copy()
            if not valid.empty:
                valid['pct'] = (valid['price'] - valid['last_close']) / valid['last_close'] * 100
                valid['code'] = valid['code'].astype(str).str.zfill(6)
                members_count = len(valid)
                for _, r in valid.iterrows():
                    pct = float(r['pct'])
                    code = str(r['code'])
                    thresh = 19.8 if code.startswith(('30', '68')) else 9.8
                    if pct >= thresh:
                        buckets['zt_up'] += 1
                    elif pct >= 5:
                        buckets['g5_7'] += 1
                    elif pct >= 3:
                        buckets['g3_5'] += 1
                    elif pct <= -thresh:
                        buckets['zt_down'] += 1
                    elif pct <= -5:
                        buckets['d5_7'] += 1
                    elif pct <= -3:
                        buckets['d3_5'] += 1

        # 交易分钟序号(9:25=0, 11:30=125, 13:00=126, 15:00=246)
        _now_hm = _dt.now().hour * 60 + _dt.now().minute
        if _now_hm < 9 * 60 + 25:
            _trade_min = 0
        elif _now_hm <= 11 * 60 + 30:
            _trade_min = _now_hm - (9 * 60 + 25)
        elif _now_hm < 13 * 60:
            _trade_min = 125
        elif _now_hm <= 15 * 60:
            _trade_min = _now_hm - (13 * 60) + 126
        else:
            _trade_min = 246

        # 追加采样点(有报价数据时才采, 避免非交易时段产生空点)
        now_str = _time.strftime('%H:%M:%S')
        tl = _concept_dist_timelines.setdefault(concept, [])
        if members_count > 0:
            # 同一 trade_min 不重复采(缓存miss但仍在同一分钟内)
            if not tl or tl[-1].get('trade_min') != _trade_min:
                tl.append({
                    'ts': now_str,
                    'trade_min': _trade_min,
                    'buckets': dict(buckets),
                })
                if len(tl) > 200:
                    _concept_dist_timelines[concept] = tl[-200:]

        if not tl:
            return jsonify({'success': False, 'error': '暂无分时数据(可能非交易时段且无历史采样)'}), 404

        result = {
            'success': True,
            'concept': concept,
            'members_count': members_count or (len(codes)),
            'timeline': tl[:],
            'current': {'ts': now_str, 'buckets': buckets, 'trade_min': _trade_min},
            'source': 'mootdx',
        }
        _concept_dist_cache[concept] = {'data': result, 'ts': _time.time()}
        return jsonify(result)
    except Exception as e:
        cached = _concept_dist_cache.get(concept)
        if cached:
            return jsonify(cached['data'])
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



# ===== WebSocket 实时推送 =====

# tqcenter batch_pricevol 批量报价(1秒取1400只) + mootdx 分时(分时数据 tqcenter 盘中拿不到)

_ws_push_thread = None

_ws_subscribed_stocks = set()  # 前端订阅的个股分时代码

_ws_subscribed_quote_codes = {}  # 前端订阅的报价股票: {'cyb': [code,...], 'kcb': [code,...]}

_tq_emit_lock = threading.Lock()  # socketio.emit 线程安全


@monitor_bp.route('/api/monitor/chain-board', methods=['GET'])

def api_chain_board():
    """产业链实时盯盘: 按 产业链->上下游层级->概念 聚合成员股实时涨幅/涨停数/领涨股。
    数据来源: ths.concept_member(成员) + 通达信实时批量报价。结果缓存 15 秒。"""
    import time as _t
    import concept_chain as _cc
    import db as _db
    import tdx_source as _ts
    now = _t.time()
    if _chain_board_cache['data'] and now - _chain_board_cache['ts'] < 15:
        return jsonify(_chain_board_cache['data'])

    ci = _cc.chain_info()
    concept_set = set()
    for c in ci.values():
        concept_set.update(c['concepts'])

    if not _ts.is_available():
        return jsonify({'success': False, 'error': '通达信tq接口不可用(请确认量化版客户端已开启并登录)'}), 503

    # 名称表(缓存1小时) - 先取, 供下方成员ST过滤使用(口径与zt-stats一致)
    if not _astock_name_cache['names'] or now - _astock_name_cache['ts'] > 3600:
        lst = _ts.stock_list('5', with_name=True) or []
        _astock_name_cache['names'] = {x['code']: x['name'] for x in lst}
        _astock_name_cache['ts'] = now
    names = _astock_name_cache['names']
    # ST/退市代码集合(名称含ST或退), 与zt-stats排除口径一致
    excluded_codes = {
        str(code).zfill(6) for code, nm in names.items()
        if 'ST' in str(nm).upper() or '退' in str(nm)
    }

    # 概念->成员(缓存1小时, 成员变化慢)
    # 口径与zt-stats的_load_concept_members一致: 仅沪深主板(60/00)+创业板(30)+科创板(68),
    # 排除北交所(8/4开头)及ST/退市, 使产业链统计与涨停分布盯盘数值可比。
    if not _chain_members_cache['members'] or now - _chain_members_cache['ts'] > 3600:
        try:
            conn = _db.get_conn()
        except Exception as e:
            return jsonify({'success': False, 'error': f'数据库不可用: {e}'}), 503
        con_members = {}
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT concept, stock_code FROM ths.concept_member "
                            "WHERE concept = ANY(%s) AND stock_code ~ '^(60|00|30|68)'",
                            (list(concept_set),))
                for con, code in cur.fetchall():
                    if code in excluded_codes:
                        continue
                    con_members.setdefault(con, set()).add(code)
        finally:
            conn.close()
        _chain_members_cache['members'] = con_members
        _chain_members_cache['ts'] = now
    con_members = _chain_members_cache['members']

    all_codes = set()
    for s in con_members.values():
        all_codes |= s
    if not all_codes:
        return jsonify({'success': False, 'error': '概念成员为空(请先同步同花顺概念数据)'}), 404

    # 批量实时报价(分块)
    quotes = {}
    codes = list(all_codes)
    for i in range(0, len(codes), 800):
        q = _ts.batch_pricevol(codes[i:i + 800]) or {}
        quotes.update(q)

    def agg(code_set):
        pcts = []
        up = lu = up5 = 0
        tops = []
        for c in code_set:
            q = quotes.get(c)
            if not q:
                continue
            p = q.get('pct', 0)
            pcts.append(p)
            if p > 0:
                up += 1
            if p >= 5:
                up5 += 1
            if p >= _limit_threshold(c):
                lu += 1
            tops.append((c, p))
        tops.sort(key=lambda x: -x[1])
        top3 = [{'code': c, 'name': names.get(c, c), 'pct': round(p, 2)} for c, p in tops[:3]]
        return {'members': len(code_set), 'quoted': len(pcts),
                'avg': round(sum(pcts) / len(pcts), 2) if pcts else 0,
                'up': up, 'up5': up5, 'limitup': lu, 'top': top3}

    chains = []
    for cid, c in ci.items():
        tiers = []
        chain_codes = set()
        for t in c['tiers']:
            cons = []
            for con in t['concepts']:
                m = con_members.get(con, set())
                chain_codes |= m
                a = agg(m)
                a['concept'] = con
                cons.append(a)
            cons.sort(key=lambda x: -x['avg'])
            tiers.append({'tier': t['tier'], 'concepts': cons})
        summ = agg(chain_codes)
        chains.append({'id': cid, 'name': c['name'], 'summary': summ, 'tiers': tiers})
    chains.sort(key=lambda x: -x['summary']['avg'])

    out = {'success': True, 'ts': datetime.now().strftime('%H:%M:%S'), 'chains': chains}
    _chain_board_cache['data'] = out
    _chain_board_cache['ts'] = now
    return jsonify(out)


