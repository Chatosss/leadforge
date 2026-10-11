# LeadForge

Ferramenta de prospecção B2B que construí para vender presença digital a
clínicas e pequenas empresas. Ela encontra os negócios no Google Maps, avalia
quem vale a pena abordar, monta um diagnóstico do site e uma proposta em PDF,
faz o primeiro contato por WhatsApp, Instagram e e-mail e acompanha as
conversas até a reunião.

*English version below.*

## O que ela faz

```
coleta (Google Maps) → qualificação → diagnóstico do site → proposta em PDF
        → primeiro contato (WhatsApp / Instagram / e-mail) → follow-up → CRM
```

- **Funil em kanban** por empresa atendida, com esteira de mensagens por etapa
  e follow-up automático.
- **WhatsApp multi-conta** (whatsapp-web.js): envio em lote com intervalo
  sorteado, limite diário por número e chat ao vivo com foto, áudio e documento.
- **Instagram**: prospecção por DM com rodízio de contas, Direct de cada conta
  dentro do painel e aquecimento das contas no Reels.
- **E-mail** pelo Gmail, com controle de cota por conta.
- **IA** para personalizar a mensagem de cada lead e para o diagnóstico, com
  fallback para o texto padrão quando o modelo falha.

## Partes que deram mais trabalho

- **Limite diário por conta sem contador.** O quanto cada conta já enviou é
  sempre contado a partir do registro de envios, então nada precisa ser
  zerado à meia-noite e o número continua certo depois de um reinício. A vaga
  é reservada no momento em que o lote vai para o bot e confirmada quando a
  mensagem sai, o que impede dois lotes simultâneos de passarem do limite.
- **Um navegador para várias contas.** As contas do Instagram abertas no
  painel rodam como contextos isolados de um único Chromium. Os cookies são
  lidos da pasta de perfil (DPAPI + AES-GCM), sem abrir nem alterar a pasta.
  A página é transmitida para o painel por screencast do DevTools Protocol, e
  cliques e teclas voltam pelo mesmo canal. Contas sem ninguém olhando dormem
  e acordam em cerca de 1 s. Com três contas, a memória caiu de ~1,3 GB para
  ~0,75 GB.
- **Reels leve.** No aquecimento, as respostas do Instagram são reescritas no
  navegador (CDP `Fetch`) para deixar no manifesto DASH só a versão de menor
  qualidade e sem trilha de áudio. Assim o vídeo toca no Chromium sem codecs
  proprietários e custa uma fração da banda e da memória.
- **Mídia do WhatsApp.** Como a versão atual do WhatsApp Web não expõe mais o
  id serializado das mensagens, o bot monta o id a partir das partes e decifra
  a mídia por conta própria (HKDF + AES-256-CBC com verificação de MAC).
- **Painel só local.** Guarda de `Host` contra DNS rebinding, conferência de
  origem nas rotas que mudam estado e serviços internos ouvindo só em
  `127.0.0.1`.

## Tecnologias

Python 3 (Flask, Playwright, APScheduler, SQLite), Node.js (Express,
whatsapp-web.js), JavaScript puro no painel, Chrome DevTools Protocol.

## Estrutura

| Caminho | O que tem |
|---|---|
| `dashboard/` | painel web (Flask + HTML/CSS/JS) |
| `senders/whatsapp_bot/` | bot de WhatsApp em Node, uma sessão por número |
| `senders/dm_bot/` | bot do Instagram (prospecção, Direct no painel, aquecimento) |
| `senders/mail_bot/` | envio de e-mail pelo Gmail |
| `limites.py`, `envio_fila.py`, `esteira_wa.py`, `follow_up.py` | regras de envio |
| `identidade.py`, `diagnostico.py`, `proposta.py` | qualificação, diagnóstico e proposta |
| `test_*.py` | testes (rodam com `python test_nome.py`) |

## Como rodar

Requisitos: Python 3.12+, Node.js 20+ e Windows (a leitura dos cookies do
Chromium usa a DPAPI).

```bash
pip install -r requirements.txt
python -m playwright install chromium
cd senders/whatsapp_bot && npm install && cd ../..
cp .env.example .env        # e preencha as chaves
python subir.py             # sobe painel, bot de WhatsApp e bot do Instagram
```

O painel abre em `http://localhost:5020`. `python subir.py --parar` encerra
tudo. Para rodar um teste: `python -X utf8 test_limite_por_conta.py`.

## Uso responsável

Envio em volume para quem não pediu fere as regras das plataformas e a LGPD.
O projeto tem modo de teste (só envia para contatos liberados), limite diário
por conta, intervalo entre mensagens e trava contra repetir a mesma mensagem,
mas quem opera continua responsável pelo uso.

---

## English

LeadForge is a B2B prospecting tool I built to sell digital presence services
to clinics and small businesses. It finds businesses on Google Maps, scores
which ones are worth reaching, generates a website diagnosis and a PDF
proposal, makes first contact over WhatsApp, Instagram and email, and tracks
the conversations up to the meeting.

**Highlights**

- **Per-account daily limits without counters**: usage is always derived from
  the send log, a slot is reserved when a batch is handed to the bot and
  confirmed when the message goes out, so concurrent batches can't exceed the
  limit and nothing has to be reset at midnight.
- **One browser for many accounts**: Instagram accounts opened in the dashboard
  run as isolated contexts of a single Chromium, with cookies read from the
  profile folder (DPAPI + AES-GCM). The page is streamed into the dashboard via
  a DevTools Protocol screencast; idle accounts sleep and wake in about a
  second. Three accounts went from ~1.3 GB to ~0.75 GB of RAM.
- **Lightweight Reels**: during account warm-up, Instagram responses are
  rewritten inside the browser (CDP `Fetch`) so the DASH manifest only keeps the
  lowest-quality rendition and no audio track, which lets the video play in a
  Chromium build without proprietary codecs.
- **WhatsApp media**: the bot rebuilds message ids and decrypts media itself
  (HKDF + AES-256-CBC with MAC check) after WhatsApp Web stopped exposing the
  serialized id.
- **Local-only dashboard**: Host header guard against DNS rebinding, origin
  checks on state-changing routes, internal services bound to `127.0.0.1`.

**Stack**: Python 3 (Flask, Playwright, APScheduler, SQLite), Node.js
(Express, whatsapp-web.js), vanilla JavaScript, Chrome DevTools Protocol.

**Running it**: see *Como rodar* above. The commands are the same; the code
and the UI are in Portuguese.
