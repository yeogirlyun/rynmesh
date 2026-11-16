use crate::errors::{DiagnosticReason, ErrorCode, ProtocolError};
use crate::protocol::{
    ScreenshotArtifact, ScreenshotOptions, ScreenshotUnavailableReason, WindowGeometry,
    MAX_PNG_BYTES,
};
use crate::windows::{
    enumerate_top_level_hwnds, get_window_geometry, get_window_pid_tid, hwnd_to_u64,
    is_hwnd_related_to_target, is_window_capture_protected, is_window_cloaked,
};
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

pub struct ScreenshotCaptureProbe {
    pub hwnd_u64: Option<u64>,
    pub minimized: bool,
    pub visible: bool,
    pub cloaked: bool,
    pub capture_protected: bool,
    pub display_affinity_protected: bool,
    pub occluded: bool,
    pub max_png_bytes: usize,
    pub print_result: Result<(Vec<u8>, &'static str), ()>,
}

impl ScreenshotCaptureProbe {
    pub fn healthy_non_blank(width: u32, height: u32) -> Self {
        let pixel_count = (width.max(1) as usize) * (height.max(1) as usize);
        let mut rgba = vec![0u8; pixel_count * 4];
        for px in rgba.chunks_exact_mut(4) {
            px[0] = 64;
            px[1] = 128;
            px[2] = 192;
            px[3] = 255;
        }
        Self {
            hwnd_u64: Some(0x1001),
            minimized: false,
            visible: true,
            cloaked: false,
            capture_protected: false,
            display_affinity_protected: false,
            occluded: false,
            max_png_bytes: MAX_PNG_BYTES,
            print_result: Ok((rgba, "print_window_full_content")),
        }
    }
}

pub fn try_capture_window_screenshot_with_probe(
    geometry: &WindowGeometry,
    options: &ScreenshotOptions,
    artifact_id: String,
    probe: ScreenshotCaptureProbe,
) -> Result<ScreenshotArtifact, ProtocolError> {
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

    let make_unavailable = |status: &str,
                            method: &str,
                            w: u32,
                            h: u32,
                            min: bool,
                            prot: bool,
                            reason: Option<ScreenshotUnavailableReason>| {
        ScreenshotArtifact {
            id: artifact_id.clone(),
            status: status.to_string(),
            capture_method: method.to_string(),
            mime_type: "image/png".to_string(),
            width: w,
            height: h,
            origin_x,
            origin_y,
            z_index,
            dpi: geometry.dpi,
            scale_factor: geometry.scale_factor,
            sha256: empty_sha256(),
            data_base64: None,
            minimized: min,
            occluded: probe.occluded,
            capture_protected: prot,
            reason,
        }
    };

    if !enabled {
        return Ok(make_unavailable(
            "unavailable",
            "none",
            0,
            0,
            probe.minimized,
            probe.capture_protected,
            Some(ScreenshotUnavailableReason::Disabled),
        ));
    }

    if probe.minimized {
        return Ok(make_unavailable(
            "minimized",
            "none",
            0,
            0,
            true,
            probe.capture_protected,
            None,
        ));
    }

    if !probe.visible {
        return Ok(make_unavailable(
            "unavailable",
            "none",
            0,
            0,
            false,
            probe.capture_protected,
            Some(ScreenshotUnavailableReason::NotVisible),
        ));
    }

    if probe.cloaked {
        return Ok(make_unavailable(
            "unavailable",
            "none",
            0,
            0,
            false,
            probe.capture_protected,
            Some(ScreenshotUnavailableReason::Cloaked),
        ));
    }

    if width > 8192 || height > 8192 {
        return Ok(make_unavailable(
            "unavailable",
            "none",
            0,
            0,
            false,
            probe.capture_protected,
            Some(ScreenshotUnavailableReason::TooLarge),
        ));
    }

    if width == 0 || height == 0 {
        return Ok(make_unavailable(
            "unavailable",
            "none",
            0,
            0,
            false,
            probe.capture_protected,
            Some(ScreenshotUnavailableReason::PrintFailed),
        ));
    }

    match probe.print_result {
        Ok((rgba, method_name)) => {
            if is_buffer_completely_blank(&rgba) {
                if probe.display_affinity_protected || probe.capture_protected {
                    return Err(ProtocolError::observe(
                        ErrorCode::WindowCaptureProtected,
                        DiagnosticReason::WindowCaptureProtected,
                    )
                    .with_hwnds(probe.hwnd_u64, None));
                }
                return Ok(make_unavailable(
                    "unavailable",
                    method_name,
                    width,
                    height,
                    false,
                    false,
                    Some(ScreenshotUnavailableReason::Blank),
                ));
            }

            match encode_rgba_to_png(&rgba, width, height) {
                Ok(png_bytes) => {
                    if png_bytes.len() > probe.max_png_bytes {
                        return Ok(make_unavailable(
                            "unavailable",
                            method_name,
                            width,
                            height,
                            false,
                            probe.capture_protected,
                            Some(ScreenshotUnavailableReason::PngTooLarge),
                        ));
                    }
                    let mut hasher = Sha256::new();
                    hasher.update(&png_bytes);
                    let sha256 = hex::encode(hasher.finalize());
                    let b64 = BASE64_STANDARD.encode(&png_bytes);

                    Ok(ScreenshotArtifact {
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
                        occluded: probe.occluded,
                        capture_protected: false,
                        reason: None,
                    })
                }
                Err(_) => Ok(make_unavailable(
                    "unavailable",
                    method_name,
                    0,
                    0,
                    false,
                    probe.capture_protected,
                    Some(ScreenshotUnavailableReason::PrintFailed),
                )),
            }
        }
        Err(()) => {
            if probe.display_affinity_protected || probe.capture_protected {
                return Err(ProtocolError::observe(
                    ErrorCode::WindowCaptureProtected,
                    DiagnosticReason::WindowCaptureProtected,
                )
                .with_hwnds(probe.hwnd_u64, None));
            }
            Ok(make_unavailable(
                "unavailable",
                "none",
                0,
                0,
                false,
                false,
                Some(ScreenshotUnavailableReason::PrintFailed),
            ))
        }
    }
}

pub fn try_capture_window_screenshot(
    hwnd: HWND,
    geometry: &WindowGeometry,
    options: &ScreenshotOptions,
    artifact_id: String,
) -> Result<ScreenshotArtifact, ProtocolError> {
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
    let minimized = unsafe { IsIconic(hwnd).as_bool() };
    let visible = unsafe { IsWindowVisible(hwnd).as_bool() };
    let cloaked = is_window_cloaked(hwnd);
    let capture_protected = is_window_capture_protected(hwnd);
    let occluded = check_window_center_occluded(hwnd, &target_rect);

    let print_result = if enabled
        && !minimized
        && visible
        && !cloaked
        && width > 0
        && height > 0
        && width <= 8192
        && height <= 8192
    {
        match capture_via_print_window(hwnd, width, height, client_only) {
            Ok((mut rgba, mut method_name)) => {
                if !is_buffer_completely_blank(&rgba)
                    && composite_owned_popups(hwnd, &target_rect, &mut rgba)
                {
                    method_name = "print_window_with_owned_popups";
                }
                Ok((rgba, method_name))
            }
            Err(()) => Err(()),
        }
    } else {
        Err(())
    };
    let post_affinity_protected = is_window_capture_protected(hwnd);

    let probe = ScreenshotCaptureProbe {
        hwnd_u64: Some(hwnd_to_u64(hwnd)),
        minimized,
        visible,
        cloaked,
        capture_protected,
        display_affinity_protected: post_affinity_protected,
        occluded,
        max_png_bytes: MAX_PNG_BYTES,
        print_result,
    };
    try_capture_window_screenshot_with_probe(geometry, options, artifact_id, probe)
}

/// Phase-1 window-scoped PNG screenshot candidate using `PrintWindow` (`PW_RENDERFULLCONTENT`).
/// Never silently crops the desktop screen buffer when another window occludes the target;
/// instead reports `minimized`, `capture_protected`, or `unavailable` status honestly.
pub fn capture_window_screenshot(
    hwnd: HWND,
    geometry: &WindowGeometry,
    options: &ScreenshotOptions,
    artifact_id: String,
) -> ScreenshotArtifact {
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
    let occluded = check_window_center_occluded(hwnd, &target_rect);
    match try_capture_window_screenshot(hwnd, geometry, options, artifact_id.clone()) {
        Ok(art) => art,
        Err(err) if err.code == ErrorCode::WindowCaptureProtected => ScreenshotArtifact {
            id: artifact_id,
            status: "capture_protected".to_string(),
            capture_method: "none".to_string(),
            mime_type: "image/png".to_string(),
            width: 0,
            height: 0,
            origin_x: target_rect.left,
            origin_y: target_rect.top,
            z_index: 0,
            dpi: geometry.dpi,
            scale_factor: geometry.scale_factor,
            sha256: empty_sha256(),
            data_base64: None,
            minimized: false,
            occluded,
            capture_protected: true,
            reason: Some(ScreenshotUnavailableReason::Blank),
        },
        Err(_) => ScreenshotArtifact {
            id: artifact_id,
            status: "unavailable".to_string(),
            capture_method: "none".to_string(),
            mime_type: "image/png".to_string(),
            width: 0,
            height: 0,
            origin_x: target_rect.left,
            origin_y: target_rect.top,
            z_index: 0,
            dpi: geometry.dpi,
            scale_factor: geometry.scale_factor,
            sha256: empty_sha256(),
            data_base64: None,
            minimized: false,
            occluded,
            capture_protected: false,
            reason: Some(ScreenshotUnavailableReason::PrintFailed),
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
