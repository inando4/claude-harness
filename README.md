# Claude Harness

[![CI](https://github.com/inando4/claude-harness/actions/workflows/ci.yml/badge.svg)](https://github.com/inando4/claude-harness/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Un flujo reutilizable para implementar specs con **Claude Code**, comprobar el resultado y revisarlo antes de publicarlo. Un controlador Python coordina worktrees Git, pruebas, entornos locales, agentes de implementación/QA/documentación y aprobaciones humanas.

**Borrador → autorización de la spec → implementación → tests → QA en navegador → revisión humana → documentación.** Si habilitas publicación: **PR → CI → tu merge → limpieza del entorno temporal**.

Se instala dentro de tu proyecto. Admite un único repositorio o backend/frontend con Git separados; cada trabajo tiene sus propios worktrees y servicios. PostgreSQL usa un clon local a partir de un backup autorizado. También admite proyectos sin base de datos.

## Requisitos

- **Linux**, Python **3.10+** y Git. La gestión de procesos usa `/proc` y `fcntl`; Windows nativo y macOS no están soportados.
- [Claude Code](https://code.claude.com/docs/en/overview) instalado y autenticado. El harness utiliza tu sesión y sus permisos; no incluye una suscripción ni acceso a modelos.
- [Graphify](https://github.com/Graphify-Labs/graphify) en el `PATH`, con los comandos `graphify . --code-only` y `graphify query`. El paquete Python se llama `graphifyy`; sigue la instalación de su repositorio.
- Node.js/npm (`npx`), Google Chrome y un escritorio gráfico para QA visual. El lanzador usa una versión fijada de Playwright MCP.
- Las herramientas y dependencias de tu stack. Docker si el proyecto usa PostgreSQL; [GitHub CLI](https://cli.github.com/) autenticado si habilitas PR/CI.

El adaptador genérico ejecuta los comandos de tu proyecto y lee informes JUnit. Las plantillas requieren configuración asistida: no garantizan compatibilidad automática con todos los frameworks. Otros motores de base de datos necesitan un adaptador.

## Instalar

Clona el paquete completo y ejecútalo desde su raíz:

```bash
git clone https://github.com/inando4/claude-harness.git
cd claude-harness
python3 harness/install.py /ruta/a/tu-proyecto --dry-run
python3 harness/install.py /ruta/a/tu-proyecto
```

El destino debe ser un repositorio Git existente; necesitará al menos un commit antes de iniciar trabajos. Sustituye `/ruta/a/tu-proyecto` por su ruta real. Si backend y frontend tienen repositorios separados, instala solo en el backend: setup incorpora el frontend al mismo controlador.

El instalador incorpora `harness/`, la skill `/harness`, tres agentes, la guía, la plantilla de spec y el hook local de autorización. Conserva tus instrucciones y permisos ajenos al harness; ante archivos incompatibles se detiene antes de escribir. No copies únicamente `install.py`: necesita el resto del repositorio.

Abre **una sesión nueva de Claude Code en el proyecto destino** y escribe:

```text
/harness setup
```

Claude revisa el stack y configura comandos de tests, builds, servicios, informes y conexión local. Si usa PostgreSQL, necesitará la ruta de un backup autorizado y anonimizado, además de una cuenta sintética para QA. El harness no extrae datos de producción. La validación de setup no sustituye las pruebas y el QA de cada funcionalidad.

## Implementar una spec

1. Crea `specs/mi-feature.md` desde `specs/plantilla-spec.md` y completa sus criterios. Empieza en **borrador**.
2. Escribe `/harness ready specs/mi-feature.md`. Revisa el alcance y responde exactamente `LISTA PARA IMPLEMENTAR <código>` con el código que te muestre Claude.
3. Inicia el trabajo con `/harness specs/mi-feature.md`. Autorizar la spec no inicia su implementación.
4. Revisa el resultado en la URL local que recibas después de tests y QA. Entrega feedback o la confirmación explícita que solicite el controlador.

La autorización corresponde a una versión concreta: cualquier cambio posterior invalida el permiso. Escribir a mano «lista para implementar» no la autoriza. La aprobación de la spec y la aprobación de la entrega son independientes.

Usa una conversación por spec. El límite predeterminado es dos trabajos simultáneos y cinco ciclos de corrección por trabajo. Los agentes heredan el modelo elegido en tu sesión. Con publicación habilitada, el controlador abre los PR y espera CI y **tu merge** antes de limpiar; no fusiona automáticamente.

## Actualizar una instalación

Cuando no haya trabajos activos en el proyecto destino, desde tu copia de este paquete:

```bash
git pull --ff-only
python3 harness/install.py /ruta/a/tu-proyecto --update --dry-run
python3 harness/install.py /ruta/a/tu-proyecto --update
```

El instalador bloquea actualizaciones con trabajos abiertos, respalda los archivos reemplazados y conserva configuración, permisos y estado local. Aplica únicamente migraciones de configuración reconocidas. Abre otra sesión de Claude Code para cargar los cambios; repite `/harness setup` si el controlador pide revalidar la configuración o el entorno.

## Documentación y límites

- [Guía completa y referencia de configuración](docs/harness-claude-spec.md)
- [Plantilla de spec](specs/plantilla-spec.md)
- [Limitaciones y pendientes conocidos](docs/harness-pendientes.md)
- [Cómo contribuir y ejecutar pruebas](CONTRIBUTING.md)

Este repositorio distribuye código, plantillas y pruebas. `harness/config.json` se genera en cada proyecto mediante el instalador; el paquete fuente no es una aplicación donde debas ejecutar `/harness setup`. Los estados `.harness/`, backups, credenciales y conversaciones permanecen locales.

El harness ejecuta procesos con los permisos de tu usuario. Sus controles de flujo y las aprobaciones de Claude no constituyen un sandbox contra código malicioso. Usa proyectos confiables y datos sintéticos o anonimizados para las pruebas. No incluyas secretos ni datos de usuarios al abrir un issue.

## Licencia

[MIT](LICENSE). Puedes utilizar, modificar y redistribuir el código conservando el aviso de licencia. Proyecto independiente, sin afiliación oficial con Anthropic; Claude Code y las demás herramientas conservan sus propias condiciones.
