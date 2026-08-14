# integrator.io

Hub de automação de webhook do Winning Vocal.

Substitui os cenários do Make.com. Cada agente de voz tem sua própria URL de webhook;
o hub identifica a venue pela URL, resolve o link do pacote e monta o SMS.

**Fase atual: 4.** Recebe, normaliza, roda regras editáveis, monta a mensagem e
envia por **SMS (Twilio)** ou **e-mail (SMTP)**, com registro de entrega, retry e
callback de status. O envio só acontece no modo escolhido no topo do console.

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
   | `PUBLIC_BASE_URL` | a URL pública do app, **com `https://`** (preencha depois do primeiro deploy) |
   | `TWILIO_ACCOUNT_SID` | o Account SID da conta Twilio |
   | `TWILIO_AUTH_TOKEN` | o Auth Token da mesma conta |
   | `SMTP_USER` | a conta que envia os e-mails (ex. `app@winningrealty.com`) |
   | `SMTP_PASSWORD` | app password de 16 caracteres, **sem espaços** |
   | `SMTP_HOST` | opcional, padrão `smtp.gmail.com` |
   | `SMTP_PORT` | opcional, padrão `587` |
   | `SMTP_FROM` | opcional; se omitido usa `SMTP_USER` |
   | `HOOK_TOKEN_SALT` | torna os tokens dos webhooks estáveis — veja abaixo |

5. *Settings → Networking → Generate Domain*.
6. Confira `https://SEU-APP.up.railway.app/health` → deve responder
   `{"ok": true, "mode": "dry_run"}`.

Deploys seguintes: só `git push`.

## 2. Pegar as URLs dos webhooks

Entre no console e vá em **Webhooks**. A tela lista uma URL por agente, com botão
de copiar em cada uma e um "copiar tudo" no topo. Formato:

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

## Regras

Cada venue tem uma lista de regras ordenada por prioridade (menor roda primeiro).
Uma regra sem condição nenhuma casa com tudo — é o fallback, e deve ficar com a
maior prioridade numérica.

**Parar aqui** (ligado por padrão): a regra que casa encerra a cadeia e a chamada
gera uma mensagem só. Desligado, a avaliação continua nas regras seguintes e a
mesma chamada pode gerar duas ou mais mensagens — que é como o Make encadeia,
onde um módulo dispara e o fluxo segue para o próximo filtro.

Condições comparam um campo do payload com um valor. Operadores: igual, diferente,
contém, não contém, vazio, preenchido, é sim/verdadeiro, é não/falso.

Os dois últimos existem porque agentes escrevem booleanos de formas variadas —
`Yes`, `true`, `1`, `No`, `unsuccessful`, vazio. Em vez de você adivinhar a grafia,
use **é sim** ou **é não**.

A aba Regras mostra, ao final, **os campos vistos nos payloads recentes** daquela
venue, com um exemplo de valor. É de lá que saem os nomes para usar nas condições
e nos templates.

### Como a Winning Realty está configurada

| Prioridade | Regra | Vai para | Parar aqui |
|---|---|---|---|
| 10 | Transferência falhou | `{{agent_email}}` | sim |
| 20 | Transferência concluída | `{{agent_email}}` | sim |
| 100 | Chamada atendida (fallback) | destinatário padrão de Ajustes | sim |

Chamada transferida notifica **só o agente**. Chamada sem transferência gera o
log geral para o admin.

O endereço do admin é o **destinatário padrão** em Ajustes — mude ali, não no
template.

### Emergência

A venue **Emergência (todas as venues)** é universal: um webhook só, usado por
qualquer agente que detecte um incidente. O payload traz `club_name`, `report` e
`phone`, e o alerta vai para dois plantões fixos, do número do Hustler LV.

Duas particularidades:

- Os destinatários são **fixos no template**, não vêm do payload. Em SMS, um
  campo Destinatário preenchido tem precedência sobre o telefone do payload.
- A venue é **sempre ao vivo**: ignora o modo do console. Um alerta de emergência
  nunca deve ser engolido porque alguém deixou o hub em Simulação ou Teste.

A primeira regra tem **parar aqui** desligado, para o alerta chegar aos dois.

### Quando a IA usa outro nome

A IA nem sempre diz o nome do pacote como está no CartVIP — ela pode dizer
"FREE ENTRY PASS" para o pacote cadastrado como "Free Ride and Entry Pass".
Nesse caso a chamada fica como **Pacote sem link** e nada é enviado.

O erro no console mostra o nome exato que a IA mandou e sugere os pacotes de
nome parecido. Para resolver, cadastre o nome que a IA usa como um pacote
adicional apontando para o mesmo link — é um apelido, e os dois passam a
funcionar. Depois clique **Reenviar** na chamada, e o cliente recebe.

O hub nunca escolhe um pacote parecido sozinho. "Over The Top" casaria tanto com
o pacote de \$1200 quanto com o de \$150 — mandar o checkout errado é pior que
não mandar link nenhum.

## Canais

- **SMS** — Twilio. Vai para o telefone do cliente, do número da venue.
- **E-mail** — SMTP. O destinatário sai do campo **Destinatário** do template,
  que pode usar um campo do payload (ex. `{{agent_email}}`) ou um endereço fixo.
  Se vier vazio, cai no **destinatário padrão** de Ajustes; sem esse padrão, a
  mensagem não é enviada e fica registrada como "Sem destinatário".

## Segurança do payload

O hub **não grava campos de aparência sensível**. Qualquer chave contendo
`password`, `senha`, `secret`, `token`, `api_key` ou `credential` é substituída
por um marcador antes do payload ir para o banco.

Isso importa porque o payload do agente da Winning Realty declara campos como
`godaddy_password`, `instagram_password` e `username_and_password`. Se algum dia
forem preenchidos, não ficam registrados em texto puro.

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
   ├─ roda as regras         → primeira que casar define canal e template
   ├─ procura o pacote       → só se o template usar {{link}}
   │                           chave normalizada: "$20 SPECIAL" == "$20 Special"
   ├─ renderiza o template   → {{campo}} ou {{campo|padrão}}
   └─ envia ou registra      → conforme o modo
```

O achatamento do payload cobre três formatos: campos no nível raiz, aninhados em
`custom_parameters`, e aninhados em `post_call_analysis` no formato
`{"type": "string", "value": "..."}` — este último é o do agente da Winning Realty.

Status possíveis: `preview_ok`, `duplicate`, `no_link`, `no_phone`,
`no_template`, `no_sender`, `unknown_venue`, `bad_token`, `venue_off`.

## Painel de automações

A tela inicial lista um cartão por agente: quantas regras estão ativas, quais
canais usa, quantas chamadas nas últimas 24h, quantas com problema, e quando foi
a última. Dali dá para **pausar ou ativar a venue inteira**, ou clicar numa regra
para pausá-la individualmente.

Venue pausada responde `403` ao webhook e registra o evento, mas não envia nada.
Serve para desligar uma automação sem mexer no agente do Agni.

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

**Cuidado com `PUBLIC_BASE_URL`:** ela tem que incluir o esquema. Sem `https://`,
a Twilio recusa o StatusCallback com *"is not a valid URL"* e a mensagem **nem é
criada** — a entrega falha inteira, não só o rastreamento. O hub agora adiciona
`https://` sozinho se você esquecer, mas confira o valor na aba Ajustes.

Para o status virar **Entregue** em vez de parar em **Saiu da Twilio**, o hub
precisa do callback. Defina `PUBLIC_BASE_URL` e o próprio hub manda a URL de
callback junto de cada mensagem — não precisa configurar nada no console da
Twilio. A URL aparece na aba Ajustes.

## Bancada de testes

A aba **Testar** dispara uma mensagem sem precisar de ligação. Escolha a venue,
o pacote e o nome; o hub monta a mensagem pelo mesmo caminho que uma chamada real
usaria e mostra a prévia com contagem de caracteres e segmentos.

A bancada **só envia para números da allowlist**, em qualquer modo — inclusive em
Simulação. É por isso que ela é segura de usar sem virar a chave para Ao vivo.

## Primeira vez enviando

1. Ajustes → confira que as credenciais da Twilio aparecem como presentes.
2. Ajustes → ponha **o seu celular** na allowlist.
3. Testar → escolha venue e pacote, clique **Enviar teste**.
4. O SMS chega no seu número. Confira o texto, o remetente e o link.
5. Repita para a outra venue e para alguns pacotes diferentes.
6. Só então troque para **Ao vivo** e desligue o cenário do Make.

## Tokens estáveis

Sem `HOOK_TOKEN_SALT`, os tokens são gerados aleatoriamente quando cada venue é
criada. Se o banco for recriado — volume desmontado, `DATA_DIR` errado, volume
apagado — os tokens mudam e **todas as URLs já coladas no Agni passam a responder
401**, sem ninguém ter mexido em nada.

Com o salt definido, o token de cada venue é derivado de `HMAC(salt, slug)`:
sempre o mesmo, sobrevive a qualquer perda de banco. Defina a variável, faça o
deploy, e **recopie as URLs uma última vez** — a partir daí elas não mudam mais.

Nunca troque o salt depois: trocar equivale a rotacionar todos os tokens de uma vez.

## Diagnóstico

`GET /health` responde sem autenticação:

```json
{
  "ok": true,
  "mode": "live",
  "db_path": "/data/hub.sqlite3",
  "persistence": "volume",
  "stable_tokens": true,
  "events": 128,
  "booted_at": "2026-08-14T19:55:09+00:00"
}
```

- `persistence: "ephemeral"` → o banco será perdido no próximo restart. Monte o volume.
- `stable_tokens: false` → os tokens mudam se o banco for recriado.
- `events` caindo para zero, ou `booted_at` mudando sozinho, indica restart com
  perda de dados.

## Migrar uma venue do Make

Exporte o cenário: abra no Make, menu **⋯** na barra inferior, **Export Blueprint**.
Depois:

```bash
python3 tools/from_make.py blueprint.json --slug dejavu-stockton --name "Deja Vu Stockton"
```

Ele imprime o bloco pronto para colar em `SEED_VENUES` no `db.py`, e audita o
cenário no stderr antes: rotas apontando para o mesmo link, rodapés que
divergiram entre as rotas, remetentes diferentes na mesma venue. São os erros que
se acumulam quando cada rota é um módulo editado à mão — vale ler o aviso antes
de colar.

## Migrações

`init_db()` roda em todo boot: cria o que falta e adiciona colunas novas em
tabelas que já existem. É idempotente — rodar várias vezes não causa problema.

Isso importa porque o volume do Railway preserva o banco entre deploys, e
`CREATE TABLE IF NOT EXISTS` **não** altera uma tabela existente. Ao adicionar
uma coluna ao `SCHEMA`, adicione-a também ao dicionário `MIGRATIONS` no `db.py`,
senão o app sobe em banco novo e quebra em banco antigo.

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
