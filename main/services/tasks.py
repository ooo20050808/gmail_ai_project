# tasks.py (JSONパース失敗対策・全ボックス未要約救済・確定フォールバック対応版)

import base64
import json
import logging
import re
import time
from datetime import datetime, timezone as dt_timezone

from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from main.models import Email, GmailAccount
from .ai_service import FAILED_SUMMARY_MESSAGES, AIService

logger = logging.getLogger(__name__)


def _to_model_tab_category(label_list, fallback_tab="primary"):
    labels = set(label_list or [])
    if 'CATEGORY_PROMOTIONS' in labels:
        return "promotions"
    if 'CATEGORY_SOCIAL' in labels:
        return "social"
    if 'CATEGORY_FORUMS' in labels:
        return "forums"
    if 'CATEGORY_UPDATES' in labels:
        return "primary" if 'IMPORTANT' in labels else "updates"
    if 'CATEGORY_PERSONAL' in labels:
        return "primary"

    tab_lower = (fallback_tab or "primary").lower()
    mapping = {
        "primary": "primary",
        "メイン": "primary",
        "updates": "updates",
        "新着": "updates",
        "更新": "updates",
        "promotions": "promotions",
        "プロモーション": "promotions",
        "social": "social",
        "ソーシャル": "social",
        "forums": "forums",
        "フォーラム": "forums",
    }
    return mapping.get(tab_lower, "primary")


def _extract_body_from_payload(payload):
    if not payload:
        return ""

    body_data = payload.get('body', {}).get('data')
    if 'parts' not in payload:
        if body_data:
            return base64.urlsafe_b64decode(body_data.encode('UTF-8')).decode('utf-8', errors='replace')
        return ""

    plain_text = ""
    html_text = ""
    for part in payload.get('parts', []):
        mime_type = part.get('mimeType', '')
        part_data = part.get('body', {}).get('data')
        if mime_type == 'text/plain' and part_data and not plain_text:
            plain_text = base64.urlsafe_b64decode(part_data.encode('UTF-8')).decode('utf-8', errors='replace')
        elif mime_type == 'text/html' and part_data and not html_text:
            html_text = base64.urlsafe_b64decode(part_data.encode('UTF-8')).decode('utf-8', errors='replace')
        elif 'parts' in part:
            nested = _extract_body_from_payload(part)
            if nested:
                if '<' in nested and '>' in nested and not html_text:
                    html_text = nested
                elif not plain_text:
                    plain_text = nested

    return html_text or plain_text or (
        base64.urlsafe_b64decode(body_data.encode('UTF-8')).decode('utf-8', errors='replace')
        if body_data else ""
    )


def _is_valid_summary(summary):
    if not summary:
        return False
    s = summary.strip()
    if not s or s.startswith("失敗") or s in FAILED_SUMMARY_MESSAGES:
        return False
    return True


def _build_fallback_summary(email):
    subject = (email.subject or "(件名なし)").strip()
    raw_body = email.body or ""
    soup = BeautifulSoup(raw_body, "html.parser")
    for tag in soup(["script", "style", "head", "meta", "link"]):
        tag.decompose()
    text = re.sub(r'\s+', ' ', soup.get_text(" ", strip=True)).strip()
    if text:
        return f"【件名】{subject}（概要: {text[:150]}）"
    return f"【件名】{subject}（送信者: {email.sender or '不明'}）"


def background_fetch_and_save_emails(
    user_id,
    box_type="inbox",
    tab_type="primary",
    force_refresh=False,
    limit=500,
    newer_than_timestamp=None,
    **kwargs,
):
    cache_key = f"processing_email_ids_{user_id}"

    try:
        User = get_user_model()
        user = User.objects.get(id=user_id)
        logger.info(
            f"ユーザーID {user_id} のメールバックグラウンド取得・解析処理を開始します。"
            f"(box={box_type}, tab={tab_type}, force_refresh={force_refresh}, newer_than={newer_than_timestamp})"
        )

        actual_inserted_count = 0
        actual_updated_count = 0
        skipped_duplicate_count = 0
        skipped_error_count = 0
        saved_email_ids = []
        normalized_box = (box_type or "inbox").lower()

        try:
            gmail_account = GmailAccount.objects.get(user=user)

            key_base = settings.SECRET_KEY.ljust(32)[:32].encode()
            key = base64.urlsafe_b64encode(key_base)
            f = Fernet(key)
            decrypted_token_json = f.decrypt(gmail_account.app_password.encode()).decode()
            token_data = json.loads(decrypted_token_json)

            credentials = Credentials(
                token=token_data.get('token'),
                refresh_token=token_data.get('refresh_token'),
                token_uri=token_data.get('token_uri'),
                client_id=token_data.get('client_id'),
                client_secret=token_data.get('client_secret'),
                scopes=token_data.get('scopes'),
            )

            service = build('gmail', 'v1', credentials=credentials)
            label_id = "INBOX" if normalized_box == "inbox" else "SENT"

            q_parts = []
            if newer_than_timestamp and not force_refresh:
                safe_ts = max(0, int(newer_than_timestamp) - 300)
                q_parts.append(f"after:{safe_ts}")
            q_str = " ".join(q_parts).strip()

            messages = []
            page_token = None
            max_list_scan = max(int(limit or 500) * 2, 500)

            while len(messages) < max_list_scan:
                list_params = {
                    'userId': 'me',
                    'maxResults': min(200, max_list_scan - len(messages)),
                    'labelIds': [label_id],
                }
                if q_str:
                    list_params['q'] = q_str
                if page_token:
                    list_params['pageToken'] = page_token

                try:
                    results = service.users().messages().list(**list_params).execute()
                except Exception as list_e:
                    if "invalid_grant" in str(list_e):
                        logger.error(f"Google認証トークン期限切れ（ユーザーID: {user_id}）。再連携が必要です。")
                        break
                    logger.error(f"Gmail messages.list 取得エラー: {list_e}")
                    time.sleep(3.0)
                    break

                msgs = results.get('messages', [])
                if not msgs:
                    break
                messages.extend(msgs)

                page_token = results.get('nextPageToken')
                if not page_token:
                    break
                time.sleep(0.2)

            logger.info(f"Gmail APIから見つかったメッセージ総数（ID一覧）: {len(messages)}件。")

            existing_message_ids = set(
                Email.objects.filter(user=user, gmail_box=normalized_box)
                .values_list('message_id', flat=True)
            )

            new_messages = []
            for m in messages:
                raw_gmail_id = m['id']
                prefixed_id = f"gm-{raw_gmail_id}"
                if raw_gmail_id in existing_message_ids or prefixed_id in existing_message_ids:
                    skipped_duplicate_count += 1
                else:
                    new_messages.append(m)

            logger.info(f"📊 【スキップ集計】既存データと重複のためスキップした件数: {skipped_duplicate_count} 件")
            logger.info(f"新しく取得・保存対象となる総メール件数: {len(new_messages)}件")

            batch_fetch_limit = min(int(limit or 500), 400)
            target_new_messages = new_messages[:batch_fetch_limit]

            for msg_meta in target_new_messages:
                msg_id = msg_meta['id']
                success = False
                for attempt in range(3):
                    try:
                        msg_data = service.users().messages().get(
                            userId='me', id=msg_id, format='full'
                        ).execute()

                        payload = msg_data.get('payload', {})
                        headers = payload.get('headers', [])

                        subject = "(件名なし)"
                        sender = ""
                        header_msg_id = msg_id
                        for header in headers:
                            name = header.get('name', '').lower()
                            if name == 'subject':
                                subject = (header.get('value', '') or "(件名なし)")[:255]
                            elif name in ('from', 'sender'):
                                sender = (header.get('value', '') or "")[:255]
                            elif name == 'message-id':
                                header_msg_id = header.get('value', '') or msg_id

                        body = _extract_body_from_payload(payload)
                        model_tab = _to_model_tab_category(msg_data.get('labelIds', []), fallback_tab=tab_type)

                        internal_date_ms = int(msg_data.get('internalDate', time.time() * 1000))
                        utc_dt = datetime.fromtimestamp(internal_date_ms / 1000.0, tz=dt_timezone.utc)
                        received_at = utc_dt.astimezone(timezone.get_current_timezone())

                        clean_header_id = header_msg_id.strip().strip('<>') if header_msg_id else msg_id
                        normalized_message_id = f"gm-{clean_header_id}"[:255]
                        raw_thread_id = msg_data.get('threadId', msg_id)
                        normalized_thread_id = (f"th-{raw_thread_id}" if raw_thread_id else f"th-{msg_id}")[:255]

                        existing_obj = Email.objects.filter(
                            message_id__in=[normalized_message_id, f"gm-{msg_id}", msg_id],
                        ).first()

                        if existing_obj:
                            if existing_obj.user_id == user.id:
                                existing_obj.gmail_account = existing_obj.gmail_account or gmail_account
                                existing_obj.thread_id = existing_obj.thread_id or normalized_thread_id
                                existing_obj.subject = subject
                                existing_obj.sender = sender
                                existing_obj.body = body or existing_obj.body
                                existing_obj.received_at = received_at
                                existing_obj.tab_category = model_tab
                                existing_obj.gmail_box = normalized_box
                                existing_obj.save()
                                actual_updated_count += 1
                                saved_email_ids.append(existing_obj.id)
                            else:
                                unique_msg_id = f"gm-{user.id}-{msg_id}"[:255]
                                email_obj = Email.objects.create(
                                    user=user,
                                    gmail_account=gmail_account,
                                    message_id=unique_msg_id,
                                    thread_id=normalized_thread_id,
                                    subject=subject,
                                    sender=sender,
                                    body=body,
                                    received_at=received_at,
                                    tab_category=model_tab,
                                    gmail_box=normalized_box,
                                )
                                actual_inserted_count += 1
                                saved_email_ids.append(email_obj.id)
                        else:
                            email_obj = Email.objects.create(
                                user=user,
                                gmail_account=gmail_account,
                                message_id=normalized_message_id,
                                thread_id=normalized_thread_id,
                                subject=subject,
                                sender=sender,
                                body=body,
                                received_at=received_at,
                                tab_category=model_tab,
                                gmail_box=normalized_box,
                            )
                            actual_inserted_count += 1
                            saved_email_ids.append(email_obj.id)

                        success = True
                        time.sleep(0.35)
                        break

                    except Exception as inner_e:
                        error_str = str(inner_e)
                        if "Quota exceeded" in error_str or "rateLimitExceeded" in error_str or "403" in error_str or "429" in error_str:
                            sleep_time = 10 * (attempt + 1)
                            logger.warning(f"API制限検知。{sleep_time}秒後に再試行します (ID: {msg_id})...")
                            time.sleep(sleep_time)
                        else:
                            logger.error(f"⚠️ メッセージID {msg_id} の詳細取得エラー（例外スキップ）: {inner_e}")
                            break

                if not success:
                    skipped_error_count += 1

            logger.info(
                f"✅ 実際に新規作成されたメール件数: {actual_inserted_count} 件 "
                f"(更新件数: {actual_updated_count}件)"
            )
            logger.info(
                f"📊 【今回のまとめ】重複スキップ: {skipped_duplicate_count}件 / "
                f"エラースキップ: {skipped_error_count}件"
            )

        except Exception as e:
            logger.error(f"Gmail APIからのメール取得プロセス全体でエラーが発生しました: {e}", exc_info=True)

        # 2. 処理対象メールの抽出（全ボックスの未要約・失敗分も対象に含める）
        base_query = Email.objects.filter(user=user)

        if force_refresh:
            target_emails = base_query.filter(gmail_box=normalized_box).order_by("-received_at")
            logger.info("【強制再要約モード】すべてのメールをAI解析対象として再処理します。")
        else:
            target_emails = base_query.filter(
                Q(id__in=saved_email_ids) |
                Q(summary__isnull=True) |
                Q(summary="") |
                Q(summary__startswith="失敗") |
                Q(summary__in=list(FAILED_SUMMARY_MESSAGES))
            ).order_by("-received_at")

        target_ids = [email.id for email in target_emails]
        cache.set(cache_key, target_ids, timeout=1800)
        logger.info(f"AI解析対象のメール総数: {len(target_ids)}件。バッチ処理を開始します。")

        if not target_ids:
            logger.info(f"ユーザーID {user_id} の処理対象メールはありませんでした。")
            return

        # 3. AIServiceインスタンスの生成
        ai_service = AIService()

        # 4. 5件ずつのチャンクで処理し、失敗時は確定フォールバック要約を保存
        batch_size = 5
        email_queryset = list(Email.objects.filter(id__in=target_ids).order_by("-received_at"))

        for i in range(0, len(email_queryset), batch_size):
            chunk = email_queryset[i:i + batch_size]
            chunk_data = [
                {
                    "id": email.id,
                    "subject": email.subject,
                    "body": email.body,
                    "sender": email.sender,
                }
                for email in chunk
            ]

            batch_results = ai_service.analyze_emails_batch(chunk_data)

            for email in chunk:
                try:
                    if email.id in batch_results:
                        category, summary = batch_results[email.id]
                    else:
                        category, summary = ai_service.analyze_email(
                            email.subject, email.body, sender=email.sender
                        )

                    if not _is_valid_summary(summary):
                        logger.warning(
                            f"⚠️ メールID {email.id} のAI要約が失敗文言だったためフォールバック要約を確定保存します。"
                        )
                        summary = _build_fallback_summary(email)

                    with transaction.atomic():
                        email.category = category or email.category or "other"
                        email.summary = summary
                        email.summarized_at = timezone.now()
                        email.save(update_fields=['category', 'summary', 'summarized_at'])
                except Exception as e:
                    logger.error(f"メールID: {email.id} の保存エラー: {e}")

            time.sleep(0.05)

        cache.delete(cache_key)
        logger.info(f"🎉 ユーザーID {user_id} のメール一括バッチAI解析がすべて完了しました！")

    except Exception as e:
        logger.error(f"バックグラウンドメール処理全体のエラー (ユーザーID: {user_id}): {e}", exc_info=True)