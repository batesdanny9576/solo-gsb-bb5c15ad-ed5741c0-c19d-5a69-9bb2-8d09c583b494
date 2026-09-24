"""SQLite 连接与建表。

为便于区间比较，时间统一以 ``YYYY-MM-DDTHH:MM:SSZ``（UTC）文本保存，
该格式的字典序与时间顺序一致，因此可直接用字符串比较。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

DB_PATH = os.getenv("TRACE_DB_PATH", "/data/traceability.db")

# 写事务（BEGIN IMMEDIATE）等待锁的最长时间，避免并发预约直接报 database is locked
BUSY_TIMEOUT_MS = 5000

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    code        TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    manufacturer TEXT,
    model       TEXT,
    serial_no   TEXT,
    is_national_standard INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS certificates (
    id              TEXT PRIMARY KEY,
    instrument_code TEXT NOT NULL REFERENCES instruments(code),
    cert_number     TEXT NOT NULL,
    issued_at       TEXT NOT NULL,
    valid_from      TEXT NOT NULL,
    valid_until     TEXT NOT NULL,
    uncertainty     REAL NOT NULL,
    unit            TEXT NOT NULL DEFAULT '',
    digest_alg      TEXT NOT NULL DEFAULT 'sha256',
    signature_digest TEXT NOT NULL,
    signed_payload  TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cert_instrument ON certificates(instrument_code);
CREATE INDEX IF NOT EXISTS idx_cert_interval ON certificates(valid_from, valid_until);

CREATE TABLE IF NOT EXISTS trace_edges (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    child_cert_id    TEXT NOT NULL REFERENCES certificates(id),
    parent_cert_id   TEXT NOT NULL REFERENCES certificates(id),
    valid_from       TEXT NOT NULL,
    valid_until      TEXT NOT NULL,
    note             TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    UNIQUE(child_cert_id, parent_cert_id)
);

CREATE INDEX IF NOT EXISTS idx_edge_child ON trace_edges(child_cert_id);
CREATE INDEX IF NOT EXISTS idx_edge_parent ON trace_edges(parent_cert_id);

-- 校准工位预约：全实验室共用单一工位（station_code 固定 CAL-STATION-01），
-- 占用区间按半开区间 [start_at, end_at) 判定，首尾相接不算冲突。
-- start_certificate_id / certificate_valid_until 记录确认时刻的溯源链
-- 起点证书及其到期时间，作为“预约在到期前结束”约束的持久化证据。
CREATE TABLE IF NOT EXISTS calibration_bookings (
    id              TEXT PRIMARY KEY,
    station_code    TEXT NOT NULL,
    instrument_code TEXT NOT NULL REFERENCES instruments(code),
    start_at        TEXT NOT NULL,
    end_at          TEXT NOT NULL,
    duration_minutes INTEGER NOT NULL,
    start_certificate_id    TEXT NOT NULL REFERENCES certificates(id),
    certificate_valid_until TEXT NOT NULL,
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_booking_instrument
    ON calibration_bookings(instrument_code);
CREATE INDEX IF NOT EXISTS idx_booking_station_interval
    ON calibration_bookings(station_code, start_at, end_at);
"""


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(
        DB_PATH, check_same_thread=False, timeout=BUSY_TIMEOUT_MS / 1000
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def get_conn_ctx() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_db() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个连接。"""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def get_preflight_conn() -> Iterator[sqlite3.Connection]:
    """预检专用连接：独立于请求连接，进入即开启事务，退出一律 ROLLBACK。

    预检流程可在其中照常执行 upsert 语句做高保真模拟，但任何写入都随回滚
    消失，绝不持久化到数据库。
    """
    conn = _connect()
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@contextmanager
def get_write_tx() -> Iterator[sqlite3.Connection]:
    """写事务专用连接：进入即 ``BEGIN IMMEDIATE`` 立即取得 SQLite 写锁。

    预约确认需要在“同一写事务”内先重查占用与证书截止、再插入预约，
    以 IMMEDIATE 方式开启事务可保证两个并发预约在占用检查阶段即被串行化，
    杜绝各自读到不冲突、随后双双写入的竞态。正常退出 COMMIT，异常 ROLLBACK。
    """
    conn = _connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn_ctx() as conn:
        conn.executescript(SCHEMA)
