// DI registration for services (ASP.NET Core, worker services): IHttpClientFactory + Polly v8 resilience
// (Microsoft.Extensions.Http.Resilience) + optional bearer tokens. Compiled only in the normal (non-offline) build.
#if !BIG360_OFFLINE
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Http.Resilience;
using Polly;

namespace BiG360.Client;

public static class ServiceCollectionExtensions
{
    /// <summary>
    /// Registers a typed Big360Client. Pipeline: RequestId -> [Bearer] -> resilience (total timeout 2 min, retry x3
    /// exponential + jitter on 408 / 429 / 5xx / network errors for GET / HEAD only, circuit breaker, 60 s attempt
    /// timeout). The SSE stream is not cut by the timeouts: its response headers arrive at once and the body is read
    /// after the pipeline has returned.
    /// </summary>
    public static IHttpClientBuilder AddBig360Client(this IServiceCollection services, Action<Big360ClientOptions> configure)
    {
        var options = new Big360ClientOptions();
        configure(options);
        services.AddSingleton(options);
        services.AddTransient<RequestIdHandler>();
        if (options.UseBearerToken)
            services.AddTransient<BearerTokenHandler>();

        var builder = services.AddHttpClient<Big360Client>(c =>
            {
                c.BaseAddress = options.BaseAddress;
                c.Timeout = Timeout.InfiniteTimeSpan; // timeouts are owned by the resilience pipeline
            })
            .AddHttpMessageHandler<RequestIdHandler>();
        if (options.UseBearerToken)
            builder.AddHttpMessageHandler<BearerTokenHandler>();

        builder.AddResilienceHandler("big360", pipeline =>
        {
            pipeline.AddTimeout(TimeSpan.FromMinutes(2));
            var retry = new HttpRetryStrategyOptions
            {
                MaxRetryAttempts = options.MaxRetries,
                BackoffType = DelayBackoffType.Exponential,
                UseJitter = true,
                Delay = TimeSpan.FromMilliseconds(500),
                // default ShouldHandle: 408, 429, 5xx, HttpRequestException, TimeoutRejectedException
                ShouldRetryAfterHeader = true,
                OnRetry = args =>
                {
                    options.OnRetry?.Invoke(args.AttemptNumber + 1, args.Outcome.Exception?.Message
                                            ?? ("HTTP " + (int?)args.Outcome.Result?.StatusCode));
                    return default;
                },
            };
            retry.DisableForUnsafeHttpMethods(); // never replay POST / PUT / PATCH / DELETE
            pipeline.AddRetry(retry);
            pipeline.AddCircuitBreaker(new HttpCircuitBreakerStrategyOptions
            {
                SamplingDuration = TimeSpan.FromSeconds(60),
                FailureRatio = 0.5,
                MinimumThroughput = 10,
                BreakDuration = TimeSpan.FromSeconds(30),
            });
            pipeline.AddTimeout(options.Timeout);
        });
        return builder;
    }
}
#endif
