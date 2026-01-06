import os
import re
import uuid
import logging
from datetime import date, datetime, timedelta

import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, Response

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    # Render 배포 시 Environment Variables에 DATABASE_URL 반드시 설정해야 함
    raise RuntimeError("Error: DATABASE_URL 환경변수가 설정되어 있지 않습니다.")


# =========================
# DB
# =========================
def get_db():
    # Render Postgres는 보통 ssl 요구. url에 포함돼도 되지만 안전하게 sslmode=require 적용
    # (URL에 ?sslmode=... 가 이미 있으면 psycopg2가 알아서 처리)
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    """
    - 기존에 SQLite 쓰다가 Postgres로 넘어오면서 스키마가 꼬였을 수 있음
    - 이미 테이블이 있더라도 필요한 컬럼을 ALTER로 보강
    - end는 예약어라 항상 "end"로 쿼리에서 인용
    """
    conn = get_db()
    cur = conn.cursor()
    # 1) 테이블 생성(없을 때만)
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            "start" TEXT NOT NULL,
            "end"   TEXT NOT NULL,
            event_date DATE,
            group_id TEXT,
            business TEXT,
            course TEXT,
            time TEXT,
            people TEXT,
            place TEXT,
            admin TEXT,
            excluded_dates TEXT,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )

    # 2) 컬럼 보강(있으면 스킵)
    cur.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS "start" TEXT;')
    cur.execute('ALTER TABLE events ADD COLUMN IF NOT EXISTS "end" TEXT;')
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS event_date DATE;")
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS group_id TEXT;")
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS business TEXT;")
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS course TEXT;")
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS time TEXT;")
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS people TEXT;")
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS place TEXT;")
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS admin TEXT;")
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS excluded_dates TEXT;")
    cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ;")

    # 3) NOT NULL 보정(기존 데이터가 NULL일 수 있으니 먼저 채우고 제약 강화)
    # start/end가 NULL인 경우 event_date를 기준으로 채우기 시도
    cur.execute(
        """
        UPDATE events
        SET "start" = COALESCE("start", TO_CHAR(event_date, 'YYYY-MM-DD')),
            "end"   = COALESCE("end",   TO_CHAR(event_date, 'YYYY-MM-DD'))
        WHERE ("start" IS NULL OR "end" IS NULL) AND event_date IS NOT NULL;
        """
    )
    # event_date가 NULL인데 start가 있는 경우 start로 채우기
    cur.execute(
        """
        UPDATE events
        SET event_date = COALESCE(event_date, TO_DATE("start",'YYYY-MM-DD'))
        WHERE event_date IS NULL AND "start" ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$';
        """
    )
    # 마지막으로 start/end가 NULL이면 오늘 날짜로라도 채움(저장 실패 방지)
    cur.execute(
        """
        UPDATE events
        SET "start" = COALESCE("start", TO_CHAR(NOW()::date,'YYYY-MM-DD')),
            "end"   = COALESCE("end",   TO_CHAR(NOW()::date,'YYYY-MM-DD'))
        WHERE "start" IS NULL OR "end" IS NULL;
        """
    )

    # 4) 인덱스
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_event_date ON events(event_date);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_start ON events(\"start\");")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_group_id ON events(group_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_business ON events(business);")

    conn.commit()
    cur.close()
    conn.close()
    logging.info("DB init OK")


init_db()


# =========================
# Helpers
# =========================
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date_yyyy_mm_dd(s: str) -> date:
    if not s or not isinstance(s, str) or not DATE_RE.match(s.strip()):
        raise ValueError("YYYY-MM-DD 형식이 아닙니다.")
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def daterange(d1: date, d2: date):
    # inclusive range
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)


def normalize_excluded_dates(s: str) -> set[str]:
    """
    "2026-01-07,2026-01-08" 또는 "2026-01-07 2026-01-08" 등 입력 허용
    """
    if not s:
        return set()
    raw = re.split(r"[,\s]+", s.strip())
    out = set()
    for x in raw:
        x = x.strip()
        if not x:
            continue
        if DATE_RE.match(x):
            out.add(x)
    return out


def row_to_dict(r):
    # DictCursor row -> dict
    return {
        "id": r["id"],
        "start": r["start"],
        "end": r["end"],
        "event_date": r["event_date"].isoformat() if r["event_date"] else None,
        "group_id": r["group_id"],
        "business": r["business"],
        "course": r["course"],
        "time": r["time"],
        "people": r["people"],
        "place": r["place"],
        "admin": r["admin"],
        "excluded_dates": r["excluded_dates"],
    }


# =========================
# API
# =========================
@app.route("/api/events", methods=["GET"])
def api_get_events():
    business = request.args.get("business", "").strip()
    view = request.args.get("view", "").strip()  # optional
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    if business and business != "전체":
        cur.execute(
            """
            SELECT id, "start", "end", event_date, group_id, business, course, time, people, place, admin, excluded_dates
            FROM events
            WHERE business = %s
            ORDER BY event_date, id;
            """,
            (business,),
        )
    else:
        cur.execute(
            """
            SELECT id, "start", "end", event_date, group_id, business, course, time, people, place, admin, excluded_dates
            FROM events
            ORDER BY event_date, id;
            """
        )

    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([row_to_dict(r) for r in rows])


@app.route("/api/businesses", methods=["GET"])
def api_get_businesses():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT business
        FROM events
        WHERE business IS NOT NULL AND TRIM(business) <> ''
        ORDER BY business;
        """
    )
    items = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(items)


@app.route("/api/events", methods=["POST"])
def api_create_events():
    """
    기간 등록:
    - start/end(YYYY-MM-DD) 받아서 날짜별로 쪼개 저장
    - excluded_dates에 포함된 날짜는 스킵
    - group_id 부여(원하면 나중에 같은 그룹으로 조회 가능)
    """
    data = request.get_json(silent=True) or {}

    # 프론트에서 키가 다르게 올 수도 있으니 유연하게 받음
    start_s = (data.get("start") or data.get("start_date") or data.get("startDate") or "").strip()
    end_s = (data.get("end") or data.get("end_date") or data.get("endDate") or "").strip()

    try:
        sdt = parse_date_yyyy_mm_dd(start_s)
        edt = parse_date_yyyy_mm_dd(end_s)
    except Exception:
        return jsonify({"ok": False, "error": "시작/종료일은 YYYY-MM-DD 형식으로 입력하세요."}), 400

    if edt < sdt:
        return jsonify({"ok": False, "error": "종료일은 시작일보다 빠를 수 없습니다."}), 400

    business = (data.get("business") or "").strip()
    course = (data.get("course") or "").strip()
    time_s = (data.get("time") or "").strip()
    people = (data.get("people") or "").strip()
    place = (data.get("place") or "").strip()
    admin = (data.get("admin") or "").strip()
    excluded_raw = (data.get("excluded_dates") or data.get("excludedDates") or "").strip()
    excluded = normalize_excluded_dates(excluded_raw)

    group_id = str(data.get("group_id") or data.get("groupId") or uuid.uuid4())

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    created = []
    try:
        for d in daterange(sdt, edt):
            ds = d.isoformat()
            if ds in excluded:
                continue

            # per-day 저장: start/end는 날짜 문자열로 동일하게 저장
            cur.execute(
                """
                INSERT INTO events ("start", "end", event_date, group_id, business, course, time, people, place, admin, excluded_dates)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, "start", "end", event_date, group_id, business, course, time, people, place, admin, excluded_dates;
                """,
                (
                    ds,
                    ds,
                    d,
                    str(group_id),
                    business if business else None,
                    course if course else None,
                    time_s if time_s else None,
                    people if people else None,
                    place if place else None,
                    admin if admin else None,
                    excluded_raw if excluded_raw else None,
                ),
            )
            created.append(row_to_dict(cur.fetchone()))

        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.exception("POST /api/events failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        cur.close()
        conn.close()

    return jsonify({"ok": True, "created": created, "group_id": group_id})


@app.route("/api/events/<int:event_id>", methods=["PUT"])
def api_update_event(event_id: int):
    data = request.get_json(silent=True) or {}

    # 개별 수정: 날짜는 유지(원하면 수정도 가능하게 해둠)
    start_s = (data.get("start") or "").strip()
    end_s = (data.get("end") or "").strip()
    event_date_s = (data.get("event_date") or data.get("eventDate") or "").strip()

    business = (data.get("business") or "").strip()
    course = (data.get("course") or "").strip()
    time_s = (data.get("time") or "").strip()
    people = (data.get("people") or "").strip()
    place = (data.get("place") or "").strip()
    admin = (data.get("admin") or "").strip()

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    try:
        # 기존 값 읽기
        cur.execute(
            """
            SELECT id, "start", "end", event_date, group_id, business, course, time, people, place, admin, excluded_dates
            FROM events
            WHERE id = %s;
            """,
            (event_id,),
        )
        old = cur.fetchone()
        if not old:
            return jsonify({"ok": False, "error": "not found"}), 404

        # 날짜 파싱(주면 반영, 아니면 기존 유지)
        new_event_date = old["event_date"]
        new_start = old["start"]
        new_end = old["end"]

        if event_date_s:
            try:
                dd = parse_date_yyyy_mm_dd(event_date_s)
                new_event_date = dd
                new_start = dd.isoformat()
                new_end = dd.isoformat()
            except Exception:
                return jsonify({"ok": False, "error": "event_date는 YYYY-MM-DD 형식이어야 합니다."}), 400
        else:
            # start/end로 들어오는 경우도 처리
            if start_s:
                try:
                    dd = parse_date_yyyy_mm_dd(start_s)
                    new_start = dd.isoformat()
                    new_event_date = dd
                except Exception:
                    return jsonify({"ok": False, "error": "start는 YYYY-MM-DD 형식이어야 합니다."}), 400
            if end_s:
                try:
                    dd = parse_date_yyyy_mm_dd(end_s)
                    new_end = dd.isoformat()
                except Exception:
                    return jsonify({"ok": False, "error": "end는 YYYY-MM-DD 형식이어야 합니다."}), 400

        # 업데이트 (end는 예약어 => "end")
        cur.execute(
            """
            UPDATE events
            SET "start" = %s,
                "end" = %s,
                event_date = %s,
                business = %s,
                course = %s,
                time = %s,
                people = %s,
                place = %s,
                admin = %s
            WHERE id = %s
            RETURNING id, "start", "end", event_date, group_id, business, course, time, people, place, admin, excluded_dates;
            """,
            (
                new_start,
                new_end,
                new_event_date,
                business if business else None,
                course if course else None,
                time_s if time_s else None,
                people if people else None,
                place if place else None,
                admin if admin else None,
                event_id,
            ),
        )
        updated = row_to_dict(cur.fetchone())
        conn.commit()
        return jsonify({"ok": True, "event": updated})
    except Exception as e:
        conn.rollback()
        logging.exception("PUT /api/events/<id> failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/events/<int:event_id>", methods=["DELETE"])
def api_delete_event(event_id: int):
    conn = get_db()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM events WHERE id = %s;", (event_id,))
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        logging.exception("DELETE /api/events/<id> failed")
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        cur.close()
        conn.close()


# =========================
# UI (Single-page HTML)
# =========================
HTML_PAGE = r"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>포항산학 월별일정</title>
  <style>
    :root{
      --bg:#ffffff;
      --text:#111;
      --muted:#666;
      --line:#e7e7e7;
      --card:#f6f6f6;
      --btn:#f2f2f2;
      --btnText:#111;
      --shadow: 0 10px 30px rgba(0,0,0,.12);
      --radius:14px;
    }
    *{box-sizing:border-box}
    body{margin:0;background:var(--bg);color:var(--text);font-family:system-ui,-apple-system,Segoe UI,Roboto,Pretendard,Apple SD Gothic Neo,sans-serif}
    .wrap{max-width:1200px;margin:0 auto;padding:16px}
    h1{margin:10px 0 0;text-align:center;font-size:44px;letter-spacing:-1px}
    h2{margin:6px 0 10px;text-align:center;font-size:34px;font-weight:800;letter-spacing:-1px}
    .toolbar{
      display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:center;
      margin:14px 0 14px;
    }
    .toolbar .left, .toolbar .right{
      display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:center;
    }
    button, select, input{
      font:inherit;
      border:1px solid var(--line);
      background:var(--btn);
      color:var(--btnText);
      padding:10px 12px;
      border-radius:10px;
      outline:none;
    }
    button{cursor:pointer}
    button.primary{background:#fff;border:1px solid #cfcfcf}
    button.wide{min-width:160px}
    .filterRow{
      display:flex;gap:10px;flex-wrap:wrap;align-items:center;justify-content:center;
      margin-bottom:10px;
    }
    .addBtnRow{
      display:flex;justify-content:center;margin:10px 0 12px;
    }
    .addBtnRow button{
      width:min(860px, 100%);
      padding:14px 16px;
      font-size:20px;
      border-radius:12px;
      background:#fff;
    }

    /* Calendar */
    .calendar{
      border:1px solid var(--line);
      border-radius:12px;
      overflow:hidden;
      background:#fff;
    }
    .dow{
      display:grid;
      grid-template-columns:repeat(7,1fr);
      background:#fafafa;
      border-bottom:1px solid var(--line);
    }
    .dow div{
      text-align:center;padding:10px 6px;font-weight:800;border-right:1px solid var(--line);
    }
    .dow div:last-child{border-right:none}
    .dow .sun{color:#d63232}
    .dow .sat{color:#1c6bd6}

    .grid{
      display:grid;
      grid-template-columns:repeat(7,1fr);
    }
    .cell{
      min-height:140px;
      border-right:1px solid var(--line);
      border-bottom:1px solid var(--line);
      padding:8px;
      position:relative;
      background:#fff;
    }
    .cell:nth-child(7n){border-right:none}
    .dateNum{
      position:absolute;top:8px;left:8px;font-weight:800;color:#111;
    }
    .dateNum.sun{color:#d63232}
    .dateNum.sat{color:#1c6bd6}
    .events{
      margin-top:26px;
      display:grid;
      grid-template-columns: 1fr 1fr; /* PC 기본 2개 */
      gap:6px;
    }
    .eventCard{
      border-radius:12px;
      padding:10px 10px 9px;
      border:1px solid rgba(0,0,0,.08);
      background:linear-gradient(180deg, rgba(0,0,0,.02), rgba(0,0,0,.00));
      box-shadow:0 3px 10px rgba(0,0,0,.06);
      cursor:pointer;
      min-height:50px;
      display:flex;flex-direction:column;gap:6px;
    }
    .eventTitle{
      font-weight:900;
      font-size:16px;
      overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
    }
    .eventLines{font-size:12px;color:#222;line-height:1.25}
    .eventLines .line{display:block}
    .muted{color:var(--muted)}

    /* Week view */
    .week{
      border:1px solid var(--line);
      border-radius:12px;
      overflow:hidden;background:#fff;
    }
    .weekHead{
      display:grid;
      grid-template-columns:140px 1fr;
      background:#fafafa;border-bottom:1px solid var(--line);
    }
    .weekHead div{padding:10px;font-weight:900;border-right:1px solid var(--line)}
    .weekHead div:last-child{border-right:none}
    .weekRow{
      display:grid;
      grid-template-columns:140px 1fr;
      border-bottom:1px solid var(--line);
    }
    .weekRow:last-child{border-bottom:none}
    .weekDate{
      padding:10px;border-right:1px solid var(--line);font-weight:900;
    }
    .weekEvents{
      padding:10px;
      display:grid;
      grid-template-columns: repeat(2, minmax(0,1fr));
      gap:8px;
    }

    /* Modal */
    .backdrop{
      position:fixed;inset:0;background:rgba(0,0,0,.35);
      display:none;align-items:center;justify-content:center;
      padding:14px;
      z-index:50;
    }
    .modal{
      width:min(920px, 100%);
      background:#fff;
      border-radius:16px;
      box-shadow:var(--shadow);
      overflow:hidden;
    }
    .modalHeader{
      padding:14px 16px;
      font-weight:1000;
      font-size:20px;
      border-bottom:1px solid var(--line);
      display:flex;justify-content:space-between;align-items:center;
    }
    .modalBody{padding:14px 16px}
    .grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    label{display:block;font-size:12px;color:#333;margin:10px 0 6px;font-weight:800}
    input[type="text"], input[type="date"]{
      width:100%;
      background:#fff;
      border:1px solid var(--line);
      border-radius:10px;
      padding:12px 12px;
    }
    .modalActions{
      padding:12px 16px;
      border-top:1px solid var(--line);
      display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap;
    }
    .danger{background:#ffecec;border-color:#ffbcbc}
    .ok{background:#ecfff2;border-color:#b8f0c7}

    /* Mobile tuning */
    @media (max-width: 740px){
      h1{font-size:38px}
      h2{font-size:30px}
      .wrap{padding:12px}
      .cell{min-height:120px;padding:7px}
      .events{grid-template-columns: 1fr; gap:6px;} /* 세로는 1개가 더 읽기 좋음 */
      .eventTitle{font-size:15px}
      .eventLines{font-size:12px}
      .weekHead{grid-template-columns:110px 1fr}
      .weekRow{grid-template-columns:110px 1fr}
      .weekEvents{grid-template-columns: 1fr; }
      .grid2{grid-template-columns:1fr}
      .addBtnRow button{font-size:18px}
    }
    @media (max-width: 740px) and (orientation: landscape){
      .events{grid-template-columns: 1fr 1fr;}  /* 모바일 가로면 2개 */
      .weekEvents{grid-template-columns: 1fr 1fr;}
    }

    /* “가로모드(시각적)” : 강제 회전은 브라우저에서 제한이 있어,
       대신 가로 스크롤/넓은 레이아웃으로 보는 모드 제공 */
    .landscapeMode .wrap{max-width:1400px}
    .landscapeMode .calendar{overflow:auto}
    .landscapeMode .cell{min-height:150px}
    .landscapeMode .events{grid-template-columns: 1fr 1fr;}
  </style>
</head>
<body>
  <div id="root"></div>

<script>
(function(){
  const root = document.getElementById("root");

  const state = {
    today: new Date(),
    cursor: new Date(), // current displayed month/week 기준
    viewMode: "month",  // "month" | "week"
    businessFilter: "전체",
    businesses: [],
    events: [],
    landscape: false
  };

  const fmtDate = (d) => {
    const y = d.getFullYear();
    const m = String(d.getMonth()+1).padStart(2,"0");
    const dd = String(d.getDate()).padStart(2,"0");
    return `${y}-${m}-${dd}`;
  };

  const parseDate = (s) => {
    // YYYY-MM-DD
    const [y,m,d] = s.split("-").map(Number);
    return new Date(y, m-1, d);
  };

  const startOfWeek = (d) => {
    const x = new Date(d);
    const day = x.getDay(); // 0 sun
    x.setDate(x.getDate() - day);
    x.setHours(0,0,0,0);
    return x;
  };

  const addDays = (d, n) => {
    const x = new Date(d);
    x.setDate(x.getDate()+n);
    return x;
  };

  const escapeHtml = (s) => {
    if (s === null || s === undefined) return "";
    return String(s)
      .replaceAll("&","&amp;")
      .replaceAll("<","&lt;")
      .replaceAll(">","&gt;")
      .replaceAll('"',"&quot;")
      .replaceAll("'","&#039;");
  };

  const pickColor = (business) => {
    const base = (business || "기타").trim();
    let hash = 0;
    for(let i=0;i<base.length;i++){
      hash = (hash*31 + base.charCodeAt(i)) >>> 0;
    }
    const h = hash % 360;
    return `hsl(${h} 70% 88%)`;
  };

  const fetchJSON = async (url, opts={}) => {
    const r = await fetch(url, opts);
    const ct = (r.headers.get("content-type") || "");
    if(!ct.includes("application/json")){
      const t = await r.text();
      throw new Error("서버 응답 파싱 실패");
    }
    const j = await r.json();
    if(!r.ok){
      throw new Error(j && j.error ? j.error : "서버 오류");
    }
    return j;
  };

  const loadAll = async () => {
    const qs = state.businessFilter && state.businessFilter !== "전체"
      ? `?business=${encodeURIComponent(state.businessFilter)}`
      : "";
    const events = await fetchJSON(`/api/events${qs}`);
    state.events = events;

    const biz = await fetchJSON("/api/businesses");
    state.businesses = ["전체", ...biz];
    if(!state.businesses.includes(state.businessFilter)) state.businessFilter = "전체";
  };

  const groupByDate = () => {
    const map = new Map();
    for(const e of state.events){
      const key = e.event_date || e.start;
      if(!map.has(key)) map.set(key, []);
      map.get(key).push(e);
    }
    return map;
  };

  const render = () => {
    document.body.classList.toggle("landscapeMode", !!state.landscape);

    const y = state.cursor.getFullYear();
    const m = state.cursor.getMonth(); // 0-based
    const titleYM = `${y}년 ${m+1}월`;

    const toolbar = `
      <div class="wrap">
        <h1>포항산학 월별일정</h1>
        <h2>${escapeHtml(titleYM)}</h2>

        <div class="toolbar">
          <div class="left">
            <button id="prevBtn">◀ 이전</button>
            <button id="nextBtn">다음 ▶</button>
            <button id="monthBtn" class="primary">월별</button>
            <button id="weekBtn">주별</button>
            <button id="landBtn">${state.landscape ? "가로모드 해제" : "가로모드"}</button>
          </div>
        </div>

        <div class="filterRow">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:center;">
            <div style="font-weight:900;font-size:22px;">사업명:</div>
            <select id="bizSel"></select>
            <button id="resetFilter">필터 초기화</button>
          </div>
        </div>

        <div class="addBtnRow">
          <button id="openAdd" class="wide">+ 일정 추가하기</button>
        </div>

        <div id="viewHost"></div>
      </div>

      <div id="modalBackdrop" class="backdrop"></div>
    `;

    root.innerHTML = toolbar;

    // build business select
    const sel = document.getElementById("bizSel");
    sel.innerHTML = state.businesses.map(b => `<option value="${escapeHtml(b)}">${escapeHtml(b)}</option>`).join("");
    sel.value = state.businessFilter;

    document.getElementById("prevBtn").onclick = () => {
      if(state.viewMode === "month"){
        state.cursor = new Date(y, m-1, 1);
      } else {
        state.cursor = addDays(state.cursor, -7);
      }
      renderAndLoad();
    };
    document.getElementById("nextBtn").onclick = () => {
      if(state.viewMode === "month"){
        state.cursor = new Date(y, m+1, 1);
      } else {
        state.cursor = addDays(state.cursor, +7);
      }
      renderAndLoad();
    };

    document.getElementById("monthBtn").onclick = () => {
      state.viewMode = "month";
      render();
      renderCalendar();
    };
    document.getElementById("weekBtn").onclick = () => {
      state.viewMode = "week";
      render();
      renderWeek();
    };

    document.getElementById("landBtn").onclick = () => {
      state.landscape = !state.landscape;
      render();
      if(state.viewMode === "month") renderCalendar();
      else renderWeek();
    };

    sel.onchange = () => {
      state.businessFilter = sel.value;
      renderAndLoad();
    };

    document.getElementById("resetFilter").onclick = () => {
      state.businessFilter = "전체";
      renderAndLoad();
    };

    document.getElementById("openAdd").onclick = () => openAddModal();

    if(state.viewMode === "month") renderCalendar();
    else renderWeek();
  };

  const renderCalendar = () => {
    const host = document.getElementById("viewHost");
    const y = state.cursor.getFullYear();
    const m = state.cursor.getMonth();
    const first = new Date(y, m, 1);
    const startDay = first.getDay();
    const last = new Date(y, m+1, 0);
    const daysInMonth = last.getDate();

    const map = groupByDate();

    let cells = [];
    // leading blanks
    for(let i=0;i<startDay;i++) cells.push(null);
    for(let d=1; d<=daysInMonth; d++){
      cells.push(new Date(y, m, d));
    }
    // trailing blanks to full weeks
    while(cells.length % 7 !== 0) cells.push(null);

    const dow = `
      <div class="dow">
        <div class="sun">일</div><div>월</div><div>화</div><div>수</div><div>목</div><div>금</div><div class="sat">토</div>
      </div>
    `;

    const grid = cells.map((d) => {
      if(!d){
        return `<div class="cell"></div>`;
      }
      const ds = fmtDate(d);
      const list = (map.get(ds) || []).slice().sort((a,b)=> (a.id-b.id));
      const dayClass = d.getDay()===0 ? "sun" : (d.getDay()===6 ? "sat" : "");
      const cards = list.map(ev => eventCardHTML(ev)).join("");
      return `
        <div class="cell" data-date="${ds}">
          <div class="dateNum ${dayClass}">${d.getDate()}</div>
          <div class="events">${cards}</div>
        </div>
      `;
    }).join("");

    host.innerHTML = `
      <div class="calendar">
        ${dow}
        <div class="grid">${grid}</div>
      </div>
    `;

    // bind card clicks
    document.querySelectorAll("[data-ev-id]").forEach(el=>{
      el.addEventListener("click", ()=>{
        const id = Number(el.getAttribute("data-ev-id"));
        const ev = state.events.find(x=>x.id===id);
        if(ev) openEventModal(ev);
      });
    });
  };

  const renderWeek = () => {
    const host = document.getElementById("viewHost");
    const start = startOfWeek(state.cursor);
    const map = groupByDate();

    const rows = [];
    for(let i=0;i<7;i++){
      const d = addDays(start, i);
      const ds = fmtDate(d);
      const weekday = ["일","월","화","수","목","금","토"][d.getDay()];
      const list = (map.get(ds) || []).slice().sort((a,b)=> (a.id-b.id));
      const cards = list.map(ev => eventCardHTML(ev)).join("");
      rows.push(`
        <div class="weekRow">
          <div class="weekDate">${ds} (${weekday})</div>
          <div class="weekEvents">${cards || `<span class="muted">일정 없음</span>`}</div>
        </div>
      `);
    }

    host.innerHTML = `
      <div class="week">
        <div class="weekHead"><div>날짜</div><div>일정</div></div>
        ${rows.join("")}
      </div>
    `;

    document.querySelectorAll("[data-ev-id]").forEach(el=>{
      el.addEventListener("click", ()=>{
        const id = Number(el.getAttribute("data-ev-id"));
        const ev = state.events.find(x=>x.id===id);
        if(ev) openEventModal(ev);
      });
    });
  };

  const fieldLine = (label, value) => {
    const v = (value||"").trim();
    if(!v) return "";
    return `<span class="line">• ${escapeHtml(label)}: ${escapeHtml(v)}</span>`;
  };

  const eventCardHTML = (ev) => {
    const bg = pickColor(ev.business);
    // 카드에서는 너무 길면 가독성 떨어져서 라인 수 제한(상세는 모달에서)
    const lines = [
      fieldLine("과정", ev.course),
      fieldLine("시간", ev.time),
      fieldLine("인원", ev.people),
      fieldLine("장소", ev.place),
      fieldLine("행정", ev.admin),
    ].filter(Boolean);

    // 모바일은 줄이 길면 세로로 찢어지니 최대 2줄만 보이게
    const isMobile = window.matchMedia("(max-width: 740px)").matches;
    const showLines = isMobile ? lines.slice(0,2) : lines.slice(0,5);

    return `
      <div class="eventCard" data-ev-id="${ev.id}" style="background:${bg}">
        <div class="eventTitle">${escapeHtml(ev.business || "일정")}</div>
        <div class="eventLines">${showLines.join("")}</div>
      </div>
    `;
  };

  const closeModal = () => {
    const bd = document.getElementById("modalBackdrop");
    bd.style.display = "none";
    bd.innerHTML = "";
  };

  const showModal = (innerHTML) => {
    const bd = document.getElementById("modalBackdrop");
    bd.style.display = "flex";
    bd.innerHTML = innerHTML;
    bd.onclick = (e) => {
      if(e.target === bd) closeModal();
    };
  };

  const openAddModal = () => {
    const today = fmtDate(new Date());
    showModal(`
      <div class="modal">
        <div class="modalHeader">
          <div>일정 추가(기간 등록)</div>
          <button id="xClose">✕</button>
        </div>
        <div class="modalBody">
          <div class="grid2">
            <div>
              <label>시작일 (YYYY-MM-DD)</label>
              <input id="mStart" type="date" value="${today}" />
            </div>
            <div>
              <label>종료일 (YYYY-MM-DD)</label>
              <input id="mEnd" type="date" value="${today}" />
            </div>
          </div>

          <label>사업명</label>
          <input id="mBiz" type="text" placeholder="예: 대관 / 행사 / 일학습 등" />

          <label>과정</label>
          <input id="mCourse" type="text" placeholder="예: 멀티캠퍼스" />

          <div class="grid2">
            <div>
              <label>시간</label>
              <input id="mTime" type="text" placeholder="예: 10:00~14:00" />
            </div>
            <div>
              <label>인원</label>
              <input id="mPeople" type="text" placeholder="예: 10" />
            </div>
          </div>

          <div class="grid2">
            <div>
              <label>장소</label>
              <input id="mPlace" type="text" placeholder="예: 본관3층" />
            </div>
            <div>
              <label>행정</label>
              <input id="mAdmin" type="text" placeholder="예: 담당자명" />
            </div>
          </div>

          <label>제외할 날짜(선택)</label>
          <input id="mExclude" type="text" placeholder="예: 2026-01-07,2026-01-08" />
          <div class="muted" style="margin-top:6px;font-size:12px;">
            기간 중 특정 날짜만 빼고 저장하고 싶을 때
          </div>
        </div>
        <div class="modalActions">
          <button id="mCancel">닫기</button>
          <button id="mSave" class="ok">저장</button>
        </div>
      </div>
    `);

    document.getElementById("xClose").onclick = closeModal;
    document.getElementById("mCancel").onclick = closeModal;

    document.getElementById("mSave").onclick = async () => {
      try{
        const start = document.getElementById("mStart").value;
        const end = document.getElementById("mEnd").value;
        if(!start || !end){
          alert("시작/종료일을 입력하세요.");
          return;
        }

        const payload = {
          start,
          end,
          business: document.getElementById("mBiz").value.trim(),
          course: document.getElementById("mCourse").value.trim(),
          time: document.getElementById("mTime").value.trim(),
          people: document.getElementById("mPeople").value.trim(),
          place: document.getElementById("mPlace").value.trim(),
          admin: document.getElementById("mAdmin").value.trim(),
          excluded_dates: document.getElementById("mExclude").value.trim(),
        };

        const res = await fetchJSON("/api/events", {
          method:"POST",
          headers: {"Content-Type":"application/json"},
          body: JSON.stringify(payload)
        });

        // 신규 사업명은 별도 테이블 없이도 이벤트에 저장되면 businesses에 반영됨
        closeModal();
        await renderAndLoad();
      }catch(e){
        alert("저장 중 오류가 발생했습니다.\n\n" + e.message);
      }
    };
  };

  const openEventModal = (ev) => {
    // 개별 이벤트 수정/삭제(해당일만)
    showModal(`
      <div class="modal">
        <div class="modalHeader">
          <div>일정 상세/수정 (${escapeHtml(ev.event_date || ev.start)})</div>
          <button id="xClose">✕</button>
        </div>
        <div class="modalBody">
          <div class="grid2">
            <div>
              <label>날짜</label>
              <input id="eDate" type="date" value="${escapeHtml(ev.event_date || ev.start)}" />
            </div>
            <div>
              <label>사업명</label>
              <input id="eBiz" type="text" value="${escapeHtml(ev.business||"")}" />
            </div>
          </div>

          <label>과정</label>
          <input id="eCourse" type="text" value="${escapeHtml(ev.course||"")}" />

          <div class="grid2">
            <div>
              <label>시간</label>
              <input id="eTime" type="text" value="${escapeHtml(ev.time||"")}" />
            </div>
            <div>
              <label>인원</label>
              <input id="ePeople" type="text" value="${escapeHtml(ev.people||"")}" />
            </div>
          </div>

          <div class="grid2">
            <div>
              <label>장소</label>
              <input id="ePlace" type="text" value="${escapeHtml(ev.place||"")}" />
            </div>
            <div>
              <label>행정</label>
              <input id="eAdmin" type="text" value="${escapeHtml(ev.admin||"")}" />
            </div>
          </div>

          <div class="muted" style="margin-top:8px;font-size:12px;">
            ※ 비워둔 항목은 일정 카드에 표시되지 않습니다.
          </div>
        </div>
        <div class="modalActions">
          <button id="eDelete" class="danger">이날 삭제</button>
          <button id="eCancel">닫기</button>
          <button id="eSave" class="ok">수정 저장</button>
        </div>
      </div>
    `);

    document.getElementById("xClose").onclick = closeModal;
    document.getElementById("eCancel").onclick = closeModal;

    document.getElementById("eDelete").onclick = async () => {
      if(!confirm("이 날짜의 일정만 삭제할까요?")) return;
      try{
        await fetchJSON(`/api/events/${ev.id}`, { method:"DELETE" });
        closeModal();
        await renderAndLoad();
      }catch(e){
        alert("삭제 중 오류:\n" + e.message);
      }
    };

    document.getElementById("eSave").onclick = async () => {
      try{
        const payload = {
          event_date: document.getElementById("eDate").value,
          business: document.getElementById("eBiz").value.trim(),
          course: document.getElementById("eCourse").value.trim(),
          time: document.getElementById("eTime").value.trim(),
          people: document.getElementById("ePeople").value.trim(),
          place: document.getElementById("ePlace").value.trim(),
          admin: document.getElementById("eAdmin").value.trim()
        };
        await fetchJSON(`/api/events/${ev.id}`, {
          method:"PUT",
          headers: {"Content-Type":"application/json"},
          body: JSON.stringify(payload)
        });
        closeModal();
        await renderAndLoad();
      }catch(e){
        alert("수정 중 오류:\n" + e.message);
      }
    };
  };

  const renderAndLoad = async () => {
    try{
      await loadAll();
      render();
      if(state.viewMode === "month") renderCalendar();
      else renderWeek();
    }catch(e){
      alert(e.message || "로딩 오류");
      render();
    }
  };

  // Init
  renderAndLoad();
})();
</script>
</body>
</html>
"""


@app.route("/", methods=["GET"])
def index():
    return Response(HTML_PAGE, mimetype="text/html")


# Render/Gunicorn entry is app:app
