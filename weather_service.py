# weather_service.py (재시도 로직 추가된 최종 완성본)

import os
import requests
import platform
import time # ⬅️ time 라이브러리 추가
from utils import image_to_base64_string
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from collections import defaultdict
from config import Config
from PIL import Image, ImageDraw, ImageFont

class WeatherService:
    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.short_term_url = "http://apis.data.go.kr/1360000/VilageFcstInfoService_2.0/getVilageFcst"
        self.mid_term_temp_url = "http://apis.data.go.kr/1360000/MidFcstInfoService/getMidTa"
        self.mid_term_land_url = "http://apis.data.go.kr/1360000/MidFcstInfoService/getMidLandFcst"

    
    def get_weekly_weather_risks(self):
        """
        7일간의 날씨 예보 데이터에서 물류 리스크(태풍, 폭설 등)를 찾아 리스트로 반환합니다.
        """
        # 1. 기존의 데이터 수집 함수를 그대로 사용합니다.
        daily_forecast = self._get_weather_forecast()
        if not daily_forecast:
            return []

        # 2. 기존의 리스크 분석 함수를 재사용하여 '위험', '주의' 수준과 리스크 텍스트를 가져옵니다.
        analyzed_forecast = self._analyze_weather_risk(daily_forecast)

        risks = []
        risk_keywords = ["태풍", "폭설", "호우", "강풍", "풍랑"]

        for date_str, regions in analyzed_forecast.items():
            current_date = datetime.strptime(date_str, "%Y%m%d").date()
            for location, weather_data in regions.items():
                risk_text = weather_data.get('risk_text', '')
                for keyword in risk_keywords:
                    if keyword in risk_text:
                        risks.append({"date": current_date, "location": location, "event": keyword})
        
        # 중복 리스크를 제거하고 반환합니다.
        unique_risks = list({(r['date'], r['event'], r['location']): r for r in risks}.values())
        return unique_risks
    

    def create_dashboard_image(self, today_str):
        """날씨 데이터로 대시보드 이미지를 생성하고, 파일 경로와 Base64 문자열을 딕셔너리로 반환합니다."""
        # 1. 날짜가 포함된 고유한 파일명 생성
        filename = f"images/weather_dashboard_{today_str}.png"
        
        try:
            # 2. 날씨 데이터 수집 및 분석
            weather_data = self._get_weather_forecast()
            if not weather_data:
                print("⚠️ 날씨 데이터를 수집하지 못해 대시보드 생성을 건너뜁니다.")
                return None
            
            analyzed_data = self._analyze_weather_risk(weather_data)

            # 3. Pillow를 사용하여 이미지 그리기
            print("\n--- 🖼️ 대시보드 이미지 생성 시작 ---")
            img_width, cell_height, top_margin = 1000, 100, 130 
            img_height = top_margin + (cell_height * len(self.config.LOGISTICS_HUBS))
            
            font_path = self._get_font_path()
            title_font = ImageFont.truetype(font_path, 32)
            header_font = ImageFont.truetype(font_path, 18)
            temp_font = ImageFont.truetype(font_path, 16)
            risk_text_font = ImageFont.truetype(font_path, 14)

            image = Image.new('RGB', (img_width, img_height), '#F9FAFB')
            draw = ImageDraw.Draw(image)
            
            days = sorted(analyzed_data.keys())
            if not days:
                print("⚠️ 분석된 날씨 데이터가 없어 대시보드 생성을 중단합니다.")
                return None

            start_date = datetime.strptime(days[0], "%Y%m%d").strftime("%m/%d")
            end_date = datetime.strptime(days[-1], "%Y%m%d").strftime("%m/%d")
            draw.text((50, 30), f"권역별 주간 날씨 체크 ({start_date} ~ {end_date})", font=title_font, fill='#111827')

            short_base_date, short_base_time = self._get_short_term_base_datetime()
            update_time_str = f"업데이트: {short_base_date[4:6]}/{short_base_date[6:8]} {short_base_time[:2]}:{short_base_time[2:]} 기준"
            update_text_width = draw.textlength(update_time_str, font=temp_font)
            draw.text((img_width - update_text_width - 50, 45), update_time_str, font=temp_font, fill='#6B7280')

            regions = list(self.config.LOGISTICS_HUBS.keys())
            cell_width = (img_width - 100) / len(days)
            weekdays = ['월', '화', '수', '목', '금', '토', '일']

            for i, day in enumerate(days):
                dt = datetime.strptime(day, "%Y%m%d")
                x = 100 + (i * cell_width)
                header_text = f"{dt.strftime('%m/%d')}({weekdays[dt.weekday()]})"
                text_width = draw.textlength(header_text, font=header_font)
                draw.text((x + cell_width/2 - text_width/2, top_margin - 50), header_text, font=header_font, fill='#374151')

            for j, region in enumerate(regions):
                y = top_margin + (j * cell_height)
                text_width = draw.textlength(region, font=header_font)
                draw.text((50 - text_width/2 if 50 - text_width/2 > 0 else 5, y + cell_height/2 - 10), region, font=header_font, fill='#1F2937')
                for i, day in enumerate(days):
                    x = 100 + (i * cell_width)
                    data = analyzed_data.get(day, {}).get(region)
                    if data and data.get('min_temp'):
                        risk_level = data.get('risk_level', '안전')
                        risk_color = {'안전': '#FFFFFF', '주의': '#FFFBEB', '위험': '#FEF2F2'}.get(risk_level, '#FFFFFF')
                        draw.rectangle([x, y, x + cell_width, y + cell_height], fill=risk_color, outline='#E5E7EB')
                        weather_icon = self._get_weather_icon(data.get('icon_code', 'sunny'))
                        if weather_icon: image.paste(weather_icon, (int(x + cell_width/2 - 20), int(y + 15)), weather_icon)
                        min_t, max_t = data.get('min_temp', '-'), data.get('max_temp', '-')
                        temp_text = f"{max_t}° / {min_t}°"
                        text_width = draw.textlength(temp_text, font=temp_font)
                        draw.text((x + cell_width/2 - text_width/2, y + 60), temp_text, font=temp_font, fill='#4B5563')
                        risk_text = data.get('risk_text', '')
                        if risk_text and risk_text not in ["비", "눈"]:
                            text_width = draw.textlength(risk_text, font=risk_text_font)
                            text_color = {'주의': '#D97706', '위험': '#DC2626'}.get(risk_level)
                            draw.text((x + cell_width/2 - text_width/2, y + 80), risk_text, font=risk_text_font, fill=text_color)
                    else:
                        draw.rectangle([x, y, x + cell_width, y + cell_height], fill='#F3F4F6', outline='#E5E7EB')
                        text_width = draw.textlength("정보 없음", font=header_font)
                        draw.text((x + cell_width/2 - text_width/2, y + cell_height/2 - 10), "정보 없음", font=header_font, fill='#9CA3AF')
            
            # 4. 이미지 파일로 저장 (이메일 첨부용)
            image.save(filename)
            print(f"✅ 7일 예보 대시보드 이미지 '{filename}' 저장 완료!")

            # 5. 저장된 파일을 Base64로 변환 (웹페이지 삽입용)
            base64_image = image_to_base64_string(filename)
            
            # 6. 최종 결과인 딕셔너리 반환
            return {"filepath": filename, "base64": base64_image}

        except Exception as e:
            print(f"❌ 날씨 대시보드 이미지 생성 실패: {e}")
            return None
            return None

    def _get_weather_forecast(self):
        print("\n--- ☀️ 7일 날씨 데이터 수집 및 가공 시작 ---")
        short_base_date, short_base_time = self._get_short_term_base_datetime()
        mid_base_datetime = self._get_mid_term_base_datetime()

        all_regions_forecast = {}
        for region_name, hub_info in self.config.LOGISTICS_HUBS.items():
            print(f"-> {region_name} 날씨 정보 처리 중...")
            try:
                short_term_raw = self._fetch_short_term_forecast(hub_info, short_base_date, short_base_time)
                mid_term_temp_raw = self._fetch_mid_term_temp_forecast(hub_info, mid_base_datetime)
                mid_term_land_raw = self._fetch_mid_term_land_forecast(hub_info, mid_base_datetime)

                if short_term_raw or mid_term_temp_raw or mid_term_land_raw:
                    parsed_data = self._parse_forecast_data(short_term_raw, mid_term_temp_raw, mid_term_land_raw)
                    all_regions_forecast[region_name] = parsed_data
                else:
                    print(f"⚠️ {region_name}의 데이터를 가져오지 못했습니다.")
            except Exception as e:
                print(f"❌ {region_name} 처리 중 예외 발생: {e}")
        
        return self._restructure_by_date(all_regions_forecast)

    def _parse_forecast_data(self, short_term, mid_term_temp, mid_term_land):
        # (이전과 동일한 코드)
        forecast = defaultdict(lambda: defaultdict(str))
        daily_temps = defaultdict(list)
        if short_term:
            for item in short_term:
                fcst_date = item['fcstDate']
                category = item['category']
                value = item['fcstValue']
                if category == 'TMP': daily_temps[fcst_date].append(float(value))
                elif category == 'PTY': forecast[fcst_date]['pty'] = int(value)
                elif category == 'SKY': forecast[fcst_date]['sky'] = int(value)
                elif category == 'WSD':
                    current_wsd = float(forecast[fcst_date].get('wsd', 0))
                    forecast[fcst_date]['wsd'] = max(current_wsd, float(value))
            for day, temps in daily_temps.items():
                if temps:
                    forecast[day]['min_temp'] = f"{min(temps):.0f}"
                    forecast[day]['max_temp'] = f"{max(temps):.0f}"
        if mid_term_temp and isinstance(mid_term_temp, list) and mid_term_land and isinstance(mid_term_land, list):
            temp_item = mid_term_temp[0]
            land_item = mid_term_land[0]
            for i in range(3, 8):
                date_str = (datetime.now(ZoneInfo('Asia/Seoul')) + timedelta(days=i)).strftime('%Y%m%d')
                min_key, max_key = f'taMin{i}', f'taMax{i}'
                if min_key in temp_item and max_key in temp_item:
                    forecast[date_str]['min_temp'] = str(temp_item[min_key])
                    forecast[date_str]['max_temp'] = str(temp_item[max_key])
                am_key, pm_key = f'wf{i}Am', f'wf{i}Pm'
                if am_key in land_item and pm_key in land_item:
                    am_fcst, pm_fcst = land_item[am_key], land_item[pm_key]
                    forecast[date_str]['condition'] = am_fcst if am_fcst == pm_fcst else f"{am_fcst}/{pm_fcst}"
        return dict(forecast)

    def _restructure_by_date(self, all_regions_forecast):
        # (이전과 동일한 코드)
        daily_data = defaultdict(dict)
        for region, forecast_by_date in all_regions_forecast.items():
            for date, weather_info in forecast_by_date.items():
                daily_data[date][region] = weather_info
        return dict(sorted(daily_data.items()))

    def _analyze_weather_risk(self, daily_forecast):
        # (이전과 동일한 코드)
        for date, regions in daily_forecast.items():
            for region, weather in regions.items():
                pty = weather.get('pty', 0)
                wsd = weather.get('wsd', 0.0)
                condition = weather.get('condition', '')
                risk_level, risk_text, icon_code = '안전', '', 'sunny'
                if pty == 0:
                    sky = weather.get('sky', 0)
                    if sky == 1: icon_code = 'sunny'
                    elif sky in [3, 4]: icon_code = 'cloudy'
                elif pty in [1, 2, 5]: risk_level, risk_text, icon_code = '주의', '비', 'rain'
                elif pty in [3, 6, 7]: risk_level, risk_text, icon_code = '위험', '눈', 'snow'
                if not pty and not weather.get('sky'):
                    if "맑음" in condition: icon_code = 'sunny'
                    elif any(s in condition for s in ["구름많음", "흐림"]): icon_code = 'cloudy'
                    elif "비" in condition: risk_level, risk_text, icon_code = '주의', '비', 'rain'
                    elif "눈" in condition: risk_level, risk_text, icon_code = '위험', '눈', 'snow'
                if wsd >= 9.0:
                    risk_level = '위험'
                    risk_text = f"{risk_text},강풍" if risk_text else "강풍"
                weather['risk_level'] = risk_level
                weather['risk_text'] = risk_text.strip(',')
                weather['icon_code'] = icon_code
        return daily_forecast

    def _draw_dashboard_image(self, analyzed_data):
        # (이전과 동일한, 가운데 정렬된 코드)
        try:
            print("\n--- 🖼️ 대시보드 이미지 생성 시작 ---")
            img_width, cell_height, top_margin = 1000, 100, 130 
            img_height = top_margin + (cell_height * len(self.config.LOGISTICS_HUBS))
            font_path = self._get_font_path()
            title_font = ImageFont.truetype(font_path, 32)
            header_font = ImageFont.truetype(font_path, 18)
            temp_font = ImageFont.truetype(font_path, 16)
            risk_text_font = ImageFont.truetype(font_path, 14)
            image = Image.new('RGB', (img_width, img_height), '#F9FAFB')
            draw = ImageDraw.Draw(image)
            days = [(datetime.now(ZoneInfo('Asia/Seoul')) + timedelta(days=i)).strftime('%Y%m%d') for i in range(7)]
            start_date = datetime.strptime(days[0], "%Y%m%d").strftime("%m/%d")
            end_date = datetime.strptime(days[-1], "%Y%m%d").strftime("%m/%d")
            draw.text((50, 30), f"권역별 주간 날씨 체크 ({start_date} ~ {end_date})", font=title_font, fill='#111827')
            short_base_date, short_base_time = self._get_short_term_base_datetime()
            update_time_str = f"업데이트: {short_base_date[4:6]}/{short_base_date[6:8]} {short_base_time[:2]}:{short_base_time[2:]} 기준"
            update_text_width = draw.textlength(update_time_str, font=temp_font)
            draw.text((img_width - update_text_width - 50, 45), update_time_str, font=temp_font, fill='#6B7280')
            regions = list(self.config.LOGISTICS_HUBS.keys())
            cell_width = (img_width - 100) / len(days)
            weekdays = ['월', '화', '수', '목', '금', '토', '일']
            for i, day in enumerate(days):
                dt = datetime.strptime(day, "%Y%m%d")
                x = 100 + (i * cell_width)
                header_text = f"{dt.strftime('%m/%d')}({weekdays[dt.weekday()]})"
                text_width = draw.textlength(header_text, font=header_font)
                draw.text((x + cell_width/2 - text_width/2, top_margin - 50), header_text, font=header_font, fill='#374151')
            for j, region in enumerate(regions):
                y = top_margin + (j * cell_height)
                text_width = draw.textlength(region, font=header_font)
                draw.text((50 - text_width/2, y + cell_height/2 - 10), region, font=header_font, fill='#1F2937')
                for i, day in enumerate(days):
                    x = 100 + (i * cell_width)
                    data = analyzed_data.get(day, {}).get(region)
                    if data and data.get('min_temp'):
                        risk_level = data.get('risk_level', '안전')
                        risk_color = {'안전': '#FFFFFF', '주의': '#FFFBEB', '위험': '#FEF2F2'}.get(risk_level, '#FFFFFF')
                        draw.rectangle([x, y, x + cell_width, y + cell_height], fill=risk_color, outline='#E5E7EB')
                        weather_icon = self._get_weather_icon(data.get('icon_code', 'sunny'))
                        if weather_icon: image.paste(weather_icon, (int(x + cell_width/2 - 20), int(y + 15)), weather_icon)
                        min_t, max_t = data.get('min_temp', '-'), data.get('max_temp', '-')
                        temp_text = f"{max_t}° / {min_t}°"
                        text_width = draw.textlength(temp_text, font=temp_font)
                        draw.text((x + cell_width/2 - text_width/2, y + 60), temp_text, font=temp_font, fill='#4B5563')
                        risk_text = data.get('risk_text', '')
                        if risk_text and risk_text not in ["비", "눈"]:
                            text_width = draw.textlength(risk_text, font=risk_text_font)
                            text_color = {'주의': '#D97706', '위험': '#DC2626'}.get(risk_level)
                            draw.text((x + cell_width/2 - text_width/2, y + 80), risk_text, font=risk_text_font, fill=text_color)
                    else:
                        draw.rectangle([x, y, x + cell_width, y + cell_height], fill='#F3F4F6', outline='#E5E7EB')
                        text_width = draw.textlength("정보 없음", font=header_font)
                        draw.text((x + cell_width/2 - text_width/2, y + cell_height/2 - 10), "정보 없음", font=header_font, fill='#9CA3AF')
            filename = "weather_dashboard.png"
            image.save(filename)
            print(f"✅ 7일 예보 대시보드 이미지 '{filename}' 저장 완료!")
            return filename
        except Exception as e:
            print(f"❌ 이미지 생성 중 오류 발생: {e}")
            return None
    
    def _get_short_term_base_datetime(self):
        # (이전과 동일한 안정화된 코드)
        now = datetime.now(ZoneInfo('Asia/Seoul'))
        target_time = now - timedelta(hours=2)
        base_times = ["0200", "0500", "0800", "1100", "1400", "1700", "2000", "2300"]
        if target_time.hour < 2:
            base_date, base_time = (target_time - timedelta(days=1)).strftime('%Y%m%d'), "2300"
        else:
            base_date = target_time.strftime('%Y%m%d')
            base_time = max(bt for bt in base_times if int(bt[:2]) <= target_time.hour)
        print(f"   ㄴ 단기예보 기준시각: {base_date} {base_time}")
        return base_date, base_time
        
    def _get_mid_term_base_datetime(self):
        # (이전과 동일한 코드)
        now = datetime.now(ZoneInfo('Asia/Seoul'))
        if now.hour < 6: base_time = (now - timedelta(days=1)).strftime('%Y%m%d') + "1800"
        elif now.hour < 18: base_time = now.strftime('%Y%m%d') + "0600"
        else: base_time = now.strftime('%Y%m%d') + "1800"
        return base_time

    def _fetch_api(self, url, params):
        """ ✨ 재시도 로직과 URL 인코딩 문제를 해결한 최종 API 호출 함수 """
        for attempt in range(3): # 최대 3번 재시도
            try:
                service_key = params.pop('serviceKey')
                query_string = requests.models.urlencode(params)
                request_url = f"{url}?serviceKey={service_key}&{query_string}"
                
                response = self.session.get(request_url, timeout=15)
                response.raise_for_status()
                
                # serviceKey를 다시 채워넣어 다음 재시도에 사용될 수 있도록 함
                params['serviceKey'] = service_key

                data = response.json()
                if data.get('response', {}).get('header', {}).get('resultCode') == '00':
                    return data.get('response', {}).get('body', {}).get('items', {}).get('item')
                else:
                    error_msg = data.get('response', {}).get('header', {}).get('resultMsg', 'Unknown Error')
                    print(f"   ㄴ API 오류 (시도 {attempt+1}/3): {error_msg}")
            
            except requests.exceptions.RequestException as e:
                print(f"   ㄴ 요청 또는 파싱 오류 (시도 {attempt+1}/3): {e}")
                if 'response' in locals():
                    print(f"   ㄴ 서버 실제 응답: {response.text}")

            # 실패 시 대기
            if attempt < 2:
                sleep_time = (attempt + 1) * 2 # 2초, 4초 대기
                print(f"   ... {sleep_time}초 후 재시도합니다 ...")
                time.sleep(sleep_time)
        
        return None # 3번 모두 실패하면 None 반환

    # (이하 _fetch_... , _get_weather_icon, _get_font_path 함수는 이전 최종본과 동일)
    def _fetch_short_term_forecast(self, hub_info, base_date, base_time):
        params = {'serviceKey': self.config.WEATHER_API_KEY, 'dataType': 'JSON', 'numOfRows': '1000', 'base_date': base_date, 'base_time': base_time, 'nx': str(hub_info['nx']), 'ny': str(hub_info['ny'])}
        return self._fetch_api(self.short_term_url, params)

    def _fetch_mid_term_temp_forecast(self, hub_info, base_datetime):
        params = {'serviceKey': self.config.WEATHER_API_KEY, 'dataType': 'JSON', 'regId': hub_info['regId_temp'], 'tmFc': base_datetime}
        return self._fetch_api(self.mid_term_temp_url, params)

    def _fetch_mid_term_land_forecast(self, hub_info, base_datetime):
        params = {'serviceKey': self.config.WEATHER_API_KEY, 'dataType': 'JSON', 'regId': hub_info['regId_land'], 'tmFc': base_datetime}
        return self._fetch_api(self.mid_term_land_url, params)

    def _get_weather_icon(self, icon_code):
        icon_map = {'sunny': 'assets/sunny.png', 'cloudy': 'assets/cloudy.png', 'rain': 'assets/rain.png', 'snow': 'assets/snow.png'}
        path = icon_map.get(icon_code, 'assets/sunny.png')
        if not os.path.exists(path):
            print(f"⚠️ 아이콘 파일 '{path}'을 찾을 수 없습니다.")
            return None
        return Image.open(path).convert("RGBA").resize((40, 40))

    def _get_font_path(self):
        local_font_path = "assets/NanumGothicBold.ttf"
        if os.path.exists(local_font_path): return local_font_path
        system_name = platform.system()
        if system_name == 'Windows': return 'malgun.ttf'
        elif system_name == 'Darwin': return '/System/Library/Fonts/AppleSDGothicNeo.ttc'
        else:
            if os.path.exists('/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf'):
                return '/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf'
        raise FileNotFoundError("적절한 한글 폰트를 찾을 수 없습니다. 'assets' 폴더에 NanumGothicBold.ttf를 넣어주세요.")

if __name__ == '__main__':
    config = Config()
    weather_service = WeatherService(config)
    weather_service.create_dashboard_image()
