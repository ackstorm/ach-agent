# Agent Catalog Deep-Dive — internal-docs grounding (v0.1)

Date: 2026-07-16 · Status: DRAFT · Owner: Juan Carlos · Companion to
`2026-07-16-agent-catalog-bullet-belt.md` (validates it and deepens the agent definitions).
Source of truth audited: `documentacion-general/docs/internal-docs/**` (122 files; all
blueprint + runbook files read in full by 6 parallel readers, 2026-07-16).

> INTERNAL — do NOT commit to the public ach-agent repo (lives under gitignored `docs/plan/`).

---

## 1. Validation verdict on catalog v0.1

Catalog structure, risk tiers, build order: **sound**. Grounding: **mostly right, 14
corrections needed** — 4 of them change agent capability decisions, the rest are detail fixes.

### 1.1 Capability-changing corrections

| # | Catalog said | Docs actually say | Impact |
|---|---|---|---|
| C1 | #4/#5 evidence via **prometheus** | Monitoring stack is **ecmanaged-agent (telegraf fork) → InfluxDB → Python alert-check scripts (InfluxQL) → Alerta**. Prometheus/Zabbix appear nowhere in the monitoring docset (`ackmetrics/getting_started.md`, `alerta/index.md`) | Evidence queries for alerts = **Grafana/InfluxDB**, not prometheus-mcp. Prometheus stays only for k8s-native metrics if a customer cluster ships it |
| C2 | Alert diagnosis runnable with kubectl-ro | Half the documented diagnoses are **SSH-on-VM commands** (`top`, `df -h`, `docker stats`, `du -shx`…) (`common_alerts/index.md`) | Agent-based (VM) alerts: v1 = **Grafana evidence only + runbook link**, no live VM diagnosis. K8s/probe/cloudstats/nodata alert classes are fully coverable |
| C3 | #8 domain inventory in **zoho-crm** | Inventory lives in **`crm.ackstorm.es`** — legacy in-house ColdFusion CRM, not Zoho (`domains-management.md`). Nominalia = browser-only, **no API** | #8 domains scope blocked on legacy-CRM access; renewals stay human. v1 #8 = certs only |
| C4 | zoho-desk/projects "HAVE" implies ready | MCP exists (platform `vmcp-zoho`) but internal-docs contain **zero Zoho workflow documentation** (2 trivial mentions in 122 files). Documented ticketing = legacy **OTRS**, linked from Alerta | Ticket-writing agents can act mechanically, but queue/SLA/escalation conventions must be **captured into a doc first** — nothing to prompt-source today. See §6 |

### 1.2 Detail corrections (patch list for catalog v0.2)

1. **#1 flux handoff is a two-apply choreography**, not a flag flip: apply #1 with bootstrap
   *disabled* generates the SSH keypair into cloud Secret Manager → register write deploy key
   on GitOps repo → set `enable_flux_bootstrap = true` → apply #2. Enabling before first
   apply **fails** (`terraform/tips_tricks.md` FAQ). Also documented in
   `terraform/getting_started.md §Configuring Flux` — not in the gitops docs.
2. **#1 strip list**: also `README.md`, not just `.git` + semantic-release files.
3. **#1 repo path**: docs self-contradict — prose `terraform/customers/<CUSTOMER>/<ACCOUNT>`
   (plural, nested) vs literal example `terraform/customer/my-platform.git`. Follow the prose.
4. **#1 GCP CI vars footgun**: bootstrap script echoes `SERVICEACCOUNT` (table wants
   `SERVICEACCOUNT_NAME`) and never prints `PROJECT_NUMBER` — agent must remap/derive, not
   copy verbatim. Full var table: AWS = `AWS_ROLE_ARN`, `OIDC_TOKEN`; GCP =
   `GCP_WORKLOAD_IDENTITY_PROVIDER`, `IDENTITY_POOL`, `IDENTITY_PROVIDER`, `PROJECT_NUMBER`,
   `SERVICEACCOUNT_NAME`, `TERRAFORM_BACKEND_BUCKET`.
5. **#1 GCP+GitLab script gap**: never creates the workload-identity **pool** (assumes it
   exists); only the GitHub variant creates it. Agent must pre-check/warn.
6. **#3 "warns twice" → warns once**: exactly one `!!! danger` block on the forgotten
   `kustomization.yaml` entry (`gitops/customizing.md:259`). Still the footgun.
7. **#3 namespace edits are conditional** (only when overriding default), and common-namespace
   services go to `infra/system/` + `infra/system/kustomization.yaml` (different sibling file).
8. **GitOps layout**: top-level = `cluster/ helmrepos/ infra/ microservices/` (not "apps");
   **one repo per environment**, base/overlay explicitly banned (`customizing.md`).
9. **Secrets mechanism = ExternalSecrets only** (checklist mandates it); SOPS unmentioned;
   the sealed-secrets snippet in `fluxcd.md` is explicitly "DO NOT use as base template".
10. **#7 overclaim**: `module_upgrades.md` documents IRSA(v1.x) vs Pod Identity(v2.x) as two
    live tracks + one VPC-CNI known issue + small matrix — **no v1→v2 migration runbook**.
    Upgrade Planner must build its own matrix; the file is a seed, not a source.
11. **#10**: `monitor-alert-checks` is the **host** (bastion-reached), not a named repo; edit
    dir = `/usr/local/ackstorm/alert-check/`, changes are `git commit && push`ed from there
    but the remote is unnamed in docs — locate it in GitLab before promising the MR path.
    Skip formats: GCP `project.name::project.id`, AWS free-text name. Bonus tool:
    `list_alerta_alerts_by_origin.py --origin=billing_check_nodata` bulk-lists open nodata alerts.
12. **#6 "35–45%"** is a **CPU/mem utilization target band** (under → rightsize down, over →
    upscale), not a savings percentage. GCP-reco cadence is "periodically", not "monthly".
13. **#4 severities**: fatal / critical (24x7) / major (workhours) / warning / minor / normal.
    Two instances confirmed: `alerta.ackstorm.io` (prod, Nagios calls) and
    `alerta.ackstorm.com` (notify, vault-key access) — shadow-mode target confirmed.
14. **#2 scope extension**: `gitlab/` and `cicd/` blueprints also ship verification
    checklists (24 + 9 items) — catalog only counted terraform+gitops.

### 1.3 Systemic findings

- **FinOps data sources are documented console-only** (CloudCheckr, Cost Explorer, Cost
  Optimization Hub, GCP Billing/Anomalies UI). API-reachable today: **Grafana** (orgId 3
  `d/e1MCEv57z` GCP recommendations; orgId 13 billing customer view; rightsizing dashboards),
  **GCP Recommender API** (doc names it as the automation alternative), AWS Budgets/CE APIs
  (exist, but need the cloud read role — §5.2). BigQuery billing export is used by the
  nodata checker but never documented as a FinOps source.
- **No read-only cloud role exists** for humans or machines. All 6 documented AWS roles are
  write-tier (least = SystemAdministrator). Precedents to build on: OIDC federation already
  used for `ackstorm_gitlab`/`ackstorm_github` CI roles; CloudCheckr already installs a
  read-only cross-account CFN role; EKS access-entry validation already allow-lists
  `AmazonEKSViewPolicy`. → §5.2 design.
- **kubectl access itself is undocumented** in internal-docs (`aws eks update-kubeconfig`
  appears only in one customer doc) — the kubectl-ro MCP (§5.1) must encode it.
- **Doc hygiene debt is real** and includes a security issue (§7) — strengthens the case for
  a Docs Gardener internal mode.

---

## 2. Corrected MCP dependency matrix

| MCP server | Status | Used by | Notes |
|---|---|---|---|
| gitlab-mcp | HAVE (sibling repo) | 1 2 3 5 7 9 10 | unchanged |
| repoCheckout (harness built-in) | HAVE | 1 3 7 9 | unchanged |
| zoho-desk / zoho-projects | HAVE (platform `vmcp-zoho`) | 3 4 6 8 10 | mechanically usable; **no internal workflow doc to prompt from** (C4) |
| zoho-crm | HAVE | — | **dropped for #8** — inventory is in legacy `crm.ackstorm.es` (C3) |
| grafana (+ influx queries) | HAVE (off-the-shelf; `grafana.ackstorm.com`) | 2 4 5 6 8 10 | **promoted: primary evidence source** for alerts + finops (C1) |
| prometheus | optional | 5 | only where a customer cluster ships it; not the ACK monitoring stack |
| kubectl-ro | BUILD (§5.1) | 2 4 5 | flux/helm/kubectl read verbs; per-customer kubeconfig; no exec/delete |
| ackmetrics (ecmanagedApi) | BUILD (§5.3) | 2 4 8 10 | REST surface confirmed + gotchas (416, admin-id, alert-builder) |
| alerta (OSS REST) | BUILD later (§5.4) | 4 10 | intake stays webhook→`generic`; API for read + policy-gated ack/blackout |
| GCP Recommender API | wire via read role | 6 | the documented automation path for recommendations |
| AWS Budgets / Cost Explorer API | wire via read role | 6 8 | replaces console-only doc flow |
| BigQuery (billing export) | wire via read role | 6 10 | nodata checker's GCP source |
| ssh-exec on customer VMs | **NOT available — accepted gap** | (4) | VM-alert diagnosis degrades to Grafana evidence + runbook link (C2) |
| CloudCheckr / Nominalia | none (console/browser only) | — | out of agent reach; human steps remain |

---

## 3. Deep dives — the agents Juan Carlos flagged

### 3.1 #1 Platform Bootstrapper [W] — Terraform onboarding

**Inputs** (intake payload): customer, account/platform name, cloud (`aws|gcp`), region,
CI host (GitLab assumed v1; Bitbucket/Azure/GitHub differ in pipeline file + flux auth),
env (`pre|pro`), CIDR plan, GitOps repo URL. `session_key = customer:account`.

**Corrected flow** (7 steps; docs: `terraform/getting_started.md` end-to-end):

| Step | Actor | Tool calls | Verify |
|---|---|---|---|
| 0. Pre-flight | agent | gitlab-mcp: assert dest project `terraform/customers/<C>/<A>` absent; create it | project exists, empty |
| 1. Scaffold | agent | repoCheckout: clone `base-{aws,gcp}`; strip `.git README.md .releaserc.json package.json package-lock.json .gitlab-ci.yml`; `git init -b main`; push | tree diff vs golden; stripped files absent |
| 2. Cloud init | **human** (CloudShell) | agent renders the right script variant (aws/gcp × gitlab/github) with all vars pre-filled — no CHANGE_ME left — posts to task; human runs, pastes echo block back | agent parses echo output |
| 3. CI vars | agent | gitlab-mcp: set vars per table (correction #4 remaps!), masked+protected; install pipeline file from `blueprints/terraform/pipelines` as `.gitlab-ci.yml` | vars present w/ flags; pipeline file present |
| 4. Backend+tfvars | agent | edit `config.tf` (bucket/region) + `terraform.tfvars`; **secret-scan before push** (mirrors Gitleaks) | Gitleaks+Validate+Plan+Check green |
| 5. Apply #1 | **human** (manual gate) | agent watches pipeline; `DESTROY=OK` is human-only, always | apply job success |
| 6. Flux handoff | mixed | human retrieves deploy key from Secret Manager (agent has no cloud creds) → agent registers it write-enabled on GitOps repo via gitlab-mcp → MR flipping `enable_flux_bootstrap=true` → human apply #2 | deploy key `can_push=true` before flag flip |
| 7. GitOps repo + verify | agent | scaffold GitOps repo (with-terraform-base path: copy only `infra/ helmrepos/ cluster/ CHANGELOG.md`); install Gitleaks+Validate pipeline; **a2a → #2 Handover Verifier** | #2's report |

**Prompt sources**: `terraform/getting_started.md`, `terraform/customizing.md`,
`terraform/tips_tricks.md` (FAQ = failure modes), `gitops/getting_started.md`,
`gitops/customizing.md`. Landing Zone (NGLZ) is a **separate manual track** (AWS-only,
Control Tower; v2 roadmap adds a pipeline) — out of scope, detect-and-refuse if asked.

**Footguns to encode** (quoted in docs): flux-before-first-apply fails; "Never include
sensitive values in this file" (tfvars, stated twice); GCP var remap; prose-vs-example path;
`.gitlab-ci.yml` stripped in step 1 is *not* the one installed in step 3.

**Evals**: (a) golden scaffold diff base-gcp; (b) CI vars complete/masked from a fixture echo
block incl. the `SERVICEACCOUNT` remap; (c) tfvars with seeded secret → refuse+flag;
(d) `enable_flux_bootstrap=true` pre-first-apply fixture → agent resets to false citing FAQ;
(e) re-run after partial failure → idempotent.

### 3.2 #2 Handover Verifier [R] — validation

**Quantified** (all four checklists inventoried): **89 items** — terraform 36, gitops 20,
gitlab 24, cicd 9. Machine-checkable: **57 with gitlab-mcp + kubectl-ro alone (v0)**,
**64 adding grafana + ackmetrics (v1)**, 25 human/cloud-creds-only (reported as `MANUAL`
rows in the output, never silently skipped).

- v0 check styles: file/content asserts (backend block, `sensitive=true` regex, README,
  escalation section, semantic-release absence, PRO version pins exact vs semver-range,
  `${VAR}`↔`flux.tf` cross-check), pipeline-job status (plan/apply/lint/gitleaks), cluster
  asserts (`kubectl get nodes/pvc/hpa`, `flux check`, `flux get ks/hr -A` all Ready).
- v1 adds: ackmetrics `GET environment` (timeperiod+mode set), `GET alerts` empty, Grafana
  dashboard-exists, probe overall-health.
- Remaining 25 (budgets, PITR, bucket versioning, IAM least-privilege, "ops trained") need
  the §5.2 cloud read role or stay human — the report marks them explicitly.

**Session**: `session_key = customer:env`. Report posted to MR/task; squad signs off.
**Evals**: seeded violations — missing `sensitive=true`; PRO HelmRelease with semver range;
kustomization not-ready; open ackmetrics alert; two-`FROM` Dockerfile (cicd) → all caught,
zero false positives on clean env.

### 3.3 #3 GitOps Service Adder [W]

**Corrected procedure** (`gitops/customizing.md`): identify source folder in
`bases-and-resources/resources` (root-level = own-namespace, folder name **must equal**
target namespace; `infra_common_namespace/` = shared) → copy into `/infra/<svc>` or
`/infra/system/<svc>` → namespace override **only if requested** (edit `namespace.yaml` +
`install.yaml` path/substitute) → **update the correct sibling** `kustomization.yaml`
(`/infra/` vs `/infra/system/` — two different files) → version pin per policy tier:
PRO/critical = exact pin; non-critical may use PATCH/MINOR semver range; MAJOR auto-upgrade
never.

**Design note**: the resources catalog is NOT enumerated in docs (placeholders only) — the
agent must **list the live `resources` repo tree via gitlab-mcp at runtime**, never from a
baked-in list. Everything is GitLab-API-only (Flux applies post-merge) — kubectl-ro not needed.

**Evals**: karpenter add (namespace-override branch, 3 files touched); common-namespace add
(system/kustomization.yaml, not infra/); duplicate add → report, no MR; MR must pass
Gitleaks+Validate.

### 3.4 #5 Flux Doctor [R→W]

**Documented toolbox** (`gitops/tips_tricks.md`, `fluxcd.md`): `flux get ks|hr|all -A`,
`flux stats`, controller logs (`kubectl logs -n flux-system -l app=helm-controller|
kustomize-controller|source-controller`), ordered manual checklist Kustomization →
HelmRepository → HelmChart → HelmRelease, `helm list -A`, stuck-"Upgrade in progress" fix =
delete helm-controller pod, immutable-field failures → `spec.force: true` **with data-loss
warning**, suspend/resume gotcha (suspend the Kustomization, not the HelmRelease).

**Not documented — the agent's added value** (build into prompt/skill): failure-class
taxonomy (transient / config / immutable-field / quota / upstream chart), `kubectl get
events` + `helm status/history/get values` usage, reading `status.conditions[].message`
programmatically, remediate-vs-escalate policy (e.g. helm-controller pod-kill = suggest to
human in v1; MAJOR-bump breakage = always escalate).

**Evals**: bad image tag → fix MR; wrong values key → fix MR; stuck upgrade-in-progress →
recommend pod delete (no MR); immutable field → `force:true` MR incl. warning; failed MAJOR
auto-bump → escalate citing version policy.

### 3.5 #4 Alert First-Responder [C] — alerts/automation

**Scope by alert class** (consequence of C1/C2):

| Class | Examples | Diagnosis capability |
|---|---|---|
| K8s | restart-count>15, unavailable-replicas, pods-failed | FULL: kubectl-ro describe/logs/events (delete-pod remediation = suggest only) |
| Probe | http, ssl, error-rate | FULL: openssl checks in-harness + Grafana worldping panels |
| Cloudstats/nodata | cloudstats-no-metric, billing nodata | FULL: grafana/influx + ackmetrics API (+ #10 for billing) |
| VM/agent-based | cpu-idle, cpu-steal, disk-usage/grow-rate, inodes, mysql-qps, input=all | DEGRADED: Grafana metric evidence + KB runbook link + "seen before" memory; no live `top`/`df` |

**Prompt skeleton = the KB table** distilled from `common_alerts/index.md` (~20 alert types
with diagnosis + escalation + real OTRS ticket refs, e.g. Disk Usage #78729, K8s Restart
#78904, cloudstats #75241/#76708). **Coverage gap**: `alerta/index.md` lists many more
default-alert families (Redis, PostgreSQL, HAProxy, ES cluster/heap/shards, HPA,
disk-prediction…) with **no procedure** — agent must say "no documented runbook" + attach
evidence, never improvise a diagnosis. That honesty rule goes in the prompt.

**Flow**: alerta webhook → `generic` channel (`session_key = customer:resource`, flapping
shares a lane) → classify vs KB → run class-appropriate evidence gathering → post to ticket:
probable cause, evidence (every number from a tool call), runbook link, suggested action,
seen-before (hindsight per-customer bank). Never acks/blackouts in v1.

**Integration facts**: resource pattern `{account_id}/{key=value}/{event}`; per-alert fields
Severity/Status/Origin/Customer/Environment/Timeperiod/OtrsTicket/Event/Resource; exact
webhook JSON schema is NOT in internal docs — take it from OSS Alerta API docs when wiring.
New machines: no alerts first 5 min (`alerta/index.md`) vs agent `WARMUPTIME` 120s — treat
suppression window as configurable, don't hardcode.

**Evals** (replayed from KB tickets): disk-usage spike → autoclean/expansion suggestion;
restart-count → describe/logs chain; cloudstats-no-metric → verify vs cloud dashboard +
rerun suggestion; undocumented family (e.g. Redis) → "no runbook" + evidence, no hallucinated
diagnosis; zero numbers without a backing tool call.

### 3.6 #10 NODATA / Billing Sentinel [C→W]

Runbook confirmed (`runbooks/monitoring/billing_nodata.md`): 4-day InfluxQL nodata window;
sources BigQuery (GCP) / Cost Explorer (AWS); causes account-closed / export-broken /
new-account. **v1 flow**: influx/grafana query → classify → ticket with evidence; skip-list
change proposed as MR **only after locating the alert-check repo remote in GitLab**
(unnamed in docs — discovery task; until then, ticket carries the exact one-line diff for a
human to apply via the documented SSH path). Use `list_alerta_alerts_by_origin.py` output
pattern for bulk triage. Eval that matters: broken-export fixture must produce a ticket with
the failing query — **never** a skip-list entry (silencing a real gap is the failure mode).

### 3.7 #6 FinOps Reviewer [C]

**Rescoped to API-reachable sources** (§1.3): v1 = **GCP recommendations flow** — fully
documented 6-step process (`google_recomendations.md`: review/export → internal ticket → N2
review → customer approval → act → communicate savings) with an existing Grafana dashboard
(orgId 3, `d/e1MCEv57z`) + GCP Recommender API; drafts internal ticket (zoho vmcp) + client
email from the **verbatim ES/EN templates** (CUD/rightsizing template in
`google_recomendations.md`; 4 anomaly variants AWS/GCP × CMS/no-CMS in `finops-docs.md`).
v2 (needs §5.2 role) = AWS Budgets/CE/Optimization-Hub reads + six-monthly VIP review pack.

**Per-customer flags the agent needs** (nowhere maintained today — create a config): CMS
yes/no (template variant), **rebilling flag** (suppress RI/SP suggestions — central pool),
VIP flag + last-review date, billing account IDs, assigned squad/N2 contact.

**Hard rules from docs**: 35–45% = utilization band; always cross-check CloudCheckr CPU-only
data with Grafana RAM; dedupe daily AWS budget alerts against open tickets; keep ticket open
until anomaly resolved/budget resets; gradual client communication ("so as not to alarm the
client"); CUD lock-in warning in every draft; agent drafts, **never sends**.

**Evals**: SHUTDOWN_INSTANCE rec on a monthly-batch VM → flag for N2 exclusion; duplicate
budget alert → skip; rebilling client → RI/SP suppressed; CUD draft halts at approval gate;
non-CMS anomaly → correct template, no root-cause attempt.

### 3.8 #8 Cert & Domain Steward [C] — rescoped

**v1 = certs only**: sweep Grafana worldping HTTP-probe Overall Health (OK/CRITICAL) +
in-harness `openssl x509 -noout -dates` per endpoint; verify renewals actually applied
(probe recovered post-renewal); open/close zoho-desk tickets; dedup via router + memory.
**No numeric expiry threshold is documented** — policy decision needed (docs only give
binary CRITICAL); agent must not invent "30 days" (eval fixture). ACM nuance: reimport
auto-propagates to LB/CloudFront. RDS CA rotation (`update_rds_ca_certificate.md`) =
detect+ticket only (client app-code gate first).

**Domains = blocked** (C3): inventory in legacy `crm.ackstorm.es`, Nominalia browser-only,
zone deletion has human-timing rules (non-holiday morning) and the CRM-cancel ≠
Nominalia-unregister trap. Revisit if/when inventory migrates to an API-reachable system.

---

## 4. The Zoho Desk story (C4)

What's true today: `vmcp-zoho` gives mechanical ticket CRUD; agents #3 #4 #6 #8 #10 write
tickets through it. What's missing: internal-docs define severity/SLA/escalation **only for
Alerta+OTRS**; Zoho queue conventions, ticket taxonomy, department routing, SLA tiers,
customer-visibility rules are undocumented → an agent prompt would encode guesses.

**Action before any Desk-heavy agent**: capture a 1-2 page `runbooks/support/zoho-desk.md`
(queues, severities↔Alerta mapping, escalation ladder, what agents may write where,
OTRS-vs-Desk boundary during migration). Cheap to write, unblocks #4/#6/#8/#10 ticket
quality, and doubles as the Desk-triage-agent prompt source later. Until then, agents write
tickets with a fixed conservative shape (dept + severity passthrough + evidence body).

---

## 5. New infra to build (prerequisite track)

### 5.1 kubectl-ro MCP
Read-only k8s/flux/helm for #2 #4 #5. Command allowlist (union of the deep-dives):
`kubectl get|describe` (nodes, pods, deploy, svc, pvc, hpa, cronjob, externalsecrets,
nodepools, events), `kubectl logs`, `kubectl top nodes|pods`, `flux check|stats|get ks|hr|
sources -A`, `helm list|status|history|get values`. **Excluded**: exec, delete, edit,
port-forward, secret *values* (names/presence only). Per-customer kubeconfig; ships with the
§5.2 credential (EKS access entry `AmazonEKSViewPolicy` / GKE viewer). Encode the
update-kubeconfig path — it's undocumented internally.

### 5.2 `ackstorm_agent_readonly` cloud credential
No read-only role exists today (§1.3). Design on existing precedents: AWS role per customer
account, trust = **OIDC federation** (same mechanism as `ackstorm_gitlab`/`ackstorm_github`,
no SAML), policy = managed `ReadOnlyAccess` (+ `AmazonEKSViewPolicy` access entry), rolled
out via the same `ackstorm_iam` Terraform module `new_ack_roles.md` edits. GCP: SA with
`roles/viewer` + `roles/container.viewer`. Unblocks: #2's 25 blocked checks (budgets, PITR,
bucket versioning), #6 v2 (Budgets/CE/Recommender/BigQuery), #8 (ACM/RDS cert reads).
Sales-friendly framing: same pattern CloudCheckr already uses on customer accounts.

### 5.3 ackmetrics MCP (ecmanagedApi)
Base `https://api.ecmanaged.com/v2`, Falcon; auth `Authorization: <token>` (user key vs
ADMIN key; optional `admin-id:` header lifts bulk rate limits). v1 tools (read):
env get, alerts list (active), probe list/get, agent get, cloud_stat get, notification list,
timeperiod get, holiday check. v2 (policy-gated writes): timeperiod/maintenance-window
create; probe create. **Gotchas to bake into the wrapper**: validation errors return HTTP
**416**; `account_id` regex stricter than generic ids; `time_duration` needs `\d+[dhms]`;
holiday endpoint returns 400 for "not a holiday" (not an error); telegram_bot example is
`/v1` — verify; **alert C/U/D actually flows via the GitLab `alert-builder` pipeline**, not
raw REST — alert writes are a gitlab-mcp job, not an ackmetrics-mcp one.

### 5.4 alerta MCP (later)
Intake stays webhook→`generic` (no MCP needed to receive). API wrapper for: read alerts
(triage/bulk), then policy-gated `ack`/`blackout` once #4 earns trust. Hard rules from
`alerta/index.md`: blackout must carry reason=ticket ref; **never scope a blackout to
account+env only** (always extra selectors); prefer shelve for short silences. Schema from
OSS Alerta docs (not reproduced internally).

---

## 6. New agent candidates surfaced (beyond the 10)

| Candidate | Ground | 1-liner |
|---|---|---|
| MR Policy Linter [R] | gitops/terraform checklists | every MR: secret-shaped strings, ExternalSecrets-only, no `0.0.0.0/0`, no static cloud keys, PRO pins exact — the checklist rules as a pre-merge bot |
| Flux image-automation gate [W] | `fluxcd.md` image-update flow | review/merge bot for `flux-updates` branch MRs (patch/minor auto, major → human) |
| Budget-config auditor [R] | `configure_budgets.md` | cron: budget naming `ACK-<...>-<Period>` + threshold table compliance per account |
| Static-cred→OIDC migrator [W] | `terraform/getting_started.md §Migrate old pipelines` | sweep old repos still on AWS keys/GCP SA-keys → migration MRs |
| GCP anomaly poller [C] | `finops-docs.md` (GCP pushes nothing) | scheduled Recommender/Anomaly read → ticket; closes the GCP no-push gap |
| CUD/RI recalculator [R] | `google_recomendations.md` spreadsheet model | periodic steady-state usage → reservation-candidate report |
| Internal-docs QA (Docs Gardener internal mode) [W] | §7 findings | lint runbooks: secrets, broken H1s, contradictions, stub TODOs → MRs to documentacion-general |

Deliberately NOT agents: Nominalia renewals (no API), gitlab_helm/mysql/EKS-AL2023 upgrades
(high-blast-radius, human-gated by design), Landing Zone bootstrap (manual until its v2 pipeline).

## 7. Doc hygiene findings (feed Docs Gardener / fix now)

1. **SECURITY**: `runbooks/other/recover_specific_directories_backup.md` contains a
   **plaintext live-looking password** and a copy-pasted wrong H1 ("Cloudcheckr Configuration
   in AWS"). Rotate the credential + purge from git history. Human action, today.
2. `terraform/getting_started.md`: repo-path prose vs example contradiction (§1.2.3); GCP
   script var-name/echo mismatches (§1.2.4); GCP+GitLab pool-creation gap (§1.2.5).
3. Severity mapping differs between `alerta/index.md` and `ackmetrics/cloud_artifacts.md`.
4. `tools.md` ends in TODO stubs (k9s, OpenLens, kctx/kns, TFSwitch); kubectl-access runbook
   missing entirely (only in one customer doc).
5. `template/operations.md` + `terraform/operations.md` ≈ empty placeholders (catalog's
   cron-duty ideas can't cite them); `gitops/deep-dive/proxies/kong.md` is an empty stub.
6. `common_alerts` covers ~20 of the alert families listed in `alerta/index.md` — the rest
   have no procedure (limits #4's KB; each new procedure written = instant agent upgrade).
7. GCP budget monthly/quarterly thresholds exist only in screenshots, not text.

## 8. Revised build order

```
Sprint 0 (parallel infra): kubectl-ro MCP (§5.1) + agent_readonly role design (§5.2)
Sprint 1  #2 Handover Verifier   v0 = 57 checks (gitlab+kubectl-ro); MANUAL rows explicit
Sprint 2  #4 Alert First-Responder shadow on alerta.ackstorm.com — k8s/probe/cloudstats
          classes full, VM classes degraded-evidence; + ackmetrics MCP (§5.3) → #2 v1 (64)
          + write runbooks/support/zoho-desk.md (§4, unblocks ticket quality everywhere)
Sprint 3  #1 Platform Bootstrapper (7-step corrected flow) + #2 as a2a verifier
Sprint 4  #6 FinOps Reviewer v1 (GCP recos: Grafana+Recommender+templates)
Then      #3 #5 #7 #10 #9, #8-certs; #6 v2 + #2 v2 once agent_readonly lands
```

Unchanged from v0.1: #2 first (still the eval oracle + case-study), #4 second (recurring
ROI), dogfood-as-GTM logic. Changed: explicit infra sprint 0, #4 class-scoping, #6 GCP-first,
#8 certs-only, Zoho process-capture as a sprint-2 deliverable.
