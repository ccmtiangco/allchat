from django.contrib.auth.views import LoginView, LogoutView
from django.urls import path

from .accounts import views as account_views
from . import views

app_name = 'chat'

urlpatterns = [
    path('', views.home, name='home'),
    path('conversations/<int:conversation_id>/', views.conversation_detail, name='conversation'),
    path('messages/send/', views.send_message, name='send_message'),
    path('accounts/signup/', account_views.signup, name='signup'),
    path(
        'accounts/login/',
        LoginView.as_view(
            template_name='registration/login.html',
            redirect_authenticated_user=True,
        ),
        name='login',
    ),
    path('accounts/logout/', LogoutView.as_view(), name='logout'),
]
