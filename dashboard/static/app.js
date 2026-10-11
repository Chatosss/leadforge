// ── Estado global ─────────────────────────────────────────────────────────────
let KCOLS = [], CANAIS = [], FERR = {}, LEADS = [];
// Estado dos módulos novos. Fica AQUI, no topo, e não junto das funções que o
// usam lá embaixo: `boot()` roda antes do fim do arquivo, e `let` declarado
// depois ainda está na zona morta temporal — o painel abria com a tela de
// carregamento estampando "Cannot access 'BOOT_FEITO' before initialization".
let QUADROS = [], QUADRO_ATIVO = null, QUADRO_EDITANDO = null;
let COL_EDITANDO = null, COL_ARRASTANDO = null;
const COLS_OBRIGATORIAS = ['lista','entrar_contato','contato_iniciado','problema','respondeu'];
let BKP_ARQUIVO = null;
const BOOT_PASSOS = 5;
let BOOT_FEITO = 0;
let COL_COR = {}, COL_LABEL = {};
let SELECTED = new Set();
let TPLS_TODOS = [];
let ESTEIRA = {leads:{}, etapas:[], info:{}};
let ETAPAS_ESTEIRA = [];
let CHAT_CHANNEL = '', CHAT_THREADS = [], CHAT_SELECTED = null, CHAT_TIMER = null;
let AQ_TIMER = null, AQ_STATE = {accounts:[],active:[],online:false};
let IGD_TIMER = null, IGD_STATE = {contas:[],active:[],online:false};

// ── Helpers ───────────────────────────────────────────────────────────────────
const $ = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];
async function jget(u){ const r = await fetch(u); return r.json(); }
async function jsend(u, m, b){
  const r = await fetch(u,{method:m,headers:{'Content-Type':'application/json'},
    body:b?JSON.stringify(b):null});
  const contentType = r.headers.get('content-type') || '';
  if(!contentType.toLowerCase().includes('application/json')){
    // Serviços Flask/Express antigos devolvem HTML no 404. Tentar JSON.parse
    // escondia a causa com "Unexpected token '<'" e não dizia que o serviço
    // precisava carregar a rota nova.
    const detalhe = r.status===404
      ? `Rota indisponível (HTTP 404). Reinicie o serviço para carregar a versão atual: ${u}`
      : `Resposta inválida do servidor (HTTP ${r.status}).`;
    return {ok:false,erro:detalhe};
  }
  const dados = await r.json();
  if(!r.ok) return {...dados,ok:false,erro:dados.erro||`Falha HTTP ${r.status}.`};
  return dados;
}
function toast(msg){ const t=$('#toast'); t.textContent=msg; t.classList.add('show');
  clearTimeout(t._t); t._t=setTimeout(()=>t.classList.remove('show'),2600); }
// Escapa também aspas: `esc` é usado dentro de value="..." (contatos, e agora o
// nome da clínica).
function esc(s){ return (s??'').toString().replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function scoreCor(s){ if(s==null)return '#94a3b8'; if(s>=60)return '#dc2626'; if(s>=40)return '#d97706'; if(s>=20)return '#2563eb'; return '#16a34a'; }
function money(n){ return 'R$ '+(Math.round(n||0)).toLocaleString('pt-BR'); }
function waPill(l){ if(l.tem_whatsapp===1)return '<span class="wa-pill wa-sim">Zap ✓</span>';
  if(l.tem_whatsapp===0)return '<span class="wa-pill wa-nao">sem Zap</span>'; return ''; }
function psInfo(l){ if(!l.pagespeed_json)return null; try{return JSON.parse(l.pagespeed_json);}catch(e){return null;} }
function psPill(l){ const d=psInfo(l); if(!d||d.score==null)return '';
  const c=d.score>=90?'#16a34a':d.score>=50?'#d97706':'#dc2626';
  return `<span class="ps-pill" style="background:${c}">site ${d.score}</span>`; }

// ── Bootstrap ─────────────────────────────────────────────────────────────────
async function boot(){
  // Rede de segurança: se um passo travar (bot fora do ar respondendo devagar,
  // rota pendurada), a tela sai sozinha. Prender o operador numa tela de
  // carregamento é pior do que mostrar o painel com um painel vazio.
  const destravar = setTimeout(()=>{
    const p=document.getElementById('boot-passo');
    if(p) p.textContent='demorou mais que o esperado — abrindo assim mesmo';
    bootFim();
  }, 15000);
  try{
    bootPasso('lendo colunas e quadros...');
    const b = await jget('/api/bootstrap');
    KCOLS = b.kanban_cols; CANAIS = b.canais; FERR = b.ferramentas;
    ETAPAS_ESTEIRA = b.esteira_etapas || [];
    KCOLS.forEach(c=>{ COL_COR[c.key]=c.cor; COL_LABEL[c.key]=c.label; });
    QUADROS = b.quadros || []; QUADRO_ATIVO = b.quadro_ativo;

    bootPasso('montando os seletores...');
    popularSelects();
    quadrosRender();

    bootPasso('carregando os leads...');
    await recarregarLeads();

    bootPasso('conferindo a visão geral...');
    await vgCarregar().catch(()=>{});


    bootPasso('pronto');
    clearTimeout(destravar);
    bootFim();
  }catch(e){
    clearTimeout(destravar);
    bootErro('Não deu para carregar: '+(e && e.message ? e.message : e)
             + ' — o painel abre assim mesmo.');
  }
}

function renderKPIs(s){}

function popularSelects(){
  // prospecção canal (só ferramentas ativas)
  const pc=$('#pros-canal'); pc.innerHTML='';
  CANAIS.filter(c=>FERR[c]).forEach(c=>pc.append(new Option(c,c)));
  // coluna filtro
  const col=$('#pros-coluna'); col.innerHTML='<option value="">Todas</option>';
  KCOLS.forEach(c=>col.append(new Option(c.label,c.key)));
  // coluna destino da barra de seleção em massa
  const bc=$('#bulk-col'); if(bc){ bc.innerHTML=''; KCOLS.forEach(c=>bc.append(new Option(c.label,c.key))); }
  // template canal
  const tc=$('#tpl-canal'); tc.innerHTML='';
  CANAIS.forEach(c=>tc.append(new Option(c,c)));
  // etapa da esteira: só existe para WhatsApp. Nos outros canais o campo some,
  // senão sugere uma automação que aquele canal não tem.
  const te=$('#tpl-etapa');
  if(te){
    te.innerHTML='';
    te.append(new Option('(avulso — entra no rodízio de "Entrar em contato")',''));
    ETAPAS_ESTEIRA.forEach(e=>te.append(new Option(e.rotulo, e.etapa)));
    te.onchange=()=>{
      const e=ETAPAS_ESTEIRA.find(x=>x.etapa===te.value);
      $('#tpl-etapa-uso').textContent = e ? e.uso
        : 'Sem etapa: o template entra no rodízio do primeiro contato.';
    };
  }
  tc.onchange=()=>{
    const eh = tc.value==='email', wa = tc.value==='whatsapp';
    $('#tpl-assunto-l').style.display = eh?'block':'none';
    $('#tpl-assunto').style.display = eh?'block':'none';
    $('#tpl-etapa-l').style.display = wa?'block':'none';
    $('#tpl-etapa').style.display = wa?'block':'none';
    $('#tpl-etapa-uso').style.display = wa?'block':'none';
    if(wa) $('#tpl-etapa').onchange();
  };
  tc.onchange();
}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function switchTab(name){
  $$('.nav-link').forEach(l=>l.classList.toggle('active',l.dataset.tab===name));
  $$('.pane').forEach(p=>p.classList.toggle('active',p.id==='pane-'+name));
  if(TAB_LOADERS[name]) TAB_LOADERS[name]();
}
function switchSubtab(prefix, subtabName){
  $$('#subtabs-'+prefix+' .subtab').forEach(t=>t.classList.remove('active'));
  $$('.subpane[id^="subpane-'+prefix+'-"]').forEach(p=>p.classList.remove('active'));
  const tabBtn=$$('#subtabs-'+prefix+' .subtab').find(t=>t.textContent.toLowerCase().includes(subtabName.substring(0,4)));
  if(tabBtn)tabBtn.classList.add('active');
  const pane=$('#subpane-'+prefix+'-'+subtabName);
  if(pane)pane.classList.add('active');
  if(prefix==='ig')igLoadSubpane(subtabName);
  if(prefix==='email')emailLoadSubpane(subtabName);
  if(prefix==='wa')waLoadSubpane(subtabName);
  if(prefix==='chat')chatLoadSubpane(subtabName);
}

// ── KANBAN ─────────────────────────────────────────────────────────────────────
async function recarregarLeads(){
  LEADS = await jget('/api/leads');
  await carregarEsteira();
  renderBoard();
}

// ── Esteira de WhatsApp ─────────────────────────────────────────────────────
async function carregarEsteira(){
  try{
    const d = await jget('/api/esteira');
    ESTEIRA.etapas = d.etapas||[];
    ESTEIRA.info = {enviados_hoje:d.enviados_hoje, vagas:d.vagas_hoje,
                    janela:d.janela, dentro:d.dentro_da_janela};
    ESTEIRA.leads = {};
    (d.leads||[]).forEach(l=>{ ESTEIRA.leads[l.id]=l; });
  }catch(e){ ESTEIRA.leads={}; }
}

// "vence 13/08 18:47" vira "em 6h" — o operador quer saber se e hoje, nao a data
function quandoVence(iso){
  if(!iso) return '';
  const t = new Date(iso.replace(' ','T'));
  const dif = t - new Date();
  if(dif <= 0) return 'agora';
  const h = Math.round(dif/3600000);
  if(h < 24) return `em ${h}h`;
  return `em ${Math.round(h/24)}d`;
}

// Em qual campanha o lead entrou, direto no card da coluna. Sem isto, saber a
// campanha de um lead exigia abrir o card um por um — e é a campanha que
// explica por que dois leads receberam textos diferentes.
function seloCampanha(l){
  if(!l.campanha_id) return '';
  const c = (CAMPANHAS||[]).find(x=>x.id===l.campanha_id);
  if(!c) return '';
  const e = ESTEIRA.leads[l.id];
  const passo = e ? ` ${e.passo}/${e.total}` : '';
  return `<div class="tpl-etapa" style="background:var(--muted);margin-bottom:6px"
    title="campanha do lead">🎯 ${esc(c.nome)}${passo}</div>`;
}

function seloEsteira(l){
  const e = ESTEIRA.leads[l.id];
  if(!e) return '';
  if(e.concluida) return '<span class="esteira-selo fim">esteira concluída</span>';
  if(e.sem_texto) return '<span class="esteira-selo fim" title="sem campanha ou '
    + 'sequência esgotada: nada será enviado">régua parada</span>';
  const cor = e.pronto ? 'pronto' : '';
  return `<span class="esteira-selo ${cor}" title="próximo: ${esc(e.proxima_rotulo)}">`
    + `${e.passo}/${e.total} · ${esc(e.proxima_rotulo.replace(/^\d+ · /,''))}`
    + (e.pronto ? ' · agora' : ` · ${quandoVence(e.vence_em)}`) + '</span>';
}

function resumoEsteiraHTML(leads){
  const porEtapa = {};
  let prontos = 0, concluidas = 0, parados = 0;
  leads.forEach(l=>{
    const e = ESTEIRA.leads[l.id];
    if(!e) return;
    if(e.concluida){ concluidas++; return; }
    // sem texto para a próxima posição, este lead não sai: contar como pronto
    // faria o botão prometer envios que a rodada vai pular
    if(e.sem_texto){ parados++; return; }
    const k = e.proxima_rotulo || '?';
    porEtapa[k] = (porEtapa[k]||0) + 1;
    if(e.pronto) prontos++;
  });
  const linhas = Object.entries(porEtapa)
    .map(([k,n])=>`<div class="esteira-linha"><span>${esc(k)}</span><b>${n}</b></div>`)
    .join('');
  const i = ESTEIRA.info || {};
  const janela = i.dentro ? '' :
    `<div class="esteira-aviso">fora do horário de envio (${(i.janela||[])[0]}h-${(i.janela||[])[1]}h)</div>`;
  return `<div class="esteira-titulo">Esteira · próximo toque</div>`
    + (linhas || '<div class="esteira-linha muted">nenhum toque pendente</div>')
    + (concluidas?`<div class="esteira-linha muted"><span>concluídas</span><b>${concluidas}</b></div>`:'')
    + (parados?`<div class="esteira-linha muted"><span>sem campanha (parados)</span><b>${parados}</b></div>`:'')
    + janela
    + `<div class="esteira-rodape">${i.enviados_hoje||0} enviados hoje`
    + (i.vagas!=null ? ` · ${i.vagas} vaga(s) nas contas` : '')
    + `</div>`
    // o botao nao pode oferecer uma acao que sera recusada: fora da janela ou
    // sem vaga nas contas ele vira aviso em vez de convite
    + (prontos && i.dentro && i.vagas!==0
        ? `<button class="esteira-btn" onclick="dispararCadencia()">Enviar ${prontos} toque(s) vencido(s)</button>`
        : (prontos
            ? `<div class="esteira-aviso">${prontos} toque(s) vencido(s), esperando `
              + (!i.dentro ? 'o horário de envio' : 'vaga nas contas (limite de hoje atingido)') + '</div>'
            : ''));
}

// Nome com sufixo `Cadencia` de propósito: já existia `rodarEsteira(id)` para a
// esteira de ENRIQUECIMENTO (apurar, diagnóstico, proposta, material).
async function dispararCadencia(){
  // sempre mostra o que sairia ANTES de mandar: a esteira dispara para clinica
  // real, e o projeto nao manda nada sem o operador ver o que vai sair
  const previa = await jsend('/api/esteira/rodar','POST',{seco:true});
  const vao = previa.enviados||[];
  if(!vao.length){ toast(previa.motivo||'Nenhum toque vencido'); return; }
  const q = String.fromCharCode(10);
  const lista = vao.map(e=>`• ${e.nome||('lead '+e.lead_id)} — ${e.etapa}`).join(q);
  const r = await jsend('/api/esteira/rodar','POST',{seco:false});
  await recarregarLeads();
  const ok=(r.enviados||[]).length, pulados=(r.pulados||[]);
  toast(ok?`${ok} toque(s) enviados`:(r.motivo||'Nada saiu'));
  if(pulados.length){
    alert([`${pulados.length} não saíram:`,'',
      pulados.map(p=>`• ${p.nome||p.lead_id}: ${p.motivo}`).join(q)].join(q));
  }
}

// Bloco da cadência no detalhe do lead: em que passo está e o botão de mandar o
// próximo toque só dele.
function campanhaDoLeadHtml(l){
  const e = l.esteira || {};
  const camp = l.campanha;
  const opcoes = (CAMPANHAS||[]).map(c=>
    `<option value="${c.id}" ${camp && camp.id===c.id?'selected':''}>${esc(c.nome)}`
    + `${c.ativa?'':' (desativada)'}</option>`).join('');
  const passos = [];
  for(let o=0; o<(e.total||CAMP_MAX); o++){
    const rot = o===0 ? 'Abertura' : ('Reforço '+o);
    const passo = CAMP_PASSOS[o] || {};
    passos.push(`<option value="${o}" ${e.proxima_ordem===o?'selected':''}>`
      + `${rot} (dia ${passo.dia!=null?passo.dia:'?'})</option>`);
  }
  passos.push(`<option value="${e.total||CAMP_MAX}" ${e.concluida?'selected':''}>`
    + `Concluída — não mandar mais</option>`);

  const situacao = camp
    ? `Na campanha <b>${esc(camp.nome)}</b>${camp.ativa?'':' <span class="muted">(desativada: segue valendo para quem já está dentro)</span>'}`
    : '<span class="muted">Sem campanha. Sem campanha não há de onde tirar o texto dos reforços, então ele não recebe mais nada da régua.</span>';
  const andamento = e.concluida
    ? `sequência concluída (${e.total} de ${e.total})`
    : `passo ${e.passo} de ${e.total} · próxima: <b>${esc(e.proxima_rotulo||'—')}</b>`
      + (e.vence_em ? ` (${e.pronto?'vencida':'vence '+quandoVence(e.vence_em)})` : '');

  return `<div class="cadencia-box">
    <b>Campanha:</b> ${situacao}<br>
    <span class="s">${andamento}</span>
    <div class="row" style="margin-top:8px">
      <div><label>Campanha</label>
        <select id="d-campanha">
          <option value="">— sem campanha —</option>${opcoes}
        </select></div>
      <div><label>Próxima mensagem</label>
        <select id="d-passo">${passos.join('')}</select></div>
    </div>
    <div class="row" style="gap:6px">
      <button class="btn btn-sm btn-primary" style="flex:0 0 auto"
        onclick="salvarCampanhaDoLead(${l.id})">Salvar campanha e passo</button>
      ${camp?`<button class="btn btn-sm btn-danger" style="flex:0 0 auto"
        onclick="tirarDaCampanha(${l.id})">Tirar da campanha</button>`:''}
    </div>
    <p class="hint">Adiantar o passo marca os toques anteriores como vencidos
      <b>sem enviar nada</b> — eles não contam no teto do dia nem nas métricas.
      Voltar para antes de um toque que já saiu é recusado: a clínica leu aquilo.</p>
  </div>`;
}

async function salvarCampanhaDoLead(id){
  const cid = $('#d-campanha').value;
  const r = await jsend(`/api/leads/${id}/campanha`,'PATCH',
    {campanha_id: cid === '' ? null : +cid, ordem: +$('#d-passo').value});
  if(r && r.erro){ toast(r.erro); return; }
  toast('Campanha e passo atualizados');
  await recarregarLeads(); abrirLead(id);
}

async function tirarDaCampanha(id){
  const r = await jsend(`/api/leads/${id}/campanha`,'PATCH',{campanha_id:null});
  if(r && r.erro){ toast(r.erro); return; }
  toast('Lead fora da campanha: não recebe mais reforço');
  await recarregarLeads(); abrirLead(id);
}

function cadenciaHtml(id, lead){
  const e = (lead && lead.esteira) || ESTEIRA.leads[id];
  if(!e) return '';
  if(e.concluida){
    return `<div class="cadencia-box"><b>Cadência de WhatsApp:</b> concluída
      (${e.total} de ${e.total} toques).</div>`;
  }
  // sem texto para a próxima posição nada sai — e o botão não pode oferecer uma
  // ação que será recusada.
  if(e.sem_texto){
    return `<div class="cadencia-box">
      <b>Cadência de WhatsApp:</b> parada no passo ${e.passo} de ${e.total}.
      <div class="s" style="margin-top:4px">Não há mensagem para
        <b>${esc(e.proxima_rotulo||'a próxima posição')}</b>: o lead está fora de
        campanha, ou a sequência da campanha dele terminou. Nada será enviado.</div>
    </div>`;
  }
  const quando = e.pronto ? '<b>vencido, pode enviar agora</b>'
                          : `vence ${quandoVence(e.vence_em)} (${esc(e.vence_em||'')})`;
  return `<div class="cadencia-box">
    <b>Cadência de WhatsApp:</b> passo ${e.passo} de ${e.total}.
    Próximo: <b>${esc(e.proxima_rotulo)}</b>, ${quando}.
    <div style="margin-top:8px">
      <button class="btn btn-sm btn-primary" onclick="enviarToqueLead(${id})">
        💬 Enviar o próximo toque</button>
    </div>
  </div>`;
}

async function excluirAnexo(attId, leadId){
  // Sem confirmação: o material é regerado sob demanda pelo botão "Só o material
  // (PDF)", então apagar não perde trabalho.
  const r = await jsend('/api/attachments/'+attId,'DELETE');
  if(r && r.erro){ toast(r.erro); return; }
  toast(r.arquivo_apagado ? 'Material excluído' :
        (r.compartilhado ? 'Removido do lead (o arquivo é usado por outro)' : 'Removido'));
  await recarregarLeads();
  abrirLead(leadId);                       // reabre para mostrar a lista já sem ele
}

// Envio de UM lead, fora do lote. O botão da coluna dispara todo mundo que
// venceu; aqui o operador adianta uma clínica específica sem mexer nas outras.
async function enviarToqueLead(leadId, forcar){
  const previa = await jsend('/api/esteira/lead/'+leadId,'POST',{seco:true});
  if(!previa || !previa.ok){ toast((previa&&previa.erro)||'Não deu para montar a prévia'); return; }

  const aviso = previa.pronto ? ''
    : 'Este toque ainda NÃO venceu (previsto para ' + (previa.vence_em||'?') + ').';
  const q = String.fromCharCode(10);
  const corpo = previa.previa
    ? [`Enviar "${previa.rotulo||previa.etapa}" para este lead?`, '', previa.previa, '',
       aviso, aviso?'':null, 'Mensagem enviada não tem desfazer.'].filter(x=>x!==null).join(q)
    : [aviso, '', 'Enviar o próximo toque assim mesmo?'].join(q);

  const r = await jsend('/api/esteira/lead/'+leadId,'POST',
                        {ignorar_prazo: true});
  await recarregarLeads();
  if(r && r.ok) toast(`Enviado: ${r.rotulo||r.etapa}`);
  else toast((r && r.erro) || 'Não saiu');
}

function matchBusca(l,q){
  if(!q) return true;
  const hay=[l.nome,l.categoria,l.nicho,l.telefone,l.email,l.instagram,l.endereco,l.razao_social,l.cnpj]
    .map(x=>(x||'').toString().toLowerCase()).join(' | ');
  return q.split(/\s+/).every(t=>hay.includes(t));
}
function ordenarColuna(leads,key){
  if(key==='lista') return leads.sort((a,b)=>(b.score??-1)-(a.score??-1)); // lista: sempre por nota
  return leads.sort((a,b)=>((a.kanban_ordem??0)-(b.kanban_ordem??0)) || ((b.score??-1)-(a.score??-1)));
}
function getDragAfter(body,y){
  const els=[...body.querySelectorAll('.lcard:not(.dragging)')];
  return els.reduce((closest,child)=>{
    const box=child.getBoundingClientRect(); const offset=y-box.top-box.height/2;
    if(offset<0 && offset>closest.offset) return {offset,el:child};
    return closest;
  },{offset:-Infinity}).el;
}
function renderBoard(){
  const q = ($('#kanban-search').value||'').toLowerCase().trim();
  const board = $('#board'); board.innerHTML='';
  let totalVis = 0;
  KCOLS.forEach(col=>{
    let leads = LEADS.filter(l=>(l.kanban||'lista')===col.key && matchBusca(l,q));
    ordenarColuna(leads,col.key);
    totalVis += leads.length;
    const el = document.createElement('div'); el.className='col';
    el.innerHTML = `<div class="col-head" style="border-top-color:${col.cor}" title="arraste para reordenar">
        <span>${col.label}</span>
        <span class="col-acoes">
          <button class="col-sel" data-col="${col.key}" title="selecionar a coluna inteira">todos</button>
          <span class="col-count">${leads.length}</span>
          <button class="col-menu" data-col="${col.key}" title="renomear, recolorir ou excluir">⋯</button>
        </span></div>
        <div class="col-body" data-col="${col.key}"></div>`;
    const btnMenu = $('.col-menu', el);
    if(btnMenu) btnMenu.onclick = ev => { ev.stopPropagation(); colAbrirEditor(col.key); };
    if(COL_EDITANDO === col.key) $('.col-head', el).after(colEditorEl(col));
    ligarArrasteDaColuna(el, col);
    const body = $('.col-body', el);
    // o botao da coluna nao pode abrir lead nem arrastar: e acao do cabecalho
    const btnSel = $('.col-sel', el);
    if(btnSel){
      const todosJa = leads.length>0 && leads.every(l=>SELECTED.has(l.id));
      btnSel.textContent = todosJa ? 'nenhum' : 'todos';
      btnSel.onclick = ev => { ev.stopPropagation(); toggleColuna(col.key); };
    }
    // A coluna "Entrar em contato" e uma FILA: o disparo e um clique explicito,
    // nunca o arrastar do card.
    if(col.key==='contato_iniciado' && leads.length){
      const p=document.createElement('div');
      p.className='esteira-painel';
      p.innerHTML=resumoEsteiraHTML(leads);
      $('.col-head', el).after(p);
    }
    if(col.key==='entrar_contato' && leads.length){
      const acao=document.createElement('button');
      acao.className='col-acao-enviar';
      acao.textContent=`Enviar WhatsApp (${leads.length})`;
      acao.title='Manda o primeiro contato e move para Contato iniciado';
      acao.onclick=ev=>{ ev.stopPropagation(); enviarEntrarContato(); };
      $('.col-head', el).after(acao);
    }
    leads.forEach(l=>body.append(cardEl(l)));
    // arrastar-e-soltar em TODA a coluna + reordenar dentro dela (menos 'lista')
    body.ondragover=e=>{ if(COL_ARRASTANDO) return;
      e.preventDefault(); body.classList.add('drag-over');
      const dragging=document.querySelector('.lcard.dragging'); if(!dragging) return;
      if(col.key==='lista') return; // lista continua por nota, sem reordenar manual
      const after=getDragAfter(body,e.clientY);
      if(after==null) body.appendChild(dragging); else body.insertBefore(dragging,after);
    };
    body.ondragleave=e=>{ if(!body.contains(e.relatedTarget)) body.classList.remove('drag-over'); };
    body.ondrop=async e=>{ if(COL_ARRASTANDO) return;
      e.preventDefault(); e.stopPropagation(); body.classList.remove('drag-over');
      const id=+e.dataTransfer.getData('id'); const lead=LEADS.find(x=>x.id===id); if(!lead) return;
      lead.kanban=col.key;
      if(col.key==='lista'){
        await jsend(`/api/leads/${id}/kanban`,'POST',{kanban:'lista'});
      } else {
        const ids=[...body.querySelectorAll('.lcard')].map(c=>+c.dataset.id);
        if(!ids.includes(id)) ids.push(id);
        ids.forEach((lid,i)=>{const l=LEADS.find(x=>x.id===lid); if(l){l.kanban=col.key; l.kanban_ordem=i;}});
        await jsend('/api/kanban/reorder','POST',{kanban:col.key, ids});
      }
      renderBoard(); atualizarStats();
    };
    board.append(el);
  });
  // criar coluna direto no quadro, no lugar onde ela vai aparecer
  const mais = document.createElement('button');
  mais.className = 'col-fantasma';
  mais.textContent = '+ nova coluna';
  mais.onclick = colCriarRapida;
  board.append(mais);
  const kc=$('#kanban-count'); if(kc) kc.textContent = q?`${totalVis} resultado(s) para "${q}"`:'';
}

function cardEl(l){
  const c = document.createElement('div'); c.className='lcard'+(SELECTED.has(l.id)?' sel':'');
  c.draggable=true; c.dataset.id=l.id;
  c.ondragstart=e=>{e.dataTransfer.setData('id',l.id);e.dataTransfer.effectAllowed='move';c.classList.add('dragging');};
  c.ondragend=()=>c.classList.remove('dragging');
  // Ctrl/Cmd+clique seleciona em vez de abrir; Shift+clique pega o intervalo
  // desde o ultimo clicado na MESMA coluna. Sem isso, montar um lote de 8 leads
  // exigia acertar 8 caixinhas de 14px.
  c.onclick=e=>{
    if(e.target.classList.contains('card-chk')) return;
    if(e.ctrlKey || e.metaKey){
      e.preventDefault();
      toggleSelect(l.id, !SELECTED.has(l.id));
      ULTIMO_CLICADO = l.id;
      return;
    }
    if(e.shiftKey){
      e.preventDefault();
      selecionarIntervalo(l.id);
      return;
    }
    abrirLead(l.id);
  };
  const chips = [
    ['📸', l.tem_instagram], ['✉️', l.email], ['📞', l.telefone], ['🌐', l.tem_site]
  ].map(([i,on])=>`<span class="chip ${on?'on':''}">${i}</span>`).join('');
  const extra = (l.n_notas?`📝${l.n_notas} `:'')+(l.n_anexos?`📎${l.n_anexos}`:'');
  const pills = [waPill(l), psPill(l)].filter(Boolean).join(' ');
  c.innerHTML = `<input type="checkbox" class="card-chk" ${SELECTED.has(l.id)?'checked':''} title="selecionar">
    <h4>${esc(l.nome)}</h4>
    <div class="meta"><span>${esc(l.nicho||'')}</span>${l.roi?`<span>${money(l.roi)}/mês</span>`:''}</div>
    ${pills?`<div style="margin-bottom:7px">${pills}</div>`:''}
    ${seloCampanha(l)}
    ${seloEsteira(l)}
    <div class="foot">
      <span class="badge" style="background:${scoreCor(l.score)}">${l.score!=null?l.score:'—'}</span>
      <div class="chips">${chips}</div>
      <span class="s" style="font-size:11px;color:var(--muted)">${extra}</span>
    </div>`;
  const chk=$('.card-chk',c);
  chk.onclick=e=>{ e.stopPropagation(); toggleSelect(l.id, chk.checked); };
  return c;
}

$('#kanban-search').oninput = renderBoard;
async function atualizarStats(){ renderKPIs(await jget('/api/stats')); }

// ── Coluna "Entrar em contato" ─────────────────────────
async function enviarEntrarContato(){
  const n = document.querySelectorAll('.col-body[data-col="entrar_contato"] .lcard').length;
  if(!n){ toast('Nenhum lead na coluna'); return; }
  const aviso = [
    `Enviar o primeiro contato por WhatsApp para ${n} lead(s)?`, '',
    'Sai um por vez, com intervalo sorteado entre eles. Cada lead é',
    'verificado na hora em que a mensagem dele vai sair — quem falhar vai',
    'para a coluna "Problema" com o motivo, e a fila segue para o próximo.', '',
    'Mensagem enviada não tem desfazer.'
  ].join(String.fromCharCode(10));

  const r = await jsend('/api/kanban/entrar-contato/enviar','POST',{});
  if(!r || !r.ok){
    toast('Nada foi enviado');
    alert(['Nada foi enviado.', '', (r && r.erro) || 'motivo não informado pelo servidor'
          ].join(String.fromCharCode(10)));
    return;
  }
  toast(`Rodada iniciada: ${r.na_rodada} lead(s)`);
  const parados = r.parados || [];
  if(parados.length){
    const q = String.fromCharCode(10);
    console.log(`${parados.length} fora da rodada:` + q
      + parados.map(p=>`• ${p.nome||('lead '+p.lead_id)}: ${p.motivo}`).join(q));
  }
  acompanharEnvio();
}

// ── Acompanhamento da rodada ─────────────────────────────────────────────────
// A rodada leva de 15 a 45 minutos: o request não pode ficar preso esperando, e
// o operador não pode ficar sem saber onde está. A tela sonda o progresso.
let ENVIO_SONDA = null;

function acompanharEnvio(){
  if(ENVIO_SONDA) clearInterval(ENVIO_SONDA);
  ENVIO_SONDA = setInterval(atualizarEnvio, 3000);
  atualizarEnvio();
}

async function atualizarEnvio(){
  const box = $('#envio-progresso'); if(!box) return;
  const e = await jget('/api/kanban/entrar-contato/progresso');
  // O botão de abortar vive FORA do painel de progresso: o painel só aparece
  // depois que a rodada começa, e até lá não havia como parar pela tela.
  const bA = $('#wa-btn-abortar'), est = $('#wa-op-estado');
  const rodando = !!(e && e.rodando);
  if(bA){ bA.disabled = !rodando; bA.style.opacity = rodando ? 1 : 0.5; }
  if(est){ est.textContent = rodando
      ? ('enviando ' + (e.feitos||0) + ' de ' + (e.total||0))
      : ''; }
  if(!e || (!e.rodando && !e.total)){ box.style.display='none'; return; }

  const atual = e.atual
    ? `<div class="s">enviando para <b>${esc(e.atual.nome)}</b> (${e.atual.posicao} de ${e.total})</div>`
    : (e.proximo_em ? `<div class="s">próximo em ${e.proximo_em}s</div>` : '');
  const fim = e.rodando ? '' :
    `<div class="s" style="color:${e.motivo_parada?'#dc2626':'#16a34a'}">`
    + (e.motivo_parada ? esc('parou: '+e.motivo_parada) : 'rodada concluída') + '</div>';
  box.style.display='';
  box.innerHTML = `<h3 style="margin:0 0 6px">${e.rodando?'Enviando…':'Rodada encerrada'}</h3>
    <div class="s"><b>${e.enviados}</b> enviado(s) · <b>${e.problemas}</b> em Problema
      · ${e.feitos} de ${e.total}</div>
    ${atual}${fim}
    <div style="margin-top:8px;display:flex;gap:6px">
      ${e.rodando?`<button class="btn btn-sm btn-danger" onclick="pararEnvio()">Parar</button>`:''}
      <button class="btn btn-sm" onclick="$('#envio-progresso').style.display='none'">Fechar</button>
    </div>`;

  if(!e.rodando){
    clearInterval(ENVIO_SONDA); ENVIO_SONDA = null;
    await recarregarLeads(); atualizarStats();
  }
}

async function pararEnvio(){
  const r = await jsend('/api/kanban/entrar-contato/parar','POST',{});
  toast(r && r.ok ? 'Vai parar depois do lead atual' : (r && r.erro) || 'nada rodando');
  atualizarEnvio();
}

// Enquanto o funil estiver aberto, o estado do envio anda sozinho. Sem isto o
// botão de abortar só sabia da rodada depois de alguém ter clicado em enviar
// naquela mesma sessão da página.
setInterval(()=>{
  const sp = document.getElementById('subpane-wa-funil');
  const pane = document.getElementById('pane-whatsapp');
  if(sp && sp.classList.contains('active') && pane && pane.classList.contains('active'))
    atualizarEnvio();
}, 4000);

// ── Seleção em massa ────────────────────────────────────────────────────────
let ULTIMO_CLICADO = null;   // âncora do Shift+clique

function toggleSelect(id,checked){
  if(checked) SELECTED.add(id); else SELECTED.delete(id);
  const c=document.querySelector(`.lcard[data-id="${id}"]`); if(c) c.classList.toggle('sel',checked);
  const chk=document.querySelector(`.lcard[data-id="${id}"] .card-chk`); if(chk) chk.checked=checked;
  updateBulkBar();
}

function idsDaColuna(key){
  const body=document.querySelector(`.col-body[data-col="${key}"]`);
  return body ? [...body.querySelectorAll('.lcard')].map(c=>+c.dataset.id) : [];
}

function colunaDoLead(id){
  const c=document.querySelector(`.lcard[data-id="${id}"]`);
  return c ? c.closest('.col-body')?.dataset.col : null;
}

function selecionarIntervalo(id){
  const col=colunaDoLead(id);
  const ids=idsDaColuna(col);
  const fim=ids.indexOf(id);
  const ini=ULTIMO_CLICADO!=null ? ids.indexOf(ULTIMO_CLICADO) : -1;
  if(ini<0 || fim<0){ toggleSelect(id,true); ULTIMO_CLICADO=id; return; }
  const [a,b]=[Math.min(ini,fim),Math.max(ini,fim)];
  ids.slice(a,b+1).forEach(x=>toggleSelect(x,true));
}

// Seleciona (ou limpa) a coluna inteira, respeitando a busca: o que está
// filtrado fora da tela não entra, senão o botão pegaria leads invisíveis.
function toggleColuna(key){
  const ids=idsDaColuna(key);
  if(!ids.length) return;
  const todosJa = ids.every(i=>SELECTED.has(i));
  ids.forEach(i=>toggleSelect(i, !todosJa));
  ULTIMO_CLICADO = todosJa ? null : ids[ids.length-1];
  const b=document.querySelector(`.col-sel[data-col="${key}"]`);
  if(b) b.textContent = todosJa ? 'todos' : 'nenhum';
}

function clearSelection(){ SELECTED.clear(); ULTIMO_CLICADO=null; updateBulkBar(); renderBoard(); }
function updateBulkBar(){
  const bar=$('#bulk-bar'); if(!bar) return;
  $('#bulk-count').textContent=SELECTED.size;
  bar.classList.toggle('show', SELECTED.size>0);
}
async function bulkMover(){
  if(!SELECTED.size) return; const k=$('#bulk-col').value;
  const r = await jsend('/api/leads/bulk','POST',{ids:[...SELECTED],acao:'mover',kanban:k});
  if(r && r.erro){ toast(r.erro); return; }
  toast(`${SELECTED.size} movido(s) para ${COL_LABEL[k]}`); SELECTED.clear();
  updateBulkBar(); await recarregarLeads(); atualizarStats();
}
// Mudar de QUADRO é diferente de mudar de coluna: a coluna é a etapa dentro
// do mesmo funil, o quadro é de quem é o lead. Um lead só vive em um quadro.
async function bulkMoverQuadro(){
  if(!SELECTED.size) return;
  const qid = parseInt($('#bulk-quadro').value);
  if(!qid){ toast('Escolha o quadro de destino'); return; }
  if(qid === QUADRO_ATIVO){ toast('Os leads já estão neste quadro'); return; }
  const n = SELECTED.size;
  const r = await jsend('/api/leads/mover-quadro','POST',{ids:[...SELECTED], quadro_id:qid});
  if(r.ok===false){ toast(r.erro||'Não deu para mover'); return; }
  const q = QUADROS.find(x=>x.id===qid);
  toast(n+' lead(s) foram para "'+(q?q.nome:qid)+'"');
  SELECTED.clear(); updateBulkBar();
  await quadrosCarregar(); await recarregarLeads(); atualizarStats();
}

async function bulkExcluir(){
  if(!SELECTED.size) return;
  const ids = [...SELECTED];
  let r = await jsend('/api/leads/bulk','POST',{ids,acao:'excluir'});
  // ou vai tudo, ou não vai nada (o backend checa a lista inteira antes de
  // apagar). Recusa com `pode_forcar` é dado real: oferece a exclusão
  // definitiva, que exige a palavra digitada
  if(r && r.erro && r.pode_forcar){
    const palavra = prompt(r.erro + String.fromCharCode(10,10)
      + `Para apagar DEFINITIVAMENTE ${ids.length} lead(s), com identidade, laudo, `
      + `diagnóstico, proposta, notas e toques junto, digite ${r.confirmacao}:`);
    if(!palavra) { toast('Exclusão cancelada'); return; }
    r = await jsend('/api/leads/bulk','POST',
      {ids, acao:'excluir', forcar:true, confirmacao:palavra});
  }
  if(r && r.erro){ toast('Exclusão recusada'); alert(r.erro); return; }
  toast(`${r.n != null ? r.n : ids.length} excluído(s)`
        + (r.definitivo ? ' definitivamente' : '')); SELECTED.clear();
  updateBulkBar(); await recarregarLeads(); atualizarStats();
}
async function bulkEsteira(){
  if(!SELECTED.size) return;
  toast(`Rodando esteira em ${SELECTED.size} lead(s)... aguarde`);
  const r=await jsend('/api/leads/bulk/esteira','POST',{ids:[...SELECTED]});
  toast(`Esteira: ${r.concluidos||0}/${r.total||0} concluído(s)`); SELECTED.clear();
  updateBulkBar(); await recarregarLeads(); atualizarStats();
}
async function rodarEsteira(id){
  toast('Rodando esteira completa (apurar → referências → diagnóstico → proposta → material)...');
  const r=await jsend(`/api/leads/${id}/esteira`,'POST',{});
  if(r.ok){ toast(`✅ Esteira: ${(r.passos||[]).join(' → ')}`); }
  else toast(r.erro||'falhou');
  await recarregarLeads(); atualizarStats(); abrirLead(id);
}

// ── MODAL LEAD ─────────────────────────────────────────────────────────────────
async function abrirLead(id){
  const l = await jget(`/api/leads/${id}`);
  // o card mostra e edita a campanha do lead; se a aba de Templates ainda não
  // foi aberta nesta sessão, a lista de campanhas ainda não veio
  if(!CAMPANHAS.length) await carregarCampanhas();
  const nota = f => (l[f]!=null&&l[f]!=='')?l[f]:'';
  const gov = l.gov || null;
  const colOpts = KCOLS.map(c=>`<option value="${c.key}" ${l.kanban===c.key?'selected':''}>${c.label}</option>`).join('');
  $('#drawer').innerHTML = `
    <div class="drawer-head">
      <span class="close-x" onclick="fecharModal()">×</span>
      <h2 id="d-nome-titulo">${esc(l.nome)}
        <button class="btn btn-sm" title="editar o nome da clínica"
          onclick="editarNome()">✏️</button></h2>
      <div id="d-nome-edicao" class="row" style="display:none">
        <input id="d-nome" value="${esc(l.nome)}" placeholder="Nome da clínica"
          onkeydown="if(event.key==='Enter')salvarNome(${id}); if(event.key==='Escape')cancelarNome()">
        <button class="btn btn-sm btn-primary" style="flex:0 0 auto"
          onclick="salvarNome(${id})">Salvar</button>
        <button class="btn btn-sm" style="flex:0 0 auto"
          onclick="cancelarNome()">Cancelar</button>
      </div>
      <div class="sub">${esc(l.categoria||'')} · ${esc(l.nicho||'')} · score ${l.score!=null?l.score:'—'}</div>
    </div>
    <div class="drawer-body">
      <div class="sec">
        <h4>Situação</h4>
        ${l.maps_url?`<div style="margin-bottom:8px"><a class="maps-link" href="${esc(l.maps_url)}" target="_blank">📍 Ver no Google Maps ↗</a></div>`:''}
        <div class="row">
          <div><label>Coluna do Kanban</label>
            <select id="d-kanban" onchange="moverColuna(${id},this.value)">${colOpts}</select></div>
        </div>
        <div class="row">
          <button class="btn btn-primary btn-sm" onclick="rodarEsteira(${id})">▶ Rodar esteira completa</button>
          <button class="btn btn-sm" onclick="gerarMaterial(${id})">📄 Só o material (PDF)</button>
          <button class="btn btn-danger btn-sm" onclick="excluirLead(${id})">🗑 Excluir</button>
        </div>
        <p class="hint">Esteira = apura o site → busca referências da cidade → diagnóstico → proposta → material PDF → move para "Material pronto".</p>
        ${campanhaDoLeadHtml(l)}
        ${cadenciaHtml(id, l)}
      </div>

      <div class="sec">
        <h4>Contatos</h4>
        <div class="row"><input id="d-telefone" placeholder="Telefone" value="${esc(nota('telefone'))}">
          <input id="d-email" placeholder="E-mail" value="${esc(nota('email'))}"></div>
        <div class="row"><input id="d-instagram" placeholder="@instagram" value="${esc(nota('instagram'))}">
          <input id="d-website" placeholder="Site" value="${esc(nota('website'))}"></div>
        <div class="row"><input id="d-endereco" placeholder="Endereço" value="${esc(nota('endereco'))}"></div>
        <button class="btn btn-sm" onclick="salvarContatos(${id})">Salvar contatos</button>
      </div>

      <div class="sec">
        <h4>Apuração de dados · site · WhatsApp · PageSpeed</h4>
        <div class="row" style="gap:6px">
          <button class="btn btn-sm" onclick="apurarSite(${id})">🔎 Apurar pelo site</button>
          <button class="btn btn-sm" onclick="verificarWhatsapp(${id})">💬 Confirmar WhatsApp</button>
          <button class="btn btn-sm" onclick="rodarPagespeed(${id})">⚡ PageSpeed</button>
        </div>
        <div id="apur-box">
          ${l.tem_whatsapp===1?`<div class="gov-result">💬 WhatsApp: <b>SIM</b> · <a class="maps-link" href="${esc(l.whatsapp)}" target="_blank">abrir conversa ↗</a></div>`:
            l.tem_whatsapp===0?`<div class="gov-result err">💬 WhatsApp: não confirmado (número fixo?)</div>`:''}
          ${(()=>{const d=psInfo(l);return d?`<div class="gov-result">⚡ PageSpeed ${d.strategy}: <b>${d.score}/100</b> · LCP ${d.metrics?.LCP||'?'}</div>`:'';})()}
        </div>
      </div>

      <div class="sec">
        <h4>Métricas</h4>
        <div class="kv">
          <span class="k">Posição Maps</span><span>${l.posicao||'—'}º</span>
          <span class="k">Nota Google</span><span>${l.avaliacao_num!=null?l.avaliacao_num:'—'}</span>
          <span class="k">Avaliações</span><span>${l.total_avaliacoes_num||0}</span>
          <span class="k">ROI estimado</span><span>${l.diagnostico?money(l.diagnostico.roi_reais)+'/mês':'—'}</span>
          <span class="k">Plano indicado</span><span>${l.proposta?esc(l.proposta.nivel_nome):'—'}</span>
        </div>
      </div>

      <div class="sec">
        <h4>Enriquecimento (Governo / Receita)</h4>
        <div class="row"><input id="d-cnpj" placeholder="CNPJ" value="${esc(nota('cnpj'))}">
          <button class="btn btn-sm" style="flex:0 0 auto" onclick="enriquecer(${id})">🏛 Enriquecer</button></div>
        <div id="gov-box">${gov?govHtml(gov):(l.razao_social?`<div class="gov-result">Razão social: ${esc(l.razao_social)}</div>`:'')}</div>
      </div>

      <div class="sec">
        <h4>Conteúdo com IA</h4>
        <div class="row" style="gap:6px">
          <button class="btn btn-sm" onclick="gerarConteudo(${id},'post')">📸 Post</button>
          <button class="btn btn-sm" onclick="gerarConteudo(${id},'email')">✉️ E-mail</button>
          <button class="btn btn-sm" onclick="gerarConteudo(${id},'whatsapp')">💬 WhatsApp</button>
        </div>
        <div id="conteudo-box"></div>
      </div>

      <div class="sec">
        <h4>Notas</h4>
        <div class="row"><textarea id="d-nota" rows="2" placeholder="Registrar interação..."></textarea></div>
        <button class="btn btn-sm" onclick="addNota(${id})">+ Nota</button>
        <div style="margin-top:10px">${(l.notas||[]).map(n=>`<div class="note">${esc(n.texto)}<div class="d">${n.created_at}</div></div>`).join('')||'<span class="muted">Sem notas.</span>'}</div>
      </div>

      <div class="sec">
        <h4>Anexos</h4>
        <div>${(l.anexos||[]).map(a=>`<div class="att"><span>📎 ${esc(a.filename)}</span>
          <span style="display:flex;gap:6px">
            <a class="btn btn-sm" href="/api/attachments/${a.id}">baixar</a>
            <button class="btn btn-sm btn-danger" title="excluir o material"
              onclick="excluirAnexo(${a.id}, ${id})">×</button>
          </span></div>`).join('')||'<span class="muted">Sem anexos.</span>'}</div>
        <div class="row" style="margin-top:8px"><input type="file" id="d-file">
          <button class="btn btn-sm" style="flex:0 0 auto" onclick="uploadAnexo(${id})">⬆️ Anexar</button></div>
      </div>
    </div>`;
  $('#overlay').classList.add('open');
}
function govHtml(g){
  if(!g.ok) return `<div class="gov-result err">${esc(g.erro||'não encontrado')}${g.orientacao?'<br>'+esc(g.orientacao):''}</div>`;
  return `<div class="gov-result">
    <b>${esc(g.razao_social||'')}</b> ${g.nome_fantasia?'· '+esc(g.nome_fantasia):''}<br>
    Situação: ${esc(g.situacao||'—')} · Fonte: ${esc(g.fonte||'')}<br>
    Atividade: ${esc(g.cnae||'—')}<br>
    ${g.telefone?'📞 '+esc(g.telefone)+' ':''} ${g.email?'✉️ '+esc(g.email):''}<br>
    ${g.endereco?'📍 '+esc(g.endereco):''}</div>`;
}
function fecharModal(){ $('#overlay').classList.remove('open'); }
function fecharModalFora(e){ if(e.target.id==='overlay') fecharModal(); }

async function moverColuna(id,k){
  const r = await jsend(`/api/leads/${id}/kanban`,'POST',{kanban:k});
  if(r && r.erro){ toast(r.erro); abrirLead(id); return; }   // devolve o select ao valor real
  const l=LEADS.find(x=>x.id==id); if(l)l.kanban=k; renderBoard(); atualizarStats(); toast('Movido para '+COL_LABEL[k]);
}
async function salvarContatos(id){
  await jsend(`/api/leads/${id}`,'PATCH',{telefone:$('#d-telefone').value,email:$('#d-email').value,
    instagram:$('#d-instagram').value,website:$('#d-website').value,endereco:$('#d-endereco').value,cnpj:$('#d-cnpj').value});
  toast('Contatos salvos'); recarregarLeads(); }

// ── Nome da clínica ───────────────────────────────────────────────────────────
// O nome vem do Maps e frequentemente vem sujo ("Dr.
function editarNome(){
  $('#d-nome-titulo').style.display='none';
  $('#d-nome-edicao').style.display='flex';
  const i=$('#d-nome'); i.focus(); i.select();
}
function cancelarNome(){
  $('#d-nome-edicao').style.display='none';
  $('#d-nome-titulo').style.display='';
}
async function salvarNome(id){
  const nome=$('#d-nome').value.trim();
  if(!nome){ toast('O nome da clínica não pode ficar vazio'); return; }
  const r=await jsend(`/api/leads/${id}`,'PATCH',{nome});
  if(r && r.erro){ toast(r.erro); return; }
  toast('Nome salvo');
  await recarregarLeads(); atualizarStats(); abrirLead(id);
}
async function addNota(id){ const t=$('#d-nota').value.trim(); if(!t)return;
  await jsend(`/api/leads/${id}/notes`,'POST',{texto:t}); toast('Nota adicionada'); abrirLead(id); recarregarLeads(); }
async function enriquecer(id){
  $('#gov-box').innerHTML='<span class="muted">Consultando Receita...</span>';
  const r = await jsend(`/api/leads/${id}/enrich`,'POST',{cnpj:$('#d-cnpj').value.trim()});
  $('#gov-box').innerHTML = govHtml(r); if(r.ok){toast('Dados do governo importados');recarregarLeads();}
}
async function gerarMaterial(id){
  toast('Gerando diagnóstico + proposta + PDF...');
  const r = await jsend(`/api/leads/${id}/material`,'POST',{});
  toast(r.pdf?'Material pronto (PDF anexado)':'Material gerado (PDF indisponível)');
  abrirLead(id); recarregarLeads(); atualizarStats();
}
async function excluirLead(id){
  let r = await jsend(`/api/leads/${id}`,'DELETE');
  // 409 = trava de dado real. A tela ignorava a resposta e dizia "Lead
  // excluído" de qualquer jeito: o lead continuava lá e ninguém ficava sabendo
  if(r && r.erro && r.pode_forcar){
    const palavra = prompt(r.erro + String.fromCharCode(10,10)
      + 'Para apagar DEFINITIVAMENTE, com identidade, laudo, diagnóstico, '
      + `proposta, notas e toques junto, digite ${r.confirmacao}:`);
    if(!palavra){ toast('Exclusão cancelada'); return; }
    r = await jsend(`/api/leads/${id}?forcar=1&confirmacao=`
      + encodeURIComponent(palavra),'DELETE');
  }
  if(r && r.erro){ toast('Exclusão recusada'); alert(r.erro); return; }
  fecharModal(); recarregarLeads(); atualizarStats();
  toast(r.definitivo ? 'Lead apagado definitivamente' : 'Lead excluído');
}
async function uploadAnexo(id){
  const f=$('#d-file').files[0]; if(!f){toast('Escolha um arquivo');return;}
  const fd=new FormData(); fd.append('file',f);
  await fetch(`/api/leads/${id}/attachments`,{method:'POST',body:fd});
  toast('Anexo enviado'); abrirLead(id); recarregarLeads();
}
async function apurarSite(id){
  $('#apur-box').innerHTML='<span class="muted">Visitando o site e extraindo contatos reais...</span>';
  const r=await jsend(`/api/leads/${id}/apurar`,'POST',{website:$('#d-website').value.trim()});
  if(!r.ok){ $('#apur-box').innerHTML=`<div class="gov-result err">${esc(r.erro||'falhou')}</div>`; return; }
  const a=r.achados, w=r.whatsapp||{};
  $('#apur-box').innerHTML=`<div class="gov-result">✅ Apurado do site (${a.paginas_lidas} páginas):<br>
    📞 ${(a.telefones||[]).join(' · ')||'—'}<br>
    💬 WhatsApp: ${w.tem?`<b>SIM</b> (${w.tipo}) · <a class="maps-link" target="_blank" href="${w.link}">abrir ↗</a>`:'não confirmado'}<br>
    ✉️ ${(a.emails||[]).join(' · ')||'—'}<br>
    📸 ${(a.instagram||[]).map(i=>'@'+esc(i)).join(' ')||'—'}<br>
    ${r.mudancas&&r.mudancas.length?'<b>Aplicado:</b> '+r.mudancas.map(esc).join('; '):'nada a corrigir'}</div>`;
  toast('Site apurado'); recarregarLeads();
}
async function verificarWhatsapp(id){
  const r=await jsend(`/api/leads/${id}/whatsapp`,'POST',{numero:$('#d-telefone').value.trim()});
  const label=r.tem?'SIM':(r.tem===false?'não':'indefinido');
  $('#apur-box').innerHTML=`<div class="gov-result ${r.tem?'':'err'}">💬 WhatsApp: <b>${label}</b> (${r.tipo})<br>
    ${esc(r.motivo||'')}${r.link?` · <a class="maps-link" target="_blank" href="${r.link}">abrir conversa ↗</a>`:''}</div>`;
  toast('WhatsApp verificado'); recarregarLeads();
}
async function rodarPagespeed(id){
  $('#apur-box').innerHTML='<span class="muted">Rodando Google PageSpeed (~15-30s)...</span>';
  const r=await jsend(`/api/leads/${id}/pagespeed`,'POST',{});
  if(!r.ok){ $('#apur-box').innerHTML=`<div class="gov-result err">${esc(r.erro||'falhou')}</div>`; return; }
  const m=r.metrics||{};
  $('#apur-box').innerHTML=`<div class="gov-result">⚡ PageSpeed (${r.strategy}): <b>${r.score}/100</b><br>
    LCP ${esc(m.LCP||'?')} · CLS ${esc(m.CLS||'?')} · TBT ${esc(m.TBT||'?')}<br>
    📎 Screenshot anexado — entra no material do cliente.</div>`;
  toast('PageSpeed concluído'); recarregarLeads();
}
async function gerarConteudo(id,tipo){
  $('#conteudo-box').innerHTML='<span class="muted">Gerando com IA (~5-15s)...</span>';
  const r=await jsend(`/api/leads/${id}/conteudo`,'POST',{tipo});
  if(!r.ok){ $('#conteudo-box').innerHTML=`<div class="gov-result err">${esc(r.erro||'falhou')}</div>`; return; }
  $('#conteudo-box').innerHTML=`<div class="gov-result" style="white-space:pre-wrap">${esc(r.texto)}
    <div style="margin-top:8px"><button class="btn btn-sm" onclick="copiarTexto(this)">📋 Copiar</button></div></div>`;
}
function copiarTexto(btn){
  const box=btn.closest('.gov-result');
  const t=box.childNodes[0].textContent.trim();
  navigator.clipboard.writeText(t).then(()=>toast('Copiado!'));
}
function abrirNovoLead(){ switchTab('config'); $('#man-nome').focus(); toast('Preencha em "Inserir lead manualmente"'); }

// ── SCRAP
// ────────────────────────────────────────────────────────────────────── ──
// RASPAGEM (Google Maps) ─────────────────────────────────────────────────── Uma
// busca concluida NAO some: ela guarda os leads que encontrou e fica na fila
// esperando um destino.
let SCRAP_ITENS = [];

async function carregarScrap(){
  SCRAP_ITENS = await jget('/api/scrap').catch(()=>[]);
  const box = $('#scrap-list');
  if(!box) return;
  if(!SCRAP_ITENS.length){
    box.innerHTML = '<span class="muted">Nenhuma busca na fila.</span>';
    return;
  }
  box.innerHTML = SCRAP_ITENS.map(s=>{
    const L = s.leads || {total:0, sem_quadro:0, por_quadro:{}};
    const alocados = Object.entries(L.por_quadro||{});
    let selo = '';
    if(L.total){
      selo = L.sem_quadro
        ? `<span class="busca-leads pendente" title="ainda sem quadro">${L.sem_quadro} lead(s) sem destino</span>`
        : `<span class="busca-leads alocado">${L.total} lead(s)</span>`;
    }
    const destinos = alocados.length
      ? `<div class="s" style="margin-top:2px">em: ${alocados.map(([n,q])=>esc(n)+' ('+q+')').join(' · ')}</div>`
      : '';
    // so faz sentido despachar o que existe
    const btnEnviar = L.total
      ? `<button class="btn btn-sm btn-primary" onclick="scrapAbrirDestino(${s.id})"
           title="mandar estes leads para um quadro">→ Quadro</button>`
      : '';
    // descartar so aparece quando ha o que descartar: lead sem quadro
    const btnDescartar = L.sem_quadro
      ? `<button class="btn btn-sm btn-danger" onclick="scrapDescartar(${s.id})"
           title="apagar os ${L.sem_quadro} sem destino, sem mandar para quadro">
           Descartar</button>`
      : '';
    return `<div class="list-item" style="flex-direction:column;align-items:stretch;gap:6px">
      <div class="busca-linha">
        <div style="flex:1;min-width:140px">
          <div class="t">${esc(s.termo)}</div>
          <div class="s">${esc(s.localizacao||'—')}${s.modo==='estado'
            ? ` · <b style="color:#00ff88">varredura</b> (até ${s.orcamento||30} áreas)` : ''}${
            s.tem_progresso ? ` · <b style="color:#ffc048">pausada em ${s.areas_feitas} área(s)</b>` : ''}</div>
          ${destinos}
        </div>
        ${selo}
        <span class="status-pill st-${s.status}">${s.status}</span>
        ${btnEnviar}
        ${btnDescartar}
        <button class="btn btn-sm${s.status==='pausado'?' btn-primary':''}"
          onclick="runScrap(${s.id})"
          title="${s.tem_progresso ? 'continua de onde parou ('+s.areas_feitas+' áreas já varridas)' : 'rodar de novo'}"
          >${s.tem_progresso ? '▶ Continuar' : '▶'}</button>
        <button class="btn btn-sm btn-danger" onclick="delScrap(${s.id})">×</button>
      </div>
      <div id="scrap-destino-${s.id}"></div>
    </div>`;
  }).join('');
}

async function addScrap(){
  const termo = $('#scrap-termo').value.trim();
  if(!termo){ toast('Informe o termo'); return; }
  const modo = ($('#scrap-modo')||{}).value || 'busca';
  const r = await jsend('/api/scrap','POST',{
    termo, localizacao: $('#scrap-local').value.trim(), modo,
    orcamento: parseInt(($('#scrap-orcamento')||{}).value || '30')});
  if(r && r.erro){ toast(r.erro); alert(r.erro); return; }
  $('#scrap-termo').value=''; $('#scrap-local').value='';
  carregarScrap(); toast(modo==='estado' ? 'Varredura na fila' : 'Adicionado à fila');
  scrapPrever();
}

// O que a varredura vai custar, ANTES de comecar. Uma varredura leva de dez
// minutos a uma hora: sem o numero na tela, quem manda rodar nao tem como
// distinguir "esta trabalhando" de "travou".
async function scrapPrever(){
  const modo = ($('#scrap-modo')||{}).value || 'busca';
  const caixa = $('#scrap-varredura'), aviso = $('#scrap-previsao');
  if(caixa) caixa.style.display = (modo==='estado') ? '' : 'none';
  if(!aviso) return;
  if(modo!=='estado'){ aviso.textContent=''; return; }
  const local = ($('#scrap-local')||{}).value.trim();
  if(!local){ aviso.textContent = 'Escreva o estado, sozinho: "Alagoas" ou "AL".'; return; }
  const orc = parseInt(($('#scrap-orcamento')||{}).value || '30');
  const p = await jget(`/api/scrap/plano?local=${encodeURIComponent(local)}&orcamento=${orc}`)
              .catch(()=>({ok:false}));
  aviso.innerHTML = p && p.ok
    ? `Varre <b>${esc(p.estado)}</b> em até ${p.orcamento} áreas do mapa `
      + `(começa com ${p.celulas_iniciais} e divide onde achar mais). `
      + `Cerca de <b>${p.minutos_estimados} min</b>.`
    : `<span style="color:#ffc048">"${esc(local)}" não é um estado. `
      + `Para varrer, escreva só o estado; para uma cidade, use "Uma busca".</span>`;
}

// ── A coleta ao vivo, com estado visivel do primeiro segundo ────────────────
let _livePoll = null;
let _liveUltimo = 0;

function scrapEstado(estado, texto){
  const farol = $('#scrap-farol'), barra = $('#scrap-barra'), esq = $('#scrap-esqueleto');
  if(farol) farol.className = 'coleta-farol ' + estado;
  if(barra) barra.classList.toggle('parada', estado !== 'rodando');
  if(texto && $('#scrap-esqueleto-txt')) $('#scrap-esqueleto-txt').textContent = texto;
  if(estado !== 'rodando' && esq) esq.classList.add('oculto');
}

async function runScrap(id){
  const item = SCRAP_ITENS.find(s=>s.id===id);

  // A TELA REAGE ANTES DA REDE. O card abria vazio e com o icone de imagem
  // quebrada ate o primeiro screenshot chegar -- uns 10 segundos parecendo que
  // nada aconteceu. Agora o esqueleto entra no clique.
  const card = $('#scrap-live-card');
  card.style.display = 'block';
  $('#scrap-live-count').textContent = '0';
  $('#scrap-live-list').innerHTML = '';
  $('#scrap-live-fim').innerHTML = '';
  $('#scrap-live-msg').textContent = '';
  $('#scrap-live-termo').textContent = item ? (item.termo + (item.localizacao ? ' · '+item.localizacao : '')) : '';
  const img = $('#scrap-live-img');
  img.removeAttribute('src'); img.dataset.vazio = '1';
  $('#scrap-esqueleto').classList.remove('oculto');
  _liveUltimo = 0;
  scrapEstado('rodando', 'abrindo o navegador...');
  card.scrollIntoView({behavior:'smooth', block:'nearest'});

  const r = await jsend(`/api/scrap/${id}/run`,'POST',{});
  if(!r.ok){
    scrapEstado('erro');
    $('#scrap-live-msg').textContent = r.erro || 'não deu para iniciar';
    toast(r.erro || 'Não deu para iniciar');
    return;
  }
  toast(r.mensagem || 'Coleta iniciada');
  carregarScrap();
  startLivePoll(id);
}

// Pausar nao mata o processo. Matar perderia a rolagem em andamento e, na
// varredura, a area onde ela estava.
let _pausando = false;

async function scrapPausar(){
  if(_pausando) return;
  _pausando = true;
  const b = $('#scrap-pausar');
  if(b){ b.disabled = true; b.textContent = 'pausando...'; }
  const r = await jsend(`/api/scrap/${_liveSid}/parar`,'POST',{});
  if(r && r.ok === false){
    toast(r.erro || 'Não deu para pausar');
    if(b){ b.disabled = false; b.textContent = '⏸ Pausar'; }
    _pausando = false;
    return;
  }
  toast('Pausando... a coleta para depois desta rolagem');
}

let _liveSid = null;

function startLivePoll(sid){
  _liveSid = sid; _pausando = false;
  const bp = $('#scrap-pausar');
  if(bp){ bp.style.display = ''; bp.disabled = false; bp.textContent = '⏸ Pausar'; }
  clearInterval(_livePoll);
  const img=$('#scrap-live-img'), list=$('#scrap-live-list'),
        msg=$('#scrap-live-msg'), cnt=$('#scrap-live-count');

  _livePoll = setInterval(async()=>{
    let p; try{ p = await jget('/api/scrap/live?sid='+sid); }catch(e){ return; }

    if(p.screenshot){
      img.src = p.screenshot;
      img.dataset.vazio = '0';
      $('#scrap-esqueleto').classList.add('oculto');
    }
    msg.textContent = p.msg || '';

    // o contador salta quando entra alguem: da para ver o progresso de longe
    const n = p.count || 0;
    if(n !== _liveUltimo){
      cnt.textContent = n;
      cnt.classList.remove('subiu'); void cnt.offsetWidth; cnt.classList.add('subiu');
      _liveUltimo = n;
    }

    list.innerHTML = (p.businesses||[]).map(b=>`<div class="live-row">
      <div><b>${esc(b.nome)}</b> ${b.avaliacao?`<span class="r">⭐ ${esc(b.avaliacao)} (${esc(b.total_avaliacoes||'0')})</span>`:''}
        <div class="r">${esc(b.categoria||'')}${b.telefone?' · 📞 '+esc(b.telefone):''}${b.website?' · 🌐 site':''}</div></div>
      <a class="maps-link" href="${esc(b.maps_url)}" target="_blank">Maps ↗</a></div>`).join('');

    if(p.done){
      clearInterval(_livePoll);
      const pausada = /^Pausada/.test(p.msg||'');
      const bp2 = $('#scrap-pausar'); if(bp2) bp2.style.display = 'none';
      scrapEstado(n ? 'pronto' : 'erro');
      toast(p.msg || 'Coleta concluída');
      await carregarScrap();
      recarregarLeads(); atualizarStats();
      await quadrosCarregar();

      // Terminou: o passo seguinte fica na tela, nao escondido numa aba.
      $('#scrap-live-fim').innerHTML = n
        ? `<div style="padding:11px;border:1px solid ${pausada?'#ffc048':'#00ff88'};
              border-radius:8px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
             <span style="color:${pausada?'#ffc048':'#00ff88'};font-weight:700">
               ${pausada?'⏸':'✓'} ${n} lead(s) ${pausada?'coletados até aqui':'coletados'}</span>
             <span class="muted" style="font-size:12px">${pausada
               ? 'Já estão salvos. O ▶ na fila continua de onde parou.'
               : 'Escolha para qual quadro eles vão.'}</span>
             ${pausada ? `<button class="btn btn-sm btn-primary"
               onclick="runScrap(${sid})">▶ Continuar</button>` : ''}
             <button class="btn btn-sm btn-primary" style="margin-left:auto"
               onclick="scrapAbrirDestino(${sid})">Escolher quadro</button>
             <button class="btn btn-sm btn-danger"
               title="apaga estes leads; não mexe em quem já está em quadro"
               onclick="scrapDescartar(${sid})">Descartar</button>
           </div>`
        : `<div style="padding:11px;border:1px solid #ff4757;border-radius:8px;color:#ff4757">
             Nenhum resultado. Tente outro termo ou localização.</div>`;
      if(n) scrapAbrirDestino(sid);
    }
  }, 1500);
}

// A coleta veio ruim -- termo errado, cidade errada, 300 lugares que nao servem.
async function scrapDescartar(sid, apagarBusca){
  const s = SCRAP_ITENS.find(x=>x.id===sid) || {};
  const L = s.leads || {sem_quadro:0};
  const n = L.sem_quadro || 0;
  if(!n){ toast('Nada para descartar: estes leads já estão em quadros'); return; }
  if(!confirm(`Descartar ${n} lead(s) desta coleta?`
      + String.fromCharCode(10,10)
      + `Eles ainda não foram para quadro nenhum e serão apagados.`
      + (L.total>n ? String.fromCharCode(10) + `Os ${L.total-n} que já estão em quadros ficam onde estão.` : '')))
    return;
  const r = await jsend(`/api/scrap/${sid}/leads`
    + (apagarBusca?'?apagar_busca=1':''), 'DELETE');
  if(r && r.erro){ toast(r.erro); return; }
  toast(`${r.excluidos||0} descartado(s)`
    + (r.mantidos ? ` · ${r.mantidos} mantido(s) em quadros` : '')
    + (apagarBusca ? (r.busca_apagada ? ' · busca apagada'
                                      : ' · a busca fica na fila pelos que estão em quadros') : ''));
  $('#scrap-live-fim').innerHTML = '';
  carregarScrap(); recarregarLeads(); atualizarStats();
}

async function delScrap(id, forcar){
  const r = await jsend(`/api/scrap/${id}`+(forcar?'?forcar=1':''),'DELETE');
  if(r.ok === false){
    // a busca ainda tem lead sem destino: mostra a saida em vez de so recusar
    const box = $('#scrap-destino-'+id);
    if(box){
      box.dataset.aberto = '1';
      // As TRES saidas, cada uma dizendo o que faz com os leads.
      const n = r.sem_quadro || 0;
      // Quantos desta busca JA estao em quadro. Eles mudam o que o botao pode
      // prometer: se existirem, a linha da busca nao some -- apagar a linha
      // desligaria esses leads da unica tela por onde se chega neles.
      const sc = SCRAP_ITENS.find(x=>x.id===id) || {};
      const jaEmQuadro = Math.max(0, ((sc.leads||{}).total||0) - n);
      box.innerHTML = `<div class="repetidos-box">
        <div style="font-weight:700;color:#ffc048;font-size:13px">
          Esta busca tem ${n} lead(s) que ainda não foram para quadro nenhum.
          Escolha o que fazer com eles:</div>
        ${jaEmQuadro ? `<div class="muted" style="font-size:12px;margin-top:4px">
          Outros ${jaEmQuadro} já estão em quadros e não são tocados por nenhuma
          destas opções — por isso a linha da busca continua na fila.</div>` : ''}
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
          <button class="btn btn-sm btn-primary" onclick="scrapAbrirDestino(${id})">
            Mandar para um quadro</button>
          <button class="btn btn-sm btn-danger" onclick="scrapDescartar(${id}, true)">
            ${jaEmQuadro ? `Apagar os ${n} sem destino` : `Apagar a busca e os ${n} lead(s)`}</button>
          <button class="btn btn-sm" onclick="delScrap(${id}, true)">
            Apagar só a busca (os ${n} vão para o quadro padrão)</button>
          <button class="btn btn-sm" onclick="carregarScrap()">Cancelar</button>
        </div></div>`;
    }
    toast(r.erro || 'Não deu para apagar');
    return;
  }
  if(r.realocados) toast(`Busca apagada · ${r.realocados} lead(s) foram para o quadro padrão`);
  carregarScrap();
}

// ── Para qual quadro vao os leads desta busca ───────────────────────────────
async function scrapAbrirDestino(sid){
  const box = $('#scrap-destino-'+sid);
  if(!box){ switchSubtab('wa','raspagem'); return; }
  if(box.dataset.aberto === '1'){ box.innerHTML=''; box.dataset.aberto='0'; return; }
  box.dataset.aberto = '1';

  if(!QUADROS.length) await quadrosCarregar();
  const s = SCRAP_ITENS.find(x=>x.id===sid) || {};
  const L = s.leads || {total:0};

  box.innerHTML = `<div class="card-box" style="margin-top:4px">
    <div style="font-size:13px;font-weight:700;margin-bottom:8px">
      Enviar ${L.total||0} lead(s) de "${esc(s.termo||'')}" para um quadro</div>
    <div style="display:flex;gap:8px;align-items:end;flex-wrap:wrap">
      <div style="flex:1;min-width:170px">
        <label class="lf-label">Quadro existente</label>
        <select id="sd-quadro-${sid}" class="lf-input" onchange="scrapAnalisar(${sid})">
          ${QUADROS.map(q=>`<option value="${q.id}">${esc(q.nome)} (${q.leads})</option>`).join('')}
        </select>
      </div>
      <div style="flex:1;min-width:170px">
        <label class="lf-label">ou criar um novo</label>
        <input id="sd-novo-${sid}" class="lf-input" placeholder="ex.: Marcenarias Maceió"
               oninput="scrapAnalisar(${sid})">
      </div>
      <button class="btn btn-primary" onclick="scrapEnviar(${sid})">Enviar</button>
      <button class="btn" onclick="scrapAbrirDestino(${sid})">Fechar</button>
    </div>
    <div id="sd-analise-${sid}" style="margin-top:10px"></div>
  </div>`;
  scrapAnalisar(sid);
}

// Confere ANTES de enviar quantos ja pertencem a outro quadro.
async function scrapAnalisar(sid){
  const alvo = $('#sd-analise-'+sid); if(!alvo) return;
  const novo = ($('#sd-novo-'+sid)||{}).value || '';
  if(novo.trim()){
    // quadro novo nao tem lead nenhum: nao existe repetido possivel
    alvo.innerHTML = `<div class="muted" style="font-size:12px">
      Um quadro novo chamado "<b>${esc(novo.trim())}</b>" será criado e receberá todos os leads.</div>`;
    return;
  }
  const qid = parseInt(($('#sd-quadro-'+sid)||{}).value);
  if(!qid) return;

  const a = await jsend(`/api/scrap/${sid}/analisar`,'POST',{quadro_id:qid});
  if(a.ok === false){ alvo.innerHTML = `<div style="color:#ff4757;font-size:12px">${esc(a.erro||'')}</div>`; return; }

  let html = `<div class="muted" style="font-size:12px">
      ${a.novos} inédito(s) entram · ${a.ja_no_destino} já estão neste quadro</div>`;

  if(a.repetidos){
    const origens = Object.entries(a.origens||{})
      .map(([n,q])=>`<b>${esc(n)}</b> (${q})`).join(', ');
    html += `<div class="repetidos-box">
      <div style="font-weight:700;color:#ffc048;font-size:13px">
        ⚠ ${a.repetidos} lead(s) já pertencem a outro quadro</div>
      <div class="muted" style="font-size:12px;margin-top:4px">Vindos de: ${origens}.</div>
      <div class="muted" style="font-size:11.5px;margin-top:4px">
        ${a.exemplos_repetidos.map(e=>esc(e.nome)+' — '+esc(e.quadro)).join(' · ')}</div>
      <div class="repetidos-opcoes">
        <label><input type="radio" name="pol-${sid}" value="ignorar" checked>
          <span><b>Ignorar</b> — deixa cada um onde está
          <span class="desc">Só os ${a.novos} inéditos entram. Nada é alterado nos outros quadros.</span></span></label>
        <label><input type="radio" name="pol-${sid}" value="substituir">
          <span><b>Substituir</b> — traz todos para este quadro
          <span class="desc">Os ${a.repetidos} repetidos SAEM do quadro atual e passam para este.</span></span></label>
        <label><input type="radio" name="pol-${sid}" value="duplicar">
          <span><b>Duplicar</b> — cria uma cópia aqui
          <span class="desc">O original continua no quadro dele e uma cópia entra neste, no início do funil.
          Atenção: o mesmo lugar passa a existir duas vezes, e pode receber mensagem de dois quadros.</span></span></label>
      </div>
    </div>`;
  }
  alvo.innerHTML = html;
}

async function scrapEnviar(sid){
  const novo = (($('#sd-novo-'+sid)||{}).value || '').trim();
  const qid = parseInt(($('#sd-quadro-'+sid)||{}).value) || null;
  const escolha = document.querySelector(`input[name="pol-${sid}"]:checked`);
  const politica = escolha ? escolha.value : 'ignorar';

  if(!novo && !qid){ toast('Escolha um quadro ou dê um nome ao novo'); return; }

  const r = await jsend(`/api/scrap/${sid}/enviar`,'POST',
                        {quadro_id: qid, novo_quadro: novo || null, politica});
  if(r.ok === false){ toast(r.erro || 'Não deu para enviar'); return; }

  const partes = [];
  if(r.movidos)      partes.push(`${r.movidos} enviado(s)`);
  if(r.substituidos) partes.push(`${r.substituidos} movido(s) de outro quadro`);
  if(r.duplicados)   partes.push(`${r.duplicados} duplicado(s)`);
  if(r.ignorados)    partes.push(`${r.ignorados} mantido(s) onde estavam`);
  if(r.ja_estavam)   partes.push(`${r.ja_estavam} já estavam aqui`);
  toast(partes.join(' · ') || 'Nada a mover');

  const box = $('#scrap-destino-'+sid); if(box){ box.innerHTML=''; box.dataset.aberto='0'; }
  await quadrosCarregar();
  await carregarScrap();
  await recarregarLeads();
  atualizarStats();
}


// ── PROSPECÇÃO ──────────────────────────────────────────────────────────────────
let TPLS_CANAL = [];
async function carregarTemplatesCanal(){
  const canal=$('#pros-canal').value; if(!canal)return;
  TPLS_CANAL = await jget('/api/templates?canal='+canal);
  const sel=$('#pros-template'); sel.innerHTML='';
  TPLS_CANAL.forEach(t=>sel.append(new Option(t.nome,t.id)));
}
async function carregarLeadsProspeccao(){
  const col=$('#pros-coluna').value;
  const leads = LEADS.filter(l=>!col||l.kanban===col);
  const box=$('#pros-leads'); box.innerHTML = leads.length?'':'<span class="muted">Nenhum lead nesta coluna.</span>';
  leads.forEach(l=>{ const d=document.createElement('label'); d.className='pl';
    d.innerHTML=`<input type="checkbox" value="${l.id}"> ${esc(l.nome)} <span class="s" style="color:var(--muted)">· ${l.nicho||''} · score ${l.score??'—'}</span>`;
    box.append(d); });
}
function leadsSelecionados(){ return $$('#pros-leads input:checked').map(c=>+c.value); }
async function previewProspeccao(){ await enviarProspeccao(false); }
async function dispararProspeccao(){ await enviarProspeccao($('#pros-enviar').checked); }
async function enviarProspeccao(enviar){
  const ids=leadsSelecionados(); if(!ids.length){toast('Selecione ao menos 1 lead');return;}
  const r = await jsend('/api/prospect','POST',{canal:$('#pros-canal').value,
    template_id:+$('#pros-template').value, lead_ids:ids, enviar});
  if(r.erro){toast(r.erro);return;}
  const box=$('#pros-resultado');
  box.innerHTML = `<h4 style="margin:10px 0">${enviar?'✅ Disparado':'👁️ Pré-visualização'} — ${r.previews.length} lead(s)</h4>`+
    r.previews.map(p=>`<div class="preview"><div class="pn">${esc(p.nome)}</div>
      ${p.assunto?`<div class="s" style="color:var(--teal)">Assunto: ${esc(p.assunto)}</div>`:''}
      <div class="pm">${esc(p.corpo)}</div></div>`).join('');
  if(enviar){ toast('Disparo concluído'); recarregarLeads(); atualizarStats();
    if(r.envio) box.innerHTML+=`<div class="hint">Retorno do envio: ${esc(JSON.stringify(r.envio))}</div>`; }
}

// ── TEMPLATES
// ──────────────────────────────────────────────────────────────────── ──
// CAMPANHAS ────────────────────────────────────────────────────────────────
// Campanha = abertura + reforcos.
let CAMPANHAS = [];
let CAMP_PASSOS = [];      // config.ESTEIRA_WHATSAPP: da o dia de cada posicao
let CAMP_MAX = 4;
let CAMP_EDITANDO = null;  // {cid, ordem, campanha, rotulo} enquanto edita no form

async function carregarCampanhas(){
  const d = await jget('/api/campanhas');
  CAMPANHAS = d.campanhas || [];
  CAMP_PASSOS = d.passos || [];
  CAMP_MAX = d.max_mensagens || 4;
  const box = $('#camp-list');
  if(!box) return;
  if(!CAMPANHAS.length){
    box.innerHTML = '<span class="muted">Nenhuma campanha. Crie a primeira acima.</span>';
    return;
  }
  const emRodizio = d.em_rodizio || [];
  box.innerHTML = CAMPANHAS.map(c=>{
    const dentro = emRodizio.includes(c.nome);
    const selo = c.ativa
      ? (dentro ? '<span class="tpl-etapa" style="background:#16a34a">em rodízio</span>'
                : '<span class="tpl-etapa" style="background:#d97706">ativa, sem abertura escrita</span>')
      : '<span class="tpl-etapa" style="background:#94a3b8">desativada</span>';
    const linhas = [];
    for(let o=0; o<CAMP_MAX; o++){
      const m = (c.mensagens||[]).find(x=>(x.ordem||0)===o);
      const passo = CAMP_PASSOS[o] || {};
      const rotulo = o===0 ? 'Abertura' : ('Reforço '+o);
      const previa = m ? esc((m.corpo||'').replace(/\s+/g,' ').slice(0,70))+'…'
                       : '<span class="muted">vazia — não sai nada nesta posição</span>';
      linhas.push(`<div class="esteira-linha" style="align-items:flex-start">
        <span style="flex:1">
          <b>${rotulo}</b> <span class="muted">· dia ${passo.dia!=null?passo.dia:'?'}</span><br>
          <span class="s">${previa}</span>
        </span>
        <button class="btn btn-sm" onclick="editarMensagemCampanha(${c.id},${o})">${m?'✏️':'+ escrever'}</button>
      </div>`);
    }
    return `<div class="list-item" style="display:block">
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
        <input class="camp-nome" style="flex:1;font-weight:600" value="${esc(c.nome)}"
          onchange="renomearCampanha(${c.id}, this.value)">
        ${selo}
        <label class="chk" title="tira do rodízio; quem já está dentro termina a sequência">
          <input type="checkbox" ${c.ativa?'checked':''}
            onchange="ativarCampanha(${c.id}, this.checked)"> ativa</label>
        <button class="btn btn-sm btn-danger" title="excluir campanha"
          onclick="excluirCampanha(${c.id})">×</button>
      </div>
      ${linhas.join('')}
    </div>`;
  }).join('');
}

async function criarCampanha(){
  const nome = $('#camp-nova').value.trim();
  if(!nome){ toast('Dê um nome para a campanha'); return; }
  const r = await jsend('/api/campanhas','POST',{nome});
  if(r && r.erro){ toast(r.erro); return; }
  $('#camp-nova').value='';
  toast('Campanha criada. Escreva a abertura para ela entrar no rodízio.');
  carregarCampanhas();
}

async function renomearCampanha(cid, nome){
  const r = await jsend('/api/campanhas/'+cid,'PUT',{nome});
  if(r && r.erro){ toast(r.erro); }
  carregarCampanhas();
}

async function ativarCampanha(cid, ativa){
  const r = await jsend('/api/campanhas/'+cid,'PUT',{ativa});
  if(r && r.erro){ toast(r.erro); carregarCampanhas(); return; }
  const dentro = (r.em_rodizio||[]);
  toast(ativa ? `Ativada. Em rodízio agora: ${dentro.join(', ')||'nenhuma'}`
              : 'Desativada. Não recebe leads novos; quem já está dentro termina.');
  carregarCampanhas();
}

async function excluirCampanha(cid){
  const r = await jsend('/api/campanhas/'+cid,'DELETE');
  if(r && r.erro){ toast(r.erro); return; }   // 409 quando já tem lead dentro
  toast('Campanha excluída');
  carregarCampanhas();
}

// Reaproveita o MESMO formulário dos templates: uma caixa de texto só para
// escrever mensagem, em vez de dois editores que divergem com o tempo.
function editarMensagemCampanha(cid, ordem){
  const c = CAMPANHAS.find(x=>x.id===cid); if(!c){ toast('Campanha não encontrada'); return; }
  const m = (c.mensagens||[]).find(x=>(x.ordem||0)===ordem);
  const passo = CAMP_PASSOS[ordem] || {};
  const rotulo = ordem===0 ? 'Abertura' : ('Reforço '+ordem);
  CAMP_EDITANDO = {cid, ordem, campanha:c.nome, rotulo};
  TPL_EDITANDO = null;
  $('#tpl-titulo').textContent = `${c.nome} · ${rotulo} (dia ${passo.dia!=null?passo.dia:'?'})`;
  const aviso = $('#tpl-campanha-aviso');
  aviso.style.display='';
  aviso.innerHTML = `Editando uma mensagem de campanha. ${esc(passo.uso||'')}`;
  $('#tpl-nome').value = (m && m.nome) || rotulo;
  $('#tpl-corpo').value = (m && m.corpo) || '';
  $('#tpl-assunto').value = '';
  // canal e etapa não se escolhem aqui: vêm da campanha e da posição
  $('#tpl-canal').value='whatsapp'; $('#tpl-canal').onchange();
  $('#tpl-etapa-l').style.display='none'; $('#tpl-etapa').style.display='none';
  $('#tpl-etapa-uso').style.display='none';
  $('#tpl-salvar').textContent='Salvar mensagem';
  $('#tpl-cancelar').style.display='';
  $('#tpl-corpo').focus();
}

let TPL_EDITANDO = null;   // id do template aberto no formulario, ou null

async function carregarTemplates(){
  // as mensagens que pertencem a uma campanha aparecem DENTRO dela, no cartão de
  // cima. Repeti-las aqui daria dois lugares para editar a mesma coisa, e o de
  // baixo não sabe a posição na sequência
  const todos = await jget('/api/templates');
  TPLS_TODOS = (todos||[]).filter(t=>!t.campanha_id);
  const box=$('#tpl-list');
  box.innerHTML = TPLS_TODOS.length?'':'<span class="muted">Nenhum template solto.</span>';
  TPLS_TODOS.forEach(t=>{ const d=document.createElement('div');
    d.className='list-item'+(TPL_EDITANDO===t.id?' editando':'');
    const marca = t.etapa ? `<span class="tpl-etapa">${esc(t.etapa)}</span>` : '';
    d.innerHTML=`<div><div class="t">[${t.canal}] ${esc(t.nome)} ${marca}</div>
      <div class="s">${esc((t.corpo||'').slice(0,70))}...</div></div>
      <span style="display:flex;gap:6px">
        <button class="btn btn-sm" onclick="editarTemplate(${t.id})" title="editar">✏️</button>
        <button class="btn btn-sm btn-danger" onclick="delTemplate(${t.id})" title="excluir">×</button>
      </span>`;
    box.append(d); });
}

// Editar carrega o template NO MESMO formulario de criar. Antes so dava para
// criar e apagar: corrigir uma virgula obrigava a recriar, o que troca o id.
function editarTemplate(id){
  const t=(TPLS_TODOS||[]).find(x=>x.id===id); if(!t){toast('Template não encontrado');return;}
  if(CAMP_EDITANDO) cancelarEdicaoTemplate();   // sai do modo campanha primeiro
  TPL_EDITANDO=id;
  $('#tpl-canal').value=t.canal; $('#tpl-canal').onchange();
  $('#tpl-etapa').value=t.etapa||''; $('#tpl-etapa').onchange();
  $('#tpl-nome').value=t.nome||''; $('#tpl-assunto').value=t.assunto||'';
  $('#tpl-corpo').value=t.corpo||'';
  $('#tpl-titulo').textContent='Editando: '+(t.nome||'sem nome');
  $('#tpl-salvar').textContent='Salvar alterações';
  $('#tpl-cancelar').style.display='';
  carregarTemplates();
  $('#tpl-nome').focus();
}

function cancelarEdicaoTemplate(){
  if(CAMP_EDITANDO){
    CAMP_EDITANDO = null;
    $('#tpl-campanha-aviso').style.display='none';
    $('#tpl-etapa-l').style.display=''; $('#tpl-etapa').style.display='';
    $('#tpl-etapa-uso').style.display='';
    carregarCampanhas();
  }
  TPL_EDITANDO=null;
  $('#tpl-nome').value='';$('#tpl-corpo').value='';$('#tpl-assunto').value='';
  $('#tpl-titulo').textContent='Novo template';
  if($('#tpl-etapa')){ $('#tpl-etapa').value=''; $('#tpl-canal').onchange(); }
  $('#tpl-salvar').textContent='Salvar template';
  $('#tpl-cancelar').style.display='none';
  carregarTemplates();
}

async function salvarTemplate(){
  const corpo=$('#tpl-corpo').value.trim(); if(!corpo){toast('Escreva o corpo');return;}
  // mensagem de campanha vai por outra rota: lá a etapa sai da POSIÇÃO, e não
  // do que a tela mandar, para os dois nunca discordarem
  if(CAMP_EDITANDO){
    const {cid, ordem, rotulo} = CAMP_EDITANDO;
    const r = await jsend(`/api/campanhas/${cid}/mensagens`,'POST',
      {ordem, corpo, nome:$('#tpl-nome').value||rotulo});
    if(r && r.erro){ toast(r.erro); return; }
    toast(`${rotulo} salva`);
    cancelarEdicaoTemplate();
    return;
  }
  const dados={canal:$('#tpl-canal').value,nome:$('#tpl-nome').value||'Sem nome',
    assunto:$('#tpl-assunto').value,corpo,
    etapa: $('#tpl-canal').value==='whatsapp' ? ($('#tpl-etapa').value||'') : ''};
  if(TPL_EDITANDO){
    const r=await jsend('/api/templates/'+TPL_EDITANDO,'PUT',dados);
    if(r&&r.erro){toast(r.erro);return;}
    toast('Template atualizado'); cancelarEdicaoTemplate(); return;
  }
  const novo = await jsend('/api/templates','POST',dados);
  if(novo && novo.erro){ toast(novo.erro); return; }
  $('#tpl-nome').value='';$('#tpl-corpo').value='';$('#tpl-assunto').value='';
  carregarTemplates(); toast('Template salvo');
}

async function delTemplate(id){
  await jsend('/api/templates/'+id,'DELETE');
  if(TPL_EDITANDO===id) cancelarEdicaoTemplate(); else carregarTemplates();
}


// ── COLUNAS DO QUADRO ────────────────────────────────────────────────────────
// A chave nunca aparece editável: é ela que está gravada em `leads.kanban`, e
// trocá-la deixaria os cards apontando para uma coluna que não existe.




// o quadro inteiro depende das colunas: mudou uma, tudo é remontado
async function recarregarTudoDoQuadro(){
  const b = await jget('/api/bootstrap');
  KCOLS = b.kanban_cols; COL_COR = {}; COL_LABEL = {};
  KCOLS.forEach(c=>{ COL_COR[c.key]=c.cor; COL_LABEL[c.key]=c.label; });
  popularSelects();
  await recarregarLeads();
  atualizarStats();
}

// ── CONFIG ───────────────────────────────────────────────────────────────────────
async function inserirManual(){
  const nome=$('#man-nome').value.trim(); if(!nome){toast('Nome é obrigatório');return;}
  await jsend('/api/leads','POST',{nome,categoria:$('#man-categoria').value,telefone:$('#man-telefone').value,
    email:$('#man-email').value,instagram:$('#man-instagram').value,website:$('#man-website').value,
    cnpj:$('#man-cnpj').value,endereco:$('#man-endereco').value});
  ['man-nome','man-categoria','man-telefone','man-email','man-instagram','man-website','man-cnpj','man-endereco']
    .forEach(i=>$('#'+i).value='');
  recarregarLeads(); atualizarStats(); toast('Lead adicionado'); switchTab('whatsapp'); switchSubtab('wa','funil');
}
async function importar(){
  const f=$('#import-file').files[0]; if(!f){toast('Escolha um arquivo');return;}
  const fd=new FormData(); fd.append('file',f);
  const r=await (await fetch('/api/import',{method:'POST',body:fd})).json();
  toast(r.erro?r.erro:`Importados: ${r.importados}`); recarregarLeads(); atualizarStats();
}

// ── SCHEDULER ─────────────────────────────────────────────────────────────────

// ── FOLLOW-UP ─────────────────────────────────────────────────────────────────

// ── CONTAS ────────────────────────────────────────────────────────────────────

// Pareamento de WhatsApp em andamento.
const WA_QR = { conta:null, qr:null, timer:null };

function accSpinner(texto){
  return '<div style="display:flex;align-items:center;gap:6px;font-size:11px;color:#64748b">'
    +'<span class="lf-spin" style="width:12px;height:12px;border:2px solid #2a3a52;'
    +'border-top-color:#00ff88;border-radius:50%;display:inline-block"></span>'
    +esc(texto)+'</div>';
}

// keyframes injetado uma vez só: o CSS do painel é servido estático e não vale
// editar arquivo de tema por causa de um spinner
(function(){
  if(document.getElementById('lf-spin-css')) return;
  const st=document.createElement('style');
  st.id='lf-spin-css';
  st.textContent='@keyframes lfspin{to{transform:rotate(360deg)}}.lf-spin{animation:lfspin .8s linear infinite}';
  document.head.appendChild(st);
})();

function accRenderQR(){
  if(!WA_QR.conta) return;
  const box=$('#wa-qr-'+WA_QR.conta);
  if(!box) return;
  box.innerHTML = WA_QR.qr
    ? '<img src="'+WA_QR.qr+'" alt="QR Code" style="width:180px;height:180px;background:#fff;padding:6px;border-radius:6px">'
      +'<div style="font-size:11px;color:#64748b;margin-top:4px">Escaneie no WhatsApp do '+esc(WA_QR.conta)+'</div>'
    : accSpinner('gerando o QR...');
}

async function accAbrirPerfil(canal, identificador){
  const rotas={
    instagram:'/api/connect/instagram/abrir-perfil',
    whatsapp:'/api/connect/whatsapp/abrir-perfil',
  };
  const rota=rotas[canal];
  if(!rota) return;
  toast('Abrindo o navegador autenticado...');
  const r=await jsend(rota,'POST',{conta:identificador}).catch(e=>({ok:false,erro:String(e)}));
  toast(r.mensagem||r.erro||(r.ok?'Navegador aberto.':'Não foi possível abrir a sessão.'));
  accCarregar();
}

function accBotaoAbrirNavegador(canal, identificador, aberto=false){
  const canalJs=esc(JSON.stringify(canal));
  const contaJs=esc(JSON.stringify(String(identificador||'')));
  const texto=aberto?'🧭 Navegador aberto':'🧭 Abrir navegador';
  return `<button class="btn btn-sm" style="margin-top:4px" ${aberto?'disabled':''}
             onclick="accAbrirPerfil(${canalJs},${contaJs})"
             title="Abrir a sessão salva desta conta no navegador">${texto}</button>`;
}

async function accCarregar(){
  // Busca status real de conexão de cada canal As contas de Gmail passaram a morar
  // na aba E-mail, que já as administra pelo pcz.db (autenticação, limite diário,
  // validação contra a API).
  const [igSt, waSt] = await Promise.all([
    jget('/api/connect/instagram/status').catch(()=>[]),
    jget('/api/connect/whatsapp/status').catch(()=>[]),
  ]);

  // Instagram
  const igBox=$('#acc-ig');
  if(igBox){
    const contas = Array.isArray(igSt) ? igSt : [];
    igBox.innerHTML = contas.length ? contas.map(a=>{
      const pct=Math.min(100,Math.round((a.sent_today/a.daily_limit)*100));
      const restam=Math.max(0,a.daily_limit-a.sent_today);
      const cor=pct>=90?'#ff4757':pct>=50?'#ffc048':'#00ff88';
      // `sessao_ativa` vem do MESMO perfil que o bot abre (cookies sessionid +
      // ds_user_id). O campo antigo `tem_sessao` olhava uma pasta que nenhum
      // bot lia, e por isso mostrava as duas contas conectadas com zero sessão.
      const connCor = a.sessao_ativa ? '#00ff88' : '#ff4757';
      const connTxt = a.sessao_ativa ? '✅ Conectada' : '❌ Sem sessão';
      const botTxt = a.bot_online ? '' : ' (bot de DM offline)';
      return `<div class="acc-row">
        <div>
          <div style="font-weight:700;font-size:13px;color:#e2e8f0">${esc(a.display_name||a.identifier)}</div>
          <div style="font-size:12px;color:#64748b">@${esc(a.identifier)}</div>
        </div>
        <div>
          <div style="font-size:12px;color:#64748b">${a.sent_today} hoje</div>${a.libera_em?`<div style="font-size:11px;color:#ffc048">cheia · zera em ${esc(a.libera_em)}</div>`:""}
          <div class="progress-bar"><div class="progress-fill" style="width:${pct}%;background:${cor}"></div></div>
          <div style="display:flex;align-items:center;gap:5px;margin-top:4px">
            <label class="lf-label" style="margin:0" title="teto do dia — zera à meia-noite">limite/dia</label>
            <input type="number" min="1" max="200" value="${a.daily_limit}" class="lf-input"
                   style="width:70px;padding:4px" onchange="accSalvarLimite(${a.id},this.value)">
            <span style="font-size:11px;color:${cor}">${restam} restam</span>
          </div>
        </div>
        <div>
          <div style="font-size:12px;color:${connCor};font-weight:600">${a.perfil_aberto?'🧭 Aberta no Chat ao vivo ou numa janela':connTxt}${a.perfil_aberto?'':esc(botTxt)}</div>
          <button class="btn btn-sm" style="margin-top:4px;border-color:${connCor};color:${connCor}" ${a.perfil_aberto?'disabled title="Feche a conta (Chat ao vivo › Instagram Direct ou a janela) antes de logar de novo"':''} onclick="accAuthIG('${esc(a.identifier)}',${a.sessao_ativa?'true':'false'})">${a.sessao_ativa||a.perfil_aberto?'🔄 Relogar':'🔑 Entrar'}</button>
          ${accBotaoAbrirNavegador('instagram',a.identifier,a.perfil_aberto)}
          <div style="font-size:10px;color:#475569;margin-top:3px" title="${esc(a.perfil||'')}">perfil próprio desta conta</div>
        </div>
        <div style="display:flex;gap:4px;align-items:center">
          <label class="toggle"><input type="checkbox" ${a.auto_prospect?'checked':''} onchange="accToggleAuto(${a.id},this.checked)"><span class="slider"></span></label>
          <button class="btn btn-sm btn-danger" onclick="accExcluir(${a.id})">🗑</button>
        </div>
      </div>`;
    }).join('') : '<div style="padding:10px;color:#64748b;font-size:13px">Nenhuma conta Instagram.</div>';
  }

  // WhatsApp
  const waBox=$('#acc-wa');
  if(waBox){
    const contas = Array.isArray(waSt) ? waSt : [];
    waBox.innerHTML = contas.length ? contas.map(a=>{
      const pct=Math.min(100,Math.round((a.sent_today/a.daily_limit)*100));
      const restam=Math.max(0,a.daily_limit-a.sent_today);
      const cor=pct>=90?'#ff4757':pct>=50?'#ffc048':'#00ff88';
      // Cada conta tem a SUA sessão no bot (um cliente por número). O estado
      // vem de /api/contas do bot, não mais de "qual número o processo pareou".
      const connCor = a.conectado ? '#00ff88' : (a.bot_online ? '#ffc048' : '#ff4757');
      const connTxt = a.bot_online ? (a.status_conexao || '—') : '❌ Bot offline';
      return `<div class="acc-row">
        <div>
          <div style="font-weight:700;font-size:13px;color:#e2e8f0">${esc(a.display_name||a.identifier)}</div>
          <div style="font-size:12px;color:#64748b">${esc(a.identifier)}</div>
        </div>
        <div>
          <div style="font-size:12px;color:#64748b">${a.sent_today} hoje</div>${a.libera_em?`<div style="font-size:11px;color:#ffc048">cheia · zera em ${esc(a.libera_em)}</div>`:""}
          <div class="progress-bar"><div class="progress-fill" style="width:${pct}%;background:${cor}"></div></div>
          <div style="display:flex;align-items:center;gap:5px;margin-top:4px">
            <label class="lf-label" style="margin:0" title="teto do dia — zera à meia-noite">limite/dia</label>
            <input type="number" min="1" max="200" value="${a.daily_limit}" class="lf-input"
                   style="width:70px;padding:4px" onchange="accSalvarLimite(${a.id},this.value)">
            <span style="font-size:11px;color:${cor}">${restam} restam</span>
          </div>
        </div>
        <div>
          <div style="font-size:12px;color:${connCor};font-weight:600">${a.perfil_aberto?'🧭 Perfil aberto no navegador':esc(connTxt)}</div>
          ${!a.conectado && a.bot_online ? (WA_QR.conta===String(a.identifier)
              ? `<button class="btn btn-sm" disabled style="margin-top:4px;opacity:.6;cursor:default">⏳ aguardando scan</button>`
              : `<button class="btn btn-sm" style="margin-top:4px;border-color:${connCor};color:${connCor}" onclick="accConectarWA('${esc(a.identifier)}',${a.tem_qr?'true':'false'})">📡 ${a.tem_qr?'Ver QR':'Conectar'}</button>`) : ''}
          ${a.conectado ? `<button class="btn btn-sm" style="margin-top:4px" onclick="accDesconectarWA('${esc(a.identifier)}')">Desconectar</button>` : ''}
          ${accBotaoAbrirNavegador('whatsapp',a.identifier,a.perfil_aberto)}
          <div id="wa-qr-${esc(a.identifier)}" style="margin-top:6px"></div>
        </div>
        <div style="display:flex;gap:4px;align-items:center">
          <label class="toggle"><input type="checkbox" ${a.auto_prospect?'checked':''} onchange="accToggleAuto(${a.id},this.checked)"><span class="slider"></span></label>
          <button class="btn btn-sm btn-danger" onclick="accExcluir(${a.id})">🗑</button>
        </div>
      </div>`;
    }).join('') : '<div style="padding:10px;color:#64748b;font-size:13px">Nenhuma conta WhatsApp.</div>';
  }

  // a lista acabou de ser redesenhada: devolve o QR aberto para o lugar dele
  accRenderQR();
}

// Atualização automática enquanto a aba Contas estiver aberta.
setInterval(()=>{
  const wa=document.getElementById('subpane-wa-contas');
  const ig=document.getElementById('subpane-ig-perfis');
  const visivel = (el)=>el && el.classList.contains('active')
    && el.closest('.pane') && el.closest('.pane').classList.contains('active');
  if(!(visivel(wa) || visivel(ig))) return;
  // Não redesenha por cima de quem está digitando. A lista é recriada inteira a
  // cada atualização, e com o cursor dentro do campo de limite o valor sumia no
  // meio da edição.
  const foco = document.activeElement;
  if(foco && (foco.tagName === 'INPUT' || foco.tagName === 'SELECT'
              || foco.tagName === 'TEXTAREA')) return;
  accCarregar();
}, 6000);

// ── CONEXÃO POR CANAL ────────────────────────────────────────────────────────

async function accAuthIG(username, jaLogada){
  const user=username.replace('@','');
  toast('Abrindo a janela de login de @'+user+'...');
  const r=await jsend('/api/connect/instagram/auth/'+user,'POST',{trocar_conta:!!jaLogada});
  toast(r.mensagem||r.erro||'Verifique a janela que abriu');
  // O login é manual e demora: a janela abre, você digita, resolve o 2FA. Por
  // isso o status é reconsultado por um tempo em vez de uma vez só.
  let n=0; const t=setInterval(async()=>{ n++; await accCarregar(); if(n>=20) clearInterval(t); }, 15000);
}

async function accConectarWA(conta, jaTemQr){
  const num=String(conta||'').replace(/\D/g,'');
  // Conta que JÁ está mostrando QR: só abre o visualizador. Mandar o POST de
  // conectar de novo reabria o pareamento no bot e matava o código que estava
  // na tela — o operador escaneava um QR de um cliente que já tinha morrido.
  if(jaTemQr){ accMostrarQR(num); return; }
  toast('Preparando o QR de '+num+'...');
  const r=await jsend('/api/connect/whatsapp/conectar','POST',{conta:num});
  // Só `ok:false` é falha. O campo `erro` do status é o ÚLTIMO erro que a conta
  // já viu (um LOGOUT antigo, por exemplo) e continua preenchido depois de
  // resolvido: tratá-lo como falha da chamada abortava o QR sem motivo.
  if(r.ok===false){toast(r.erro||'não deu para iniciar o pareamento');return;}
  toast(r.mensagem||'Aguardando o QR Code');
  accMostrarQR(num);
}

// Mostra o QR DAQUELA conta e fica olhando até parear.
async function accMostrarQR(conta){
  conta = String(conta||'').replace(/\D/g,'');
  // Um pareamento por vez. Clicar de novo não pode empilhar um segundo laço de
  // consulta sobre o mesmo QR — dois laços pedindo o código ao mesmo tempo é o
  // caminho para o operador escanear um que já foi substituído.
  if(WA_QR.timer) clearInterval(WA_QR.timer);
  WA_QR.conta = conta;
  WA_QR.qr = null;
  accCarregar();               // troca o botão dessa linha para o estado de espera
  accRenderQR();

  let n = 0;
  const tick = async () => {
    n++;
    const r = await jget('/api/connect/whatsapp/qr?conta='+conta).catch(()=>({}));
    WA_QR.qr = (r && r.qr) || null;
    accRenderQR();

    const lista = await jget('/api/connect/whatsapp/status').catch(()=>[]);
    const eu = (Array.isArray(lista)?lista:[])
      .find(c=>String(c.identifier).replace(/\D/g,'')===conta);

    // Erro do bot manda parar. Antes o laço seguia girando "gerando o QR..." por 5
    // minutos enquanto o bot já tinha desistido — o operador ficava esperando um
    // código que nunca viria, sem nenhuma pista do motivo.
    if(n >= 3 && eu && eu.erro_bot && !WA_QR.qr){
      clearInterval(WA_QR.timer); WA_QR.timer=null;
      const box=$('#wa-qr-'+conta);
      if(box) box.innerHTML =
        '<div style="font-size:11.5px;color:#ff4757;line-height:1.45">'
        + '<b>Não deu para abrir a sessão.</b><br>' + esc(String(eu.erro_bot).slice(0,150))
        + '</div>'
        + '<button class="btn btn-sm" style="margin-top:6px" '
        + 'onclick="accRecriarSessao(\'' + esc(conta) + '\')">Recriar sessão e tentar de novo</button>';
      WA_QR.conta=null; WA_QR.qr=null;
      accCarregar();
      return;
    }

    if(eu && eu.conectado){
      clearInterval(WA_QR.timer);
      WA_QR.timer=null; WA_QR.conta=null; WA_QR.qr=null;
      toast(conta+' conectada!');
      accCarregar();
      return;
    }
    if(n>=60){
      clearInterval(WA_QR.timer);
      WA_QR.timer=null;
      const box=$('#wa-qr-'+conta);
      if(box) box.innerHTML='<div style="font-size:11px;color:#64748b">tempo esgotado, clique em Ver QR de novo</div>';
      WA_QR.conta=null; WA_QR.qr=null;
      accCarregar();
    }
  };
  WA_QR.timer = setInterval(tick, 5000);
  tick();   // primeiro desenho imediato: esperar 5s parecia que nada aconteceu
}

async function accDesconectarWA(conta){
  const num=String(conta||'').replace(/\D/g,'');
  const r=await jsend('/api/connect/whatsapp/desconectar','POST',{conta:num});
  toast(r.mensagem||r.erro||'Desconectada');
  accCarregar();
}

// Apaga a pasta de sessão e começa o pareamento do zero.
async function accRecriarSessao(conta){
  const num=String(conta||'').replace(/\D/g,'');
  if(!confirm('Apagar a sessão salva de '+num+' e parear do zero?'
    +String.fromCharCode(10,10)
    +'Você vai precisar ler o QR de novo no celular. As outras contas não são afetadas.')) return;
  toast('Recriando a sessão de '+num+'...');
  await jsend('/api/connect/whatsapp/desconectar','POST',{conta:num}).catch(()=>({}));
  // o bot precisa de um instante para fechar o Chromium e soltar a pasta
  await new Promise(r=>setTimeout(r,3000));
  const r=await jsend('/api/connect/whatsapp/conectar','POST',{conta:num}).catch(()=>({}));
  if(r && r.ok===false){ toast(r.erro||'não deu para recriar'); accCarregar(); return; }
  accMostrarQR(num);
}


async function accCriarIG(){
  const user=$('#acc-ig-user').value.trim().replace('@','');
  const nome=$('#acc-ig-nome').value.trim();
  const lim=parseInt($('#acc-ig-limite').value)||15;
  if(!user){toast('Username obrigatório');return;}
  await jsend('/api/accounts','POST',{channel:'instagram',identifier:user,display_name:nome||user,daily_limit:lim});
  toast('Conta Instagram adicionada!');
  $('#acc-ig-user').value='';$('#acc-ig-nome').value='';
  accCarregar();
}

async function accCriarWA(){
  const phone=$('#acc-wa-phone').value.trim().replace(/\D/g,'');
  const nome=$('#acc-wa-nome').value.trim();
  const lim=parseInt($('#acc-wa-limite').value)||30;
  if(!phone){toast('Telefone obrigatório');return;}
  await jsend('/api/accounts','POST',{channel:'whatsapp',identifier:phone,display_name:nome||phone,daily_limit:lim});
  toast('Conta WhatsApp adicionada!');
  $('#acc-wa-phone').value='';$('#acc-wa-nome').value='';
  accCarregar();
}

// O limite diário é o freio de aquecimento do canal. Até aqui só dava para
// defini-lo no cadastro: mudar de 15 para 30 exigia apagar a conta e recriar,
// perdendo o contador do dia.
async function accSalvarLimite(id, valor){
  const limite = parseInt(valor) || 0;
  const r = await jsend('/api/accounts/'+id, 'PATCH', {daily_limit: limite});
  toast(r.ok ? ('Limite diário agora é '+limite) : (r.erro||'Não deu para salvar'));
  accCarregar();
}

async function accToggleAuto(id,ativo){
  await jsend('/api/accounts/'+id+'/auto-prospect','PATCH',{auto_prospect:ativo?1:0});
  toast(ativo?'Auto-prospect ativado':'Auto-prospect desativado');
  accCarregar();
}
async function accExcluir(id){
  await jsend('/api/accounts/'+id,'DELETE');
  toast('Conta excluída'); accCarregar();
}

// ── ANALYTICS ─────────────────────────────────────────────────────────────────

// ── RELATÓRIOS ────────────────────────────────────────────────────────────────

// ── BACKUP ────────────────────────────────────────────────────────────────────
async function backupListar(){
  const r=await jget('/api/backup/listar');
  const box=$('#backup-list');
  if(!r||!r.length){box.innerHTML='<div class="list-item"><div class="s muted">Nenhum backup encontrado.</div></div>';return;}
  box.innerHTML=r.map(b=>`<div class="list-item"><div class="s">Slot ${b.slot} — ${esc(b.data)} — ${b.tamanho_kb} KB</div></div>`).join('');
}
async function backupCriar(){
  toast('Criando backup...');
  const r=await jsend('/api/backup/criar','POST');
  toast(r.ok?`Backup slot ${r.slot} criado (${r.tamanho_kb} KB)`:'Erro ao criar backup');
  backupListar();
}

// ── SIDEBAR (removido, dados vão para system status) ──────────────────────────

// ── VISÃO GERAL ───────────────────────────────────────────────────────────────
// Uma chamada só (`/api/visao-geral`) e três perguntas: quanta base existe em
// cada ferramenta, quanto saiu hoje e quanto ainda cabe, e o que está de pé.
let VG_CHART = null;

function vgNum(n){ return (n||0).toLocaleString('pt-BR'); }

function vgBarra(usado, teto, cor){
  const pct = teto ? Math.min(100, Math.round(usado/teto*100)) : 0;
  return '<div class="progress-bar"><div class="progress-fill" style="width:'+pct
    +'%;background:'+cor+'"></div></div>';
}

async function vgCarregar(){
  const d = await jget('/api/visao-geral').catch(()=>({}));
  if(!d || d.erro){
    const box=$('#vg-canais');
    if(box) box.innerHTML='<div class="muted">Não deu para carregar: '+esc((d&&d.erro)||'sem resposta')+'</div>';
    return;
  }

  const at=$('#vg-atualizado'); if(at) at.textContent='atualizado '+(d.atualizado_em||'');
  const r=d.ritmo||{}, b=d.base||{};
  if($('#vg-hoje'))  $('#vg-hoje').textContent  = vgNum(r.enviados_hoje);
  if($('#vg-cabem')) $('#vg-cabem').textContent = vgNum(r.cabem_hoje);
  if($('#vg-base'))  $('#vg-base').textContent  = vgNum(b.alcancaveis);
  if($('#vg-novos')) $('#vg-novos').textContent = vgNum((b.leadforge||{}).nunca_contatados);

  vgRenderCanais(r, d.servicos||{});
  vgRenderBases(b);
  vgRenderServicos(d.servicos||{});
  vgRenderKanban((b.leadforge||{}).por_kanban||{});
  vgRenderGrafico(d.por_dia||[]);
}

// Cada canal com o seu teto: o teto é do CANAL, não do sistema. Somar tudo num
// número só esconde que o WhatsApp está no limite enquanto o e-mail tem folga.
function vgRenderCanais(r, srv){
  const box=$('#vg-canais'); if(!box) return;
  const canais=[
    {k:'whatsapp',  nome:'WhatsApp',  d:r.whatsapp||{},  s:srv.whatsapp||{},  cor:'#25d366'},
    {k:'email',     nome:'E-mail',    d:r.email||{},     s:srv.email||{},     cor:'#00ff88'},
    {k:'instagram', nome:'Instagram', d:r.instagram||{}, s:srv.instagram||{}, cor:'#e1306c'},
  ];
  box.innerHTML='<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px">'
    + canais.map(c=>{
      const hoje=c.d.hoje||0, teto=c.d.teto||0, restam=c.d.restam||0;
      const pct=teto?Math.round(hoje/teto*100):0;
      const cor=pct>=90?'#ff4757':pct>=50?'#ffc048':c.cor;
      const online=c.s.no_ar;
      return '<div style="padding:12px;background:#151d2e;border:1px solid #2a3a52;border-radius:8px">'
        +'<div style="display:flex;justify-content:space-between;align-items:center">'
          +'<b style="font-size:13px;color:#e2e8f0">'+c.nome+'</b>'
          +'<span style="font-size:11px;color:'+(online?'#00ff88':'#ff4757')+'">'
            +(online?'no ar':'offline')
            +(c.s.conectadas!==undefined?' · '+c.s.conectadas+'/'+(c.s.cadastradas||0)+' conta(s)':'')
          +'</span>'
        +'</div>'
        +'<div style="font-size:24px;font-weight:700;color:'+cor+';margin:6px 0 2px">'+vgNum(hoje)+'</div>'
        +'<div style="font-size:11px;color:#64748b;margin-bottom:6px">enviados hoje · teto '+vgNum(teto)+'</div>'
        + vgBarra(hoje, teto, cor)
        +'<div style="font-size:11px;color:#64748b;margin-top:4px">'+vgNum(restam)+' ainda cabem'
        +(c.d.disparando?' · <span style="color:#00ff88">disparando</span>':'')
        +(c.d.contas_sem_oauth?' · <span style="color:#ffc048">'+c.d.contas_sem_oauth+' sem OAuth</span>':'')
        +'</div>'
      +'</div>';
    }).join('') + '</div>';
}

function vgRenderBases(b){
  const box=$('#vg-bases'); if(!box) return;
  const lf=b.leadforge||{}, em=b.email||{}, ig=b.instagram||{};
  const linha=(nome,total,detalhe)=>'<div style="display:flex;justify-content:space-between;'
    +'align-items:center;padding:10px 0;border-bottom:1px solid #2a3a52">'
    +'<div><div style="font-size:13px;color:#e2e8f0;font-weight:600">'+nome+'</div>'
    +'<div style="font-size:11px;color:#64748b">'+detalhe+'</div></div>'
    +'<b style="font-size:18px;color:#00ff88">'+vgNum(total)+'</b></div>';

  box.innerHTML =
      linha('Leads (Google Maps)', lf.total,
            vgNum(lf.com_telefone)+' com telefone · '+vgNum(lf.com_email)+' com e-mail · '
            +vgNum(lf.com_instagram)+' com Instagram')
    + linha('Prospects de e-mail', em.total,
            em.ok===false ? 'banco do mail bot indisponível'
            : vgNum(em.pendentes)+' pendentes · '+vgNum(em.enviados)+' já enviados · '
              +vgNum(em.listas)+' lista(s)')
    + linha('Perfis do Instagram', ig.total,
            (ig.nichos||[]).length+' nicho(s): '
            + ((ig.nichos||[]).map(n=>esc(n.nicho)+' ('+n.processados+')').join(', ') || 'nenhum'))
    + '<div style="margin-top:10px;font-size:11px;color:#64748b">'
      +vgNum(lf.ja_contatados)+' lead(s) do Maps já receberam ao menos um toque; '
      +vgNum(lf.nunca_contatados)+' nunca foram contatados.</div>';
}

function vgRenderServicos(s){
  const box=$('#vg-servicos'); if(!box) return;
  const item=(nome, ok, txt)=>'<div style="display:flex;justify-content:space-between;'
    +'align-items:center;padding:10px 0;border-bottom:1px solid #2a3a52">'
    +'<div style="display:flex;align-items:center;gap:8px">'
    +'<span style="width:8px;height:8px;border-radius:50%;background:'+(ok?'#00ff88':'#ff4757')+'"></span>'
    +'<span style="font-size:13px;color:#e2e8f0">'+nome+'</span></div>'
    +'<span style="font-size:12px;color:#64748b">'+txt+'</span></div>';

  const wa=s.whatsapp||{}, ig=s.instagram||{}, em=s.email||{}, sc=s.scheduler||{};
  box.innerHTML =
      item('Bot WhatsApp', wa.no_ar,
           wa.no_ar ? (wa.conectadas+' de '+(wa.cadastradas||0)+' conta(s) conectada(s)'
             + (wa.numeros&&wa.numeros.length?' · '+esc(wa.numeros.join(', ')):''))
           : 'fora do ar (porta 5021)')
    + item('Bot Instagram', ig.no_ar,
           ig.no_ar ? (ig.conectadas+' de '+(ig.cadastradas||0)+' sessão(ões) ativa(s)')
           : 'fora do ar (porta 5022)')
    + item('E-mail (mail bot)', em.no_ar,
           em.no_ar ? (em.conectadas+' de '+(em.cadastradas||0)+' conta(s) autenticada(s)'
             + (em.disparando?' · disparando':''))
           : 'banco indisponível')
    + item('Scheduler', sc.rodando, sc.rodando ? 'rodando' : 'parado');
}

function vgRenderKanban(porKanban){
  const box=$('#vg-kanban'); if(!box) return;
  const entradas=Object.entries(porKanban).sort((a,b)=>b[1]-a[1]);
  const total=entradas.reduce((s,[,n])=>s+n,0)||1;
  box.innerHTML = entradas.length ? entradas.map(([nome,n])=>{
    const pct=Math.round(n/total*100);
    return '<div style="padding:7px 0;border-bottom:1px solid #2a3a52">'
      +'<div style="display:flex;justify-content:space-between;font-size:12px;color:#94a3b8">'
      +'<span>'+esc(nome)+'</span><b style="color:#e2e8f0">'+n+'</b></div>'
      + vgBarra(n, total, '#00ff88')
      +'<div style="font-size:10px;color:#475569;margin-top:2px">'+pct+'%</div></div>';
  }).join('') : '<div class="muted">Nenhum lead.</div>';
}

function vgRenderGrafico(dias){
  const ctx=document.getElementById('vg-chart-dias');
  if(!ctx || typeof Chart==='undefined' || !dias.length) return;
  const rotulo=d=>d.slice(8,10)+'/'+d.slice(5,7);
  if(VG_CHART){ VG_CHART.destroy(); VG_CHART=null; }
  VG_CHART=new Chart(ctx,{
    type:'bar',
    data:{ labels:dias.map(d=>rotulo(d.dia)), datasets:[
      {label:'E-mail',    data:dias.map(d=>d.email),     backgroundColor:'#00ff88'},
      {label:'WhatsApp',  data:dias.map(d=>d.whatsapp),  backgroundColor:'#25d366'},
      {label:'Instagram', data:dias.map(d=>d.instagram), backgroundColor:'#e1306c'},
    ]},
    options:{ responsive:true,
      plugins:{legend:{labels:{color:'#94a3b8',boxWidth:12,font:{size:11}}}},
      scales:{ x:{stacked:true,ticks:{color:'#64748b'},grid:{color:'#1e293b'}},
               y:{stacked:true,beginAtZero:true,ticks:{color:'#64748b'},grid:{color:'#1e293b'}} } }
  });
}

async function recarregarTudo(){
  await recarregarLeads();
  vgCarregar();
  toast('Atualizado');
}

// ── Tab Loaders Atualizados ──────────────────────────────────────────────────
// ── AGENDAMENTO DE ENVIOS ────────────────────────────────────────────────────
// Um agendamento é "toda terça e quinta, às 9h, comece a mandar".
const AG_DIAS = ['seg','ter','qua','qui','sex','sáb','dom'];

async function agCarregar(canal){
  const d = await jget('/api/agendamentos?canal='+canal).catch(()=>({}));
  const lista = (d && d.agendamentos) || [];

  const prox = $('#ag-'+canal+'-proximo');
  if(prox){
    prox.innerHTML = (d && d.proximo)
      ? '<div style="font-size:20px;font-weight:700;color:#00ff88">'+esc(d.proximo.proximo_em)+'</div>'
        +'<div style="font-size:12px;color:#64748b;margin-top:4px">'+esc(d.proximo.resumo)+'</div>'
      : '<div class="muted" style="font-size:13px">Nenhum agendamento ativo neste canal.</div>';
  }

  const box = $('#ag-'+canal+'-lista');
  if(box){
    box.innerHTML = lista.length ? lista.map(a=>{
      const cfg = a.config || {};
      const detalhe = canal==='email'
          ? ('campanha #'+(cfg.campaign_id||'?')+' · intervalo '+(cfg.min_delay||30)+'-'+(cfg.max_delay||90)+'s')
        : canal==='instagram'
          ? ('nicho '+esc(cfg.nicho||'?')+(cfg.conta?' · @'+esc(cfg.conta):' · todas as contas em paralelo'))
        : canal==='aquecimento'
          ? (cfg.acao==='desativar' ? 'fecha o Reels de todas as contas'
             : 'abre o Reels de todas as contas com sessão'+(cfg.fechar_apos_min?' · fecha em '+cfg.fechar_apos_min+' min':''))
          : ('fila "Entrar em contato" · quadro ' + esc(a.quadro_nome||'?'));
      return '<div class="lf-row">'
        +'<div style="flex:1">'
          +'<div style="font-weight:700;font-size:13px;color:'+(a.ativo?'#e2e8f0':'#475569')+'">'+esc(a.resumo)+'</div>'
          +'<div style="font-size:11px;color:#64748b">'+detalhe+'</div>'
          +(a.ultimo_resultado?'<div style="font-size:11px;color:'
             +(String(a.ultimo_resultado).startsWith('ok')?'#00ff88':'#ff4757')
             +';margin-top:2px">último: '+esc(a.ultimo_resultado)+'</div>':'')
        +'</div>'
        +'<div style="display:flex;gap:4px;align-items:center">'
          +'<button class="btn btn-sm" onclick="agRodarAgora('+a.id+",'"+canal+"'"+')">Rodar agora</button>'
          +'<label class="toggle" title="ativo/pausado"><input type="checkbox" '+(a.ativo?'checked':'')
          +' onchange="agAtivar('+a.id+',this.checked,\''+canal+'\')"><span class="slider"></span></label>'
          +'<button class="btn btn-sm btn-danger" onclick="agExcluir('+a.id+",'"+canal+"'"+')">Excluir</button>'
        +'</div>'
      +'</div>';
    }).join('') : '<div class="muted" style="font-size:13px">Nenhum agendamento. Crie o primeiro acima.</div>';
  }

  if(canal==='email')     agPopularEmail();
  if(canal==='instagram') agPopularInstagram();
  if(canal==='whatsapp')  agPopularWhatsapp();
}

// Os seletores do formulário saem do que JÁ está carregado na aba: campanha de
// e-mail vem do pcz, nicho e conta do Instagram vêm do bot. Sem isso o operador
// digitaria um id na mão e descobriria o erro só no dia do disparo.
function agPopularEmail(){
  const sel=$('#ag-email-campanha'); if(!sel) return;
  const camps = (typeof EMAIL!=='undefined' && EMAIL.campanhas) || [];
  sel.innerHTML = camps.length
    ? camps.map(c=>'<option value="'+c.id+'">'+esc(c.name)+' — '+(c.pendentes||0).toLocaleString('pt-BR')+' pendentes</option>').join('')
    : '<option value="">crie uma campanha na aba Disparos</option>';
}

async function agPopularInstagram(){
  const selN=$('#ag-instagram-nicho');
  if(selN && !selN.dataset.pronto){
    const nichos = await jget('/api/ig/nichos').catch(()=>[]);
    selN.innerHTML = (Array.isArray(nichos)&&nichos.length)
      ? nichos.map(n=>'<option value="'+esc(n.nicho||n)+'">'+esc(n.nicho||n)+'</option>').join('')
      : '<option value="">nenhum nicho cadastrado</option>';
    selN.dataset.pronto='1';
  }
  const selC=$('#ag-instagram-conta');
  if(selC && !selC.dataset.pronto){
    const contas = await jget('/api/connect/instagram/status').catch(()=>[]);
    // "" = rodizio por todas: o agendamento roda sem ninguem olhando, e fixar
    // uma conta faria o disparo noturno queimar sempre o mesmo perfil
    selC.innerHTML = '<option value="">Todas as contas (em paralelo)</option>'
      + (Array.isArray(contas)?contas:[]).map(c=>'<option value="'+esc(c.identifier)+'">@'
          +esc(c.identifier)+(c.sessao_ativa?'':' (sem sessão)')+'</option>').join('');
    selC.dataset.pronto='1';
  }
}

// O agendamento de WhatsApp precisa dizer QUAL quadro dispara: ele roda sem
// ninguém olhando, e "o quadro que estiver aberto" muda conforme a véspera.
function agPopularWhatsapp(){
  const sel=$('#ag-whatsapp-quadro'); if(!sel) return;
  const atual = sel.value;
  sel.innerHTML = QUADROS.length
    ? QUADROS.map(q=>'<option value="'+q.id+'">'+esc(q.nome)+' ('+q.leads+' leads)</option>').join('')
    : '<option value="">nenhum quadro</option>';
  if(atual) sel.value = atual;
  else if(QUADRO_ATIVO) sel.value = QUADRO_ATIVO;
}

function agDiasMarcados(canal){
  return $$('.ag-'+canal+'-dia').filter(c=>c.checked).map(c=>c.value).join(',');
}

async function agCriar(canal){
  const hora=$('#ag-'+canal+'-hora').value;
  const dias=agDiasMarcados(canal);
  if(!hora){ toast('Escolha o horário'); return; }
  if(!dias){ toast('Marque ao menos um dia'); return; }

  const cfg={};
  if(canal==='email'){
    cfg.campaign_id=parseInt($('#ag-email-campanha').value)||0;
    if(!cfg.campaign_id){ toast('Escolha a campanha'); return; }
    cfg.min_delay=parseInt($('#ag-email-min').value)||30;
    cfg.max_delay=parseInt($('#ag-email-max').value)||90;
  }
  if(canal==='whatsapp'){
    cfg.quadro_id=parseInt($('#ag-whatsapp-quadro').value)||0;
    if(!cfg.quadro_id){ toast('Escolha o quadro'); return; }
  }
  if(canal==='instagram'){
    cfg.nicho=$('#ag-instagram-nicho').value;
    cfg.conta=$('#ag-instagram-conta').value;
    cfg.mensagem=$('#ag-instagram-mensagem').value.trim();
    if(!cfg.nicho){ toast('Escolha o nicho'); return; }
    if(!cfg.mensagem){ toast('Escreva a mensagem'); return; }
  }

  if(canal==='aquecimento'){
    cfg.acao=$('#ag-aquecimento-acao').value;
    const dur=parseInt($('#ag-aquecimento-duracao').value)||0;
    if(cfg.acao==='ativar'&&dur) cfg.fechar_apos_min=dur;
  }

  const r=await jsend('/api/agendamentos','POST',{canal,hora,dias,config:cfg});
  toast(r.ok?'Agendamento criado':(r.erro||'Não deu para criar'));
  if(r.ok) agCarregar(canal);
}

async function agAtivar(id, ativo, canal){
  const r=await jsend('/api/agendamentos/'+id,'PUT',{ativo:ativo?1:0});
  toast(r.ok?(ativo?'Agendamento ativado':'Agendamento pausado'):(r.erro||'Falhou'));
  agCarregar(canal);
}

async function agExcluir(id, canal){
  const r=await jsend('/api/agendamentos/'+id,'DELETE');
  toast(r.ok?'Agendamento excluído':(r.erro||'Falhou'));
  agCarregar(canal);
}

// Rodar agora existe para conferir o encanamento sem esperar o horário — e
// manda de verdade, então pergunta antes.
async function agRodarAgora(id, canal){
  toast('Disparando...');
  const r=await jsend('/api/agendamentos/'+id+'/rodar','POST');
  toast(r.ok?(r.mensagem||'Disparo iniciado'):(r.erro||'Não deu para disparar'));
  agCarregar(canal);
}

// Qual sub-aba do WhatsApp está aberta. O Kanban virou sub-aba daqui: o funil
// É a fila do WhatsApp, e mantê-lo numa aba separada obrigava a ir e voltar
// entre "quem contatar" e "o bot está de pé?".
let WA_SUB = 'funil';

function waLoadSubpane(sub){
  WA_SUB = sub || 'funil';
  if(WA_SUB==='funil')     { recarregarLeads(); quadrosCarregar(); return; }
  if(WA_SUB==='mensagens') { carregarCampanhas(); carregarTemplates();
                             carregarTemplatesCanal(); carregarLeadsProspeccao(); return; }
  if(WA_SUB==='raspagem')    { carregarScrap(); return; }
  if(WA_SUB==='agendamento') { agCarregar('whatsapp'); return; }
  if(WA_SUB==='contas')      { accCarregar();     return; }
}

const TAB_LOADERS={
  dashboard: ()=>{ vgCarregar(); },
  whatsapp: ()=>{ waLoadSubpane(WA_SUB); },
  instagram: ()=>{ igCarregarTudo(); },
  emails: ()=>{ emailCarregarTudo(); },
  config: ()=>{ cfgCarregar(); },
  chat: ()=>{ chatStart(); },
  aquecimento: ()=>{ aqStart(); },
};

// ── Aquecimento manual do Instagram ─────────────────────────────────────────
function aqStart(){
  aqCarregar();
  agCarregar('aquecimento');
  if(AQ_TIMER) clearInterval(AQ_TIMER);
  AQ_TIMER=setInterval(()=>{
    if($('#pane-aquecimento')?.classList.contains('active')) aqCarregar(true);
  },5000);
}
async function aqCarregar(quiet=false){
  const root=$('#aq-accounts'); if(!root) return;
  try{
    AQ_STATE=await jget('/api/aquecimento/instagram');
    const accounts=Array.isArray(AQ_STATE.contas)?AQ_STATE.contas:[];
    // Uma conta tem uma janela só, que pode estar no Reels ou no Direct do
    // Chat ao vivo; aqui o switch liga só quando ela está no Reels.
    const janelas=new Map((AQ_STATE.active||[]).filter(x=>!x.embutida).map(x=>[String(x.conta||'').toLowerCase(),x.destino]));
    const fecha=new Map((AQ_STATE.active||[]).filter(x=>x.fecha_em_min!=null).map(x=>[String(x.conta||'').toLowerCase(),x.fecha_em_min]));
    const noPainel=new Set((AQ_STATE.active||[]).filter(x=>x.embutida).map(x=>String(x.conta||'').toLowerCase()));
    const active=new Set([...janelas].filter(([,d])=>d==='reels').map(([c])=>c));
    $('#aq-status').textContent=AQ_STATE.online
      ? `${active.size} sessão(ões) aberta(s)`
      : 'Bot Instagram indisponível';
    root.innerHTML=accounts.length?accounts.map(a=>{
      const id=String(a.identifier||'');
      const on=active.has(id.toLowerCase());
      const painel=noPainel.has(id.toLowerCase());
      const outra=!on&&!painel&&janelas.has(id.toLowerCase());
      const busy=(!!a.perfil_aberto&&!on&&!outra)||painel;
      // com a janela aberta o arquivo de cookies fica travado: conta aberta é conta logada
      const enabled=!!a.sessao_ativa||outra;
      const disabled=on?false:(!enabled||busy||!AQ_STATE.online);
      const resta=fecha.get(id.toLowerCase());
      const state=on?(AQ_STATE.online?'Reels aberto · prospecção bloqueada'+(resta!=null?` · fecha em ${resta} min`:''):'Reels aberto · bot offline; feche a janela manualmente')
        :painel?'Aberta no Chat ao vivo · feche lá para usar o Reels'
        :outra?'Janela aberta em outra página · ative para levar ao Reels'
        :!enabled?'Sem sessão autenticada'
        :busy?'Perfil já está aberto em outro navegador'
        :'Sessão pronta para uso manual';
      const label=`Abrir ou fechar sessão Reels de @${id}`;
      return `<div class="aq-account ${on?'active':''}">
        <div><div class="aq-account-name">${esc(a.display_name||'@'+id)}</div>
          <div class="aq-account-state">@${esc(id)} · ${esc(state)}</div></div>
        <div class="aq-account-control"><span class="muted">${on?'Ativo':'Inativo'}</span>
          <label class="toggle"><input type="checkbox" role="switch" data-aq-conta="${esc(id)}" aria-label="${esc(label)}" ${on?'checked':''} ${disabled?'disabled':''} onchange="aqAlternar(this)"><span class="slider"></span></label>
        </div></div>`;
    }).join(''):'<div class="muted">Nenhuma conta Instagram cadastrada.</div>';
  }catch(e){
    if(!quiet) $('#aq-status').textContent='Não foi possível carregar as contas.';
    root.innerHTML='<div class="muted">Falha ao consultar o bot de Instagram.</div>';
  }
}
async function aqAlternar(input){
  const conta=input.dataset.aqConta, wanted=input.checked;
  input.disabled=true;
  const corpo={conta,ativo:wanted};
  const dur=parseInt(($('#aq-duracao')||{}).value)||0;
  if(wanted&&dur) corpo.fechar_apos_min=dur;
  const result=await jsend('/api/aquecimento/instagram','POST',corpo);
  if(!result.ok){ input.checked=!wanted; toast(result.erro||'Não foi possível alterar o aquecimento.'); }
  else toast(result.mensagem||(wanted?'Sessão aberta.':'Sessão encerrada.'));
  await aqCarregar(true);
}

// Todas as contas de uma vez. Quem espaça as aberturas é o bot (uma por vez):
// as janelas vão aparecendo nos próximos minutos.
async function aqTodas(ativo){
  const corpo={ativo};
  const dur=parseInt(($('#aq-duracao')||{}).value)||0;
  if(ativo&&dur) corpo.fechar_apos_min=dur;
  if(!ativo&&!confirm('Fechar o Reels de todas as contas?')) return;
  const r=await jsend('/api/aquecimento/instagram/todas','POST',corpo);
  let msg=r.mensagem||r.erro||'Sem resposta do painel.';
  if((r.puladas||[]).length) msg+=' — '+r.puladas.slice(0,3).map(p=>'@'+p.conta+': '+p.motivo).join('; ');
  toast(msg);
  await aqCarregar(true);
}

// ── Chat ao vivo manual ─────────────────────────────────────────────────────
function chatSetChannel(channel){
  CHAT_CHANNEL=channel;
  $$('.chat-filter').forEach(b=>b.classList.toggle('active',b.dataset.channel===channel));
  chatLoad();
}
function chatStart(){
  chatLoad();
  igOfCarregar(true);
  if(CHAT_TIMER) clearInterval(CHAT_TIMER);
  CHAT_TIMER=setInterval(()=>{
    if(chatSubpaneAtiva('conversas')) chatLoad(true);
  },4000);
  if(chatSubpaneAtiva('direct')) igdStart();
}
function chatSubpaneAtiva(nome){
  return !!($('#pane-chat')?.classList.contains('active')
    && $('#subpane-chat-'+nome)?.classList.contains('active'));
}
function chatLoadSubpane(nome){
  // O cabeçalho (contagem e Atualizar) é das conversas; no Direct a subaba tem o seu.
  const acoes=$('#pane-chat .hdr-actions'); if(acoes) acoes.style.display=nome==='direct'?'none':'';
  if(nome==='direct') igdStart(); else chatLoad();
}

// ── Instagram Direct dentro do painel (v024) ──────────────────────────────── O
// bot abre o perfil logado da conta num Chromium FORA DA TELA e transmite a
// página (MJPEG em /tela) enquanto alguém olha.
let IGD_ATUAL='', IGD_FILA=[], IGD_ENVIANDO=false, IGD_CARREGANDO=false, IGD_TENTATIVAS=0;
let IGD_PAUSADO=false, IGD_SEGURAR_ENTER=false;
// mesmas teclas que janela_remota.TECLAS / ATALHOS (test_janela_manual.py [12])
const IGD_TECLAS=new Set(['Enter','Backspace','Tab','Escape','Delete','Home','End','PageUp','PageDown',
  'ArrowLeft','ArrowUp','ArrowRight','ArrowDown']);
const IGD_ATALHOS=['a','z','y'];
function igdStart(){
  igdCarregar();
  if(IGD_TIMER) clearInterval(IGD_TIMER);
  IGD_TIMER=setInterval(()=>{ if(chatSubpaneAtiva('direct')&&!document.hidden) igdCarregar(true); },5000);
}
async function igdCarregar(quiet=false){
  if(IGD_CARREGANDO) return;              // consulta anterior ainda no ar: não empilha
  IGD_CARREGANDO=true;
  try{ IGD_STATE=await jget('/api/chat/instagram-direct'); igdAvisarFalhas(IGD_STATE.falhas); }
  catch(e){ if(!quiet) $('#igd-status').textContent='Não foi possível consultar as contas.'; return; }
  finally{ IGD_CARREGANDO=false; }
  igdRender();
}
// Não lidas da conta: o número do ícone do Direct do Instagram cobre a lista
// toda (ela só desenha o que cabe na tela) e o que se vê na lista pode passar
// dele — mostra o maior, para nunca dizer menos do que está na tela.
function igdNaoLidas(ct){
  if(!ct) return 0;
  return Math.max(Number(ct.marca_instagram)||0, Number(ct.nao_lidas)||0);
}
// "há 4 min" de um ISO
function igdHa(iso){
  const s=(Date.now()-new Date(iso).getTime())/1000;
  if(!(s>=0)) return 'pouco';
  return s<90?'1 min':s<3600?`${Math.round(s/60)} min`:`${Math.round(s/3600)} h`;
}
// conta que não conseguiu abrir: o bot guarda o motivo por 3 min; avisa uma vez
const IGD_FALHAS_AVISADAS={};
function igdAvisarFalhas(falhas){
  for(const [c,f] of Object.entries(falhas||{})){
    const em=f&&f.em, motivo=(f&&f.motivo)||f;
    if(IGD_FALHAS_AVISADAS[c]===em) continue;     // a mesma falha de novo avisa de novo
    IGD_FALHAS_AVISADAS[c]=em;
    toast(`@${c} não abriu no painel: ${motivo}`);
  }
}
function igdChave(c){ return String(c||'').toLowerCase().replace(/^@/,''); }
function igdJanelas(){ return new Map((IGD_STATE.active||[]).map(x=>[igdChave(x.conta),x])); }
function igdRender(){
  const root=$('#igd-contas'); if(!root) return;
  const st=IGD_STATE||{}, contas=Array.isArray(st.contas)?st.contas:[];
  const janelas=igdJanelas();
  // Com a janela aberta o Chromium trava o arquivo de cookies e a leitura de
  // sessão falha: a conta aberta conta como logada, senão sumiria da lista.
  const logadas=contas.filter(c=>c.sessao_ativa||c.perfil_aberto||janelas.has(igdChave(c.identifier)));
  const semSessao=contas.length-logadas.length;
  const busca=igdChave(($('#igd-busca')?.value||'').trim());
  const visiveis=logadas.filter(c=>!busca||igdChave(c.identifier).includes(busca)
      ||String(c.display_name||'').toLowerCase().includes(busca))
    // contas abertas primeiro: é para elas que se volta a toda hora
    .sort((a,b)=>janelas.has(igdChave(b.identifier))-janelas.has(igdChave(a.identifier)));
  const noPainel=[...janelas.values()].filter(j=>j.embutida).length;
  $('#igd-status').textContent=!st.online?'Bot de Instagram desligado'
    :`${logadas.length} logada(s) · ${noPainel} aberta(s)${st.prospeccao_rodando?' · prospecção rodando':''}`;
  $('#igd-fechar-todas').disabled=!st.online||!noPainel;
  root.innerHTML=visiveis.length?visiveis.map(c=>{
    const id=igdChave(c.identifier), j=janelas.get(id);
    const aberta=!!(j&&j.embutida), externa=!!(j&&!j.embutida);
    const bloqueio=!st.online?'bot desligado'
      :externa?'aberta em janela externa'
      :(!aberta&&(st.contas_prospectando||[]).includes(id))?'prospectando agora'
      :(!j&&c.perfil_aberto)?'perfil em uso':'';
    const novas=aberta?igdNaoLidas(j.contagem):0;
    const sono=aberta&&j.dormindo?'dormindo':'';
    const quando=aberta&&j.dormindo&&j.contagem_em?` (há ${igdHa(j.contagem_em)})`:'';
    const estado=bloqueio||(aberta&&j.aviso?j.aviso:'')||[sono,aberta?(j.contagem?`${novas} não lida(s)${quando}`:'aberta'):''].filter(Boolean).join(' · ');
    return `<button class="igd-conta ${aberta?'aberta':''} ${IGD_ATUAL===id?'atual':''}" type="button"
        data-igd-conta="${esc(id)}" ${bloqueio&&!aberta?'disabled':''} aria-label="Abrir o Direct de @${esc(id)}">
      <span class="igd-ponto" aria-hidden="true"></span>
      <span class="igd-conta-txt"><span class="igd-nome">${esc(c.display_name||'@'+id)}</span>
        <span class="igd-estado">@${esc(id)}${estado?' · '+esc(estado):''}</span></span>
      ${novas?`<b class="chat-unread">${Number(novas)||0}</b>`:''}
    </button>`;
  }).join(''):`<div class="muted igd-rodape">${logadas.length?'Nenhuma conta com esse nome.':'Nenhuma conta do Instagram com sessão salva.'}</div>`;
  $('#igd-rodape').textContent=semSessao?`${semSessao} conta(s) sem sessão salva não aparecem — faça o login em Instagram › Perfis.`:'';
  // a conta da tela fechou por fora (janela caiu, Fechar todas): limpa a tela
  if(IGD_ATUAL && st.online && !(janelas.get(IGD_ATUAL)||{}).embutida) igdMostrar('');
  else igdBarra();
}
function igdBarra(){
  const j=igdJanelas().get(IGD_ATUAL);
  $('#igd-barra').hidden=!IGD_ATUAL;
  if(!IGD_ATUAL) return;
  $('#igd-titulo').textContent='@'+IGD_ATUAL;
  const ct=j&&j.contagem;
  const filtro=j&&j.so_nao_lidas?' · filtro: só não lidas':'';
  $('#igd-contagem').textContent=(!j?' · abrindo…'
    :j.destino!=='direct'?' · fora da caixa de entrada'
    :ct?` · ${igdNaoLidas(ct)} não lida(s)${ct.marca_instagram!=null&&ct.marca_instagram>ct.nao_lidas?` (${ct.nao_lidas} nesta parte da lista)`:''}${filtro}`:'')
    +(j&&j.aviso?` · ${j.aviso}`:'');
}
function igdUrlTela(id){ return `/api/chat/instagram-direct/${encodeURIComponent(id)}/tela?t=${Date.now()}`; }
// Desliga a imagem quando ninguém está vendo a subaba e religa ao voltar.
function igdVisivel(){ return chatSubpaneAtiva('direct')&&!document.hidden; }
function igdConferirVisibilidade(){
  const img=$('#igd-img'); if(!img||!IGD_ATUAL) return;
  if(!igdVisivel()&&!IGD_PAUSADO){ IGD_PAUSADO=true; img.removeAttribute('src'); }
  else if(igdVisivel()&&IGD_PAUSADO){ IGD_PAUSADO=false; IGD_TENTATIVAS=0; img.src=igdUrlTela(IGD_ATUAL); }
}
document.addEventListener('visibilitychange',igdConferirVisibilidade);
setInterval(igdConferirVisibilidade,1000);
// Põe a conta na tela (ou limpa, com ''). A imagem é um fluxo contínuo:
// trocar o src fecha o fluxo da conta anterior.
function igdMostrar(id){
  IGD_ATUAL=id; IGD_FILA=[]; IGD_TENTATIVAS=0; IGD_PAUSADO=false; IGD_SEGURAR_ENTER=false;
  igdFecharPlayer();
  if(id&&IGD_MIDIA_SEQS[id]==null){ IGD_MIDIA_SEQS[id]=Infinity; igdMarcarMidiaVista(id); }
  const img=$('#igd-img'), vazio=$('#igd-vazio');
  if(!id){
    img.removeAttribute('src'); img.hidden=true;
    vazio.hidden=false; vazio.textContent='Escolha uma conta à esquerda e o Direct dela abre aqui.';
  }else{
    vazio.hidden=false; vazio.textContent=`Abrindo o Direct de @${id}…`; img.hidden=true;
    img.src=igdUrlTela(id);
    // o 'load' de um fluxo MJPEG nem sempre dispara: confere o 1º quadro pelo tamanho
    const vigia=setInterval(()=>{
      if(IGD_ATUAL!==id){ clearInterval(vigia); return; }
      if(img.naturalWidth>1){ clearInterval(vigia); igdPrimeiroQuadro(); }
    },300);
    setTimeout(()=>clearInterval(vigia),60000);
  }
  $$('#igd-contas .igd-conta').forEach(b=>b.classList.toggle('atual',b.dataset.igdConta===id));
  igdBarra();
}
async function igdAbrir(id){
  const j=igdJanelas().get(id);
  if(!(j&&j.embutida)){
    // o filtro vai junto: vale desde o carregamento, mesmo antes do 1º quadro
    const r=await jsend('/api/chat/instagram-direct','POST',{conta:id,ativo:true,
      so_nao_lidas:!!$('#igd-so-nao-lidas')?.checked});
    if(!r.ok){ toast(r.erro||'Não foi possível abrir o Direct desta conta.'); return; }
  }
  igdMostrar(id);
  igdCarregar(true);
}
async function igdFechar(id){
  if(!id) return;
  if(IGD_ATUAL===id) igdMostrar('');
  const r=await jsend('/api/chat/instagram-direct','POST',{conta:id,ativo:false});
  if(!r.ok) toast(r.erro||'Não foi possível fechar.');
  await igdCarregar(true);
}
async function igdFecharTodas(){
  const contas=(IGD_STATE.active||[]).filter(x=>x.embutida).map(x=>x.conta);
  if(!contas.length) return;
  $('#igd-fechar-todas').disabled=true;
  igdMostrar('');
  await Promise.all(contas.map(conta=>jsend('/api/chat/instagram-direct','POST',{conta,ativo:false})));
  toast(`Fechando ${contas.length} conta(s).`);
  await igdCarregar(true);
}
// ── Áudio/vídeo do Direct: o painel toca (a janela da conta não tem os codecs)
// Depois de um clique na tela, pergunta ao bot se a página deu play em alguma
// mídia; se deu, toca aqui pelo player sobre a conta. o último play já visto,
// POR CONTA: voltar para uma conta (ou recarregar a página) não pode tocar de
// novo o áudio que já tocou
let IGD_MIDIA_SEQS={}, IGD_MIDIA_EPOCAS={}, IGD_MIDIA_TIMERS=[];
async function igdMarcarMidiaVista(conta){
  let seq=0, epoca=null;
  try{
    const d=await jget(`/api/chat/instagram-direct/${encodeURIComponent(conta)}/midia`);
    seq=(d&&d.midia&&d.midia.seq)||0; epoca=(d&&d.epoca)||null;
  }catch(e){}
  // o que já tocou antes de a conta aparecer na tela (ou antes do F5) não toca de novo
  IGD_MIDIA_SEQS[conta]=seq; IGD_MIDIA_EPOCAS[conta]=epoca;
}
function igdOuvirMidia(){
  IGD_MIDIA_TIMERS.forEach(clearTimeout);
  const conta=IGD_ATUAL;
  IGD_MIDIA_TIMERS=[600,1400,2600,4200].map(ms=>setTimeout(async()=>{
    if(conta!==IGD_ATUAL) return;
    let d; try{ d=await jget(`/api/chat/instagram-direct/${encodeURIComponent(conta)}/midia`); }catch(e){ return; }
    // conta reaberta ou bot reiniciado: a contagem do bot recomeçou do 1, e
    // comparar com a antiga deixava todo áudio mudo até passar dela
    if(d&&d.epoca&&IGD_MIDIA_EPOCAS[conta]!==undefined&&d.epoca!==IGD_MIDIA_EPOCAS[conta]){
      IGD_MIDIA_EPOCAS[conta]=d.epoca; IGD_MIDIA_SEQS[conta]=0;
    }
    const m=d&&d.midia; if(!m||m.seq<=(IGD_MIDIA_SEQS[conta]||0)) return;
    IGD_MIDIA_SEQS[conta]=m.seq;
    if(!m.id){ toast(m.erro||'Esta mídia não pode ser tocada pelo painel.'); return; }
    igdTocar(conta,m);
  },ms));
}
function igdTocar(conta,m){
  const caixa=$('#igd-player'), lugar=$('#igd-player-midia');
  const url=`/api/chat/instagram-direct/${encodeURIComponent(conta)}/midia/${encodeURIComponent(m.id)}`;
  lugar.innerHTML='';
  const el=document.createElement(m.tipo==='video'?'video':'audio');
  el.controls=true; el.autoplay=true; el.src=url;
  el.className=m.tipo==='video'?'igd-player-video':'igd-player-audio';
  el.onerror=()=>toast('Não consegui tocar esta mídia do Instagram.');
  lugar.appendChild(el);
  $('#igd-player-rotulo').textContent=m.tipo==='video'?'🎬 Vídeo do Instagram':'🔊 Áudio do Instagram';
  caixa.hidden=false;
  el.play().catch(()=>{});
}
function igdFecharPlayer(){
  const lugar=$('#igd-player-midia'); if(lugar) lugar.innerHTML='';
  const caixa=$('#igd-player'); if(caixa) caixa.hidden=true;
}

function igdPrimeiroQuadro(){
  const img=$('#igd-img');
  if(!IGD_ATUAL||!img.hidden) return;
  img.hidden=false; $('#igd-vazio').hidden=true;
  igdAjustarTamanho();
  igdEnviar({t:'filtro',v:$('#igd-so-nao-lidas').checked});
}
// A página da conta toma o tamanho da área do painel (texto em tamanho real).
function igdAjustarTamanho(){
  const t=$('#igd-tela'); if(!IGD_ATUAL||!t) return;
  igdEnviar({t:'tamanho',w:Math.round(t.clientWidth),h:Math.round(t.clientHeight)});
}
let IGD_RESIZE=null;
window.addEventListener('resize',()=>{ clearTimeout(IGD_RESIZE); IGD_RESIZE=setTimeout(igdAjustarTamanho,300); });
function lfLembrar(chave,valor){ try{ localStorage.setItem(chave,valor?'1':'0'); }catch(e){} }
function lfLembrado(chave){ try{ return localStorage.getItem(chave)==='1'; }catch(e){ return false; } }
function igdFiltro(){
  const v=$('#igd-so-nao-lidas').checked;
  lfLembrar('lf_igd_nao_lidas',v);
  if(IGD_ATUAL) igdEnviar({t:'filtro',v});
}
(function(){
  const ig=$('#igd-so-nao-lidas'); if(ig) ig.checked=lfLembrado('lf_igd_nao_lidas');
  const wa=$('#chat-only-unread');
  if(wa){ wa.checked=lfLembrado('lf_chat_nao_lidas');
          wa.addEventListener('change',()=>lfLembrar('lf_chat_nao_lidas',wa.checked)); }
})();

// ── Entrada: tudo passa por uma fila, um pedido por vez, para chegar na ordem
function igdEnviar(ev){
  if(!IGD_ATUAL) return;
  if(ev.t==='tecla'&&ev.k==='Enter'&&IGD_SEGURAR_ENTER){
    // parte do que foi digitado não chegou: um Enter agora mandaria a mensagem cortada
    IGD_SEGURAR_ENTER=false;
    toast('Parte do que você digitou não chegou ao Instagram. Confira a mensagem na tela e aperte Enter de novo.');
    return;
  }
  const ult=IGD_FILA[IGD_FILA.length-1];
  if(ev.t==='mouse'&&ev.a==='move'&&ult&&ult.t==='mouse'&&ult.a==='move') IGD_FILA[IGD_FILA.length-1]=ev;
  else if(ev.t==='texto'&&ult&&ult.t==='texto') ult.s+=ev.s;
  else IGD_FILA.push(ev);
  igdDescarregar();
}
async function igdDescarregar(){
  if(IGD_ENVIANDO||!IGD_FILA.length||!IGD_ATUAL) return;
  IGD_ENVIANDO=true;
  const conta=IGD_ATUAL, lote=IGD_FILA.splice(0,200);
  let falhou=false;
  try{
    const r=await jsend(`/api/chat/instagram-direct/${encodeURIComponent(conta)}/entrada`,'POST',{eventos:lote});
    if(!r.ok){ falhou=true; if(conta===IGD_ATUAL) toast(r.erro||'A conta não recebeu o comando.'); }
  }catch(e){ falhou=true; }
  finally{
    if(falhou&&conta===IGD_ATUAL){
      // o que vinha depois dependia do que se perdeu: não manda pela metade
      IGD_FILA=[];
      if(lote.some(ev=>ev.t==='texto')) IGD_SEGURAR_ENTER=true;
    }
    IGD_ENVIANDO=false; if(IGD_FILA.length) igdDescarregar();
  }
}
// Posição do mouse na página do bot (0..1), descontando as faixas do "contain".
function igdPonto(e){
  const img=$('#igd-img'), r=img.getBoundingClientRect();
  const nw=img.naturalWidth||r.width, nh=img.naturalHeight||r.height;
  const k=Math.min(r.width/nw, r.height/nh), w=nw*k, h=nh*k;
  const x=(e.clientX-r.left-(r.width-w)/2)/w, y=(e.clientY-r.top-(r.height-h)/2)/h;
  return {x:Math.max(0,Math.min(1,x)), y:Math.max(0,Math.min(1,y))};
}
function igdMod(e){ return (e.altKey?1:0)|(e.ctrlKey?2:0)|(e.metaKey?4:0)|(e.shiftKey?8:0); }
(function igdLigarEntrada(){
  const img=$('#igd-img'), tecl=$('#igd-teclado'); if(!img||!tecl) return;
  let ultMove=0;
  img.addEventListener('load',()=>{ if(img.naturalWidth>1) igdPrimeiroQuadro(); });
  img.addEventListener('error',()=>{
    // a janela ainda não registrou ou o fluxo caiu: tenta de novo algumas vezes
    if(!IGD_ATUAL||IGD_PAUSADO||IGD_TENTATIVAS>=8) return;
    IGD_TENTATIVAS++;
    const id=IGD_ATUAL;
    setTimeout(()=>{ if(IGD_ATUAL===id&&!IGD_PAUSADO) img.src=igdUrlTela(id); },1500);
  });
  img.addEventListener('mousedown',e=>{
    e.preventDefault(); tecl.focus({preventScroll:true});
    igdEnviar({t:'mouse',a:'down',b:e.button===2?'right':'left',n:e.detail||1,...igdPonto(e)});
  });
  let apertado=false;
  img.addEventListener('mousedown',()=>{ apertado=true; },true);
  // soltar fora da imagem também solta na página (senão o botão fica "preso"
  // e todo movimento seguinte vira arrastar/selecionar)
  window.addEventListener('mouseup',e=>{
    if(!apertado) return; apertado=false;
    igdEnviar({t:'mouse',a:'up',b:e.button===2?'right':'left',n:e.detail||1,...igdPonto(e)});
    igdOuvirMidia();
  });
  $('#igd-player-fechar')?.addEventListener('click',igdFecharPlayer);
  img.addEventListener('mousemove',e=>{
    const agora=performance.now(); if(agora-ultMove<(apertado?40:100)) return; ultMove=agora;
    igdEnviar({t:'mouse',a:'move',...igdPonto(e)});
  });
  img.addEventListener('wheel',e=>{
    e.preventDefault();
    const f=e.deltaMode===1?40:e.deltaMode===2?(img.naturalHeight||800):1;
    igdEnviar({t:'roda',dx:e.deltaX*f,dy:e.deltaY*f,...igdPonto(e)});
  },{passive:false});
  img.addEventListener('contextmenu',e=>e.preventDefault());
  // Teclado: um <textarea> escondido recebe o foco. Texto (inclusive acento,
  // emoji e colar) sai como texto; Enter, setas e afins, como tecla.
  tecl.addEventListener('keydown',e=>{
    if(e.isComposing) return;
    const ctrl=e.ctrlKey||e.metaKey;
    if(IGD_TECLAS.has(e.key)){ e.preventDefault(); igdEnviar({t:'tecla',k:e.key,mod:igdMod(e)}); return; }
    if(ctrl&&IGD_ATALHOS.includes(e.key.toLowerCase())){ e.preventDefault(); igdEnviar({t:'tecla',k:e.key.toLowerCase(),mod:igdMod(e)}); }
  });
  tecl.addEventListener('input',e=>{
    if(e.isComposing) return;
    if(tecl.value){ igdEnviar({t:'texto',s:tecl.value}); tecl.value=''; }
  });
  tecl.addEventListener('compositionend',e=>{
    if(e.data) igdEnviar({t:'texto',s:e.data}); tecl.value='';
  });
  tecl.addEventListener('paste',e=>{
    e.preventDefault();
    const s=(e.clipboardData||window.clipboardData).getData('text');
    if(s) igdEnviar({t:'texto',s});
  });
  tecl.addEventListener('focus',()=>$('#igd-tela').classList.add('focada'));
  tecl.addEventListener('blur',()=>$('#igd-tela').classList.remove('focada'));
  $('#igd-contas')?.addEventListener('click',e=>{
    const b=e.target.closest('button[data-igd-conta]'); if(b) igdAbrir(b.dataset.igdConta);
  });
  $('#igd-barra')?.addEventListener('click',e=>{
    const n=e.target.closest('button[data-igd-nav]');
    if(n){ igdEnviar({t:'nav',a:n.dataset.igdNav}); return; }
    if(e.target.closest('#igd-fechar-atual')) igdFechar(IGD_ATUAL);
  });
})();
async function chatLoad(quiet=false){
  if(!$('#chat-list')) return;
  const params=new URLSearchParams();
  if(CHAT_CHANNEL) params.set('channel',CHAT_CHANNEL);
  const account=$('#chat-account-filter').value;
  if(account) params.set('account',account);
  const search=$('#chat-search').value.trim();
  if(search) params.set('search',search);
  if($('#chat-only-unread')?.checked){ params.set('unread','1'); if(CHAT_SELECTED) params.set('keep',CHAT_SELECTED); }
  try{
    const d=await jget('/api/chat/conversations?'+params.toString());
    CHAT_THREADS=d.conversations||[];
    const select=$('#chat-account-filter'), chosen=select.value;
    const accounts=[...new Set(CHAT_THREADS.map(t=>t.account))].sort();
    select.innerHTML='<option value="">Todas as contas</option>'+accounts.map(a=>`<option value="${esc(a)}">${esc(a)}</option>`).join('');
    if(accounts.includes(chosen)) select.value=chosen;
    const naoLidas=CHAT_THREADS.filter(t=>t.unread_count>0).length;
    const so=$('#chat-only-unread')?.checked;
    $('#chat-list').innerHTML=CHAT_THREADS.length?CHAT_THREADS.map(t=>{
      const name=t.display_name||t.peer_id;
      const channel=t.channel==='whatsapp'?'WhatsApp':'Instagram';
      return `<button class="chat-item ${CHAT_SELECTED===t.id?'active':''} ${t.unread_count?'nova':''}" onclick="chatSelect(${t.id})">
        <div class="chat-item-top"><strong>${esc(name)}</strong><time>${esc(chatQuando(t.last_message_at))}</time></div>
        <div class="chat-item-mid"><span class="chat-preview">${esc(t.last_message_preview)}</span>${t.unread_count?`<b class="chat-unread">${t.unread_count}</b>`:''}</div>
        <div class="chat-item-meta"><span class="chat-channel-badge">${channel}</span><span>${esc(t.account)}${chatSituacao(t)}</span></div></button>`;
    }).join(''):`<div class="muted" style="padding:12px">${so?'Nenhuma conversa com mensagem nova.':'Nenhuma conversa recebida ainda.'}</div>`;
    const tot=$('#chat-unread-total');
    if(tot){ const n=so?naoLidas:CHAT_THREADS.filter(t=>t.unread_count>0).length; tot.textContent=n; tot.hidden=!n; }
    $('#chat-status').textContent=`${CHAT_THREADS.length} conversa(s)${so?' não lida(s)':''} · atualização automática`;
    if(CHAT_SELECTED && CHAT_THREADS.some(t=>t.id===CHAT_SELECTED)){
      chatPodeEnviar(CHAT_THREADS.find(t=>t.id===CHAT_SELECTED));
      await chatHistory(quiet);
    }
    else if(CHAT_SELECTED && !CHAT_THREADS.some(t=>t.id===CHAT_SELECTED)) chatSelect(null);
  }catch(e){ if(!quiet) $('#chat-status').textContent='Não foi possível carregar as conversas.'; }
}
function chatSelect(id){
  CHAT_SELECTED=id;
  const t=CHAT_THREADS.find(x=>x.id===id);
  const empty=$('#chat-empty'), view=$('#chat-conversation');
  if(!t){ empty.hidden=false; view.hidden=true; CHAT_SELECTED=null; return; }
  empty.hidden=true; view.hidden=false;
  $('#chat-peer').textContent=t.display_name||t.peer_id;
  $('#chat-account').textContent=`${t.channel==='whatsapp'?'+'+t.peer_id+' · ':''}pela conta ${t.account}`;
  $('#chat-channel-badge').textContent=t.channel==='whatsapp'?'WhatsApp':'Instagram';
  chatPodeEnviar(t);
  chatLoad();
}
// Hora na lista, como no WhatsApp: hoje mostra a hora, antes disso a data.
function chatQuando(iso){
  const d=new Date(iso||''); if(isNaN(d)) return '';
  const hoje=new Date(), ontem=new Date(); ontem.setDate(hoje.getDate()-1);
  if(d.toDateString()===hoje.toDateString()) return d.toLocaleTimeString('pt-BR',{hour:'2-digit',minute:'2-digit'});
  if(d.toDateString()===ontem.toDateString()) return 'ontem';
  return d.toLocaleDateString('pt-BR',{day:'2-digit',month:'2-digit'});
}
// Hora curta de um ISO, para "responde até 14:32".
function chatHora(iso){
  const d=new Date(iso||'');
  return isNaN(d)?'':d.toLocaleString('pt-BR',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'});
}
// Rodapé do item da lista: o que impede (ou até quando dá para) responder.
function chatSituacao(t){
  if(t.channel==='instagram'){
    if(t.available) return ` · responde até ${chatHora(t.window_closes_at)}`;
    return ` · <span class="fechada">${t.api_connected?'janela de 24h fechada':'sem API oficial'}</span>`;
  }
  return t.available?'':' · sessão indisponível';
}
// Liga/desliga a caixa de texto conforme a conversa permite enviar.
function chatPodeEnviar(t){
  const box=$('#chat-text'), pode=!!(t&&t.available);
  box.disabled=!pode; $('#chat-compose button').disabled=!pode;
  box.placeholder=pode?'Escreva uma resposta…':(t&&t.unavailable_reason)||'Não é possível enviar por esta conversa.';
  const nota=$('#chat-note');
  if(nota) nota.textContent=t&&t.channel==='instagram'
    ?'Atendimento manual · sai pela API oficial, pela conta desta conversa · até 1.000 bytes por mensagem.'
    :'Atendimento manual · a resposta será enviada pela conta desta conversa.';
}
// Foto, áudio, vídeo e documento (v024). O arquivo vem de /api/chat/mensagens/<id>/midia,
// que na primeira vez o busca no WhatsApp; áudio e vídeo só baixam ao dar play.
const CHAT_ROTULO=/^\[(imagem|figurinha|áudio|vídeo|documento)\]\s*/;
function chatMidiaHtml(m){
  const md=m.media; if(!md) return '';
  if(md.indisponivel) return `<span class="chat-midia-falhou">[${esc(md.tipo)} indisponível — ${esc(md.indisponivel)}]</span>`;
  const url=esc(md.url);
  if(md.tipo==='imagem') return `<a class="chat-midia" href="${url}" target="_blank" rel="noopener"><img class="chat-midia-img" src="${url}" loading="lazy" alt="imagem" onerror="chatMidiaFalhou(this,'imagem')"></a>`;
  if(md.tipo==='figurinha') return `<img class="chat-midia chat-midia-fig" src="${url}" loading="lazy" alt="figurinha" onerror="chatMidiaFalhou(this,'figurinha')">`;
  if(md.tipo==='audio') return `<audio class="chat-midia chat-midia-audio" controls preload="none" src="${url}" onerror="chatMidiaFalhou(this,'áudio')"></audio>`;
  if(md.tipo==='video') return `<video class="chat-midia chat-midia-video" controls preload="none" src="${url}" onerror="chatMidiaFalhou(this,'vídeo')"></video>`;
  return `<a class="chat-midia chat-midia-doc" href="${url}" target="_blank" rel="noopener">📄 ${esc(md.nome||'documento')}</a>`;
}
function chatMidiaFalhou(el,tipo){
  // 1ª falha pode ser só a fila cheia (503): tenta de novo uma vez
  if(!el.dataset.tentou){
    el.dataset.tentou='1';
    const src=el.getAttribute('src');
    setTimeout(()=>{ if(el.isConnected) el.setAttribute('src',src+(src.includes('?')?'&':'?')+'r=1'); },3000);
    return;
  }
  const aviso=document.createElement('span');
  aviso.className='chat-midia-falhou';
  aviso.textContent=`[${tipo} indisponível — o WhatsApp não tem mais o arquivo ou a conta está desconectada]`;
  (el.closest('a.chat-midia')||el).replaceWith(aviso);
}
function chatMsgHtml(m){
  const date=new Date(m.created_at);
  const time=isNaN(date)?'':date.toLocaleString('pt-BR',{dateStyle:'short',timeStyle:'short'});
  // com a mídia na tela, o rótulo "[áudio]" sai; fica só a legenda, se houver
  let texto=String(m.text||'');
  if(m.media) texto=m.media.tipo==='documento'?'':texto.replace(CHAT_ROTULO,'');
  return `<div class="chat-message ${m.direction==='out'?'out':'in'}" data-msg="${Number(m.id)||0}">${chatMidiaHtml(m)}${texto?`<div class="chat-texto">${esc(texto)}</div>`:''}<time>${time}${m.delivery_state==='sent'?' · enviado':''}</time></div>`;
}
// O que está desenhado, para acrescentar só o que é novo: redesenhar tudo a
// cada 4 s cortava o áudio no meio e baixava as fotos de novo.
let CHAT_DESENHO={thread:null, ids:[]};
async function chatHistory(quiet=false){
  if(!CHAT_SELECTED) return;
  const box=$('#chat-messages'), nearBottom=box.scrollHeight-box.scrollTop-box.clientHeight<60;
  try{
    const thread=CHAT_SELECTED;
    const d=await jget(`/api/chat/conversations/${thread}/messages`);
    if(thread!==CHAT_SELECTED) return;
    const msgs=d.messages||[], ids=msgs.map(m=>m.id);
    // Acrescenta a partir da ÚLTIMA mensagem desenhada (não pelo começo da
    // lista): em conversa com mais de 200 mensagens a janela desliza a cada
    // mensagem nova e o redesenho cortava o áudio que estava tocando.
    const ultimo=CHAT_DESENHO.ids[CHAT_DESENHO.ids.length-1];
    const pos=CHAT_DESENHO.thread===thread&&ultimo!=null?ids.lastIndexOf(ultimo):-1;
    const mesma=pos>=0;
    if(mesma){
      const novas=msgs.slice(pos+1);
      if(!novas.length) return;
      box.querySelector('.muted')?.remove();
      box.insertAdjacentHTML('beforeend',novas.map(chatMsgHtml).join(''));
    }else{
      box.innerHTML=msgs.map(chatMsgHtml).join('')||'<div class="muted">Sem mensagens nesta conversa.</div>';
    }
    CHAT_DESENHO={thread, ids};
    if(nearBottom||!mesma) box.scrollTop=box.scrollHeight;
  }catch(e){if(!quiet) toast('Não foi possível carregar o histórico.');}
}
$('#chat-compose')?.addEventListener('submit',async e=>{
  e.preventDefault();
  const input=$('#chat-text'), text=input.value.trim();
  if(!CHAT_SELECTED||!text) return;
  const t=CHAT_THREADS.find(x=>x.id===CHAT_SELECTED);
  const bytes=new TextEncoder().encode(text).length;
  if(t&&t.channel==='instagram'&&bytes>1000){
    toast(`O Instagram aceita até 1.000 bytes por mensagem (esta tem ${bytes}). Divida em duas.`);
    return;
  }
  const button=$('#chat-compose button'); button.disabled=true;
  const result=await jsend(`/api/chat/conversations/${CHAT_SELECTED}/messages`,'POST',{text});
  button.disabled=false;
  if(!result.ok){toast(result.erro||'Falha ao enviar.');return;}
  input.value=''; await chatLoad(true);
});

// ── Instagram pela API oficial (v023) ───────────────────────────────────────
// Token colado aqui vai para o painel e fica cifrado no banco; a tela só
// recebe de volta @, validade e situação — nunca o token.
async function igOfCarregar(quiet=false){
  if(!$('#ig-oficial')) return;
  let d;
  try{ d=await jget('/api/instagram-oficial/status'); }
  catch(e){ if(!quiet) toast('Não foi possível ler a situação do Instagram.'); return; }
  const cfg=d.configurado||{}, faltam=Object.keys(cfg).filter(k=>!cfg[k]);
  const nomes={app_secret:'IG_API_APP_SECRET',rele_url:'IG_RELE_URL',rele_token:'IG_RELE_TOKEN'};
  const est=d.estado||{}, contas=d.contas||[];
  $('#ig-of-resumo').textContent=`— ${contas.length} conta(s) conectada(s)`
    +(faltam.length?' · relé não configurado':est.ultimo_erro?' · relé com erro':' · recebendo');
  $('#ig-of-rele').innerHTML=faltam.length
    ?`<span class="falta">Falta no .env: ${faltam.map(k=>esc(nomes[k]||k)).join(', ')}.</span> Sem isso nenhuma mensagem do Instagram chega.`
    :`Relé: ${esc(d.rele_url)} · última busca ${est.ultimo_ok?esc(chatHora(est.ultimo_ok)):'ainda não'}`
      +` · ${est.mensagens||0} mensagem(ns) nesta execução`
      +(est.ultimo_erro?` · <span class="falta">${esc(est.ultimo_erro)}</span>`:'');
  $('#ig-of-contas').innerHTML=contas.length?contas.map(c=>{
    const validade=c.dias_restantes==null?'validade conferida na 1ª renovação':`token vale mais ${c.dias_restantes} dia(s)`;
    return `<div class="ig-of-conta"><strong>@${esc(c.username)}</strong>
      <span class="muted">${esc(c.tipo_conta||'')} · ${validade} · ${c.inscrito?'notificações ligadas':'notificações DESLIGADAS'}</span>
      <span class="acoes" data-ig-id="${esc(c.ig_id)}" data-user="${esc(c.username)}">
        <button class="btn" type="button" data-acao="inscrever">Ligar notificações</button>
        <button class="btn" type="button" data-acao="renovar">Renovar token</button>
        <button class="btn" type="button" data-acao="remover">Remover</button>
      </span>${c.ultimo_erro?`<span class="erro">${esc(c.ultimo_erro)}</span>`:''}</div>`;
  }).join(''):'<div class="muted">Nenhuma conta ligada à API oficial ainda.</div>';
}
// Um ouvinte só para os botões das contas (nada de valor dentro de onclick).
$('#ig-of-contas')?.addEventListener('click',e=>{
  const b=e.target.closest('button[data-acao]'); if(!b) return;
  const caixa=b.closest('[data-ig-id]'), igId=caixa.dataset.igId, user=caixa.dataset.user;
  if(b.dataset.acao==='remover') igOfRemover(igId,user); else igOfAcao(igId,b.dataset.acao);
});
async function igOfConectar(e){
  e.preventDefault();
  const campo=$('#ig-of-token'), token=campo.value.trim();
  if(!token) return;
  const r=await jsend('/api/instagram-oficial/contas','POST',{token});
  if(!r.ok){ toast(r.erro||'Não foi possível conectar a conta.'); return; }
  campo.value='';
  toast(`@${r.conta.username} conectada${r.conta.inscrito?'':' — mas as notificações não ligaram (veja o erro)'}.`);
  igOfCarregar(true); chatLoad(true);
}
async function igOfAcao(igId, acao){
  const r=await jsend(`/api/instagram-oficial/contas/${encodeURIComponent(igId)}/${acao}`,'POST');
  toast(r.ok?(acao==='renovar'?'Token renovado por mais 60 dias.':'Notificações ligadas.'):(r.erro||'A Meta não confirmou.'));
  igOfCarregar(true);
}
async function igOfRemover(igId, user){
  if(!confirm(`Desligar @${user} da API oficial? As conversas ficam; só param de chegar e de sair por aqui.`)) return;
  const r=await jsend(`/api/instagram-oficial/contas/${encodeURIComponent(igId)}`,'DELETE');
  toast(r.ok?`@${user} desligada.`:(r.erro||'Não foi possível remover.'));
  igOfCarregar(true); chatLoad(true);
}
async function igOfSincronizar(){
  const r=await jsend('/api/instagram-oficial/sincronizar','POST');
  toast(r.ok?`${r.novas||0} mensagem(ns) nova(s) do Instagram.`:(r.erro||'Não foi possível buscar agora.'));
  igOfCarregar(true); chatLoad(true);
}

// ── FOLLOW-UP: ENVIAR PRÓXIMO ─────────────────────────────────────────────────

// ── CONFIG ───────────────────────────────────────────────────────────────────
// O teto diário de WhatsApp saiu daqui: ele já é definido na aba do canal, e
// dois campos para o mesmo número divergem no dia em que alguém mexe num só.
async function cfgCarregar(){
  backupListar();
  bkpPrevisao();
  bkpListarExports();
  await cfgCarregarModo();
  cfgCarregarSistema();
}

async function cfgCarregarModo(){
  const d = await jget('/api/config/modo').catch(()=>({}));
  const sel = $('#cfg-modo');
  if(sel && d.modo) sel.value = d.modo;

  const box = $('#cfg-modo-atual');
  if(box){
    box.innerHTML = d.ao_vivo
      ? '<div style="padding:10px;border:1px solid #ff4757;border-radius:8px;color:#ff4757;font-weight:700">'
        +'AO VIVO — as mensagens saem para contatos reais</div>'
      : '<div style="padding:10px;border:1px solid #00ff88;border-radius:8px;color:#00ff88;font-weight:700">'
        +'TESTE — só os destinos abaixo recebem</div>';
  }

  const dest = $('#cfg-destinos');
  if(dest){
    const dd = d.destinos_teste || {};
    const linhas = Object.keys(dd).map(k=>
      '<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #2a3a52;font-size:12px">'
      +'<span style="color:#94a3b8">'+esc(k)+'</span>'
      +'<b style="color:#e2e8f0">'+esc((dd[k]||[]).join(', ')||'—')+'</b></div>').join('');
    dest.innerHTML = linhas
      ? '<label class="lf-label">Destinos liberados no modo teste</label>'+linhas
      : '';
  }
}

async function cfgSalvarModo(){
  const m = $('#cfg-modo').value;
  const r = await jsend('/api/config/modo','POST',{modo:m});
  toast(r && r.erro ? r.erro : ('Modo: '+m));
  cfgCarregarModo();
}

// Diagnóstico: responde "o painel está olhando o arquivo certo?" sem terminal.
// Metade dos defeitos desta semana foi exatamente isso — perfil, banco ou pasta
// de nichos divergindo entre o painel e quem executa.
async function cfgCarregarSistema(){
  const box = $('#cfg-sistema'); if(!box) return;
  const d = await jget('/api/sistema').catch(()=>({}));
  if(!d || d.erro){ box.innerHTML='<div class="muted">indisponível</div>'; return; }

  const linha = (nome, caminho, ok, extra) =>
    '<div style="padding:8px 0;border-bottom:1px solid #2a3a52">'
    +'<div style="display:flex;justify-content:space-between;gap:10px">'
      +'<span style="font-size:13px;color:#e2e8f0">'+esc(nome)+'</span>'
      +'<span style="font-size:11px;color:'+(ok?'#00ff88':'#ff4757')+'">'+(ok?'existe':'não encontrado')+'</span>'
    +'</div>'
    +'<div style="font-size:11px;color:#64748b;word-break:break-all">'+esc(caminho)+'</div>'
    +(extra?'<div style="font-size:11px;color:#94a3b8;margin-top:2px">'+extra+'</div>':'')
    +'</div>';

  box.innerHTML =
      (d.bancos||[]).map(b=>linha(b.nome, b.caminho, b.existe,
          b.mb!=null ? (b.mb+' MB') : '')).join('')
    + (d.sessoes||[]).map(x=>linha(x.nome, x.caminho, x.existe,
          (x.itens||[]).length ? esc(x.itens.join(', ')) : 'vazio')).join('')
    + '<div style="margin-top:10px;font-size:11px;color:#64748b">Portas: dashboard '
      +(d.portas||{}).dashboard+' · WhatsApp '+(d.portas||{}).whatsapp
      +' · Instagram '+(d.portas||{}).instagram+'</div>'
    + (d.aviso_onedrive
        ? '<div style="margin-top:10px;padding:8px;border:1px solid #ffc048;border-radius:6px;color:#ffc048;font-size:12px">'
          +'O banco está dentro do OneDrive. A sincronização mexe nos arquivos '
          +'<code>.db-wal</code> e <code>.db-shm</code> por baixo dos processos, e é '
          +'fonte conhecida de "database is locked" com o painel e o daemon juntos.</div>'
        : '');
}

// ── SCHEDULER: PARAR ──────────────────────────────────────────────────────────

// ── API: FOLLOW-UP ENVIAR ─────────────────────────────────────────────────────
// Adiciona rota se não existir (o backend precisa dela)

boot();

// ── INSTAGRAM JS ─────────────────────────────────────────────────────────────
let IG_NICHO_ATUAL = '';
let IG_SUB = 'controle';   // sub-aba aberta do Instagram

async function igCarregarTudo(){
  await igCarregarNichos();
  await igPopularContas();
  await igVerificarStatusBot();
  igAtualizarOperacao();
}

async function igCarregarNichos(){
  const nichos = await jget('/api/ig/nichos');
  const sel = $('#ig-sel-nicho');
  if(sel){
    sel.innerHTML = nichos.length
      ? nichos.map(n => `<option value="${esc(n.nicho)}">${esc(n.nicho)} — ${n.dms_enviadas} DMs, ${n.fontes_pendentes||0} fontes na fila</option>`).join('')
      : '<option value="">nenhum nicho nesta cópia</option>';
    if(nichos.length && !IG_NICHO_ATUAL) IG_NICHO_ATUAL = nichos[0].nicho;
    if(IG_NICHO_ATUAL) sel.value = IG_NICHO_ATUAL;
  }
  const stats = $('#ig-nicho-stats');
  if(stats){
    const a = nichos.find(n => n.nicho === IG_NICHO_ATUAL);
    stats.innerHTML = a
      ? '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">'
        +'<div style="padding:8px;background:#1a2236;border:1px solid #2a3a52;border-radius:6px">'
          +'<b style="color:#00ff88">'+a.dms_enviadas+'</b> <span style="color:#64748b">DMs enviadas</span></div>'
        +'<div style="padding:8px;background:#1a2236;border:1px solid #2a3a52;border-radius:6px">'
          +'<b style="color:#00ff88">'+(a.fontes_pendentes||0)+'</b> <span style="color:#64748b">fontes na fila</span></div>'
        +'<div style="padding:8px;background:#1a2236;border:1px solid #2a3a52;border-radius:6px">'
          +'<b style="color:#00ff88">'+a.responderam+'</b> <span style="color:#64748b">responderam</span></div>'
        +'<div style="padding:8px;background:#1a2236;border:1px solid #2a3a52;border-radius:6px">'
          +'<b style="color:#00ff88">'+(a.templates_ativos||0)+'/'+(a.templates_total||0)+'</b> <span style="color:#64748b">modelos ativos</span></div>'
        +'</div>'
      : '<div class="muted">Nenhum nicho selecionado.</div>';
  }

  const resumo = $('#ig-nicho-resumo');
  const atual = nichos.find(n => n.nicho === IG_NICHO_ATUAL);
  if(resumo){
    resumo.innerHTML = atual
      ? `<b style="color:#e2e8f0">${esc(atual.nicho)}</b> — ${atual.dms_enviadas} DMs enviadas · `
        + `${atual.fontes_pendentes||0} perfis-fonte na fila · ${atual.templates_ativos||0} de ${atual.templates_total||0} modelo(s) ativo(s)`
      : 'Nenhum nicho selecionado.';
  }
  // Nichos que existem no bot ORIGINAL e ainda não foram copiados. A tela
  // mostrava aqueles como se fossem daqui, e editá-los não mudava o envio:
  // quem dispara lê a pasta desta cópia.
  const imp = await jget('/api/ig/nichos/importaveis').catch(()=>({nichos:[]}));
  const box = $('#ig-importar-box'), selImp = $('#ig-importar-nicho');
  if(box && selImp){
    const lista = (imp && imp.nichos) || [];
    box.style.display = lista.length ? '' : 'none';
    selImp.innerHTML = lista.map(n=>`<option value="${esc(n)}">${esc(n)}</option>`).join('');
  }
  const lista = $('#ig-lista-nichos');
  if(lista){
    lista.innerHTML = nichos.map(n => `
      <div style="padding:8px;border:1px solid #e3e9ef;border-radius:6px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center">
        <div><b>${esc(n.nicho)}</b> <span style="font-size:12px;color:#7a8a99">• ${n.dms_enviadas} DMs • ${n.responderam} respostas (${n.taxa_resposta}%)</span></div>
        <button class="btn btn-sm" onclick="igSelecionarNicho('${esc(n.nicho)}')">Abrir</button>
      </div>
    `).join('') || '<div class="muted">Nenhum nicho criado.</div>';
  }
}

// ── Operação ao vivo do bot de DM ───────────────────────────────────────────
// Enquanto a aba do Instagram estiver aberta, o painel pergunta ao bot o que ele
// está fazendo.
let IG_OP_TIMER = null;

async function igAtualizarOperacao(){
  const est = await jget('/api/ig/estado').catch(()=>({}));
  const box = $('#ig-operacao');
  const log = $('#ig-log');

  if(box){
    if(est && est.erro){
      box.innerHTML = '<div style="padding:8px;border:1px solid #ff4757;border-radius:6px;'
        +'color:#ff4757;font-size:12px">'+esc(est.erro)+'</div>';
    }else if(est && est.rodando){
      const e = est.estatisticas || {};
      box.innerHTML = '<div style="padding:10px;border:1px solid #00ff88;border-radius:6px">'
        +'<div style="color:#00ff88;font-weight:700;font-size:13px">'
          +(est.parando ? 'ENCERRANDO — vai parar após a etapa atual' : 'RODANDO')+'</div>'
        +'<div style="font-size:12px;color:#94a3b8;margin-top:4px">nicho <b>'+esc(est.nicho||'?')+'</b>'
          +(est.conta?' · conta <b>@'+esc(est.conta)+'</b>':'')
          +(est.rodizio&&est.rodizio.length>1
              ? ' · em paralelo: '+est.rodizio.map(c=>'@'+esc(c)).join(' + ') : '')
          +(est.modelo?' · modelo <b>'+esc(est.modelo)+'</b>':'')+'</div>'
        +'<div style="font-size:12px;color:#64748b;margin-top:4px">'
          +(e.hoje||0)+' enviadas hoje · '+(e.total||0)+' no total · '
          +(e.rejeitados||0)+' rejeitadas · limite '+(est.limite_diario||'?')+'</div>'
        +'</div>';
    }else{
      box.innerHTML = '<div style="padding:8px;border:1px solid #2a3a52;border-radius:6px;'
        +'color:#64748b;font-size:12px">parado'
        +(est && est.perfil ? ' · perfil: '+esc(String(est.perfil).split(/[\\\/]/).pop()) : '')
        +'</div>';
    }
  }

  if(log && est && Array.isArray(est.log)){
    log.textContent = est.log.length ? est.log.join(String.fromCharCode(10))
                                     : 'sem atividade recente.';
  }

  const bA = $('#ig-btn-abortar'), bI = $('#ig-btn-iniciar');
  const rodando = !!(est && est.rodando);
  if(bA){ bA.disabled = !rodando; bA.style.opacity = rodando ? 1 : 0.5; }
  if(bI){ bI.disabled = rodando;  bI.style.opacity = rodando ? 0.5 : 1; }
}

async function igAbortar(){
  const r = await jsend('/api/ig/parar','POST');
  toast(r.ok ? (r.mensagem || 'Parada solicitada') : (r.erro || 'Não deu para parar'));
  igAtualizarOperacao();
}

// Só sonda com a aba aberta: perguntar de 3 em 3 segundos com a aba fechada é
// tráfego para ninguém ver.
setInterval(()=>{
  const pane = document.getElementById('pane-instagram');
  if(pane && pane.classList.contains('active')) igAtualizarOperacao();
}, 3000);

async function igVerificarStatusBot(){
  const st = await jget('/api/ig/status');
  const box = $('#ig-bot-status-box');
  if(box){
    box.innerHTML = st.online 
      ? `<div style="color:#16a34a;font-weight:700">● Servidor de DM Ativo (Porta 5022)</div>`
      : `<div style="color:#dc2626">● Bot Offline (Inicie o Servidor_N8N.py)</div>`;
  }
}

async function igSelecionarNicho(nome){
  IG_NICHO_ATUAL = nome;
  const sel=$('#ig-sel-nicho'); if(sel) sel.value = nome;
  await igCarregarNichos();
  igLoadSubpane(IG_SUB);
}

// Trocar o nicho recarrega a sub-aba ABERTA. Antes forçava 'keywords': quem
// estava vendo templates era jogado para outra tela ao trocar de nicho.
async function igTrocarNichoAtivo(){
  IG_NICHO_ATUAL = $('#ig-sel-nicho').value;
  await igCarregarNichos();
  igLoadSubpane(IG_SUB);
}

async function igImportarNicho(){
  const nome = $('#ig-importar-nicho').value;
  if(!nome){ toast('Escolha o nicho'); return; }
  const r = await jsend('/api/ig/nichos/importar','POST',{nicho:nome});
  if(!r.ok){ toast(r.erro||'Não deu para importar'); return; }
  toast(`${nome} importado (${(r.arquivos||[]).length} arquivos)`);
  IG_NICHO_ATUAL = nome;
  await igCarregarNichos();
  igLoadSubpane(IG_SUB);
}

// Templates A/B do Instagram. A lista já era desenhada, mas
// `igAdicionarTemplate`, `igSalvarTemplate` e `igToggleTemplate` nunca
// existiram: os botões estavam na tela e não faziam nada.
let IG_TEMPLATES = [];

async function igGravarTemplates(){
  if(!IG_NICHO_ATUAL){ toast('Escolha um nicho primeiro'); return false; }
  const r = await jsend('/api/ig/nicho/'+encodeURIComponent(IG_NICHO_ATUAL)+'/salvar',
                        'POST', {templates: IG_TEMPLATES});
  if(!r.ok){ toast(r.erro||'Não deu para salvar'); return false; }
  return true;
}

async function igAdicionarTemplate(){
  if(!IG_NICHO_ATUAL){ toast('Escolha um nicho primeiro'); return; }
  IG_TEMPLATES.push({id: IG_TEMPLATES.length+1,
                     nome: 'Modelo '+(IG_TEMPLATES.length+1),
                     texto: '', ativo: true});
  if(await igGravarTemplates()){ toast('Modelo adicionado'); igLoadSubpane('templates'); }
}

async function igSalvarTemplate(idx){
  const campo = $('#ig-tpl-txt-'+idx);
  if(!campo || !IG_TEMPLATES[idx]) return;
  const texto = campo.value.trim();
  if(!texto){ toast('Escreva a mensagem antes de salvar'); return; }
  IG_TEMPLATES[idx].texto = texto;
  if(await igGravarTemplates()) toast('Modelo salvo');
}

async function igToggleTemplate(idx, ativo){
  if(!IG_TEMPLATES[idx]) return;
  IG_TEMPLATES[idx].ativo = !!ativo;
  if(await igGravarTemplates()) toast(ativo?'Modelo ativado':'Modelo desativado');
}

async function igLoadSubpane(sub){
  IG_SUB = sub || IG_SUB;
  if(sub==='agendamento'){ agCarregar('instagram'); return; }
  if(sub==='perfis'){ accCarregar(); return; }
  if(sub === 'keywords' || sub === 'nichos' || sub === 'templates'){
    if(!IG_NICHO_ATUAL) return;
    const d = await jget(`/api/ig/nicho/${IG_NICHO_ATUAL}/dados`);
    if(d.ok){
      if($('#ig-fontes')) $('#ig-fontes').value = d.fontes || '';
      if($('#ig-kw-busca')) $('#ig-kw-busca').value = d.palavras_chave || '';
      if($('#ig-kw-contexto')) $('#ig-kw-contexto').value = d.palavras_contexto || '';
      if($('#ig-kw-bloqueio')) $('#ig-kw-bloqueio').value = d.lista_negra || '';
      if($('#ig-templates-lista') && d.templates){
        IG_TEMPLATES = d.templates;
        $('#ig-templates-lista').innerHTML = d.templates.map((t, idx) => `
          <div style="border:1px solid #e3e9ef;border-radius:8px;padding:10px;margin-bottom:8px">
            <div style="display:flex;justify-content:space-between;margin-bottom:6px">
              <b>${esc(t.nome || 'Modelo '+(idx+1))}</b>
              <label class="toggle"><input type="checkbox" ${t.ativo?'checked':''} onchange="igToggleTemplate(${idx}, this.checked)"><span class="slider"></span></label>
            </div>
            <textarea id="ig-tpl-txt-${idx}" rows="3" style="width:100%">${esc(t.texto||t.body||'')}</textarea>
            <div style="margin-top:6px;display:flex;justify-content:flex-end">
              <button class="btn btn-sm" onclick="igSalvarTemplate(${idx})">Salvar</button>
            </div>
          </div>
        `).join('');
      }
    }
  }
  if(sub === 'crm'){
    const leads = await jget('/api/leads?limit=50');
    const crmBox = $('#ig-crm-tabela');
    if(crmBox){
      crmBox.innerHTML = `
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead><tr style="text-align:left;border-bottom:2px solid #e3e9ef"><th style="padding:6px">Lead</th><th style="padding:6px">Instagram</th><th style="padding:6px">Status</th><th style="padding:6px">Score</th></tr></thead>
          <tbody>
            ${leads.filter(l=>l.instagram).map(l=>`
              <tr style="border-bottom:1px solid #e3e9ef">
                <td style="padding:6px">${esc(l.nome)}</td>
                <td style="padding:6px"><a href="https://instagram.com/${esc(l.instagram)}" target="_blank">@${esc(l.instagram)}</a></td>
                <td style="padding:6px"><span class="badge" style="background:#2563eb">${esc(l.kanban||'lista')}</span></td>
                <td style="padding:6px;color:${scoreCor(l.score)}">${l.score||0}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      `;
    }
  }
}

async function igCriarNicho(){
  const nome = $('#ig-novo-nicho').value.trim();
  if(!nome){ toast('Nome obrigatório'); return; }
  const r = await jsend('/api/ig/nicho/criar', 'POST', { nome });
  toast(r.ok ? 'Nicho criado!' : (r.erro || 'Erro ao criar'));
  $('#ig-novo-nicho').value = '';
  igCarregarNichos();
}

async function igSalvarFontes(){
  if(!IG_NICHO_ATUAL) return;
  const fontes = $('#ig-fontes').value;
  await jsend(`/api/ig/nicho/${IG_NICHO_ATUAL}/salvar`, 'POST', { fontes });
  toast('Fontes salvas para ' + IG_NICHO_ATUAL);
}

async function igSalvarKeywords(){
  if(!IG_NICHO_ATUAL) return;
  await jsend(`/api/ig/nicho/${IG_NICHO_ATUAL}/salvar`, 'POST', {
    palavras_chave: $('#ig-kw-busca').value,
    palavras_contexto: $('#ig-kw-contexto').value,
    lista_negra: $('#ig-kw-bloqueio').value
  });
  toast('Keywords salvas para ' + IG_NICHO_ATUAL);
}

async function igAutenticarConta(){
  const sel=$('#ig-auth-conta');
  const u=sel?sel.value:$('#ig-auth-user')?.value?.trim()||'';
  if(!u){toast('Selecione ou informe um @username');return;}
  toast('Iniciando navegador para autenticar @'+u+'...');
  const r=await jsend('/api/ig/auth/'+u,'POST');
  toast(r.mensagem||r.erro);
  if($('#ig-auth-status'))$('#ig-auth-status').innerHTML=r.ok?'<span style="color:#00ff88">Navegador aberto. Faça login e aguarde.</span>':'<span style="color:#ff4757">'+(r.erro||'Erro')+'</span>';
}

// Dispara o bot de DM no nicho ativo.
async function igPopularContas(){
  const sel = $('#ig-conta-disparo'); if(!sel) return;
  const anterior = sel.value;
  const contas = await jget('/api/connect/instagram/status').catch(()=>[]);
  const comSessao = (Array.isArray(contas)?contas:[]).filter(c=>c.sessao_ativa);
  let html = '<option value="">Todas as contas (em paralelo) — '
           + comSessao.length + ' com sessão</option>';
  html += (Array.isArray(contas)?contas:[]).map(c=>
      '<option value="'+esc(c.identifier)+'"'+(c.sessao_ativa?'':' disabled')+'>@'
      + esc(c.identifier) + (c.sessao_ativa
          ? ' — ' + (c.restam!=null?c.restam+' vaga(s)':'')
          : ' (sem sessão)') + '</option>').join('');
  sel.innerHTML = html;
  if(anterior) sel.value = anterior;
}

async function igIniciarBot(){
  if(!IG_NICHO_ATUAL){ toast('Selecione um nicho'); return; }

  const d = await jget('/api/ig/nicho/'+encodeURIComponent(IG_NICHO_ATUAL)+'/dados')
                  .catch(()=>({}));
  const ativos = ((d && d.templates) || []).filter(t=>t.ativo && (t.texto||'').trim());
  if(!ativos.length){
    toast('Nenhum modelo ativo com texto em "'+IG_NICHO_ATUAL+'". Ative um em Templates A/B.');
    return;
  }
  // com mais de um ativo, sorteia: é para isso que o A/B existe
  const escolhido = ativos[Math.floor(Math.random()*ativos.length)];

  const fontes = ((d && d.fontes) || '').split(String.fromCharCode(10)).filter(l=>l.trim()).length;

  const _c = ($('#ig-conta-disparo') || {}).value || '';
  toast('Disparando '+IG_NICHO_ATUAL+' · modelo '+escolhido.nome+' · '+fontes
        +' fontes · '+(_c ? '@'+_c : 'todas as contas em paralelo'));
  const conta = ($('#ig-conta-disparo') || {}).value || '';
  const r = await jsend('/api/ig/prospectar','POST',
                        {nicho: IG_NICHO_ATUAL, mensagem: escolhido.texto, conta});
  toast(r.ok ? (r.mensagem || 'Disparo iniciado') : (r.erro || 'Não deu para iniciar'));
  igCarregarNichos();
  igAtualizarOperacao();
}

// ── EMAIL JS ──────────────────────────────────────────────────────────────────
// ── E-MAIL MARKETING ─────────────────────────────────────────────────────────
// Tudo aqui fala com o pcz.db, o banco do mail bot — onde estão as listas, os
// templates, as campanhas e as contas que de fato enviam.
const EMAIL = { listas:[], templates:[], contas:[], campanhas:[] };

async function emailCarregarTudo(){
  const [stats, listas, tpls, contas, camps] = await Promise.all([
    jget('/api/email/stats').catch(()=>({})),
    jget('/api/email/listas').catch(()=>[]),
    jget('/api/email/templates').catch(()=>[]),
    jget('/api/email/contas').catch(()=>[]),
    jget('/api/email/campanhas').catch(()=>[]),
  ]);
  EMAIL.listas    = Array.isArray(listas)? listas : [];
  EMAIL.templates = Array.isArray(tpls)  ? tpls   : [];
  EMAIL.contas    = Array.isArray(contas)? contas : [];
  EMAIL.campanhas = Array.isArray(camps) ? camps  : [];

  emailRenderAviso(stats);
  emailRenderKPIs(stats);
  emailRenderSelects();
  emailRenderCampanhas();
  emailRenderListas();
  emailRenderTemplates();
  emailRenderContas();
  emailAtualizarDisparo();
}

function emailRenderAviso(s){
  const box=$('#email-aviso'); if(!box) return;
  if(s && s.ok===false){
    box.innerHTML='<div style="padding:10px;border:1px solid #ff4757;border-radius:8px;color:#ff4757;font-size:13px">'
      +'Banco do mail bot nao encontrado em '+esc(s.banco||'?')+'. A area de e-mail depende dele.</div>';
    return;
  }
  if(s && s.launcher_ok===false){
    box.innerHTML='<div style="padding:10px;border:1px solid #ffc048;border-radius:8px;color:#ffc048;font-size:13px">'
      +'O disparador nao foi encontrado em '+esc(s.launcher||'?')+'. Da para montar campanha, mas nao para enviar.</div>';
    return;
  }
  box.innerHTML='';
}

function emailRenderKPIs(s){
  const box=$('#email-kpis-box'); if(!box||!s||s.ok===false) return;
  const cel=(v,r)=>'<div style="padding:8px;background:#1a2236;border:1px solid #2a3a52;border-radius:6px">'
    +'<b style="color:#00ff88">'+v+'</b> <span style="color:#64748b">'+r+'</span></div>';
  box.innerHTML='<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">'
    + cel((s.pendentes||0).toLocaleString('pt-BR'),'pendentes')
    + cel((s.enviados||0).toLocaleString('pt-BR'),'enviados')
    + cel(s.enviados_hoje||0,'hoje')
    + cel(s.capacidade_hoje||0,'cabem hoje')
    + '</div>'
    + '<div style="margin-top:8px;font-size:11px;color:#64748b">'
    + (s.contas_ativas||0)+' conta(s) ativa(s) &middot; '+(s.respostas||0)+' resposta(s) &middot; '
    + (s.erros||0)+' erro(s)</div>';
}

function emailRenderSelects(){
  const selL=$('#email-camp-lista');
  if(selL) selL.innerHTML = EMAIL.listas.length
    ? EMAIL.listas.map(l=>'<option value="'+l.id+'">'+esc(l.name)+' &mdash; '+(l.pendentes||0).toLocaleString('pt-BR')+' pendentes</option>').join('')
    : '<option value="">nenhuma lista importada</option>';

  const selT=$('#email-camp-template');
  if(selT) selT.innerHTML = EMAIL.templates.length
    ? EMAIL.templates.map(t=>'<option value="'+t.id+'">'+esc(t.name)+'</option>').join('')
    : '<option value="">nenhum template criado</option>';

  emailRenderContasCampanha();

  const selC=$('#email-disparo-campanha');
  if(selC) selC.innerHTML = EMAIL.campanhas.length
    ? EMAIL.campanhas.map(c=>'<option value="'+c.id+'">'+esc(c.name)+' &mdash; '+(c.pendentes||0).toLocaleString('pt-BR')+' pendentes'+(c.problema?' (! '+esc(c.problema)+')':'')+'</option>').join('')
    : '<option value="">crie uma campanha primeiro</option>';

  emailPreviaTemplate();
  if(selT) selT.onchange = emailPreviaTemplate;
  if(selL) selL.onchange = emailResumoCampanha;
}

// Contas na tela de campanha: resumo primeiro, escolha só se você quiser.
function emailRenderContasCampanha(){
  const ativas=EMAIL.contas.filter(c=>c.active);
  const prontas=ativas.filter(c=>c.autenticada!==false);
  const cabem=prontas.reduce((s,c)=>s+(c.restam||0),0);
  const resumo=$('#email-camp-contas-resumo');
  if(resumo){
    resumo.innerHTML = ativas.length
      ? '<div style="display:flex;justify-content:space-between;align-items:center;gap:8px">'
        +'<span>usando <b style="color:#00ff88">'+prontas.length+'</b> conta(s) &middot; cabem <b style="color:#00ff88">'+cabem+'</b> e-mails hoje'
        +(ativas.length!==prontas.length?' <span style="color:#ffc048">('+(ativas.length-prontas.length)+' sem OAuth)</span>':'')+'</span>'
        +'<button class="btn btn-sm" onclick="emailToggleContas()">escolher</button></div>'
      : '<span style="color:#ffc048">nenhuma conta ativa. Cadastre em <b>Contas de envio</b>.</span>';
  }
  const cx=$('#email-camp-contas');
  if(cx) cx.innerHTML = EMAIL.contas.map(c=>
    '<label style="display:flex;align-items:center;gap:6px;font-size:12px;color:'+(c.active?'#94a3b8':'#475569')+';margin-bottom:4px">'
      +'<input type="checkbox" class="email-conta-cb" value="'+c.id+'" '+(c.active&&c.autenticada!==false?'checked':'')+'>'
      +esc(c.email)+' <span class="lf-tag">'+(c.sent_today||0)+'/'+(c.daily_limit||0)+'</span>'
      +(c.autenticada===false?' <span class="lf-tag" style="color:#ffc048;border-color:#ffc048">sem OAuth</span>':'')
    +'</label>').join('');
  emailResumoCampanha();
}

function emailToggleContas(){
  const cx=$('#email-camp-contas');
  if(cx) cx.style.display = cx.style.display==='none' ? 'block' : 'none';
}

// Uma frase dizendo o que vai acontecer. O formulário tinha quatro campos e
// nenhuma leitura do resultado antes de clicar.
function emailResumoCampanha(){
  const box=$('#email-camp-resumo'); if(!box) return;
  const l=EMAIL.listas.find(x=>String(x.id)===$('#email-camp-lista')?.value);
  const t=EMAIL.templates.find(x=>String(x.id)===$('#email-camp-template')?.value);
  const n=$$('.email-conta-cb').filter(c=>c.checked).length;
  if(!l||!t){ box.textContent=''; return; }
  box.innerHTML='Vai enviar <b style="color:#e2e8f0">'+esc(t.name)+'</b> para <b style="color:#e2e8f0">'
    +(l.pendentes||0).toLocaleString('pt-BR')+'</b> pendentes de <b style="color:#e2e8f0">'+esc(l.name)
    +'</b>, por '+n+' conta(s).';
}

// ── Contas de envio ─────────────────────────────────────────────────────────
function emailRenderContas(){
  const resumo=$('#email-contas-resumo');
  if(resumo){
    const ativas=EMAIL.contas.filter(c=>c.active);
    const semOauth=ativas.filter(c=>c.autenticada===false);
    const cabem=ativas.filter(c=>c.autenticada!==false).reduce((s,c)=>s+(c.restam||0),0);
    const usado=EMAIL.contas.reduce((s,c)=>s+(c.sent_today||0),0);
    const teto=ativas.reduce((s,c)=>s+(c.daily_limit||0),0);
    const pct=teto?Math.min(100,Math.round(usado/teto*100)):0;
    resumo.innerHTML='<div style="font-size:26px;font-weight:700;color:#00ff88">'+cabem+'</div>'
      +'<div style="font-size:12px;color:#64748b;margin-bottom:10px">e-mails ainda cabem hoje</div>'
      +'<div class="progress-bar"><div class="progress-fill" style="width:'+pct+'%;background:'+(pct>=90?'#ff4757':pct>=50?'#ffc048':'#00ff88')+'"></div></div>'
      +'<div style="font-size:11px;color:#64748b;margin-top:4px">'+usado+' de '+teto+' usados &middot; '+ativas.length+' conta(s) ativa(s)</div>'
      +(semOauth.length?'<div style="margin-top:10px;padding:8px;border:1px solid #ffc048;border-radius:6px;color:#ffc048;font-size:12px">'
        +semOauth.length+' conta(s) sem autenticação não enviam nada.</div>':'');
  }

  const box=$('#email-contas-lista'); if(!box) return;
  box.innerHTML = EMAIL.contas.length ? EMAIL.contas.map(c=>{
    const pct=c.daily_limit?Math.min(100,Math.round((c.sent_today||0)/c.daily_limit*100)):0;
    const cor=pct>=90?'#ff4757':pct>=50?'#ffc048':'#00ff88';
    const ok=c.autenticada===true;
    const corOauth=ok?'#00ff88':(c.autenticada===false?'#ff4757':'#64748b');
    return '<div class="lf-row">'
      +'<div style="flex:1;min-width:180px">'
        +'<div style="font-weight:700;font-size:13px;color:'+(c.active?'#e2e8f0':'#475569')+'">'+esc(c.email)+'</div>'
        +'<div style="font-size:11px;color:#64748b">'+esc(c.sender_name||'')+'</div>'
        +'<div style="font-size:11px;color:'+corOauth+';margin-top:2px">'+esc(c.oauth_txt||'')+'</div>'
      +'</div>'
      +'<div style="width:170px">'
        +'<div style="font-size:12px;color:#64748b">'+(c.sent_today||0)+' / '+(c.daily_limit||0)+' hoje</div>'
        +'<div class="progress-bar"><div class="progress-fill" style="width:'+pct+'%;background:'+cor+'"></div></div>'
        +'<div style="font-size:11px;color:'+cor+';margin-top:2px">'+(c.restam||0)+' restante(s)</div>'
      +'</div>'
      +'<div style="display:flex;align-items:center;gap:4px">'
        +'<label class="lf-label" style="margin:0">limite</label>'
        +'<input type="number" min="1" max="500" value="'+(c.daily_limit||0)+'" class="lf-input" style="width:72px;padding:5px" '
        +'onchange="emailSalvarLimite('+c.id+',this.value)">'
      +'</div>'
      +'<div style="display:flex;gap:4px;align-items:center">'
        +(ok?'<button class="btn btn-sm" onclick="emailValidarConta('+c.id+')">Validar</button>'
            :'<button class="btn btn-sm btn-primary" onclick="emailAutenticarConta('+c.id+')">Autenticar</button>')
        +'<label class="toggle" title="ativa/inativa"><input type="checkbox" '+(c.active?'checked':'')
        +' onchange="emailToggleConta('+c.id+',this.checked)"><span class="slider"></span></label>'
        +'<button class="btn btn-sm btn-danger" onclick="emailExcluirConta('+c.id+',this)" data-email="'+esc(c.email)+'">Excluir</button>'
      +'</div>'
    +'</div>';
  }).join('')
  : '<div class="muted" style="font-size:13px">Nenhuma conta cadastrada. Adicione a primeira acima.</div>';
}

async function emailCriarConta(){
  const email=$('#email-conta-email').value.trim();
  const nome=$('#email-conta-nome').value.trim();
  const limite=parseInt($('#email-conta-limite').value)||70;
  if(!email){ toast('Informe o e-mail da conta'); return; }
  const r=await jsend('/api/email/contas','POST',{email,nome,limite});
  if(!r.ok){ toast(r.erro||'Nao deu para cadastrar'); return; }
  toast(r.mensagem||'Conta cadastrada');
  $('#email-conta-email').value=''; $('#email-conta-nome').value='';
  await emailCarregarTudo();
}

async function emailSalvarLimite(id,valor){
  const limite=parseInt(valor)||0;
  const r=await jsend('/api/email/contas/'+id,'PUT',{limite});
  toast(r.ok?('Limite diario agora e '+limite):(r.erro||'Nao deu para salvar'));
  emailCarregarTudo();
}

async function emailToggleConta(id,ativo){
  const r=await jsend('/api/email/contas/'+id,'PUT',{ativo:!!ativo});
  toast(r.ok?(ativo?'Conta ativada':'Conta desativada'):(r.erro||'Nao deu para alterar'));
  emailCarregarTudo();
}

async function emailValidarConta(id){
  toast('Perguntando ao Gmail...');
  const r=await jsend('/api/email/contas/'+id+'/validar','POST');
  toast(r.ok?(r.mensagem||'Conta valida'):(r.erro||'Conta nao respondeu'));
  emailCarregarTudo();
}

async function emailAutenticarConta(id){
  const r=await jsend('/api/email/contas/'+id+'/autenticar','POST');
  toast(r.ok?(r.mensagem||'Consentimento aberto'):(r.erro||'Nao deu para abrir'));
  // o OAuth acontece fora do painel: reconsulta por um tempo para a linha virar
  // "autenticada" sozinha quando voce aprovar
  let n=0; const t=setInterval(async()=>{ n++; await emailCarregarTudo(); if(n>=20) clearInterval(t); },10000);
}

async function emailExcluirConta(id,btn){
  const email=(btn&&btn.dataset.email)||('#'+id);
  const r=await jsend('/api/email/contas/'+id,'DELETE');
  toast(r.ok?'Conta removida':(r.erro||'Nao deu para excluir'));
  emailCarregarTudo();
}

function emailPreviaTemplate(){
  const sel=$('#email-camp-template'), box=$('#email-camp-previa');
  if(!sel||!box) return;
  const t=EMAIL.templates.find(x=>String(x.id)===sel.value);
  box.textContent = t ? ('Assunto: '+(t.subject||'(sem assunto)')) : '';
}

function emailRenderCampanhas(){
  const box=$('#email-campanhas-lista'); if(!box) return;
  box.innerHTML = EMAIL.campanhas.length ? EMAIL.campanhas.map(c=>
    '<div class="lf-row">'
      +'<div style="flex:1">'
        +'<div style="font-weight:700;font-size:13px;color:#e2e8f0">'+esc(c.name)+'</div>'
        +'<div style="font-size:11px;color:#64748b">lista: '+esc(c.lista_nome||'-')+' &middot; mensagem: '+esc(c.template_nome||'-')+' &middot; '+(c.contas||0)+' conta(s)</div>'
        +(c.problema?'<div style="font-size:11px;color:#ffc048;margin-top:3px">! '+esc(c.problema)+'</div>':'')
      +'</div>'
      +'<div style="text-align:right">'
        +'<div style="font-size:12px;color:#00ff88">'+(c.pendentes||0).toLocaleString('pt-BR')+' pendentes</div>'
        +'<div style="font-size:11px;color:#64748b">'+(c.enviados||0).toLocaleString('pt-BR')+' enviados</div>'
      +'</div>'
      +'<button class="btn btn-sm btn-danger" onclick="emailExcluirCampanha('+c.id+')">Excluir</button>'
    +'</div>').join('')
    : '<div class="muted" style="font-size:13px">Nenhuma campanha. Crie uma acima escolhendo a lista e a mensagem.</div>';
}

function emailRenderListas(){
  const box=$('#email-listas-cadastradas'); if(!box) return;
  box.innerHTML = EMAIL.listas.length ? EMAIL.listas.map(l=>
    '<div class="lf-row">'
      +'<div style="flex:1">'
        +'<div style="font-weight:700;font-size:13px;color:#e2e8f0">'+esc(l.name)+'</div>'
        +'<div style="font-size:11px;color:#64748b">'+(l.total||0).toLocaleString('pt-BR')+' prospects &middot; '
        +(l.pendentes||0).toLocaleString('pt-BR')+' pendentes &middot; '+(l.enviados||0).toLocaleString('pt-BR')+' enviados'
        +(l.erros?' &middot; '+l.erros+' erros':'')+'</div>'
      +'</div>'
      +'<button class="btn btn-sm btn-danger" onclick="emailExcluirLista('+l.id+',this)" data-nome="'+esc(l.name)+'">Excluir</button>'
    +'</div>').join('')
    : '<div class="muted" style="font-size:13px">Nenhuma lista importada.</div>';
}

function emailRenderTemplates(){
  const box=$('#email-templates-lista'); if(!box) return;
  box.innerHTML = EMAIL.templates.length ? EMAIL.templates.map(t=>
    '<div class="lf-row" style="flex-direction:column;align-items:stretch">'
      +'<div style="display:flex;justify-content:space-between;gap:8px;align-items:start">'
        +'<div style="flex:1">'
          +'<div style="font-weight:700;font-size:13px;color:#e2e8f0">'+esc(t.name)+'</div>'
          +'<div style="font-size:11px;color:#94a3b8;margin-top:2px">Assunto: '+esc(t.subject||'(sem assunto)')+'</div>'
          +(t.em_campanhas?'<span class="lf-tag" style="margin-top:4px;display:inline-block">em '+t.em_campanhas+' campanha(s)</span>':'')
        +'</div>'
        +'<div style="display:flex;gap:4px">'
          +'<button class="btn btn-sm" onclick="emailEditarTemplate('+t.id+')">Editar</button>'
          +'<button class="btn btn-sm btn-danger" onclick="emailExcluirTemplate('+t.id+',this)" data-nome="'+esc(t.name)+'">Excluir</button>'
        +'</div>'
      +'</div>'
      +'<pre style="background:#0f1626;border:1px solid #2a3a52;color:#94a3b8;padding:8px;border-radius:4px;margin:8px 0 0;font-size:11px;white-space:pre-wrap;max-height:120px;overflow:auto">'+esc((t.body||'').slice(0,600))+'</pre>'
    +'</div>').join('')
    : '<div class="muted" style="font-size:13px">Nenhum template. Crie o primeiro ao lado.</div>';
}

// ── Templates: criar, editar, excluir ───────────────────────────────────────
function emailLimparFormTemplate(){
  $('#email-tpl-id').value=''; $('#email-tpl-nome').value='';
  $('#email-tpl-assunto').value=''; $('#email-tpl-corpo').value='';
  $('#email-tpl-titulo').textContent='Novo template';
}

function emailEditarTemplate(id){
  const t=EMAIL.templates.find(x=>x.id===id); if(!t) return;
  $('#email-tpl-id').value=t.id;
  $('#email-tpl-nome').value=t.name||'';
  $('#email-tpl-assunto').value=t.subject||'';
  $('#email-tpl-corpo').value=t.body||'';
  $('#email-tpl-titulo').textContent='Editando: '+t.name;
  $('#email-tpl-nome').scrollIntoView({behavior:'smooth',block:'center'});
}

async function emailSalvarTemplate(){
  const id=$('#email-tpl-id').value.trim();
  const dados={ nome:$('#email-tpl-nome').value.trim(),
                assunto:$('#email-tpl-assunto').value.trim(),
                corpo:$('#email-tpl-corpo').value.trim() };
  if(!dados.nome||!dados.assunto||!dados.corpo){
    toast('Nome, assunto e corpo sao obrigatorios'); return;
  }
  const r = id ? await jsend('/api/email/templates/'+id,'PUT',dados)
               : await jsend('/api/email/templates','POST',dados);
  if(!r.ok){ toast(r.erro||'Nao deu para salvar'); return; }
  toast(id?'Template atualizado':'Template criado');
  emailLimparFormTemplate();
  emailCarregarTudo();
}

async function emailExcluirTemplate(id,btn){
  const nome=(btn&&btn.dataset.nome)||('#'+id);
  const r=await jsend('/api/email/templates/'+id,'DELETE');
  toast(r.ok?'Template excluido':(r.erro||'Nao deu para excluir'));
  emailCarregarTudo();
}

// ── Listas ──────────────────────────────────────────────────────────────────
async function emailImportarLista(){
  const nome=$('#email-lista-nome').value.trim();
  const texto=$('#email-lista-texto').value.trim();
  if(!nome||!texto){ toast('Nome e lista sao obrigatorios'); return; }
  const r=await jsend('/api/email/listas','POST',{nome,texto});
  if(!r.ok){ toast(r.erro||'Erro ao importar'); return; }
  toast('Importados '+r.inseridos+' prospects'+(r.ignorados?' ('+r.ignorados+' linhas ignoradas)':''));
  $('#email-lista-nome').value=''; $('#email-lista-texto').value='';
  emailCarregarTudo();
}

async function emailExcluirLista(id,btn){
  const nome=(btn&&btn.dataset.nome)||('#'+id);
  const r=await jsend('/api/email/listas/'+id,'DELETE');
  toast(r.ok?('Lista excluida ('+r.prospects_removidos+' prospects)'):(r.erro||'Nao deu para excluir'));
  emailCarregarTudo();
}

// ── Campanhas ───────────────────────────────────────────────────────────────
async function emailCriarCampanha(){
  const nome=$('#email-camp-nome').value.trim();
  const list_id=parseInt($('#email-camp-lista').value)||0;
  const template_id=parseInt($('#email-camp-template').value)||0;
  const contas=$$('.email-conta-cb').filter(c=>c.checked).map(c=>parseInt(c.value));
  if(!nome){ toast('De um nome a campanha'); return; }
  if(!list_id){ toast('Escolha a lista'); return; }
  if(!template_id){ toast('Escolha a mensagem'); return; }
  if(!contas.length){ toast('Marque ao menos uma conta remetente'); return; }
  const r=await jsend('/api/email/campanhas','POST',{nome,list_id,template_id,contas});
  if(!r.ok){ toast(r.erro||'Nao deu para criar'); return; }
  toast('Campanha criada');
  $('#email-camp-nome').value='';
  emailCarregarTudo();
}

async function emailExcluirCampanha(id){
  const r=await jsend('/api/email/campanhas/'+id,'DELETE');
  toast(r.ok?'Campanha excluida':(r.erro||'Nao deu para excluir'));
  emailCarregarTudo();
}

// ── Disparo ─────────────────────────────────────────────────────────────────
async function emailIniciarDisparo(){
  const campaign_id=parseInt($('#email-disparo-campanha').value)||0;
  if(!campaign_id){ toast('Escolha a campanha'); return; }
  const min_delay=parseInt($('#email-delay-min').value)||30;
  const max_delay=parseInt($('#email-delay-max').value)||90;
  const camp=EMAIL.campanhas.find(c=>c.id===campaign_id);
  const r=await jsend('/api/email/disparo/iniciar','POST',{campaign_id,min_delay,max_delay});
  toast(r.ok?(r.mensagem||'Disparo iniciado'):(r.erro||'Nao deu para iniciar'));
  emailAtualizarDisparo();
}

async function emailPararDisparo(){
  const r=await jsend('/api/email/disparo/parar','POST');
  toast(r.ok?(r.mensagem||'Disparo encerrado'):(r.erro||'Nada em andamento'));
  emailAtualizarDisparo();
}

async function emailAtualizarDisparo(){
  const d=await jget('/api/email/disparo/status').catch(()=>({}));
  const box=$('#email-disparo-estado');
  if(box){
    box.innerHTML = d.rodando
      ? '<div style="padding:8px;border:1px solid #00ff88;border-radius:6px;color:#00ff88;font-size:12px;font-weight:600">disparando agora (pid '+d.pid+') &middot; '+(d.enviados_hoje||0)+' enviados hoje</div>'
      : '<div style="padding:8px;border:1px solid #2a3a52;border-radius:6px;color:#64748b;font-size:12px">parado &middot; '+(d.enviados_hoje||0)+' enviados hoje</div>';
  }
  const bd=$('#email-btn-disparar'), bp=$('#email-btn-parar');
  if(bd){ bd.disabled=!!d.rodando; bd.style.opacity=d.rodando?0.5:1; }
  if(bp){ bp.disabled=!d.rodando;  bp.style.opacity=d.rodando?1:0.5; }
  const log=$('#email-disparo-log');
  if(log && Array.isArray(d.log)) log.textContent = d.log.length? d.log.join(String.fromCharCode(10)) : 'sem disparo recente.';
}

// Enquanto a aba de e-mail estiver aberta, o estado do disparo anda sozinho:
// e processo de horas, e obrigar F5 para saber se ainda esta enviando nao serve.
setInterval(()=>{
  const pane=document.getElementById('pane-emails');
  if(pane && pane.classList.contains('active')) emailAtualizarDisparo();
}, 8000);

async function emailLoadSubpane(sub){
  if(sub==='agendamento'){ await emailCarregarTudo(); agCarregar('email'); return; }
  // A leitura de respostas saiu da aba: sem uma medição confiável de resposta,
  // a tela mostrava zero e o operador concluía que ninguém respondeu.
  emailCarregarTudo();
}



// QUADROS — um funil por empresa atendida O quadro ATIVO mora no servidor, não
// aqui.

async function quadrosCarregar(){
  const d = await jget('/api/quadros').catch(()=>null);
  if(!d) return;
  QUADROS = d.quadros || [];
  QUADRO_ATIVO = d.ativo;
  quadrosRender();
}

function quadrosRender(){
  const bq = $('#bulk-quadro');
  if(bq){
    const atual = bq.value;
    bq.innerHTML = QUADROS.filter(q=>q.id!==QUADRO_ATIVO)
      .map(q=>'<option value="'+q.id+'">para: '+esc(q.nome)+'</option>').join('')
      || '<option value="">crie outro quadro</option>';
    if(atual) bq.value = atual;
  }
  const bar = $('#quadro-bar'); if(!bar) return;
  const abas = QUADROS.map(q=>{
    const ativo = q.id === QUADRO_ATIVO;
    return '<button class="quadro-tab'+(ativo?' ativo':'')+'" style="--quadro-cor:'+esc(q.cor)+'"'
      + ' onclick="quadroTrocar('+q.id+')" title="'+q.leads+' lead(s)">'
      + '<span class="ponto"></span>'
      + '<span>'+esc(q.nome)+'</span>'
      + '<span class="qtd">'+q.leads+'</span>'
      + '<button class="lapis" title="renomear, recolorir ou excluir"'
      + ' onclick="event.stopPropagation();quadroAbrirEditor('+q.id+')">✎</button>'
      + '</button>';
  }).join('');
  bar.innerHTML = abas
    + '<button class="quadro-novo" onclick="quadroAbrirEditor(0)">+ Novo quadro</button>'
    + (QUADRO_EDITANDO===null ? '' : quadroEditorHTML());
  if(QUADRO_EDITANDO!==null){
    const inp = $('#quadro-nome'); if(inp){ inp.focus(); inp.select(); }
  }
}

function quadroEditorHTML(){
  const novo = QUADRO_EDITANDO === 0;
  const q = novo ? {nome:'', cor:'#00ff88', padrao:0, leads:0}
                 : (QUADROS.find(x=>x.id===QUADRO_EDITANDO) || {nome:'',cor:'#00ff88'});
  const outros = QUADROS.filter(x=>x.id!==QUADRO_EDITANDO);
  return '<div class="quadro-editor">'
    + '<div style="flex:1;min-width:200px"><label class="lf-label">'
      + (novo?'Nome do novo quadro':'Nome do quadro')+'</label>'
      + '<input id="quadro-nome" class="lf-input" value="'+esc(q.nome)+'"'
      + ' placeholder="ex.: Clínica Sorriso" onkeydown="if(event.key===\'Enter\')quadroSalvar()"></div>'
    + '<div><label class="lf-label">Cor</label>'
      + '<input id="quadro-cor" type="color" class="lf-input" value="'+esc(q.cor)+'" style="width:56px;padding:3px"></div>'
    + '<button class="btn btn-primary" onclick="quadroSalvar()">'+(novo?'Criar quadro':'Salvar')+'</button>'
    + '<button class="btn" onclick="quadroFecharEditor()">Cancelar</button>'
    + (novo || q.padrao ? '' :
        '<div style="margin-left:auto;display:flex;gap:8px;align-items:end">'
        + (outros.length ? '<div><label class="lf-label">Ao excluir, mover '+q.leads+' lead(s) para</label>'
            + '<select id="quadro-destino" class="lf-input">'
            + outros.map(o=>'<option value="'+o.id+'">'+esc(o.nome)+'</option>').join('')
            + '</select></div>' : '')
        + '<button class="btn btn-danger" onclick="quadroExcluir()">Excluir quadro</button></div>')
    + (q.padrao ? '<div class="muted" style="margin-left:auto;font-size:12px;align-self:center">'
        + 'O quadro padrão não pode ser excluído.</div>' : '');
}

function quadroAbrirEditor(id){ QUADRO_EDITANDO = id; quadrosRender(); }
function quadroFecharEditor(){ QUADRO_EDITANDO = null; quadrosRender(); }

async function quadroTrocar(id){
  if(id === QUADRO_ATIVO){ return; }
  const r = await jsend('/api/quadros/ativo','POST',{quadro_id:id});
  if(r.ok===false){ toast(r.erro||'Não deu para trocar de quadro'); return; }
  QUADRO_ATIVO = id;
  QUADRO_EDITANDO = null;
  clearSelection();
  await quadrosCarregar();
  await recarregarLeads();
  atualizarStats();
  const q = QUADROS.find(x=>x.id===id);
  toast('Quadro: '+(q?q.nome:id));
}

async function quadroSalvar(){
  const nome = ($('#quadro-nome')?$('#quadro-nome').value:'').trim();
  const cor  = $('#quadro-cor') ? $('#quadro-cor').value : '#00ff88';
  if(!nome){ toast('Dê um nome ao quadro'); return; }
  const novo = QUADRO_EDITANDO === 0;
  const r = novo ? await jsend('/api/quadros','POST',{nome,cor})
                 : await jsend('/api/quadros/'+QUADRO_EDITANDO,'PATCH',{nome,cor});
  if(r.ok===false){ toast(r.erro||'Não deu para salvar'); return; }
  QUADRO_EDITANDO = null;
  await quadrosCarregar();
  if(novo){ await quadroTrocar(r.id); toast('Quadro "'+nome+'" criado'); }
  else toast('Quadro salvo');
}

async function quadroExcluir(){
  const id = QUADRO_EDITANDO;
  const destino = $('#quadro-destino') ? parseInt($('#quadro-destino').value) : null;
  const r = await jsend('/api/quadros/'+id,'DELETE',{destino_id:destino});
  if(r.ok===false){ toast(r.erro||'Não deu para excluir'); return; }
  QUADRO_EDITANDO = null;
  toast(r.movidos ? (r.movidos+' lead(s) foram para "'+r.destino_nome+'"') : 'Quadro excluído');
  await quadrosCarregar();
  await recarregarLeads();
  atualizarStats();
}

// COLUNAS — geridas dentro do próprio quadro Antes havia um card "Colunas do
// funil" embaixo do quadro, com a lista inteira repetida.

// as cinco que o fluxo de envio referencia pelo nome

function colEditorEl(col){
  const div = document.createElement('div');
  div.className = 'col-editor';
  const obrig = COLS_OBRIGATORIAS.indexOf(col.key) >= 0;
  const outras = KCOLS.filter(c=>c.key!==col.key);
  div.innerHTML =
      '<div class="linha">'
    +   '<input type="color" class="lf-input" id="cole-cor" value="'+esc(col.cor)+'" style="width:42px;padding:2px">'
    +   '<input type="text" class="lf-input" id="cole-nome" value="'+esc(col.label)+'">'
    + '</div>'
    + (obrig
        ? '<div class="muted" style="font-size:11px">Coluna usada pelo envio: dá para renomear e recolorir, não para excluir.</div>'
        : '<div class="linha"><select class="lf-input" id="cole-destino" style="flex:1">'
          + outras.map(c=>'<option value="'+c.key+'"'+(c.key==='lista'?' selected':'')+'>mover leads para: '+esc(c.label)+'</option>').join('')
          + '</select></div>')
    + '<div class="linha">'
    +   '<button class="btn btn-sm btn-primary" onclick="colSalvar(\''+col.key+'\')">Salvar</button>'
    +   '<button class="btn btn-sm" onclick="colFecharEditor()">Cancelar</button>'
    +   (obrig ? '' : '<button class="btn btn-sm btn-danger" style="margin-left:auto"'
                      + ' onclick="colExcluir(\''+col.key+'\')">Excluir</button>')
    + '</div>';
  return div;
}

function colAbrirEditor(key){
  COL_EDITANDO = (COL_EDITANDO === key) ? null : key;
  renderBoard();
}
function colFecharEditor(){ COL_EDITANDO = null; renderBoard(); }

async function colSalvar(key){
  const nome = $('#cole-nome') ? $('#cole-nome').value.trim() : '';
  const cor  = $('#cole-cor')  ? $('#cole-cor').value : null;
  if(!nome){ toast('A coluna precisa de um nome'); return; }
  const r = await jsend('/api/kanban/colunas/'+encodeURIComponent(key),'PATCH',{rotulo:nome, cor});
  if(r && r.erro){ toast(r.erro); return; }
  COL_EDITANDO = null;
  toast('Coluna salva');
  await recarregarTudoDoQuadro();
}

async function colExcluir(key){
  const destino = $('#cole-destino') ? $('#cole-destino').value : 'lista';
  const r = await jsend('/api/kanban/colunas/'+encodeURIComponent(key),'DELETE',{destino});
  if(r && r.erro){ toast(r.erro); return; }
  COL_EDITANDO = null;
  toast(r.movidos ? ('Coluna excluída · '+r.movidos+' lead(s) movidos') : 'Coluna excluída');
  await recarregarTudoDoQuadro();
}

async function colCriarRapida(){
  const r = await jsend('/api/kanban/colunas','POST',{rotulo:'Nova coluna', cor:'#00ff88'});
  if(r && r.erro){ toast(r.erro); return; }
  await recarregarTudoDoQuadro();
  COL_EDITANDO = r.coluna ? r.coluna.chave : null;   // já abre para nomear
  renderBoard();
}

// Arrastar o CABEÇALHO reordena a coluna. O corpo continua recebendo card:
// são dois arrastes diferentes no mesmo quadro, e `COL_ARRASTANDO` é o que
// impede o card de ser solto no meio de um reordenamento de coluna.
function ligarArrasteDaColuna(el, col){
  const head = $('.col-head', el);
  if(!head) return;
  head.draggable = true;
  head.ondragstart = e=>{
    COL_ARRASTANDO = col.key;
    el.classList.add('col-arrastando');
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('coluna', col.key);
  };
  head.ondragend = ()=>{
    COL_ARRASTANDO = null;
    $$('.col').forEach(c=>c.classList.remove('col-arrastando','col-alvo'));
  };
  el.ondragover = e=>{
    if(!COL_ARRASTANDO || COL_ARRASTANDO===col.key) return;
    e.preventDefault();
    el.classList.add('col-alvo');
  };
  el.ondragleave = ()=>el.classList.remove('col-alvo');
  el.ondrop = async e=>{
    if(!COL_ARRASTANDO || COL_ARRASTANDO===col.key) return;
    e.preventDefault(); e.stopPropagation();
    el.classList.remove('col-alvo');
    const de = COL_ARRASTANDO;
    COL_ARRASTANDO = null;
    const ordem = KCOLS.map(c=>c.key).filter(k=>k!==de);
    ordem.splice(ordem.indexOf(col.key), 0, de);
    const r = await jsend('/api/kanban/colunas/ordem','POST',{chaves:ordem});
    if(r.ok===false){ toast(r.erro||'Não deu para reordenar'); return; }
    await recarregarTudoDoQuadro();
  };
}
// ═══════════════════════════════════════════════════════════════════════════
//  BACKUP E MIGRAÇÃO
// ═══════════════════════════════════════════════════════════════════════════

async function bkpPrevisao(){
  const sess = $('#bkp-sessoes') ? $('#bkp-sessoes').checked : true;
  const logs = $('#bkp-logs') ? $('#bkp-logs').checked : true;
  const d = await jget('/api/backup/previsao?sessoes='+(sess?1:0)+'&logs='+(logs?1:0)).catch(()=>null);
  const box = $('#bkp-previsao'); if(!box || !d) return;
  box.innerHTML = d.itens.map(i=>
      '<div class="list-item" style="opacity:'+(i.existe?1:0.4)+'">'
    + '<span style="flex:1">'+(i.existe?'✓':'—')+' '+esc(i.rotulo)+'</span>'
    + '<span class="muted s">'+(i.existe? i.tamanho_mb+' MB' : 'não existe')+'</span></div>'
  ).join('') + '<div class="list-item" style="font-weight:700;border-top:1px solid #1e293b">'
    + '<span style="flex:1">Total do pacote</span><span>'+d.total_mb+' MB</span></div>';

  const peso = $('#bkp-peso-sessoes');
  if(peso){
    const s = d.itens.filter(i=>i.pesado).reduce((a,b)=>a+b.tamanho_mb,0);
    peso.textContent = sess && s ? '(+'+Math.round(s)+' MB)' : '';
  }
}

async function bkpExportar(){
  toast('Gerando o pacote... isso pode levar alguns minutos');
  const r = await jsend('/api/backup/exportar','POST',{
    sessoes: $('#bkp-sessoes') ? $('#bkp-sessoes').checked : true,
    logs:    $('#bkp-logs')    ? $('#bkp-logs').checked    : true});
  if(r.ok===false){ toast(r.erro||'Falhou ao exportar'); return; }
  toast('Pacote pronto: '+r.nome+' ('+r.tamanho_mb+' MB)');
  bkpListarExports();
}

async function bkpListarExports(){
  const d = await jget('/api/backup/exports').catch(()=>({exports:[]}));
  const box = $('#bkp-exports'); if(!box) return;
  box.innerHTML = (d.exports||[]).length ? d.exports.map(e=>
      '<div class="list-item"><span style="flex:1">'+esc(e.nome)+'</span>'
    + '<span class="muted s">'+e.data+' · '+e.tamanho_mb+' MB</span>'
    + '<a class="btn btn-sm" href="/api/backup/baixar?arquivo='+encodeURIComponent(e.arquivo)+'">⬇ Baixar</a>'
    + '<button class="btn btn-sm" onclick="bkpUsarExistente(\''+esc(e.arquivo).replace(/\\/g,'\\\\')+'\')">Restaurar</button>'
    + '</div>').join('')
    : '<span class="muted">Nenhum pacote gerado ainda.</span>';
}

async function bkpUsarExistente(caminho){
  BKP_ARQUIVO = caminho;
  const r = await jsend('/api/backup/inspecionar','POST',{arquivo:caminho});
  bkpMostrarManifesto(r);
}

async function bkpInspecionar(){
  const f = $('#bkp-arquivo').files[0];
  if(!f) return;
  toast('Lendo o pacote...');
  const fd = new FormData(); fd.append('arquivo', f);
  const r = await (await fetch('/api/backup/inspecionar',{method:'POST',body:fd})).json();
  BKP_ARQUIVO = r.arquivo;
  bkpMostrarManifesto(r);
}

function bkpMostrarManifesto(r){
  const box = $('#bkp-manifesto'); if(!box) return;
  if(!r || r.ok===false){
    box.innerHTML = '<div style="color:#ff4757;font-size:13px">'+esc((r&&r.erro)||'não deu para ler o pacote')+'</div>';
    return;
  }
  const m = r.manifesto || {};
  const res = m.resumo || {};
  const linhas = Object.keys(res).map(k=>k+': '+res[k]).join(' · ');
  box.innerHTML =
      '<div class="card-box" style="border-color:#00ff88">'
    + '<div style="font-weight:700;color:#00ff88;font-size:13px">Pacote válido'
      + (m.formato===1?' (formato antigo)':'')+'</div>'
    + '<div class="muted" style="font-size:12px;margin-top:6px">'
      + (m.gerado_em?('gerado em '+esc(m.gerado_em)):'')
      + (m.maquina?(' · máquina '+esc(m.maquina)):'')
      + ' · '+(m.arquivos||0)+' arquivos · '+(r.tamanho_mb||0)+' MB</div>'
    + (linhas?'<div class="muted" style="font-size:12px;margin-top:4px">'+esc(linhas)+'</div>':'')
    + '<div style="display:flex;gap:16px;flex-wrap:wrap;margin:12px 0 6px">'
    +   '<label style="display:flex;align-items:center;gap:6px;font-size:13px;color:#94a3b8">'
    +     '<input type="checkbox" id="bkp-imp-sessoes" '+(m.inclui_sessoes?'checked':'')+'> restaurar sessões logadas</label>'
    +   '<label style="display:flex;align-items:center;gap:6px;font-size:13px;color:#94a3b8">'
    +     '<input type="checkbox" id="bkp-imp-logs"> restaurar logs</label>'
    + '</div>'
    + '<div style="padding:9px;border:1px solid #ff4757;border-radius:7px;color:#ff4757;font-size:12px">'
    +   'Restaurar SUBSTITUI os dados atuais (leads, contas, sessões). Uma cópia do estado de agora'
    +   ' é guardada antes, em data/backups/slot_00.</div>'
    + '<button class="btn btn-danger" style="margin-top:10px" onclick="bkpImportar(false)">Restaurar este pacote</button>'
    + '<div id="bkp-imp-resultado" style="margin-top:10px"></div>'
    + '</div>';
}

async function bkpImportar(forcar){
  if(!BKP_ARQUIVO){ toast('Escolha um pacote primeiro'); return; }
  toast('Restaurando...');
  const r = await jsend('/api/backup/importar','POST',{
    arquivo: BKP_ARQUIVO,
    sessoes: $('#bkp-imp-sessoes') ? $('#bkp-imp-sessoes').checked : true,
    logs:    $('#bkp-imp-logs')    ? $('#bkp-imp-logs').checked    : false,
    forcar: !!forcar});
  const box = $('#bkp-imp-resultado');
  if(r.ok===false){
    if(box) box.innerHTML = '<div style="color:#ff4757;font-size:12px">'+esc(r.erro||'falhou')+'</div>'
      + (r.precisa_forcar ? '<button class="btn btn-sm btn-danger" style="margin-top:6px"'
          + ' onclick="bkpImportar(true)">Restaurar mesmo assim</button>' : '');
    toast(r.erro||'Não deu para restaurar');
    return;
  }
  if(box) box.innerHTML = '<div style="color:#00ff88;font-size:12px">'
    + r.escritos+' arquivo(s) restaurados. '+esc(r.aviso||'')+'</div>';
  toast('Restaurado. Reinicie o LeadForge.');
}

// TELA DE CARREGAMENTO O painel abria com as abas vazias e ia preenchendo
// conforme cada chamada voltava: por alguns segundos ele mostrava zero lead e
// "Bot Offline", que é exatamente o que se vê quando algo está quebrado.

function bootPasso(texto){
  BOOT_FEITO++;
  const f = document.getElementById('boot-fill');
  const p = document.getElementById('boot-passo');
  if(f) f.style.width = Math.min(100, Math.round(BOOT_FEITO / BOOT_PASSOS * 100)) + '%';
  if(p) p.textContent = texto;
}

function bootFim(){
  const b = document.getElementById('boot');
  const f = document.getElementById('boot-fill');
  if(f) f.style.width = '100%';
  if(!b) return;
  b.classList.add('saindo');
  setTimeout(()=>b.classList.add('pronto'), 380);
}

function bootErro(msg){
  const e = document.getElementById('boot-erro');
  if(e) e.textContent = msg;
  // não trava o usuário na tela de carregamento por causa de um painel que falhou
  setTimeout(bootFim, 1800);
}

// PERSONALIZAÇÃO POR LEAD (personalizar.py + ia_roteador.py / 9router) Um cartão
// por canal, no alto da aba de templates de cada um.
const PERSO_CANAIS = ['whatsapp', 'instagram', 'email'];
const PERSO_NOME = {whatsapp: 'WhatsApp', instagram: 'Instagram', email: 'E-mail'};
const PERSO_NOTA = {
  whatsapp: 'Vale para a fila de primeiro contato, a esteira, o botão do card, a prospecção em lote e o follow-up.',
  instagram: 'Vale para a prospecção por seguidores (a IA lê o nome e a bio de cada perfil) e para as DMs de leads do Maps.',
  email: 'Vale para os e-mails enviados a leads (prospecção em lote e follow-up). Os disparos por lista, mais abaixo, usam o motor do mail bot e ainda não passam por aqui.',
};
const PERSO_EXEMPLO = {
  whatsapp: '{saudacao}! Aqui é o Leonardo, web designer. {se nota: Vi que vocês têm nota {nota} no Google.} {Preparei|Montei} uma análise rápida da presença digital de {nome_curto}. Posso te mandar?',
  instagram: '{Olá|Oi}{se nome: , {nome}}! Tudo bem?\n\nMe chamo Leonardo e encontrei o seu perfil enquanto pesquisava {algumas agências|alguns profissionais} de marketing.\n\nSou Web Designer e trabalho com criação de landing pages e sites focados em conversão. Estou buscando parcerias com quem queira terceirizar essa parte dos projetos, seja para demandas pontuais ou recorrentes.',
  email: '{saudacao},\n\nSou o Leonardo, web designer, e preparei uma análise rápida da presença digital de {nome_curto}. {se nota: A nota de vocês no Google é {nota}, com {avaliacoes} avaliações.}\n\nPosso enviar a análise?\n\nLeonardo',
};
let PERSO = null;
const PERSO_DESENHADO = {};

async function persoCarregar(){
  try { PERSO = await jget('/api/personalizar'); } catch(e){ return; }
  if(!PERSO || !PERSO.canais) return;
  for(const c of PERSO_CANAIS){
    if(!PERSO_DESENHADO[c]) persoDesenhar(c);
    persoAtualizar(c);
  }
}

function persoDesenhar(canal){
  const box = document.getElementById('perso-' + canal);
  if(!box || !PERSO) return;
  const aj = PERSO.canais[canal] || {};
  const vars = canal === 'instagram'
    ? (PERSO.variaveis_perfil || []).concat([{nome: '…', descricao: 'e, nas DMs de leads do Maps, as mesmas variáveis do WhatsApp'}])
    : (PERSO.variaveis || []);
  const perfilIG = canal !== 'instagram' ? '' : `
      <div class="muted" style="font-size:12px;margin-top:8px">Testar como <b>prospecção por seguidores</b>: preencha o @ (e, se quiser, o nome e a bio). Com o @ vazio, o teste usa um lead do Maps.</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">
        <input id="perso-usuario-instagram" class="lf-input" style="width:180px" placeholder="@usuario">
        <input id="perso-nome-instagram" class="lf-input" style="width:160px" placeholder="primeiro nome">
      </div>
      <textarea id="perso-bio-instagram" class="lf-input" rows="2" style="margin-top:6px" placeholder="bio do perfil (o que aparece no cabeçalho)"></textarea>`;
  box.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap">
      <h3 style="margin:0">Personalização por lead — ${PERSO_NOME[canal]}</h3>
      <span id="perso-rt-${canal}" style="font-size:12px"></span>
    </div>
    <p class="muted" style="margin-top:6px">As funções do template (variáveis, variações e blocos) valem sempre. Ligada, a IA do 9router reescreve cada mensagem para o lead; se ela falhar ou o texto for reprovado nas checagens (número inventado, texto cortado, cara de robô), sai o template normal. ${PERSO_NOTA[canal]}</p>
    <div style="display:flex;align-items:center;gap:10px;margin:10px 0">
      <label class="toggle"><input type="checkbox" id="perso-ativa-${canal}" ${aj.ativa ? 'checked' : ''} onchange="persoSalvar('${canal}')"><span class="slider"></span></label>
      <b>Personalizar cada mensagem com IA</b>
    </div>
    <label class="lf-label">Instrução para a IA (opcional)</label>
    <textarea id="perso-instr-${canal}" class="lf-input" rows="2" placeholder="ex.: cite a nota do Google e a cidade; termine com uma pergunta curta">${esc(aj.instrucao || '')}</textarea>
    <button class="btn" style="margin-top:6px" onclick="persoSalvar('${canal}')">Salvar instrução</button>

    <details style="margin-top:14px">
      <summary style="cursor:pointer"><b>Testar num lead</b> <span class="muted">(não envia nada)</span></summary>
      <label class="lf-label" style="margin-top:8px">Texto base</label>
      <textarea id="perso-corpo-${canal}" class="lf-input" rows="6">${esc(PERSO_EXEMPLO[canal])}</textarea>
      ${perfilIG}
      <div style="display:flex;gap:8px;align-items:end;margin-top:8px;flex-wrap:wrap">
        <div><label class="lf-label">Lead (id)</label>
          <input id="perso-lead-${canal}" class="lf-input" style="width:120px" placeholder="aleatório"></div>
        <button class="btn btn-primary" onclick="persoTestar('${canal}')">Testar agora</button>
      </div>
      <div id="perso-res-${canal}" style="margin-top:10px"></div>
    </details>

    <details style="margin-top:8px">
      <summary style="cursor:pointer"><b>Funções do template</b></summary>
      <div class="muted" style="font-size:12px;margin-top:6px;line-height:1.7">
        <div><code>{Oi|Olá|Bom dia}</code> — <b>variação</b>: cada lead recebe uma das opções, sempre a mesma para ele (a prévia bate com o envio).</div>
        <div><code>{se nota: Vi que vocês têm nota {nota} no Google.}</code> — <b>bloco</b> que só aparece se o lead tiver o dado.</div>
        <div><code>{sem site: Vocês ainda não têm site?}</code> — o contrário: só aparece se o dado faltar.</div>
        <div style="margin-top:6px"><b>Variáveis:</b> ${vars.map(v => `<code>{${esc(v.nome)}}</code> ${esc(v.descricao)}`).join(' · ')}</div>
      </div>
    </details>

    <details style="margin-top:8px">
      <summary style="cursor:pointer"><b>Últimas decisões</b></summary>
      <div id="perso-ult-${canal}" style="margin-top:6px"></div>
    </details>`;
  PERSO_DESENHADO[canal] = true;
}

function persoAtualizar(canal){
  if(!PERSO) return;
  const rt = PERSO.roteador || {};
  const el = document.getElementById('perso-rt-' + canal);
  if(el){
    el.innerHTML = !rt.configurado
      ? '<b style="color:#fbbf24">sem a chave do 9router no .env</b>'
      : (rt.no_ar ? '<b style="color:#00ff88">● 9router no ar</b>'
                  : '<b style="color:#f87171">● 9router fechado</b> <button class="btn" style="padding:2px 8px;font-size:11px;margin-left:4px" onclick="persoAbrir9router()">abrir</button>');
  }
  const chk = document.getElementById('perso-ativa-' + canal);
  if(chk && document.activeElement !== chk) chk.checked = !!((PERSO.canais || {})[canal] || {}).ativa;
  const ult = document.getElementById('perso-ult-' + canal);
  if(ult){
    const itens = (PERSO.ultimas || {})[canal] || [];
    ult.innerHTML = itens.length ? itens.map(i => `
      <div style="font-size:12px;padding:5px 0;border-bottom:1px solid #1f2b3d">
        <span class="muted">${esc(i.criado_em || '')}</span> · ${esc(i.alvo || '')} ·
        ${i.resultado === 'personalizada'
          ? `<b style="color:#00ff88">personalizada</b>${i.modelo ? ' <span class="muted">(' + esc(i.modelo) + ')</span>' : ''}`
          : `<b style="color:#fbbf24">saiu o template</b> — ${esc(i.motivo || '')}`}
      </div>`).join('') : '<div class="muted" style="font-size:12px">Nenhuma mensagem personalizada ainda.</div>';
  }
}

async function persoSalvar(canal){
  const chk = document.getElementById('perso-ativa-' + canal);
  const instr = document.getElementById('perso-instr-' + canal);
  const ativa = !!(chk && chk.checked);
  if(ativa) toast('Salvando… se o 9router estiver fechado, ele será aberto');
  let r;
  try { r = await jsend('/api/personalizar', 'POST', {canal, ativa, instrucao: instr ? instr.value : ''}); }
  catch(e){ toast('Não salvou: ' + e); return; }
  if(!r || !r.ok){ toast('Não salvou: ' + ((r && r.erro) || 'erro')); return; }
  PERSO.canais = r.canais; PERSO.roteador = r.roteador;
  PERSO_CANAIS.forEach(persoAtualizar);
  const rt = r.roteador || {};
  toast(`Personalização do ${PERSO_NOME[canal]} ${ativa ? 'LIGADA' : 'desligada'}`
        + (ativa ? (rt.no_ar ? ' · 9router no ar' : ' · 9router FECHADO — as mensagens saem do template') : ''));
}

async function persoTestar(canal){
  const res = document.getElementById('perso-res-' + canal);
  const pedido = {canal,
                  corpo: document.getElementById('perso-corpo-' + canal).value,
                  lead_id: document.getElementById('perso-lead-' + canal).value.trim()};
  if(canal === 'instagram'){
    const u = document.getElementById('perso-usuario-instagram').value.trim().replace(/^@/, '');
    if(u) pedido.perfil = {usuario: u,
                           nome: document.getElementById('perso-nome-instagram').value.trim(),
                           bio: document.getElementById('perso-bio-instagram').value};
  }
  res.innerHTML = '<div class="muted">Gerando com a IA do 9router… (pode levar até ~50 s se os modelos estiverem lentos)</div>';
  let r;
  try { r = await jsend('/api/personalizar/testar', 'POST', pedido); }
  catch(e){ res.innerHTML = `<div style="color:#f87171">Falhou: ${esc(e)}</div>`; return; }
  if(!r || !r.ok){ res.innerHTML = `<div style="color:#f87171">${esc((r && r.erro) || 'falhou')}</div>`; return; }
  const ok = r.personalizado;
  const tent = (r.tentativas || []).map(t => `${esc(t.modelo)}: ${esc(t.resultado)}${t.segundos ? ' (' + t.segundos + 's)' : ''}`).join(' · ');
  res.innerHTML = `
    <div class="muted" style="font-size:12px">${esc(r.alvo || '')} · ${r.segundos}s${r.modelo ? ' · ' + esc(r.modelo) : ''}</div>
    <div class="grid-2" style="margin-top:6px">
      <div><div class="lf-label">Template montado <span class="muted">(é o que sai se a IA falhar)</span></div>
        <div style="white-space:pre-wrap;font-size:13px;padding:8px;border:1px solid #2a3a52;border-radius:6px">${esc(r.base || '')}</div></div>
      <div><div class="lf-label">${ok ? '<span style="color:#00ff88">Versão da IA — aprovada</span>' : '<span style="color:#fbbf24">A IA não passou — sairia o template</span>'}</div>
        <div style="white-space:pre-wrap;font-size:13px;padding:8px;border:1px solid ${ok ? '#00ff88' : '#2a3a52'};border-radius:6px">${esc(ok ? r.texto : (r.reprovada || '—'))}</div>
        ${ok ? '' : `<div style="font-size:12px;color:#fbbf24;margin-top:4px">${esc(r.motivo || '')}</div>`}</div>
    </div>
    ${tent ? `<div class="muted" style="font-size:11px;margin-top:6px">Tentativas: ${tent}</div>` : ''}`;
}

async function persoAbrir9router(){
  toast('Abrindo o 9router…');
  let r;
  try { r = await jsend('/api/personalizar/9router', 'POST', {}); } catch(e){ toast('9router: ' + e); return; }
  if(PERSO && r && r.roteador){ PERSO.roteador = r.roteador; PERSO_CANAIS.forEach(persoAtualizar); }
  toast(r && r.ok ? '9router no ar' : ('9router: ' + ((r && r.resultado && r.resultado.detalhe) || 'não abriu')));
}

// status e decisões se atualizam sozinhos, mas só com um dos cartões à vista
setInterval(() => {
  const visivel = PERSO_CANAIS.some(c => { const el = document.getElementById('perso-' + c); return el && el.offsetParent !== null; });
  if(visivel) persoCarregar();
}, 30000);
persoCarregar();
