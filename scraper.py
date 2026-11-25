import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from config import SCRAPEDO_TOKEN, SCRAPERAPI_KEY
from memory_manager import recycle_memory

logger = logging.getLogger("buscador.scraper")

MAX_HTML_CHARS = 1_800_000

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
    "Cache-Control": "no-cache",
    "Upgrade-Insecure-Requests": "1",
}

TRACKING_PARAM_PREFIXES = ("utm_", "scm", "pdp_", "fbclid", "gclid", "mc_", "yclid")
TRACKING_PARAM_EXACT = {
    "spm",
    "pvid",
    "aecmd",
    "gatewayadapt",
    "ref",
    "ref_",
    "srsltid",
    "tag",
    "linkcode",
    "pd_rd_r",
    "pd_rd_w",
    "pd_rd_wg",
    "pf_rd_p",
    "pf_rd_r",
    "qid",
    "sr",
    "th",
    "psc",
    "dib",
    "dib_tag",
    "keywords",
    "aff_fcid",
    "aff_fsk",
    "aff_platform",
    "sk",
    "aff_trace_key",
    "terminal_id",
}

NOISE_LINE_RE = re.compile(
    r"\b(\d+\s*x\s*(de)?|parcela\w*|juros|frete|shipping|cupom|coupon|"
    r"desconto|economize|save\b|off\b|acima\s+de|orders?\s+over|"
    r"pay\s+later|cashback|ganhe|extra\s+\d+%|min\.?\s*order)\b",
    re.IGNORECASE,
)

# Ordem importa: R$ e US$ antes de $ genérico
CURRENCY_PRICE_RE = re.compile(
    r"(?P<curr>R\$|BRL|US\s?\$|USD|€|EUR|£|GBP|\$)\s*"
    r"(?P<num>\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)",
    re.IGNORECASE,
)


@dataclass
class ScrapeResult:
    title: Optional[str] = None
    price: Optional[float] = None
    currency: str = "BRL"
    source: str = "desconhecida"
    store: str = "desconhecida"
    error: Optional[str] = None
    historical_points: list[tuple[str, float]] = field(default_factory=list)


def extract_store_name(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "desconhecida"


def clean_product_url(url: str) -> str:
    """Remove parâmetros de rastreamento que quebram proxies/readers e normaliza lojas conhecidas."""
    url = url.strip()
    parsed = urlparse(url)
    host = parsed.netloc.lower()

    # AliExpress: /item/1005008518479099.html sem dezenas de params de tracking
    if "aliexpress" in host:
        m = re.search(r"(/item/\d+\.html)", parsed.path)
        if m:
            return urlunparse((parsed.scheme, parsed.netloc, m.group(1), "", "", ""))

    # Amazon: /dp/ASIN
    if "amazon." in host:
        m = re.search(r"(/dp/[A-Z0-9]{10})", parsed.path)
        if m:
            return urlunparse((parsed.scheme, parsed.netloc, m.group(1), "", "", ""))

    # Genérico: remove apenas parâmetros de rastreamento conhecidos
    if parsed.query:
        kept = []
        for k, v in parse_qsl(parsed.query, keep_blank_values=False):
            kl = k.lower()
            if kl in TRACKING_PARAM_EXACT or any(kl.startswith(p) for p in TRACKING_PARAM_PREFIXES):
                continue
            kept.append((k, v))
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", urlencode(kept), ""))

    return url


def format_brl(value: Optional[float], currency: str = "BRL") -> str:
    if value is None:
        return "Indisponível"
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


def _normalize_currency(token: str) -> str:
    t = token.strip().upper().replace(" ", "")
    if t in ("R$", "BRL"):
        return "BRL"
    if t in ("US$", "USD", "$"):
        return "USD"
    if t in ("€", "EUR"):
        return "EUR"
    if t in ("£", "GBP"):
        return "GBP"
    return "BRL"


def parse_price(raw: Any) -> Optional[float]:
    """Converte textos/números variados de preço (BR ou US) para float."""
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
        return round(val, 2) if 0.5 <= val <= 500_000 else None

    text = str(raw).strip()
    if not text:
        return None

    text = (
        text.replace("\xa0", " ")
        .replace("R$", "")
        .replace("US$", "")
        .replace("BRL", "")
        .replace("USD", "")
        .replace("€", "")
        .replace("£", "")
        .replace("$", "")
        .strip()
    )

    match = re.search(r"(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)", text)
    if not match:
        return None

    num = match.group(1)

    if "," in num and "." in num:
        if num.rfind(",") > num.rfind("."):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "," in num:
        parts = num.split(",")
        if len(parts[-1]) in (1, 2):
            num = num.replace(".", "").replace(",", ".")
        else:
            num = num.replace(",", "")
    elif "." in num:
        parts = num.split(".")
        if len(parts[-1]) == 3 and len(parts) >= 2:
            num = num.replace(".", "")

    try:
        val = float(num)
        return round(val, 2) if 0.5 <= val <= 500_000 else None
    except ValueError:
        return None


def _clean_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    cleaned = re.sub(r"\s+", " ", str(title)).strip()
    return cleaned[:140] if cleaned else None


def _extract_from_json_ld(soup: BeautifulSoup) -> tuple[Optional[str], Optional[float], str]:
    title: Optional[str] = None
    price: Optional[float] = None
    currency = "BRL"

    def walk(node: Any) -> None:
        nonlocal title, price, currency
        if isinstance(node, list):
            for item in node:
                walk(item)
            return
        if not isinstance(node, dict):
            return

        node_type = node.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        types_lower = [str(t).lower() for t in types if t]

        if any("product" in t for t in types_lower):
            if not title and node.get("name"):
                title = _clean_title(node.get("name"))

            offers = node.get("offers")
            if offers:
                offer_list = offers if isinstance(offers, list) else [offers]
                for offer in offer_list:
                    if not isinstance(offer, dict):
                        continue
                    for key in ("price", "lowPrice", "highPrice"):
                        if offer.get(key) is not None and price is None:
                            parsed = parse_price(offer.get(key))
                            if parsed is not None:
                                price = parsed
                                currency = str(offer.get("priceCurrency") or currency).upper()
                                break
                    spec = offer.get("priceSpecification")
                    if price is None and isinstance(spec, dict):
                        parsed = parse_price(spec.get("price"))
                        if parsed is not None:
                            price = parsed
                            currency = str(spec.get("priceCurrency") or currency).upper()
                    nested_offers = offer.get("offers")
                    if price is None and nested_offers:
                        walk({"@type": "Product", "offers": nested_offers})

        if "@graph" in node:
            walk(node["@graph"])

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw_json = script.string or script.get_text()
        if not raw_json or not raw_json.strip():
            continue
        try:
            data = json.loads(raw_json)
            walk(data)
        except Exception:
            continue
        if price is not None:
            break

    return title, price, currency


def _extract_from_next_data(soup: BeautifulSoup) -> tuple[Optional[str], Optional[float], str]:
    """Lê dados embutidos do Next.js (__NEXT_DATA__), comum em Buscapé, Zoom, KaBuM, Magalu."""
    script = soup.find("script", id="__NEXT_DATA__")
    if not script:
        return None, None, "BRL"

    raw_json = script.string or script.get_text()
    if not raw_json:
        return None, None, "BRL"

    try:
        data = json.loads(raw_json)
    except Exception:
        return None, None, "BRL"

    found_title: Optional[str] = None
    found_price: Optional[float] = None

    price_keys = (
        "price",
        "lowestPrice",
        "minPrice",
        "bestPrice",
        "priceWithDiscount",
        "salePrice",
        "sellingPrice",
        "spotPrice",
    )
    title_keys = ("productName", "title", "name")

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal found_title, found_price
        if depth > 14 or (found_title and found_price is not None):
            return
        if isinstance(node, list):
            for item in node[:40]:
                walk(item, depth + 1)
        elif isinstance(node, dict):
            if not found_title:
                for tk in title_keys:
                    val = node.get(tk)
                    if isinstance(val, str) and len(val.strip()) > 8:
                        found_title = _clean_title(val)
                        break
            if found_price is None:
                for pk in price_keys:
                    if pk in node:
                        val = node[pk]
                        if isinstance(val, (int, float, str)):
                            parsed = parse_price(val)
                            if parsed is not None:
                                found_price = parsed
                                break
                        elif isinstance(val, dict) and "value" in val:
                            parsed = parse_price(val.get("value"))
                            if parsed is not None:
                                found_price = parsed
                                break
            for v in node.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)

    walk(data)
    return found_title, found_price, "BRL"


def _extract_from_embedded_scripts(soup: BeautifulSoup) -> tuple[Optional[float], str]:
    """Busca campos JSON de preço embutidos em scripts (VTEX, Shopify, WooCommerce, Nuxt, etc.)."""
    key_patterns = (
        r'"(?:lowPrice|lowestPrice|bestPrice|sellingPrice|spotPrice|salePrice|priceWithDiscount|minPrice|actSkuCalPrice)"\s*:\s*"?(\d+(?:[.,]\d{1,2})?)"?',
        r'"(?:formattedActivityPrice|formatedAmount|formattedPrice)"\s*:\s*"([^"]{2,30})"',
    )
    for script in soup.find_all("script"):
        text = script.string or script.get_text()
        if not text or len(text) < 20 or len(text) > 500_000:
            continue
        for pat in key_patterns:
            m = re.search(pat, text)
            if m:
                raw_val = m.group(1)
                curr = "BRL"
                cm = CURRENCY_PRICE_RE.search(raw_val)
                if cm:
                    curr = _normalize_currency(cm.group("curr"))
                val = parse_price(raw_val)
                if val is not None:
                    # VTEX às vezes guarda preço em centavos (ex: 19990 para 199.90) em bestPrice/sellingPrice
                    if "bestPrice" in m.group(0) or "sellingPrice" in m.group(0):
                        if isinstance(raw_val, str) and "." not in raw_val and "," not in raw_val and val >= 10000:
                            val = round(val / 100.0, 2)
                    return val, curr
    return None, "BRL"


def _extract_from_meta_tags(soup: BeautifulSoup) -> tuple[Optional[str], Optional[float], str]:
    title: Optional[str] = None
    price: Optional[float] = None
    currency = "BRL"

    for prop in ("og:title", "twitter:title"):
        tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
        if tag and tag.get("content"):
            title = _clean_title(tag["content"])
            break

    if not title:
        h1 = soup.find("h1")
        if h1:
            title = _clean_title(h1.get_text(" ", strip=True))
    if not title and soup.title:
        title = _clean_title(soup.title.get_text(" ", strip=True))

    price_meta_keys = (
        ("property", "product:price:amount"),
        ("property", "og:price:amount"),
        ("name", "product:price:amount"),
        ("name", "twitter:data1"),
        ("itemprop", "price"),
    )
    for attr, key in price_meta_keys:
        tag = soup.find("meta", attrs={attr: key})
        if tag and tag.get("content"):
            parsed = parse_price(tag["content"])
            if parsed is not None:
                price = parsed
                break

    curr_tag = (
        soup.find("meta", attrs={"property": "product:price:currency"})
        or soup.find("meta", attrs={"property": "og:price:currency"})
        or soup.find("meta", attrs={"itemprop": "priceCurrency"})
    )
    if curr_tag and curr_tag.get("content"):
        currency = str(curr_tag["content"]).strip().upper()

    if price is None:
        itemprop_el = soup.find(attrs={"itemprop": "price"})
        if itemprop_el:
            raw = itemprop_el.get("content") or itemprop_el.get_text(" ", strip=True)
            price = parse_price(raw)

    return title, price, currency


def _extract_from_html_selectors(soup: BeautifulSoup, store: str) -> tuple[Optional[float], str]:
    selectors: list[str] = []

    if "amazon" in store:
        selectors.extend([
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            "#corePrice_feature_div .a-price .a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            ".a-price .a-offscreen",
        ])
    elif "mercadolivre" in store or "mercadolibre" in store:
        container = soup.select_one(".ui-pdp-price__second-line .andes-money-amount")
        if container:
            frac = container.select_one(".andes-money-amount__fraction")
            cents = container.select_one(".andes-money-amount__cents")
            if frac:
                raw = frac.get_text(strip=True)
                if cents:
                    raw = f"{raw},{cents.get_text(strip=True)}"
                parsed = parse_price(raw)
                if parsed is not None:
                    return parsed, "BRL"
        selectors.extend([
            ".ui-pdp-price__second-line .andes-money-amount__fraction",
            ".price-tag-fraction",
        ])
    elif "kabum" in store:
        selectors.extend(["h4.finalPrice", "[class*='finalPrice']"])
    elif "magazineluiza" in store or "magalu" in store:
        selectors.extend(["[data-testid='price-value']", "[class*='price-template']"])
    elif "buscape" in store or "zoom" in store:
        selectors.extend(["[data-testid='product-price']", "[class*='Price_Value']", "[class*='price']"])

    selectors.extend([
        "[data-testid*='price']",
        "[class*='sale-price']",
        "[class*='best-price']",
        "[class*='product-price']",
        "[id*='product-price']",
    ])

    for selector in selectors:
        for el in soup.select(selector)[:5]:
            text = el.get_text(" ", strip=True)
            if NOISE_LINE_RE.search(text):
                continue
            cm = CURRENCY_PRICE_RE.search(text)
            curr = _normalize_currency(cm.group("curr")) if cm else "BRL"
            parsed = parse_price(cm.group("num") if cm else text)
            if parsed is not None:
                return parsed, curr

    return None, "BRL"


async def _try_mercadolivre_api(client: httpx.AsyncClient, url: str) -> Optional[ScrapeResult]:
    match = re.search(r"(MLB)-?(\d{6,})", url, re.IGNORECASE)
    if not match:
        return None

    item_id = f"{match.group(1).upper()}{match.group(2)}"
    api_url = f"https://api.mercadolibre.com/items/{item_id}"
    try:
        resp = await client.get(api_url, timeout=12.0)
        if resp.status_code == 200:
            data = resp.json()
            price = parse_price(data.get("price"))
            title = _clean_title(data.get("title"))
            currency = str(data.get("currency_id") or "BRL").upper()
            if price is not None:
                return ScrapeResult(
                    title=title,
                    price=price,
                    currency=currency,
                    source="mercadolivre_api",
                    store=extract_store_name(url),
                )
    except Exception as exc:
        logger.debug("Falha na API pública do Mercado Livre (%s): %s", item_id, exc)
    return None


def _extract_price_from_markdown(text: str) -> tuple[Optional[str], Optional[float], str]:
    """Extrai título e preço principal de Markdown renderizado (Jina Reader), multi-moeda."""
    title_match = re.search(r"^Title:\s*(.+)$", text, re.MULTILINE)
    title = _clean_title(title_match.group(1)) if title_match else None

    # Ignora páginas de erro/bloqueio
    lowered_head = text[:1200].lower()
    if any(
        bad in lowered_head
        for bad in (
            "title: 404",
            "page not found",
            "_____tmd_____/punish",
            "access denied",
            "verify you are human",
        )
    ):
        return None, None, "BRL"

    lines = text.splitlines()

    # Localiza o título principal do produto (# Heading longo, ignorando logo/menu como "# Aliexpress")
    h1_index = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# "):
            heading_text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", stripped[2:]).strip()
            if len(heading_text) >= 15 and not heading_text.lower().startswith("ai overview"):
                h1_index = i
                if not title:
                    title = _clean_title(heading_text)
                break

    def scan_slice(slice_lines: list[str]) -> Optional[tuple[float, str]]:
        stop_sections = (
            "ai overview",
            "related products",
            "more to love",
            "você também pode gostar",
            "quem viu",
            "produtos similares",
            "customer reviews",
            "avaliações dos clientes",
        )
        for line in slice_lines:
            clean_line = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", line)
            clean_line = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", clean_line).strip()
            if not clean_line:
                continue
            low = clean_line.lower()
            if any(sec in low for sec in stop_sections):
                break
            if NOISE_LINE_RE.search(clean_line):
                continue
            for m in CURRENCY_PRICE_RE.finditer(clean_line):
                val = parse_price(m.group("num"))
                if val is not None:
                    return val, _normalize_currency(m.group("curr"))
        return None

    # 1ª tentativa: janela logo abaixo do título do produto (alta precisão)
    if h1_index != -1:
        found = scan_slice(lines[h1_index + 1 : h1_index + 60])
        if found:
            return title, found[0], found[1]

    # 2ª tentativa: documento inteiro após o cabeçalho do Jina ("Markdown Content:")
    body_start = 0
    for i, line in enumerate(lines[:30]):
        if line.strip().startswith("Markdown Content:"):
            body_start = i + 1
            break
    found = scan_slice(lines[body_start : body_start + 250])
    if found:
        return title, found[0], found[1]

    return title, None, "BRL"


async def _try_jina_reader(client: httpx.AsyncClient, url: str, store: str) -> Optional[ScrapeResult]:
    """Fallback gratuito (r.jina.ai) com renderização JS para qualquer site."""
    clean_url = clean_product_url(url)
    urls_to_try = [clean_url]
    if url != clean_url:
        urls_to_try.append(url)

    for target in urls_to_try:
        jina_url = f"https://r.jina.ai/{target}"
        try:
            resp = await client.get(
                jina_url,
                headers={"Accept": "text/plain"},
                timeout=30.0,
            )
            if resp.status_code != 200 or not resp.text:
                continue

            title, price, currency = _extract_price_from_markdown(resp.text)
            if price is not None:
                return ScrapeResult(
                    title=title,
                    price=price,
                    currency=currency,
                    source="jina_reader",
                    store=store,
                )
        except Exception as exc:
            logger.debug("Falha no fallback Jina Reader para %s: %s", target, exc)

    return None


async def _try_scrapedo(client: httpx.AsyncClient, url: str, store: str) -> Optional[ScrapeResult]:
    """Fallback opcional Scrape.do (1.000 req/mês grátis permanente, com JS + geolocalização BR)."""
    if not SCRAPEDO_TOKEN:
        return None
    target = clean_product_url(url)
    api_url = (
        f"https://api.scrape.do/?token={SCRAPEDO_TOKEN}"
        f"&url={quote(target, safe='')}&render=true&geoCode=br"
    )
    try:
        resp = await client.get(api_url, timeout=40.0)
        if resp.status_code == 200 and resp.text:
            parsed = _parse_html(resp.text, store)
            if parsed.price is not None:
                parsed.source = f"scrapedo+{parsed.source}"
                return parsed
    except Exception as exc:
        logger.debug("Erro no fallback Scrape.do (%s): %s", target, exc)
    return None


def _extract_buscape_zoom_next_data(soup: BeautifulSoup, store: str) -> Optional[ScrapeResult]:
    """Extrai nome, preço atual e histórico diário real (até 40 dias) de páginas do Buscapé e Zoom."""
    if "buscape" not in store and "zoom" not in store:
        return None

    script = soup.find("script", id="__NEXT_DATA__")
    if not script:
        return None

    raw_json = script.string or script.get_text()
    if not raw_json:
        return None

    try:
        data = json.loads(raw_json)
    except Exception:
        return None

    redux = data.get("props", {}).get("initialReduxState", {})
    prod_info = redux.get("products", {}).get("product", {}).get("product", {})
    title = _clean_title(prod_info.get("name"))
    current_price = parse_price(prod_info.get("minPrice") or prod_info.get("price"))

    # Extrai série histórica diária real (ex: 40 dias)
    points: list[tuple[str, float]] = []
    ph_map = redux.get("priceHistory", {}).get("priceHistory", {})
    if isinstance(ph_map, dict) and ph_map:
        first_entry = next(iter(ph_map.values()), {})
        days = first_entry.get("days", []) if isinstance(first_entry, dict) else []
        seen_dates: set[str] = set()
        for item in days:
            if not isinstance(item, dict):
                continue
            d_str = str(item.get("date") or "").strip()[:10]
            p_val = parse_price(item.get("price"))
            if re.match(r"^\d{4}-\d{2}-\d{2}$", d_str) and p_val is not None and d_str not in seen_dates:
                seen_dates.add(d_str)
                points.append((d_str, p_val))
        points.sort(key=lambda x: x[0])

    if current_price is None and points:
        current_price = points[-1][1]

    if current_price is not None:
        return ScrapeResult(
            title=title,
            price=current_price,
            currency="BRL",
            source="buscape_zoom_historico" if points else "next_data",
            store=store,
            historical_points=points,
        )
    return None


def _parse_html(html: str, store: str) -> ScrapeResult:
    if len(html) > MAX_HTML_CHARS:
        html = html[:MAX_HTML_CHARS]

    soup = BeautifulSoup(html, "html.parser")
    try:
        bz = _extract_buscape_zoom_next_data(soup, store)
        if bz and bz.price is not None:
            return bz

        title, price, currency = _extract_from_json_ld(soup)
        if price is not None:
            return ScrapeResult(title=title, price=price, currency=currency, source="json_ld", store=store)

        nd_title, nd_price, nd_currency = _extract_from_next_data(soup)
        title = title or nd_title
        if nd_price is not None:
            return ScrapeResult(title=title, price=nd_price, currency=nd_currency, source="next_data", store=store)

        meta_title, meta_price, meta_currency = _extract_from_meta_tags(soup)
        title = title or meta_title
        if meta_price is not None:
            return ScrapeResult(title=title, price=meta_price, currency=meta_currency, source="meta_tags", store=store)

        emb_price, emb_currency = _extract_from_embedded_scripts(soup)
        if emb_price is not None:
            return ScrapeResult(title=title, price=emb_price, currency=emb_currency, source="script_json", store=store)

        sel_price, sel_currency = _extract_from_html_selectors(soup, store)
        if sel_price is not None:
            return ScrapeResult(title=title, price=sel_price, currency=sel_currency, source="html_selector", store=store)

        return ScrapeResult(title=title, price=None, currency="BRL", source="nenhuma", store=store)
    finally:
        # Quebra todas as referências cíclicas do DOM para evitar vazamento de RAM
        soup.decompose()


async def fetch_product_info(url: str) -> ScrapeResult:
    """Extrai título e preço de qualquer link de produto usando múltiplas camadas."""
    url = url.strip()
    store = extract_store_name(url)

    limits = httpx.Limits(max_connections=4, max_keepalive_connections=0)
    try:
        async with httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=20.0,
            limits=limits,
        ) as client:
            # Camada 1: API oficial grátis do Mercado Livre (quando aplicável)
            if "mercadolivre" in store or "mercadolibre" in store:
                ml_result = await _try_mercadolivre_api(client, url)
                if ml_result and ml_result.price is not None:
                    return ml_result

            # Camada 2: Requisição HTTP direta + extrator universal (Buscapé/Zoom, JSON-LD, Next.js, Meta, Scripts, CSS)
            best_title: Optional[str] = None
            last_error: Optional[str] = None
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.text:
                    html_text = resp.text
                    await resp.aclose()
                    parsed = _parse_html(html_text, store)
                    del html_text
                    best_title = parsed.title
                    if parsed.price is not None:
                        return parsed
                else:
                    last_error = f"HTTP {resp.status_code}"
                    await resp.aclose()
            except Exception as exc:
                last_error = str(exc)
                logger.debug("Erro no fetch direto (%s): %s", url, exc)

            # Camada 3 (opcional): Scrape.do grátis (1.000/mês com JS + BR)
            scrapedo_result = await _try_scrapedo(client, url, store)
            if scrapedo_result and scrapedo_result.price is not None:
                scrapedo_result.title = scrapedo_result.title or best_title
                return scrapedo_result

            # Camada 4 (opcional): ScraperAPI (se chave configurada no .env)
            if SCRAPERAPI_KEY:
                try:
                    target = clean_product_url(url)
                    api_url = (
                        f"https://api.scraperapi.com/?api_key={SCRAPERAPI_KEY}"
                        f"&url={quote(target, safe='')}&country_code=br"
                    )
                    resp = await client.get(api_url, timeout=30.0)
                    if resp.status_code == 200 and resp.text:
                        html_text = resp.text
                        await resp.aclose()
                        parsed = _parse_html(html_text, store)
                        del html_text
                        if parsed.price is not None:
                            parsed.source = f"scraperapi+{parsed.source}"
                            parsed.title = parsed.title or best_title
                            return parsed
                    else:
                        await resp.aclose()
                except Exception as exc:
                    logger.debug("Erro no fallback ScraperAPI (%s): %s", url, exc)

            # Camada 5: Fallback gratuito Jina Reader (r.jina.ai) com renderização JS + multi-moeda
            jina_result = await _try_jina_reader(client, url, store)
            if jina_result and jina_result.price is not None:
                jina_result.title = jina_result.title or best_title
                return jina_result

            return ScrapeResult(
                title=best_title,
                price=None,
                currency="BRL",
                source="falha",
                store=store,
                error=last_error or "Não foi possível localizar o preço na página (site com proteção ou JS restrito).",
            )
    finally:
        recycle_memory()
