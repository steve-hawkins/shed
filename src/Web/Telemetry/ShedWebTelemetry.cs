using System.Collections.Frozen;
using System.Diagnostics;
using System.Diagnostics.Metrics;

namespace Shed.Web.Telemetry;

/// <summary>
/// OpenTelemetry instrumentation primitives for Shed static web pages.
/// Uses a bounded set of known page routes to avoid high-cardinality metric labels
/// and prevent untrusted user input from leaking into telemetry attributes.
/// </summary>
public sealed class ShedWebTelemetry : IDisposable
{
    public const string SourceName = "Shed.Web";
    public const string MeterName = "Shed.Web";

    private static readonly FrozenDictionary<string, string> KnownPages =
        new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
        {
            ["/"] = "landing",
            ["/index.html"] = "landing",
            ["/how-it-works"] = "how-it-works",
            ["/how-it-works.html"] = "how-it-works",
            ["/recommendations"] = "recommendations",
            ["/recommendations.html"] = "recommendations",
            ["/telemetry"] = "telemetry",
            ["/telemetry.html"] = "telemetry",
            ["/early-access"] = "early-access",
            ["/early-access.html"] = "early-access",
        }.ToFrozenDictionary(StringComparer.OrdinalIgnoreCase);

    private readonly ActivitySource _activitySource;
    private readonly Counter<long> _pageViews;

    public ShedWebTelemetry(IMeterFactory meterFactory)
    {
        ArgumentNullException.ThrowIfNull(meterFactory);

        _activitySource = new ActivitySource(SourceName);
        var meter = meterFactory.Create(MeterName);
        _pageViews = meter.CreateCounter<long>(
            name: "shed.web.page_views",
            unit: "{view}",
            description: "Number of Shed static page views segmented by canonical page name.");
    }

    public static bool TryResolvePageName(string? requestPath, out string pageName)
    {
        if (string.IsNullOrWhiteSpace(requestPath))
        {
            pageName = string.Empty;
            return false;
        }

        var normalized = requestPath.Length > 1 ? requestPath.TrimEnd('/') : requestPath;
        if (KnownPages.TryGetValue(normalized, out var matched))
        {
            pageName = matched;
            return true;
        }

        pageName = string.Empty;
        return false;
    }

    public Activity? StartPageActivity(string pageName)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(pageName);

        var activity = _activitySource.StartActivity("shed.web.page.render", ActivityKind.Internal);
        activity?.SetTag("shed.page.name", pageName);
        return activity;
    }

    public void RecordPageView(string pageName)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(pageName);

        _pageViews.Add(1, new KeyValuePair<string, object?>("shed.page.name", pageName));
    }

    public void Dispose()
    {
        // Meter lifetime is managed by IMeterFactory; only dispose our ActivitySource.
        _activitySource.Dispose();
    }
}
