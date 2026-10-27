/**
 * Produtividade de Solicitações: quanto cada pessoa confirmou (cancelamento
 * e reputação) por período. Espelha cadastro-produtividade.js.
 * API: GET /api/solicitacoes-cancelamento/produtividade?de=AAAA-MM-DD&ate=AAAA-MM-DD
 * (só admin/supervisor/gerente_setor do setor "solicitacoes_cancelamento").
 */
(function () {
  "use strict";

  let periodoAtual = "hoje";
  let ocupado = false;

  function esc(t) {
    return String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  // "hoje" no horário de Brasília (e não no do computador de quem está olhando).
  function hojeBrasilia() {
    return new Intl.DateTimeFormat("en-CA", { timeZone: "America/Sao_Paulo", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  }

  function somarDias(iso, dias) {
    const [a, m, d] = iso.split("-").map(Number);
    const data = new Date(Date.UTC(a, m - 1, d + dias));
    return data.toISOString().slice(0, 10);
  }

  function intervalo(per) {
    const hoje = hojeBrasilia();
    if (per === "ontem") { const o = somarDias(hoje, -1); return [o, o]; }
    if (per === "7dias") return [somarDias(hoje, -6), hoje];
    if (per === "mes") return [hoje.slice(0, 8) + "01", hoje];
    return [hoje, hoje];
  }

  // Tempo médio em "3h 20min" / "2d 3h" ("—" quando ninguém confirmou pedido no período).
  function tempoMedio(seg) {
    if (seg === null || seg === undefined) return "—";
    const min = Math.floor(seg / 60);
    if (min < 1) return "menos de 1 min";
    if (min < 60) return `${min}min`;
    const h = Math.floor(min / 60), m = min % 60;
    if (h < 24) return m ? `${h}h ${String(m).padStart(2, "0")}min` : `${h}h`;
    const d = Math.floor(h / 24), hr = h % 24;
    return hr ? `${d}d ${hr}h` : `${d}d`;
  }

  function dataCurta(iso) {
    const [a, m, d] = iso.split("-");
    return `${d}/${m}/${a}`;
  }

  function aviso(texto) {
    const el = document.getElementById("pd-aviso");
    el.textContent = texto || "";
    el.style.display = texto ? "block" : "none";
  }

  async function buscar(de, ate) {
    const r = await fetch("/api/solicitacoes-cancelamento/produtividade?" + new URLSearchParams({ de, ate }).toString());
    const d = await r.json().catch(() => null);
    if (!r.ok) throw new Error((d && typeof d.detail === "string" && d.detail) || `status ${r.status}`);
    return d;
  }

  function desenharFila(f) {
    const card = (titulo, valor, sub) => `<div class="pd-card"><small>${esc(titulo)}</small><b>${esc(valor)}</b><span>${esc(sub)}</span></div>`;
    document.getElementById("pd-fila").innerHTML =
      card("Pedidos pendentes", f.pendentes, "aguardando confirmação") +
      card("Livres", f.livres, "esperando alguém assumir") +
      card("Em atendimento", f.em_atendimento, "já estão com alguém");
  }

  function desenharPessoas(d) {
    document.getElementById("pd-titulo-periodo").textContent =
      d.de === d.ate ? `No dia ${dataCurta(d.de)}` : `De ${dataCurta(d.de)} a ${dataCurta(d.ate)}`;
    const alvo = document.getElementById("pd-pessoas");
    if (!d.pessoas.length) {
      alvo.innerHTML = '<div class="pd-vazio">Ninguém confirmou pedido neste período.</div>';
      return;
    }
    const maior = Math.max(1, ...d.pessoas.map((p) => p.total));
    const linhas = d.pessoas.map((p) => `<tr>
      <td class="pd-nome">${esc(p.nome)}<span class="pd-barra"><i style="width:${Math.round((p.total / maior) * 100)}%"></i></span></td>
      <td>${p.cancelamento}</td><td>${p.reputacao}</td><td>${p.reverso || 0}</td>
      <td class="pd-total">${p.total}</td>
      <td>${p.assumidas_primeiro}</td>
      <td>${esc(tempoMedio(p.tempo_medio_seg))}</td>
    </tr>`).join("");
    const t = d.totais;
    alvo.innerHTML = `<table>
      <thead><tr><th>Pessoa</th><th>Cancelamentos concluídos</th><th>Reputações resolvidas</th><th>Reversos concluídos</th><th>Total</th><th>Assumidas primeiro</th><th>Tempo médio</th></tr></thead>
      <tbody>${linhas}</tbody>
      <tfoot><tr><td>Todos</td><td>${t.cancelamento}</td><td>${t.reputacao}</td><td>${t.reverso || 0}</td><td>${t.total}</td><td>${t.assumidas_primeiro}</td><td>${esc(tempoMedio(t.tempo_medio_seg))}</td></tr></tfoot>
    </table>`;
  }

  async function buscarReversos(de, ate) {
    const r = await fetch("/api/solicitacoes-cancelamento/reversos-resumo?" + new URLSearchParams({ de, ate }).toString());
    const d = await r.json().catch(() => null);
    if (!r.ok) throw new Error((d && typeof d.detail === "string" && d.detail) || `status ${r.status}`);
    return d;
  }

  function tabelaReversos(linhas, rotuloPrimeira, nome, total) {
    if (!linhas.length) return '<div class="pd-vazio">Nenhum reverso chegou neste período.</div>';
    const tr = (x, destaque) => `<tr${destaque ? ' style="font-weight:700"' : ""}>
      <td class="pd-nome">${esc(destaque ? "Total" : nome(x))}</td><td class="pd-total">${x.chegaram}</td>
      <td>${x.liberados_venda}</td><td>${x.nao_voltaram}</td><td>${x.avariados}</td><td>${x.em_andamento}</td><td>${x.pct_venda}%</td></tr>`;
    return `<table><thead><tr><th>${esc(rotuloPrimeira)}</th><th>Chegaram</th><th>Voltaram para o estoque</th><th>Não voltaram</th><th>(avariados)</th><th>Em andamento</th><th>% voltou para o estoque</th></tr></thead>
      <tbody>${linhas.map((x) => tr(x, false)).join("")}</tbody><tfoot>${tr(total, true).replace("<tr", '<tr class="pd-tot"')}</tfoot></table>`;
  }

  function desenharReversos(d) {
    const card = (titulo, valor, sub) => `<div class="pd-card"><small>${esc(titulo)}</small><b>${esc(valor)}</b><span>${esc(sub)}</span></div>`;
    const t = d.total;
    document.getElementById("pd-rev-kpis").innerHTML =
      card("Chegaram hoje", d.chegaram_hoje, "reversos recebidos hoje") +
      card("Chegaram na semana", d.chegaram_semana, "últimos 7 dias") +
      card("Chegaram no período", t.chegaram, "do período escolhido") +
      card("Voltaram para o estoque", t.liberados_venda, `${t.pct_venda}% dos que chegaram`) +
      card("Não voltaram", t.nao_voltaram, `${t.avariados} avariado(s)`) +
      card("Em andamento", t.em_andamento, "ainda sem conclusão");
    document.getElementById("pd-rev-conta").innerHTML = tabelaReversos(d.por_conta, "Conta", (x) => x.conta, t);
    document.getElementById("pd-rev-galpao").innerHTML = tabelaReversos(d.por_galpao, "Galpão", (x) => x.galpao_nome, t);
  }

  async function carregar(de, ate, silencioso) {
    if (ocupado) return;
    ocupado = true;
    try {
      const d = await buscar(de, ate);
      aviso("");
      desenharFila(d.fila);
      desenharPessoas(d);
      // A seção de Reversos só existe na tela de admin/supervisor (27/10): sem ela, nem busca.
      if (document.getElementById("pd-rev-kpis")) {
        try { desenharReversos(await buscarReversos(de, ate)); } catch (e2) { if (!silencioso) aviso("Reversos: " + e2.message); }
      }
    } catch (e) {
      if (!silencioso) aviso("Não consegui carregar: " + e.message);
    } finally {
      ocupado = false;
    }
  }

  function escolherPeriodo(per) {
    periodoAtual = per;
    const [de, ate] = intervalo(per);
    document.getElementById("pd-de").value = de;
    document.getElementById("pd-ate").value = ate;
    document.querySelectorAll(".pd-chip").forEach((b) => b.classList.toggle("ativo", b.dataset.per === per));
    carregar(de, ate);
  }

  document.querySelectorAll(".pd-chip").forEach((b) => b.addEventListener("click", () => escolherPeriodo(b.dataset.per)));

  document.getElementById("pd-aplicar").addEventListener("click", () => {
    const de = document.getElementById("pd-de").value, ate = document.getElementById("pd-ate").value;
    if (!de || !ate) return aviso("Escolha as duas datas.");
    periodoAtual = null;
    document.querySelectorAll(".pd-chip").forEach((b) => b.classList.remove("ativo"));
    carregar(de, ate);
  });

  // Atualiza sozinha a cada minuto (só com a aba visível). Período por botão acompanha a virada do dia.
  setInterval(() => {
    if (document.hidden) return;
    if (periodoAtual) { const [de, ate] = intervalo(periodoAtual); carregar(de, ate, true); }
    else carregar(document.getElementById("pd-de").value, document.getElementById("pd-ate").value, true);
  }, 60000);

  escolherPeriodo("hoje");
})();
