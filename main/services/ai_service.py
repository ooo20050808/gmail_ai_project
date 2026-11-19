import json
import logging
import os
import re
import time
from typing import Literal

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

# .envファイルから環境変数を読み込む
load_dotenv()

logger = logging.getLogger(__name__)

FAILED_SUMMARY_MESSAGES = {
    '要約の生成に失敗しました。',
    '要約の生成中にエラーが発生しました。',
    'メール本文が空であるか、テキストを抽出できませんでした。',
}

DEFAULT_MODEL = "qwen2.5:7b"


def _get_env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        val = int(raw.strip())
        return val if val > 0 else default
    except ValueError:
        logger.warning(f"環境変数 {name}={raw!r} の数値変換に失敗したためデフォルト {default} を使用します。")
        return default


class EmailAnalysisSchema(BaseModel):
    category: Literal["agent", "company", "other"] = Field(
        description="メールのカテゴリ。必ず指定された3つのいずれか1つを選択してください。"
    )
    summary: str = Field(
        description="メールの要約。すべてのカテゴリでURLや重要な詳細情報を省略せず記載。"
    )


class AIService:
    def __init__(self, model=None, diagnose=True):
        # 念のためインスタンス生成時にも .env を再読み込み可能にする
        load_dotenv()

        # 優先順位: 明示引数 model > .env(環境変数 OLLAMA_MODEL) > デフォルトモデル
        env_model = (os.getenv("OLLAMA_MODEL") or "").strip()
        target_model = model or env_model or DEFAULT_MODEL
        self.model = target_model

        # 8GB RAM / CPU推論環境向けにコンテキスト長と最大生成トークンを環境変数で制御
        num_ctx = _get_env_int("OLLAMA_NUM_CTX", 2048)
        num_predict = _get_env_int("OLLAMA_NUM_PREDICT", 1024)
        num_thread = _get_env_int("OLLAMA_NUM_THREAD", max(1, (os.cpu_count() or 4) - 1))

        self.llm = ChatOllama(
            model=target_model,
            temperature=0.1,
            num_ctx=num_ctx,
            num_predict=num_predict,
            num_thread=num_thread,
        )

        # JSON出力モードの設定（失敗時は通常LLMにフォールバック）
        try:
            self.structured_chain = self.llm.bind(response_format={"type": "json_object"})
            self.use_fallback_json = True
        except Exception as e:
            logger.warning(f"JSONモード設定に失敗しました: {e}")
            self.structured_chain = self.llm
            self.use_fallback_json = False

        if diagnose:
            self._diagnose_mode(num_ctx=num_ctx, num_thread=num_thread)

    def _diagnose_mode(self, num_ctx: int, num_thread: int):
        try:
            logger.info("=========================================")
            logger.info(
                f"🤖 ローカルLLM (Ollama) モデル: {self.model} "
                f"(ctx={num_ctx}, threads={num_thread}) 起動完了"
            )
            logger.info("=========================================")
        except Exception as e:
            logger.error(f"❌ 診断中にエラーが発生しました: {e}")

    @staticmethod
    def needs_summary(email_obj, regenerate=False):
        if regenerate or not email_obj.summary or email_obj.summary in FAILED_SUMMARY_MESSAGES:
            return True
        return False

    @staticmethod
    def _parse_json_response(raw_content: str) -> dict:
        content = (raw_content or "").strip()
        if content.startswith("```"):
            parts = content.split("```")
            if len(parts) >= 2:
                content = parts[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()

        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end != -1 and end > start:
            content = content[start:end + 1]

        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else {}

    def analyze_email(self, subject, email_body, sender=""):
        """
        単一のメールを分析・要約するメソッド
        """
        rule_category = self._quick_classify_rule(subject, sender, email_body)
        cleaned_text = self._extract_clean_text_with_urls(email_body)

        if not cleaned_text or len(cleaned_text.strip()) < 5:
            return rule_category or 'other', 'メール本文が空であるか、テキストを抽出できませんでした。'

        safe_text = cleaned_text.strip()
        if len(safe_text) > 2500:
            safe_text = safe_text[:2500] + "\n...(以下省略)..."

        prompt_text = f"""以下のメールを分析し、必ずJSONフォーマットのみで結果を返してください。

【カテゴリ分類ルール（厳守）】
- "agent": フリーランスエージェント、転職エージェント、求人サイト・求人サービス（doda、エンゲージ、リクルート、マイナビ、レバテック等）、IT系派遣・紹介会社からの求人紹介、案件紹介、スカウト、面談案内などのメール。
- "company": 企業やその企業の担当者からの直接の連絡（企業の採用担当・人事・現場担当者からの選考/面接/内定連絡、企業や取引先担当者からの直接のビジネス・業務連絡など）。※エージェントや求人サイト経由の仲介メールは "agent" とすること。
- "other": 上記以外のメール。各種Webサービスの通知、システム通知・自動送信メール、注文確認、領収書、セキュリティ/認証通知、ニュースレター、および家族・友人・知人などプライベートな連絡はすべて "other" とすること。

【要約（summary）作成ルール（厳守）】
- エージェントからの案件紹介・求人情報メールの場合、記載されている「すべての案件名」「主な内容」「詳細ページURL」を省略せず箇条書きで網羅してください。
- それ以外のメールも重要な詳細情報やURLを省略せず簡潔かつ正確にまとめてください。

出力JSONフォーマット:
{{
  "category": "agent" または "company" または "other",
  "summary": "要約テキスト"
}}

送信者: {sender}
件名: {subject}
本文:
{safe_text}
"""
        max_retries = 2
        for attempt in range(max_retries):
            try:
                messages = [HumanMessage(content=prompt_text)]
                response = self.structured_chain.invoke(messages)
                data = self._parse_json_response(response.content)

                llm_category = (data.get("category") or "").strip().lower()
                if llm_category not in ["agent", "company", "other"]:
                    llm_category = ""

                # 明確なエージェント判定やシステム/サービス通知ルールを優先しつつ、LLMの判定を反映
                if rule_category == "agent":
                    category = "agent"
                elif rule_category == "other" and llm_category != "company":
                    category = "other"
                elif llm_category:
                    category = llm_category
                else:
                    category = rule_category or "other"

                summary = (data.get("summary") or "").strip()
                if not summary:
                    summary = "要約の生成に失敗しました。"

                return category, summary

            except Exception as e:
                logger.error(f"ローカルLLM解析エラー (試行 {attempt + 1}/{max_retries}): {e}")
                if attempt == max_retries - 1:
                    return rule_category or "other", "要約の生成に失敗しました。"
                time.sleep(0.2)

        return rule_category or "other", "要約の生成に失敗しました。"

    def analyze_emails_batch(self, emails_data):
        """
        8GB RAM環境でメモリ逼迫・タイムアウトを防ぐため、1件ずつ逐次実行する。
        """
        if not emails_data:
            return {}

        mapping = {}
        for item in emails_data:
            email_id = item.get("id")
            if email_id is None:
                continue
            try:
                cat, summary = self.analyze_email(
                    subject=item.get("subject", ""),
                    email_body=item.get("body", ""),
                    sender=item.get("sender", ""),
                )
                mapping[email_id] = (cat, summary)
            except Exception as e:
                logger.error(f"メールID {email_id} のバッチ内処理エラー: {e}")
                fallback_cat = (
                    self._quick_classify_rule(
                        item.get("subject", ""),
                        item.get("sender", ""),
                        item.get("body", ""),
                    )
                    or "other"
                )
                mapping[email_id] = (fallback_cat, "要約の生成に失敗しました。")

        return mapping

    def generate_reply(self, email_body, sender, subject, tone="polite", category="other"):
        cleaned_text = self._extract_clean_text_with_urls(email_body)
        safe_text = cleaned_text.strip()
        if len(safe_text) > 800:
            safe_text = safe_text[:800] + "..."

        prompt = (
            f"Generate a reply in Japanese. Tone: {tone}\n"
            f"Subject: {subject}\nBody: {safe_text}\nReply:"
        )
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

        # 1. エージェント（フリーランスエージェント・転職エージェント・dodaやエンゲージ等の求人/案件サービス）
        agent_domains = [
            'doda.jp', 'en-gage.com', 'enjapan.com', 'mynavi.jp', 'bizreach.jp',
            'indeed.com', 'rikunabi.com', 'r-agent.com', 'levtech.jp', 'midworks.jp',
            'pe-bank.jp', 'itpropartners.com', 'geektospace.com', 'pasona.co.jp',
            'randstad.co.jp', 'forkwell.com', 'tenshoku.mynavi.jp', 'findy-code.io',
            'lapras.com', 'green-japan.com', 'wantedly.com',
        ]
        agent_keywords = [
            'doda', 'エンゲージ', 'エージェント', '求人紹介', 'おすすめ求人', 'スカウト',
            '新着求人', '案件紹介', '案件のご案内', 'フリーランス', 'エンジニア案件',
            'プロポーザル', 'ご提案案件', '案件情報', '求人情報', '求人をご紹介',
        ]

        if (
            any(d in sender_lower for d in agent_domains)
            or any(k in subject_lower for k in agent_keywords)
            or any(k in sender_lower for k in ['エージェント', 'doda', 'エンゲージ'])
            or any(k in body_lower for k in ['案件紹介', 'おすすめ案件', '求人をご紹介', 'フリーランスエージェント'])
        ):
            return 'agent'

        # 2. その他（サービス通知・システム通知・決済/認証・プライベート連絡など）
        other_keywords = [
            '領収書', '注文確認', 'パスワード', '決済完了', '認証コード', 'ワンタイム',
            'システム通知', 'サービス通知', '自動送信', '配信停止', 'noreply',
            'no-reply', 'mailer-daemon', '家族', 'お父さん', 'お母さん', '父より', '母より',
        ]
        if any(k in sender_lower for k in other_keywords) or any(k in subject_lower for k in other_keywords):
            return 'other'

        # 3. 企業直接（企業やその企業の担当者からの直接の連絡）
        company_domains = [
            'hrmos.co', 'onehr.hrmos.co', 'talentio.com',
            'sonar-ats.jp', 'i-webs.jp', 'herp.careers',
        ]
        company_keywords = [
            '選考結果', '応募完了', '面接日程', '面接のご案内', '内定', '書類選考',
            '採用担当', '人事部', '株式会社',
        ]
        if (
            any(d in sender_lower for d in company_domains)
            or any(k in subject_lower for k in company_keywords)
            or any(k in sender_lower for k in ['株式会社'])
        ):
            return 'company'

        # 判定が確定しない場合は LLM 側の分類に委ねるため None を返す
        return None

    def _extract_clean_text_with_urls(self, html_content):
        if not html_content:
            return ""
        if (
            "<html" not in html_content.lower()
            and "<div" not in html_content.lower()
            and "<a " not in html_content.lower()
        ):
            return html_content
        try:
            soup = BeautifulSoup(html_content, "html.parser")
            for element in soup(["style", "script", "head", "title", "meta", "noscript"]):
                element.decompose()

            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"].strip()
                if href.startswith("http"):
                    link_text = a_tag.get_text(strip=True)
                    if link_text and href not in link_text:
                        a_tag.replace_with(f"{link_text} ({href})")
                    elif not link_text:
                        a_tag.replace_with(f"({href})")

            text = soup.get_text(separator='\n', strip=True)
            text = re.sub(r'\n{3,}', '\n\n', text)
            return text
        except Exception:
            return html_content