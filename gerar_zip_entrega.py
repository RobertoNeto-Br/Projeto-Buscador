"""
Gera automaticamente `dados_coletados.csv` e empacota o arquivo único
`Entrega_MiniSAD_Buscador.zip` pronto para envio no AVA (sem expor .env nem .venv).
"""

import csv
from pathlib import Path
import zipfile

from database import Database

BASE_DIR = Path(__file__).resolve().parent
CSV_PATH = BASE_DIR / "dados_coletados.csv"
ZIP_PATH = BASE_DIR / "Entrega_MiniSAD_Buscador.zip"

INCLUDE_FILES = [
    "main.py",
    "sad.py",
    "scraper.py",
    "database.py",
    "api_server.py",
    "config.py",
    "gerar_zip_entrega.py",
    "requirements.txt",
    "discloud.config",
    ".env.example",
    "README.md",
    "dados_coletados.csv",
]


def main() -> None:
    db = Database()
    rows = db.get_all_history_for_export()
    distinct_days = {str(r["coletado_em"])[:10] for r in rows if r.get("coletado_em")}

    with CSV_PATH.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
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

    print(f"[OK] CSV gerado em: {CSV_PATH} ({len(rows)} registros | {len(distinct_days)} dia(s) distintos)")
    if len(distinct_days) < 5:
        print(
            "[AVISO] Voce tem menos de 5 dias distintos coletados ainda! "
            "Adicione pelo menos 1 produto do Buscape ou Zoom para importar ate 40 dias reais."
        )

    screenshots = [
        p
        for p in BASE_DIR.iterdir()
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")
    ]
    if not screenshots:
        print(
            "[AVISO] Nenhuma captura de tela (.png/.jpg) encontrada na pasta raiz! "
            "Salve um print do dashboard (ex: dashboard.png) na pasta Buscador e rode este script novamente."
        )

    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel in INCLUDE_FILES:
            fp = BASE_DIR / rel
            if fp.exists():
                zf.write(fp, arcname=rel)

        # Inclui banco SQLite
        db_file = BASE_DIR / "data" / "buscador.db"
        if db_file.exists():
            zf.write(db_file, arcname="data/buscador.db")

        # Inclui pasta web/
        web_dir = BASE_DIR / "web"
        if web_dir.exists():
            for fp in web_dir.rglob("*"):
                if fp.is_file():
                    zf.write(fp, arcname=str(fp.relative_to(BASE_DIR)))

        # Inclui capturas de tela da pasta raiz
        for img in screenshots:
            zf.write(img, arcname=img.name)

    print(f"[OK] ZIP pronto para entrega no AVA: {ZIP_PATH}")
    print(f"     Capturas incluidas: {[s.name for s in screenshots]}")


if __name__ == "__main__":
    main()
