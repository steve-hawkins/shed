using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Diagnostics.HealthChecks;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Diagnostics.HealthChecks;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using OpenTelemetry.Exporter;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

namespace Shed.ServiceDefaults;

/// <summary>
/// Shared Aspire service defaults providing OpenTelemetry observability (compatible with
/// both the local .NET Aspire Dashboard and standalone/remote OTLP OpenTelemetry Collectors),
/// health checks, service discovery, and resilient HTTP clients.
/// </summary>
public static class Extensions
{
    public const string HealthEndpointPath = "/health";
    public const string AlivenessEndpointPath = "/alive";
    internal const string PrimaryExporterPrefix = "primary";
    internal const string CollectorExporterPrefix = "collector";
    private const string DefaultServiceNamespace = "shed";

    public static TBuilder AddServiceDefaults<TBuilder>(this TBuilder builder)
        where TBuilder : IHostApplicationBuilder
    {
        ArgumentNullException.ThrowIfNull(builder);

        builder.ConfigureOpenTelemetry();
        builder.AddDefaultHealthChecks();

        builder.Services.AddServiceDiscovery();

        builder.Services.ConfigureHttpClientDefaults(http =>
        {
            http.AddStandardResilienceHandler();
            http.AddServiceDiscovery();
        });

        return builder;
    }

    public static TBuilder ConfigureOpenTelemetry<TBuilder>(this TBuilder builder)
        where TBuilder : IHostApplicationBuilder
    {
        ArgumentNullException.ThrowIfNull(builder);

        var serviceNamespace = builder.Configuration["OpenTelemetry:ServiceNamespace"];
        if (string.IsNullOrWhiteSpace(serviceNamespace))
        {
            serviceNamespace = DefaultServiceNamespace;
        }

        var customMeters = builder.Configuration.GetSection("OpenTelemetry:Meters").Get<string[]>() ?? [];
        var customSources = builder.Configuration.GetSection("OpenTelemetry:Sources").Get<string[]>() ?? [];

        builder.Logging.AddOpenTelemetry(logging =>
        {
            logging.IncludeFormattedMessage = true;
            logging.IncludeScopes = true;
        });

        builder.Services.AddOpenTelemetry()
            .ConfigureResource(resource =>
            {
                resource.AddService(
                    serviceName: builder.Environment.ApplicationName,
                    serviceNamespace: serviceNamespace);
            })
            .WithMetrics(metrics =>
            {
                metrics.AddAspNetCoreInstrumentation()
                    .AddHttpClientInstrumentation()
                    .AddRuntimeInstrumentation();

                foreach (var meter in customMeters)
                {
                    metrics.AddMeter(meter);
                }
            })
            .WithTracing(tracing =>
            {
                tracing.AddSource(builder.Environment.ApplicationName)
                    .AddAspNetCoreInstrumentation(options =>
                    {
                        options.Filter = context =>
                            !context.Request.Path.StartsWithSegments(HealthEndpointPath)
                            && !context.Request.Path.StartsWithSegments(AlivenessEndpointPath);
                    })
                    .AddHttpClientInstrumentation();

                foreach (var source in customSources)
                {
                    tracing.AddSource(source);
                }
            });

        builder.AddOpenTelemetryExporters();

        return builder;
    }

    private static TBuilder AddOpenTelemetryExporters<TBuilder>(this TBuilder builder)
        where TBuilder : IHostApplicationBuilder
    {
        // Primary OTLP exporter: honors standard OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_PROTOCOL
        // (automatically injected by Aspire Dashboard locally, or set directly to an OTel Collector).
        var primaryEndpoint = ResolveEndpoint(
            builder.Configuration["OTEL_EXPORTER_OTLP_ENDPOINT"],
            builder.Configuration["OpenTelemetry:OtlpEndpoint"]);

        if (primaryEndpoint is not null)
        {
            var primaryProtocol = ResolveProtocol(
                builder.Configuration["OTEL_EXPORTER_OTLP_PROTOCOL"]
                ?? builder.Configuration["OpenTelemetry:OtlpProtocol"],
                primaryEndpoint);

            RegisterOtlpExporter(
                builder,
                exporterKey: PrimaryExporterPrefix,
                primaryEndpoint,
                primaryProtocol,
                overrideHeaders: null);
        }

        // Optional secondary OTel Collector exporter so local runs under Aspire Dashboard
        // can simultaneously forward telemetry to a local/external OpenTelemetry Collector.
        var collectorEndpoint = ResolveEndpoint(
            builder.Configuration["OTEL_COLLECTOR_ENDPOINT"],
            builder.Configuration["OpenTelemetry:CollectorEndpoint"]);

        if (collectorEndpoint is not null
            && (primaryEndpoint is null || !Uri.Equals(collectorEndpoint, primaryEndpoint)))
        {
            var collectorProtocol = ResolveProtocol(
                builder.Configuration["OTEL_COLLECTOR_PROTOCOL"]
                ?? builder.Configuration["OpenTelemetry:CollectorProtocol"],
                collectorEndpoint);

            // Isolate secondary collector headers so primary Aspire Dashboard keys
            // (OTEL_EXPORTER_OTLP_HEADERS) are never forwarded to an external collector.
            var collectorHeaders = builder.Configuration["OTEL_COLLECTOR_HEADERS"]
                ?? builder.Configuration["OpenTelemetry:CollectorHeaders"]
                ?? string.Empty;

            RegisterOtlpExporter(
                builder,
                exporterKey: CollectorExporterPrefix,
                collectorEndpoint,
                collectorProtocol,
                overrideHeaders: collectorHeaders);
        }

        return builder;
    }

    private static void RegisterOtlpExporter<TBuilder>(
        TBuilder builder,
        string exporterKey,
        Uri endpoint,
        OtlpExportProtocol protocol,
        string? overrideHeaders)
        where TBuilder : IHostApplicationBuilder
    {
        builder.Services.ConfigureOpenTelemetryTracerProvider(tracing =>
            tracing.AddOtlpExporter($"{exporterKey}-traces", options =>
            {
                options.Endpoint = ResolveSignalEndpoint(endpoint, protocol, "/v1/traces");
                options.Protocol = protocol;
                if (overrideHeaders is not null)
                {
                    options.Headers = overrideHeaders;
                }
            }));

        builder.Services.ConfigureOpenTelemetryMeterProvider(metrics =>
            metrics.AddOtlpExporter($"{exporterKey}-metrics", options =>
            {
                options.Endpoint = ResolveSignalEndpoint(endpoint, protocol, "/v1/metrics");
                options.Protocol = protocol;
                if (overrideHeaders is not null)
                {
                    options.Headers = overrideHeaders;
                }
            }));

        builder.Services.ConfigureOpenTelemetryLoggerProvider(logging =>
            logging.AddOtlpExporter($"{exporterKey}-logs", options =>
            {
                options.Endpoint = ResolveSignalEndpoint(endpoint, protocol, "/v1/logs");
                options.Protocol = protocol;
                if (overrideHeaders is not null)
                {
                    options.Headers = overrideHeaders;
                }
            }));
    }

    internal static Uri ResolveSignalEndpoint(Uri baseEndpoint, OtlpExportProtocol protocol, string signalPath)
    {
        ArgumentNullException.ThrowIfNull(baseEndpoint);
        ArgumentException.ThrowIfNullOrWhiteSpace(signalPath);

        if (protocol != OtlpExportProtocol.HttpProtobuf)
        {
            return baseEndpoint;
        }

        var normalizedSignalPath = signalPath.StartsWith('/') ? signalPath : "/" + signalPath;
        var currentPath = baseEndpoint.AbsolutePath.TrimEnd('/');

        if (currentPath.EndsWith(normalizedSignalPath, StringComparison.OrdinalIgnoreCase))
        {
            return baseEndpoint;
        }

        var builder = new UriBuilder(baseEndpoint)
        {
            Path = currentPath + normalizedSignalPath,
        };

        return builder.Uri;
    }

    internal static Uri? ResolveEndpoint(params string?[] candidates)
    {
        foreach (var candidate in candidates)
        {
            if (!string.IsNullOrWhiteSpace(candidate)
                && Uri.TryCreate(candidate.Trim(), UriKind.Absolute, out var uri)
                && (uri.Scheme == Uri.UriSchemeHttp || uri.Scheme == Uri.UriSchemeHttps))
            {
                return uri;
            }
        }

        return null;
    }

    internal static OtlpExportProtocol ResolveProtocol(string? configuredProtocol, Uri endpoint)
    {
        if (!string.IsNullOrWhiteSpace(configuredProtocol))
        {
            return configuredProtocol.Trim().ToLowerInvariant() switch
            {
                "http/protobuf" or "httpprotobuf" => OtlpExportProtocol.HttpProtobuf,
                "grpc" => OtlpExportProtocol.Grpc,
                _ => endpoint.Port == 4318 ? OtlpExportProtocol.HttpProtobuf : OtlpExportProtocol.Grpc,
            };
        }

        return endpoint.Port == 4318
            ? OtlpExportProtocol.HttpProtobuf
            : OtlpExportProtocol.Grpc;
    }

    public static TBuilder AddDefaultHealthChecks<TBuilder>(this TBuilder builder)
        where TBuilder : IHostApplicationBuilder
    {
        ArgumentNullException.ThrowIfNull(builder);

        builder.Services.AddHealthChecks()
            .AddCheck("self", () => HealthCheckResult.Healthy(), ["live"]);

        return builder;
    }

    public static WebApplication MapDefaultEndpoints(this WebApplication app)
    {
        ArgumentNullException.ThrowIfNull(app);

        // Adding health checks endpoints to applications in non-development environments has security implications.
        // Restrict default public exposure to Development until private/internal probe routing is configured.
        if (app.Environment.IsDevelopment())
        {
            app.MapHealthChecks(HealthEndpointPath);

            app.MapHealthChecks(AlivenessEndpointPath, new HealthCheckOptions
            {
                Predicate = registration => registration.Tags.Contains("live"),
            });
        }

        return app;
    }
}
