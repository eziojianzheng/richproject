# -*- coding: utf-8 -*-
"""server.bp_ths_sync - 同花顺概念数据同步。

原 api_server.py 行 3865-3955。
"""
import re
import threading
from flask import Blueprint, request, jsonify

from .common import ht  # noqa: F401  (保持与原文件一致的 hot_track 引入)

ths_sync_bp = Blueprint('ths_sync', __name__)

_ths_sync_running = False
_ths_sync_progress = {'current': 0, 'total': 0, 'name': '', 'count': 0, 'ok': True, 'done': False, 'result': None}


@ths_sync_bp.route('/api/sync/ths/status', methods=['GET'])
def ths_sync_status():
    """获取同花顺概念同步状态 (CSV + DB 数据量统计)。"""
    try:
        import ths_sync
        status = ths_sync.get_status()
        return jsonify({'success': True, 'status': status, 'syncing': _ths_sync_running, 'progress': _ths_sync_progress})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@ths_sync_bp.route('/api/sync/ths/concepts', methods=['POST'])
def ths_sync_concepts():
    """同步同花顺概念数据 (拉取 adata -> CSV -> PostgreSQL)。
    异步执行, 立即返回; 前端轮询 /api/sync/ths/status 获取进度。"""
    global _ths_sync_running, _ths_sync_progress
    if _ths_sync_running:
        return jsonify({'success': False, 'error': '同步正在进行中'}), 409

    refresh = request.json.get('refresh', False) if request.json else False

    _ths_sync_running = True
    _ths_sync_progress = {'current': 0, 'total': 0, 'name': '初始化...', 'count': 0, 'ok': True, 'done': False, 'result': None}

    def _run():
        global _ths_sync_running, _ths_sync_progress
        try:
            import ths_sync

            def on_progress(idx, total, name, count, ok):
                _ths_sync_progress['current'] = idx
                _ths_sync_progress['total'] = total
                _ths_sync_progress['name'] = name
                _ths_sync_progress['count'] = count
                _ths_sync_progress['ok'] = ok

            # 1. 拉取概念数据 -> CSV
            fetch_result = ths_sync.fetch_all_concepts(refresh=refresh, on_progress=on_progress)

            # 2. 入库 PostgreSQL
            db_result = ths_sync.load_to_db()

            _ths_sync_progress['done'] = True
            _ths_sync_progress['result'] = {
                'fetch': fetch_result,
                'db': db_result,
            }
        except Exception as e:
            _ths_sync_progress['done'] = True
            _ths_sync_progress['result'] = {'error': f'{type(e).__name__}: {e}'}
        finally:
            _ths_sync_running = False

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return jsonify({'success': True, 'message': '同步已启动'})


@ths_sync_bp.route('/api/sync/ths/concepts/list', methods=['GET'])
def ths_concepts_list():
    """列出所有同花顺概念及其成分股数量。"""
    try:
        import ths_sync
        limit = request.args.get('limit', default=100, type=int)
        offset = request.args.get('offset', default=0, type=int)
        result = ths_sync.list_concepts(limit=min(limit, 500), offset=offset)
        return jsonify({'success': True, **result})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500


@ths_sync_bp.route('/api/sync/ths/stock-concepts', methods=['GET'])
def ths_stock_concepts():
    """查询单只股票的同花顺概念列表。"""
    code = request.args.get('code', '')
    if not re.match(r'^\d{6}$', code):
        return jsonify({'success': False, 'error': 'code 需为6位数字'}), 400
    try:
        import ths_sync
        concepts = ths_sync.get_stock_concepts(code)
        return jsonify({'success': True, 'code': code, 'concepts': concepts})
    except Exception as e:
        return jsonify({'success': False, 'error': f'{type(e).__name__}: {e}'}), 500
