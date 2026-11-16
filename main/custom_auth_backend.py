from typing import Any, Optional
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.contrib.auth.base_user import AbstractBaseUser
from django.http import HttpRequest

UserModel = get_user_model()


class EmailBackend(ModelBackend):
    """
    ユーザー名、またはメールアドレスを使用してユーザー認証を行うためのカスタム認証バックエンド。
    ご提示いただいた堅牢な実装（タイミング攻撃対策など）をベースに、両方のログインに対応させます。
    """

    def authenticate(
        self,
        request: Optional[HttpRequest],
        username: Optional[str] = None,
        email: Optional[str] = None,
        password: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[AbstractBaseUser]:
        """
        ユーザー名またはメールアドレスと、パスワードを用いてユーザーを検証します。
        """
        # フォームから渡される 'username' または 'email' の値を取得
        lookup_value = email or username

        if not lookup_value or not password:
            return None

        try:
            # '@' が含まれている場合はメールアドレスとして、それ以外はユーザー名として検索
            if '@' in lookup_value:
                user = UserModel.objects.get(email__iexact=lookup_value)
            else:
                user = UserModel.objects.get(username__iexact=lookup_value)
        except UserModel.DoesNotExist:
            # 認証失敗時のタイミング攻撃（タイミングサイドチャネル）を防ぐため、
            # ユーザーが存在しない場合でもダミーのパスワード検証を行い、処理時間を一定に保つセキュリティ対策
            UserModel().set_password(password)
            return None
        except UserModel.MultipleObjectsReturned:
            # 同一メールアドレスのユーザーが複数存在する場合のフォールバック
            user = UserModel.objects.filter(email__iexact=lookup_value).order_by('id').first()

        if user and user.check_password(password) and self.user_can_authenticate(user):
            return user

        return None