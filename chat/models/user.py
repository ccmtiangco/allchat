from django.contrib.auth.models import AbstractUser
from django.db import transaction


class User(AbstractUser):
    def save(self, *args, **kwargs):
        is_new = self._state.adding
        using = kwargs.get('using')

        with transaction.atomic(using=using):
            super().save(*args, **kwargs)
            if is_new:
                from ..billing.services import provision_initial_wallet

                provision_initial_wallet(self, using=using)
