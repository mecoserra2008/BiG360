// Entra ID token provider (MSAL, client-credentials flow for service-to-service calls).
// Compiled only with -p:Big360UseMsal=true (adds the Microsoft.Identity.Client package).
// For a user-facing desktop app use PublicClientApplicationBuilder + AcquireTokenInteractive / Silent (WAM) instead.
#if BIG360_MSAL
using Microsoft.Identity.Client;

namespace BiG360.Client;

public sealed class MsalTokenProvider : IAccessTokenProvider
{
    private readonly IConfidentialClientApplication _app;
    private readonly string[] _scopes;

    public MsalTokenProvider(Big360ClientOptions o)
    {
        if (string.IsNullOrEmpty(o.TenantId) || string.IsNullOrEmpty(o.ClientId) || string.IsNullOrEmpty(o.Scope))
            throw new InvalidOperationException("MsalTokenProvider needs TenantId, ClientId and Scope (e.g. api://big360/.default)");
        var b = ConfidentialClientApplicationBuilder.Create(o.ClientId)
            .WithAuthority(AzureCloudInstance.AzurePublic, o.TenantId);
        // Prefer a certificate (WithCertificate) or a managed identity in production; a secret is shown for brevity.
        _app = (o.ClientSecret is null ? b : b.WithClientSecret(o.ClientSecret)).Build();
        _scopes = new[] { o.Scope };
    }

    public async Task<string> GetTokenAsync(CancellationToken ct)
    {
        // MSAL caches the token in memory and refreshes it shortly before expiry.
        var r = await _app.AcquireTokenForClient(_scopes).ExecuteAsync(ct).ConfigureAwait(false);
        return r.AccessToken;
    }
}
#endif
