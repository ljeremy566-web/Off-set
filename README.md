# Offset · Relay RTMP local con delay dinámico (0.5 · Hito A3)

Motor local anti-stream-sniping diseñado para correr junto a OBS en tu propia PC: **activa y desactiva el delay en vivo sin cortar tu transmisión, sin recodificar (`-c copy`) y sin configuraciones complejas**. No requiere `pip`, `npm`, Electron ni servicios en la nube.

## Qué incluye hoy

- **Delay dinámico en vivo (`0 s ↔ 120 s`):**
  - **Activar delay (`0 → D` o subir `30 → 90`):** espera el próximo keyframe IDR (≤2 s), muestra tu **imagen de espera personalizada (PNG/JPG/WebP)** en bucle compatible con tu resolución/FPS/audio mientras se llena el buffer, y reanuda en el IDR exacto con **0 frames perdidos de OBS**.
  - **Desactivar delay (`D → 0` o bajar `60 → 30`):** elige en vivo entre **Directo ya** (salto limpio al último IDR en ≤2 s) o **Sin perder nada** (*catch-up* gradual a `1.15× / 1.25× / 1.40×` con 0 frames descartados).
- **Protección inmediata (Hito A3):**
  - **Pantalla Segura YA (Botón de Pánico):** corta a tu imagen de espera en ≤2 s y la mantiene fija hasta que decidas reanudar en directo o desde el buffer.
  - **Purga anti-leak (`-15 s / -30 s / Todo`):** elimina los segundos más recientes del buffer antes de que salgan al directo. *(Lo ya entregado al transporte no puede recuperarse.)*
  - **Reconexión automática:** si OBS se cierra o reinicia, Offset sostiene el RTMP saliente con tu imagen segura hasta que OBS reconecta (nueva época) y reanuda sin tumbar tu directo; también reintenta microcortes del destino RTMP.
- **Cero mega-configuración:**
  - Compatible con el encoder normal de OBS (**NVENC, x264, AMF; B-frames soportados** por defecto).
  - Presets de destino para **Twitch, YouTube Live, Kick, Local y Custom** (`rtmp://` / `rtmps://`), con enmascarado de claves en UI/logs y guardado opcional cifrado con **Windows DPAPI** (`data/settings.json`).
  - **Vista Dock para OBS (`http://127.0.0.1:8765/?dock=1`)** y **atajos globales de Windows** sin Alt+Tab (`Ctrl+Shift+F9/F10/F11/F12`).
  - Medición nativa de RAM y CPU en Windows y Linux.

## Requisitos

- **Python 3.11 o posterior** (solo biblioteca estándar).
- **FFmpeg** y **FFprobe** accesibles en `PATH` (`libx264`, `aac`, `avsynctest`).

## Inicio rápido

### 1. Probar sin OBS (demo dinámica automática)
```powershell
python demo.py --open
```
Genera 30 s de señal real H.264 (`bf=2`) + AAC, arranca en directo (`0 s`), activa 4 s de delay con slate, vuelve a directo sin perder frames (`1.35×`) y guarda la grabación continua en `offset-demo-FECHA.flv`.

### 2. Usar con OBS
```powershell
python run.py --open
```
O abre `start-windows.bat`:
1. En el panel (`http://127.0.0.1:8765`), sube tu imagen de espera (opcional), elige destino (**Twitch / YouTube / Kick / Local**) y pulsa **Preparar transmisión**.
2. En **OBS → Ajustes → Emisión → Personalizado**:
   - **Servidor:** `rtmp://127.0.0.1:1935/live`
   - **Clave:** `input`
3. *(Opcional, recomendado)* En **OBS → Paneles → Paneles de navegador personalizados**, añade `http://127.0.0.1:8765/?dock=1` para controlar el delay, la pantalla segura y la purga directamente dentro de OBS.

## Pruebas automatizadas

```powershell
python -X dev -W error::ResourceWarning -m unittest discover -s tests -v
python -m tests.test_dynamic_integration
python -m tests.test_a3_rtmp_integration
python -m tests.integration
```

## Estructura

| Archivo | Responsabilidad |
|---|---|
| `offset/media.py` | Parser FLV acotado, validación H.264/AAC (compatible con B-frames o modo estricto) y detección IDR |
| `offset/buffer.py` | Cola comprimida acotada, salto IDR parcial/total, purga anti-leak y `Pacer` con catch-up |
| `offset/slate.py` | Almacenamiento de imagen personalizada, sondeo de formato OBS, compilador FLV y `SlateCursor` exacto (`Fraction`) |
| `offset/relay.py` | Motor RTMP dual FFmpeg, máquina de delay dinámico, pánico, purga, reconexión por épocas y métricas Win32/Linux |
| `offset/destinations.py` | Presets Twitch/YouTube/Kick/Local/Custom y enmascarado seguro de Stream Keys |
| `offset/hotkeys.py` | Atajos globales nativos de Windows (`RegisterHotKey` vía `ctypes`) |
| `offset/settings.py` | Persistencia local en `data/settings.json` + cifrado Windows DPAPI para claves |
| `offset/server.py` | API HTTP en loopback (`127.0.0.1`), token de sesión, CSP y rutas de control/slate |
| `web/` | Panel de control oscuro + modo Dock compacto (`/?dock=1`) sin frameworks |

## Próximo hito (Hito B · Producción y distribución)

- Pruebas en vivo con cuentas reales en Twitch, YouTube Live, Kick (RTMPS) y MediaMTX.
- Prueba de resistencia prolongada (60 min) con transiciones dinámicas múltiples.
- Integración opcional con `obs-websocket` para máscaras/escenas automáticas.
- Empaquetado en ejecutable `.exe` / instalador standalone para Windows (sin pedir instalar Python/FFmpeg aparte).
