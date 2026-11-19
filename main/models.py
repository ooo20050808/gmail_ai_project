import os
from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.db import models


def get_encryption_key():
  """暗号化キーを取得または生成"""
  key = os.environ.get("ENCRYPTION_KEY")
  if not key:
    key_file = os.path.join(os.path.dirname(__file__), ".encryption_key")
    if os.path.exists(key_file):
      with open(key_file, "rb") as f:
        key = f.read()
    else:
      key = Fernet.generate_key()
      with open(key_file, "wb") as f:
        f.write(key)
  return key


def encrypt_password(password):
  """パスワードを暗号化"""
  if not password:
    return password
  key = get_encryption_key()
  fernet = Fernet(key)
  return fernet.encrypt(password.encode()).decode()


def decrypt_password(encrypted_password):
  """パスワードを復号化"""
  if not encrypted_password:
    return encrypted_password
  try:
    key = get_encryption_key()
    fernet = Fernet(key)
    return fernet.decrypt(encrypted_password.encode()).decode()
  except Exception:
    return encrypted_password


class GmailAccount(models.Model):
  user = models.OneToOneField(User, on_delete=models.CASCADE)
  gmail_address = models.EmailField()
  app_password = models.TextField()
  imap_enabled = models.BooleanField(default=True)
  created_at = models.DateTimeField(auto_now_add=True)
  updated_at = models.DateTimeField(auto_now=True)

  def set_app_password(self, raw_password):
    """生のパスワードを暗号化して保存"""
    self.app_password = encrypt_password(raw_password)

  def get_app_password(self):
    """暗号化されたパスワードを復号化して取得"""
    return decrypt_password(self.app_password)

  def __str__(self):
    return self.gmail_address


class Email(models.Model):
  PRIORITY_CHOICES = [
      ("low", "低"),
      ("medium", "中"),
      ("high", "高"),
      ("urgent", "緊急"),
  ]

  CATEGORY_CHOICES = [
      ("agent", "転職エージェント"),
      ("scout", "直接スカウト"),
      ("company", "企業直接"),
      ("service", "サービス通知"),
      ("other", "その他"),
  ]

  GMAIL_BOX_CHOICES = [
      ("inbox", "受信トレイ"),
      ("sent", "送信済み"),
      ("junk", "迷惑メール"),
      ("trash", "ゴミ箱"),
  ]

  TAB_CATEGORY_CHOICES = [
      ("primary", "メイン"),
      ("social", "ソーシャル"),
      ("updates", "更新"),
      ("promotions", "プロモーション"),
      ("forums", "フォーラム"),
  ]

  user = models.ForeignKey(
      User, on_delete=models.CASCADE, related_name="emails"
  )
  gmail_account = models.ForeignKey(
      GmailAccount,
      on_delete=models.CASCADE,
      related_name="emails",
      null=True,
      blank=True,
  )
  message_id = models.CharField(max_length=255, unique=True)
  thread_id = models.CharField(
      max_length=255, db_index=True, blank=True, null=True
  )
  subject = models.CharField(max_length=255)
  sender = models.CharField(max_length=255)
  body = models.TextField()
  summary = models.TextField(blank=True, null=True)
  summarized_at = models.DateTimeField(
      blank=True, null=True, db_index=True, verbose_name="要約日時"
  )
  reply_draft = models.TextField(blank=True, null=True)
  priority = models.CharField(
      max_length=10, choices=PRIORITY_CHOICES, default="medium"
  )
  category = models.CharField(
      max_length=20, choices=CATEGORY_CHOICES, default="other"
  )
  gmail_box = models.CharField(
      max_length=20, choices=GMAIL_BOX_CHOICES, default="inbox"
  )
  tab_category = models.CharField(
      max_length=20, choices=TAB_CATEGORY_CHOICES, default="primary"
  )
  is_read = models.BooleanField(default=False)
  received_at = models.DateTimeField()
  created_at = models.DateTimeField(auto_now_add=True)

  class Meta:
    ordering = ["-received_at"]

  def __str__(self):
    return self.subject


class KeywordAlert(models.Model):
  """キーワードアラート設定"""

  user = models.ForeignKey(
      User, on_delete=models.CASCADE, related_name="keyword_alerts"
  )
  keyword = models.CharField(max_length=100)
  is_active = models.BooleanField(default=True)
  created_at = models.DateTimeField(auto_now_add=True)

  class Meta:
    unique_together = ["user", "keyword"]

  def __str__(self):
    return self.keyword