# admin.py
from django.contrib import admin
from django.contrib.admin import AdminSite
from .forms import EmailAdminAuthenticationForm
from .models import GmailAccount, Email, KeywordAlert

# Django標準のデフォルト管理サイトのログインフォームをカスタムフォームに差し替える
admin.site.login_form = EmailAdminAuthenticationForm

# 管理画面にモデルを登録
admin.site.register(GmailAccount)
admin.site.register(Email)
admin.site.register(KeywordAlert)