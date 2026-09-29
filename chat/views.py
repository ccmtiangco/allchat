import json
from uuid import uuid4

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .billing.models import UsageRequest
from .billing.services import IdempotencyConflict, InsufficientBalance
from .conversations.forms import MessageSubmissionForm
from .conversations.models import Conversation
from .conversations.orchestration import stream_message_turn, submit_message
from .conversations.services import get_owned_conversation
from .proxy.router import ProxyConfigurationError, ProxyError
from .templatetags.chat_markdown import safe_markdown


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
        messages.error(request, 'This route is not available right now. No charge was made.')
        return _redirect_to_form_conversation(request, form)
    except ProxyError:
        messages.warning(
            request,
            'Connection timed out. A temporary hold is under review to prevent an overcharge.',
        )
        return _redirect_to_form_conversation(request, form)
    except IdempotencyConflict:
        messages.info(request, 'Message already sent. We prevented an accidental duplicate charge.')
        return _redirect_to_form_conversation(request, form)
    except ValidationError as exc:
        messages.error(request, '; '.join(exc.messages))
        return _redirect_to_form_conversation(request, form)

    if result.duplicate:
        messages.info(request, 'Message already sent. We prevented an accidental duplicate charge.')
    elif result.usage_request.status == UsageRequest.Status.RECONCILIATION_REQUIRED:
        messages.warning(request, 'The response is under review to prevent an overcharge.')
    elif result.completion_status == 'incomplete':
        messages.warning(request, 'The provider reached its output limit; the partial answer was charged by reported usage.')
    elif result.completion_status == 'filtered':
        messages.warning(request, 'The provider marked this response as filtered.')

    return redirect('chat:conversation', conversation_id=result.conversation.pk)


@login_required
@require_POST
def stream_message(request):
    form = MessageSubmissionForm(request.POST)
    if not form.is_valid():
        return JsonResponse({'error': 'Please correct the message and try again.'}, status=400)

    response = StreamingHttpResponse(
        _stream_chat_events(request, form.cleaned_data),
        content_type='text/event-stream',
    )
    response['Cache-Control'] = 'no-cache, no-transform'
    response['X-Accel-Buffering'] = 'no'
    return response


def _stream_chat_events(request, data):
    try:
        for event in stream_message_turn(
            user=request.user,
            content=data['content'],
            provider=data['provider'],
            idempotency_key=data['idempotency_key'],
            conversation_id=data['conversation_id'],
        ):
            payload = dict(event.payload)
            if payload.get('conversation_id'):
                payload['conversation_url'] = reverse(
                    'chat:conversation',
                    kwargs={'conversation_id': payload['conversation_id']},
                )
            if 'assistant_text' in payload:
                payload['assistant_html'] = str(safe_markdown(payload['assistant_text']))
            if event.kind in {
                'reserved',
                'failed_before_upstream',
                'completed',
                'reconciliation_required',
                'already_processed',
            }:
                wallet = request.user.wallet
                wallet.refresh_from_db()
                payload['wallet_balance'] = wallet.formatted_balance
            yield _format_sse_event(event.kind, payload)
    except IdempotencyConflict:
        yield _format_sse_event(
            'error',
            {'message': 'Message already sent. We prevented an accidental duplicate charge.'},
        )
    except ValidationError:
        yield _format_sse_event('error', {'message': 'Please correct the message and try again.'})
    except Conversation.DoesNotExist:
        yield _format_sse_event('error', {'message': 'That conversation is not available.'})


def _format_sse_event(event_name, payload):
    return f"event: {event_name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


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
