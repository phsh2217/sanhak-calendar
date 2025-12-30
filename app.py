import os
import re
from datetime import date, datetime, timedelta
from typing import Optional, Tuple, List, Dict

import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL 환경변수가 설정되어 있지 않습니다.")

# ---------------------------
# DB
# ---------------------------

def get_db():
    # Render Postgres는 보통 SSL 필요. URL에 sslmode가 없으면 require로 강제.
    dsn = DATABASE_URL
    if "sslmode=" not in dsn:
        if "?" in dsn:
            dsn += "&sslmode=require"
        else:
            dsn += "?sslmode=require"
    conn = psycopg2.connect(dsn)
    return conn

def init_db():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("""
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
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_date ON events(event_date);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_business ON events(business);")
    conn.commit()
    conn.close()

init_db()

# ---------------------------
# Utils
# ---------------------------

def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()

def daterange(d1: date, d2: date):
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)

def clean(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    s2 = str(s).strip()
    return s2 if s2 else None

def to_payload_row(r) -> dict:
    return {
        "id": r["id"],
        "event_date": r["event_date"].strftime("%Y-%m-%d"),
        "business": r["business"],
        "course": r["course"],
        "time": r["time"],
        "people": r["people"],
        "place": r["place"],
        "admin": r["admin"],
    }

TIME_PATTERNS = [
    # 18:00~22:00 / 18:00-22:00
    re.compile(r"^\s*(\d{1,2})[:.](\d{2})\s*[~\-]\s*(\d{1,2})[:.](\d{2})\s*$"),
    # 0900~2200 / 0900-2200
    re.compile(r"^\s*(\d{2})(\d{2})\s*[~\-]\s*(\d{2})(\d{2})\s*$"),
]

def parse_time_range(s: Optional[str]) -> Optional[Tuple[int, int]]:
    """time 문자열을 분 단위로 파싱. 못 파싱하면 None."""
    if not s:
        return None
    ss = s.strip()
    for pat in TIME_PATTERNS:
        m = pat.match(ss)
        if m:
            h1, m1, h2, m2 = map(int, m.groups())
            start = h1 * 60 + m1
            end = h2 * 60 + m2
            if end < start:
                # 자정 넘어가는 케이스는 일단 허용하지 않음
                return None
            return (start, end)
    return None

def overlaps(a: Tuple[int, int], b: Tuple[int, int]) -> bool:
    return not (a[1] <= b[0] or b[1] <= a[0])

def check_place_conflict(conn, event_date: date, place: Optional[str], time_str: Optional[str], exclude_id: Optional[int] = None):
    """같은 날짜+같은 장소에서 시간이 겹치면 충돌(409). 시간 파싱 안되면 충돌검사 생략."""
    place = clean(place)
    tr = parse_time_range(time_str)
    if not place or not tr:
        return  # 검사 생략

    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    if exclude_id:
        cur.execute("""
            SELECT id, time FROM events
            WHERE event_date=%s AND place=%s AND id<>%s
        """, (event_date, place, exclude_id))
    else:
        cur.execute("""
            SELECT id, time FROM events
            WHERE event_date=%s AND place=%s
        """, (event_date, place))

    rows = cur.fetchall()
    for r in rows:
        other_tr = parse_time_range(r["time"])
        if other_tr and overlaps(tr, other_tr):
            raise ValueError(f"장소 중복 예약: {place} / {event_date} / {time_str} (겹치는 일정 id={r['id']})")

def distinct_businesses(conn) -> List[str]:
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT business FROM events WHERE business IS NOT NULL AND business<>'' ORDER BY business;")
    items = [x[0] for x in cur.fetchall() if x and x[0]]
    return items

# ---------------------------
# Pages
# ---------------------------

@app.route("/favicon.ico")
def favicon():
    return Response(status=204)

@app.route("/")
def index():
    # 단일 HTML(프론트) + API(백엔드) 구조
    html = r"""
<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>포항산학 월별일정</title>
  <style>
    :root{
      --border:#d6d6d6;
      --muted:#666;
      --bg:#fff;
      --card:#f7f7f7;
      --shadow:0 1px 2px rgba(0,0,0,.06);
      --radius:12px;
    }
    *{box-sizing:border-box}
    body{margin:0;background:#fff;color:#111;font-family:system-ui,-apple-system,Segoe UI,Roboto,Apple SD Gothic Neo,Noto Sans KR,sans-serif}
    .wrap{max-width:1200px;margin:0 auto;padding:18px 14px 30px}
    h1{margin:10px 0 6px;text-align:center;font-size:44px;letter-spacing:-1px}
    .ym{margin:0 0 10px;text-align:center;font-size:34px;font-weight:800}
    .topbar{
      display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;
      margin:10px 0 12px;
    }
    .leftControls, .rightControls{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
    .btn{
      background:#f3f3f3;border:1px solid #bdbdbd;border-radius:8px;
      padding:10px 14px;font-size:18px;cursor:pointer;box-shadow:var(--shadow);
    }
    .btn:active{transform:translateY(1px)}
    .select{
      border:1px solid #bdbdbd;border-radius:8px;padding:9px 10px;font-size:18px;background:#fff;
    }
    .label{font-size:22px;font-weight:800}
    .addBtn{
      background:#f3f3f3;border:1px solid #bdbdbd;border-radius:10px;
      padding:12px 16px;font-size:18px;cursor:pointer;box-shadow:var(--shadow);
      white-space:nowrap;
    }

    /* Calendar */
    .calendar{
      width:100%;
      border:1px solid var(--border);
      border-radius:14px;
      overflow:hidden;
      background:#fff;
    }
    .dowRow{
      display:grid;
      grid-template-columns:repeat(7,1fr);
      background:#fafafa;
      border-bottom:1px solid var(--border);
    }
    .dow{
      text-align:center;padding:10px 0;font-weight:900;font-size:18px;
      border-right:1px solid var(--border);
    }
    .dow:last-child{border-right:none}
    .dow.sun{color:#d50000}
    .dow.sat{color:#1356d6}

    .grid{
      display:grid;
      grid-template-columns:repeat(7,1fr);
    }
    .cell{
      min-height:140px;
      border-right:1px solid var(--border);
      border-bottom:1px solid var(--border);
      padding:8px 8px 10px;
      position:relative;
      background:#fff;
    }
    .cell:nth-child(7n){border-right:none}
    .dateNum{
      font-weight:900;
      font-size:18px;
      display:inline-block;
      padding:2px 6px;
      border-radius:8px;
    }
    .dateNum.sun{color:#d50000}
    .dateNum.sat{color:#1356d6}
    .dateNum.muted{color:#aaa}

    /* events layout: 2 per row */
    .events{
      margin-top:8px;
      display:grid;
      grid-template-columns:repeat(2,minmax(0,1fr));
      gap:8px;
    }
    .eventCard{
      border-radius:14px;
      padding:10px 10px 10px;
      border:1px solid rgba(0,0,0,.08);
      box-shadow:0 1px 2px rgba(0,0,0,.05);
      cursor:pointer;
      overflow:hidden;
      min-height:86px;
    }
    .eventTitle{
      text-align:center;
      font-weight:1000;
      font-size:18px;
      margin-bottom:6px;
      letter-spacing:-.3px;
      word-break:keep-all;
    }
    .eventLines{
      font-size:14px;
      line-height:1.25;
      color:#111;
      word-break:break-word;
    }
    .line{display:block;margin:2px 0}
    .sym{font-weight:900;margin-right:4px}

    /* Weekly view */
    .weekWrap{
      border:1px solid var(--border);
      border-radius:14px;
      overflow:hidden;
      background:#fff;
    }
    .weekHeader{
      display:flex;align-items:center;justify-content:space-between;
      padding:10px 12px;background:#fafafa;border-bottom:1px solid var(--border)
    }
    .weekGrid{
      display:grid;grid-template-columns:repeat(7,1fr);
    }
    .weekCol{
      border-right:1px solid var(--border);
      padding:10px;
      min-height:420px;
    }
    .weekCol:last-child{border-right:none}
    .weekDayTitle{
      font-weight:1000;font-size:16px;margin-bottom:8px
    }
    .weekEvents{
      display:flex;flex-direction:column;gap:8px;
    }

    /* Modal */
    .modalBg{
      position:fixed;inset:0;background:rgba(0,0,0,.45);
      display:none;align-items:center;justify-content:center;padding:14px;z-index:50;
    }
    .modal{
      width:min(720px,100%);
      background:#fff;border-radius:16px;box-shadow:0 10px 30px rgba(0,0,0,.2);
      overflow:hidden;
    }
    .modalHeader{
      display:flex;align-items:center;justify-content:space-between;
      padding:14px 16px;border-bottom:1px solid #eee;
      font-weight:1000;font-size:18px;
    }
    .modalBody{padding:14px 16px}
    .row{display:grid;grid-template-columns:140px 1fr;gap:10px;align-items:center;margin:10px 0}
    .row label{font-weight:900}
    .inp{
      width:100%;
      border:1px solid #c9c9c9;border-radius:10px;
      padding:10px 12px;font-size:16px;
    }
    .modalFooter{
      padding:14px 16px;border-top:1px solid #eee;
      display:flex;gap:10px;justify-content:flex-end;flex-wrap:wrap
    }
    .danger{background:#ffecec;border:1px solid #ffb4b4}
    .primary{background:#eef6ff;border:1px solid #b8d6ff}
    .hint{color:var(--muted);font-size:13px;margin-top:6px;line-height:1.35}

    /* Print color */
    @media print{
      body{-webkit-print-color-adjust:exact;print-color-adjust:exact}
      .topbar,.modalBg{display:none !important}
      .wrap{max-width:none;padding:0}
      .calendar,.weekWrap{border:none;border-radius:0}
      .cell{min-height:140px}
    }

    /* Mobile tuning (가독성 개선) */
    @media (max-width: 820px){
      .wrap{padding:14px 10px 22px}
      h1{font-size:38px}
      .ym{font-size:30px}
      .btn,.addBtn,.select{font-size:17px}
      .label{font-size:20px}
      .cell{min-height:120px;padding:7px}
      .events{grid-template-columns:1fr;gap:8px} /* 모바일은 1열이 더 읽기 좋음 */
      .eventTitle{font-size:18px}
      .eventLines{font-size:15px;line-height:1.35}
      .row{grid-template-columns:110px 1fr}
    }

    /* "가로모드 강제" - 기술적으로 100% 강제는 어려워서(브라우저 제한),
       세로일 때만 화면을 회전시키는 옵션을 제공 */
    .force-landscape{
      position:fixed; inset:0; background:#fff; z-index:5;
      transform:rotate(90deg) translateY(-100%);
      transform-origin:top left;
      width:100vh; height:100vw;
      overflow:auto;
    }
    .landscapeTip{
      display:none;
      text-align:center;
      padding:8px 10px;
      border:1px dashed #bbb;
      border-radius:12px;
      margin:10px 0 0;
      color:#333;
      background:#fafafa;
      font-size:13px;
    }
    @media (max-width:820px){
      .landscapeTip{display:block}
    }
  </style>
</head>
<body>
  <div id="appWrap" class="wrap">
    <h1>포항산학 월별일정</h1>
    <div id="ym" class="ym">-</div>

    <div class="topbar">
      <div class="leftControls">
        <button class="btn" id="prevBtn">◀ 이전</button>
        <button class="btn" id="nextBtn">다음 ▶</button>
        <button class="btn" id="monthBtn">월별</button>
        <button class="btn" id="weekBtn">주별</button>
        <span class="label">사업명:</span>
        <select id="bizSelect" class="select"></select>
        <button class="btn" id="resetFilterBtn">필터 초기화</button>
      </div>
      <div class="rightControls">
        <button class="addBtn" id="openAddBtn">+ 일정 추가하기</button>
      </div>
    </div>

    <div class="landscapeTip">
      📱 모바일 가독성이 더 필요하면 <b>주별</b> 보기 추천! (월별은 칸이 좁아져서 글이 길면 줄바꿈이 많아요)
      <br/>
      <label style="display:inline-flex;align-items:center;gap:6px;margin-top:6px;">
        <input type="checkbox" id="forceLandscapeChk"/> 세로일 때 “가로처럼” 보기(회전)
      </label>
    </div>

    <div id="viewArea"></div>
  </div>

  <!-- Add/Edit Modal -->
  <div class="modalBg" id="modalBg">
    <div class="modal">
      <div class="modalHeader">
        <span id="modalTitle">일정 추가</span>
        <button class="btn" id="closeModalBtn">닫기</button>
      </div>
      <div class="modalBody">
        <div class="row">
          <label>기간(시작)</label>
          <input type="date" id="startDate" class="inp"/>
        </div>
        <div class="row">
          <label>기간(종료)</label>
          <input type="date" id="endDate" class="inp"/>
        </div>

        <div class="row">
          <label>사업명</label>
          <input id="business" class="inp" list="bizList" placeholder="예) 지산맞 / 대관 / 사업주 ..."/>
          <datalist id="bizList"></datalist>
        </div>

        <div class="row">
          <label>과정명</label>
          <input id="course" class="inp" placeholder="예) 파이썬 offjt"/>
        </div>
        <div class="row">
          <label>시간</label>
          <input id="time" class="inp" placeholder="예) 0900~2200 또는 18:00~22:00"/>
        </div>
        <div class="row">
          <label>인원</label>
          <input id="people" class="inp" placeholder="예) 10"/>
        </div>
        <div class="row">
          <label>훈련장소</label>
          <input id="place" class="inp" placeholder="예) 테크노1관2층"/>
        </div>
        <div class="row">
          <label>행정</label>
          <input id="admin" class="inp" placeholder="예) 김민수"/>
        </div>
        <div class="hint">
          ✅ 기간으로 등록하면 <b>각 날짜가 개별 일정</b>으로 저장됩니다.  
          등록 후에는 해당 날짜 카드 클릭 → 수정/삭제 가능.
          <br/>✅ “훈련장소+시간”이 겹치면 중복 예약으로 저장이 막힙니다(시간 형식이 파싱 가능한 경우).
        </div>
      </div>
      <div class="modalFooter">
        <button class="btn danger" id="deleteBtn" style="display:none;">이 날짜 삭제</button>
        <button class="btn" id="cancelBtn">취소</button>
        <button class="btn primary" id="saveBtn">저장</button>
      </div>
    </div>
  </div>

  <script>
    // -------------------------
    // State
    // -------------------------
    const state = {
      viewMode: "month", // month | week
      cursor: new Date(), // 기준 날짜
      events: [],
      businessFilter: "전체",
      editingId: null,
      bizList: [],
    };

    // stable colors per business (기본 팔레트 + 해시)
    const baseColors = [
      "#f7c6dc", "#d8f5d2", "#d8ecff", "#ffe7c6", "#eadcff",
      "#ffd7d7", "#d7fff0", "#fff3c0", "#cfe0ff", "#e6e6e6"
    ];

    function hashCode(str){
      let h = 0;
      for(let i=0;i<str.length;i++) h = ((h<<5)-h) + str.charCodeAt(i), h |= 0;
      return Math.abs(h);
    }
    function bizColor(biz){
      if(!biz) return "#eaeaea";
      const idx = hashCode(biz) % baseColors.length;
      return baseColors[idx];
    }

    // -------------------------
    // DOM
    // -------------------------
    const ymEl = document.getElementById("ym");
    const viewArea = document.getElementById("viewArea");
    const prevBtn = document.getElementById("prevBtn");
    const nextBtn = document.getElementById("nextBtn");
    const monthBtn = document.getElementById("monthBtn");
    const weekBtn = document.getElementById("weekBtn");
    const bizSelect = document.getElementById("bizSelect");
    const resetFilterBtn = document.getElementById("resetFilterBtn");
    const openAddBtn = document.getElementById("openAddBtn");

    const modalBg = document.getElementById("modalBg");
    const closeModalBtn = document.getElementById("closeModalBtn");
    const cancelBtn = document.getElementById("cancelBtn");
    const saveBtn = document.getElementById("saveBtn");
    const deleteBtn = document.getElementById("deleteBtn");
    const modalTitle = document.getElementById("modalTitle");

    const startDate = document.getElementById("startDate");
    const endDate = document.getElementById("endDate");
    const business = document.getElementById("business");
    const course = document.getElementById("course");
    const time = document.getElementById("time");
    const people = document.getElementById("people");
    const place = document.getElementById("place");
    const admin = document.getElementById("admin");
    const bizListDatalist = document.getElementById("bizList");

    const forceLandscapeChk = document.getElementById("forceLandscapeChk");
    const appWrap = document.getElementById("appWrap");

    // -------------------------
    // Helpers
    // -------------------------
    function pad(n){ return String(n).padStart(2,"0"); }
    function toYMD(d){
      return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}`;
    }
    function fromYMD(s){
      const [y,m,d] = s.split("-").map(Number);
      return new Date(y, m-1, d);
    }
    function setYMTitle(){
      const y = state.cursor.getFullYear();
      const m = state.cursor.getMonth()+1;
      ymEl.textContent = `${y}년 ${m}월`;
    }

    function startOfWeek(d){
      const x = new Date(d);
      const day = x.getDay(); // 0 Sun
      x.setDate(x.getDate() - day);
      x.setHours(0,0,0,0);
      return x;
    }

    function filteredEvents(){
      if(state.businessFilter === "전체") return state.events;
      return state.events.filter(e => (e.business || "") === state.businessFilter);
    }

    function buildBizSelect(){
      bizSelect.innerHTML = "";
      const optAll = document.createElement("option");
      optAll.value = "전체";
      optAll.textContent = "전체";
      bizSelect.appendChild(optAll);

      state.bizList.forEach(b => {
        const opt = document.createElement("option");
        opt.value = b;
        opt.textContent = b;
        bizSelect.appendChild(opt);
      });

      bizSelect.value = state.businessFilter;
    }

    function buildBizDatalist(){
      bizListDatalist.innerHTML = "";
      state.bizList.forEach(b => {
        const o = document.createElement("option");
        o.value = b;
        bizListDatalist.appendChild(o);
      });
    }

    function maybeApplyForceLandscape(){
      const want = forceLandscapeChk.checked;
      const isMobile = window.matchMedia("(max-width: 820px)").matches;
      if(!isMobile){
        appWrap.classList.remove("force-landscape");
        return;
      }
      // 세로(높이>너비)일 때만 회전 적용
      const portrait = window.innerHeight > window.innerWidth;
      if(want && portrait) appWrap.classList.add("force-landscape");
      else appWrap.classList.remove("force-landscape");
    }

    // -------------------------
    // API
    // -------------------------
    async function apiGetEvents(){
      const res = await fetch("/api/events");
      const data = await res.json();
      state.events = data.events || [];
      state.bizList = data.businesses || [];
      buildBizSelect();
      buildBizDatalist();
    }

    async function apiCreateRange(payload){
      const res = await fetch("/api/events/range",{
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if(!res.ok) throw new Error(data.error || "저장 중 오류");
      return data;
    }

    async function apiUpdate(id, payload){
      const res = await fetch(`/api/events/${id}`,{
        method:"PUT",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify(payload)
      });
      const data = await res.json();
      if(!res.ok) throw new Error(data.error || "수정 중 오류");
      return data;
    }

    async function apiDelete(id){
      const res = await fetch(`/api/events/${id}`,{method:"DELETE"});
      const data = await res.json();
      if(!res.ok) throw new Error(data.error || "삭제 중 오류");
      return data;
    }

    // -------------------------
    // Render Month View
    // -------------------------
    function renderMonth(){
      setYMTitle();
      const y = state.cursor.getFullYear();
      const m = state.cursor.getMonth();
      const first = new Date(y,m,1);
      const last = new Date(y,m+1,0);
      const startDow = first.getDay(); // 0..6

      const all = filteredEvents();
      // map date -> events
      const map = new Map();
      for(const ev of all){
        const k = ev.event_date;
        if(!map.has(k)) map.set(k, []);
        map.get(k).push(ev);
      }
      // 정렬(사업명/과정/시간)
      for(const [k, arr] of map){
        arr.sort((a,b)=> (a.business||"").localeCompare(b.business||"") || (a.course||"").localeCompare(b.course||""));
      }

      const cal = document.createElement("div");
      cal.className = "calendar";

      const dowRow = document.createElement("div");
      dowRow.className = "dowRow";
      const dows = ["일","월","화","수","목","금","토"];
      dows.forEach((d,i)=>{
        const el = document.createElement("div");
        el.className = "dow" + (i===0?" sun": i===6?" sat":"");
        el.textContent = d;
        dowRow.appendChild(el);
      });
      cal.appendChild(dowRow);

      const grid = document.createElement("div");
      grid.className = "grid";

      // 이전달 채우기
      const prevLast = new Date(y,m,0).getDate();
      for(let i=0;i<startDow;i++){
        const dayNum = prevLast - (startDow-1-i);
        grid.appendChild(buildCell(new Date(y,m-1,dayNum), true, map));
      }
      // 이번달
      for(let d=1; d<=last.getDate(); d++){
        grid.appendChild(buildCell(new Date(y,m,d), false, map));
      }
      // 다음달 채우기 (6주 고정 느낌)
      const totalCells = grid.childElementCount;
      const need = (totalCells <= 35) ? (42-totalCells) : (49-totalCells);
      for(let i=1;i<=need;i++){
        grid.appendChild(buildCell(new Date(y,m+1,i), true, map));
      }

      cal.appendChild(grid);
      viewArea.innerHTML = "";
      viewArea.appendChild(cal);
    }

    function buildCell(dt, muted, map){
      const cell = document.createElement("div");
      cell.className = "cell";

      const dn = document.createElement("span");
      const dow = dt.getDay();
      dn.className = "dateNum" + (muted? " muted":"") + (dow===0?" sun": dow===6? " sat":"");
      dn.textContent = dt.getDate();
      cell.appendChild(dn);

      const k = toYMD(dt);
      const arr = map.get(k) || [];
      if(arr.length){
        const evBox = document.createElement("div");
        evBox.className = "events";
        arr.forEach(ev=>{
          evBox.appendChild(buildEventCard(ev));
        });
        cell.appendChild(evBox);
      }
      return cell;
    }

    function buildEventCard(ev){
      const card = document.createElement("div");
      card.className = "eventCard";
      card.style.background = bizColor(ev.business || "");
      card.onclick = () => openEditModal(ev);

      const title = document.createElement("div");
      title.className = "eventTitle";
      title.textContent = (ev.business || "일정");
      card.appendChild(title);

      const lines = document.createElement("div");
      lines.className = "eventLines";

      // 공란은 아예 미표기
      if(ev.course){
        lines.appendChild(lineEl("▪", `과정: ${ev.course}`));
      }
      if(ev.time){
        lines.appendChild(lineEl("▪", `시간: ${ev.time}`));
      }
      if(ev.people){
        lines.appendChild(lineEl("▪", `인원: ${ev.people}`));
      }
      if(ev.place){
        lines.appendChild(lineEl("▪", `장소: ${ev.place}`));
      }
      if(ev.admin){
        lines.appendChild(lineEl("▪", `행정: ${ev.admin}`));
      }

      card.appendChild(lines);
      return card;
    }

    function lineEl(sym, text){
      const s = document.createElement("span");
      s.className = "line";
      s.innerHTML = `<span class="sym">${sym}</span>${escapeHtml(text)}`;
      return s;
    }
    function escapeHtml(str){
      return String(str)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;")
        .replaceAll("'","&#039;");
    }

    // -------------------------
    // Render Week View
    // -------------------------
    function renderWeek(){
      const start = startOfWeek(state.cursor);
      const end = new Date(start);
      end.setDate(end.getDate()+6);

      // 주 타이틀(대략)
      ymEl.textContent = `${start.getFullYear()}년 ${start.getMonth()+1}월 ${start.getDate()}일 ~ ${end.getMonth()+1}월 ${end.getDate()}일`;

      const all = filteredEvents();
      const map = new Map();
      for(const ev of all){
        const k = ev.event_date;
        if(!map.has(k)) map.set(k, []);
        map.get(k).push(ev);
      }
      for(const [k, arr] of map){
        arr.sort((a,b)=> (a.business||"").localeCompare(b.business||"") || (a.course||"").localeCompare(b.course||""));
      }

      const wrap = document.createElement("div");
      wrap.className = "weekWrap";

      const header = document.createElement("div");
      header.className = "weekHeader";
      header.innerHTML = `<div style="font-weight:1000">주별 보기</div><div style="color:#666;font-size:13px">카드 클릭 → 수정/삭제</div>`;
      wrap.appendChild(header);

      const grid = document.createElement("div");
      grid.className = "weekGrid";

      const dows = ["일","월","화","수","목","금","토"];

      for(let i=0;i<7;i++){
        const day = new Date(start);
        day.setDate(day.getDate()+i);
        const col = document.createElement("div");
        col.className = "weekCol";

        const title = document.createElement("div");
        title.className = "weekDayTitle";
        title.innerHTML = `<span style="color:${i===0?'#d50000':i===6?'#1356d6':'#111'}">${dows[i]}</span> ${day.getMonth()+1}/${day.getDate()}`;
        col.appendChild(title);

        const box = document.createElement("div");
        box.className = "weekEvents";
        const arr = map.get(toYMD(day)) || [];
        arr.forEach(ev => box.appendChild(buildEventCard(ev)));
        col.appendChild(box);

        grid.appendChild(col);
      }

      wrap.appendChild(grid);
      viewArea.innerHTML = "";
      viewArea.appendChild(wrap);
    }

    // -------------------------
    // Modal
    // -------------------------
    function openAddModal(){
      state.editingId = null;
      modalTitle.textContent = "일정 추가";
      deleteBtn.style.display = "none";

      // 기본값: 오늘 기준
      const today = new Date();
      startDate.value = toYMD(today);
      endDate.value = toYMD(today);

      business.value = "";
      course.value = "";
      time.value = "";
      people.value = "";
      place.value = "";
      admin.value = "";

      modalBg.style.display = "flex";
    }

    function openEditModal(ev){
      state.editingId = ev.id;
      modalTitle.textContent = "일정 수정";
      deleteBtn.style.display = "inline-block";

      startDate.value = ev.event_date;
      endDate.value = ev.event_date; // 수정은 해당 날짜 단일
      business.value = ev.business || "";
      course.value = ev.course || "";
      time.value = ev.time || "";
      people.value = ev.people || "";
      place.value = ev.place || "";
      admin.value = ev.admin || "";

      modalBg.style.display = "flex";
    }

    function closeModal(){
      modalBg.style.display = "none";
    }

    async function saveModal(){
      const sd = startDate.value;
      const ed = endDate.value;
      if(!sd || !ed){
        alert("기간(시작/종료)을 입력해주세요.");
        return;
      }

      const payload = {
        start: sd,
        end: ed,
        business: business.value.trim(),
        course: course.value.trim(),
        time: time.value.trim(),
        people: people.value.trim(),
        place: place.value.trim(),
        admin: admin.value.trim(),
      };

      try{
        if(state.editingId){
          // 수정은 단일 날짜(=sd=ed로 둠)
          await apiUpdate(state.editingId, payload);
        }else{
          // 기간 등록 => 일별 개별 생성
          await apiCreateRange(payload);
        }
        await refresh();
        closeModal();
      }catch(e){
        alert(e.message || "저장 중 오류");
      }
    }

    async function deleteOneDay(){
      if(!state.editingId) return;
      if(!confirm("이 날짜 일정을 삭제할까요?")) return;
      try{
        await apiDelete(state.editingId);
        await refresh();
        closeModal();
      }catch(e){
        alert(e.message || "삭제 중 오류");
      }
    }

    // -------------------------
    // Navigation
    // -------------------------
    function movePrev(){
      if(state.viewMode === "month"){
        state.cursor = new Date(state.cursor.getFullYear(), state.cursor.getMonth()-1, 1);
      }else{
        const d = new Date(state.cursor);
        d.setDate(d.getDate()-7);
        state.cursor = d;
      }
      render();
    }
    function moveNext(){
      if(state.viewMode === "month"){
        state.cursor = new Date(state.cursor.getFullYear(), state.cursor.getMonth()+1, 1);
      }else{
        const d = new Date(state.cursor);
        d.setDate(d.getDate()+7);
        state.cursor = d;
      }
      render();
    }

    function render(){
      if(state.viewMode === "month") renderMonth();
      else renderWeek();
      maybeApplyForceLandscape();
    }

    async function refresh(){
      await apiGetEvents();
      render();
    }

    // -------------------------
    // Events
    // -------------------------
    prevBtn.onclick = movePrev;
    nextBtn.onclick = moveNext;
    monthBtn.onclick = () => { state.viewMode="month"; render(); };
    weekBtn.onclick = () => { state.viewMode="week"; render(); };

    bizSelect.onchange = () => {
      state.businessFilter = bizSelect.value;
      render();
    };
    resetFilterBtn.onclick = () => {
      state.businessFilter = "전체";
      buildBizSelect();
      render();
    };

    openAddBtn.onclick = openAddModal;
    closeModalBtn.onclick = closeModal;
    cancelBtn.onclick = closeModal;
    saveBtn.onclick = saveModal;
    deleteBtn.onclick = deleteOneDay;

    modalBg.addEventListener("click",(e)=>{
      if(e.target === modalBg) closeModal();
    });

    forceLandscapeChk.addEventListener("change", maybeApplyForceLandscape);
    window.addEventListener("resize", maybeApplyForceLandscape);

    // boot
    refresh();
  </script>
</body>
</html>
"""
    return Response(html, mimetype="text/html")

# ---------------------------
# API
# ---------------------------

@app.route("/api/events", methods=["GET"])
def api_get_events():
    conn = get_db()
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    cur.execute("""
        SELECT id, event_date, business, course, time, people, place, admin
        FROM events
        ORDER BY event_date, id
    """)
    rows = cur.fetchall()
    events = [to_payload_row(r) for r in rows]

    businesses = distinct_businesses(conn)

    conn.close()
    return jsonify({"events": events, "businesses": businesses})

@app.route("/api/events/range", methods=["POST"])
def api_create_range():
    data = request.get_json(force=True) or {}
    start = data.get("start")
    end = data.get("end")
    if not start or not end:
        return jsonify({"error": "start/end가 필요합니다."}), 400

    sd = parse_ymd(start)
    ed = parse_ymd(end)
    if ed < sd:
        return jsonify({"error": "종료일은 시작일보다 빠를 수 없습니다."}), 400

    business = clean(data.get("business"))
    course = clean(data.get("course"))
    time = clean(data.get("time"))
    people = clean(data.get("people"))
    place = clean(data.get("place"))
    admin = clean(data.get("admin"))

    conn = get_db()
    try:
        # 날짜별로 개별 이벤트 생성
        created = 0
        cur = conn.cursor()
        for d in daterange(sd, ed):
            # 장소 중복 검사(가능한 형식일 때)
            check_place_conflict(conn, d, place, time, exclude_id=None)
            cur.execute("""
                INSERT INTO events(event_date, business, course, time, people, place, admin)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
            """, (d, business, course, time, people, place, admin))
            created += 1
        conn.commit()
        return jsonify({"ok": True, "created": created}), 201
    except ValueError as ve:
        conn.rollback()
        return jsonify({"error": str(ve)}), 409
    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"저장 실패: {e}"}), 500
    finally:
        conn.close()

@app.route("/api/events/<int:event_id>", methods=["PUT"])
def api_update(event_id: int):
    data = request.get_json(force=True) or {}

    # 수정은 “해당 날짜 단일” 기준
    start = data.get("start")
    end = data.get("end")
    if not start:
        return jsonify({"error": "start(날짜)가 필요합니다."}), 400

    d = parse_ymd(start)

    business = clean(data.get("business"))
    course = clean(data.get("course"))
    time = clean(data.get("time"))
    people = clean(data.get("people"))
    place = clean(data.get("place"))
    admin = clean(data.get("admin"))

    conn = get_db()
    try:
        # 해당 id 존재 확인
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur.execute("SELECT id FROM events WHERE id=%s", (event_id,))
        if not cur.fetchone():
            return jsonify({"error": "해당 일정이 없습니다."}), 404

        # 장소 중복 검사(가능한 형식일 때)
        check_place_conflict(conn, d, place, time, exclude_id=event_id)

        cur2 = conn.cursor()
        cur2.execute("""
            UPDATE events
            SET event_date=%s, business=%s, course=%s, time=%s, people=%s, place=%s, admin=%s, updated_at=NOW()
            WHERE id=%s
        """, (d, business, course, time, people, place, admin, event_id))
        conn.commit()

        # 업데이트 결과 반환
        cur3 = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
        cur3.execute("""
            SELECT id, event_date, business, course, time, people, place, admin
            FROM events WHERE id=%s
        """, (event_id,))
        row = cur3.fetchone()
        return jsonify({"ok": True, "event": to_payload_row(row)})
    except ValueError as ve:
        conn.rollback()
        return jsonify({"error": str(ve)}), 409
    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"수정 실패: {e}"}), 500
    finally:
        conn.close()

@app.route("/api/events/<int:event_id>", methods=["DELETE"])
def api_delete(event_id: int):
    conn = get_db()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM events WHERE id=%s", (event_id,))
        if cur.rowcount == 0:
            conn.rollback()
            return jsonify({"error": "해당 일정이 없습니다."}), 404
        conn.commit()
        return jsonify({"ok": True})
    except Exception as e:
        conn.rollback()
        return jsonify({"error": f"삭제 실패: {e}"}), 500
    finally:
        conn.close()

if __name__ == "__main__":
    # 로컬 실행용
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)
