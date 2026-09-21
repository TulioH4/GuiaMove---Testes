"""
tools/braille_plaquinha.py
Gera um STL (placa retangular + pontos de braile em cúpula) a partir de texto
JÁ transcrito em braile Unicode (U+2800..U+28FF). O script NÃO interpreta nem
corrige o texto: cada caractere braile vira exatamente os pontos que ele
representa. A responsabilidade pela transcrição correta é de quem a gerou.

Uso:
    python tools/braille_plaquinha.py                 # usa TEXTO abaixo
    python tools/braille_plaquinha.py -o placa.stl
Sem dependências externas. Unidades: milímetros.
"""

import argparse
import math
import struct

# ── Parâmetros (padrão de sinalização tátil; ajuste conforme a impressora) ──
LARGURA_MM   = 200.0   # L
ALTURA_MM    = 150.0   # A
ESPESSURA_MM = 3.0     # espessura da base

DOT_DIAM_MM   = 1.6    # diâmetro da base do ponto
DOT_HEIGHT_MM = 0.8    # altura da cúpula (FDM: 0.8~1.0 sai melhor que 0.6)
DOT_SPACING   = 2.5    # entre pontos da mesma célula (horizontal e vertical)
CELL_PITCH    = 6.2    # entre células (centro a centro do 1º ponto)
LINE_PITCH    = 10.2   # entre linhas
MARGEM_MM     = 10.0

SEGMENTOS = 20         # resolução radial dos pontos
ANEIS     = 5          # anéis da cúpula
EMBUTIR   = 0.2        # quanto o ponto entra na base (fusão limpa no fatiador)

TEXTO = ("⠠⠛⠥⠊⠁⠠⠍⠕⠧⠑ ⠤ ⠠⠑⠭⠑⠗⠉⠌⠉⠊⠕ ⠋⠌⠎⠊⠉⠕ ⠛⠥⠊⠁⠙⠕ ⠏⠕⠗ ⠧⠕⠵⠂ "
         "⠏⠁⠗⠁ ⠏⠑⠎⠎⠕⠁⠎ ⠉⠑⠛⠁⠎ ⠕⠥ ⠉⠕⠍ ⠃⠁⠊⠭⠁ ⠧⠊⠎⠜⠕")


def pontos_da_celula(ch):
    """Retorna lista de (col, row) 0-based dos pontos 1-6 ativos."""
    bits = ord(ch) - 0x2800
    mapa = {0: (0, 0), 1: (0, 1), 2: (0, 2), 3: (1, 0), 4: (1, 1), 5: (1, 2)}
    return [mapa[i] for i in range(6) if bits & (1 << i)]


def quebrar_linhas(texto, max_celulas):
    """Quebra por palavras (espaço comum ou U+2800) sem cortar palavras."""
    palavras = texto.replace("⠀", " ").split()
    linhas, atual = [], ""
    for p in palavras:
        if len(p) > max_celulas:
            raise ValueError(f"Palavra maior que a linha ({len(p)} > {max_celulas}): {p}")
        tentativa = p if not atual else atual + " " + p
        if len(tentativa) <= max_celulas:
            atual = tentativa
        else:
            linhas.append(atual)
            atual = p
    if atual:
        linhas.append(atual)
    return linhas


class Malha:
    def __init__(self):
        self.tris = []

    def tri(self, a, b, c):
        self.tris.append((a, b, c))

    def quad(self, a, b, c, d):
        self.tri(a, b, c)
        self.tri(a, c, d)

    def caixa(self, x0, y0, z0, x1, y1, z1):
        p = [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0),
             (x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)]
        self.quad(p[0], p[3], p[2], p[1])  # base (normal -z)
        self.quad(p[4], p[5], p[6], p[7])  # topo
        self.quad(p[0], p[1], p[5], p[4])
        self.quad(p[1], p[2], p[6], p[5])
        self.quad(p[2], p[3], p[7], p[6])
        self.quad(p[3], p[0], p[4], p[7])

    def cupula(self, cx, cy, z_base):
        """Calota esférica com raio de base r e altura h, embutida na base."""
        r, h = DOT_DIAM_MM / 2, DOT_HEIGHT_MM
        R = (r * r + h * h) / (2 * h)
        zc = z_base + h - R
        theta_max = math.asin(min(1.0, r / R))
        aneis = []
        for i in range(ANEIS + 1):
            th = theta_max * i / ANEIS
            aneis.append((R * math.sin(th), zc + R * math.cos(th)))
        z_fundo = z_base - EMBUTIR
        aneis.append((r, z_fundo))

        def pt(rad, z, k):
            a = 2 * math.pi * k / SEGMENTOS
            return (cx + rad * math.cos(a), cy + rad * math.sin(a), z)

        topo = (cx, cy, aneis[0][1])
        for k in range(SEGMENTOS):
            k2 = (k + 1) % SEGMENTOS
            self.tri(topo, pt(*aneis[1], k), pt(*aneis[1], k2))
        for i in range(1, len(aneis) - 1):
            for k in range(SEGMENTOS):
                k2 = (k + 1) % SEGMENTOS
                a, b = pt(*aneis[i], k), pt(*aneis[i], k2)
                c, d = pt(*aneis[i + 1], k2), pt(*aneis[i + 1], k)
                self.quad(a, d, c, b)
        centro_fundo = (cx, cy, z_fundo)
        for k in range(SEGMENTOS):
            k2 = (k + 1) % SEGMENTOS
            self.tri(centro_fundo, pt(r, z_fundo, k2), pt(r, z_fundo, k))

    def salvar_stl(self, caminho):
        with open(caminho, "wb") as f:
            f.write(b"placa braille".ljust(80, b"\0"))
            f.write(struct.pack("<I", len(self.tris)))
            for a, b, c in self.tris:
                ux, uy, uz = b[0]-a[0], b[1]-a[1], b[2]-a[2]
                vx, vy, vz = c[0]-a[0], c[1]-a[1], c[2]-a[2]
                n = (uy*vz - uz*vy, uz*vx - ux*vz, ux*vy - uy*vx)
                m = math.sqrt(sum(x*x for x in n)) or 1.0
                f.write(struct.pack("<3f", *(x / m for x in n)))
                for v in (a, b, c):
                    f.write(struct.pack("<3f", *v))
                f.write(b"\0\0")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--saida", default="placa_braille.stl")
    ap.add_argument("--texto", default=TEXTO)
    args = ap.parse_args()

    largura_util = LARGURA_MM - 2 * MARGEM_MM
    max_celulas = int((largura_util - DOT_SPACING) // CELL_PITCH) + 1
    linhas = quebrar_linhas(args.texto, max_celulas)

    altura_bloco = (len(linhas) - 1) * LINE_PITCH + 2 * DOT_SPACING
    if altura_bloco > ALTURA_MM - 2 * MARGEM_MM:
        raise SystemExit(f"Texto não cabe: precisa {altura_bloco:.1f} mm de altura útil.")

    malha = Malha()
    malha.caixa(0, 0, 0, LARGURA_MM, ALTURA_MM, ESPESSURA_MM)

    y_topo = (ALTURA_MM + altura_bloco) / 2  # centraliza verticalmente
    total_pontos = 0
    for li, linha in enumerate(linhas):
        largura_linha = (len(linha) - 1) * CELL_PITCH + DOT_SPACING
        x0 = (LARGURA_MM - largura_linha) / 2  # centraliza horizontalmente
        y_linha = y_topo - li * LINE_PITCH
        for ci, ch in enumerate(linha):
            if ch == " ":
                continue
            if not 0x2800 <= ord(ch) <= 0x28FF:
                raise SystemExit(f"Caractere não-braile na linha {li+1}: {ch!r}")
            for col, row in pontos_da_celula(ch):
                cx = x0 + ci * CELL_PITCH + col * DOT_SPACING
                cy = y_linha - row * DOT_SPACING
                malha.cupula(cx, cy, ESPESSURA_MM)
                total_pontos += 1

    malha.salvar_stl(args.saida)
    print(f"Placa {LARGURA_MM:.0f}x{ALTURA_MM:.0f}x{ESPESSURA_MM:.0f} mm | "
          f"{len(linhas)} linhas | {total_pontos} pontos | {len(malha.tris)} triângulos")
    for i, l in enumerate(linhas, 1):
        print(f"  linha {i}: {l}")
    print(f"Salvo em: {args.saida}")


if __name__ == "__main__":
    main()
