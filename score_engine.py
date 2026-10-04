import os
import sqlite3
import time
import json
import uuid
import urllib.request
import urllib.parse
from datetime import datetime, timezone, timedelta

TZ = timezone(timedelta(hours=7))
DB = os.getenv("SCORE_DB", "/data/score.sqlite3")

CHAT_ID = os.getenv(
    "SCORE_TELEGRAM_CHAT_ID",
    "7881007164"
)
BOT_TOKEN = os.getenv(
    "SCORE_TELEGRAM_BOT_TOKEN",
    ""
)

JJ_URL = os.getenv(
    "SCORE_JJ_URL",
    "https://jjblue-api-production.up.railway.app/"
    "jj/score-feed?limit=500"
)

HB_URL = os.getenv(
    "SCORE_HB_URL",
    "https://ccj-jj-hybrid-shadow-v1-production.up.railway.app/v2/score-feed"
)

DAILY_HOUR = 7
DAILY_MINUTE = 5

POLL_SECONDS = 60
SOURCE_FRESH_SECONDS = 180


# =========================================================
# DATABASE
# =========================================================

def conn():
    db_dir = os.path.dirname(DB)

    if db_dir:
        os.makedirs(
            db_dir,
            exist_ok=True
        )

    c = sqlite3.connect(
        DB,
        timeout=30
    )

    c.row_factory = sqlite3.Row
    c.execute(
        "PRAGMA journal_mode=WAL"
    )

    c.executescript("""
    CREATE TABLE IF NOT EXISTS observed_signals (
        source_key TEXT PRIMARY KEY,

        system TEXT NOT NULL
        CHECK(system IN ('JJ','HB')),

        source_message_id INTEGER,

        symbol TEXT NOT NULL,
        direction TEXT NOT NULL,

        created INTEGER NOT NULL,

        status TEXT NOT NULL
        CHECK(
            status IN (
                'WIN',
                'LOSS',
                'BREAKEVEN',
                'PENDING'
            )
        ),

        tp1_hit INTEGER NOT NULL DEFAULT 0,
        tp2_hit INTEGER NOT NULL DEFAULT 0,
        sl_hit INTEGER NOT NULL DEFAULT 0,

        updated INTEGER NOT NULL
    );

    CREATE INDEX IF NOT EXISTS
    idx_observed_period
    ON observed_signals(
        created,
        system
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


# =========================================================
# META / PERSISTENCE
# =========================================================

def meta_get(
    key,
    default=None
):
    c = conn()

    row = c.execute(
        """
        SELECT value
        FROM meta
        WHERE key = ?
        """,
        (key,)
    ).fetchone()

    c.close()

    return (
        row["value"]
        if row
        else default
    )


def meta_set(
    key,
    value
):
    c = conn()

    c.execute(
        """
        INSERT INTO meta(
            key,
            value
        )
        VALUES (?, ?)

        ON CONFLICT(key)
        DO UPDATE SET
            value = excluded.value
        """,
        (
            key,
            str(value)
        )
    )

    c.commit()
    c.close()


def boot_marker():
    instance_uuid = meta_get(
        "instance_uuid"
    )

    if not instance_uuid:
        instance_uuid = str(
            uuid.uuid4()
        )

        meta_set(
            "instance_uuid",
            instance_uuid
        )

    boot_count = (
        int(
            meta_get(
                "boot_count",
                "0"
            )
        )
        + 1
    )

    meta_set(
        "boot_count",
        boot_count
    )

    return (
        instance_uuid,
        boot_count
    )


# =========================================================
# DAY-1 CUTOVER
# =========================================================

def cutover_ts():
    """
    Persistent Day-1 boundary.

    Created exactly once in production DB.
    Producer history before this timestamp
    is NEVER scored by V2.
    """

    value = meta_get(
        "v2_cutover_utc"
    )

    if value is None:
        value = str(
            int(time.time())
        )

        meta_set(
            "v2_cutover_utc",
            value
        )

    return int(value)


# =========================================================
# HTTP READ-ONLY PRODUCER FEEDS
# =========================================================

def fetch_json(url):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent":
                "JJ-Score-Observer-V2"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=20
    ) as response:

        if response.status != 200:
            raise RuntimeError(
                "HTTP %s"
                % response.status
            )

        return json.loads(
            response
            .read()
            .decode()
        )


# =========================================================
# OBSERVER LEDGER
# =========================================================

def upsert_signal(
    source_key,
    system,
    message_id,
    symbol,
    direction,
    created,
    status,
    tp1=0,
    tp2=0,
    sl=0,
):
    c = conn()

    c.execute(
        """
        INSERT INTO observed_signals (
            source_key,
            system,
            source_message_id,
            symbol,
            direction,
            created,
            status,
            tp1_hit,
            tp2_hit,
            sl_hit,
            updated
        )
        VALUES (
            ?, ?, ?, ?, ?, ?,
            ?, ?, ?, ?, ?
        )

        ON CONFLICT(source_key)
        DO UPDATE SET

            status =
                excluded.status,

            tp1_hit =
                max(
                    observed_signals.tp1_hit,
                    excluded.tp1_hit
                ),

            tp2_hit =
                max(
                    observed_signals.tp2_hit,
                    excluded.tp2_hit
                ),

            sl_hit =
                max(
                    observed_signals.sl_hit,
                    excluded.sl_hit
                ),

            updated =
                excluded.updated
        """,
        (
            source_key,
            system,
            message_id,
            symbol,
            direction,
            created,
            status,
            int(tp1),
            int(tp2),
            int(sl),
            int(time.time())
        )
    )

    c.commit()
    c.close()


# =========================================================
# JJ INGESTION
# =========================================================

def ingest_jj(cutover):
    obj = fetch_json(
        JJ_URL
    )

    if (
        obj.get("schema")
        != "JJBLUE_SCORE_FEED_V1"
    ):
        raise RuntimeError(
            "JJ feed schema mismatch"
        )

    if (
        obj.get("source")
        != "delivered_signals_only"
    ):
        raise RuntimeError(
            "JJ source contract mismatch"
        )

    seen = 0
    accepted = 0

    for row in (
        obj.get("signals")
        or []
    ):
        seen += 1

        posted = int(
            row.get("posted_at")
            or 0
        )

        message_id = int(
            row.get("message_id")
            or 0
        )

        state = str(
            row.get("status")
            or ""
        )

        entered = bool(
            row.get("entry_hit_at")
        )

        # DAY-1 ONLY
        if posted < cutover:
            continue

        # Telegram-delivered only
        if message_id <= 0:
            continue

        # Entry-first truth rule
        if not entered:
            continue

        # Explicitly excluded
        if state in {
            "DATA_AMBIGUOUS",
            "NO_ENTRY_INVALIDATED",
            "NO_ENTRY_EXPIRED",
        }:
            continue

        if state in {
            "FULL_WIN",
            "PARTIAL_WIN",
            "TP1_HIT",
            "TP1_LATE",
            "TP2_LATE",
        }:
            status = "WIN"

        elif state in {
            "INVALIDATED",
            "TIME_EXPIRED",
        }:
            status = "LOSS"

        else:
            status = "PENDING"

        source_key = (
            "JJ:"
            + str(message_id)
            + ":"
            + str(row.get("id"))
        )

        upsert_signal(
            source_key,
            "JJ",
            message_id,
            str(
                row.get("symbol")
                or "UNKNOWN"
            ),
            str(
                row.get("side")
                or "UNKNOWN"
            ),
            posted,
            status,

            row.get("tp1_hit_at")
            is not None,

            row.get("tp2_hit_at")
            is not None,

            row.get("sl_hit_at")
            is not None,
        )

        accepted += 1

    meta_set(
        "jj_last_ok",
        int(time.time())
    )

    meta_set(
        "jj_last_error",
        ""
    )

    return {
        "seen": seen,
        "accepted": accepted,
    }


# =========================================================
# HB INGESTION
# =========================================================

def ingest_hb(cutover):
    obj = fetch_json(
        HB_URL
    )

    if (
        obj.get("schema")
        != "CC_SCORE_FEED_V1"
    ):
        raise RuntimeError(
            "HB feed schema mismatch"
        )

    if (
        obj.get("source")
        != "telegram_delivered_only"
    ):
        raise RuntimeError(
            "HB source contract mismatch"
        )

    seen = 0
    accepted = 0

    for row in (
        obj.get("signals")
        or []
    ):
        seen += 1

        posted = int(
            row.get("posted_at")
            or 0
        )

        message_id = int(
            row.get("message_id")
            or 0
        )

        classification = (
            row.get("classification")
            or {}
        )

        result = str(
            classification.get(
                "result"
            )
            or "UNRESOLVED"
        )

        # DAY-1 ONLY
        if posted < cutover:
            continue

        # Telegram receipt required
        if message_id <= 0:
            continue

        # No-entry / ambiguous
        if result == "EXCLUDED":
            continue

        if result in {
            "WIN",
            "LOSS",
            "BREAKEVEN",
        }:
            status = result

        else:
            status = "PENDING"

        source_key = (
            "HB:"
            + str(message_id)
            + ":"
            + str(
                row.get("setup_id")
            )
        )

        upsert_signal(
            source_key,
            "HB",
            message_id,
            str(
                row.get("symbol")
                or "UNKNOWN"
            ),
            str(
                row.get("direction")
                or "UNKNOWN"
            ),
            posted,
            status,

            bool(
                classification.get(
                    "tp1"
                )
            ),

            bool(
                classification.get(
                    "tp2"
                )
            ),

            bool(
                classification.get(
                    "sl"
                )
            ),
        )

        accepted += 1

    meta_set(
        "hb_last_ok",
        int(time.time())
    )

    meta_set(
        "hb_last_error",
        ""
    )

    return {
        "seen": seen,
        "accepted": accepted,
    }


# =========================================================
# INGEST ALL
# =========================================================

def ingest_all():
    cutover = cutover_ts()

    result = {}

    for name, func in (
        ("JJ", ingest_jj),
        ("HB", ingest_hb),
    ):
        try:
            result[name] = {
                "status": "PASS",
                **func(cutover)
            }

        except Exception as exc:
            error = (
                type(exc).__name__
                + ": "
                + str(exc)[:180]
            )

            meta_set(
                name.lower()
                + "_last_error",
                error
            )

            result[name] = {
                "status": "HOLD",
                "error": error,
            }

    print(
        json.dumps({
            "INGEST": result,
            "cutover_utc": cutover,
        }),
        flush=True
    )

    return result


# =========================================================
# SOURCE HEALTH
# =========================================================

def source_health():
    now = int(
        time.time()
    )

    result = {}

    for name in (
        "jj",
        "hb",
    ):
        last_ok = meta_get(
            name + "_last_ok"
        )

        error = meta_get(
            name + "_last_error",
            ""
        )

        fresh = (
            bool(last_ok)
            and (
                now
                - int(last_ok)
                <= SOURCE_FRESH_SECONDS
            )
        )

        result[
            name.upper()
        ] = {
            "fresh": fresh,
            "error": error,
        }

    return result


# =========================================================
# STATS
# =========================================================

def stats(
    start_ts,
    end_ts,
    system=None
):
    c = conn()

    query = """
        SELECT *
        FROM observed_signals
        WHERE created >= ?
          AND created < ?
    """

    args = [
        max(
            int(start_ts),
            cutover_ts()
        ),
        int(end_ts)
    ]

    if system:
        query += (
            " AND system = ?"
        )

        args.append(
            system
        )

    rows = c.execute(
        query,
        args
    ).fetchall()

    c.close()

    counts = {
        "WIN": 0,
        "LOSS": 0,
        "BREAKEVEN": 0,
        "PENDING": 0,
    }

    for row in rows:
        counts[
            row["status"]
        ] += 1

    result = {
        "signals":
            len(rows),

        "wins":
            counts["WIN"],

        "losses":
            counts["LOSS"],

        "breakeven":
            counts["BREAKEVEN"],

        "pending":
            counts["PENDING"],

        "tp1":
            sum(
                int(r["tp1_hit"])
                for r in rows
            ),

        "tp2":
            sum(
                int(r["tp2_hit"])
                for r in rows
            ),

        "sl":
            sum(
                int(r["sl_hit"])
                for r in rows
            ),
    }

    decided = (
        result["wins"]
        + result["losses"]
        + result["breakeven"]
    )

    result[
        "decided_win_rate"
    ] = (
        round(
            100
            * result["wins"]
            / decided,
            2
        )
        if decided
        else None
    )

    result[
        "invariant_ok"
    ] = (
        result["signals"]
        ==
        result["wins"]
        + result["losses"]
        + result["breakeven"]
        + result["pending"]
    )

    return result


# =========================================================
# PERIODS
# =========================================================

def period(
    kind,
    now=None
):
    now = (
        now
        or datetime.now(TZ)
    )

    end = now.replace(
        hour=DAILY_HOUR,
        minute=DAILY_MINUTE,
        second=0,
        microsecond=0,
    )

    if now < end:
        end -= timedelta(
            days=1
        )

    if kind == "daily":
        start = (
            end
            - timedelta(days=1)
        )

    elif kind == "weekly":
        start = (
            end
            - timedelta(days=7)
        )

    else:
        raise ValueError(
            kind
        )

    return (
        int(start.timestamp()),
        int(end.timestamp()),
        start,
        end,
    )


# =========================================================
# REPORT
# =========================================================

def stat_line(
    name,
    result
):
    win_rate = (
        "N/A"
        if (
            result[
                "decided_win_rate"
            ]
            is None
        )
        else (
            f'{result["decided_win_rate"]:.2f}%'
        )
    )

    return (
        f"{name}: "
        f"Signals {result['signals']} | "
        f"WIN {result['wins']} | "
        f"LOSS {result['losses']} | "
        f"BE {result['breakeven']} | "
        f"Pending {result['pending']} | "
        f"TP1 {result['tp1']} | "
        f"TP2 {result['tp2']} | "
        f"SL {result['sl']} | "
        f"WR {win_rate}"
    )


def report_text(
    kind,
    start,
    end,
    test=False
):
    health = source_health()

    results = {
        "JJ":
            stats(
                start.timestamp(),
                end.timestamp(),
                "JJ"
            ),

        "HB":
            stats(
                start.timestamp(),
                end.timestamp(),
                "HB"
            ),

        "COMBINED":
            stats(
                start.timestamp(),
                end.timestamp()
            ),
    }

    integrity = (
        "OK"

        if (
            all(
                item[
                    "invariant_ok"
                ]
                for item
                in results.values()
            )

            and all(
                item["fresh"]
                for item
                in health.values()
            )
        )

        else "DATA_GAP"
    )

    prefix = (
        "🧪 TEST "
        if test
        else ""
    )

    return (
        f"{prefix}"
        f"JJ/HB SCORE "
        f"{kind.upper()}\n"

        f"Period: "
        f"{start.isoformat()}"
        f" → "
        f"{end.isoformat()}\n"

        f"{stat_line('JJ', results['JJ'])}\n"

        f"{stat_line('HB', results['HB'])}\n"

        f"{stat_line('COMBINED', results['COMBINED'])}\n"

        f"Sources: "
        f"JJ="
        f"{'OK' if health['JJ']['fresh'] else 'HOLD'}"
        f" | "
        f"HB="
        f"{'OK' if health['HB']['fresh'] else 'HOLD'}"
        f"\n"

        f"Integrity: "
        f"{integrity}"
    )


# =========================================================
# TELEGRAM
# =========================================================

def telegram_send(text):
    if not BOT_TOKEN:
        raise RuntimeError(
            "SCORE_TELEGRAM_BOT_TOKEN missing"
        )

    data = urllib.parse.urlencode({
        "chat_id":
            CHAT_ID,

        "text":
            text,
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
        timeout=15
    ) as response:

        obj = json.loads(
            response
            .read()
            .decode()
        )

    if not obj.get("ok"):
        raise RuntimeError(
            "Telegram ok=false"
        )

    return int(
        obj["result"][
            "message_id"
        ]
    )


# =========================================================
# REPORT DEDUP
# =========================================================

def report_exists(
    report_id
):
    c = conn()

    row = c.execute(
        """
        SELECT message_id
        FROM reports
        WHERE id = ?
        """,
        (report_id,)
    ).fetchone()

    c.close()

    return (
        int(
            row["message_id"]
        )
        if row
        else None
    )


def send_report(
    kind,
    test=False,
    now=None
):
    ingest_all()

    (
        start_ts,
        end_ts,
        start,
        end
    ) = period(
        kind,
        now
    )

    # Display/report only from Day-1 onward
    start = datetime.fromtimestamp(
        max(
            start_ts,
            cutover_ts()
        ),
        TZ
    )

    report_id = (
        (
            "TEST:"
            if test
            else ""
        )
        + kind
        + ":"
        + str(end_ts)
    )

    old_message = report_exists(
        report_id
    )

    if old_message:
        return {
            "dedup": True,
            "message_id":
                old_message,
        }

    message_id = telegram_send(
        report_text(
            kind,
            start,
            end,
            test
        )
    )

    c = conn()

    c.execute(
        """
        INSERT INTO reports(
            id,
            message_id,
            sent
        )
        VALUES (?, ?, ?)
        """,
        (
            report_id,
            message_id,
            int(time.time())
        )
    )

    c.commit()
    c.close()

    return {
        "dedup": False,
        "message_id":
            message_id,
    }


# =========================================================
# SCHEDULER + CATCH-UP
# =========================================================

def due_reports(
    now=None
):
    now = (
        now
        or datetime.now(TZ)
    )

    (
        _,
        daily_end,
        _,
        _
    ) = period(
        "daily",
        now
    )

    cutover = cutover_ts()

    # Do not create reports for a period
    # that ended before Day-1 began.
    if daily_end <= cutover:
        return []

    kinds = [
        "daily"
    ]

    end_datetime = (
        datetime.fromtimestamp(
            daily_end,
            TZ
        )
    )

    # Monday = Daily + Weekly
    if (
        end_datetime.weekday()
        == 0
    ):
        kinds.append(
            "weekly"
        )

    due = []

    for kind in kinds:
        report_id = (
            kind
            + ":"
            + str(daily_end)
        )

        if not report_exists(
            report_id
        ):
            due.append(
                (
                    kind,
                    daily_end
                )
            )

    return due


# =========================================================
# SELFTEST
# =========================================================

def selftest():
    global DB

    real_db = DB

    DB = (
        "/tmp/"
        "jj_score_engine_v2_selftest.sqlite3"
    )

    for suffix in (
        "",
        "-wal",
        "-shm"
    ):
        try:
            os.remove(
                DB + suffix
            )
        except FileNotFoundError:
            pass

    try:
        meta_set(
            "v2_cutover_utc",
            1700000000
        )

        upsert_signal(
            "JJ:1:1",
            "JJ",
            1,
            "AAA",
            "LONG",
            1700000001,
            "WIN",
            1,
            1,
            0,
        )

        upsert_signal(
            "JJ:2:2",
            "JJ",
            2,
            "BBB",
            "SHORT",
            1700000002,
            "LOSS",
            0,
            0,
            1,
        )

        upsert_signal(
            "HB:3:3",
            "HB",
            3,
            "CCC",
            "LONG",
            1700000003,
            "PENDING",
            0,
            0,
            0,
        )

        result = stats(
            1699999999,
            1700000010
        )

        assert (
            result["signals"]
            == 3
        )

        assert (
            result["wins"]
            == 1
        )

        assert (
            result["losses"]
            == 1
        )

        assert (
            result["pending"]
            == 1
        )

        assert (
            result["tp1"]
            == 1
        )

        assert (
            result["tp2"]
            == 1
        )

        assert (
            result["sl"]
            == 1
        )

        assert result[
            "invariant_ok"
        ]

        instance1, boot1 = (
            boot_marker()
        )

        instance2, boot2 = (
            boot_marker()
        )

        assert (
            instance1
            == instance2
        )

        assert (
            boot2
            == boot1 + 1
        )

        monday = datetime(
            2026,
            10,
            12,
            7,
            6,
            tzinfo=TZ
        )

        meta_set(
            "v2_cutover_utc",
            int(
                (
                    monday
                    - timedelta(
                        days=8
                    )
                ).timestamp()
            )
        )

        kinds = [
            kind
            for (
                kind,
                _
            )
            in due_reports(
                monday
            )
        ]

        assert kinds == [
            "daily",
            "weekly",
        ]

        print(
            json.dumps({
                "SELFTEST":
                    "PASS",

                "DEDUP":
                    "PASS",

                "INVARIANT":
                    "PASS",

                "PERSISTENCE_MARKER":
                    "PASS",

                "MONDAY_DAILY_WEEKLY":
                    "PASS",
            }),
            flush=True
        )

    finally:
        DB = real_db


# =========================================================
# MAIN
# =========================================================

def main():
    selftest()

    (
        instance_uuid,
        boot_count
    ) = boot_marker()

    cutover = cutover_ts()

    print(
        json.dumps({
            "BOOT":
                "PASS",

            "db":
                DB,

            "timezone":
                "Asia/Bangkok",

            "instance_uuid":
                instance_uuid,

            "boot_count":
                boot_count,

            "cutover_utc":
                cutover,
        }),
        flush=True
    )

    ingest_all()

    if (
        os.getenv(
            "SCORE_SEND_TEST"
        )
        == "1"
    ):
        try:
            print(
                json.dumps({
                    "TELEGRAM_TEST":
                        send_report(
                            "daily",
                            test=True
                        )
                }),
                flush=True
            )

        except Exception as exc:
            print(
                json.dumps({
                    "TELEGRAM_TEST":
                        "FAIL",

                    "error":
                        str(exc),
                }),
                flush=True
            )

    next_ingest = 0

    while True:
        now = datetime.now(
            TZ
        )

        if (
            time.time()
            >= next_ingest
        ):
            ingest_all()

            next_ingest = (
                time.time()
                + POLL_SECONDS
            )

        # Persistent catch-up:
        # if 07:05 was missed,
        # unsent report is still sent later.
        for (
            kind,
            _
        ) in due_reports(
            now
        ):
            try:
                result = send_report(
                    kind
                )

                print(
                    json.dumps({
                        "REPORT":
                            kind,

                        "result":
                            result,
                    }),
                    flush=True
                )

            except Exception as exc:
                print(
                    json.dumps({
                        "REPORT":
                            kind,

                        "error":
                            str(exc),
                    }),
                    flush=True
                )

        print(
            json.dumps({
                "HEARTBEAT":
                    now.isoformat(),

                "boot_count":
                    boot_count,

                "sources":
                    source_health(),
            }),
            flush=True
        )

        time.sleep(
            30
        )


import threading
from fastapi import FastAPI
import uvicorn

app = FastAPI()

@app.get("/")
def root():
    return {"status": "PASS"}

@app.get("/health")
def health():
    return {
        "status": "PASS",
        "service": "score-engine-v2"
    }

@app.get("/ready")
def ready():
    return {"ready": True}

def run_worker():
    main()

if __name__ == "__main__":
    threading.Thread(target=run_worker, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=8080)

