#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
同花顺概念数据同步模块

整合自 Desktop\AI盯盘 项目的 export_concepts.py + load_to_postgres.py

功能:
  - fetch_all_concepts(): 通过 adata 拉取全市场 A 股「个股 -> 同花顺概念」映射
  - load_to_db(): 将 CSV 导入 PostgreSQL (ths schema)
  - get_status(): 获取当前同步状态
  - diff_with_last(): 对比上次同步结果, 返回新增/删除的映射

数据源: adata.stock.info (同花顺另一套接口, 一次调用返回全部成分股, 无需翻页)
存储: CSV (断点续跑) + PostgreSQL ths schema (最新快照)
"""

import os
import time
from datetime import datetime

import pandas as pd

# CSV 文件路径 (与原 Desktop 项目保持一致, 便于复用已有数据)
LONG_CSV = "ths_concept_members_long.csv"      # code, concept (多对多长表)
WIDE_CSV = "ths_stock_concept.csv"             # code, block_name (每股一行)
STATUS_CSV = "ths_concept_status.csv"          # concept_code, concept, ok, count (断点)

SCHEMA = "ths"


# ============== 断点续跑 ==============

def load_done():
    """已成功抓完的概念(concept_code), 用于断点续跑。"""
    if not os.path.exists(STATUS_CSV):
        return set()
    try:
        df = pd.read_csv(STATUS_CSV, dtype=str).drop_duplicates("concept_code", keep="last")
        return set(df[df["ok"] == "True"]["concept_code"])
    except Exception:
        return set()


def record_status(concept_code, name, ok, count):
    header = not os.path.exists(STATUS_CSV)
    pd.DataFrame([{"concept_code": concept_code, "concept": name, "ok": ok, "count": count}]).to_csv(
        STATUS_CSV, mode="a", header=header, index=False, encoding="utf-8-sig"
    )


def append_long(rows):
    header = not os.path.exists(LONG_CSV)
    pd.DataFrame(rows).to_csv(LONG_CSV, mode="a", header=header, index=False, encoding="utf-8-sig")


def build_wide():
    """长表聚合成 每股一行、概念逗号分隔 的宽表。"""
    if not os.path.exists(LONG_CSV):
        return 0
    df = pd.read_csv(LONG_CSV, dtype=str).dropna()
    df = df.drop_duplicates(["code", "concept"])
    wide = (
        df.groupby("code")["concept"]
        .agg(lambda x: ",".join(sorted(set(x))))
        .reset_index()
        .rename(columns={"concept": "block_name"})
    )
    wide.to_csv(WIDE_CSV, index=False, encoding="utf-8-sig")
    return len(wide)


# ============== 拉取概念成分股 ==============

def fetch_members(index_code, concept_code, name, retries=3):
    """抓一个概念的全部成分股代码(纯6位)。"""
    def _pick(df):
        if df is not None and not df.empty:
            codes = df["stock_code"].astype(str).str.zfill(6)
            return sorted(codes[codes.str.fullmatch(r"\d{6}")].unique())
        return []

    attempts = []
    if isinstance(index_code, str) and index_code and index_code.lower() != "nan":
        attempts.append({"index_code": index_code})
    if isinstance(concept_code, str) and concept_code and concept_code.lower() != "nan":
        attempts.append({"concept_code": concept_code})
    if isinstance(name, str) and name:
        attempts.append({"name": name})

    import adata
    for _ in range(retries):
        for kw in attempts:
            try:
                res = _pick(adata.stock.info.concept_constituent_ths(**kw))
                if res:
                    return res
            except Exception:
                pass
        time.sleep(1.0)
    return []


def fetch_all_concepts(refresh=False, limit=0, sleep=0.3, on_progress=None):
    """全量拉取同花顺概念成分股, 写 CSV。

    Args:
        refresh: 忽略断点, 全部重抓
        limit: 仅处理前 N 个概念(0=全部)
        sleep: 每个概念间隔秒数
        on_progress: 回调 fn(idx, total, name, count, ok) 用于实时进度

    Returns:
        dict: {total, done, failed, failed_names: []}
    """
    import adata

    if refresh:
        for f in (LONG_CSV, STATUS_CSV):
            if os.path.exists(f):
                os.remove(f)

    # 先保存旧长表快照(用于 diff)
    old_long = set()
    if os.path.exists(LONG_CSV):
        try:
            df_old = pd.read_csv(LONG_CSV, dtype=str).dropna()
            old_long = set(zip(df_old["code"], df_old["concept"]))
        except Exception:
            pass

    concepts = adata.stock.info.all_concept_code_ths()
    if limit:
        concepts = concepts.head(limit)
    done = load_done()
    total = len(concepts)
    todo = total - concepts["concept_code"].astype(str).isin(done).sum()

    if on_progress:
        on_progress(0, total, "开始拉取...", 0, True)

    failed = []
    for i, r in enumerate(concepts.itertuples(index=False), 1):
        ccode, name, icode = str(r.concept_code), r.name, str(r.index_code)
        if ccode in done:
            if on_progress:
                on_progress(i, total, f"{name} (跳过)", 0, True)
            continue
        members = fetch_members(icode, ccode, name)
        if members:
            append_long([{"code": c, "concept": name} for c in members])
            record_status(ccode, name, True, len(members))
            if on_progress:
                on_progress(i, total, name, len(members), True)
        else:
            record_status(ccode, name, False, 0)
            failed.append(name)
            if on_progress:
                on_progress(i, total, name, 0, False)
        time.sleep(sleep)

    # 聚合宽表
    stock_count = build_wide()

    # 计算改动 (新增/删除的 code-concept 对)
    new_long = set()
    if os.path.exists(LONG_CSV):
        try:
            df_new = pd.read_csv(LONG_CSV, dtype=str).dropna()
            new_long = set(zip(df_new["code"], df_new["concept"]))
        except Exception:
            pass
    added = new_long - old_long
    removed = old_long - new_long

    return {
        "total": total,
        "done": total - len(failed),
        "failed": len(failed),
        "failed_names": failed[:20],
        "stock_count": stock_count,
        "added": len(added),
        "removed": len(removed),
        "changes": {
            "added": [{"code": c, "concept": con} for c, con in sorted(added)[:200]],
            "removed": [{"code": c, "concept": con} for c, con in sorted(removed)[:200]],
        },
    }


# ============== PostgreSQL 入库 ==============

_DDL = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

DROP TABLE IF EXISTS {SCHEMA}.stock_concept;
CREATE TABLE {SCHEMA}.stock_concept (
    code        varchar(6) PRIMARY KEY,
    block_name  text        NOT NULL,
    updated_at  timestaptz NOT NULL DEFAULT now()
);

DROP TABLE IF EXISTS {SCHEMA}.concept_member;
CREATE TABLE {SCHEMA}.concept_member (
    concept     text        NOT NULL,
    stock_code  varchar(6)  NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (concept, stock_code)
);
CREATE INDEX idx_concept_member_stock ON {SCHEMA}.concept_member (stock_code);
"""

_DDL_CORRECT = f"""
CREATE SCHEMA IF NOT EXISTS {SCHEMA};

DROP TABLE IF EXISTS {SCHEMA}.stock_concept;
CREATE TABLE {SCHEMA}.stock_concept (
    code        varchar(6) PRIMARY KEY,
    block_name  text        NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

DROP TABLE IF EXISTS {SCHEMA}.concept_member;
CREATE TABLE {SCHEMA}.concept_member (
    concept     text        NOT NULL,
    stock_code  varchar(6)  NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (concept, stock_code)
);
CREATE INDEX idx_concept_member_stock ON {SCHEMA}.concept_member (stock_code);
"""


def load_to_db():
    """将 CSV 导入 PostgreSQL ths schema (drop+recreate, 保证最新快照)。

    Returns:
        dict: {success, stock_count, member_count, concept_count} 或 {success:False, error}
    """
    for f in (WIDE_CSV, LONG_CSV):
        if not os.path.exists(f):
            return {"success": False, "error": f"缺少 {f}, 请先运行同步"}

    try:
        import db as _db
        conn = _db.get_conn()
    except Exception as e:
        return {"success": False, "error": f"数据库连接失败: {e}"}

    try:
        wide = pd.read_csv(WIDE_CSV, dtype=str).dropna()
        long_df = pd.read_csv(LONG_CSV, dtype=str).dropna().drop_duplicates(["code", "concept"])
        long_df = long_df.rename(columns={"code": "stock_code"})[["concept", "stock_code"]]
        now = datetime.now()

        with conn.cursor() as cur:
            for stmt in filter(str.strip, _DDL_CORRECT.split(";")):
                cur.execute(stmt)

            # 批量插入宽表
            from psycopg2.extras import execute_values
            wide_rows = [(r["code"], r["block_name"], now) for _, r in wide.iterrows()]
            execute_values(cur, f"INSERT INTO {SCHEMA}.stock_concept (code, block_name, updated_at) VALUES %s", wide_rows, page_size=2000)

            # 批量插入长表
            long_rows = [(r["concept"], r["stock_code"], now) for _, r in long_df.iterrows()]
            execute_values(cur, f"INSERT INTO {SCHEMA}.concept_member (concept, stock_code, updated_at) VALUES %s", long_rows, page_size=5000)

            # 统计
            cur.execute(f"SELECT count(*) FROM {SCHEMA}.stock_concept")
            n_stock = cur.fetchone()[0]
            cur.execute(f"SELECT count(*) FROM {SCHEMA}.concept_member")
            n_member = cur.fetchone()[0]
            cur.execute(f"SELECT count(DISTINCT concept) FROM {SCHEMA}.concept_member")
            n_concept = cur.fetchone()[0]

        conn.commit()
        return {
            "success": True,
            "stock_count": n_stock,
            "member_count": n_member,
            "concept_count": n_concept,
        }
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return {"success": False, "error": f"{type(e).__name__}: {e}"}
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ============== 状态查询 ==============

def get_status():
    """获取当前同步状态 (CSV + DB)。"""
    result = {
        "csv": {"long_rows": 0, "wide_stocks": 0, "concepts_done": 0, "concepts_failed": 0},
        "db": {"available": False, "stock_count": 0, "member_count": 0, "concept_count": 0, "updated_at": None},
    }

    # CSV 状态
    if os.path.exists(LONG_CSV):
        try:
            result["csv"]["long_rows"] = len(pd.read_csv(LONG_CSV, dtype=str))
        except Exception:
            pass
    if os.path.exists(WIDE_CSV):
        try:
            result["csv"]["wide_stocks"] = len(pd.read_csv(WIDE_CSV, dtype=str))
        except Exception:
            pass
    if os.path.exists(STATUS_CSV):
        try:
            df_s = pd.read_csv(STATUS_CSV, dtype=str)
            result["csv"]["concepts_done"] = int((df_s["ok"] == "True").sum())
            result["csv"]["concepts_failed"] = int((df_s["ok"] == "False").sum())
        except Exception:
            pass

    # DB 状态
    try:
        import db as _db
        conn = _db.get_conn()
        with conn.cursor() as cur:
            cur.execute(f"SELECT count(*) FROM {SCHEMA}.stock_concept")
            result["db"]["stock_count"] = cur.fetchone()[0]
            cur.execute(f"SELECT count(*) FROM {SCHEMA}.concept_member")
            result["db"]["member_count"] = cur.fetchone()[0]
            cur.execute(f"SELECT count(DISTINCT concept) FROM {SCHEMA}.concept_member")
            result["db"]["concept_count"] = cur.fetchone()[0]
            cur.execute(f"SELECT max(updated_at) FROM {SCHEMA}.stock_concept")
            ts = cur.fetchone()[0]
            result["db"]["updated_at"] = str(ts) if ts else None
            result["db"]["available"] = True
        conn.close()
    except Exception:
        result["db"]["available"] = False

    return result


def get_stock_concepts(code):
    """查询单只股票的概念列表 (从 DB)。"""
    try:
        import db as _db
        conn = _db.get_conn()
        with conn.cursor() as cur:
            cur.execute(f"SELECT block_name FROM {SCHEMA}.stock_concept WHERE code = %s", (code,))
            row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return row[0].split(",")
        return []
    except Exception:
        # DB 不可用时回退读 CSV
        if os.path.exists(WIDE_CSV):
            try:
                df = pd.read_csv(WIDE_CSV, dtype=str)
                row = df[df["code"] == code]
                if not row.empty and row.iloc[0]["block_name"]:
                    return row.iloc[0]["block_name"].split(",")
            except Exception:
                pass
        return []


def list_concepts(limit=100, offset=0):
    """列出所有概念及其成分股数量 (从 DB)。"""
    try:
        import db as _db
        conn = _db.get_conn()
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT concept, count(*) as cnt
                FROM {SCHEMA}.concept_member
                GROUP BY concept
                ORDER BY cnt DESC
                LIMIT %s OFFSET %s
            """, (limit, offset))
            rows = cur.fetchall()
            cur.execute(f"SELECT count(DISTINCT concept) FROM {SCHEMA}.concept_member")
            total = cur.fetchone()[0]
        conn.close()
        return {"concepts": [{"concept": r[0], "count": r[1]} for r in rows], "total": total}
    except Exception:
        # DB 不可用时回退读 CSV
        if os.path.exists(LONG_CSV):
            try:
                df = pd.read_csv(LONG_CSV, dtype=str).dropna()
                grp = df.groupby("concept").size().reset_index(name="count").sort_values("count", ascending=False)
                total = len(grp)
                rows = grp.iloc[offset:offset + limit]
                return {"concepts": [{"concept": r["concept"], "count": int(r["count"])} for _, r in rows.iterrows()], "total": total}
            except Exception:
                pass
        return {"concepts": [], "total": 0}
