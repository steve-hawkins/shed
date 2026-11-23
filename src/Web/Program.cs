using Shed.ServiceDefaults;
using Shed.Web.Telemetry;

var builder = WebApplication.CreateBuilder(args);

builder.AddServiceDefaults();
builder.Services.AddSingleton<ShedWebTelemetry>();
builder.Services.AddOpenTelemetry()
    .WithTracing(tracing => tracing.AddSource(ShedWebTelemetry.SourceName))
    .WithMetrics(metrics => metrics.AddMeter(ShedWebTelemetry.MeterName));

var app = builder.Build();

if (!app.Environment.IsDevelopment())
{
    app.UseHsts();
}

// Baseline security headers for static content delivery
app.Use(async (context, next) =>
{
    var headers = context.Response.Headers;
    headers["X-Content-Type-Options"] = "nosniff";
    headers["X-Frame-Options"] = "DENY";
    headers["Referrer-Policy"] = "strict-origin-when-cross-origin";
    headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()";
    headers["Content-Security-Policy"] =
        "default-src 'self'; style-src 'self'; img-src 'self' data:; font-src 'self'; script-src 'none'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'; form-action 'self'";

    await next();
});

// Clean extensionless URL rewriting for static placeholder pages
var cleanRoutes = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase)
{
    ["/how-it-works"] = "/how-it-works.html",
    ["/recommendations"] = "/recommendations.html",
    ["/telemetry"] = "/telemetry.html",
    ["/early-access"] = "/early-access.html",
};

app.Use(async (context, next) =>
{
    var rawPath = context.Request.Path.Value;
    var telemetry = context.RequestServices.GetRequiredService<ShedWebTelemetry>();

    if (!string.IsNullOrEmpty(rawPath))
    {
        var trimmed = rawPath.Length > 1 ? rawPath.TrimEnd('/') : rawPath;
        if (cleanRoutes.TryGetValue(trimmed, out var targetFile))
        {
            context.Request.Path = targetFile;
        }
    }

    if (HttpMethods.IsGet(context.Request.Method)
        && ShedWebTelemetry.TryResolvePageName(rawPath, out var pageName))
    {
        using var activity = telemetry.StartPageActivity(pageName);
        await next();

        activity?.SetTag("http.response.status_code", context.Response.StatusCode);

        if (context.Response.StatusCode == StatusCodes.Status200OK)
        {
            telemetry.RecordPageView(pageName);
        }

        return;
    }

    await next();
});

app.UseDefaultFiles();
app.UseStaticFiles();

app.MapDefaultEndpoints();

await app.RunAsync();

public partial class Program;
