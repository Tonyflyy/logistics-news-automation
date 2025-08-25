# config.py

import os
from dotenv import load_dotenv

# .env 파일에서 환경 변수 로드
load_dotenv()

class Config:
    """설정 값들을 관리하는 클래스"""
    
    # API 키 및 수신자 목록 (환경 변수에서 로드)
    GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
    RECIPIENT_LIST = [email.strip() for email in os.getenv('RECIPIENT_LIST', '').split(',')]
    SENDER_EMAIL = "zzzfbwnsgh@gmail.com" # 실제 발신자 이메일로 변경 필요

    # 파일 경로
    SENT_LINKS_FILE = 'sent_links_logistics.txt'
    TOKEN_FILE = 'token.json'
    CREDENTIALS_FILE = 'credentials.json'

    # 이미지 스크래핑 설정
    MIN_IMAGE_WIDTH = 300
    MIN_IMAGE_HEIGHT = 150
    DEFAULT_IMAGE_URL = 'https://via.placeholder.com/600x300.png?text=News' # 기본 이미지 URL
    
    # 뉴스 수집 기간
    NEWS_FETCH_HOURS = 48
    MAX_ARTICLES = 100

    RSS_FEEDS = [
        # --- 물류/공급망 전문 RSS (글로벌) ---
        "https://www.freightwaves.com/rss",                     # FreightWaves (화물/물류 뉴스)
        "https://www.supplychaindive.com/rss",                # Supply Chain Dive (공급망/물류)
        "https://feeds.feedburner.com/logisticsmgmt/latest",    # Logistics Management (물류 관리)
        "https://www.ttnews.com/rss",                           # Transport Topics (운송/화물)
        "https://www.dcvelocity.com/rss",                       # DC Velocity (물류/창고)
        "https://www.supplychainbrain.com/rss/feeds/all",       # SupplyChainBrain (공급망)
        "https://maritime-executive.com/rss",                   # Maritime Executive (해상 운송/화물)

        # --- 🆕 추가: 국내 물류 전문 RSS ---
        "https://www.klnews.co.kr/rss/S1N1.xml",                # 물류신문 (국내 물류 소식)
        
        # --- 주요 국내/외신 RSS (한국 관련 및 경제/IT) ---
        "https://www.zdnet.co.kr/rss/all.xml",                  # ZDNet Korea (IT 기술)
        "https://www.etnews.com/rss/all.xml",                   # 전자신문 (IT/기술)
        "http://rss.edaily.co.kr/edaily_news.xml",              # 이데일리 (경제)
        "https://www.hankyung.com/feed/it",                      # 한국경제 (IT)
        "https://www.kedglobal.com/newsRss",                    # 코리아 경제일보 (영문)
        "http://www.businesskorea.co.kr/rss/allEngArticle.xml", # 비즈니스코리아 (영문)
        "https://en.yna.co.kr/rss/",                            # 연합뉴스 (영문)
        
        # --- 🆕 추가: 주요 글로벌 경제/기술 미디어 ---
        "https://feeds.reuters.com/reuters/businessNews",       # Reuters (비즈니스)
        "https://feeds.bloomberg.com/technology/news.rss",      # Bloomberg (기술)
        "https://www.wsj.com/xml/rss/3_7455.xml",                # Wall Street Journal (기술)
        
        # --- 특정 키워드 모니터링 RSS ---
        "https://world.einnews.com/rss/south-korea-logistics",  # EIN News - South Korea Logistics
        "https://shipping.einnews.com/rss/shipping-logistics",  # EIN News - Shipping & Logistics
    ]

    # 검색 키워드
    KEYWORDS = [
        '화물', '물류', '티맵', '티맵화물', '화물운송', '육상운송', 
        '화물차', '트럭', '화물정보망', '택배차', '화물차 운전',
        '물류 스타트업', '화물 플랫폼', '내륙 운송', '도로 화물',
        '스마트물류', '물류센터', '풀필먼트', '콜드체인', '라스트마일',
        '운송', '배송', '택배', '물류산업', '운수', '배달', '물류창고', 
        '화물운영', '화물운송플랫폼', '물류기술', '물류혁신', '운송기술', 
        '배달서비스', '물류자동화'
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

    # AI 모델 설정
    GEMINI_MODEL = 'gemini-1.5-flash-latest' # 혹은 gemini-1.5-flash 등 필요에 따라 변경

    AD_DOMAINS_BLACKLIST = [
        'contentsfeed.com',
        'googleadservices.com',
        'doubleclick.net',

    ]
