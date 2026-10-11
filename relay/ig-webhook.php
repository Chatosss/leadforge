<?php
/**
 * Relé de webhooks do Instagram (API oficial) para o painel LeadForge — v023.
 *
 * POR QUE EXISTE: a Meta entrega a mensagem do lead num endereço HTTPS público,
 * e o painel roda num PC sem endereço público (e às vezes desligado). Este
 * arquivo fica na hospedagem, sempre no ar: confere a assinatura da Meta e
 * guarda o corpo EXATO numa fila FORA do site. O painel puxa a fila e confere
 * a assinatura de novo antes de gravar.
 *
 * Três conversas, e só três:
 *   GET  ?hub.mode=subscribe&hub.verify_token=..&hub.challenge=..   verificação da Meta
 *   POST (corpo da Meta, cabeçalho X-Hub-Signature-256)               notificação
 *   POST ?acao=puxar | ?acao=confirmar  (JSON com o token do painel)  painel lendo a fila
 *
 * Segredos e fila ficam em ../leadforge-dados (ao lado de public_html), como na
 * central de assinaturas. Se essa pasta não existir, o relé NÃO cai para dentro
 * do site: mensagem de lead servida como arquivo público seria pior do que ficar
 * fora do ar (a Meta reentrega por 36h).
 */
declare(strict_types=1);

ini_set('display_errors', '0');
header('Cache-Control: no-store, private, max-age=0');
header('X-Content-Type-Options: nosniff');

const MAX_CORPO = 1048576;   // 1 MB por notificação
const MAX_FILA  = 5000;      // acima disto recusa e a Meta tenta de novo depois
const MAX_LOTE  = 100;

function sair(int $status, array $dados): void {
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($dados, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

$raiz  = rtrim(str_replace('\\', '/', (string)($_SERVER['DOCUMENT_ROOT'] ?? __DIR__)), '/');
$DADOS = dirname($raiz) . '/leadforge-dados';
$FILA  = $DADOS . '/ig-fila';
$cfg   = @json_decode((string)@file_get_contents($DADOS . '/ig-webhook.json'), true);
if (!is_array($cfg) || empty($cfg['app_secret']) || empty($cfg['verify_token'])
        || empty($cfg['token_pull'])) {
    sair(503, ['ok' => false, 'erro' => 'relé não configurado']);
}

$metodo = $_SERVER['REQUEST_METHOD'] ?? 'GET';

// ── 1. Verificação do webhook (o PHP troca "." por "_" nos nomes) ─────────────
if ($metodo === 'GET') {
    $modo    = (string)($_GET['hub_mode'] ?? '');
    $tok     = (string)($_GET['hub_verify_token'] ?? '');
    $desafio = (string)($_GET['hub_challenge'] ?? '');
    if ($modo === 'subscribe' && $tok !== '' && hash_equals((string)$cfg['verify_token'], $tok)
            && preg_match('/^[A-Za-z0-9_-]{1,128}$/', $desafio)) {
        http_response_code(200);
        header('Content-Type: text/plain; charset=utf-8');
        echo $desafio;
        exit;
    }
    sair(403, ['ok' => false]);
}
if ($metodo !== 'POST') {
    sair(405, ['ok' => false]);
}

$corpo = (string)file_get_contents('php://input', false, null, 0, MAX_CORPO + 1);
if (strlen($corpo) > MAX_CORPO) {
    sair(413, ['ok' => false]);
}
$acao = (string)($_GET['acao'] ?? '');

// ── 2. Notificação da Meta: assinatura conferida, corpo guardado sem mexer ────
if ($acao === '') {
    $assinatura = (string)($_SERVER['HTTP_X_HUB_SIGNATURE_256'] ?? '');
    $esperada = 'sha256=' . hash_hmac('sha256', $corpo, (string)$cfg['app_secret']);
    if ($assinatura === '' || !hash_equals($esperada, $assinatura)) {
        sair(403, ['ok' => false]);
    }
    if (!is_dir($FILA) && !@mkdir($FILA, 0700, true)) {
        sair(500, ['ok' => false]);
    }
    if (count(glob($FILA . '/*.json') ?: []) >= MAX_FILA) {
        sair(507, ['ok' => false]);
    }
    // nome = milissegundos + sorteio: ordena por chegada e não colide
    $id  = sprintf('%013d-%s', (int)floor(microtime(true) * 1000), bin2hex(random_bytes(6)));
    $reg = json_encode(['id' => $id, 'recebido_em' => gmdate('c'),
                        'assinatura' => $assinatura, 'corpo_b64' => base64_encode($corpo)]);
    $tmp = $FILA . '/' . $id . '.tmp';
    if ($reg === false || @file_put_contents($tmp, $reg, LOCK_EX) === false
            || !@rename($tmp, $FILA . '/' . $id . '.json')) {
        @unlink($tmp);
        sair(500, ['ok' => false]);
    }
    sair(200, ['ok' => true]);
}

// ── 3. Painel puxando ou confirmando a fila ───────────────────────────────────
$pedido = json_decode($corpo, true);
$token  = is_array($pedido) ? ($pedido['token'] ?? null) : null;
if (!is_string($token) || $token === '' || !hash_equals((string)$cfg['token_pull'], $token)) {
    usleep(300000);   // tentativa de adivinhar o token custa caro
    sair(403, ['ok' => false]);
}

if ($acao === 'puxar') {
    $arquivos = glob($FILA . '/*.json') ?: [];
    sort($arquivos, SORT_STRING);
    $max = max(1, min(MAX_LOTE, (int)($pedido['max'] ?? 50)));
    $eventos = [];
    foreach (array_slice($arquivos, 0, $max) as $arq) {
        $d = json_decode((string)@file_get_contents($arq), true);
        if (is_array($d)) {
            $eventos[] = $d;
        }
    }
    sair(200, ['ok' => true, 'eventos' => $eventos,
               'restantes' => max(0, count($arquivos) - count($eventos))]);
}

if ($acao === 'confirmar') {
    $ids = $pedido['ids'] ?? [];
    if (!is_array($ids)) {
        sair(400, ['ok' => false]);
    }
    $apagados = 0;
    foreach (array_slice($ids, 0, MAX_LOTE * 2) as $id) {
        // só o formato que este arquivo gera: nada de "../" chegando ao unlink
        if (is_string($id) && preg_match('/^\d{13}-[0-9a-f]{12}$/', $id)
                && @unlink($FILA . '/' . $id . '.json')) {
            $apagados++;
        }
    }
    sair(200, ['ok' => true, 'apagados' => $apagados]);
}

sair(400, ['ok' => false]);
