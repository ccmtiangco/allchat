import os
import subprocess
import sys

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from .models import INITIAL_BALANCE_MICRO_USD, WalletLedgerEntry

User = get_user_model()


class AuthenticationViewsTests(TestCase):
    def test_signup_reports_required_fields_for_empty_submission(self):
        response = self.client.post(reverse('chat:signup'), {})

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['form'].errors)
        self.assertEqual(User.objects.count(), 0)

    def test_signup_creates_user_wallet_and_initial_credit(self):
        response = self.client.post(
            reverse('chat:signup'),
            {
                'username': 'new-user',
                'password1': 'A-strong-password-8675309',
                'password2': 'A-strong-password-8675309',
            },
        )

        self.assertRedirects(response, reverse('chat:home'))
        user = User.objects.get(username='new-user')
        self.assertTrue(user.check_password('A-strong-password-8675309'))
        self.assertEqual(user.wallet.balance_micro_usd, INITIAL_BALANCE_MICRO_USD)
        self.assertEqual(user.wallet.formatted_balance, '$5.000000')
        self.assertEqual(user.wallet.ledger_entries.count(), 1)
        self.assertEqual(
            user.wallet.ledger_entries.get().entry_type,
            WalletLedgerEntry.EntryType.INITIAL_CREDIT,
        )

    def test_saving_user_again_does_not_grant_another_initial_credit(self):
        user = User.objects.create_user(username='existing-user', password='password')
        user.first_name = 'Updated'
        user.save()

        self.assertEqual(user.wallet.balance_micro_usd, INITIAL_BALANCE_MICRO_USD)
        self.assertEqual(user.wallet.ledger_entries.count(), 1)

    def test_login_and_logout(self):
        User.objects.create_user(username='login-user', password='A-strong-password-8675309')

        login_response = self.client.post(
            reverse('chat:login'),
            {'username': 'login-user', 'password': 'A-strong-password-8675309'},
        )
        self.assertRedirects(login_response, reverse('chat:home'))
        self.assertContains(self.client.get(reverse('chat:home')), '$5.000000')

        logout_response = self.client.post(reverse('chat:logout'))
        self.assertRedirects(logout_response, reverse('chat:login'))

    def test_home_requires_authentication(self):
        response = self.client.get(reverse('chat:home'))
        self.assertRedirects(response, f'{reverse("chat:login")}?next={reverse("chat:home")}')

    def test_signup_and_login_templates_render(self):
        signup_response = self.client.get(reverse('chat:signup'))
        login_response = self.client.get(reverse('chat:login'))

        self.assertTemplateUsed(signup_response, 'chat/signup.html')
        self.assertTemplateUsed(login_response, 'registration/login.html')


class UserCreationTests(TestCase):
    def test_superuser_creation_also_creates_the_initial_wallet(self):
        user = User.objects.create_superuser(
            username='admin-user',
            email='admin@example.com',
            password='A-strong-password-8675309',
        )

        self.assertEqual(user.wallet.balance_micro_usd, INITIAL_BALANCE_MICRO_USD)
        self.assertEqual(user.wallet.ledger_entries.count(), 1)


class ConfigurationTests(TestCase):
    def test_production_configuration_reports_missing_key_names_only(self):
        env = os.environ.copy()
        env.update(
            {
                'DJANGO_DEBUG': 'False',
                'DJANGO_SECRET_KEY': 'test-secret-that-must-not-be-printed',
                'OPENAI_PROXY_KEY': '',
                'ANTHROPIC_PROXY_KEY': '',
                'GOOGLE_PROXY_KEY': '',
            }
        )
        result = subprocess.run(
            [sys.executable, '-c', 'import allchat_project.settings'],
            capture_output=True,
            check=False,
            env=env,
            text=True,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn('OPENAI_PROXY_KEY', result.stderr)
        self.assertIn('ANTHROPIC_PROXY_KEY', result.stderr)
        self.assertIn('GOOGLE_PROXY_KEY', result.stderr)
        self.assertNotIn('test-secret-that-must-not-be-printed', result.stderr)
