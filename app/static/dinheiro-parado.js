/* Dinheiro parado (31/10): visual para admin/supervisor (gráfico por conta + relatório da conta) e para o seller.
   Usado por reversos-gestor.html e meus-reversos.html. Requer Chart.js; o PNG usa html2canvas (static/vendor). */
(function () {
  "use strict";
  const brl = (v) => Number(v || 0).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
  const esc = (t) => String(t ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const COR = { galpao: "#b45309", fabrica: "#7c5cbf", analise: "#d98e2f", ok: "#2f9e63", perdido: "#c24141", texto: "#1f2d3d", suave: "#6b7a8a" };
  const instancias = new Map();
  function novoGrafico(canvas, cfg) {
    if (instancias.has(canvas)) { try { instancias.get(canvas).destroy(); } catch (e) {} }
    const g = new Chart(canvas, cfg); instancias.set(canvas, g); return g;
  }

  // Barras empilhadas horizontais: uma barra por conta (galpão + aguardando a fábrica). onClick(conta_chave).
  function graficoContas(canvas, contas, onClick) {
    const lista = contas.filter(c => c.parado_total > 0 || c.galpao_sem_custo > 0).slice(0, 20);
    canvas.parentElement.style.height = Math.max(160, 36 * lista.length + 70) + "px";
    return novoGrafico(canvas, {
      type: "bar",
      data: {
        labels: lista.map(c => c.conta),
        datasets: [
          { label: "No galpão (garantia/quebrado)", data: lista.map(c => c.galpao_valor), backgroundColor: COR.galpao },
          { label: "Aguardando solução da fábrica", data: lista.map(c => c.fabrica_valor), backgroundColor: COR.fabrica },
        ],
      },
      options: {
        indexAxis: "y", responsive: true, maintainAspectRatio: false,
        scales: { x: { stacked: true, ticks: { callback: v => brl(v) } }, y: { stacked: true } },
        plugins: { tooltip: { callbacks: {
          label: (it) => `${it.dataset.label}: ${brl(it.raw)}`,
          footer: (its) => { const c = lista[its[0].dataIndex]; return `Total parado: ${brl(c.parado_total)}` + (c.galpao_sem_custo ? ` · ${c.galpao_sem_custo} sem custo (fora do valor)` : ""); } } } },
        onClick: (_e, els) => { if (els.length && onClick) onClick(lista[els[0].index].conta_chave, lista[els[0].index].conta); },
        onHover: (e, els) => { e.native.target.style.cursor = els.length ? "pointer" : "default"; },
      },
    });
  }

  function diasTxt(n) { return n === 0 ? "hoje" : n === 1 ? "1 dia" : n + " dias"; }

  // Relatório visual de UMA conta (cartões + rosca + lista). `c` = linha da conta com `itens`.
  function relatorio(el, c, conta, geradoEm) {
    c = c || { galpao_valor: 0, fabrica_valor: 0, parado_total: 0, galpao_qtd: 0, galpao_garantia: 0, galpao_quebrado: 0, galpao_sem_custo: 0,
               fabrica_qtd: 0, analise_qtd: 0, analise_valor: 0, analise_sem_custo: 0, recuperado: 0, perdido: 0, itens: [] };
    const mini = (cor, titulo, valor, sub) => `<div style="flex:1 1 150px;min-width:150px;background:#fff;border:1px solid #dfe5ec;border-top:4px solid ${cor};border-radius:10px;padding:10px 12px">
      <div style="font-size:12px;color:${COR.suave}">${titulo}</div><div style="font-size:20px;font-weight:700;color:${COR.texto}">${valor}</div><div style="font-size:11.5px;color:${COR.suave}">${sub}</div></div>`;
    const semCusto = c.galpao_sem_custo ? `<div style="font-size:12px;color:#a83232;margin-top:6px">${c.galpao_sem_custo} item(ns) no galpão ainda sem custo cadastrado ficam de fora do valor.</div>` : "";
    const linhas = (c.itens || []).map(i => `<tr style="border-bottom:1px solid #e8edf2">
      <td style="padding:7px 8px"><span style="font-weight:600;color:${i.tipo_chave === "garantia" ? "#8a4b00" : "#a83232"}">${esc(i.tipo || "—")}</span></td>
      <td style="padding:7px 8px"><b>${esc(i.sku || "—")}</b><div style="font-size:11.5px;color:${COR.suave}">${esc(i.produto || "")}</div></td>
      <td style="padding:7px 8px">${esc(i.numero_venda)}</td>
      <td style="padding:7px 8px">${i.situacao === "fabrica" ? `Na fábrica · nota ${esc(i.nota)}<div style="font-size:11.5px;color:${COR.suave}">há ${diasTxt(i.dias)}</div>` : `No galpão<div style="font-size:11.5px;color:${COR.suave}">há ${diasTxt(i.dias)}</div>`}</td>
      <td style="padding:7px 8px;text-align:right;font-weight:600">${i.custo == null ? '<span style="color:#a83232">sem custo</span>' : brl(i.custo)}</td></tr>`).join("");
    el.innerHTML = `<div style="background:#f4f7fa;padding:18px;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:${COR.texto};width:100%;box-sizing:border-box">
      <div style="display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:6px">
        <div style="font-size:17px;font-weight:700">Reversos · ${esc(conta)}</div><div style="font-size:12px;color:${COR.suave}">Posição em ${esc(geradoEm || "")}</div></div>
      <div style="display:flex;flex-wrap:wrap;gap:14px;margin:12px 0;align-items:stretch">
        <div style="flex:2 1 300px;background:#fff;border:1px solid #dfe5ec;border-left:6px solid ${COR.galpao};border-radius:12px;padding:16px 18px">
          <div style="font-size:13px;color:${COR.suave}">Dinheiro parado no seu estoque</div>
          <div style="font-size:34px;font-weight:800;color:#8a4b00;line-height:1.15">${brl(c.parado_total)}</div>
          <div style="font-size:13px;margin-top:4px">${brl(c.galpao_valor)} no galpão · ${brl(c.fabrica_valor)} aguardando a fábrica</div>
          <div style="font-size:12.5px;color:${COR.suave};margin-top:2px">${c.galpao_garantia} garantia (defeito) · ${c.galpao_quebrado} quebrado/sem condição no galpão · ${c.fabrica_qtd} na fábrica</div>${semCusto}</div>
        <div style="flex:1 1 200px;background:#fff;border:1px solid #dfe5ec;border-radius:12px;padding:10px 14px">
          <div style="height:150px;position:relative"><canvas id="dp-rosca" role="img" aria-label="Divisão do dinheiro parado"></canvas></div></div></div>
      <div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:12px">
        ${mini(COR.analise, "Em análise pela equipe", brl(c.analise_valor), c.analise_qtd + " reverso(s) aguardando decisão" + (c.analise_sem_custo ? ` · ${c.analise_sem_custo} sem custo` : ""))}
        ${mini(COR.galpao, "No galpão", brl(c.galpao_valor), c.galpao_qtd + " item(ns) sem poder vender")}
        ${mini(COR.fabrica, "Aguardando a fábrica", brl(c.fabrica_valor), c.fabrica_qtd + " item(ns) enviados")}
        ${mini(COR.ok, "Recuperado", brl(c.recuperado), "ressarcido, troca ou crédito")}
        ${mini(COR.perdido, "Perdido", brl(c.perdido), "fábrica não substituiu")}</div>
      <div style="background:#fff;border:1px solid #dfe5ec;border-radius:12px;padding:12px 14px">
        <div style="font-weight:700;margin-bottom:6px">Produtos parados</div>
        <div style="overflow-x:auto"><table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead><tr style="text-align:left;color:${COR.suave};font-size:11.5px"><th style="padding:6px 8px">Tipo</th><th style="padding:6px 8px">SKU / produto</th><th style="padding:6px 8px">Nº da venda</th><th style="padding:6px 8px">Onde está</th><th style="padding:6px 8px;text-align:right">Custo</th></tr></thead>
          <tbody>${linhas || `<tr><td colspan="5" style="padding:18px;text-align:center;color:${COR.suave}">Nenhum produto parado. 🎉</td></tr>`}</tbody></table></div></div></div>`;
    const cv = el.querySelector("#dp-rosca");
    const gal = (c.itens || []).filter(i => i.situacao === "galpao");
    const vGar = gal.filter(i => i.tipo_chave === "garantia").reduce((s, i) => s + (i.custo || 0), 0);
    const vQuebrado = gal.filter(i => i.tipo_chave !== "garantia").reduce((s, i) => s + (i.custo || 0), 0);
    if (cv && window.Chart && c.parado_total > 0) {
      novoGrafico(cv, { type: "doughnut",
        data: { labels: ["Garantia (galpão)", "Quebrado (galpão)", "Na fábrica"],
                datasets: [{ data: [vGar, vQuebrado, c.fabrica_valor], backgroundColor: [COR.galpao, "#c24141", COR.fabrica] }] },
        options: { responsive: true, maintainAspectRatio: false, animation: false, cutout: "58%",
                   plugins: { legend: { position: "right", labels: { boxWidth: 10, font: { size: 11 } } }, tooltip: { callbacks: { label: it => `${it.label}: ${brl(it.raw)}` } } } } });
    } else if (cv) { cv.parentElement.innerHTML = `<div style="text-align:center;color:${COR.suave};padding-top:56px;font-size:13px">Sem dinheiro parado</div>`; }
  }

  async function baixarPng(el, nome) {
    if (!window.html2canvas) throw new Error("Biblioteca de imagem não carregou.");
    const canvas = await window.html2canvas(el, { backgroundColor: "#f4f7fa", scale: 2, useCORS: true });
    const a = document.createElement("a");
    a.href = canvas.toDataURL("image/png"); a.download = nome; document.body.appendChild(a); a.click(); a.remove();
  }
  window.DinheiroParado = { graficoContas, relatorio, baixarPng, brl };
})();
