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

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "dev-only-change-me")
app.teardown_appcontext(db.close_db)

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD") or os.getenv("HUB_ADMIN_PASSWORD", "")

with app.app_context():
    db.init_db()


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
            return redirect(request.args.get("next") or url_for("events"))
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
        body=payload_text,
        call_session_id=fields.get("call_session_id"),
        customer_phone=fields.get("customer_phone"),
        agent_did=fields.get("agent_did"),
        package=fields.get("package"),
        first_name=fields.get("first_name"),
        matched_link=fields.get("link"),
        preview_body=fields.get("sms_body"),
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
    result = engine.process(venue, payload)

    dup = db.find_duplicate(venue["id"], result.get("call_session_id"))
    if dup:
        _record("duplicate", slug, venue, raw,
                note=f"Mesmo call_session_id do evento #{dup['id']}", result=result)
        return jsonify(ok=True, status="duplicate", of=dup["id"]), 200

    mode = db.get_setting("mode", "dry_run")
    status = result["status"]
    note = result.get("note", "")

    if status == "ready":
        # Fase 3 pluga o envio aqui. Por ora só registra o preview.
        status = "preview_ok"
        note = f"Modo {mode} — nada enviado. SMS pronto, {len(result['sms_body'])} caracteres."

    event_id = _record(status, slug, venue, raw, note=note, result=result)
    return jsonify(ok=True, event=event_id, status=status, mode=mode), 200


@app.route("/health")
def health():
    return jsonify(ok=True, mode=db.get_setting("mode", "dry_run"))


# ---------------------------------------------------------------- console

STATUS_LABELS = {
    "preview_ok": ("Pronto pra enviar", "live"),
    "duplicate": ("Duplicado", "hold"),
    "no_link": ("Pacote sem link", "fault"),
    "no_phone": ("Telefone inválido", "fault"),
    "no_template": ("Sem template", "fault"),
    "no_sender": ("Sem remetente", "fault"),
    "unknown_venue": ("Endpoint desconhecido", "fault"),
    "bad_token": ("Token recusado", "fault"),
    "venue_off": ("Venue desativada", "hold"),
}


@app.context_processor
def inject_globals():
    return {
        "mode": db.get_setting("mode", "dry_run"),
        "status_labels": STATUS_LABELS,
        "venues_nav": db.list_venues(),
    }


@app.route("/")
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


@app.route("/venues")
@login_required
def venues():
    base = os.getenv("PUBLIC_BASE_URL", request.url_root.rstrip("/"))
    rows = []
    for v in db.list_venues():
        rows.append({
            "v": v,
            "url": f"{base}/hook/{v['slug']}/k/{v['token']}",
            "packages": db.list_packages(v["id"]),
            "template": db.get_template(v["id"], "booking_link"),
        })
    return render_template("venues.html", rows=rows)


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
    return redirect(request.referrer or url_for("events"))


@app.template_filter("shortphone")
def shortphone(value):
    if not value or len(value) < 8:
        return value or "—"
    return f"{value[:5]}…{value[-4:]}"


if __name__ == "__main__":
    app.run(port=5080, debug=True)
