/**
 * Tela "Cadastro": produtos novos das notas fiscais do Operador Logístico,
 * agrupados por empresa, esperando o time de Cadastro.
 * API: /api/cadastro/produtos (ver app/routers/cadastro_produtos.py).
 */
(function () {
  "use strict";

  const estado = { itens: [], contagem: { pendente: 0, cadastrado: 0 }, status: "pendente", busca: "", truncado: false, ocupado: false };

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
    const r = await fetch("/api/cadastro" + caminho, Object.assign({ headers: { "Content-Type": "application/json" } }, opcoes || {}));
    const dados = await r.json().catch(() => null);
    if (!r.ok) throw new Error((dados && dados.detail) || `status ${r.status}`);
    return dados;
  }

  function moeda(v) {
    return Number(v || 0).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
  }

  function quantidade(v) {
    const n = Number(v || 0);
    return Number.isInteger(n) ? String(n) : n.toLocaleString("pt-BR", { maximumFractionDigits: 3 });
  }

  // As datas chegam em UTC (com "Z"); mostra no horário de Brasília.
  function dataBr(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  // Mesma ideia da "chave da conta" do servidor: "JC Machado" e "JC MACHADO " são a mesma empresa.
  function chaveEmpresa(nome) {
    return String(nome || "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toLowerCase().trim().replace(/\s+/g, " ");
  }

  async function carregar(silencioso) {
    if (estado.ocupado) return;
    estado.ocupado = true;
    try {
      const q = new URLSearchParams({ status: estado.status, busca: estado.busca });
      const d = await api("/produtos?" + q.toString());
      estado.itens = d.itens;
      estado.contagem = d.contagem;
      estado.truncado = d.truncado;
      if (!silencioso) aviso("");
      desenhar();
    } catch (e) {
      if (!silencioso) aviso("Não consegui carregar a lista: " + e.message, "erro");
    } finally {
      estado.ocupado = false;
    }
  }

  function desenharChips() {
    const c = estado.contagem;
    const rotulos = { pendente: `Pendentes (${c.pendente})`, cadastrado: `Cadastrados (${c.cadastrado})`, todos: `Todos (${c.pendente + c.cadastrado})` };
    document.querySelectorAll(".cd-chip").forEach((b) => {
      b.textContent = rotulos[b.dataset.status];
      b.classList.toggle("ativo", b.dataset.status === estado.status);
    });
  }

  function linhaProduto(p) {
    const feito = p.status === "cadastrado";
    const nota = `<div class="cd-nota"><b>${p.numero_nota ? "Nota " + esc(p.numero_nota) : "—"}</b><small>${esc(p.fornecedor || "sem fornecedor")} · ${esc(dataBr(p.data_nota))}</small></div>`;
    const acao = feito
      ? `<div class="cd-feito"><small>✔ ${esc(p.cadastrado_por || "—")} · ${esc(dataBr(p.cadastrado_em))}</small><br><button class="cd-btn" type="button" data-desfazer="${p.id}">Desfazer</button></div>`
      : `<button class="cd-btn cd-btn-ok" type="button" data-marcar="${p.id}">Marcar cadastrado</button>`;
    return `<div class="cd-linha${feito ? " feito" : ""}">
      <div class="cd-sku">${esc(p.sku)}</div>
      <div class="cd-produto" title="${esc(p.produto)}">${esc(p.produto || "—")}</div>
      <div class="cd-mono">${esc(p.gtin || "não informado")}</div>
      <div>${esc(quantidade(p.quantidade))}</div>
      <div>${esc(moeda(p.custo_unitario))}</div>
      ${nota}
      <div class="cd-acao">${acao}</div>
    </div>`;
  }

  function desenhar() {
    desenharChips();

    // agrupa por empresa (a lista já vem ordenada pelo servidor)
    const grupos = [];
    const porChave = {};
    estado.itens.forEach((p) => {
      const k = chaveEmpresa(p.empresa);
      if (!porChave[k]) { porChave[k] = { nome: p.empresa, itens: [] }; grupos.push(porChave[k]); }
      porChave[k].itens.push(p);
    });

    document.getElementById("cd-contador").textContent =
      `${estado.itens.length} produto(s) em ${grupos.length} empresa(s)`;

    const alvo = document.getElementById("cd-lista");
    if (!grupos.length) {
      let msg;
      if (estado.busca.trim()) msg = "Nada encontrado com essa busca.";
      else if (estado.status === "pendente") msg = "Nenhum produto esperando cadastro. Quando uma nota com produto novo for lançada no Operador, ele aparece aqui sozinho.";
      else if (estado.status === "cadastrado") msg = "Nenhum produto marcado como cadastrado ainda.";
      else msg = "Nenhum produto recebido ainda.";
      alvo.innerHTML = `<div class="cd-vazio">${esc(msg)}</div>`;
      return;
    }

    alvo.innerHTML = grupos.map((g) => {
      const pendentes = g.itens.filter((p) => p.status !== "cadastrado").length;
      const badge = pendentes
        ? `<span class="cd-badge">${pendentes} pendente(s)</span>`
        : `<span class="cd-badge zero">tudo cadastrado</span>`;
      return `<section class="cd-empresa">
        <div class="cd-empresa-topo"><h2>${esc(g.nome)}</h2>${badge}</div>
        <div class="cd-tabela">
          <div class="cd-linha cab"><span>SKU</span><span>Produto</span><span>EAN</span><span>Qtd</span><span>Custo un.</span><span>Nota</span><span></span></div>
          ${g.itens.map(linhaProduto).join("")}
        </div>
      </section>`;
    }).join("") + (estado.truncado ? `<p style="color:var(--cmx-texto-suave);font-size:13px">Mostrando só os primeiros produtos da lista — use a busca para afinar.</p>` : "");
  }

  async function alterar(id, cadastrado, botao) {
    botao.disabled = true;
    try {
      await api(`/produtos/${id}/status`, { method: "POST", body: JSON.stringify({ cadastrado }) });
      aviso(cadastrado ? "Marcado como cadastrado." : "Voltou para pendente.", "ok");
      estado.ocupado = false;
      await carregar(true);
    } catch (e) {
      aviso("Não consegui alterar: " + e.message, "erro");
      botao.disabled = false;
    }
  }

  // ---------------------------------------------------------------- eventos
  document.getElementById("cd-lista").addEventListener("click", (e) => {
    const marcar = e.target.closest("[data-marcar]");
    if (marcar) return alterar(Number(marcar.dataset.marcar), true, marcar);
    const desfazer = e.target.closest("[data-desfazer]");
    if (desfazer) return alterar(Number(desfazer.dataset.desfazer), false, desfazer);
  });

  document.querySelectorAll(".cd-chip").forEach((b) => {
    b.addEventListener("click", () => { estado.status = b.dataset.status; carregar(); });
  });

  let esperaBusca = null;
  document.getElementById("cd-busca").addEventListener("input", (e) => {
    estado.busca = e.target.value;
    clearTimeout(esperaBusca);
    esperaBusca = setTimeout(() => carregar(), 300);
  });

  document.getElementById("cd-atualizar").addEventListener("click", () => carregar());

  // Atualiza sozinha a cada minuto (só com a aba visível) -- produto novo aparece sem precisar recarregar.
  setInterval(() => { if (!document.hidden) carregar(true); }, 60000);

  carregar();
})();
