# Phase 2 — CONTRACT_v3 `engine.pi` cross-repo contract polish

**Goal:** CONTRACT_v3 fully documents the `engine.pi` seam as a cross-repo contract — what the
fields mean, that `../ach` renders them and the harness validates them, and the
image-ships-before-operator-advertises ordering rule. **Docs only; no schema change** (SP1 froze
the schema).

**Files:**
- Modify: `docs/plan/CONTRACT_v3.md` (the engine block, around `:121-123`)

**Interfaces:**
- Consumes: SP1's frozen `PiEngineBlock{binaryPath, mcpAdapterPath}` and the `engine.type` field.
- Produces: nothing code-facing (documentation).

---

- [ ] **Step 1: Expand the inline `type`/`pi` comments**

`docs/plan/CONTRACT_v3.md:121-123` currently reads:

```jsonc
    "type": "opencode",                       // opencode | pi (SP1). Selects the EngineDriver.
    "pi": null                                // PiEngineBlock {binaryPath, mcpAdapterPath}; only
    //                                           consulted when type == "pi".
```

Replace with the fuller cross-repo form:

```jsonc
    "type": "opencode",                       // opencode | pi. Selects the EngineDriver. Rendered
    //                                           by ../ach (AgentProfile.engine.type, free string);
    //                                           the harness is the enforcer (unknown → hard-fail).
    "pi": null                                // PiEngineBlock; consulted only when type == "pi":
    //                                           { "binaryPath": "pi",           // pi on PATH in the image
    //                                             "mcpAdapterPath": "" }        // "" → image default
    //                                           /opt/pi-mcp-adapter/node_modules/pi-mcp-adapter
```

- [ ] **Step 2: Add the cross-repo ordering note**

Immediately after the closing `},` of the engine block (the line after `:124`), add:

```jsonc
  // ── engine.type=pi is a CROSS-REPO contract ──────────────────────────────────
  // The ach-agent IMAGE must ship the `pi` binary + pinned pi-mcp-adapter BEFORE any
  // control plane renders engine.type=pi — otherwise the rendered config names a binary
  // the image lacks. Ship order: ach-agent image (Pi SP2) → then ../ach advertises pi.
```

- [ ] **Step 3: Verify the doc reads correctly**

Run: `grep -n "mcpAdapterPath\|CROSS-REPO contract\|node_modules/pi-mcp-adapter" docs/plan/CONTRACT_v3.md`
Expected: the new lines are present.

- [ ] **Step 4: Commit**

```bash
git add docs/plan/CONTRACT_v3.md
git commit -m "docs(contract): document engine.pi as a cross-repo seam + ship-order rule

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```
