import os
import uuid
from datetime import datetime, date, timedelta
from typing import Optional, List

import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Error: DATABASE_URL 환경변수가 설정되어 있지 않습니다.")


# ---------------------------
# DB Helpers
# ---------------------------
def get_db():
    conn = psycopg2.connect(
        DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor
    )
    # ✅ UUID 어댑터 등록 (can't adapt type 'UUID' 방지)
    psycopg2.extras.register_uuid(conn_or_curs=conn)
    return conn


def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def date_range(d1: date, d2: date):
    cur = d1
    step = timedelta(days=1)
    while cur <= d2:
        yield cur
        cur += step


def split_excluded(excluded) -> List[str]:
    if not excluded:
        return []
    if isinstance(excluded, list):
        return sorted(set([str(x).strip() for x in excluded if str(x).strip()]))
    items = [x.strip() for x in str(excluded).split(",") if x.strip()]
    return sorted(set(items))


def safe_str(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def init_db():
    conn = get_db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            event_date DATE,
            business TEXT,
            course TEXT,
            time TEXT,
            people TEXT,
            place TEXT,
            admin TEXT,
            group_id UUID,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )

    # ✅ 기존 테이블(구버전) 대비 컬럼 보정
    cur.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS event_date DATE;')
    cur.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS business TEXT;')
    cur.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS course TEXT;')
    cur.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS time TEXT;')
    cur.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS people TEXT;')
    cur.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS place TEXT;')
    cur.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS admin TEXT;')
    cur.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS group_id UUID;')
    cur.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();')
    cur.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();')

    # (선택) 구버전 start 컬럼에서 event_date 채우기
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name='events'
        """
    )
    cols = {r["column_name"] for r in cur.fetchall()}

    if "start" in cols:
        try:
            cur.execute(
                """
                UPDATE events
                SET event_date = CAST(start AS DATE)
                WHERE event_date IS NULL AND start IS NOT NULL AND start <> '';
                """
            )
        except Exception:
            conn.rollback()
            cur = conn.cursor()

    # 인덱스
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_event_date ON events(event_date);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_group_id ON events(group_id);")

    conn.commit()
    conn.close()


init_db()


# ---------------------------
# Error handler (항상 JSON)
# ---------------------------
@app.errorhandler(Exception)
def handle_exception(e):
    return jsonify({"ok": False, "error": str(e)}), 500


# ---------------------------
# API
# ---------------------------
@app.get("/api/events")
def api_get_events():
    start = request.args.get("start")
    end = request.args.get("end")

    conn = get_db()
    cur = conn.cursor()

    if start and end:
        s = parse_ymd(start)
        e = parse_ymd(end)
        cur.execute(
            """
            SELECT id, event_date, business, course, time, people, place, admin, group_id
            FROM events
            WHERE event_date BETWEEN %s AND %s
            ORDER BY event_date ASC, id ASC
            """,
            (s, e),
        )
    else:
        cur.execute(
            """
            SELECT id, event_date, business, course, time, people, place, admin, group_id
            FROM events
            ORDER BY event_date ASC, id ASC
            """
        )

    rows = cur.fetchall()
    conn.close()

    items = []
    for r in rows:
        if not r.get("event_date"):
            continue
        items.append(
            {
                "id": r["id"],
                "date": r["event_date"].strftime("%Y-%m-%d"),
                "business": r.get("business") or "",
                "course": r.get("course") or "",
                "time": r.get("time") or "",
                "people": r.get("people") or "",
                "place": r.get("place") or "",
                "admin": r.get("admin") or "",
                "group_id": str(r["group_id"]) if r.get("group_id") else None,
            }
        )

    return jsonify({"ok": True, "events": items})


@app.post("/api/events")
def api_create_events():
    data = request.get_json(silent=True) or {}

    start = safe_str(data.get("start"))
    end = safe_str(data.get("end"))
    if not start or not end:
        return jsonify({"ok": False, "error": "시작/종료일은 YYYY-MM-DD 형식으로 입력하세요."}), 400

    try:
        s = parse_ymd(start)
        e = parse_ymd(end)
    except Exception:
        return jsonify({"ok": False, "error": "시작/종료일은 YYYY-MM-DD 형식으로 입력하세요."}), 400

    if e < s:
        return jsonify({"ok": False, "error": "종료일은 시작일보다 빠를 수 없습니다."}), 400

    excluded_list = split_excluded(data.get("excluded_dates"))

    business = safe_str(data.get("business"))
    course = safe_str(data.get("course"))
    time = safe_str(data.get("time"))
    people = safe_str(data.get("people"))
    place = safe_str(data.get("place"))
    admin = safe_str(data.get("admin"))

    # ✅ UUID 객체 대신 문자열로 저장(가장 확실)
    group_id = str(uuid.uuid4())

    conn = get_db()
    cur = conn.cursor()

    created_ids = []
    for d in date_range(s, e):
        ds = d.strftime("%Y-%m-%d")
        if ds in excluded_list:
            continue
        cur.execute(
            """
            INSERT INTO events(event_date, business, course, time, people, place, admin, group_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s::uuid)
            RETURNING id
            """,
            (d, business, course, time, people, place, admin, group_id),
        )
        created_ids.append(cur.fetchone()["id"])

    conn.commit()
    conn.close()

    return jsonify({"ok": True, "created_ids": created_ids, "group_id": group_id}), 201


@app.put("/api/events/<int:event_id>")
def api_update_event(event_id: int):
    data = request.get_json(silent=True) or {}

    new_date = safe_str(data.get("date"))
    d = None
    if new_date:
        try:
            d = parse_ymd(new_date)
        except Exception:
            return jsonify({"ok": False, "error": "date는 YYYY-MM-DD 형식이어야 합니다."}), 400

    fields = {
        "business": safe_str(data.get("business")),
        "course": safe_str(data.get("course")),
        "time": safe_str(data.get("time")),
        "people": safe_str(data.get("people")),
        "place": safe_str(data.get("place")),
        "admin": safe_str(data.get("admin")),
    }

    sets = []
    vals = []
    if d is not None:
        sets.append("event_date = %s")
        vals.append(d)

    for k, v in fields.items():
        sets.append(f"{k} = %s")
        vals.append(v)

    sets.append("updated_at = NOW()")
    vals.append(event_id)

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        f"""
        UPDATE events
        SET {", ".join(sets)}
        WHERE id = %s
        RETURNING id, event_date, business, course, time, people, place, admin, group_id
        """,
        tuple(vals),
    )
    row = cur.fetchone()
    conn.commit()
    conn.close()

    if not row:
        return jsonify({"ok": False, "error": "해당 이벤트를 찾을 수 없습니다."}), 404

    return jsonify(
        {
            "ok": True,
            "event": {
                "id": row["id"],
                "date": row["event_date"].strftime("%Y-%m-%d"),
                "business": row.get("business") or "",
                "course": row.get("course") or "",
                "time": row.get("time") or "",
                "people": row.get("people") or "",
                "place": row.get("place") or "",
                "admin": row.get("admin") or "",
                "group_id": str(row["group_id"]) if row.get("group_id") else None,
            },
        }
    )


@app.delete("/api/events/<int:event_id>")
def api_delete_event(event_id: int):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM events WHERE id=%s RETURNING id", (event_id,))
    row = cur.fetchone()
    conn.commit()
    conn.close()

    if not row:
        return jsonify({"ok": False, "error": "해당 이벤트를 찾을 수 없습니다."}), 404
    return jsonify({"ok": True})


@app.get("/api/businesses")
def api_businesses():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT business
        FROM events
        WHERE business IS NOT NULL AND business <> ''
        ORDER BY business ASC
        """
    )
    rows = cur.fetchall()
    conn.close()
    items = [r["business"] for r in rows if r.get("business")]
    return jsonify({"ok": True, "businesses": items})


# ---------------------------
# Frontend (Single-file HTML)
# ---------------------------
def _html() -> str:
    # (너가 쓰던 HTML/JS 그대로 유지하면 됨)
    return r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>포항산학 월별일정</title>
  <!-- ⚠️ 여기에는 너가 쓰던 통짜 HTML/JS를 그대로 넣어야 함 -->
  <style>body{font-family:system-ui}</style>
</head>
<body>
  HTML omitted. (여기엔 너가 쓰던 통짜 HTML을 그대로 유지해야 함.)
</body>
</html>"""


@app.get("/")
def index():
    return Response(_html(), mimetype="text/html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)
