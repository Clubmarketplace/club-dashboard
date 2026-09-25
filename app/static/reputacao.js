/**
 * Tela "Reputação das contas": termômetro do Mercado Livre de todas as contas.
 *
 * - Rosca clicável no topo: clicar numa cor filtra as contas daquela situação.
 * - Cartões dos 4 indicadores: clicar filtra as contas em atenção naquele indicador.
 * - Busca por nome + chips de situação.
 * - "Precisam de atenção" sempre aberto; "Em dia" e "Sem leitura" recolhidos.
 * - Gaveta de detalhe ao clicar numa conta.
 * - "Atualizar agora" (admin/supervisor) dispara a leitura e acompanha o progresso.
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
    return `<button class="rp-card" style="${borda}" data-conta="${conta.id}">
      <div class="rp-card-topo"><span class="rp-tag" style="${tagEstilo}">${tag}</span>
        <span>${conta.vendas !== null && conta.vendas !== undefined ? conta.vendas + " vendas" : ""}</span></div>
      <div><div class="rp-nome">${esc(conta.nome)}</div>${medalhaHtml(conta)}</div>
      ${termometro(conta.nivel, false)}
      <div class="rp-mets">${mets}</div>
      ${mot ? `<div class="rp-motivo">${esc(mot)}</div>` : ""}
      ${antiga}
    </button>`;
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

    const ordemPior = (a, b) => (estado.indicador
      ? (usoDe(b, estado.indicador) ?? 0) - (usoDe(a, estado.indicador) ?? 0)
      : piorUso(b) - piorUso(a));
    const porNome = (a, b) => a.nome.localeCompare(b.nome, "pt-BR");
    const atencao = base.filter((c) => c.situacao === "critico" || c.situacao === "atencao").sort(ordemPior);
    const ok = base.filter((c) => c.situacao === "ok" || c.situacao === "sem").sort(porNome);
    const semDados = base.filter((c) => c.situacao === "sem_dados").sort(porNome);

    const chipsDef = [["todas", "Todas"], ["critico", "Passaram"], ["atencao", "A verificar"], ["sem", "Sem medalha"], ["ok", "Em dia"], ["sem_dados", "Sem leitura"]];
    const chips = chipsDef.map(([k, n]) =>
      `<button class="rp-chip ${estado.situacao === k ? "ativo" : ""}" data-chip="${k}">${n}${k === "todas" ? "" : " · " + (contagem[k] || 0)}</button>`).join("");
    const indAtivo = estado.indicador ? d.limites.find((i) => i.chave === estado.indicador) : null;

    const mostrarOk = estado.mostrarOk || estado.situacao === "ok" || estado.situacao === "sem" || !!termo;
    const mostrarSem = estado.mostrarSemDados || estado.situacao === "sem_dados" || !!termo;

    let html = `<div class="rp-resumo">${roscaHtml(contagem, todas.length)}${indicadoresHtml(todas)}</div>
      <div class="rp-filtros">
        <input class="rp-busca" id="rp-busca" type="search" placeholder="Buscar conta…" value="${esc(estado.busca)}" aria-label="Buscar conta">
        ${chips}
        ${indAtivo ? `<button class="rp-chip ativo" data-limpar-indicador>${esc(indAtivo.nome)} em atenção ✕</button>` : ""}
        <span class="rp-contador">${atencao.length + ok.length + semDados.length} de ${todas.length} contas</span>
      </div>`;

    if (atencao.length) {
      html += `<div class="rp-titulo-grupo"><span>Precisam de atenção <span style="color:#c2410c">(${atencao.length})</span></span></div>
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
        <div style="display:flex;flex-direction:column;gap:12px;margin-top:18px">${mets}</div>
      </aside>`;
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
    const el = e.target.closest("[data-situacao],[data-chip],[data-indicador],[data-limpar-indicador],[data-alternar],[data-conta],[data-fechar]");
    if (!el || !estado.dados) return;
    if (el.hasAttribute("data-fechar")) { estado.detalhe = null; desenharDetalhe(); return; }
    if (el.dataset.conta) { estado.detalhe = Number(el.dataset.conta); desenharDetalhe(); return; }
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

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && estado.detalhe !== null) { estado.detalhe = null; desenharDetalhe(); }
  });

  document.addEventListener("DOMContentLoaded", () => {
    document.getElementById("rp-atualizar").addEventListener("click", atualizarAgora);
    carregar().catch((erro) => {
      console.error(erro);
      document.getElementById("rp-conteudo").innerHTML =
        `<div class="rp-painel rp-vazio">Não foi possível carregar a reputação das contas (${esc(erro.message)}).</div>`;
    });
  });
})();
