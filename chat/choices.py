from django.db import models


class ProviderInterface(models.TextChoices):
    OPENAI = 'openai', 'OpenAI-compatible'
    ANTHROPIC = 'anthropic', 'Anthropic-compatible'
    GOOGLE = 'google', 'Google-compatible'
