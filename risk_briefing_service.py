# risk_briefing_service.py

from datetime import date, timedelta
import holidays
# ✨ [수정] relativedelta에서 요일(TH)을 직접 임포트합니다.
from dateutil.relativedelta import relativedelta, TH, MO 
from config import Config
from weather_service import WeatherService

class RiskBriefingService:
    def __init__(self):
        self.config = Config()
        self.countries = self.config.RISK_BRIEFING_TARGET_COUNTRIES
        self.country_names = {"KR": "한국", "CN": "중국", "US": "미국", "VN": "베트남", "DE": "독일"}

    def _get_holidays(self, start_date, end_date):
        """
        (개선) 지정된 기간 내의 공휴일 정보를 더 안정적인 방식으로 수집하고, 한글로 번역합니다.
        """
        holiday_events = []
        
        # ✨ [핵심 수정] 날짜를 하루씩 순회하며 공휴일 여부를 직접 확인하는 방식으로 변경
        total_days = (end_date - start_date).days + 1
        date_range = [start_date + timedelta(days=i) for i in range(total_days)]

        for country_code in self.countries:
            try:
                if country_code == 'KR':
                    country_holidays = holidays.country_holidays(country_code, language='ko')
                else:
                    country_holidays = holidays.country_holidays(country_code)

                for single_date in date_range:
                    if single_date in country_holidays:
                        holiday_name = country_holidays.get(single_date)
                        translated_name = self.config.HOLIDAY_NAME_TRANSLATIONS.get(holiday_name, holiday_name)
                        
                        holiday_events.append({
                            "date": single_date,
                            "country": self.country_names.get(country_code, country_code),
                            "name": translated_name,
                            "risk_level": "높음",
                            "impact_summary": "현지 공휴일로 인한 통관, 내륙 운송, 생산 및 선적 지연이 예상됩니다."
                        })
            except Exception as e:
                print(f"WARN: {country_code} 공휴일 정보를 가져오는 중 오류 발생: {e}")
        return holiday_events

    def _get_manual_events(self, start_date, end_date):
        """Config 파일에 정의된 수동 이벤트 정보를 수집합니다."""
        manual_events = []
        for event in self.config.MANUAL_LOGISTICS_EVENTS:
            for year in range(start_date.year, end_date.year + 2):
                event_date = None
                if "day" in event:
                    event_date = date(year, event['month'], event['day'])
                # ✨ [수정] 'n번째 요일'을 계산하는 로직을 올바른 문법으로 변경
                elif "day_of_week" in event:
                    if event['name'] == '미국 블랙프라이데이':
                        # 11월 1일에서 시작해서 4번째 목요일(TH(4))을 찾고, 거기에 하루를 더함
                        thanksgiving = date(year, 11, 1) + relativedelta(weekday=TH(4))
                        event_date = thanksgiving + timedelta(days=1)
                    elif event['name'] == '미국 사이버먼데이':
                        # 11월 1일에서 시작해서 4번째 목요일(TH(4))을 찾고, 4일을 더함
                        thanksgiving = date(year, 11, 1) + relativedelta(weekday=TH(4))
                        event_date = thanksgiving + timedelta(days=4)
                
                if event_date and start_date <= event_date <= end_date:
                    manual_events.append({
                        "date": event_date,
                        "country": self.country_names.get(event['country_code'], event['country_code']),
                        "name": event['name'],
                        "risk_level": event['risk_level'],
                        "impact_summary": event['impact_summary']
                    })
        return manual_events

    def _get_weather_risks(self, start_date, end_date):
        """WeatherService를 이용해 날씨 리스크 정보를 수집합니다."""
        weather_events = []
        try:
            # ✨ [수정] self.config를 WeatherService에 전달합니다.
            weather_service = WeatherService(self.config)
            
            # WeatherService는 7일 예보만 제공하므로, 오늘부터 7일까지만 확인
            risks = weather_service.get_weekly_weather_risks()
            for risk in risks:
                if start_date <= risk['date'] <= end_date:
                    weather_events.append({
                        "date": risk['date'],
                        "country": f"한국({risk['location']})",
                        "name": f"기상 악화 ({risk['event']})",
                        "risk_level": "높음",
                        "impact_summary": f"{risk['event']}의 영향으로 해당 권역의 항만 운영 및 내륙 운송에 차질이 예상됩니다."
                    })
        except Exception as e:
            print(f"WARN: 날씨 리스크 정보를 가져오는 중 오류 발생: {e}")
        return weather_events

    def generate_risk_events(self):
        """모든 리스크 정보를 종합하여 날짜순으로 정렬된 리스트를 반환합니다."""
        today = date.today()
        
        start_date = today - timedelta(days=1)
        end_date = today + timedelta(days=21)
        
        print("-> 글로벌 물류 리스크 이벤트 수집 시작...")
        # ✨ [디버깅 로그] 어떤 기간을 조회하는지 출력
        print(f"[DEBUG] 검색 기간: {start_date} ~ {end_date}")

        holidays = self._get_holidays(start_date, end_date)
        # ✨ [디버깅 로그] 공휴일 수집 결과 출력
        print(f"[DEBUG] 공휴일 수집 결과 ({len(holidays)}개): {holidays}")

        manual_events = self._get_manual_events(start_date, end_date)
        # ✨ [디버깅 로그] 수동 이벤트 수집 결과 출력
        print(f"[DEBUG] 수동 이벤트 수집 결과 ({len(manual_events)}개): {manual_events}")

        # weather_risks = self._get_weather_risks(start_date, end_date)
        # # ✨ [디-버깅 로그] 날씨 리스크 수집 결과 출력
        # print(f"[DEBUG] 날씨 리스크 수집 결과 ({len(weather_risks)}개): {weather_risks}")

        all_events = holidays + manual_events 
        
        unique_events = list({(e['date'], e['name']): e for e in all_events}.values())
        sorted_events = sorted(unique_events, key=lambda x: x['date'])
        
        print(f"✅ {len(sorted_events)}개의 물류 리스크 이벤트를 발견했습니다.")
        return sorted_events
