//! The macOS launcher of an app, built by the macOS writer and checked by portable tests.
//!
//! Finder hands documents to an app as an "open documents" Apple Event, never as arguments, so
//! a launcher has to be a real app that handles that event. Each launcher is an AppleScript
//! applet (osacompile) whose `on open` handler passes the documents on to Ryn with `open -b`;
//! Ryn then opens each in the app for its type. The applet declares only its own document types,
//! as an alternate handler, and exists only while its editor is installed and enabled.

use std::io;
use std::path::Path;
use super::{SystemApp, IDENTIFIER};

/// Info.plist key holding a fingerprint of everything Ryn put into the launcher.
pub(super) const REVISION_KEY: &str = "RynLauncherRevision";

/// Every path inside a launcher that Ryn writes or replaces, in the applet layout.
pub(super) const WRITTEN: [&str; 8] = ["", "Contents", "Contents/MacOS", "Contents/Resources",
    "Contents/Info.plist", "Contents/MacOS/applet", "Contents/Resources/Scripts", "Contents/Resources/AppIcon.icns"];

pub(super) fn bundle_id(app: &SystemApp) -> String {
    format!("{IDENTIFIER}.app.{}", app.id)
}

/// The applet's script. Running it opens the app; opening documents with it hands them to Ryn.
pub(super) fn applescript(app: &SystemApp) -> String {
    let mut lines = vec![
        format!("-- {}: opens the app in Ryn. Ryn removes this launcher when the app is turned off.", app.name),
        "on run".to_string(),
        // A second Ryn passes its arguments to the running one and exits (single-instance plugin).
        format!("\tdo shell script \"/usr/bin/open -n -b '{IDENTIFIER}' --args --app '{}'\"", app.id),
        "end run".to_string(),
    ];
    if !app.documents.is_empty() {
        lines.extend([
            "on open theDocuments".to_string(),
            // `open -b` delivers them to Ryn as an Apple Event too, starting Ryn when needed.
            format!("\tset command to \"/usr/bin/open -b '{IDENTIFIER}'\""),
            "\trepeat with theDocument in theDocuments".to_string(),
            "\t\tset command to command & \" \" & quoted form of POSIX path of theDocument".to_string(),
            "\tend repeat".to_string(),
            "\tdo shell script command".to_string(),
            "end open".to_string(),
        ]);
    }
    lines.join("\n")
}

/// CFBundleDocumentTypes for `plutil -replace … -xml`: only the app's types, never the default.
pub(super) fn document_types(app: &SystemApp) -> String {
    let mut xml = String::from("<array>");
    for doc in app.documents {
        xml.push_str(&format!(concat!("<dict><key>CFBundleTypeName</key><string>{}</string>",
            "<key>CFBundleTypeRole</key><string>Editor</string>",
            "<key>LSHandlerRank</key><string>Alternate</string>",
            "<key>CFBundleTypeIconFile</key><string>AppIcon</string>",
            "<key>LSItemContentTypes</key><array><string>{}</string></array></dict>"), escape(doc.kind), escape(doc.uti)));
    }
    xml.push_str("</array>");
    xml
}

/// The `plutil -replace` edits that turn osacompile's applet into the app's launcher.
pub(super) fn plist_edits(app: &SystemApp, version: &str, revision: &str) -> Vec<(&'static str, &'static str, String)> {
    let mut edits = vec![
        ("CFBundleIdentifier", "-string", bundle_id(app)),
        ("CFBundleName", "-string", app.name.to_string()),
        ("CFBundleDisplayName", "-string", app.name.to_string()),
        ("CFBundleIconFile", "-string", "AppIcon".to_string()),
        ("CFBundleShortVersionString", "-string", version.to_string()),
        ("CFBundleVersion", "-string", version.to_string()),
        // The applet only relays to Ryn; it never needs a Dock icon of its own.
        ("LSUIElement", "-bool", "YES".to_string()),
        (REVISION_KEY, "-string", revision.to_string()),
    ];
    if !app.documents.is_empty() {
        edits.push(("CFBundleDocumentTypes", "-xml", document_types(app)));
    }
    edits
}

/// A stable fingerprint, so an unchanged launcher is not rebuilt and re-signed on every start.
pub(super) fn revision(parts: &[&[u8]]) -> String {
    let mut hash: u64 = 0xcbf2_9ce4_8422_2325;
    for part in parts {
        for byte in part.iter().chain(&[0xff]) {
            hash ^= u64::from(*byte);
            hash = hash.wrapping_mul(0x0100_0000_01b3);
        }
    }
    format!("{hash:016x}")
}

pub(super) fn current_revision(bundle: &Path) -> Option<String> {
    let plist = std::fs::read_to_string(bundle.join("Contents/Info.plist")).ok()?;
    plist_string(&plist, REVISION_KEY).map(str::to_string)
}

/// An existing launcher may only be updated when Ryn owns it. Check every path
/// the writer follows first, including files which could themselves be links.
pub(super) fn ensure_writable(bundle: &Path, identifier: &str) -> io::Result<()> {
    if checked_exists(bundle)? && !has_identifier(bundle, identifier) {
        return Err(io::Error::new(io::ErrorKind::AlreadyExists, "launcher path belongs to another app"));
    }
    Ok(())
}

/// Unowned or linked bundles are left alone during removal too.
pub(super) fn owned(bundle: &Path, identifier: &str) -> bool {
    matches!(checked_exists(bundle), Ok(true)) && has_identifier(bundle, identifier)
}

fn checked_exists(bundle: &Path) -> io::Result<bool> {
    for relative in WRITTEN {
        let path = if relative.is_empty() { bundle.to_path_buf() } else { bundle.join(relative) };
        match std::fs::symlink_metadata(&path) {
            Ok(metadata) if metadata.file_type().is_symlink() => {
                return Err(io::Error::new(io::ErrorKind::PermissionDenied, "launcher path contains a symbolic link"));
            }
            Ok(_) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                if relative.is_empty() { return Ok(false); }
            }
            Err(error) => return Err(error),
        }
    }
    Ok(true)
}

fn has_identifier(bundle: &Path, identifier: &str) -> bool {
    // A matching value under some other key, or inside a comment, does not establish ownership.
    std::fs::read_to_string(bundle.join("Contents/Info.plist"))
        .is_ok_and(|plist| !plist.contains("<!--") && plist_string(&plist, "CFBundleIdentifier") == Some(identifier))
}

/// The string under `key` in an XML property list as osacompile and plutil write it, with any
/// whitespace between key and value. A key present more than once is ambiguous and yields none.
pub(super) fn plist_string<'a>(plist: &'a str, key: &str) -> Option<&'a str> {
    let marker = format!("<key>{key}</key>");
    let mut matches = plist.match_indices(&marker);
    let (index, _) = matches.next()?;
    if matches.next().is_some() { return None; }
    let value = plist[index + marker.len()..].trim_start().strip_prefix("<string>")?;
    Some(&value[..value.find("</string>")?])
}

fn escape(text: &str) -> String {
    text.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::system_apps::find;
    use std::path::PathBuf;
    use std::sync::atomic::{AtomicU64, Ordering};

    const ID: &str = "ai.rynmesh.desktop.app.gridcraft";
    static NEXT: AtomicU64 = AtomicU64::new(0);

    struct TempDir(PathBuf);

    impl TempDir {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!("ryn-launcher-test-{}-{}", std::process::id(), NEXT.fetch_add(1, Ordering::Relaxed)));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
    }

    impl Drop for TempDir {
        fn drop(&mut self) { let _ = std::fs::remove_dir_all(&self.0); }
    }

    fn bundle(root: &Path, plist: &str) -> PathBuf {
        let path = root.join("Ryn Excel.app");
        std::fs::create_dir_all(path.join("Contents/MacOS")).unwrap();
        std::fs::create_dir_all(path.join("Contents/Resources/Scripts")).unwrap();
        std::fs::write(path.join("Contents/Info.plist"), plist).unwrap();
        path
    }

    /// The way plutil writes a converted property list: key and value on their own lines.
    fn plutil_plist(identifier: &str) -> String {
        format!("<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n<plist version=\"1.0\">\n<dict>\n\t<key>CFBundleExecutable</key>\n\t<string>applet</string>\n\t<key>CFBundleIdentifier</key>\n\t<string>{identifier}</string>\n\t<key>{REVISION_KEY}</key>\n\t<string>00aa</string>\n</dict>\n</plist>\n")
    }

    #[test]
    fn new_launchers_and_our_existing_launchers_can_be_written() {
        let root = TempDir::new();
        assert!(ensure_writable(&root.0.join("Ryn Excel.app"), ID).is_ok());
        let path = bundle(&root.0, &plutil_plist(ID));
        assert!(ensure_writable(&path, ID).is_ok());
        assert!(owned(&path, ID));
        assert_eq!(current_revision(&path).as_deref(), Some("00aa"));
    }

    #[test]
    fn another_apps_bundle_is_neither_overwritten_nor_removed() {
        let root = TempDir::new();
        let original = format!("<key>CFBundleIdentifier</key><string>com.example.excel</string><key>OtherValue</key><string>{ID}</string>");
        let path = bundle(&root.0, &original);
        std::fs::write(path.join("Contents/MacOS/applet"), b"original launcher").unwrap();
        assert_eq!(ensure_writable(&path, ID).unwrap_err().kind(), io::ErrorKind::AlreadyExists);
        assert!(!owned(&path, ID));
        assert_eq!(std::fs::read_to_string(path.join("Contents/Info.plist")).unwrap(), original);
        assert_eq!(std::fs::read(path.join("Contents/MacOS/applet")).unwrap(), b"original launcher");
    }

    #[test]
    fn a_commented_or_repeated_identifier_does_not_establish_ownership() {
        let root = TempDir::new();
        let path = bundle(&root.0, &format!("<!-- <key>CFBundleIdentifier</key><string>{ID}</string> -->"));
        assert!(ensure_writable(&path, ID).is_err());
        assert!(!owned(&path, ID));
        let repeated = format!("<key>CFBundleIdentifier</key><string>{ID}</string><key>CFBundleIdentifier</key><string>com.example</string>");
        assert_eq!(plist_string(&repeated, "CFBundleIdentifier"), None);
    }

    #[cfg(unix)]
    #[test]
    fn symbolic_links_at_every_written_path_are_rejected() {
        use std::os::unix::fs::symlink;
        for relative in WRITTEN {
            let root = TempDir::new();
            let path = bundle(&root.0, &plutil_plist(ID));
            let linked = if relative.is_empty() { path.clone() } else { path.join(relative) };
            let target = root.0.join("unrelated");
            if linked.is_dir() {
                std::fs::rename(&linked, &target).unwrap();
            } else {
                if linked.exists() { std::fs::rename(&linked, &target).unwrap(); }
                else { std::fs::write(&target, b"unrelated").unwrap(); }
            }
            symlink(&target, &linked).unwrap();
            assert_eq!(ensure_writable(&path, ID).unwrap_err().kind(), io::ErrorKind::PermissionDenied, "{relative}");
            assert!(!owned(&path, ID), "{relative}");
        }
    }

    #[test]
    fn the_applet_opens_its_app_and_passes_finder_documents_to_ryn() {
        let excel = applescript(find("gridcraft").unwrap());
        assert!(excel.contains("on run\n\tdo shell script \"/usr/bin/open -n -b 'ai.rynmesh.desktop' --args --app 'gridcraft'\"\nend run"));
        assert!(excel.contains("on open theDocuments"));
        assert!(excel.contains("quoted form of POSIX path of theDocument"));
        assert!(excel.contains("set command to \"/usr/bin/open -b 'ai.rynmesh.desktop'\""));
        // An app without documents declares no open handler, so Finder never offers it.
        assert!(!applescript(find("cadcraft").unwrap()).contains("on open"));
    }

    #[test]
    fn launchers_declare_only_their_own_types_and_never_the_default() {
        let excel = find("gridcraft").unwrap();
        let types = document_types(excel);
        assert_eq!(types.matches("<dict>").count(), excel.documents.len());
        assert_eq!(types.matches("<string>Alternate</string>").count(), excel.documents.len());
        assert!(types.contains("<key>LSItemContentTypes</key><array><string>org.openxmlformats.spreadsheetml.sheet</string></array>"));
        assert!(types.contains("<key>CFBundleTypeName</key><string>Excel workbook</string>"));
        assert!(!types.contains("public.data") && !types.contains("****") && !types.contains("docx"));

        let edits = plist_edits(excel, "0.7.0", "rev");
        let value = |key: &str| edits.iter().find(|(name, _, _)| *name == key).map(|(_, kind, value)| (*kind, value.as_str()));
        assert_eq!(value("CFBundleIdentifier"), Some(("-string", ID)));
        assert_eq!(value("CFBundleName"), Some(("-string", "Ryn Excel")));
        assert_eq!(value("LSUIElement"), Some(("-bool", "YES")));
        assert_eq!(value(REVISION_KEY), Some(("-string", "rev")));
        assert_eq!(value("CFBundleDocumentTypes"), Some(("-xml", types.as_str())));
        assert!(plist_edits(find("cadcraft").unwrap(), "0.7.0", "rev").iter().all(|(key, _, _)| *key != "CFBundleDocumentTypes"));
    }

    #[test]
    fn the_revision_follows_every_input() {
        let base = revision(&[b"script", b"0.7.0", b"icon"]);
        assert_eq!(base, revision(&[b"script", b"0.7.0", b"icon"]));
        assert_ne!(base, revision(&[b"script", b"0.7.1", b"icon"]));
        assert_ne!(base, revision(&[b"scrip", b"t0.7.0", b"icon"]));
    }
}
