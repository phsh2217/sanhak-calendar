import os
import sqlite3
from pathlib import Path
from flask import Flask, request, jsonify

app = Flask(__name__)
DB_PATH = Path("calendar.db")


# ---------- DB & 컬럼 체크 ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def table_has_admin_column(conn):
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(events)")]
    return "admin" in cols


def init_db():
    conn = get_db()
    # 기본 테이블 생성 (없으면 새로 생성)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            start    TEXT NOT NULL,
            end      TEXT NOT NULL,
            business TEXT,
            course   TEXT,
            time     TEXT,
            people   TEXT,
            place    TEXT
        )
        """
    )
    # admin 컬럼 없으면 추가
    cols = [row["name"] for row in conn.execute("PRAGMA table_info(events)")]
    if "admin" not in cols:
        conn.execute("ALTER TABLE events ADD COLUMN admin TEXT")
    conn.commit()
    conn.close()


# ---------- HTML (프론트) ----------
INDEX_HTML = """
<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>포항산학 월별일정</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
body { font-family: Arial, sans-serif; }
#calendar { width: 100%; max-width: 1000px; margin: 20px auto; }
table { width: 100%; border-collapse: collapse; table-layout: fixed; }
th, td {
    border: 1px solid #ccc;
    padding: 4px;
    vertical-align: top;
    height: 95px;
    font-size: 12px;
    word-wrap: break-word;
}
th:nth-child(1), td:nth-child(1) { color: red; }   /* 일요일 */
th:nth-child(7), td:nth-child(7) { color: blue; }  /* 토요일 */
th { background: #f3f3f3; }

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
    text-align: center;  /* 사업명 가운데 정렬 */
}

.nav { width: 100%; max-width: 1000px; margin: 10px auto; text-align: right; }
button { padding: 8px 14px; margin: 0 4px; }

#top-controls {
    width: 100%;
    max-width: 1000px;
    margin: 0 auto 10px auto;
    text-align: right;
}

#formBox {
    position: fixed;
    top: 50%; left: 50%;
    transform: translate(-50%, -50%);
    background: white;
    border: 1px solid #aaa;
    padding: 16px 20px;
    display: none;
    z-index: 1000;
    width: 360px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
}
#formOverlay {
    position: fixed;
    top: 0; left: 0;
    width: 100%; height: 100%;
    background: rgba(0,0,0,0.3);
    display: none;
    z-index: 900;
}
#formBox h3 { margin-top: 0; }
.form-row { margin-bottom: 8px; font-size: 13px; }
.form-row input, .form-row select { width: 100%; box-sizing: border-box; }
.form-actions { text-align: right; margin-top: 10px; }
.form-actions button { margin-left: 4px; }

/* 모바일 화면 대응 */
@media (max-width: 768px) {
    #calendar {
        width: 100%;
        margin: 10px auto;
        max-width: 1000px;
    }
    table {
        font-size: 11px;
    }
    th, td {
        height: 70px;
        padding: 2px;
    }
    .event {
        margin-top: 2px;
        padding: 2px;
        font-size: 10px;
    }
    .nav, #top-controls {
        width: 100%;
        max-width: 1000px;
        text-align: center;
        display: flex;
        flex-wrap: wrap;
        justify-content: center;
        gap: 4px;
    }
    #title {
        width: 100%;
        display: block;
        margin-bottom: 6px;
        text-align: center;
    }
}
</style>
</head>
<body>

<!-- 상단 타이틀(가운데) -->
<h1 style="text-align:center; margin-top:20px; margin-bottom:10px;">
    포항산학 월별일정
</h1>

<!-- 월 이동 버튼 + 월 타이틀 -->
<div class="nav">
    <button onclick="prevMonth()">◀ 이전달</button>
    <span id="title" style="font-weight:bold; font-size:16px; margin:0 8px;"></span>
    <button onclick="nextMonth()">다음달 ▶</button>
</div>

<!-- 사업명 필터 + 초기화 + 일정추가 -->
<div id="top-controls">
    <label style="margin-right:8px;">
        사업명:
        <select id="businessFilter" onchange="onBusinessFilterChange()">
            <option value="">전체</option>
        </select>
    </label>
    <button onclick="resetFilter()">필터 초기화</button>
    <button onclick="openFormNew()">+ 일정 추가하기</button>
</div>

<div id="calendar"></div>

<div id="formOverlay" onclick="closeForm()"></div>

<div id="formBox">
    <h3 id="formTitle">일정 추가</h3>
    <div class="form-row">
        시작일: <input type="date" id="f_start">
    </div>
    <div class="form-row">
        종료일(선택): <input type="date" id="f_end">
    </div>
    <div class="form-row">
        사업명(선택): 
        <select id="f_business_select">
            <option value="">(클릭해서 선택)</option>
        </select>
    </div>
    <div class="form-row">
        사업명 직접입력: <input type="text" id="f_business" placeholder="새 사업명 입력">
    </div>
    <div class="form-row">
        과정명: <input type="text" id="f_course">
    </div>
    <div class="form-row">
        훈련(대관) 시간: <input type="text" id="f_time" placeholder="예: 0900~1800">
    </div>
    <div class="form-row">
        대상 인원: <input type="text" id="f_people">
    </div>
    <div class="form-row">
        훈련장소: <input type="text" id="f_place">
    </div>
    <div class="form-row">
        행정: <input type="text" id="f_admin" placeholder="행정 관련 메모">
    </div>
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
let currentBusinessFilter = "";  // 상단 필터에 선택된 사업명

// 고정 사업명 목록
const BUSINESS_LIST = ["대관", "지산맞", "일학습", "배터리", "기회발전", "사업주"];

// 사업명별 색상 팔레트
const palette = [
    "#ffe5e5", "#e5f7ff", "#e9ffe5", "#fff4e5",
    "#f0e5ff", "#ffe5f2", "#e5fff7", "#f5e5ff"
];

// 사업명별 고정 색상
const BUSINESS_COLOR_MAP = {
    "대관":   "#ffe5e5",
    "지산맞": "#e5f7ff",
    "일학습": "#e9ffe5",
    "배터리": "#fff4e5",
    "기회발전": "#f0e5ff",
    "사업주": "#ffe5f2"
};


// 문자열 날짜 d가 [start, end] 범위 안인지 체크
function inRange(d, start, end) {
    return (d >= start && d <= end);
}

// 사업명별 색상 할당 (고정 색상 + 그 외는 팔레트)
function rebuildColorMap() {
    // 먼저 고정 색상들 세팅
    colorMap = { ...BUSINESS_COLOR_MAP };

    // 그 외(새로운 사업명)가 나오면 팔레트에서 순차 배정
    let idx = 0;
    events.forEach(e => {
        const key = e.business || "";
        if (key && !(key in colorMap)) {
            colorMap[key] = palette[idx % palette.length];
            idx++;
        }
    });
}


// 상단 필터용 사업명 드롭다운 재생성 (고정 목록 사용)
function rebuildBusinessFilter() {
    const select = document.getElementById("businessFilter");
    if (!select) return;

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

// 폼 안 사업명 선택 상자 재생성 (고정 목록 사용)
function rebuildBusinessSelect() {
    const sel = document.getElementById("f_business_select");
    if (!sel) return;

    const prev = sel.value;
    sel.innerHTML = '<option value="">(클릭해서 사업명 선택)</option>';
    BUSINESS_LIST.forEach(name => {
        const opt = document.createElement("option");
        opt.value = name;
        opt.textContent = name;
        sel.appendChild(opt);
    });

    if (BUSINESS_LIST.includes(prev)) {
        sel.value = prev;
    } else {
        sel.value = "";
    }
}

// 상단 필터 변경 시
function onBusinessFilterChange() {
    const select = document.getElementById("businessFilter");
    currentBusinessFilter = select ? select.value : "";
    buildCalendar();
}

// 필터 초기화 버튼
function resetFilter() {
    currentBusinessFilter = "";
    const select = document.getElementById("businessFilter");
    if (select) select.value = "";
    buildCalendar();
}

function buildCalendar() {
    const year = current.getFullYear();
    const month = current.getMonth();
    document.getElementById("title").innerText = year + "년 " + (month + 1) + "월";

    const first = new Date(year, month, 1).getDay();
    const last = new Date(year, month + 1, 0).getDate();

    let html = "<table><tr>";
    const week = ['일','월','화','수','목','금','토'];
    for (let w of week) html += "<th>" + w + "</th>";
    html += "</tr><tr>";

    for (let i = 0; i < first; i++) html += "<td></td>";

    for (let d = 1; d <= last; d++) {
        let fd = year + "-" + String(month + 1).padStart(2, '0') + "-" + String(d).padStart(2, '0');
        html += "<td><b>" + d + "</b>";

        events.forEach(e => {
            const start = e.start;
            const end = e.end || e.start;

            // 상단 사업명 필터 적용
            if (currentBusinessFilter && (e.business || "") !== currentBusinessFilter) {
                return;
            }

            if (!inRange(fd, start, end)) return;

            const bg = colorMap[e.business || ""] || "#eef";
            html += `
                <div class="event"
                     data-id="${e.id}"
                     onclick="editEvent(${e.id})"
                     style="background-color:${bg};">
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

function prevMonth() {
    current.setMonth(current.getMonth() - 1);
    buildCalendar();
}

function nextMonth() {
    current.setMonth(current.getMonth() + 1);
    buildCalendar();
}

function openFormNew() {
    editingId = null;
    document.getElementById("formTitle").innerText = "일정 추가";
    document.getElementById("f_start").value = "";
    document.getElementById("f_end").value = "";
    document.getElementById("f_business_select").value = "";
    document.getElementById("f_business").value = "";
    document.getElementById("f_course").value = "";
    document.getElementById("f_time").value = "";
    document.getElementById("f_people").value = "";
    document.getElementById("f_place").value = "";
    document.getElementById("f_admin").value = "";
    document.getElementById("deleteBtn").style.display = "none";
    document.getElementById("formBox").style.display = "block";
    document.getElementById("formOverlay").style.display = "block";
}

function findEvent(id) {
    return events.find(e => e.id === id);
}

function editEvent(id) {
    const e = findEvent(id);
    if (!e) return;
    editingId = id;
    document.getElementById("formTitle").innerText = "일정 수정";
    document.getElementById("f_start").value = e.start;
    document.getElementById("f_end").value = e.end || "";

    const sel = document.getElementById("f_business_select");
    const input = document.getElementById("f_business");
    if (e.business && sel) {
        if (BUSINESS_LIST.includes(e.business)) {
            sel.value = e.business;
            if (input) input.value = "";
        } else {
            sel.value = "";
            if (input) input.value = e.business;
        }
    } else {
        sel.value = "";
        if (input) input.value = "";
    }

    document.getElementById("f_course").value = e.course || "";
    document.getElementById("f_time").value = e.time || "";
    document.getElementById("f_people").value = e.people || "";
    document.getElementById("f_place").value = e.place || "";
    document.getElementById("f_admin").value = e.admin || "";
    document.getElementById("deleteBtn").style.display = "inline-block";
    document.getElementById("formBox").style.display = "block";
    document.getElementById("formOverlay").style.display = "block";
}

function closeForm() {
    document.getElementById("formBox").style.display = "none";
    document.getElementById("formOverlay").style.display = "none";
}

async function saveEvent() {
    const start = document.getElementById("f_start").value;
    let end = document.getElementById("f_end").value;
    const selBusiness = document.getElementById("f_business_select").value;
    let business = selBusiness || document.getElementById("f_business").value;
    const course = document.getElementById("f_course").value;
    const time = document.getElementById("f_time").value;
    const people = document.getElementById("f_people").value;
    const place = document.getElementById("f_place").value;
    const admin = document.getElementById("f_admin").value;

    if (!start) {
        alert("시작일을 입력해주세요.");
        return;
    }
    if (!end) end = start;

    const payload = { start, end, business, course, time, people, place, admin };

    try {
        if (editingId === null) {
            const res = await fetch("/api/events", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            const created = await res.json();
            events.push(created);
        } else {
            const res = await fetch("/api/events/" + editingId, {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            const updated = await res.json();
            const idx = events.findIndex(e => e.id === editingId);
            if (idx !== -1) events[idx] = updated;
        }
        rebuildColorMap();
        rebuildBusinessFilter();
        rebuildBusinessSelect();
        buildCalendar();
        closeForm();
    } catch (err) {
        console.error(err);
        alert("저장 중 오류가 발생했습니다.");
    }
}

async function deleteEvent() {
    if (editingId === null) return;
    if (!confirm("이 일정을 삭제하시겠습니까?")) return;

    try {
        await fetch("/api/events/" + editingId, { method: "DELETE" });
        events = events.filter(e => e.id !== editingId);
        rebuildColorMap();
        rebuildBusinessFilter();
        rebuildBusinessSelect();
        buildCalendar();
        closeForm();
    } catch (err) {
        console.error(err);
        alert("삭제 중 오류가 발생했습니다.");
    }
}

async function loadEvents() {
    try {
        const res = await fetch("/api/events");
        const json = await res.json();
        events = json.map(e => ({
            id: e.id,
            start: e.start,
            end: e.end,
            business: e.business || "",
            course: e.course || "",
            time: e.time || "",
            people: e.people || "",
            place: e.place || "",
            admin: e.admin || ""
        }));
        rebuildColorMap();
        rebuildBusinessFilter();
        rebuildBusinessSelect();
        buildCalendar();
    } catch (err) {
        console.error(err);
        alert("일정을 불러오는 중 오류가 발생했습니다.");
    }
}

loadEvents();
</script>

</body>
</html>
"""


# ---------- Flask 라우트 ----------
@app.route("/")
def index():
    return INDEX_HTML


@app.route("/api/events", methods=["GET"])
def list_events():
    conn = get_db()
    rows = conn.execute("SELECT * FROM events ORDER BY start, id").fetchall()
    conn.close()
    events = [dict(row) for row in rows]
    return jsonify(events)


@app.route("/api/events", methods=["POST"])
def create_event():
    data = request.get_json(force=True)
    start = (data.get("start") or "").strip()
    end = (data.get("end") or start).strip()
    business = (data.get("business") or "").strip()
    course = (data.get("course") or "").strip()
    time_ = (data.get("time") or "").strip()
    people = (data.get("people") or "").strip()
    place = (data.get("place") or "").strip()
    admin = (data.get("admin") or "").strip()

    if not start:
        return jsonify({"error": "start is required"}), 400
    if not end:
        end = start

    conn = get_db()
    has_admin = table_has_admin_column(conn)

    if has_admin:
        conn.execute(
            "INSERT INTO events (start, end, business, course, time, people, place, admin) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (start, end, business, course, time_, people, place, admin),
        )
    else:
        conn.execute(
            "INSERT INTO events (start, end, business, course, time, people, place) "
            "VALUES (?,?,?,?,?,?,?)",
            (start, end, business, course, time_, people, place),
        )

    conn.commit()
    row = conn.execute(
        "SELECT * FROM events WHERE id = (SELECT MAX(id) FROM events)"
    ).fetchone()
    conn.close()
    return jsonify(dict(row)), 201


@app.route("/api/events/<int:event_id>", methods=["PUT"])
def update_event(event_id):
    data = request.get_json(force=True)
    start = (data.get("start") or "").strip()
    end = (data.get("end") or start).strip()
    business = (data.get("business") or "").strip()
    course = (data.get("course") or "").strip()
    time_ = (data.get("time") or "").strip()
    people = (data.get("people") or "").strip()
    place = (data.get("place") or "").strip()
    admin = (data.get("admin") or "").strip()

    if not start:
        return jsonify({"error": "start is required"}), 400
    if not end:
        end = start

    conn = get_db()
    has_admin = table_has_admin_column(conn)

    if has_admin:
        conn.execute(
            """
            UPDATE events
               SET start=?, end=?, business=?, course=?, time=?, people=?, place=?, admin=?
             WHERE id=?
            """,
            (start, end, business, course, time_, people, place, admin, event_id),
        )
    else:
        conn.execute(
            """
            UPDATE events
               SET start=?, end=?, business=?, course=?, time=?, people=?, place=?
             WHERE id=?
            """,
            (start, end, business, course, time_, people, place, event_id),
        )

    conn.commit()
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    conn.close()
    if row is None:
        return jsonify({"error": "not found"}), 404
    return jsonify(dict(row))


@app.route("/api/events/<int:event_id>", methods=["DELETE"])
def delete_event_api(event_id):
    conn = get_db()
    conn.execute("DELETE FROM events WHERE id = ?", (event_id,))
    conn.commit()
    conn.close()
    return "", 204


if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)



