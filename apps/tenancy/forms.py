"""Forms para admin — edição segura de credenciais criptografadas."""

from __future__ import annotations

import json

from django import forms

from .models import OrganizationDataSource


class DataSourceCredentialsForm(forms.ModelForm):
    """Form que expõe credenciais IXC como campos individuais.

    Ao salvar, serializa os campos em JSON e grava no EncryptedTextField.
    Ao carregar, parseia o JSON e preenche os campos.
    """

    base_url = forms.URLField(
        label="URL base do ERP",
        required=False,
        widget=forms.URLInput(attrs={"placeholder": "https://erp.cliente.com.br", "size": 60}),
    )
    user_id = forms.CharField(
        label="User ID",
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "1", "size": 10}),
    )
    api_token = forms.CharField(
        label="API Token",
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "hash do token (sem user_id:)", "size": 80}),
        help_text="Apenas o hash — sem o prefixo 'ID:'. Ex: 187d91cab0...",
    )

    class Meta:
        model = OrganizationDataSource
        fields = (
            "organization", "source_type", "capability",
            "priority", "is_active",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance and self.instance.pk:
            try:
                creds = self.instance.get_credentials()
                self.fields["base_url"].initial = creds.get("base_url", "")
                self.fields["user_id"].initial = creds.get("user_id", "")
                self.fields["api_token"].initial = creds.get("api_token", "")
            except Exception:
                pass  # credenciais corrompidas — deixa campos vazios

    def save(self, commit=True):
        instance = super().save(commit=False)
        creds = {}
        base_url = self.cleaned_data.get("base_url")
        user_id = self.cleaned_data.get("user_id")
        api_token = self.cleaned_data.get("api_token")
        if base_url:
            creds["base_url"] = base_url
        if user_id:
            creds["user_id"] = user_id
        if api_token:
            creds["api_token"] = api_token
        instance.credentials_encrypted = json.dumps(creds, ensure_ascii=False)
        if commit:
            instance.save()
        return instance
