# gmail_api_service.py (Gmail APIクォータ制限・バックオフ対応版)

import base64
import logging
import re
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

    def fetch_emails_from_box_paginated(self, mailbox="INBOX", limit=500, search_query="", target_tab="all",
                                        page_token=None):
        all_fetched_emails = []
        current_page_token = page_token

        try:
            label_ids = ["INBOX"] if mailbox == "INBOX" else ["SENT"]
            q_parts = []
            if search_query:
                q_parts.append(search_query)
            q_str = " ".join(q_parts)

            while len(all_fetched_emails) < limit:
                remaining_limit = limit - len(all_fetched_emails)
                fetch_count = min(remaining_limit, 50)

                list_params = {
                    'userId': 'me',
                    'labelIds': label_ids,
                    'q': q_str,
                    'maxResults': fetch_count
                }
                if current_page_token:
                    list_params['pageToken'] = current_page_token

                # リスト取得時のクォータ制限対策
                results = None
                for list_attempt in range(3):
                    try:
                        results = self.service.users().messages().list(**list_params).execute()
                        break
                    except Exception as e:
                        if "403" in str(e) or "Quota exceeded" in str(e) or "rateLimitExceeded" in str(e):
                            sleep_sec = 5 * (list_attempt + 1)
                            logger.warning(f"Gmail API リスト取得クォータ制限。{sleep_sec}秒後に再試行します...")
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
                    msg_id = msg_meta['id']

                    # 各メール詳細取得時のクォータ制限・負荷対策としてウェイトを0.4秒に設定
                    time.sleep(0.4)

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
                            if "403" in error_str or "Quota exceeded" in error_str or "rateLimitExceeded" in error_str:
                                sleep_time = backoff_factor ** (attempt + 1)
                                logger.warning(
                                    f"Gmail API 詳細取得クォータ制限検知。{sleep_time}秒後に再試行します (ID: {msg_id}, 試行 {attempt + 1}/{max_retries})"
                                )
                                time.sleep(sleep_time)
                            else:
                                logger.error(f"メッセージ詳細取得失敗 (ID: {msg_id}): {e}")
                                break

                    if not msg_data:
                        continue

                    label_list = msg_data.get('labelIds', [])
                    if 'CATEGORY_PERSONAL' in label_list:
                        tab_category = "メイン"
                    elif 'IMPORTANT' in label_list and 'CATEGORY_UPDATES' in label_list:
                        tab_category = "メイン"
                    elif 'CATEGORY_PROMOTIONS' in label_list:
                        tab_category = "プロモーション"
                    elif 'CATEGORY_SOCIAL' in label_list:
                        tab_category = "ソーシャル"
                    elif 'CATEGORY_UPDATES' in label_list:
                        tab_category = "新着"
                    elif 'CATEGORY_FORUMS' in label_list:
                        tab_category = "フォーラム"
                    else:
                        tab_category = "メイン"

                    headers = msg_data['payload'].get('headers', [])
                    subject = next((h['value'] for h in headers if h['name'].lower() == 'subject'), '(件名なし)')
                    sender = next((h['value'] for h in headers if h['name'].lower() in ['from', 'sender']), '')
                    raw_msg_id = next((h['value'] for h in headers if h['name'].lower() == 'message-id'), msg_id)
                    thread_id = msg_data.get('threadId', msg_id)

                    # 修正後（確実にUTCからターゲットのタイムゾーンへ安全に変換）
                    internal_date_ms = int(msg_data.get('internalDate', time.time() * 1000))
                    internal_sec = internal_date_ms / 1000.0
                    # 明示的にUTCのaware datetimeオブジェクトを作成
                    utc_dt = datetime.fromtimestamp(internal_sec, tz=dt_timezone.utc)
                    # Djangoの設定タイムゾーン（Asia/Tokyoなど）へ変換
                    received_at = utc_dt.astimezone(timezone.get_current_timezone())

                    email_item = {
                        "message_id": f"gm-{raw_msg_id.strip('<>')}",
                        "thread_id": f"th-{thread_id}",
                        "subject": subject,
                        "sender": sender,
                        "body": body,
                        "received_at": received_at,
                        "tab_category": tab_category,
                    }
                    all_fetched_emails.append(email_item)

                if not current_page_token or len(messages_meta) == 0:
                    break

            return all_fetched_emails, None

        except Exception as e:
            logger.error(f"Gmail API取得エラー: {e}")
            return all_fetched_emails, None

    def _extract_body(self, payload):
        body = ""
        if 'parts' in payload:
            for part in payload['parts']:
                mime_type = part.get('mimeType', '')
                if mime_type == 'text/plain' and not body:
                    data = part['body'].get('data')
                    if data:
                        body = base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8', errors='replace')
                elif mime_type == 'text/html':
                    data = part['body'].get('data')
                    if data:
                        return base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8', errors='replace')
                elif 'parts' in part:
                    sub_body = self._extract_body(part)
                    if sub_body:
                        body = sub_body
        else:
            data = payload.get('body', {}).get('data')
            if data:
                body = base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8', errors='replace')
        return body