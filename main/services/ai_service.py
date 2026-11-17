# ai_service.py (構文エラー修正・完全版)

import logging
import os
import time
from bs4 import BeautifulSoup
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field
from typing import Literal
from langchain_core.messages import HumanMessage
import json

logger = logging.getLogger(__name__)

FAILED_SUMMARY_MESSAGES = {
    '要約の生成に失敗しました。',
    '要約の生成中にエラーが発生しました。',
    'メール本文が空であるか、テキストを抽出できませんでした。',
}


class EmailAnalysisSchema(BaseModel):
    category: Literal["agent", "company", "service", "other"] = Field(
        description="メールのカテゴリ。必ず指定された4つのいずれか1つを選択してください。"
    )
    summary: str = Field(
        description="メールの要約。すべてのカテゴリでURLや重要な詳細情報を省略せず記載。"
    )


class AIService:
    def __init__(self, model=None, diagnose=True):
        target_model = model or "llama3"
        self.model = target_model

        self.llm = ChatOllama(
            model=target_model,
            temperature=0.1,
        )

        try:
            self.structured_chain = self.llm.bind(response_format={"type": "json_object"})
            self.use_fallback_json = True
        except Exception as e:
            logger.warning(f"JSONモード設定に失敗しました: {e}")
            self.structured_chain = self.llm
            self.use_fallback_json = False

        if diagnose:
            self._diagnose_mode()

    def _diagnose_mode(self):
        try:
            logger.info("=========================================")
            logger.info(f"🤖 ローカルLLM (Ollama) モデル: {self.model} (JSONモード) 起動完了")
            logger.info("=========================================")
        except Exception as e:
            logger.error(f"❌ 診断中にエラーが発生しました: {e}")

    @staticmethod
    def needs_summary(email_obj, regenerate=False):
        if regenerate or not email_obj.summary or email_obj.summary in FAILED_SUMMARY_MESSAGES:
            return True
        return False

    def analyze_email(self, subject, email_body, sender=""):
        """
        単一のメールを分析・要約するメソッド（フォールバックおよび個別実行用）
        """
        rule_category = self._quick_classify_rule(subject, sender, email_body)
        cleaned_text = self._extract_clean_text_with_urls(email_body)

        if not cleaned_text or len(cleaned_text.strip()) < 5:
            return rule_category or 'other', 'メール本文が空であるか、テキストを抽出できませんでした。'

        safe_text = cleaned_text.strip()
        if len(safe_text) > 5000:
            safe_text = safe_text[:5000] + "\n...(以下省略)..."

        prompt_text = f"""以下のメールを分析し、必ず以下のJSONフォーマットのみで結果を返してください。

【カテゴリ分類ルール（厳守）】
- "agent": フリーランスエージェント、IT系派遣・紹介会社（レバテック、Midworks、PE-BANK、ITプロパートナーズ等）、求人案内、スカウト、および **Dodaなど転職エージェントからの企業紹介・案件紹介メール**。※迷ったら案件や求人紹介であれば "agent" にしてください。
- "company": **企業からの直接の連絡**（応募完了、書類選考の案内、面接日程調整、合否結果、内定通知、お問い合わせへの回答など）。
- "service": 各種サービスの通知、注文確認、領収書、セキュリティ通知など。
- "other": 上記のいずれにも当てはまらないメール。

【要約（summary）作成ルール（厳守）】
- **【最重要】フリーランスエージェントやDodaからの案件紹介・求人情報メールの場合、メール内に記載されている「すべての案件の名前」「案件の主な内容」「それぞれの詳細ページへのリンク（URL）」を、件数が多い場合でも一切省略せずにすべて箇条書きなどで網羅して要約してください。**
- 省略せずすべてのURLを出力してください。文字数制限はありません。

出力JSONフォーマット:
{{
  "category": "agent" または "company" または "service" または "other",
  "summary": "作成した詳細な要約テキスト"
}}

[システム判定ヒント]: {rule_category or "agent"}
送信者: {sender}
Subject: {subject}
Body:
{safe_text}
"""
        max_retries = 2
        for attempt in range(max_retries):
            try:
                messages = [HumanMessage(content=prompt_text)]
                response = self.structured_chain.invoke(messages)
                content = response.content.strip()

                if content.startswith("```"):
                    content = content.split("```")[1]
                    if content.startswith("json"):
                        content = content[4:]
                    content = content.strip()

                data = json.loads(content)
                category = data.get("category", rule_category or 'agent')

                if rule_category == 'agent':
                    category = 'agent'

                if category not in ["agent", "company", "service", "other"]:
                    category = "other"

                summary = data.get("summary", "要約の生成に失敗しました。")
                return category, summary

            except Exception as e:
                logger.error(f"ローカルLLM解析エラー (試行 {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    return rule_category or 'agent', '要約の生成に失敗しました。'
                time.sleep(0.5)

        return rule_category or 'agent', '要約の生成に失敗しました。'

    def analyze_emails_batch(self, emails_data):
        """
        複数のメール（リスト）を一括解析するバッチ処理メソッド
        """
        if not emails_data:
            return {}

        mapping = {}
        sub_batch_size = 2
        for i in range(0, len(emails_data), sub_batch_size):
            sub_chunk = emails_data[i:i + sub_batch_size]
            sub_result = self._process_sub_batch(sub_chunk)
            mapping.update(sub_result)

        return mapping

    def _process_sub_batch(self, emails_data):
        formatted_emails = []
        rule_categories = {}

        for item in emails_data:
            rule_cat = self._quick_classify_rule(item['subject'], item['sender'], item['body'])
            rule_categories[item['id']] = rule_cat

            cleaned_text = self._extract_clean_text_with_urls(item['body'])
            safe_text = cleaned_text.strip()
            if len(safe_text) > 3000:
                safe_text = safe_text[:3000] + "\n...(以下省略)..."

            formatted_emails.append({
                "id": item['id'],
                "sender": item['sender'],
                "subject": item['subject'],
                "hint": rule_cat or "agent",
                "body": safe_text
            })

        prompt_text = f"""以下の複数のメールをそれぞれ分析し、必ず以下のJSONオブジェクト形式（キー名: "results"）で結果を返してください。
Markdownのバッククォート（```json など）は含めず、純粋なJSON文字列のみを出力してください。

【カテゴリ分類ルール（厳守）】
- "agent": フリーランスエージェント、IT派遣、Dodaなどの求人紹介・スカウト・案件案内。
- "company": 企業からの直接の連絡（応募完了、選考、面接、内定、回答など）。
- "service": サービス通知、領収書など。
- "other": その他。

【要約（summary）作成ルール（厳守）】
- すべてのメールについて、特にエージェントからの案件紹介は、**「すべての案件名」「内容」「すべてのURL」を一切省略せずに**要約に含めてください。文字数制限はありません。

{{
  "results": [
    {{
      "id": メールID,
      "category": "agent" または "company" または "service" または "other",
      "summary": "作成した詳細な要約テキスト（すべての案件名とURLを網羅）"
    }}
  ]
}}

分析対象のメール一覧:
{json.dumps(formatted_emails, ensure_ascii=False)}
"""

        try:
            messages = [HumanMessage(content=prompt_text)]
            response = self.structured_chain.invoke(messages)
            content = response.content.strip()

            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

            parsed_data = json.loads(content)

            if isinstance(parsed_data, dict):
                results_list = parsed_data.get("results", [])
            elif isinstance(parsed_data, list):
                results_list = parsed_data
            else:
                results_list = []

            mapping = {}
            for res in results_list:
                if isinstance(res, str):
                    try:
                        res = json.loads(res)
                    except Exception:
                        continue

                if isinstance(res, dict):
                    email_id = res.get("id")
                    cat = res.get("category", "other")

                    if email_id in rule_categories and rule_categories[email_id] == 'agent':
                        cat = 'agent'

                    if cat not in ["agent", "company", "service", "other"]:
                        cat = "other"

                    sum_text = res.get("summary", "要約の生成に失敗しました。")
                    if email_id:
                        mapping[email_id] = (cat, sum_text)

            for item in emails_data:
                e_id = item['id']
                if e_id not in mapping:
                    rc = rule_categories.get(e_id) or 'other'
                    mapping[e_id] = (rc, "要約の生成に失敗しました。")

            return mapping

        except Exception as e:
            logger.error(f"ローカルLLMバッチ解析エラー: {e}")
            fallback_mapping = {}
            for item in emails_data:
                e_id = item['id']
                rc = rule_categories.get(e_id) or 'other'
                fallback_mapping[e_id] = (rc, "要約の生成に失敗しました。")
            return fallback_mapping

    def generate_reply(self, email_body, sender, subject, tone="polite", category="other"):
        cleaned_text = self._extract_clean_text_with_urls(email_body)
        safe_text = cleaned_text.strip()
        if len(safe_text) > 1000:
            safe_text = safe_text[:1000] + "..."

        prompt = f"""Generate a reply in Japanese. Tone: {tone}\nSubject: {subject}\nBody: {safe_text}\nReply:"""
        try:
            response = self.llm.invoke([HumanMessage(content=prompt)])
            if response and response.content:
                return response.content.strip()
        except Exception as e:
            logger.error(f"返信案生成エラー: {e}")
        return "返信案の生成に失敗しました。"

    def _quick_classify_rule(self, subject, sender, body=""):
        subject_lower = (subject or "").lower()
        sender_lower = (sender or "").lower()
        body_lower = (body or "").lower()

        company_domains = ['hrmos.co', 'onehr.hrmos.co', 'talentio.com', 'sonar-ats.jp', 'i-webs.jp', 'herp.careers']
        if any(d in sender_lower for d in company_domains) or any(
                k in subject_lower for k in
                ['選考結果', '応募完了', '面接日程', '内定', '書類選考', 'お問い合わせ', 'ご回答']):
            return 'company'

        agent_domains = [
            'doda.jp', 'mynavi.jp', 'bizreach.jp', 'indeed.com', 'rikunabi.com', 'r-agent.com',
            'levtech.jp', 'midworks.jp', 'pe-bank.jp', 'itpropartners.com', 'geektospace.com',
            'pasona.co.jp', 'randstad.co.jp', 'forkwell.com', 'tenshoku.mynavi.jp'
        ]
        agent_keywords = [
            '求人紹介', 'おすすめ求人', 'スカウト', '新着求人', '案件紹介', '案件のご案内',
            'フリーランス', 'エンジニア案件', 'プロポーザル', 'ご提案案件', '案件情報', '求人情報'
        ]

        if any(d in sender_lower for d in agent_domains) or any(
                k in subject_lower for k in agent_keywords) or any(
            k in body_lower for k in ['案件紹介', 'おすすめ案件', '求人をご紹介']):
            return 'agent'

        if any(k in subject_lower for k in ['領収書', '注文確認', 'パスワード', '決済完了', '認証コード']):
            return 'service'

        return None

    def _extract_clean_text_with_urls(self, html_content):
        if not html_content:
            return ""
        if "<html" not in html_content.lower() and "<div" not in html_content.lower() and "<a " not in html_content.lower():
            return html_content
        try:
            soup = BeautifulSoup(html_content, "html.parser")
            for element in soup(["style", "script", "head", "title", "meta"]):
                element.decompose()

            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"].strip()
                if href.startswith("http"):
                    link_text = a_tag.get_text(strip=True)  # <-- カッコの閉じ忘れを修正しました
                    if link_text and href not in link_text:
                        a_tag.replace_with(f"{link_text} ({href})")
                    elif not link_text:
                        a_tag.replace_with(f"({href})")

            text = soup.get_text(separator='\n', strip=True)
            text = re.sub(r'\n{3,}', '\n\n', text)
            return text
        except Exception:
            return html_content