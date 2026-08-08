"""
Saída de mensagens. Hoje só Twilio SMS.

Adicionar um provedor novo = escrever uma função com esta assinatura:
    send(to, from_, body, status_callback) -> dict
e apontar dispatch() pra ela. O motor não muda.
"""

import os
import threading
import time

import requests

import db

TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

# Erros da Twilio que não adianta tentar de novo — o problema é o destino,
# não a rede. Repetir só queima cota e dinheiro.
PERMANENT_CODES = {
    "21211",  # número 'To' inválido
    "21214",  # 'To' não é discável
    "21606",  # 'From' não é um número válido da conta
    "21610",  # destinatário deu STOP
    "21612",  # rota não disponível
    "21614",  # 'To' não é celular
    "21408",  # conta sem permissão pra essa região
    "30006",  # landline ou não alcançável
    "30007",  # filtrado pela operadora
}

FRIENDLY = {
    "21610": "O número deu STOP e está bloqueado na Twilio. Não dá pra reenviar até ele mandar START.",
    "21614": "O número não é celular — SMS não chega em fixo.",
    "21211": "A Twilio recusou o número de destino como inválido.",
    "30007": "A operadora filtrou a mensagem. Costuma ser conteúdo ou registro A2P.",
    "21606": "O número remetente não pertence a esta conta Twilio, ou não está habilitado pra SMS.",
    "20003": "Credenciais da Twilio recusadas. Confira TWILIO_ACCOUNT_SID e TWILIO_AUTH_TOKEN.",
}


def credentials():
    return os.getenv("TWILIO_ACCOUNT_SID", ""), os.getenv("TWILIO_AUTH_TOKEN", "")


def configured() -> bool:
    sid, token = credentials()
    return bool(sid and token)


def send(to: str, from_: str, body: str, status_callback: str = "") -> dict:
    """
    Devolve sempre um dict:
      {"ok": True,  "sid": "SM...", "status": "queued"}
      {"ok": False, "code": "21610", "message": "...", "retryable": False}
    Nunca levanta exceção — quem chama decide o que fazer.
    """
    sid, token = credentials()
    if not (sid and token):
        return {"ok": False, "code": "no_credentials", "retryable": False,
                "message": "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN não configurados."}

    data = {"To": to, "From": from_, "Body": body}
    if status_callback:
        data["StatusCallback"] = status_callback

    try:
        r = requests.post(
            TWILIO_API.format(sid=sid), data=data, auth=(sid, token), timeout=20
        )
    except requests.RequestException as exc:
        return {"ok": False, "code": "network", "retryable": True, "message": str(exc)}

    try:
        payload = r.json()
    except ValueError:
        payload = {}

    if r.status_code in (200, 201):
        return {"ok": True, "sid": payload.get("sid", ""),
                "status": payload.get("status", "queued")}

    code = str(payload.get("code", r.status_code))
    message = payload.get("message") or f"HTTP {r.status_code}"
    retryable = (r.status_code >= 500 or r.status_code == 429) and code not in PERMANENT_CODES
    return {"ok": False, "code": code, "retryable": retryable,
            "message": FRIENDLY.get(code, message)}


def dispatch(app, delivery_id: int, status_callback: str = ""):
    """Envia uma entrega já criada no banco e grava o resultado."""
    with app.app_context():
        if not db.claim_delivery(delivery_id):
            return  # outro worker pegou, ou já saiu
        d = db.get_delivery(delivery_id)
        result = send(d["to_number"], d["from_number"], d["body"], status_callback)

        if result["ok"]:
            db.update_delivery(delivery_id, status=result["status"] or "sent",
                               provider_sid=result["sid"], error_code=None,
                               error_message=None)
            return

        attempts = d["attempts"] + 1
        status = "retry" if (result["retryable"] and attempts < 3) else "failed"
        db.update_delivery(delivery_id, status=status, error_code=result["code"],
                           error_message=result["message"])


def start_retry_worker(app, status_callback_builder):
    """Roda no fundo e reprocessa o que ficou em 'retry'. Backoff simples."""
    def loop():
        while True:
            time.sleep(60)
            try:
                with app.app_context():
                    pending = db.pending_retries()
                    callback = status_callback_builder()
                for d in pending:
                    dispatch(app, d["id"], callback)
                    time.sleep(1)
            except Exception:  # nunca deixa a thread morrer
                pass

    threading.Thread(target=loop, daemon=True, name="retry-worker").start()
