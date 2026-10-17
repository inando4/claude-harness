# Harness de Claude — guía y agentes

**Distribución pública · 2026-10-17.** Claude Code nativo, una spec por conversación, subagentes por etapa y controlador Python. El modo predeterminado conserva la conversación donde escribes `/harness specs/Ready/mi-feature.md`, incluida la aprobación humana. El modo anterior de sesión gestionada queda como opción técnica.

Modelo y esfuerzo: `/harness` y los agentes de implementación, QA y documentación declaran `model: inherit`: usan el modelo activo de la sesión principal. Al invocar o reanudar agentes no se pasa un modelo explícito. Los archivos declaran `effort: xhigh`; cambiar el modelo no cambia esta preferencia y los niveles disponibles dependen del modelo.

Elige el modelo en tu sesión antes de ejecutar `/harness specs/mi-feature.md`. No hace falta repetir setup. Abre una sesión nueva para cargar las definiciones actualizadas; los trabajos y agentes ya iniciados conservan su configuración. Herencia documentada por Claude Code para [skills](https://code.claude.com/docs/en/skills#frontmatter-reference) y [subagentes](https://code.claude.com/docs/en/sub-agents#choose-a-model). No se lanzó una llamada al modelo para validar este cambio.

El lanzamiento independiente antiguo (`--launch`) sigue iniciando su propia sesión con `--model claude-opus-5 --effort xhigh`; no conoce el modelo seleccionado en otra conversación. Sus agentes heredan el de esa sesión. Esta excepción no afecta al flujo habitual `/harness <spec>`, que continúa en la conversación actual.

## 1. Flujo

**Spec en borrador → autorización para implementar → orden de inicio → worktrees + clon local → Graphify → implementación → comprobaciones → QA con Chrome visible → tu revisión en localhost → aprobación de la entrega → documentación → PR → CI → tu merge → limpieza.**

Toda spec nueva parte de `Estado: borrador` en `specs/Draft/`, incluida la creada desde [la plantilla](../specs/plantilla-spec.md). Completarla, resolver sus dudas o aprobar una maqueta no la habilita. Con `/harness ready specs/Draft/mi-feature.md`, Claude presenta la versión y solicita una confirmación `LISTA PARA IMPLEMENTAR <código>`. El hook `UserPromptSubmit` registra únicamente esa respuesta explícita en la sesión que pidió la revisión; `spec-accept` comprueba su vigencia, escribe `Estado: lista para implementar` y mueve la spec a `specs/Ready/`. No crea un trabajo ni lanza agentes. Esta autorización es independiente de la aprobación posterior al QA.

El recibo local de `.harness/spec-readiness/` vincula ruta, contenido y sesión de autorización. Se puede redactar y autorizar en una sesión y empezar a implementar en otra del mismo proyecto. `start` y `prepare` exigen el estado y el recibo vigentes; escribir la etiqueta o mover el archivo a Ready a mano no autoriza. Cualquier edición posterior, también de formato, invalida el recibo. Antes de modificar una spec autorizada que aún no tenga trabajo, usa `python3 harness/harness.py spec-draft <ruta>` para revocar la autorización y devolverla a `specs/Draft/`. `spec-status <ruta>` consulta su estado efectivo sin modificarla. Una spec sin campo Estado se considera borrador; las existentes no se autorizan automáticamente al actualizar.

La estructura es `specs/Draft/` (incompletas o sin autorización), `specs/Ready/` (autorizadas) y `specs/Done/` (terminadas), con `specs/plantilla-spec.md` en la raíz. Para adjuntos usa `specs/Draft/<nombre>/spec.md`: los movimientos incluyen la carpeta completa. El controlador actualiza destinos Markdown relativos, imágenes y referencias desde README, CLAUDE, docs y specs; conserva enlaces internos del conjunto. Los bloques de código no se reescriben. Los movimientos hechos por el controlador pueden conservar un recibo válido tras ajustar únicamente ruta, Estado o enlaces: registran las huellas anteriores y posteriores, sin inventar consentimiento. Los cambios manuales no tienen esa excepción.

Usa las rutas devueltas por los comandos. Una ruta antigua ausente se resuelve a su equivalente entre Draft/Ready/Done solo si es única; una ambigüedad bloquea la operación. Los documentos fuera de `specs/` mantienen compatibilidad, sin clasificarse en estas carpetas.

La autorización se comprueba de nuevo al registrar el trabajo y al copiar su spec al worktree. Los trabajos ya preparados continúan con su flujo y versión: repetir `start` en su conversación recupera el mismo trabajo y no exige aprobarlo retroactivamente. No se cambia la autorización de una spec que tenga un trabajo abierto. Actualizar el controlador requiere que sus trabajos hayan terminado; otra instalación independiente puede seguir trabajando.

Ejecuta `/harness spec/spec01.md` en una sesión y otra spec en otra sesión. No hay una segunda conversación coordinadora por spec. Dos trabajos independientes pueden estar abiertos simultáneamente. Cada uno tiene una sesión principal de Claude, una rama y worktree por repositorio, servicios y, si corresponde, PostgreSQL exclusivos. Los límites se aplican por repositorio/controlador; dos proyectos instalados no comparten cola ni reserva de navegador. Dentro de esa sesión se invocan los subagentes implementador, QA y documentalista **en orden**, con contexto separado por rol. Las correcciones regresan al implementador. Cada trabajo reserva su propia instancia de Chrome únicamente durante exploración visual; las comprobaciones y la lectura de código no reservan navegador. Dos trabajos pueden explorar simultáneamente, sin compartir perfil ni conexión MCP.

Claude Code muestra conversaciones, preguntas y permisos; Python prepara recursos y valida transiciones. No hay servidor, SDK, dashboard propio ni seis agentes ejecutándose a la vez. Una sesión en espera no genera llamadas periódicas. En esta versión la espera humana conserva uno de los dos cupos; no existe una cola automática para un tercer trabajo.

Cada instalación tiene un controlador en un repositorio principal (normalmente backend). Puede coordinar un monorepo o varios repositorios Git separados: cada trabajo crea la misma rama en un worktree de cada repositorio, con bases independientes, servicios propios y un único clon temporal. El frontend apunta al backend de ese trabajo. No necesitas instalar otro harness en el frontend. La versión revisada incluye ambos árboles: cambiar cualquiera exige repetir comprobaciones/QA. En SistemaJuridico, Spring Boot y Thymeleaf comparten proceso. Para specs dependientes, integra primero la dependencia y prepara la siguiente desde esa base. El CLI avanzado conserva lotes antiguos; la skill admite una sola spec por conversación. No hay resolución automática de dependencias entre ramas.

## 2. Archivos y configuración

| Archivo | Responsabilidad |
|---|---|
| [harness.py](../harness/harness.py) | Worktrees, Docker, comprobaciones, preview, estados y aprobación |
| `harness/config.json` (generado al instalar) | Adaptador, límites, comandos, servicios y base del proyecto |
| [Instalador](../harness/install.py) | Copiar la plantilla a otro repositorio sin reemplazar archivos existentes |
| [Adaptador por comandos](../harness/adapters/commands.py) | Cualquier stack con procesos configurables e informes JUnit |
| [Adaptador SistemaJuridico](../harness/adapters/sistema_juridico.py) | Adaptador específico de referencia para Spring Boot, Maven y Flyway |
| [skill /harness](../.claude/skills/harness/SKILL.md) | Entrada única: setup, inicio de specs y coordinación de roles |
| [Conversación y consentimiento](../harness/conversation.py) | Vincular sesión, solicitar revisión y registrar la respuesta explícita del usuario |
| [Autorización de specs](../harness/spec_readiness.py) | Borrador por defecto, permiso por versión y comprobación antes de preparar recursos |
| [Carpetas de specs](../harness/spec_layout.py) | Movimientos de archivos/carpetas, enlaces, colisiones y respaldo privado |
| [Chrome de QA](../harness/browser_mcp.py) | Lanzar MCP aislado y fijado a la versión del proyecto solo para QA |
| [Setup asistido](../.claude/skills/harness/setup.md) | Instrucciones de descubrimiento y configuración para Claude |
| [Soporte de setup](../harness/setup_project.py) | Inspección, validación, snapshot autorizado e inicio simplificado |
| [Implementador](../.claude/agents/harness-implementer.md) | Implementación y correcciones |
| [QA](../.claude/agents/harness-qa.md) | Revisión independiente, tests y navegador visible |
| [Documentalista](../.claude/agents/harness-documenter.md) | Actualizar documentación después de tu aprobación |
| [Permisos](../harness/claude-settings.json) | Restricciones de herramientas y operaciones reservadas al usuario |

`.harness/runs/<id>/` conserva estado, eventos, logs, reporte QA, credenciales locales y capturas; `.harness/worktrees/<id>/` contiene el código; con varios repositorios, los subdirectorios `<repo>/` contienen sus worktrees. Ambas quedan fuera de Git. No crear documentos por agente o intento.

Requisitos comunes: Linux, Python 3.10+, Git, Graphify, Node/npx, Google Chrome y escritorio gráfico. Además, las herramientas de tu stack: Java para SistemaJuridico; Docker solo cuando el proyecto usa PostgreSQL. Windows nativo y macOS no están soportados por la gestión de procesos basada en /proc y fcntl. Se comprobó Claude Code 2.1.273 y Playwright MCP 0.0.80. La primera invocación de npx puede descargar el paquete fijado. Usa `claude auth login` con tu suscripción; en modo independiente el controlador elimina variables de API de Anthropic al lanzar Claude. El modo conversacional utiliza la autenticación y ajustes de tu sesión ya abierta; no cambia de proveedor ni inicia otro cliente. Comprueba tu cuenta con `claude auth status`.

El modo conversacional hereda los ajustes y permisos de tu sesión. El instalador combina un hook `UserPromptSubmit` y restricciones del harness en `.claude/settings.local.json` sin reemplazar tus hooks/permisos existentes ni ampliar permisos. Abre una sesión nueva después de actualizar para cargarlo. El agente QA declara un MCP propio: `browser_mcp.py` usa la versión exacta de configuración, Chrome visible y perfil aislado; las evidencias se guardan en artifacts del trabajo. Se necesita la confianza normal del proyecto para cargarlo. No se usa `--dangerously-skip-permissions` ni codebase-memory-mcp. El lanzamiento independiente anterior conserva su configuración restringida de MCP.

**Alcance del aislamiento:** los comandos del controlador solo restauran, migran y eliminan su contenedor etiquetado, publicado en loopback. El agente conserva herramientas de shell y acceso de tu usuario: las instrucciones y permisos de Claude no equivalen a un sandbox de seguridad contra código malicioso. No concedas acceso a credenciales de producción. El snapshot debe estar autorizado y anonimizado; el controlador comprueba formato, identidad e historial, pero no puede certificar la anonimización.

## 3. Instalar en otro proyecto

Desde la raíz de esta instalación:

```bash
python3 harness/install.py /ruta/al/otro-proyecto --dry-run
python3 harness/install.py /ruta/al/otro-proyecto
```

El destino debe ser un repositorio Git existente con al menos un commit antes de preparar trabajos. El instalador copia `harness/` (sin cachés), la skill `/harness`, los tres agentes, esta guía y `specs/plantilla-spec.md`; crea Draft, Ready y Done con marcadores `.gitkeep` para conservarlas en Git. En una plantilla local existente solo cambia el campo Estado a borrador y conserva sus secciones. Añade a `CLAUDE.md` la referencia, la regla de autorización y el uso de las carpetas, y las exclusiones necesarias a `.gitignore`, conservando su contenido. La configuración local `harness/config.json` y los permisos `harness/claude-settings.json` quedan excluidos de nuevas publicaciones. Si un archivo del paquete ya existe con contenido diferente, detiene toda la instalación antes de escribir. Además combina el hook local y fija la ruta del lanzador MCP de QA al destino. No copia `.harness/`, backups, secretos, worktrees, datos de aplicación o sesiones. Una actualización guarda respaldo de los archivos modificados y no cambia el estado de specs existentes.

Después de actualizar, organiza las specs anteriores desde el proyecto destino con `python3 harness/harness.py spec-organize` (vista previa) y `python3 harness/harness.py spec-organize --apply` (aplicar). Requiere que no haya trabajos abiertos. Revisa archivos Markdown sueltos y carpetas con `spec.md`; conserva la plantilla y las specs ya en Done. Una autorización vigente permite Ready, `Estado: TERMINADO` permite Done y lo demás pasa a Draft. Una etiqueta descriptiva `Status` no sustituye el recibo de autorización ni acredita un cierre. Comprueba todo el lote antes de moverlo, bloquea colisiones/enlaces simbólicos y guarda respaldo privado en `.harness/spec-moves/`; ante errores de escritura revierte la operación sin pisar ediciones concurrentes. Repetir el comando sin cambios no vuelve a mover archivos. El instalador no ejecuta esta organización automáticamente.

Después de instalar, abre Claude Code en el proyecto destino y escribe:

```text
/harness setup
```

Claude inspecciona manifests, lockfiles, Graphify y scripts; configura los comandos, servicios, informes y conexión local por ti. Reutiliza una configuración existente que funcione. Solo pregunta lo que no puede deducir con evidencia (por ejemplo, backup autorizado y cuenta de prueba). No necesitas elegir una plantilla ni editar JSON a mano. Si no puede determinar un requisito, informa el pendiente en vez de inventar un comando.

El setup guarda ruta/huella del backup autorizado y estado local en `.harness/setup.json`; no copia el backup ni conserva contraseñas del origen. Si falta, deja `needs_snapshot`. Si cambian manifests/configuración o se sustituye el backup, pide validar otra vez. No modifica la configuración mientras haya trabajos abiertos.

**Validación por etapas:** setup comprueba configuración y herramientas y, con PostgreSQL, restaura un clon desechable y ejecuta la migración real antes de quedar listo. `databasePreflight` registra resultado, snapshot y contexto/motor Docker; cambiar de motor exige repetir setup. El clon de comprobación se elimina conservando logs. No usa la base principal ni el clon de un trabajo abierto. La primera spec ejecuta pruebas/build/migraciones y arranca el preview en un worktree aislado. Solo entonces se registra `checks_and_preview_passed`; QA y tu aprobación siguen pendientes. La preparación asistida no es una garantía automática de compatibilidad con cualquier stack.

Opciones avanzadas: puedes elegir una de estas plantillas con `--preset`; por defecto usa `auto` y Claude la adapta durante setup:

| `--preset` | Uso |
|---|---|
| `auto` (por defecto) | Punto de partida sin validar para `/harness setup`; no asume el stack ni la ausencia de base |
| `web-no-db` | Aplicación web sin base de datos; ejemplo de comandos npm/Vite |
| `node-postgres` | Ejemplo de frontend/backend en un repositorio, con PostgreSQL local restaurado |
| `sistema-juridico` | Configuración específica existente de Spring Boot/Maven/Flyway |

Referencia técnica para ajustes avanzados: las plantillas genéricas empiezan con `needsConfiguration: true`. La skill completa `harness/config.json` y cambia ese campo a `false` tras inspeccionar el proyecto. El JSON declara:

| Campo del adaptador `adapters/commands.py` | Qué declarar |
|---|---|
| `repositories`, `primaryRepository` | Opcionales: mapa nombre → `{path, base: "HEAD", copyPaths: []}` y nombre principal. El principal debe apuntar al controlador; cada entrada es una raíz Git distinta. |
| `requiredTools` | Ejecutables adicionales: npm, uv, dotnet, etc. |
| `setup`, `checks`, `build` | Listas de pasos con `command` (argumentos separados), `cwd` relativo, `repo` opcional y `env` opcional |
| `testReports` | Nombre de suite → ruta/glob JUnit XML generado por checks, o `{repo, glob}` |
| `database` | `provider: none`, o `postgres` con image, schema, migratorRole, appRole e historySql |
| `migrationSetup` | Pasos opcionales para instalar dependencias o construir el migrador en el checkout temporal del setup; mismo formato que setup. |
| `migrationFiles`, `migrate` | Globs de migraciones (texto o `{repo, glob}`) y paso que las aplica al clon |
| `integrationChecks`, `integrationReports` | Pasos e informes adicionales, ejecutados **después de migrar**; deben declararse juntos |
| `services` | Lista en orden de arranque: name, repo opcional, command, cwd, env, healthPath y artifacts (archivos/carpetas construidos) |
| `previewService` | Servicio cuya URL recibes para revisión; por defecto el primero |
| `env` | Variables comunes explícitas; nunca credenciales del origen |

Ejemplo mínimo para dos repositorios hermanos, configurado por `/harness setup` desde el backend:

```json
{
  "primaryRepository": "backend",
  "repositories": {
    "backend": {"path": ".", "base": "HEAD"},
    "frontend": {"path": "../mi-frontend", "base": "HEAD"}
  }
}
```

Los pasos del frontend declaran `"repo": "frontend"`; `cwd`, artefactos y globs son relativos a ese árbol. Sin `repo` se usa el principal. El preparador comprueba todas las bases/ramas antes de crear árboles y espera Graphify en cada uno. Conserva las carpetas originales, incluidos sus cambios sin commit; estos no se incorporan automáticamente al trabajo. `copyPaths` permite únicamente auxiliares locales explícitos de setup, respetando gitignore y rechazando secretos. Las specs se indican desde el controlador. Al revisar recibes la URL del frontend, ambos worktrees y el clon. Documentación actualiza únicamente los docs afectados de cada repo tras una aprobación conjunta. La creación Git no es una transacción entre repositorios: si algo falla después del preflight, queda `blocked` con recursos registrados para recuperación.

Cada comando se ejecuta directamente, sin interpretación de shell. Ejemplo: `["npm", "run", "build"]`. Los scripts del proyecto deben producir JUnit real, devolver error cuando fallen, respetar los puertos asignados y permanecer en primer plano al servir. Configura también las rutas de artefactos de build: el controlador comprueba que no cambien durante QA/revisión. Las integraciones que modifican datos deben limitarse al clon temporal o a sus propias bases desechables.

Variables disponibles dentro de argumentos y valores de `env`: `${PORT}`, `${URL}`, `${HOST}`, `${PORT_WEB}`, `${URL_API}` (según los nombres de servicios), `${JOB_ID}`, `${WORKTREE}` (del repo del paso), `${WORKTREE_BACKEND}`, `${WORKTREE_FRONTEND}` (según nombres declarados), `${RUN_DIR}`. Con PostgreSQL: `${DATABASE_URL}`, `${DB_HOST}`, `${DB_PORT}`, `${DB_NAME}`, `${DB_USER}`, `${DB_PASSWORD}`, `${DB_CONTAINER}`, `${DB_RESOURCE_KEY}`, `${SCHEMA}`. `DB_RESOURCE_KEY` es la identidad registrada del clon (`resourceKey`), exportada también como `E2E_CLONE_ID`; no se reconstruye desde el nombre del trabajo. El E2E del frontend recibe `E2E_PG=${DB_CONTAINER}`, `PGUSER=${DB_USER}`, `PGDATABASE=${DB_NAME}`, `E2E_CLONE_ID=${DB_RESOURCE_KEY}`, `E2E_API=${URL_API}/api/v1` y `E2E_PORTAL=${URL_WEB}`. Solo una ejecución E2E por clon. Los checks unitarios que importen helpers E2E deben incluirlos en su huella; conserva esas entradas en los scopes correspondientes. También se exportan DATABASE_URL y las variables PG* con la conexión local. En `migrate` se usa el rol migrador; en aplicación y tests, el rol de aplicación. Si tu framework usa otras variables, mapea esos nombres en `env`. Los endpoints de API incluidos en un frontend compilado deben configurarse durante `build`, no solo al arrancarlo.

`historySql` es un SELECT del historial de tu herramienta de migración, con orden estable; `${SCHEMA}` se sustituye por el esquema local validado. No depende de Flyway: adapta tabla/columnas a Prisma, Django, Liquibase, etc. La consulta se ejecuta en una transacción de solo lectura. El snapshot debe tener historial no vacío y ACL compatibles con los roles configurados. No cambies los nombres de roles de un backup sin preparar una copia compatible.

Para otro stack no hace falta editar el controlador si se expresa mediante estos comandos e informes. Si necesitas lógica adicional, copia `adapters/commands.py` a un adaptador propio dentro de `harness/` y apunta `adapter` a él. Sus funciones reciben el contexto `h` del controlador: `migration_setup` (preparación del migrador para el preflight), `validate`, `tools`, `database_config`, `service_names`, `app_env`, `checks`, `test_summary`, `migration_history`, `migrations_version`, `migrate`, `integration_checks` y `services`. La gestión de bases incluida admite **PostgreSQL o ausencia de base**; otros motores requieren implementar un proveedor, no basta con cambiar su nombre en JSON.

Después:

```bash
cd /ruta/al/otro-proyecto
python3 harness/harness.py doctor
python3 -m unittest discover -s harness/tests -v
```

Conserva el paquete reutilizable como respaldo. Si decides versionar la configuración del harness, hazlo en un cambio de herramientas separado: los PR de funcionalidades la excluyen. Mantén la configuración estable mientras haya trabajos abiertos. Los estados antiguos de v1 no se migran automáticamente: termina/limpia con esa versión antes de instalar v2. El instalador permite `--update` con respaldo y sin trabajos activos, conservando config y permisos locales.

## 4. Uso diario

Con setup completado, escribe en la sesión de origen:

```text
/harness specs/mi-feature.md
```

Pasa una sola spec. Claude ejecuta internamente `start <ruta> --session-id <sesión-actual>` y continúa en esta conversación: implementador, QA y documentalista son subagentes con contexto separado. No hay attach ni otra terminal. El coordinador permanece en el controlador; todos los cambios de producto usan rutas absolutas de los worktrees. Repetir la orden recupera el trabajo existente. Una segunda spec requiere otra sesión. Dos sesiones conservan bases, puertos y ramas independientes; cada QA vincula su Chrome independiente justo antes de explorar.

No repitas puertos, backup ni setup. El hook registra que está cargado antes de crear recursos: si falta, se pide abrir una nueva sesión, sin duplicar trabajos. Los comandos siguientes son **operación avanzada/compatibilidad con sesiones independientes**, no pasos del flujo diario.

Ejecuta siempre el controlador desde esta raíz, también cuando los agentes trabajen en sus worktrees. Con backend y frontend separados, usa la raíz del backend:

```bash
cd /ruta/al/backend
python3 harness/harness.py doctor
```

Escribe una spec breve en `specs/Draft/`: objetivo, alcance, actor, criterios observables con ID (AC-1…), casos de error, cambios de datos y dependencias. Reutiliza Spec Kit si el cambio lo necesita; no generes una colección de documentos para un ajuste pequeño. Autorízala antes de preparar el trabajo; la ruta devuelta estará en `specs/Ready/`.

Prepara un trabajo con una ruta **real** de backup local y una cuenta sintética del snapshot:

```bash
python3 harness/harness.py prepare consentimiento \
  --spec specs/Ready/texto-consentimiento.md \
  --branch feat/textoConsentimiento \
  --snapshot /ruta/al/backup-autorizado.dump \
  --safe-snapshot \
  --test-account candidato-prueba@example.test
python3 harness/harness.py launch consentimiento
```

Las rutas y la spec del ejemplo se sustituyen por las tuyas. `--spec` se puede repetir para un lote; `--base` permite indicar un commit/rama de partida. El controlador copia las specs y archivos del harness no versionados necesarios, pero no tus modificaciones de producto ni archivos `.env`. Si una funcionalidad depende de cambios sin commit, integra esos cambios en una base antes de prepararla.

Para PostgreSQL, el backup debe ser un `pg_dump -Fc` compatible con la imagen configurada, con datos, ACL compatibles con los roles locales e historial de migraciones. En SistemaJuridico se usa PostgreSQL 17, esquema `sistema_juridico` e historial Flyway. `--schema` permite elegir el esquema de la copia. Debe estar previamente anonimizado, sin destinos/secretos de servicios reales y con datos utilizables por la cuenta indicada. No se acepta una base vacía como sustituto del snapshot. El harness no extrae datos de producción ni elige un archivo solo porque sea el más reciente. Si falta, se detiene antes de crear recursos. Se registra su ruta y huella SHA-256; no publiques el backup ni credenciales.

Para otra spec independiente, repite `prepare` con otro ID/rama y abre `launch` en otra terminal. También puedes usar:

```bash
python3 harness/harness.py launch consentimiento --background
claude agents
python3 harness/harness.py status
python3 harness/harness.py show consentimiento
```

`claude agents` permite abrir y seguir sesiones. No lances otra sesión sobre el mismo trabajo activo. Estos comandos son opcionales y no se usan para una spec conversacional.

Para un proyecto explícitamente sin base (`database.provider: none`), no se pide backup ni cuenta salvo que su flujo necesite autenticación:

```bash
python3 harness/harness.py prepare mejora --spec specs/mejora.md --branch feat/mejora
python3 harness/harness.py launch mejora
```

### Preguntas y continuación

La sesión principal usa `AskUserQuestion` para mostrar opciones o recibir texto libre. Si la duda nace en un subagente, este devuelve `needs_input` y la principal te la transmite. Responde dentro de esa sesión; el otro trabajo puede continuar. Conversaciones y preguntas pertenecen a Claude Code, no a una segunda cola construida en Python.

Tras salir de Claude, reanuda tu conversación original con Claude Code. `launch <id>` solo aplica a trabajos del modo independiente; se rechaza para los vinculados a una conversación. Lee primero el estado/logs si hubo una interrupción: no se recrean automáticamente servicios ni se repite una migración dudosa. Una pregunta respondida y un permiso de herramienta nunca equivalen a aprobación final.

### Revisión final y documentación

Cuando QA pasa, Claude entrega **URL localhost**, rama/worktree, versión, cuenta de prueba, identidad/puerto del clon y criterios/evidencias. La aplicación y la base siguen encendidas durante tu revisión. Su contraseña local permanece en el archivo privado `secrets.json`; no se incluye en prompts o capturas.

Si pides cambios, la sesión registra `feedback` y vuelve al implementador. Si editas código manualmente, ejecuta `recheck` y vuelve a QA antes de aprobar:

```bash
python3 harness/harness.py feedback consentimiento "Corrige el comportamiento descrito"
# O bien, si ya hiciste cambios manuales:
python3 harness/harness.py recheck consentimiento
```

Cuando estés conforme, escribe **en el chat** la frase que te entrega Claude, por ejemplo `APROBAR a1b2c3d4`. Ese código identifica una solicitud con trabajo, sesión y versión exactos. Autoriza documentación, PR, espera de CI y limpieza después de que fusiones los PR; no autoriza merge automático. No es un comando de shell ni hace falta cambiar de terminal.

Internamente, `review <id> --session-id "${CLAUDE_SESSION_ID}"` prepara la solicitud; el hook registra únicamente esa frase exacta enviada por el usuario; `accept-review <id>` comprueba nuevamente versión y evidencias antes de documentar. Un “ok”, una respuesta de cuestionario o un permiso de herramienta no cuentan. Correcciones/recheck invalidan el consentimiento anterior. El agente no puede sustituir un hook ausente con aprobación propia. Es un control local como la confirmación por terminal, no una barrera contra procesos maliciosos con acceso de escritura al controlador.

Los trabajos antiguos sin vínculo conversacional conservan `approve <id> --version <huella>` en terminal interactiva.

Solo entonces actúa el documentalista: actualiza lo afectado en `/docs`, specs canónicas en `/specs` y referencias de `CLAUDE.md`. Un documento nuevo exige una necesidad duradera sin ubicación adecuada. `finish` comprueba que el producto aprobado no cambió y valida enlaces locales de los documentos editados. Para trabajos nuevos con `publication.enabled`, `finish` crea el commit y un PR por repositorio con cambios, espera CI y la fusión manual de todos los PR, y entonces limpia el entorno local. La aprobación incluye este cierre y se explica al pedir la confirmación. No hay merge ni despliegue automático. Los trabajos anteriores sin inventario de publicación mantienen su cierre conservando el entorno.

## 5. Qué comprueba el controlador

- **Graphify:** crea el worktree omitiendo hooks de Git solo para esa operación; ejecuta `graphify . --code-only`, espera, valida JSON y una consulta. Antes de QA actualiza únicamente los repositorios cuyas fuentes o grafo cambiaron; conserva una huella por repositorio y versión de Graphify. No comparte el grafo mediante symlinks ni usa codebase-memory-mcp. La caché entre worktrees queda como optimización futura.
- **Base:** un contenedor, volumen, puerto y credenciales temporales por trabajo. Confirma ID, etiqueta y exposición exclusiva en `127.0.0.1` antes de escribir o eliminar. Restaura el snapshot y conserva el historial configurado antes/después. Un proyecto declarado sin base omite estos pasos de forma explícita. El entorno de la aplicación sustituye las conexiones locales y no hereda variables de producción.
- **Comprobaciones de SistemaJuridico:** `./mvnw -B -Dui.capture=true -Dux.audit=true verify`, informes Surefire/Failsafe nuevos sin fallos ni tests omitidos, empaquetado, migraciones sobre el clon y health check del jar construido. Las dos propiedades activan los tres casos visuales que el proyecto omite normalmente. Los tests actuales usan sus propios Testcontainers; QA debe comprobar además el preview con datos del snapshot.
- **Comprobaciones genéricas:** instalación durante preparación → checks/build ejecutados o reutilizados con evidencia vigente → migración → integrationChecks nuevos → Graphify → servicios saludables. Conserva cada preview mientras sus fuentes, artefactos, entorno e identidad sigan vigentes; reinicia solo lo invalidado. Exige tests ejecutados, sin fallos ni omisiones. Comprueba que los puertos pertenezcan a los procesos del trabajo, incluidos hijos de lanzadores como npm.
- **Exploración de QA:** Playwright MCP controla Chrome visible para investigar caminos alternativos, errores, límites, navegación, permisos y persistencia. QA observa capturas, consola y red, y registra escenarios/hallazgos. No se limita al recorrido feliz de la spec.
- **E2E repetibles:** archivos de tests Playwright versionables con aserciones y datos aislados, ejecutados por CLI/runner (Playwright Test en JS/TS o Playwright Java/JUnit existente). Se ejecutan en headless, sin abrir ventanas. El comando normal de Playwright Test ya usa este modo; no usar `--headed`, `--ui`, `--debug` ni variables de debug que lo cambien. Conservar el motor/canal requerido por el proyecto; solo abrir E2E con ventana para depuración solicitada explícitamente por el usuario. La exploración MCP mantiene Chrome visible. Se registra comando, tests aprobados, código de salida e informe; los bugs exploratorios reproducibles se convierten en regresiones cuando sea viable. Los tests que requieren el preview se ejecutan por QA tras su arranque. Añadir tests exige recheck y QA de la versión final.
- **QA:** exige versión vigente, criterios con evidencia, reporte sin hallazgos pendientes, declaración de Chrome visible/inspección visual/migraciones y archivos de evidencia existentes. El reporte exige `exploration` y `repeatableE2E` separados; no acepta exploración MCP como única prueba E2E. Estos controles no demuestran por sí solos que el agente cubrió todos los criterios: revisa el reporte. Los tests que añada QA requieren repetir comprobaciones antes de su pass final.
- **Aprobación:** huella de archivos nuevos/modificados/eliminados, configuración, snapshot, conexión e historial de migración. Cambiar el producto después de QA impide aprobar hasta revalidar. La documentación espera confirmación interactiva del usuario.

Secuencia: `preparing → implementing → checking → ready_for_qa → qa → awaiting_human_approval → documenting → publishing → awaiting_ci → awaiting_merge → cleaning → completed`. Fallos producen `blocked`, correcciones vuelven a implementación y al agotar intentos se pasa a `paused`. Son cinco ciclos por defecto contando fallos de tests/producto, reportes QA `fail` y feedback. Preguntas, QA `blocked`, timeouts detectados y fallos de migración/infraestructura no consumen ciclos; requieren resolver el impedimento, sin reintentos automáticos. No se mide todavía un presupuesto acumulado de tokens por trabajo.

### Recuperar una conversación

Si Claude continúa con otro ID, cierra primero la conversación anterior y sus agentes y escribe `/harness recover <id>` en la nueva sesión. El controlador comprueba que el trabajo no tenga otra conversación activa y entrega una frase `RECUPERAR <id> <código>`. Solo tu mensaje exacto autoriza transferirlo. Se conserva el trabajo, QA y sus evidencias; se invalida la frase APROBAR antigua. Para aprobar, se emite otra revisión después de validar versión y preview. Si no puede comprobarse el cierre de la sesión anterior, reanúdala con `claude --resume <id-sesión>`; no se reasocian trabajos por carpeta ni se editan hashes.

### Verificación incremental y diagnóstico

El implementador ejecuta tests dirigidos. El controlador verifica la suite completa una vez por grupo mientras sus entradas y evidencia sigan vigentes. QA agrupa hallazgos y tests relacionados; pide `recheck` solo cuando el lote está listo. Cada nuevo ciclo usa implementador y QA nuevos con un traspaso breve. `brief.qaContext` identifica archivos cambiados desde el QA anterior y referencia su listado completo; orienta las regresiones, sin certificar evidencia ni datos vivos. El QA define una matriz breve de criterios, pantallas y variantes para corregir casos relacionados juntos. Las preguntas y un lote `tests_added` permiten reanudar dentro del mismo ciclo. La exploración en Chrome y los E2E contra el preview se realizan sobre la versión final; no se cachean.

En `adapters/commands.py`, setup configura por ti IDs estables, `cache`, `usesDatabase`, `reports`, `inputs`, `dependsOn`, `tools` y `outputs`. Ejemplo de un grupo Maven con Testcontainers propios (solo si se comprobó que no depende del clon):

```json
{
  "id": "backend-tests",
  "repo": "backend",
  "command": ["./mvnw", "-B", "verify"],
  "cache": true,
  "usesDatabase": false,
  "reports": ["unitarias", "integracion"],
  "inputs": [{"repo": "backend", "exclude": ["e2e/**"]}],
  "tools": ["java", "docker"],
  "outputs": ["target/classes", "target/test-classes"]
}
```

Conserva los flags reales de tu proyecto; el ejemplo no autoriza excluir tests. Cada informe pertenece a un solo grupo. Las dependencias apuntan a IDs anteriores; un grupo frontend puede depender de su instalación y de contratos backend. Sin exclusiones verificadas se considera todo el repo: un archivo desconocido invalida el resultado. No se deduce el alcance solamente de la extensión de un archivo. Setup revisa imports/configuración antes de excluir E2E de Maven, unitarios o build.

Cada recibo queda dentro del trabajo y combina fuentes nuevas/modificadas/eliminadas y permisos, specs, comando/configuración pertinente, herramientas, dependencias e informes/artefactos. Se conservan logs y copias de JUnit por intento. Un fallo, interrupción, cambio de entradas o evidencia borrada/alterada impide reutilizar. QA y aprobación revisan de nuevo los recibos. No se comparten recibos entre trabajos ni se convierten logs antiguos sin recibo en éxitos. El adaptador específico de SistemaJuridico y configuraciones sin grupos conservan su verificación completa.

La instalación declara `node_modules` en outputs. Setup puede excluir cachés generadas conocidas mediante `outputExcludes`, nunca los paquetes instalados. No enlaza instalaciones entre trabajos. Si no se pueden determinar entradas seguras, invalida conservadoramente y reinstala. Los servicios admiten `inputs` con el mismo criterio para conservar el preview ante cambios exclusivos de tests.

Uso diario: `verify <id>` y `recheck <id>` aplican estos controles automáticamente. Para investigar una caché dudosa: `verify <id> --force`. Las pruebas que usan el clon (`usesDatabase: true`) y `integrationChecks` siempre se ejecutan.

`show <id>` y `status` muestran el intento, grupos ejecutados/reutilizados, motivo, duración, comando actual y última actividad observada. Los logs están en `.harness/runs/<id>/attempts/<n>/`; `commands.jsonl` conserva duración/código de salida y `steps.jsonl` el resultado por grupo. Los procesos de preview que se conservan continúan escribiendo en el log del intento en que arrancaron. No se estima tiempo de razonamiento ni consumo de tokens a partir de silencios del agente.

### Actualizar una instalación existente

Desde cualquier terminal, con el paquete completo disponible:

```bash
python3 /ruta/a/claude-harness/harness/install.py /ruta/al/backend --update
```

La actualización se bloquea si hay cualquier trabajo abierto. Conserva `harness/config.json`, los permisos locales, estados y worktrees; guarda los archivos sustituidos en `.harness/install-backups/`. `--dry-run` muestra el alcance sin escribir. Después consulta `python3 harness/harness.py setup`: si la configuración/manifests y comprobaciones guardadas siguen vigentes, no necesitas repetir setup. Si el controlador detecta cambios, ejecuta `/harness setup` para revalidar lo que corresponda conservando tus elecciones. Con frontend separado conserva un único controlador y el mapa de repositorios. No edites hashes del estado para actualizar un trabajo activo.

### Recuperación y limpieza

| Situación | Acción |
|---|---|
| Fallo de tests/código | Leer el log, corregir y ejecutar `verify <id>`; después repetir QA |
| Graphify falló | Resolver la causa y ejecutar `graph <id>` antes de seguir |
| QA añadió tests o editaste código | `recheck <id>` y nuevo QA |
| Migración falló o cambió tras aplicarse | Tú ejecutas `reset-db <id>`, confirmas `RESTAURAR <id>` y verificas desde el mismo snapshot |
| Límite agotado | Tú decides si ejecutar `extend <id> --cycles 5`; confirmar `AMPLIAR <id>` |
| Interrupción durante preparación | Inspeccionar estado/logs; cancelar y limpiar recursos propios antes de preparar con un ID nuevo |
| Trabajo abandonado | Detener primero su sesión desde `claude agents`, después `cancel <id>` |
| Ya terminaste de revisar | Detener la sesión y ejecutar `cleanup <id>`; confirmar `ELIMINAR-CLON <id>` |

`cleanup` es la operación manual anterior para trabajos completados/cancelados; conserva worktree, logs y evidencias. `stop <id>` detiene el preview y marca bloqueo; no elimina la base. No se eliminan trabajos automáticamente por antigüedad. Una migración fallida no se arregla con `repair` ni alterando checksums. Al restaurar el clon se pierde únicamente su información temporal: registra feedback primero si estaba en revisión/aprobado.

### PR y limpieza sin repetir QA

Configura una vez mediante `/harness setup`. El cierre usa Python y GitHub CLI, sin nuevos agentes: aprovecha aprobación, tests y evidencia existentes. Los hooks Git de validación se conservan; el hook Graphify compatible recibe `GRAPHIFY_SKIP_HOOK=1`. Si un hook modifica el contenido, se bloquea antes del push.

```json
"publication": {
  "enabled": true,
  "pollSeconds": 30,
  "timeoutSeconds": 3600,
  "repositories": {
    "backend": {
      "remote": "origin", "base": "main", "workflows": ["ci.yml"],
      "requiredChecks": [],
      "disposablePaths": ["target", "test-results", "graphify-out", "harness/__pycache__"]
    },
    "frontend": {
      "remote": "origin", "base": "main", "workflows": ["ci.yml"],
      "requiredChecks": [],
      "disposablePaths": ["node_modules", "dist", "test-results", "playwright-report", "graphify-out"]
    }
  }
}
```

El ejemplo se adapta al stack: con un único repo se usa la clave `main`. Setup y preparación comprueban acceso GitHub, identidad Git, ramas y workflows antes de empezar la funcionalidad. Las rutas desechables solo pueden contener salidas generadas. La política incorpora la ruta concreta `<cwd>/node_modules` de instalaciones declaradas `npm ci` que la indiquen en `outputs`; cubre `e2e/node_modules` sin permitir todas las carpetas de ese nombre ni convertir outputs arbitrarios en borrables. Se registra por trabajo, se verifican sus límites y los archivos ignorados desconocidos siguen bloqueando. No habilites publicación si la configuración de destino/CI no está validada.

El índice privado publica producto, tests nuevos y documentación pertinente, quitando únicamente los overlays registrados al preparar. Revisa también commits nuevos, para que un archivo interno eliminado del diff final no viaje en el historial. Ante cambios ambiguos en un archivo compartido, secretos detectados, preparación parcial en el índice o contenido no aprobado, conserva el trabajo y bloquea. No usa `git add .`, force-push ni merge. Un repo sin cambios no genera PR.

Cada repo registra commit, push, PR y CI. El proceso independiente espera los workflows declarados y checks obligatorios para la revisión publicada. Un workflow ausente, pendiente, cancelado o fallido no permite limpiar. Consulta cada 30 segundos, hasta 60 minutos de espera de CI por ejecución. Con CI verde pasa a `awaiting_merge`, conserva el entorno y espera tus fusiones sin ese límite de tiempo. Mientras espera merge consulta solo los PR; no repite suites ni invoca modelos. Al fusionarse todos, revalida CI antes de limpiar. Si falla GitHub, CI o se agota el tiempo, se conservan entorno y evidencia; no se gasta un ciclo de corrección de producto.

```bash
python3 harness/harness.py show <id>
python3 harness/harness.py finalize <id>
```

`finalize` retoma el cierre en segundo plano después de resolver el bloqueo; no repite tests, documentación ni PR ya creados. El progreso y los enlaces quedan en `show`, `.harness/runs/<id>/state.json` y `finalize.log`. La sesión de Claude entrega control al ejecutar `finish` y termina su turno; no necesita esperar ni consultar CI. En modo conversacional todos los subagentes deben haber terminado y el coordinador dejar de tocar el trabajo al entregar el cierre. Su conversación original permanece abierta en la raíz del controlador; no se registra como proceso propio para detener.

Un PR abierto sobre la revisión publicada mantiene el trabajo en espera; solo todos los PR fusionados de forma verificable permiten limpiar. Un PR cerrado sin fusionar bloquea. Para merge, squash y rebase se comprueba el resultado de la fusión en la rama destino, por lo que puede haberse eliminado la rama remota de la funcionalidad. Si GitHub elimina la asociación del workflow al PR tras el merge, se verifica la ejecución previamente identificada o sus metadatos completos de commit/rama/repositorio/fechas; se consulta su resultado actual, sin aceptar un éxito antiguo tras un rerun fallido. Véase el [contrato de PR de GitHub](https://docs.github.com/en/rest/pulls/pulls).

Solo con todos los PR fusionados y CI válidos se comprueba el respaldo remoto exacto y se retiran la sesión solo si es gestionada por el modo independiente, previews, contenedor/volumen etiquetados del clon, worktrees y ramas locales de ese trabajo. Las ramas remotas, PR, snapshot autorizado, conversación y evidencias permanecen. Las specs de entrada copiadas y sin seguimiento se respaldan al preparar en `artifacts/input-specs/`; una copia ignorada solo se puede retirar si su contenido y su respaldo conservan el hash registrado. Esto no convierte `/specs` en una carpeta desechable. Archivos ignorados desconocidos, copias modificadas, respaldos ausentes o cambios locales posteriores bloquean el borrado. Si la limpieza se interrumpe, se retoma por checkpoints; una corrección después de empezar a retirar el entorno requiere un trabajo nuevo.

### Suites E2E opcionales

`apiE2E` es opcional y puede conservarse con `"enabled": false`: el setup y QA no la exigen ni la ejecutan. No se elimina su carpeta ni se cambian los tests unitarios/integración del backend. Los E2E del frontend pueden cubrir navegador, API y clon en un mismo recorrido. Una sección `e2e` o `apiE2E` sin `enabled` mantiene el comportamiento habilitado.

El preflight comprueba cada suite habilitada y bloquea las de Playwright sin archivos de pruebas reconocibles/no vacíos. Esta inspección no prueba que se hayan ejecutado: `runtimeValidation` permanece pendiente hasta la verificación real. Una suite deshabilitada se registra como tal, sin fabricar un resultado de tests. Los paths `apiE2E.testDir`/`report` son relativos al repo; los de `e2e`, a su `cwd`.

## 6. Validación y límites actuales

La suite del controlador usa repositorios Git temporales, procesos locales y GitHub simulado. Cubre instalación, configuración, autorizaciones, aislamiento, recibos de verificación, recuperación, publicación y limpieza. No llama a modelos ni valida automáticamente todos los escenarios de negocio de una aplicación.

```bash
python3 -B -m unittest discover -s harness/tests -p 'test_*.py'
```

Prueba optativa de portabilidad, con Node.js y Graphify disponibles. Crea y elimina repositorios temporales Python, Node y backend/frontend separados, y arranca servidores locales sin modelos ni base de datos:

```bash
python3 -B harness/tests/smoke_portability.py --run
```

Las pruebas de Docker y navegador son independientes. `harness/tests/smoke_runtime.py` comprueba recuperación de PostgreSQL sintético; `smoke_browser_isolation.py` requiere Chrome y escritorio. `smoke_local.py` es específico de una instalación compatible con el adaptador Spring Boot/Maven; no se ejecuta directamente sobre el paquete fuente sin esa aplicación. Consulta `--help` antes de usarlas.

La interpretación y escritura de configuración durante `/harness setup`, las preguntas, roles y reanudación dependen de Claude Code. Las pruebas del soporte determinista no equivalen a una sesión del modelo configurando un stack desconocido. El preflight de base tampoco acredita E2E o disponibilidad de servicios externos. Los permisos mantienen intervención humana cuando Claude la solicita.

Esta versión no incluye supervisión como servicio ni recuperación automática de conversaciones cerradas. Incluye métricas locales bajo demanda, sin equivalencia automática con cuota o precio. Las dependencias entre grupos de checks son explícitas; no planifica dependencias entre specs. Consulta los [pendientes conocidos](https://github.com/inando4/claude-harness/blob/main/docs/harness-pendientes.md), incluidos los límites de recuperación de volúmenes en modo local y de detección estática de destinos de base.

## 7. Mejoras recomendadas después del primer uso

1. Ejecutar primero una spec pequeña de extremo a extremo; después usar dos simultáneas. Identificar un snapshot autorizado reutilizable y probar su compatibilidad.
2. Mantener contexto corto: spec y hallazgos concretos por rol, logs detallados solo ante fallos, documentación una vez por lote. Ajustar modelo desde Claude según los resultados; no añadir agentes para tareas mecánicas.
3. Medir antes de aumentar concurrencia o construir un SDK runner. Añadir cola/presupuesto propio solo cuando la interfaz nativa se quede corta.
4. Al integrar ramas, probar el conjunto y las migraciones combinadas desde un snapshot actualizado. Dos QA individuales no prueban su integración.

## 8. Referencias y operaciones avanzadas

Documentación de las herramientas externas: [Claude Code](https://code.claude.com/docs/en/overview), [subagentes](https://code.claude.com/docs/en/sub-agents), [Playwright MCP](https://github.com/microsoft/playwright-mcp) y [CLI de Playwright Test](https://playwright.dev/docs/test-cli). El controlador espera explícitamente su generación de Graphify; no requiere copiar hooks de otros proyectos.

### Recuperar una sesión vacía del modo independiente anterior

El lanzamiento guarda el ID real devuelto por `claude --bg`, que puede diferir del UUID solicitado. Usa `claude attach <backgroundId>` para abrirla y `claude logs <backgroundId>` para ver su salida. El estado del trabajo incluye `backgroundId` y `sessionId`; `launch.log` conserva la salida del lanzamiento. Si Claude señala un diálogo de arranque pero la conversación aparece vacía, completa cualquier diálogo visible y escribe `/harness <id-trabajo> <ruta-controlador>` dentro de esa sesión. No ejecutes `start` ni setup: el trabajo ya existe. Si persiste el bloqueo, conserva la sesión y consulta logs antes de reiniciar. El controlador impide relanzar si hay una sesión activa en ese worktree incluso si su ID no coincide.

El prompt se separa de las opciones con `--`: en Claude Code 2.1.277, al retirar `--bg`, la opción variable `--add-dir` podía absorberlo como otra carpeta y abrir una sesión vacía. Si Claude devuelve `idle — send a prompt to start`, el controlador conserva la identidad de la sesión, devuelve un bloqueo con el comando de recuperación y evita anunciar éxito o crear otra sesión. Esto no confunde una petición normal de permiso/pregunta (`needs input`) con un prompt perdido.

Regresiones locales: separación del prompt en uno/dos repositorios, reanudación, arranque en primer plano, salida vacía aun con código 0, ID asignado por Claude y prevención de duplicados. El parser local de Claude reproduce el fallo antes del separador y recibe el prompt después; no se lanzó otra sesión del modelo para comprobarlo. La recuperación visual y el próximo arranque real siguen requiriendo validación en terminal. Actualiza instalaciones solo cuando sus trabajos estén cerrados: el controlador forma parte de la huella de cada trabajo.

### Comprobación temprana de migraciones y recuperación de red

`setup --validate` ejecuta la restauración y el migrador sobre un clon nuevo con el backup autorizado. Guarda los logs en una ejecución `setup-check-*`; no genera documentos. Un fallo no puede conservar un setup marcado listo. `doctor` sigue siendo solo una comprobación de herramientas. En proyectos sin base no crea Docker.

En Docker Desktop, `--network host` y el puerto publicado en el portátil no siempre representan la misma red. Para Flyway en Docker se comparte la red del clon: `--network container:${DB_CONTAINER}`, URL `jdbc:postgresql://127.0.0.1:5432/${DB_NAME}`. Backend y pruebas ejecutados en el host siguen usando `${DB_PORT}`.

El operador puede ejecutar `python3 harness/harness.py repair-flyway-network <id>` únicamente para este fallo conocido: exige trabajo bloqueado, sesión inactiva, ausencia de otros trabajos, log de conexión rechazada, migraciones e historial sin cambios y configuración anterior intacta. Guarda copias de configuración/estado, cambia solo transporte/puerto del migrador y comprueba la solución en otro clon. Solo entonces actualiza la configuración del trabajo y devuelve el intento consumido por infraestructura. Si falla el preflight restaura la configuración anterior. No modifica el clon del trabajo, no omite QA ni aprueba; vuelve a `implementing` y requiere `verify`. No se reutilizan automáticamente pruebas antiguas que carecen de una huella de código guardada al terminar. Nunca edites `configVersion` manualmente.

Los fallos en migración, Graphify o arranque del preview quedan bloqueados sin consumir ciclos de corrección de producto; no se reintentan automáticamente. Los fallos de checks/integración sí mantienen el límite. `node_modules` local e ignorado es normal en cada worktree y se conserva para evitar reinstalaciones innecesarias.


El setup estático de E2E comprueba la existencia del runner, script, configuración y reportero declarados, y detecta destinos Docker/psql fijos frecuentes en helpers. Un `pendientes.e2e` sin resolver bloquea preparación de specs. Esto complementa la inspección de scripts del asistente y el preflight real de migraciones; no demuestra que todos los caminos de conexión sean seguros ni que el runner haya emitido informes. La ejecución real y QA siguen siendo obligatorias.


### Aprobación entre terminales y entrega compacta de QA

Cada trabajo nuevo conserva un entorno permitido (`PATH`, locale, Java, escritorio y conexión Docker) y una huella en su estado privado. Los comandos de verificación, QA, aprobación y cierre lo usan también desde otra terminal; no se guardan claves de API ni variables de aplicación del entorno del usuario. Las herramientas se resuelven dentro de ese entorno y se comprueba su identidad. Cambiar un ejecutable o artefacto sigue invalidando resultados; las diferencias se explican por categoría sin imprimir valores sensibles. No edites huellas ni actualices un controlador con trabajos activos.

La sesión principal usa estos comandos (el usuario solo necesita su flujo habitual):

```bash
python3 harness/harness.py brief <id>
python3 harness/harness.py qa-start <id>
python3 harness/harness.py qa-template <id>
python3 harness/harness.py qa-check <id> <ruta-reporte>
python3 harness/harness.py qa-result <id> <ruta-reporte>
```

`brief` devuelve rutas, bases, suites habilitadas, versiones, resultados y referencias de helpers sin volcar el estado completo. `qa-template` crea un borrador por versión dentro de artifacts, sin sobrescribir evidencia ni marcar criterios como aprobados. `qa-check` es de solo lectura y devuelve los errores de contrato antes de registrar el reporte; comparte validación con `qa-result`. Un hallazgo abierto sigue impidiendo `pass`; `observations` no sirve para ocultar defectos del alcance. Si QA añade tests y encuentra bugs, registra primero el fallo y verifica una sola vez después de corregirlos. Solo los tests nuevos sin hallazgos requieren recheck antes de otro QA; el pase exige siempre la versión vigente. El QA final conserva Playwright CLI y exploración con Chrome visible.

Los agentes priorizan Graphify, diff, helpers y lecturas por sección. Documentación actualiza solo secciones afectadas de docs existentes. El modo solicitado es manual, pero los permisos efectivos dependen de Claude: una denegación explícita se transmite con su motivo, sin evasión ni permisos globales nuevos; un timeout admite como máximo un reintento equivalente si el sistema lo permite. Setup no promete eliminar todas las preguntas de permisos.

### Métricas locales bajo demanda

```bash
python3 harness/harness.py metrics <id>
python3 harness/harness.py metrics <id> --transcript /ruta/a/<sessionId>.jsonl
```

Muestra grupos ejecutados/reutilizados y duración registrada por etapa. Los intervalos incluyen espera humana y coordinación, no solo cómputo. Con la conversación local de esa sesión agrega sus subagentes, deduplica fragmentos por ID de mensaje y separa entrada, creación/lectura de caché y salida; no vuelve a sumar `iterations`. Si faltan datos no inventa consumo. Las ejecuciones activas se marcan parciales. No exporta contenido de conversaciones, no consulta en bucle ni llama a un modelo; no traduce contadores a dinero o cuota de suscripción. Estados y evidencias siguen fuera de los PR.

### Conversación única y aprobación

La actualización mantiene reutilización de comprobaciones, recheck por lote, brief compacto y cierre Python sin consultas periódicas al modelo. No vuelve a ejecutar tests al registrar la aprobación; valida recibos, versión y preview. El hook solo escribe un registro pequeño al invocar /harness o confirmar, y no llama a un modelo. No garantiza un ahorro porcentual de tokens o tiempo: hace falta medir una nueva spec real. Las pruebas del controlador no equivalen a una sesión real de Claude cargando permisos, subagentes y Chrome.

Validación de v2.6: 161 pruebas del controlador aprobadas, más smoke de portabilidad con arranque conversacional en Python, Node y backend/frontend separados. Incluye idempotencia del inicio, sesiones y puertos independientes, reutilización de checks, confirmación exacta por hook, rechazo de versión/sesión incorrecta y limpieza de dos repos sin detener la conversación. El hook instalado se ejecutó por stdin en un proyecto temporal con espacios en su ruta. Falta verificar una spec completa dentro de Claude Code real; no se hicieron llamadas al modelo ni se aprobó ningún trabajo de producto.

Cambio solicitado el 22/09/2026: el flujo habitual hereda el modelo de la sesión principal y conserva esfuerzo Extra (`xhigh`). Sustituye la selección fija de Opus 5 del 19/09. No requiere repetir setup; abre una sesión nueva para cargar las definiciones actualizadas.

### Recibos y medición local

La preparación tardó 49 s; hubo cinco verificaciones (36 min 33 s en total), con la última de 46 s reutilizando nueve grupos. Las pausas por límite de uso y reanudación no son tiempo de ejecución. Se eliminaron dos fuentes de repetición: verificar tests nuevos antes de corregir bugs conocidos y guardar JUnit de Vitest dentro del outputDir que Playwright limpia.

- `qa-result` acepta un fallo de la qaVersion revisada aunque QA haya añadido tests; vuelve a implementación y consume un solo ciclo. No permite aprobar una revisión modificada.
- El implementador corre pruebas dirigidas incluso antes del primer verify; la suite completa y build los centraliza el controlador. QA revisa el delta y usa E2E según riesgo.
- `brief` incluye checkPlan, testReports y hallazgos abiertos: los agentes reciben rutas y traspasos cortos, no el historial completo. El modelo se hereda de la sesión y se conserva esfuerzo Extra. Esto reduce contexto evitable; no promete un porcentaje de ahorro de cuota.
- Setup bloquea reportes de checks dentro de test-results/outputDir de Playwright. `install.py --update`, solo sin trabajos activos y con respaldo, migra únicamente el par de comando/reporte conocido `test-results/harness-unit.xml` a `.harness/reports/harness-unit.xml`. Conserva el resto de la configuración. Si cambió ese destino, se valida setup antes del siguiente trabajo.

No se rebajan las puertas de aprobación, las comprobaciones de evidencia ni las pruebas de seguridad/transacciones. Los fallos históricos sin comparación con la base se declaran como limitación; no se asume que son preexistentes.

`show` distingue el estado guardado de la actividad del cierre mediante `publication.workerStatus` (running/stopped/unknown). Si el proceso no está identificado o se detuvo, indica cómo retomar con finalize. Un cambio del head del PR después de publicar sigue bloqueando; el diagnóstico muestra ambos SHA y distingue ese cambio de un squash/merge normal.


### Vigencia del preflight E2E

Cambiar, añadir o eliminar tests/helpers no exige repetir setup: cada inicio valida la suite actual y los imports locales relativos, incluyendo auxiliares fuera de testDir. Configuración del runner y sus imports, manifests/dependencias o configuración del harness sí conservan sus controles de vigencia. Una suite habilitada vacía, un import local ausente o un helper con destino fijo sigue bloqueando.

Antes de crear el clon o instalar dependencias, el controlador valida los worktrees efectivos, sus manifests y entradas estables y guarda un comprobante por trabajo. No cambia el setup ni la evidencia de otra spec abierta. Al actualizar desde el formato anterior se ejecuta una sola vez `python3 harness/harness.py setup --refresh-tests`, sin trabajos abiertos: es validación estática, no restaura DB ni ejecuta suites. Si cambió infraestructura real se necesita setup completo.

`metrics --transcript` cuenta solo el intervalo del trabajo y los traspasos de conversación registrados; excluye mensajes previos y posteriores al cierre. Los trabajos antiguos sin marca del prompt usan el registro inicial como límite y se declaran parciales; filas sin fecha no se atribuyen. Informa contexto de entrada por mensaje (mediana/máximo), caché y salida por separado; thinking forma parte de output y no se suma de nuevo. No es una factura ni un medidor de cuota de suscripción.

`metrics` separa duración de etapas, espera en revisión humana y comandos registrados; estos tiempos se solapan y no deben sumarse. Señala pasos sin resultado final y separa tokens nuevos de lecturas de caché. Los intervalos sin mensajes no prueban inactividad; el tiempo efectivo pensando/explorando y la cuota facturada no se infieren de estos datos.


## Recuperación y recursos — 23/09/2026

`show` y `brief` leen el estado sin esperar el bloqueo del comando activo. `execution.status` distingue `running`, `interrupted`, `unknown` e `idle` usando PID, inicio y arranque del sistema; incluye hijos del grupo. El silencio del log no implica interrupción. `verify` recupera una verificación interrumpida bajo bloqueo exclusivo, conserva el intento y reutiliza recibos válidos. No duplica procesos vivos, no consume ciclo por interrupción ni reintenta una migración de resultado incierto.

Dos specs pueden avanzar a la vez. Los comandos pesados configurados del controlador comparten una reserva por instalación, con un supervisor que la conserva si muere el controlador. Valores opcionales de `resources`: `heavySlots: 1`, `waitSeconds: 1800`, `minAvailableMiB: 512`. Se detectan Maven/Gradle y comandos habituales de build/tests Node; `resourceClass: heavy|light` permite ajustar un paso. Espera y ejecución se miden por separado. La reserva no controla comandos externos de agentes/IDE, otras instalaciones ni memoria interna de Docker Desktop. Un timeout de recursos bloquea por infraestructura; no se cierran procesos ajenos ni se deduce OOM de un código de salida.

Los nuevos clones usan un puerto loopback explícito para conservarlo al reiniciar Docker. `verify` detecta el clon detenido y recupera el mismo contenedor tras acreditar ID, etiqueta, volumen y puerto; `resume-runtime <id>` permite pedir esa recuperación explícitamente; no restaura ni borra datos. Invalida la entrega viva y exige `verify`/QA, reutilizando checks válidos. No cambia entregas ya aprobadas ni adopta puertos dinámicos antiguos. `reset-db` conserva su autorización humana.

Antes de documentar/publicar se detectan conflictos y preparación parcial de Git. Desde `blockedPhase=publishing`, `finish` puede retomarse tras corregir índice/documentación permitida: exige la aprobación original, producto y evidencias intactos, configuración vigente y checkpoints consistentes. No repite QA por una reparación administrativa ni descarta staging. Después de publicar, usa `finalize` para CI/merge/limpieza. Un timeout de lectura GitHub admite un único reintento; errores de permisos y mutaciones no se reintentan por esta regla.

`finish` archiva el contenido final de las specs de entrada ignoradas en `artifacts/final-specs/<repo>/specs/`, conservando el original en `artifacts/input-specs/`. No fuerza specs ignoradas dentro del PR. Al completar el cierre, mueve la spec local ignorada a `specs/Done/` con `Estado: TERMINADO` solo si coincide con el origen registrado; conserva cambios del usuario y colisiones. Las specs ya versionadas se mueven dentro del mismo PR, después de la aprobación: el estado de la implementación no sustituye el estado CI/merge/limpieza del controlador. Se archiva además la versión completada en `artifacts/completed-specs/`. Valida contenido ignorado antes de publicar y otra vez al limpiar. Solo reconoce cachés adicionales con recibos de archivos creados por comandos Gradle configurados; archivos previos, modificados fuera del comando, desconocidos o enlaces externos siguen bloqueando. `.gitignore` no autoriza borrar.

Los selectores Maven globales deben incluir las clases anidadas: por ejemplo `-Dtest=*Test,*Tests,*Test$*,*Tests$*`. Las exclusiones de clases externas se conservan también para sus anidadas. El instalador actualiza únicamente estos patrones conocidos; setup verifica la configuración. Las suites nuevas no heredan pases de los selectores anteriores.


## Integración de specs simultáneas

Dos trabajos avanzan en paralelo; cada uno empieza desde la rama remota configurada. `verify` incorpora nuevos commits mediante una integración Git controlada antes de ejecutar pruebas. QA, revisión humana, aprobación, publicación y el cierre comprueban nuevamente la base; no se consulta al modelo en bucle. No se espera por otra revisión humana salvo dependencia explícita (`sync <id> --after <otro-id>`) o entrega parcialmente fusionada entre repositorios.

`sync` crea un checkpoint de producto filtrado y resuelve la integración en árboles temporales propios; valida estáticamente los tests/helpers E2E resultantes antes de aplicar cambios al worktree. Si hay conflictos, el implementador resuelve/prepara los archivos indicados y el controlador reanuda; no modifica main, hace force push ni duplica PR. Los cambios ya publicados se actualizan por avance de la misma rama/PR. Una interrupción conserva checkpoints y permite repetir `sync`.

La integración no consume un ciclo. Se conservan recibos, instalaciones y logs; `verify` reutiliza grupos vigentes según entradas/dependencias, no según fecha o historial Git. Cambios de producto invalidan QA/aprobación y requieren revisar el delta. Si únicamente cambia documentación y producto, preview y evidencia permanecen equivalentes, se conserva QA y, si ya existía, la aprobación. Las migraciones adicionales pueden aplicarse al clon conservando hashes de las anteriores; editar/eliminar una aplicada o un fallo de migración sigue requiriendo diagnóstico y reset autorizado.

Al retomar un bloqueo remoto, la skill ejecuta `sync` y sigue `lastResult.next`; no hace falta que el usuario lo ejecute en otra terminal. Python no resuelve conflictos de negocio ni despierta por sí mismo una conversación finalizada: si detecta el cambio durante CI/merge, conserva el entorno y requiere continuar la sesión. La limpieza sigue esperando todos los PR fusionados con CI válido. El controlador no impide que un humano fusione desde GitHub entre dos comprobaciones; las reglas del repositorio son la última protección de esa carrera.

El límite predeterminado es cinco ciclos totales. Actualizaciones conservan límites locales salvo petición explícita al instalador: `python3 harness/install.py <backend> --update --max-cycles 5`. Los trabajos antiguos no se migran en caliente al nuevo seguimiento de bases.


## Eficiencia y organización — entrega del 26/09/2026

- **Chrome por trabajo:** Playwright MCP expone `harness_browser_start(job, version)`. El agente lo llama con `brief.browser` inmediatamente antes de explorar; `browser_close` libera la instancia y su reserva. Las conexiones parten sin navegador ni trabajo asignado y rechazan herramientas de navegación hasta vincular un QA vigente. Perfiles efímeros, conexión MCP y artifacts independientes. La exclusión es por trabajo; `browser.maxExplorations: 1` permite volver a serializar solo la parte visual si falta memoria (predeterminado: 2). No compartir el clon entre exploración y tests que escriban simultáneamente. `metrics.browserExplorationSeconds` contabiliza reservas cerradas, no tiempo de razonamiento.
- **QA recuperable:** `qaContext.testDelta` conserva un diff de fuentes de tests, incluidos archivos nuevos sin seguimiento, con objetos privados deduplicados por hash (texto de hasta 256 KiB por archivo). No sustituye el diff de producto ni la vigencia de evidencias. `qa-checkpoint <id> <reporte-en-artifacts>` guarda progreso parcial asociado a qaVersion; `brief.qaCheckpoint` permite retomar sin rastrear conversaciones. Guardar por bloques significativos, no por llamada; agrupar tests antes de recheck. Nunca convertir un checkpoint en un pase ni repetir suites completas sin criterio o fallo que lo justifique.
- **Cierre durante un merge:** si cambia la base después de consultar los PR, volver a consultar su estado antes de declarar un avance ajeno. Una fusión propia espera al resto de PR y conserva la revalidación de CI; un avance ajeno con PR aún abiertos sigue bloqueando. No llama modelos, rehace QA ni necesita un mensaje humano para continuar una fusión propia observada.
- **Specs:** borradores en `specs/Draft/`, autorizadas en `specs/Ready/` e implementadas en `specs/Done/`, conservando subcarpetas y adjuntos. Las versionadas viajan en el PR aprobado; las ignoradas se mueven al completar el cierre sin publicar ni pisar contenido nuevo del usuario. Un conjunto con `spec.md` se archiva completo; si mezcla recursos versionados/locales o cambió su inventario final, conserva el origen para reconciliación explícita. El controlador actualiza enlaces Markdown relativos del archivo movido; en specs versionadas también ajusta enlaces explícitos desde README/CLAUDE/docs/specs. Colisiones o modificaciones locales preservan el original y se reflejan en `specCompletion`; la copia final permanece en artifacts. No se modifica el código del checkout principal ni se crea un PR extra para cambiar el estado.
- **Diseño:** el implementador usa Impeccable cuando esté disponible y el alcance incluya una pantalla nueva, rediseño o cambio visual relevante; conserva la identidad existente en cambios pequeños. No instala la skill automáticamente ni añade un agente de diseño. QA mantiene la exploración visible. No activar `/goal` ni `/loop`.
- **Concurrencia:** conservar dos trabajos y un QA por etapa. La medición no justifica añadir agentes por defecto: primero eliminar esperas de navegador y reconstrucción de contexto. La mejora real de tiempo/RAM/tokens se contrasta en próximas specs comparables; pruebas del controlador y smoke no garantizan menor duración para cualquier funcionalidad.

### Preflight y remedios de infraestructura

El preflight diferencia declaraciones de namespaces XML conocidas de destinos de conexión en auxiliares E2E. La excepción es exacta y local a la declaración: no autoriza dominios completos ni oculta otras conexiones del archivo. Los roles, contenedores y bases fijos siguen bloqueados. Es una comprobación estática conservadora, no una prueba completa de flujo de datos.

Si el defecto corresponde al controlador/configuración, los agentes entregan un diagnóstico acotado y no editan el paquete durante el trabajo. Una huella de publicación cambiada no se resuelve repitiendo verify/QA: hay que restaurar la versión exacta respaldada o preparar una recuperación independiente, sin editar hashes. Las actualizaciones mediante instalador requieren cero trabajos activos.
