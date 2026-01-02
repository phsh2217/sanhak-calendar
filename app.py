import os
import re
import json
from datetime import datetime, date, timedelta

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

# -----------------------------
# Config
# -----------------------------
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
if not DATABASE_URL:
    # Render 로그에 명확히 찍히게
    raise RuntimeError("Error: DATABASE_URL 환경변수가 설정되어 있지 않습니다.")

# (선택) Render의 DATABASE_URL이 'postgres://'로 시작하면 psycopg2가 싫어할 수 있어 보정
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://") :]


def get_conn():
    # sslmode=require 는 Render Postgres에 안전
    return psycopg2.connect(DATABASE_URL, sslmode="require")


def init_db():
    """테이블/인덱스를 '있으면 건드리지 않고' 생성"""
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id SERIAL PRIMARY KEY,
                    event_date DATE NOT NULL,
                    business TEXT,
                    course TEXT,
                    time TEXT,
                    people TEXT,
                    place TEXT,
                    admin TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    updated_at TIMESTAMP DEFAULT NOW()
                );
                """
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_business ON events(business);")


init_db()


# -----------------------------
# Utils
# -----------------------------
def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def daterange(d1: date, d2: date):
    if d2 < d1:
        d1, d2 = d2, d1
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)


def clean_text(v):
    if v is None:
        return None
    v = str(v).strip()
    return v if v else None


def safe_json_error(message, status=400):
    return jsonify({"ok": False, "error": message}), status


# -----------------------------
# API
# -----------------------------
@app.get("/api/businesses")
def api_businesses():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT business
                FROM events
                WHERE business IS NOT NULL AND business <> ''
                ORDER BY business;
                """
            )
            items = [r[0] for r in cur.fetchall()]
    return jsonify({"ok": True, "items": items})


@app.get("/api/events")
def api_list_events():
    """
    GET /api/events?start=YYYY-MM-DD&end=YYYY-MM-DD&business=...
    """
    start = request.args.get("start")
    end = request.args.get("end")
    business = request.args.get("business")

    # 기본: 이번 달 +- 여유 없이도 되지만, 프론트에서 항상 범위를 준다 가정
    if not start or not end:
        today = date.today()
        start_d = date(today.year, today.month, 1)
        end_d = start_d + timedelta(days=42)  # 넉넉히
    else:
        start_d = parse_ymd(start)
        end_d = parse_ymd(end)

    q = """
        SELECT id, event_date, business, course, time, people, place, admin
        FROM events
        WHERE event_date BETWEEN %s AND %s
    """
    params = [start_d, end_d]

    if business and business != "전체":
        q += " AND business = %s"
        params.append(business)

    q += " ORDER BY event_date ASC, id ASC;"

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(q, params)
            rows = cur.fetchall()

    # JSON friendly
    for r in rows:
        r["event_date"] = r["event_date"].isoformat()

    return jsonify({"ok": True, "items": rows})


@app.post("/api/events")
def api_create_events():
    """
    기간 등록 가능:
      {
        "start": "2026-01-05",
        "end": "2026-01-09",
        "business": "...",
        ...
      }
    또는 단일:
      {"event_date":"2026-01-05", ...}
    """
    data = request.get_json(force=True) or {}

    start = data.get("start")
    end = data.get("end")
    event_date = data.get("event_date")

    # start/end 우선, 없으면 event_date 사용
    if not start and not event_date:
        return safe_json_error("start 또는 event_date가 필요합니다.", 400)

    if start:
        start_d = parse_ymd(start)
        end_d = parse_ymd(end) if end else start_d
    else:
        start_d = parse_ymd(event_date)
        end_d = start_d

    excluded = data.get("excluded_dates") or []
    excluded_set = set()
    for x in excluded:
        try:
            excluded_set.add(parse_ymd(x))
        except Exception:
            pass

    business = clean_text(data.get("business"))
    course = clean_text(data.get("course"))
    time_ = clean_text(data.get("time"))
    people = clean_text(data.get("people"))
    place = clean_text(data.get("place"))
    admin = clean_text(data.get("admin"))

    created_ids = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            for d in daterange(start_d, end_d):
                if d in excluded_set:
                    continue
                cur.execute(
                    """
                    INSERT INTO events (event_date, business, course, time, people, place, admin, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s, NOW())
                    RETURNING id;
                    """,
                    (d, business, course, time_, people, place, admin),
                )
                created_ids.append(cur.fetchone()[0])

    return jsonify({"ok": True, "created_ids": created_ids}), 201


@app.put("/api/events/<int:event_id>")
def api_update_event(event_id: int):
    data = request.get_json(force=True) or {}

    # 날짜 변경도 허용
    if data.get("event_date"):
        try:
            event_date = parse_ymd(data["event_date"])
        except Exception:
            return safe_json_error("event_date 형식이 올바르지 않습니다(YYYY-MM-DD).", 400)
    else:
        event_date = None

    business = clean_text(data.get("business"))
    course = clean_text(data.get("course"))
    time_ = clean_text(data.get("time"))
    people = clean_text(data.get("people"))
    place = clean_text(data.get("place"))
    admin = clean_text(data.get("admin"))

    sets = []
    params = []

    def add_set(col, val):
        sets.append(f"{col} = %s")
        params.append(val)

    if event_date is not None:
        add_set("event_date", event_date)
    add_set("business", business)
    add_set("course", course)
    add_set("time", time_)
    add_set("people", people)
    add_set("place", place)
    add_set("admin", admin)
    sets.append("updated_at = NOW()")

    params.append(event_id)

    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                f"""
                UPDATE events
                SET {", ".join(sets)}
                WHERE id = %s
                RETURNING id, event_date, business, course, time, people, place, admin;
                """,
                params,
            )
            row = cur.fetchone()

    if not row:
        return safe_json_error("해당 이벤트를 찾지 못했습니다.", 404)

    row["event_date"] = row["event_date"].isoformat()
    return jsonify({"ok": True, "item": row})


@app.delete("/api/events/<int:event_id>")
def api_delete_event(event_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM events WHERE id=%s RETURNING id;", (event_id,))
            deleted = cur.fetchone()
    if not deleted:
        return safe_json_error("이미 삭제되었거나 존재하지 않습니다.", 404)
    return jsonify({"ok": True, "deleted_id": event_id})


# -----------------------------
# Front (single-file HTML)
# -----------------------------
def _html():
    # 주의: 이 함수는 "그냥 문자열"만 반환해야 함 (파이썬이 JS를 실행하면 안됨)
    return r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover" />
  <title>포항산학 월별일정</title>
  <style>
    :root{
      --border:#d9d9d9;
      --muted:#666;
      --bg:#fff;
      --cardText:#111;
      --shadow:0 2px 8px rgba(0,0,0,.08);
      --radius:14px;
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      font-family: system-ui, -apple-system, Segoe UI, Roboto, "Noto Sans KR", Arial, sans-serif;
      background: var(--bg);
      color:#111;
    }
    .wrap{
      max-width: 1200px;
      margin: 0 auto;
      padding: 18px 14px 40px;
    }
    h1{
      margin: 10px 0 6px;
      text-align:center;
      font-size: clamp(26px, 4.5vw, 44px);
      letter-spacing:-1px;
    }
    .subtitle{
      text-align:center;
      font-weight:800;
      font-size: clamp(18px, 3vw, 30px);
      margin-bottom: 14px;
    }

    .topbar{
      display:flex;
      justify-content:center;
      gap:10px;
      flex-wrap:wrap;
      margin: 8px 0 14px;
    }
    .btn{
      border:1px solid #bdbdbd;
      background:#fff;
      padding: 10px 14px;
      border-radius:10px;
      cursor:pointer;
      font-size:16px;
      font-weight:700;
      min-height:42px;
    }
    .btn:active{transform: translateY(1px)}
    .btn.primary{
      background:#111;
      color:#fff;
      border-color:#111;
    }

    .controls{
      display:flex;
      align-items:center;
      justify-content:center;
      gap:10px;
      flex-wrap:wrap;
      margin: 10px 0 14px;
    }
    label{
      font-weight:900;
      font-size:18px;
    }
    select{
      min-height:42px;
      font-size:16px;
      font-weight:700;
      padding: 8px 10px;
      border-radius:10px;
      border:1px solid #bdbdbd;
      background:#fff;
    }

    /* 모바일에서 가독성: 달력은 가로 스크롤 허용 */
    .calendar-scroll{
      overflow-x:auto;
      -webkit-overflow-scrolling:touch;
      padding-bottom: 10px;
    }
    .calendar{
      width:100%;
      min-width: 980px; /* 모바일 세로에서 '가로로 보여야' 읽힘 → 스크롤 */
      border:1px solid var(--border);
      border-radius: 12px;
      overflow:hidden;
      background:#fff;
    }
    .dow{
      display:grid;
      grid-template-columns: repeat(7, 1fr);
      border-bottom:1px solid var(--border);
      background:#fafafa;
      font-weight:900;
      font-size:18px;
    }
    .dow div{
      padding: 10px 8px;
      text-align:center;
      border-right:1px solid var(--border);
    }
    .dow div:last-child{border-right:none}
    .dow .sun{color:#d50000}
    .dow .sat{color:#0b57d0}

    .grid{
      display:grid;
      grid-template-columns: repeat(7, 1fr);
    }
    .cell{
      min-height: 124px;
      border-right:1px solid var(--border);
      border-bottom:1px solid var(--border);
      padding: 8px;
      position:relative;
      background:#fff;
    }
    .cell:nth-child(7n){border-right:none}

    .daynum{
      font-weight:900;
      font-size:18px;
      margin-bottom: 6px;
    }
    .muted{color:var(--muted)}

    /* ✅ 한 행에 2개씩 나오게: 이벤트 카드 그리드 */
    .events-grid{
      display:grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
      align-items:start;
    }

    .event{
      border-radius: 12px;
      padding: 8px 8px;
      box-shadow: var(--shadow);
      cursor:pointer;
      border:1px solid rgba(0,0,0,.06);
      color:var(--cardText);
      overflow:hidden;
    }
    .event .title{
      font-weight:1000;
      font-size: 16px;
      margin-bottom: 4px;
      white-space:nowrap;
      overflow:hidden;
      text-overflow:ellipsis;
    }
    .event .line{
      font-size: 13px;
      line-height: 1.25;
      margin: 2px 0;
      white-space:nowrap;
      overflow:hidden;
      text-overflow:ellipsis;
    }

    /* 주별 보기 */
    .week-wrap{
      border:1px solid var(--border);
      border-radius: 12px;
      overflow:hidden;
      background:#fff;
    }
    .week-head{
      display:grid;
      grid-template-columns: 140px 1fr;
      border-bottom:1px solid var(--border);
      background:#fafafa;
      font-weight:900;
    }
    .week-row{
      display:grid;
      grid-template-columns: 140px 1fr;
      border-bottom:1px solid var(--border);
    }
    .week-row:last-child{border-bottom:none}
    .week-date{
      padding:10px;
      border-right:1px solid var(--border);
      font-weight:900;
      font-size:16px;
    }
    .week-events{
      padding:10px;
      display:grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }

    /* Modal */
    .modal-backdrop{
      position:fixed;
      inset:0;
      background: rgba(0,0,0,.42);
      display:none;
      align-items:center;
      justify-content:center;
      padding: 14px;
      z-index: 9999;
    }
    .modal{
      width:min(720px, 100%);
      background:#fff;
      border-radius: 16px;
      box-shadow: 0 12px 40px rgba(0,0,0,.22);
      overflow:hidden;
    }
    .modal header{
      padding: 12px 14px;
      font-weight:1000;
      border-bottom:1px solid var(--border);
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:10px;
    }
    .modal .content{
      padding: 12px 14px 2px;
    }
    .row{
      display:grid;
      grid-template-columns: 120px 1fr;
      gap: 10px;
      margin-bottom: 10px;
      align-items:center;
    }
    .row input, .row textarea{
      width:100%;
      border:1px solid #bdbdbd;
      border-radius:10px;
      padding:10px 10px;
      font-size:16px;
    }
    .row textarea{min-height:80px; resize:vertical}
    .modal footer{
      padding: 12px 14px;
      display:flex;
      gap:10px;
      justify-content:flex-end;
      border-top:1px solid var(--border);
      flex-wrap:wrap;
    }

    /* 모바일에서 버튼/텍스트 가독성 */
    @media (max-width: 560px){
      .wrap{padding:14px 10px 34px}
      .btn{font-size:15px;padding:10px 12px}
      label{font-size:17px}
      .calendar{min-width: 980px} /* 세로에서도 가로 스크롤 */
      .event .title{font-size:15px}
      .event .line{font-size:12px}
    }
  </style>
</head>

<body>
  <div class="wrap">
    <h1>포항산학 월별일정</h1>
    <div class="subtitle" id="subtitle"></div>

    <div class="topbar">
      <button class="btn" id="prevBtn">◀ 이전</button>
      <button class="btn" id="nextBtn">다음 ▶</button>
      <button class="btn" id="monthBtn">월별</button>
      <button class="btn" id="weekBtn">주별</button>
    </div>

    <div class="controls">
      <label>사업명:</label>
      <select id="businessFilter"></select>
      <button class="btn" id="resetFilterBtn">필터 초기화</button>
      <button class="btn primary" id="addBtn">+ 일정 추가하기</button>
    </div>

    <div id="viewMonth" class="calendar-scroll">
      <div class="calendar">
        <div class="dow">
          <div class="sun">일</div><div>월</div><div>화</div><div>수</div><div>목</div><div>금</div><div class="sat">토</div>
        </div>
        <div class="grid" id="monthGrid"></div>
      </div>
    </div>

    <div id="viewWeek" style="display:none;">
      <div class="week-wrap">
        <div class="week-head">
          <div style="padding:10px;border-right:1px solid var(--border);">날짜</div>
          <div style="padding:10px;">일정</div>
        </div>
        <div id="weekBody"></div>
      </div>
    </div>

  </div>

  <!-- Modal -->
  <div class="modal-backdrop" id="modalBackdrop">
    <div class="modal">
      <header>
        <div id="modalTitle">일정</div>
        <button class="btn" id="closeModalBtn">닫기</button>
      </header>
      <div class="content">
        <div class="row">
          <div style="font-weight:900;">기간</div>
          <div style="display:flex;gap:8px;flex-wrap:wrap">
            <input id="startDate" type="date" />
            <input id="endDate" type="date" />
            <div class="muted" style="font-size:13px;align-self:center">※ 등록은 기간 가능 / 등록 후에는 날짜별 개별 수정·삭제</div>
          </div>
        </div>

        <div class="row"><div style="font-weight:900;">사업명</div><input id="business" placeholder="예: 지산맞, 일학습, 사업주..." /></div>
        <div class="row"><div style="font-weight:900;">과정</div><input id="course" placeholder="예: 파이썬, 용접, HRD..." /></div>
        <div class="row"><div style="font-weight:900;">시간</div><input id="time" placeholder="예: 09:00~18:00" /></div>
        <div class="row"><div style="font-weight:900;">인원</div><input id="people" placeholder="예: 20" /></div>
        <div class="row"><div style="font-weight:900;">장소</div><input id="place" placeholder="예: 본관 3층, 2강의실" /></div>
        <div class="row"><div style="font-weight:900;">행정</div><input id="admin" placeholder="예: 담당자 성명" /></div>

      </div>
      <footer>
        <button class="btn" id="deleteBtn" style="display:none;">이 날짜만 삭제</button>
        <button class="btn primary" id="saveBtn">저장</button>
      </footer>
    </div>
  </div>

  <script>
    // ----------------------------
    // State
    // ----------------------------
    const state = {
      mode: "month",          // month | week
      cursor: new Date(),     // 기준 날짜
      business: "전체",
      events: [],             // range fetched
      selectedEvent: null,    // {id,...} for edit/delete
    };

    const $ = (id) => document.getElementById(id);

    function pad2(n){ return String(n).padStart(2,"0"); }
    function toYMD(dt){
      const y = dt.getFullYear();
      const m = pad2(dt.getMonth()+1);
      const d = pad2(dt.getDate());
      return `${y}-${m}-${d}`;
    }
    function ymdToDate(ymd){
      const [y,m,d] = ymd.split("-").map(Number);
      return new Date(y, m-1, d);
    }
    function monthTitle(dt){
      return `${dt.getFullYear()}년 ${dt.getMonth()+1}월`;
    }

    // business -> color (stable)
    function hashCode(str){
      let h=0;
      for(let i=0;i<str.length;i++){
        h = ((h<<5)-h) + str.charCodeAt(i);
        h |= 0;
      }
      return Math.abs(h);
    }
    function businessColor(biz){
      const base = hashCode(biz || "기타");
      const h = base % 360;
      // 보기 편한 파스텔
      return `hsl(${h} 70% 86%)`;
    }

    // ----------------------------
    // API
    // ----------------------------
    async function apiGet(url){
      const r = await fetch(url);
      const j = await r.json();
      if(!j.ok) throw new Error(j.error || "API 오류");
      return j;
    }
    async function apiPost(url, body){
      const r = await fetch(url, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
      const j = await r.json().catch(()=>({ok:false,error:"서버 응답 파싱 실패"}));
      if(!j.ok) throw new Error(j.error || "저장 실패");
      return j;
    }
    async function apiPut(url, body){
      const r = await fetch(url, {method:"PUT", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body)});
      const j = await r.json().catch(()=>({ok:false,error:"서버 응답 파싱 실패"}));
      if(!j.ok) throw new Error(j.error || "수정 실패");
      return j;
    }
    async function apiDel(url){
      const r = await fetch(url, {method:"DELETE"});
      const j = await r.json().catch(()=>({ok:false,error:"서버 응답 파싱 실패"}));
      if(!j.ok) throw new Error(j.error || "삭제 실패");
      return j;
    }

    // ----------------------------
    // Load & Render
    // ----------------------------
    async function loadBusinesses(){
      const j = await apiGet("/api/businesses");
      const sel = $("businessFilter");
      sel.innerHTML = "";
      const optAll = document.createElement("option");
      optAll.value = "전체";
      optAll.textContent = "전체";
      sel.appendChild(optAll);

      j.items.forEach(b=>{
        const o = document.createElement("option");
        o.value = b;
        o.textContent = b;
        sel.appendChild(o);
      });
      sel.value = state.business;
    }

    function getMonthRange(cursor){
      const first = new Date(cursor.getFullYear(), cursor.getMonth(), 1);
      const start = new Date(first);
      start.setDate(first.getDate() - first.getDay()); // 일요일 시작
      const end = new Date(start);
      end.setDate(start.getDate() + 41); // 6주(42칸)
      return {start, end};
    }

    function getWeekRange(cursor){
      const d = new Date(cursor);
      const start = new Date(d);
      start.setDate(d.getDate() - d.getDay());
      const end = new Date(start);
      end.setDate(start.getDate() + 6);
      return {start, end};
    }

    async function loadEvents(){
      let range;
      if(state.mode === "month") range = getMonthRange(state.cursor);
      else range = getWeekRange(state.cursor);

      const qs = new URLSearchParams({
        start: toYMD(range.start),
        end: toYMD(range.end),
      });
      if(state.business && state.business !== "전체"){
        qs.set("business", state.business);
      }
      const j = await apiGet("/api/events?" + qs.toString());
      state.events = j.items || [];
    }

    function eventsByDate(){
      const map = new Map();
      for(const e of state.events){
        const k = e.event_date;
        if(!map.has(k)) map.set(k, []);
        map.get(k).push(e);
      }
      return map;
    }

    function buildEventLines(e){
      const lines = [];
      // ✅ 빈 값이면 아예 라벨도 숨김
      if(e.course) lines.push(`▪ 과정: ${e.course}`);
      if(e.time) lines.push(`▪ 시간: ${e.time}`);
      if(e.people) lines.push(`▪ 인원: ${e.people}`);
      if(e.place) lines.push(`▪ 장소: ${e.place}`);
      if(e.admin) lines.push(`▪ 행정: ${e.admin}`);
      return lines;
    }

    function renderMonth(){
      $("viewMonth").style.display = "";
      $("viewWeek").style.display = "none";

      $("subtitle").textContent = monthTitle(state.cursor);

      const grid = $("monthGrid");
      grid.innerHTML = "";
      const {start} = getMonthRange(state.cursor);
      const map = eventsByDate();

      for(let i=0;i<42;i++){
        const d = new Date(start);
        d.setDate(start.getDate()+i);
        const ymd = toYMD(d);

        const cell = document.createElement("div");
        cell.className = "cell";

        const dayNum = document.createElement("div");
        dayNum.className = "daynum";
        dayNum.textContent = d.getDate();
        // 일/토 색
        if(d.getDay()===0) dayNum.style.color = "#d50000";
        if(d.getDay()===6) dayNum.style.color = "#0b57d0";
        // 다른 달 흐리게
        if(d.getMonth() !== state.cursor.getMonth()) dayNum.classList.add("muted");
        cell.appendChild(dayNum);

        const eventsWrap = document.createElement("div");
        eventsWrap.className = "events-grid";

        const list = map.get(ymd) || [];
        for(const e of list){
          const card = document.createElement("div");
          card.className = "event";
          card.style.background = businessColor(e.business || "기타");

          const t = document.createElement("div");
          t.className = "title";
          t.textContent = e.business || "(사업명 없음)";
          card.appendChild(t);

          const lines = buildEventLines(e);
          for(const ln of lines){
            const p = document.createElement("div");
            p.className = "line";
            p.textContent = ln;
            card.appendChild(p);
          }

          card.addEventListener("click", ()=>{
            openEditModal(e);
          });

          eventsWrap.appendChild(card);
        }

        cell.appendChild(eventsWrap);
        grid.appendChild(cell);
      }
    }

    function renderWeek(){
      $("viewMonth").style.display = "none";
      $("viewWeek").style.display = "";
      const {start, end} = getWeekRange(state.cursor);

      $("subtitle").textContent = monthTitle(state.cursor) + " (주별)";

      const map = eventsByDate();
      const body = $("weekBody");
      body.innerHTML = "";

      for(let i=0;i<7;i++){
        const d = new Date(start);
        d.setDate(start.getDate()+i);
        const ymd = toYMD(d);

        const row = document.createElement("div");
        row.className = "week-row";

        const left = document.createElement("div");
        left.className = "week-date";
        left.textContent = `${d.getMonth()+1}/${d.getDate()} (${["일","월","화","수","목","금","토"][d.getDay()]})`;
        if(d.getDay()===0) left.style.color="#d50000";
        if(d.getDay()===6) left.style.color="#0b57d0";

        const right = document.createElement("div");
        right.className = "week-events";

        const list = map.get(ymd) || [];
        for(const e of list){
          const card = document.createElement("div");
          card.className = "event";
          card.style.background = businessColor(e.business || "기타");

          const t = document.createElement("div");
          t.className = "title";
          t.textContent = e.business || "(사업명 없음)";
          card.appendChild(t);

          const lines = buildEventLines(e);
          for(const ln of lines){
            const p = document.createElement("div");
            p.className = "line";
            p.textContent = ln;
            card.appendChild(p);
          }

          card.addEventListener("click", ()=> openEditModal(e));
          right.appendChild(card);
        }

        row.appendChild(left);
        row.appendChild(right);
        body.appendChild(row);
      }
    }

    async function refresh(){
      await loadEvents();
      await loadBusinesses();
      if(state.mode==="month") renderMonth();
      else renderWeek();
    }

    // ----------------------------
    // Modal: Add / Edit
    // ----------------------------
    function showModal(show){
      $("modalBackdrop").style.display = show ? "flex" : "none";
    }

    function openAddModal(){
      state.selectedEvent = null;
      $("modalTitle").textContent = "일정 추가";
      $("deleteBtn").style.display = "none";

      const today = new Date(state.cursor);
      const ymd = toYMD(today);

      $("startDate").value = ymd;
      $("endDate").value = ymd;

      $("business").value = "";
      $("course").value = "";
      $("time").value = "";
      $("people").value = "";
      $("place").value = "";
      $("admin").value = "";

      showModal(true);
    }

    function openEditModal(e){
      state.selectedEvent = e;
      $("modalTitle").textContent = `일정 수정 (${e.event_date})`;
      $("deleteBtn").style.display = "";

      // 수정 모드: 날짜는 그 날짜만 대상으로 처리 (기간 개념은 등록용)
      $("startDate").value = e.event_date;
      $("endDate").value = e.event_date;

      $("business").value = e.business || "";
      $("course").value = e.course || "";
      $("time").value = e.time || "";
      $("people").value = e.people || "";
      $("place").value = e.place || "";
      $("admin").value = e.admin || "";

      showModal(true);
    }

    async function onSave(){
      const start = $("startDate").value;
      const end = $("endDate").value || start;

      const payload = {
        start,
        end,
        business: $("business").value.trim(),
        course: $("course").value.trim(),
        time: $("time").value.trim(),
        people: $("people").value.trim(),
        place: $("place").value.trim(),
        admin: $("admin").value.trim(),
      };

      try{
        if(state.selectedEvent){
          // ✅ 개별 수정: event_date는 startDate 한 날짜만 의미
          await apiPut(`/api/events/${state.selectedEvent.id}`, {
            event_date: start,
            business: payload.business,
            course: payload.course,
            time: payload.time,
            people: payload.people,
            place: payload.place,
            admin: payload.admin,
          });
        }else{
          // ✅ 기간 등록: 날짜별로 개별 row 생성
          await apiPost("/api/events", payload);
        }

        showModal(false);
        await refresh();
      }catch(err){
        alert("저장 중 오류가 발생했습니다.\n\n" + (err?.message || err));
      }
    }

    async function onDeleteOneDay(){
      if(!state.selectedEvent) return;
      if(!confirm("이 날짜 일정 1건을 삭제할까요?")) return;
      try{
        await apiDel(`/api/events/${state.selectedEvent.id}`);
        showModal(false);
        await refresh();
      }catch(err){
        alert("삭제 중 오류가 발생했습니다.\n\n" + (err?.message || err));
      }
    }

    // ----------------------------
    // Events
    // ----------------------------
    $("prevBtn").addEventListener("click", async ()=>{
      const d = new Date(state.cursor);
      if(state.mode==="month"){
        d.setMonth(d.getMonth()-1);
      }else{
        d.setDate(d.getDate()-7);
      }
      state.cursor = d;
      await refresh();
    });

    $("nextBtn").addEventListener("click", async ()=>{
      const d = new Date(state.cursor);
      if(state.mode==="month"){
        d.setMonth(d.getMonth()+1);
      }else{
        d.setDate(d.getDate()+7);
      }
      state.cursor = d;
      await refresh();
    });

    $("monthBtn").addEventListener("click", async ()=>{
      state.mode = "month";
      await refresh();
    });

    $("weekBtn").addEventListener("click", async ()=>{
      state.mode = "week";
      await refresh();
    });

    $("businessFilter").addEventListener("change", async (e)=>{
      state.business = e.target.value;
      await refresh();
    });

    $("resetFilterBtn").addEventListener("click", async ()=>{
      state.business = "전체";
      $("businessFilter").value = "전체";
      await refresh();
    });

    $("addBtn").addEventListener("click", openAddModal);
    $("closeModalBtn").addEventListener("click", ()=> showModal(false));
    $("modalBackdrop").addEventListener("click", (e)=>{
      if(e.target === $("modalBackdrop")) showModal(false);
    });

    $("saveBtn").addEventListener("click", onSave);
    $("deleteBtn").addEventListener("click", onDeleteOneDay);

    // init
    (async ()=>{
      // 기본: 현재 달
      state.cursor = new Date();
      await refresh();
    })();
  </script>
</body>
</html>
"""


@app.get("/")
def index():
    return Response(_html(), mimetype="text/html")


# ---- simple health check
@app.get("/health")
def health():
    return jsonify({"ok": True})


# Render/Gunicorn entry
# gunicorn app:app
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
