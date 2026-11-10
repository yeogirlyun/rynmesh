//! Windows: a Start menu shortcut per app, its own taskbar identity, and per-user "Open with"
//! registration that Settings > Default apps lists under the app's name. Nothing here needs
//! administrator rights, and nothing sets a default: Windows reserves that choice for the user.

use std::path::{Path, PathBuf};
use windows::core::{Interface, HSTRING, PCWSTR, PWSTR, w};
use windows::Win32::Foundation::HWND;
use windows::Win32::Storage::EnhancedStorage::{PKEY_AppUserModel_ID, PKEY_AppUserModel_RelaunchCommand,
    PKEY_AppUserModel_RelaunchDisplayNameResource, PKEY_AppUserModel_RelaunchIconResource};
use windows::Win32::System::Com::StructuredStorage::{PropVariantClear, PROPVARIANT};
use windows::Win32::System::Com::{CoCreateInstance, CoInitializeEx, CoTaskMemAlloc, CoTaskMemFree, CoUninitialize,
    IPersistFile, CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED};
use windows::Win32::System::Variant::VT_LPWSTR;
use windows::Win32::UI::Shell::PropertiesSystem::{IPropertyStore, SHGetPropertyStoreForWindow};
use windows::Win32::UI::Shell::{IShellLinkW, SHChangeNotify, SHGetKnownFolderPath, ShellExecuteW, ShellLink,
    FOLDERID_Programs, KF_FLAG_DEFAULT, SHCNE_ASSOCCHANGED, SHCNF_IDLIST};
use windows::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;
use winreg::enums::{HKEY_CURRENT_USER, REG_NONE};
use winreg::{RegKey, RegValue};
use super::{ico, SystemApp, APPS};

/// Creates the entries of `wanted` apps and removes those of every other app. Reports the
/// outcome of every wanted app; one failing app does not stop the others.
pub fn apply(app: &tauri::AppHandle, wanted: &[&'static SystemApp]) -> Vec<(&'static str, Result<(), String>)> {
    let _com = Com::init();
    let places = std::env::current_exe().map_err(|error| format!("system_apps_failed: {error}"))
        .and_then(|exe| Ok((exe, programs_folder().map_err(|error| format!("start_menu_unavailable: {error}"))?)));
    let (exe, programs) = match places {
        Ok(places) => places,
        Err(error) => return wanted.iter().map(|system_app| (system_app.id, Err(error.clone()))).collect(),
    };
    let mut outcomes = Vec::new();
    for system_app in APPS {
        let shortcut = programs.join(format!("{}.lnk", system_app.name));
        if wanted.iter().any(|wanted| wanted.id == system_app.id) {
            outcomes.push((system_app.id, install(app, &exe, &shortcut, system_app)));
        } else {
            match std::fs::remove_file(&shortcut) {
                Err(error) if error.kind() != std::io::ErrorKind::NotFound => log::warn!("could not remove {}: {error}", shortcut.display()),
                _ => {}
            }
            unregister(system_app);
        }
    }
    unsafe { SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None) };
    outcomes
}

fn install(app: &tauri::AppHandle, exe: &Path, shortcut: &Path, system_app: &SystemApp) -> Result<(), String> {
    let icon = icon_file(app, system_app)?;
    write_shortcut(shortcut, exe, &icon, system_app).map_err(|error| format!("start_menu_failed: {error}"))?;
    register(exe, &icon, system_app).map_err(|error| format!("file_types_failed: {error}"))
}

/// Whether the app's Start menu shortcut and "Open with" registration exist right now.
pub fn present(system_app: &SystemApp) -> bool {
    let Ok(programs) = programs_folder() else { return false };
    if !programs.join(format!("{}.lnk", system_app.name)).is_file() {
        return false;
    }
    let user = RegKey::predef(HKEY_CURRENT_USER);
    let registered = |key: String| user.open_subkey(key).is_ok();
    system_app.documents.iter().all(|doc| registered(format!(r"Software\Classes\{}\shell\open\command", prog_id(system_app, doc.ext))))
        && (system_app.documents.is_empty() || user.open_subkey(r"Software\RegisteredApplications")
            .and_then(|key| key.get_value::<String, _>(system_app.name)).is_ok())
}

/// Opens Settings > Default apps on the app's own page, where the user picks its types.
pub fn choose_default(system_app: &SystemApp) -> Result<(), String> {
    let page = HSTRING::from(format!("ms-settings:defaultapps?registeredAppUser={}", system_app.name.replace(' ', "%20")));
    let result = unsafe { ShellExecuteW(None, w!("open"), &page, PCWSTR::null(), PCWSTR::null(), SW_SHOWNORMAL) };
    // ShellExecute reports success with a value above 32.
    if result.0 as isize > 32 { Ok(()) } else { Err("default_apps_unavailable".into()) }
}

/// The app's .ico on disk, for shortcuts, file types and taskbar buttons; rewritten when Ryn's
/// copy changes.
pub fn icon_file(app: &tauri::AppHandle, system_app: &SystemApp) -> Result<PathBuf, String> {
    let bytes = ico(system_app.id).ok_or("system_app_unknown")?;
    let folder = crate::deployment::native_profile_dir(app).map_err(|_| "system_apps_failed")?.join("app-icons");
    let path = folder.join(format!("{}.ico", system_app.id));
    if std::fs::read(&path).ok().as_deref() != Some(bytes) {
        std::fs::create_dir_all(&folder).and_then(|_| std::fs::write(&path, bytes))
            .map_err(|error| format!("system_apps_failed: {error}"))?;
    }
    Ok(path)
}

/// Taskbar identity of an app window: grouped with the app's shortcut, pinned as the app.
pub fn identify_window(hwnd: HWND, exe: &Path, icon: &Path, system_app: &SystemApp) -> windows::core::Result<()> {
    unsafe {
        let store: IPropertyStore = SHGetPropertyStoreForWindow(hwnd)?;
        // The relaunch values must be in place before the ID that makes the button pinnable.
        store.SetValue(&PKEY_AppUserModel_RelaunchCommand, &PropString::new(&relaunch_command(exe, system_app))?.0)?;
        store.SetValue(&PKEY_AppUserModel_RelaunchDisplayNameResource, &PropString::new(system_app.name)?.0)?;
        store.SetValue(&PKEY_AppUserModel_RelaunchIconResource, &PropString::new(&format!("{},0", icon.display()))?.0)?;
        store.SetValue(&PKEY_AppUserModel_ID, &PropString::new(&system_app.app_user_model_id())?.0)?;
    }
    Ok(())
}

/// Window properties own their copies, so release them before the native window is destroyed.
pub fn clear_window_identity(hwnd: HWND) {
    unsafe {
        let store: IPropertyStore = match SHGetPropertyStoreForWindow(hwnd) {
            Ok(store) => store,
            Err(error) => {
                log::warn!("could not clear the app window's identity: {error}");
                return;
            }
        };
        for key in [&PKEY_AppUserModel_ID, &PKEY_AppUserModel_RelaunchCommand,
            &PKEY_AppUserModel_RelaunchDisplayNameResource, &PKEY_AppUserModel_RelaunchIconResource] {
            if let Err(error) = store.SetValue(key, &PROPVARIANT::default()) {
                log::warn!("could not clear an app window property: {error}");
            }
        }
    }
}

fn relaunch_command(exe: &Path, system_app: &SystemApp) -> String {
    format!("\"{}\" --app {}", exe.display(), system_app.id)
}

fn write_shortcut(path: &Path, exe: &Path, icon: &Path, system_app: &SystemApp) -> windows::core::Result<()> {
    unsafe {
        let link: IShellLinkW = CoCreateInstance(&ShellLink, None, CLSCTX_INPROC_SERVER)?;
        link.SetPath(&HSTRING::from(exe))?;
        link.SetArguments(&HSTRING::from(format!("--app {}", system_app.id)))?;
        link.SetIconLocation(&HSTRING::from(icon), 0)?;
        link.SetDescription(&HSTRING::from(system_app.name))?;
        if let Some(folder) = exe.parent() { link.SetWorkingDirectory(&HSTRING::from(folder))?; }
        let store: IPropertyStore = link.cast()?;
        store.SetValue(&PKEY_AppUserModel_ID, &PropString::new(&system_app.app_user_model_id())?.0)?;
        store.Commit()?;
        link.cast::<IPersistFile>()?.Save(&HSTRING::from(path), true)
    }
}

fn programs_folder() -> windows::core::Result<PathBuf> {
    unsafe {
        let path = SHGetKnownFolderPath(&FOLDERID_Programs, KF_FLAG_DEFAULT, None)?;
        let folder = path.to_string().map(PathBuf::from);
        CoTaskMemFree(Some(path.0 as *const _));
        folder.map_err(|_| windows::core::Error::from_win32())
    }
}

/// One registry value the app owns under HKEY_CURRENT_USER. An empty `data` with `marker`
/// is the REG_NONE entry that lists a ProgID under a type's "Open with".
#[derive(Debug, PartialEq)]
struct Entry { key: String, name: String, data: String, marker: bool }

fn prog_id(system_app: &SystemApp, ext: &str) -> String {
    format!("Ryn.{}.{ext}", system_app.id)
}

fn capabilities_key(system_app: &SystemApp) -> String {
    format!(r"Software\Ryn\Apps\{}\Capabilities", system_app.id)
}

/// Everything that registers the app's document types with the shell; none for an app without.
fn registration(exe: &Path, icon: &Path, system_app: &SystemApp) -> Vec<Entry> {
    if system_app.documents.is_empty() {
        return Vec::new();
    }
    let value = |key: String, name: &str, data: String| Entry { key, name: name.into(), data, marker: false };
    let icon_ref = format!("{},0", icon.display());
    let aumid = system_app.app_user_model_id();
    let capabilities = capabilities_key(system_app);
    let mut entries = vec![
        value(capabilities.clone(), "ApplicationName", system_app.name.into()),
        value(capabilities.clone(), "ApplicationDescription", format!("{} in Ryn", system_app.name)),
        value(capabilities.clone(), "ApplicationIcon", icon_ref.clone()),
        value(r"Software\RegisteredApplications".into(), system_app.name, capabilities.clone()),
    ];
    for doc in system_app.documents {
        let id = prog_id(system_app, doc.ext);
        let class = format!(r"Software\Classes\{id}");
        let kind = format!("{} ({})", doc.kind, system_app.name);
        entries.extend([
            value(class.clone(), "", kind.clone()),
            value(class.clone(), "FriendlyTypeName", kind),
            value(class.clone(), "AppUserModelID", aumid.clone()),
            value(format!(r"{class}\DefaultIcon"), "", icon_ref.clone()),
            value(format!(r"{class}\Application"), "ApplicationName", system_app.name.into()),
            value(format!(r"{class}\Application"), "ApplicationIcon", icon_ref.clone()),
            value(format!(r"{class}\Application"), "ApplicationCompany", "Ryn".into()),
            value(format!(r"{class}\Application"), "AppUserModelID", aumid.clone()),
            value(format!(r"{class}\shell\open"), "FriendlyAppName", system_app.name.into()),
            value(format!(r"{class}\shell\open\command"), "", format!("\"{}\" --app {} --open \"%1\"", exe.display(), system_app.id)),
            value(format!(r"{capabilities}\FileAssociations"), &format!(".{}", doc.ext), id.clone()),
            Entry { key: format!(r"Software\Classes\.{}\OpenWithProgids", doc.ext), name: id, data: String::new(), marker: true },
        ]);
    }
    entries
}

fn register(exe: &Path, icon: &Path, system_app: &SystemApp) -> std::io::Result<()> {
    let user = RegKey::predef(HKEY_CURRENT_USER);
    for entry in registration(exe, icon, system_app) {
        let (key, _) = user.create_subkey(&entry.key)?;
        if entry.marker {
            key.set_raw_value(&entry.name, &RegValue { vtype: REG_NONE, bytes: Vec::new() })?;
        } else {
            key.set_value(&entry.name, &entry.data)?;
        }
    }
    Ok(())
}

/// Removes what `register` wrote, leaving the types' own keys and other apps' entries alone.
fn unregister(system_app: &SystemApp) {
    let user = RegKey::predef(HKEY_CURRENT_USER);
    let quiet = |result: std::io::Result<()>| match result {
        Err(error) if error.kind() != std::io::ErrorKind::NotFound => log::warn!("could not unregister {}: {error}", system_app.id),
        _ => {}
    };
    for doc in system_app.documents {
        let id = prog_id(system_app, doc.ext);
        quiet(user.delete_subkey_all(format!(r"Software\Classes\{id}")));
        quiet(user.open_subkey_with_flags(format!(r"Software\Classes\.{}\OpenWithProgids", doc.ext), winreg::enums::KEY_SET_VALUE)
            .and_then(|key| key.delete_value(&id)));
    }
    quiet(user.delete_subkey_all(format!(r"Software\Ryn\Apps\{}", system_app.id)));
    quiet(user.open_subkey_with_flags(r"Software\RegisteredApplications", winreg::enums::KEY_SET_VALUE)
        .and_then(|key| key.delete_value(system_app.name)));
}

/// A VT_LPWSTR property value, the type the AppUserModel properties require.
struct PropString(PROPVARIANT);

impl PropString {
    fn new(value: &str) -> windows::core::Result<Self> {
        let wide: Vec<u16> = value.encode_utf16().chain(Some(0)).collect();
        unsafe {
            let text = CoTaskMemAlloc(wide.len() * 2) as *mut u16;
            if text.is_null() { return Err(windows::Win32::Foundation::E_OUTOFMEMORY.into()); }
            text.copy_from_nonoverlapping(wide.as_ptr(), wide.len());
            let mut variant = PROPVARIANT::default();
            (*variant.Anonymous.Anonymous).vt = VT_LPWSTR;
            (*variant.Anonymous.Anonymous).Anonymous.pwszVal = PWSTR(text);
            Ok(Self(variant))
        }
    }
}

impl Drop for PropString {
    fn drop(&mut self) {
        unsafe { let _ = PropVariantClear(&mut self.0); }
    }
}

/// COM for this thread; a thread that already joined another apartment keeps it.
struct Com(bool);

impl Com {
    fn init() -> Self {
        Com(unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED) }.is_ok())
    }
}

impl Drop for Com {
    fn drop(&mut self) {
        if self.0 { unsafe { CoUninitialize() } }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::system_apps::find;

    fn excel() -> Vec<Entry> {
        registration(Path::new(r"C:\Users\a\AppData\Local\Ryn\Ryn.exe"), Path::new(r"C:\Icons\gridcraft.ico"), find("gridcraft").unwrap())
    }

    #[test]
    fn excel_is_offered_for_its_types_and_opens_the_clicked_file() {
        let entries = excel();
        let get = |key: &str, name: &str| entries.iter().find(|e| e.key == key && e.name == name).map(|e| e.data.as_str());
        assert_eq!(get(r"Software\Classes\Ryn.gridcraft.xlsx\shell\open\command", ""),
            Some(r#""C:\Users\a\AppData\Local\Ryn\Ryn.exe" --app gridcraft --open "%1""#));
        assert_eq!(get(r"Software\Classes\Ryn.gridcraft.xlsx\Application", "ApplicationName"), Some("Ryn Excel"));
        assert_eq!(get(r"Software\Classes\Ryn.gridcraft.xlsx\DefaultIcon", ""), Some(r"C:\Icons\gridcraft.ico,0"));
        assert_eq!(get(r"Software\RegisteredApplications", "Ryn Excel"), Some(r"Software\Ryn\Apps\gridcraft\Capabilities"));
        assert_eq!(get(r"Software\Ryn\Apps\gridcraft\Capabilities\FileAssociations", ".csv"), Some("Ryn.gridcraft.csv"));
        let listed = entries.iter().find(|e| e.key == r"Software\Classes\.xlsx\OpenWithProgids").unwrap();
        assert!(listed.marker && listed.name == "Ryn.gridcraft.xlsx" && listed.data.is_empty());
    }

    #[test]
    fn registration_never_takes_a_default() {
        // A type's own default value or a UserChoice is the user's; Ryn only adds itself as an option.
        for entry in excel() {
            assert!(entry.key.starts_with(r"Software\Classes\Ryn.gridcraft.") || entry.key.starts_with(r"Software\Ryn\Apps\gridcraft\")
                || entry.key == r"Software\RegisteredApplications"
                || (entry.key.ends_with(r"\OpenWithProgids") && entry.marker), "{entry:?}");
        }
        assert!(registration(Path::new("Ryn.exe"), Path::new("a.ico"), find("cadcraft").unwrap()).is_empty());
    }

    #[test]
    fn the_uninstaller_removes_every_entry_ryn_creates() {
        let hooks = include_str!("../../scripts/windows-installer-hooks.nsh");
        for app in APPS {
            assert!(hooks.contains(&format!(r#"Delete "$SMPROGRAMS\{}.lnk""#, app.name)), "shortcut of {}", app.id);
            if app.documents.is_empty() { continue; }
            assert!(hooks.contains(&format!(r#"DeleteRegValue HKCU "Software\RegisteredApplications" "{}""#, app.name)), "{}", app.id);
            for doc in app.documents {
                assert!(hooks.contains(&format!(r#"!insertmacro RYN_REMOVE_FILE_TYPE "{}" "{}""#, app.id, doc.ext)), "{}", prog_id(app, doc.ext));
            }
        }
        // The macro removes exactly the ProgID and "Open with" marker that registration writes.
        assert_eq!(prog_id(find("gridcraft").unwrap(), "xlsx"), "Ryn.gridcraft.xlsx");
        assert!(hooks.contains(r#"DeleteRegKey HKCU "Software\Classes\Ryn.${APP}.${EXT}""#));
        assert!(hooks.contains(r#"DeleteRegValue HKCU "Software\Classes\.${EXT}\OpenWithProgids" "Ryn.${APP}.${EXT}""#));
        assert!(hooks.contains(r#"DeleteRegKey HKCU "Software\Ryn\Apps""#));
    }

    /// Lists what Explorer's "Open with" shows for `ext`.
    fn open_with(ext: &str) -> Vec<String> {
        use windows::Win32::UI::Shell::{IAssocHandler, SHAssocEnumHandlers, ASSOC_FILTER_RECOMMENDED};
        let mut names = Vec::new();
        unsafe {
            let handlers = SHAssocEnumHandlers(&HSTRING::from(ext), ASSOC_FILTER_RECOMMENDED).unwrap();
            loop {
                let mut next: [Option<IAssocHandler>; 1] = [None];
                let mut fetched = 0;
                if handlers.Next(&mut next, Some(&mut fetched)).is_err() || fetched == 0 { break; }
                let name = next[0].as_ref().unwrap().GetUIName().unwrap();
                names.push(name.to_string().unwrap());
                CoTaskMemFree(Some(name.0 as *const _));
            }
        }
        names
    }

    fn shortcut_app_id(path: &Path) -> String {
        use windows::Win32::System::Com::STGM_READ;
        unsafe {
            let link: IShellLinkW = CoCreateInstance(&ShellLink, None, CLSCTX_INPROC_SERVER).unwrap();
            link.cast::<IPersistFile>().unwrap().Load(&HSTRING::from(path), STGM_READ).unwrap();
            let mut value = link.cast::<IPropertyStore>().unwrap().GetValue(&PKEY_AppUserModel_ID).unwrap();
            let id = (*value.Anonymous.Anonymous).Anonymous.pwszVal.to_string().unwrap();
            PropVariantClear(&mut value).unwrap();
            id
        }
    }

    #[test]
    #[ignore = "creates a hidden native window; run with --ignored"]
    fn window_identity_properties_are_released_before_close() {
        use windows::Win32::System::Variant::VT_EMPTY;
        use windows::Win32::UI::WindowsAndMessaging::{CreateWindowExW, DestroyWindow, WINDOW_EX_STYLE, WS_POPUP};

        struct Window(HWND);
        impl Drop for Window {
            fn drop(&mut self) {
                clear_window_identity(self.0);
                unsafe { let _ = DestroyWindow(self.0); }
            }
        }

        let _com = Com::init();
        let window = Window(unsafe {
            CreateWindowExW(WINDOW_EX_STYLE::default(), w!("STATIC"), w!("Ryn identity test"),
                WS_POPUP, 0, 0, 1, 1, None, None, None, None).unwrap()
        });
        identify_window(window.0, Path::new(r"C:\Ryn\Ryn.exe"), Path::new(r"C:\Ryn\gridcraft.ico"), find("gridcraft").unwrap()).unwrap();
        clear_window_identity(window.0);
        let store: IPropertyStore = unsafe { SHGetPropertyStoreForWindow(window.0).unwrap() };
        for key in [&PKEY_AppUserModel_ID, &PKEY_AppUserModel_RelaunchCommand,
            &PKEY_AppUserModel_RelaunchDisplayNameResource, &PKEY_AppUserModel_RelaunchIconResource] {
            let mut value = unsafe { store.GetValue(key).unwrap() };
            let kind = unsafe { (*value.Anonymous.Anonymous).vt };
            unsafe { PropVariantClear(&mut value).unwrap(); }
            assert_eq!(kind, VT_EMPTY);
        }
    }

    #[test]
    #[ignore = "writes unique test registry entries and a temporary shortcut; run with --ignored"]
    fn windows_offers_ryn_excel_under_open_with() {
        let unique = format!("{}-{}", std::process::id(), std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH).unwrap().as_nanos());
        let app = SystemApp {
            id: Box::leak(format!("gridcraft-test-{unique}").into_boxed_str()),
            name: Box::leak(format!("Ryn Excel Test {unique}").into_boxed_str()),
            documents: find("gridcraft").unwrap().documents,
        };
        let app = &app;
        let folder = std::env::temp_dir().join(format!("ryn-system-apps-{unique}"));
        std::fs::create_dir_all(&folder).unwrap();
        struct Cleanup<'a> { app: &'a SystemApp, folder: PathBuf }
        impl Drop for Cleanup<'_> {
            fn drop(&mut self) {
                unregister(self.app);
                unsafe { SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None) };
                let _ = std::fs::remove_dir_all(&self.folder);
            }
        }
        let cleanup = Cleanup { app, folder: folder.clone() };
        let icon = folder.join("gridcraft.ico");
        std::fs::write(&icon, ico("gridcraft").unwrap()).unwrap();
        let exe = std::env::current_exe().unwrap();
        let _com = Com::init();

        let shortcut = folder.join("Ryn Excel.lnk");
        write_shortcut(&shortcut, &exe, &icon, app).unwrap();
        let shortcut_id = shortcut_app_id(&shortcut);

        register(&exe, &icon, app).unwrap();
        unsafe { SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None) };
        let offered = open_with(".xlsx");
        drop(cleanup);
        let after = open_with(".xlsx");

        assert_eq!(shortcut_id, app.app_user_model_id());
        assert!(offered.iter().any(|name| name == app.name), "Open with: {offered:?}");
        assert!(!after.iter().any(|name| name == app.name), "left behind: {after:?}");
        let user = RegKey::predef(HKEY_CURRENT_USER);
        assert!(user.open_subkey(format!(r"Software\Classes\{}", prog_id(app, "xlsx"))).is_err());
        assert!(user.open_subkey(format!(r"Software\Ryn\Apps\{}", app.id)).is_err());
        println!("Open with .xlsx while registered: {offered:?}");
    }
}
