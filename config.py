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
    MIN_IMAGE_WIDTH = 500
    MIN_IMAGE_HEIGHT = 250
    DEFAULT_IMAGE_URL = 'https://via.placeholder.com/600x300.png?text=News' # 기본 이미지 URL
    
    # 뉴스 수집 기간
    NEWS_FETCH_HOURS = 48
    MAX_ARTICLES = 100

    RSS_FEEDS = [
    # [물류/IT/경제]
    "https://www.klnews.co.kr/rss/S1N1.xml",      # 물류신문 (전체 기사)
    "https://www.etnews.com/rss/all.xml",         # 전자신문 (전체 기사)
    "https://www.ddaily.co.kr/rss/all.xml",      # 디지털데일리 (전체 기사)
    "https://www.hankyung.com/feed/it",           # 한국경제 (IT 섹션)
    "https://www.mk.co.kr/rss/30100041/",         # 매일경제 (산업 섹션)
    "http://rss.edaily.co.kr/edaily_news.xml",   # 이데일리 (주요 뉴스)

    # [종합]
    "https://www.yonhapnewstv.co.kr/browse/feed/",# 연합뉴스TV (전체 기사)
    "https://fs.jtbc.co.kr/RSS/newsflash.xml",   # JTBC (속보)
    "https://rss.donga.com/total.xml",           # 동아일보 (전체 기사)
        ]

    # 검색 키워드
    KEYWORDS = [
        # 한글 키워드
        '화물', '물류', '운송', '배송',  '정보망','주선사','화주',
        '물류센터', '화물차', 
        '물류 스타트업', '화물 플랫폼'
        
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

    DEFAULT_IMAGE_URL = "https://via.placeholder.com/600x300.png?text=News"






