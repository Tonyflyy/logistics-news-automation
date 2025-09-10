import base64
import markdown
from datetime import datetime
from zoneinfo import ZoneInfo

def get_kst_today_str():
    """현재 KST 날짜를 'YYYY-MM-DD' 형식의 문자열로 반환합니다."""
    return datetime.now(ZoneInfo('Asia/Seoul')).strftime("%Y-%m-%d")

def get_kst_week_str():
    """현재 KST 날짜 기준으로 'YYYY년 MM월 N주차' 형식의 문자열을 반환합니다."""
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    # 해당 월의 1일이 무슨 요일인지 찾아, 그 주의 첫 날(일요일)을 기준으로 주차를 계산
    first_day_of_month = now.replace(day=1)
    week_of_month = (now.day + first_day_of_month.weekday() + 1) // 7 + 1
    return f"{now.year}년 {now.month}월 {week_of_month}주차"

def markdown_to_html(text):
    """Markdown 텍스트를 HTML로 변환합니다."""
    return markdown.markdown(text) if text else ""

def image_to_base64_string(filepath):
    """이미지 파일 경로를 받아 Base64 텍스트 문자열로 변환합니다."""
    try:
        with open(filepath, 'rb') as image_file:
            return base64.b64encode(image_file.read()).decode('utf-8')
    except Exception as e:
        print(f"❌ 이미지를 Base64로 변환 중 오류: {e}")
        return None
