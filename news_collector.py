import os, base64, markdown, json, time, random, re, logging
from datetime import datetime
from email.mime.text import MIMEText
from urllib.parse import urlparse, urljoin
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage

import openai
# 서드파티 라이브러리
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import ssl
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader
from PIL import Image
from zoneinfo import ZoneInfo
from newspaper import Article, ArticleException
from newspaper.article import ArticleDownloadState

# 구글 인증 관련
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import google.generativeai as genai
from config import Config

class CustomHttpAdapter(HTTPAdapter):
    def __init__(self, *args, **kwargs):
        self.ssl_context = ssl.create_default_context()
        self.ssl_context.set_ciphers('DEFAULT@SECLEVEL=1')
        super().__init__(*args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False):
        self.poolmanager = requests.packages.urllib3.poolmanager.PoolManager(
            num_pools=connections, maxsize=maxsize,
            block=block, ssl_context=self.ssl_context)

def setup_logging():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

def get_kst_today_str():
    return datetime.now(ZoneInfo('Asia/Seoul')).strftime("%Y-%m-%d")

def markdown_to_html(text):
    return markdown.markdown(text) if text else ""

class NewsScraper:
    def __init__(self, config):
        self.config = config
        self.session = self._create_session()

    def _create_session(self):
        session = requests.Session()
        session.mount('https://', CustomHttpAdapter())
        return session

    # ⬇️ (수정) 이미지 스크레이핑 로직 전체를 개선합니다.
    def get_image_url(self, article_url: str) -> str:
        logging.info(f" -> 이미지 스크래핑 시작: {article_url[:80]}...")
        try:
            headers = { "User-Agent": random.choice(self.config.USER_AGENTS) }
            response = self.session.get(article_url, headers=headers, timeout=10, allow_redirects=True)
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "lxml")
            
            # 1순위: Open Graph 및 트위터 카드 메타 태그 (가장 정확도가 높음)
            meta_image = soup.find("meta", property="og:image") or soup.find("meta", name="twitter:image")
            if meta_image and meta_image.get("content"):
                meta_url = self._resolve_url(article_url, meta_image["content"])
                if self._is_valid_candidate(meta_url) and self._validate_image(meta_url):
                    logging.info(" -> ✅ 1순위(메타 태그)에서 고화질 이미지 발견!")
                    return meta_url

            # 2순위: 본문 내의 figure 또는 picture 태그 (주로 대표 이미지)
            for tag in soup.select('figure > img, picture > img, .article_photo img, .photo_center img', limit=5):
                img_url = tag.get('src') or tag.get('data-src') or (tag.get('srcset').split(',')[0].strip().split(' ')[0] if tag.get('srcset') else None)
                if img_url and self._is_valid_candidate(img_url):
                    full_url = self._resolve_url(article_url, img_url)
                    if self._validate_image(full_url):
                        logging.info(" -> ✅ 2순위(본문 대표 태그)에서 고화질 이미지 발견!")
                        return full_url
            
            # 3순위: 본문의 모든 img 태그 (가장 마지막 수단)
            for img in soup.find_all("img", limit=10):
                img_url = img.get("src") or img.get("data-src")
                if img_url and self._is_valid_candidate(img_url):
                    full_url = self._resolve_url(article_url, img_url)
                    if self._validate_image(full_url):
                        logging.info(" -> ✅ 3순위(본문 전체)에서 이미지 발견.")
                        return full_url

            logging.warning(f" -> ⚠️ 유효 이미지를 찾지 못함: {article_url[:80]}...")
            return self.config.DEFAULT_IMAGE_URL
        except Exception:
            logging.error(f" -> 🚨 이미지 추출 중 오류 발생: {article_url[:80]}...", exc_info=True)
            return self.config.DEFAULT_IMAGE_URL

    def _resolve_url(self, base_url, image_url):
        if image_url.startswith('//'): return 'https:' + image_url
        return urljoin(base_url, image_url)

    def _is_valid_candidate(self, image_url):
        if 'news.google.com' in image_url or 'lh3.googleusercontent.com' in image_url: return False
        # (수정) 로고나 아이콘 같은 이미지 패턴을 더 적극적으로 필터링
        unwanted_patterns = self.config.UNWANTED_IMAGE_PATTERNS + ['logo', 'icon', 'ci', 'bi', 'symbol', 'banner']
        return not any(pattern in image_url.lower() for pattern in unwanted_patterns)

    def _validate_image(self, image_url):
        """이미지를 직접 다운로드하여 크기와 비율을 검사하는 함수"""
        try:
            response = self.session.get(image_url, stream=True, timeout=5)
            response.raise_for_status()
            
            content_type = response.headers.get('Content-Type', '').lower()
            if 'image' not in content_type: return False
            
            # (추가) 너무 작은 파일은 이미지 처리 없이 바로 건너뛰기 (효율성)
            if 'content-length' in response.headers and int(response.headers['content-length']) < 10000: # 10KB 이하
                return False

            img_data = BytesIO(response.content)
            with Image.open(img_data) as img:
                width, height = img.size
                # (수정) 최소 가로/세로 크기 기준을 높여 작은 썸네일 제외
                if width < self.config.MIN_IMAGE_WIDTH or height < self.config.MIN_IMAGE_HEIGHT:
                    return False
                # (수정) 가로가 더 긴 이미지를 선호하도록 비율 조정 (뉴스 이미지는 보통 가로가 김)
                aspect_ratio = width / height
                if aspect_ratio > 4.0 or aspect_ratio < 0.5: # 너무 길거나 세로로 긴 이미지 제외
                    return False
                if aspect_ratio < 1.2: # 가로가 세로보다 1.2배 이상 길어야 함
                    return False
                return True
        except Exception:
            return False
    # ⬆️ 이미지 스크레이핑 로직 개선 완료

class AIService:
    def __init__(self, config):
        self.config = config
        # OpenAI API 키 유효성 검사
        if not self.config.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY 환경 변수가 설정되지 않았습니다.")
        # OpenAI 클라이언트 초기화
        self.client = openai.OpenAI(api_key=self.config.OPENAI_API_KEY)
    def _call_openai_api(self, system_prompt, user_prompt, is_json=False):
        """OpenAI API를 호출하는 중앙 함수"""
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
        try:
            response_format = {"type": "json_object"} if is_json else {"type": "text"}
            
            response = self.client.chat.completions.create(
                model=self.config.OPENAI_MODEL,
                messages=messages,
                temperature=0.7,
                response_format=response_format
            )
            content = response.choices[0].message.content.strip()
            
            if is_json:
                # JSON 형식인지 다시 한번 확인
                json.loads(content)
            
            return content
        except Exception as e:
            logging.error(f" -> 🚨 OpenAI API 호출 중 예외 발생: {e}", exc_info=True)
            return None    

    def generate_single_summary(self, article_title: str, article_text: str) -> str:
        logging.info(f" -> ChatGPT 요약 생성 요청: {article_title}")
        if not article_text or len(article_text) < 100:
            logging.warning(" -> ⚠️ 텍스트가 너무 짧아 요약을 건너뜁니다.")
            return "요약 정보를 생성할 수 없습니다."
        
        system_prompt = "당신은 핵심만 간결하게 전달하는 뉴스 에디터입니다. 뉴스 기사 내용을 독자들이 이해하기 쉽게 3줄로 요약해주세요."
        user_prompt = f"[제목]: {article_title}\n[본문]:\n{article_text[:2000]}"
        
        summary = self._call_openai_api(system_prompt, user_prompt)
        
        if summary:
            logging.info(" -> ✅ ChatGPT 요약 생성 성공.")
            return summary
        else:
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
        logging.info(f"ChatGPT 뉴스 선별 시작... (대상: {len(news_list)}개)")
        context = "\n\n".join([f"기사 #{i}\n제목: {news['title']}\n요약: {news['summary']}" for i, news in enumerate(news_list)])
        
        system_prompt = """
        당신은 대한민국 최고의 '물류 전문' 뉴스 에디터입니다. 
        당신의 임무는 화물차 운송, 주선, 육상 운송, 공급망 관리(SCM) 분야의 종사자들에게 가장 실용적이고 중요한 최신 정보를 선별하여 제공하는 것입니다.
        아래 뉴스 목록을 분석하여 다음의 엄격한 기준에 따라 최종 Top 10 뉴스를 선정해주세요.

        [선별 기준]
        1.  **핵심 주제 집중:** 반드시 아래 분야와 직접적으로 관련된 뉴스만 선정해야 합니다.
            - 화물 운송 및 트럭킹 동향 (화물차, 운임, 유가 등)
            - 주선사 및 운송사 소식 (M&A, 신규 서비스, 실적 발표 등)
            - 물류 기술(Logi-Tech), 플랫폼, 스타트업 소식
            - 풀필먼트, 창고 자동화, 라스트마일 배송
            - 공급망 관리(SCM) 최신 전략
            - 정부의 물류/운송 관련 정책 및 규제 변경
        2.  **관련성 낮은 주제 제외:** IT, 반도체, 자동차 등 다른 산업 뉴스는 물류와 직접적인 연관성이 언급된 경우에만 포함합니다.
        3.  **해운/항만 뉴스 비중 유지:** 해양, 항만, 선박 관련 뉴스는 여전히 전체 10개 중 **최대 2개까지만** 포함하여 육상 운송 위주의 균형을 맞춰주세요.
        4.  **중복 제거:** 내용이 사실상 동일한 뉴스는 단 하나만 선정합니다. 제목이 가장 구체적이고 정보가 풍부한 기사를 대표로 선택하세요.
        5.  **중요도 순서:** 위 기준을 모두 만족하는 후보들 중에서, 업계 종사자에게 가장 큰 영향을 미칠 수 있는 중요도 순서대로 정렬해주세요.

        [출력 형식]
        - 반드시 JSON 형식으로만 응답해야 합니다.
        - 'selected_indices' 키에 당신이 최종 선정한 기사 10개의 번호(인덱스)를 **중요도 순서대로** 숫자 배열로 담아주세요.
        예: {"selected_indices": [3, 15, 4, 8, 22, 1, 30, 11, 19, 5]}
        """
        user_prompt = f"[뉴스 목록]\n{context}"
        
        response_text = self._call_openai_api(system_prompt, user_prompt, is_json=True)

        if response_text:
            try:
                selected_indices = json.loads(response_text).get('selected_indices', [])
                top_news = [news_list[i] for i in selected_indices if i < len(news_list)]
                logging.info(f"✅ ChatGPT가 {len(top_news)}개 뉴스를 선별했습니다.")
                return top_news
            except (json.JSONDecodeError, KeyError) as e:
                logging.error(f"❌ ChatGPT 응답 파싱 실패: {e}. 상위 10개 뉴스를 임의로 선택합니다.")
        return news_list[:10]

    def generate_briefing(self, news_list):
        logging.info("ChatGPT 브리핑 생성 시작...")
        context = "\n\n".join([f"제목: {news['title']}\n요약: {news.get('ai_summary') or news.get('summary')}" for news in news_list])
        
        system_prompt = """
        당신은 탁월한 통찰력을 가진 물류/경제 뉴스 큐레이터입니다. 아래 뉴스 목록을 분석하여, 독자를 위한 매우 간결하고 읽기 쉬운 '데일리 브리핑'을 마크다운 형식으로 작성해주세요.
        
        **출력 형식 규칙:**
        1. '에디터 브리핑'은 '## 에디터 브리핑' 헤더로 시작하며, 오늘 뉴스의 핵심을 2~3 문장으로 요약합니다.
        2. '주요 뉴스 분석'은 '## 주요 뉴스 분석' 헤더로 시작합니다.
        3. 주요 뉴스 분석에서는 가장 중요한 뉴스 카테고리 2~3개를 '###' 헤더로 구분합니다.
        4. 각 카테고리 안에서는, 관련된 여러 뉴스를 하나의 간결한 문장으로 요약하고 글머리 기호(`*`)를 사용합니다.
        5. 문장 안에서 강조하고 싶은 특정 키워드는 큰따옴표(" ")로 묶어주세요.
        """
        user_prompt = f"[오늘의 뉴스 목록]\n{context}"

        briefing = self._call_openai_api(system_prompt, user_prompt)
        
        if briefing:
            logging.info("✅ ChatGPT 브리핑 생성 성공!")
            return briefing
        else:
            logging.warning("⚠️ ChatGPT 브리핑 생성 실패.")
            return "데일리 브리핑 생성에 실패했습니다."

class NewsService:
    def __init__(self, config, scraper, ai_service):
        self.config = config
        self.scraper = scraper
        self.ai_service = ai_service
        self.sent_links = self._load_sent_links()

    def __del__(self):
        pass

    def _load_sent_links(self):
        try:
            with open(self.config.SENT_LINKS_FILE, 'r', encoding='utf-8') as f:
                links = set(line.strip() for line in f)
                logging.info(f"✅ {len(links)}개 발송 기록 로드 완료.")
                return links
        except FileNotFoundError:
            logging.warning("⚠️ 발송 기록 파일이 없어 새로 시작합니다.")
            return set()

    def _fetch_rss_feeds(self):
        logging.info("🆕 여러 RSS 피드를 수집합니다... (총 {}개 소스)".format(len(self.config.RSS_FEEDS)))
        all_entries = []
        headers = {"User-Agent": random.choice(self.config.USER_AGENTS)}
        for rss_url in self.config.RSS_FEEDS:
            try:
                response = requests.get(rss_url, headers=headers, timeout=15)
                response.raise_for_status()
                soup = BeautifulSoup(response.content, 'xml')
                entries = [{
                    'rss_title': item.title.text if item.title else "",
                    'link': item.link.text if item.link else "",
                    'rss_summary': item.description.text if item.description else ""
                } for item in soup.find_all('item')]
                all_entries.extend(entries)
                logging.info(f"✅ {rss_url}에서 {len(entries)}개 entry 수집 완료.")
            except Exception as e:
                logging.warning(f"⚠️ {rss_url} 수집 실패: {e}")
        logging.info(f"총 {len(all_entries)}개의 후보 기사를 발견했습니다.")
        return all_entries

    def get_fresh_news(self):
        try:
            initial_articles = self._fetch_rss_feeds()
            logging.info(f"총 {len(initial_articles)}개의 새로운 후보 기사를 발견했습니다.")
            processed_articles = []
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_entry = {executor.submit(self._resolve_and_process_article, entry): entry for entry in initial_articles[:self.config.MAX_ARTICLES] if entry['link'] not in self.sent_links}
                for future in as_completed(future_to_entry):
                    article_data = future.result()
                    if article_data:
                        processed_articles.append(article_data)
            logging.info(f"✅ 총 {len(processed_articles)}개의 유효한 새 뉴스를 처리했습니다.")
            return processed_articles
        except Exception:
            logging.error("❌ 뉴스 수집 중 심각한 오류 발생:", exc_info=True)
            return []
            
    def _clean_url(self, url: str) -> str | None:
        try:
            parsed = urlparse(url)
            if any(ad_domain in parsed.netloc for ad_domain in self.config.AD_DOMAINS_BLACKLIST):
                return None
            return parsed._replace(fragment="").geturl()
        except Exception:
            return None

    def _resolve_and_process_article(self, entry):
        logging.info(f"-> URL 처리 시도: {entry['rss_title']}")
        try:
            cleaned_url = self._clean_url(entry['link'])
            if not cleaned_url:
                logging.warning(f" -> ⚠️ 유효하지 않은 URL: {entry['rss_title']}")
                return None
            
            article = Article(cleaned_url, language='ko') 
            article.download()
            article.parse()
            
            if article.meta_lang != 'ko':
                logging.info(f" -> 🌐 한국어 기사가 아니므로 건너뜁니다: (언어: {article.meta_lang}) {article.title}")
                return None

            if not article.text and not article.title:
                logging.warning(f" -> ⚠️ 기사 내용 추출 실패 (403 Forbidden 등): {cleaned_url}")
                return None

            final_title = article.title if article.title else entry['rss_title']
            logging.info(f" -> ✅ [한국어 뉴스] 최종 URL/제목 확보: {final_title}")
            
            final_url = article.url 

            return {
                'title': final_title,
                'link': final_url,
                'url': final_url,
                'summary': BeautifulSoup(entry.get('rss_summary', ''), 'lxml').get_text(strip=True)[:150] + "...",
                'image_url': self.scraper.get_image_url(final_url),
                'full_text': article.text
            }
        except ArticleException as e:
            logging.error(f" -> 🚨 기사 처리 라이브러리 오류: {e}")
            return None
        except Exception:
            logging.error(f" -> 🚨 URL 처리 중 예외 발생: {entry['rss_title']}", exc_info=True)
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
    
    def send_email(self, subject, body_html, news_list):
        if not self.config.RECIPIENT_LIST:
            logging.warning("❌ 수신자 목록이 비어있어 이메일을 발송할 수 없습니다.")
            return
        try:
            service = build('gmail', 'v1', credentials=self.credentials)
            msg = MIMEMultipart('related')
            msg['To'] = ", ".join(self.config.RECIPIENT_LIST)
            msg['From'] = self.config.SENDER_EMAIL
            msg['Subject'] = subject
            msg_alternative = MIMEMultipart('alternative')
            msg_alternative.attach(MIMEText(body_html, 'html', 'utf-8'))
            msg.attach(msg_alternative)

            for news in news_list:
                if news.get('image_data'):
                    image = MIMEImage(news['image_data'])
                    image.add_header('Content-ID', f"<{news['cid']}>")
                    msg.attach(image)

            encoded_message = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            create_message = {'raw': encoded_message}
            
            send_message = service.users().messages().send(userId="me", body=create_message).execute()
            logging.info(f"✅ 이메일 발송 성공! (Message ID: {send_message['id']})")
            
            # ⬇️ (추가) 발송된 뉴스 목록을 로그로 기록합니다.
            logging.info("--- 📧 발송된 뉴스레터 목록 ---")
            for i, news in enumerate(news_list):
                logging.info(f"  {i+1}. {news['title']}")
                logging.info(f"     - 링크: {news['link']}")
            logging.info("-----------------------------")
            # ⬆️ 로그 기록 추가 완료

        except HttpError:
            logging.error("❌ 이메일 발송 실패:", exc_info=True)

def main():
    setup_logging()
    logging.info("🚀 뉴스레터 자동 생성 프로세스를 시작합니다.")
    news_service = None
    try:
        config = Config()
        news_scraper = NewsScraper(config)
        ai_service = AIService(config)
        news_service = NewsService(config, news_scraper, ai_service)
        
        all_news = news_service.get_fresh_news()
        if not all_news:
            logging.info("ℹ️ 발송할 새로운 뉴스가 없습니다. 프로세스를 종료합니다.")
            return
            
        top_10_news_base = ai_service.select_top_news(all_news)
        if not top_10_news_base:
            logging.warning("⚠️ AI가 Top 뉴스를 선별하지 못했습니다.")
            return
            
        logging.info(f"✅ AI Top 10 선별 완료. 선별된 {len(top_10_news_base)}개 뉴스의 개별 AI 요약을 시작합니다...")
        with ThreadPoolExecutor(max_workers=2) as executor:
            future_to_news = {executor.submit(ai_service.generate_single_summary, news['title'], news['full_text']): news for news in top_10_news_base}
            for future in as_completed(future_to_news):
                news = future_to_news[future]
                try:
                    summary = future.result()
                    # ⬇️ (수정) AI 요약 결과가 비정상적일 경우 대체 텍스트를 생성합니다.
                    if "오류" in summary or "생성할 수 없습니다" in summary or len(summary) < 20:
                        logging.warning(f" -> ⚠️ AI 요약 실패, 대체 텍스트를 생성합니다: {news['title']}")
                        # 기사 본문의 첫 200자를 가져와서 문장을 마무리하고 "..."를 붙입니다.
                        clean_text = re.sub(r'\s+', ' ', news['full_text']).strip()
                        end_index = clean_text.find('.', 150) # 150자 근처의 첫 마침표를 찾음
                        if end_index != -1:
                            news['ai_summary'] = clean_text[:end_index+1]
                        else:
                            news['ai_summary'] = clean_text[:200] + "..."
                    else:
                        news['ai_summary'] = summary
                except Exception as e:
                    logging.error(f" -> 🚨 AI 요약 스레드에서 심각한 오류 발생: {e}")
                    news['ai_summary'] = news['summary'] # RSS 요약으로 대체
        # ⬆️ 수정 완료
        
        ai_briefing_md = ai_service.generate_briefing(top_10_news_base)
        ai_briefing_html = markdown_to_html(ai_briefing_md)

        logging.info("📧 이메일 발송을 위해 뉴스 이미지를 다운로드합니다...")
        with ThreadPoolExecutor(max_workers=5) as executor:
            future_to_news = {}
            for i, news in enumerate(top_10_news_base):
                news['cid'] = f"image_{i}_{int(time.time())}"
                if news.get('image_url') and news['image_url'] != config.DEFAULT_IMAGE_URL:
                    future_to_news[executor.submit(news_scraper.session.get, news['image_url'], timeout=10)] = news
                else:
                    news['image_data'] = None

            for future in as_completed(future_to_news):
                news = future_to_news[future]
                try:
                    response = future.result()
                    response.raise_for_status()
                    news['image_data'] = response.content
                    logging.info(f" -> ✅ 이미지 다운로드 성공: {news['title'][:30]}...")
                except Exception as e:
                    news['image_data'] = None
                    logging.warning(f" -> ⚠️ 이미지 다운로드 실패: {news['title'][:30]}... ({e})")

        email_service = EmailService(config)
        today_str = get_kst_today_str()
        email_subject = f"[{today_str}] 오늘의 IT/산업 뉴스 Top {len(top_10_news_base)}"
        
        email_body = email_service.create_email_body(top_10_news_base, ai_briefing_html, today_str)
        email_service.send_email(email_subject, email_body, top_10_news_base)
        
        news_service.update_sent_links_log(top_10_news_base)
        logging.info("🎉 모든 프로세스가 성공적으로 완료되었습니다.")

    except (ValueError, FileNotFoundError) as e:
        logging.critical(f"🚨 설정 또는 파일 오류: {e}")
    except Exception:
        logging.critical("🔥 치명적인 오류 발생:", exc_info=True)
    finally:
        if news_service:
            del news_service

if __name__ == "__main__":
    main()


