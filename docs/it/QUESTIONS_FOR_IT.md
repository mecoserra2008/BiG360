# Decisions for BiG IT

This is the list of decisions IT needs to make before or during the move from the analyst PC (Stage A) to a department server (Stage B) and integration with the .NET estate (Stage C). Each row gives our recommendation and what depends on it. Nothing in Stage A is blocked by these.

| # | Decision | Options | Our recommendation | Depends on it |
|---|---|---|---|---|
| Q1 | **.NET version** for integration code | .NET 8 LTS (support to Nov 2026), .NET 10 LTS (Nov 2025, support to Nov 2028), .NET 9 STS | **The current LTS the bank standardises on.** If .NET 8 is the standard today, plan .NET 10 for new services, because .NET 8 support ends in November 2026. The sample targets net8.0 and is C# 10 compatible, so retargeting is a one-line change. | Sample client, YARP gateway, bridge service |
| Q2 | **On-premises or Azure** | (a) On-prem Windows Server VM. (b) Azure VM. (c) Azure Container Apps / AKS. | **(a) or (b): one Windows Server 2022 VM** (8–16 vCPU, 32–64 GB RAM, 500 GB SSD). It is the smallest change from today and needs no container platform. Revisit (c) only if many API consumers need horizontal scaling. | Hosting (DEPLOYMENT_SERVER.md §1), monitoring, backup |
| Q3 | **Identity** | Entra ID (OIDC / OAuth2), AD FS, Windows Integrated Auth (Kerberos / NTLM) | **Entra ID** app registration with app roles `BiG360.Viewer`, `.Analyst` and `.Admin`, mapped to AD groups. The API already validates Entra tokens (switched off today). Use AD FS only if the bank has no Entra tenant. | Gateway, per-user state, service-to-service calls |
| Q4 | **SQL Server standard** for shared user state | SQL Server 2019 / 2022 / 2025 on-prem, Azure SQL Database / Managed Instance; PostgreSQL if it is the standard | **SQL Server 2022 (or Azure SQL)**, database `BiG360`, schema `app`, Windows auth for a gMSA. The data is small (MB), so a shared DBA-managed instance is enough. The analytics stay in DuckDB / Parquet (read-mostly). | app_state migration (DEPLOYMENT_SERVER.md §6), backups |
| Q5 | **Message bus** for events | Azure Service Bus, RabbitMQ, Kafka (Confluent / Event Hubs Kafka endpoint), none | **Whichever the bank already runs.** For this event volume (tens to hundreds of messages a day), Service Bus or RabbitMQ is plenty. Start with **none**: consumers can read the SSE stream directly, and the bridge is added when a second consumer appears. | SSE bridge (INTEGRATION_DOTNET.md §2.2) |
| Q6 | **Container policy** | Allowed (which registry and platform?), not allowed | Not needed for Stage B. If containers are standard, a Linux image of the API plus worker is possible; `backend/Dockerfile` is a starting point. | Hosting option B |
| Q7 | **Python on servers** | Allowed with an internal PyPI mirror; allowed as a self-contained runtime only; not allowed | **Self-contained runtime.** The portable build ships an embedded Python 3.13 and locked wheels in one folder. Nothing is installed system-wide and patches come with releases. The SBOM lists every package. | Everything in Stage B. If Python is not allowed, the app stays on a workstation and .NET integrates through exports only (§3 of INTEGRATION_DOTNET.md). |
| Q8 | **Outbound proxy** | Explicit proxy (URL), PAC, authenticated (NTLM / Kerberos), TLS inspection | An explicit proxy URL for the service account, an allow-list of the source hosts (DATA_LICENSING.md), and **no authentication for the service account**, or a local forwarder. Provide the TLS-inspection root CA bundle. | All data refreshes |
| Q9 | **Market-data licences the bank holds** | Bloomberg (Terminal / B-PIPE / Data License), LSEG (Workspace / RDP / DataScope), FactSet, S&P, ICE, internal market-data service | **Mandatory decision before any multi-user use.** Yahoo/yfinance, AKShare and BaoStock must be switched off (not licensed). Polygon is plan-dependent. Tell us which feed and entitlement (internal display, derived data, number of users) covers end-of-day prices, corporate actions and fundamentals for US, EU, UK, JP, KR, CN/HK and LATAM. The adapter interface is ready (DATA_LICENSING.md §4). | Prices for non-US markets, benchmarks, futures curves, earnings dates, China coverage |
| Q10 | **Security review and change management** | Standard application onboarding (threat model, pen test, SAST / SCA), CAB process, release cadence | Treat it as an **internal research application, data class Internal / Confidential** (SECURITY.md §6). Deliverables ready for review: threat model, SBOM, configuration reference, runbook. Proposed cadence: a monthly release through CAB plus emergency fixes; data refreshes are operational, not changes. | Go-live of Stage B |
| Q11 | Monitoring stack | SCOM, Azure Monitor / Log Analytics, Grafana / Prometheus / Loki, Splunk | **The existing one.** All hooks are standard: `/health`, `/ready`, Prometheus `/metrics`, JSON log files with request ids (DEPLOYMENT_SERVER.md §8). | Alerting |
| Q12 | Backup tooling and RPO / RTO | Veeam, DPM, Azure Backup; RPO 24 h / RTO 1 day? | RPO 24 h and RTO 1 working day are adequate: weekly analytics plus small user state. SQL backups for app_state per DBA standard. | Backup design (DEPLOYMENT_SERVER.md §7) |
| Q13 | Gateway product and URL | YARP (ASP.NET Core) or IIS + ARR; dedicated host name or path prefix | **YARP**, with its own host name `big360.<bank domain>` and a certificate from the internal PKI. A path prefix also works (`BIG360_ROOT_PATH`). | TLS, auth, rate limits |
| Q14 | Who operates it | The analyst team (business-owned app) or IT run teams | Business-owned, with IT hosting the server, gateway, SQL and backups. The RUNBOOK covers both roles. | Support model |
| Q15 | Software deployment | SCCM / Intune package, manual install, a share with the update check | **SCCM / Intune** for the server and any workstation installs (the per-user Inno Setup installer is silent-install capable). The in-app "update available" check reads a share. | Releases |
| Q16 | Hugging Face model weights | Download at runtime, or mirror internally | **Mirror internally** (artefact store or file share) and set `HF_HUB_OFFLINE=1`. The model licences need confirming (DATA_LICENSING.md §2.7). | NLP sentiment and central-bank tone jobs |

**What we need from IT to start Stage B.** Answers to Q2, Q3, Q4, Q7, Q8 and Q9, plus:
- a server, service account and SQL database;
- an Entra app registration with the three app roles;
- the proxy allow-list;
- a certificate.

**Effort on our side once those exist:**

| Item | Estimate |
|---|---|
| Server install, service, gateway | 2–3 days |
| app_state on SQL Server (DEPLOYMENT_SERVER.md §6) | 5–8 days |
| Licensed vendor adapter | 2–5 days per vendor |
| UI sign-in through the gateway (BFF) | 1–2 days |
