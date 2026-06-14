# SeeMove

Sistema de acompanhamento postural com Wii Balance Board.  
Feedback 100% auditivo — acessível para pessoas com deficiência visual.

---

## Estrutura do projeto

```
seemove/
├── main.py                    # Ponto de entrada
├── requirements.txt
├── core/
│   ├── balance_board.py       # Hardware real + simulador
│   ├── cog.py                 # Cálculo do Centro de Gravidade
│   └── session.py             # Loop principal da sessão
├── audio/
│   ├── tts_engine.py          # Síntese de voz (pyttsx3)
│   └── sonification.py        # Bipes direcionais (sounddevice)
├── exercises/
│   ├── base.py                # Classe base + FeedbackResult
│   ├── implementations.py     # Agachamento, unipodial, estático, lunge
│   └── registry.py            # Fábrica de exercícios
├── reports/
│   └── reporter.py            # CSV, JSON, resumo textual
└── config/
    └── settings.py            # Parâmetros globais
```

---

## Instalação

```bash
pip install -r requirements.txt
```

Para hardware real (Balance Board físico), instale PyBluez:
```bash
pip install PyBluez
```

> **Windows**: PyBluez requer Python 32-bit ou compilação manual.  
> Alternativa: `pip install PyBluez-updated`

---

## Uso

### Modo simulação (sem hardware)
```bash
python main.py
python main.py --exercise balance
python main.py --exercise lunge --threshold 0.20
```

### Modo hardware (Balance Board físico)
```bash
# 1. Descubra o MAC do seu dispositivo
python main.py --list-devices

# 2. Inicie com hardware real
python main.py --hardware --exercise squat
```

### Opções completas
```
--hardware          Conecta ao Balance Board via Bluetooth
--list-devices      Lista dispositivos Bluetooth e encerra
--exercise          squat | balance | stand | lunge  (padrão: squat)
--threshold 0.15    Limiar de desvio CoG para feedback (padrão: 0.15)
--no-tts            Desativa síntese de voz
--no-sonification   Desativa bipes direcionais
--report FILE.csv   Salva relatório ao encerrar
```

### Exemplo com relatório
```bash
python main.py --exercise squat --report sessao_2024-01-15.csv
```

---

## Arquitetura — fluxo de dados

```
Balance Board (Bluetooth)
        │
        ▼
  SensorData (4 × float kg)
        │
        ▼
  calculate_cog()  →  CoGReading (x, y, magnitude)
        │
        ├──▶ CoGHistory (buffer circular, suavização)
        │
        ├──▶ Exercise.analyze()  →  FeedbackResult
        │           │
        │           ├──▶ TTSEngine.speak()       (voz sintetizada)
        │           └──▶ SonificationEngine.play() (bipe direcional)
        │
        └──▶ SessionReporter.record()  →  CSV / JSON
```

---

## Cálculo do Centro de Gravidade

```
Sensores:  TL (Frente-Esq)   TR (Frente-Dir)
           BL (Trás-Esq)     BR (Trás-Dir)

total = TL + TR + BL + BR

X = [(TR + BR) - (TL + BL)] / total   → -1 (esq) a +1 (dir)
Y = [(TL + TR) - (BL + BR)] / total   → -1 (trás) a +1 (frente)
```

---

## Exercícios disponíveis

| Chave | Nome | Limiar X | Limiar Y | Observação |
|---|---|---|---|---|
| `squat` | Agachamento | 0.12 | 0.20 | Tolerante no eixo Y |
| `balance` | Equilíbrio unipodial | 0.25 | 0.15 | Aceita oscilações naturais |
| `stand` | Postura estática | 0.08 | 0.10 | Limiares mais rígidos |
| `lunge` | Avanço (lunge) | 0.18 | 0.35 | Assimetria ântero-post. esperada |

---

## Adicionando novos exercícios

```python
# exercises/implementations.py

from exercises.base import Exercise, FeedbackResult

class MeuExercicio(Exercise):
    name = "Meu Exercício"
    start_message = "Iniciando meu exercício."
    end_message = "Concluído."

    def analyze(self, cog_x, cog_y, total_kg) -> FeedbackResult:
        if abs(cog_x) > 0.15:
            return FeedbackResult(
                "Centralize o peso lateralmente",
                should_speak=True,
                severity="warn",
                cog_x=cog_x, cog_y=cog_y,
            )
        return FeedbackResult(
            "Postura correta",
            should_speak=False,
            severity="ok",
            cog_x=cog_x, cog_y=cog_y,
        )
```

Registre em `exercises/registry.py`:
```python
_REGISTRY = {
    ...
    "meu_ex": MeuExercicio,
}
```

---

## Vozes em português no Windows

Para verificar vozes disponíveis:
```python
import pyttsx3
e = pyttsx3.init()
for v in e.getProperty('voices'):
    print(v.id, v.name)
```

Para instalar vozes pt-BR:  
**Configurações → Hora e idioma → Fala → Adicionar vozes → Português (Brasil)**

---

## Protocolo Bluetooth do Balance Board

O Balance Board se identifica como `Nintendo RVL-WBC-01` e usa o protocolo Wiimote via L2CAP HID:

- Canal de controle: porta **17**
- Canal de interrupção: porta **19**
- Relatório de extensão: `0x32` (8 bytes de sensores)
- Calibração: memória `0xA40024` (24 bytes, 3 pontos × 4 sensores × 2 bytes)

Para uma implementação completa de baixo nível, consulte:  
https://wiibrew.org/wiki/Wii_Balance_Board

---

## Relatório da sessão

O CSV gerado contém:

| Coluna | Descrição |
|---|---|
| `timestamp` | Unix timestamp da leitura |
| `tl_kg` .. `br_kg` | Pressão em kg por sensor |
| `total_kg` | Peso total sobre a plataforma |
| `cog_x` | Desvio lateral (-1 a +1) |
| `cog_y` | Desvio ântero-posterior (-1 a +1) |
| `magnitude` | Distância do CoG ao centro |
| `is_centered` | 1 = dentro do limiar, 0 = desviado |
| `stability_pct` | Percentual de estabilidade (0-100) |

---

## Versao com Kinect RGB + MediaPipe

Esta copia separada inclui suporte complementar ao Kinect v1 (Xbox 360)
usando somente a camera RGB como entrada de video. O rastreamento de
esqueleto e feito por MediaPipe Pose, sem depender do SDK do Kinect nem do
sensor de profundidade.

Fluxo adicional:

```text
Kinect v1 RGB -> OpenCV -> MediaPipe Pose -> juntas corporais -> dashboard
```

Na dashboard existe uma aba `Kinect` com:

- selecao entre `Webcam no navegador`, `Webcam via Python` e `Kinect / camera USB`;
- botao para conectar/desconectar a camera;
- seletor de indice da camera (`0`, `1`, `2`...);
- visualizacao do video com o esqueleto desenhado;
- metricas de confianca, inclinacao dos ombros, inclinacao do quadril e
  posicao vertical dos joelhos.

Dependencias adicionais:

```bash
pip install opencv-python mediapipe
```

Observacao: o Kinect v1 de Xbox 360 precisa do adaptador/fonte USB para ser
reconhecido como camera no Windows. Se a imagem nao aparecer com indice `0`,
tente outro indice no campo da aba Kinect.

Para teste sem Kinect, prefira `Webcam no navegador` e clique em
`Conectar camera`. Esse modo usa MediaPipe Tasks em JavaScript, acessa a webcam
direto pelo navegador e nao depende do pacote `mediapipe` instalado no Python.

Use `Webcam via Python` ou `Kinect / camera USB` somente quando quiser testar a
captura pelo backend OpenCV. Versoes novas do pacote Python `mediapipe` podem
nao expor mais a API antiga `mediapipe.solutions`; nesses casos o modo do
navegador continua sendo o caminho mais estavel para validar o esqueleto.
