"""
Saida de SMS pela Telnyx.

Mesma interface do sender.py (Twilio): send() devolve sempre um dict e nunca
levanta excecao. Trocar de provedor nao muda nada no motor nem nas regras.
"""

import os
import time

import requests

import db

API = "https://api.telnyx.com/v2/messages"

# Erros em que repetir nao adianta — o problema e o destino ou a conta.
PERMANENT_CODES = {
    "40001",  # numero invalido
    "40002",  # numero nao roteavel
    "40003",  # destinatario bloqueou / opt-out
    "40300",  # destino nao suportado pela conta
    "40301",  # numero de origem nao pertence a conta
    "40305",  # numero nao habilitado para SMS
    "40010",  # perfil de mensagens invalido
    "10007",  # nao autorizado
}

FRIENDLY = {
    "40003": "O numero deu STOP e esta bloqueado na Telnyx. So volta se ele mandar START.",
    "40001": "A Telnyx recusou o numero de destino como invalido.",
    "40002": "Numero nao roteavel — geralmente fixo ou fora de cobertura.",
    "40301": "O numero remetente nao pertence a esta conta Telnyx.",
    "40305": "O numero remetente nao esta habilitado para SMS na Telnyx.",
    "10007": "Chave de API recusada. Confira TELNYX_API_KEY.",
    "40010": "Messaging Profile invalido. Confira TELNYX_MESSAGING_PROFILE_ID.",
}


def api_key() -> str:
    return os.getenv("TELNYX_API_KEY", "").strip()


def profile_id() -> str:
    return os.getenv("TELNYX_MESSAGING_PROFILE_ID", "").strip()


def configured() -> bool:
    return bool(api_key())


def send(to: str, from_: str, body: str, status_callback: str = "") -> dict:
    """
    Devolve sempre um dict:
      {"ok": True,  "sid": "<id>", "status": "queued"}
      {"ok": False, "code": "40003", "message": "...", "retryable": False}
    """
    key = api_key()
    if not key:
        return {"ok": False, "code": "no_credentials", "retryable": False,
                "message": "TELNYX_API_KEY nao configurada."}

    payload = {"from": from_, "to": to, "text": body}
    if profile_id():
        payload["messaging_profile_id"] = profile_id()
    if status_callback:
        payload["webhook_url"] = status_callback

    try:
        r = requests.post(
            API, json=payload, timeout=20,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
        )
    except requests.RequestException as exc:
        return {"ok": False, "code": "network", "retryable": True, "message": str(exc)}

    try:
        data = r.json()
    except ValueError:
        data = {}

    if r.status_code in (200, 201, 202):
        corpo = data.get("data", {})
        destinos = corpo.get("to") or [{}]
        return {"ok": True, "sid": corpo.get("id", ""),
                "status": destinos[0].get("status", "queued")}

    erros = data.get("errors") or [{}]
    primeiro = erros[0]
    code = str(primeiro.get("code", r.status_code))
    message = primeiro.get("detail") or primeiro.get("title") or f"HTTP {r.status_code}"
    retryable = (r.status_code >= 500 or r.status_code == 429) and code not in PERMANENT_CODES
    return {"ok": False, "code": code, "retryable": retryable,
            "message": FRIENDLY.get(code, message)}


def dispatch(app, delivery_id: int, status_callback: str = ""):
    """Envia uma entrega ja criada no banco e grava o resultado."""
    with app.app_context():
        if not db.claim_delivery(delivery_id):
            return
        d = db.get_delivery(delivery_id)
        resultado = send(d["to_number"], d["from_number"], d["body"], status_callback)

        if resultado["ok"]:
            db.update_delivery(delivery_id, status=resultado["status"] or "sent",
                               provider_sid=resultado["sid"], error_code=None,
                               error_message=None)
            return

        tentativas = d["attempts"] + 1
        status = "retry" if (resultado["retryable"] and tentativas < 3) else "failed"
        db.update_delivery(delivery_id, status=status, error_code=resultado["code"],
                           error_message=resultado["message"])


def parse_status_webhook(payload: dict) -> tuple:
    """
    Le o webhook de status da Telnyx.

    O formato e diferente do da Twilio: o id e o status vem aninhados em
    data.payload, e o status de entrega fica dentro da lista 'to'.

    Devolve (sid, status, codigo_de_erro) — qualquer um pode vir vazio.
    """
    corpo = (payload or {}).get("data", {}).get("payload", {})
    sid = corpo.get("id", "")

    destinos = corpo.get("to") or []
    status = destinos[0].get("status", "") if destinos else ""

    erros = corpo.get("errors") or []
    codigo = str(erros[0].get("code", "")) if erros else ""

    return sid, status, codigo
