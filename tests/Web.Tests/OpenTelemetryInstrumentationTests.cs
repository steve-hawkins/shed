using System.Diagnostics;
using System.Net;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using Microsoft.Extensions.Options;
using OpenTelemetry;
using OpenTelemetry.Exporter;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Trace;
using Shed.ServiceDefaults;
using Shed.Web.Telemetry;

namespace Shed.Web.Tests;

[Collection("WebTelemetryIsolation")]
public sealed class OpenTelemetryInstrumentationTests
{
    private static readonly Action<ILogger, Exception?> LogTelemetrySmoke =
        LoggerMessage.Define(LogLevel.Information, new EventId(1001, "TelemetrySmoke"), "Shed telemetry smoke log verification");

    [Fact]
    public async Task KnownStaticPagesEmitTracesMetricsLogsAndResourceMetadata()
    {
        var exportedActivities = new List<Activity>();
        var exportedMetrics = new List<Metric>();
        var exportedLogs = new List<LogRecord>();

        await using var factory = new WebApplicationFactory<Program>()
            .WithWebHostBuilder(builder =>
            {
                builder.ConfigureServices(services =>
                {
                    services.ConfigureOpenTelemetryTracerProvider(tracing =>
                        tracing.AddInMemoryExporter(exportedActivities));
                    services.ConfigureOpenTelemetryMeterProvider(metrics =>
                        metrics.AddInMemoryExporter(exportedMetrics));
                    services.ConfigureOpenTelemetryLoggerProvider(logging =>
                        logging.AddInMemoryExporter(exportedLogs));
                });
            });

        using var client = factory.CreateClient();

        using var homeResponse = await client.GetAsync("/");
        using var conditionalRequest = new HttpRequestMessage(HttpMethod.Get, "/");
        if (homeResponse.Headers.ETag is not null)
        {
            conditionalRequest.Headers.IfNoneMatch.Add(homeResponse.Headers.ETag);
        }

        using var conditionalHomeResponse = await client.SendAsync(conditionalRequest);
        using var recResponse = await client.GetAsync("/recommendations");
        using var caseInsensitiveResponse = await client.GetAsync("/HOW-IT-WORKS");
        using var postHomeResponse = await client.PostAsync("/", content: null);
        using var missingResponse = await client.GetAsync("/does-not-exist-sensitive-token");
        using var healthResponse = await client.GetAsync("/health");

        Assert.Equal(HttpStatusCode.OK, homeResponse.StatusCode);
        Assert.Equal(HttpStatusCode.NotModified, conditionalHomeResponse.StatusCode);
        Assert.Equal(HttpStatusCode.OK, recResponse.StatusCode);
        Assert.Equal(HttpStatusCode.OK, caseInsensitiveResponse.StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, missingResponse.StatusCode);
        Assert.Equal(HttpStatusCode.OK, healthResponse.StatusCode);
        Assert.NotEqual(HttpStatusCode.OK, postHomeResponse.StatusCode);

        var logger = factory.Services.GetRequiredService<ILogger<OpenTelemetryInstrumentationTests>>();
        LogTelemetrySmoke(logger, null);

        var tracerProvider = factory.Services.GetRequiredService<TracerProvider>();
        var meterProvider = factory.Services.GetRequiredService<MeterProvider>();
        var loggerProvider = factory.Services.GetRequiredService<LoggerProvider>();
        tracerProvider.ForceFlush();
        meterProvider.ForceFlush();
        loggerProvider.ForceFlush();

        // Verify OpenTelemetry Resource attributes include service.name and service.namespace
        var resourceAttributes = tracerProvider.GetResource().Attributes
            .ToDictionary(kv => kv.Key, kv => kv.Value, StringComparer.Ordinal);
        Assert.Equal("shed", resourceAttributes["service.namespace"]);
        Assert.True(resourceAttributes.ContainsKey("service.name"));
        Assert.False(string.IsNullOrWhiteSpace(resourceAttributes["service.name"]?.ToString()));

        // Verify custom page render traces (including HTTP status code tags on 200 and 304) and health probe filtering
        var pageActivities = exportedActivities
            .Where(a => a.Source.Name == ShedWebTelemetry.SourceName && a.DisplayName == "shed.web.page.render")
            .ToList();

        Assert.Equal(4, pageActivities.Count);
        Assert.Contains(pageActivities, a =>
            Equals(a.GetTagItem("shed.page.name"), "landing")
            && Equals(a.GetTagItem("http.response.status_code"), 200));
        Assert.Contains(pageActivities, a =>
            Equals(a.GetTagItem("shed.page.name"), "landing")
            && Equals(a.GetTagItem("http.response.status_code"), 304));
        Assert.Contains(pageActivities, a =>
            Equals(a.GetTagItem("shed.page.name"), "recommendations")
            && Equals(a.GetTagItem("http.response.status_code"), 200));
        Assert.Contains(pageActivities, a =>
            Equals(a.GetTagItem("shed.page.name"), "how-it-works")
            && Equals(a.GetTagItem("http.response.status_code"), 200));
        Assert.DoesNotContain(exportedActivities, a =>
            Equals(a.GetTagItem("url.path"), "/health")
            || Equals(a.GetTagItem("http.target"), "/health"));

        // Verify bounded page view metrics (no 404/untrusted paths, no POST count)
        var pageViewMetric = exportedMetrics.LastOrDefault(m => m.Name == "shed.web.page_views");
        Assert.NotNull(pageViewMetric);

        var recordedTags = new Dictionary<string, long>(StringComparer.Ordinal);
        foreach (ref readonly var point in pageViewMetric.GetMetricPoints())
        {
            foreach (var tag in point.Tags)
            {
                if (tag.Key == "shed.page.name" && tag.Value is string name)
                {
                    recordedTags[name] = point.GetSumLong();
                }
            }
        }

        Assert.Equal(3, recordedTags.Count);
        Assert.Equal(1, recordedTags["landing"]);
        Assert.Equal(1, recordedTags["recommendations"]);
        Assert.Equal(1, recordedTags["how-it-works"]);
        Assert.DoesNotContain("does-not-exist-sensitive-token", recordedTags.Keys);

        // Verify OpenTelemetry structured log export
        Assert.Contains(exportedLogs, log =>
            string.Equals(log.FormattedMessage, "Shed telemetry smoke log verification", StringComparison.Ordinal));
    }

    [Fact]
    public async Task DualExporterConfigurationIsolatesSignalEndpointsAndCollectorHeaders()
    {
        await using var factory = new WebApplicationFactory<Program>()
            .WithWebHostBuilder(builder =>
            {
                builder.UseSetting("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:19180");
                builder.UseSetting("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc");
                builder.UseSetting("OTEL_EXPORTER_OTLP_HEADERS", "x-otlp-api-key=aspire-local-only");
                builder.UseSetting("OTEL_COLLECTOR_ENDPOINT", "http://127.0.0.1:4318");
                builder.UseSetting("OTEL_COLLECTOR_PROTOCOL", "http/protobuf");
            });

        using var client = factory.CreateClient();
        using var response = await client.GetAsync("/telemetry");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);

        var optionsMonitor = factory.Services.GetRequiredService<IOptionsMonitor<OtlpExporterOptions>>();

        var primaryTraces = optionsMonitor.Get($"{Extensions.PrimaryExporterPrefix}-traces");
        var primaryMetrics = optionsMonitor.Get($"{Extensions.PrimaryExporterPrefix}-metrics");
        var primaryLogs = optionsMonitor.Get($"{Extensions.PrimaryExporterPrefix}-logs");

        Assert.Equal(OtlpExportProtocol.Grpc, primaryTraces.Protocol);
        Assert.Equal(new Uri("http://127.0.0.1:19180/"), primaryTraces.Endpoint);
        Assert.Equal(new Uri("http://127.0.0.1:19180/"), primaryMetrics.Endpoint);
        Assert.Equal(new Uri("http://127.0.0.1:19180/"), primaryLogs.Endpoint);
        Assert.Equal("x-otlp-api-key=aspire-local-only", primaryTraces.Headers);
        Assert.Equal("x-otlp-api-key=aspire-local-only", primaryMetrics.Headers);
        Assert.Equal("x-otlp-api-key=aspire-local-only", primaryLogs.Headers);

        var collectorTraces = optionsMonitor.Get($"{Extensions.CollectorExporterPrefix}-traces");
        var collectorMetrics = optionsMonitor.Get($"{Extensions.CollectorExporterPrefix}-metrics");
        var collectorLogs = optionsMonitor.Get($"{Extensions.CollectorExporterPrefix}-logs");

        Assert.Equal(OtlpExportProtocol.HttpProtobuf, collectorTraces.Protocol);
        Assert.Equal(new Uri("http://127.0.0.1:4318/v1/traces"), collectorTraces.Endpoint);
        Assert.Equal(new Uri("http://127.0.0.1:4318/v1/metrics"), collectorMetrics.Endpoint);
        Assert.Equal(new Uri("http://127.0.0.1:4318/v1/logs"), collectorLogs.Endpoint);

        // Ensure primary Aspire Dashboard headers are never leaked to the secondary collector exporter
        Assert.Equal(string.Empty, collectorTraces.Headers);
        Assert.Equal(string.Empty, collectorMetrics.Headers);
        Assert.Equal(string.Empty, collectorLogs.Headers);
    }

    [Fact]
    public async Task SecondaryCollectorForwardsExplicitHeadersWhileRemainingIsolatedFromPrimary()
    {
        await using var factory = new WebApplicationFactory<Program>()
            .WithWebHostBuilder(builder =>
            {
                builder.UseSetting("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:19180");
                builder.UseSetting("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc");
                builder.UseSetting("OTEL_EXPORTER_OTLP_HEADERS", "x-otlp-api-key=aspire-primary");
                builder.UseSetting("OTEL_COLLECTOR_ENDPOINT", "http://127.0.0.1:4318");
                builder.UseSetting("OTEL_COLLECTOR_PROTOCOL", "http/protobuf");
                builder.UseSetting("OTEL_COLLECTOR_HEADERS", "Authorization=Basic Y29sbGVjdG9y");
            });

        using var client = factory.CreateClient();
        using var response = await client.GetAsync("/telemetry");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);

        var optionsMonitor = factory.Services.GetRequiredService<IOptionsMonitor<OtlpExporterOptions>>();

        Assert.Equal("x-otlp-api-key=aspire-primary", optionsMonitor.Get($"{Extensions.PrimaryExporterPrefix}-traces").Headers);
        Assert.Equal("x-otlp-api-key=aspire-primary", optionsMonitor.Get($"{Extensions.PrimaryExporterPrefix}-metrics").Headers);
        Assert.Equal("x-otlp-api-key=aspire-primary", optionsMonitor.Get($"{Extensions.PrimaryExporterPrefix}-logs").Headers);

        Assert.Equal("Authorization=Basic Y29sbGVjdG9y", optionsMonitor.Get($"{Extensions.CollectorExporterPrefix}-traces").Headers);
        Assert.Equal("Authorization=Basic Y29sbGVjdG9y", optionsMonitor.Get($"{Extensions.CollectorExporterPrefix}-metrics").Headers);
        Assert.Equal("Authorization=Basic Y29sbGVjdG9y", optionsMonitor.Get($"{Extensions.CollectorExporterPrefix}-logs").Headers);
    }

    [Fact]
    public async Task EqualPrimaryAndCollectorEndpointsSkipDuplicateCollectorRegistration()
    {
        await using var factory = new WebApplicationFactory<Program>()
            .WithWebHostBuilder(builder =>
            {
                builder.UseSetting("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318");
                builder.UseSetting("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf");
                builder.UseSetting("OTEL_COLLECTOR_ENDPOINT", "http://127.0.0.1:4318");
                builder.UseSetting("OTEL_COLLECTOR_PROTOCOL", "http/protobuf");
                builder.UseSetting("OTEL_COLLECTOR_HEADERS", "Authorization=Basic ZHVwbGljYXRl");
            });

        using var client = factory.CreateClient();
        using var response = await client.GetAsync("/");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);

        var optionsMonitor = factory.Services.GetRequiredService<IOptionsMonitor<OtlpExporterOptions>>();

        // Primary exporter is configured for HTTP/protobuf signal endpoints
        Assert.Equal(
            new Uri("http://127.0.0.1:4318/v1/traces"),
            optionsMonitor.Get($"{Extensions.PrimaryExporterPrefix}-traces").Endpoint);

        // Secondary collector exporter was skipped due to endpoint equality, so its overrides were not applied
        var collectorTraces = optionsMonitor.Get($"{Extensions.CollectorExporterPrefix}-traces");
        Assert.NotEqual("Authorization=Basic ZHVwbGljYXRl", collectorTraces.Headers);
        Assert.NotEqual(new Uri("http://127.0.0.1:4318/v1/traces"), collectorTraces.Endpoint);
    }

    [Fact]
    public async Task PrimaryHttpProtobufExporterConfiguresDistinctSignalEndpoints()
    {
        await using var factory = new WebApplicationFactory<Program>()
            .WithWebHostBuilder(builder =>
            {
                builder.UseSetting("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318");
                builder.UseSetting("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf");
            });

        using var client = factory.CreateClient();
        using var response = await client.GetAsync("/");
        Assert.Equal(HttpStatusCode.OK, response.StatusCode);

        var optionsMonitor = factory.Services.GetRequiredService<IOptionsMonitor<OtlpExporterOptions>>();

        Assert.Equal(
            new Uri("http://127.0.0.1:4318/v1/traces"),
            optionsMonitor.Get($"{Extensions.PrimaryExporterPrefix}-traces").Endpoint);
        Assert.Equal(
            new Uri("http://127.0.0.1:4318/v1/metrics"),
            optionsMonitor.Get($"{Extensions.PrimaryExporterPrefix}-metrics").Endpoint);
        Assert.Equal(
            new Uri("http://127.0.0.1:4318/v1/logs"),
            optionsMonitor.Get($"{Extensions.PrimaryExporterPrefix}-logs").Endpoint);
    }

    [Theory]
    [InlineData(null, "http://127.0.0.1:4317", OtlpExportProtocol.Grpc)]
    [InlineData(null, "http://127.0.0.1:4318", OtlpExportProtocol.HttpProtobuf)]
    [InlineData("http/protobuf", "http://127.0.0.1:4317", OtlpExportProtocol.HttpProtobuf)]
    [InlineData("grpc", "http://127.0.0.1:4318", OtlpExportProtocol.Grpc)]
    [InlineData("unknown", "http://127.0.0.1:4318", OtlpExportProtocol.HttpProtobuf)]
    public void ResolveProtocolHonorsExplicitOverrideAndPortConvention(
        string? configuredProtocol,
        string endpoint,
        OtlpExportProtocol expected)
    {
        var actual = Extensions.ResolveProtocol(configuredProtocol, new Uri(endpoint));

        Assert.Equal(expected, actual);
    }

    [Theory]
    [InlineData(null, null)]
    [InlineData("", "   ")]
    [InlineData("not-a-uri", "file:///etc/passwd")]
    [InlineData("javascript:alert(1)", "ftp://collector.local:4317")]
    public void ResolveEndpointRejectsInvalidAndNonHttpSchemes(string? first, string? second)
    {
        var resolved = Extensions.ResolveEndpoint(first, second);

        Assert.Null(resolved);
    }

    [Fact]
    public void ResolveEndpointSelectsFirstValidHttpOrHttpsUri()
    {
        var resolved = Extensions.ResolveEndpoint("invalid", "http://127.0.0.1:4318", "https://otlp.example.com");

        Assert.Equal(new Uri("http://127.0.0.1:4318"), resolved);
    }

    [Theory]
    [InlineData("http://127.0.0.1:4317", OtlpExportProtocol.Grpc, "/v1/traces", "http://127.0.0.1:4317/")]
    [InlineData("http://127.0.0.1:4318", OtlpExportProtocol.HttpProtobuf, "/v1/traces", "http://127.0.0.1:4318/v1/traces")]
    [InlineData("http://127.0.0.1:4318", OtlpExportProtocol.HttpProtobuf, "/v1/metrics", "http://127.0.0.1:4318/v1/metrics")]
    [InlineData("http://127.0.0.1:4318", OtlpExportProtocol.HttpProtobuf, "/v1/logs", "http://127.0.0.1:4318/v1/logs")]
    [InlineData("https://otlp.example.com/otlp/", OtlpExportProtocol.HttpProtobuf, "/v1/traces", "https://otlp.example.com/otlp/v1/traces")]
    [InlineData("http://127.0.0.1:4318/v1/traces", OtlpExportProtocol.HttpProtobuf, "/v1/traces", "http://127.0.0.1:4318/v1/traces")]
    public void ResolveSignalEndpointAppendsSignalPathForHttpProtobufOnly(
        string baseUrl,
        OtlpExportProtocol protocol,
        string signalPath,
        string expectedUrl)
    {
        var actual = Extensions.ResolveSignalEndpoint(new Uri(baseUrl), protocol, signalPath);

        Assert.Equal(new Uri(expectedUrl), actual);
    }

    [Theory]
    [InlineData("/", true, "landing")]
    [InlineData("/recommendations/", true, "recommendations")]
    [InlineData("/TELEMETRY", true, "telemetry")]
    [InlineData("/unknown", false, "")]
    [InlineData("", false, "")]
    [InlineData(null, false, "")]
    public void TryResolvePageNameEnforcesBoundedPageAllowlist(string? path, bool expectedMatch, string expectedName)
    {
        var matched = ShedWebTelemetry.TryResolvePageName(path, out var pageName);

        Assert.Equal(expectedMatch, matched);
        Assert.Equal(expectedName, pageName);
    }
}
