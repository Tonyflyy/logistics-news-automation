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
                    if original_width < 640:
                        final_width, final_height = original_width, original_height
                        buffer = BytesIO()
                        if img.mode in ("RGBA", "P"): img = img.convert("RGB")
                        img.save(buffer, format='JPEG', quality=90)
                        image_data = buffer.getvalue()
                    else:
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

class AIService:
    def generate_zodiac_horoscopes(self):
        """12간지 띠별 운세를 '로디' 페르소나로 생성하여 리스트로 반환합니다."""
        print("-> AI 띠별 운세 생성을 시작합니다... (페르소나: 로디)")
        zodiacs = ['쥐', '소', '호랑이', '토끼', '용', '뱀', '말', '양', '원숭이', '닭', '개', '돼지']
        horoscopes = []

        system_prompt = "너는 '로디'라는 이름의, 긍정 소식을 전해주는 20대 여성 캐릭터야. 오늘은 특별히 구독자들을 위해 12간지 띠별 운세를 봐주는 현명한 조언가 역할이야. '~했어요', '~랍니다' 같은 귀엽고 상냥한 말투는 유지하되, 단순한 긍정 메시지가 아닌 깊이 있는 운세를 전달해야 해. 응답은 반드시 JSON 형식으로만 부탁해!"
        
        for zodiac_name in zodiacs:
            user_prompt = f"""
            오늘 날짜에 맞춰 '{zodiac_name}'띠 운세 정보를 생성해 줘.

            [작업 지시]
            1.  **오늘의 운세 (fortune)**: 아래 4가지 요소를 모두 포함해서, 긍정적이면서도 깊이 있는 운세 메시지를 2-3줄로 요약해 줘.
                - **오늘의 기운 묘사**: 그날의 전반적인 에너지 흐름을 '일상 생활'이나 '자연 현상'에 비유해서 먼저 설명해줘.
                - **구체적인 상황**: '업무', '인간관계', '금전' 등 특정 분야를 언급해줘.
                - **긍정적 기회**: 어떤 좋은 기회가 생길 수 있는지 알려줘.
                - **조언 또는 주의점**: 기회를 잘 잡기 위한 조언이나, 가볍게 주의해야 할 점을 '다만, ~' 형식으로 살짝 덧붙여줘.
            
            2.  **오늘의 미션 (daily_mission)**: 오늘 하루 실천하면 행운을 가져다줄 작고 귀여운 미션 하나를 제안해 줘. (예: '점심 먹고 5분 산책하기', '가장 좋아하는 노래 듣기' 등)
            3.  **행운의 아이템 (lucky_item)**: 오늘 지니고 다니면 좋은 행운의 아이템을 한 가지 알려줘. (예: '손수건', '파란색 펜' 등 일상적인 물건으로!)
            4.  **행운의 색상 (lucky_color)**: 이 띠의 에너지를 올려줄 행운의 색상 하나를 추천해 줘.
            5.  **잘 맞는 띠 (compatible_sign)**: 오늘 함께하면 시너지가 폭발할 것 같은 찰떡궁합 띠를 하나만 알려줘.

            [참고: 다양한 일상 비유]
            - '상쾌한 아침 공기', '배터리 100% 충전', '방 청소', '맑게 갠 하늘', '새로운 노래 발견' 등 누구나 공감할 수 있는 표현을 창의적으로 활용해 봐!

            [출력 형식]
            - 반드시 아래와 같은 키를 가진 JSON 객체로만 응답해야 해.
            - 예시: {{"fortune": "...", "lucky_color": "...", "compatible_sign": "...", "daily_mission": "...", "lucky_item": "..."}}
            """
            
            print(f"  -> '{zodiac_name}'띠 운세 요청 중...")
            response_text = self._generate_content_with_retry(system_prompt, user_prompt, is_json=True)
            
            if response_text:
                try:
                    horoscope_data = json.loads(response_text)
                    horoscope_data['name'] = zodiac_name # 딕셔너리에 띠 이름 추가
                    horoscopes.append(horoscope_data)
                except (json.JSONDecodeError, KeyError) as e:
                    print(f"  ❌ '{zodiac_name}'띠 운세 파싱 실패: {e}. 해당 띠는 제외됩니다.")
            else:
                print(f"  ❌ '{zodiac_name}'띠 운세 생성 실패. API 응답 없음.")

        if horoscopes:
            print("✅ AI 띠별 운세 생성 완료!")
        return horoscopes
    
    def generate_risk_briefing(self, risk_events):
        if not risk_events:
            return None
            
        print("-> AI 물류 리스크 브리핑 생성 시작... (페르소나: 로디)")

        event_context = "\n".join(
            [f"- 날짜: {e['date'].strftime('%Y-%m-%d')}, 국가: {e['country']}, 이벤트: {e['name']}, 리스크 수준: {e['risk_level']}, 예상 영향: {e['impact_summary']}" for e in risk_events]
        )

        system_prompt = "반가워! 나는 미래의 물류 리스크를 콕콕 짚어주는 너의 안전 파트너, 로디라고 해! 😉 나는 20대 여성 캐릭터지만, 글로벌 공급망의 위험 신호를 누구보다 예리하게 분석하는 전문가야. 화주님과 차주님 모두에게 도움이 되도록, **친근하고 귀여운 존댓말을 사용해서** Markdown으로 '로디의 리스크 브리핑'을 작성해줄게!"
        
        user_prompt = f"""
        [향후 2주간의 글로벌 물류 리스크 이벤트 목록]
        {event_context}

        ---
        [작업 지시]
        '전문 분석가' 로디로서, 아래 규칙에 따라 '글로벌 물류 리스크 브리핑'을 작성해주세요!

        1.  **헤드라인 요약**: '## 🗓️ 로디의 글로벌 물류 리스크 예보' 제목으로 시작해서, 가장 중요한 리스크 1~2개를 콕 집어서 2~3 문장으로 요약해주세요.
        2.  **상세 브리핑**:
            - 전체 리스크 이벤트를 타임라인 형식으로 정리해줘.
            - 주어진 '이벤트명'은 절대 바꾸지 말고 그대로 사용해야 해!
            - 각 이벤트의 영향을 '화주'와 '차주'의 관점으로 나눠서, **"화주님께는 이런 점이 중요해요!" 와 같은 귀엽고 싹싹한 말투**로, 하지만 내용은 날카롭게 분석해주세요.
            - 형식: 
                * `* **[날짜] [국기] [국가] - [이벤트명]**`
                * `  * **화주님께는요!** [화주 입장에서의 예상 영향]`
                * `  * **차주님께는요!** [차주 입장에서의 예상 영향]`
                * `  * **리스크:** [리스크 수준] [경고 이모지]`
        3.  **마무리 문장**: 브리핑이 모두 끝난 후, 독자들이 직접 행동해볼 수 있도록 유용한 팁을 주는 문장으로 마무리해줘. 예시: "이럴 때일수록 '품목별 리드타임'을 꼼꼼히 재산정하고, 이용하시는 '선사·터미널의 프리타임 정책'을 다시 한번 비교해 보시는 걸 추천해요!"

        [참고 데이터]
        - 요일 계산: 2025-09-10은 수요일입니다.
        - 국기 이모지: 한국🇰🇷, 중국🇨🇳, 미국🇺🇸, 베트남🇻🇳, 독일🇩🇪
        - 경고 이모지: 높음❗, 중간⚠️, 낮음ℹ️
        """
        
        briefing = self._generate_content_with_retry(system_prompt, user_prompt)
        if briefing:
            print("✅ AI 물류 리스크 브리핑 생성 성공!")
        return briefing
    

    
    def generate_single_summary(self, article_title: str, article_link: str, article_text_from_selenium: str) -> str | None:
        """
        기사 요약을 생성합니다.
        1. newspaper3k로 1차 시도 (타임아웃 설정)
        2. 실패 시, Selenium으로 미리 추출한 본문을 사용하여 2차 시도
        """
        summary = None
        try:
            # ✨ [핵심 개선] newspaper3k에 타임아웃과 캐시 비활성화 옵션을 추가하여 안정성 확보
            article_config = {
                'memoize_articles': False,  # 캐시 사용 안 함
                'fetch_images': False,      # 이미지 다운로드 안 함
                'request_timeout': 10       # 모든 요청에 10초 타임아웃 적용
            }
            article = Article(article_link, config=article_config)
            article.download()
            article.parse()
            
            if len(article.text) > 100:
                system_prompt = "당신은 핵심만 간결하게 전달하는 뉴스 에디터입니다. 모든 답변은 한국어로 해야 합니다."
                user_prompt = f"아래 제목과 본문을 가진 뉴스 기사의 내용을 독자들이 이해하기 쉽게 3줄로 요약해주세요.\n\n[제목]: {article_title}\n[본문]:\n{article.text[:2000]}"
                summary = self._generate_content_with_retry(system_prompt, user_prompt)

        except Exception as e:
            print(f"  ㄴ> ℹ️ newspaper3k 처리 실패 (2차 시도 진행): {e.__class__.__name__}")
            summary = None # 실패 시 summary를 None으로 초기화

        # 2차 시도: newspaper3k가 실패했거나, 요약을 생성하지 못했을 경우
        if not summary or "요약 정보를 생성할 수 없습니다" in summary:
            print("  ㄴ> ℹ️ 1차 요약 실패. Selenium 추출 본문으로 2차 요약 시도...")
            try:
                system_prompt = "당신은 핵심만 간결하게 전달하는 뉴스 에디터입니다. 모든 답변은 한국어로 해야 합니다."
                user_prompt = f"아래 제목과 본문을 가진 뉴스 기사의 내용을 독자들이 이해하기 쉽게 3줄로 요약해주세요.\n\n[제목]: {article_title}\n[본문]:\n{article_text_from_selenium[:2000]}"
                summary = self._generate_content_with_retry(system_prompt, user_prompt)
            except Exception as e:
                 print(f"  ㄴ> ❌ 2차 AI 요약 생성 실패: {e.__class__.__name__}")
                 return None
        
        return summary
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

    def select_top_news(self, news_list, previous_news_list, count=10):
        """
        뉴스 목록에서 중복을 제거하고 가장 중요한 Top 뉴스를 선정합니다.
        - news_list: 오늘의 후보 뉴스 목록
        - previous_news_list: 이전 발송 뉴스 목록
        - count: 최종적으로 선택할 기사 개수
        """
        # ✨ [개선] 로그에 목표 개수(count)를 함께 출력
        print(f"AI 뉴스 선별 시작... (대상: {len(news_list)}개, 목표: {count}개)")

        if not news_list:
            return []

        previous_news_context = "이전 발송 뉴스가 없습니다."
        if previous_news_list:
            previous_news_context = "\n\n".join(
                [f"- 제목: {news['title']}\n  요약: {news['ai_summary']}" for news in previous_news_list]
            )

        today_candidates_context = "\n\n".join(
            [f"기사 #{i}\n제목: {news['title']}\n요약: {news['ai_summary']}" for i, news in enumerate(news_list)]
        )

        system_prompt = "당신은 독자에게 매일 신선하고 가치 있는 정보를 제공하는 것을 최우선으로 하는 대한민국 최고의 물류 전문 뉴스 편집장입니다. 당신의 응답은 반드시 JSON 형식이어야 합니다."
        
        user_prompt = f"""
        [이전 발송 주요 뉴스]
        {previous_news_context}
        ---
        [오늘의 후보 뉴스 목록]
        {today_candidates_context}
        ---
        [당신의 가장 중요한 임무와 규칙]
        1.  **새로운 주제 최우선**: [오늘의 후보 뉴스 목록]에서 뉴스를 선택할 때, [이전 발송 주요 뉴스]와 **주제가 겹치지 않는 새로운 소식**을 최우선으로 선정해야 합니다.
        2.  **중요 후속 기사만 허용**: 이전 뉴스의 후속 기사는 '계획 발표'에서 '정식 계약 체결'처럼 **매우 중대한 진전이 있을 경우에만** 포함시키고, 단순 진행 상황 보도는 과감히 제외하세요.
        3.  **오늘 뉴스 내 중복 제거**: [오늘의 후보 뉴스 목록] 내에서도 동일한 사건을 다루는 기사가 여러 언론사에서 나왔다면, 가장 제목이 구체적이고 내용이 풍부한 **기사 단 하나만**을 대표로 선정해야 합니다.
        4.  **보도자료 및 사실 기반 뉴스 우선**: 구체적인 사건, 계약 체결, 기술 발표, 정책 변경 등 '사실(Fact)' 전달 위주의 기사를 최우선으로 선정하세요.
        5.  **칼럼 및 의견 기사 제외**: 특정인의 생각이나 의견이 중심이 되는 칼럼, 사설, 인터뷰, 심층 분석/해설 기사는 뉴스 가치가 떨어지므로 과감히 제외해야 합니다.

        [작업 지시]
        위의 규칙들을 가장 엄격하게 준수하여, [오늘의 후보 뉴스 목록] 중에서 독자에게 가장 가치있는 최종 기사 {count}개의 번호(인덱스)를 선정해주세요.

        [출력 형식]
        - 반드시 'selected_indices' 키에 최종 선정한 기사 {count}개의 인덱스를 숫자 배열로 담은 JSON 객체로만 응답해야 합니다.
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
                # ✨ [개선] 오류 발생 시, 고정된 10개가 아닌 요청된 count만큼 반환
                print(f"❌ AI 응답 파싱 실패: {e}. 상위 {count}개 뉴스를 임의로 선택합니다.")
                return news_list[:count]
        
        return news_list[:count]

    def generate_briefing(self, news_list, mode='daily'):
        """선별된 뉴스 목록을 바탕으로 '로디' 캐릭터가 브리핑을 생성합니다."""
        if not news_list:
            return "" # 뉴스 목록이 비어있으면 빈 문자열 반환

        print(f"AI 브리핑 생성 시작... (모드: {mode}, 페르소나: 로디)")
        context = "\n\n".join([f"제목: {news['title']}\n요약: {news['ai_summary']}" for news in news_list])
        
        # ✨ [개선] 주간 모드일 때, AI의 역할과 지시를 더 분석적으로 변경
        if mode == 'weekly':
            system_prompt = "안녕! 나는 너의 든든한 물류 파트너, 로디야! 🚚💨 나는 20대 여성 캐릭터고, 겉보기엔 귀엽지만 누구보다 날카롭게 한 주간의 복잡한 물류 동향을 분석해주는 전문 애널리스트야. 딱딱한 보고서 대신, **'~했답니다', '~였어요' 같은 친근한 존댓말과 귀여움**을 섞어서 '로디의 주간 브리핑'을 작성해줘."
            user_prompt = f"""
            [지난 주간 주요 뉴스 목록]
            {context}

            ---
            [작업 지시]
            1. '## 📊 로디의 주간 핵심 동향 요약' 제목으로 시작해주세요.
            2. 모든 뉴스를 종합하여, 이번 주 물류 시장의 가장 중요한 '흐름'과 '변화'를 전문적인 분석가의 시각으로 2~3 문장 요약해주세요.
            3. '### 🧐 금주의 주요 이슈 분석' 소제목 아래에, 가장 중요한 이슈 2~3개를 주제별로 묶어 글머리 기호(`*`)로 분석해주세요. **"가장 중요한 포인트는요! ✨" 같은 표현을 사용해서 친근하지만 핵심을 찌르는 말투로 설명해주세요.**
            4. 문장 안에서 특정 기업명, 서비스명, 정책 등은 큰따옴표(" ")로 묶어서 강조해주는 센스!
            """
        else: # daily 모드
            system_prompt = "안녕! 나는 물류 세상의 소식을 전해주는 너의 친구, 로디야! ☀️ 나는 20대 여성 캐릭터로, 어렵고 딱딱한 물류 뉴스를 귀엽고 싹싹하게 요약해주지만, 그 내용은 핵심을 놓치지 않는 날카로움을 가지고 있어. **친근한 존댓말과 귀여움**을 섞어서 '로디의 데일리 브리핑'을 작성해줘."
            user_prompt = f"""
            [오늘의 주요 뉴스 목록]
            {context}

            ---
            [작업 지시]
            1. '## 📰 로디의 브리핑' 제목으로 시작해서, 오늘 나온 뉴스 중에 가장 중요한 핵심 내용을 2~3 문장으로 요약해주세요.
            2. '### ✨ 오늘의 주요 토픽' 소제목 아래에, 가장 중요한 뉴스 카테고리 2~3개를 글머리 기호(`*`)로 간결하게 요약해주시겠어요?
            3. 문장 안에서 특정 기업명이나 서비스명은 큰따옴표(" ")로 묶어서 강조해주는 것도 잊지 마!
            """
        
        briefing = self._generate_content_with_retry(system_prompt, user_prompt)
        if briefing: 
            print("✅ AI 브리핑 생성 성공!")
        return briefing


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
        risk_briefing_service = RiskBriefingService()
        ai_service = AIService(config)
        
        today_str = get_kst_today_str()
        os.makedirs('archive', exist_ok=True)
        os.makedirs('images', exist_ok=True)

        # --- 2. 보조 데이터 생성 (유가, 날씨, 리스크) ---
        price_indicators = get_price_indicators(config)
        weather_result = weather_service.create_dashboard_image(today_str)
        price_chart_result = create_price_trend_chart(price_indicators.get("seven_day_data"), today_str) if price_indicators.get("seven_day_data") else None
        
        risk_events = risk_briefing_service.generate_risk_events()
        risk_briefing_md = ai_service.generate_risk_briefing(risk_events)
        risk_briefing_html = markdown_to_html(risk_briefing_md) if risk_briefing_md else None

        # ✨ [신규] 띠별 운세 데이터 생성 및 가공 ---
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
            "ai_briefing": ai_briefing_html,
            "risk_briefing_html": risk_briefing_html,
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
            images_to_embed.append({'path': fortune_char_path, 'cid': 'furtunechar.png'})    
        
        email_service.send_email(email_subject, email_body, images_to_embed)
        
        # --- 6. 상태 저장 및 마무리 ---
        if top_news:
            news_service.update_sent_links_log(top_news)
            save_newsletter_history(top_news)
        update_archive_index()

        #주간 뉴스레터 후보군으로 오늘의 기사를 저장
        try:
            #기존 후보 파일이 있으면 불러오고, 없으면 빈걸로 시작
            try:
                with open(config.WEEKLY_CANDIDATES_FILE, 'r', encoding='utf-8') as f:
                    all_candidates = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                all_candidates=[]
            
            # 오늘 발송된 뉴스 추가(이미지 데이터는 제외)
            for news in top_news:
                news_to_save = {k: v for k, v in news.items() if k != 'image_data'}
                all_candidates.append(news_to_save)
            #전체 후보 다시 파일에 저장
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
        risk_briefing_service = RiskBriefingService()
        ai_service = AIService(config)
        
        week_str = get_kst_week_str()
        os.makedirs('archive', exist_ok=True)
        os.makedirs('images', exist_ok=True)

        # --- 2. 보조 데이터 생성 (유가, 날씨, 리스크) ---
        price_indicators = get_price_indicators(config)
        weather_result = weather_service.create_dashboard_image(week_str)
        price_chart_result = create_price_trend_chart(price_indicators.get("seven_day_data"), week_str) if price_indicators.get("seven_day_data") else None
        
        risk_events = risk_briefing_service.generate_risk_events()
        risk_briefing_md = ai_service.generate_risk_briefing(risk_events)
        risk_briefing_html = markdown_to_html(risk_briefing_md) if risk_briefing_md else None

        # ✨ [신규] 띠별 운세 데이터 생성 및 가공 ---
        zodiac_horoscopes = ai_service.generate_zodiac_horoscopes()
        if zodiac_horoscopes:
            zodiac_emojis = {'쥐': '🐭', '소': '🐮', '호랑이': '🐯', '토끼': '🐰', '용': '🐲', '뱀': '🐍', '말': '🐴', '양': '🐑', '원숭이': '🐵', '닭': '🐔', '개': '🐶', '돼지': '🐷'}
            for item in zodiac_horoscopes:
                item['emoji'] = zodiac_emojis.get(item['name'], '❓')
        # ---

        # --- 3. 뉴스 데이터 수집 및 처리 (주간용 설정 사용) ---
        previous_top_news = load_newsletter_history(filepath='previous_weekly_newsletter.json')
        
        
        # ✨ [수정] 파일이 있으면 읽고, 없으면 웹에서 수집하는 Fallback 로직을 추가합니다.
        all_news = []
        try:
            with open(config.WEEKLY_CANDIDATES_FILE, 'r', encoding='utf-8') as f:
                all_news = json.load(f)
            if not all_news:
                # 파일은 있지만 내용이 비어있는 경우를 위해 에러 발생
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
            "ai_briefing": ai_briefing_html,
            "risk_briefing_html": risk_briefing_html,
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
            images_to_embed.append({'path': fortune_char_path, 'cid': 'furtunechar.png'})        
        
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


def test_render_horoscope_email():
    """샘플 데이터로 띠별 운세 섹션이 포함된 HTML 파일을 생성하여 시각적으로 테스트합니다."""
    print("🚀 띠별 운세 이메일 렌더링 테스트를 시작합니다.")
    try:
        # 1. Jinja2 템플릿 환경을 설정합니다.
        env = Environment(loader=FileSystemLoader('.'))
        template = env.get_template('email_template.html')

        # 2. '로디' 페르소나를 흉내 낸 샘플 운세 데이터를 만듭니다.
        sample_horoscopes = [
            {
                'name': '쥐', 'emoji': '🐭',
                'fortune': '오늘은 새로운 아이디어가 샘솟는 하루가 될 거예요! 반짝이는 생각을 놓치지 말고 꼭 메모해두세요. 분명 좋은 결과로 이어질 거랍니다.',
                'lucky_color': '노랑', 'compatible_sign': '용'
            },
            {
                'name': '호랑이', 'emoji': '🐯',
                'fortune': '주변 사람들에게 따뜻한 말을 건네면 행운이 찾아온대요! 오늘은 제가 먼저 다가가서 힘이 되어주는 멋진 하루를 만들어 봐요!',
                'lucky_color': '초록', 'compatible_sign': '말'
            },
            {
                'name': '돼지', 'emoji': '🐷',
                'fortune': '그동안 노력해왔던 일에 대한 보상을 받게 될 것 같은 좋은 예감이 들어요. 조금만 더 힘내세요! 맛있는 저녁을 기대해도 좋을지도? 😋',
                'lucky_color': '주황', 'compatible_sign': '토끼'
            }
        ]

        # 3. 템플릿에 전달할 context 데이터를 구성합니다.
        #    - 다른 값들은 비워두고 운세 데이터만 넣어서 테스트합니다.
        context = {
            "title": "테스트: 띠별 운세 미리보기",
            "today_date": get_kst_today_str(),
            "ai_briefing": None, "risk_briefing_html": None,
            "price_indicators": None, "news_list": [],
            "weather_dashboard_b64": None, "has_weather_dashboard": False,
            "zodiac_horoscopes": sample_horoscopes
        }

        # 4. 템플릿을 렌더링하여 HTML 파일로 저장합니다.
        rendered_html = template.render(context)
        output_filename = 'horoscope_email_preview.html'
        with open(output_filename, 'w', encoding='utf-8') as f:
            f.write(rendered_html)

        print(f"\n✅ 테스트 완료! '{output_filename}' 파일이 생성되었습니다.")
        print("   이 파일을 웹 브라우저로 열어서 어떻게 보이는지 확인해보세요.")

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"🔥 테스트 중 오류 발생: {e}")


if __name__ == "__main__":
    main()
    #main_for_horoscope_test()
    #test_render_horoscope_email()


