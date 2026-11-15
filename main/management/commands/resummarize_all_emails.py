import time
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import transaction
from main.models import Email
from main.services.ai_service import AIService


class Command(BaseCommand):
    help = "データベース内のすべてのメールに対してAIによる再要約とカテゴリ再分類を実行します。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--user-id",
            type=int,
            help="特定のユーザーIDのメールのみを再要約する場合に指定します。",
        )
        parser.add_argument(
            "--box",
            type=str,
            help="特定のフォルダ（inbox, sent, junk, trash など）のみを対象にする場合に指定します。",
        )
        parser.add_argument(
            "--batch-size",
            type=int,
            default=10,
            help="1回のバッチ処理で処理する件数（デフォルト: 10）",
        )

    def handle(self, *args, **options):
        user_id = options.get("user_id")
        box_type = options.get("box")
        batch_size = options.get("batch_size")

        queryset = Email.objects.all().order_by("-received_at")

        if user_id:
            User = get_user_model()
            if not User.objects.filter(id=user_id).exists():
                self.stderr.write(self.style.ERROR(f"指定されたユーザーID ({user_id}) は存在しません。"))
                return
            queryset = queryset.filter(user_id=user_id)

        if box_type:
            queryset = queryset.filter(gmail_box=box_type)

        total_count = queryset.count()
        if total_count == 0:
            self.stdout.write(self.style.WARNING("再要約対象のメールが見つかりませんでした。"))
            return

        self.stdout.write(self.style.SUCCESS(f"全 {total_count} 件のメールの再要約・再分類を開始します..."))

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

            try:
                batch_results = ai_service.analyze_emails_batch(chunk_data)
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"バッチ解析中に例外が発生しました: {e}"))
                batch_results = {}

            for email in chunk:
                try:
                    if email.id in batch_results:
                        category, summary = batch_results[email.id]
                    else:
                        # バッチ処理の結果から漏れた場合は個別解析へフォールバック
                        category, summary = ai_service.analyze_email(
                            email.subject, email.body, sender=email.sender
                        )

                    with transaction.atomic():
                        email.category = category
                        email.summary = summary
                        email.save(update_fields=["category", "summary"])

                    processed_count += 1
                except Exception as e:
                    error_count += 1
                    self.stderr.write(self.style.ERROR(f"メールID {email.id} の保存エラー: {e}"))

            self.stdout.write(f"進捗: {min(i + batch_size, total_count)} / {total_count} 件完了")
            time.sleep(0.1)

        self.stdout.write(
            self.style.SUCCESS(
                f"処理完了！ 成功: {processed_count} 件 / エラー: {error_count} 件"
            )
        )