# integrator.io

Hub de automação de webhook do Winning Vocal.

Substitui os cenários do Make.com. Cada agente de voz tem sua própria URL de webhook;
o hub identifica a venue pela URL, resolve o link do pacote e monta o SMS.

**Fase atual: 1 + 2.** Recebe, normaliza, resolve e mostra o preview.
**Nada é enviado ainda** — a integração com a Twilio entra na Fase 3.

---

## 1. Subir no Railway

1. Crie o repositório e faça o push destes arquivos.
2. No Railway: **New Project → Deploy from GitHub repo** e escolha o repo.
3. **Adicione um volume** antes do primeiro deploy dar certo:
   *Settings → Volumes → New Volume*, mount path **`/data`**.
   Sem isso o SQLite é apagado a cada deploy.
4. Em *Variables*, defina:

   | Variável | Valor |
   |---|---|
   | `ADMIN_PASSWORD` | a senha do console |
   | `SECRET_KEY` | `python -c "import secrets;print(secrets.token_urlsafe(32))"` |
   | `DATA_DIR` | `/data` |
   | `PUBLIC_BASE_URL` | a URL pública do app (preencha depois do primeiro deploy) |

5. *Settings → Networking → Generate Domain*.
6. Confira `https://SEU-APP.up.railway.app/health` → deve responder
   `{"ok": true, "mode": "dry_run"}`.

Deploys seguintes: só `git push`.

## 2. Pegar as URLs dos webhooks

Entre no console, vá em **Venues**. Cada uma mostra uma URL como:

```
https://SEU-APP.up.railway.app/hook/hustler-lv/k/TOKEN
```

O token está na própria URL porque o Agni normalmente só deixa configurar a URL,
sem headers. Se der pra mandar header, `X-Integrator-Token: TOKEN` também funciona.

## 3. Apontar os agentes

Nesta fase, **não desligue o Make**. Rode os dois em paralelo: o Make continua
enviando os SMS de verdade e o hub só observa. Se o agente aceitar só um webhook,
comece pelo Kings, que tem o volume menor.

Uma ligação de teste deve aparecer em **Chamadas** em segundos.

## 4. O que conferir antes da Fase 3

- O payload cru bate com o esperado (a página de detalhe mostra o JSON exato).
- Todos os pacotes que os agentes mandam existem na tabela — qualquer
  **Pacote sem link** é um pacote faltando ou com nome diferente.
- Os textos dos SMS estão certos, incluindo assinatura, endereço e telefone
  de cada venue.

---

## Como o roteamento funciona

```
POST /hook/<slug>/k/<token>
   │
   ├─ slug não existe        → 404, registrado como "Endpoint desconhecido"
   ├─ token errado           → 401, registrado como "Token recusado"
   ├─ venue desativada       → 403
   │
   ├─ normaliza o payload    → telefone em E.164, first_name "Unknown" vira vazio,
   │                           lê tanto o nível raiz quanto custom_parameters
   ├─ call_session_id repetido → "Duplicado", não processa de novo
   ├─ procura o pacote       → chave normalizada: "$20 SPECIAL" == "$20 Special"
   ├─ renderiza o template   → {{first_name|there}}, {{link}}, {{package}}
   └─ registra o preview     → "Pronto pra enviar"
```

Status possíveis: `preview_ok`, `duplicate`, `no_link`, `no_phone`,
`no_template`, `no_sender`, `unknown_venue`, `bad_token`, `venue_off`.

## Modos

O interruptor no topo do console controla o comportamento de envio.

- **Simulação** — nada sai. É o modo desta fase.
- **Teste** — envia só para números da allowlist. (Fase 3)
- **Ao vivo** — envia para clientes reais. (Fase 4)

## Arquivos

| Arquivo | Papel |
|---|---|
| `app.py` | rotas: receptor, login, console |
| `engine.py` | normalização, lookup de pacote, renderização do template |
| `db.py` | schema, seed e todo acesso ao banco |
| `templates/` | telas do console (Jinja — só aqui) |

Os templates de SMS **não** usam Jinja: são editáveis pela interface, e Jinja ali
seria execução de código pelo navegador. A substituição é regex simples de `{{var}}`.

## Rodar local

```bash
pip install -r requirements.txt
cp .env.example .env      # preencha ADMIN_PASSWORD e SECRET_KEY
DATA_DIR=./data python app.py
```

Console em http://127.0.0.1:5080
