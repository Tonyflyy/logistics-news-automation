# news_collector.py
import os
import smtplib
import platform
import base64
import markdown
import json
import time
import random
from weather_service import WeatherService 
from risk_briefing_service import RiskBriefingService
from ai_service import AIService
from utils import get_kst_today_str,get_kst_week_str, markdown_to_html, image_to_base64_string
import logging
from datetime import datetime, timezone, timedelta, date
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from urllib.parse import urljoin, urlparse
from io import BytesIO
from concurrent.futures import ProcessPoolExecutor, as_completed
import re
from newspaper import Article
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
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
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import openai

from config import Config

def _create_driver_for_process(driver_path: str): # ✨ 드라이버 경로를 인자로 받음
    """각 프로세스를 위한 독립적인 Selenium 드라이버를 생성하는 함수"""
    config = Config()
    chrome_options = Options()
    chrome_options.page_load_strategy = 'eager'
    chrome_options.add_argument("--headless=new") 
    chrome_options.add_argument(f'--user-agent={random.choice(config.USER_AGENTS)}')
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--log-level=3") 
    chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
    chrome_options.add_argument("--blink-settings=imagesEnabled=false") 
    chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    chrome_options.add_experimental_option('useAutomationExtension', False)
    chrome_options.add_argument("--disable-blink-features=AutomationControlled")
    try:
        # ✨ 더 이상 드라이버를 매번 설치하지 않고, 전달받은 경로를 사용
        service = ChromeService(executable_path=driver_path)
        driver = webdriver.Chrome(service=service, options=chrome_options)
        stealth(driver, languages=["ko-KR", "ko"], vendor="Google Inc.", platform="Win32",
                webgl_vendor="Intel Inc.", renderer="Intel Iris OpenGL Engine", fix_hairline=True)
        #driver.set_page_load_timeout(20)
        return driver
    except Exception as e:
        print(f"🚨 드라이버 생성 실패: {e}")
        return None


def _clean_and_validate_url_worker(url):
    """(독립 함수) URL의 유효성을 검사하고 정제합니다."""
    config = Config()
    try:
        parsed = urlparse(url)
        if any(ad_domain in parsed.netloc for ad_domain in config.AD_DOMAINS_BLACKLIST):
            return None
        if any(pattern in parsed.path.lower() for pattern in config.UNWANTED_URL_PATTERNS):
            return None
            
        path = parsed.path.lower()
        is_likely_article = (
            any(char.isdigit() for char in path) or
            any(keyword in path for keyword in ['/news/', '/article/', '/view/']) or
            path.endswith('.html') or path.endswith('.php') or path.endswith('.do')
        )
        if 'hyundai.co.kr' in parsed.netloc:
            pass
        elif not is_likely_article:
            return None
        return parsed._replace(fragment="").geturl()
    except Exception:
        return None

def resolve_google_news_url_worker(entry, driver_path: str):
    start_time = time.time()
    title = entry['title']
    gnews_link = entry['link']
    print(f"[DEBUG] '{title}' URL 추출 시작...")
    
    driver = None
    try:
        driver_start = time.time()
        driver = _create_driver_for_process(driver_path)
        if not driver: return None
        print(f"[DEBUG] '{title}' | 드라이버 생성 | {time.time() - driver_start:.2f}s")

        get_start = time.time()
        driver.get(gnews_link)
        print(f"[DEBUG] '{title}' | driver.get() | {time.time() - get_start:.2f}s")
        
        wait_start = time.time()
        wait = WebDriverWait(driver, 30)
        link_element = wait.until(EC.presence_of_element_located((By.TAG_NAME, 'a')))
        print(f"[DEBUG] '{title}' | WebDriverWait | {time.time() - wait_start:.2f}s")

        original_url = link_element.get_attribute('href')
        validated_url = _clean_and_validate_url_worker(original_url)
        
        if validated_url:
            print(f"  -> ✅ URL 추출 성공: {title} | 총 소요시간: {time.time() - start_time:.2f}s")
            return {'title': title, 'link': validated_url}
        else:
            print(f"   ㄴ> 🗑️ 기사 URL 패턴이 아니라서 제외: {original_url}")
            return None
    except Exception as e:
        if 'TimeoutException' in e.__class__.__name__:
             print(f"  ㄴ> ❌ URL 추출 타임아웃: '{title}' (현재 URL: {driver.current_url if driver else 'N/A'})")
        else:
             print(f"  ㄴ> ❌ URL 추출 실패: '{title}'에서 오류 발생: {e.__class__.__name__}")
        return None
    finally:
        if driver:
            driver.quit()


def process_article_content_worker(articles_batch, driver_path: str):
    processed_in_batch = []
    driver = None
    config = Config()
    scraper = NewsScraper(config)
    ai_service = AIService(config)

    for i, article_info in enumerate(articles_batch):
        batch_start_time = time.time()
        title = article_info['title']
        url = article_info['link']
        print(f"[DEBUG] '{title}' 콘텐츠 처리 시작...")

        if i % 7 == 0:
            if driver: driver.quit()
            driver_start = time.time()
            driver = _create_driver_for_process(driver_path)
            print(f"[DEBUG] '{title}' | 새 드라이버 생성 | {time.time() - driver_start:.2f}s")
        if not driver:
            print("   ㄴ> 🚨 드라이버가 없어 현재 배치를 중단합니다.")
            break

        try:
            get_start = time.time()
            driver.get(url)
            print(f"[DEBUG] '{title}' | 1. driver.get() | {time.time() - get_start:.2f}s")
            
            wait_start = time.time()
            content_selectors = '#article-view-content, .article_body, .entry-content, #article-view, #articleBody, .post-content, #articles_detail'
            WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.CSS_SELECTOR, content_selectors)))
            print(f"[DEBUG] '{title}' | 2. WebDriverWait | {time.time() - wait_start:.2f}s")
            
            html_content = driver.page_source
            soup = BeautifulSoup(html_content, 'lxml')
            content_area = soup.select_one(content_selectors)
            if not content_area: continue
            
            text_processing_start = time.time()
            article_text = content_area.get_text(strip=True)
            if len(article_text) < 300: continue
            print(f"[DEBUG] '{title}' | 3. 본문 텍스트 처리 | {time.time() - text_processing_start:.2f}s")

            summary_start = time.time()
            ai_summary = ai_service.generate_single_summary(title, url, article_text_from_selenium=article_text)
            if not ai_summary or "요약 정보를 생성할 수 없습니다" in ai_summary: continue
            print(f"[DEBUG] '{title}' | 4. AI 요약 | {time.time() - summary_start:.2f}s")

            image_start = time.time()
            image_url = scraper.get_image_url(soup, url)
            print(f"[DEBUG] '{title}' | 5. 이미지 URL 검색 | {time.time() - image_start:.2f}s")
            
            # ... (이하 이미지 처리 및 저장 로직은 동일)
            image_data, final_width, final_height = None, 0, 0
            if image_url and image_url != config.DEFAULT_IMAGE_URL:
                try:
                    img_dl_start = time.time()
                    img_response = scraper.session.get(image_url, timeout=10)
                    img_response.raise_for_status()
                    img = Image.open(BytesIO(img_response.content))
                    print(f"[DEBUG] '{title}' | 6. 이미지 다운로드 | {time.time() - img_dl_start:.2f}s")
                    # 리사이징 로직 ...
                    original_width, original_height = img.size
                   
                    aspect_ratio = original_height / original_width
                    if aspect_ratio > 1.5:
                        target_height = min(original_height, 800)
                        target_width = int(target_height / aspect_ratio)
                        img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
                    else:
                        target_width = 640
                        target_height = int(target_width * aspect_ratio)
                        img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
                    final_width, final_height = img.size
                    buffer = BytesIO()
                    if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                    img.save(buffer, format='JPEG', quality=85)
                    image_data = buffer.getvalue()
                except Exception: image_data = None
            if not image_data: continue
            
            processed_in_batch.append({'title': title, 'link': url, 'ai_summary': ai_summary, 'image_data': image_data, 'image_final_width': final_width, 'image_final_height': final_height})
            print(f"  -> ✅ 콘텐츠 처리 성공: '{title}' | 총 소요시간: {time.time() - batch_start_time:.2f}s")

        except Exception as e:
            if 'TimeoutException' in e.__class__.__name__:
                print(f"  > ❌ 콘텐츠 처리 타임아웃: '{title}' (현재 URL: {driver.current_url if driver else 'N/A'}) | 총 소요시간: {time.time() - batch_start_time:.2f}s")
            else:
                print(f"  ㄴ> ❌ 콘텐츠 처리 중 오류: '{title}' ({e.__class__.__name__}) | 총 소요시간: {time.time() - batch_start_time:.2f}s")
            continue
    if driver:
        driver.quit()
    return processed_in_batch

def render_html_template(context, target='email'):
    """Jinja2 템플릿을 렌더링합니다. target에 따라 이미지 경로를 다르게 설정합니다."""
    env = Environment(loader=FileSystemLoader('.'))
    template = env.get_template('email_template.html')
    
    # context에서 Base64 데이터 추출
    price_chart_b64 = context.get("price_indicators", {}).get("price_chart_b64")
    weather_dashboard_b64 = context.get("weather_dashboard_b64")

    context['target'] = target

    if target == 'web':
        # 웹페이지에서는 Base64 데이터 URI를 사용
        if price_chart_b64:
            context['price_chart_src'] = f"data:image/png;base64,{price_chart_b64}"
        if weather_dashboard_b64:
            context['weather_dashboard_src'] = f"data:image/png;base64,{weather_dashboard_b64}"
    else: # 'email'
        context['price_chart_src'] = 'cid:price_chart'
        context['weather_dashboard_src'] = 'cid:weather_dashboard'
    
    return template.render(context)
def format_change(change):
            if change > 0:
                return f"주간 +{change:,.0f}원 ▲"
            elif change < 0:
                return f"주간 {change:,.0f}원 ▼"
            else:
                return "주간 변동 없음"
            
def create_price_trend_chart(seven_day_data, today_str):
    """(개선) 최근 7일 유가 데이터로 각 날짜별 가격이 표시된 차트 이미지를 생성합니다."""
    filename = f"images/price_chart_{today_str}.png"
    try:
        # --- 폰트 설정 (기존과 동일) ---
        system_name = platform.system()
        if system_name == 'Windows':
            plt.rc('font', family='Malgun Gothic')
        elif system_name == 'Darwin':
            plt.rc('font', family='AppleGothic')
        else:
            font_path = '/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf'
            if os.path.exists(font_path):
                # Matplotlib의 폰트 목록에 나눔고딕이 없으면, 캐시를 재생성
                if 'NanumGothic' not in [f.name for f in fm.fontManager.ttflist]:
                    print("-> 나눔고딕 폰트를 찾을 수 없어 Matplotlib 폰트 캐시를 재생성합니다.")
                    fm._rebuild()
                plt.rc('font', family='NanumGothic') # 'NanumGothicBold' 대신 'NanumGothic' 사용이 더 안정적
            else:
                print("⚠️ 나눔고딕 폰트 파일이 없어 기본 폰트로 출력됩니다.")
        plt.rcParams['axes.unicode_minus'] = False

        # --- 데이터 준비 (기존과 동일) ---
        dates = [d['DATE'][-4:-2] + "/" + d['DATE'][-2:] for d in seven_day_data['gasoline']]
        gasoline_prices = [float(p['PRICE']) for p in seven_day_data['gasoline']]
        diesel_prices = [float(p['PRICE']) for p in seven_day_data['diesel']]
        
        # --- 그래프 생성 ---
        fig, ax = plt.subplots(figsize=(8, 5)) # 그래프 크기를 약간 조정
        ax.plot(dates, gasoline_prices, 'o-', label='휘발유', color='#3498db', linewidth=2)
        ax.plot(dates, diesel_prices, 'o-', label='경유', color='#e74c3c', linewidth=2)
        
        ax.set_title("최근 7일 유가 추이", fontsize=16, pad=20, fontweight='bold')
        ax.legend()
        ax.grid(True, which='both', linestyle=':', linewidth=0.7)
        
        formatter = FuncFormatter(lambda y, _: f'{int(y):,}원')
        ax.yaxis.set_major_formatter(formatter)
        ax.tick_params(axis='x', rotation=0)
        
        # ✨ [신규] 각 데이터 포인트에 가격 텍스트를 추가하는 로직
        # va='bottom'은 포인트 바로 위에, va='top'은 바로 아래에 텍스트를 위치시킵니다.
        for i, price in enumerate(gasoline_prices):
            ax.text(i, price + 5, f'{int(price):,}', ha='center', va='bottom', fontsize=9, color='#005a9c')
            
        for i, price in enumerate(diesel_prices):
            ax.text(i, price + 5, f'{int(price):,}', ha='center', va='bottom', fontsize=9, color='#a8382c')
        
        # Y축 범위를 살짝 늘려서 위쪽 텍스트가 잘리지 않도록 함
        ymin, ymax = ax.get_ylim()
        ax.set_ylim(ymin, ymax * 1.05)
        
        fig.tight_layout()
        
        # --- 파일 저장 및 반환 (기존과 동일) ---
        plt.savefig(filename, dpi=150)
        plt.close(fig)
        print(f"✅ 유가 추이 차트 이미지 '{filename}'를 생성했습니다.")
        
        base64_image = image_to_base64_string(filename)
        return {"filepath": filename, "base64": base64_image}

    except Exception as e:
        print(f"❌ 차트 이미지 생성 실패: {e}")
        return None
    
    
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
        response = requests.get(sido_price_url, timeout=30)
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
            response = requests.get(urea_url, timeout=30)
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
            time.sleep(5)
        except Exception as e:
            area_name = config.AREA_CODE_MAP.get(area_code, "알 수 없는 지역")
            print(f"❌ {area_name} 요소수 가격 조회 실패: {e}")
            continue
    print("✅ 주요 도시별 요소수 가격 정보를 가져왔습니다.")

    # --- 3. 전국 가격 추세 및 차트용 데이터 가져오기 (API 호출 1회) ---
    try:
        trend_url = f"http://www.opinet.co.kr/api/avgRecentPrice.do?out=json&code={config.OPINET_API_KEY}"
        response = requests.get(trend_url, timeout=30)
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
    #indicator_data["cheapest_stations"] = get_cheapest_stations(config, count=20)

    # --- 최종 데이터 구조 정리 ---
    indicator_data["city_prices"] = list(city_data_map.values())
    return indicator_data
    

class NewsScraper:
    def __init__(self, config):
        self.config = config
        self.session = self._create_session()

    def _create_session(self):
        session = requests.Session()
        retry = Retry(total=5, backoff_factor=1.0, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session
    
    def _transform_thumbnail_url(self, url: str) -> str:
        """썸네일 URL을 원본 URL로 변형 시도 (예: _v150.jpg 제거)"""
        # 정규표현식을 사용하여 URL 끝에 있는 '_v숫자', '_w숫자', '_s숫자' 등의 썸네일 패턴을 제거
        transformed_url = re.sub(r'(_[vws]\d+)\.(jpg|jpeg|png|gif)$', r'.\2', url, flags=re.IGNORECASE)
        return transformed_url

    def get_image_url(self, soup: BeautifulSoup, base_url: str) -> str:
        try:
            # 1순위: 메타 태그 (이제 soup 객체에서 바로 찾음)
            meta_image = soup.find("meta", property="og:image") or soup.find("meta", name="twitter:image")
            if meta_image and meta_image.get("content"):
                thumbnail_url = meta_image["content"]
                original_url_candidate = self._transform_thumbnail_url(thumbnail_url)
                
                full_url = self._resolve_url(base_url, original_url_candidate)
                if self._is_valid_candidate(full_url) and self._validate_image(full_url):
                    return full_url
                
                full_thumbnail_url = self._resolve_url(base_url, thumbnail_url)
                if full_thumbnail_url != full_url:
                    if self._is_valid_candidate(full_thumbnail_url) and self._validate_image(full_thumbnail_url):
                        return full_thumbnail_url

            # 2순위: 특정 기사 본문 영역 안에서 이미지 검색
            content_area = soup.select_one('#article-view-content-div, .entry-content, .article-body, #article-view-content, #article-view, #articleBody, .post-content')
            if content_area:
                for img in content_area.find_all("img", limit=5):
                    img_url = img.get("src") or img.get("data-src")
                    if img_url and self._is_valid_candidate(img_url):
                        full_url = self._resolve_url(base_url, img_url)
                        if self._validate_image(full_url):
                            return full_url
            
            # 3순위: 본문 <figure> 또는 <picture> 태그
            for tag in soup.select('figure > img, picture > img', limit=5):
                img_url = tag.get('src') or tag.get('data-src') or (tag.get('srcset').split(',')[0].strip().split(' ')[0] if tag.get('srcset') else None)
                if img_url and self._is_valid_candidate(img_url):
                    full_url = self._resolve_url(base_url, img_url)
                    if self._validate_image(full_url):
                        return full_url
            
            # 4순위: 일반 <img> 태그
            for img in soup.find_all("img", limit=10):
                img_url = img.get("src") or img.get("data-src")
                if img_url and self._is_valid_candidate(img_url):
                    full_url = self._resolve_url(base_url, img_url)
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



class NewsService:
    def __init__(self, config):
        self.config = config
        self.sent_links = self._load_sent_links()


    def _load_sent_links(self):
        try:
            with open(self.config.SENT_LINKS_FILE, 'r', encoding='utf-8') as f:
                return set(line.strip() for line in f)
        except FileNotFoundError:
            return set()

    def fetch_candidate_articles(self, keywords, hours):
        print("최신 뉴스 수집을 시작합니다...")
        client = GoogleNews(lang='ko', country='KR')
        all_entries, unique_links = [], set()
        end_date, start_date = date.today(), date.today() - timedelta(hours=hours)
        print(f"검색 기간: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")
        
        for i, group in enumerate(keywords):
            query = ' OR '.join(f'"{k}"' for k in group) + ' -해운 -항공'
            print(f"\n({i+1}/{len(keywords)}) 그룹 검색 중: [{', '.join(group)}]")
            try:
                search_results = client.search(query, from_=start_date.strftime('%Y-%m-%d'), to_=end_date.strftime('%Y-%m-%d'))
                for entry in search_results['entries']:
                    source_url = entry.source.get('href', '').lower()
                    if any(b_domain in source_url for b_domain in self.config.AD_DOMAINS_BLACKLIST):
                        continue
                    link = entry.get('link')
                    if link and link not in unique_links:
                        all_entries.append(entry)
                        unique_links.add(link)
                print(f" ➡️ {len(search_results['entries'])}개 발견, 현재까지 총 {len(all_entries)}개의 고유 기사 확보")
                time.sleep(4)
            except Exception as e:
                print(f" ❌ 그룹 검색 중 오류 발생: {e}")

        print(f"\n모든 그룹 검색 완료. 총 {len(all_entries)}개의 중복 없는 기사를 발견했습니다.")
        valid_articles = []
        now, time_limit = datetime.now(timezone.utc), timedelta(hours=hours)
        for entry in all_entries:
            if 'published_parsed' in entry and (now - datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=timezone.utc)) <= time_limit:
                valid_articles.append(entry)
        
        print(f"시간 필터링 후 {len(valid_articles)}개의 유효한 기사가 남았습니다.")
        new_articles = [article for article in valid_articles if _clean_and_validate_url_worker(article['link']) not in self.sent_links]
        print(f"이미 발송된 기사를 제외하고, 총 {len(new_articles)}개의 새로운 후보 기사를 발견했습니다.")
        return new_articles
    
    def process_articles(self, articles, driver_path):
        if not articles: 
            return []
        
        print("\n--- 1단계: 실제 기사 URL 추출 시작 (병렬 처리) ---")
        resolved_articles = []
        with ProcessPoolExecutor(max_workers=5) as executor:
            future_to_entry = {executor.submit(resolve_google_news_url_worker, entry, driver_path): entry for entry in articles[:self.config.MAX_ARTICLES_TO_PROCESS]}
            for future in as_completed(future_to_entry):
                resolved_info = future.result()
                if resolved_info: resolved_articles.append(resolved_info)
        print(f"--- 1단계 완료: {len(resolved_articles)}개의 유효한 실제 URL 확보 ---\n")
        
        if not resolved_articles: 
            return []

        print(f"--- 2단계: 기사 콘텐츠 병렬 처리 시작 (대상: {len(resolved_articles)}개) ---")
        processed_news = []
        max_workers = 2
        chunk_size = len(resolved_articles) // max_workers + (1 if len(resolved_articles) % max_workers > 0 else 0)
        article_batches = [resolved_articles[i:i + chunk_size] for i in range(0, len(resolved_articles), chunk_size)]
        
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            future_to_batch = {executor.submit(process_article_content_worker, batch, driver_path): batch for batch in article_batches}
            for future in as_completed(future_to_batch):
                try:
                    results_from_batch = future.result()
                    processed_news.extend(results_from_batch)
                except Exception as exc:
                    print(f"  ㄴ> ❌ 배치 처리 중 심각한 오류 발생: {exc.__class__.__name__} - {exc}")

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
        # 인증 객체 생성 로직이 더 이상 필요 없으므로 __init__이 매우 간단해집니다.

    def create_email_body(self, news_list, ai_briefing_html, today_date_str, price_indicators, has_weather_dashboard=False):
        env = Environment(loader=FileSystemLoader('.'))
        template = env.get_template('email_template.html')
        return template.render(
            news_list=news_list,
            today_date=today_date_str,
            ai_briefing=ai_briefing_html,
            price_indicators=price_indicators,
            has_weather_dashboard=has_weather_dashboard 
        )

    def _get_credentials(self):
        """서비스 계정으로만 인증을 시도합니다 (GitHub Actions 또는 로컬 파일)."""
        gcp_json_credentials_str = os.getenv('GCP_SA_KEY_JSON')
        
        # 1. GitHub Actions 환경일 경우
        if gcp_json_credentials_str:
            print("-> 서비스 계정(GitHub Secret)으로 인증을 시도합니다.")
            try:
                credentials_info = json.loads(gcp_json_credentials_str)
                credentials = service_account.Credentials.from_service_account_info(
                    credentials_info,
                    scopes=['https://www.googleapis.com/auth/gmail.send'],
                    subject=self.config.SENDER_EMAIL
                )
                print("✅ 서비스 계정(Secret)으로 인증 성공!")
                return credentials
            except Exception as e:
                print(f"❌ 서비스 계정(Secret) 인증 실패: {e}")
                return None
        
        # 2. 로컬 환경일 경우
        elif os.path.exists('service-account-key.json'):
            print("-> 로컬 서비스 계정 파일(service-account-key.json)으로 인증을 시도합니다.")
            try:
                credentials = service_account.Credentials.from_service_account_file(
                    'service-account-key.json',
                    scopes=['https://www.googleapis.com/auth/gmail.send'],
                    subject=self.config.SENDER_EMAIL
                )
                print("✅ 로컬 서비스 계정 파일로 인증 성공!")
                return credentials
            except Exception as e:
                print(f"❌ 로컬 서비스 계정 파일 인증 실패: {e}")
                return None
        
        # 3. 위 두 가지가 모두 실패한 경우
        else:
            print("🚨 인증 정보를 찾을 수 없습니다. GitHub Secret 또는 service-account-key.json 파일이 필요합니다.")
            return None


    def send_email(self, subject, body_html, images_to_embed=None):
        # ✨ [수정] 실행 모드에 따라 데일리/위클리 수신자를 선택합니다.
        if self.config.EXECUTION_MODE == 'weekly':
            recipients = self.config.WEEKLY_RECIPIENT_LIST
            print(f"-> 위클리 수신자 목록을 사용합니다. (총 {len(recipients)}명)")
        else: # 'daily'
            recipients = self.config.DAILY_RECIPIENT_LIST
            print(f"-> 데일리 수신자 목록을 사용합니다. (총 {len(recipients)}명)")

        if not recipients:
            print("❌ 수신자 목록이 비어있어 이메일을 발송할 수 없습니다.")
            return

        sender_email = self.config.SENDER_EMAIL
        app_password = os.getenv('GMAIL_APP_PASSWORD')

        if not app_password:
            print("🚨 GMAIL_APP_PASSWORD Secret이 설정되지 않았습니다.")
            return

        try:
            # SMTP 서버에 먼저 연결하고 로그인
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(sender_email, app_password)

            # ✨ [수정] 선택된 수신자 목록(recipients)을 사용합니다.
            for recipient in recipients:
                msg = MIMEMultipart('related')
                msg['From'] = formataddr((self.config.SENDER_NAME, sender_email))
                msg['Subject'] = subject
                msg['To'] = recipient

                msg_alternative = MIMEMultipart('alternative')
                msg_alternative.attach(MIMEText(body_html, 'html', 'utf-8'))
                msg.attach(msg_alternative)

                if images_to_embed:
                    for image_info in images_to_embed:
                        image_cid = image_info['cid']
                        msg_image = None
                        if 'path' in image_info and os.path.exists(image_info['path']):
                            with open(image_info['path'], 'rb') as f:
                                msg_image = MIMEImage(f.read())
                        elif 'data' in image_info and image_info['data']:
                            msg_image = MIMEImage(image_info['data'])

                        if msg_image:
                            msg_image.add_header('Content-ID', f'<{image_cid}>')
                            msg.attach(msg_image)

                server.send_message(msg)
                print(f" -> ✅ 이메일 발송 성공: {recipient}")

            server.quit()
            print(f"✅ 총 {len(recipients)}명에게 이메일 발송을 완료했습니다.")

        except Exception as e:
            print(f"❌ SMTP 이메일 발송 중 오류 발생: {e}")


def load_newsletter_history(filepath='previous_newsletter.json'):
    """이전에 발송된 뉴스레터 내용을 JSON 파일에서 불러옵니다."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            # 👇 파일 내용이 비어있는지 확인하는 로직 추가
            if not content:
                print("ℹ️ 이전 뉴스레터 기록 파일이 비어있습니다.")
                return []
            history = json.loads(content)
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
    # 이미지 데이터는 저장할 필요 없으므로 제외하고 저장
    history_to_save = [
        {k: v for k, v in news.items() if k != 'image_data'} 
        for news in news_list
    ]
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(history_to_save, f, ensure_ascii=False, indent=4)
        print(f"✅ 이번 뉴스레터 내용({len(history_to_save)}개)을 다음 실행을 위해 저장했습니다.")
    except Exception as e:
        print(f"❌ 뉴스레터 내용 저장 실패: {e}")

def update_archive_index():
    """archive 폴더의 html 파일 목록을 생성 시간순으로 정렬하여 index.html을 생성/업데이트합니다."""
    print("-> 아카이브 인덱스 페이지를 업데이트합니다...")
    try:
        archive_dir = 'archive'
        
        # ✨ [핵심 수정] 파일 이름이 아닌, 파일의 최종 수정 시간을 기준으로 정렬합니다.
        
        # 1. 'index.html'을 제외한 모든 html 파일의 전체 경로를 가져옵니다.
        file_paths = [
            os.path.join(archive_dir, f) 
            for f in os.listdir(archive_dir) 
            if f.endswith('.html') and f != 'index.html'
        ]
        
        # 2. 파일의 최종 수정 시간을 기준으로 내림차순(최신순) 정렬합니다.
        sorted_paths = sorted(file_paths, key=os.path.getmtime, reverse=True)
        
        # 3. 전체 경로에서 파일 이름만 다시 추출합니다.
        html_files = [os.path.basename(p) for p in sorted_paths]

        # HTML 페이지 기본 구조 (기존과 동일)
        html_content = """
        <!DOCTYPE html>
        <html lang="ko">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>지난 뉴스레터 목록</title>
            <style>
                body { font-family: sans-serif; margin: 40px; background-color: #f6f8fa; }
                .container { max-width: 600px; margin: 0 auto; background-color: #fff; border: 1px solid #e1e4e8; border-radius: 6px; padding: 20px 40px; }
                h1 { text-align: center; }
                ul { list-style: none; padding: 0; }
                li { margin: 15px 0; }
                a { text-decoration: none; font-size: 1.1em; color: #0366d6; }
                a:hover { text-decoration: underline; }
            </style>
        </head>
        <body>
            <div class="container">
                <h1>지난 뉴스레터 목록</h1>
                <ul>
        """

        # 파일 목록으로 링크 생성 (기존과 동일)
        for filename in html_files:
            date_str = filename.replace('.html', '')
            html_content += f'            <li><a href="{filename}">{date_str} 뉴스레터</a></li>\n'

        html_content += """
                </ul>
            </div>
        </body>
        </html>
        """

        with open(os.path.join(archive_dir, 'index.html'), 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        print("✅ 아카이브 인덱스 페이지 업데이트 완료.")

    except Exception as e:
        print(f"❌ 아카이브 인덱스 페이지 업데이트 실패: {e}")

def image_to_base64_string(filepath):
    """이미지 파일 경로를 받아 Base64 텍스트 문자열로 변환합니다."""
    try:
        with open(filepath, 'rb') as image_file:
            encoded_bytes = base64.b64encode(image_file.read())
            return encoded_bytes.decode('utf-8')
    except Exception as e:
        print(f"❌ 이미지를 Base64로 변환하는 중 오류 발생: {e}")
        return None

def run_daily_newsletter(config, driver_path):
    """일간 뉴스레터 생성의 모든 과정을 처리하는 함수"""
    print("🚀 일간 뉴스레터 생성을 시작합니다.")
    try:
        # --- 1. 서비스 객체 초기화 ---
        news_service = NewsService(config)
        email_service = EmailService(config)
        weather_service = WeatherService(config)
        ai_service = AIService(config) 
        risk_briefing_service = RiskBriefingService(ai_service)
        
        today_str = get_kst_today_str()
        os.makedirs('archive', exist_ok=True)
        os.makedirs('images', exist_ok=True)

        # --- 2. 보조 데이터 생성 (유가, 날씨, 리스크) ---
        price_indicators = get_price_indicators(config)
        weather_result = weather_service.create_dashboard_image(today_str)
        price_chart_result = create_price_trend_chart(price_indicators.get("seven_day_data"), today_str) if price_indicators.get("seven_day_data") else None
        
        risk_events = risk_briefing_service.generate_risk_events()


        # 띠별 운세 데이터 생성 및 가공 ---
        zodiac_horoscopes = ai_service.generate_zodiac_horoscopes()
        if zodiac_horoscopes:
            zodiac_emojis = {'쥐': '🐭', '소': '🐮', '호랑이': '🐯', '토끼': '🐰', '용': '🐲', '뱀': '🐍', '말': '🐴', '양': '🐑', '원숭이': '🐵', '닭': '🐔', '개': '🐶', '돼지': '🐷'}
            for item in zodiac_horoscopes:
                item['emoji'] = zodiac_emojis.get(item['name'], '❓')
        # ---

        # --- 3. 뉴스 데이터 수집 및 처리 ---
        previous_top_news = load_newsletter_history()
        
        # ✨ [수정] 분리된 함수를 일간용 설정으로 순서대로 호출
        candidate_articles = news_service.fetch_candidate_articles(
            keywords=config.KEYWORD_GROUPS_DAILY, 
            hours=config.NEWS_FETCH_HOURS_DAILY
        )
        all_news = news_service.process_articles(candidate_articles, driver_path)
        
        if not all_news:
            print("ℹ️ 발송할 새로운 뉴스가 없습니다.")
        
        top_news = ai_service.select_top_news(all_news, previous_top_news, count=config.SELECT_NEWS_COUNT_DAILY)
        
        if not top_news:
            print("ℹ️ AI가 뉴스를 선별하지 못했습니다.")

        ai_briefing_md = ai_service.generate_briefing(top_news, mode='daily')
        ai_briefing_html = markdown_to_html(ai_briefing_md)
        
        # --- 4. 템플릿에 전달할 최종 데이터 준비 ---
        title_text = "로디와 함께하는 오늘의 물류 산책"
        if price_chart_result: price_indicators['price_chart_b64'] = price_chart_result['base64']
        weather_dashboard_b64 = weather_result['base64'] if weather_result else None
        
        web_news_list = []
        for news in top_news:
            news_copy = news.copy()
            if news_copy.get('image_data'):
                news_copy['image_src'] = f"data:image/jpeg;base64,{base64.b64encode(news_copy['image_data']).decode('utf-8')}"
            web_news_list.append(news_copy)

        context = {
            "title": title_text,
            "today_date": today_str,
            "date": date,
            "ai_briefing": ai_briefing_html,
            "risk_events": risk_events,               # 상세 리스크 목록
            "price_indicators": price_indicators,
            "news_list": web_news_list,
            "weather_dashboard_b64": weather_dashboard_b64,
            "has_weather_dashboard": True if weather_dashboard_b64 else False,
            "zodiac_horoscopes": zodiac_horoscopes
        }
        
        # --- 5. HTML 생성 및 이메일 발송 ---
        web_html = render_html_template(context, target='web')
        archive_filepath = f"archive/{today_str}.html"
        with open(archive_filepath, 'w', encoding='utf-8') as f: f.write(web_html)
        print(f"✅ 웹페이지 버전을 '{archive_filepath}'에 저장했습니다.")

        for i, news_item in enumerate(top_news):
            if news_item.get('image_data'): news_item['image_cid'] = f'news_image_{i}'
        
        context['news_list'] = top_news
        email_body = render_html_template(context, target='email')
        email_subject = f"[{today_str}] {title_text}"
        
        images_to_embed = []
        if price_chart_result and price_chart_result.get('filepath'):
            images_to_embed.append({'path': price_chart_result['filepath'], 'cid': 'price_chart'})
        if weather_result and weather_result.get('filepath'):
            images_to_embed.append({'path': weather_result['filepath'], 'cid': 'weather_dashboard'})
        for news_item in top_news:
            if news_item.get('image_data') and news_item.get('image_cid'):
                images_to_embed.append({'data': news_item['image_data'], 'cid': news_item['image_cid']})
        
        # 뉴스레터 배너 이미지를 첨부합니다.
        banner_path = "assets/logicharacter.png"
        if os.path.exists(banner_path):
            images_to_embed.append({'path': banner_path, 'cid': 'newsletter_banner'})

         # 운세 캐릭터 이미지를 첨부합니다.
        fortune_char_path = "assets/fortunechar.png"
        if os.path.exists(fortune_char_path):
            images_to_embed.append({'path': fortune_char_path, 'cid': 'fortunechar.png'})    
        
        email_service.send_email(email_subject, email_body, images_to_embed)
        
        # --- 6. 상태 저장 및 마무리 ---
        if top_news:
            news_service.update_sent_links_log(top_news)
            save_newsletter_history(top_news)
        update_archive_index()

        #주간 뉴스레터 후보군으로 오늘의 기사를 저장
        try:
            # 기존 후보군 파일이 있으면 불러오고, 없으면 빈 리스트로 시작
            try:
                with open(config.WEEKLY_CANDIDATES_FILE, 'r', encoding='utf-8') as f:
                    all_candidates = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                all_candidates = []
    
            # 오늘 발송된 뉴스를 추가 (이미지 데이터를 Base64로 인코딩하여 저장)
            for news in top_news:
                news_to_save = news.copy() # 원본 수정을 피하기 위해 복사
                if 'image_data' in news_to_save and news_to_save['image_data']:
                # 이미지(bytes)를 Base64(string)으로 변환
                    news_to_save['image_data'] = base64.b64encode(news_to_save['image_data']).decode('utf-8')
                all_candidates.append(news_to_save)

            # 전체 후보군을 다시 파일에 저장
            with open(config.WEEKLY_CANDIDATES_FILE, 'w', encoding='utf-8') as f:
                json.dump(all_candidates, f, ensure_ascii=False, indent=4)
            print(f"✅ 주간 후보 뉴스로 {len(top_news)}개를 저장했습니다. (총 {len(all_candidates)}개)")

        except Exception as e:
            print(f"❌ 주간 후보 뉴스 저장 실패: {e}")

        print("\n🎉 일간 뉴스레터 프로세스가 성공적으로 완료되었습니다.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"🔥 일간 뉴스레터 생성 중 치명적인 오류 발생: {e.__class__.__name__}: {e}")


def run_weekly_newsletter(config, driver_path):
    """주간 뉴스레터 생성의 모든 과정을 처리하는 함수"""
    print("🚀 주간 뉴스레터 생성을 시작합니다.")
    try:
        # --- 1. 서비스 객체 초기화 ---
        news_service = NewsService(config)
        email_service = EmailService(config)
        weather_service = WeatherService(config)
        ai_service = AIService(config) 
        risk_briefing_service = RiskBriefingService(ai_service)
        
        week_str = get_kst_week_str()
        os.makedirs('archive', exist_ok=True)
        os.makedirs('images', exist_ok=True)

        # --- 2. 보조 데이터 생성 (유가, 날씨, 리스크) ---
        price_indicators = get_price_indicators(config)
        weather_result = weather_service.create_dashboard_image(week_str)
        price_chart_result = create_price_trend_chart(price_indicators.get("seven_day_data"), week_str) if price_indicators.get("seven_day_data") else None
        
        risk_events = risk_briefing_service.generate_risk_events()


        # ✨ [신규] 띠별 운세 데이터 생성 및 가공 ---
        zodiac_horoscopes = ai_service.generate_zodiac_horoscopes()
        if zodiac_horoscopes:
            zodiac_emojis = {'쥐': '🐭', '소': '🐮', '호랑이': '🐯', '토끼': '🐰', '용': '🐲', '뱀': '🐍', '말': '🐴', '양': '🐑', '원숭이': '🐵', '닭': '🐔', '개': '🐶', '돼지': '🐷'}
            for item in zodiac_horoscopes:
                item['emoji'] = zodiac_emojis.get(item['name'], '❓')
        # ---

        all_news = []
        try:
            with open(config.WEEKLY_CANDIDATES_FILE, 'r', encoding='utf-8') as f:
                all_news = json.load(f)
            if not all_news:
                raise FileNotFoundError("주간 후보 파일이 비어있습니다.")
            print(f"✅ 주간 후보 뉴스 {len(all_news)}개를 파일에서 불러왔습니다.")
        except (FileNotFoundError, json.JSONDecodeError):
            print(f"⚠️ 주간 후보 파일을 사용할 수 없어 웹에서 직접 뉴스를 수집합니다 (Fallback).")
            # --- Fallback: 기존의 웹 스크래핑 로직 실행 ---
            candidate_articles = news_service.fetch_candidate_articles(
                keywords=config.KEYWORD_GROUPS_WEEKLY, 
                hours=config.NEWS_FETCH_HOURS_WEEKLY
            )
            all_news = news_service.process_articles(candidate_articles, driver_path)

        # ✨ [신규] 파일에서 불러온 Base64 이미지 데이터를 원래의 bytes 형태로 복원합니다.
        for news in all_news:
            if 'image_data' in news and isinstance(news['image_data'], str):
                # Base64(string)을 다시 이미지(bytes)로 변환
                news['image_data'] = base64.b64decode(news['image_data'])
        
        # --- 3. 뉴스 데이터 수집 및 처리 (주간용 설정 사용) ---
        previous_top_news = load_newsletter_history(filepath='previous_weekly_newsletter.json')
        top_news = ai_service.select_top_news(all_news, previous_top_news, count=config.SELECT_NEWS_COUNT_WEEKLY)
        
        if not top_news:
            print("ℹ️ AI가 주간 뉴스를 선별하지 못했습니다. (또는 수집된 뉴스가 없습니다)")

        ai_briefing_md = ai_service.generate_briefing(top_news, mode='weekly')
        ai_briefing_html = markdown_to_html(ai_briefing_md)
        
        # --- 4. 템플릿에 전달할 최종 데이터 준비 ---
        title_text = "로디와 함께하는 주간 물류 산책"
        if price_chart_result: price_indicators['price_chart_b64'] = price_chart_result['base64']
        weather_dashboard_b64 = weather_result['base64'] if weather_result else None
        
        web_news_list = []
        for news in top_news:
            news_copy = news.copy()
            if news_copy.get('image_data'):
                news_copy['image_src'] = f"data:image/jpeg;base64,{base64.b64encode(news_copy['image_data']).decode('utf-8')}"
            web_news_list.append(news_copy)

        context = {
            "title": title_text,
            "today_date": week_str,
            "date": date,
            "ai_briefing": ai_briefing_html,
            "risk_events": risk_events,              
            "price_indicators": price_indicators,
            "news_list": web_news_list,
            "weather_dashboard_b64": weather_dashboard_b64,
            "has_weather_dashboard": True if weather_dashboard_b64 else False,
            "zodiac_horoscopes": zodiac_horoscopes
        }
        
        # --- 5. HTML 생성 및 이메일 발송 ---
        web_html = render_html_template(context, target='web')
        archive_filepath = f"archive/{week_str}.html"
        with open(archive_filepath, 'w', encoding='utf-8') as f: f.write(web_html)
        print(f"✅ 웹페이지 버전을 '{archive_filepath}'에 저장했습니다.")

        for i, news_item in enumerate(top_news):
            if news_item.get('image_data'): news_item['image_cid'] = f'news_image_{i}'
        
        context['news_list'] = top_news
        email_body = render_html_template(context, target='email')
        email_subject = f"[{week_str}] {title_text} 요약"
        
        images_to_embed = []
        if price_chart_result and price_chart_result.get('filepath'):
            images_to_embed.append({'path': price_chart_result['filepath'], 'cid': 'price_chart'})
        if weather_result and weather_result.get('filepath'):
            images_to_embed.append({'path': weather_result['filepath'], 'cid': 'weather_dashboard'})
        for news_item in top_news:
            if news_item.get('image_data') and news_item.get('image_cid'):
                images_to_embed.append({'data': news_item['image_data'], 'cid': news_item['image_cid']})
        

        # 뉴스레터 배너 이미지를 첨부합니다.
        banner_path = "assets/logicharacter.png"
        if os.path.exists(banner_path):
            images_to_embed.append({'path': banner_path, 'cid': 'newsletter_banner'})


         # 운세 캐릭터 이미지를 첨부합니다.
        fortune_char_path = "assets/fortunechar.png"
        if os.path.exists(fortune_char_path):
            images_to_embed.append({'path': fortune_char_path, 'cid': 'fortunechar.png'})        
        
        email_service.send_email(email_subject, email_body, images_to_embed)
        
        # --- 6. 상태 저장 및 마무리 ---
        if top_news:
            news_service.update_sent_links_log(top_news)
            save_newsletter_history(top_news, filepath='previous_weekly_newsletter.json')
        update_archive_index()

        try:
            with open(config.WEEKLY_CANDIDATES_FILE, 'w', encoding='utf-8') as f:
                json.dump([], f) # 빈 리스트를 파일에 덮어쓰기
            print(f"✅ '{config.WEEKLY_CANDIDATES_FILE}' 파일을 초기화했습니다.")
        except Exception as e:
            print(f"❌ 주간 후보 뉴스 파일 초기화 실패: {e}")

        print("\n🎉 주간 뉴스레터 프로세스가 성공적으로 완료되었습니다.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"🔥 주간 뉴스레터 생성 중 치명적인 오류 발생: {e.__class__.__name__}: {e}")


def main():
    """실행 모드에 따라 적절한 뉴스레터 생성 함수를 호출하는 컨트롤러"""
    print("-> Chrome 드라이버를 준비합니다...")
    try:
        driver_path = ChromeDriverManager().install()
        print(f"✅ 드라이버 준비 완료: {driver_path}")
    except Exception as e:
        print(f"🔥 치명적인 오류 발생: Chrome 드라이버를 준비할 수 없습니다. {e}")
        return

    config = Config()
    
    if config.EXECUTION_MODE == 'weekly':
        run_weekly_newsletter(config, driver_path)
    elif config.EXECUTION_MODE == 'daily':
        run_daily_newsletter(config, driver_path)
    else:
        print(f"❌ 알 수 없는 실행 모드입니다: '{config.EXECUTION_MODE}'. 'daily' 또는 'weekly'로 설정해주세요.")

def main_for_chart_test():
    """오직 '유가 추이 차트' 생성 기능만 테스트하는 함수"""
    print("🚀 유가 추이 차트 생성 테스트를 시작합니다.")
    try:
        # --- 1. 필요한 객체 및 폴더 준비 ---
        config = Config()
        today_str = get_kst_today_str()
        os.makedirs('images', exist_ok=True)

        # --- 2. 유가 데이터 수집 ---
        price_indicators = get_price_indicators(config)
        
        # --- 3. 차트 생성 (테스트 핵심) ---
        if price_indicators.get("seven_day_data"):
            create_price_trend_chart(price_indicators["seven_day_data"], today_str)
        else:
            print("❌ 차트를 생성하는 데 필요한 7일간의 유가 데이터를 가져오지 못했습니다.")

        print("\n🎉 차트 생성 테스트가 완료되었습니다. 'images' 폴더를 확인해주세요.")

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"🔥 테스트 중 치명적인 오류 발생: {e.__class__.__name__}: {e}")




def main_for_horoscope_test():
    """오직 '띠별 운세' 생성 기능만 테스트하는 함수"""
    print("🚀 띠별 운세 생성 테스트를 시작합니다.")
    try:
        config = Config()
        ai_service = AIService(config)
        
        horoscopes = ai_service.generate_zodiac_horoscopes()
        
        if horoscopes:
            print("\n--- [AI 띠별 운세 생성 결과] ---")
            for h in horoscopes:
                print(f"\n[ {h.get('name')}띠 ]")
                print(f"  - 운세: {h.get('fortune')}")
                print(f"  - 행운색: {h.get('lucky_color')}")
                print(f"  - 궁합: {h.get('compatible_sign')}")
            print("\n---------------------------------")
        else:
            print("❌ 운세 데이터를 생성하지 못했습니다.")

        print("\n🎉 띠별 운세 생성 테스트가 완료되었습니다.")
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"🔥 테스트 중 치명적인 오류 발생: {e.__class__.__name__}: {e}")

def test_image_rendering():
    """
    모든 이미지가 HTML에 정상적으로 표시되는지 확인하기 위해
    1) 웹페이지용 'image_test_preview.html' 파일 생성
    2) 데일리 수신자에게 실제 테스트 이메일 발송
    을 모두 수행합니다.
    """
    print("🚀 이미지 렌더링 및 이메일 발송 테스트를 시작합니다...")
    try:
        # --- 1. 테스트에 필요한 기본 객체 및 폴더 준비 ---
        config = Config()
        today_str = get_kst_today_str()
        os.makedirs('images', exist_ok=True)
        email_service = EmailService(config)

        # --- 2. 동적 이미지 생성 (날씨 대시보드, 유가 차트) ---
        weather_service = WeatherService(config)
        weather_result = weather_service.create_dashboard_image(today_str)
        price_chart_result = create_price_trend_chart({
            "gasoline": [{"DATE": f"202509{d:02d}", "PRICE": str(1750+d)} for d in range(10, 17)],
            "diesel": [{"DATE": f"202509{d:02d}", "PRICE": str(1650+d)} for d in range(10, 17)]
        }, today_str)
        print("✅ (테스트) 동적 이미지 생성 완료")
        
        # --- 3. 웹페이지용 HTML 렌더링 및 저장 ---
        sample_news_image_b64 = image_to_base64_string('assets/fortunechar.png')
        web_context = {
            "title": "이미지 렌더링 테스트 (웹)", "today_date": today_str, "target": "web",
            "has_weather_dashboard": True,
            "weather_dashboard_b64": weather_result['base64'] if weather_result else None,
            "price_indicators": {'price_chart_b64': price_chart_result['base64']} if price_chart_result else {},
            "news_list": [{'title': '[샘플 뉴스]','link': '#','ai_summary': '웹용 이미지 테스트','image_src': f"data:image/png;base64,{sample_news_image_b64}"}],
            "zodiac_horoscopes": []
        }
        web_html = render_html_template(web_context, target='web')
        output_filename = 'image_test_preview.html'
        with open(output_filename, 'w', encoding='utf-8') as f: f.write(web_html)
        print(f"✅ 웹 미리보기 파일 '{output_filename}' 생성 완료!")

        # --- 4. [신규] 이메일 발송을 위한 데이터 준비 및 실제 발송 ---
        print("\n🚀 실제 이메일 발송을 준비합니다...")
        
        # (A) 이메일용 context 및 본문 생성
        email_context = {
            "title": "이미지 렌더링 테스트 (이메일)", "today_date": today_str, "target": "email",
            "has_weather_dashboard": True,
            "weather_dashboard_b64": None, "price_indicators": {}, # cid를 사용하므로 b64 데이터는 불필요
            "news_list": [{'title': '[샘플 뉴스]','link': '#','ai_summary': '이메일용 이미지 테스트','image_data': base64.b64decode(sample_news_image_b64), 'image_cid': 'sample_news_image_0'}],
            "zodiac_horoscopes": []
        }
        email_body = render_html_template(email_context, target='email')

        # (B) 이메일에 첨부할 이미지 목록 생성
        images_to_embed = []
        if os.path.exists('assets/logicharacter.png'): images_to_embed.append({'path': 'assets/logicharacter.png', 'cid': 'newsletter_banner'})
        if os.path.exists('assets/fortunechar.png'): images_to_embed.append({'path': 'assets/fortunechar.png', 'cid': 'fortunechar.png'})
        if weather_result: images_to_embed.append({'path': weather_result['filepath'], 'cid': 'weather_dashboard'})
        if price_chart_result: images_to_embed.append({'path': price_chart_result['filepath'], 'cid': 'price_chart'})
        images_to_embed.append({'data': base64.b64decode(sample_news_image_b64), 'cid': 'sample_news_image_0'})

        # (C) 이메일 발송 (데일리 수신자에게)
        email_subject = "[이미지 테스트] 뉴스레터"
        config.EXECUTION_MODE = 'daily' # EmailService가 데일리 수신자를 선택하도록 모드 설정
        email_service.send_email(email_subject, email_body, images_to_embed)

        print("\n🎉 모든 테스트가 완료되었습니다.")

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"🔥 이미지 테스트 중 오류 발생: {e}")



if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        mode = sys.argv[1]
        if mode == 'test_images':
            test_image_rendering()
        elif mode == 'test_horoscope':
            main_for_horoscope_test()
        else:
            # 기본 main() 실행 또는 다른 인자 처리
            main()
    else:
        # 로컬에서 직접 실행 시 (인자 없음)
        main()
        # test_image_rendering() # 로컬 테스트 시 이 부분 주석 해제

# if __name__ == "__main__":
#     # --- 빠른 테스트 실행을 위한 설정 ---
#     print("-> Chrome 드라이버를 준비합니다...")
#     try:
#         driver_path = ChromeDriverManager().install()
#         print(f"✅ 드라이버 준비 완료: {driver_path}")
#     except Exception as e:
#         print(f"🔥 치명적인 오류 발생: Chrome 드라이버를 준비할 수 없습니다. {e}")
#         # 드라이버가 없어도 테스트는 계속 진행 가능
#         driver_path = None
    
#     config = Config()
    
#     # ✨ 아래 함수를 호출하여 빠른 테스트를 실행합니다.
#     run_fast_test(config, driver_path)










