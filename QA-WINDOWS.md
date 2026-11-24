# Corrección experimental para Windows

Versión `0.3.0-alpha.1+local.windowsqa.3`. No corresponde a la revisión oficial `+qa.1`.

## Problemas y cambios

La alpha.1 acumulaba retraso bajo Python 3.12/Windows: `monotonic()` tenía resolución de 15,625 ms y la espera por paquete con `Event.wait()` provocaba reanclajes repetidos. También retenía payloads al detener o fallar. La candidata v2 corrigió el comportamiento medido con OBS, pero una revisión posterior reprodujo fallos de cierre no cubiertos por esas pruebas.

La v3 conserva `perf_counter()` en un único dominio temporal y la espera de alta resolución de Python 3.11+ en Windows. La usa tanto en el egreso como en las fixtures de entrada. No introduce busy-wait ni cambia la política de reanclaje tras bloqueos reales.

El cierre ahora protege la creación y publicación de procesos frente a cancelaciones concurrentes. Las excepciones al cerrar streams no impiden señalar la terminación del escritor ni liberar su cola. STOPPED y FAULT no pueden volver a estados activos.

`done` indica que el escritor ya terminó de usar la cola (o no llegó a iniciarse); no confirma por sí solo que todos los procesos hayan finalizado. `stop()` comprueba también los procesos e hilos, devuelve `False` si el cierre sigue pendiente y mantiene un FAULT explícito. El controlador no permite sustituir esa sesión hasta que el cierre se complete. No se vacía la cola desde otro hilo mientras el escritor puede estar utilizándola. Los bytes ya entregados al pipe de FFmpeg no son recuperables.

El cierre normal de la fuente conserva los paquetes para drenarlos. El cierre por error o detención los descarta después de que el escritor deje de usarlos. Los fallos persistentes del sistema operativo pueden exigir intervención; no se afirma un límite de tiempo absoluto para operaciones del propio SO.

## Evidencia de esta candidata

Ejecutado en Windows con Python 3.12.10 y FFmpeg 9.0.2:

- 38 tests unitarios, incluidos fallos inyectados de escritura/cierre, un escritor bloqueado, carrera spawn/stop, watchdog tardío tras stop y rechazo HTTP 409 de nueva sesión con cierre pendiente.
- 150 casos de concurrencia: 50 repeticiones de spawn/stop, escritor aún activo y lectura de estado durante stop.
- Integración con codecs reales y RTMP local: 309 paquetes idénticos, desplazamiento A/V común de 0 s y decodificación sin errores.
- Rechazo de B-frames del encoder real, incluidos CTS manipulados a cero; límite de cola, destino ausente y desconexión de destino con fallo explícito.

Los resultados detallados de esta colaboración se entregan aparte. Las pruebas OBS de 164 y 235 segundos corresponden a la candidata v2; **no son una ejecución OBS de v3**. La validación histórica de Linux en `docs/VALIDATION.md` y los JSON originales de `docs/` pertenecen a la entrega base.

Un resultado tardío del watchdog no cambia una parada voluntaria ya solicitada a FAULT. Un cierre que no termina a tiempo sigue produciendo FAULT y HTTP 409 de forma explícita. Si la entrada ya llegó completa y su cierre falla, se registra el aviso y se conserva el drenaje. Un fallo al cerrar la auditoría durante una finalización normal sí impide declarar FINISHED.

Pendientes: nueva ejecución OBS con v3, 120 s de retraso, 60 minutos, macOS, MediaMTX y plataformas públicas. No se añaden delay dinámico, slate, purgas ni reconexión automática.
