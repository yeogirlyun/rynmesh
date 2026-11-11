#[path = "update_key_build.rs"]
mod update_key;

fn main() {
  println!("cargo:rerun-if-env-changed=RYN_DESKTOP_UPDATE_PUBLIC_KEY");
  println!("cargo:rerun-if-env-changed=RYN_DESKTOP_UPDATE_TEST_ENDPOINT");
  println!("cargo:rerun-if-changed=desktop-update.pub");
  println!("cargo:rerun-if-changed=update_key_build.rs");
  let key = update_key::resolve_public_key(
    std::env::var("RYN_DESKTOP_UPDATE_PUBLIC_KEY").ok().as_deref(),
    std::env::var("RYN_DESKTOP_UPDATE_TEST_ENDPOINT").ok().as_deref(),
  ).expect("Invalid desktop updater trust configuration");
  println!("cargo:rustc-env=RYN_DESKTOP_UPDATE_PUBLIC_KEY={key}");
  println!("cargo:rustc-env=RYN_TARGET={}", std::env::var("TARGET").unwrap());
  tauri_build::build()
}
