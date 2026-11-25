import asyncio
import csv
from datetime import datetime, timedelta
import io
import logging
import os
import sys
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import tasks

from api_server import start_api_server
from cloud_sync import CloudSync
from config import (
    ALERT_CHANNEL_ID,
    DISCORD_TOKEN,
    ENABLE_WEB_SERVER,
    MAX_LINKS,
    MAX_RAM_MB,
    RECYCLE_INTERVAL_MINUTES,
    WEB_URL,
)
from database import (
    Database,
    build_schedule,
    format_date_br,
    format_timestamp,
    now_dt,
    parse_iso,
    parse_user_date,
)
from memory_manager import get_ram_mb, recycle_memory
from sad import analyze_product_history
from scraper import extract_store_name, fetch_product_info, format_brl

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("buscador.bot")


def is_valid_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def format_uptime(started_at: datetime) -> str:
    delta = now_dt() - started_at
    total_sec = max(0, int(delta.total_seconds()))
    days, rem = divmod(total_sec, 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {mins}m"
    if hours > 0:
        return f"{hours}h {mins}m"
    return f"{mins}m"


class BuscadorBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        # max_messages=None evita acumular cache de 1.000 mensagens na RAM
        super().__init__(intents=intents, max_messages=None)
        self.tree = app_commands.CommandTree(self)
        self.db = Database()
        self.cloud = CloudSync()
        self._synced = False
        self._api_runner = None
        self._last_attempt: dict[int, datetime] = {}
        self._started_at = now_dt()
        self._recycle_count = 0
        self._is_scraping = False
        self._restarting = False

    async def setup_hook(self) -> None:
        if ENABLE_WEB_SERVER:
            try:
                self._api_runner = await start_api_server(self)
            except Exception as exc:
                logger.error("Não foi possível iniciar a API Web embutida: %s", exc)
        else:
            logger.info("API Web local desativada (ENABLE_WEB_SERVER=false) para economizar RAM.")

        self.monitor_job.start()
        self.memory_recycle_job.change_interval(minutes=RECYCLE_INTERVAL_MINUTES)
        self.memory_recycle_job.start()

        if self.cloud.enabled:
            self.cloud_commands_job.start()
            logger.info("Sincronização em nuvem (Supabase) ATIVA para integração com a Vercel.")

    async def close(self) -> None:
        try:
            self.db.checkpoint_and_optimize()
        except Exception:
            pass
        if self._api_runner is not None:
            await self._api_runner.cleanup()
        await super().close()

    async def sync_cloud_state(self) -> None:
        """Envia o estado atual completo (produtos, histórico, Mini-SAD) para o Supabase."""
        if not self.cloud.enabled:
            return
        payload = self.db.get_dashboard_payload()
        payload["bot_online"] = self.is_ready()
        payload["bot_user"] = str(self.user) if self.user else "Buscador"
        payload["ram_mb"] = get_ram_mb()
        payload["max_ram_mb"] = MAX_RAM_MB
        payload["uptime"] = format_uptime(self._started_at)
        await self.cloud.push_dashboard(payload)

    async def on_ready(self) -> None:
        if not self._synced:
            await self.tree.sync()
            self._synced = True
            logger.info("Comandos slash sincronizados com o Discord.")
        global_min, global_label = self.db.get_global_schedule()
        logger.info(
            "Bot conectado como %s (ID: %s) | Global: a cada %s | RAM: %.1f MB (reciclo em %.0f MB) | Nuvem: %s",
            self.user,
            self.user.id if self.user else "?",
            global_label,
            get_ram_mb(),
            MAX_RAM_MB,
            "Supabase ON" if self.cloud.enabled else "Local",
        )
        await self.sync_cloud_state()

    def _default_channel_id(self) -> int:
        if ALERT_CHANNEL_ID:
            return ALERT_CHANNEL_ID
        for ep in self.db.list_products():
            if ep.get("canal_id"):
                return int(ep["canal_id"])
        return 0

    async def check_single_product(self, product: dict, notify_on_change: bool = True) -> dict:
        self._last_attempt[product["id"]] = now_dt()
        self._is_scraping = True
        try:
            result = await fetch_product_info(product["url"])
        finally:
            self._is_scraping = False

        status = {
            "product": product,
            "result": result,
            "old_price": product["ultimo_preco"],
            "changed": False,
        }

        if result.price is None:
            logger.warning(
                "Não foi possível coletar preço do produto #%d (%s): %s",
                product["id"],
                product["loja"],
                result.error,
            )
            return status

        old_price = self.db.record_price(
            product_id=product["id"],
            preco=result.price,
            moeda=result.currency,
            fonte=result.source,
            novo_nome=result.title,
        )
        if result.historical_points:
            self.db.import_historical_points(
                product_id=product["id"],
                points=result.historical_points,
                moeda=result.currency,
                fonte=result.source,
            )
        status["old_price"] = old_price

        if old_price is not None and abs(result.price - old_price) >= 0.01:
            status["changed"] = True
            if notify_on_change:
                await self._send_price_alert(product, old_price, result.price, result.currency)

        return status

    async def _send_price_alert(
        self, product: dict, old_price: float, new_price: float, currency: str
    ) -> None:
        channel_id = ALERT_CHANNEL_ID or product["canal_id"]
        if not channel_id:
            return
        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception as exc:
                logger.error("Não foi possível acessar canal %s para alerta: %s", channel_id, exc)
                return

        diff = new_price - old_price
        pct = (diff / old_price) * 100 if old_price > 0 else 0.0

        if diff < 0:
            titulo = "📉 Preço baixou!"
            cor = discord.Color.green()
            variacao = f"{pct:.1f}% ({format_brl(diff, currency)})"
        else:
            titulo = "📈 Preço subiu"
            cor = discord.Color.orange()
            variacao = f"+{pct:.1f}% (+{format_brl(diff, currency)})"

        embed = discord.Embed(
            title=titulo,
            description=f"**[{product['nome']}]({product['url']})**",
            color=cor,
        )
        embed.add_field(name="Loja", value=product["loja"], inline=True)
        embed.add_field(name="Preço anterior", value=format_brl(old_price, currency), inline=True)
        embed.add_field(name="Preço atual", value=format_brl(new_price, currency), inline=True)
        embed.add_field(name="Variação", value=variacao, inline=False)
        embed.set_footer(text=f"ID #{product['id']} • Monitoramento automático")

        try:
            await channel.send(embed=embed)
        except Exception as exc:
            logger.error("Erro ao enviar alerta no canal %s: %s", channel_id, exc)

    def _is_product_due(self, product: dict, global_minutes: int, agora: datetime) -> bool:
        interval_min = product.get("intervalo_minutos") or global_minutes
        delta = timedelta(minutes=interval_min)

        last_attempt = self._last_attempt.get(product["id"])
        last_saved = parse_iso(product.get("atualizado_em"))

        reference = last_saved
        if last_attempt and (reference is None or last_attempt > reference):
            reference = last_attempt

        if reference is None:
            return True
        return (agora - reference) >= delta

    async def _execute_cloud_command(self, cmd: dict) -> None:
        cmd_id = int(cmd["id"])
        acao = str(cmd.get("acao") or "").strip().lower()
        body = cmd.get("payload") if isinstance(cmd.get("payload"), dict) else {}

        await self.cloud.update_command(cmd_id, "processando")
        try:
            if acao == "adicionar":
                url = str(body.get("url") or "").strip()
                apelido = str(body.get("apelido") or "").strip()
                if not is_valid_url(url):
                    raise ValueError("Envie uma URL válida começando com http:// ou https://.")
                existing = self.db.get_product_by_url(url)
                if existing:
                    raise ValueError(f"Esse link já está monitorado com o ID #{existing['id']}.")
                if self.db.count_products() >= MAX_LINKS:
                    raise ValueError(f"Limite máximo de {MAX_LINKS} links atingido.")

                self._is_scraping = True
                try:
                    result = await fetch_product_info(url)
                finally:
                    self._is_scraping = False

                store = extract_store_name(url)
                nome = (apelido[:140] if apelido else None) or result.title or f"Produto ({store})"
                product = self.db.add_product(
                    url=url,
                    nome=nome,
                    loja=store,
                    canal_id=self._default_channel_id(),
                    usuario_id=0,
                    preco=result.price,
                    moeda=result.currency,
                    fonte=result.source,
                )
                dias_importados = 0
                if result.historical_points:
                    dias_importados = self.db.import_historical_points(
                        product_id=product["id"],
                        points=result.historical_points,
                        moeda=result.currency,
                        fonte=result.source,
                    )
                await self.sync_cloud_state()
                await self.cloud.update_command(
                    cmd_id,
                    "concluido",
                    {
                        "ok": True,
                        "produto_id": product["id"],
                        "dias_historicos_importados": dias_importados,
                        "scrape_error": result.error if result.price is None else None,
                    },
                )

            elif acao == "remover":
                pid = int(body.get("produto_id"))
                removed = self.db.remove_product(pid)
                if not removed:
                    raise ValueError(f"Produto #{pid} não encontrado.")
                self._last_attempt.pop(pid, None)
                await self.sync_cloud_state()
                await self.cloud.update_command(cmd_id, "concluido", {"ok": True, "removido_id": pid})

            elif acao == "verificar":
                products = self.db.list_products()
                resultados = []
                for p in products:
                    res = await self.check_single_product(p, notify_on_change=True)
                    sr = res["result"]
                    resultados.append(
                        {"id": p["id"], "ok": sr.price is not None, "preco": sr.price, "erro": sr.error}
                    )
                    if len(products) > 1:
                        await asyncio.sleep(1.2)
                await self.sync_cloud_state()
                await self.cloud.update_command(cmd_id, "concluido", {"ok": True, "resultados": resultados})

            elif acao == "verificar_um":
                pid = int(body.get("produto_id"))
                product = self.db.get_product(pid)
                if not product:
                    raise ValueError(f"Produto #{pid} não encontrado.")
                res = await self.check_single_product(product, notify_on_change=True)
                sr = res["result"]
                await self.sync_cloud_state()
                await self.cloud.update_command(
                    cmd_id,
                    "concluido",
                    {"ok": sr.price is not None, "preco": sr.price, "moeda": sr.currency, "erro": sr.error},
                )

            elif acao == "agendar":
                escopo = str(body.get("escopo", "global")).strip().lower()
                unidade = str(body.get("unidade", "horas")).strip().lower()
                valor = int(body.get("valor", 0))
                if escopo == "individual":
                    pid = int(body.get("produto_id"))
                    if valor == 0:
                        updated = self.db.set_product_schedule(pid, None, None)
                        if not updated:
                            raise ValueError(f"Produto #{pid} não encontrado.")
                    else:
                        minutes, label = build_schedule(valor, unidade)
                        if minutes < 5:
                            raise ValueError("O intervalo mínimo permitido é de 5 minutos.")
                        updated = self.db.set_product_schedule(pid, minutes, label)
                        if not updated:
                            raise ValueError(f"Produto #{pid} não encontrado.")
                else:
                    minutes, label = build_schedule(valor, unidade)
                    if minutes < 5:
                        raise ValueError("O intervalo mínimo permitido é de 5 minutos.")
                    self.db.set_global_schedule(minutes, label)

                await self.sync_cloud_state()
                await self.cloud.update_command(cmd_id, "concluido", {"ok": True})

            elif acao == "atualizar":
                await self.sync_cloud_state()
                await self.cloud.update_command(cmd_id, "concluido", {"ok": True})

            else:
                raise ValueError(f"Ação desconhecida: {acao}")

        except Exception as exc:
            await self.cloud.update_command(cmd_id, "erro", {"ok": False, "error": str(exc)})
        finally:
            recycle_memory()

    @tasks.loop(seconds=5)
    async def cloud_commands_job(self) -> None:
        if not self.cloud.enabled or self._restarting:
            return
        pending = await self.cloud.fetch_pending_commands(limit=3)
        for cmd in pending:
            await self._execute_cloud_command(cmd)

    @cloud_commands_job.before_loop
    async def before_cloud_commands_job(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=1)
    async def monitor_job(self) -> None:
        if self._restarting:
            return
        products = self.db.list_products()
        if not products:
            return

        global_min, _ = self.db.get_global_schedule()
        agora = now_dt()
        due_products = [p for p in products if self._is_product_due(p, global_min, agora)]
        if not due_products:
            return

        logger.info("Job disparado: verificando %d/%d produto(s) no prazo...", len(due_products), len(products))
        ok = 0
        for product in due_products:
            res = await self.check_single_product(product, notify_on_change=True)
            if res["result"].price is not None:
                ok += 1
            await asyncio.sleep(2)

        await self.sync_cloud_state()
        rec = recycle_memory()
        logger.info(
            "Ciclo concluído: %d/%d preços coletados | RAM após reciclo: %.1f MB",
            ok,
            len(due_products),
            rec["after_mb"],
        )

    @monitor_job.before_loop
    async def before_monitor_job(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(minutes=5)
    async def memory_recycle_job(self) -> None:
        if self._restarting or self._is_scraping:
            return

        # Limpa IDs antigos do cache de tentativas
        valid_ids = {p["id"] for p in self.db.list_products()}
        for stale_id in list(self._last_attempt.keys()):
            if stale_id not in valid_ids:
                self._last_attempt.pop(stale_id, None)

        self.db.checkpoint_and_optimize()
        if self.cloud.enabled:
            await self.cloud.cleanup_old_commands()

        rec = recycle_memory()
        self._recycle_count += 1
        ram_after = rec["after_mb"]

        if rec["freed_mb"] >= 1.0:
            logger.info(
                "Reciclo #%d: %.1f MB -> %.1f MB (liberou %.1f MB)",
                self._recycle_count,
                rec["before_mb"],
                ram_after,
                rec["freed_mb"],
            )

        # Reinício preventivo seguro (sem perda de dados) caso a RAM continue perto do teto da Discloud
        if MAX_RAM_MB > 0 and ram_after >= MAX_RAM_MB:
            self._restarting = True
            logger.warning(
                "RAM em %.1f MB (>= limite de %.0f MB). Salvando banco e realizando reinício limpo...",
                ram_after,
                MAX_RAM_MB,
            )
            try:
                await self.sync_cloud_state()
                self.db.checkpoint_and_optimize()
            finally:
                await self.close()
                os._exit(0)

    @memory_recycle_job.before_loop
    async def before_memory_recycle_job(self) -> None:
        await self.wait_until_ready()


bot = BuscadorBot()


@bot.tree.command(name="tutorial", description="Mostra o guia completo de como usar todos os comandos do bot.")
async def cmd_tutorial(interaction: discord.Interaction) -> None:
    _, global_label = bot.db.get_global_schedule()
    embed = discord.Embed(
        title="📘 Tutorial — Bot Buscador de Preços",
        description=(
            f"Eu monitoro até **{MAX_LINKS} links de produtos** de qualquer loja, salvo o histórico "
            "de preços automaticamente e aviso aqui no Discord quando o preço muda!"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="🛒 Gerenciar produtos",
        value=(
            "• `/adicionar <link> [apelido]` — Salva um produto e já lê o preço atual.\n"
            "• `/listar` — Mostra os produtos monitorados, preços e o intervalo de cada um.\n"
            "• `/remover <id>` — Remove um produto pelo ID e libera uma vaga."
        ),
        inline=False,
    )
    embed.add_field(
        name="📊 Consultas, Mini-SAD e Painel Web",
        value=(
            "• `/verificar [id]` — Força a coleta agora (de 1 produto ou de todos).\n"
            "• `/historico <id> [data_inicio] [todo_historico]` — Mostra o histórico **a partir da data escolhida até hoje** (ou desde a data de adição se não informar).\n"
            "• `/sad [id] [data_inicio] [preco_alvo]` — Executa o **Mini-SAD** (decisão de compra, score, tendência, simulação What-If e ranking).\n"
            "• `/exportar [baixar_csv]` — Envia o **link do Painel Web** (com gráficos, Mini-SAD, botão de atualizar e opção de adicionar links)."
        ),
        inline=False,
    )
    embed.add_field(
        name="⏱️ Agendamento & Saúde do Bot",
        value=(
            "• `/agendar` — Define varredura **Global** ou **Individual** em **minutos, horas, dias ou semanas** (mín. 5 min).\n"
            "• `/status [reciclar]` — Mostra uso de RAM, uptime, nuvem e permite reciclar memória sem perder dados.\n"
            f"*(Varredura global atual: **a cada {global_label}**)*"
        ),
        inline=False,
    )
    embed.set_footer(text="Dica: links do Buscapé ou Zoom importam até 40 dias de histórico automaticamente!")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="status", description="Mostra consumo de RAM, uptime, sincronização e permite reciclar memória.")
@app_commands.describe(reciclar="Marque True para forçar uma reciclagem imediata de memória (sem perda de dados)")
async def cmd_status(interaction: discord.Interaction, reciclar: bool = False) -> None:
    rec_info = None
    if reciclar:
        bot.db.checkpoint_and_optimize()
        rec_info = recycle_memory()
        bot._recycle_count += 1
        await bot.sync_cloud_state()

    ram_now = get_ram_mb()
    payload = bot.db.get_dashboard_payload()

    embed = discord.Embed(
        title="🩺 Status do Bot & Reciclagem de Memória",
        color=discord.Color.green() if ram_now < MAX_RAM_MB * 0.8 else discord.Color.orange(),
    )
    embed.add_field(name="RAM em uso", value=f"**{ram_now:.1f} MB** / {MAX_RAM_MB:.0f} MB (teto seguro)", inline=True)
    embed.add_field(name="Uptime", value=format_uptime(bot._started_at), inline=True)
    embed.add_field(name="Reciclagens", value=f"{bot._recycle_count}x (a cada {RECYCLE_INTERVAL_MINUTES} min)", inline=True)
    embed.add_field(name="Produtos / Coletas", value=f"{payload['total_produtos']}/{payload['max_links']} • {payload['total_coletas']} coletas", inline=True)
    embed.add_field(name="Ponte Vercel (Supabase)", value="🟢 Conectada" if bot.cloud.enabled else "⚪ Apenas local", inline=True)
    embed.add_field(name="Painel Web", value=WEB_URL, inline=True)

    if rec_info is not None:
        embed.add_field(
            name="♻️ Reciclagem executada agora",
            value=f"{rec_info['before_mb']:.1f} MB → **{rec_info['after_mb']:.1f} MB** (liberou {rec_info['freed_mb']:.1f} MB | banco intacto!)",
            inline=False,
        )

    embed.set_footer(text="Todos os dados ficam salvos em disco (SQLite) e sincronizados sem perda.")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="adicionar", description=f"Adiciona um link de produto para acompanhar (máx. {MAX_LINKS}).")
@app_commands.describe(
    link="URL completa do produto (qualquer loja)",
    apelido="Nome opcional para identificar o produto facilmente",
)
async def cmd_adicionar(interaction: discord.Interaction, link: str, apelido: str | None = None) -> None:
    await interaction.response.defer(thinking=True)

    url = link.strip()
    if not is_valid_url(url):
        await interaction.followup.send("Envie uma URL válida começando com `http://` ou `https://`.")
        return

    existing = bot.db.get_product_by_url(url)
    if existing:
        await interaction.followup.send(
            f"Esse link já está sendo acompanhado com o **ID #{existing['id']}** (`{existing['nome']}`)."
        )
        return

    total = bot.db.count_products()
    if total >= MAX_LINKS:
        await interaction.followup.send(
            f"Limite de **{MAX_LINKS} links** atingido! Use `/listar` e `/remover <id>` para liberar espaço."
        )
        return

    bot._is_scraping = True
    try:
        result = await fetch_product_info(url)
    finally:
        bot._is_scraping = False

    store = extract_store_name(url)
    nome = (apelido.strip()[:140] if apelido else None) or result.title or f"Produto ({store})"

    product = bot.db.add_product(
        url=url,
        nome=nome,
        loja=store,
        canal_id=interaction.channel_id or 0,
        usuario_id=interaction.user.id,
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

    await bot.sync_cloud_state()

    novo_total = bot.db.count_products()
    _, global_label = bot.db.get_global_schedule()

    if result.price is not None:
        embed = discord.Embed(
            title="✅ Produto adicionado ao monitoramento",
            description=f"**[{product['nome']}]({product['url']})**",
            color=discord.Color.blue(),
        )
        embed.add_field(name="ID", value=f"#{product['id']}", inline=True)
        embed.add_field(name="Loja", value=product["loja"], inline=True)
        embed.add_field(name="Preço inicial", value=format_brl(result.price, result.currency), inline=True)
        embed.add_field(name="Método", value=f"`{result.source}`", inline=True)
        embed.add_field(name="Vagas usadas", value=f"{novo_total}/{MAX_LINKS}", inline=True)
        if dias_importados > 0:
            embed.add_field(
                name="📅 Histórico real importado",
                value=f"+{dias_importados} dias anteriores carregados automaticamente!",
                inline=False,
            )
        embed.set_footer(text=f"Varredura global: a cada {global_label} • Use /sad ou /historico para analisar")
    else:
        embed = discord.Embed(
            title="⚠️ Produto salvo, mas o preço não foi lido agora",
            description=f"**[{product['nome']}]({product['url']})**",
            color=discord.Color.gold(),
        )
        embed.add_field(name="ID", value=f"#{product['id']}", inline=True)
        embed.add_field(name="Loja", value=product["loja"], inline=True)
        embed.add_field(name="Vagas usadas", value=f"{novo_total}/{MAX_LINKS}", inline=True)
        embed.add_field(
            name="Motivo",
            value=result.error or "O site pode estar bloqueando requisições automáticas.",
            inline=False,
        )
        embed.set_footer(text="O job vai tentar coletar novamente no próximo ciclo ou via /verificar.")

    await interaction.followup.send(embed=embed)


@bot.tree.command(name="listar", description="Mostra todos os produtos que estão sendo acompanhados.")
async def cmd_listar(interaction: discord.Interaction) -> None:
    products = bot.db.list_products()
    if not products:
        await interaction.response.send_message(
            "Nenhum produto cadastrado ainda. Use `/adicionar <link>` ou `/tutorial` para começar!"
        )
        return

    _, global_label = bot.db.get_global_schedule()
    embed = discord.Embed(
        title=f"🛒 Produtos acompanhados ({len(products)}/{MAX_LINKS})",
        color=discord.Color.blurple(),
    )
    for p in products:
        preco_txt = format_brl(p["ultimo_preco"], p["moeda"])
        atualizado = format_timestamp(p["atualizado_em"])
        regra = (
            f"A cada {p['intervalo_label']} *(individual)*"
            if p.get("intervalo_minutos")
            else f"Global ({global_label})"
        )
        embed.add_field(
            name=f"#{p['id']} — {p['nome']}",
            value=(
                f"💰 **{preco_txt}** | 🏪 `{p['loja']}`\n"
                f"⏱️ {regra} | 🕒 Última coleta: {atualizado} | [Abrir link]({p['url']})"
            ),
            inline=False,
        )
    embed.set_footer(text=f"Varredura global atual: a cada {global_label} • Use /agendar para alterar")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="agendar",
    description="Define o intervalo de verificação (global ou individual) em minutos, horas, dias ou semanas.",
)
@app_commands.describe(
    escopo="Aplicar a todos os produtos (Global) ou a apenas 1 produto (Individual)",
    valor="Quantidade (mínimo 5 minutos; use 0 no modo Individual para voltar ao padrão Global)",
    unidade="Unidade de tempo da varredura",
    produto_id="ID do produto (obrigatório apenas quando escolher Individual)",
)
@app_commands.choices(
    escopo=[
        app_commands.Choice(name="Global (todos os produtos sem regra própria)", value="global"),
        app_commands.Choice(name="Individual (apenas 1 produto)", value="individual"),
    ],
    unidade=[
        app_commands.Choice(name="Minutos", value="minutos"),
        app_commands.Choice(name="Horas", value="horas"),
        app_commands.Choice(name="Dias", value="dias"),
        app_commands.Choice(name="Semanas", value="semanas"),
    ],
)
async def cmd_agendar(
    interaction: discord.Interaction,
    escopo: app_commands.Choice[str],
    valor: int,
    unidade: app_commands.Choice[str],
    produto_id: int | None = None,
) -> None:
    if escopo.value == "individual":
        if produto_id is None:
            await interaction.response.send_message(
                "Para agendamento **Individual**, informe também o `produto_id` (veja com `/listar`).",
                ephemeral=True,
            )
            return

        product = bot.db.get_product(produto_id)
        if not product:
            await interaction.response.send_message(
                f"Produto `#{produto_id}` não encontrado. Use `/listar` para ver os IDs.",
                ephemeral=True,
            )
            return

        if valor == 0:
            bot.db.set_product_schedule(produto_id, None, None)
            await bot.sync_cloud_state()
            _, global_label = bot.db.get_global_schedule()
            await interaction.response.send_message(
                f"🔄 Produto **#{product['id']} ({product['nome']})** voltou a usar o agendamento **Global** (a cada **{global_label}**)."
            )
            return

        minutes, label = build_schedule(valor, unidade.value)
        if minutes < 5:
            await interaction.response.send_message(
                "O intervalo mínimo permitido é de **5 minutos** (para evitar bloqueio nas lojas).",
                ephemeral=True,
            )
            return

        bot.db.set_product_schedule(produto_id, minutes, label)
        await bot.sync_cloud_state()
        embed = discord.Embed(
            title="⏱️ Agendamento individual atualizado",
            description=f"**#{product['id']} — {product['nome']}**",
            color=discord.Color.green(),
        )
        embed.add_field(name="Nova frequência", value=f"A cada **{label}**", inline=True)
        embed.add_field(name="Escopo", value="Individual", inline=True)
        await interaction.response.send_message(embed=embed)
        return

    if valor <= 0:
        await interaction.response.send_message(
            "Informe um valor maior que zero (mínimo **5 minutos**).",
            ephemeral=True,
        )
        return

    minutes, label = build_schedule(valor, unidade.value)
    if minutes < 5:
        await interaction.response.send_message(
            "O intervalo mínimo permitido é de **5 minutos** (para evitar bloqueio nas lojas).",
            ephemeral=True,
        )
        return

    bot.db.set_global_schedule(minutes, label)
    await bot.sync_cloud_state()
    embed = discord.Embed(
        title="⏱️ Agendamento global atualizado",
        description="Todos os produtos sem regra individual agora seguem este intervalo.",
        color=discord.Color.green(),
    )
    embed.add_field(name="Nova frequência global", value=f"A cada **{label}**", inline=True)
    embed.add_field(name="Escopo", value="Global", inline=True)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="remover", description="Remove um produto do acompanhamento pelo ID.")
@app_commands.describe(id="ID do produto (veja usando /listar)")
async def cmd_remover(interaction: discord.Interaction, id: int) -> None:
    removed = bot.db.remove_product(id)
    if not removed:
        await interaction.response.send_message(f"Não encontrei nenhum produto com o ID `#{id}`.", ephemeral=True)
        return
    bot._last_attempt.pop(id, None)
    await bot.sync_cloud_state()
    restantes = bot.db.count_products()
    await interaction.response.send_message(
        f"🗑️ Produto **#{removed['id']} ({removed['nome']})** removido. Vagas usadas: **{restantes}/{MAX_LINKS}**."
    )


@bot.tree.command(
    name="historico",
    description="Mostra o histórico de preços a partir de uma data até hoje (ou desde a data de adição).",
)
@app_commands.describe(
    id="ID do produto (veja usando /listar)",
    data_inicio="Data inicial opcional (DD/MM/AAAA ou AAAA-MM-DD). Se vazio, usa a data de adição do produto.",
    todo_historico="Marque True para ver todas as datas disponíveis (incluindo dias importados do Buscapé/Zoom).",
)
async def cmd_historico(
    interaction: discord.Interaction,
    id: int,
    data_inicio: str | None = None,
    todo_historico: bool = False,
) -> None:
    product = bot.db.get_product(id)
    if not product:
        await interaction.response.send_message(f"Produto `#{id}` não encontrado.", ephemeral=True)
        return

    added_date = str(product["criado_em"])[:10]
    if data_inicio:
        try:
            start_date = parse_user_date(data_inicio)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        periodo_label = f"De {format_date_br(start_date)} até hoje"
    elif todo_historico:
        start_date = None
        periodo_label = "Todo o histórico disponível"
    else:
        start_date = added_date
        periodo_label = f"Desde a data de adição ({format_date_br(added_date)}) até hoje"

    stats = bot.db.get_price_stats(id, start_date=start_date)
    all_stats = bot.db.get_price_stats(id, start_date=None)
    history = bot.db.get_history(id, limit=15, start_date=start_date)
    history_full_asc = list(reversed(bot.db.get_history(id, limit=200, start_date=start_date)))
    sad_info = analyze_product_history(history_full_asc, currency=product["moeda"])

    embed = discord.Embed(
        title=f"📊 Histórico — #{product['id']} {product['nome']}",
        description=f"📅 **Período:** {periodo_label}",
        url=product["url"],
        color=discord.Color.teal(),
    )

    if not stats or not history:
        embed.add_field(
            name="Sem registros no período",
            value="Não há coletas nessa janela de datas. Tente outra `data_inicio` ou `todo_historico:True`.",
            inline=False,
        )
        await interaction.response.send_message(embed=embed)
        return

    moeda = product["moeda"]
    embed.add_field(name="Atual", value=format_brl(product["ultimo_preco"], moeda), inline=True)
    embed.add_field(name="Menor", value=format_brl(stats["menor"], moeda), inline=True)
    embed.add_field(name="Maior", value=format_brl(stats["maior"], moeda), inline=True)
    embed.add_field(name="Média", value=format_brl(stats["media"], moeda), inline=True)
    embed.add_field(name="Dias distintos", value=f"{stats['dias_distintos']} dia(s)", inline=True)
    embed.add_field(
        name="Mini-SAD",
        value=f"**{sad_info['decisao']}** ({sad_info['score_oportunidade']}/100)",
        inline=True,
    )

    linhas = [
        f"• `{format_timestamp(h['coletado_em'])}` — **{format_brl(h['preco'], h['moeda'])}** (`{h['fonte']}`)"
        for h in history
    ]
    embed.add_field(
        name=f"Últimas {len(history)} coletas no período (total: {stats['total']})",
        value="\n".join(linhas),
        inline=False,
    )

    if all_stats and all_stats["total"] > stats["total"]:
        embed.set_footer(
            text=(
                f"Há {all_stats['dias_distintos']} dias no banco completo. "
                "Use data_inicio (ex: 15/11/2026) ou todo_historico:True para expandir."
            )
        )
    else:
        embed.set_footer(text="Use /sad para ver a análise completa de apoio à decisão.")

    await interaction.response.send_message(embed=embed)


@bot.tree.command(
    name="sad",
    description="Mini-SAD: análise estatística, recomendação de compra, simulação What-If e ranking.",
)
@app_commands.describe(
    id="ID opcional do produto (deixe vazio para ver o ranking comparativo de todos)",
    data_inicio="Data inicial opcional (DD/MM/AAAA ou AAAA-MM-DD). Se vazio, analisa todo o histórico.",
    preco_alvo="Opcional (Análise What-If): informe o preço que você deseja pagar",
)
async def cmd_sad(
    interaction: discord.Interaction,
    id: int | None = None,
    data_inicio: str | None = None,
    preco_alvo: float | None = None,
) -> None:
    start_date = None
    if data_inicio:
        try:
            start_date = parse_user_date(data_inicio)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

    if id is None:
        payload = bot.db.get_dashboard_payload(start_date=start_date)
        ranking = payload["ranking_sad"]
        if not ranking:
            await interaction.response.send_message("Nenhum produto cadastrado para analisar no Mini-SAD.")
            return

        embed = discord.Embed(
            title="🧠 Mini-SAD — Ranking de Oportunidade de Compra",
            description=(
                f"Critérios: desconto vs média (45%), proximidade da mínima (35%) e tendência (20%).\n"
                f"📅 **Filtro:** {f'A partir de {format_date_br(start_date)}' if start_date else 'Todo o histórico disponível'}"
            ),
            color=discord.Color.blurple(),
        )
        medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
        for idx, item in enumerate(ranking):
            medal = medals[idx] if idx < len(medals) else f"#{idx + 1}"
            icon = {"green": "🟢", "orange": "🟡", "blue": "🔵"}.get(item["nivel"], "⚪")
            embed.add_field(
                name=f"{medal} #{item['id']} — {item['nome']}",
                value=(
                    f"{icon} **{item['decisao']}** • Score: **{item['score_oportunidade']}/100** • Tendência: {item['tendencia_label']}\n"
                    f"💰 Atual: **{format_brl(item['preco_atual'], item['moeda'])}** | "
                    f"Média: {format_brl(item['media'], item['moeda'])} | "
                    f"Mínima: {format_brl(item['menor_preco'], item['moeda'])} | "
                    f"📅 {item['dias_distintos']} dia(s)"
                ),
                inline=False,
            )
        embed.set_footer(text="Use /sad id:<numero> preco_alvo:<valor> para análise detalhada e What-If.")
        await interaction.response.send_message(embed=embed)
        return

    product = bot.db.get_product(id)
    if not product:
        await interaction.response.send_message(f"Produto `#{id}` não encontrado.", ephemeral=True)
        return

    history_asc = list(reversed(bot.db.get_history(id, limit=200, start_date=start_date)))
    sad = analyze_product_history(history_asc, currency=product["moeda"], target_price=preco_alvo)
    moeda = product["moeda"]

    color_map = {
        "green": discord.Color.green(),
        "orange": discord.Color.orange(),
        "blue": discord.Color.blue(),
    }
    icon_map = {"green": "🟢", "orange": "🟡", "blue": "🔵"}

    embed = discord.Embed(
        title=f"🧠 Mini-SAD — #{product['id']} {product['nome']}",
        description=(
            f"### {icon_map.get(sad['nivel'], '⚪')} Recomendação: **{sad['decisao']}** "
            f"(Score: **{sad['score_oportunidade']}/100**)\n"
            f"{sad['justificativa']}"
        ),
        url=product["url"],
        color=color_map.get(sad["nivel"], discord.Color.greyple()),
    )

    if sad["disponivel"]:
        embed.add_field(name="Preço atual", value=format_brl(sad["preco_atual"], moeda), inline=True)
        embed.add_field(name="Média", value=format_brl(sad["media"], moeda), inline=True)
        embed.add_field(name="Desvio padrão", value=format_brl(sad["desvio_padrao"], moeda), inline=True)
        embed.add_field(
            name="Mínima / Máxima",
            value=f"{format_brl(sad['menor_preco'], moeda)} / {format_brl(sad['maior_preco'], moeda)}",
            inline=True,
        )
        embed.add_field(name="Tendência", value=sad["tendencia_label"], inline=True)
        embed.add_field(
            name="Base histórica",
            value=f"{sad['dias_distintos']} dia(s) distintos ({'✅ ≥ 5 dias' if sad['atingiu_minimo_5_dias'] else '⏳ < 5 dias'})",
            inline=True,
        )
        if sad.get("what_if"):
            embed.add_field(
                name="🎯 Simulação What-If (Preço-Alvo)",
                value=sad["what_if"]["resumo"],
                inline=False,
            )

    embed.set_footer(
        text=f"Período: {f'de {format_date_br(start_date)} até hoje' if start_date else 'todo o histórico'}"
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="verificar", description="Força a coleta de preço agora (de 1 produto ou de todos).")
@app_commands.describe(id="ID opcional do produto (deixe vazio para verificar todos)")
async def cmd_verificar(interaction: discord.Interaction, id: int | None = None) -> None:
    await interaction.response.defer(thinking=True)

    if id is not None:
        product = bot.db.get_product(id)
        if not product:
            await interaction.followup.send(f"Produto `#{id}` não encontrado.")
            return
        targets = [product]
    else:
        targets = bot.db.list_products()
        if not targets:
            await interaction.followup.send("Nenhum produto cadastrado para verificar.")
            return

    linhas: list[str] = []
    for p in targets:
        res = await bot.check_single_product(p, notify_on_change=False)
        sr = res["result"]
        if sr.price is not None:
            anterior = format_brl(res["old_price"], sr.currency) if res["old_price"] is not None else "—"
            atual = format_brl(sr.price, sr.currency)
            linhas.append(f"✅ **#{p['id']} {p['nome']}**: {atual} *(antes: {anterior} | via `{sr.source}`)*")
        else:
            linhas.append(f"❌ **#{p['id']} {p['nome']}**: falha ({sr.error})")
        if len(targets) > 1:
            await asyncio.sleep(1.5)

    await bot.sync_cloud_state()
    recycle_memory()

    embed = discord.Embed(
        title="🔄 Verificação concluída",
        description="\n".join(linhas),
        color=discord.Color.green(),
    )
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="exportar", description="Envia o link do Painel Web interativo (ou baixa o CSV).")
@app_commands.describe(baixar_csv="Marque True se quiser baixar também a planilha CSV direto aqui no Discord")
async def cmd_exportar(interaction: discord.Interaction, baixar_csv: bool = False) -> None:
    payload = bot.db.get_dashboard_payload()

    aviso_local = ""
    if "localhost" in WEB_URL and sys.platform.startswith("linux"):
        aviso_local = (
            "\n\n⚠️ *Você ainda está com `WEB_URL=http://localhost:8080`. "
            "Configure `WEB_URL` com o link da sua Vercel nas variáveis da Discloud!*"
        )

    embed = discord.Embed(
        title="🌐 Painel Web — Buscador de Preços",
        description=(
            f"Acesse o painel para ver **gráficos de evolução**, histórico completo, "
            f"clicar em **Atualizar** ou **adicionar novos links**!\n\n"
            f"🔗 **{WEB_URL}**{aviso_local}"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Produtos",
        value=f"{payload['total_produtos']}/{payload['max_links']}",
        inline=True,
    )
    embed.add_field(name="Coletas salvas", value=str(payload["total_coletas"]), inline=True)
    embed.add_field(
        name="Varredura global",
        value=f"A cada {payload['agendamento_global']['label']}",
        inline=True,
    )

    view = discord.ui.View()
    if WEB_URL.startswith(("http://", "https://")):
        view.add_item(
            discord.ui.Button(
                label="Abrir Painel Web",
                url=WEB_URL,
                emoji="🌐",
                style=discord.ButtonStyle.link,
            )
        )

    if baixar_csv:
        rows = bot.db.get_all_history_for_export()
        if not rows:
            await interaction.response.send_message(embed=embed, view=view)
            return

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
        csv_bytes = io.BytesIO(buffer.getvalue().encode("utf-8-sig"))
        file = discord.File(fp=csv_bytes, filename="historico_precos.csv")
        await interaction.response.send_message(embed=embed, view=view, file=file)
        return

    await interaction.response.send_message(embed=embed, view=view)


if __name__ == "__main__":
    if not DISCORD_TOKEN or DISCORD_TOKEN == "SEU_TOKEN_AQUI":
        raise SystemExit(
            "ERRO: Configure DISCORD_TOKEN no arquivo .env antes de iniciar o bot!"
        )
    bot.run(DISCORD_TOKEN)
