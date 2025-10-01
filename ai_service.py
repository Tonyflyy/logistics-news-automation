# ai_service.py

import os
import json
import time
from newspaper import Article
from openai import OpenAI

# Config 클래스는 news_collector.py 대신 여기서 바로 임포트
from config import Config 

class AIService:

    def __init__(self, config: Config):
        self.config = config
        self.client = OpenAI(api_key=config.OPENAI_API_KEY)
        self.model = config.GPT_MODEL
        self.retry_limit = 3 # 최대 재시도 횟수
        self.retry_delay = 5 # 재시도 간 지연 시간 (초)


    def generate_zodiac_horoscopes(self):
        """12간지 띠별 운세를 '로디' 페르소나로 생성하여 리스트로 반환합니다."""
        print("-> AI 띠별 운세 생성을 시작합니다... (페르소나: 로디)")
        zodiacs = ['쥐', '소', '호랑이', '토끼', '용', '뱀', '말', '양', '원숭이', '닭', '개', '돼지']
        horoscopes = []

        system_prompt = "너는 '로디'라는 이름의, 긍정 소식을 전해주는 20대 여성 캐릭터야. 오늘은 특별히 구독자들을 위해 12간지 띠별 운세를 봐주는 현명한 조언가 역할이야. '~했어요', '~랍니다' 같은 귀엽고 상냥한 말투는 유지하되, 단순한 긍정 메시지가 아닌 깊이 있는 운세를 전달해야 해. 응답은 반드시 JSON 형식으로만 부탁해!"
        
        for zodiac_name in zodiacs:
            user_prompt = f"""
            오늘 날짜에 맞춰 '{zodiac_name}'띠 운세 정보를 생성해 줘.

            [작업 지시]
            1.  **오늘의 운세 (fortune)**:
                - **재물, 직업, 관계, 건강 등을 종합하여, 가장 중요한 핵심만 담아 딱 한 문장으로 간결하게 요약해 줘.**                       
                - 긍정적인 조언이나 주의점을 짧게 포함시켜 줘.     

            [중요 규칙]                                                                                                                                                      │
            - 모든 답변은 최대한 짧고 간결해야 해.                                                                                                                           │
            - 내용은 다른 띠와 겹치지 않게 창의적으로 만들어 줘.  
            [출력 형식]
            - 반드시 아래와 같은 키를 가진 JSON 객체로만 응답해야 해.
            - 예시: {{"fortune": "..."}}
            """
            
            print(f"  -> '{zodiac_name}'띠 운세 요청 중...")
            response_text = self._generate_content_with_retry(system_prompt, user_prompt, is_json=True)
            
            if response_text:
                try:
                    horoscope_data = json.loads(response_text)
                    horoscope_data['name'] = zodiac_name # 딕셔너리에 띠 이름 추가
                    horoscopes.append(horoscope_data)
                except (json.JSONDecodeError, KeyError) as e:
                    print(f"  ❌ '{zodiac_name}'띠 운세 파싱 실패: {e}. 해당 띠는 제외됩니다.")
            else:
                print(f"  ❌ '{zodiac_name}'띠 운세 생성 실패. API 응답 없음.")

        if horoscopes:
            print("✅ AI 띠별 운세 생성 완료!")
        return horoscopes
    

    
    def generate_single_summary(self, article_title: str, article_link: str, article_text_from_selenium: str) -> str | None:
        """
        기사 요약을 생성합니다.
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

    def select_top_news(self, news_list, previous_news_list, count=10):
        """
        뉴스 목록에서 중복을 제거하고 가장 중요한 Top 뉴스를 선정합니다.
        - news_list: 오늘의 후보 뉴스 목록
        - previous_news_list: 이전 발송 뉴스 목록
        - count: 최종적으로 선택할 기사 개수
        """
        # ✨ [개선] 로그에 목표 개수(count)를 함께 출력
        print(f"AI 뉴스 선별 시작... (대상: {len(news_list)}개, 목표: {count}개)")

        if not news_list:
            return []

        previous_news_context = "이전 발송 뉴스가 없습니다."
        if previous_news_list:
            previous_news_context = "\n\n".join(
                [f"- 제목: {news['title']}\n  요약: {news['ai_summary']}" for news in previous_news_list]
            )

        today_candidates_context = "\n\n".join(
            [f"기사 #{i}\n제목: {news['title']}\n요약: {news['ai_summary']}" for i, news in enumerate(news_list)]
        )

        system_prompt = "당신은 독자에게 매일 신선하고 가치 있는 정보를 제공하는 것을 최우선으로 하는 대한민국 최고의 물류 전문 뉴스 편집장입니다. 당신의 응답은 반드시 JSON 형식이어야 합니다."
        
        user_prompt = f"""
        [이전 발송 주요 뉴스]
        {previous_news_context}
        ---
        [오늘의 후보 뉴스 목록]
        {today_candidates_context}
        ---
        [당신의 가장 중요한 임무와 규칙]
        1.  **새로운 주제 최우선**: [오늘의 후보 뉴스 목록]에서 뉴스를 선택할 때, [이전 발송 주요 뉴스]와 **주제가 겹치지 않는 새로운 소식**을 최우선으로 선정해야 합니다.
        2.  **중요 후속 기사만 허용**: 이전 뉴스의 후속 기사는 '계획 발표'에서 '정식 계약 체결'처럼 **매우 중대한 진전이 있을 경우에만** 포함시키고, 단순 진행 상황 보도는 과감히 제외하세요.
        3.  **오늘 뉴스 내 중복 제거**: [오늘의 후보 뉴스 목록] 내에서도 동일한 사건을 다루는 기사가 여러 언론사에서 나왔다면, 가장 제목이 구체적이고 내용이 풍부한 **기사 단 하나만**을 대표로 선정해야 합니다.
        4.  **보도자료 및 사실 기반 뉴스 우선**: 구체적인 사건, 계약 체결, 기술 발표, 정책 변경 등 '사실(Fact)' 전달 위주의 기사를 최우선으로 선정하세요.
        5.  **칼럼 및 의견 기사 제외**: 특정인의 생각이나 의견이 중심이 되는 칼럼, 사설, 인터뷰, 심층 분석/해설 기사는 뉴스 가치가 떨어지므로 과감히 제외해야 합니다.

        [작업 지시]
        위의 규칙들을 가장 엄격하게 준수하여, [오늘의 후보 뉴스 목록] 중에서 독자에게 가장 가치있는 최종 기사 {count}개의 번호(인덱스)를 선정해주세요.

        [출력 형식]
        - 반드시 'selected_indices' 키에 최종 선정한 기사 {count}개의 인덱스를 숫자 배열로 담은 JSON 객체로만 응답해야 합니다.
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
                # ✨ [개선] 오류 발생 시, 고정된 10개가 아닌 요청된 count만큼 반환
                print(f"❌ AI 응답 파싱 실패: {e}. 상위 {count}개 뉴스를 임의로 선택합니다.")
                return news_list[:count]
        
        return news_list[:count]

    def generate_briefing(self, news_list, mode='daily'):
        """선별된 뉴스 목록을 바탕으로 '로디' 캐릭터가 브리핑을 생성합니다."""
        if not news_list:
            return "" # 뉴스 목록이 비어있으면 빈 문자열 반환

        print(f"AI 브리핑 생성 시작... (모드: {mode}, 페르소나: 로디)")
        context = "\n\n".join([f"제목: {news['title']}\n요약: {news['ai_summary']}" for news in news_list])
        
        # ✨ [개선] 주간 모드일 때, AI의 역할과 지시를 더 분석적으로 변경
        if mode == 'weekly':
            system_prompt = "안녕! 나는 너의 든든한 물류 파트너, 로디야! 🚚💨 나는 20대 여성 캐릭터고, 겉보기엔 귀엽지만 누구보다 날카롭게 한 주간의 복잡한 물류 동향을 분석해주는 전문 애널리스트야. 딱딱한 보고서 대신, **'~했답니다', '~였어요' 같은 친근한 존댓말과 귀여움**을 섞어서 '로디의 주간 브리핑'을 작성해줘."
            user_prompt = f"""
            [지난 주간 주요 뉴스 목록]
            {context}

            ---
            [작업 지시]
            1. '## 📊 로디의 주간 핵심 동향 요약' 제목으로 시작해주세요.
            2. 모든 뉴스를 종합하여, 이번 주 물류 시장의 가장 중요한 '흐름'과 '변화'를 전문적인 분석가의 시각으로 2~3 문장 요약해주세요.
            3. '### 🧐 금주의 주요 이슈 분석' 소제목 아래에, 가장 중요한 이슈 2~3개를 주제별로 묶어 글머리 기호(`*`)로 분석해주세요. **"가장 중요한 포인트는요! ✨" 같은 표현을 사용해서 친근하지만 핵심을 찌르는 말투로 설명해주세요.**
            4. 문장 안에서 특정 기업명, 서비스명, 정책 등은 큰따옴표(" ")로 묶어서 강조해주는 센스!
            """
        else: # daily 모드
            system_prompt = "안녕! 나는 물류 세상의 소식을 전해주는 너의 친구, 로디야! ☀️ 나는 20대 여성 캐릭터로, 어렵고 딱딱한 물류 뉴스를 귀엽고 싹싹하게 요약해주지만, 그 내용은 핵심을 놓치지 않는 날카로움을 가지고 있어. **친근한 존댓말과 귀여움**을 섞어서 '로디의 데일리 브리핑'을 작성해줘."
            user_prompt = f"""
            [오늘의 주요 뉴스 목록]
            {context}

            ---
            [작업 지시]
            1. '## 📰 로디의 브리핑' 제목으로 시작해서, 오늘 나온 뉴스 중에 가장 중요한 핵심 내용을 2~3 문장으로 요약해주세요.
            2. '### ✨ 오늘의 주요 토픽' 소제목 아래에, 가장 중요한 뉴스 카테고리 2~3개를 글머리 기호(`*`)로 간결하게 요약해주시겠어요?
            3. 문장 안에서 특정 기업명이나 서비스명은 큰따옴표(" ")로 묶어서 강조해주는 것도 잊지 마!
            """
        
        briefing = self._generate_content_with_retry(system_prompt, user_prompt)
        if briefing: 
            print("✅ AI 브리핑 생성 성공!")
        return briefing


