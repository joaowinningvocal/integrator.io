"""
integrator.io — hub de automação de webhook do Winning Vocal.

Fase 1+2: recebe os POSTs dos agentes, normaliza, resolve o pacote e mostra
exatamente o SMS que sairia. Nenhuma mensagem é enviada nesta fase.
"""

import hmac
import json
import os
from functools import wraps

from flask import (
    Flask, abort, flash, g, jsonify, redirect, render_template,
    request, session, url_for,
)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import db
import engine
import mailer
import sender

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-only-change-me")
app.teardown_appcontext(db.close_db)

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or os.getenv("HUB_ADMIN_PASSWORD", "")

with app.app_context():
    db.init_db()


def public_base_url() -> str:
    """
    Normaliza PUBLIC_BASE_URL. Sem o esquema, a Twilio recusa o StatusCallback
    com "is not a valid URL" e a mensagem nem chega a ser criada.
    """
    base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    if not base:
        return ""
    if base.startswith("http://"):
        base = "https://" + base[len("http://"):]
    elif not base.startswith("https://"):
        base = "https://" + base.lstrip("/")
    return base


def status_callback_url() -> str:
    """URL que a Twilio chama quando o status da mensagem muda."""
    base = public_base_url()
    if not base:
        return ""
    with app.app_context():
        token = db.get_setting("status_callback_token", "")
    return f"{base}/twilio/status/{token}" if token else ""


def _retry_all(app_ref):
    import threading, time

    def loop():
        while True:
            time.sleep(60)
            try:
                with app_ref.app_context():
                    pending = db.pending_retries()
                    callback = status_callback_url()
                for d in pending:
                    if d["channel"] == "email":
                        mailer.dispatch(app_ref, d["id"])
                    else:
                        sender.dispatch(app_ref, d["id"], callback)
                    time.sleep(1)
            except Exception:
                pass

    threading.Thread(target=loop, daemon=True, name="retry-worker").start()


_retry_all(app)


# ---------------------------------------------------------------- auth

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("authed"):
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        supplied = request.form.get("password", "")
        if ADMIN_PASSWORD and hmac.compare_digest(supplied, ADMIN_PASSWORD):
            session["authed"] = True
            session.permanent = True
            return redirect(request.args.get("next") or url_for("home"))
        return render_template("login.html", error="Senha incorreta."), 401
    if not ADMIN_PASSWORD:
        return render_template(
            "login.html",
            error="ADMIN_PASSWORD não está definida. Configure no Railway antes de usar.",
        ), 503
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- receptor

def _safe_body(raw: str) -> str:
    """Nunca grava campos de aparência sensível (o payload da Lily carrega senhas)."""
    parsed = engine.parse_body(raw)
    if not parsed:
        return raw
    return json.dumps(engine.redact(parsed), ensure_ascii=False)


def _record(status, slug, venue, payload_text, note="", result=None):
    venue_id = venue["id"] if venue else None
    fields = (result or {})
    return db.insert_event(
        venue_id=venue_id,
        slug=slug,
        received_at=db.now_iso(),
        remote_ip=request.headers.get("X-Forwarded-For", request.remote_addr),
        content_type=request.content_type,
        headers=json.dumps(dict(request.headers), ensure_ascii=False),
        body=_safe_body(payload_text),
        call_session_id=fields.get("call_session_id"),
        customer_phone=fields.get("customer_phone"),
        agent_did=fields.get("agent_did"),
        package=fields.get("package"),
        first_name=fields.get("first_name"),
        matched_link=fields.get("link"),
        preview_body=fields.get("body") or fields.get("sms_body"),
        preview_from=fields.get("sms_from"),
        status=status,
        note=note or fields.get("note", ""),
    )


@app.route("/hook/<slug>", methods=["POST"])
@app.route("/hook/<slug>/k/<token>", methods=["POST"])
def inbound(slug, token=None):
    raw = request.get_data(as_text=True) or ""
    venue = db.get_venue_by_slug(slug)

    if venue is None:
        _record("unknown_venue", slug, None, raw, note=f"Nenhuma venue com slug '{slug}'")
        return jsonify(ok=False, error="unknown endpoint"), 404

    supplied = token or request.headers.get("X-Integrator-Token") or request.headers.get("X-Hub-Token") or request.args.get("k") or ""
    if not hmac.compare_digest(supplied, venue["token"]):
        _record("bad_token", slug, venue, raw, note="Token ausente ou incorreto")
        return jsonify(ok=False, error="unauthorized"), 401

    if not venue["active"]:
        _record("venue_off", slug, venue, raw, note="Venue está desativada no console")
        return jsonify(ok=False, error="venue disabled"), 403

    payload = engine.parse_body(raw, request.form)
    results = engine.process_all(venue, payload)
    result = results[0]

    dup = db.find_duplicate(venue["id"], result.get("call_session_id"))
    if dup:
        _record("duplicate", slug, venue, raw,
                note=f"Mesmo call_session_id do evento #{dup['id']}", result=result)
        return jsonify(ok=True, status="duplicate", of=dup["id"]), 200

    mode = db.get_setting("mode", "dry_run")
    status = result["status"]
    note = result.get("note", "")

    send_now = False
    if venue["always_live"]:
        mode = "live"
    if status == "ready":
        channel = result["channel"]
        if mode == "dry_run":
            status = "preview_ok"
            names = ", ".join(r["rule_name"] for r in results if r["status"] == "ready")
            note = f"Simulação — nada enviado. Regra{'s' if len(results) > 1 else ''}: {names}."
        elif mode == "test" and channel == "sms" and result["customer_phone"] not in db.allowlist():
            status = "blocked_test"
            note = f"Modo teste — {result['customer_phone']} não está na allowlist."
        else:
            status = "dispatched"
            note = f"Regra: {result['rule_name']}"
            send_now = True

    event_id = _record(status, slug, venue, raw, note=note, result=result)

    if send_now:
        sent = []
        for r in results:
            if r["status"] != "ready":
                continue
            if (mode == "test" and r["channel"] == "sms"
                    and not venue["always_live"]
                    and r["customer_phone"] not in db.allowlist()):
                continue
            d = db.get_delivery(_create_and_send(event_id, venue, r))
            sent.append({"rule": r["rule_name"], "channel": r["channel"],
                         "status": d["status"], "sid": d["provider_sid"]})
        if len(sent) > 1:
            db.update_event_note(event_id,
                                 f"{len(sent)} mensagens: " +
                                 ", ".join(x["rule"] for x in sent))
        return jsonify(ok=True, event=event_id, status=status, mode=mode,
                       sent=sent), 200

    return jsonify(ok=True, event=event_id, status=status, mode=mode), 200


def _create_and_send(event_id, venue, result) -> int:
    """Cria a entrega e despacha pelo adapter do canal da regra."""
    if result["channel"] == "email":
        delivery_id = db.create_delivery(
            event_id, venue["id"], result["recipient"],
            mailer.sender_address(), result["body"], channel="email",
            subject=result["subject"], rule_name=result["rule_name"],
        )
        mailer.dispatch(app, delivery_id)
    else:
        delivery_id = db.create_delivery(
            event_id, venue["id"], result["customer_phone"], result["sms_from"],
            result["body"], channel="sms", rule_name=result["rule_name"],
        )
        sender.dispatch(app, delivery_id, status_callback_url())
    return delivery_id


@app.route("/health")
def health():
    return jsonify(ok=True, mode=db.get_setting("mode", "dry_run"))


# ---------------------------------------------------------------- console

STATUS_LABELS = {
    "dispatched": ("Despachado", "live"),
    "test_send": ("Teste manual", "hold"),
    "blocked_test": ("Bloqueado (teste)", "hold"),
    "preview_ok": ("Pronto pra enviar", "live"),
    "duplicate": ("Duplicado", "hold"),
    "no_link": ("Pacote sem link", "fault"),
    "no_rule": ("Nenhuma regra casou", "fault"),
    "no_recipient": ("Sem destinatário", "fault"),
    "no_phone": ("Telefone inválido", "fault"),
    "no_template": ("Sem template", "fault"),
    "no_sender": ("Sem remetente", "fault"),
    "unknown_venue": ("Endpoint desconhecido", "fault"),
    "bad_token": ("Token recusado", "fault"),
    "venue_off": ("Venue desativada", "hold"),
}


DELIVERY_LABELS = {
    "queued": ("Na fila da Twilio", "hold"),
    "sending": ("Enviando", "hold"),
    "sent": ("Saiu da Twilio", "hold"),
    "delivered": ("Entregue", "live"),
    "undelivered": ("Não entregue", "fault"),
    "failed": ("Falhou", "fault"),
    "retry": ("Vai tentar de novo", "hold"),
}


@app.context_processor
def inject_globals():
    return {
        "mode": db.get_setting("mode", "dry_run"),
        "status_labels": STATUS_LABELS,
        "venues_nav": db.list_venues(),
        "delivery_labels": DELIVERY_LABELS,
        "twilio_ready": sender.configured(),
    }


@app.route("/")
@login_required
def home():
    """Painel de automações: o que está no ar, o que está parado, o que falhou."""
    cards = []
    for v in db.list_venues():
        rules_all = db.list_rules(v["id"])
        recent = db.list_events(limit=40, venue_id=v["id"])
        failures = [e for e in recent
                    if e["delivery_status"] in ("failed", "undelivered")
                    or e["status"] in ("no_rule", "no_link", "no_phone", "no_recipient",
                                       "no_template", "no_sender")]
        cards.append({
            "v": v,
            "rules": rules_all,
            "active_rules": [r for r in rules_all if r["active"]],
            "channels": sorted({r["channel"] for r in rules_all if r["active"]}),
            "last": recent[0] if recent else None,
            "count_24h": len([e for e in recent if e["received_at"] >= _since_hours(24)]),
            "failures": len(failures),
            "ready": bool(rules_all) and v["active"],
        })
    return render_template("home.html", cards=cards, counts=db.event_counts())


def _since_hours(hours: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")


@app.route("/venues/<int:venue_id>/toggle", methods=["POST"])
@login_required
def toggle_venue(venue_id):
    v = db.get_venue(venue_id)
    if not v:
        abort(404)
    db.update_venue(venue_id, v["name"], v["sender_number"] or "", 0 if v["active"] else 1)
    flash(f"{v['name']} " + ("pausada." if v["active"] else "ativada."))
    return redirect(request.referrer or url_for("home"))


@app.route("/rules/<int:rule_id>/toggle", methods=["POST"])
@login_required
def toggle_rule(rule_id):
    r = db.get_rule(rule_id)
    if not r:
        abort(404)
    db.save_rule(r["venue_id"], r["name"], r["priority"], r["conditions"], r["match_all"],
                 r["channel"], r["template_name"], r["stop_after"],
                 0 if r["active"] else 1, rule_id)
    flash(f"Regra '{r['name']}' " + ("pausada." if r["active"] else "ativada."))
    return redirect(request.referrer or url_for("home"))


@app.route("/calls")
@login_required
def events():
    venue_id = request.args.get("venue", type=int)
    status = request.args.get("status") or None
    rows = db.list_events(limit=200, venue_id=venue_id, status=status)
    return render_template(
        "events.html", events=rows, counts=db.event_counts(),
        active_venue=venue_id, active_status=status,
    )


@app.route("/events/<int:event_id>")
@login_required
def event_detail(event_id):
    row = db.get_event(event_id)
    if not row:
        abort(404)
    chars, segments, encoding = db.sms_segments(row["preview_body"] or "")
    return render_template(
        "event.html", e=row, pretty=db.pretty_json(row["body"]),
        chars=chars, segments=segments, encoding=encoding,
        deliveries=db.deliveries_for_event(event_id),
    )


@app.route("/events/<int:event_id>/replay", methods=["POST"])
@login_required
def replay(event_id):
    """Roda o payload guardado de novo pelo motor, com as regras atuais."""
    row = db.get_event(event_id)
    if not row or not row["venue_id"]:
        abort(404)
    venue = db.get_venue(row["venue_id"])
    payload = engine.parse_body(row["body"])
    result = engine.process(venue, payload)
    session["replay"] = {
        "event_id": event_id,
        "status": result["status"],
        "note": result.get("note", ""),
        "sms_body": result.get("sms_body", ""),
        "sms_from": result.get("sms_from", ""),
        "to": result.get("customer_phone", ""),
    }
    return redirect(url_for("event_detail", event_id=event_id))


@app.route("/rules")
@login_required
def rules():
    venue_id = request.args.get("venue", type=int)
    all_venues = db.list_venues()
    if not venue_id and all_venues:
        venue_id = all_venues[0]["id"]
    venue = db.get_venue(venue_id) if venue_id else None
    return render_template(
        "rules.html", venue=venue, all_venues=all_venues,
        rules=db.list_rules(venue_id) if venue_id else [],
        templates=db.list_templates(venue_id) if venue_id else [],
        operators=engine.OPERATORS,
        fields_seen=_known_fields(venue_id) if venue_id else [],
    )


def _known_fields(venue_id, limit=40):
    """Campos vistos nos payloads recentes desta venue, pra ajudar a montar regras."""
    seen = {}
    for e in db.list_events(limit=limit, venue_id=venue_id):
        for k, v in engine.flatten(engine.parse_body(e["body"])).items():
            value = engine.clean(v)
            if value:
                seen.setdefault(k, value)
    return sorted(seen.items())


@app.route("/rules/save", methods=["POST"])
@login_required
def save_rule():
    venue_id = request.form.get("venue_id", type=int)
    if request.form.get("delete"):
        db.delete_rule(int(request.form["delete"]))
        flash("Regra removida.")
        return redirect(url_for("rules", venue=venue_id))

    conditions = []
    fields = request.form.getlist("cond_field")
    ops = request.form.getlist("cond_op")
    values = request.form.getlist("cond_value")
    for f, o, v in zip(fields, ops, values):
        if f.strip():
            conditions.append({"field": f.strip(), "op": o, "value": v.strip()})

    db.save_rule(
        venue_id,
        request.form.get("name", "").strip() or "Sem nome",
        request.form.get("priority", type=int) or 100,
        json.dumps(conditions, ensure_ascii=False),
        1 if request.form.get("match_all") else 0,
        request.form.get("channel", "sms"),
        request.form.get("template_name", "").strip(),
        1 if request.form.get("stop_after") else 0,
        1 if request.form.get("active") else 0,
        request.form.get("rule_id", type=int),
    )
    flash("Regra salva.")
    return redirect(url_for("rules", venue=venue_id))


@app.route("/templates/save", methods=["POST"])
@login_required
def save_template_route():
    venue_id = request.form.get("venue_id", type=int)
    db.save_template(
        venue_id,
        request.form.get("name", "").strip(),
        request.form.get("body", ""),
        request.form.get("subject", "").strip(),
        request.form.get("recipient", "").strip(),
    )
    flash("Template salvo.")
    return redirect(url_for("rules", venue=venue_id))


@app.route("/venues")
@login_required
def venues():
    base = public_base_url() or request.url_root.rstrip("/")
    rows = []
    for v in db.list_venues():
        rows.append({
            "v": v,
            "url": f"{base}/hook/{v['slug']}/k/{v['token']}",
            "packages": db.list_packages(v["id"]),
            "template": db.get_template(v["id"], "booking_link"),
        })
    return render_template("venues.html", rows=rows)


@app.route("/webhooks")
@login_required
def webhooks():
    base = public_base_url() or request.url_root.rstrip("/")
    rows = [{
        "v": v,
        "url": f"{base}/hook/{v['slug']}/k/{v['token']}",
    } for v in db.list_venues()]
    return render_template("webhooks.html", rows=rows)


@app.route("/venues/<int:venue_id>/save", methods=["POST"])
@login_required
def save_venue(venue_id):
    db.update_venue(
        venue_id,
        request.form.get("name", "").strip(),
        db.to_e164(request.form.get("sender_number", "")) or request.form.get("sender_number", "").strip(),
        1 if request.form.get("active") else 0,
    )
    db.save_template(venue_id, "booking_link", request.form.get("template", ""))
    flash("Venue salva.")
    return redirect(url_for("venues"))


@app.route("/venues/<int:venue_id>/rotate", methods=["POST"])
@login_required
def rotate(venue_id):
    db.rotate_venue_token(venue_id)
    flash("Token trocado. Atualize a URL no agente do Agni.")
    return redirect(url_for("venues"))


@app.route("/venues/<int:venue_id>/packages", methods=["POST"])
@login_required
def save_packages(venue_id):
    if request.form.get("delete"):
        db.delete_package(int(request.form["delete"]))
        flash("Pacote removido.")
    else:
        db.upsert_package(
            venue_id,
            request.form.get("label", "").strip(),
            request.form.get("price", "").strip(),
            request.form.get("link", "").strip(),
            request.form.get("package_id", type=int),
        )
        flash("Pacote salvo.")
    return redirect(url_for("venues"))


@app.route("/mode", methods=["POST"])
@login_required
def set_mode():
    chosen = request.form.get("mode", "dry_run")
    if chosen in ("dry_run", "test", "live"):
        db.set_setting("mode", chosen)
        flash(f"Modo alterado para {chosen}.")
    return redirect(request.referrer or url_for("home"))


@app.route("/events/<int:event_id>/send", methods=["POST"])
@login_required
def send_now(event_id):
    """Reenvia (ou envia pela primeira vez) o SMS de um evento já registrado."""
    row = db.get_event(event_id)
    if not row or not row["venue_id"]:
        abort(404)
    venue = db.get_venue(row["venue_id"])
    result = engine.process(venue, engine.parse_body(row["body"]))

    if result["status"] != "ready":
        flash(f"Não dá pra enviar: {result.get('note') or result['status']}")
        return redirect(url_for("event_detail", event_id=event_id))

    mode = db.get_setting("mode", "dry_run")
    if (mode == "test" and result["channel"] == "sms"
            and result["customer_phone"] not in db.allowlist()):
        flash(f"Modo teste — {result['customer_phone']} não está na allowlist.")
        return redirect(url_for("event_detail", event_id=event_id))
    if mode == "dry_run":
        flash("Modo simulação — troque para Teste ou Ao vivo para enviar.")
        return redirect(url_for("event_detail", event_id=event_id))

    delivery_id = _create_and_send(event_id, venue, result)
    d = db.get_delivery(delivery_id)
    label = DELIVERY_LABELS.get(d["status"], (d["status"],))[0]
    flash(f"{label}." + (f" {d['error_message']}" if d["error_message"] else ""))
    return redirect(url_for("event_detail", event_id=event_id))


@app.route("/twilio/status/<token>", methods=["POST"])
def twilio_status(token):
    """A Twilio chama aqui quando o status da mensagem muda."""
    expected = db.get_setting("status_callback_token", "")
    if not expected or not hmac.compare_digest(token, expected):
        return ("", 403)
    sid = request.form.get("MessageSid", "")
    status = request.form.get("MessageStatus", "")
    error = request.form.get("ErrorCode", "")
    d = db.delivery_by_sid(sid) if sid else None
    if d and status:
        fields = {"status": status}
        if error:
            fields["error_code"] = error
            fields["error_message"] = sender.FRIENDLY.get(error, f"Twilio código {error}")
        db.update_delivery(d["id"], **fields)
    return ("", 204)


@app.route("/test", methods=["GET", "POST"])
@login_required
def testbench():
    """
    Dispara uma mensagem sem precisar de ligação.
    Só envia para números da allowlist — por isso funciona em qualquer modo.
    """
    venues_all = db.list_venues()
    packages = {v["id"]: [dict(p) for p in db.list_packages(v["id"])] for v in venues_all}
    allow = sorted(db.allowlist())
    form = {
        "venue_id": request.form.get("venue_id", type=int) or (venues_all[0]["id"] if venues_all else None),
        "package": request.form.get("package", ""),
        "first_name": request.form.get("first_name", ""),
        "to": request.form.get("to", ""),
    }
    preview = None

    if request.method == "POST" and form["venue_id"]:
        venue = db.get_venue(form["venue_id"])
        payload = {
            "call_session_id": f"testbench-{db.now_iso()}",
            "phone": form["to"] or (allow[0] if allow else ""),
            "callee_number": venue["sender_number"] or "",
            "package": form["package"],
            "first_name": form["first_name"],
        }
        result = engine.process(venue, payload)
        preview = {"result": result, "sent": None}

        if request.form.get("action") == "send":
            to = result["customer_phone"]
            if not allow:
                flash("Adicione um número na allowlist em Ajustes antes de enviar um teste.")
            elif to not in allow:
                flash(f"{to or 'O número informado'} não está na allowlist. A bancada só envia para números dela.")
            elif result["status"] != "ready":
                flash(f"Não dá pra enviar: {result.get('note') or result['status']}")
            elif not sender.configured():
                flash("Credenciais da Twilio não configuradas. Veja Ajustes.")
            else:
                event_id = db.insert_event(
                    venue_id=venue["id"], slug=venue["slug"], received_at=db.now_iso(),
                    remote_ip="bancada", content_type="application/json",
                    headers="{}", body=json.dumps(payload, ensure_ascii=False),
                    call_session_id=payload["call_session_id"],
                    customer_phone=to, agent_did=payload["callee_number"],
                    package=result["package_label"], first_name=result["first_name"],
                    matched_link=result["link"], preview_body=result["sms_body"],
                    preview_from=result["sms_from"], status="test_send",
                    note="Disparado pela bancada de testes",
                )
                delivery_id = db.create_delivery(
                    event_id, venue["id"], to, result["sms_from"], result["sms_body"]
                )
                sender.dispatch(app, delivery_id, status_callback_url())
                d = db.get_delivery(delivery_id)
                label = DELIVERY_LABELS.get(d["status"], (d["status"],))[0]
                flash(f"{label}." + (f" {d['error_message']}" if d["error_message"] else ""))
                preview["sent"] = dict(d)

    return render_template(
        "testbench.html", venues_all=venues_all, packages=packages,
        allow=allow, form=form, preview=preview,
    )


@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        db.set_setting("test_allowlist", request.form.get("test_allowlist", "").strip())
        db.set_setting("notify_email", request.form.get("notify_email", "").strip())
        flash("Ajustes salvos.")
        return redirect(url_for("settings"))
    return render_template(
        "settings.html",
        allowlist=db.get_setting("test_allowlist", ""),
        parsed=sorted(db.allowlist()),
        callback=status_callback_url(),
        notify_email=db.get_setting("notify_email", ""),
        smtp_ready=mailer.configured(),
        smtp_from=mailer.sender_address(),
        deliveries=db.list_deliveries(limit=60),
    )


@app.template_filter("fromjson")
def fromjson_filter(raw):
    try:
        return json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []


@app.template_filter("segments")
def segments_filter(body):
    return db.sms_segments(body or "")


@app.template_filter("shortphone")
def shortphone(value):
    if not value or len(value) < 8:
        return value or "—"
    return f"{value[:5]}…{value[-4:]}"


if __name__ == "__main__":
    app.run(port=5080, debug=True)
