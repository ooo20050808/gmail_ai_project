from django.core.management.base import BaseCommand
from django.utils import timezone
from datetime import datetime, timezone as dt_timezone
from main.models import Email


class Command(BaseCommand):
    help = 'データベースに保存されているすべてのメールの受信日時（received_at）のタイムゾーンおよび時刻を修正します。'

    def handle(self, *args, **options):
        emails = Email.objects.all()
        total_count = emails.count()
        self.stdout.write(f"合計 {total_count} 件のメールの日時修正を開始します...")

        updated_count = 0
        for email in emails:
            if email.received_at:
                # 既存の received_at が naive（タイムゾーン情報なし）な場合や、
                # 誤った解釈がされている場合の補正を行います。
                # 例: データベース内の時刻が UTC 扱いになってしまっているものを JST (+9:00) に正しく変換する例

                dt = email.received_at

                # もしタイムゾーン情報がない場合は、一旦UTCとみなしてローカルタイムゾーン（Asia/Tokyo）へ変換
                if timezone.is_naive(dt):
                    aware_dt = timezone.make_aware(dt, dt_timezone.utc)
                    email.received_at = aware_dt.astimezone(timezone.get_current_timezone())
                    email.save(update_fields=['received_at'])
                    updated_count += 1
                else:
                    # すでにawareな場合でも、タイムゾーンのズレを強制的にJSTに統一・再解釈する場合
                    current_tz = timezone.get_current_timezone()
                    email.received_at = dt.astimezone(current_tz)
                    email.save(update_fields=['received_at'])
                    updated_count += 1

        self.stdout.write(self.style.SUCCESS(f"完了しました。合計 {updated_count} 件のメール日時を修正しました。"))