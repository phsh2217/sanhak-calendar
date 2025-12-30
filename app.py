import os
from datetime import datetime, timedelta
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL 환경변수가 설정되어 있지 않습니다.")


# =========================
# DB
# =========================
def get_db():
    # 연결 오류가 나면 아래처럼 sslmode=require 추가 고려:
    # return psycopg2.connect(DATABASE_URL, sslmode="require")
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            start TEXT NOT NULL,
            "end" TEXT NOT NULL,
            business TEXT,
            course TEXT,
            time TEXT,
            people TEXT,
            place TEXT,
            admin TEXT,
            excluded_dates TEXT
        );
        """
    )
    conn.commit()
    cur.close()
    conn.close()


init_db()


def row_to_event(row):
    return {
        "id": row["id"],
        "start": row["start"],
        "end": row["end"],
        "business": row["business"],
        "course": row["course"],
        "time": row["time"],
        "people": row["people"],
        "place": row["place"],
        "admin": row["admin"],
        "excluded_dates": row["excluded_dates"] or "",
    }


def normalize_excluded(excluded_str: str) -> list[str]:
    if not excluded_str:
        return []
    items = [s.strip() for s in excluded_str.split(",") if s.strip()]
    return sorted(set(items))


def excluded_to_str(excluded_list: list[str]) -> str:
    return ",".join(excluded_list)


def parse_ymd(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d")


def daterange(start_s: str, end_s: str):
    """yield YYYY-MM-DD for each day from start to end inclusive"""
    cur = parse_ymd(start_s)
    end = parse_ymd(end_s)
    while cur <= end:
        yield cur.strftime("%Y-%m-%d")
        cur += timedelta(days=1)


# =========================
# API
# =========================
@app.route("/api/events", methods=["GET"])
def api_get_events():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(
        """
        SELECT id, start, "end", business, course, time, people, place, admin, excluded_dates
        FROM events
        ORDER BY start, id;
        """
    )
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return jsonify([row_to_event(r) for r in rows])


@app.route("/api/events", methods=["POST"])
def api_create_event():
    """
    ✅ 기간 입력을 DB에 '날짜별 개별 일정'으로 저장
    - start == end : 1건 생성
    - start <  end : 기간을 날짜별로 쪼개서 N건 생성 (각 건은 start=end=해당 날짜)
    응답:
      - 1건 생성이면 객체 1개
      - N건 생성이면 배열 N개
    """
    data = request.json or {}

    start = (data.get("start") or "").strip()
    end = (data.get("end") or "").strip()
    if not start or not end:
        return jsonify({"error": "start and end are required"}), 400

    business = data.get("business")
    course = data.get("course")
    time_ = data.get("time")
    people = data.get("people")
    place = data.get("place")
    admin = data.get("admin")

    # 새로 생성되는 날짜별 일정은 excluded_dates 사용하지 않음(호환 위해 컬럼은 유지)
    excluded_dates = ""

    # 날짜 검증
    try:
        sdt = parse_ymd(start)
        edt = parse_ymd(end)
    except Exception:
        return jsonify({"error": "date format must be YYYY-MM-DD"}), 400

    if edt < sdt:
        return jsonify({"error": "end must be >= start"}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    created = []

    # 기간이면 날짜별로 분할 저장
    for d in daterange(start, end):
        cur.execute(
            """
            INSERT INTO events (start, "end", business, course, time, people, place, admin, excluded_dates)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id, start, "end", business, course, time, people, place, admin, excluded_dates;
            """,
            (d, d, business, course, time_, people, place, admin, excluded_dates),
        )
        created.append(row_to_event(cur.fetchone()))

    conn.commit()
    cur.close()
    conn.close()

    if len(created) == 1:
        return jsonify(created[0]), 201
    return jsonify(created), 201


@app.route("/api/events/<int:event_id>", methods=["PUT"])
def api_update_event(event_id):
    """
    ✅ 이제 대부분 '하루짜리(start=end)' 일정이므로,
    하루만 수정하는 목적에 잘 맞음.
    """
    data = request.json or {}

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(
        """
        UPDATE events
        SET start=%s,
            "end"=%s,
            business=%s,
            course=%s,
            time=%s,
            people=%s,
            place=%s,
            admin=%s,
            excluded_dates=%s
        WHERE id=%s
        RETURNING id, start, "end", business, course, time, people, place, admin, excluded_dates;
        """,
        (
            (data.get("start") or "").strip(),
            (data.get("end") or "").strip(),
            data.get("business"),
            data.get("course"),
            data.get("time"),
            data.get("people"),
            data.get("place"),
            data.get("admin"),
            data.get("excluded_dates") or "",
            event_id,
        ),
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify(row_to_event(row))


@app.route("/api/events/<int:event_id>", methods=["DELETE"])
def api_delete_event(event_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM events WHERE id=%s;", (event_id,))
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    if deleted == 0:
        return jsonify({"error": "not found"}), 404
    return jsonify({"status": "deleted"})


# (호환 유지) 예전 '기간 1건' 이벤트가 남아 있을 수 있으니 exclude는 유지
@app.route("/api/events/<int:event_id>/exclude", methods=["POST"])
def api_exclude_one_day(event_id):
    data = request.json or {}
    date_str = (data.get("date") or "").strip()
    if not date_str:
        return jsonify({"error": "date is required"}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cur.execute(
        """
        SELECT id, start, "end", business, course, time, people, place, admin, excluded_dates
        FROM events
        WHERE id=%s;
        """,
        (event_id,),
    )
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "not found"}), 404

    excluded_list = normalize_excluded(row["excluded_dates"] or "")
    excluded_list.append(date_str)
    excluded_list = sorted(set(excluded_list))
    new_excluded = excluded_to_str(excluded_list)

    cur.execute(
        """
        UPDATE events
        SET excluded_dates=%s
        WHERE id=%s
        RETURNING id, start, "end", business, course, time, people, place, admin, excluded_dates;
        """,
        (new_excluded, event_id),
    )
    updated = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(row_to_event(updated))


# =========================
# UI (Single-file)
# =========================
INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8" />
  <title>포항산학 월별일정</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <style>
    :root{
      --bg:#fff;
      --border:#ddd;
      --sun:#e74c3c;
      --sat:#2980b9;
      --text:#222;
      --btn-bg:#f5f5f5;
      --btn-border:#ccc;
    }
    *{box-sizing:border-box;}
    body{margin:0;padding:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:var(--bg);color:var(--text);}

    .container{
      max-width:1600px;
      margin:0 auto;
      padding:10px 10px;
    }

    h1{text-align:center;margin:10px 0 4px;font-size:28px;}
    .current-month{text-align:center;font-size:20px;margin-bottom:10px;}

    .top-bar{
      display:flex;flex-wrap:wrap;justify-content:space-between;gap:8px;align-items:center;margin-bottom:8px;
    }
    .top-left,.top-right{display:flex;flex-wrap:wrap;gap:6px;align-items:center;}
    button,select,input{font-size:14px;}
    button{
      padding:6px 12px;border-radius:4px;border:1px solid var(--btn-border);background:var(--btn-bg);cursor:pointer;
    }
    button:hover{background:#eee;}
    select{padding:4px 8px;}

    .calendar{
      width:100%;
      border-collapse:collapse;
      table-layout:fixed;
    }
    .calendar th,.calendar td{border:1px solid var(--border);vertical-align:top;}
    .calendar thead th{padding:6px 0;text-align:center;background:#fafafa;font-weight:600;}
    .calendar tbody td{
      height:125px;
      padding:4px;
      position:relative;
      overflow:hidden;
    }

    .day-number{font-size:13px;font-weight:600;}
    .sun{color:var(--sun);}
    .sat{color:var(--sat);}

    /* ✅ 일정 영역: 2열 배치 */
    .events{
      margin-top:4px;
      max-height:calc(100% - 18px);
      overflow-y:auto;

      display:grid;
      grid-template-columns:repeat(2, minmax(0, 1fr));
      gap:4px;
      align-content:start;
    }

    .event-card{
      font-size:11px;
      padding:3px 4px;
      border-radius:4px;
      border:1px solid rgba(0,0,0,0.08);
      word-wrap:break-word;
      cursor:pointer;
      min-width:0;
    }

    .event-business{
      font-weight:600;
      margin-bottom:2px;
      text-align:center;
    }

    .event-line{
      white-space:normal;
      line-height:1.15;
    }

    .event-dot{margin-right:2px;}

    /* 사업별 색상 */
    .biz-대관{background:#ffe6e6;}
    .biz-지산맞{background:#e6f7ff;}
    .biz-일학습{background:#fff7e6;}
    .biz-배터리{background:#e9ffe6;}
    .biz-기회발전{background:#f4e6ff;}
    .biz-사업주{background:#fce6f2;}
    .biz-행사{background:#ff99cc;}
    .biz-기타{background:#f0f0f0;}

    /* 모달 */
    .modal-backdrop{
      position:fixed;inset:0;background:rgba(0,0,0,0.4);
      display:none;align-items:center;justify-content:center;z-index:20;
    }
    .modal{
      background:#fff;max-width:420px;width:90%;
      border-radius:8px;padding:14px 16px 16px;box-shadow:0 6px 18px rgba(0,0,0,0.25);
    }
    .modal-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;}
    .modal-title{font-weight:600;}
    .modal-close{cursor:pointer;border:none;background:none;font-size:20px;line-height:1;}
    .field{margin-bottom:6px;}
    .field label{display:block;font-size:13px;margin-bottom:2px;}
    .field input,.field select{width:100%;padding:4px 6px;font-size:13px;}
    .field-inline{display:flex;gap:6px;}
    .field-inline .field{flex:1;margin-bottom:0;}
    .modal-footer{display:flex;justify-content:space-between;gap:4px;margin-top:8px;flex-wrap:wrap;}
    .btn-primary{background:#2d89ff;border-color:#1b5fcc;color:#fff;}
    .btn-danger{background:#ff4d4f;border-color:#d9363e;color:#fff;}
    .mini-btn{padding:6px 10px;border-radius:4px;border:1px solid #ccc;background:#f7f7f7;cursor:pointer;}
    .mini-danger{background:#ff4d4f;border-color:#d9363e;color:#fff;}

    @media (max-width: 768px){
      h1{font-size:22px;}
      .current-month{font-size:17px;}
      .calendar tbody td{height:145px;}
      .event-card{font-size:12px;}
      .events{grid-template-columns:1fr;}
    }

    @media print{
      .top-bar,.modal-backdrop{display:none !important;}
      .container{margin:0;max-width:100%;padding:0;}
      body{background:#fff;}
      *{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }
    }
  </style>
</head>
<body>
<div class="container">
  <h1>포항산학 월별일정</h1>
  <div class="current-month" id="currentMonth"></div>

  <div class="top-bar">
    <div class="top-left">
      <button id="prevBtn">◀ 이전</button>
      <button id="nextBtn">다음 ▶</button>

      <button id="monthViewBtn">월별</button>
      <button id="weekViewBtn">주별</button>

      <label>
        사업명:
        <select id="businessFilter">
          <option value="전체">전체</option>
          <option value="대관">대관</option>
          <option value="지산맞">지산맞</option>
          <option value="일학습">일학습</option>
          <option value="배터리">배터리</option>
          <option value="기회발전">기회발전</option>
          <option value="사업주">사업주</option>
          <option value="행사">행사</option>
        </select>
      </label>
      <button id="resetFilterBtn">필터 초기화</button>
    </div>
    <div class="top-right">
      <button id="addEventBtn">+ 일정 추가하기</button>
    </div>
  </div>

  <table class="calendar">
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

<div class="modal-backdrop" id="modalBackdrop">
  <div class="modal">
    <div class="modal-header">
      <div class="modal-title" id="modalTitle">일정 추가</div>
      <button class="modal-close" id="modalCloseBtn">×</button>
    </div>

    <div class="modal-body">
      <input type="hidden" id="eventId">
      <input type="hidden" id="clickedDay">

      <div class="field-inline">
        <div class="field">
          <label for="startDate">시작일</label>
          <input type="date" id="startDate">
        </div>
        <div class="field">
          <label for="endDate">종료일</label>
          <input type="date" id="endDate">
        </div>
      </div>

      <div class="field">
        <label for="business">사업명</label>
        <select id="business">
          <option value="">선택</option>
          <option value="대관">대관</option>
          <option value="지산맞">지산맞</option>
          <option value="일학습">일학습</option>
          <option value="배터리">배터리</option>
          <option value="기회발전">기회발전</option>
          <option value="사업주">사업주</option>
          <option value="행사">행사</option>
        </select>
      </div>

      <div class="field">
        <label for="course">과정명</label>
        <input type="text" id="course" placeholder="예: 파이썬 기초">
      </div>

      <div class="field-inline">
        <div class="field">
          <label for="time">훈련(대관) 시간</label>
          <input type="text" id="time" placeholder="예: 09:00~18:00">
        </div>
        <div class="field">
          <label for="people">대상인원</label>
          <input type="text" id="people" placeholder="예: 20">
        </div>
      </div>

      <div class="field-inline">
        <div class="field">
          <label for="place">훈련장소</label>
          <input type="text" id="place" placeholder="예: 1강의실">
        </div>
        <div class="field">
          <label for="admin">행정</label>
          <input type="text" id="admin" placeholder="예: 김OO">
        </div>
      </div>

      <div class="field" style="display:none;">
        <label for="excludedDates">제외일자</label>
        <input type="text" id="excludedDates">
      </div>

      <div style="font-size:12px;color:#666;margin-top:6px;">
        ※ 기간으로 입력해도 저장 후에는 <b>날짜별 개별 일정</b>으로 분할됩니다.
      </div>
    </div>

    <div class="modal-footer">
      <button class="btn-danger" id="deleteEventBtn" style="display:none;">삭제</button>

      <!-- (호환) 예전 기간형 이벤트만 이날삭제(exclude) 필요하지만, 일단 유지 -->
      <button class="mini-btn mini-danger" id="deleteOneDayBtn" style="display:none;">이날 삭제</button>

      <div style="flex:1;"></div>
      <button id="cancelBtn">취소</button>
      <button class="btn-primary" id="saveEventBtn">저장</button>
    </div>
  </div>
</div>

<script>
  let allEvents = [];
  let currentYear, currentMonth;
  let mode = "create";

  let viewMode = "month"; // "month" | "week"
  let anchorDate = new Date();

  const businessColors = {
    "대관":"biz-대관",
    "지산맞":"biz-지산맞",
    "일학습":"biz-일학습",
    "배터리":"biz-배터리",
    "기회발전":"biz-기회발전",
    "사업주":"biz-사업주",
    "행사":"biz-행사"
  };

  function escapeHtml(s){
    if(s === null || s === undefined) return "";
    return String(s)
      .replaceAll("&","&amp;")
      .replaceAll("<","&lt;")
      .replaceAll(">","&gt;")
      .replaceAll('"',"&quot;")
      .replaceAll("'","&#39;");
  }

  function lineIf(label, value){
    const v = (value ?? "").toString().trim();
    if(!v) return "";
    return `<div class="event-line"><span class="event-dot">▪</span>${label}: ${escapeHtml(v)}</div>`;
  }

  function formatDate(d){
    const y = d.getFullYear();
    const m = String(d.getMonth()+1).padStart(2,"0");
    const day = String(d.getDate()).padStart(2,"0");
    return `${y}-${m}-${day}`;
  }
  function parseDate(str){
    const [y,m,d] = str.split("-").map(Number);
    return new Date(y, m-1, d);
  }

  function startOfWeek(d){
    const x = new Date(d);
    x.setHours(0,0,0,0);
    x.setDate(x.getDate() - x.getDay());
    return x;
  }

  // 예전 기간형 이벤트 호환을 위한 함수 (현재는 대부분 start=end)
  function getRangeDates(startStr, endStr, excludedStr){
    const dates = [];
    if(!startStr || !endStr) return dates;

    let current = parseDate(startStr);
    const end = parseDate(endStr);

    const excluded = (excludedStr || "")
      .split(",")
      .map(s=>s.trim())
      .filter(Boolean);

    while(current <= end){
      const iso = formatDate(current);
      if(!excluded.includes(iso)) dates.push(iso);
      current.setDate(current.getDate()+1);
    }
    return dates;
  }

  function getBusinessClass(biz){
    return businessColors[biz] || "biz-기타";
  }

  function buildCardHTML(ev){
    return `
      <div class="event-business">${escapeHtml(ev.business || "사업명 없음")}</div>
      ${lineIf("과정", ev.course)}
      ${lineIf("시간", ev.time)}
      ${lineIf("인원", ev.people)}
      ${lineIf("장소", ev.place)}
      ${lineIf("행정", ev.admin)}
    `;
  }

  async function fetchEvents(){
    const res = await fetch("/api/events");
    if(!res.ok){ alert("일정 불러오기 실패"); return; }
    allEvents = await res.json();
    renderCalendar();
  }

  // ✅ 서버가 기간을 자동 분할해서 (객체 또는 배열)로 응답할 수 있음
  async function createEvent(payload){
    const res = await fetch("/api/events",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload)
    });
    if(!res.ok){ alert("저장 실패"); return; }

    const created = await res.json();
    if(Array.isArray(created)){
      allEvents.push(...created);
    }else{
      allEvents.push(created);
    }
    renderCalendar();
  }

  async function updateEvent(id, payload){
    const res = await fetch(`/api/events/${id}`,{
      method:"PUT",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload)
    });
    if(!res.ok){ alert("수정 실패"); return; }
    const updated = await res.json();
    allEvents = allEvents.map(ev=>ev.id===id?updated:ev);
    renderCalendar();
  }

  async function deleteEvent(id){
    if(!confirm("정말 삭제하시겠습니까?")) return;
    const res = await fetch(`/api/events/${id}`,{method:"DELETE"});
    if(!res.ok){ alert("삭제 실패"); return; }
    allEvents = allEvents.filter(ev=>ev.id!==id);
    renderCalendar();
    closeModal();
  }

  async function excludeOneDay(id, dateStr){
    const res = await fetch(`/api/events/${id}/exclude`,{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({date: dateStr})
    });
    if(!res.ok){ alert("이날 삭제 실패"); return; }
    const updated = await res.json();
    allEvents = allEvents.map(ev=>ev.id===id?updated:ev);
    renderCalendar();
  }

  function renderCalendar(){
    if(viewMode === "week"){
      renderWeek();
    }else{
      renderMonth();
    }
  }

  function renderMonth(){
    const body = document.getElementById("calendarBody");
    body.innerHTML = "";

    const firstDay = new Date(currentYear, currentMonth, 1);
    document.getElementById("currentMonth").textContent = `${currentYear}년 ${currentMonth+1}월`;

    const businessFilter = document.getElementById("businessFilter").value;

    let dateCursor = new Date(firstDay);
    dateCursor.setDate(dateCursor.getDate() - firstDay.getDay());

    for(let week=0; week<6; week++){
      const tr = document.createElement("tr");

      for(let dow=0; dow<7; dow++){
        const td = document.createElement("td");
        const dayNumDiv = document.createElement("div");
        dayNumDiv.className = "day-number";

        const cellMonth = dateCursor.getMonth();
        const iso = formatDate(dateCursor);

        if(cellMonth === currentMonth){
          if(dow===0) dayNumDiv.classList.add("sun");
          if(dow===6) dayNumDiv.classList.add("sat");
          dayNumDiv.textContent = dateCursor.getDate();
        }else{
          dayNumDiv.textContent = "";
        }

        td.appendChild(dayNumDiv);

        const eventsDiv = document.createElement("div");
        eventsDiv.className = "events";

        if(cellMonth === currentMonth){
          allEvents.forEach(ev=>{
            if(businessFilter !== "전체" && ev.business !== businessFilter) return;
            const dates = getRangeDates(ev.start, ev.end, ev.excluded_dates);
            if(!dates.includes(iso)) return;

            const card = document.createElement("div");
            card.className = "event-card " + getBusinessClass(ev.business || "");
            card.addEventListener("click", ()=>openEditModal(ev, iso));
            card.innerHTML = buildCardHTML(ev);
            eventsDiv.appendChild(card);
          });
        }

        td.appendChild(eventsDiv);
        tr.appendChild(td);

        dateCursor.setDate(dateCursor.getDate()+1);
      }

      body.appendChild(tr);
    }
  }

  function renderWeek(){
    const body = document.getElementById("calendarBody");
    body.innerHTML = "";

    const ws = startOfWeek(anchorDate);
    const we = new Date(ws); we.setDate(we.getDate()+6);

    document.getElementById("currentMonth").textContent =
      `${ws.getFullYear()}년 ${ws.getMonth()+1}월 (주별: ${formatDate(ws)} ~ ${formatDate(we)})`;

    const businessFilter = document.getElementById("businessFilter").value;
    const tr = document.createElement("tr");

    for(let dow=0; dow<7; dow++){
      const d = new Date(ws);
      d.setDate(d.getDate()+dow);

      const td = document.createElement("td");
      const iso = formatDate(d);

      const dayNumDiv = document.createElement("div");
      dayNumDiv.className = "day-number";
      if(dow===0) dayNumDiv.classList.add("sun");
      if(dow===6) dayNumDiv.classList.add("sat");
      dayNumDiv.textContent = d.getDate();
      td.appendChild(dayNumDiv);

      const eventsDiv = document.createElement("div");
      eventsDiv.className = "events";
      td.appendChild(eventsDiv);

      allEvents.forEach(ev=>{
        if(businessFilter !== "전체" && ev.business !== businessFilter) return;
        const dates = getRangeDates(ev.start, ev.end, ev.excluded_dates);
        if(!dates.includes(iso)) return;

        const card = document.createElement("div");
        card.className = "event-card " + getBusinessClass(ev.business || "");
        card.addEventListener("click", ()=>openEditModal(ev, iso));
        card.innerHTML = buildCardHTML(ev);
        eventsDiv.appendChild(card);
      });

      tr.appendChild(td);
    }

    body.appendChild(tr);
  }

  function openCreateModal(dateStr){
    mode="create";
    document.getElementById("modalTitle").textContent="일정 추가";
    document.getElementById("eventId").value="";
    document.getElementById("clickedDay").value="";
    document.getElementById("startDate").value=dateStr||"";
    document.getElementById("endDate").value=dateStr||"";
    document.getElementById("business").value="";
    document.getElementById("course").value="";
    document.getElementById("time").value="";
    document.getElementById("people").value="";
    document.getElementById("place").value="";
    document.getElementById("admin").value="";
    document.getElementById("excludedDates").value="";
    document.getElementById("deleteEventBtn").style.display="none";
    document.getElementById("deleteOneDayBtn").style.display="none";
    document.getElementById("modalBackdrop").style.display="flex";
  }

  function openEditModal(ev, clickedDay){
    mode="edit";
    document.getElementById("modalTitle").textContent="일정 수정";
    document.getElementById("eventId").value=ev.id;
    document.getElementById("clickedDay").value = clickedDay || "";

    document.getElementById("startDate").value=ev.start;
    document.getElementById("endDate").value=ev.end;
    document.getElementById("business").value=ev.business||"";
    document.getElementById("course").value=ev.course||"";
    document.getElementById("time").value=ev.time||"";
    document.getElementById("people").value=ev.people||"";
    document.getElementById("place").value=ev.place||"";
    document.getElementById("admin").value=ev.admin||"";
    document.getElementById("excludedDates").value=ev.excluded_dates||"";

    document.getElementById("deleteEventBtn").style.display="inline-block";

    // ✅ 이제 대부분 날짜별 일정이므로 이날 삭제(exclude)는 사실상 필요 없음.
    // 다만 과거 '기간 1건' 이벤트가 남아있을 수 있어서, start!=end일 때만 표시.
    document.getElementById("deleteOneDayBtn").style.display =
      (clickedDay && ev.start !== ev.end) ? "inline-block" : "none";

    document.getElementById("modalBackdrop").style.display="flex";
  }

  function closeModal(){
    document.getElementById("modalBackdrop").style.display="none";
  }

  document.addEventListener("DOMContentLoaded", ()=>{
    const today = new Date();
    currentYear = today.getFullYear();
    currentMonth = today.getMonth();
    anchorDate = new Date(today);

    document.getElementById("prevBtn").addEventListener("click", ()=>{
      if(viewMode === "week"){
        anchorDate.setDate(anchorDate.getDate()-7);
      }else{
        currentMonth--;
        if(currentMonth<0){ currentMonth=11; currentYear--; }
      }
      renderCalendar();
    });

    document.getElementById("nextBtn").addEventListener("click", ()=>{
      if(viewMode === "week"){
        anchorDate.setDate(anchorDate.getDate()+7);
      }else{
        currentMonth++;
        if(currentMonth>11){ currentMonth=0; currentYear++; }
      }
      renderCalendar();
    });

    document.getElementById("monthViewBtn").addEventListener("click", ()=>{
      viewMode = "month";
      renderCalendar();
    });

    document.getElementById("weekViewBtn").addEventListener("click", ()=>{
      viewMode = "week";
      anchorDate = new Date();
      renderCalendar();
    });

    document.getElementById("businessFilter").addEventListener("change", renderCalendar);
    document.getElementById("resetFilterBtn").addEventListener("click", ()=>{
      document.getElementById("businessFilter").value="전체";
      renderCalendar();
    });

    document.getElementById("addEventBtn").addEventListener("click", ()=>openCreateModal());

    document.getElementById("modalCloseBtn").addEventListener("click", closeModal);
    document.getElementById("cancelBtn").addEventListener("click", closeModal);

    document.getElementById("saveEventBtn").addEventListener("click", async ()=>{
      const payload = {
        start: document.getElementById("startDate").value,
        end: document.getElementById("endDate").value,
        business: document.getElementById("business").value || null,
        course: document.getElementById("course").value || null,
        time: document.getElementById("time").value || null,
        people: document.getElementById("people").value || null,
        place: document.getElementById("place").value || null,
        admin: document.getElementById("admin").value || null,
        excluded_dates: "" // 신규 저장은 분할이므로 사용 안 함
      };

      if(!payload.start || !payload.end){
        alert("시작일과 종료일을 입력해주세요.");
        return;
      }

      if(mode==="create"){
        await createEvent(payload); // ✅ 서버가 자동 분할 저장
      }else{
        const id = parseInt(document.getElementById("eventId").value, 10);
        await updateEvent(id, payload);
      }
      closeModal();
    });

    document.getElementById("deleteEventBtn").addEventListener("click", ()=>{
      const id = parseInt(document.getElementById("eventId").value, 10);
      if(!id) return;
      deleteEvent(id);
    });

    // (호환) 과거 기간형 이벤트일 때만 의미 있음
    document.getElementById("deleteOneDayBtn").addEventListener("click", async ()=>{
      const id = parseInt(document.getElementById("eventId").value, 10);
      const day = document.getElementById("clickedDay").value;
      if(!id || !day) return;

      await excludeOneDay(id, day);
      closeModal();
    });

    fetchEvents();
  });
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return INDEX_HTML


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
