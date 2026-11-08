// BiG360.exe - launcher stub for the BiG Equity 360 tray app (O3).
// Starts "<root>\runtime\pythonw.exe -m app.desktop.tray [args]" (installed / portable build) or, in a development
// tree, "<root>\.venv\Scripts\pythonw.exe -m app.desktop.tray [args]", with the working directory <root>\backend.
// No console window. Arguments are passed through (--background, --quit, --no-supervisor).
// Build: packaging\build_launcher.ps1 (csc.exe of the .NET Framework 4.x that ships with Windows).
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;

[assembly: AssemblyTitle("BiG Equity 360")]
[assembly: AssemblyProduct("BiG Equity 360")]
[assembly: AssemblyDescription("BiG Equity 360 tray launcher")]

static class Launcher
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    static extern int MessageBoxW(IntPtr hWnd, string text, string caption, uint type);

    // Windows command-line quoting (CommandLineToArgvW rules)
    static string Quote(string a)
    {
        if (a.Length > 0 && a.IndexOfAny(new[] { ' ', '\t', '"' }) < 0) return a;
        var sb = new StringBuilder("\"");
        int bs = 0;
        foreach (char c in a)
        {
            if (c == '\\') { bs++; continue; }
            if (c == '"') { sb.Append('\\', bs * 2 + 1); sb.Append('"'); bs = 0; continue; }
            sb.Append('\\', bs); bs = 0; sb.Append(c);
        }
        sb.Append('\\', bs * 2);
        sb.Append('"');
        return sb.ToString();
    }

    [STAThread]
    static int Main(string[] args)
    {
        string root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string[] candidates = {
            Path.Combine(Path.Combine(root, "runtime"), "pythonw.exe"),
            Path.Combine(Path.Combine(Path.Combine(root, ".venv"), "Scripts"), "pythonw.exe"),
        };
        string py = null;
        foreach (string c in candidates) { if (File.Exists(c)) { py = c; break; } }
        string backend = Path.Combine(root, "backend");
        if (py == null || !Directory.Exists(Path.Combine(backend, "app")))
        {
            MessageBoxW(IntPtr.Zero, "BiG Equity 360: Python runtime or application files not found next to " +
                        "BiG360.exe.\n\nExpected runtime\\pythonw.exe (installed) or .venv\\Scripts\\pythonw.exe " +
                        "(development tree) and backend\\app.\n\nFolder: " + root, "BiG Equity 360", 0x10);
            return 1;
        }
        var cmd = new StringBuilder("-m app.desktop.tray");
        foreach (string a in args) { cmd.Append(' '); cmd.Append(Quote(a)); }
        var psi = new ProcessStartInfo(py, cmd.ToString());
        psi.WorkingDirectory = backend;
        psi.UseShellExecute = false;
        psi.CreateNoWindow = true;
        psi.EnvironmentVariables["BIG360_APP"] = "1";
        try
        {
            Process p = Process.Start(psi);
            bool waitFor = Array.IndexOf(args, "--quit") >= 0 || Array.IndexOf(args, "--status") >= 0;
            if (waitFor && p != null) { p.WaitForExit(); return p.ExitCode; }
        }
        catch (Exception e)
        {
            MessageBoxW(IntPtr.Zero, "BiG Equity 360 could not start:\n" + e.Message, "BiG Equity 360", 0x10);
            return 1;
        }
        return 0;
    }
}
