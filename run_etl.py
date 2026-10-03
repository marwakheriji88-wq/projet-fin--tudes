# -*- coding: utf-8 -*-
"""
Pipeline ETL — orchestration Extract → Transform → Load.

Point d'entrée formalisé de la collecte (méthode ETL retenue : batch Python/pandas).
Réutilise les fonctions éprouvées de `scraping.py` via le package `etl/`.

Utilisation :
    python run_etl.py               # pipeline complet (E → T → L) + base
    python run_etl.py --offline     # rejeu depuis les données brutes archivées (sans réseau)
    python run_etl.py --no-reports  # sans les rapports annuels (plus rapide)
    python run_etl.py --stars       # couche 6 : OCR complémentaire (STARS)
"""

import sys
from etl import extract, transform, load


def run(offline=False, do_reports=True):
    print("=== ETL · EXTRACT ===")
    data = extract.run_extract(offline, do_reports)
    print("\n=== ETL · TRANSFORM ===")
    t = transform.run_transform(data)
    print("\n=== ETL · LOAD ===")
    meta = load.run_load(data, t)
    print(f"\n[ETL terminé] Universités : {meta['n_universities']} | Lignes : {meta['n_rows']} | "
          f"ARWU : {meta['n_arwu']} | Disciplines : {meta['n_subjects']} | Rapports : {meta['n_reports']}")
    return meta


if __name__ == "__main__":
    if "--stars" in sys.argv:
        import stars_ocr                       # couche 6 — OCR complémentaire (STARS)
        stars_ocr.run()
    else:
        run(offline="--offline" in sys.argv, do_reports="--no-reports" not in sys.argv)
