"""
Saída de e-mail via SMTP. Usado para notificação interna.

O destinatário vem sempre das configurações, nunca do payload — é notificação
para a equipe, e assim não há como um dado de lead redirecionar o e-mail.
"""

import os
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr

import db


def credentials():
    return (
        os.getenv("SMTP_HOST", "smtp.gmail.com"),
        int(os.getenv("SMTP_PORT", "587")),
        os.getenv("SMTP_USER", ""),
        os.getenv("SMTP_PASSWORD", ""),
    )


def configured() -> bool:
    _, _, user, password = credentials()
    return bool(user and password)


def sender_address() -> str:
    # O Gmail exige que o From seja o próprio usuário autenticado (ou um alias
    # configurado em "Enviar e-mail como"). Qualquer outro valor é reescrito.
    return os.getenv("SMTP_FROM", "") or os.getenv("SMTP_USER", "")


def send(to: str, subject: str, body: str) -> dict:
    """
    Devolve sempre um dict, nunca levanta exceção:
      {"ok": True,  "detail": "..."}
      {"ok": False, "code": "...", "message": "...", "retryable": bool}
    """
    host, port, user, password = credentials()
    if not (user and password):
        return {"ok": False, "code": "no_credentials", "retryable": False,
                "message": "SMTP_USER / SMTP_PASSWORD não configurados."}
    if not to:
        return {"ok": False, "code": "no_recipient", "retryable": False,
                "message": "Nenhum destinatário configurado."}

    msg = EmailMessage()
    msg["From"] = formataddr(("integrator.io", sender_address()))
    msg["To"] = to
    msg["Subject"] = subject or "(sem assunto)"
    msg.set_content(body)

    try:
        context = ssl.create_default_context()
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=25) as smtp:
                smtp.login(user, password)
                smtp.send_message(msg)
        else:
            with smtplib.SMTP(host, port, timeout=25) as smtp:
                smtp.starttls(context=context)
                smtp.login(user, password)
                smtp.send_message(msg)
        return {"ok": True, "detail": "enviado"}

    except smtplib.SMTPAuthenticationError as exc:
        return {"ok": False, "code": "auth", "retryable": False,
                "message": "SMTP recusou as credenciais. Se for Gmail, confirme que "
                           "a verificação em duas etapas está ativa e que a senha é "
                           "um app password de 16 caracteres, sem espaços. "
                           f"({exc.smtp_code})"}
    except smtplib.SMTPRecipientsRefused:
        return {"ok": False, "code": "recipient_refused", "retryable": False,
                "message": f"O servidor recusou o destinatário {to}."}
    except smtplib.SMTPException as exc:
        return {"ok": False, "code": "smtp", "retryable": True, "message": str(exc)}
    except OSError as exc:
        return {"ok": False, "code": "network", "retryable": True, "message": str(exc)}


def dispatch(app, delivery_id: int):
    with app.app_context():
        if not db.claim_delivery(delivery_id):
            return
        d = db.get_delivery(delivery_id)
        result = send(d["to_number"], d["subject"] or "", d["body"])

        if result["ok"]:
            db.update_delivery(delivery_id, status="sent", error_code=None, error_message=None)
            return

        attempts = d["attempts"] + 1
        status = "retry" if (result["retryable"] and attempts < 3) else "failed"
        db.update_delivery(delivery_id, status=status, error_code=result["code"],
                           error_message=result["message"])
