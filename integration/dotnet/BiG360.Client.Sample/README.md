# BiG360.Client.Sample (.NET 8)

This is a sample C# solution for calling the BiG Equity 360 API: REST over `contracts/api.yaml` 1.31.0, plus the SSE live stream.

| Project | What it contains |
|---|---|
| `src/BiG360.Client` | A class library. It holds a typed client (`Big360Client`) with these operations: health, ready, contract version, system status, search, Company 360 overview, screener, market overview, earnings calendar and the SSE stream. It also holds the DTOs, a resuming `LiveSubscriber`, handlers (request id, optional bearer token, built-in retry) and a DI registration (`AddBig360Client`) that uses `IHttpClientFactory` and Polly v8. An optional MSAL token provider is included. |
| `samples/BiG360.Console` | A console demo. It calls health and ready, checks the contract version, reads system status, looks up a company and its Company 360 summary, runs a screener query, reads the market overview and the earnings calendar, subscribes to the SSE stream with cancellation, and runs a retry demo. |

The client is **hand-written** for these key endpoints because NSwag and Kiota were not available offline when it was written. Its methods map 1:1 to the contract's `operationId`s. For the full API, generate a client as described below.

Every DTO keeps fields it does not know in `AdditionalProperties`. Within API major version 1, a newer server therefore never breaks this client.

## Build and run (.NET 8 SDK)

Run these from this folder:

```powershell
dotnet --list-sdks                       # needs 8.0.x
dotnet restore                           # pulls Microsoft.Extensions.Http, .Http.Resilience (Polly v8), .DependencyInjection
dotnet build -c Release
dotnet run --project samples/BiG360.Console -c Release -- --base-url http://127.0.0.1:8010/ --stream-seconds 20
dotnet run --project samples/BiG360.Console -c Release -- --di --retry-demo          # IHttpClientFactory + Polly path
dotnet run --project samples/BiG360.Console -c Release -- --ticker ASML --no-stream
```

**Options:**
- `--base-url URL` sets the API address. The default is `http://127.0.0.1:8010/`, or the `BIG360_API_URL` environment variable when it is set.
- `--ticker T` picks the company to look up.
- `--stream-seconds N` sets how long the SSE subscription runs.
- `--no-stream` skips the SSE subscription.
- `--retry-demo` calls a closed port to show the retries.
- `--di` uses the `IHttpClientFactory` + Polly path.
- `--bearer` sends `BIG360_BEARER_TOKEN` as a bearer token. Use it against a server with `BIG360_AUTH=oidc`.

Ctrl+C cancels at any point. All calls are read-only. The screener `POST` is a read-only computation.

**MSAL (Entra ID client credentials).** Build with the MSAL provider:

```powershell
dotnet build -c Release -p:Big360UseMsal=true
```

Then construct `new MsalTokenProvider(new Big360ClientOptions { TenantId = ..., ClientId = ..., Scope = "api://big360/.default", ClientSecret = <from Key Vault> })`. Pass it to `Big360ClientFactory.Create(options, tokens)`, or register it as `IAccessTokenProvider` with `UseBearerToken = true`.

## Verified on this PC (.NET 6 SDK only)

The code is kept C# 10 compatible. It was built and run on the .NET 6.0.100 SDK without NuGet packages:

```powershell
dotnet build BiG360.Client.Sample.sln -c Release -p:Big360TargetFramework=net6.0 -p:Big360Offline=true
dotnet samples/BiG360.Console/bin/Release/net6.0/BiG360.Console.dll --stream-seconds 20 --retry-demo
```

`Big360Offline=true` leaves out the package-based code: the DI registration with Polly and the MSAL provider. It uses `Big360ClientFactory`, which is built on `SocketsHttpHandler` and the built-in `SimpleRetryHandler`. The output of the run against the live API is in `docs/it/INTEGRATION_DOTNET.md` (appendix A).

The default net8.0 build, with Polly, `IHttpClientFactory` and MSAL, could not be compiled here. It needs the .NET 8 SDK and nuget.org (or the bank's NuGet feed) for 3–4 packages.

## Generating a full client from the contract

The contract is OpenAPI **3.1**. Kiota reads 3.1. NSwag's 3.1 support is partial, so for NSwag use the 3.0.3 file produced by `scripts/openapi_31_to_30.py`. It is written to `integration/dotnet/api.v30.json`.

**Kiota**, installed once with `dotnet tool install --global Microsoft.OpenApi.Kiota`:

```powershell
kiota generate -l CSharp -d ..\..\..\contracts\api.yaml -c Big360ApiClient -n BiG360.Client.Kiota -o src\BiG360.Client.Kiota --exclude-backward-compatible
# or, if your Kiota version rejects 3.1:  -d ..\api.v30.json
```

Kiota clients use `Microsoft.Kiota.Bundle` and an `IAuthenticationProvider`: `AnonymousAuthenticationProvider` locally, or `AzureIdentityAuthenticationProvider` with Entra ID.

**NSwag**, installed once with `dotnet tool install --global NSwag.ConsoleCore`:

```powershell
..\..\..\.venv\Scripts\python.exe ..\..\..\scripts\openapi_31_to_30.py      # refresh api.v30.json from contracts/api.yaml
nswag run nswag.json /runtime:Net80                                         # -> src/BiG360.Client.Generated/Big360GeneratedClient.g.cs
```

Put the generated file in a new class library that references `Newtonsoft.Json` or `System.Text.Json` (the config uses `SystemTextJson`). Register it with `services.AddHttpClient<IBig360GeneratedClient, Big360GeneratedClient>()` and the same handlers and resilience pipeline as `AddBig360Client`.

The SSE stream (`streamLive`) is not modelled by generators, because its response is `text/event-stream`. Keep using `Big360Client.StreamLiveAsync` or `LiveSubscriber` for it.

## Compatibility

- `Big360Client.ContractVersion` is the contract this client targets. `GetContractVersionAsync()` reads the served contract's `info.version`.
- `IsCompatible()` is true when the server has the same major version and the same or a later minor version.
- An older server, such as 1.30 before the `/ready` endpoint, returns 404 or 501 for newer endpoints. The console handles that case.

The rules are in `docs/it/INTEGRATION_DOTNET.md` §7.
