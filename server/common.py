# -*- coding: utf-8 -*-
"""server.common — 跨 Blueprint 共享的基础设施.

持有 Flask app / SocketIO 单例、调试体系、被多组共用的工具函数
(概念成员缓存、通达信本地 .day/.lc1 读取)。

本模块不 import 任何 bp_*.py, 避免循环依赖。
业务模块(hot_track/tdx_source/db 等)在此仍按需函数级 import, 与原 api_server 一致。
"""
import os
import time
import uuid
import logging


# ============== 调试配置/日志 ==============
# debug.enabled 只控制开发诊断能力；默认关闭，config.yml 或 AI_KANPAN_DEBUG=1 可开启。
def _load_debug_config():
    try:
        import yaml as _yaml
        with open('config.yml', 'r', encoding='utf-8') as _f:
            cfg = (_yaml.safe_load(_f) or {}).get('debug') or {}
            return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


_DEBUG_CONFIG = _load_debug_config()
_debug_env = os.environ.get('AI_KANPAN_DEBUG')
DEBUG_MODE = (_debug_env == '1') if _debug_env is not None else bool(_DEBUG_CONFIG.get('enabled', False))
SERVER_STARTED_AT = time.strftime('%Y-%m-%d %H:%M:%S')
SERVER_INSTANCE_ID = f'{os.getpid()}-{uuid.uuid4().hex[:8]}'

_dbg_logger = logging.getLogger('ai_kanpan')
# 抑制 tdxpy/mootdx 内部 NotImplementedError 噪音日志(不影响功能)
logging.getLogger('tdxpy').setLevel(logging.CRITICAL)
if DEBUG_MODE:
    _dbg_handler = logging.FileHandler('_debug.log', encoding='utf-8')
    _dbg_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', '%H:%M:%S'))
    _dbg_logger.addHandler(_dbg_handler)
    _dbg_logger.setLevel(logging.DEBUG)


def _dlog(msg, level='DEBUG'):
    if not DEBUG_MODE:
        return
    getattr(_dbg_logger, level.lower(), _dbg_logger.debug)(msg)


# ============== Flask app / SocketIO 单例 ==============
from flask import Flask, request

app = Flask(__name__)
from flask_socketio import SocketIO
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins='*')


@app.after_request
def _debug_no_cache(response):
    """Debug 模式禁用页面/API的浏览器HTTP缓存，并标记实际服务进程实例。"""
    if DEBUG_MODE and (request.path.startswith('/api/') or response.mimetype == 'text/html'):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
        response.headers['X-AI-Kanpan-Instance'] = SERVER_INSTANCE_ID
    return response


# 导入热门股追踪模块(各组共用)
import hot_track as ht


# ============== 概念成员缓存(跨 monitor/hot/caizhaomao 共用) ==============
_EXCLUDED_CONCEPTS = {'国企改革', '深股通', '沪股通', '融资融券', '市场连板股', 'ST板块'}
_concept_members_cache = None    # {concept: [code, ...]}, 1小时缓存
_concept_members_ts = 0


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


# ============== 交易日历(跨 download/hot/caizhaomao 共用) ==============
_TRADE_DAYS = None        # A股交易日集合(YYYYMMDD), mootdx 数据源
_TRADE_DAYS_MAX = None    # 日历覆盖的最新交易日


def get_trade_days():
    """
    返回A股交易日集合(YYYYMMDD)，用 mootdx 交易日历(新浪数据源)，带内存缓存。
    获取失败返回 None（调用方回退到工作日判断）。
    """
    global _TRADE_DAYS, _TRADE_DAYS_MAX
    if _TRADE_DAYS is not None:
        return _TRADE_DAYS
    try:
        from mootdx.utils.holiday import holidays
        df = holidays()
        # mootdx holidays() 返回列 ['date', 'year'], date 为 datetime.date
        days = {str(d).replace('-', '') for d in df['date']}
        if not days:
            return None
        _TRADE_DAYS = days
        _TRADE_DAYS_MAX = max(days)
        print(f"[交易日历] 已加载 {len(days)} 个交易日 (至 {_TRADE_DAYS_MAX})")
        return _TRADE_DAYS
    except Exception as e:
        print(f"[交易日历] 获取失败，回退到工作日判断: {e}")
        return None


def is_trading_day(date_folder):
    """
    判断 date_folder(YYYYMMDD) 是否为A股交易日。
    - 交易日历可用: 命中集合即为交易日；若日期超出日历覆盖范围则回退到工作日判断
    - 交易日历不可用: 回退到周一~周五判断
    """
    from datetime import datetime
    days = get_trade_days()
    if days:
        # 超出日历覆盖范围（比最新已知交易日还新）时回退到工作日判断
        if _TRADE_DAYS_MAX and date_folder > _TRADE_DAYS_MAX:
            try:
                return datetime.strptime(date_folder, '%Y%m%d').weekday() < 5
            except ValueError:
                return True
        return date_folder in days
    # 日历不可用: 工作日
    try:
        return datetime.strptime(date_folder, '%Y%m%d').weekday() < 5
    except ValueError:
        return True


def trading_days_in_range(start8, end8):
    """返回 [start8, end8] 区间内的所有A股交易日(YYYYMMDD)，升序。"""
    from datetime import datetime as _dt, timedelta as _td
    try:
        s = _dt.strptime(start8, '%Y%m%d')
        e = _dt.strptime(end8, '%Y%m%d')
    except ValueError:
        return []
    if s > e:
        s, e = e, s
    days = []
    cur = s
    while cur <= e:
        d8 = cur.strftime('%Y%m%d')
        if is_trading_day(d8):
            days.append(d8)
        cur += _td(days=1)
    return days


# ============== 涨停阈值 / K线日期工具(跨 hot/caizhaomao 共用) ==============
def _limit_threshold(code):
    """涨停判定阈值: 创业板(30x)/科创板(68x)=19.8, 其它主板=9.8。
    口径与zt-stats的 code.startswith(('30','68')) 一致, 保证产业链与涨停分布统计可比。"""
    return 19.8 if code.startswith(('30', '68')) else 9.8


def _bar_date8(b):
    """K线 bar 的 date('YYYY-MM-DD') -> 'YYYYMMDD'"""
    return str(b.get('date', ''))[:10].replace('-', '')


# ============== 通达信本地 .day/.lc1 读取(跨 hot/monitor/caizhaomao 共用) ==============
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
