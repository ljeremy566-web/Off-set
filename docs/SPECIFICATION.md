# Offset · Especificación corregida v1.1

Fecha: 19 de octubre de 2026. Base: la especificación del relay ligero y su revisión técnica. Esta versión separa **contrato del producto previsto** de **alcance implementado en 0.3 alpha**. No acredita funciones pendientes ni pruebas no realizadas.

## 1. Decisión de implementación

El primer hito es una transmisión local real con retraso fijo y copia de paquetes. Se construye antes de introducir cambios de origen, pantallas seguras y purgas. Esto permite comprobar transporte, memoria y conservación audiovisual con una referencia sencilla.

El motor usa Python estándar y dos procesos FFmpeg con `-c copy`: uno termina la conexión RTMP de entrada y produce FLV; el otro recibe FLV y publica al destino RTMP. El motor no decodifica ni recodifica imágenes o audio. Los transportes sí tienen buffers y coste de proceso; se deben medir por separado del payload acumulado.

La interfaz local se sirve en `127.0.0.1:8765`, sin frameworks ni servicios externos. La arquitectura hospedada del simulador anterior no recibe medios ni controla el equipo del usuario.

## 2. Matriz de alcance

| Función | Alpha 0.3 | Hito posterior |
|---|---|---|
| RTMP local real, H.264 + AAC | Implementado | Ampliar compatibilidad |
| Retraso fijo 0–120 s | Implementado | — |
| Cambios de retraso en vivo | Deshabilitado | Tras validar slate y empalmes |
| Buffer comprimido y límites | Implementado | Índices de acceso a IDR para saltos |
| Validación B-frames y timestamps | Implementado | Ampliar formatos solo con pruebas |
| Pantalla segura / slate | Pendiente | Captura segura y bucle compatible |
| Máscaras OBS | Pendiente | obs-websocket y confirmación en el flujo |
| Purga y sustitución | Pendiente | Intervalos A/V y cola pendiente |
| Reconexión automática | Pendiente | Épocas, anclaje del reloj y estado seguro |
| Drenaje final de fuente | Implementado si termina sin error | — |
| Drenaje competitivo y salto a directo | Pendiente | Marcador y contenido verificable |
| RTMPS y plataformas reales | Fuera del hito local | Pruebas específicas |

## 3. Contrato de entrada actual

- Entrada RTMP local única, normalizada a FLV clásico por FFmpeg. El validador inspecciona ese FLV normalizado; no es un parser del RTMP original.
- Una pista H.264 y una AAC-LC, 44,1 o 48 kHz, mono/estéreo; AAC de 1024 muestras sin configuración central extendida.
- Configuración fija por sesión; cabeceras iguales byte a byte. Se rechazan nuevos SPS/PPS en el contenido que no estén declarados en la cabecera inicial.
- Cada access unit de vídeo admitida contiene slices I/P; el primer vídeo debe incluir un IDR. B, SP, SI y extensiones no contempladas se rechazan.
- **PTS = DTS es una comprobación independiente de la ausencia de B-frames.** Se inspecciona `slice_type` en las NAL de vídeo, sin decodificar píxeles.
- DTS estrictamente creciente por pista; FLV intercalado sin retroceso global. La cadencia fija debe configurarse en el encoder. El motor no reconstruye una entrada desordenada o mal configurada.
- Máximo 2 MiB por tag antes de reservar su payload. Un flujo fuera de contrato detiene la sesión.
- El parser no pretende sustituir una validación completa de decodificación H.264/AAC. Las pruebas de referencia decodifican la salida en el receptor.

## 4. Cola y reloj actuales

Cada paquete conserva tipo, timestamp en milisegundos, payload y llegada medida con reloj monotónico. La cola `deque` comparte datos entre ingreso y egreso con bloqueo breve. No hay copia de vídeo descomprimido.

El egreso no retira el paquete de la cola hasta que termina su escritura al pipe del FFmpeg emisor. Esto **no significa que el socket remoto ni el espectador ya lo hayan recibido**. El panel usa «entregado al transporte».

La condición mínima de envío es:

```text
ahora_monotónico >= llegada_paquete + retraso_configurado
```

También se respeta la distancia temporal entre los paquetes respecto al anclaje de envío. Un bloqueo que supere 10 ms reancla la planificación, sin alterar timestamps ni intentar recuperar todo el tiempo en una ráfaga. La tolerancia de planificación no reduce el mínimo de residencia.

Se preservan el payload y los timestamps relativos entre audio y vídeo. Los transportes pueden aplicar un desplazamiento común de origen; las pruebas comprueban que sea igual para ambas pistas.

La cola se acota por bytes, 30 000 paquetes y una duración/residencia máxima de D + 30 s. Si se alcanza un límite se pasa a `FAULT`, se interrumpen los transportes y no se adelanta el puntero para descartar contenido en silencio.

**El límite de cola no es el límite de RSS del proceso ni del conjunto Python + FFmpeg.** Se deben incluir metadatos, referencias, paquetes transitorios, pipes y buffers de los transportes en cualquier evaluación de memoria total.

## 5. Estados implementados

| Estado | Comportamiento |
|---|---|
| READY | Panel listo, motor sin sesión |
| WAITING_INPUT | Listener iniciado; espera la fuente |
| BUFFERING | Valida y acumula; salida sin conectar |
| RELAYING | Entrega paquetes al transporte con retraso mínimo |
| DRAINING | La fuente terminó sin error; envía lo pendiente a 1× |
| FINISHED | Cola vacía y transporte finalizado sin error |
| STOPPED | Interrupción explícita; no envía lo pendiente |
| FAULT | Error de formato, capacidad o transporte; salida interrumpida |

Sin slate preparado no se promete una pantalla segura. Al fallar se corta la salida. Se necesita una nueva sesión para reconectar, con carga inicial completa.

## 6. Contratos corregidos para los próximos hitos

Estos requisitos son obligatorios antes de habilitar las funciones correspondientes, pero **todavía no están implementados**.

### 6.1 Empalmes y audio

Se conserva un desplazamiento temporal común para audio y vídeo. El vídeo de destino empieza en IDR. Las duraciones se manejan con fracciones exactas, y solo se redondean al empaquetar timestamps.

No se acepta `round(hueco_a / dur_a)` como regla general para silencio: puede generar solapamiento. Cada trama insertada debe terminar antes o en el inicio de A0. El algoritmo final debe definir qué hace con el residuo y validar ausencia de solapamiento, sincronía y deriva bajo muchos empalmes.

Ejemplo de regresión obligatorio: AAC de 21,333 ms y hueco de 14 ms; insertar una trama completa produciría un solapamiento de 7,333 ms. Este caso no puede pasar inadvertido por comprobar solo monotonía de DTS.

### 6.2 Preparación de pantalla segura

El slate debe tener una configuración compatible con la sesión de salida. La captura desde el encoder activo requiere demostrar que el GOP capturado contiene exclusivamente la imagen segura, sin transiciones ni vídeo anterior en colas.

Para probar sin OBS debe existir una fixture de slate asociada al encoder de prueba, con cabeceras comparadas y contenido verificado. No se obliga a conectar obs-websocket para pruebas que se describen como exclusivamente FFmpeg.

Si la entrada falla antes de disponer de un slate válido, la salida permanece desconectada. Nunca se informa «pantalla segura activa» si no hay un recurso seguro preparado.

### 6.3 Protección y purgas

Se separan dos acciones: **mostrar pantalla segura inmediatamente** y **purgar un intervalo del contenido pendiente**. Un incidente puede afectar al write head o a lo que está saliendo por el read head; no se asume siempre el primero.

Una confirmación obs-websocket demuestra una operación de control, no identifica por sí sola el paquete codificado donde ya aparece la máscara. Para cerrar una purga automática se necesita identificar el cambio en el flujo o una cota de demora previamente medida y validada. «ACK + un fotograma» no es una garantía suficiente.

La purga cubre vídeo hasta un IDR de retorno y todos los paquetes de audio que se solapen. También se revisan las colas pendientes de egreso: lo ya entregado al transporte puede no ser recuperable. La interfaz informa ese límite, sin afirmar que conoce qué contenido vio cada espectador.

El comportamiento se define en todos los estados, incluidos preparación, carga, drenaje y fallos. Los márgenes para reacción y para terminar bloque competitivo se configuran por separado.

### 6.4 Reconexión

Una nueva época define de forma inequívoca el final del último intervalo y el comienzo del siguiente, incluyendo duraciones. La hora de llegada es una referencia local afectada por colas y jitter; no equivale a la hora original de captura.

Reconectar requiere reanclar el reloj de envío además de resolver los timestamps del nuevo RTMP. Conservar timestamps y mantener 1× son condiciones independientes. Se prohíbe reducir automáticamente el colchón mediante ráfagas.

Si se descarta contenido por límites de capacidad en un hito posterior, deben invalidarse los punteros y marcadores que lo referencien. El drenaje no puede seguir prometiendo entrega completa de contenido que ya se eliminó.

### 6.5 Métricas

- «Residencia del último paquete entregado»: envío al transporte menos llegada al relay. No es latencia al espectador.
- «Contenido pendiente»: extensión temporal en cola; al introducir huecos, se descuentan todos los intervalos sin contenido, no solo las purgas.
- «Retraso objetivo»: configuración solicitada, separada de lo observado.
- Antes del primer envío, residencia desconocida: `null`, no cero ficticio.
- Durante slate, se informa pantalla segura y su estado, sin presentar una antigüedad de gameplay inexistente.
- RSS del conjunto solo si se puede medir con procesos correctamente identificados; de lo contrario no disponible. Payload, RSS propio y RSS conjunto no son intercambiables.

## 7. Criterios de aceptación

### Hito A1: retraso fijo

1. RTMP de entrada y salida local; destino real independiente.
2. Payloads de audio/vídeo idénticos entre fuente y salida y desplazamiento temporal común.
3. Decodificación de salida sin errores.
4. Ningún paquete entregado antes de cumplir el retraso mínimo local.
5. Rechazo de B-frames normales y de B-frames con CTS manipulado a cero.
6. Límites de cola respetados y fallo explícito, sin salto silencioso.
7. Destino ausente produce fallo acotado, sin cuelgue.
8. Panel y API no permiten cambiar la sesión en marcha ni control desde un origen externo.
9. Medición con 120 s de retraso; resultados y limitaciones documentados.

El informe `VALIDATION.md` indica qué se ejecutó. La compatibilidad OBS manual, Windows/macOS, MediaMTX, sesiones de 60 minutos y plataformas reales siguen siendo puertas de validación independientes.

### Hito A2: slate y empalmes

Se añaden pruebas de silencio sin solapamiento, bucles de slate, deriva, sincronía tras cada empalme y cabeceras. No se habilitan los controles antes de pasar esas pruebas.

### Hito A3: cambios dinámicos, purgas y reconexión

Se cubren todos los estados, borrado de intervalos, contenido ya enviado, cola saturada, épocas y reinicio de destino. Los 13 casos de la especificación anterior se revisan para reflejar los contratos corregidos y deben pasar. **No existe una excepción que permita dar por terminado el prototipo si falla MediaMTX.** Una prueba bloqueada queda pendiente y el hito correspondiente no se considera validado.

### Hito B: plataformas

Se prueba cada destino real con sus requisitos de transporte y cadencia. No se extrapola el éxito local a Twitch o YouTube, ni se habilita recuperación gradual basada en timestamps sin experimentos independientes.

## 8. Decisiones abiertas

- Captura y validación del GOP seguro con OBS, incluidos buffers previos al relay.
- Algoritmo definitivo de empalme AAC y tolerancias medidas.
- Semántica completa de los dos controles de protección.
- Reanudar o iniciar una nueva sesión tras errores según el perfil competitivo.
- Primera plataforma de pruebas y distribución empaquetada para Windows.
- Sustitución futura de procesos FFmpeg por una biblioteca de transporte si las mediciones justifican el coste de mantenimiento.

El lenguaje ya está decidido para A1: Python estándar. Los siguientes hitos conservarán el plano de control y el contrato de paquetes; la implementación del transporte puede cambiar sin convertir el panel en un procesador multimedia.
