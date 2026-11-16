from django import forms
from .models import GmailAccount
from django.contrib.auth.forms import AuthenticationForm
from django import forms
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth import get_user_model

UserModel = get_user_model()

class EmailAdminAuthenticationForm(AdminAuthenticationForm):
    """
    管理者サイトで「ユーザー名」の代わりに「メールアドレス」でもログインできるようにするフォーム
    """
    def clean(self):
        username = self.cleaned_data.get('username')
        password = self.cleaned_data.get('password')

        if username and password:
            # 入力値に '@' が含まれている場合はメールアドレスとしてユーザーを検索
            if '@' in username:
                try:
                    user_obj = UserModel.objects.get(email__iexact=username)
                    # 見つかった場合、フォームが期待する username フィールドに実際のユーザー名を代入する
                    self.cleaned_data['username'] = user_obj.username
                except UserModel.DoesNotExist:
                    pass  # 存在しない場合はそのまま親クラスのバリデーションに委ねる（エラーになる）
                except UserModel.MultipleObjectsReturned:
                    user_obj = UserModel.objects.filter(email__iexact=username).order_by('id').first()
                    if user_obj:
                        self.cleaned_data['username'] = user_obj.username

        return super().clean()

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
