"""
O motor: payload cru -> contexto canônico -> regra que casa -> mensagem pronta.

Não envia nada. Devolve o que *seria* enviado, e o motivo quando não dá.
Os adapters (sender.py, mailer.py) recebem o resultado disso.
"""

import json
import re

import db

# Valores que os agentes mandam quando não capturaram nada.
BLANK = {"", "unknown", "none", "null", "n/a", "na", "-", "not provided", "not mentioned"}

# Nomes de campo cujo conteúdo nunca deve ser gravado.
# O payload da Lily carrega godaddy_password, instagram_password, etc.
SECRET_HINTS = ("password", "passwd", "senha", "secret", "api_key", "apikey",
                "token", "credential", "username_and_password")

VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_.]+)\s*(?:\|([^}]*))?\}\}")

TRUE_WORDS = {"yes", "true", "1", "sim", "y", "success", "successful", "completed", "done"}
FALSE_WORDS = {"no", "false", "0", "nao", "não", "n", "failed", "unsuccessful", "none"}


def parse_body(raw_text: str, form_data=None) -> dict:
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


def is_secret(key: str) -> bool:
    k = (key or "").lower()
    return any(hint in k for hint in SECRET_HINTS)


def redact(payload):
    """Remove campos de aparência sensível antes de gravar o payload."""
    if isinstance(payload, dict):
        out = {}
        for k, v in payload.items():
            if is_secret(k):
                out[k] = "[removido pelo integrator.io]"
            else:
                out[k] = redact(v)
        return out
    if isinstance(payload, list):
        return [redact(v) for v in payload]
    return payload


def _unwrap(value):
    """
    Os campos de post_call_analysis vêm como {"type": "string", "value": "..."}.
    Devolve o value; se não for esse formato, devolve como está.
    """
    if isinstance(value, dict):
        if "value" in value:
            inner = value["value"]
            return inner if not isinstance(inner, (dict, list)) else ""
        return ""
    if isinstance(value, list):
        return ""
    return value


def flatten(payload: dict) -> dict:
    """
    Achata o payload numa tabela plana de campos utilizáveis.

    Cobre três formatos:
      - nível raiz simples            (payload dos clubes)
      - custom_parameters aninhado    (payload dos clubes)
      - post_call_analysis com .value (payload da Lily)

    Campos sensíveis nunca entram.
    """
    flat = {}

    # Os aninhados vêm primeiro e têm precedência: post_call_analysis.agent_name é
    # o agente humano da chamada, enquanto o agent_name do nível raiz é o nome da
    # própria IA. Se o raiz viesse antes, sobrescreveria o que interessa.
    for key in ("post_call_analysis", "post_call_analysisCollection",
                "custom_parameters", "custom_parametersCollection", "customParameters",
                "call_session", "call_sessionCollection"):
        nested = payload.get(key)
        if not isinstance(nested, dict):
            continue
        for k, v in nested.items():
            if is_secret(k):
                continue
            unwrapped = _unwrap(v)
            if unwrapped is not None and str(unwrapped).strip() != "":
                flat.setdefault(k, unwrapped)

    # O nível raiz preenche só o que ainda falta, e fica também com prefixo para
    # quando você precisar do valor do topo explicitamente.
    nested_keys = set()
    for key in ("post_call_analysis", "post_call_analysisCollection",
                "custom_parameters", "custom_parametersCollection", "customParameters"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            nested_keys.update(nested.keys())

    for k, v in payload.items():
        if is_secret(k) or isinstance(v, (dict, list)):
            continue
        # Se o agente declara o campo no bloco aninhado, aquele é o campo canônico —
        # mesmo vindo vazio. Deixar o topo preencher trocaria o agente humano pela IA.
        if k not in nested_keys:
            flat.setdefault(k, v)
        flat[f"root_{k}"] = v

    return flat


def clean(value) -> str:
    text = ("" if value is None else str(value)).strip()
    return "" if text.lower() in BLANK else text


def truthy(value) -> bool:
    text = clean(value).lower()
    if not text:
        return False
    if text in FALSE_WORDS:
        return False
    return True


# ---------------------------------------------------------------- regras

OPERATORS = {
    "equals": "é igual a",
    "not_equals": "é diferente de",
    "contains": "contém",
    "not_contains": "não contém",
    "is_empty": "está vazio",
    "is_not_empty": "está preenchido",
    "truthy": "é sim / verdadeiro",
    "falsy": "é não / vazio / falso",
}


def test_condition(flat: dict, cond: dict) -> bool:
    raw = flat.get(cond.get("field", ""), "")
    actual = clean(raw)
    expected = str(cond.get("value", "")).strip()
    op = cond.get("op", "equals")

    if op == "equals":       return actual.lower() == expected.lower()
    if op == "not_equals":   return actual.lower() != expected.lower()
    if op == "contains":     return expected.lower() in actual.lower()
    if op == "not_contains": return expected.lower() not in actual.lower()
    if op == "is_empty":     return actual == ""
    if op == "is_not_empty": return actual != ""
    if op == "truthy":       return truthy(raw)
    if op == "falsy":        return not truthy(raw)
    return False


def match_rule(flat: dict, rule) -> bool:
    try:
        conditions = json.loads(rule["conditions"] or "[]")
    except (ValueError, TypeError):
        conditions = []
    if not conditions:
        return True  # regra sem condição = fallback
    results = [test_condition(flat, c) for c in conditions]
    return all(results) if rule["match_all"] else any(results)


def render(text: str, ctx: dict) -> str:
    """
    Substituição de {{var}} com fallback: {{client_name|no name}}.
    Deliberadamente NÃO é Jinja — templates são editáveis pela interface.
    """
    def replace(match):
        name, fallback = match.group(1), match.group(2)
        value = clean(ctx.get(name))
        return value if value else (fallback or "").strip()
    return VAR_PATTERN.sub(replace, text or "")


# ---------------------------------------------------------------- pipeline

def process_all(venue, payload: dict) -> list:
    """
    Roda a cadeia de regras e devolve TODAS as mensagens que sairiam.

    Uma regra com "parar aqui" ligada encerra a cadeia (comportamento padrão).
    Desligada, a avaliação continua nas regras seguintes — é assim que o Make
    encadeia: um módulo dispara e o fluxo segue para o próximo filtro.
    """
    flat = flatten(payload)
    rules = db.active_rules(venue["id"])
    matched = []
    for rule in rules:
        if not match_rule(flat, rule):
            continue
        matched.append(rule)
        if rule["stop_after"]:
            break

    if not matched:
        base = _base_result(venue, flat)
        base["status"] = "no_rule"
        base["note"] = ("Nenhuma regra casou com este payload."
                        if rules else f"{venue['name']} não tem nenhuma regra ativa.")
        return [base]

    return [_apply_rule(venue, flat, rule) for rule in matched]


def _base_result(venue, flat: dict) -> dict:
    fields = {
        "call_session_id": clean(flat.get("call_session_id") or flat.get("callSessionId")),
        "customer_phone": db.to_e164(flat.get("phone") or flat.get("caller_number")
                                     or flat.get("client_phone") or ""),
        "customer_phone_raw": clean(flat.get("phone") or flat.get("caller_number")
                                    or flat.get("client_phone")),
        "agent_did": db.to_e164(flat.get("callee_number") or flat.get("to_phone_number") or ""),
        "package": clean(flat.get("package") or flat.get("package_name")),
        "first_name": clean(flat.get("first_name") or flat.get("client_name")),
    }
    return {
        **fields, "flat": flat, "status": "ready", "note": "", "link": "",
        "package_label": "", "rule_name": "", "channel": "", "subject": "",
        "recipient": "", "recipient_source": "",
        "body": "", "sms_body": "", "sms_from": venue["sender_number"] or "",
    }


def process(venue, payload: dict) -> dict:
    """Compatibilidade: devolve só a primeira mensagem da cadeia."""
    return process_all(venue, payload)[0]


def _apply_rule(venue, flat: dict, rule) -> dict:
    """Monta a mensagem de uma regra que já casou."""
    result = _base_result(venue, flat)
    fields = result
    result["rule_name"] = rule["name"]
    result["channel"] = rule["channel"]

    tpl = db.get_template(venue["id"], rule["template_name"])
    if not tpl:
        result["status"] = "no_template"
        result["note"] = (f"A regra '{rule['name']}' aponta para o template "
                          f"'{rule['template_name']}', que não existe.")
        return result

    # contexto de renderização: campos do payload + derivados
    ctx = dict(flat)
    ctx.update({k: v for k, v in fields.items() if k != "customer_phone_raw"})

    if "{{link}}" in tpl["body"] or "{{link}}" in (tpl["subject"] or ""):
        pkg = db.find_package(venue["id"], fields["package"]) if fields["package"] else None
        if not pkg:
            result["status"] = "no_link"
            result["note"] = (
                f"Pacote '{fields['package']}' não existe na tabela de {venue['name']} "
                f"(chave: {db.norm_key(fields['package'])})" if fields["package"]
                else "O template usa {{link}} mas o payload não trouxe o campo package"
            )
            return result
        result["link"] = pkg["link"]
        result["package_label"] = pkg["label"]
        ctx["link"] = pkg["link"]
        ctx["package"] = pkg["label"]

    result["body"] = render(tpl["body"], ctx)
    result["subject"] = render(tpl["subject"] or "", ctx)
    result["recipient"] = render(tpl["recipient"] or "", ctx).strip()

    if rule["channel"] == "sms":
        result["sms_body"] = result["body"]

        # Destinatário fixo no template tem precedência sobre o telefone do
        # payload. É o que permite alertas internos (emergência) usarem o mesmo
        # motor das mensagens para cliente.
        fixed = db.to_e164(result["recipient"]) if result["recipient"] else ""
        if fixed:
            result["customer_phone"] = fixed
            result["recipient_source"] = "template"

        if not result["customer_phone"]:
            result["status"] = "no_phone"
            result["note"] = (f"Telefone do cliente ausente ou inválido: "
                              f"{fields['customer_phone_raw'] or '(vazio)'}")
        elif not venue["sender_number"]:
            result["status"] = "no_sender"
            result["note"] = f"{venue['name']} não tem número remetente configurado"

    elif rule["channel"] == "email":
        fallback = db.get_setting("notify_email", "").strip()
        to = result["recipient"]
        if to and "@" in to:
            result["recipient_source"] = "payload"
        elif fallback:
            result["recipient"] = fallback
            result["recipient_source"] = "fallback"
            if tpl["recipient"]:
                result["note"] = ("O payload não trouxe endereço; usando o destinatário "
                                  "padrão de Ajustes.")
        else:
            result["status"] = "no_recipient"
            result["note"] = ("O template espera um endereço do payload e ele veio vazio, "
                              "e não há destinatário padrão configurado em Ajustes.")

    return result
