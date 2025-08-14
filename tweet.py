# -*- coding: utf-8 -*-
import os, json, pathlib, re
from datetime import datetime
import pytz, requests
from requests_oauthlib import OAuth1
from dotenv import load_dotenv
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# ===================== 기본 설정 =====================
load_dotenv()
# API_KEY = os.getenv("API_KEY")
# API_KEY_SECRET = os.getenv("API_KEY_SECRET")
# ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")
# ACCESS_TOKEN_SECRET = os.getenv("ACCESS_TOKEN_SECRET")
# YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
# YT_VIDEO_ID = os.getenv("YT_VIDEO_ID")
# TARGET_TITLE = (os.getenv("TARGET_TITLE") or "Surf").strip()
# TARGET_ARTIST = (os.getenv("TARGET_ARTIST") or "NCT WISH").strip()
# Twitter API Credentials
API_KEY = os.environ["API_KEY"]
API_KEY_SECRET = os.environ["API_KEY_SECRET"]
ACCESS_TOKEN = os.environ["ACCESS_TOKEN"]
ACCESS_TOKEN_SECRET = os.environ["ACCESS_TOKEN_SECRET"]

# YouTube API
YOUTUBE_API_KEY = os.environ["YOUTUBE_API_KEY"]
YT_VIDEO_ID = os.environ["YT_VIDEO_ID"]

# Target Song Info (기본값 설정 + 공백 제거)
TARGET_TITLE = os.environ.get("TARGET_TITLE", "Surf").strip()
TARGET_ARTIST = os.environ.get("TARGET_ARTIST", "NCT WISH").strip()

for k, v in {
    "API_KEY": API_KEY, "API_KEY_SECRET": API_KEY_SECRET,
    "ACCESS_TOKEN": ACCESS_TOKEN, "ACCESS_TOKEN_SECRET": ACCESS_TOKEN_SECRET,
}.items():
    if not v:
        raise SystemExit(f"❌ .env에 {k}가 필요합니다.")

KST = pytz.timezone("Asia/Seoul")
STATE = pathlib.Path("state.json")  # 이전 순위 저장해서 🔺/🔻 계산
SITES = [
    ("멜론 TOP100", "melon_top100"),
    ("멜론 HOT100", "melon_hot100"),
    ("지니", "genie"),
    ("벅스", "bugs"),
    ("FLO", "flo"),
    ("VIBE", "vibe"),  
]



# ===================== 유틸 =====================
def normalize(s: str) -> str:
    s = s.lower()
    s = re.sub(r"\(feat\.?.*?\)|\(prod\.?.*?\)", "", s)
    s = re.sub(r"feat\.?|featuring|prod\.?", "", s)
    s = re.sub(r"[\[\]\(\)\-–—·~_:/.,!?']", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s

def is_match(title: str, artist: str, target_title: str, target_artist: str) -> bool:
    t1, a1 = normalize(title), normalize(artist)
    t2, a2 = normalize(target_title), normalize(target_artist)
    if t2 in t1 or t1 in t2:
        return len(set(a1.split()) & set(a2.split())) > 0
    return False

def load_state():
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_state(d):
    STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

def delta_text(prev, curr):
    if prev is None or curr is None:
        return ""
    if curr < prev:
        return f" (🔺{prev - curr})"
    if curr > prev:
        return f" (🔻{curr - prev})"
    return " (-)"

def format_views(n: int | None) -> str:
    return "❌" if n is None else f"{n:,}"

def tweet(text: str) -> int:
    url = "https://api.twitter.com/2/tweets"
    auth = OAuth1(API_KEY, API_KEY_SECRET, ACCESS_TOKEN, ACCESS_TOKEN_SECRET)
    r = requests.post(url, json={"text": text}, auth=auth, timeout=20)
    print("Tweet:", r.status_code, r.text)
    print("Headers:", {
    "x-rate-limit-limit": r.headers.get("x-rate-limit-limit"),
    "x-rate-limit-remaining": r.headers.get("x-rate-limit-remaining"),
    "x-rate-limit-reset": r.headers.get("x-rate-limit-reset"),
    "retry-after": r.headers.get("retry-after"),
})
    return r.status_code

def as_int(x):
    if isinstance(x, (list, tuple)):
        try:
            return int(x[0])
        except Exception:
            return None
    try:
        return int(x) if x is not None else None
    except Exception:
        return None

# ===================== YouTube 조회수 =====================
def fetch_youtube_views() -> int | None:
    if not (YOUTUBE_API_KEY and YT_VIDEO_ID):
        return None
    try:
        url = "https://www.googleapis.com/youtube/v3/videos"
        params = {"part": "statistics", "id": YT_VIDEO_ID, "key": YOUTUBE_API_KEY}
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return None
        return int(items[0]["statistics"]["viewCount"])
    except Exception as e:
        print("YouTube fetch error:", e)
        return None

# ===================== 공통: 페이지 렌더 + 파싱 =====================
def render_get_html(url: str, timeout_ms=20000) -> str | None:
    """동적 렌더링 페이지를 Playwright로 열고 HTML 반환."""
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(headless=True)
            pg = br.new_page()
            pg.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            html = pg.content()
            br.close()
            return html
    except Exception as e:
        print(f"[render] {url} error:", e)
        return None

# ===================== 사이트별 스크래퍼 =====================
def fetch_melon_top100(title: str, artist: str):
    html = render_get_html("https://www.melon.com/chart/index.htm")
    if not html:
        return None, None

    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("tr.lst50, tr.lst100")
    for row in rows:
        rank_el = row.select_one(".rank")
        title_el = row.select_one(".rank01 a")
        artist_el = row.select_one(".rank02 a")

        if not (rank_el and title_el and artist_el):
            continue

        # 순위
        digits = "".join(c for c in rank_el.get_text(strip=True) if c.isdigit())
        if not digits:
            continue
        rank = int(digits)

        # 등락 (정확한 구조 기반)
        change_val = None
        rank_wrap = row.select_one(".rank_wrap")
        if rank_wrap:
            if rank_wrap.select_one(".rank_up"):
                num = "".join(c for c in rank_wrap.select_one(".up").get_text(strip=True) if c.isdigit())
                change_val = +int(num) if num else 0
            elif rank_wrap.select_one(".rank_down"):
                num = "".join(c for c in rank_wrap.select_one(".down").get_text(strip=True) if c.isdigit())
                change_val = -int(num) if num else 0
            elif rank_wrap.select_one(".rank_static"):
                change_val = 0
            elif rank_wrap.select_one(".rank_new"):
                change_val = 0

        # 매칭
        t = title_el.get_text(" ", strip=True)
        a = artist_el.get_text(" ", strip=True)
        if is_match(t, a, title, artist):
            return rank, change_val

    return None, None


def fetch_melon_hot100(title: str, artist: str):
    html = render_get_html("https://www.melon.com/chart/hot100/index.htm")
    if not html:
        return None, None

    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("tr.lst50, tr.lst100")
    for row in rows:
        rank_el = row.select_one(".rank")
        title_el = row.select_one(".rank01 a")
        artist_el = row.select_one(".rank02 a")

        if not (rank_el and title_el and artist_el):
            continue

        # 순위
        digits = "".join(c for c in rank_el.get_text(strip=True) if c.isdigit())
        if not digits:
            continue
        rank = int(digits)

        # 등락 (정확한 구조 기반)
        change_val = None
        rank_wrap = row.select_one(".rank_wrap")
        if rank_wrap:
            if rank_wrap.select_one(".rank_up"):
                num = "".join(c for c in rank_wrap.select_one(".up").get_text(strip=True) if c.isdigit())
                change_val = +int(num) if num else 0
            elif rank_wrap.select_one(".rank_down"):
                num = "".join(c for c in rank_wrap.select_one(".down").get_text(strip=True) if c.isdigit())
                change_val = -int(num) if num else 0
            elif rank_wrap.select_one(".rank_static"):
                change_val = 0
            elif rank_wrap.select_one(".rank_new"):
                change_val = 0

        # 매칭
        t = title_el.get_text(" ", strip=True)
        a = artist_el.get_text(" ", strip=True)
        if is_match(t, a, title, artist):
            return rank, change_val

    return None, None


# 지니 (데스크톱 Top200 전용, 변동치 포함)
def fetch_genie_rank(title: str, artist: str):
    """
    지니 Top200에서 특정 곡 순위와 변동치 검색
    - 페이지당 50위, 총 4페이지(200위) 순회
    - 반환: (rank:int|None, change_sign:int|None, change_abs:int|None)
    """

    def parse_page(html: str, page: int):
        soup = BeautifulSoup(html, "lxml")
        rows = soup.select("tr.list") or soup.select("tbody tr")
        preview = []

        for idx, tr in enumerate(rows, start=1):
            try:
                # 순위
                rank_tag = tr.select_one(".number") or tr.select_one(".rank")
                if rank_tag:
                    m = re.search(r"\d+", rank_tag.get_text(" ", strip=True))
                    rank = int(m.group()) if m else (page - 1) * 50 + idx
                else:
                    rank = (page - 1) * 50 + idx

                # 변동치
                change_sign, change_abs = (None, None)
                up_tag = tr.select_one(".rank-up")
                down_tag = tr.select_one(".rank-down")

                if up_tag:
                    m = re.search(r"\d+", up_tag.get_text(strip=True))
                    if m:
                        change_sign = +1
                        change_abs = int(m.group())
                elif down_tag:
                    m = re.search(r"\d+", down_tag.get_text(strip=True))
                    if m:
                        change_sign = -1
                        change_abs = int(m.group())

                # 곡/가수
                title_tag = tr.select_one(".title a") or tr.select_one(".title")
                artist_tag = tr.select_one(".artist a") or tr.select_one(".artist")
                song = title_tag.get_text(strip=True) if title_tag else ""
                art = artist_tag.get_text(strip=True) if artist_tag else ""

                # 매칭
                if is_match(song, art, title, artist):
                    return rank, change_sign, change_abs
                if len(preview) < 3:
                    preview.append(f"{rank} | {song} | {art}")

            except Exception:
                continue

        if preview:
            print(f"[지니 p{page} 미스매치]\n  " + "\n  ".join(preview))
        return None, None, None

    # 페이지 순회
    for page in range(1, 5):
        url = f"https://www.genie.co.kr/chart/top200?pg={page}"
        html = render_get_html(url)
        if not html:
            continue
        rank, change_sign, change_abs = parse_page(html, page)
        if rank is not None:
            return (rank, change_sign, change_abs)

    return (None, None, None)



# 벅스 — 구조 고정 파서: div.ranking > strong(현재순위), p.change.up/down > em(변동치)
# 반환: (rank:int|None, change_sign:int|None, change_abs:int|None)
def fetch_bugs_rank(title: str, artist: str):

    URL = "https://music.bugs.co.kr/chart"
    html = render_get_html(URL, timeout_ms=30000)
    if not html:
        return (None, None, None)

    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("tr[rowtype='track']") or \
           soup.select("table.list.trackList > tbody > tr") or \
           soup.select("table.list > tbody > tr") or \
           soup.select("tbody > tr")

    for row in rows:
        ranking_box = row.select_one("div.ranking")
        strong = ranking_box.select_one("strong") if ranking_box else None
        if not strong:
            continue
        m = re.search(r"\d+", strong.get_text(strip=True))
        if not m:
            continue
        curr_rank = int(m.group(0))

        # 변동치
        change_p = ranking_box.select_one("p.change") if ranking_box else None
        change_abs, change_sign = (None, None)
        if change_p:
            em = change_p.select_one("em")
            if em:
                m2 = re.search(r"\d+", em.get_text(strip=True))
                if m2:
                    change_abs = int(m2.group(0))
            cls = " ".join(change_p.get("class", [])).lower()
            if "up" in cls:
                change_sign = +1
            elif "down" in cls:
                change_sign = -1

        # 제목/아티스트
        title_el = row.select_one("th[scope='row'] p.title a") or row.select_one("p.title a") or row.select_one(".title a")
        artist_el = row.select_one("td.left p.artist a") or row.select_one("p.artist a") or row.select_one(".artist a")
        if not (title_el and artist_el):
            continue

        t_txt = title_el.get_text(" ", strip=True)
        a_txt = artist_el.get_text(" ", strip=True)

        if is_match(t_txt, a_txt, title, artist):
            if change_abs is not None and change_sign is not None:
                arrow = "▲" if change_sign > 0 else "▼"
                print(f"[bugs] MATCHED -> rank={curr_rank}, change={arrow}{change_abs} | '{t_txt}' / '{a_txt}'")
            else:
                print(f"[bugs] MATCHED -> rank={curr_rank} | '{t_txt}' / '{a_txt}'")
            return (curr_rank, change_sign, change_abs)

    print("[bugs] target not found in chart")
    return (None, None, None)


# ===================== FLO Top100 (API 직접 호출) =====================
def fetch_flo_rank(title: str, artist: str):
    """
    FLO Top100에서 특정 곡 순위와 변동치 검색
    - API: https://www.music-flo.com/api/display/v1/browser/chart/1/track/list?size=100
    - 반환: (rank:int|None, change_sign:int|None, change_abs:int|None)
    """
    url = "https://www.music-flo.com/api/display/v1/browser/chart/1/track/list?size=100"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.music-flo.com/browse?chartId=1",
    }
    try:
        r = requests.get(url, headers=headers, timeout=20)
        r.raise_for_status()
        data = r.json()

        track_list = data.get("data", {}).get("trackList", [])
        for idx, track in enumerate(track_list, start=1):
            song_name = track.get("name", "")
            artist_name = track.get("representationArtist", {}).get("name", "")
            if is_match(song_name, artist_name, title, artist):
                # rankBadge: 양수 = 상승, 음수 = 하락, 0 = 변동 없음
                rank_badge = track.get("rank", {}).get("rankBadge", 0)
                change_sign, change_abs = (None, None)
                if rank_badge > 0:
                    change_sign = +1
                    change_abs = rank_badge
                elif rank_badge < 0:
                    change_sign = -1
                    change_abs = abs(rank_badge)
                return idx, change_sign, change_abs

        print("[flo] target not found in chart")
        return (None, None, None)
    except Exception as e:
        print("flo error:", e)
        return (None, None, None)

# ===================== VIBE 급상승 (API 직접 호출) =====================
def fetch_vibe_rank(title: str, artist: str):
    """
    VIBE 국내 차트 Top100에서 특정 곡 순위와 변동치 검색
    - 쿠키 만료 방지를 위해 매 호출 시 chart/domestic 페이지 접속 후 API 호출
    """
    session = requests.Session()

    try:
        # 1) 메인 페이지 접속 → 세션 쿠키 발급
        session.get("https://vibe.naver.com/chart/domestic", headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/139.0.0.0 Safari/537.36",
            "Referer": "https://vibe.naver.com/",
            "Accept-Language": "ko,en-US;q=0.9,en;q=0.8"
        }, timeout=10)

        # 2) API 호출
        api_url = "https://apis.naver.com/vibeWeb/musicapiweb/vibe/v1/chart/track/domestic?start=1&display=100"
        headers = {
            "Accept": "application/json",
            "Origin": "https://vibe.naver.com",
            "Referer": "https://vibe.naver.com/chart/domestic",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/139.0.0.0 Safari/537.36",
            "Accept-Language": "ko,en-US;q=0.9,en;q=0.8,ko-KR;q=0.7"
        }
        r = session.get(api_url, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()

        tracks = data.get("response", {}).get("result", {}).get("chart", {}).get("items", {}).get("tracks", [])
        for idx, track in enumerate(tracks, start=1):
            song_name = track.get("trackTitle", "")
            artist_list = track.get("artists", [])
            artist_name = artist_list[0].get("artistName", "") if artist_list else ""

            if is_match(song_name, artist_name, title, artist):
                variation = track.get("rank", {}).get("rankVariation", 0)
                change_sign, change_abs = (None, None)
                if variation > 0:
                    change_sign = +1
                    change_abs = variation
                elif variation < 0:
                    change_sign = -1
                    change_abs = abs(variation)
                return idx, change_sign, change_abs

        print("[vibe] target not found in chart")
        return (None, None, None)

    except Exception as e:
        print("vibe error:", e)
        return (None, None, None)


# ===================== 본문 생성 =====================
def build_text(now_kst: datetime,
               ranks: dict[str, int | None],
               views: int | None,
               prev_state: dict,
               site_changes: dict[str, int] | None = None) -> str:
    # site_changes: 사이트가 직접 알려준 변동치(부호 포함). 예) 벅스 { "bugs": -4 }
    site_changes = site_changes or {}

    def site_delta_to_text(signed: int | None) -> str:
        if signed is None:
            return ""
        if signed > 0:
            return f" (🔺{signed})"
        if signed < 0:
            return f" (🔻{abs(signed)})"
        return " (-)"

    header = f"🏄‍♂️Surf | {now_kst.strftime('%Y-%m-%d %H:%M')}"
    lines = [header, ""]
    prev_ranks = prev_state.get("ranks", {})

    for label, key in SITES:
        curr = as_int(ranks.get(key))
        prev = as_int(prev_ranks.get(key))
        if curr is None:
            lines.append(f"•{label} ❌")
            continue

        # 벅스: 사이트 제공 변동치 우선, 없으면 state 비교
        if key in site_changes:
            lines.append(f"•{label} {curr}{site_delta_to_text(site_changes.get(key))}")
        else:
            lines.append(f"•{label} {curr}{delta_text(prev, curr)}")

    lines.append("")
    lines.append(f"🎬 {format_views(views)}")
    return "\n".join(lines)

# ===================== 실행(한 번) =====================
def run_once():
    now = datetime.now(KST)
    print(f"[DEBUG] 실행 시각: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    state = load_state()

    ranks: dict[str, int | None] = {}
    site_changes: dict[str, int] = {}
    title, artist = TARGET_TITLE, TARGET_ARTIST

    # 멜론 TOP100
    try:
        rank, change_val = fetch_melon_top100(title, artist)
        ranks["melon_top100"] = rank
        if change_val is not None:
            site_changes["melon_top100"] = change_val
    except Exception as e:
        print("melon_top100 error:", e)
        ranks["melon_top100"] = None

    # 멜론 HOT100
    try:
        rank, change_val = fetch_melon_hot100(title, artist)
        ranks["melon_hot100"] = rank
        if change_val is not None:
            site_changes["melon_hot100"] = change_val
    except Exception as e:
        print("melon_hot100 error:", e)
        ranks["melon_hot100"] = None

    # 지니
    try:
        genie_rank, genie_change_sign, genie_change_abs = fetch_genie_rank(title, artist)
        ranks["genie"] = genie_rank
        if genie_change_sign is not None and genie_change_abs is not None:
            site_changes["genie"] = genie_change_sign * genie_change_abs
    except Exception as e:
        print("genie error:", e)
        ranks["genie"] = None

    # 벅스
    try:
        bugs_rank, bugs_change_sign, bugs_change_abs = fetch_bugs_rank(title, artist)
        ranks["bugs"] = bugs_rank
        if bugs_change_sign is not None and bugs_change_abs is not None:
            site_changes["bugs"] = bugs_change_sign * bugs_change_abs
    except Exception as e:
        print("bugs error:", e)
        ranks["bugs"] = None

    # FLO
    try:
        flo_rank, flo_change_sign, flo_change_abs = fetch_flo_rank(title, artist)
        ranks["flo"] = flo_rank
        if flo_change_sign is not None and flo_change_abs is not None:
            site_changes["flo"] = flo_change_sign * flo_change_abs
    except Exception as e:
        print("flo error:", e)
        ranks["flo"] = None

    # VIBE
    try:
        vibe_rank, vibe_change_sign, vibe_change_abs = fetch_vibe_rank(title, artist)
        ranks["vibe"] = vibe_rank
        if vibe_change_sign is not None and vibe_change_abs is not None:
            site_changes["vibe"] = vibe_change_sign * vibe_change_abs
    except Exception as e:
        print("vibe error:", e)
        ranks["vibe"] = None

    # YouTube 조회수
    views = fetch_youtube_views()

    text = build_text(now, ranks, views, state, site_changes=site_changes)
    print("----- Tweet body -----\n" + text + "\n----------------------")

    code = tweet(text)
    if 200 <= code < 300:
        state.setdefault("ranks", {})
        for _, key in SITES:
            state["ranks"][key] = as_int(ranks.get(key))
        state["youtube_views"] = views
        state["last_posted_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)


# ===================== 스케줄러(매시 정각) =====================
def main():
    sched = BlockingScheduler(timezone="Asia/Seoul")
    # 매시 정각
    sched.add_job(run_once, CronTrigger(minute=0, timezone="Asia/Seoul"))
    print("Scheduler started. (KST 매시 정각 자동 트윗)")
    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        pass
def lambda_handler(event=None, context=None):
    run_once()
    return {"statusCode": 200, "body": "Tweet posted"}

if __name__ == "__main__":
    import sys
    if "--once" in sys.argv:
        run_once()
    else:
        main()
 