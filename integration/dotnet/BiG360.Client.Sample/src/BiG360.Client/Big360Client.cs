// Thin typed client for the BiG Equity 360 API (contracts/api.yaml 1.31.0).
//
// Hand-written for the key endpoints because NSwag / Kiota were not available offline when this sample was made;
// the README gives the codegen commands that produce a full client from the same contract. The public surface here
// (one method per operationId) matches what a generated client exposes, so callers can switch later.
//
// HttpClient is injected (HttpClientFactory in the DI path, or new HttpClient(handlers) in the offline path).
// Errors: non-2xx responses throw Big360ApiException carrying the RFC 7807 Problem body and the X-Request-ID.

using System.Net;
using System.Net.Http.Json;
using System.Runtime.CompilerServices;
using System.Text;
using System.Text.Json;

namespace BiG360.Client;

public sealed class Big360ClientOptions
{
    /// <summary>API base address, e.g. http://127.0.0.1:8010/ (local app) or https://big360.bank.internal/ (gateway).</summary>
    public Uri BaseAddress { get; set; } = new("http://127.0.0.1:8010/");
    /// <summary>Per-request timeout for normal calls (the SSE stream is not subject to it).</summary>
    public TimeSpan Timeout { get; set; } = TimeSpan.FromSeconds(60);
    /// <summary>Send a bearer token (OIDC / Entra ID). Off by default: the local app has no authentication.</summary>
    public bool UseBearerToken { get; set; }
    /// <summary>Entra ID scope for the client-credentials flow, e.g. api://big360/.default</summary>
    public string? Scope { get; set; }
    public string? TenantId { get; set; }
    public string? ClientId { get; set; }
    /// <summary>Client secret: read it from Key Vault / Windows Credential Manager / environment, never from source.</summary>
    public string? ClientSecret { get; set; }
    /// <summary>Retries on transient failures (5xx except 501, 408, 429, network errors). GET/HEAD only by default.</summary>
    public int MaxRetries { get; set; } = 3;
    /// <summary>Optional log hook called before each retry (attempt number, reason).</summary>
    public Action<int, string>? OnRetry { get; set; }
}

public sealed class Big360ApiException : Exception
{
    public HttpStatusCode StatusCode { get; }
    public Problem? Problem { get; }
    public string? RequestId { get; }

    public Big360ApiException(HttpStatusCode status, Problem? problem, string? requestId, string message)
        : base(message)
    {
        StatusCode = status;
        Problem = problem;
        RequestId = requestId;
    }
}

public sealed class Big360Client
{
    /// <summary>Contract version this client was written against (info.version of contracts/api.yaml).</summary>
    public const string ContractVersion = "1.31.0";

    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web)
    {
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull,
    };

    private readonly HttpClient _http;

    public Big360Client(HttpClient http)
    {
        _http = http ?? throw new ArgumentNullException(nameof(http));
        _http.DefaultRequestHeaders.Accept.ParseAdd("application/json");
    }

    public HttpClient HttpClient => _http;

    // ------------------------------------------------------------------ probes / system
    /// <summary>GET /health (getHealth) - liveness.</summary>
    public Task<Health> GetHealthAsync(CancellationToken ct = default) => GetAsync<Health>("health", ct);

    /// <summary>GET /ready (getReady) - readiness; 503 is returned as a Readiness with Status = not_ready.</summary>
    public async Task<Readiness> GetReadyAsync(CancellationToken ct = default)
    {
        using var resp = await _http.GetAsync("ready", ct).ConfigureAwait(false);
        if (resp.StatusCode == HttpStatusCode.ServiceUnavailable || resp.IsSuccessStatusCode)
            return (await resp.Content.ReadFromJsonAsync<Readiness>(Json, ct).ConfigureAwait(false))!;
        throw await ErrorAsync(resp, ct).ConfigureAwait(false);
    }

    /// <summary>info.version of the served contract (GET /openapi.json) - check it at start-up: same major = compatible.</summary>
    public async Task<string> GetContractVersionAsync(CancellationToken ct = default)
    {
        using var doc = await GetAsync<JsonDocument>("openapi.json", ct).ConfigureAwait(false);
        return doc.RootElement.GetProperty("info").GetProperty("version").GetString() ?? "";
    }

    /// <summary>True when the served contract has the same major version and at least this client's minor.</summary>
    public static bool IsCompatible(string served, string expected = ContractVersion)
    {
        if (!Version.TryParse(served, out var s) || !Version.TryParse(expected, out var e)) return false;
        return s.Major == e.Major && s.Minor >= e.Minor;
    }

    /// <summary>GET /api/v1/system/status (getSystemStatus).</summary>
    public Task<SystemStatus> GetSystemStatusAsync(CancellationToken ct = default) =>
        GetAsync<SystemStatus>("api/v1/system/status", ct);

    // ------------------------------------------------------------------ search / company
    /// <summary>GET /api/v1/search (searchSecurities) - ticker / name / ISIN lookup.</summary>
    public Task<List<SecurityRef>> SearchAsync(string q, int limit = 10, CancellationToken ct = default) =>
        GetAsync<List<SecurityRef>>($"api/v1/search?q={Uri.EscapeDataString(q)}&limit={limit}", ct);

    /// <summary>GET /api/v1/companies/{security_id}/overview (getCompanyOverview) - Company 360 summary.</summary>
    public Task<CompanyOverview> GetCompanyOverviewAsync(string securityId, string? profile = null, CancellationToken ct = default) =>
        GetAsync<CompanyOverview>($"api/v1/companies/{Uri.EscapeDataString(securityId)}/overview"
                                  + (profile is null ? "" : $"?profile={Uri.EscapeDataString(profile)}"), ct);

    // ------------------------------------------------------------------ screener
    /// <summary>POST /api/v1/screener/query (runScreener). Read-only computation (allowed for the viewer role).</summary>
    public async Task<ScreenerResult> RunScreenerAsync(ScreenerQuery query, CancellationToken ct = default)
    {
        using var resp = await _http.PostAsJsonAsync("api/v1/screener/query", query, Json, ct).ConfigureAwait(false);
        return await ReadAsync<ScreenerResult>(resp, ct).ConfigureAwait(false);
    }

    // ------------------------------------------------------------------ market / earnings
    /// <summary>GET /api/v1/market/overview (getMarketOverview).</summary>
    public Task<MarketOverview> GetMarketOverviewAsync(string? region = null, int? moversLimit = null, CancellationToken ct = default)
    {
        var q = new List<string>();
        if (region is not null) q.Add("region=" + Uri.EscapeDataString(region));
        if (moversLimit is not null) q.Add("movers_limit=" + moversLimit.Value);
        return GetAsync<MarketOverview>("api/v1/market/overview" + (q.Count > 0 ? "?" + string.Join("&", q) : ""), ct);
    }

    /// <summary>GET /api/v1/earnings/calendar (getEarningsCalendar). Dates yyyy-MM-dd; compact drops null fields.</summary>
    public Task<EarningsCalendar> GetEarningsCalendarAsync(DateTime? from = null, DateTime? to = null, string? region = null,
        string? universe = null, int limit = 200, int offset = 0, bool compact = true, CancellationToken ct = default)
    {
        var q = new List<string> { "limit=" + limit, "offset=" + offset, "compact=" + (compact ? "true" : "false") };
        if (from is not null) q.Add("from=" + from.Value.ToString("yyyy-MM-dd"));
        if (to is not null) q.Add("to=" + to.Value.ToString("yyyy-MM-dd"));
        if (region is not null) q.Add("region=" + Uri.EscapeDataString(region));
        if (universe is not null) q.Add("universe=" + Uri.EscapeDataString(universe));
        return GetAsync<EarningsCalendar>("api/v1/earnings/calendar?" + string.Join("&", q), ct);
    }

    // ------------------------------------------------------------------ live stream (SSE)
    /// <summary>
    /// GET /api/v1/live/stream (streamLive): Server-Sent Events. Yields every frame (hello, heartbeat, topic frames).
    /// Cancel the token to stop. Pass the last seen id to resume (sent as the Last-Event-ID header).
    /// Reconnects are the caller's job (see LiveSubscriber for a resuming loop).
    /// </summary>
    public async IAsyncEnumerable<SseFrame> StreamLiveAsync(string topics = "filings,news,macro,alerts", long? lastEventId = null,
        int replay = 0, double heartbeatSeconds = 15, [EnumeratorCancellation] CancellationToken ct = default)
    {
        var url = $"api/v1/live/stream?topics={Uri.EscapeDataString(topics)}&heartbeat_s={heartbeatSeconds.ToString(System.Globalization.CultureInfo.InvariantCulture)}";
        if (replay > 0 && lastEventId is null) url += "&replay=" + replay;
        using var req = new HttpRequestMessage(HttpMethod.Get, url);
        req.Headers.Accept.Clear();
        req.Headers.Accept.ParseAdd("text/event-stream");
        if (lastEventId is not null) req.Headers.TryAddWithoutValidation("Last-Event-ID", lastEventId.Value.ToString());
        req.Options.Set(Big360HttpOptions.IsStream, true);
        using var resp = await _http.SendAsync(req, HttpCompletionOption.ResponseHeadersRead, ct).ConfigureAwait(false);
        if (!resp.IsSuccessStatusCode)
            throw await ErrorAsync(resp, ct).ConfigureAwait(false);
#if NET5_0_OR_GREATER
        using var stream = await resp.Content.ReadAsStreamAsync(ct).ConfigureAwait(false);
#else
        using var stream = await resp.Content.ReadAsStreamAsync().ConfigureAwait(false);
#endif
        using var reader = new StreamReader(stream, Encoding.UTF8);
        using var reg = ct.Register(() => stream.Dispose()); // unblocks ReadLineAsync on older frameworks
        string evt = "message";
        string? id = null;
        var data = new StringBuilder();
        while (!ct.IsCancellationRequested)
        {
            string? line;
            try
            {
#if NET7_0_OR_GREATER
                line = await reader.ReadLineAsync(ct).ConfigureAwait(false);
#else
                line = await reader.ReadLineAsync().ConfigureAwait(false);
#endif
            }
            catch (Exception) when (ct.IsCancellationRequested)
            {
                yield break;
            }
            if (line is null) yield break; // server closed the stream
            if (line.Length == 0)
            {
                if (data.Length > 0)
                {
                    yield return new SseFrame { Event = evt, Id = id, Data = data.ToString() };
                }
                evt = "message";
                data.Clear();
                continue; // per the SSE spec the last event id persists until a frame sets a new one
            }
            if (line.StartsWith(':')) continue; // comment
            var colon = line.IndexOf(':');
            var field = colon < 0 ? line : line.Substring(0, colon);
            var value = colon < 0 ? "" : line.Substring(colon + 1).TrimStart(' ');
            switch (field)
            {
                case "event": evt = value; break;
                case "data":
                    if (data.Length > 0) data.Append('\n');
                    data.Append(value);
                    break;
                case "id": id = value; break;
                case "retry": break;
            }
        }
    }

    /// <summary>Deserialize a topic frame's data (LiveStreamEvent). Returns null for control frames.</summary>
    public static LiveStreamEvent? ParseEvent(SseFrame f) =>
        f.Event is "hello" or "heartbeat" ? null : JsonSerializer.Deserialize<LiveStreamEvent>(f.Data, Json);

    // ------------------------------------------------------------------ plumbing
    private async Task<T> GetAsync<T>(string url, CancellationToken ct)
    {
        using var resp = await _http.GetAsync(url, ct).ConfigureAwait(false);
        return await ReadAsync<T>(resp, ct).ConfigureAwait(false);
    }

    private static async Task<T> ReadAsync<T>(HttpResponseMessage resp, CancellationToken ct)
    {
        if (!resp.IsSuccessStatusCode)
            throw await ErrorAsync(resp, ct).ConfigureAwait(false);
        var v = await resp.Content.ReadFromJsonAsync<T>(Json, ct).ConfigureAwait(false);
        return v ?? throw new Big360ApiException(resp.StatusCode, null, RequestIdOf(resp), "empty response body");
    }

    private static string? RequestIdOf(HttpResponseMessage resp) =>
        resp.Headers.TryGetValues("X-Request-ID", out var v) ? v.FirstOrDefault() : null;

    private static async Task<Big360ApiException> ErrorAsync(HttpResponseMessage resp, CancellationToken ct)
    {
        Problem? p = null;
        try
        {
            var media = resp.Content.Headers.ContentType?.MediaType ?? "";
            if (media.Contains("json"))
                p = await resp.Content.ReadFromJsonAsync<Problem>(Json, ct).ConfigureAwait(false);
        }
        catch (JsonException)
        {
        }
        var rid = RequestIdOf(resp);
        return new Big360ApiException(resp.StatusCode, p, rid,
            $"BiG360 API {(int)resp.StatusCode} {p?.Title ?? resp.ReasonPhrase}: {p?.Detail} (request id {rid})");
    }
}

/// <summary>Per-request options read by the handlers.</summary>
public static class Big360HttpOptions
{
    /// <summary>Marks the long-lived SSE request: no retry / no per-request timeout.</summary>
    public static readonly HttpRequestOptionsKey<bool> IsStream = new("big360.stream");
}
