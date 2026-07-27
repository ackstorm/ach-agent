# CONTRACT_v3 / harness-spec — ADDENDUM: `prompt.system` source (text | file | ach) + `.ach-state` root

**Status:** ADDENDUM — 2026-06-30. **BREAKING — coordinate-first.** This amends `CONTRACT_v3.md §2`
(the `prompt` block) and pins the hydration on-disk layout left ambiguous between `CONTRACT_v3.md §3`
("under `workDir`/`mountPath`") and `CONTRACT_v3-ADDENDUM-hydration.md §2` ("under `<workDir>`").
It MUST land in lockstep across **ach-runtime** (the operator that renders `prompt.system`) and
**ach-agent** (the harness that reads it). Neither side may ship alone — `prompt.system` is a
contract-reserved field rendered by the operator.

**Why:** today `prompt.system` is an inline string only. Agents want the persona to come from a
**hydrated prompt artifact** (managed centrally in ACH, fetched at boot) instead of being inlined
into every rendered config. The prompt `claude-plugins-ackstorm-prompt1` is already fetched to disk
at hydration — the only gap is letting `prompt.system` *point* at it.

---

## 1. The breaking change — `prompt.system` becomes a typed source (no shorthand)

`prompt.system` is **no longer a string**. It is a discriminated object; `type` is **required**.
The plain-string shorthand is **removed** — every rendered config and example must migrate.

```jsonc
// text — inline persona (replaces the old bare string)
"prompt": { "system": { "type": "text", "text": "…agent persona (markdown ok)…" }, "compose": "append" },

// file — persona from a hydrated prompt file, addressed by path under .ach-state
"prompt": { "system": { "type": "file", "file": "prompts/<name>/<file>.md" }, "compose": "append" },

// ach — persona from a hydrated prompt addressed by NAME; the harness resolves the file
// (the prompt dir's sole file, or the optional `file` subpath). Preferred: the operator
// names the prompt, not its on-disk path.
"prompt": { "system": { "type": "ach", "ach": "<prompt-name>" }, "compose": "append" },
"prompt": { "system": { "type": "ach", "ach": "<prompt-name>", "file": "<subpath>.md" }, "compose": "append" },
```

YAML (local hand-authored form):

```yaml
# text
prompt:
  system:
    type: text
    text: "You are a concise software-engineering assistant."
  compose: append

# file
prompt:
  system:
    type: file
    file: prompts/claude-plugins-ackstorm-prompt1/example1.md   # relative to <home>/.ach-state
  compose: append
```

**Removed:** `"prompt": { "system": "…string…" }`. A bare string now fails validation
(`extra=forbid` + discriminator). This is the lockstep break: ach-runtime MUST render the object
form, ach-agent MUST reject the string form, on the same release.

`compose` is unchanged — still contract-reserved (`"append"`, accepted, not yet executed).

---

## 2. The `.ach-state` hydration root — pinned

Hydration writes three kinds with different consumers. They are consolidated under **one root in
the engine HOME**:

```
<home>/.ach-state/prompts/<name>/…       # harness-consumed (persona layering); agent does not need it
<home>/.ach-state/artifacts/<name>/…     # agent working material (e.g. ach-cr-samples)
<home>/.config/opencode/skills/<name>/   # UNCHANGED — opencode scans this exact path; not movable
```

- **`ACH_STATE = <home>/.ach-state`.** `home` resolves as today (`engine.home`, else
  `<mountPath>/home` when persistence enabled, else `/tmp/ach-home`).
- **Why HOME, not workDir:** `workDir` is the agent's mutable cwd (it clones repos, writes scratch).
  Hydrated state is read-only — keep it out of the churn. HOME already holds skills, so it is
  already the hydration home. The `.`-prefix keeps it out of the agent's workspace `ls`.
- **ek-hygiene:** the on-disk `ek_` (ADDENDUM-hydration §4) lives under `mountPath` for the
  forwarder. Keeping `.ach-state` under HOME (distinct from the ek material) keeps agent-reachable
  state and secret material from co-mingling.
- **Agent shell access to artifacts:** if `workDir != home`, the harness symlinks
  `<workDir>/.ach-state` → `<home>/.ach-state` so the agent reaches artifacts at one stable path.

This **supersedes** the prompts/artifacts location in `CONTRACT_v3.md §3` and
`CONTRACT_v3-ADDENDUM-hydration.md §2` (was `<mountPath>/{prompts,artifacts}` /
"under `<workDir>`"). Skills are unchanged.

---

## 3. Resolution + security (harness)

For `{ "type": "file", "file": F }`:

1. Resolve `F` relative to `ACH_STATE` (= `<home>/.ach-state`).
2. **Reject** if `F` is absolute, or the resolved real path escapes `ACH_STATE` (any `..`
   traversal). This is a hard validation error, not a warning — a `file:` is operator/agent-spoofable
   and must not be able to read arbitrary disk (e.g. secret files) into the system prompt.
3. **Error** (startup failure within `startupTimeoutSeconds`) if the file is missing — a persona the
   operator declared but hydration did not deliver is a misconfiguration, not a fail-open case.
4. Read its bytes → same materialization as today: written to
   `<home>/.config/opencode/personality/system_prompt.txt`, referenced via opencode.json
   `instructions: [...]` (append mode). One materialization path for all forms.

For `{ "type": "ach", "ach": A, "file"?: F }`:

1. Resolve the prompt dir `<ACH_STATE>/prompts/A` (`A` rejected as absolute/`..` at load).
2. **Error** (startup failure) if that dir is not hydrated (not a directory under `ACH_STATE`).
3. Pick the file: if `F` is given, `<prompt-dir>/F`; else the dir's **sole** file, searched
   recursively (`rglob`) — **error** if the dir has 0 or >1 files (the log lists them so the
   operator can add an explicit `file:`; a prompt shipping a sidecar must disambiguate).
4. Then the same read-time containment + missing-file + read tail as `file` (steps 2-4 above).

`ach` is `file` with the path auto-derived from the hydrated prompt name — the preferred form,
since the operator names the prompt (stable) rather than its on-disk layout.

`{ "type": "text" }` and the removed string form are byte-identical in effect (inline → file).

---

## 4. Schema shape (ach-agent, Pydantic v2)

```python
class SystemText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["text"]
    text: str

class SystemFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["file"]
    file: str   # relative to <home>/.ach-state; absolute or ".." → ValidationError

class SystemAch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["ach"]
    ach: str          # hydrated prompt name → <.ach-state>/prompts/<ach>/
    file: str = ""    # optional subpath; empty → the prompt dir's sole file

SystemPrompt = Annotated[SystemText | SystemFile | SystemAch, Field(discriminator="type")]

class PromptBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    system: SystemPrompt | None = None   # omitted → empty persona (today's "")
    compose: str = "append"              # contract-reserved (accepted, not executed)
```

Path-traversal rejection is enforced in a `field_validator` on `SystemFile.file` (structural, so
it holds regardless of caller), with the real-path containment re-checked at read time against the
resolved `ACH_STATE` (which is not known until engine paths resolve).

---

## 5. Migration impact (lockstep checklist)

- **ach-runtime (operator):** render `prompt.system` as the object form; add the CRD surface for
  text-vs-file + the prompt-artifact reference; never emit a bare string. Update its copy of
  `CONTRACT_v3`.
- **ach-agent (harness):** `PromptBlock` union + discriminator; `.ach-state` root + `prompts`/
  `artifacts` relocation under `<home>/.ach-state`; `<workDir>/.ach-state` symlink; file resolution
  + traversal rejection + missing-file startup error; `system_prompt.txt` unchanged.
- **Examples / configs that break and MUST migrate:** `example.yaml`, `docs/configuration.md`,
  `docs/getting-started.md`, `docker/quickstart/config.yaml`, the test agent
  `../ach-agent-test/config.yaml`. All currently use `system: "<string>"`.
- **Tests:** `tests/config/test_schema.py` (the contract-reserved guard + a new
  string-form-rejected + traversal-rejected + file-resolves test).

No harness code until ach-runtime confirms the object shape and the `.ach-state` root.
