import os
import sqlite3
import time
import json
import hashlib
import uuid
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=7))
DB = os.getenv("SCORE_DB", "/data/score.sqlite3")

CHAT_ID = os.getenv("SCORE_TELEGRAM_CHAT_ID", "7881007164")
BOT_TOKEN = os.getenv("SCORE_TELEGRAM_BOT_TOKEN", "")

DAILY_HOUR = 7
DAILY_MINUTE = 5


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

    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
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
        (id, system, symbol, direction,
         ep, tp1, tp2, sl, created)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        signal_id, system, symbol, direction,
        ep, tp1, tp2, sl, created
    ))

    inserted = cur.rowcount
    c.commit()
    c.close()

    return signal_id, inserted


def setout(signal_id, status, tp1=0, tp2=0, sl=0):
    if status not in {
        "WIN", "LOSS", "BREAKEVEN", "PENDING"
    }:
        raise ValueError(status)

    c = conn()

    c.execute("""
        UPDATE signals
        SET status = ?,
            tp1_hit = max(tp1_hit, ?),
            tp2_hit = max(tp2_hit, ?),
            sl_hit = max(sl_hit, ?)
        WHERE id = ?
    """, (
        status, tp1, tp2, sl, signal_id
    ))

    c.commit()
    c.close()


def stats(start_ts, end_ts, system=None):
    c = conn()

    query = """
        SELECT * FROM signals
        WHERE created >= ? AND created < ?
    """

    args = [start_ts, end_ts]

    if system:
        query += " AND system = ?"
        args.append(system)

    rows = c.execute(query, args).fetchall()
    c.close()

    counts = {
        "WIN": 0,
        "LOSS": 0,
        "BREAKEVEN": 0,
        "PENDING": 0,
    }

    for row in rows:
        status = row["status"]

        if status not in counts:
            raise RuntimeError(
                "Unknown status: %s" % status
            )

        counts[status] += 1

    result = {
        "signals": len(rows),
        "wins": counts["WIN"],
        "losses": counts["LOSS"],
        "breakeven": counts["BREAKEVEN"],
        "pending": counts["PENDING"],
        "tp1": sum(r["tp1_hit"] for r in rows),
        "tp2": sum(r["tp2_hit"] for r in rows),
        "sl": sum(r["sl_hit"] for r in rows),
    }

    decided = (
        result["wins"]
        + result["losses"]
        + result["breakeven"]
    )

    result["decided_win_rate"] = (
        round(
            100 * result["wins"] / decided,
            2
        )
        if decided else None
    )

    result["invariant_ok"] = (
        result["signals"]
        ==
        result["wins"]
        + result["losses"]
        + result["breakeven"]
        + result["pending"]
    )

    return result


def boot_marker():
    c = conn()

    row = c.execute("""
        SELECT value FROM meta
        WHERE key = 'instance_uuid'
    """).fetchone()

    if row:
        instance_uuid = row["value"]
    else:
        instance_uuid = str(uuid.uuid4())

        c.execute("""
            INSERT INTO meta(key, value)
            VALUES('instance_uuid', ?)
        """, (instance_uuid,))

    row = c.execute("""
        SELECT value FROM meta
        WHERE key = 'boot_count'
    """).fetchone()

    boot_count = (
        int(row["value"]) + 1
        if row else 1
    )

    c.execute("""
        INSERT INTO meta(key, value)
        VALUES('boot_count', ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
    """, (str(boot_count),))

    c.commit()
    c.close()

    return instance_uuid, boot_count


def period(kind, now=None):
    now = now or datetime.now(TZ)

    end = now.replace(
        hour=DAILY_HOUR,
        minute=DAILY_MINUTE,
        second=0,
        microsecond=0,
    )

    if kind == "daily":
        start = end - timedelta(days=1)

    elif kind == "weekly":
        start = end - timedelta(days=7)

    else:
        raise ValueError(kind)

    return (
        int(start.timestamp()),
        int(end.timestamp()),
        start,
        end,
    )


def report_text(kind, result, start, end, test=False):
    prefix = "🧪 TEST " if test else ""

    integrity = (
        "OK"
        if result["invariant_ok"]
        else "DATA_GAP"
    )

    win_rate = (
        "N/A"
        if result["decided_win_rate"] is None
        else f'{result["decided_win_rate"]:.2f}%'
    )

    return (
        f"{prefix}JJ SCORE {kind.upper()}\n"
        f"Period: {start.isoformat()} → {end.isoformat()}\n"
        f"Signals: {result['signals']} | "
        f"WIN: {result['wins']} | "
        f"LOSS: {result['losses']} | "
        f"BE: {result['breakeven']} | "
        f"Pending: {result['pending']}\n"
        f"TP1: {result['tp1']} | "
        f"TP2: {result['tp2']} | "
        f"SL: {result['sl']}\n"
        f"Decided Win Rate: {win_rate}\n"
        f"Integrity: {integrity}"
    )


def telegram_send(text):
    if not BOT_TOKEN:
        raise RuntimeError(
            "SCORE_TELEGRAM_BOT_TOKEN missing"
        )

    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
    }).encode()

    request = urllib.request.Request(
        "https://api.telegram.org/bot"
        + BOT_TOKEN
        + "/sendMessage",
        data=data,
        method="POST",
    )

    with urllib.request.urlopen(
        request,
        timeout=15,
    ) as response:
        obj = json.loads(
            response.read().decode()
        )

    if not obj.get("ok"):
        raise RuntimeError(
            "Telegram ok=false"
        )

    return int(
        obj["result"]["message_id"]
    )


def send_report(kind, test=False, now=None):
    start_ts, end_ts, start, end = period(
        kind, now
    )

    report_id = (
        ("TEST:" if test else "")
        + kind
        + ":"
        + str(end_ts)
    )

    c = conn()

    row = c.execute("""
        SELECT message_id FROM reports
        WHERE id = ?
    """, (report_id,)).fetchone()

    c.close()

    if row:
        return {
            "dedup": True,
            "message_id": row["message_id"],
        }

    result = stats(
        start_ts,
        end_ts,
    )

    text = report_text(
        kind,
        result,
        start,
        end,
        test,
    )

    message_id = telegram_send(text)

    c = conn()

    c.execute("""
        INSERT INTO reports(
            id, message_id, sent
        )
        VALUES (?, ?, ?)
    """, (
        report_id,
        message_id,
        int(time.time()),
    ))

    c.commit()
    c.close()

    return {
        "dedup": False,
        "message_id": message_id,
    }


def due(now, last_minute):
    stamp = now.strftime(
        "%Y-%m-%d %H:%M"
    )

    if (
        now.hour == DAILY_HOUR
        and now.minute == DAILY_MINUTE
        and stamp != last_minute
    ):
        kind = (
            "weekly"
            if now.weekday() == 0
            else "daily"
        )

        return kind, stamp

    return None, last_minute


def selftest():
    global DB

    real_db = DB
    DB = (
        "/tmp/"
        "jj_score_engine_selftest.sqlite3"
    )

    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB + suffix)
        except FileNotFoundError:
            pass

    try:
        now = 1700000000

        win_id, a = add(
            "TEST", "AAA", "LONG",
            1, 2, 3, 0.5, now
        )

        loss_id, b = add(
            "TEST", "BBB", "SHORT",
            3, 2, 1, 4, now + 1
        )

        _, c = add(
            "TEST", "CCC", "LONG",
            1, 2, 3, 0.5, now + 2
        )

        _, duplicate = add(
            "TEST", "AAA", "LONG",
            1, 2, 3, 0.5, now
        )

        setout(
            win_id,
            "WIN",
            tp1=1,
            tp2=1,
        )

        setout(
            loss_id,
            "LOSS",
            sl=1,
        )

        result = stats(
            now - 1,
            now + 10,
        )

        assert (
            a, b, c, duplicate
        ) == (1, 1, 1, 0)

        assert result["signals"] == 3
        assert result["wins"] == 1
        assert result["losses"] == 1
        assert result["pending"] == 1
        assert result["tp1"] == 1
        assert result["tp2"] == 1
        assert result["sl"] == 1
        assert result["invariant_ok"]

        instance1, boot1 = boot_marker()
        instance2, boot2 = boot_marker()

        assert instance1 == instance2
        assert boot2 == boot1 + 1

        monday = datetime(
            2026, 10, 5,
            7, 5,
            tzinfo=TZ,
        )

        kind, _ = due(
            monday, None
        )

        assert kind == "weekly"

        tuesday = datetime(
            2026, 10, 6,
            7, 5,
            tzinfo=TZ,
        )

        kind, _ = due(
            tuesday, None
        )

        assert kind == "daily"

        print(json.dumps({
            "SELFTEST": "PASS",
            "DEDUP": "PASS",
            "INVARIANT": "PASS",
            "PERSISTENCE_MARKER": "PASS",
            "SCHEDULER": "PASS",
            "stats": result,
        }), flush=True)

    finally:
        DB = real_db


def main():
    selftest()

    instance_uuid, boot_count = (
        boot_marker()
    )

    print(json.dumps({
        "BOOT": "PASS",
        "db": DB,
        "timezone": "Asia/Bangkok",
        "instance_uuid": instance_uuid,
        "boot_count": boot_count,
    }), flush=True)

    if os.getenv(
        "SCORE_SEND_TEST"
    ) == "1":
        try:
            print(json.dumps({
                "TELEGRAM_TEST":
                    send_report(
                        "daily",
                        test=True,
                    )
            }), flush=True)

        except Exception as exc:
            print(json.dumps({
                "TELEGRAM_TEST": "FAIL",
                "error": str(exc),
            }), flush=True)

    last_minute = None

    while True:
        now = datetime.now(TZ)

        kind, stamp = due(
            now,
            last_minute,
        )

        if kind:
            try:
                result = send_report(
                    kind
                )

                print(json.dumps({
                    "REPORT": kind,
                    "result": result,
                }), flush=True)

                last_minute = stamp

            except Exception as exc:
                print(json.dumps({
                    "REPORT": kind,
                    "error": str(exc),
                }), flush=True)

        print(json.dumps({
            "HEARTBEAT":
                now.isoformat(),
            "boot_count":
                boot_count,
        }), flush=True)

        time.sleep(30)


if __name__ == "__main__":
    main()
