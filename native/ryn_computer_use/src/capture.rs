use crate::protocol::{ScreenshotArtifact, ScreenshotOptions, WindowGeometry, MAX_PNG_BYTES};
use crate::windows::{enumerate_top_level_hwnds, get_window_geometry, get_window_pid_tid,
    is_hwnd_related_to_target, is_window_capture_protected, is_window_cloaked};
use base64::engine::general_purpose::STANDARD as BASE64_STANDARD;
use base64::Engine;
use png::{BitDepth, ColorType, Encoder};
use sha2::{Digest, Sha256};
use std::ffi::c_void;
use windows::Win32::Foundation::{HWND, POINT};
use windows::Win32::Graphics::Gdi::{
    CreateCompatibleDC, CreateDIBSection, DeleteDC, DeleteObject, GetDC, ReleaseDC, SelectObject,
    BITMAPINFO, BITMAPINFOHEADER, BI_RGB, DIB_RGB_COLORS, HBITMAP, HDC, HGDIOBJ,
};
use windows::Win32::Storage::Xps::{PrintWindow, PRINT_WINDOW_FLAGS, PW_CLIENTONLY};
use windows::Win32::UI::WindowsAndMessaging::{IsIconic, IsWindowVisible, WindowFromPoint};

const PW_RENDERFULLCONTENT_FLAG: u32 = 0x00000002;

/// Phase-1 window-scoped PNG screenshot candidate using `PrintWindow` (`PW_RENDERFULLCONTENT`).
/// Never silently crops the desktop screen buffer when another window occludes the target;
/// instead reports `minimized`, `capture_protected`, or `unavailable` status honestly.
pub fn capture_window_screenshot(
    hwnd: HWND,
    geometry: &WindowGeometry,
    options: &ScreenshotOptions,
    artifact_id: String,
) -> ScreenshotArtifact {
    let enabled = options.enabled.unwrap_or(true);
    let client_only = options
        .mode
        .as_deref()
        .map(|m| m.eq_ignore_ascii_case("client"))
        .unwrap_or(false);

    let target_rect = if client_only {
        geometry.client_rect
    } else {
        geometry.window_rect
    };

    let width = target_rect.width.max(0) as u32;
    let height = target_rect.height.max(0) as u32;
    let origin_x = target_rect.left;
    let origin_y = target_rect.top;
    let z_index = 0u32;
    let minimized = unsafe { IsIconic(hwnd).as_bool() };
    let visible = unsafe { IsWindowVisible(hwnd).as_bool() };
    let cloaked = is_window_cloaked(hwnd);
    let capture_protected = is_window_capture_protected(hwnd);
    let occluded = check_window_center_occluded(hwnd, &target_rect);

    if !enabled {
        return ScreenshotArtifact {
            id: artifact_id,
            status: "unavailable".to_string(),
            capture_method: "none".to_string(),
            mime_type: "image/png".to_string(),
            width: 0,
            height: 0,
            origin_x,
            origin_y,
            z_index,
            dpi: geometry.dpi,
            scale_factor: geometry.scale_factor,
            sha256: empty_sha256(),
            data_base64: None,
            minimized,
            occluded,
            capture_protected,
        };
    }

    if minimized {
        return ScreenshotArtifact {
            id: artifact_id,
            status: "minimized".to_string(),
            capture_method: "none".to_string(),
            mime_type: "image/png".to_string(),
            width: 0,
            height: 0,
            origin_x,
            origin_y,
            z_index,
            dpi: geometry.dpi,
            scale_factor: geometry.scale_factor,
            sha256: empty_sha256(),
            data_base64: None,
            minimized: true,
            occluded,
            capture_protected,
        };
    }

    if capture_protected {
        return ScreenshotArtifact {
            id: artifact_id,
            status: "capture_protected".to_string(),
            capture_method: "none".to_string(),
            mime_type: "image/png".to_string(),
            width: 0,
            height: 0,
            origin_x,
            origin_y,
            z_index,
            dpi: geometry.dpi,
            scale_factor: geometry.scale_factor,
            sha256: empty_sha256(),
            data_base64: None,
            minimized: false,
            occluded,
            capture_protected: true,
        };
    }

    if !visible || cloaked || width == 0 || height == 0 || width > 8192 || height > 8192 {
        return ScreenshotArtifact {
            id: artifact_id,
            status: "unavailable".to_string(),
            capture_method: "none".to_string(),
            mime_type: "image/png".to_string(),
            width: 0,
            height: 0,
            origin_x,
            origin_y,
            z_index,
            dpi: geometry.dpi,
            scale_factor: geometry.scale_factor,
            sha256: empty_sha256(),
            data_base64: None,
            minimized,
            occluded,
            capture_protected,
        };
    }

    match capture_via_print_window(hwnd, width, height, client_only) {
        Ok((mut rgba, mut method_name)) => {
            if is_buffer_completely_blank(&rgba) {
                return ScreenshotArtifact {
                    id: artifact_id,
                    status: "unavailable".to_string(),
                    capture_method: method_name.to_string(),
                    mime_type: "image/png".to_string(),
                    width,
                    height,
                    origin_x,
                    origin_y,
                    z_index,
                    dpi: geometry.dpi,
                    scale_factor: geometry.scale_factor,
                    sha256: empty_sha256(),
                    data_base64: None,
                    minimized,
                    occluded,
                    capture_protected,
                };
            }

            if composite_owned_popups(hwnd, &target_rect, &mut rgba) {
                method_name = "print_window_with_owned_popups";
            }
            match encode_rgba_to_png(&rgba, width, height) {
                Ok(png_bytes) => {
                    if png_bytes.len() > MAX_PNG_BYTES {
                        return ScreenshotArtifact {
                            id: artifact_id,
                            status: "unavailable".to_string(),
                            capture_method: method_name.to_string(),
                            mime_type: "image/png".to_string(),
                            width,
                            height,
                            origin_x,
                            origin_y,
                            z_index,
                            dpi: geometry.dpi,
                            scale_factor: geometry.scale_factor,
                            sha256: empty_sha256(),
                            data_base64: None,
                            minimized,
                            occluded,
                            capture_protected,
                        };
                    }
                    let mut hasher = Sha256::new();
                    hasher.update(&png_bytes);
                    let sha256 = hex::encode(hasher.finalize());
                    let b64 = BASE64_STANDARD.encode(&png_bytes);

                    ScreenshotArtifact {
                        id: artifact_id,
                        status: "ok".to_string(),
                        capture_method: method_name.to_string(),
                        mime_type: "image/png".to_string(),
                        width,
                        height,
                        origin_x,
                        origin_y,
                        z_index,
                        dpi: geometry.dpi,
                        scale_factor: geometry.scale_factor,
                        sha256,
                        data_base64: Some(b64),
                        minimized: false,
                        occluded,
                        capture_protected: false,
                    }
                }
                Err(_) => ScreenshotArtifact {
                    id: artifact_id,
                    status: "unavailable".to_string(),
                    capture_method: method_name.to_string(),
                    mime_type: "image/png".to_string(),
                    width: 0,
                    height: 0,
                    origin_x,
                    origin_y,
                    z_index,
                    dpi: geometry.dpi,
                    scale_factor: geometry.scale_factor,
                    sha256: empty_sha256(),
                    data_base64: None,
                    minimized,
                    occluded,
                    capture_protected,
                },
            }
        }
        Err(_) => ScreenshotArtifact {
            id: artifact_id,
            status: "unavailable".to_string(),
            capture_method: "none".to_string(),
            mime_type: "image/png".to_string(),
            width: 0,
            height: 0,
            origin_x,
            origin_y,
            z_index,
            dpi: geometry.dpi,
            scale_factor: geometry.scale_factor,
            sha256: empty_sha256(),
            data_base64: None,
            minimized,
            occluded,
            capture_protected,
        },
    }
}

fn check_window_center_occluded(hwnd: HWND, rect: &crate::protocol::RectPx) -> bool {
    if rect.width <= 4 || rect.height <= 4 {
        return false;
    }
    let center = POINT {
        x: rect.left + rect.width / 2,
        y: rect.top + rect.height / 2,
    };
    unsafe {
        let hit = WindowFromPoint(center);
        if hit.0.is_null() {
            return true;
        }
        !is_hwnd_related_to_target(hit, hwnd)
    }
}

/// PrintWindow on a main HWND omits owned top-level menus/search results.
/// Composite only visible, non-protected, directionally-owned windows from
/// the same process; never read pixels from the desktop or unrelated windows.
fn composite_owned_popups(hwnd: HWND, target: &crate::protocol::RectPx, rgba: &mut [u8]) -> bool {
    let pid = get_window_pid_tid(hwnd).0;
    let mut candidates: Vec<_> = enumerate_top_level_hwnds().into_iter().filter(|candidate| {
        *candidate != hwnd && unsafe { IsWindowVisible(*candidate).as_bool() }
            && get_window_pid_tid(*candidate).0 == pid
            && !unsafe { IsIconic(*candidate).as_bool() }
            && !is_window_cloaked(*candidate) && !is_window_capture_protected(*candidate)
            && is_hwnd_related_to_target(*candidate, hwnd)
    }).take(16).collect();
    // EnumWindows is top-to-bottom; paint lower popups before higher ones.
    candidates.reverse();
    let mut painted = false;
    for candidate in candidates {
        let Ok(geometry) = get_window_geometry(candidate) else { continue; };
        let rect = geometry.window_rect;
        if rect.width <= 0 || rect.height <= 0 || rect.width > 8192 || rect.height > 8192
            // Root-sized owned decoration/shadow surfaces are not popup
            // content. PrintWindow renders their transparency as an opaque
            // blank canvas. Separate large dialogs need their own observation.
            || rect.width >= target.width || rect.height >= target.height
            || rect.left < target.left || rect.top < target.top
            || rect.right > target.right || rect.bottom > target.bottom
            || rect.right <= target.left || rect.left >= target.right
            || rect.bottom <= target.top || rect.top >= target.bottom { continue; }
        let Ok((pixels, _)) = capture_via_print_window(candidate, rect.width as u32, rect.height as u32, false) else { continue; };
        if is_buffer_completely_blank(&pixels) || !is_hwnd_related_to_target(candidate, hwnd)
            || is_window_capture_protected(candidate) { continue; }
        composite_rgba(rgba, target, &pixels, &rect);
        painted = true;
    }
    painted
}

fn composite_rgba(dst: &mut [u8], target: &crate::protocol::RectPx, src: &[u8], rect: &crate::protocol::RectPx) {
    let left = target.left.max(rect.left);
    let right = target.right.min(rect.right);
    let top = target.top.max(rect.top);
    let bottom = target.bottom.min(rect.bottom);
    if left >= right || top >= bottom { return; }
    let count = (right - left) as usize * 4;
    for y in top..bottom {
        let di = (((y - target.top) as usize * target.width as usize) + (left - target.left) as usize) * 4;
        let si = (((y - rect.top) as usize * rect.width as usize) + (left - rect.left) as usize) * 4;
        dst[di..di + count].copy_from_slice(&src[si..si + count]);
    }
}

fn capture_via_print_window(
    hwnd: HWND,
    width: u32,
    height: u32,
    client_only: bool,
) -> Result<(Vec<u8>, &'static str), ()> {
    unsafe {
        let screen_dc = GetDC(Some(hwnd));
        if screen_dc.0.is_null() {
            return Err(());
        }
        let _release_dc = ScreenDcGuard(hwnd, screen_dc);

        let mem_dc = CreateCompatibleDC(Some(screen_dc));
        if mem_dc.0.is_null() {
            return Err(());
        }
        let _mem_dc_guard = MemDcGuard(mem_dc);

        let bmi = BITMAPINFO {
            bmiHeader: BITMAPINFOHEADER {
                biSize: std::mem::size_of::<BITMAPINFOHEADER>() as u32,
                biWidth: width as i32,
                biHeight: -(height as i32), // Top-down DIB
                biPlanes: 1,
                biBitCount: 32,
                biCompression: BI_RGB.0,
                biSizeImage: width * height * 4,
                biXPelsPerMeter: 0,
                biYPelsPerMeter: 0,
                biClrUsed: 0,
                biClrImportant: 0,
            },
            bmiColors: Default::default(),
        };

        let mut bits_ptr: *mut c_void = std::ptr::null_mut();
        let hbm = CreateDIBSection(
            Some(mem_dc),
            &bmi,
            DIB_RGB_COLORS,
            &mut bits_ptr,
            None,
            0,
        )
        .map_err(|_| ())?;

        if hbm.is_invalid() || bits_ptr.is_null() {
            return Err(());
        }
        let _bmp_guard = BitmapGuard(hbm);

        let old_obj = SelectObject(mem_dc, HGDIOBJ::from(hbm));
        let _select_guard = SelectObjectGuard(mem_dc, old_obj);

        let base_flag = if client_only { PW_CLIENTONLY.0 } else { 0 };
        let full_flags = PRINT_WINDOW_FLAGS(base_flag | PW_RENDERFULLCONTENT_FLAG);
        let mut ok = PrintWindow(hwnd, mem_dc, full_flags).as_bool();
        let mut method = "print_window_full_content";

        if !ok {
            let fallback_flags = PRINT_WINDOW_FLAGS(base_flag);
            ok = PrintWindow(hwnd, mem_dc, fallback_flags).as_bool();
            method = "print_window";
        }

        if !ok {
            return Err(());
        }

        let byte_len = (width as usize) * (height as usize) * 4;
        let raw_bgra = std::slice::from_raw_parts(bits_ptr as *const u8, byte_len);
        let mut rgba = vec![0u8; byte_len];
        for (src, dst) in raw_bgra.chunks_exact(4).zip(rgba.chunks_exact_mut(4)) {
            dst[0] = src[2]; // R
            dst[1] = src[1]; // G
            dst[2] = src[0]; // B
            dst[3] = 255; // Opaque alpha
        }

        Ok((rgba, method))
    }
}

fn is_buffer_completely_blank(rgba: &[u8]) -> bool {
    rgba.chunks_exact(4)
        .all(|px| px[0] == 0 && px[1] == 0 && px[2] == 0)
}

pub fn encode_rgba_to_png(rgba: &[u8], width: u32, height: u32) -> Result<Vec<u8>, ()> {
    let mut buf = Vec::with_capacity((width * height) as usize);
    {
        let mut encoder = Encoder::new(&mut buf, width, height);
        encoder.set_color(ColorType::Rgba);
        encoder.set_depth(BitDepth::Eight);
        let mut writer = encoder.write_header().map_err(|_| ())?;
        writer.write_image_data(rgba).map_err(|_| ())?;
    }
    Ok(buf)
}

fn empty_sha256() -> String {
    let hasher = Sha256::new();
    hex::encode(hasher.finalize())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::protocol::RectPx;

    #[test]
    fn popup_composition_clips_at_window_bounds_and_preserves_pixel_origin() {
        let target = RectPx::from_ltrb(-100, 20, -96, 23);
        let popup = RectPx::from_ltrb(-102, 21, -98, 24);
        let mut dst = vec![0; 4 * 3 * 4];
        let src: Vec<u8> = (1..=12).flat_map(|v| [v, v, v, 255]).collect();
        composite_rgba(&mut dst, &target, &src, &popup);
        assert_eq!(&dst[16..24], &[3,3,3,255,4,4,4,255]);
        assert_eq!(&dst[32..40], &[7,7,7,255,8,8,8,255]);
        assert_eq!(&dst[..16], &[0;16]);
        assert_eq!(&dst[24..32], &[0;8]);
    }
}

struct ScreenDcGuard(HWND, HDC);
impl Drop for ScreenDcGuard {
    fn drop(&mut self) {
        unsafe {
            let _ = ReleaseDC(Some(self.0), self.1);
        }
    }
}

struct MemDcGuard(HDC);
impl Drop for MemDcGuard {
    fn drop(&mut self) {
        unsafe {
            let _ = DeleteDC(self.0);
        }
    }
}

struct BitmapGuard(HBITMAP);
impl Drop for BitmapGuard {
    fn drop(&mut self) {
        unsafe {
            let _ = DeleteObject(HGDIOBJ::from(self.0));
        }
    }
}

struct SelectObjectGuard(HDC, HGDIOBJ);
impl Drop for SelectObjectGuard {
    fn drop(&mut self) {
        if !self.1 .0.is_null() {
            unsafe {
                let _ = SelectObject(self.0, self.1);
            }
        }
    }
}
