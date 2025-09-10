# config.py

import os
from dotenv import load_dotenv

# .env 파일에서 환경 변수 로드
load_dotenv()

class Config:
    """설정 값들을 관리하는 클래스"""
    
    # API 키 및 수신자 목록 (환경 변수에서 로드)
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    OPINET_API_KEY = os.getenv('OPINET_API_KEY')
    GPT_MODEL='gpt-5'
    RECIPIENT_LIST = [email.strip() for email in os.getenv('RECIPIENT_LIST', '').split(',')]
    SENDER_EMAIL = "zzzfbwnsgh@gmail.com" # 실제 발신자 이메일로 변경 필요
    SENDER_NAME = "YLP 뉴스레터"
    

    # 파일 경로
    SENT_LINKS_FILE = 'sent_links_logistics.txt'
    TOKEN_FILE = 'token.json'
    CREDENTIALS_FILE = 'credentials.json'

    # 이미지 스크래핑 설정
    MIN_IMAGE_WIDTH = 300
    MIN_IMAGE_HEIGHT = 150
    DEFAULT_IMAGE_URL = 'https://via.placeholder.com/600x300.png?text=News' # 기본 이미지 URL
    
    # 뉴스 수집 기간
    NEWS_FETCH_HOURS = 24
    MAX_ARTICLES = 500

    # 검색 키워드
    KEYWORD_GROUPS = [
        ['물류', '화물', '운송', '배송', '물류산업'],
        ['육상운송', '화물차', '트럭', '물류센터', '물류창고'],
        ['풀필먼트', '콜드체인', '라스트마일', '미들마일'],
        ['공급망', 'SCM', '이커머스 물류', '3PL', '4PL', '디지털 물류'],
        ['스마트물류', '물류자동화', '물류기술', '물류 로봇'],
        ['화물 주선', '운송 중개', '화물운송주선업', '화물정보망', '화물 플랫폼', '화물운송플랫폼'],
        ['CJ대한통운', '쿠팡로지스틱스', '한진택배', '롯데글로벌로지스', '국토교통부 물류'],
        ['티맵화물', '센디', '로지스퀘어', '고고엑스', '원콜', '카고링크']
    ]

    UNWANTED_IMAGE_PATTERNS = [
        'logo', 'icon', 'favicon', 'ad', 'banner', 'btn', 'button', 'spinner', 'loading',
        'gravatar.com', 'googleusercontent.com/profile', '1x1.gif', 'spacer.gif'
    ]
    
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:129.0) Gecko/20100101 Firefox/129.0"
    ]

    AREA_CODE_MAP = {
        '01': '서울', '02': '부산', '03': '대구', '04': '인천',
        '05': '광주', '06': '대전', '07': '울산', '08': '경기',
    }

    TARGET_AREA_CODES = ['01', '02', '03', '04', '05', '06', '07', '08']

    # AI 모델 설정
    GEMINI_MODEL = 'gemini-1.5-flash-latest' # 혹은 gemini-1.5-flash 등 필요에 따라 변경

    AD_DOMAINS_BLACKLIST = [
        'contentsfeed.com',
        'googleadservices.com',
        'doubleclick.net',''
        'msn.com',
        'nate.com',
        'zum.com'
    ]

    # 날씨 API 설정
    WEATHER_API_KEY = os.getenv('WEATHER_API_KEY') 

    # 주요 물류 거점 좌표 (단기예보용 nx, ny / 중기예보용 regId)
    LOGISTICS_HUBS = {
        "수도권": {"nx": 60, "ny": 127, "regId_temp": "11B10101", "regId_land": "11B00000"},
        "영남권": {"nx": 98, "ny": 76,  "regId_temp": "11H20201", "regId_land": "11H20000"},
        "호남권": {"nx": 58, "ny": 74,  "regId_temp": "11F20501", "regId_land": "11F20000"},
        "강원권": {"nx": 92, "ny": 131, "regId_temp": "11D20501", "regId_land": "11D20000"}
    }

    # 분석 대상 국가 코드 (Python 'holidays' 라이브러리 기준)
    RISK_BRIEFING_TARGET_COUNTRIES = ['KR', 'CN', 'US', 'VN', 'DE'] # 한국, 중국, 미국, 베트남, 독일

     # 수동으로 관리할 주요 물류 이벤트 목록
    MANUAL_LOGISTICS_EVENTS = [
        # --- 기존 이벤트 ---
        {
            "name": "중국 광군제 (双十一)", "country_code": "CN", "month": 11, "day": 11,
            "risk_level": "높음", "impact_summary": "중국발 항공/해상 화물 수요가 폭증하며, 배송 지연 및 운임 급등이 예상됩니다."
        },
        {
            "name": "미국 블랙프라이데이", "country_code": "US", "month": 11, "day_of_week": 4, "week_of_month": 4,
            "risk_level": "높음", "impact_summary": "미국행 항공 화물 수요가 폭증하고 현지 내륙 운송의 병목 현상이 발생할 수 있습니다."
        },
        {
            "name": "미국 사이버먼데이", "country_code": "US", "month": 11, "day_of_week": 0, "week_after_thanksgiving": 1,
            "risk_level": "높음", "impact_summary": "블랙프라이데이와 이어지는 온라인 쇼핑 이벤트로 항공 화물 수요 증가세가 지속됩니다."
        },

        {
            "name": "중국 춘절 연휴 시작", "country_code": "CN", "month": 1, "day": 29, # 2025년 기준, 매년 변동
            "risk_level": "높음", "impact_summary": "중국 대부분의 공장과 물류 시스템이 약 2주간 멈추므로, 연휴 전 심각한 선적 지연이 발생합니다."
        },
        {
            "name": "아마존 프라임데이", "country_code": "US", "month": 7, "day": 8, # 매년 아마존이 발표, 보통 7월 중
            "risk_level": "높음", "impact_summary": "단기간에 이커머스 물량이 폭증하여 글로벌 항공 화물 운임에 영향을 미칩니다."
        },
        {
            "name": "중국 618 쇼핑 페스티벌", "country_code": "CN", "month": 6, "day": 18,
            "risk_level": "중간", "impact_summary": "중국 내수 물동량이 급증하며, 일부 항공/해상 운송에도 영향을 미칩니다."
        },
        {
            "name": "박싱데이", "country_code": "DE", "month": 12, "day": 26, # 독일을 유럽 대표로 설정
            "risk_level": "낮음", "impact_summary": "유럽 내 소매 물류 및 반품 물류량이 일시적으로 증가할 수 있습니다."
        }
    ]

    HOLIDAY_NAME_TRANSLATIONS = {
        # 미국 (US)
        "US:New Year's Day": "새해 첫날",
        "US:Martin Luther King, Jr. Day": "마틴 루터 킹 주니어의 날",
        "US:Washington's Birthday": "워싱턴 탄생일",
        "US:Memorial Day": "메모리얼 데이",
        "US:Juneteenth National Independence Day": "준틴스 독립기념일",
        "US:Independence Day": "독립기념일",
        "US:Labor Day": "노동절",
        "US:Columbus Day": "콜럼버스의 날",
        "US:Veterans Day": "재향군인의 날",
        "US:Thanksgiving": "추수감사절",
        "US:Christmas Day": "크리스마스",
        
        # 중국 (CN)
        "CN:New Year's Day": "새해 첫날",
        "CN:Chinese New Year's Eve": "춘절 이브",
        "CN:Chinese New Year": "춘절",
        "CN:Lantern Festival": "원소절",
        "CN:Qingming Festival": "청명절",
        "CN:Labour Day": "노동절",
        "CN:Dragon Boat Festival": "단오절",
        "CN:Mid-Autumn Festival": "중추절",
        "CN:National Day": "국경절",
        "CN:Day off (substituted from Sunday)": "대체 휴일",
        "CN:Day off (substituted from Saturday)": "대체 휴일",
        
        # 베트남 (VN)
        "VN:New Year's Day": "새해 첫날",
        "VN:Vietnamese New Year's Eve": "뗏 이브",
        "VN:Vietnamese New Year": "뗏 (설날)",
        "VN:Hung Kings' Festival": "흥왕 기념일",
        "VN:Reunification Day": "남부 해방 기념일",
        "VN:International Workers' Day": "국제 노동절",
        "VN:National Day": "독립기념일",

        # 독일 (DE)
        "DE:New Year's Day": "새해 첫날",
        "DE:Good Friday": "성금요일",
        "DE:Easter Monday": "이스터 먼데이",
        "DE:Labour Day": "노동절",
        "DE:Ascension Day": "주님 승천 대축일",
        "DE:Whit Monday": "성령 강림 대축일 월요일",
        "DE:Day of German Unity": "독일 통일의 날",
        "DE:Christmas Day": "크리스마스",
        "DE:Second Day of Christmas": "크리스마스 연휴",
    }


