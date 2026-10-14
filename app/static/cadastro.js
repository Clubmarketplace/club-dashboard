/**
 * Tela "Cadastro": produtos novos (do Operador Logístico ou incluídos à mão),
 * agrupados por empresa. Cada produto tem 3 marcações — Mercado Livre, TikTok
 * e Shopee — e o sistema guarda quem fez cada uma. Fica OK quando os 3 estão
 * resolvidos (cadastrado ou "não se aplica").
 * API: /api/cadastro/* (ver app/routers/cadastro_produtos.py).
 */
(function () {
  "use strict";

  const MP = {
    ml: { curto: "ML", longo: "Mercado Livre" },
    tiktok: { curto: "TikTok", longo: "TikTok" },
    shopee: { curto: "Shopee", longo: "Shopee" },
  };
  const CHAVES = ["ml", "tiktok", "shopee"];

  const estado = {
    itens: [], contagem: { pendente: 0, cadastrado: 0 }, status: "pendente", busca: "", falta: "",
    truncado: false, ocupado: false,
    naModo: new Set(), mpOcupados: new Set(),
  };

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

  // As datas chegam em UTC (com "Z"); mostra no horário de Brasília.
  function dataBr(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    return d.toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  // Mesma ideia da "chave da conta" do servidor: "JC Machado" e "JC MACHADO " são a mesma empresa.
  function chaveEmpresa(nome) {
    return String(nome || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim().replace(/\s+/g, " ");
  }

  function mpDe(p, k) {
    return (p.marketplaces && p.marketplaces[k]) || { estado: null, por: null, em: null };
  }

  // Marcação antiga: produto marcado "cadastrado" antes de existir a marcação por marketplace.
  function ehMarcacaoAntiga(p) {
    return p.status === "cadastrado" && CHAVES.every((k) => !mpDe(p, k).estado);
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

  // Um clique marca o canal como cadastrado (cor do canal: ML amarelo, TikTok preto, Shopee vermelho);
  // outro clique desfaz. "Não se aplica" é à parte (botão da linha) e também se desfaz com um clique.
  function botaoMp(p, k) {
    const m = mpDe(p, k);
    const nome = MP[k];
    let classe = `cd-mp cd-mp-${k}`, rotulo = nome.longo, dica;
    if (m.estado === "feito") {
      classe += " cd-mp-feito"; rotulo = "✔ " + nome.longo;
      dica = `${nome.longo}: cadastrado por ${m.por || "—"} em ${dataBr(m.em)}. Clique para desfazer.`;
    } else if (m.estado === "nao_se_aplica") {
      classe += " cd-mp-na";
      dica = `${nome.longo}: não se aplica (marcado por ${m.por || "—"} em ${dataBr(m.em)}). Clique para desfazer.`;
    } else if (estado.naModo.has(p.id)) {
      classe += " cd-mp-escolher";
      dica = `Clique para marcar que ${nome.longo} NÃO se aplica a este produto.`;
    } else {
      dica = `${nome.longo}: falta cadastrar. Clique quando cadastrar.`;
    }
    const ocupado = estado.mpOcupados.has(p.id + ":" + k) ? " disabled" : "";
    return `<button type="button" class="${classe}" data-mp="${k}" data-id="${p.id}" title="${esc(dica)}"${ocupado}>${esc(rotulo)}</button>`;
  }

  // "ML Ana · TikTok Bruno" — quem fez cada marketplace, sempre visível.
  function textoQuem(p) {
    const partes = [];
    CHAVES.forEach((k) => {
      const m = mpDe(p, k);
      if (m.estado === "feito" && m.por) partes.push(`${MP[k].curto} ${esc(m.por)}`);
    });
    return partes.length ? `<div class="cd-quem">${partes.join(" · ")}</div>` : "";
  }

  function blocoMarketplaces(p) {
    const feito = p.status === "cadastrado";
    let topo = "";
    if (feito) {
      topo = ehMarcacaoAntiga(p)
        ? `<span class="cd-ok">✔ OK</span> <small class="cd-quem">por ${esc(p.cadastrado_por || "—")} · ${esc(dataBr(p.cadastrado_em))} (marcação antiga, sem separar por marketplace)</small>`
        : `<span class="cd-ok">✔ OK</span> <small class="cd-quem">fechado por ${esc(p.cadastrado_por || "—")} · ${esc(dataBr(p.cadastrado_em))}</small>`;
    }
    const tags = ehMarcacaoAntiga(p) ? "" : `<div class="cd-mps">${CHAVES.map((k) => botaoMp(p, k)).join("")}</div>`;
    const quem = ehMarcacaoAntiga(p) ? "" : textoQuem(p);
    const emModo = estado.naModo.has(p.id);
    const btnNa = feito ? "" : ` <button class="cd-btn cd-mini" type="button" data-namodo="${p.id}" title="Para o canal em que este produto não vai ser cadastrado">${emModo ? "Cancelar" : "Não se aplica…"}</button>`;
    const atalho = (feito
      ? `<button class="cd-btn cd-mini" type="button" data-desfazer="${p.id}">Desfazer tudo</button>`
      : `<button class="cd-btn cd-mini" type="button" data-todos="${p.id}" title="Marca como cadastrado o que ainda falta, em nome de quem clicar">✔ Cadastrei nos 3</button>`) + btnNa;
    const dicaModo = emModo ? `<div class="cd-quem" style="color:var(--cmx-rust)">Clique no canal que <b>não se aplica</b> a este produto.</div>` : "";
    return `<div class="cd-acao">${topo}${tags}${quem}${dicaModo}${atalho}</div>`;
  }

  function linhaProduto(p) {
    const feito = p.status === "cadastrado";
    const manual = (p.origem === "manual"
      ? `<div><span class="cd-selo-manual" title="Incluído à mão por ${esc(p.criado_por || "—")}">incluído à mão${p.criado_por ? " · " + esc(p.criado_por) : ""}</span></div>`
      : "") + (p.refazer ? `<div><span class="cd-selo-refazer" title="Este produto já foi cadastrado antes e está sendo refeito">↻ REFAZER</span></div>` : "");
    const nota = `<div class="cd-nota"><b>${p.numero_nota ? "Nota " + esc(p.numero_nota) : "—"}</b><small>${esc(p.fornecedor || "sem fornecedor")} · ${esc(dataBr(p.data_nota))}</small></div>`;
    return `<div class="cd-linha${feito ? " feito" : ""}" data-linha="${p.id}">
      <div><div class="cd-sku">${esc(p.sku)}</div>${manual}</div>
      <div class="cd-produto" title="${esc(p.produto)}">${esc(p.produto || "—")}</div>
      <div class="cd-mono" data-rot="EAN">${esc(p.gtin || "não informado")}</div>
      <div data-rot="Qtd">${esc(quantidade(p.quantidade))}</div>
      <div data-rot="Custo un.">${esc(moeda(p.custo_unitario))}</div>
      ${nota}
      ${blocoMarketplaces(p)}
    </div>`;
  }

  function itensVisiveis() {
    if (!estado.falta) return estado.itens;
    return estado.itens.filter((p) => p.status !== "cadastrado" && !mpDe(p, estado.falta).estado);
  }

  function desenhar() {
    desenharChips();

    const itens = itensVisiveis();
    // agrupa por empresa (a lista já vem ordenada pelo servidor)
    const grupos = [];
    const porChave = {};
    itens.forEach((p) => {
      const k = chaveEmpresa(p.empresa);
      if (!porChave[k]) { porChave[k] = { nome: p.empresa, itens: [] }; grupos.push(porChave[k]); }
      porChave[k].itens.push(p);
    });

    document.getElementById("cd-contador").textContent =
      `${itens.length} produto(s) em ${grupos.length} empresa(s)`;

    const alvo = document.getElementById("cd-lista");
    if (!grupos.length) {
      let msg;
      if (estado.busca.trim()) msg = "Nada encontrado com essa busca.";
      else if (estado.falta) msg = `Nenhum produto com ${MP[estado.falta].longo} faltando.`;
      else if (estado.status === "pendente") msg = "Nenhum produto esperando cadastro. Quando uma nota com produto novo for lançada no Operador, ele aparece aqui sozinho — ou use “+ Incluir produto”.";
      else if (estado.status === "cadastrado") msg = "Nenhum produto marcado como cadastrado ainda.";
      else msg = "Nenhum produto recebido ainda.";
      alvo.innerHTML = `<div class="cd-vazio">${esc(msg)}</div>`;
      return;
    }

    alvo.innerHTML = grupos.map((g) => {
      const pendentes = g.itens.filter((p) => p.status !== "cadastrado").length;
      const badge = pendentes
        ? `<span class="cd-badge">${pendentes} pendente(s)</span>`
        : `<span class="cd-badge zero">tudo OK</span>`;
      return `<section class="cd-empresa">
        <div class="cd-empresa-topo"><h2>${esc(g.nome)}</h2>${badge}</div>
        <div class="cd-tabela">
          <div class="cd-linha cab"><span>SKU</span><span>Produto</span><span>EAN</span><span>Qtd</span><span>Custo un.</span><span>Nota</span><span>Cadastro por marketplace</span></div>
          ${g.itens.map(linhaProduto).join("")}
        </div>
      </section>`;
    }).join("") + (estado.truncado ? `<p style="color:var(--cmx-texto-suave);font-size:13px">Mostrando só os primeiros produtos da lista — use a busca para afinar.</p>` : "");
  }

  // Troca o item na lista pelo que o servidor devolveu (a linha mostra o resultado na hora, mesmo que já não seja "pendente").
  function aplicarItem(item) {
    const i = estado.itens.findIndex((x) => x.id === item.id);
    if (i >= 0) estado.itens[i] = item;
    desenhar();
  }

  async function alternarMp(id, mp) {
    const p = estado.itens.find((x) => x.id === id);
    if (!p) return;
    const chave = id + ":" + mp;
    if (estado.mpOcupados.has(chave)) return;
    const atual = mpDe(p, mp).estado || "";
    // marcado (cadastrado ou não se aplica) -> desfaz; sem marca -> cadastrado (ou "não se aplica" se a linha estiver nesse modo)
    const novo = atual ? "pendente" : (estado.naModo.has(id) ? "nao_se_aplica" : "feito");
    estado.mpOcupados.add(chave);
    desenhar();
    try {
      const d = await api(`/produtos/${id}/marketplace`, { method: "POST", body: JSON.stringify({ marketplace: mp, estado: novo }) });
      estado.mpOcupados.delete(chave);
      if (novo === "nao_se_aplica") estado.naModo.delete(id);
      aplicarItem(d.item);
      if (d.item.status === "cadastrado") aviso("Produto OK: os 3 canais estão resolvidos.", "ok");
      else aviso("");
      carregarContagem();
    } catch (e) {
      estado.mpOcupados.delete(chave);
      desenhar();
      aviso("Não consegui marcar: " + e.message, "erro");
    }
  }

  async function atalhoTodos(id, cadastrado, botao) {
    botao.disabled = true;
    try {
      const d = await api(`/produtos/${id}/status`, { method: "POST", body: JSON.stringify({ cadastrado }) });
      aplicarItem(d.item);
      aviso(cadastrado ? "Marcado nos 3 marketplaces." : "Voltou para pendente.", "ok");
      carregarContagem();
    } catch (e) {
      aviso("Não consegui alterar: " + e.message, "erro");
      botao.disabled = false;
    }
  }

  // Atualiza só os números das abas (Pendentes/Cadastrados) depois de uma marcação.
  async function carregarContagem() {
    try {
      const d = await api("/produtos?status=pendente&limite=1");
      estado.contagem = d.contagem;
      desenharChips();
    } catch (e) { /* sem problema: o ciclo de 1 minuto corrige */ }
  }

  // ---------------------------------------------------------------- eventos
  document.getElementById("cd-lista").addEventListener("click", (e) => {
    const mp = e.target.closest("[data-mp]");
    if (mp) return alternarMp(Number(mp.dataset.id), mp.dataset.mp);
    const na = e.target.closest("[data-namodo]");
    if (na) {
      const id = Number(na.dataset.namodo);
      if (estado.naModo.has(id)) estado.naModo.delete(id); else estado.naModo.add(id);
      return desenhar();
    }
    const todos = e.target.closest("[data-todos]");
    if (todos) return atalhoTodos(Number(todos.dataset.todos), true, todos);
    const desfazer = e.target.closest("[data-desfazer]");
    if (desfazer) {
      if (!window.confirm("Desfazer as marcações dos 3 marketplaces deste produto?")) return;
      return atalhoTodos(Number(desfazer.dataset.desfazer), false, desfazer);
    }
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

  document.getElementById("cd-falta").addEventListener("change", (e) => {
    estado.falta = e.target.value;
    if (estado.falta && estado.status !== "pendente") { estado.status = "pendente"; carregar(); } else desenhar();
  });

  document.getElementById("cd-atualizar").addEventListener("click", () => carregar());

  // ------------------------------------------------------- inclusão manual
  const form = document.getElementById("cd-form");
  const campo = (id) => document.getElementById(id);

  async function carregarEmpresas() {
    try {
      const d = await api("/empresas");
      campo("cf-empresas").innerHTML = d.empresas.map((n) => `<option value="${esc(n)}"></option>`).join("");
    } catch (e) { /* a lista de sugestões é só conforto */ }
  }

  // Botão "↻ Refazer": liga/desliga a marca. Só isso -- sem ligação com o cadastro anterior.
  const marcaRefazer = campo("cf-refazer");
  function pintarRefazer() { campo("cf-refazer-btn").classList.toggle("ligado", marcaRefazer.checked); }
  marcaRefazer.addEventListener("change", pintarRefazer);

  document.getElementById("cd-novo").addEventListener("click", () => {
    const aberto = form.style.display === "block";
    form.style.display = aberto ? "none" : "block";
    if (!aberto) { carregarEmpresas(); campo(campo("cf-empresa").value ? "cf-sku" : "cf-empresa").focus(); }
  });
  document.getElementById("cf-fechar").addEventListener("click", () => { form.style.display = "none"; });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const botao = campo("cf-salvar");
    botao.disabled = true;
    try {
      const corpo = {
        empresa: campo("cf-empresa").value,
        fornecedor: campo("cf-fornecedor").value,
        numero_nota: campo("cf-nota").value,
        sku: campo("cf-sku").value,
        produto: campo("cf-produto").value,
        gtin: campo("cf-gtin").value,
        quantidade: Number(campo("cf-qtd").value || 0),
        custo_unitario: Number(campo("cf-custo").value || 0),
        refazer: marcaRefazer.checked,
      };
      const d = await api("/produtos", { method: "POST", body: JSON.stringify(corpo) });
      aviso((corpo.refazer ? `Incluído para REFAZER: ${d.item.sku} — ${d.item.produto}.` : `Incluído: ${d.item.sku} — ${d.item.produto}.`) + (d.aviso ? " " + d.aviso : ""), d.aviso ? "aviso" : "ok");
      // limpa só o que muda de um produto pro outro; empresa, fornecedor e nota ficam pro próximo da mesma nota
      ["cf-sku", "cf-produto", "cf-gtin"].forEach((id) => { campo(id).value = ""; });
      campo("cf-qtd").value = "0";
      campo("cf-custo").value = "0";
      marcaRefazer.checked = false; pintarRefazer();
      campo("cf-sku").focus();
      estado.status = "pendente"; estado.falta = ""; campo("cd-falta").value = "";
      estado.ocupado = false;
      await carregar(true);
      carregarEmpresas();
    } catch (err) {
      aviso("Não consegui incluir: " + err.message, "erro");
    } finally {
      botao.disabled = false;
    }
  });

  // Atualiza sozinha a cada minuto (só com a aba visível) -- produto novo aparece sem precisar recarregar.
  setInterval(() => { if (!document.hidden && form.style.display !== "block") carregar(true); }, 60000);

  carregar();
})();
