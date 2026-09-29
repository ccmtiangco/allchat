# AllChat

AllChat is a Django chat application with a responsive, server-rendered interface
and in-place assistant response streaming. It routes chat requests through
OpenAI-, Anthropic-, or Google-compatible proxy interfaces and records token-based
wallet charges. Provider credentials stay on the server and are never sent to
the browser.

## Quick Start

Requirements: Python 3.10 or newer and Git. SQLite is used by default for local
development.

```sh
git clone https://github.com/ccmtiangco/allchat.git
cd allchat
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

On Windows PowerShell, activate the environment with
`.\.venv\Scripts\Activate.ps1` instead of the `source` command.

Edit `.env` before making live chat requests. Set `DJANGO_SECRET_KEY` to a
private random value and add the proxy credentials for the interfaces you plan
to use: `OPENAI_PROXY_KEY`, `ANTHROPIC_PROXY_KEY`, and `GOOGLE_PROXY_KEY`. Keep
`.env` private; it is ignored by Git. Do not put credentials in source files or
commit history. Tests use mocked proxy requests and do not require API keys.

Apply the database migrations and start the development server:

```sh
python manage.py migrate
python manage.py runserver
```

Open <http://127.0.0.1:8000/> and create an account. New accounts receive the
configured initial wallet credit. To run the test suite, use
`python manage.py test`.

## How Chat Works

The browser submits chat messages to the authenticated Django application. With
JavaScript enabled, it reads a same-origin server-sent event stream and displays
assistant text as it arrives; without JavaScript, the regular form submission
and redirect flow remains available. The server reserves funds before contacting
the proxy and settles charges only after receiving valid final usage. Ambiguous
upstream failures are not automatically retried.

The three choices select API-compatible interfaces and do not necessarily
represent three different underlying model vendors. See the detailed guide for
the current proxy configuration and operational notes.

## Documentation

- [Local setup, configuration, billing, and deployment](doc/wiki/README.md)
- [Core architecture study](doc/study/01-core-architecture.md)
- [Streaming and pink accent study](doc/study/03-live-chat-streaming-and-pink-accent.md)
