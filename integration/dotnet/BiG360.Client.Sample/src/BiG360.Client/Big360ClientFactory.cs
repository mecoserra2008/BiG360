// Composition without DI (console tools, offline build): handler chain RequestId -> [Bearer] -> Retry -> socket.
// Services should prefer the DI path (ServiceCollectionExtensions.AddBig360Client), which uses IHttpClientFactory
// and Polly through Microsoft.Extensions.Http.Resilience.

namespace BiG360.Client;

public static class Big360ClientFactory
{
    public static Big360Client Create(Big360ClientOptions options, IAccessTokenProvider? tokens = null)
    {
        HttpMessageHandler inner = new SocketsHttpHandler
        {
            PooledConnectionLifetime = TimeSpan.FromMinutes(5), // DNS changes behind a gateway / load balancer
            AutomaticDecompression = System.Net.DecompressionMethods.GZip | System.Net.DecompressionMethods.Deflate,
            UseProxy = true, // system / corporate proxy (HTTPS_PROXY or WinINET settings)
        };
        inner = new SimpleRetryHandler(options.MaxRetries) { InnerHandler = inner, OnRetry = options.OnRetry };
        if (options.UseBearerToken)
        {
            if (tokens is null)
                throw new InvalidOperationException("UseBearerToken = true needs an IAccessTokenProvider");
            inner = new BearerTokenHandler(tokens) { InnerHandler = inner };
        }
        inner = new RequestIdHandler { InnerHandler = inner };
        var http = new HttpClient(inner) { BaseAddress = options.BaseAddress, Timeout = options.Timeout };
        return new Big360Client(http);
    }
}
