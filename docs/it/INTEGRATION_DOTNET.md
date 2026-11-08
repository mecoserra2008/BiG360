# Integrating BiG Equity 360 with the .NET estate

**Audience:** .NET developers and integration architects.
**Principle:** Python stays the analytics engine. .NET integrates through stable contracts; no rewrite is planned.

**Reading order:**
1. §1: the REST contract and a typed client. This is the default and covers most needs.
2. §2: events.
3. §3: bulk data.
4. §4: gateway.
5. §5: gRPC (rarely).
6. §7: compatibility rules (read before building anything).

A working sample solution is in [`integration/dotnet/BiG360.Client.Sample`](../../integration/dotnet/BiG360.Client.Sample/README.md). It targets .NET 8 and was verified on .NET 6 (appendix A).

| # | Pattern | Use when | Latency | Coupling |
|---|---|---|---|---|
| 1 | REST + OpenAPI client (NSwag / Kiota), HttpClientFactory, Polly, MSAL | Any service or UI needing scores, screens, Company 360 data, macro, calendars | 5–300 ms per call | Contract only |
| 2 | SSE live stream, bridged to Service Bus / RabbitMQ / Kafka | React to new filings, insider buys, macro surprises or alerts | Seconds | Contract + bus topic |
| 3 | Data-level: Parquet / DuckDB.NET / SQL Server bulk load | Reporting, risk, BI (Power BI), joins with internal data | Batch (weekly / daily) | Export contract |
| 4 | YARP gateway in front | Stage B: auth, TLS, routing, rate limits, one URL | +1–2 ms | Infrastructure |
| 5 | gRPC | Only for sub-10 ms service-to-service calls at high volume | < 10 ms | Strong (proto) |

---

## 1. REST through the OpenAPI contract (recommended)

**Contract facts:**
- **File:** `contracts/api.yaml`, OpenAPI 3.1, `info.version` 1.31.0.
- **Served:** live at `GET /openapi.json`, with Swagger UI at `/docs`.
- **Size:** 175 paths. All data endpoints are `GET` except a few read-only `POST` computations, such as `POST /api/v1/screener/query`, and the user-state writes.
- **Formats:** JSON with snake_case names. Dates are `yyyy-MM-dd` and timestamps are ISO-8601 UTC. Percent-like values are fractions (0.12 = 12 %). Scores run 0–100.
- **Errors:** every error is `application/problem+json` (RFC 7807: `type`, `title`, `status`, `detail`, `errors[]`).
- **Caching:** snapshot-backed `GET`s carry `ETag`, `Last-Modified` and `X-Serving-Run-Id`. A matching `If-None-Match` returns 304.
- **Correlation:** every response has `X-Request-ID`. Send your own (a safe token of 128 characters or fewer) and the API logs it.

### 1.1 Generate a client

The contract is OpenAPI **3.1** (nullable is written `type: [string, "null"]`).
- **Kiota** reads 3.1.
- **NSwag**'s 3.1 support is partial. Down-convert first with `scripts/openapi_31_to_30.py`, which writes `integration/dotnet/api.v30.json`, and generate from that file.

```powershell
# Kiota (Microsoft): idiomatic, request builders, IAuthenticationProvider
dotnet tool install --global Microsoft.OpenApi.Kiota
kiota generate -l CSharp -d contracts\api.yaml -c Big360ApiClient -n Bank.Big360.Client -o src\Bank.Big360.Client

# NSwag: one class per operationId, System.Text.Json DTOs
.venv\Scripts\python.exe scripts\openapi_31_to_30.py
dotnet tool install --global NSwag.ConsoleCore
nswag run integration\dotnet\BiG360.Client.Sample\nswag.json /runtime:Net80
```

Pin the contract version you generated from. The CI step below fails the build when the served contract is incompatible:

```csharp
var served = await client.GetContractVersionAsync(ct);   // GET /openapi.json -> info.version
if (!Big360Client.IsCompatible(served))                   // same major, minor >= the client's
    throw new InvalidOperationException($"BiG360 API {served} is older than client {Big360Client.ContractVersion}");
```

### 1.2 Register with HttpClientFactory, Polly and MSAL

This is ASP.NET Core / Worker Service `Program.cs` (.NET 8). It uses the packages `Microsoft.Extensions.Http.Resilience` (Polly v8) and `Microsoft.Identity.Web` or `Azure.Identity`.

```csharp
using Azure.Core;
using Azure.Identity;
using BiG360.Client;
using Microsoft.Extensions.Http.Resilience;
using Polly;

var builder = WebApplication.CreateBuilder(args);
var cfg = builder.Configuration.GetSection("Big360");   // BaseAddress, Scope, UseBearerToken

// Entra ID token for the BiG360 API app registration (client credentials or managed identity).
builder.Services.AddSingleton<TokenCredential>(new DefaultAzureCredential());
builder.Services.AddSingleton<IAccessTokenProvider>(sp => new AzureIdentityTokenProvider(
    sp.GetRequiredService<TokenCredential>(), cfg["Scope"]!));        // e.g. "api://big360/.default"

builder.Services.AddBig360Client(o =>                    // from BiG360.Client (sample library)
{
    o.BaseAddress = new Uri(cfg["BaseAddress"]!);        // https://big360.bank.internal/ (the YARP gateway)
    o.UseBearerToken = cfg.GetValue<bool>("UseBearerToken");
    o.MaxRetries = 3;
});

// Equivalent hand-rolled registration for a generated (NSwag / Kiota) client:
builder.Services.AddHttpClient<IBig360GeneratedClient, Big360GeneratedClient>(c =>
        c.BaseAddress = new Uri(cfg["BaseAddress"]!))
    .AddHttpMessageHandler<RequestIdHandler>()
    .AddHttpMessageHandler<BearerTokenHandler>()
    .AddResilienceHandler("big360", p =>
    {
        p.AddTimeout(TimeSpan.FromMinutes(2));                                  // total
        var retry = new HttpRetryStrategyOptions
        {
            MaxRetryAttempts = 3, BackoffType = DelayBackoffType.Exponential, UseJitter = true,
            Delay = TimeSpan.FromMilliseconds(500), ShouldRetryAfterHeader = true,
        };
        retry.DisableForUnsafeHttpMethods();                                    // never replay writes
        p.AddRetry(retry);
        p.AddCircuitBreaker(new HttpCircuitBreakerStrategyOptions
            { FailureRatio = 0.5, MinimumThroughput = 10, SamplingDuration = TimeSpan.FromSeconds(60),
              BreakDuration = TimeSpan.FromSeconds(30) });
        p.AddTimeout(TimeSpan.FromSeconds(60));                                 // per attempt
    });

sealed class AzureIdentityTokenProvider : IAccessTokenProvider
{
    private readonly TokenCredential _cred; private readonly string[] _scopes;
    public AzureIdentityTokenProvider(TokenCredential cred, string scope) { _cred = cred; _scopes = new[] { scope }; }
    public async Task<string> GetTokenAsync(CancellationToken ct) =>
        (await _cred.GetTokenAsync(new TokenRequestContext(_scopes), ct)).Token;   // cached and refreshed by Azure.Identity
}
```

**Notes:**
- **Retries.** Retry only idempotent calls. `POST /screener/query` is read-only, but generic policies treat every POST as unsafe. To retry it, add an explicit predicate.
- **Timeouts.** Endpoints that aggregate heavily can take 1–3 s on a cold cache, for example market overview, the events map and backtests. Use a 60 s attempt timeout and 2 min in total. Typical p95 is under 300 ms.
- **Status codes:**
  - 501 means "defined in the contract, not implemented on this server". Do not retry it.
  - 503 from `/ready` means the server is not ready.
  - 401 / 403 appear only when the server runs with `BIG360_AUTH=oidc`.
- **MSAL directly** (without Azure.Identity): see `MsalTokenProvider` in the sample (`ConfidentialClientApplicationBuilder.AcquireTokenForClient`). For a desktop or WPF client, use `PublicClientApplicationBuilder` with WAM and `AcquireTokenSilent`, falling back to `AcquireTokenInteractive`.

### 1.3 Typical calls

```csharp
var hits = await big360.SearchAsync("ASML", 5, ct);                                   // searchSecurities
var ov   = await big360.GetCompanyOverviewAsync(hits[0].SecurityId, ct: ct);         // getCompanyOverview
var scr  = await big360.RunScreenerAsync(new ScreenerQuery {                          // runScreener
    UniverseId = "mvp_global", Regions = new() { "EU" }, MinMarketCapUsd = 10e9,
    Filters = new() { new() { Field = "roic", Op = "gte", Value = 0.15 } },
    Columns = new() { "roic", "ev_ebit" }, SortBy = "composite", Limit = 50 }, ct);
var mkt  = await big360.GetMarketOverviewAsync("US", ct: ct);                        // getMarketOverview
var cal  = await big360.GetEarningsCalendarAsync(DateTime.Today, DateTime.Today.AddDays(14),
                                                 region: "US", compact: true, ct: ct); // getEarningsCalendar
```

**Labelling rules for downstream displays:**
- Several outputs are **model-based** and must stay labelled as such: composite and factor scores, fair value, ML ranks, earnings expectations ("not consensus"), macro expectations and regime.
- The API carries the labels, for example `model_note`, `source_label` and `model_based: true`.
- `data_status = pending` means the upstream tables are empty. Placeholders in that case must not be displayed.

## 2. Event streaming: the SSE live stream

`GET /api/v1/live/stream` is a standard Server-Sent Events stream (`text/event-stream`).

**Frames:**
- `id: <bus id>`: global, strictly increasing.
- `event: <topic>`: one of filings, news, events, macro, alerts or signals.
- `data: <LiveStreamEvent JSON>`.
- Control frames: `hello` (first), `heartbeat` (every `heartbeat_s`, default 15 s, with no id) and `retry: 3000`.

**Resuming:**
- Send the `Last-Event-ID` header to resume.
- Messages are retained for: filings 14 d, news 7 d, events 72 h, macro 60 d, alerts 30 d.
- Query parameters: `topics`, `types`, `replay` (the newest N on first connect) and `heartbeat_s`.

The stream needs the realtime layer (`worker --realtime`) to carry data. Without it, the stream still connects and sends heartbeats.

### 2.1 Consuming with HttpClient

`Big360Client.StreamLiveAsync` returns `IAsyncEnumerable<SseFrame>`. `LiveSubscriber` adds reconnect with back-off and `Last-Event-ID` resume. The core of it:

```csharp
using var req = new HttpRequestMessage(HttpMethod.Get, "api/v1/live/stream?topics=filings,macro,alerts");
req.Headers.Accept.ParseAdd("text/event-stream");
if (lastId is not null) req.Headers.TryAddWithoutValidation("Last-Event-ID", lastId.ToString());
using var resp = await http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct);   // do not buffer
resp.EnsureSuccessStatusCode();
using var reader = new StreamReader(await resp.Content.ReadAsStreamAsync(ct));
string? line; string evt = "message", data = "";
while ((line = await reader.ReadLineAsync(ct)) is not null)
{
    if (line.Length == 0) { if (data.Length > 0) Handle(evt, data); evt = "message"; data = ""; continue; }
    if (line.StartsWith("event:")) evt = line[6..].Trim();
    else if (line.StartsWith("data:")) data += line[5..].TrimStart();
    else if (line.StartsWith("id:")) lastId = long.Parse(line[3..].Trim());
}
```

Use a dedicated `HttpClient` (or named client) with `Timeout = Timeout.InfiniteTimeSpan` for the stream. Behind a proxy or gateway, disable response buffering for this route (§4).

### 2.2 Bridge to the enterprise bus

Run the subscriber as a `BackgroundService`. It publishes each event to the bus the bank already runs and checkpoints `LastEventId` after the bus acknowledges. Delivery is at-least-once; consumers deduplicate on `MessageId = <bus id>`.

```csharp
public sealed class Big360BusBridge : BackgroundService
{
    private readonly Big360Client _client; private readonly IEventSink _sink; private readonly ICheckpointStore _cp;
    public Big360BusBridge(Big360Client c, IEventSink s, ICheckpointStore cp) { _client = c; _sink = s; _cp = cp; }

    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        var sub = new LiveSubscriber(_client, "filings,news,macro,alerts,signals", await _cp.LoadAsync(ct));
        await sub.RunAsync(async ev =>
        {
            await _sink.PublishAsync(ev, ct);           // bus ack first ...
            await _cp.SaveAsync(ev.Id, ct);             // ... then checkpoint (SQL row / blob / file)
        }, status => { /* log */ }, ct);
    }
}

// Azure Service Bus (Azure.Messaging.ServiceBus): one topic, subscription filters on Subject = topic/type
sealed class ServiceBusSink : IEventSink
{
    private readonly ServiceBusSender _s;
    public ServiceBusSink(ServiceBusClient c) => _s = c.CreateSender("big360-live");
    public Task PublishAsync(LiveStreamEvent e, CancellationToken ct) => _s.SendMessageAsync(new ServiceBusMessage(
        BinaryData.FromObjectAsJson(e)) { MessageId = e.Id.ToString(), Subject = $"{e.Topic}/{e.Type}",
        ContentType = "application/json", ApplicationProperties = { ["key"] = e.Key } }, ct);
}   // enable duplicate detection on the topic (MessageId) for exactly-once within the window

// RabbitMQ (RabbitMQ.Client 7): topic exchange, routing key = topic.type
sealed class RabbitSink : IEventSink
{
    private readonly IChannel _ch;
    public RabbitSink(IChannel ch) => _ch = ch;     // await ch.ExchangeDeclareAsync("big360.live", ExchangeType.Topic, durable: true)
    public async Task PublishAsync(LiveStreamEvent e, CancellationToken ct) =>
        await _ch.BasicPublishAsync("big360.live", $"{e.Topic}.{e.Type}", mandatory: false,
            new BasicProperties { MessageId = e.Id.ToString(), ContentType = "application/json", Persistent = true },
            JsonSerializer.SerializeToUtf8Bytes(e), ct);   // use publisher confirms (CreateChannelOptions) before checkpointing
}

// Kafka (Confluent.Kafka): topic per BiG360 topic, key = natural key (ordering per entity)
sealed class KafkaSink : IEventSink
{
    private readonly IProducer<string, string> _p;   // EnableIdempotence = true, Acks = All
    public KafkaSink(IProducer<string, string> p) => _p = p;
    public Task PublishAsync(LiveStreamEvent e, CancellationToken ct) => _p.ProduceAsync($"big360.{e.Topic}",
        new Message<string, string> { Key = e.Key, Value = JsonSerializer.Serialize(e),
            Headers = new Headers { { "big360-id", BitConverter.GetBytes(e.Id) } } }, ct);
}
```

**Design notes:**
- Run **one bridge instance**, active/passive. The SSE ids are global, so a single checkpoint is enough.
- If the bridge is down longer than the retention window, replay what is still retained. After that, read the REST `GET /api/v1/live/{topic}` endpoints, which return the latest N items merged with the snapshot.
- Event payloads carry identifiers (`security_id`, `issuer_id`, `filing_id`, `series_id`), so consumers can call the REST API for detail.

## 3. Data-level access (reporting, risk, BI)

**Integrate against the export contract, not the snapshot's internal tables.** The serving snapshot's internal tables can change between releases. The export contract is versioned (`EXPORT_VERSION` 1.0) and additive-only, like the API.

### 3.1 Export from the snapshot

```powershell
cd backend
..\.venv\Scripts\python.exe -m app.ops.export --out \\fileserver\big360\exports --format parquet
..\.venv\Scripts\python.exe -m app.ops.export --out D:\exports --format csv --datasets securities,fundamentals
..\.venv\Scripts\python.exe -m app.ops.export --out D:\exports --format parquet --datasets prices_daily --prices-since 2025-01-01
```

The export opens the current snapshot read-only, uses about 2 GB of RAM and never blocks the API. It writes the following to `<out>/<snapshot run id>/`:

| Dataset | Rows (2026-10-19) | Key | Content |
|---|---|---|---|
| `securities` | 24,961 | security_id | Identity (ISIN / FIGI / LEI / CUSIP / MIC), latest quote and returns, valuation multiples, 10 factor scores, composite / grade / rank, moat, fair value |
| `prices_daily` | 65.8 M (15 y) | security_id, date | OHLCV in the quote currency, adjustment factors, USD close |
| `fundamentals` | 15.0 M | issuer_id, item_id, period_kind, period_end, knowledge_date | Canonical statement items, point-in-time |
| `metrics` | 3.2 M | issuer_id, metric_id, period_kind, period_end | Ratios and sector KPIs |

It also writes `manifest.json` and `create_tables.sql`:
- `manifest.json`: snapshot run id and as-of, row counts, and columns with their DuckDB and SQL Server types.
- `create_tables.sql`: T-SQL DDL in schema `big360` with primary keys.

CSV files are UTF-8, comma-separated, RFC 4180 quoted, with ISO dates, a `.` decimal separator and an empty field for NULL. SQL Server 2017+ loads them with `BULK INSERT … WITH (FORMAT = 'CSV', FIRSTROW = 2, CODEPAGE = '65001')`.

Run the export after each refresh: as a Windows scheduled task, or as a job added to the weekly DAG.

### 3.2 Load into SQL Server

**From Parquet with DuckDB.NET.** DuckDB's data reader is an `IDataReader`, so `SqlBulkCopy` can stream it with no intermediate file:

```csharp
using DuckDB.NET.Data;                 // DuckDB.NET.Data.Full
using Microsoft.Data.SqlClient;

await using var duck = new DuckDBConnection("Data Source=:memory:");
await duck.OpenAsync(ct);
await using var cmd = duck.CreateCommand();
cmd.CommandText = @"SELECT * FROM read_parquet('\\fileserver\big360\exports\R20261020T2112319CSG\securities.parquet')";
await using var reader = await cmd.ExecuteReaderAsync(ct);

await using var sql = new SqlConnection(cs);
await sql.OpenAsync(ct);
using var bulk = new SqlBulkCopy(sql, SqlBulkCopyOptions.TableLock, null)
    { DestinationTableName = "big360.securities_stage", BatchSize = 50_000, BulkCopyTimeout = 0, EnableStreaming = true };
for (var i = 0; i < reader.FieldCount; i++) bulk.ColumnMappings.Add(reader.GetName(i), reader.GetName(i));
await bulk.WriteToServerAsync(reader, ct);
// then, in one transaction: TRUNCATE big360.securities; INSERT ... SELECT FROM securities_stage  (or ALTER TABLE ... SWITCH)
```

**Other routes:**
- **Without DuckDB.NET:** Parquet.Net (`ParquetReader`) or the CSV files with `BULK INSERT`.
- **SQL Server 2022:** `OPENROWSET(BULK '…parquet', FORMAT = 'PARQUET')` against an S3-compatible or Azure Blob location.
- **Power BI:** connect to the SQL Server tables. Alternatively, the Parquet folder works directly with the Parquet connector.

**Reading the live snapshot.** A .NET process can open the live snapshot with `DuckDBConnection("Data Source=<path>;ACCESS_MODE=READ_ONLY")`, because DuckDB allows several read-only processes. Reserve this for ad-hoc analysis, for two reasons:
- The file changes name at every refresh (`data/db/CURRENT`).
- Its tables are internal.

## 4. A YARP gateway in front (Stage B)

A small ASP.NET Core app with **YARP** (`Yarp.ReverseProxy`) gives one TLS endpoint and does the following:
- **Authentication:** Entra ID, through `Microsoft.Identity.Web`.
- **Routing:** `/api` and `/openapi.json` go to Python; `/` goes to the built UI.
- **Rate limits.**
- **Header handling:** forwarded headers, plus an `X-Request-ID` when the caller sent none.
- **Shielding:** the Python process stays off the network, because it binds to `127.0.0.1` on the same host or a private interface.

`appsettings.json`:

```json
{
  "AzureAd": { "Instance": "https://login.microsoftonline.com/", "TenantId": "<tenant-id>",
               "ClientId": "<gateway-app-id>", "Audience": "api://big360" },
  "ReverseProxy": {
    "Routes": {
      "live":   { "ClusterId": "big360", "Order": 1, "AuthorizationPolicy": "viewer",
                  "Match": { "Path": "/api/v1/live/stream" },
                  "Metadata": { "Big360.Streaming": "true" } },
      "api":    { "ClusterId": "big360", "Order": 10, "AuthorizationPolicy": "viewer", "RateLimiterPolicy": "per-user",
                  "Match": { "Path": "/api/{**rest}" } },
      "probes": { "ClusterId": "big360", "Order": 5, "AuthorizationPolicy": "anonymous",
                  "Match": { "Path": "/{probe:regex(^(health|ready)$)}" } },
      "ui":     { "ClusterId": "big360", "Order": 100, "Match": { "Path": "/{**rest}" } }
    },
    "Clusters": {
      "big360": {
        "HttpRequest": { "ActivityTimeout": "00:10:00" },
        "HealthCheck": { "Active": { "Enabled": true, "Interval": "00:00:15", "Path": "/ready", "Policy": "ConsecutiveFailures" } },
        "Destinations": { "py": { "Address": "http://127.0.0.1:8010/" } }
      }
    }
  }
}
```

`Program.cs`:

```csharp
builder.Services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
    .AddMicrosoftIdentityWebApi(builder.Configuration.GetSection("AzureAd"));
builder.Services.AddAuthorization(o =>
{
    o.AddPolicy("viewer", p => p.RequireRole("BiG360.Viewer", "BiG360.Analyst", "BiG360.Admin"));
    o.AddPolicy("analyst", p => p.RequireRole("BiG360.Analyst", "BiG360.Admin"));
});
builder.Services.AddRateLimiter(o => o.AddPolicy("per-user", ctx => RateLimitPartition.GetTokenBucketLimiter(
    ctx.User.FindFirst("oid")?.Value ?? ctx.Connection.RemoteIpAddress?.ToString() ?? "anon",
    _ => new TokenBucketRateLimiterOptions { TokenLimit = 60, TokensPerPeriod = 20, ReplenishmentPeriod = TimeSpan.FromSeconds(1),
                                             QueueLimit = 20 })));
builder.Services.AddReverseProxy().LoadFromConfig(builder.Configuration.GetSection("ReverseProxy"))
    .AddTransforms(t => t.AddRequestTransform(c =>
    {
        if (!c.ProxyRequest.Headers.Contains("X-Request-ID"))
            c.ProxyRequest.Headers.TryAddWithoutValidation("X-Request-ID", c.HttpContext.TraceIdentifier);
        return ValueTask.CompletedTask;
    }));
var app = builder.Build();
app.UseAuthentication(); app.UseAuthorization(); app.UseRateLimiter();
app.MapReverseProxy(p => p.Use(async (ctx, next) =>
{
    if (ctx.GetReverseProxyFeature().Route.Config.Metadata?.ContainsKey("Big360.Streaming") == true)
        ctx.Features.Get<IHttpResponseBodyFeature>()?.DisableBuffering();        // SSE: flush each frame
    await next();
}));
app.Run();
```

**Two authentication options:**
- **Gateway-only** (simplest): the gateway validates the token and the Python API stays on `BIG360_AUTH=none`, reachable only from the gateway host.
- **Defence in depth** (recommended): the gateway forwards the bearer token and the API also validates it, with `BIG360_AUTH=oidc` and the same tenant and audience. The API enforces its own role policy and logs the user (`principal` in the access log). Per-user watchlists need this option ([DEPLOYMENT_SERVER.md](DEPLOYMENT_SERVER.md) §6).

**Path prefix.** To publish under a prefix such as `https://apps.bank.internal/big360/`, have YARP strip the prefix (`"Transforms": [{ "PathRemovePrefix": "/big360" }]`) and set `BIG360_ROOT_PATH=/big360`.

**IIS/ARR** works too: URL Rewrite plus ARR proxy. For the SSE route, set `responseBufferLimit="0"` and disable dynamic compression. YARP is preferred because it runs in the .NET team's normal tooling.

## 5. When gRPC makes sense

Today it does not. REST p95 is under 300 ms against a read-only snapshot, and the data changes weekly or daily, not per tick. gRPC would add a second contract (`.proto`), HTTP/2 end-to-end through the proxies, and a Python gRPC server process.

Consider it only if both of these hold:
1. A .NET service needs **sub-10 ms** synchronous calls at high rates (for example, pre-trade checks that look up scores for every order).
2. The data is fresh enough for that use, which requires intraday prices (R2) and a licensed real-time feed.

Even then, a cheaper first step is a .NET-side cache, such as an in-memory dictionary refreshed from the export (§3) or from the `ETag`-aware REST calls. If gRPC is adopted:
- Generate the `.proto` from the same contract, a subset of operations.
- Serve it from Python with `grpcio` alongside FastAPI.
- Keep REST as the source of truth.

## 6. Mapping identifiers

| BiG360 id | Meaning | Map to |
|---|---|---|
| `security_id` | A listing line. Stable across ticker changes; split when an identifier is reused. | ISIN + MIC, or FIGI (exported in `securities`) |
| `issuer_id` | The issuer (legal entity) | LEI (`securities.lei`) and CIK for US issuers |
| `series_id` | Macro series | Publisher's code in `macro_catalog.yaml` |
| `filing_id` | `<source>:<accession>` | Regulator's filing id |

Use ISIN, FIGI or LEI to join with the bank's instrument master, never tickers.

## 7. Versioning and compatibility rules

These rules follow from the additive-only contract (`docs/CONTRACT_CHANGES.md`).

1. **Semantic version in `info.version`:**
   - A **minor** bump (1.30 → 1.31) adds things only: new paths, new optional query parameters, new optional response fields, new values in output enums.
   - A **major** bump (2.0) is the only way to rename, remove or retype something, and it is announced in the change log.
   - Within major version 1, nothing is removed.
2. **Be a tolerant reader.**
   - Ignore unknown JSON fields: `[JsonExtensionData]`, or `JsonSerializerOptions` without `UnmappedMemberHandling.Disallow`.
   - Treat output enums as open strings. Do not map them to C# enums that throw on unknown values; if you use C# enums, add an `Unknown` fallback converter.
   - Treat every field marked `[x, "null"]` as nullable.
3. **Do not depend on field order, array length or undocumented fields.** Paging: use `limit`, `offset` and `total`.
4. **Check the served version at start-up** (`GET /openapi.json`, `info.version`). A server whose minor version is older than your client's may not have newer endpoints:
   - 404 for paths outside the contract.
   - 501 for paths in the contract that are not implemented.
5. **Probes are stable:** `/health`, `/ready` and `/metrics` stay at the root, outside `/api/v1`.
6. **Major versions** would be served side by side under `/api/v2` for a deprecation period agreed with IT. `/api/v1` stays until consumers have migrated.
7. **The export contract** (§3) follows the same rules, with its own version (`EXPORT_VERSION` in `manifest.json`).
8. **Events** (§2): `LiveStreamEvent` fields are additive too. Topic names and `type` values can be added but are never renamed.

---

## Appendix A: sample run on this PC (2026-10-20)

**Environment:**
- .NET 6.0.100 SDK. .NET 8 is not installed.
- Build: `dotnet build -p:Big360TargetFramework=net6.0 -p:Big360Offline=true`, which succeeded with 0 warnings.
- Run against the live local API at `http://127.0.0.1:8010` (contract 1.31.0), read-only.
- The console prints numbers in the pt-PT locale.

```
[health] ok  api 0.1.0  snapshot R20261020T2112319CSG  (99 ms)
[contract] served 1.31.0; client 1.31.0; compatible
[ready]  ready: serving_snapshot=ok; worker_heartbeat=ok; gap_filler=ok (info)
[status] api 0.1.0; data as of 2026-10-19; worker alive True; ops alerts 2
[search] 'AAPL': AAPL (Apple Inc., XNAS), AAP (Advance Auto Parts Inc, XNYS), AAL (American Airlines Group Inc., XNAS)
[company] Apple Inc. [SS8MA4AQCCX39] as of 2026-10-19: close 342,94 USD, composite 69,4 grade A (rank 43); ...
[screener] EU, cap >= $10bn, ROIC >= 15%: 34 matches as of 2026-10-19; top 5: RACE.MI, EQNR.OL, PRX.AS, ASML.AS, AMS.MC
[market] US as of 2026-10-19: S&P 500 7690,73 (+0,41%)  breadth: 1750 up / 3591 down
[earnings] 2026-10-20..2026-11-03: 348 events (date source labelled, e.g. "Provider estimate (Yahoo)")
[live] connected (hello, last_event_id 8781), heartbeat received, cancelled after 20 s (realtime layer off: no data events)
[retry] closed port: retry #1..#3 with back-off, then gave up
```

**After .NET 8 is installed,** run from `integration\dotnet\BiG360.Client.Sample`:

```powershell
dotnet restore
dotnet build -c Release
dotnet run --project samples/BiG360.Console -c Release -- --stream-seconds 20 --retry-demo
dotnet run --project samples/BiG360.Console -c Release -- --di --no-stream          # IHttpClientFactory + Polly path
dotnet build -c Release -p:Big360UseMsal=true                                        # with the MSAL token provider
```
