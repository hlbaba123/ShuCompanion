// Local, offline speech bridge. --probe never constructs an audio input.
// Build with the Windows .NET Framework 4 compiler; see build_voice_helper.ps1.
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Speech.Recognition;
using System.Text;
using System.Threading;

internal static class VoiceHelper
{
    private static readonly object OutputLock = new object();

    private static string Quote(string text)
    {
        StringBuilder result = new StringBuilder("\"");
        foreach (char c in text ?? "")
        {
            switch (c)
            {
                case '"': result.Append("\\\""); break;
                case '\\': result.Append("\\\\"); break;
                case '\n': result.Append("\\n"); break;
                case '\r': result.Append("\\r"); break;
                case '\t': result.Append("\\t"); break;
                default:
                    if (c < 32) result.Append("\\u" + ((int)c).ToString("x4"));
                    else result.Append(c);
                    break;
            }
        }
        return result.Append('"').ToString();
    }

    private static void Emit(string json)
    {
        lock (OutputLock) { Console.WriteLine(json); Console.Out.Flush(); }
    }

    private static RecognizerInfo SelectChinese()
    {
        RecognizerInfo fallback = null;
        foreach (RecognizerInfo info in SpeechRecognitionEngine.InstalledRecognizers())
        {
            if (info.Culture.Name.Equals("zh-CN", StringComparison.OrdinalIgnoreCase)) return info;
            if (info.Culture.TwoLetterISOLanguageName == "zh" && fallback == null) fallback = info;
        }
        return fallback;
    }

    private static int Probe()
    {
        List<string> languages = new List<string>();
        foreach (RecognizerInfo info in SpeechRecognitionEngine.InstalledRecognizers())
            if (!languages.Contains(info.Culture.Name)) languages.Add(info.Culture.Name);
        string values = String.Join(",", languages.ConvertAll<string>(Quote).ToArray());
        Emit("{\"type\":\"capabilities\",\"available\":" +
             (SelectChinese() != null ? "true" : "false") +
             ",\"offline\":true,\"microphone_opened\":false,\"languages\":[" + values + "]}");
        return 0;
    }

    private static int Recognize(string wavePath)
    {
        RecognizerInfo info = SelectChinese();
        if (info == null) { Emit("{\"type\":\"error\",\"code\":\"missing_recognizer\"}"); return 1; }
        bool openingMicrophone = false;
        using (ManualResetEvent stopped = new ManualResetEvent(false))
        using (ManualResetEvent completed = new ManualResetEvent(false))
        {
            try
            {
                using (SpeechRecognitionEngine engine = new SpeechRecognitionEngine(info))
                {
                    object transcriptLock = new object();
                    List<string> parts = new List<string>();
                    long lastLevel = 0;
                    Exception recognitionError = null;
                    engine.LoadGrammar(new DictationGrammar());
                    engine.InitialSilenceTimeout = TimeSpan.FromSeconds(60);
                    engine.BabbleTimeout = TimeSpan.FromSeconds(60);
                    engine.EndSilenceTimeout = TimeSpan.FromMilliseconds(650);
                    engine.EndSilenceTimeoutAmbiguous = TimeSpan.FromMilliseconds(900);
                    engine.SpeechRecognized += delegate(object sender, SpeechRecognizedEventArgs e)
                    {
                        if (e.Result != null && !String.IsNullOrWhiteSpace(e.Result.Text))
                            lock (transcriptLock) parts.Add(e.Result.Text);
                    };
                    engine.AudioLevelUpdated += delegate(object sender, AudioLevelUpdatedEventArgs e)
                    {
                        long now = Environment.TickCount & Int32.MaxValue;
                        if (now - lastLevel < 50 && now >= lastLevel) return;
                        lastLevel = now;
                        Emit("{\"type\":\"level\",\"value\":" +
                             (Math.Max(0, Math.Min(100, e.AudioLevel)) / 100.0).ToString("0.00", CultureInfo.InvariantCulture) + "}");
                    };
                    engine.RecognizeCompleted += delegate(object sender, RecognizeCompletedEventArgs e)
                    { recognitionError = e.Error; completed.Set(); };
                    if (wavePath == null)
                    {
                        openingMicrophone = true;
                        engine.SetInputToDefaultAudioDevice();
                        openingMicrophone = false;
                        Thread commands = new Thread(delegate()
                        {
                            try
                            {
                                string command;
                                while ((command = Console.ReadLine()) != null)
                                    if (command.Trim() == "stop") { stopped.Set(); return; }
                                stopped.Set(); // A closed parent pipe must not leave recording running.
                            }
                            catch (Exception) { try { stopped.Set(); } catch (ObjectDisposedException) { } }
                        });
                        commands.IsBackground = true;
                        commands.Start();
                    }
                    else engine.SetInputToWaveFile(wavePath);

                    engine.RecognizeAsync(RecognizeMode.Multiple);
                    Emit("{\"type\":\"ready\",\"offline\":true,\"microphone_opened\":" + (wavePath == null ? "true" : "false") + "}");
                    if (wavePath == null)
                    {
                        WaitHandle.WaitAny(new WaitHandle[] { stopped, completed }, 60000);
                        if (!completed.WaitOne(0)) engine.RecognizeAsyncStop();
                    }
                    if (!completed.WaitOne(wavePath == null ? 4000 : 30000))
                    { engine.RecognizeAsyncCancel(); throw new TimeoutException(); }
                    if (recognitionError != null) throw recognitionError;
                    string text;
                    lock (transcriptLock) text = String.Join("", parts.ToArray());
                    engine.SetInputToNull();
                    Emit("{\"type\":\"result\",\"text\":" + Quote(text) + "}");
                    return 0;
                }
            }
            catch (Exception error)
            {
                string code = "recognition_failed";
                if (error is UnauthorizedAccessException || error.HResult == unchecked((int)0x80070005)) code = "microphone_denied";
                else if (openingMicrophone) code = "microphone_unavailable";
                else if (error is InvalidOperationException || error is ArgumentException) code = "recognizer_unavailable";
                Emit("{\"type\":\"error\",\"code\":" + Quote(code) + "}");
                return 1;
            }
        }
    }

    [MTAThread]
    public static int Main(string[] args)
    {
        Console.InputEncoding = new UTF8Encoding(false);
        Console.OutputEncoding = new UTF8Encoding(false);
        try
        {
            if (args.Length == 1 && args[0] == "--probe") return Probe();
            if (args.Length == 1 && args[0] == "--listen") return Recognize(null);
            if (args.Length == 2 && args[0] == "--wave") return Recognize(args[1]);
            Emit("{\"type\":\"error\",\"code\":\"invalid_arguments\"}");
            return 2;
        }
        catch (Exception)
        { Emit("{\"type\":\"error\",\"code\":\"recognizer_unavailable\"}"); return 1; }
    }
}
