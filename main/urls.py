from django.urls import path
from . import views

urlpatterns = [
    path('login/', views.custom_login, name='login'),
    path('', views.dashboard, name='dashboard'),
    path('logout/', views.custom_logout, name='logout'),
    path('auth/start/', views.gmail_auth_start, name='gmail_auth_start'),
    path('auth/callback/', views.oauth2_callback, name='oauth2_callback'),
    path('gmail-settings/', views.gmail_settings, name='gmail_settings'),
    path('fetch-emails/', views.fetch_emails, name='fetch_emails'),
    path('emails/', views.email_list, name='email_list'),
    path('emails/processing/', views.email_processing_list, name='email_processing_list'),
    path('emails/<int:email_id>/', views.email_detail, name='email_detail'),
    path('emails/<int:email_id>/summary/', views.email_summary, name='email_summary'),
    path('emails/<int:email_id>/reply/', views.email_reply, name='email_reply'),
    path('emails/<int:email_id>/classify-priority/', views.classify_priority, name='classify_priority'),
    path('keyword-alerts/', views.keyword_alerts, name='keyword_alerts'),
    path('keyword-alerts/<int:alert_id>/delete/', views.delete_keyword_alert, name='delete_keyword_alert'),
    path('keyword-alerts/check/', views.check_keyword_alerts, name='check_keyword_alerts'),
    path('emails/<int:email_id>/delete/', views.delete_email, name='delete_email'),
    path('emails/<int:email_id>/restore/', views.restore_email, name='restore_email'),
]