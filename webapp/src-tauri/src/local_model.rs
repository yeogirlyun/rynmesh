// The picker reads a local path, never uploads the model through the WebView.
#[tauri::command]
pub async fn choose_local_model() -> Result<Option<String>, String> {
    tauri::async_runtime::spawn_blocking(|| {
        rfd::FileDialog::new()
            .set_title("GGUF")
            .add_filter("GGUF", &["gguf"])
            .pick_file()
            .map(|path| path.to_string_lossy().into_owned())
    }).await.map_err(|error| error.to_string())
}
