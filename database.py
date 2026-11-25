from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import re
import sqlite3
from typing import Iterator, Optional

from config import CHECK_INTERVAL_HOURS, DB_PATH, MAX_LINKS
from sad import analyze_product_history, build_ranking

BRT = timezone(timedelta(hours=-3))

UNIT_MULTIPLIERS = {
    "minutos": 1,
    "horas": 60,
    "dias": 1440,
    "semanas": 10080,
}

UNIT_LABELS = {
    "minutos": ("minuto", "minutos"),
    "horas": ("hora", "horas"),
    "dias": ("dia", "dias"),
    "semanas": ("semana", "semanas"),
}


def now_dt() -> datetime:
    return datetime.now(BRT)


def now_iso() -> str:
    return now_dt().isoformat(timespec="seconds")


def parse_iso(iso_str: Optional[str]) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=BRT)
        return dt
    except ValueError:
        return None


def format_timestamp(iso_str: Optional[str]) -> str:
    dt = parse_iso(iso_str)
    if not dt:
        return "Nunca"
    return dt.strftime("%d/%m/%Y %H:%M")


def parse_user_date(raw: Optional[str]) -> Optional[str]:
    """Converte data digitada pelo usuário (DD/MM/AAAA, AAAA-MM-DD ou DD/MM) para AAAA-MM-DD."""
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None

    m_iso = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if m_iso:
        dt = datetime(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        return dt.strftime("%Y-%m-%d")

    m_br = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", text)
    if m_br:
        dt = datetime(int(m_br.group(3)), int(m_br.group(2)), int(m_br.group(1)))
        return dt.strftime("%Y-%m-%d")

    m_short = re.match(r"^(\d{1,2})/(\d{1,2})$", text)
    if m_short:
        dt = datetime(now_dt().year, int(m_short.group(2)), int(m_short.group(1)))
        return dt.strftime("%Y-%m-%d")

    raise ValueError("Formato de data inválido. Use DD/MM/AAAA (ex: 18/11/2026) ou AAAA-MM-DD.")


def format_date_br(date_iso: Optional[str]) -> str:
    if not date_iso:
        return "—"
    part = str(date_iso)[:10]
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", part)
    if m:
        return f"{m.group(3)}/{m.group(2)}/{m.group(1)}"
    return part


def build_schedule(valor: int, unidade: str) -> tuple[int, str]:
    unit_key = unidade.strip().lower()
    if unit_key not in UNIT_MULTIPLIERS:
        raise ValueError("Unidade inválida. Use: minutos, horas, dias ou semanas.")
    minutes = int(valor) * UNIT_MULTIPLIERS[unit_key]
    singular, plural = UNIT_LABELS[unit_key]
    word = singular if valor == 1 else plural
    return minutes, f"{valor} {word}"


def format_minutes_human(minutes: int) -> str:
    if minutes % 10080 == 0:
        w = minutes // 10080
        return f"{w} {'semana' if w == 1 else 'semanas'}"
    if minutes % 1440 == 0:
        d = minutes // 1440
        return f"{d} {'dia' if d == 1 else 'dias'}"
    if minutes % 60 == 0:
        h = minutes // 60
        return f"{h} {'hora' if h == 1 else 'horas'}"
    return f"{minutes} {'minuto' if minutes == 1 else 'minutos'}"


class Database:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA cache_size = -1024;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            try:
                conn.execute("PRAGMA shrink_memory;")
            except Exception:
                pass
            conn.close()

    def checkpoint_and_optimize(self) -> None:
        """Garante integridade no disco e reduz cache interno do SQLite sem perder dados."""
        with self._connect() as conn:
            conn.execute("PRAGMA optimize;")
            conn.execute("PRAGMA shrink_memory;")

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS produtos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT NOT NULL UNIQUE,
                    nome TEXT NOT NULL,
                    loja TEXT NOT NULL,
                    canal_id INTEGER NOT NULL,
                    usuario_id INTEGER NOT NULL,
                    ultimo_preco REAL,
                    moeda TEXT NOT NULL DEFAULT 'BRL',
                    criado_em TEXT NOT NULL,
                    atualizado_em TEXT,
                    intervalo_minutos INTEGER,
                    intervalo_label TEXT
                );

                CREATE TABLE IF NOT EXISTS historico_precos (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    produto_id INTEGER NOT NULL,
                    preco REAL NOT NULL,
                    moeda TEXT NOT NULL DEFAULT 'BRL',
                    fonte TEXT NOT NULL,
                    coletado_em TEXT NOT NULL,
                    FOREIGN KEY (produto_id) REFERENCES produtos(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS configuracoes (
                    chave TEXT PRIMARY KEY,
                    valor TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_historico_produto
                    ON historico_precos(produto_id, coletado_em);
                """
            )
            # Migração segura para bancos já existentes
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(produtos)").fetchall()
            }
            if "intervalo_minutos" not in cols:
                conn.execute("ALTER TABLE produtos ADD COLUMN intervalo_minutos INTEGER")
            if "intervalo_label" not in cols:
                conn.execute("ALTER TABLE produtos ADD COLUMN intervalo_label TEXT")

            # Inicializa intervalo global padrão caso ainda não exista no banco
            default_min = max(5, int(round(CHECK_INTERVAL_HOURS * 60)))
            default_label = format_minutes_human(default_min)
            conn.execute(
                "INSERT OR IGNORE INTO configuracoes (chave, valor) VALUES ('global_interval_minutes', ?)",
                (str(default_min),),
            )
            conn.execute(
                "INSERT OR IGNORE INTO configuracoes (chave, valor) VALUES ('global_interval_label', ?)",
                (default_label,),
            )

    def get_global_schedule(self) -> tuple[int, str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT chave, valor FROM configuracoes WHERE chave IN ('global_interval_minutes', 'global_interval_label')"
            ).fetchall()
            cfg = {r["chave"]: r["valor"] for r in rows}
            minutes = int(cfg.get("global_interval_minutes", max(5, int(round(CHECK_INTERVAL_HOURS * 60)))))
            label = cfg.get("global_interval_label") or format_minutes_human(minutes)
            return minutes, label

    def set_global_schedule(self, minutes: int, label: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO configuracoes (chave, valor) VALUES ('global_interval_minutes', ?) "
                "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
                (str(minutes),),
            )
            conn.execute(
                "INSERT INTO configuracoes (chave, valor) VALUES ('global_interval_label', ?) "
                "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
                (label,),
            )

    def set_product_schedule(
        self, product_id: int, minutes: Optional[int], label: Optional[str]
    ) -> Optional[dict]:
        product = self.get_product(product_id)
        if not product:
            return None
        with self._connect() as conn:
            conn.execute(
                "UPDATE produtos SET intervalo_minutos = ?, intervalo_label = ? WHERE id = ?",
                (minutes, label, product_id),
            )
        return self.get_product(product_id)

    def count_products(self) -> int:
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*) AS total FROM produtos").fetchone()
            return int(row["total"])

    def get_product_by_url(self, url: str) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM produtos WHERE url = ?", (url,)).fetchone()
            return dict(row) if row else None

    def get_product(self, product_id: int) -> Optional[dict]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM produtos WHERE id = ?", (product_id,)).fetchone()
            return dict(row) if row else None

    def list_products(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM produtos ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def add_product(
        self,
        url: str,
        nome: str,
        loja: str,
        canal_id: int,
        usuario_id: int,
        preco: Optional[float],
        moeda: str = "BRL",
        fonte: str = "desconhecida",
    ) -> dict:
        if self.count_products() >= MAX_LINKS:
            raise ValueError(f"Limite máximo de {MAX_LINKS} links atingido.")

        agora = now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO produtos (url, nome, loja, canal_id, usuario_id, ultimo_preco, moeda, criado_em, atualizado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (url, nome, loja, canal_id, usuario_id, preco, moeda, agora, agora if preco is not None else None),
            )
            product_id = cursor.lastrowid
            if preco is not None:
                conn.execute(
                    """
                    INSERT INTO historico_precos (produto_id, preco, moeda, fonte, coletado_em)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (product_id, preco, moeda, fonte, agora),
                )

        return self.get_product(product_id)

    def record_price(
        self,
        product_id: int,
        preco: float,
        moeda: str = "BRL",
        fonte: str = "desconhecida",
        novo_nome: Optional[str] = None,
    ) -> Optional[float]:
        """Salva nova leitura de preço e retorna o preço anterior (se houver)."""
        product = self.get_product(product_id)
        if not product:
            return None

        preco_anterior = product["ultimo_preco"]
        agora = now_iso()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO historico_precos (produto_id, preco, moeda, fonte, coletado_em)
                VALUES (?, ?, ?, ?, ?)
                """,
                (product_id, preco, moeda, fonte, agora),
            )
            if novo_nome and product["nome"].startswith("Produto ("):
                conn.execute(
                    """
                    UPDATE produtos
                    SET ultimo_preco = ?, moeda = ?, atualizado_em = ?, nome = ?
                    WHERE id = ?
                    """,
                    (preco, moeda, agora, novo_nome, product_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE produtos
                    SET ultimo_preco = ?, moeda = ?, atualizado_em = ?
                    WHERE id = ?
                    """,
                    (preco, moeda, agora, product_id),
                )

        return preco_anterior

    def import_historical_points(
        self,
        product_id: int,
        points: list[tuple[str, float]],
        moeda: str = "BRL",
        fonte: str = "buscape_zoom_historico",
    ) -> int:
        """Importa pontos históricos diários reais sem duplicar datas já registradas."""
        if not points:
            return 0

        inserted = 0
        today_str = now_dt().strftime("%Y-%m-%d")
        with self._connect() as conn:
            existing_dates = {
                row["d"]
                for row in conn.execute(
                    "SELECT DISTINCT substr(coletado_em, 1, 10) AS d FROM historico_precos WHERE produto_id = ?",
                    (product_id,),
                ).fetchall()
            }
            for date_str, price_val in points:
                # Hoje já é gravado com o horário real da coleta
                if date_str in existing_dates or date_str == today_str:
                    continue
                iso_ts = f"{date_str}T12:00:00-03:00"
                conn.execute(
                    """
                    INSERT INTO historico_precos (produto_id, preco, moeda, fonte, coletado_em)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (product_id, float(price_val), moeda, fonte, iso_ts),
                )
                existing_dates.add(date_str)
                inserted += 1
        return inserted

    def remove_product(self, product_id: int) -> Optional[dict]:
        product = self.get_product(product_id)
        if not product:
            return None
        with self._connect() as conn:
            conn.execute("DELETE FROM produtos WHERE id = ?", (product_id,))
        return product

    def get_history(
        self,
        product_id: int,
        limit: int = 100,
        start_date: Optional[str] = None,
    ) -> list[dict]:
        with self._connect() as conn:
            if start_date:
                rows = conn.execute(
                    """
                    SELECT * FROM historico_precos
                    WHERE produto_id = ? AND substr(coletado_em, 1, 10) >= ?
                    ORDER BY coletado_em DESC, id DESC
                    LIMIT ?
                    """,
                    (product_id, start_date[:10], limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM historico_precos
                    WHERE produto_id = ?
                    ORDER BY coletado_em DESC, id DESC
                    LIMIT ?
                    """,
                    (product_id, limit),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_price_stats(self, product_id: int, start_date: Optional[str] = None) -> Optional[dict]:
        with self._connect() as conn:
            if start_date:
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COUNT(DISTINCT substr(coletado_em, 1, 10)) AS dias_distintos,
                        MIN(preco) AS menor,
                        MAX(preco) AS maior,
                        AVG(preco) AS media
                    FROM historico_precos
                    WHERE produto_id = ? AND substr(coletado_em, 1, 10) >= ?
                    """,
                    (product_id, start_date[:10]),
                ).fetchone()
            else:
                row = conn.execute(
                    """
                    SELECT
                        COUNT(*) AS total,
                        COUNT(DISTINCT substr(coletado_em, 1, 10)) AS dias_distintos,
                        MIN(preco) AS menor,
                        MAX(preco) AS maior,
                        AVG(preco) AS media
                    FROM historico_precos
                    WHERE produto_id = ?
                    """,
                    (product_id,),
                ).fetchone()
            if not row or row["total"] == 0:
                return None
            return dict(row)

    def get_all_history_for_export(self, start_date: Optional[str] = None) -> list[dict]:
        with self._connect() as conn:
            if start_date:
                rows = conn.execute(
                    """
                    SELECT
                        p.id AS produto_id,
                        p.nome AS produto_nome,
                        p.loja AS loja,
                        p.url AS url,
                        h.preco AS preco,
                        h.moeda AS moeda,
                        h.fonte AS fonte,
                        h.coletado_em AS coletado_em
                    FROM historico_precos h
                    JOIN produtos p ON p.id = h.produto_id
                    WHERE substr(h.coletado_em, 1, 10) >= ?
                    ORDER BY h.coletado_em ASC, h.id ASC
                    """,
                    (start_date[:10],),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT
                        p.id AS produto_id,
                        p.nome AS produto_nome,
                        p.loja AS loja,
                        p.url AS url,
                        h.preco AS preco,
                        h.moeda AS moeda,
                        h.fonte AS fonte,
                        h.coletado_em AS coletado_em
                    FROM historico_precos h
                    JOIN produtos p ON p.id = h.produto_id
                    ORDER BY h.coletado_em ASC, h.id ASC
                    """
                ).fetchall()
            return [dict(r) for r in rows]

    def get_dashboard_payload(self, start_date: Optional[str] = None) -> dict:
        """Retorna todos os produtos, estatísticas, histórico, Mini-SAD e ranking para a API web."""
        global_min, global_label = self.get_global_schedule()
        products = self.list_products()
        enriched = []
        total_readings = 0
        all_distinct_dates: set[str] = set()

        for p in products:
            history_desc = self.get_history(p["id"], limit=200, start_date=start_date)
            history_asc = list(reversed(history_desc))
            stats = self.get_price_stats(p["id"], start_date=start_date)
            total_readings += stats["total"] if stats else 0

            for h in history_asc:
                if h.get("coletado_em"):
                    all_distinct_dates.add(str(h["coletado_em"])[:10])

            sad_result = analyze_product_history(history_asc, currency=p["moeda"])

            variacao_pct = None
            if len(history_asc) >= 2 and history_asc[0]["preco"] > 0:
                primeiro = history_asc[0]["preco"]
                atual = history_asc[-1]["preco"]
                variacao_pct = round(((atual - primeiro) / primeiro) * 100, 2)

            enriched.append(
                {
                    **p,
                    "atualizado_em_fmt": format_timestamp(p["atualizado_em"]),
                    "criado_em_fmt": format_timestamp(p["criado_em"]),
                    "criado_em_data": str(p["criado_em"])[:10],
                    "agendamento_efetivo_minutos": p["intervalo_minutos"] or global_min,
                    "agendamento_efetivo_label": p["intervalo_label"] or f"Global ({global_label})",
                    "usa_agendamento_individual": p["intervalo_minutos"] is not None,
                    "stats": stats,
                    "variacao_pct": variacao_pct,
                    "sad": sad_result,
                    "historico": [
                        {
                            **h,
                            "coletado_em_fmt": format_timestamp(h["coletado_em"]),
                            "data_iso": str(h["coletado_em"])[:10],
                        }
                        for h in history_asc
                    ],
                }
            )

        ranking = build_ranking(enriched)

        return {
            "max_links": MAX_LINKS,
            "total_produtos": len(enriched),
            "total_coletas": total_readings,
            "dias_distintos_totais": len(all_distinct_dates),
            "filtro_data_inicio": start_date,
            "agendamento_global": {
                "minutos": global_min,
                "label": global_label,
            },
            "atualizado_em": now_iso(),
            "atualizado_em_fmt": format_timestamp(now_iso()),
            "ranking_sad": ranking,
            "produtos": enriched,
        }
