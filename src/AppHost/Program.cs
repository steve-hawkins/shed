using Aspire.Hosting;

var builder = DistributedApplication.CreateBuilder(args);

var web = builder.AddDotnetProject("shed", "../Web/Shed.Web.csproj")
    .WithExternalHttpEndpoints()
    .WithHttpHealthCheck("/health");

// Optional: forward telemetry to a local/external OpenTelemetry Collector alongside
// the local Aspire Dashboard when OTEL_COLLECTOR_ENDPOINT is provided.
var collectorEndpoint = builder.Configuration["OTEL_COLLECTOR_ENDPOINT"];
if (!string.IsNullOrWhiteSpace(collectorEndpoint))
{
    web.WithEnvironment("OTEL_COLLECTOR_ENDPOINT", collectorEndpoint);

    var collectorProtocol = builder.Configuration["OTEL_COLLECTOR_PROTOCOL"];
    if (!string.IsNullOrWhiteSpace(collectorProtocol))
    {
        web.WithEnvironment("OTEL_COLLECTOR_PROTOCOL", collectorProtocol);
    }

    var collectorHeaders = builder.Configuration["OTEL_COLLECTOR_HEADERS"];
    if (!string.IsNullOrWhiteSpace(collectorHeaders))
    {
        web.WithEnvironment("OTEL_COLLECTOR_HEADERS", collectorHeaders);
    }
}

await builder.Build().RunAsync();
