// kinect_color_bridge.cpp
//
// Ponte entre a API oficial do Kinect for Windows SDK v1.8 (NUI) e o
// GuiaMove. O driver oficial do Kinect v1 não expõe a câmera como um
// dispositivo de captura genérico (DirectShow/Media Foundation), então
// cv2.VideoCapture nunca consegue abri-la — só a API NUI (Kinect10.dll)
// tem acesso aos streams de cor e profundidade. Este programa fica
// rodando, lê os dois streams pela API NUI e escreve cada um numa área
// de memória compartilhada nomeada própria; o lado Python
// (core/kinect_sdk_bridge.py) lê essas áreas via mmap, sem precisar de
// nenhum binding direto com a DLL.
//
// Build: ver build.bat neste mesmo diretório.

#include <windows.h>
#include <NuiApi.h>
#include <cstdio>
#include <cstring>

static const int WIDTH  = 640;
static const int HEIGHT = 480;

// -- Stream de cor (RGB32 / BGRA) -------------------------------------------
static const int COLOR_PIXEL_SIZE   = 4;
static const size_t COLOR_FRAME_BYTES  = (size_t)WIDTH * HEIGHT * COLOR_PIXEL_SIZE;
static const size_t HEADER_BYTES       = 16; // frame_counter(u64) + width(u32) + height(u32)
static const size_t COLOR_TOTAL_BYTES  = HEADER_BYTES + COLOR_FRAME_BYTES;
static const char* COLOR_SHM_NAME = "GuiaMoveKinectColor";

// -- Stream de profundidade (USHORT, mm já desempacotado) -------------------
static const size_t DEPTH_FRAME_BYTES  = (size_t)WIDTH * HEIGHT * sizeof(USHORT);
static const size_t DEPTH_TOTAL_BYTES  = HEADER_BYTES + DEPTH_FRAME_BYTES;
static const char* DEPTH_SHM_NAME = "GuiaMoveKinectDepth";

// -- Canal de controle do motor de inclinação --------------------------------
// Python escreve requested_angle+command_seq; a bridge aplica (respeitando o
// cooldown do motor) e escreve de volta current_angle+last_applied_seq, pra
// o dashboard mostrar o ângulo real e saber quando o comando foi aplicado.
struct TiltControl {
    LONG requested_angle;
    LONG command_seq;
    LONG current_angle;
    LONG last_applied_seq;
};
static const size_t CONTROL_TOTAL_BYTES = sizeof(TiltControl);
static const char* CONTROL_SHM_NAME = "GuiaMoveKinectControl";
// Kinect v1: o motor de inclinação rejeita/atrasa comandos muito seguidos
// (a própria API documenta ERROR_RETRY e "too many calls"). 450ms foi
// tentado e travou o motor físico de verdade (a API aceitava os comandos
// sem erro, mas o ângulo real nunca mudava — sinal de estresse mecânico
// por reverter direção rápido demais, muitas vezes seguidas). Voltado
// pra 700ms, que já foi comprovado estável em múltiplos testes. Se
// NuiCameraElevationSetAngle falhar por ser cedo demais, o comando só
// fica pendente e é reaplicado sozinho no próximo ciclo do loop.
static const ULONGLONG TILT_COOLDOWN_MS = 700;

// NuiCameraElevationGetAngle/SetAngle rodavam originalmente dentro do loop
// principal de captura (junto com WaitForMultipleObjects de cor/
// profundidade) — descoberto ao vivo que NuiCameraElevationGetAngle pode
// travar indefinidamente nesta mesma máquina/hardware (sem retornar erro,
// só nunca volta), e como a chamada inicial acontecia ANTES do loop de
// frames começar, isso travava a bridge inteira: câmera nunca chegava a
// publicar um frame sequer. Isolado numa thread própria — se o motor
// travar, só a inclinação para de responder; cor e profundidade nunca são
// afetadas.
static DWORD WINAPI TiltThreadProc(LPVOID param) {
    TiltControl* control = (TiltControl*)param;
    ULONGLONG lastTiltTick = 0;
    ULONGLONG lastAngleQueryTick = 0;
    ULONGLONG lastSetAngleErrLog = 0;
    ULONGLONG lastGetAngleErrLog = 0;
    const ULONGLONG ERR_LOG_INTERVAL_MS = 3000;

    while (true) {
        ULONGLONG now = GetTickCount64();
        if (control->command_seq != control->last_applied_seq &&
            (now - lastTiltTick) >= TILT_COOLDOWN_MS) {
            LONG target = control->requested_angle;
            if (target > NUI_CAMERA_ELEVATION_MAXIMUM) target = NUI_CAMERA_ELEVATION_MAXIMUM;
            if (target < NUI_CAMERA_ELEVATION_MINIMUM) target = NUI_CAMERA_ELEVATION_MINIMUM;
            HRESULT tiltHr = NuiCameraElevationSetAngle(target);
            if (SUCCEEDED(tiltHr)) {
                control->last_applied_seq = control->command_seq;
            } else if (now - lastSetAngleErrLog >= ERR_LOG_INTERVAL_MS) {
                fprintf(stderr, "[bridge] NuiCameraElevationSetAngle(%ld) falhou: 0x%08lx\n", target, tiltHr);
                fflush(stderr);
                lastSetAngleErrLog = now;
            }
            lastTiltTick = now;
        }
        if (now - lastAngleQueryTick >= 500) {
            LONG cur;
            HRESULT getHr = NuiCameraElevationGetAngle(&cur);
            if (SUCCEEDED(getHr)) {
                control->current_angle = cur;
            } else if (now - lastGetAngleErrLog >= ERR_LOG_INTERVAL_MS) {
                fprintf(stderr, "[bridge] NuiCameraElevationGetAngle falhou: 0x%08lx\n", getHr);
                fflush(stderr);
                lastGetAngleErrLog = now;
            }
            lastAngleQueryTick = now;
        }
        Sleep(100);
    }
    return 0;
}

static unsigned char* CreateSharedBuffer(const char* name, size_t totalBytes, HANDLE* outMapFile) {
    HANDLE hMapFile = CreateFileMappingA(
        INVALID_HANDLE_VALUE, NULL, PAGE_READWRITE, 0, (DWORD)totalBytes, name
    );
    if (hMapFile == NULL) {
        fprintf(stderr, "[bridge] CreateFileMapping(%s) falhou: %lu\n", name, GetLastError());
        return NULL;
    }
    unsigned char* pBuf = (unsigned char*)MapViewOfFile(hMapFile, FILE_MAP_ALL_ACCESS, 0, 0, totalBytes);
    if (pBuf == NULL) {
        fprintf(stderr, "[bridge] MapViewOfFile(%s) falhou: %lu\n", name, GetLastError());
        CloseHandle(hMapFile);
        return NULL;
    }
    *outMapFile = hMapFile;
    return pBuf;
}

int main() {
    HRESULT hr = NuiInitialize(NUI_INITIALIZE_FLAG_USES_COLOR | NUI_INITIALIZE_FLAG_USES_DEPTH);
    if (FAILED(hr)) {
        fprintf(stderr, "[bridge] NuiInitialize falhou: 0x%08lx\n", hr);
        return 1;
    }
    fprintf(stderr, "[bridge] Kinect inicializado (cor + profundidade).\n");

    HANDLE hNextColorFrameEvent = CreateEventW(NULL, TRUE, FALSE, NULL);
    HANDLE hColorStreamHandle = NULL;
    hr = NuiImageStreamOpen(
        NUI_IMAGE_TYPE_COLOR, NUI_IMAGE_RESOLUTION_640x480,
        0, 2, hNextColorFrameEvent, &hColorStreamHandle
    );
    if (FAILED(hr)) {
        fprintf(stderr, "[bridge] NuiImageStreamOpen(COLOR) falhou: 0x%08lx\n", hr);
        NuiShutdown();
        return 1;
    }

    HANDLE hNextDepthFrameEvent = CreateEventW(NULL, TRUE, FALSE, NULL);
    HANDLE hDepthStreamHandle = NULL;
    hr = NuiImageStreamOpen(
        NUI_IMAGE_TYPE_DEPTH, NUI_IMAGE_RESOLUTION_640x480,
        0, 2, hNextDepthFrameEvent, &hDepthStreamHandle
    );
    if (FAILED(hr)) {
        fprintf(stderr, "[bridge] NuiImageStreamOpen(DEPTH) falhou: 0x%08lx\n", hr);
        NuiShutdown();
        return 1;
    }
    fprintf(stderr, "[bridge] Streams de cor e profundidade abertos (%dx%d).\n", WIDTH, HEIGHT);

    HANDLE hColorMapFile = NULL, hDepthMapFile = NULL, hControlMapFile = NULL;
    unsigned char* pColorBuf = CreateSharedBuffer(COLOR_SHM_NAME, COLOR_TOTAL_BYTES, &hColorMapFile);
    unsigned char* pDepthBuf = CreateSharedBuffer(DEPTH_SHM_NAME, DEPTH_TOTAL_BYTES, &hDepthMapFile);
    unsigned char* pControlBufRaw = CreateSharedBuffer(CONTROL_SHM_NAME, CONTROL_TOTAL_BYTES, &hControlMapFile);
    if (!pColorBuf || !pDepthBuf || !pControlBufRaw) {
        NuiShutdown();
        return 1;
    }
    TiltControl* control = (TiltControl*)pControlBufRaw;

    *(unsigned __int64*)(pColorBuf)      = 0;
    *(unsigned __int32*)(pColorBuf + 8)  = WIDTH;
    *(unsigned __int32*)(pColorBuf + 12) = HEIGHT;

    *(unsigned __int64*)(pDepthBuf)      = 0;
    *(unsigned __int32*)(pDepthBuf + 8)  = WIDTH;
    *(unsigned __int32*)(pDepthBuf + 12) = HEIGHT;

    // Ângulo inicial fica 0/0 (centro) até a thread de inclinação conseguir
    // consultar o motor de verdade — NÃO espera essa consulta aqui, pra não
    // arriscar travar a publicação de cor/profundidade se o motor não
    // responder (ver comentário em TiltThreadProc).
    control->command_seq = 0;
    control->last_applied_seq = 0;
    control->current_angle = 0;
    control->requested_angle = 0;

    fprintf(stderr, "[bridge] Memoria compartilhada pronta ('%s', '%s', '%s'). Streaming...\n",
            COLOR_SHM_NAME, DEPTH_SHM_NAME, CONTROL_SHM_NAME);
    fflush(stderr);

    CreateThread(NULL, 0, TiltThreadProc, control, 0, NULL);

    unsigned __int64 colorFrameCounter = 0;
    unsigned __int64 depthFrameCounter = 0;
    int colorRowBytes = WIDTH * COLOR_PIXEL_SIZE;

    HANDLE waitHandles[2] = { hNextColorFrameEvent, hNextDepthFrameEvent };

    while (true) {
        DWORD wait = WaitForMultipleObjects(2, waitHandles, FALSE, 2000);

        if (wait == WAIT_OBJECT_0) {
            // -- Frame de cor pronto --
            const NUI_IMAGE_FRAME* pImageFrame = NULL;
            hr = NuiImageStreamGetNextFrame(hColorStreamHandle, 0, &pImageFrame);
            if (SUCCEEDED(hr) && pImageFrame != NULL) {
                INuiFrameTexture* pTexture = pImageFrame->pFrameTexture;
                NUI_LOCKED_RECT lockedRect;
                pTexture->LockRect(0, &lockedRect, NULL, 0);
                if (lockedRect.Pitch != 0) {
                    unsigned char* dst = pColorBuf + HEADER_BYTES;
                    unsigned char* src = (unsigned char*)lockedRect.pBits;
                    for (int y = 0; y < HEIGHT; y++) {
                        memcpy(dst + (size_t)y * colorRowBytes, src + (size_t)y * lockedRect.Pitch, colorRowBytes);
                    }
                    colorFrameCounter++;
                    *(unsigned __int64*)(pColorBuf) = colorFrameCounter;
                }
                pTexture->UnlockRect(0);
                NuiImageStreamReleaseFrame(hColorStreamHandle, pImageFrame);
            }
            ResetEvent(hNextColorFrameEvent);

        } else if (wait == WAIT_OBJECT_0 + 1) {
            // -- Frame de profundidade pronto --
            const NUI_IMAGE_FRAME* pDepthFrame = NULL;
            hr = NuiImageStreamGetNextFrame(hDepthStreamHandle, 0, &pDepthFrame);
            if (SUCCEEDED(hr) && pDepthFrame != NULL) {
                INuiFrameTexture* pTexture = pDepthFrame->pFrameTexture;
                NUI_LOCKED_RECT lockedRect;
                pTexture->LockRect(0, &lockedRect, NULL, 0);
                if (lockedRect.Pitch != 0) {
                    unsigned char* dst = pDepthBuf + HEADER_BYTES;
                    unsigned char* src = (unsigned char*)lockedRect.pBits;
                    for (int y = 0; y < HEIGHT; y++) {
                        const USHORT* srcRow = (const USHORT*)(src + (size_t)y * lockedRect.Pitch);
                        USHORT* dstRow = (USHORT*)(dst + (size_t)y * WIDTH * sizeof(USHORT));
                        for (int x = 0; x < WIDTH; x++) {
                            // Os 3 bits baixos guardam o indice de player (não
                            // usado aqui, sem tracking de esqueleto ligado) —
                            // NuiDepthPixelToDepth desempacota o valor em mm.
                            dstRow[x] = NuiDepthPixelToDepth(srcRow[x]);
                        }
                    }
                    depthFrameCounter++;
                    *(unsigned __int64*)(pDepthBuf) = depthFrameCounter;
                }
                pTexture->UnlockRect(0);
                NuiImageStreamReleaseFrame(hDepthStreamHandle, pDepthFrame);
            }
            ResetEvent(hNextDepthFrameEvent);
        }
        // timeout (WAIT_TIMEOUT): só volta a esperar de novo.
    }

    UnmapViewOfFile(pColorBuf);
    CloseHandle(hColorMapFile);
    UnmapViewOfFile(pDepthBuf);
    CloseHandle(hDepthMapFile);
    UnmapViewOfFile(pControlBufRaw);
    CloseHandle(hControlMapFile);
    NuiShutdown();
    return 0;
}
