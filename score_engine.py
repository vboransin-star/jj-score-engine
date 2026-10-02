import os
import sqlite3
import time
import json
import hashlib
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=7))
DB = os.getenv("SCORE_DB", "/data/score.sqlite3")


def conn():
    db_dir = os.path.dirname(DB)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")

    c.executescript("""
    CREATE TABLE IF NOT EXISTS signals (
        id TEXT PRIMARY KEY,
        system TEXT,
        symbol TEXT,
        direction TEXT,
        ep REAL,
        tp1 REAL,
        tp2 REAL,
        sl REAL,
        created INTEGER,
        status TEXT DEFAULT 'PENDING',
        tp1_hit INTEGER DEFAULT 0,
        tp2_hit INTEGER DEFAULT 0,
        sl_hit INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS reports (
        id TEXT PRIMARY KEY,
        message_id INTEGER,
        sent INTEGER
    );
    """)

    c.commit()
    return c


def sid(*parts):
    raw = "|".join(map(str, parts))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


def add(system, symbol, direction, ep, tp1, tp2, sl, created):
    signal_id = sid(
        system, symbol, direction,
        ep, tp1, tp2, sl, created
    )

    c = conn()

    cur = c.execute("""
        INSERT OR IGNORE INTO signals
        (id, system, symbol, direction, ep, tp1, tp2, sl, created)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        signal_id,
        system,
        symbol,
        direction,
        ep,
        tp1,
        tp2,
        sl,
        created
    ))

    inserted = cur.rowcount
    c.commit()
    c.close()

    return signal_id, inserted


def setout(signal_id, status, tp1=0, tp2=0, sl=0):
    c = conn()

    c.execute("""
        UPDATE signals
        SET
            status = ?,
            tp1_hit = max(tp1_hit, ?),
            tp2_hit = max(tp2_hit, ?),
            sl_hit = max(sl_hit, ?)
        WHERE id = ?
    """, (
        status,
        tp1,
        tp2,
        sl,
        signal_id
    ))

    c.commit()
    c.close()


def stats(start_ts, end_ts):
    c = conn()

    rows = c.execute("""
        SELECT *
        FROM signals
        WHERE created >= ?
          AND created < ?
    """, (start_ts, end_ts)).fetchall()

    c.close()

    counts = {
        "WIN": 0,
        "LOSS": 0,
        "BREAKEVEN": 0,
        "PENDING": 0
    }

    for row in rows:
        status = row["status"]

        if status not in counts:
            raise RuntimeError(
                "Unknown signal status: %s" % status
            )

        counts[status] += 1

    result = {
        "signals": len(rows),
        "wins": counts["WIN"],
        "losses": counts["LOSS"],
        "breakeven": counts["BREAKEVEN"],
        "pending": counts["PENDING"],
        "tp1": sum(row["tp1_hit"] for row in rows),
        "tp2": sum(row["tp2_hit"] for row in rows),
        "sl": sum(row["sl_hit"] for row in rows)
    }

    result["invariant_ok"] = (
        result["signals"]
        ==
        result["wins"]
        + result["losses"]
        + result["breakeven"]
        + result["pending"]
    )

    return result


def selftest():
    global DB

    real_db = DB
    test_db = "/tmp/jj_score_engine_selftest.sqlite3"

    DB = test_db

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(test_db + suffix)
        except FileNotFoundError:
            pass

    try:
        now = int(time.time())

        win_id, inserted1 = add(
            "TEST",
            "AAA",
            "LONG",
            1.0,
            2.0,
            3.0,
            0.5,
            now
        )

        loss_id, inserted2 = add(
            "TEST",
            "BBB",
            "SHORT",
            3.0,
            2.0,
            1.0,
            4.0,
            now + 1
        )

        _, inserted3 = add(
            "TEST",
            "CCC",
            "LONG",
            1.0,
            2.0,
            3.0,
            0.5,
            now + 2
        )

        _, duplicate_inserted = add(
            "TEST",
            "AAA",
            "LONG",
            1.0,
            2.0,
            3.0,
            0.5,
            now
        )

        setout(
            win_id,
            "WIN",
            tp1=1,
            tp2=1,
            sl=0
        )

        setout(
            loss_id,
            "LOSS",
            tp1=0,
            tp2=0,
            sl=1
        )

        result = stats(
            now - 1,
            now + 10
        )

        expected = {
            "signals": 3,
            "wins": 1,
            "losses": 1,
            "breakeven": 0,
            "pending": 1,
            "tp1": 1,
            "tp2": 1,
            "sl": 1,
            "invariant_ok": True
        }

        assert inserted1 == 1
        assert inserted2 == 1
        assert inserted3 == 1
        assert duplicate_inserted == 0
        assert result == expected, (result, expected)

        print(json.dumps({
            "SELFTEST": "PASS",
            "DEDUP": "PASS",
            "INVARIANT": "PASS",
            "stats": result
        }), flush=True)

    finally:
        DB = real_db


def main():
    selftest()

    c = conn()
    c.close()

    print(json.dumps({
        "BOOT": "PASS",
        "db": DB,
        "timezone": "Asia/Bangkok"
    }), flush=True)

    while True:
        print(json.dumps({
            "HEARTBEAT": datetime.now(TZ).isoformat()
        }), flush=True)

        time.sleep(60)


if __name__ == "__main__":
    main()
