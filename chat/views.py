from uuid import uuid4

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .billing.models import UsageRequest
from .billing.services import IdempotencyConflict, InsufficientBalance
from .conversations.forms import MessageSubmissionForm
from .conversations.models import Conversation
from .conversations.orchestration import submit_message
from .conversations.services import get_owned_conversation
from .proxy.router import ProxyConfigurationError, ProxyError


@login_required
def home(request):
    return render(
        request,
        'chat/home.html',
        _chat_context(request.user),
    )


@login_required
def conversation_detail(request, conversation_id):
    try:
        conversation = get_owned_conversation(request.user, conversation_id)
    except Conversation.DoesNotExist as exc:
        raise Http404 from exc

    return render(
        request,
        'chat/conversation.html',
        _chat_context(request.user, conversation),
    )


@login_required
@require_POST
def send_message(request):
    form = MessageSubmissionForm(request.POST)
    if not form.is_valid():
        messages.error(request, 'Please correct the message form and try again.')
        return _redirect_to_form_conversation(request, form)

    data = form.cleaned_data
    try:
        result = submit_message(
            user=request.user,
            content=data['content'],
            provider=data['provider'],
            idempotency_key=data['idempotency_key'],
            conversation_id=data['conversation_id'],
        )
    except Conversation.DoesNotExist as exc:
        raise Http404 from exc
    except InsufficientBalance:
        messages.error(request, 'Your wallet balance is too low for this request.')
        return _redirect_to_form_conversation(request, form)
    except ProxyConfigurationError:
        messages.error(request, 'The selected provider is not configured. No provider request was sent.')
        return _redirect_to_form_conversation(request, form)
    except ProxyError:
        messages.warning(
            request,
            'The response or its usage could not be confirmed. Funds remain reserved for reconciliation; the request was not retried.',
        )
        return _redirect_to_form_conversation(request, form)
    except IdempotencyConflict:
        messages.error(request, 'This submission key was already used for a different message.')
        return _redirect_to_form_conversation(request, form)
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
        return _redirect_to_form_conversation(request, form)

    if result.duplicate:
        messages.info(request, 'This submission was already processed; no second provider call was made.')
    elif result.usage_request.status == UsageRequest.Status.RECONCILIATION_REQUIRED:
        messages.warning(request, 'Usage exceeded its reservation and is awaiting reconciliation.')
    elif result.completion_status == 'incomplete':
        messages.warning(request, 'The provider reached its output limit; the partial answer was charged by reported usage.')
    elif result.completion_status == 'filtered':
        messages.warning(request, 'The provider marked this response as filtered.')

    return redirect('chat:conversation', conversation_id=result.conversation.pk)


def _chat_context(user, conversation=None):
    form_initial = {'idempotency_key': uuid4().hex}
    if conversation:
        form_initial['conversation_id'] = conversation.pk
    return {
        'wallet': user.wallet,
        'conversations': Conversation.objects.filter(owner=user),
        'conversation': conversation,
        'chat_messages': conversation.messages.all() if conversation else (),
        'usage_requests': conversation.usage_requests.all() if conversation else (),
        'form': MessageSubmissionForm(initial=form_initial),
        'max_output_tokens': settings.MAX_CHAT_OUTPUT_TOKENS,
    }


def _redirect_to_form_conversation(request, form):
    conversation_id = form.data.get('conversation_id')
    if conversation_id:
        try:
            conversation = get_owned_conversation(request.user, int(conversation_id))
        except (Conversation.DoesNotExist, ValueError):
            raise Http404
        return redirect('chat:conversation', conversation_id=conversation.pk)
    return redirect('chat:home')
