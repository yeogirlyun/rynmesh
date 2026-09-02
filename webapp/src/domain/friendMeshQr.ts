import QRCode from "qrcode";

/** Generate an on-device QR data URL. This function has no network code path. */
export function friendInviteQrDataUrl(value: string): string {
  const qr = QRCode.create(value, { errorCorrectionLevel: "M" });
  const border = 4;
  const size = qr.modules.size;
  const paths: string[] = [];
  for (let row = 0; row < size; row += 1) {
    for (let column = 0; column < size; column += 1) {
      if (qr.modules.get(row, column)) paths.push(`M${column + border} ${row + border}h1v1h-1z`);
    }
  }
  const viewBox = size + border * 2;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${viewBox} ${viewBox}" shape-rendering="crispEdges"><rect width="100%" height="100%" fill="white"/><path d="${paths.join("")}" fill="black"/></svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}
