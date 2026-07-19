# -*- coding: utf-8 -*-
"""server.bp_hot - 热门股追踪: track/compute/concept-track/explosive/daymap/
ladder/newhigh/concept-daymap/breadth/hnrlive (不含 caizhaomao)。

原 api_server.py 行 282-339 (explosive持久化) + 3959-5217 + 5218-5471 (explosive) +
5472-5600 (daymap) + 5600-6186 (ladder/newhigh/...) + 6309-6436 (hnrlive)。
"""
import os
import re
import json
import time
import struct
import threading
from datetime import datetime, timedelta
from collections import defaultdict, deque
from flask import Blueprint, request, jsonify

from .common import (
    ht,  # noqa: F401
    app,
    _load_concept_members,
    _read_day_closes_batch,
    _read_lc1_minutes,
    _read_lc1_minutes_batch,
    trading_days_in_range,
    _limit_threshold,
    _bar_date8,
    _dlog,
)

hot_bp = Blueprint('hot', __name__)


@hot_bp.route('/api/hot/explosive/picks', methods=['GET'])

def api_explosive_picks_get():
    """获取已保存的爆发股已选列表"""
    return jsonify({'success': True, 'picks': _load_exp_picks()})



@hot_bp.route('/api/hot/explosive/picks', methods=['POST'])

def api_explosive_picks_save():
    """保存爆发股已选列表 (全量覆盖)"""
    try:
        data = request.get_json(silent=True) or {}
        picks = data.get('picks', [])
        _save_exp_picks(picks)
        return jsonify({'success': True, 'count': len(picks)})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



# ===== 爆发股扫描结果持久化 (.exp_scan.json) =====

EXP_SCAN_FILE = '.exp_scan.json'



def _load_exp_scan():
    """读取上次扫描结果 {month, window, threshold, candidates, saved_at}"""
    try:
        if os.path.exists(EXP_SCAN_FILE):
            with open(EXP_SCAN_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return None



def _save_exp_scan(scan_data):
    try:
        with open(EXP_SCAN_FILE, 'w', encoding='utf-8') as f:
            json.dump(scan_data, f, ensure_ascii=False)
    except Exception as e:
        print(f'保存爆发股扫描结果失败: {e}')



@hot_bp.route('/api/hot/explosive/scan-result', methods=['GET'])

def api_explosive_scan_result_get():
    """获取上次保存的扫描结果"""
    return jsonify({'success': True, 'scan': _load_exp_scan()})



@hot_bp.route('/api/hot/explosive/scan-result', methods=['POST'])

def api_explosive_scan_result_save():
    """保存扫描结果 (全量覆盖)"""
    try:
        data = request.get_json(silent=True) or {}
        _save_exp_scan(data)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



def hot_track_page():
    """热门股追踪页面"""
    resp = make_response(render_template('hot_track.html'))
    resp.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    return resp



@hot_bp.route('/api/hot/track', methods=['GET'])

def api_hot_track():
    """热门股追踪数据API"""
    import re
    
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    sort = request.args.get('sort', 'stock_count')
    with_price = request.args.get('price', '1') not in ('0', 'false', 'no')
    # 筛选条件: filter_and(与门, 全部满足) / filter_or(或门, 任一满足), 逗号分隔的条件ID
    filter_and = [x for x in request.args.get('filter_and', '').split(',') if x] or None
    filter_or = [x for x in request.args.get('filter_or', '').split(',') if x] or None
    
    # 验证日期格式
    if not (start and re.match(r'^\d{8}$', start)) or not (end and re.match(r'^\d{8}$', end)):
        return jsonify({'success': False, 'error': '日期格式错误, 请用 YYYYMMDD'}), 400
    
    if start > end:
        start, end = end, start
    
    if sort not in ('stock_count', 'days', 'times', 'total'):
        sort = 'stock_count'
    
    # 转换排序参数
    sort_map = {'times': 'stock_count', 'total': 'days'}
    actual_sort = sort_map.get(sort, sort)
    
    try:
        data = ht.track_hot_stocks(start, end, sort=actual_sort, with_price=with_price,
                                   filter_and=filter_and, filter_or=filter_or)
    except Exception as e:
        return jsonify({'success': False, 'error': f'统计失败: {e}'}), 500
    
    # 检查数据完整性
    missing_stats = {
        'pct': 0,        # 缺少涨幅
        'ma10': 0,       # 缺少MA10
        'below_ma10': 0, # 缺少跌破状态
        'details': []    # 详细信息
    }
    
    if with_price:
        for day in data.get('by_date', []):
            for block in day.get('blocks', []):
                for stock in block.get('stocks', []):
                    track = stock.get('track', {}).get(day['date'], {})
                    # 停牌日不计为缺失
                    if track.get('suspended'):
                        continue
                    missing = []
                    
                    if track.get('pct') is None:
                        missing_stats['pct'] += 1
                        missing.append('涨幅')
                    
                    if track.get('ma10') is None:
                        missing_stats['ma10'] += 1
                        missing.append('MA10')
                    
                    if track.get('below_ma10') is None:
                        missing_stats['below_ma10'] += 1
                        missing.append('跌破状态')
                    
                    if missing and len(missing_stats['details']) < 10:
                        missing_stats['details'].append({
                            'code': stock['code'],
                            'name': stock['name'],
                            'date': day['date'],
                            'missing': missing
                        })
    
    # 瘦身: by_date 里每只股票只保留当天的 track(前端只用 s.track[当天])
    _slim_by_date_tracks(data)

    return jsonify({
        'success': True, 
        **data,
        'data_integrity': missing_stats
    })



@hot_bp.route('/api/hot/dates', methods=['GET'])

def api_hot_dates():
    """返回数据库中已入库的所有日期(热门股追踪的可选范围)。
    自动追加今天(如果是工作日且未入库), 使同花顺概念追踪能选到今天。"""
    import db as _db
    from datetime import datetime as _dt
    try:
        dates = sorted(_db.get_submitted_dates())
    except _db.DBError as e:
        return jsonify({'success': True, 'dates': [], 'db_connected': False, 'db_message': str(e)})
    # 追加今天(工作日且不在列表中)
    today_str = _dt.now().strftime('%Y%m%d')
    if _dt.now().weekday() < 5 and today_str not in dates:
        dates.append(today_str)
        dates.sort()
    return jsonify({'success': True, 'dates': dates, 'db_connected': True})



_concept_daily_zt_cache = {}  # {date_int: {code: name}} 每日涨停股缓存



def _read_day_files_zt(date_int, all_codes):
    """读通达信本地 .day 日线文件, 判定某日涨停股。
    .day 文件每条记录32字节: date(I) open(I*100) high(I*100) low(I*100) close(I*100)
                            amount(f) vol(I) reserved(I)
    3189只主板股票约1秒读完, 无网络依赖。
    返回 {code: name} 涨停股字典(name暂用code代替, .day不含名称)。
    """
    import struct
    import os
    try:
        import tdx_source as _ts
        base = os.path.join(_ts.TDX_INSTALL_DIR, 'Vipdoc')
    except Exception:
        return {}
    sh_dir = os.path.join(base, 'sh', 'lday')
    sz_dir = os.path.join(base, 'sz', 'lday')
    if not (os.path.isdir(sh_dir) and os.path.isdir(sz_dir)):
        return {}

    result = {}
    target = int(date_int)
    for code in all_codes:
        if code.startswith('6'):
            fp = os.path.join(sh_dir, f'sh{code}.day')
        else:
            fp = os.path.join(sz_dir, f'sz{code}.day')
        try:
            with open(fp, 'rb') as f:
                data = f.read()
            n = len(data) // 32
            # 倒序找目标日期(.day按时间升序, 倒序更快)
            for i in range(n - 1, 0, -1):
                rec = data[i * 32:(i + 1) * 32]
                d = struct.unpack('I', rec[:4])[0]
                if d == target:
                    close = struct.unpack('I', rec[16:20])[0] / 100.0
                    prev_rec = data[(i - 1) * 32:i * 32]
                    prev_close = struct.unpack('I', prev_rec[16:20])[0] / 100.0
                    if prev_close > 0 and (close - prev_close) / prev_close * 100 >= 9.8:
                        result[code] = code
                    break
                if d < target:
                    break  # 已越过目标日期, 文件无该日数据
        except Exception:
            continue
    return result



def _read_day_closes_batch(codes):
    """批量读通达信本地 .day 文件, 返回每只股票的 {date_int: close} 日线收盘价。
    支持全市场(沪市6/科创板688 -> sh, 深市0/创业板300 -> sz)。
    约5000只2秒读完, 无网络依赖。
    返回 {code: {date_int: close_price}}。
    """
    import struct
    import os
    try:
        import tdx_source as _ts
        base = os.path.join(_ts.TDX_INSTALL_DIR, 'Vipdoc')
    except Exception:
        return {}
    sh_dir = os.path.join(base, 'sh', 'lday')
    sz_dir = os.path.join(base, 'sz', 'lday')
    if not (os.path.isdir(sh_dir) and os.path.isdir(sz_dir)):
        return {}

    result = {}
    for code in codes:
        # 沪市: 6开头(主板60/科创板688) / 9开头 -> sh
        # 深市: 0/3开头(主板00/创业板300) -> sz
        if code.startswith('6') or code.startswith('9'):
            fp = os.path.join(sh_dir, f'sh{code}.day')
        else:
            fp = os.path.join(sz_dir, f'sz{code}.day')
        try:
            with open(fp, 'rb') as f:
                data = f.read()
            closes = {}
            for i in range(0, len(data), 32):
                rec = data[i:i + 32]
                if len(rec) < 32:
                    break
                d = struct.unpack('I', rec[:4])[0]
                close = struct.unpack('I', rec[16:20])[0] / 100.0
                if close > 0:
                    closes[str(d)] = close
            if closes:
                result[code] = closes
        except Exception:
            continue
    return result



def _read_lc1_minutes(code, date_int):
    """读通达信本地 .lc1 1分钟线文件, 提取指定日期的逐分钟价格。
    .lc1 每条记录32字节: date(I) time(I) open(f) high(f) low(f) close(f) amount(f) vol(I)
    返回 [price_per_minute, ...] (用close作为price, 对齐mootdx minutes的price列) 或 None。
    """
    import struct
    import os
    try:
        import tdx_source as _ts
        base = os.path.join(_ts.TDX_INSTALL_DIR, 'Vipdoc')
    except Exception:
        return None
    if code.startswith('6') or code.startswith('9'):
        fp = os.path.join(base, 'sh', 'minline', f'sh{code}.lc1')
    else:
        fp = os.path.join(base, 'sz', 'minline', f'sz{code}.lc1')
    if not os.path.exists(fp):
        return None

    target = int(date_int)
    try:
        with open(fp, 'rb') as f:
            data = f.read()
        prices = []
        for i in range(0, len(data), 32):
            rec = data[i:i + 32]
            if len(rec) < 32:
                break
            d = struct.unpack('I', rec[:4])[0]
            if d < target:
                continue
            if d > target:
                break  # 已越过目标日期
            # d == target: 取close作为price
            close = struct.unpack('f', rec[16:20])[0]
            if close > 0:
                prices.append(round(close, 3))
        return prices if prices else None
    except Exception:
        return None



def _read_lc1_minutes_batch(codes, date_int):
    """批量读本地 .lc1 文件, 提取指定日期的逐分钟分时。
    返回 {code: [price, ...]}, 只含成功读取的股票。
    若 minline 目录无 .lc1 文件(未下载分钟线), 返回空dict, 调用方回退mootdx。
    """
    import os
    try:
        import tdx_source as _ts
        base = os.path.join(_ts.TDX_INSTALL_DIR, 'Vipdoc')
    except Exception:
        return {}
    sh_dir = os.path.join(base, 'sh', 'minline')
    sz_dir = os.path.join(base, 'sz', 'minline')
    if not os.path.isdir(sh_dir) and not os.path.isdir(sz_dir):
        return {}

    result = {}
    for code in codes:
        p = _read_lc1_minutes(code, date_int)
        if p:
            result[code] = p
    return result



def _fetch_daily_zt_codes(date_int, concept_map, cancel_check=None):
    """判定某日涨停股(主板60/00, pct>=9.8%), 按日缓存。
    优先级:
    - 今天: tqcenter batch_pricevol(走本地通达信, ~0.8秒) > mootdx quotes兜底
    - 历史: 本地 .day 文件(~1秒) > mootdx bars逐只兜底(极慢)
    cancel_check: 可选的无参回调, 返回True时中止
    返回 {code: name} 涨停股字典。
    """
    if date_int in _concept_daily_zt_cache:
        return _concept_daily_zt_cache[date_int]

    # 收集所有主板成分股代码
    all_codes = set()
    for codes in concept_map.values():
        all_codes.update(codes)
    all_codes = sorted(all_codes)

    result = {}
    from datetime import datetime as _dt
    is_today = (date_int == int(_dt.now().strftime('%Y%m%d')))
    src = ''

    # ---------- 优先路径 ----------
    try:
        if is_today:
            # 今天: tqcenter 批量报价(本地通达信, 不封IP, ~0.8秒)
            import tdx_source as _ts
            if _ts.is_available():
                pv = _ts.batch_pricevol(all_codes)
                if pv:
                    for code, v in pv.items():
                        if float(v.get('pct', 0)) >= 9.8:
                            result[code] = code
                    src = 'tqcenter'
        else:
            # 历史: 本地 .day 文件(~1秒, 无网络)
            r = _read_day_files_zt(date_int, all_codes)
            if r:
                result = r
                src = 'dayfile'
    except Exception as e:
        print(f'[概念] 优先路径失败, 回退mootdx: {e}')

    # ---------- 兜底: mootdx (优先路径无结果时) ----------
    if not result and not (cancel_check and cancel_check()):
        try:
            import hot_track as ht
            import pandas as pd
            with ht._get_tdx_lock():
                client = ht._get_tdx_client()

                if is_today:
                    # 兜底: mootdx 批量 quotes (80只/批)
                    frames = []
                    for i in range(0, len(all_codes), 80):
                        if cancel_check and cancel_check():
                            break
                        batch = all_codes[i:i + 80]
                        df = ht._tdx_call_with_timeout(client, 'quotes', timeout=8, symbol=batch)
                        if df is not None and not df.empty:
                            frames.append(df)
                    if frames:
                        all_q = pd.concat(frames, ignore_index=True)
                        for col in ('last_close', 'price'):
                            if col in all_q.columns:
                                all_q[col] = pd.to_numeric(all_q[col], errors='coerce')
                        valid = all_q[(all_q['last_close'] > 0) & (all_q['price'] > 0)].copy()
                        valid['pct'] = (valid['price'] - valid['last_close']) / valid['last_close'] * 100
                        valid['code'] = valid['code'].astype(str).str.zfill(6)
                        for _, r in valid.iterrows():
                            if float(r['pct']) >= 9.8:
                                name = r.get('name', '') if 'name' in valid.columns else ''
                                result[str(r['code'])] = str(name)
                        src = src or 'mootdx-quotes'
                else:
                    # 兜底: mootdx 逐只 bars (极慢~16分钟, 仅在.day文件缺失时)
                    target = str(date_int)
                    for code in all_codes:
                        if cancel_check and cancel_check():
                            break
                        try:
                            df = ht._tdx_call_with_timeout(client, 'bars', timeout=5, symbol=code, frequency=9, offset=10)
                            if df is None or df.empty:
                                continue
                            for idx in range(len(df)):
                                row = df.iloc[idx]
                                dt = str(row.get('datetime', ''))
                                if dt[:10].replace('-', '') == target:
                                    close = float(row['close'])
                                    if idx > 0:
                                        prev_close = float(df.iloc[idx - 1]['close'])
                                    else:
                                        prev_close = float(row['open'])
                                    if prev_close > 0:
                                        pct = (close - prev_close) / prev_close * 100
                                        if pct >= 9.8:
                                            result[code] = code
                                    break
                        except Exception:
                            pass
                    src = src or 'mootdx-bars'
        except Exception as e:
            print(f'[概念] mootdx兜底失败: {e}')

    _concept_daily_zt_cache[date_int] = result
    print(f'[概念] 涨停判定 date={date_int} [{src}]: {len(result)}只')
    return result



# ===== 同花顺概念追踪: 异步任务 (带进度+取消) =====

_concept_tasks = {}  # {task_id: {status, progress, cur_date, total_dates, logs, result, error, _cancel, days_partial}}

# 按天结果缓存(跨任务复用): {date_str: day_result}。今天的数据不缓存(实时变化)。

# 持久化到磁盘, 重启后历史日期不再重复计算(历史分时数据不变, 结果确定)。

_CONCEPT_DAY_CACHE_FILE = '.concept_day_cache.json'

_concept_day_cache = {}

_concept_day_cache_lock = threading.Lock()



def _load_concept_day_cache():
    global _concept_day_cache
    try:
        if os.path.exists(_CONCEPT_DAY_CACHE_FILE):
            with open(_CONCEPT_DAY_CACHE_FILE, 'r', encoding='utf-8') as f:
                _concept_day_cache = json.load(f) or {}
            print(f'[概念] 已载入按天缓存 {len(_concept_day_cache)} 天(历史日期不再重算)')
    except Exception as e:
        print(f'[概念] 载入按天缓存失败: {e}')
        _concept_day_cache = {}



def _save_concept_day_cache():
    """全量落盘(按天缓存, 数量不大, 几十~几百天)。"""
    try:
        with _concept_day_cache_lock:
            with open(_CONCEPT_DAY_CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(_concept_day_cache, f, ensure_ascii=False)
    except Exception as e:
        print(f'[概念] 保存按天缓存失败: {e}')


_load_concept_day_cache()


@hot_bp.route('/api/hot/concept-track', methods=['GET'])

def api_hot_concept_track():
    """同花顺概念涨停追踪: 启动异步任务, 返回 task_id。
    后续用 /api/hot/concept-track/status/<task_id> 轮询进度。
    """
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    force = request.args.get('force') in ('1', 'true', 'True', 'yes')
    if not start or not end:
        return jsonify({'success': False, 'error': '需要 start 和 end 参数'}), 400

    import uuid
    task_id = str(uuid.uuid4())[:8]
    _concept_tasks[task_id] = {
        'status': 'pending', 'progress': 0,
        'cur_date': '', 'total_dates': 0, 'cur_idx': 0,
        'start': start, 'end': end, 'force': force,
        'logs': [], 'result': None, 'error': None,
        '_cancel': False,
    }
    threading.Thread(target=_concept_track_task,
                     args=(task_id, start, end, force), daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id,
                    'status_url': f'/api/hot/concept-track/status/{task_id}'})



@hot_bp.route('/api/hot/concept-track/status/<task_id>', methods=['GET'])

def api_hot_concept_track_status(task_id):
    """查询概念追踪任务进度; ?since=N 增量拉日志; 完成时返回 result"""
    if task_id not in _concept_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    t = _concept_tasks[task_id]
    since = request.args.get('since', default=0, type=int)
    resp = {
        'success': True, 'status': t['status'],
        'progress': t['progress'],
        'cur_date': t['cur_date'],
        'total_dates': t['total_dates'],
        'cur_idx': t['cur_idx'],
        'logs': t['logs'][since:], 'log_count': len(t['logs']),
    }
    if t['status'] == 'completed':
        resp['result'] = t['result']
        # 完成后清理任务(保留30秒让前端取结果)
    elif t['status'] == 'failed':
        resp['error'] = t['error']
    elif t['status'] == 'cancelled':
        resp['error'] = t.get('error', '已取消')
    return jsonify(resp)



@hot_bp.route('/api/hot/concept-track/cancel/<task_id>', methods=['POST'])

def api_hot_concept_track_cancel(task_id):
    """取消概念追踪任务"""
    if task_id not in _concept_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    t = _concept_tasks[task_id]
    if t['status'] in ('completed', 'failed', 'cancelled'):
        return jsonify({'success': True, 'status': t['status'], 'msg': '任务已结束'})
    t['_cancel'] = True
    return jsonify({'success': True, 'msg': '已发送取消信号'})



def _concept_track_task(task_id, start, end, force=False):
    """概念追踪后台任务: 逐日拉取涨停分时, 重建概念涨停轨迹。
    force=True 时忽略按天缓存, 强制重算并覆盖缓存。"""
    global _concept_tasks
    t = _concept_tasks[task_id]
    t['status'] = 'running'

    def log(msg):
        t['logs'].append(msg)

    try:
        import db as _db
        # 用 zt_daily 的日期作为交易日参考
        conn = _db.get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                SELECT DISTINCT to_char(trade_date, 'YYYYMMDD') AS d
                FROM zt_daily
                WHERE trade_date BETWEEN %s AND %s
                ORDER BY d
            """, (f'{start[:4]}-{start[4:6]}-{start[6:8]}', f'{end[:4]}-{end[4:6]}-{end[6:8]}'))
            trade_dates = [r[0] for r in cur.fetchall()]
        conn.close()

        # 也加入今天(如果今天在范围内且是交易日)
        from datetime import datetime as _dt
        today_str = _dt.now().strftime('%Y%m%d')
        if start <= today_str <= end and today_str not in trade_dates:
            if _dt.now().weekday() < 5:
                trade_dates.append(today_str)
                trade_dates.sort()

        if not trade_dates:
            t['status'] = 'failed'
            t['error'] = '该日期范围无交易日'
            return

        t['total_dates'] = len(trade_dates)
        log(f'共 {len(trade_dates)} 个交易日: {", ".join(trade_dates)}')

        # 概念体系 + code->concepts 反向映射
        concept_map = _load_concept_members()
        if not concept_map:
            t['status'] = 'failed'
            t['error'] = '无概念成分股数据'
            return
        code_to_concepts = {}
        for concept, codes in concept_map.items():
            for c in codes:
                code_to_concepts.setdefault(c, set()).add(concept)

        days_result = []
        from datetime import datetime as _dt
        _today_str = _dt.now().strftime('%Y%m%d')
        for di, date_str in enumerate(trade_dates):
            # 检查取消
            if t['_cancel']:
                t['status'] = 'cancelled'
                t['error'] = f'用户取消 (已完成 {di}/{len(trade_dates)} 天)'
                log(f'⚠️ 用户取消, 已完成 {di}/{len(trade_dates)} 天')
                return

            t['cur_idx'] = di + 1
            t['cur_date'] = date_str
            t['progress'] = int(di / max(len(trade_dates), 1) * 100)
            date_int = int(date_str)
            d_display = f'{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}'

            # 缓存命中: 非今天且已计算过且非强制刷新 -> 直接复用, 跳过重算
            if not force and date_str != _today_str and date_str in _concept_day_cache:
                cached = _concept_day_cache[date_str]
                days_result.append(cached)
                top_name = cached['top10'][0]['concept'] if cached['top10'] else '无'
                top_cnt = cached['top10'][0]['zt_count'] if cached['top10'] else 0
                log(f'⏩ [{di+1}/{len(trade_dates)}] {d_display}: 命中缓存, Top概念 {top_name}({top_cnt}只)')
                continue

            log(f'📍 [{di+1}/{len(trade_dates)}] 处理 {d_display} ...')

            # 用日K线判定涨停股(不依赖zt_stocks)
            log(f'   {d_display}: 判定涨停股(拉日K)...')
            zt_codes_map = _fetch_daily_zt_codes(date_int, concept_map,
                                                  cancel_check=lambda: t['_cancel'])
            if t['_cancel']:
                t['status'] = 'cancelled'
                t['error'] = f'用户取消 (已完成 {di}/{len(trade_dates)} 天)'
                log(f'⚠️ 用户取消, 已完成 {di}/{len(trade_dates)} 天')
                return
            zt_codes = set(zt_codes_map.keys())
            if not zt_codes:
                log(f'   {d_display}: 无涨停股, 跳过')
                continue
            log(f'   {d_display}: 涨停{len(zt_codes)}只, 拉分时数据...')

            # 拉分时(按日期缓存)
            minute_map = _fetch_zt_minutes(zt_codes, date_int,
                                            cancel_check=lambda: t['_cancel'])
            if t['_cancel']:
                t['status'] = 'cancelled'
                t['error'] = f'用户取消 (已完成 {di}/{len(trade_dates)} 天)'
                log(f'⚠️ 用户取消, 已完成 {di}/{len(trade_dates)} 天')
                return
            if not minute_map:
                log(f'   {d_display}: 无分时数据, 跳过')
                continue
            log(f'   {d_display}: 分时数据就绪 {len(minute_map)}/{len(zt_codes)}只')

            # 逐分钟统计封板
            from collections import defaultdict
            minute_concept_counts = defaultdict(lambda: defaultdict(int))
            final_counts = defaultdict(int)
            concept_zt_stocks = defaultdict(list)

            for code, prices in minute_map.items():
                if code not in zt_codes:
                    continue
                concepts = code_to_concepts.get(code, set())
                if not concepts:
                    continue
                max_p = max(prices)
                for i, p in enumerate(prices):
                    if abs(p - max_p) < 0.005:
                        for con in concepts:
                            minute_concept_counts[i][con] += 1
                # 收盘封板
                if abs(prices[-1] - max_p) < 0.005:
                    for con in concepts:
                        final_counts[con] += 1
                        concept_zt_stocks[con].append({
                            'code': code, 'name': zt_codes_map.get(code, '')
                        })

            if not minute_concept_counts:
                log(f'   {d_display}: 无封板数据, 跳过')
                continue

            # Top10 概念(按最终封板数)
            top10_sorted = sorted(final_counts.items(), key=lambda x: x[1], reverse=True)[:10]
            top10_concepts = set(c for c, _ in top10_sorted)

            # 柱状图数据
            top10 = []
            for con, cnt in top10_sorted:
                if cnt == 0:
                    continue
                top10.append({
                    'concept': con,
                    'zt_count': cnt,
                    'zt_stocks': concept_zt_stocks[con][:20],
                })

            # 折线图 timeline (只保留 top10 概念, 只输出变化点)
            def min_idx_to_trade_min(idx):
                return idx + 5 if idx < 120 else idx + 6

            all_mins = sorted(minute_concept_counts.keys())
            timeline = []
            prev_counts = {}
            for m in all_mins:
                counts = {c: minute_concept_counts[m].get(c, 0) for c in top10_concepts if minute_concept_counts[m].get(c, 0) > 0}
                if counts != prev_counts:
                    tm = min_idx_to_trade_min(m)
                    timeline.append({
                        'ts': _time_to_label(tm),
                        'trade_min': tm,
                        'counts': counts,
                    })
                    prev_counts = counts

            day_result = {
                'date': date_str,
                'top10': top10,
                'timeline': timeline,
                'top10_ever': sorted(top10_concepts),
            }
            days_result.append(day_result)
            # 非今天的结果存入跨任务缓存并落盘(今天实时变化, 不缓存)
            if date_str != _today_str:
                _concept_day_cache[date_str] = day_result
                _save_concept_day_cache()
            log(f'   ✅ {d_display}: 涨停{len(zt_codes)}只, Top概念 {top10[0]["concept"] if top10 else "无"}({top10[0]["zt_count"] if top10 else 0}只)')

        t['progress'] = 100
        t['result'] = {
            'success': True,
            'start': start,
            'end': end,
            'days': days_result,
        }
        t['status'] = 'completed'
        log(f'🎉 全部完成: {len(days_result)}/{len(trade_dates)} 天有数据')

    except Exception as e:
        import traceback
        traceback.print_exc()
        t['status'] = 'failed'
        t['error'] = f'{type(e).__name__}: {e}'



def _build_missing_report(result):
    """
    扫描计算结果, 按"每日 -> 板块"列出当天在榜但缺涨幅/缺MA10的个股。
    返回: (missing_by_date, missing_codes)
      missing_by_date: [{date, blocks:[{block, stocks:[{code,name,missing:[...]}]}]}]
      missing_codes: 去重的缺失股票代码列表
    注意: 停牌日(suspended=True)不计为缺失——当日无交易, 无涨幅/MA10 是正常的。
    """
    missing_by_date = []
    missing_codes = set()
    for day in result.get('by_date', []):
        d = day['date']
        blocks_out = []
        for b in day.get('blocks', []):
            miss_stocks = []
            for s in b.get('stocks', []):
                cell = s.get('track', {}).get(d) or {}
                # 停牌日跳过: 当日无交易, 涨幅/MA10 缺失是正常的
                if cell.get('suspended'):
                    continue
                # 不再跳过 present=False: 历史入榜跟踪股当日未涨停时仍需涨幅/MA10
                missing = []
                if cell.get('pct') is None:
                    missing.append('涨幅')
                if cell.get('ma10') is None:
                    missing.append('MA10')
                if missing:
                    miss_stocks.append({'code': s['code'], 'name': s['name'], 'missing': missing})
                    missing_codes.add(s['code'])
            if miss_stocks:
                blocks_out.append({'block': b['block'], 'stocks': miss_stocks})
        if blocks_out:
            missing_by_date.append({'date': d, 'blocks': blocks_out})
    return missing_by_date, sorted(missing_codes)



def hot_compute_task(task_id, start, end, with_price):
    _dlog(f'=== hot_compute_task 启动 task={task_id} {start}~{end} price={with_price} ===')
    """
    后台热门股计算任务(分阶段):
      阶段1 build: 载入板块/个股
      阶段2 price: 获取行情
      -> 检查数据缺失: 缺失则暂停询问(再次同步/跳过), 因为数据不全时移除判断不可靠
      阶段3 remove: 用户确认后才应用移除规则, 出最终结果
    """
    global hot_tasks
    t = hot_tasks[task_id]
    t['status'] = 'running'

    def prog(stage, msg, cur=None, total=None):
        t['stage'] = stage
        t['logs'].append({'stage': stage, 'msg': msg, 'cur': cur, 'total': total})
        if cur is not None and total:
            t['progress'] = int(cur / max(total, 1) * 100)

    try:
        missing_by_date, missing_codes = [], []

        if with_price:
            # 循环: 构建+行情(不移除) -> 检查缺失 -> 询问; 直到无缺失或用户跳过
            while True:
                _dlog(f'[{task_id}] 第二步: 开始 track_hot_stocks apply_removal=False')
                data = ht.track_hot_stocks(start, end, with_price=True, progress=prog,
                                           source='db', apply_removal=False)

                # 从 fetch_report 里取出本轮拉取失败的票
                fetch_report = data.get('fetch_report') or {}
                fetch_failed = fetch_report.get('failed', [])  # [{code, reason}, ...]
                _dlog(f'[{task_id}] fetch_report: cached={fetch_report.get("cached")} success={fetch_report.get("success")} failed={len(fetch_failed)}')

                # 第二步结束: 如果有拉取失败的票, 立即进入 awaiting
                if fetch_failed and not t.get('_skip'):
                    _dlog(f'[{task_id}] 进入 awaiting，失败票: {[f["code"] for f in fetch_failed]}')
                    fail_codes = [f['code'] for f in fetch_failed]
                    lines = [f"{f['code']} ({f['reason']})" for f in fetch_failed]
                    # 构建 await_stocks 详情(带名称)供前端勾选移除
                    _name_map = {}
                    for _day in data.get('by_date', []):
                        for _b in _day.get('blocks', []):
                            for _s in _b.get('stocks', []):
                                _name_map.setdefault(_s['code'], _s.get('name', ''))
                    t['await_stocks'] = [{'code': f['code'],
                                          'name': _name_map.get(f['code'], ''),
                                          'reason': f['reason']} for f in fetch_failed]
                    prog('await',
                         f'行情获取完成，以下 {len(fetch_failed)} 只股票无法获取数据:\n'
                         + '\n'.join(f'  {l}' for l in lines)
                         + '\n可「再次同步」重试、「移除选中」剔除不再追踪，或「跳过」直接进入第三步',
                         None, None)
                    t['missing_report'] = []   # 此时还没做 remove, 用 await_stocks 代替
                    t['missing_codes'] = fail_codes
                    t['status'] = 'awaiting'
                    t['_event'].clear()
                    t['_event'].wait()
                    action = t.get('_action')
                    t['_action'] = None
                    t['status'] = 'running'
                    _dlog(f'[{task_id}] awaiting 用户选择: action={action}')
                    if action == 'resync':
                        prog('price', f'再次同步 {len(fail_codes)} 只个股行情(通达信)…', 0, len(fail_codes))
                        ok_cnt, still_fail = 0, []
                        for i, c in enumerate(fail_codes):
                            # 先清除之前写入的 None 占位和 _no_data 标记, 让 fetch 重新尝试
                            _cache = ht._load_price_cache()
                            with ht._cache_lock:
                                _cache.pop(f'{c}_no_data', None)
                                _cache.pop(f'{c}_no_data_reason', None)
                                for _d in data.get('dates', []):
                                    for _sfx in ('', '_ma10', '_below_ma10', '_20d', '_60d'):
                                        _cache.pop(f'{c}_{_d}{_sfx}', None)
                                ht._save_price_cache()
                            try:
                                if ht.fetch_range(c, start, end):
                                    ok_cnt += 1
                                else:
                                    still_fail.append(c)
                            except Exception:
                                still_fail.append(c)
                            prog('price', f'同步 {i + 1}/{len(fail_codes)} ({c})', i + 1, len(fail_codes))
                        msg = f'同步完成: 成功 {ok_cnt}/{len(fail_codes)}'
                        if still_fail:
                            msg += f', 仍失败 {len(still_fail)} 只: ' + ', '.join(still_fail)
                        prog('price', msg, len(fail_codes), len(fail_codes))
                        continue  # 重新构建 + 再检查
                    else:
                        t['_skip'] = True
                        # 跳过: 保留 None 占位, 直接进第三步

                missing_by_date, missing_codes = _build_missing_report(data)
                if not missing_codes:
                    break
                # 兜底: fetch 全部成功但缓存里仍有 None (理论上不应再触发)
                n = sum(len(s['stocks']) for day in missing_by_date for s in day['blocks'])
                # 构建 await_stocks 详情供前端勾选移除
                _await_map = {}
                for _day in missing_by_date:
                    for _b in _day['blocks']:
                        for _s in _b['stocks']:
                            if _s['code'] not in _await_map:
                                _await_map[_s['code']] = {'code': _s['code'], 'name': _s['name'],
                                                           'reason': '缺' + '、'.join(_s['missing'])}
                t['await_stocks'] = list(_await_map.values())
                t['missing_report'] = missing_by_date
                t['missing_codes'] = missing_codes
                prog('await', f'检测到 {len(missing_codes)} 只个股共 {n} 处缺数据(缺涨幅/MA10)。'
                              f'可「再次同步」补数据、「移除选中」剔除不再追踪，或「跳过」按现有数据执行', None, None)
                t['status'] = 'awaiting'
                t['_event'].clear()
                t['_event'].wait()
                action = t.get('_action')
                t['_action'] = None
                t['status'] = 'running'
                if action == 'resync':
                    prog('price', f'再次同步 {len(missing_codes)} 只个股行情(通达信)…', 0, len(missing_codes))
                    ok_cnt, fail_list = 0, []
                    for i, c in enumerate(missing_codes):
                        try:
                            if ht.fetch_range(c, start, end):
                                ok_cnt += 1
                            else:
                                fail_list.append(c)
                        except Exception:
                            fail_list.append(c)
                        prog('price', f'同步 {i + 1}/{len(missing_codes)} ({c})', i + 1, len(missing_codes))
                    msg = f'同步完成: 成功 {ok_cnt}/{len(missing_codes)}'
                    if fail_list:
                        msg += f', 失败 {len(fail_list)} 只'
                    prog('price', msg, len(missing_codes), len(missing_codes))
                    continue
                else:
                    t['_skip'] = True
                    break

        # 阶段3: 直接在第二步的数据上应用移除规则, 不再重新拉行情
        _dlog(f'[{task_id}] 第三步: 应用移除规则 with_price={with_price}')
        prog('remove', '数据就绪，应用移除规则(跌破10日线次日删除)…', 0, 1)
        if not with_price:
            # 未拉行情时才需要重新跑
            data = ht.track_hot_stocks(start, end, with_price=False, progress=prog,
                                       source='db', apply_removal=True)
        else:
            # 复用第二步数据, 只重跑移除逻辑部分(含用户手动剔除的票)
            _remove_codes = t.get('_remove_codes') or []
            data = ht.apply_removal_rules(data, progress=prog, manual_remove_codes=_remove_codes)
        final = data
        final['missing_report'] = missing_by_date
        final['missing_codes'] = missing_codes
        n = sum(len(s['stocks']) for day in missing_by_date for s in day['blocks'])
        _removed = t.get('_remove_codes') or []
        if _removed:
            _removed_set = set(_removed)
            _still_missing = [c for c in missing_codes if c not in _removed_set]
            _msg = f'计算完成(已移除 {len(_removed)} 只不再追踪'
            if _still_missing:
                _msg += f', 另跳过 {len(_still_missing)} 只缺数据个股'
            _msg += ')'
            prog('done', _msg, 1, 1)
        elif missing_codes:
            prog('done', f'计算完成(跳过 {len(missing_codes)} 只缺数据个股, 共 {n} 处)', 1, 1)
        else:
            prog('done', '计算完成，数据完整', 1, 1)
        _dlog(f'[{task_id}] 计算完成 missing_codes={missing_codes}')
        t['result'] = final
        t['status'] = 'completed'
        t['progress'] = 100
        # 瘦身: 裁剪 by_date 里非当天 track(前端只用 s.track[当天]), 减小体积~95%
        _slim_by_date_tracks(final)
        # 服务端持久化最近结果(供刷新恢复)
        global hot_last
        hot_last = {'start': start, 'end': end, 'price': with_price,
                    'saved_at': time.time(), 'result': final}
        _save_hot_last()
    except Exception as e:
        _dlog(f'[{task_id}] 计算异常: {e}', 'ERROR')
        t['status'] = 'failed'
        t['error'] = str(e)
        t['logs'].append({'stage': 'error', 'msg': f'计算失败: {e}', 'cur': None, 'total': None})



@hot_bp.route('/api/hot/last', methods=['GET'])

def api_hot_last():
    """返回最近一次计算结果(服务端缓存, 刷新恢复用)。"""
    if not hot_last or not hot_last.get('result'):
        return jsonify({'success': True, 'has': False})
    return jsonify({
        'success': True, 'has': True,
        'start': hot_last.get('start'), 'end': hot_last.get('end'),
        'price': hot_last.get('price', True), 'saved_at': hot_last.get('saved_at'),
        'result': hot_last['result'],
    })



@hot_bp.route('/api/hot/compute', methods=['POST'])

def api_hot_compute():
    _dlog(f'POST /api/hot/compute body={request.get_data(as_text=True)}')
    """启动热门股计算(异步, 带分阶段进度)。范围受数据库已入库日期约束。"""
    import db as _db
    data = request.get_json() or {}
    start = (data.get('start') or '').strip()
    end = (data.get('end') or '').strip()
    with_price = data.get('price', True) not in (0, '0', False, 'false', 'no')

    if not (re.match(r'^\d{8}$', start) and re.match(r'^\d{8}$', end)):
        return jsonify({'success': False, 'error': '日期格式错误, 请用 YYYYMMDD'}), 400
    if start > end:
        start, end = end, start

    try:
        db_dates = _db.get_submitted_dates()
    except _db.DBError as e:
        return jsonify({'success': False, 'error': f'数据库未连接: {e}'}), 503
    if not db_dates:
        return jsonify({'success': False, 'error': '数据库暂无已入库数据, 请先在数据同步页入库'}), 404

    dmin, dmax = min(db_dates), max(db_dates)
    if start < dmin or end > dmax:
        return jsonify({'success': False,
                        'error': f'日期超出已入库范围({dmin}~{dmax})'}), 400

    import uuid
    task_id = str(uuid.uuid4())[:8]
    hot_tasks[task_id] = {
        'status': 'pending', 'stage': '', 'progress': 0,
        'logs': [], 'result': None, 'error': None,
        'missing_report': [], 'missing_codes': [], 'await_stocks': [],
        '_event': threading.Event(), '_action': None, '_skip': False,
        '_remove_codes': [],
    }
    threading.Thread(target=hot_compute_task,
                     args=(task_id, start, end, with_price), daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id,
                    'status_url': f'/api/hot/compute/status/{task_id}'})



@hot_bp.route('/api/hot/resync', methods=['POST'])

def api_hot_resync():
    """强制用通达信 mootdx 重新拉取指定个股的行情(覆盖缓存)，用于补齐缺失的涨幅/MA10。"""
    data = request.get_json() or {}
    codes = data.get('codes', [])
    start = (data.get('start') or '').strip()
    end = (data.get('end') or '').strip()
    if not codes:
        return jsonify({'success': False, 'error': '没有需要同步的股票'}), 400

    import time
    ok, failed = 0, []
    for c in codes:
        try:
            if ht.fetch_range(c, start, end):
                ok += 1
            else:
                failed.append(c)
            time.sleep(0.05)
        except Exception as e:
            failed.append(c)
    return jsonify({'success': True, 'fetched': ok, 'total': len(codes), 'failed': failed})



@hot_bp.route('/api/hot/compute/status/<task_id>', methods=['GET'])

def api_hot_compute_status(task_id):
    """查询热门股计算进度; 支持 ?since=N 增量拉日志; awaiting时返回缺失报告; 完成时返回 result"""
    if task_id not in hot_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    t = hot_tasks[task_id]
    since = request.args.get('since', default=0, type=int)
    resp = {
        'success': True, 'status': t['status'], 'stage': t['stage'],
        'progress': t['progress'],
        'logs': t['logs'][since:], 'log_count': len(t['logs']),
    }
    if t['status'] == 'awaiting':
        resp['missing_report'] = t.get('missing_report', [])
        resp['missing_codes'] = t.get('missing_codes', [])
        resp['await_stocks'] = t.get('await_stocks', [])
    elif t['status'] == 'completed':
        resp['result'] = t['result']
    elif t['status'] == 'failed':
        resp['error'] = t['error']
    return jsonify(resp)



@hot_bp.route('/api/hot/compute/resolve', methods=['POST'])

def api_hot_compute_resolve():
    _dlog(f'POST /api/hot/compute/resolve body={request.get_data(as_text=True)}')
    """
    响应缺失数据询问:
      action='resync'  -> 再次同步后重算
      action='skip'    -> 跳过缺失直接出结果
      action='remove'  -> 移除指定 codes(不再追踪)后直接出结果, 需带 codes 列表
    """
    data = request.get_json() or {}
    task_id = data.get('task_id')
    action = data.get('action')
    if task_id not in hot_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    if action not in ('resync', 'skip', 'remove'):
        return jsonify({'success': False, 'error': 'action 必须是 resync / skip / remove'}), 400
    t = hot_tasks[task_id]
    if t['status'] != 'awaiting':
        return jsonify({'success': False, 'error': '任务当前不在等待状态'}), 409
    if action == 'remove':
        codes = data.get('codes', [])
        if not codes or not isinstance(codes, list):
            return jsonify({'success': False, 'error': '移除操作需要提供 codes 列表'}), 400
        t['_remove_codes'] = codes
    t['_action'] = action
    t['_event'].set()  # 唤醒后台线程继续
    return jsonify({'success': True, 'action': action})



@hot_bp.route('/api/hot/sync', methods=['POST'])

def api_hot_sync():
    """
    同步指定板块的股票涨幅数据
    
    参数:
        codes: 股票代码列表
        date: 日期 (YYYYMMDD)
    """
    data = request.get_json() or {}
    codes = data.get('codes', [])
    date = data.get('date', '')
    
    if not codes or not date:
        return jsonify({'success': False, 'error': '缺少参数'}), 400
    
    # 只获取缺失的股票数据
    cache = ht._load_price_cache()
    need_fetch = [c for c in codes if f'{c}_{date}' not in cache]
    
    if not need_fetch:
        return jsonify({'success': True, 'message': '所有股票数据已存在', 'fetched': 0})
    
    # 同步获取
    import time
    success = 0
    failed_codes = []
    for c in need_fetch:
        try:
            if ht.fetch_range(c, date, date):
                success += 1
            else:
                failed_codes.append(c)
            time.sleep(0.5)
        except Exception as e:
            print(f"同步{c}失败: {e}")
            failed_codes.append(c)
    
    return jsonify({
        'success': True,
        'message': f'成功获取 {success}/{len(need_fetch)} 只股票数据',
        'fetched': success,
        'total': len(need_fetch),
        'failed_codes': failed_codes
    })



@hot_bp.route('/api/hot/sync-ma10', methods=['POST'])

def api_hot_sync_ma10():
    """
    同步所有股票的MA10数据
    
    参数:
        start: 开始日期 (YYYYMMDD)
        end: 结束日期 (YYYYMMDD)
    
    返回:
        同步报告，包含成功/失败列表
    """
    data = request.get_json() or {}
    start = data.get('start', '')
    end = data.get('end', '')
    
    if not start or not end:
        return jsonify({'success': False, 'error': '缺少日期参数'}), 400
    
    # 获取该日期范围内的所有股票
    try:
        track_data = ht.track_hot_stocks(start, end, with_price=False)
    except Exception as e:
        return jsonify({'success': False, 'error': f'获取股票列表失败: {e}'}), 500
    
    # 收集所有股票代码和日期
    codes = set()
    dates = track_data.get('dates', [])
    for day in track_data.get('by_date', []):
        for block in day.get('blocks', []):
            for stock in block.get('stocks', []):
                codes.add(stock['code'])
    
    if not codes:
        return jsonify({'success': True, 'message': '没有需要同步的股票', 'report': {}})
    
    # 检查缓存中缺少MA10的数据
    cache = ht._load_price_cache()
    missing_report = {
        'total_stocks': len(codes),
        'total_dates': len(dates),
        'missing_ma10': [],
        'missing_below': [],
        'already_have': 0
    }
    
    for code in codes:
        for d in dates:
            has_ma10 = f'{code}_{d}_ma10' in cache
            has_below = f'{code}_{d}_below_ma10' in cache
            
            if not has_ma10:
                missing_report['missing_ma10'].append({'code': code, 'date': d})
            if not has_below:
                missing_report['missing_below'].append({'code': code, 'date': d})
            if has_ma10 and has_below:
                missing_report['already_have'] += 1
    
    # 重新获取缺少MA10数据的股票
    need_fetch = set(item['code'] for item in missing_report['missing_ma10'])
    
    import time
    success = 0
    failed = []
    for i, code in enumerate(need_fetch):
        try:
            # 获取更长时间范围的数据以确保有足够历史数据计算MA10
            # 走统一入口 fetch_range (mootdx 远程 -> 本地通达信), 不再直调废弃的 akshare
            if ht.fetch_range(code, start, end):
                success += 1
            time.sleep(0.5)
            if (i + 1) % 10 == 0:
                print(f"同步MA10进度: {i + 1}/{len(need_fetch)}")
        except Exception as e:
            failed.append({'code': code, 'error': str(e)})
            time.sleep(1)
    
    # 重新检查
    cache = ht._load_price_cache()
    still_missing = []
    for item in missing_report['missing_ma10']:
        if f"{item['code']}_{item['date']}_ma10" not in cache:
            still_missing.append(item)
    
    return jsonify({
        'success': True,
        'message': f'完成: 成功获取 {success}/{len(need_fetch)} 只股票MA10数据',
        'report': {
            'total_stocks': len(codes),
            'total_dates': len(dates),
            'missing_before': len(missing_report['missing_ma10']),
            'fetched': success,
            'failed': failed,
            'still_missing': still_missing[:20],  # 只返回前20条
            'still_missing_count': len(still_missing)
        }
    })



@hot_bp.route('/api/hot/cache/clear', methods=['POST'])

def api_hot_cache_clear():
    """
    清除涨幅缓存
    """
    try:
        cache_file = ht._PRICE_CACHE_FILE
        if os.path.exists(cache_file):
            os.remove(cache_file)
            ht._price_cache = None  # 重置内存缓存
            return jsonify({'success': True, 'message': '缓存已清除'})
        else:
            return jsonify({'success': True, 'message': '缓存文件不存在'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500



@hot_bp.route('/api/hot/refetch', methods=['POST'])

def api_hot_refetch():
    """
    重新获取股票涨幅数据（仅获取缺失数据）
    
    参数:
        codes: 股票代码列表
        start: 开始日期 (YYYYMMDD)
        end: 结束日期 (YYYYMMDD)
    """
    data = request.get_json() or {}
    codes = data.get('codes', [])
    start = data.get('start', '')
    end = data.get('end', '')
    
    if not codes or not start or not end:
        return jsonify({'success': False, 'error': '缺少参数'}), 400
    
    # 生成日期范围内所有交易日
    from datetime import datetime, timedelta
    try:
        s_dt = datetime.strptime(start, '%Y%m%d')
        e_dt = datetime.strptime(end, '%Y%m%d')
    except:
        return jsonify({'success': False, 'error': '日期格式错误'}), 400
    
    query_dates = []
    current = s_dt
    while current <= e_dt:
        if current.weekday() < 5:  # 只统计工作日
            query_dates.append(current.strftime('%Y%m%d'))
        current += timedelta(days=1)
    
    # 检查缓存，只获取缺失数据的股票
    cache = ht._load_price_cache()
    need_fetch = []
    for c in codes:
        # 检查该股票是否有任意一天的涨幅数据
        has_data = any(f'{c}_{d}' in cache for d in query_dates)
        if not has_data:
            need_fetch.append(c)
    
    if not need_fetch:
        return jsonify({
            'success': True, 
            'message': f'所有 {len(codes)} 只股票数据已存在，无需重新获取',
            'fetched': 0,
            'skipped': len(codes)
        })
    
    import time
    success = 0
    for i, c in enumerate(need_fetch):
        try:
            if ht.fetch_range(c, start, end):
                success += 1
            time.sleep(0.5)
            if (i + 1) % 10 == 0:
                print(f"已处理 {i + 1}/{len(need_fetch)}，成功 {success}")
        except Exception as e:
            print(f"获取 {c} 失败: {e}")
            time.sleep(1)
    
    return jsonify({
        'success': True,
        'message': f'完成: 成功获取 {success}/{len(need_fetch)} 只股票数据（跳过 {len(codes) - len(need_fetch)} 只有数据）',
        'fetched': success,
        'skipped': len(codes) - len(need_fetch)
    })



# ============== 历史爆发股票汇总 ==============


def _bar_date8(b):
    """K线 bar 的 date('YYYY-MM-DD') -> 'YYYYMMDD'"""
    return str(b.get('date', ''))[:10].replace('-', '')



def _build_windows(days, size, step):
    """按交易日切窗口。days: 升序 YYYYMMDD 列表。
    返回 [(start8, end8, [d,...]), ...]，相邻窗口以上一窗最后一日为下一窗起点(overlap=size-step)。"""
    windows = []
    i = 0
    n = len(days)
    while i + size <= n:
        seg = days[i:i + size]
        windows.append((seg[0], seg[-1], seg))
        i += step
    return windows



def explosive_scan_task(task_id, month, size, step, threshold):
    """
    后台扫描：对某月按交易日切 size 天窗口(步进 step)，
    找出窗口内 N 日涨幅 > threshold% 的非ST个股(数据来自通达信tq)。
    N日涨幅 = (窗口最后一日收盘 - 窗口首日前一交易日收盘) / 前一交易日收盘 * 100
    """
    t = explosive_tasks[task_id]
    t['status'] = 'running'

    try:
        import tdx_source as ts
        if not ts.is_available():
            t['status'] = 'failed'
            t['error'] = '通达信tq接口不可用(请确认量化版客户端已开启并登录)'
            return

        month_start = month.replace('-', '') + '01'
        # 月末
        y, m = int(month[:4]), int(month[5:7])
        if m == 12:
            nm_y, nm = y + 1, 1
        else:
            nm_y, nm = y, m + 1
        from datetime import date as _date, timedelta as _td2
        month_end = (_date(nm_y, nm, 1) - _td2(days=1)).strftime('%Y%m%d')

        days = trading_days_in_range(month_start, month_end)
        if len(days) < size:
            t['status'] = 'failed'
            t['error'] = f'{month} 内交易日不足 {size} 天'
            return
        windows = _build_windows(days, size, step)
        t['windows_count'] = len(windows)

        today8 = datetime.now().strftime('%Y%m%d')
        # 取窗口首日前 ~10 个自然日作为回溯起点(拿到前一交易日收盘)
        scan_from = (datetime.strptime(month_start, '%Y%m%d') - timedelta(days=12)).strftime('%Y%m%d')
        need = len(trading_days_in_range(scan_from, today8)) + 8
        need = max(30, min(need, 1200))

        # 全市场A股(含名称), 过滤ST/退市/北交所
        lst = ts.stock_list('5', with_name=True) or []
        universe = []
        for it in lst:
            code = str(it.get('code', '')).strip()
            name = str(it.get('name', '') or '').strip()
            if not re.match(r'^\d{6}$', code):
                continue
            if code.startswith(('4', '8')):  # 跳过北交所
                continue
            up = name.upper()
            if 'ST' in up or '退' in name:  # 跳过ST/退市
                continue
            universe.append((code, name))

        total = len(universe)
        t['total'] = total
        candidates = []

        # 批量读本地 .day 日线文件(全市场~2秒), 替代逐只 ts.kline(~18分钟)
        all_codes = [c for c, _ in universe]
        day_closes = _read_day_closes_batch(all_codes)
        t['day_loaded'] = len(day_closes)
        log_msg = f'本地日线加载 {len(day_closes)}/{len(all_codes)} 只'
        # 统计本地缺失的股票(后续逐只回退tqcenter)
        missing_codes = [c for c in all_codes if c not in day_closes]

        for i, (code, name) in enumerate(universe):
            if t.get('cancel_requested'):
                break
            t['processed'] = i
            t['progress'] = int(i / max(total, 1) * 100)
            try:
                closes = day_closes.get(code)
                order = None
                if not closes:
                    # 本地无该股.day文件, 回退 tqcenter kline
                    bars = ts.kline(code, count=need, period='1d', dividend_type='none')
                    if not bars or len(bars) < size + 1:
                        continue
                    closes = {}
                    for b in bars:
                        d8 = _bar_date8(b)
                        c = float(b.get('close', 0) or 0)
                        if d8 and c > 0:
                            closes[d8] = c
                if not closes or len(closes) < size + 1:
                    continue
                order = sorted(closes.keys())
                for (w_start, w_end, seg) in windows:
                    if w_start not in closes or w_end not in closes:
                        continue
                    # 窗口首日的前一交易日
                    try:
                        pos = order.index(w_start)
                    except ValueError:
                        continue
                    if pos == 0:
                        continue
                    prev_close = closes[order[pos - 1]]
                    if prev_close <= 0:
                        continue
                    gain = (closes[w_end] - prev_close) / prev_close * 100
                    if gain >= threshold:
                        candidates.append({
                            'range_start': w_start,
                            'range_end': w_end,
                            'range_label': f'{w_start[4:6]}/{w_start[6:8]}~{w_end[4:6]}/{w_end[6:8]}',
                            'code': code,
                            'name': name,
                            'gain': round(gain, 2),
                        })
            except Exception:
                continue

        # 按窗口起始 + 涨幅降序
        candidates.sort(key=lambda x: (x['range_start'], -x['gain']))
        t['processed'] = total
        t['progress'] = 100
        t['candidates'] = candidates
        t['status'] = 'cancelled' if t.get('cancel_requested') else 'completed'
    except Exception as e:
        t['status'] = 'failed'
        t['error'] = f'{type(e).__name__}: {e}'



@hot_bp.route('/api/hot/explosive/scan', methods=['POST'])

def api_explosive_scan():
    """启动历史爆发股票扫描(异步)。参数: month(YYYY-MM), window(默认3), step(默认2), threshold(默认18)"""
    data = request.get_json() or {}
    month = str(data.get('month', '')).strip()
    if not re.match(r'^\d{4}-\d{2}$', month):
        return jsonify({'success': False, 'error': 'month 需为 YYYY-MM 格式'}), 400
    try:
        size = int(data.get('window', 3))
        step = int(data.get('step', size - 1 if size > 1 else 1))
        threshold = float(data.get('threshold', 18))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '参数格式错误'}), 400
    size = max(2, min(size, 10))
    step = max(1, min(step, size))

    import uuid
    task_id = str(uuid.uuid4())[:8]
    explosive_tasks[task_id] = {
        'status': 'pending', 'progress': 0, 'processed': 0, 'total': 0,
        'month': month, 'window': size, 'step': step, 'threshold': threshold,
        'candidates': [],
    }
    threading.Thread(target=explosive_scan_task,
                     args=(task_id, month, size, step, threshold), daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id,
                    'status_url': f'/api/hot/explosive/status/{task_id}'})



@hot_bp.route('/api/hot/explosive/status/<task_id>', methods=['GET'])

def api_explosive_status(task_id):
    """查询扫描任务状态/结果"""
    if task_id not in explosive_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    t = explosive_tasks[task_id]
    return jsonify({
        'success': True, 'task_id': task_id,
        'status': t['status'], 'progress': t.get('progress', 0),
        'processed': t.get('processed', 0), 'total': t.get('total', 0),
        'threshold': t.get('threshold'), 'window': t.get('window'),
        'error': t.get('error'),
        'candidates': t.get('candidates', []),
    })



@hot_bp.route('/api/hot/explosive/cancel/<task_id>', methods=['POST'])

def api_explosive_cancel(task_id):
    """请求中断扫描任务"""
    if task_id not in explosive_tasks:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    explosive_tasks[task_id]['cancel_requested'] = True
    return jsonify({'success': True, 'message': '已请求中断，正在停止…'})



@hot_bp.route('/api/hot/explosive/kline', methods=['GET'])

def api_explosive_kline():
    """个股日K线, 截止到 end(YYYYMMDD) 当日, 返回最近 count 根(默认250≈1年)。数据来自通达信tq。"""
    code = request.args.get('code', '')
    if not re.match(r'^\d{6}$', code):
        return jsonify({'success': False, 'error': 'code 需为6位数字'}), 400
    end = request.args.get('end', '')
    if not re.match(r'^\d{8}$', end):
        return jsonify({'success': False, 'error': 'end 需为 YYYYMMDD'}), 400
    disp = request.args.get('count', default=250, type=int)
    disp = max(60, min(disp, 500))
    # 复权方式: 默认 front(前复权, 主流看盘软件默认, K线形态连续); none=不复权(可走本地.day更快)
    fq = (request.args.get('fq', 'front') or 'front').lower()
    if fq not in ('front', 'back', 'none'):
        fq = 'front'
    today8 = datetime.now().strftime('%Y%m%d')
    need = len(trading_days_in_range(end, today8)) + disp + 10
    need = max(disp + 10, min(need, 1400))
    try:
        import tdx_source as ts
        if fq == 'none':
            # 不复权: 优先读本地 .day 文件(快, 无需客户端在线), 读不到再回退 tqcenter
            bars, source = ts.kline_daily(code, count=need, prefer_local=True)
        else:
            # 前/后复权: 只能走 tqcenter(本地 .day 仅存不复权原始数据)
            if not ts.is_available():
                return jsonify({'success': False, 'error': '前复权需通达信tq接口, 当前不可用'}), 503
            bars = ts.kline(code, count=need, period='1d', dividend_type=fq)
            source = 'tqcenter'
        if not bars:
            if not ts.is_available():
                return jsonify({'success': False, 'error': '无K线数据且通达信tq接口不可用'}), 503
            return jsonify({'success': False, 'error': f'{code} 无K线数据'}), 404
        # 截止到 end 当日
        bars = [b for b in bars if _bar_date8(b) <= end]
        if not bars:
            return jsonify({'success': False, 'error': f'{code} 无 {end} 前的K线数据'}), 404
        bars = bars[-disp:]
        for i in range(len(bars)):
            bars[i]['last_close'] = bars[i - 1]['close'] if i > 0 else bars[i]['open']
        # source: local_day=本地通达信.day / tqcenter=通达信量化接口; fq: 复权方式
        return jsonify({'success': True, 'code': code, 'end': end, 'bars': bars, 'source': source, 'fq': fq})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500



# ============== 复盘: 每日概念热力(今天在炒啥) ==============


# ===== 概念成员每日收盘缓存(供"当日在涨谁/在杀谁"统计) =====

_MEMBER_CLOSES_FILE = '.member_closes.json'

_member_closes = {}                 # {code: {YYYYMMDD: close}}

_member_closes_lock = threading.Lock()

_daymap_build_tasks = {}            # {task_id: {...}}

_daymap_members_cache = {'ts': 0, 'data': None}



def _load_member_closes():
    global _member_closes
    try:
        if os.path.exists(_MEMBER_CLOSES_FILE):
            with open(_MEMBER_CLOSES_FILE, 'r', encoding='utf-8') as f:
                _member_closes = json.load(f) or {}
            print(f'[daymap] 已载入成员收盘缓存 {len(_member_closes)} 只')
    except Exception as e:
        print(f'[daymap] 载入收盘缓存失败: {e}')
        _member_closes = {}



def _save_member_closes():
    try:
        with _member_closes_lock:
            with open(_MEMBER_CLOSES_FILE, 'w', encoding='utf-8') as f:
                json.dump(_member_closes, f, ensure_ascii=False)
    except Exception as e:
        print(f'[daymap] 保存收盘缓存失败: {e}')


_load_member_closes()



def _member_pct(code, date, prev):
    """从收盘缓存算某日涨跌幅%(相对前一交易日)。缺数据返回 None。"""
    m = _member_closes.get(code)
    if not m:
        return None
    c = m.get(date)
    p = m.get(prev)
    if c and p and p > 0:
        return (c - p) / p * 100
    return None



def _get_daymap_members(cur):
    """全部产业链概念的成员映射 {concept: set(code)}, 1小时缓存。"""
    import time as _t
    import concept_chain as _cc
    if _daymap_members_cache['data'] and _t.time() - _daymap_members_cache['ts'] < 3600:
        return _daymap_members_cache['data']
    concepts = list(set(_cc.meaningful_concepts()))
    cur.execute("SELECT concept, stock_code FROM ths.concept_member WHERE concept = ANY(%s)", (concepts,))
    d = {}
    for con, code in cur.fetchall():
        d.setdefault(con, set()).add(code)
    _daymap_members_cache['data'] = d
    _daymap_members_cache['ts'] = _t.time()
    return d



def _daymap_build_task(task_id):
    """拉取全部产业链概念成员的日线收盘, 写入持久缓存(供当日涨/杀统计)。"""
    import concept_chain as _cc
    import db as _db
    import tdx_source as _ts
    t = _daymap_build_tasks[task_id]
    t['status'] = 'running'
    try:
        concepts = list(set(_cc.meaningful_concepts()))
        conn = _db.get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT stock_code FROM ths.concept_member WHERE concept = ANY(%s)", (concepts,))
            codes = sorted({r[0] for r in cur.fetchall()})
        conn.close()
        if not _ts.is_available():
            t['status'] = 'failed'
            t['error'] = '通达信tq接口不可用(请确认量化版客户端已开启并登录)'
            return
        t['total'] = len(codes)
        done = 0
        for code in codes:
            if t.get('_cancel'):
                t['status'] = 'cancelled'
                _save_member_closes()
                return
            bars = _ts.kline(code, count=260, period='1d', dividend_type='none')
            if bars:
                m = _member_closes.setdefault(code, {})
                for b in bars:
                    m[b['date'].replace('-', '')] = b['close']
            done += 1
            t['done'] = done
            t['progress'] = int(done / max(len(codes), 1) * 100)
            if done % 500 == 0:
                _save_member_closes()
        _save_member_closes()
        t['status'] = 'completed'
    except Exception as e:
        t['status'] = 'failed'
        t['error'] = f'{type(e).__name__}: {e}'



@hot_bp.route('/api/hot/daymap/build', methods=['POST'])

def api_daymap_build():
    """构建/刷新 成员日收盘缓存(供当日涨/杀统计)。首次较慢(拉全部成员日线)。"""
    import uuid
    for tid, t in _daymap_build_tasks.items():
        if t['status'] in ('pending', 'running'):
            return jsonify({'success': True, 'task_id': tid, 'reused': True})
    task_id = str(uuid.uuid4())[:8]
    _daymap_build_tasks[task_id] = {'status': 'pending', 'progress': 0, 'done': 0,
                                    'total': 0, 'error': None, '_cancel': False}
    threading.Thread(target=_daymap_build_task, args=(task_id,), daemon=True).start()
    return jsonify({'success': True, 'task_id': task_id})



@hot_bp.route('/api/hot/daymap/build/status/<task_id>', methods=['GET'])

def api_daymap_build_status(task_id):
    t = _daymap_build_tasks.get(task_id)
    if not t:
        return jsonify({'success': False, 'error': '任务不存在'}), 404
    return jsonify({'success': True, 'status': t['status'], 'progress': t['progress'],
                    'done': t.get('done', 0), 'total': t.get('total', 0), 'error': t.get('error')})



def _parse_board(s):
    """从连板字段解析当前连板数(如 '3板'/'2/3天2板'->取末尾N板)。复盘收录默认至少首板=1。"""
    if not s:
        return 1
    m = re.findall(r'(\d+)\s*板', str(s))
    if m:
        return int(m[-1])
    m2 = re.match(r'^\s*(\d+)\s*$', str(s))
    return int(m2.group(1)) if m2 else 1



@hot_bp.route('/api/hot/ladder-concepts', methods=['GET'])

def api_ladder_concepts():
    """板块梯队视图的概念下拉: 返回产业链概念 + 累计复盘活跃度, 按活跃度降序。"""
    import db as _db
    import concept_chain as _cc
    try:
        conn = _db.get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT cm.concept, count(*) FROM zt_stocks zs "
                        "JOIN ths.concept_member cm ON cm.stock_code=zs.code GROUP BY cm.concept")
            rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({'success': False, 'error': f'{e}'}), 503
    out = []
    for con, n in rows:
        ci = _cc.concept_to_chain(con)
        if not ci or _cc.is_blocked(con):
            continue
        out.append({'concept': con, 'chain': ci.get('chain_name', ''), 'total': n})
    out.sort(key=lambda x: -x['total'])
    return jsonify({'success': True, 'concepts': out})



@hot_bp.route('/api/hot/sector-ladder', methods=['GET'])

def api_sector_ladder():
    """板块内部梯队/龙头健康。两套口径都返回:
    emotion(情绪梯队): 每日 龙头高度 + 高位(≥3板)/中位(2板)/低位(首板) 涨停数 + 结构标签。
    trend(趋势主升): 每日 创60日新高数 / 沿均线主升数(收盘>MA20且MA5>MA20) / MA20上方数 + 龙头是否新高 + 标签。
    参数: concept(必填), start,end(YYYYMMDD, 缺省=最近30交易日)。"""
    import db as _db
    from collections import defaultdict
    concept = request.args.get('concept', '').strip()
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    if not concept:
        return jsonify({'success': False, 'error': '需要 concept 参数'}), 400
    try:
        conn = _db.get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT to_char(trade_date,'YYYYMMDD') FROM zt_stocks GROUP BY trade_date ORDER BY trade_date")
            all_dates = [x[0] for x in cur.fetchall()]
            if not re.match(r'^\d{8}$', end):
                end = all_dates[-1] if all_dates else datetime.now().strftime('%Y%m%d')
            if not re.match(r'^\d{8}$', start):
                idx = all_dates.index(end) if end in all_dates else len(all_dates) - 1
                start = all_dates[max(0, idx - 29)] if all_dates else end
            dates = [d for d in all_dates if start <= d <= end]
            # 概念成员
            cur.execute("SELECT stock_code FROM ths.concept_member WHERE concept=%s", (concept,))
            members = [r[0] for r in cur.fetchall()]
            # 该概念成员在区间内的复盘连板记录
            sd = f'{start[:4]}-{start[4:6]}-{start[6:8]}'
            ed = f'{end[:4]}-{end[4:6]}-{end[6:8]}'
            cur.execute(
                "SELECT to_char(zs.trade_date,'YYYYMMDD') d, zs.code, zs.lianban FROM zt_stocks zs "
                "JOIN ths.concept_member cm ON cm.stock_code=zs.code "
                "WHERE cm.concept=%s AND zs.trade_date>=%s AND zs.trade_date<=%s", (concept, sd, ed))
            zt = cur.fetchall()
        conn.close()
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500

    # ---- 情绪梯队 ----
    day_boards = defaultdict(list)
    for d, code, lb in zt:
        day_boards[d].append(_parse_board(lb))
    emotion = []
    prev_top = 0
    for d in dates:
        b = day_boards.get(d, [])
        tot = len(b)
        top = max(b) if b else 0
        hi = sum(1 for x in b if x >= 3)
        mid = sum(1 for x in b if x == 2)
        lo = sum(1 for x in b if x <= 1)
        if tot == 0:
            tag = ''
        elif top >= 4 and hi >= 2:
            tag = '强·高位梯队在走'
        elif top > prev_top and top >= 3:
            tag = '转强·龙头晋级'
        elif hi == 0 and lo >= tot * 0.7:
            tag = '弱·全低位补涨'
        elif prev_top and top < prev_top:
            tag = '退·龙头断板'
        else:
            tag = ''
        emotion.append({'date': d, 'total': tot, 'top': top, 'hi': hi, 'mid': mid, 'lo': lo, 'tag': tag})
        if tot:
            prev_top = top

    # ---- 趋势主升(需成员日收盘缓存) ----
    trend_ready = len(_member_closes) > 0
    trend = []
    if trend_ready:
        # 预排每个成员的收盘日期序列
        mdates = {}
        for c in members:
            mc = _member_closes.get(c)
            if mc:
                mdates[c] = sorted(mc.keys())
        prev_nh = 0
        for d in dates:
            newhigh = rising = above = quoted = 0
            biases = []
            leader_ratio = 0.0
            for c in members:
                mc = _member_closes.get(c)
                dl = mdates.get(c)
                if not mc or not dl or d not in mc:
                    continue
                i = dl.index(d) if d in dl else -1
                if i < 5:
                    continue
                win = dl[max(0, i - 59):i + 1]
                closes = [mc[x] for x in win]
                close = mc[d]
                if close <= 0:
                    continue
                quoted += 1
                hi60 = max(closes)
                ma20 = sum(closes[-20:]) / len(closes[-20:])
                ma5 = sum(closes[-5:]) / len(closes[-5:])
                if close >= hi60 * 0.999:
                    newhigh += 1
                if close > ma20:
                    above += 1
                    if ma5 > ma20:
                        rising += 1
                        biases.append((close - ma20) / ma20 * 100)
                leader_ratio = max(leader_ratio, close / hi60 if hi60 > 0 else 0)
            avg_bias = round(sum(biases) / len(biases), 1) if biases else 0
            leader_nh = leader_ratio >= 0.999
            if quoted == 0:
                tag = ''
            elif leader_nh and newhigh >= prev_nh and newhigh >= 2:
                tag = '强·主升续创新高'
            elif newhigh > 0 and newhigh >= prev_nh:
                tag = '转强·新高增多'
            elif newhigh == 0 and rising > 0:
                tag = '弱·无新高(滞涨)'
            elif prev_nh and newhigh < prev_nh:
                tag = '退·新高减少'
            else:
                tag = ''
            trend.append({'date': d, 'members': quoted, 'newhigh': newhigh,
                          'rising': rising, 'above': above, 'avg_bias': avg_bias,
                          'leader_nh': leader_nh, 'tag': tag})
            if quoted:
                prev_nh = newhigh

    return jsonify({'success': True, 'concept': concept, 'start': start, 'end': end,
                    'all_dates': all_dates, 'dates': dates, 'members': len(members),
                    'emotion': emotion, 'trend': trend, 'trend_ready': trend_ready})



# ---- 新高追踪: 每日创N日新高的个股, 按同花顺板块分类 ----

# 全量计算缓存: {n: (ts, {all_dates, days_by_date: {date: {...}}})}

# .day文件一天才变一次, 一次全量算完所有日期, 切换日期范围只在内存中切片

_newhigh_full_cache = {}  # {n: (ts, full_result)}



def _compute_newhigh_full(n):
    """全量计算所有已入库交易日创N日新高的个股, 按概念分类。
    返回 {all_dates: [...], days: {date_str: {date,total,sectors}}}。
    .day文件不变则结果不变, 缓存1小时。"""
    import time as _time
    cached = _newhigh_full_cache.get(n)
    if cached and (_time.time() - cached[0] < 3600):
        return cached[1]

    from collections import defaultdict
    import db as _db
    from datetime import datetime as _dt

    # 已入库交易日列表
    try:
        all_dates = sorted(_db.get_submitted_dates())
    except Exception:
        all_dates = []
    today_str = _dt.now().strftime('%Y%m%d')
    if _dt.now().weekday() < 5 and today_str not in all_dates:
        all_dates.append(today_str)
        all_dates.sort()
    if not all_dates:
        return {'all_dates': [], 'days': {}}

    # 概念成员
    cm = _load_concept_members()
    if not cm:
        return {'all_dates': all_dates, 'days': {}}
    code_concepts = defaultdict(list)
    all_codes = set()
    for concept, codes in cm.items():
        for c in codes:
            code_concepts[c].append(concept)
            all_codes.add(c)
    all_codes = sorted(all_codes)

    # 批量读 .day 收盘价
    closes_map = _read_day_closes_batch(all_codes)
    if not closes_map:
        return {'all_dates': all_dates, 'days': {}}

    # 股票名称
    names = {}
    try:
        import tdx_source as _ts
        lst = _ts.stock_list('5', with_name=True) or []
        names = {x['code']: x.get('name', '') for x in lst}
    except Exception:
        pass

    # 预排每只股票的有序日期列表(加速索引)
    stock_dates = {}
    for c in all_codes:
        mc = closes_map.get(c)
        if mc and len(mc) > n:
            stock_dates[c] = sorted(mc.keys())

    # 全量计算: 每个交易日
    FWD_DAYS = 10  # 创新高后追踪10个交易日
    days_by_date = {}
    for d in all_dates:
        nh_stocks = {}
        for c in all_codes:
            mc = closes_map.get(c)
            dl = stock_dates.get(c)
            if not mc or not dl or d not in mc:
                continue
            i = dl.index(d)
            if i < n:
                continue
            close = mc[d]
            if close <= 0:
                continue
            win = dl[i - n:i]
            prev_hi = max(mc[x] for x in win)
            if close >= prev_hi * 0.999:
                prev_close = mc.get(dl[i - 1], 0)
                pct = round((close - prev_close) / prev_close * 100, 2) if prev_close > 0 else 0
                # 创新高后FWD_DAYS日累计涨幅
                fwd = []
                for k in range(1, FWD_DAYS + 1):
                    if i + k < len(dl):
                        c_k = mc[dl[i + k]]
                        fwd.append(round((c_k / close - 1) * 100, 2))
                    else:
                        fwd.append(None)  # 数据不足
                nh_stocks[c] = {'code': c, 'name': names.get(c, c),
                                'close': round(close, 2), 'pct': pct, 'fwd': fwd}
        if not nh_stocks:
            days_by_date[d] = {'date': d, 'total': 0, 'sectors': [], 'fwd_avg': [None] * FWD_DAYS}
            continue
        sector_stocks = defaultdict(list)
        for c, info in nh_stocks.items():
            for con in code_concepts.get(c, []):
                sector_stocks[con].append(info)

        def _avg_fwd(stocks):
            """计算一批股票的平均前向涨幅, 返回[10]数组(None表示无数据)"""
            out = []
            for k in range(FWD_DAYS):
                vals = [s['fwd'][k] for s in stocks if s['fwd'][k] is not None]
                out.append(round(sum(vals) / len(vals), 2) if vals else None)
            return out

        sectors = []
        for con, stocks in sector_stocks.items():
            stocks.sort(key=lambda x: -x['pct'])
            sectors.append({'concept': con, 'count': len(stocks),
                            'stocks': stocks, 'fwd_avg': _avg_fwd(stocks)})
        sectors.sort(key=lambda x: -x['count'])
        # 全日平均前向涨幅(所有创新高个股)
        all_fwd = _avg_fwd(list(nh_stocks.values()))
        days_by_date[d] = {'date': d, 'total': len(nh_stocks), 'sectors': sectors, 'fwd_avg': all_fwd}

    full = {'all_dates': all_dates, 'days': days_by_date, 'closes_map': closes_map,
            'stock_dates': stock_dates}
    _newhigh_full_cache[n] = (_time.time(), full)
    return full



@hot_bp.route('/api/hot/newhigh', methods=['GET'])

def api_hot_newhigh():
    """每日创N日收盘新高的个股, 按同花顺概念板块分类。
    参数: start,end(YYYYMMDD), n(60/120/200, 默认60), force=1强制重算。
    全量计算按n缓存1小时(.day文件一天才变), 切换日期范围只在内存中切片, 瞬间返回。"""
    import time as _time
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    try:
        n = int(request.args.get('n', '60'))
    except ValueError:
        n = 60
    if n not in (60, 120, 200):
        n = 60
    force = request.args.get('force', '') == '1'
    if force:
        _newhigh_full_cache.pop(n, None)

    full = _compute_newhigh_full(n)
    all_dates = full['all_dates']
    days_by_date = full['days']
    if not all_dates:
        return jsonify({'success': False, 'error': '无已入库交易日'})

    # 默认日期范围: 最近30天
    if not re.match(r'^\d{8}$', end):
        end = all_dates[-1]
    if not re.match(r'^\d{8}$', start):
        start = all_dates[max(0, len(all_dates) - 30)]
    dates = [d for d in all_dates if start <= d <= end]
    if not dates:
        return jsonify({'success': False, 'error': '区间内无已入库交易日'})

    # 切片: 从全量缓存中取出区间内的天数
    days = [days_by_date[d] for d in dates if d in days_by_date]
    # 给每只stock补算 range_pct: 范围起始日到结束日的累计涨幅
    # 同时计算每只股票K线窗口(250根截止end_date)的最大涨跌幅, 用于统一比例尺
    closes_map = full.get('closes_map', {})
    stock_dates_map = full.get('stock_dates', {})
    start_date = dates[0]
    end_date = dates[-1]
    import bisect
    KLINE_COUNT = 250
    seen_codes = set()
    for day in days:
        for sec in day.get('sectors', []):
            for s in sec.get('stocks', []):
                code = s['code']
                if code in seen_codes:
                    continue
                seen_codes.add(code)
                mc = closes_map.get(code)
                dl = stock_dates_map.get(code)
                if mc and dl:
                    # 找到 >= start_date 和 <= end_date 的最近日期
                    cs = mc.get(start_date)
                    ce = mc.get(end_date)
                    if not cs:
                        for dd in dl:
                            if dd >= start_date:
                                cs = mc.get(dd)
                                break
                    if not ce:
                        for dd in reversed(dl):
                            if dd <= end_date:
                                ce = mc.get(dd)
                                break
                    if cs and ce and cs > 0:
                        s['range_pct'] = round((ce / cs - 1) * 100, 2)
                    else:
                        s['range_pct'] = None
                    # K线窗口(250根截止end_date)的最大涨跌幅, 用于统一比例尺
                    ei = bisect.bisect_right(dl, end_date) - 1
                    if ei >= 0:
                        si = max(0, ei - KLINE_COUNT + 1)
                        base = mc[dl[si]]
                        if base > 0:
                            win_closes = [mc[dl[j]] for j in range(si, ei + 1)]
                            s['kline_max_pct'] = round((max(win_closes) / base - 1) * 100, 2)
                            s['kline_min_pct'] = round((min(win_closes) / base - 1) * 100, 2)
                        else:
                            s['kline_max_pct'] = 0
                            s['kline_min_pct'] = 0
                    else:
                        s['kline_max_pct'] = 0
                        s['kline_min_pct'] = 0
                else:
                    s['range_pct'] = None
                    s['kline_max_pct'] = 0
                    s['kline_min_pct'] = 0
    result = {'success': True, 'n': n, 'start': start, 'end': end,
              'dates': dates, 'days': days}
    return jsonify(result)



@hot_bp.route('/api/hot/concept-daymap', methods=['GET'])

def api_concept_daymap():
    """某一交易日的产业链地图: 按 产业链 -> 上下游层级 -> 概念。
    每个概念给出: 复盘涨停数(zt) + 当日涨(>5%)数(up) + 杀(<-5%,含跌停)数(down)。
    涨/杀 来自成员日收盘缓存(需先 /api/hot/daymap/build 构建, 未构建则只有复盘涨停)。
    参数: date(YYYYMMDD, 缺省=最新交易日)。"""
    import db as _db
    import concept_chain as _cc
    from collections import defaultdict
    date = request.args.get('date', '')
    try:
        conn = _db.get_conn()
    except Exception as e:
        return jsonify({'success': False, 'error': f'数据库不可用: {e}'}), 503
    try:
        with conn.cursor() as cur:
            if not re.match(r'^\d{8}$', date):
                cur.execute("SELECT to_char(max(trade_date),'YYYYMMDD') FROM zt_stocks")
                r = cur.fetchone()
                date = (r and r[0]) or datetime.now().strftime('%Y%m%d')
            dd = f'{date[:4]}-{date[4:6]}-{date[6:8]}'
            cur.execute(
                "SELECT zs.code, cm.concept FROM zt_stocks zs "
                "JOIN ths.concept_member cm ON cm.stock_code = zs.code "
                "WHERE zs.trade_date = %s", (dd,))
            zt_rows = cur.fetchall()
            cur.execute("SELECT to_char(trade_date,'YYYYMMDD') FROM zt_stocks GROUP BY trade_date ORDER BY trade_date")
            all_dates = [x[0] for x in cur.fetchall()]
            con_members = _get_daymap_members(cur)
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        conn.close()

    # 复盘涨停: 概念 -> 个股集合
    concept_zt = defaultdict(set)
    for code, con in zt_rows:
        ci = _cc.concept_to_chain(con)
        if not ci or _cc.is_blocked(con):
            continue
        concept_zt[con].add(code)

    # 前一交易日(用于算涨跌幅)
    prev = None
    if date in all_dates:
        i = all_dates.index(date)
        if i > 0:
            prev = all_dates[i - 1]
    price_ready = bool(prev) and len(_member_closes) > 0

    def con_sets(con):
        """返回 4 类互斥的成员集合: lu涨停 / up5(5%~涨停) / dn5(-5%~跌停) / ld跌停。"""
        S = {'lu': set(), 'up5': set(), 'dn5': set(), 'ld': set()}
        if price_ready:
            for c in con_members.get(con, ()):
                pct = _member_pct(c, date, prev)
                if pct is None:
                    continue
                thr = _limit_threshold(c)
                if pct >= thr:
                    S['lu'].add(c)
                elif pct > 5:
                    S['up5'].add(c)
                if pct <= -thr:
                    S['ld'].add(c)
                elif pct < -5:
                    S['dn5'].add(c)
        return S

    info = _cc.chain_info()
    all_chains = [{'id': cid, 'name': c['name']} for cid, c in info.items()]
    chains = []
    for cid, c in info.items():
        agg = {'zt': set(), 'lu': set(), 'up5': set(), 'dn5': set(), 'ld': set()}
        n_tiers = len(c['tiers'])
        tiers = []
        for i, t in enumerate(c['tiers']):
            pos = ('中游' if n_tiers == 1 else
                   '上游' if i == 0 else '下游' if i == n_tiers - 1 else '中游')
            cons = []
            for con in t['concepts']:
                zt = concept_zt.get(con, set())
                S = con_sets(con)
                active = len(zt) + len(S['lu']) + len(S['up5']) + len(S['dn5']) + len(S['ld'])
                if active == 0:
                    continue
                agg['zt'] |= zt
                for k in ('lu', 'up5', 'dn5', 'ld'):
                    agg[k] |= S[k]
                cons.append({'concept': con, 'zt': len(zt),
                             'lu': len(S['lu']), 'up5': len(S['up5']),
                             'dn5': len(S['dn5']), 'ld': len(S['ld'])})
            cons.sort(key=lambda x: -(x['lu'] * 100 + x['up5'] * 10 + x['zt']))
            if cons:
                tiers.append({'tier': t['tier'], 'pos': pos, 'concepts': cons})
        tot = sum(len(agg[k]) for k in ('lu', 'up5', 'dn5', 'ld')) + len(agg['zt'])
        if tot > 0:
            chains.append({'id': cid, 'name': c['name'],
                           'zt': len(agg['zt']), 'lu': len(agg['lu']), 'up5': len(agg['up5']),
                           'dn5': len(agg['dn5']), 'ld': len(agg['ld']),
                           'total': tot, 'tiers': tiers})
    chains.sort(key=lambda x: -x['total'])
    return jsonify({'success': True, 'date': date, 'prev': prev, 'price_ready': price_ready,
                    'all_dates': all_dates, 'all_chains': all_chains, 'chains': chains})



@hot_bp.route('/api/hot/breadth-scan', methods=['GET'])

def api_breadth_scan():
    """板块广度起爆信号扫描: 在日期区间内, 逐日算每个产业链概念的广度
    (上涨占比、涨>5%占比、涨停数、平均涨幅), 标记"起爆日"(广度突然爆发=资金整体扫货)。
    起爆判定(默认): 有效成员≥MIN, 上涨占比≥UP, 涨>5%占比≥UP5。
    需先构建成员日收盘缓存(/api/hot/daymap/build)。
    参数: start,end(YYYYMMDD, 缺省=最近30交易日); up(默认0.85); up5(默认0.25); min(默认15)。"""
    import db as _db
    import concept_chain as _cc
    if len(_member_closes) == 0:
        return jsonify({'success': False, 'needs_build': True,
                        'error': '未构建成员日收盘缓存, 请先在「每日热力」点「计算涨/杀」构建'}), 400
    start = request.args.get('start', '')
    end = request.args.get('end', '')
    try:
        UP = float(request.args.get('up', 0.85))
        UP5 = float(request.args.get('up5', 0.25))
        MIN = int(request.args.get('min', 15))
        MINLU = int(request.args.get('minlu', 5))
    except Exception:
        UP, UP5, MIN, MINLU = 0.85, 0.25, 15, 5
    try:
        conn = _db.get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT to_char(trade_date,'YYYYMMDD') FROM zt_stocks GROUP BY trade_date ORDER BY trade_date")
            all_dates = [x[0] for x in cur.fetchall()]
            if not re.match(r'^\d{8}$', end):
                end = all_dates[-1] if all_dates else datetime.now().strftime('%Y%m%d')
            if not re.match(r'^\d{8}$', start):
                idx = all_dates.index(end) if end in all_dates else len(all_dates) - 1
                start = all_dates[max(0, idx - 29)] if all_dates else end
            con_members = _get_daymap_members(cur)
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500
    finally:
        try: conn.close()
        except Exception: pass

    dates = [d for d in all_dates if start <= d <= end]
    prev_of = {}
    for i, d in enumerate(all_dates):
        prev_of[d] = all_dates[i - 1] if i > 0 else None

    events = []           # 起爆事件(concept-day)
    first_ignite = {}     # concept -> 区间内首个起爆日
    for con, mem in con_members.items():
        if _cc.is_blocked(con):
            continue
        ci = _cc.concept_to_chain(con)
        chain = ci.get('chain_name', '') if ci else ''
        mem = list(mem)
        if len(mem) < MIN:
            continue
        for d in dates:
            p = prev_of.get(d)
            if not p:
                continue
            quoted = up = up5 = lu = 0
            ssum = 0.0
            for c in mem:
                pct = _member_pct(c, d, p)
                if pct is None:
                    continue
                quoted += 1
                ssum += pct
                if pct > 0:
                    up += 1
                if pct >= 5:
                    up5 += 1
                if pct >= _limit_threshold(c):
                    lu += 1
            if quoted < MIN:
                continue
            up_ratio = up / quoted
            up5_ratio = up5 / quoted
            avg = ssum / quoted
            ignited = up_ratio >= UP and up5_ratio >= UP5 and lu >= MINLU
            if ignited:
                score = round(up5_ratio * 100 + lu * 2 + avg, 1)
                is_first = con not in first_ignite
                if is_first:
                    first_ignite[con] = d
                events.append({
                    'concept': con, 'chain': chain, 'date': d,
                    'quoted': quoted, 'up': up, 'up5': up5, 'lu': lu,
                    'up_ratio': round(up_ratio * 100, 1), 'up5_ratio': round(up5_ratio * 100, 1),
                    'avg': round(avg, 2), 'score': score, 'first': is_first,
                })
    # 排序: 先日期升序(时间线), 同日按强度降序
    events.sort(key=lambda e: (e['date'], -e['score']))
    return jsonify({'success': True, 'start': start, 'end': end, 'all_dates': all_dates,
                    'threshold': {'up': UP, 'up5': UP5, 'min': MIN, 'minlu': MINLU},
                    'count': len(events), 'events': events})



# ============== 盯盘: 产业链实时盯盘 ==============


_chain_board_cache = {'ts': 0, 'data': None}

_chain_members_cache = {'ts': 0, 'members': None}

_astock_name_cache = {'ts': 0, 'names': {}}



# ============== 湖南人盯盘: Top10板块实时涨幅聚合 ==============


# 实时报价缓存(5秒): key=排序后的板块名拼接, value={data, ts, source}

_hnrlive_quotes_cache = {}



@hot_bp.route('/api/hot/live/quotes', methods=['POST'])

def api_hnrlive_quotes():
    """湖南人盯盘: 对前端给定的Top10板块(及其成员股代码)批量拉实时涨幅并按板块聚合。
    后端无状态: 不重算 track_hot_stocks, 由前端传入 {blocks:[{block,codes:[...],names:{code:name}}]}。
    优先 tqcenter batch_pricevol(走DLL不封IP), 回退 mootdx 批量 quotes。
    结果按板块返回 avg_pct/limitup/stocks。5秒缓存。"""
    import time as _t
    import tdx_source as _ts
    import hot_track as ht

    body = request.get_json(silent=True) or {}
    blocks = body.get('blocks') or []
    if not blocks:
        return jsonify({'success': False, 'error': '缺少 blocks 参数'}), 400

    # 缓存key: 排序后的板块名(忽略codes顺序, 同一组板块5秒内复用)
    cache_key = '|'.join(sorted(b.get('block', '') for b in blocks))
    cached = _hnrlive_quotes_cache.get(cache_key)
    if cached and (_t.time() - cached['ts'] < 5):
        return jsonify(cached['data'])

    # 汇总所有成员股代码 + 名称表(前端传names, 后端不依赖stock_list名称表)
    all_codes = []
    name_map = {}
    for b in blocks:
        for c in (b.get('codes') or []):
            c = str(c).zfill(6)
            all_codes.append(c)
        for c, nm in (b.get('names') or {}).items():
            name_map[str(c).zfill(6)] = nm
    # 去重保序
    seen = set()
    uniq_codes = []
    for c in all_codes:
        if c not in seen:
            seen.add(c)
            uniq_codes.append(c)
    if not uniq_codes:
        return jsonify({'success': False, 'error': '无成员股代码'}), 400

    quotes = {}   # code -> {price, pct, vol}
    source = 'none'
    # 优先 tqcenter batch_pricevol
    try:
        if _ts.is_available():
            pv = _ts.batch_pricevol(uniq_codes) or {}
            if pv:
                for c, info in pv.items():
                    quotes[str(c).zfill(6)] = {'pct': info.get('pct', 0), 'price': info.get('price', 0)}
                source = 'tqcenter'
    except Exception as e:
        print(f'[TQ] 湖南人盯盘报价异常: {e}')

    # 回退 mootdx 批量 quotes (80只一批)
    if not quotes:
        try:
            import pandas as pd
            with ht._get_tdx_lock():
                client = ht._get_tdx_client()
                frames = []
                for i in range(0, len(uniq_codes), 80):
                    batch = uniq_codes[i:i + 80]
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
                for _, row in valid.iterrows():
                    code = str(row.get('code', '')).zfill(6)
                    price = row['price'] if pd.notna(row['price']) and row['price'] > 0 else row['last_close']
                    pct = ((price - row['last_close']) / row['last_close'] * 100) if row['last_close'] > 0 else 0.0
                    quotes[code] = {'pct': round(float(pct), 2), 'price': round(float(price), 2)}
                source = 'mootdx'
        except Exception as e:
            print(f'[mootdx] 湖南人盯盘报价异常: {e}')

    # 按板块聚合
    out_blocks = []
    for b in blocks:
        bname = b.get('block', '')
        bcodes = [str(c).zfill(6) for c in (b.get('codes') or [])]
        pcts = []
        stocks = []
        lu = 0
        for c in bcodes:
            q = quotes.get(c)
            if not q:
                continue
            p = round(float(q.get('pct', 0)), 2)
            pcts.append(p)
            if p >= _limit_threshold(c):
                lu += 1
            stocks.append({'code': c, 'name': name_map.get(c, ''), 'pct': p})
        # 按涨幅降序
        stocks.sort(key=lambda x: -x['pct'])
        out_blocks.append({
            'block': bname,
            'avg_pct': round(sum(pcts) / len(pcts), 2) if pcts else 0,
            'limitup': lu,
            'up': sum(1 for p in pcts if p > 0),
            'quoted': len(pcts),
            'members': len(bcodes),
            'stocks': stocks,
        })

    out = {
        'success': True,
        'ts': datetime.now().strftime('%H:%M:%S'),
        'source': source,
        'blocks': out_blocks,
    }
    _hnrlive_quotes_cache[cache_key] = {'data': out, 'ts': _t.time(), 'source': source}
    return jsonify(out)



# ============== 招财猫复盘: 大涨/大跌个股行为收集 ==============

