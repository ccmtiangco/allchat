from django import forms

from ..choices import ProviderInterface
from .services import MAX_CONTEXT_CHARACTERS


class MessageSubmissionForm(forms.Form):
    content = forms.CharField(
        label='Message',
        max_length=MAX_CONTEXT_CHARACTERS,
        widget=forms.Textarea(
            attrs={
                'rows': 4,
                'placeholder': 'Write a message...',
                'autocomplete': 'off',
            }
        ),
    )
    provider = forms.ChoiceField(
        label='Route through',
        choices=ProviderInterface.choices,
        initial=ProviderInterface.OPENAI,
    )
    conversation_id = forms.IntegerField(required=False, widget=forms.HiddenInput)
    idempotency_key = forms.CharField(max_length=128, widget=forms.HiddenInput)
