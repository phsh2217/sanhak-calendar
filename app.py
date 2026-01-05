import os
import json
import uuid
from datetime import datetime, date, timedelta
from typing import Optional, List, Dict, Any

import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify, Response

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Error: DATABASE_URL 환경변수가 설정되어 있지 않습니다.")


# ---------------------------
# DB Helpers
# ---------------------------
def get_db():
    # Render Postgres URL 그대로 사용
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn


def parse_ymd(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def date_range(d1: date, d2: date):
    cur = d1
    step = timedelta(days=1)
    while cur <= d2:
        yield cur
        cur += step


def split_excluded(excluded: Optional[str]) -> List[str]:
    if not excluded:
        return []
    if isinstance(excluded, list):
        # 프론트에서 리스트로 보내는 경우
        return sorted(set([str(x).strip() for x in excluded if str(x).strip()]))
    # 문자열(콤마)로 오는 경우
    items = [x.strip() for x in str(excluded).split(",") if x.strip()]
    return sorted(set(items))


def safe_str(v) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def init_db():
    """
    최종 스키마: events 테이블은 '날짜 1건 = row 1개'
    event_date DATE NOT NULL
    """

    conn = get_db()
    cur = conn.cursor()

    # 1) 최종 테이블 생성
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
            group_id UUID,
            created_at TIMESTAMPTZ DEFAULT NOW(),
            updated_at TIMESTAMPTZ DEFAULT NOW()
        );
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_event_date ON events(event_date);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_events_group_id ON events(group_id);")

    # 2) 구버전 테이블( start / end )가 남아있으면 가능한 범위에서 마이그레이션
    #    - events 테이블에 'start' 컬럼이 존재하는지 확인
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name='events'
        """
    )
    cols = {r["column_name"] for r in cur.fetchall()}

    # 구버전 컬럼이 섞여있는 상태(예: start, end, excluded_dates)가면 마이그레이션 시도
    # (이미 최종 events를 만들었으니, 구버전이 '별도 테이블'로 존재하는 경우만 안전하게 처리 가능)
    # → 사용자가 과거 코드로 만든 테이블명이 동일(events)이라면,
    #    이미 여기서 CREATE TABLE이 실행되며 충돌은 없고 컬럼이 섞인 상태가 될 수 있음.
    #    그 경우엔 '구버전 컬럼'들을 읽어 새 테이블로 옮긴 뒤 재구성한다.
    if "start" in cols or "excluded_dates" in cols or "end" in cols or '"end"' in cols:
        # events_v2 만들어서 옮긴 뒤 교체
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS events_v2 (
                id SERIAL PRIMARY KEY,
                event_date DATE NOT NULL,
                business TEXT,
                course TEXT,
                time TEXT,
                people TEXT,
                place TEXT,
                admin TEXT,
                group_id UUID,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_v2_event_date ON events_v2(event_date);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_events_v2_group_id ON events_v2(group_id);")

        # 구버전 데이터 읽기 (가능한 케이스만)
        # end 컬럼은 과거에 "end"로 만들었을 수 있어서 둘 다 시도
        # start/end가 없으면(이미 v2 구조) 마이그레이션 skip
        if "start" in cols and ("end" in cols or "end" in cols or '"end"' in cols):
            try:
                # end가 예약어라 "end"로 저장된 경우를 우선 시도
                cur.execute(
                    """
                    SELECT id, start, "end" AS end, business, course, time, people, place, admin, excluded_dates
                    FROM events
                    """
                )
            except Exception:
                conn.rollback()
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT id, start, end AS end, business, course, time, people, place, admin, excluded_dates
                    FROM events
                    """
                )

            rows = cur.fetchall()
            for r in rows:
                try:
                    s = parse_ymd(r["start"])
                    e = parse_ymd(r["end"])
                except Exception:
                    continue

                excluded_list = split_excluded(r.get("excluded_dates"))
                g = uuid.uuid4()

                for d in date_range(s, e):
                    ds = d.strftime("%Y-%m-%d")
                    if ds in excluded_list:
                        continue
                    cur.execute(
                        """
                        INSERT INTO events_v2(event_date, business, course, time, people, place, admin, group_id)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                        """,
                        (
                            d,
                            r.get("business"),
                            r.get("course"),
                            r.get("time"),
                            r.get("people"),
                            r.get("place"),
                            r.get("admin"),
                            g,
                        ),
                    )

            # 교체: 기존 events 백업 후 v2를 events로
            # (권한/락 문제시 실패 가능 → 그땐 그냥 v2만 남아도 서비스는 살아야 하므로 try)
            try:
                cur.execute("ALTER TABLE events RENAME TO events_old;")
                cur.execute("ALTER TABLE events_v2 RENAME TO events;")
                cur.execute("DROP TABLE IF EXISTS events_old;")
            except Exception:
                conn.rollback()

    conn.commit()
    conn.close()


init_db()


# ---------------------------
# API
# ---------------------------
@app.errorhandler(Exception)
def handle_exception(e):
    # 프론트가 JSON 파싱 실패하지 않도록 항상 JSON으로 응답
    return jsonify({"ok": False, "error": str(e)}), 500


@app.get("/api/events")
def api_get_events():
    """
    ?start=YYYY-MM-DD&end=YYYY-MM-DD (옵션)
    """
    start = request.args.get("start")
    end = request.args.get("end")

    conn = get_db()
    cur = conn.cursor()

    if start and end:
        s = parse_ymd(start)
        e = parse_ymd(end)
        cur.execute(
            """
            SELECT id, event_date, business, course, time, people, place, admin, group_id
            FROM events
            WHERE event_date BETWEEN %s AND %s
            ORDER BY event_date ASC, id ASC
            """,
            (s, e),
        )
    else:
        cur.execute(
            """
            SELECT id, event_date, business, course, time, people, place, admin, group_id
            FROM events
            ORDER BY event_date ASC, id ASC
            """
        )

    rows = cur.fetchall()
    conn.close()

    # 프론트 호환: start/end 대신 start로 event_date 문자열 제공
    items = []
    for r in rows:
        items.append(
            {
                "id": r["id"],
                "date": r["event_date"].strftime("%Y-%m-%d"),
                "business": r.get("business") or "",
                "course": r.get("course") or "",
                "time": r.get("time") or "",
                "people": r.get("people") or "",
                "place": r.get("place") or "",
                "admin": r.get("admin") or "",
                "group_id": str(r["group_id"]) if r.get("group_id") else None,
            }
        )

    return jsonify({"ok": True, "events": items})


@app.post("/api/events")
def api_create_events():
    """
    기간 등록:
    {
      "start": "YYYY-MM-DD",
      "end": "YYYY-MM-DD",
      "business": "...",
      "course": "...",
      "time": "...",
      "people": "...",
      "place": "...",
      "admin": "...",
      "excluded_dates": ["YYYY-MM-DD", ...] 또는 "YYYY-MM-DD,YYYY-MM-DD"
    }
    => 날짜별 row로 분해 저장
    """
    data = request.get_json(silent=True) or {}

    start = safe_str(data.get("start"))
    end = safe_str(data.get("end"))
    if not start or not end:
        return jsonify({"ok": False, "error": "시작/종료일은 YYYY-MM-DD 형식으로 입력하세요."}), 400

    try:
        s = parse_ymd(start)
        e = parse_ymd(end)
    except Exception:
        return jsonify({"ok": False, "error": "시작/종료일은 YYYY-MM-DD 형식으로 입력하세요."}), 400

    if e < s:
        return jsonify({"ok": False, "error": "종료일은 시작일보다 빠를 수 없습니다."}), 400

    excluded_list = split_excluded(data.get("excluded_dates"))

    business = safe_str(data.get("business"))
    course = safe_str(data.get("course"))
    time = safe_str(data.get("time"))
    people = safe_str(data.get("people"))
    place = safe_str(data.get("place"))
    admin = safe_str(data.get("admin"))

    g = uuid.uuid4()

    conn = get_db()
    cur = conn.cursor()

    created_ids = []
    for d in date_range(s, e):
        ds = d.strftime("%Y-%m-%d")
        if ds in excluded_list:
            continue
        cur.execute(
            """
            INSERT INTO events(event_date, business, course, time, people, place, admin, group_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id
            """,
            (d, business, course, time, people, place, admin, g),
        )
        created_ids.append(cur.fetchone()["id"])

    conn.commit()
    conn.close()

    return jsonify({"ok": True, "created_ids": created_ids, "group_id": str(g)}), 201


@app.put("/api/events/<int:event_id>")
def api_update_event(event_id: int):
    """
    날짜 1건(row 1개) 수정
    {
      "business": "...",
      "course": "...",
      "time": "...",
      "people": "...",
      "place": "...",
      "admin": "...",
      "date": "YYYY-MM-DD" (옵션: 날짜 이동)
    }
    """
    data = request.get_json(silent=True) or {}

    new_date = safe_str(data.get("date"))
    if new_date:
        try:
            d = parse_ymd(new_date)
        except Exception:
            return jsonify({"ok": False, "error": "date는 YYYY-MM-DD 형식이어야 합니다."}), 400
    else:
        d = None

    fields = {
        "business": safe_str(data.get("business")),
        "course": safe_str(data.get("course")),
        "time": safe_str(data.get("time")),
        "people": safe_str(data.get("people")),
        "place": safe_str(data.get("place")),
        "admin": safe_str(data.get("admin")),
    }

    sets = []
    vals = []
    if d is not None:
        sets.append("event_date = %s")
        vals.append(d)

    for k, v in fields.items():
        sets.append(f"{k} = %s")
        vals.append(v)

    sets.append("updated_at = NOW()")

    vals.append(event_id)

    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        f"""
        UPDATE events
        SET {", ".join(sets)}
        WHERE id = %s
        RETURNING id, event_date, business, course, time, people, place, admin, group_id
        """,
        tuple(vals),
    )
    row = cur.fetchone()
    conn.commit()
    conn.close()

    if not row:
        return jsonify({"ok": False, "error": "해당 이벤트를 찾을 수 없습니다."}), 404

    return jsonify(
        {
            "ok": True,
            "event": {
                "id": row["id"],
                "date": row["event_date"].strftime("%Y-%m-%d"),
                "business": row.get("business") or "",
                "course": row.get("course") or "",
                "time": row.get("time") or "",
                "people": row.get("people") or "",
                "place": row.get("place") or "",
                "admin": row.get("admin") or "",
                "group_id": str(row["group_id"]) if row.get("group_id") else None,
            },
        }
    )


@app.delete("/api/events/<int:event_id>")
def api_delete_event(event_id: int):
    """
    개별 날짜(row 1개) 삭제
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM events WHERE id=%s RETURNING id", (event_id,))
    row = cur.fetchone()
    conn.commit()
    conn.close()

    if not row:
        return jsonify({"ok": False, "error": "해당 이벤트를 찾을 수 없습니다."}), 404
    return jsonify({"ok": True})


@app.get("/api/businesses")
def api_businesses():
    """
    사업명 드롭다운을 위한 distinct 목록
    """
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT DISTINCT business
        FROM events
        WHERE business IS NOT NULL AND business <> ''
        ORDER BY business ASC
        """
    )
    rows = cur.fetchall()
    conn.close()
    items = [r["business"] for r in rows if r.get("business")]
    return jsonify({"ok": True, "businesses": items})


# ---------------------------
# Frontend (Single-file HTML)
# ---------------------------
def _html() -> str:
    # ⚠️ Python f-string 쓰면 JS의 ${}와 충돌(과거 NameError 원인)
    # 그래서 절대 f"" 쓰지 않고, 순수 문자열로 제공
    return r"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>포항산학 월별일정</title>
  <style>
    :root{
      --border:#d9d9d9;
      --text:#111;
      --muted:#666;
      --bg:#fff;
      --card-radius:14px;
    }
    body{margin:0; font-family:system-ui,-apple-system,Segoe UI,Roboto,Apple SD Gothic Neo,Noto Sans KR,sans-serif; color:var(--text); background:var(--bg);}
    .wrap{max-width:1200px; margin:0 auto; padding:20px 12px 40px;}
    h1{margin:10px 0 0; text-align:center; font-size:44px; letter-spacing:-1px;}
    h2{margin:6px 0 18px; text-align:center; font-size:34px; font-weight:800;}

    .toolbar{display:flex; flex-wrap:wrap; gap:10px; align-items:center; justify-content:center; margin:10px 0 12px;}
    .btn{border:1px solid #bdbdbd; background:#fff; padding:10px 14px; border-radius:10px; font-size:16px; cursor:pointer;}
    .btn:active{transform:translateY(1px);}
    .select{border:1px solid #bdbdbd; border-radius:10px; padding:10px 12px; font-size:16px; background:#fff;}
    .label{font-size:20px; font-weight:700; margin-right:6px;}
    .spacer{flex:1 1 auto;}
    .addWide{width:min(100%,760px); padding:14px 18px; font-size:18px; font-weight:800; border-radius:12px;}

    /* Calendar */
    .cal{border:1px solid var(--border); border-radius:12px; overflow:hidden; background:#fff;}
    .dow{display:grid; grid-template-columns:repeat(7,1fr); background:#fafafa; border-bottom:1px solid var(--border);}
    .dow div{padding:10px 0; text-align:center; font-weight:800;}
    .dow .sun{color:#d40000;}
    .dow .sat{color:#0066cc;}

    .grid{display:grid; grid-template-columns:repeat(7,1fr);}
    .cell{min-height:120px; border-right:1px solid var(--border); border-bottom:1px solid var(--border); padding:8px; position:relative;}
    .cell:nth-child(7n){border-right:none;}
    .daynum{font-weight:900; font-size:18px;}
    .daynum.sun{color:#d40000;}
    .daynum.sat{color:#0066cc;}

    /* events: 한 칸에 2개씩(자동 줄바꿈) */
    .events{margin-top:8px; display:flex; flex-wrap:wrap; gap:8px;}
    .ev{
      flex:0 0 calc(50% - 4px);
      box-sizing:border-box;
      border-radius:14px;
      padding:10px 10px;
      border:1px solid rgba(0,0,0,.08);
      background:var(--evbg, #f3f3f3);
      cursor:pointer;
      overflow:hidden;
    }
    .ev strong{display:block; font-size:18px; margin-bottom:6px;}
    .ev .line{font-size:14px; color:#222; line-height:1.25; white-space:nowrap; overflow:hidden; text-overflow:ellipsis;}
    .ev .muted{color:var(--muted);}

    /* Week view */
    .weekWrap{border:1px solid var(--border); border-radius:12px; overflow:hidden;}
    .weekRow{display:grid; grid-template-columns:120px 1fr; border-bottom:1px solid var(--border);}
    .weekRow:last-child{border-bottom:none;}
    .weekDate{padding:12px; font-weight:900; background:#fafafa; border-right:1px solid var(--border);}
    .weekList{padding:10px;}
    .weekList .events{margin-top:0}

    /* Modal */
    .modalBack{position:fixed; inset:0; background:rgba(0,0,0,.35); display:none; align-items:center; justify-content:center; padding:14px; z-index:50;}
    .modal{width:min(820px, 100%); background:#fff; border-radius:16px; padding:18px; box-shadow:0 10px 30px rgba(0,0,0,.2);}
    .modal h3{margin:0 0 12px; font-size:22px;}
    .formGrid{display:grid; grid-template-columns:1fr 1fr; gap:12px;}
    .field label{display:block; font-weight:800; margin:0 0 6px;}
    .field input, .field textarea{
      width:100%; box-sizing:border-box;
      border:1px solid #cfcfcf; border-radius:10px;
      padding:12px; font-size:16px;
    }
    .actions{display:flex; gap:10px; justify-content:flex-end; margin-top:14px;}
    .danger{border-color:#ffb3b3; background:#fff5f5;}
    .hint{font-size:13px; color:var(--muted); margin-top:6px;}

    /* Mobile 개선 */
    @media (max-width: 720px){
      .wrap{padding:14px 10px 26px;}
      h1{font-size:34px;}
      h2{font-size:26px;}
      .label{font-size:18px;}
      .btn,.select{font-size:15px; padding:10px 12px;}
      .addWide{font-size:17px;}
      .cell{min-height:110px; padding:7px;}
      .ev{flex:0 0 100%; padding:10px;}
      .ev strong{font-size:16px;}
      .ev .line{font-size:13px;}
      .formGrid{grid-template-columns:1fr;}
    }
  </style>
</head>

<body>
  <div class="wrap">
    <h1>포항산학 월별일정</h1>
    <h2 id="ymTitle">-</h2>

    <div class="toolbar">
      <button class="btn" id="prevBtn">◀ 이전</button>
      <button class="btn" id="nextBtn">다음 ▶</button>
      <button class="btn" id="monthBtn">월별</button>
      <button class="btn" id="weekBtn">주별</button>

      <span class="label">사업명:</span>
      <select class="select" id="bizFilter">
        <option value="">전체</option>
      </select>
      <button class="btn" id="resetFilterBtn">필터 초기화</button>
    </div>

    <div class="toolbar" style="margin-top:6px">
      <button class="btn addWide" id="openAddBtn">+ 일정 추가하기</button>
    </div>

    <div id="viewHost"></div>
  </div>

  <!-- Add modal (기간 등록) -->
  <div class="modalBack" id="addBack">
    <div class="modal">
      <h3>일정 추가(기간 등록)</h3>

      <div class="formGrid">
        <div class="field">
          <label>시작일 (YYYY-MM-DD)</label>
          <input type="date" id="addStart" />
        </div>
        <div class="field">
          <label>종료일 (YYYY-MM-DD)</label>
          <input type="date" id="addEnd" />
        </div>

        <div class="field" style="grid-column:1/-1">
          <label>사업명</label>
          <input type="text" id="addBusiness" placeholder="예: 지산맞, 일학습, 대관 등" />
        </div>

        <div class="field" style="grid-column:1/-1">
          <label>과정</label>
          <input type="text" id="addCourse" />
        </div>

        <div class="field">
          <label>시간</label>
          <input type="text" id="addTime" placeholder="예: 10:00~14:00" />
        </div>
        <div class="field">
          <label>인원</label>
          <input type="text" id="addPeople" placeholder="예: 10" />
        </div>

        <div class="field">
          <label>장소</label>
          <input type="text" id="addPlace" placeholder="예: 본관3층" />
        </div>
        <div class="field">
          <label>행정</label>
          <input type="text" id="addAdmin" placeholder="예: 담당자명" />
        </div>

        <div class="field" style="grid-column:1/-1">
          <label>제외할 날짜(선택)</label>
          <input type="text" id="addExcluded" placeholder="예: 2026-01-07,2026-01-08" />
          <div class="hint">기간 중 특정 날짜만 빼고 저장하고 싶을 때 사용</div>
        </div>
      </div>

      <div class="actions">
        <button class="btn" id="addCloseBtn">닫기</button>
        <button class="btn" id="addSaveBtn">저장</button>
      </div>
    </div>
  </div>

  <!-- Detail modal (개별 날짜 수정/삭제) -->
  <div class="modalBack" id="detailBack">
    <div class="modal">
      <h3>일정 상세</h3>

      <div class="formGrid">
        <div class="field">
          <label>날짜</label>
          <input type="date" id="dDate" />
        </div>
        <div class="field">
          <label>사업명</label>
          <input type="text" id="dBusiness" />
        </div>

        <div class="field" style="grid-column:1/-1">
          <label>과정</label>
          <input type="text" id="dCourse" />
        </div>

        <div class="field">
          <label>시간</label>
          <input type="text" id="dTime" />
        </div>
        <div class="field">
          <label>인원</label>
          <input type="text" id="dPeople" />
        </div>

        <div class="field">
          <label>장소</label>
          <input type="text" id="dPlace" />
        </div>
        <div class="field">
          <label>행정</label>
          <input type="text" id="dAdmin" />
        </div>
      </div>

      <div class="actions">
        <button class="btn danger" id="dDeleteBtn">이 날짜 삭제</button>
        <button class="btn" id="dCloseBtn">닫기</button>
        <button class="btn" id="dSaveBtn">저장</button>
      </div>
    </div>
  </div>

<script>
  const viewHost = document.getElementById('viewHost');
  const ymTitle = document.getElementById('ymTitle');

  const bizFilter = document.getElementById('bizFilter');
  const resetFilterBtn = document.getElementById('resetFilterBtn');

  const prevBtn = document.getElementById('prevBtn');
  const nextBtn = document.getElementById('nextBtn');
  const monthBtn = document.getElementById('monthBtn');
  const weekBtn = document.getElementById('weekBtn');

  const addBack = document.getElementById('addBack');
  const openAddBtn = document.getElementById('openAddBtn');
  const addCloseBtn = document.getElementById('addCloseBtn');
  const addSaveBtn = document.getElementById('addSaveBtn');

  const detailBack = document.getElementById('detailBack');
  const dCloseBtn = document.getElementById('dCloseBtn');
  const dSaveBtn = document.getElementById('dSaveBtn');
  const dDeleteBtn = document.getElementById('dDeleteBtn');

  const addStart = document.getElementById('addStart');
  const addEnd = document.getElementById('addEnd');
  const addBusiness = document.getElementById('addBusiness');
  const addCourse = document.getElementById('addCourse');
  const addTime = document.getElementById('addTime');
  const addPeople = document.getElementById('addPeople');
  const addPlace = document.getElementById('addPlace');
  const addAdmin = document.getElementById('addAdmin');
  const addExcluded = document.getElementById('addExcluded');

  const dDate = document.getElementById('dDate');
  const dBusiness = document.getElementById('dBusiness');
  const dCourse = document.getElementById('dCourse');
  const dTime = document.getElementById('dTime');
  const dPeople = document.getElementById('dPeople');
  const dPlace = document.getElementById('dPlace');
  const dAdmin = document.getElementById('dAdmin');

  let mode = 'month'; // month | week
  let current = new Date();
  current.setHours(0,0,0,0);

  let allEvents = []; // {id,date,business,course,time,people,place,admin,group_id}
  let selectedEventId = null;

  function ymd(d){
    const y = d.getFullYear();
    const m = String(d.getMonth()+1).padStart(2,'0');
    const day = String(d.getDate()).padStart(2,'0');
    return `${y}-${m}-${day}`;
  }
  function parseYMD(s){
    const [y,m,d] = s.split('-').map(n=>parseInt(n,10));
    const dt = new Date(y, m-1, d);
    dt.setHours(0,0,0,0);
    return dt;
  }
  function sameDay(a,b){
    return a.getFullYear()===b.getFullYear() && a.getMonth()===b.getMonth() && a.getDate()===b.getDate();
  }

  function hslForBusiness(name){
    // 사업명별 색상: 문자열 해시
    const s = (name||'').trim();
    if(!s) return 'hsl(0 0% 92%)';
    let hash = 0;
    for(let i=0;i<s.length;i++){
      hash = ((hash<<5)-hash) + s.charCodeAt(i);
      hash |= 0;
    }
    const h = Math.abs(hash) % 360;
    return `hsl(${h} 70% 88%)`;
  }

  function filteredEvents(){
    const f = bizFilter.value;
    if(!f) return allEvents;
    return allEvents.filter(e => (e.business||'') === f);
  }

  async function loadBusinesses(){
    const res = await fetch('/api/businesses');
    const j = await res.json();
    if(!j.ok) return;
    const keep = bizFilter.value;

    // reset options
    bizFilter.innerHTML = '<option value="">전체</option>';
    j.businesses.forEach(b=>{
      const op = document.createElement('option');
      op.value = b;
      op.textContent = b;
      bizFilter.appendChild(op);
    });
    bizFilter.value = keep;
  }

  async function loadEventsRange(start, end){
    const res = await fetch(`/api/events?start=${start}&end=${end}`);
    const j = await res.json();
    if(!j.ok){
      alert(j.error || '불러오기 실패');
      return [];
    }
    return j.events || [];
  }

  async function refresh(){
    // month: 해당 월 범위, week: 해당 주 범위만 로드
    let start, end;

    if(mode === 'month'){
      const y = current.getFullYear();
      const m = current.getMonth();
      const first = new Date(y, m, 1);
      const last = new Date(y, m+1, 0);
      start = ymd(first);
      end = ymd(last);
      ymTitle.textContent = `${y}년 ${m+1}월`;
    }else{
      const d = new Date(current);
      const day = d.getDay(); // 0 sun
      const diff = day; // sunday start
      const ws = new Date(d);
      ws.setDate(d.getDate()-diff);
      const we = new Date(ws);
      we.setDate(ws.getDate()+6);
      start = ymd(ws);
      end = ymd(we);
      ymTitle.textContent = `${start} ~ ${end}`;
    }

    allEvents = await loadEventsRange(start, end);
    await loadBusinesses();
    render();
  }

  function render(){
    viewHost.innerHTML = '';
    if(mode==='month') renderMonth();
    else renderWeek();
  }

  function renderMonth(){
    const y = current.getFullYear();
    const m = current.getMonth();
    const first = new Date(y, m, 1);
    const last = new Date(y, m+1, 0);

    const startCell = new Date(first);
    startCell.setDate(1 - first.getDay()); // sunday start

    const endCell = new Date(last);
    endCell.setDate(last.getDate() + (6 - last.getDay()));

    const cal = document.createElement('div');
    cal.className = 'cal';

    const dow = document.createElement('div');
    dow.className = 'dow';
    ['일','월','화','수','목','금','토'].forEach((t,i)=>{
      const div = document.createElement('div');
      div.textContent = t;
      if(i===0) div.classList.add('sun');
      if(i===6) div.classList.add('sat');
      dow.appendChild(div);
    });
    cal.appendChild(dow);

    const grid = document.createElement('div');
    grid.className = 'grid';

    const evs = filteredEvents();

    let cur = new Date(startCell);
    while(cur <= endCell){
      const cell = document.createElement('div');
      cell.className = 'cell';

      const num = document.createElement('div');
      num.className = 'daynum';
      if(cur.getDay()===0) num.classList.add('sun');
      if(cur.getDay()===6) num.classList.add('sat');
      num.textContent = cur.getDate();
      cell.appendChild(num);

      const list = document.createElement('div');
      list.className = 'events';

      const ds = ymd(cur);
      const dayEvents = evs.filter(e => e.date === ds);

      dayEvents.forEach(e=>{
        const card = document.createElement('div');
        card.className = 'ev';
        card.style.setProperty('--evbg', hslForBusiness(e.business));
        card.onclick = ()=> openDetail(e);

        const title = document.createElement('strong');
        title.textContent = e.business || '(사업명 없음)';
        card.appendChild(title);

        // 공란이면 라인 자체를 숨김 (요청사항)
        function addLine(label, value){
          if(!value) return;
          const div = document.createElement('div');
          div.className='line';
          div.textContent = `${label}: ${value}`;
          card.appendChild(div);
        }
        addLine('과정', e.course);
        addLine('시간', e.time);
        addLine('인원', e.people);
        addLine('장소', e.place);
        addLine('행정', e.admin);

        list.appendChild(card);
      });

      cell.appendChild(list);
      grid.appendChild(cell);

      cur.setDate(cur.getDate()+1);
    }

    cal.appendChild(grid);
    viewHost.appendChild(cal);
  }

  function renderWeek(){
    const base = new Date(current);
    const day = base.getDay();
    const ws = new Date(base);
    ws.setDate(base.getDate()-day);
    const rows = [];

    const wrap = document.createElement('div');
    wrap.className = 'weekWrap';

    const evs = filteredEvents();

    for(let i=0;i<7;i++){
      const d = new Date(ws);
      d.setDate(ws.getDate()+i);
      const ds = ymd(d);

      const row = document.createElement('div');
      row.className = 'weekRow';

      const left = document.createElement('div');
      left.className='weekDate';
      left.textContent = `${ds} (${['일','월','화','수','목','금','토'][d.getDay()]})`;

      const right = document.createElement('div');
      right.className='weekList';

      const list = document.createElement('div');
      list.className='events';

      evs.filter(e=>e.date===ds).forEach(e=>{
        const card = document.createElement('div');
        card.className='ev';
        card.style.setProperty('--evbg', hslForBusiness(e.business));
        card.onclick = ()=> openDetail(e);

        const title = document.createElement('strong');
        title.textContent = e.business || '(사업명 없음)';
        card.appendChild(title);

        function addLine(label, value){
          if(!value) return;
          const div = document.createElement('div');
          div.className='line';
          div.textContent = `${label}: ${value}`;
          card.appendChild(div);
        }
        addLine('과정', e.course);
        addLine('시간', e.time);
        addLine('인원', e.people);
        addLine('장소', e.place);
        addLine('행정', e.admin);

        list.appendChild(card);
      });

      right.appendChild(list);
      row.appendChild(left);
      row.appendChild(right);
      wrap.appendChild(row);
    }

    viewHost.appendChild(wrap);
  }

  function openAdd(){
    const today = new Date();
    const ds = ymd(today);
    addStart.value = ds;
    addEnd.value = ds;
    addBusiness.value = '';
    addCourse.value = '';
    addTime.value = '';
    addPeople.value = '';
    addPlace.value = '';
    addAdmin.value = '';
    addExcluded.value = '';
    addBack.style.display='flex';
  }
  function closeAdd(){ addBack.style.display='none'; }

  async function saveAdd(){
    const s = addStart.value;
    const e = addEnd.value;

    if(!s || !e){
      alert('시작/종료일은 YYYY-MM-DD 형식으로 입력하세요.');
      return;
    }

    const payload = {
      start: s,
      end: e,
      business: addBusiness.value.trim(),
      course: addCourse.value.trim(),
      time: addTime.value.trim(),
      people: addPeople.value.trim(),
      place: addPlace.value.trim(),
      admin: addAdmin.value.trim(),
      excluded_dates: addExcluded.value.trim()
    };

    const res = await fetch('/api/events', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });

    const j = await res.json().catch(()=>null);
    if(!j || !j.ok){
      alert((j && j.error) ? j.error : '저장 중 오류가 발생했습니다.');
      return;
    }

    closeAdd();
    await refresh();
  }

  function openDetail(e){
    selectedEventId = e.id;
    dDate.value = e.date;
    dBusiness.value = e.business || '';
    dCourse.value = e.course || '';
    dTime.value = e.time || '';
    dPeople.value = e.people || '';
    dPlace.value = e.place || '';
    dAdmin.value = e.admin || '';
    detailBack.style.display='flex';
  }
  function closeDetail(){ detailBack.style.display='none'; selectedEventId=null; }

  async function saveDetail(){
    if(!selectedEventId) return;

    const payload = {
      date: dDate.value,
      business: dBusiness.value.trim(),
      course: dCourse.value.trim(),
      time: dTime.value.trim(),
      people: dPeople.value.trim(),
      place: dPlace.value.trim(),
      admin: dAdmin.value.trim()
    };

    const res = await fetch(`/api/events/${selectedEventId}`,{
      method:'PUT',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    const j = await res.json().catch(()=>null);
    if(!j || !j.ok){
      alert((j && j.error) ? j.error : '수정 중 오류가 발생했습니다.');
      return;
    }
    closeDetail();
    await refresh();
  }

  async function deleteDetail(){
    if(!selectedEventId) return;
    if(!confirm('이 날짜 일정만 삭제할까요?')) return;

    const res = await fetch(`/api/events/${selectedEventId}`, {method:'DELETE'});
    const j = await res.json().catch(()=>null);
    if(!j || !j.ok){
      alert((j && j.error) ? j.error : '삭제 중 오류가 발생했습니다.');
      return;
    }
    closeDetail();
    await refresh();
  }

  // events
  openAddBtn.onclick = openAdd;
  addCloseBtn.onclick = closeAdd;
  addSaveBtn.onclick = saveAdd;

  dCloseBtn.onclick = closeDetail;
  dSaveBtn.onclick = saveDetail;
  dDeleteBtn.onclick = deleteDetail;

  addBack.addEventListener('click', (ev)=>{ if(ev.target === addBack) closeAdd(); });
  detailBack.addEventListener('click', (ev)=>{ if(ev.target === detailBack) closeDetail(); });

  prevBtn.onclick = ()=>{
    if(mode==='month'){
      current = new Date(current.getFullYear(), current.getMonth()-1, 1);
    }else{
      current = new Date(current.getFullYear(), current.getMonth(), current.getDate()-7);
    }
    refresh();
  };
  nextBtn.onclick = ()=>{
    if(mode==='month'){
      current = new Date(current.getFullYear(), current.getMonth()+1, 1);
    }else{
      current = new Date(current.getFullYear(), current.getMonth(), current.getDate()+7);
    }
    refresh();
  };
  monthBtn.onclick = ()=>{ mode='month'; refresh(); };
  weekBtn.onclick = ()=>{ mode='week'; refresh(); };

  bizFilter.onchange = render;
  resetFilterBtn.onclick = ()=>{
    bizFilter.value = '';
    render();
  };

  // init
  refresh();
</script>
</body>
</html>"""


@app.get("/")
def index():
    return Response(_html(), mimetype="text/html")


if __name__ == "__main__":
    # local debug
    port = int(os.environ.get("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=True)
