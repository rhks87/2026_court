"""
동탄·죽미 코트 예약현황 v1.0
주요 기능:
  - 화성(왕배산/여울공원/금반저류지/돌모루/중동) + 오산(죽미실내/실외/시립) 통합 코트 조회
  - 시간/코트 필터, 지난 주 자동 압축, 요일 헤더 고정
  - 날씨: 단기예보(3일, 위젯+슬롯별 강수확률) + 중기예보(4~10일, 날짜 배지)
  - 다크모드 자동 전환(OS 연동)
  - 텔레그램 신규 빈자리 알림 (오픈 시각 반영, 조용한 시간대 보류, 코트별 그룹핑, 날씨 첨부)
  - 모바일 2단계 탭(예약 이동 전 상세 확인)
"""
import requests, json, time, os, calendar
from datetime import datetime, timezone, timedelta, date

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
KMA_SERVICE_KEY = os.environ.get("KMA_SERVICE_KEY", "")
KMA_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
KMA_NX, KMA_NY = 62, 119  # 동탄 기준 격자좌표 (최초 실행 결과로 유효성 확인 필요)
KMA_ANNOUNCE_HOURS = [2, 5, 8, 11, 14, 17, 20, 23]
WEATHER_CACHE_FILE = "weather_cache.json"

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
    slots = {}  # "YYYYMMDD-HHMM": {"tmp":.., "pop":.., "sky":.., "pty":..}
    for it in items:
        d, t, cat, val = it["fcstDate"], it["fcstTime"], it["category"], it["fcstValue"]
        by_date.setdefault(d, {"tmp": [], "pop": [], "sky": [], "pty": []})
        slots.setdefault(f"{d}-{t}", {})
        if cat == "TMP":
            by_date[d]["tmp"].append(float(val)); slots[f"{d}-{t}"]["tmp"] = val
        elif cat == "POP":
            by_date[d]["pop"].append(int(val)); slots[f"{d}-{t}"]["pop"] = int(val)
        elif cat == "SKY":
            by_date[d]["sky"].append(val); slots[f"{d}-{t}"]["sky"] = val
        elif cat == "PTY":
            by_date[d]["pty"].append(val); slots[f"{d}-{t}"]["pty"] = val

    summary = {}
    sorted_days = sorted(by_date.items())
    for d, v in sorted_days[:3]:  # 위젯용: 항상 3일 고정
        summary[d] = {
            "tmin": round(min(v["tmp"])) if v["tmp"] else None,
            "tmax": round(max(v["tmp"])) if v["tmp"] else None,
            "pop":  max(v["pop"]) if v["pop"] else 0,
            "sky":  max(set(v["sky"]), key=v["sky"].count) if v["sky"] else "1",
            "pty":  "1" if any(p != "0" for p in v["pty"]) else "0",
        }
    # 위젯 3일 넘게 실제로 받아온 날짜가 있으면(발표시각에 따라 종종 있음) 버리지 않고
    # 배지 전용으로 보관 — 중기예보(보통 +3일부터)와의 빈틈을 자연스럽게 메움
    extra_days = {}
    for d, v in sorted_days[3:]:
        extra_days[d] = {
            "tmin": round(min(v["tmp"])) if v["tmp"] else None,
            "tmax": round(max(v["tmp"])) if v["tmp"] else None,
            "pop":  max(v["pop"]) if v["pop"] else 0,
            "sky":  max(set(v["sky"]), key=v["sky"].count) if v["sky"] else "1",
            "pty":  "1" if any(p != "0" for p in v["pty"]) else "0",
        }
    # 슬롯(시간대별)은 위젯 3일 범위 밖은 제거 (용량 절약) — extra_days는 날짜 배지에만 쓰이므로 슬롯 불필요
    valid_dates = set(summary.keys())
    slots = {k: v for k, v in slots.items() if k.split("-")[0] in valid_dates}
    today_str = datetime.now(KST).strftime("%Y%m%d")  # 라벨링 기준 — API 응답 순서가 아닌 실제 오늘 날짜
    return {"base_date": base_date, "base_time": base_time, "today": today_str,
            "days": summary, "slots": slots, "extra_days": extra_days}


# ===== 기상특보 (오늘 발효 중인 특보만, 배너용) =====
WARN_AREA_CODE = "L1013200"  # 화성 (특보구역코드표에서 확인됨)
WARN_URL = "http://apis.data.go.kr/1360000/WthrWrnInfoService/getWthrWrnList"
WARN_TEST_MODE = True  # 배너 화면 디자인 확인용 — 확인 끝나면 False로 변경

def fetch_warnings():
    """
    현재 발효 중인 기상특보 조회 (낙뢰/폭염/강풍 등 — 강수확률로는 안 잡히는 위험 정보).
    실패 시 빈 리스트 반환 — 실패해도 나머지 기능에 영향 없음.
    """
    if not KMA_SERVICE_KEY:
        return []
    try:
        r = requests.get(WARN_URL, params={
            "serviceKey": KMA_SERVICE_KEY, "pageNo": 1, "numOfRows": 10,
            "dataType": "JSON", "areaCode": WARN_AREA_CODE,
        }, timeout=15)
        data = r.json()
        if data["response"]["header"]["resultCode"] != "00":
            print(f"  [!] 기상특보 오류: {data['response']['header']['resultMsg']}")
            return []
        items = data["response"]["body"]["items"].get("item", [])
        if isinstance(items, dict):
            items = [items]
        result = []
        for it in items:
            result.append({"title": it.get("t6", it.get("t1", "특보"))})
        return result
    except Exception as e:
        print(f"  [!] 기상특보 조회 실패: {e}")
        return []


# ===== 중기예보 (기상청, 화성 지역 — 날짜 배지용, 3~10일차만) =====
MID_REG_ID = "11B20604"  # 화성 (예보구역코드표에서 확인됨)
MID_LAND_URL = "http://apis.data.go.kr/1360000/MidFcstInfoService/getMidLandFcst"
MID_TA_URL   = "http://apis.data.go.kr/1360000/MidFcstInfoService/getMidTa"

def fetch_midterm():
    """
    중기예보(3~10일차) 조회 → 날짜별 최저/최고기온 + 강수확률만 추출 (달력 날짜 배지 전용).
    단기예보(오늘~+2일)와 정확히 이어지는 +3일부터 시작. 위젯에는 넣지 않음 (복잡도 방지).
    실패 시 빈 dict 반환 — 실패해도 나머지 기능에 영향 없음.
    """
    if not KMA_SERVICE_KEY:
        return {}
    KST = timezone(timedelta(hours=9))
    now = datetime.now(KST)
    # 발표시각: 06:00 또는 18:00 중 가장 최근 발표
    if now.hour >= 18:
        base_dt = now.replace(hour=18, minute=0, second=0, microsecond=0)
    elif now.hour >= 6:
        base_dt = now.replace(hour=6, minute=0, second=0, microsecond=0)
    else:
        base_dt = (now - timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
    tmFc = base_dt.strftime("%Y%m%d%H%M")

    try:
        r1 = requests.get(MID_LAND_URL, params={
            "serviceKey": KMA_SERVICE_KEY, "pageNo": 1, "numOfRows": 10,
            "dataType": "JSON", "regId": MID_REG_ID, "tmFc": tmFc,
        }, timeout=15)
        r2 = requests.get(MID_TA_URL, params={
            "serviceKey": KMA_SERVICE_KEY, "pageNo": 1, "numOfRows": 10,
            "dataType": "JSON", "regId": MID_REG_ID, "tmFc": tmFc,
        }, timeout=15)
        land_data = r1.json()
        ta_data = r2.json()
        if land_data["response"]["header"]["resultCode"] != "00":
            print(f"  [!] 중기육상예보 오류: {land_data['response']['header']['resultMsg']}")
            return {}
        if ta_data["response"]["header"]["resultCode"] != "00":
            print(f"  [!] 중기기온 오류: {ta_data['response']['header']['resultMsg']}")
            return {}
        land = land_data["response"]["body"]["items"]["item"][0]
        ta = ta_data["response"]["body"]["items"]["item"][0]
    except Exception as e:
        print(f"  [!] 중기예보 조회 실패: {e}")
        return {}

    result = {}
    base_date = base_dt.date()
    real_today = now.date()
    # 발표일(base_date)과 실제 오늘이 다를 수 있음(자정~06시 사이엔 어제 18시 발표분을 씀) —
    # 항상 '실제 오늘+3일'부터 시작하도록 보정 (그렇지 않으면 그 시간대에 하루가 통째로 빠짐)
    start_n = 3 + (real_today - base_date).days
    end_n = min(10, start_n + 7)  # API가 최대 10일차까지만 제공
    for n in range(start_n, end_n + 1):
        d = base_date + timedelta(days=n)
        dkey = d.strftime("%Y%m%d")
        if n <= 7:
            pop_am, pop_pm = land.get(f"rnSt{n}Am"), land.get(f"rnSt{n}Pm")
            pops = [p for p in (pop_am, pop_pm) if p is not None]
            pop = max(pops) if pops else None
            wf = land.get(f"wf{n}Pm") or land.get(f"wf{n}Am")  # 날씨 텍스트(맑음/구름많음/비 등) — 오후 우선
        else:
            pop = land.get(f"rnSt{n}")
            wf = land.get(f"wf{n}")
        tmin, tmax = ta.get(f"taMin{n}"), ta.get(f"taMax{n}")
        if tmin is None and tmax is None and pop is None:
            continue
        result[dkey] = {"tmin": tmin, "tmax": tmax, "pop": pop, "wf": wf}
    return result


# ===== 텔레그램 신규 빈자리 알림 =====
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
NOTIFY_HOURS = {18, 20}  # 알림 대상 시간대 — 대시보드 기본 필터(저녁)와 동일, 필요시 조정
HSCITY_OPEN_DAY, HSCITY_OPEN_HOUR = 27, 10  # 화성 예약시스템: 매월 27일 10:00에 '다음달' 오픈
OSAN_OPEN_DAY,   OSAN_OPEN_HOUR   = 26, 20  # 오산 예약시스템: 매월 26일 20:00에 '다음달' 오픈
QUIET_START_HOUR, QUIET_END_HOUR  = 23, 7   # 23시~07시(다음날)는 발송 보류, 아침에 모아서 전송
PREV_STATE_FILE = "previous_slots.json"
PENDING_FILE    = "pending_notify.json"

def _notify_date_range(today):
    """이번달(오늘~말일) + 다음달(각 시스템 오픈일 지났을 때만) 범위 계산."""
    _, cur_last = calendar.monthrange(today.year, today.month)
    cur_month_end = today.replace(day=cur_last)
    ny, nm = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    _, next_last = calendar.monthrange(ny, nm)
    next_month_start = date(ny, nm, 1)
    next_month_end = date(ny, nm, next_last)
    return cur_month_end, next_month_start, next_month_end

def _is_quiet_hour(now):
    h = now.hour
    return h >= QUIET_START_HOUR or h < QUIET_END_HOUR

def _format_group_message(entries):
    """코트별로 묶어서 메시지 구성. entries: [{"court","md","wd","begin","end","pop"}, ...]"""
    grouped = {}
    order = []
    for e in entries:
        if e["court"] not in grouped:
            grouped[e["court"]] = []
            order.append(e["court"])
        grouped[e["court"]].append(e)
    lines = ["🎾 새 빈자리 발생!"]
    for court in order:
        lines.append(f"\n<b>{court}</b>")
        for e in grouped[court]:
            wtxt = f"  💧{e['pop']}%" if e.get("pop") is not None else ""
            lines.append(f"  {e['md']}({e['wd']}) {e['begin']}~{e['end']}{wtxt}")
    return "\n".join(lines)

def notify_new_slots(result, weather):
    """
    직전 실행과 비교해 새로 열린 빈자리를 텔레그램으로 알림 (NOTIFY_HOURS 시간대만).
    - 기간: 오늘~이번달 말일은 항상, 다음달은 각 시스템 오픈 일시가 지난 경우만
    - 날씨: 단기예보 범위(3일) 내 날짜면 강수확률을 함께 표시
    - 조용한 시간대(23~07시): 즉시 발송 대신 보류했다가, 다음 정상 시간대 실행 때 모아서 전송
    - 최초 실행: 알림 폭탄 방지를 위해 기준선만 저장, 알림 없음
    - 코트별로 그룹핑해서 가독성 확보
    """
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("  [!] 텔레그램 미설정 — 알림 생략")
        return

    KST = timezone(timedelta(hours=9))
    now = datetime.now(KST)
    today = now.date()
    cur_month_end, next_month_start, next_month_end = _notify_date_range(today)
    hscity_open_at = now.replace(day=HSCITY_OPEN_DAY, hour=HSCITY_OPEN_HOUR, minute=0, second=0, microsecond=0)
    osan_open_at   = now.replace(day=OSAN_OPEN_DAY,   hour=OSAN_OPEN_HOUR,   minute=0, second=0, microsecond=0)
    weather_days = (weather or {}).get("days", {})

    current = set()
    entry_by_key = {}
    for c in result:
        is_osan = str(c["idx"]).startswith("osan_")
        open_at = osan_open_at if is_osan else hscity_open_at
        for s in c["empty_slots"]:
            try:
                hour = int(s["begin"].split(":")[0])
                sdate = datetime.strptime(s["date"], "%Y-%m-%d").date()
            except Exception:
                continue
            if hour not in NOTIFY_HOURS:
                continue
            if today <= sdate <= cur_month_end:
                pass  # 이번달: 항상 허용
            elif next_month_start <= sdate <= next_month_end and now >= open_at:
                pass  # 다음달: 오픈 일시 지난 경우만 허용
            else:
                continue
            key = f'{c["idx"]}|{s["date"]}|{s["begin"]}'
            current.add(key)
            wkey = s["date"].replace("-", "")
            pop = weather_days.get(wkey, {}).get("pop")
            entry_by_key[key] = {
                "court": c["name"], "md": s["date"][5:],
                "wd": "월화수목금토일"[sdate.weekday()],
                "begin": s["begin"], "end": s["end"], "pop": pop,
            }

    is_first_run = not os.path.exists(PREV_STATE_FILE)
    prev = set()
    if not is_first_run:
        try:
            with open(PREV_STATE_FILE, encoding="utf-8") as f:
                prev = set(json.load(f))
        except Exception:
            prev = set()

    if is_first_run:
        print(f"  [알림] 최초 실행 — 기준선 {len(current)}건만 저장, 알림 생략")
    else:
        new_entries = [entry_by_key[k] for k in sorted(current - prev)]

        pending = []
        if os.path.exists(PENDING_FILE):
            try:
                with open(PENDING_FILE, encoding="utf-8") as f:
                    pending = json.load(f)
            except Exception:
                pending = []

        if _is_quiet_hour(now):
            if new_entries:
                pending.extend(new_entries)
                with open(PENDING_FILE, "w", encoding="utf-8") as f:
                    json.dump(pending, f, ensure_ascii=False)
                print(f"  [알림] 조용한 시간대 — {len(new_entries)}건 보류 (누적 {len(pending)}건)")
            else:
                print("  [알림] 조용한 시간대 — 신규 없음")
        else:
            combined = pending + new_entries
            if combined:
                msg = _format_group_message(combined[:30])  # 과다 알림 방지
                more = len(combined) - 30
                if more > 0:
                    msg += f"\n\n...외 {more}건 더"
                try:
                    requests.post(
                        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
                        data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                        timeout=10,
                    )
                    print(f"  [알림] 신규 빈자리 {len(combined)}건 전송 완료 (보류분 {len(pending)}건 포함)")
                except Exception as e:
                    print(f"  [!] 텔레그램 전송 실패: {e}")
                if os.path.exists(PENDING_FILE):
                    os.remove(PENDING_FILE)
            else:
                print("  [알림] 신규 빈자리 없음")

    with open(PREV_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(current), f, ensure_ascii=False)


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
    if weather:
        print(f"{len(weather.get('days', {}))}일치 확보", end="")
        # 최신 응답에서 '오늘' 데이터가 빠졌으면(자정 임박 등) 캐시에서 병합해 자정까지 유지
        today_str = weather.get("today", "")
        if os.path.exists(WEATHER_CACHE_FILE):
            try:
                with open(WEATHER_CACHE_FILE, encoding="utf-8") as f:
                    cache = json.load(f)
                merged = 0
                for dkey, dval in cache.get("days", {}).items():
                    if dkey >= today_str and dkey not in weather["days"]:
                        weather["days"][dkey] = dval
                        merged += 1
                for skey, sval in cache.get("slots", {}).items():
                    if skey.split("-")[0] >= today_str and skey not in weather["slots"]:
                        weather["slots"][skey] = sval
                if merged:
                    # 병합으로 4일치 이상이 되면, 가장 이른(오늘 포함) 3일만 유지
                    keep_dates = sorted(weather["days"].keys())[:3]
                    weather["days"] = {k: v for k, v in weather["days"].items() if k in keep_dates}
                    weather["slots"] = {k: v for k, v in weather["slots"].items() if k.split("-")[0] in keep_dates}
                    print(f" (캐시에서 {merged}일치 보완)")
                else:
                    print()
            except Exception:
                print()
        else:
            print()
        try:
            with open(WEATHER_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(weather, f, ensure_ascii=False)
        except Exception:
            pass
    else:
        # 조회 자체가 실패한 경우 — 직전 성공 데이터로 통째로 대체
        if os.path.exists(WEATHER_CACHE_FILE):
            try:
                with open(WEATHER_CACHE_FILE, encoding="utf-8") as f:
                    weather = json.load(f)
                print(f"조회 실패 — 직전 데이터로 대체 ({len(weather.get('days', {}))}일치)")
            except Exception:
                weather = {}
                print("조회 실패, 캐시도 손상됨 — 생략")
        else:
            print("조회 실패, 캐시 없음 — 생략")

    # 신규 빈자리 텔레그램 알림 (날씨 정보 함께 전달)
    notify_new_slots(result, weather)

    # 중기예보 (3~10일차, 달력 배지 전용 — 위젯에는 영향 없음)
    print(f"  [중기예보] 화성 3~10일차 조회 중...", end=" ")
    try:
        midterm = fetch_midterm()
        weather["days_mid"] = {}
        # 단기예보에서 3일 넘게 실제로 받아온 날짜가 있으면 우선 사용 (sky/pty가 더 정확함)
        extra = weather.get("extra_days", {})
        if extra:
            weather["days_mid"].update(extra)
        # 중기예보는 이미 위에서 채운 날짜와 안 겹치는 것만 추가
        if midterm:
            for dkey, dval in midterm.items():
                weather["days_mid"].setdefault(dkey, dval)
        weather.pop("extra_days", None)  # 배지에는 days_mid로 통일, 원본은 정리
        print(f"{len(weather['days_mid'])}일치 확보 (단기예보 보완 {len(extra)}일 포함)")
    except Exception as e:
        print(f"실패: {e}")

    # 기상특보 (오늘 발효 중, 배너용 — 시험 적용)
    print(f"  [특보] 조회 중...", end=" ")
    try:
        warnings_list = fetch_warnings()
        if WARN_TEST_MODE:  # 배너 화면 확인용 — 확인 끝나면 False로 바꾸세요
            warnings_list = warnings_list + [{"title": "[테스트] 폭염주의보"}]
        weather["warnings"] = warnings_list
        print(f"{len(warnings_list)}건" if warnings_list else "없음")
    except Exception as e:
        print(f"실패: {e}")

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
  border-bottom:1px solid var(--border);
  position:sticky;top:0;z-index:20}
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

.day-wx{position:absolute;top:2px;right:3px;font-size:10px;font-weight:700;
  color:var(--muted);white-space:nowrap;line-height:1;cursor:pointer}
.day-wx .dwx-pop.hi{color:var(--sat);font-weight:800}
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
.weather-wrap{display:flex;gap:8px;margin-top:12px;overflow-x:auto;
  -webkit-overflow-scrolling:touch;touch-action:pan-x}

.warn-banner{background:#fef2f2;border:1px solid #fca5a5;color:#b91c1c;
  border-radius:10px;padding:10px 14px;margin-top:12px;font-size:13px;
  font-weight:700;display:flex;align-items:center;gap:8px}
[data-theme=dark] .warn-banner{background:#3f1d1d;border-color:#7f1d1d;color:#fca5a5}
.wcard{flex:1;min-width:90px;background:var(--card);border:1px solid var(--border);
  border-radius:12px;padding:10px 8px;text-align:center;box-shadow:var(--shadow);cursor:pointer}
.wcard.sel{box-shadow:0 0 0 2px var(--accent)}
.wcard .wd{font-size:11px;font-weight:700;color:var(--muted);margin-bottom:4px}
.wcard .wi{font-size:22px;line-height:1}
.wcard .wt{font-size:13px;font-weight:700;margin-top:4px}
.wcard .wt .lo{color:var(--muted);font-weight:500}
.wcard .wp{font-size:11px;margin-top:2px}
.wcard .wp.high{color:var(--sat);font-weight:700}
@media(max-width:700px){.wcard{min-width:76px;padding:8px 6px}.wcard .wi{font-size:18px}}

.weather-hourly{display:flex;gap:0;margin-top:8px;background:var(--card);
  border:1px solid var(--border);border-radius:12px;padding:12px 8px;
  overflow-x:auto;-webkit-overflow-scrolling:touch;touch-action:pan-x;
  box-shadow:var(--shadow)}
.whr{flex:0 0 auto;min-width:52px;text-align:center;padding:0 6px;
  border-right:1px solid var(--border)}
.whr:last-child{border-right:none}
.whr-t{font-size:11px;color:var(--muted);font-weight:700;margin-bottom:4px}
.whr-i{font-size:18px;line-height:1;margin-bottom:4px}
.whr-temp{font-size:13px;font-weight:700}
.whr-pop{font-size:10px;color:var(--sat);margin-top:2px}
.whr.now{background:color-mix(in srgb, var(--accent) 12%, transparent);
  border-radius:8px;margin:0 1px}
.whr.now .whr-t{color:var(--accent)}
.now-dot{color:var(--accent);font-size:8px}
@media(max-width:700px){.whr{min-width:44px}.whr-i{font-size:15px}}

.tip{position:fixed;pointer-events:none;z-index:9999;
  background:#1e293b;color:#fff;font-size:12px;font-weight:500;
  padding:5px 10px;border-radius:7px;white-space:nowrap;
  box-shadow:0 4px 12px rgba(0,0,0,.25);opacity:0;transition:opacity .1s}

.confirm-bar{position:fixed;left:0;right:0;bottom:0;z-index:1000;
  background:var(--card);border-top:1px solid var(--border);
  padding:12px 14px;display:flex;align-items:center;justify-content:space-between;
  gap:10px;box-shadow:0 -4px 16px rgba(0,0,0,.15);
  transform:translateY(110%);transition:transform .2s ease}
.confirm-bar.show{transform:translateY(0)}
.confirm-bar .cb-txt{font-size:13px;font-weight:700;color:var(--text);
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.confirm-bar button{flex-shrink:0;background:var(--accent);color:#fff;border:none;
  padding:9px 16px;border-radius:9px;font-weight:700;font-size:13px;
  font-family:inherit;cursor:pointer}

@media(max-width:700px){
  table.cal td{height:120px;padding:4px 3px}
  .slot{font-size:10px;padding:3px 4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .dnum{font-size:12px}
  .slots{grid-template-columns:1fr}
  .slots.exp{grid-template-columns:1fr}  /* 더보기 펼쳐도 1열 유지 */
  .slot-hint{display:none}
  .sn-f{display:none}
  .sn-s{display:inline}
  .sn-tf{display:none}  /* 모바일: 전체 시간 숨김, sn-s가 대신 표시 */

  /* 완전히 지난 주 — 모바일에서만 얇게 압축 (PC는 그대로 유지) */
  table.cal tr.row-past td{height:18px!important;padding:2px 4px!important;overflow:hidden}
  table.cal tr.row-past .day-wx,
  table.cal tr.row-past .holi,
  table.cal tr.row-past .slots{display:none}
  table.cal tr.row-past .dnum{font-size:10px;margin:0}

  /* 날짜 배지 — 모바일은 좁아서 아이콘만, 텍스트(온도/강수확률)는 숨김 */
  .day-wx{font-size:12px;top:1px;right:1px}
  .day-wx .dwx-txt{display:none}

  /* 모바일 헤더 한 줄 강제 */
  .hdr h1{font-size:15px}
  .hdr h1 em{font-size:9px;margin-left:3px}
  .update-time{font-size:9px;margin-left:5px;display:block;margin-top:2px}
  .btn.icon{padding:5px 8px;font-size:13px}
}
</style>
</head>
<body>
<script>
/* 초기 테마: 저장된 수동 설정 > 시스템(OS) 다크모드 설정 순으로 결정 */
(function(){
  const saved = localStorage.getItem('theme');
  const sysDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  document.body.dataset.theme = saved || (sysDark ? 'dark' : 'light');
})();
</script>
<div class="tip" id="tip"></div>
<div class="confirm-bar" id="confirmBar">
  <span class="cb-txt" id="cbTxt"></span>
  <button onclick="goPending()">예약하러 가기 →</button>
</div>

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
<div class="weather-hourly" id="weatherHourly" style="display:none"></div>
<div id="warnBanner"></div>


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
/* 날짜(YYYYMMDD) 문자열을 실제 오늘(WEATHER.today) 기준으로 오늘/내일/모레 라벨링 — 배열 순서가 아닌 실제 날짜로 판단 */
function dayLabel(dateStr, todayStr){
  const p = s => new Date(+s.slice(0,4), +s.slice(4,6)-1, +s.slice(6,8));
  const diff = Math.round((p(dateStr) - p(todayStr)) / 86400000);
  if(diff === 0) return '오늘';
  if(diff === 1) return '내일';
  if(diff === 2) return '모레';
  return `${dateStr.slice(4,6)}/${dateStr.slice(6,8)}`;
}

/* 날짜(YYYYMMDD) → 요일(월화수목금토일) */
function wDayName(dateStr){
  const y=+dateStr.slice(0,4), m=+dateStr.slice(4,6)-1, d=+dateStr.slice(6,8);
  const dw = new Date(y,m,d).getDay();  // 0=일 1=월 ... 6=토
  return "월화수목금토일"[(dw+6)%7];
}

/* 기상특보 배너 — 발효 중인 특보가 있을 때만 표시 */
function renderWarnings(){
  const el = document.getElementById('warnBanner');
  if(!el) return;
  const list = (WEATHER && WEATHER.warnings) ? WEATHER.warnings : [];
  if(list.length === 0){ el.innerHTML = ''; return; }
  const titles = list.map(w => w.title).join(' · ');
  el.innerHTML = `<div class="warn-banner">⚠️ 기상특보: ${titles}</div>`;
}

let expandedWDay;  // undefined = 아직 초기화 안 됨 (최초 1회만 "오늘"로 기본 오픈)

function renderWeather(){
  const wrap = document.getElementById('weatherWrap');
  if(!wrap) return;
  const days = (WEATHER && WEATHER.days) ? WEATHER.days : {};
  const keys = Object.keys(days).sort();
  if(keys.length === 0){ wrap.style.display='none'; return; }
  const todayStr = WEATHER.today || keys[0];
  // 오늘(실제 날짜) 데이터가 아예 없으면(자정 임박 등) — 가장 빠른 날짜를 '오늘' 기준으로 승격
  const refStr = days[todayStr] ? todayStr : keys[0];
  if(expandedWDay === undefined) expandedWDay = keys[0];  // 최초 로드시에만 "오늘"(또는 첫 데이터) 기본 오픈
  let h = '';
  keys.forEach((d) => {
    const v = days[d];
    const md = d.slice(4,6)+'/'+d.slice(6,8);
    const popCls = v.pop >= 60 ? 'high' : '';
    const sel = d === expandedWDay ? ' sel' : '';
    h += `<div class="wcard${sel}" onclick="toggleWDay('${d}')">
      <div class="wd">${dayLabel(d, refStr)} <span style="font-weight:400">${md}(${wDayName(d)})</span></div>
      <div class="wi">${skyIcon(v.sky, v.pty)}</div>
      <div class="wt">${v.tmax ?? '-'}° <span class="lo">${v.tmin ?? '-'}°</span></div>
      <div class="wp ${popCls}">💧 ${v.pop}%</div>
    </div>`;
  });
  wrap.innerHTML = h;
  renderWeatherHourly();
}

function toggleWDay(d){
  expandedWDay = (expandedWDay === d) ? null : d;
  renderWeather();
}

function renderWeatherHourly(){
  const panel = document.getElementById('weatherHourly');
  if(!panel) return;
  if(!expandedWDay){ panel.innerHTML=''; panel.style.display='none'; return; }
  const wslots = (WEATHER && WEATHER.slots) ? WEATHER.slots : {};
  let items = Object.keys(wslots).filter(k=>k.startsWith(expandedWDay)).sort();

  // 오늘 날짜면 이미 지난 시간대는 목록에서 제외 (현재 시각이 속한 구간부터만 표시)
  let nowBucketKey = null;
  if(expandedWDay === WEATHER.today){
    const curHour = new Date().getHours();
    const buckets = [0,3,6,9,12,15,18,21].filter(h=>h<=curHour);
    const nb = buckets.length ? Math.max(...buckets) : 0;
    nowBucketKey = `${expandedWDay}-${String(nb).padStart(2,'0')}00`;
    items = items.filter(k => k >= nowBucketKey);
  }

  if(items.length === 0){ panel.innerHTML=''; panel.style.display='none'; return; }
  panel.style.display='flex';

  let h='';
  items.forEach(k=>{
    const hm = k.split('-')[1];  // "0600"
    const hr = parseInt(hm.slice(0,2));
    const v = wslots[k];
    const nowCls = k === nowBucketKey ? ' now' : '';
    h += `<div class="whr${nowCls}">
      <div class="whr-t">${hr}시${nowCls ? ' <span class=\'now-dot\'>●</span>' : ''}</div>
      <div class="whr-i">${skyIcon(v.sky, v.pty)}</div>
      <div class="whr-temp">${v.tmp ?? '-'}°</div>
      <div class="whr-pop">💧${v.pop ?? 0}%</div>
    </div>`;
  });
  panel.innerHTML = h;
}

/* 코트 슬롯 시각(date, "HH:MM")에 가장 가까운 3시간 단위 예보 찾기 */
function weatherForSlotTime(ds, beginHm){
  const wslots = (WEATHER && WEATHER.slots) ? WEATHER.slots : {};
  const dKey = ds.replace(/-/g, '');
  const hour = parseInt(beginHm.split(':')[0]);
  // 기상청 발표 시간대: 0,3,6,9,12,15,18,21 — 가장 가까운(이후) 시간대로 반올림
  const fcstHours = [0,3,6,9,12,15,18,21];
  let nearest = fcstHours.reduce((a,b)=> Math.abs(b-hour)<=Math.abs(a-hour) && b<=hour+2 ? b : a, fcstHours[0]);
  const tKey = `${dKey}-${String(nearest).padStart(2,'0')}00`;
  return wslots[tKey] || null;
}
/* 중기예보 날씨 텍스트(맑음/구름많음/흐림/비 등) → 아이콘 */
function wfIcon(wf){
  if(!wf) return '☁️';
  if(wf.includes('비') || wf.includes('소나기')) return '🌧️';
  if(wf.includes('눈')) return '❄️';
  if(wf.includes('맑음')) return '☀️';
  if(wf.includes('구름많') || wf.includes('구름조금')) return '⛅';
  return '☁️';
}

function weatherForDate(ds){
  const days = (WEATHER && WEATHER.days) ? WEATHER.days : {};
  const key = ds.replace(/-/g, '');
  if(days[key]) return days[key];
  const mid = (WEATHER && WEATHER.days_mid) ? WEATHER.days_mid : {};
  return mid[key] || null;
}
const GH = {금반저류지:215, 왕배산:145, 여울공원:340, 돌모루:275, 죽미실내:25, 죽미실외:165, 시립:55, 중동:190};
function groupColor(g){ return `hsl(${GH[g]??0},65%,50%)`; }
function slotColor(c){ return groupColor(c.group||c.name); }

function shortNm(c){
  const n=(c.name.match(/(\d+)번/)||[])[1]||'';
  const m={금반저류지:'금반',왕배산:'왕배산',여울공원:'여울',돌모루:'돌모루',죽미실내:'죽미(내)',죽미실외:'죽미(외)',시립:'시립',중동:'중동'};
  return (m[c.group]||c.group)+n;
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

/* 필터 버튼 표시명 (내부 그룹 키는 유지, 화면 표시만 변경) */
const GROUP_LABEL = {금반저류지:'금반', 여울공원:'여울', 죽미실내:'죽미(실내)', 죽미실외:'죽미(실외)'};
function groupLabel(g){ return GROUP_LABEL[g] || g; }

/* ★ 코트 버튼 — 배경색 꽉 채우기 */
const gf=document.getElementById('gf');
allGroups.forEach(g=>{
  const b=document.createElement('button');
  b.className='btn-court'; b.dataset.v=g; b.textContent=groupLabel(g);
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
    const tip2=`${s.court.name}  ${s.begin}~${s.end}`;
    const wx = weatherForSlotTime(ds, s.begin);
    const tipFull = wx ? `${tip2}  💧${wx.pop ?? '-'}%` : tip2;
    const rainMark = (wx && wx.pop >= 70) ? ' ☔' : '';
    const hOnly=s.begin.split(':')[0];  // "18:00" → "18"
    h+=`<a class="slot" href="${s.court.url}" target="_blank"
      style="background:${col}"
      data-url="${s.court.url}" data-tip="${tipFull.replace(/"/g,'&quot;')}"
      onclick="return handleSlotClick(event,this)"
      onmouseenter="showTip(event,'${tipFull.replace(/'/g,"\\'")}' )"
      onmouseleave="hideTip()"
    ><span class='t'><span class='sn-tf'>${s.begin}</span><span class='sn-s'>${parseInt(hOnly)}시</span></span> <span class='sn-f'>${sn}</span>${rainMark}</a>`;
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
    // 이 행(주)의 모든 날짜가 이미 지났는지 미리 확인 → 지났으면 얇게 압축
    let rowHasFuture=false, rowHasDay=false;
    {
      let probe=day;
      for(let cc=0;cc<7;cc++){
        const isBlank=(row===0&&cc<sd)||probe>td;
        if(!isBlank){
          rowHasDay=true;
          const dtChk=new Date(y,m-1,probe);
          if(dtChk>=T0) rowHasFuture=true;
          probe++;
        }
      }
    }
    const rowPast = rowHasDay && !rowHasFuture;

    html+=`<tr class="${rowPast?'row-past':''}">`;
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
        if(wx){
          const icon = wx.wf ? wfIcon(wx.wf) : skyIcon(wx.sky, wx.pty);
          const popCls = wx.pop >= 70 ? ' hi' : '';
          const wxTipTxt = `최고 ${wx.tmax}° / 최저 ${wx.tmin}° · 강수확률 ${wx.pop}%`;
          html+=`<div class="day-wx" title="${wxTipTxt}" onclick="showWxTip(event,'${wxTipTxt.replace(/'/g,"\\'")}' )">${icon}<span class="dwx-pop${popCls}">💧${wx.pop}%</span><span class="dwx-txt"> ${wx.tmax}°</span></div>`;
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

/* 날짜 배지(day-wx) — title 속성은 모바일 터치에서 안 뜨므로, 탭하면 잠깐 표시 */
function showWxTip(e, txt){
  e.stopPropagation();
  showTip(e, txt);
  clearTimeout(window._wxTipTimer);
  window._wxTipTimer = setTimeout(hideTip, 2500);
}

/* 모바일(터치, hover 불가) 전용 2단계 탭: 1차=상세보기, 2차=이동 */
const touchMode = window.matchMedia('(hover: none) and (pointer: coarse)').matches;
let pendingSlot = null;
const confirmBar = document.getElementById('confirmBar');
const cbTxt = document.getElementById('cbTxt');

function handleSlotClick(e, el){
  if(!touchMode) return true;              // 데스크톱: 기존처럼 바로 이동
  if(pendingSlot === el){                  // 같은 슬롯 재탭 → 이동 허용
    pendingSlot = null; confirmBar.classList.remove('show');
    return true;
  }
  e.preventDefault();                      // 1차 탭: 이동 막고 상세 표시
  pendingSlot = el;
  cbTxt.textContent = el.dataset.tip;
  confirmBar.dataset.url = el.dataset.url;
  confirmBar.classList.add('show');
  return false;
}
function goPending(){
  const url = confirmBar.dataset.url;
  if(url) window.open(url, '_blank');
  pendingSlot = null;
  confirmBar.classList.remove('show');
}
document.addEventListener('click', (e)=>{
  if(!pendingSlot) return;
  if(e.target.closest('.slot') || e.target.closest('.confirm-bar')) return;
  pendingSlot = null; confirmBar.classList.remove('show');
});

function toggleTheme(){
  const next = document.body.dataset.theme==='dark' ? 'light' : 'dark';
  document.body.dataset.theme = next;
  localStorage.setItem('theme', next);  // 수동 설정 저장 — 이후 시스템 변경보다 우선
}
/* 사용자가 수동으로 설정한 적 없으면, OS 다크모드 전환 시 자동 반영 */
window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', e=>{
  if(!localStorage.getItem('theme')){
    document.body.dataset.theme = e.matches ? 'dark' : 'light';
  }
});

// 초기 UI 동기화
syncUI();
render();
renderWeather();
renderWarnings();

// 시간대별 하이라이트가 실제 시간 흐름에 맞게 유지되도록 5분마다 재계산
// (페이지를 계속 열어둔 채 안 새로고침해도 '지금' 표시가 stale해지지 않음)
setInterval(renderWeatherHourly, 5 * 60 * 1000);
</script>
</body>
</html>"""

if __name__ == "__main__":
    main()
