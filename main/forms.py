from django import forms
from .models import GmailAccount
# forms.py に追加
from django.contrib.auth.forms import AuthenticationForm
from django import forms

class EmailOrUsernameAuthenticationForm(AuthenticationForm):
    """
    ログイン画面のラベルやプレースホルダーを「ユーザー名またはメールアドレス」に変更するフォーム
    """
    username = forms.CharField(
        label="ユーザー名 または メールアドレス",
        widget=forms.TextInput(attrs={'autofocus': True, 'placeholder': 'ユーザー名 または メールアドレス'})
    )


class GmailAccountForm(forms.ModelForm):
    class Meta:
        model = GmailAccount
        fields = ['gmail_address', 'app_password', 'imap_enabled']
        widgets = {
            'app_password': forms.PasswordInput(attrs={'placeholder': '16桁のApp Password'}),
            'gmail_address': forms.EmailInput(attrs={'placeholder': 'your@gmail.com'}),
        }
        labels = {
            'gmail_address': 'Gmailアドレス',
            'app_password': 'App Password',
            'imap_enabled': 'IMAPを有効にする',
        }
        help_texts = {
            'app_password': 'Googleアカウント設定で生成した16桁のApp Passwordを入力してください',
        }
    
    def save(self, commit=True):
        instance = super().save(commit=False)
        # パスワードを暗号化して保存
        if self.cleaned_data.get('app_password'):
            instance.set_app_password(self.cleaned_data['app_password'])
        if commit:
            instance.save()
        return instance
