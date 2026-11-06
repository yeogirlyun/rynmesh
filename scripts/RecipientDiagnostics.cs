// Build with the Windows .NET Framework compiler. No PowerShell, Python,
// administrator rights, downloaded runtime or installation is required.
using System;
using System.Collections;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.IO.Compression;
using System.Linq;
using System.Net;
using System.Net.NetworkInformation;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

static class RecipientDiagnostics
{
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 4000000 };
    static readonly object Gate = new object();
    static string Folder, Zip, Session;
    static string SelectedInstaller;
    static volatile bool Stop;
    static Task Worker;
    static Action<string> Display = delegate {};
    static readonly HashSet<string> SeenPayloads = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    static readonly ConcurrentDictionary<int,bool> InstallerIds = new ConcurrentDictionary<int,bool>();
    static readonly Dictionary<string,string> Dialogs = new Dictionary<string,string>();
    static readonly HashSet<string> KnownTemp = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    static readonly HashSet<string> SeenHelpers = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
    static DateTime Started;
    static bool UITest;
    static string LastProcesses = "";
    delegate bool EnumWindow(IntPtr window, IntPtr arg);
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumWindow callback, IntPtr arg);
    [DllImport("user32.dll")] static extern bool EnumChildWindows(IntPtr parent, EnumWindow callback, IntPtr arg);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr window, out uint process);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern int GetWindowText(IntPtr window, StringBuilder text, int count);
    [DllImport("user32.dll", CharSet=CharSet.Unicode)] static extern IntPtr SendMessageTimeout(IntPtr window, uint message, IntPtr size, StringBuilder text, uint flags, uint timeout, out IntPtr result);

    [STAThread] static int Main(string[] args)
    {
        Application.EnableVisualStyles();
        Started = DateTime.UtcNow;
        Session = "Ryn-diagnostic-" + DateTime.Now.ToString("yyyyMMdd-HHmmss") + "-" + Guid.NewGuid().ToString("N").Substring(0,6);
        try {
            if (args.Contains("--self-test")) return SelfTest();
            if (args.Contains("--smoke-test")) {
                Initialize(new [] { Path.GetTempPath() });
                Snapshot(); WatchInstallers(); Export();
                File.WriteAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"diagnostic-smoke-result.txt"), Zip);
                return 0;
            }
            if(args.Contains("--ui-test")) {
                UITest=true; Initialize(new [] {Path.GetTempPath()}); ShowForm(); return 0;
            }
            Initialize(new [] { Environment.GetFolderPath(Environment.SpecialFolder.DesktopDirectory),
                Environment.GetFolderPath(Environment.SpecialFolder.MyDocuments),
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), Path.GetTempPath(), AppDomain.CurrentDomain.BaseDirectory });
            ShowForm();
            return 0;
        } catch(Exception e) {
            MessageBox.Show("诊断程序未能启动或保存报告。\n" + e.Message + "\n请把此窗口截图发回，无需修改权限。", "Ryn 诊断", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
    static string Redact(string text) {
        string profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        if (!String.IsNullOrEmpty(profile)) {
            text = text.Replace(profile, "%USERPROFILE%");
            text = text.Replace(profile.Replace("\\", "\\\\"), "%USERPROFILE%");
        }
        return text;
    }
    static void Initialize(IEnumerable<string> roots) {
        var errors = new List<string>();
        foreach(string root in roots.Where(r => !String.IsNullOrWhiteSpace(r))) {
            try {
                string candidate = Path.Combine(root, Session);
                Directory.CreateDirectory(candidate);
                File.WriteAllText(Path.Combine(candidate,"diagnostic.jsonl"), "", Encoding.UTF8);
                Folder = candidate; Zip = candidate + ".zip";
                // Test the actual ZIP destination as well, before reporting success.
                Export();
                Record("startup", new { version=2, windows=Environment.OSVersion.VersionString,
                    process64=Environment.Is64BitProcess, fallback_errors=errors.ToArray() });
                return;
            } catch(Exception e) { errors.Add(Redact(root) + ": " + e.Message); }
        }
        throw new IOException("所有备用保存位置均不可写。\n" + String.Join("\n", errors));
    }
    static void Record(string kind, object value) {
        lock(Gate) {
            var line = Json.Serialize(new { time=DateTimeOffset.Now.ToString("o"), kind=kind, data=value });
            File.AppendAllText(Path.Combine(Folder,"diagnostic.jsonl"), Redact(line)+Environment.NewLine, Encoding.UTF8);
        }
    }
    static void Check(string name, Func<object> action) {
        try { Record(name, action()); }
        catch(Exception e) { Record(name, new { error=e.GetType().Name, detail=e.Message }); }
    }
    static object Pick(IDictionary<string,object> row, params string[] keys) {
        var selected = new Dictionary<string,object>();
        foreach(string key in keys) if(row.ContainsKey(key)) selected[key] = row[key];
        return selected;
    }
    static IDictionary<string,object> ReadJson(string url, bool direct) {
        var request = (HttpWebRequest)WebRequest.Create(url);
        if(direct) request.Proxy=null;
        request.Timeout=3000; request.ReadWriteTimeout=3000; request.AllowAutoRedirect=false;
        using(var response = (HttpWebResponse)request.GetResponse())
        using(var stream = response.GetResponseStream())
        using(var reader = new StreamReader(stream)) {
            char[] buffer=new char[400001]; int count=0, read;
            while(count<buffer.Length && (read=reader.Read(buffer,count,buffer.Length-count))>0) count+=read;
            if(count==buffer.Length) throw new IOException("Response exceeds diagnostic limit");
            lock(Gate) return Json.Deserialize<Dictionary<string,object>>(new string(buffer,0,count));
        }
    }
    static object Local(string route, params string[] keys) {
        return Pick(ReadJson("http://127.0.0.1:8791/api/local/"+route,true),keys);
    }
    static void Snapshot() {
        Check("windows",()=> {
            using(var key=Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion"))
                return new {build=key.GetValue("CurrentBuildNumber"),revision=key.GetValue("UBR"),display_version=key.GetValue("DisplayVersion")};
        });
        Check("network", () => NetworkInterface.GetAllNetworkInterfaces().Select(n => new {
            name=n.Name, state=n.OperationalStatus.ToString(), addresses=n.GetIPProperties().UnicastAddresses
                .Where(a => a.Address.AddressFamily==System.Net.Sockets.AddressFamily.InterNetwork)
                .Select(a=>a.Address.ToString()).ToArray() }).ToArray());
        Check("discovery", () => Local("settings","peer_http_host","peer_http_port","public_endpoint","lan_discovery"));
        // Deliberate allowlist: /space also contains invitation tokens; never save the full response.
        Check("space", () => Local("space","membership","ai_access","self_id","members","last_error","coordinator"));
        Check("services", () => {
            var value=ReadJson("http://127.0.0.1:8791/api/local/llm/services",true);
            var rows=(IEnumerable)value["services"];
            return rows.Cast<IDictionary<string,object>>().Select(row => Pick(row,
                "peer_id","node_name","configured","ready","online","availability","access_policy","binding_verified")).ToArray();
        });
        Check("company", () => Pick(ReadJson("http://127.0.0.1:18140/api/local/runtime/health",true),"ok","service","status"));
        Check("installation",()=> {
            string root=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"Ryn");
            using(var key=Registry.CurrentUser.OpenSubKey(@"Software\Microsoft\Windows\CurrentVersion\Uninstall\Ryn")) {
                if(key!=null && key.GetValue("InstallLocation") is string) root=((string)key.GetValue("InstallLocation")).Trim('"');
            }
            var files=new List<object>();
            foreach(string name in new [] {"Ryn.exe","rynmesh-peer.exe","uninstall.exe"}) {
                string path=Path.Combine(root,name);
                try { files.Add(new {file=name,exists=File.Exists(path),sha256=File.Exists(path)?Hash(path):""}); }
                catch(Exception e) { files.Add(new {file=name,error=e.Message}); }
            }
            return new {directory=root,files=files};
        });
        foreach(string host in new [] {"172.16.8.191","10.10.10.187"}) {
            foreach(bool direct in new [] {true,false}) {
                Check("provider-"+host+(direct?"-direct":"-system-proxy"), () => {
                    // Public discovery only. No remote local-control calls or tokens.
                    var row=ReadJson("http://"+host+":8791/api/peer/lan-record?nonce="+Guid.NewGuid().ToString("N"),direct);
                    return new { responded=row.ContainsKey("payload") };
                });
            }
        }
    }
    static string Hash(string path) {
        using(var file=new FileStream(path,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete))
        using(var sha=SHA256.Create()) return BitConverter.ToString(sha.ComputeHash(file)).Replace("-","").ToLowerInvariant();
    }
    static object VerifyPayload(string root) {
        Dictionary<string,object> manifest;
        lock(Gate) manifest=Json.Deserialize<Dictionary<string,object>>(File.ReadAllText(Path.Combine(root,"full-package.json")));
        var files=(Dictionary<string,object>)manifest["files"];
        var failures=new List<object>(); int count=0;
        foreach(var entry in files) {
            if(Stop) break;
            string path=Path.GetFullPath(Path.Combine(root,entry.Key));
            if(!path.StartsWith(Path.GetFullPath(root).TrimEnd('\\')+"\\",StringComparison.OrdinalIgnoreCase)) throw new IOException("Unsafe payload path");
            try { if(Hash(path)!=(string)entry.Value) failures.Add(new {file=entry.Key,error="hash mismatch"}); }
            catch(Exception e) { failures.Add(new {file=entry.Key,error=e.Message}); }
            count++;
        }
        return new { checked_files=count, expected_files=files.Count, complete=count==files.Count,
            failures=failures, ryn_commit=manifest.ContainsKey("ryn_commit")?manifest["ryn_commit"]:null };
    }
    static void WatchInstallers() {
        var processes=new List<object>(); var helperRoots=new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        foreach(var p in Process.GetProcesses()) using(p) {
            try {
                string name=p.ProcessName;
                bool helper=name.Equals("ryn-full-install",StringComparison.OrdinalIgnoreCase);
                if(!helper && name!="Ryn" && name!="rynmesh-peer" && name!="company-service" &&
                    !name.StartsWith("Ryn_",StringComparison.OrdinalIgnoreCase) && !InstallerIds.ContainsKey(p.Id)) continue;
                string path="";
                try { path=p.MainModule.FileName; } catch(Exception e) { path="unreadable: "+e.Message; }
                processes.Add(new {name=name,pid=p.Id,path=path});
                if(helper && File.Exists(path)) {
                    string root=Path.GetDirectoryName(Path.GetDirectoryName(path));
                    helperRoots.Add(root); SeenHelpers.Add(root); KnownTemp.Add(root);
                }
                if(name.StartsWith("Ryn_",StringComparison.OrdinalIgnoreCase)) InstallerIds.TryAdd(p.Id,true);
            } catch { /* Processes can exit during enumeration. */ }
        }
        string current;
        lock(Gate) current=Json.Serialize(processes);
        if(current!=LastProcesses) { Record("process-change",processes); LastProcesses=current; }
        EnumWindows(delegate(IntPtr window,IntPtr arg) {
            uint pid; GetWindowThreadProcessId(window,out pid);
            if(!InstallerIds.ContainsKey((int)pid)) return true;
            var title=new StringBuilder(1024); GetWindowText(window,title,title.Capacity);
            var texts=new List<string>(); texts.Add(title.ToString());
            EnumChildWindows(window,delegate(IntPtr child,IntPtr ignored) {
                var text=new StringBuilder(4096); IntPtr result;
                SendMessageTimeout(child,13,(IntPtr)text.Capacity,text,2,100,out result);
                if(text.Length>0) texts.Add(text.ToString()); return true;
            },IntPtr.Zero);
            string combined=String.Join("\n",texts.Distinct()); string key=pid+":"+window;
            if(!Dialogs.ContainsKey(key) || Dialogs[key]!=combined) {
                Dialogs[key]=combined; Record("installer-window",new {pid=pid,text=combined});
            }
            return true;
        },IntPtr.Zero);
        // Remember extracted locations while installation is running; users do
        // not have to keep the error dialog open at just the right instant.
        foreach(string root in Directory.GetDirectories(Path.GetTempPath(),"ns*.tmp")) {
            if(File.Exists(Path.Combine(root,"payload","full-package.json"))) KnownTemp.Add(root);
        }
        foreach(string root in KnownTemp.ToArray()) {
            string manifest=Path.Combine(root,"payload","full-package.json");
            if(!File.Exists(manifest)) continue;
            string copy=Path.Combine(Folder,"payload-manifest-"+Path.GetFileName(root)+".json");
            if(!File.Exists(copy)) Check("manifest-saved",()=>{File.Copy(manifest,copy,false); return new {file=Path.GetFileName(copy)};});
            // Do not hash partially extracted payloads: wait for the provisioner
            // or a visible failure after extraction. Capture once per temp root.
            bool failed=Dialogs.Values.Any(t=>t.IndexOf("Installation stopped",StringComparison.OrdinalIgnoreCase)>=0 ||
                t.IndexOf("Installation preparation failed",StringComparison.OrdinalIgnoreCase)>=0);
            if(!SeenPayloads.Contains(root) && (SeenHelpers.Contains(root)||failed)) {
                SeenPayloads.Add(root);
                Check("payload-verification",()=>VerifyPayload(Path.Combine(root,"payload")));
            }
        }
    }
    static void Export() {
        lock(Gate) {
            string stage=Zip+".new";
            if(File.Exists(stage)) File.Delete(stage); // Exact file owned by this unique diagnostic session.
            ZipFile.CreateFromDirectory(Folder,stage,CompressionLevel.Optimal,false);
            if(File.Exists(Zip)) File.Replace(stage,Zip,null); else File.Move(stage,Zip);
        }
    }
    static void ShowForm() {
        var form=new Form { Text="Ryn 安装与服务诊断",Width=740,Height=400,StartPosition=FormStartPosition.CenterScreen };
        var instructions=new Label { Left=20,Top=18,Width=680,Height=82,Text=
            "诊断正在持续记录，无需管理员权限。\n冷安：点击“选择安装包并开始”，正常安装即可。\n阿九：保持 Ryn 开着，等待首次检查完成即可。\n完成后把下方 ZIP 报告发回；程序不会改配置、卸载或发送消息。" };
        var status=new Label {Left=20,Top=108,Width=680,Height=45,Text="正在首次检查，界面可以正常操作……"};
        var path=new TextBox {Left=20,Top=160,Width=680,Height=55,Multiline=true,ReadOnly=true,Text=Zip};
        var launch=new Button {Left=20,Top=230,Width=200,Height=42,Text="选择安装包并开始"};
        var open=new Button {Left=235,Top=230,Width=200,Height=42,Text="打开报告文件夹"};
        var finish=new Button {Left=450,Top=230,Width=250,Height=42,Text="完成记录并关闭"};
        var note=new Label {Left=20,Top=288,Width=680,Height=50,Text="报告自动保存，关闭窗口也会保存。包含设备名、内网地址和诊断错误；不采集密码、邀请令牌、聊天或数据库。"};
        form.Controls.AddRange(new Control[]{instructions,status,path,launch,open,finish,note});
        Display=delegate(string text) { if(!form.IsDisposed && form.IsHandleCreated) try { form.BeginInvoke((Action)(()=>status.Text=text)); } catch(InvalidOperationException) {} };
        launch.Click+=delegate {
            using(var picker=new OpenFileDialog {Title="选择原来的 Ryn 完整安装包",Filter="Ryn 安装包 (*.exe)|*.exe"}) {
                if(picker.ShowDialog()!=DialogResult.OK) return;
                try {
                    SelectedInstaller=picker.FileName;
                    Record("installer-selected",new {file=Path.GetFileName(SelectedInstaller),bytes=new FileInfo(SelectedInstaller).Length});
                    var start=new ProcessStartInfo(SelectedInstaller) {UseShellExecute=true,WorkingDirectory=Path.GetDirectoryName(SelectedInstaller)};
                    using(var process=Process.Start(start)) if(process!=null) InstallerIds.TryAdd(process.Id,true);
                    launch.Enabled=false;
                    Display("安装已启动，正在持续记录；出现报错也无需重新打开诊断程序。");
                } catch(Exception e) { Check("installer-start-error",()=>{throw e;}); MessageBox.Show(form,e.Message,"安装包无法启动"); }
            }
        };
        open.Click+=delegate { try { Process.Start(new ProcessStartInfo("explorer.exe","/select,\""+Zip+"\"") {UseShellExecute=true}); } catch(Exception e) { MessageBox.Show(form,"报告位置：\n"+Zip+"\n"+e.Message); } };
        finish.Click+=delegate { form.Close(); };
        bool closing=false;
        form.FormClosing+=async delegate(object sender,FormClosingEventArgs e) {
            if(closing) return;
            e.Cancel=true; Stop=true; finish.Enabled=false; launch.Enabled=false;
            status.Text="正在保存最后的报告，请稍候……";
            if(Worker!=null) await Worker;
            try { Export(); closing=true; form.Close(); }
            catch(Exception error) { status.Text="ZIP 保存失败；原始诊断仍在："+Folder; closing=true;
                MessageBox.Show(form,"请发送该文件夹内的 diagnostic.jsonl：\n"+Folder+"\n"+error.Message,"备用报告已保留"); form.Close(); }
        };
        form.Shown+=delegate {
            var monitoring=Task.Run(()=>{
                while(!Stop) {
                    try { WatchInstallers(); }
                    catch(Exception error) {
                        try { Record("monitor-error",new {detail=error.Message}); } catch {}
                        Display("安装监测部分失败："+error.Message+"；已有记录保留。");
                    }
                    for(int i=0;i<10 && !Stop;i++) Thread.Sleep(100);
                }
            });
            var snapshots=Task.Run(()=>{
                DateTime next=DateTime.MinValue;
                while(!Stop) {
                    try {
                        if(DateTime.UtcNow>=next) {
                            Snapshot(); Export(); next=DateTime.UtcNow.AddSeconds(30);
                            Display("报告已保存（"+DateTime.Now.ToString("HH:mm:ss")+"），仍在持续记录。可以发送 ZIP 报告。");
                        }
                    } catch(Exception error) {
                        try { Record("diagnostic-error",new {detail=error.Message}); } catch {}
                        Display("部分检查失败："+error.Message+"；其余记录继续，已有报告保留。");
                    }
                    for(int i=0;i<10 && !Stop;i++) Thread.Sleep(100);
                }
            });
            Worker=Task.WhenAll(monitoring,snapshots);
            if(UITest) {
                var timer=new System.Windows.Forms.Timer {Interval=3000};
                timer.Tick+=delegate {
                    timer.Stop();
                    using(var bitmap=new System.Drawing.Bitmap(form.Width,form.Height)) {
                        form.DrawToBitmap(bitmap,new System.Drawing.Rectangle(0,0,form.Width,form.Height));
                        bitmap.Save(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"diagnostic-ui.png"));
                    }
                    form.Close(); timer.Dispose();
                };
                timer.Start();
            }
        };
        Application.Run(form);
    }
    static int SelfTest() {
        string root=Path.Combine(Path.GetTempPath(),"ryn-diagnostic-tests-"+Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(root);
        // Force the first save location to fail, and require the fallback.
        string blocked=Path.Combine(root,"not-a-directory"); File.WriteAllText(blocked,"test");
        Initialize(new [] {blocked,root});
        if(!Folder.StartsWith(root+"\\")) throw new Exception("fallback failed");
        var sample=new Dictionary<string,object>{{"membership","active"},{"ai_access","local"},{"invitation","SECRET"}};
        string selected=Json.Serialize(Pick(sample,"membership","ai_access"));
        if(selected.Contains("SECRET")) throw new Exception("secret leaked");
        Check("expected-failure",()=>{throw new UnauthorizedAccessException("test denial");});
        Record("after-failure",new {ok=true});
        string payload=Path.Combine(root,"payload"); Directory.CreateDirectory(payload);
        string asset=Path.Combine(payload,"asset"); File.WriteAllText(asset,"original");
        File.WriteAllText(Path.Combine(payload,"full-package.json"),Json.Serialize(new {files=new Dictionary<string,string>{{"asset",Hash(asset)}}}));
        if(!Json.Serialize(VerifyPayload(payload)).Contains("\"failures\":[]")) throw new Exception("valid payload rejected");
        File.WriteAllText(asset,"changed");
        if(!Json.Serialize(VerifyPayload(payload)).Contains("hash mismatch")) throw new Exception("corruption not detected");
        Export(); Record("repeat-export",new {ok=true}); Export();
        using(var zip=ZipFile.OpenRead(Zip)) {
            using(var reader=new StreamReader(zip.GetEntry("diagnostic.jsonl").Open())) {
                string text=reader.ReadToEnd();
                if(!text.Contains("after-failure")||!text.Contains("repeat-export")) throw new Exception("durability failed");
            }
        }
        File.WriteAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory,"diagnostic-self-test.txt"),"PASS: fallback, secret allowlist, per-check failure, payload integrity, durable repeated ZIP export\n"+Zip);
        return 0;
    }
}
