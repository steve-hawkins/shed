using System.Net;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.Extensions.Hosting;

namespace Shed.Web.Tests;

[Collection("WebTelemetryIsolation")]
public sealed class LandingPageTests(WebApplicationFactory<Program> factory)
    : IClassFixture<WebApplicationFactory<Program>>
{
    [Theory]
    [InlineData("/", "Shed idle compute, excess spend, and carbon intensity.")]
    [InlineData("/index.html", "Shed idle compute, excess spend, and carbon intensity.")]
    [InlineData("/how-it-works", "From raw infrastructure signals to concrete savings.")]
    [InlineData("/how-it-works.html", "From raw infrastructure signals to concrete savings.")]
    [InlineData("/recommendations", "Clear, ranked actions engineers can ship.")]
    [InlineData("/recommendations.html", "Clear, ranked actions engineers can ship.")]
    [InlineData("/telemetry", "Observable locally and in any OTLP pipeline.")]
    [InlineData("/telemetry.html", "Observable locally and in any OTLP pipeline.")]
    [InlineData("/early-access", "Shed is currently in active early build.")]
    [InlineData("/early-access.html", "Shed is currently in active early build.")]
    public async Task StaticPageReturnsSuccessHtmlAndSecurityHeaders(string route, string expectedHeading)
    {
        using var client = factory.CreateClient();

        using var response = await client.GetAsync(route);
        var body = await response.Content.ReadAsStringAsync();

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("text/html", response.Content.Headers.ContentType?.MediaType);
        Assert.Contains(expectedHeading, body, StringComparison.Ordinal);

        Assert.True(response.Headers.TryGetValues("X-Content-Type-Options", out var nosniff));
        Assert.Contains("nosniff", nosniff);
        Assert.True(response.Headers.TryGetValues("X-Frame-Options", out var frameOptions));
        Assert.Contains("DENY", frameOptions);
        Assert.True(response.Headers.TryGetValues("Content-Security-Policy", out var csp));
        Assert.Contains("script-src 'none'", string.Join(" ", csp), StringComparison.Ordinal);
        Assert.True(response.Headers.TryGetValues("Referrer-Policy", out var referrerPolicy));
        Assert.Contains("strict-origin-when-cross-origin", referrerPolicy);
    }

    [Theory]
    [InlineData("/css/site.css", "text/css")]
    [InlineData("/img/shed-mark.svg", "image/svg+xml")]
    public async Task StaticAssetsAreServedWithExpectedMediaType(string route, string expectedMediaType)
    {
        using var client = factory.CreateClient();

        using var response = await client.GetAsync(route);
        var body = await response.Content.ReadAsStringAsync();

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal(expectedMediaType, response.Content.Headers.ContentType?.MediaType);
        Assert.False(string.IsNullOrWhiteSpace(body));
    }

    [Theory]
    [InlineData("/health")]
    [InlineData("/alive")]
    public async Task HealthEndpointsReturnHealthyInDevelopment(string route)
    {
        using var client = factory.CreateClient();

        using var response = await client.GetAsync(route);
        var body = await response.Content.ReadAsStringAsync();

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal("Healthy", body);
    }

    [Theory]
    [InlineData("/health")]
    [InlineData("/alive")]
    public async Task HealthEndpointsAreNotExposedInProduction(string route)
    {
        await using var prodFactory = factory.WithWebHostBuilder(builder =>
            builder.UseEnvironment(Environments.Production));
        using var client = prodFactory.CreateClient();

        using var response = await client.GetAsync(route);

        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
    }

    [Fact]
    public async Task UnknownRouteReturnsNotFound()
    {
        using var client = factory.CreateClient();

        using var response = await client.GetAsync("/nonexistent-page");

        Assert.Equal(HttpStatusCode.NotFound, response.StatusCode);
    }
}
