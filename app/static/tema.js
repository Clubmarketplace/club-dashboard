(function () {
  "use strict";
  // Modo dia/noite do site (painel) -- só visual, preferência salva no
  // navegador (localStorage), por pessoa/computador, nunca no servidor.
  // Aplicado via atributo data-tema="escuro" no <html>; o resto é CSS
  // (ver :root[data-tema="escuro"] em dashboard.css).

  function temaSalvo() {
    try {
      return localStorage.getItem("cmxTema") === "escuro" ? "escuro" : "claro";
    } catch (e) {
      return "claro"; // storage bloqueado (modo privado etc.) -- cai no claro, sem quebrar a página
    }
  }

  function aplicarTema(tema) {
    if (tema === "escuro") {
      document.documentElement.setAttribute("data-tema", "escuro");
    } else {
      document.documentElement.removeAttribute("data-tema");
    }
    document.querySelectorAll(".cmx-botao-tema").forEach(function (botao) {
      botao.textContent = tema === "escuro" ? "☀️ Modo dia" : "🌙 Modo noite";
    });
  }

  // Aplica o quanto antes (antes do DOMContentLoaded), pra reduzir o
  // "flash" de tela clara em quem já escolheu o modo noite.
  aplicarTema(temaSalvo());

  document.addEventListener("DOMContentLoaded", function () {
    aplicarTema(temaSalvo()); // garante o texto certo no botão, que só existe a partir daqui
    document.querySelectorAll(".cmx-botao-tema").forEach(function (botao) {
      botao.addEventListener("click", function () {
        var novo = temaSalvo() === "escuro" ? "claro" : "escuro";
        try {
          localStorage.setItem("cmxTema", novo);
        } catch (e) {
          /* sem storage: alterna só nessa visita, sem lembrar da próxima vez */
        }
        aplicarTema(novo);
      });
    });
  });
})();
