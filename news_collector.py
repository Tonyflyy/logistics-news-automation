# main.py
import os, base64, markdown, json, time, random, re, logging, feedparser
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from urllib.parse import urlparse, urljoin
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed

# 서드파티 라이브러리
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader
from PIL import Image
from zoneinfo import ZoneInfo
from newspaper import Article, ArticleException

# 구글 인증 관련
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import google.generativeai as genai

from config import Config

# --- 로깅 설정 ---
def setup_logging():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

# --- 유틸리티 함수 ---
def get_kst_today_str():
    return datetime.now(ZoneInfo('Asia/Seoul')).strftime("%Y-%m-%d")

def markdown_to_html(text):
    return markdown.markdown(text) if text else ""

# --- 핵심 기능 클래스 ---
class NewsScraper:
    def __init__(self, config):
        self.config = config
        self.session = self._create_session()

    def _create_session(self):
        session = requests.Session()
        retry = Retry(total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504])
        adapter = HTTPAdapter(max_retries=retry)
        session.mount('https://', adapter)
        session.mount('http://', adapter)
        return session

    def get_image_url(self, soup, article_url):
        try:
            meta_image = soup.find("meta", property="og:image") or soup.find("meta", name="twitter:image")
            if meta_image and meta_image.get("content"):
                meta_url = self._resolve_url(article_url, meta_image["content"])
                if self._validate_image(meta_url): return meta_url

            for tag in soup.select('figure > img, picture > img', limit=5):
                img_url = tag.get('src') or tag.get('data-src') or (tag.get('srcset').split(',')[0].strip().split(' ')[0] if tag.get('srcset') else None)
                if img_url:
                    full_url = self._resolve_url(article_url, img_url)
                    if self._validate_image(full_url): return full_url
            
            for img in soup.find_all("img", limit=10):
                img_url = img.get("src") or img.get("data-src")
                if img_url:
                    full_url = self._resolve_url(article_url, img_url)
                    if self._validate_image(full_url): return full_url
            
            return self.config.DEFAULT_IMAGE_URL
        except Exception:
            return self.config.DEFAULT_IMAGE_URL

    def _resolve_url(self, base_url, image_url):
        if image_url.startswith('//'): return 'https:' + image_url
        return urljoin(base_url, image_url)

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
                return True
        except Exception:
            return False

class AIService:
    # (이전과 동일, 변경 없음)
    def __init__(self, config):
        self.config = config
        if not self.config.GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY 환경 변수가 설정되지 않았습니다.")
        genai.configure(api_key=self.config.GEMINI_API_KEY)
        self.model = genai.GenerativeModel(self.config.GEMINI_MODEL)

    def generate_single_summary(self, article_title: str, article_text: str) -> str:
        logging.info(f"    -> AI 요약 생성 요청: {article_title}")
        if not article_text or len(article_text) < 100:
            logging.warning("      -> ⚠️ 텍스트가 너무 짧아 요약을 건너<binary data, 2 bytes><binary data, 2 bytes>니다.")
            return "요약 정보를 생성할 수 없습니다."
        try:
            prompt = f"당신은 핵심만 간결하게 전달하는 뉴스 에디터입니다. 아래 제목과 본문을 가진 뉴스 기사의 내용을 독자들이 이해하기 쉽게 3줄로 요약해주세요.\n\n[제목]: {article_title}\n[본문]:\n{article_text[:2000]}"
            response = self.model.generate_content(prompt)
            logging.info(f"      -> ✅ AI 요약 생성 성공.")
            return response.text.strip()
        except Exception:
            logging.error("      -> 🚨 AI 요약 API 호출 중 예외 발생", exc_info=True)
            return "AI 요약 중 오류가 발생했습니다."

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
                logging.warning(f"AI 생성 실패 (시도 {attempt + 1}/3): {e}")
                time.sleep(2 ** attempt)
        return None

    def select_top_news(self, news_list):
        logging.info(f"AI 뉴스 선별 시작... (대상: {len(news_list)}개)")
        context = "\n\n".join([f"기사 #{i}\n제목: {news['title']}\n요약: {news['summary']}" for i, news in enumerate(news_list)])
        prompt = f"당신은 대한민국 최고의 물류 전문 뉴스 편집장입니다. 당신의 임무는 독자에게 가장 가치 있는 정보만을 제공하는 것입니다. 아래 뉴스 목록을 분석하여 다음 두 가지 작업을 순서대로 수행해주세요. 작업 1: 주제별 그룹화 및 대표 기사 선정 - 내용이 사실상 동일한 뉴스들을 하나의 그룹으로 묶으세요. (예: 동일한 사건, 발표, 인물 인터뷰 등) - 각 그룹에서 제목이 가장 구체적이고 요약 정보가 풍부한 기사를 **단 하나만** 대표로 선정하세요. - **하나의 동일한 사건에 대해서는 반드시 단 하나의 대표 기사만 최종 후보가 될 수 있습니다.** 작업 2: 최종 Top 10 선정 - 대표 기사로 선정된 후보들 중에서, 시장 동향, 기술 혁신, 주요 기업 소식을 종합적으로 고려하여 가장 중요도가 높은 순서대로 최종 10개를 선정해주세요. [뉴스 목록]\n{context}\n\n[출력 형식] - 반드시 JSON 형식으로만 응답해야 합니다. - 'selected_indices' 키에 당신이 최종 선정한 기사 10개의 번호(인덱스)를 숫자 배열로 담아주세요. 예: {{\"selected_indices\": [3, 15, 4, 8, 22, 1, 30, 11, 19, 5]}}"
        response_text = self._generate_content_with_retry(prompt, is_json=True)
        if response_text:
            try:
                selected_indices = json.loads(response_text).get('selected_indices', [])
                top_news = [news_list[i] for i in selected_indices if i < len(news_list)]
                logging.info(f"✅ AI가 {len(top_news)}개 뉴스를 선별했습니다.")
                return top_news
            except (json.JSONDecodeError, KeyError) as e:
                logging.error(f"❌ AI 응답 파싱 실패: {e}. 상위 10개 뉴스를 임의로 선택합니다.")
        return news_list[:10]

    def generate_briefing(self, news_list):
        logging.info("AI 브리핑 생성 시작...")
        context = "\n\n".join([f"제목: {news['title']}\n요약: {news.get('ai_summary') or news.get('summary')}" for news in news_list])
        prompt = f"당신은 탁월한 통찰력을 가진 IT/경제 뉴스 큐레이터입니다. 아래 뉴스 목록을 분석하여, 독자를 위한 매우 간결하고 읽기 쉬운 '데일리 브리핑'을 작성해주세요. **출력 형식 규칙:** 1. '에디터 브리핑'은 '## 에디터 브리핑' 헤더로 시작하며, 오늘 뉴스의 핵심을 2~3 문장으로 요약합니다. 2. '주요 뉴스 분석'은 '## 주요 뉴스 분석' 헤더로 시작합니다. 3. 주요 뉴스 분석에서는 가장 중요한 뉴스 카테고리 2~3개를 '###' 헤더로 구분합니다. 4. 각 카테고리 안에서는, 관련된 여러 뉴스를 하나의 간결한 문장으로 요약하고 글머리 기호(`*`)를 사용합니다. 5. 문장 안에서 강조하고 싶은 특정 키워드는 큰따옴표(\" \")로 묶어주세요. [오늘의 뉴스 목록]\n{context}"
        briefing = self._generate_content_with_retry(prompt)
        if briefing: logging.info("✅ AI 브리핑 생성 성공!")
        else: logging.warning("⚠️ AI 브리핑 생성 실패.")
        return briefing

class NewsService:
    def __init__(self, config, scraper, ai_service):
        self.config = config
        self.scraper = scraper
        self.ai_service = ai_service
        self.sent_links = self._load_sent_links()

    def _load_sent_links(self):
        try:
            with open(self.config.SENT_LINKS_FILE, 'r', encoding='utf-8') as f:
                return set(line.strip() for line in f)
        except FileNotFoundError:
            return set()

    def get_fresh_news(self):
        try:
            all_articles = self._fetch_articles_from_rss_feeds()
            logging.info(f"총 {len(all_articles)}개의 후보 기사를 수집했습니다.")

            # 이미 보낸 링크와 중복 링크를 1차로 필터링
            unique_articles = []
            seen_links = set()
            for article in all_articles:
                if article['link'] not in self.sent_links and article['link'] not in seen_links:
                    seen_links.add(article['link'])
                    unique_articles.append(article)
            
            logging.info(f"중복 제외 후 {len(unique_articles)}개의 기사를 처리합니다.")

            # 병렬 처리를 통해 각 기사의 상세 정보(본문, 이미지)를 가져옴
            processed_articles = []
            with ThreadPoolExecutor(max_workers=10) as executor:
                future_to_article = {executor.submit(self._process_single_article, article): article for article in unique_articles}
                for future in as_completed(future_to_article):
                    result = future.result()
                    if result:
                        processed_articles.append(result)
            
            logging.info(f"✅ 총 {len(processed_articles)}개의 유효한 새 뉴스를 처리했습니다.")
            return processed_articles
        except Exception as e:
            logging.error("❌ 뉴스 수집 중 심각한 오류 발생:", exc_info=True)
            return []
    
    def _fetch_articles_from_rss_feeds(self):
        """RSS 피드 목록을 순회하며 모든 기사 항목을 가져옵니다."""
        all_entries = []
        for feed_url in self.config.RSS_FEEDS:
            try:
                logging.info(f"-> RSS 피드 수집 중: {feed_url}")
                feed = feedparser.parse(feed_url, agent=random.choice(self.config.USER_AGENTS))
                cutoff_date = datetime.now(ZoneInfo('UTC')) - timedelta(days=2)
                
                for entry in feed.entries:
                    published_time = datetime(*entry.published_parsed[:6], tzinfo=ZoneInfo('UTC')) if hasattr(entry, 'published_parsed') else datetime.now(ZoneInfo('UTC'))
                    if published_time > cutoff_date:
                        all_entries.append({
                            'title': entry.title,
                            'link': entry.link,
                            'summary': BeautifulSoup(entry.summary, 'lxml').get_text(strip=True) if hasattr(entry, 'summary') else ""
                        })
            except Exception:
                logging.warning(f"  -> ⚠️ RSS 피드 처리 실패: {feed_url}")
        return all_entries

    def _process_single_article(self, article_info):
        """단일 기사를 받아 키워드 필터링, 본문 및 이미지 추출을 수행합니다."""
        try:
            # 1. 키워드 필터링
            search_text = article_info['title'] + " " + article_info['summary']
            if not any(keyword.lower() in search_text.lower() for keyword in self.config.KEYWORDS):
                return None
            
            logging.info(f"  -> 키워드 일치, 처리 시작: {article_info['title']}")
            
            # 2. newspaper3k를 이용해 본문 및 최종 정보 추출
            article = Article(article_info['link'])
            article.download()
            article.parse()
            
            # 유효하지 않은 기사(내용이 없거나 제목이 없는 경우) 제외
            if not article.text or not article.title: return None

            # 3. 이미지 추출
            # newspaper3k가 파싱한 HTML(soup)을 이미지 스크래퍼에 전달
            image_url = self.scraper.get_image_url(article.soup, article.url)
            
            return {
                'title': article.title,
                'link': article.url,
                'summary': article_info['summary'][:150] + "...",
                'image_url': image_url,
                'full_text': article.text
            }
        except Exception:
            # logging.error(f"  -> 🚨 기사 처리 중 오류: {article_info.get('title')}", exc_info=True)
            return None

    def update_sent_links_log(self, news_list):
        links = [news['link'] for news in news_list]
        try:
            with open(self.config.SENT_LINKS_FILE, 'a', encoding='utf-8') as f:
                for link in links: f.write(link + '\n')
            logging.info(f"✅ {len(links)}개 링크를 발송 기록에 추가했습니다.")
        except Exception as e:
            logging.error("❌ 발송 기록 파일 업데이트 실패:", exc_info=True)

class EmailService:
    # (이전과 동일, 변경 없음)
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
            logging.warning("❌ 수신자 목록이 비어있어 이메일을 발송할 수 없습니다.")
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
            logging.info(f"✅ 이메일 발송 성공! (Message ID: {send_message['id']})")
        except HttpError as error:
            logging.error("❌ 이메일 발송 실패:", exc_info=True)

def main():
    setup_logging()
    logging.info("🚀 뉴스레터 자동 생성 프로세스를 시작합니다.")
    try:
        config = Config()
        news_scraper = NewsScraper(config)
        ai_service = AIService(config)
        news_service = NewsService(config, news_scraper, ai_service)

        # 1. 뉴스 후보 수집 및 상세 정보 처리
        all_news = news_service.get_fresh_news()
        if not all_news:
            logging.info("ℹ️ 발송할 새로운 뉴스가 없습니다. 프로세스를 종료합니다.")
            return

        # 2. AI를 이용해 Top 10 뉴스 선별
        top_10_news_base = ai_service.select_top_news(all_news)
        if not top_10_news_base:
            logging.warning("⚠️ AI가 Top 뉴스를 선별하지 못했습니다.")
            return
            
        # 3. 선별된 Top 10 뉴스의 AI 요약 생성 (API 호출 최소화)
        logging.info(f"✅ AI Top 10 선별 완료. 선별된 {len(top_10_news_base)}개 뉴스의 개별 AI 요약을 시작합니다...")
        for news in top_10_news_base:
            news['ai_summary'] = ai_service.generate_single_summary(news['title'], news['full_text'])

        # 4. 전체 브리핑 생성
        ai_briefing_md = ai_service.generate_briefing(top_10_news_base)
        ai_briefing_html = markdown_to_html(ai_briefing_md)

        # 5. 이메일 발송
        email_service = EmailService(config)
        today_str = get_kst_today_str()
        email_subject = f"[{today_str}] 오늘의 화물/물류 뉴스 Top {len(top_10_news_base)}"
        email_body = email_service.create_email_body(top_10_news_base, ai_briefing_html, today_str)
        email_service.send_email(email_subject, email_body)
        
        # 6. 발송 기록 업데이트
        news_service.update_sent_links_log(top_10_news_base)

        logging.info("🎉 모든 프로세스가 성공적으로 완료되었습니다.")
    except (ValueError, FileNotFoundError) as e:
        logging.critical(f"🚨 설정 또는 파일 오류: {e}")
    except Exception as e:
        logging.critical("🔥 치명적인 오류 발생:", exc_info=True)

if __name__ == "__main__":
    main()
