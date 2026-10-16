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
    mpOcupados: new Set(), resp: "", eu: "", papel: "", conta: "",
    // 16/10: ids em modo "escolhendo plataformas" + o que cada um tem selecionado
    // nesse momento (antes de salvar). Só existe enquanto o bloco está aberto na tela.
    escolhendoPlataformas: new Set(), selecaoPlataformas: {},
    confirmOcupados: new Set(),
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
      // 16/10 (correção): pedido e ainda pendente -- fica em evidência em laranja
      // (cor neutra de "falta fazer"), desde o momento que foi marcado como pedido,
      // não só depois de alguém assumir. Só troca pra cor da própria plataforma
      // depois que for de fato marcado "feito" (cd-mp-feito, acima).
      classe += " cd-mp-pedido";
      dica = p.assumido_por
        ? `${nome.longo}: falta cadastrar (${p.assumido_por} está com este produto). Clique quando cadastrar.`
        : `${nome.longo}: falta cadastrar. Clique quando cadastrar.`;
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
  // 16/10: quem pode definir as plataformas de um produto automático (admin/supervisor/
  // gerente_setor -- igual _supervisao_logado no servidor; "gerente_setor" só enxerga esta
  // tela se já tiver o setor "cadastro", então não precisa conferir de novo aqui).
  function souSupervisaoOuGerente() { return souSupervisao() || estado.papel === "gerente_setor"; }

  // 16/10: todos os 3 marketplaces resolvidos (feito ou não se aplica) -- é o sinal
  // pra liberar o Confirmar. Diferente de "status === cadastrado": resolvido ainda
  // não travou, só fica pronto esperando o Confirmar.
  function todosResolvidos(p) { return CHAVES.every((k) => !!mpDe(p, k).estado); }
  // Nenhum dos 3 foi tocado ainda -- é quando dá pra definir quais foram pedidos.
  function nenhumTocado(p) { return CHAVES.every((k) => !mpDe(p, k).estado); }
  // 16/10 (correção): produto automático sem nada tocado ainda -- ninguém pode
  // assumir nem marcar canal até o supervisor/gerente escolher quais marketplaces
  // essa solicitação precisa (o servidor também recusa, com 409 -- isto é só o
  // espelho visual). Fecha a brecha de o operador clicar direto nos botões de
  // canal antes de alguém definir.
  function aguardandoPlataformas(p) { return p.origem === "automatico" && nenhumTocado(p); }

  function blocoReabertura(p) {
    if (!p.reaberto_por) return "";
    return `<div class="cd-quem" style="color:var(--cmx-rust)">↻ Reaberto por ${esc(p.reaberto_por)} em ${esc(dataBr(p.reaberto_em))}${p.motivo_reabertura ? ": " + esc(p.motivo_reabertura) : ""}</div>`;
  }

  // 16/10: supervisor/gerente escolhe quais marketplaces essa solicitação precisa,
  // ANTES de alguém assumir -- só aparece quando nenhum dos 3 foi tocado ainda, pra
  // quem tem permissão. O que não for marcado aqui já vira "não se aplica" sozinho.
  function blocoDefinirPlataformas(p) {
    if (!souSupervisaoOuGerente() || !nenhumTocado(p)) return "";
    if (!estado.escolhendoPlataformas.has(p.id)) {
      return `<button class="cd-btn cd-mini" type="button" data-abrir-plataformas="${p.id}">🏷 Definir plataformas</button>`;
    }
    const sel = estado.selecaoPlataformas[p.id] || new Set();
    const chips = CHAVES.map((k) => {
      const ativo = sel.has(k) ? " ativo" : "";
      return `<button type="button" class="cd-plat-chip plat-${k}${ativo}" data-plat-chip="${k}" data-plat-id="${p.id}">${esc(MP[k].longo)}</button>`;
    }).join("");
    const salvarDisabled = sel.size ? "" : " disabled";
    return `<div class="cd-plat-bloco">
      <p>Marque só os marketplaces que essa solicitação precisa. O que ficar de fora já vira "não se aplica" sozinho.</p>
      <div class="cd-plat-chips">${chips}</div>
      <button class="cd-btn cd-btn-ok cd-mini" type="button" data-salvar-plataformas="${p.id}"${salvarDisabled}>Salvar</button>
      <button class="cd-btn cd-mini" type="button" data-cancelar-plataformas="${p.id}">Cancelar</button>
    </div>`;
  }

  // 16/10: só libera quando os 3 estão resolvidos (feito ou não se aplica) -- é a
  // ÚNICA forma do produto travar/virar "Concluído" (marcar o último marketplace
  // sozinho não fecha mais nada, ver cadastro_produtos.py).
  function blocoConfirmar(p) {
    if (!todosResolvidos(p)) return "";
    const ocupado = estado.confirmOcupados.has(p.id) ? " disabled" : "";
    return `<button class="cd-btn cd-btn-confirmar" type="button" data-confirmar="${p.id}"${ocupado}>✓ Confirmar cadastro</button>`;
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
    // 16/10 (correção): esperando o supervisor/gerente definir as plataformas -- ninguém marca
    // canal ainda, nem quem tem permissão de definir (o servidor recusa também, com 409).
    const esperandoPlataformas = !feito && aguardandoPlataformas(p);
    const tags = (feito || ehMarcacaoAntiga(p) || esperandoPlataformas) ? "" : `<div class="cd-mps">${CHAVES.map((k) => botaoMp(p, k)).join("")}</div>`;
    const avisoEspera = esperandoPlataformas ? `<small class="cd-quem">Aguardando o supervisor/gerente definir quais marketplaces essa solicitação precisa.</small>` : "";
    const quem = ehMarcacaoAntiga(p) ? "" : textoQuem(p);
    const definirPlataformas = feito ? "" : blocoDefinirPlataformas(p);
    const confirmar = feito ? "" : blocoConfirmar(p);
    // Pendente (15/10): nenhum atalho -- só os botões por canal acima (tags), que já
    // registram quem cadastrou cada marketplace. Travado: só o Reabrir, pra admin/supervisor.
    let atalho = "";
    if (feito) {
      // Travado: só admin/supervisor vê como mexer de novo, e precisa informar o motivo.
      atalho = souSupervisao()
        ? `<button class="cd-btn cd-mini" type="button" data-reabrir="${p.id}" title="Volta pra pendente, pede o motivo e fica registrado">↻ Reabrir</button>`
        : `<small class="cd-quem">Travado — só supervisor/admin pode reabrir.</small>`;
    }
    // 15/10: excluir uma solicitação PENDENTE -- só admin/supervisor, motivo obrigatório.
    // Não apaga de verdade (o servidor só marca "excluida"); some da lista.
    const excluir = (!feito && souSupervisao())
      ? `<button class="cd-btn cd-mini cd-btn-excluir" type="button" data-excluir="${p.id}" title="Remove da lista; pede o motivo e fica registrado">🗑 Excluir</button>`
      : "";
    const tempo = blocoTempo(p);
    return `<div class="cd-acao">${topo}${tags}${avisoEspera}${definirPlataformas}${confirmar}${quem}${tempo}${blocoReabertura(p)}${atalho}${excluir}</div>`;
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
      if (p.assumido_por && !isNaN(chegou)) linhas.push(`<span class="cd-chegou">📅 Chegou ${esc(dataBr(p.data_nota))}</span> <small>(há ${esc(duracao(agora - chegou))})</small>`);
    }
    let botao = "";
    // 16/10 (correção): sem plataforma definida ainda, nem o botão Assumir aparece --
    // só depois do supervisor/gerente escolher (ver blocoDefinirPlataformas/blocoMarketplaces).
    if (p.status !== "cadastrado" && !aguardandoPlataformas(p)) {
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
    // 16/10: link das fotos (Drive etc.), quando preenchido na solicitação.
    const fotos = p.link_fotos ? `<div><a class="cd-link-fotos" href="${esc(p.link_fotos)}" target="_blank" rel="noopener">📷 Fotos</a></div>` : "";
    const nota = `<div class="cd-nota"><b>${p.numero_nota ? "Nota " + esc(p.numero_nota) : "—"}</b><small>${esc(p.fornecedor || "sem fornecedor")} · ${esc(dataBr(p.data_nota))}</small></div>`;
    return `<div class="cd-linha${feito ? " feito" : ""}" data-linha="${p.id}">
      <div><div class="cd-sku">${esc(p.sku)}</div>${manual}${fotos}</div>
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
    // 15/10: filtro por Conta -- compara pela mesma "chave" usada pra agrupar (ignora
    // acento/maiúscula), porque o nome do produto vem do Operador e pode não bater
    // letra a letra com o apelido cadastrado em "Contas conectadas".
    if (estado.conta) itens = itens.filter((p) => chaveEmpresa(p.empresa) === chaveEmpresa(estado.conta));
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
      // 16/10: marcar o último já não fecha mais sozinho -- só avisa que falta Confirmar.
      if (todosResolvidos(d.item) && d.item.status !== "cadastrado") aviso("Tudo marcado — clique em Confirmar cadastro para concluir.", "ok");
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

  // 16/10: Confirmar -- só funciona se o servidor concordar que os 3 estão
  // resolvidos (confere de novo lá, não confia só na tela). Trava de vez.
  async function confirmarProduto(id) {
    if (estado.confirmOcupados.has(id)) return;
    estado.confirmOcupados.add(id);
    desenhar();
    try {
      const d = await api(`/produtos/${id}/confirmar`, { method: "POST" });
      estado.confirmOcupados.delete(id);
      aplicarItem(d.item);
      aviso("Cadastro confirmado — produto concluído.", "ok");
      carregarContagem();
    } catch (e) {
      estado.confirmOcupados.delete(id);
      desenhar();
      aviso("Não consegui confirmar: " + e.message, "erro");
    }
  }

  // 16/10: supervisor/gerente escolhe as plataformas de um produto automático,
  // antes de alguém assumir. Abre/fecha o bloco de seleção e salva no servidor.
  function abrirEscolhaPlataformas(id) {
    estado.escolhendoPlataformas.add(id);
    if (!estado.selecaoPlataformas[id]) estado.selecaoPlataformas[id] = new Set();
    desenhar();
  }
  function cancelarEscolhaPlataformas(id) {
    estado.escolhendoPlataformas.delete(id);
    delete estado.selecaoPlataformas[id];
    desenhar();
  }
  function alternarChipPlataforma(id, chave) {
    if (!estado.selecaoPlataformas[id]) estado.selecaoPlataformas[id] = new Set();
    const sel = estado.selecaoPlataformas[id];
    if (sel.has(chave)) sel.delete(chave); else sel.add(chave);
    desenhar();
  }
  async function salvarEscolhaPlataformas(id) {
    const sel = estado.selecaoPlataformas[id];
    if (!sel || !sel.size) return;
    try {
      const d = await api(`/produtos/${id}/plataformas`, { method: "POST", body: JSON.stringify({ plataformas: Array.from(sel) }) });
      estado.escolhendoPlataformas.delete(id);
      delete estado.selecaoPlataformas[id];
      aplicarItem(d.item);
      aviso("Plataformas definidas.", "ok");
    } catch (e) {
      aviso("Não consegui salvar as plataformas: " + e.message, "erro");
      desenhar();
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
    const excl = e.target.closest("[data-excluir]");
    if (excl) return excluirProduto(Number(excl.dataset.excluir));
    const confirmar = e.target.closest("[data-confirmar]");
    if (confirmar) return confirmarProduto(Number(confirmar.dataset.confirmar));
    const abrirPlat = e.target.closest("[data-abrir-plataformas]");
    if (abrirPlat) return abrirEscolhaPlataformas(Number(abrirPlat.dataset.abrirPlataformas));
    const cancelarPlat = e.target.closest("[data-cancelar-plataformas]");
    if (cancelarPlat) return cancelarEscolhaPlataformas(Number(cancelarPlat.dataset.cancelarPlataformas));
    const chipPlat = e.target.closest("[data-plat-chip]");
    if (chipPlat) return alternarChipPlataforma(Number(chipPlat.dataset.platId), chipPlat.dataset.platChip);
    const salvarPlat = e.target.closest("[data-salvar-plataformas]");
    if (salvarPlat) return salvarEscolhaPlataformas(Number(salvarPlat.dataset.salvarPlataformas));
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

  // Exclui uma solicitação PENDENTE (admin/supervisor, 15/10). Não apaga de verdade --
  // o servidor só marca "excluida" e guarda quem, quando e o motivo; aqui só some da lista.
  async function excluirProduto(id) {
    const motivo = window.prompt("Por que esta solicitação está sendo excluída? (fica registrado)");
    if (motivo === null) return; // cancelou
    if (motivo.trim().length < 5) { aviso("Descreva o motivo com um pouco mais de detalhe (mínimo 5 caracteres).", "erro"); return; }
    try {
      await api(`/produtos/${id}/excluir`, { method: "POST", body: JSON.stringify({ motivo: motivo.trim() }) });
      estado.itens = estado.itens.filter((x) => x.id !== id);
      desenhar();
      aviso("Solicitação excluída.", "ok");
      carregarContagem();
    } catch (e) {
      aviso("Não consegui excluir: " + e.message, "erro");
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
  document.getElementById("cd-conta").addEventListener("change", (e) => { estado.conta = e.target.value; desenhar(); });
  estado.eu = document.getElementById("cd-lista").dataset.eu || "";
  estado.papel = document.getElementById("cd-lista").dataset.papel || "";

  document.getElementById("cd-atualizar").addEventListener("click", () => carregar());

  // ------------------------------------------------------- solicitação manual (15/10)
  // Uma linha por produto, numa tabela editável (como planilha): digita célula por
  // célula ou cola um trecho copiado de uma planilha de verdade (preenche várias
  // linhas/colunas de uma vez a partir da célula clicada). Nada é mandado pro servidor
  // enquanto preenche -- só ao clicar em "Finalizar solicitação", que manda tudo numa
  // chamada só pro /produtos/lote. Fornecedor e Nota são por linha (cada produto pode
  // ter o seu), não mais um campo único lá em cima.
  const form = document.getElementById("cd-form");
  const campo = (id) => document.getElementById(id);
  const marcaRefazer = campo("cf-refazer");
  const incluidos = []; // produtos incluídos na última chamada bem-sucedida (lista "Incluídos agora")

  // 15/10: "é kit" e os canais marcados -- valem pra toda a solicitação (uma
  // marcação só, não por linha da grade). Os dois são obrigatórios: enquanto
  // não escolhidos, o Finalizar fica travado (ver faltando()).
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

  // Mesma lista (tabela "contas") alimenta DOIS seletores: o da solicitação ("Conta *",
  // obrigatório escolher) e o filtro da lista de pendentes/cadastrados ("Conta: todas",
  // opcional). Uma chamada só na API, cada um com seu texto de placeholder.
  async function carregarContas() {
    try {
      const d = await api("/contas");
      const preencher = (sel, placeholder) => {
        const atual = sel.value;
        sel.innerHTML = `<option value="">${esc(placeholder)}</option>` + d.contas.map((n) => `<option value="${esc(n)}">${esc(n)}</option>`).join("");
        if (atual && d.contas.includes(atual)) sel.value = atual;
      };
      preencher(campo("cf-conta"), "Selecione a conta…");
      preencher(document.getElementById("cd-conta"), "Conta: todas");
    } catch (e) { /* select fica só com o placeholder; dá pra tentar de novo reabrindo o formulário ou atualizando a página */ }
  }

  function pintarRefazer() { campo("cf-refazer-btn").classList.toggle("ligado", marcaRefazer.checked); }
  marcaRefazer.addEventListener("change", pintarRefazer);

  // Divide em linhas e colunas respeitando aspas (descrição com vírgula/quebra de linha
  // dentro de "..."). Usado tanto pro CSV anexado quanto pro texto colado numa célula.
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

  // "1.234,50" -> 1234.5 ; "R$ 12,5" -> 12.5 ; "1.234" -> 1234 ; "12.5" -> 12.5
  function numeroBr(x) {
    let v = String(x ?? "").replace(/R\$|\s/g, "");
    if (v === "") return 0;
    if (v.includes(",")) v = v.replace(/\./g, "").replace(",", ".");
    else if (/^\d{1,3}(\.\d{3})+$/.test(v)) v = v.replace(/\./g, "");
    const n = Number(v);
    return isFinite(n) ? n : NaN;
  }
  // Número que não dá pra entender vira 0, nunca NaN -- um NaN no JSON vira `null` e
  // derrubaria o lote inteiro no servidor (erro de tipo), em vez de só essa linha.
  function numeroOuZero(x) { const n = numeroBr(x); return isNaN(n) ? 0 : n; }

  // Ordem das colunas da grade -- mesma ordem do cabeçalho na tela.
  const CAMPOS_GRADE = ["sku", "produto", "gtin", "quantidade", "custo_unitario", "fornecedor", "numero_nota"];

  function linhaVazia() {
    return { sku: "", produto: "", gtin: "", quantidade: "", custo_unitario: "", fornecedor: "", numero_nota: "", link_fotos: "" };
  }
  function linhaTemConteudo(r) { return CAMPOS_GRADE.some((c) => String(r[c] || "").trim() !== ""); }
  // 15/10: obrigatórios por produto -- SKU, nome, EAN e custo maior que zero.
  // Habilita o Finalizar; quem recusa de fato (SKU repetido etc.) é o servidor.
  function linhaPreenchida(r) {
    return !!(
      String(r.sku || "").trim() &&
      String(r.produto || "").trim() &&
      String(r.gtin || "").trim() &&
      numeroBr(r.custo_unitario) > 0
    );
  }

  let linhasGrade = [linhaVazia(), linhaVazia(), linhaVazia()];

  // 16/10: posiciona o popover do link em "fixed" (calculado pelo botão que abriu),
  // em vez de "absolute" dentro da grade -- a grade tem overflow-x:auto, que corta
  // qualquer coisa "absolute" que passe da borda vertical dela.
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

  // Espalha um trecho colado (texto com várias linhas/colunas) a partir da célula onde
  // o Ctrl+V foi feito -- mesmo comportamento de colar numa planilha de verdade. Cria
  // linhas novas no fim se o trecho colado tiver mais linhas do que a grade atual.
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
  // Clique fora de um popover de link aberto -- fecha (sem perder o que já foi salvo).
  document.addEventListener("click", (e) => {
    if (e.target.closest(".cd-link-popover") || e.target.closest("[data-abrir-link]")) return;
    document.querySelectorAll(".cd-link-popover.aberto").forEach((p) => p.classList.remove("aberto"));
  });
  campo("cf-adicionar-linha").addEventListener("click", () => { linhasGrade.push(linhaVazia()); desenharGrade(); });

  // Anexar planilha salva como CSV: lê o arquivo e substitui a grade inteira por ele
  // (mesma ordem de colunas do cabeçalho: SKU, Produto, EAN, Qtd., Custo, Fornecedor, Nota).
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

  // O que falta pra habilitar "Finalizar solicitação": Conta, pelo menos 1 produto
  // completo (SKU, nome, EAN, custo > 0), pelo menos 1 canal marcado e "é kit"
  // escolhido. Não depende da grade inteira estar "certa" -- o que o servidor
  // recusar linha a linha (SKU repetido, EAN faltando...) volta em `ignorados`,
  // sem travar o resto.
  function faltando() {
    const f = [];
    if (!campo("cf-conta").value.trim()) f.push("conta");
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
  campo("cf-conta").addEventListener("change", atualizarBotaoFinalizar);

  // ---------- lista "Incluídos agora" ----------
  function desenharIncluidos(erros) {
    const bloco = campo("cf-incluidos");
    bloco.style.display = incluidos.length || (erros && erros.length) ? "block" : "none";
    campo("cf-incluidos-n").textContent = incluidos.length ? `(${incluidos.length})` : "";
    campo("cf-incluidos-lista").innerHTML =
      (erros || []).map((e) => `<div class="cd-inc-erro">✖ Linha ${esc(e.linha)}${e.sku ? " (" + esc(e.sku) + ")" : ""}: não entrou — ${esc(e.motivo)}</div>`).join("") +
      incluidos.slice().reverse().map((p) => `<div class="cd-inc-item"><b>${esc(p.sku)}</b><span style="flex:1">${esc(p.produto)}</span><span>${esc(quantidade(p.quantidade))} un · ${esc(moeda(p.custo_unitario))}</span></div>`).join("");
  }

  async function aposIncluir() {
    estado.status = "pendente"; estado.falta = ""; campo("cd-falta").value = ""; estado.resp = ""; campo("cd-resp").value = "";
    estado.ocupado = false;
    await carregar(true);
    carregarContas();
  }

  function limparGrade() {
    linhasGrade = [linhaVazia(), linhaVazia(), linhaVazia()];
    desenharGrade();
  }

  // "Finalizar solicitação": manda TODA linha com algo preenchido numa chamada só pro
  // /produtos/lote (cada uma já carrega seu fornecedor/nota) -- quem recusa linha
  // incompleta/inválida é o servidor, por item, sem travar as demais. Se tudo entrou,
  // fecha o formulário; se sobrou alguma recusada, deixa só essas na grade pra corrigir
  // e finalizar de novo.
  campo("cf-finalizar").addEventListener("click", async () => {
    if (faltando().length) { atualizarBotaoFinalizar(); return; } // defesa -- o botão já estaria desabilitado
    const conta = campo("cf-conta").value.trim();
    const itensValidos = linhasGrade.filter(linhaTemConteudo);
    const botao = campo("cf-finalizar");
    botao.disabled = true;
    try {
      const d = await api("/produtos/lote", {
        method: "POST",
        body: JSON.stringify({
          empresa: conta,
          refazer: marcaRefazer.checked,
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
      const refaz = marcaRefazer.checked ? " para REFAZER" : "";
      aviso(`${d.criados.length} produto(s) incluído(s)${refaz}.` + (d.ignorados.length ? ` ${d.ignorados.length} não entrou(aram) — corrija e finalize de novo.` : ""), d.ignorados.length ? "aviso" : "ok");
      if (d.ignorados.length) {
        const recusadas = new Set(d.ignorados.map((x) => x.linha));
        linhasGrade = itensValidos.filter((_, i) => recusadas.has(i + 1));
        desenharGrade();
      } else {
        form.style.display = "none";
        incluidos.length = 0;
        campo("cf-conta").value = "";
        limparGrade();
        marcaRefazer.checked = false; pintarRefazer();
        limparSelecoes();
      }
      await aposIncluir();
    } catch (e) {
      aviso("Não consegui enviar a solicitação: " + e.message, "erro");
    } finally {
      atualizarBotaoFinalizar();
    }
  });

  function abrirFormNovo() {
    form.style.display = "block";
    carregarContas();
    (campo("cf-conta").value ? campo("cf-grade-corpo").querySelector("input") : campo("cf-conta")).focus();
  }

  document.getElementById("cd-novo").addEventListener("click", () => {
    if (form.style.display === "block") { form.style.display = "none"; return; }
    abrirFormNovo();
  });

  // 16/10: link "Nova solicitação" do menu lateral manda pra /cadastro?novo=1 --
  // já abre o formulário direto (igual o /cadastro-seller faz pro seller), sem
  // precisar achar o botão "+ Incluir produto" na tela. Limpa o "?novo=1" da URL
  // depois (replaceState) pra um F5 não reabrir sozinho.
  if (new URLSearchParams(location.search).get("novo") === "1") {
    abrirFormNovo();
    history.replaceState(null, "", location.pathname);
  }

  campo("cf-cancelar").addEventListener("click", () => {
    form.style.display = "none";
    incluidos.length = 0; desenharIncluidos([]);
    campo("cf-conta").value = "";
    limparGrade();
    marcaRefazer.checked = false; pintarRefazer();
    limparSelecoes();
    aviso("");
  });

  // Atualiza sozinha a cada minuto (só com a aba visível) -- produto novo aparece e os tempos andam, sem recarregar.
  setInterval(() => { if (!document.hidden) { if (form.style.display !== "block") carregar(true); else desenhar(); } }, 60000);

  desenharGrade();
  carregar();
  carregarContas(); // popula o filtro "Conta" da lista já na abertura da tela
})();
