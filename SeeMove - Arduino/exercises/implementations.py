"""
exercises/implementations.py
Feedback de áudio projetado para não poluir — frases curtas,
uma instrução por vez, só fala quando há mudança real de estado.
"""

import math
from exercises.base import Exercise, FeedbackResult


class SquatExercise(Exercise):
    name = "Agachamento"
    start_message = "Agachamento. Pés na largura dos ombros."
    end_message = "Bom trabalho."
    THRESHOLD_X = 0.12
    THRESHOLD_Y = 0.22

    def analyze(self, cog_x, cog_y, total_kg) -> FeedbackResult:
        if total_kg < 20:
            return FeedbackResult("Suba na plataforma.", True, "warn", cog_x, cog_y)

        # Prioriza o maior desvio — uma instrução por vez
        ax, ay = abs(cog_x), abs(cog_y)
        if ax > self.THRESHOLD_X and ax >= ay:
            sev = "error" if ax > 0.30 else "warn"
            msg = "Esquerda." if cog_x > 0 else "Direita."
            return FeedbackResult(msg, True, sev, cog_x, cog_y)
        if ay > self.THRESHOLD_Y:
            msg = "Recue o quadril." if cog_y > 0 else "Avance o peso."
            return FeedbackResult(msg, True, "warn", cog_x, cog_y)

        return FeedbackResult("Certo.", False, "ok", cog_x, cog_y)


class UnipodialBalanceExercise(Exercise):
    name = "Equilíbrio unipodial"
    start_message = "Equilíbrio. Eleve uma perna devagar."
    end_message = "Ótimo controle."
    THRESHOLD_MAG = 0.38

    def analyze(self, cog_x, cog_y, total_kg) -> FeedbackResult:
        mag = math.sqrt(cog_x**2 + cog_y**2)
        if mag > 0.55:
            return FeedbackResult("Apoie as duas pernas.", True, "error", cog_x, cog_y)
        if mag > self.THRESHOLD_MAG:
            return FeedbackResult("Foque em um ponto fixo.", True, "warn", cog_x, cog_y)
        return FeedbackResult("Ótimo.", False, "ok", cog_x, cog_y)


class StaticPostureExercise(Exercise):
    name = "Postura estática"
    start_message = "Postura estática. Fique relaxado."
    end_message = "Avaliação concluída."
    THRESHOLD_X = 0.08
    THRESHOLD_Y = 0.10

    def analyze(self, cog_x, cog_y, total_kg) -> FeedbackResult:
        ax, ay = abs(cog_x), abs(cog_y)
        if ax > self.THRESHOLD_X and ax >= ay:
            sev = "error" if ax > 0.20 else "warn"
            msg = "Peso à esquerda." if cog_x > 0 else "Peso à direita."
            return FeedbackResult(msg, True, sev, cog_x, cog_y)
        if ay > self.THRESHOLD_Y:
            msg = "Recue." if cog_y > 0 else "Avance."
            return FeedbackResult(msg, True, "warn", cog_x, cog_y)
        return FeedbackResult("Alinhado.", False, "ok", cog_x, cog_y)


class LungeExercise(Exercise):
    name = "Avanço (lunge)"
    start_message = "Avanço. Coluna ereta."
    end_message = "Concluído."
    THRESHOLD_X = 0.18

    def analyze(self, cog_x, cog_y, total_kg) -> FeedbackResult:
        if abs(cog_x) > self.THRESHOLD_X:
            msg = "Esquerda." if cog_x > 0 else "Direita."
            return FeedbackResult(msg, True, "warn", cog_x, cog_y)
        if cog_y < -0.42:
            return FeedbackResult("Incline para frente.", True, "warn", cog_x, cog_y)
        return FeedbackResult("Certo.", False, "ok", cog_x, cog_y)
