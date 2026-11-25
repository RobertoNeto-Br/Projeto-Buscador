(() => {
  const STORAGE_API_URL = "buscador_api_url";
  const STORAGE_API_KEY = "buscador_api_key";
  const STORAGE_SB_URL = "buscador_supabase_url";
  const STORAGE_SB_KEY = "buscador_supabase_key";

  let rawCloudPayload = null;
  let dashboardData = null;
  let selectedProductId = null;
  let priceChart = null;
  let activeStartDate = ""; // "" = todo o histórico, ou "YYYY-MM-DD"

  const $ = (id) => document.getElementById(id);

  function getSupabaseConfig() {
    const url = (localStorage.getItem(STORAGE_SB_URL) || window.BUSCADOR_CONFIG?.SUPABASE_URL || "")
      .trim()
      .replace(/\/+$/, "");
    const key = (localStorage.getItem(STORAGE_SB_KEY) || window.BUSCADOR_CONFIG?.SUPABASE_ANON_KEY || "").trim();
    return { url, key, enabled: Boolean(url && key) };
  }

  function getApiBase() {
    const saved = (localStorage.getItem(STORAGE_API_URL) || "").trim();
    if (saved) return saved.replace(/\/+$/, "");

    const cfg = (window.BUSCADOR_CONFIG?.API_BASE_URL || "").trim();
    if (cfg) return cfg.replace(/\/+$/, "");

    if (window.location.protocol.startsWith("http")) {
      return window.location.origin.replace(/\/+$/, "");
    }
    return "http://localhost:8080";
  }

  function getApiKey() {
    return (localStorage.getItem(STORAGE_API_KEY) || "").trim();
  }

  function formatMoney(value, currency = "BRL") {
    if (value === null || value === undefined || Number.isNaN(Number(value))) {
      return "Indisponível";
    }
    const num = Number(value);
    const curr = (currency || "BRL").toUpperCase();
    if (curr === "BRL") {
      return num.toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
    }
    if (curr === "USD") return `US$ ${num.toFixed(2)}`;
    if (curr === "EUR") return `€ ${num.toFixed(2)}`;
    if (curr === "GBP") return `£ ${num.toFixed(2)}`;
    return `${curr} ${num.toFixed(2)}`;
  }

  function formatDateBr(isoDate) {
    if (!isoDate) return "";
    const [y, m, d] = isoDate.slice(0, 10).split("-");
    return d && m && y ? `${d}/${m}/${y}` : isoDate;
  }

  function daysAgoIso(days) {
    const dt = new Date();
    dt.setDate(dt.getDate() - days);
    const y = dt.getFullYear();
    const m = String(dt.getMonth() + 1).padStart(2, "0");
    const d = String(dt.getDate()).padStart(2, "0");
    return `${y}-${m}-${d}`;
  }

  function escapeHtml(str) {
    return String(str ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function showToast(message, type = "info") {
    const container = $("toastContainer");
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 220);
    }, 3800);
  }

  // Mini-SAD client-side (usado quando filtramos por data no modo Nuvem/Supabase)
  function computeSadClient(historyAsc, currency = "BRL") {
    if (!historyAsc || !historyAsc.length) {
      return {
        disponivel: false,
        total_coletas: 0,
        dias_distintos: 0,
        atingiu_minimo_5_dias: false,
        decisao: "SEM DADOS",
        nivel: "neutral",
        score_oportunidade: 0,
        tendencia: "indefinida",
        tendencia_label: "Indefinida",
        justificativa: "Sem registros neste período. Ajuste a data ou clique em 'Todo o histórico'.",
      };
    }

    const prices = historyAsc.map((h) => Number(h.preco));
    const dates = new Set(historyAsc.map((h) => String(h.coletado_em || "").slice(0, 10)).filter(Boolean));
    const n = prices.length;
    const diasDistintos = dates.size;

    const precoInicial = prices[0];
    const precoAtual = prices[n - 1];
    const menor = Math.min(...prices);
    const maior = Math.max(...prices);
    const media = prices.reduce((a, b) => a + b, 0) / n;
    const amplitude = maior - menor;
    const variance = n > 1 ? prices.reduce((a, p) => a + (p - media) ** 2, 0) / n : 0;
    const desvio = Math.sqrt(variance);
    const volatilidadePct = media > 0 ? (desvio / media) * 100 : 0;
    const diferencaMediaPct = media > 0 ? ((precoAtual - media) / media) * 100 : 0;
    const posicaoFaixaPct = amplitude > 0.001 ? ((precoAtual - menor) / amplitude) * 100 : 50;

    let slope = 0;
    if (n >= 2) {
      const xMean = (n - 1) / 2;
      let num = 0;
      let den = 0;
      for (let i = 0; i < n; i++) {
        num += (i - xMean) * (prices[i] - media);
        den += (i - xMean) ** 2;
      }
      slope = den > 0 ? num / den : 0;
    }
    const slopeRelPct = media > 0 ? (slope / media) * 100 : 0;
    let tendencia = "estavel";
    let tendenciaLabel = "Estável";
    if (n >= 2 && Math.abs(slopeRelPct) >= 0.15) {
      tendencia = slopeRelPct < 0 ? "em_queda" : "em_alta";
      tendenciaLabel = slopeRelPct < 0 ? "Em queda" : "Em alta";
    }

    const scoreMedia = Math.max(0, Math.min(45, 22.5 - diferencaMediaPct * 2.25));
    const scoreFaixa = Math.max(0, Math.min(35, (1 - posicaoFaixaPct / 100) * 35));
    let scoreTendencia = 13;
    if (tendencia === "em_queda") scoreTendencia = posicaoFaixaPct <= 20 ? 14 : 6;
    else if (tendencia === "em_alta") scoreTendencia = diferencaMediaPct <= 0 ? 18 : 8;

    const score = Math.round(Math.max(0, Math.min(100, scoreMedia + scoreFaixa + scoreTendencia)));

    let decisao = "MONITORAR";
    let nivel = "blue";
    let justificativa = "";
    if (n === 1) {
      justificativa = `Apenas 1 coleta no período (${formatMoney(precoAtual, currency)}).`;
    } else if (diferencaMediaPct <= -2.0 || (posicaoFaixaPct <= 25 && amplitude > 0.01) || score >= 65) {
      decisao = "COMPRAR AGORA";
      nivel = "green";
      justificativa = `Preço atual (${formatMoney(precoAtual, currency)}) está ${Math.abs(diferencaMediaPct).toFixed(1)}% ${diferencaMediaPct <= 0 ? "abaixo" : "acima"} da média (${formatMoney(media, currency)}) — ótima oportunidade.`;
    } else if (
      diferencaMediaPct >= 2.5 ||
      (posicaoFaixaPct >= 70 && amplitude > 0.01) ||
      (tendencia === "em_queda" && posicaoFaixaPct > 35)
    ) {
      decisao = "AGUARDAR QUEDA";
      nivel = "orange";
      justificativa = `Preço atual (${formatMoney(precoAtual, currency)}) está próximo do topo do período (máxima ${formatMoney(maior, currency)}) — recomenda-se aguardar recuo.`;
    } else {
      justificativa = `Preço atual (${formatMoney(precoAtual, currency)}) próximo da média do período (${formatMoney(media, currency)}), volatilidade de ${volatilidadePct.toFixed(1)}%.`;
    }

    return {
      disponivel: true,
      total_coletas: n,
      dias_distintos: diasDistintos,
      atingiu_minimo_5_dias: diasDistintos >= 5,
      preco_inicial: Number(precoInicial.toFixed(2)),
      preco_atual: Number(precoAtual.toFixed(2)),
      menor_preco: Number(menor.toFixed(2)),
      maior_preco: Number(maior.toFixed(2)),
      media: Number(media.toFixed(2)),
      desvio_padrao: Number(desvio.toFixed(2)),
      amplitude: Number(amplitude.toFixed(2)),
      volatilidade_pct: Number(volatilidadePct.toFixed(2)),
      diferenca_media_pct: Number(diferencaMediaPct.toFixed(2)),
      posicao_faixa_pct: Number(posicaoFaixaPct.toFixed(1)),
      tendencia,
      tendencia_label: tendenciaLabel,
      score_oportunidade: score,
      decisao,
      nivel,
      justificativa,
    };
  }

  function applyClientDateFilter(basePayload, startDate) {
    if (!startDate) return basePayload;
    const allDates = new Set();
    let totalReadings = 0;

    const produtos = (basePayload.produtos || []).map((p) => {
      const filteredHist = (p.historico || []).filter((h) => {
        const d = String(h.data_iso || h.coletado_em || "").slice(0, 10);
        return d >= startDate;
      });
      totalReadings += filteredHist.length;
      filteredHist.forEach((h) => allDates.add(String(h.data_iso || h.coletado_em || "").slice(0, 10)));

      const sad = computeSadClient(filteredHist, p.moeda);
      const stats = filteredHist.length
        ? {
            total: filteredHist.length,
            dias_distintos: sad.dias_distintos,
            menor: sad.menor_preco,
            maior: sad.maior_preco,
            media: sad.media,
          }
        : null;

      let variacaoPct = null;
      if (filteredHist.length >= 2 && filteredHist[0].preco > 0) {
        const first = Number(filteredHist[0].preco);
        const last = Number(filteredHist[filteredHist.length - 1].preco);
        variacaoPct = Number((((last - first) / first) * 100).toFixed(2));
      }

      return {
        ...p,
        historico: filteredHist,
        stats,
        variacao_pct: variacaoPct,
        sad,
      };
    });

    const ranking = produtos
      .map((p) => ({
        id: p.id,
        nome: p.nome,
        loja: p.loja,
        moeda: p.moeda,
        preco_atual: p.ultimo_preco,
        media: p.sad?.media,
        menor_preco: p.sad?.menor_preco,
        dias_distintos: p.sad?.dias_distintos || 0,
        score_oportunidade: p.sad?.score_oportunidade || 0,
        decisao: p.sad?.decisao || "SEM DADOS",
        nivel: p.sad?.nivel || "neutral",
        tendencia_label: p.sad?.tendencia_label || "Indefinida",
      }))
      .sort((a, b) => b.score_oportunidade - a.score_oportunidade || b.dias_distintos - a.dias_distintos);

    return {
      ...basePayload,
      total_coletas: totalReadings,
      dias_distintos_totais: allDates.size,
      filtro_data_inicio: startDate,
      produtos,
      ranking_sad: ranking,
    };
  }

  async function supabaseFetch(path, options = {}) {
    const sb = getSupabaseConfig();
    const headers = {
      apikey: sb.key,
      Authorization: `Bearer ${sb.key}`,
      ...(options.headers || {}),
    };
    if (options.body && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    const resp = await fetch(`${sb.url}${path}`, { ...options, headers });
    const text = await resp.text();
    let data = {};
    try {
      data = text ? JSON.parse(text) : {};
    } catch {
      data = {};
    }
    if (!resp.ok) {
      throw new Error(data.message || data.error || `Supabase HTTP ${resp.status}`);
    }
    return data;
  }

  async function sendSupabaseCommand(acao, payload = {}) {
    const rows = await supabaseFetch("/rest/v1/buscador_comandos", {
      method: "POST",
      headers: { Prefer: "return=representation" },
      body: JSON.stringify({ acao, payload }),
    });
    const created = Array.isArray(rows) ? rows[0] : rows;
    const cmdId = created?.id;
    if (!cmdId) throw new Error("Não foi possível enfileirar o comando no Supabase.");

    // Aguarda o bot na Discloud processar (verifica a cada 1.2s por até 42s)
    for (let i = 0; i < 35; i++) {
      await new Promise((r) => setTimeout(r, 1200));
      const check = await supabaseFetch(`/rest/v1/buscador_comandos?id=eq.${cmdId}&select=status,resultado`);
      const item = Array.isArray(check) ? check[0] : null;
      if (!item) continue;
      if (item.status === "concluido") {
        return item.resultado || { ok: true };
      }
      if (item.status === "erro") {
        throw new Error(item.resultado?.error || "O bot retornou erro ao executar o comando.");
      }
    }
    throw new Error("O bot na Discloud demorou para responder. Verifique se ele está Online.");
  }

  async function apiFetch(path, options = {}) {
    const base = getApiBase();
    const headers = { ...(options.headers || {}) };
    if (options.body && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    const key = getApiKey();
    if (key) headers["X-API-Key"] = key;

    const resp = await fetch(`${base}${path}`, {
      ...options,
      headers,
    });

    const data = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      throw new Error(data.error || `Erro HTTP ${resp.status}`);
    }
    return data;
  }

  function setConnectionStatus(online, text) {
    const dot = $("statusDot");
    dot.classList.toggle("online", online);
    dot.classList.toggle("offline", !online);
    $("statusText").textContent = text;
    $("connectionBanner").classList.toggle("hidden", online);
  }

  function updateDateFilterButtons() {
    $("filterStartDate").value = activeStartDate;
    const isAll = !activeStartDate;
    $("btnDateAll").className = isAll ? "btn btn-small btn-primary" : "btn btn-small btn-ghost";

    const periodText = activeStartDate
      ? `De ${formatDateBr(activeStartDate)} até hoje`
      : "Todo o histórico";
    $("historyPeriodBadge").textContent = periodText;
    $("sadPeriodTag").textContent = periodText;
  }

  function renderDashboard(data) {
    dashboardData = data;
    const products = data.produtos || [];
    const sb = getSupabaseConfig();
    const modeTag = sb.enabled ? "Nuvem (Supabase)" : "API Local";
    const ramTag = data.ram_mb ? ` • RAM ${data.ram_mb} MB` : "";

    setConnectionStatus(
      true,
      data.bot_online
        ? `Conectado ao Discord (${data.bot_user || "Buscador"}) • ${modeTag}${ramTag}`
        : `${modeTag} conectado`
    );

    $("kpiLinks").textContent = `${data.total_produtos} / ${data.max_links}`;
    $("kpiReadings").textContent = `${data.total_coletas ?? 0} coletas`;
    const distinctDays = data.dias_distintos_totais ?? 0;
    const daysEl = $("kpiDaysBadge");
    daysEl.textContent =
      distinctDays >= 5
        ? `✅ ${distinctDays} dias distintos (meta ≥ 5 dias OK)`
        : `${distinctDays} dia(s) distinto(s)`;
    daysEl.className = distinctDays >= 5 ? "kpi-sub text-green" : "kpi-sub";

    $("kpiSchedule").textContent = data.agendamento_global?.label
      ? `A cada ${data.agendamento_global.label}`
      : "—";
    $("kpiSync").textContent = data.atualizado_em_fmt || "Agora";
    $("kpiBotName").textContent = data.bot_user || "Discord Bot";
    $("slotsPill").textContent = `${data.total_produtos}/${data.max_links} vagas`;

    const isFull = data.total_produtos >= data.max_links;
    $("btnAddSubmit").disabled = isFull;
    $("inputUrl").disabled = isFull;
    $("inputAlias").disabled = isFull;
    $("inputUrl").placeholder = isFull
      ? "Limite de 5 links atingido — remova um produto para adicionar outro"
      : "https://www.buscape.com.br/... ou qualquer outra loja";

    if (!products.some((p) => p.id === selectedProductId)) {
      selectedProductId = products.length ? products[0].id : null;
    }

    updateDateFilterButtons();
    renderProductsList(products);
    renderSelectedDetail();
    renderSadRanking(data.ranking_sad || []);
  }

  function renderProductsList(products) {
    const list = $("productsList");
    if (!products.length) {
      list.innerHTML = `
        <div class="empty-state">
          Nenhum produto monitorado ainda.<br />
          Cole um link acima (dica: links do Buscapé ou Zoom já carregam 40 dias reais!) ou use <code>/adicionar</code> no Discord!
        </div>
      `;
      return;
    }

    list.innerHTML = products
      .map((p) => {
        const isActive = p.id === selectedProductId;
        const priceStr = formatMoney(p.ultimo_preco, p.moeda);
        const sad = p.sad || {};
        const sadIcon = { green: "🟢", orange: "🟡", blue: "🔵" }[sad.nivel] || "⚪";

        let varHtml = `<div class="product-var muted-text">${sadIcon} ${escapeHtml(sad.decisao || "MONITORAR")}</div>`;
        if (p.variacao_pct !== null && p.variacao_pct !== undefined) {
          if (p.variacao_pct < 0) {
            varHtml = `<div class="product-var text-green">▼ ${p.variacao_pct}% • ${sadIcon} ${escapeHtml(sad.decisao || "")}</div>`;
          } else if (p.variacao_pct > 0) {
            varHtml = `<div class="product-var text-orange">▲ +${p.variacao_pct}% • ${sadIcon} ${escapeHtml(sad.decisao || "")}</div>`;
          }
        }

        const schedBadgeClass = p.usa_agendamento_individual ? "badge badge-custom" : "badge";

        return `
          <article class="product-card ${isActive ? "active" : ""}" data-id="${p.id}">
            <div class="product-top">
              <div>
                <div class="product-title">${escapeHtml(p.nome)}</div>
                <div class="product-badges">
                  <span class="badge badge-id">#${p.id}</span>
                  <span class="badge">🏪 ${escapeHtml(p.loja)}</span>
                  <span class="${schedBadgeClass}">⏱️ ${escapeHtml(p.agendamento_efetivo_label)}</span>
                  <span class="badge">📅 ${sad.dias_distintos || 0} dia(s)</span>
                </div>
              </div>
              <div class="product-price-box">
                <div class="product-price">${escapeHtml(priceStr)}</div>
                ${varHtml}
              </div>
            </div>
            <div class="product-meta">
              <span>🕒 ${escapeHtml(p.atualizado_em_fmt || "Nunca")}</span>
              <div class="product-actions">
                <button class="btn btn-small btn-ghost" data-action="verify" data-id="${p.id}" title="Coletar preço agora">
                  ⚡ Varrer
                </button>
                <button class="btn btn-small btn-ghost" data-action="schedule" data-id="${p.id}" title="Definir frequência individual">
                  ⏱️
                </button>
                <a class="btn btn-small btn-ghost" href="${escapeHtml(p.url)}" target="_blank" rel="noopener noreferrer" data-stop="1">
                  ↗
                </a>
                <button class="btn btn-small btn-danger" data-action="delete" data-id="${p.id}" title="Remover produto">
                  🗑️
                </button>
              </div>
            </div>
          </article>
        `;
      })
      .join("");
  }

  function renderSelectedDetail() {
    const products = dashboardData?.produtos || [];
    const product = products.find((p) => p.id === selectedProductId);

    const detailStats = $("detailStats");
    const detailLink = $("detailLink");
    const chartEmpty = $("chartEmpty");
    const historyBody = $("historyBody");

    $("whatIfResult").classList.add("hidden");

    if (!product) {
      $("detailTitle").textContent = "Evolução de preços";
      $("detailSubtitle").textContent = "Selecione um produto para ver o gráfico e as coletas.";
      detailStats.classList.add("hidden");
      detailLink.classList.add("hidden");
      chartEmpty.classList.remove("hidden");
      if (priceChart) {
        priceChart.destroy();
        priceChart = null;
      }
      historyBody.innerHTML = `<tr><td colspan="3" class="muted-cell">Nenhuma coleta selecionada.</td></tr>`;
      renderSadPanel(null);
      return;
    }

    $("detailTitle").textContent = `#${product.id} — ${product.nome}`;
    $("detailSubtitle").textContent = `${product.loja} • Adicionado em ${formatDateBr(product.criado_em_data)} • ${product.agendamento_efetivo_label}`;
    detailLink.href = product.url;
    detailLink.classList.remove("hidden");

    const history = product.historico || [];
    const stats = product.stats;

    detailStats.classList.remove("hidden");
    $("statCurrent").textContent = formatMoney(product.ultimo_preco, product.moeda);
    $("statMin").textContent = stats ? formatMoney(stats.menor, product.moeda) : "—";
    $("statMax").textContent = stats ? formatMoney(stats.maior, product.moeda) : "—";
    $("statAvg").textContent = stats ? formatMoney(stats.media, product.moeda) : "—";

    renderSadPanel(product);

    if (!history.length) {
      chartEmpty.classList.remove("hidden");
      chartEmpty.textContent = activeStartDate
        ? `Nenhuma coleta a partir de ${formatDateBr(activeStartDate)}. Tente clicar em "Todo o histórico".`
        : "Ainda não há coletas de preço para este item. Clique em 'Varrer'.";
      if (priceChart) {
        priceChart.destroy();
        priceChart = null;
      }
      historyBody.innerHTML = `<tr><td colspan="3" class="muted-cell">Sem registros neste período.</td></tr>`;
      return;
    }

    chartEmpty.classList.add("hidden");
    renderChart(product, history);

    const desc = [...history].reverse();
    historyBody.innerHTML = desc
      .map(
        (h) => `
        <tr>
          <td>${escapeHtml(h.coletado_em_fmt)}</td>
          <td><strong>${escapeHtml(formatMoney(h.preco, h.moeda))}</strong></td>
          <td><code>${escapeHtml(h.fonte)}</code></td>
        </tr>
      `
      )
      .join("");
  }

  function renderSadPanel(product) {
    const badge = $("sadDecisionBadge");
    if (!product || !product.sad || !product.sad.disponivel) {
      badge.className = "decision-badge neutral";
      badge.textContent = "⚪ SEM DADOS NO PERÍODO";
      $("sadScoreText").textContent = "0 / 100";
      $("sadScoreFill").style.width = "0%";
      $("sadJustification").textContent =
        product?.sad?.justificativa ||
        "Selecione um produto monitorado para visualizar a recomendação do Mini-SAD.";
      $("sadTrend").textContent = "—";
      $("sadStdDev").textContent = "—";
      $("sadPosition").textContent = "—";
      $("sadDays").textContent = "0 dia(s)";
      return;
    }

    const sad = product.sad;
    const icon = { green: "🟢", orange: "🟡", blue: "🔵" }[sad.nivel] || "⚪";
    badge.className = `decision-badge ${sad.nivel || "neutral"}`;
    badge.textContent = `${icon} ${sad.decisao}`;

    $("sadScoreText").textContent = `${sad.score_oportunidade} / 100`;
    $("sadScoreFill").style.width = `${sad.score_oportunidade}%`;
    $("sadJustification").textContent = sad.justificativa;

    $("sadTrend").textContent = sad.tendencia_label;
    $("sadStdDev").textContent = `${formatMoney(sad.desvio_padrao, product.moeda)} (${sad.volatilidade_pct}%)`;
    $("sadPosition").textContent = `${sad.posicao_faixa_pct}% da faixa`;
    $("sadDays").textContent = sad.atingiu_minimo_5_dias
      ? `✅ ${sad.dias_distintos} dias`
      : `${sad.dias_distintos} dia(s)`;
  }

  function renderSadRanking(ranking) {
    const container = $("sadRankingList");
    if (!ranking.length) {
      container.innerHTML = `<div class="muted-text">Nenhum produto para ranquear ainda.</div>`;
      return;
    }
    const medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"];
    container.innerHTML = ranking
      .map((item, idx) => {
        const icon = { green: "🟢", orange: "🟡", blue: "🔵" }[item.nivel] || "⚪";
        return `
          <div class="ranking-item" data-rank-id="${item.id}">
            <div class="ranking-left">
              <span class="ranking-pos">${medals[idx] || `#${idx + 1}`}</span>
              <div style="min-width: 0;">
                <div class="ranking-name">#${item.id} ${escapeHtml(item.nome)}</div>
                <div class="ranking-sub">
                  ${icon} ${escapeHtml(item.decisao)} • ${escapeHtml(item.tendencia_label)} • ${item.dias_distintos} dia(s)
                </div>
              </div>
            </div>
            <div class="ranking-right">
              <div class="ranking-score">${item.score_oportunidade}/100</div>
              <div class="ranking-sub">${escapeHtml(formatMoney(item.preco_atual, item.moeda))}</div>
            </div>
          </div>
        `;
      })
      .join("");
  }

  function renderChart(product, history) {
    const ctx = $("priceChart").getContext("2d");
    const labels = history.map((h) => h.coletado_em_fmt.slice(0, 10));
    const values = history.map((h) => Number(h.preco));
    const avgVal = product.sad?.media;

    if (priceChart) priceChart.destroy();

    const gradient = ctx.createLinearGradient(0, 0, 0, 240);
    gradient.addColorStop(0, "rgba(99, 102, 241, 0.42)");
    gradient.addColorStop(1, "rgba(6, 182, 212, 0.02)");

    const datasets = [
      {
        label: `Preço (${product.moeda})`,
        data: values,
        borderColor: "#818cf8",
        backgroundColor: gradient,
        borderWidth: 2.5,
        pointRadius: values.length > 25 ? 2.5 : 4,
        pointHoverRadius: 6,
        pointBackgroundColor: "#06b6d4",
        pointBorderColor: "#0f172a",
        pointBorderWidth: 2,
        fill: true,
        tension: 0.28,
      },
    ];

    if (avgVal !== undefined && avgVal !== null && values.length >= 2) {
      datasets.push({
        label: "Média do período",
        data: values.map(() => Number(avgVal)),
        borderColor: "rgba(245, 158, 11, 0.7)",
        borderDash: [6, 6],
        borderWidth: 1.5,
        pointRadius: 0,
        fill: false,
      });
    }

    priceChart = new Chart(ctx, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: {
            display: datasets.length > 1,
            labels: { color: "#cbd5e1", font: { size: 11 } },
          },
          tooltip: {
            backgroundColor: "#0f172a",
            borderColor: "rgba(148, 163, 184, 0.25)",
            borderWidth: 1,
            padding: 10,
            callbacks: {
              label: (item) => ` ${item.dataset.label}: ${formatMoney(item.parsed.y, product.moeda)}`,
            },
          },
        },
        scales: {
          x: {
            ticks: { color: "#94a3b8", maxRotation: 25, font: { size: 11 } },
            grid: { color: "rgba(148, 163, 184, 0.08)" },
          },
          y: {
            ticks: {
              color: "#94a3b8",
              font: { size: 11 },
              callback: (v) => formatMoney(v, product.moeda),
            },
            grid: { color: "rgba(148, 163, 184, 0.08)" },
          },
        },
      },
    });
  }

  async function loadDashboard(showSuccessToast = false) {
    const btn = $("btnRefresh");
    btn.disabled = true;
    const sb = getSupabaseConfig();

    try {
      if (sb.enabled) {
        const rows = await supabaseFetch("/rest/v1/buscador_estado?id=eq.principal&select=payload,atualizado_em");
        const row = Array.isArray(rows) ? rows[0] : null;
        if (!row || !row.payload || !Object.keys(row.payload).length) {
          throw new Error("O bot ainda não enviou dados ao Supabase. Verifique SUPABASE_URL e SUPABASE_KEY na Discloud.");
        }
        rawCloudPayload = row.payload;
        renderDashboard(applyClientDateFilter(rawCloudPayload, activeStartDate));
      } else {
        const qs = activeStartDate ? `?data_inicio=${encodeURIComponent(activeStartDate)}` : "";
        const data = await apiFetch(`/api/produtos${qs}`);
        renderDashboard(data);
      }

      if (showSuccessToast) {
        showToast("Dados sincronizados com o bot!", "success");
      }
    } catch (err) {
      setConnectionStatus(false, sb.enabled ? "Supabase desconectado" : `Desconectado (${getApiBase()})`);
      $("connectionBannerMsg").textContent =
        `Falha ao conectar (${err.message}). Clique em ⚙️ para configurar o Supabase (Vercel + Discloud) ou API local.`;
      if (showSuccessToast) {
        showToast(`Erro ao atualizar: ${err.message}`, "error");
      }
    } finally {
      btn.disabled = false;
    }
  }

  async function handleAddSubmit(event) {
    event.preventDefault();
    const url = $("inputUrl").value.trim();
    const apelido = $("inputAlias").value.trim();
    if (!url) return;

    const btn = $("btnAddSubmit");
    const prevText = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = "<span>⏳ Lendo preço...</span>";

    const sb = getSupabaseConfig();
    try {
      if (sb.enabled) {
        const resp = await sendSupabaseCommand("adicionar", { url, apelido });
        $("inputUrl").value = "";
        $("inputAlias").value = "";
        selectedProductId = resp.produto_id ?? selectedProductId;
        activeStartDate = "";
        await loadDashboard(false);
        if (resp.dias_historicos_importados > 0) {
          showToast(`Produto adicionado com +${resp.dias_historicos_importados} dias reais de histórico!`, "success");
        } else if (resp.scrape_error) {
          showToast(`Produto salvo, mas sem preço agora: ${resp.scrape_error}`, "info");
        } else {
          showToast("Produto adicionado pelo bot na Discloud!", "success");
        }
      } else {
        const resp = await apiFetch("/api/produtos", {
          method: "POST",
          body: JSON.stringify({ url, apelido }),
        });
        $("inputUrl").value = "";
        $("inputAlias").value = "";
        selectedProductId = resp.produto?.id ?? selectedProductId;
        activeStartDate = "";
        renderDashboard(resp.dashboard);
        if (resp.dias_historicos_importados > 0) {
          showToast(`Produto adicionado com +${resp.dias_historicos_importados} dias reais de histórico!`, "success");
        } else if (resp.scrape_error) {
          showToast(`Produto salvo, mas sem preço agora: ${resp.scrape_error}`, "info");
        } else {
          showToast("Produto adicionado e sincronizado com o Discord!", "success");
        }
      }
    } catch (err) {
      showToast(err.message, "error");
    } finally {
      btn.disabled = false;
      btn.innerHTML = prevText;
    }
  }

  async function handleVerifyAll() {
    const btn = $("btnScrapeAll");
    const prev = btn.innerHTML;
    btn.disabled = true;
    btn.innerHTML = "<span>⏳ Varrendo...</span>";
    const sb = getSupabaseConfig();

    try {
      const resp = sb.enabled
        ? await sendSupabaseCommand("verificar")
        : await apiFetch("/api/verificar", { method: "POST" });
      await loadDashboard(false);
      const okCount = (resp.resultados || []).filter((r) => r.ok).length;
      const total = (resp.resultados || []).length;
      showToast(`Varredura concluída: ${okCount}/${total} preços coletados!`, "success");
    } catch (err) {
      showToast(`Falha na varredura: ${err.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.innerHTML = prev;
    }
  }

  async function handleVerifyOne(id) {
    showToast(`Coletando preço do produto #${id}...`, "info");
    const sb = getSupabaseConfig();
    try {
      const resp = sb.enabled
        ? await sendSupabaseCommand("verificar_um", { produto_id: id })
        : await apiFetch(`/api/verificar/${id}`, { method: "POST" });
      await loadDashboard(false);
      if (resp.ok) {
        showToast(`Produto #${id} atualizado: ${formatMoney(resp.preco, resp.moeda)}`, "success");
      } else {
        showToast(`Não foi possível ler o preço agora (${resp.erro || "bloqueado"}).`, "error");
      }
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  async function handleDelete(id) {
    if (!confirm(`Remover o produto #${id} e todo o seu histórico?`)) return;
    const sb = getSupabaseConfig();
    try {
      if (sb.enabled) {
        await sendSupabaseCommand("remover", { produto_id: id });
      } else {
        await apiFetch(`/api/produtos/${id}`, { method: "DELETE" });
      }
      await loadDashboard(false);
      showToast(`Produto #${id} removido.`, "success");
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  function downloadCsvClientSide() {
    const products = dashboardData?.produtos || [];
    const lines = ["produto_id;produto_nome;loja;preco;moeda;fonte;coletado_em;url"];
    for (const p of products) {
      for (const h of p.historico || []) {
        const safeName = String(p.nome || "").replaceAll(";", ",");
        lines.push(
          `${p.id};${safeName};${p.loja};${Number(h.preco).toFixed(2)};${h.moeda};${h.fonte};${h.coletado_em};${p.url}`
        );
      }
    }
    const blob = new Blob(["\uFEFF" + lines.join("\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "historico_precos.csv";
    a.click();
    URL.revokeObjectURL(url);
  }

  function handleWhatIfSubmit(e) {
    e.preventDefault();
    const products = dashboardData?.produtos || [];
    const product = products.find((p) => p.id === selectedProductId);
    const out = $("whatIfResult");
    if (!product || product.ultimo_preco === null || product.ultimo_preco === undefined) {
      showToast("Selecione um produto com preço coletado para simular.", "error");
      return;
    }
    const target = Number($("inputTargetPrice").value);
    if (!target || target <= 0) {
      showToast("Informe um preço-alvo válido.", "error");
      return;
    }

    const atual = Number(product.ultimo_preco);
    const menor = product.sad?.menor_preco;
    const moeda = product.moeda;
    const diff = atual - target;
    const pct = atual > 0 ? (diff / atual) * 100 : 0;

    out.classList.remove("hidden", "hit");
    if (atual <= target) {
      out.classList.add("hit");
      out.textContent = `✅ Meta atingida! O preço atual (${formatMoney(atual, moeda)}) já está igual ou ${formatMoney(target - atual, moeda)} abaixo da sua meta (${formatMoney(target, moeda)}).`;
    } else {
      let viabilidade = "";
      if (menor !== undefined && menor !== null) {
        viabilidade =
          target >= menor
            ? ` Meta realista: o item já custou ${formatMoney(menor, moeda)} no período.`
            : ` Meta agressiva: abaixo da mínima registrada (${formatMoney(menor, moeda)}).`;
      }
      out.textContent = `📉 Falta cair ${formatMoney(diff, moeda)} (${pct.toFixed(1)}%) para atingir ${formatMoney(target, moeda)}.${viabilidade}`;
    }
  }

  function openScheduleModal(scope, product = null) {
    $("schedScope").value = scope;
    $("schedProductId").value = product ? String(product.id) : "";

    if (scope === "individual" && product) {
      $("scheduleModalTitle").textContent = `Agendar produto #${product.id}`;
      $("scheduleModalDesc").textContent = `Defina uma frequência exclusiva para "${product.nome}".`;
      $("btnResetToGlobal").classList.remove("hidden");
    } else {
      $("scheduleModalTitle").textContent = "Agendamento global";
      $("scheduleModalDesc").textContent =
        "Aplica a todos os produtos que não possuem regra individual.";
      $("btnResetToGlobal").classList.add("hidden");
    }
    $("scheduleModal").classList.remove("hidden");
  }

  async function submitSchedule(payload) {
    const sb = getSupabaseConfig();
    try {
      if (sb.enabled) {
        await sendSupabaseCommand("agendar", payload);
      } else {
        await apiFetch("/api/agendar", {
          method: "PATCH",
          body: JSON.stringify(payload),
        });
      }
      await loadDashboard(false);
      $("scheduleModal").classList.add("hidden");
      showToast("Agendamento atualizado com sucesso!", "success");
    } catch (err) {
      showToast(err.message, "error");
    }
  }

  function openSettingsModal() {
    $("cfgSupabaseUrl").value =
      localStorage.getItem(STORAGE_SB_URL) || window.BUSCADOR_CONFIG?.SUPABASE_URL || "";
    $("cfgSupabaseKey").value =
      localStorage.getItem(STORAGE_SB_KEY) || window.BUSCADOR_CONFIG?.SUPABASE_ANON_KEY || "";
    $("cfgApiUrl").value =
      localStorage.getItem(STORAGE_API_URL) || window.BUSCADOR_CONFIG?.API_BASE_URL || "";
    $("cfgApiKey").value = localStorage.getItem(STORAGE_API_KEY) || "";
    $("settingsModal").classList.remove("hidden");
  }

  function applyFilterChange() {
    const sb = getSupabaseConfig();
    if (sb.enabled && rawCloudPayload) {
      renderDashboard(applyClientDateFilter(rawCloudPayload, activeStartDate));
    } else {
      loadDashboard(false);
    }
  }

  // Event Listeners
  $("btnRefresh").addEventListener("click", () => loadDashboard(true));
  $("btnScrapeAll").addEventListener("click", handleVerifyAll);
  $("btnGlobalSchedule").addEventListener("click", () => openScheduleModal("global"));
  $("btnCsv").addEventListener("click", () => {
    const sb = getSupabaseConfig();
    if (sb.enabled) {
      downloadCsvClientSide();
    } else {
      const qs = activeStartDate ? `?data_inicio=${encodeURIComponent(activeStartDate)}` : "";
      window.open(`${getApiBase()}/api/exportar.csv${qs}`, "_blank");
    }
  });
  $("btnSettings").addEventListener("click", openSettingsModal);
  $("btnBannerConfig").addEventListener("click", openSettingsModal);
  $("addForm").addEventListener("submit", handleAddSubmit);
  $("whatIfForm").addEventListener("submit", handleWhatIfSubmit);

  // Filtros de data
  $("filterStartDate").addEventListener("change", (e) => {
    activeStartDate = e.target.value || "";
    applyFilterChange();
  });

  $("btnDateSinceAdded").addEventListener("click", () => {
    const products = dashboardData?.produtos || [];
    const product = products.find((p) => p.id === selectedProductId);
    if (!product?.criado_em_data) return;
    activeStartDate = product.criado_em_data;
    applyFilterChange();
  });

  $("btnDate5Days").addEventListener("click", () => {
    activeStartDate = daysAgoIso(5);
    applyFilterChange();
  });

  $("btnDateAll").addEventListener("click", () => {
    activeStartDate = "";
    applyFilterChange();
  });

  $("productsList").addEventListener("click", (e) => {
    const actionBtn = e.target.closest("[data-action]");
    if (actionBtn) {
      e.stopPropagation();
      const id = Number(actionBtn.dataset.id);
      const action = actionBtn.dataset.action;
      if (action === "verify") handleVerifyOne(id);
      if (action === "delete") handleDelete(id);
      if (action === "schedule") {
        const prod = (dashboardData?.produtos || []).find((p) => p.id === id);
        if (prod) openScheduleModal("individual", prod);
      }
      return;
    }

    if (e.target.closest("[data-stop]")) return;

    const card = e.target.closest(".product-card");
    if (card) {
      selectedProductId = Number(card.dataset.id);
      renderProductsList(dashboardData?.produtos || []);
      renderSelectedDetail();
    }
  });

  $("sadRankingList").addEventListener("click", (e) => {
    const item = e.target.closest("[data-rank-id]");
    if (item) {
      selectedProductId = Number(item.dataset.rankId);
      renderProductsList(dashboardData?.produtos || []);
      renderSelectedDetail();
    }
  });

  $("scheduleForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const escopo = $("schedScope").value;
    const valor = Number($("schedValue").value);
    const unidade = $("schedUnit").value;
    const produtoId = $("schedProductId").value ? Number($("schedProductId").value) : undefined;
    submitSchedule({ escopo, valor, unidade, produto_id: produtoId });
  });

  $("btnResetToGlobal").addEventListener("click", () => {
    const produtoId = Number($("schedProductId").value);
    if (!produtoId) return;
    submitSchedule({ escopo: "individual", valor: 0, unidade: "horas", produto_id: produtoId });
  });

  $("settingsForm").addEventListener("submit", (e) => {
    e.preventDefault();
    const sbUrl = $("cfgSupabaseUrl").value.trim().replace(/\/+$/, "");
    const sbKey = $("cfgSupabaseKey").value.trim();
    const url = $("cfgApiUrl").value.trim().replace(/\/+$/, "");
    const key = $("cfgApiKey").value.trim();

    if (sbUrl) localStorage.setItem(STORAGE_SB_URL, sbUrl);
    else localStorage.removeItem(STORAGE_SB_URL);

    if (sbKey) localStorage.setItem(STORAGE_SB_KEY, sbKey);
    else localStorage.removeItem(STORAGE_SB_KEY);

    if (url) localStorage.setItem(STORAGE_API_URL, url);
    else localStorage.removeItem(STORAGE_API_URL);

    if (key) localStorage.setItem(STORAGE_API_KEY, key);
    else localStorage.removeItem(STORAGE_API_KEY);

    $("settingsModal").classList.add("hidden");
    loadDashboard(true);
  });

  $("btnResetApi").addEventListener("click", () => {
    localStorage.removeItem(STORAGE_SB_URL);
    localStorage.removeItem(STORAGE_SB_KEY);
    localStorage.removeItem(STORAGE_API_URL);
    localStorage.removeItem(STORAGE_API_KEY);
    $("cfgSupabaseUrl").value = "";
    $("cfgSupabaseKey").value = "";
    $("cfgApiUrl").value = "";
    $("cfgApiKey").value = "";
    $("settingsModal").classList.add("hidden");
    loadDashboard(true);
  });

  document.querySelectorAll("[data-close]").forEach((btn) => {
    btn.addEventListener("click", () => {
      $(btn.dataset.close).classList.add("hidden");
    });
  });

  document.querySelectorAll(".modal-backdrop").forEach((backdrop) => {
    backdrop.addEventListener("click", (e) => {
      if (e.target === backdrop) backdrop.classList.add("hidden");
    });
  });

  loadDashboard(false);
})();
