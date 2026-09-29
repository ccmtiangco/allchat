from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render

from .forms import SignUpForm


def signup(request):
    if request.user.is_authenticated:
        return redirect('chat:home')

    form = SignUpForm(request.POST if request.method == 'POST' else None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect('chat:home')

    return render(request, 'chat/signup.html', {'form': form})


@login_required
def home(request):
    return render(request, 'chat/home.html', {'wallet': request.user.wallet})
