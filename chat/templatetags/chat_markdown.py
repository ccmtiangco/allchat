import bleach
import markdown
from django import template
from django.utils.safestring import mark_safe

register = template.Library()

_ALLOWED_TAGS = {
    'a',
    'blockquote',
    'br',
    'code',
    'del',
    'em',
    'h1',
    'h2',
    'h3',
    'h4',
    'h5',
    'h6',
    'hr',
    'li',
    'ol',
    'p',
    'pre',
    'strong',
    'table',
    'tbody',
    'td',
    'th',
    'thead',
    'tr',
    'ul',
}
_ALLOWED_ATTRIBUTES = {'a': ['href', 'title'], 'code': ['class']}


@register.filter
def safe_markdown(value):
    rendered = markdown.markdown(str(value), extensions=['fenced_code', 'sane_lists', 'tables'])
    cleaned = bleach.clean(
        rendered,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        protocols={'http', 'https', 'mailto'},
        strip=True,
        strip_comments=True,
    )
    return mark_safe(cleaned)
