# Validación de Offset 0.3 alpha

> Registro histórico de la entrega base. No certifica la candidata Windows `local.windowsqa.3`. Consulte `../QA-WINDOWS.md` y los resultados de la colaboración para esa versión.

Fecha: 19 de octubre de 2026. Entorno de ejecución: Linux administrado, Python 3.12 y FFmpeg 6.1.1 con libx264. Las cifras describen este entorno, no un PC Windows del usuario.

## Resultado del hito

Hay un relay local de **retraso fijo real**, con panel y pruebas repetibles. Audio y vídeo atraviesan dos conexiones RTMP. No se considera completado el motor de retraso dinámico descrito en la especificación v1.1.

## Pruebas ejecutadas

| Prueba | Resultado |
|---|---|
| 16 pruebas unitarias y de API | Pasaron |
| FLV: tamaño máximo, truncamiento y timestamps | Rechazo explícito |
| B-frames normales | Rechazo por PTS/DTS incompatibles |
| B-frames con CTS manipulado a cero | Rechazo por slice H.264 tipo B |
| Byte cap, duración y número máximo de paquetes | Límites respetados sin descartar contenido antiguo silenciosamente |
| Reloj después de bloqueo y pequeñas demoras del scheduler | Reanclaje y control de deriva de planificación |
| API local: token, origen y CSP | Comprobaciones pasaron |
| Fuente FLV real con retraso mínimo de 0,4 s | 309 payloads idénticos, timestamps relativos conservados, decodificación sin errores |
| Fuente RTMP → Offset → receptor RTMP FFmpeg | 309 payloads idénticos, desplazamiento temporal común de 0 s, decodificación sin errores |
| Retraso mínimo configurado de 1 s en RTMP | Mínimo observado ≈ 1,001 s en entrega al transporte |
| Buffer deliberadamente insuficiente | FAULT antes de transmitir; límite respetado |
| Destino ausente | FAULT sin cuelgue |
| Destino interrumpido durante transmisión | FAULT sin cuelgue, sin reconexión ni salto |
| Demostración completa con panel/API | 1539 paquetes recibidos y entregados; estado FINISHED; grabación de 398 850 bytes sin errores de decodificación |

Los detalles numéricos están en `integration-results.json` y `demo-results.json`. La igualdad de hashes verifica copia del contenido comprimido. La igualdad del desplazamiento de timestamps entre pistas verifica conservación temporal respecto a la fuente. **No sustituye una medición audiovisual perceptual después de empalmes**, que aún no existen en este hito.

## Medición de 120 segundos de retraso

Comando ejecutado:

```sh
python -m tests.benchmark --delay 120 --seconds 150
```

Fuente: clip 1920×1080 a 60 fps, H.264 sin B-frames, repetido mediante stream copy. Encoder configurado con objetivo CBR de 6 Mbps y AAC de 160 kbps. **El bitrate efectivo de esa fuente no se midió**; no se interpreta el objetivo del encoder como una medición exacta ni se extrapola el consumo a todo stream de 6 Mbps.

| Métrica | Resultado observado |
|---|---:|
| Tiempo medido | 150,06 s |
| Retraso configurado | 120 s |
| Pico de datos en cola | 106 082 497 bytes ≈ 101,17 MiB |
| Menor residencia observada en muestras de envío | 120,700 s |
| Última residencia observada | 120,771 s |
| Muestras de 1 s con salida activa | 30 |
| Paquetes entregados al finalizar la medición | 3154 |
| CPU media del proceso Python | ≈ 1,30 % de un núcleo |
| Pico RSS de Python | 125 960 192 bytes ≈ 120,13 MiB |
| RSS y CPU conjunta de Python + FFmpeg | No disponibles de forma fiable en este entorno |

El entorno expone identificadores de proceso inconsistentes entre `getpid` y `/proc`. El monitor detecta esa situación y devuelve `null`, en lugar de sumar memoria de procesos que no corresponden. El pico de Python se obtiene mediante `getrusage`; la CPU indicada excluye los procesos FFmpeg y el generador de la señal.

El archivo `benchmark-120s.json` conserva las 150 muestras. El benchmark corta la sesión explícitamente al terminar el tiempo configurado, por lo que no evalúa el vaciado completo de un buffer de 120 segundos. La prueba corta de integración sí comprueba entrega completa.

**No se ha demostrado un consumo total inferior a 120 MB ni estabilidad durante 60 minutos.** El resultado confirma que se almacena contenido comprimido con consumo acotado en esta prueba corta; el presupuesto total requiere medición en el equipo de destino.

## Verificaciones pendientes

- Uso manual con OBS y su encoder concreto, incluida la disponibilidad de B-frames = 0.
- Windows y macOS; scripts Windows incluidos, todavía sin ejecución verificada en esos sistemas.
- Sesión de resistencia de 60 minutos y evaluación de deriva/estabilidad prolongada.
- MediaMTX como receptor. Se usó FFmpeg para las pruebas RTMP actuales.
- Red lenta sostenida y fallos de sistema distintos de los casos automatizados.
- Pantalla segura, purga, máscaras, ajuste dinámico y reconexión automática.
- Plataformas públicas, RTMPS y comportamiento de reproductores reales.
- Revisión visual e interacción manual del panel en navegador. Se verificaron su sintaxis JavaScript, rutas HTTP y API; no se realizó QA visual de navegador.

## Repetición recomendada en el equipo del usuario

1. Ejecutar `python demo.py --open` y comprobar estado y grabación.
2. Ejecutar las pruebas unitarias y de integración.
3. Probar OBS con retraso de 10 s contra un receptor local.
4. Ejecutar el benchmark y observar el Administrador de tareas para Python y ambos FFmpeg en Windows.
5. Guardar el diagnóstico del panel. Solo después avanzar a sesiones largas y al siguiente hito del motor.

No hay aprobación automática por omitir pruebas: cada comprobación pendiente debe pasar antes de acreditar la función a la que corresponde.
