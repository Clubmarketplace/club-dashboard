/**
 * Conversas de pós-venda (28/10) -- a MESMA tela para o seller e para a equipe.
 *   <div id="pv-app" data-base="/api/meu-pos-venda" data-modo="seller"></div>
 *   <div id="pv-app" data-base="/api/pos-venda/conversas" data-modo="equipe"></div>
 *
 * Abas: "Aguardando" (lista + conversa + resposta) e "Histórico e relatório"
 * (data, dia, hora, o que o comprador disse, quem respondeu e o quê; números do período; Excel).
 * Nada é enviado sozinho: a sugestão da IA só preenche o campo, quem decide e clica é a pessoa.
 * Regras do Mercado Livre (350 caracteres, sem link nem dado pessoal) são avisadas aqui E conferidas no servidor.
 */
(function () {
  var raiz = document.getElementById("pv-app");
  if (!raiz) return;
  var BASE = raiz.dataset.base, EQUIPE = raiz.dataset.modo === "equipe";
  var LIMITE = 350, SEG_ATUALIZA = 30;
  var estado = { itens: [], respondidas: [], selId: null, aba: "aguardando", dias: 10, conta: "", contasReconectar: [], varredura: null, inicio: "", iniciado: true };
  var atual = null; // conversa aberta: {item, sugestao, podeResponder}

  function esc(t) { return String(t == null ? "" : t).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  function quando(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }
  function espera(iso) {
    if (!iso) return { txt: "", cor: "#6b7280" };
    var h = Math.max(0, (Date.now() - new Date(iso).getTime()) / 3600000);
    var txt = h < 1 ? "esperando há " + Math.max(1, Math.round(h * 60)) + " min" : h < 24 ? "esperando há " + Math.floor(h) + " h" : "esperando há " + Math.floor(h / 24) + " d " + Math.floor(h % 24) + " h";
    return { txt: txt, cor: h >= 48 ? "#b91c1c" : h >= 24 ? "#b45309" : "#15803d" };
  }
  function minutosTxt(m) { return m == null ? "—" : m < 60 ? m + " min" : m < 1440 ? Math.floor(m / 60) + " h " + (m % 60) + " min" : Math.floor(m / 1440) + " d " + Math.floor((m % 1440) / 60) + " h"; }

  // Mesmas regras do servidor (pos_venda_conversas.validar_texto), só para avisar enquanto digita.
  function validar(t) {
    t = (t || "").trim();
    if (!t) return "Escreva a resposta antes de enviar.";
    if (t.length > LIMITE) return "Passou de " + LIMITE + " caracteres.";
    if (/[\w.+-]+@[\w-]+/i.test(t)) return "Não inclua e-mail ou redes sociais: o Mercado Livre não aceita.";
    if (/(https?:\/\/|www\.|\b[\w-]+\.(com|com\.br|br|net|org|me|io|app|shop|store)\b)/i.test(t)) return "O Mercado Livre não aceita links na mensagem.";
    var achados = t.match(/\+?\d[\d\s().-]{6,}\d/g) || [];
    for (var i = 0; i < achados.length; i++) { var d = achados[i].replace(/\D/g, "").length; if (d >= 8 && d <= 15) return "Não inclua telefone ou outros dados pessoais: o Mercado Livre não aceita."; }
    return "";
  }

  async function api(caminho, opcoes) {
    var r = await fetch(BASE + caminho, Object.assign({ cache: "no-store", headers: { "Content-Type": "application/json" } }, opcoes || {}));
    var corpo = await r.json().catch(function () { return {}; });
    if (!r.ok) { var e = new Error(corpo.detail || ("erro " + r.status)); e.status = r.status; throw e; }
    return corpo;
  }

  // ------------------------------------------------------------ esqueleto
  raiz.innerHTML =
    '<style>' +
    '.pv-abas{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap}' +
    '.pv-aba{min-height:44px;padding:0 18px;border-radius:10px;border:2px solid #111827;background:#fff;color:#111827;font-weight:700;font-size:14px;font-family:inherit;cursor:pointer}' +
    '.pv-aba.ativa{background:#111827;color:#fff}' +
    '.pv-grade{display:grid;grid-template-columns:minmax(280px,380px) minmax(0,1fr);gap:14px;align-items:start}' +
    '@media(max-width:800px){.pv-grade{grid-template-columns:1fr}}' +
    '.pv-card{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:14px}' +
    '.pv-item{display:block;width:100%;text-align:left;cursor:pointer;border:2px solid #e5e7eb;background:#fff;border-radius:10px;padding:10px 12px;margin-top:8px;font:inherit;color:inherit}' +
    '.pv-item.sel{border-color:#111827;background:#f9fafb}' +
    '.pv-linha{display:flex;justify-content:space-between;gap:8px;flex-wrap:wrap;align-items:center}' +
    '.pv-selo{font-size:12px;font-weight:700;padding:2px 8px;border-radius:99px}' +
    '.pv-msgs{background:#f3f4f6;border-radius:10px;padding:12px;display:flex;flex-direction:column;gap:8px;max-height:320px;overflow:auto;margin:10px 0}' +
    '.pv-bolha{max-width:80%;border-radius:12px;padding:8px 12px;font-size:13.5px;white-space:pre-wrap;word-break:break-word}' +
    '.pv-meta{font-size:11px;color:#6b7280;margin-top:3px}' +
    '.pv-btn{min-height:44px;border-radius:10px;border:0;background:#111827;color:#fff;font-weight:700;font-size:15px;font-family:inherit;cursor:pointer;padding:0 18px}' +
    '.pv-btn[disabled]{background:#9ca3af;cursor:not-allowed}' +
    '.pv-btn2{min-height:40px;border-radius:8px;border:1px solid #6366f1;background:#eef2ff;color:#3730a3;font-weight:700;font-size:13px;font-family:inherit;cursor:pointer;padding:0 12px}' +
    '.pv-aviso{border-radius:10px;padding:10px 12px;font-size:13.5px;margin-top:8px}' +
    '.pv-cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:12px 0}' +
    '.pv-num{background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:12px;text-align:center}' +
    '.pv-num b{display:block;font-size:24px}.pv-num span{font-size:12px;color:#6b7280}' +
    '.pv-tab{width:100%;border-collapse:collapse;font-size:13px}' +
    '.pv-tab th{text-align:left;font-size:11px;color:#6b7280;padding:8px;border-bottom:1px solid #e5e7eb;background:#fafbfc}' +
    '.pv-tab td{padding:8px;border-bottom:1px solid #f0f1f3;vertical-align:top}' +
    '.pv-rolagem{overflow-x:auto}' +
    '</style>' +
    '<div class="pv-abas"><button type="button" class="pv-aba ativa" data-aba="aguardando">Aguardando <span id="pv-n"></span></button>' +
    '<button type="button" class="pv-aba" data-aba="historico">Histórico e relatório</button></div>' +
    '<div id="pv-aviso-geral"></div><div id="pv-corpo"></div>';
  var corpo = document.getElementById("pv-corpo"), avisoGeral = document.getElementById("pv-aviso-geral");
  raiz.querySelectorAll(".pv-aba").forEach(function (b) {
    b.addEventListener("click", function () {
      liberarAtual();
      estado.aba = b.dataset.aba;
      raiz.querySelectorAll(".pv-aba").forEach(function (x) { x.classList.toggle("ativa", x === b); });
      desenhar();
    });
  });

  // ------------------------------------------------------------ dados
  async function carregarLista() {
    var qs = EQUIPE && estado.conta ? "?conta=" + encodeURIComponent(estado.conta) : "";
    var d = await api("/lista" + qs);
    estado.itens = d.itens; estado.respondidas = d.respondidas; estado.contasReconectar = d.contas_reconectar || []; estado.varredura = d.varredura || null; estado.inicio = d.inicio; estado.iniciado = d.iniciado;
    var n = document.getElementById("pv-n"); if (n) n.textContent = d.itens.length ? "(" + d.itens.length + ")" : "";
  }

  function liberarAtual() {
    if (atual && atual.podeResponder && atual.item) {
      try { navigator.sendBeacon(BASE + "/" + atual.item.id + "/liberar"); } catch (e) { /* a trava expira sozinha */ }
    }
    atual = null; estado.selId = null;
  }
  window.addEventListener("pagehide", function () { liberarAtual(); });

  async function atualizar() {
    try { await carregarLista(); } catch (e) { avisoGeral.innerHTML = '<div class="pv-aviso" style="background:#fdeceb;color:#8a1f14">Não consegui carregar as mensagens agora.</div>'; return; }
    avisoGeral.innerHTML = "";
    if (EQUIPE && estado.contasReconectar.length) {
      avisoGeral.innerHTML = '<div class="pv-aviso" style="background:#fff4e0;color:#7a3e00;margin-bottom:10px"><b>Reconectar conta:</b> ' + estado.contasReconectar.map(esc).join(", ") + ' precisa(m) ser reconectada(s) ao Mercado Livre para responder.</div>';
    }
    if (estado.aba === "aguardando") { desenharLista(); }
  }

  // ------------------------------------------------------------ aba Aguardando
  function desenhar() { if (estado.aba === "aguardando") desenharAguardando(); else desenharHistorico(); }

  function desenharAguardando() {
    var filtro = "";
    if (EQUIPE) {
      var contas = {}; estado.itens.concat(estado.respondidas).forEach(function (i) { contas[i.conta] = 1; });
      filtro = '<div class="pv-linha" style="margin-bottom:10px;gap:8px"><select id="pv-conta" aria-label="Conta" style="min-height:40px;border:1px solid #d1d5db;border-radius:8px;padding:0 8px;font:inherit"><option value="">Todas as contas</option>' +
        Object.keys(contas).sort().map(function (c) { return '<option' + (c === estado.conta ? " selected" : "") + ">" + esc(c) + "</option>"; }).join("") + '</select>' +
        '<button type="button" class="pv-btn2" id="pv-sync" ' + (estado.varredura && estado.varredura.rodando ? "disabled" : "") + '>' + (estado.varredura && estado.varredura.rodando ? "Buscando…" : "↻ Atualizar agora") + '</button>' +
        '<button type="button" class="pv-btn2" id="pv-ant" style="border-color:#9ca3af;background:#f3f4f6;color:#374151" ' + (estado.varredura && estado.varredura.rodando ? "disabled" : "") + '>Buscar mensagens anteriores (10 dias)</button></div>';
    }
    corpo.innerHTML = filtro + '<div class="pv-grade"><div class="pv-card"><b id="pv-titulo-lista"></b><div id="pv-lista"></div></div><div class="pv-card" id="pv-detalhe"></div></div>';
    if (EQUIPE) {
      document.getElementById("pv-conta").addEventListener("change", function (e) { estado.conta = e.target.value; atualizar().then(desenharAguardando); });
      document.getElementById("pv-sync").addEventListener("click", async function () {
        try { await api("/sincronizar", { method: "POST" }); this.disabled = true; this.textContent = "Buscando…"; setTimeout(atualizar, 15000); } catch (e) { alert(e.message); }
      });
      document.getElementById("pv-ant").addEventListener("click", async function () {
        if (!confirm("Buscar também as mensagens dos últimos 10 dias, de antes de " + estado.inicio + "?\nElas entram na fila da equipe e do seller de cada conta.")) return;
        try { await api("/sincronizar?anteriores=true", { method: "POST" }); this.disabled = true; this.textContent = "Buscando…"; setTimeout(atualizar, 20000); } catch (e) { alert(e.message); }
      });
    }
    desenharLista();
    desenharDetalhe();
  }

  function desenharLista() {
    var lista = document.getElementById("pv-lista"); if (!lista) return;
    document.getElementById("pv-titulo-lista").textContent = (EQUIPE ? "Mensagens dos pedidos (todas as contas)" : "Mensagens dos seus compradores");
    if (!estado.itens.length) { lista.innerHTML = '<p style="color:#6b7280;font-size:13.5px;text-align:center;padding:18px">' + (estado.iniciado ? "Nenhuma mensagem aguardando resposta. 🎉" : "As mensagens começam a chegar aqui a partir de <b>" + esc(estado.inicio) + "</b>.") + '<br><small>Contamos as mensagens recebidas desde ' + esc(estado.inicio) + '.</small></p>'; return; }
    lista.innerHTML = estado.itens.map(function (i) {
      var e = espera(i.recebida_em);
      var selo = i.em_atendimento_por ? ['Em atendimento: ' + esc(i.em_atendimento_por), '#e0e7ff', '#3730a3'] : ['Aguardando', '#fde9cf', '#8a4a07'];
      return '<button type="button" class="pv-item' + (i.id === estado.selId ? " sel" : "") + '" data-id="' + i.id + '">' +
        '<div class="pv-linha"><b style="font-size:14px">' + esc(i.comprador) + '</b><span class="pv-selo" style="background:' + selo[1] + ';color:' + selo[2] + '">' + selo[0] + '</span></div>' +
        '<div style="font-size:12.5px;color:#6b7280">' + (EQUIPE ? esc(i.conta) + " · " : "") + (i.pedido ? "pedido " + esc(i.pedido) : "") + '</div>' +
        '<div style="font-size:13px;color:#374151;margin-top:3px">“' + esc(i.ultima.length > 80 ? i.ultima.slice(0, 80) + "…" : i.ultima) + '”</div>' +
        (i.pedido_cancelamento ? '<div style="font-size:12px;color:#b91c1c;font-weight:700;margin-top:3px">possível pedido de cancelamento</div>' : "") +
        '<div style="font-size:12px;font-weight:600;color:' + e.cor + ';margin-top:3px">' + e.txt + '</div></button>';
    }).join("");
    lista.querySelectorAll(".pv-item").forEach(function (b) { b.addEventListener("click", function () { abrir(parseInt(b.dataset.id, 10)); }); });
  }

  async function abrir(id) {
    if (estado.selId === id) return;
    liberarAtual();
    estado.selId = id;
    desenharLista();
    var det = document.getElementById("pv-detalhe"); det.innerHTML = '<p style="color:#6b7280">Abrindo a conversa…</p>';
    try {
      var d = await api("/" + id + "/abrir", { method: "POST" });
      atual = { item: d.item, conversa: d.conversa, sugestao: d.sugestao, podeResponder: d.pode_responder };
      if (d.item.status === "respondida") { await atualizar(); }
    } catch (e) { det.innerHTML = '<div class="pv-aviso" style="background:#fdeceb;color:#8a1f14">' + esc(e.message) + '</div>'; estado.selId = null; return; }
    desenharDetalhe();
    document.getElementById("pv-detalhe").scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  function desenharDetalhe() {
    var det = document.getElementById("pv-detalhe"); if (!det) return;
    if (!atual) { det.innerHTML = '<p style="color:#6b7280;text-align:center;padding:40px 12px;font-size:14px">Escolha uma mensagem na lista para ver a conversa e responder.</p>'; return; }
    var it = atual.item;
    var msgs = (atual.conversa || []).map(function (m) {
      if (m.de === "aviso") return '<div class="pv-aviso" style="background:#fff4e0;color:#7a3e00">' + esc(m.texto) + '</div>';
      var v = m.de === "vendedor";
      return '<div style="display:flex;justify-content:' + (v ? "flex-end" : "flex-start") + '"><div class="pv-bolha" style="background:' + (v ? "#dbeafe" : "#fff") + '">' + esc(m.texto) + '<div class="pv-meta">' + (v ? "Vendedor" : "Comprador") + (m.quando ? " · " + quando(m.quando) : "") + '</div></div></div>';
    }).join("");
    var topo = '<div class="pv-linha"><div><b style="font-size:16px">' + esc(it.comprador) + '</b><div style="font-size:12.5px;color:#6b7280">' + esc(it.conta) + (it.pedido ? " · pedido " + esc(it.pedido) : "") + (it.produto ? " · " + esc(it.produto) : "") + '</div></div></div>' +
      '<div class="pv-msgs" id="pv-msgs">' + msgs + '</div>';
    var rodape = "";
    if (it.status === "respondida") {
      rodape = '<div class="pv-aviso" style="background:#e8f6ee;color:#14532d;border:2px solid #2f9e63"><b>Respondida' + (it.respondida_por ? " por " + esc(it.respondida_por) : "") + '.</b> O comprador viu a resposta no chat do Mercado Livre.</div>';
    } else if (!atual.podeResponder) {
      rodape = '<div class="pv-aviso" style="background:#fde9cf;color:#7a3e00;border:1px solid #f0b36d"><b>Em atendimento por ' + esc(it.em_atendimento_por || "outra pessoa") + '.</b> Para não ir resposta dobrada, só quem está atendendo envia.' +
        (EQUIPE ? '<div style="margin-top:8px"><button type="button" class="pv-btn2" id="pv-assumir">Assumir no lugar de ' + esc(it.em_atendimento_por || "outra pessoa") + '</button></div>' : "") + '</div>';
    } else {
      rodape = '<div class="pv-aviso" style="background:#e0e7ff;color:#3730a3;margin:0 0 8px">Em atendimento por <b>você</b>. Se sair sem responder, a mensagem volta para a fila.</div>' +
        '<div class="pv-linha"><label for="pv-resp" style="font-size:12px;font-weight:600;color:#6b7280">Sua resposta ao comprador</label>' +
        (atual.sugestao ? '<button type="button" class="pv-btn2" id="pv-ia">✨ Usar sugestão da IA</button>' : "") + '</div>' +
        '<textarea id="pv-resp" rows="4" placeholder="Escreva ao comprador" style="width:100%;box-sizing:border-box;padding:10px 12px;border:1px solid #d1d5db;border-radius:8px;font-size:15px;font-family:inherit;resize:vertical;margin-top:6px"></textarea>' +
        '<div class="pv-linha" style="font-size:12.5px;margin:4px 0 8px"><span id="pv-dica" style="color:#6b7280"></span><b id="pv-cont" style="color:#6b7280">0/' + LIMITE + '</b></div>' +
        '<button type="button" class="pv-btn" id="pv-enviar" disabled style="width:100%">Enviar pelo Mercado Livre</button><div id="pv-msg-envio"></div>';
    }
    det.innerHTML = topo + rodape;
    var caixa = document.getElementById("pv-msgs"); if (caixa) caixa.scrollTop = caixa.scrollHeight;
    var ass = document.getElementById("pv-assumir");
    if (ass) ass.addEventListener("click", async function () {
      try { var d = await api("/" + it.id + "/assumir", { method: "POST" }); atual = { item: d.item, conversa: d.conversa, sugestao: d.sugestao, podeResponder: d.pode_responder }; desenharDetalhe(); atualizar(); } catch (e) { alert(e.message); }
    });
    var area = document.getElementById("pv-resp");
    if (area) {
      var ia = document.getElementById("pv-ia"), btn = document.getElementById("pv-enviar");
      function checa() {
        var erro = validar(area.value), n = area.value.length;
        document.getElementById("pv-cont").textContent = n + "/" + LIMITE;
        document.getElementById("pv-cont").style.color = n > LIMITE ? "#b91c1c" : "#6b7280";
        var dica = document.getElementById("pv-dica"); dica.textContent = area.value.trim() ? (erro || "Pronto para enviar quando você revisar.") : ""; dica.style.color = erro && area.value.trim() ? "#b91c1c" : "#6b7280";
        btn.disabled = !!erro;
      }
      area.addEventListener("input", checa);
      if (ia) ia.addEventListener("click", function () { area.value = atual.sugestao; checa(); area.focus(); });
      btn.addEventListener("click", async function () {
        if (validar(area.value)) return;
        btn.disabled = true; btn.textContent = "Enviando…";
        try {
          await api("/" + it.id + "/responder", { method: "POST", body: JSON.stringify({ texto: area.value }) });
          atual = null; estado.selId = null;
          await atualizar(); desenharAguardando();
          avisoGeral.innerHTML = '<div class="pv-aviso" style="background:#e8f6ee;color:#14532d;margin-bottom:10px">✓ Resposta enviada. O comprador vê no chat do Mercado Livre como mensagem normal do vendedor.</div>';
          setTimeout(function () { avisoGeral.innerHTML = ""; }, 6000);
        } catch (e) {
          btn.disabled = false; btn.textContent = "Enviar pelo Mercado Livre";
          document.getElementById("pv-msg-envio").innerHTML = '<div class="pv-aviso" style="background:#fdeceb;color:#8a1f14">' + esc(e.message) + '</div>';
          if (e.status === 409) { atualizar(); }
        }
      });
    }
  }

  // ------------------------------------------------------------ aba Histórico e relatório
  async function desenharHistorico() {
    corpo.innerHTML = '<p style="color:#6b7280">Carregando…</p>';
    var conta = EQUIPE && estado.conta ? "&conta=" + encodeURIComponent(estado.conta) : "";
    var d;
    try { d = await api("/relatorio?dias=" + estado.dias + conta); } catch (e) { corpo.innerHTML = '<div class="pv-aviso" style="background:#fdeceb;color:#8a1f14">' + esc(e.message) + '</div>'; return; }
    var r = d.resumo;
    var opcoes = [7, 10, 30, 90].map(function (n) { return '<option value="' + n + '"' + (n === estado.dias ? " selected" : "") + '>Últimos ' + n + ' dias</option>'; }).join("");
    var num = function (v, rot) { return '<div class="pv-num"><b>' + v + '</b><span>' + rot + '</span></div>'; };
    var porConta = EQUIPE && r.por_conta.length ? '<h3 style="font-size:14px;margin:14px 0 6px">Por conta</h3><div class="pv-rolagem"><table class="pv-tab"><tr><th>Conta</th><th>Recebidas</th><th>Pelo sistema</th><th>Direto no ML</th><th>Aguardando</th></tr>' +
      r.por_conta.map(function (c) { return "<tr><td>" + esc(c.conta) + "</td><td>" + c.recebidas + "</td><td>" + c.pelo_sistema + "</td><td>" + c.no_ml + "</td><td>" + c.aguardando + "</td></tr>"; }).join("") + "</table></div>" : "";
    var linhas = d.itens.map(function (i) {
      return "<tr><td style='white-space:nowrap'>" + esc(i.recebida.data) + "<br><small style='color:#6b7280'>" + esc(i.recebida.dia) + " · " + esc(i.recebida.hora) + "</small></td>" +
        (EQUIPE ? "<td>" + esc(i.conta) + "</td>" : "") +
        "<td>" + esc(i.comprador) + (i.pedido ? "<br><small style='color:#6b7280'>pedido " + esc(i.pedido) + "</small>" : "") + "</td>" +
        "<td style='min-width:200px'>" + esc(i.mensagem) + "</td>" +
        "<td style='white-space:nowrap'>" + (i.status === "Respondida" ? "✓ " + esc(i.respondida.data) + "<br><small style='color:#6b7280'>" + esc(i.respondida.hora) + " · " + minutosTxt(i.tempo_min) + "</small>" : "<b style='color:#b45309'>Aguardando</b>") + "</td>" +
        "<td>" + esc(i.respondida_por) + (i.origem ? "<br><small style='color:#6b7280'>" + esc(i.origem) + "</small>" : "") + "</td>" +
        "<td style='min-width:200px'>" + esc(i.resposta) + "</td></tr>";
    }).join("");
    corpo.innerHTML =
      '<div class="pv-card"><div class="pv-linha" style="gap:8px"><select id="pv-dias" aria-label="Período" style="min-height:40px;border:1px solid #d1d5db;border-radius:8px;padding:0 8px;font:inherit">' + opcoes + '</select>' +
      '<a class="pv-btn2" style="display:inline-flex;align-items:center;text-decoration:none" href="' + BASE + '/relatorio/excel?dias=' + estado.dias + conta + '">⬇ Baixar relatório (Excel)</a></div>' +
      '<div class="pv-cards">' + num(r.recebidas, "mensagens recebidas") + num(r.pelo_sistema, "respondidas pelo nosso sistema") + num(r.direto_no_ml, "respondidas direto no ML") + num(r.aguardando, "aguardando resposta") +
      num(r.pct_pelo_sistema == null ? "—" : r.pct_pelo_sistema + "%", "intermediado pelo sistema") + num(minutosTxt(r.tempo_medio_min), "tempo médio de resposta") + '</div>' + porConta +
      '<h3 style="font-size:14px;margin:14px 0 6px">Histórico</h3><div class="pv-rolagem"><table class="pv-tab"><tr><th>Chegou</th>' + (EQUIPE ? "<th>Conta</th>" : "") + '<th>Comprador</th><th>Mensagem</th><th>Respondida</th><th>Por quem</th><th>Resposta enviada</th></tr>' +
      (linhas || '<tr><td colspan="7" style="text-align:center;color:#6b7280;padding:18px">Nenhuma mensagem nesse período.</td></tr>') + '</table></div>' +
      (d.itens.length >= 500 ? '<p style="font-size:12px;color:#6b7280">Mostrando as 500 mais recentes; o Excel traz a mesma lista.</p>' : "") + '</div>';
    document.getElementById("pv-dias").addEventListener("change", function (e) { estado.dias = parseInt(e.target.value, 10); desenharHistorico(); });
  }

  // ------------------------------------------------------------ início
  atualizar().then(function () { desenhar(); });
  setInterval(function () { if (document.visibilityState === "visible" && estado.aba === "aguardando") atualizar(); }, SEG_ATUALIZA * 1000);
})();
