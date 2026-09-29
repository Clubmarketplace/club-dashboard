/**
 * Tela "Reputação das contas": termômetro do Mercado Livre de todas as contas.
 *
 * - Rosca clicável no topo: clicar numa cor filtra as contas daquela situação.
 * - Cartões dos 4 indicadores: clicar filtra as contas em atenção naquele indicador.
 * - Busca por nome + chips de situação.
 * - "Precisam de atenção" sempre aberto; "Em dia" e "Sem leitura" recolhidos.
 * - Gaveta de detalhe ao clicar numa conta.
 * - "Atualizar agora" (admin/supervisor) dispara a leitura e acompanha o progresso.
 * - ATENDIMENTO (mesmo padrão das Solicitações sellers): cada conta em atenção
 *   mostra no rodapé do cartão quem está com ela ("Com Davi · há 2h10"),
 *   com Assumir / Concluir / Liberar. A gaveta mostra tempos, histórico e
 *   anotações. Regras no servidor: app/reputacao_atendimento.py.
 * - Supervisão (admin/supervisor): quadro "Equipe agora" + filtros de
 *   atendimento. O operador vê só os cartões, como antes, com a etiqueta.
 *
 * Os dados vêm de GET /api/reputacao/contas; a classificação (situação) é
 * feita no servidor (app/reputacao.py), aqui só desenhamos.
 */
(function () {
  "use strict";

  const SITUACOES = [
    { chave: "critico", nome: "Passaram do limite", centro: "passaram do limite", cor: "#dc2626" },
    { chave: "atencao", nome: "A verificar (≥ 80% do limite)", centro: "a verificar", cor: "#f08a24" },
    { chave: "sem", nome: "Sem medalha", centro: "sem medalha", cor: "#9aa4af" },
    { chave: "ok", nome: "Em dia", centro: "em dia", cor: "#1f9d55" },
    { chave: "sem_dados", nome: "Sem leitura", centro: "sem leitura", cor: "#cbd5df" },
  ];
  const CORES_TERMO = ["#f3a3a3", "#f8c79a", "#f6e07a", "#bfe3a0", "#1f9d55"];

  const estado = {
    dados: null,
    situacao: "todas",
    indicador: null, // chave do indicador filtrado (ou null)
    busca: "",
    mostrarOk: false,
    mostrarSemDados: false,
    detalhe: null, // id da conta aberta na gaveta
    detalheAtd: null, // atendimento carregado da conta aberta (histórico, tempos)
    formulario: null, // "anotar" | "concluir" (formulário aberto na gaveta)
    atd: "todos", // filtro de atendimento (supervisão): todos | livres | em_atendimento | meus | paradas
    operador: "", // filtro por operador (id), supervisão
    ocupado: false, // uma ação em andamento (evita clique duplo)
  };

  // ---------------------------------------------------------------- utilidades
  function esc(texto) {
    return String(texto ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function dataBr(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "short", timeZone: "America/Sao_Paulo" });
  }

  function pct(valor) {
    if (valor === null || valor === undefined) return "—";
    if (valor === 0) return "0%";
    return valor.toFixed(2).replace(".", ",").replace(/0$/, "") + "%";
  }

  // "agora", "há 25 min", "há 2h10", "há 3 dias"
  function haQuanto(iso) {
    if (!iso) return "";
    const min = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 60000));
    if (min < 1) return "agora";
    if (min < 60) return `há ${min} min`;
    if (min < 60 * 24) return `há ${Math.floor(min / 60)}h${String(min % 60).padStart(2, "0")}`;
    return `há ${Math.floor(min / 1440)} dia(s)`;
  }

  function periodoBr(p) {
    if (!p) return "";
    return String(p).replace("days", "dias").replace("months", "meses");
  }

  function corUso(uso) {
    if (uso === null || uso === undefined) return "#cbd5df";
    return uso >= 1 ? "#dc2626" : uso >= estado.dados.faixa_atencao ? "#f08a24" : "#1f9d55";
  }

  function piorUso(conta) {
    return Math.max(0, ...conta.metricas.map((m) => m.uso ?? 0));
  }

  function usoDe(conta, chave) {
    const m = conta.metricas.find((x) => x.chave === chave);
    return m && m.uso !== null ? m.uso : null;
  }

  function aviso(texto, erro) {
    const el = document.getElementById("rp-aviso");
    if (!texto) { el.style.display = "none"; return; }
    el.textContent = texto;
    el.style.display = "block";
    el.style.background = erro ? "#fbe9e3" : "#fdf3e3";
    el.style.color = erro ? "#96341c" : "#8a4200";
  }

  // ---------------------------------------------------------------- dados
  async function carregar() {
    const r = await fetch("/api/reputacao/contas");
    if (!r.ok) {
      const d = await r.json().catch(() => null);
      throw new Error((d && d.detail) || `status ${r.status}`);
    }
    estado.dados = await r.json();
    desenhar();
    configurarBotaoAtualizar();
  }

  // ---------------------------------------------------------------- desenho
  function termometro(nivel, alto) {
    return `<div class="rp-termo" title="Termômetro do Mercado Livre">${CORES_TERMO.map((cor, i) =>
      `<span style="height:${alto ? 9 : 6}px;background:${cor};opacity:${nivel === i + 1 ? 1 : 0.28}"></span>`).join("")}</div>`;
  }

  function medalhaHtml(conta) {
    const tem = !!conta.medalha;
    return `<div class="rp-medalha" style="color:${tem ? "#1e7a47" : "#6b7684"}">${esc(conta.medalha || "Sem medalha")}</div>`;
  }

  function tagDe(conta) {
    switch (conta.situacao) {
      case "critico": return ["PASSOU DO LIMITE", "background:#fde8e6;color:#b3261e"];
      case "atencao": return ["A VERIFICAR", "background:#fff0e0;color:#b45309"];
      case "sem_dados": return ["SEM LEITURA", "background:#eef1f4;color:#4a5663"];
      default: return ["EM DIA", "background:#e3f5ea;color:#1a6641"];
    }
  }

  function motivo(conta) {
    const faixa = estado.dados.faixa_atencao;
    const piores = conta.metricas.filter((m) => m.uso !== null && m.uso >= faixa).sort((a, b) => b.uso - a.uso);
    const partes = piores.map((m) => `${m.nome} ${pct(m.taxa)} de ${pct(m.limite)}`);
    if (!partes.length && conta.nivel && conta.nivel <= 4) partes.push("Termômetro abaixo do verde");
    return partes.join(" · ");
  }

  // ---------------------------------------------------------------- atendimento
  const eu = () => (estado.dados.eu || {});
  const ehMeu = (a) => a && a.em_atendimento_por_id && a.em_atendimento_por_id === eu().id;
  const naFila = (a) => a && a.status === "aberto";

  function linkSolicitacoes(conta) {
    const n = conta.solicitacoes_reputacao || 0;
    if (!n) return "";
    const url = `/solicitacoes-painel?tipo=reputacao&status=pendente&conta=${encodeURIComponent(conta.nome)}`;
    return `<a class="rp-solic" href="${url}" data-parar>${n} solicitaç${n > 1 ? "ões" : "ão"} de reputação do seller →</a>`;
  }

  // Etiqueta de quem está com a conta (usada no cartão e na gaveta).
  function etiquetaAtd(a) {
    if (!a) return "";
    if (a.status === "concluido") return `<span class="rp-pill rp-p-verde">✓ Tratada por ${esc(a.concluido_por || "—")}</span>`;
    if (!a.em_atendimento_por_id) return `<span class="rp-pill rp-p-livre">Livre</span>`;
    if (ehMeu(a)) return `<span class="rp-pill rp-p-meu">Com você · ${haQuanto(a.em_atendimento_desde)}</span>`;
    return `<span class="rp-pill ${a.parada ? "rp-p-verm" : "rp-p-azul"}">Com ${esc(a.em_atendimento_por)} · ${haQuanto(a.em_atendimento_desde)}</span>`;
  }

  function botoesAtd(conta, grande) {
    const a = conta.atendimento;
    if (!naFila(a)) return "";
    const cls = grande ? "rp-btn" : "rp-btn rp-btn-p";
    if (ehMeu(a)) return `<button class="${cls} rp-btn-escuro" data-acao="concluir" data-id="${conta.id}">Concluir</button>
      <button class="${cls}" data-acao="liberar" data-id="${conta.id}">Liberar</button>`;
    if (a.em_atendimento_por_id) return `<button class="${cls}" data-acao="assumir-lugar" data-id="${conta.id}">${grande ? "Assumir no lugar de " + esc(a.em_atendimento_por) : "Assumir"}</button>`;
    return `<button class="${cls} rp-btn-escuro" data-acao="assumir" data-id="${conta.id}">Assumir</button>`;
  }

  function rodapeAtd(conta) {
    const a = conta.atendimento;
    const solic = linkSolicitacoes(conta);
    if (!a && !solic) return "";
    let sub = "";
    if (a && a.status === "concluido") sub = `${dataBr(a.concluido_em)} · volta para a fila se continuar acima do limite`;
    else if (a && !a.em_atendimento_por_id) sub = `${a.livre_ha_muito ? "⚠ " : ""}na fila ${haQuanto(a.aberto_em)}`;
    else if (a && a.parada) sub = `⚠ parada há mais de ${estado.dados.horas_parada}h`;
    const alerta = a && (a.parada || (a.livre_ha_muito && !a.em_atendimento_por_id));
    return `<div class="rp-atd">
      ${a ? `<div class="rp-atd-linha">
        <div class="rp-atd-quem">${etiquetaAtd(a)}${sub ? `<small class="${alerta ? "rp-alerta" : ""}">${esc(sub)}</small>` : ""}</div>
        <div class="rp-atd-btns">${botoesAtd(conta, false)}</div>
      </div>` : ""}
      ${solic}
    </div>`;
  }

  function cardHtml(conta) {
    const [tag, tagEstilo] = tagDe(conta);
    const destaque = conta.situacao === "critico" || conta.situacao === "atencao";
    const borda = destaque ? `border-top:4px solid ${conta.situacao === "critico" ? "#dc2626" : "#f08a24"};` : "";
    const faixa = estado.dados.faixa_atencao;
    const mets = conta.metricas.map((m) => {
      const forte = m.uso !== null && m.uso >= faixa;
      const cor = m.uso >= 1 ? "#b3261e" : "#b45309";
      return `<div class="rp-met"><span class="rp-dot" style="background:${corUso(m.uso)};width:8px;height:8px"></span>` +
        `<span style="${forte ? `font-weight:700;color:${cor}` : ""}">${pct(m.taxa)}</span><small>${esc(m.curto)}</small></div>`;
    }).join("");
    const mot = destaque ? motivo(conta) : "";
    const antiga = conta.erro && conta.lido_em
      ? `<div style="font-size:11.5px;color:#96341c">Leitura de ${dataBr(conta.lido_em)} (a última falhou)</div>` : "";
    // <div> (e não <button>): o cartão tem botões dentro (Assumir, Concluir...).
    return `<div class="rp-card" role="button" tabindex="0" style="${borda}" data-conta="${conta.id}">
      <div class="rp-card-topo"><span class="rp-tag" style="${tagEstilo}">${tag}</span>
        <span>${conta.vendas !== null && conta.vendas !== undefined ? conta.vendas + " vendas" : ""}</span></div>
      <div><div class="rp-nome">${esc(conta.nome)}</div>${medalhaHtml(conta)}</div>
      ${termometro(conta.nivel, false)}
      <div class="rp-mets">${mets}</div>
      ${mot ? `<div class="rp-motivo">${esc(mot)}</div>` : ""}
      ${antiga}
      ${rodapeAtd(conta)}
    </div>`;
  }

  function roscaHtml(contagem, total) {
    const raio = 60;
    const circ = 2 * Math.PI * raio;
    let giro = 0;
    const fatias = SITUACOES.map((s) => {
      const n = contagem[s.chave] || 0;
      if (!n || !total) return "";
      const tam = (n / total) * circ;
      const traco = Math.max(0, tam - (n === total ? 0 : 2));
      const on = estado.situacao === s.chave;
      const opac = estado.situacao === "todas" || on ? 1 : 0.4;
      const html = `<circle class="rp-fatia" data-situacao="${s.chave}" cx="80" cy="80" r="${raio}" fill="none"
        stroke="${s.cor}" stroke-width="${on ? 34 : 26}" stroke-dasharray="${traco} ${circ - traco}"
        stroke-dashoffset="${-giro}" transform="rotate(-90 80 80)" opacity="${opac}"><title>${esc(s.nome)}: ${n}</title></circle>`;
      giro += tam;
      return html;
    }).join("");

    let centroN = total;
    let centroTxt = "contas";
    if (estado.situacao !== "todas") {
      const s = SITUACOES.find((x) => x.chave === estado.situacao);
      centroN = contagem[estado.situacao] || 0;
      centroTxt = s ? s.centro : "contas";
    }
    const legenda = SITUACOES.map((s) => `
      <button class="rp-leg ${estado.situacao === s.chave ? "ativo" : ""}" data-situacao="${s.chave}">
        <span class="rp-dot" style="background:${s.cor};width:12px;height:12px"></span>
        <span class="nome">${esc(s.nome)}</span><b style="font-size:15px">${contagem[s.chave] || 0}</b>
      </button>`).join("");

    return `<div class="rp-painel rp-rosca">
      <div class="rp-rosca-svg">
        <svg width="160" height="160" viewBox="0 0 160 160" role="img" aria-label="Situação das contas; clique numa cor para filtrar">
          <circle cx="80" cy="80" r="${raio}" fill="none" stroke="#eef1f4" stroke-width="26"></circle>${fatias}
        </svg>
        <div class="rp-rosca-centro"><b>${centroN}</b><span>${esc(centroTxt)}</span></div>
      </div>
      <div class="rp-legenda">${legenda}</div>
    </div>`;
  }

  function indicadoresHtml(contas) {
    const faixa = estado.dados.faixa_atencao;
    return `<div class="rp-inds">${estado.dados.limites.map((ind) => {
      const usos = contas.map((c) => usoDe(c, ind.chave)).filter((u) => u !== null);
      const atencao = usos.filter((u) => u >= faixa && u < 1).length;
      const passaram = usos.filter((u) => u >= 1).length;
      return `<button class="rp-ind ${estado.indicador === ind.chave ? "ativo" : ""}" data-indicador="${ind.chave}">
        <div class="n">${esc(ind.nome)}</div><div class="l">limite ${pct(ind.limite)}</div>
        <div class="nums">
          <div><b style="color:#c2410c">${atencao}</b><small>em atenção</small></div>
          <div><b style="color:#b3261e">${passaram}</b><small>passaram</small></div>
        </div>
      </button>`;
    }).join("")}</div>`;
  }

  // Quadro "Equipe agora" (só vem do servidor pra admin/supervisor).
  function equipeHtml() {
    const e = estado.dados.equipe;
    if (!e) return "";
    const kpi = (n, rotulo, filtro, alerta, cor) =>
      `<button class="rp-kpi ${alerta && n ? "alerta" : ""} ${estado.atd === filtro ? "ativo" : ""}" data-atd="${filtro}">
        <b style="${cor ? "color:" + cor : ""}">${n}</b><span>${rotulo}</span></button>`;
    const linhas = e.operadores.map((o) => {
      const carga = o.contas.length + o.solicitacoes;
      const antiga = o.mais_antiga
        ? `<span class="rp-pill ${o.mais_antiga.parada ? "rp-p-verm" : "rp-p-cinza"}">${o.mais_antiga.parada ? "⚠ " : ""}${haQuanto(o.mais_antiga.desde)} · ${esc(o.mais_antiga.descricao)}</span>`
        : `<span class="rp-pill rp-p-verde">livre para assumir</span>`;
      const tratadas = [o.tratadas_hoje_contas ? `${o.tratadas_hoje_contas} conta(s)` : "", o.tratadas_hoje_solicitacoes ? `${o.tratadas_hoje_solicitacoes} solicitação(ões)` : ""].filter(Boolean).join(" · ") || "0";
      return `<tr class="${carga ? "clic" : ""}" ${o.contas.length ? `data-operador="${o.id}"` : ""}>
        <td><div class="rp-op"><span class="rp-av">${esc((o.nome || "?")[0].toUpperCase())}</span>${esc(o.nome)}</div></td>
        <td><b>${o.contas.length || "—"}</b></td>
        <td><b>${o.solicitacoes || "—"}</b>${o.solicitacoes_reputacao ? ` <span class="rp-pill rp-p-roxo">${o.solicitacoes_reputacao} reputação</span>` : ""}
          ${o.solicitacoes ? ` <a class="rp-mini" href="/solicitacoes-painel?aba=atendimento" data-parar>ver</a>` : ""}</td>
        <td>${antiga}</td>
        <td class="rp-contas-op">${o.contas.length ? esc(o.contas.join(", ")) : "—"}</td>
        <td>${tratadas}</td>
      </tr>`;
    }).join("");
    return `<section class="rp-equipe">
      <div class="rp-equipe-cab">
        <div><h2>Equipe agora</h2><p>Quem está com o quê — reputação das contas e solicitações dos sellers. Clique num número ou num operador para filtrar.</p></div>
        <div class="rp-kpis">
          ${kpi(e.precisam, "contas precisam de atenção", "todos")}
          ${kpi(e.livres, "livres (ninguém assumiu)", "livres", true)}
          ${kpi(e.em_atendimento, "em atendimento", "em_atendimento")}
          ${kpi(e.paradas, `paradas há +${estado.dados.horas_parada}h`, "paradas", true)}
          ${kpi(e.tratadas_hoje, "tratadas hoje", "tratadas", false, "#1c9a63")}
        </div>
      </div>
      ${e.operadores.length ? `<div class="rp-tabela-rolagem"><table class="rp-eq">
        <thead><tr><th>Operador</th><th>Reputação (contas)</th><th>Solicitações sellers</th><th>Mais antiga com ele</th><th>Contas</th><th>Tratadas hoje</th></tr></thead>
        <tbody>${linhas}</tbody></table></div>` : `<div class="rp-sub">Nenhum operador cadastrado.</div>`}
    </section>`;
  }

  // Filtros de atendimento (supervisão).
  function filtroAtdHtml(contagemAtd) {
    if (!estado.dados.equipe) return "";
    const ops = estado.dados.equipe.operadores.filter((o) => o.contas.length);
    const chip = (k, n) => `<button class="rp-chip ${estado.atd === k ? "ativo" : ""}" data-atd="${k}">${n}</button>`;
    return `<div class="rp-filtros" style="margin-top:-6px">
      <span class="rp-sub" style="font-weight:600">Atendimento:</span>
      ${chip("todos", "Todos")}${chip("livres", "Livres · " + contagemAtd.livres)}${chip("em_atendimento", "Em atendimento · " + contagemAtd.em_atendimento)}
      ${chip("meus", "Meus · " + contagemAtd.meus)}${chip("paradas", `Paradas +${estado.dados.horas_parada}h · ` + contagemAtd.paradas)}${chip("tratadas", "Tratadas")}
      <select class="rp-select" id="rp-operador" aria-label="Filtrar por operador">
        <option value="">Operador: todos</option>
        ${ops.map((o) => `<option value="${o.id}" ${String(estado.operador) === String(o.id) ? "selected" : ""}>${esc(o.nome)} (${o.contas.length})</option>`).join("")}
      </select>
    </div>`;
  }

  function passaFiltroAtd(c) {
    const a = c.atendimento;
    if (estado.operador && !(naFila(a) && String(a.em_atendimento_por_id) === String(estado.operador))) return false;
    switch (estado.atd) {
      case "livres": return naFila(a) && !a.em_atendimento_por_id;
      case "em_atendimento": return naFila(a) && !!a.em_atendimento_por_id;
      case "meus": return naFila(a) && ehMeu(a);
      case "paradas": return naFila(a) && a.parada;
      case "tratadas": return !!a && a.status === "concluido";
      default: return true;
    }
  }

  // Ordem dos cartões em atenção: paradas e livres primeiro (as que esperam há
  // mais tempo no topo), depois as que estão com alguém, e as tratadas por último.
  function pesoAtd(c) {
    const a = c.atendimento;
    if (!a) return 1;
    if (a.status === "concluido") return 3;
    if (a.parada) return 0;
    if (!a.em_atendimento_por_id) return 1;
    return 2;
  }

  function desenhar() {
    const d = estado.dados;
    const todas = d.contas;
    const contagem = {};
    todas.forEach((c) => { contagem[c.situacao] = (contagem[c.situacao] || 0) + 1; });

    document.getElementById("rp-subtitulo").textContent =
      `Termômetro do Mercado Livre de ${todas.length} contas · ` +
      (d.atualizado_em ? `atualizado em ${dataBr(d.atualizado_em)}` : "ainda sem leitura");

    // Filtros
    const termo = estado.busca.trim().toLowerCase();
    let base = todas.filter((c) => !termo || c.nome.toLowerCase().includes(termo));
    if (estado.indicador) base = base.filter((c) => (usoDe(c, estado.indicador) ?? 0) >= d.faixa_atencao);
    if (estado.situacao !== "todas") base = base.filter((c) => c.situacao === estado.situacao);
    const filtrandoAtd = d.equipe && (estado.atd !== "todos" || estado.operador);
    if (filtrandoAtd) base = base.filter(passaFiltroAtd);
    const contagemAtd = { livres: 0, em_atendimento: 0, meus: 0, paradas: 0 };
    todas.forEach((c) => {
      const a = c.atendimento;
      if (!naFila(a)) return;
      if (!a.em_atendimento_por_id) contagemAtd.livres++; else contagemAtd.em_atendimento++;
      if (ehMeu(a)) contagemAtd.meus++;
      if (a.parada) contagemAtd.paradas++;
    });

    const ordemPior = (a, b) => (estado.indicador
      ? (usoDe(b, estado.indicador) ?? 0) - (usoDe(a, estado.indicador) ?? 0)
      : piorUso(b) - piorUso(a));
    const porNome = (a, b) => a.nome.localeCompare(b.nome, "pt-BR");
    const atencao = base.filter((c) => c.situacao === "critico" || c.situacao === "atencao" || (filtrandoAtd && c.atendimento))
      .sort((a, b) => (pesoAtd(a) - pesoAtd(b)) || ordemPior(a, b));
    const ok = filtrandoAtd ? [] : base.filter((c) => c.situacao === "ok" || c.situacao === "sem").sort(porNome);
    const semDados = filtrandoAtd ? [] : base.filter((c) => c.situacao === "sem_dados").sort(porNome);

    const chipsDef = [["todas", "Todas"], ["critico", "Passaram"], ["atencao", "A verificar"], ["sem", "Sem medalha"], ["ok", "Em dia"], ["sem_dados", "Sem leitura"]];
    const chips = chipsDef.map(([k, n]) =>
      `<button class="rp-chip ${estado.situacao === k ? "ativo" : ""}" data-chip="${k}">${n}${k === "todas" ? "" : " · " + (contagem[k] || 0)}</button>`).join("");
    const indAtivo = estado.indicador ? d.limites.find((i) => i.chave === estado.indicador) : null;

    const mostrarOk = estado.mostrarOk || estado.situacao === "ok" || estado.situacao === "sem" || !!termo;
    const mostrarSem = estado.mostrarSemDados || estado.situacao === "sem_dados" || !!termo;

    let html = equipeHtml() + `<div class="rp-resumo">${roscaHtml(contagem, todas.length)}${indicadoresHtml(todas)}</div>
      <div class="rp-filtros">
        <input class="rp-busca" id="rp-busca" type="search" placeholder="Buscar conta…" value="${esc(estado.busca)}" aria-label="Buscar conta">
        ${chips}
        ${indAtivo ? `<button class="rp-chip ativo" data-limpar-indicador>${esc(indAtivo.nome)} em atenção ✕</button>` : ""}
        <span class="rp-contador">${atencao.length + ok.length + semDados.length} de ${todas.length} contas</span>
      </div>${filtroAtdHtml(contagemAtd)}`;

    if (atencao.length) {
      html += `<div class="rp-titulo-grupo"><span>Precisam de atenção <span style="color:#c2410c">(${atencao.length})</span>
        <span class="rp-sub" style="font-weight:400"> · as paradas e as livres aparecem primeiro</span></span></div>
        <div class="rp-grade">${atencao.map(cardHtml).join("")}</div>`;
    }
    if (ok.length) {
      html += `<div class="rp-titulo-grupo"><span>Em dia <span style="color:#1e7a47">(${ok.length})</span></span>
        <button class="rp-btn" data-alternar="ok">${mostrarOk ? "Recolher" : `Mostrar ${ok.length} contas em dia`}</button></div>
        ${mostrarOk ? `<div class="rp-grade">${ok.map(cardHtml).join("")}</div>` : ""}`;
    }
    if (semDados.length) {
      html += `<div class="rp-titulo-grupo"><span>Sem leitura <span style="color:#6b7684">(${semDados.length})</span></span>
        <button class="rp-btn" data-alternar="sem_dados">${mostrarSem ? "Recolher" : `Mostrar ${semDados.length}`}</button></div>
        ${mostrarSem ? `<div class="rp-painel" style="padding:6px 0;margin-bottom:22px">${semDados.map((c) => `
          <div style="display:flex;justify-content:space-between;gap:12px;padding:9px 16px;border-bottom:1px solid #f0f2f4;font-size:13.5px">
            <b>${esc(c.nome)}</b>
            <span style="color:#6b7684;text-align:right">${!c.conectada ? "Não conectada — reconecte em Contas conectadas"
              : c.erro ? "Falhou: " + esc(c.erro.slice(0, 120)) : "Aguardando a primeira leitura"}</span>
          </div>`).join("")}</div>` : ""}`;
    }
    if (!atencao.length && !ok.length && !semDados.length) {
      html += `<div class="rp-painel rp-vazio">Nenhuma conta com esse filtro.</div>`;
    }

    const main = document.getElementById("rp-conteudo");
    const tinhaFoco = document.activeElement && document.activeElement.id === "rp-busca";
    const cursor = tinhaFoco ? document.activeElement.selectionStart : null;
    main.innerHTML = html;
    if (tinhaFoco) {
      const b = document.getElementById("rp-busca");
      b.focus();
      if (cursor !== null) b.setSelectionRange(cursor, cursor);
    }
    desenharDetalhe();
  }

  function desenharDetalhe() {
    const alvo = document.getElementById("rp-detalhe");
    const c = estado.detalhe !== null ? estado.dados.contas.find((x) => x.id === estado.detalhe) : null;
    if (!c) { alvo.innerHTML = ""; return; }
    const [tag, tagEstilo] = tagDe(c);
    const faixa = estado.dados.faixa_atencao;
    const mets = c.metricas.length ? c.metricas.map((m) => {
      const u = m.uso;
      const corValor = u >= 1 ? "#b3261e" : u >= faixa ? "#b45309" : "#1b2430";
      return `<div class="rp-det-met">
        <div style="display:flex;justify-content:space-between;align-items:baseline">
          <span style="font-size:14px;font-weight:600">${esc(m.nome)}</span>
          <span style="font-size:20px;font-weight:700;color:${corValor}">${pct(m.taxa)}</span>
        </div>
        <div class="rp-barra"><div style="width:${u === null ? 0 : Math.max(2, Math.min(100, u * 100))}%;background:${corUso(u)}"></div></div>
        <div style="display:flex;justify-content:space-between;font-size:12px;color:#5b6776">
          <span>${m.qtd ?? "—"} venda(s) · limite ${pct(m.limite)}</span>
          <span>${u === null ? "sem dado do ML" : Math.round(u * 100) + "% do limite"}</span>
        </div>
      </div>`;
    }).join("") : `<div class="rp-painel rp-vazio">${!c.conectada ? "Conta não conectada." : c.erro ? esc(c.erro) : "Ainda sem leitura."}</div>`;

    alvo.innerHTML = `<div class="rp-fundo" data-fechar></div>
      <aside class="rp-gaveta" role="dialog" aria-label="Reputação de ${esc(c.nome)}">
        <div style="display:flex;justify-content:space-between;gap:10px">
          <div>
            <span class="rp-tag" style="${tagEstilo}">${tag}</span>
            <div style="font-size:22px;font-weight:700;margin-top:8px">${esc(c.nome)}</div>
            ${medalhaHtml(c)}
          </div>
          <button class="rp-btn" style="width:38px;padding:0" aria-label="Fechar" data-fechar>✕</button>
        </div>
        <div style="margin:16px 0 6px">${termometro(c.nivel, true)}</div>
        <div style="font-size:13px;color:#5b6776">${c.vendas ?? "—"} vendas${c.periodo ? " nos últimos " + esc(periodoBr(c.periodo)) : ""}
          · lido em ${dataBr(c.lido_em)}</div>
        ${c.erro && c.lido_em ? `<div style="font-size:12.5px;color:#96341c;margin-top:6px">A última leitura falhou: ${esc(c.erro.slice(0, 160))}</div>` : ""}
        ${blocoAtdGaveta(c)}
        <div style="display:flex;flex-direction:column;gap:12px;margin-top:18px">${mets}</div>
      </aside>`;
    const campo = document.getElementById("rp-form-texto");
    if (campo && !campo.value) campo.focus();
  }

  // Parte de atendimento da gaveta: etiqueta, ações, comparação, tempos, histórico.
  function blocoAtdGaveta(c) {
    const a = c.atendimento;
    const det = estado.detalheAtd && estado.detalheAtd.conta === c.id ? estado.detalheAtd.dados : null;
    const solic = linkSolicitacoes(c);
    if (!a && !det) return solic ? `<div class="rp-bloco">${solic}</div>` : "";
    const comp = det && det.metricas_abertura && Object.keys(det.metricas_abertura).length ? `
      <div class="rp-bloco"><div class="rp-rot">Métricas: quando entrou na fila × agora</div>
        <table class="rp-comp"><tr><th>Indicador</th><th>Entrou</th><th>Agora</th></tr>
        ${c.metricas.map((m) => {
          const antes = det.metricas_abertura[m.chave];
          const seta = antes === null || antes === undefined || m.taxa === null ? "" : m.taxa < antes ? " ▼" : m.taxa > antes ? " ▲" : "";
          const cor = seta === " ▼" ? "#1c9a63" : seta === " ▲" ? "#b3261e" : "inherit";
          return `<tr><td>${esc(m.nome)}</td><td>${pct(antes)}</td><td style="color:${cor};font-weight:${seta ? 700 : 400}">${pct(m.taxa)}${seta}</td></tr>`;
        }).join("")}</table></div>` : "";
    const form = estado.formulario && naFila(a) ? `
      <div class="rp-bloco rp-form">
        <div class="rp-rot" style="font-weight:600;color:#1b2430">${estado.formulario === "concluir" ? "Concluir atendimento — o que foi feito? *" : "Nova anotação"}</div>
        <textarea id="rp-form-texto" maxlength="1000" placeholder="${estado.formulario === "concluir" ? "Ex.: Reclamação da venda 2000187 retirada pelo ML após contestação." : "Ex.: Abri contestação no ML, protocolo 55821. Aguardando retorno."}"></textarea>
        ${estado.formulario === "concluir" ? `<input id="rp-form-protocolo" class="rp-busca" style="width:100%;margin-top:8px" maxlength="80" placeholder="Protocolo (opcional)">` : ""}
        <div class="rp-erro" id="rp-form-erro"></div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:10px">
          <button class="rp-btn" data-acao="cancelar-form">Cancelar</button>
          <button class="rp-btn rp-btn-escuro" data-acao="salvar-form" data-id="${c.id}">${estado.formulario === "concluir" ? "Concluir" : "Salvar anotação"}</button>
        </div>
      </div>` : "";
    const tempos = det && det.tempos ? `
      <div class="rp-bloco"><div class="rp-rot" style="font-weight:600;color:#1b2430">Tempo</div>
        <div style="line-height:1.75;font-size:13.5px">Esperando na fila: <b>${esc(det.tempos.fila)}</b>
        ${det.tempos.por_operador.map((o) => `<br>• <b>${esc(o.nome)}</b>: ${esc(o.texto)}${o.atual && naFila(a) ? ' <span class="rp-pill rp-p-azul">atual</span>' : ""}`).join("") || "<br>• Ninguém assumiu ainda"}
        <br>Total: <b>${esc(det.tempos.total)}</b></div></div>` : "";
    const hist = det ? `
      <div class="rp-bloco"><div class="rp-rot" style="font-weight:600;color:#1b2430">Histórico</div>
        <div class="rp-hist">${det.historico.map((e) => `<div><span class="q">${dataBr(e.quando)}</span><b>${esc(e.rotulo)}</b>${e.quem && e.quem !== "Sistema" ? " · " + esc(e.quem) : ""}${e.detalhe ? `<div class="d">${esc(e.detalhe)}</div>` : ""}</div>`).join("")}</div></div>`
      : `<div class="rp-bloco rp-sub">Carregando histórico…</div>`;
    const acoes = naFila(a) ? `<div class="rp-acoes-gaveta">${botoesAtd(c, true)}
      <button class="rp-btn" data-acao="anotar" data-id="${c.id}">+ Anotação</button></div>` : "";
    const conclusao = a && a.status === "concluido" ? `<div class="rp-bloco"><div class="rp-rot">O que foi feito</div>${esc(a.conclusao || "")}${a.protocolo ? `<div class="rp-sub">Protocolo: <b>${esc(a.protocolo)}</b></div>` : ""}</div>` : "";
    return `<div class="rp-atd-gaveta">
      <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center">${etiquetaAtd(a)}${a && a.parada ? `<span class="rp-pill rp-p-verm">parada há +${estado.dados.horas_parada}h</span>` : ""}</div>
      ${acoes}${form}${conclusao}
      ${solic ? `<div class="rp-bloco">${solic}</div>` : ""}
      ${comp}${tempos}${hist}
    </div>`;
  }

  async function carregarDetalheAtd(contaId) {
    try {
      const r = await fetch(`/api/reputacao/atendimento/${contaId}`, { cache: "no-store" });
      const d = await r.json();
      if (!r.ok) throw new Error(d.detail || "falha");
      if (estado.detalhe !== contaId) return;
      estado.detalheAtd = { conta: contaId, dados: d.atendimento };
      desenharDetalhe();
    } catch (erro) { console.error("Histórico do atendimento", erro); }
  }

  function abrirGaveta(contaId) {
    estado.detalhe = contaId;
    estado.formulario = null;
    estado.detalheAtd = null;
    desenharDetalhe();
    carregarDetalheAtd(contaId);
  }

  function fecharGaveta() {
    estado.detalhe = null;
    estado.detalheAtd = null;
    estado.formulario = null;
    desenharDetalhe();
  }

  // Ações de atendimento: chama o servidor, recarrega a tela e o histórico.
  async function acaoAtd(contaId, caminho, corpo) {
    if (estado.ocupado) return false;
    estado.ocupado = true;
    try {
      const r = await fetch(`/api/reputacao/atendimento/${contaId}/${caminho}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(corpo || {}),
      });
      const d = await r.json().catch(() => ({}));
      if (r.status === 401) { window.location.href = "/login"; return false; }
      if (!r.ok) {
        const msg = typeof d.detail === "string" ? d.detail : (d.detail && d.detail.mensagem) || "Não foi possível concluir a ação.";
        const erroForm = document.getElementById("rp-form-erro");
        if (erroForm && (caminho === "concluir" || caminho === "anotar")) { erroForm.textContent = msg; return false; }
        aviso(msg, true);
        await carregar();
        return false;
      }
      aviso("");
      await carregar();
      if (estado.detalhe === contaId) carregarDetalheAtd(contaId);
      return true;
    } catch (erro) {
      aviso("Sem conexão com o servidor agora. Tente de novo.", true);
      return false;
    } finally {
      estado.ocupado = false;
    }
  }

  async function executarAcao(acao, contaId) {
    const conta = estado.dados.contas.find((x) => x.id === contaId);
    const a = conta && conta.atendimento;
    if (acao === "assumir") return acaoAtd(contaId, "assumir", { forcar: false });
    if (acao === "assumir-lugar") {
      if (!a || !confirm(`${a.em_atendimento_por} está com a conta ${conta.nome} ${haQuanto(a.em_atendimento_desde)}.\nAssumir no lugar? O histórico registra quanto tempo ficou com cada um.`)) return;
      return acaoAtd(contaId, "assumir", { forcar: true });
    }
    if (acao === "liberar") return acaoAtd(contaId, "liberar");
    if (acao === "concluir" || acao === "anotar") {
      if (estado.detalhe !== contaId) abrirGaveta(contaId);
      estado.formulario = acao;
      desenharDetalhe();
      return;
    }
    if (acao === "cancelar-form") { estado.formulario = null; desenharDetalhe(); return; }
    if (acao === "salvar-form") {
      const texto = (document.getElementById("rp-form-texto").value || "").trim();
      const erro = document.getElementById("rp-form-erro");
      if (!texto) { erro.textContent = estado.formulario === "concluir" ? "Conte o que foi feito para concluir." : "Escreva a anotação."; return; }
      const corpo = { texto };
      const prot = document.getElementById("rp-form-protocolo");
      if (prot) corpo.protocolo = prot.value.trim() || null;
      const ok = await acaoAtd(contaId, estado.formulario === "concluir" ? "concluir" : "anotar", corpo);
      if (ok) { estado.formulario = null; desenharDetalhe(); }
    }
  }

  // ---------------------------------------------------------------- "Atualizar agora"
  let acompanhando = null;

  function configurarBotaoAtualizar() {
    const botao = document.getElementById("rp-atualizar");
    if (!estado.dados.pode_atualizar) { botao.style.display = "none"; return; }
    botao.style.display = "inline-block";
    if (estado.dados.progresso && estado.dados.progresso.em_andamento) acompanhar();
  }

  async function atualizarAgora() {
    const botao = document.getElementById("rp-atualizar");
    botao.disabled = true;
    try {
      const r = await fetch("/api/reputacao/atualizar", { method: "POST" });
      const d = await r.json().catch(() => null);
      if (!r.ok) throw new Error((d && d.detail) || `status ${r.status}`);
      acompanhar();
    } catch (erro) {
      aviso(`Não foi possível iniciar a atualização: ${erro.message}`, true);
      botao.disabled = false;
    }
  }

  function acompanhar() {
    const botao = document.getElementById("rp-atualizar");
    botao.disabled = true;
    if (acompanhando) return;
    acompanhando = setInterval(async () => {
      try {
        const r = await fetch("/api/reputacao/status");
        const p = await r.json();
        if (p.em_andamento) {
          botao.textContent = `Lendo ${p.feitas} de ${p.total}…`;
          return;
        }
        clearInterval(acompanhando);
        acompanhando = null;
        botao.textContent = "Atualizar agora";
        botao.disabled = false;
        aviso("");
        await carregar();
      } catch (erro) {
        console.error(erro); // falha pontual: tenta de novo no próximo ciclo
      }
    }, 3000);
  }

  // ---------------------------------------------------------------- eventos
  document.addEventListener("click", (e) => {
    if (!estado.dados) return;
    if (e.target.closest("[data-parar]")) return; // links dentro do cartão (ex.: solicitações do seller)
    const botaoAtd = e.target.closest("[data-acao]");
    if (botaoAtd) {
      e.stopPropagation();
      executarAcao(botaoAtd.dataset.acao, Number(botaoAtd.dataset.id));
      return;
    }
    const el = e.target.closest("[data-situacao],[data-chip],[data-indicador],[data-limpar-indicador],[data-alternar],[data-conta],[data-fechar],[data-atd],[data-operador]");
    if (!el) return;
    if (el.hasAttribute("data-fechar")) { fecharGaveta(); return; }
    if (el.dataset.conta) { abrirGaveta(Number(el.dataset.conta)); return; }
    if (el.dataset.atd) {
      estado.atd = el.dataset.atd;
      estado.operador = "";
      if (estado.atd !== "todos") { estado.situacao = "todas"; estado.indicador = null; }
      desenhar();
      return;
    }
    if (el.dataset.operador) {
      estado.operador = estado.operador === el.dataset.operador ? "" : el.dataset.operador;
      estado.atd = "todos";
      desenhar();
      const grade = document.querySelector(".rp-grade");
      if (grade && estado.operador) grade.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    if (el.dataset.situacao) {
      const k = el.dataset.situacao;
      estado.situacao = estado.situacao === k ? "todas" : k;
      estado.indicador = null;
    } else if (el.dataset.chip) {
      estado.situacao = el.dataset.chip;
    } else if (el.dataset.indicador) {
      const k = el.dataset.indicador;
      estado.indicador = estado.indicador === k ? null : k;
      estado.situacao = "todas";
    } else if (el.hasAttribute("data-limpar-indicador")) {
      estado.indicador = null;
    } else if (el.dataset.alternar === "ok") {
      estado.mostrarOk = !estado.mostrarOk;
    } else if (el.dataset.alternar === "sem_dados") {
      estado.mostrarSemDados = !estado.mostrarSemDados;
    }
    desenhar();
  });

  document.addEventListener("input", (e) => {
    if (e.target.id === "rp-busca") { estado.busca = e.target.value; desenhar(); }
  });
  document.addEventListener("change", (e) => {
    if (e.target.id === "rp-operador") { estado.operador = e.target.value; estado.atd = "todos"; desenhar(); }
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && estado.detalhe !== null) { fecharGaveta(); return; }
    // Cartão é um <div role="button">: Enter/Espaço abrem a conta, como um botão.
    if ((e.key === "Enter" || e.key === " ") && e.target.classList && e.target.classList.contains("rp-card")) {
      e.preventDefault();
      abrirGaveta(Number(e.target.dataset.conta));
    }
  });

  // Atualiza sozinha a cada 60 s (quem assumiu o quê e os "há X min"), mas
  // não enquanto alguém está escrevendo uma anotação/conclusão.
  setInterval(() => {
    if (!estado.dados || estado.formulario || estado.ocupado || document.hidden) return;
    carregar().then(() => { if (estado.detalhe !== null) carregarDetalheAtd(estado.detalhe); }).catch(() => {});
  }, 60000);

  document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("rp-atualizar").addEventListener("click", atualizarAgora);
    carregar().catch((erro) => {
      console.error(erro);
      document.getElementById("rp-conteudo").innerHTML =
        `<div class="rp-painel rp-vazio">Não foi possível carregar a reputação das contas (${esc(erro.message)}).</div>`;
    });
  });
})();
