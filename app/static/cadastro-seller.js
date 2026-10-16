/**
 * "Solicitar cadastro" no painel do SELLER (15/10). Mesma grade/validação
 * da tela da equipe (app/static/cadastro.js), mas sem Conta (vem fixa do
 * servidor, pela sessão do seller) e sem lista de pendentes (o seller só
 * manda a solicitação, não acompanha a fila).
 * API: POST /api/painel/cadastro/produtos/lote (app/routers/cadastro_seller.py).
 */
(function () {
  "use strict";

  function esc(t) {
    return String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function aviso(texto, tipo) {
    const el = document.getElementById("cd-aviso");
    if (!texto) { el.style.display = "none"; return; }
    el.textContent = texto;
    el.style.display = "block";
    el.style.background = tipo === "erro" ? "var(--cmx-rust-fundo)" : tipo === "ok" ? "var(--cmx-verde-fundo)" : "var(--cmx-ambar-fundo)";
    el.style.color = tipo === "erro" ? "var(--cmx-rust)" : tipo === "ok" ? "var(--cmx-verde)" : "var(--cmx-ambar)";
  }

  async function api(caminho, opcoes) {
    const r = await fetch("/api/painel/cadastro" + caminho, Object.assign({ headers: { "Content-Type": "application/json" } }, opcoes || {}));
    const dados = await r.json().catch(() => null);
    if (!r.ok) throw new Error((dados && typeof dados.detail === "string" && dados.detail) || `status ${r.status}`);
    return dados;
  }

  function moeda(v) {
    return Number(v || 0).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
  }
  function quantidade(v) {
    const n = Number(v || 0);
    return Number.isInteger(n) ? String(n) : n.toLocaleString("pt-BR", { maximumFractionDigits: 3 });
  }

  const campo = (id) => document.getElementById(id);
  const incluidos = []; // produtos incluídos na última chamada bem-sucedida

  // ---------- mesma lógica de planilha/grade da tela da equipe ----------
  function dividirTabela(texto) {
    const t = String(texto || "").replace(/\r\n?/g, "\n");
    const delim = t.includes("\t") ? "\t" : (t.includes(";") ? ";" : ",");
    const linhas = []; let linha = [], cel = "", aspas = false;
    for (let i = 0; i < t.length; i++) {
      const c = t[i];
      if (aspas) {
        if (c === '"') { if (t[i + 1] === '"') { cel += '"'; i++; } else aspas = false; }
        else cel += c;
      } else if (c === '"' && cel === "") aspas = true;
      else if (c === delim) { linha.push(cel); cel = ""; }
      else if (c === "\n") { linha.push(cel); linhas.push(linha); linha = []; cel = ""; }
      else cel += c;
    }
    linha.push(cel); linhas.push(linha);
    return linhas.map((l) => l.map((x) => x.trim())).filter((l) => l.some((x) => x !== ""));
  }

  function numeroBr(x) {
    let v = String(x ?? "").replace(/R\$|\s/g, "");
    if (v === "") return 0;
    if (v.includes(",")) v = v.replace(/\./g, "").replace(",", ".");
    else if (/^\d{1,3}(\.\d{3})+$/.test(v)) v = v.replace(/\./g, "");
    const n = Number(v);
    return isFinite(n) ? n : NaN;
  }
  function numeroOuZero(x) { const n = numeroBr(x); return isNaN(n) ? 0 : n; }

  const CAMPOS_GRADE = ["sku", "produto", "gtin", "quantidade", "custo_unitario", "fornecedor", "numero_nota"];
  function linhaVazia() {
    return { sku: "", produto: "", gtin: "", quantidade: "", custo_unitario: "", fornecedor: "", numero_nota: "", link_fotos: "" };
  }
  function linhaTemConteudo(r) { return CAMPOS_GRADE.some((c) => String(r[c] || "").trim() !== ""); }
  // Obrigatórios por produto: SKU, nome, EAN e custo maior que zero.
  function linhaPreenchida(r) {
    return !!(
      String(r.sku || "").trim() &&
      String(r.produto || "").trim() &&
      String(r.gtin || "").trim() &&
      numeroBr(r.custo_unitario) > 0
    );
  }

  let linhasGrade = [linhaVazia(), linhaVazia(), linhaVazia()];

  // 16/10: popover do link em "fixed" (calculado pelo botão) -- a grade tem
  // overflow-x:auto, que corta um "absolute" que passe da borda vertical dela.
  function posicionarPopoverLink(botao, pop) {
    const r = botao.getBoundingClientRect();
    pop.style.top = (r.bottom + 6) + "px";
    let esquerda = r.right - 300;
    if (esquerda < 8) esquerda = 8;
    pop.style.left = esquerda + "px";
  }

  function desenharGrade() {
    campo("cf-grade-corpo").innerHTML = linhasGrade.map((r, i) => `
      <div class="cd-grade-linha" data-linha="${i}">
        <input data-campo="sku" value="${esc(r.sku)}" aria-label="SKU" />
        <input data-campo="produto" value="${esc(r.produto)}" aria-label="Produto" />
        <input data-campo="gtin" value="${esc(r.gtin)}" aria-label="EAN" />
        <input data-campo="quantidade" value="${esc(r.quantidade)}" aria-label="Quantidade" />
        <input data-campo="custo_unitario" value="${esc(r.custo_unitario)}" aria-label="Custo unitário" />
        <input data-campo="fornecedor" value="${esc(r.fornecedor)}" aria-label="Fornecedor" />
        <input data-campo="numero_nota" value="${esc(r.numero_nota)}" aria-label="Nota" />
        <div class="cd-grade-link">
          <button type="button" class="cd-link-btn${r.link_fotos ? " tem-link" : ""}" data-abrir-link="${i}" title="${r.link_fotos ? "Link salvo — clique pra ver/editar" : "Sem link — clique pra adicionar (opcional)"}">🔗</button>
          <div class="cd-link-popover" data-link-popover="${i}">
            <label>Link das fotos (Drive, etc.)</label>
            <input type="url" data-link-input="${i}" value="${esc(r.link_fotos)}" placeholder="https://drive.google.com/…" />
            <div class="cd-link-acoes">
              <button type="button" class="cd-btn cd-mini cd-link-remover-btn" data-limpar-link="${i}">Remover</button>
              <button type="button" class="cd-btn cd-mini" data-cancelar-link="${i}">Cancelar</button>
              <button type="button" class="cd-btn cd-mini cd-btn-ok" data-salvar-link="${i}">Salvar</button>
            </div>
          </div>
        </div>
        <button type="button" class="cd-grade-remover" data-remover="${i}" title="Remover linha" aria-label="Remover linha">✕</button>
      </div>`).join("");
    atualizarBotaoFinalizar();
  }

  function espalharColado(linhaInicio, colInicio, texto) {
    const tabela = dividirTabela(texto);
    if (!tabela.length) return;
    tabela.forEach((cels, r) => {
      const alvo = linhaInicio + r;
      while (alvo >= linhasGrade.length) linhasGrade.push(linhaVazia());
      cels.forEach((valor, c) => {
        const col = colInicio + c;
        if (col < CAMPOS_GRADE.length) linhasGrade[alvo][CAMPOS_GRADE[col]] = valor;
      });
    });
  }

  campo("cf-grade-corpo").addEventListener("input", (e) => {
    const alvo = e.target.closest("input[data-campo]");
    if (!alvo) return;
    linhasGrade[Number(alvo.closest("[data-linha]").dataset.linha)][alvo.dataset.campo] = alvo.value;
    atualizarBotaoFinalizar();
  });
  campo("cf-grade-corpo").addEventListener("paste", (e) => {
    const alvo = e.target.closest("input[data-campo]");
    if (!alvo) return;
    const texto = (e.clipboardData || window.clipboardData).getData("text");
    if (!texto) return;
    e.preventDefault();
    espalharColado(Number(alvo.closest("[data-linha]").dataset.linha), CAMPOS_GRADE.indexOf(alvo.dataset.campo), texto);
    desenharGrade();
  });
  campo("cf-grade-corpo").addEventListener("click", (e) => {
    const rem = e.target.closest("[data-remover]");
    if (rem) {
      linhasGrade.splice(Number(rem.dataset.remover), 1);
      if (!linhasGrade.length) linhasGrade.push(linhaVazia());
      desenharGrade();
      return;
    }
    const abrirLink = e.target.closest("[data-abrir-link]");
    if (abrirLink) {
      e.stopPropagation();
      const i = abrirLink.dataset.abrirLink;
      const pop = campo("cf-grade-corpo").querySelector(`.cd-link-popover[data-link-popover="${i}"]`);
      const jaAberto = pop.classList.contains("aberto");
      campo("cf-grade-corpo").querySelectorAll(".cd-link-popover.aberto").forEach((p) => p.classList.remove("aberto"));
      if (!jaAberto) {
        posicionarPopoverLink(abrirLink, pop);
        pop.classList.add("aberto");
        pop.querySelector("input").focus();
      }
      return;
    }
    const salvarLink = e.target.closest("[data-salvar-link]");
    if (salvarLink) {
      const i = Number(salvarLink.dataset.salvarLink);
      linhasGrade[i].link_fotos = campo("cf-grade-corpo").querySelector(`[data-link-input="${i}"]`).value.trim();
      desenharGrade();
      return;
    }
    const cancelarLink = e.target.closest("[data-cancelar-link]");
    if (cancelarLink) {
      campo("cf-grade-corpo").querySelectorAll(".cd-link-popover.aberto").forEach((p) => p.classList.remove("aberto"));
      return;
    }
    const limparLink = e.target.closest("[data-limpar-link]");
    if (limparLink) {
      linhasGrade[Number(limparLink.dataset.limparLink)].link_fotos = "";
      desenharGrade();
      return;
    }
  });
  document.addEventListener("click", (e) => {
    if (e.target.closest(".cd-link-popover") || e.target.closest("[data-abrir-link]")) return;
    document.querySelectorAll(".cd-link-popover.aberto").forEach((p) => p.classList.remove("aberto"));
  });
  campo("cf-adicionar-linha").addEventListener("click", () => { linhasGrade.push(linhaVazia()); desenharGrade(); });

  campo("cf-arquivo").addEventListener("change", async (e) => {
    const arq = e.target.files && e.target.files[0];
    if (!arq) return;
    try {
      const buf = await arq.arrayBuffer();
      let texto;
      try { texto = new TextDecoder("utf-8", { fatal: true }).decode(buf); } catch (err) { texto = new TextDecoder("windows-1252").decode(buf); }
      const tabela = dividirTabela(texto.replace(/^﻿/, ""));
      linhasGrade = tabela.length
        ? tabela.map((cels) => { const r = linhaVazia(); CAMPOS_GRADE.forEach((c, i) => { r[c] = cels[i] || ""; }); return r; })
        : [linhaVazia(), linhaVazia(), linhaVazia()];
      desenharGrade();
    } catch (err) {
      aviso("Não consegui ler o arquivo: " + err.message, "erro");
    }
    e.target.value = "";
  });

  function limparGrade() {
    linhasGrade = [linhaVazia(), linhaVazia(), linhaVazia()];
    desenharGrade();
  }

  // ---------- "é kit?" + canal(is) -- obrigatórios, igual na tela da equipe ----------
  let kitEscolhido = null; // null = ainda não escolheu | true | false
  const plataformasEscolhidas = new Set();

  document.querySelectorAll(".cf-kit").forEach((b) => {
    b.addEventListener("click", () => {
      kitEscolhido = b.dataset.kit === "sim";
      document.querySelectorAll(".cf-kit").forEach((x) => x.classList.toggle("ativo", x === b));
      atualizarBotaoFinalizar();
    });
  });
  document.querySelectorAll(".cf-canal").forEach((b) => {
    b.addEventListener("click", () => {
      const chave = b.dataset.canal;
      if (plataformasEscolhidas.has(chave)) plataformasEscolhidas.delete(chave); else plataformasEscolhidas.add(chave);
      b.classList.toggle("ativo");
      atualizarBotaoFinalizar();
    });
  });
  function limparSelecoes() {
    kitEscolhido = null;
    plataformasEscolhidas.clear();
    document.querySelectorAll(".cf-kit, .cf-canal").forEach((b) => b.classList.remove("ativo"));
  }

  // ---------- validação / Finalizar ----------
  function faltando() {
    const f = [];
    if (!linhasGrade.some(linhaPreenchida)) f.push("pelo menos 1 produto completo (SKU, nome, EAN e custo maior que zero)");
    if (!plataformasEscolhidas.size) f.push("pelo menos 1 canal");
    if (kitEscolhido === null) f.push("se é kit");
    return f;
  }
  function atualizarBotaoFinalizar() {
    const f = faltando();
    campo("cf-finalizar").disabled = f.length > 0;
    campo("cf-finalizar-ajuda").textContent = f.length ? `Falta: ${f.join(", ")}.` : 'Nada é gravado até clicar aqui.';
  }

  function desenharIncluidos(erros) {
    const bloco = campo("cf-incluidos");
    bloco.style.display = incluidos.length || (erros && erros.length) ? "block" : "none";
    campo("cf-incluidos-n").textContent = incluidos.length ? `(${incluidos.length})` : "";
    campo("cf-incluidos-lista").innerHTML =
      (erros || []).map((e) => `<div class="cd-inc-erro">✖ Linha ${esc(e.linha)}${e.sku ? " (" + esc(e.sku) + ")" : ""}: não entrou — ${esc(e.motivo)}</div>`).join("") +
      incluidos.slice().reverse().map((p) => `<div class="cd-inc-item"><b>${esc(p.sku)}</b><span style="flex:1">${esc(p.produto)}</span><span>${esc(quantidade(p.quantidade))} un · ${esc(moeda(p.custo_unitario))}</span></div>`).join("");
  }

  // "Finalizar solicitação": manda toda linha com algo preenchido numa chamada só.
  // Quem recusa linha incompleta/inválida é o servidor, por item, sem travar as demais.
  campo("cf-finalizar").addEventListener("click", async () => {
    if (faltando().length) { atualizarBotaoFinalizar(); return; } // defesa -- o botão já estaria desabilitado
    const itensValidos = linhasGrade.filter(linhaTemConteudo);
    const botao = campo("cf-finalizar");
    botao.disabled = true;
    try {
      const d = await api("/produtos/lote", {
        method: "POST",
        body: JSON.stringify({
          kit: kitEscolhido,
          plataformas: Array.from(plataformasEscolhidas),
          itens: itensValidos.map((r) => ({
            sku: r.sku, produto: r.produto, gtin: r.gtin,
            quantidade: numeroOuZero(r.quantidade), custo_unitario: numeroOuZero(r.custo_unitario),
            fornecedor: r.fornecedor, numero_nota: r.numero_nota, link_fotos: r.link_fotos,
          })),
        }),
      });
      d.criados.forEach((p) => incluidos.push(p));
      desenharIncluidos(d.ignorados);
      aviso(
        `${d.criados.length} produto(s) enviado(s) pro cadastro.` + (d.ignorados.length ? ` ${d.ignorados.length} não entrou(aram) — corrija e finalize de novo.` : ""),
        d.ignorados.length ? "aviso" : "ok"
      );
      if (d.ignorados.length) {
        const recusadas = new Set(d.ignorados.map((x) => x.linha));
        linhasGrade = itensValidos.filter((_, i) => recusadas.has(i + 1));
        desenharGrade();
      } else {
        limparGrade();
        limparSelecoes();
      }
    } catch (e) {
      aviso("Não consegui enviar a solicitação: " + e.message, "erro");
    } finally {
      atualizarBotaoFinalizar();
    }
  });

  campo("cf-limpar").addEventListener("click", () => {
    incluidos.length = 0; desenharIncluidos([]);
    limparGrade();
    limparSelecoes();
    aviso("");
  });

  desenharGrade();
})();
