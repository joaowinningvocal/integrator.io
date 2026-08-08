"""
O motor: payload cru -> contexto canônico -> link do pacote -> texto do SMS.

Não envia nada. Devolve o que *seria* enviado, e o motivo quando não dá.
Fase 3 pluga o adapter da Twilio na saída disso, sem mexer aqui.
"""

import json
import re

import db

# Valores que os agentes mandam quando não capturaram o nome.
BLANK_NAMES = {"", "unknown", "none", "null", "n/a", "na", "-"}

VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*(?:\|([^}]*))?\}\}")


def parse_body(raw_text: str, form_data=None) -> dict:
    """Aceita JSON; cai pra form-encoded se o agente mandar assim."""
    if raw_text:
        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, dict):
                return parsed
            return {"_root": parsed}
        except (ValueError, TypeError):
            pass
    if form_data:
        return dict(form_data)
    return {}


def _flatten(payload: dict) -> dict:
    """
    Junta o nível raiz com os parâmetros customizados aninhados.
    O Make exibe isso como 'custom_parametersCollection', mas no corpo cru
    costuma vir como um objeto. Os dois casos são cobertos.
    """
    flat = {k: v for k, v in payload.items() if not isinstance(v, (dict, list))}
    for key in ("custom_parameters", "custom_parametersCollection", "customParameters"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            for k, v in nested.items():
                if not isinstance(v, (dict, list)):
                    flat.setdefault(k, v)
    return flat


def _clean_name(value) -> str:
    text = ("" if value is None else str(value)).strip()
    return "" if text.lower() in BLANK_NAMES else text


def extract(payload: dict) -> dict:
    """Payload do agente -> campos canônicos."""
    flat = _flatten(payload)

    def pick(*names):
        for n in names:
            v = flat.get(n)
            if v is not None and str(v).strip() != "":
                return str(v).strip()
        return ""

    return {
        "call_session_id": pick("call_session_id", "callSessionId", "session_id"),
        "room_id": pick("room_id", "roomId"),
        "customer_phone": db.to_e164(pick("phone", "caller_number", "from", "customer_phone")),
        "customer_phone_raw": pick("phone", "caller_number", "from", "customer_phone"),
        "agent_did": db.to_e164(pick("callee_number", "to_phone_number", "to")),
        "package": pick("package", "package_name"),
        "first_name": _clean_name(pick("first_name", "firstName", "name")),
    }


def render(template_body: str, ctx: dict) -> str:
    """
    Substituição simples de {{var}} com fallback opcional: {{first_name|there}}.
    Deliberadamente NÃO é Jinja — templates são editáveis pelo dashboard e
    Jinja ali seria execução de código pela interface.
    """
    def replace(match):
        name, fallback = match.group(1), match.group(2)
        value = ctx.get(name)
        text = "" if value is None else str(value).strip()
        if not text:
            text = (fallback or "").strip()
        return text

    return VAR_PATTERN.sub(replace, template_body or "")


def process(venue, payload: dict) -> dict:
    """
    Roda o pipeline inteiro pra uma venue já identificada.
    Devolve status + preview. Status possíveis:
      ready      -> tem tudo pra enviar
      no_phone   -> telefone do cliente ausente ou inválido
      no_link    -> pacote não encontrado na tabela da venue
      no_template-> venue sem template cadastrado
    """
    fields = extract(payload)
    result = {
        **fields,
        "status": "ready",
        "note": "",
        "link": "",
        "package_label": "",
        "sms_body": "",
        "sms_from": venue["sender_number"] or "",
    }

    problems = []

    if not fields["customer_phone"]:
        problems.append("no_phone")
        result["note"] = (
            f"Telefone do cliente ausente ou inválido: {fields['customer_phone_raw'] or '(vazio)'}"
        )

    pkg = db.find_package(venue["id"], fields["package"]) if fields["package"] else None
    if pkg:
        result["link"] = pkg["link"]
        result["package_label"] = pkg["label"]
    else:
        problems.append("no_link")
        if fields["package"]:
            result["note"] = (
                f"Pacote \"{fields['package']}\" não existe na tabela de "
                f"{venue['name']} (chave: {db.norm_key(fields['package'])})"
            )
        else:
            result["note"] = "Payload não trouxe o campo package"

    tpl = db.get_template(venue["id"], "booking_link")
    if not tpl:
        problems.append("no_template")
        result["note"] = f"{venue['name']} não tem template booking_link"

    if not venue["sender_number"]:
        problems.append("no_sender")
        result["note"] = f"{venue['name']} não tem número remetente configurado"

    if problems:
        result["status"] = problems[0]
        return result

    result["sms_body"] = render(
        tpl["body"],
        {
            "first_name": fields["first_name"],
            "link": result["link"],
            "package": result["package_label"],
            "venue_name": venue["name"],
            "phone": fields["customer_phone"],
        },
    )
    return result
