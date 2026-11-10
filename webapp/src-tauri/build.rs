fn main() {
  println!("cargo:rerun-if-env-changed=RYN_DESKTOP_UPDATE_PUBLIC_KEY");
  println!("cargo:rustc-env=RYN_TARGET={}", std::env::var("TARGET").unwrap());
  tauri_build::build()
}
