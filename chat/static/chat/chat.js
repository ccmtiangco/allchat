(() => {
    const providerLabels = {
        openai: 'OpenAI-compatible',
        anthropic: 'Anthropic-compatible',
        google: 'Google-compatible',
    };

    function createIdempotencyKey() {
        if (window.crypto && typeof window.crypto.randomUUID === 'function') {
            return window.crypto.randomUUID().replaceAll('-', '');
        }
        return `${Date.now().toString(16)}${Math.random().toString(16).slice(2)}`;
    }

    function formatDuration(milliseconds) {
        const elapsed = Math.max(0, Math.floor(milliseconds));
        const minutes = String(Math.floor(elapsed / 60_000)).padStart(2, '0');
        const seconds = String(Math.floor((elapsed % 60_000) / 1_000)).padStart(2, '0');
        const tenths = Math.floor((elapsed % 1_000) / 100);
        return `${minutes}:${seconds}.${tenths}`;
    }

    async function readEventStream(response, onEvent) {
        if (!response.ok) {
            const payload = await response.json().catch(() => ({}));
            throw new Error(payload.error || 'The message could not be sent. Please try again.');
        }
        if (!response.headers.get('content-type')?.startsWith('text/event-stream') || !response.body) {
            throw new Error('Your session may have expired. Refresh the page and sign in again.');
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';

        async function dispatch(frame) {
            let eventName = 'message';
            const data = [];
            for (const line of frame.split('\n')) {
                if (line.startsWith('event:')) eventName = line.slice(6).trim();
                if (line.startsWith('data:')) data.push(line.slice(5).trimStart());
            }
            if (data.length) await onEvent(eventName, JSON.parse(data.join('\n')));
        }

        while (true) {
            const { value, done } = await reader.read();
            buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
            buffer = buffer.replace(/\r\n/g, '\n');
            let boundary = buffer.indexOf('\n\n');
            while (boundary !== -1) {
                await dispatch(buffer.slice(0, boundary));
                buffer = buffer.slice(boundary + 2);
                boundary = buffer.indexOf('\n\n');
            }
            if (done) {
                if (buffer.trim()) await dispatch(buffer);
                return;
            }
        }
    }

    function bindForm(form) {
        const panel = form.closest('.chat-panel');
        const list = panel.querySelector('[data-message-list]');
        const status = form.querySelector('[data-stream-status]');
        const timer = form.querySelector('[data-stream-timer]');
        const indicator = form.querySelector('[data-stream-indicator]');
        const sendButton = form.querySelector('.send-button');
        const textarea = form.elements.content;
        let active = false;
        let blockedText = null;
        let currentAssistant = null;
        let currentRequestId = null;
        let accepted = false;
        let stopwatch = null;

        function startStopwatch() {
            stopwatch = startTimer(timer);
        }

        function stopTimer(serverDuration = null) {
            if (!stopwatch) return;
            stopwatch.stop(serverDuration);
            stopwatch = null;
        }

        function setStatus(text, busy = false) {
            status.textContent = text;
            indicator.hidden = !busy;
        }

        function refreshIdempotencyKey() {
            form.elements.idempotency_key.value = createIdempotencyKey();
            blockedText = null;
            sendButton.disabled = active;
        }

        function clearEmptyState() {
            list.querySelector('.empty-chat-state, .empty-transcript')?.remove();
        }

        function appendUserMessage(messageId, content) {
            if (messageId && list.querySelector(`[data-message-id="${messageId}"]`)) return;
            clearEmptyState();
            const row = document.createElement('article');
            row.className = 'message-row message-row--user';
            if (messageId) row.dataset.messageId = messageId;
            const group = document.createElement('div');
            group.className = 'user-message-group';
            const bubble = document.createElement('div');
            bubble.className = 'user-bubble';
            const text = document.createElement('div');
            text.className = 'message-content';
            text.textContent = content;
            bubble.append(text);
            group.append(bubble);
            const timestamp = document.createElement('time');
            timestamp.textContent = new Intl.DateTimeFormat(undefined, {
                month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
            }).format(new Date());
            group.append(timestamp);
            row.append(group);
            list.append(row);
            scrollTranscript();
        }

        function appendAssistantMessage(requestId, provider) {
            if (currentAssistant?.dataset.streamRequestId === String(requestId)) return currentAssistant;
            clearEmptyState();
            const row = document.createElement('article');
            row.className = 'message-row message-row--assistant';
            row.dataset.streamRequestId = requestId;
            const response = document.createElement('div');
            response.className = 'assistant-response';
            const header = document.createElement('header');
            header.className = 'assistant-message-header';
            const avatar = document.createElement('span');
            avatar.className = 'assistant-avatar';
            avatar.setAttribute('aria-hidden', 'true');
            avatar.textContent = 'A';
            const name = document.createElement('strong');
            name.textContent = 'AllChat';
            const providerBadge = document.createElement('span');
            providerBadge.className = 'provider-badge';
            providerBadge.textContent = providerLabels[provider] || 'Assistant';
            const timestamp = document.createElement('time');
            timestamp.textContent = new Intl.DateTimeFormat(undefined, {
                month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
            }).format(new Date());
            header.append(avatar, name, providerBadge, timestamp);
            const content = document.createElement('div');
            content.className = 'message-content';
            content.dataset.streamAssistantContent = '';
            response.append(header, content);
            row.append(response);
            list.append(row);
            currentAssistant = row;
            scrollTranscript();
            return row;
        }

        function setAssistantContent(row, text, safeHtml = null) {
            const content = row.querySelector('[data-stream-assistant-content]');
            if (safeHtml !== null) {
                // This HTML is generated by the server-side Markdown allow-list sanitizer.
                content.innerHTML = safeHtml;
            } else {
                content.textContent = text;
            }
        }

        function addNotice(message, warning = false) {
            const notice = document.createElement('aside');
            notice.className = warning ? 'request-notice request-notice--warning' : 'request-notice';
            notice.textContent = message;
            list.prepend(notice);
        }

        function addUsageReceipt(row, payload) {
            const response = row.querySelector('.assistant-response');
            if (response.querySelector('.usage-receipt')) return;
            row.dataset.messageId = payload.assistant_message_id;
            row.dataset.streamRequestId = payload.usage_request_id;
            const latency = document.createElement('span');
            latency.className = 'response-latency';
            latency.textContent = `Response ${payload.latency_ms} ms`;
            response.querySelector('.assistant-message-header').append(latency);

            const receipt = document.createElement('footer');
            receipt.className = 'usage-receipt';
            for (const label of [
                `${payload.input_tokens} input`,
                `${payload.output_tokens} output`,
                `${payload.total_tokens} total tokens`,
            ]) {
                const item = document.createElement('span');
                item.textContent = label;
                receipt.append(item);
            }
            const charge = document.createElement('strong');
            charge.textContent = `-${payload.formatted_charge}`;
            receipt.append(charge);
            response.append(receipt);
        }

        function updateSession(payload) {
            if (!payload.conversation_id) return;
            form.elements.conversation_id.value = payload.conversation_id;
            if (payload.conversation_url && window.location.pathname !== payload.conversation_url) {
                window.history.pushState({}, '', payload.conversation_url);
            }
            const title = payload.conversation_title || 'Conversation';
            const titleNode = panel.querySelector('.chat-topbar-title h1');
            if (titleNode) titleNode.textContent = title;
            document.title = `${title} | AllChat`;

            const section = document.querySelector('.sessions-section');
            if (!section) return;
            let navigation = section.querySelector('.conversation-list');
            if (!navigation) {
                navigation = document.createElement('nav');
                navigation.className = 'conversation-list';
                navigation.setAttribute('aria-label', 'Conversation history');
                section.querySelector('.sidebar-empty')?.remove();
                section.append(navigation);
            }
            const selector = `[data-conversation-id="${payload.conversation_id}"]`;
            let link = navigation.querySelector(selector);
            if (!link) {
                link = document.createElement('a');
                link.className = 'conversation-link';
                link.href = payload.conversation_url;
                link.dataset.conversationId = payload.conversation_id;
                const linkTitle = document.createElement('span');
                linkTitle.className = 'conversation-link-title';
                const date = document.createElement('time');
                link.append(linkTitle, date);
                navigation.prepend(link);
            }
            link.classList.add('is-current');
            link.setAttribute('aria-current', 'page');
            link.querySelector('.conversation-link-title').textContent = title;
            link.querySelector('time').textContent = new Intl.DateTimeFormat(undefined, {
                month: 'short', day: 'numeric', year: 'numeric',
            }).format(new Date());
            navigation.querySelectorAll('.conversation-link').forEach(item => {
                if (item !== link) {
                    item.classList.remove('is-current');
                    item.removeAttribute('aria-current');
                }
            });
        }

        function updateWallet(balance) {
            if (!balance) return;
            const wallet = document.querySelector('.wallet-chip strong');
            if (wallet) wallet.textContent = balance;
        }

        function scrollTranscript() {
            const transcript = panel.querySelector('.transcript-scroll');
            if (transcript) transcript.scrollTop = transcript.scrollHeight;
        }

        function setIdleAfterFailure(message, keepKey) {
            active = false;
            stopTimer();
            setStatus(message);
            sendButton.disabled = false;
            if (!keepKey) refreshIdempotencyKey();
        }

        textarea.addEventListener('input', () => {
            if (blockedText !== null && textarea.value !== blockedText) refreshIdempotencyKey();
        });

        form.addEventListener('submit', async event => {
            if (active || !window.fetch || !window.ReadableStream) return;
            event.preventDefault();
            active = true;
            sendButton.disabled = true;
            const submittedText = textarea.value;
            blockedText = null;
            currentAssistant = null;
            accepted = false;
            setStatus('Sending message...', true);
            startStopwatch();

            try {
                const response = await window.fetch(form.dataset.streamUrl, {
                    method: 'POST',
                    credentials: 'same-origin',
                    headers: { Accept: 'text/event-stream' },
                    body: new FormData(form),
                });
                await readEventStream(response, (eventName, payload) => {
                    if (eventName === 'started') {
                        accepted = true;
                        currentRequestId = payload.usage_request_id;
                        updateSession(payload);
                        if (!payload.duplicate) appendUserMessage(payload.user_message_id, payload.user_text);
                        setStatus(payload.duplicate ? 'Checking the previous message...' : 'Message accepted.', true);
                        return;
                    }
                    if (eventName === 'reserved') {
                        updateWallet(payload.wallet_balance);
                        setStatus('Processing message (funds temporarily held)...', true);
                        return;
                    }
                    if (eventName === 'delta') {
                        setStatus('Generating response...', true);
                        const row = appendAssistantMessage(currentRequestId, form.elements.provider.value);
                        const content = row.querySelector('[data-stream-assistant-content]');
                        content.append(document.createTextNode(payload.text));
                        scrollTranscript();
                        return;
                    }
                    if (eventName === 'completed') {
                        const row = appendAssistantMessage(payload.usage_request_id, payload.provider);
                        setAssistantContent(row, payload.assistant_text, payload.assistant_html);
                        addUsageReceipt(row, payload);
                        updateWallet(payload.wallet_balance);
                        stopTimer(payload.latency_ms);
                        active = false;
                        sendButton.disabled = false;
                        textarea.value = '';
                        blockedText = null;
                        refreshIdempotencyKey();
                        setStatus(`Response complete in ${formatDuration(payload.latency_ms)}.`, false);
                        scrollTranscript();
                        return;
                    }
                    if (eventName === 'reconciliation_required') {
                        const row = currentAssistant || (payload.assistant_message_id || payload.assistant_text
                            ? appendAssistantMessage(payload.usage_request_id, form.elements.provider.value)
                            : null);
                        if (row) {
                            row.dataset.messageId = payload.assistant_message_id || '';
                            if (payload.assistant_html) setAssistantContent(row, payload.assistant_text, payload.assistant_html);
                            if (payload.partial_response && !row.querySelector('.partial-response-label')) {
                                const partialLabel = document.createElement('p');
                                partialLabel.className = 'partial-response-label';
                                partialLabel.textContent = 'Partial response';
                                row.querySelector('.assistant-response').prepend(partialLabel);
                            }
                        }
                        updateWallet(payload.wallet_balance);
                        addNotice(payload.message, true);
                        stopTimer(payload.latency_ms);
                        active = false;
                        blockedText = submittedText;
                        sendButton.disabled = true;
                        setStatus(payload.message, false);
                        scrollTranscript();
                        return;
                    }
                    if (eventName === 'failed_before_upstream') {
                        updateWallet(payload.wallet_balance);
                        addNotice(payload.message);
                        setIdleAfterFailure(payload.message, false);
                        return;
                    }
                    if (eventName === 'already_processed') {
                        updateSession(payload);
                        if (!list.querySelector(`[data-message-id="${payload.user_message_id}"]`)) {
                            appendUserMessage(payload.user_message_id, payload.user_text);
                        }
                        if (payload.assistant_text && !list.querySelector(`[data-message-id="${payload.assistant_message_id}"]`)) {
                            const row = appendAssistantMessage(payload.usage_request_id, payload.provider);
                            row.dataset.messageId = payload.assistant_message_id;
                            if (payload.assistant_html) setAssistantContent(row, payload.assistant_text, payload.assistant_html);
                        }
                        if (payload.status === 'succeeded') {
                            if (payload.assistant_message_id && payload.total_tokens !== null) {
                                const row = list.querySelector(`[data-message-id="${payload.assistant_message_id}"]`)
                                    || appendAssistantMessage(payload.usage_request_id, payload.provider);
                                addUsageReceipt(row, payload);
                            }
                            updateWallet(payload.wallet_balance);
                            stopTimer(payload.latency_ms);
                            setIdleAfterFailure('Message already sent. We prevented an accidental duplicate charge.', false);
                        } else {
                            updateWallet(payload.wallet_balance);
                            stopTimer(payload.latency_ms);
                            active = false;
                            if (payload.status === 'reconciliation_required') {
                                const message = payload.message || 'Connection timed out. A temporary hold is under review to prevent an overcharge.';
                                addNotice(message, true);
                                blockedText = submittedText;
                                sendButton.disabled = true;
                                setStatus(message);
                            } else if (payload.status === 'failed_before_upstream') {
                                addNotice('Message could not be sent. No charge was made.');
                                sendButton.disabled = false;
                                refreshIdempotencyKey();
                                setStatus('Message could not be sent. No charge was made.');
                            } else {
                                addNotice('Processing message (funds temporarily held)...');
                                blockedText = submittedText;
                                sendButton.disabled = true;
                                setStatus('Processing message (funds temporarily held)...', true);
                            }
                        }
                        return;
                    }
                    if (eventName === 'error') {
                        setIdleAfterFailure(payload.message || 'The message could not be sent.', false);
                    }
                });
                if (active) setIdleAfterFailure('The stream ended before the response was confirmed.', true);
            } catch (error) {
                active = false;
                stopTimer();
                if (accepted) {
                    blockedText = submittedText;
                    sendButton.disabled = true;
                    const message = 'Connection timed out. A temporary hold is under review to prevent an overcharge.';
                    setStatus(message);
                    addNotice(message, true);
                } else {
                    sendButton.disabled = false;
                    setStatus(error.message || 'The message could not be sent. Please try again.');
                    refreshIdempotencyKey();
                }
            }
        });
    }

    function startTimer(timer) {
        const startedAt = performance.now();
        let frame = 0;
        let lastPaint = 0;
        timer.hidden = false;
        function paint(now) {
            if (now - lastPaint >= 100) {
                timer.textContent = formatDuration(now - startedAt);
                lastPaint = now;
            }
            frame = window.requestAnimationFrame(paint);
        }
        frame = window.requestAnimationFrame(paint);
        return {
            stop(milliseconds = null) {
                window.cancelAnimationFrame(frame);
                timer.textContent = formatDuration(milliseconds === null ? performance.now() - startedAt : milliseconds);
            },
        };
    }

    function bindForms() {
        document.querySelectorAll('.message-form[data-stream-url]').forEach(bindForm);
    }

    document.addEventListener('DOMContentLoaded', bindForms);
})();
