"""
Ponte em nuvem gratuita via Supabase REST API (usando apenas httpx).

Permite que o bot rodando na Discloud (plano Gold / TYPE=bot, sem porta HTTP aberta)
sincronize dados em tempo real com o painel hospedado na Vercel e execute comandos
enviados pelo site (adicionar link, varrer agora, agendar e remover).
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from config import SUPABASE_KEY, SUPABASE_URL
from database import now_iso

logger = logging.getLogger("buscador.cloud")


class CloudSync:
    def __init__(self, url: str = SUPABASE_URL, key: str = SUPABASE_KEY) -> None:
        self.url = url.rstrip("/")
        self.key = key.strip()
        self.enabled = bool(self.url and self.key)

    def _headers(self, prefer: Optional[str] = None) -> dict[str, str]:
        headers = {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
        }
        if prefer:
            headers["Prefer"] = prefer
        return headers

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=15.0,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=0),
        )

    async def push_dashboard(self, payload: dict) -> bool:
        """Envia o snapshot atualizado do banco + Mini-SAD para o Supabase."""
        if not self.enabled:
            return False

        endpoint = f"{self.url}/rest/v1/buscador_estado?on_conflict=id"
        body = {
            "id": "principal",
            "payload": payload,
            "atualizado_em": now_iso(),
        }
        try:
            async with self._client() as client:
                resp = await client.post(
                    endpoint,
                    headers=self._headers("resolution=merge-duplicates,return=minimal"),
                    json=body,
                )
                if resp.status_code in (200, 201, 204):
                    return True
                logger.warning("Falha ao sincronizar estado no Supabase (HTTP %s): %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.warning("Erro ao enviar estado para o Supabase: %s", exc)
        return False

    async def fetch_pending_commands(self, limit: int = 5) -> list[dict]:
        """Busca comandos pendentes enviados pelo site na Vercel."""
        if not self.enabled:
            return []

        endpoint = (
            f"{self.url}/rest/v1/buscador_comandos"
            f"?status=eq.pendente&order=id.asc&limit={limit}"
        )
        try:
            async with self._client() as client:
                resp = await client.get(endpoint, headers=self._headers())
                if resp.status_code == 200:
                    data = resp.json()
                    return data if isinstance(data, list) else []
        except Exception as exc:
            logger.debug("Erro ao consultar comandos pendentes no Supabase: %s", exc)
        return []

    async def update_command(
        self,
        command_id: int,
        status: str,
        resultado: Optional[dict] = None,
    ) -> None:
        if not self.enabled:
            return

        endpoint = f"{self.url}/rest/v1/buscador_comandos?id=eq.{command_id}"
        body = {
            "status": status,
            "resultado": resultado or {},
            "processado_em": now_iso(),
        }
        try:
            async with self._client() as client:
                await client.patch(
                    endpoint,
                    headers=self._headers("return=minimal"),
                    json=body,
                )
        except Exception as exc:
            logger.warning("Erro ao atualizar comando #%d no Supabase: %s", command_id, exc)

    async def cleanup_old_commands(self) -> None:
        """Remove comandos já concluídos antigos para manter o Supabase sempre leve."""
        if not self.enabled:
            return

        endpoint = (
            f"{self.url}/rest/v1/buscador_comandos"
            "?status=neq.pendente&order=id.desc&offset=30&select=id"
        )
        try:
            async with self._client() as client:
                resp = await client.get(endpoint, headers=self._headers())
                if resp.status_code == 200:
                    rows = resp.json()
                    if isinstance(rows, list) and rows:
                        ids = ",".join(str(r["id"]) for r in rows if "id" in r)
                        if ids:
                            await client.delete(
                                f"{self.url}/rest/v1/buscador_comandos?id=in.({ids})",
                                headers=self._headers("return=minimal"),
                            )
        except Exception:
            pass
