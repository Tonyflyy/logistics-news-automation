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
from utils import get_kst_today_str, markdown_to_html, image_to_base64_string
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
import google.generativeai as genai
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
                print(f"  ㄴ> ❌ 콘텐츠 처리 타임아웃: '{title}' (현재 URL: {driver.current_url if driver else 'N/A'}) | 총 소요시간: {time.time() - batch_start_time:.2f}s")
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

def create_price_trend_chart(seven_day_data, today_str):
    """최근 7일 유가 데이터로 차트 이미지를 생성하고, 파일 경로와 Base64 문자열을 딕셔너리로 반환합니다."""
    filename = f"images/price_chart_{today_str}.png"
    try:
        # --- (차트를 그리는 로직은 동일합니다) ---
        system_name = platform.system()
        if system_name == 'Windows':
            plt.rc('font', family='Malgun Gothic')
        elif system_name == 'Darwin':
            plt.rc('font', family='AppleGothic')
        else:
            if os.path.exists('/usr/share/fonts/truetype/nanum/NanumGothic.ttf'):
                plt.rc('font', family='NanumGothic')
            else:
                print("⚠️ NanumGothic 폰트가 없어 기본 폰트로 출력됩니다 (한글 깨짐 가능성).")
        plt.rcParams['axes.unicode_minus'] = False

        dates = [d['DATE'][-4:-2] + "/" + d['DATE'][-2:] for d in seven_day_data['gasoline']]
        gasoline_prices = [float(p['PRICE']) for p in seven_day_data['gasoline']]
        diesel_prices = [float(p['PRICE']) for p in seven_day_data['diesel']]
        
        fig, ax = plt.subplots(figsize=(7, 4))
        ax.plot(dates, gasoline_prices, 'o-', label='휘발유', color='#3498db')
        ax.plot(dates, diesel_prices, 'o-', label='경유', color='#e74c3c')
        
        ax.set_title("최근 7일 휘발유·경유 가격 추이", fontsize=15, pad=20)
        ax.legend()
        ax.grid(True, which='both', linestyle='--', linewidth=0.5)
        
        formatter = FuncFormatter(lambda y, _: f'{int(y):,}원')
        ax.yaxis.set_major_formatter(formatter)
        
        ax.tick_params(axis='x', rotation=0)
        fig.tight_layout()
        
        # 1. 이미지 파일로 저장
        plt.savefig(filename, dpi=150)
        plt.close(fig)
        print(f"✅ 유가 추이 차트 이미지 '{filename}'를 생성했습니다.")
        
        # 2. Base64 문자열로 변환
        base64_image = image_to_base64_string(filename)
        
        # 3. 딕셔너리 형태로 반환
        return {"filepath": filename, "base64": base64_image}

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
        response = requests.get(url, timeout=30)
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
    def generate_risk_briefing(self, risk_events):
        """수집된 리스크 이벤트 목록을 바탕으로 AI 브리핑을 생성합니다."""
        if not risk_events:
            return None
            
        print("-> AI 물류 리스크 브리핑 생성 시작...")

        event_context = "\n".join(
            [f"- 날짜: {e['date'].strftime('%Y-%m-%d')}, 국가: {e['country']}, 이벤트: {e['name']}, 리스크 수준: {e['risk_level']}, 예상 영향: {e['impact_summary']}" for e in risk_events]
        )

        system_prompt = "당신은 글로벌 공급망 리스크 분석 전문가입니다. 주어진 데이터를 바탕으로, 화주와 차주 모두에게 유용한 물류 리스크 브리핑을 Markdown 형식으로 작성합니다."
        
        # ✨ [최종 개선] AI가 '화주'와 '차주'의 관점을 분리하여 분석하도록 프롬프트 수정
        user_prompt = f"""
        [향후 2주간의 글로벌 물류 리스크 이벤트 목록]
        {event_context}

        ---
        [작업 지시]
        당신은 단순한 정보 전달자가 아닌 '분석가'입니다. 아래 규칙에 따라 '글로벌 물류 리스크 브리핑'을 작성해주세요.

        1.  **헤드라인 요약**:
            - '## 🗓️ 주간 글로벌 물류 리스크 브리핑' 제목으로 시작합니다.
            - 목록에서 가장 중요하고 영향이 큰 리스크 1~2개를 식별하여, 화주와 차주 모두에게 미칠 핵심 영향을 2~3 문장으로 요약해주세요. 연속된 공휴일은 '연휴'로 묶어서 표현해야 합니다.

        2.  **상세 브리핑**:
            - 전체 리스크 이벤트를 타임라인 형식으로 정리합니다.
            - **핵심 규칙: 각 이벤트의 영향을 '화주'와 '차주'의 관점으로 반드시 나누어 각각 한 문장으로 설명해주세요.**
                - **화주 영향:** 선적 예약의 어려움, 운임 변동, 리드타임 증가 등 '비용'과 '일정' 관점의 정보를 제공합니다.
                - **차주 영향:** 터미널 혼잡, 운행 대기시간 증가, 특정 구간 물량 변동 등 '운행'과 '수입' 관점의 정보를 제공합니다.
            - 여러 날에 걸친 동일한 이벤트는 `[YYYY/MM/DD ~ MM/DD]` 형식으로 기간을 묶어서 표현해주세요.
            - 형식: 
                * `* **[날짜 또는 기간] [국기] [국가] - [이벤트명]**`
                * `  * **화주 영향:** [화주 입장에서의 예상 영향]`
                * `  * **차주 영향:** [차주 입장에서의 예상 영향]`
                * `  * **리스크:** [리스크 수준] [경고 이모지]`

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
        (최종 안정화 버전) 기사 요약을 생성합니다.
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
        4.  **보도자료 및 사실 기반 뉴스 우선**: 구체적인 사건, 계약 체결, 기술 발표, 정책 변경 등 '사실(Fact)' 전달 위주의 기사를 최우선으로 선정하세요.
        5.  **칼럼 및 의견 기사 제외**: 특정인의 생각이나 의견이 중심이 되는 칼럼, 사설, 인터뷰, 심층 분석/해설 기사는 뉴스 가치가 떨어지므로 과감히 제외해야 합니다.

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

    # def _create_stealth_driver(self):
    #     chrome_options = Options()
    #     # ✨ [개선] '--headless=new'는 최신 headless 모드를 의미합니다.
    #     chrome_options.add_argument("--headless=new") 
    #     chrome_options.add_argument("--no-sandbox")
    #     chrome_options.add_argument("--disable-dev-shm-usage")
        
    #     # ✨ [개선] 불필요한 로그 메시지를 숨겨서 터미널을 깨끗하게 유지합니다.
    #     chrome_options.add_argument("--log-level=3") 
    #     chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])

    #     # ✨ [성능 향상] 스크래핑 시 이미지 로딩을 비활성화하여 페이지 로딩 속도를 대폭 향상시킵니다.
    #     chrome_options.add_argument("--blink-settings=imagesEnabled=false") 

    #     chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
    #     chrome_options.add_experimental_option('useAutomationExtension', False)
    #     chrome_options.add_argument(f'--user-agent={random.choice(self.config.USER_AGENTS)}')
    #     chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        
    #     try:
    #         service = ChromeService(ChromeDriverManager().install())
    #         driver = webdriver.Chrome(service=service, options=chrome_options)
    #         stealth(driver, languages=["ko-KR", "ko"], vendor="Google Inc.", platform="Win32",
    #                 webgl_vendor="Intel Inc.", renderer="Intel Iris OpenGL Engine", fix_hairline=True)
            
    #         # ✨ [개선] 페이지 전체가 로딩될 때까지 기다리지 않고, HTML 구조만 다운로드되면 바로 다음 단계로 진행하여 속도를 개선합니다.
    #         driver.set_page_load_timeout(20) # 페이지 전체 로딩 타임아웃
    #         return driver
    #     except Exception as e:
    #         print(f"🚨 드라이버 생성 실패: {e}")
    #         return None

    def _load_sent_links(self):
        try:
            with open(self.config.SENT_LINKS_FILE, 'r', encoding='utf-8') as f:
                return set(line.strip() for line in f)
        except FileNotFoundError:
            return set()

    # def _clean_and_validate_url(self, url: str) -> str | None:
    #     try:
    #         parsed = urlparse(url)
            
    #         # 1. 광고 도메인 필터링
    #         if any(ad_domain in parsed.netloc for ad_domain in self.config.AD_DOMAINS_BLACKLIST):
    #             return None
            
    #         # ✨ [개선] URL 패턴으로 '기사 페이지' 여부 판별
    #         path = parsed.path.lower()
    #         # 기사 URL의 흔한 패턴: 숫자가 있거나, 특정 키워드가 있거나, .html로 끝나거나
    #         is_likely_article = (
    #             any(char.isdigit() for char in path) or
    #             any(keyword in path for keyword in ['/news/', '/article/', '/view/']) or
    #             path.endswith('.html') or path.endswith('.php') or path.endswith('.do')
    #         )
            
    #         # 예외 사이트 처리 (hyundai.co.kr은 경로가 짧아도 허용)
    #         if 'hyundai.co.kr' in parsed.netloc:
    #             pass
    #         # 위의 패턴에 해당하지 않으면 기사가 아닐 확률이 높음
    #         elif not is_likely_article:
    #             print(f"   ㄴ> 🗑️ 기사 URL 패턴이 아니라서 제외: {url}...")
    #             return None

    #         cleaned_url = parsed._replace(fragment="").geturl()
    #         return cleaned_url
    #     except Exception:
    #         return None
    
    # def _resolve_google_news_url(self, entry):
    #     """Selenium을 사용해 Google News 링크에서 실제 기사 URL만 추출합니다."""
    #     driver = None
    #     try:
    #         driver = self._create_stealth_driver()
    #         if not driver: return None
            
    #         driver.get(entry['link'])
    #         wait = WebDriverWait(driver, 20)
    #         link_element = wait.until(EC.presence_of_element_located((By.TAG_NAME, 'a')))
    #         original_url = link_element.get_attribute('href')
    #         validated_url = self._clean_and_validate_url(original_url)
            
    #         if validated_url:
    #             print(f"  -> ✅ URL 추출 성공: {entry['title']}")
    #             return {'title': entry['title'], 'link': validated_url}
    #         return None
    #     except Exception as e:
    #         print(f"  ㄴ> ❌ URL 추출 실패: '{entry['title']}'에서 오류 발생: {e.__class__.__name__}")
    #         return None
    #     finally:
    #         if driver:
    #             driver.quit()

    # def _process_article_content(self, article_info):
    #     """(Selenium 대기 기능 강화) 실제 URL을 받아 콘텐츠 분석, AI 요약, 이미지 스크래핑을 수행합니다."""
    #     title = article_info['title']
    #     url = article_info['link']
    #     driver = None

    #     try:
    #         driver = self._create_stealth_driver()
    #         if not driver:
    #             print(f"  ㄴ> ❌ 드라이버 생성 실패, 기사 건너뜀: {title}")
    #             return None
            
    #         driver.get(url)
            
    #         # ✨ [개선] 페이지의 핵심 본문 영역 중 하나가 나타날 때까지 최대 10초간 '지능적으로' 기다립니다.
    #         content_selectors = '#article-view-content, .article_body, .entry-content, #article-view, #articleBody, .post-content'
    #         WebDriverWait(driver, 20).until(
    #             EC.presence_of_element_located((By.CSS_SELECTOR, content_selectors))
    #         )
            
    #         html_content = driver.page_source
    #         soup = BeautifulSoup(html_content, 'lxml')

    #         content_area = soup.select_one(content_selectors)
            
    #         if not content_area:
    #             print(f"   ㄴ> 🗑️ (대기 후에도) 기사 본문 구조를 찾지 못해 제외: {url}")
    #             return None
            
    #         article_text = content_area.get_text(strip=True)

    #         if len(article_text) < 300:
    #             print(f"  ㄴ> 🗑️ 본문 내용이 짧아 제외: {url}")
    #             return None
            
    #         ai_summary = self.ai_service.generate_single_summary(title, url)
    #         if not ai_summary or "요약 정보를 생성할 수 없습니다" in ai_summary:
    #             print(f"  ㄴ> ⚠️ AI 요약 생성 실패, 기사 제외")
    #             return None
            
    #         image_url = self.scraper.get_image_url(url)
    #         image_data = None
    #         final_width, final_height = 0, 0
            
    #         IMAGE_MAX_WIDTH = 640
    #         IMAGE_MAX_HEIGHT = 800
    #         TALL_IMAGE_ASPECT_RATIO_THRESHOLD = 1.5

    #         if image_url and image_url != self.config.DEFAULT_IMAGE_URL:
    #             try:
    #                 img_response = self.scraper.session.get(image_url, timeout=10)
    #                 img_response.raise_for_status()
    #                 img = Image.open(BytesIO(img_response.content))
    #                 original_width, original_height = img.size
                    
    #                 if original_width < IMAGE_MAX_WIDTH:
    #                     final_width, final_height = original_width, original_height
    #                     buffer = BytesIO()
    #                     if img.mode in ("RGBA", "P"): img = img.convert("RGB")
    #                     img.save(buffer, format='JPEG', quality=90)
    #                     image_data = buffer.getvalue()
    #                 else:
    #                     aspect_ratio = original_height / original_width # ✨ [버그 수정] 오타 수정
    #                     if aspect_ratio > TALL_IMAGE_ASPECT_RATIO_THRESHOLD:
    #                         target_height = min(original_height, IMAGE_MAX_HEIGHT)
    #                         target_width = int(target_height / aspect_ratio)
    #                         img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
    #                     else:
    #                         target_width = IMAGE_MAX_WIDTH
    #                         target_height = int(target_width * aspect_ratio)
    #                         img = img.resize((target_width, target_height), Image.Resampling.LANCZOS)
    #                     final_width, final_height = img.size
    #                     buffer = BytesIO()
    #                     if img.mode in ("RGBA", "P"): img = img.convert("RGB")
    #                     img.save(buffer, format='JPEG', quality=85)
    #                     image_data = buffer.getvalue()
    #             except Exception as e:
    #                 print(f"  ㄴ> ⚠️ 이미지 처리 실패: {e.__class__.__name__}, 이미지는 제외하고 진행")
    #                 image_data = None

    #         if not image_data:
    #             print(f"   ㄴ> 🗑️ 이미지가 없어 기사 제외: {title}")
    #             return None

    #         return {
    #             'title': title, 'link': url, 'ai_summary': ai_summary, 'image_data': image_data,
    #             'image_final_width': final_width, 'image_final_height': final_height
    #         }
    #     except Exception as e:
    #         print(f"  ㄴ> ❌ 콘텐츠 처리 중 오류: '{title}' ({e.__class__.__name__})")
    #         return None
    #     finally:
    #         if driver:
    #             driver.quit()

    def get_fresh_news(self,driver_path: str):
        # --- (상단의 뉴스 검색 및 필터링 로직은 기존과 동일) ---
        print("최신 뉴스 수집을 시작합니다...")
        client = GoogleNews(lang='ko', country='KR')
        all_entries, unique_links = [], set()
        end_date, start_date = date.today(), date.today() - timedelta(hours=self.config.NEWS_FETCH_HOURS)
        print(f"검색 기간: {start_date.strftime('%Y-%m-%d')} ~ {end_date.strftime('%Y-%m-%d')}")
        for i, group in enumerate(self.config.KEYWORD_GROUPS):
            query = ' OR '.join(f'"{keyword}"' for keyword in group) + ' -해운 -항공'
            print(f"\n({i+1}/{len(self.config.KEYWORD_GROUPS)}) 그룹 검색 중: [{', '.join(group)}]")
            try:
                search_results = client.search(query, from_=start_date.strftime('%Y-%m-%d'), to_=end_date.strftime('%Y-%m-%d'))
                for entry in search_results['entries']:
                    source_url = entry.source.get('href', '').lower()
                    if any(b_domain in source_url for b_domain in self.config.AD_DOMAINS_BLACKLIST):
                        continue # 블랙리스트에 포함된 출처면 이 기사는 건너뜀
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
        now, time_limit = datetime.now(timezone.utc), timedelta(hours=self.config.NEWS_FETCH_HOURS)
        for entry in all_entries:
            if 'published_parsed' in entry and (now - datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=timezone.utc)) <= time_limit:
                valid_articles.append(entry)
        print(f"시간 필터링 후 {len(valid_articles)}개의 유효한 기사가 남았습니다.")
        
        new_articles = [article for article in valid_articles if _clean_and_validate_url_worker(article['link']) not in self.sent_links]
        print(f"이미 발송된 기사를 제외하고, 총 {len(new_articles)}개의 새로운 후보 기사를 발견했습니다.")

        if not new_articles:
            return []
            
        print("\n--- 1단계: 실제 기사 URL 추출 시작 (병렬 처리) ---")
        resolved_articles = []
        with ProcessPoolExecutor(max_workers=5) as executor:
            future_to_entry = {executor.submit(resolve_google_news_url_worker, entry, driver_path): entry for entry in new_articles[:self.config.MAX_ARTICLES]}
            for future in as_completed(future_to_entry):
                resolved_info = future.result()
                if resolved_info: resolved_articles.append(resolved_info)
        print(f"--- 1단계 완료: {len(resolved_articles)}개의 유효한 실제 URL 확보 ---\n")

        if not resolved_articles: return []

        # ✨ [핵심 개선] 2단계: 기사 콘텐츠를 '묶음'으로 나누어 병렬 처리
        print(f"--- 2단계: 기사 콘텐츠 병렬 처리 시작 (대상: {len(resolved_articles)}개) ---")
        
        processed_news = []
        max_workers = 2
        
        # 전체 기사를 max_workers 개수만큼의 묶음으로 나눕니다.
        # 예: 27개 기사, max_workers=2 -> [14개 묶음], [13개 묶음]
        chunk_size = len(resolved_articles) // max_workers
        if len(resolved_articles) % max_workers > 0:
            chunk_size += 1
        
        article_batches = [
            resolved_articles[i:i + chunk_size]
            for i in range(0, len(resolved_articles), chunk_size)
        ]

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
        if not self.config.RECIPIENT_LIST:
            print("❌ 수신자 목록이 비어있어 이메일을 발송할 수 없습니다.")
            return

        sender_email = self.config.SENDER_EMAIL
        app_password = os.getenv('GMAIL_APP_PASSWORD')

        if not app_password:
            print("🚨 GMAIL_APP_PASSWORD Secret이 설정되지 않았습니다.")
            return

        try:
            # ✨ [개선] SMTP 서버에 먼저 연결하고 로그인합니다.
            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(sender_email, app_password)

            # ✨ [핵심 개선] 수신자 목록을 한 명씩 순회하며 개별 이메일을 발송합니다.
            for recipient in self.config.RECIPIENT_LIST:
                # 각 수신자마다 새로운 메시지 객체를 생성합니다.
                msg = MIMEMultipart('related')
                msg['From'] = formataddr((self.config.SENDER_NAME, sender_email))
                msg['Subject'] = subject
                msg['To'] = recipient # ✨ 받는 사람을 현재 수신자 1명으로 설정

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
                
                # 서버에 현재 수신자를 위한 메시지를 보냅니다.
                server.send_message(msg)
                print(f" -> ✅ 이메일 발송 성공: {recipient}")
            
            # ✨ [개선] 모든 발송이 끝난 후 서버 연결을 종료합니다.
            server.quit()
            print(f"✅ 총 {len(self.config.RECIPIENT_LIST)}명에게 이메일 발송을 완료했습니다.")

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
    """archive 폴더의 html 파일 목록을 읽어 index.html을 생성/업데이트합니다."""
    print("-> 아카이브 인덱스 페이지를 업데이트합니다...")
    try:
        archive_dir = 'archive'
        html_files = sorted(
            [f for f in os.listdir(archive_dir) if f.endswith('.html') and f != 'index.html'],
            reverse=True # 최신 날짜가 위로 오도록 역순 정렬
        )

        # HTML 페이지 기본 구조
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

        # 파일 목록으로 링크 생성
        for filename in html_files:
            date_str = filename.replace('.html', '')
            html_content += f'            <li><a href="{filename}">{date_str} 뉴스레터</a></li>\n'

        # HTML 페이지 마무리
        html_content += """
                </ul>
            </div>
        </body>
        </html>
        """

        # index.html 파일 쓰기
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

def main():
    print("🚀 뉴스레터 자동 생성 프로세스를 시작합니다.")
    try:

        # ✨ [핵심] 모든 병렬 작업 시작 전에 드라이버를 딱 한 번만 설치/준비합니다.
        print("-> Chrome 드라이버를 준비합니다...")
        try:
            driver_path = ChromeDriverManager().install()
            print(f"✅ 드라이버 준비 완료: {driver_path}")
        except Exception as e:
            print(f"🔥 치명적인 오류 발생: Chrome 드라이버를 준비할 수 없습니다. {e}")
            return
        config = Config()
        # ✨ [개선] 메인 로직에서는 더 이상 scraper와 ai_service를 직접 생성하지 않습니다.
        news_service = NewsService(config, None, None) 
        email_service = EmailService(config)
        weather_service = WeatherService(config)
        risk_briefing_service = RiskBriefingService()


        os.makedirs('archive', exist_ok=True)
        os.makedirs('images', exist_ok=True)
        today_str = get_kst_today_str()

        price_indicators = get_price_indicators(config)
        weather_result = weather_service.create_dashboard_image(today_str)
        
        price_chart_result = None
        if price_indicators.get("seven_day_data"):
            price_chart_result = create_price_trend_chart(price_indicators["seven_day_data"], today_str)


        risk_events = risk_briefing_service.generate_risk_events()
        ai_service_main = AIService(config)
        risk_briefing_md = ai_service_main.generate_risk_briefing(risk_events)
        risk_briefing_html = markdown_to_html(risk_briefing_md) if risk_briefing_md else None


        previous_top_news = load_newsletter_history()
        # ✨ [개선] news_service는 이제 ai_service를 직접 사용하지 않고, 독립적인 함수를 호출합니다.
        all_news = news_service.get_fresh_news(driver_path)
        if not all_news:
            print("ℹ️ 발송할 새로운 뉴스가 없어 프로세스를 종료합니다.")
            update_archive_index()
            return
        
        # ✨ AI 선별과 브리핑은 별도의 AIService 인스턴스를 통해 처리
        top_news = ai_service_main.select_top_news(all_news, previous_top_news)

        if not top_news:
            print("ℹ️ AI가 뉴스를 선별하지 못했습니다.")
            return
        
        ai_briefing_md = ai_service_main.generate_briefing(top_news)
        ai_briefing_html = markdown_to_html(ai_briefing_md)
        
        if price_chart_result: price_indicators['price_chart_b64'] = price_chart_result['base64']
        weather_dashboard_b64 = weather_result['base64'] if weather_result else None
        
        web_news_list = []
        for news in top_news:
            news_copy = news.copy()
            if news_copy.get('image_data'):
                news_copy['image_src'] = f"data:image/jpeg;base64,{base64.b64encode(news_copy['image_data']).decode('utf-8')}"
            web_news_list.append(news_copy)

        context = {
            "today_date": today_str, "ai_briefing": ai_briefing_html,
            "risk_briefing_html": risk_briefing_html,
            "price_indicators": price_indicators, "news_list": web_news_list,
            "weather_dashboard_b64": weather_dashboard_b64,
            "has_weather_dashboard": True if weather_dashboard_b64 else False
        }
        web_html = render_html_template(context, target='web')
        archive_filepath = f"archive/{today_str}.html"
        with open(archive_filepath, 'w', encoding='utf-8') as f: f.write(web_html)
        print(f"✅ 웹페이지 버전을 '{archive_filepath}'에 저장했습니다.")

        for i, news_item in enumerate(top_news):
            if news_item.get('image_data'): news_item['image_cid'] = f'news_image_{i}'
        
        context['news_list'] = top_news
        email_body = render_html_template(context, target='email')
        email_subject = f"[{today_str}] 오늘의 화물/물류 뉴스"
        
        images_to_embed = []
        if price_chart_result and price_chart_result.get('filepath'):
            images_to_embed.append({'path': price_chart_result['filepath'], 'cid': 'price_chart'})
        if weather_result and weather_result.get('filepath'):
            images_to_embed.append({'path': weather_result['filepath'], 'cid': 'weather_dashboard'})
        for news_item in top_news:
            if news_item.get('image_data') and news_item.get('image_cid'):
                images_to_embed.append({'data': news_item['image_data'], 'cid': news_item['image_cid']})
        
        email_service.send_email(email_subject, email_body, images_to_embed)
        
        news_service.update_sent_links_log(top_news)
        save_newsletter_history(top_news)
        update_archive_index()

        print("\n🎉 모든 프로세스가 성공적으로 완료되었습니다.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"🔥 치명적인 오류 발생: {e.__class__.__name__}: {e}")

def main_for_risk_briefing_test():
    """뉴스 수집을 건너뛰고 '글로벌 물류 리스크 브리핑' 기능만 테스트하는 함수"""
    print("🚀 물류 리스크 브리핑 기능 테스트를 시작합니다.")
    try:
        # 1. 필요한 서비스 객체들 생성
        config = Config()
        email_service = EmailService(config)
        ai_service = AIService(config)
        
        # ✨ 테스트 대상인 RiskBriefingService 임포트 및 생성
        from risk_briefing_service import RiskBriefingService
        risk_briefing_service = RiskBriefingService()
        
        today_str = get_kst_today_str()

        # 2. 리스크 이벤트 수집 및 AI 브리핑 생성 (테스트 핵심 로직)
        risk_events = risk_briefing_service.generate_risk_events()
        risk_briefing_md = ai_service.generate_risk_briefing(risk_events)
        risk_briefing_html = markdown_to_html(risk_briefing_md) if risk_briefing_md else "<i>(AI 리스크 브리핑 생성에 실패했거나, 해당 기간에 리스크가 없습니다.)</i>"

        # 3. 이메일 템플릿에 전달할 context 준비 (나머지는 빈 데이터)
        context = {
            "today_date": today_str,
            "ai_briefing": "<i>(뉴스 브리핑은 테스트에서 생략됩니다.)</i>",
            "risk_briefing_html": risk_briefing_html,
            "price_indicators": {}, # 빈 데이터
            "news_list": [], # 빈 리스트
            "weather_dashboard_b64": None,
            "has_weather_dashboard": False
        }
        
        # 4. 이메일 본문 생성 및 발송
        email_body = render_html_template(context, target='email')
        email_subject = f"[{today_str}] 🗓️ 글로벌 물류 리스크 브리핑 기능 테스트"
        
        email_service.send_email(email_subject, email_body)
        
        print("\n🎉 리스크 브리핑 테스트 이메일 발송이 성공적으로 완료되었습니다.")

    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"🔥 테스트 중 치명적인 오류 발생: {e.__class__.__name__}: {e}")

def main_for_test():
    """뉴스 수집을 건너뛰고 날씨/데이터 지표 기능만 테스트하는 함수"""
    print("🚀 뉴스레터 기능 테스트를 시작합니다 (날씨 + 데이터 지표).")
    try:
        config = Config()
        email_service = EmailService(config)
        weather_service = WeatherService(config)

        os.makedirs('archive', exist_ok=True)
        os.makedirs('images', exist_ok=True)
        today_str = get_kst_today_str()

        # --- 1. 데이터 및 이미지 생성 ---
        price_indicators = get_price_indicators(config)
        weather_result = weather_service.create_dashboard_image(today_str)
        
        price_chart_result = None
        if price_indicators.get("seven_day_data"):
            price_chart_result = create_price_trend_chart(price_indicators["seven_day_data"], today_str)

        # --- 2. 뉴스/AI 관련 부분은 테스트용 빈 데이터로 설정 ---
        top_news = []
        ai_briefing_html = "<i>(AI 브리핑 및 뉴스 목록은 테스트에서 생략됩니다.)</i>"
        
        # --- 3. 템플릿용 데이터 준비 ---
        if price_chart_result:
            price_indicators['price_chart_b64'] = price_chart_result['base64']
        
        weather_dashboard_b64 = weather_result['base64'] if weather_result else None
        
        context = {
            "today_date": today_str, "ai_briefing": ai_briefing_html,
            "price_indicators": price_indicators, "news_list": top_news,
            "weather_dashboard_b64": weather_dashboard_b64,
            "has_weather_dashboard": True if weather_dashboard_b64 else False
        }
        
        # --- 4. 웹/이메일용 HTML 생성 및 저장 ---
        web_html = render_html_template(context, target='web')
        email_body = render_html_template(context, target='email')
        
        archive_filepath = f"archive/{today_str}.html"
        with open(archive_filepath, 'w', encoding='utf-8') as f:
            f.write(web_html)
        print(f"✅ 웹페이지 버전을 '{archive_filepath}'에 저장했습니다.")
        
        # --- 5. 이메일 발송 ---
        email_subject = f"[{today_str}] 📊 데이터 기능 테스트"
        images_to_embed = []
        if price_chart_result and price_chart_result.get('filepath'):
            images_to_embed.append({'path': price_chart_result['filepath'], 'cid': 'price_chart'})
        if weather_result and weather_result.get('filepath'):
            images_to_embed.append({'path': weather_result['filepath'], 'cid': 'weather_dashboard'})

        email_service.send_email(email_subject, email_body, images_to_embed)
        
        update_archive_index()
        
        print("\n🎉 테스트 이메일 발송이 성공적으로 완료되었습니다.")

    except Exception as e:
        print(f"🔥 테스트 중 치명적인 오류 발생: {e.__class__.__name__}: {e}")

if __name__ == "__main__":
     main()
     #main_for_test()
     #main_for_risk_briefing_test()
     

