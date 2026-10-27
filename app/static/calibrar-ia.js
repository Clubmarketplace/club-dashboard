/*
 * Tela "Calibrar IA" (Administração). Lê os ajustes em /api/calibrar-ia,
 * deixa marcar/desmarcar e salva só quando clicar em "Salvar ajustes".
 */
(function () {
  const $ = (id) => document.getElementById(id);
  const escapar = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  let salvo = null;   // ajustes como estão no servidor
  let atual = null;   // ajustes na tela (com as mudanças ainda não salvas)
  let degraus = [];

  const NOMES = {
    ia_ativa: "Nossa IA ligada", janela_ml_min: "Tempo para a IA do ML", fonte_respostas_padrao: "Fonte: respostas padrão",
    fonte_dados_anuncio: "Fonte: ficha do anúncio", fonte_manuais: "Fonte: manuais", fonte_internet: "Fonte: pesquisa na internet",
    degraus: "Degraus da pesquisa", duas_fontes: "Exigir duas fontes", buscas_por_degrau: "Buscas por degrau",
    modelo_pesquisa: "Modelo da pesquisa", tom: "Tom", tamanho: "Tamanho",
    credito_comprado_usd: "Saldo comprado (crédito da IA)",
    credito_saldo_usd: "Saldo conferido no Console", credito_saldo_em: "Data da conferência do saldo", credito_calibracao: "Ajuste da estimativa de gasto",
  };
  const DESCRICOES = {
    fabricante: "Site oficial da marca.",
    pdf: "Manual, ficha ou boletim técnico em PDF do mesmo modelo, mesmo que esteja no site de uma loja.",
    lojas: "Ficha técnica na página do produto em lojas grandes (Leroy, Magalu, Casas Bahia, Amazon…).",
    videos: "Vídeos do canal oficial da marca (título, descrição, transcrição). A IA não vê a imagem.",
    tecnicos: "Catálogos de autopeças, distribuidores e portais técnicos. Pode confundir versões parecidas.",
    aberta: "Qualquer site confiável. É o que mais acha, e o que mais pode errar.",
  };

  function aviso(texto, tipo) {
    const el = $("ci-aviso");
    el.textContent = texto;
    el.style.display = texto ? "block" : "none";
    el.style.background = tipo === "erro" ? "#fdecea" : "#e8f6ef";
    el.style.color = tipo === "erro" ? "#8a1f11" : "#14532d";
  }

  function formatarValor(chave, v) {
    if (v === null || v === undefined) return "—";
    if (typeof v === "boolean") return v ? "sim" : "não";
    if (chave === "degraus") return degraus.filter((d) => v[d.chave]).map((d) => d.nome.split(" · ")[0]).join(", ") || "nenhum";
    if (chave === "janela_ml_min") return v + " min";
    if (chave === "modelo_pesquisa") return v === "preciso" ? "mais preciso" : "econômico";
    if (chave === "credito_comprado_usd" || chave === "credito_saldo_usd") return "US$ " + Number(v).toFixed(2);
    if (chave === "credito_saldo_em") return new Date(v).toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    if (chave === "credito_calibracao") return "×" + Number(v).toFixed(2);
    return String(v);
  }

  function mudancas() {
    // janela_ml_min é fixa (3 min): nunca vai como alteração
    // Os campos do termômetro (credito_*) têm botão próprio ("Conferir agora"): nunca viram alteração pendente.
    return Object.keys(atual).filter((k) => k !== "janela_ml_min" && !k.startsWith("credito_") && JSON.stringify(atual[k]) !== JSON.stringify(salvo[k]));
  }

  function pintar() {
    // Liga/pausa
    const sw = $("ci-ativa");
    sw.setAttribute("aria-checked", atual.ia_ativa ? "true" : "false");
    sw.style.background = atual.ia_ativa ? "#16a36a" : "#c3cad3";
    sw.querySelector("span").style.left = atual.ia_ativa ? "26px" : "4px";
    $("ci-ativa-txt").textContent = atual.ia_ativa ? "Ligada — respondendo sozinha" : "Pausada — tudo vai para a equipe";
    $("ci-janela-txt").textContent = atual.janela_ml_min + " minutos";
    document.querySelectorAll("input[data-chave]").forEach((c) => { c.checked = !!atual[c.dataset.chave]; });
    document.querySelectorAll("input[data-degrau]").forEach((c) => { c.checked = !!atual.degraus[c.dataset.degrau]; c.disabled = !atual.fonte_internet; });
    $("ci-duas").disabled = $("ci-buscas").disabled = !atual.fonte_internet;
    $("ci-buscas").value = String(atual.buscas_por_degrau);
    // Campo do saldo: só preenche se a pessoa não estiver digitando nele agora.
    if (document.activeElement !== $("ci-saldo-input") && !$("ci-saldo-input").value && atual.credito_saldo_usd > 0) {
      $("ci-saldo-input").value = Number(atual.credito_saldo_usd).toFixed(2);
    }
    ["modelo", "tom", "tamanho"].forEach((nome) => {
      const chave = nome === "modelo" ? "modelo_pesquisa" : nome;
      document.querySelectorAll(`input[name="${nome}"]`).forEach((r) => { r.checked = r.value === atual[chave]; });
    });
    const m = mudancas();
    $("ci-mudancas").textContent = m.length ? `${m.length} alteração(ões) não salva(s): ${m.map((k) => NOMES[k] || k).join(", ")}` : "Nenhuma alteração.";
    $("ci-salvar").disabled = $("ci-desfazer").disabled = !m.length;
  }

  function montarCampos() {
    $("ci-buscas").innerHTML = [1, 2, 3, 4, 5].map((n) => `<option value="${n}">${n}</option>`).join("");
    $("ci-degraus").innerHTML = degraus.map((d, i) => {
      const [num, ...resto] = d.nome.split(" · ");
      return `<div class="ci-linha"><span class="ci-num">${escapar(num)}</span><input type="checkbox" id="dg-${d.chave}" data-degrau="${d.chave}" />
        <label for="dg-${d.chave}">${escapar(resto.join(" · ").replace(/^./, (c) => c.toUpperCase()))}<small>${escapar(DESCRICOES[d.chave] || "")}</small></label></div>`;
    }).join("");
  }

  function pintarHistorico(hist) {
    $("ci-hist").innerHTML = hist.length ? hist.map((h) => {
      const quando = h.em ? new Date(h.em).toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
      return `<tr><td>${quando}</td><td>${escapar(h.por || "—")}</td><td>${escapar(NOMES[h.chave] || h.chave)}</td><td>${escapar(formatarValor(h.chave, h.antes))}</td><td>${escapar(formatarValor(h.chave, h.depois))}</td></tr>`;
    }).join("") : '<tr><td colspan="5">Nenhuma alteração ainda — a IA está com os ajustes padrão.</td></tr>';
    const ultimaPausa = hist.find((h) => h.chave === "ia_ativa");
    $("ci-ativa-info").textContent = ultimaPausa ? `${ultimaPausa.depois ? "Ligada" : "Pausada"} por ${ultimaPausa.por || "—"} em ${new Date(ultimaPausa.em).toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo" })}` : "";
  }

  // Traduz o erro técnico da API numa dica clara. Só ajuda a ler: o motivo
  // original sempre é mostrado inteiro logo abaixo, então nada fica escondido.
  function dicaDoMotivo(motivo) {
    const m = String(motivo || "").toLowerCase();
    if (!m) return "";
    if (m.includes("credit balance") || m.includes("billing")) return "Falta de crédito na conta da Anthropic.";
    if (m.includes("401") || m.includes("invalid x-api-key") || m.includes("authentication")) return "Chave da API inválida ou revogada (ANTHROPIC_API_KEY).";
    if (m.includes("permission") || m.includes("403")) return "A chave não tem permissão para esse recurso/modelo.";
    if (m.includes("not_found") || m.includes("404") || m.includes("model")) return "Modelo não encontrado ou desativado (CLAUDE_MODEL_PRE_VENDA).";
    if (m.includes("429") || m.includes("rate")) return "Limite de requisições atingido — costuma passar sozinho.";
    if (m.includes("overloaded") || m.includes("529") || m.includes("500") || m.includes("timeout") || m.includes("connection")) return "Instabilidade temporária da API ou de rede.";
    return "";
  }

  function pintarSaude(saude) {
    const el = $("ci-saude");
    if (!saude) { el.style.display = "none"; return; }
    const hora = new Date(saude.desde).toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", hour: "2-digit", minute: "2-digit" });
    const dica = dicaDoMotivo(saude.motivo);
    // textContent (nunca innerHTML): o motivo vem de texto de erro externo.
    el.textContent = `⚠️ IA falhando desde ${hora} — perguntas estão indo direto pra equipe.`
      + (dica ? `\n${dica}` : "")
      + `\nMotivo registrado: ${saude.motivo || "(sem detalhe registrado)"}`;
    el.style.whiteSpace = "pre-line";
    el.style.display = "block";
  }

  const dinheiro = (n) => "US$ " + Number(n).toFixed(2).replace(".", ",");
  const dataHora = (iso) => new Date(iso).toLocaleString("pt-BR", { timeZone: "America/Sao_Paulo", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  function haQuanto(horas) {
    if (horas < 1) return "há menos de 1 hora";
    if (horas < 48) return "há " + Math.round(horas) + " h";
    return "há " + Math.round(horas / 24) + " dias";
  }

  function pintarCredito(credito) {
    if (!credito) return;
    const semChave = $("ci-termo-sem-chave"), corpo = $("ci-termo-corpo");
    if (!credito.disponivel) {
      semChave.style.display = "block";
      corpo.style.display = "none";
      return;
    }
    semChave.style.display = "none";
    corpo.style.display = "block";

    const selo = $("ci-termo-selo");
    const info = $("ci-termo-info"), velho = $("ci-termo-aviso-velho"), baixo = $("ci-termo-aviso-baixo");
    const barra = $("ci-termo-barra");
    barra.classList.remove("ci-termo-amarelo", "ci-termo-vermelho");
    velho.style.display = "none"; velho.classList.remove("ci-velho-forte");

    if (credito.modo === "saldo") {
      // Saldo conferido no Console + gasto estimado desde aquele instante.
      selo.textContent = "estimado desde a conferência";
      selo.className = "ci-termo-selo ci-selo-estimado";
      selo.title = "Saldo informado do Console menos o gasto calculado pelos tokens de cada resposta da IA desde a conferência. Quanto mais recente a conferência, mais preciso.";
      $("ci-termo-gasto-rot").textContent = "Gasto desde a conferência";
      $("ci-termo-base-rot").textContent = "Saldo conferido";
      $("ci-termo-resta").textContent = dinheiro(credito.resta_usd);
      $("ci-termo-gasto").textContent = dinheiro(credito.gasto_usd);
      $("ci-termo-comprado-txt").textContent = dinheiro(credito.saldo_usd);

      const pct = credito.saldo_usd > 0 ? Math.max(0, Math.min(100, (credito.resta_usd / credito.saldo_usd) * 100)) : 0;
      barra.style.width = pct + "%";
      if (credito.comprar_mais) barra.classList.add("ci-termo-vermelho");
      else if (pct <= 40) barra.classList.add("ci-termo-amarelo");

      const partes = [`Conferido em ${dataHora(credito.conferido_em)} (${haQuanto(credito.horas_desde)})`];
      if (credito.ritmo_dia_usd) partes.push(`ritmo de ${dinheiro(credito.ritmo_dia_usd)} por dia`);
      if (credito.dias_restantes != null) partes.push(`dá para cerca de ${credito.dias_restantes.toLocaleString("pt-BR")} dia(s)`);
      if (Math.abs(credito.fator - 1) >= 0.02) partes.push(`estimativa ajustada ×${credito.fator.toFixed(2).replace(".", ",")} pelas últimas conferências`);
      info.textContent = partes.join(" · ");
      info.style.display = "block";

      baixo.style.display = credito.comprar_mais ? "block" : "none";
      if (credito.horas_desde >= 72) {
        velho.textContent = "⚠ O saldo foi conferido " + haQuanto(credito.horas_desde) + ". Confira no Console e clique em \"Conferir agora\": a estimativa perde precisão com o tempo.";
        velho.classList.add("ci-velho-forte"); velho.style.display = "block";
      } else if (credito.horas_desde >= 24) {
        velho.textContent = "Faz mais de 24 h desde a última conferência. Vale conferir o saldo no Console de novo.";
        velho.style.display = "block";
      }
      return;
    }

    // Legado: ninguém informou o saldo do Console ainda.
    selo.textContent = credito.exato ? "exato" : "estimado";
    selo.className = "ci-termo-selo " + (credito.exato ? "ci-selo-exato" : "ci-selo-estimado");
    selo.title = credito.exato ? "Direto da Cost API da Anthropic." : "Calculado pelos tokens que a nossa IA registrou em cada resposta.";
    $("ci-termo-gasto-rot").textContent = "Gasto este mês (só o que a IA registrou)";
    $("ci-termo-base-rot").textContent = "Comprado";
    const gasto = credito.gasto_usd ?? 0, comprado = credito.comprado_usd ?? 0;
    const resta = credito.resta_usd ?? (comprado - gasto);
    $("ci-termo-resta").textContent = dinheiro(resta);
    $("ci-termo-gasto").textContent = dinheiro(gasto);
    $("ci-termo-comprado-txt").textContent = dinheiro(comprado);
    const pct = comprado > 0 ? Math.max(0, Math.min(100, (resta / comprado) * 100)) : 0;
    barra.style.width = pct + "%";
    if (pct <= 15) barra.classList.add("ci-termo-vermelho"); else if (pct <= 40) barra.classList.add("ci-termo-amarelo");
    info.textContent = "Para medir com precisão, informe o saldo que o Console mostra agora e clique em \"Conferir agora\".";
    info.style.display = "block";
    baixo.style.display = (pct <= 15 && comprado > 0) ? "block" : "none";
  }

  // Botão "Conferir agora": manda o saldo do Console; o servidor grava o valor e o instante.
  async function conferirSaldo() {
    const campo = $("ci-saldo-input"), botao = $("ci-saldo-btn");
    const valor = Number(String(campo.value).replace(",", "."));
    if (!(valor > 0)) { aviso("Informe o saldo que aparece no Console (ex.: 19.99).", "erro"); return; }
    botao.disabled = true; botao.textContent = "Conferindo…";
    try {
      const r = await fetch("/api/calibrar-ia/saldo", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ saldo_usd: valor }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || "status " + r.status);
      // Atualiza só o termômetro e o histórico: não mexe em ajustes que a pessoa esteja editando.
      ["credito_saldo_usd", "credito_saldo_em", "credito_calibracao"].forEach((k) => { salvo[k] = d.config[k]; atual[k] = d.config[k]; });
      pintarCredito(d.credito || null);
      pintarHistorico(d.historico || []);
      aviso("✓ Saldo conferido: " + dinheiro(valor) + ". O gasto passa a ser contado a partir de agora.", "ok");
    } catch (e) {
      aviso("Não foi possível conferir o saldo: " + e.message, "erro");
    } finally { botao.disabled = false; botao.textContent = "Conferir agora"; }
  }
  $("ci-saldo-btn").addEventListener("click", conferirSaldo);
  $("ci-saldo-input").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); conferirSaldo(); } });

  function aplicar(dados) {
    degraus = dados.degraus;
    salvo = JSON.parse(JSON.stringify(dados.config));
    atual = JSON.parse(JSON.stringify(dados.config));
    montarCampos();
    pintarHistorico(dados.historico || []);
    pintarSaude(dados.saude || null);
    pintarCredito(dados.credito || null);
    pintar();
  }

  async function carregar() {
    try {
      const r = await fetch("/api/calibrar-ia", { cache: "no-store" });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || "status " + r.status);
      aplicar(d);
    } catch (e) { aviso("Não consegui carregar os ajustes: " + e.message, "erro"); }
  }

  // Confere de novo a cada minuto, só pra manter o aviso de saúde da IA em
  // dia (some sozinho assim que uma pergunta for respondida com sucesso).
  setInterval(async () => {
    try {
      const r = await fetch("/api/calibrar-ia", { cache: "no-store" });
      const d = await r.json().catch(() => ({}));
      if (r.ok) { pintarSaude(d.saude || null); pintarCredito(d.credito || null); }
    } catch (e) { /* silencioso -- não interrompe quem está editando */ }
  }, 60000);

  // Interações
  $("ci-ativa").addEventListener("click", () => {
    if (atual.ia_ativa && !confirm("Pausar a nossa IA? Enquanto pausada, tudo que a IA do ML não responder vai direto para a equipe.")) return;
    atual.ia_ativa = !atual.ia_ativa; pintar();
  });
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (!atual) return;
    if (t.id === "ci-saldo-input") return;  // só vale ao clicar em "Conferir agora"
    if (t.dataset.chave) atual[t.dataset.chave] = t.checked;
    else if (t.dataset.degrau) atual.degraus = { ...atual.degraus, [t.dataset.degrau]: t.checked };
    else if (t.id === "ci-buscas") atual.buscas_por_degrau = Number(t.value);
    else if (t.name === "modelo") atual.modelo_pesquisa = t.value;
    else if (t.name === "tom" || t.name === "tamanho") atual[t.name] = t.value;
    else return;
    pintar();
  });
  $("ci-desfazer").addEventListener("click", () => { atual = JSON.parse(JSON.stringify(salvo)); pintar(); });
  $("ci-salvar").addEventListener("click", async () => {
    const m = mudancas();
    if (!m.length) return;
    if (atual.fonte_internet && !Object.values(atual.degraus).some(Boolean)) {
      aviso("Ligue pelo menos um degrau da pesquisa, ou desligue a pesquisa na internet.", "erro"); return;
    }
    const corpo = {}; m.forEach((k) => { corpo[k] = atual[k]; });
    $("ci-salvar").disabled = true; $("ci-salvar").textContent = "Salvando…";
    try {
      const r = await fetch("/api/calibrar-ia", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(corpo) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.detail || "status " + r.status);
      aplicar(d);
      aviso("✓ Ajustes salvos. Valem para as próximas perguntas em até 30 segundos.", "ok");
    } catch (e) {
      aviso("Não foi possível salvar: " + e.message, "erro");
    } finally { $("ci-salvar").textContent = "Salvar ajustes"; pintar(); }
  });
  window.addEventListener("beforeunload", (e) => { if (atual && mudancas().length) { e.preventDefault(); e.returnValue = ""; } });

  carregar();
})();
