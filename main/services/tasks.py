# tasks.py (重複スキップと実際の保存件数の不一致を解消した正確な同期・要約版)

import base64
import json
import logging
import time
from django.utils import timezone

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
import django.db.utils
from django_q.tasks import async_task

from main.models import Email, GmailAccount
from .ai_service import FAILED_SUMMARY_MESSAGES, AIService
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)


def background_fetch_and_save_emails(user_id, box_type="inbox", tab_type="primary", force_refresh=False, **kwargs):
    """
    Gmailから未取得のメールを安全にバッチ取得し、実際の新規作成件数を正確にカウントしながら同期・要約を行うタスク
    """
    try:
        User = get_user_model()
        user = User.objects.get(id=user_id)
        logger.info(f"ユーザーID {user_id} のメールバックグラウンド取得・解析処理を開始します。(強制再要約: {force_refresh})")

        actual_inserted_count = 0
        actual_updated_count = 0
        skipped_duplicate_count = 0
        skipped_error_count = 0
        saved_email_ids = []

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
            label_id = "INBOX" if box_type == "inbox" else "SENT"

            q_param = ""
            if box_type == "inbox" and tab_type and tab_type != "all":
                tab_lower = tab_type.lower()
                if tab_lower in ["primary", "メイン"]:
                    q_param = "category:primary"
                elif tab_lower in ["updates", "新着"]:
                    q_param = "category:updates"
                elif tab_lower in ["promotions", "プロモーション"]:
                    q_param = "category:promotions"
                elif tab_lower in ["social", "ソーシャル"]:
                    q_param = "category:social"
                elif tab_lower in ["forums", "フォーラム"]:
                    q_param = "category:forums"

            messages = []
            page_token = None

            # メッセージID一覧の取得（未取得の過去メールを取りこぼさないよう最大2000件まで拡張）
            while len(messages) < 2000:
                list_params = {
                    'userId': 'me',
                    'maxResults': 200,
                    'labelIds': [label_id],
                }
                if q_param:
                    list_params['q'] = q_param
                if page_token:
                    list_params['pageToken'] = page_token

                try:
                    results = service.users().messages().list(**list_params).execute()
                except Exception as list_e:
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
                Email.objects.filter(user=user, gmail_box=box_type)
                .values_list('message_id', flat=True)
            )

            new_messages = []
            for m in messages:
                if m['id'] in existing_message_ids:
                    skipped_duplicate_count += 1
                else:
                    new_messages.append(m)

            logger.info(f"📊 【スキップ集計】既存データと重複のためスキップした件数: {skipped_duplicate_count} 件")
            logger.info(f"新しく取得・保存対象となる総メール件数: {len(new_messages)}件")

            # 1回のタスク実行で詳細を取得する上限を 100件 に設定して確実な保存を担保
            batch_fetch_limit = 400
            target_new_messages = new_messages[:batch_fetch_limit]
            if len(new_messages) > batch_fetch_limit:
                logger.info(f"API制限回避のため、今回は先頭の {batch_fetch_limit} 件の詳細を取得します（残りは次回の同期で自動取得されます）。")

            for idx, msg_meta in enumerate(target_new_messages):
                msg_id = msg_meta['id']
                success = False
                for attempt in range(3):
                    try:
                        msg_data = service.users().messages().get(userId='me', id=msg_id, format='full').execute()

                        payload = msg_data.get('payload', {})
                        headers = payload.get('headers', [])

                        subject = ""
                        sender = ""
                        for header in headers:
                            name = header.get('name', '').lower()
                            if name == 'subject':
                                subject = header.get('value', '')
                            elif name == 'from':
                                sender = header.get('value', '')

                        body = ""
                        if 'body' in payload and payload['body'].get('data'):
                            body = base64.urlsafe_b64decode(payload['body']['data'].encode('UTF-8')).decode('utf-8', errors='ignore')
                        elif 'parts' in payload:
                            def extract_text(parts):
                                text = ""
                                for part in parts:
                                    mime_type = part.get('mimeType', '')
                                    if mime_type == 'text/plain':
                                        data = part.get('body', {}).get('data')
                                        if data:
                                            text += base64.urlsafe_b64decode(data.encode('UTF-8')).decode('utf-8', errors='ignore')
                                    elif 'parts' in part:
                                        text += extract_text(part['parts'])
                                return text
                            body = extract_text(payload['parts'])

                        internal_date = int(msg_data.get('internalDate', time.time() * 1000)) / 1000.0
                        received_at = timezone.make_aware(timezone.datetime.fromtimestamp(internal_date))

                        # 実際の作成・更新を厳密に判定
                        email_obj, created = Email.objects.update_or_create(
                            user=user,
                            message_id=msg_id,
                            defaults={
                                "thread_id": msg_data.get('threadId'),
                                "subject": subject,
                                "sender": sender,
                                "body": body,
                                "received_at": received_at,
                                "tab_category": tab_type,
                                "gmail_box": box_type,
                            }
                        )
                        if created:
                            actual_inserted_count += 1
                        else:
                            actual_updated_count += 1

                        saved_email_ids.append(email_obj.id)
                        success = True

                        time.sleep(0.4)
                        break

                    except Exception as inner_e:
                        error_str = str(inner_e)
                        if "Quota exceeded" in error_str or "rateLimitExceeded" in error_str or "403" in error_str:
                            sleep_time = 10 * (attempt + 1)
                            logger.warning(f"API制限検知。{sleep_time}秒後に再試行します (ID: {msg_id})...")
                            time.sleep(sleep_time)
                        else:
                            logger.error(f"⚠️ メッセージID {msg_id} の詳細取得エラー（例外スキップ）: {inner_e}")
                            break

                if not success and attempt == 2:
                    skipped_error_count += 1
                    logger.error(f"❌ メッセージID {msg_id} の取得に連続失敗したため、エラーとしてスキップしました（累計エラー数: {skipped_error_count}件）。")

            logger.info(f"✅ 実際に新規作成されたメール件数: {actual_inserted_count} 件 (更新件数: {actual_updated_count}件)")
            logger.info(f"📊 【今回のまとめ】重複スキップ: {skipped_duplicate_count}件 / エラースキップ: {skipped_error_count}件")

        except Exception as e:
            logger.error(f"Gmail APIからのメール取得プロセス全体でエラーが発生しました: {e}")

        # 2. 処理対象メールの抽出（未要約、または強制リフレッシュ）
        base_query = Email.objects.filter(user=user, gmail_box=box_type)

        if force_refresh:
            target_emails = base_query.order_by("-received_at")
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

        cache_key = f"processing_email_ids_{user_id}"
        cache.set(cache_key, target_ids, timeout=1800)
        logger.info(f"AI解析対象のメール総数: {len(target_ids)}件。バッチ処理を開始します。")

        if not target_ids:
            logger.info(f"ユーザーID {user_id} の処理対象メールはありませんでした。")
            return

        # 3. AIServiceインスタンスの生成
        ai_service = AIService()

        # 4. 10件ずつのグループ（チャンク）に分割してバッチ処理
        batch_size = 10
        email_queryset = list(Email.objects.filter(id__in=target_ids))

        for i in range(0, len(email_queryset), batch_size):
            chunk = email_queryset[i:i + batch_size]
            chunk_data = [
                {
                    "id": email.id,
                    "subject": email.subject,
                    "body": email.body,
                    "sender": email.sender
                }
                for email in chunk
            ]

            # LLMにまとめてリクエスト
            batch_results = ai_service.analyze_emails_batch(chunk_data)

            # 結果をデータベースに保存
            for email in chunk:
                try:
                    if email.id in batch_results:
                        category, summary = batch_results[email.id]
                        with transaction.atomic():
                            email.category = category
                            email.summary = summary
                            email.save(update_fields=['category', 'summary'])
                    else:
                        # バッチ解析から漏れた場合のフォールバック（1件ずつ個別解析）
                        category, summary = ai_service.analyze_email(
                            email.subject, email.body, sender=email.sender
                        )
                        with transaction.atomic():
                            email.category = category
                            email.summary = summary
                            email.save(update_fields=['category', 'summary'])
                except Exception as e:
                    logger.error(f"メールID: {email.id} の保存エラー: {e}")

            time.sleep(0.05)

        # 5. 処理完了後にキャッシュをクリア
        cache.delete(cache_key)
        logger.info(f"🎉 ユーザーID {user_id} のメール一括バッチAI解析がすべて完了しました！")

    except Exception as e:
        logger.error(f"バックグラウンドメール処理全体のエラー (ユーザーID: {user_id}): {e}")