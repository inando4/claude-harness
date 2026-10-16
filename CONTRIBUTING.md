# Contribuir a Claude Harness

Trabaja sobre un fork y envía un pull request con el problema, el cambio y su validación. Para errores, incluye pasos reproducibles, versión de Python/Claude Code, stack y un extracto del fallo sin secretos. Usa repositorios y datos sintéticos; no adjuntes conversaciones, estados completos de trabajos ni backups de bases.

Lee [AGENTS.md](AGENTS.md) y la [guía](docs/harness-claude-spec.md) antes de modificar el controlador. Los [pendientes](docs/harness-pendientes.md) distinguen limitaciones conocidas de capacidades implementadas.

## Pruebas del controlador

En Linux, desde la raíz del repositorio, con Python 3.10+ y Git:

```bash
python3 -B -m unittest discover -s harness/tests -p 'test_*.py'
```

La suite usa la biblioteca estándar de Python, repositorios Git temporales y procesos locales. No requiere llamadas a modelos, credenciales de GitHub, una base de producción ni Docker. Necesita poder abrir sockets loopback y ejecutar subprocesos; un sandbox que los prohíba puede bloquear casos de infraestructura. CI ejecuta la suite en Linux con Python 3.10, 3.12 y 3.14.

## Prueba de portabilidad

Con Node.js y Graphify disponibles:

```bash
python3 -B harness/tests/smoke_portability.py --run
```

Esta prueba instala el paquete y ejecuta el controlador en proyectos temporales Python, Node y backend/frontend separados. Arranca servicios locales y los limpia al terminar. No usa modelos ni bases de datos. Sus fixtures sintéticos de consentimiento prueban el protocolo; no autorizan ninguna spec de un usuario.

Las pruebas opcionales de Docker y navegador están separadas de la suite del controlador. Consulta `--help` en `harness/tests/smoke_runtime.py`, `smoke_browser_isolation.py` y `smoke_local.py` antes de ejecutarlas. `smoke_local.py` corresponde al adaptador específico Spring Boot/Maven y requiere una instalación compatible, no esta carpeta fuente.

## Comprobar el instalador

Instala la copia modificada en un repositorio Git desechable con `python3 harness/install.py /ruta/al/repositorio-temporal`. Prueba allí las instrucciones del README. Las instalaciones reales son independientes: no actualices controladores de otros proyectos con trabajos activos y no copies su estado a este repositorio.

Mantén pruebas enfocadas para cambios de comportamiento y actualiza la guía cuando cambie el contrato. Las pruebas verdes del controlador no sustituyen los tests, QA visual y aprobación humana del producto que lo use.
