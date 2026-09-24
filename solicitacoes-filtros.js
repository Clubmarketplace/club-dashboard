/**
 * Barra de filtros das Solicitações de Cancelamento (v5).
 *
 * Usada em duas telas: Solicitações de Cancelamento (interna) e
 * Solicitações do galpão (logística).
 *
 * Como funciona (decidido com a equipe):
 *   - A tela ABRE MOSTRANDO TUDO: todas as contas, todos os status.
 *   - Os filtros são um FORMULÁRIO: Conta, Status, Galpão, Período e
 *     "Mais filtros" (plataforma e origem). Nada muda até clicar em
 *     "Aplicar filtros" -- o botão acende e mostra quantas escolhas estão
 *     pendentes, pra ninguém ficar na dúvida se aplicou ou não.
 *   - A busca por nº da venda aplica com Enter ou no botão "Buscar".
 *   - Ao lado da busca: "Atualizando…" enquanto carrega e, depois,
 *     "N pedido(s) · atualizado às HH:MM:SS".
 *   - O que está valendo aparece como etiquetas, com ✕ pra remover.
 *   - Abas (só na tela interna) são atalhos de atendimento: Todos,
 *     Em atendimento, Meus -- aplicam na hora.
 *   - Tudo fica no endereço da página: recarregar não perde, e dá pra
 *     mandar o link já filtrado.
 *
 * Uso:
 *   const barra = criarBarraFiltros({
 *     container, onChange,              // onChange: recarrega a lista
 *     mostrarAtendimento: true,         // abas Todos / Em atendimento / Meus
 *     mostrarOrigem: true,              // "origem" dentro de Mais filtros
 *     ordem: "espera",                  // "espera" (pendentes primeiro) | "recentes"
 *     acaoDireita: elemento,            // ex.: botão "Gerar relatório"
 *   });
 *   barra.params()        -> parâmetros pro /busca
 *   barra.estado()        -> filtros aplicados (pro relatório)
 *   barra.atualizarContadores(contadores, atendimento)  -> também marca "atualizado às…"
 */
(function () {
  const PADRAO = { aba: "todos", conta: "", status: "todos", galpao: "", dias: "30", de: "", ate: "", plataforma: "", origem: "", busca: "" };
  const CAMPOS_FORM = ["conta", "status", "galpao", "dias", "de", "ate", "plataforma", "origem"];
  const NOMES_STATUS = { todos: "Todos", pendente: "Aguardando", confirmado: "Confirmados" };
  const NOMES_PERIODO = { "1": "Hoje", "7": "Últimos 7 dias", "30": "Últimos 30 dias", personalizado: "Datas" };
  const NOMES_ORIGEM = { seller: "Sellers", logistica: "Galpão", publico: "Link público" };
  // Padrões; atualizados pelo servidor (/opcoes e /contas), que são a fonte única.
  let PLATAFORMAS = [["mercado_livre", "Mercado Livre"], ["shopee", "Shopee"], ["magalu", "Magalu"], ["tiktok_shop", "TikTok Shop"]];
  let GALPOES = [[1, "Galpão 1"], [2, "Galpão 2"], [3, "Galpão 3"]];
  let CONTAS = [];

  const ICONE_BUSCA = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>';

  function injetarEstilo() {
    if (document.getElementById("estilo-barra-filtros-v5")) return;
    const css = `
      .bf2 { background:#fff; border:1px solid #e2e6ea; border-radius:14px; margin-bottom:16px; }
      .bf2-topo { display:flex; justify-content:space-between; align-items:flex-end; gap:12px; padding:0 18px; border-bottom:1px solid #e2e6ea; flex-wrap:wrap; min-height:52px; }
      .bf2-topo.sem-abas { align-items:center; }
      .bf2-titulo { font-size:14px; font-weight:600; color:#1b2430; padding:14px 0; }
      .bf2-abas { display:flex; gap:2px; flex-wrap:wrap; }
      .bf2-aba { background:none; border:none; border-bottom:2px solid transparent; padding:14px 10px 11px; font:inherit; font-size:14px; color:#5b6776; cursor:pointer; display:inline-flex; align-items:center; gap:7px; }
      .bf2-aba:hover { color:#1b2430; }
      .bf2-aba.on { color:#1b2430; font-weight:600; border-bottom-color:#1b2430; }
      .bf2-n { font-size:11.5px; font-weight:600; border-radius:999px; padding:1px 8px; background:#f1f3f5; color:#5b6776; }
      .bf2-aba.on .bf2-n { background:#1b2430; color:#fff; }
      .bf2-acoes { padding:8px 0 9px; display:flex; gap:8px; }
      .bf2-form { display:flex; gap:12px; align-items:flex-end; padding:16px 18px 6px; flex-wrap:wrap; }
      .bf2-campo { display:flex; flex-direction:column; gap:5px; }
      .bf2-campo label { font-size:12px; font-weight:600; color:#5b6776; }
      .bf2-campo select, .bf2-campo input, .bf2-busca input { height:38px; box-sizing:border-box; border:1px solid #d5dbe1; border-radius:9px; padding:0 10px; font:inherit; font-size:14px; background:#fff; color:#1b2430; }
      .bf2-campo select:focus, .bf2-busca input:focus { outline:none; border-color:#1b2430; }
      .bf2-campo.conta { flex:1 1 200px; min-width:180px; }
      .bf2-campo.conta select { width:100%; }
      .bf2-campo.datas { display:none; }
      .bf2-campo.datas.aberto { display:flex; }
      .bf2-campo.datas .linha { display:flex; gap:6px; align-items:center; font-size:12px; color:#5b6776; }
      .bf2-campo.datas input { width:140px; font-size:13px; }
      .bf2-btn { height:38px; padding:0 14px; border-radius:9px; border:1px solid #d5dbe1; background:#fff; color:#1b2430; font:inherit; font-size:13.5px; font-weight:500; display:inline-flex; align-items:center; gap:7px; cursor:pointer; white-space:nowrap; }
      .bf2-btn:hover { background:#f7f9fb; }
      .bf2-aplicar { background:#1b2430; border-color:#1b2430; color:#fff; font-weight:600; opacity:.55; transition:opacity .15s, box-shadow .15s; }
      .bf2-aplicar:hover { background:#2a3644; }
      .bf2-aplicar.pendente { opacity:1; box-shadow:0 0 0 3px rgba(43,175,242,.35); }
      .bf2-badge { background:#1b2430; color:#fff; border-radius:999px; font-size:11px; font-weight:600; padding:0 7px; line-height:18px; }
      .bf2-link { background:none; border:none; padding:0 4px; height:38px; font:inherit; font-size:13px; color:#5b6776; text-decoration:underline; cursor:pointer; }
      .bf2-dd { position:relative; }
      .bf2-pop { display:none; position:absolute; top:44px; right:0; z-index:30; background:#fff; border:1px solid #d5dbe1; border-radius:12px; box-shadow:0 12px 30px rgba(20,30,40,.14); width:260px; padding:14px 16px; }
      .bf2-pop.aberto { display:block; }
      .bf2-pop .bf2-campo { margin-bottom:12px; }
      .bf2-pop .bf2-campo select { width:100%; }
      .bf2-linha2 { display:flex; gap:10px; align-items:center; padding:10px 18px 8px; flex-wrap:wrap; }
      .bf2-busca { position:relative; width:340px; max-width:100%; }
      .bf2-busca svg { position:absolute; left:12px; top:11px; color:#7c8a98; }
      .bf2-busca input { width:100%; padding-left:36px; }
      .bf2-status { margin-left:auto; font-size:13px; color:#5b6776; display:flex; align-items:center; gap:8px; }
      .bf2-status.carregando { color:#1b2430; font-weight:600; }
      .bf2-giro { width:12px; height:12px; border-radius:50%; border:2px solid #c9d2db; border-top-color:#1b2430; display:none; animation:bf2-girar .7s linear infinite; }
      .bf2-status.carregando .bf2-giro { display:inline-block; }
      @keyframes bf2-girar { to { transform:rotate(360deg); } }
      .bf2-chips { display:flex; gap:8px; flex-wrap:wrap; align-items:center; padding:4px 18px 14px; min-height:28px; }
      .bf2-chip { display:inline-flex; align-items:center; gap:6px; background:#f1f3f5; border-radius:999px; padding:5px 8px 5px 12px; font-size:12.5px; color:#1b2430; }
      .bf2-chip.fixo { padding-right:12px; }
      .bf2-chip button { border:none; background:transparent; color:#5b6776; font-size:13px; padding:0 4px; cursor:pointer; }
      @media (prefers-reduced-motion: reduce) { .bf2-giro { animation:none; } }
      @media (max-width: 760px) {
        .bf2-topo, .bf2-form, .bf2-linha2, .bf2-chips { padding-left:10px; padding-right:10px; }
        .bf2-aba { padding:12px 7px 10px; font-size:13px; }
        .bf2-campo { flex:1 1 45%; }
        .bf2-campo select { width:100%; }
        .bf2-status { margin-left:0; width:100%; }
        .bf2-pop { right:auto; left:0; }
      }
    `;
    const el = document.createElement("style");
    el.id = "estilo-barra-filtros-v5";
    el.textContent = css;
    document.head.appendChild(el);
  }

  const escapar = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const opcoes = (lista, atual) => lista.map(([v, n]) => `<option value="${escapar(v)}"${String(atual) === String(v) ? " selected" : ""}>${escapar(n)}</option>`).join("");
  const dataCurta = (iso) => { if (!iso) return ""; const [a, m, d] = iso.split("-"); return `${d}/${m}/${a.slice(2)}`; };

  window.criarBarraFiltros = function ({ container, onChange, mostrarAtendimento = false, mostrarOrigem = false, ordem = "recentes", acaoDireita = null, titulo = "Filtros" }) {
    injetarEstilo();
    const ABAS = [["todos", "Todos"], ["atendimento", "Em atendimento"], ["meus", "Meus"]];

    // ---- Filtros APLICADOS: vêm do endereço da página (aceita links antigos) ----
    const url = new URLSearchParams(location.search);
    const aplicado = Object.assign({}, PADRAO);
    Object.keys(PADRAO).forEach((k) => { if (url.get(k) !== null) aplicado[k] = url.get(k); });
    if (url.get("status") === "confirmado" || url.get("status") === "pendente") aplicado.status = url.get("status");
    if (url.get("aba") === "aguardando") aplicado.status = "pendente";
    if (url.get("aba") === "confirmados") aplicado.status = "confirmado";
    if (!mostrarAtendimento || !ABAS.some((a) => a[0] === aplicado.aba)) aplicado.aba = "todos";
    if (!NOMES_STATUS[aplicado.status]) aplicado.status = "todos";
    if (aplicado.de || aplicado.ate) aplicado.dias = "personalizado";
    // ---- RASCUNHO: o que está escolhido no formulário (só vale ao aplicar) ----
    let rascunho = {};
    CAMPOS_FORM.forEach((k) => { rascunho[k] = aplicado[k]; });

    container.innerHTML = `
      <div class="bf2">
        <div class="bf2-topo${mostrarAtendimento ? "" : " sem-abas"}">
          ${mostrarAtendimento ? '<div class="bf2-abas" role="tablist" data-el="abas"></div>' : `<div class="bf2-titulo">${escapar(titulo)}</div>`}
          <div class="bf2-acoes" data-el="acoes"></div>
        </div>
        <div class="bf2-form">
          <div class="bf2-campo conta"><label for="bf2-conta">Conta</label><select id="bf2-conta" data-f="conta"></select></div>
          <div class="bf2-campo"><label for="bf2-status">Status</label>
            <select id="bf2-status" data-f="status">${opcoes([["todos", "Todos"], ["pendente", "Aguardando"], ["confirmado", "Confirmados"]], rascunho.status)}</select></div>
          <div class="bf2-campo"><label for="bf2-galpao">Galpão</label><select id="bf2-galpao" data-f="galpao"></select></div>
          <div class="bf2-campo"><label for="bf2-periodo">Período</label>
            <select id="bf2-periodo" data-f="dias">${opcoes([["1", "Hoje"], ["7", "Últimos 7 dias"], ["30", "Últimos 30 dias"], ["personalizado", "Escolher datas…"]], rascunho.dias)}</select></div>
          <div class="bf2-campo datas" data-el="datas"><label>De / até</label>
            <div class="linha"><input type="date" data-f="de" aria-label="De" /><span>até</span><input type="date" data-f="ate" aria-label="Até" /></div></div>
          <div class="bf2-dd">
            <button type="button" class="bf2-btn" data-el="btn-mais" aria-haspopup="true">Mais filtros<span class="bf2-badge" data-el="badge-mais" style="display:none"></span></button>
            <div class="bf2-pop" data-el="pop-mais">
              <div class="bf2-campo"><label for="bf2-plataforma">Plataforma</label><select id="bf2-plataforma" data-f="plataforma"></select></div>
              ${mostrarOrigem ? `<div class="bf2-campo"><label for="bf2-origem">Origem</label>
                <select id="bf2-origem" data-f="origem">${opcoes([["", "Todas"], ["seller", "Sellers"], ["logistica", "Galpão"], ["publico", "Link público"]], rascunho.origem)}</select></div>` : ""}
            </div>
          </div>
          <button type="button" class="bf2-btn bf2-aplicar" data-el="aplicar">Aplicar filtros</button>
          <button type="button" class="bf2-link" data-el="limpar">Limpar</button>
        </div>
        <div class="bf2-linha2">
          <div class="bf2-busca">${ICONE_BUSCA}<input type="search" data-el="busca" placeholder="Buscar nº da venda (Enter)" aria-label="Buscar nº da venda" autocomplete="off" /></div>
          <button type="button" class="bf2-btn" data-el="buscar">Buscar</button>
          <div class="bf2-status" data-el="status" role="status" aria-live="polite"><span class="bf2-giro"></span><span data-el="status-txt">Carregando…</span></div>
        </div>
        <div class="bf2-chips" data-el="chips"></div>
      </div>`;
    const el = (n) => container.querySelector(`[data-el="${n}"]`);
    const campo = (n) => container.querySelector(`[data-f="${n}"]`);
    if (acaoDireita) el("acoes").appendChild(acaoDireita);
    campo("de").value = rascunho.de;
    campo("ate").value = rascunho.ate;
    el("busca").value = aplicado.busca;

    let contadores = null;

    function preencherListas() {
      const contaAtual = rascunho.conta;
      const lista = CONTAS.slice();
      if (contaAtual && !lista.some((c) => c.toLowerCase() === contaAtual.toLowerCase())) lista.push(contaAtual);
      campo("conta").innerHTML = opcoes([["", "Todas as contas"]].concat(lista.map((c) => [c, c])), contaAtual);
      campo("galpao").innerHTML = opcoes([["", "Todos"]].concat(GALPOES), rascunho.galpao);
      campo("plataforma").innerHTML = opcoes([["", "Todas"]].concat(PLATAFORMAS), rascunho.plataforma);
    }

    function pendentes() { return CAMPOS_FORM.filter((k) => String(rascunho[k] || "") !== String(aplicado[k] || "")).length; }

    function pintarForm() {
      el("datas").classList.toggle("aberto", rascunho.dias === "personalizado");
      const n = pendentes();
      el("aplicar").classList.toggle("pendente", n > 0);
      el("aplicar").textContent = n ? `Aplicar filtros (${n})` : "Aplicar filtros";
      const nMais = (rascunho.plataforma ? 1 : 0) + (mostrarOrigem && rascunho.origem ? 1 : 0);
      el("badge-mais").style.display = nMais ? "inline-block" : "none";
      el("badge-mais").textContent = nMais;
    }

    function contagemAba(k) {
      if (!contadores) return "–";
      if (k === "atendimento") return contadores.em_atendimento;
      if (k === "meus") return contadores.meus;
      return { todos: contadores.total, pendente: contadores.pendentes, confirmado: contadores.confirmados }[aplicado.status];
    }
    function pintarAbas() {
      if (!mostrarAtendimento) return;
      el("abas").innerHTML = ABAS.map(([k, nome]) =>
        `<button type="button" role="tab" aria-selected="${aplicado.aba === k}" class="bf2-aba${aplicado.aba === k ? " on" : ""}" data-aba="${k}">${nome}<span class="bf2-n">${contagemAba(k)}</span></button>`).join("");
    }

    function pintarChips() {
      const c = [];
      const chip = (texto, remover) => c.push(remover
        ? `<span class="bf2-chip">${escapar(texto)}<button type="button" data-rem="${remover}" aria-label="Remover filtro">✕</button></span>`
        : `<span class="bf2-chip fixo">${escapar(texto)}</span>`);
      chip(aplicado.conta || "Todas as contas", aplicado.conta ? "conta" : null);
      if (aplicado.status !== "todos") chip(NOMES_STATUS[aplicado.status], "status");
      if (aplicado.galpao) chip((GALPOES.find(([v]) => String(v) === String(aplicado.galpao)) || [0, "Galpão " + aplicado.galpao])[1], "galpao");
      if (aplicado.busca) chip(`Venda: ${aplicado.busca} (todo o histórico)`, "busca");
      else chip(aplicado.dias === "personalizado" ? `${dataCurta(aplicado.de) || "…"} a ${dataCurta(aplicado.ate) || "hoje"}` : NOMES_PERIODO[aplicado.dias] || "Últimos 30 dias", aplicado.dias !== "30" ? "periodo" : null);
      if (aplicado.plataforma) chip((PLATAFORMAS.find(([v]) => v === aplicado.plataforma) || [0, aplicado.plataforma])[1], "plataforma");
      if (mostrarOrigem && aplicado.origem) chip(NOMES_ORIGEM[aplicado.origem] || aplicado.origem, "origem");
      el("chips").innerHTML = c.join("");
    }

    function gravarUrl() {
      const p = new URLSearchParams();
      Object.keys(PADRAO).forEach((k) => { if (aplicado[k] && aplicado[k] !== PADRAO[k]) p.set(k, aplicado[k]); });
      if (aplicado.dias !== "personalizado") { p.delete("de"); p.delete("ate"); }
      history.replaceState(null, "", location.pathname + (p.toString() ? "?" + p : ""));
    }

    function marcarAtualizando() {
      el("status").classList.add("carregando");
      el("status-txt").textContent = "Atualizando…";
    }

    // Aplica: copia o rascunho pros filtros valendo e recarrega.
    function aplicarAgora() {
      CAMPOS_FORM.forEach((k) => { aplicado[k] = rascunho[k]; });
      if (aplicado.dias !== "personalizado") { aplicado.de = ""; aplicado.ate = ""; rascunho.de = ""; rascunho.ate = ""; }
      disparar();
    }
    function disparar() {
      el("pop-mais").classList.remove("aberto");
      gravarUrl(); pintarForm(); pintarAbas(); pintarChips();
      marcarAtualizando();
      onChange && onChange();
    }

    // ---- Eventos ----
    container.addEventListener("change", (e) => {
      const f = e.target.dataset && e.target.dataset.f;
      if (!f) return;
      rascunho[f] = e.target.value;
      if (f === "de" || f === "ate") rascunho.dias = "personalizado", campo("dias").value = "personalizado";
      pintarForm();
    });
    container.addEventListener("click", (e) => {
      const alvo = e.target.closest("button");
      if (!alvo || !container.contains(alvo)) return;
      if (alvo.dataset.aba) { aplicado.aba = alvo.dataset.aba; return disparar(); }
      if (alvo.dataset.rem) {
        const r = alvo.dataset.rem;
        if (r === "periodo") { aplicado.dias = "30"; aplicado.de = ""; aplicado.ate = ""; rascunho.dias = "30"; rascunho.de = ""; rascunho.ate = ""; campo("dias").value = "30"; }
        else if (r === "busca") { aplicado.busca = ""; el("busca").value = ""; }
        else { aplicado[r] = PADRAO[r]; rascunho[r] = PADRAO[r]; const c = campo(r); if (c) c.value = PADRAO[r]; }
        return disparar();
      }
    });
    el("aplicar").addEventListener("click", aplicarAgora);
    el("limpar").addEventListener("click", () => {
      CAMPOS_FORM.forEach((k) => { rascunho[k] = PADRAO[k]; aplicado[k] = PADRAO[k]; });
      aplicado.busca = ""; el("busca").value = "";
      preencherListas();
      campo("status").value = "todos"; campo("dias").value = "30"; campo("de").value = ""; campo("ate").value = "";
      if (mostrarOrigem) campo("origem").value = "";
      disparar();
    });
    el("btn-mais").addEventListener("click", (e) => { e.stopPropagation(); el("pop-mais").classList.toggle("aberto"); });
    // Clique fora fecha o "Mais filtros" (composedPath: funciona mesmo se algo for redesenhado).
    document.addEventListener("click", (e) => { if (!e.composedPath().includes(el("pop-mais")) && !e.composedPath().includes(el("btn-mais"))) el("pop-mais").classList.remove("aberto"); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") el("pop-mais").classList.remove("aberto"); });
    const buscar = () => { aplicado.busca = el("busca").value.trim(); disparar(); };
    el("buscar").addEventListener("click", buscar);
    el("busca").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); buscar(); } });
    el("busca").addEventListener("search", () => { if (!el("busca").value && aplicado.busca) buscar(); }); // o "x" do campo

    // Listas oficiais do servidor (se falhar, ficam as padrão).
    fetch("/api/solicitacoes-cancelamento/opcoes", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : null))
      .then((op) => {
        if (!op) return;
        if (Array.isArray(op.galpoes) && op.galpoes.length) GALPOES = op.galpoes.map((g) => [g.valor, g.nome]);
        if (Array.isArray(op.plataformas) && op.plataformas.length) PLATAFORMAS = op.plataformas.map((p) => [p.valor, p.nome]);
        preencherListas(); pintarChips();
      }).catch((erro) => console.error("Não consegui carregar as opções", erro));
    fetch("/api/solicitacoes-cancelamento/contas", { cache: "no-store" })
      .then((r) => (r.ok ? r.json() : []))
      .then((lista) => { CONTAS = (lista || []).map((c) => c.nome).filter(Boolean).sort((a, b) => a.localeCompare(b, "pt-BR")); preencherListas(); })
      .catch((erro) => console.error("Não consegui carregar as contas", erro));

    preencherListas();
    gravarUrl(); pintarForm(); pintarAbas(); pintarChips(); marcarAtualizando();

    function params() {
      const p = { ordem };
      let status = aplicado.status;
      if (aplicado.aba === "atendimento") { status = "pendente"; p.atendimento = "em_atendimento"; }
      if (aplicado.aba === "meus") { status = "pendente"; p.atendimento = "meus"; }
      p.status = status;
      if (aplicado.conta) p.conta = aplicado.conta;
      if (aplicado.galpao) p.galpao = aplicado.galpao;
      if (aplicado.plataforma) p.plataforma = aplicado.plataforma;
      if (mostrarOrigem && aplicado.origem) p.origem = aplicado.origem;
      if (aplicado.busca) p.busca = aplicado.busca;
      if (aplicado.dias === "personalizado") {
        if (aplicado.de) p.de = aplicado.de;
        if (aplicado.ate) p.ate = aplicado.ate;
        if (!aplicado.de && !aplicado.ate) p.dias = "30";
      } else {
        p.dias = aplicado.dias;
      }
      return p;
    }

    return {
      params,
      estado: () => Object.assign({}, aplicado),
      // Chamado a cada carga (inclusive a automática): atualiza os números e o "atualizado às".
      atualizarContadores(c, atendimento) {
        contadores = {
          total: c.total, pendentes: c.pendentes, confirmados: c.confirmados,
          em_atendimento: atendimento ? atendimento.em_atendimento : 0,
          meus: atendimento ? atendimento.meus : 0,
        };
        pintarAbas();
        const n = aplicado.aba === "atendimento" ? contadores.em_atendimento : aplicado.aba === "meus" ? contadores.meus : contagemAba("todos");
        el("status").classList.remove("carregando");
        el("status-txt").textContent = `${n} pedido(s) · atualizado às ${new Date().toLocaleTimeString("pt-BR", { timeZone: "America/Sao_Paulo" })}`;
      },
      falhou() {
        el("status").classList.remove("carregando");
        el("status-txt").textContent = "Não foi possível atualizar agora";
      },
      atualizarResumo() {}, // compatibilidade
    };
  };
})();
