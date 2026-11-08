// BiG360 console sample - read-only walk through the API:
//   1. liveness (/health) and readiness (/ready)
//   2. system status (data as-of, worker, ops alerts)
//   3. company lookup (search) -> Company 360 summary (overview)
//   4. screener query (POST, read-only)
//   5. market overview and earnings calendar
//   6. live SSE stream with cancellation (Ctrl+C or --stream-seconds)
//   7. retry demo against a closed port (--retry-demo)
//
// Usage:
//   dotnet run --project samples/BiG360.Console -- [--base-url http://127.0.0.1:8010/] [--ticker AAPL]
//        [--stream-seconds 20] [--no-stream] [--retry-demo] [--di] [--bearer]
//   --di      use IHttpClientFactory + Polly (normal net8.0 build only)
//   --bearer  send BIG360_BEARER_TOKEN as a bearer token (server with BIG360_AUTH=oidc)

using System.Diagnostics;
using BiG360.Client;
#if !BIG360_OFFLINE
using Microsoft.Extensions.DependencyInjection;
#endif

var baseUrl = Arg("--base-url") ?? Environment.GetEnvironmentVariable("BIG360_API_URL") ?? "http://127.0.0.1:8010/";
if (!baseUrl.EndsWith("/")) baseUrl += "/";
var ticker = Arg("--ticker") ?? "AAPL";
var streamSeconds = int.TryParse(Arg("--stream-seconds"), out var ss) ? ss : 20;
var noStream = Flag("--no-stream");
var useBearer = Flag("--bearer");

Console.OutputEncoding = System.Text.Encoding.UTF8;
using var cts = new CancellationTokenSource();
Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };

var options = new Big360ClientOptions
{
    BaseAddress = new Uri(baseUrl),
    UseBearerToken = useBearer,
    OnRetry = (n, why) => Console.WriteLine($"   retry #{n}: {why}"),
};
IAccessTokenProvider? tokens = useBearer ? new EnvironmentTokenProvider() : null;

Big360Client client;
#if !BIG360_OFFLINE
ServiceProvider? sp = null;
if (Flag("--di"))
{
    var services = new ServiceCollection();
    if (tokens is not null) services.AddSingleton(tokens);
    services.AddBig360Client(o =>
    {
        o.BaseAddress = options.BaseAddress;
        o.UseBearerToken = options.UseBearerToken;
        o.OnRetry = options.OnRetry;
    });
    sp = services.BuildServiceProvider();
    client = sp.GetRequiredService<Big360Client>();
    Console.WriteLine("client: IHttpClientFactory + Microsoft.Extensions.Http.Resilience (Polly v8)");
}
else
#endif
{
    client = Big360ClientFactory.Create(options, tokens);
    Console.WriteLine("client: Big360ClientFactory (SocketsHttpHandler + built-in retry)");
}
Console.WriteLine($"API: {baseUrl}  (client written for contract {Big360Client.ContractVersion})");

var exit = 0;
try
{
    // 1. probes
    var sw = Stopwatch.StartNew();
    var health = await client.GetHealthAsync(cts.Token);
    Console.WriteLine($"\n[health] {health.Status}  api {health.Version}  snapshot {health.ServingRunId}  ({sw.ElapsedMilliseconds} ms)");
    var served = await client.GetContractVersionAsync(cts.Token);
    Console.WriteLine($"[contract] served {served}; client {Big360Client.ContractVersion}; " +
                      (Big360Client.IsCompatible(served) ? "compatible" : "older server - newer fields / endpoints may be missing"));
    try
    {
        var ready = await client.GetReadyAsync(cts.Token);
        Console.WriteLine($"[ready]  {ready.Status}: " + string.Join("; ", ready.Checks.Select(c => $"{c.Name}={(c.Ok ? "ok" : "FAIL")}{(c.Required ? "" : " (info)")}")));
    }
    catch (Big360ApiException e) when (e.StatusCode == System.Net.HttpStatusCode.NotFound || (int)e.StatusCode == 501)
    {
        Console.WriteLine("[ready]  not available on this server (API < 1.31 - restart the API to load it)");
    }

    // 2. system status
    var st = await client.GetSystemStatusAsync(cts.Token);
    Console.WriteLine($"[status] api {st.Version}; data as of {st.DataAsOfOverall ?? st.ServingAsOf}; worker alive {st.Worker?.Alive}; " +
                      $"ops alerts {st.OpsAlerts?.Count ?? 0}");
    foreach (var a in (st.OpsAlerts ?? new List<OpsAlert>()).Take(3))
        Console.WriteLine($"         - {a.Severity}: {a.Title}");

    // 3. company lookup -> Company 360
    var hits = await client.SearchAsync(ticker, 5, cts.Token);
    Console.WriteLine($"\n[search] '{ticker}': " + string.Join(", ", hits.Select(h => $"{h.Ticker} ({h.Name}, {h.Mic ?? h.Country})")));
    var pick = hits.FirstOrDefault(h => string.Equals(h.Ticker, ticker, StringComparison.OrdinalIgnoreCase)) ?? hits.FirstOrDefault();
    if (pick is not null)
    {
        var ov = await client.GetCompanyOverviewAsync(pick.SecurityId, ct: cts.Token);
        Console.WriteLine($"[company] {ov.Security.Name} [{ov.Security.SecurityId}] as of {ov.AsOf}: close {ov.Quote?.Close} {ov.Quote?.Currency}, " +
                          $"composite {ov.Composite.Score:0.0} grade {ov.Composite.Grade} (rank {ov.Composite.RankGlobal}); " +
                          $"factors " + string.Join(" ", ov.Factors.Select(f => $"{f.FactorId}={f.Score:0}")));
        if (ov.Badges.Count > 0) Console.WriteLine($"          badges: {string.Join(", ", ov.Badges)}");
        Console.WriteLine($"          + {ov.AdditionalProperties?.Count ?? 0} more top-level fields kept as JSON (forward compatible)");
    }

    // 4. screener (read-only POST)
    var res = await client.RunScreenerAsync(new ScreenerQuery
    {
        UniverseId = "mvp_global",
        Regions = new List<string> { "EU" },
        MinMarketCapUsd = 10e9,
        Filters = new List<ScreenerFilter> { new() { Field = "roic", Op = "gte", Value = 0.15 } },
        Columns = new List<string> { "roic", "ev_ebit" },
        SortBy = "composite",
        SortDir = "desc",
        Limit = 5,
    }, cts.Token);
    Console.WriteLine($"\n[screener] EU, cap >= $10bn, ROIC >= 15%: {res.Total} matches as of {res.AsOf}; top {res.Rows.Count}:");
    foreach (var r in res.Rows)
        Console.WriteLine($"   {r.Rank,4} {r.Security.Ticker,-8} {Trunc(r.Security.Name, 32),-32} composite {r.Composite,6:0.0}  roic {Num(r.Values, "roic"):P1}");

    // 5. market overview + earnings calendar
    var mo = await client.GetMarketOverviewAsync("US", 5, cts.Token);
    Console.WriteLine($"\n[market] US as of {mo.AsOf}: " + string.Join("  ", mo.Indices.Take(4).Select(i => $"{i.Name} {i.Last:0.##} ({i.Change1D:+0.00%;-0.00%})")));
    Console.WriteLine($"         breadth: {mo.Breadth.Advancers} up / {mo.Breadth.Decliners} down; top gainer " +
                      (mo.Gainers.Count > 0 ? $"{mo.Gainers[0].Security.Ticker} {mo.Gainers[0].Change1D:+0.0%}" : "n/a"));
    var cal = await client.GetEarningsCalendarAsync(DateTime.Today, DateTime.Today.AddDays(14), region: "US", universe: "mvp_us",
        limit: 5, ct: cts.Token);
    Console.WriteLine($"[earnings] {cal.From}..{cal.To}: {cal.Total} events; first {cal.Items.Count}:");
    foreach (var e in cal.Items)
        Console.WriteLine($"   {e.Date} {e.Security.Ticker,-8} {e.EventType,-10} {e.Status,-9} date source: {e.SourceLabel ?? e.DateSource}");
    Console.WriteLine($"   note: {Trunc(cal.ModelNote, 110)}");

    // 6. SSE live stream with cancellation
    if (!noStream)
    {
        Console.WriteLine($"\n[live] subscribing to /api/v1/live/stream for {streamSeconds}s (Ctrl+C to stop) ...");
        using var streamCts = CancellationTokenSource.CreateLinkedTokenSource(cts.Token);
        streamCts.CancelAfter(TimeSpan.FromSeconds(streamSeconds));
        var sub = new LiveSubscriber(client, "filings,news,macro,alerts");
        var n = 0;
        await sub.RunAsync(ev =>
        {
            n++;
            Console.WriteLine($"   #{ev.Id} {ev.Topic}/{ev.Type} {ev.Key}");
            return Task.CompletedTask;
        }, status => Console.WriteLine($"   ({Trunc(status, 100)})"), streamCts.Token);
        Console.WriteLine($"[live] stopped after {streamSeconds}s: {n} data events, last id {sub.LastEventId?.ToString() ?? "none"}" +
                          " (no events is normal when the realtime layer is off; heartbeats prove the stream works)");
    }

    // 7. retry demo
    if (Flag("--retry-demo"))
    {
        Console.WriteLine("\n[retry] GET /health on a closed port (127.0.0.1:1) with 3 retries:");
        var bad = Big360ClientFactory.Create(new Big360ClientOptions
        {
            BaseAddress = new Uri("http://127.0.0.1:1/"), MaxRetries = 3, Timeout = TimeSpan.FromSeconds(90),
            OnRetry = options.OnRetry,
        });
        try
        {
            await bad.GetHealthAsync(cts.Token);
        }
        catch (HttpRequestException e)
        {
            Console.WriteLine($"   gave up after retries: {Trunc(e.Message, 90)}");
        }
        catch (TaskCanceledException) when (!cts.IsCancellationRequested)
        {
            Console.WriteLine("   gave up: overall HttpClient.Timeout reached during retries");
        }
    }
}
catch (Big360ApiException e)
{
    Console.Error.WriteLine($"API error {(int)e.StatusCode}: {e.Problem?.Title} - {e.Problem?.Detail} (X-Request-ID {e.RequestId})");
    exit = 2;
}
catch (HttpRequestException e)
{
    Console.Error.WriteLine($"cannot reach {baseUrl}: {e.Message}");
    exit = 3;
}
catch (OperationCanceledException)
{
    Console.WriteLine("cancelled");
}
#if !BIG360_OFFLINE
sp?.Dispose();
#endif
return exit;

string? Arg(string name)
{
    var a = Environment.GetCommandLineArgs();
    var i = Array.IndexOf(a, name);
    return i >= 0 && i + 1 < a.Length ? a[i + 1] : null;
}

bool Flag(string name) => Environment.GetCommandLineArgs().Contains(name);

static string Trunc(string? s, int n) => s is null ? "" : s.Length <= n ? s : s.Substring(0, n - 1) + "~";

static double? Num(Dictionary<string, System.Text.Json.JsonElement> values, string key) =>
    values.TryGetValue(key, out var v) && v.ValueKind == System.Text.Json.JsonValueKind.Number ? v.GetDouble() : null;
