# Desarrollo del harness

Este repositorio contiene el código fuente y el instalador de Claude Harness. Los proyectos donde se instala tienen configuración y estado independientes.

- Para preguntas de código y relaciones usa Graphify: si existe `graphify-out/graph.json`, consulta `graphify query "<pregunta>"`. Si falta o está desactualizado, ejecuta `graphify . --code-only`. No uses codebase-memory-mcp ni sus hooks.
- Guía vigente: `docs/harness-claude-spec.md`. Pendientes: `docs/harness-pendientes.md`. Los pendientes no implican autorización para implementarlos.
- Las instalaciones en otros proyectos son independientes. No sobrescribas controladores/configuración ni evidencias de trabajos activos. Usa el instalador y sus validaciones cuando corresponda.
- Pruebas del controlador: `python3 -B -m unittest discover -s harness/tests -p 'test_*.py'`. Las pruebas smoke con Docker/navegador son independientes.
- No publiques `.harness/`, `.migration-private/`, configuración de instalaciones, backups de bases ni conversaciones. No muevas ni actualices trabajos activos de otros proyectos durante cambios del código fuente.
