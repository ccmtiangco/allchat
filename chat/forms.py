from django.contrib.auth.forms import UserCreationForm
from django import forms

from .models import User


class SignUpForm(UserCreationForm):
    email = forms.EmailField(required=True)

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'email')
