# [Nombre de la funcionalidad]

Estado: borrador

Crear, completar o revisar esta spec no autoriza su implementación. Solo el usuario
puede habilitar la versión revisada mediante `/harness ready <ruta-spec>` y la
confirmación `LISTA PARA IMPLEMENTAR <código>` que devuelve el controlador.
El controlador cambia el estado; no marcarla lista manualmente. Esta autorización
no inicia un trabajo ni sustituye la aprobación de la entrega después del QA.
Antes de iniciar un trabajo, si se modifica una spec ya autorizada, devolverla a
borrador con `spec-draft` y solicitar una nueva autorización.

## Objetivo
¿Qué problema resuelve y para quién?

## Contexto
- Comportamiento actual:
- Pantallas o procesos afectados:
- Referencias existentes: enlaces a docs, diseños o specs.

## Alcance
- Incluye:
- Queda fuera:

## Comportamiento esperado
1. El usuario...
2. El sistema...
3. Si ocurre un error...

## Referencia visual
Completar si se crea una pantalla o cambia su diseño. Para cambios sin impacto visual
o que solo reutilizan componentes existentes, indicar «No aplica» y omitir el resto.

- Maqueta o Artifact: [enlace al prototipo, imagen o boceto].
- Versión aprobada: [fecha/versión]. Usar la misma referencia durante implementación y QA.
- Capturas locales aprobadas: [enlaces Markdown relativos a las imágenes], disponibles
  en los worktrees. Incluir pantalla, estado y dimensiones de la ventana en cada referencia.
  Conservar esta copia aunque exista un enlace externo que pueda cambiar o exigir sesión.
- Pantallas y estados relevantes: normal, vacío, cargando, con error y con datos largos;
  incluir paneles, menús o formularios desplegados cuando formen parte del cambio.
- Escritorio y móvil: [dimensiones a comprobar y cómo se adapta la pantalla].
- Diseño existente: [enlace a la guía del proyecto y componentes que se reutilizan].
- Fidelidad requerida: [disposición, jerarquía, controles e interacciones obligatorios];
  puede adaptarse: [aspectos concretos a criterio de quien implementa].

Los textos y reglas de esta spec prevalecen sobre la maqueta. Respetar el diseño existente
salvo los cambios visuales aquí definidos. Si una interacción necesita más detalle, añadir
su recorrido o una copia exportada del prototipo junto a las capturas.

## Criterios de aceptación
- AC-01: Dado [estado inicial], cuando [acción],
  entonces [resultado observable].
- AC-02: ...
- AC-03: Dado [actor y estado], cuando abre [pantalla] en [dimensiones],
  entonces [elementos concretos] cumplen la referencia [captura aprobada] y las reglas
  de adaptación descritas. Reemplazar o eliminar este ejemplo según el alcance.

## Casos límite y errores
- Datos vacíos o inválidos:
- Usuario sin permisos:
- Recarga, reintento o doble envío:
- Otros casos relevantes:

## Datos y compatibilidad
- Información que debe guardarse:
- Comportamiento para registros existentes:
- Restricciones de compatibilidad:
- Si no aplica, indicarlo.

## Verificación
- Actor/rol y datos de prueba necesarios:
- Recorridos que deben quedar como E2E repetibles en headless:
- Riesgos que QA debe explorar con Chrome visible:
- Comparación visual de QA (si aplica): [pantallas, estados y dimensiones] frente a
  [capturas aprobadas]; guardar evidencia de la implementación y registrar diferencias
  respecto a los elementos obligatorios como hallazgos.
- Resultado que quiero comprobar personalmente:

## Dependencias y dudas
- Requiere:
- Decisiones pendientes: ninguna / [pregunta concreta].
