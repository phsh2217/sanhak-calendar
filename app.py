# app.py
import os
import json
import uuid
from datetime import datetime, date, timedelta
from flask import Flask, request, jsonify, Response
import psycopg2
import psycopg2.extras

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL".lower())

def get_conn():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL 환경변수가 설정되어 있지 않습니다.")
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    conn = get_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # events table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id SERIAL PRIMARY KEY,
                    event_date DATE NOT NULL,
                    business TEXT NOT NULL,
                    course TEXT,
                    time_range TEXT,
                    people TEXT,
                    place TEXT,
                    admin TEXT,
                    memo TEXT,
                    color_key TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_events_business ON events(business);")

            # businesses table
            cur.execute("""
                CREATE TABLE IF NOT EXISTS businesses (
                    id SERIAL PRIMARY KEY,
                    name TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMP DEFAULT NOW()
                );
            """)
            # seed default business if empty
            cur.execute("SELECT COUNT(*) FROM businesses;")
            cnt = cur.fetchone()[0]
            if cnt == 0:
                cur.execute("INSERT INTO businesses(name) VALUES (%s) ON CONFLICT DO NOTHING;", ("전체",))
    finally:
        conn.close()

init_db()

def parse_date(s: str):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return None

def clean_str(v):
    if v is None:
        return None
    v = str(v).strip()
    return v if v != "" else None

def event_row_to_dict(r):
    return {
        "id": r["id"],
        "event_date": r["event_date"].strftime("%Y-%m-%d"),
        "business": r["business"],
        "course": r["course"],
        "time": r["time_range"],
        "people": r["people"],
        "place": r["place"],
        "admin": r["admin"],
        "memo": r["memo"],
        "color_key": r["color_key"],
    }

@app.get("/api/businesses")
def api_businesses():
    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SELECT name FROM businesses ORDER BY name;")
            rows = cur.fetchall()
        names = [r["name"] for r in rows if r["name"]]
        # ensure "전체" appears first in UI dropdown
        if "전체" not in names:
            names.insert(0, "전체")
        else:
            names = ["전체"] + [x for x in names if x != "전체"]
        return jsonify({"ok": True, "businesses": names})
    finally:
        conn.close()

@app.post("/api/businesses")
def api_add_business():
    data = request.get_json(force=True, silent=True) or {}
    name = clean_str(data.get("name"))
    if not name:
        return jsonify({"ok": False, "error": "사업명을 입력하세요."}), 400
    conn = get_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO businesses(name) VALUES (%s) ON CONFLICT (name) DO NOTHING;", (name,))
        return jsonify({"ok": True})
    finally:
        conn.close()

@app.get("/api/events")
def api_list_events():
    # optional range filtering
    start = parse_date(request.args.get("start"))
    end = parse_date(request.args.get("end"))
    business = request.args.get("business")

    conn = get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            q = "SELECT * FROM events WHERE 1=1"
            params = []
            if start:
                q += " AND event_date >= %s"
                params.append(start)
            if end:
                q += " AND event_date <= %s"
                params.append(end)
            if business and business != "전체":
                q += " AND business = %s"
                params.append(business)
            q += " ORDER BY event_date ASC, id ASC;"
            cur.execute(q, params)
            rows = cur.fetchall()
        return jsonify({"ok": True, "events": [event_row_to_dict(r) for r in rows]})
    finally:
        conn.close()

@app.post("/api/events")
def api_add_events_range():
    """
    기간 등록:
    start, end (YYYY-MM-DD)
    business (required)
    course, time, people, place, admin, memo, color_key (optional)
    excluded_dates: "YYYY-MM-DD,YYYY-MM-DD" (optional)
    """
    data = request.get_json(force=True, silent=True) or {}
    start_s = clean_str(data.get("start"))
    end_s = clean_str(data.get("end"))
    business = clean_str(data.get("business"))
    if not start_s or not end_s:
        return jsonify({"ok": False, "error": "시작/종료일은 YYYY-MM-DD 형식으로 입력하세요."}), 400
    start_d = parse_date(start_s)
    end_d = parse_date(end_s)
    if not start_d or not end_d:
        return jsonify({"ok": False, "error": "시작/종료일은 YYYY-MM-DD 형식으로 입력하세요."}), 400
    if end_d < start_d:
        return jsonify({"ok": False, "error": "종료일은 시작일보다 빠를 수 없습니다."}), 400
    if not business:
        return jsonify({"ok": False, "error": "사업명은 필수입니다."}), 400

    excluded_raw = clean_str(data.get("excluded_dates")) or ""
    excluded = set()
    if excluded_raw:
        for part in excluded_raw.split(","):
            d = parse_date(part.strip())
            if d:
                excluded.add(d)

    course = clean_str(data.get("course"))
    time_range = clean_str(data.get("time"))
    people = clean_str(data.get("people"))
    place = clean_str(data.get("place"))
    admin = clean_str(data.get("admin"))
    memo = clean_str(data.get("memo"))
    color_key = clean_str(data.get("color_key"))

    # insert each day separately
    conn = get_conn()
    conn.autocommit = True
    inserted = 0
    try:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO businesses(name) VALUES (%s) ON CONFLICT (name) DO NOTHING;", (business,))
            d = start_d
            while d <= end_d:
                if d not in excluded:
                    cur.execute("""
                        INSERT INTO events(event_date,business,course,time_range,people,place,admin,memo,color_key)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """, (d, business, course, time_range, people, place, admin, memo, color_key))
                    inserted += 1
                d += timedelta(days=1)
        return jsonify({"ok": True, "inserted": inserted})
    finally:
        conn.close()

@app.patch("/api/events/<int:event_id>")
def api_update_event(event_id: int):
    """
    특정 날짜(개별 이벤트) 수정
    """
    data = request.get_json(force=True, silent=True) or {}
    # allow only specific fields
    business = clean_str(data.get("business"))
    course = clean_str(data.get("course"))
    time_range = clean_str(data.get("time"))
    people = clean_str(data.get("people"))
    place = clean_str(data.get("place"))
    admin = clean_str(data.get("admin"))
    memo = clean_str(data.get("memo"))
    color_key = clean_str(data.get("color_key"))

    conn = get_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            # if business changed, ensure businesses table contains it
            if business:
                cur.execute("INSERT INTO businesses(name) VALUES (%s) ON CONFLICT (name) DO NOTHING;", (business,))
            cur.execute("""
                UPDATE events
                SET business = COALESCE(%s, business),
                    course = %s,
                    time_range = %s,
                    people = %s,
                    place = %s,
                    admin = %s,
                    memo = %s,
                    color_key = %s
                WHERE id = %s
            """, (business, course, time_range, people, place, admin, memo, color_key, event_id))
        return jsonify({"ok": True})
    finally:
        conn.close()

@app.delete("/api/events/<int:event_id>")
def api_delete_event(event_id: int):
    conn = get_conn()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM events WHERE id = %s;", (event_id,))
        return jsonify({"ok": True})
    finally:
        conn.close()

def _html():
    return r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"/>
  <title>포항산학 월별일정</title>
  <style>
    :root{
      --bg:#f6f7fb;
      --card:#ffffff;
      --line:#dfe3ea;
      --text:#111827;
      --muted:#6b7280;
      --primary:#2563eb;
      --danger:#dc2626;
      --shadow:0 6px 22px rgba(0,0,0,.06);
      --radius:16px;
    }
    *{box-sizing:border-box}
    body{margin:0;font-family:system-ui,-apple-system,Segoe UI,Roboto,Apple SD Gothic Neo,Noto Sans KR,Arial,sans-serif;background:var(--bg);color:var(--text)}
    .wrap{max-width:2200px;margin:0 auto;padding:14px}
    @media (min-width: 1800px){.wrap{max-width:2400px;}}

    .top{
      display:flex;flex-direction:column;gap:10px;
      align-items:center;justify-content:center;
      padding:12px 10px 6px;
    }
    h1{margin:0;font-size:44px;letter-spacing:-1px}
    h2{margin:0;font-size:30px;font-weight:900}
    .controls{
      width:100%;
      display:flex;flex-wrap:wrap;gap:10px;
      align-items:center;justify-content:center;
      padding:10px 0 6px;
    }
    button, select, input{
      font:inherit;
      border:1px solid var(--line);
      background:#fff;
      border-radius:10px;
      padding:10px 12px;
      min-height:44px;
    }
    button{cursor:pointer;font-weight:800}
    button.primary{background:var(--primary);color:white;border-color:var(--primary)}
    button.ghost{background:#fff}
    button:active{transform:translateY(1px)}
    select{min-width:120px}
    .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;justify-content:center}

    .panel{
      background:var(--card);
      border:1px solid var(--line);
      border-radius:var(--radius);
      box-shadow:var(--shadow);
      overflow:hidden;
    }

    table{width:100%;border-collapse:collapse;table-layout:fixed}
    th,td{border:1px solid var(--line);vertical-align:top;background:#fff}
    th{padding:10px 6px;font-size:15px;background:#fbfbfd}
    .sun{color:#dc2626}
    .sat{color:#2563eb}
    .cell{
      position:relative;
      height:140px;
      padding:6px;
    }
    .date{
      font-weight:900;
      font-size:14px;
      position:absolute;
      top:6px;left:8px;
      color:#111827;
    }
    .date.muted{color:#c0c4cc}
    .events{
      margin-top:22px;
      display:grid;
      grid-template-columns:repeat(2, minmax(0,1fr));
      gap:8px;
      align-content:start;
    }
    .event-card{
      border:1px solid var(--line);
      border-radius:14px;
      padding:10px;
      background:#fff;
      box-shadow:0 2px 10px rgba(0,0,0,.04);
      cursor:pointer;
      user-select:none;
      overflow:hidden;
      min-height:64px;
    }
    .event-card:hover{border-color:#c9d1ff}
    .event-title{font-weight:900;font-size:15px;line-height:1.2;margin-bottom:6px}
    .kv{font-size:13px;line-height:1.25;color:#111827}
    .kv .k{color:var(--muted);font-weight:800;margin-right:6px}
    .muted{color:var(--muted);font-weight:700}
    .chip{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--line);background:#fff;border-radius:999px;padding:6px 10px;font-weight:900}
    .chip small{font-weight:800;color:var(--muted)}

    /* business color classes */
    .biz-a{background:#fce7f3}
    .biz-b{background:#e0f2fe}
    .biz-c{background:#dcfce7}
    .biz-d{background:#fef9c3}
    .biz-e{background:#ede9fe}

    /* Week list layout (PC: 3 cards/row) */
    .week-td{padding:12px;background:transparent;}
    .week-list{display:flex;flex-direction:column;gap:14px;}
    .week-day{border:1px solid var(--line);border-radius:16px;background:#fff;padding:12px;}
    .week-day-head{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;gap:10px;}
    .week-day-title{font-weight:900;}
    .week-day-sub{color:var(--muted);font-size:12px;}
    .week-cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;}
    @media (max-width: 1200px){.week-cards{grid-template-columns:repeat(2,minmax(0,1fr));}}
    @media (max-width: 700px){.week-cards{grid-template-columns:1fr;}}

    /* Modal */
    .backdrop{
      position:fixed;inset:0;background:rgba(0,0,0,.45);
      display:none;align-items:center;justify-content:center;
      padding:14px;z-index:50;
    }
    .modal{
      width:min(760px, 100%);
      background:#fff;border-radius:18px;border:1px solid var(--line);
      box-shadow:0 18px 50px rgba(0,0,0,.25);
      overflow:hidden;
    }
    .modal-head{
      padding:14px 16px;border-bottom:1px solid var(--line);
      display:flex;justify-content:space-between;align-items:center;gap:10px;
    }
    .modal-title{font-weight:1000}
    .modal-body{padding:14px 16px;display:flex;flex-direction:column;gap:10px}
    .grid2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
    .grid1{display:grid;grid-template-columns:1fr;gap:10px}
    label{font-weight:900;font-size:13px;color:#374151}
    .field{display:flex;flex-direction:column;gap:6px}
    textarea{min-height:86px;resize:vertical}
    .modal-foot{
      padding:12px 16px;border-top:1px solid var(--line);
      display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap;
    }
    button.danger{background:var(--danger);color:#fff;border-color:var(--danger)}
    .hint{font-size:12px;color:var(--muted);font-weight:700}

    /* Mobile tuning */
    @media (max-width: 820px){
      .wrap{padding:10px}
      h1{font-size:34px}
      h2{font-size:22px}
      .cell{height:122px}
      .events{grid-template-columns:1fr}
      .event-title{font-size:14px}
      .kv{font-size:12px}
      .grid2{grid-template-columns:1fr}
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="top">
      <h1>포항산학 월별일정</h1>
      <h2 id="currentMonth">-</h2>

      <div class="controls">
        <div class="row">
          <button id="prevBtn" class="ghost">◀ 이전</button>
          <button id="nextBtn" class="ghost">다음 ▶</button>
          <button id="monthViewBtn" class="ghost">월별</button>
          <button id="weekViewBtn" class="ghost">주별</button>
        </div>

        <div class="row">
          <span style="font-weight:1000;font-size:20px">사업명:</span>
          <select id="businessFilter"></select>
          <button id="resetFilterBtn" class="ghost">필터 초기화</button>
          <button id="openAddBtn" class="primary">+ 일정 추가하기</button>
        </div>
      </div>
    </div>

    <div class="panel">
      <table>
        <thead>
          <tr>
            <th class="sun">일</th>
            <th>월</th>
            <th>화</th>
            <th>수</th>
            <th>목</th>
            <th>금</th>
            <th class="sat">토</th>
          </tr>
        </thead>
        <tbody id="calendarBody"></tbody>
      </table>
    </div>
  </div>

  <!-- Add Modal (range) -->
  <div id="addBackdrop" class="backdrop">
    <div class="modal">
      <div class="modal-head">
        <div class="modal-title">일정 추가(기간 등록)</div>
        <button id="addCloseBtn" class="ghost">✕</button>
      </div>
      <div class="modal-body">
        <div class="grid2">
          <div class="field">
            <label>시작일</label>
            <input id="addStart" type="date" />
          </div>
          <div class="field">
            <label>종료일</label>
            <input id="addEnd" type="date" />
          </div>
        </div>

        <div class="field">
          <label>사업명</label>
          <input id="addBusiness" placeholder="예: 청년 일경험" />
          <div class="hint">※ 신규 사업명은 입력하면 자동으로 목록에 추가됩니다.</div>
        </div>

        <div class="field">
          <label>과정</label>
          <input id="addCourse" placeholder="예: 멀티캠퍼스" />
        </div>

        <div class="grid2">
          <div class="field">
            <label>시간</label>
            <input id="addTime" placeholder="예: 10:00~14:00" />
          </div>
          <div class="field">
            <label>인원</label>
            <input id="addPeople" placeholder="예: 10" />
          </div>
        </div>

        <div class="grid2">
          <div class="field">
            <label>장소</label>
            <input id="addPlace" placeholder="예: 본관 3층" />
          </div>
          <div class="field">
            <label>행정</label>
            <input id="addAdmin" placeholder="예: 담당자명" />
          </div>
        </div>

        <div class="field">
          <label>메모</label>
          <textarea id="addMemo" placeholder="추가 메모(선택)"></textarea>
        </div>

        <div class="field">
          <label>제외할 날짜(선택)</label>
          <input id="addExcluded" placeholder="예: 2026-01-07,2026-01-08" />
          <div class="hint">기간 중 특정 날짜만 빼고 저장하고 싶을 때</div>
        </div>
      </div>
      <div class="modal-foot">
        <button id="addSaveBtn" class="primary">저장</button>
        <button id="addCancelBtn" class="ghost">닫기</button>
      </div>
    </div>
  </div>

  <!-- Edit Modal (single day) -->
  <div id="editBackdrop" class="backdrop">
    <div class="modal">
      <div class="modal-head">
        <div class="modal-title" id="editTitle">일정</div>
        <button id="editCloseBtn" class="ghost">✕</button>
      </div>
      <div class="modal-body">
        <div class="field">
          <label>날짜</label>
          <input id="editDate" disabled />
        </div>
        <div class="field">
          <label>사업명</label>
          <input id="editBusiness" />
        </div>
        <div class="field">
          <label>과정</label>
          <input id="editCourse" />
        </div>
        <div class="grid2">
          <div class="field">
            <label>시간</label>
            <input id="editTime" />
          </div>
          <div class="field">
            <label>인원</label>
            <input id="editPeople" />
          </div>
        </div>
        <div class="grid2">
          <div class="field">
            <label>장소</label>
            <input id="editPlace" />
          </div>
          <div class="field">
            <label>행정</label>
            <input id="editAdmin" />
          </div>
        </div>
        <div class="field">
          <label>메모</label>
          <textarea id="editMemo"></textarea>
        </div>
        <div class="hint">※ 이 화면은 “선택한 날짜(해당 1건)”만 수정/삭제합니다.</div>
      </div>
      <div class="modal-foot">
        <button id="editDeleteBtn" class="danger">이날 삭제</button>
        <button id="editSaveBtn" class="primary">저장</button>
        <button id="editCancelBtn" class="ghost">닫기</button>
      </div>
    </div>
  </div>

  <script>
    let events = [];
    let businesses = [];
    let viewMode = "month"; // month | week
    let anchorDate = new Date(); // used for month/week navigation

    function pad(n){ return String(n).padStart(2,"0"); }
    function formatISO(d){
      return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
    }
    function startOfWeek(d){
      const x = new Date(d);
      const day = x.getDay(); // 0 Sun
      x.setDate(x.getDate() - day);
      x.setHours(0,0,0,0);
      return x;
    }
    function startOfMonth(d){
      return new Date(d.getFullYear(), d.getMonth(), 1);
    }
    function endOfMonth(d){
      return new Date(d.getFullYear(), d.getMonth()+1, 0);
    }

    function getRangeDates(startISO, endISO, excludedCSV){
      const s = new Date(startISO + "T00:00:00");
      const e = new Date(endISO + "T00:00:00");
      const excluded = new Set();
      if(excludedCSV){
        excludedCSV.split(",").map(x=>x.trim()).filter(Boolean).forEach(x=>excluded.add(x));
      }
      const out = [];
      const d = new Date(s);
      while(d <= e){
        const iso = formatISO(d);
        if(!excluded.has(iso)) out.push(iso);
        d.setDate(d.getDate()+1);
      }
      return out;
    }

    function getBusinessClass(name){
      // stable hash to one of 5 classes
      let h=0;
      for(let i=0;i<name.length;i++) h = (h*31 + name.charCodeAt(i)) >>> 0;
      const idx = h % 5;
      return ["biz-a","biz-b","biz-c","biz-d","biz-e"][idx];
    }

    function buildCardHTML(ev){
      // 입력 안한 항목은 표시 안되게
      const lines = [];
      if(ev.course) lines.push(`<div class="kv"><span class="k">· 과정</span>${escapeHTML(ev.course)}</div>`);
      if(ev.time) lines.push(`<div class="kv"><span class="k">· 시간</span>${escapeHTML(ev.time)}</div>`);
      if(ev.people) lines.push(`<div class="kv"><span class="k">· 인원</span>${escapeHTML(ev.people)}</div>`);
      if(ev.place) lines.push(`<div class="kv"><span class="k">· 장소</span>${escapeHTML(ev.place)}</div>`);
      if(ev.admin) lines.push(`<div class="kv"><span class="k">· 행정</span>${escapeHTML(ev.admin)}</div>`);
      if(ev.memo) lines.push(`<div class="kv"><span class="k">· 메모</span>${escapeHTML(ev.memo)}</div>`);
      return `
        <div class="event-title">${escapeHTML(ev.business)}</div>
        ${lines.join("")}
      `;
    }

    function escapeHTML(s){
      return String(s).replace(/[&<>"']/g, (m)=>({
        "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"
      }[m]));
    }

    async function loadBusinesses(){
      const r = await fetch("/api/businesses");
      const j = await r.json();
      if(!j.ok) throw new Error(j.error || "사업명 로드 실패");
      businesses = j.businesses;
      const sel = document.getElementById("businessFilter");
      sel.innerHTML = "";
      businesses.forEach(b=>{
        const opt = document.createElement("option");
        opt.value = b;
        opt.textContent = b;
        sel.appendChild(opt);
      });
      sel.value = "전체";
    }

    async function loadEvents(){
      const r = await fetch("/api/events");
      const j = await r.json();
      if(!j.ok) throw new Error(j.error || "이벤트 로드 실패");
      events = j.events;
    }

    function render(){
      if(viewMode === "month") renderMonth();
      else renderWeek();
    }

    function renderMonth(){
      const body = document.getElementById("calendarBody");
      body.innerHTML = "";

      const mStart = startOfMonth(anchorDate);
      const mEnd = endOfMonth(anchorDate);

      document.getElementById("currentMonth").textContent =
        `${mStart.getFullYear()}년 ${mStart.getMonth()+1}월`;

      const filter = document.getElementById("businessFilter").value || "전체";

      // calendar starts at sunday of week containing 1st
      const first = new Date(mStart);
      const start = startOfWeek(first);
      // calendar ends at saturday of week containing last day
      const last = new Date(mEnd);
      const end = new Date(startOfWeek(last));
      end.setDate(end.getDate()+6);

      const d = new Date(start);
      while(d <= end){
        const tr = document.createElement("tr");
        for(let i=0;i<7;i++){
          const td = document.createElement("td");
          td.className = "cell";
          const iso = formatISO(d);

          const inMonth = (d.getMonth() === mStart.getMonth());
          const dateDiv = document.createElement("div");
          dateDiv.className = "date" + (inMonth ? "" : " muted");
          dateDiv.textContent = d.getDate();
          td.appendChild(dateDiv);

          const evWrap = document.createElement("div");
          evWrap.className = "events";

          const dayEvents = events.filter(ev=>{
            if(ev.event_date !== iso) return false;
            if(filter !== "전체" && (ev.business||"") !== filter) return false;
            return true;
          });

          dayEvents.forEach(ev=>{
            const card = document.createElement("div");
            card.className = "event-card " + getBusinessClass(ev.business || "");
            card.addEventListener("click", ()=>openEditModal(ev, iso));
            card.innerHTML = buildCardHTML(ev);
            evWrap.appendChild(card);
          });

          td.appendChild(evWrap);
          tr.appendChild(td);
          d.setDate(d.getDate()+1);
        }
        body.appendChild(tr);
      }
    }

    function renderWeek(){
      const body = document.getElementById("calendarBody");
      body.innerHTML = "";

      const ws = startOfWeek(anchorDate);
      const we = new Date(ws); we.setDate(we.getDate()+6);

      const y = ws.getFullYear();
      const m = ws.getMonth()+1;
      document.getElementById("currentMonth").textContent =
        `${y}년 ${m}월 (주별: ${formatISO(ws)} ~ ${formatISO(we)})`;

      const filter = document.getElementById("businessFilter").value || "전체";
      const weekday = ["일","월","화","수","목","금","토"];

      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 7;
      td.className = "week-td";

      const wrap = document.createElement("div");
      wrap.className = "week-list";

      for(let i=0;i<7;i++){
        const d = new Date(ws); d.setDate(ws.getDate()+i);
        const iso = formatISO(d);

        const dayEvents = events.filter(ev=>{
          if(ev.event_date !== iso) return false;
          if(filter !== "전체" && (ev.business||"") !== filter) return false;
          return true;
        });

        const section = document.createElement("section");
        section.className = "week-day";

        const head = document.createElement("div");
        head.className = "week-day-head";

        const left = document.createElement("div");
        left.innerHTML = `<div class="week-day-title">${iso} (${weekday[d.getDay()]})</div>
                          <div class="week-day-sub">일정 ${dayEvents.length}건</div>`;

        head.appendChild(left);

        const cards = document.createElement("div");
        cards.className = "week-cards";

        if(dayEvents.length === 0){
          const empty = document.createElement("div");
          empty.className = "muted";
          empty.style.padding = "6px 2px";
          empty.textContent = "등록된 일정 없음";
          section.appendChild(head);
          section.appendChild(empty);
          wrap.appendChild(section);
          continue;
        }

        dayEvents.forEach(ev=>{
          const card = document.createElement("div");
          card.className = "event-card " + getBusinessClass(ev.business || "");
          card.addEventListener("click", ()=>openEditModal(ev, iso));
          card.innerHTML = buildCardHTML(ev);
          cards.appendChild(card);
        });

        section.appendChild(head);
        section.appendChild(cards);
        wrap.appendChild(section);
      }

      td.appendChild(wrap);
      tr.appendChild(td);
      body.appendChild(tr);
    }

    // navigation
    document.getElementById("prevBtn").addEventListener("click", ()=>{
      if(viewMode === "month"){
        anchorDate = new Date(anchorDate.getFullYear(), anchorDate.getMonth()-1, 1);
      }else{
        anchorDate = new Date(anchorDate);
        anchorDate.setDate(anchorDate.getDate()-7);
      }
      render();
    });
    document.getElementById("nextBtn").addEventListener("click", ()=>{
      if(viewMode === "month"){
        anchorDate = new Date(anchorDate.getFullYear(), anchorDate.getMonth()+1, 1);
      }else{
        anchorDate = new Date(anchorDate);
        anchorDate.setDate(anchorDate.getDate()+7);
      }
      render();
    });

    document.getElementById("monthViewBtn").addEventListener("click", ()=>{
      viewMode = "month";
      render();
    });
    document.getElementById("weekViewBtn").addEventListener("click", ()=>{
      viewMode = "week";
      render();
    });

    document.getElementById("businessFilter").addEventListener("change", ()=>{
      render();
    });
    document.getElementById("resetFilterBtn").addEventListener("click", ()=>{
      document.getElementById("businessFilter").value = "전체";
      render();
    });

    // add modal
    const addBackdrop = document.getElementById("addBackdrop");
    const editBackdrop = document.getElementById("editBackdrop");

    function openAddModal(){
      addBackdrop.style.display = "flex";
      // default dates = today
      const today = new Date();
      document.getElementById("addStart").value = formatISO(today);
      document.getElementById("addEnd").value = formatISO(today);
      document.getElementById("addBusiness").value = "";
      document.getElementById("addCourse").value = "";
      document.getElementById("addTime").value = "";
      document.getElementById("addPeople").value = "";
      document.getElementById("addPlace").value = "";
      document.getElementById("addAdmin").value = "";
      document.getElementById("addMemo").value = "";
      document.getElementById("addExcluded").value = "";
    }
    function closeAddModal(){ addBackdrop.style.display = "none"; }
    document.getElementById("openAddBtn").addEventListener("click", openAddModal);
    document.getElementById("addCloseBtn").addEventListener("click", closeAddModal);
    document.getElementById("addCancelBtn").addEventListener("click", closeAddModal);
    addBackdrop.addEventListener("click", (e)=>{ if(e.target===addBackdrop) closeAddModal(); });

    document.getElementById("addSaveBtn").addEventListener("click", async ()=>{
      const start = document.getElementById("addStart").value;
      const end = document.getElementById("addEnd").value;
      const business = document.getElementById("addBusiness").value.trim();
      const course = document.getElementById("addCourse").value.trim();
      const time = document.getElementById("addTime").value.trim();
      const people = document.getElementById("addPeople").value.trim();
      const place = document.getElementById("addPlace").value.trim();
      const admin = document.getElementById("addAdmin").value.trim();
      const memo = document.getElementById("addMemo").value.trim();
      const excluded_dates = document.getElementById("addExcluded").value.trim();

      if(!start || !end){
        alert("시작/종료일은 YYYY-MM-DD 형식으로 입력하세요.");
        return;
      }
      if(!business){
        alert("사업명은 필수입니다.");
        return;
      }

      try{
        // ensure business exists
        await fetch("/api/businesses", {
          method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({name: business})
        });

        const r = await fetch("/api/events",{
          method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({
            start, end, business,
            course: course || null,
            time: time || null,
            people: people || null,
            place: place || null,
            admin: admin || null,
            memo: memo || null,
            excluded_dates: excluded_dates || null
          })
        });
        const j = await r.json();
        if(!j.ok){
          alert("저장 중 오류가 발생했습니다.\n\n" + (j.error||""));
          return;
        }
        await loadBusinesses();
        await loadEvents();
        closeAddModal();
        render();
      }catch(err){
        alert("저장 중 오류가 발생했습니다.\n\n" + err);
      }
    });

    // edit modal
    let editingEventId = null;
    let editingDateISO = null;

    function openEditModal(ev, dateISO){
      editingEventId = ev.id;
      editingDateISO = dateISO;

      document.getElementById("editTitle").textContent = `일정 (${dateISO})`;
      document.getElementById("editDate").value = dateISO;
      document.getElementById("editBusiness").value = ev.business || "";
      document.getElementById("editCourse").value = ev.course || "";
      document.getElementById("editTime").value = ev.time || "";
      document.getElementById("editPeople").value = ev.people || "";
      document.getElementById("editPlace").value = ev.place || "";
      document.getElementById("editAdmin").value = ev.admin || "";
      document.getElementById("editMemo").value = ev.memo || "";

      editBackdrop.style.display = "flex";
    }
    function closeEditModal(){
      editBackdrop.style.display = "none";
      editingEventId = null;
      editingDateISO = null;
    }
    document.getElementById("editCloseBtn").addEventListener("click", closeEditModal);
    document.getElementById("editCancelBtn").addEventListener("click", closeEditModal);
    editBackdrop.addEventListener("click", (e)=>{ if(e.target===editBackdrop) closeEditModal(); });

    document.getElementById("editSaveBtn").addEventListener("click", async ()=>{
      if(!editingEventId) return;
      const payload = {
        business: document.getElementById("editBusiness").value.trim() || null,
        course: document.getElementById("editCourse").value.trim() || null,
        time: document.getElementById("editTime").value.trim() || null,
        people: document.getElementById("editPeople").value.trim() || null,
        place: document.getElementById("editPlace").value.trim() || null,
        admin: document.getElementById("editAdmin").value.trim() || null,
        memo: document.getElementById("editMemo").value.trim() || null
      };
      if(!payload.business){
        alert("사업명은 필수입니다.");
        return;
      }

      try{
        await fetch("/api/businesses", {
          method:"POST",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify({name: payload.business})
        });

        const r = await fetch(`/api/events/${editingEventId}`,{
          method:"PATCH",
          headers:{"Content-Type":"application/json"},
          body:JSON.stringify(payload)
        });
        const j = await r.json();
        if(!j.ok){
          alert("수정 중 오류\n\n" + (j.error||""));
          return;
        }
        await loadBusinesses();
        await loadEvents();
        closeEditModal();
        render();
      }catch(err){
        alert("수정 중 오류\n\n" + err);
      }
    });

    document.getElementById("editDeleteBtn").addEventListener("click", async ()=>{
      if(!editingEventId) return;
      if(!confirm("선택한 날짜의 일정 1건을 삭제할까요?")) return;
      try{
        const r = await fetch(`/api/events/${editingEventId}`, {method:"DELETE"});
        const j = await r.json();
        if(!j.ok){
          alert("삭제 중 오류\n\n" + (j.error||""));
          return;
        }
        await loadEvents();
        closeEditModal();
        render();
      }catch(err){
        alert("삭제 중 오류\n\n" + err);
      }
    });

    // boot
    (async function(){
      try{
        await loadBusinesses();
        await loadEvents();
        render();
      }catch(err){
        alert("초기 로드 오류: " + err);
      }
    })();
  </script>
</body>
</html>
"""

@app.get("/")
def index():
    return Response(_html(), mimetype="text/html")

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
