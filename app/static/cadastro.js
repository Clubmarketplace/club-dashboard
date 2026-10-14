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

  // "2d 3h", "3h 20min", "15min", "menos de 1 min" -- tempo corrido (dias e horas que o produto está levando).
  function duracao(ms) {
    const min = Math.floor(ms / 60000);
    if (!(min >= 1)) return "menos de 1 min";
    if (min < 60) return `${min}min`;
    const h = Math.floor(min / 60), m = min % 60;
    if (h < 24) return m ? `${h}h ${String(m).padStart(2, "0")}min` : `${h}h`;
    const d = Math.floor(h / 24), hr = h % 24;
    return hr ? `${d}d ${hr}h` : `${d}d`;
  }
  const HORAS_DEMORA = 24; // passou disso esperando/em andamento, o tempo fica em vermelho

  const estado = {
    itens: [], contagem: { pendente: 0, cadastrado: 0 }, status: "pendente", busca: "", falta: "",
    truncado: false, ocupado: false,
    mpOcupados: new Set(), resp: "", eu: "", papel: "",
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
  // outro clique desfaz. (15/10: tiramos o modo "Não se aplica" e o atalho "Cadastrei nos 3" --
  // agora só existe este botão por canal, com nome e cor, pra sempre ficar registrado quem
  // cadastrou cada marketplace. Produtos antigos que já tinham um canal marcado "não se aplica"
  // continuam mostrando esse estado normalmente, só não dá mais pra marcar um novo.)
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

  // Quem pode destravar um produto já cadastrado (o servidor também confere -- isto é só o espelho visual).
  function souSupervisao() { return estado.papel === "admin" || estado.papel === "supervisor"; }

  function blocoReabertura(p) {
    if (!p.reaberto_por) return "";
    return `<div class="cd-quem" style="color:var(--cmx-rust)">↻ Reaberto por ${esc(p.reaberto_por)} em ${esc(dataBr(p.reaberto_em))}${p.motivo_reabertura ? ": " + esc(p.motivo_reabertura) : ""}</div>`;
  }

  function blocoMarketplaces(p) {
    const feito = p.status === "cadastrado";
    let topo = "";
    if (feito) {
      // Bolha "✓ Concluído" em linha própria + autoria embaixo, com o nome em destaque
      // (mesmo padrão visual do "✓ Resolvido" que o Solicitações já usa).
      topo = ehMarcacaoAntiga(p)
        ? `<span class="cd-ok">✓ Concluído</span><div class="cd-quem">por <b class="cd-quem-nome">${esc(p.cadastrado_por || "—")}</b><br>${esc(dataBr(p.cadastrado_em))} (marcação antiga, sem separar por marketplace)</div>`
        : `<span class="cd-ok">✓ Concluído</span><div class="cd-quem">por <b class="cd-quem-nome">${esc(p.cadastrado_por || "—")}</b><br>${esc(dataBr(p.cadastrado_em))}</div>`;
    }
    // Travado: ninguém mexe mais nos botões por marketplace (o servidor recusa também, com 423).
    const tags = (feito || ehMarcacaoAntiga(p)) ? "" : `<div class="cd-mps">${CHAVES.map((k) => botaoMp(p, k)).join("")}</div>`;
    const quem = ehMarcacaoAntiga(p) ? "" : textoQuem(p);
    // Pendente (15/10): nenhum atalho -- só os botões por canal acima (tags), que já
    // registram quem cadastrou cada marketplace. Travado: só o Reabrir, pra admin/supervisor.
    let atalho = "";
    if (feito) {
      // Travado: só admin/supervisor vê como mexer de novo, e precisa informar o motivo.
      atalho = souSupervisao()
        ? `<button class="cd-btn cd-mini" type="button" data-reabrir="${p.id}" title="Volta pra pendente, pede o motivo e fica registrado">↻ Reabrir</button>`
        : `<small class="cd-quem">Travado — só supervisor/admin pode reabrir.</small>`;
    }
    const tempo = blocoTempo(p);
    return `<div class="cd-acao">${topo}${tags}${quem}${tempo}${blocoReabertura(p)}${atalho}</div>`;
  }

  // Quem assumiu, desde quando e quanto tempo está levando / levou.
  function blocoTempo(p) {
    const agora = Date.now();
    const chegou = p.data_nota ? new Date(p.data_nota).getTime() : NaN;
    const assumiu = p.assumido_em ? new Date(p.assumido_em).getTime() : NaN;
    const fechou = p.cadastrado_em ? new Date(p.cadastrado_em).getTime() : NaN;
    const linhas = [];
    if (p.status === "cadastrado") {
      if (!isNaN(assumiu) && !isNaN(fechou)) {
        linhas.push(`⏱ <b>Levou ${esc(duracao(fechou - assumiu))}</b> · ${esc(p.assumido_por || "—")} assumiu ${esc(dataBr(p.assumido_em))} → OK ${esc(dataBr(p.cadastrado_em))}`);
      } else if (!isNaN(chegou) && !isNaN(fechou)) {
        linhas.push(`⏱ Ninguém assumiu · da chegada até o OK: ${esc(duracao(fechou - chegou))}`);
      }
    } else {
      if (p.assumido_por) {
        const decorrido = isNaN(assumiu) ? 0 : agora - assumiu;
        const demora = decorrido > HORAS_DEMORA * 3600000 ? " cd-demora" : "";
        linhas.push(`<span class="cd-assumido">👤 ${esc(p.assumido_por)} assumiu</span> há <b class="${demora.trim()}">${esc(duracao(decorrido))}</b> <small>(desde ${esc(dataBr(p.assumido_em))})</small>`);
      } else if (!isNaN(chegou)) {
        const decorrido = agora - chegou;
        const demora = decorrido > HORAS_DEMORA * 3600000 ? ' class="cd-demora"' : "";
        linhas.push(`Sem responsável · aguardando há <b${demora}>${esc(duracao(decorrido))}</b>`);
      }
      if (p.assumido_por && !isNaN(chegou)) linhas.push(`<small>Chegou há ${esc(duracao(agora - chegou))}</small>`);
    }
    let botao = "";
    if (p.status !== "cadastrado") {
      if (!p.assumido_por) botao = `<button class="cd-btn cd-mini" type="button" data-assumir="${p.id}">🙋 Assumir</button>`;
      else if (p.assumido_por === estado.eu) botao = `<button class="cd-btn cd-mini" type="button" data-soltar="${p.id}" title="Devolve o produto para a fila, sem responsável">Soltar</button>`;
      else botao = `<button class="cd-btn cd-mini" type="button" data-assumir="${p.id}" title="${esc(p.assumido_por)} já assumiu">🙋 Assumir mesmo assim</button>`;
    }
    return `<div class="cd-tempo">${linhas.join("<br>")}${botao ? "<div>" + botao + "</div>" : ""}</div>`;
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
    let itens = estado.itens;
    if (estado.falta) itens = itens.filter((p) => p.status !== "cadastrado" && !mpDe(p, estado.falta).estado);
    if (estado.resp === "meus") itens = itens.filter((p) => p.assumido_por && p.assumido_por === estado.eu);
    if (estado.resp === "ninguem") itens = itens.filter((p) => !p.assumido_por);
    return itens;
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
    // marcado (cadastrado ou não se aplica, de antes) -> desfaz; sem marca -> cadastrado.
    // (15/10: não existe mais o modo "não se aplica" pra marcar de novo, só pra desfazer
    // uma marcação antiga que já estava assim.)
    const novo = atual ? "pendente" : "feito";
    estado.mpOcupados.add(chave);
    desenhar();
    try {
      const d = await api(`/produtos/${id}/marketplace`, { method: "POST", body: JSON.stringify({ marketplace: mp, estado: novo }) });
      estado.mpOcupados.delete(chave);
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

  async function assumir(id, assumirFlag, forcar) {
    try {
      const d = await api(`/produtos/${id}/assumir`, { method: "POST", body: JSON.stringify({ assumir: assumirFlag, forcar: !!forcar }) });
      aplicarItem(d.item);
      aviso(assumirFlag ? "Você assumiu este cadastro." : "Produto devolvido para a fila.", "ok");
    } catch (e) {
      if (assumirFlag && !forcar && /já assumiu/.test(e.message)) {
        if (window.confirm(e.message + " Assumir mesmo assim?")) return assumir(id, true, true);
        return;
      }
      aviso("Não consegui: " + e.message, "erro");
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
    const ass = e.target.closest("[data-assumir]");
    if (ass) return assumir(Number(ass.dataset.assumir), true, false);
    const solt = e.target.closest("[data-soltar]");
    if (solt) return assumir(Number(solt.dataset.soltar), false, false);
    const reabrir = e.target.closest("[data-reabrir]");
    if (reabrir) return reabrirProduto(Number(reabrir.dataset.reabrir));
  });

  // Reabrir um produto travado (só aparece o botão pra admin/supervisor -- o servidor confere de novo).
  async function reabrirProduto(id) {
    const motivo = window.prompt("Por que este produto precisa ser reaberto? (fica registrado)");
    if (motivo === null) return; // cancelou
    if (motivo.trim().length < 5) { aviso("Descreva o motivo com um pouco mais de detalhe (mínimo 5 caracteres).", "erro"); return; }
    try {
      const d = await api(`/produtos/${id}/reabrir`, { method: "POST", body: JSON.stringify({ motivo: motivo.trim() }) });
      aplicarItem(d.item);
      aviso("Produto reaberto — voltou para a fila de pendentes.", "ok");
      carregarContagem();
    } catch (e) {
      aviso("Não consegui reabrir: " + e.message, "erro");
    }
  }

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

  document.getElementById("cd-resp").addEventListener("change", (e) => { estado.resp = e.target.value; desenhar(); });
  estado.eu = document.getElementById("cd-lista").dataset.eu || "";
  estado.papel = document.getElementById("cd-lista").dataset.papel || "";

  document.getElementById("cd-atualizar").addEventListener("click", () => carregar());

  // ------------------------------------------------------- inclusão manual
  const form = document.getElementById("cd-form");
  const campo = (id) => document.getElementById(id);
  const marcaRefazer = campo("cf-refazer");
  const incluidos = []; // produtos incluídos desde que o formulário foi aberto (a lista "Incluídos agora")

  async function carregarEmpresas() {
    try {
      const d = await api("/empresas");
      campo("cf-empresas").innerHTML = d.empresas.map((n) => `<option value="${esc(n)}"></option>`).join("");
    } catch (e) { /* a lista de sugestões é só conforto */ }
  }

  function pintarRefazer() { campo("cf-refazer-btn").classList.toggle("ligado", marcaRefazer.checked); }
  marcaRefazer.addEventListener("change", pintarRefazer);

  function modo(qual) {
    form.classList.toggle("modo-colar", qual === "colar");
    form.classList.toggle("modo-um", qual === "um");
    campo("cf-aba-colar").classList.toggle("ativo", qual === "colar");
    campo("cf-aba-um").classList.toggle("ativo", qual === "um");
  }
  campo("cf-aba-colar").addEventListener("click", () => modo("colar"));
  campo("cf-aba-um").addEventListener("click", () => { modo("um"); campo("cf-sku").focus(); });

  // ---------- ler o que foi colado da planilha ----------
  // Divide em linhas e colunas respeitando aspas (descrição com vírgula/quebra de linha dentro de "...").
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

  function semAcento(x) { return String(x || "").normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase().trim(); }

  const CABECALHOS = {
    sku: ["sku", "codigo", "cod", "ref", "referencia", "cod produto", "codigo produto", "codigo do produto"],
    produto: ["produto", "descricao", "nome", "item", "descricao do produto", "nome do produto"],
    gtin: ["ean", "gtin", "ean/gtin", "codigo de barras", "cod barras", "barras"],
    quantidade: ["qtd", "qtde", "quantidade", "quant", "qtd.", "qtde."],
    custo_unitario: ["custo", "custo un", "custo un.", "custo unitario", "custo unit", "valor unit", "valor unitario", "preco", "preco unitario", "vl unit"],
  };

  function campoDoCabecalho(txt) {
    const n = semAcento(txt);
    for (const [k, lista] of Object.entries(CABECALHOS)) if (lista.includes(n)) return k;
    return null;
  }

  // "1.234,50" -> 1234.5 ; "R$ 12,5" -> 12.5 ; "1.234" -> 1234 ; "12.5" -> 12.5
  function numeroBr(x) {
    let v = String(x ?? "").replace(/R\$|\s/g, "");
    if (v === "") return 0;
    if (v.includes(",")) v = v.replace(/\./g, "").replace(",", ".");
    else if (/^\d{1,3}(\.\d{3})+$/.test(v)) v = v.replace(/\./g, "");
    const n = Number(v);
    return isFinite(n) ? n : NaN;
  }

  const ORDEM_PADRAO = {
    2: ["sku", "produto"],
    3: ["sku", "produto", "quantidade"],
    4: ["sku", "produto", "quantidade", "custo_unitario"],
    5: ["sku", "produto", "gtin", "quantidade", "custo_unitario"],
  };

  // Devolve { linhas: [{n, sku, produto, gtin, quantidade, custo_unitario, bruto, erro}], colunas: [...], cabecalho: bool }
  function lerColado(texto) {
    const tabela = dividirTabela(texto);
    if (!tabela.length) return { linhas: [], colunas: [], cabecalho: false };
    let colunas = null, inicio = 0, cabecalho = false;
    const mapeadas = tabela[0].map(campoDoCabecalho);
    if (mapeadas.filter(Boolean).length >= 2 && mapeadas.includes("sku")) { colunas = mapeadas; inicio = 1; cabecalho = true; }
    if (!colunas) {
      const largura = Math.min(5, Math.max(2, Math.max(...tabela.map((l) => l.length))));
      colunas = (ORDEM_PADRAO[largura] || ORDEM_PADRAO[5]).slice();
    }
    const linhas = [];
    for (let i = inicio; i < tabela.length; i++) {
      const cels = tabela[i];
      const r = { n: linhas.length + 1, sku: "", produto: "", gtin: "", quantidade: 0, custo_unitario: 0, bruto: cels, erro: "" };
      colunas.forEach((k, j) => {
        if (!k) return;
        const v = cels[j] ?? "";
        if (k === "quantidade" || k === "custo_unitario") r[k] = numeroBr(v);
        else r[k] = v;
      });
      if (!r.sku) r.erro = "sem SKU";
      else if (!r.produto) r.erro = "sem nome do produto";
      else if (isNaN(r.quantidade)) r.erro = "quantidade inválida";
      else if (isNaN(r.custo_unitario)) r.erro = "custo inválido";
      else if (r.quantidade < 0 || r.custo_unitario < 0) r.erro = "valor negativo";
      else if (/e\+/i.test(r.gtin)) r.erro = "EAN em notação científica (formate a coluna como texto na planilha)";
      linhas.push(r);
    }
    const vistos = new Set();
    linhas.forEach((r) => { if (!r.erro) { const k = r.sku.toLowerCase(); if (vistos.has(k)) r.erro = "SKU repetido nesta lista"; vistos.add(k); } });
    return { linhas, colunas, cabecalho };
  }

  let lidos = { linhas: [] };

  function desenharPrevia() {
    lidos = lerColado(campo("cf-colar").value);
    const caixa = campo("cf-previa");
    const bons = lidos.linhas.filter((r) => !r.erro);
    campo("cf-incluir-lote").disabled = bons.length === 0;
    campo("cf-incluir-lote").textContent = bons.length ? `Incluir ${bons.length}` : "Incluir";
    if (!lidos.linhas.length) { caixa.innerHTML = ""; return; }
    const ruins = lidos.linhas.length - bons.length;
    const resumo = `${bons.length} produto(s) prontos para incluir` + (ruins ? ` · <span style="color:var(--cmx-rust)">${ruins} com problema (não entram)</span>` : "") + (lidos.cabecalho ? " · cabeçalho reconhecido" : "");
    const linhas = lidos.linhas.slice(0, 200).map((r) => `<tr class="${r.erro ? "ruim" : ""}">
        <td>${r.n}</td><td><b>${esc(r.sku)}</b></td><td>${esc(r.produto)}</td><td>${esc(r.gtin)}</td>
        <td>${isNaN(r.quantidade) ? "?" : esc(quantidade(r.quantidade))}</td><td>${isNaN(r.custo_unitario) ? "?" : esc(moeda(r.custo_unitario))}</td>
        <td>${r.erro ? "✖ " + esc(r.erro) : "✔"}</td></tr>`).join("");
    caixa.innerHTML = `<div class="res">${resumo}</div><div class="cd-previa"><table>
      <thead><tr><th>#</th><th>SKU</th><th>Produto</th><th>EAN</th><th>Qtd</th><th>Custo un.</th><th></th></tr></thead><tbody>${linhas}</tbody></table></div>`;
  }
  campo("cf-colar").addEventListener("input", desenharPrevia);
  campo("cf-limpar").addEventListener("click", () => { campo("cf-colar").value = ""; desenharPrevia(); });

  // Anexar planilha salva como CSV: lê o arquivo e joga o texto na caixa (que mostra a prévia).
  campo("cf-arquivo").addEventListener("change", async (e) => {
    const arq = e.target.files && e.target.files[0];
    if (!arq) return;
    try {
      const buf = await arq.arrayBuffer();
      let texto;
      try { texto = new TextDecoder("utf-8", { fatal: true }).decode(buf); } catch (err) { texto = new TextDecoder("windows-1252").decode(buf); }
      campo("cf-colar").value = texto.replace(/^﻿/, "");
      desenharPrevia();
    } catch (err) {
      aviso("Não consegui ler o arquivo: " + err.message, "erro");
    }
    e.target.value = "";
  });

  // ---------- lista "Incluídos agora" ----------
  function desenharIncluidos(erros) {
    const bloco = campo("cf-incluidos");
    bloco.style.display = incluidos.length || (erros && erros.length) ? "block" : "none";
    campo("cf-incluidos-n").textContent = incluidos.length ? `(${incluidos.length})` : "";
    campo("cf-incluidos-lista").innerHTML =
      (erros || []).map((e) => `<div class="cd-inc-erro">✖ Linha ${esc(e.linha)}${e.sku ? " (" + esc(e.sku) + ")" : ""}: não entrou — ${esc(e.motivo)}</div>`).join("") +
      incluidos.slice().reverse().map((p) => `<div class="cd-inc-item"><b>${esc(p.sku)}</b><span style="flex:1">${esc(p.produto)}</span><span>${esc(quantidade(p.quantidade))} un · ${esc(moeda(p.custo_unitario))}</span></div>`).join("");
  }

  function dadosDaNota() {
    return { empresa: campo("cf-empresa").value.trim(), fornecedor: campo("cf-fornecedor").value, numero_nota: campo("cf-nota").value, refazer: marcaRefazer.checked };
  }

  async function aposIncluir() {
    estado.status = "pendente"; estado.falta = ""; campo("cd-falta").value = ""; estado.resp = ""; campo("cd-resp").value = "";
    estado.ocupado = false;
    await carregar(true);
    carregarEmpresas();
  }

  // Botão "Incluir" do modo colar: manda tudo de uma vez; o que não entrar fica na caixa pra corrigir.
  campo("cf-incluir-lote").addEventListener("click", async () => {
    const nota = dadosDaNota();
    if (!nota.empresa) { aviso("Informe a empresa antes de incluir.", "erro"); campo("cf-empresa").focus(); return; }
    const bons = lidos.linhas.filter((r) => !r.erro);
    if (!bons.length) return;
    const botao = campo("cf-incluir-lote");
    botao.disabled = true;
    try {
      const d = await api("/produtos/lote", {
        method: "POST",
        body: JSON.stringify(Object.assign({}, nota, { itens: bons.map((r) => ({ sku: r.sku, produto: r.produto, gtin: r.gtin, quantidade: r.quantidade, custo_unitario: r.custo_unitario })) })),
      });
      d.criados.forEach((p) => incluidos.push(p));
      const falhas = d.ignorados.map((x) => Object.assign({}, x, { linha: bons[x.linha - 1] ? bons[x.linha - 1].n : x.linha }));
      desenharIncluidos(falhas);
      // deixa na caixa só o que NÃO entrou (linhas com problema + as recusadas pelo servidor), pra corrigir e incluir de novo
      const recusadas = new Set(d.ignorados.map((x) => bons[x.linha - 1]));
      const ficam = lidos.linhas.filter((r) => r.erro || recusadas.has(r));
      campo("cf-colar").value = ficam.map((r) => r.bruto.join("\t")).join("\n");
      desenharPrevia();
      const refaz = nota.refazer ? " para REFAZER" : "";
      aviso(`${d.criados.length} produto(s) incluído(s)${refaz}.` + (falhas.length ? ` ${falhas.length} não entrou(aram) — veja abaixo.` : ""), falhas.length ? "aviso" : "ok");
      marcaRefazer.checked = false; pintarRefazer();
      await aposIncluir();
    } catch (e) {
      aviso("Não consegui incluir: " + e.message, "erro");
      desenharPrevia();
    }
  });

  document.getElementById("cd-novo").addEventListener("click", () => {
    const aberto = form.style.display === "block";
    form.style.display = aberto ? "none" : "block";
    if (!aberto) { carregarEmpresas(); (campo("cf-empresa").value ? campo("cf-colar") : campo("cf-empresa")).focus(); }
  });

  function finalizar() {
    form.style.display = "none";
    incluidos.length = 0; desenharIncluidos([]);
    campo("cf-colar").value = ""; desenharPrevia();
    ["cf-sku", "cf-produto", "cf-gtin"].forEach((id) => { campo(id).value = ""; });
    marcaRefazer.checked = false; pintarRefazer();
  }
  campo("cf-fechar").addEventListener("click", finalizar);

  // Modo "digitar um produto" (um de cada vez).
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const botao = campo("cf-salvar");
    const nota = dadosDaNota();
    if (!nota.empresa || !campo("cf-sku").value.trim() || !campo("cf-produto").value.trim()) {
      aviso("Preencha empresa, SKU e nome do produto.", "erro");
      return;
    }
    botao.disabled = true;
    try {
      const corpo = Object.assign({}, nota, {
        produto: campo("cf-produto").value,
        sku: campo("cf-sku").value,
        gtin: campo("cf-gtin").value,
        quantidade: Number(campo("cf-qtd").value || 0),
        custo_unitario: Number(campo("cf-custo").value || 0),
      });
      const d = await api("/produtos", { method: "POST", body: JSON.stringify(corpo) });
      incluidos.push(d.item); desenharIncluidos([]);
      aviso((corpo.refazer ? `Incluído para REFAZER: ${d.item.sku} — ${d.item.produto}.` : `Incluído: ${d.item.sku} — ${d.item.produto}.`) + (d.aviso ? " " + d.aviso : ""), d.aviso ? "aviso" : "ok");
      // limpa só o que muda de um produto pro outro; empresa, fornecedor e nota ficam pro próximo da mesma nota
      ["cf-sku", "cf-produto", "cf-gtin"].forEach((id) => { campo(id).value = ""; });
      campo("cf-qtd").value = "0";
      campo("cf-custo").value = "0";
      marcaRefazer.checked = false; pintarRefazer();
      campo("cf-sku").focus();
      await aposIncluir();
    } catch (err) {
      aviso("Não consegui incluir: " + err.message, "erro");
    } finally {
      botao.disabled = false;
    }
  });

  // Atualiza sozinha a cada minuto (só com a aba visível) -- produto novo aparece e os tempos andam, sem recarregar.
  setInterval(() => { if (!document.hidden) { if (form.style.display !== "block") carregar(true); else desenhar(); } }, 60000);

  desenharPrevia();
  carregar();
})();
