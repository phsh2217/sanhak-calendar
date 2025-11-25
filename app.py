import os
import sqlite3
from pathlib import Path
from flask import Flask, request, jsonify

app = Flask(__name__)
DB_PATH = Path("calendar.db")


# ---------- DB 초기화 ----------
def reset_db():
    if DB_PATH.exists():
        DB_PATH.unlink()  # 기존 DB 삭제
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start TEXT NOT NULL,
            end TEXT NOT NULL,
            business TEXT,
            course TEXT,
            time TEXT,
            people TEXT,
            place TEXT,
            admin TEXT,
            excluded_dates TEXT
        )
        """
    )
    conn.commit()
    conn.close()


# 서버 시작 시 DB 초기화 실행
reset_db()


# ---------- DB 유틸 ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- HTML ----------
INDEX_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>포항산학 월별일정</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">

<style>
body { font-family: Arial, sans-serif; margin: 0; padding: 0; }
h1 { text-align:center; margin-top:20px; margin-bottom:10px; }

/* 월 제목 */
.month-title {
    text-align: center;
    font-size: 22px;
    font-weight: bold;
    margin-top: 0;
    margin-bottom: 5px;
}

/* 버튼 컨트롤 */
#month-controls {
    width: 100%;
    max-width: 1000px;
    margin: 5px auto 10px auto;
    display: flex;
    justify-content: space-between;
    align-items: center;
}
#month-controls-left button {
    padding: 8px 12px;
    margin-right: 5px;
    cursor: pointer;
}
#month-controls-right button {
    padding: 8px 12px;
    cursor: pointer;
}

#top-controls {
    width: 100%;
    max-width: 1000px;
    margin: 0 auto 10px auto;
    display:flex;
    justify-content: flex-start;
    align-items:center;
    gap: 10px;
}
button { cursor:pointer; }

#calendar {
    width: 100%;
    max-width: 1000px;
    margin: 10px auto;
}

/* 달력 테이블 */
table { width: 100%; border-collapse: collapse; table-layout: fixed; }
th, td {
    border: 1px solid #ccc;
    padding: 4px;
    vertical-align: top;
    height: 95px;
    font-size: 12px;
}
th { background:#f3f3f3; }
th:nth-child(1), td:nth-child(1) { color: red; }
th:nth-child(7), td:nth-child(7) { color: blue; }

.event {
    font-size: 11px;
    margin-top: 3px;
    padding: 3px;
    border-radius: 4px;
    cursor: pointer;
}
.event .title {
    display: block;
    font-weight: bold;
    text-align: center;
}

/* 팝업 */
#formOverlay {
    position: fixed; top:0; left:0; width:100%; height:100%;
    background: rgba(0,0,0,0.3);
    display:none;
}
#formBox {
    position: fixed; top:50%; left:50%;
    transform: translate(-50%, -50%);
    background: #fff; padding: 20px;
    width: 350px; border:1px solid #777;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
    display:none;
}
.form-row { margin-bottom: 8px; font-size:14px; }
.form-row input, .form-row select { width:100%; padding:4px; }
.form-actions { text-align:right; margin-top:10px; }
.form-actions button { margin-left:4px; }

/* 모바일 */
@media (max-width: 768px) {
    th, td { height: 70px; font-size: 11px; }
}
</style>
</head>
<body>

<h1>포항산학 월별일정</h1>

<!-- 월 제목 -->
<div class="month-title" id="title"></div>

<!-- 이전달/다음달 + 일정추가 -->
<div id="month-controls">
    <div id="month-controls-left">
        <button onclick="prevMonth()">◀ 이전달</button>
        <button onclick="nextMonth()">다음달 ▶</button>
    </div>
    <div id="month-controls-right">
        <button onclick="openFormNew()">+ 일정 추가하기</button>
    </div>
</div>

<!-- 필터 -->
<div id="top-controls">
    <label>사업명:
        <select id="businessFilter" onchange="onBusinessFilterChange()">
            <option value="">전체</option>
        </select>
    </label>
    <button onclick="resetFilter()">필터 초기화</button>
</div>

<div id="calendar"></div>
<div id="formOverlay" onclick="closeForm()"></div>

<!-- 일정 입력창 -->
<div id="formBox">
    <h3 id="formTitle">일정 추가</h3>

    <div class="form-row">시작일: <input type="date" id="f_start"></div>
    <div class="form-row">종료일: <input type="date" id="f_end"></div>
    <div class="form-row">사업명:
        <select id="f_business_select"></select>
        <input type="text" id="f_business" placeholder="직접입력">
    </div>
    <div class="form-row">과정명: <input type="text" id="f_course"></div>
    <div class="form-row">훈련(대관) 시간: <input type="text" id="f_time"></div>
    <div class="form-row">대상 인원: <input type="text" id="f_people"></div>
    <div class="form-row">훈련장소: <input type="text" id="f_place"></div>
    <div class="form-row">행정: <input type="text" id="f_admin"></div>

    <div class="form-actions">
        <button onclick="closeForm()">취소</button>
        <button id="deleteBtn" onclick="deleteEvent()" style="display:none;">삭제</button>
        <button onclick="saveEvent()">저장</button>
    </div>
</div>

<script>
let events = [];
let colorMap = {};
let current = new Date();
let editingId = null;
let editingDate = null;
let currentBusinessFilter = "";

const BUSINESS_LIST = ["대관","지산맞","일학습","배터리","기회발전","사업주"];
const PALETTE = ["#ffe5e5","#e5f7ff","#e9ffe5","#fff4e5","#f0e5ff","#ffe5f2"];
const BUSINESS_COLOR = {
    "대관": "#ffe5e5", "지산맞": "#e5f7ff", "일학습": "#e9ffe5",
    "배터리": "#fff4e5", "기회발전": "#f0e5ff", "사업주": "#ffe5f2"
};

function inRange(d, start, end) { return (d >= start && d <= end); }

function rebuildColorMap() {
    colorMap = {...BUSINESS_COLOR};
    let idx = 0;
    events.forEach(e => {
        const key = e.business || "";
        if (key && !(key in colorMap)) {
            colorMap[key] = PALETTE[idx % PALETTE.length];
            idx++;
        }
    });
}

function rebuildBusinessFilter() {
    const select = document.getElementById("businessFilter");
    const prev = currentBusinessFilter;
    select.innerHTML = '<option value="">전체</option>';
    BUSINESS_LIST.forEach(name => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        if (name === prev) opt.selected = true;
        select.appendChild(opt);
    });
}

function rebuildBusinessSelect() {
    const sel = document.getElementById("f_business_select");
    sel.innerHTML = '<option value="">(선택)</option>';
    BUSINESS_LIST.forEach(n => {
        const opt = document.createElement("option");
        opt.value = n;
        opt.textContent = n;
        sel.appendChild(opt);
    });
}

function onBusinessFilterChange() {
    currentBusinessFilter = document.getElementById("businessFilter").value;
    buildCalendar();
}

function resetFilter() {
    currentBusinessFilter = "";
    document.getElementById("businessFilter").value = "";
    buildCalendar();
}

function addExcludedDate(oldStr, date) {
    let arr = oldStr ? oldStr.split(",").map(a => a.trim()) : [];
    if (!arr.includes(date)) arr.push(date);
    return arr.join(",");
}

function buildCalendar() {
    const y = current.getFullYear();
    const m = current.getMonth();
    document.getElementById("title").innerText = y + "년 " + (m + 1) + "월";

    const first = new Date(y, m, 1).getDay();
    const last = new Date(y, m + 1, 0).getDate();

    let html = "<table><tr>";
    for (const w of ["일","월","화","수","목","금","토"]) html += "<th>" + w + "</th>";
    html += "</tr><tr>";

    for (let i = 0; i < first; i++) html += "<td></td>";

    for (let d = 1; d <= last; d++) {
        let dateStr = y + "-" + String(m + 1).padStart(2,'0') + "-" + String(d).padStart(2,'0');
        html += "<td><b>" + d + "</b>";

        events.forEach(e => {
            if (currentBusinessFilter && e.business !== currentBusinessFilter) return;
            const start = e.start;
            const end = e.end || start;
            if (!inRange(dateStr, start, end)) return;

            const excl = (e.excluded_dates || "").split(",").map(a=>a.trim()).filter(Boolean);
            if (excl.includes(dateStr)) return;

            const bg = colorMap[e.business] || "#eef";
            html += `
                <div class="event"
                     style="background:${bg};"
                     onclick="editEvent(${e.id}, '${dateStr}')">
                    <span class="title">${e.business || ""}</span>
                    ▪ 과정: ${e.course || ""}<br>
                    ▪ 시간: ${e.time || ""}<br>
                    ▪ 인원: ${e.people || ""}<br>
                    ▪ 장소: ${e.place || ""}<br>
                    ▪ 행정: ${e.admin || ""}
                </div>`;
        });

        html += "</td>";
        if ((first + d) % 7 === 0) html += "</tr><tr>";
    }
    html += "</tr></table>";
    document.getElementById("calendar").innerHTML = html;
}

function prevMonth() { current.setMonth(current.getMonth() - 1); buildCalendar(); }
function nextMonth() { current.setMonth(current.getMonth() + 1); buildCalendar(); }

function openFormNew() {
    editingId = null;
    editingDate = null;
    document.getElementById("formTitle").innerText = "일정 추가";
    ["f_start","f_end","f_business","f_course","f_time","f_people","f_place","f_admin"]
      .forEach(id => document.getElementById(id).value = "");
    document.getElementById("f_business_select").value = "";
    document.getElementById("deleteBtn").style.display = "none";
    document.getElementById("formBox").style.display = "block";
    document.getElementById("formOverlay").style.display = "block";
}

function closeForm(){
    document.getElementById("formBox").style.display = "none";
    document.getElementById("formOverlay").style.display = "none";
    editingId = null;
    editingDate = null;
}

function findEvent(id) { return events.find(e => e.id === id); }

function editEvent(id, dateStr){
    const e = findEvent(id);
    if (!e) return;
    editingId = id;
    editingDate = dateStr;
    document.getElementById("formTitle").innerText = "일정 수정";
    document.getElementById("f_start").value = e.start;
    document.getElementById("f_end").value = e.end;
    if (BUSINESS_LIST.includes(e.business)) {
        document.getElementById("f_business_select").value = e.business;
        document.getElementById("f_business").value = "";
    } else {
        document.getElementById("f_business_select").value = "";
        document.getElementById("f_business").value = e.business;
    }
    document.getElementById("f_course").value = e.course;
    document.getElementById("f_time").value = e.time;
    document.getElementById("f_people").value = e.people;
    document.getElementById("f_place").value = e.place;
    document.getElementById("f_admin").value = e.admin;
    document.getElementById("deleteBtn").style.display = "inline-block";
    document.getElementById("formBox").style.display = "block";
    document.getElementById("formOverlay").style.display = "block";
}

async function saveEvent() {
    const start = document.getElementById("f_start").value;
    let end = document.getElementById("f_end").value || start;
    const sel = document.getElementById("f_business_select").value;
    const business = sel || document.getElementById("f_business").value;
    const course = document.getElementById("f_course").value;
    const time = document.getElementById("f_time").value;
    const people = document.getElementById("f_people").value;
    const place = document.getElementById("f_place").value;
    const admin = document.getElementById("f_admin").value;

    if (!start) return alert("시작일은 필수입니다.");

    try{
        if (editingId === null) {
            await fetch("/api/events", {
                method:"POST",
                headers:{"Content-Type":"application/json"},
                body:JSON.stringify({start,end,business,course,time,people,place,admin,excluded_dates:""})
            });
        } else {
            const original = findEvent(editingId);
            if (editingDate && original.start !== original.end) {
                const newExcluded = addExcludedDate(original.excluded_dates || "", editingDate);
                await fetch("/api/events/" + editingId, {
                    method:"PUT",
                    headers:{"Content-Type":"application/json"},
                    body:JSON.stringify({
                        start:original.start,
                        end:original.end,
                        business:original.business,
                        course:original.course,
                        time:original.time,
                        people:original.people,
                        place:original.place,
                        admin:original.admin,
                        excluded_dates:newExcluded
                    })
                });
                await fetch("/api/events", {
                    method:"POST",
                    headers:{"Content-Type":"application/json"},
                    body:JSON.stringify({
                        start:editingDate,
                        end:editingDate,
                        business,course,time,people,place,admin,
                        excluded_dates:""
                    })
                });
            } else {
                await fetch("/api/events/" + editingId, {
                    method:"PUT",
                    headers:{"Content-Type":"application/json"},
                    body:JSON.stringify({
                        start,end,business,course,time,people,place,admin,
                        excluded_dates:original.excluded_dates || ""
                    })
                });
            }
        }
        await loadEvents();
        closeForm();
    } catch(e){ alert("저장 실패"); }
}

async function deleteEvent(){
    if (editingId === null) return;
    if (!confirm("삭제하시겠습니까?")) return;
    await fetch("/api/events/" + editingId, {method:"DELETE"});
    await loadEvents();
    closeForm();
}

async function loadEvents(){
    const res = await fetch("/api/events");
    const json = await res.json();
    events = json.map(e => ({
        id:e.id, start:e.start, end:e.end,
        business:e.business || "", course:e.course || "",
        time:e.time || "", people:e.people || "",
        place:e.place || "", admin:e.admin || "",
        excluded_dates:e.excluded_dates || ""
    }));
    rebuildColorMap();
    rebuildBusinessFilter();
    rebuildBusinessSelect();
    buildCalendar();
}

loadEvents();
</script>

</body>
</html>
"""


# ---------- API ----------
@app.route("/")
def index():
    return INDEX_HTML


@app.route("/api/events", methods=["GET"])
def get_events():
    conn = get_db()
    rows = conn.execute("SELECT * FROM events ORDER BY start, id").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/events", methods=["POST"])
def create_event():
    d = request.get_json()
    conn = get_db()
    cur = conn.execute(
        """INSERT INTO events (start,end,business,course,time,people,place,admin,excluded_dates)
        VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            d.get("start"), d.get("end"), d.get("business",""),
            d.get("course",""), d.get("time",""), d.get("people",""),
            d.get("place",""), d.get("admin",""), d.get("excluded_dates","")
        )
    )
    conn.commit()
    new_id = cur.lastrowid
    row = conn.execute("SELECT * FROM events WHERE id=?", (new_id,)).fetchone()
    conn.close()
    return jsonify(dict(row)), 201


@app.route("/api/events/<int:event_id>", methods=["PUT"])
def update_event(event_id):
    d = request.get_json()
    conn = get_db()
    conn.execute(
        """UPDATE events SET start=?,end=?,business=?,course=?,time=?,people=?,place=?,admin=?,excluded_dates=? WHERE id=?""",
        (
            d.get("start"), d.get("end"), d.get("business",""), d.get("course",""),
            d.get("time",""), d.get("people",""), d.get("place",""), d.get("admin",""),
            d.get("excluded_dates",""), event_id
        )
    )
    conn.commit()
    row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
    conn.close()
    return jsonify(dict(row))


@app.route("/api/events/<int:event_id>", methods=["DELETE"])
def delete_event(event_id):
    conn = get_db()
    conn.execute("DELETE FROM events WHERE id=?", (event_id,))
    conn.commit()
    conn.close()
    return "", 204


# ---------- 서버 실행 ----------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
