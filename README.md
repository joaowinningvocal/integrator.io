# integrator.io

Hub de automação de webhook do Winning Vocal.

Substitui os cenários do Make.com. Cada agente de voz tem sua própria URL de webhook;
o hub identifica a venue pela URL, resolve o link do pacote e monta o SMS.

**Fase atual: 3.** Recebe, normaliza, resolve, monta o SMS e envia pela Twilio,
com registro de entrega, retry e callback de status.
O envio só acontece no modo que você escolher no topo do console.

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
   | `TWILIO_ACCOUNT_SID` | o Account SID da conta Twilio |
   | `TWILIO_AUTH_TOKEN` | o Auth Token da mesma conta |

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

- **Simulação** — nada sai. O hub registra o que enviaria.
- **Teste** — envia só para os números da allowlist (aba Ajustes).
  Qualquer outro destino vira `blocked_test` e fica registrado.
- **Ao vivo** — envia para clientes reais.

## Entregas

Cada envio vira uma linha na tabela `deliveries`, visível na página da chamada
e em Ajustes. Estados: `queued` → `sent` → `delivered`, ou `failed` /
`undelivered` quando dá errado.

O hub separa erro **permanente** de **transitório**. STOP do destinatário,
número fixo, número inválido, filtro da operadora — não adianta repetir, marca
`failed` direto. Timeout de rede, 429 e 5xx da Twilio viram `retry`, e um worker
tenta de novo a cada minuto, até 3 tentativas.

Para o status virar **Entregue** em vez de parar em **Saiu da Twilio**, o hub
precisa do callback. Defina `PUBLIC_BASE_URL` e o próprio hub manda a URL de
callback junto de cada mensagem — não precisa configurar nada no console da
Twilio. A URL aparece na aba Ajustes.

## Primeira vez enviando

1. Ajustes → confira que as credenciais da Twilio aparecem como presentes.
2. Ajustes → ponha **o seu celular** na allowlist.
3. Topo → modo **Teste**.
4. Faça uma ligação de teste, ou abra uma chamada antiga e clique **Enviar agora**.
5. O SMS chega no seu número. Confira o texto, o remetente e o link.
6. Só então troque para **Ao vivo** e desligue o cenário do Make.

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
