# app.py
import os
import hashlib
from datetime import date, datetime, timedelta
from urllib.parse import urlparse

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

# -----------------------------
# DB 연결
# -----------------------------
def _get_database_url() -> str:
    # Render 표준: DATABASE_URL
    db_url = os.getenv("DATABASE_URL") or os.getenv("DATABASE_URL")  # 혹시 이름을 다르게 넣었을 때 대비
    if not db_url:
        raise RuntimeError("Error: DATABASE_URL 환경변수가 설정되어 있지 않습니다.")

    # Render가 postgres:// 를 주는 경우 psycopg2에서 경고/문제될 수 있어 보정
    if db_url.startswith("postgres://"):
        db_url = "postgresql://" + db_url[len("postgres://"):]
    return db_url


def get_conn():
    db_url = _get_database_url()

    # Render Postgres는 외부접속 시 SSL 필요인 경우가 많음
    # Internal URL이라도 문제 없게 기본 sslmode=require를 붙여줌
    # (이미 쿼리스트링에 있으면 그대로 사용)
    if "sslmode=" not in db_url:
        joiner = "&" if "?" in db_url else "?"
        db_url = db_url + f"{joiner}sslmode=require"

    return psycopg2.connect(db_url)


# -----------------------------
# DB 초기화/마이그레이션 (무료 플랜에서 Shell 없이 안전하게)
# -----------------------------
def init_db():
    conn = get_conn()
    cur = conn.cursor()

    # 1) 기본 테이블 생성
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
            created_at TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )

    # 2) 컬럼 누락 자동 보정(예전 구조/중간 수정으로 누락될 수 있음)
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name='events';
        """
    )
    cols = {r[0] for r in cur.fetchall()}

    # 혹시 예전 DB에 start/end 기반으로 만들어진 흔적이 있을 수 있어도,
    # 현재는 day 단위 row(event_date) 모델로 통일.
    # event_date 없으면 추가
    if "event_date" not in cols:
        cur.execute("ALTER TABLE events ADD COLUMN event_date DATE;")

    # 3) 인덱스(이제 event_date가 있으니 안전)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_event_date ON events(event_date);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_business ON events(business);")

    conn.commit()
    cur.close()
    conn.close()


# 앱 시작 시 1회 실행(데이터 삭제 절대 없음)
try:
    init_db()
except Exception as e:
    # Render 배포 중 원인 파악용(로그에 찍힘)
    print("DB init failed:", repr(e))
    raise


# -----------------------------
# 유틸
# -----------------------------
def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _daterange(d1: date, d2: date):
    if d2 < d1:
        d1, d2 = d2, d1
    cur = d1
    while cur <= d2:
        yield cur
        cur += timedelta(days=1)


# -----------------------------
# API
# -----------------------------
@app.get("/api/events")
def api_list_events():
    """
    Query:
      from=YYYY-MM-DD
      to=YYYY-MM-DD
      business= (optional, '전체'면 무시)
    """
    from_s = request.args.get("from")
    to_s = request.args.get("to")
    business = request.args.get("business")

    if not from_s or not to_s:
        return jsonify({"error": "from/to가 필요합니다."}), 400

    d_from = _parse_date(from_s)
    d_to = _parse_date(to_s)

    where = ["event_date BETWEEN %s AND %s"]
    params = [d_from, d_to]

    if business and business != "전체":
        where.append("business = %s")
        params.append(business)

    sql = f"""
        SELECT id, event_date, business, course, time, people, place, admin
        FROM events
        WHERE {" AND ".join(where)}
        ORDER BY event_date ASC, id ASC;
    """

    conn = get_conn()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    cur.execute(sql, params)
    rows = cur.fetchall()
    cur.close()
    conn.close()

    # event_date를 문자열로
    for r in rows:
        r["event_date"] = r["event_date"].strftime("%Y-%m-%d")

    return jsonify(rows)


@app.get("/api/businesses")
def api_list_businesses():
    """
    DB에 등록된 사업명 목록(중복 제거) 반환
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT COALESCE(business,'') FROM events WHERE COALESCE(business,'') <> '' ORDER BY 1;")
    rows = [r[0] for r in cur.fetchall()]
    cur.close()
    conn.close()
    return jsonify(rows)


@app.post("/api/events")
def api_create_events():
    """
    Body(JSON):
      start: YYYY-MM-DD
      end: YYYY-MM-DD
      business, course, time, people, place, admin (optional)
    기간 입력 시 날짜별로 개별 row 생성(=추후 날짜별 수정/삭제 가능)
    """
    data = request.get_json(force=True, silent=True) or {}
    start_s = (data.get("start") or "").strip()
    end_s = (data.get("end") or "").strip()

    if not start_s:
        return jsonify({"error": "start가 필요합니다."}), 400
    if not end_s:
        end_s = start_s

    try:
        d1 = _parse_date(start_s)
        d2 = _parse_date(end_s)
    except Exception:
        return jsonify({"error": "날짜 형식은 YYYY-MM-DD 입니다."}), 400

    business = (data.get("business") or "").strip()
    course = (data.get("course") or "").strip()
    time_ = (data.get("time") or "").strip()
    people = (data.get("people") or "").strip()
    place = (data.get("place") or "").strip()
    admin = (data.get("admin") or "").strip()

    conn = get_conn()
    cur = conn.cursor()

    created_ids = []
    for d in _daterange(d1, d2):
        cur.execute(
            """
            INSERT INTO events (event_date, business, course, time, people, place, admin)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id;
            """,
            (d, business or None, course or None, time_ or None, people or None, place or None, admin or None),
        )
        created_ids.append(cur.fetchone()[0])

    conn.commit()
    cur.close()
    conn.close()

    return jsonify({"created": len(created_ids), "ids": created_ids}), 201


@app.put("/api/events/<int:event_id>")
def api_update_event(event_id: int):
    """
    개별 날짜 이벤트 1건 수정
    """
    data = request.get_json(force=True, silent=True) or {}

    fields = {}
    for k in ["business", "course", "time", "people", "place", "admin"]:
        if k in data:
            v = (data.get(k) or "").strip()
            fields[k] = v if v != "" else None

    # 날짜 수정도 가능하게(옵션)
    if "event_date" in data:
        try:
            fields["event_date"] = _parse_date((data.get("event_date") or "").strip())
        except Exception:
            return jsonify({"error": "event_date 형식은 YYYY-MM-DD 입니다."}), 400

    if not fields:
        return jsonify({"error": "수정할 값이 없습니다."}), 400

    sets = []
    params = []
    for k, v in fields.items():
        sets.append(f"{k} = %s")
        params.append(v)
    params.append(event_id)

    sql = f"UPDATE events SET {', '.join(sets)} WHERE id = %s RETURNING id;"

    conn = get_conn()
    cur = conn.cursor()
    cur.execute(sql, params)
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if not row:
        return jsonify({"error": "해당 id를 찾을 수 없습니다."}), 404

    return jsonify({"ok": True, "id": event_id})


@app.delete("/api/events/<int:event_id>")
def api_delete_event(event_id: int):
    """
    개별 날짜 이벤트 1건 삭제
    """
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("DELETE FROM events WHERE id = %s RETURNING id;", (event_id,))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if not row:
        return jsonify({"error": "해당 id를 찾을 수 없습니다."}), 404

    return jsonify({"ok": True, "deleted_id": event_id})


# -----------------------------
# 프론트(단일 파일)
# -----------------------------
def _html() -> str:
    return f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
  <title>포항산학 월별일정</title>
  <style>
    :root {{
      --bg: #ffffff;
      --text: #111;
      --muted: #666;
      --line: #d9d9d9;
      --cardLine: rgba(0,0,0,.10);
      --shadow: 0 8px 24px rgba(0,0,0,.08);
      --radius: 14px;
    }}

    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: system-ui, -apple-system, Segoe UI, Roboto, Apple SD Gothic Neo, Noto Sans KR, sans-serif;
    }}

    .wrap {{
      max-width: 1100px;
      margin: 0 auto;
      padding: 14px 12px 28px;
    }}

    header {{
      text-align: center;
      padding: 12px 0 8px;
    }}
    .title {{
      font-size: 40px;
      font-weight: 900;
      letter-spacing: -1px;
      margin: 0;
    }}
    .subtitle {{
      font-size: 28px;
      font-weight: 900;
      margin: 12px 0 8px;
    }}

    .toolbar {{
      display: grid;
      grid-template-columns: 1fr;
      gap: 10px;
      margin: 10px auto 14px;
    }}

    .row {{
      display: flex;
      gap: 8px;
      justify-content: center;
      align-items: center;
      flex-wrap: wrap;
    }}

    .row.right {{
      justify-content: flex-end;
    }}

    .btn {{
      border: 1px solid #aaa;
      background: #f6f6f6;
      padding: 10px 14px;
      border-radius: 8px;
      font-weight: 700;
      cursor: pointer;
      user-select: none;
    }}
    .btn:active {{ transform: translateY(1px); }}
    .btn.primary {{
      background: #f0f0ff;
      border-color: #8b8bff;
    }}

    select, input {{
      border: 1px solid #aaa;
      background: #fff;
      padding: 10px 12px;
      border-radius: 8px;
      font-size: 16px;
    }}

    .calendarShell {{
      border: 1px solid var(--line);
      border-radius: 12px;
      overflow: hidden;
      background: #fff;
    }}

    .dow {{
      display: grid;
      grid-template-columns: repeat(7, 1fr);
      border-bottom: 1px solid var(--line);
      background: #fafafa;
    }}
    .dow div {{
      padding: 10px 8px;
      text-align: center;
      font-weight: 900;
    }}
    .dow .sun {{ color: #d40000; }}
    .dow .sat {{ color: #0055d4; }}

    .grid {{
      display: grid;
      grid-template-columns: repeat(7, 1fr);
    }}

    .cell {{
      min-height: 110px;
      border-right: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      padding: 8px;
      position: relative;
      background: #fff;
    }}
    .grid .cell:nth-child(7n) {{ border-right: 0; }}
    .grid .cell.lastRow {{ border-bottom: 0; }}

    .dateNum {{
      font-weight: 900;
      font-size: 16px;
      margin-bottom: 6px;
    }}
    .dateNum.sun {{ color: #d40000; }}
    .dateNum.sat {{ color: #0055d4; }}
    .dateNum.muted {{ color: #aaa; }}

    /* ✅ 모바일 가독성 핵심: 이벤트를 2열로 배치(공간 활용) */
    .eventsGrid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 6px;
      align-items: start;
    }}

    .event {{
      border: 1px solid var(--cardLine);
      border-radius: 12px;
      padding: 8px 8px;
      box-shadow: 0 1px 0 rgba(0,0,0,.03);
      cursor: pointer;
      overflow: hidden;
    }}
    .event .b {{
      font-weight: 900;
      font-size: 14px;
      line-height: 1.2;
      margin-bottom: 4px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}
    .event .line {{
      font-size: 12px;
      line-height: 1.25;
      color: #111;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }}

    .emptyHint {{
      color: #bbb;
      font-size: 12px;
    }}

    /* 모달 */
    .backdrop {{
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,.35);
      display: none;
      align-items: center;
      justify-content: center;
      padding: 14px;
      z-index: 999;
    }}
    .modal {{
      width: min(560px, 100%);
      background: #fff;
      border-radius: 16px;
      box-shadow: var(--shadow);
      overflow: hidden;
    }}
    .modalHeader {{
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }}
    .modalTitle {{
      font-weight: 900;
      font-size: 18px;
      margin: 0;
    }}
    .modalBody {{
      padding: 14px 16px 6px;
    }}
    .modalGrid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }}
    .modalGrid .full {{ grid-column: 1 / -1; }}
    .modalFooter {{
      padding: 12px 16px 16px;
      display: flex;
      gap: 8px;
      justify-content: flex-end;
      border-top: 1px solid var(--line);
    }}
    .danger {{
      background: #fff1f1;
      border-color: #ff9a9a;
    }}

    .detailBox {{
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 12px;
      background: #fafafa;
      margin-top: 10px;
      font-size: 14px;
      line-height: 1.35;
    }}
    .detailBox .k {{ color: var(--muted); font-weight: 800; }}

    /* ✅ 모바일 튜닝 */
    @media (max-width: 520px) {{
      .wrap {{ padding: 10px 8px 22px; }}
      .title {{ font-size: 34px; }}
      .subtitle {{ font-size: 24px; }}
      .cell {{ min-height: 120px; padding: 8px 6px; }}
      .eventsGrid {{
        grid-template-columns: 1fr; /* 너무 좁으면 1열이 가독성 더 좋음 */
      }}
      .event .b {{ font-size: 15px; }}
      .event .line {{ font-size: 13px; }}
      select, input {{ width: 100%; }}
      .row {{ width: 100%; }}
      .row.right {{ justify-content: center; }}
      .btn {{ width: auto; }}
    }}

    /* ✅ 프린트에서 색이 날아가는 문제 대응 */
    @media print {{
      * {{
        -webkit-print-color-adjust: exact !important;
        print-color-adjust: exact !important;
      }}
      .toolbar, .btn, .backdrop {{ display: none !important; }}
      .wrap {{ max-width: none; padding: 0; }}
      header {{ padding: 0 0 10px; }}
      .calendarShell {{ border: 0; }}
      .cell {{ min-height: 90px; }}
    }}
  </style>
</head>
<body>
  <div class="wrap">
    <header>
      <h1 class="title">포항산학 월별일정</h1>
      <div class="subtitle" id="monthTitle">-</div>
    </header>

    <div class="toolbar">
      <div class="row">
        <button class="btn" id="prevBtn">◀ 이전</button>
        <button class="btn" id="nextBtn">다음 ▶</button>
        <button class="btn" id="modeMonth">월별</button>
        <button class="btn" id="modeWeek">주별</button>
      </div>

      <div class="row">
        <label style="font-weight:900;">사업명:</label>
        <select id="businessFilter">
          <option value="전체">전체</option>
        </select>
        <button class="btn" id="resetFilter">필터 초기화</button>
      </div>

      <div class="row right">
        <button class="btn primary" id="addBtn">+ 일정 추가하기</button>
      </div>
    </div>

    <div class="calendarShell">
      <div class="dow">
        <div class="sun">일</div><div>월</div><div>화</div><div>수</div><div>목</div><div>금</div><div class="sat">토</div>
      </div>
      <div class="grid" id="grid"></div>
    </div>
  </div>

  <!-- 모달: 추가/수정 -->
  <div class="backdrop" id="formBackdrop">
    <div class="modal">
      <div class="modalHeader">
        <h3 class="modalTitle" id="formTitle">일정 추가</h3>
        <button class="btn" id="closeForm">닫기</button>
      </div>
      <div class="modalBody">
        <div class="modalGrid">
          <div>
            <label style="font-weight:900; display:block; margin-bottom:6px;">시작일</label>
            <input type="date" id="startDate"/>
          </div>
          <div>
            <label style="font-weight:900; display:block; margin-bottom:6px;">종료일</label>
            <input type="date" id="endDate"/>
          </div>

          <div class="full">
            <label style="font-weight:900; display:block; margin-bottom:6px;">사업명</label>
            <div style="display:flex; gap:8px; flex-wrap:wrap;">
              <select id="businessSelect" style="flex:1; min-width:180px;"></select>
              <input id="businessNew" placeholder="신규 사업명 입력(선택)" style="flex:1; min-width:180px;"/>
            </div>
            <div style="color:#777; font-size:12px; margin-top:6px;">
              * 신규 사업명은 위 입력칸에 적고 저장하면 자동으로 목록에 반영됩니다.
            </div>
          </div>

          <div class="full">
            <label style="font-weight:900; display:block; margin-bottom:6px;">과정</label>
            <input id="course" placeholder="예) 파이썬 / 용접 / 홍보행사 등"/>
          </div>

          <div>
            <label style="font-weight:900; display:block; margin-bottom:6px;">시간</label>
            <input id="time" placeholder="예) 09:00~18:00"/>
          </div>
          <div>
            <label style="font-weight:900; display:block; margin-bottom:6px;">인원</label>
            <input id="people" placeholder="예) 20"/>
          </div>

          <div class="full">
            <label style="font-weight:900; display:block; margin-bottom:6px;">장소</label>
            <input id="place" placeholder="예) 본관 3층 / 2강의실"/>
          </div>

          <div class="full">
            <label style="font-weight:900; display:block; margin-bottom:6px;">행정</label>
            <input id="admin" placeholder="예) 담당자명"/>
          </div>
        </div>

        <div class="detailBox" id="editHint" style="display:none;">
          <div><span class="k">안내</span> : 수정은 <b>선택한 날짜(1건)</b>만 변경됩니다.</div>
          <div style="margin-top:6px; color:#777; font-size:12px;">
            * 기간으로 등록한 일정은 날짜별로 개별 인식되므로, 6일/7일 담당자를 다르게 수정할 수 있습니다.
          </div>
        </div>
      </div>
      <div class="modalFooter">
        <button class="btn danger" id="deleteBtn" style="display:none;">이 날 삭제</button>
        <button class="btn primary" id="saveBtn">저장</button>
      </div>
    </div>
  </div>

<script>
  // -----------------------------
  // 상태
  // -----------------------------
  let mode = "month"; // month | week
  let current = new Date(); // 현재 기준일
  let events = []; // 현재 범위 events
  let businessFilter = "전체";
  let editingId = null; // 수정중 이벤트 id(날짜 1건)

  // 기본 사업 목록(원하면 여기만 추가하면 됨)
  const presetBusinesses = ["대관","지산맞","일학습","배터리","기회발전","사업주","행사"];

  // -----------------------------
  // 색상
  // -----------------------------
  const colorMap = {{
    "지산맞": "#dff3ff",
    "대관": "#ffe4ea",
    "일학습": "#e7ffe1",
    "배터리": "#fff2cf",
    "기회발전": "#efe5ff",
    "사업주": "#ffe8d2",
    "행사": "#ffd6f2"
  }};

  function hashColor(str) {{
    // 등록된 새 사업명도 안정적으로 같은 파스텔색 나오게
    const h = [...str].reduce((a,c)=>a + c.charCodeAt(0), 0) % 360;
return "hsl(" + h + " 70% 88%)";
  }}

  function getBg(biz) {{
    if (!biz) return "#f4f4f4";
    return colorMap[biz] || hashColor(biz);
  }}

  // -----------------------------
  // 날짜 유틸
  // -----------------------------
  function ymd(d) {{
    const y = d.getFullYear();
    const m = String(d.getMonth()+1).padStart(2,"0");
    const da = String(d.getDate()).padStart(2,"0");
    return `${{y}}-${{m}}-${{da}}`;
  }}

  function parseYmd(s) {{
    const [y,m,d] = s.split("-").map(Number);
    return new Date(y, m-1, d);
  }}

  function startOfMonth(d) {{
    return new Date(d.getFullYear(), d.getMonth(), 1);
  }}

  function endOfMonth(d) {{
    return new Date(d.getFullYear(), d.getMonth()+1, 0);
  }}

  function startOfWeek(d) {{
    // 일요일 시작
    const x = new Date(d);
    x.setHours(0,0,0,0);
    const day = x.getDay();
    x.setDate(x.getDate() - day);
    return x;
  }}

  function addDays(d, n) {{
    const x = new Date(d);
    x.setDate(x.getDate() + n);
    return x;
  }}

  // -----------------------------
  // DOM
  // -----------------------------
  const $monthTitle = document.getElementById("monthTitle");
  const $grid = document.getElementById("grid");
  const $businessFilter = document.getElementById("businessFilter");

  const $formBackdrop = document.getElementById("formBackdrop");
  const $formTitle = document.getElementById("formTitle");
  const $closeForm = document.getElementById("closeForm");
  const $saveBtn = document.getElementById("saveBtn");
  const $deleteBtn = document.getElementById("deleteBtn");
  const $editHint = document.getElementById("editHint");

  const $startDate = document.getElementById("startDate");
  const $endDate = document.getElementById("endDate");
  const $businessSelect = document.getElementById("businessSelect");
  const $businessNew = document.getElementById("businessNew");
  const $course = document.getElementById("course");
  const $time = document.getElementById("time");
  const $people = document.getElementById("people");
  const $place = document.getElementById("place");
  const $admin = document.getElementById("admin");

  // -----------------------------
  // API
  // -----------------------------
  async function apiGet(url) {{
    const r = await fetch(url);
    if (!r.ok) throw new Error(await r.text());
    return await r.json();
  }}
  async function apiSend(url, method, body) {{
    const r = await fetch(url, {{
      method,
      headers: {{ "Content-Type": "application/json" }},
      body: body ? JSON.stringify(body) : null
    }});
    if (!r.ok) throw new Error(await r.text());
    return await r.json();
  }}

  // -----------------------------
  // 사업 목록 갱신
  // -----------------------------
  async function refreshBusinesses() {{
    const dbBusinesses = await apiGet("/api/businesses");
    const all = Array.from(new Set([...presetBusinesses, ...dbBusinesses].filter(x=>x && x.trim() !== "")));

    // filter select
    $businessFilter.innerHTML = `<option value="전체">전체</option>` + all.map(b=>`<option value="${{escapeHtml(b)}}">${{escapeHtml(b)}}</option>`).join("");

    // form select
    $businessSelect.innerHTML = all.map(b=>`<option value="${{escapeHtml(b)}}">${{escapeHtml(b)}}</option>`).join("");
  }}

  // -----------------------------
  // 렌더 범위 계산 + 이벤트 로드
  // -----------------------------
  async function loadAndRender() {{
    const range = getViewRange();
    const from = ymd(range.from);
    const to = ymd(range.to);

    const q = new URLSearchParams();
    q.set("from", from);
    q.set("to", to);
    if (businessFilter && businessFilter !== "전체") q.set("business", businessFilter);

    events = await apiGet("/api/events?" + q.toString());

    renderTitle();
    renderGrid(range);
  }}

  function renderTitle() {{
    const y = current.getFullYear();
    const m = current.getMonth() + 1;
    if (mode === "month") {{
      $monthTitle.textContent = `${{y}}년 ${{m}}월`;
    }} else {{
      const s = startOfWeek(current);
      const e = addDays(s, 6);
      $monthTitle.textContent = `${{y}}년 ${{m}}월 (주별: ${{s.getMonth()+1}}/${{s.getDate()}} ~ ${{e.getMonth()+1}}/${{e.getDate()}})`;
    }}
  }}

  function getViewRange() {{
    if (mode === "month") {{
      const s = startOfMonth(current);
      const e = endOfMonth(current);
      // 달력 그리드: 시작 요일 맞추기
      const gridStart = startOfWeek(s);
      const gridEnd = addDays(startOfWeek(addDays(e, 7 - (e.getDay()+1))), 6);
      // 위 계산이 헷갈리면 간단히: e가 속한 주의 토요일까지
      const endWeekStart = startOfWeek(e);
      const gridEnd2 = addDays(endWeekStart, 6);
      return {{ from: gridStart, to: gridEnd2 }};
    }} else {{
      const s = startOfWeek(current);
      const e = addDays(s, 6);
      return {{ from: s, to: e }};
    }}
  }}

  // -----------------------------
  // 달력 렌더
  // -----------------------------
  function renderGrid(range) {{
    // 날짜 배열 생성
    const days = [];
    for (let d = new Date(range.from); d <= range.to; d = addDays(d, 1)) {{
      days.push(new Date(d));
    }}

    // 월별이면 6주(최대) / 주별이면 1주
    $grid.innerHTML = "";
    $grid.style.gridTemplateRows = mode === "week" ? "repeat(1, 1fr)" : "";

    const curMonth = current.getMonth();

    days.forEach((d, idx) => {{
      const cell = document.createElement("div");
      cell.className = "cell";
      // 마지막 줄 border 제거용
      if (mode === "month") {{
        const remaining = days.length - idx;
        if (remaining <= 7) cell.classList.add("lastRow");
      }} else {{
        cell.classList.add("lastRow");
      }}

      const dn = document.createElement("div");
      dn.className = "dateNum";
      const dayOfWeek = d.getDay();
      if (dayOfWeek === 0) dn.classList.add("sun");
      if (dayOfWeek === 6) dn.classList.add("sat");
      if (mode === "month" && d.getMonth() !== curMonth) dn.classList.add("muted");
      dn.textContent = d.getDate();
      cell.appendChild(dn);

      const dayKey = ymd(d);
      const dayEvents = events.filter(e => e.event_date === dayKey);

      const box = document.createElement("div");
      box.className = "eventsGrid";

      if (dayEvents.length === 0) {{
        const empty = document.createElement("div");
        empty.className = "emptyHint";
        empty.textContent = "";
        // 박스는 유지하되 텍스트는 공백(깔끔)
        box.appendChild(empty);
      }} else {{
        dayEvents.forEach(ev => {{
          const el = document.createElement("div");
          el.className = "event";
          el.style.background = getBg(ev.business);

          // 타이틀(사업명)
          const b = document.createElement("div");
          b.className = "b";
          b.textContent = ev.business || "(미지정)";
          el.appendChild(b);

          // 아래 라인: 공란 항목은 아예 표시 안 함
          const lines = [];
          if (ev.course) lines.push(`과정: ${{ev.course}}`);
          if (ev.time) lines.push(`시간: ${{ev.time}}`);
          if (ev.people) lines.push(`인원: ${{ev.people}}`);
          if (ev.place) lines.push(`장소: ${{ev.place}}`);
          if (ev.admin) lines.push(`행정: ${{ev.admin}}`);

          // 이벤트 카드에서는 너무 길면 2줄까지만
          const show = lines.slice(0, 2);
          show.forEach(t => {{
            const l = document.createElement("div");
            l.className = "line";
            l.textContent = t;
            el.appendChild(l);
          }});

          el.addEventListener("click", () => openEdit(ev));
          box.appendChild(el);
        }});
      }}

      cell.appendChild(box);
      $grid.appendChild(cell);
    }});
  }}

  // -----------------------------
  // 모달(추가/수정/삭제)
  // -----------------------------
  function openAdd(prefillDate=null) {{
    editingId = null;
    $formTitle.textContent = "일정 추가";
    $deleteBtn.style.display = "none";
    $editHint.style.display = "none";

    const today = prefillDate ? new Date(prefillDate) : new Date();
    const s = ymd(today);
    $startDate.value = s;
    $endDate.value = s;

    $businessNew.value = "";
    $course.value = "";
    $time.value = "";
    $people.value = "";
    $place.value = "";
    $admin.value = "";

    // 기본 선택
    if ($businessSelect.options.length > 0) {{
      $businessSelect.value = $businessSelect.options[0].value;
    }}

    showForm(true);
  }}

  function openEdit(ev) {{
    editingId = ev.id;
    $formTitle.textContent = "일정 수정(선택한 날짜 1건)";
    $deleteBtn.style.display = "inline-block";
    $editHint.style.display = "block";

    $startDate.value = ev.event_date;
    $endDate.value = ev.event_date; // 수정은 1건만
    $businessNew.value = "";
    $course.value = ev.course || "";
    $time.value = ev.time || "";
    $people.value = ev.people || "";
    $place.value = ev.place || "";
    $admin.value = ev.admin || "";

    // 사업명 선택
    const biz = ev.business || "";
    let found = false;
    [...$businessSelect.options].forEach(o => {{
      if (o.value === biz) found = true;
    }});
    if (found) {{
      $businessSelect.value = biz;
    }} else {{
      // 목록에 없는 경우 신규칸에 넣어주고, 셀렉트는 첫번째
      $businessNew.value = biz;
      if ($businessSelect.options.length > 0) $businessSelect.value = $businessSelect.options[0].value;
    }}

    showForm(true);
  }}

  function showForm(on) {{
    $formBackdrop.style.display = on ? "flex" : "none";
  }}

  function pickBusiness() {{
    const n = ($businessNew.value || "").trim();
    if (n) return n;
    return ($businessSelect.value || "").trim();
  }}

  async function saveForm() {{
    const s = $startDate.value;
    const e = $endDate.value || s;
    const business = pickBusiness();
    const payload = {{
      start: s,
      end: e,
      business: business,
      course: $course.value,
      time: $time.value,
      people: $people.value,
      place: $place.value,
      admin: $admin.value
    }};

    try {{
      if (editingId) {{
        // 수정은 1건만(날짜 개별 인식)
        const upd = {{
          event_date: s, // 혹시 바꿨다면
          business: business,
          course: $course.value,
          time: $time.value,
          people: $people.value,
          place: $place.value,
          admin: $admin.value
        }};
        await apiSend(`/api/events/${{editingId}}`, "PUT", upd);
      }} else {{
        await apiSend("/api/events", "POST", payload);
      }}

      // 신규 사업명 반영
      await refreshBusinesses();
      showForm(false);
      await loadAndRender();
    }} catch (err) {{
      alert("저장 중 오류가 발생했습니다.\\n" + err);
    }}
  }}

  async function deleteOneDay() {{
    if (!editingId) return;
    if (!confirm("이 날 일정(1건)을 삭제할까요?")) return;

    try {{
      await apiSend(`/api/events/${{editingId}}`, "DELETE");
      showForm(false);
      await loadAndRender();
    }} catch (err) {{
      alert("삭제 중 오류가 발생했습니다.\\n" + err);
    }}
  }}

  // -----------------------------
  // 이벤트 바인딩
  // -----------------------------
  document.getElementById("prevBtn").addEventListener("click", async () => {{
    if (mode === "month") {{
      current = new Date(current.getFullYear(), current.getMonth()-1, 1);
    }} else {{
      current = addDays(current, -7);
    }}
    await loadAndRender();
  }});

  document.getElementById("nextBtn").addEventListener("click", async () => {{
    if (mode === "month") {{
      current = new Date(current.getFullYear(), current.getMonth()+1, 1);
    }} else {{
      current = addDays(current, 7);
    }}
    await loadAndRender();
  }});

  document.getElementById("modeMonth").addEventListener("click", async () => {{
    mode = "month";
    document.getElementById("modeMonth").classList.add("primary");
    document.getElementById("modeWeek").classList.remove("primary");
    await loadAndRender();
  }});

  document.getElementById("modeWeek").addEventListener("click", async () => {{
    mode = "week";
    document.getElementById("modeWeek").classList.add("primary");
    document.getElementById("modeMonth").classList.remove("primary");
    await loadAndRender();
  }});

  document.getElementById("addBtn").addEventListener("click", () => openAdd(current));

  $closeForm.addEventListener("click", () => showForm(false));
  $formBackdrop.addEventListener("click", (e) => {{
    if (e.target === $formBackdrop) showForm(false);
  }});

  $saveBtn.addEventListener("click", saveForm);
  $deleteBtn.addEventListener("click", deleteOneDay);

  $businessFilter.addEventListener("change", async () => {{
    businessFilter = $businessFilter.value;
    await loadAndRender();
  }});

  document.getElementById("resetFilter").addEventListener("click", async () => {{
    businessFilter = "전체";
    $businessFilter.value = "전체";
    await loadAndRender();
  }});

  // XSS 방지용(간단)
  function escapeHtml(s) {{
    return String(s)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }}

  // -----------------------------
  // 초기 로드
  // -----------------------------
  (async function init() {{
    try {{
      // 모드 버튼 표시
      document.getElementById("modeMonth").classList.add("primary");

      await refreshBusinesses();
      await loadAndRender();
    }} catch (err) {{
      alert("초기 로딩 오류: " + err);
    }}
  }})();
</script>
</body>
</html>
"""


@app.get("/")
def index():
    return Response(_html(), mimetype="text/html")


# -----------------------------
# 로컬 실행용
# -----------------------------
if __name__ == "__main__":
    # 로컬에서는 FLASK_RUN_HOST 같은 환경변수로 바꿔도 됨
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=True)

