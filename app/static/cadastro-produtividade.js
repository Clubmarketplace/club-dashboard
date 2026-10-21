/**
 * Produtividade do Cadastro: quanto cada pessoa cadastrou por marketplace no período.
 * API: GET /api/cadastro/produtividade?de=AAAA-MM-DD&ate=AAAA-MM-DD (só admin/supervisor).
 */
(function () {
  "use strict";

  const NOMES = { ml: "Mercado Livre", tiktok: "TikTok", shopee: "Shopee" };
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
    if (per === "semana") {
      const [a, m, d] = hoje.split("-").map(Number);
      const diaSemana = new Date(Date.UTC(a, m - 1, d)).getUTCDay(); // 0=dom .. 6=sáb
      const inicioSemana = diaSemana === 0 ? 6 : diaSemana - 1; // segunda-feira
      return [somarDias(hoje, -inicioSemana), hoje];
    }
    if (per === "mes") return [hoje.slice(0, 8) + "01", hoje];
    return [hoje, hoje];
  }

  // Tempo médio em "3h 20min" / "2d 3h" ("—" quando ninguém assumiu/fechou produto no período).
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
    const r = await fetch("/api/cadastro/produtividade?" + new URLSearchParams({ de, ate }).toString());
    const d = await r.json().catch(() => null);
    if (!r.ok) throw new Error((d && typeof d.detail === "string" && d.detail) || `status ${r.status}`);
    return d;
  }

  // 20/10: painel de gestão. Total geral = tudo que chegou (concluídas +
  // pendentes + aguardando definição). "Aguardando definição" = ainda sem
  // canal escolhido: conta no total geral, mas não entra em nenhum canal.
  // Cada canal mostra: chegaram / feito / falta (chegaram = feito + falta) e,
  // embaixo, o que cada pessoa fez ali Hoje / Semana / Mês. "Falta" é do
  // canal (fila compartilhada), não de uma pessoa.
  function desenharFila(d) {
    const f = d.fila;
    const porCanal = d.pessoas_canais || {};
    const linha = (rotulo, valor) => `<div class="pd-fila-linha"><span>${esc(rotulo)}</span><b>${esc(valor)}</b></div>`;
    let html = `<div class="pd-card"><small>Total geral</small><b>${esc(f.total_geral)}</b>`
      + `<span>${esc(f.concluidas ?? 0)} concluídas · ${esc(f.pendentes ?? 0)} pendentes</span></div>`;
    html += `<div class="pd-card"><small>Aguardando definição</small><b>${esc(f.aguardando_definicao ?? 0)}</b>`
      + `<span>sem canal definido — já contam no total geral, mas ainda não entram em nenhuma plataforma</span></div>`;
    document.getElementById("pd-fila").innerHTML = html;

    let canais = "";
    for (const k of ["ml", "shopee", "tiktok"]) {
      const { total = 0, feito = 0, falta = 0 } = f[k] || {};
      const pessoas = porCanal[k] || [];
      const linhasPessoas = pessoas.length
        ? pessoas.map((p) => `<tr><td class="pd-nome">${esc(p.nome)}</td><td>${p.hoje}</td><td>${p.semana}</td><td>${p.mes}</td></tr>`).join("")
        : '<tr><td colspan="4" class="pd-vazio">Ninguém cadastrou neste canal este mês.</td></tr>';
      canais += `<div class="pd-canal"><div class="pd-canal-topo"><h3>${esc(NOMES[k])}</h3>`
        + `<div class="pd-canal-nums">`
        + `<div><small>Chegaram</small><b>${esc(total)}</b></div>`
        + `<div><small>Feito</small><b>${esc(feito)}</b></div>`
        + `<div><small>Falta</small><b>${esc(falta)}</b></div></div></div>`
        + `<div class="pd-tabela pd-canal-tab"><table><thead><tr><th>Quem fez</th><th>Hoje</th><th>Semana</th><th>Mês</th></tr></thead>`
        + `<tbody>${linhasPessoas}</tbody></table></div></div>`;
    }
    document.getElementById("pd-canais").innerHTML = canais;
  }

  function desenharPessoas(d) {
    document.getElementById("pd-titulo-periodo").textContent =
      d.de === d.ate ? `No dia ${dataCurta(d.de)}` : `De ${dataCurta(d.de)} a ${dataCurta(d.ate)}`;
    const alvo = document.getElementById("pd-pessoas");
    if (!d.pessoas.length) {
      alvo.innerHTML = '<div class="pd-vazio">Ninguém marcou cadastro neste período.</div>';
      return;
    }
    const maior = Math.max(1, ...d.pessoas.map((p) => p.total));
    const linhas = d.pessoas.map((p) => `<tr>
      <td class="pd-nome">${esc(p.nome)}<span class="pd-barra"><i style="width:${Math.round((p.total / maior) * 100)}%"></i></span></td>
      <td>${p.ml}</td><td>${p.tiktok}</td><td>${p.shopee}</td>
      <td class="pd-total">${p.total}</td>
      <td class="pd-total">${p.total_hoje}</td>
      <td>${p.produtos_fechados}</td>
      <td>${p.incluidos_manual}</td>
      <td>${esc(tempoMedio(p.tempo_medio_seg))}</td>
    </tr>`).join("");
    const t = d.totais;
    alvo.innerHTML = `<table>
      <thead><tr><th>Pessoa</th><th>${NOMES.ml}</th><th>${NOMES.tiktok}</th><th>${NOMES.shopee}</th><th>Total</th><th>Hoje</th><th>Cadastros concluídos</th><th>Incluídos à mão</th><th>Tempo médio por produto</th></tr></thead>
      <tbody>${linhas}</tbody>
      <tfoot><tr><td>Todos</td><td>${t.ml}</td><td>${t.tiktok}</td><td>${t.shopee}</td><td>${t.total}</td><td>${t.total_hoje}</td><td>${t.produtos_fechados}</td><td>${t.incluidos_manual}</td><td>${esc(tempoMedio(t.tempo_medio_seg))}</td></tr></tfoot>
    </table>`;
  }

  async function carregar(de, ate, silencioso) {
    if (ocupado) return;
    ocupado = true;
    try {
      const d = await buscar(de, ate);
      aviso("");
      desenharFila(d);
      desenharPessoas(d);
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
