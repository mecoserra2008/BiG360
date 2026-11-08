// Delegating handlers: optional bearer token (off by default) and a built-in retry used when Polly is not available
// (offline build). In the normal net8.0 build, retries come from Microsoft.Extensions.Http.Resilience (Polly v8);
// see ServiceCollectionExtensions.

using System.Net;
using System.Net.Http.Headers;

namespace BiG360.Client;

/// <summary>Supplies access tokens (Entra ID / any OIDC provider).</summary>
public interface IAccessTokenProvider
{
    Task<string> GetTokenAsync(CancellationToken ct);
}

/// <summary>Token from an environment variable (default BIG360_BEARER_TOKEN) - for tests and scripts only.</summary>
public sealed class EnvironmentTokenProvider : IAccessTokenProvider
{
    private readonly string _variable;

    public EnvironmentTokenProvider(string variable = "BIG360_BEARER_TOKEN") => _variable = variable;

    public Task<string> GetTokenAsync(CancellationToken ct) =>
        Task.FromResult(Environment.GetEnvironmentVariable(_variable)
                        ?? throw new InvalidOperationException($"{_variable} is not set"));
}

/// <summary>Adds Authorization: Bearer &lt;token&gt;. Only added to the pipeline when UseBearerToken = true.</summary>
public sealed class BearerTokenHandler : DelegatingHandler
{
    private readonly IAccessTokenProvider _tokens;

    public BearerTokenHandler(IAccessTokenProvider tokens) => _tokens = tokens;

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        var token = await _tokens.GetTokenAsync(ct).ConfigureAwait(false);
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", token);
        return await base.SendAsync(request, ct).ConfigureAwait(false);
    }
}

/// <summary>Adds a fresh X-Request-ID to each request (correlates the caller's logs with the API's JSON logs).</summary>
public sealed class RequestIdHandler : DelegatingHandler
{
    protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        if (!request.Headers.Contains("X-Request-ID"))
            request.Headers.TryAddWithoutValidation("X-Request-ID", "net-" + Guid.NewGuid().ToString("N"));
        return base.SendAsync(request, ct);
    }
}

/// <summary>
/// Minimal retry (exponential back-off with jitter, honours Retry-After) for idempotent requests: GET / HEAD and the
/// read-only screener POST. Used by the offline build; the DI build uses Polly via AddResilienceHandler instead.
/// </summary>
public sealed class SimpleRetryHandler : DelegatingHandler
{
    private readonly int _maxRetries;
    private readonly TimeSpan _baseDelay;
    private static readonly Random Jitter = new();

    public Action<int, string>? OnRetry { get; set; }

    public SimpleRetryHandler(int maxRetries = 3, TimeSpan? baseDelay = null)
    {
        _maxRetries = maxRetries;
        _baseDelay = baseDelay ?? TimeSpan.FromMilliseconds(500);
    }

    /// <summary>408, 429, 500, 502, 503, 504 (501 = operation not implemented by this server: not retried).</summary>
    public static bool IsTransient(HttpStatusCode s) =>
        s == HttpStatusCode.RequestTimeout || (int)s == 429 || ((int)s >= 500 && s != HttpStatusCode.NotImplemented);

    private static bool Idempotent(HttpRequestMessage r) =>
        r.Method == HttpMethod.Get || r.Method == HttpMethod.Head ||
        (r.Method == HttpMethod.Post && r.RequestUri is not null && r.RequestUri.AbsolutePath.EndsWith("/screener/query"));

    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken ct)
    {
        var isStream = request.Options.TryGetValue(Big360HttpOptions.IsStream, out var s) && s;
        if (isStream || !Idempotent(request) || _maxRetries <= 0)
            return await base.SendAsync(request, ct).ConfigureAwait(false);
        for (var attempt = 0; ; attempt++)
        {
            HttpResponseMessage? resp = null;
            string reason;
            try
            {
                resp = await base.SendAsync(request, ct).ConfigureAwait(false);
                if (!IsTransient(resp.StatusCode) || attempt >= _maxRetries)
                    return resp;
                reason = "HTTP " + (int)resp.StatusCode;
            }
            catch (HttpRequestException e) when (attempt < _maxRetries && !ct.IsCancellationRequested)
            {
                reason = e.Message;
            }
            catch (TaskCanceledException) when (attempt < _maxRetries && !ct.IsCancellationRequested)
            {
                reason = "attempt timeout";
            }
            OnRetry?.Invoke(attempt + 1, reason);
            var delay = resp?.Headers.RetryAfter?.Delta
                        ?? TimeSpan.FromMilliseconds(_baseDelay.TotalMilliseconds * Math.Pow(2, attempt) + Jitter.Next(0, 250));
            resp?.Dispose();
            await Task.Delay(delay, ct).ConfigureAwait(false);
        }
    }
}
