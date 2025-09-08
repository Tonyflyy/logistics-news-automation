import base64
import markdown
from datetime import datetime
from zoneinfo import ZoneInfo

def get_kst_today_str():
    """현재 KST 날짜를 'YYYY-MM-DD' 형식의 문자열로 반환합니다."""
    return datetime.now(ZoneInfo('Asia/Seoul')).strftime("%Y-%m-%d")

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