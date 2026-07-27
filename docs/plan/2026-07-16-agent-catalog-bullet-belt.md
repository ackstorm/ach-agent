# ACKstorm Agent Catalog — the "Bullet Belt" (v0.1)

Date: 2026-07-16 · Status: DRAFT for internal review · Owner: Juan Carlos
Scope: 10 agent definitions grounded in `documentacion-general` internal docs, runnable on
ACH (`ACHAgent` operator) + `ach-agent` harness as they exist today.

> INTERNAL — references ackstorm internal systems and doc paths. Do NOT commit to the
> public ach-agent repo (this file lives under gitignored `docs/plan/`). Candidate homes:
> `documentacion-general` or a private `ach-blueprints` repo.

---

## 0. The mold — blueprint standard

Every "bullet" ships as one bundle (a directory in a future `ach-blueprints` repo):

```
<agent-name>/
  README.md            # this catalog one-pager (mission, ROI, risk tier, demo script)
  environment.yaml     # Environment CR: models + MCP servers + skills (the capability)
  agentprofile.yaml    # or reference to a shared golden profile (below)
  achagent.yaml        # the instance: channels, prompt, memory, mcpServers, limits
  prompt/              # system prompt + skill files, sourced from internal docs
  evals/               # golden tasks: input event fixture + expected-output rubric
  dashboard.json       # Grafana panel(s): turns, cost, agent-specific ROI metric
  onepager.pdf|md      # sales-facing: problem, outcome, metric, price anchor
```

**Two golden AgentProfiles cover all 10 agents** (profile = infra, agent = instance):

| Profile | For | Traits |
|---|---|---|
| `ack-reader` | report/diagnose agents (#2 #4 #5 #6 #8 #10) | read-only MCP set, no repoCheckout, small resources, memory: hindsight |
| `ack-mr-writer` | git-acting agents (#1 #3 #7 #9) | + repoCheckout, gitlab write scope; same pods, bigger `maxInvocationSeconds` |

**Risk tiers** (drives approval policy + sales conversation):
- **R** read/report — produces reports/comments only.
- **C** comment/ticket — writes to ticketing (Zoho Desk) / MR comments.
- **W** git-write — creates branches/MRs. NEVER applies: **MR review is the human gate;
  FluxCD is the only thing that touches clusters.** Agents propose, GitOps disposes.

---

## 0.1 MCP dependency matrix

| MCP server | Status | Used by |
|---|---|---|
| gitlab-mcp | HAVE (sibling repo) | 1 2 3 5 7 9 10 |
| repoCheckout (harness-hosted, built-in) | HAVE | 1 3 7 9 |
| zoho-desk | HAVE | 3 4 6 8 10 |
| zoho-projects | HAVE | 1 6 |
| zoho-crm | HAVE | 8 |
| prometheus | HAVE (off-the-shelf) | 4 5 |
| kubectl (READ-ONLY build) | ASSUMED (to build) | 2 4 5 |
| grafana / influxdb query | off-the-shelf exists | 4 6 8 10 |
| **ackmetrics (ecmanagedApi)** | **BUILD — own REST, fully documented** (`blueprints/ackmetrics/ecmanaged_api/`) | 2 4 8 10 |
| alerta | intake = webhook → `generic` channel (no MCP needed); MCP later for ack/blackout actions | 4 10 |

OTRS: legacy — no MCP. New agent output routes to Zoho Desk.

---

## 1. Platform Bootstrapper 🥇 flagship  [W]

**Mission.** Automate the Terraform+GitOps blueprint bootstrap for a new customer/environment
end to end — today a multi-hour manual dance across two repos, CloudShell, and CI settings
(`docs/internal-docs/blueprints/terraform/getting_started.md`,
`blueprints/gitops/getting_started.md`).

**Trigger & identity.** Zoho Projects task or `generic` webhook ("bootstrap CUSTOMER/ACCOUNT
on aws|gcp, env pre|pro"). `session_key = customer:env` — one lane per bootstrap, safe re-runs.

**Capability.** gitlab-mcp (repo create, files, CI variables, deploy keys, MR),
repoCheckout, zoho-projects. **No cloud credentials — by design** (see step 2).

**Flow** (mirrors documented phases A–C):
1. Scaffold IaC repo: clone `blueprints/terraform/bases-and-resources/base-{aws,gcp}`,
   strip `.git` + semantic-release files (`.releaserc.json package.json package-lock.json
   .gitlab-ci.yml`), init, push to `terraform/customers/<CUSTOMER>/<ACCOUNT>`.
2. Generate the CloudShell bootstrap script parameterized (OIDC provider/WI pool, admin
   role/SA, versioned+encrypted state bucket, EBS-encryption loop / GCP API enablement) and
   post it in the task for a human to paste into CloudShell. Agent never holds cloud creds —
   the operator runs it; agent then **verifies** outcomes via the echoed outputs and
   **populates GitLab CI/CD variables itself** (masked/protected: `AWS_ROLE_ARN` /
   `GCP_WORKLOAD_IDENTITY_PROVIDER`, `TERRAFORM_BACKEND_BUCKET`, …) — the error-prone paste step.
3. Install the right pipeline file from `blueprints/terraform/pipelines`
   (Gitleaks → Validate → Plan → Check → manual Apply, `check-destroy` guard intact);
   skeleton `config.tf` + `terraform.tfvars` (no secrets in tfvars — enforced).
4. Flux handoff: after first apply, instruct/verify deploy-key registration on the GitOps
   repo (write-enabled), flip `enable_flux_bootstrap = true` via MR.
5. Scaffold GitOps repo from `blueprints/gitops/bases-and-resources/base-{aws,gcp}` +
   validation pipeline (same strip-and-push dance).
6. **a2a → Handover Verifier (#2)** to run the checklist and produce the handover report.

**Human gates.** CloudShell script execution (operator), every MR, manual Apply stage.
**Prompt sources.** `terraform/getting_started.md`, `terraform/customizing.md`,
`gitops/getting_started.md`, `gitops/customizing.md`, pipeline repos' READMEs.
**Evals.** (a) scaffold from base-gcp → repo diff matches golden repo; (b) CI vars complete
and masked for aws-sts variant; (c) tfvars containing a secret → agent refuses + flags;
(d) re-run after partial failure → idempotent, no duplicate repos.
**ROI.** Bootstrap lead time (target: days → hours) · error rate at handover.

**CR sketch:**
```yaml
apiVersion: ach.ackstorm.ai/v1alpha1
kind: ACHAgent
metadata: {name: platform-bootstrapper}
spec:
  profileRef: {name: ack-mr-writer}
  identity: {secretRef: {name: ek-bootstrapper}}
  capability:
    environment: env-bootstrapper        # gitlab-mcp + zoho-projects + model
  prompt:
    system: {type: ach, ach: {name: prompt-bootstrapper}}   # built from the docs above
  memory: {type: hindsight, hindsight: {…}}                 # remembers per-customer quirks
  mcpServers:
    - {name: repo, type: repoCheckout}
  channels:
    - name: intake
      type: webhook
      source: generic
      session: {type: auto}
```

---

## 2. Handover Verifier 🥈 first bullet to build  [R]

**Mission.** Execute the two handover checklists as an automated audit:
`blueprints/terraform/verification_checklist.md` + `blueprints/gitops/verification_checklist.md`.
Output: a signed handover report (pass/fail per item + evidence) posted to the MR/task.

**Trigger.** `generic` webhook or cron ("verify CUSTOMER/env"). `session_key = customer:env`.
**Capability.** gitlab-mcp (repo state, pipeline results), kubectl-ro (cluster reachable,
`flux get kustomizations`, workloads up), prometheus/grafana (dashboards exist),
ackmetrics MCP when built (Timeperiod/Environment configured, no active alerts).
v0 without ackmetrics: git+cluster checks only — still valuable.

**Checks (from the checklists).** repo cloned at latest release + semantic-release removed;
TF/provider versions; backend config; no CIDR overlap vs customer docs; secrets in Secret
Manager, `sensitive=true` outputs; plan clean; Flux bootstrapped, all kustomizations ready;
alerts clean; budgets configured; docs/diagram/escalation present.

**Human gate.** None needed — read-only. Report IS the deliverable; squad signs off on it.
**Evals.** Golden env with 3 seeded violations (CIDR overlap, missing budget, kustomization
not-ready) → report catches all 3, zero false positives on clean env.
**ROI.** Handover review time · defects escaping to operations. Doubles as **eval oracle
for #1** and the case-study generator (run on next real handover).

---

## 3. GitOps Service Adder  [W]

**Mission.** Turn "add service X to customer Y's cluster" (Zoho Desk ticket / webhook) into
a correct MR: copy the service folder from `blueprints/gitops/bases-and-resources/resources`,
edit `namespace.yaml`/`install.yaml`, **and update the sibling `kustomization.yaml`** — the
documented easy-to-forget step (`gitops/customizing.md` warns twice).
**Trigger.** Zoho Desk / generic webhook. `session_key = customer:cluster`.
**Capability.** gitlab-mcp, repoCheckout, zoho-desk (ticket update).
**Human gate.** MR review; Flux applies only after merge.
**Evals.** (a) add redis to `infra/system` → MR diff matches golden; (b) kustomization.yaml
entry present (the footgun); (c) service already present → agent reports, no duplicate MR.
**ROI.** Ticket-to-MR lead time · % MRs merged without review corrections.

---

## 4. Alert First-Responder ⭐ highest recurring ROI  [C]

**Mission.** Enrich every fatal/critical/major alert before the on-call human answers the
Nagios call. Knowledge base already written:
`blueprints/ackmetrics/alerta/common_alerts/index.md` (per-alert diagnosis + escalation,
with real ticket references) → that IS the system prompt.
**Trigger.** Alerta webhook → `generic` channel (or queue). `session_key = customer:resource`
— flapping alerts on the same resource share one lane/conversation.
**Capability.** kubectl-ro, prometheus + grafana/influx (evidence queries), zoho-desk
(enrich/create ticket), ackmetrics MCP later (ack/blackout suggestions, probe history).
**Flow.** classify alert vs KB → run the documented diagnosis (CPU steal? disk grow-rate?
k8s restarts? probe ssl?) → post to ticket: probable cause, evidence (queries+numbers),
runbook link, suggested action, "seen before" from memory (hindsight per-customer bank).
**Human gate.** Suggests only; never acks, never blackouts (until trust earned — then
policy-gated ack for `minor/warning`).
**Evals.** Replay 20 historical alerts with known root causes → cause matched ≥80%,
runbook link correct, zero hallucinated evidence (every number must come from a tool call).
**ROI.** MTTA/MTTR delta · % alerts pre-enriched before pickup · on-call satisfaction.
**Note.** Dogfood in shadow mode on `alerta.ackstorm.com` (notify panel, no calls) first.

---

## 5. Flux Doctor  [R→W]

**Mission.** When kustomizations/helmreleases fail (alert or cron sweep): diagnose via
`flux get` / events / helm status (kubectl-ro), classify (image pull, values error, timeout,
drift), then either propose the fix MR in the gitops repo or escalate with a precise ticket.
**Trigger.** k8s alert webhook (`restart count`, `unavailable replicas`, `pods-failed` from
common_alerts) or cron sweep. `session_key = customer:cluster`.
**Capability.** kubectl-ro, gitlab-mcp, prometheus.
**Human gate.** MR review for fixes; ticket for escalations.
**Evals.** Seeded failures in a test cluster: bad image tag, wrong values key, missing
namespace → correct classification + fix-MR for the first two, escalation for ambiguous.
**ROI.** GitOps failure MTTR · % auto-diagnosed correctly.

---

## 6. FinOps Reviewer  [C]

**Mission.** Automate the two documented periodic FinOps procedures:
(a) GCP recommendations review (`runbooks/finops/google_recomendations.md`: review dashboard →
internal ticket → N2 review → customer approval → act → communicate savings);
(b) the **six-monthly VIP cost review** (`runbooks/finops/finops-docs.md`: CloudCheckr, Cost
Explorer/Optimization Hub, FinOps Hub, Grafana rightsizing 35–45%).
Client email drafts from the documented ES/EN templates — verbatim prompt material.
**Trigger.** cron (monthly recos; six-monthly VIP calendar). `session_key = customer`.
**Capability.** grafana/influx (+BigQuery for GCP billing), zoho-desk + zoho-projects
(tickets/approval tracking), gitlab-mcp (report archive).
**Human gate.** N2 technical review (existing step) + customer approval — agent drafts, never sends.
**Evals.** Fixture billing dataset with 3 known recommendations → all surfaced, savings
math correct, email draft matches template tone in ES and EN.
**ROI.** € savings surfaced/executed · FinOps-tribe hours per review cycle.

---

## 7. Upgrade Planner  [W]

**Mission.** Cron sweep of customer terraform+gitops repos against documented version
matrices and migration paths: module versions (`terraform/module_upgrades.md` — base-aws
v1→v2, IRSA→Pod Identity, VPC-CNI known-issue), EKS AL2→AL2023 (`runbooks/other/eks_linux2023.md`),
GitLab chart v8→v9 (`runbooks/other/gitlab_helm_upgrade_path.md`), RDS CA
(`runbooks/other/update_rds_ca_certificate.md`). Output: per-customer upgrade-plan issue,
optionally the scaffolded MR for mechanical bumps.
**Trigger.** cron weekly. `session_key = customer:repo`.
**Capability.** gitlab-mcp, repoCheckout.
**Human gate.** Issues + MR review; `check-destroy` pipeline guard stays as backstop.
**Evals.** Repo pinned at base-aws v1.x → plan cites v2 migration steps + known-issue;
up-to-date repo → "no action", no noise.
**ROI.** Patch/upgrade lag (days behind matrix) · upgrade toil hours.

---

## 8. Cert & Domain Steward  [C]

**Mission.** Close the renewal loops: SSL validity sweep (`runbooks/other/certificate_validity.md`
— Grafana + probes, confirm renewals actually applied) and domain renewals
(`runbooks/other/domains-management.md` — CRM inventory + Nominalia renewals, zone deactivation).
**Trigger.** cron daily (certs) / weekly (domains). `session_key = batch date` (cron lane).
**Capability.** prometheus/probe data + grafana, zoho-crm (domain inventory), zoho-desk
(renewal tickets), ackmetrics MCP (SSL probe results) when built.
**Human gate.** Tickets only; deactivations stay human.
**Evals.** Fixture: cert expiring in 20d + one renewed-but-probe-stale → one ticket opened,
one verified-applied and closed, no duplicate tickets on re-run (dedup via router + memory).
**ROI.** Expiry incidents (target 0) · renewal toil.

---

## 9. Customer Docs Gardener  [W]

**Mission.** (a) Onboarding: scaffold `docs/customers/<name>/` sub-site from the right
template (`single/` vs `nglz/` — `docs/customers/template/`), fill the Projects/Accounts +
Support/Monit tables from the IaC repo facts, register the `!include` in root `mkdocs.yml`.
(b) Cron drift: accounts present in `terraform/customers/<CUSTOMER>` but missing in docs,
stale support tiers, missing runbook stubs → MR to `documentation/general`.
**Trigger.** onboarding webhook + cron monthly. `session_key = customer`.
**Capability.** gitlab-mcp, repoCheckout.
**Human gate.** MR review (docs repo).
**Evals.** New customer fixture → sub-site builds under mkdocs monorepo without error,
tables complete; drift fixture (1 undocumented account) → caught.
**ROI.** Doc drift count · onboarding doc time. Also fixes the stated gap: "no single
consolidated onboarding runbook" — the agent's own procedure becomes it.

---

## 10. NODATA / Billing Sentinel  [C→W]

**Mission.** Execute `runbooks/monitoring/billing_nodata.md` on NODATA billing alerts:
check the InfluxDB nodata query, determine cause (account closed / export broken / new
account), then either open the fix ticket or — replacing today's documented SSH-edit —
propose the MR adding the account to `SKIP_ACCOUNTS_{GCP,AWS}` in `billing_check_nodata.py`
on the `monitor-alert-checks` repo.
**Trigger.** Alerta nodata webhook. `session_key = account`.
**Capability.** influx/grafana query, gitlab-mcp, zoho-desk.
**Human gate.** MR review for skip-list; ticket otherwise.
**Evals.** Closed-account fixture → skip-list MR with justification; broken-export fixture
→ ticket with the failing query as evidence, NOT a skip-list MR (the failure mode that matters).
**ROI.** NODATA alert noise · time-to-silence legitimate gaps.

---

## Build order & dogfood plan

```
Sprint 1  #2 Handover Verifier      — read-only, checklist exists, run on next real handover
Sprint 2  #4 Alert First-Responder  — shadow mode on alerta.ackstorm.com (notify, no calls)
          + build ackmetrics MCP (unblocks 2's full checks, 4, 8, 10)
Sprint 3  #1 Platform Bootstrapper  — with #2 as its verifier via a2a (multi-agent demo)
Sprint 4  #6 FinOps Reviewer        — one squad's customers first
Then      MR-pattern pack: #3 #7 #10 #9 #5 #8 (same Environments/profiles, mostly prompt work)
```

Dogfooding = the GTM factory: every sprint produces eval sets, ROI numbers, and the demo.
Case-study targets: #2 on a real handover, #4 over one on-call week, #6 one review cycle.

## ROI summary (sales one-liner per bullet)

| # | Agent | Headline metric |
|---|---|---|
| 1 | Platform Bootstrapper | env bootstrap: days → hours |
| 2 | Handover Verifier | zero-defect handovers, minutes not meetings |
| 3 | GitOps Service Adder | ticket→MR in minutes, footgun eliminated |
| 4 | Alert First-Responder | every 3am call arrives pre-diagnosed |
| 5 | Flux Doctor | gitops failures self-explain |
| 6 | FinOps Reviewer | € savings surfaced on schedule, drafts ready |
| 7 | Upgrade Planner | never silently behind a version matrix |
| 8 | Cert & Domain Steward | zero expiry incidents |
| 9 | Docs Gardener | customer docs never lie |
| 10 | Billing Sentinel | NODATA noise → one reviewed MR |
