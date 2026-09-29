from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone

from ..choices import ProviderInterface

DEFAULT_CONVERSATION_TITLE = 'New conversation'
MAX_GENERATED_TITLE_LENGTH = 80


class Conversation(models.Model):
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='conversations',
    )
    title = models.CharField(max_length=160, default=DEFAULT_CONVERSATION_TITLE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ('-updated_at', '-id')
        indexes = [models.Index(fields=('owner', '-updated_at'), name='conversation_owner_updated')]

    @classmethod
    def create_from_first_message(cls, owner, content):
        if not content.strip():
            raise ValidationError({'content': 'A conversation must start with a non-empty message.'})

        first_line = content.strip().splitlines()[0]
        title = first_line[:MAX_GENERATED_TITLE_LENGTH] or DEFAULT_CONVERSATION_TITLE
        with transaction.atomic():
            conversation = cls.objects.create(owner=owner, title=title)
            message = Message.objects.create(
                conversation=conversation,
                role=Message.Role.USER,
                content=content,
            )
        return conversation, message

    def __str__(self):
        return self.title


class Message(models.Model):
    class Role(models.TextChoices):
        USER = 'user', 'User'
        ASSISTANT = 'assistant', 'Assistant'

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    content = models.TextField()
    provider = models.CharField(
        max_length=16,
        choices=ProviderInterface.choices,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ('created_at', 'id')
        indexes = [models.Index(fields=('conversation', 'created_at', 'id'), name='message_conversation_order')]
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(role='user', provider__isnull=True)
                    | Q(
                        role='assistant',
                        provider__isnull=False,
                        provider__in=ProviderInterface.values,
                    )
                ),
                name='message_provider_matches_role',
            ),
            models.CheckConstraint(
                condition=~Q(content=''),
                name='message_content_nonempty',
            ),
        ]

    def save(self, *args, **kwargs):
        using = kwargs.get('using')
        with transaction.atomic(using=using):
            super().save(*args, **kwargs)
            Conversation.objects.using(using).filter(pk=self.conversation_id).update(
                updated_at=timezone.now()
            )

    def __str__(self):
        return f'{self.get_role_display()} message in {self.conversation_id}'
