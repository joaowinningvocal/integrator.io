"""
Camada de dados do hub. Tudo que toca o banco passa por aqui.
Trocar SQLite por Postgres depois = reescrever só este arquivo.
"""

import json
import os
import re
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from flask import g


def _resolve_data_dir() -> Path:
    """No Railway o volume fica em /data. Local, cai pra ./data."""
    candidate = Path(os.getenv("DATA_DIR", "/data"))
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return candidate
    except OSError:
        fallback = Path(__file__).parent / "data"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "hub.sqlite3"

SCHEMA = """
CREATE TABLE IF NOT EXISTS venues (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    token         TEXT NOT NULL,
    sender_number TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    pinned        INTEGER NOT NULL DEFAULT 0,
    always_live   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    label    TEXT NOT NULL,
    norm_key TEXT NOT NULL,
    price    TEXT,
    link     TEXT NOT NULL,
    UNIQUE (venue_id, norm_key)
);

CREATE TABLE IF NOT EXISTS templates (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    subject   TEXT NOT NULL DEFAULT '',
    recipient TEXT NOT NULL DEFAULT '',
    body      TEXT NOT NULL,
    UNIQUE (venue_id, name)
);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id        INTEGER REFERENCES venues(id) ON DELETE SET NULL,
    slug            TEXT,
    received_at     TEXT NOT NULL,
    remote_ip       TEXT,
    content_type    TEXT,
    headers         TEXT,
    body            TEXT,
    call_session_id TEXT,
    customer_phone  TEXT,
    agent_did       TEXT,
    package         TEXT,
    first_name      TEXT,
    matched_link    TEXT,
    preview_body    TEXT,
    preview_from    TEXT,
    status          TEXT NOT NULL,
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_received ON events(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_events_session  ON events(venue_id, call_session_id);
CREATE INDEX IF NOT EXISTS idx_events_status   ON events(status);

CREATE TABLE IF NOT EXISTS rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    venue_id      INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    priority      INTEGER NOT NULL DEFAULT 100,
    conditions    TEXT NOT NULL DEFAULT '[]',
    match_all     INTEGER NOT NULL DEFAULT 1,
    channel       TEXT NOT NULL DEFAULT 'sms',
    template_name TEXT NOT NULL,
    recipient     TEXT NOT NULL DEFAULT '',
    stop_after    INTEGER NOT NULL DEFAULT 1,
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_rules_venue ON rules(venue_id, priority);

CREATE TABLE IF NOT EXISTS deliveries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      INTEGER REFERENCES events(id) ON DELETE SET NULL,
    venue_id      INTEGER REFERENCES venues(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    channel       TEXT NOT NULL DEFAULT 'sms',
    rule_name     TEXT,
    subject       TEXT,
    to_number     TEXT NOT NULL,
    from_number   TEXT NOT NULL,
    body          TEXT NOT NULL,
    provider      TEXT NOT NULL DEFAULT 'twilio',
    provider_sid  TEXT,
    status        TEXT NOT NULL,
    error_code    TEXT,
    error_message TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_deliveries_event ON deliveries(event_id);
CREATE INDEX IF NOT EXISTS idx_deliveries_sid   ON deliveries(provider_sid);
CREATE INDEX IF NOT EXISTS idx_deliveries_state ON deliveries(status);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    # dry_run -> só registra o que enviaria
    # test    -> envia apenas pra allowlist  (Fase 3)
    # live    -> envia de verdade            (Fase 4)
    "mode": "dry_run",
    "test_allowlist": "",
    "status_callback_token": "",
    "notify_email": "",
}

# --------------------------------------------------------------------------
# Seed inicial: as duas venues que entram primeiro.
# Os rodapés vieram do texto atual do Make e precisam de conferência.
# --------------------------------------------------------------------------

HUSTLER_TEMPLATE = """Hey {{first_name|there}},
This is the link for you to book your reservation:
{{link}}
This SMS was sent by Heather.
Larry Flynt's Hustler Club
6007 Dean Martin Drive
(702) 795-3131"""

KINGS_TEMPLATE = """Hey {{first_name|there}},
This is the link for you to book your reservation:
{{link}}
This SMS was sent by Ace.
Kings of Hustler
6007 Dean Martin Drive
(702) 795-3131"""

LILY_BASE = """Lily handled a call!
ACTION NEEDED: {{action_needed}}
Client Name: {{client_name}}
Client Phone: {{client_phone}}
Client E-mail: {{client_email}}
Call Summary: {{call_summary}}"""

LILY_TRANSFER_OK = """Hi, {{agent_name|there}},

{{client_name}} reached out to you and the AI agent transferred the call!

Full name:
{{client_name}}
Client E-mail:
{{client_email}}
Phone: {{client_phone}}
Summary of the call:
{{call_summary}}
Service: {{service}}
Property: {{property}}
{{property_link}}

Recording:
{{recording_url}}"""

LILY_TRANSFER_FAIL = """Hi there, {{agent_name|there}},

Lily tried to transfer a call to you, but the call transfer was unsuccessful, so she gathered the client's information so you can call back.

Full name:
{{client_name}}
Client E-mail:
{{client_email}}
Phone: {{client_phone}}
Summary of the call:
{{call_summary}}
Service: {{service}}
Property: {{property}}
{{property_link}}

Recording:
{{recording_url}}"""

# Venues sem tabela de pacotes ainda. Criadas desligadas: o webhook responde 403
# e registra, mas não envia, até você cadastrar pacotes e ligar.
PENDING_VENUES = [
]

# Slugs que não devem existir. Removidos no boot se não tiverem histórico.
RETIRED_SLUGS = ("james-wv",)

YPSILANTI_TEMPLATE = """Hey there, {{first_name|there}}
This is the link for you to book your reservation:
{{link}}
This SMS was sent by Heather, Deja Vu's Concierge.
Deja Vu Showgirls
(734) 557-8812
dejavuypsilanti.com"""

STOCKTON_TEMPLATE = """Hey there, {{first_name|there}}

This is the link for you to book your reservation:
{{link}}

This SMS was sent by Heather, Deja Vu's Concierge.

Deja Vu Showgirls Stockton
(209) 462-7800
dejavustockton.com"""

KALAMAZOO_TEMPLATE = """Hey there!

This is the link for you to book your package:

{{link}}

Jade from Deja Vu"""

CATS_MEOW_TEMPLATE = """Hey there, {{first_name|there}}

This is the link for you to book your reservation:
{{link}}

This SMS was sent by Heather, Cats Meow's Concierge.

Cats Meow Karaoke Bar
catskaraoke.com"""

BARELY_LEGAL_TEMPLATE = """Hey there, {{first_name|there}}

This is the link for you to book your reservation:
{{link}}

This SMS was sent by Heather, Barely Legal's Concierge.

Barely Legal NOLA
844-571-6340
https://barelylegalnola.com"""

HUSTLER_NOLA_TEMPLATE = """Hey there, {{first_name|there}}

This is the link for you to book your reservation:
{{link}}

This SMS was sent by Heather, Hustler's Concierge.

Larry Flynt's Hustler Club
504-524-0010
neworleanshustlerclub.com"""

EMERGENCY_DISPATCH = """POSSIBLE EMERGENCY AT {{club_name}}

The AI detected the following incident:
{{report}}
Caller ID: {{phone}}"""

EMERGENCY_JASON = """Jason, POSSIBLE EMERGENCY AT {{club_name}}

The AI detected the following incident:
{{report}}"""

GOBEST_INQUIRY = """New GoBEST inquiry — {{inquiry_type}}

Name: {{caller_name|not provided}}
Company: {{company_name|not provided}}
Phone: {{caller_phone|not provided}}
E-mail: {{caller_email|not provided}}

What they need:
{{summary|not provided}}

Handled by the AI receptionist. Caller ID: {{phone|unknown}}"""

SEED_VENUES = [
    {
        "slug": "gobest",
        "name": "GoBEST (triagem)",
        "sender_number": "+17027283109",
        "template": None,
        "packages": [],
        "templates": [
            ("gobest_inquiry", "GoBEST inquiry — {{inquiry_type}} — {{caller_name|no name}}",
             "", GOBEST_INQUIRY),
        ],
        # Cada rota do one-sheet vira uma regra. O destinatário está na regra,
        # então um template só atende todas.
        "rules": [
            ("Marketing / vendas", 10,
             [{"field": "inquiry_type", "op": "equals", "value": "Marketing or Sales"}],
             1, "email", "gobest_inquiry", 1, "marketing@gobestbiz.com"),
            ("Web / design gráfico", 20,
             [{"field": "inquiry_type", "op": "equals", "value": "Web or Graphic Design"}],
             1, "email", "gobest_inquiry", 1, "submit@gobestvip.com"),
            ("Social / reputação", 30,
             [{"field": "inquiry_type", "op": "equals", "value": "Social Media or Reputation"}],
             1, "email", "gobest_inquiry", 1, "social@gobestvip.com"),
            ("Solicitação de venue parceira", 40,
             [{"field": "inquiry_type", "op": "equals", "value": "Marketing Request from Powered Venue"}],
             1, "email", "gobest_inquiry", 1, "submit@gobestvip.com"),
            ("Pedidos BundlesAdvantage", 50,
             [{"field": "inquiry_type", "op": "equals", "value": "BundlesAdvantage Order"}],
             1, "email", "gobest_inquiry", 1, "orders@myordersvip.com"),
            ("Pedidos GearMeUp", 60,
             [{"field": "inquiry_type", "op": "equals", "value": "GearMeUp Order"}],
             1, "email", "gobest_inquiry", 1, "alyssa@gobestbiz.com"),
            ("Problema com pedido", 70,
             [{"field": "inquiry_type", "op": "equals", "value": "Order Problem or Complaint"}],
             1, "email", "gobest_inquiry", 1, "dawn@empowhereq.com"),
            ("Multi-unidade / contrato", 80,
             [{"field": "inquiry_type", "op": "equals", "value": "Multi-location or Contract Pricing"}],
             1, "email", "gobest_inquiry", 1, "adia@empowhereq.com"),
            ("Currículo / emprego", 90,
             [{"field": "inquiry_type", "op": "equals", "value": "Employment"}],
             1, "email", "gobest_inquiry", 1, "careers@gobestbiz.com"),
            # Fallback: qualquer coisa que não casou vai para a caixa geral.
            ("Geral (fallback)", 999, [], 1, "email", "gobest_inquiry", 1,
             "elevate@gobestbiz.com"),
        ],
    },
    {
        "slug": "emergency",
        "name": "Emergência (todas as venues)",
        "sender_number": "+17029970961",
        "template": None,
        "packages": [],
        "pinned": 1,
        # Ignora o modo do console: um alerta de emergência nunca deve ser
        # engolido porque alguém deixou o hub em Simulação.
        "always_live": 1,
        "templates": [
            ("emergency_dispatch", "", "+17755135260", EMERGENCY_DISPATCH),
            ("emergency_jason", "", "+13109307413", EMERGENCY_JASON),
        ],
        "rules": [
            # stop_after = 0 na primeira: o alerta vai para os dois plantões.
            ("Alerta — plantão", 10, [], 1, "sms", "emergency_dispatch", 0),
            ("Alerta — Jason", 20, [], 1, "sms", "emergency_jason", 1),
        ],
    },
    {
        "slug": "cats-meow",
        "name": "Cats Meow Karaoke",
        "sender_number": "+15046819283",
        "template": CATS_MEOW_TEMPLATE,
        # Os rótulos são os valores exatos que os filtros do Make usam, porque é
        # isso que a IA manda no campo package. Os links vêm da lista do CartVIP.
        "packages": [
            ("Cut the Karaoke Line", "", "https://app.cartvip.com/cats-nola/package/cut-the-karaoke-line-71/checkout"),
            ("Swingin' Cat's Package", "", "https://app.cartvip.com/cats-nola/package/swingin-cats-72/checkout"),
            ("Tom Cats Bachelor Party Package", "", "https://app.cartvip.com/cats-nola/package/tom-cats-bachelor-party-74/checkout"),
            ("Frisky Felines Bachelorette Party Package", "", "https://app.cartvip.com/cats-nola/package/frisky-felines-bachelorette-75/checkout"),
            ("Cool Cats Party Package (Sun - Thurs)", "", "https://app.cartvip.com/cats-nola/package/cool-cats-balcony-bar-76/checkout"),
            ("Wild Cats Weekend (Fri-Sat)", "", "https://app.cartvip.com/cats-nola/package/wild-cats-balcony-bar-77/checkout"),
            ("Top Cats Party Package (Sun - Thurs)", "", "https://app.cartvip.com/cats-nola/package/top-cats-balcony-bar-73/checkout"),
            ("Purrfect-Kitty Party Package (Fri - Sat)", "", "https://app.cartvip.com/cats-nola/package/purr-fect-kitty-balcony-bar-78/checkout"),
            ("Spicy Cats Soiree (Sun-Thurs)", "", "https://app.cartvip.com/cats-nola/package/spicy-cats-soiree-full-club-79/checkout"),
            ("Mind-Blowing Pussy Cat Party (Fri-Sat)", "", "https://app.cartvip.com/cats-nola/package/mind-blowing-pussy-cat-full-club-80/checkout"),
        ],
    },
    {
        "slug": "barely-legal-nola",
        "name": "Barely Legal New Orleans",
        "sender_number": "+15044746323",
        "template": BARELY_LEGAL_TEMPLATE,
        "packages": [
            ("VIP One Time Admission", "", "https://app.cartvip.com/barely-legal-new-orleans/package/vip-one-time-admission-66/checkout"),
            ("Entry, Dance & Drink SPECIAL", "", "https://app.cartvip.com/barely-legal-new-orleans/package/entry-drink-couch-dance-67/checkout"),
            ("Couples Package", "", "https://app.cartvip.com/barely-legal-new-orleans/package/couples-package-58/checkout"),
            ("Silver Party", "", "https://app.cartvip.com/barely-legal-new-orleans/package/silver-party-59/checkout"),
            ("Dance Party", "", "https://app.cartvip.com/barely-legal-new-orleans/package/dance-party-60/checkout"),
            ("Gold Party", "", "https://app.cartvip.com/barely-legal-new-orleans/package/gold-party-61/checkout"),
            ("Wild Party", "", "https://app.cartvip.com/barely-legal-new-orleans/package/wild-party-62/checkout"),
            ("Platinum Party", "", "https://app.cartvip.com/barely-legal-new-orleans/package/platinum-party-63/checkout"),
            ("Executive Party", "", "https://app.cartvip.com/barely-legal-new-orleans/package/the-executive-64/checkout"),
            ("Royal Party", "", "https://app.cartvip.com/barely-legal-new-orleans/package/the-royal-65/checkout"),
            ("Booth & Bar Reservation (up to 5 guests)", "$300", "https://app.cartvip.com/barely-legal-new-orleans/package/booth-bar-300-68/checkout"),
            ("Booth & Bar Reservation (up to 10 guests)", "$550", "https://app.cartvip.com/barely-legal-new-orleans/package/booth-bar-550-69/checkout"),
            ("Booth & Bar Reservation (up to 15 guests)", "$800", "https://app.cartvip.com/barely-legal-new-orleans/package/booth-bar-800-70/checkout"),
        ],
    },
    {
        "slug": "hustler-nola",
        "name": "Hustler Club New Orleans",
        "sender_number": "+15045141440",
        "template": HUSTLER_NOLA_TEMPLATE,
        "packages": [
            ("VIP One Time Admission", "", "https://app.cartvip.com/hustler-club-new-orleans/package/vip-one-time-admission-81/checkout"),
            ("Entry, Drink & Lap Dance SPECIAL", "", "https://app.cartvip.com/hustler-club-new-orleans/package/entry-drink-couch-dance-83/checkout"),
            ("Couples Package", "", "https://app.cartvip.com/hustler-club-new-orleans/package/couples-package-82/checkout"),
            # Existe na lista do CartVIP, mas não tinha rota no Make.
            ("Silver Party", "", "https://app.cartvip.com/hustler-club-new-orleans/package/silver-party-84/checkout"),
            ("The Wild Party", "", "https://app.cartvip.com/hustler-club-new-orleans/package/the-wild-party-87/checkout"),
            ("Gold Party", "", "https://app.cartvip.com/hustler-club-new-orleans/package/gold-party-85/checkout"),
            ("Platinum Party", "", "https://app.cartvip.com/hustler-club-new-orleans/package/platinum-party-86/checkout"),
            ("The Wildest Party", "", "https://app.cartvip.com/hustler-club-new-orleans/package/the-wildest-party-88/checkout"),
            ("The Executive Party", "", "https://app.cartvip.com/hustler-club-new-orleans/package/the-executive-party-89/checkout"),
            ("The Royal Party", "", "https://app.cartvip.com/hustler-club-new-orleans/package/the-royal-party-90/checkout"),
        ],
    },
    {
        "slug": "dejavu-kalamazoo",
        "name": "Deja Vu Showgirls Kalamazoo",
        "sender_number": "+12697754582",
        "template": KALAMAZOO_TEMPLATE,
        "packages": [
            ("18th Birthday", "", "https://vip-packages.com/products/deja-vu-showgirls-kalamazoo-18th-birthday"),
            ("Couples Package", "", "https://vip-packages.com/products/deja-vu-showgirls-kalamazoo-couples-package-1"),
            ("Group of 8", "", "https://vip-packages.com/products/deja-vu-showgirls-kalamazoo-groups-of-8-vip-package"),
            ("Group of 10", "", "https://vip-packages.com/products/deja-vu-showgirls-kalamazoo-groups-of-10-vip-package"),
        ],
    },
    {
        "slug": "dejavu-stockton",
        "name": "Deja Vu Showgirls Stockton",
        "sender_number": "+19163504781",
        "template": STOCKTON_TEMPLATE,
        "packages": [
            ("VIP One Time Admission", "", "https://vip-packages.com/products/deja-vu-showgirls-stockton-vip-one-time-admission"),
            ("Couples Package", "", "https://vip-packages.com/products/deja-vu-showgirls-stockton-couples-package"),
            # No Make esta rota apontava para o link do Couples Package.
            ("Silver VIP Party", "", "https://vip-packages.com/products/deja-vu-showgirls-stockton-silver-vip-party"),
            ("Gold VIP Party", "", "https://vip-packages.com/products/deja-vu-showgirls-stockton-gold-vip-party"),
            ("Platinum VIP Party", "", "https://vip-packages.com/products/deja-vu-showgirls-stockton-platinum-vip-party"),
            ("VIP Baller Package", "", "https://vip-packages.com/products/deja-vu-showgirls-stockton-vip-baller-package"),
        ],
    },
    {
        "slug": "dejavu-ypsilanti",
        "name": "Deja Vu Showgirls Ypsilanti",
        "sender_number": "+17348965038",
        "template": YPSILANTI_TEMPLATE,
        "packages": [
            ("VIP One Time Admission", "", "https://vip-packages.com/products/deja-vu-showgirls-ypsilanti-vip-one-time-admission"),
            ("Couples Package", "", "https://vip-packages.com/products/deja-vu-showgirls-ypsilanti-couples-package"),
            ("Silver VIP", "", "https://vip-packages.com/products/deja-vu-showgirls-ypsilanti-silver-vip-party"),
            ("Gold VIP", "", "https://vip-packages.com/products/deja-vu-showgirls-ypsilanti-gold-vip-party"),
            ("Platinum VIP", "", "https://vip-packages.com/products/deja-vu-showgirls-ypsilanti-platinum-vip-party"),
        ],
    },
    {
        "slug": "hustler-lv",
        "name": "Hustler Club Las Vegas",
        "sender_number": "+17029970961",
        "template": HUSTLER_TEMPLATE,
        "packages": [
            ("Free Ride and Entry Pass", "$0", "https://app.cartvip.com/vegashustlerclub/package/free-ride-and-entry-pass-32/checkout"),
            # Apelido: a IA costuma dizer o nome curto.
            ("Free Entry Pass", "$0", "https://app.cartvip.com/vegashustlerclub/package/free-ride-and-entry-pass-32/checkout"),
            ("$20 Special", "$20", "https://app.cartvip.com/vegashustlerclub/package/20-special-1/checkout"),
            ("Just the Two of Us", "$150", "https://app.cartvip.com/vegashustlerclub/package/just-the-two-of-us-30/checkout"),
            ("Couch with a View", "$250", "https://app.cartvip.com/vegashustlerclub/package/couch-with-a-view-25/checkout"),
            ("Blowout Fest", "$450", "https://app.cartvip.com/vegashustlerclub/package/blowout-fest-26/checkout"),
            ("What Happens In Vegas", "$800", "https://app.cartvip.com/vegashustlerclub/package/what-happens-in-vegas-28/checkout"),
            ("Guaranteed Over The Top Experience", "$1200", "https://app.cartvip.com/vegashustlerclub/package/over-the-top-29/checkout"),
        ],
    },
    {
        "slug": "winning-realty",
        "name": "Winning Realty (Lily)",
        "sender_number": "",
        "template": None,
        "packages": [],
        "templates": [
            ("call_handling", "Lily handled a call!", "", LILY_BASE),
            ("transfer_ok", "Someone reached out to you!", "{{agent_email}}", LILY_TRANSFER_OK),
            ("transfer_failed", "Lily tried to transfer a call to you, but it didn't go through!",
             "{{agent_email}}", LILY_TRANSFER_FAIL),
        ],
        "rules": [
            # stop_after = 1: chamada transferida notifica só o agente.
            # O log geral para o admin fica para as chamadas não transferidas.
            ("Transferência falhou", 10,
             [{"field": "was_transfer_made", "op": "truthy", "value": ""},
              {"field": "was_transfer_sucessful", "op": "falsy", "value": ""}],
             1, "email", "transfer_failed", 1),
            ("Transferência concluída", 20,
             [{"field": "was_transfer_made", "op": "truthy", "value": ""},
              {"field": "was_transfer_sucessful", "op": "truthy", "value": ""}],
             1, "email", "transfer_ok", 1),
            ("Chamada atendida", 100, [], 1, "email", "call_handling", 1),
        ],
    },
    {
        "slug": "kings",
        "name": "Kings of Hustler",
        "sender_number": "+17025471904",
        "template": KINGS_TEMPLATE,
        "packages": [
            ("Free Ride and Free Entry", "$0", "https://app.cartvip.com/kingsofhustler/package/free-ride-and-free-entry-40/checkout"),
            ("Free Entry Pass", "$0", "https://app.cartvip.com/kingsofhustler/package/free-ride-and-free-entry-40/checkout"),
            ("Free Entry & Ride", "$0", "https://app.cartvip.com/kingsofhustler/package/free-ride-and-free-entry-40/checkout"),
            ("Showstopper", "$50", "https://app.cartvip.com/kingsofhustler/package/showstopper-41/checkout"),
            ("Bad Mom's Club", "$300", "https://app.cartvip.com/kingsofhustler/package/bad-moms-club-33/checkout"),
            ("Champagne with a King", "$400", "https://app.cartvip.com/kingsofhustler/package/champagne-with-a-king-13/checkout"),
            ("Rosè All Day", "$700", "https://app.cartvip.com/kingsofhustler/package/rose-all-day-35/checkout"),
            ("Screaming Orgasm", "$900", "https://app.cartvip.com/kingsofhustler/package/screaming-orgasm-36/checkout"),
            ("One Last Hoerahh", "$1400", "https://app.cartvip.com/kingsofhustler/package/one-last-hoerahh-37/checkout"),
            ("Bride and Boujee", "$1800", "https://app.cartvip.com/kingsofhustler/package/bride-and-boujee-38/checkout"),
            ("One King Forever!", "$2000", "https://app.cartvip.com/kingsofhustler/package/one-king-forever-39/checkout"),
        ],
    },
]


# ---------- utilidades ----------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_token() -> str:
    return secrets.token_urlsafe(24)


def norm_key(text: str) -> str:
    """
    Normaliza o nome do pacote pra casar apesar de caixa, acento e pontuação.
    "$20 SPECIAL" e "$20 Special" -> "20 special"
    "Rosè All Day"               -> "rose all day"
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def to_e164(raw: str) -> str:
    """Normaliza telefone US pra E.164. Devolve vazio se não der."""
    if not raw:
        return ""
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10:
        return "+1" + digits
    if len(digits) == 11 and digits.startswith("1"):
        return "+" + digits
    if str(raw).strip().startswith("+") and 8 <= len(digits) <= 15:
        return "+" + digits
    return ""


def pretty_json(raw: str) -> str:
    try:
        return json.dumps(json.loads(raw), indent=2, ensure_ascii=False)
    except Exception:
        return raw or ""


def sms_segments(body: str) -> tuple[int, int, str]:
    """(caracteres, segmentos, encoding). GSM-7 vs UCS-2 muda o tamanho do segmento."""
    if not body:
        return 0, 0, "GSM-7"
    gsm = set(
        "@£$¥èéùìòÇØøÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
        "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
        "\n\r"
    )
    extended = set("^{}\\[~]|€")
    is_gsm = all(c in gsm or c in extended for c in body)
    if is_gsm:
        length = sum(2 if c in extended else 1 for c in body)
        single, multi = 160, 153
        enc = "GSM-7"
    else:
        length = len(body)
        single, multi = 70, 67
        enc = "UCS-2"
    segments = 1 if length <= single else -(-length // multi)
    return length, segments, enc


# ---------- conexão ----------

def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        g.db = conn
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# Colunas adicionadas depois do primeiro deploy. CREATE TABLE IF NOT EXISTS não
# altera tabela existente, então bancos antigos precisam de ALTER TABLE.
MIGRATIONS = {
    "templates": [
        ("subject",   "TEXT NOT NULL DEFAULT ''"),
        ("recipient", "TEXT NOT NULL DEFAULT ''"),
    ],
    "deliveries": [
        ("channel",   "TEXT NOT NULL DEFAULT 'sms'"),
        ("rule_name", "TEXT"),
        ("subject",   "TEXT"),
    ],
    "events": [
        ("matched_link", "TEXT"),
        ("preview_body", "TEXT"),
        ("preview_from", "TEXT"),
    ],
    "rules": [
        ("stop_after", "INTEGER NOT NULL DEFAULT 1"),
        ("recipient",  "TEXT NOT NULL DEFAULT ''"),
    ],
    "venues": [
        ("pinned",      "INTEGER NOT NULL DEFAULT 0"),
        ("always_live", "INTEGER NOT NULL DEFAULT 0"),
    ],
}


def migrate(conn):
    """Adiciona colunas que faltam. Seguro de rodar em todo boot."""
    for table, columns in MIGRATIONS.items():
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if not exists:
            continue
        present = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, spec in columns:
            if name not in present:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {spec}")
    conn.commit()


def init_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migrate(conn)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))
    row = conn.execute("SELECT value FROM settings WHERE key = 'status_callback_token'").fetchone()
    if not row or not row["value"]:
        conn.execute("UPDATE settings SET value = ? WHERE key = 'status_callback_token'", (new_token(),))

    for slug in RETIRED_SLUGS:
        row = conn.execute("SELECT id FROM venues WHERE slug = ?", (slug,)).fetchone()
        if row:
            used = conn.execute(
                "SELECT 1 FROM events WHERE venue_id = ? LIMIT 1", (row["id"],)
            ).fetchone()
            if not used:
                conn.execute("DELETE FROM venues WHERE id = ?", (row["id"],))

    for slug, name, number in PENDING_VENUES:
        exists = conn.execute("SELECT 1 FROM venues WHERE slug = ?", (slug,)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO venues (slug, name, token, sender_number, active, created_at) "
                "VALUES (?, ?, ?, ?, 0, ?)",
                (slug, name, new_token(), number, now_iso()),
            )

    for v in SEED_VENUES:
        row = conn.execute("SELECT id FROM venues WHERE slug = ?", (v["slug"],)).fetchone()
        if row:
            venue_id = row["id"]
        else:
            cur = conn.execute(
                "INSERT INTO venues (slug, name, token, sender_number, active, "
                "pinned, always_live, created_at) VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
                (v["slug"], v["name"], new_token(), v["sender_number"],
                 v.get("pinned", 0), v.get("always_live", 0), now_iso()),
            )
            venue_id = cur.lastrowid
        if v.get("template"):
            conn.execute(
                "INSERT OR IGNORE INTO templates (venue_id, name, subject, body) VALUES (?, ?, '', ?)",
                (venue_id, "booking_link", v["template"]),
            )
            has_rule = conn.execute(
                "SELECT 1 FROM rules WHERE venue_id = ? AND name = ?",
                (venue_id, "Link do pacote"),
            ).fetchone()
            if not has_rule:
                conn.execute(
                    "INSERT INTO rules (venue_id, name, priority, conditions, match_all, "
                    "channel, template_name, stop_after, active) "
                    "VALUES (?, ?, 100, '[]', 1, 'sms', ?, 1, 1)",
                    (venue_id, "Link do pacote", "booking_link"),
                )
        for name, subject, recipient, body in v.get("templates", []):
            conn.execute(
                "INSERT OR IGNORE INTO templates (venue_id, name, subject, recipient, body) "
                "VALUES (?, ?, ?, ?, ?)",
                (venue_id, name, subject, recipient, body),
            )
        for rule in v.get("rules", []):
            name, prio, conds, match_all, channel, tpl, stop = rule[:7]
            recipient = rule[7] if len(rule) > 7 else ""
            exists = conn.execute(
                "SELECT 1 FROM rules WHERE venue_id = ? AND name = ?", (venue_id, name)
            ).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO rules (venue_id, name, priority, conditions, match_all, "
                    "channel, template_name, stop_after, active, recipient) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)",
                    (venue_id, name, prio, json.dumps(conds), match_all, channel, tpl,
                     stop, recipient),
                )
        for label, price, link in v["packages"]:
            conn.execute(
                "INSERT OR IGNORE INTO packages (venue_id, label, norm_key, price, link) "
                "VALUES (?, ?, ?, ?, ?)",
                (venue_id, label, norm_key(label), price, link),
            )
    conn.commit()
    conn.close()


# ---------- settings ----------

def get_setting(key: str, default: str = "") -> str:
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str):
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


# ---------- venues ----------

def list_venues():
    return get_db().execute(
        "SELECT * FROM venues ORDER BY pinned DESC, name"
    ).fetchall()


def get_venue_by_slug(slug: str):
    return get_db().execute("SELECT * FROM venues WHERE slug = ?", (slug,)).fetchone()


def get_venue(venue_id: int):
    return get_db().execute("SELECT * FROM venues WHERE id = ?", (venue_id,)).fetchone()


def update_venue(venue_id: int, name: str, sender_number: str, active: int):
    db = get_db()
    db.execute(
        "UPDATE venues SET name = ?, sender_number = ?, active = ? WHERE id = ?",
        (name, sender_number, active, venue_id),
    )
    db.commit()


def set_always_live(venue_id: int, value: int):
    db = get_db()
    db.execute("UPDATE venues SET always_live = ? WHERE id = ?", (int(value), venue_id))
    db.commit()


def rotate_venue_token(venue_id: int):
    db = get_db()
    db.execute("UPDATE venues SET token = ? WHERE id = ?", (new_token(), venue_id))
    db.commit()


# ---------- packages ----------

def list_packages(venue_id: int):
    return get_db().execute(
        "SELECT * FROM packages WHERE venue_id = ? ORDER BY id", (venue_id,)
    ).fetchall()


def find_package(venue_id: int, raw_label: str):
    key = norm_key(raw_label)
    if not key:
        return None
    return get_db().execute(
        "SELECT * FROM packages WHERE venue_id = ? AND norm_key = ?", (venue_id, key)
    ).fetchone()


def upsert_package(venue_id: int, label: str, price: str, link: str, package_id=None):
    db = get_db()
    if package_id:
        db.execute(
            "UPDATE packages SET label = ?, norm_key = ?, price = ?, link = ? WHERE id = ?",
            (label, norm_key(label), price, link, package_id),
        )
    else:
        db.execute(
            "INSERT INTO packages (venue_id, label, norm_key, price, link) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(venue_id, norm_key) DO UPDATE SET "
            "label = excluded.label, price = excluded.price, link = excluded.link",
            (venue_id, label, norm_key(label), price, link),
        )
    db.commit()


def delete_package(package_id: int):
    db = get_db()
    db.execute("DELETE FROM packages WHERE id = ?", (package_id,))
    db.commit()


# ---------- templates ----------

def get_template(venue_id: int, name: str = "booking_link"):
    return get_db().execute(
        "SELECT * FROM templates WHERE venue_id = ? AND name = ?", (venue_id, name)
    ).fetchone()


def save_template(venue_id: int, name: str, body: str, subject: str = "", recipient: str = ""):
    db = get_db()
    db.execute(
        "INSERT INTO templates (venue_id, name, subject, recipient, body) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(venue_id, name) DO UPDATE SET "
        "body = excluded.body, subject = excluded.subject, recipient = excluded.recipient",
        (venue_id, name, subject, recipient, body),
    )
    db.commit()


def list_templates(venue_id: int):
    return get_db().execute(
        "SELECT * FROM templates WHERE venue_id = ? ORDER BY name", (venue_id,)
    ).fetchall()


# ---------- rules ----------

def list_rules(venue_id: int):
    return get_db().execute(
        "SELECT * FROM rules WHERE venue_id = ? ORDER BY priority, id", (venue_id,)
    ).fetchall()


def active_rules(venue_id: int):
    return get_db().execute(
        "SELECT * FROM rules WHERE venue_id = ? AND active = 1 ORDER BY priority, id",
        (venue_id,),
    ).fetchall()


def get_rule(rule_id: int):
    return get_db().execute("SELECT * FROM rules WHERE id = ?", (rule_id,)).fetchone()


def save_rule(venue_id, name, priority, conditions, match_all, channel,
              template_name, stop_after, active, rule_id=None, recipient=""):
    db = get_db()
    args = (name, int(priority), conditions, int(match_all), channel,
            template_name, int(stop_after), int(active), recipient or "")
    if rule_id:
        db.execute(
            "UPDATE rules SET name=?, priority=?, conditions=?, match_all=?, channel=?, "
            "template_name=?, stop_after=?, active=?, recipient=? WHERE id=?",
            (*args, rule_id))
    else:
        db.execute(
            "INSERT INTO rules (name, priority, conditions, match_all, channel, "
            "template_name, stop_after, active, recipient, venue_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*args, venue_id))
    db.commit()


def delete_rule(rule_id: int):
    db = get_db()
    db.execute("DELETE FROM rules WHERE id = ?", (rule_id,))
    db.commit()


# ---------- events ----------

def find_duplicate(venue_id, call_session_id: str):
    if not call_session_id:
        return None
    return get_db().execute(
        "SELECT id FROM events WHERE venue_id IS ? AND call_session_id = ? "
        "AND status != 'duplicate' ORDER BY id LIMIT 1",
        (venue_id, call_session_id),
    ).fetchone()


EVENT_COLS = (
    "venue_id", "slug", "received_at", "remote_ip", "content_type", "headers",
    "body", "call_session_id", "customer_phone", "agent_did", "package",
    "first_name", "matched_link", "preview_body", "preview_from", "status", "note",
)


def insert_event(**kw) -> int:
    db = get_db()
    cur = db.execute(
        f"INSERT INTO events ({', '.join(EVENT_COLS)}) "
        f"VALUES ({', '.join('?' * len(EVENT_COLS))})",
        [kw.get(c) for c in EVENT_COLS],
    )
    db.commit()
    return cur.lastrowid


def update_event_note(event_id: int, note: str):
    db = get_db()
    db.execute("UPDATE events SET note = ? WHERE id = ?", (note, event_id))
    db.commit()


def list_events(limit=100, venue_id=None, status=None):
    sql = ["SELECT e.*, v.name AS venue_name, "
           "(SELECT status FROM deliveries WHERE event_id = e.id ORDER BY id DESC LIMIT 1) "
           "AS delivery_status "
           "FROM events e LEFT JOIN venues v ON v.id = e.venue_id WHERE 1=1"]
    args = []
    if venue_id:
        sql.append("AND e.venue_id = ?")
        args.append(venue_id)
    if status:
        sql.append("AND e.status = ?")
        args.append(status)
    sql.append("ORDER BY e.id DESC LIMIT ?")
    args.append(limit)
    return get_db().execute(" ".join(sql), args).fetchall()


def get_event(event_id: int):
    return get_db().execute(
        "SELECT e.*, v.name AS venue_name FROM events e "
        "LEFT JOIN venues v ON v.id = e.venue_id WHERE e.id = ?",
        (event_id,),
    ).fetchone()


def event_counts():
    rows = get_db().execute(
        "SELECT status, COUNT(*) AS n FROM events GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


# ---------- deliveries ----------

def create_delivery(event_id, venue_id, to_number, from_number, body, status="queued",
                    channel="sms", subject="", rule_name=""):
    db = get_db()
    ts = now_iso()
    provider = "twilio" if channel == "sms" else "smtp"
    cur = db.execute(
        "INSERT INTO deliveries (event_id, venue_id, created_at, updated_at, channel, "
        "rule_name, subject, to_number, from_number, body, provider, status, attempts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)",
        (event_id, venue_id, ts, ts, channel, rule_name, subject, to_number,
         from_number, body, provider, status),
    )
    db.commit()
    return cur.lastrowid


def update_delivery(delivery_id, **kw):
    if not kw:
        return
    kw["updated_at"] = now_iso()
    sets = ", ".join(f"{k} = ?" for k in kw)
    db = get_db()
    db.execute(f"UPDATE deliveries SET {sets} WHERE id = ?", [*kw.values(), delivery_id])
    db.commit()


def claim_delivery(delivery_id) -> bool:
    """Marca como 'sending' só se ainda estiver pendente. Evita dois workers pegarem o mesmo."""
    db = get_db()
    cur = db.execute(
        "UPDATE deliveries SET status = 'sending', attempts = attempts + 1, updated_at = ? "
        "WHERE id = ? AND status IN ('queued', 'retry')",
        (now_iso(), delivery_id),
    )
    db.commit()
    return cur.rowcount == 1


def deliveries_for_event(event_id):
    return get_db().execute(
        "SELECT * FROM deliveries WHERE event_id = ? ORDER BY id DESC", (event_id,)
    ).fetchall()


def latest_delivery(event_id):
    return get_db().execute(
        "SELECT * FROM deliveries WHERE event_id = ? ORDER BY id DESC LIMIT 1", (event_id,)
    ).fetchone()


def delivery_by_sid(sid: str):
    return get_db().execute(
        "SELECT * FROM deliveries WHERE provider_sid = ?", (sid,)
    ).fetchone()


def get_delivery(delivery_id):
    return get_db().execute(
        "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
    ).fetchone()


def pending_retries(limit=20, channel=None):
    if channel:
        return get_db().execute(
            "SELECT * FROM deliveries WHERE status = 'retry' AND attempts < 3 "
            "AND channel = ? ORDER BY id LIMIT ?", (channel, limit)
        ).fetchall()
    return get_db().execute(
        "SELECT * FROM deliveries WHERE status = 'retry' AND attempts < 3 "
        "ORDER BY id LIMIT ?", (limit,)
    ).fetchall()


def list_deliveries(limit=100):
    return get_db().execute(
        "SELECT d.*, v.name AS venue_name FROM deliveries d "
        "LEFT JOIN venues v ON v.id = d.venue_id ORDER BY d.id DESC LIMIT ?", (limit,)
    ).fetchall()


def allowlist() -> set:
    raw = get_setting("test_allowlist", "")
    return {to_e164(p) for p in re.split(r"[\s,;]+", raw) if to_e164(p)}
