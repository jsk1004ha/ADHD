using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Threading.Tasks;
using System.Windows.Forms;

internal static class SetupLauncher
{
    private const string ManifestHash = "__MANIFEST_SHA256__";
    private const string Version = "__VERSION__";

    private sealed class Options
    {
        internal string CodexHome;
        internal string Report;
        internal bool Quiet;
        internal bool ExplicitHome;
    }

    private static string Hash(byte[] data)
    {
        using (SHA256 sha = SHA256.Create())
        {
            byte[] digest = sha.ComputeHash(data);
            StringBuilder result = new StringBuilder(digest.Length * 2);
            foreach (byte value in digest) result.Append(value.ToString("x2"));
            return result.ToString();
        }
    }

    private static byte[] ReadBounded(Stream input, long limit)
    {
        using (MemoryStream result = new MemoryStream())
        {
            byte[] buffer = new byte[65536];
            int read;
            while ((read = input.Read(buffer, 0, buffer.Length)) > 0)
            {
                if (result.Length + read > limit) throw new InvalidDataException("Setup payload exceeds its size limit.");
                result.Write(buffer, 0, read);
            }
            return result.ToArray();
        }
    }

    private static bool SafeName(string name)
    {
        if (!name.StartsWith("source/", StringComparison.Ordinal) || name.IndexOf('\\') >= 0 ||
            name.IndexOf(':') >= 0 || name.EndsWith("/", StringComparison.Ordinal)) return false;
        string[] parts = name.Split('/');
        foreach (string part in parts)
            if (part.Length == 0 || part == "." || part == "..") return false;
        return true;
    }

    private static void ExtractChecked(string destination)
    {
        using (FileStream file = File.OpenRead(Application.ExecutablePath))
        using (ZipArchive archive = new ZipArchive(file, ZipArchiveMode.Read, false))
        {
            Dictionary<string, ZipArchiveEntry> entries = new Dictionary<string, ZipArchiveEntry>(StringComparer.Ordinal);
            foreach (ZipArchiveEntry entry in archive.Entries)
            {
                if (entries.ContainsKey(entry.FullName)) throw new InvalidDataException("Repeated setup payload path.");
                if (entry.FullName != "MANIFEST.sha256" && !SafeName(entry.FullName))
                    throw new InvalidDataException("Unsafe setup payload path.");
                if (((entry.ExternalAttributes >> 16) & 0xF000) == 0xA000)
                    throw new InvalidDataException("Linked setup payload path.");
                entries.Add(entry.FullName, entry);
            }
            ZipArchiveEntry manifestEntry;
            if (!entries.TryGetValue("MANIFEST.sha256", out manifestEntry))
                throw new InvalidDataException("Setup payload manifest is missing.");
            byte[] manifestBytes;
            using (Stream source = manifestEntry.Open()) manifestBytes = ReadBounded(source, 250000);
            if (!String.Equals(Hash(manifestBytes), ManifestHash, StringComparison.Ordinal))
                throw new InvalidDataException("Setup payload manifest changed.");
            Dictionary<string, string> expected = new Dictionary<string, string>(StringComparer.Ordinal);
            foreach (string line in Encoding.UTF8.GetString(manifestBytes).Split('\n'))
            {
                if (line.Length == 0) continue;
                int separator = line.IndexOf('\t');
                if (separator != 64 || line.IndexOf('\t', separator + 1) >= 0)
                    throw new InvalidDataException("Invalid setup payload manifest.");
                string name = line.Substring(separator + 1);
                if (!SafeName(name) || expected.ContainsKey(name))
                    throw new InvalidDataException("Invalid setup payload inventory.");
                expected.Add(name, line.Substring(0, separator));
            }
            if (expected.Count != entries.Count - 1)
                throw new InvalidDataException("Setup payload inventory changed.");
            string root = Path.GetFullPath(destination).TrimEnd(Path.DirectorySeparatorChar) + Path.DirectorySeparatorChar;
            long total = 0;
            foreach (KeyValuePair<string, string> item in expected)
            {
                ZipArchiveEntry entry;
                if (!entries.TryGetValue(item.Key, out entry))
                    throw new InvalidDataException("Setup payload file is missing.");
                if (entry.Length < 0 || entry.Length > 100 * 1024 * 1024 || total + entry.Length > 700 * 1024 * 1024)
                    throw new InvalidDataException("Setup payload exceeds its size limit.");
                total += entry.Length;
                byte[] contents;
                using (Stream source = entry.Open()) contents = ReadBounded(source, entry.Length);
                if (contents.LongLength != entry.Length || !String.Equals(Hash(contents), item.Value, StringComparison.Ordinal))
                    throw new InvalidDataException("Setup payload file changed: " + item.Key);
                string target = Path.GetFullPath(Path.Combine(destination, item.Key.Replace('/', Path.DirectorySeparatorChar)));
                if (!target.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidDataException("Unsafe setup payload destination.");
                Directory.CreateDirectory(Path.GetDirectoryName(target));
                using (FileStream output = new FileStream(target, FileMode.CreateNew, FileAccess.Write))
                    output.Write(contents, 0, contents.Length);
            }
        }
    }

    private static string Quote(string value)
    {
        StringBuilder result = new StringBuilder("\"");
        int slash = 0;
        foreach (char character in value)
        {
            if (character == '\\') { slash++; continue; }
            if (character == '"')
            {
                result.Append('\\', slash * 2 + 1);
                result.Append('"');
            }
            else
            {
                result.Append('\\', slash);
                result.Append(character);
            }
            slash = 0;
        }
        result.Append('\\', slash * 2);
        result.Append('"');
        return result.ToString();
    }

    private static Options Parse(string[] args)
    {
        Options options = new Options();
        for (int i = 0; i < args.Length; i++)
        {
            if (args[i] == "--quiet") options.Quiet = true;
            else if ((args[i] == "--codex-home" || args[i] == "--report") && i + 1 < args.Length)
            {
                if (args[i] == "--codex-home") { options.CodexHome = args[++i]; options.ExplicitHome = true; }
                else options.Report = args[++i];
            }
            else throw new ArgumentException("Unknown or incomplete setup option: " + args[i]);
        }
        if (String.IsNullOrWhiteSpace(options.CodexHome))
        {
            string configured = Environment.GetEnvironmentVariable("CODEX_HOME");
            options.CodexHome = String.IsNullOrWhiteSpace(configured)
                ? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), ".codex")
                : configured;
        }
        options.CodexHome = Path.GetFullPath(options.CodexHome);
        if (options.Report != null) options.Report = Path.GetFullPath(options.Report);
        return options;
    }

    private static void ValidateTarget(Options options)
    {
        string home = options.CodexHome;
        if (!Directory.Exists(home)) throw new DirectoryNotFoundException("Codex home was not found: " + home);
        if (!options.ExplicitHome && !File.Exists(Path.Combine(home, "config.toml")) &&
            !File.Exists(Path.Combine(home, "auth.json")) &&
            !Directory.Exists(Path.Combine(home, "sessions")) &&
            !Directory.Exists(Path.Combine(home, "skills")))
            throw new InvalidOperationException("Codex was not found at " + home + ". Install or open Codex first.");
    }

    private static string JsonString(string value)
    {
        StringBuilder result = new StringBuilder("\"");
        foreach (char ch in value)
        {
            if (ch == '"' || ch == '\\') { result.Append('\\'); result.Append(ch); }
            else if (ch == '\n') result.Append("\\n");
            else if (ch == '\r') result.Append("\\r");
            else if (ch < 32) result.Append("\\u" + ((int)ch).ToString("x4"));
            else result.Append(ch);
        }
        return result.Append('"').ToString();
    }

    private static void WriteFailureReport(Options options, string error)
    {
        if (options.Report == null) return;
        Directory.CreateDirectory(Path.GetDirectoryName(options.Report));
        File.WriteAllText(options.Report, "{\"ok\":false,\"error\":" + JsonString(error) + "}\n", Encoding.UTF8);
    }

    private static string RunInstall(Options options)
    {
        ValidateTarget(options);
        string temp = Path.Combine(Path.GetTempPath(), "ADHD-Setup-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(temp);
        try
        {
            ExtractChecked(temp);
            string source = Path.Combine(temp, "source");
            ProcessStartInfo start = new ProcessStartInfo();
            start.FileName = Path.Combine(source, "runtime", "python.exe");
            start.Arguments = "-m adhd.windows_setup --codex-home " + Quote(options.CodexHome) +
                              (options.Report == null ? "" : " --report " + Quote(options.Report));
            start.WorkingDirectory = source;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            start.StandardOutputEncoding = Encoding.UTF8;
            start.StandardErrorEncoding = Encoding.UTF8;
            using (Process process = Process.Start(start))
            {
                Task<string> stdout = process.StandardOutput.ReadToEndAsync();
                Task<string> stderr = process.StandardError.ReadToEndAsync();
                if (!process.WaitForExit(300000))
                {
                    process.Kill();
                    process.WaitForExit(5000);
                    throw new TimeoutException("Setup timed out. Review the installation report before retrying.");
                }
                string output = stdout.Result;
                string error = stderr.Result;
                if (process.ExitCode != 0)
                    throw new InvalidOperationException(String.IsNullOrWhiteSpace(error) ? output.Trim() : error.Trim());
                return output.Trim();
            }
        }
        finally
        {
            try { Directory.Delete(temp, true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
        }
    }

    [STAThread]
    private static int Main(string[] args)
    {
        Options options;
        try { options = Parse(args); }
        catch (Exception error)
        {
            MessageBox.Show(error.Message, "ADHD Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        if (options.Quiet)
        {
            try { RunInstall(options); return 0; }
            catch (Exception error) { WriteFailureReport(options, error.Message); return 1; }
        }
        Application.EnableVisualStyles();
        Form form = new Form();
        form.Text = "ADHD Setup " + Version;
        form.Width = 520;
        form.Height = 215;
        form.StartPosition = FormStartPosition.CenterScreen;
        form.FormBorderStyle = FormBorderStyle.FixedDialog;
        form.MaximizeBox = false;
        Label heading = new Label();
        heading.Left = 24; heading.Top = 22; heading.Width = 460; heading.Height = 48;
        heading.Text = "Install ADHD for Codex?\r\nLocation: " + options.CodexHome;
        form.Controls.Add(heading);
        Label status = new Label();
        status.Left = 24; status.Top = 87; status.Width = 460; status.Height = 40;
        status.Text = "Your existing Codex settings are preserved.";
        form.Controls.Add(status);
        Button install = new Button();
        install.Text = "Install"; install.Left = 385; install.Top = 135; install.Width = 95;
        form.Controls.Add(install);
        bool installing = false;
        bool installed = false;
        form.FormClosing += delegate(object sender, FormClosingEventArgs eventArgs)
        {
            if (installing) eventArgs.Cancel = true;
        };
        install.Click += delegate
        {
            if (installed) { form.Close(); return; }
            if (installing) return;
            installing = true;
            install.Enabled = false;
            status.Text = "Installing. This may take a moment...";
            BackgroundWorker worker = new BackgroundWorker();
            worker.DoWork += delegate(object sender, DoWorkEventArgs eventArgs) { eventArgs.Result = RunInstall(options); };
            worker.RunWorkerCompleted += delegate(object sender, RunWorkerCompletedEventArgs eventArgs)
            {
                installing = false;
                if (eventArgs.Error != null)
                {
                    WriteFailureReport(options, eventArgs.Error.Message);
                    status.Text = "Installation could not finish.";
                    MessageBox.Show(eventArgs.Error.Message, "ADHD Setup", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    install.Enabled = true;
                }
                else
                {
                    installed = true;
                    status.Text = "Installed. Restart Codex, then review and trust the new commands when prompted.";
                    install.Text = "Close";
                    install.Enabled = true;
                }
            };
            worker.RunWorkerAsync();
        };
        Application.Run(form);
        return 0;
    }
}
