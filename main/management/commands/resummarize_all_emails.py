# resummarize_all_emails.py (要約日時記録・進捗確実カウント版)

import os
import time
import sys
from datetime import timedelta
from dotenv import load_dotenv
from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from main.models import Email
from main.services.ai_service import AIService, DEFAULT_MODEL, FAILED_SUMMARY_MESSAGES

# .envファイルから環境変数を読み込む
load_dotenv()


class Command(BaseCommand):
    help = "現在受信しているすべてのメールに対してAIによる再要約とカテゴリ再分類を実行し、完了日時を記録します。"

    def add_arguments(self, parser):
        parser.add_argument(
            "--batch-size",
            type=int,
            default=3,
            help="1回のバッチ処理で処理する件数（デフォルト: 3）",
        )
        parser.add_argument(
            "--only-failed",
            action="store_true",
            help="指定した場合のみ、未要約または失敗したメールだけを対象にします",
        )
        parser.add_argument(
            "--skip-recent-hours",
            type=float,
            default=0,
            help="指定した時間（時間単位）以内に既に再要約済みのメールをスキップして続きから実行します（例: --skip-recent-hours 24）",
        )
        parser.add_argument(
            "--model",
            type=str,
            default=None,
            help="使用するOllamaモデル名（未指定時は .env の OLLAMA_MODEL を使用）",
        )

    def handle(self, *args, **options):
        # 実行時にも最新の .env を確実に反映
        load_dotenv()

        batch_size = options.get("batch_size")
        only_failed = options.get("only_failed")
        skip_recent_hours = options.get("skip_recent_hours")

        # 優先順位: コマンドライン引数 --model > .env (OLLAMA_MODEL) > DEFAULT_MODEL
        env_model = (os.getenv("OLLAMA_MODEL") or "").strip()
        target_model = options.get("model") or env_model or DEFAULT_MODEL

        # コマンド実行開始時刻を記録
        run_started_at = timezone.now()

        # 対象クエリセットの構築
        queryset = Email.objects.all().order_by("-received_at")

        if only_failed:
            queryset = queryset.filter(
                Q(summary__isnull=True) |
                Q(summary="") |
                Q(summary__in=FAILED_SUMMARY_MESSAGES)
            )
        elif skip_recent_hours > 0:
            cutoff = run_started_at - timedelta(hours=skip_recent_hours)
            queryset = queryset.filter(
                Q(summarized_at__isnull=True) | Q(summarized_at__lt=cutoff)
            )

        target_ids = list(queryset.values_list("id", flat=True))
        total_count = len(target_ids)

        if total_count == 0:
            self.stdout.write(self.style.SUCCESS("対象となるメールはありません。"))
            return

        self.stdout.write(
            self.style.SUCCESS(
                f"使用モデル [{target_model}] で対象メール全 {total_count} 件の再要約・再分類を開始します..."
            )
        )

        ai_service = AIService(model=target_model, diagnose=True)
        completed_count = 0
        processed_count = 0
        error_count = 0

        for i in range(0, total_count, batch_size):
            chunk_ids = target_ids[i:i + batch_size]
            chunk_emails = list(Email.objects.filter(id__in=chunk_ids).order_by("-received_at"))

            chunk_data = [
                {
                    "id": email.id,
                    "subject": email.subject,
                    "body": email.body,
                    "sender": email.sender,
                }
                for email in chunk_emails
            ]

            batch_results = {}
            try:
                batch_results = ai_service.analyze_emails_batch(chunk_data)
            except Exception as e:
                self.stderr.write(
                    self.style.ERROR(f"\n[警告] バッチ解析中に例外が発生しました（個別解析へ切り替えます）: {e}")
                )

            for email in chunk_emails:
                try:
                    category = None
                    summary = None

                    if email.id in batch_results and batch_results[email.id]:
                        category, summary = batch_results[email.id]

                    if not summary or summary in FAILED_SUMMARY_MESSAGES:
                        category, summary = ai_service.analyze_email(
                            email.subject, email.body, sender=email.sender
                        )

                    # DBへの保存（summarized_at に現在時刻を記録）
                    with transaction.atomic():
                        email.category = category
                        email.summary = summary
                        email.summarized_at = timezone.now()
                        email.save(update_fields=["category", "summary", "summarized_at"])

                    if summary in FAILED_SUMMARY_MESSAGES:
                        error_count += 1
                    else:
                        processed_count += 1

                except Exception as e:
                    error_count += 1
                    self.stderr.write(
                        self.style.ERROR(f"\n[エラー] メールID {email.id} の処理・保存中にエラー: {e}")
                    )
                finally:
                    completed_count += 1
                    # バッファリングで出力が止まらないよう print(..., flush=True) を使用
                    print(
                        f"\r進捗: {completed_count} / {total_count} 件完了 "
                        f"(成功: {processed_count}, 失敗/エラー: {error_count})",
                        end="",
                        flush=True,
                    )

            time.sleep(0.1)

        # DB上の実績件数を最終確認
        actual_updated_in_db = Email.objects.filter(summarized_at__gte=run_started_at).count()

        self.stdout.write(
            self.style.SUCCESS(
                f"\n\n全件処理完了！ 完了総数: {completed_count} / {total_count} 件 "
                f"(成功: {processed_count} 件 / 失敗・エラー: {error_count} 件 / DB記録確認済: {actual_updated_in_db} 件)"
            )
        )