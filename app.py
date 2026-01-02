import os
import re
from datetime import date, datetime, timedelta
from flask import Flask, request, jsonify, Response
import psycopg2
import psycopg2.extras

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL 환경변수가 설정되어 있지 않습니다.")

# ----------------------------
# DB
# ----------------------------
def get_db():
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    return conn

def _col_exists(cur, table, col):
    cur.execute(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name=%s AND column_name=%s
        """,
        (table, col),
    )
    return cur.fetchone() is not None

def init_db():
    """무료 플랜에서 Shell이 없어도 자동으로 스키마 생성/보강"""
    conn = get_db()
    try:
        cur = conn.cursor()

        # 테이블 생성 (없으면)
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
                admin TEXT
            );
            """
        )

        # 기존 테이블이 예전 스키마(start/end 등)였던 경우 보강
        # event_date 없으면 추가 (이미 위에서 만들면 존재)
        if not _col_exists(cur, "events", "event_date"):
            cur.execute("ALTER TABLE events ADD COLUMN event_date DATE;")

        # 컬럼 누락 보강
        for c in ["business", "course", "time", "people", "place", "admin"]:
            if not _col_exists(cur, "events", c):
                cur.execute(f"ALTER TABLE events ADD COLUMN {c} TEXT;")

        # 예전 데이터가 start 텍스트로 존재했을 가능성: event_date가 NULL이면 가능한 범위에서 채우기
        # (start 컬럼이 있으면)
        if _col_exists(cur, "events", "start") and _col_exists(cur, "events", "event_date"):
            # start가 'YYYY-MM-DD' 형태인 것만 채움
            cur.execute(
                """
                UPDATE events
                SET event_date = CASE
                    WHEN event_date IS NULL AND start ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                    THEN start::date
                    ELSE event_date
                END
                """
            )

        # 인덱스
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_business ON events(business);")

        conn.commit()
    finally:
        conn.close()

init_db()

# ----------------------------
# Utils
# ----------------------------
def parse_yyyy_mm_dd(s: str) -> date:
    if not s or not isinstance(s, str):
        raise ValueError("date string is empty")
    # allow "YYYY-MM-DD"
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        raise ValueError("date format must be YYYY-MM-DD")
    y, m, d = map(int, s.split("-"))
    return date(y, m, d)

def daterange_inclusive(d1: date, d2: date):
    if d2 < d1:
        d1, d2 = d2, d1
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)

def clean_str(x):
    if x is None:
        return None
    if not isinstance(x, str):
        x = str(x)
    x = x.strip()
    return x if x != "" else None

def row_to_dict(r):
    return {
        "id": r["id"],
        "event_date": r["event_date"].isoformat() if r["event_date"] else None,
        "business": r["business"],
        "course": r["course"],
        "time": r["time"],
        "people": r["people"],
        "place": r["place"],
        "admin": r["admin"],
    }

# ----------------------------
# API
# ----------------------------
@app.route("/api/events", methods=["GET"])
def api_get_events():
    """
    Query params:
    - start=YYYY-MM-DD&end=YYYY-MM-DD (기간 조회)
    or
    - month=YYYY-MM (월 조회)
    """
    start = request.args.get("start")
    end = request.args.get("end")
    month = request.args.get("month")

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

        if month:
            if not re.match(r"^\d{4}-\d{2}$", month):
                return jsonify({"ok": False, "error": "month must be YYYY-MM"}), 400
            y, m = map(int, month.split("-"))
            start_d = date(y, m, 1)
            # next month
            if m == 12:
                end_d = date(y + 1, 1, 1) - timedelta(days=1)
            else:
                end_d = date(y, m + 1, 1) - timedelta(days=1)
        else:
            start_d = parse_yyyy_mm_dd(start) if start else None
            end_d = parse_yyyy_mm_dd(end) if end else None
            if not (start_d and end_d):
                # default: this month
                today = date.today()
                start_d = date(today.year, today.month, 1)
                if today.month == 12:
                    end_d = date(today.year + 1, 1, 1) - timedelta(days=1)
                else:
                    end_d = date(today.year, today.month + 1, 1) - timedelta(days=1)

        cur.execute(
            """
            SELECT id, event_date, business, course, time, people, place, admin
            FROM events
            WHERE event_date BETWEEN %s AND %s
            ORDER BY event_date ASC, id ASC
            """,
            (start_d, end_d),
        )
        rows = cur.fetchall()
        return jsonify({"ok": True, "items": [row_to_dict(r) for r in rows]})
    finally:
        conn.close()

@app.route("/api/events", methods=["POST"])
def api_create_events():
    """
    기간 등록 -> 날짜별로 개별 row 생성
    Body JSON:
    {
      "start": "YYYY-MM-DD",
      "end": "YYYY-MM-DD",
      "business": "...",
      "course": "...",
      "time": "...",
      "people": "...",
      "place": "...",
      "admin": "..."
    }
    """
    data = request.get_json(silent=True) or {}

    try:
        start_d = parse_yyyy_mm_dd(data.get("start"))
        end_d = parse_yyyy_mm_dd(data.get("end"))
    except Exception as e:
        return jsonify({"ok": False, "error": f"날짜 형식 오류: {e}"}), 400

    business = clean_str(data.get("business"))
    course = clean_str(data.get("course"))
    time_ = clean_str(data.get("time"))
    people = clean_str(data.get("people"))
    place = clean_str(data.get("place"))
    admin = clean_str(data.get("admin"))

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        created = []
        for d in daterange_inclusive(start_d, end_d):
            cur.execute(
                """
                INSERT INTO events (event_date, business, course, time, people, place, admin)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                RETURNING id, event_date, business, course, time, people, place, admin
                """,
                (d, business, course, time_, people, place, admin),
            )
            created.append(row_to_dict(cur.fetchone()))
        conn.commit()
        return jsonify({"ok": True, "items": created}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/events/<int:event_id>", methods=["PUT"])
def api_update_event(event_id: int):
    data = request.get_json(silent=True) or {}

    # event_date는 보통 변경 안 하지만 필요시 허용
    event_date = data.get("event_date")
    try:
        event_date = parse_yyyy_mm_dd(event_date).isoformat() if event_date else None
    except Exception as e:
        return jsonify({"ok": False, "error": f"event_date 형식 오류: {e}"}), 400

    business = clean_str(data.get("business"))
    course = clean_str(data.get("course"))
    time_ = clean_str(data.get("time"))
    people = clean_str(data.get("people"))
    place = clean_str(data.get("place"))
    admin = clean_str(data.get("admin"))

    conn = get_db()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute(
            """
            UPDATE events
            SET
              event_date = COALESCE(%s, event_date),
              business = %s,
              course = %s,
              time = %s,
              people = %s,
              place = %s,
              admin = %s
            WHERE id = %s
            RETURNING id, event_date, business, course, time, people, place, admin
            """,
            (event_date, business, course, time_, people, place, admin, event_id),
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return jsonify({"ok": False, "error": "not found"}), 404
        conn.commit()
        return jsonify({"ok": True, "item": row_to_dict(row)})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/events/<int:event_id>", methods=["DELETE"])
def api_delete_event(event_id: int):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM events WHERE id=%s", (event_id,))
        if cur.rowcount == 0:
            conn.rollback()
            return jsonify({"ok": False, "error": "not found"}), 404
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        conn.close()

@app.route("/api/businesses", methods=["GET"])
def api_businesses():
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT business FROM events WHERE business IS NOT NULL AND business <> '' ORDER BY business;")
        items = [r[0] for r in cur.fetchall()]
        return jsonify({"ok": True, "items": items})
    finally:
        conn.close()

# ----------------------------
# Front (Single HTML)
# ----------------------------
HTML_PAGE = r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>포항산학 월별일정</title>
  <style>
    :root{
      --border:#d7d7d7;
      --muted:#666;
      --bg:#fff;
    }
    *{box-sizing:border-box;}
    body{margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,"Noto Sans KR",sans-serif; background:var(--bg); color:#111;}
    .wrap{max-width:1200px; margin:0 auto; padding:16px 10px 24px;}
    h1{margin:10px 0 0; text-align:center; font-size:40px; letter-spacing:-1px;}
    .sub{margin:6px 0 14px; text-align:center; font-size:28px; font-weight:800;}
    .topbar{
      display:flex; flex-wrap:wrap; gap:10px;
      justify-content:center; align-items:center;
      margin:10px 0 10px;
    }
    button, select, input{
      font-size:16px; padding:10px 12px; border:1px solid var(--border); border-radius:8px; background:#fff;
    }
    button{cursor:pointer;}
    button.primary{font-weight:800;}
    .btnrow{display:flex; gap:10px; align-items:center;}
    .filters{display:flex; gap:10px; align-items:center; flex-wrap:wrap; justify-content:center;}
    .addwide{width:min(900px, 100%); margin:10px auto 12px; display:block; padding:12px 14px; font-size:18px; font-weight:900;}

    /* Calendar grid */
    table.calendar{width:100%; border-collapse:collapse; table-layout:fixed;}
    table.calendar th, table.calendar td{border:1px solid var(--border); vertical-align:top;}
    table.calendar th{height:44px; background:#fafafa; font-size:18px;}
    table.calendar td{height:130px; padding:6px;}
    .dow-sun{color:#d40000;}
    .dow-sat{color:#0070c9;}
    .daynum{font-weight:900; font-size:18px; margin-bottom:6px;}
    .events{
      display:grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap:6px;
      align-content:start;
    }
    .event{
      border-radius:12px;
      padding:8px 8px;
      border:1px solid rgba(0,0,0,.08);
      box-shadow:0 1px 0 rgba(0,0,0,.03);
      overflow:hidden;
      min-height:44px;
    }
    .event .title{font-weight:900; margin-bottom:4px; font-size:14px;}
    .event .line{font-size:12px; line-height:1.25; word-break:break-word; color:#111;}
    .event .muted{color:var(--muted);}
    .event:hover{outline:2px solid rgba(0,0,0,.08);}

    /* Week view */
    .weekhead{display:flex; gap:8px; justify-content:center; align-items:center; margin:10px 0 10px;}
    .weekgrid{width:100%; border:1px solid var(--border); border-radius:12px; overflow:hidden;}
    .weekrow{display:grid; grid-template-columns: 92px 1fr; border-top:1px solid var(--border);}
    .weekrow:first-child{border-top:none;}
    .wkdate{padding:10px; background:#fafafa; font-weight:900; border-right:1px solid var(--border);}
    .wkcontent{padding:10px;}
    .wkcards{display:grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap:8px;}
    .wkcards .event{cursor:pointer;}

    /* Modal */
    .modalback{position:fixed; inset:0; background:rgba(0,0,0,.35); display:none; align-items:center; justify-content:center; padding:16px; z-index:50;}
    .modal{width:min(720px, 100%); background:#fff; border-radius:14px; padding:14px; box-shadow:0 10px 30px rgba(0,0,0,.2);}
    .modal h2{margin:0 0 10px; font-size:20px;}
    .grid{display:grid; grid-template-columns:1fr 1fr; gap:10px;}
    .grid .full{grid-column:1/-1;}
    .row{display:flex; flex-direction:column; gap:6px;}
    .row label{font-size:12px; color:var(--muted); font-weight:800;}
    .actions{display:flex; gap:10px; justify-content:flex-end; margin-top:12px; flex-wrap:wrap;}
    .danger{border-color:#ffb3b3; background:#fff5f5;}
    .small{font-size:12px; color:var(--muted);}

    /* Mobile tuning */
    @media (max-width: 820px){
      h1{font-size:34px;}
      .sub{font-size:22px;}
      table.calendar td{height:120px;}
      .events{grid-template-columns: 1fr;} /* 기본은 1열 */
      .event .title{font-size:13px;}
      .event .line{font-size:12px;}
      .grid{grid-template-columns:1fr;}
      .wkcards{grid-template-columns: 1fr;}
    }
    @media (max-width: 420px){
      table.calendar td{height:112px;}
    }

    /* Optional "Rotate / Landscape" */
    body.force-landscape .wrap{
      transform: rotate(90deg);
      transform-origin: top left;
      position:absolute;
      top:0;
      left:100%;
      width:100vh;
      min-height:100vw;
      padding:10px;
      background:#fff;
    }
    body.force-landscape{height:100vw; overflow:auto;}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>포항산학 월별일정</h1>
    <div class="sub" id="titleYM">-</div>

    <div class="topbar">
      <div class="btnrow">
        <button id="btnPrev">◀ 이전</button>
        <button id="btnNext">다음 ▶</button>
        <button id="btnMonth" class="primary">월별</button>
        <button id="btnWeek">주별</button>
        <button id="btnRotate">가로모드</button>
      </div>
      <div class="filters">
        <div style="display:flex; align-items:center; gap:8px;">
          <span style="font-weight:900;">사업명:</span>
          <select id="bizFilter">
            <option value="__ALL__">전체</option>
          </select>
        </div>
        <button id="btnReset">필터 초기화</button>
      </div>
    </div>

    <button class="addwide" id="btnAdd">+ 일정 추가하기</button>

    <div id="monthView"></div>
    <div id="weekView" style="display:none;"></div>
  </div>

  <div class="modalback" id="modalBack">
    <div class="modal">
      <h2 id="modalTitle">일정</h2>

      <div class="grid">
        <div class="row">
          <label>시작일 (YYYY-MM-DD)</label>
          <input id="fStart" placeholder="2026-01-05" />
        </div>
        <div class="row">
          <label>종료일 (YYYY-MM-DD)</label>
          <input id="fEnd" placeholder="2026-01-09" />
        </div>

        <div class="row full">
          <label>사업명</label>
          <input id="fBusiness" placeholder="예: 지산맞 / 재직자 / 청년일경험 ..." />
        </div>

        <div class="row full">
          <label>과정</label>
          <input id="fCourse" placeholder="예: 파이썬, 용접, 전기..." />
        </div>

        <div class="row">
          <label>시간</label>
          <input id="fTime" placeholder="예: 09:00~18:00" />
        </div>
        <div class="row">
          <label>인원</label>
          <input id="fPeople" placeholder="예: 20" />
        </div>

        <div class="row">
          <label>장소</label>
          <input id="fPlace" placeholder="예: 1강의실 / 실습실..." />
        </div>
        <div class="row">
          <label>행정</label>
          <input id="fAdmin" placeholder="예: 담당자명" />
        </div>

        <div class="row full small" id="editHint" style="display:none;">
          ※ 기간등록은 “새로 추가”에서만 사용됩니다. 이미 등록된 일정은 날짜별로 개별 수정/삭제가 가능합니다.
        </div>
      </div>

      <div class="actions">
        <button id="btnDelete" class="danger" style="display:none;">이 날짜 일정 삭제</button>
        <button id="btnSave" class="primary">저장</button>
        <button id="btnClose">닫기</button>
      </div>
    </div>
  </div>

<script>
  // -------------------------
  // State
  // -------------------------
  let mode = "month"; // month | week
  let cur = new Date(); // current anchor date
  let allEvents = [];
  let biz = "__ALL__";
  let editingId = null;

  // -------------------------
  // Helpers
  // -------------------------
  const pad2 = (n) => String(n).padStart(2, "0");
  const fmtDate = (d) => `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())}`;
  const parseDate = (s) => {
    if(!s || !/^\\d{4}-\\d{2}-\\d{2}$/.test(s)) return null;
    const [y,m,da] = s.split("-").map(Number);
    const d = new Date(y, m-1, da);
    if(d.getFullYear()!==y || d.getMonth()!==m-1 || d.getDate()!==da) return null;
    return d;
  };
  const yyyymm = (d) => `${d.getFullYear()}-${pad2(d.getMonth()+1)}`;
  const startOfWeek = (d) => {
    const x = new Date(d);
    const day = x.getDay(); // 0 sun
    x.setDate(x.getDate() - day);
    x.setHours(0,0,0,0);
    return x;
  };
  const endOfWeek = (d) => {
    const s = startOfWeek(d);
    const e = new Date(s);
    e.setDate(e.getDate()+6);
    return e;
  };

  function hashHue(str){
    // deterministic hue 0~359
    let h = 0;
    for(let i=0;i<str.length;i++){
      h = (h*31 + str.charCodeAt(i)) % 360;
    }
    return h;
  }
  function bizColor(b){
    if(!b) return "hsl(210 70% 92%)";
    const h = hashHue(b);
    return `hsl(${h} 70% 88%)`;
  }

  function fieldLine(label, value){
    if(!value) return "";
    return `<div class="line"><span class="muted">${label}</span> ${escapeHtml(value)}</div>`;
  }

  function escapeHtml(s){
    return String(s)
      .replaceAll("&","&amp;")
      .replaceAll("<","&lt;")
      .replaceAll(">","&gt;")
      .replaceAll('"',"&quot;")
      .replaceAll("'","&#39;");
  }

  // -------------------------
  // API
  // -------------------------
  async function apiGetEventsForCurrent(){
    if(mode==="month"){
      const m = yyyymm(cur);
      const r = await fetch(`/api/events?month=${encodeURIComponent(m)}`);
      const j = await r.json();
      if(!j.ok) throw new Error(j.error || "불러오기 실패");
      allEvents = j.items || [];
    }else{
      const s = startOfWeek(cur);
      const e = endOfWeek(cur);
      const r = await fetch(`/api/events?start=${fmtDate(s)}&end=${fmtDate(e)}`);
      const j = await r.json();
      if(!j.ok) throw new Error(j.error || "불러오기 실패");
      allEvents = j.items || [];
    }
  }

  async function apiBusinesses(){
    const r = await fetch(`/api/businesses`);
    const j = await r.json();
    if(!j.ok) return [];
    return j.items || [];
  }

  async function apiCreateRange(payload){
    const r = await fetch(`/api/events`, {
      method:"POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });
    const j = await r.json().catch(()=>({ok:false, error:"서버 응답 파싱 실패"}));
    if(!j.ok) throw new Error(j.error || "저장 실패");
    return j.items;
  }

  async function apiUpdateOne(id, payload){
    const r = await fetch(`/api/events/${id}`, {
      method:"PUT",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });
    const j = await r.json().catch(()=>({ok:false, error:"서버 응답 파싱 실패"}));
    if(!j.ok) throw new Error(j.error || "수정 실패");
    return j.item;
  }

  async function apiDeleteOne(id){
    const r = await fetch(`/api/events/${id}`, { method:"DELETE" });
    const j = await r.json().catch(()=>({ok:false, error:"서버 응답 파싱 실패"}));
    if(!j.ok) throw new Error(j.error || "삭제 실패");
    return true;
  }

  // -------------------------
  // Render
  // -------------------------
  function filteredEvents(){
    if(biz==="__ALL__") return allEvents;
    return allEvents.filter(e => (e.business||"") === biz);
  }

  function renderTitle(){
    if(mode==="month"){
      document.getElementById("titleYM").textContent =
        `${cur.getFullYear()}년 ${cur.getMonth()+1}월`;
    }else{
      const s = startOfWeek(cur);
      const e = endOfWeek(cur);
      document.getElementById("titleYM").textContent =
        `${s.getFullYear()}년 ${s.getMonth()+1}월 ${s.getDate()}일 ~ ${e.getFullYear()}년 ${e.getMonth()+1}월 ${e.getDate()}일`;
    }
  }

  function renderMonth(){
    const container = document.getElementById("monthView");
    container.innerHTML = "";

    const y = cur.getFullYear();
    const m = cur.getMonth();
    const first = new Date(y, m, 1);
    const last = new Date(y, m+1, 0);

    const startDay = first.getDay(); // 0 sun
    const totalDays = last.getDate();

    const events = filteredEvents();
    const byDate = new Map();
    for(const e of events){
      if(!e.event_date) continue;
      if(!byDate.has(e.event_date)) byDate.set(e.event_date, []);
      byDate.get(e.event_date).push(e);
    }

    const table = document.createElement("table");
    table.className = "calendar";

    const thead = document.createElement("thead");
    const trh = document.createElement("tr");
    const dows = ["일","월","화","수","목","금","토"];
    dows.forEach((d,i)=>{
      const th = document.createElement("th");
      th.textContent = d;
      if(i===0) th.classList.add("dow-sun");
      if(i===6) th.classList.add("dow-sat");
      trh.appendChild(th);
    });
    thead.appendChild(trh);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    let day = 1;
    for(let r=0; r<6; r++){
      const tr = document.createElement("tr");
      for(let c=0; c<7; c++){
        const td = document.createElement("td");
        const cellIndex = r*7 + c;
        if(cellIndex < startDay || day > totalDays){
          td.innerHTML = "";
        }else{
          const d = new Date(y, m, day);
          const key = fmtDate(d);

          const daynum = document.createElement("div");
          daynum.className = "daynum";
          daynum.textContent = day;
          if(c===0) daynum.style.color = "#d40000";
          if(c===6) daynum.style.color = "#0070c9";
          td.appendChild(daynum);

          const evWrap = document.createElement("div");
          evWrap.className = "events";

          const list = (byDate.get(key) || []);
          for(const e of list){
            const card = document.createElement("div");
            card.className = "event";
            card.style.background = bizColor(e.business || "");
            card.innerHTML = `
              <div class="title">${escapeHtml(e.course || e.business || "일정")}</div>
              ${fieldLine("사업:", e.business)}
              ${fieldLine("시간:", e.time)}
              ${fieldLine("인원:", e.people)}
              ${fieldLine("장소:", e.place)}
              ${fieldLine("행정:", e.admin)}
            `;
            card.addEventListener("click", ()=> openEdit(e));
            evWrap.appendChild(card);
          }

          td.appendChild(evWrap);
          day++;
        }
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
      if(day > totalDays) break;
    }

    table.appendChild(tbody);
    container.appendChild(table);
  }

  function renderWeek(){
    const container = document.getElementById("weekView");
    container.innerHTML = "";

    const events = filteredEvents();
    const byDate = new Map();
    for(const e of events){
      if(!e.event_date) continue;
      if(!byDate.has(e.event_date)) byDate.set(e.event_date, []);
      byDate.get(e.event_date).push(e);
    }

    const grid = document.createElement("div");
    grid.className = "weekgrid";

    const s = startOfWeek(cur);
    for(let i=0;i<7;i++){
      const d = new Date(s);
      d.setDate(d.getDate()+i);
      const key = fmtDate(d);

      const row = document.createElement("div");
      row.className = "weekrow";

      const left = document.createElement("div");
      left.className = "wkdate";
      const dow = ["일","월","화","수","목","금","토"][d.getDay()];
      left.textContent = `${d.getMonth()+1}/${d.getDate()} (${dow})`;

      const right = document.createElement("div");
      right.className = "wkcontent";

      const cards = document.createElement("div");
      cards.className = "wkcards";

      const list = (byDate.get(key) || []);
      for(const e of list){
        const card = document.createElement("div");
        card.className = "event";
        card.style.background = bizColor(e.business || "");
        card.innerHTML = `
          <div class="title">${escapeHtml(e.course || e.business || "일정")}</div>
          ${fieldLine("사업:", e.business)}
          ${fieldLine("시간:", e.time)}
          ${fieldLine("인원:", e.people)}
          ${fieldLine("장소:", e.place)}
          ${fieldLine("행정:", e.admin)}
        `;
        card.addEventListener("click", ()=> openEdit(e));
        cards.appendChild(card);
      }

      right.appendChild(cards);
      row.appendChild(left);
      row.appendChild(right);
      grid.appendChild(row);
    }

    container.appendChild(grid);
  }

  async function refresh(){
    renderTitle();
    await apiGetEventsForCurrent();
    renderTitle();

    if(mode==="month"){
      document.getElementById("monthView").style.display = "";
      document.getElementById("weekView").style.display = "none";
      renderMonth();
    }else{
      document.getElementById("monthView").style.display = "none";
      document.getElementById("weekView").style.display = "";
      renderWeek();
    }

    // business dropdown 갱신
    const list = await apiBusinesses();
    const sel = document.getElementById("bizFilter");
    const curVal = sel.value || "__ALL__";
    sel.innerHTML = `<option value="__ALL__">전체</option>` + list.map(x=>`<option value="${escapeHtml(x)}">${escapeHtml(x)}</option>`).join("");
    // 유지
    if([...sel.options].some(o=>o.value===curVal)) sel.value = curVal;
    biz = sel.value;
  }

  // -------------------------
  // Modal (add/edit)
  // -------------------------
  function openAdd(){
    editingId = null;
    document.getElementById("modalTitle").textContent = "일정 추가(기간 등록)";
    document.getElementById("btnDelete").style.display = "none";
    document.getElementById("editHint").style.display = "none";

    const today = new Date();
    document.getElementById("fStart").value = fmtDate(today);
    document.getElementById("fEnd").value = fmtDate(today);
    document.getElementById("fBusiness").value = "";
    document.getElementById("fCourse").value = "";
    document.getElementById("fTime").value = "";
    document.getElementById("fPeople").value = "";
    document.getElementById("fPlace").value = "";
    document.getElementById("fAdmin").value = "";

    document.getElementById("modalBack").style.display = "flex";
  }

  function openEdit(e){
    editingId = e.id;
    document.getElementById("modalTitle").textContent = `일정 수정 (해당 날짜만)`;
    document.getElementById("btnDelete").style.display = "";
    document.getElementById("editHint").style.display = "";

    document.getElementById("fStart").value = e.event_date || "";
    document.getElementById("fEnd").value = e.event_date || "";

    document.getElementById("fBusiness").value = e.business || "";
    document.getElementById("fCourse").value = e.course || "";
    document.getElementById("fTime").value = e.time || "";
    document.getElementById("fPeople").value = e.people || "";
    document.getElementById("fPlace").value = e.place || "";
    document.getElementById("fAdmin").value = e.admin || "";

    document.getElementById("modalBack").style.display = "flex";
  }

  function closeModal(){
    document.getElementById("modalBack").style.display = "none";
  }

  async function onSave(){
    try{
      const s = parseDate(document.getElementById("fStart").value.trim());
      const e = parseDate(document.getElementById("fEnd").value.trim());
      if(!s || !e) return alert("시작/종료일은 YYYY-MM-DD 형식으로 입력하세요.");

      const payload = {
        start: fmtDate(s),
        end: fmtDate(e),
        business: document.getElementById("fBusiness").value.trim(),
        course: document.getElementById("fCourse").value.trim(),
        time: document.getElementById("fTime").value.trim(),
        people: document.getElementById("fPeople").value.trim(),
        place: document.getElementById("fPlace").value.trim(),
        admin: document.getElementById("fAdmin").value.trim()
      };

      if(editingId){
        // 편집은 "해당 날짜만" -> start/end는 같은 날이어야 함
        if(payload.start !== payload.end){
          return alert("수정은 날짜별(1일)만 가능합니다. 시작일과 종료일을 같은 날짜로 맞춰주세요.");
        }
        await apiUpdateOne(editingId, {
          event_date: payload.start,
          business: payload.business,
          course: payload.course,
          time: payload.time,
          people: payload.people,
          place: payload.place,
          admin: payload.admin
        });
      }else{
        await apiCreateRange(payload);
      }

      closeModal();
      await refresh();
    }catch(err){
      alert("저장 중 오류가 발생했습니다.\\n" + (err?.message || err));
    }
  }

  async function onDelete(){
    if(!editingId) return;
    if(!confirm("이 날짜의 일정만 삭제할까요?")) return;
    try{
      await apiDeleteOne(editingId);
      closeModal();
      await refresh();
    }catch(err){
      alert("삭제 중 오류가 발생했습니다.\\n" + (err?.message || err));
    }
  }

  // -------------------------
  // Events
  // -------------------------
  document.getElementById("btnAdd").addEventListener("click", openAdd);
  document.getElementById("btnClose").addEventListener("click", closeModal);
  document.getElementById("modalBack").addEventListener("click", (e)=>{ if(e.target.id==="modalBack") closeModal(); });
  document.getElementById("btnSave").addEventListener("click", onSave);
  document.getElementById("btnDelete").addEventListener("click", onDelete);

  document.getElementById("bizFilter").addEventListener("change", async (e)=>{
    biz = e.target.value;
    await refresh();
  });
  document.getElementById("btnReset").addEventListener("click", async ()=>{
    biz = "__ALL__";
    document.getElementById("bizFilter").value = "__ALL__";
    await refresh();
  });

  document.getElementById("btnMonth").addEventListener("click", async ()=>{
    mode = "month";
    document.getElementById("btnMonth").classList.add("primary");
    document.getElementById("btnWeek").classList.remove("primary");
    await refresh();
  });
  document.getElementById("btnWeek").addEventListener("click", async ()=>{
    mode = "week";
    document.getElementById("btnWeek").classList.add("primary");
    document.getElementById("btnMonth").classList.remove("primary");
    await refresh();
  });

  document.getElementById("btnPrev").addEventListener("click", async ()=>{
    if(mode==="month"){
      cur = new Date(cur.getFullYear(), cur.getMonth()-1, 1);
    }else{
      cur = new Date(cur);
      cur.setDate(cur.getDate()-7);
    }
    await refresh();
  });
  document.getElementById("btnNext").addEventListener("click", async ()=>{
    if(mode==="month"){
      cur = new Date(cur.getFullYear(), cur.getMonth()+1, 1);
    }else{
      cur = new Date(cur);
      cur.setDate(cur.getDate()+7);
    }
    await refresh();
  });

  document.getElementById("btnRotate").addEventListener("click", ()=>{
    document.body.classList.toggle("force-landscape");
  });

  // init
  refresh().catch(err=>{
    alert("초기 로딩 실패: " + (err?.message || err));
  });
</script>
</body>
</html>
"""

@app.route("/", methods=["GET"])
def index():
    return Response(HTML_PAGE, mimetype="text/html")
