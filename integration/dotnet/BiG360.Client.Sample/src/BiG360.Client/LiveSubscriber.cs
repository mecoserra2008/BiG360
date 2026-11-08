// Resuming subscriber for the SSE live stream: reconnects with back-off and resumes from the last event id
// (Last-Event-ID), which the server honours for the hot-store retention window (filings 14 d, news 7 d,
// events 72 h, macro 60 d, alerts 30 d). This is the loop a bridge to Service Bus / RabbitMQ / Kafka would run.

namespace BiG360.Client;

public sealed class LiveSubscriber
{
    private readonly Big360Client _client;
    private readonly string _topics;

    /// <summary>Last data event id seen; persist it (file / DB) to resume after a restart.</summary>
    public long? LastEventId { get; private set; }

    public LiveSubscriber(Big360Client client, string topics = "filings,news,macro,alerts", long? lastEventId = null)
    {
        _client = client;
        _topics = topics;
        LastEventId = lastEventId;
    }

    /// <summary>Runs until cancelled. onEvent is awaited for each data event (at-least-once delivery).</summary>
    public async Task RunAsync(Func<LiveStreamEvent, Task> onEvent, Action<string>? onStatus = null, CancellationToken ct = default)
    {
        var backoff = TimeSpan.FromSeconds(1);
        while (!ct.IsCancellationRequested)
        {
            try
            {
                await foreach (var frame in _client.StreamLiveAsync(_topics, LastEventId, ct: ct).ConfigureAwait(false))
                {
                    backoff = TimeSpan.FromSeconds(1);
                    if (frame.Event == "hello") { onStatus?.Invoke("connected: " + frame.Data); continue; }
                    if (frame.Event == "heartbeat") { onStatus?.Invoke("heartbeat"); continue; }
                    var ev = Big360Client.ParseEvent(frame);
                    if (ev is null) continue;
                    await onEvent(ev).ConfigureAwait(false);
                    LastEventId = ev.Id; // commit after the handler succeeded
                }
                if (ct.IsCancellationRequested) return;
                onStatus?.Invoke("stream closed by server");
            }
            catch (OperationCanceledException) when (ct.IsCancellationRequested)
            {
                return;
            }
            catch (Exception e) when (e is HttpRequestException or IOException or Big360ApiException)
            {
                onStatus?.Invoke($"disconnected ({e.GetType().Name}: {e.Message}); retry in {backoff.TotalSeconds:0}s");
            }
            try
            {
                await Task.Delay(backoff, ct).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                return;
            }
            backoff = TimeSpan.FromSeconds(Math.Min(60, backoff.TotalSeconds * 2));
        }
    }
}
