"""
화성 테니스 빈 코트 캘린더 v0.6
변경사항:
  - 시간 단축버튼: 전체/오전/오후/저녁(18~) + 세부 2시간 버튼 연동
  - 코트 필터 버튼 배경색 꽉 채우기
  - 같은 그룹 = 완전 동일한 색
"""
import requests, json, time, csv, io, re
from datetime import datetime, timezone, timedelta

API_URL  = "https://yeyak.hscity.go.kr/stadium/stadiumReserveUseList.do"
RESV_URL = "https://yeyak.hscity.go.kr/stadiumDetail.do?stadiumIdx="
HEADERS  = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://yeyak.hscity.go.kr/stadiumDetail.do",
}

def fetch(idx, year, month):
    try:
        r = requests.post(API_URL,
            data={"stadiumIdx": idx, "searchYear": str(year), "searchMonth": str(month)},
            headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  [X] {idx} {year}-{month}: {e}")
        return None

def extract_empty(resp):
    if not resp:
        return []
    return [{"date": s.get("sorDate"),
             "begin": s.get("stadiumBeginHm"),
             "end":   s.get("stadiumEndHm")}
            for s in resp.get("useCntList", [])
            if s.get("applyStatusCd") is None]

# ===== 죽미·시립 (오산시 테니스협회 신 시스템) =====
OSAN_API  = "https://ost.moklab.kr/api/reservations/slots"
OSAN_RESV = "https://ost.moklab.kr/reservation"

def fetch_osan(year, month):
    """
    오산시 테니스협회 API → 죽미/시립 빈자리 추출
    반환: {"죽미": [...], "시립": [...]} 형식
    """
    import calendar
    from datetime import datetime as _dt

    max_day = calendar.monthrange(year, month)[1]
    today = _dt.now().date()

    # 코트별 빈자리: {그룹명: {코트번호: {(date, begin): end}}}
    courts_by_group = {"죽미실내": {}, "죽미실외": {}, "시립": {}}

    for day in range(1, max_day + 1):
        date_str = f"{year}-{month:02d}-{day:02d}"
        target = _dt(year, month, day).date()
        if target < today:
            continue

        try:
            res = requests.get(OSAN_API,
                params={"date": date_str},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15)
            if res.status_code != 200:
                continue
            data = res.json()
        except Exception as e:
            print(f"[X] 오산 {date_str}: {e}")
            continue

        for slot in data.get("slots", []):
            if slot.get("statusCode") != "OPEN":
                continue
            
            court_name = slot.get("courtName", "")
            # 그룹 판별 — 죽미는 "실내코트"/"실외코트" 표기로 세부 구분
            if "죽미" in court_name:
                group = "죽미실내" if "실내" in court_name else "죽미실외"
            elif "시립" in court_name:
                group = "시립"
            else:
                continue

            # "죽미테니스장 - 1번코트" → "1"
            import re as _re
            m = _re.search(r'(\d+)번', court_name)
            if not m:
                continue
            court_num = m.group(1)

            start = slot.get("startAt", "")
            end_at = slot.get("endAt", "")
            try:
                begin = start.split("T")[1][:5]
                end   = end_at.split("T")[1][:5]
            except (IndexError, AttributeError):
                continue

            if court_num not in courts_by_group[group]:
                courts_by_group[group][court_num] = {}
            courts_by_group[group][court_num][(date_str, begin)] = end

        time.sleep(0.15)

    # 결과를 코트별 리스트로 변환
    result = {"죽미실내": [], "죽미실외": [], "시립": []}
    for group, courts in courts_by_group.items():
        for num in sorted(courts.keys(), key=lambda x: int(x)):
            slots = [{"date": k[0], "begin": k[1], "end": v}
                     for k, v in courts[num].items()]
            slots.sort(key=lambda s: (s["date"], s["begin"]))
            result[group].append({"num": num, "slots": slots})

    return result



# ===== 날씨 (기상청 단기예보, 동탄 기준) =====
KMA_SERVICE_KEY = __import__("os").environ.get("KMA_SERVICE_KEY", "")
KMA_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
KMA_NX, KMA_NY = 62, 119  # 동탄 기준 격자좌표 (최초 실행 결과로 유효성 확인 필요)
KMA_ANNOUNCE_HOURS = [2, 5, 8, 11, 14, 17, 20, 23]

def fetch_weather():
    """
    기상청 단기예보(3일치) 조회 → 날짜별 요약(최저/최고기온, 최대강수확률, 대표하늘상태)으로 가공.
    실패 시 빈 dict 반환 — 날씨 실패가 코트 정보 갱신을 막지 않도록 함.
    """
    if not KMA_SERVICE_KEY:
        print("  [!] KMA_SERVICE_KEY 미설정 — 날씨 정보 생략")
        return {}

    KST = timezone(timedelta(hours=9))
    now = datetime.now(KST) - timedelta(minutes=10)  # 발표 직후 버퍼
    candidates = [h for h in KMA_ANNOUNCE_HOURS if h <= now.hour]
    if candidates:
        base_hour = max(candidates)
        base_date = now.strftime("%Y%m%d")
    else:
        base_hour = 23
        base_date = (now - timedelta(days=1)).strftime("%Y%m%d")
    base_time = f"{base_hour:02d}00"

    try:
        for attempt in range(3):
            try:
                r = requests.get(KMA_URL, params={
                    "serviceKey": KMA_SERVICE_KEY, "pageNo": 1, "numOfRows": 1000,
                    "dataType": "JSON", "base_date": base_date, "base_time": base_time,
                    "nx": KMA_NX, "ny": KMA_NY,
                }, timeout=20)
                r.raise_for_status()
                break
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ConnectionError) as e:
                if attempt == 2:
                    raise
                print(f"  [!] 날씨 API 연결 재시도 {attempt+1}/3...", end=" ")
                time.sleep(3)
        data = r.json()
        header = data.get("response", {}).get("header", {})
        if header.get("resultCode") != "00":
            print(f"  [!] 날씨 API 오류: {header.get('resultCode')} {header.get('resultMsg')}")
            return {}
        items = data["response"]["body"]["items"]["item"]
    except Exception as e:
        print(f"  [!] 날씨 조회 실패: {e}")
        return {}

    by_date = {}
    for it in items:
        d, cat, val = it["fcstDate"], it["category"], it["fcstValue"]
        by_date.setdefault(d, {"tmp": [], "pop": [], "sky": [], "pty": []})
        if cat == "TMP": by_date[d]["tmp"].append(float(val))
        elif cat == "POP": by_date[d]["pop"].append(int(val))
        elif cat == "SKY": by_date[d]["sky"].append(val)
        elif cat == "PTY": by_date[d]["pty"].append(val)

    summary = {}
    for d, v in sorted(by_date.items())[:3]:  # 오늘+모레까지 최대 3일
        summary[d] = {
            "tmin": round(min(v["tmp"])) if v["tmp"] else None,
            "tmax": round(max(v["tmp"])) if v["tmp"] else None,
            "pop":  max(v["pop"]) if v["pop"] else 0,
            "sky":  max(set(v["sky"]), key=v["sky"].count) if v["sky"] else "1",
            "pty":  "1" if any(p != "0" for p in v["pty"]) else "0",
        }
    return {"base_date": base_date, "base_time": base_time, "days": summary}


def main():
    with open("stadiums.json", encoding="utf-8") as f:
        stadiums = json.load(f)

    KST = timezone(timedelta(hours=9))
    now = datetime.now(KST)
    y, m = now.year, now.month
    months = [(y, m)]
    nm, ny = m+1, y
    if nm > 12: nm, ny = 1, y+1
    months.append((ny, nm))

    print(f"\n[조회] {months[0][0]}-{months[0][1]:02d} + {months[1][0]}-{months[1][1]:02d}")
    print("=" * 60)

    result = []
    for s in stadiums:
        idx = s["idx"]
        print(f"  [{idx:>4s}] {s['name']:<14s}", end=" ")
        slots = []
        for yr, mo in months:
            d = fetch(idx, yr, mo)
            time.sleep(0.3)
            slots.extend(extract_empty(d))
        result.append({
            "idx": idx, "name": s["name"], "group": s.get("group",""),
            "url": RESV_URL + idx, "empty_slots": slots
        })
        print(f"빈자리 {len(slots):>3d}개")

    # 오산시 테니스협회 (죽미 + 시립)
    print(f"  [오산] 죽미·시립 통합 API     ", end=" ")
    osan_by_group = {"죽미실내": {}, "죽미실외": {}, "시립": {}}  # {그룹: {코트번호: {(date,begin):end}}}
    for yr, mo in months:
        month_data = fetch_osan(yr, mo)
        for group_name, courts in month_data.items():
            for court in courts:
                num = court["num"]
                if num not in osan_by_group[group_name]:
                    osan_by_group[group_name][num] = {}
                for s in court["slots"]:
                    osan_by_group[group_name][num][(s["date"], s["begin"])] = s["end"]

    osan_total = 0
    for group_name, courts in osan_by_group.items():
        for num in sorted(courts.keys(), key=lambda x: int(x)):
            slots = [{"date": k[0], "begin": k[1], "end": v}
                     for k, v in courts[num].items()]
            slots.sort(key=lambda s: (s["date"], s["begin"]))
            result.append({
                "idx": f"osan_{group_name}_{num}",
                "name": f"{group_name} {num}번",
                "group": group_name,
                "url": OSAN_RESV,
                "empty_slots": slots,
            })
            osan_total += len(slots)
    print(f"빈자리 {osan_total:>3d}개")

    # 날씨 (동탄 기준, 실패해도 코트 정보는 정상 생성)
    print(f"  [날씨] 동탄 단기예보 조회 중...", end=" ")
    weather = fetch_weather()
    print(f"{len(weather.get('days', {}))}일치 확보" if weather else "생략")

    ts = now.strftime("%Y-%m-%d %H:%M")
    html = (HTML
            .replace("__DATA__",  json.dumps(result, ensure_ascii=False))
            .replace("__WEATHER__", json.dumps(weather, ensure_ascii=False))
            .replace("__TIME__",  ts)
            .replace("__YEAR__",  str(months[0][0]))
            .replace("__MONTH__", str(months[0][1])))

    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html)

    total = sum(len(c["empty_slots"]) for c in result)
    print("=" * 60)
    print(f"[OK] index.html 생성 / 총 빈자리 {total}개")
    print("→ index.html 더블클릭해서 브라우저에서 열어보세요 🎾")


HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>동탄·죽미 코트 예약현황 🎾</title>
<style>
:root{
  --bg:#f0f2f5;--card:#fff;--text:#1a1a2e;--muted:#6b7280;
  --border:#e2e8f0;--accent:#3b82f6;--hover:#f1f5f9;
  --sun:#ef4444;--sat:#3b82f6;--today-ring:#f59e0b;
  --shadow:0 1px 4px rgba(0,0,0,.08);
}
[data-theme=dark]{
  --bg:#0f172a;--card:#1e293b;--text:#e2e8f0;--muted:#64748b;
  --border:#334155;--accent:#60a5fa;--hover:#273449;
  --sun:#f87171;--sat:#60a5fa;--today-ring:#fbbf24;
  --shadow:0 1px 4px rgba(0,0,0,.3);
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Noto Sans KR",-apple-system,sans-serif;
  background:var(--bg);color:var(--text);
  min-height:100vh;padding:16px;
  transition:background .25s,color .25s}

.hdr{display:flex;justify-content:space-between;align-items:center;
  flex-wrap:nowrap;gap:8px;margin-bottom:14px}
.hdr-left{flex:1;min-width:0}
.hdr h1{font-size:20px;font-weight:800;letter-spacing:-.5px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.hdr h1 em{font-size:11px;font-weight:400;color:var(--muted);
  font-style:normal;margin-left:6px}
.hdr p{font-size:11px;color:var(--muted);margin-top:3px}
.hdr-r{display:flex;gap:6px;flex-shrink:0}
.update-time{font-size:11px;font-weight:400;color:var(--muted);margin-left:8px}

/* 공통 버튼 */
.btn{padding:6px 12px;border:1px solid var(--border);background:var(--card);
  color:var(--text);border-radius:8px;cursor:pointer;font-size:12px;
  font-family:inherit;font-weight:600;transition:all .15s;line-height:1.4}
.btn:hover{border-color:var(--accent);color:var(--accent)}
.btn.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn.icon{padding:6px 10px;font-size:15px}

/* 필터 */
.filters{background:var(--card);border:1px solid var(--border);
  border-radius:12px;padding:12px 14px;margin-bottom:12px;
  display:flex;flex-direction:column;gap:10px;box-shadow:var(--shadow)}
.fg{display:flex;align-items:center;gap:5px;flex-wrap:wrap}
.fg-lbl{font-size:10px;font-weight:700;color:var(--muted);
  text-transform:uppercase;letter-spacing:.8px;min-width:42px;flex-shrink:0}
.fg-div{width:1px;height:20px;background:var(--border);margin:0 4px}

/* 단축 버튼 — 조금 더 굵게 */
.btn-short{padding:7px 14px;font-size:13px}

/* 세부 시간 버튼 — 작게 */
.btn-detail{padding:5px 9px;font-size:11px;border-radius:6px}

/* ★ 코트 버튼 — 배경색 꽉 채우기 */
.btn-court{
  padding:7px 14px;font-size:13px;font-weight:700;
  border:none;border-radius:8px;cursor:pointer;
  color:#fff;font-family:inherit;
  transition:filter .15s,transform .1s;
  opacity:1;
}
.btn-court:hover{filter:brightness(1.1);transform:translateY(-1px)}
.btn-court.off{opacity:.35;filter:grayscale(.4)}

/* 월 네비 */
.mnav{display:flex;justify-content:center;align-items:center;
  gap:16px;margin-bottom:10px}
.mnav-title{font-size:19px;font-weight:800;min-width:150px;text-align:center}
.mbtn{width:34px;height:34px;border-radius:50%;
  border:1px solid var(--border);background:var(--card);color:var(--text);
  cursor:pointer;font-size:15px;display:flex;align-items:center;
  justify-content:center;transition:all .15s}
.mbtn:hover{background:var(--hover);border-color:var(--accent)}
.mbtn:disabled{opacity:.3;cursor:default}

/* 캘린더 */
.cal-wrap{background:var(--card);border:1px solid var(--border);
  border-radius:12px;overflow:hidden;box-shadow:var(--shadow)}
table.cal{width:100%;border-collapse:collapse;table-layout:fixed}
table.cal th{padding:10px 4px;font-size:12px;font-weight:700;
  color:var(--muted);background:var(--hover);
  border-bottom:1px solid var(--border)}
th.h-sun{color:var(--sun)}
th.h-sat{color:var(--sat)}
table.cal td{
  border:1px solid var(--border);
  vertical-align:top;
  height:128px;          /* 6개 슬롯 기준 고정 */
  width:14.28%;
  padding:4px;
  background:var(--card);
  overflow:hidden;       /* 넘쳐도 셀 크기 유지 */
}
td.empty{background:var(--hover);opacity:.45}
td.past{opacity:.4}
td.today{box-shadow:inset 0 0 0 2px var(--today-ring)}

.dnum{font-size:11px;font-weight:700;margin-bottom:2px;  /* 날짜 숫자 작게 */
  padding:1px 3px;display:inline-block;border-radius:4px}
.dnum.sun{color:var(--sun)}
.dnum.sat{color:var(--sat)}
td.today .dnum{background:var(--today-ring);color:#fff;border-radius:50%;
  width:19px;height:19px;font-size:10px;
  display:flex;align-items:center;justify-content:center;padding:0}

.holi{font-size:10px;color:var(--sun);font-weight:700;
  margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
td.holiday-bg{background:rgba(239,68,68,.07)!important}
[data-theme=dark] td.holiday-bg{background:rgba(248,113,113,.1)!important}

.rain-badge{position:absolute;top:2px;right:3px;font-size:11px;
  display:flex;align-items:center;gap:1px;line-height:1}
.rain-badge .rp{font-size:8px;font-weight:700;color:var(--sat)}
td{position:relative}

.slots{display:grid;grid-template-columns:1fr 1fr;gap:3px;overflow:hidden}
.slots.exp{grid-template-columns:1fr 1fr}

.slot{
  display:block;padding:4px 6px;border-radius:5px;
  cursor:pointer;text-decoration:none;
  color:#fff;font-size:11px;font-weight:700;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  transition:filter .12s,transform .1s;line-height:1.3;
  text-align:center;
}
.slot:hover{filter:brightness(1.12);transform:translateY(-1px)}
.sn-s{display:none}
.sn-tf{display:inline} /* PC 시간 */

.more-btn{
  grid-column:1/-1;font-size:11px;color:var(--accent);font-weight:700;
  padding:3px 5px;cursor:pointer;text-align:center;
  border:1px dashed var(--border);border-radius:5px;
  background:none;font-family:inherit;transition:background .12s;margin-top:1px;
}
.more-btn:hover{background:var(--hover)}



.legend{display:flex;flex-wrap:wrap;gap:12px;margin-top:12px;
  justify-content:center;font-size:12px;color:var(--muted)}
.leg-item{display:flex;align-items:center;gap:6px}
.leg-dot{width:14px;height:14px;border-radius:3px}

.summary{font-size:11px;color:var(--muted);text-align:center;margin-top:8px}

/* 날씨 요약 카드 */
.weather-wrap{display:flex;gap:8px;margin-top:12px;overflow-x:auto}
.wcard{flex:1;min-width:90px;background:var(--card);border:1px solid var(--border);
  border-radius:12px;padding:10px 8px;text-align:center;box-shadow:var(--shadow)}
.wcard .wd{font-size:11px;font-weight:700;color:var(--muted);margin-bottom:4px}
.wcard .wi{font-size:22px;line-height:1}
.wcard .wt{font-size:13px;font-weight:700;margin-top:4px}
.wcard .wt .lo{color:var(--muted);font-weight:500}
.wcard .wp{font-size:11px;margin-top:2px}
.wcard .wp.high{color:var(--sat);font-weight:700}
@media(max-width:700px){.wcard{min-width:76px;padding:8px 6px}.wcard .wi{font-size:18px}}

.tip{position:fixed;pointer-events:none;z-index:9999;
  background:#1e293b;color:#fff;font-size:12px;font-weight:500;
  padding:5px 10px;border-radius:7px;white-space:nowrap;
  box-shadow:0 4px 12px rgba(0,0,0,.25);opacity:0;transition:opacity .1s}

@media(max-width:700px){
  table.cal td{height:120px;padding:4px 3px}
  .slot{font-size:10px;padding:3px 4px}
  .dnum{font-size:12px}
  .slots{grid-template-columns:1fr}
  .slots.exp{grid-template-columns:1fr}  /* 더보기 펼쳐도 1열 유지 */
  .slot-hint{display:none}
  .sn-f{display:none}
  .sn-s{display:inline}
  .sn-tf{display:none}  /* 모바일: 전체 시간 숨김, sn-s가 대신 표시 */

  /* 모바일 헤더 한 줄 강제 */
  .hdr h1{font-size:15px}
  .hdr h1 em{font-size:9px;margin-left:3px}
  .update-time{font-size:9px;margin-left:5px;display:block;margin-top:2px}
  .btn.icon{padding:5px 8px;font-size:13px}
}
</style>
</head>
<body data-theme="light">
<div class="tip" id="tip"></div>

<div class="hdr">
  <div class="hdr-left">
    <h1>🎾 동탄·죽미 코트 예약현황 <span class="update-time">__TIME__ 기준</span></h1>
  </div>
  <div class="hdr-r">
    <button class="btn icon" onclick="toggleTheme()">🌙</button>
  </div>
</div>

<div class="filters">
  <!-- 시간 필터 -->
  <div class="fg">
    <span class="fg-lbl">⏰ 시간</span>
    <!-- 단축 버튼 -->
    <button class="btn btn-short f-short" data-s="all"     onclick="setShort('all')">🔍 전체</button>
    <button class="btn btn-short f-short" data-s="morning" onclick="setShort('morning')">🌅 오전 (~12)</button>
    <button class="btn btn-short f-short" data-s="afternoon" onclick="setShort('afternoon')">☀️ 오후 (12~18)</button>
    <button class="btn btn-short on f-short" data-s="evening" onclick="setShort('evening')">🌙 저녁 (18~) ⭐</button>
    <!-- 구분선 -->
    <div class="fg-div"></div>
    <!-- 세부 버튼 -->
    <button class="btn btn-detail f-t" data-h="6"  onclick="togT(6)">06-08</button>
    <button class="btn btn-detail f-t" data-h="8"  onclick="togT(8)">08-10</button>
    <button class="btn btn-detail f-t" data-h="10" onclick="togT(10)">10-12</button>
    <button class="btn btn-detail f-t" data-h="12" onclick="togT(12)">12-14</button>
    <button class="btn btn-detail f-t" data-h="14" onclick="togT(14)">14-16</button>
    <button class="btn btn-detail f-t" data-h="16" onclick="togT(16)">16-18</button>
    <button class="btn btn-detail on f-t" data-h="18" onclick="togT(18)">18-20</button>
    <button class="btn btn-detail on f-t" data-h="20" onclick="togT(20)">20-22</button>
  </div>
  <!-- 코트 필터 -->
  <div class="fg" id="gf">
    <span class="fg-lbl">📍 코트</span>
  </div>
</div>

<div class="mnav">
  <button class="mbtn" id="pb" onclick="goM(-1)">◀</button>
  <div class="mnav-title" id="mt"></div>
  <button class="mbtn" id="nb" onclick="goM(1)">▶</button>
</div>

<div class="cal-wrap"><div id="cal"></div></div>

<div class="weather-wrap" id="weatherWrap"></div>


<script>
const COURTS = __DATA__;
const WEATHER = __WEATHER__;
const T0 = new Date(); T0.setHours(0,0,0,0);

/* 날씨 요약 카드 렌더 */
function skyIcon(sky, pty){
  if(pty && pty !== "0") return "🌧️";
  if(sky === "1") return "☀️";
  if(sky === "3") return "⛅";
  return "☁️";
}
function renderWeather(){
  const wrap = document.getElementById('weatherWrap');
  if(!wrap) return;
  const days = (WEATHER && WEATHER.days) ? WEATHER.days : {};
  const keys = Object.keys(days).sort();
  if(keys.length === 0){ wrap.style.display='none'; return; }
  const labels = ['오늘','내일','모레'];
  let h = '';
  keys.forEach((d, i) => {
    const v = days[d];
    const md = d.slice(4,6)+'/'+d.slice(6,8);
    const popCls = v.pop >= 60 ? 'high' : '';
    h += `<div class="wcard">
      <div class="wd">${labels[i]||md} <span style="font-weight:400">${md}</span></div>
      <div class="wi">${skyIcon(v.sky, v.pty)}</div>
      <div class="wt">${v.tmax ?? '-'}° <span class="lo">${v.tmin ?? '-'}°</span></div>
      <div class="wp ${popCls}">💧 ${v.pop}%</div>
    </div>`;
  });
  wrap.innerHTML = h;
}

/* 날짜(YYYY-MM-DD) → 날씨 요약 조회 */
function weatherForDate(ds){
  const days = (WEATHER && WEATHER.days) ? WEATHER.days : {};
  const key = ds.replace(/-/g, '');
  return days[key] || null;
}
const GH = {금반저류지:215, 왕배산:145, 여울공원:340, 돌모루:275, 죽미실내:25, 죽미실외:35, 시립:55, 중동:190};
function groupColor(g){ return `hsl(${GH[g]??0},65%,50%)`; }
function slotColor(c){ return groupColor(c.group||c.name); }

function shortNm(c){
  const n=(c.name.match(/(\d+)번/)||[])[1]||'';
  const m={금반저류지:'금반',왕배산:'왕배산',여울공원:'여울',돌모루:'돌모루',죽미실내:'죽미(내)',죽미실외:'죽미(외)',시립:'시립',중동:'중동'};
  return (m[c.group]||c.group)+n;
}
function mobileNm(c){
  const n=(c.name.match(/(\d+)번/)||[])[1]||'';
  const m={금반저류지:'금',왕배산:'왕',여울공원:'여',돌모루:'돌',죽미실내:'죽내',죽미실외:'죽외',시립:'시',중동:'중'};
  return (m[c.group]||c.group)+n+'번';
}

const HOLI={
  /* 2026년 확정 공휴일 */
  '2026-01-01':'신정',
  '2026-02-16':'설 연휴','2026-02-17':'설날','2026-02-18':'설 연휴',
  '2026-03-01':'삼일절(일)','2026-03-02':'삼일절 대체',
  '2026-05-01':'노동절',
  '2026-05-05':'어린이날',
  '2026-05-24':'부처님오신날(일)','2026-05-25':'부처님오신날 대체',
  '2026-06-03':'지방선거',
  '2026-06-06':'현충일(토)',
  '2026-07-17':'제헌절',
  '2026-08-15':'광복절(토)','2026-08-17':'광복절 대체',
  '2026-09-24':'추석 연휴','2026-09-25':'추석','2026-09-26':'추석 연휴(토)',
  '2026-10-03':'개천절(토)',
  '2026-10-09':'한글날',
  '2026-12-25':'성탄절',
};

const MONTHS=[];
{const y=parseInt("__YEAR__"),m=parseInt("__MONTH__");
 MONTHS.push({y,m});
 let ny=y,nm=m+1;if(nm>12){nm=1;ny=y+1;}
 MONTHS.push({y:ny,m:nm});}
let cur=0;

/* 단축 그룹 정의 */
const SHORT_MAP = {
  all:       [6,8,10,12,14,16,18,20],
  morning:   [6,8,10],
  afternoon: [12,14,16],
  evening:   [18,20],
};

/* 필터 상태 — 기본: 저녁 ON */
let fHours = new Set([18,20]);
const allGroups=[...new Set(COURTS.map(c=>c.group||c.name))];
let fGroups=new Set(allGroups);
const expanded=new Set();

/* ★ 코트 버튼 — 배경색 꽉 채우기 */
const gf=document.getElementById('gf');
allGroups.forEach(g=>{
  const b=document.createElement('button');
  b.className='btn-court'; b.dataset.v=g; b.textContent=g;
  b.style.background=groupColor(g);
  b.onclick=()=>togG(g);
  gf.appendChild(b);
});
/* 같은 줄 오른쪽 끝에 힌트 텍스트 */
const hint=document.createElement('span');
hint.className='slot-hint';
hint.style.cssText='margin-left:auto;font-size:11px;color:var(--muted);white-space:nowrap';
hint.innerHTML='✅ 슬롯 클릭 → 예약페이지 이동 &nbsp;·&nbsp; +N개 → 펼치기';
gf.appendChild(hint);

/* 단축 버튼: 해당 시간 셋 켜기/끄기 */
function setShort(s){
  const hours = SHORT_MAP[s];
  const allOn = hours.every(h=>fHours.has(h));
  // 모두 켜진 상태 → 끄기 / 아니면 → 켜기 (all도 동일하게 토글)
  if(allOn){ hours.forEach(h=>fHours.delete(h)); }
  else      { hours.forEach(h=>fHours.add(h)); }
  syncUI(); expanded.clear(); render();
}

/* 세부 버튼 개별 토글 */
function togT(h){
  fHours.has(h)?fHours.delete(h):fHours.add(h);
  syncUI(); expanded.clear(); render();
}

/* UI 동기화 (세부 버튼 + 단축 버튼 active 상태) */
function syncUI(){
  // 세부 버튼
  document.querySelectorAll('.f-t').forEach(b=>
    b.classList.toggle('on', fHours.has(parseInt(b.dataset.h))));
  // 단축 버튼 — 해당 그룹 시간이 모두 켜진 경우 active
  document.querySelectorAll('.f-short').forEach(b=>{
    const s=b.dataset.s;
    if(s==='all'){
      b.classList.toggle('on', SHORT_MAP.all.every(h=>fHours.has(h)));
    } else {
      b.classList.toggle('on', SHORT_MAP[s].every(h=>fHours.has(h)));
    }
  });
}

function togG(v){
  if(fGroups.has(v)) fGroups.delete(v);
  else fGroups.add(v);
  // 버튼 off 클래스
  document.querySelectorAll('.btn-court').forEach(b=>
    b.classList.toggle('off', !fGroups.has(b.dataset.v)));
  expanded.clear(); render();
}
function goM(d){
  const n=cur+d; if(n<0||n>=MONTHS.length)return;
  cur=n; expanded.clear(); render();
}

function ok(slot,court){
  if(!fGroups.has(court.group||court.name)) return false;
  if(!fHours.has(parseInt(slot.begin))) return false;
  if(new Date(slot.date)<T0) return false;
  return true;
}
function slotsOn(ds){
  const out=[];
  COURTS.forEach(c=>c.empty_slots.forEach(s=>{
    if(s.date===ds&&ok(s,c)) out.push({...s,court:c});
  }));
  return out.sort((a,b)=>a.begin.localeCompare(b.begin)||
                          a.court.idx.localeCompare(b.court.idx));
}

function toggleExp(ds){
  expanded.has(ds)?expanded.delete(ds):expanded.add(ds);
  render();
}

const MAX=6; /* 2열×3행 */

function buildSlots(slots,ds){
  const isExp=expanded.has(ds);
  const vis=isExp?slots:slots.slice(0,MAX);
  const rest=slots.length-MAX;
  let h=`<div class="slots${isExp?' exp':''}">`;
  vis.forEach(s=>{
    const col=slotColor(s.court);
    const sn=shortNm(s.court);
    const mob=mobileNm(s.court);
    const tip2=`${s.court.name}  ${s.begin}~${s.end}`;
    const hOnly=s.begin.split(':')[0];  // "18:00" → "18"
    h+=`<a class="slot" href="${s.court.url}" target="_blank"
      style="background:${col}"
      onmouseenter="showTip(event,'${tip2.replace(/'/g,"\\'")}' )"
      onmouseleave="hideTip()"
    ><span class='t'><span class='sn-tf'>${s.begin}</span><span class='sn-s'>${parseInt(hOnly)}시</span></span> <span class='sn-f'>${sn}</span></a>`;
  });
  if(!isExp&&rest>0){
    h+=`<button class="more-btn" onclick="toggleExp('${ds}')">+${rest}개 더 보기 🔽</button>`;
  } else if(isExp&&slots.length>MAX){
    h+=`<button class="more-btn" onclick="toggleExp('${ds}')">접기 🔼</button>`;
  }
  h+='</div>';
  return h;
}

function render(){
  const {y,m}=MONTHS[cur];
  document.getElementById('mt').textContent=`📅 ${y}년 ${m}월`;
  document.getElementById('pb').disabled=(cur===0);
  document.getElementById('nb').disabled=(cur===MONTHS.length-1);

  const fd=new Date(y,m-1,1),ld=new Date(y,m,0);
  const sd=fd.getDay(),td=ld.getDate();

  let html=`<table class="cal"><thead><tr>
    <th class="h-sun">일</th><th>월</th><th>화</th><th>수</th>
    <th>목</th><th>금</th><th class="h-sat">토</th>
  </tr></thead><tbody>`;

  let day=1,row=0,shown=0;
  while(row<6){                  /* 항상 정확히 6행 */
    html+='<tr>';
    for(let c=0;c<7;c++){
      const isBlank = (row===0&&c<sd) || day>td;
      if(isBlank){
        html+='<td class="empty"></td>';
      } else {
        const ds=`${y}-${String(m).padStart(2,'0')}-${String(day).padStart(2,'0')}`;
        const dt=new Date(y,m-1,day);
        const isPast=dt<T0,isToday=dt.getTime()===T0.getTime();
        const slots=slotsOn(ds);
        shown+=slots.length;
        const dw=dt.getDay();
        const dc=dw===0?'sun':dw===6?'sat':'';
        let cls=''; if(isPast)cls='past'; if(isToday)cls='today';
        const holi=HOLI[ds]||'';
        html+=`<td class="${cls}${holi?' holiday-bg':''}">`;
        const wx = weatherForDate(ds);
        if(wx && wx.pop >= 60){
          html+=`<div class="rain-badge" title="강수확률 ${wx.pop}%">🌧️<span class="rp">${wx.pop}%</span></div>`;
        }
        html+=`<div class="dnum ${dc}">${day}</div>`;
        if(holi) html+=`<div class="holi">${holi}</div>`;
        html+=buildSlots(slots,ds);
        html+=`</td>`;
        day++;
      }
    }
    html+='</tr>'; row++;
  }
  html+='</tbody></table>';
  document.getElementById('cal').innerHTML=html;


}

const tip=document.getElementById('tip');
function showTip(e,txt){tip.textContent=txt;tip.style.opacity='1';moveTip(e);}
document.addEventListener('mousemove',moveTip);
function moveTip(e){tip.style.left=(e.clientX+12)+'px';tip.style.top=(e.clientY-30)+'px';}
function hideTip(){tip.style.opacity='0';}

function toggleTheme(){
  document.body.dataset.theme=document.body.dataset.theme==='dark'?'light':'dark';}
function saveJson(){
  const blob=new Blob([JSON.stringify(COURTS,null,2)],{type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);
  a.download=`tennis_${new Date().toISOString().slice(0,10)}.json`;
  a.click();
}

// 초기 UI 동기화
syncUI();
render();
renderWeather();
</script>
</body>
</html>"""

if __name__ == "__main__":
    main()
