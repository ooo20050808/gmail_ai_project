# resummarize_all_emails.py (自動で失敗分を対象にする安全版)

import time
import sys
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from main.models import Email
from main.services.ai_service import AIService, FAILED_SUMMARY_MESSAGES


class Command(BaseCommand):
    help = "要約が失敗している、または未処理のメールに対してAIによる再要約とカテゴリ再分類を自動実行します。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=3,
            help="1回のバッチ処理で処理する件数（デフォルト: 3）",
        )

    def handle(self, *args, **options):
        batch_size = options.get("batch_size")

        # シェルで確認された失敗メッセージ入り、または未要約のメールのみを自動ターゲットにする
        queryset = Email.objects.filter(
            Q(summary__isnull=True) |
            Q(summary="") |
            Q(summary__in=FAILED_SUMMARY_MESSAGES)
        ).order_by("-received_at")

        total_count = queryset.count()
        if total_count == 0:
            self.stdout.write(self.style.SUCCESS("対象となるメールはありません。すべてのメールが正常に要約されています！"))
            return

        self.stdout.write(self.style.SUCCESS(f"要約が必要なメール全 {total_count} 件の再要約・再分類を開始します..."))

        ai_service = AIService(diagnose=True)
        email_list = list(queryset)
        processed_count = 0
        error_count = 0

        for i in range(0, total_count, batch_size):
            chunk = email_list[i:i + batch_size]
            chunk_data = [
                {
                    "id": email.id,
                    "subject": email.subject,
                    "body": email.body,
                    "sender": email.sender,
                }
                for email in chunk
            ]

            batch_results = {}
            try:
                batch_results = ai_service.analyze_emails_batch(chunk_data)
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"\n[警告] バッチ解析中に例外が発生しました（個別解析へフォールバックします）: {e}"))

            for email in chunk:
                try:
                    if email.id in batch_results and batch_results[email.id]:
                        category, summary = batch_results[email.id]
                    else:
                        category, summary = ai_service.analyze_email(
                            email.subject, email.body, sender=email.sender
                        )

                    with transaction.atomic():
                        email.category = category
                        email.summary = summary
                        email.save(update_fields=["category", "summary"])

                    if summary in FAILED_SUMMARY_MESSAGES:
                        error_count += 1
                    else:
                        processed_count += 1
                except Exception as e:
                    error_count += 1
                    self.stderr.write(self.style.ERROR(f"\n[エラー] メールID {email.id} の保存エラー: {e}"))

            completed_so_far = min(i + batch_size, total_count)
            sys.stdout.write(f"\r進捗: {completed_so_far} / {total_count} 件完了 (成功: {processed_count}, 失敗/エラー: {error_count})")
            sys.stdout.flush()
            time.sleep(0.1)

        self.stdout.write(
            self.style.SUCCESS(
                f"\n\n処理完了！ 成功: {processed_count} 件 / 失敗・エラー: {error_count} 件"
            )
        )