"""Calibrer les seuils de décision (``kefind.decide.Thresholds``) depuis des
tickets étiquetés.

Ne réimplémente rien : fait tourner le moteur avec ``scoreboard.runner`` et
lit le résultat avec ``scoreboard.metrics.calibrate`` (balayage de seuil
d'abstention, conservateur via la borne haute de l'IC à 95 %). Le score
utilisé est le score de fusion recherche du temps 2 ; ``calibrate`` trouve
le seuil le plus bas qui garde le taux de fiches fausses sous le plafond
donné — fixez ``min_score`` à ce seuil. Calibrez ``gap`` séparément (par
exemple en comparant le taux de questions utiles à plusieurs valeurs) : ce
que ``calibrate`` mesure est le seuil d'abstention, pas l'écart de question.
"""

from __future__ import annotations

from scoreboard.dataset import Ticket
from scoreboard.metrics import Calibration, calibrate
from scoreboard.runner import run_engine


def calibrate_engine(engine, tickets: list[Ticket], max_wrong: float = 0.05, runs: int = 1) -> Calibration | None:
    records = run_engine(engine, tickets, runs=runs)
    return calibrate(records, max_wrong)


__all__ = ["calibrate_engine"]
