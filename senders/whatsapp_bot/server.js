/**
 * server.js — Bot de WhatsApp do LeadForge. MULTI-CONTA.
 *
 * O que faz:
 *   • Conecta VÁRIAS contas via QR code, cada uma com a sua sessão salva
 *   • Envia mensagens (usado pela bridge do dashboard)
 *   • RECEBE mensagens e repassa para a IA responder (atendimento automático)
 *   • Verifica se um número tem WhatsApp (isRegisteredUser)
 *
 * Painel:  http://localhost:5021
 * API:     POST /api/enviar          { numero, mensagem, conta? }
 *          POST /api/enviar_lote     { alvos: [{numero, mensagem}], conta? }
 *          POST /api/enviar_arquivo  { numero, caminho, legenda, conta? }
 *          GET  /api/verificar?numero=55...&conta=...
 *          GET  /api/status?conta=...
 *          GET  /api/contas                  lista todas com o estado de cada
 *          POST /api/contas       { conta }   cadastra e inicia (gera o QR)
 *          DELETE /api/contas/:conta          desconecta e esquece a sessão
 *
 * SOBRE MULTI-CONTA
 * Cada conta é um cliente próprio do whatsapp-web.js, com o seu Chromium e a
 * sua pasta de sessão (`LocalAuth` com `clientId`). Não dá para compartilhar:
 * uma sessão do WhatsApp Web pertence a um número, e é por isso que a versão
 * anterior só conseguia um número por processo.
 *
 * CUSTO: cada conta conectada é um Chromium de verdade rodando (~300 MB). Duas
 * contas, dois navegadores. Por isso nenhuma conta sobe sozinha sem ter sido
 * cadastrada antes.
 *
 * COMPATIBILIDADE: quem chamar sem `conta` fala com a conta PADRÃO, que é a
 * sessão que já existia antes desta mudança (pasta `session`, sem clientId).
 * Isso preserva o pareamento atual — reparear custa um QR novo no celular.
 *
 * A IA de conversa roda no Python (conversa_ia.py). Este servidor só entrega a
 * mensagem recebida e devolve a resposta.
 */

const express = require('express');
const { Client, LocalAuth, MessageMedia } = require('whatsapp-web.js');
const qrcode = require('qrcode');
const path = require('path');
const fs = require('fs');
// Para encerrar navegador orfao no Windows — ver `matarNavegadorOrfao`.
const { execFileSync } = require('child_process');

const PORT = process.env.PORT || 5021;
const IA_URL = process.env.IA_URL || 'http://localhost:5020/api/conversa/responder';
// Onde o chat ao vivo grava o que SAI desta conta (ver `message_create`). Mesmo
// servidor da IA_URL; a rota so grava, nao responde nem mexe na regua.
const CHAT_URL = process.env.CHAT_URL || new URL('/api/chat/inbound', IA_URL).toString();
// Onde a foto, o áudio e o documento de uma mensagem são entregues ao chat.
const MIDIA_URL = new URL('/api/chat/midia', IA_URL).toString();
// Tipos que têm arquivo e o teto do que se baixa (vídeo longo não entra no chat).
const TIPOS_COM_MIDIA = new Set(['image', 'video', 'audio', 'ptt', 'document', 'sticker']);
const MIDIA_MAX_BYTES = 25 * 1024 * 1024;
// A sessao NAO pode morar no OneDrive.
const SESSION_DIR = process.env.WA_SESSION_DIR
  || path.join(process.env.LOCALAPPDATA || process.env.HOME || __dirname,
               'clinica_prospector', 'wa-sessao');
const RESPOSTA_ARQ = path.join(__dirname, 'resposta.json');
// Quais contas existem. Sem isto, reiniciar o processo esqueceria os numeros
// pareados e o operador teria que cadastrar tudo de novo a cada restart.
const CONTAS_ARQ = path.join(__dirname, 'contas.json');

const app = express();
app.use((req, res, next) => {
  if (!hostPermitido(req.headers.host)) {
    return res.status(403).type('text/plain').send('Host não permitido: só localhost ou IP.');
  }
  if (req.path.startsWith('/api/')) {
    const origem = String(req.headers.origin || '');
    const deOutroSite = String(req.headers['sec-fetch-site'] || '').toLowerCase() === 'cross-site'
      || origem === 'null'
      || (origem && (() => { try { return new URL(origem).host !== req.headers.host; } catch (e) { return true; } })());
    if (deOutroSite) return res.status(403).type('text/plain').send('Pedido de outro site recusado.');
  }
  next();
});
app.use(express.json({ limit: '2mb' }));

const CONTA_PADRAO = '';        // sessao legada, sem clientId

function soDigitos(n) {
  return String(n || '').replace(/\D/g, '');
}

// Como cada tipo aparece no chat ao vivo. Fora desta lista e de 'chat' sao
// avisos do proprio WhatsApp (criptografia, chamada perdida...): nao entram.
const ROTULO_MIDIA = {
  image: 'imagem', video: 'vídeo', audio: 'áudio', ptt: 'áudio',
  document: 'documento', sticker: 'figurinha', location: 'localização',
  vcard: 'contato', multi_vcard: 'contatos', revoked: 'mensagem apagada',
};

/**
 * O texto que o chat mostra para uma mensagem. Em midia, `body` e a legenda;
 * em localizacao e contato ele traz dado cru (miniatura, vCard) e fica de fora.
 */
function textoDaMensagem(msg) {
  const corpo = String((msg && msg.body) || '').trim();
  const tipo = String((msg && msg.type) || 'chat');
  if (tipo === 'chat') return corpo;
  const rotulo = ROTULO_MIDIA[tipo];
  if (!rotulo) return '';
  const semCorpo = ['location', 'vcard', 'multi_vcard', 'revoked'].includes(tipo);
  const legenda = semCorpo ? '' : corpo.slice(0, 2000);
  return `[${rotulo}]` + (legenda ? ' ' + legenda : '');
}

/**
 * Id da mensagem no WhatsApp ("true_5582...@c.us_3EB0..."). É o que impede
 * gravar a mesma mensagem duas vezes no chat e o que permite buscar a mídia
 * dela depois. Nem toda versão do WhatsApp Web devolve `_serialized` pronto:
 * sem ele, monta das partes. Até a v024 as 344 mensagens do chat ficaram sem id.
 */
function idDaMensagem(msg) {
  const i = msg && msg.id;
  if (!i) return null;
  if (typeof i === 'string') return i;
  if (i._serialized) return String(i._serialized);
  const r = i.remote;
  const remoto = r && (typeof r === 'string' ? r
    : r._serialized || (r.user && r.server ? `${r.user}@${r.server}` : ''));
  if (i.id && remoto) return `${i.fromMe ? 'true' : 'false'}_${remoto}_${i.id}`;
  const d = msg._data && msg._data.id;
  return (d && (d._serialized || (typeof d === 'string' ? d : null))) || null;
}

// ── Mídia: decifragem padrão do WhatsApp ──────────────────────────────────── O
// arquivo fica no servidor do WhatsApp cifrado com AES-256-CBC; a chave sai de
// HKDF-SHA256(mediaKey, 112 bytes, "WhatsApp <Tipo> Keys"): 16 de IV, 32 de
// chave e 32 de MAC.
const crypto = require('crypto');
const INFO_MIDIA = {
  image: 'WhatsApp Image Keys', sticker: 'WhatsApp Image Keys', video: 'WhatsApp Video Keys',
  audio: 'WhatsApp Audio Keys', ptt: 'WhatsApp Audio Keys', document: 'WhatsApp Document Keys',
};
async function decifrarMidia({ directPath, url, mediaKey, type }) {
  const rotulo = INFO_MIDIA[type];
  if (!rotulo || !mediaKey) return null;
  const endereco = directPath ? 'https://mmg.whatsapp.net' + directPath : url;
  if (!/^https:\/\/[a-z0-9.-]+\.whatsapp\.net\//i.test(endereco)) return null;   // só o servidor do WhatsApp
  const r = await fetch(endereco, { headers: { Origin: 'https://web.whatsapp.com' },
                                   redirect: 'error', signal: AbortSignal.timeout(30000) });
  if (!r.ok) {
    // 404/410: o WhatsApp não tem mais o arquivo (o painel não pede de novo)
    const e = new Error('mídia indisponível no WhatsApp'); e.status = r.status; throw e;
  }
  // o tamanho declarado pelo remetente não basta: confere o que o servidor diz
  if (Number(r.headers.get('content-length') || 0) > MIDIA_MAX_BYTES + 64) return null;
  const cifrado = Buffer.from(await r.arrayBuffer());
  if (cifrado.length <= 10 || cifrado.length > MIDIA_MAX_BYTES + 64) return null;
  const chaves = Buffer.from(crypto.hkdfSync('sha256', Buffer.from(mediaKey, 'base64'),
                                             Buffer.alloc(32), Buffer.from(rotulo), 112));
  const iv = chaves.subarray(0, 16), chave = chaves.subarray(16, 48), chaveMac = chaves.subarray(48, 80);
  const corpo = cifrado.subarray(0, -10), mac = cifrado.subarray(-10);
  const esperado = crypto.createHmac('sha256', chaveMac).update(iv).update(corpo).digest().subarray(0, 10);
  if (!crypto.timingSafeEqual(mac, esperado)) throw new Error('mídia com MAC inválido');
  const d = crypto.createDecipheriv('aes-256-cbc', chave, iv);
  return Buffer.concat([d.update(corpo), d.final()]);
}

// No máximo 2 mídias baixando ao mesmo tempo (um lote de 100 envios com o
// mesmo PDF disparava 100 downloads juntos, na página que estava enviando).
let midiasBaixando = 0;
const filaMidias = [];
function naFilaDeMidia(tarefa) {
  return new Promise((resolve, reject) => {
    const rodar = async () => {
      midiasBaixando++;
      try { resolve(await tarefa()); } catch (e) { reject(e); }
      finally { midiasBaixando--; const prox = filaMidias.shift(); if (prox) prox(); }
    };
    if (midiasBaixando < 2) rodar(); else filaMidias.push(rodar);
  });
}

// Arquivo que o PRÓPRIO bot mandou (material, proposta): o chat lê do disco em
// vez de baixar de volta do WhatsApp o que acabou de sair daqui.
const ARQUIVOS_ENVIADOS = new Map();    // "conta|número completo" -> { caminho, t }
function chaveNumero(conta, n) { return `${conta || ''}|${comDDI(n)}`; }

// Guarda de Host e de outro site, a mesma do painel e do bot de Instagram
// (servidor_local.py): um site aberto no navegador do operador não fala com este
// bot nem passando por 127.0.0.1 (DNS rebinding, formulário, <img>).
const NOMES_LOCAIS = new Set(['localhost', require('os').hostname().toLowerCase()]);
function hostPermitido(host) {
  let h = String(host || '').trim().toLowerCase();
  if (!h) return false;
  if (h.startsWith('[')) {
    const fim = h.indexOf(']');
    const resto = fim > 0 ? h.slice(fim + 1) : 'x';
    if (resto && !/^:\d+$/.test(resto)) return false;
    h = h.slice(1, fim);
  } else if ((h.match(/:/g) || []).length === 1) {
    h = h.split(':')[0];
  }
  h = h.replace(/\.$/, '');
  if (NOMES_LOCAIS.has(h) || h.endsWith('.localhost')) return true;
  return require('net').isIP(h) !== 0;
}

/** Id de conta seguro: vira nome de pasta, entao so digitos. */
function normalizarConta(c) {
  return soDigitos(c);
}

// ── Trava de resposta automática ────────────────────────────────────────────
// Sem isto, a IA responde qualquer mensagem privada que chegar na conta. off =
// nunca responde (só registra no log).
let resposta = { modo: 'off', permitidos: [] };

function carregarResposta() {
  try {
    if (fs.existsSync(RESPOSTA_ARQ)) {
      const d = JSON.parse(fs.readFileSync(RESPOSTA_ARQ, 'utf8'));
      resposta.modo = ['off', 'lista', 'todos'].includes(d.modo) ? d.modo : 'off';
      resposta.permitidos = (d.permitidos || []).map(soDigitos).filter(Boolean);
    }
  } catch (e) {
    console.log('Falha ao ler resposta.json, usando modo off: ' + e.message);
  }
}

function salvarResposta() {
  fs.writeFileSync(RESPOSTA_ARQ, JSON.stringify(resposta, null, 2), 'utf8');
}

/** Compara pelos 8 dígitos finais: ignora 55, DDD e o 9 extra. */
function permitido(numero) {
  const alvo = soDigitos(numero).slice(-8);
  if (alvo.length < 8) return false;
  return resposta.permitidos.some((p) => p.slice(-8) === alvo);
}

/** Decide se a IA pode responder este número. Devolve o motivo quando não pode. */
function podeResponder(numero) {
  if (resposta.modo === 'todos') return { ok: true };
  if (resposta.modo === 'lista') {
    return permitido(numero) ? { ok: true }
                             : { ok: false, motivo: 'número fora da allowlist' };
  }
  return { ok: false, motivo: 'resposta automática desligada (modo off)' };
}

carregarResposta();

// ── Registro de contas conhecidas ───────────────────────────────────────────
function lerContasSalvas() {
  try {
    if (fs.existsSync(CONTAS_ARQ)) {
      const d = JSON.parse(fs.readFileSync(CONTAS_ARQ, 'utf8'));
      return (d.contas || []).map(normalizarConta).filter(Boolean);
    }
  } catch (e) {
    console.log('Falha ao ler contas.json: ' + e.message);
  }
  return [];
}

function salvarContas() {
  const ids = [...contas.keys()].filter((id) => id !== CONTA_PADRAO);
  fs.writeFileSync(CONTAS_ARQ, JSON.stringify({ contas: ids }, null, 2), 'utf8');
}

// ── Uma conta = um cliente, um estado, um log, um religador ─────────────────
/** @type {Map<string, ReturnType<typeof criarConta>>} */
const contas = new Map();

function criarConta(id) {
  id = id === CONTA_PADRAO ? CONTA_PADRAO : normalizarConta(id);

  const estado = { pronto: false, qr: null, numero: null, iniciado: false,
                   ultimoErro: null, perfilAberto: false };
  const log = [];

  function registrar(msg) {
    const etiqueta = id ? `[${id}]` : '[padrão]';
    const linha = `[${new Date().toLocaleTimeString('pt-BR')}] ${etiqueta} ${msg}`;
    log.unshift(linha);
    if (log.length > 200) log.pop();
    console.log(linha);
  }

  // `clientId` indefinido na conta padrão de propósito: é o que mantém a sessão
  // que já estava pareada antes do multi-conta (pasta `session`, não
  // `session-<id>`). Reparear custa um QR novo no celular do operador.
  const auth = id
    ? new LocalAuth({ dataPath: SESSION_DIR, clientId: id })
    : new LocalAuth({ dataPath: SESSION_DIR });

  const client = new Client({
    authStrategy: auth,
    puppeteer: {
      headless: true,
      args: ['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage'],
    },
  });

  // ── Manter conectado ────────────────────────────────────────────────────── O
  // cliente era inicializado UMA vez e `estado.iniciado` nunca voltava a false.
  let religando = false;
  let tentativas = 0;
  let religarTimer = null;
  let abrindoPerfil = false;

  // HA UMA ABERTURA EM ANDAMENTO NESTA CONTA?
  let abrindo = false;

  async function abrir() {
    abrindo = true;
    try {
      await client.initialize();
    } finally {
      abrindo = false;
    }
  }

  // Desde quando esta conta esta carregando, e quanto tempo se da a ela.
  let carregandoDesde = 0;
  const CARGA_MAXIMA_MS = 4 * 60 * 1000;

  // A pasta de perfil desta conta. `LocalAuth` usa `session-<id>`, ou `session`
  // na conta padrao (sem clientId).
  const pastaPerfil = path.join(SESSION_DIR, id ? `session-${id}` : 'session');

  // Remove a trava que sobrou de um processo morto.
  function limparTravaOrfa() {
    const trava = path.join(pastaPerfil, 'lockfile');
    try {
      if (!fs.existsSync(trava)) return false;
      fs.unlinkSync(trava);
      registrar('Trava orfa removida do perfil (lockfile) — sobrou de um '
                + 'encerramento forcado');
      return true;
    } catch (e) {
      // Se o arquivo estiver mesmo em uso, o Windows recusa a exclusao. Nesse
      // caso ha um navegador vivo de verdade e nao ha nada a limpar.
      registrar('Nao deu para remover a trava: ' + String((e && e.message) || e).slice(0, 80));
      return false;
    }
  }

  // Encerra o navegador que ficou VIVO segurando este perfil.
  function matarNavegadorOrfao() {
    if (process.platform !== 'win32') return false;
    try {
      // O caminho vai como argumento do PowerShell, nao concatenado no script: uma
      // pasta com aspas ou `;` no nome viraria comando executavel.
      if (abrindo) { return false; }

      // E, nao havendo abertura em andamento, mata QUALQUER navegador naquele perfil —
      // inclusive filho nosso.
      const script = [
        '$nome = $env:LF_NOME; $padrao = $env:LF_PADRAO;',
        '$ps = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |',
        '  Where-Object { $_.Name -eq $nome -and $_.CommandLine -like $padrao };',
        '$n = 0;',
        'foreach ($x in $ps) {',
        '  try { Stop-Process -Id $x.ProcessId -Force -ErrorAction Stop; $n++ } catch {}',
        '};',
        'Write-Output $n',
      ].join(' ');
      const saida = execFileSync(
        'powershell.exe',
        ['-NoProfile', '-NonInteractive', '-Command', script],
        { timeout: 20000, encoding: 'utf8', windowsHide: true,
          env: Object.assign({}, process.env, {
            LF_NOME: 'chrome.exe',
            LF_PADRAO: '*--user-data-dir=' + pastaPerfil + '*',
          }) });
      const quantos = parseInt(String(saida).trim(), 10) || 0;
      if (quantos > 0) {
        registrar(`Encerrei ${quantos} navegador(es) orfao(s) que seguravam o perfil`);
        return true;
      }
      return false;
    } catch (e) {
      registrar('Nao deu para encerrar navegador orfao: '
                + String((e && e.message) || e).slice(0, 80));
      return false;
    }
  }

  // As duas travas, na ordem que faz sentido: primeiro o processo (se houver),
  // depois o arquivo que ele deixou. Ao contrario, apagar o arquivo com o
  // processo vivo nao resolve nada — ele o reescreve.
  function destravarPerfil() {
    const matou = matarNavegadorOrfao();
    if (matou) {
      // O Chromium leva um instante para soltar o arquivo depois de morto.
      try { execFileSync('powershell.exe',
        ['-NoProfile', '-NonInteractive', '-Command', 'Start-Sleep -Milliseconds 1500'],
        { timeout: 8000, windowsHide: true }); } catch (e) { /* segue */ }
    }
    const limpou = limparTravaOrfa();
    if (!matou && !limpou) {
      // Silencio aqui foi o que escondeu,, que a rotina de matar orfao nem estava
      // rodando: o log so mostrava o EBUSY do arquivo — o sintoma — e nada sobre a
      // causa.
      registrar('Nada a destravar: nenhum navegador orfao neste perfil e '
                + 'nenhuma trava removivel');
    }
    return matou || limpou;
  }

  async function religar(motivo) {
    if (religando) return;
    religando = true;
    estado.pronto = false;
    estado.iniciado = false;
    const espera = Math.min(60000, 5000 * Math.pow(2, Math.min(tentativas, 4)));
    tentativas += 1;
    registrar(`Religando em ${Math.round(espera / 1000)}s (motivo: ${motivo}, tentativa ${tentativas})`);
    clearTimeout(religarTimer);
    religarTimer = setTimeout(async () => {
      let falhou = null;
      try {
        // destruir antes de subir de novo evita duas instâncias do Chromium na
        // mesma sessão, que é o que o WhatsApp trata como conflito
        try { await client.destroy(); } catch (e) { /* já estava morto */ }
        // o Chromium não solta o lockfile do userDataDir no mesmo instante em que é
        // fechado.
        await new Promise((r) => setTimeout(r, 4000));
        estado.iniciado = true;
        carregandoDesde = Date.now();
        await abrir();
        tentativas = 0;          // voltou: a próxima queda recomeça a espera curta
      } catch (e) {
        estado.iniciado = false;
        falhou = String((e && e.message) || e).slice(0, 160);
      }
      // A trava orfa e o unico erro que se repete identico para sempre: sem
      // limpar, as tentativas seguintes recebem a mesma mensagem. Tenta uma
      // vez, na hora, em vez de esperar o proximo ciclo de 60s.
      if (falhou && /already running/i.test(falhou) && destravarPerfil()) {
        try {
          estado.iniciado = true;
          carregandoDesde = Date.now();
          await abrir();
          tentativas = 0;
          falhou = null;
          registrar('Religou depois de limpar a trava');
        } catch (e) {
          estado.iniciado = false;
          falhou = String((e && e.message) || e).slice(0, 160);
        }
      }

      religando = false;
      if (falhou) {
        registrar('Falha ao religar: ' + falhou);
        // TENTAR DE NOVO. Antes parava aqui: `estado.iniciado` ficava false e o monitor,
        // que só age com `iniciado` true, nunca mais olhava para isso.
        religar('religamento anterior falhou');
      }
    }, espera);
  }

  // Pergunta de verdade se a sessão responde. `pronto` sozinho mente: o evento
  // 'ready' já disparou e o Chromium pode ter morrido depois, sem avisar.
  const monitor = setInterval(async () => {
    if (religando || abrindoPerfil || estado.perfilAberto || !estado.iniciado) return;
    // Esperando pareamento NÃO é falha.
    if (estado.qr) return;
    // ainda dentro da janela de carga: nao ha o que julgar
    if (carregandoDesde && Date.now() - carregandoDesde < CARGA_MAXIMA_MS) return;
    try {
      const st = await client.getState();
      if (st === 'CONNECTED') {
        estado.pronto = true;
        return;
      }
      registrar('Monitor: estado ' + st);
      religar('estado ' + st);
    } catch (e) {
      registrar('Monitor: sem resposta do cliente');
      religar('cliente não respondeu');
    }
  }, 60000);

  client.on('qr', async (qr) => {
    // Gerou QR: o erro anterior deixou de valer. `ultimoErro` guarda o ULTIMO
    // erro visto e ficava preenchido para sempre — a tela nao tinha como
    // distinguir "quebrou agora" de "quebrou semana passada e ja voltou".
    estado.ultimoErro = null;
    estado.qr = await qrcode.toDataURL(qr);
    estado.pronto = false;
    registrar('QR code gerado. Abra http://localhost:' + PORT + ' e escaneie.');
  });

  client.on('authenticated', () => registrar('Autenticado com sucesso.'));

  client.on('ready', () => {
    carregandoDesde = 0;          // carregou: o monitor volta a julgar normalmente
    estado.ultimoErro = null;     // conectou: erro velho nao pode aparecer como atual
    estado.pronto = true;
    tentativas = 0;               // reconectou: a espera volta ao mínimo
    estado.qr = null;
    estado.ultimoErro = null;     // conectou: o erro anterior deixou de valer
    estado.numero = client.info?.wid?.user || null;
    registrar(`Conectado como ${estado.numero}. Pronto para enviar e receber.`);
    // Cadastrar a conta pelo número real evita o descompasso entre o id que o
    // operador digitou e o número que de fato pareou o QR.
    if (id && estado.numero && soDigitos(estado.numero) !== id) {
      registrar(`ATENÇÃO: cadastrada como ${id}, mas o QR pareou ${estado.numero}.`);
    }
  });

  client.on('auth_failure', (m) => {
    estado.ultimoErro = 'Falha de autenticação: ' + m;
    registrar(estado.ultimoErro);
  });

  client.on('disconnected', (motivo) => {
    if (abrindoPerfil) return;
    if (estado.perfilAberto) {
      estado.perfilAberto = false;
      client.options.puppeteer.headless = true;
      registrar('Janela visível fechada; vou retomar o bot em segundo plano.');
    }
    estado.pronto = false;
    estado.numero = null;
    estado.ultimoErro = 'desconectado: ' + motivo;
    registrar('Desconectado: ' + motivo);
    if (String(motivo).toUpperCase() === 'LOGOUT') {
      registrar('A sessão foi encerrada no celular. Escaneie o QR de novo em '
        + 'http://localhost:' + PORT + ' (aba Contas do dashboard).');
    }
    // religa nos dois casos: em LOGOUT é o próprio religar que produz o QR novo
    religar('desconectado: ' + motivo);
  });

  async function abrirPerfil() {
    if (estado.perfilAberto || abrindoPerfil) {
      return { ok: false, erro: 'O navegador desta conta já está aberto ou abrindo.' };
    }
    if (!estado.iniciado || !client.pupBrowser) {
      return { ok: false, erro: 'A sessão do WhatsApp ainda não está aberta no bot. Conecte a conta primeiro.' };
    }

    abrindoPerfil = true;
    estado.pronto = false;
    estado.ultimoErro = null;
    registrar('Preparando a sessão atual para abrir em uma janela visível...');
    try {
      // Reutiliza a mesma Client e o mesmo diretório LocalAuth. Não faz logout,
      // não cria um segundo perfil e não abre um Chromium concorrente.
      await client.destroy();
      await new Promise((r) => setTimeout(r, 4000));
      client.options.puppeteer.headless = false;
      estado.iniciado = true;
      carregandoDesde = Date.now();
      await client.initialize();
      const browser = client.pupBrowser;
      if (!browser) throw new Error('O navegador visível não iniciou.');

      estado.perfilAberto = true;
      abrindoPerfil = false;
      browser.once('disconnected', () => {
        if (!estado.perfilAberto) return;
        estado.perfilAberto = false;
        client.options.puppeteer.headless = true;
        estado.pronto = false;
        estado.iniciado = false;
        registrar('Janela do perfil fechada; retomando a sessão headless.');
        religar('janela do perfil fechada');
      });
      registrar('Sessão aberta no navegador visível. Feche a janela para retomar o modo automático.');
      return { ok: true, conta: id || estado.numero,
               mensagem: 'Navegador autenticado aberto. Feche a janela para retomar o modo automático.' };
    } catch (e) {
      abrindoPerfil = false;
      estado.perfilAberto = false;
      estado.pronto = false;
      estado.iniciado = false;
      client.options.puppeteer.headless = true;
      const erro = String((e && e.message) || e).slice(0, 180);
      registrar('Falha ao abrir o perfil no navegador: ' + erro);
      religar('falha ao abrir janela do perfil');
      return { ok: false, erro };
    }
  }

  // ── Recebe mensagem e chama a IA ──────────────────────────────────────────
  /**
   * Descobre o TELEFONE real de quem mandou a mensagem.
   *
   * O WhatsApp passou a entregar o remetente como `<id>@lid` (identificador
   * interno) em vez de `<telefone>@c.us`. Tirar só o sufixo devolvia o id
   * interno, e aí a allowlist comparava id contra telefone: nunca casava, e a
   * IA nunca respondia nem quem estava liberado. O contato resolve o número.
   */
  async function numeroReal(msg) {
    try {
      const c = await msg.getContact();
      // ATENÇÃO à ordem. Em conta com LID, `c.number` devolve o LID (ex.:
      // 130335262130272) e só `c.id.user` traz o telefone de verdade (ex.:
      // 558299990000, server 'c.us').
      if (c && c.id && c.id.server === 'c.us') {
        const n = soDigitos(c.id.user);
        if (n.length >= 10 && n.length <= 13) return n;
      }
      // reserva: só aceita se tiver cara de telefone. LID tem 15+ dígitos.
      const alt = soDigitos(c && c.number);
      if (alt.length >= 10 && alt.length <= 13) return alt;
    } catch (e) { /* cai no id cru abaixo */ }
    return soDigitos(String(msg.from).split('@')[0]);
  }

  // Grupo, canal (newsletter), lista de transmissão e status não são conversa
  // com lead: entravam no chat ao vivo com o id do grupo/canal no lugar do
  // número. Regra única para o que chega e o que sai.
  function ehConversaPessoal(jid) {
    const j = String(jid || '');
    return !!j && !j.endsWith('@g.us') && !j.endsWith('@newsletter') && !j.endsWith('@broadcast');
  }

  client.on('message', async (msg) => {
    try {
      if (!ehConversaPessoal(msg.from)) return;
      if (msg.fromMe) return;
      // Atualização de STATUS não é mensagem para nós.
      if (msg.from === 'status@broadcast' || msg.isStatus) return;
      if (msg.broadcast) return;
      const numero = await numeroReal(msg);
      const cru = String(msg.from).split('@')[0];
      const etiqueta = numero === cru ? numero : `${numero} (${msg.from})`;
      registrar(`Recebido de ${etiqueta}: ${(msg.body || '').slice(0, 60)}`);

      // A trava local decide se o bot pode RESPONDER — não se o Python fica sabendo.
      const trava = podeResponder(numero);

      const resp = await fetch(IA_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // `conta` vai junto para o Python saber por QUAL número a conversa chegou: com
        // duas contas ativas, responder pela errada põe o lead falando com um número que
        // nunca o procurou.
        body: JSON.stringify({ numero, mensagem: msg.body || '', conta: id || null,
          nome: (msg._data && (msg._data.notifyName || msg._data.pushname)) || '',
          message_id: idDaMensagem(msg),
          texto_chat: textoDaMensagem(msg), timestamp: msg.timestamp || null }),
      });
      const dados = await resp.json();
      if (msg.hasMedia) anexarMidia(msg, numero);    // à parte: não atrasa a resposta

      if (!trava.ok) {
        registrar(`Não respondido para ${numero}: ${trava.motivo}`);
        return;
      }

      if (dados.responder && dados.texto) {
        // atraso curto para parecer humano
        const espera = Math.min(6000, 1200 + (dados.texto.length * 25));
        await new Promise((r) => setTimeout(r, espera));
        await client.sendMessage(msg.from, dados.texto);
        registrar(`Respondido para ${numero}: ${dados.texto.slice(0, 60)}`);
      } else {
        registrar(`Sem resposta automática para ${numero} (${dados.motivo || 'IA desligada'})`);
      }
    } catch (e) {
      const dica = /fetch failed|ECONNREFUSED/i.test(e.message)
        ? ' (o dashboard em localhost:5020 está desligado?)' : '';
      registrar('Erro ao tratar mensagem: ' + e.message + dica);
    }
  });

  /**
   * O telefone do contato de um chat (`msg.to` quando a mensagem SAI daqui).
   *
   * Mesma regra de `numeroReal`, que resolve o REMETENTE: com LID, so
   * `c.id.user` traz o telefone. As duas precisam chegar ao mesmo numero, senao
   * o que sai e o que chega viram duas conversas com a mesma pessoa.
   */
  async function numeroDoChat(chatId) {
    try {
      const c = await client.getContactById(chatId);
      if (c && c.id && c.id.server === 'c.us') {
        const n = soDigitos(c.id.user);
        if (n.length >= 10 && n.length <= 13) return n;
      }
      const alt = soDigitos(c && c.number);
      if (alt.length >= 10 && alt.length <= 13) return alt;
    } catch (e) { /* cai no id cru abaixo */ }
    return soDigitos(String(chatId).split('@')[0]);
  }

  // ── O que SAI desta conta tambem entra no chat ───────────────────────────── A
  // caixa mostrava so o que o lead escreveu e o que se respondeu pelo painel: a
  // abordagem do bot, a resposta dada pelo celular e o material em PDF ficavam de
  // fora, e a conversa aparecia pela metade.
  client.on('message_create', async (msg) => {
    try {
      if (!msg.fromMe) return;
      const para = String(msg.to || '');
      if (!ehConversaPessoal(para) || msg.isStatus || msg.broadcast) return;
      if (msg.from && para === msg.from) return;          // recado para si mesmo
      const texto = textoDaMensagem(msg);
      if (!texto) return;
      const numero = await numeroDoChat(para);
      if (!numero) return;
      await fetch(CHAT_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel: 'whatsapp', account: id || 'padrão',
          peer_id: numero, text: texto, direction: 'out', delivery_state: 'sent',
          provider_id: idDaMensagem(msg),
          timestamp: msg.timestamp || null }),
      });
      if (msg.hasMedia) anexarMidia(msg, numero);
    } catch (e) {
      registrar('Nao gravei no chat a mensagem enviada: ' + e.message);
    }
  });

  /**
   * Descobre o ID de destino que o WhatsApp aceita HOJE.
   *
   * Montar "55DDD9XXXXXXXX@c.us" na mão parou de funcionar: o WhatsApp migrou
   * para LID e o envio falha com "No LID for user". `getNumberId` pergunta ao
   * servidor qual é o id correto do contato, e ainda resolve de quebra a
   * variação do 9º dígito e os fixos. Devolve null quando não tem WhatsApp.
   */
  // Uma consulta ao WhatsApp por contato a cada 10 min: a conversa com 30 fotos
  // antigas fazia 30 consultas iguais — volume de consulta de contato é o tipo
  // de coisa que o WhatsApp vigia.
  const destinos = new Map();
  async function resolverDestino(numero) {
    const chave = comDDI(numero), guardado = destinos.get(chave);
    if (guardado && Date.now() - guardado.t < 10 * 60 * 1000) return guardado.id;
    try {
      const wid = await client.getNumberId(chave);
      const id = wid && (wid._serialized || (wid.user && wid.server ? `${wid.user}@${wid.server}` : null));
      if (id) { destinos.set(chave, { id, t: Date.now() }); return id; }
    } catch (e) {
      registrar(`Falha ao resolver ${numero}: ${e.message}`);
    }
    return null;
  }

  /**
   * Envia resolvendo o destino primeiro, com o chat como segunda tentativa.
   * Devolve { ok, id, erro }.
   */
  async function entregar(numero, mensagem) {
    const destino = await resolverDestino(numero);
    if (!destino) return { ok: false, erro: 'número não tem WhatsApp' };
    // `message_id` volta para quem chamou: o chat grava a resposta enviada pelo
    // painel com ele, e o `message_create` da mesma mensagem cai no mesmo
    // registro em vez de duplicar a linha.
    const idDa = (m) => idDaMensagem(m);
    try {
      const m = await client.sendMessage(destino, mensagem);
      return { ok: true, id: destino, message_id: idDa(m) };
    } catch (e1) {
      // segunda tentativa pelo objeto de chat: em algumas contas o envio direto
      // pelo id falha, mas pelo chat já aberto funciona
      try {
        const chat = await client.getChatById(destino);
        const m = await chat.sendMessage(mensagem);
        return { ok: true, id: destino, via: 'chat', message_id: idDa(m) };
      } catch (e2) {
        return { ok: false, id: destino, erro: `${e1.message} | chat: ${e2.message}` };
      }
    }
  }

  /**
   * Envia um arquivo local (o material da análise) resolvendo o destino igual
   * ao texto. `legenda` vai junto como caption, para a mensagem e o anexo
   * chegarem como uma coisa só em vez de duas notificações soltas.
   */
  async function entregarArquivo(numero, caminho, legenda) {
    const ok = caminhoPermitido(caminho);
    if (!ok.ok) return { ok: false, erro: ok.erro };
    const destino = await resolverDestino(numero);
    if (!destino) return { ok: false, erro: 'número não tem WhatsApp' };
    if (!fs.existsSync(caminho)) return { ok: false, id: destino, erro: `arquivo não existe: ${caminho}` };
    ARQUIVOS_ENVIADOS.set(chaveNumero(id, numero), { caminho, t: Date.now() });
    try {
      const media = MessageMedia.fromFilePath(caminho);
      await client.sendMessage(destino, media, legenda ? { caption: legenda } : undefined);
      return { ok: true, id: destino };
    } catch (e1) {
      try {
        const chat = await client.getChatById(destino);
        const media = MessageMedia.fromFilePath(caminho);
        await chat.sendMessage(media, legenda ? { caption: legenda } : undefined);
        return { ok: true, id: destino, via: 'chat' };
      } catch (e2) {
        ARQUIVOS_ENVIADOS.delete(chaveNumero(id, numero));   // não saiu: não vale para a próxima mídia
        return { ok: false, id: destino, erro: `${e1.message} | chat: ${e2.message}` };
      }
    }
  }

  // Um lote de cada vez POR CONTA.
  let filaDeLotes = Promise.resolve();

  async function processarLote(alvos, intervalo) {
    let enviados = 0;
    let falhas = 0;
    for (let i = 0; i < alvos.length; i++) {
      const a = alvos[i];
      const r = await entregar(a.numero, a.mensagem);
      if (r.ok) {
        enviados++;
        registrar(`Lote: enviado para ${a.numero} (${enviados}/${alvos.length})`);
      } else {
        falhas++;
        registrar(`Lote: falha em ${a.numero}: ${r.erro}`);
      }
      // O resultado REAL volta para o Python.
      avisarResultado(a, r, id);
      // não espera depois do último: atrasaria o fim do lote sem proteger nada
      if (i < alvos.length - 1) {
        const espera = sortearEspera(intervalo);
        registrar(`Lote: próximo envio em ${Math.round(espera / 1000)}s`);
        await new Promise((seguir) => setTimeout(seguir, espera));
      }
    }
    registrar(`Lote concluído. Enviados: ${enviados}, falhas: ${falhas}`);
  }

  function enfileirarLote(alvos, intervalo) {
    filaDeLotes = filaDeLotes
      .then(() => processarLote(alvos, intervalo))
      .catch((e) => registrar('Erro no lote: ' + String((e && e.message) || e)));
  }

  async function encerrar() {
    clearInterval(monitor);
    clearTimeout(religarTimer);
    try { await client.logout(); } catch (e) { /* pode não estar logado */ }
    try { await client.destroy(); } catch (e) { /* já estava morto */ }
  }

  function iniciar(forcar) {
    if (estado.perfilAberto || abrindoPerfil) {
      return { ok: false,
               mensagem: 'Feche a janela visível desta conta para retomar o bot.' };
    }
    if (forcar) {
      religar('pedido manual');
      return { ok: true, mensagem: 'religando' };
    }
    if (estado.iniciado && estado.pronto) return { ok: true, mensagem: 'já conectado' };

    // ESPERANDO PAREAMENTO NÃO SE RELIGA.
    if (estado.iniciado && estado.qr) {
      return { ok: true, mensagem: 'aguardando o QR ser escaneado' };
    }
    if (estado.iniciado && !religando) {
      religar('reinício pedido pelo painel');
      return { ok: true, mensagem: 'religando' };
    }
    if (estado.iniciado) return { ok: true, mensagem: 'já iniciando' };
    estado.iniciado = true;
    estado.ultimoErro = null;      // erro velho não pode aparecer como atual
    registrar('Iniciando cliente WhatsApp...');

    // DESTRAVA ANTES DE TENTAR, e nao so depois de falhar.
    destravarPerfil();

    carregandoDesde = Date.now();

    // A promessa era largada sem `catch`.
    abrir().catch(async (e) => {
      const msg = String((e && e.message) || e);
      if (/already running/i.test(msg) && destravarPerfil()) {
        try {
          await abrir();
          registrar('Iniciou depois de limpar a trava');
          return;
        } catch (e2) {
          estado.iniciado = false;
          estado.ultimoErro = String((e2 && e2.message) || e2).slice(0, 200);
          registrar('Falha ao iniciar: ' + estado.ultimoErro);
          religar('falha na partida');
          return;
        }
      }
      estado.iniciado = false;
      estado.ultimoErro = msg.slice(0, 200);
      registrar('Falha ao iniciar: ' + estado.ultimoErro);
      religar('falha na partida');
    });
    return { ok: true, mensagem: 'iniciando, aguarde o QR' };
  }

  /**
   * O arquivo de uma mensagem (foto, áudio, documento), em base64.
   * Devolve null quando não há o que baixar ou passou do teto.
   */
  async function midiaDe(msg) {
    if (!msg || !msg.hasMedia || !TIPOS_COM_MIDIA.has(String(msg.type || ''))) return null;
    const mid = idDaMensagem(msg);
    if (!mid) return null;
    const m = await baixarPeloId(mid);
    if (!m || !m.data) return null;
    const bytes = Math.floor(m.data.length * 3 / 4);
    if (bytes > MIDIA_MAX_BYTES) {
      registrar(`Mídia de ${Math.round(bytes / 1048576)} MB fora do chat (teto ${MIDIA_MAX_BYTES / 1048576} MB)`);
      return null;
    }
    return { mimetype: m.mimetype || '', filename: m.filename || '', data: m.data };
  }

  /**
   * O mesmo que `msg.downloadMedia()` da biblioteca, com o id certo. A da
   * biblioteca manda `this.id._serialized`, que esta versão do WhatsApp Web não
   * tem mais: pedia a mensagem "undefined" e quebrava lá dentro ("r").
   */
  async function baixarPeloId(msgId) {
    // O que a página sabe da mídia (endereço criptografado e chave).
    const info = await client.pupPage.evaluate(async (id) => {
      const Col = window.require('WAWebCollections');
      const msg = Col.Msg.get(id) || (await Col.Msg.getMessagesById([id]))?.messages?.[0];
      if (!msg || !msg.mediaKey || !(msg.directPath || msg.clientUrl || msg.deprecatedMms3Url)) return null;
      return { directPath: msg.directPath || '', url: msg.clientUrl || msg.deprecatedMms3Url || '',
               mediaKey: msg.mediaKey, type: msg.type, mimetype: msg.mimetype || '',
               filename: msg.filename || '', size: msg.size || 0 };
    }, msgId);
    if (!info) return null;
    if (info.size && info.size > MIDIA_MAX_BYTES) return null;
    const dados = await decifrarMidia(info);
    return dados ? { data: dados.toString('base64'), mimetype: info.mimetype, filename: info.filename } : null;
  }

  /**
   * Manda ao chat o arquivo de uma mensagem que acabou de ser gravada. Roda à
   * parte (sem await de quem chama): baixar um áudio não pode atrasar a
   * resposta da IA nem a gravação do texto.
   */
  async function anexarMidia(msg, numero) {
    try {
      let m = null;
      const enviado = msg.fromMe && msg.type === 'document' && ARQUIVOS_ENVIADOS.get(chaveNumero(id, numero));
      if (enviado && Date.now() - enviado.t < 5 * 60 * 1000 && fs.existsSync(enviado.caminho)) {
        ARQUIVOS_ENVIADOS.delete(chaveNumero(id, numero));
        const local = MessageMedia.fromFilePath(enviado.caminho);
        m = { mimetype: local.mimetype || '', filename: local.filename || path.basename(enviado.caminho),
              data: local.data };
      } else {
        m = await naFilaDeMidia(() => midiaDe(msg));
      }
      if (!m) return;
      await fetch(MIDIA_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channel: 'whatsapp', account: id || 'padrão', peer_id: numero,
          provider_id: idDaMensagem(msg), ...m }),
        signal: AbortSignal.timeout(30000),
      });
    } catch (e) {
      registrar('Não anexei a mídia no chat: ' + e.message);
    }
  }

  /**
   * Mídia de uma mensagem antiga (o chat pede ao abrir a conversa). Pelo id,
   * quando a mensagem foi gravada com ele; senão, procura na conversa a
   * mensagem com mídia daquele horário e direção (as gravadas sem id).
   */
  // rótulo do chat -> tipos do WhatsApp
  const TIPOS_DO_ROTULO = { imagem: ['image'], figurinha: ['sticker'], audio: ['audio', 'ptt'],
                            video: ['video'], documento: ['document'] };

  async function baixarMidiaAntiga({ messageId, numero, timestamp, fromMe, tipo, excluir, indice }) {
    if (messageId) {
      const m = await naFilaDeMidia(() => baixarPeloId(messageId));
      return m && m.data ? { mimetype: m.mimetype || '', filename: m.filename || '',
                             data: m.data, message_id: messageId } : null;
    }
    if (!numero || !timestamp) return null;
    const destino = await resolverDestino(numero);
    if (!destino) return null;
    // Pela mesma via do `chat.fetchMessages` da biblioteca, mas parando assim
    // que acha a mensagem ou passa do horário dela: o laço da biblioteca pede
    // páginas até o limite e quebra lá dentro ("r") quando a conversa acaba.
    const achado = await client.pupPage.evaluate(async (chatId, ts, deMim) => {
      const chat = await window.WWebJS.getChat(chatId, { getAsModel: false });
      if (!chat || !chat.msgs) return { ids: [], diag: 'conversa não carregada' };
      const todas = () => chat.msgs.getModelsArray();
      // o id desta versão do WhatsApp Web não tem `_serialized`: o texto sai do toString()
      const idTexto = (m) => (m.id && (m.id._serialized || (m.id.toString && m.id.toString()))) || '';
      const achar = () => todas().filter((m) => Math.abs(Number(m.t || 0) - ts) <= 2
          && !!(m.id && m.id.fromMe) === deMim && m.mediaKey && /_/.test(idTexto(m)))
        .map((m) => ({ id: idTexto(m), t: Number(m.t || 0), type: String(m.type || '') }));
      let ids = achar();
      for (let pag = 0; !ids.length && pag < 10; pag++) {
        const datas = todas().map((m) => Number(m.t || 0));
        if (datas.length && Math.min(...datas) <= ts) break;     // já passou do horário
        let novas = null;
        try {
          novas = await window.require('WAWebChatLoadMessages').loadEarlierMsgs({ chat });
        } catch (e) { break; }
        if (!novas || !novas.length) break;
        ids = achar();
      }
      return { ids, diag: `${todas().length} mensagem(ns) carregada(s)` };
    }, destino, Number(timestamp), !!fromMe);
    // Quem decide é o tipo + o horário mais próximo + o que ainda não está em
    // outra linha da conversa. Num álbum (várias fotos no mesmo segundo) a
    // primeira foto ia para todas as linhas; com empate, melhor não mostrar.
    const usados = new Set((excluir || []).map(String));
    const tipos = Object.prototype.hasOwnProperty.call(TIPOS_DO_ROTULO, tipo) ? TIPOS_DO_ROTULO[tipo] : null;
    const ts = Number(timestamp);
    // na ordem da conversa (a página devolve em ordem cronológica)
    const doTipo = (achado.ids || []).filter((c) => !tipos || tipos.includes(c.type));
    if (!doTipo.length) {
      registrar(`Mídia antiga não achada (${achado.diag})`);
      return null;
    }
    const melhor = Math.min(...doTipo.map((c) => Math.abs(c.t - ts)));
    const grupo = doTipo.filter((c) => Math.abs(c.t - ts) === melhor);
    let escolhida = null;
    if (grupo.length === 1) {
      escolhida = grupo[0];
    } else if (Number.isInteger(indice) && indice >= 0 && indice < grupo.length) {
      // álbum: várias no mesmo instante; a N-ésima linha do chat é a N-ésima da conversa
      escolhida = grupo[indice];
    } else {
      return { empate: true };
    }
    if (usados.has(escolhida.id)) return { empate: true };
    const mid = escolhida.id;
    const m = await naFilaDeMidia(() => baixarPeloId(mid));
    if (!m || !m.data) return null;
    if (Math.floor(m.data.length * 3 / 4) > MIDIA_MAX_BYTES) return { grande: true };
    return { mimetype: m.mimetype || '', filename: m.filename || '', data: m.data, message_id: mid };
  }

  return { id, client, estado, log, registrar, religar, resolverDestino,
           entregar, entregarArquivo, enfileirarLote, iniciar, encerrar, abrirPerfil,
           anexarMidia, baixarMidiaAntiga };
}

// ── Helpers globais ─────────────────────────────────────────────────────────
function comDDI(numero) {
  const d = soDigitos(numero);
  return d.startsWith('55') ? d : '55' + d;
}

// Só o material gerado pelo projeto pode ser enviado.
const OUT_DIR = path.resolve(__dirname, '..', '..', 'out');
const EXT_PERMITIDAS = new Set(['.pdf']);

function caminhoPermitido(caminho) {
  let alvo;
  try {
    alvo = path.resolve(String(caminho || ''));
  } catch (e) {
    return { ok: false, erro: 'caminho inválido' };
  }
  // `path.relative` resolve ".." antes da comparação, então "out/../.env" não
  // passa por parecer começar com o diretório certo
  const rel = path.relative(OUT_DIR, alvo);
  if (rel.startsWith('..') || path.isAbsolute(rel)) {
    return { ok: false, erro: `arquivo fora de out/: ${alvo}` };
  }
  if (!EXT_PERMITIDAS.has(path.extname(alvo).toLowerCase())) {
    return { ok: false, erro: `extensão não permitida: ${path.extname(alvo)}` };
  }
  return { ok: true, alvo };
}

// Avisa o dashboard do resultado de UM alvo do lote. Falha aqui não pode
// derrubar o envio dos demais: o Python reconcilia pelo log se isto se perder.
async function avisarResultado(alvo, r, conta) {
  if (!alvo || alvo.lead_id === undefined || alvo.lead_id === null) return;
  const url = (process.env.IA_URL || 'http://localhost:5020/api/conversa/responder')
    .replace('/api/conversa/responder', '/api/whatsapp/resultado');
  try {
    await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        lead_id: alvo.lead_id, numero: alvo.numero, etapa: alvo.etapa || '',
        ok: !!r.ok, erro: r.erro || '', conta: conta || null,
      }),
    });
  } catch (e) {
    console.log(`Não deu para avisar o resultado de ${alvo.numero}: ${e.message}`);
  }
}

// ── Ritmo do lote ───────────────────────────────────────────────────────────
// Piso de segurança para quando o pedido vier sem faixa (chamada antiga, curl na
// mão).
const INTERVALO_PADRAO = { min: 25, max: 60 };
const INTERVALO_TETO = 600;    // 10 min: valor absurdo no config travaria o lote

function sortearEspera(intervalo) {
  const i = intervalo || {};
  let min = Number(i.min);
  let max = Number(i.max);
  if (!isFinite(min) || min < 0) min = INTERVALO_PADRAO.min;
  if (!isFinite(max) || max < min) max = Math.max(min, INTERVALO_PADRAO.max);
  min = Math.min(min, INTERVALO_TETO);
  max = Math.min(max, INTERVALO_TETO);
  return Math.round((min + Math.random() * (max - min)) * 1000);
}

// ── Resolução da conta de cada requisição ───────────────────────────────────
/**
 * Qual conta atende esta requisição.
 *
 * Sem `conta` no pedido, cai na conta PADRÃO — é o que mantém funcionando todo
 * o Python que já existia antes do multi-conta. Se a padrão não estiver
 * pareada mas houver exatamente UMA conta pronta, usa ela: é o caso de quem
 * migrou o número para uma conta nomeada e não quer reescrever chamada nenhuma.
 */
function contaDe(req) {
  const bruto = (req.body && req.body.conta) || req.query.conta || '';
  const pedida = normalizarConta(bruto);

  if (pedida) {
    const c = contas.get(pedida);
    if (!c) return { erro: `conta ${pedida} não cadastrada`, codigo: 404 };
    return { conta: c };
  }

  const padrao = contas.get(CONTA_PADRAO);
  if (padrao && padrao.estado.pronto) return { conta: padrao };

  const prontas = [...contas.values()].filter((c) => c.estado.pronto);
  if (prontas.length === 1) return { conta: prontas[0] };
  if (prontas.length > 1) {
    return { erro: 'há mais de uma conta conectada: informe "conta" no pedido',
             codigo: 400, contas: prontas.map((c) => c.id || 'padrão') };
  }
  return { conta: padrao || null };
}

/** Resolve a conta e já recusa quando ela não pode enviar. */
function contaPronta(req, res) {
  const r = contaDe(req);
  if (r.erro) {
    res.status(r.codigo).json({ ok: false, erro: r.erro, contas: r.contas });
    return null;
  }
  if (!r.conta) {
    res.status(400).json({ ok: false, erro: 'nenhuma conta de WhatsApp cadastrada' });
    return null;
  }
  if (r.conta.estado.perfilAberto) {
    res.status(409).json({ ok: false,
                           erro: 'Feche a janela visível do WhatsApp antes de enviar por esta conta.' });
    return null;
  }
  if (!r.conta.estado.pronto) {
    res.status(400).json({ ok: false, erro: 'WhatsApp não conectado',
                           conta: r.conta.id || 'padrão' });
    return null;
  }
  return r.conta;
}

function resumo(c) {
  return {
    conta: c.id || null,
    padrao: c.id === CONTA_PADRAO,
    pronto: c.estado.pronto,
    numero: c.estado.numero,
    temQr: !!c.estado.qr,
    iniciado: c.estado.iniciado,
    erro: c.estado.ultimoErro,
    perfilAberto: c.estado.perfilAberto,
  };
}

// ── API ─────────────────────────────────────────────────────────────────────

app.get('/api/contas', (req, res) => {
  res.json({ contas: [...contas.values()].map(resumo), resposta });
});

app.post('/api/contas', (req, res) => {
  const id = normalizarConta((req.body || {}).conta);
  if (!id) return res.status(400).json({ ok: false, erro: 'informe "conta" (só dígitos do número)' });
  if (contas.has(id)) {
    const c = contas.get(id);
    // Conta já cadastrada e já esperando o scan: devolve o estado e NÃO toca no
    // cliente. Ver o QR é leitura; reabrir o pareamento aqui matava o código
    // que estava na tela do operador.
    if (c.estado.pronto) {
      return res.json({ ok: true, ja_existia: true, mensagem: 'já conectado', ...resumo(c) });
    }
    if (c.estado.iniciado && c.estado.qr) {
      return res.json({ ok: true, ja_existia: true,
                        mensagem: 'aguardando o QR ser escaneado', ...resumo(c) });
    }
    const r = c.iniciar(false);
    return res.json({ ok: true, ja_existia: true, ...r, ...resumo(c) });
  }
  const c = criarConta(id);
  contas.set(id, c);
  salvarContas();
  const r = c.iniciar(false);
  res.json({ ok: true, ...r, ...resumo(c) });
});

app.post('/api/contas/:conta/abrir-perfil', async (req, res) => {
  const pedido = normalizarConta(req.params.conta);
  if (pedido.length < 8 || pedido.length > 15) {
    return res.status(400).json({ ok: false, erro: 'telefone de conta inválido' });
  }
  const exata = contas.get(pedido);
  const coincidencias = exata ? [exata] : [...contas.values()].filter((c) => {
    const identificadores = [c.id, c.estado.numero].map(soDigitos).filter(Boolean);
    return identificadores.some((n) => n.length >= 8 && n.slice(-8) === pedido.slice(-8));
  });
  if (coincidencias.length > 1) {
    return res.status(409).json({ ok: false, erro: 'mais de uma conta corresponde a este telefone' });
  }
  const c = coincidencias[0];
  if (!c) return res.status(404).json({ ok: false, erro: 'conta WhatsApp não encontrada no bot' });
  const resultado = await c.abrirPerfil();
  res.status(resultado.ok ? 202 : 409).json(resultado);
});

app.delete('/api/contas/:conta', async (req, res) => {
  const id = normalizarConta(req.params.conta);
  const c = contas.get(id);
  if (!c) return res.status(404).json({ ok: false, erro: 'conta não cadastrada' });
  if (c.estado.perfilAberto) {
    return res.status(409).json({ ok: false,
                                  erro: 'Feche o navegador desta conta antes de removê-la.' });
  }
  if (id === CONTA_PADRAO) {
    return res.status(400).json({ ok: false, erro: 'a conta padrão não pode ser removida' });
  }
  await c.encerrar();
  contas.delete(id);
  salvarContas();
  res.json({ ok: true, mensagem: `conta ${id} desconectada e removida` });
});

app.get('/api/status', (req, res) => {
  const r = contaDe(req);
  if (r.erro) return res.status(r.codigo).json({ erro: r.erro, contas: r.contas });
  const c = r.conta;
  if (!c) {
    return res.json({ pronto: false, numero: null, temQr: false, iniciado: false,
                      erro: 'nenhuma conta cadastrada', log: [], resposta,
                      contas: [] });
  }
  res.json({ ...resumo(c), log: c.log.slice(0, 40), resposta,
             contas: [...contas.values()].map(resumo) });
});

app.get('/api/qr', (req, res) => {
  const r = contaDe(req);
  if (r.erro) return res.status(r.codigo).json({ erro: r.erro });
  res.json({ qr: r.conta ? r.conta.estado.qr : null,
             conta: r.conta ? (r.conta.id || null) : null });
});

app.post('/api/iniciar', (req, res) => {
  const r = contaDe(req);
  if (r.erro) return res.status(r.codigo).json({ ok: false, erro: r.erro });
  if (!r.conta) return res.status(400).json({ ok: false, erro: 'nenhuma conta cadastrada' });
  res.json(r.conta.iniciar((req.body || {}).forcar));
});

app.get('/api/resposta', (req, res) => res.json(resposta));

app.post('/api/resposta', (req, res) => {
  const { modo, permitidos } = req.body || {};
  if (modo !== undefined) {
    if (!['off', 'lista', 'todos'].includes(modo))
      return res.status(400).json({ ok: false, erro: 'modo inválido' });
    resposta.modo = modo;
  }
  if (permitidos !== undefined) {
    // separa só por linha/vírgula/ponto-e-vírgula: quebrar no espaço partia
    // "(82) 99962-0931" em dois pedaços e guardava o número truncado
    const lista = Array.isArray(permitidos) ? permitidos
                                            : String(permitidos).split(/[\n\r,;]+/);
    resposta.permitidos = lista.map(soDigitos).filter((n) => n.length >= 10);
  }
  salvarResposta();
  console.log(`Trava alterada: modo=${resposta.modo}, permitidos=${resposta.permitidos.length}`);
  res.json({ ok: true, ...resposta });
});

app.get('/api/verificar', async (req, res) => {
  const r = contaDe(req);
  if (r.erro) return res.json({ tem: null, erro: r.erro });
  if (!r.conta || !r.conta.estado.pronto) {
    return res.json({ tem: null, erro: 'WhatsApp não conectado' });
  }
  try {
    // getNumberId é a mesma resolução usada no envio: se ele acha o id, o
    // número tem WhatsApp E é entregável. Evita "verificado ok" seguido de
    // falha no envio.
    const id = await r.conta.resolverDestino(req.query.numero || '');
    res.json({ tem: !!id, numero: req.query.numero, id: id || null,
               conta: r.conta.id || null });
  } catch (e) {
    res.json({ tem: null, erro: e.message });
  }
});

// Mídia de uma mensagem já gravada no chat (o painel pede ao abrir a conversa).
// Só desta máquina: o arquivo é conversa de terceiros.
app.post('/api/midia', async (req, res) => {
  const ip = String(req.socket.remoteAddress || '');
  if (!['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(ip)) {
    return res.status(403).json({ ok: false, erro: 'só nesta máquina' });
  }
  const c = contaPronta(req, res);
  if (!c) return;
  const b = req.body || {};
  const pedido = { messageId: String(b.message_id || ''), numero: String(b.numero || ''),
                   timestamp: b.timestamp, fromMe: !!b.from_me, tipo: String(b.tipo || ''),
                   excluir: Array.isArray(b.excluir) ? b.excluir.slice(0, 500) : [],
                   indice: Number.isInteger(b.indice) ? b.indice : null };
  if (!pedido.messageId && !(pedido.numero && pedido.timestamp)) {
    return res.status(400).json({ ok: false, erro: 'informe message_id, ou numero + timestamp' });
  }
  try {
    const m = await c.baixarMidiaAntiga(pedido);
    if (m && m.empate) return res.status(409).json({ ok: false, empate: true,
      erro: 'duas mídias no mesmo instante: não dá para saber qual é desta mensagem' });
    if (m && m.grande) return res.status(413).json({ ok: false, grande: true, erro: 'mídia acima de 25 MB' });
    if (!m || !m.data) return res.status(404).json({ ok: false, erro: 'mensagem sem mídia disponível' });
    res.json({ ok: true, ...m });
  } catch (e) {
    if (e && (e.status === 404 || e.status === 410)) {
      return res.status(410).json({ ok: false, sumiu: true, erro: 'o WhatsApp não tem mais este arquivo' });
    }
    c.registrar('Mídia antiga: ' + String((e && (e.stack || e.message)) || e).slice(0, 600));
    res.status(502).json({ ok: false, erro: 'não consegui baixar: ' + (e && e.message) });
  }
});

app.post('/api/enviar', async (req, res) => {
  const c = contaPronta(req, res);
  if (!c) return;
  const { numero, mensagem } = req.body || {};
  if (!numero || !mensagem) return res.status(400).json({ ok: false, erro: 'numero e mensagem são obrigatórios' });
  const r = await c.entregar(numero, mensagem);
  if (r.ok) {
    c.registrar(`Enviado para ${numero} (${r.id})`);
    return res.json({ ok: true, id: r.id, conta: c.id || null, message_id: r.message_id || null });
  }
  c.registrar(`Falha ao enviar para ${numero}: ${r.erro}`);
  res.status(500).json({ ok: false, erro: r.erro, conta: c.id || null });
});

app.post('/api/enviar_arquivo', async (req, res) => {
  const c = contaPronta(req, res);
  if (!c) return;
  const { numero, caminho, legenda } = req.body || {};
  if (!numero || !caminho) {
    return res.status(400).json({ ok: false, erro: 'numero e caminho são obrigatórios' });
  }
  const r = await c.entregarArquivo(numero, caminho, legenda);
  if (r.ok) {
    c.registrar(`Arquivo enviado para ${numero} (${path.basename(caminho)})`);
    return res.json({ ok: true, id: r.id, conta: c.id || null });
  }
  c.registrar(`Falha ao enviar arquivo para ${numero}: ${r.erro}`);
  res.status(500).json({ ok: false, erro: r.erro });
});

app.post('/api/enviar_lote', async (req, res) => {
  const c = contaPronta(req, res);
  if (!c) return;
  const corpo = req.body || {};
  const alvos = corpo.alvos || [];
  if (!alvos.length) return res.status(400).json({ ok: false, erro: 'lista vazia' });

  const amostra = sortearEspera(corpo.intervalo);
  res.json({
    ok: true, total: alvos.length, mensagem: 'envio iniciado em segundo plano',
    conta: c.id || null,
    intervalo_seg: { min: (corpo.intervalo || INTERVALO_PADRAO).min,
                     max: (corpo.intervalo || INTERVALO_PADRAO).max },
    proximo_em_seg: Math.round(amostra / 1000),
  });

  c.enfileirarLote(alvos, corpo.intervalo);
});

/**
 * Diagnóstico: mostra o que o WhatsApp expõe de um contato/chat.
 * Usado para descobrir onde mora o telefone real quando o remetente chega
 * como @lid. Só leitura, não envia nada.
 */
app.get('/api/debug/contato', async (req, res) => {
  const r = contaDe(req);
  if (r.erro || !r.conta || !r.conta.estado.pronto) {
    return res.status(400).json({ erro: r.erro || 'não conectado' });
  }
  const id = req.query.id;
  if (!id) return res.status(400).json({ erro: 'informe ?id=...' });
  const saida = { id, conta: r.conta.id || null };
  try {
    const c = await r.conta.client.getContactById(id);
    saida.contato = {
      number: c.number, isUser: c.isUser, isWAContact: c.isWAContact,
      pushname: c.pushname, name: c.name,
      id: c.id, _data_keys: Object.keys(c._data || {}),
      _data: c._data,
    };
  } catch (e) { saida.erro_contato = e.message; }
  try {
    const chat = await r.conta.client.getChatById(id);
    saida.chat = { name: chat.name, id: chat.id, isGroup: chat.isGroup };
  } catch (e) { saida.erro_chat = e.message; }
  res.json(saida);
});

// Sessão derrubada + arquivo travado = processo morto.
process.on('uncaughtException', (e) => {
  const m = String((e && e.message) || e);
  console.log('Erro não tratado (o servidor continua no ar): ' + m.slice(0, 200));
  if (/EBUSY|lockfile|logout/i.test(m)) {
    console.log('Sessão precisa ser refeita: escaneie o QR em http://localhost:' + PORT);
  }
  if (/detached Frame|Execution context|Protocol error|Target closed/i.test(m)) {
    for (const c of contas.values()) {
      c.estado.ultimoErro = m.slice(0, 300);
      if (c.estado.iniciado) c.religar('erro de navegador: ' + m.slice(0, 60));
    }
  }
});
process.on('unhandledRejection', (e) => {
  console.log('Promessa rejeitada sem tratamento: ' + String((e && e.message) || e).slice(0, 200));
});

// ── Painel ──────────────────────────────────────────────────────────────────
app.get('/', (req, res) => res.sendFile(path.join(__dirname, 'painel.html')));

// ── Subida ──────────────────────────────────────────────────────────────────
/** Esta conta já tem sessão salva no disco? */
function temSessaoSalva(id) {
  const pasta = id ? path.join(SESSION_DIR, `session-${id}`)
                   : path.join(SESSION_DIR, 'session');
  try {
    return fs.existsSync(pasta) && fs.readdirSync(pasta).length > 0;
  } catch (e) {
    return false;
  }
}

contas.set(CONTA_PADRAO, criarConta(CONTA_PADRAO));
for (const id of lerContasSalvas()) {
  if (!contas.has(id)) contas.set(id, criarConta(id));
}

// Só nesta máquina: a guarda de Host/Origem barra navegador, não um `curl` da
// rede — e por aqui se envia mensagem e se apaga conta. Quem fala com o bot
// (painel, scripts) está no mesmo computador.
app.listen(PORT, '127.0.0.1', () => {
  console.log('='.repeat(56));
  console.log('  Bot de WhatsApp do LeadForge (multi-conta)');
  console.log(`  Painel: http://localhost:${PORT}`);
  console.log(`  Contas: ${[...contas.keys()].map((k) => k || 'padrão').join(', ')}`);
  console.log('='.repeat(56));

  // Sobe só quem JÁ TEM sessão salva.
  let subiram = 0;
  for (const c of contas.values()) {
    if (temSessaoSalva(c.id)) {
      c.iniciar(false);
      subiram++;
    } else {
      c.registrar('sem sessão salva: aguardando o pedido de pareamento');
    }
  }
  if (!subiram) {
    console.log('  Nenhuma conta pareada. Conecte uma pela aba Contas do dashboard,');
    console.log(`  ou: curl -X POST http://localhost:${PORT}/api/contas -H "Content-Type: application/json" -d "{\"conta\":\"55...\"}"`);
  }
});
