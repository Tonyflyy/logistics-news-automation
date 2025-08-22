import os
import base64
import markdown
import json
import feedparser
import smtplib
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart 
from urllib.parse import urljoin, urlparse, quote
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import google.generativeai as genai
from bs4 import BeautifulSoup
from jinja2 import Environment, FileSystemLoader
from zoneinfo import ZoneInfo
import requests

def generate_ai_briefing(news_list):
    print("AI 서식화 브리핑 생성을 시작합니다...")
    try:
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            print("GEMINI_API_KEY가 설정되지 않았습니다.")
            return None
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash-latest')
        news_context = ""
        for news in news_list:
            news_context += f"제목: {news['title']}\n요약: {news['summary']}\n\n"
        prompt = f"""
        당신은 탁월한 통찰력을 가진 IT/경제 뉴스 큐레이터입니다.
        아래 뉴스 목록을 분석하여, 독자를 위한 매우 간결하고 읽기 쉬운 '데일리 브리핑'을 작성해주세요.

        **출력 형식 규칙:**
        1. '에디터 브리핑'은 '## 에디터 브리핑' 헤더로 시작하며, 오늘 뉴스의 핵심을 2~3 문장으로 요약합니다.
        2. '주요 뉴스 분석'은 '## 주요 뉴스 분석' 헤더로 시작합니다.
        3. 주요 뉴스 분석에서는 가장 중요한 뉴스 카테고리 2~3개를 '###' 헤더로 구분합니다.
        4. 각 카테고리 안에서는, 관련된 여러 뉴스를 **하나의 간결한 문장으로 요약**하고 글머리 기호(`*`)를 사용합니다.
        5. 문장 안에서 강조하고 싶은 특정 키워드는 굵은 글씨 대신 **큰따옴표(" ")**로 묶어주세요.

        [오늘의 뉴스 목록]
        {news_context}
        """
        response = model.generate_content(prompt)
        print("AI 서식화 브리핑 생성 성공!")
        return response.text
    except Exception as e:
        print(f"AI 브리핑 생성 중 오류 발생: {e}")
        return None
    
def get_image_from_url(page_url):
    """
    웹 페이지에서 대표 이미지 URL을 추출하고, 유효하며 의미있는 이미지인지 검사합니다.
    """
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'}
        response = requests.get(page_url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        og_image = soup.find('meta', property='og:image')
        
        if og_image and og_image.get('content'):
            image_url = og_image['content']
            
            if image_url.startswith('/'):
                parsed_uri = urlparse(page_url)
                base_url = f"{parsed_uri.scheme}://{parsed_uri.netloc}"
                image_url = urljoin(base_url, image_url)

            # --- 이 부분이 더 강력한 필터로 수정되었습니다 ---
            try:
                # URL을 파싱하여 도메인(netloc)을 직접 확인합니다.
                parsed_url = urlparse(image_url)
                if 'googleusercontent.com' in parsed_url.netloc:
                    print(f"❌ 알려진 기본 이미지이므로 제외합니다: {image_url}")
                    return None # 이미지가 없는 것으로 처리합니다.
            except Exception as e:
                print(f"URL 파싱 중 오류 발생: {e}") # 혹시 모를 이상한 URL 형식에 대비
            # --- 여기까지 ---

            # 이미지 유효성 검사 단계
            try:
                image_res = requests.head(image_url, timeout=5, allow_redirects=True, headers=headers)
                if image_res.status_code == 200 and 'image' in image_res.headers.get('Content-Type', '').lower():
                    print(f"✅ 이미지 유효성 검사 성공: {image_url}")
                    return image_url
                else:
                    print(f"❌ 이미지 유효성 검사 실패 (Status: {image_res.status_code}, Type: {image_res.headers.get('Content-Type')}): {image_url}")
            except Exception as e:
                print(f"이미지 유효성 검사 중 네트워크 오류: {e}")
            
    except Exception as e:
        print(f"이미지 URL 추출을 위한 페이지 로딩 중 오류 발생 (URL: {page_url}): {e}")
        
    return None

def update_sent_links(links):
    try:
        with open('sent_links_logistics.txt', 'a', encoding='utf-8') as f:
            for link in links:
                f.write(link + '\n')
        print(f"{len(links)}개의 새 링크를 sent_links_logistics.txt에 추가했습니다.")
    except Exception as e:
        print(f"sent_links_logistics.txt 파일 업데이트 중 오류 발생: {e}")

# --- AI 뉴스 선별 함수 ---
def select_top_news_with_ai(news_list):
    print(f"AI 뉴스 큐레이션을 시작합니다... (대상: {len(news_list)}개)")
    try:
        api_key = os.getenv('GEMINI_API_KEY')
        if not api_key:
            print("GEMINI_API_KEY가 설정되지 않았습니다.")
            return news_list[:10]
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-1.5-flash-latest')
        news_context_for_selection = ""
        for i, news in enumerate(news_list):
            news_context_for_selection += f"기사 #{i}:\n제목: {news['title']}\n요약: {news['summary']}\n\n"

        prompt = f"""
        당신은 한국의 화물/물류 분야를 다루는 매우 꼼꼼한 뉴스 편집장입니다.
        아래는 오늘 수집된 뉴스 기사 목록입니다. 각 기사에는 고유한 번호(#)가 있습니다.

        당신의 임무는 다음 3단계에 따라 독자에게 제공할 최종 뉴스 10개를 선별하는 것입니다.

        **작업 순서:**
        1. **주제 그룹화:** 내용이 거의 동일한 뉴스(예: 같은 사건을 다른 언론사가 보도한 기사)들을 하나의 그룹으로 묶습니다.
        2. **대표 기사 선택:** 각 그룹에서 제목과 요약이 가장 구체적이고 정보량이 많은 대표 기사를 하나씩만 선택합니다.
        3. **최종 10개 선별:** 이렇게 그룹별로 추려진 대표 기사들 중에서, 시장 동향, 기술 혁신 등을 종합적으로 고려하여 가장 중요한 최종 10개를 선별해주세요.

        **출력 형식 규칙:**
        - 반드시 JSON 형식으로 응답해야 합니다.
        - JSON 객체는 'top_10_indices'라는 키를 가져야 합니다.
        - 'top_10_indices'의 값은 당신이 선택한 최종 기사 10개의 '번호'를 담은 숫자 배열(array)이어야 합니다. 예: [3, 15, 4, ...].

        [오늘의 뉴스 목록]
        {news_context_for_selection}
        """
        
        response = model.generate_content(prompt)
        json_response_text = response.text.strip().replace("```json", "").replace("```", "")
        selected_data = json.loads(json_response_text)
        selected_indices = selected_data.get('top_10_indices', [])
        top_10_news = [news_list[i] for i in selected_indices if i < len(news_list)]
        print(f"AI가 {len(top_10_news)}개의 Top 뉴스를 선별했습니다.")
        return top_10_news
    except Exception as e:
        print(f"AI 뉴스 큐레이션 중 오류 발생: {e}")
        return news_list[:10]
    
# --- 뉴스 수집 함수 (RSS 방식) ---
def get_news_from_rss():
    sent_links = set()
    try:
        with open('sent_links_logistics.txt', 'r', encoding='utf-8') as f:
            sent_links = set(line.strip() for line in f)
        print(f"총 {len(sent_links)}개의 보낸 기록을 sent_links_logistics.txt에서 불러왔습니다.")
    except FileNotFoundError:
        print("sent_links_logistics.txt 파일을 찾을 수 없어, 새로운 기록을 시작합니다.")

    cutoff_datetime = datetime.now(timezone.utc) - timedelta(hours=48)
    
    rss_feeds = [
        # 물류/교통
        'https://www.klnews.co.kr/rss/S1N1.xml', 'http://www.ksg.co.kr/rss/S1N1.xml',
        'http://www.gyotongn.com/rss/S1N2.xml', 'https://www.cargonews.co.kr/rss/S1N1.xml',
        # IT/기술
        'https://www.etnews.com/rss/all.xml', 'http://www.ddaily.co.kr/rss.xml',
        'https://www.zdnet.co.kr/rss/all.xml', 'https://www.bloter.net/rss',
        # 경제
        'https://www.hankyung.com/feed/economy', 'https://www.mk.co.kr/rss/all.xml',
        'https://rss.mt.co.kr/mt_all.xml', 'https://www.fnnews.com/rss/fn_realnews_all.xml',
        # 종합
        'https://www.chosun.com/arc/outboundfeeds/rss/?outputType=xml',
        'https://rss.joins.com/joins_news_list.xml', 'https://rss.donga.com/total.xml',
        # 정부
        'https://www.molit.go.kr/USR/RSS/m_294_rss.jsp'
    ]

    # --- 이 부분이 새로 추가되었습니다: 제외할 키워드 ---
    excluded_keywords = ['항공', '공항', '해운', '선박', '항만', '선사', '해상', '컨테이너선','비행기','배']
    # --- 여기까지 ---
    
    naver_rss_feeds = [
        'https://news.google.com/rss/search?q=화물+OR+물류+OR+티맵&hl=ko&gl=KR&ceid=KR:ko', # 구글 뉴스
        'https://news.naver.com/main/rss.naver?sid1=105',  # 네이버 IT/과학
        'https://news.naver.com/main/rss.naver?sid1=101',  # 네이버 경제
        'http://media.daum.net/rss/part/primary/digital.xml', # 다음 IT/과학
        'http://media.daum.net/rss/part/primary/economic.xml', # 다음 경제
    ]

    keywords = [
        '화물', '물류', '티맵', '티맵화물', '화물운송', '육상운송', 
        '화물차', '트럭', '화물정보망', '택배차', '화물차 운전',
        '물류 스타트업', '화물 플랫폼', '내륙 운송', '도로 화물',
        '스마트물류', '물류센터', '풀필먼트', '콜드체인', '라스트마일',
        '운송', '배송', '택배'
    ]
    
    found_news = []
    unique_links = set()

    # 1단계: 키워드로 우선순위 뉴스 검색
    print("1단계: 키워드 기반으로 우선순위 뉴스를 검색합니다...")
    all_feeds = rss_feeds + naver_rss_feeds
    for url in all_feeds:
        try:
            feed = feedparser.parse(url, agent='Mozilla/5.0')
            for entry in feed.entries:
                 # --- 시간 필터링 로직 추가 ---
                published_time = datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=timezone.utc)
                if published_time < cutoff_datetime:
                    continue # 48시간보다 오래된 기사는 건너뜀
                # --- 여기까지 ---
                if entry.link in sent_links or entry.link in unique_links:
                    continue
                
                summary_html = entry.get('summary', '요약 없음')
                soup = BeautifulSoup(summary_html, 'lxml')
                summary_text = soup.get_text(strip=True)
                search_text = entry.title + " " + summary_text

                # --- 제외 키워드 필터링 로직 추가 ---
                is_excluded = False
                for ex_keyword in excluded_keywords:
                    if ex_keyword in search_text:
                        is_excluded = True
                        break
                if is_excluded:
                    continue # 제외 키워드가 있으면 이 기사는 건너뜀
                # --- 여기까지 ---
                
                for keyword in keywords:
                    if keyword.lower() in search_text.lower():
                        image_url = get_image_from_url(entry.link)
                        news_item = {
                            'title': entry.title,
                            'link': entry.link,
                            'summary': summary_text[:150] + '...',
                            'image_url': image_url
                        }
                        found_news.append(news_item)
                        unique_links.add(entry.link)
                        break
        except Exception as e:
            print(f"'{url}' 처리 중 오류 발생: {e}")

    # 2단계: 10개가 안되면 수량 채우기
    if len(found_news) < 10:
        print(f"1단계 결과 {len(found_news)}개의 뉴스를 찾았습니다. 10개를 채우기 위해 2단계 검색을 시작합니다...")
        needed = 10 - len(found_news)
        for url in naver_rss_feeds: # 신뢰도 높은 네이버 뉴스로만 채우기
            if needed <= 0: break
            try:
                feed = feedparser.parse(url, agent='Mozilla/5.0')
                for entry in feed.entries:
                    # --- 시간 필터링 로직 추가 ---
                    published_time = datetime.fromtimestamp(time.mktime(entry.published_parsed), tz=timezone.utc)
                    if published_time < cutoff_datetime:
                        continue
                    # --- 여기까지 ---
                    
                    if needed <= 0: break
                    if entry.link in sent_links or entry.link in unique_links:
                        continue
                    
                    summary_html = entry.get('summary', '요약 없음')
                    soup = BeautifulSoup(summary_html, 'lxml')
                    summary_text = soup.get_text(strip=True)
                    image_url = get_image_from_url(entry.link)
                    news_item = {
                        'title': entry.title,
                        'link': entry.link,
                        'summary': summary_text[:150] + '...',
                        'image_url': image_url
                    }
                    found_news.append(news_item)
                    unique_links.add(entry.link)
                    needed -= 1
            except Exception as e:
                print(f"'{url}' 처리 중 오류 발생: {e}")

    print(f"총 {len(found_news)}개의 새로운 뉴스를 찾았습니다.")
    return found_news


def create_email_html(news_list, ai_briefing, today_date_str):
    env = Environment(loader=FileSystemLoader('.'))
    template = env.get_template('email_template.html')
    return template.render(news_list=news_list, today_date=today_date_str, ai_briefing=ai_briefing)

def send_email_smtp(sender_email, receiver_emails, password, subject, body):
    # 확정된 SMTP 서버 정보
    SMTP_SERVER = "mail.ylp.co.kr"
    SMTP_PORT = 465

    msg = MIMEMultipart()
    msg['From'] = sender_email
    msg['To'] = ", ".join(receiver_emails)
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html', 'utf-8'))

    try:
        # SSL을 사용하는 SMTP 서버에 연결
        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
        server.login(sender_email, password)
        server.sendmail(sender_email, receiver_emails, msg.as_string())
        server.quit()
        print(f"SMTP 이메일 발송 성공! (수신자: {', '.join(receiver_emails)})")
    except Exception as e:
        print(f"SMTP 이메일 발송 중 오류 발생: {e}")

def send_email_oauth(sender_email, receiver_emails, subject, body):
    SCOPES = ['https://www.googleapis.com/auth/gmail.send']
    creds = None
    if os.path.exists('token.json'):
        creds = Credentials.from_authorized_user_file('token.json', SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file('credentials.json', SCOPES)
            creds = flow.run_local_server(port=0)
        with open('token.json', 'w') as token:
            token.write(creds.to_json())
    try:
        service = build('gmail', 'v1', credentials=creds)
        message = MIMEText(body, 'html')
        message['To'] = ", ".join(receiver_emails)
        message['From'] = sender_email
        message['Subject'] = subject
        encoded_message = base64.urlsafe_b64encode(message.as_bytes()).decode()
        create_message = {'raw': encoded_message}
        send_message = (service.users().messages().send(userId="me", body=create_message).execute())
        print(f"메시지 ID: {send_message['id']} 이메일 발송 성공! (수신자: {', '.join(receiver_emails)})")
    except HttpError as error:
        print(f"이메일 발송 중 오류 발생: {error}")

# --- 메인 실행 부분 ---
if __name__ == "__main__":
    recipients_str = os.getenv('RECIPIENT_LIST')
    if not recipients_str:
        print("수신자 목록(RECIPIENT_LIST)이 설정되지 않았습니다.")
        exit()
    recipient_list = [email.strip() for email in recipients_str.split(',')]
    
    # 발신자 이메일 주소 (Gmail 계정)
    SENDER_EMAIL = "zzzfbwnsgh@gmail.com"
    
    utc_now = datetime.now(ZoneInfo('UTC'))
    kst_now = utc_now.astimezone(ZoneInfo('Asia/Seoul'))
    kst_today_str = kst_now.strftime("%Y-%m-%d")

    all_news_data = get_news_from_rss()
    
    if all_news_data:
        top_news_data = select_top_news_with_ai(all_news_data)
        if top_news_data:
            ai_briefing_markdown = generate_ai_briefing(top_news_data)
            ai_briefing_html = markdown.markdown(ai_briefing_markdown) if ai_briefing_markdown else None
            
            email_body = create_email_html(top_news_data, ai_briefing_html, kst_today_str)
            email_subject = f"[{kst_today_str}] 오늘의 화물/물류 뉴스 Top {len(top_news_data)}"
            
            # OAuth 방식의 이메일 발송 함수 호출
            send_email_oauth(SENDER_EMAIL, recipient_list, email_subject, email_body)
            
            new_links_to_save = [news['link'] for news in top_news_data]
            update_sent_links(new_links_to_save)
        else:
            print("AI가 Top 뉴스를 선별하지 못했습니다.")
    else:
        print("발송할 새로운 뉴스가 없습니다.")




