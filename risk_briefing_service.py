# risk_briefing_service.py

from datetime import date, timedelta
import holidays
import json
from dateutil.relativedelta import relativedelta, TH, MO 
from config import Config

class RiskBriefingService:
    def __init__(self, ai_service_instance): 
        self.config = Config()
        self.countries = self.config.RISK_BRIEFING_TARGET_COUNTRIES
        self.country_names = {"KR": "한국"}
        self.ai_service = ai_service_instance 

    def _get_base_holidays(self, start_date, end_date):
        """라이브러리를 통해 기본적인 공휴일 정보만 수집합니다."""
        holiday_events = []
        date_range = [start_date + timedelta(days=i) for i in range((end_date - start_date).days + 1)]

        for country_code in self.countries:
            try:
                country_holidays = holidays.country_holidays(country_code, language='ko')
                for single_date in date_range:
                    if single_date in country_holidays:
                        holiday_name = country_holidays.get(single_date)
                        holiday_events.append({
                            "date": single_date,
                            "country": self.country_names.get(country_code, country_code),
                            "name": holiday_name,
                        })
            except Exception as e:
                print(f"WARN: {country_code} 공휴일 정보를 가져오는 중 오류 발생: {e}")
        
        # 날짜와 이름이 같은 중복 이벤트 제거
        unique_events = list({(e['date'], e['name']): e for e in holiday_events}.values())
        return unique_events

    def _group_consecutive_holidays(self, holidays):
        """연속된 공휴일을 단일 '연휴' 이벤트로 그룹화합니다."""
        if not holidays:
            return []
        
        holidays.sort(key=lambda x: x['date'])
        grouped = []
        
        current_group = [holidays[0]]
        for i in range(1, len(holidays)):
            # 이전 휴일과 하루 차이이고, 이름에 '대체' 또는 '전날/다음날'이 포함된 경우 그룹화
            if (holidays[i]['date'] - holidays[i-1]['date']).days == 1 and \
               any(keyword in holidays[i]['name'] for keyword in ['추석', '설날', '대체', '전날', '다음날']):
                current_group.append(holidays[i])
            else:
                grouped.append(current_group)
                current_group = [holidays[i]]
        grouped.append(current_group)

        final_events = []
        for group in grouped:
            if len(group) > 1:
                # 그룹 이름 결정 (예: '추석'이 포함된 이름 우선)
                group_name = "연휴"
                for name_part in ['추석', '설날']:
                    if any(name_part in item['name'] for item in group):
                        group_name = f"{name_part} 연휴"
                        break

                final_events.append({
                    "date_range": (group[0]['date'], group[-1]['date']),
                    "name": group_name
                })
            else:
                final_events.append({
                    "date_range": (group[0]['date'], group[0]['date']),
                    "name": group[0]['name']
                })
        return final_events
    
    def _get_ai_risk_summary(self, event):
        """AI를 호출하여 이벤트의 물류 리스크 한 줄 요약을 생성합니다."""
        event_name = event['name']
        start_date = event['date_range'][0].strftime('%m/%d')
        end_date = event['date_range'][1].strftime('%m/%d')
        date_str = start_date if start_date == end_date else f"{start_date}~{end_date}"
        
        print(f"-> AI에게 '{event_name}' 리스크 요약 요청 중...")

        system_prompt = "너는 '로디'라는 이름의 물류 리스크 전문 분석가야. 특정 공휴일이 한국 물류 시장에 미치는 영향을 '물류 리스크'라는 제목으로 한 문장으로 요약해야 해. 답변은 반드시 JSON 형식으로만 해야 해."
        user_prompt = f"""
        '{event_name}'({date_str}) 기간의 핵심적인 물류 리스크를 한 문장으로 요약해줘.

        [출력 형식]
        - 반드시 "risk_summary" 라는 키를 가진 JSON 객체로만 응답해야 해.
        - 예시: {{"risk_summary": "장기 연휴로 국내 물류 시스템 대부분이 중단되므로, 연휴 전 선적 마감과 연휴 후 병목 현상 대비가 필수입니다."}}
        """
        
        response_text = self.ai_service._generate_content_with_retry(system_prompt, user_prompt, is_json=True)
        
        if response_text:
            try:
                data = json.loads(response_text)
                event['risk_summary'] = data.get('risk_summary', '리스크 정보를 생성하지 못했습니다.')
                return event
            except (json.JSONDecodeError, KeyError):
                return None
        return None
    

    def _enrich_event_with_ai(self, event):
        """단일 이벤트를 AI에게 보내 상세 요약과 체크포인트를 받아옵니다."""
        print(f"-> AI에게 '{event['name']}' 상세 리스크 분석 요청 중...")
        
        system_prompt = "너는 '로디'라는 이름의, 물류 리스크 전문 분석가야. 특정 공휴일이 물류(특히 수출입, 내륙 운송)에 미치는 영향을 분석해서, 화주와 운송사 모두에게 도움이 되는 정보를 제공해야 해. 답변은 반드시 JSON 형식으로만 해야 해."
        
        user_prompt = f"""
        '{event['name']}'({event['date'].strftime('%Y-%m-%d')}) 공휴일이 한국 물류 시장에 미칠 영향에 대해 분석해줘.

        [작업 지시]
        1.  **핵심 요약 (summary)**: 이 공휴일의 특징과 가장 중요한 영향을 1~2 문장으로 요약해줘.
        2.  **주요 체크포인트 (checkpoints)**: 화주나 운송사가 꼭 확인해야 할 사항들을 2~3개의 짧은 문장으로 된 리스트(배열)로 만들어줘. 예: ["서류 마감: ...", "터미널 운영: ...", "내륙 운송: ..."]
        
        [출력 형식]
        - 반드시 아래와 같은 키를 가진 JSON 객체로만 응답해야 해.
        - 예시: {{"summary": "...", "checkpoints": ["...", "..."]}}
        """
        
        response_text = self.ai_service._generate_content_with_retry(system_prompt, user_prompt, is_json=True)
        
        if response_text:
            try:
                enriched_data = json.loads(response_text)
                event['summary'] = enriched_data.get('summary', '상세 요약 정보를 가져오지 못했습니다.')
                event['checkpoints'] = enriched_data.get('checkpoints', [])
                return event
            except (json.JSONDecodeError, KeyError):
                return None
        return None

    def generate_risk_events(self):
        """모든 리스크 정보를 종합하여 AI 요약이 추가된 리스트를 반환합니다."""
        today = date.today()
        start_date = today
        end_date = today + timedelta(days=21)
        
        print("-> 물류 리스크 이벤트 수집 및 분석 시작...")
        
        base_holidays = self._get_base_holidays(start_date, end_date)
        if not base_holidays:
            print("✅ 분석 기간 내에 해당하는 공휴일이 없습니다.")
            return []

        grouped_events = self._group_consecutive_holidays(base_holidays)
        
        final_risk_events = []
        for event in grouped_events:
            enriched_event = self._get_ai_risk_summary(event)
            if enriched_event:
                final_risk_events.append(enriched_event)
        
        print(f"✅ 총 {len(final_risk_events)}개의 물류 리스크 이벤트를 분석했습니다.")
        return final_risk_events


    
