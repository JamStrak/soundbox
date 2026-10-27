// Windows standalone package entry point. The product and its Hub launcher remain separate.
using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;

internal static class StandaloneLauncher
{
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    private static extern int MessageBoxW(IntPtr owner, string text, string caption, uint type);

    [STAThread]
    private static int Main()
    {
        string root = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
        string python = Path.Combine(root, "runtime", "python", "pythonw.exe");
        string launcher = Path.Combine(root, "tools", "soundbox", "launcher.py");
        if (!File.Exists(python) || !File.Exists(launcher))
        {
            MessageBoxW(IntPtr.Zero, "声匣文件不完整。请先完整解压整个压缩包，并保留所有文件夹。", "声匣启动失败", 0x10);
            return 1;
        }
        try
        {
            var info = new ProcessStartInfo(python, "-I -B \"" + launcher + "\"");
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.WorkingDirectory = Path.GetDirectoryName(launcher);
            Process.Start(info);
            return 0;
        }
        catch (Exception error)
        {
            MessageBoxW(IntPtr.Zero, "无法启动声匣，请检查目录是否完整且可写。\n" + error.Message, "声匣启动失败", 0x10);
            return 1;
        }
    }
}
