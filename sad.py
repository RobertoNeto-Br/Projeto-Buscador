"""
Mini-SAD (Sistema de Apoio à Decisão) para Análise de Preços Históricos.

Calcula indicadores estatísticos (média, desvio padrão, volatilidade, posição na faixa,
tendência por regressão linear) e aplica regras multicritério para gerar uma recomendação
justificada de compra (COMPRAR AGORA, AGUARDAR QUEDA ou MONITORAR), além de análise
What-If (simulação de preço-alvo) e ranking comparativo entre produtos.
"""

from __future__ import annotations

import math
from typing import Optional


def _linear_slope(values: list[float]) -> float:
    """Calcula a inclinação (regressão linear simples) da série de preços."""
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den > 0 else 0.0


def _fmt_money(value: float, currency: str = "BRL") -> str:
    curr = (currency or "BRL").upper()
    if curr == "BRL":
        formatted = f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"R$ {formatted}"
    if curr == "USD":
        return f"US$ {value:,.2f}"
    if curr == "EUR":
        return f"€ {value:,.2f}"
    if curr == "GBP":
        return f"£ {value:,.2f}"
    return f"{curr} {value:,.2f}"


def analyze_product_history(
    history_asc: list[dict],
    currency: str = "BRL",
    target_price: Optional[float] = None,
) -> dict:
    """
    Executa a análise completa do Mini-SAD sobre o histórico (em ordem cronológica).
    """
    if not history_asc:
        return {
            "disponivel": False,
            "total_coletas": 0,
            "dias_distintos": 0,
            "atingiu_minimo_5_dias": False,
            "decisao": "SEM DADOS",
            "nivel": "neutral",
            "score_oportunidade": 0,
            "tendencia": "indefinida",
            "tendencia_label": "Indefinida",
            "justificativa": "Ainda não existem coletas no período selecionado. Execute /verificar ou ajuste a data.",
            "what_if": None,
        }

    prices = [float(h["preco"]) for h in history_asc]
    dates = {str(h.get("coletado_em", ""))[:10] for h in history_asc if h.get("coletado_em")}
    n = len(prices)
    dias_distintos = len(dates)

    preco_inicial = prices[0]
    preco_atual = prices[-1]
    menor = min(prices)
    maior = max(prices)
    media = sum(prices) / n
    amplitude = maior - menor

    variance = sum((p - media) ** 2 for p in prices) / n if n > 1 else 0.0
    desvio_padrao = math.sqrt(variance)
    volatilidade_pct = (desvio_padrao / media * 100.0) if media > 0 else 0.0

    variacao_periodo_pct = (
        ((preco_atual - preco_inicial) / preco_inicial) * 100.0 if preco_inicial > 0 else 0.0
    )
    diferenca_media_pct = ((preco_atual - media) / media) * 100.0 if media > 0 else 0.0
    posicao_faixa_pct = ((preco_atual - menor) / amplitude * 100.0) if amplitude > 0.001 else 50.0

    # Tendência por regressão linear normalizada em relação à média
    slope = _linear_slope(prices)
    slope_rel_pct = (slope / media * 100.0) if media > 0 else 0.0
    if n < 2 or abs(slope_rel_pct) < 0.15:
        tendencia = "estavel"
        tendencia_label = "Estável"
    elif slope_rel_pct < 0:
        tendencia = "em_queda"
        tendencia_label = "Em queda"
    else:
        tendencia = "em_alta"
        tendencia_label = "Em alta"

    # Score Multicritério (0 a 100):
    # 1) Posição relativa à média (45 pts): quanto mais abaixo da média, maior o score
    #    -10% abaixo da média => 45 pts | na média => 22.5 pts | +10% acima => 0 pts
    score_media = max(0.0, min(45.0, 22.5 - (diferenca_media_pct * 2.25)))

    # 2) Proximidade do menor preço do período (35 pts):
    #    0% (na mínima) => 35 pts | 100% (na máxima) => 0 pts
    score_faixa = max(0.0, min(35.0, (1.0 - (posicao_faixa_pct / 100.0)) * 35.0))

    # 3) Componente de tendência (20 pts):
    #    Em alta com preço baixo => bom comprar antes de subir mais (18 pts)
    #    Estável => neutro favorável (12 pts)
    #    Em queda com preço ainda alto => sinal de esperar cair mais (6 pts)
    if tendencia == "em_queda":
        score_tendencia = 14.0 if posicao_faixa_pct <= 20 else 6.0
    elif tendencia == "em_alta":
        score_tendencia = 18.0 if diferenca_media_pct <= 0 else 8.0
    else:
        score_tendencia = 13.0

    score = int(round(max(0.0, min(100.0, score_media + score_faixa + score_tendencia))))

    # Regra de Decisão Explícita
    if n == 1:
        decisao = "MONITORAR"
        nivel = "blue"
        justificativa = (
            f"Apenas 1 coleta registrada ({_fmt_money(preco_atual, currency)}). "
            "Continue coletando por mais dias para identificar tendência e média confiáveis."
        )
    elif (diferenca_media_pct <= -2.0) or (posicao_faixa_pct <= 25.0 and amplitude > 0.01) or score >= 65:
        decisao = "COMPRAR AGORA"
        nivel = "green"
        justificativa = (
            f"O preço atual ({_fmt_money(preco_atual, currency)}) está "
            f"{abs(diferenca_media_pct):.1f}% {'abaixo' if diferenca_media_pct <= 0 else 'acima'} da média "
            f"({_fmt_money(media, currency)}) e na faixa de {posicao_faixa_pct:.0f}% entre a mínima "
            f"({_fmt_money(menor, currency)}) e a máxima ({_fmt_money(maior, currency)}). "
            f"Tendência: {tendencia_label.lower()} — ótima oportunidade de compra."
        )
    elif (diferenca_media_pct >= 2.5) or (posicao_faixa_pct >= 70.0 and amplitude > 0.01) or (
        tendencia == "em_queda" and posicao_faixa_pct > 35.0
    ):
        decisao = "AGUARDAR QUEDA"
        nivel = "orange"
        justificativa = (
            f"O preço atual ({_fmt_money(preco_atual, currency)}) está "
            f"{abs(diferenca_media_pct):.1f}% {'acima' if diferenca_media_pct >= 0 else 'abaixo'} da média "
            f"({_fmt_money(media, currency)}), próximo ao topo do período (máxima de {_fmt_money(maior, currency)}). "
            f"Tendência: {tendencia_label.lower()} — recomenda-se aguardar novo recuo próximo de {_fmt_money(menor, currency)}."
        )
    else:
        decisao = "MONITORAR"
        nivel = "blue"
        justificativa = (
            f"O preço atual ({_fmt_money(preco_atual, currency)}) está próximo da média do período "
            f"({_fmt_money(media, currency)}, variação de {diferenca_media_pct:+.1f}%), com volatilidade de "
            f"{volatilidade_pct:.1f}%. Compra neutra se houver urgência; caso contrário, mantenha em monitoramento."
        )

    what_if = None
    if target_price is not None and target_price > 0:
        what_if = simulate_target_price(preco_atual, target_price, menor, currency)

    return {
        "disponivel": True,
        "total_coletas": n,
        "dias_distintos": dias_distintos,
        "atingiu_minimo_5_dias": dias_distintos >= 5,
        "preco_inicial": round(preco_inicial, 2),
        "preco_atual": round(preco_atual, 2),
        "menor_preco": round(menor, 2),
        "maior_preco": round(maior, 2),
        "media": round(media, 2),
        "desvio_padrao": round(desvio_padrao, 2),
        "amplitude": round(amplitude, 2),
        "volatilidade_pct": round(volatilidade_pct, 2),
        "variacao_periodo_pct": round(variacao_periodo_pct, 2),
        "diferenca_media_pct": round(diferenca_media_pct, 2),
        "posicao_faixa_pct": round(posicao_faixa_pct, 1),
        "tendencia": tendencia,
        "tendencia_label": tendencia_label,
        "score_oportunidade": score,
        "decisao": decisao,
        "nivel": nivel,
        "justificativa": justificativa,
        "what_if": what_if,
    }


def simulate_target_price(
    preco_atual: float,
    preco_alvo: float,
    menor_historico: Optional[float] = None,
    currency: str = "BRL",
) -> dict:
    """Análise What-If (Sensibilidade): avalia quão distante o preço atual está da meta do decisor."""
    diff = preco_atual - preco_alvo
    pct_queda_necessaria = (diff / preco_atual * 100.0) if preco_atual > 0 else 0.0
    atingida = preco_atual <= preco_alvo

    if atingida:
        economia = preco_alvo - preco_atual
        resumo = (
            f"Meta atingida! O preço atual ({_fmt_money(preco_atual, currency)}) já está "
            f"{_fmt_money(economia, currency)} abaixo/igual ao seu alvo ({_fmt_money(preco_alvo, currency)})."
        )
    else:
        viabilidade = ""
        if menor_historico is not None:
            if preco_alvo >= menor_historico:
                viabilidade = f" Meta realista: o produto já atingiu {_fmt_money(menor_historico, currency)} no histórico."
            else:
                viabilidade = f" Meta agressiva: abaixo da mínima histórica de {_fmt_money(menor_historico, currency)}."
        resumo = (
            f"Ainda falta cair {_fmt_money(diff, currency)} ({pct_queda_necessaria:.1f}%) "
            f"para atingir {_fmt_money(preco_alvo, currency)}.{viabilidade}"
        )

    return {
        "preco_alvo": round(preco_alvo, 2),
        "atingida": atingida,
        "diferenca": round(diff, 2),
        "queda_necessaria_pct": round(max(0.0, pct_queda_necessaria), 2),
        "resumo": resumo,
    }


def build_ranking(products_with_sad: list[dict]) -> list[dict]:
    """Ordena produtos do maior para o menor score de oportunidade de compra."""
    ranked = []
    for p in products_with_sad:
        sad = p.get("sad") or {}
        ranked.append(
            {
                "id": p["id"],
                "nome": p["nome"],
                "loja": p["loja"],
                "moeda": p.get("moeda", "BRL"),
                "preco_atual": p.get("ultimo_preco"),
                "media": sad.get("media"),
                "menor_preco": sad.get("menor_preco"),
                "dias_distintos": sad.get("dias_distintos", 0),
                "score_oportunidade": sad.get("score_oportunidade", 0),
                "decisao": sad.get("decisao", "SEM DADOS"),
                "nivel": sad.get("nivel", "neutral"),
                "tendencia_label": sad.get("tendencia_label", "Indefinida"),
            }
        )
    ranked.sort(key=lambda item: (item["score_oportunidade"], item["dias_distintos"]), reverse=True)
    return ranked
