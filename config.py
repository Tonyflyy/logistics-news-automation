# config.py

import os
from dotenv import load_dotenv

# .env 파일에서 환경 변수 로드
load_dotenv()

class Config:
    """설정 값들을 관리하는 클래스"""
    

    # 'daily' 또는 'weekly'로 실행 모드를 선택
    EXECUTION_MODE = 'daily'

    # API 키 및 수신자 목록 (환경 변수에서 로드)
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
    OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
    OPINET_API_KEY = os.getenv('OPINET_API_KEY')
    GPT_MODEL='gpt-5'
    
    DAILY_RECIPIENT_LIST = [email.strip() for email in os.getenv('DAILY_RECIPIENT_LIST', '').split(',')]
    WEEKLY_RECIPIENT_LIST = [email.strip() for email in os.getenv('WEEKLY_RECIPIENT_LIST', '').split(',')]
    SENDER_EMAIL = "zzzfbwnsgh@gmail.com" # 실제 발신자 이메일로 변경 필요
    SENDER_NAME = "YLP 뉴스레터"
    

    # 파일 경로
    SENT_LINKS_FILE = 'sent_links_logistics.txt'
    TOKEN_FILE = 'token.json'
    CREDENTIALS_FILE = 'credentials.json'
    WEEKLY_CANDIDATES_FILE = 'weekly_candidates.json'

    # --- 스크래핑 설정 ---
    MIN_IMAGE_WIDTH = 300
    MIN_IMAGE_HEIGHT = 150
    DEFAULT_IMAGE_URL = 'https://via.placeholder.com/600x300.png?text=News'
    MAX_ARTICLES_TO_PROCESS = 300 # 수집할 최대 기사 수
    
     # ✨ [분리] 뉴스 수집 기간 설정
    NEWS_FETCH_HOURS_DAILY = 24
    NEWS_FETCH_HOURS_WEEKLY = 168 # 7일

    # ✨ [분리] AI가 최종 선택할 기사 수 설정
    SELECT_NEWS_COUNT_DAILY = 10
    SELECT_NEWS_COUNT_WEEKLY = 15

    # 검색 키워드
    KEYWORD_GROUPS_DAILY  = [
        ['물류', '화물', '운송', '배송', '물류산업'],
        ['육상운송', '화물차', '트럭', '물류센터', '물류창고'],
        ['풀필먼트', '콜드체인', '라스트마일', '미들마일'],
        ['공급망', 'SCM', '이커머스 물류', '3PL', '4PL', '디지털 물류'],
        ['스마트물류', '물류자동화', '물류기술', '물류 로봇'],
        ['화물 주선', '운송 중개', '화물운송주선업', '화물정보망', '화물 플랫폼', '화물운송플랫폼'],
        ['CJ대한통운', '쿠팡로지스틱스', '한진택배', '롯데글로벌로지스', '국토교통부 물류'],
        ['티맵화물', '센디', '로지스퀘어', '고고엑스', '원콜', '카고링크']
    ]

    # ✨ [분리] 주간용 키워드 그룹
    KEYWORD_GROUPS_WEEKLY = [
        ['물류산업 동향', '공급망 관리', 'SCM', '물류 M&A', '이커머스 물류'],
        ['육상운송', '화물차 파업', '물류센터', '철도운송', '콜드체인'],
        ['해상운송', '항공운송', '컨테이너선', '항공화물', '인천항', '부산항', 'SCFI', '해상운임'],
        ['CJ대한통운', '쿠팡 로지스틱스', '한진', '롯데글로벌로지스', 'HMM', '국토교통부', '물류 정책', '유가', '관세'],
        ['스마트물류', '물류자동화', '물류 로봇', '디지털 물류', 'AI 물류', 'ESG 물류'],
        ['화물운송플랫폼', '디지털 포워딩', '티맵화물', '센디', '로지스퀘어', '화물정보망']
    ]

    UNWANTED_IMAGE_PATTERNS = [
        'logo', 'icon', 'favicon', 'ad', 'banner', 'btn', 'button', 'spinner', 'loading',
        'gravatar.com', 'googleusercontent.com/profile', '1x1.gif', 'spacer.gif'
    ]

    UNWANTED_URL_PATTERNS = [
    '/admin/', '/login', 'LoginForm.html', '/join/', '/member/', 
    '/v.daum.net/v/', 
    'cooper=RSS',       
    'articleList.html', 
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
        'doubleclick.net',
        'msn.com',
        'nate.com',
        'zum.com',
        'ads.mtgroup.kr'
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
    #RISK_BRIEFING_TARGET_COUNTRIES = ['KR', 'CN', 'US', 'VN', 'DE'] # 한국, 중국, 미국, 베트남, 독일
    RISK_BRIEFING_TARGET_COUNTRIES = ['KR'] 



