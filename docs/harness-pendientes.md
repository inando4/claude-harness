# Limitaciones y pendientes conocidos

Estado de la distribución pública: 16 de octubre de 2026. Esta lista registra trabajo pendiente; no implica que las correcciones estén implementadas ni autoriza cambios en instalaciones existentes.

## Compatibilidad

- Linux es la plataforma soportada. La gestión de procesos depende de `/proc`, grupos de procesos y `fcntl`; Windows nativo y macOS necesitan adaptación.
- Se admiten PostgreSQL y proyectos sin base. Otros motores requieren un adaptador. Las plantillas son ejemplos que setup debe adaptar y validar.
- La configuración asistida depende de Claude Code y de las herramientas del proyecto. La suite determinista no acredita que el modelo configure correctamente cualquier stack desconocido.
- QA visual requiere Chrome y escritorio gráfico. No hay un modo cloud completo ni un servicio que reactive por sí solo conversaciones cerradas.

## Fiabilidad que debe mejorarse

1. **Recuperación de volúmenes en modo local.** La creación y validación de etiquetas Docker no es consistente cuando publicación está deshabilitada. `resume-runtime` puede fallar si Docker devuelve `Labels: null`. Hay que etiquetar recursos propios con independencia del modo de publicación y rechazar recursos sin identidad de forma controlada. No adoptar ni borrar un volumen ajeno para resolverlo.
2. **Aislamiento de todas las pruebas de base.** El preflight estático detecta patrones frecuentes, pero no demuestra todos los destinos de conexión. Los comandos del proyecto deben usar el clon del trabajo o una base sintética propia. Una suite con conexiones fijas a una base compartida puede agotar conexiones y comprometer el aislamiento; requiere corregir su configuración antes de usarla.
3. **Preflight y diagnóstico de infraestructura.** Ampliar detección temprana de outputs enlazados, tests omitidos, colisiones de versiones de migración y residuos de compilación. Mejorar la clasificación de fallos de infraestructura durante checks para evitar consumir ciclos como si fueran errores del producto.
4. **Contexto inicial de QA.** La primera captura puede producir un delta de archivos demasiado amplio. Compararla con la base real del trabajo reduciría lecturas sin sustituir la comprobación de evidencia vigente.
5. **Cierre local frente a publicación.** Con `publication.enabled: false`, `completed` indica cierre local y puede conservar el preview. La descripción de autorización todavía debe adaptarse mejor a ese modo; no inferir que existe un PR, que pasó CI o que se limpiaron recursos por ver ese estado.

## Validación y evolución

- El pase del controlador, los tests unitarios y los mocks no prueban por sí solos todos los escenarios de negocio. QA debe declarar qué verificó contra el preview real y qué quedó cubierto solo mediante tests aislados.
- Medir ahorro de tiempo, memoria y tokens en trabajos comparables antes de aumentar concurrencia o introducir más agentes. Las pruebas sintéticas de portabilidad no garantizan mejoras de rendimiento para todos los proyectos.
- Al integrar specs, comprobar contratos y migraciones combinados. Dos QA individuales no demuestran la compatibilidad de sus cambios integrados.

Las specs en borrador, la autorización explícita por versión, la invalidación tras editar y el bloqueo de actualizaciones con trabajos activos **sí están implementados**. Consulta su uso en la [guía](harness-claude-spec.md).
