using System.Text.Json;
using System.Xml.Linq;
using NetArchTest.Rules;
using Shed.Web.Telemetry;

namespace Shed.Architecture.Tests;

public sealed class SolutionArchitectureTests
{
    [Fact]
    public void ServiceDefaultsMustNotDependOnWebOrAppHost()
    {
        var result = Types.InAssembly(typeof(Shed.ServiceDefaults.Extensions).Assembly)
            .ShouldNot()
            .HaveDependencyOnAny("Shed.Web", "Shed.AppHost", "Microsoft.EntityFrameworkCore")
            .GetResult();

        Assert.True(result.IsSuccessful, $"Forbidden dependencies found: {string.Join(", ", result.FailingTypeNames ?? [])}");
    }

    [Fact]
    public void WebMustNotDependOnAppHostOrEfCore()
    {
        var result = Types.InAssembly(typeof(ShedWebTelemetry).Assembly)
            .ShouldNot()
            .HaveDependencyOnAny("Shed.AppHost", "Microsoft.EntityFrameworkCore")
            .GetResult();

        Assert.True(result.IsSuccessful, $"Forbidden dependencies found: {string.Join(", ", result.FailingTypeNames ?? [])}");
    }

    [Fact]
    public void SolutionMustEnforceNetLtsFrameworkPolicyAcrossSdkAndTargets()
    {
        var repoRoot = FindRepoRoot();

        // 1. global.json must pin an even-numbered .NET LTS SDK (currently .NET 10)
        using var globalJson = JsonDocument.Parse(File.ReadAllText(Path.Combine(repoRoot, "global.json")));
        var sdkVersion = globalJson.RootElement.GetProperty("sdk").GetProperty("version").GetString();
        Assert.NotNull(sdkVersion);
        var sdkMajor = ParseMajorVersion(sdkVersion);
        Assert.True(sdkMajor >= 10 && sdkMajor % 2 == 0, $"SDK version '{sdkVersion}' must target an even-numbered .NET LTS release.");
        Assert.StartsWith("10.", sdkVersion, StringComparison.Ordinal);

        // 2. Directory.Build.props and all *.csproj files must target an even-numbered .NET LTS TFM (currently net10.0).
        // NuGet package dependency versions are intentionally excluded from this check.
        var propsDoc = XDocument.Load(Path.Combine(repoRoot, "Directory.Build.props"));
        var targetFramework = propsDoc.Descendants("TargetFramework").Single().Value;
        AssertLtsTargetFramework(targetFramework);

        var projectFiles = Directory.EnumerateFiles(repoRoot, "*.csproj", SearchOption.AllDirectories)
            .Where(path => !path.Contains($"{Path.DirectorySeparatorChar}bin{Path.DirectorySeparatorChar}", StringComparison.Ordinal)
                && !path.Contains($"{Path.DirectorySeparatorChar}obj{Path.DirectorySeparatorChar}", StringComparison.Ordinal))
            .ToList();

        Assert.NotEmpty(projectFiles);

        foreach (var projectFile in projectFiles)
        {
            var doc = XDocument.Load(projectFile);
            var singleTargets = doc.Descendants("TargetFramework").Select(e => e.Value);
            var multiTargets = doc.Descendants("TargetFrameworks")
                .SelectMany(e => e.Value.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries));

            Assert.All(singleTargets.Concat(multiTargets), AssertLtsTargetFramework);
        }
    }

    private static void AssertLtsTargetFramework(string tfm)
    {
        Assert.StartsWith("net", tfm, StringComparison.Ordinal);
        var versionPart = tfm["net".Length..];
        var major = ParseMajorVersion(versionPart);
        Assert.True(major >= 10 && major % 2 == 0, $"TargetFramework '{tfm}' must be an even-numbered .NET LTS framework.");
        Assert.Equal("net10.0", tfm);
    }

    private static int ParseMajorVersion(string version)
    {
        var dotIndex = version.IndexOf('.');
        var majorToken = dotIndex > 0 ? version[..dotIndex] : version;
        Assert.True(int.TryParse(majorToken, out var major), $"Unable to parse major version from '{version}'.");
        return major;
    }

    private static string FindRepoRoot()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null)
        {
            if (File.Exists(Path.Combine(dir.FullName, "Shed.slnx")))
            {
                return dir.FullName;
            }

            dir = dir.Parent;
        }

        throw new InvalidOperationException("Could not locate Shed.slnx from test output directory.");
    }
}
