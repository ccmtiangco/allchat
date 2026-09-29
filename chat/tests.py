import os
import subprocess
import sys

from django.contrib.auth import get_user_model
from django.contrib import admin
from django.test import RequestFactory, TestCase
from django.urls import reverse

from .billing.models import INITIAL_BALANCE_MICRO_USD, UsageRequest, Wallet, WalletLedgerEntry
from .conversations.models import Conversation, Message
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


class AdminConfigurationTests(TestCase):
    def setUp(self):
        self.admin_user = User.objects.create_superuser(
            username='admin-config-user',
            email='admin-config@example.com',
            password='A-strong-password-8675309',
        )
        self.request = RequestFactory().get('/admin/')
        self.request.user = self.admin_user

    def test_required_models_are_registered_in_django_admin(self):
        for model in (User, Wallet, WalletLedgerEntry, Conversation, Message, UsageRequest):
            with self.subTest(model=model.__name__):
                self.assertIn(model, admin.site._registry)

    def test_financial_audit_records_are_read_only_in_admin(self):
        ledger_admin = admin.site._registry[WalletLedgerEntry]
        usage_admin = admin.site._registry[UsageRequest]
        wallet_admin = admin.site._registry[Wallet]

        self.assertFalse(ledger_admin.has_add_permission(self.request))
        self.assertFalse(ledger_admin.has_change_permission(self.request))
        self.assertFalse(ledger_admin.has_delete_permission(self.request))
        self.assertEqual(
            set(ledger_admin.get_readonly_fields(self.request)),
            {field.name for field in WalletLedgerEntry._meta.fields},
        )
        self.assertFalse(usage_admin.has_add_permission(self.request))
        self.assertFalse(usage_admin.has_delete_permission(self.request))
        self.assertEqual(
            set(usage_admin.get_readonly_fields(self.request)),
            {field.name for field in UsageRequest._meta.fields},
        )
        self.assertIn('balance_micro_usd', wallet_admin.get_readonly_fields(self.request))

    def test_wallet_adjustment_action_requires_amount_and_reason_fields(self):
        wallet_admin = admin.site._registry[Wallet]
        usage_admin = admin.site._registry[UsageRequest]

        self.assertIn('adjust_wallet_balances', wallet_admin.actions)
        self.assertIn('amount_micro_usd', wallet_admin.action_form.base_fields)
        self.assertIn('reason', wallet_admin.action_form.base_fields)
        self.assertIn('release_unknown_reservations', usage_admin.actions)
        self.assertIn('reason', usage_admin.action_form.base_fields)
