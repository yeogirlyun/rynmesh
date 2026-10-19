//! macOS: a launcher app per editor in ~/Applications ("Ryn Excel"), which Launchpad, Spotlight
//! and Finder's "Open With" list with the editor's name and icon. See launcher_bundle.rs for why
//! each launcher is an AppleScript applet and how documents reach Ryn.

use std::path::{Path, PathBuf};
use std::process::Command;
use super::{icns, launcher_bundle, SystemApp, APPS};

const LSREGISTER: &str = "/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister";

/// Creates or updates the launchers of `wanted` apps and removes Ryn's launchers of every other
/// app. Reports the outcome of every wanted app.
pub fn apply(wanted: &[&'static SystemApp]) -> Vec<(&'static str, Result<(), String>)> {
    let folder = match applications_folder() {
        Ok(folder) => folder,
        Err(error) => return wanted.iter().map(|app| (app.id, Err(error.clone()))).collect(),
    };
    let mut outcomes = Vec::new();
    for app in APPS {
        if wanted.iter().any(|wanted| wanted.id == app.id) {
            outcomes.push((app.id, install(&folder, app)));
        } else {
            remove(&folder.join(format!("{}.app", app.name)), app);
        }
    }
    outcomes
}

/// Whether the app's launcher is in place, as Ryn wrote it.
pub fn present(app: &SystemApp) -> bool {
    applications_folder().is_ok_and(|folder| {
        let bundle = folder.join(format!("{}.app", app.name));
        launcher_bundle::owned(&bundle, &launcher_bundle::bundle_id(app)) && launcher_bundle::current_revision(&bundle).is_some()
    })
}

/// The user asked for the app to open its types by default: its launcher becomes their handler.
pub fn choose_default(app: &SystemApp) -> Result<(), String> {
    if !present(app) {
        return Err("system_app_missing".into());
    }
    let handler = CfString::new(&launcher_bundle::bundle_id(app));
    for doc in app.documents {
        let content_type = CfString::new(doc.uti);
        let status = unsafe { LSSetDefaultRoleHandlerForContentType(content_type.0, LS_ROLES_ALL, handler.0) };
        if status != 0 {
            return Err(format!("default_app_failed: {} ({status})", doc.ext));
        }
    }
    Ok(())
}

fn applications_folder() -> Result<PathBuf, String> {
    let home = std::env::var_os("HOME").map(PathBuf::from).ok_or("home_unavailable")?;
    let folder = home.join("Applications");
    std::fs::create_dir_all(&folder).map_err(|error| format!("launcher_failed: {error}"))?;
    Ok(folder)
}

fn install(folder: &Path, app: &SystemApp) -> Result<(), String> {
    let bundle = folder.join(format!("{}.app", app.name));
    let id = launcher_bundle::bundle_id(app);
    let icon = icns(app.id).ok_or("system_app_unknown")?;
    let script = launcher_bundle::applescript(app);
    let version = env!("CARGO_PKG_VERSION");
    let revision = launcher_bundle::revision(&[script.as_bytes(), version.as_bytes(),
        launcher_bundle::document_types(app).as_bytes(), icon]);
    if launcher_bundle::owned(&bundle, &id) && launcher_bundle::current_revision(&bundle).as_deref() == Some(revision.as_str()) {
        return Ok(());
    }
    launcher_bundle::ensure_writable(&bundle, &id).map_err(|error| format!("launcher_failed: {error}"))?;
    // Build beside the old launcher (a hidden name Spotlight skips), then swap it in.
    let staging = folder.join(format!(".{}.ryn-staging.app", app.name));
    let _ = std::fs::remove_dir_all(&staging);
    if let Err(error) = build(&staging, app, &script, version, &revision, icon) {
        let _ = std::fs::remove_dir_all(&staging);
        return Err(format!("launcher_failed: {error}"));
    }
    replace(folder, &bundle, &staging, app).map_err(|error| format!("launcher_failed: {error}"))?;
    if let Err(error) = run(Command::new(LSREGISTER).arg("-f").arg(&bundle)) {
        log::warn!("could not register {}: {error}", bundle.display());
    }
    Ok(())
}

fn build(staging: &Path, app: &SystemApp, script: &str, version: &str, revision: &str, icon: &[u8]) -> Result<(), String> {
    let mut compile = Command::new("/usr/bin/osacompile");
    compile.arg("-o").arg(staging);
    for line in script.lines() { compile.arg("-e").arg(line); }
    run(&mut compile)?;
    let plist = staging.join("Contents/Info.plist");
    for (key, kind, value) in launcher_bundle::plist_edits(app, version, revision) {
        run(Command::new("/usr/bin/plutil").args(["-replace", key, kind, value.as_str()]).arg(&plist))?;
    }
    if app.documents.is_empty() {
        // No `on open` handler means osacompile declared no types; make sure none remain.
        let _ = Command::new("/usr/bin/plutil").args(["-remove", "CFBundleDocumentTypes"]).arg(&plist).output();
    }
    run(Command::new("/usr/bin/plutil").args(["-convert", "xml1"]).arg(&plist))?;
    let resources = staging.join("Contents/Resources");
    std::fs::write(resources.join("AppIcon.icns"), icon).map_err(|error| error.to_string())?;
    let _ = std::fs::remove_file(resources.join("applet.icns"));
    // The edits above break the seal osacompile signed; sign the finished launcher again, ad hoc.
    run(Command::new("/usr/bin/codesign").args(["--force", "--sign", "-"]).arg(staging))
}

fn replace(folder: &Path, bundle: &Path, staging: &Path, app: &SystemApp) -> std::io::Result<()> {
    if std::fs::symlink_metadata(bundle).is_err() {
        return std::fs::rename(staging, bundle);
    }
    let old = folder.join(format!(".{}.ryn-old.app", app.name));
    let _ = std::fs::remove_dir_all(&old);
    std::fs::rename(bundle, &old)?;
    if let Err(error) = std::fs::rename(staging, bundle) {
        let _ = std::fs::rename(&old, bundle);
        return Err(error);
    }
    let _ = std::fs::remove_dir_all(&old);
    Ok(())
}

/// Unregisters and deletes a launcher Ryn owns, so Finder stops offering it at once.
fn remove(bundle: &Path, app: &SystemApp) {
    if !launcher_bundle::owned(bundle, &launcher_bundle::bundle_id(app)) { return; }
    if let Err(error) = run(Command::new(LSREGISTER).arg("-u").arg(bundle)) {
        log::warn!("could not unregister {}: {error}", bundle.display());
    }
    if let Err(error) = std::fs::remove_dir_all(bundle) {
        log::warn!("could not remove {}: {error}", bundle.display());
    }
}

fn run(command: &mut Command) -> Result<(), String> {
    let program = command.get_program().to_string_lossy().into_owned();
    let output = command.output().map_err(|error| format!("{program}: {error}"))?;
    if output.status.success() { return Ok(()); }
    Err(format!("{program}: {}", String::from_utf8_lossy(&output.stderr).trim()))
}

type CFStringRef = *const std::ffi::c_void;
const UTF8: u32 = 0x0800_0100;
const LS_ROLES_ALL: u32 = 0xFFFF_FFFF;

#[link(name = "CoreFoundation", kind = "framework")]
extern "C" {
    fn CFStringCreateWithBytes(allocator: *const std::ffi::c_void, bytes: *const u8, length: isize, encoding: u32, external: u8) -> CFStringRef;
    fn CFRelease(value: *const std::ffi::c_void);
}

#[link(name = "CoreServices", kind = "framework")]
extern "C" {
    fn LSSetDefaultRoleHandlerForContentType(content_type: CFStringRef, role: u32, handler_bundle_id: CFStringRef) -> i32;
}

struct CfString(CFStringRef);

impl CfString {
    fn new(value: &str) -> Self {
        CfString(unsafe { CFStringCreateWithBytes(std::ptr::null(), value.as_ptr(), value.len() as isize, UTF8, 0) })
    }
}

impl Drop for CfString {
    fn drop(&mut self) {
        if !self.0.is_null() { unsafe { CFRelease(self.0) } }
    }
}
