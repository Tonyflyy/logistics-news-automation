# news_collector.py

import os
import base64
import markdown
import json
import time
import random
from datetime import datetime, timezone, timedelta, date
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from urllib.parse import urljoin, urlparse
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from newspaper import Article
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter
# 서드파티 라이브러리
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader
from PIL import Image
from pygooglenews import GoogleNews
from zoneinfo import ZoneInfo

# ⬇️⬇️⬇️ Selenium의 '지능적 기다림' 기능을 위한 임포트 추가 ⬇️⬇️⬇️
from selenium import webdriver
from selenium.webdriver.chrome.service import Service as ChromeService
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.chrome.options import Options
from selenium_stealth import stealth
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# 구글 인증 관련
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import google.generativeai as genai
import openai

from config import Config

# --- 유틸리티 함수 ---
def get_kst_today_str():
    return datetime.now(ZoneInfo('Asia/Seoul')).strftime("%Y-%m-%d")

def markdown_to_html(text):
    return markdown.markdown(text) if text else ""


def create_price_trend_chart(seven_day_data, filename="price_chart.png"):
    """최근 7일간의 유가 데이터로 차트 이미지를 생성하고 파일 경로를 반환합니다."""
    try:
        # 1. 한글 폰트 설정 (맑은 고딕)
        plt.rcParams['font.family'] = 'Malgun Gothic'
        plt.rcParams['axes.unicode_minus'] = False # 마이너스 폰트 깨짐 방지

        # 2. 데이터 분리 및 준비
        dates = [d['DATE'][-4:-2] + "/" + d['DATE'][-2:] for d in seven_day_data['gasoline']]
        gasoline_prices = [float(p['PRICE']) for p in seven_day_data['gasoline']]
        diesel_prices = [float(p['PRICE']) for p in seven_day_data['diesel']]

        # 3. 차트 생성
        fig, ax = plt.subplots(figsize=(7, 4)) # 차트 크기 조절
        
        ax.plot(dates, gasoline_prices, 'o-', label='휘발유', color='#3498db')
        ax.plot(dates, diesel_prices, 'o-', label='경유', color='#e74c3c')
        
        # 4. 차트 꾸미기
        ax.set_title("최근 7일 휘발유·경유 가격 추이", fontsize=15, pad=20)
        ax.legend()
        ax.grid(True, which='both', linestyle='--', linewidth=0.5)
        
        # Y축 단위를 '1,700원' 형식으로 변경
        formatter = FuncFormatter(lambda y, _: f'{int(y):,}원')
        ax.yaxis.set_major_formatter(formatter)
        
        ax.tick_params(axis='x', rotation=0)
        fig.tight_layout()

        # 5. 이미지 파일로 저장
        plt.savefig(filename, dpi=150)
        plt.close(fig) # 메모리 해제
        
        print(f"✅ 유가 추이 차트 이미지 '{filename}'를 생성했습니다.")
        return filename
    except Exception as e:
        print(f"❌ 차트 이미지 생성 실패: {e}")
        return None
    
def get_cheapest_stations(config, count=20):
    """오피넷 API로 전국 최저가 경유 주유소 정보를 가져옵니다."""
    if not config.OPINET_API_KEY:
        return []

    # API 파라미터 설정: prodcd=D047 (경유), cnt=가져올 개수
    url = f"http://www.opinet.co.kr/api/lowTop10.do?out=json&code={config.OPINET_API_KEY}&prodcd=D047&cnt={count}"
    
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()['RESULT']['OIL']
        
        cheapest_stations = []
        for station in data:
            # 주소에서 '시/도'와 '시/군/구' 정보만 간추리기
            address_parts = station.get('VAN_ADR', '').split(' ')
            location = " ".join(address_parts[:2]) if len(address_parts) >= 2 else address_parts[0]
            
            cheapest_stations.append({
                "name": station.get('OS_NM'),
                "price": f"{int(station.get('PRICE', 0)):,}원",
                "location": location
            })
        
        print(f"✅ 전국 최저가 주유소 Top {len(cheapest_stations)} 정보를 가져왔습니다.")
        return cheapest_stations

    except Exception as e:
        print(f"❌ 최저가 주유소 정보 조회 실패: {e}")
        return []
    
def get_price_indicators(config):
    """오피넷 API를 사용하여 주요 도시별 유가, 요소수 가격, 추세, 최저가 주유소 정보를 가져와 하나의 객체로 반환합니다."""
    if not config.OPINET_API_KEY:
        print("⚠️ 오피넷 API 키가 설정되지 않았습니다.")
        return {}

    # 최종 데이터를 담을 기본 구조 정의
    indicator_data = {
        "timestamp": datetime.now(ZoneInfo('Asia/Seoul')).strftime('%Y-%m-%d %H:%M 기준'),
        "city_prices": [],
        "trend_comment": "",
        "seven_day_data": {},
        "cheapest_stations": []
    }
    
    # --- 1. 주요 도시별 휘발유/경유 가격 가져오기 (API 호출 1회) ---
    city_data_map = {code: {"name": name} for code, name in config.AREA_CODE_MAP.items() if code in config.TARGET_AREA_CODES}
    try:
        sido_price_url = f"http://www.opinet.co.kr/api/avgSidoPrice.do?out=json&code={config.OPINET_API_KEY}"
        response = requests.get(sido_price_url, timeout=5)
        response.raise_for_status()
        sido_data = response.json()['RESULT']['OIL']
        for oil in sido_data:
            area_code = oil.get('SIDOCD')
            if area_code in config.TARGET_AREA_CODES:
                prod_code = oil.get('PRODCD')
                price = f"{float(oil['PRICE']):,.0f}원"
                if prod_code == 'B027': # 보통휘발유
                    city_data_map[area_code]['gasoline'] = price
                elif prod_code == 'D047': # 자동차용경유
                    city_data_map[area_code]['diesel'] = price
        print("✅ 주요 도시별 유가 정보를 가져왔습니다.")
    except Exception as e:
        print(f"❌ 시도별 유가 정보 조회 실패: {e}")

    # --- 2. 주요 도시별 요소수 평균 가격 가져오기 (도시별 API 호출) ---
    print("-> 주요 도시별 요소수 가격 정보를 조회합니다...")
    for area_code in config.TARGET_AREA_CODES:
        urea_url = f"http://www.opinet.co.kr/api/ureaPrice.do?out=json&code={config.OPINET_API_KEY}&area={area_code}"
        try:
            response = requests.get(urea_url, timeout=5)
            response.raise_for_status()
            urea_data = json.loads(response.text, strict=False)['RESULT']['OIL']
            total_price, stock_count = 0, 0
            for station in urea_data:
                stock_yn = station.get('STOCK_YN', '').strip()
                price_str = station.get('PRICE', '').strip()
                if stock_yn == 'Y' and price_str:
                    total_price += int(price_str)
                    stock_count += 1
            if stock_count > 0:
                avg_price = total_price / stock_count
                city_data_map[area_code]['urea'] = f"{avg_price:,.0f}원/L"
            time.sleep(0.5)
        except Exception as e:
            area_name = config.AREA_CODE_MAP.get(area_code, "알 수 없는 지역")
            print(f"❌ {area_name} 요소수 가격 조회 실패: {e}")
            continue
    print("✅ 주요 도시별 요소수 가격 정보를 가져왔습니다.")

    # --- 3. 전국 가격 추세 및 차트용 데이터 가져오기 (API 호출 1회) ---
    try:
        trend_url = f"http://www.opinet.co.kr/api/avgRecentPrice.do?out=json&code={config.OPINET_API_KEY}"
        response = requests.get(trend_url, timeout=5)
        response.raise_for_status()
        trend_data = response.json()['RESULT']['OIL']
        
        # 차트용 7일 데이터 준비
        gasoline_7day = sorted([p for p in trend_data if p['PRODCD'] == 'B027'], key=lambda x: x['DATE'])
        diesel_7day = sorted([p for p in trend_data if p['PRODCD'] == 'D047'], key=lambda x: x['DATE'])
        if gasoline_7day and diesel_7day:
            indicator_data["seven_day_data"] = {"gasoline": gasoline_7day, "diesel": diesel_7day}
            print("✅ 차트용 7일 유가 데이터를 준비했습니다.")

        # 경유 가격 추세 분석
        if len(diesel_7day) >= 2:
            today_price = float(diesel_7day[-1]['PRICE'])
            yesterday_price = float(diesel_7day[-2]['PRICE'])
            trend_comment = ""
            if today_price > yesterday_price: trend_comment += "어제보다 소폭 상승했습니다."
            elif today_price < yesterday_price: trend_comment += "어제보다 소폭 하락했습니다."
            else: trend_comment += "어제와 가격이 동일합니다."
            
            if len(diesel_7day) >= 7:
                week_ago_price = float(diesel_7day[0]['PRICE'])
                if today_price > week_ago_price: trend_comment += " 주간 단위로는 상승세입니다."
                elif today_price < week_ago_price: trend_comment += " 주간 단위로는 하락세입니다."
            
            indicator_data["trend_comment"] = f"전국 경유 가격은 {trend_comment}"
            print("✅ 전국 유가 추세 정보를 가져왔습니다.")
    except Exception as e:
        print(f"❌ 유가 추세 정보 조회 실패: {e}")

    # --- 4. 전국 최저가 주유소 정보 가져오기 ---
    indicator_data["cheapest_stations"] = get_cheapest_stations(config, count=20)

    # --- 최종 데이터 구조 정리 ---
    indicator_data["city_prices"] = list(city_data_map.values())
    return indicator_data
    

class NewsScraper:
    def __init__(self, config):
        self.config = config
        self.session = self._create_session()

    def _create_session(self):
        session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session

    def get_image_url(self, article_url: str) -> str:
        try:
            headers = { "User-Agent": random.choice(self.config.USER_AGENTS) }
            response = self.session.get(article_url, headers=headers, timeout=10, allow_redirects=True)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")
            
            # 1. 메타 태그 (가장 신뢰도 높음)
            meta_image = soup.find("meta", property="og:image") or soup.find("meta", name="twitter:image")
            if meta_image and meta_image.get("content"):
                meta_url = self._resolve_url(article_url, meta_image["content"])
                if self._is_valid_candidate(meta_url) and self._validate_image(meta_url):
                    return meta_url

            # 2. 본문 <figure> 또는 <picture> 태그
            for tag in soup.select('figure > img, picture > img', limit=5):
                img_url = tag.get('src') or tag.get('data-src') or (tag.get('srcset').split(',')[0].strip().split(' ')[0] if tag.get('srcset') else None)
                if img_url and self._is_valid_candidate(img_url):
                    full_url = self._resolve_url(article_url, img_url)
                    if self._validate_image(full_url):
                        return full_url
            
            # 2.5. 기사 본문 영역(entry-content, article-body 등)을 특정하여 이미지 검색
            content_area = soup.select_one('.entry-content, .article-body, #article-view-content')
            if content_area:
                for img in content_area.find_all("img", limit=5):
                    img_url = img.get("src") or img.get("data-src")
                    if img_url and self._is_valid_candidate(img_url):
                        full_url = self._resolve_url(article_url, img_url)
                        if self._validate_image(full_url):
                            return full_url
            # --- ⬆️⬆️⬆️ 수정 완료 ⬆️⬆️⬆️
            
            # 3. 일반 <img> 태그 (최후의 수단)
            for img in soup.find_all("img", limit=10):
                img_url = img.get("src") or img.get("data-src")
                if img_url and self._is_valid_candidate(img_url):
                    full_url = self._resolve_url(article_url, img_url)
                    if self._validate_image(full_url):
                        return full_url

            return self.config.DEFAULT_IMAGE_URL
        except Exception:
            return self.config.DEFAULT_IMAGE_URL

    def _resolve_url(self, base_url, image_url):
        if image_url.startswith('//'): return 'https:' + image_url
        return urljoin(base_url, image_url)

    def _is_valid_candidate(self, image_url):
        if 'news.google.com' in image_url or 'lh3.googleusercontent.com' in image_url: return False
        return not any(pattern in image_url.lower() for pattern in self.config.UNWANTED_IMAGE_PATTERNS)

    def _validate_image(self, image_url):
        try:
            response = self.session.get(image_url, stream=True, timeout=5)
            response.raise_for_status()
            content_type = response.headers.get('Content-Type', '').lower()
            if 'image' not in content_type: return False
            img_data = BytesIO(response.content)
            with Image.open(img_data) as img:
                width, height = img.size
                if width < self.config.MIN_IMAGE_WIDTH or height < self.config.MIN_IMAGE_HEIGHT: return False
                aspect_ratio = width / height
                if aspect_ratio > 4.0 or aspect_ratio < 0.25: return False
                

                return True
        except Exception:
            return False

class AIService:
    def generate_single_summary(self, article_title: str, article_link: str) -> str | None:
        """기사 제목과 원문을 바탕으로 3줄 요약을 생성합니다."""
        try:
            article = Article(article_link)
            article.download()
            article.parse()
            
            if len(article.text) < 100:
                return "요약 정보를 생성할 수 없습니다."
            
            system_prompt = "당신은 핵심만 간결하게 전달하는 뉴스 에디터입니다. 모든 답변은 한국어로 해야 합니다."
            user_prompt = f"""
            아래 제목과 본문을 가진 뉴스 기사의 내용을 독자들이 이해하기 쉽게 3줄로 요약해주세요.
            
            [제목]: {article_title}
            [본문]:
            {article.text[:2000]}
            """
            
            summary = self._generate_content_with_retry(system_prompt, user_prompt)
            return summary

        except Exception as e:
            print(f"  ㄴ> ❌ AI 요약 생성 실패: {e.__class__.__name__}")
            return None
    # (변경 없음)
    def __init__(self, config):
        self.config = config
        if not self.config.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY 환경 변수가 설정되지 않았습니다.")
        # OpenAI 클라이언트 초기화
        self.client = openai.OpenAI(api_key=self.config.OPENAI_API_KEY)

    def _generate_content_with_retry(self, system_prompt: str, user_prompt: str, is_json: bool = False):
        """
        OpenAI API를 호출하여 콘텐츠를 생성합니다. 실패 시 재시도합니다.
        - system_prompt: AI의 역할과 지침을 정의합니다.
        - user_prompt: AI에게 전달할 실제 요청 내용입니다.
        - is_json: JSON 형식으로 응답을 요청할지 여부를 결정합니다.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        
        # JSON 모드 요청 시 추가 옵션 설정
        request_options = {"model": self.config.GPT_MODEL, "messages": messages}
        if is_json:
            request_options["response_format"] = {"type": "json_object"}

        for attempt in range(3):
            try:
                response = self.client.chat.completions.create(**request_options)
                content = response.choices[0].message.content
                
                # JSON 모드일 경우, 응답이 유효한 JSON인지 한 번 더 확인
                if is_json:
                    json.loads(content) # 파싱에 실패하면 예외 발생
                
                return content
            
            except Exception as e:
                print(f"❌ OpenAI API 호출 실패 (시도 {attempt + 1}/3): {e}")
                time.sleep(2 ** attempt) # 재시도 전 대기 시간 증가
        return None

    def select_top_news(self, news_list, previous_news_list):
        """
        뉴스 목록에서 중복을 제거하고 가장 중요한 Top 10 뉴스를 선정합니다.
        - news_list: 오늘의 후보 뉴스 목록
        - previous_news_list: 어제 발송했던 뉴스 목록
        """
        print(f"AI 뉴스 선별 시작... (대상: {len(news_list)}개)")

        # (추가) 어제 뉴스 목록을 AI에게 전달할 형식으로 변환
        previous_news_context = "어제는 발송된 뉴스가 없습니다."
        if previous_news_list:
            previous_news_context = "\n\n".join(
                [f"- 제목: {news['title']}\n  요약: {news['ai_summary']}" for news in previous_news_list]
            )

        # 오늘의 후보 뉴스 목록을 형식에 맞게 변환
        today_candidates_context = "\n\n".join(
            [f"기사 #{i}\n제목: {news['title']}\n요약: {news['ai_summary']}" for i, news in enumerate(news_list)]
        )

        system_prompt = "당신은 독자에게 매일 신선하고 가치 있는 정보를 제공하는 것을 최우선으로 하는 대한민국 최고의 물류 전문 뉴스 편집장입니다. 당신의 응답은 반드시 JSON 형식이어야 합니다."
        
        # (변경) 두 가지 중복 제거 규칙이 모두 포함된 최종 프롬프트
        user_prompt = f"""
        [어제 발송된 주요 뉴스]
        {previous_news_context}

        ---

        [오늘의 후보 뉴스 목록]
        {today_candidates_context}

        ---

        [당신의 가장 중요한 임무와 규칙]
        1.  **새로운 주제 최우선**: [오늘의 후보 뉴스 목록]에서 뉴스를 선택할 때, [어제 발송된 주요 뉴스]와 **주제가 겹치지 않는 새로운 소식**을 최우선으로 선정해야 합니다.
        2.  **중요 후속 기사만 허용**: 어제 뉴스의 후속 기사는 '계획 발표'에서 '정식 계약 체결'처럼 **매우 중대한 진전이 있을 경우에만** 포함시키고, 단순 진행 상황 보도는 과감히 제외하세요.
        3.  **오늘 뉴스 내 중복 제거**: [오늘의 후보 뉴스 목록] 내에서도 동일한 사건(예: 'A사 물류센터 개장')을 다루는 기사가 여러 언론사에서 나왔다면, 가장 제목이 구체적이고 내용이 풍부한 **기사 단 하나만**을 대표로 선정해야 합니다.

        [작업 지시]
        위의 규칙들을 가장 엄격하게 준수하여, [오늘의 후보 뉴스 목록] 중에서 독자에게 가장 가치있는 최종 기사 10개의 번호(인덱스)를 선정해주세요.

        [출력 형식]
        - 반드시 'selected_indices' 키에 최종 선정한 기사 10개의 인덱스를 숫자 배열로 담은 JSON 객체로만 응답해야 합니다.
        - 예: {{"selected_indices": [3, 15, 4, 8, 22, 1, 30, 11, 19, 5]}}
        """
        
        response_text = self._generate_content_with_retry(system_prompt, user_prompt, is_json=True)
        
        if response_text:
            try:
                selected_indices = json.loads(response_text).get('selected_indices', [])
                top_news = [news_list[i] for i in selected_indices if i < len(news_list)]
                print(f"✅ AI가 {len(top_news)}개 뉴스를 선별했습니다.")
                return top_news
            except (json.JSONDecodeError, KeyError) as e:
                print(f"❌ AI 응답 파싱 실패: {e}. 상위 10개 뉴스를 임의로 선택합니다.")
        
        return news_list[:10]

    def generate_briefing(self, news_list):
        """선별된 뉴스 목록을 바탕으로 데일리 브리핑을 생성합니다."""
        print("AI 브리핑 생성 시작...")
        context = "\n\n".join([f"제목: {news['title']}\n요약: {news['ai_summary']}" for news in news_list])
        
        system_prompt = "당신은 탁월한 통찰력을 가진 IT/경제 뉴스 큐레이터입니다. Markdown 형식을 사용하여 매우 간결하고 읽기 쉬운 '데일리 브리핑'을 작성해주세요."
        user_prompt = f"""
        아래 뉴스 목록을 분석하여, 독자를 위한 '데일리 브리핑'을 작성해주세요.
        
        **출력 형식 규칙:**
        1. '에디터 브리핑'은 '## 에디터 브리핑' 헤더로 시작하며, 오늘 뉴스의 핵심을 2~3 문장으로 요약합니다.
        2. '주요 뉴스 분석'은 '## 주요 뉴스 분석' 헤더로 시작합니다.
        3. 주요 뉴스 분석에서는 가장 중요한 뉴스 카테고리 2~3개를 '###' 헤더로 구분합니다.
        4. 각 카테고리 안에서는, 관련된 여러 뉴스를 하나의 간결한 문장으로 요약하고 글머리 기호(`*`)를 사용합니다.
        5. 문장 안에서 강조하고 싶은 특정 키워드는 큰따옴표(" ")로 묶어주세요.
        
        [오늘의 뉴스 목록]
        {context}
        """
        
        briefing = self._generate_content_with_retry(system_prompt, user_prompt)
        if briefing: 
            print("✅ AI 브리핑 생성 성공!")
        return briefing


class NewsService:
    def __init__(self, config, scraper, ai_service):
        self.config = config
        self.scraper = scraper
        self.ai_service = ai_service
        self.sent_links = self._load_sent_links()

    def _create_stealth_driver(self):
        chrome_options = Options()
        chrome_options.page_load_strategy = 'eager'
        chrome_options.add_argument("--headless")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--log-level=3")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        chrome_options.add_argument(f'--user-agent={random.choice(self.config.USER_AGENTS)}')
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        
        try:
            service = ChromeService(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=chrome_options)
            stealth(driver, languages=["ko-KR", "ko"], vendor="Google Inc.", platform="Win32",
                    webgl_vendor="Intel Inc.", renderer="Intel Iris OpenGL Engine", fix_hairline=True)
            driver.set_page_load_timeout(15)
            return driver
        except Exception as e:
            print(f"🚨 드라이버 생성 실패: {e}")
            return None

    def _load_sent_links(self):
        try:
            with open(self.config.SENT_LINKS_FILE, 'r', encoding='utf-8') as f:
                return set(line.strip() for line in f)
        except FileNotFoundError:
            return set()

    def _clean_and_validate_url(self, url: str) -> str | None:
        try:
            parsed = urlparse(url)
            if any(ad_domain in parsed.netloc for ad_domain in self.config.AD_DOMAINS_BLACKLIST):
                return None
            
            if not parsed.path or len(parsed.path) <= 5:
                if not any(allowed in parsed.netloc for allowed in ['hyundai.co.kr']):
                    return None
            
            cleaned_url = parsed._replace(fragment="").geturl()
            return cleaned_url
        except Exception:
            return None
    
    def _resolve_google_news_url(self, entry):
        """Selenium을 사용해 Google News 링크에서 실제 기사 URL만 추출합니다."""
        driver = None
        try:
            driver = self._create_stealth_driver()
            if not driver: return None
            
            driver.get(entry['link'])
            wait = WebDriverWait(driver, 10)
            link_element = wait.until(EC.presence_of_element_located((By.TAG_NAME, 'a')))
            original_url = link_element.get_attribute('href')
            validated_url = self._clean_and_validate_url(original_url)
            
            if validated_url:
                print(f"  -> ✅ URL 추출 성공: {entry['title']}")
                return {'title': entry['title'], 'link': validated_url}
            return None
        except Exception as e:
            print(f"  ㄴ> ❌ URL 추출 실패: '{entry['title']}'에서 오류 발생: {e.__class__.__name__}")
            return None
        finally:
            if driver:
                driver.quit()

    def _process_article_content(self, article_info):
        """실제 URL을 받아 콘텐츠 분석, AI 요약, 이미지 스크래핑을 수행합니다."""
        title = article_info['title']
        url = article_info['link']
        
        try:
            headers = {"User-Agent": random.choice(self.config.USER_AGENTS)}
            response = self.scraper.session.get(url, headers=headers, timeout=10)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'lxml')
            
            article_text = ''
            content_area = soup.select_one('#article-view-content, .article_body, .article-body, .entry-content')
            if content_area:
                article_text = content_area.get_text(strip=True)
            else:
                article_text = ' '.join(p.get_text(strip=True) for p in soup.find_all('p'))

            if len(article_text) < 300:
                print(f"  ㄴ> 🗑️ 본문 내용이 짧아 제외: {url[:80]}...")
                return None

            ai_summary = self.ai_service.generate_single_summary(title, url)
            if not ai_summary or "요약 정보를 생성할 수 없습니다" in ai_summary:
                print(f"  ㄴ> ⚠️ AI 요약 생성 실패, 기사 제외")
                return None
            
            return {
                'title': title,
                'link': url, 'url': url,
                'ai_summary': ai_summary,
                'image_url': self.scraper.get_image_url(url)
            }
        except Exception as e:
            print(f"  ㄴ> ❌ 콘텐츠 처리 중 오류: '{title}' ({e.__class__.__name__})")
            return None

    def get_fresh_news(self):
        print("최신 뉴스 수집을 시작합니다...")
        client = GoogleNews(lang='ko', country='KR')
        
        # --- ⬇️ (변경) 그룹 검색 로직 시작 ⬇️ ---
        all_entries = []
        unique_links = set() # 링크 중복을 실시간으로 확인하기 위한 set

        # 검색할 기간 설정
        end_date = date.today()
        start_date = end_date - timedelta(hours=self.config.NEWS_FETCH_HOURS)
        
        print(f"검색 기간: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")

        # 설정된 키워드 그룹을 하나씩 순회
        for i, group in enumerate(self.config.KEYWORD_GROUPS):
            query = ' OR '.join(f'"{keyword}"' for keyword in group) # 키워드에 공백이 있어도 안전하도록 "" 처리
            query += ' -해운 -항공' # 제외 키워드 추가
            
            print(f"\n({i+1}/{len(self.config.KEYWORD_GROUPS)}) 그룹 검색 중: [{', '.join(group)}]")

            try:
                # 각 그룹별로 뉴스 검색 실행
                search_results = client.search(query, from_=start_date.strftime('%Y-%m-%d'), to_=end_date.strftime('%Y-%m-%d'))
                
                # 중복을 확인하며 결과 수집
                for entry in search_results['entries']:
                    link = entry.get('link')
                    if link and link not in unique_links:
                        all_entries.append(entry)
                        unique_links.add(link)
                
                print(f" ➡️ {len(search_results['entries'])}개 발견, 현재까지 총 {len(all_entries)}개의 고유 기사 확보")

                # IP 차단을 피하기 위해 각 요청 사이에 2초 대기
                time.sleep(2)

            except Exception as e:
                print(f" ❌ 그룹 검색 중 오류 발생: {e}")
        
        print(f"\n모든 그룹 검색 완료. 총 {len(all_entries)}개의 중복 없는 기사를 발견했습니다.")
        # --- ⬆️ (변경) 그룹 검색 로직 종료 ⬆️ ---

        # 시간 필터링 (이미 검색 시 기간을 정했지만, 더 정확하게 시간 단위로 필터링)
        valid_articles = []
        now = datetime.now(timezone.utc)
        time_limit = timedelta(hours=self.config.NEWS_FETCH_HOURS)

        for entry in all_entries:
            if 'published_parsed' in entry:
                published_dt = datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=timezone.utc)
                if (now - published_dt) <= time_limit:
                    valid_articles.append(entry)
        
        print(f"시간 필터링 후 {len(valid_articles)}개의 유효한 기사가 남았습니다.")
        
        # 이미 발송된 링크 제외
        new_articles = [article for article in valid_articles if self._clean_and_validate_url(article['link']) not in self.sent_links]
        print(f"이미 발송된 기사를 제외하고, 총 {len(new_articles)}개의 새로운 후보 기사를 발견했습니다.")

        if not new_articles:
            print("처리할 새로운 기사가 없습니다.")
            return []

        # --- 나머지 로직은 기존과 거의 동일 ---
        print("\n--- 1단계: 실제 기사 URL 추출 시작 (병렬 처리) ---")
        resolved_articles = []
        with ThreadPoolExecutor(max_workers=5) as executor: # URL 추출도 병렬로 처리하여 속도 개선
            future_to_entry = {executor.submit(self._resolve_google_news_url, entry): entry for entry in new_articles[:self.config.MAX_ARTICLES]}
            for future in as_completed(future_to_entry):
                resolved_info = future.result()
                if resolved_info:
                    resolved_articles.append(resolved_info)
        print(f"--- 1단계 완료: {len(resolved_articles)}개의 유효한 실제 URL 확보 ---\n")

        if not resolved_articles:
            print("URL 추출 후 처리할 새로운 기사가 없습니다.")
            return []

        print(f"--- 2단계: 기사 콘텐츠 병렬 처리 시작 (대상: {len(resolved_articles)}개) ---")
        processed_news = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            future_to_article = {executor.submit(self._process_article_content, article): article for article in resolved_articles}
            for future in as_completed(future_to_article):
                article = future_to_article[future]
                try:
                    result = future.result(timeout=60)
                    if result:
                        processed_news.append(result)
                except TimeoutError:
                    print(f"  ㄴ> ❌ 시간 초과: '{article['title']}' 기사 처리가 너무 오래 걸려 건너뜁니다.")
                except Exception as exc:
                    print(f"  ㄴ> ❌ 처리 중 오류: '{article['title']}' 기사에서 예외 발생: {exc}")
        
        print(f"--- 2단계 완료: 총 {len(processed_news)}개 기사 처리 성공 ---\n")
        return processed_news

    def update_sent_links_log(self, news_list):
        links = [news['link'] for news in news_list]
        try:
            with open(self.config.SENT_LINKS_FILE, 'a', encoding='utf-8') as f:
                for link in links: f.write(link + '\n')
            print(f"✅ {len(links)}개 링크를 발송 기록에 추가했습니다.")
        except Exception as e:
            print(f"❌ 발송 기록 파일 업데이트 실패: {e}")

class EmailService:
    def __init__(self, config):
        self.config = config
        self.credentials = self._get_credentials()

    def _get_credentials(self):
        creds = None
        if os.path.exists(self.config.TOKEN_FILE):
            creds = Credentials.from_authorized_user_file(self.config.TOKEN_FILE, ['https://www.googleapis.com/auth/gmail.send'])
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(self.config.CREDENTIALS_FILE, ['https://www.googleapis.com/auth/gmail.send'])
                creds = flow.run_local_server(port=0)
            with open(self.config.TOKEN_FILE, 'w') as token:
                token.write(creds.to_json())
        return creds

    def create_email_body(self, news_list, ai_briefing_html, today_date_str, price_indicators):
        env = Environment(loader=FileSystemLoader('.'))
        template = env.get_template('email_template.html')

        return template.render(
            news_list=news_list, 
            today_date=today_date_str, 
            ai_briefing=ai_briefing_html, 
            price_indicators = price_indicators
        )

    def send_email(self, subject, body_html, image_path=None):
        if not self.config.RECIPIENT_LIST:
            print("❌ 수신자 목록이 비어있어 이메일을 발송할 수 없습니다.")
            return

        try:
            service = build('gmail', 'v1', credentials=self.credentials)
        
            # 이메일 본문과 이미지를 함께 보내기 위한 MIMEMultipart 객체 생성
            message = MIMEMultipart('related')
            message['To'] = ", ".join(self.config.RECIPIENT_LIST)
            message['From'] = formataddr((self.config.SENDER_NAME, self.config.SENDER_EMAIL))
            message['Subject'] = subject

            # HTML 본문 첨부
            msg_alternative = MIMEMultipart('alternative')
            msg_alternative.attach(MIMEText(body_html, 'html', 'utf-8'))
            message.attach(msg_alternative)

            # 이미지 파일이 있으면 첨부
            if image_path and os.path.exists(image_path):
                with open(image_path, 'rb') as f:
                    msg_image = MIMEImage(f.read())
                    # Content-ID 설정. HTML의 <img src="cid:price_chart">에서 이 ID를 사용함
                    msg_image.add_header('Content-ID', '<price_chart>')
                    message.attach(msg_image)
                    print(f"✅ 이메일에 '{image_path}' 이미지를 첨부했습니다.")

            encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
            create_message = {'raw': encoded_message}
            send_message = service.users().messages().send(userId="me", body=create_message).execute()
            print(f"✅ 이메일 발송 성공! (Message ID: {send_message['id']})")
        except HttpError as error:
            print(f"❌ 이메일 발송 실패: {error}")

def load_newsletter_history(filepath='previous_newsletter.json'):
    """이전에 발송된 뉴스레터 내용을 JSON 파일에서 불러옵니다."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            history = json.load(f)
            print(f"✅ 이전 뉴스레터 기록({len(history)}개)을 불러왔습니다.")
            return history
    except FileNotFoundError:
        print("ℹ️ 이전 뉴스레터 기록 파일이 없습니다. 첫 실행으로 간주합니다.")
        return []
    except Exception as e:
        print(f"❌ 이전 뉴스레터 기록 로딩 실패: {e}")
        return []

def save_newsletter_history(news_list, filepath='previous_newsletter.json'):
    """발송 완료된 뉴스레터 내용을 다음 실행을 위해 JSON 파일로 저장합니다."""
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(news_list, f, ensure_ascii=False, indent=4)
        print(f"✅ 이번 뉴스레터 내용({len(news_list)}개)을 다음 실행을 위해 저장했습니다.")
    except Exception as e:
        print(f"❌ 뉴스레터 내용 저장 실패: {e}")

def main():
    print("🚀 뉴스레터 자동 생성 프로세스를 시작합니다.")
    try:
        config = Config()
        news_scraper = NewsScraper(config)
        ai_service = AIService(config)
        news_service = NewsService(config, news_scraper, ai_service)
        email_service = EmailService(config)

        # 1. 이전 뉴스 기록 및 모든 가격 지표 가져오기
        previous_top_news = load_newsletter_history()
        price_indicators = get_price_indicators(config)

        # 2. (추가) 유가 데이터로 차트 이미지 생성
        chart_image_file = None
        if price_indicators.get("seven_day_data"):
            chart_image_file = create_price_trend_chart(price_indicators["seven_day_data"])

        # 3. 최신 뉴스 수집 및 AI 선별
        all_news = news_service.get_fresh_news()
        if not all_news:
            print("ℹ️ 발송할 새로운 뉴스가 없습니다. 프로세스를 종료합니다.")
            return

        top_news = ai_service.select_top_news(all_news, previous_top_news)
        if not top_news:
            print("ℹ️ AI가 뉴스를 선별하지 못했습니다. 프로세스를 종료합니다.")
            return

        # 4. AI 브리핑 생성 및 이메일 본문 준비
        ai_briefing_md = ai_service.generate_briefing(top_news)
        ai_briefing_html = markdown_to_html(ai_briefing_md)
        today_str = get_kst_today_str()
        email_subject = f"[{today_str}] 오늘의 화물/물류 뉴스 Top {len(top_news)}"
        
        email_body = email_service.create_email_body(top_news, ai_briefing_html, today_str, price_indicators)
        
        # 5. (추가) 이메일 발송 시 생성된 차트 이미지 파일 경로 전달
        email_service.send_email(email_subject, email_body, chart_image_file)
        
        # 6. 로그 및 히스토리 저장
        news_service.update_sent_links_log(top_news)
        save_newsletter_history(top_news)

        print("\n🎉 모든 프로세스가 성공적으로 완료되었습니다.")

    except (ValueError, FileNotFoundError) as e:
        print(f"🚨 설정 또는 파일 오류: {e}")
    except Exception as e:
        print(f"🔥 치명적인 오류 발생: {e.__class__.__name__}: {e}")


if __name__ == "__main__":
    main()
