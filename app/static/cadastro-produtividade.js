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
    if (per === "mes") return [hoje.slice(0, 8) + "01", hoje];
    return [hoje, hoje];
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

  function desenharFila(f) {
    const card = (titulo, valor, sub) => `<div class="pd-card"><small>${esc(titulo)}</small><b>${esc(valor)}</b><span>${esc(sub)}</span></div>`;
    document.getElementById("pd-fila").innerHTML =
      card("Produtos pendentes", f.produtos_pendentes, "esperando cadastro") +
      card("Faltam no Mercado Livre", f.ml, "produtos pendentes") +
      card("Faltam no TikTok", f.tiktok, "produtos pendentes") +
      card("Faltam na Shopee", f.shopee, "produtos pendentes");
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
      <td>${p.produtos_fechados}</td>
      <td>${p.incluidos_manual}</td>
    </tr>`).join("");
    const t = d.totais;
    alvo.innerHTML = `<table>
      <thead><tr><th>Pessoa</th><th>${NOMES.ml}</th><th>${NOMES.tiktok}</th><th>${NOMES.shopee}</th><th>Total</th><th>Produtos fechados</th><th>Incluídos à mão</th></tr></thead>
      <tbody>${linhas}</tbody>
      <tfoot><tr><td>Todos</td><td>${t.ml}</td><td>${t.tiktok}</td><td>${t.shopee}</td><td>${t.total}</td><td>${t.produtos_fechados}</td><td>${t.incluidos_manual}</td></tr></tfoot>
    </table>`;
  }

  async function carregar(de, ate, silencioso) {
    if (ocupado) return;
    ocupado = true;
    try {
      const d = await buscar(de, ate);
      aviso("");
      desenharFila(d.fila);
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
