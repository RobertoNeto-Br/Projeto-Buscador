import asyncio
import csv
import io
import logging
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from aiohttp import web

from config import ALERT_CHANNEL_ID, API_HOST, API_KEY, API_PORT, MAX_LINKS, WEB_DIR
from database import build_schedule, parse_user_date
from scraper import extract_store_name, fetch_product_info

if TYPE_CHECKING:
    from main import BuscadorBot

logger = logging.getLogger("buscador.api")


def _is_valid_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _cors_headers() -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, PATCH, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-API-Key, Authorization",
    }


@web.middleware
async def cors_middleware(request: web.Request, handler):
    if request.method == "OPTIONS":
        return web.Response(status=204, headers=_cors_headers())

    try:
        response = await handler(request)
    except web.HTTPException as exc:
        exc.headers.update(_cors_headers())
        raise
    except Exception as exc:
        logger.exception("Erro interno na API web: %s", exc)
        response = web.json_response({"error": "Erro interno no servidor."}, status=500)

    response.headers.update(_cors_headers())
    return response


def _check_api_key(request: web.Request) -> bool:
    if not API_KEY:
        return True
    provided = request.headers.get("X-API-Key", "").strip()
    if not provided:
        auth = request.headers.get("Authorization", "").strip()
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
    return provided == API_KEY


def create_web_app(bot: "BuscadorBot") -> web.Application:
    app = web.Application(middlewares=[cors_middleware])

    async def handle_health(_request: web.Request) -> web.Response:
        global_min, global_label = bot.db.get_global_schedule()
        return web.json_response(
            {
                "status": "online",
                "bot_online": bot.is_ready(),
                "bot_user": str(bot.user) if bot.user else None,
                "total_produtos": bot.db.count_products(),
                "max_links": MAX_LINKS,
                "agendamento_global": {"minutos": global_min, "label": global_label},
                "requer_api_key": bool(API_KEY),
            }
        )

    async def handle_list_products(request: web.Request) -> web.Response:
        raw_date = request.query.get("data_inicio", "").strip()
        start_date = None
        if raw_date:
            try:
                start_date = parse_user_date(raw_date)
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)

        payload = bot.db.get_dashboard_payload(start_date=start_date)
        payload["bot_online"] = bot.is_ready()
        payload["bot_user"] = str(bot.user) if bot.user else "Buscador"
        payload["requer_api_key"] = bool(API_KEY)
        return web.json_response(payload)

    async def handle_sad_report(request: web.Request) -> web.Response:
        raw_date = request.query.get("data_inicio", "").strip()
        start_date = None
        if raw_date:
            try:
                start_date = parse_user_date(raw_date)
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)

        payload = bot.db.get_dashboard_payload(start_date=start_date)
        return web.json_response(
            {
                "filtro_data_inicio": start_date,
                "dias_distintos_totais": payload["dias_distintos_totais"],
                "ranking_sad": payload["ranking_sad"],
                "analises": [
                    {
                        "id": p["id"],
                        "nome": p["nome"],
                        "loja": p["loja"],
                        "moeda": p["moeda"],
                        "sad": p["sad"],
                    }
                    for p in payload["produtos"]
                ],
            }
        )

    async def handle_add_product(request: web.Request) -> web.Response:
        if not _check_api_key(request):
            return web.json_response({"error": "Chave de API inválida ou ausente."}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "JSON inválido."}, status=400)

        url = str(body.get("url") or body.get("link") or "").strip()
        apelido = str(body.get("apelido") or "").strip()

        if not _is_valid_url(url):
            return web.json_response(
                {"error": "Envie uma URL válida começando com http:// ou https://."}, status=400
            )

        existing = bot.db.get_product_by_url(url)
        if existing:
            return web.json_response(
                {"error": f"Esse link já está monitorado com o ID #{existing['id']}."}, status=409
            )

        if bot.db.count_products() >= MAX_LINKS:
            return web.json_response(
                {"error": f"Limite máximo de {MAX_LINKS} links atingido. Remova um produto antes."},
                status=400,
            )

        result = await fetch_product_info(url)
        store = extract_store_name(url)
        nome = (apelido[:140] if apelido else None) or result.title or f"Produto ({store})"

        # Usa o canal padrão se configurado, ou o último canal conhecido de outro produto
        canal_id = ALERT_CHANNEL_ID or 0
        if not canal_id:
            existing_products = bot.db.list_products()
            for ep in existing_products:
                if ep.get("canal_id"):
                    canal_id = ep["canal_id"]
                    break

        product = bot.db.add_product(
            url=url,
            nome=nome,
            loja=store,
            canal_id=canal_id,
            usuario_id=0,
            preco=result.price,
            moeda=result.currency,
            fonte=result.source,
        )

        dias_importados = 0
        if result.historical_points:
            dias_importados = bot.db.import_historical_points(
                product_id=product["id"],
                points=result.historical_points,
                moeda=result.currency,
                fonte=result.source,
            )

        return web.json_response(
            {
                "ok": True,
                "produto": product,
                "dias_historicos_importados": dias_importados,
                "scrape_error": result.error if result.price is None else None,
                "dashboard": bot.db.get_dashboard_payload(),
            },
            status=201,
        )

    async def handle_delete_product(request: web.Request) -> web.Response:
        if not _check_api_key(request):
            return web.json_response({"error": "Chave de API inválida ou ausente."}, status=401)

        try:
            product_id = int(request.match_info["id"])
        except ValueError:
            return web.json_response({"error": "ID inválido."}, status=400)

        removed = bot.db.remove_product(product_id)
        if not removed:
            return web.json_response({"error": f"Produto #{product_id} não encontrado."}, status=404)

        return web.json_response(
            {
                "ok": True,
                "removido": removed,
                "dashboard": bot.db.get_dashboard_payload(),
            }
        )

    async def handle_verify_all(request: web.Request) -> web.Response:
        if not _check_api_key(request):
            return web.json_response({"error": "Chave de API inválida ou ausente."}, status=401)

        products = bot.db.list_products()
        resultados = []
        for p in products:
            res = await bot.check_single_product(p, notify_on_change=True)
            sr = res["result"]
            resultados.append(
                {
                    "id": p["id"],
                    "nome": p["nome"],
                    "ok": sr.price is not None,
                    "preco": sr.price,
                    "moeda": sr.currency,
                    "fonte": sr.source,
                    "erro": sr.error,
                }
            )
            if len(products) > 1:
                await asyncio.sleep(1.2)

        return web.json_response(
            {
                "ok": True,
                "resultados": resultados,
                "dashboard": bot.db.get_dashboard_payload(),
            }
        )

    async def handle_verify_one(request: web.Request) -> web.Response:
        if not _check_api_key(request):
            return web.json_response({"error": "Chave de API inválida ou ausente."}, status=401)

        try:
            product_id = int(request.match_info["id"])
        except ValueError:
            return web.json_response({"error": "ID inválido."}, status=400)

        product = bot.db.get_product(product_id)
        if not product:
            return web.json_response({"error": f"Produto #{product_id} não encontrado."}, status=404)

        res = await bot.check_single_product(product, notify_on_change=True)
        sr = res["result"]
        return web.json_response(
            {
                "ok": sr.price is not None,
                "preco": sr.price,
                "moeda": sr.currency,
                "fonte": sr.source,
                "erro": sr.error,
                "dashboard": bot.db.get_dashboard_payload(),
            }
        )

    async def handle_schedule(request: web.Request) -> web.Response:
        if not _check_api_key(request):
            return web.json_response({"error": "Chave de API inválida ou ausente."}, status=401)

        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "JSON inválido."}, status=400)

        escopo = str(body.get("escopo", "global")).strip().lower()
        unidade = str(body.get("unidade", "horas")).strip().lower()
        try:
            valor = int(body.get("valor", 0))
        except (ValueError, TypeError):
            return web.json_response({"error": "Valor numérico inválido."}, status=400)

        if escopo == "individual":
            try:
                product_id = int(body.get("produto_id"))
            except (ValueError, TypeError):
                return web.json_response({"error": "Informe produto_id para agendamento individual."}, status=400)

            if valor == 0:
                updated = bot.db.set_product_schedule(product_id, None, None)
                if not updated:
                    return web.json_response({"error": f"Produto #{product_id} não encontrado."}, status=404)
                return web.json_response({"ok": True, "dashboard": bot.db.get_dashboard_payload()})

            try:
                minutes, label = build_schedule(valor, unidade)
            except ValueError as exc:
                return web.json_response({"error": str(exc)}, status=400)

            if minutes < 5:
                return web.json_response({"error": "O intervalo mínimo permitido é de 5 minutos."}, status=400)

            updated = bot.db.set_product_schedule(product_id, minutes, label)
            if not updated:
                return web.json_response({"error": f"Produto #{product_id} não encontrado."}, status=404)
            return web.json_response({"ok": True, "dashboard": bot.db.get_dashboard_payload()})

        # Global
        try:
            minutes, label = build_schedule(valor, unidade)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)

        if minutes < 5:
            return web.json_response({"error": "O intervalo mínimo permitido é de 5 minutos."}, status=400)

        bot.db.set_global_schedule(minutes, label)
        return web.json_response({"ok": True, "dashboard": bot.db.get_dashboard_payload()})

    async def handle_export_csv(request: web.Request) -> web.Response:
        raw_date = request.query.get("data_inicio", "").strip()
        start_date = None
        if raw_date:
            try:
                start_date = parse_user_date(raw_date)
            except ValueError:
                start_date = None

        rows = bot.db.get_all_history_for_export(start_date=start_date)
        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";")
        writer.writerow(["produto_id", "produto_nome", "loja", "preco", "moeda", "fonte", "coletado_em", "url"])
        for r in rows:
            writer.writerow(
                [
                    r["produto_id"],
                    r["produto_nome"],
                    r["loja"],
                    f"{r['preco']:.2f}",
                    r["moeda"],
                    r["fonte"],
                    r["coletado_em"],
                    r["url"],
                ]
            )
        return web.Response(
            body=buffer.getvalue().encode("utf-8-sig"),
            content_type="text/csv",
            charset="utf-8",
            headers={"Content-Disposition": 'attachment; filename="historico_precos.csv"'},
        )

    async def handle_index(_request: web.Request) -> web.StreamResponse:
        index_file = WEB_DIR / "index.html"
        if not index_file.exists():
            return web.json_response({"status": "Buscador API online"})
        return web.FileResponse(index_file)

    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/produtos", handle_list_products)
    app.router.add_get("/api/sad", handle_sad_report)
    app.router.add_post("/api/produtos", handle_add_product)
    app.router.add_delete("/api/produtos/{id}", handle_delete_product)
    app.router.add_post("/api/verificar", handle_verify_all)
    app.router.add_post("/api/verificar/{id}", handle_verify_one)
    app.router.add_patch("/api/agendar", handle_schedule)
    app.router.add_get("/api/exportar.csv", handle_export_csv)

    app.router.add_get("/", handle_index)
    if WEB_DIR.exists():
        app.router.add_static("/", WEB_DIR, show_index=False)

    return app


async def start_api_server(bot: "BuscadorBot") -> web.AppRunner:
    app = create_web_app(bot)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=API_HOST, port=API_PORT)
    await site.start()
    logger.info("API Web ativa em http://%s:%d (painel local: http://localhost:%d)", API_HOST, API_PORT, API_PORT)
    return runner
