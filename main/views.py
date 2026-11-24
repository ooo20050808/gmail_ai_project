import base64
import html
import json
import logging
import re
import traceback
from datetime import timedelta

import bleach  # <-- 【追加】ここで bleach をインポートします
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db.models import Case, Count, IntegerField, Q, Value, When
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.html import urlize
from django.utils import timezone
from django_q.tasks import async_task
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from .forms import GmailAccountForm
from .models import Email, GmailAccount, KeywordAlert
from .services.ai_service import FAILED_SUMMARY_MESSAGES, AIService
from .services.gmail_api_service import GmailApiService
from .forms import GmailAccountForm, EmailOrUsernameAuthenticationForm

# HTMLメールのスタイルを維持するための許可リスト設定
ALLOWED_TAGS = [
    'a', 'abbr', 'acronym', 'b', 'blockquote', 'br', 'code', 'div', 'em',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'hr', 'i', 'li', 'ol', 'p', 'pre',
    'span', 'strong', 'table', 'tbody', 'td', 'th', 'thead', 'tr', 'u', 'ul',
    'img', 'style', 'font', 'center'
]

ALLOWED_ATTRIBUTES = {
    'a': ['href', 'title', 'target', 'rel'],
    'img': ['src', 'alt', 'width', 'height', 'style', 'align', 'border'],
    '*': ['style', 'class', 'id', 'align', 'valign', 'bgcolor', 'color', 'width', 'height', 'cellspacing', 'cellpadding', 'border'],
}

ALLOWED_STYLES = [
    'color', 'background-color', 'font-family', 'font-size', 'font-weight',
    'text-align', 'text-decoration', 'width', 'height', 'margin', 'margin-top',
    'margin-right', 'margin-bottom', 'margin-left', 'padding', 'padding-top',
    'padding-right', 'padding-bottom', 'padding-left', 'border', 'border-top',
    'border-right', 'border-bottom', 'border-left', 'display', 'line-height'
]

logger = logging.getLogger(__name__)


@login_required
def gmail_auth_start(request):
    current_host = request.get_host()
    dynamic_redirect_uri = f"http://{current_host}/auth/callback/"

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [dynamic_redirect_uri],
            }
        },
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        redirect_uri=dynamic_redirect_uri
    )

    authorization_url, state = flow.authorization_url(
        access_type='offline',
        include_granted_scopes='true',
        prompt='consent'
    )

    cache.set(f"oauth_verifier_{state}", flow.code_verifier, timeout=600)
    return redirect(authorization_url)


@login_required
def oauth2_callback(request):
    state = request.GET.get('state')
    current_host = request.get_host()
    dynamic_redirect_uri = f"http://{current_host}/auth/callback/"

    if not state:
        messages.error(request, "不正なリクエストです（stateがありません）。")
        return redirect("gmail_settings")

    code_verifier = cache.get(f"oauth_verifier_{state}")

    if not code_verifier:
        logger.error("OAuthキャッシュから code_verifier が取得できませんでした。")
        messages.error(request, "認証セッションの有効期限が切れたか、無効なリクエストです。もう一度やり直してください。")
        return redirect("gmail_settings")

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": settings.GOOGLE_OAUTH_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [dynamic_redirect_uri],
            }
        },
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        state=state,
        redirect_uri=dynamic_redirect_uri
    )

    flow.code_verifier = code_verifier
    try:
        flow.fetch_token(authorization_response=request.build_absolute_uri())
    except Exception as e:
        logger.error(f"トークン取得エラー: {e}")
        messages.error(request, "Googleトークンの取得に失敗しました。")
        return redirect("gmail_settings")

    credentials = flow.credentials
    cache.delete(f"oauth_verifier_{state}")

    # 1. Gmailプロフィール（メールアドレス）の取得テスト
    gmail_address = ""
    try:
        gmail_service = build('gmail', 'v1', credentials=credentials)
        profile = gmail_service.users().getProfile(userId='me').execute()
        gmail_address = profile.get('emailAddress', '')
        logger.info(f"【DEBUG】Gmailアドレス取得成功: {gmail_address}")
    except Exception as e:
        logger.error(f"【DEBUG】Gmailプロフィールの取得に失敗しました: {e}")

    token_data = {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes,
    }
    token_json_str = json.dumps(token_data)

    key_base = settings.SECRET_KEY.ljust(32)[:32].encode()
    key = base64.urlsafe_b64encode(key_base)
    f = Fernet(key)
    encrypted_token = f.encrypt(token_json_str.encode()).decode()

    # 2. データベースへの保存処理とデバッグ出力
    try:
        account, created = GmailAccount.objects.get_or_create(user=request.user)
        account.app_password = encrypted_token
        if gmail_address:
            account.gmail_address = gmail_address
        account.save()
        logger.info(f"【DEBUG】GmailAccount保存完了 (User ID: {request.user.id}, Created: {created}, Email: {account.gmail_address})")
    except Exception as e:
        logger.error(f"【DEBUG】GmailAccountの保存中に例外が発生しました: {e}")

    messages.success(request, "Googleアカウントの連携が完了しました。")
    return redirect("dashboard")


def _generate_and_save_summary(email_obj, regenerate=False):
    if not AIService.needs_summary(email_obj, regenerate=regenerate):
        return True

    ai_service = AIService()
    category, summary = ai_service.analyze_email(
        subject=email_obj.subject,
        email_body=email_obj.body,
        sender=email_obj.sender,
    )

    if summary and summary not in FAILED_SUMMARY_MESSAGES:
        email_obj.category = category
        email_obj.summary = summary
        email_obj.summarized_at = timezone.now()
        email_obj.save(update_fields=["category", "summary", "summarized_at"])
        return True

    email_obj.category = category or email_obj.category
    email_obj.summary = summary or "要約の生成に失敗しました。"
    email_obj.summarized_at = timezone.now()
    email_obj.save(update_fields=["category", "summary", "summarized_at"])
    logger.error(f"メールID {email_obj.id} の要約生成に失敗しました。")
    return False

def custom_login(request):
    if request.user.is_authenticated:
        return redirect("dashboard")

    if request.method == "POST":
        # カスタムフォームに変更
        form = EmailOrUsernameAuthenticationForm(request, data=request.POST)
        if form.is_valid():
            user = form.get_user()
            login(request, user)
            next_url = request.GET.get("next") or request.POST.get("next") or "dashboard"
            return redirect(next_url)
        else:
            messages.error(request, "ユーザー名(またはメールアドレス)またはパスワードが正しくありません")
    else:
        form = EmailOrUsernameAuthenticationForm()

    return render(request, "base.html", {"form": form})


@login_required
def custom_logout(request):
    logout(request)
    return redirect("login")


@login_required
def gmail_settings(request):
    try:
        gmail_account = GmailAccount.objects.get(user=request.user)
        initial_data = {
            "gmail_address": gmail_account.gmail_address,
            "app_password": "",
        }
    except GmailAccount.DoesNotExist:
        gmail_account = None
        initial_data = None

    if request.method == "POST":
        # 【修正】未定義だった account を gmail_account に変更
        form = GmailAccountForm(request.POST, instance=gmail_account)
        if form.is_valid():
            new_account = form.save(commit=False)
            new_account.user = request.user
            raw_password = form.cleaned_data.get("app_password")
            if raw_password:
                key_base = settings.SECRET_KEY.ljust(32)[:32].encode()
                key = base64.urlsafe_b64encode(key_base)
                f = Fernet(key)
                new_account.app_password = f.encrypt(raw_password.encode()).decode()
            elif gmail_account:
                new_account.app_password = gmail_account.app_password
            new_account.save()
            messages.success(request, "Gmailの接続設定を保存しました。")
            return redirect("dashboard")
    else:
        form = GmailAccountForm(initial=initial_data)

    return render(request, "gmail_settings.html", {"form": form, "gmail_account": gmail_account})


@login_required
def dashboard(request):
    try:
        gmail_account = GmailAccount.objects.get(user=request.user)
    except GmailAccount.DoesNotExist:
        gmail_account = None

    # デフォルトで受信トレイ（inbox）を対象にする。必要に応じて 'sent' などに変更、または条件を外す
    box_type = request.GET.get("box", "inbox")
    emails = Email.objects.filter(user=request.user, gmail_box=box_type)

    context = {
        "gmail_account": gmail_account,
        "gmail_configured": gmail_account is not None,
        "total_emails": emails.count(),
        "summarized_emails": (
            emails.exclude(summary__isnull=True)
            .exclude(summary="")
            .exclude(summary__in=FAILED_SUMMARY_MESSAGES)
            .count()
        ),
        "replied_emails": (
            emails.exclude(reply_draft__isnull=True)
            .exclude(reply_draft="")
            .count()
        ),
        "recent_emails": emails.order_by("-received_at")[:5],
        "current_box": box_type,
    }
    return render(request, "dashboard.html", context)



@login_required
def email_detail(request, email_id):
    email_obj = get_object_or_404(Email, id=email_id, user=request.user)

    # 1. 自分が送信したメールも含め、同一スレッドのメールを取得（.distinct() でSQLレベルの重複を防ぐ）
    thread_emails = Email.objects.filter(
        user=request.user,
        thread_id=email_obj.thread_id
    ).order_by("received_at").distinct()

    colors = ['#4e73df', '#1cc88a', '#36b9cc', '#f6c23e', '#e74a3b', '#6f42c1']
    thread_messages = []
    seen_ids = set()  # 重複排除用のセット

    total_count = thread_emails.count()
    for idx, msg in enumerate(thread_emails):
        # データベース上のIDベースで完全に重複している場合はスキップ
        if msg.id in seen_ids:
            continue
        seen_ids.add(msg.id)

        sender_str = msg.sender or "(送信者なし)"
        initial = sender_str[0].upper() if sender_str else "?"
        avatar_color = colors[hash(sender_str) % len(colors)]

        # 閉じているときの表示用スニペット作成
        clean_body = BeautifulSoup(msg.body or "", "html.parser").get_text()
        snippet = clean_body[:80] + "..." if len(clean_body) > 80 else clean_body

        body_text = msg.body or ""

        # HTML形式とプレーンテキスト形式で処理を分岐してスタイル崩れを防止
        is_html = bool(re.search(r'<[a-z][\s\S]*>', body_text, re.IGNORECASE))

        if is_html:
            display_body = bleach.clean(
                body_text,
                tags=ALLOWED_TAGS,
                attributes=ALLOWED_ATTRIBUTES,
                strip=False
            )
        else:
            display_body = urlize(body_text, trim_url_limit=None, nofollow=True)
            display_body = display_body.replace('<a ', '<a target="_blank" rel="noopener noreferrer" ')
            display_body = display_body.replace('\r\n', '\n').replace('\n', '<br>')

        is_latest = (idx == total_count - 1)

        thread_messages.append({
            'id': msg.id,
            'subject': msg.subject,
            'sender': sender_str,
            'initial': initial,
            'avatar_color': avatar_color,
            'snippet': snippet,
            'received_at': msg.received_at,
            'display_body': display_body,
            'summary': msg.summary,
            'reply_draft': msg.reply_draft,
            'is_open': is_latest,
        })

    return render(request, "email_detail.html", {
        "email": email_obj,
        "thread_messages": thread_messages,
        "thread_count": len(thread_messages),  # 実際の重複排除後件数を使用
    })

@login_required
def email_summary(request, email_id):
    email = get_object_or_404(Email, id=email_id, user=request.user)
    regenerate = request.GET.get("regenerate")
    if AIService.needs_summary(email, regenerate=bool(regenerate)):
        if _generate_and_save_summary(email, regenerate=bool(regenerate)):
            messages.success(request, "要約とカテゴリ分類を生成しました")
        else:
            messages.error(request, "要約の生成に失敗しました。")
    return render(request, "email_summary.html", {"email": email})


@login_required
def email_reply(request, email_id):
    email = get_object_or_404(Email, id=email_id, user=request.user)
    tone = request.GET.get("tone", "polite")
    if not email.reply_draft or request.GET.get("regenerate"):
        ai_service = AIService()
        category = email.category
        if not category or category == "other":
            detected_category, _ = ai_service.analyze_email(
                subject=email.subject,
                email_body=email.body,
                sender=email.sender,
            )
            category = detected_category or email.category or "other"
            email.category = category
            email.save(update_fields=["category"])

        email.reply_draft = ai_service.generate_reply(
            email.body,
            email.sender,
            email.subject,
            tone=tone,
            category=category,
        )
        email.save(update_fields=["reply_draft"])
    return render(request, "email_reply.html", {"email": email, "tone": tone})

@login_required
def fetch_emails(request):
    box_type = request.GET.get("box", "inbox")
    tab_type = request.GET.get("tab", "primary")

    try:
        GmailAccount.objects.get(user=request.user)
    except GmailAccount.DoesNotExist:
        messages.warning(request, "Gmailアカウントが設定されていません。")
        return redirect("gmail_settings")

    user_box_emails = Email.objects.filter(user=request.user, gmail_box=box_type)
    existing_ids = list(user_box_emails.values_list("id", flat=True))
    latest_email = user_box_emails.order_by("-received_at").first()
    latest_timestamp = int(latest_email.received_at.timestamp()) if latest_email and latest_email.received_at else None

    request.session["initial_existing_ids"] = existing_ids
    request.session["last_known_received_ts"] = latest_timestamp
    request.session["processing_box"] = box_type
    request.session["processing_tab"] = tab_type
    request.session["is_fetching_background"] = True

    fetch_limit = getattr(settings, "GMAIL_FETCH_LIMIT", 500)

    async_task(
        "main.services.tasks.background_fetch_and_save_emails",
        request.user.id,
        box_type,
        tab_type,
        limit=fetch_limit,
        newer_than_timestamp=latest_timestamp,
    )

    messages.info(request, "メールのバックグラウンド同期とAI解析を開始しました。一覧画面で進捗を確認できます。")
    return redirect(f"/emails/?box={box_type}&tab={tab_type}")

@login_required
def email_list(request):
    from django.db.models import Max

    box_type = request.GET.get("box", "inbox")
    tab_type = request.GET.get("tab", "primary")

    emails = Email.objects.filter(user=request.user, gmail_box=box_type)

    if box_type == "inbox" and tab_type != "all":
        tab_lower = tab_type.lower()
        if tab_lower in ["primary", "メイン"]:
            emails = emails.filter(
                Q(tab_category="メイン") |
                Q(tab_category="primary") |
                Q(tab_category="") |
                Q(tab_category__isnull=True)
            )
        elif tab_lower in ["新着", "updates"]:
            emails = emails.filter(Q(tab_category="新着") | Q(tab_category__iexact="updates"))
        elif tab_lower in ["プロモーション", "promotions"]:
            emails = emails.filter(Q(tab_category="プロモーション") | Q(tab_category__iexact="promotions"))
        elif tab_lower in ["フォーラム", "forums"]:
            emails = emails.filter(Q(tab_category="フォーラム") | Q(tab_category__iexact="forums"))
        elif tab_lower in ["ソーシャル", "social"]:
            emails = emails.filter(Q(tab_category="ソーシャル") | Q(tab_category__iexact="social"))
        else:
            emails = emails.filter(tab_category=tab_type)

    # 【改善】フィルタ適用後のクエリセット内で thread_id ごとの最新IDを取得
    # thread_id が空の場合は id 単位で扱うため、Coalesce やフォールバックを考慮
    latest_email_ids = (
        emails.exclude(thread_id__isnull=True)
        .exclude(thread_id="")
        .values("thread_id")
        .annotate(latest_id=Max("id"))
        .values_list("latest_id", flat=True)
    )
    no_thread_ids = (
        emails.filter(Q(thread_id__isnull=True) | Q(thread_id=""))
        .values_list("id", flat=True)
    )
    combined_ids = list(latest_email_ids) + list(no_thread_ids)
    emails = emails.filter(id__in=combined_ids)

    # --- ステータスバー・プログレス表示用の計算ロジック ---
    is_fetching = request.session.get("is_fetching_background", False)
    target_ids = cache.get(f"processing_email_ids_{request.user.id}")

    # キャッシュに未反映でも、initial_existing_ids 以降に新規作成されたメールがあれば拾う
    initial_ids = set(request.session.get("initial_existing_ids") or [])
    if not target_ids and initial_ids:
        newly_created_ids = list(
            Email.objects.filter(user=request.user, gmail_box=box_type)
            .exclude(id__in=initial_ids)
            .values_list("id", flat=True)
        )
        if newly_created_ids:
            target_ids = newly_created_ids

    total_count = 0
    completed_count = 0

    if target_ids:
        processing_emails = Email.objects.filter(id__in=target_ids)
        total_count = len(target_ids)
        completed_count = (
            processing_emails.exclude(summary__isnull=True)
            .exclude(summary="")
            .exclude(summary__in=FAILED_SUMMARY_MESSAGES)
            .count()
        )
        if completed_count >= total_count and total_count > 0:
            cache.delete(f"processing_email_ids_{request.user.id}")
            request.session.pop("is_fetching_background", None)
            request.session.pop("initial_existing_ids", None)
            is_fetching = False

    emails = emails.select_related("gmail_account").order_by("-received_at", "-id")
    paginator = Paginator(emails, 50)
    page_obj = paginator.get_page(request.GET.get("page"))

    context = {
        "page_obj": page_obj,
        "current_box": box_type,
        "current_tab": tab_type,
        "is_fetching": is_fetching,
        "total_count": total_count,
        "completed_count": completed_count,
    }

    return render(request, "email_list.html", context)

@login_required
def email_processing_list(request):
    box_type = request.session.get("processing_box", "inbox")
    tab_type = request.session.get("processing_tab", "primary")

    target_ids = cache.get(f"processing_email_ids_{request.user.id}")

    if target_ids is None:
        context = {
            "emails": [],
            "is_processing": True,
            "total_count": 0,
            "completed_count": 0,
            "message": "Gmailからメールをバックグラウンドで取得しています..."
        }
        return render(request, "email_processing_list.html", context)

    if len(target_ids) == 0:
        cache.delete(f"processing_email_ids_{request.user.id}")
        messages.info(request, "新着メールはありませんでした。")
        return redirect(f"/emails/?box={box_type}&tab={tab_type}")

    emails = Email.objects.filter(id__in=target_ids).order_by("-received_at")
    total_count = len(target_ids)

    completed_count = (
        emails.exclude(summary__isnull=True)
        .exclude(summary="")
        .exclude(summary__in=FAILED_SUMMARY_MESSAGES)
        .count()
    )

    is_processing = completed_count < total_count

    if not is_processing:
        cache.delete(f"processing_email_ids_{request.user.id}")
        request.session.pop("processing_box", None)
        request.session.pop("processing_tab", None)
        request.session.pop("is_fetching_background", None)
        messages.success(request, "メールの同期とAI解析が完了しました。")
        return redirect(f"/emails/?box={box_type}&tab={tab_type}")

    context = {
        "emails": emails[:50],
        "is_processing": True,
        "total_count": total_count,
        "completed_count": completed_count,
        "message": "メールの保存が完了しました。順次AI要約を実行しています..."
    }
    return render(request, "email_processing_list.html", context)


@login_required
def classify_priority(request, email_id):
    email = get_object_or_404(Email, id=email_id, user=request.user)
    ai_service = AIService()
    prompt = f"以下のメールの重要度を「低」「中」「高」「緊急」のいずれか1語のみで答えてください。\n件名: {email.subject}\n重要度:"
    try:
        response = ai_service._call_gemini(prompt)
        if response:
            if "緊急" in response:
                priority = "urgent"
            elif "高" in response:
                priority = "high"
            elif "低" in response:
                priority = "low"
            else:
                priority = "medium"
            email.priority = priority
            email.save(update_fields=["priority"])
            messages.success(request, f"重要度を設定しました")
    except Exception as e:
        logger.error("重要度判定エラー")
    return redirect("email_detail", email_id=email_id)


@login_required
def keyword_alerts(request):
    alerts = KeywordAlert.objects.filter(user=request.user)
    if request.method == "POST":
        keyword = request.POST.get("keyword", "").strip()
        if keyword:
            KeywordAlert.objects.get_or_create(user=request.user, keyword=keyword)
            messages.success(request, f"キーワードを追加しました")
            return redirect("keyword_alerts")
    return render(request, "keyword_alerts.html", {"alerts": alerts})


@login_required
def delete_keyword_alert(request, alert_id):
    alert = get_object_or_404(KeywordAlert, id=alert_id, user=request.user)
    alert.delete()
    return redirect("keyword_alerts")


@login_required
def check_keyword_alerts(request):
    alerts = KeywordAlert.objects.filter(user=request.user)
    emails = Email.objects.filter(user=request.user)

    matched_count = 0
    for alert in alerts:
        matches = emails.filter(Q(subject__icontains=alert.keyword) | Q(body__icontains=alert.keyword))
        matched_count += matches.count()

    messages.success(request, f"キーワードチェックが完了しました（関連メール数: {matched_count}件）")
    return redirect("keyword_alerts")


# views.py に以下のビューを追加してください

@login_required
def delete_email(request, email_id):
    """
    メールを削除する処理：
    - すでにゴミ箱(trash)にある場合は、データベースから完全に削除する。
    - ゴミ箱以外にある場合は、gmail_boxを 'trash' に変更してゴミ箱に移動する。
    """
    email_obj = get_object_or_404(Email, id=email_id, user=request.user)

    # 現在の所属ボックスを保持（削除後のリダイレクト用）
    current_box = request.GET.get('box', email_obj.gmail_box)

    if email_obj.gmail_box == 'trash':
        # すでにゴミ箱にある場合は完全削除
        email_obj.delete()
        messages.success(request, "メールを完全に削除しました。")
    else:
        # ゴミ箱に移動
        email_obj.gmail_box = 'trash'
        email_obj.save(update_fields=['gmail_box'])
        messages.success(request, "メールをゴミ箱に移動しました。")

    # 削除前の画面（タブやページ情報を維持してリダイレクトできればベストですが、一覧へ戻します）
    return redirect(f"/emails/?box={current_box}")


# views.py の適な場所（delete_email の周辺など）に以下を追加してください

@login_required
def restore_email(request, email_id):
    """
    ゴミ箱にあるメールを受信トレイ（inbox）に戻す処理
    """
    email_obj = get_object_or_404(Email, id=email_id, user=request.user)

    # 現在の所属ボックスを保持（リダイレクト用）
    current_box = request.GET.get('box', email_obj.gmail_box)

    if email_obj.gmail_box == 'trash':
        # ゴミ箱から受信トレイ（inbox）に復元
        email_obj.gmail_box = 'inbox'
        email_obj.save(update_fields=['gmail_box'])
        messages.success(request, "メールを受信トレイに戻しました。")
    else:
        messages.warning(request, "このメールはゴミ箱にありません。")

    return redirect(f"/emails/?box={current_box}")