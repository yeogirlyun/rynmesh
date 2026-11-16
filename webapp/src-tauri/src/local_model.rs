// The picker reads a local path, never uploads the model through the WebView.
use std::fs::File;
use std::io::{ErrorKind, Read};
use std::path::Path;

const GGUF_MAGIC: [u8; 4] = [0x47, 0x47, 0x55, 0x46];

pub(crate) fn is_gguf(path: &Path) -> Result<(), &'static str> {
    let mut file = File::open(path).map_err(|_| "local_model_unreadable")?;
    let mut magic = [0u8; 4];
    match file.read_exact(&mut magic) {
        Ok(()) if magic == GGUF_MAGIC => Ok(()),
        Ok(()) => Err("local_model_not_gguf"),
        Err(error) if error.kind() == ErrorKind::UnexpectedEof => Err("local_model_not_gguf"),
        Err(_) => Err("local_model_unreadable"),
    }
}

#[tauri::command]
pub async fn choose_local_model() -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let Some(path) = rfd::FileDialog::new()
            .set_title("选择 GGUF 模型文件")
            .add_filter("GGUF", &["gguf"])
            .pick_file()
        else {
            return Ok(None);
        };
        is_gguf(&path).map_err(str::to_owned)?;
        Ok(Some(path.to_string_lossy().into_owned()))
    })
    .await
    .map_err(|error| error.to_string())?
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_valid_gguf_magic_header() {
        let dir = std::env::temp_dir().join(format!("ryn-gguf-ok-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let file = dir.join("valid.gguf");
        std::fs::write(&file, b"GGUF\x03\x00\x00\x00weights").unwrap();
        assert_eq!(is_gguf(&file), Ok(()));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn rejects_text_file_empty_file_and_missing_path() {
        let dir = std::env::temp_dir().join(format!("ryn-gguf-bad-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();

        let text_file = dir.join("notes.txt");
        std::fs::write(&text_file, b"plain text, not gguf").unwrap();
        assert_eq!(is_gguf(&text_file), Err("local_model_not_gguf"));

        let empty_file = dir.join("empty.gguf");
        std::fs::write(&empty_file, b"").unwrap();
        assert_eq!(is_gguf(&empty_file), Err("local_model_not_gguf"));

        let missing = dir.join("does-not-exist.gguf");
        assert_eq!(is_gguf(&missing), Err("local_model_unreadable"));

        std::fs::remove_dir_all(&dir).unwrap();
    }
}
