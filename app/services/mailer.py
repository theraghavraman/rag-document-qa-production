"""Optional SMTP email for verifiable onboarding and invitations."""
import logging
import smtplib
import ssl
from email.message import EmailMessage
from html import escape
from urllib.parse import quote
from app.config import get_settings

logger = logging.getLogger(__name__)


def send_access_link(email: str, token: str, operation: str):
    settings = get_settings()
    if not settings.smtp_enabled:
        raise RuntimeError('SMTP is not configured')
    if operation not in {'verify', 'invite'}:
        raise ValueError('Unsupported mail operation')
    url = settings.public_app_url + '/app?' + operation + '=' + quote(token, safe='')
    title = 'Verify your RAG workspace registration' if operation == 'verify' else 'Join your RAG workspace'
    body = (f'Use the following link within 48 hours:\n\n{url}\n\n'
            'If you did not request this, you can ignore this message.')
    msg = EmailMessage()
    msg['Subject'] = title
    msg['From'] = settings.smtp_sender
    msg['To'] = email
    msg.set_content(body)
    msg.add_alternative(f'<p>{escape(title)}</p><p><a href="{escape(url, quote=True)}">Continue securely</a>'
                        f'</p><p>This link expires in 48 hours.</p>', subtype='html')
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as client:
        if settings.smtp_starttls:
            client.starttls(context=ssl.create_default_context())
        if settings.smtp_user:
            client.login(settings.smtp_user, settings.smtp_password)
        client.send_message(msg)
