# gmail_api_service.py (新着メール取得漏れ・バグ修正・差分取得対応版)

import base64
import logging
import time
from datetime import datetime, timezone as dt_timezone
from django.utils import timezone
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)


class GmailApiService:
    def __init__(self, token_info):
        self.creds = Credentials(**token_info) if isinstance(token_info, dict) else token_info
        self.service = build('gmail', 'v1', credentials=self.creds)

    def fetch_emails_from_box_paginated(
        self,
        mailbox="INBOX",
        limit=500,
        search_query="",
        target_tab="all",
        page_token=None,
        newer_than_timestamp=None,
    ):
        """
        指定されたメールボックス（INBOX / SENT）からメールを取得する。
        newer_than_timestamp (UNIX秒) が指定された場合、それ以降に受信したメールを優先・対象とする。
        """
        all_fetched_emails = []
        current_page_token = page_token

        try:
            mailbox_upper = (mailbox or "INBOX").upper()
            label_ids = ["INBOX"] if mailbox_upper == "INBOX" else ["SENT"]

            q_parts = []
            if search_query:
                q_parts.append(search_query.strip())

            # 新着取りこぼし防止: 直近の基準日時がある場合は余裕（例: 5分=300秒）を持たせて after: を付与
            if newer_than_timestamp:
                safe_ts = max(0, int(newer_than_timestamp) - 300)
                q_parts.append(f"after:{safe_ts}")

            q_str = " ".join(q_parts).strip()

            while len(all_fetched_emails) < limit:
                remaining_limit = limit - len(all_fetched_emails)
                fetch_count = min(remaining_limit, 50)

                list_params = {
                    'userId': 'me',
                    'labelIds': label_ids,
                    'maxResults': fetch_count,
                }
                if q_str:
                    list_params['q'] = q_str
                if current_page_token:
                    list_params['pageToken'] = current_page_token

                results = None
                for list_attempt in range(3):
                    try:
                        results = self.service.users().messages().list(**list_params).execute()
                        break
                    except Exception as e:
                        err_str = str(e)
                        if "403" in err_str or "429" in err_str or "Quota exceeded" in err_str or "rateLimitExceeded" in err_str:
                            sleep_sec = 5 * (list_attempt + 1)
                            logger.warning(f"Gmail API リスト取得クォータ制限。{sleep_sec}秒後に再試行します... ({e})")
                            time.sleep(sleep_sec)
                        else:
                            raise e

                if not results:
                    break

                messages_meta = results.get('messages', [])
                current_page_token = results.get('nextPageToken')

                if not messages_meta:
                    break

                for msg_meta in messages_meta:
                    if len(all_fetched_emails) >= limit:
                        break

                    msg_id = msg_meta['id']
                    time.sleep(0.25)

                    msg_data = None
                    max_retries = 3
                    backoff_factor = 3

                    for attempt in range(max_retries):
                        try:
                            msg_data = self.service.users().messages().get(
                                userId='me',
                                id=msg_id,
                                format='full'
                            ).execute()
                            break
                        except Exception as e:
                            error_str = str(e)
                            if "403" in error_str or "429" in error_str or "Quota exceeded" in error_str or "rateLimitExceeded" in error_str:
                                sleep_time = backoff_factor ** (attempt + 1)
                                logger.warning(
                                    f"Gmail API 詳細取得クォータ制限検知。{sleep_time}秒後に再試行します "
                                    f"(ID: {msg_id}, 試行 {attempt + 1}/{max_retries})"
                                )
                                time.sleep(sleep_time)
                            else:
                                logger.error(f"メッセージ詳細取得失敗 (ID: {msg_id}): {e}")
                                break

                    if not msg_data:
                        continue

                    label_list = msg_data.get('labelIds', [])
                    tab_category = self._detect_tab_category(label_list)

                    payload = msg_data.get('payload', {})
                    headers = payload.get('headers', [])
                    subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), '(件名なし)')
                    sender = next((h['value'] for h in headers if h['name'].lower() in ['from', 'sender']), '')
                    raw_msg_id = next((h['value'] for h in headers if h['name'].lower() == 'message-id'), msg_id)
                    thread_id = msg_data.get('threadId', msg_id)

                    # 【修正】未定義だった body を _extract_body で確実に取得
                    body = self._extract_body(payload)

                    internal_date_ms = int(msg_data.get('internalDate', time.time() * 1000))
                    internal_sec = internal_date_ms / 1000.0
                    utc_dt = datetime.fromtimestamp(internal_sec, tz=dt_timezone.utc)
                    received_at = utc_dt.astimezone(timezone.get_current_timezone())

                    clean_raw_id = raw_msg_id.strip().strip('<>') if raw_msg_id else msg_id
                    email_item = {
                        "gmail_msg_id": msg_id,
                        "message_id": f"gm-{clean_raw_id}",
                        "thread_id": f"th-{thread_id}" if thread_id else f"th-{msg_id}",
                        "subject": subject or "(件名なし)",
                        "sender": sender,
                        "body": body,
                        "received_at": received_at,
                        "tab_category": tab_category,
                    }
                    all_fetched_emails.append(email_item)

                if not current_page_token or len(messages_meta) == 0:
                    break

            # 新しいメールが先頭に来るよう受信日時降順で整列
            all_fetched_emails.sort(key=lambda x: x["received_at"], reverse=True)
            return all_fetched_emails, None

        except Exception as e:
            logger.error(f"Gmail API取得エラー: {e}", exc_info=True)
            return all_fetched_emails, str(e)

    @staticmethod
    def _detect_tab_category(label_list):
        labels = set(label_list or [])
        if 'CATEGORY_PROMOTIONS' in labels:
            return "プロモーション"
        if 'CATEGORY_SOCIAL' in labels:
            return "ソーシャル"
        if 'CATEGORY_FORUMS' in labels:
            return "フォーラム"
        if 'CATEGORY_UPDATES' in labels:
            if 'IMPORTANT' in labels:
                return "メイン"
            return "新着"
        if 'CATEGORY_PERSONAL' in labels:
            return "メイン"
        return "メイン"

    def _extract_body(self, payload):
        if not payload:
            return ""

        mime_type = payload.get('mimeType', '')
        body_data = payload.get('body', {}).get('data')

        # 単一パートの場合
        if 'parts' not in payload:
            if body_data:
                return base64.urlsafe_b64decode(body_data.encode('UTF-8')).decode('utf-8', errors='replace')
            return ""

        plain_text = ""
        html_text = ""

        for part in payload.get('parts', []):
            part_mime = part.get('mimeType', '')
            part_body_data = part.get('body', {}).get('data')

            if part_mime == 'text/plain' and part_body_data and not plain_text:
                plain_text = base64.urlsafe_b64decode(part_body_data.encode('UTF-8')).decode('utf-8', errors='replace')
            elif part_mime == 'text/html' and part_body_data and not html_text:
                html_text = base64.urlsafe_b64decode(part_body_data.encode('UTF-8')).decode('utf-8', errors='replace')
            elif 'parts' in part:
                nested = self._extract_body(part)
                if nested:
                    # ネストされたパートから取得できた場合（HTML優先またはテキスト補完）
                    if '<' in nested and '>' in nested and not html_text:
                        html_text = nested
                    elif not plain_text:
                        plain_text = nested

        # HTMLがあればHTMLを返し、なければプレーンテキストを返す
        return html_text or plain_text or (
            base64.urlsafe_b64decode(body_data.encode('UTF-8')).decode('utf-8', errors='replace')
            if body_data else ""
        )