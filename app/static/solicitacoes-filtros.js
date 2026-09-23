/**
 * Barra de filtros das Solicitações de Cancelamento.
 *
 * Um componente só, usado em duas telas (Solicitações de Cancelamento e
 * Solicitações do galpão), pra quem usa as duas não precisar aprender
 * duas barras diferentes.
 *
 * Boas práticas aplicadas:
 *  - Escolha única (status, galpão, período) = botões segmentados.
 *  - Padrão ao abrir: "Aguardando" + "30 dias".
 *  - Busca geral (nº da venda/conta) procura em TODO o histórico.
 *  - Filtros ficam na URL: recarregar não perde, e dá pra mandar o link.
 *  - A filtragem de verdade é feita no servidor (/busca), com paginação.
 *
 * Uso:
 *   const barra = criarBarraFiltros({
 *     container: document.getElementById("barra-filtros"),
 *     mostrarOrigem: true,               // tela interna: filtra por origem
 *     onChange: (params) => carregar(params),
 *   });
 *   barra.atualizarContadores({ total, pendentes, confirmados });
 *   barra.atualizarResumo(buscandoTodoHistorico);
 */
(function () {
  const PADRAO = { status: "pendente", galpao: "", dias: "30", de: "", ate: "", conta: "", plataforma: "", origem: "", busca: "" };
  const NOMES_STATUS = { pendente: "Aguardando", confirmado: "Confirmados", todos: "Todos" };
  const NOMES_PERIODO = { "1": "Hoje", "7": "7 dias", "30": "30 dias", personalizado: "Personalizado" };
  const NOMES_PLATAFORMA = { mercado_livre: "Mercado Livre", shopee: "Shopee" };
  const NOMES_ORIGEM = { seller: "Sellers", logistica: "Galpão", publico: "Link público" };

  function injetarEstilo() {
    if (document.getElementById("estilo-barra-filtros")) return;
    const css = `
      .bf { background:#fff; border:1px solid #e2e6ea; border-radius:12px; padding:12px 14px; margin-bottom:16px; display:flex; flex-direction:column; gap:10px; }
      .bf-linha { display:flex; gap:10px 14px; flex-wrap:wrap; align-items:center; }
      .bf-rotulo { font-size:12px; color:#6b7684; font-weight:600; }
      .bf-grupo { display:inline-flex; align-items:center; gap:8px; flex-wrap:nowrap; }
      .bf-seg { display:inline-flex; border:1px solid #d5dbe1; border-radius:9px; overflow:hidden; }
      .bf-seg button { border:none; background:#fff; padding:7px 12px; font-size:12.5px; font-weight:600; color:#4a5663; cursor:pointer; font-family:inherit; }
      .bf-seg button + button { border-left:1px solid #d5dbe1; }
      .bf-seg button.on { background:#1b2430; color:#fff; }
      .bf-seg .bf-cont { display:inline-block; min-width:18px; margin-left:5px; padding:0 5px; border-radius:999px; background:#eef1f4; color:#4a5663; font-size:11px; }
      .bf-seg button.on .bf-cont { background:rgba(255,255,255,.2); color:#fff; }
      .bf input, .bf select { height:34px; border:1px solid #d5dbe1; border-radius:8px; padding:0 10px; font-size:13px; font-family:inherit; background:#fff; color:#1b2430; }
      .bf input[type="date"] { padding:0 6px; }
      .bf-busca { flex:1; min-width:200px; }
      .bf-limpar { height:34px; border:1px solid #d5dbe1; border-radius:8px; background:#fff; padding:0 12px; font-size:12.5px; font-weight:600; cursor:pointer; font-family:inherit; }
      .bf-resumo { font-size:12px; color:#6b7684; }
      .bf-resumo b { color:#1b2430; }
      .bf-personalizado { display:none; gap:6px; align-items:center; }
      .bf-personalizado.aberto { display:inline-flex; }
    `;
    const tag = document.createElement("style");
    tag.id = "estilo-barra-filtros";
    tag.textContent = css;
    document.head.appendChild(tag);
  }

  function escapar(t) {
    return String(t ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function chaveConta(nome) {
    return String(nome || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").replace(/\s+/g, " ").trim().toLowerCase();
  }

  function lerDaUrl() {
    const url = new URLSearchParams(window.location.search);
    const estado = { ...PADRAO };
    Object.keys(PADRAO).forEach(k => { if (url.has(k)) estado[k] = url.get(k); });
    if (estado.de || estado.ate) estado.dias = "personalizado";
    return estado;
  }

  function gravarNaUrl(estado) {
    const url = new URLSearchParams();
    Object.keys(PADRAO).forEach(k => {
      if (k === "dias" && estado.dias === "personalizado") return;
      if (estado[k] !== "" && estado[k] !== PADRAO[k]) url.set(k, estado[k]);
    });
    const qs = url.toString();
    history.replaceState(null, "", window.location.pathname + (qs ? "?" + qs : ""));
  }

  window.criarBarraFiltros = function ({ container, mostrarOrigem = false, onChange }) {
    injetarEstilo();
    let estado = lerDaUrl();
    let contas = []; // [{chave, nome}]

    const seg = (nome, opcoes) =>
      `<span class="bf-seg" data-seg="${nome}">${opcoes.map(([v, t]) => `<button type="button" data-v="${v}">${t}</button>`).join("")}</span>`;

    container.innerHTML = `
      <div class="bf">
        <div class="bf-linha">
          <span class="bf-grupo"><span class="bf-rotulo">Status</span>
          ${seg("status", [["pendente", 'Aguardando<span class="bf-cont" data-cont="pendentes">–</span>'], ["confirmado", 'Confirmados<span class="bf-cont" data-cont="confirmados">–</span>'], ["todos", 'Todos<span class="bf-cont" data-cont="total">–</span>']])}</span>
          <span class="bf-grupo"><span class="bf-rotulo">Galpão</span>
          ${seg("galpao", [["", "Todos"], ["1", "Galpão 1"], ["2", "Galpão 2"]])}</span>
          <span class="bf-grupo"><span class="bf-rotulo">Período</span>
          ${seg("dias", [["1", "Hoje"], ["7", "7 dias"], ["30", "30 dias"], ["personalizado", "Personalizado"]])}</span>
          <span class="bf-personalizado" data-el="personalizado">
            <input type="date" data-el="de" /> <span class="bf-rotulo">até</span> <input type="date" data-el="ate" />
          </span>
        </div>
        <div class="bf-linha">
          <input type="text" list="bf-lista-contas" data-el="conta" placeholder="Todas as contas (digite para buscar)" autocomplete="off" style="min-width:220px;" />
          <datalist id="bf-lista-contas"></datalist>
          <select data-el="plataforma">
            <option value="">Todas as plataformas</option>
            <option value="mercado_livre">Mercado Livre</option>
            <option value="shopee">Shopee</option>
          </select>
          ${mostrarOrigem ? `<select data-el="origem">
            <option value="">Todas as origens</option>
            <option value="seller">Sellers</option>
            <option value="logistica">Galpão</option>
            <option value="publico">Link público</option>
          </select>` : ""}
          <input type="text" class="bf-busca" data-el="busca" placeholder="Buscar nº da venda em todo o histórico..." autocomplete="off" />
          <button type="button" class="bf-limpar" data-el="limpar">Limpar filtros</button>
        </div>
        <div class="bf-resumo" data-el="resumo"></div>
      </div>`;

    const el = nome => container.querySelector(`[data-el="${nome}"]`);

    function nomeDaConta(chave) {
      const c = contas.find(x => x.chave === chave);
      return c ? c.nome : chave;
    }

    function pintar() {
      container.querySelectorAll("[data-seg]").forEach(grupo => {
        const campo = grupo.dataset.seg;
        grupo.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.v === estado[campo]));
      });
      el("personalizado").classList.toggle("aberto", estado.dias === "personalizado");
      el("de").value = estado.de;
      el("ate").value = estado.ate;
      el("conta").value = estado.conta ? nomeDaConta(estado.conta) : "";
      el("plataforma").value = estado.plataforma;
      if (el("origem")) el("origem").value = estado.origem;
      el("busca").value = estado.busca;
    }

    function params() {
      const p = { status: estado.status, pagina: 1 };
      if (estado.galpao) p.galpao = estado.galpao;
      if (estado.conta) p.conta = estado.conta;
      if (estado.plataforma) p.plataforma = estado.plataforma;
      if (estado.origem) p.origem = estado.origem;
      if (estado.busca) p.busca = estado.busca;
      if (estado.dias === "personalizado") {
        if (estado.de) p.de = estado.de;
        if (estado.ate) p.ate = estado.ate;
        if (!estado.de && !estado.ate) p.dias = 0; // personalizado sem datas = sem limite
      } else {
        p.dias = estado.dias;
      }
      return p;
    }

    function mudou() {
      gravarNaUrl(estado);
      pintar();
      onChange(params());
    }

    // Segmentados
    container.querySelectorAll("[data-seg] button").forEach(b => b.addEventListener("click", () => {
      const campo = b.closest("[data-seg]").dataset.seg;
      estado[campo] = b.dataset.v;
      if (campo === "dias" && b.dataset.v !== "personalizado") { estado.de = ""; estado.ate = ""; }
      mudou();
    }));
    el("de").addEventListener("change", e => { estado.de = e.target.value; mudou(); });
    el("ate").addEventListener("change", e => { estado.ate = e.target.value; mudou(); });

    // Conta: aceita escolher da lista ou digitar -- sempre vira chave
    el("conta").addEventListener("change", e => { estado.conta = chaveConta(e.target.value); mudou(); });
    el("plataforma").addEventListener("change", e => { estado.plataforma = e.target.value; mudou(); });
    if (el("origem")) el("origem").addEventListener("change", e => { estado.origem = e.target.value; mudou(); });

    // Busca com pequena espera, pra não consultar a cada tecla
    let espera = null;
    el("busca").addEventListener("input", e => {
      clearTimeout(espera);
      espera = setTimeout(() => {
        estado.busca = e.target.value.trim();
        // Quem busca um nº de venda quer achá-la em qualquer status: troca
        // "Aguardando" por "Todos" (visível nos botões, dá pra voltar).
        if (estado.busca && estado.status === "pendente") estado.status = "todos";
        mudou();
      }, 350);
    });

    el("limpar").addEventListener("click", () => { estado = { ...PADRAO }; mudou(); });

    // Lista de contas pro filtro (mesma do formulário)
    fetch("/api/solicitacoes-cancelamento/contas", { cache: "no-store" })
      .then(r => (r.ok ? r.json() : []))
      .then(lista => {
        contas = lista;
        container.querySelector("#bf-lista-contas").innerHTML = lista.map(c => `<option value="${escapar(c.nome)}"></option>`).join("");
        pintar();
      })
      .catch(err => console.error("Não consegui carregar a lista de contas", err));

    pintar();

    return {
      params,
      atualizarContadores({ total, pendentes, confirmados }) {
        const setar = (k, v) => { const x = container.querySelector(`[data-cont="${k}"]`); if (x) x.textContent = v; };
        setar("total", total); setar("pendentes", pendentes); setar("confirmados", confirmados);
      },
      atualizarResumo(buscandoTodoHistorico) {
        const partes = [];
        if (buscandoTodoHistorico) partes.push("<b>Buscando em todo o histórico</b>");
        partes.push(NOMES_STATUS[estado.status]);
        if (estado.galpao) partes.push("Galpão " + estado.galpao);
        if (!buscandoTodoHistorico) {
          if (estado.dias === "personalizado") {
            const f = d => d ? d.split("-").reverse().join("/") : "…";
            partes.push(estado.de || estado.ate ? `${f(estado.de)} até ${f(estado.ate)}` : "Todo o período");
          } else partes.push(NOMES_PERIODO[estado.dias]);
        }
        if (estado.conta) partes.push(escapar(nomeDaConta(estado.conta)));
        if (estado.plataforma) partes.push(NOMES_PLATAFORMA[estado.plataforma]);
        if (estado.origem) partes.push(NOMES_ORIGEM[estado.origem]);
        el("resumo").innerHTML = "Filtros: " + partes.join(" · ");
      },
    };
  };
})();
