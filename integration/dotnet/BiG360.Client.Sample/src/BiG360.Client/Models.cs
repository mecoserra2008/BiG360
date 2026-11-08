// DTOs for the subset of contracts/api.yaml (1.31.0) used by the sample.
//
// Compatibility rule (the contract is additive-only within a major version): every DTO keeps unknown fields in
// AdditionalProperties ([JsonExtensionData]), so a newer server never breaks this client. Nullable reference /
// value types mirror `type: [x, "null"]` in the contract. Dates are ISO yyyy-mm-dd strings in the contract; they are
// exposed as string here to stay tolerant (parse with DateOnly.Parse on net6+).

using System.Text.Json;
using System.Text.Json.Serialization;

namespace BiG360.Client;

public abstract class ApiObject
{
    /// <summary>Fields this client version does not model (forward compatibility).</summary>
    [JsonExtensionData]
    public Dictionary<string, JsonElement>? AdditionalProperties { get; set; }
}

public sealed class Health : ApiObject
{
    [JsonPropertyName("status")] public string Status { get; set; } = "";
    [JsonPropertyName("version")] public string Version { get; set; } = "";
    [JsonPropertyName("time")] public DateTimeOffset Time { get; set; }
    [JsonPropertyName("serving_run_id")] public string? ServingRunId { get; set; }
    [JsonPropertyName("serving_built_at")] public DateTimeOffset? ServingBuiltAt { get; set; }
}

public sealed class ReadinessCheck : ApiObject
{
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("required")] public bool Required { get; set; }
    [JsonPropertyName("ok")] public bool Ok { get; set; }
    [JsonPropertyName("detail")] public string? Detail { get; set; }
}

public sealed class Readiness : ApiObject
{
    [JsonPropertyName("status")] public string Status { get; set; } = "";
    [JsonPropertyName("time")] public DateTimeOffset Time { get; set; }
    [JsonPropertyName("serving_run_id")] public string? ServingRunId { get; set; }
    [JsonPropertyName("serving_as_of")] public string? ServingAsOf { get; set; }
    [JsonPropertyName("worker_heartbeat_age_s")] public double? WorkerHeartbeatAgeSeconds { get; set; }
    [JsonPropertyName("checks")] public List<ReadinessCheck> Checks { get; set; } = new();
    [JsonIgnore] public bool IsReady => Status == "ready";
}

public sealed class OpsAlert : ApiObject
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("severity")] public string Severity { get; set; } = "";
    [JsonPropertyName("title")] public string Title { get; set; } = "";
    [JsonPropertyName("detail")] public string? Detail { get; set; }
}

public sealed class WorkerInfo : ApiObject
{
    [JsonPropertyName("alive")] public bool Alive { get; set; }
    [JsonPropertyName("heartbeat_at")] public DateTimeOffset? HeartbeatAt { get; set; }
    [JsonPropertyName("next_runs")] public Dictionary<string, string>? NextRuns { get; set; }
}

public sealed class SystemStatus : ApiObject
{
    [JsonPropertyName("version")] public string Version { get; set; } = "";
    [JsonPropertyName("serving_run_id")] public string? ServingRunId { get; set; }
    [JsonPropertyName("serving_built_at")] public DateTimeOffset? ServingBuiltAt { get; set; }
    [JsonPropertyName("serving_as_of")] public string? ServingAsOf { get; set; }
    [JsonPropertyName("data_as_of_overall")] public string? DataAsOfOverall { get; set; }
    [JsonPropertyName("data_as_of")] public Dictionary<string, string>? DataAsOf { get; set; }
    [JsonPropertyName("keys_present")] public Dictionary<string, bool> KeysPresent { get; set; } = new();
    [JsonPropertyName("universe_counts")] public Dictionary<string, long>? UniverseCounts { get; set; }
    [JsonPropertyName("worker")] public WorkerInfo? Worker { get; set; }
    [JsonPropertyName("ops_alerts")] public List<OpsAlert>? OpsAlerts { get; set; }
}

public sealed class SecurityRef : ApiObject
{
    [JsonPropertyName("security_id")] public string SecurityId { get; set; } = "";
    [JsonPropertyName("issuer_id")] public string IssuerId { get; set; } = "";
    [JsonPropertyName("ticker")] public string Ticker { get; set; } = "";
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("region")] public string Region { get; set; } = "";
    [JsonPropertyName("country")] public string Country { get; set; } = "";
    [JsonPropertyName("currency")] public string Currency { get; set; } = "";
    [JsonPropertyName("isin")] public string? Isin { get; set; }
    [JsonPropertyName("mic")] public string? Mic { get; set; }
    [JsonPropertyName("exchange_name")] public string? ExchangeName { get; set; }
    [JsonPropertyName("sector_id")] public string? SectorId { get; set; }
    [JsonPropertyName("market_cap_usd")] public double? MarketCapUsd { get; set; }
}

// ---------------------------------------------------------------- screener
public sealed class ScreenerFilter
{
    [JsonPropertyName("field")] public string Field { get; set; } = "";
    /// <summary>eq | neq | gt | gte | lt | lte | between | in | not_in | is_null | not_null</summary>
    [JsonPropertyName("op")] public string Op { get; set; } = "gte";
    [JsonPropertyName("value")] public object? Value { get; set; }
    [JsonPropertyName("value2")] public double? Value2 { get; set; }
}

public sealed class ScreenerQuery
{
    /// <summary>mvp_us | mvp_global | full | watch (default full)</summary>
    [JsonPropertyName("universe_id")] public string? UniverseId { get; set; }
    [JsonPropertyName("regions")] public List<string>? Regions { get; set; }
    [JsonPropertyName("sector_ids")] public List<string>? SectorIds { get; set; }
    [JsonPropertyName("min_market_cap_usd")] public double? MinMarketCapUsd { get; set; }
    [JsonPropertyName("filters")] public List<ScreenerFilter>? Filters { get; set; }
    [JsonPropertyName("profile_id")] public string? ProfileId { get; set; }
    [JsonPropertyName("columns")] public List<string>? Columns { get; set; }
    [JsonPropertyName("sort_by")] public string? SortBy { get; set; }
    [JsonPropertyName("sort_dir")] public string? SortDir { get; set; }
    [JsonPropertyName("limit")] public int? Limit { get; set; }
    [JsonPropertyName("offset")] public int? Offset { get; set; }
}

public sealed class ScreenerRow : ApiObject
{
    [JsonPropertyName("security")] public SecurityRef Security { get; set; } = new();
    [JsonPropertyName("price")] public double? Price { get; set; }
    [JsonPropertyName("chg_1d")] public double? Change1D { get; set; }
    [JsonPropertyName("chg_1m")] public double? Change1M { get; set; }
    [JsonPropertyName("composite")] public double? Composite { get; set; }
    [JsonPropertyName("grade")] public string? Grade { get; set; }
    [JsonPropertyName("rank")] public int? Rank { get; set; }
    [JsonPropertyName("factor_scores")] public Dictionary<string, double?> FactorScores { get; set; } = new();
    [JsonPropertyName("values")] public Dictionary<string, JsonElement> Values { get; set; } = new();
}

public sealed class ScreenerResult : ApiObject
{
    [JsonPropertyName("data_status")] public string? DataStatus { get; set; }
    [JsonPropertyName("as_of")] public string AsOf { get; set; } = "";
    [JsonPropertyName("total")] public int Total { get; set; }
    [JsonPropertyName("limit")] public int Limit { get; set; }
    [JsonPropertyName("offset")] public int Offset { get; set; }
    [JsonPropertyName("rows")] public List<ScreenerRow> Rows { get; set; } = new();
}

// ---------------------------------------------------------------- company 360
public sealed class CompositeSummary : ApiObject
{
    [JsonPropertyName("profile_id")] public string ProfileId { get; set; } = "";
    [JsonPropertyName("score")] public double? Score { get; set; }
    [JsonPropertyName("grade")] public string? Grade { get; set; }
    [JsonPropertyName("rank_global")] public int? RankGlobal { get; set; }
    [JsonPropertyName("rank_sector")] public int? RankSector { get; set; }
}

public sealed class IssuerInfo : ApiObject
{
    [JsonPropertyName("issuer_id")] public string IssuerId { get; set; } = "";
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("country")] public string Country { get; set; } = "";
    [JsonPropertyName("reporting_currency")] public string? ReportingCurrency { get; set; }
    [JsonPropertyName("identifiers")] public Dictionary<string, string>? Identifiers { get; set; }
}

public sealed class Quote : ApiObject
{
    [JsonPropertyName("date")] public string? Date { get; set; }
    [JsonPropertyName("close")] public double? Close { get; set; }
    [JsonPropertyName("currency")] public string? Currency { get; set; }
    [JsonPropertyName("chg_1d")] public double? Change1D { get; set; }
    [JsonPropertyName("chg_ytd")] public double? ChangeYtd { get; set; }
    [JsonPropertyName("market_cap_usd")] public double? MarketCapUsd { get; set; }
}

public sealed class FactorScore : ApiObject
{
    [JsonPropertyName("factor_id")] public string FactorId { get; set; } = "";
    [JsonPropertyName("score")] public double? Score { get; set; }
}

public sealed class CompanyOverview : ApiObject
{
    [JsonPropertyName("data_status")] public string? DataStatus { get; set; }
    [JsonPropertyName("security")] public SecurityRef Security { get; set; } = new();
    [JsonPropertyName("issuer")] public IssuerInfo Issuer { get; set; } = new();
    [JsonPropertyName("as_of")] public string AsOf { get; set; } = "";
    [JsonPropertyName("quote")] public Quote? Quote { get; set; }
    [JsonPropertyName("composite")] public CompositeSummary Composite { get; set; } = new();
    [JsonPropertyName("factors")] public List<FactorScore> Factors { get; set; } = new();
    [JsonPropertyName("badges")] public List<string> Badges { get; set; } = new();
    [JsonPropertyName("valuation")] public Dictionary<string, double?>? Valuation { get; set; }
}

// ---------------------------------------------------------------- market overview
public sealed class IndexPerf : ApiObject
{
    [JsonPropertyName("id")] public string Id { get; set; } = "";
    [JsonPropertyName("name")] public string Name { get; set; } = "";
    [JsonPropertyName("region")] public string? Region { get; set; }
    [JsonPropertyName("last")] public double? Last { get; set; }
    [JsonPropertyName("chg_1d")] public double? Change1D { get; set; }
    [JsonPropertyName("chg_ytd")] public double? ChangeYtd { get; set; }
    [JsonPropertyName("as_of")] public string? AsOf { get; set; }
}

public sealed class MoverRow : ApiObject
{
    [JsonPropertyName("security")] public SecurityRef Security { get; set; } = new();
    [JsonPropertyName("close")] public double? Close { get; set; }
    [JsonPropertyName("chg_1d")] public double Change1D { get; set; }
}

public sealed class Breadth : ApiObject
{
    [JsonPropertyName("pct_above_200d")] public double? PctAbove200D { get; set; }
    [JsonPropertyName("advancers")] public int Advancers { get; set; }
    [JsonPropertyName("decliners")] public int Decliners { get; set; }
}

public sealed class MarketOverview : ApiObject
{
    [JsonPropertyName("data_status")] public string? DataStatus { get; set; }
    [JsonPropertyName("as_of")] public string AsOf { get; set; } = "";
    [JsonPropertyName("region")] public string? Region { get; set; }
    [JsonPropertyName("indices")] public List<IndexPerf> Indices { get; set; } = new();
    [JsonPropertyName("breadth")] public Breadth Breadth { get; set; } = new();
    [JsonPropertyName("gainers")] public List<MoverRow> Gainers { get; set; } = new();
    [JsonPropertyName("losers")] public List<MoverRow> Losers { get; set; } = new();
}

// ---------------------------------------------------------------- earnings calendar
public sealed class EarningsCalendarItem : ApiObject
{
    [JsonPropertyName("kind")] public string Kind { get; set; } = "";
    [JsonPropertyName("event_type")] public string EventType { get; set; } = "";
    [JsonPropertyName("status")] public string Status { get; set; } = "";
    [JsonPropertyName("date")] public string Date { get; set; } = "";
    [JsonPropertyName("session")] public string? Session { get; set; }
    /// <summary>official_release | official_filing | provider | company_announced | provider_estimate | model_projection</summary>
    [JsonPropertyName("date_source")] public string? DateSource { get; set; }
    [JsonPropertyName("source_label")] public string? SourceLabel { get; set; }
    [JsonPropertyName("security")] public SecurityRef Security { get; set; } = new();
    /// <summary>Model-based expectation (not consensus).</summary>
    [JsonPropertyName("eps_expected")] public double? EpsExpected { get; set; }
    [JsonPropertyName("eps_actual")] public double? EpsActual { get; set; }
}

public sealed class EarningsCalendar : ApiObject
{
    [JsonPropertyName("from")] public string From { get; set; } = "";
    [JsonPropertyName("to")] public string To { get; set; } = "";
    [JsonPropertyName("as_of")] public string AsOf { get; set; } = "";
    [JsonPropertyName("total")] public int Total { get; set; }
    [JsonPropertyName("truncated")] public bool Truncated { get; set; }
    [JsonPropertyName("offset")] public int? Offset { get; set; }
    [JsonPropertyName("next_offset")] public int? NextOffset { get; set; }
    [JsonPropertyName("items")] public List<EarningsCalendarItem> Items { get; set; } = new();
    [JsonPropertyName("model_note")] public string ModelNote { get; set; } = "";
}

// ---------------------------------------------------------------- live stream (SSE)
/// <summary>One Server-Sent Events frame of GET /api/v1/live/stream.</summary>
public sealed class SseFrame
{
    public string Event { get; init; } = "message";
    public string? Id { get; init; }
    public string Data { get; init; } = "";
}

/// <summary>Data of a topic frame (LiveStreamEvent).</summary>
public sealed class LiveStreamEvent : ApiObject
{
    [JsonPropertyName("id")] public long Id { get; set; }
    [JsonPropertyName("topic")] public string Topic { get; set; } = "";
    [JsonPropertyName("type")] public string Type { get; set; } = "";
    [JsonPropertyName("key")] public string Key { get; set; } = "";
    [JsonPropertyName("ts_source")] public string? TsSource { get; set; }
    [JsonPropertyName("ts_ingest")] public string TsIngest { get; set; } = "";
    [JsonPropertyName("sent_at")] public string SentAt { get; set; } = "";
    [JsonPropertyName("item")] public JsonElement Item { get; set; }
}

/// <summary>application/problem+json error body.</summary>
public sealed class Problem : ApiObject
{
    [JsonPropertyName("type")] public string? Type { get; set; }
    [JsonPropertyName("title")] public string Title { get; set; } = "";
    [JsonPropertyName("status")] public int Status { get; set; }
    [JsonPropertyName("detail")] public string? Detail { get; set; }
}
