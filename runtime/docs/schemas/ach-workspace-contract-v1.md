# Contrato de ACH para ach-runtime v1

Este documento define la interfaz nueva entre el operador ACH, Channels + Harness y los pods de ejecución de **ach-runtime**, cuyo repositorio de destino es [ackstorm/ach-runtime](https://github.com/ackstorm/ach-runtime). Recoge el modelo acordado el 30 de septiembre de 2026. Sustituye el contrato del runtime anterior para esta implementación; `old-ach-agent/` es referencia, no una dependencia. El contrato es un diseño para implementar, no describe funcionalidades ya disponibles en el operador. La carpeta local actual puede seguir llamándose `ach-agent`; no cambia la identidad del producto.

El ejemplo completo está en [gitlab-pr.yaml](../../examples/gitlab-pr.yaml). El plan está en [el plan de implementación](../superpowers/plans/2026-09-30-workspace-runtime.md). Las decisiones concretas de protocolo y los valores por defecto de este documento son la propuesta técnica que hace ejecutable el modelo acordado.

## 1. Modelo y responsabilidades

Un `ACHAgent` declara identidad de ACH, canales, instrucciones, modelo, capacidades y políticas. ACH despliega un pod de control con Channels + Harness. El Harness crea y gestiona múltiples Workspaces, cada uno con un StatefulSet de cero o una réplica. Todos los trabajos, incluidos eventos atómicos y futuras fases de workflows, usan esta misma topología. No existen Jobs ni placements `standalone`, `distributed` o `sandboxed` configurables.

Cada Workspace pertenece a un único `ACHAgent` y no puede migrar a otro. Cada sesión activa tiene su proceso OpenCode v2 dentro del pod de ese Workspace. Varias sesiones pueden ejecutarse simultáneamente o en secuencia, sujetas a los límites. Un Workspace tiene como máximo un pod activo escritor. Un StatefulSet no se escala a varias réplicas para aumentar la concurrencia.

| Componente | Responsabilidad |
| --- | --- |
| Operador ACH | Validar el CR, resolver el perfil, provisionar pod de control, exposición, permisos y configuración de almacenamiento; publicar condiciones del ACHAgent. |
| Channels | Autenticar, filtrar y normalizar entradas; obtener las dos claves; convertir aceptación o rechazo al protocolo del origen. |
| Harness | Admitir eventos, deduplicar, ordenar, activar Workspaces, ejecutar handoff, entregar trabajo, conservar credenciales, publicar fachadas y coordinar Storage. |
| Mini-harness | Gestionar procesos y sesiones, confirmar arranque de cada ejecución, cancelación, hooks y snapshots consistentes. |
| OpenCode v2 | Ejecutar el agente con configuración pública y herramientas autorizadas. |
| Storage | Restaurar y persistir Workspaces y sesiones; verificar, registrar y recuperar Artifacts; resolver caché y almacenamiento durable. |

Inicialmente el Harness crea directamente los objetos Kubernetes mediante un cliente reducido. Un futuro CR `Workspace` reconciliado por ACH puede sustituir ese backend sin cambiar Channels, ejecución ni Storage. El pod de control tiene una réplica; la primera versión no promete HA de varios Harness coordinadores del mismo ACHAgent.

Release boundary confirmed on 2026-10-01: v0.1.0 uses a standard Workspace
StatefulSet with zero/one replica and execution settings resolved by ACH from
the profile. The Harness uses a fixed workload template, not arbitrary PodSpecs
or its own profile resolver. An ACH-managed Workspace CR is deferred to v0.1.1;
it is not a requirement or an implemented feature of v0.1.0. The Kubernetes
backend remains behind WorkspaceManager so replacing it does not change channel
identities or the mini-harness Started protocol. Webhook HTTP acknowledgement is
now separate from that internal Started signal, as described in section 3.

## 2. Identidades y evento normalizado

La identidad de Workspace es `(namespace, ACHAgent UID, workspace_key)`. Usar el UID evita que un ACHAgent nuevo con el mismo nombre adopte accidentalmente estado del eliminado. Una sesión se identifica por esa referencia y `session_key`. Su nombre legible compuesto es `<workspace_key>:<session_key>`, pero no se reconstruyen los campos separando por `:`.

```json
{
  "eventId": "gitlab-delivery-uuid",
  "channel": "gitlab-mr-review",
  "workspaceKey": "gitlab:ackstorm:project:42",
  "sessionKey": "mr:7",
  "type": "merge_request",
  "payload": {},
  "context": {
    "gitlab": {
      "projectId": "42",
      "projectPath": "team/service",
      "mergeRequestIid": "7"
    }
  }
}
```

`eventId` identifica una entrega; `invocationId` identifica su ejecución y permanece estable entre reintentos previos al ACK. El Harness lo deriva de ACHAgent UID, channel y eventId. Un UUID de entrega GitLab procede de su cabecera de entrega, validada por el adapter; no se usa como clave de Workspace. Un origen sin identificador debe definir una derivación estable o recibir un error; no se genera un identificador nuevo en cada redelivery.

`channels[].routing` es opcional. El channel entrega ambas claves ya resueltas al Harness; este no interpreta el payload para deducir conversaciones. Cada override, `workspaceKey` o `sessionKey`, es opcional e independiente: el campo omitido conserva el default del adapter. Un override presente debe ser una plantilla string no vacía y un render inválido falla sin fallback. `workspaceKey: session` es una palabra reservada: un Workspace por sesión, con clave JSON `[<workspace por defecto>, <session key>]` (una `session_key` solo es única dentro de su Workspace: `mr:7` existe en cada proyecto). `null` no es utilizable: el campo del CRD de ACH es un string opcional no nulo y Kubernetes lo poda. Los orígenes sin identidad de conversación estándar (webhook genérico, Redis queue) deben declarar `routing.sessionKey`; sin él la configuración no carga.

Defaults aprobados el 1 de octubre de 2026:

| Origen | Workspace | Sesión |
| --- | --- | --- |
| GitLab MR y sus notes | Identidad configurada del servidor + projectId | `mr:<mergeRequestIid>` |
| GitLab issue y sus notes | Identidad configurada del servidor + projectId | `issue:<issueIid>` |
| Webhook genérico | `webhook:<channel>` | `routing.sessionKey` (obligatorio) |
| GitHub pull request | `github:<channel>:<repo>` | `pr:<number>` |
| Cron | Channel | Tick programado original en UTC |
| Redis queue | `queue:<channel>` | `routing.sessionKey` (obligatorio) |
| A2A | `a2a:<channel>` | `contextId` o `taskId` |

Los prefijos separan dominios (`gitlab`, `webhook`, `github`, `cron`, `queue`); los campos se codifican sin ambigüedad antes de derivar referencias. GitLab no usa event.type ni el ID del comentario como sesión: MR y note de MR continúan la misma conversación, igual que issue y note de issue. Un evento no soportado se ignora explícitamente; uno soportado sin identidad necesaria se rechaza. Cron conserva archivos entre ticks pero crea una conversación por tick; `routing.sessionKey: main` permite continuidad explícita. GitHub conserva una sesión por pull request dentro del repositorio configurado. Redis queue, cron, A2A y el webhook genérico comparten un Workspace por channel. Redis queue y el webhook genérico no inventan una identidad de conversación del payload: la declara `routing.sessionKey`. A2A usa `contextId` o `taskId`; las cabeceras de la petición están disponibles como `event.headers.<nombre-en-minúsculas>`, salvo la cabecera de autenticación del channel, `authorization`, `cookie` y `proxy-authorization`. Un channel que quiera sesiones por llamador lo declara, p. ej. `sessionKey: "{{ event.headers.x-litellm-user-id }}:{{ event.a2a.contextId }}"`; una cabecera ausente rechaza el evento.

Las plantillas opcionales de routing usan `event.*` normalizado y `payload.*`; el prompt añade `internal.*`. No hay namespace de entorno. Para GitLab, `event.gitlab` corresponde a `context.gitlab`; `event.type` al tipo normalizado. MR events y notes de MR se normalizan a los mismos projectId e IID; issues y sus notes proporcionan issueIid. `mergeRequestsOnly: true` descarta issues y notes ajenas a MRs antes de admisión. La identidad del servidor procede de la configuración del channel, no de una URL arbitraria enviada en el payload.

Claves vacías, campos necesarios ausentes o renders inválidos se rechazan; no se cae silenciosamente a otra sesión. Los identificadores son datos opacos: nombres Kubernetes, prefijos y rutas se derivan mediante SHA-256 del conjunto de campos codificado sin ambigüedad. Nunca se interpolan claves originales en rutas del filesystem.

## 3. Admisión, límites y ACK

El orden es deduplicación → capacidad → cola FIFO de sesión. La primera versión usa colas pendientes en RAM. No promete una cola durable ni entrega exactamente una vez. El máximo de cola incluye eventos aceptados internamente pendientes o en ejecución; duplicados no consumen capacidad adicional.

Dos eventos de un Workspace inactivo comparten una única activación. El primero inicia el arranque/restauración; los demás esperan el mismo resultado. Dentro del Workspace, eventos de una misma sesión se ejecutan en orden; sesiones distintas compiten por permisos del Workspace, del channel y del Harness.

| Límite | Alcance |
| --- | --- |
| `limits.maxActiveWorkspaces` | Workspaces arrancando, activos o cerrándose de un ACHAgent; solo desaparecen del contador al desactivarse. Con el límite lleno, la activación espera un hueco liberado hasta el plazo de la invocación y después falla; no se expulsan Workspaces inactivos. |
| `limits.maxConcurrentInvocations` | Ejecuciones de agente activas entre todos sus Workspaces. |
| `workspace.maxConcurrentSessions` | Sesiones ejecutando trabajo simultáneamente en un Workspace; sesiones abiertas inactivas no ocupan ese permiso. |
| `channels[].concurrency` | Ejecuciones activas de ese channel, a través de todos los Workspaces. |
| `limits.maxQueuedTotal` | Eventos admitidos aún no terminados; siempre finito. |
| `limits.maxInvocationSeconds` | Plazo desde la admisión, incluyendo espera, activación, handoff y ejecución. |
| `limits.maxStallSeconds` | Opcional (300 por defecto). Una ejecución sin progreso del motor (ninguna parte de mensaje ni cambio de estado de herramienta nuevos) durante este tiempo se cancela como `failed` con motivo `stalled`. |
| `limits.maxSteps` | Pasos OpenCode por entrada; entero positivo, sin valor ilimitado. |
| `limits.idempotencyWindowSeconds` | Retención mínima del registro de entregas terminadas; no expira una entrega en curso. |
| `limits.maxConcurrentScripts` | Ejecuciones deterministas `webhook-script`, separadas de los procesos OpenCode. |

Webhook HTTP ACK is returned after validation, deduplication and successful admission to the Harness's existing in-memory queue. It does not wait for Workspace activation, handoff, mini-harness Started or an execution result. This applies to both `webhook` and `webhook-script`. A full or closed admission queue returns HTTP 503; invalid input/authentication remains 4xx, and filtered events remain HTTP 200.

```text
Channel -> Harness admission queue -> HTTP 202 to webhook caller
                       |
                       -> activate Workspace -> handoff -> mini-harness
                                                           |
                                   internal Started(invocationId, sessionRef)
```

Once admitted, startup failure, success, failure, cancellation, timeout or pod loss do not revoke the webhook ACK or automatically replay the event. Admission retains the internal Started/completed outcomes and the Harness's existing alarms report startup anomalies. A `webhook-script` does not acquire a Workspace or Session; script spawn remains its internal Started boundary, not its HTTP ACK boundary.

The internal mini-harness Started signal still confirms execution acceptance, after session reservation and preparation; it does not wait for a first token or final result. Queue adapters still acknowledge their source at Started, cron still records startup without replaying old ticks, and A2A retains its existing protocol. These adapters are not changed by the webhook HTTP ACK decision.

Webhook admission is not durable. An acknowledged event still waiting in RAM can be lost if the Harness restarts before execution; the webhook source has already received HTTP 202 and need not redeliver. No durable queue or exactly-once delivery is promised.

Un reintento de `Start` con el mismo invocationId consulta el registro del mini-harness y devuelve el mismo arranque o resultado; no envía otra entrada a OpenCode. Si se perdió la respuesta y no se puede establecer si el trabajo arrancó, se registra resultado desconocido y alarma; no se reproduce ciegamente una posible mutación. La persistencia del registro está limitada por snapshots, como el resto del estado: no se promete deduplicación perfecta tras perder un pod.

En un reinicio del Harness se redescubren sus StatefulSets por UID del ACHAgent y se consultan runs del mini-harness, incluyendo invocationId, channel, estado y plazo original UTC. Con ellos se reconstruyen permisos de ejecuciones activas. El mini-harness hace cumplir ese plazo independientemente del Harness; un reinicio no lo renueva. Retry de Start solo sobre el mismo pod UID: un escritor nuevo con resultado anterior ambiguo produce unknown y alarma, no otro prompt. Pendientes no confirmados en RAM pueden desaparecer; la redelivery depende del origen. Runs confirmados no se reinician. No se agregan reintentos de negocio por encima de la entrega.

## 4. Ejecución y protección frente a bucles

OpenCode con APIs v2 es la única implementación. `engine` conserva opciones, no `type`, versión seleccionable, Pi, binarios configurables ni placements. La imagen fija una release verificada; las referencias usan tags de versión y su digest se registra como evidencia. El número mayor del binario no demuestra qué runner/plugin está activo. El adaptador expone `start(invocation)`, `status(invocationId)`, `cancel(invocationId)`, `snapshot(sessionRef)` y `restore(sessionRef, snapshot)`.

Cada sesión usa un directorio privado de HOME/estado OpenCode separado del Workspace compartido. Scratch también se mantiene fuera del snapshot del Workspace. Esto permite cerrar y copiar una sesión sin copiar la base de datos de otros procesos activos. El formato de estado OpenCode se encapsula en el adaptador y se acompaña de versión del motor y formato del snapshot; una incompatibilidad falla explícitamente, no crea silenciosamente otra conversación.

En v0.1.0 solo se restaura estado de la misma release exacta del motor fijada en la imagen y de un formato de snapshot soportado. El adaptador valida ambas versiones antes de arrancar OpenCode; una discrepancia rechaza la restauración sin lanzar el motor ni crear una sesión de reemplazo. No hay migración automática de bases de datos entre releases. Esta validación pertenece al envelope del runtime, no presupone que OpenCode detecte cualquier incompatibilidad por sí mismo.

El manifest de sesión usa `snapshotVersion: "session-v1"`, `engine.{package,version}`, `agent.{name,namespace,uid}`, `workspaceRef`, `sessionRef`, `nativeSessionId` y `workspacePath`. En la primera imagen el motor es `@opencode/cli@2.0.20`; se compara su versión completa, no solo el major. Las referencias, identidad y path estable se contrastan con el destino esperado, no se adoptan desde un archive no validado. La copia consistente incluye el registro del mini-harness junto al HOME/XDG privado, sin credenciales/capabilities de invocación; checksum y tamaño del archive se verifican con el recibo de Storage. `session-v1` es el formato de snapshot, distinto de `schemaVersion: "workspace-v1"` de la configuración.

`limits.maxSteps` se traduce a `agents.<managed-agent>.steps`. El último paso es de finalización con `toolChoice: none`; llamadas devueltas indebidamente deben rechazarse sin ejecutar la herramienta. La presencia de definiciones de herramientas en el request no equivale a permiso de ejecución. Nueva entrada reinicia el contador. El Harness entrega los eventos uno a uno, sin steering adicional que resetee el contador de una misma invocación. Esta garantía requiere la prueba del runner de la release elegida, no basta con el schema o la introspección del agente.

Los pasos no equivalen a llamadas de herramientas ni a euros. Puede haber varias herramientas por paso, llamadas adicionales de compactación y reintentos del transporte. No se expone `maxToolCalls` como límite nativo ni se heredan `session.maxTokens`/`overflow`. Se configura `engine.compaction.auto`, `keep.tokens` y `buffer` directamente. `engine.denyTools` lista herramientas nativas de OpenCode (p. ej. `question`, `execute`, `websearch`, `webfetch`) que el plugin gestionado deniega al agente; las herramientas MCP se gobiernan en `ach.capability.filter.exclude.tools`.

El plazo es una protección independiente. Al vencer, el Harness ordena cancelación y el mini-harness termina el grupo de procesos si no se detiene en 10 segundos. La cancelación de un proceso obliga a restaurar o relanzar esa sesión antes del siguiente evento. No se concede un nuevo plazo por reintento. Las compactaciones también quedan dentro del plazo. El presupuesto de ACH/LiteLLM limita gasto acumulado; maxSteps no se presenta como presupuesto monetario. The model facade authenticates the Workspace, not the invocation: it does not cut off model calls at the deadline, and a live Workspace keeps model access between invocations; the deadline is enforced by process cancellation.

El modo de ejecución es exclusivamente el servidor privado `serve --stdio`, no el servicio compartido ni TUI. En la release fijada, ese entrypoint no ejecuta el updater del TUI; `update: "disable"` en un documento de contenido no se presenta como prueba de que el updater de otro modo esté desactivado. Se verifica el bloqueo de catálogo remoto y el efecto de `snapshots: false` frente a un control positivo. `lsp: false` se conserva como ajuste interno conservador y se verifica su resolución, pero v0.1.0 no anuncia soporte LSP ni una desactivación conductual comprobada de LSP. These engine settings do not establish cluster isolation; infrastructure remains operator-controlled, and this scope requires no admission guards or new CNI project. La inspección de configuración consulta `/api/config` del propio servidor privado; el CLI `debug config`, que puede iniciar/reutilizar un servicio separado, no acredita la configuración de ese proceso.

La revisión inicial de [runner dev](https://github.com/anomalyco/opencode/blob/9b4882db54627f2656a6990daafa412f9f3c7c82/packages/core/src/session/runner/llm.ts) y sus tests no constituye evidencia de una release adoptada. El spike contrasta la publicación real [v2.0.20](https://github.com/anomalyco/opencode/tree/v2.0.20), distribuida como `@opencode/cli@2.0.20`, antes de construir sobre sus APIs. `opencode-ai@1.18.33` pertenece a la línea anterior y no puede certificar este contrato.

## 5. Configuración y credenciales

`spec.ach` agrupa `baseUrl`, `environment`, `identity.secretRef` y `capability.filter.exclude.{tools,mcpServers,skills}`. La credencial define el entorno autorizado; un `environment` explícito distinto del hidratado es error. La URL puede heredarse del perfil y, por último, del operador. No existe modo sin ACH para este producto.

El operador mezcla defaults del perfil con valores del agente: escalares explícitos ganan, objetos se resuelven por campo, listas/mapas explícitos reemplazan. `false`, `0` permitido y lista vacía son valores explícitos, no ausencia. `spec.env` se mezcla por nombre sobre el perfil; cada entrada sustituye el EnvVar completo. `ach.identity` es del agente, no una credencial compartida implícita del perfil.

El operador monta un JSON versionado `schemaVersion: "workspace-v1"` en `/etc/ach-runtime/config.json`. Incluye `agent.{name,namespace,uid}`, configVersion SHA-256 de configuración canónica, campos de comportamiento resueltos, configuración de infraestructura resuelta y solo nombres/referencias de credenciales. El schema nuevo se genera en este repo y ACH lo vendoriza con test de drift. No se modifica el schema del runtime viejo ni se etiqueta la nueva forma como su versión `"1"`.

`configVersion` is 64 lowercase hexadecimal characters, with no prefix: SHA-256 of the RFC 8785/JCS UTF-8 bytes of the complete wire object, excluding only its top-level `configVersion` member. Arrays retain their order, Unicode is not normalized, and accepted nulls and empty values remain present. The producer resolves defaults before hashing; the consumer verifies that original wire object before applying internal defaults or changing presence. Keys sort by UTF-16 code units, not native Go/Python map order. Numbers must be finite binary64 values within ±9007199254740991; out-of-range numbers and NaN/Infinity are rejected. Ingress uses ordinary `encoding/json` or `json.loads`: standard-library parsing rejects malformed JSON and trailing data, while the existing config boundary rejects non-object roots and unsupported numbers. Duplicate object keys use the last value. Raw Unicode/surrogate handling follows the standard parsers and pinned JCS libraries; there is no custom scanner or cross-language duplicate/raw-Unicode rejection guarantee. The shared byte/digest corpus verifies exact positive compatibility and the shared numeric/trailing-data rejections. Secret-rotation rollout hashes are independent and never enter this object.

SecretRefs del CR se traducen a variables privadas del pod de control. El JSON no contiene valores de secretos. `spec.env` vive en Channels + Harness; `engine.forwardEnv` selecciona nombres para procesos del pod de ejecución y `handoff.forwardEnv` selecciona nombres para scripts del Harness. Solo se reservan los nombres propiedad del runtime o necesarios para su arranque: `HOME`, los espacios `ACH_`, `XDG_` y `OPENCODE_CONFIG`, y los nombres de servidor/bootstrap OpenCode existentes. La selección explícita del operador puede incluir nombres AWS/Azure/GCP, TLS/CA, loader/interpreter o proxy. En v1 solo valores literales pueden llegar por `engine.forwardEnv`; SecretRefs se usan en Harness o fachadas. Seleccionar un nombre inexistente produce error de configuración.

El mini-harness recibe una proyección pública: modelo, permisos, prompts, URLs de fachadas, solo los valores literales que el operador seleccionó explícitamente para `engine.forwardEnv`, hooks sin secretos, límites y referencias de ejecución. No hereda las credenciales de ACH o Storage: `ek_`, credenciales S3 del runtime, PAT GitLab y la clave privada de autenticación del control nunca se exportan implícitamente. El operador puede seleccionar explícitamente valores de entorno AWS/Azure/GCP para el motor. El canal de control usa credenciales internas acotadas al pod; no son credenciales de los servicios externos. No se exponen en configuración OpenCode ni logs.

El Harness conserva hidratación, identidad, scopes y secretos. Se reutiliza la conversión de modelos y transforms de [alitellm-auth](../../../alitellm-auth/clients/opencode/index.mjs) mediante un plugin de configuración v2 separado del login OAuth. El plugin consume un documento público suministrado por el mini-harness: providers/modelos autorizados, capacidades, costes, límites de contexto/salida, MCP con URLs de fachada y skills hidratadas. No consulta el endpoint de configuración de usuarios con un ek_. No registra OAuth ni escribe tokens. Configuración esencial ausente o inválida impide iniciar ejecución; no hay fallback silencioso a providers externos.

Configuración y plugins del checkout no pueden sustituir el modelo gestionado, sus URLs, permisos o límites. El spike valida una forma efectiva de aislar la configuración administrada usando las APIs/flags de la versión fijada. El plugin registra configuración; la autorización y revocación se aplican además en las fachadas.

Cada invocación usa una configVersion fija. La siguiente ejecución usa la nueva configuración disponible en el Harness; cambios que exijan relanzar el proceso se aplican entre ejecuciones. Losing the Workspace (closed, replaced, or no longer owned by this Harness) blocks its facade access on the next request; capability-filter changes take effect when the Harness loads the new configuration, not mid-run. Cambiar una plantilla de routing afecta futuros eventos, no renombra Workspaces existentes. Un rollout del pod de control puede perder pendientes en RAM; no se promete hot-reload de un ConfigMap montado con subPath. Los StatefulSets de ejecución usan OnDelete: cambiar su plantilla no elimina un escritor activo. The Harness stamps each Workspace StatefulSet with a hash of its rendered pod template (`runtime.ach.ackstorm.ai/template-hash`); when an ensure finds an idle Workspace (scaled to zero by close) whose hash differs or is missing, it deletes that StatefulSet (UID-preconditioned) and creates a fresh one, then restores from Storage. A Workspace with a live replica keeps its old template until it is next closed. El mini-harness informa versión de protocolo, motor y snapshot al conectarse; incompatibilidad impide Start y se resuelve en una desactivación segura.

## 6. Handoff y hooks

`channels[].handoff` es un script estático del Harness con scope `event` (único valor admitido; `session` se rechaza porque el destino es un directorio compartido por todas las sesiones del Workspace — los archivos por sesión usan `workspaceKey: session`), destino relativo, timeout y forwardEnv propio. Se ejecuta en cada evento. El script trabaja en staging vacío y recibe variables validadas `ACH_WORKSPACE_KEY`, `ACH_SESSION_KEY`, `ACH_EVENT_ID`, `ACH_CHANNEL`, `ACH_HANDOFF_DIR`, y `ACH_EVENT_*` específicas del adapter. No se renderiza código shell desde el payload.

Su salida se importa solo en el destino declarado dentro del Workspace. No se copia el entorno ni se reemplaza todo el Workspace. Se rechazan escapes de rutas, archivos especiales y archivos que excedan los límites de importación. A handoff failure prevents internal Started, but does not revoke an already admitted webhook's HTTP ACK. No se hacen checkouts o pushes implícitos fuera de los scripts declarados.

Un handoff que reemplaza un directorio compartido requiere exclusión de todos los procesos que puedan usarlo. V1 exige `workspace.maxConcurrentSessions: 1` si hay handoff con destino compartido; no basta con bloquear dos importaciones entre sí. La revisión de PR utiliza esta restricción.

Los hooks de agente `sessionStart`, `sessionRestore` y `sessionSuspend` se ejecutan en mini-harness con solo el entorno autorizado de engine. Start se ejecuta una vez al crear sesión; Restore al restaurar; Suspend antes del snapshot/cierre. Start/Restore fallidos impiden el arranque de esa invocación. Suspend fallido se registra y alarma, pero no autoriza perder el snapshot ni bloquea indefinidamente el cierre. No se reejecuta inicialización como consecuencia de restaurar.

Agent lifecycle hooks, all in the mini-harness (`hooks.*`, `{script, timeoutSeconds}`, no secrets):

| Hook | Runs | cwd | Failure |
|---|---|---|---|
| `workspaceStart` | Fresh, reactivated or recovered pod, after the workspace restore, before the first session. `ACH_WORKSPACE_RESTORED=true\|false`. | `/workspace` | Anomaly `workspace_start_failed`; never blocks the workspace (gate runs in `sessionStart`). |
| `sessionStart` | First engine send of a session (cron: every tick). | session HOME | See the gate below. |
| `sessionRestore` | Session restored from a snapshot. | session HOME | The invocation does not start. |
| `sessionSuspend` | Before a session snapshot. | session HOME | Recorded and alarmed; never blocks the snapshot. |
| `workspaceStop` | Clean idle close, before the workspace snapshot (also without persistence). | `/workspace` | Anomaly `workspace_stop_failed`; never blocks the close. |

`sessionStart` is the run gate: exit `0` continues and non-empty stdout (at most 64 KiB) is appended to the prompt after a blank line; exit `78` ends the invocation `skipped` without calling the engine; any other exit or a timeout ends it `failed` (`sessionStart exited N: <stderr tail>`). Skipped and failed gates publish Started first, so the run is recorded, and the session is not marked started, so the gate runs again on its next invocation. Hook scripts get the approved engine env plus `ACH_WORKSPACE` and `ACH_MCP_URLS` (the pod's MCP facade URLs, used by `ach-mcp call <server> <tool> '<json>'` to call tools as the agent); `sessionStart` also gets `ACH_CHANNEL`, `ACH_INVOCATION_ID`, `ACH_SESSION_REF`, `ACH_WORKSPACE_KEY` and `ACH_SESSION_KEY` (the routed keys; the other hooks get no keys). `webhook-script` runs without a workspace or session and sets no `ACH_SESSION_KEY`. Workspace hooks may call `ach-mcp` while the workspace is not yet (start) or no longer (stop) serving runs. `workspaceStop` may run again when a close is retried, so it must be idempotent; a close whose snapshot fails transiently returns the workspace to service without re-running `workspaceStart`. Workspace hooks do not run on unclean pod death.

## 7. Persistencia y recuperación

`workspace.session.idleTimeoutSeconds` cuenta desde que termina el trabajo de una sesión y no queda evento pendiente para ella. Mientras ejecuta no expira por inactividad. Una sesión abierta inactiva sigue perteneciendo al Workspace. Al cerrar: detener → snapshot consistente → persistir → liberar proceso/estado local. Restaurar no equivale a crear.

`workspace.idleTimeoutSeconds` comienza cuando no hay sesiones activas/abiertas ni trabajo pendiente. Al vencer: cerrar admisión local → congelar escrituras → archivar Workspace → persistir → desactivar StatefulSet a cero. Un evento llegado durante ese archivado espera; al completarse reactiva el Workspace. El tiempo de apagado Kubernetes debe permitir `workspace.shutdownTimeoutSeconds` y el margen de cancelación.

Si falla un guardado, se conserva la copia local, se reintenta con el mismo snapshotId y se genera alarma. Reintentos con backoff exponencial de 1 a 30 segundos, acotados por cada intento de cierre; al agotar shutdownTimeout el Workspace permanece disponible para recuperación operativa y marcado PersistenceFailed, sin borrado de su única copia. Un apagado forzado puede perder trabajo: no se promete supervivencia de una copia solo local.

Si health falla, se elimina el pod y se crea otro desde la última copia persistida del Workspace. Se usa borrado normal y confirmación de terminación antes de restaurar otro escritor; un nodo inaccesible no habilita un force-delete automático que pueda dejarlo escribiendo. Los mecanismos habituales de Kubernetes/nodo resuelven ese caso, con alarma mientras no pueda completarse. Trabajo posterior a la última copia puede perderse; runs ya confirmados no se vuelven a ejecutar automáticamente.

Al arrancar, el Harness redescubre los pods de Workspace en ejecución. Si el registro de Storage anota como escritor otro pod (`writer_pod_uid`), Kubernetes recreó ese pod fuera del Harness —p. ej. pérdida de nodo mientras el control estaba caído— y su `/workspace` está vacío: el Harness lo restaura antes de usarlo, igual que en una activación nueva (restaurar, `workspaceStart`; el nuevo escritor sube el epoch, así que el lease del pod muerto no puede comprometer). Si el registro ya anota ese pod, o no existe registro, o la persistencia del Workspace está desactivada, no se restaura (sin persistencia solo se pierde `workspaceStart`).

Si no existe ninguna copia, un Workspace nunca persistido comienza vacío. Si había copia registrada pero no se puede recuperar o está corrupta, se falla y alarma; no se sustituye por vacío. Una sesión archivada se restaura explícitamente; si su snapshot se perdió, se informa, no se finge continuidad.

`persistence.enabled: false` significa estado volátil de ese dominio y ausencia de restauración tras cierre. La persistencia de sesión no garantiza conservar sus archivos de trabajo si Workspace persistence es false. RetentionDays se cuenta desde la última copia comprometida; el snapshot de un dominio no referencia una copia incompleta de otro. Snapshots de Workspace incluyen archivos de trabajo, no Scratch, secretos, Artifacts publicados ni HOME privado de sesiones.

Storage expone operaciones lógicas `restore(ref)`, `persist(ref, snapshotId, stream)`, `create_artifact(...)` y `read_artifact(artifactId)`. Los refs incluyen ACHAgent UID y Workspace/session según dominio. Persist devuelve recibo con checksum y tamaño solo tras durabilidad. El puntero a latest se actualiza al final y no retrocede por reintentos antiguos; cada dominio serializa sus commits. La caché es optimización, no una segunda copia activa escritora.

Bucket, region, endpointUrl and prefix (default `ach`) are operator-global S3 settings, not per-agent buckets or a separate Storage service. They reach the control/Harness pod through private `ACH_STORAGE_S3_BUCKET`, `ACH_STORAGE_S3_REGION`, `ACH_STORAGE_S3_ENDPOINT_URL` and `ACH_STORAGE_S3_PREFIX` environment variables, never config.json, PublicConfig or execution pods. Optional `credentialsSecretName` selects an existing namespace-local Secret (`AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` required, `AWS_SESSION_TOKEN` optional), injected through SecretKeyRefs; otherwise the Harness uses the SDK's ordinary ambient credential chain. ACH does not assume IAM roles or issue AWS credentials per agent. Requested session/Workspace persistence or Artifacts with an empty bucket blocks reconciliation with StorageUnavailable; there is no volatile fallback. Actual SDK/backend operation errors remain failures, never degraded success. The Harness calls logical operations for its ACHAgent; the mini-harness transfers streams over its authenticated channel.

## 8. Artifacts

`artifacts.enabled` incorpora `create_artifact` y `read_artifact`; no hay lista redundante de tools. MaxArtifactBytes y retentionDays son políticas del agente. Los refs y ACL se inyectan por el Harness, nunca se aceptan como selección libre de otro ACHAgent desde argumentos del modelo.

Crear sigue verificar → persistir → registrar → publicar. La herramienta devuelve un artifactId y referencia publicable solo después de esos pasos. Metadatos incluyen ACHAgent UID, Workspace, Session/run productor, MIME, nombre, tamaño, checksum y fechas. Una clave idempotente estable permite reintentar la misma publicación; distinto contenido con esa clave da conflicto. Un fallo de registro no publica URL y la copia huérfana queda para limpieza.

El Artifact no depende del pod ni de que el Workspace siga montado. Los resultados publicados son versiones inmutables. Las referencias de descarga resuelven autorización y expiración; no se publican paths locales, credenciales ni URLs que aparenten una retención superior a la real.

## 9. Resultados y observabilidad

Cada run pasa de queued a starting, running y después completed, failed o cancelled; `skipped` is the terminal state of a run that `sessionStart` decided not to start (exit 78). `unknown` expresa pérdida de observación tras una posible ejecución, no un permiso para repetir. Timeout produce failed con razón timeout. Started is the internal execution-acceptance boundary; webhook HTTP ACK occurs earlier, at Harness admission. Completed no significa que el agente haya decidido publicar un comentario o Artifact.

El mini-harness expone estado por invocationId y stream con secuencia de eventos. El Harness registra actor, channel, referencias, configVersion, duración, tokens y resultado. El origen obtiene resultados mediante sus herramientas/canal; no se inventa un JSON terminal obligatorio para todo agente. Un esquema de resultado específico puede añadirse después por caso de uso.

Logs y métricas incluyen fallos de snapshot, restauración, arranque, respuesta de arranque perdida, pod unhealthy, configuración inválida, límites y publicación de Artifacts. Contadores Prometheus no usan claves de usuario como labels; logs correlacionados sí usan identificadores seguros. No hay cuerpos secretos ni URLs de fachada autenticadas en logs. Alerta sobre PersistenceFailed, RestoreFailed, StartOutcomeUnknown y fallos repetidos de pod; reglas Prometheus de instalación, no mensajes enviados por el runtime.

Readiness del pod de control exige configuración/hidratación válidas y adapters escuchando; no exige que haya un Workspace activo. Readiness del pod de ejecución exige mini-harness configurado y restauración terminada. Health de un Workspace no depende de haber arrancado un proceso OpenCode; fallos de una invocación no deben reiniciar el pod de control.

## 10. Inventario del contrato anterior

Esta tabla evita que una ausencia en el ejemplo se interprete como decisión accidental de eliminación.

| Superficie | Tratamiento nuevo |
| --- | --- |
| Identidad, entorno y filtros ACH | Bajo `ach`; conservar exclusión de tools/MCP/skills. |
| Modelo, params, thinking | Conservar; traducción efectiva a settings de modelo/provider v2, verificada en spike. |
| Prompt text/file/ach, append/replace | Conservar; file/ach se resuelven en hidratación. |
| Engine forwardEnv/startup | Conservar; literal y allowlist explícita. |
| MaxSteps | Conservar como límite obligatorio por invocación. |
| MaxToolCalls, maxTokens, overflow, terminalOutputRetries | No se exponen en v1; compaction nativa y resultados explícitos. |
| Engine Pi/type/home/workDir/idleTtl | Retirar; único motor, rutas internas y timeouts nuevos. |
| Cost.source | Retirar selector; registrar uso y procedencia de cualquier coste, no confundir estimación con facturación. |
| Channels webhook/webhook-script/cron/queue/a2a | Conservar superficies como tareas posteriores al recorrido PR review; el runtime rechaza tipos aún no implementados. |
| Session none/auto/custom | Defaults de identidad internos del adapter; routing opcional con overrides independientes. Clave estable permite resume; por entrega/tick crea conversación independiente. |
| Handoff y hooks | Conservar con Workspace compartido y Restore explícito. |
| Ach-memory | Conservar fachada, exclusión del MCP bruto y scope fijado. |
| Codemem | Fuera del primer recorrido; no implementar por arrastre. Decisión de inclusión separada. |
| MCP adicional local/remote | Soporte adicional planificado mediante frontera del Harness; no conservar passthrough secreto al motor. |
| Egress autenticado HTTP | Planificado para operaciones que requieren credencial fuera de MCP; secretos permanecen en Harness. |
| PVC/mountPath/health configurables | Infraestructura del operador/perfil; puertos y rutas del runtime se fijan internamente. |
| Placement/warmPool/sandboxclaim | Retirar; StatefulSet por Workspace. |
| Profile/env/recursos/scheduling/red/podTemplate | Conservar infraestructura, separando control y ejecución. Overlays no cambian réplica, identidad, scopes ni mounts protegidos. |
| Status/gatewayURL/schema/hidratación/telemetría | Conservar con semántica nueva; fallos explícitos y schema independiente. |

El perfil define imágenes y recursos separados para control y ejecución, imagePullSecrets, nodeSelector, tolerations, policies de red, volúmenes activos y grace period. El operador proporciona Storage global; no se define un bucket diferente en cada ACHAgent. El contrato de defaults/overlays se prueba en ACH, incluyendo protección de los campos que sostienen aislamiento y topología.

## 11. Interfaz de infraestructura fijada para workspace-v1

La implementación usa un contenedor Channels + Harness en el StatefulSet de control de una réplica por ACHAgent. Storage access is global, operator-configured infrastructure (D1): when persistence or artifacts are requested without a configured backend, the corresponding operations fail explicitly. This is not authority for a compulsory auth/Storage service when persistence and artifacts are disabled — no such service is required in that case.

| Rol | Comando | Puertos |
| --- | --- | --- |
| Control | `python -m ach_runtime control --config /etc/ach-runtime/config.json` | 8080 Channels/health; 8081 HTTP(S) for Workspace control and facades (current Service endpoint, not a mandated TLS/CA channel). |
| Ejecución | `python -m ach_runtime execution --config /etc/ach-runtime/bootstrap.json` | 8080 internal mini-harness API through the Workspace headless Service; no public ingress or Gateway. OpenCode endpoints remain local to the execution pod. |

These are the runtime interfaces; documenting them does not prove successful cluster integration. Health/readiness use /healthz and /readyz; Harness metrics use /metrics/.

The operator mounts the public ConfigMap ach-execution-<uid> containing bootstrap.json. This separate, strict, secret-free document contains bootstrapVersion: workspace-v1, agent.{name,namespace,uid}, controlEndpoint and facadeEndpoint; it contains no model, prompts, engine.env or invocation configVersion. The Harness delivers PublicConfig through the mini-harness /configure API and starts invocations through authenticated application calls, reusing the existing signed mini bearer and observed pod-UID binding. The execution pod calls Harness model/MCP facades through the existing HMAC-protected URLs. No projected-token, CA, audience, reverse-connection enrollment or additional auth service is introduced. Configuration changes apply between executions without giving the Harness ConfigMap write permission. The execution container environment contains pod/Workspace identity fieldRefs and the public ACH_SANDBOX_VERIFY_KEY used to verify Harness signatures; this public Ed25519 key is not an ACH credential or private signing key. Selected engine.env literals are delivered to the OpenCode subprocess, not added to the Kubernetes execution template.

The control name is `agent-<agent name>`; an agent name longer than 46 characters, or containing a dot, becomes its first 37 characters with `.` turned into `-` and any trailing `-` removed, then `-`, then the first 8 hex characters of SHA-256 of the full name (at most 52 characters). The control pod is selected by the labels `ach.ackstorm.ai/agent=<agent name>` plus `ach.ackstorm.ai/component=control`; a workspace pod never carries `ach.ackstorm.ai/component=control`. Workspace names are `agent-<name part up to 25>-<20 hex>`, at most 52 characters, and runtime-owned: the operator never parses or validates them. They share the control name's `agent-` stem so an agent's execution pods sort next to its control pod; the ACHAgent CRD rejects agent names ending in `-<20 hex>` so a control name never equals a Workspace name. Los nombres protegidos de ejecución son `agent-<agente>-<workspace20hex>`. UID es el UUID canónico minúsculo del ACHAgent; `<agente>` se deriva de `agent.name` (el metadata.name DNS-1123 del ACHAgent, con los puntos sustituidos por `-` porque el Service exige una etiqueta DNS-1035): si tiene 25 caracteres o menos se usa tal cual; si no, sus 16 primeros caracteres sin `-` final, `-` y los 8 primeros hex del SHA-256 del nombre completo. Un nombre vacío o no DNS-1123 se rechaza. La unicidad la da `<workspace20hex>`, cuyo digest ya incluye namespace y UID; el nombre del agente nunca entra en las comprobaciones de propiedad, que usan siempre el UID completo (label `runtime.ach.ackstorm.ai/agent-uid`, ownerReferences y anotaciones). Un StatefulSet propio con un nombre anterior (`ach-ws-<uid24hex>-<workspace20hex>` o `ach-ws-<agente hasta 24>-<workspace20hex>`) no se adopta ni se borra: el arranque registra un conflicto, mantiene reservado y fallido ese Workspace y continúa; borrarlo libera el hueco en el siguiente arranque. El nombre del StatefulSet tiene como máximo 52 caracteres, de modo que el sufijo `-<hash de revisión>` (hasta 10) que el controlador añade al label `controller-revision-hash` cabe en los 63 permitidos, y el pod con ordinal `-0`, 54. Los labels usan el prefijo `runtime.ach.ackstorm.ai/`, distinto del runtime antiguo; la única excepción es `ach.ackstorm.ai/agent=<agent.name>` en la plantilla de pod de ejecución (el mismo label que llevan los pods de control), que es solo informativo, no entra en el selector ni en las comprobaciones de propiedad y se omite si el nombre no es un valor de label válido (más de 63 caracteres). El sufijo de Workspace abrevia su referencia SHA-256 completa a 80 bits; una colisión o vínculo de propiedad diferente produce conflicto, nunca adopción por nombre. Son nombres de infraestructura, no claves de usuario ni una forma de migrar Workspaces.

La configuración privada incluye `infrastructure.control.{serviceAccount}` y `infrastructure.execution.{image,serviceAccount,controlEndpoint,facadeEndpoint,resources,ephemeralStorage,terminationGracePeriodSeconds,imagePullSecrets,nodeSelector,tolerations}`. Los endpoints son Service DNS del clúster (`http://agent-<agent name>.<namespace>.svc:8081`, y su ruta `/facades`); se valida que sean un endpoint http(s) con host, no un esquema obligatorio. La proyección pública excluye por completo infrastructure.control. The Storage broker, its client TLS and the execution pod's projected-token authentication (formerly `infrastructure.control.broker`, `infrastructure.control.tls` and `infrastructure.execution.auth`) are excluded from this schema, not relocated to D1/D2: D1 is globally configured Storage access with an explicit error when a requested backend is missing, and D2 is minimal reuse of the existing signed mini-harness bearer and HMAC facade authentication for authenticated application calls — neither reintroduces a broker, client TLS or projected-token/CA/audience scheme.

El JSON renderizado usa camelCase, distinto del YAML de autoría cuando resuelve credenciales: `ach.identity: {env: "ACH_SECRET_IDENTITY"}`, webhook/A2A `auth.env` y handoff/script `secretEnv.<nombre>: {env: "ACH_SECRET_..."}` contienen solo aliases privados. Handoff/script `env` contiene literales seleccionados. CR `engine.forwardEnv` se resuelve a JSON `engine.env: {nombre: valorLiteral}`; no se copian SecretRefs ni se realiza expansión contra el entorno privado. Hooks solo heredan ese mapa. El renderer comprueba procedencia y nombres ausentes antes de producir JSON; el runtime rechaza valores no string y nombres reservados del mapa. Campos de políticas renderizados tienen valores resueltos; `maxSteps` sigue siendo requerido positivo. La configuración canónica usada para configVersion excluye solo configVersion, no UID, infraestructura ni políticas. El fixture de render y el schema generado son el acuerdo compartido entre Go y Python.

Control y ejecución usan SAs distintos. La SA de ejecución es `ach-execution-<uid>`, derivada del UID y verificada por este schema. La SA de control es `ach-harness-<uid>` salvo que el AgentProfile fije una SA estable preexistente (`controlServiceAccountName`); el schema solo exige que sea una etiqueta DNS-1123. The execution pod's projected-token design, the Storage broker's TokenReview verification and the Harness presenting a token to the broker (issuer/audience/admission for those authenticated calls) are excluded entirely, not a D1/D2-owned integration limit to implement later; they are not a requirement of this resolved configuration nor of the public/bootstrap boundary described above. D1/D2 define the actual remaining limit: globally configured Storage access that fails explicitly without a backend (D1), and minimal reuse of the existing signed bearer/HMAC facade authentication for application calls (D2) — no new enrollment, capability or admission framework.

Isolating agents in a shared namespace relies on the Harness's own Kubernetes identity as the StatefulSet/Pod creator: The Harness is the sole Kubernetes creator for Workspace StatefulSets/Pods, using standard namespace-scoped creator RBAC bound only to `ach-harness-<uid>`. The execution SA `ach-execution-<uid>` has no creator RBAC. Harness RBAC verbs are exactly: `apps/statefulsets` get, list, create, patch, delete; `pods` get, list, delete; `services` get, create. The runtime issues no watch, no update/PUT, no Service list/patch/delete and no scale-subresource call (it scales by patching `spec.replicas`); the owner reference it sets is `controller: true` without `blockOwnerDeletion`. Harness permissions are limited to these resources and operations, with no Secrets/RBAC CRUD, pod exec/attach/ephemeralcontainers or access to other namespaces. There is no fail-closed admission-guard webhook, no final-Pod admission validation and no Kubernetes admission-policy lifecycle in this boundary — that mutating/validating-webhook design is excluded, not a near-term addition. Workspace ownership (the UID/Workspace-derived protected names described above) is not migratable between ACHAgents or clusters; a name collision or a different ownership reference is always a conflict, never an adoption. This is the schema's explicit integration limit: it defines the resolved configuration and naming, not a cluster-enforced isolation guarantee beyond standard RBAC.

Storage conserva un registro CAS por dominio con writerEpoch, writerPodUID y última copia comprometida opcional. Adquirir un escritor nuevo conserva la copia anterior; no convierte un dominio existente en vacío. Persistir sube bytes inmutables y cambia latest mediante ETag/If-Match del predecesor; primer registro usa If-None-Match. Un escritor obsoleto no relee latest para autorizar su snapshot antiguo bajo otra época. Recibos llevan época, generación, ETag, checksum y tamaño. El backend S3 compatible debe probar estas condiciones reales.

Los Workspaces usan almacenamiento local efímero acotado: conservar la copia local implica conservar ese pod, no supervivencia a pérdida del nodo. PersistenceFailed mantiene el pod y su slot, permite recuperación y continúa reintentos en background; scale-to-zero ocurre solo tras el cierre comprometido. Los objetos inactivos y blobs huérfanos requieren limpieza sin eliminar una copia comprometida vigente.

Publishing this contract does not establish cluster readiness or isolation. The operator controls infrastructure and explicitly selects the engine environment, including cloud credential names; this scope imposes no blanket cloud-identity ban, mandatory metadata/PodIdentity denial or new CNI project. Admission guards are excluded. Harness-held ACH/runtime Storage credentials remain separate from selected engine values, and unenforced YAML does not establish isolation.
