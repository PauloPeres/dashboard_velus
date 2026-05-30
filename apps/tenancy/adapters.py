"""Adapters allauth — controla signup e vinculação de contas sociais."""

from allauth.account.adapter import DefaultAccountAdapter
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


class NoSignupAccountAdapter(DefaultAccountAdapter):
    """Bloqueia signup público — só admin cria contas."""

    def is_open_for_signup(self, request):
        return False


class AutoConnectSocialAdapter(DefaultSocialAccountAdapter):
    """Vincula conta Google automaticamente se o email já existir.

    Sem isso, allauth redireciona para /accounts/3rdparty/signup/ que
    falha porque signup está desabilitado.
    """

    def is_open_for_signup(self, request, sociallogin):
        # Permite "signup" social APENAS se o email já pertence a um user
        # existente — na prática, é um login, não um cadastro novo.
        email = sociallogin.email_addresses[0].email if sociallogin.email_addresses else None
        if email:
            from apps.tenancy.models import User

            return User.objects.filter(email__iexact=email).exists()
        return False
