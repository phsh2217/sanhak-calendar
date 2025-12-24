import os
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


# excluded_dates: "2025-11-27,2025-11-28" 형태(콤마 문자열) 유지
def parse_excluded(excluded_str: str):
    if not excluded_str:
        return []
    return [s.strip() for s in excluded_str.split(",") if s.strip()]


def dump_excluded(excluded_list):
    # 중복 제거 + 정렬
    uniq = sorted(set([d.strip() for d in excluded_list if d and d.strip()]))
    return ",".join(uniq)


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
    data = request.json or {}

    # 안전 처리: end 비었으면 start와 동일하게
    start = data.get("start")
    end = data.get("end") or start

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute(
        """
        INSERT INTO events (start, "end", business, course, time, people, place, admin, excluded_dates)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id, start, "end", business, course, time, people, place, admin, excluded_dates;
        """,
        (
            start,
            end,
            data.get("business"),
            data.get("course"),
            data.get("time"),
            data.get("people"),
            data.get("place"),
            data.get("admin"),
            data.get("excluded_dates") or "",
        ),
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()
    return jsonify(row_to_event(row)), 201


@app.route("/api/events/<int:event_id>", methods=["PUT"])
def api_update_event(event_id):
    data = request.json or {}

    start = data.get("start")
    end = data.get("end") or start

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
            start,
            end,
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


# ✅ (추가 기능) 특정 날짜만 제외(=그 날짜만 삭제처럼 숨김)
@app.route("/api/events/<int:event_id>/exclude", methods=["POST"])
def api_exclude_one_day(event_id):
    data = request.json or {}
    date = (data.get("date") or "").strip()  # YYYY-MM-DD
    if not date:
        return jsonify({"error": "date is required"}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cur.execute("SELECT excluded_dates FROM events WHERE id=%s;", (event_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "not found"}), 404

    excluded = parse_excluded(row["excluded_dates"] or "")
    if date not in excluded:
        excluded.append(date)

    new_excluded = dump_excluded(excluded)
    cur.execute("UPDATE events SET excluded_dates=%s WHERE id=%s;", (new_excluded, event_id))
    conn.commit()

    # 갱신된 이벤트 반환
    cur.execute(
        """
        SELECT id, start, "end", business, course, time, people, place, admin, excluded_dates
        FROM events WHERE id=%s;
        """,
        (event_id,),
    )
    updated = cur.fetchone()

    cur.close()
    conn.close()
    return jsonify(row_to_event(updated))


# ✅ (추가 기능) 제외 해제(복구)
@app.route("/api/events/<int:event_id>/exclude", methods=["DELETE"])
def api_unexclude_one_day(event_id):
    data = request.json or {}
    date = (data.get("date") or "").strip()
    if not date:
        return jsonify({"error": "date is required"}), 400

    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    cur.execute("SELECT excluded_dates FROM events WHERE id=%s;", (event_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return jsonify({"error": "not found"}), 404

    excluded = parse_excluded(row["excluded_dates"] or "")
    excluded = [d for d in excluded if d != date]
    new_excluded = dump_excluded(excluded)

    cur.execute("UPDATE events SET excluded_dates=%s WHERE id=%s;", (new_excluded, event_id))
    conn.commit()

    cur.execute(
        """
        SELECT id, start, "end", business, course, time, people, place, admin, excluded_dates
        FROM events WHERE id=%s;
        """,
        (event_id,),
    )
    updated = cur.fetchone()

    cur.close()
    conn.close()
    return jsonify(row_to_event(updated))


# =========================
# UI
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
      height:130px;
      padding:4px;
      position:relative;
      overflow:hidden;
    }

    .day-number{font-size:13px;font-weight:600;}
    .sun{color:var(--sun);}
    .sat{color:var(--sat);}

    /* 일정 영역: 2열 배치 */
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
      margin-bottom:0;
      border:1px solid rgba(0,0,0,0.08);
      word-wrap:break-word;
      cursor:pointer;
      min-width:0;
      position:relative;
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

    /* ✅ 날짜별 삭제/복구 버튼 */
    .event-actions{
      display:flex;
      gap:4px;
      margin-top:4px;
      justify-content:space-between;
    }
    .mini-btn{
      font-size:10px;
      padding:3px 6px;
      border-radius:4px;
      border:1px solid rgba(0,0,0,0.2);
      background:#fff;
      cursor:pointer;
    }
    .mini-btn:hover{background:#f3f3f3;}
    .mini-danger{border-color:#d9363e;color:#d9363e;}
    .mini-ok{border-color:#1b5fcc;color:#1b5fcc;}

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
    .field input,.field select,.field textarea{width:100%;padding:4px 6px;font-size:13px;}
    textarea{resize:vertical;}
    .field-inline{display:flex;gap:6px;}
    .field-inline .field{flex:1;margin-bottom:0;}
    .modal-footer{display:flex;justify-content:space-between;gap:4px;margin-top:8px;}
    .btn-primary{background:#2d89ff;border-color:#1b5fcc;color:#fff;}
    .btn-danger{background:#ff4d4f;border-color:#d9363e;color:#fff;}

    @media (max-width: 768px){
      h1{font-size:22px;}
      .current-month{font-size:17px;}
      .calendar tbody td{height:155px;}
      .event-card{font-size:12px;}
      .events{grid-template-columns:1fr;}
      .mini-btn{font-size:11px;}
    }

    @media print{
      .top-bar,.modal-backdrop{display:none !important;}
      .container{margin:0;max-width:100%;padding:0;}
      body{background:#fff;}
    }
  </style>
</head>
<body>
<div class="container">
  <h1>포항산학 월별일정</h1>
  <div class="current-month" id="currentMonth"></div>

  <div class="top-bar">
    <div class="top-left">
      <button id="prevMonthBtn">◀ 이전달</button>
      <button id="nextMonthBtn">다음달 ▶</button>

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

<!-- 모달 -->
<div class="modal-backdrop" id="modalBackdrop">
  <div class="modal">
    <div class="modal-header">
      <div class="modal-title" id="modalTitle">일정 추가</div>
      <button class="modal-close" id="modalCloseBtn">×</button>
    </div>

    <div class="modal-body">
      <input type="hidden" id="eventId">
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

      <div class="field">
        <label for="excludedDates">제외일자 (쉼표로 구분, 예: 2025-11-27)</label>
        <input type="text" id="excludedDates" placeholder="없으면 비워두기">
      </div>
    </div>

    <div class="modal-footer">
      <button class="btn-danger" id="deleteEventBtn" style="display:none;">전체 삭제</button>
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

  const businessColors = {
    "대관":"biz-대관",
    "지산맞":"biz-지산맞",
    "일학습":"biz-일학습",
    "배터리":"biz-배터리",
    "기회발전":"biz-기회발전",
    "사업주":"biz-사업주",
    "행사":"biz-행사"
  };

  function escapeHtml(str){
    return String(str ?? "")
      .replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;")
      .replaceAll('"',"&quot;").replaceAll("'","&#039;");
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

  function parseExcluded(excludedStr){
    return (excludedStr || "")
      .split(",")
      .map(s=>s.trim())
      .filter(Boolean);
  }
  function joinExcluded(arr){
    const uniq = Array.from(new Set(arr.filter(Boolean))).sort();
    return uniq.join(",");
  }

  // ✅ 기간(start~end) -> 날짜 배열 생성 (excluded_dates 제외)
  function getRangeDates(startStr, endStr, excludedStr){
    const dates = [];
    if(!startStr || !endStr) return dates;

    let current = parseDate(startStr);
    const end = parseDate(endStr);
    const excluded = new Set(parseExcluded(excludedStr));

    while(current <= end){
      const iso = formatDate(current);
      if(!excluded.has(iso)) dates.push(iso);
      current.setDate(current.getDate()+1);
    }
    return dates;
  }

  function getBusinessClass(biz){
    return businessColors[biz] || "biz-기타";
  }

  async function fetchEvents(){
    const res = await fetch("/api/events");
    if(!res.ok){ alert("일정 불러오기 실패"); return; }
    allEvents = await res.json();
    renderCalendar();
  }

  async function createEvent(payload){
    const res = await fetch("/api/events",{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify(payload)
    });
    if(!res.ok){ alert("저장 실패"); return; }
    const ev = await res.json();
    allEvents.push(ev);
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
    if(!confirm("이 일정(기간 전체)을 삭제하시겠습니까?")) return;
    const res = await fetch(`/api/events/${id}`,{method:"DELETE"});
    if(!res.ok){ alert("삭제 실패"); return; }
    allEvents = allEvents.filter(ev=>ev.id!==id);
    renderCalendar();
    closeModal();
  }

  // ✅ 특정 날짜만 삭제(숨김) = exclude
  async function excludeOneDay(eventId, day){
    if(!confirm(`${day} 날짜만 삭제(숨김)할까요?`)) return;

    const res = await fetch(`/api/events/${eventId}/exclude`,{
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({date: day})
    });
    if(!res.ok){ alert("날짜 삭제 중 오류"); return; }

    const updated = await res.json();
    allEvents = allEvents.map(ev=>ev.id===updated.id?updated:ev);
    renderCalendar();
  }

  // ✅ 특정 날짜 삭제 취소(복구) = unexclude
  async function unexcludeOneDay(eventId, day){
    if(!confirm(`${day} 날짜 삭제를 취소(복구)할까요?`)) return;

    const res = await fetch(`/api/events/${eventId}/exclude`,{
      method:"DELETE",
      headers:{"Content-Type":"application/json"},
      body:JSON.stringify({date: day})
    });
    if(!res.ok){ alert("복구 중 오류"); return; }

    const updated = await res.json();
    allEvents = allEvents.map(ev=>ev.id===updated.id?updated:ev);
    renderCalendar();
  }

  function renderCalendar(){
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

            // ✅ 기간에 해당하는 모든 날짜 표시
            const dates = getRangeDates(ev.start, ev.end, ev.excluded_dates);
            if(!dates.includes(iso)) return;

            const card = document.createElement("div");
            card.className = "event-card " + getBusinessClass(ev.business || "");
            card.addEventListener("click", ()=>openEditModal(ev));

            const bizDiv = document.createElement("div");
            bizDiv.className = "event-business";
            bizDiv.textContent = ev.business || "사업명 없음";

            const courseDiv = document.createElement("div");
            courseDiv.className = "event-line";
            courseDiv.innerHTML = `<span class="event-dot">▪</span>과정: ${escapeHtml(ev.course || "")}`;

            const timeDiv = document.createElement("div");
            timeDiv.className = "event-line";
            timeDiv.innerHTML = `<span class="event-dot">▪</span>시간: ${escapeHtml(ev.time || "")}`;

            const peopleDiv = document.createElement("div");
            peopleDiv.className = "event-line";
            peopleDiv.innerHTML = `<span class="event-dot">▪</span>인원: ${escapeHtml(ev.people || "")}`;

            const placeDiv = document.createElement("div");
            placeDiv.className = "event-line";
            placeDiv.innerHTML = `<span class="event-dot">▪</span>장소: ${escapeHtml(ev.place || "")}`;

            const adminDiv = document.createElement("div");
            adminDiv.className = "event-line";
            adminDiv.innerHTML = `<span class="event-dot">▪</span>행정: ${escapeHtml(ev.admin || "")}`;

            // ✅ 이 날짜만 삭제/복구 버튼
            const actions = document.createElement("div");
            actions.className = "event-actions";

            const btnDelDay = document.createElement("button");
            btnDelDay.className = "mini-btn mini-danger";
            btnDelDay.textContent = "이날 삭제";
            btnDelDay.addEventListener("click", (e)=>{
              e.stopPropagation(); // 카드 클릭(수정 모달) 방지
              excludeOneDay(ev.id, iso);
            });

            const btnUndo = document.createElement("button");
            btnUndo.className = "mini-btn mini-ok";
            btnUndo.textContent = "복구";
            btnUndo.addEventListener("click", (e)=>{
              e.stopPropagation();
              unexcludeOneDay(ev.id, iso);
            });

            actions.appendChild(btnDelDay);
            actions.appendChild(btnUndo);

            card.appendChild(bizDiv);
            card.appendChild(courseDiv);
            card.appendChild(timeDiv);
            card.appendChild(peopleDiv);
            card.appendChild(placeDiv);
            card.appendChild(adminDiv);
            card.appendChild(actions);

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

  function openCreateModal(dateStr){
    mode="create";
    document.getElementById("modalTitle").textContent="일정 추가";
    document.getElementById("eventId").value="";
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
    document.getElementById("modalBackdrop").style.display="flex";
  }

  function openEditModal(ev){
    mode="edit";
    document.getElementById("modalTitle").textContent="일정 수정";
    document.getElementById("eventId").value=ev.id;
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
    document.getElementById("modalBackdrop").style.display="flex";
  }

  function closeModal(){
    document.getElementById("modalBackdrop").style.display="none";
  }

  document.addEventListener("DOMContentLoaded", ()=>{
    const today = new Date();
    currentYear = today.getFullYear();
    currentMonth = today.getMonth();

    document.getElementById("prevMonthBtn").addEventListener("click", ()=>{
      currentMonth--;
      if(currentMonth<0){ currentMonth=11; currentYear--; }
      renderCalendar();
    });

    document.getElementById("nextMonthBtn").addEventListener("click", ()=>{
      currentMonth++;
      if(currentMonth>11){ currentMonth=0; currentYear++; }
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
      let start = document.getElementById("startDate").value;
      let end = document.getElementById("endDate").value || start;

      if(!start){
        alert("시작일을 입력해주세요.");
        return;
      }
      // start > end이면 스왑
      if(end && start > end){
        const tmp = start; start = end; end = tmp;
      }

      const payload = {
        start: start,
        end: end,
        business: document.getElementById("business").value || null,
        course: document.getElementById("course").value || null,
        time: document.getElementById("time").value || null,
        people: document.getElementById("people").value || null,
        place: document.getElementById("place").value || null,
        admin: document.getElementById("admin").value || null,
        excluded_dates: document.getElementById("excludedDates").value || ""
      };

      if(mode==="create"){
        await createEvent(payload);
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
