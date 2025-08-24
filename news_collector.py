# main.py

import os
import base64
import markdown
import json
import time
import random
from datetime import datetime
from email.mime.text import MIMEText
from urllib.parse import urljoin, urlparse
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from newspaper import Article
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

from config import Config

# --- 유틸리티 함수 ---
def get_kst_today_str():
    return datetime.now(ZoneInfo('Asia/Seoul')).strftime("%Y-%m-%d")

def markdown_to_html(text):
    return markdown.markdown(text) if text else ""

# --- 핵심 기능 클래스 (NewsScraper, AIService, EmailService는 이전과 동일) ---
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

            # 2. 본문 <figure> 또는 <picture> 태그 (byline, klnews 등 최신 사이트 대응)
            for tag in soup.select('figure > img, picture > img', limit=5):
                img_url = tag.get('src') or tag.get('data-src') or (tag.get('srcset').split(',')[0].strip().split(' ')[0] if tag.get('srcset') else None)
                if img_url and self._is_valid_candidate(img_url):
                    full_url = self._resolve_url(article_url, img_url)
                    if self._validate_image(full_url):
                        return full_url
            
            # 3. 일반 <img> 태그 (가장 기본적인 방법)
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
                if aspect_ratio < 1.2: return False
                return True
        except Exception:
            return False

class AIService:
    def generate_single_summary(self, article_title: str, article_link: str) -> str | None:
        """기사 제목과 원문 링크를 바탕으로 3줄 요약을 생성합니다."""
        try:
            # newspaper3k 라이브러리를 이용해 기사 본문을 추출
            article = Article(article_link)
            article.download()
            article.parse()
            
            # 본문이 너무 짧으면 요약하지 않음
            if len(article.text) < 100:
                return "요약 정보를 생성할 수 없습니다."
            
            prompt = f"""
            당신은 핵심만 간결하게 전달하는 뉴스 에디터입니다.
            아래 제목과 본문을 가진 뉴스 기사의 내용을 독자들이 이해하기 쉽게 3줄로 요약해주세요.
            
            [제목]: {article_title}
            [본문]:
            {article.text[:1500]} 
            """ # (토큰 사용량을 줄이기 위해 본문 앞 1500자만 사용)

            response = self.model.generate_content(prompt)
            return response.text

        except Exception as e:
            print(f"  ㄴ> ❌ AI 요약 생성 실패: {e.__class__.__name__}")
            return None
    # (변경 없음)
    def __init__(self, config):
        self.config = config
        if not self.config.GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY 환경 변수가 설정되지 않았습니다.")
        genai.configure(api_key=self.config.GEMINI_API_KEY)
        self.model = genai.GenerativeModel(self.config.GEMINI_MODEL)

    def _generate_content_with_retry(self, prompt, is_json=False):
        for attempt in range(3):
            try:
                response = self.model.generate_content(prompt)
                if is_json:
                    cleaned_text = response.text.strip().replace("```json", "").replace("```", "")
                    json.loads(cleaned_text)
                    return cleaned_text
                return response.text
            except Exception as e:
                print(f"AI 생성 실패 (시도 {attempt + 1}/3): {e}")
                time.sleep(2 ** attempt)
        return None

    def select_top_news(self, news_list):
        print(f"AI 뉴스 선별 시작... (대상: {len(news_list)}개)")
        
        # 변경점: AI에게 제목뿐만 아니라 '요약' 내용도 함께 전달
        context = "\n\n".join(
            [f"기사 #{i}\n제목: {news['title']}\n요약: {news['summary']}" for i, news in enumerate(news_list)]
        )
        
        # 변경점: 중복 제거 지시를 훨씬 더 강화하고 명확하게 변경
        prompt = f"""
        당신은 대한민국 최고의 물류 전문 뉴스 편집장입니다. 당신의 임무는 독자에게 가장 가치 있는 정보만을 제공하는 것입니다.
        아래 뉴스 목록을 분석하여 다음 두 가지 작업을 순서대로 수행해주세요.

        작업 1: 주제별 그룹화 및 대표 기사 선정
        - 내용이 사실상 동일한 뉴스들을 하나의 그룹으로 묶으세요. (예: 동일한 사건, 발표, 인물 인터뷰 등)
        - 각 그룹에서 제목이 가장 구체적이고 요약 정보가 풍부한 기사를 **단 하나만** 대표로 선정하세요.
        - **하나의 동일한 사건에 대해서는 반드시 단 하나의 대표 기사만 최종 후보가 될 수 있습니다.**

        작업 2: 최종 Top 10 선정
        - 대표 기사로 선정된 후보들 중에서, 시장 동향, 기술 혁신, 주요 기업 소식을 종합적으로 고려하여 가장 중요도가 높은 순서대로 최종 10개를 선정해주세요.

        [뉴스 목록]
        {context}

        [출력 형식]
        - 반드시 JSON 형식으로만 응답해야 합니다.
        - 'selected_indices' 키에 당신이 최종 선정한 기사 10개의 번호(인덱스)를 숫자 배열로 담아주세요.
        - 예: {{"selected_indices": [3, 15, 4, 8, 22, 1, 30, 11, 19, 5]}}
        """
        
        response_text = self._generate_content_with_retry(prompt, is_json=True)
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
        print("AI 브리핑 생성 시작...")
        context = "\n\n".join([f"제목: {news['title']}\n요약: {news['summary']}" for news in news_list])
        prompt = f"""당신은 탁월한 통찰력을 가진 IT/경제 뉴스 큐레이터입니다. 아래 뉴스 목록을 분석하여, 독자를 위한 매우 간결하고 읽기 쉬운 '데일리 브리핑'을 작성해주세요. **출력 형식 규칙:** 1. '에디터 브리핑'은 '## 에디터 브리핑' 헤더로 시작하며, 오늘 뉴스의 핵심을 2~3 문장으로 요약합니다. 2. '주요 뉴스 분석'은 '## 주요 뉴스 분석' 헤더로 시작합니다. 3. 주요 뉴스 분석에서는 가장 중요한 뉴스 카테고리 2~3개를 '###' 헤더로 구분합니다. 4. 각 카테고리 안에서는, 관련된 여러 뉴스를 하나의 간결한 문장으로 요약하고 글머리 기호(`*`)를 사용합니다. 5. 문장 안에서 강조하고 싶은 특정 키워드는 큰따옴표(" ")로 묶어주세요. [오늘의 뉴스 목록]\n{context}"""
        briefing = self._generate_content_with_retry(prompt)
        if briefing: print("✅ AI 브리핑 생성 성공!")
        return briefing

# ⬇️⬇️⬇️ 최종 로직이 적용된 NewsService 클래스 ⬇️⬇️⬇️
class NewsService:
    def __init__(self, config, scraper,ai_service):
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

    def get_fresh_news(self):
        print("최신 뉴스 수집을 시작합니다...")
        client = GoogleNews(lang='ko', country='KR')
        query = ' OR '.join(self.config.KEYWORDS) + ' -해운 -항공'
        search_results = client.search(query, when=f'{self.config.NEWS_FETCH_HOURS}h')
        
        all_articles = [entry for entry in search_results['entries'] if entry.get('link')]
        unique_articles = list({article['link']: article for article in all_articles}.values())
        print(f"총 {len(unique_articles)}개의 새로운 후보 기사를 발견했습니다.")

        processed_news = []
        with ThreadPoolExecutor(max_workers=4) as executor:
            future_to_entry = {executor.submit(self._resolve_and_process_entry, entry): entry for entry in unique_articles[:30]}
            for future in as_completed(future_to_entry):
                result = future.result()
                if result:
                    processed_news.append(result)

        print(f"✅ 총 {len(processed_news)}개 기사 원본 URL 추출 및 처리 완료.")
        
        final_news = []
        seen_urls = set()
        for news in processed_news:
            if news['link'] not in self.sent_links and news['link'] not in seen_urls:
                seen_urls.add(news['link'])
                final_news.append(news)
        
        print(f"✅ 총 {len(final_news)}개의 유효한 새 뉴스를 처리했습니다.")
        return final_news

    def _clean_and_validate_url(self, url: str) -> str | None:
        try:
            parsed = urlparse(url)
            if any(ad_domain in parsed.netloc for ad_domain in self.config.AD_DOMAINS_BLACKLIST):
                return None
            
            # 홈페이지 URL 필터링: 경로가 없거나 매우 짧으면(5글자 이하) 제외
            if not parsed.path or len(parsed.path) <= 5:
                # 단, 허용 목록에 있는 도메인은 예외 (예: hyundai.co.kr)
                if not any(allowed in parsed.netloc for allowed in ['hyundai.co.kr']):
                    print(f"  ㄴ> 🗑️ 홈페이지 링크 제외: {url[:80]}...")
                    return None
            
            cleaned_url = parsed._replace(fragment="").geturl()
            return cleaned_url
        except Exception:
            return None

    def _resolve_and_process_entry(self, entry):
        """드라이버 생성부터 URL 추출, 뉴스 처리 및 AI 요약까지 한 번에 수행합니다."""
        driver = None
        try:
            driver = self._create_stealth_driver()
            if not driver: return None

            driver.get(entry['link'])
            wait = WebDriverWait(driver, 10)
            link_element = wait.until(EC.presence_of_element_located((By.TAG_NAME, 'a')))
            original_url = link_element.get_attribute('href')
            validated_url = self._clean_and_validate_url(original_url)
            if not validated_url:
                return None

            print(f"  -> ✅ URL 처리 성공: {entry['title']}")

            # ⬇️⬇️⬇️ AI 요약 기능 호출 추가 ⬇️⬇️⬇️
            # AIService 객체를 직접 생성하거나 main에서 넘겨받아야 합니다.
            # 이 구조에서는 main 함수에서 NewsService를 생성할 때 AIService 객체를 넘겨주는 것이 좋습니다.
            # 하지만 간단한 수정을 위해, 이 함수 내에서 임시로 생성하도록 하겠습니다.
            # (더 나은 구조는 main.py 전체 코드를 다시 구성할 때 반영할 수 있습니다.)
            ai_summary = self.ai_service.generate_single_summary(entry['title'], validated_url)

            scraped_summary = BeautifulSoup(entry.get('summary', ''), 'lxml').get_text(strip=True)
            
            return {
                'title': entry['title'],
                'link': validated_url, 
                'url': validated_url,
                'summary': scraped_summary[:150] + '...' if scraped_summary else "", # 기존 요약은 만약을 위해 유지
                'ai_summary': ai_summary, # 새로 추가된 AI 요약
                'image_url': self.scraper.get_image_url(validated_url)
            }
        except Exception:
            return None
        finally:
            if driver:
                driver.quit()

    def update_sent_links_log(self, news_list):
        links = [news['link'] for news in news_list]
        try:
            with open(self.config.SENT_LINKS_FILE, 'a', encoding='utf-8') as f:
                for link in links: f.write(link + '\n')
            print(f"✅ {len(links)}개 링크를 발송 기록에 추가했습니다.")
        except Exception as e:
            print(f"❌ 발송 기록 파일 업데이트 실패: {e}")

class EmailService:
    # (변경 없음)
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

    def create_email_body(self, news_list, ai_briefing_html, today_date_str):
        env = Environment(loader=FileSystemLoader('.'))
        template = env.get_template('email_template.html')
        return template.render(news_list=news_list, today_date=today_date_str, ai_briefing=ai_briefing_html)

    def send_email(self, subject, body):
        if not self.config.RECIPIENT_LIST:
            print("❌ 수신자 목록이 비어있어 이메일을 발송할 수 없습니다.")
            return
        try:
            service = build('gmail', 'v1', credentials=self.credentials)
            message = MIMEText(body, 'html', 'utf-8')
            message['To'] = ", ".join(self.config.RECIPIENT_LIST)
            message['From'] = self.config.SENDER_EMAIL
            message['Subject'] = subject
            encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
            create_message = {'raw': encoded_message}
            send_message = service.users().messages().send(userId="me", body=create_message).execute()
            print(f"✅ 이메일 발송 성공! (Message ID: {send_message['id']})")
        except HttpError as error:
            print(f"❌ 이메일 발송 실패: {error}")

def main():
    # (변경 없음)
    print("🚀 뉴스레터 자동 생성 프로세스를 시작합니다.")
    news_service = None # finally 블록에서 사용하기 위해 미리 선언
    try:
        config = Config()
        news_scraper = NewsScraper(config)
        ai_service = AIService(config)
        news_service = NewsService(config, news_scraper, ai_service)
        email_service = EmailService(config)

        all_news = news_service.get_fresh_news()
        if not all_news:
            print("ℹ️ 발송할 새로운 뉴스가 없습니다. 프로세스를 종료합니다.")
            return

        top_news = ai_service.select_top_news(all_news)
        if not top_news:
            print("ℹ️ AI가 뉴스를 선별하지 못했습니다. 프로세스를 종료합니다.")
            return

        ai_briefing_md = ai_service.generate_briefing(top_news)
        ai_briefing_html = markdown_to_html(ai_briefing_md)

        today_str = get_kst_today_str()
        email_subject = f"[{today_str}] 오늘의 화물/물류 뉴스 Top {len(top_news)}"
        email_body = email_service.create_email_body(top_news, ai_briefing_html, today_str)
        
        email_service.send_email(email_subject, email_body)
        news_service.update_sent_links_log(top_news)

        print("🎉 모든 프로세스가 성공적으로 완료되었습니다.")
    except (ValueError, FileNotFoundError) as e:
        print(f"🚨 설정 또는 파일 오류: {e}")
    except Exception as e:
        print(f"🔥 치명적인 오류 발생: {e}")
    finally:
        # 프로그램이 어떻게 종료되든 (성공, 실패, 예외) 항상 브라우저 드라이버를 확실하게 종료
        if news_service:
            del news_service

if __name__ == "__main__":
    main()
